# aGiT

aGiT stands for agent + git. It is an interactive Python CLI that wraps coding-agent command line tools and Git so agentic code changes are committed with traceable metadata.

The MVP supports OpenCode as the first backend.

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

Run against another repository:

```bash
agit --repo /path/to/repo
```

Show aGiT diagnostic messages:

```bash
agit --verbose
```

Inside the interactive CLI, plain text is sent to the active agent backend:

```text
> fix the parser bug
```

aGiT commands use `:` so OpenCode-native `/` input is not intercepted:

```text
:help              show commands
:status            show git status
:user-commit       create a <user> commit
:stage             review and stage untracked files
:unstaged          show intentionally unstaged files
:model <model>     set the backend model
:agent opencode    select the OpenCode backend
:exit              exit
```

The current MVP invokes OpenCode through `opencode run --format json` for each prompt so aGiT can capture the final response and create traceable commits.

When running in a terminal, aGiT shows a bottom status bar with the active backend, target repo, model, and unstaged-new-file count. Typing `:` shows aGiT command completions. Typing `/` shows common OpenCode command completions, and slash commands are forwarded to OpenCode rather than handled by aGiT.

## Commit Behavior

- Tracked modifications and deletions are staged with `git add -u`.
- New untracked files require confirmation before staging.
- Declined untracked files are remembered in repository-local `.agit/state.json`.
- Agent commits use the `<agent>` tag and include the full interaction trace since the last code-changing commit.
- Agent commit metadata includes context token count and token usage accumulated since the last code-changing commit.
- User commits use the `<user>` tag and include aGiT metadata.
- Commits are created only when staged changes exist.
