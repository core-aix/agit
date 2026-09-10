"""Deliver aGiTrack's start-on-session-open to OpenCode, the way ``claude_settings`` does for
Claude and ``codex_settings`` for Codex.

OpenCode has no lifecycle HOOK for a session opening — its ``experimental.hook`` config covers
``file_edited`` and ``session_completed``, both of which are turn-end signals aGiTrack already
has better ones for. What it does have is a PLUGIN, and a plugin is loaded once when OpenCode
starts in a project, with the project's directory in hand: measured live, the plugin factory
runs with ``{client, project, worktree, directory, experimental_workspace, serverUrl, $}`` and a
cwd of the project root, before the first message is sent. That is the same moment a
``SessionStart`` hook fires on the other two backends, so it is where the tracker is started.

WHERE. ``<repo>/.opencode/plugin/agitrack-autostart.js``, and ``.opencode/`` is in
``git/repo.py``'s ``_NEVER_STAGE_PREFIXES``, so nothing written here can reach a commit. The file
is aGiTrack's alone — it carries a marker line, and removal refuses to delete a file without it.

WHAT IT DOES NOT CARRY, and why. The other two backends' session-start hooks also deliver the
commit-guidance note, because their hook protocol has a documented channel for it
(``additionalContext``). OpenCode's plugin API has no equivalent: the only place a plugin can
add text to a turn is ``chat.message``, which hands it the user's own message parts to mutate —
so the note would be recorded as part of what the USER said, and aGiTrack would then commit that
polluted prompt in its own interaction trace. Until there is a channel that does not rewrite the
user's words, OpenCode keeps the pre-existing behaviour: the agent is not told, and aGiTrack
folds any commit the agent makes itself.
"""

from __future__ import annotations

import json
from pathlib import Path

PLUGIN_RELPATH = Path(".opencode") / "plugin" / "agitrack-autostart.js"

# The line that identifies the file as ours. Removal deletes a file only when it is present, so
# a user file that happens to sit at this path is never destroyed by `agitrack -b stop`.
MARKER = "@agitrack-managed"


def _plugin_source(command: list[str]) -> str:
    """The plugin, with aGiTrack's own invocation baked in.

    The command is the same self-reference the git and Claude hooks use (interpreter path plus
    ``-m agitrack``), not the bare name: a plugin runs in whatever environment OpenCode was
    launched from, which is not necessarily one with aGiTrack's scripts directory on PATH. It is
    embedded as JSON so a Windows path's backslashes survive as data instead of becoming escape
    sequences in the JavaScript source.

    The child is detached with its streams ignored, and unref'd: a tracker must outlive the
    OpenCode process that happened to start it, and a plugin that waits on it would hold up the
    user's session. Every failure is swallowed for the same reason — a plugin that throws breaks
    OpenCode's startup, which is a far worse outcome than not starting a tracker.

    It spawns in the project directory, where an ``agitrack`` folder may well sit (a repository
    that holds your projects, aGiTrack's own checkout among them), so ``PYTHONSAFEPATH`` is
    passed the way every other aGiTrack child gets it — see ``proc.isolated_env``."""
    return f"""// {MARKER} — written and removed by aGiTrack; do not edit.
// Starts aGiTrack's background tracker when an OpenCode session opens in this project, which is
// what the SessionStart hooks do for Claude Code and Codex. See
// agitrack/backends/opencode_settings.py.
import {{ spawn }} from "node:child_process"

const COMMAND = {json.dumps(command)}

export const AgitrackAutostart = async ({{ directory, worktree }}) => {{
  try {{
    const child = spawn(COMMAND[0], COMMAND.slice(1), {{
      cwd: worktree || directory || process.cwd(),
      env: {{ ...process.env, PYTHONSAFEPATH: "1" }},
      detached: true,
      stdio: "ignore",
    }})
    child.unref()
  }} catch {{
    // never let aGiTrack's presence break an OpenCode session
  }}
  return {{}}
}}
"""


def install_agent_autostart_hook(repo: Path, *, debug=None) -> bool:
    """Write (or refresh) the plugin. Idempotent — the file is rewritten each time, because the
    aGiTrack invocation it embeds moves when aGiTrack is reinstalled or self-updates."""
    from agitrack.backends.claude_settings import AGENT_AUTOSTART_FLAG
    from agitrack.proc import agitrack_invocation

    path = Path(repo) / PLUGIN_RELPATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_plugin_source([*agitrack_invocation(), AGENT_AUTOSTART_FLAG]), encoding="utf-8")
    except OSError as error:
        if debug:
            debug(f"could not write opencode plugin at {path}: {error!r}")
        return False
    return True


def remove_agent_autostart_hook(repo: Path, *, debug=None) -> bool:
    """Delete the plugin, and the directories that existed only to hold it. Returns True when
    something was removed.

    Refuses a file without aGiTrack's marker: at this path that would be the user's own plugin,
    and stopping a tracker must never delete one."""
    path = Path(repo) / PLUGIN_RELPATH
    try:
        if not path.exists():
            return False
        if MARKER not in path.read_text(encoding="utf-8"):
            if debug:
                debug(f"opencode plugin at {path} is not aGiTrack's; leaving it alone")
            return False
        path.unlink()
        for parent in (path.parent, path.parent.parent):  # .opencode/plugin, then .opencode
            try:
                parent.rmdir()
            except OSError:
                break  # the user has other content there; leave it
    except (OSError, UnicodeDecodeError) as error:
        if debug:
            debug(f"could not remove opencode plugin at {path}: {error!r}")
        return False
    return True


def hook_is_installed(repo: Path) -> bool:
    path = Path(repo) / PLUGIN_RELPATH
    try:
        return path.exists() and MARKER in path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
