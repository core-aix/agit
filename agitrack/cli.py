from __future__ import annotations

import argparse
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from agitrack.backends.setup import backend_installed, select_default_backend, select_default_summarizer_model
from agitrack.backends.proxy_agents import available_backends, backend_phrase
from agitrack.git import GitError, GitRepo, RepoLock, already_running_message
from agitrack.proc import UTF8_TEXT, console_isolation_kwargs
from agitrack.console import stdin_is_interactive, stdout_is_interactive, timestamp_output
from agitrack.config import GlobalConfig, settings
from agitrack.shell import AgitrackShell

try:
    # The proxy drives the agent through a (Con)PTY. Imported at module level so tests and
    # the launch path reference ``cli.ProxyRunner`` directly, but tolerant of a platform
    # where the proxy's platform layer can't load yet — the headless paths (json mode,
    # dashboard, --version) don't need it, and proxy mode reports it cleanly below.
    from agitrack.proxy import BackgroundRunner, ProxyRunner
except ImportError:  # pragma: no cover - only when the proxy platform layer is unavailable
    ProxyRunner = None  # type: ignore[assignment,misc]
    BackgroundRunner = None  # type: ignore[assignment,misc]


def _backend_command(name: str) -> str | None:
    """The executable that launches *name*'s CLI, or None when *name* is not a backend.

    Derived from the backend's own spawn command rather than a hand-written map: a literal
    table is one more list to forget when a backend is added, and `agitrack -- --help`
    would then reject a backend the rest of aGiTrack happily runs."""
    from agitrack.backends.setup import _executable

    if name not in available_backends():
        return None
    return _executable(name)


def _git_install_hint() -> str:
    """Shown when ``git`` isn't on PATH. aGiTrack manages your commits with git, so it can't
    run without it — a common state right after the VS Code extension installs the CLI but
    git itself isn't installed. Covers macOS, Linux, and Windows so any user sees a command
    that works; each part is its own block (blank line between) for legibility."""
    return "\n\n".join(
        [
            "git is not installed (or not on your PATH). aGiTrack manages your commits with "
            "git, so it can't run without it. Install it:",
            "  macOS:    brew install git    (or: xcode-select --install)",
            "  Linux:    use your package manager, e.g. sudo apt install git / sudo dnf install git",
            "  Windows:  winget install Git.Git    (or https://git-scm.com/download/win)",
            "Then open a NEW terminal so the updated PATH is picked up.",
        ]
    )


def _gh_install_hint() -> str:
    """Shown when the GitHub CLI (``gh``) isn't installed. gh is OPTIONAL — it gives the
    dashboard committer identities by GitHub username and powers session sharing — so this is
    informational. Covers macOS, Linux, and Windows, each part its own block for legibility."""
    return "\n\n".join(
        [
            "GitHub CLI (gh) isn't installed. aGiTrack uses it for the dashboard's committer "
            "identities and for session sharing; without it those features are limited (the "
            "dashboard groups authors by email instead). It's optional — you can continue "
            "without it. To install it:",
            "  macOS:    brew install gh",
            "  Linux:    sudo apt install gh    (or your package manager)",
            "  Windows:  winget install GitHub.cli",
            "Then run `gh auth login` and restart aGiTrack.",
        ]
    )


def _installed_via_msi() -> bool:
    """True for a frozen (PyInstaller) build — i.e. the Windows MSI bundle. There,
    prerequisite setup (backends, git, gh, git identity, gh login) is the MSI installer's
    job, so aGiTrack does NOT prompt for it at runtime. A pip/source install is not frozen
    and does its setup at first run, on every platform (including Windows)."""
    return bool(getattr(sys, "frozen", False))


