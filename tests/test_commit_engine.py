"""Focused unit tests for agitrack.proxy.CommitEngine (#29, P4).

These tests construct CommitEngine directly — no ProxyRunner.__new__ required —
verifying the core commit pipeline, parse-result consumption, parse-worker
launch, and the simple state helpers.
"""

from __future__ import annotations

import threading
import types

import pytest

from agitrack.backends.base import TokenUsage
from agitrack.transcripts.opencode import SessionTurn
from agitrack.proxy.commit_engine import CommitEngine
from agitrack.proxy.session import Session
from agitrack.transcripts import ExportedSession
from agitrack.transcripts.types import turns_after
from agitrack.config import AgitrackState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Repo:
    """Minimal GitRepo stand-in."""

    def __init__(self, *, staged: bool = True):
        self._staged = staged
        self.message: str | None = None
        self.staged_paths: list[str] = []
        self.untracked: list[str] = []

    def add_tracked(self) -> None:
        pass

    def has_staged_changes(self) -> bool:
        return self._staged

    def commit(self, message: str) -> str:
        self.message = message
        return "dead1234"

    def untracked_files(self) -> list[str]:
        return list(self.untracked)

    def stage_paths(self, paths: list[str]) -> None:
        self.staged_paths.extend(paths)


def _noop_stage(repo, state) -> None:
    """No-op stage_untracked_fn for tests that don't care about untracked."""


def _engine(tmp_path, *, staged: bool = True) -> tuple[CommitEngine, _Repo, AgitrackState]:
    repo = _Repo(staged=staged)
    state = AgitrackState(tmp_path)
    engine = CommitEngine(repo, state)
    return engine, repo, state


def _turn(
    prompt: str,
    response: str,
    *,
    total: int = 1,
    output: int = 1,
    reasoning_effort: str | None = None,
    assistant_id: str = "aid",
    backend_version: str | None = None,
) -> SessionTurn:
    return SessionTurn(
        "uid",
        assistant_id,
        prompt,
        response,
        TokenUsage(total=total, output=output, input=total - output),
        None,
        complete=True,
        reasoning_effort=reasoning_effort,
        backend_version=backend_version,
    )


def test_commit_turns_records_latest_reasoning_effort(tmp_path):
    engine, repo, state = _engine(tmp_path)
    engine.commit_turns(
        # The most recent turn that recorded a level wins; a later None doesn't erase it.
        turns=[
            _turn("a", "done", reasoning_effort="on"),
            _turn("b", "done", reasoning_effort="high"),
            _turn("c", "done"),
        ],
        backend="opencode",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
    )
    assert repo.message is not None
    assert "reasoning_effort: high" in repo.message


def test_commit_turns_records_the_harness_version_that_made_the_latest_turn(tmp_path):
    # The agent HARNESS owns the tool set, the system prompt and the editing style, and updates
    # itself far more often than the model changes — so a commit that names only the backend and
    # model cannot say what actually produced it. A CLI can also update itself MID-SPAN, which is
    # why the newest turn's version wins, exactly as the model and reasoning effort do.
    engine, repo, state = _engine(tmp_path)
    engine.commit_turns(
        turns=[
            _turn("a", "done", backend_version="2.1.236"),
            _turn("b", "done", backend_version="2.1.238"),
            _turn("c", "done"),  # a later turn without one must not erase it
        ],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
    )
    assert repo.message is not None
    assert "backend_version: 2.1.238" in repo.message


def test_commit_turns_omits_the_harness_version_when_no_turn_records_one(tmp_path):
    # An ordinary commit's metadata is unchanged for a backend that reports no version.
    engine, repo, state = _engine(tmp_path)
    engine.commit_turns(
        turns=[_turn("a", "done")],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
    )
    assert repo.message is not None and "backend_version" not in repo.message


@pytest.mark.parametrize("label", ["(background task completed)", "(background monitor update)"])
def test_a_background_wake_up_is_not_attributed_to_the_user(tmp_path, label):
    # THE BUG: the harness's synthetic label for a turn the agent ran off a BACKGROUND EVENT was
    # recorded as an ordinary user prompt, so the commit carried a "## User" block the user never
    # wrote — and the same label reached the commit subject and the dashboard's per-commit prompt.
    engine, repo, state = _engine(tmp_path)

    engine.commit_turns(
        turns=[_turn(label, "Verified the run and wrote the report.")],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
    )

    assert repo.message is not None
    assert "## User" not in repo.message
    assert f"## User\n\n{label}" not in repo.message
    # The subject describes what was ASKED FOR; nobody asked for this, so it falls back rather
    # than claiming the user requested "(background task completed)".
    assert repo.message.startswith("<aGiTrack> claude changes")
    # The event itself survives — it is what explains a turn with no prompt.
    note = " ".join(line.lstrip("> ") for line in repo.message.splitlines() if line.startswith(">"))
    assert "woken here by a background event" in note
    assert "## Agent\n\nVerified the run and wrote the report." in repo.message


def test_a_real_prompt_alongside_a_background_wake_up_still_owns_the_subject(tmp_path):
    # A commit routinely spans both kinds of turn (a deferred wake-up folds into the next real
    # one). The real prompt must still be the user's, and still drive the subject.
    engine, repo, state = _engine(tmp_path)

    engine.commit_turns(
        turns=[
            _turn("(background task completed)", "Build finished."),
            _turn("now ship it", "Shipped."),
        ],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
    )

    assert repo.message is not None
    assert repo.message.count("## User") == 1
    assert "## User\n\nnow ship it" in repo.message
    assert repo.message.startswith("<aGiTrack> now ship it")


def test_commit_turns_omits_reasoning_effort_when_no_turn_records_it(tmp_path):
    engine, repo, state = _engine(tmp_path)
    engine.commit_turns(
        turns=[_turn("a", "done")],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
    )
    assert repo.message is not None
    assert "reasoning_effort:" not in repo.message


def test_commit_turns_records_only_final_agent_message_by_default(tmp_path):
    engine, repo, state = _engine(tmp_path)
    turn = _turn("do it", "Done.")
    turn.agent_messages = ["On it.", "Done."]
    engine.commit_turns(
        turns=[turn],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
    )
    assert repo.message is not None
    # Default: only the final reply lands in the trace.
    assert "Done." in repo.message
    assert "On it." not in repo.message


def test_commit_turns_records_all_agent_messages_when_option_on(tmp_path):
    engine, repo, state = _engine(tmp_path)
    state.full_agent_messages = True
    turn = _turn("do it", "Done.")
    turn.agent_messages = ["On it.", "Done."]
    engine.commit_turns(
        turns=[turn],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
    )
    assert repo.message is not None
    # Every user-facing message appears, each as its own "## Agent" block.
    assert "On it." in repo.message
    assert "Done." in repo.message
    assert repo.message.count("## Agent") == 2


def test_commit_turns_full_agent_messages_override_forces_on(tmp_path):
    # The per-run override (e.g. --full-agent-messages) forces all messages even
    # when the per-repo config is off.
    repo = _Repo(staged=True)
    state = AgitrackState(tmp_path)
    assert state.full_agent_messages is False
    engine = CommitEngine(repo, state, full_agent_messages=True)
    turn = _turn("do it", "Done.")
    turn.agent_messages = ["On it.", "Done."]
    engine.commit_turns(
        turns=[turn],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
    )
    assert repo.message is not None
    assert "On it." in repo.message
    assert repo.message.count("## Agent") == 2


def test_commit_turns_full_agent_messages_override_none_defers_to_config(tmp_path):
    # With no override (None), the per-repo config decides — here it's on.
    repo = _Repo(staged=True)
    state = AgitrackState(tmp_path)
    state.full_agent_messages = True
    engine = CommitEngine(repo, state, full_agent_messages=None)
    turn = _turn("do it", "Done.")
    turn.agent_messages = ["On it.", "Done."]
    engine.commit_turns(
        turns=[turn],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
    )
    assert repo.message is not None
    assert "On it." in repo.message


def test_commit_turns_records_conversation_anchor_of_last_turn(tmp_path):
    engine, repo, state = _engine(tmp_path)
    engine.commit_turns(
        # The anchor links to the last covered turn's backend message id.
        turns=[
            _turn("a", "done", assistant_id="msg-1"),
            _turn("b", "done", assistant_id="msg-2"),
        ],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
    )
    assert repo.message is not None
    assert "conversation_anchor: msg-2" in repo.message


def test_commit_turns_omits_conversation_anchor_when_no_message_id(tmp_path):
    engine, repo, state = _engine(tmp_path)
    engine.commit_turns(
        turns=[_turn("a", "done", assistant_id="")],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
    )
    assert repo.message is not None
    assert "conversation_anchor:" not in repo.message


# ---------------------------------------------------------------------------
# CommitEngine.commit_turns — proxy mode (accumulate_trace_only_on_commit=False)
# ---------------------------------------------------------------------------


def test_an_interrupted_turn_is_committed_as_interrupted_not_as_completed_work(tmp_path):
    # A turn the user cancelled (Esc) still commits — its partial edits are real work — but
    # it must SAY so. Claude answers "I'll create the ten files now" before its first tool
    # call, so an interrupted turn carries a final_response and reads exactly like a
    # finished one; the commit then documented ten files over a two-file diff.
    engine, repo, state = _engine(tmp_path)
    turn = _turn("create ten files r1..r10", "I'll create the ten files now.")
    turn.interrupted = True

    assert (
        engine.commit_turns(
            turns=[turn],
            backend="claude",
            backend_session_id="s1",
            model="m",
            stage_untracked_fn=_noop_stage,
        )
        is True
    )
    assert "interrupted: true" in repo.message  # machine-readable
    assert "interrupted this turn before the agent finished" in repo.message  # human-readable
    assert "NOT completed" in repo.message
    # …and the ONE line `git log --oneline` shows says so too.
    assert repo.message.splitlines()[0].startswith("<aGiTrack> (interrupted) ")


