# aGiT

aGiT stands for agent + git. It is an interactive Python CLI that wraps coding-agent command line tools and Git so agentic code changes are committed with traceable metadata.

aGiT supports OpenCode and Claude (Claude Code) as interchangeable backends. Every aGiT feature works the same regardless of the selected backend.

## Dashboard

`agit --dashboard` (or `-d`) opens a **live, auto-refreshing web dashboard** of your repository — who and what wrote the code — served on `localhost` and opened in your browser. Every number is computed from commit metadata alone, so it's identical on every clone; nothing is sent anywhere.

```bash
agit --dashboard        # serve on localhost and open the browser (Ctrl-C to stop)
agit -d text            # one-shot plain-text report instead (pipe it, paste it into an issue)
```

![The aGiT dashboard](https://raw.githubusercontent.com/core-aix/agit/main/docs/images/dashboard-v2.png)

- **aGiT-tracked AI vs non-tracked lines** — what the agents wrote (tracked by aGiT) versus everything else; it never claims a human wrote what the model did.
- **Filter live** — narrow the whole dashboard to one committer (merged to their GitHub ID), a backend, a model, or a time range.
- **Tokens, efficiency, and loop detection**, plus a commit log you can click to read the full message and jump to the commit on GitHub.

See [Repository dashboard](#repository-dashboard) below for the full breakdown.

## Requirements

aGiT needs **git** and at least one backend CLI — [Claude Code](https://docs.claude.com/en/docs/claude-code) or [OpenCode](https://opencode.ai) — on your `PATH`. The dashboard's committer view additionally uses the **GitHub CLI (`gh`)** to resolve commit authors to their GitHub logins: install it from [cli.github.com](https://cli.github.com) and run `gh auth login`. `gh` is optional — without it the dashboard still works and falls back to merging committer identities by email and no-reply login.

## Install

```bash
pip install agit-ai
```

This installs the `agit` command and the terminal UI dependency used for status bars and contextual command hints. (The PyPI distribution is named `agit-ai` because the `agit` name is taken by an unrelated project; the command you run and the importable package are both still `agit`.) Once installed, aGiT keeps itself up to date — see [Self-update](#self-update).

For local development, install from a checkout instead:

```bash
python3 -m pip install -e .
```

### Contributing

Install dependencies and the optional git hooks:

```bash
uv sync --group dev
make install-hooks
```

This installs two hooks:

- **commit** — `ruff` (lint + format) and basic file hygiene, so commits stay fast.
- **push** — the full CI-equivalent gate (`ruff`, `mypy` vs the baseline, tests + coverage), so a push that would break CI fails locally first.

Run the same full gate by hand at any time:

```bash
make check        # or: ./scripts/check.sh
```

This is the definition of "done" for a change — it mirrors CI exactly (`.github/workflows/ci.yml`).

## Usage

Run in the current repository:

```bash
agit
```

By default, `agit` runs in proxy mode: it launches the real backend TUI (OpenCode or Claude) in a pseudo-terminal, renders it through an internal terminal screen, and reserves a bottom status line for aGiT showing the session (and the base branch it merges into), the backend, the summarizer state, and the repository the agent is working on (the base repository path, home-abbreviated and elided from the left when space is tight). Press `Ctrl-G` to enter aGiT command mode (configurable via `menu_key` in `~/.agit/config.json` — see Configuration).

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

Help follows the same model: `agit --help` (or `-h`) prints aGiT's own options followed by the active backend's help, so one command documents both layers. To run only the backend's help, forward it explicitly: `agit -- --help`.

On the first run, aGiT asks which backend should be the default (listed alphabetically, with each backend's install status). If the chosen backend's CLI is not installed, aGiT shows install instructions and lets you install it or pick a different one. The choice is saved in `~/.agit/config.json` (`default_backend`) and reused for future runs. You can also switch backends mid-session with the `agent-backend` command below.

In proxy mode (default), press `Ctrl-G`, then type one of these aGiT commands:

```text
session                   switch / start (own worktree) / stop a live session
agent-backend             switch backend (opencode|claude); shows a picker
summarizer                toggle summarization on/off, set model, show status
git-base-branch           switch the branch sessions integrate into
git-status                show git status
git-stage                 review and stage untracked files
git-unstaged              show intentionally unstaged files
git-user-commit           create a user commit
update                    check for / install an aGiT self-update
exit                      exit (with confirmation)
```

aGiT tracks one session per repository and stays pinned to the session it launched (so it does not drift to other sessions you open). Use the `session` command (`Ctrl-G`, then `session`) to start a new session, switch the tracked session to another existing one, or sync tracking to the most recently active session — for example after starting a new conversation inside the backend's own TUI. This works the same for all backends.

Only `session` starts with `s`, so `Ctrl-G` then `s` + Enter jumps straight to the session picker. The session menu marks each session `running` or `idle`. Git-specific commands share a `git-` prefix.

### Self-update

aGiT keeps itself current. On startup, and then about every five minutes while you work, it checks whether a newer aGiT is available:

- **Source-linked install** (the editable `pip install -e .` from a git checkout): it fetches the checkout's upstream branch and compares it with your current branch.
- **Package install** (a wheel from a package index): it compares the installed version with the latest published one.

If an update exists, aGiT prompts you at startup and shows a notice during a session (run the `update` command from the `Ctrl-G` menu to act on it). When you accept, aGiT waits until **every session has finished and all commits are integrated**, installs the update, then restarts itself automatically. It never interrupts a merge: while any session is resolving a merge/conflict, the notice is held back and an accepted update is deferred until the merge is done. Source updates are fast-forward-only and are skipped (with a message) if the checkout has uncommitted changes or has diverged from upstream, so an update never disturbs local development. Choose "Stop checking for updates" — or set `"check_for_updates": false` in `~/.agit/config.json` — to turn the checks off; tune the cadence with `timings.update_check_seconds`.


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

When a session's commits are merged into the base branch and the merge has conflicts, the agent backend resolves them, and the resolution is recorded as an `<aGiT-merge>` commit listing the base commits it was resolved against.

On startup, aGiT reconciles worktrees left behind by previous runs: it integrates any pending commits into the base branch and then deletes the worktree. Worktrees that cannot be integrated cleanly (a conflict, or uncommitted changes) are kept so no work is lost. The backend conversation itself persists (keyed by the worktree path) and stays resumable.

### Commit message format

aGiT commit messages use a consistent Markdown-style structure. The first line is the subject (prefixed with `<aGiT>` for agent commits — including the cover commits placed on top of backend-made commits — `<aGiT-merge>` for agent-resolved merges, or left plain for user commits). When summarization is enabled the summary leads the message: its first line is the subject and the rest is the first paragraph of the body. The rest of the body is organized into `#` sections — `# Prompts` (when a summary takes the subject), `# Interaction Trace`, `# aGiT Metadata` — with `## User` / `## Agent` subsections inside the interaction trace. Commits are written with `git commit -F -` (no editor), so the `#` lines are preserved rather than stripped as git comments. Secrets and terminal escape sequences are masked out of subjects and trace bodies before committing.

Because the conversation is recorded in commit messages, aGiT shows a privacy warning at startup — never enter passwords, API keys, or other sensitive information in the chat — which must be acknowledged to continue (skipped when there is no terminal to acknowledge from).

### Summarization

When summarization is enabled (the default), aGiT runs a second LLM stream alongside the coding session to preserve design context that would otherwise be lost to session compaction or terse commit subjects:

- **Commit summaries** — each agent commit gets an LLM-written summary of what changed and why. The summary leads the commit message: its first line becomes the subject and the rest follows as the first paragraph of the body (the prompts that used to head the message move to `# Prompts`); it is also stored as a git note in the `agit/commit-summary` namespace.
- **Session summaries** — a rolling narrative of the session (goals, architectural decisions, design evolution) is updated on every commit and attached as a git note in the `agit/session-summary` namespace.
- **Pre-compaction capture** — when you run `/compact` in the backend, aGiT first exports the full session transcript and folds it into the session summary, so compaction does not lose the conversation's context.

Because summaries are git notes, they travel with the repository and can be read independently of commit messages:

```bash
git notes --ref agit/commit-summary show <commit>
git notes --ref agit/session-summary show <commit>
```

Summarization never blocks the session: commits are created immediately with a prompt-based subject, the summary is computed on a background worker (the status line shows "aGiT is summarizing commit ..."), and the commit message is then amended in place. The amend only happens while it is safe — the commit is still the latest, unintegrated, and nothing new is staged; integration waits for the summary up to `summary_wait_seconds` and then proceeds, in which case the summary is recorded in git notes only. The metadata records the summarization cost next to the session's own usage (`summary_model`, `summary_tokens_input`, `summary_tokens_output`).

The status bar shows whether summarization is active (`sum:on` / `sum:off`). Use the `summarizer` command (`Ctrl-G`, then `summarizer`, or `:summarizer` in JSON mode) to toggle it (`summarizer on|off`), set the summarization model (`summarizer model`), or show the current status; changes persist to the repository-local `.agit/config.json` (see Configuration).

## Commit Behavior

- Tracked modifications and deletions are staged with `git add -u`.
- New untracked files require confirmation before staging.
- Declined untracked files are remembered in repository-local `.agit/state.json`.
- Agent commits use the `<aGiT>` tag and include the full interaction trace since the last code-changing commit.
- Agent commit metadata includes context token count and generated token usage accumulated since the last code-changing commit.
  - Token figures are read directly from the backend's session transcript (each assistant message's reported usage) and broken out by category: `input`, `output`, `cache_read`, `cache_write`, and (when the backend reports it) `reasoning`. For Claude, the recorded output count already includes extended-thinking and tool-call tokens. Sub-agent/sidechain turns are counted separately under the matching `subagent_*` categories rather than dropped. Each category is recorded only when the backend reports a non-zero value, so backends that omit a field (e.g. OpenCode does not expose sub-agent usage) simply have no line for it.
  - The categories are **non-overlapping**: `output` counts only the main agent's generated tokens, and `subagent_output` counts only sub-agent generated tokens — neither includes the other, so nothing is double-counted. For a grand total of generated tokens, sum the matching pairs yourself (e.g. `output + subagent_output`, and `reasoning + subagent_reasoning` for OpenCode). The input side counts every token exactly once: `input` is all *fresh* input processed since the last commit — the uncached remainder plus the cache-creation tokens (so a first run's input reflects the full context instead of looking near zero next to the cache) — with `cache_write` kept as the "of which was written to the cache" breakdown. `cache_read` stays separate because those tokens were already counted as input when first processed; they are replayed from the cache and billed at a different rate.
  - The figures should still be treated as a lower bound: any consumption the backend does not record in the transcript (e.g. internal compaction, retried requests, or usage a provider omits) is not captured, so actual tokens consumed may be higher than reported.
- Proxy mode baselines the continued backend session on startup so token metadata only includes turns after aGiT starts tracking new changes.
- Proxy mode preserves the backend's selected model in commit metadata when it can be read from session data.
- User commits use the user-provided subject and include aGiT metadata.
- Commits are created only when staged changes exist.
- If the backend commits on its own (e.g. the agent runs `git commit` itself, or a hook does), aGiT never rewrites those commits — their hashes stay exactly what the agent may already have reported in PR or issue comments. Instead, once the turn finishes, aGiT adds a *cover commit* on top carrying the interaction trace and metadata: a merge-shaped commit in the GitHub PR merge style, whose tree is the backend head's tree and whose parents are the turn's start and the backend's head, so `git log --first-parent` reads turn-by-turn while the backend's own commits remain reachable via the second parent. The `covered_commits` metadata line records the hashes of the backend-made commits the cover accounts for; when aGiT also has uncommitted changes to commit, its own (regular) commit carries that line instead.
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

### Scripted runs and the demo

`--prompt` runs JSON mode fully scripted: each prompt is sent to the backend in order (lines starting with `:` are aGiT commands), every turn that changes files becomes a commit, and aGiT exits when the prompts are done.

```bash
agit --repo path/to/repo --backend claude \
  --prompt "add input validation to parse()" \
  --prompt ":status" \
  --permission-mode acceptEdits
```

Scripted runs never block on a question: the privacy warning is printed without waiting for acknowledgment, and new untracked files are staged automatically (with a notice) instead of being reviewed interactively. The same non-interactive defaults apply when prompts are piped to `agit --mode json` on stdin. Note that headless Claude needs permission to edit files — forward `--permission-mode acceptEdits` (or your preferred permission flags) through aGiT as shown above; OpenCode's `run` mode edits by default.

`scripts/demo.sh` is a self-contained showcase built on this: it creates a fresh repository in a temporary directory, has the agent write a small program and its tests through aGiT, and leaves the repository behind so you can inspect the `<aGiT>` commit history or continue interactively.

```bash
scripts/demo.sh                      # drive the demo with claude
scripts/demo.sh --backend opencode   # ... or with opencode
scripts/demo.sh --model haiku --dir /tmp/agit-demo
```

### Repository dashboard

`agit --dashboard` (or `-d`) opens a live web dashboard of repository metrics computed entirely from the aGiT metadata in commit messages — no extra state, so the numbers are identical on every clone. It is served on `localhost`, opens in your browser, and **auto-refreshes** (the page polls the server, which recomputes from `git log` on each request), so you can watch metrics update as an agent commits. Press Ctrl-C to stop.

- **Coverage**: how many commits are aGiT-tracked (agent commits, backend-made commits covered by an aGiT cover commit, agent-resolved merges, user commits, and aGiT's own integration merges) versus non-tracked.
- **Code changes**: lines added/removed split two ways — **aGiT-tracked AI** (agent commits + the backend-made commits an aGiT cover commit accounts for + agent-resolved merges) and **non-tracked** (everything else: user commits, plain commits with no aGiT metadata, and squash/PR-merge commits whose message concatenates several metadata blocks and so can't be cleanly attributed). There is deliberately no "human" category — even a user-made commit can contain lines an agent produced off the record, so the only honest claim is whether aGiT tracked the lines as AI. Cover commits are merges and contribute no line counts of their own, so a turn's lines are never double-counted.
- **aGiT-ops**: the integration merge commits aGiT makes itself (e.g. bringing base into a session branch) are shown as their own class, not lumped into non-tracked. They carry no diff, so they add no lines.
- **Squashed commits**: when several aGiT commits are squashed into one (a squash- or PR-merge concatenates their metadata blocks — git flattens this even across multiple rounds of squashing), the dashboard parses every original back out, so their tokens and per-model/backend usage are still counted instead of lost in the aggregate. In the web commit log such a commit is tagged `⧉ N squashed` and expands on click into its original commits, each itself expandable to its full message.
- **Tokens**: totals per category (input, output, reasoning, cache read/write, sub-agents, summarizer) and an efficiency ratio — AI-changed lines per 1k output tokens.
- **Breakdowns** by backend, by model (a cover commit's bucket includes the lines of the backend-made commits it covers), and by committer. Committer identities are merged to **GitHub IDs** via the `gh` CLI when available (every commit GitHub knows is keyed by its real login); without `gh` it falls back to merging by email and no-reply login. Each committer's lines are split into the aGiT-tracked AI they drove versus non-tracked.
- **Possible loops**: runs of three or more consecutive turns with near-identical prompts (or the same prompt repeated within one turn's trace), with the output tokens they consumed — a sign the conversation is going in circles.

The web page (styled like the [project page](https://github.com/core-aix/agit/tree/main/docs)) lets you **filter live** — narrow the whole dashboard to one committer or view the entire team, slice by backend or model, or restrict to a **time range** (presets or a custom from/to). The server recomputes the metrics for each filter, and the **commit log is paginated** (fetched a page at a time), so the browser never holds the whole history — memory stays bounded no matter how big the repo is. Each log line shows per-line token metrics; clicking a line opens the full commit message **rendered as Markdown** with a link to the commit on GitHub, and a squash expands into its original commits (each itself expandable). Agent commits also record when the AI-driven conversation started and ended (`agent_started_at` / `agent_ended_at` in the metadata block).

`agit --dashboard text` (or `-d text`) prints the same metrics as a one-shot plain-text report instead of serving — handy for piping or pasting into an issue.

The dashboard is read-only in either form: it never commits, never prompts, and skips the privacy acknowledgment.


## Configuration

Repository-local configuration can be stored in `.agit/config.json`:

```json
{
  "trace_turn_limit": 5,
  "summarization_enabled": true,
  "summarization_model": null
}
```

`trace_turn_limit` controls the maximum number of recent user turns included in an agent commit body. The default is `5`.

`summarization_enabled` (default `true`) toggles the LLM summarization stream (see Summarization above). `summarization_model` sets the model the summarizer asks the backend to use; leave it unset (`null`) to use the backend's default model. Both keys can also be set user-wide in `~/.agit/config.json`; the repository-local value wins, and the `summarizer` command writes its changes here.

User-wide settings live in `~/.agit/config.json` (override the directory with `AGIT_CONFIG_DIR`):

```json
{
  "default_backend": "opencode",
  "menu_key": "ctrl-g",
  "sandbox": true,
  "use_worktrees": true,
  "timings": {
    "base_poll_seconds": 3.0
  }
}
```

`default_backend` (`opencode` or `claude`) is used for repositories that have no backend recorded yet. It is updated whenever you pass `--backend` or switch backends with `agent-backend`.

`sandbox` (default `true`) confines the agent's writes to its own session worktree (via `sandbox-exec` on macOS), keeping the base repository and sibling worktrees read-only to the agent. Set it to `false` to disable confinement; when sandboxing is unavailable, aGiT instead warns when the base repository is edited while a session runs.

`use_worktrees` (default `true`) controls whether sessions run in isolated worktrees. Set it to `false` to run the agent directly on the current branch by default — the same behavior as `--no-worktree` (which always wins over the config). See the `--no-worktree` notes under Usage for the trade-offs.

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
| `summary_wait_seconds` | `45.0` | How long integration waits for a background commit summary before proceeding without it. |
