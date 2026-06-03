# aGiT Requirements

aGiT stands for agent + git. It is a Python library and interactive CLI that combines coding-agent command line tools with automatic Git commits so agentic code changes are easier to trace.

## Goals

- Provide a common interactive interface for coding-agent backends and Git commit automation.
- Start with OpenCode as the first backend.
- Keep the user experience similar to the selected backend CLI, while adding aGiT commands for agent switching, user commits, staging, status, and configuration.
- Make agentic code changes traceable by automatically committing code changes made after agent prompts.
- Support running aGiT from any folder against a target working repository.

## Commit Types

- Agent commits use the `<agent>` tag.
- User-triggered commits use the `<user>` tag.
- Before an agent acts, if user changes already exist, aGiT creates a separate `<user>` commit first.
- A commit is created only when code has changed and staged changes exist.

## Commit Messages

- Agent commit subjects start with `<agent>` followed immediately by the latest user query for readability.
- User commit subjects start with `<user>` followed by the user-provided commit message.
- If the user leaves the user commit message blank, use `No user message provided`.
- Agent commit bodies include the full interaction trace since the last code-changing commit.
- The interaction trace includes full user prompts and final agent responses.
- Do not include thinking tokens or intermediate responses.
- Commit bodies include metadata such as backend, backend session ID, aGiT session ID, model, commit type, and timestamps.
- Agent commit metadata includes the current context token count and token usage accumulated since the last code-changing commit.

## Staging Behavior

- Use `git add -u` by default for tracked modifications and deletions.
- When new untracked files are present, ask whether they should be staged.
- If the user declines staging untracked files, remember those files in repository-local state and do not ask about them again automatically.
- Inform the user when intentionally unstaged files exist.
- Provide an interactive CLI command to review and stage intentionally unstaged files.

## Repository-Local State

- Store state in `.agit/state.json` in the target repository.
- Ignore `.agit/` by default.
- State includes the aGiT session ID, selected backend, selected model, backend session ID, declined untracked files, and pending interaction trace.

## MVP Interface

- `agit` starts the interactive CLI in the current repository.
- `agit --repo PATH` starts the interactive CLI for another repository.
- `agit --verbose` shows aGiT diagnostic messages; normal mode should avoid debug/status chatter.
- Plain text input is sent to the active agent backend.
- aGiT commands use `:` instead of `/` so OpenCode-native slash controls are not intercepted.
- The interactive UI should show status information and contextual command hints for both `:` aGiT controls and `/` OpenCode-native controls.
- Intentionally unstaged-file notices should live in the status bar, not in the main transcript.
- `:user-commit` creates a user commit.
- `:stage` reviews and optionally stages untracked files, including previously declined files.
- `:unstaged` shows intentionally unstaged files.
- `:status` shows Git status.
- `:model <model>` sets the backend model.
- `:agent opencode` selects OpenCode.
- `:exit` exits.

## OpenCode Backend

- Use the OpenCode CLI, initially through `opencode run --format json`.
- Parse the final response, backend session ID, and model when available.
- Preserve only the final response in commit messages.
