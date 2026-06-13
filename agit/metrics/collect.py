"""Collect per-commit statistics from git history and aGiT metadata (#54).

Every aGiT commit carries a ``# aGiT Metadata`` block (``commit_type``,
``backend``, ``model``, ``tokens_since_last_commit_*``, ``covered_commits``,
…) in its message, so the whole dashboard can be computed from ``git log``
alone — no extra state files, and it works on any clone of the repository.

Commit classification:

``agent``
    A commit aGiT created from the agent's work (``commit_type: agent``),
    including the merge-shaped cover commits placed on top of backend-made
    commits (#58).
``covered``
    A commit the backend made itself (``git commit`` run by the agent); it has
    no metadata of its own but is listed in some aGiT commit's
    ``covered_commits`` line. AI work.
``agent-merge``
    An integration merge whose conflicts an agent resolved.
``user``
    A user commit created through aGiT (``commit_type: user``). Non-tracked
    lines: even a user-made commit may contain lines an agent produced, so it is
    not claimed as human work.
``agit-ops``
    aGiT's own integration plumbing — the auto-generated merge commits it makes
    to bring base into a session branch (or back). Tracked, but its merges carry
    no diff, so it contributes no lines.
``untracked``
    No aGiT metadata and not covered — made outside aGiT. Non-tracked lines.

Line provenance is reported two ways only: aGiT-tracked AI (agent + covered +
agent-merge) versus non-tracked (user + untracked); there is no "human" bucket.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from functools import cached_property

from agit.commits import METADATA_HEADER
from agit.git import GitRepo

_NUMSTAT_RE = re.compile(r"^(\d+|-)\t(\d+|-)\t")
_TOKEN_KEY_PREFIX = "tokens_since_last_commit_"
_RECORD_SEP = "\x00"
_FIELD_SEP = "\x01"

# aGiT turn branches are named `agit/<backend>/<session>/t<n>`; an auto-merge
# subject naming one is aGiT's own integration plumbing.
_AGIT_BRANCH_RE = re.compile(r"agit/[^/\s']+/[^/\s']+/t\d+")

# A GitHub no-reply address (optionally `ID+`-prefixed) carries the user's
# login verbatim — the only GitHub identity that lives in `git log` itself.
_NOREPLY_RE = re.compile(
    r"^(?:\d+\+)?([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)@users\.noreply\.github\.com$",
    re.IGNORECASE,
)

# Same word-overlap rule as commit_engine._same_prompt: editing artifacts and
# rephrasings shuffle words, genuinely different prompts share few.
_WORD_RE = re.compile(r"[a-z0-9]+")
_SIMILARITY_THRESHOLD = 0.6
# A prompt repeated twice is a plausible retry; three or more near-identical
# prompts in a row suggest the conversation is going in circles.
_LOOP_MIN_RUN = 3


@dataclass
class CommitStat:
    """One commit's contribution to the dashboard."""

    sha: str
    author: str
    email: str
    subject: str
    kind: str  # agent | covered | agent-merge | user | agit-ops | untracked
    timestamp: int = 0  # commit author date, epoch seconds (every commit has one)
    started_at: str = ""  # AI conversation start, ISO-8601 UTC (agent commits)
    ended_at: str = ""  # AI conversation end, ISO-8601 UTC (agent commits)
    backend: str | None = None
    model: str | None = None
    tokens: dict[str, int] = field(default_factory=dict)
    insertions: int = 0
    deletions: int = 0
    covered_commits: list[str] = field(default_factory=list)
    prompt: str = ""  # the turn's prompt text (loop detection)
    user_prompts: list[str] = field(default_factory=list)  # trace ## User entries
    metadata_block: str = ""  # raw `# aGiT Metadata` text, for duplicate detection
    message: str = ""  # the full commit message (shown when a log entry is opened)
    # For a squash / PR-merge that concatenated several commits' metadata blocks:
    # the original commits, each parsed from its block, so their tokens and
    # model/backend usage are counted and the squash is expandable in the UI.
    constituents: list[CommitStat] = field(default_factory=list)

    @property
    def short(self) -> str:
        return self.sha[:7]

    @property
    def lines(self) -> int:
        return self.insertions + self.deletions


@dataclass
class LoopFinding:
    """A run of consecutive agent turns with near-identical prompts."""

    shas: list[str]
    prompt: str
    output_tokens: int
    within_commit: bool = False  # the same prompt repeated inside one turn's trace