def test_a_span_that_ENDS_on_a_finished_turn_is_not_marked_interrupted(tmp_path):
    """One Esc early in a multi-turn span must not stamp the whole commit as cut short.

    A commit routinely covers several turns. Marking on "any turn was interrupted" made
    `git log --oneline` say the work was unfinished when the closing turn had run to
    completion — seen live on a four-turn span where the user had stopped a single tool call
    at the start and everything after it finished and was pushed. What the mark answers is
    whether the work this commit DELIVERS was cut short, and the turn it ends on decides that.
    """
    engine, repo, state = _engine(tmp_path)
    stopped = _turn("start the refactor", "I'll begin.")
    stopped.interrupted = True
    finished = _turn("now finish it", "Done: all call sites updated.")

    assert (
        engine.commit_turns(
            turns=[stopped, finished],
            backend="claude",
            backend_session_id="s1",
            model="m",
            stage_untracked_fn=_noop_stage,
        )
        is True
    )
    assert not repo.message.splitlines()[0].startswith("<aGiTrack> (interrupted) ")
    assert "interrupted: true" not in repo.message
    assert "interrupted this turn before the agent finished" not in repo.message


def test_a_span_that_ENDS_on_an_interrupted_turn_is_still_marked(tmp_path):
    # The other direction, and the reason the mark exists: the work the commit closes on was
    # cut short, so the one line most people read has to say so.
    engine, repo, state = _engine(tmp_path)
    finished = _turn("add the parser", "Done.")
    stopped = _turn("now wire it up", "I'll wire it up now.")
    stopped.interrupted = True

    engine.commit_turns(
        turns=[finished, stopped],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
    )

    assert repo.message.splitlines()[0].startswith("<aGiTrack> (interrupted) ")
    assert "interrupted: true" in repo.message


def test_the_summarizer_is_told_the_turn_was_interrupted(tmp_path):
    # The trace is the summarizer's SOLE input, so the fact has to travel in the trace
    # text itself: otherwise the subject line asserts the whole request as done.
    engine, repo, state = _engine(tmp_path)
    seen: list[str] = []
    turn = _turn("create ten files r1..r10", "I'll create the ten files now.")
    turn.interrupted = True

    engine.commit_turns(
        turns=[turn],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
        summarize_fn=lambda trace: (seen.append(trace), ("Started creating the files.", []))[1],
    )

    assert seen and "interrupted this turn before the agent finished" in seen[0]


def test_an_ordinary_turn_carries_no_interruption_note(tmp_path):
    engine, repo, state = _engine(tmp_path)
    engine.commit_turns(
        turns=[_turn("add a file", "Added it.")],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
    )
    assert "interrupted" not in repo.message


def test_commit_turns_surfaces_compactions_and_clears_origin_event(tmp_path):
    # The commit message records the compaction count (summed across the committed
    # turns) and the session's fork/copy origin; the origin event is one-shot, cleared
    # once surfaced so later commits don't keep re-announcing the lineage.
    engine, repo, state = _engine(tmp_path)
    state.set_session_origin_event(kind="fork", source="ses_parent", source_name="main")
    turns = [_turn("first", "a"), _turn("second", "b")]
    turns[0].compaction_count = 1
    turns[1].compaction_count = 2

    assert (
        engine.commit_turns(
            turns=turns,
            backend="claude",
            backend_session_id="s1",
            model="m",
            stage_untracked_fn=_noop_stage,
        )
        is True
    )
    assert "context_compactions: 3" in repo.message
    assert "forked_from: ses_parent (main)" in repo.message
    assert "forked from 'main'" in repo.message  # the trace note
    # One-shot: the event is gone after the commit that surfaced it.
    assert state.session_origin_event() is None


def test_commit_turns_returns_false_for_empty_turns(tmp_path):
    engine, repo, state = _engine(tmp_path)
    assert (
        engine.commit_turns(
            turns=[],
            backend="claude",
            backend_session_id="s1",
            model="m",
            stage_untracked_fn=_noop_stage,
        )
        is False
    )
    assert repo.message is None


def test_commit_turns_returns_false_when_nothing_staged(tmp_path):
    engine, repo, state = _engine(tmp_path, staged=False)
    assert (
        engine.commit_turns(
            turns=[_turn("fix it", "done")],
            backend="claude",
            backend_session_id="s1",
            model="m",
            stage_untracked_fn=_noop_stage,
        )
        is False
    )


def test_commit_turns_creates_commit_when_staged(tmp_path):
    engine, repo, state = _engine(tmp_path)
    assert (
        engine.commit_turns(
            turns=[_turn("fix the bug", "done")],
            backend="claude",
            backend_session_id="s1",
            model="m",
            stage_untracked_fn=_noop_stage,
        )
        is True
    )
    assert repo.message is not None
    assert repo.message.startswith("<aGiTrack> fix the bug")


def test_commit_turns_token_not_counted_on_failed_attempt(tmp_path):
    engine, repo, state = _engine(tmp_path, staged=False)
    engine.commit_turns(
        turns=[_turn("fix it", "done", total=100, output=10)],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
    )
    assert state.pending_token_usage()["input"] == 0


def test_commit_turns_calls_pre_commit_fn(tmp_path):
    engine, repo, state = _engine(tmp_path)
    called = []
    engine.commit_turns(
        turns=[_turn("do it", "done")],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
        pre_commit_fn=lambda: called.append(True),
    )
    assert called == [True]


def test_commit_turns_calls_on_commit_fn_with_sha_and_trace(tmp_path):
    engine, repo, state = _engine(tmp_path)
    received = []
    engine.commit_turns(
        turns=[_turn("do it", "done")],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
        on_commit_fn=lambda sha, trace, is_cover: received.append((sha, trace, is_cover)),
    )
    assert len(received) == 1
    sha, trace, is_cover = received[0]
    assert sha == "dead1234"
    assert is_cover is False  # a plain commit (no backend commits to cover)
    # The trace handed to the callback is the real, rebuilt interaction trace
    # (the summarizer's input), not an empty/stale one.
    assert "## User" in trace and "do it" in trace
    assert "## Agent" in trace and "done" in trace


def test_commit_turns_subject_joins_multiple_prompts(tmp_path):
    engine, repo, state = _engine(tmp_path)
    engine.commit_turns(
        turns=[
            _turn("add parser", "done1"),
            _turn("add tests", "done2"),
        ],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
    )
    subject = repo.message.splitlines()[0]
    assert subject == "<aGiTrack> add parser / add tests"


def test_commit_turns_pending_trace_cleared_after_commit(tmp_path):
    engine, repo, state = _engine(tmp_path)
    state.append_trace("user", "prior prompt")
    engine.commit_turns(
        turns=[_turn("new prompt", "done")],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
    )
    assert state.pending_trace() == []


def test_commit_turns_preserves_pending_users_not_in_turns(tmp_path):
    # A user prompt recorded in state that never appears as a turn's user_prompt
    # (e.g. from an earlier incomplete parse) is carried into the commit.
    engine, repo, state = _engine(tmp_path)
    state.append_trace("user", "initial orphan prompt")
    engine.commit_turns(
        turns=[_turn("second prompt", "done")],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
    )
    assert "initial orphan prompt" in repo.message
    assert "second prompt" in repo.message


def test_garbled_and_empty_leftovers_do_not_pollute_the_trace(tmp_path):
    # The proxy reconstructs raw typed bytes, so a follow-up typed while the agent was busy can be
    # captured garbled ("ontinue" for "Continue") or empty. Such captures used to leak into the
    # trace as spurious extra ## User headings (the empty one rendered as a bare "## User").
    engine, repo, state = _engine(tmp_path)
    state.append_trace("user", "ontinue")  # a dropped-leading-char capture of "Continue"
    state.append_trace("user", "   ")  # a blank capture
    engine.commit_turns(
        turns=[_turn("Continue", "done")],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
    )
    trace = repo.message.split("# Interaction Trace", 1)[1].split("# aGiTrack Metadata", 1)[0]
    # Exactly one ## User (the clean "Continue") — the garbled and empty captures are dropped,
    # not rendered as extra (or bare) headings.
    assert trace.count("## User") == 1 and "Continue" in trace


def test_leftover_user_message_precedes_multi_message_agent_response(tmp_path):
    # A message the user sent mid-turn (a pending "leftover" prompt) must appear BEFORE the
    # agent's response in the trace — even when the turn emitted several agent messages. The
    # agent's reply covers everything the user said, so the leftover belongs right after the
    # turn's prompt, not wedged between (or after) the agent's messages. The old logic
    # inserted before the LAST agent message, which (with full_agent_messages on) dropped
    # the leftover after the agent's earlier replies — the Claude trace-ordering bug.
    engine, repo, state = _engine(tmp_path)
    engine._full_agent_messages = True  # each agent message becomes its own ## Agent block
    state.append_trace("user", "wait, also handle the edge case")  # typed while agent worked
    turn = _turn("add the feature", "all done")
    turn.agent_messages = ["starting now", "still working", "all done"]
    engine.commit_turns(
        turns=[turn],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
    )
    msg = repo.message
    assert msg is not None
    # rindex skips the leftover's appearance in the subject line and finds its ## User block
    # in the trace body — which must precede every ## Agent block of the turn.
    leftover_pos = msg.rindex("wait, also handle the edge case")
    assert leftover_pos < msg.index("starting now")
    assert msg.index("starting now") < msg.index("still working") < msg.index("all done")


def test_queued_followups_render_as_separate_user_headings_without_duplication(tmp_path):
    # A message the user QUEUES mid-turn belongs to the turn but is a DISTINCT message: it gets its
    # OWN ## User heading (sent after the agent already responded), not merged into the base prompt.
    # The submit-time capture still records the base separately — it must dedup against the turn's
    # base (not re-added), and tokens are counted once per TURN regardless of the trace text.
    engine, repo, state = _engine(tmp_path)
    base = "Please fix the parser and make sure the tests pass"
    state.append_trace("user", base)  # submit-time capture of the base prompt
    turn = _turn(base, "done", total=100, output=40)
    turn.queued_followups = ["Also add a status command.", "Also verify the token counts are correct."]
    engine.commit_turns(
        turns=[turn],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
    )
    msg = repo.message
    assert msg is not None
    body = msg.split("# Interaction Trace", 1)[1]  # ignore the subject line
    # Three DISTINCT ## User headings: the base + the two queued follow-ups. The base is NOT
    # duplicated by the submit-time leftover.
    assert body.count("## User") == 3
    assert "Also add a status command." in body and "Also verify the token counts are correct." in body
    # Tokens reflect the ONE turn, not doubled by the extra trace entries.
    assert "tokens_since_last_commit_output: 40" in msg
    assert "tokens_since_last_commit_output: 80" not in msg


