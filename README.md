# aGiT

aGiT stands for agent + git. It is an interactive Python CLI that wraps coding-agent command line tools and Git so agentic code changes are committed with traceable metadata.

aGiT supports OpenCode and Claude (Claude Code) as interchangeable backends. Every aGiT feature works the same regardless of the selected backend.

## Install

For local development:

```bash
python3 -m pip install -e .
```

This installs the `agit` command and the terminal UI dependency used for status bars and contextual command hints.

## Usage

Run in the current repository:

```bash
agit
```

By default, `agit` runs in proxy mode: it launches the real backend TUI (OpenCode or Claude) in a pseudo-terminal, renders it through an internal terminal screen, and reserves a bottom status line for aGiT. Press `Ctrl-G` to enter aGiT command mode.

Run against another repository:

```bash
agit --repo /path/to/repo
```

Choose the backend (also saved as the global default for future runs):

```bash
agit --backend claude
agit --backend opencode
```

On the first run, aGiT asks which backend should be the default (listed alphabetically, with each backend's install status). If the chosen backend's CLI is not installed, aGiT shows install instructions and lets you install it or pick a different one. The choice is saved in `~/.agit/config.json` (`default_backend`) and reused for future runs. You can also switch backends mid-session with the `agent-backend` command below.

aGiT tracks one session per repository and stays pinned to the session it launched (so it does not drift to other sessions you open). Use the `session` command (`Ctrl-G`, then `session`) to start a new session, switch the tracked session to another existing one, or sync tracking to the most recently active session — for example after starting a new conversation inside the backend's own TUI. This works the same for both backends.

Show aGiT diagnostic messages:

```bash
agit --verbose
```

Use the structured JSON fallback mode:

```bash
agit --mode json
```

In proxy mode, press `Ctrl-G`, then type one of these aGiT commands:

```text
status                    show git status
user-commit               create a user commit
stage                     review and stage untracked files
unstaged                  show intentionally unstaged files
session                   start a new session, switch the tracked session, or sync to the latest
agent-backend             switch backend (opencode|claude); shows a picker
exit                      exit
```

In proxy mode, aGiT commands are triggered with `Ctrl-G` only (not `:`); `:` is forwarded to the backend like any other character.

The command palette previews available commands. Use Up/Down to select a command, Tab to complete it, and Enter to run it.

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

JSON mode invokes the backend non-interactively for each prompt (`opencode run --format json` or `claude -p --output-format json`) so aGiT can capture the final response and create traceable commits.

Proxy mode launches the backend's native TUI directly and recovers session metadata for automatic agent commits — via `opencode export` for OpenCode, or by reading the session transcript under `~/.claude/projects/` for Claude.

In JSON mode, aGiT shows a bottom status bar with the active backend, target repo, model, and unstaged-new-file count. Typing `:` shows aGiT command completions. Typing `/` shows common backend command completions, and slash commands are forwarded to the backend rather than handled by aGiT.

## Commit Behavior

- Tracked modifications and deletions are staged with `git add -u`.
- New untracked files require confirmation before staging.
- Declined untracked files are remembered in repository-local `.agit/state.json`.
- Agent commits use the `<agent>` tag and include the full interaction trace since the last code-changing commit.
- Agent commit metadata includes context token count and generated token usage accumulated since the last code-changing commit.
- Proxy mode baselines the continued backend session on startup so token metadata only includes turns after aGiT starts tracking new changes.
- Proxy mode preserves the backend's selected model in commit metadata when it can be read from session data.
- User commits use the user-provided subject and include aGiT metadata.
- Commits are created only when staged changes exist.

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
  "default_backend": "opencode"
}
```

`default_backend` (`opencode` or `claude`) is used for repositories that have no backend recorded yet. It is updated whenever you pass `--backend` or switch backends with `agent-backend`.