@dataclass
class Dashboard:
    repo: str
    branch: str
    stats: list[CommitStat]  # oldest first
    loops: list[LoopFinding]
    sha_logins: dict[str, str] = field(default_factory=dict)  # commit SHA → GitHub login (best-effort)
    commit_base: str = ""  # GitHub commit URL prefix, or "" when no GitHub remote

    # --- derived aggregates -------------------------------------------------

    @property
    def total_commits(self) -> int:
        return len(self.stats)

    def count(self, *kinds: str) -> int:
        return sum(1 for stat in self.stats if stat.kind in kinds)

    @property
    def tracked_commits(self) -> int:
        return self.count("agent", "covered", "agent-merge", "user", "agit-ops")

    @property
    def coverage(self) -> float:
        return self.tracked_commits / self.total_commits if self.total_commits else 0.0

    def lines_changed(self, *kinds: str) -> tuple[int, int]:
        ins = sum(stat.insertions for stat in self.stats if stat.kind in kinds)
        dels = sum(stat.deletions for stat in self.stats if stat.kind in kinds)
        return ins, dels

    @property
    def ai_lines(self) -> tuple[int, int]:
        # aGiT-tracked AI work: agent commits, the backend-made commits an aGiT
        # cover commit accounts for (#58), and agent-resolved merges.
        return self.lines_changed("agent", "covered", "agent-merge")

    @property
    def nontracked_lines(self) -> tuple[int, int]:
        # Everything aGiT did not track as AI — user commits and plain commits
        # alike. We do NOT call these "human": even a user-made commit can
        # contain lines an agent produced off the record, so the only honest
        # claim is that aGiT did not track them as AI.
        return self.lines_changed("user", "untracked")

    @property
    def token_totals(self) -> dict[str, int]:
        totals: dict[str, int] = defaultdict(int)
        for stat in self.stats:
            for key, value in stat.tokens.items():
                totals[key] += value
        return dict(totals)

    @property
    def lines_per_1k_output_tokens(self) -> float | None:
        output = self.token_totals.get("output", 0)
        if not output:
            return None
        ins, dels = self.ai_lines
        return (ins + dels) / output * 1000

    def group_by(self, label_fn) -> dict[str, dict[str, int]]:
        """Aggregate agent turns by a label (backend, model). A turn's lines
        are its own plus those of the backend-made commits it covers — the
        cover commit (#58) carries the tokens while the covered commits carry
        the diff, and both belong to the same backend/model."""
        by_sha = {stat.sha: stat for stat in self.stats}
        covered_lines: dict[str, tuple[int, int]] = {}
        for stat in self.stats:
            ins, dels = stat.insertions, stat.deletions
            for short in stat.covered_commits:
                covered = next((s for s in self.stats if s.sha.startswith(short)), None)
                if covered is not None and covered.sha != stat.sha:
                    ins += covered.insertions
                    dels += covered.deletions
            covered_lines[stat.sha] = (ins, dels)

        groups: dict[str, dict[str, int]] = {}
        for stat in self.stats:
            # A squash counts each original commit it contains under that
            # original's own model/backend (so the usage is split correctly),
            # while its single combined diff is attributed to the dominant one.
            if stat.constituents:
                ai_parts = [p for p in stat.constituents if p.kind in ("agent", "covered", "agent-merge")]
                for part in ai_parts:
                    bucket = groups.setdefault(label_fn(part) or "unknown", defaultdict(int))
                    bucket["commits"] += 1
                    bucket["output_tokens"] += part.tokens.get("output", 0)
                    bucket["input_tokens"] += part.tokens.get("input", 0)
                dominant = max(ai_parts, key=lambda p: p.tokens.get("output", 0), default=None)
                if dominant is not None:
                    bucket = groups.setdefault(label_fn(dominant) or "unknown", defaultdict(int))
                    bucket["insertions"] += stat.insertions
                    bucket["deletions"] += stat.deletions
                continue
            if stat.kind != "agent":
                continue
            label = label_fn(by_sha[stat.sha]) or "unknown"
            bucket = groups.setdefault(label, defaultdict(int))
            bucket["commits"] += 1
            ins, dels = covered_lines[stat.sha]
            bucket["insertions"] += ins
            bucket["deletions"] += dels
            bucket["output_tokens"] += stat.tokens.get("output", 0)
            bucket["input_tokens"] += stat.tokens.get("input", 0)
        return {label: dict(bucket) for label, bucket in groups.items()}

    @property
    def by_backend(self) -> dict[str, dict[str, int]]:
        return self.group_by(lambda stat: stat.backend)

    @property
    def by_model(self) -> dict[str, dict[str, int]]:
        return self.group_by(lambda stat: stat.model)

    @cached_property
    def committer_labels(self) -> dict[tuple[str, str], str]:
        """Map each ``(author name, email)`` to a merged committer label, so
        name variants of one person collapse to a single identity. Cached: it is
        read once per commit during serialization."""
        return resolve_committers(self.stats, self.sha_logins)

    def label_of(self, stat: CommitStat) -> str:
        return self.committer_labels.get((stat.author or "", (stat.email or "").strip().lower())) or "unknown"

    @property
    def by_author(self) -> dict[str, dict[str, int]]:
        """Per committer, lines split into aGiT-tracked AI and non-tracked.
        Agent commits are git-authored by whoever ran aGiT but written by the
        model, so they count as that person's AI-driven lines; their user and
        plain commits are non-tracked (not claimed as human)."""
        labels = self.committer_labels
        groups: dict[str, dict[str, int]] = {}
        for stat in self.stats:
            label = labels.get((stat.author or "", (stat.email or "").strip().lower())) or "unknown"
            bucket = groups.setdefault(label, defaultdict(int))
            bucket["commits"] += 1
            if stat.kind != "untracked":
                bucket["agit_commits"] += 1
            if stat.kind in ("agent", "covered", "agent-merge"):
                bucket["ai_insertions"] += stat.insertions
                bucket["ai_deletions"] += stat.deletions
            elif stat.kind == "agit-ops":
                pass  # operational merges carry no diff; only the commit counts
            else:
                bucket["nontracked_insertions"] += stat.insertions
                bucket["nontracked_deletions"] += stat.deletions
        return {label: dict(bucket) for label, bucket in groups.items()}