def test_a_long_turns_queued_messages_survive_the_trace_limit(tmp_path):
    """A turn is limited as ONE turn however many messages the user queued into it.

    The engine used to record every follow-up as a plain `user` entry, and the trace limiter
    counts `user` entries as turns — so a turn carrying more queued messages than the limit was
    cut inside itself, losing the prompt that opened it. Measured on a real session: eleven
    queued messages against a limit of 5 left the commit's trace starting at the ninth thing the
    user had said, with the opening prompt in no commit at all."""
    engine, repo, state = _engine(tmp_path)
    turn = _turn("the opening prompt", "one answer covering all of it", total=100, output=40)
    turn.queued_followups = [f"queued thought {i}" for i in range(11)]

    engine.commit_turns(
        turns=[turn],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
    )

    body = (repo.message or "").split("# Interaction Trace", 1)[1]
    assert "the opening prompt" in body
    assert all(f"queued thought {i}" in body for i in range(11))


def test_queued_message_the_user_deleted_before_sending_is_not_traced(tmp_path):
    # A message typed into the composer, queued, and then DELETED never reaches the agent, so the
    # backend transcript has no record of it — but the proxy captured it at submit time and it used
    # to survive as a "leftover" ## User block. The user RETRACTED that text; attributing it to
    # them is wrong (a prompt meant for another window once landed in a commit this way). When the
    # turn carries queued follow-ups, the transcript is proven to record delivered queued messages,
    # so a capture matching none of them was never delivered — drop it.
    engine, repo, state = _engine(tmp_path)
    base = "When I created a new session in no worktree mode, aGiTrack died"
    state.append_trace("user", base)  # submit-time capture of the base prompt
    state.append_trace("user", "Once fixed, please run the TUI yourself on a dummy repo")
    state.append_trace("user", "Is this the minimum amount of maths needed/mo")  # queued, then deleted
    turn = _turn(base, "done")
    turn.queued_followups = ["Once fixed, please run the TUI yourself on a dummy repo"]
    engine.commit_turns(
        turns=[turn],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
    )
    msg = repo.message
    assert msg is not None
    assert "minimum amount of maths" not in msg  # neither in the trace nor in the subject
    body = msg.split("# Interaction Trace", 1)[1]
    assert body.count("## User") == 2  # the base prompt and the DELIVERED queued follow-up
    assert base in body and "run the TUI yourself" in body


def test_pending_capture_is_still_kept_when_the_turn_has_no_queued_followups(tmp_path):
    # The safety net stays where there is no evidence either way: with no queued follow-ups on the
    # turn, a capture the transcript doesn't show may be a genuine mid-turn message (or one from an
    # earlier incomplete parse), so it is still carried into the commit rather than dropped.
    engine, repo, state = _engine(tmp_path)
    state.append_trace("user", "wait, also handle the edge case")
    engine.commit_turns(
        turns=[_turn("add the feature", "all done")],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
    )
    assert "wait, also handle the edge case" in repo.message


def test_commit_turns_stage_untracked_fn_receives_repo_and_state(tmp_path):
    engine, repo, state = _engine(tmp_path)
    calls = []

    def capture_stage(r, s):
        calls.append((r, s))

    engine.commit_turns(
        turns=[_turn("do it", "done")],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=capture_stage,
    )
    assert calls == [(repo, state)]


def test_commit_turns_auto_stages_untracked_excluding_declined(tmp_path):
    # When stage_untracked_fn does the auto-staging (non-interactive / proxy
    # exit), it must skip files the user intentionally declined.
    engine, repo, state = _engine(tmp_path)
    repo.untracked = ["new_file.py", "ignored.log"]
    state.add_declined(["ignored.log"])

    def stage_untracked_fn(r, s):
        declined = set(s.declined_untracked())
        r.stage_paths([p for p in r.untracked_files() if p not in declined])

    engine.commit_turns(
        turns=[_turn("add file", "done")],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=stage_untracked_fn,
    )
    assert "new_file.py" in repo.staged_paths
    assert "ignored.log" not in repo.staged_paths


# ---------------------------------------------------------------------------
# CommitEngine.commit_turns — actions mode (accumulate_trace_only_on_commit=True)
# ---------------------------------------------------------------------------


def test_commit_turns_actions_mode_no_trace_on_failed_attempt(tmp_path):
    # Bug #14 (d041d10): a failed attempt must leave state pristine.
    engine, repo, state = _engine(tmp_path, staged=False)
    engine.commit_turns(
        turns=[_turn("fix it", "done", total=140, output=10)],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
        accumulate_trace_only_on_commit=True,
    )
    assert state.pending_trace() == []
    assert state.pending_token_usage()["input"] == 0


def test_commit_turns_actions_mode_succeeds_with_clean_state(tmp_path):
    engine, repo, state = _engine(tmp_path)
    result = engine.commit_turns(
        turns=[_turn("fix it", "done", total=50, output=10)],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
        accumulate_trace_only_on_commit=True,
    )
    assert result is True
    assert repo.message is not None
    assert state.pending_trace() == []


# ---------------------------------------------------------------------------
# CommitEngine.record_user_prompt / await_followup
# ---------------------------------------------------------------------------


def test_record_user_prompt_appends_to_trace(tmp_path):
    engine, _, state = _engine(tmp_path)
    engine.record_user_prompt("hello world")
    trace = state.pending_trace()
    assert len(trace) == 1
    assert trace[0]["role"] == "user"
    assert trace[0]["content"] == "hello world"


def test_record_user_prompt_noop_on_empty(tmp_path):
    engine, _, state = _engine(tmp_path)
    engine.record_user_prompt("")
    assert state.pending_trace() == []


def test_record_user_prompt_skips_slash_commands(tmp_path):
    # A bare slash command (e.g. the user typing /compact in the TUI) is a backend
    # directive, not a prompt. It must not be recorded into the trace, or it surfaces
    # in the commit as a stray '## User /comp' entry — redundant with the compaction
    # lead-in note the trace already carries.
    engine, _, state = _engine(tmp_path)
    for command in ("/compact", "/comp", "  /model opus  ", "/clear"):
        engine.record_user_prompt(command)
    assert state.pending_trace() == []  # none recorded
    # A real prompt that merely mentions a slash mid-sentence is still recorded.
    engine.record_user_prompt("run /compact after you finish")
    assert [item["content"] for item in state.pending_trace()] == ["run /compact after you finish"]


def test_await_followup_appends_normalized(tmp_path):
    engine, _, _ = _engine(tmp_path)
    result = engine.await_followup("  fix   the bug  ", [])
    assert result == ["fix the bug"]


def test_await_followup_skips_slash_commands(tmp_path):
    engine, _, _ = _engine(tmp_path)
    result = engine.await_followup("/compact", [])
    assert result == []


def test_await_followup_skips_empty(tmp_path):
    engine, _, _ = _engine(tmp_path)
    assert engine.await_followup("", []) == []


# ---------------------------------------------------------------------------
# CommitEngine.sanitize_state_trace
# ---------------------------------------------------------------------------


def test_sanitize_state_trace_removes_event_blobs(tmp_path):
    engine, _, state = _engine(tmp_path)
    blob = '{"type":"event","payload":"..."}'

    class FakeBackend:
        def is_event_blob(self, content):
            return content.startswith('{"type":"event"')

    state.append_trace("user", "my prompt")
    state.append_trace("agent", blob)
    state.append_trace("agent", "a normal response")

    engine.sanitize_state_trace(FakeBackend())

    items = state.pending_trace()
    assert len(items) == 2
    assert all(item["content"] != blob for item in items)


def test_sanitize_state_trace_noop_when_clean(tmp_path):
    engine, _, state = _engine(tmp_path)

    class FakeBackend:
        def is_event_blob(self, content):
            return False

    state.append_trace("user", "hello")
    state.append_trace("agent", "world")
    engine.sanitize_state_trace(FakeBackend())
    assert len(state.pending_trace()) == 2


# ---------------------------------------------------------------------------
# CommitEngine.start_parse / parse worker
# ---------------------------------------------------------------------------


def test_start_parse_sets_parse_thread_on_session(tmp_path):
    state = AgitrackState(tmp_path)
    session = Session.bare()
    session.state = state
    session.worktree = None
    session.backend = types.SimpleNamespace(
        name="claude",
        latest_session_id=lambda repo: None,
        export_session=lambda repo, sid: None,
    )
    session.repo = types.SimpleNamespace(repo=tmp_path)

    engine = CommitEngine(None, state)
    started = engine.start_parse(
        session=session,
        discover_session_id_fn=lambda: None,
        debug_fn=lambda *a, **k: None,
    )
    assert started is True
    assert session.agent_parse_thread is not None
    session.agent_parse_thread.join(timeout=5)


def test_start_parse_returns_false_when_already_running(tmp_path):
    state = AgitrackState(tmp_path)
    session = Session.bare()
    session.state = state
    session.worktree = None

    ready = threading.Event()

    class SlowBackend:
        name = "claude"

        def latest_session_id(self, repo):
            return None

        def export_session(self, repo, sid):
            ready.wait(timeout=5)
            return None

    session.backend = SlowBackend()
    session.repo = types.SimpleNamespace(repo=tmp_path)

    engine = CommitEngine(None, state)
    assert (
        engine.start_parse(
            session=session,
            discover_session_id_fn=lambda: None,
            debug_fn=lambda *a, **k: None,
        )
        is True
    )
    # Second launch while thread is alive
    assert (
        engine.start_parse(
            session=session,
            discover_session_id_fn=lambda: None,
            debug_fn=lambda *a, **k: None,
        )
        is False
    )
    ready.set()
    session.agent_parse_thread.join(timeout=5)


def test_start_parse_writes_result_to_owning_session(tmp_path):
    state = AgitrackState(tmp_path)
    session = Session.bare()
    session.state = state
    # Use a worktree so the worker calls backend.latest_session_id to find the id.
    session.worktree = object()

    exported = ExportedSession("ses-1", "m", None, [])

    class Backend:
        name = "claude"

        def latest_session_id(self, repo):
            return "ses-1"

        def export_session(self, repo, sid):
            return exported

    session.backend = Backend()
    session.repo = types.SimpleNamespace(repo=tmp_path)

    engine = CommitEngine(None, state)
    engine.start_parse(
        session=session,
        discover_session_id_fn=lambda: None,
        debug_fn=lambda *a, **k: None,
    )
    session.agent_parse_thread.join(timeout=5)
    assert session.agent_parse_result is not None
    sid, exp, last_id, owner_state = session.agent_parse_result
    assert sid == "ses-1"
    assert exp is exported
    assert owner_state is state


