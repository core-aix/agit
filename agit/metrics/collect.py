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
    A user commit created through aGiT (``commit_type: user``). Human work.
``untracked``
    No aGiT metadata and not covered — made outside aGiT. Human work.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from agit.commits import METADATA_HEADER
from agit.git import GitRepo

_NUMSTAT_RE = re.compile(r"^(\d+|-)\t(\d+|-)\t")
_TOKEN_KEY_PREFIX = "tokens_since_last_commit_"
_RECORD_SEP = "\x00"
_FIELD_SEP = "\x01"

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
    subject: str
    kind: str  # agent | covered | agent-merge | user | untracked
    backend: str | None = None
    model: str | None = None
    tokens: dict[str, int] = field(default_factory=dict)
    insertions: int = 0
    deletions: int = 0
    covered_commits: list[str] = field(default_factory=list)
    prompt: str = ""  # the turn's prompt text (loop detection)
    user_prompts: list[str] = field(default_factory=list)  # trace ## User entries
    metadata_block: str = ""  # raw `# aGiT Metadata` text, for duplicate detection

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

    # --- derived aggregates -------------------------------------------------

    @property
    def total_commits(self) -> int:
        return len(self.stats)

    def count(self, *kinds: str) -> int:
        return sum(1 for stat in self.stats if stat.kind in kinds)

    @property
    def tracked_commits(self) -> int:
        return self.count("agent", "covered", "agent-merge", "user")

    @property
    def coverage(self) -> float:
        return self.tracked_commits / self.total_commits if self.total_commits else 0.0

    def lines_changed(self, *kinds: str) -> tuple[int, int]:
        ins = sum(stat.insertions for stat in self.stats if stat.kind in kinds)
        dels = sum(stat.deletions for stat in self.stats if stat.kind in kinds)
        return ins, dels

    @property
    def ai_lines(self) -> tuple[int, int]:
        # AI work: agent commits, the backend-made commits an aGiT cover commit
        # accounts for (#58), and agent-resolved merges.
        return self.lines_changed("agent", "covered", "agent-merge")

    @property
    def human_lines(self) -> tuple[int, int]:
        # True human work: only user commits made *inside* aGiT carry the
        # metadata that proves a human authored them.
        return self.lines_changed("user")

    @property
    def nontracked_lines(self) -> tuple[int, int]:
        # No aGiT metadata: could be a human commit made outside aGiT, or an
        # agent's commit that was never tracked. Deliberately NOT called human.
        return self.lines_changed("untracked")

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

    @property
    def by_author(self) -> dict[str, dict[str, int]]:
        groups: dict[str, dict[str, int]] = {}
        for stat in self.stats:
            bucket = groups.setdefault(stat.author or "unknown", defaultdict(int))
            bucket["commits"] += 1
            if stat.kind != "untracked":
                bucket["agit_commits"] += 1
            bucket["insertions"] += stat.insertions
            bucket["deletions"] += stat.deletions
        return {label: dict(bucket) for label, bucket in groups.items()}


def collect_commit_stats(repo: GitRepo, ref: str = "HEAD") -> list[CommitStat]:
    """Parse ``git log`` into :class:`CommitStat` records, oldest first."""
    # %x00/%x01 are git's own escapes: a literal NUL is not representable in
    # an argv string, but git happily PRINTS one as a record separator.
    log = repo._run(
        ["git", "log", "--format=%H%x01%an%x01%B%x00", ref],
        check=False,
    ).stdout
    stats: list[CommitStat] = []
    for record in log.split(_RECORD_SEP):
        record = record.strip("\n")
        if not record.strip():
            continue
        sha, _, rest = record.partition(_FIELD_SEP)
        author, _, body = rest.partition(_FIELD_SEP)
        stats.append(_parse_commit(sha.strip(), author, body))

    stats = _drop_inherited_metadata(repo, ref, stats)

    _apply_numstat(repo, ref, {stat.sha: stat for stat in stats})

    # Backend-made commits have no metadata of their own; they are AI work if
    # some aGiT commit's covered_commits names them (#35/#58).
    covered_prefixes = [short for stat in stats for short in stat.covered_commits]
    for stat in stats:
        if stat.kind == "untracked" and any(stat.sha.startswith(prefix) for prefix in covered_prefixes):
            stat.kind = "covered"

    stats.reverse()  # oldest first
    return stats


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


def build_dashboard(repo: GitRepo, ref: str = "HEAD") -> Dashboard:
    stats = collect_commit_stats(repo, ref)
    branch = repo.current_branch()
    return Dashboard(repo=str(repo.repo), branch=branch, stats=stats, loops=_detect_loops(stats))


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _parse_commit(sha: str, author: str, body: str) -> CommitStat:
    subject = body.splitlines()[0] if body.splitlines() else ""
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
    else:
        kind = "untracked"
    tokens: dict[str, int] = {}
    for key, value in metadata.items():
        if key.startswith(_TOKEN_KEY_PREFIX) and value.isdigit():
            tokens[key.removeprefix(_TOKEN_KEY_PREFIX)] = int(value)
        elif key in ("summary_tokens_input", "summary_tokens_output") and value.isdigit():
            tokens["summary_" + key.removeprefix("summary_tokens_")] = int(value)
    return CommitStat(
        sha=sha,
        author=author,
        subject=subject,
        kind=kind,
        backend=metadata.get("backend"),
        model=metadata.get("model"),
        tokens=tokens,
        covered_commits=(metadata.get("covered_commits") or "").split(),
        prompt=_extract_prompt(body, subject, kind),
        user_prompts=_extract_user_prompts(body),
        metadata_block=_metadata_block(body),
    )


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
