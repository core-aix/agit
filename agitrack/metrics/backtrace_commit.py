"""``agitrack --backtrace commit``: rewrite a repository's history onto a NEW branch so that the
commits which correspond to AI-made changes carry the aGiTrack metadata + interaction trace they
would have had if the sessions had been run with aGiTrack — reconstructed from local transcripts.

It answers "I built this project with a coding agent but WITHOUT aGiTrack; can I still get the
tracked history?" — yes: this replays every existing commit onto a new branch (same trees, same
authors/dates, same structure), and for each commit whose files an AI turn produced, appends the
turn's ``# Interaction Trace`` and ``# aGiTrack Metadata`` (backend, model, tokens, timings). User
commits with no AI correspondence are copied verbatim.

Because it rewrites history (every commit gets a new SHA), it is destructive-adjacent: it only
runs on a NEW branch, requires a clean working tree, and never touches the original branch — the
user force-replaces the old branch themselves, after reviewing, if they want to.

AI-vs-user attribution: a commit is "AI" when its changed files overlap the files an agent turn
edited (matched to the earliest commit at/after the turn's time that contains those files). A
commit no turn explains is a user commit.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from agitrack.backends.base import TokenUsage
from agitrack.commits import METADATA_HEADER, carries_ai_history
from agitrack.transcripts import capabilities
from agitrack.commits.message import _trace_and_metadata_lines
from agitrack.metrics import backtrace as bt
from agitrack.metrics import files as filesmod
from agitrack.metrics.collect import _abbreviate_home


@dataclass
class _TurnRec:
    """The bits of one agent turn that matter for reconstructing a commit."""

    ended_at: int
    started_at: int | None
    files: set[str]
    backend: str
    model: str | None
    tokens: TokenUsage
    user_prompt: str
    final_response: str
    queued_followups: list[str] = field(default_factory=list)
    session_id: str = ""
    reasoning_effort: str | None = None
    # The extensions this turn used (MCP servers/tools, skills, sub-agents) — carried through so a
    # backtraced commit records the same provenance a live aGiTrack commit would.
    mcp_servers: list[str] = field(default_factory=list)
    mcp_tools: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    subagents: list[str] = field(default_factory=list)
    # The turn's true identity, from the backend API. Resuming or rewinding a conversation
    # FORKS it into a new session that replays the earlier turns, so the same turn is read
    # several times; this is what tells those copies apart from genuinely distinct work.
    assistant_id: str = ""
    turn_index: int = 0  # position within its session, for the committed-anchor cutoff


def backtrace_commit(directory: Path, new_branch: str, *, _input=input) -> int:
    """Entry point for ``agitrack --backtrace commit --backtrace-branch <new_branch>``.

    Always interactive — it rewrites history, so it prompts for confirmation and has no
    skip-confirmation flag. Returns a process exit code. Prints all user-facing guidance itself
    (git-init hint, dirty-tree hint, the warning + confirmation, the progress bar, and the
    force-replace instructions). ``_input`` is injectable only so tests can drive the prompt.

    HOLDS THE REPO'S SINGLE-WRITER LOCK for the whole run, like every other aGiTrack path that
    writes git. It is the heaviest writer there is — it replays every commit onto a new branch —
    and it was the one path that took no lock, so it could run beside a live tracker or TUI. Two
    concrete ways that ends badly: the tracker commits a turn between the clean-tree check and
    the replay, so the reconstruction is built from a tree that no longer exists; or the replay's
    `git switch` moves HEAD out from under a session that is mid-commit. Read-only paths (the
    dashboards, `--backtrace` serving) take no lock and are unaffected."""
    from agitrack.git import GitError, GitRepo, RepoLock, already_running_message

    directory = directory.expanduser().resolve()

    # 1) Must be a git repository — a plain directory can't hold reconstructed commits.
    try:
        repo = GitRepo.discover(directory)
    except (GitError, OSError):
        print(
            f"{_abbreviate_home(str(directory))} is not a git repository.\n"
            "`--backtrace commit` writes real commits, so the directory must be a repo first. "
            "Initialize one and commit your current files, then re-run:\n"
            "    git init\n"
            "    git add -A && git commit -m 'initial snapshot'\n"
            "    agitrack --backtrace commit --backtrace-branch <new-branch>"
        )
        return 1
    root = repo.repo

    # 1b) Take the single-writer lock before anything is inspected, so every check below and the
    #     replay itself see one consistent repository. Acquired here rather than just before the
    #     rewrite because the checks are part of the decision: a tree that was clean when we
    #     looked is worthless if a tracker commits into it while the user reads the warning.
    #     The lock file lives in `.agitrack/`, which this command's target repo has by definition
    #     never had — so git-ignore it FIRST (in `.git/info/exclude`, not the user's `.gitignore`)
    #     or taking the lock would itself dirty the tree that step 3 requires to be clean.
    from agitrack.config import AgitrackState

    AgitrackState(root).ensure_local_ignore()
    lock = RepoLock(root / ".agitrack" / "lock")
    if not lock.acquire():
        print(already_running_message(lock.owner_pid(), repo_root=root))
        return 1
    try:
        return _backtrace_commit_locked(repo, root, new_branch, _input=_input)
    finally:
        lock.release()


def _backtrace_commit_locked(repo, root: Path, new_branch: str, *, _input=input) -> int:
    """The body of :func:`backtrace_commit`, with the repo lock already held."""
    # 2) A new branch name is required (this never writes to the current branch). The command is
    #    interactive anyway, so ASK rather than exit with instructions: printing a flag and
    #    quitting was a dead end for anyone who mistyped it, because `parse_known_args` funnels an
    #    unknown flag to the backend instead of erroring — the same message came back forever.
    new_branch = (new_branch or "").strip()
    if not new_branch:
        print(
            "The reconstruction rewrites history, so it is placed on its own NEW branch. Your "
            "current branch keeps every commit it has now — but aGiTrack DOES check the new "
            "branch out at the end, so you will be standing on it (it tells you how to switch "
            "back)."
        )
        try:
            new_branch = (_input("Name for the new branch (blank to cancel): ") or "").strip()
        except (EOFError, KeyboardInterrupt):
            new_branch = ""
        if not new_branch:
            print(
                "Cancelled. You can also pass it up front:\n"
                "    agitrack --backtrace commit --backtrace-branch <new-branch>"
            )
            return 1
    if _branch_exists(repo, new_branch):
        print(f"Branch '{new_branch}' already exists. Choose a new branch name that does not exist yet.")
        return 1

    # 3) The working tree must be clean — a rewrite that carried uncommitted edits would be
    #    unreconstructable and unsafe. Instruct the user to commit or ignore everything first.
    dirty = repo._run(["git", "status", "--porcelain"], check=False).stdout.strip()
    if dirty:
        print(
            "Your working tree has uncommitted changes:\n\n"
            + "\n".join("    " + line for line in dirty.splitlines())
            + "\n\n`--backtrace commit` rewrites history and must start from a CLEAN tree. Commit the "
            "changes you want to keep, or add them to .gitignore (then `git rm --cached` anything "
            "already tracked), until `git status` is clean, and re-run."
        )
        return 1

    # 4) Reconstruct the agent turns (with the files each changed) from local transcripts.
    turns = [t for t in _gather_turns(root) if t.files]
    if not turns:
        from agitrack.backends.proxy_agents import backend_phrase

        print(
            f"No AI-made file changes were found in local {backend_phrase()} transcripts for "
            f"{_abbreviate_home(str(root))}.\nThere is nothing to reconstruct — this command annotates "
            "commits whose changes an agent produced."
        )
        return 0

    # 5) The commits to replay (oldest first), and which of them are AI-made.
    commits = _commits_oldest_first(repo)
    if not commits:
        print("This repository has no commits yet — nothing to reconstruct.")
        return 0
    changed = filesmod._numstat_by_commit(repo, set(commits))
    ai_map = _match_turns_to_commits(repo, commits, changed, turns)
    # AI commits that already record AI WORK (a repo that used aGiTrack for part of its life) are
    # left untouched — only agent-made commits without that record are annotated. The test is
    # `carries_ai_history`, NOT "has a metadata block": a plain USER commit made through aGiTrack
    # carries an attribution-only block with no turn, no trace and no tokens, and reading that as
    # "already tracked" made the whole command dead-end with "Nothing to add" on exactly the repos
    # it exists for — someone who ran aGiTrack, committed once, and has all their real history
    # sitting in the backend transcripts.
    already_tracked = {sha for sha in ai_map if carries_ai_history(_commit_message(repo, sha))}
    to_annotate = len(ai_map) - len(already_tracked)
    if to_annotate == 0:
        kept = "already tracked" if already_tracked else "user commits with no agent-made edits to attribute"
        print(
            f"Nothing to add: every agent-made commit here is {kept}. "
            "The reconstructed transcripts turned up no untracked AI work to annotate."
        )
        return 0

    # 6) Warn (history rewrite, new SHAs, force needed) and confirm.
    already_line = (
        f"  • {len(already_tracked)} agent commit(s) already carry aGiTrack metadata and are kept as-is.\n"
        if already_tracked
        else ""
    )
    print(
        f"Reconstructing tracked history for {_abbreviate_home(str(root))}:\n"
        f"  • {len(commits)} commit(s) will be replayed onto a new branch '{new_branch}'.\n"
        f"  • {to_annotate} agent commit(s) NOT yet tracked will gain aGiTrack metadata (backend, model, "
        f"tokens, and the user-agent trace) from {len(turns)} reconstructed agent turn(s).\n"
        + already_line
        + f"  • {len(commits) - len(ai_map)} will be kept verbatim as user commits.\n\n"
        "This REWRITES history: every commit gets a new hash, so the new branch is NOT a "
        "fast-forward of your current branch. Your current branch keeps every commit it has "
        "now — nothing on it is changed or lost — but your CHECKOUT moves: aGiTrack switches "
        "you onto the new branch at the end, and prints how to switch back."
    )
    # Guarded exactly like the branch-name prompt above: run from a script, a CI job or any
    # pipeline (`agitrack --backtrace commit --branch x < /dev/null`) `input()` raises EOFError,
    # which escaped as a raw traceback. No answer means NO — the safe reading for a prompt whose
    # "yes" rewrites history.
    try:
        answer = _input(f"Create branch '{new_branch}' with the reconstructed history? [y/N] ")
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer.strip().lower() not in ("y", "yes"):
        print("Aborted — no changes made.")
        return 0

    # 7) Replay onto the new branch, with a progress bar.
    original_branch = repo.current_branch()
    new_tip = _replay(repo, commits, ai_map, new_branch)
    if not new_tip:
        print("Reconstruction failed — no commits were written. Your repository is unchanged.")
        return 1
    repo._run(["git", "branch", new_branch, new_tip], check=False)
    switched = repo._run(["git", "switch", new_branch], check=False)
    on_branch = switched.returncode == 0

    _print_completion(new_branch, original_branch, on_branch, len(commits), to_annotate)
    return 0


def _commit_message(repo, sha: str) -> str:
    return repo._run(["git", "log", "-1", "--format=%B", sha], check=False).stdout


# ---------------------------------------------------------------------------
# Reconstruction helpers
# ---------------------------------------------------------------------------


def _already_committed_turns(root: Path) -> tuple[dict[str, int], set[str]]:
    """What git already accounts for: ``({session_id: last committed turn index}, {turn ids})``.

    aGiTrack commits record ``backend_session_id`` plus ``conversation_anchor`` — the last
    message id that commit covered — so within a session every turn up to the newest anchor
    is already committed, with its trace and its tokens. Both forms are returned because a
    fork records the same turn under a different session id: the anchor cutoff catches it in
    the session that was committed, the id set catches it in every other copy.
    """
    anchors = bt._committed_anchors(root)
    if not anchors:
        return {}, set()
    cutoffs: dict[str, int] = {}
    ids: set[str] = set()
    for source in bt._discover(root):
        session_anchors = anchors.get(source.ref_id)
        if not session_anchors:
            continue
        try:
            exported = source.export()
        except Exception:
            continue
        if exported is None:
            continue
        committed = [i for i, turn in enumerate(exported.turns) if (turn.assistant_message_id or "") in session_anchors]
        if not committed:
            continue
        cutoff = max(committed)
        cutoffs[source.ref_id] = max(cutoffs.get(source.ref_id, -1), cutoff)
        for turn in exported.turns[: cutoff + 1]:
            identity = bt._turn_key(source.backend, turn.assistant_message_id or "")
            if identity:
                ids.add(identity)
    return cutoffs, ids


def _is_tracked(tracked: tuple[dict[str, int], set[str]], session_id: str, turn_index: int, identity: str) -> bool:
    cutoffs, ids = tracked
    if identity and identity in ids:
        return True
    cutoff = cutoffs.get(session_id)
    return cutoff is not None and turn_index <= cutoff


def _gather_turns(root: Path) -> list[_TurnRec]:
    """Every agent turn recorded for the repository that is NOT already accounted for in git,
    with its edited files relativized to the repo root (so they compare directly to git's
    repo-relative paths). Each turn appears at most once.

    Two things would otherwise be counted twice in a repo that used aGiTrack for part of its
    life — which is exactly the repo this command is for:

    * **Forked conversations.** Resuming or rewinding replays the earlier turns into a NEW
      session id, so the same turn is read from several transcripts. Keyed by the assistant
      message id (the backend's own identity for the turn), the newest copy wins — the same
      rule the dashboard reconstruction uses. Without it one turn's trace would be appended
      several times over and its tokens summed once per copy.
    * **Turns aGiTrack already committed.** Those commits carry the turn's trace and its
      token counts already. Re-attributing them here — file overlap alone can easily land
      them on a LATER untracked commit — would count the same tokens a second time and print
      the same conversation twice in one history.
    """
    tracked = _already_committed_turns(root)
    seen: set[str] = set()
    out: list[_TurnRec] = []
    for source in bt._discover(root):  # newest session first
        try:
            exported = source.export()
        except Exception:
            exported = None
        if exported is None:
            continue
        bases = bt._relativize_bases(root, source.base_dir)
        for turn_index, turn in enumerate(exported.turns):
            identity = bt._turn_key(source.backend, turn.assistant_message_id or "")
            if identity:
                if identity in seen:
                    continue  # an older fork of a turn already taken from a newer session
                seen.add(identity)
            if _is_tracked(tracked, exported.session_id, turn_index, identity):
                continue  # aGiTrack already committed this turn, with its tokens and trace
            # Edits outside the repo (scratch files, plans) never map to a commit's paths.
            edits = [rel for rel in (bt._relativize(edit, bases) for edit in turn.edits) if rel is not None]
            files = {edit.path for edit in edits if edit.path}
            if not files:
                continue
            out.append(
                _TurnRec(
                    assistant_id=turn.assistant_message_id or "",
                    turn_index=turn_index,
                    ended_at=int(turn.ended_at or turn.started_at or exported.updated or 0),
                    started_at=turn.started_at,
                    files=files,
                    backend=source.backend,
                    model=turn.model,
                    tokens=turn.tokens,
                    user_prompt=turn.user_prompt,
                    final_response=turn.final_response,
                    queued_followups=list(turn.queued_followups),
                    session_id=exported.session_id,
                    reasoning_effort=turn.reasoning_effort,
                    mcp_servers=list(turn.mcp_servers),
                    mcp_tools=list(turn.mcp_tools),
                    skills=list(turn.skills),
                    subagents=list(turn.subagents),
                )
            )
    return out


def _commits_oldest_first(repo) -> list[str]:
    """Every commit reachable from HEAD, parents before children (topological, oldest first)."""
    out = repo._run(["git", "log", "--topo-order", "--reverse", "--format=%H", "HEAD", "--"], check=False).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def _match_turns_to_commits(
    repo, commits: list[str], changed: dict, turns: list[_TurnRec]
) -> dict[str, list[_TurnRec]]:
    """Attribute each turn to the commit where its change landed: the commit whose changed files
    overlap the turn's files (the primary signal — content is authoritative). Among such commits,
    prefer the EARLIEST one made at/after the turn (where the agent's work was first committed); if
    none is later — clock/timezone skew, or a commit dated before the turn — fall back to the LATEST
    overlapping commit. Time only disambiguates; file overlap decides. A commit no turn's files
    match is a user commit and is left out. Returns ``sha -> [turns]``."""
    times = {
        sha: int(repo._run(["git", "log", "-1", "--format=%at", sha], check=False).stdout.strip() or 0)
        for sha in commits
    }
    commit_files = {sha: {path for (path, _ins, _del) in changed.get(sha, [])} for sha in commits}
    ai_map: dict[str, list[_TurnRec]] = {}
    for turn in sorted(turns, key=lambda t: t.ended_at):
        candidates = [sha for sha in commits if turn.files & commit_files[sha]]
        if not candidates:
            continue
        after = [sha for sha in candidates if times[sha] >= turn.ended_at]
        best = min(after, key=lambda sha: times[sha]) if after else max(candidates, key=lambda sha: times[sha])
        ai_map.setdefault(best, []).append(turn)
    return ai_map


def _without_attribution_only_metadata(message: str) -> str:
    """*message* with any attribution-only aGiTrack block removed, leaving the user's own text.

    Only reached for a commit `carries_ai_history` said records no AI work, so anything dropped
    here is a bare ``commit_type: user`` block — session id, system and version, no turn behind it.
    The block is a run of contiguous ``key: value`` lines, so it ends at the first blank line.
    """
    out, rest = [], message
    while METADATA_HEADER in rest:
        head, _, tail = rest.partition(METADATA_HEADER)
        out.append(head)
        block, sep, remainder = tail.partition("\n\n")
        rest = remainder if sep else ""  # a trailing block runs to the end: drop it entirely
        del block
    out.append(rest)
    return "".join(out).rstrip()


def _annotation(turns: list[_TurnRec], repo_root: str | None = None) -> str:
    """The ``# Interaction Trace`` + ``# aGiTrack Metadata`` block to append to a commit, built from
    the turns that produced it — rendered in the exact format a real aGiTrack commit uses.

    ``repo_root`` is this repository, so other projects' names are kept out of the trace exactly
    as a live commit keeps them out (agitrack/commits/foreign.py). It matters MORE here, not
    less: these commits are reconstructed from months of transcripts at once, so they carry every
    project the user happened to mention across all of it, and the branch they land on is written
    to be pushed. (The backtrace DASHBOARD is a different surface — a local view of local
    transcripts, published nowhere — and deliberately does not redact.)"""
    ordered = sorted(turns, key=lambda t: t.ended_at)
    trace: list[dict] = []
    tokens = TokenUsage()
    backends: Counter[str] = Counter()
    models: Counter[str] = Counter()
    effort: str | None = None
    session_ids: set[str] = set()
    starts: list[int] = []
    ends: list[int] = []
    for turn in ordered:
        if turn.user_prompt.strip():
            trace.append({"role": "user", "content": turn.user_prompt})
        for followup in turn.queued_followups:
            if followup.strip():
                trace.append({"role": "user", "content": followup})
        if turn.final_response.strip():
            trace.append({"role": "agent", "content": turn.final_response})
        tokens.add(turn.tokens)
        backends[turn.backend] += 1
        if turn.model:
            models[turn.model] += 1
        effort = effort or turn.reasoning_effort
        session_ids.add(turn.session_id)
        if turn.started_at:
            starts.append(turn.started_at)
        if turn.ended_at:
            ends.append(turn.ended_at)
    backend = backends.most_common(1)[0][0] if backends else "unknown"
    model = models.most_common(1)[0][0] if models else None
    backend_session_id = session_ids.pop() if len(session_ids) == 1 else "multiple"
    lines = _trace_and_metadata_lines(
        trace=trace,
        backend=backend,
        backend_session_id=backend_session_id,
        # These sessions weren't run under aGiTrack; mark the reconstruction honestly rather than
        # inventing a session identity/name.
        agitrack_session_id="reconstructed",
        session_name="backtrace",
        model=model,
        reasoning_effort=effort,
        capabilities=capabilities.merge_turns(ordered),
        token_usage=tokens.to_dict(),
        trace_turn_limit=len(trace) + 1,
        covered_commits=None,
        started_at=min(starts) if starts else None,
        ended_at=max(ends) if ends else None,
        repo_root=repo_root,
    )
    return "\n".join(lines)


def _replay(repo, commits: list[str], ai_map: dict[str, list[_TurnRec]], new_branch: str) -> str:
    """Recreate every commit with ``git commit-tree`` — same tree, same author/committer identity
    and dates, parents remapped to the rewritten history — appending the aGiTrack annotation to AI
    commits. Returns the new tip SHA (or "" on failure). Shows a progress bar."""
    old_to_new: dict[str, str] = {}
    total = len(commits)
    print(f"Reconstructing {total} commit(s) onto '{new_branch}' …")
    for index, sha in enumerate(commits, start=1):
        tree = repo._run(["git", "rev-parse", f"{sha}^{{tree}}"], check=False).stdout.strip()
        if not tree:
            _progress(index, total)
            continue
        parent_line = repo._run(["git", "rev-list", "--parents", "-n", "1", sha], check=False).stdout.split()
        parents = parent_line[1:] if len(parent_line) > 1 else []
        new_parents = [old_to_new[p] for p in parents if p in old_to_new]

        message = repo._run(["git", "log", "-1", "--format=%B", sha], check=False).stdout.rstrip("\n")
        turns = ai_map.get(sha)
        # Don't double-annotate a commit that already RECORDS AI WORK (a repo that used aGiTrack
        # for part of its life). Same predicate as the count promised above, so what gets written
        # matches what the user agreed to. An attribution-only user block is not such a record, and
        # is dropped rather than left beside the annotation: two metadata blocks would read as a
        # squash aggregate whose user constituent carries nothing.
        if turns and not carries_ai_history(message):
            # Only the ANNOTATION is redacted, never the commit's own original message: this
            # command promises to preserve the history it rewrites and add metadata beside it,
            # and the subject is the author's, not aGiTrack's.
            annotation = _annotation(turns, str(repo.repo))
            message = _without_attribution_only_metadata(message).rstrip() + "\n\n" + annotation + "\n"

        args = ["git", "commit-tree", tree]
        for parent in new_parents:
            args += ["-p", parent]
        new_sha = repo._run(args, input_text=message, env=_identity_env(repo, sha), check=False).stdout.strip()
        if not new_sha:
            print("\n  ! failed to write a commit; aborting reconstruction.")
            return ""
        old_to_new[sha] = new_sha
        _progress(index, total)
    print()  # end the progress line
    return old_to_new.get(commits[-1], "")


def _identity_env(repo, sha: str) -> dict[str, str]:
    """The author/committer identity + dates of ``sha``, as git env vars, so the replayed commit
    preserves who made it and when."""
    raw = repo._run(
        ["git", "log", "-1", "--format=%an%x1f%ae%x1f%aI%x1f%cn%x1f%ce%x1f%cI", sha], check=False
    ).stdout.strip()
    parts = raw.split("\x1f")
    if len(parts) != 6:
        return {}
    an, ae, ad, cn, ce, cd = parts
    return {
        "GIT_AUTHOR_NAME": an,
        "GIT_AUTHOR_EMAIL": ae,
        "GIT_AUTHOR_DATE": ad,
        "GIT_COMMITTER_NAME": cn,
        "GIT_COMMITTER_EMAIL": ce,
        "GIT_COMMITTER_DATE": cd,
    }


def _branch_exists(repo, name: str) -> bool:
    result = repo._run(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"], check=False)
    return result.returncode == 0 and bool(result.stdout.strip())


def _progress(done: int, total: int) -> None:
    """A single-line textual progress bar (repositories with long histories can take a while)."""
    width = 32
    filled = int(width * done / total) if total else width
    bar = "█" * filled + "░" * (width - filled)
    pct = int(100 * done / total) if total else 100
    print(f"\r  [{bar}] {pct:3d}%  {done}/{total} commits", end="", flush=True)


def _print_completion(new_branch: str, original_branch: str, on_branch: bool, n_commits: int, n_ai: int) -> None:
    here = (
        f"You are now on '{new_branch}'." if on_branch else f"Created '{new_branch}' (couldn't switch automatically)."
    )
    old = original_branch or "your previous branch"
    print(
        f"\nDone. Reconstructed {n_commits} commit(s) on '{new_branch}' "
        f"({n_ai} annotated with aGiTrack metadata). {here}\n"
        f"\nReview the result before doing anything irreversible:\n"
        f"    git switch {new_branch}\n"
        f"    agitrack --dashboard          # see the reconstructed AI attribution\n"
        f"    git log --stat                # spot-check the commits and their messages\n"
        f"\nThis branch is a REWRITE of '{old}', so it does NOT merge cleanly (every commit has a new "
        f"hash). If, after reviewing, you want '{new_branch}' to REPLACE '{old}':\n"
        f"    git branch -f {old} {new_branch}       # move the old branch to the reconstructed tip\n"
        f"    git switch {old}\n"
        f"    # if it was already pushed, others must re-clone/reset — force-push only if you are sure:\n"
        f"    git push --force-with-lease origin {old}\n"
        f"Keep '{new_branch}' (or a backup of '{old}') until you have confirmed everything looks right."
    )