def _noworktree_session(tmp_path, state, *, export_sink):
    session = Session.bare()
    session.state = state
    session.worktree = None

    class Backend:
        name = "claude"

        def latest_session_id(self, repo):
            return None  # unused in no-worktree mode

        def export_session(self, repo, sid):
            export_sink["sid"] = sid
            return ExportedSession(sid, "m", None, [])

    session.backend = Backend()
    session.repo = types.SimpleNamespace(repo=tmp_path)
    return session


def test_start_parse_no_worktree_follows_switched_session(tmp_path):
    # No-worktree mode prefers snapshot-based discovery (a session that appeared AFTER
    # launch — a Claude /resume or a new conversation started inside the backend) over the
    # pinned id, so all modes follow an in-backend session switch.
    state = AgitrackState(tmp_path)
    state.backend_session_id = "pinned"
    seen: dict = {}
    session = _noworktree_session(tmp_path, state, export_sink=seen)

    engine = CommitEngine(None, state)
    engine.start_parse(session=session, discover_session_id_fn=lambda: "switched", debug_fn=lambda *a, **k: None)
    session.agent_parse_thread.join(timeout=5)

    assert seen["sid"] == "switched"  # followed the switch, not the pinned id
    assert session.agent_parse_result[0] == "switched"


def test_start_parse_no_worktree_falls_back_to_pinned_when_no_switch(tmp_path):
    # When discovery finds no post-launch switch (returns None) the worker keeps the pinned
    # id — so a normal continuation is unaffected.
    state = AgitrackState(tmp_path)
    state.backend_session_id = "pinned"
    seen: dict = {}
    session = _noworktree_session(tmp_path, state, export_sink=seen)

    engine = CommitEngine(None, state)
    engine.start_parse(session=session, discover_session_id_fn=lambda: None, debug_fn=lambda *a, **k: None)
    session.agent_parse_thread.join(timeout=5)

    assert seen["sid"] == "pinned"


def test_start_parse_reads_per_conversation_watermark(tmp_path):
    # The parse worker reads the watermark for the conversation actually being exported —
    # so after a switch it uses that conversation's own committed mark, not the last one.
    state = AgitrackState(tmp_path)
    state.backend_session_id = "A"
    state.set_backend_message_id("A", "a-hi")  # A's committed high-water mark
    state.data["backend_message_ids"] = {"A": "a-hi", "B": "b-hi"}  # B was tracked before
    seen: dict = {}
    session = _noworktree_session(tmp_path, state, export_sink=seen)

    engine = CommitEngine(None, state)
    engine.start_parse(session=session, discover_session_id_fn=lambda: "B", debug_fn=lambda *a, **k: None)
    session.agent_parse_thread.join(timeout=5)

    sid, _exp, last_id, _owner = session.agent_parse_result
    assert sid == "B"
    assert last_id == "b-hi"  # B's own watermark, not A's (a-hi)


# ---------------------------------------------------------------------------
# CommitEngine.finish_parse_if_ready
# ---------------------------------------------------------------------------


def _make_finish_helpers(tmp_path, session, exported_session, *, last_message_id=None):
    """Set up a CommitEngine and helper stubs for finish_parse_if_ready tests."""
    state = AgitrackState(tmp_path)
    session.state = state
    session.backend = types.SimpleNamespace(name="claude")
    session.agent_parse_result = (exported_session.session_id, exported_session, last_message_id, state)
    session.agent_parse_thread = None

    repo = _Repo(staged=True)
    engine = CommitEngine(repo, state)

    commits = []

    def commit_fn(**kwargs):
        commits.append(kwargs)
        return True

    return engine, state, commits, commit_fn


def test_finish_parse_defers_monitor_update_only_turns(tmp_path):
    # A Monitor tick wakes the agent, it acknowledges, the turn completes — but committing
    # every tick floods the history (a real overnight session produced 100+ such commits).
    # While the live loop runs (require_complete), monitor-update-only turns are deferred:
    # no commit, watermark untouched, so they fold into the next substantive commit.
    session = Session.bare()
    exported = ExportedSession(
        "ses-mon",
        "m",
        None,
        [
            SessionTurn(
                "tn1",
                "a1",
                "(background monitor update)",
                "Noted. Waiting for finals.",
                TokenUsage(total=1, output=1),
                None,
            ),
            SessionTurn(
                "tn2", "a2", "(background monitor update)", "Still running.", TokenUsage(total=1, output=1), None
            ),
        ],
    )
    engine, state, commits, commit_fn = _make_finish_helpers(tmp_path, session, exported)

    result, _ = engine.finish_parse_if_ready(
        session=session,
        quiet=True,
        prompt_untracked=False,
        require_complete=True,
        awaited_followups=[],
        agent_is_active_fn=lambda: False,
        debug_fn=lambda *a, **k: None,
        note_session_change_fn=lambda sid: None,
        mirror_fn=lambda sid: None,
        commit_fn=commit_fn,
    )
    assert result is None and commits == []
    # The watermark did not advance: the deferred turns will be re-exported next parse.
    assert state.backend_message_id_for("ses-mon") is None


def test_prompt_only_turn_defers_until_the_agent_answers(tmp_path):
    # Background auto mode, the eb85b0f bug: the user typed a prompt, the agent has
    # not replied yet, and the tree is dirty (a pre-commit flush, leftover edits).
    # The live loop must WAIT for the agent's final message, never commit a trace
    # that is only the user's message.
    session = Session.bare()
    exported = ExportedSession(
        "ses-prompt-only",
        "m",
        None,
        [SessionTurn("u1", "", "expand the experiments", "", TokenUsage(), None, complete=False)],
    )
    engine, state, commits, commit_fn = _make_finish_helpers(tmp_path, session, exported)

    result, _ = engine.finish_parse_if_ready(
        session=session,
        quiet=True,
        prompt_untracked=False,
        require_complete=True,
        awaited_followups=[],
        agent_is_active_fn=lambda: False,
        debug_fn=lambda *a, **k: None,
        note_session_change_fn=lambda sid: None,
        mirror_fn=lambda sid: None,
        commit_fn=commit_fn,
    )
    assert result is None
    assert commits == []
    assert state.backend_message_id_for("ses-prompt-only") is None


def test_force_commit_trims_trailing_unanswered_turn(tmp_path):
    # A flush/exit force commit (require_complete=False) arriving just after the user
    # typed the NEXT prompt: the answered turn commits, but the unanswered trailing turn
    # is trimmed — the trace never ends with a bare user message, the watermark stays
    # before the unanswered turn, and that turn commits properly once the agent replies.
    session = Session.bare()
    exported = ExportedSession(
        "ses-trim",
        "m",
        None,
        [
            SessionTurn("u1", "a1", "fix the tests", "Fixed them.", TokenUsage(total=5, output=5), None),
            SessionTurn("u2", "", "now update the docs", "", TokenUsage(), None, complete=False),
        ],
    )
    engine, state, commits, commit_fn = _make_finish_helpers(tmp_path, session, exported)

    result, _ = engine.finish_parse_if_ready(
        session=session,
        quiet=True,
        prompt_untracked=False,
        require_complete=False,
        awaited_followups=[],
        agent_is_active_fn=lambda: False,
        debug_fn=lambda *a, **k: None,
        note_session_change_fn=lambda sid: None,
        mirror_fn=lambda sid: None,
        commit_fn=commit_fn,
    )
    assert result is True and len(commits) == 1
    assert [t.user_message_id for t in commits[0]["turns"]] == ["u1"]
    # Watermark = the answered turn, so the unanswered one re-exports next parse.
    assert state.backend_message_id_for("ses-trim") == "a1"


def test_sole_unanswered_turn_still_force_commits_on_exit(tmp_path):
    # The exception: exit with ONLY an in-flight turn still captures it (nothing else
    # would); turns_after re-exports it if the conversation later continues.
    session = Session.bare()
    exported = ExportedSession(
        "ses-sole",
        "m",
        None,
        [SessionTurn("u1", "", "long running task", "", TokenUsage(), None, complete=True)],
    )
    engine, state, commits, commit_fn = _make_finish_helpers(tmp_path, session, exported)

    result, _ = engine.finish_parse_if_ready(
        session=session,
        quiet=True,
        prompt_untracked=False,
        require_complete=False,
        awaited_followups=[],
        agent_is_active_fn=lambda: False,
        debug_fn=lambda *a, **k: None,
        note_session_change_fn=lambda sid: None,
        mirror_fn=lambda sid: None,
        commit_fn=commit_fn,
    )
    assert result is True and len(commits) == 1


def test_finish_parse_commits_substantive_monitor_turn_immediately(tmp_path):
    # The final report of a monitored job often arrives ON a monitor wake-up, so its turn
    # carries the update label. A NORMAL final message (substantive response, real output)
    # must commit immediately, without waiting for a terminal notification, another
    # prompt, or any background-task horizon; only trivial tick acknowledgments defer.
    session = Session.bare()
    report = (
        "Sweep finished: 62.5% accuracy on the eval set, 80% on the probe. I wrote the "
        "full comparison to results/ANALYSIS.md, updated the plots, and the cap-fifo "
        "variant is the clear winner; unbounded memory plateaus after 4.4k tokens. "
        "Next I suggest rerunning the 2b model with the tuned eviction threshold."
    )
    exported = ExportedSession(
        "ses-mon-final",
        "m",
        None,
        [
            SessionTurn("tn1", "a1", "(background monitor update)", "Noted.", TokenUsage(total=1, output=1), None),
            SessionTurn("tn2", "a2", "(background monitor update)", report, TokenUsage(total=900, output=900), None),
        ],
    )
    engine, state, commits, commit_fn = _make_finish_helpers(tmp_path, session, exported)

    result, _ = engine.finish_parse_if_ready(
        session=session,
        quiet=True,
        prompt_untracked=False,
        require_complete=True,
        awaited_followups=[],
        agent_is_active_fn=lambda: False,
        debug_fn=lambda *a, **k: None,
        note_session_change_fn=lambda sid: None,
        mirror_fn=lambda sid: None,
        commit_fn=commit_fn,
    )
    assert result is True and len(commits) == 1
    assert len(commits[0]["turns"]) == 2  # the deferred tick rides along


