# aGiT

aGiT stands for agent + git. It is an interactive Python CLI that wraps coding-agent command line tools and Git so agentic code changes are committed with traceable metadata.

aGiT supports OpenCode and Claude (Claude Code) as interchangeable backends. Every aGiT feature works the same regardless of the selected backend.

## Install

For local development:

```bash
python3 -m pip install -e .
```

This installs the `agit` command and the terminal UI dependency used for status bars and contextual command hints.

### Contributing

Install the optional pre-commit hooks for fast local lint/format on every commit (same checks as CI):

```bash
uv sync --group dev
uv run pre-commit install
```

The hooks run `ruff` (lint + format) and basic file hygiene. Tests, coverage, and type checks run in CI.

## Usage

Run in the current repository:

```bash
agit
```

By default, `agit` runs in proxy mode: it launches the real backend TUI (OpenCode or Claude) in a pseudo-terminal, renders it through an internal terminal screen, and reserves a bottom status line for aGiT. Press `Ctrl-G` to enter aGiT command mode (configurable via `menu_key` in `~/.agit/config.json` — see Configuration).

Run against another repository:

```bash
agit --repo /path/to/repo
```

Choose the backend (also saved as the global default for future runs):

```bash
agit --backend claude
agit --backend opencode
```

By default aGiT resumes the previous conversation for the repository. Start a fresh one instead with:

```bash
agit --new-session
```

Run without a worktree (the agent edits the current branch directly, so changes are visible live as it works):

```bash
agit --no-worktree
```

This is for single-session use: there's no isolation or auto-integration, and concurrent sessions are unsafe in this mode (starting a new session is blocked). Set `"use_worktrees": false` in `~/.agit/config.json` to make it the default; `--no-worktree` always wins.

### Forwarding arguments to the backend

aGiT does not reduce the backend's own functionality: any argument it doesn't recognize is forwarded verbatim to the backend CLI (`claude` / `opencode`).

```bash
agit --backend opencode --port 12345      # --port 12345 goes to opencode
```

Use `--` to forward an argument that aGiT also defines (e.g. `--verbose`), or to pass a bare prompt:

```bash
agit -- --verbose "fix the bug"           # everything after -- goes to the backend
```

aGiT's own flags (`--repo`, `--verbose`, `--mode`, `--backend`, `--new-session`, `--no-worktree`) bind to aGiT when they appear before `--`. Note that aGiT manages session selection itself, so forwarding session flags (`--resume`, `--session-id`, `--session`, `--continue`) may interfere with its session tracking — it warns when you do.