# ---------------------------------------------------------------------------
# Committer identity: collapse name variants of the same person (#54)
# ---------------------------------------------------------------------------


def _login_of(email: str) -> str | None:
    match = _NOREPLY_RE.match(email.strip())
    return match.group(1).lower() if match else None


class _Union:
    """Tiny union-find over hashable nodes."""

    def __init__(self) -> None:
        self._parent: dict[object, object] = {}

    def find(self, node: object) -> object:
        self._parent.setdefault(node, node)
        root = node
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[node] != root:
            self._parent[node], node = root, self._parent[node]
        return root

    def union(self, a: object, b: object) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[ra] = rb


def resolve_committers(stats: list[CommitStat], sha_logins: dict[str, str] | None = None) -> dict[tuple[str, str], str]:
    """Map every ``(author name, email)`` pair to a single committer label.

    ``sha_logins`` (commit SHA → GitHub login, from :mod:`agit.metrics.github`)
    is the authoritative GitHub identity when available — every commit GitHub
    knows is merged under its real login. Without it (``gh`` missing, offline, no
    GitHub remote) we fall back to ``git log`` alone: identities merge when they
    share an email, a GitHub login parsed from a no-reply address, or an email
    local-part that equals a known login (linking ``user@host`` to
    ``user@users.noreply.github.com``). The label prefers the GitHub login;
    otherwise it is the person's most frequent name. Two people who never share a
    login or email but share a name cannot be told apart here and may merge."""
    sha_logins = sha_logins or {}
    union = _Union()
    logins: set[str] = set()

    def login_for(stat: CommitStat, email: str) -> str | None:
        return sha_logins.get(stat.sha) or _login_of(email)

    for stat in stats:
        email = (stat.email or "").strip().lower()
        node = ("pair", stat.author or "", email)
        union.find(node)
        if email:
            union.union(node, ("email", email))
        login = login_for(stat, email)
        if login:
            union.union(node, ("login", login.lower()))
            logins.add(login.lower())
    # Bridge a plain email to a login when its local-part is that login.
    for stat in stats:
        email = (stat.email or "").strip().lower()
        local = email.split("@", 1)[0]
        if email and local in logins:
            union.union(("email", email), ("login", local))

    root_logins: dict[object, set[str]] = defaultdict(set)
    root_names: dict[object, Counter[str]] = defaultdict(Counter)
    for stat in stats:
        email = (stat.email or "").strip().lower()
        root = union.find(("pair", stat.author or "", email))
        if stat.author:
            root_names[root][stat.author] += 1
        login = login_for(stat, email)
        if login:
            root_logins[root].add(login.lower())

    labels: dict[tuple[str, str], str] = {}
    for stat in stats:
        email = (stat.email or "").strip().lower()
        root = union.find(("pair", stat.author or "", email))
        if root_logins[root]:
            label = sorted(root_logins[root])[0]
        elif root_names[root]:
            label = root_names[root].most_common(1)[0][0]
        else:
            label = email or "unknown"
        labels[(stat.author or "", email)] = label
    return labels