def test_finish_parse_commits_monitor_turn_with_heavy_output_despite_short_reply(tmp_path):
    # A monitor turn that did real work (tool calls, edits) but closed with a terse reply:
    # the output tokens betray the work, so it commits rather than deferring.
    session = Session.bare()
    exported = ExportedSession(
        "ses-mon-work",
        "m",
        None,
        [
            SessionTurn(
                "tn1",
                "a1",
                "(background monitor update)",
                "Fixed the crash.",
                TokenUsage(total=2400, output=2400),
                None,
            ),
        ],
    )
    engine, state, commits, commit_fn = _make_finish_helpers(tmp_path, session, exported)

    result, _ = engine.finish_parse_if_ready(
        session=session,
        quiet=True,
        prompt_untracked=False,
        require_complete=True,
        awaited_followups=[],
        agent_is_active_fn=lambda: False,
        debug_fn=lambda *a, **k: None,
        note_session_change_fn=lambda sid: None,
        mirror_fn=lambda sid: None,
        commit_fn=commit_fn,
    )
    assert result is True and len(commits) == 1


def test_finish_parse_commits_monitor_updates_with_a_substantive_turn(tmp_path):
    # Once a substantive turn lands (here: the terminal task-completed turn), the deferred
    # monitor ticks ride along in the SAME commit instead of their own.
    session = Session.bare()
    exported = ExportedSession(
        "ses-mon2",
        "m",
        None,
        [
            SessionTurn("tn1", "a1", "(background monitor update)", "Noted.", TokenUsage(total=1, output=1), None),
            SessionTurn(
                "tn2",
                "a2",
                "(background task completed)",
                "Sweep done: wrote results.",
                TokenUsage(total=2, output=2),
                None,
            ),
        ],
    )
    engine, state, commits, commit_fn = _make_finish_helpers(tmp_path, session, exported)

    result, _ = engine.finish_parse_if_ready(
        session=session,
        quiet=True,
        prompt_untracked=False,
        require_complete=True,
        awaited_followups=[],
        agent_is_active_fn=lambda: False,
        debug_fn=lambda *a, **k: None,
        note_session_change_fn=lambda sid: None,
        mirror_fn=lambda sid: None,
        commit_fn=commit_fn,
    )
    assert result is True and len(commits) == 1
    assert [t.user_prompt for t in commits[0]["turns"]] == [
        "(background monitor update)",
        "(background task completed)",
    ]


def test_finish_parse_exit_finalize_commits_monitor_update_only_turns(tmp_path):
    # The exit finalize (require_complete=False) must flush deferred monitor turns so a
    # session that only ever ticked still leaves nothing uncommitted behind.
    session = Session.bare()
    exported = ExportedSession(
        "ses-mon3",
        "m",
        None,
        [
            SessionTurn("tn1", "a1", "(background monitor update)", "Noted.", TokenUsage(total=1, output=1), None),
        ],
    )
    engine, state, commits, commit_fn = _make_finish_helpers(tmp_path, session, exported)

    result, _ = engine.finish_parse_if_ready(
        session=session,
        quiet=True,
        prompt_untracked=False,
        require_complete=False,
        awaited_followups=[],
        agent_is_active_fn=lambda: False,
        debug_fn=lambda *a, **k: None,
        note_session_change_fn=lambda sid: None,
        mirror_fn=lambda sid: None,
        commit_fn=commit_fn,
    )
    assert result is True and len(commits) == 1


def test_finish_parse_defers_while_async_subagents_are_still_running(tmp_path):
    # The reported bug: the agent spawns async sub-agents, then posts what READS as a final
    # answer ("kicked it off, here's the plan") while the sub-agents are doing the actual job.
    # The turn is complete by every other measure, so aGiTrack committed there — recording a
    # finished-sounding trace (and a summary built from it) over a tree the sub-agents were
    # still writing. While any sub-agent is live the commit WAITS and the watermark stays put.
    session = Session.bare()
    exported = ExportedSession(
        "ses-sub",
        "m",
        None,
        [SessionTurn("u1", "a1", "rewrite the parser", "Launched two agents on it.", TokenUsage(total=9), None)],
        live_subagent_ids=["a40f517ebe10c670c", "ab6501a5bf1f3589d"],
    )
    engine, state, commits, commit_fn = _make_finish_helpers(tmp_path, session, exported)

    result, _ = engine.finish_parse_if_ready(
        session=session,
        quiet=True,
        prompt_untracked=False,
        require_complete=True,
        awaited_followups=[],
        agent_is_active_fn=lambda: False,
        debug_fn=lambda *a, **k: None,
        note_session_change_fn=lambda sid: None,
        mirror_fn=lambda sid: None,
        commit_fn=commit_fn,
    )

    assert result is None and commits == []
    assert state.backend_message_id_for("ses-sub") is None
    # The live sub-agents also count as background writers of the tree, so the pre-agent
    # user-commit dialog stands down rather than claiming their edits as the user's.
    assert session.live_background_task_ids == ["a40f517ebe10c670c", "ab6501a5bf1f3589d"]


def test_finish_parse_commits_once_the_sub_agents_have_reported_back(tmp_path):
    # Same session one parse later: the sub-agents reported back, the agent did its follow-up
    # work, and nothing is live any more — so the deferred launch turn and the follow-up turn
    # commit TOGETHER, which is the point of deferring rather than dropping.
    session = Session.bare()
    exported = ExportedSession(
        "ses-sub2",
        "m",
        None,
        [
            SessionTurn("u1", "a1", "rewrite the parser", "Launched two agents on it.", TokenUsage(total=9), None),
            SessionTurn(
                "tn1", "a2", "(background task completed)", "Both agents landed; verified.", TokenUsage(total=40), None
            ),
        ],
    )
    engine, state, commits, commit_fn = _make_finish_helpers(tmp_path, session, exported)

    result, _ = engine.finish_parse_if_ready(
        session=session,
        quiet=True,
        prompt_untracked=False,
        require_complete=True,
        awaited_followups=[],
        agent_is_active_fn=lambda: False,
        debug_fn=lambda *a, **k: None,
        note_session_change_fn=lambda sid: None,
        mirror_fn=lambda sid: None,
        commit_fn=commit_fn,
    )

    assert result is True and len(commits) == 1
    assert [t.user_prompt for t in commits[0]["turns"]] == ["rewrite the parser", "(background task completed)"]
    assert state.backend_message_id_for("ses-sub2") == "a2"


def test_finish_parse_exit_finalize_commits_despite_live_sub_agents(tmp_path):
    # The exit/stop finalize (require_complete=False) never defers: a sub-agent that is still
    # running when the session ends must not take the work already on disk with it.
    session = Session.bare()
    exported = ExportedSession(
        "ses-sub3",
        "m",
        None,
        [SessionTurn("u1", "a1", "rewrite the parser", "Launched an agent on it.", TokenUsage(total=9), None)],
        live_subagent_ids=["a40f517ebe10c670c"],
    )
    engine, state, commits, commit_fn = _make_finish_helpers(tmp_path, session, exported)

    result, _ = engine.finish_parse_if_ready(
        session=session,
        quiet=True,
        prompt_untracked=False,
        require_complete=False,
        awaited_followups=[],
        agent_is_active_fn=lambda: False,
        debug_fn=lambda *a, **k: None,
        note_session_change_fn=lambda sid: None,
        mirror_fn=lambda sid: None,
        commit_fn=commit_fn,
    )

    assert result is True and len(commits) == 1


def test_finish_parse_returns_none_when_no_result(tmp_path):
    state = AgitrackState(tmp_path)
    session = Session.bare()
    session.state = state
    session.agent_parse_thread = None
    session.agent_parse_result = None

    engine = CommitEngine(None, state)
    result, awaited = engine.finish_parse_if_ready(
        session=session,
        quiet=True,
        prompt_untracked=False,
        require_complete=True,
        awaited_followups=[],
        agent_is_active_fn=lambda: False,
        debug_fn=lambda *a, **k: None,
        note_session_change_fn=lambda sid: None,
        mirror_fn=lambda sid: None,
        commit_fn=lambda **k: True,
    )
    assert result is None


def test_finish_parse_defers_incomplete_turn(tmp_path):
    session = Session.bare()
    exported = ExportedSession(
        "ses-1",
        "m",
        None,
        [
            SessionTurn("u1", "a1", "do it", "partial", TokenUsage(total=1, output=1), None, complete=False),
        ],
    )
    engine, state, commits, commit_fn = _make_finish_helpers(tmp_path, session, exported)

    result, _ = engine.finish_parse_if_ready(
        session=session,
        quiet=True,
        prompt_untracked=False,
        require_complete=True,
        awaited_followups=[],
        agent_is_active_fn=lambda: True,
        debug_fn=lambda *a, **k: None,
        note_session_change_fn=lambda sid: None,
        mirror_fn=lambda sid: None,
        commit_fn=commit_fn,
    )
    assert result is None
    assert commits == []


def test_finish_parse_commits_complete_turn(tmp_path):
    session = Session.bare()
    # SessionTurn(user_message_id, assistant_message_id, user_prompt, final_response,
    #             tokens, model, complete)
    exported = ExportedSession(
        "ses-1",
        "m",
        None,
        [
            SessionTurn("u1", "msg-1", "fix it", "done", TokenUsage(total=1, output=1), None, complete=True),
        ],
    )
    engine, state, commits, commit_fn = _make_finish_helpers(tmp_path, session, exported)

    result, _ = engine.finish_parse_if_ready(
        session=session,
        quiet=True,
        prompt_untracked=False,
        require_complete=True,
        awaited_followups=[],
        agent_is_active_fn=lambda: False,
        debug_fn=lambda *a, **k: None,
        note_session_change_fn=lambda sid: None,
        mirror_fn=lambda sid: None,
        commit_fn=commit_fn,
    )
    assert result is True
    assert len(commits) == 1
    # Watermark advanced to the assistant_message_id of the last complete turn
    assert state.last_backend_message_id == "msg-1"


def test_finish_parse_defers_awaited_followup_while_active(tmp_path):
    session = Session.bare()
    exported = ExportedSession(
        "ses-1",
        "m",
        None,
        [
            SessionTurn("u1", "a1", "prompt one", "done", TokenUsage(total=1, output=1), None, complete=True),
        ],
    )
    engine, state, commits, commit_fn = _make_finish_helpers(tmp_path, session, exported)

    result, new_awaited = engine.finish_parse_if_ready(
        session=session,
        quiet=True,
        prompt_untracked=False,
        require_complete=True,
        awaited_followups=["queued follow-up"],
        agent_is_active_fn=lambda: True,  # agent still running
        debug_fn=lambda *a, **k: None,
        note_session_change_fn=lambda sid: None,
        mirror_fn=lambda sid: None,
        commit_fn=commit_fn,
    )
    assert result is None
    assert commits == []
    assert "queued follow-up" in new_awaited


