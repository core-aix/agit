"""The shared-session store: a history-free git ref that keeps only the latest
copy of each contributor's shared sessions, scoped to this repo.

Layout under ``refs/agit/shared-sessions``::

    <repo-fingerprint>/<github-id>/<session-name>/transcript.jsonl
    <repo-fingerprint>/<github-id>/<session-name>/manifest.json

``<repo-fingerprint>`` is the repo's root-commit SHA (clone-stable). Listing is
filtered to the current repo's fingerprint, so another repo's sessions can never
surface here even if the ref somehow accumulated them. Every write rebuilds the
whole tree into a single parent-less commit (see
:meth:`GitRepo.commit_tree_orphan`), so old copies become unreferenced and the
ref never grows a history.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from agit.git import GitRepo
from agit.sessions.identity import slug

REF = "refs/agit/shared-sessions"
DEFAULT_KEEP = 5  # most-recent shared sessions retained per contributor
# Throttle remote fetches when the dashboard polls. Your OWN shared sessions land
# in the local ref directly (no fetch needed); this only pulls collaborators'
# newly-shared sessions, so a long interval is fine and keeps load off the remote.
_FETCH_TTL = 300.0
_fetch_at: dict[str, float] = {}


@dataclass
class SharedEntry:
    github_id: str
    name: str
    manifest: dict

    @property
    def display(self) -> str:
        return f"{self.github_id}/{self.name}"


@dataclass
class PublishResult:
    remote: bool  # whether a git remote exists at all
    pushed: bool  # whether the push succeeded
    pruned: int = 0  # how many of the contributor's stale sessions were removed
    error: str = ""


@dataclass
class SharedSessionStore:
    repo: GitRepo  # the BASE repo (owns the remote and the shared object db)
    ref: str = REF
    _fingerprint: str | None = field(default=None, repr=False)

    def fingerprint(self) -> str:
        if self._fingerprint is None:
            self._fingerprint = self.repo.root_commit() or "no-root"
        return self._fingerprint

    def _prefix(self) -> str:
        return f"{self.fingerprint()}/"

    # --- reading -----------------------------------------------------------

    def entries(self) -> list[SharedEntry]:
        """Shared sessions for *this* repo, newest first (by manifest ``updated``)."""
        prefix = self._prefix()
        seen: dict[tuple[str, str], SharedEntry] = {}
        for path in self.repo.read_tree_paths(self.ref):
            if not path.startswith(prefix):
                continue
            rest = path[len(prefix) :].split("/")
            if len(rest) < 3:
                continue
            key = (rest[0], rest[1])
            if key in seen:
                continue
            seen[key] = SharedEntry(github_id=key[0], name=key[1], manifest=self._manifest(*key))
        return sorted(seen.values(), key=lambda e: e.manifest.get("updated", 0), reverse=True)

    def _manifest(self, github_id: str, name: str) -> dict:
        raw = self.repo.read_ref_blob(self.ref, f"{self._prefix()}{github_id}/{name}/manifest.json")
        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return {}

    def read_transcript(self, entry: SharedEntry) -> str | None:
        return self.repo.read_ref_blob(self.ref, f"{self._prefix()}{entry.github_id}/{entry.name}/transcript.jsonl")

    # --- writing -----------------------------------------------------------

    def _commit(self, entries: dict[str, str], message: str) -> None:
        tree = self.repo.write_tree_from(entries)
        sha = self.repo.commit_tree_orphan(tree, message)
        self.repo.update_ref(self.ref, sha)

    def _add_session(self, github_id: str, name: str, transcript: str, manifest: dict) -> None:
        base = f"{self._prefix()}{github_id}/{name}/"
        entries = {k: v for k, v in self.repo.read_tree_paths(self.ref).items() if not k.startswith(base)}
        entries[base + "transcript.jsonl"] = self.repo.write_blob(transcript)
        entries[base + "manifest.json"] = self.repo.write_blob(json.dumps(manifest, indent=2, sort_keys=True))
        self._commit(entries, f"agit: share session {github_id}/{name}")

    def prune_own_stale(self, github_id: str, *, keep: int = DEFAULT_KEEP) -> int:
        """Drop all but the most-recent ``keep`` sessions belonging to ``github_id``
        (a contributor only prunes their own). Returns the number removed."""
        gid = slug(github_id)
        mine = [e for e in self.entries() if e.github_id == gid]  # newest first
        stale = mine[keep:]
        if not stale:
            return 0
        prefix = self._prefix()
        drop_bases = [f"{prefix}{gid}/{e.name}/" for e in stale]
        kept = {
            path: blob
            for path, blob in self.repo.read_tree_paths(self.ref).items()
            if not any(path.startswith(base) for base in drop_bases)
        }
        self._commit(kept, f"agit: prune {len(stale)} stale session(s) for {gid}")
        return len(stale)

    # --- sync --------------------------------------------------------------

    def fetch(self) -> bool:
        """Pull the latest shared ref from the remote (best-effort)."""
        if not self.repo.remote_exists():
            return False
        return self.repo.fetch_ref(f"+{self.ref}:{self.ref}")

    def fetch_throttled(self) -> None:
        """Best-effort fetch at most once per TTL — for pollers (the dashboard)
        that want others' newly-shared sessions without hammering the remote."""
        key = str(self.repo.repo)
        now = time.monotonic()
        if now - _fetch_at.get(key, 0.0) < _FETCH_TTL:
            return
        _fetch_at[key] = now
        self.fetch()

    def unshare(self, github_id: str, name: str) -> PublishResult:
        """Remove one of the contributor's own shared sessions and push the
        removal. Sync-then-rewrite-then-push, like :meth:`publish`."""
        gid, nm = slug(github_id), slug(name)
        base = f"{self._prefix()}{gid}/{nm}/"
        remote = self.repo.remote_exists()
        if remote:
            self.fetch()
        old = self.repo.ref_sha(self.ref)
        kept = {k: v for k, v in self.repo.read_tree_paths(self.ref).items() if not k.startswith(base)}
        self._commit(kept, f"agit: unshare session {gid}/{nm}")
        if not remote:
            return PublishResult(remote=False, pushed=False)
        lease = f"{self.ref}:{old}" if old else None
        ok, err = self.repo.push_ref(f"{self.ref}:{self.ref}", force_with_lease=lease)
        return PublishResult(remote=True, pushed=ok, error="" if ok else err.strip())

    def publish(
        self, *, github_id: str, name: str, transcript: str, manifest: dict, keep: int = DEFAULT_KEEP
    ) -> PublishResult:
        """Share one session: sync from the remote, add it, prune the
        contributor's stale ones, and push. The local copy is always saved (so it
        can be pushed later); the push is best-effort and reports its outcome.

        Uses ``--force-with-lease`` against the just-fetched remote tip, so a
        concurrent contributor's push is never clobbered — it fails cleanly and
        the user can retry (the retry re-fetches their work first)."""
        gid, nm = slug(github_id), slug(name)
        remote = self.repo.remote_exists()
        if remote:
            self.fetch()
        old = self.repo.ref_sha(self.ref)  # remote tip we're racing against
        self._add_session(gid, nm, transcript, manifest)
        pruned = self.prune_own_stale(gid, keep=keep)
        if not remote:
            return PublishResult(remote=False, pushed=False, pruned=pruned)
        lease = f"{self.ref}:{old}" if old else None  # None ⇒ creating the ref
        ok, err = self.repo.push_ref(f"{self.ref}:{self.ref}", force_with_lease=lease)
        return PublishResult(remote=True, pushed=ok, pruned=pruned, error="" if ok else err.strip())