def collect_commit_stats(repo: GitRepo, ref: str = "HEAD") -> list[CommitStat]:
    """Parse ``git log`` into :class:`CommitStat` records, oldest first."""
    # %x00/%x01 are git's own escapes: a literal NUL is not representable in
    # an argv string, but git happily PRINTS one as a record separator.
    log = repo._run(
        ["git", "log", "--format=%H%x01%an%x01%ae%x01%at%x01%B%x00", ref],
        check=False,
    ).stdout
    stats: list[CommitStat] = []
    for record in log.split(_RECORD_SEP):
        record = record.strip("\n")
        if not record.strip():
            continue
        sha, _, rest = record.partition(_FIELD_SEP)
        author, _, rest = rest.partition(_FIELD_SEP)
        email, _, rest = rest.partition(_FIELD_SEP)
        committed_at, _, body = rest.partition(_FIELD_SEP)
        stats.append(_parse_commit(sha.strip(), author, email, committed_at.strip(), body))

    stats = _drop_inherited_metadata(repo, ref, stats)

    _apply_numstat(repo, ref, {stat.sha: stat for stat in stats})

    # Backend-made commits have no metadata of their own; they are AI work if
    # some aGiT commit's covered_commits names them (#35/#58).
    covered_prefixes = [short for stat in stats for short in stat.covered_commits]
    for stat in stats:
        if stat.kind == "untracked" and any(stat.sha.startswith(prefix) for prefix in covered_prefixes):
            stat.kind = "covered"

    stats.reverse()  # oldest first
    _dedupe_squash_constituents(stats)
    return stats


def _dedupe_squash_constituents(stats: list[CommitStat]) -> None:
    """Count each original commit's tokens once, even when it lands in several squashes.

    Tokens are recorded per original commit, inside that commit's metadata block.
    Squashing copies the block verbatim, so if the same commit is rolled into more
    than one squash (e.g. a branch is squash-merged, then that result is squashed
    again), its block — and its tokens — appears in every such squash. Walking
    oldest first, the first squash to contain a commit keeps it; later squashes drop
    the repeat and have their token totals recomputed from the constituents that
    remain. Repeats *within* a single squash are left intact: two byte-identical
    blocks squashed together are genuine back-to-back turns, not one commit seen
    twice, so a squash's own constituents are marked seen only after it is filtered."""
    seen: set[str] = set()
    for stat in stats:
        if not stat.constituents:
            continue
        kept = [p for p in stat.constituents if not (p.metadata_block and p.metadata_block in seen)]
        seen.update(p.metadata_block for p in stat.constituents if p.metadata_block)
        if len(kept) != len(stat.constituents):
            stat.constituents = kept
            stat.tokens = _sum_tokens(kept)


def _drop_inherited_metadata(repo: GitRepo, ref: str, stats: list[CommitStat]) -> list[CommitStat]:
    """Drop commits that merely inherit a parent's aGiT metadata block.

    When a session branch is integrated with a real merge commit (e.g. a GitHub
    PR merged with "Create a merge commit"), GitHub copies the PR title and body
    into the merge commit message. The cover commit (#58) at the branch tip is
    that PR's source, so the merge ends up carrying a byte-identical copy of the
    cover's metadata — same tokens, same trace. Counting both would double every
    figure for that turn (tokens, lines-via-covered_commits).

    The fingerprint is a *merge* commit (>1 parent) that shares an identical
    metadata block with one of its parents: that parent is the cover commit it
    merges, and the block was copied wholesale. We keep the original (the parent)
    and discard the inheriting merge. Restricting to merges is what separates this
    from a genuine run of repeated turns, which is linear — each such commit has a
    single parent, so an identical neighbouring block is a real (repeated) turn,
    not a copy. Cover commits and integration merges are themselves merge-shaped
    but never share a parent's block, so they are untouched."""
    block_by_sha = {stat.sha: stat.metadata_block for stat in stats if stat.metadata_block}
    if not block_by_sha:
        return stats
    parents = _parents(repo, ref)
    duplicates = {
        stat.sha
        for stat in stats
        if stat.metadata_block
        and len(parents.get(stat.sha, ())) > 1
        and any(block_by_sha.get(parent) == stat.metadata_block for parent in parents[stat.sha])
    }
    return [stat for stat in stats if stat.sha not in duplicates]