def test_finish_parse_clears_awaited_on_interrupt(tmp_path):
    # An Esc interrupt clears the awaited list so commits are not deferred forever.
    session = Session.bare()
    exported = ExportedSession(
        "ses-1",
        "m",
        None,
        [
            SessionTurn(
                "u1", "a1", "fix", "partial", TokenUsage(total=1, output=1), None, complete=True, interrupted=True
            ),
        ],
    )
    engine, state, commits, commit_fn = _make_finish_helpers(tmp_path, session, exported)

    result, new_awaited = engine.finish_parse_if_ready(
        session=session,
        quiet=True,
        prompt_untracked=False,
        require_complete=True,
        awaited_followups=["a prompt that was discarded"],
        agent_is_active_fn=lambda: True,
        debug_fn=lambda *a, **k: None,
        note_session_change_fn=lambda sid: None,
        mirror_fn=lambda sid: None,
        commit_fn=commit_fn,
    )
    assert result is True
    assert new_awaited == []


def _cancelled_exported():
    # A turn the user interrupted before any committable response: interrupted,
    # empty final_response (so it is not in complete_turns).
    return ExportedSession(
        "ses-1",
        "m",
        None,
        [
            SessionTurn("u1", "a1", "build it", "", TokenUsage(total=1, output=1), None, interrupted=True),
        ],
    )


def test_finish_parse_invokes_cancel_handler_and_advances_watermark(tmp_path):
    # A cancelled turn with no committable response routes to on_cancelled_fn; when
    # it reports it handled the leftover changes, the watermark advances past the
    # turn so it isn't reconsidered, and no normal commit happens.
    session = Session.bare()
    engine, state, commits, commit_fn = _make_finish_helpers(tmp_path, session, _cancelled_exported())
    seen = []

    result, _ = engine.finish_parse_if_ready(
        session=session,
        quiet=True,
        prompt_untracked=False,
        require_complete=True,
        awaited_followups=[],
        agent_is_active_fn=lambda: False,
        debug_fn=lambda *a, **k: None,
        note_session_change_fn=lambda sid: None,
        mirror_fn=lambda sid: None,
        commit_fn=commit_fn,
        on_cancelled_fn=lambda turns: seen.append(turns) or True,
    )
    assert result is False
    assert commits == []  # no normal commit for a response-less turn
    assert len(seen) == 1
    assert state.last_backend_message_id == "a1"
    # ...with the mark's TIME, like every other advance: turns_after falls back to it when a
    # compaction stops the id matching a turn boundary, and a stale one re-exports committed turns.
    assert state.backend_message_marked_at_for(state.backend_session_id)


def test_finish_parse_cancel_handler_keep_does_not_advance_watermark(tmp_path):
    # If the handler declines (user kept the changes to decide later), the watermark
    # is left untouched so the turn is still the current tail.
    session = Session.bare()
    engine, state, commits, commit_fn = _make_finish_helpers(tmp_path, session, _cancelled_exported())

    result, _ = engine.finish_parse_if_ready(
        session=session,
        quiet=True,
        prompt_untracked=False,
        require_complete=True,
        awaited_followups=[],
        agent_is_active_fn=lambda: False,
        debug_fn=lambda *a, **k: None,
        note_session_change_fn=lambda sid: None,
        mirror_fn=lambda sid: None,
        commit_fn=commit_fn,
        on_cancelled_fn=lambda turns: False,
    )
    assert result is False
    assert state.last_backend_message_id is None


def test_finish_parse_commits_finished_turn_with_no_final_text_response(tmp_path):
    # A turn that FINISHED (complete, not interrupted) but emitted no final TEXT response —
    # e.g. its last action was a file edit and the agent stopped without a closing message —
    # must still be committed if it changed files. Dropping it would leave the work uncommitted
    # forever (the live loop re-parses and the exit finalize hits the same gate). It is NOT a
    # cancellation, so the cancel handler is never consulted.
    session = Session.bare()
    exported = ExportedSession(
        "ses-1",
        "m",
        None,
        [SessionTurn("u1", "a1", "build it", "", TokenUsage(total=1, output=1), None, complete=True)],
    )
    engine, state, commits, commit_fn = _make_finish_helpers(tmp_path, session, exported)
    called = []

    result, _ = engine.finish_parse_if_ready(
        session=session,
        quiet=True,
        prompt_untracked=False,
        require_complete=True,
        awaited_followups=[],
        agent_is_active_fn=lambda: False,
        debug_fn=lambda *a, **k: None,
        note_session_change_fn=lambda sid: None,
        mirror_fn=lambda sid: None,
        commit_fn=commit_fn,
        on_cancelled_fn=lambda turns: called.append(turns) or True,
    )
    assert result is True  # the edits are committed, not dropped
    assert len(commits) == 1
    assert called == []  # not a cancellation — the handler is never called
    # Watermark advances past the turn (its assistant id) so it isn't re-committed.
    assert state.last_backend_message_id == "a1"


def test_finish_parse_no_text_turn_watermark_falls_back_to_user_id(tmp_path):
    # A no-text finished turn never recorded an assistant id (that is only set from a text
    # message). The watermark must still advance — off the user id — so the turn isn't
    # reconsidered and re-committed on the next parse.
    session = Session.bare()
    exported = ExportedSession(
        "ses-1",
        "m",
        None,
        [SessionTurn("u1", "", "build it", "", TokenUsage(total=1, output=1), None, complete=True)],
    )
    engine, state, commits, commit_fn = _make_finish_helpers(tmp_path, session, exported)

    result, _ = engine.finish_parse_if_ready(
        session=session,
        quiet=True,
        prompt_untracked=False,
        require_complete=True,
        awaited_followups=[],
        agent_is_active_fn=lambda: False,
        debug_fn=lambda *a, **k: None,
        note_session_change_fn=lambda sid: None,
        mirror_fn=lambda sid: None,
        commit_fn=commit_fn,
    )
    assert result is True
    assert state.last_backend_message_id == "u1"


def test_finish_parse_defers_a_genuinely_mid_flight_turn(tmp_path):
    # The mid-flight guard is upstream: a turn still in progress (complete=False) with
    # require_complete=True is DEFERRED (None), never reaching the no-response commit path.
    session = Session.bare()
    exported = ExportedSession(
        "ses-1",
        "m",
        None,
        [SessionTurn("u1", "a1", "build it", "", TokenUsage(total=1, output=1), None, complete=False)],
    )
    engine, state, commits, commit_fn = _make_finish_helpers(tmp_path, session, exported)

    result, _ = engine.finish_parse_if_ready(
        session=session,
        quiet=True,
        prompt_untracked=False,
        require_complete=True,
        awaited_followups=[],
        agent_is_active_fn=lambda: False,
        debug_fn=lambda *a, **k: None,
        note_session_change_fn=lambda sid: None,
        mirror_fn=lambda sid: None,
        commit_fn=commit_fn,
    )
    assert result is None  # deferred, not committed
    assert commits == []


def _codex_interrupted_exported(reply: str = "partial"):
    """A turn the user interrupted, shaped the way CODEX records it.

    Claude resolves an interrupt to ``complete=True``; Codex closes the aborted turn with
    ``complete=False`` (``turn_aborted``/``task_aborted``/``turn_interrupted``). Both mean the
    same thing — the turn is over — so both must commit the same way.
    """
    return ExportedSession(
        "ses-1",
        "m",
        None,
        [
            SessionTurn(
                "u1",
                "a1",
                "big refactor",
                reply,
                TokenUsage(total=1, output=1),
                None,
                complete=False,
                interrupted=True,
            )
        ],
    )


def test_finish_parse_commits_an_interrupted_turn_the_backend_left_incomplete(tmp_path):
    # An INTERRUPTED turn can never receive more messages, so the mid-flight gate must not hold
    # it: on Codex (which closes an aborted turn with complete=False) the commit was deferred on
    # every poll and the agent's partial edits were never committed at all — the same Esc on
    # Claude (complete=True) committed immediately.
    session = Session.bare()
    engine, state, commits, commit_fn = _make_finish_helpers(tmp_path, session, _codex_interrupted_exported())

    result, _ = engine.finish_parse_if_ready(
        session=session,
        quiet=True,
        prompt_untracked=False,
        require_complete=True,
        awaited_followups=[],
        agent_is_active_fn=lambda: False,
        debug_fn=lambda *a, **k: None,
        note_session_change_fn=lambda sid: None,
        mirror_fn=lambda sid: None,
        commit_fn=commit_fn,
    )
    assert result is True
    assert len(commits) == 1
    # Anchored on the ASSISTANT id, not the user id: the turn is finished, and a user-id anchor
    # would re-export it inclusively on the next parse and commit its trace a second time.
    assert state.last_backend_message_id == "a1"
    assert state.partial_turn_usage() is None


def test_finish_parse_offers_an_incomplete_interrupted_turn_to_the_cancel_handler(tmp_path):
    # Same shape, but the interrupt landed before any committable response: it must reach the
    # keep/commit/discard offer (which lives past the mid-flight gate), not be deferred forever.
    session = Session.bare()
    engine, state, commits, commit_fn = _make_finish_helpers(tmp_path, session, _codex_interrupted_exported(""))
    seen = []

    result, _ = engine.finish_parse_if_ready(
        session=session,
        quiet=True,
        prompt_untracked=False,
        require_complete=True,
        awaited_followups=[],
        agent_is_active_fn=lambda: False,
        debug_fn=lambda *a, **k: None,
        note_session_change_fn=lambda sid: None,
        mirror_fn=lambda sid: None,
        commit_fn=commit_fn,
        on_cancelled_fn=lambda turns: seen.append(turns) or True,
    )
    assert result is False
    assert commits == [] and len(seen) == 1