def _maybe_install_tool(name: str, *, required: bool) -> bool:
    """Offer to auto-install a missing prerequisite (``git`` or ``gh``); return True once it
    is available. Only prompts on an interactive TTY where a supported package manager
    exists — otherwise returns False so the caller falls back to printing the manual hint.

    The MSI bundle is intentionally excluded: there, prerequisites are set up by the MSI
    installer, not by aGiTrack at runtime. A pip/source install still offers it (any OS)."""
    if _installed_via_msi():
        return False
    if not (stdin_is_interactive() and stdout_is_interactive()):
        return False
    from agitrack.system_tools import can_install_tool, install_system_tool

    if not can_install_tool(name):
        return False
    label = "git" if name == "git" else "the GitHub CLI (gh)"
    note = "" if required else " (optional)"
    # Two leading newlines, not one: whatever ran before this may have been a subprocess
    # (an installer, `gh auth status`) whose output does not end in a newline, so the first
    # closes that partial line and the second leaves a blank line before the question.
    #
    # An OPTIONAL tool defaults to NO. `gh` is explicitly optional — aGiTrack degrades to git
    # author names without it — yet a bare Enter shelled straight into `brew install gh`, which
    # is a package install nobody asked for on the most reflexive keypress there is. `git` is
    # genuinely required for aGiTrack to work at all, so that one keeps its Y default.
    default_yes = required
    suffix = "[Y/n]" if default_yes else "[y/N]"
    try:
        answer = _ask(f"\n\n{label} isn't installed. Install it now{note}? {suffix}: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    if default_yes:
        if answer in {"n", "no"}:
            return False
    elif not answer.startswith("y"):
        return False
    return install_system_tool(name)


def _git_config_global(config_args: list[str]) -> str:
    """Run ``git config --global`` and return its stdout (empty on any failure)."""
    try:
        result = subprocess.run(
            ["git", "config", "--global", *config_args],
            **UTF8_TEXT,
            capture_output=True,
            check=False,
            **console_isolation_kwargs(),  # keep git off a console on Windows (proc.py)
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip()


def _ensure_git_identity() -> None:
    """git refuses to commit without ``user.name`` and ``user.email`` ("Author identity
    unknown"), and aGiTrack commits every turn — so on a fresh machine, prompt for whichever
    is missing and set it globally. Interactive callers only; non-TTY callers should not
    reach here (they get no prompt and a polluted machine-readable stream is avoided)."""
    name = _git_config_global(["--get", "user.name"])
    email = _git_config_global(["--get", "user.email"])
    if name and email:
        return
    print("\n\ngit needs a name and email to record commits (aGiTrack commits your work each turn).")
    if not name:
        entered = _ask("  Name for git commits: ").strip()
        if entered:
            _git_config_global(["user.name", entered])
    if not email:
        entered = _ask("  Email for git commits: ").strip()
        if entered:
            _git_config_global(["user.email", entered])
    if not (_git_config_global(["--get", "user.name"]) and _git_config_global(["--get", "user.email"])):
        print("git identity is still incomplete; aGiTrack's commits may fail until name and email are set.")


def _make_console_output_lossy() -> None:
    """Never let an unencodable character turn console output into a crash.

    ``agitrack --help`` died with a UnicodeEncodeError on a cp1252 console because one help
    string contained ``↔`` (#233). The character is gone and a test keeps the help text
    encodable, but the class of bug is wider than the help text: a repo path, a branch name,
    a backend's error or a commit subject can all carry something the console's legacy code
    page cannot represent, and none of those are ours to sanitize. Degrading to ``?`` for one
    glyph is always better than losing the whole message and the exit code with it.

    Best-effort by design: under pytest's capture (and anywhere else stdout is not a real
    ``TextIOWrapper``) there is nothing to reconfigure, and that is fine."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None or getattr(stream, "errors", None) in ("replace", "backslashreplace"):
            continue
        try:
            reconfigure(errors="replace")
        except (ValueError, OSError):  # a stream that refuses; keep the original behaviour
            pass


def main(argv: list[str] | None = None) -> int:
    """Entry point. Turns an unhandled ``GitError`` into a message and exit 1.

    Every command guards the ``GitRepo.discover()`` that OPENS the repository, but nothing
    guarded the git commands that follow, and git can fail long after discovery succeeds. The
    reproducible case is Windows: on a box with neither ``core.longpaths`` nor the OS long-path
    opt-in, a repository whose path passes MAX_PATH opens fine and then fails on the first read
    of ``.git/packed-refs`` — ``agitrack --repo <deep path> -d text`` printed a raw traceback
    (``fatal: couldn't read .git/packed-refs: Filename too long``) rather than saying so. A
    traceback is never the right answer to "git could not do that", whatever the cause."""
    try:
        return _dispatch(argv)
    except GitError as error:
        print(error)  # _dispatch made the console lossy on its first line, so this cannot crash
        if _looks_like_a_long_path_failure(str(error)):
            print(
                "\nThis is Windows' MAX_PATH limit: git cannot reach a file whose path is longer\n"
                "than 260 characters. aGiTrack cannot fix it for you — the setting is git's and\n"
                "the OS's — but either of these does:\n"
                "  git config --global core.longpaths true\n"
                "  ...or move the repository somewhere with a shorter path."
            )
        return 1


def _looks_like_a_long_path_failure(message: str) -> bool:
    """Whether a git failure is really Windows' MAX_PATH limit. git says "Filename too long";
    the OS error underneath is ``ENAMETOOLONG``. Both spellings are matched because the message
    reaches us from git's stderr in one case and from Python in the other.

    Windows only: ``core.longpaths`` is a Windows-only git setting, and ENAMETOOLONG elsewhere
    is the filesystem's own (much larger) limit, which that advice would not fix."""
    if sys.platform != "win32":
        return False
    lowered = message.lower()
    return "filename too long" in lowered or "enametoolong" in lowered


# Bare-word commands aGiTrack answers, as opposed to flags. Kept deliberately tiny: everything
# else is a flag, and every word promoted to a command is one that can no longer be the first
# word of a bare prompt (`agitrack "stop the retry loop"` still works — see _split_command).
# The description is what `agitrack <typo>` lists, so it is written for someone who does not
# yet know what the command does.
_COMMANDS: dict[str, str] = {
    "status": "report whether aGiTrack is running for this repository, and in which mode",
    "stop": "stop whatever aGiTrack is running for this repository, in any mode",
}


def _split_command(argv: list[str]) -> tuple[str | None, list[str]]:
    """Peel a leading bare command verb off ``argv``, returning ``(command, rest)``.

    Only the FIRST token counts. `agitrack stop` has to be the way to stop this repo's tracking
    whatever mode it runs in, but aGiTrack also forwards unrecognized arguments to the backend as
    a prompt, so a word promoted to a command is a word stolen from every prompt that starts with
    it. Restricting it to position 0 keeps the theft to exactly the phrase the user typed as a
    command, and `agitrack -- stop …` (or any prompt with a word in front of it) is untouched."""
    if argv and argv[0] in _COMMANDS:
        return argv[0], argv[1:]
    return None, argv


# A command is one word: a letter first, then letters/digits and the punctuation a
# subcommand may reasonably carry. Deliberately excludes whitespace (a prompt), a
# leading digit, and anything path-like.
_COMMAND_WORD = re.compile(r"[a-zA-Z][a-zA-Z0-9._-]*")


def _looks_like_a_command(token: str) -> bool:
    """Whether ``token`` is a word typed in a command's position rather than a prompt.

    aGiTrack forwards what it does not recognize to the backend, which used to mean a MISTYPED
    command (`agitrack statsu`) silently launched a whole interactive agent session with the typo
    as its prompt — the one outcome nobody wants from a typo. A single bare word is a command
    attempt; a sentence is a prompt. So: one word, no whitespace, starting with a letter, and not
    a flag. Anything else — `agitrack "fix the bug"`, a path, `--anything` — is left alone, and
    `agitrack -- statsu` still forwards the word verbatim."""
    if not token or token.startswith("-"):
        return False
    return bool(_COMMAND_WORD.fullmatch(token))


def _unknown_command(token: str) -> int:
    """Report a bare word that is not one of aGiTrack's commands, list the ones that are, and
    exit 2 (the conventional code for "you typed it wrong") WITHOUT starting anything."""
    print(f"Unknown command: {token}\n")
    print("Known commands:")
    width = max(len(name) for name in _COMMANDS)
    for name, description in sorted(_COMMANDS.items()):
        print(f"  {name:<{width}}  {description}")
    print(
        "\nRun `agitrack --help` for the full list of options, `agitrack` on a terminal to pick a\n"
        f"mode from a menu, or `agitrack -- {token}` to send the word to the backend as a prompt."
    )
    return 2


# Every parsed option that, on its own, says what aGiTrack should DO. If none of them is set and
# no backend arguments were forwarded, the user asked for nothing in particular — which is when
# the mode menu appears. Kept as a list of attribute names rather than a hand-written condition so
# a new mode is one entry, and forgetting it shows up as "the menu appears when it shouldn't"
# rather than as a mode that silently launches the TUI.
_MODE_OPTIONS = (
    "interactive",
    "background",
    "dashboard",
    "backtrace",
    "status",
    "daemons",
    "json_mode",
    "prompts",
    "ui_bridge",
    "share_sessions",
    "remove_hooks",
    "recover",
    # Internal entry points (hooks and detached children). They never reach a terminal, so the
    # menu would not fire for them anyway, but naming them keeps the intent explicit.
    "dashboard_serve",
    "backtrace_serve",
    "background_serve",
    "autostart_on_change",
    "claude_session_note",
    "precommit_sync",
)


def _no_mode_requested(args: argparse.Namespace, backend_args: list[str]) -> bool:
    """True when nothing on the command line picked a mode.

    Backend arguments count as a request: `agitrack -- "fix the bug"` is someone naming a task
    for the agent, and answering that with a menu would be obtuse. `--mode json`, the deprecated
    alias, counts too, and so does `--yes` — it means "never ask me a question", which a menu is."""
    if backend_args or getattr(args, "mode", "proxy") == "json" or getattr(args, "yes", False):
        return False
    return not any(getattr(args, name, None) for name in _MODE_OPTIONS)


def _choose_mode() -> list[str] | None:
    """Show the mode menu and return the argv the choice stands for, or None to quit.

    The menu itself lives in :mod:`agitrack.modes`, where the modes are enumerated as data —
    the same table the docs and the website quote, so a mode cannot be added in one place and
    forgotten in the other."""
    from agitrack.modes import choose

    # Whatever the user pressed while an earlier startup step ran would otherwise be delivered
    # to the menu the instant it appears, choosing a mode nobody looked at.
    _drain_terminal_input()
    mode = choose()
    return list(mode.argv) if mode is not None else None


def _dispatch(argv: list[str] | None = None) -> int:
    _make_console_output_lossy()
    command, argv = _split_command(list(sys.argv[1:] if argv is None else argv))
    # A bare word in the command position that is NOT a command: say so and stop. Without this it
    # fell through to the backend as a prompt, so `agitrack statsu` launched an entire interactive
    # agent session — the typo became the task. Answered before argparse, before the config and
    # before any repo work, because nothing about the rest of the command line can rescue it.
    if command is None and argv and _looks_like_a_command(argv[0]):
        return _unknown_command(argv[0])
    parser = argparse.ArgumentParser(
        # `prog` explicitly, or Python 3.14 on Windows derives it from sys.argv[0] and the usage
        # line becomes `usage: python.exe C:\Users\<name>\AppData\Local\Python\...\Scripts\agitrack`
        # — the user's home layout leaked into any pasted help output, and the only line over 80
        # columns.
        prog="agitrack",
        description="Interactive agent + git commit orchestration.",
        add_help=False,
        # NO PREFIX MATCHING. It let `--overwrite` resolve to `--overwrite-shared`, so a message
        # naming a flag that does not exist appeared to work — and would have broken silently the
        # moment a second `--overwrite*` option was added. An unambiguous typo is still a typo.
        allow_abbrev=False,
    )
    parser.add_argument(
        "-h",
        "--help",
        action="store_true",
        help="show this help message and exit",
    )
    parser.add_argument(
        "-i",
        "--interactive",
        action="store_true",
        help="interactive mode: run the coding agent inside aGiTrack's TUI, which commits each "
        "agent turn for you. This is the mode `agitrack` used to start with no arguments; a bare "
        "`agitrack` on a terminal now asks which mode you want instead, and `-i` is how you say "
        "'this one' up front.",
    )
    parser.add_argument(
        "-b",
        "--background",
        dest="background",
        nargs="?",
        const="run",
        choices=["run", "stop", "status"],
        default=None,
        help="background (headless) mode: run WITHOUT the interactive TUI, so you drive the "
        "coding agent from any UI you like (its native CLI, an IDE extension, …) while aGiTrack "
        "watches the local session transcript and performs all the tracking the TUI would — "
        "recording each turn, summarizing, and installing the commit hooks that fold the "
        "interaction trace and token metadata into your commits. ALWAYS runs without a worktree "
        "(implies --no-worktree). Uses AUTO commits by default (like interactive mode); add "
        "--manual-commits / -m for user-triggered commits. Bare `-b` (no argument) means `-b run`; "
        "`-b stop` / `-b status` stop or report the background tracker running on this repo. "
        "Also settable via 'background' in config.",
    )
    parser.add_argument(
        "--no-worktree",
        action="store_true",
        help="run the agent against the current branch instead of an isolated worktree "
        "(edits are visible live; no isolation/integration; unsafe with concurrent sessions). "
        "Background (-b) and manual-commit (-m) modes always imply this.",
    )
    parser.add_argument(
        "-m",
        "--manual-commits",
        dest="manual_commits",
        action="store_true",
        help="user-triggered commits. ALWAYS runs without a worktree (implies --no-worktree): the "
        "agent edits the current branch directly and each turn is recorded as a hidden 'latent' "
        "commit on a side ref instead of landing on the branch. When you commit (via the aGiTrack "
        "menu or an external `git commit`), the pending agent turns are folded into that one "
        "commit. Also settable via 'manual_commits' in config.",
    )
    parser.add_argument(
        "-d",
        "--dashboard",
        nargs="?",
        const="html",
        choices=["text", "html", "stop", "status", "export"],
        default=None,
        help="show repository metrics computed from aGiTrack commit metadata "
        "(coverage, AI / human / non-tracked line changes, tokens, per-backend/"
        "model/committer breakdowns, loop detection). Bare `-d` (no argument) means `-d html`: "
        "it starts a filterable, auto-refreshing dashboard as a background daemon on localhost, "
        "opens it in the browser, and returns to the shell; the daemon keeps running "
        "(surviving this terminal) until `-d stop`. `status` reports it; `text` prints a "
        "one-shot report and exits; `export` writes a server-free static demo copy of the "
        "dashboard (see --export-dir) that any static web host can serve",
    )
    parser.add_argument(
        "--export-dir",
        default=None,
        help="where `-d export` writes the static demo site (default: .agitrack/demo-site "
        "inside the repo). The directory is REPLACED: it must be empty, absent, or a previous "
        "export, otherwise the command refuses rather than deleting files it did not write "
        "(pass --force to delete it anyway).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="with `-d export`, allow --export-dir to replace a non-empty directory that "
        "aGiTrack did not write. Everything currently in it is deleted permanently.",
    )
    parser.add_argument(
        "--backtrace",
        nargs="?",
        const="html",
        choices=["text", "html", "stop", "status", "commit"],
        default=None,
        help="reconstruct how PAST coding-agent conversations changed THIS directory, from local "
        f"{backend_phrase()} transcripts alone — even if you have never used aGiTrack here, and even if "
        "the directory is not a git repo. It reads the sessions that ran in this directory (or a "
        "subdirectory), recovers each turn's file edits, and shows the same dashboard (tokens, "
        "models, lines changed, and the full user-agent trace behind each change) marked clearly as "
        "a historical backtrace, not live repo status. Bare `--backtrace` (or `--backtrace html`) "
        "starts it as a background daemon on localhost, opens the browser, and returns to the shell "
        "(it keeps running, surviving this terminal, until `--backtrace stop`); `status` reports it; "
        "`text` prints a one-shot report. `commit` REWRITES history onto a NEW branch (`--backtrace-branch`), "
        "annotating the commits that made AI changes with aGiTrack metadata — so a project built "
        "without aGiTrack still gets a tracked history (requires a clean working tree).",
    )
    parser.add_argument(
        "--backtrace-branch",
        # `--backtrace-branch` is the canonical spelling (help text and every message use it).
        # `--branch` is kept as an undocumented alias ONLY because earlier guidance printed it, and
        # an unknown flag does not error here — `parse_known_args` funnels it to the backend — so
        # without the alias that stale spelling silently produced "give me a branch name" forever.
        "--branch",
        dest="backtrace_branch",
        default=None,
        help="the NEW branch to create for `--backtrace commit` (the reconstructed, history-"
        "rewritten commits are placed here; your current branch is left untouched).",
    )
    parser.add_argument(
        "-s",
        "--status",
        action="store_true",
        help="report whether aGiTrack is running for this repo and in which mode (interactive vs "
        "background, auto vs manual commit, worktree vs no-worktree), then exit. Also spelled "
        "`agitrack status`.",
    )
    parser.add_argument(
        "--daemons",
        nargs="?",
        const="list",
        choices=("list", "stop"),
        default=None,
        help="list every running aGiTrack daemon across ALL repositories — its function (repo "
        "dashboard, backtrace dashboard, or background mode), repo name, and PID — then exit. "
        "`--daemons stop` stops them (the session you run it from is never stopped). Add `--repo "
        "<path>` to list or stop only that repository's daemons; without it the reach is every "
        "repository you have. A non-interactive `--daemons stop` needs `--yes`, since there is "
        "no one there to answer the confirmation.",
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="never ask a question: take the default for every startup prompt (backend choice, "
        "summarizer model, privacy acknowledgement, auto-start hooks) and answer yes to "
        "confirmations. Makes `agitrack -b` usable from a script or CI even when it is attached "
        "to a terminal. Required for `--daemons stop` when there is no terminal to prompt on.",
    )
    parser.add_argument(
        "--share-sessions",
        action="store_true",
        help="share EVERY local agent session for this repository to 'origin' in one go, then "
        "exit — the same thing the sessions menu does per session, for all of them. Use it to "
        "put a machine's work where collaborators (and your other machines) can see it, and so "
        "`--backtrace` can reconstruct the project's history across machines rather than only "
        "from the transcripts on this one. Idempotent: a session already shared unchanged is "
        "skipped, and one whose shared copy is NEWER is refused rather than rewound (re-run "
        "with --overwrite-shared to replace it).",
    )
    parser.add_argument(
        "--overwrite-shared",
        action="store_true",
        help="with --share-sessions: replace shared copies that are newer than this machine's, "
        "instead of refusing them. Rewinds them for every collaborator, so it is opt-in.",
    )
    # --- options without a short form, in rough order of how often they matter ---
    # Default None rather than "." so callers can tell "the user named a repo" from "we fell back
    # to the cwd" — `--daemons` scopes on that difference, and error messages only mention --repo
    # when the user actually typed it. Normalised to "." right after parsing, so every other
    # reader of args.repo is unaffected.
    parser.add_argument("--repo", default=None, help="target Git repository path")
    parser.add_argument(
        "--backend",
        choices=available_backends(),
        default=None,
        help="agent backend to use; also saved as the global default",
    )
    parser.add_argument(
        "--new-session",
        action="store_true",
        help="start a fresh backend conversation instead of resuming the last one",
    )
    parser.add_argument(
        "--auto-commit",
        dest="auto_commit",
        action="store_true",
        help="force automatic (aGiTrack-triggered) commits in EVERY mode — the default already, "
        "so this only matters to override a configured 'manual_commits': true, or an earlier -m "
        "on the same command line. aGiTrack commits each agent turn itself and folds tracking "
        "into the agent's own commits via a prepare-commit-msg hook.",
    )
    parser.add_argument(
        "--delay-merge",
        action="store_true",
        help="don't merge a turn's committed changes into the base branch automatically; "
        "instead leave them in the session's working directory for you to review/edit, then "
        "merge on your confirmation via the session menu. Off by default.",
    )
    parser.add_argument(
        "--no-commit-guidance",
        action="store_true",
        help="do not tell the coding agent that aGiTrack handles commits; by default aGiTrack "
        "appends a note to the agent's system prompt (where the backend supports it) so the "
        "agent does not create its own git commits unless you explicitly ask",
    )
    parser.add_argument(
        "--full-agent-messages",
        action="store_true",
        help="record every user-facing message the agent sends during a turn in the "
        "commit's interaction trace, not just the final reply (tool calls and file edits "
        "are still excluded); also settable per-repo via full_agent_messages in config",
    )
    parser.add_argument(
        "--log-file",
        dest="log_file",
        default=None,
        metavar="PATH",
        help="append notable aGiTrack events (a daemon starting or stopping, a tracker following "
        "you to another coding agent, an AI change detected, a commit made, an update available) to PATH — a plain-text log you can "
        "`tail -f`. Works in every mode: the TUI, -b, --prompt and --json. A relative path is "
        "resolved against the repo root, and a log inside the repo is git-ignored for you. Also "
        "settable via 'log_file' in config.",
    )
    parser.add_argument(
        "--no-confine",
        "--no-sandbox",
        dest="no_sandbox",
        action="store_true",
        help="do not confine the agent's writes inside this REPOSITORY. By default aGiTrack "
        "stops the agent writing anywhere in the repo except its own session worktree (plus "
        ".git): on macOS/Linux via the OS sandbox (sandbox-exec/bubblewrap), and where no "
        "sandbox is available (e.g. Windows) via a git pre-commit guard that stops the agent "
        "committing into the base repo. It protects the REPOSITORY, not the rest of the disk — "
        "writes outside the repo are allowed either way, by construction. Also settable via "
        "'sandbox' in config. (--no-sandbox is kept as an alias.)",
    )
    parser.add_argument(
        "--allowed-edit-paths",
        default=None,
        metavar="PATH[:PATH...]",
        help="extra paths the sandbox lets the agent write to, beyond its worktree — "
        "multiple paths separated by '%s' (like PATH). Also settable via "
        "allowed_edit_paths in config." % os.pathsep,
    )
    parser.add_argument(
        "--backend-command",
        default=None,
        metavar="COMMAND",
        help="custom command used to launch the backend agent, replacing the backend "
        "executable so a wrapper can sit beneath aGiTrack — e.g. "
        "--backend-command 'somewrapper claude'. Split like a shell command; it must "
        "ultimately exec the chosen backend (aGiTrack's own sandbox wrapper still goes "
        "on top). Also settable via backend_command in config (a string, or an object "
        "keyed by backend name).",
    )
    parser.add_argument(
        "--remove-hooks",
        action="store_true",
        help="remove all aGiTrack-installed git hooks from the repo — the persistent auto-track "
        "pre-commit hook and the manual-commit prepare-commit-msg/post-commit fold hooks (and the "
        "worktree base-commit guard), restoring any hooks they chained. Use this to fully opt out "
        "of aGiTrack's commit-time tracking.",
    )
    parser.add_argument(
        "--recover",
        action="store_true",
        help="finalize work left by a session that exited abruptly (e.g. the VSCode window "
        "was closed mid-turn): for each session WORKTREE, commit a finished turn's "
        "uncommitted changes and merge it into the base branch (skipping the merge on a "
        "conflict). Worktree sessions only — a --no-worktree run leaves the agent's edits in "
        "your own working tree, intermixed with your changes, so committing them for you would "
        "be unsafe and `--recover` reports them instead. Runs headlessly and no-ops if a live "
        "aGiTrack holds the repo lock. Used by the VSCode extension on close; also runnable "
        "manually.",
    )
    parser.add_argument("--verbose", action="store_true", help="show aGiTrack diagnostic messages")
    parser.add_argument(
        "--version",
        action="store_true",
        help="print the aGiTrack version and exit",
    )
    # --- testing / programmatic-driver options (real interactive use never needs these) ---
    parser.add_argument(
        "--json",
        dest="json_mode",
        action="store_true",
        help="use the JSON prompt-loop instead of the interactive TUI: aGiTrack sends each typed "
        "line (or --prompt) to the backend non-interactively and captures the reply as a commit. "
        "Mainly for testing and programmatic drivers — normal interactive use does not need it.",
    )
    parser.add_argument(
        "--prompt",
        action="append",
        dest="prompts",
        metavar="TEXT",
        help="run this prompt non-interactively (implies --json) and exit; "
        "repeatable, prompts run in order. Lines starting with ':' are aGiTrack "
        "commands, e.g. --prompt ':status'",
    )
    parser.add_argument(
        "--json-events",
        action="store_true",
        help="with --json, emit one machine-readable JSON line per turn event "
        "(the agent's response, the commit produced, errors) — used by the VSCode "
        "chat extension and other programmatic drivers",
    )
    parser.add_argument(
        "--ui-bridge",
        action="store_true",
        help="with --json, run a long-lived session over stdin/stdout where interactive "
        "questions (menus, confirmations, text input) are asked of the driver program instead "
        "of a terminal — for embedding aGiTrack behind an editor/GUI front-end. The protocol is "
        'NEWLINE-DELIMITED JSON ({"type": ...} objects), not JSON-RPC 2.0; see the module '
        "docstring in agitrack/shell/bridge.py for the message types. Pair it with "
        "--skip-privacy-ack for a stream that carries nothing but events.",
    )
    parser.add_argument(
        "--mode",
        choices=["proxy", "json"],
        default="proxy",
        # Deprecated: `--mode` conflated too many things (interactive/background,
        # auto/manual, worktree/no-worktree are separate flags now). Kept as a hidden,
        # still-working alias for `--json` so existing scripts don't break: `--mode json`
        # == `--json`. New usage should prefer `--json`.
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--skip-privacy-ack",
        action="store_true",
        # Was SUPPRESS'd as an internal flag (aGiTrack sets it when re-exec'ing itself after an
        # in-app update, where the user acknowledged this session already). But it is also the
        # only way to get a clean machine-readable stream out of --json/--ui-bridge, and a
        # driver author cannot find a flag that is not in --help.
        help="do not print the startup privacy warning. Intended for programmatic drivers "
        "(--json / --ui-bridge), where the banner is prose on a stream meant to carry only "
        "events. aGiTrack also sets it internally when it re-execs itself after an update.",
    )
    parser.add_argument(
        "--autostart-on-change",
        action="store_true",
        # Internal: entry point of the Claude Code `Stop` hook that background mode installs.
        # Starts the tracker when a finished turn left changes in the tree and nothing is
        # tracking yet, so tracking resumes on new CODE rather than only on the next commit.
        # Called by Claude Code, not by hand.
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--autostart-on-agent",
        action="store_true",
        # Internal: entry point of the SESSION-START hooks background mode installs for every
        # backend (Claude Code and Codex hooks, an OpenCode plugin). Starts the tracker as soon
        # as an agent opens a session in the repo — the earliest signal that work is about to
        # happen here — and carries the commit-guidance note for the session that started it.
        # Called by the backend, not by hand.
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--claude-session-note",
        action="store_true",
        # Internal: the body of the Claude Code SessionStart hook that background mode
        # installs (backends/claude_settings.py). Prints the commit-guidance note in the
        # hook's JSON envelope, so the agent knows aGiTrack commits for it even though
        # aGiTrack never spawned it and could not pass --append-system-prompt. Called by
        # Claude Code, not by hand.
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--hub-serve",
        action="store_true",
        # Internal: the detached dashboard hub. ONE per user: it binds a single port and serves
        # every repository aGiTrack knows about, switched by URL path (agitrack/metrics/hub.py).
        # `agitrack -d` and `agitrack --backtrace` spawn it and then just open the right path.
        # Not meant for manual use.
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--dashboard-serve",
        action="store_true",
        # Internal: run the metrics dashboard HTTP server in the foreground (this
        # process). `agitrack -d` and the TUI's Ctrl-G dashboard spawn aGiTrack with this
        # flag to host the dashboard in a separate, lifecycle-bound child process (#110).
        # Not meant for manual use.
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--backtrace-serve",
        action="store_true",
        # Internal: the detached `--backtrace` child. Bare `agitrack --backtrace` spawns aGiTrack
        # with this flag to host the reconstructed dashboard out-of-process, bound to the owner
        # pid via --dashboard-owner-pid. Not meant for manual use.
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--dashboard-owner-pid",
        type=int,
        default=None,
        # Internal: pid the --dashboard-serve child watches (the launching shell for
        # `agitrack -d`, the TUI for the Ctrl-G dashboard). The child shuts itself down
        # when that pid dies, so the dashboard never outlives whatever launched it —
        # even on SIGKILL, which leaves no chance to stop us first.
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--dashboard-port",
        type=int,
        default=None,
        # Internal: port the --dashboard-serve child should bind. Used on RESTART so the
        # replacement daemon keeps the previous URL; falls back to an OS-assigned port when
        # taken. Not meant for manual use.
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--precommit-sync",
        action="store_true",
        # Internal: entry point of the persistent auto-track pre-commit hook. Records any pending
        # AI turns and renders the fold trailer so the commit being made carries the trace, and
        # (unless autotrack_hook=off) auto-starts the background daemon. Best-effort, never fails
        # a commit. Not meant for manual use.
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--background-serve",
        action="store_true",
        # Internal: run the headless background tracker loop in the foreground (this
        # process). `agitrack -b` spawns aGiTrack with this flag as a detached daemon so the
        # launching terminal is freed. Unlike the dashboard daemon it has NO owner-pid
        # watchdog — a tracker must outlive the terminal that started it (stop it with
        # `agitrack -b stop`). Not meant for manual use.
        help=argparse.SUPPRESS,
    )
    # Hand-wrapped, and rendered raw (below): the command list is a table, and argparse's
    # default formatter reflows an epilog into one paragraph, which would run the two commands
    # and their descriptions together.
    parser.epilog = (
        "Commands:\n"
        "  agitrack status   report whether aGiTrack is running for this repository, and\n"
        "                    in which mode (same as -s / --status)\n"
        "  agitrack stop     stop whatever aGiTrack is running for this repository, in any\n"
        "                    mode (the background tracker, an interactive session, this\n"
        "                    repo's dashboard)\n"
        "\n"
        "Any other bare word is reported as an unknown command rather than run as a prompt.\n"
        "Run `agitrack` with no arguments on a terminal to pick a mode from a menu.\n"
        "\n"
        "Unrecognized arguments (options, not bare words) are forwarded verbatim to the\n"
        f"backend CLI ({' / '.join(available_backends())}), e.g.\n"
        "`agitrack --backend opencode --port 12345`. Use `--` to forward arguments that\n"
        "aGiTrack also defines, or a bare prompt, e.g.\n"
        '`agitrack -- --verbose "fix the bug"`. To see the backend CLI\'s own help, run\n'
        "`agitrack -- --help` (or invoke the backend directly)."
    )
    parser.formatter_class = argparse.RawDescriptionHelpFormatter
    # parse_known_args so backend-specific flags pass through instead of erroring.
    args, backend_args = parser.parse_known_args(argv)
    # argparse leaves a single leading "--" separator in the remainder; drop it.
    if backend_args and backend_args[0] == "--":
        backend_args = backend_args[1:]
    # A detached daemon has no terminal: its stdout and stderr ARE its log file
    # (`.agitrack/background.log` for the tracker, `dashboard.log`, the hub's, the backtrace
    # daemon's). Stamp every line each of them writes from here on — as early as this, so the
    # first "aGiTrack is starting..." carries a time too — because those logs are only ever read
    # after the fact, and "did it start, and how long did it take?" cannot be answered from
    # unstamped lines. Nothing PARSES them (every reader is a human following a "see the log for
    # details" message), so a prefix costs nothing.
    if args.background_serve or args.dashboard_serve or args.backtrace_serve or args.hub_serve:
        timestamp_output()
    # `agitrack --repo <path> stop` — the command after its options, which is how half of all
    # CLIs are typed. `_split_command` only claims position 0 (so a prompt starting with the word
    # is safe), and everything else falls through to the backend as a prompt; a lone leftover
    # word that IS a command, with no `--` separator asking for it to be forwarded, is one.
    if command is None and len(backend_args) == 1 and "--" not in argv and _looks_like_a_command(backend_args[0]):
        if backend_args[0] not in _COMMANDS:
            # `agitrack --repo <path> statsu`: the same typo as the one caught before parsing,
            # only typed after the options. It cannot be a flag's value (argparse consumed
            # those) and nothing asked for it to be forwarded, so it is a mistyped command.
            return _unknown_command(backend_args[0])
        command, backend_args = backend_args[0], []
    if command == "status":
        # `agitrack status` is the bare-word spelling of `-s`/`--status`, and answered by the
        # same code below: a command that reports and exits belongs next to `stop`, since
        # someone who knows one will try the other.
        command, args.status = None, True
    # `--repo` parses as None when absent so the two cases stay distinguishable (see its help);
    # from here on it is the plain path every other reader expects.
    repo_given = args.repo is not None
    if not repo_given:
        args.repo = "."

    # First run: ask the user to choose a default backend before launching.
    config = GlobalConfig()

    # Handle help request before any other processing. Show ONLY aGiTrack's own options —
    # not the backend's help (that is available via `agitrack -- --help`, handled below).
    if args.help:
        parser.print_help()
        return 0

    # The Claude Code hook body. Answered before ANY repo discovery, config load or privacy
    # prompt: this runs on every Claude session start in a tracked repo, so it must be cheap
    # and must never prompt — a hook that blocks would hang the user's agent.
    if args.claude_session_note:
        from agitrack.backends.claude_settings import print_session_note

        return print_session_note()

    # The session-start auto-start hook, answered just as early and for the same reasons: it
    # runs on EVERY session start in a tracked repo, so it must be cheap and must never prompt.
    if args.autostart_on_agent:
        from agitrack.proxy.background import autostart_on_agent_hook

        return autostart_on_agent_hook()

    # Print the version and exit. Kept simple and side-effect-free (no repo
    # discovery, no privacy prompt) so tools — e.g. the VSCode extension checking
    # whether the installed CLI has self-updated past it — can read it cheaply.
    if args.version:
        print(_version_line())
        return 0

    if args.hub_serve:
        # Internal entry point: the detached dashboard hub. Answered early and without touching a
        # repository, because it serves ALL of them and belongs to no single one.
        from agitrack.metrics.hub import run_hub_daemon

        return run_hub_daemon(port=args.dashboard_port)

    if command == "stop":
        # `agitrack stop`: stop aGiTrack for THIS repository, whatever mode it is running in.
        # One command instead of three mode-specific ones (`-b stop`, `-d stop`, quitting the
        # TUI by hand), because the user asking for it usually does not know, or care, which
        # mode is holding the repo. No privacy prompt, no repo init, no update check.
        try:
            stop_repo = GitRepo.discover(Path(args.repo).expanduser())
        except (GitError, OSError) as error:
            print(_no_repo_message(Path(args.repo).expanduser(), error))
            return 1
        from agitrack.stop import stop_everything

        return stop_everything(stop_repo, assume_yes=args.yes)

    # Bare `agitrack` on a terminal: ASK which mode, rather than silently starting the one that
    # happened to be the historical default. aGiTrack has several modes that look nothing alike
    # (a TUI that runs the agent, a headless tracker, two dashboards) and nothing on the command
    # line said so. Interactive mode is now requested explicitly with `-i`; the menu is what you
    # get when you request nothing. Without a terminal there is no one to ask, so the historical
    # default (launch the TUI) stands — scripts and editor front-ends are unaffected.
    if _no_mode_requested(args, backend_args) and stdin_is_interactive() and stdout_is_interactive():
        chosen = _choose_mode()
        if chosen is None:
            return 0
        # Re-enter with the chosen mode PLUS whatever the user already typed (--repo, --backend,
        # …), so the menu only ever adds the one thing that was missing.
        return _dispatch([*chosen, *argv])

    if args.daemons == "stop":
        # Stop aGiTrack daemons: every one anywhere, or — with --repo — only that repository's.
        # No repo/git needed for the global form; the registry is user-wide, which is the point:
        # this is the "I have strays I cannot find" escape hatch.
        from agitrack.daemons import list_running, stop_all
        from agitrack.metrics.collect import _abbreviate_home

        scope = str(Path(args.repo).expanduser()) if repo_given else None
        # Show what is about to die BEFORE killing it. Unscoped, this reaches across every
        # repository the user has, so a bare "stop all" typed while thinking about one repo can
        # take down dashboards for four others; the listing is what makes that visible in time.
        # Sessions are listed by `--daemons` but never stopped by it (daemons._STOPPABLE_KINDS),
        # so they must not appear in the "about to stop" listing either.
        doomed = [info for info in list_running(repo=scope) if info.pid != os.getpid() and info.kind != "session"]
        if not doomed:
            where = f" for {_abbreviate_home(scope)}" if scope else ""
            print(f"No aGiTrack daemons are currently running{where}.")
            return 0
        print(f"About to stop {len(doomed)} aGiTrack daemon(s):\n")
        for info in doomed:
            print(f"  {info.pid:>7}  {info.function:<20}  {_abbreviate_home(info.repo)}")
        if not args.yes:
            if not stdin_is_interactive():
                # Previously the confirmation was simply skipped without a tty, so a script or CI
                # job silently killed every daemon on the machine — including the developer's own
                # live session — while `--daemons list` still advertised "(lists them and asks
                # first)". Unattended destruction now has to be asked for.
                print(
                    "\nRefusing to stop them: there is no terminal to confirm on.\n"
                    "Re-run with --yes to stop the daemons listed above"
                    f"{'' if scope else ', or with --repo <path> to stop only one repository'}."
                )
                return 1
            try:
                answer = input("\nStop all of these? [Y/n]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = "n"  # never kill on an unanswered prompt
            if answer in {"n", "no"}:
                print("Cancelled. Nothing was stopped.")
                return 0
        print()
        stopped, survivors = stop_all(repo=scope, log=lambda message: print(f"  {message}"))
        if not stopped and not survivors:
            print("No aGiTrack daemons are currently running.")
        elif stopped:
            print(f"Stopped {stopped} aGiTrack daemon(s).")
        for survivor in survivors:
            # Named, not force-killed: a wedged dashboard is better than one killed mid-write.
            print(f"Could not stop {survivor} — stop it by hand if it is stuck.")
        return 1 if survivors else 0
    if args.daemons:
        # Global, read-only listing of every running aGiTrack daemon — no repo/git needed.
        from agitrack.daemons import list_running
        from agitrack.metrics.collect import _abbreviate_home

        scope = str(Path(args.repo).expanduser()) if repo_given else None
        running = list_running(repo=scope)
        if not running:
            where = f" for {_abbreviate_home(scope)}" if scope else ""
            print(f"No aGiTrack daemons are currently running{where}.")
            return 0
        print(f"aGiTrack daemons running{f' for {_abbreviate_home(scope)}' if scope else ''}:\n")
        print(f"  {'PID':>7}  {'FUNCTION':<20}  DIRECTORY")
        for info in running:
            location = _abbreviate_home(info.repo)
            url = f"   {info.url}" if info.url else ""
            print(f"  {info.pid:>7}  {info.function:<20}  {location}{url}")
        # The per-daemon stop commands act on the CURRENT directory's repo, so they must be run
        # from that directory (shown above) or given its path with --repo.
        print(
            "\nTo stop one:\n"
            "  • by PID:            kill <PID>\n"
            "  • or its own stop command, from that directory (or with --repo <path>):\n"
            "        repo dashboard       agitrack --repo <path> -d stop\n"
            "        backtrace dashboard  agitrack --repo <path> --backtrace stop\n"
            "        background mode      agitrack --repo <path> -b stop"
        )
        # Whoever is reading this listing is usually here BECAUSE they have strays they cannot
        # place, and stopping five daemons one --repo at a time is the tedious way to find that
        # out. The count and the reach are spelled out: this list is every repository, not the
        # current one, so "stop them all" must not read as "stop the ones for this project".
        stoppable = [info for info in running if info.kind != "session"]
        sessions = [info for info in running if info.kind == "session"]
        if sessions:
            # An interactive session is what usually holds a repo lock, so leaving it out of this
            # listing answered "what is aGiTrack running?" with half the truth — and left the
            # reader with no idea why `agitrack` was refusing to start.
            print(
                f"\n{len(sessions)} of these is an interactive session — quit it in its own terminal "
                "(Ctrl-G → quit).\n`--daemons stop` never touches one."
            )
        if stoppable:
            print(
                f"\nTo stop all {len(stoppable)} background daemon(s) above, in every repository:\n"
                "  agitrack --daemons stop        (lists them and asks first)\n"
                "To stop only one repository's, which is usually what you want:\n"
                "  agitrack --repo <path> --daemons stop"
            )
        return 0

    # Backtrace works purely from local transcripts — no git repo AND no git binary needed —
    # so both the user command and its detached child are handled BEFORE the git check below.
    if args.backtrace_serve:
        # Internal entry point: the detached `--backtrace` child. Serves the reconstructed
        # dashboard out-of-process and shuts down when its owner pid dies.
        from agitrack.metrics.backtrace import run_backtrace_daemon

        return run_backtrace_daemon(
            Path(args.repo).expanduser().resolve(),
            owner_pid=args.dashboard_owner_pid,
            # A restart passes the previous port so the URL survives (see start_backtrace_daemon);
            # None means "start from the default and scan upward for the first free one".
            port=args.dashboard_port,
        )

    if args.backtrace:
        # Read-only reconstruction from local transcripts — no git, no privacy prompt, no repo
        # init: it works in ANY directory, including one that was never a repository. The target
        # is the directory itself (--repo, or the cwd), NOT a discovered repo root, so a
        # subdirectory backtraces its own sessions.
        directory = Path(args.repo).expanduser().resolve()
        if not directory.is_dir():
            # Same three cases as `-d` in a non-repository (see _dashboard_without_a_repo): say
            # which of them this is and what to do, rather than one flat sentence for all.
            where = _display_path(directory)
            if not directory.exists():
                print(f"{where} does not exist, so there are no sessions to reconstruct.")
                print("Check the path, or run `agitrack --backtrace` from inside the directory you mean.")
            else:
                print(f"{where} is a file, not a directory.")
                print("Point --repo at the folder the agent worked in (or run it from inside that folder).")
            return 1
        if args.backtrace == "text":
            from agitrack.metrics.backtrace import render_backtrace_text

            print(render_backtrace_text(directory))
            return 0
        if args.backtrace == "stop":
            # The backtrace is a VIEW of a repository on the shared dashboard now, not a daemon of
            # its own, so stopping it means "stop showing this repository" — the same thing
            # `agitrack stop` and `-d stop` mean. A pre-hub standalone daemon is still stopped.
            from agitrack.metrics.backtrace import stop_backtrace_daemon
            from agitrack.metrics.hub import unmount_repo

            unmounted = unmount_repo(directory)
            code = stop_backtrace_daemon(directory) if not unmounted else 0
            if unmounted:
                print("The dashboard is no longer showing this directory.")
            return code
        if args.backtrace == "status":
            from agitrack.metrics.hub import hub_status

            return hub_status()
        if args.backtrace == "commit":
            # `parse_known_args` funnels unknown flags to the backend, which the backtrace path
            # never launches — so they would vanish without a word. Say so rather than leave the
            # user re-running a command that cannot work.
            stray = [a for a in backend_args if a.startswith("-")]
            if stray:
                print(f"Ignoring unrecognized option(s): {' '.join(stray)}")
            # Reconstruct a TRACKED git history: rewrite commits onto a new branch, annotating the
            # AI-made ones with aGiTrack metadata. Requires a git repo + clean tree + a branch name.
            # Always interactive (it rewrites history) — there is no skip-confirmation flag.
            from agitrack.metrics.backtrace_commit import backtrace_commit

            return backtrace_commit(directory, args.backtrace_branch or "")
        # Bare `--backtrace`: open this directory's BACKTRACE view on the shared dashboard. The
        # view is explicit here (unlike `-d`, which lets the repo's state decide), because asking
        # for the reconstruction is exactly what this flag is.
        from agitrack.metrics.suggest import has_backtrace_history

        if not has_backtrace_history(directory):
            # Opening a browser onto an empty reconstruction says "there is nothing here" in the
            # least useful place available. Say it in the terminal the user is already looking at,
            # with what would put something there.
            from agitrack.metrics.backtrace import _empty_message

            print(_empty_message(directory))
            print(
                "\nIf you have used a coding agent here, it may have run in a different directory "
                "(sessions are matched by the working directory the agent recorded).\n"
                "To start recording from now on: run `agitrack` here and pick a mode."
            )
            return 1
        from agitrack.metrics.hub import open_dashboard
        from agitrack.repos import BACKTRACE

        return open_dashboard(directory, view=BACKTRACE)

    # aGiTrack can't do anything without git (every path below discovers/commits to a repo).
    # Check once, up front, so a missing git gives a clear, actionable message instead of a
    # raw FileNotFoundError deep in repo discovery — common right after the VS Code extension
    # installs the CLI but git isn't on PATH. --version/--help above don't need git.
    if shutil.which("git") is None and not _maybe_install_tool("git", required=True):
        print(_git_install_hint())
        return 1

    if args.dashboard_serve:
        # Internal entry point: the detached dashboard child process. `agitrack -d`
        # (and the TUI's Ctrl-G dashboard) spawn this to run the read-only HTTP server
        # out-of-process (#110); it shuts down when its owner pid dies. No privacy
        # prompt / update check (read-only).
        try:
            serve_repo = GitRepo.discover(Path(args.repo).expanduser())
        except (GitError, OSError) as error:
            print(error)
            return 1
        from agitrack.metrics.daemon import EMAIL_LOGINS_ENV, run_dashboard_daemon

        email_logins: dict[str, str] = {}
        raw_logins = os.environ.get(EMAIL_LOGINS_ENV)
        if raw_logins:
            try:
                parsed = json.loads(raw_logins)
                if isinstance(parsed, dict):
                    email_logins = {str(k): str(v) for k, v in parsed.items()}
            except json.JSONDecodeError:
                pass
        if args.dashboard_port is not None:
            return run_dashboard_daemon(
                serve_repo,
                owner_pid=args.dashboard_owner_pid,
                email_logins=email_logins,
                port=args.dashboard_port,
            )
        return run_dashboard_daemon(serve_repo, owner_pid=args.dashboard_owner_pid, email_logins=email_logins)

    if args.dashboard:
        # Read-only: nothing is logged or committed, so no privacy
        # acknowledgment and no repo initialization offer.
        try:
            dashboard_repo = GitRepo.discover(Path(args.repo).expanduser())
        except (GitError, OSError) as error:
            # OSError: --repo points at a directory that does not exist. GitError: it exists but
            # is not a repository. Neither is a reason to print git's sentence and stop — the
            # user asked to SEE something, and there is usually still something to show them or
            # one command that gets them there.
            return _dashboard_without_a_repo(Path(args.repo).expanduser(), error, mode=args.dashboard)
        if args.dashboard == "text":
            from agitrack.metrics import render_dashboard

            print(render_dashboard(dashboard_repo))
            # `-d html` diverts an empty repo to the backtrace view with an explanation, but
            # `-d text` printed a zeroed report and stopped — the cheap, obvious command giving
            # the less helpful answer. Say the same thing it says, without hijacking the output.
            from agitrack.metrics.suggest import has_tracked_tokens

            if not has_tracked_tokens(dashboard_repo):
                print(
                    "\nNo aGiTrack-tracked commits carrying token counts yet, which is why every "
                    "number above is zero.\n"
                    "  • `agitrack` or `agitrack -b` in this repository starts recording them.\n"
                    "  • `agitrack --backtrace` reconstructs what your past agent sessions already "
                    "did here, without waiting."
                )
            return 0
        if args.dashboard == "stop":
            # One dashboard serves every repository, so `-d stop` stops the whole thing — which
            # is what the flag has always meant, and is now visibly wider than it was. Use
            # `agitrack stop` to drop only this repository from it.
            import contextlib
            import io

            from agitrack.metrics import stop_dashboard_daemon
            from agitrack.metrics.hub import stop_hub

            # A pre-hub per-repo daemon, if one survived an upgrade. Silenced: its "no dashboard
            # daemon is running for this repository" is about a thing that no longer exists, and
            # printed next to the hub's own answer it reads as a contradiction.
            with contextlib.redirect_stdout(io.StringIO()):
                stop_dashboard_daemon(dashboard_repo)
            return stop_hub()
        if args.dashboard == "status":
            from agitrack.metrics.hub import hub_status

            return hub_status()
        if args.dashboard == "export":
            from agitrack.metrics.export import ExportTargetError, export_static_demo

            out_dir = (
                Path(args.export_dir).expanduser()
                if args.export_dir
                else dashboard_repo.repo / ".agitrack" / "demo-site"
            )
            try:
                export_static_demo(dashboard_repo, out_dir, force=getattr(args, "force", False))
            except ExportTargetError as error:
                print(error)
                return 1
            except OSError as error:
                # Most often a path too long for the platform (deep nesting near MAX_PATH),
                # which used to surface as a raw traceback naming a RELATIVE path — the one
                # thing that cannot be pasted back into a shell. Half-written output is left
                # in place deliberately; deleting it here would be a second surprise.
                print(f"Could not write the static demo site to {out_dir}: {error}")
                print("The export is incomplete. A shorter --export-dir usually fixes this.")
                return 1
            print(f"Static demo dashboard written to {out_dir}")
            print("Serve the directory with any static web host (or open index.html directly).")
            return 0
        # Bare `-d` / `-d html`: open this repository on the shared dashboard, starting the hub
        # if it is not up. It is NOT bound to this terminal: it keeps serving until
        # `agitrack -d stop`, and restarts itself after aGiTrack updates. Which VIEW opens is
        # decided by the repo's own state (hub.preferred_view) — an empty live dashboard is the
        # worst answer available when the backends' transcripts hold history we could
        # reconstruct, so a repo with nothing tracked yet opens on the backtrace instead.
        from agitrack.metrics.hub import open_dashboard, preferred_view
        from agitrack.metrics.suggest import SUBSTITUTION_NOTICE
        from agitrack.repos import BACKTRACE

        if preferred_view(dashboard_repo.repo) == BACKTRACE:
            print(SUBSTITUTION_NOTICE)
        return open_dashboard(dashboard_repo.repo)

    if args.background in ("stop", "status"):
        # `agitrack -b stop` / `-b status`: signal or report the background tracker running on
        # this repo. Read-only w.r.t. the agent — no privacy prompt, no repo init, no update check.
        try:
            bg_repo = GitRepo.discover(Path(args.repo).expanduser())
        except (GitError, OSError) as error:
            print(error)
            return 1
        from agitrack.proxy.background import background_status, stop_background

        return stop_background(bg_repo) if args.background == "stop" else background_status(bg_repo)

    if args.status:
        # `agitrack --status` / `-s`: report the running mode for this repo. Read-only — no privacy
        # prompt, no repo init, no update check.
        try:
            status_repo = GitRepo.discover(Path(args.repo).expanduser())
        except (GitError, OSError) as error:
            print(_no_repo_message(Path(args.repo).expanduser(), error))
            return 1
        from agitrack.proxy.background import repo_status

        return repo_status(status_repo)

    if args.overwrite_shared and not args.share_sessions:
        # `--overwrite-shared` is a MODIFIER, and it was read nowhere except next to
        # --share-sessions — so on its own it fell straight through to launching a full agent
        # session. Someone who typed it meaning to fix a rejected share got a live agent instead.
        print(
            "--overwrite-shared only means something together with --share-sessions.\n"
            "Did you mean:  agitrack --share-sessions --overwrite-shared"
        )
        return 2

    if args.share_sessions:
        # `agitrack --share-sessions`: push every local session for this repo to origin, then
        # exit. Needs a git repo (the shared store lives on a ref there) but nothing else — no
        # agent spawn, no privacy prompt, no update check.
        try:
            share_repo = GitRepo.discover(Path(args.repo).expanduser())
        except (GitError, OSError) as error:
            print(error)
            return 1
        return _run_share_sessions(share_repo, overwrite=args.overwrite_shared, assume_yes=args.yes)

    if args.remove_hooks:
        # Let the user fully opt out of aGiTrack's commit-time tracking by removing every hook it
        # installed (restoring any chained originals). Read-only w.r.t. the agent — no privacy
        # prompt, no repo init, no update check.
        try:
            rh_repo = GitRepo.discover(Path(args.repo).expanduser())
        except (GitError, OSError) as error:
            print(error)
            return 1
        from agitrack.git import hooks as git_hooks

        removed = git_hooks.remove_all_installed_hooks(rh_repo.hooks_dir())
        # The documented full opt-out has to cover the AGENT-side hooks too, or "removed every
        # hook it installed" would be false: the Claude Code entries (the session note and the
        # turn-end auto-start) would keep running after the user opted out.
        try:
            from agitrack.backends import agent_hooks, claude_settings

            if claude_settings.remove_autostart_hook(rh_repo.repo):
                removed.append("claude Stop")
            if agent_hooks.remove_commit_guidance(rh_repo.repo):
                removed.append("session-note SessionStart")
            if agent_hooks.remove_agent_autostart(rh_repo.repo):
                removed.append("session-start auto-start")
        except Exception:
            pass
        # Persist the opt-out so a later aGiTrack run doesn't silently reinstall the auto-track hook.
        try:
            rh_config = GlobalConfig()
            rh_config.load_repo_overlay(rh_repo.repo)
            rh_config.set("autotrack_hook", "off", scope="repo")
        except Exception:
            pass
        # The other thing aGiTrack wrote into the repo's config and never took back. Left in
        # place, git 2.54 prints a deprecation warning plus 8-9 hint lines on EVERY commit,
        # forever, long after the user has removed aGiTrack — the opposite of an opt-out.
        comment_char_restored = rh_repo.restore_comment_char()
        if removed:
            print(f"Removed aGiTrack git hook(s): {', '.join(removed)}. Any chained project hooks were restored.")
            print("Auto-start is now off for this repo. Re-enable it in Ctrl-G → settings or `agitrack -b`.")
        else:
            print("No aGiTrack hooks were installed in this repository. Auto-start is now off.")
        if comment_char_restored:
            print("Also restored this repo's core.commentChar, which aGiTrack had set.")
        return 0

    if args.precommit_sync:
        # Internal: the persistent auto-track pre-commit hook. Fast, best-effort, never fails a
        # commit — records any pending AI turns and renders the fold trailer for the commit being
        # made, then (unless autotrack_hook=off) auto-starts the background daemon.
        try:
            sync_repo = GitRepo.discover(Path(args.repo).expanduser())
        except (GitError, OSError):
            return 0  # not a repo / bad path ⇒ silently do nothing, never block the commit
        from agitrack.proxy.background import precommit_sync

        return precommit_sync(sync_repo)

    if args.autostart_on_change:
        # Internal: the Claude Code `Stop` hook. Same contract as the pre-commit hook above —
        # fast, best-effort, never fails — but triggered by a finished turn rather than a
        # commit, so tracking resumes on new CODE instead of waiting for the user to commit.
        try:
            change_repo = GitRepo.discover(Path(args.repo).expanduser())
        except (GitError, OSError):
            return 0
        from agitrack.proxy.background import autostart_on_change

        return autostart_on_change(change_repo)

    if args.recover:
        # Headless finalization of work left by a session that exited abruptly.
        # It only commits/merges already-produced changes (never starts new agent
        # work), so — like --dashboard — no privacy prompt and no update check. It
        # takes the repo lock itself and no-ops if a live aGiTrack holds it.
        try:
            recover_repo = GitRepo.discover(Path(args.repo).expanduser())
        except (GitError, OSError) as error:
            print(error)
            return 1
        from agitrack.config.migrate import migrate_repo_state
        from agitrack.recovery import RecoveryService

        migrate_repo_state(recover_repo)
        print(RecoveryService(recover_repo, config).recover().summary())
        return 0

    # If backend is asked for help, run it directly without TUI.
    if backend_args and any(arg in ("--help", "-h") for arg in backend_args):
        backend = args.backend or config.default_backend
        if not backend:
            print("Error: No backend selected. Use --backend to specify one.")
            return 1
        backend_cmd = _backend_command(backend)
        if not backend_cmd:
            print(f"Error: Unknown backend '{backend}'.")
            return 1
        launch, launch_error = _resolve_backend_command(args.backend_command, config, backend)
        if launch_error:
            print(launch_error)
            return 1
        # Launch under the configured wrapper if any (so `agitrack -- --help` shows the
        # backend's help exactly as it runs); otherwise verify the bare binary is on PATH.
        head = launch or [backend_cmd]
        if not launch and not shutil.which(backend_cmd):
            print(f"Error: Backend '{backend}' not found on PATH.")
            return 1
        result = subprocess.run(head + backend_args, check=False)
        return result.returncode

    # `--json` is the documented flag for the JSON prompt-loop; `--mode json` is its hidden
    # deprecated alias. `--prompt` and `--ui-bridge` both drive that same non-interactive loop.
    if args.json_mode:
        args.mode = "json"
    # `--yes` means "don't ask me anything" — the same contract `--prompt` already has, so it
    # rides the same flag. Without it, `agitrack -b` on a fresh config blocked on three startup
    # questions (backend, summarizer model, privacy acknowledgement) whenever it ran ON a
    # terminal, despite -b being the documented "no TUI, returns to your shell" path: a scripted
    # or CI use inside a terminal simply hung, and `-b status` skipping the wizard made the two
    # inconsistent. Answering with the defaults is what --yes is for.
    scripted = bool(args.prompts) or bool(getattr(args, "yes", False))
    if scripted:
        args.mode = "json"  # --prompt drives the non-interactive shell (#53)
    if args.ui_bridge:
        args.mode = "json"  # the bridge is a json-mode transport (the VSCode extension)

    # Give immediate feedback that aGiTrack is launching — the interactive TUI takes a
    # few seconds to come up (update check, backend startup) and otherwise the terminal
    # looks frozen. Printed for interactive proxy mode only, so it never pollutes the
    # machine-readable json/bridge output or the cheap --version/--dashboard paths. Shown
    # however aGiTrack was started (terminal or VSCode), then replaced by the TUI frame.
    if args.mode == "proxy":
        print("aGiTrack is starting...", flush=True)

    # Make sure git can actually commit: without a global user.name/user.email every commit
    # fails with "Author identity unknown", and aGiTrack commits each turn. Prompt for any
    # missing value on an interactive launch; the MSI bundle defers this to the installer,
    # and scripted/json runs are left clean (those users have git configured).
    if args.mode == "proxy" and not _installed_via_msi() and stdin_is_interactive() and stdout_is_interactive():
        _ensure_git_identity()

    # Make the global config self-documenting: write any settings still missing from
    # ~/.agitrack/config.json with their built-in defaults, so a user opening the file
    # sees every available knob. Only fills gaps (never overwrites a user value) and only
    # writes when something was missing, so it is a no-op on every run after the first.
    # Placed after the cheap --version/--dashboard paths return, so those stay side-effect-free.
    getattr(config, "seed_defaults", lambda: False)()

    # Offer a self-update before launching anything. Skipped for scripted/non-TTY
    # runs (no way to answer) and when the user turned update checks off. If the
    # user accepts, aGiTrack updates and re-execs immediately — no sessions are
    # running yet at startup, so there is nothing to finalize first.
    if not scripted and stdin_is_interactive() and stdout_is_interactive():
        _check_for_update_at_startup(config)

    # First-run backend setup. Runs whenever no default backend is configured (and one wasn't
    # passed via --backend) so the user always chooses one before launch — NOT gated on a
    # backend being missing: with both already installed but no default saved, skipping this
    # used to drop straight to the "No coding agent backend is configured" error every launch.
    # Skipped for the MSI bundle (the installer handles it) and for scripted/non-TTY runs (no
    # way to answer). select_default_backend lists statuses, offers to install any missing
    # ones, asks which to use as the default, and explains how to change it later.
    if (
        args.backend is None
        and not config.has_default_backend()
        and not scripted
        and not _installed_via_msi()
        and stdin_is_interactive()
        and stdout_is_interactive()
    ):
        chosen_backend = select_default_backend(config)
        # First run also picks the default summarizer model, saved to the global config.
        select_default_summarizer_model(config, chosen_backend)

    if backend_args:
        _warn_reserved_passthrough(args.backend or config.default_backend, backend_args)

    try:
        repo = _discover_or_init(Path(args.repo).expanduser())
    except OSError as error:
        # --repo points at a directory that does not exist / can't be read.
        print(error)
        return 1
    if repo is None:
        return 1

    if _refuse_during_merge_conflict(repo):
        return 1

    # Migrate a pre-rename ``.agit/`` state dir (and its worktrees) to ``.agitrack/``
    # before anything reads state, so existing sessions survive the upgrade.
    from agitrack.config.migrate import migrate_repo_state

    migrate_repo_state(repo)

    # Now that we know the repo, layer its local settings (.agitrack/config.json) over
    # the global config, then resolve the effective settings. A CLI flag always wins;
    # otherwise the (repo-overlaid) config value applies. getattr keeps a config written
    # before these keys existed (or a partial stub) working with the defaults.
    getattr(config, "load_repo_overlay", lambda _root: None)(repo.repo)
    share_config_error = getattr(config, "share_config_error", lambda: None)()
    if share_config_error:
        print(f"Configuration error: {share_config_error}")
        return 1
    manual_commits = True if getattr(args, "manual_commits", False) else getattr(config, "manual_commits", False)
    # --auto-commit forces auto commits even over a configured 'manual_commits': true. This
    # override used to sit INSIDE the `if background:` block below, so in the TUI the flag was a
    # silent no-op: the turn stayed latent on refs/agitrack/manual/…, the branch never advanced,
    # and nothing said why. It applies to every mode, which is what its --help always implied.
    if getattr(args, "auto_commit", False):
        manual_commits = False
    # Background (headless) mode: aGiTrack tracks a user-driven native backend session instead
    # of running the interactive TUI. `-b`/`--background` (const "run") or the 'background' config
    # key enable it; `-b stop`/`status` were handled earlier and never reach here. Background
    # mode ALWAYS runs without a worktree, and uses AUTO commits by default (like interactive
    # mode); --manual-commits / -m (or config manual_commits) opts into manual.
    background = (getattr(args, "background", None) == "run") or getattr(config, "background", False)
    # Manual-commit mode edits the current branch directly and defers commits to the user, so
    # it necessarily runs without a worktree (there is no per-turn branch to integrate).
    use_worktrees = False if (args.no_worktree or manual_commits or background) else config.use_worktrees
    commit_guidance = False if args.no_commit_guidance else getattr(config, "commit_guidance", True)
    sandbox_enabled = False if args.no_sandbox else getattr(config, "sandbox", True)
    # Event-log path: a per-run --log-file wins over the configured log_file; None ⇒ no log.
    log_file_spec = args.log_file if args.log_file is not None else getattr(config, "log_file", None)
    if args.allowed_edit_paths is not None:
        allowed_edit_paths = [p for p in args.allowed_edit_paths.split(os.pathsep) if p.strip()]
    else:
        allowed_edit_paths = getattr(config, "allowed_edit_paths", [])
    # Resolve the backend this run will use: an explicit --backend wins, else the
    # (repo-overlaid) configured default. There is no hardcoded fallback — if neither
    # exists we must not silently pick an agent. The interactive first-run prompt above
    # fills in a default when possible; reaching here without one means a non-interactive
    # run (scripted / no TTY) with nothing configured, so fail clearly instead.
    effective_backend = args.backend or config.default_backend
    if not effective_backend:
        # Exactly one backend installed ⇒ there is nothing to choose. Refusing here made an
        # explicitly HEADLESS mode require a terminal for its first run: with no TTY, bare
        # `agitrack` and `agitrack -b` both stopped at this gate identically whether one or two
        # backends were present, so the entire documented first-run prompt chain was unreachable
        # and five separate scenarios in the live test dead-ended on it. A single unambiguous
        # candidate is the one case where picking for the user cannot be picking wrong; it is
        # announced, and it is NOT persisted as the global default — that choice stays theirs.
        installed = [name for name in available_backends() if backend_installed(name)]
        if len(installed) == 1:
            effective_backend = installed[0]
            # Carry it downstream the same way an explicit --backend does. Every launcher below
            # reads args.backend, so resolving it only into this local would have left them
            # resolving "no backend" all over again a few lines later.
            args.backend = effective_backend
            print(f"Using the only coding agent backend installed on this machine: {effective_backend}.")
            print("Choose a different default anytime with `--backend <name>` or Ctrl-G → backend.")
        else:
            # Offer only what is actually here. The old message listed all three unconditionally,
            # so on a box with one backend two of its three suggestions would have failed.
            options = "|".join(installed or available_backends())
            print(
                "No coding agent backend is configured. Run aGiTrack in an interactive "
                f"terminal to choose a default, or pass --backend <{options}>."
            )
            if not installed:
                print("None of the supported backends are installed on this machine yet.")
            return 1
    # Resolve the backend launch wrapper (--backend-command, else config) for the backend
    # this run will use. Validate the flag here so a malformed value fails fast and clearly.
    backend_command, backend_command_error = _resolve_backend_command(args.backend_command, config, effective_backend)
    if backend_command_error:
        print(backend_command_error)
        return 1
    if not _confirm_backend_command_mismatch(effective_backend, backend_command, scripted=scripted):
        print("aGiTrack not started.")
        return 1

    # Take the single-writer lock up front — BEFORE any interactive startup prompt — and
    # hold it for the whole session. Besides refusing a second instance immediately, this
    # makes the lock (carrying our PID) present from the very start, so a session still
    # sitting at a startup prompt is already "locked". The VSCode extension reads this lock
    # to tell a starting/running session apart from a dead shell; holding it from the start
    # is what lets the aG button reliably focus the existing terminal instead of opening a
    # second one. (It was a read-only probe before, so no lock was held during startup and
    # the extension couldn't yet see the session.)
    management_lock = RepoLock(repo.repo / ".agitrack" / "lock")
    # A `--background-serve` child is a DAEMON TAKING OVER, not a second writer asking
    # permission: it is spawned either by the `-b` launcher (which released the lock first) or
    # by a tracker restarting itself onto new code (which now does too). Either way its
    # predecessor may still be dying with the lock held — on Windows a byte-range lock outlives
    # the process by the time the kernel tears its handles down — so asking once loses a race it
    # should always win. Without this the successor concluded a live tracker was already there,
    # exited 0, and the repo was left with nothing tracking it.
    if not management_lock.acquire(retry_seconds=5.0 if args.background_serve else 0.0):
        owner_pid = management_lock.owner_pid()
        replaced = False
        if args.background_serve:
            # Never the launcher's "already running — left in place": that branch exists to stop
            # a SECOND tracker starting, and this process IS the tracker. Reporting success here
            # is what turned a lost handoff into a silently untracked repo.
            print(
                f"aGiTrack background tracker could not take this repo's single-writer lock "
                f"(held by PID {owner_pid}); not starting.",
                flush=True,
            )
            return 1
        if background:
            # `agitrack -b` over a live background tracker replaces it (like re-running
            # `-d`/`--backtrace`): stop the old daemon cleanly and take over — so a rerun
            # after an aGiTrack update always runs the new code. Anything else holding the
            # lock (an interactive session) still refuses below.
            from agitrack.proxy.background import _running_tracker_is_current, replace_running_tracker

            # But if that tracker is ALREADY the current version, there is no new code to load —
            # leave it running instead of tearing it down and respawning. This needless restart
            # churn is what made the daemon appear to "quit" on every unrelated aGiTrack invocation.
            # A rerun asking for a DIFFERENT commit mode (`-b -m` over auto, `-b --auto-commit`
            # over manual) is a mode switch, so it must replace the daemon rather than be told
            # "already running" — otherwise the requested mode is silently ignored.
            if _running_tracker_is_current(repo, owner_pid=owner_pid, manual=manual_commits):
                print(
                    f"aGiTrack background tracker already running (PID {owner_pid}, current version) — left in place."
                )
                return 0
            # retry_seconds: the daemon we just terminated may still hold the OS lock for a
            # moment — on Windows `TerminateProcess` flips pid_alive instantly but the byte-range
            # lock survives ~100 ms of handle teardown. Without the retry this lost 9 times out
            # of 9 and left ZERO writers on the repo.
            stopped = replace_running_tracker(repo, owner_pid=owner_pid)
            replaced = stopped and management_lock.acquire(retry_seconds=3.0)
            if stopped and not replaced:
                # Do NOT fall through to "already running (PID N)": that names the PID we just
                # killed and tells the user to stop a process that no longer exists.
                print(
                    "Stopped the previous background tracker, but could not take over this "
                    "repo's lock in time.\nNothing is tracking it now — re-run `agitrack -b`."
                )
                return 1
        if not replaced:
            print(already_running_message(owner_pid, repo_root=repo.repo))
            return 1

    try:
        if background:
            # Headless background tracker (issue #143): no TUI, no PTY takeover. aGiTrack watches
            # the user-driven backend session and tracks it. Show the privacy warning first (it
            # auto-proceeds without a TTY — so the interactive launcher below acknowledges it,
            # then hands the detached child `--skip-privacy-ack`).
            if not _acknowledge_privacy_warning(scripted=scripted, skip=args.skip_privacy_ack):
                return 1
            if BackgroundRunner is None:  # pragma: no cover - platform without proxy support
                print("Background mode is not available on this platform yet.")
                return 1
            if args.background_serve:
                # We ARE the detached daemon child: run the tracker loop in the foreground of
                # this (already-detached) process, holding the repo lock for our whole run.
                #
                # Every exit from here goes through the log, including the ones that raise. This
                # process has no terminal — its stdout IS .agitrack/background.log — and a
                # daemon that died on the way up used to leave that log holding a bare
                # "aGiTrack is starting..." and nothing else: no reason, no traceback, no exit
                # code. A restart that strands a repo untracked is bad; one that leaves no
                # evidence of why is worse, because it cannot be diagnosed after the fact.
                try:
                    code = BackgroundRunner(
                        repo,
                        verbose=args.verbose,
                        backend=args.backend,
                        new_session=args.new_session,
                        manual_commits=manual_commits,
                        commit_guidance=commit_guidance,
                        backend_command=backend_command,
                        log_file=log_file_spec,
                        _lock=management_lock,
                    ).run()
                except BaseException as error:
                    # BaseException, not Exception: a KeyboardInterrupt or a SystemExit raised
                    # mid-startup ends the daemon just as dead, and is just as worth recording.
                    import traceback

                    print(
                        f"aGiTrack background tracker FAILED to start: {error!r}\n{traceback.format_exc()}",
                        flush=True,
                    )
                    raise
                if code != 0:
                    print(f"aGiTrack background tracker exited with code {code}.", flush=True)
                return code
            # Explain the auto-start hooks and let the user decide (once per repo) whether to
            # keep them after this tracker exits — before we spawn anything. The backend is
            # resolved first because only Claude Code gets the turn-end hook, and the prompt has
            # to describe what will actually be installed on THIS user's machine.
            _maybe_prompt_background_hook(
                config, scripted=scripted, backend=_autostart_backend(repo, args.backend, config)
            )
            # Launcher: spawn the tracker as a DETACHED daemon (like `agitrack -d`) so the
            # terminal is freed, then return to the shell. The child re-execs aGiTrack with
            # --background-serve and takes its own lock, so release ours first (the child owns
            # the single-writer lock for the daemon's lifetime; stop it with `agitrack -b stop`).
            management_lock.release()
            from agitrack.proxy.background import start_background_daemon

            child_args: list[str] = []
            if args.backend:
                child_args += ["--backend", args.backend]
            # Force the resolved commit mode explicitly so the child's own config can't flip it.
            child_args.append("--manual-commits" if manual_commits else "--auto-commit")
            if args.new_session:
                child_args.append("--new-session")
            # Same reasoning as the commit mode above: the opt-out is a property of THIS
            # invocation, and the daemon is the process that installs the guidance hook, so it
            # has to be told rather than left to re-derive it from a config that may differ.
            if not commit_guidance:
                child_args.append("--no-commit-guidance")
            if args.verbose:
                child_args.append("--verbose")
            if args.backend_command:
                child_args += ["--backend-command", args.backend_command]
            # Forward a per-run --log-file (a configured log_file the child reads itself).
            if args.log_file:
                child_args += ["--log-file", args.log_file]
            # The dashboard shows up when aGiTrack starts on a repo, in EVERY mode — including
            # this one, where there is no TUI to reach it from.
            #
            # AFTER the daemon, not before. `start_background_daemon` waits for the tracker to
            # publish its handshake, and that handshake is exactly what the dashboard's status
            # reads: opening the browser first meant the page asked "is aGiTrack running here?"
            # of a tracker that did not exist yet and was told "no", then sat on that answer.
            # A failed start is not opened at all — the terminal is where that news belongs.
            code = start_background_daemon(repo, extra_args=child_args)
            if code == 0:
                _open_dashboard_on_start(repo, config, scripted=scripted)
            return code
        if args.mode == "json":
            # json/scripted mode has no interactive pre-TUI configuration steps, so show the
            # privacy warning here (it auto-proceeds without a TTY) before the shell starts.
            # Under --json-events (and not the bridge, which frames everything itself) stdout is
            # a pure JSON stream, so the banner goes to stderr — a driver's json.loads(line)
            # used to throw on the very first line it read, before `ready` ever arrived.
            banner_stream = sys.stderr if (args.json_events and not args.ui_bridge) else sys.stdout
            if not _acknowledge_privacy_warning(scripted=scripted, skip=args.skip_privacy_ack, stream=banner_stream):
                management_lock.release()
                return 1
            management_lock.release()  # json/scripted mode runs via AgitrackShell, which takes its own lock
            # Propagate the shell's exit code: a scripted run that could not start (no backend
            # installed, another aGiTrack on the repo) otherwise exited 0 and was indistinguishable
            # from a successful turn to whatever script invoked it.
            return AgitrackShell(
                repo,
                verbose=args.verbose,
                backend=args.backend,
                new_session=args.new_session,
                backend_args=backend_args,
                backend_command=backend_command,
                prompts=args.prompts,
                commit_guidance=commit_guidance,
                json_events=args.json_events,
                ui_bridge=args.ui_bridge,
                log_file=log_file_spec,
            ).run()
        else:
            # Before the TUI takes over the terminal, check the GitHub CLI and let the
            # user install/log in or continue without it (the TUI would otherwise leave
            # no shell prompt to act on the gh warning).
            proceed, gh_handled = _check_gh_availability(repo, scripted=scripted)
            if not proceed:
                return 1
            # Warn about a menu key the host likely intercepts (e.g. VS Code's Ctrl-G) and
            # let the user test/replace it now — the only chance before the TUI takes over.
            if not _verify_menu_key(config, scripted=scripted):
                return 1
            if ProxyRunner is None:  # pragma: no cover - platform without proxy support
                print("The interactive aGiTrack TUI is not available on this platform yet.")
                return 1
            return ProxyRunner(
                repo,
                verbose=args.verbose,
                backend=args.backend,
                new_session=args.new_session,
                use_worktrees=use_worktrees,
                manual_commits=manual_commits,
                backend_args=backend_args,
                backend_command=backend_command,
                commit_guidance=commit_guidance,
                full_agent_messages=args.full_agent_messages,
                delay_merge=args.delay_merge,
                sandbox=sandbox_enabled,
                allowed_edit_paths=allowed_edit_paths,
                log_file=log_file_spec,
                gh_prechecked=gh_handled,
                skip_privacy_ack=args.skip_privacy_ack,
                # The dashboard shows up when aGiTrack starts on a repo, in every mode, on the
                # view that fits the repo. Handed to the runner rather than called here so it
                # happens after ITS startup questions (the privacy acknowledgment, the pre-agent
                # commit) rather than in the middle of them.
                on_startup_complete=lambda: _open_dashboard_on_start(repo, config, scripted=scripted),
                _lock=management_lock,
            ).run()
    except (GitError, RuntimeError) as error:
        print(error)
        return 1
    finally:
        management_lock.release()  # idempotent: run()/json mode already released on their own paths
    return 0


# Flags aGiTrack injects itself to manage session tracking; forwarding a duplicate
# can fight aGiTrack's own session handling. We warn but still forward — aGiTrack never
# silently swallows the user's intent.
_RESERVED_PASSTHROUGH = {
    "claude": {"--session-id", "--resume", "-r", "--continue", "-c"},
    # codex resumes via a `resume <id>` SUBCOMMAND rather than a flag, so the reserved
    # entries are the subcommand plus the flags aGiTrack sets on the launch line: `-C`
    # (working root) and `-c` (config override, which `--backend-args` could otherwise
    # use to re-pin the model or sandbox aGiTrack just chose).
    "codex": {"resume", "-C", "--cd", "-c", "--config", "--last"},
    "opencode": {"--session", "-s", "--continue", "-c"},
}


def _resolve_backend_command(
    flag_value: str | None, config: GlobalConfig, backend: str
) -> tuple[list[str], str | None]:
    """Resolve the command that launches the backend, replacing its executable with a
    user wrapper. The ``--backend-command`` flag (a shell-split string) wins; otherwise
    the per-backend ``backend_command`` config value applies. Returns ``(tokens, error)``
    — ``tokens`` empty means "launch the backend directly"; a non-None ``error`` is a
    user-facing message for a malformed flag (so the caller can stop)."""
    if flag_value is None:
        getter = getattr(config, "backend_command", None)
        return (list(getter(backend)) if callable(getter) else [], None)
    import shlex

    try:
        # posix=False on Windows so backslashes in paths (e.g. C:\tools\wrapper.exe) are
        # kept literally rather than treated as shell escapes.
        tokens = shlex.split(flag_value, posix=(os.name != "nt"))
    except ValueError as error:
        return ([], f"Invalid --backend-command {flag_value!r}: {error}")
    if not tokens:
        return ([], "Invalid --backend-command: the command is empty.")
    return (tokens, None)


def _confirm_backend_command_mismatch(backend: str, backend_command: list[str], *, scripted: bool) -> bool:
    """When the launch command clearly names a *different* known backend than the selected
    one (e.g. ``--backend claude --backend-command "wrap opencode"``), warn and require
    explicit confirmation before proceeding. aGiTrack tracks transcripts/sessions per the
    selected backend, so a wrapper that execs another backend silently breaks that tracking
    — the user must opt in. Returns True to proceed, False to abort.

    Only an unambiguous mismatch prompts — a known backend name appears in the command but
    the selected one does not. An opaque wrapper (no known backend named, e.g.
    ``mylauncher``) or a consistent command proceeds silently. Without a way to ask
    (scripted/non-interactive), the warning is printed and the run proceeds, since
    automation can't answer a prompt and must not hang on one."""
    if not backend_command:
        return True
    named = {os.path.basename(token) for token in backend_command}
    if backend in named:
        return True  # the command names the selected backend — consistent
    others = sorted(named & (set(available_backends()) - {backend}))
    if not others:
        return True  # opaque wrapper — don't guess which backend it runs
    print(
        f"Warning: --backend is '{backend}' but the launch command names "
        f"{', '.join(others)}. aGiTrack tracks sessions for '{backend}', so a wrapper "
        f"that runs a different backend will break session/transcript tracking. Pass "
        f"--backend {others[0]} (or set default_backend) if that's what you meant."
    )
    if scripted or not (stdin_is_interactive() and stdout_is_interactive()):
        return True  # can't prompt here; proceed with the warning rather than hang automation
    try:
        # _ask drains injected input first so a stray newline can't auto-confirm (same
        # reason as the privacy acknowledgment): this must be a deliberate keypress.
        answer = _ask(f"Proceed with backend '{backend}' anyway? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in {"y", "yes"}


def _warn_reserved_passthrough(backend: str | None, backend_args: list[str]) -> None:
    reserved = _RESERVED_PASSTHROUGH.get(backend or "", set())
    hit = sorted({arg for arg in backend_args if arg in reserved})
    if hit:
        print(
            f"Warning: forwarding {', '.join(hit)} to {backend}; aGiTrack manages "
            "session selection itself, so this may interfere with its session tracking."
        )


def _check_for_update_at_startup(config: GlobalConfig) -> None:
    """At startup, check for a newer aGiTrack and, if one exists, prompt the user to
    install it now. Best-effort: any failure (no network, no upstream) is
    swallowed so it can never block launching aGiTrack."""
    # Skip when checks are off, or when the attribute is absent — a config that
    # doesn't carry the update preference (e.g. a test stub) has nothing to read
    # or persist, so there is no update flow to run.
    if not getattr(config, "check_for_updates", None):
        return
    try:
        from agitrack.update import STARTUP_NET_TIMEOUT, Updater, restart_agitrack

        updater = Updater()
        # Bound the launch-time check tightly so an offline / bad-connection user
        # isn't blocked from starting aGiTrack (the network call fails fast and we just
        # skip the offer below).
        status = updater.check(timeout=STARTUP_NET_TIMEOUT)
    except KeyboardInterrupt:
        # The check was slow (e.g. a sluggish `git fetch`) and the user pressed Ctrl-C.
        # Treat it as "skip the update check and get on with launching" — never dump a
        # traceback over a best-effort, optional check.
        print("Skipped the update check.")
        return
    except Exception:
        return
    # A prior automatic update may have failed (or the user chose not to retry). Clear
    # that reminder the moment aGiTrack is actually current; otherwise honour it below.
    pending = getattr(config, "pending_manual_update", None)
    if pending and status.ok and not status.available:
        config.pending_manual_update = None
        pending = None
    if not status.ok or not status.available:
        return
    if pending:
        # Don't re-run the interactive auto-update — it already failed. Show a single
        # startup reminder with how to update by hand; the user keeps running the
        # current version and can also retry via the Ctrl-G 'update' menu.
        print(f"\nReminder: {status.message}")
        print(
            f"The automatic update did not complete earlier. To update aGiTrack, {updater.manual_update_instructions()}"
        )
        return
    print(f"\n{status.message}")
    try:
        # Default (empty Enter) is to update — that's the recommended action, so
        # make it the path of least resistance and say so in the prompt.
        answer = _ask("Update aGiTrack now? [Y]es / [n]o / [never] ask again: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return
    if answer in {"never", "no ask", "stop"}:
        config.check_for_updates = False
        print(
            "aGiTrack will no longer check for updates (re-enable with check_for_updates in ~/.agitrack/config.json)."
        )
        return
    if answer not in {"", "y", "yes"}:
        return
    print("Updating aGiTrack...")
    result = updater.apply()
    if not result.ok:
        # The automatic update failed. Keep aGiTrack running on the current version,
        # tell the user how to update by hand, and remember to remind (once) at the
        # next startup rather than nagging during the session.
        config.pending_manual_update = status.latest or status.current or "available"
        print(f"Update failed: {result.error}")
        print(f"aGiTrack will keep running the current version. To update it, {updater.manual_update_instructions()}")
        return
    config.pending_manual_update = None  # a successful update clears any prior reminder
    # Windows MSI build: apply() only DOWNLOADED the installer (it replaces the running
    # agitrack.exe, so it can't install in place). Hand off to the elevated installer, which
    # installs after we exit and relaunches the updated build; quit so that can proceed. Without
    # this the startup path would just re-exec the current version and re-offer the update every
    # launch (an endless "update available" loop that never installs).
    if getattr(updater, "pending_msi_path", None):
        if updater.launch_msi_bootstrapper():
            print(f"{result.message} aGiTrack will reinstall and reopen automatically.")
            sys.exit(0)
        config.pending_manual_update = status.latest or status.current or "available"
        print(f"Could not start the aGiTrack installer. To update it, {updater.manual_update_instructions()}")
        return
    # Windows package installs can't replace the running agitrack.exe in place (the OS locks
    # it), so apply() defers the pip upgrade to a helper that runs after we exit. Spawn it and
    # quit so the upgrade can proceed; the helper relaunches aGiTrack itself when it's done.
    if getattr(updater, "pending_pip_upgrade", None):
        if updater.launch_pip_bootstrapper():
            print(f"{result.message} aGiTrack will reopen automatically once it completes.")
            sys.exit(0)
        # Couldn't start the helper — keep the current version and remind the user to update.
        config.pending_manual_update = status.latest or status.current or "available"
        print(f"Could not start the aGiTrack updater. To update it, {updater.manual_update_instructions()}")
        return
    print(f"{result.message} Restarting aGiTrack...")
    restart_agitrack()  # does not return on success


# The privacy warning as one flowing paragraph; it is wrapped per terminal width at
# print time (see `_privacy_warning`) so it never overflows or is chopped mid-word on a
# narrow terminal.
_PRIVACY_WARNING_TEXT = (
    "WARNING: aGiTrack records the conversation in git commit messages — every "
    "message you enter in the chat can become part of the repository history. "
    "Do not enter passwords, API keys, or other sensitive information in the "
    "chat. (Keeping secrets out of prompts is good practice anyway.)"
)
# The width the warning is authored to wrap at on a normal/wide terminal; a narrower
# terminal wraps tighter than this so the text always fits.
_PRIVACY_WARNING_WIDTH = 73


def _privacy_warning(width: int | None = None) -> str:
    """The startup privacy warning, wrapped to fit the terminal. Wraps at the authored
    width on a normal/wide terminal, but re-wraps at the terminal's actual width when it is
    narrower, so the line breaks land in different places (and nothing overflows) on a small
    terminal. ``width`` defaults to the detected terminal width. Keeps the leading blank
    line the message has always had."""
    import textwrap

    if width is None:
        width = shutil.get_terminal_size().columns
    wrap_at = max(20, min(_PRIVACY_WARNING_WIDTH, width))
    return "\n" + textwrap.fill(_PRIVACY_WARNING_TEXT, width=wrap_at)


def _drain_terminal_input() -> None:
    """Discard any unread bytes in the controlling terminal's input queue.

    A no-op when stdin isn't a real tty. Used before every interactive startup question so
    input the user did NOT type at that question — a stray Enter pressed while an installer
    ran, or a command injected by an editor's shell integration / venv-activation hook —
    can't answer it. Best-effort: never raises."""
    from agitrack.console import drain_terminal_input

    drain_terminal_input()


def _ask(question: str) -> str:
    """``input()`` for a startup question, with the terminal's input queue drained first.

    Every pre-TUI prompt goes through this. The startup flow interleaves questions with
    steps that can take minutes (installing git / gh / an agent CLI, the update check), and
    keys pressed while one of those ran would otherwise be delivered to the next question —
    which then flashes past already answered, looking as if it had been skipped."""
    _drain_terminal_input()
    return input(question)


def _acknowledge_privacy_warning(*, scripted: bool = False, skip: bool = False, stream=None) -> bool:
    """Show the privacy warning at startup; the user must acknowledge it to
    continue. Without a TTY there is no way to acknowledge, and a scripted run
    (``--prompt``) already has its input on the command line, so in both cases
    the warning is printed and aGiTrack proceeds — never block automation on an
    ``input()`` that cannot be answered.

    ``skip`` suppresses the warning entirely; aGiTrack sets it when re-exec'ing
    itself after an in-app (menu) update, where the user acknowledged the
    warning earlier this session and should not be prompted again.

    ``stream`` redirects the banner. Under ``--json-events`` stdout carries one JSON object per
    line and nothing else, so a banner printed there made the obvious ``json.loads(line)`` throw
    on the very first line a driver read — before ``ready`` ever arrived."""
    if skip:
        return True
    print(_privacy_warning(), file=stream or sys.stdout)
    if scripted or not (stdin_is_interactive() and stdout_is_interactive()):
        return True
    # _ask discards anything already sitting in the terminal's input queue before reading, so
    # a stray newline can't auto-acknowledge this. Editors that host aGiTrack in a terminal
    # (e.g. the VSCode extension) — or their shell-integration / venv-activation hooks —
    # can inject a command whose trailing Enter would otherwise answer this prompt for the
    # user. The acknowledgment must be a deliberate keypress.
    try:
        answer = _ask("Press Enter to acknowledge and continue (q to quit): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\naGiTrack not started.", file=stream or sys.stdout)
        return False
    if answer in {"q", "quit", "n", "no"}:
        print("aGiTrack not started.", file=stream or sys.stdout)
        return False
    return True


def _dashboard_without_a_repo(directory: Path, error: Exception, *, mode: str) -> int:  # noqa: ARG001
    """`agitrack -d` where there is no git repository. Returns a process exit code.

    "Not a Git repository: ." was the whole answer, which tells someone who has just asked to look
    at their own work neither what is wrong nor what to do. There are three genuinely different
    situations here and they deserve three different answers:

    * **The directory does not exist.** Nothing to discuss; say which path, and stop.
    * **It exists, is not a repository, but coding agents HAVE worked here.** The backtrace needs
      no git at all, so there is something to show: show it, and say why this is not the dashboard
      that was asked for.
    * **It exists and there is nothing here.** Say so, and give the two commands that change that.
    """
    if not directory.exists():
        print(f"{_display_path(directory)} does not exist, so there is nothing to show.")
        print("Check the path, or run aGiTrack from inside the project you want to look at.")
        return 1
    if not directory.is_dir():
        print(f"{_display_path(directory)} is a file, not a project directory.")
        print("Point --repo at the project's folder (or run `agitrack -d` from inside it).")
        return 1

    from agitrack.metrics.suggest import has_backtrace_history

    where = _display_path(directory)
    if has_backtrace_history(directory):
        # The reconstruction reads the agents' own transcripts, so it works in a directory that
        # was never a repository. Refusing to show it here would be withholding the one view that
        # CAN answer the question.
        print(
            f"{where} is not a Git repository, so there are no tracked commits to chart.\n"
            "Coding-agent sessions HAVE run here though, so showing the BACKTRACE view instead: "
            "what those past conversations did to these files, reconstructed from the transcripts "
            "on this machine.\n"
            "To track new work here properly:\n"
            "  git init          then commit your files\n"
            "  agitrack          and pick a mode"
        )
        if mode == "text":
            from agitrack.metrics.backtrace import render_backtrace_text

            print()
            print(render_backtrace_text(directory))
            return 0
        from agitrack.metrics.hub import open_dashboard
        from agitrack.repos import BACKTRACE

        return open_dashboard(directory, view=BACKTRACE)
    print(
        f"{where} is not a Git repository, and no coding-agent sessions have run here, so there "
        "is nothing for the dashboard to show yet.\n"
        "To start:\n"
        "  git init          make this a repository\n"
        "  agitrack          pick a mode and let aGiTrack track the work\n"
        "Already have a project elsewhere? Run `agitrack -d` inside it, or "
        "`agitrack -d --repo <path>`."
    )
    return 1


def _display_path(path: Path) -> str:
    """A path as the user would recognize it (absolute, ``~`` for home), for a message.

    RESOLVED first: the default is the cwd, and a message that names the directory as "." tells
    someone reading it back later nothing at all about which directory was meant."""
    try:
        from agitrack.metrics.collect import _abbreviate_home

        try:
            path = path.resolve()
        except OSError:
            path = path.absolute()
        return _abbreviate_home(str(path))
    except Exception:
        return str(path)


def _open_dashboard_on_start(repo: GitRepo, config, *, scripted: bool = False) -> None:
    """Open this repository's dashboard as aGiTrack starts on it, whatever mode it starts in.

    The dashboard is where aGiTrack's work becomes visible, and until now you only saw it if you
    knew to ask (`-d`, or Ctrl-G inside the TUI). Since one hub serves every repository on one
    port, opening it costs a browser tab rather than a new server, and which VIEW opens is the
    repo's own state to decide: a project with nothing tracked yet lands on the reconstruction of
    its past sessions instead of on an empty page.

    Never for a scripted or non-interactive run: a CI job or an editor front-end has no browser to
    open, and a background daemon spawning one would be a surprise. Off entirely via
    ``open_dashboard_on_start`` in config."""
    if scripted or not stdout_is_interactive():
        return
    if not getattr(config, "open_dashboard_on_start", True):
        return
    try:
        from agitrack.metrics.hub import open_dashboard

        # quiet: the launch banner for the mode being started is the message that matters here,
        # and three lines about the dashboard in front of it would bury it.
        # `starting_tracking`: this is `-b` or `-i`, which is the user saying "record this
        # repository from now on". The reconstruction is the inferred history of what happened
        # BEFORE, so it is never what to open here — asking for it has its own command.
        #
        # Said out loud first, because the call below can take a couple of seconds and is the last
        # thing between the user and the mode they asked for: it starts the hub daemon if this is
        # the first repository of the day, then gives an already-open dashboard tab its chance to
        # take the navigation before a new one is opened. Silence there reads as aGiTrack hanging
        # on startup for no reason; one line makes the same wait obviously purposeful.
        print("Opening the aGiTrack dashboard in your browser…", flush=True)
        open_dashboard(repo.repo, quiet=True, starting_tracking=True)
    except Exception:
        pass  # a dashboard that will not open must never stop the mode the user asked for


def _check_gh_availability(repo: GitRepo, *, scripted: bool = False) -> tuple[bool, bool]:
    """Before the TUI takes over the terminal, check the GitHub CLI (``gh``) and let the
    user act on it. aGiTrack uses ``gh`` for the dashboard's committer identities and for
    session sharing; once the full-screen TUI starts there is no shell prompt left to run
    ``gh auth login`` in, so we surface it here while stdin is still an ordinary terminal.

    Only prompts when ``gh`` is missing or not signed in **and** the repo has a GitHub
    remote (where ``gh`` actually matters) — a local-only / non-GitHub repo is never nagged.
    Offers to log in inline (``gh auth login`` runs right here) or continue without it.

    Returns ``(proceed, handled)``: ``proceed`` is False only when the user chose to quit;
    ``handled`` is True when the interactive prompt was shown, so the runner can skip its
    own in-TUI gh notice. Never blocks automation — without an interactive TTY (or in
    scripted mode) it does nothing and returns ``(True, False)``. The MSI bundle also does
    nothing — gh setup/login there is the installer's job; a pip/source install still does it."""
    if _installed_via_msi() or scripted or not (stdin_is_interactive() and stdout_is_interactive()):
        return (True, False)
    from agitrack.metrics.github import commit_url_base, gh_status

    status = gh_status()
    if status == "ok":
        return (True, False)  # installed and authenticated — nothing to do
    if not commit_url_base(repo):
        return (True, False)  # no GitHub remote — gh isn't needed here yet
    if status == "missing":
        # Offer to install gh automatically; if it lands, it still needs a login, so fall
        # through to the unauthenticated branch below (re-checking its real status).
        if _maybe_install_tool("gh", required=False):
            status = gh_status()
            if status == "ok":
                return (True, True)
    if status == "missing":
        # Two leading newlines: the first ends any partial line left by the install attempt's
        # subprocess output, the second separates this section from it.
        print("\n\n" + _gh_install_hint())
        prompt = "\nPress Enter to continue without it (q to quit): "
    else:  # unauthenticated
        print(
            "\n\nGitHub CLI (gh) isn't signed in. aGiTrack uses it for the dashboard's committer "
            "identities and for session sharing; without it those features are limited.\n\n"
            "Signing in takes a moment and only has to be done once."
        )
        # Signing in is the recommended action, so it is the DEFAULT (bare Enter): the
        # features that depend on gh are the ones the user came for, and someone who
        # deliberately wants to run without gh can still say so with 's'.
        prompt = "\nPress Enter to run `gh auth login` now ([s] skip, q to quit): "
    # _ask drains injected input first so a stray newline can't auto-answer this (same reason
    # as the privacy acknowledgment) — the choice must be a deliberate keypress.
    try:
        answer = _ask(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\naGiTrack not started.")
        return (False, True)
    if answer in {"q", "quit"}:
        print("aGiTrack not started.")
        return (False, True)
    if status == "unauthenticated" and answer not in {"s", "skip", "n", "no"}:
        _run_gh_login()
    return (True, True)


def _confirm_bulk_share(repo: "GitRepo", *, overwrite: bool, assume_yes: bool = False) -> bool:
    """Ask before `--share-sessions` uploads anything. True to proceed.

    Says what is uploaded EVERY time, not once — a bulk share is a fresh upload of possibly
    sensitive transcripts each run, which is exactly why the in-TUI share re-shows its warning
    on every share rather than remembering an answer.

    Without a terminal there is nobody to ask, so it refuses and names `--yes`: a script that
    was going to publish conversations must say so explicitly. `--overwrite-shared` additionally
    REWINDS collaborators' copies, so it is called out separately."""
    notice = [
        f"About to share every local agent session for {repo.repo} to 'origin'.",
        "",
        "Each session's full transcript is uploaded: your prompts, the agent's replies, and the",
        "inputs to its tools — which can include file contents, command output, and secrets.",
        "Review what is in these conversations before sharing.",
    ]
    if overwrite:
        notice.append("")
        notice.append("--overwrite-shared: this REPLACES shared copies that already have newer turns.")
    print("\n".join(notice))
    if assume_yes:
        return True
    if not stdin_is_interactive():
        print("\nNot sharing: there is no terminal to confirm on. Re-run with --yes to share anyway.")
        return False
    try:
        answer = _ask("\nShare them now? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if not answer.startswith("y"):
        print("Nothing was shared.")
        return False
    try:
        GlobalConfig().acknowledge_session_sharing()
    except Exception:
        pass
    return True


def _run_share_sessions(repo: "GitRepo", *, overwrite: bool = False, assume_yes: bool = False) -> int:
    """`--share-sessions`: push every local session for ``repo`` to origin, reporting as it goes.

    Progress is printed per session because a bulk share is one network round trip each — a
    silent minute on a repo with twenty conversations reads as a hang. The exit code reflects
    whether anything actually FAILED: sessions skipped as unchanged, or refused because the
    shared copy is newer, are normal outcomes of a re-run and are not errors.
    """
    from agitrack.sessions.bulk import share_all

    def progress(index: int, total: int, candidate) -> None:  # noqa: D401 - inner helper
        print(f"  [{index}/{total}] {candidate.name} ({candidate.backend})… ", end="", flush=True)

    def on_result(outcome) -> None:
        detail = f" — {outcome.detail}" if outcome.detail else ""
        print(f"{outcome.status}{detail}", flush=True)

    # INFORMED CONSENT, the same requirement the in-TUI share has enforced all along. This path
    # published every local session's full transcript — every prompt, every reply, tool inputs
    # including file contents — to 'origin' with no consent prompt and no privacy sentence, and
    # never read or set `session_sharing_acknowledged`. One command, many conversations, and it
    # is exactly the bulk case where a stray secret is most likely to be in one of them.
    if not _confirm_bulk_share(repo, overwrite=overwrite, assume_yes=assume_yes):
        # A refusal for want of a terminal is not "nothing to do": a CI job that asked to share
        # and got exit 0 back would report success having published nothing. Answering "no" at
        # the prompt IS a deliberate outcome, so that one stays 0 — the same split `--daemons
        # stop` makes between its no-tty refusal (1) and its cancelled confirmation (0).
        return 0 if sys.stdin.isatty() else 1

    print(f"Sharing local sessions for {repo.repo} to origin…")
    result = share_all(repo, progress=progress, on_result=on_result, overwrite=overwrite)
    print(f"\n{result.summary()}")
    if result.shared:
        print(f"Shared as '{result.login}/…'. Collaborators see them after `agitrack --backtrace`.")
    failed = [o for o in result.outcomes if o.status == "failed"]
    if failed:
        print(f"\n{len(failed)} session(s) could not be shared.")
        return 1
    return 0


def _run_gh_login() -> None:
    """Run ``gh auth login`` interactively in the current terminal (we are still in cooked
    mode, before the TUI). Best-effort: any failure is reported and aGiTrack continues."""
    try:
        subprocess.run(["gh", "auth", "login"], check=False)
    except (OSError, subprocess.SubprocessError) as error:
        print(f"Could not run `gh auth login`: {error}")


def _autostart_backend(repo, requested: str | None, config: GlobalConfig) -> str | None:
    """The backend the tracker being started will actually use, resolved the way the daemon
    resolves it: this run's ``--backend``, else the one this repo last recorded, else the
    global default. Used only to describe the hooks accurately — see below."""
    if requested:
        return requested
    try:
        from agitrack.config import AgitrackState

        recorded = AgitrackState(repo.repo).data.get("backend")
    except Exception:
        recorded = None
    return recorded or getattr(config, "default_backend", None)


def _maybe_prompt_background_hook(config: GlobalConfig, *, scripted: bool, backend: str | None = None) -> None:
    """When starting `agitrack -b`, explain the auto-start hooks and let the user decide whether
    to enable them. When enabled (the default), a `git commit` made while aGiTrack isn't running
    folds the AI trace into that commit AND auto-starts the tracker (in the same commit mode as
    the last run) for the turns that follow; on Claude Code a turn-end hook additionally starts
    it as soon as a turn leaves changes behind, without waiting for a commit. Sets the
    repo-scoped ``autotrack_hook`` ("auto"/"off"). Never blocks automation (no-TTY / scripted →
    default on).

    ``backend`` decides WHICH of those the prompt describes. Only Claude Code exposes a turn-end
    hook, so telling a Codex or OpenCode user about a ``.claude/settings.local.json`` that will
    never be written is worse than saying nothing: it describes an install that does not happen,
    and it hides the limitation that actually applies to them — nothing is picked up until they
    commit."""
    if scripted or not (stdin_is_interactive() and stdout_is_interactive()):
        return
    try:
        # Skip only when auto-start is ALREADY enabled for this repo (an explicit repo-scoped
        # "auto"). Re-ask whenever it's off — including after `agitrack --remove-hooks`, which sets
        # it off — so the user can turn it back on; and ask on the first run (default is a global,
        # not repo-scoped, "auto").
        if config.autotrack_hook == "auto" and config.source("autotrack_hook") == "repo":
            return
    except Exception:
        return
    commit_hook = (
        "  * a git pre-commit hook: when you `git commit` later with aGiTrack down, that commit's\n"
        "    AI work is recorded INTO the commit, and tracking restarts (in the same auto/manual\n"
        "    mode as your last run) for the turns that follow.\n"
    )
    # Where each backend's start-on-session-open hook is written. All three have one; only the
    # file differs, and naming the actual file is what lets the user go look at it.
    session_hook_file = {
        "claude": "this repo's .claude/settings.local.json",
        "codex": "this repo's .codex/hooks.json",
        "opencode": "this repo's .opencode/plugin/",
    }.get(backend or "", "this repo's agent config")
    session_hook = (
        f"  * a session-start hook for your agent, in {session_hook_file}: opening a session in\n"
        "    this repo starts tracking right then — including the first one after a reboot.\n"
    )
    if backend == "claude":
        detail = (
            "\naGiTrack can keep tracking this repo when it isn't running, by installing three hooks —\n"
            "so tracking survives you closing the terminal, or a reboot:\n"
            "\n"
            + commit_hook
            + session_hook
            + "  * a turn-end hook for Claude Code, in this repo's .claude/settings.local.json: when a\n"
            "    turn leaves changes behind and nothing is tracking, tracking starts right then,\n"
            "    without waiting for a commit.\n"
        )
        switches = (
            "`agitrack -b stop` turns them off until you start again;\n"
            "`agitrack --remove-hooks` disables them for good."
        )
    else:
        # Codex earns one extra sentence, and only Codex: it is the single backend that will not
        # run a hook until a person has reviewed it, so an install that looks complete here does
        # nothing until they do. Saying it at the prompt is cheaper than the user discovering it
        # as "auto-start just doesn't work".
        trust = (
            "\nCodex runs a new hook only after you review it: run `/hooks` in Codex once and trust\n"
            "aGiTrack's session hooks.\n"
            if backend == "codex"
            else ""
        )
        detail = (
            "\naGiTrack can keep tracking this repo when it isn't running, by installing two hooks —\n"
            "so tracking survives you closing the terminal, or a reboot:\n"
            "\n" + commit_hook + session_hook + trust + "\n"
            "Only Claude Code exposes a turn-end hook, so on this backend a session that is already\n"
            "open when tracking stops is picked up at your next commit.\n"
        )
        switches = (
            "`agitrack -b stop` turns them off until you start again;\n"
            "`agitrack --remove-hooks` disables them for good."
        )
    print(
        detail + "\n"
        "Your commit stays your own; a purely human commit (no AI work) is left untouched, and\n"
        "nothing aGiTrack writes is ever staged.\n" + switches
    )
    try:
        answer = _ask("Enable auto-start? [Y/n]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return
    if answer.startswith("n"):
        config.set("autotrack_hook", "off", scope="repo")
        print("\naGiTrack: auto-start off — tracking runs only while `agitrack -b` is up.")
    else:
        config.set("autotrack_hook", "auto", scope="repo")
        print("\naGiTrack: auto-start enabled. Disable it anytime with `agitrack --remove-hooks`.")


def _verify_menu_key(config: GlobalConfig, *, scripted: bool = False) -> bool:
    """Before the TUI starts, warn when the configured menu key is likely intercepted by
    the host (e.g. VS Code binds Ctrl-G to "Go to Line"), and let the user test it, switch
    to another key, or keep it. The menu can't be opened from inside the TUI if its key
    doesn't reach aGiTrack, so this is the one chance to fix it while a shell prompt exists.

    Returns True to proceed, False to abort. A changed key is persisted to the global
    config (which the runner re-reads). Never blocks automation — without an interactive
    TTY, or in scripted mode, it does nothing and returns True."""
    if scripted or not (stdin_is_interactive() and stdout_is_interactive()):
        return True
    key = config.menu_key
    conflict = settings.detect_menu_key_conflict(key, os.environ)
    if conflict is None:
        return True  # no known conflict — don't bother the user
    if config._raw("menu_key_acknowledged") == key:
        return True  # the user already resolved/confirmed this key in this environment
    label = settings.menu_key_label_for(key)
    print(f"\nHeads up: aGiTrack's menu key is {label}, but {conflict}")
    print("Once the TUI starts you can't open the aGiTrack menu if that key never arrives.")
    while True:
        try:
            answer = (
                _ask(f"[t] test {label} now   [c] choose a different key   [Enter] keep it   [q] quit: ")
                .strip()
                .lower()
            )
        except (EOFError, KeyboardInterrupt):
            print("\naGiTrack not started.")
            return False
        if answer in {"q", "quit"}:
            print("aGiTrack not started.")
            return False
        if answer in {"", "k", "keep"}:
            config.set("menu_key_acknowledged", key, scope="global")  # don't nag next launch
            return True
        if answer in {"t", "test"}:
            _run_menu_key_test(key)
            continue
        if answer in {"c", "change", "choose"}:
            chosen = _choose_menu_key(config, key)
            if chosen is None:
                continue  # backed out — re-show the options
            config.set("menu_key", chosen, scope="global")
            config.set("menu_key_acknowledged", chosen, scope="global")
            print(f"Menu key set to {settings.menu_key_label_for(chosen)}.")
            return True
        print("Please choose t, c, Enter, or q.")


def _choose_menu_key(config: GlobalConfig, current: str) -> str | None:
    """Prompt for a replacement menu key, offering known-good suggestions and an optional
    test. Returns the canonical key chosen, or None if the user backed out."""
    suggestions = settings.suggest_menu_keys(current, os.environ)
    if suggestions:
        print("Suggested: " + ", ".join(settings.menu_key_label_for(k) for k in suggestions))
    while True:
        try:
            raw = _ask("New menu key (e.g. ctrl-o or ctrl+shift+g; blank to go back): ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if not raw:
            return None
        chosen = settings.normalize_menu_key(raw)
        if chosen is None:
            print("Not a valid menu key. Use ctrl-<letter> (not c/h/i/j/m) or ctrl+shift+<letter>.")
            continue
        if settings.detect_menu_key_conflict(chosen, os.environ):
            print(f"Note: {settings.menu_key_label_for(chosen)} may also be intercepted by the host.")
        if not _confirm_menu_key_by_test(chosen):
            continue  # the test failed and the user declined to use it anyway — pick another
        return chosen


def _confirm_menu_key_by_test(key: str) -> bool:
    """Offer to test *key*; if the test shows it doesn't reach aGiTrack, ask whether to use
    it anyway. Returns True to accept *key*, False to pick a different one."""
    label = settings.menu_key_label_for(key)
    try:
        if _ask(f"Test {label} now? [Y/n]: ").strip().lower() in {"n", "no"}:
            return True  # user skipped the test — accept the key as entered
    except (EOFError, KeyboardInterrupt):
        return True
    result = _run_menu_key_test(key)
    if result is not False:
        return True  # worked, or the test was cancelled/unavailable
    try:
        return _ask(f"{label} didn't reach aGiTrack. Use it anyway? [y/N]: ").strip().lower() in {"y", "yes"}
    except (EOFError, KeyboardInterrupt):
        return False


def _run_menu_key_test(key: str) -> bool | None:
    """Prompt the user to press *key* and report whether it reached aGiTrack. Returns True
    (reached), False (swallowed by the host / timed out), or None (cancelled/unavailable)."""
    label = settings.menu_key_label_for(key)
    print(f"Press {label} now (you have a few seconds)…")
    result = _read_menu_key_press(settings.menu_key_bytes_for(key), shift=settings.menu_key_is_shift(key))
    if result is True:
        print(f"  ✓ {label} reached aGiTrack — it will open the menu inside the TUI.")
    elif result is False:
        print(f"  ✗ {label} did NOT reach aGiTrack — the host likely intercepts it; choose another key.")
    else:
        print(f"  (skipped the {label} test)")
    return result


def _read_menu_key_press(expected: bytes, *, shift: bool, timeout: float = 8.0) -> bool | None:
    """Put the terminal in raw mode and wait up to *timeout* for *expected* to arrive on
    stdin. True if it does (so it will open the menu in the TUI), False on timeout (the
    host swallowed it), None if the user pressed Ctrl-C or the terminal can't go raw.

    This is the authoritative check the issue asks for: a key intercepted by VS Code (or
    any host) never reaches stdin here, so the test fails exactly when the TUI would."""
    if os.name == "nt":  # native Windows has no termios/tty — read the console via msvcrt
        return _read_menu_key_press_windows(expected, shift=shift, timeout=timeout)
    import select
    import termios
    import time
    import tty

    try:
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
    except (termios.error, ValueError, OSError):
        return None  # not a real tty (or redirected stdin) — can't test
    try:
        tty.setraw(fd)
        if shift:
            # Ask the terminal to report shifted control keys (kitty keyboard protocol).
            # If it doesn't support them, the sequence never arrives and the test fails —
            # which is correct, since the key wouldn't work in the TUI either.
            os.write(sys.stdout.fileno(), b"\x1b[>1u")
        buffer = bytearray()
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            readable, _, _ = select.select([fd], [], [], remaining)
            if not readable:
                return False
            chunk = os.read(fd, 64)
            if not chunk:
                return False
            buffer += chunk
            if b"\x03" in buffer:  # Ctrl-C cancels the test (never a valid menu key)
                return None
            if expected in buffer:
                return True
    finally:
        if shift:
            try:
                os.write(sys.stdout.fileno(), b"\x1b[<u")
            except OSError:
                pass
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        except (termios.error, ValueError, OSError):
            pass


def _read_menu_key_press_windows(expected: bytes, *, shift: bool, timeout: float) -> bool | None:
    """Native-Windows port of :func:`_read_menu_key_press` (#118).

    The Windows console hands control keys (Ctrl-G = ``0x07``) straight through
    ``msvcrt.getch`` with no echo or line buffering, so a key the host (VS Code) intercepts
    never arrives here — exactly as it wouldn't reach the TUI. Returns True if *expected*
    arrives, False on timeout, None on Ctrl-C / no console. The kitty-protocol shifted-key
    reporting the POSIX path enables isn't available on the Windows console, so a shift-based
    menu key simply times out here — which is correct, since it wouldn't work in the TUI."""
    import time

    try:
        import msvcrt
    except ImportError:  # pragma: no cover - msvcrt is always present on Windows
        return None
    # Bind the console readers once; the ignores cover mypy on POSIX, where it (correctly)
    # sees no win32 attributes on the ``msvcrt`` stub. The dispatcher only reaches here on
    # Windows; the tests substitute a fake msvcrt so this stays exercised on the POSIX gate.
    kbhit = msvcrt.kbhit  # type: ignore[attr-defined]
    getch = msvcrt.getch  # type: ignore[attr-defined]
    buffer = bytearray()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not kbhit():
            time.sleep(0.02)
            continue
        char = getch()
        if char in (b"\x00", b"\xe0"):
            # A function/arrow key: a lead byte followed by a scancode. Consume the scancode
            # so it isn't mis-read as a separate keypress; it's never a valid menu key here.
            if kbhit():
                getch()
            continue
        buffer += char
        if b"\x03" in buffer:  # Ctrl-C cancels the test (never a valid menu key)
            return None
        if expected in buffer:
            return True
    return False


def _refuse_during_merge_conflict(repo: GitRepo) -> bool:
    """True (and explain) when the repo is mid-conflict, so the caller can stop.

    aGiTrack must not start on top of an unresolved conflict. Everything it does assumes it can
    read the tree as the user's work and commit it: the pre-agent commit would fold half-merged
    files (conflict markers included) into a commit, the worktree a session is checked out from
    would inherit that state, and the agent would be handed a tree the user is still repairing.
    Refusing is also the only honest answer — there is no reading of "commit this" that is right
    while a merge is unresolved.

    Detected from the unmerged index entries rather than from ``MERGE_HEAD``, so a rebase,
    cherry-pick or revert that stopped on a conflict is caught too — none of those write
    ``MERGE_HEAD``, and all of them leave the same half-resolved tree.
    """
    try:
        conflicted = repo.unmerged_paths()
    except Exception:
        return False  # never block a start on a failed check
    if not conflicted:
        return False
    shown = conflicted[:20]
    print(f"\naGiTrack can't start: {repo.repo} has an unresolved merge conflict.")
    print("\nThese files still have conflicts:")
    for path in shown:
        print(f"  {path}")
    if len(conflicted) > len(shown):
        print(f"  … and {len(conflicted) - len(shown)} more")
    print(
        "\nResolve them first (edit the files, `git add` each one, then finish with\n"
        "`git commit` / `git rebase --continue`), or abandon the merge with\n"
        "`git merge --abort`. Then start aGiTrack again."
    )
    return True


def _version_line() -> str:
    """What ``agitrack --version`` prints — see :func:`agitrack.versioning.version_line`, which is
    the single definition every version indicator shares (the dashboard shows the same string)."""
    from agitrack.versioning import version_line

    return version_line()


# How many of a folder's repositories to name before summarising the rest.
_NESTED_REPO_SAMPLE = 5

# How many directory entries to look at when asking "does this folder HOLD repositories?".
# A projects folder shows its hand in the first few entries; the cap keeps the question cheap
# in a directory with thousands of them.
_NESTED_REPO_SCAN_LIMIT = 500


def _repositories_inside(path: Path) -> list[str]:
    """The names of ``path``'s immediate subfolders that are themselves Git repositories.

    ``.git`` is tested for EXISTENCE, not for being a directory: a linked worktree and a
    submodule both record it as a file, and either is still a repository someone works in."""
    found: list[str] = []
    try:
        entries = list(itertools.islice(path.iterdir(), _NESTED_REPO_SCAN_LIMIT))
    except OSError:
        return []
    for entry in entries:
        try:
            if (entry / ".git").exists():
                found.append(entry.name)
        except OSError:  # pragma: no cover - an entry that disappeared mid-scan
            continue
    return sorted(found)


def _folder_of_repositories_note(path: Path, found: list[str]) -> str:
    """Why aGiTrack cannot start in a folder that merely CONTAINS repositories.

    Running aGiTrack one directory too high — in the folder that holds the projects rather than
    in a project — is an easy mistake to make and, answered with a bare "Not a Git repository",
    an opaque one: the folder is full of repositories, so it looks like a place aGiTrack should
    work. It is not. aGiTrack tracks ONE repository, whose commits carry the conversation; a
    ``git init`` here would make a single new project out of the whole folder, and would still
    not track a thing in the repositories inside it (git does not descend into them, so their
    work would land as nothing but a name in the enclosing tree)."""
    shown = ", ".join(found[:_NESTED_REPO_SAMPLE])
    if len(found) > _NESTED_REPO_SAMPLE:
        shown += f", and {len(found) - _NESTED_REPO_SAMPLE} more"
    plural = "" if len(found) == 1 else "s"
    return (
        f"{_display_path(path)} is not a Git repository itself, but {len(found)} folder{plural} "
        f"inside it {'is' if len(found) == 1 else 'are'}:\n"
        f"  {shown}\n"
        "aGiTrack tracks one repository at a time, so `cd` into the project you want and run it "
        "there."
    )


# Said only where a repository is about to be CREATED — the reason the offer below defaults to no.
_INIT_HERE_WARNING = (
    "Initializing a repository HERE would make one project out of the whole folder, and would\n"
    "still not track the work in those subfolders."
)


def _no_repo_message(path: Path, error: Exception) -> str:
    """A failed ``GitRepo.discover`` as a message for a read-only command (``status``, ``stop``).

    "Not a Git repository: ." is true and useless in the folder that holds the user's projects,
    which is exactly where someone lands who ran the command one directory too high."""
    found = _repositories_inside(path)
    return f"{error}\n{_folder_of_repositories_note(path, found)}" if found else str(error)


def _discover_or_init(path: Path) -> GitRepo | None:
    """Find the Git repository for ``path``, or offer to create one. aGiTrack cannot
    run outside a Git repository, so if the user declines (or we can't prompt),
    return None and let the caller stop.

    A folder that HOLDS repositories is the one case where the offer is not the helpful
    default — see :func:`_folder_of_repositories_note`."""
    try:
        repo = GitRepo.discover(path)
        # A user who ran `git init` themselves leaves an unborn HEAD (no commits),
        # which aGiTrack's worktree setup cannot use. Seed an initial commit so an
        # otherwise-empty repository starts cleanly.
        if repo.ensure_born():
            print(f"Seeded an initial commit in empty repository {repo.repo}")
        return repo
    except GitError:
        pass
    nested = _repositories_inside(path)
    if not (stdin_is_interactive() and stdout_is_interactive()):
        if nested:
            print(_folder_of_repositories_note(path, nested))
            return None
        print(f"Not a Git repository: {path}\naGiTrack requires a Git repository to run.")
        return None
    try:
        if nested:
            # Default NO here, and only here. `git init` in a folder of projects is a big,
            # surprising thing to do on an accidental Enter — it makes every one of those
            # projects a subdirectory of a new repository — and it is not what someone who
            # ran aGiTrack one level too high meant to ask for.
            print(f"{_folder_of_repositories_note(path, nested)}\n{_INIT_HERE_WARNING}")
            answer = _ask("Initialize a repository here anyway with `git init`? [y/N] ").strip().lower()
            if answer not in {"y", "yes"}:
                print("aGiTrack not started.")
                return None
        else:
            # Default YES: aGiTrack cannot track anything without a repo, so declining ends the
            # run. Enter should take the path that lets the user get started, not the one that
            # quits.
            answer = (
                _ask(f"{path} is not a Git repository. Initialize one here with `git init`? [Y/n] ").strip().lower()
            )
            if answer in {"n", "no"}:
                print("aGiTrack cannot run outside a Git repository. Exiting.")
                return None
    except (EOFError, KeyboardInterrupt):
        # No way to answer ⇒ do NOT create a repo the user never asked for.
        print("aGiTrack cannot run outside a Git repository. Exiting.")
        return None
    try:
        repo = GitRepo.init(path)
    except GitError as error:
        print(error)
        return None
    print(f"Initialized empty Git repository in {repo.repo}")
    return repo


if __name__ == "__main__":
    raise SystemExit(main())