def _parents(repo: GitRepo, ref: str) -> dict[str, list[str]]:
    output = repo._run(["git", "log", "--format=%H %P", ref], check=False).stdout
    parents: dict[str, list[str]] = {}
    for line in output.splitlines():
        sha, _, rest = line.strip().partition(" ")
        if sha:
            parents[sha] = rest.split()
    return parents


def build_dashboard(repo: GitRepo, ref: str = "HEAD", *, sha_logins: dict[str, str] | None = None) -> Dashboard:
    from agit.metrics.github import commit_url_base

    stats = collect_commit_stats(repo, ref)
    branch = repo.current_branch()
    return Dashboard(
        repo=str(repo.repo),
        branch=branch,
        stats=stats,
        loops=_detect_loops(stats),
        sha_logins=sha_logins or {},
        commit_base=commit_url_base(repo),
    )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _parse_commit(sha: str, author: str, email: str, committed_at: str, body: str) -> CommitStat:
    subject = body.splitlines()[0] if body.splitlines() else ""
    timestamp = int(committed_at) if committed_at.isdigit() else 0
    # A clean aGiT turn carries exactly one metadata block. More than one means
    # the message is an aggregate — a squash or PR merge that concatenated
    # several commits' blocks (possibly across several rounds of squashing, which
    # git flattens). Parse each original commit so its tokens and model/backend
    # usage are still counted, and so the squash can be expanded in the UI.
    if body.count(METADATA_HEADER) > 1:
        constituents = _parse_constituents(body)
        return _build_squash(sha, author, email, subject, timestamp, body, constituents)
    metadata = _parse_metadata(body)
    commit_type = metadata.get("commit_type")
    if commit_type == "agent":
        kind = "agent"
    elif commit_type == "agent-merge":
        kind = "agent-merge"
    elif commit_type == "user":
        kind = "user"
    elif METADATA_HEADER in body:
        kind = "agent"  # metadata without commit_type: treat as agent work
    elif subject.startswith("Merge ") and _AGIT_BRANCH_RE.search(subject):
        # aGiT's own integration plumbing: the auto-generated merge commits it
        # makes to bring base into a session turn branch (or back), e.g. "Merge
        # branch 'dev' into agit/claude/session-1/t2". These carry no metadata
        # but are aGiT operations, not stray human/untracked work, so they get
        # their own class (and merges contribute no numstat lines anyway).
        kind = "agit-ops"
    else:
        kind = "untracked"
    return CommitStat(
        sha=sha,
        author=author,
        email=email,
        subject=subject,
        kind=kind,
        timestamp=timestamp,
        started_at=metadata.get("agent_started_at", ""),
        ended_at=metadata.get("agent_ended_at", ""),
        backend=metadata.get("backend"),
        model=metadata.get("model"),
        tokens=_parse_tokens(metadata),
        covered_commits=(metadata.get("covered_commits") or "").split(),
        prompt=_extract_prompt(body, subject, kind),
        user_prompts=_extract_user_prompts(body),
        metadata_block=_metadata_block(body),
        message=body.strip(),
    )


def _parse_tokens(metadata: dict[str, str]) -> dict[str, int]:
    tokens: dict[str, int] = {}
    for key, value in metadata.items():
        if key.startswith(_TOKEN_KEY_PREFIX) and value.isdigit():
            tokens[key.removeprefix(_TOKEN_KEY_PREFIX)] = int(value)
        elif key in ("summary_tokens_input", "summary_tokens_output") and value.isdigit():
            tokens["summary_" + key.removeprefix("summary_tokens_")] = int(value)
    return tokens


def _kind_from_metadata(metadata: dict[str, str]) -> str:
    commit_type = metadata.get("commit_type")
    if commit_type in ("agent", "agent-merge", "user"):
        return commit_type
    return "agent"  # a metadata block without a commit_type is agent work