def test_finish_parse_exit_commits_dangling_no_text_turn(tmp_path):
    # The exit / sync finalize path (require_complete=False) must commit a finished-but-dangling
    # no-text turn's edits — this is exactly the "commit not made even on exit" case.
    session = Session.bare()
    exported = ExportedSession(
        "ses-1",
        "m",
        None,
        # complete=False (ended on a tool_use) but the process is gone, so on exit we commit it.
        [SessionTurn("u1", "a1", "apply the change", "", TokenUsage(total=1, output=1), None, complete=False)],
    )
    engine, state, commits, commit_fn = _make_finish_helpers(tmp_path, session, exported)

    result, _ = engine.finish_parse_if_ready(
        session=session,
        quiet=True,
        prompt_untracked=False,
        require_complete=False,  # exit / sync finalize
        awaited_followups=[],
        agent_is_active_fn=lambda: False,
        debug_fn=lambda *a, **k: None,
        note_session_change_fn=lambda sid: None,
        mirror_fn=lambda sid: None,
        commit_fn=commit_fn,
    )
    assert result is True
    assert len(commits) == 1
    # Anchored on the USER id, not the assistant id the turn happens to carry: the turn is
    # captured MID-FLIGHT (complete=False) and has sent no reply, so per the force-capture
    # contract it must re-export INCLUSIVELY if the backend resumes and finishes it. Keying on
    # "a1" instead lost that turn outright — a resumed turn's real final message and edits
    # matched no boundary and the marked_at fallback dropped it for having started earlier.
    # While it stays dangling both anchors behave identically (turns_after excludes a user-id
    # watermarked turn that still has no final_response).
    assert state.last_backend_message_id == "u1"
    assert state.partial_turn_usage()["user_id"] == "u1"  # tokens counted once, via the delta


def test_finish_parse_discards_stale_session_result(tmp_path):
    # A result whose owner_state differs from self.state is silently discarded.
    state_a = AgitrackState(tmp_path / "a")
    state_b = AgitrackState(tmp_path / "b")

    session = Session.bare()
    session.state = state_a
    # Result is tagged with state_b (a stale worker from before a session switch)
    session.agent_parse_result = ("ses-b", None, None, state_b)
    session.agent_parse_thread = None

    engine = CommitEngine(None, state_a)
    result, _ = engine.finish_parse_if_ready(
        session=session,
        quiet=True,
        prompt_untracked=False,
        require_complete=True,
        awaited_followups=[],
        agent_is_active_fn=lambda: False,
        debug_fn=lambda *a, **k: None,
        note_session_change_fn=lambda sid: None,
        mirror_fn=lambda sid: None,
        commit_fn=lambda **k: True,
    )
    assert result is None  # discarded, not committed


# ---------------------------------------------------------------------------
# CommitEngine.recover_nonempty_session / initialize_session_baseline
# ---------------------------------------------------------------------------


def test_recover_nonempty_session_returns_none_on_exception(tmp_path):
    state = AgitrackState(tmp_path)
    engine = CommitEngine(None, state)

    class FailBackend:
        def latest_session_id(self, repo):
            raise RuntimeError("network error")

    result = engine.recover_nonempty_session(
        FailBackend(),
        types.SimpleNamespace(repo=tmp_path),
        lambda sid: None,
    )
    assert result is None


def test_recover_nonempty_session_returns_none_when_same_id(tmp_path):
    state = AgitrackState(tmp_path)
    state.backend_session_id = "ses-1"
    engine = CommitEngine(None, state)

    class SameBackend:
        def latest_session_id(self, repo):
            return "ses-1"

    result = engine.recover_nonempty_session(
        SameBackend(),
        types.SimpleNamespace(repo=tmp_path),
        lambda sid: None,
    )
    assert result is None


def test_initialize_session_baseline_clears_when_not_continue(tmp_path):
    state = AgitrackState(tmp_path)
    state.backend_session_id = "old-ses"
    state.last_backend_message_id = "old-msg"
    engine = CommitEngine(None, state)

    engine.initialize_session_baseline(
        None,
        None,
        should_continue_fn=lambda: False,
        stage_backend_resume_fn=lambda sid: None,
    )
    assert state.backend_session_id is None
    assert state.last_backend_message_id is None


def test_partial_turn_recommit_adds_only_the_token_delta(tmp_path):
    # A turn force-captured mid-flight (exit/restart) is re-exported INCLUSIVELY once it
    # finishes. Its tokens must be counted EXACTLY ONCE across the two commits: the first
    # commit records the usage counted so far, the re-commit adds only the delta. Adding
    # the whole turn again on every restart inflated recorded tokens 13-26x on long-turn
    # days (2026-07-25) — while per-turn parsing (and the backtrace built on it) was right.
    session = Session.bare()
    running = SessionTurn(
        "u1", "", "huge refactor", "", TokenUsage(input=100, output=10, cache_write=40), None, complete=True
    )
    exported = ExportedSession("ses-part", "m", None, [running])
    engine, state, commits, commit_fn = _make_finish_helpers(tmp_path, session, exported)

    def accumulating_commit_fn(**kwargs):
        for turn in kwargs["turns"]:
            engine._add_turn_usage(turn)  # what the real commit_turns does per turn
        commits.append(kwargs)
        return True

    kwargs = dict(
        session=session,
        quiet=True,
        prompt_untracked=False,
        awaited_followups=[],
        agent_is_active_fn=lambda: False,
        debug_fn=lambda *a, **k: None,
        note_session_change_fn=lambda sid: None,
        mirror_fn=lambda sid: None,
        commit_fn=accumulating_commit_fn,
    )
    # Commit 1: exit force-capture of the still-running turn.
    result, _ = engine.finish_parse_if_ready(require_complete=False, **kwargs)
    assert result is True and len(commits) == 1
    first = state.pending_token_usage()
    assert (first["input"], first["output"]) == (100, 10)
    partial = state.partial_turn_usage()
    assert partial and partial["user_id"] == "u1" and partial["usage"]["input"] == 100

    # The commit happened: its pending usage is flushed exactly as commit_turns does.
    state.clear_trace()

    # Commit 2: the conversation continued; the SAME turn finished, much larger.
    finished = SessionTurn(
        "u1",
        "a1",
        "huge refactor",
        "done at last",
        TokenUsage(input=300, output=50, cache_write=90),
        None,
        complete=True,
    )
    session.agent_parse_result = (exported.session_id, ExportedSession("ses-part", "m", None, [finished]), None, state)
    result, _ = engine.finish_parse_if_ready(require_complete=True, **kwargs)
    assert result is True and len(commits) == 2

    second = state.pending_token_usage()
    # Only the DELTA was added: 300-100 input, 50-10 output, 90-40 cache_write.
    assert (second["input"], second["output"], second["cache_write"]) == (200, 40, 50)
    assert state.partial_turn_usage() is None  # consumed; a third pass would start clean
    # Across both commits the turn's true totals were recorded exactly once.
    assert first["input"] + second["input"] == 300
    assert first["output"] + second["output"] == 50


def test_partial_turn_delta_never_goes_negative(tmp_path):
    # Defensive: if a re-exported turn somehow reports LESS usage than already recorded
    # (clock skew, truncated transcript), the delta floors at zero instead of corrupting
    # the pending counters.
    state = AgitrackState(tmp_path)
    engine = CommitEngine(_Repo(staged=True), state)
    state.set_partial_turn_usage(None, "u1", {"input": 500, "output": 100})
    shrunken = SessionTurn("u1", "a1", "p", "r", TokenUsage(input=200, output=40), None, complete=True)
    engine._add_turn_usage(shrunken)
    pending = state.pending_token_usage()
    assert (pending["input"], pending["output"]) == (0, 0)
    assert state.partial_turn_usage() is None


def test_lost_watermark_never_reexports_the_whole_session(tmp_path):
    # A context compaction can reshape turn boundaries so the stored watermark id no
    # longer matches any turn boundary. The old fallback exported EVERYTHING — which
    # re-committed a 20-day conversation as one 31M-token commit (2026-07-25). With the
    # mark's timestamp recorded, only turns that began after it are new; without even a
    # timestamp (legacy state), just the newest turn re-anchors tracking.
    from agitrack.transcripts.types import turns_after

    old = SessionTurn("u1", "a1", "ancient", "done", TokenUsage(output=1_000_000), None, started_at=100, ended_at=200)
    newer = SessionTurn("u2", "a2", "recent", "done", TokenUsage(output=10), None, started_at=900, ended_at=950)
    exported = ExportedSession("ses-lost", "m", None, [old, newer])

    with_time = turns_after(exported, "msg_vanished", marked_at=500)
    assert with_time == [newer]  # the ancient, already-committed turn is never re-exported

    legacy = turns_after(exported, "msg_vanished")
    assert legacy == [newer]  # last turn only — bounded, re-anchors on its id

    # A turn with NO timestamps is unknowable and errs toward export (synthetic
    # transcripts; real ones always stamp times) — it must not read as "ancient".
    untimed = SessionTurn("u3", "a3", "untimed", "done", TokenUsage(output=1), None)
    exported_untimed = ExportedSession("ses-lost2", "m", None, [old, untimed])
    assert turns_after(exported_untimed, "msg_vanished", marked_at=500) == [untimed]


def test_watermark_advance_records_the_mark_timestamp(tmp_path):
    # Every watermark advance stores WHEN the mark landed, so a later boundary reshuffle
    # falls back to time instead of the full-history export.
    session = Session.bare()
    turn = SessionTurn("u1", "a1", "p", "done", TokenUsage(output=5), None, started_at=1000, ended_at=1010)
    exported = ExportedSession("ses-mark", "m", None, [turn])
    engine, state, commits, commit_fn = _make_finish_helpers(tmp_path, session, exported)
    state.data["backend_session_id"] = "ses-mark"

    result, _ = engine.finish_parse_if_ready(
        session=session,
        quiet=True,
        prompt_untracked=False,
        require_complete=True,
        awaited_followups=[],
        agent_is_active_fn=lambda: False,
        debug_fn=lambda *a, **k: None,
        note_session_change_fn=lambda sid: None,
        mirror_fn=lambda sid: None,
        commit_fn=commit_fn,
    )
    assert result is True
    assert state.backend_message_marked_at_for("ses-mark") == 1010


