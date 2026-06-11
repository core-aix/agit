# aGiT Requirements

aGiT stands for agent + git. It is a Python library and interactive CLI that combines coding-agent command line tools with automatic Git commits so agentic code changes are easier to trace.

## Goals

- Provide a common interactive interface for coding-agent backends and Git commit automation.
- Support OpenCode and Claude (Claude Code) as interchangeable backends; every aGiT feature works the same regardless of the selected backend.
- Keep the user experience similar to the selected backend CLI, while adding aGiT commands for agent switching, user commits, staging, status, and configuration.
- Make agentic code changes traceable by automatically committing code changes made after agent prompts.
- Support running aGiT from any folder against a target working repository.

## Commit Types

- Agent commits use the `<agent>` tag.
- User-triggered commits use the user-provided subject without an aGiT subject tag.
- Before an agent acts, if user changes already exist, aGiT creates a separate user commit first.
- A commit is created only when code has changed and staged changes exist.

## Commit Messages

- Agent commit subjects start with `<agent>` followed immediately by the latest user query for readability.
- User commit subjects use the user-provided commit message.
- User commit messages are required; blank user commit messages are rejected.
- Agent commit bodies include the full interaction trace since the last code-changing commit.
- The interaction trace includes full user prompts and final agent responses.
- Do not include thinking tokens or intermediate responses.
- Commit subjects and bodies must not contain terminal escape sequences or control characters; strip arrow-key/escape residue both where the prompt is captured and when building the message.
- Commit bodies include metadata such as backend, session name, backend session ID, aGiT session ID, model, commit type, and timestamps.
- Agent commit metadata includes the current context token count and token usage accumulated since the last code-changing commit.
- Record reasoning/thinking token counts in commit metadata only when the backend session record reports them; otherwise omit the reasoning line. Do not add explanatory token notes to the metadata.
- Proxy mode must baseline continued OpenCode sessions on startup so old turns do not inflate token usage for the next commit.

## Staging Behavior

- Use `git add -u` by default for tracked modifications and deletions.
- When new untracked files are present, ask whether they should be staged.
- If the user declines staging untracked files, remember those files in repository-local state and do not ask about them again automatically.
- Inform the user when intentionally unstaged files exist.
- Provide an interactive CLI command to review and stage intentionally unstaged files.

## Repository-Local State

- Store state in `.agit/state.json` in the target repository.
- Ignore `.agit/` by default.
- State includes the aGiT session ID, selected backend, selected model, backend session ID, per-backend session IDs, declined untracked files, and pending interaction trace.
- Optional repository-local config lives in `.agit/config.json`; `trace_turn_limit` defaults to `5` and controls the maximum recent user turns included in an agent commit body.

## MVP Interface