def _is_metadata_kv(line: str) -> bool:
    """A `key: value` metadata line (key has no spaces and isn't a `#` header)."""
    if line.startswith("#") or ":" not in line:
        return False
    key = line.split(":", 1)[0].strip()
    return bool(key) and " " not in key


def _parse_constituents(body: str) -> list[CommitStat]:
    """Split a squashed message into one :class:`CommitStat` per original commit.

    git concatenates the squashed commits' messages, so each ``# aGiT Metadata``
    block marks one original aGiT commit. A constituent spans from the end of the
    previous block to the end of its own block (its subject is the nearest ``*``
    bullet, the format GitHub's squash uses). Constituents carry the original's
    kind/backend/model/tokens/span but no line counts — the squash holds the one
    combined diff."""
    lines = body.splitlines()
    headers = [i for i, line in enumerate(lines) if line.strip() == METADATA_HEADER]
    constituents: list[CommitStat] = []
    prev_end = 0
    for header in headers:
        end = len(lines)
        for j in range(header + 1, len(lines)):
            if not _is_metadata_kv(lines[j]):
                end = j
                break
        segment = lines[prev_end:end]
        constituents.append(_constituent(segment))
        prev_end = end
    return constituents


def _constituent(segment_lines: list[str]) -> CommitStat:
    text = "\n".join(segment_lines).strip()
    metadata = _parse_metadata(text)
    subject = next(
        (line[2:].strip() for line in segment_lines if line.startswith("* ")),
        next((line for line in segment_lines if line.strip() and not line.startswith("#")), ""),
    )
    return CommitStat(
        sha="",
        author="",
        email="",
        subject=subject,
        kind=_kind_from_metadata(metadata),
        started_at=metadata.get("agent_started_at", ""),
        ended_at=metadata.get("agent_ended_at", ""),
        backend=metadata.get("backend"),
        model=metadata.get("model"),
        tokens=_parse_tokens(metadata),
        # The metadata block identifies the original commit this constituent was
        # parsed from: if the same commit is captured in more than one squash, the
        # block is byte-identical, which lets us count its tokens only once.
        metadata_block=_metadata_block(text),
        message=text,
    )


def _build_squash(
    sha: str,
    author: str,
    email: str,
    subject: str,
    timestamp: int,
    body: str,
    constituents: list[CommitStat],
) -> CommitStat:
    """The parent stat for a squash: it keeps the combined diff and carries the
    summed tokens of its constituents, classified by what they contain (any AI
    constituent ⇒ aGiT-tracked AI). Its backend/model is the dominant agent
    constituent's, for line attribution and display."""
    tokens = _sum_tokens(constituents)
    ai_parts = [part for part in constituents if part.kind in ("agent", "covered", "agent-merge")]
    if ai_parts:
        kind = "agent"
    elif any(part.kind == "user" for part in constituents):
        kind = "user"
    else:
        kind = "untracked"
    dominant = max(ai_parts, key=lambda part: part.tokens.get("output", 0), default=None)
    return CommitStat(
        sha=sha,
        author=author,
        email=email,
        subject=subject,
        kind=kind,
        timestamp=timestamp,
        backend=dominant.backend if dominant else None,
        model=dominant.model if dominant else None,
        tokens=dict(tokens),
        message=body.strip(),
        constituents=constituents,
    )


def _sum_tokens(parts: list[CommitStat]) -> dict[str, int]:
    tokens: dict[str, int] = defaultdict(int)
    for part in parts:
        for key, value in part.tokens.items():
            tokens[key] += value
    return dict(tokens)


def _metadata_block(body: str) -> str:
    """The `# aGiT Metadata` section verbatim (it is always the last section).

    A GitHub PR merge commit copies its PR's title and body — and so the whole
    metadata block — into the merge commit message, byte for byte. Capturing the
    block lets :func:`collect_commit_stats` recognise such a merge as a duplicate
    of the cover commit it merges and avoid counting the turn's tokens twice."""
    lines = body.splitlines()
    try:
        start = lines.index(METADATA_HEADER)
    except ValueError:
        return ""
    return "\n".join(lines[start:]).strip()


def _parse_metadata(body: str) -> dict[str, str]:
    lines = body.splitlines()
    try:
        start = lines.index(METADATA_HEADER)
    except ValueError:
        return {}
    metadata: dict[str, str] = {}
    for line in lines[start + 1 :]:
        if line.startswith("#"):
            break
        key, sep, value = line.partition(":")
        if sep and key.strip() and " " not in key.strip():
            metadata[key.strip()] = value.strip()
    return metadata