def test_a_midflight_capture_anchors_on_the_user_id_not_a_transient_assistant_id(tmp_path):
    # THE BUG (2026-08-15): a backend mints a NEW message id per assistant response within a
    # turn, so the assistant id a force capture (exit/stop finalize, e.g. the tracker
    # restarting mid-turn) reads is TRANSIENT. Anchoring on it meant that once the turn
    # continued, its assistant_message_id became a later id, the stored mark matched no turn
    # boundary, and turns_after's marked_at fallback dropped the turn for having STARTED
    # before the mark — so history kept only the PARTIAL snapshot the capture took, and the
    # turn's real final response and remaining tokens were recorded nowhere. The old guard
    # keyed on `not assistant_message_id`, so it caught only a turn captured before it had
    # said ANYTHING; every mid-flight turn that had already spoken (the common case) fell
    # through.
    session = Session.bare()
    midflight = SessionTurn(
        "u1",
        "msg_transient",  # the id it happens to carry right now
        "rewrite the parser",
        "Working on it…",  # it HAS already spoken, which is what defeated the old guard
        TokenUsage(total=40, output=40),
        None,
        complete=False,
        started_at=1000,
        ended_at=1200,
    )
    exported = ExportedSession("ses-mid", "m", None, [midflight])
    engine, state, commits, commit_fn = _make_finish_helpers(tmp_path, session, exported)
    state.data["backend_session_id"] = "ses-mid"

    result, _ = engine.finish_parse_if_ready(
        session=session,
        quiet=True,
        prompt_untracked=False,
        require_complete=False,  # the stop finalize: capture the in-flight turn
        awaited_followups=[],
        agent_is_active_fn=lambda: False,
        debug_fn=lambda *a, **k: None,
        note_session_change_fn=lambda sid: None,
        mirror_fn=lambda sid: None,
        commit_fn=commit_fn,
    )
    assert result is True
    assert state.backend_message_id_for("ses-mid") == "u1"  # stable for the turn's whole life
    assert state.partial_turn_usage()["user_id"] == "u1"  # so tokens are counted exactly once

    # The turn finishes under the restarted tracker, with a DIFFERENT assistant id. It must
    # still be exported (and so committed), carrying the user's prompt with it.
    finished = SessionTurn(
        "u1",
        "msg_final",
        "rewrite the parser",
        "Done.",
        TokenUsage(total=90, output=90),
        None,
        started_at=1000,
        ended_at=1500,
    )
    later = ExportedSession("ses-mid", "m", None, [finished])
    remaining = turns_after(
        later,
        state.backend_message_id_for("ses-mid"),
        marked_at=state.backend_message_marked_at_for("ses-mid"),
    )
    assert [t.user_prompt for t in remaining] == ["rewrite the parser"]
    # Only the delta is added on the re-commit, so the re-export cannot inflate the tokens.
    engine._add_turn_usage(finished)
    assert state.pending_token_usage()["output"] == 50


def test_a_conversations_own_lost_watermark_still_reaches_the_bounded_fallback(tmp_path):
    # Guard for the guard. Bug A's fix makes a conversation with no mark of its own report
    # None (all turns new). It must NOT also blank a mark that genuinely BELONGS to this
    # conversation but has stopped matching a boundary (a compaction reshaping turns) —
    # that watermark has to survive so turns_after still reaches its bounded fallback. Blanking
    # it would re-export the whole conversation, which is exactly the 31M-token 20-day commit
    # of 2026-07-25 that test_lost_watermark_never_reexports_the_whole_session pins.
    state = AgitrackState(tmp_path)
    state.data["backend_session_id"] = "ses-own"
    state.set_backend_message_id("ses-own", "msg_now_vanished", marked_at=500)

    assert state.backend_message_id_for("ses-own") == "msg_now_vanished"
    assert state.backend_message_marked_at_for("ses-own") == 500

    old = SessionTurn("u1", "a1", "ancient", "done", TokenUsage(output=1_000_000), None, started_at=100, ended_at=200)
    newer = SessionTurn("u2", "a2", "recent", "done", TokenUsage(output=10), None, started_at=900, ended_at=950)
    exported = ExportedSession("ses-own", "m", None, [old, newer])
    remaining = turns_after(
        exported,
        state.backend_message_id_for("ses-own"),
        marked_at=state.backend_message_marked_at_for("ses-own"),
    )
    assert remaining == [newer]  # bounded by time — the ancient turn is never re-exported


def test_turns_ended_before_the_frontier_are_never_recounted(tmp_path):
    # Belt to the turns_after fix: even if some path re-exports history, a turn that
    # ENDED at or before the watermark's recorded time adds NOTHING — only the
    # partial-delta continuation (which ends after the frontier) may reach back.
    state = AgitrackState(tmp_path)
    state.data["backend_session_id"] = "s1"
    state.set_backend_message_id("s1", "a5", marked_at=5000)
    engine = CommitEngine(_Repo(staged=True), state)

    ancient = SessionTurn("u1", "a1", "old", "done", TokenUsage(output=1_000_000), None, started_at=100, ended_at=200)
    engine._add_turn_usage(ancient)
    assert state.pending_token_usage()["output"] == 0  # skipped entirely

    fresh = SessionTurn("u9", "a9", "new", "done", TokenUsage(output=30), None, started_at=5100, ended_at=5200)
    engine._add_turn_usage(fresh)
    assert state.pending_token_usage()["output"] == 30


def test_a_backend_dialog_keystroke_never_enters_the_commit_trace():
    """Claude and Codex both ask a trust question whose answer is a single keystroke, and the
    backend records that keystroke as a user message. aGiTrack copied it into the interaction
    trace, so every --no-worktree commit carried a stray `## User` / `1` — permanently, in
    history, across three independent live scenarios. The dialog also overlaps aGiTrack's own
    startup popup, so anything typed in that window was committed forever."""
    from agitrack.proxy.commit_engine import _is_dialog_keystroke
    from agitrack.backends.base import TokenUsage
    from agitrack.transcripts.types import SessionTurn

    answer = SessionTurn(
        user_message_id="u1",
        assistant_message_id="",
        user_prompt="1",
        final_response="",
        tokens=TokenUsage(),
        model=None,
    )
    assert _is_dialog_keystroke(answer) is True


def test_a_real_prompt_is_never_mistaken_for_a_dialog_answer():
    from agitrack.backends.base import TokenUsage
    from agitrack.proxy.commit_engine import _is_dialog_keystroke
    from agitrack.transcripts.types import SessionTurn

    # A short prompt the agent actually answered.
    answered = SessionTurn(
        user_message_id="u1",
        assistant_message_id="a1",
        user_prompt="1",
        final_response="Done.",
        tokens=TokenUsage(),
        model=None,
    )
    assert _is_dialog_keystroke(answered) is False

    # Anything longer than one character.
    real = SessionTurn(
        user_message_id="u2",
        assistant_message_id="",
        user_prompt="go",
        final_response="",
        tokens=TokenUsage(),
        model=None,
    )
    assert _is_dialog_keystroke(real) is False

    # A single character that is not a menu answer.
    other = SessionTurn(
        user_message_id="u3",
        assistant_message_id="",
        user_prompt="?",
        final_response="",
        tokens=TokenUsage(),
        model=None,
    )
    assert _is_dialog_keystroke(other) is False


def test_a_keystroke_turn_that_produced_edits_is_kept():
    """Whatever it says, a turn the agent acted on is never discarded."""
    from agitrack.proxy.commit_engine import _is_dialog_keystroke
    from agitrack.backends.base import TokenUsage
    from agitrack.transcripts.types import FileEdit, SessionTurn

    turn = SessionTurn(
        user_message_id="u1",
        assistant_message_id="",
        user_prompt="2",
        final_response="",
        tokens=TokenUsage(),
        model=None,
        edits=[FileEdit(path="a.txt", insertions=1, deletions=0, patch="")],
    )
    assert _is_dialog_keystroke(turn) is False


class _RepoWithIndex(_Repo):
    """A _Repo that can answer ``staged_paths()`` the way GitRepo does.

    The base fake uses that name for a plain list of what ``stage_paths`` recorded, which is
    a different thing; this subclass models the real method — the index the commit will take.
    """

    def __init__(self, paths: list[str]) -> None:
        super().__init__(staged=True)
        self._index = list(paths)
        # The base sets `staged_paths` as an INSTANCE attribute, which would shadow the
        # method below (instance attributes win over class ones); drop it so it does not.
        del self.staged_paths

    def staged_paths(self) -> list[str]:  # type: ignore[override]
        return list(self._index)


def test_commit_turns_tells_an_interrupted_commit_what_it_carries(tmp_path):
    """The trace of a cancelled turn stops at the agent's "I'll do X" and never reaches the
    edits, so the summarizer read the commit as having written nothing — over a diff of
    eight files. The commit states its own contents instead."""
    repo = _RepoWithIndex(["f1.txt", "f2.txt"])
    engine = CommitEngine(repo, AgitrackState(tmp_path))

    engine.commit_turns(
        turns=[
            SessionTurn(
                "u1",
                "a1",
                "Create ten files",
                "I'll create them one at a time.",
                TokenUsage(total=1, output=1),
                None,
                complete=True,
                interrupted=True,
            )
        ],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
    )

    assert repo.message is not None
    unwrapped = " ".join(repo.message.replace("> ", "").split())
    assert "it changes f1.txt, f2.txt." in unwrapped


def test_commit_turns_leaves_a_completed_turn_message_alone(tmp_path):
    """Only the interrupted case reads the index; every other commit keeps the interaction
    trace as the summarizer's sole input."""
    repo = _RepoWithIndex(["f1.txt"])
    engine = CommitEngine(repo, AgitrackState(tmp_path))

    engine.commit_turns(
        turns=[_turn("Create a file", "Created it.")],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
    )

    assert repo.message is not None and "f1.txt" not in repo.message


def test_a_repo_that_cannot_list_its_index_still_commits(tmp_path):
    """Best-effort: the note simply loses its file list rather than the commit failing."""
    engine, repo, _state = _engine(tmp_path)  # base _Repo: staged_paths is a list, not callable

    engine.commit_turns(
        turns=[
            SessionTurn(
                "u1", "a1", "do it", "starting", TokenUsage(total=1, output=1), None, complete=True, interrupted=True
            )
        ],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
    )

    assert repo.message is not None and "NOT completed" in repo.message


def test_the_latent_path_reports_its_own_changes_not_the_index(tmp_path):
    """Every `--no-worktree` mode records a hidden latent commit from a working-tree snapshot
    and never stages anything, so reading the index answered "nothing changed" for the mode
    most people run — and the interrupted note lost its file list exactly where it mattered."""
    repo = _RepoWithIndex([])  # nothing staged: the latent path never touches the index
    engine = CommitEngine(repo, AgitrackState(tmp_path))
    recorded: list[str] = []

    engine.commit_turns(
        turns=[
            SessionTurn(
                "u1", "a1", "do it", "starting", TokenUsage(total=1, output=1), None, complete=True, interrupted=True
            )
        ],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
        manual_gate_fn=lambda: True,
        manual_record_fn=lambda message: (recorded.append(message), "abc1234")[1],
        changed_paths_fn=lambda: ["latent1.txt", "latent2.txt"],
    )

    assert recorded, "the turn should have been recorded latently"
    unwrapped = " ".join(recorded[0].replace("> ", "").split())
    assert "it changes latent1.txt, latent2.txt." in unwrapped