- `agit` starts proxy mode in the current repository, launching the native OpenCode TUI through a pseudo-terminal and rendering it through an internal terminal screen with an aGiT status line.
- `agit --repo PATH` starts the interactive CLI for another repository.
- `agit --mode json` uses the structured JSON prompt-loop fallback.
- `agit --verbose` shows aGiT diagnostic messages; normal mode should avoid debug/status chatter.
- Plain text input is sent to the active agent backend.
- In proxy mode, all printable input is forwarded to the backend; aGiT controls are opened with `Ctrl-G` only. `:` is not an aGiT command trigger in proxy mode and is forwarded to the backend like any other character.
- Proxy mode command palette previews aGiT commands; Up/Down selects, Tab completes, and Enter runs the selected command.
- In JSON mode, aGiT commands use `:` instead of `/` so OpenCode-native slash controls are not intercepted.
- The interactive UI should show status information and contextual command hints for both `:` aGiT controls and `/` OpenCode-native controls.
- Intentionally unstaged-file notices should live in the status bar, not in the main transcript.
- Proxy mode renders the backend screen itself, so it must reproduce each cell's colors and attributes (bold/italic/underline/reverse) exactly as the backend emitted them.
- Proxy mode must re-emit colors in the same encoding/depth the backend used, chosen from the shared terminal color support (truecolor stays 24-bit; 256-color stays a palette index so the host terminal's own palette renders it; named ANSI stays named). Upconverting 256-color output to truecolor breaks terminals without truecolor support (e.g. Apple Terminal) and shifts colors on terminals with customized palettes.
- Proxy mode must answer the terminal capability queries the backend makes (foreground/background via OSC 10/11, palette via OSC 4, cursor position, device attributes) using the host terminal's real responses, so the backend detects the same theme it would in a native session. Without this the backend cannot match the host terminal's light/dark theme and colors render wrong.
- Proxy mode commands after `Ctrl-G` (bare names, no `:`), in this order: `session`, `agent-backend`, `git-status`, `git-stage`, `git-unstaged`, `git-user-commit`, `exit`. Order matters because the palette selects by typed prefix.
- Only `session` starts with `s`, so pressing `s`+Enter jumps straight to the session picker. Git-specific commands are grouped under a `git-` prefix.
- `session` opens an interactive menu of the live concurrent sessions to switch between them, start a new one (in its own worktree), or stop one. Each entry shows whether the session is `running` (a turn is in flight / it produced output recently) or `idle`. Typed forms: `session new`, `session <n>` (switch to the n-th).
- `agent-backend` selects the agent backend (`opencode` or `claude`); with no argument it shows a picker. Switching relaunches the backend TUI, restores that backend's previous session for the repo if known, and updates the saved global default.
- `git-status` shows Git status; `git-stage` reviews/stages untracked files; `git-unstaged` shows intentionally unstaged files; `git-user-commit` creates a user commit.
- `exit` exits (with confirmation; finalizes pending commits first).

## OpenCode Backend

- Use the OpenCode CLI, initially through `opencode run --format json`.
- Proxy mode uses the native OpenCode TUI and recovers metadata through `opencode session list --format json` and `opencode export`.
- Parse the final response, backend session ID, and model when available.
- Preserve only the final response in commit messages.

## Claude Backend

- Use the Claude Code CLI: proxy mode launches the native `claude` TUI; JSON mode uses `claude -p <prompt> --output-format json`.
- Proxy mode starts a fresh session with an explicit `claude --session-id <uuid>` so aGiT knows which transcript to read, and continues an existing session with `claude --resume <id>`.
- Recover metadata by reading the session transcript JSONL under `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl` (override the base directory with `CLAUDE_CONFIG_DIR`); the project directory name is the absolute working directory with every non-alphanumeric character replaced by a dash.
- Parse turns into user prompts and final assistant text responses; exclude thinking blocks, tool calls, tool results, sidechain (subagent) messages, and slash-command artifacts.
- Map Claude token usage (input/output/cache-read/cache-creation) onto the shared token model; reasoning/thinking tokens are not reported separately.

## Session Tracking

- aGiT tracks exactly one backend session per repository and pins to the session it launched, rather than chasing whichever session is globally newest.
- For backends that accept an explicit session id (Claude), aGiT pins at launch. For backends that assign their own id (OpenCode), aGiT snapshots existing session ids before launch and adopts the newly created one on the first parse, then stays pinned to it.
- The `session` command (proxy) lets the user start a new session, switch the tracked session to another existing one, or sync tracking to the most recently active session (useful after starting a new session inside the backend TUI). Switching or starting a new session relaunches the backend TUI and re-baselines so existing history is not re-committed.
- Session detection, listing, and switching must work identically for both OpenCode and Claude.

## Concurrency and Locking

- Only one aGiT process may auto-commit/merge in a given working tree at a time. A process acquires a single-writer lock at `<tree>/.agit/lock` (PID-based, with stale-owner reclaim). A second aGiT process on the same repo runs **read-only**: it renders the backend TUI but makes no commits, and shows a banner that another aGiT process is managing the repo. (Implemented in `agit/git/lock.py`, wired into both proxy and JSON modes.)
- Quitting a managing (non-read-only) proxy instance asks for confirmation before exiting.
- Concurrent sessions are isolated with git worktrees so changes are never attributed to the wrong session; see Concurrent Sessions below.

## Concurrent Sessions (worktrees + auto-integration)

This is the design aGiT targets for running several sessions at once. Foundations (`agit/git/lock.py`, `agit/git/worktree.py`, and the worktree/branch/merge helpers in `agit/git/repo.py`) are implemented and unit-tested; the multiplexer wiring in `agit/proxy/runner.py` is the remaining integration.

- A single aGiT process multiplexes several live sessions; one is displayed, the others keep running and integrating in the background.
- The main working tree / base branch is mutated only by the serialized merge coordinator; every session runs in its own worktree under `.agit/worktrees/<name>`.
- A session creates a transient turn branch (`agit/<name>/t<n>`) when it receives a prompt; on the turn's final agent message the branch is integrated into the base and deleted.
- Integration is serialized and completion-ordered. It runs inside the owning session's worktree (merge base into the turn branch) so the agent can resolve conflicts in place; aGiT auto-prompts the agent with the conflicting commits' context and pauses for the user if it cannot resolve.
- A `session` view shows running/idle/merging status and can stop sessions; a `base` command switches the base branch after stopping sessions and draining pending integrations; on restart aGiT offers recovery for stale `agit/*` branches and worktrees.

## Backend Selection and Global Config

- The selected backend is stored per repository in `.agit/state.json`.
- A user-wide config at `~/.agit/config.json` (override the directory with `AGIT_CONFIG_DIR`) stores `default_backend`, used when a repository has no backend recorded yet.
- On the first run (no global default yet and no `--backend`), prompt the user to choose the default backend. List backends in alphabetical order and show whether each is installed.
- Check that the backend's CLI is installed (on `PATH`) before launching it. If it is not, show install instructions and let the user install it or choose a different (installed) backend; backend switching is likewise blocked for backends that are not installed.
- `agit --backend <claude|opencode>` selects the backend for a run and saves it as the new global default.
- Switching backends saves the current backend's session id and restores the target backend's last session id for the repository, so each backend keeps its own conversation.