def _section(body: str, header: str) -> list[str]:
    lines = body.splitlines()
    try:
        start = lines.index(header)
    except ValueError:
        return []
    collected: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("# ") or line.startswith("## "):
            break
        collected.append(line)
    return collected


def _extract_prompt(body: str, subject: str, kind: str) -> str:
    if kind != "agent":
        return ""
    # When a summary leads the message (#8) the turn's prompts live in the
    # "# Prompts" section; otherwise the subject IS the prompt.
    prompts = " ".join(line.strip() for line in _section(body, "# Prompts") if line.strip())
    if prompts:
        return prompts
    return subject.removeprefix("<aGiT> ").strip()


def _extract_user_prompts(body: str) -> list[str]:
    if "# Interaction Trace" not in body:
        return []
    prompts: list[str] = []
    lines = body.splitlines()
    index = 0
    while index < len(lines):
        if lines[index].strip() == "## User":
            index += 1
            entry: list[str] = []
            while index < len(lines) and not lines[index].startswith(("## ", "# ")):
                entry.append(lines[index].strip())
                index += 1
            text = " ".join(part for part in entry if part)
            if text:
                prompts.append(text)
        else:
            index += 1
    return prompts


def _apply_numstat(repo: GitRepo, ref: str, by_sha: dict[str, CommitStat]) -> None:
    # Merge commits (cover commits #58, integration merges) report no numstat
    # by default, so a turn's lines are counted exactly once — on the commits
    # that introduced them.
    output = repo._run(["git", "log", "--numstat", "--format=%x01%H", ref], check=False).stdout
    current: CommitStat | None = None
    for line in output.splitlines():
        if line.startswith(_FIELD_SEP):
            current = by_sha.get(line[1:].strip())
            continue
        match = _NUMSTAT_RE.match(line)
        if match and current is not None:
            if match.group(1) != "-":
                current.insertions += int(match.group(1))
            if match.group(2) != "-":
                current.deletions += int(match.group(2))


# ---------------------------------------------------------------------------
# Loop detection (#54): near-identical prompts burn tokens without progress
# ---------------------------------------------------------------------------


def _similar(a: str, b: str) -> bool:
    """Word-overlap match, the same rule as commit_engine._same_prompt."""
    norm_a, norm_b = " ".join(a.lower().split()), " ".join(b.lower().split())
    if not norm_a or not norm_b:
        return False
    if norm_a == norm_b:
        return True
    words_a, words_b = set(_WORD_RE.findall(norm_a)), set(_WORD_RE.findall(norm_b))
    if not words_a or not words_b:
        return False
    return len(words_a & words_b) / len(words_a | words_b) >= _SIMILARITY_THRESHOLD


def _detect_loops(stats: list[CommitStat]) -> list[LoopFinding]:
    findings: list[LoopFinding] = []

    # Across turns: consecutive agent commits re-asking ~the same thing.
    agents = [stat for stat in stats if stat.kind == "agent" and stat.prompt]
    run: list[CommitStat] = []
    for stat in agents:
        if run and _similar(run[-1].prompt, stat.prompt):
            run.append(stat)
            continue
        if len(run) >= _LOOP_MIN_RUN:
            findings.append(_finding(run))
        run = [stat]
    if len(run) >= _LOOP_MIN_RUN:
        findings.append(_finding(run))

    # Within a turn: the trace shows the user repeating the same prompt.
    for stat in stats:
        if stat.kind != "agent" or len(stat.user_prompts) < _LOOP_MIN_RUN:
            continue
        longest = 1
        current = 1
        for previous, prompt in zip(stat.user_prompts, stat.user_prompts[1:]):
            current = current + 1 if _similar(previous, prompt) else 1
            longest = max(longest, current)
        if longest >= _LOOP_MIN_RUN:
            findings.append(
                LoopFinding(
                    shas=[stat.short],
                    prompt=stat.user_prompts[0],
                    output_tokens=stat.tokens.get("output", 0),
                    within_commit=True,
                )
            )
    return findings


def _finding(run: list[CommitStat]) -> LoopFinding:
    return LoopFinding(
        shas=[stat.short for stat in run],
        prompt=run[0].prompt,
        output_tokens=sum(stat.tokens.get("output", 0) for stat in run),
    )
