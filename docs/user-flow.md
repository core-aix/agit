<!--
  KEEP THIS DIAGRAM IN SYNC WITH THE ACTUAL USER FLOW.
  Whenever the user flow changes — a new prompt, a changed option label, a new
  decision based on file/commit status, a new Ctrl-G command, a changed exit or
  update path — update the matching graph below in the same change. AGENTS.md
  records this as a standing requirement.
-->

# aGiTrack User Flow

A complete map of what aGiTrack shows the user, **when**, and what each choice does —
including which file/commit status triggers which prompt and where every nested option
leads. Read it top to bottom: start at [Lifecycle](#1-lifecycle-overview), then drill
into whichever phase you care about.

> This document is the source of truth for the interactive flow. If you change the flow,
> change the diagram (see the note at the top of the file).

**Legend**

```mermaid
flowchart LR
  A(["Start / end"]) --- B["Action aGiTrack takes"]
  B --- C{"Decision on state"}
  C --- D[/"User is prompted"/]
  D --- E[["Background / automatic"]]
```

- `([ ])` start/end · `[ ]` an action · `{ }` a decision on repo/commit/session state ·
  `[/ /]` a prompt shown to the user · `[[ ]]` background/automatic work, no prompt.
- Edge labels are the user's answer, or the condition that selects that branch.

---

## 1. Lifecycle overview

```mermaid
flowchart TD
  start(["Run agitrack"]) --> launch["Startup and launch gating"]
  launch --> loop{"What does the user do?"}

  loop -->|Types text| pre["Before forwarding the prompt: commit/stage base and worktree"]
  pre --> turn["Agent runs the turn"]
  turn --> commit[["Auto-commit the turn onto the turn branch"]]
  commit --> integrate[["Integrate turn branch into base"]]
  integrate --> copyback["Offer to copy worktree-only files into the base repo"]
  copyback --> loop

  loop -->|Ctrl-G| menu["Ctrl-G command menu"]
  menu --> loop

  loop -->|Esc during a turn| cancel["Handle the cancelled turn"]
  cancel --> loop

  loop -->|Update available| upd["aGiTrack self-update notice"]
  upd --> loop

  loop -->|Terminal or window closed| sig["Signal exit: finalize pending work"]
  loop -->|"Ctrl-G then 'exit aGiTrack'"| ex["Exit confirmation"]
  sig --> done(["aGiTrack exits"])
  ex --> done

```

**Jump to:** [Startup and launch gating](#2-startup-and-launch-gating) ·
[Worktrees vs no-worktree](#3-worktrees-vs-no-worktree) ·
[Manual-commit mode](#3a-manual-commit-mode---manual-commits---m) ·
[Before forwarding a prompt](#4-before-forwarding-a-prompt-base-to-worktree) ·
[The agent turn](#5-the-agent-turn-auto-commit-and-integration) ·
[Copy worktree-only files](#6-after-the-turn-copy-worktree-only-files-to-base) ·
[Ctrl-G command menu](#7-ctrl-g-command-menu) ·
[Self-update flow](#9-self-update-flow) ·
[Exit and terminal close](#10-exit-and-terminal-close)

> Under **`--manual-commits` / `-m`** the auto-commit → integrate steps above are replaced: each turn
> is recorded as a hidden latent commit and the branch is only ever written by **your** commit, which
> folds the pending turns in. See [Manual-commit mode](#3a-manual-commit-mode---manual-commits---m).

---

## 2. Startup and launch gating

Everything that must resolve before the backend TUI appears.

```mermaid
flowchart TD
  start(["Run agitrack"]) --> mode{"Did the command line name a mode?"}
  mode -->|"No, and a terminal is attached"| menu[/"Mode menu: arrow keys, Enter.<br/>background+auto is the default;<br/>each row shows the command it stands for"/]
  menu -->|"q / Ctrl-C"| quit
  menu -->|"Chooses a mode"| priv
  mode -->|"stop"| stopcmd[["agitrack stop: stop the background tracker,<br/>any interactive session, and this repo's dashboard,<br/>and record that tracking is off from now"]]
  stopcmd --> quit
  mode -->|"status"| statuscmd[["agitrack status: report what is running here<br/>and in which mode (same as -s)"]]
  statuscmd --> quit
  mode -->|"any other bare word"| unknown[["Unknown command: name it, list the commands<br/>that exist, exit 2. Nothing is started"]]
  unknown --> quit
  mode -->|"-i / -b / -d / --backtrace / a prompt after --,<br/>or no terminal to ask on"| priv

  priv{"Privacy notice acknowledged before?"}
  priv -->|No| privask[/"Show privacy notice, ask to acknowledge"/]
  privask -->|Declines| quit(["Exit"])
  privask -->|Acknowledges| lock
  priv -->|Yes, or --skip-privacy-ack| lock

  lock{"Acquire single-writer repo lock .agitrack/lock?"}
  lock -->|Held by another aGiTrack process| busy[["Refuse to start: name the holding process (PID) and tell the user to stop it first"]]
  busy --> quit
  lock -->|Acquired, or stale owner reclaimed| backend

  backend{"Selected backend CLI installed on PATH?"}
  backend -->|No| binstall[/"Show install instructions: install now, or switch to an installed backend"/]
  binstall -->|Installs and retries| backend
  binstall -->|Picks another backend| backend
  binstall -->|Neither available| quit
  backend -->|Yes| firstrun

  firstrun{"First ever run? No global default backend and no --backend"}
  firstrun -->|Yes| pick[/"Choose default backend, alphabetical, shows installed state"/]
  pick --> updchk
  firstrun -->|No| updchk

  updchk[["Startup update check, TTY-gated, short network timeout"]] --> updq{"aGiTrack update available and checks enabled?"}
  updq -->|Yes| updoffer[/"Offer to update now, see Self-update"/]
  updq -->|No, non-TTY, or disabled| gap
  updoffer --> gap

  gap{"Was tracking stopped with agitrack stop and not restarted since?"}
  gap -->|Yes| gapclose[["Stamp the moment tracking resumed. Turns prompted before it<br/>are never written into a commit message (see Stopping and restarting)"]]
  gapclose --> sess
  gap -->|No| sess

  sess["Pick the session to show: resume the repo's pinned session, or start fresh"] --> dash

  dash{"open_dashboard_on_start, and a terminal attached?"}
  dash -->|Yes| opendash[["Remember this repo, start the dashboard hub if needed,<br/>open this repo's view in the browser (see Dashboard)"]]
  opendash --> spawn
  dash -->|"No, or scripted"| spawn

  spawn["Spawn backend TUI in its worktree under the sandbox"] --> ready(["Ready for input"])

```

**Jump to:** [Self-update flow](#9-self-update-flow) ·
[Worktrees vs no-worktree](#3-worktrees-vs-no-worktree)

---

## 3. Worktrees vs no-worktree

Where a session physically runs (`--no-worktree` turns worktrees off) — this decides whether the base-to-worktree and
worktree-to-base flows below apply at all. `--manual-commits` / `-m` is a **commit-timing variant of no-worktree**: it
**always runs without a worktree** (it implies `--no-worktree`) and additionally defers every commit to the user (see [Manual-commit mode](#3a-manual-commit-mode---manual-commits---m)).

```mermaid
flowchart TD
  s(["Start or switch a session"]) --> mode{"use_worktrees? Default true; off via --no-worktree/-m or config"}
  mode -->|Worktrees on| wt["Open/create a worktree at .agitrack/worktrees/&lt;name&gt;; agent edits there, isolated"]
  wt --> wtnote[["Base repo is the user's tree; worktree is the agent's sandbox. Both base-to-worktree and worktree-to-base flows apply"]]
  mode -->|No-worktree| base["Session runs on the BASE tree, worktree=None, commits to the checked-out branch"]
  base --> mc{"Manual commits? -m / manual_commits"}
  mc -->|No: auto-commit each turn| warn[/"One-time caveat: parallel no-worktree sessions share the dir and may conflict"/]
  warn --> basenote[["No isolation: the per-turn commit captures whatever is in the tree. Copy-back and base-sync flows are no-ops"]]
  mc -->|Yes: user-triggered commits| mannote[["Turns are recorded as HIDDEN latent commits; the branch is untouched until YOU commit. See Manual-commit mode"]]

  base --> resume{"Resuming a session first run in a worktree?"}
  resume -->|Yes| retarget[["Retarget the transcript's recorded cwd to the base dir so it doesn't reopen the gone worktree path"]]
```

**Jump to:** [Manual-commit mode](#3a-manual-commit-mode---manual-commits---m)

---

## 3a. Manual-commit mode (`--manual-commits` / `-m`)

Makes agent-assisted coding feel like normal git: **you** decide when to commit. It **always runs
without a worktree** (it implies `--no-worktree`, so the agent edits the current branch directly),
but aGiTrack does **not** create a commit each turn. Instead every turn is recorded as a hidden **latent** commit on a side ref
(`refs/agitrack/manual/<session>`) that your branch never shows, while `HEAD` stays put — so your
history stays clean while you work, and no interaction is lost.

When **you** commit — through `Ctrl-G → git-commit` **or** an ordinary `git commit` on the command
line / in your editor — aGiTrack folds every pending latent turn's interaction trace and metadata
into that **one** commit, alongside your own edits. A managed `prepare-commit-msg` hook does the
folding and a `post-commit` hook resets the latent ref; both are installed only for the manual-mode
session and removed on exit. So you always get a single, self-contained commit carrying your changes
**and** the full agent tracking — whether or not the commit went through aGiTrack's menu, with no
separate cover commit and no `<aGiTrack>` commits polluting the branch. Enable it for every run with
`"manual_commits": true` in config.

```mermaid
flowchart TD
  s(["Manual-commit mode: -m / manual_commits (implies --no-worktree)"]) --> turn["Agent runs a turn, editing the current branch directly"]
  turn --> changed{"Working tree changed since the last latent turn?"}
  changed -->|No| loopb(["Back to the input loop — nothing recorded"])
  changed -->|Yes| latent[["Record a HIDDEN latent commit on refs/agitrack/manual/&lt;session&gt;: snapshot the working tree, same &lt;aGiTrack&gt; trace + metadata as a normal turn commit. HEAD does NOT move; your git index is untouched"]]
  latent --> dash[["Dashboard shows it live as a 'pending' turn until you commit"]]
  dash --> loopb

  loopb --> uc{"You commit? Ctrl-G → git-commit, OR an external git commit"}
  uc -->|Hook installed| fold[["prepare-commit-msg folds the pending turns' trace + metadata into YOUR message → ONE commit with your changes AND all agent tracking; post-commit resets the latent ref to the new commit"]]
  uc -->|"core.hooksPath set (aGiTrack can't install the hook)"| cover[["Fallback: a HEAD poll detects the commit → aGiTrack adds a cover commit carrying the pending tracking (its tree = the new commit's, so it adds no diff) → reset the ref"]]
  fold --> clean(["Branch now has ONE clean commit; 0 pending turns"])
  cover --> clean
```

> The single folded commit carries a `commit_type: user` block plus one block per agent turn, so the
> dashboard parses it as a squash — summing the turns' tokens and classifying it as agent-tracked
> work. A commit made with **no** pending turns is still a plain, session-attributed user commit.
> Recovery: if a prior run left the latent ref already contained in `HEAD` (an interrupted commit),
> the next start resets it so those turns are never re-folded.

**Jump to:** [`git-commit`](#8-git-commit) · [The agent turn](#5-the-agent-turn-auto-commit-and-integration)

---

## 3b. Background mode (`--background` / `-b`)

Runs aGiTrack **without the interactive TUI** so you can drive the coding agent from **any** UI (the
**Claude desktop app**, an IDE extension, its native CLI, a chat window) while aGiTrack tracks that
session in the background — the interactive-UI-agnostic tracker of
[issue #143](https://github.com/core-aix/agitrack/issues/143). Especially handy if you'd rather stay
in a GUI than a terminal. aGiTrack does **not** launch or relay the agent; it watches the agent's
on-disk session transcript for the repo (**only** this repo's — Claude by its cwd-encoded transcript
dir, Codex and OpenCode by each session's recorded directory), records each completed turn, summarizes it, and
installs the fold hooks. It **always runs without a worktree** (implies `--no-worktree`), with either
commit style (auto — default — or `--manual-commits`).

**It runs as a DETACHED daemon, like `agitrack -d`.** `agitrack -b` starts the tracker in the
background and **returns to your shell immediately**; unlike the dashboard daemon it has **no
owner-terminal watchdog**, so it keeps tracking after the terminal closes — stop it with
`agitrack -b stop`. `agitrack -b status` reports whether one is running; **`agitrack status`** (also `--status` / `-s`)
reports the mode of whatever is tracking the repo (interactive vs background, auto/manual,
worktree/no-worktree, or not running) and whether **auto-start on commit** is enabled. The daemon
logs activity to `<repo>/.agitrack/background.log` (every line stamped with the local time, since that
log is only ever read after the fact), appends notable events to a user `--log-file`,
and reminds you (never auto-installs) when an aGiTrack update is available. Auto commits are **clean
agent commits** (subject = the LLM summary, one metadata block); the daemon waits briefly for the
summary before folding since it never amends HEAD. Only one aGiTrack runs per repo (the shared repo
lock refuses a second start).

**Never forget to start it — auto-start on commit.** aGiTrack installs a PERSISTENT `pre-commit`
hook (surviving the daemon exiting). When you `git commit` while no tracker is running and the AI
actually changed code, the hook folds that AI work's trace into your (own, manual) commit AND
**auto-starts** the daemon for the turns that follow — in the **same commit mode as your last run** —
printing an explicit "started automatically … stop with `agitrack -b stop`; disable with
`agitrack --remove-hooks`" message. A purely human commit is left untouched. The first `agitrack -b`
per repo asks whether to enable this (default on; repo-scoped `autotrack_hook`); `agitrack --remove-hooks`
turns it off. The hook calls the CURRENT aGiTrack even after a self-update (frozen-aware invocation +
PATH fallback).

**Keeping the hook's schema current.** Every background start (when `autotrack_hook` isn't `off`)
re-installs this hook and **stamps the running aGiTrack version** into it (a
`# AGITRACK-AUTOTRACK-VERSION <version>` line). Before writing, it compares the version stamped in the
already-installed hook against the running one: if the installed hook is **older** (or predates version
stamping and carries no stamp), aGiTrack **removes the previously installed hook** — restoring any
project hook it had chained — and then **installs the current version fresh**, so a changed hook schema
is never left half-migrated. A same-or-newer stamp just refreshes the baked invocation in place. This
runs on the background startup path (and equally when a no-worktree interactive session installs the same
persistent hook).

```mermaid
flowchart TD
  s(["Background mode: -b / background (implies --no-worktree); manual (default) or --auto-commit"]) --> hookq{"Auto-start enabled? autotrack_hook != off (and no custom core.hooksPath)"}
  hookq -->|No| watch
  hookq -->|Yes| stamp{"Installed pre-commit hook's stamped aGiTrack version vs the running one"}
  stamp -->|Older / unstamped schema| replace[["Remove the previously installed aGiTrack hook (restore any chained project hook), then install the current version fresh"]]
  stamp -->|Same or newer| refresh[["Refresh the baked invocation in place; keep the stamped version"]]
  replace --> watch
  refresh --> watch
  watch["Poll the newest backend session transcript in the repo dir (follows in-backend session switches)"]
  watch --> turn{"A newly completed turn?"}
  turn -->|No| watch
  turn -->|Yes| latent[["Record a HIDDEN latent commit on refs/agitrack/manual/&lt;session&gt; + summarize it (git note). HEAD does NOT move"]]
  latent --> mode{"Commit style?"}
  mode -->|Manual| user(["Wait for YOUR commit → prepare-commit-msg folds the pending turns in (cover fallback)"])
  mode -->|Auto| auto{"Working tree still has the agent's uncommitted work?"}
  auto -->|"Yes (agent didn't commit)"| aGit[["aGiTrack commits it itself, folding the pending turns' trace + metadata → ONE tracked commit; reset the ref"]]
  auto -->|"No (agent committed its own work)"| hook[["The prepare-commit-msg hook already folded the tracking into the agent's commit; cover is only the fallback"]]
  user --> watch
  aGit --> watch
  hook --> watch
```

The per-conversation commit watermark makes this exact even when you switch conversations inside the
backend (a `/resume` or a new conversation, tested on both backends): aGiTrack follows the newest
session and counts each conversation's turns exactly once, and sub-agent tokens are folded into the
launching turn. `agitrack -b stop` records any final turn (folding it in auto mode) and removes its
per-run fold hooks (the persistent auto-track `pre-commit` hook stays, so a later commit still tracks).

**Jump to:** [Manual-commit mode](#3a-manual-commit-mode---manual-commits---m) · [The agent turn](#5-the-agent-turn-auto-commit-and-integration)

---

## 4. Before forwarding a prompt (base to worktree)

Runs every time the user submits text, **before** the backend sees it
(`_pre_agent_commit_if_needed`). The point: capture the user's own edits as a user
commit and make sure the agent starts from them. Driven by where uncommitted work lives.

```mermaid
flowchart TD
  submit(["User submits a prompt"]) --> wtq{"This session's WORKTREE dirty?"}
  wtq -->|Yes| wtc[/"Prompt for a user commit message, blank is rejected"/]
  wtc --> wtcommit[["Commit the worktree onto its turn branch: git add -u for tracked; ask about untracked"]]
  wtq -->|No| baseq
  wtcommit --> baseq

  baseq{"BASE repo has user edits? Any tracked change OR any new untracked file, excluding .agitrack/"}
  baseq -->|Tracked changes| basecommit[/"Prompt for a user commit message, commit onto the base branch"/]
  baseq -->|New untracked file| stage[/"Stage all N new file(s)? y/N, lists the files"/]
  baseq -->|Nothing pending| fwd

  stage -->|y| basecommit
  stage -->|N, leave unstaged| decl[["Remember this base tree fingerprint so the same state isn't re-prompted; re-offered once the tree changes"]]
  decl --> fwd

  basecommit --> sync[["_sync_idle_worktrees_to_base: merge / re-point the new base commit into the worktree(s)"]]
  sync --> fwd["Forward the prompt to the backend"]
  fwd --> go(["Agent turn begins"])
```

**Jump to:** [The agent turn](#5-the-agent-turn-auto-commit-and-integration)

> The explicit base commit paths (this pre-prompt offer and the `git-commit`
> command) re-offer **every** untracked file (`include_declined=True`), so a previously
> declined file can always be staged here. The automatic worktree capture keeps the
> agent's own untracked decline sticky.

---

## 5. The agent turn: auto-commit and integration

```mermaid
flowchart TD
  go(["Prompt forwarded"]) --> run[["Agent works in the worktree, writes confined by the sandbox"]]
  run --> endq{"How did the turn end?"}

  endq -->|Final agent message| parse[["Parse the turn: prompts, final reply, exact token usage"]]
  parse --> mmode{"Manual-commit mode? (-m)"}
  mmode -->|Yes| mlatent[["Record the turn as a HIDDEN latent commit on refs/agitrack/manual/&lt;session&gt; (HEAD frozen; no integration). See Manual-commit mode"]]
  mlatent --> back
  mmode -->|No| changed{"Code changed AND staged changes exist?"}
  changed -->|Yes| acommit[["Create the &lt;aGiTrack&gt; turn commit: subject = tag + latest query; body = interaction trace + metadata"]]
  changed -->|No| copy
  acommit --> delay{"--delay-merge set?"}
  delay -->|No| integ[["Integrate the turn branch into base, serialized and completion-ordered; agent resolves conflicts in its worktree"]]
  delay -->|Yes| hold[/"Notice: changes held for review, names the worktree path. Merge only on explicit confirm via the session menu"/]
  integ --> conflict{"Merge conflict aGiTrack can't auto-resolve?"}
  conflict -->|Yes| cprompt[/"Auto-prompt the agent with the conflicting commits, pause for the user"/]
  conflict -->|No| copy
  hold --> copy
  cprompt --> copy

  endq -->|User pressed Esc with uncommitted edits| cancel[/"Keep them, commit with your next turn / Commit the changes now / Discard the changes"/]
  cancel -->|Keep| keep[["Remember 'keep' for this turn id so it isn't re-asked"]]
  cancel -->|Commit now| acommit
  cancel -->|Discard| dconf[/"Discard ALL uncommitted changes from the interrupted turn? Cannot be undone"/]
  dconf -->|Confirms| discard[["Discard all changes; advance the parse watermark"]]
  dconf -->|Cancels| keep
  keep --> copy
  discard --> copy

  copy["Offer to copy worktree-only files to base"] --> back(["Back to the input loop"])
```

> **The whole status → commit → integrate pipeline above runs on a dedicated git worker
> thread, never the main one** — so a `git status`/commit/merge can never block your typing,
> even right after an edit. Any dialog it needs (the conflict prompt, the copy/keep/discard
> offers) is handed to the main thread to present and the answer passed back; the worker only
> ever touches the **foreground** session. Background sessions are committed/integrated on the
> main thread (rare, throttled, and never the session you're typing in).

**Jump to:** [Copy worktree-only files to base](#6-after-the-turn-copy-worktree-only-files-to-base)

---

## 6. After the turn: copy worktree-only files to base

Only for a worktree session. Catches files the agent left UNCOMMITTED or that are
git-ignored — they integrate into nothing, so the user working in the base dir would
never see them (`_offer_copy_unstaged_to_base`). It runs for the **active** session only
(a background session is never interrupted mid-run); its files are caught instead when you
**switch to it** or on **aGiTrack exit**, just before the worktree is deleted.

First, though, aGiTrack offers to **commit** any of the user's own uncommitted edits in
the worktree (`_offer_user_commit_for_worktree_edits`) — those belong in git, not just
copied. So when both a user edit and copy-able leftovers exist, **both prompts appear**: a
commit prompt for the edits, then the copy prompt for the leftovers.

```mermaid
flowchart TD
  start(["Trigger: active session idle after a turn (committed or not), OR switched to this session, OR aGiTrack exiting"]) --> wtq{"Worktree session? (no-op under --no-worktree)"}
  wtq -->|No| done(["Nothing to do"])
  wtq -->|Yes| useredit{"User's OWN uncommitted edits in the worktree? (tracked changes / new non-declined files)"}
  useredit -->|Yes| ucommit[/"Uncommitted changes in this worktree — commit them? (the normal user-commit prompt; see git-commit). Then continue to the copy offer"/]
  useredit -->|No| gather
  ucommit --> gather[["List worktree files that won't merge: intentionally unstaged or git-ignored (new agent files are auto-staged + committed). Skip .agitrack/ and names starting with _ or ."]]
  gather --> any{"Any candidate files left to copy?"}
  any -->|No| done
  any -->|Yes| muted{"This whole SET already declined, AND no genuinely new file? (applies in EVERY context, incl. exit)"}
  muted -->|Yes| done
  muted -->|No: first ask, or a NEW path re-opens the whole set| ctx{"Context?"}

  ctx -->|Exiting| offerx[/"N file(s) will be DELETED when this worktree is removed on exit. Copy them into the base repo first? Esc cancels the exit so you can handle them yourself. (Files listed vertically under 'File(s):', PgUp/PgDn scrolls) • No, discard them with the worktree • Yes, copy to the base repo"/]
  ctx -->|Turn or switch| offer[/"N file(s) won't be merged. Copy them into the base repo? (listed vertically under 'File(s):', scrollable) Note: declining won't re-ask until the fileset changes or you switch sessions. • No, leave them in the worktree • Yes, copy to the base repo"/]

  offer -->|No| mute[["Mute this whole set of paths (re-opened only by a new file / switch / restart); notice names the worktree path"]]
  offerx -->|No, discard| disc[["Files are discarded with the worktree"]]
  offerx -->|Esc| abortx(["Abort the exit: keep the worktree + files; aGiTrack stays running so you can handle them"])
  mute --> done
  disc --> done

  offer -->|Yes| conflictq{"Any would overwrite an existing base file?"}
  offerx -->|Yes| conflictq
  conflictq -->|No conflicts| copyall[["Copy every file into the base dir"]]
  conflictq -->|Yes| ow[/"N already exist in the base repo. Overwrite them? • No, keep the base versions • Yes, overwrite all • Let me confirm each one"/]

  ow -->|No, keep the base versions| skipc[["Skip the conflicting files; still copy the non-conflicting new ones"]]
  ow -->|Yes, overwrite all| copyall
  ow -->|Let me confirm each one| each[/"Per conflicting file: overwrite it? • No, keep the base version • Yes, overwrite"/]
  each --> tally
  skipc --> tally
  copyall --> tally[["Report copied count; anything not copied gets the 'files remain' notice"]]
  tally --> done
```

**Jump to:** [`git-commit`](#8-git-commit)

> A file already accepted or left in place isn't re-offered until its content changes
> (fingerprint). Declining mutes the whole current **set of paths** — aGiTrack won't ask
> again while only those files keep changing; a genuinely new path re-opens the whole set
> (ask about all again). The mute clears on session switch and aGiTrack restart. The
> **exit** offer ignores the mute (the files are about to be deleted) and warns as much.

---

## 7. Ctrl-G command menu

`Ctrl-G` opens the command palette (type a prefix, Up/Down to select, Tab to complete,
Enter to run). Commands, in palette order:

> **One rule across every menu: Esc goes up exactly one level.** The **command palette is
> the parent of every command menu**, so Esc on a command menu (the sessions list, the
> settings list, the summarizer menu…) returns you **to the palette** — not to the agent.
> Esc on the palette returns to the agent. Esc in a sub-menu returns to the menu that opened
> it (e.g. Esc in *Manage <one shared session>* → the shared-sessions list → the sessions
> menu → the palette → the agent — one step per Esc). The only thing that unwinds further is
> a choice that moves you into a **different session** (switch / new / resume): that drops
> straight to the agent, since there is no level to come back to.
>
> Navigation is **silent and instant**: backing out shows no "closed/cancelled" message and
> never flashes the bare backend screen between levels. Internally each menu is one loop
> returning just `UP` (Esc/back → caller re-shows itself) or `DONE` (a session transition →
> unwind to the agent); a child menu the user backs out of simply re-shows its parent. So Esc
> unwinds the call stack one frame at a time and the on-screen hierarchy mirrors the code's.

```mermaid
flowchart TD
  g(["Ctrl-G"]) --> pal[/"Command palette"/]
  pal --> sessions["sessions"]
  pal --> backend["agent-backend"]
  pal --> summ["summarizer"]
  pal --> settings2["settings"]
  pal --> gunstaged["git-unstaged"]
  pal --> gcommit["git-commit"]
  pal --> dash["dashboard"]
  pal --> update["update"]
  pal --> exit["exit aGiTrack"]

  settings2 --> setmenu["Settings menu"]

  sessions --> smenu[/"Sessions menu (live sessions show running/idle; dormant worktrees and shared markers listed too)"/]
  smenu -->|Switch to a live session| sswitch[["Show it, relaunch TUI, re-baseline so history isn't re-committed; then offer to copy its worktree-only files"]]
  smenu -->|Resume an idle/dormant worktree| sresume[["Reopen that session in its worktree; continue the backend conversation if still recorded"]]
  smenu -->|Resolve an unmerged dormant worktree| sresolve[/"Integrate its pending commits, or discard the worktree (confirmed)"/]
  smenu -->|+ New session| snew["Start a new session: own worktree, or shared base dir under --no-worktree"]
  smenu -->|✎ Rename a session| srename[["Rename = fork: clears the shared lineage, so a later share creates a NEW shared entry"]]
  smenu -->|⤳ Change a session's merge branch| smb[/"Pick the branch this idle session integrates into (flushes its pending work into the old branch first)"/]
  smenu -->|↻ Resume a past conversation| spast[/"Pick from past conversations of this repo, newest first"/]
  smenu -->|⇪ Share this session with collaborators| sshare["Session sharing"]
  smenu -->|⇩ Resume a shared session| srshare["Session sharing"]
  smenu -->|⚙ Manage shared sessions| smanage["Session sharing"]
  smenu -->|✓ Integrate this session's commits / Merge reviewed changes| smerge[/"Integrate this session's committed work into its base branch (pick the branch under --delay-merge)"/]
  smenu -->|- Stop a session| sstop[["Stop the session"]]
  snew --> mode3["See Worktrees vs no-worktree"]
  sswitch --> scopy["See After the turn: copy worktree-only files"]

  backend -->|No arg, picker| bpick[/"Pick claude, codex or opencode"/]
  bpick --> bswitch[["Save current backend's session, relaunch target backend, restore its last session, update global default"]]

  summ --> smm[/"Summarizer menu: Toggle (ON/OFF) / Set model"/]
  smm -->|Toggle| stog[["Flip on/off; menu re-shows"]]
  smm -->|Set model| spick[/"Pick the summarizer model (current shown); Esc → back to the Summarizer menu"/]
  spick --> smm

  gunstaged --> gu[["Show intentionally-unstaged files in the status bar"]]

  gcommit --> gcflow["git-commit flow"]

  dash --> dserve[["Serve the read-only metrics dashboard; open the local browser only if it lands on this machine, else print the URL"]]

  update --> uflow["aGiTrack self-update flow"]

  exit --> exflow["Exit confirmation"]
```

**Jump to:** [Worktrees vs no-worktree](#3-worktrees-vs-no-worktree) ·
[Copy worktree-only files](#6-after-the-turn-copy-worktree-only-files-to-base) ·
[`git-commit`](#8-git-commit) ·
[Self-update flow](#9-self-update-flow) ·
[Exit and terminal close](#10-exit-and-terminal-close) ·
[Session sharing](#11-session-sharing) ·
[Settings menu](#12-settings-menu)

---

## 8. `git-commit`

Creates a user commit (no `<aGiTrack>` tag) from the user's own edits, from whichever
tree holds them — the base repo and/or this session's worktree.

> **Under `--manual-commits` / `-m` this command does more.** Manual mode always runs
> **no-worktree** (it implies `--no-worktree`), so there is only the base tree — and this same
> `git-commit` is the one command used for **both** a plain user commit and a commit that includes
> the agent's tracked work. It stages your changes, then folds every pending latent turn's trace +
> metadata into the message so the result is a **single** commit carrying your edits *and* the full
> agent tracking; the latent ref is then reset. (An external `git commit` you run yourself gets the
> same folding via the `prepare-commit-msg` hook.) See
> [Manual-commit mode](#3a-manual-commit-mode---manual-commits---m).

```mermaid
flowchart TD
  start(["git-commit"]) --> where{"Which tree is dirty?"}
  where -->|Worktree| wmsg[/"Ask for a commit message, blank rejected"/]
  where -->|Base repo| bmsg[/"Ask for a commit message, blank rejected"/]
  where -->|Both| wmsg
  where -->|Neither| none[/"No changes to commit"/]

  wmsg --> wuntracked{"Untracked files present?"}
  wuntracked -->|Yes| wstage[/"Stage all N new file(s)? y/N, re-offers every untracked file"/]
  wuntracked -->|No| wdo
  wstage --> wdo[["git add -u plus staged untracked, commit onto the turn branch"]]
  wdo --> bmsg

  bmsg --> buntracked{"Untracked files present?"}
  buntracked -->|Yes| bstage[/"Stage all N new file(s)? y/N"/]
  buntracked -->|No| bdo
  bstage --> bdo[["Commit onto the base branch"]]
  bdo --> bsync[["Sync the base commit into the worktree(s)"]]
  bsync --> done(["Done"])
  none --> done
```

---

## 9. Self-update flow

aGiTrack updating **itself** (not the backend agent). Checked at startup and every
`update_check_seconds` (default 300s) on a worker thread.

```mermaid
flowchart TD
  chk[["Check: source-linked vs package install"]] --> avail{"Update available?"}
  avail -->|No| idle(["Nothing to do"])
  avail -->|Yes| merging{"A merge or conflict resolution in progress in ANY session?"}
  merging -->|Yes| holdn[["Hold the notice until the merge finishes"]]
  holdn --> avail
  merging -->|No| where{"When?"}

  where -->|At startup, TTY| soffer[/"Offer to update before launching"/]
  where -->|During a session| inoffer[/"Status-bar notice plus Ctrl-G update command"/]

  soffer -->|Stop checking| off[["Disable update checks"]]
  soffer -->|Decline| idle
  soffer -->|Accept| apply
  inoffer -->|Accept| wait[["Defer until every session is finished and all commits are integrated: no agent in flight, no pending parse/merge/summary/background session"]]
  wait --> apply

  apply{"Apply the update"}
  apply -->|Source-linked| src[["Fast-forward only; abort with a message if the checkout is dirty or diverged"]]
  apply -->|Package| pkg[["Upgrade via the running interpreter's pip (PEP 668 defers to brew or prints every manual route). Runs DETACHED in its own session so a terminal-close SIGHUP mid-upgrade can't kill pip between uninstall and reinstall"]]
  src --> ok{"Succeeded?"}
  pkg --> ok
  ok -->|Yes| reexec[["Re-exec python -m agitrack so the new code loads"]]
  ok -->|No| failnotice[["Never crashes: record the target version, show a single manual-update reminder next startup; keep running current version"]]
  failnotice --> daemonfail[["A DAEMON that failed to hand over to its replacement keeps serving on the old version,<br/>takes its lock back, and says so. It stands down only if the replacement<br/>turned up late and now holds the lock"]]
  reexec --> done2(["Updated"])
  failnotice --> done2
```

> **An interrupted upgrade never leaves aGiTrack uninstalled.** `pip install --upgrade`
> removes the old version before writing the new, so the upgrade runs in its own session
> (`start_new_session`) and a SIGHUP/SIGTERM that arrives while it applies is **ignored**
> (the apply + restart finish) — closing the terminal or quitting VS Code mid-upgrade can no
> longer strand the package half-removed.

> Distinct from the **backend agent** (Claude / Codex / OpenCode) updating itself: that runs
> inside the agent TUI, and the sandbox is built to keep the agent's own install dirs
> writable so it always works. See `agitrack/proxy/sandbox.py`.

---

## 10. Exit and terminal close

Exiting **always asks first** — a deliberate safety net — regardless of whether anything is
pending. Pressing **Esc** at any exit prompt **cancels the exit**: aGiTrack keeps running and
tells you what was *not* done, so you can handle it yourself (commits already made this exit
are kept; nothing is deleted). Only an explicit "Yes"/"No" choice proceeds.

```mermaid
flowchart TD
  how{"How is aGiTrack ending?"}
  how -->|"Ctrl-C, or Ctrl-G then 'exit aGiTrack'"| conf[/"Exit aGiTrack? • No, keep working • Yes, exit (or press Ctrl-C again) — Esc cancels"/]
  conf -->|No / Esc| stay(["Exit cancelled — keep working (a message says nothing was shut down)"])
  conf -->|Yes| busy{"Background sessions still running?"}
  busy -->|Yes| term[/"Terminate them and exit? • No, keep working • Yes, terminate them and exit (Esc cancels)"/]
  busy -->|No| fin
  term -->|No / Esc| stay
  term -->|Yes| fin

  how -->|"Terminal or window closed, SIGHUP/SIGTERM (incl. system restart)"| sig[["_handle_exit_signal: note whether work was in progress, then best-effort finalize, render suppressed (non-interactive)"]]

  fin[["Finalize each session: commit a just-completed turn, integrate committed work. ALWAYS shows a notice the moment exit begins (teardown can take seconds) — a specific 'Finishing up before exit — …' naming the work when known, else a generic 'Finalizing things before exiting…'"]]
  fin --> copy2["Per session, before deleting its worktree: offer to copy its leftover files (see Copy)"]
  copy2 --> esc{"Esc on that copy offer?"}
  esc -->|Yes| abort(["Exit cancelled — worktree + files kept; message tells you to copy them then exit again"])
  esc -->|No| rm[["Remove the (fully-integrated) worktree(s), stop the dashboard"]]
  sig --> fin
  rm --> pend{"Forced close (SIGHUP/SIGTERM) that interrupted work, on a macOS desktop?"}
  pend -->|"No (chose to exit, clean close, or no GUI)"| bye(["aGiTrack exits"])
  pend -->|Yes| ask[/"Out-of-terminal dialog: Reopen aGiTrack • Quit aGiTrack (auto-quits after 25s, so a restart never hangs)"/]
  ask -->|Quit / timeout| bye
  ask -->|Reopen| again[["Release the lock, then open a new window running aGiTrack in the repo (last session auto-resumes)"]]
  again --> bye
```

**Jump to:** [Copy worktree-only files to base](#6-after-the-turn-copy-worktree-only-files-to-base)

---

## 10a. Stopping and restarting (`agitrack stop`)

`agitrack stop` is scoped to one repository and takes down everything aGiTrack is running for
it: the background tracker, any interactive session, and this repo's dashboard view. It also
disarms the auto-start hooks, so the next commit does not quietly bring the tracker back.

**What happens to the conversation in between.** Stopping the processes is only half of what
"stop tracking me" means. The conversation usually carries straight on in the backend's own UI,
writing into the very transcript file the next tracker will read, and aGiTrack's per-conversation
watermark is persistent by design (that is what lets a crashed or self-updated tracker pick up
exactly where it left off). So the stop is **recorded**, and the next start stamps a floor: the
moment tracking resumed. A turn that was **prompted before that floor** is never written into a
commit message by any mode, whatever a watermark says about it.

```mermaid
flowchart TD
  stop(["agitrack stop"]) --> rec[["Record that tracking is off from now (.agitrack/tracking-gap.json).<br/>A repeated stop keeps the moment the stretch actually began"]]
  rec --> kill[["Stop the tracker, any interactive session, this repo's dashboard view.<br/>Remove the auto-start hooks: the git commit hook and<br/>every backend's session hook, whatever was running"]]
  kill --> off(["aGiTrack is off. The user keeps talking to the agent in its own UI"])

  off --> hook{"An auto-start fires anyway?<br/>(a hook a backend had already loaded, one restored from dotfiles)"}
  hook -->|Yes| refuse[["Refused. The recorded stop outranks the hook files,<br/>so nothing starts tracking on its own"]]
  refuse --> off

  off --> back(["agitrack -b or agitrack -i: the USER starts tracking again"])
  back --> floor[["Stamp the floor: the moment tracking resumed"]]
  floor --> pick{"For each turn the transcript offers: when was it prompted?"}
  pick -->|"Before the floor"| skip[["Skipped. Its prompt, reply, tokens, model and capabilities<br/>reach no commit message; the watermark moves past it<br/>so it is not reconsidered on every poll"]]
  pick -->|"At or after the floor"| keep[["Tracked exactly as before"]]
```

**What the floor deliberately does not do.** It does not hide the *file changes*: work done
while aGiTrack was off is still sitting in the working tree and still needs committing, so the
next commit's diff carries it as it always did. What is withheld is the conversation. It does not
reach backwards either: turns committed before the stop stay in history, and
[Backtrace](#13-backtrace---backtrace--reconstruct-a-history-you-didnt-track) still reconstructs
whatever it is pointed at, because asking for that is an explicit request rather than automatic
tracking. And it is a moment, not a mode: everything from the restart onwards is tracked
normally.

**What does not count as a restart.** Only a start you type ends the stretch. An auto-start hook
firing is not you starting aGiTrack, so it is refused while the stop stands: removing the hooks is
not enough on its own, because the removal cannot reach a backend that has already loaded its
plugin. A dashboard opened on the repo mid-stretch reads history and records nothing, so it leaves
the stretch open too. And a tracker that restarts itself (a crash, a self-update swap) is
continuing the same tracking run rather than starting a new one, so it keeps the floor your own
restart put there.

**If a stop cannot reach something.** `agitrack stop` names any process that would not go and exits
non-zero. Your decision still governs: while the stretch is open nothing is recorded at all, so a
tracker that outlives the command cannot go on writing the conversation into commit messages.

**Jump to:** [Background mode](#3b-background-mode---background---b) ·
[Exit and terminal close](#10-exit-and-terminal-close)

---

## 11. Session sharing

Sharing pushes a session's **redacted** backend transcript to `origin` on a custom ref
(`refs/agitrack/shared-sessions`), keyed by repo + your GitHub id + a name, so collaborators
on the same repo can resume your conversation. Opt-in, with consent on every share — the
first prompt spells out exactly what is uploaded (`_share_session`,
`_resume_shared_session_menu`, `_manage_shared_sessions_menu`). Only backends with a portable
transcript (Claude) support it.

### Share this session

```mermaid
flowchart TD
  s(["⇪ Share this session"]) --> sup{"Backend supports sharing AND a resumable session exists?"}
  sup -->|No| nope[/"Not supported / nothing to share yet — explain why"/]
  sup -->|Yes| consent{"Consent — shown every share: the transcript may include file contents, command output, and secrets"}
  consent -->|No, cancel| cancel(["Cancelled"])
  consent -->|Yes, share it| redact[["Export + REDACT the transcript, build the manifest, record the lineage origin: owner + name + contributors"]]
  redact --> push[["Push to origin in the BACKGROUND so the terminal never freezes; the result lands as a notice"]]
  push --> behind{"Shared copy already has NEWER turns than this machine?"}
  behind -->|Yes| skip[["Refuse to rewind it: tell the user to resume the shared version first, then share again"]]
  behind -->|No| okp[["Shared (or saved locally if there is no remote). A diverged collaborator's turns are union-merged in, never lost"]]
  redact --> autoq{"Already auto-shared?"}
  autoq -->|No| autop[/"Keep this shared session up to date automatically? • Yes, keep it updated • No, I'll re-share manually"/]
  autop -->|Yes| auton[["Auto-update ON: every new turn (and exit) re-pushes the latest"]]
```

### Resume a shared session

```mermaid
flowchart TD
  r(["⇩ Resume a shared session"]) --> fetch[["Fetch shared sessions from origin (cancellable)"]]
  fetch --> anyr{"Any found for this repo?"}
  anyr -->|No| noner(["None found"])
  anyr -->|Yes| pickr[/"Pick one (newest first; shows model + age)"/]
  pickr --> origin[["Record its lineage origin, so a later re-share updates the SAME entry and adds you to the contributors"]]
  origin --> wherer{"Is this conversation already open locally?"}
  wherer -->|Running here, same id| livr[/"Update this session to the shared version / Keep both (copy to a new session) / Stay as it is — guards against replacing newer local work with an older shared copy"/]
  wherer -->|Open under the same shared lineage, different backend id| linr[/"Continue my existing session / Fetch the shared version as a separate copy"/]
  wherer -->|Not open here| namer[/"Name the local session (defaults to the share name)"/]
  livr --> bgr[["Fetch transcript + import on a worker thread; the resume completes on the main loop so the UI never freezes"]]
  linr --> bgr
  namer --> bgr
```

### Manage / unshare

```mermaid
flowchart TD
  mg(["⚙ Manage shared sessions"]) --> mine{"You've shared any in this repo?"}
  mine -->|No| nonem(["Nothing to manage"])
  mine -->|Yes| pickm[/"Pick one (shows age, auto-update state, and a 'local has newer turns' hint)"/]
  pickm --> act{"Manage this shared session"}
  act -->|↻ Update now| upd[["Re-push the latest transcript in the background; folds you into the contributor set"]]
  act -->|Toggle auto-update| tog[["Turn auto-update on (pushes once immediately) or off"]]
  act -->|✗ Unshare| uconf{"Remove from origin for everyone? Can't be undone."}
  uconf -->|Yes, unshare| undo[["Delete the shared ref entry (background)"]]
  uconf -->|No, keep it| keepm(["Kept"])
```

> Renaming a session **forks** it (`_fork_lineage_on_rename`): the shared lineage origin is
> cleared, so sharing the renamed session creates a NEW `<you>/<new-name>` shared entry rather
> than updating the one it came from. The whole feature is opt-in; nothing is uploaded without
> an explicit "Yes" each time.

---

## 12. Settings menu

`Ctrl-G → settings` opens an editor for **all** config options, each labelled in plain
language and showing its current effective value and source (`· repo` / `· global`, or
nothing for a built-in default). The menu is a small **form**: edits are collected as
**pending** changes — each one picks its own scope, **This repository**
(`<repo>/.agitrack/config.json`) or **Global** (`~/.agitrack/config.json`) — and is
written only when you **save on the way out**. A pending row shows its new value as
`· UNSAVED → repo/global`. Precedence: repo-local wins over global wins over the built-in
default (`GlobalConfig` overlay).

**Esc goes up one level**, everywhere ([§7](#7-ctrl-g-command-menu) describes the same rule
for every menu): Esc at a value editor → back to the list; Esc at the scope prompt → back to
the value editor; Esc on the list → close. **Closing with unsaved changes asks whether to
save them** — *Yes, save them / No, discard them / ← Keep editing* — so nothing is written
silently and nothing is lost without a prompt.

```mermaid
flowchart TD
  s(["Ctrl-G → settings"]) --> list[/"Settings list — each: label, value, source (or '· UNSAVED → scope' for a pending edit). Plus 'Timings (advanced)…' and '← Close (save N change(s))'"/]
  list -->|"← Close / Esc, no pending edits"| close(["Settings closed."])
  list -->|"← Close / Esc, with pending edits"| savep[/"You have N unsaved change(s). Save them? • Yes, save them • No, discard them • ← Keep editing"/]
  savep -->|Yes| writeall[["Write every pending edit to its chosen scope (repo overlay / global file). Restart-only settings note: won't take effect until YOU restart aGiTrack — it never restarts on its own"]]
  savep -->|No| discard[["Drop all pending edits — nothing written"]]
  savep -->|← Keep editing| list
  writeall --> close
  discard --> close

  list -->|Pick 'Timings…'| tlist[/"Timings list (seconds), pending shown as '· UNSAVED → scope' + '← Back'"/]
  list -->|Pick a setting| edit{"Editor depends on the setting's kind"}

  edit -->|on/off setting e.g. sandbox| eb[/"Turn ON / Turn OFF / ← Back"/]
  edit -->|choice setting e.g. backend| ec[/"Pick a value / ← Back"/]
  edit -->|path-list setting e.g. allowed edit paths| ep[/"Type paths separated by the PATH separator (blank = none) / ← Back"/]
  edit -->|text setting e.g. model, menu key| et[/"Type a value (blank = unset) / ← Back"/]

  eb -->|← Back / Esc → up one level| list
  ec -->|← Back / Esc| list
  ep -->|← Back / Esc| list
  et -->|← Back / Esc| list
  eb --> scope
  ec --> scope
  ep --> scope
  et --> scope

  scope[/"Apply 'setting' to: • This repository only • Global — all repositories • ← Back"/]
  scope -->|"← Back / Esc → up one level (re-edit the value)"| edit
  scope -->|This repository| pend[["Record a PENDING edit (value + scope) — not written yet"]]
  scope -->|Global| pend
  pend --> list

  tlist -->|← Back / Esc → up one level| list
  tlist -->|Pick a timing| tval[/"Type new seconds (> 0) / ← Back"/]
  tval -->|"← Back / Esc"| tlist
  tval -->|valid| tscope[/"Apply timing to: repo / global / ← Back"/]
  tscope -->|repo or global| tpend[["Record a PENDING timing edit (saved with the rest on close)"]]
  tpend --> tlist
  tscope -->|"← Back / Esc → re-edit the value"| tval
```

> **Sandbox & allowed edit paths.** By default the backend agent is confined (`sandbox`)
> so it can only write inside its session worktree (plus `.git`). `allowed_edit_paths`
> lists extra directories/files it may write to (e.g. a shared data dir). Both are settable
> here, in either config file, or per run on the command line: `--no-sandbox` and
> `--allowed-edit-paths <path>[:<path>…]` (`:`-separated like `PATH`; a CLI flag wins over
> config). On macOS the carve-out covers not-yet-created paths; under Linux bubblewrap a
> path under the read-only base must already exist to become writable.

---

## 13. Backtrace (`--backtrace`) — reconstruct a history you didn't track

Backtrace is **read-only reconstruction from local transcripts** — it works with no prior aGiTrack
use and even in a directory that was never a git repo. It answers "show me (and optionally commit)
what my past coding-agent sessions did here."

```mermaid
flowchart TD
  bt["agitrack --backtrace [text|html|stop|status|commit]"] --> disc[["Discover every Claude/Codex/OpenCode session whose recorded cwd is this directory (or a subdirectory)"]]
  disc --> exp[["Export each session; recover every turn's file edits from the tool calls — the editing tools (Edit/Write/MultiEdit, apply_patch) AND the shell (heredocs, inline Python, sed -i, echo, mv/rm)"]]
  exp --> map[["Map each turn → a virtual commit: model, tokens, timings, per-file diff, and the user↔agent trace (final response only, exactly as a real aGiTrack commit)"]]
  map --> mode{mode}
  mode -->|text| txt[["Print a one-shot report"]]
  mode -->|html / bare| serve[["Mount this directory's BACKTRACE view on the shared dashboard at /b/&lt;repo&gt;/ (frozen 'BACKTRACE, not live' banner); the header's tracked/backtrace toggle and repo switcher move between views and projects"]]
  mode -->|commit| commit
  subgraph commit["--backtrace commit --backtrace-branch NEW"]
    g1{git repo?} -->|no| gi[["Instruct: git init + commit, then re-run"]]
    g1 -->|yes| g2{clean tree?}
    g2 -->|no| gd[["Instruct: commit or .gitignore pending files until git status is clean"]]
    g2 -->|yes| g3{branch name given & unused?}
    g3 -->|no| gb[["Instruct: pass --backtrace-branch <new-branch>"]]
    g3 -->|yes| cls[["Classify each existing commit AI-vs-user by file overlap with the turns"]]
    cls --> warn[["Warn: this REWRITES history (new hashes, not a fast-forward); confirm (unless --yes)"]]
    warn --> replay[["Replay every commit onto NEW branch with git commit-tree — same tree/author/date/parents; append trace+metadata to AI commits, keep user commits verbatim (progress bar)"]]
    replay --> sw[["Switch to NEW branch; print review + force-replace instructions"]]
  end
```

- **Shell edits count too.** An agent edits through its shell as much as through its editing
  tools — Claude Code's auto mode explicitly tells it to — so the recovery reads the recorded
  command text as well: a `cat`/`tee` heredoc, an inline Python `read_text`/`replace`/`write_text`
  script, `sed -i`, `echo`/`printf` redirects, and `mv`/`cp`/`rm` of files the session itself
  wrote, following `cd` so work done elsewhere is not recorded as a change to this directory. Sub-agent work counts too: a delegated agent's transcript is a separate file nothing else reads, and its edits are attributed to the turn that launched it, exactly as its tokens already were. Nothing is ever executed; it is a parse of text the transcript already holds. Measured
  against this repository's own tracked commits, a session reconstructed 31.5% of the recorded
  lines before and ~99% per commit after. Anything the text cannot pin down exactly — a script
  that computes its output, `uv lock` and other subprocess side effects, a `$VAR` in a path — is
  skipped rather than guessed, because the reconstruction is the baseline every later diff is
  taken against.
- **View** (`text` / `html` / bare): nothing is written and nothing is uploaded. The served view
  is built once and cached (re-exporting per poll would be far too slow). Committer chrome and the
  commit hash are hidden — a reconstructed turn has no committer and no real commit — and only the
  agent's **final response** is shown, matching what a real aGiTrack commit records.
- **`stop` / `status`**: `status` reports the shared dashboard and what it serves; `stop` drops this
  directory from it (the same thing `agitrack stop` does), and a pre-hub standalone backtrace daemon
  is still stopped so an upgrade never strands one.
- **Which view opens** is decided per repository, not per command: a repo with nothing tracked opens
  here, and the first time it has a commit carrying token counts it switches to the tracked view,
  once. See [Dashboard](#15-dashboard-one-port-every-repository).
- **`commit`**: the only writing path. It is deliberately gated — git repo required, clean tree
  required, a new branch required — and it never touches the current branch. AI-vs-user attribution
  is by file overlap: a commit whose changed files an agent turn edited is AI (annotated); a commit
  no turn explains is a user commit (kept verbatim). Because it rewrites history, aGiTrack prints
  the exact `git branch -f` / `git push --force-with-lease` steps and tells the user to review the
  new branch first.

Implementation: `agitrack/metrics/backtrace.py` (view + daemon), `agitrack/metrics/backtrace_commit.py`
(the `commit` replay), `agitrack/metrics/files.py` (file browser), `agitrack/transcripts/edits.py`
(editing-tool recovery) and `agitrack/transcripts/shell_edits.py` (shell recovery).

## 14. Learn page (dashboard `/learn`): the agent coaches the user

The live dashboard's **learn** page turns the recorded interaction traces into a personal coach.
The design goal is *effortless*: the user never has to know what to ask for. They check in with two
taps (time available, current energy) and the **backend agent** does the rest: it reads the traces,
assesses the learner, names evidenced knowledge gaps, and proposes lessons sized to the moment.

```mermaid
flowchart TD
  open[["Open /learn (the 'Learn from these traces' card in the dashboard's agent-efficiency section, or the 🎓 header link; served by BOTH the live dashboard and the backtrace reconstruction)"]] --> paint[["Instant paint; page fetches /learn/state: GitHub id (git user.name fallback), stored profile, engine info, sync status, committer list"]]
  paint --> checkin[["Check-in: time (5/15/30 min) + mood (fresh/okay/tired) + optional trace source (own sessions default, a teammate, whole team) + branch (default: current; hidden in backtrace) + period + optional free text. (Almost) no captured trace in the selection → a notice explains --backtrace / running sessions through aGiTrack, and starter topics are offered instead of an agent call"]]
  checkin --> sugg[["POST /learn/suggest → one bare backend call over a trace digest (prompts, efficiency insights, rework hotspots, repo shape, README head, prior progress)"]]
  sugg --> cards[["Assessment + 3-4 suggestion cards, each with an evidence-based 'why now' and an estimated size"]]
  cards -->|tap a card| lesson[["POST /learn/lesson (full-screen 'writing your lesson' overlay) → the agent writes a STEP-BY-STEP lesson: 3-7 small steps walked one at a time, 1-3 official-doc links, quick-check quiz, and an in-page exercise (all material shown in the page; never 'run this in a terminal')"]]
  lesson --> read[["Guided lesson view: one step at a time with progress dots; the quiz, exercise and links unlock at the final step; reading time auto-tracked (visibility-aware), quiz auto-graded, typed exercise answer reviewed by the aGiTrack coach (POST /learn/exercise), follow-up chat (POST /learn/chat)"]]
  read --> done[["'Got it, done' → lesson completed, linked gap closed, progress strip updates"]]
  done --> next[["Next check-in builds on completed lessons instead of repeating them"]]
```

- **Per-user progress, local by default.** Everything is keyed by the user's GitHub id and stored
  in `.agitrack/learning.json` (git-ignored). Choosing a *different* trace source only changes what
  the coach reads; progress is always logged under the current user.
- **Optional git sync.** The "coach engine & progress sync" panel has an off-by-default toggle
  that publishes the user's progress log to `refs/agitrack/learning-progress`
  (`<repo-fingerprint>/<github-id>/progress.json`, a history-free orphan ref pushed with a lease),
  the same mechanism as shared sessions, so teammates' dashboards can list each other's progress.
  The ref also carries progress TO a new machine: where the local profile is empty, opening the
  page fetches the ref, restores the user's entry automatically, and re-enables sync.
- **Engine selection.** Content is generated by the latest session's backend/model by default;
  the same panel (or the `learning_backend` / `learning_model` config keys, repo over global)
  pins a specific backend and model. A model id in the other backend's format is dropped rather
  than failing the page.
- **Backtrace too.** The backtrace dashboard serves the same page over its reconstructed
  turns, so a project explored with `--backtrace` can be learned from as well; a frozen
  amber strip at the top (like the dashboard's) warns that the coach's suggestions are
  based on the backtraced reconstruction, not live tracking. In a directory
  that is not a git repo the page still works: identity falls back to the gh login, progress
  stays in `<dir>/.agitrack/learning.json`, and the sync toggle reports itself unavailable.
- **Isolation.** Every coach call is a bare one-shot run from a scratch dir outside any repo
  (like the summarizer), so learn conversations can never be adopted or resumed as coding
  sessions; one agent call runs at a time (a second request gets a friendly "busy" reply).

Implementation: `agitrack/metrics/learn.py` (service + page), routes in `agitrack/metrics/server.py`;
tests and the flow-by-flow matrix rows in `tests/test_learn.py` / `tests/FLOW_MATRIX.md` §12b.

---

## 15. Dashboard: one port, every repository

There is **one** dashboard process for the whole machine. It binds a single port and mounts every
repository aGiTrack knows about under a path, so switching projects or views is a link rather than
another daemon on another port. It opens by itself when aGiTrack starts on a repository, in any
mode (`open_dashboard_on_start`).

**Branches appear as soon as they land.** The branch selector offers every branch you could look
at, including one you have only fetched: a branch someone else pushed exists as
`origin/<name>` until you check it out, and it shows under that name until you do. A branch
arriving does not move the branch you are currently viewing, so the page notices it by watching
the set of branch names, not just the tip of the one on screen: pull, and it is in the dropdown on
the next poll, with no reload and no restart.

**And they disappear when they are deleted.** Git leaves `origin/<name>` behind forever unless you
fetch with `--prune`, so the list would otherwise fill with branches that were merged and removed
long ago. aGiTrack asks the remote which branches it still has, in the background and at most
every few minutes, and drops the rest from the dropdown. It never deletes anything: your refs stay
exactly as git left them, only the list is filtered. If the remote cannot be reached, nothing is
hidden, because a branch missing because your network was down is worse than a branch that lingers.

**People are identified per branch.** GitHub's commit listing covers the default branch only, so a
contributor whose work is on the branch you just pulled used to show under their raw git name
instead of their GitHub ID, and filtering by that ID found none of their commits. The branch you
are viewing is now looked up as well, so the same person is one person on every branch. It resolves
in the background: the first paint labels people from their email and their IDs settle a moment
later. Someone whose commit email is linked to no GitHub account keeps their git name, which is
all there is to know about them.

```mermaid
flowchart TD
  any(["agitrack -i / -b / -d / --backtrace,<br/>or Ctrl-G → dashboard"]) --> remember[["Remember this repo in ~/.agitrack/repos.json<br/>(path, display name, derived slug)"]]
  remember --> up{"Is the hub already running?<br/>(~/.agitrack/dashboard.json, pid alive)"}
  up -->|No| spawn[["Spawn it detached: binds 8765, or the next free port"]]
  spawn --> view
  up -->|Yes| view

  view{"Which view for this repo?"}
  view -->|"--backtrace asked for it"| bt
  view -->|"Nothing tracked, sessions exist"| bt
  view -->|"First commit with token counts:<br/>switch once, then never again"| act
  view -->|"Otherwise: the view last chosen here"| remembered["/r/ or /b/, as remembered"]

  act["/r/&lt;slug&gt;/ tracked view"]
  bt["/b/&lt;slug&gt;/ backtrace view"]

  act --> open
  bt --> open
  remembered --> open
  open[["Open the URL in the browser<br/>(printed instead on a remote/headless host)"]]

  act -.->|"header toggle"| bt
  bt -.->|"header toggle"| act
  act -.->|"header repo selector,<br/>keeps view and page"| other["another repo's same page"]
```

**Routes.** `/` redirects to the repository you used last that the switcher still offers, in the
view that fits it.
`/r/<slug>/…` is a repository's tracked dashboard and `/b/<slug>/…` its backtrace; each serves the
same sub-paths a standalone daemon would (`data`, `log`, `diff`, `files`, `learn`, `story`, …),
because every page fetches with **relative** URLs. `/repos` is what the header's switcher is built
from. A mount reached without its trailing slash redirects, so the address bar and the page's own
relative links never disagree.

**Which repositories the selector offers.** The ones aGiTrack is doing something for, or would:
tracking now, ARMED to start itself on the next commit or agent session, or open in this dashboard
(which always includes the repository you are looking at, so the switcher never contradicts the
header above it). Anything else is finished with — nothing runs there, nothing will, nobody is
looking at it — and listing it forever is how the dropdown fills up with last month's scratch
directories. The registry entry survives, so working in the repository again, or opening it with
`agitrack -d`, puts it back with its remembered view. "Armed" is its own state on the row,
`auto-start`, never `off`: with the hook in place the next commit records the agent's turns, and a
row that said "off" would send the reader off to start something that is already going to happen.

**The header.** Three controls, shared by all three pages: a repository selector (a listbox, so it
scrolls at a bounded height, shows each repository's own tracking state on its row, and can pin a
"show another repository" item that opens a how-to dialog), the tracked/backtrace toggle, and a
status light saying whether aGiTrack is running on the repository being shown and in which mode. Switching repository goes through `/go/<slug>/`,
which redirects to whichever view suits THAT repository: carrying the current view across is what
lands you on another project's empty page.

**Two views, deliberately separate.** The tracked view shows what aGiTrack recorded; the backtrace
shows what can be inferred from the agent's transcripts. They are never merged, because a
reconstruction is less accurate than a recording. Instead each points at the other: the backtrace
says plainly that it is not the active tracking, and the tracked view says when the backtrace holds
agent work no commit covers, so nothing goes quietly untracked.

**Empty repositories** get a reason rather than a blank page: no tracked commits but reconstructable
sessions opens the backtrace; no sessions at all says so and still lists the repository's own
commits; nothing at all says there is nothing to show yet.

**When it opens.** After the LAST startup question, never in the middle of them: the interactive
path asks its final questions (the privacy acknowledgment, the pre-agent commit) inside the runner,
so the open is handed to it rather than fired from the CLI beforehand.

**Stopping.** `agitrack stop` drops this repository from the dashboard (and stops the hub when
nothing is left); `agitrack -d stop` stops the dashboard itself, for every repository.

Implementation: `agitrack/metrics/hub.py` (routing + lifecycle), `agitrack/repos.py` (the repository
registry), `agitrack/metrics/pending.py` (what one view can see that the other cannot); the shared
header lives in `agitrack/metrics/ui.py`. Tests: `tests/test_hub.py`.

---

### Cross-references

- Prose spec: [`AGENTS.md`](../AGENTS.md) — Staging Behavior, Concurrent Sessions, Session Sharing, Self-Update, Concurrency and Locking.
- User-facing docs: [`README.md`](../README.md) — including [Sharing sessions](../README.md#sharing-sessions).
- Sandbox / confinement: `agitrack/proxy/sandbox.py`; per-turn commit and copy logic: `agitrack/proxy/runner.py`; sharing store: `agitrack/sessions/store.py`.