On the first run, aGiT asks which backend should be the default (listed alphabetically, with each backend's install status). If the chosen backend's CLI is not installed, aGiT shows install instructions and lets you install it or pick a different one. The choice is saved in `~/.agit/config.json` (`default_backend`) and reused for future runs. You can also switch backends mid-session with the `agent-backend` command below.

In proxy mode (default), press `Ctrl-G`, then type one of these aGiT commands:

```text
session                   switch / start (own worktree) / stop a live session
agent-backend             switch backend (opencode|claude); shows a picker
git-base-branch           switch the branch sessions integrate into
git-status                show git status
git-stage                 review and stage untracked files
git-unstaged              show intentionally unstaged files
git-user-commit           create a user commit
exit                      exit (with confirmation)
```

aGiT tracks one session per repository and stays pinned to the session it launched (so it does not drift to other sessions you open). Use the `session` command (`Ctrl-G`, then `session`) to start a new session, switch the tracked session to another existing one, or sync tracking to the most recently active session — for example after starting a new conversation inside the backend's own TUI. This works the same for all backends.

Only `session` starts with `s`, so `Ctrl-G` then `s` + Enter jumps straight to the session picker. The session menu marks each session `running` or `idle`. Git-specific commands share a `git-` prefix.


Proxy mode launches the backend's native TUI directly and recovers session metadata for automatic agent commits — via `opencode export` for OpenCode, or by reading the session transcript under `~/.claude/projects/` for Claude.


## How It Works

### Backends

aGiT drives an external coding agent rather than calling a model directly. OpenCode and Claude (Claude Code) are interchangeable backends, and every aGiT feature behaves the same regardless of which is selected. The per-repository backend is recorded in state; the user-wide default lives in `~/.agit/config.json`.

aGiT recovers what to commit from the backend's own session record: `opencode export` for OpenCode, and the session transcript under `~/.claude/projects/` for Claude. In JSON mode it instead invokes the backend non-interactively per prompt and captures the final response.

### Session tracking

aGiT tracks exactly one backend session per repository and stays pinned to the session it launched, so it does not drift to other conversations you open in the backend. On startup it baselines the tracked session, so token metadata and the interaction trace only cover turns that happen after aGiT starts watching — resuming an old conversation does not re-commit its history.

Use the `session` command to start a new session, switch the tracked session to another existing one, or sync tracking to the most recently active session (for example after starting a fresh conversation inside the backend's own TUI). The session menu marks each session `running` or `idle`.

### Worktrees and branches

To let sessions run without stepping on each other or on your working tree, each aGiT session runs in its own git worktree under `.agit/worktrees/<name>`, created *detached* at the base branch — a session has no branch of its own. Work within a session is committed on per-turn branches named `agit/<backend>/<name>/t<turn>`, created lazily on the first commit of each turn; once a turn is integrated its branch is deleted and the worktree is detached at the new base again. All aGiT-managed branches live under the `agit/` prefix so they are easy to recognize for cleanup and never collide with your own branches.

The base working tree (the branch you launched from) is only ever advanced by **integration**: aGiT merges a session's pending commits back into the base branch rather than committing onto it directly. A single-writer lock ensures only one aGiT process auto-commits or integrates at a time, so concurrent sessions stay consistent.

### Integration and startup recovery

When a session's commits are merged into the base branch and the merge has conflicts, the agent backend resolves them, and the resolution is recorded as an `<agent-merge>` commit listing the base commits it was resolved against.

On startup, aGiT reconciles worktrees left behind by previous runs: it integrates any pending commits into the base branch and then deletes the worktree. Worktrees that cannot be integrated cleanly (a conflict, or uncommitted changes) are kept so no work is lost. The backend conversation itself persists (keyed by the worktree path) and stays resumable.

### Commit message format

aGiT commit messages use a consistent Markdown-style structure. The first line is the subject (prefixed with `<agent>`, `<agent-merge>`, or left plain for user commits). The body is organized into `#` sections — `# Full Subject`, `# Interaction Trace`, `# aGiT Metadata` — with `## User` / `## Agent` subsections inside the interaction trace. Commits are written with `git commit -F -` (no editor), so the `#` lines are preserved rather than stripped as git comments. Secrets and terminal escape sequences are masked out of subjects and trace bodies before committing.

## Commit Behavior

- Tracked modifications and deletions are staged with `git add -u`.
- New untracked files require confirmation before staging.
- Declined untracked files are remembered in repository-local `.agit/state.json`.
- Agent commits use the `<agent>` tag and include the full interaction trace since the last code-changing commit.
- Agent commit metadata includes context token count and generated token usage accumulated since the last code-changing commit.
  - Token figures are read directly from the backend's session transcript (each assistant message's reported usage) and broken out by category: `input`, `output`, `cache_read`, `cache_write`, and (when the backend reports it) `reasoning`. For Claude, the recorded output count already includes extended-thinking and tool-call tokens. Sub-agent/sidechain turns are counted separately under the matching `subagent_*` categories rather than dropped. Each category is recorded only when the backend reports a non-zero value, so backends that omit a field (e.g. OpenCode does not expose sub-agent usage) simply have no line for it.
  - The categories are **non-overlapping**: `output` counts only the main agent's generated tokens, and `subagent_output` counts only sub-agent generated tokens — neither includes the other, so nothing is double-counted. For a grand total of generated tokens, sum the matching pairs yourself (e.g. `output + subagent_output`, and `reasoning + subagent_reasoning` for OpenCode). The input side works the same way: `input` is fresh (uncached) input, while `cache_read`/`cache_write` are the cached-input categories that are billed at different rates, which is why they are kept distinct rather than folded into `input`.
  - The figures should still be treated as a lower bound: any consumption the backend does not record in the transcript (e.g. internal compaction, retried requests, or usage a provider omits) is not captured, so actual tokens consumed may be higher than reported.
- Proxy mode baselines the continued backend session on startup so token metadata only includes turns after aGiT starts tracking new changes.
- Proxy mode preserves the backend's selected model in commit metadata when it can be read from session data.
- User commits use the user-provided subject and include aGiT metadata.
- Commits are created only when staged changes exist.
## Advanced Usage

Show aGiT diagnostic messages:

```bash
agit --verbose
```

Use the structured JSON fallback mode:

```bash
agit --mode json
```

JSON mode invokes the backend non-interactively for each prompt (`opencode run --format json` or `claude -p --output-format json`) so aGiT can capture the final response and create traceable commits.

In JSON mode, plain text is sent to the active agent backend:

```text
> fix the parser bug
```

JSON mode aGiT commands use `:` so backend-native `/` input is not intercepted:

```text
:help                      show commands
:status                    show git status
:user-commit               create a user commit
:stage                     review and stage untracked files
:unstaged                  show intentionally unstaged files
:agent-backend <backend>   switch backend (opencode|claude)
:exit                      exit
```

In JSON mode, aGiT shows a bottom status bar with the active backend, target repo, model, and unstaged-new-file count. Typing `:` shows aGiT command completions. Typing `/` shows common backend command completions, and slash commands are forwarded to the backend rather than handled by aGiT.


## Configuration

Repository-local configuration can be stored in `.agit/config.json`:

```json
{
  "trace_turn_limit": 5
}
```

`trace_turn_limit` controls the maximum number of recent user turns included in an agent commit body. The default is `5`.

User-wide settings live in `~/.agit/config.json` (override the directory with `AGIT_CONFIG_DIR`):

```json
{
  "default_backend": "opencode",
  "menu_key": "ctrl-g",
  "timings": {
    "base_poll_seconds": 3.0
  }
}
```

`default_backend` (`opencode` or `claude`) is used for repositories that have no backend recorded yet. It is updated whenever you pass `--backend` or switch backends with `agent-backend`.

`menu_key` sets the key that opens aGiT's command menu in proxy mode. The default is `ctrl-g`; any `ctrl-<letter>` works except keys the terminal or aGiT already uses (`ctrl-c` exit flow, `ctrl-h` Backspace, `ctrl-i` Tab, `ctrl-j`/`ctrl-m` Enter). An invalid value falls back to `ctrl-g`, so a typo can never lock you out of the menu. The status line and aGiT's messages show whichever key is configured.

`timings` tunes aGiT's polling and debounce intervals (all in seconds). Specify only the keys you want to change; anything omitted — or set to a non-positive / non-numeric value — keeps its default:

| Key | Default | What it controls |
| --- | --- | --- |
| `base_poll_seconds` | `3.0` | How often the base branch is re-checked for commits made outside aGiT (so worktrees pick them up). |
| `background_poll_seconds` | `2.0` | How often an idle background session is serviced (committed / integrated). |
| `file_stable_seconds` | `8.0` | Quiet period after a file change before an auto-commit. |
| `child_idle_seconds` | `4.0` | No backend output for this long counts as idle. |
| `parse_cooldown_seconds` | `10.0` | Minimum gap between agent-turn parses. |
| `base_edit_check_seconds` | `3.0` | How often aGiT warns about edits to the base repo when the sandbox is unavailable. |
| `cwd_check_seconds` | `3.0` | How often aGiT checks for the Claude resume-cwd drift bug. |
| `base_drift_check_seconds` | `2.0` | How often aGiT checks whether the base repo was switched to another branch outside aGiT. |
