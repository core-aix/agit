import pytest

from agitrack.commits import (
    build_agent_commit_message,
    build_user_commit_message,
    carries_ai_history,
    render_interaction_trace,
)
from agitrack.commits.message import (
    PATH_MASK,
    SECRET_MASK,
    TRACE_EVENT_ROLE,
    apply_summary_to_message,
    mask_paths,
)
from agitrack.commits.message import _mask_secrets as _mask_secrets_for_test


def test_render_interaction_trace_matches_committed_trace_and_masks_secrets():
    # The summarizer's sole input is this rendered trace; it must be the same
    # "## User"/"## Agent" body that lands in the commit, with secrets masked.
    trace = [
        {"role": "user", "content": "ship it, key is sk-ant-api03-SECRETSECRETSECRETSECRET"},
        {"role": "agent", "content": "Shipped it."},
    ]
    rendered = render_interaction_trace(trace, trace_turn_limit=10)
    assert rendered.startswith("## User")
    assert "## Agent\n\nShipped it." in rendered
    assert "SECRETSECRETSECRETSECRET" not in rendered  # masked

    # It is exactly the body the commit carries under "# Interaction Trace".
    message = build_agent_commit_message(
        latest_prompt="ship it",
        trace=trace,
        backend="claude",
        backend_session_id="ses-1",
        agitrack_session_id="agit-1",
        model="m",
    )
    committed = message.split("# Interaction Trace\n\n", 1)[1].split("\n# aGiTrack Metadata", 1)[0].strip()
    assert committed == rendered


def test_render_interaction_trace_respects_turn_limit():
    # A turn is an EXCHANGE: it takes the agent's reply to close one, so these are five turns
    # rather than five messages. (Five bare user entries in a row are ONE turn — see
    # test_messages_sent_back_to_back_are_one_turn_not_several.)
    trace = []
    for i in range(5):
        trace.append({"role": "user", "content": f"turn {i}"})
        trace.append({"role": "agent", "content": f"answer {i}"})
    rendered = render_interaction_trace(trace, trace_turn_limit=2)
    # Only the most recent 2 turns are kept (same limiting the commit applies).
    assert "turn 4" in rendered and "turn 3" in rendered
    assert "turn 0" not in rendered


def test_a_background_event_is_never_rendered_as_a_user_message():
    # THE BUG: the harness wakes the agent when a task it backgrounded reports back, and the
    # parser labels that turn with a synthetic prompt. Recorded under the `user` role it became
    # a "## User" / "(background task completed)" block — indistinguishable from something the
    # user typed — in every such commit. It is an event, and the trace is also the summarizer's
    # sole input, so it must not read as a request.
    trace = [
        {"role": "user", "content": "run the sweep in the background"},
        {"role": "agent", "content": "Started it."},
        {"role": TRACE_EVENT_ROLE, "content": "(background task completed)"},
        {"role": "agent", "content": "Sweep finished: 62.5% accuracy."},
    ]
    rendered = render_interaction_trace(trace, trace_turn_limit=10)

    assert rendered.count("## User") == 1  # the real prompt, and only it
    assert "## User\n\n(background task completed)" not in rendered
    # Still present — it explains a turn nobody asked for — but plainly as an event. The note
    # is a wrapped blockquote, so assert against its de-wrapped text rather than raw lines.
    note = " ".join(line.lstrip("> ") for line in rendered.splitlines() if line.startswith(">"))
    assert "woken here by a background event (background task completed), not by a user" in note


def test_a_background_event_starts_a_turn_for_the_trace_limit():
    # A wake-up is bounded like anything else: it may evict an OLDER wake-up, which is the one
    # thing a burst of them should cost. Without this a monitor ticking for an hour keeps every
    # tick in the trace, since none of those turns is committed on its own.
    trace = []
    for i in range(5):
        trace.append({"role": TRACE_EVENT_ROLE, "content": "(background monitor update)"})
        trace.append({"role": "agent", "content": f"tick {i}"})
    rendered = render_interaction_trace(trace, trace_turn_limit=2)

    assert "tick 4" in rendered and "tick 3" in rendered
    assert "tick 0" not in rendered


def test_background_wake_ups_never_evict_the_prompt_that_asked_for_the_work():
    """THE BUG, measured on a real commit: the turn limit counted a background wake-up as a turn,
    so five of them spent the whole budget of 5 and the session's opening instruction — "check
    the dependabot PRs and merge if there are no issues" — was evicted from its own commit.

    The wake-ups were timers the agent had queued while waiting on a slow CI job, and every one
    of them was answered with a sentence saying it was still waiting. Nobody asked for those
    turns; they cannot be what a trace of five turns is spent on. A turn is a user-agent PAIR."""
    trace = [
        {"role": "user", "content": "the instruction that started the work"},
        {"role": "agent", "content": "on it"},
    ]
    for i in range(5):
        trace.append({"role": TRACE_EVENT_ROLE, "content": "(background task completed)"})
        trace.append({"role": "agent", "content": f"still waiting {i}"})
    trace.append({"role": "user", "content": "a second instruction"})
    trace.append({"role": "agent", "content": "done"})

    rendered = render_interaction_trace(trace, trace_turn_limit=5)

    assert "the instruction that started the work" in rendered
    assert "a second instruction" in rendered
    assert "still waiting 0" in rendered  # seven turns, but only two of them are pairs


def test_a_storm_of_wake_ups_is_trimmed_around_the_prompt_it_follows():
    # Both budgets apply at once: the pair survives whole, and the wake-ups it drags behind it
    # are cut to the most recent `trace_turn_limit` — bounded, without costing the prompt.
    trace = [{"role": "user", "content": "start the long watch"}, {"role": "agent", "content": "watching"}]
    for i in range(20):
        trace.append({"role": TRACE_EVENT_ROLE, "content": "(background monitor update)"})
        trace.append({"role": "agent", "content": f"tick {i}"})

    rendered = render_interaction_trace(trace, trace_turn_limit=2)

    assert "start the long watch" in rendered
    assert "tick 19" in rendered and "tick 18" in rendered
    assert "tick 17" not in rendered and "tick 0" not in rendered


def test_a_wake_up_older_than_every_surviving_prompt_is_dropped_with_it():
    # An event note reads as context for the exchange around it, so one left stranded above the
    # oldest surviving prompt would explain a turn that is no longer in the trace.
    trace = [
        {"role": TRACE_EVENT_ROLE, "content": "(background task completed)"},
        {"role": "agent", "content": "ancient"},
    ]
    for i in range(4):
        trace.append({"role": "user", "content": f"prompt {i}"})
        trace.append({"role": "agent", "content": f"answer {i}"})

    rendered = render_interaction_trace(trace, trace_turn_limit=2)

    assert "ancient" not in rendered
    assert "prompt 3" in rendered and "prompt 2" in rendered
    assert "prompt 1" not in rendered


def test_a_message_queued_mid_turn_does_not_count_as_a_turn_of_its_own():
    """THE BUG: the trace limiter counted every `## User` block as a turn, but a message the user
    queues while the agent is working is not one — the agent is already answering the turn it
    belongs to.

    Measured on a real session: one turn carried ELEVEN queued messages, so a limit of 5 cut the
    trace INSIDE that turn. Its opening prompt and first eight follow-ups were dropped, and
    because that turn's work was committed here, the words that asked for it ended up in no
    commit at all — the trace began mid-conversation with the ninth thing the user said."""
    trace = [{"role": "user", "content": "the opening prompt"}]
    for i in range(11):
        trace.append({"role": "user", "content": f"queued {i}", "starts_turn": False})
    trace.append({"role": "agent", "content": "one answer covering all of it"})
    trace.append({"role": "user", "content": "a genuinely new turn"})
    trace.append({"role": "agent", "content": "and its answer"})

    rendered = render_interaction_trace(trace, trace_turn_limit=2)

    # Two turns fit the limit, and each is kept WHOLE.
    assert "the opening prompt" in rendered
    assert all(f"queued {i}" in rendered for i in range(11))
    assert "a genuinely new turn" in rendered


def test_messages_sent_back_to_back_are_one_turn_not_several():
    """A turn is an exchange, and it is the agent's REPLY that ends one.

    The user can keep typing while the agent works — a correction, an afterthought, a second
    question — and none of that is a new turn: no answer came between them. Treating each as one
    let a handful of quick messages evict everything the trace was supposed to keep, and cut a
    conversation off mid-way through what the user was saying.

    This holds regardless of how the messages were recorded, which is the point of deriving the
    boundary from the trace: the run collapses even without the `starts_turn` marker, so a
    recording path that does not set it (or an entry written by an older install) still reads as
    one turn."""
    trace = [
        {"role": "user", "content": "first thought"},
        {"role": "user", "content": "second thought"},
        {"role": "user", "content": "third thought"},
        {"role": "agent", "content": "one answer to all three"},
        {"role": "user", "content": "a real follow-up turn"},
        {"role": "agent", "content": "answered"},
    ]

    # Two turns, so a limit of 2 keeps everything...
    rendered = render_interaction_trace(trace, trace_turn_limit=2)
    assert all(t in rendered for t in ("first thought", "second thought", "third thought"))
    assert "a real follow-up turn" in rendered

    # ...and a limit of 1 drops the first exchange WHOLE, never part of it.
    rendered = render_interaction_trace(trace, trace_turn_limit=1)
    assert not any(t in rendered for t in ("first thought", "second thought", "third thought"))
    assert "a real follow-up turn" in rendered


def test_a_turn_that_is_over_the_limit_is_still_dropped_whole():
    # The limit still bites — it just counts turns. Follow-ups ride with the turn they continue,
    # so an evicted turn takes its own queued messages with it and never leaves them orphaned
    # under a later turn's heading.
    trace = []
    for turn in range(4):
        trace.append({"role": "user", "content": f"turn {turn}"})
        trace.append({"role": "user", "content": f"turn {turn} followup", "starts_turn": False})
        trace.append({"role": "agent", "content": f"answer {turn}"})

    rendered = render_interaction_trace(trace, trace_turn_limit=2)

    assert "turn 3" in rendered and "turn 3 followup" in rendered
    assert "turn 2" in rendered and "turn 2 followup" in rendered
    assert "turn 1" not in rendered and "turn 1 followup" not in rendered


def test_render_interaction_trace_drops_empty_role_entries():
    # A blank/whitespace entry (e.g. a garbled or empty proxy capture of a follow-up typed while
    # the agent was busy) must not render as a bare "## User" heading with nothing under it.
    trace = [
        {"role": "user", "content": "Continue"},
        {"role": "user", "content": "   "},
        {"role": "user", "content": ""},
        {"role": "agent", "content": "done"},
    ]
    rendered = render_interaction_trace(trace, trace_turn_limit=10)
    assert rendered.count("## User") == 1 and "Continue" in rendered
    assert rendered.count("## Agent") == 1


def test_agent_commit_message_records_reasoning_effort_when_given():
    message = build_agent_commit_message(
        latest_prompt="fix it",
        trace=[{"role": "user", "content": "fix it"}],
        backend="opencode",
        backend_session_id="ses-1",
        agitrack_session_id="agit-1",
        model="provider/model",
        reasoning_effort="high",
    )

    assert "model: provider/model\nreasoning_effort: high\n" in message


def test_agent_commit_message_omits_reasoning_effort_when_absent():
    message = build_agent_commit_message(
        latest_prompt="fix it",
        trace=[{"role": "user", "content": "fix it"}],
        backend="opencode",
        backend_session_id="ses-1",
        agitrack_session_id="agit-1",
        model="provider/model",
    )

    assert "reasoning_effort:" not in message


def test_agent_commit_message_records_conversation_anchor_when_given():
    message = build_agent_commit_message(
        latest_prompt="fix it",
        trace=[{"role": "user", "content": "fix it"}],
        backend="claude",
        backend_session_id="ses-1",
        agitrack_session_id="agit-1",
        model="provider/model",
        conversation_anchor="msg-abc",
    )

    # The anchor sits with the session identity so a reader can locate the
    # exact place in the transcript referenced by this commit.
    assert "backend_session_id: ses-1\nconversation_anchor: msg-abc\n" in message


def test_agent_commit_message_omits_conversation_anchor_when_absent():
    message = build_agent_commit_message(
        latest_prompt="fix it",
        trace=[{"role": "user", "content": "fix it"}],
        backend="claude",
        backend_session_id="ses-1",
        agitrack_session_id="agit-1",
        model="provider/model",
    )

    assert "conversation_anchor:" not in message


def test_agent_commit_message_contains_trace_and_metadata():
    message = build_agent_commit_message(
        latest_prompt="fix it",
        trace=[{"role": "user", "content": "fix it"}, {"role": "agent", "content": "fixed"}],
        backend="opencode",
        backend_session_id="ses-1",
        agitrack_session_id="agit-1",
        model="provider/model",
        token_usage={
            "context": 100,
            "total": 40,
            "input": 130,
            "output": 10,
            "reasoning": 0,
            "cache_read": 20,
            "cache_write": 5,
        },
    )

    assert message.startswith("<aGiTrack> fix it")
    assert "# Interaction Trace" in message
    assert "## User\n\nfix it" in message
    assert "## Agent\n\nfixed" in message
    assert "# aGiTrack Metadata" in message
    assert "backend: opencode" in message
    assert "backend_session_id: ses-1" in message
    assert "context_tokens: 100" in message
    # Input counts uncached input PLUS cache-creation tokens (130 + 5): cache
    # writes are fresh input processed once (#14). Cache reads stay separate.
    assert "tokens_since_last_commit_input: 135" in message
    assert "tokens_since_last_commit_output: 10" in message
    assert "tokens_since_last_commit_cache_read: 20" in message
    assert "tokens_since_last_commit_cache_write: 5" in message
    assert "tokens_since_last_commit_total" not in message
    assert "tokens_since_last_commit_subagent_input" not in message
    assert "token_note" not in message


def test_trace_message_headings_are_pushed_down_two_levels():
    # Every heading inside a trace message moves down by exactly two: `#` → `###`,
    # `##` → `####`. A UNIFORM shift, not a re-basing of the shallowest heading to some
    # floor — the levels are the message author's own and must survive as written. Two
    # levels rather than one because one nested correctly but did not READ correctly: in
    # plain-text `git log` a message's sections sat a single '#' from the role scaffolding,
    # so telling the trace's structure from the conversation's content meant counting hashes.
    message = build_agent_commit_message(
        latest_prompt="do it",
        trace=[
            {"role": "user", "content": "# Big ask\nplease\n## Detail\nmore"},
            {"role": "agent", "content": "### Already deep\nkept relative"},
        ],
        backend="claude",
        backend_session_id="ses-1",
        agitrack_session_id="agit-1",
        model="m",
    )
    assert "## User\n\n### Big ask" in message  # level 1 -> 3, never outranking the role
    assert "#### Detail" in message  # level 2 -> 4
    # The original level-1/level-2 headings must no longer appear as such.
    assert "\n# Big ask" not in message
    assert "\n## Detail" not in message
    # A message that already nested deeper moves by the SAME two levels; it is not re-based
    # onto the same floor as the message above, so its author's relative depth is preserved.
    assert "## Agent\n\n##### Already deep" in message


def test_a_deeply_nested_message_heading_is_capped_at_six():
    # Markdown has no level 7. A message already nesting near the bottom loses some depth
    # there rather than the shift being abandoned — the rare case, and preferable to
    # re-basing every message's headings onto a fixed floor.
    message = build_agent_commit_message(
        latest_prompt="x",
        trace=[{"role": "agent", "content": "##### Five\nbody\n###### Six"}],
        backend="claude",
        backend_session_id="ses-1",
        agitrack_session_id="agit-1",
        model="m",
    )

    assert "## Agent\n\n###### Five" in message
    assert "####### " not in message  # never emits an invalid level-7 heading


def test_trace_heading_nesting_skips_fenced_code_comments():
    # A leading '#' inside a fenced code block is CONTENT BEING QUOTED — a shell comment, or
    # a Markdown example — not a heading of the message. It must be left untouched even while
    # real headings around it are shifted: rewriting it would falsify what the message showed.
    # (This is also why a trace can still contain a literal "## User" line: quoted, inside a
    # fence, where it is code rather than structure.)
    message = build_agent_commit_message(
        latest_prompt="x",
        trace=[{"role": "agent", "content": "# Heading\n```sh\n# just a comment\n```"}],
        backend="claude",
        backend_session_id="ses-1",
        agitrack_session_id="agit-1",
        model="m",
    )
    assert "### Heading" in message
    assert "# just a comment" in message  # the comment kept its single '#'


def test_commit_message_masks_secrets_in_subject_and_trace():
    message = build_agent_commit_message(
        latest_prompt="use api_key=sk-abc12345678901234567890",
        trace=[
            {"role": "user", "content": "password=hunter2"},
            {"role": "agent", "content": "token: ghp_abcdefghijklmnopqrstuvwxyz"},
        ],
        backend="opencode",
        backend_session_id="ses-1",
        agitrack_session_id="agit-1",
        model="provider/model",
    )

    assert "hunter2" not in message
    assert "sk-abc" not in message
    assert "ghp_" not in message
    assert "[REDACTED]" in message


def test_commit_message_removes_mouse_coordinate_reports():
    message = build_agent_commit_message(
        latest_prompt="fix [<35;59;45M[<35;59;44M bug",
        trace=[{"role": "user", "content": "[<35;60;43M[<0;42;39m keep text"}],
        backend="opencode",
        backend_session_id="ses-1",
        agitrack_session_id="agit-1",
        model="provider/model",
    )

    assert "[<35;" not in message
    assert "[<0;" not in message
    assert "keep text" in message


def test_commit_message_removes_a_malformed_mouse_report_too():
    """Real bug: a mouse report the proxy's own capture failed to recognize as complete (cut
    off before its M/m terminator, or otherwise corrupted in flight) left a raw fragment like
    "[<35;124;48" sitting in a commit's interaction trace looking exactly like something the
    user had typed. The proxy capture is fixed separately (`_escape_sequence_complete` now
    aborts a report the moment a byte outside {digit, ';'} shows up), but the commit-message
    sanitizer must not depend on the capture being perfect: a fragment reaching it from
    anywhere else is still not something a real prompt would ever start with."""
    message = build_agent_commit_message(
        latest_prompt="fix it",
        trace=[{"role": "user", "content": "[<35;124;48/model please, keep this text"}],
        backend="claude",
        backend_session_id="ses-1",
        agitrack_session_id="agit-1",
        model="provider/model",
    )
    assert "35;124;48" not in message
    assert "keep this text" in message
    assert "please" in message


def test_agent_commit_message_preserves_user_trace_order():
    message = build_agent_commit_message(
        latest_prompt="fix it",
        trace=[
            {"role": "user", "content": "fix it"},
            {"role": "agent", "content": "working"},
            {"role": "user", "content": "also handle errors"},
            {"role": "agent", "content": "done"},
        ],
        backend="opencode",
        backend_session_id="ses-1",
        agitrack_session_id="agit-1",
        model="provider/model",
    )

    assert message.index("## User\n\nfix it") < message.index("## User\n\nalso handle errors")


def test_agent_commit_message_omits_zero_reasoning():
    message = build_agent_commit_message(
        latest_prompt="fix it",
        trace=[],
        backend="opencode",
        backend_session_id="ses-1",
        agitrack_session_id="agit-1",
        model="provider/model",
        token_usage={
            "context": 100,
            "total": 5,
            "input": 100,
            "output": 5,
            "reasoning": 0,
            "cache_read": 0,
            "cache_write": 0,
        },
    )

    assert "tokens_since_last_commit_input: 100" in message
    assert "tokens_since_last_commit_reasoning" not in message


def test_agent_commit_message_includes_nonzero_reasoning():
    message = build_agent_commit_message(
        latest_prompt="fix it",
        trace=[],
        backend="opencode",
        backend_session_id="ses-1",
        agitrack_session_id="agit-1",
        model="provider/model",
        token_usage={
            "context": 100,
            "total": 11,
            "input": 100,
            "output": 5,
            "reasoning": 6,
            "cache_read": 0,
            "cache_write": 0,
        },
    )

    assert "tokens_since_last_commit_reasoning: 6" in message


def test_first_run_input_counts_cache_creation_tokens():
    # First commit in a fresh repo (#14): the backend reports almost the whole
    # context as cache_creation_input_tokens and only a sliver as input_tokens.
    # All of it was processed as input exactly once, so the input line must
    # reflect input + cache_write — not look near zero next to the cache.
    message = build_agent_commit_message(
        latest_prompt="fix it",
        trace=[],
        backend="claude",
        backend_session_id="ses-1",
        agitrack_session_id="agit-1",
        model="m",
        token_usage={
            "context": 18250,
            "total": 200,
            "input": 250,
            "output": 200,
            "reasoning": 0,
            "cache_read": 0,
            "cache_write": 18000,
        },
    )

    assert "tokens_since_last_commit_input: 18250" in message
    assert "tokens_since_last_commit_cache_write: 18000" in message
    assert "tokens_since_last_commit_cache_read" not in message


def test_subagent_input_counts_subagent_cache_creation_tokens():
    message = build_agent_commit_message(
        latest_prompt="fix it",
        trace=[],
        backend="claude",
        backend_session_id="ses-1",
        agitrack_session_id="agit-1",
        model="m",
        token_usage={
            "context": 100,
            "total": 5,
            "input": 100,
            "output": 5,
            "reasoning": 0,
            "cache_read": 0,
            "cache_write": 0,
            "subagent_input": 40,
            "subagent_output": 60,
            "subagent_reasoning": 0,
            "subagent_cache_read": 0,
            "subagent_cache_write": 500,
        },
    )

    assert "tokens_since_last_commit_subagent_input: 540" in message
    assert "tokens_since_last_commit_subagent_cache_write: 500" in message


def test_agent_commit_message_records_subagent_token_categories():
    message = build_agent_commit_message(
        latest_prompt="fix it",
        trace=[],
        backend="claude",
        backend_session_id="ses-1",
        agitrack_session_id="agit-1",
        model="claude-opus-4-8",
        token_usage={
            "context": 100,
            "total": 5,
            "input": 100,
            "output": 5,
            "reasoning": 0,
            "cache_read": 0,
            "cache_write": 0,
            "subagent_input": 40,
            "subagent_output": 60,
            "subagent_reasoning": 0,
            "subagent_cache_read": 700,
            "subagent_cache_write": 0,
        },
    )

    assert "tokens_since_last_commit_subagent_input: 40" in message
    assert "tokens_since_last_commit_subagent_output: 60" in message
    assert "tokens_since_last_commit_subagent_cache_read: 700" in message
    # Zero-valued sub-agent categories stay out of the metadata.
    assert "tokens_since_last_commit_subagent_reasoning" not in message
    assert "tokens_since_last_commit_subagent_cache_write" not in message


def test_user_commit_message_rejects_blank_message():
    with pytest.raises(ValueError, match="required"):
        build_user_commit_message(message="", agitrack_session_id="agit-1")


def test_user_commit_message_uses_required_message_subject():
    message = build_user_commit_message(message="save work", agitrack_session_id="agit-1")

    assert message.startswith("save work")
    assert "User Message:" not in message
    assert "# aGiTrack Metadata" in message


def test_user_commit_message_masks_secret_subject():
    message = build_user_commit_message(message="save password=secret123", agitrack_session_id="agit-1")

    assert message.startswith("save password=[REDACTED]")
    assert "secret123" not in message


def test_commit_messages_include_current_agitrack_version_without_created_at():
    from agitrack import __version__

    message = build_user_commit_message(message="save work", agitrack_session_id="agit-1")

    assert f"agitrack_version: {__version__}" in message
    assert "created_at" not in message


def test_commit_metadata_stamps_the_running_version_not_stale_install_metadata():
    """The version in a commit is the version of the aGiTrack that MADE it.

    This asserted ``__version__ == importlib.metadata.version("agitrack")`` on the premise that
    ``__version__`` derives from installed distribution metadata. It does not, and deliberately
    not: :func:`agitrack._resolve_version` prefers the source checkout's ``pyproject.toml``
    PRECISELY BECAUSE the two disagree — an editable install freezes its ``.dist-info`` at
    install time, so from the moment a release bumps the version until someone reinstalls, the
    metadata names a version older than the code that is running. (That is the bug the
    preference was introduced for: commits stamped 0.0.4 while pyproject said 0.0.6.)

    So the old assertion contradicted the design, and failed on exactly the everyday setup it
    was meant to protect — a maintainer's own checkout, 0.6.20 running against 0.6.19 in
    ``.dist-info`` — while saying nothing at all about commit messages when it did. What
    actually matters is asserted instead: the message carries the RUNNING version, and never the
    stale one."""
    from importlib.metadata import PackageNotFoundError, version

    from agitrack import __version__

    message = build_user_commit_message(message="save work", agitrack_session_id="agit-1")
    assert f"agitrack_version: {__version__}" in message

    try:
        installed = version("agitrack")
    except PackageNotFoundError:  # a source tree that was never installed
        installed = None
    if installed is not None and installed != __version__:
        assert f"agitrack_version: {installed}" not in message


def test_source_version_falls_back_to_pyproject_when_metadata_missing():
    # When distribution metadata is unavailable (e.g. an editable install whose
    # .dist-info went missing after a failed reinstall), the version must fall back
    # to the source checkout's pyproject.toml — the real release version — rather
    # than the 0.0.0 placeholder, so commit metadata stays accurate.
    import re
    from pathlib import Path

    from agitrack import _source_version

    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    expected = re.search(r'^version = "([^"]+)"', pyproject.read_text(), re.M).group(1)
    assert _source_version() == expected
    assert expected != "0.0.0"  # the checkout declares a real release version


def _git_subject(message: str) -> str:
    """What `git log --oneline` / `%s` shows: the first PARAGRAPH, newlines folded to
    spaces. Not just the first line — which is why a blank line has to follow the
    subject for the split at the first sentence to mean anything."""
    first_paragraph = message.split("\n\n", 1)[0]
    return " ".join(first_paragraph.split())


def test_agent_commit_subject_splits_at_first_sentence():
    # The subject is the first sentence; the rest flows onto the next line. aGiTrack
    # never adds "…" — Git shortens the displayed subject if it's long.
    message = build_agent_commit_message(
        latest_prompt="Fix the failing parser test. Then add coverage and update the docs.",
        trace=[],
        backend="opencode",
        backend_session_id="ses-1",
        agitrack_session_id="agit-1",
        model="provider/model",
    )

    lines = message.splitlines()
    assert lines[0] == "<aGiTrack> Fix the failing parser test."  # first sentence only
    assert "..." not in lines[0]
    # The remainder is BODY, one blank line down. Git's subject is the first PARAGRAPH
    # with newlines folded to spaces, so a continuation glued directly under the first
    # sentence put the whole paragraph back in `git log --oneline`.
    assert lines[1] == ""
    assert lines[2] == "Then add coverage and update the docs."
    assert _git_subject(message) == "<aGiTrack> Fix the failing parser test."


def test_agent_commit_subject_without_period_is_kept_whole():
    # No sentence-ending period ⇒ the whole prompt is the subject, untruncated, with
    # no "…" and no continuation line.
    message = build_agent_commit_message(
        latest_prompt="please " * 40,
        trace=[],
        backend="opencode",
        backend_session_id="ses-1",
        agitrack_session_id="agit-1",
        model="provider/model",
    )

    lines = message.splitlines()
    assert lines[0].startswith("<aGiTrack> please please")
    assert not lines[0].endswith("...")
    assert lines[1] == ""  # straight to the (blank then) trace — no subject continuation


def test_user_commit_subject_splits_at_first_sentence():
    message = build_user_commit_message(
        message="Save my parser edits. They also touch the lexer.", agitrack_session_id="agit-1"
    )

    lines = message.splitlines()
    assert lines[0] == "Save my parser edits."  # first sentence
    assert "..." not in lines[0]
    assert lines[1] == ""
    assert lines[2] == "They also touch the lexer."
    assert _git_subject(message) == "Save my parser edits."


def test_commit_message_body_lines_are_wrapped_to_72():
    message = build_agent_commit_message(
        latest_prompt="change it",
        trace=[{"role": "agent", "content": "a " * 80}],
        backend="opencode",
        backend_session_id="ses-1",
        agitrack_session_id="agit-1",
        model="provider/model",
    )

    body_lines = message.splitlines()[1:]
    assert all(len(line) <= 72 for line in body_lines)


def test_agent_commit_trace_is_limited_by_user_turns():
    trace = []
    for index in range(7):
        trace.extend(
            [
                {"role": "user", "content": f"user {index}"},
                {"role": "agent", "content": f"agent {index}"},
            ]
        )

    message = build_agent_commit_message(
        latest_prompt="change it",
        trace=trace,
        backend="opencode",
        backend_session_id="ses-1",
        agitrack_session_id="agit-1",
        model="provider/model",
        trace_turn_limit=5,
    )

    assert "## User\n\nuser 0" not in message
    assert "## User\n\nuser 1" not in message
    assert "## User\n\nuser 2" in message
    assert "## Agent\n\nagent 6" in message


def test_agent_commit_records_conversation_span_as_utc_iso():
    message = build_agent_commit_message(
        latest_prompt="build it",
        trace=[{"role": "user", "content": "build it"}],
        backend="claude",
        backend_session_id="ses-1",
        agitrack_session_id="agit-1",
        model="claude-opus-4-8",
        started_at=1_718_200_000,
        ended_at=1_718_200_123,
    )
    assert "agent_started_at: 2024-06-12T13:46:40Z" in message
    assert "agent_ended_at: 2024-06-12T13:48:43Z" in message


def test_agent_commit_omits_span_when_timestamps_absent():
    message = build_agent_commit_message(
        latest_prompt="build it",
        trace=[{"role": "user", "content": "build it"}],
        backend="claude",
        backend_session_id="ses-1",
        agitrack_session_id="agit-1",
        model="claude-opus-4-8",
    )
    assert "agent_started_at" not in message
    assert "agent_ended_at" not in message


def test_subject_strips_terminal_escape_sequences():
    # Arrow-key residue and SGR colour codes must never reach the subject.
    message = build_user_commit_message(message="fix \x1b[Bthe \x1b[31mparser\x1b[0m bug", agitrack_session_id="agit-1")
    subject = message.splitlines()[0]
    assert subject == "fix the parser bug"
    assert "\x1b" not in message


def test_agent_subject_strips_orphan_mouse_and_control_chars():
    message = build_agent_commit_message(
        latest_prompt="run \x07tests\x1b]0;title\x07 now",
        trace=[{"role": "user", "content": "run \x1b[Atests now"}],
        backend="claude",
        backend_session_id="ses-1",
        agitrack_session_id="agit-1",
        model="claude-opus-4-8",
    )
    subject = message.splitlines()[0]
    assert subject == "<aGiTrack> run tests now"
    assert "\x1b" not in message
    assert "\x07" not in message


def test_legitimate_bracketed_text_is_preserved():
    # Defensive escape stripping must not damage normal bracketed prose.
    message = build_user_commit_message(message="handle [Beta] flag and list[B]", agitrack_session_id="agit-1")
    assert message.splitlines()[0] == "handle [Beta] flag and list[B]"


def test_agent_merge_message_format():
    from agitrack.commits import build_agent_merge_message

    message = build_agent_merge_message(
        session_name="feature-x",
        base_branch="main",
        source_branch="agitrack/feature-x/t2",
        agitrack_session_id="agit-1",
        backend="claude",
        backend_session_id="ses-9",
        conflicting_commits="abc123 base edit",
    )
    assert message.splitlines()[0].startswith("<aGiTrack-merge> ")
    assert "commit_type: agent-merge" in message
    assert "session_name: feature-x" in message
    assert "source_branch: agitrack/feature-x/t2" in message
    assert "base_branch: main" in message
    assert "backend_session_id: ses-9" in message
    assert "abc123 base edit" in message


def _trace():
    return [{"role": "user", "content": "do it"}, {"role": "agent", "content": "done"}]


def _base_message(**kw):
    return build_agent_commit_message(
        latest_prompt="do it",
        trace=_trace(),
        backend="claude",
        backend_session_id="ses-1",
        agitrack_session_id="agitrack-1",
        model="claude-opus-4-8",
        token_usage={"context": 100, "input": 10, "output": 5},
        **kw,
    )


def test_compaction_recorded_in_trace_note_and_metadata():
    message = _base_message(compactions=2)
    assert "context_compactions: 2" in message
    # A human-readable note leads the interaction trace, before the turns.
    trace_idx = message.index("# Interaction Trace")
    note_idx = message.index("compacted 2 times")
    user_idx = message.index("## User")
    assert trace_idx < note_idx < user_idx
    assert "> " in message  # rendered as a blockquote


def test_no_compaction_emits_no_note_or_metadata():
    message = _base_message(compactions=0)
    assert "context_compactions" not in message
    assert "compacted" not in message


def test_fork_origin_recorded_in_trace_and_metadata():
    message = _base_message(
        origin_event={"kind": "fork", "source": "ses_parent", "source_name": "main", "collaborator": "", "at": 1}
    )
    assert "forked_from: ses_parent (main)" in message
    assert "copied_from" not in message
    assert "forked from 'main'" in message


def test_copy_origin_records_contributor_in_trace_and_metadata():
    message = _base_message(
        origin_event={
            "kind": "copy",
            "source": "ses_shared",
            "source_name": "feature-x",
            "collaborator": "alice+bob",
            "at": 1,
        }
    )
    assert "copied_from: ses_shared (feature-x)" in message
    assert "copied_from_contributor: alice+bob" in message
    assert "copied from alice+bob's shared session 'feature-x'" in message


# --- absolute filesystem paths are masked out of commit messages -------------------------------
# Commit messages get pushed and read by others; an absolute path leaks the account name and the
# machine's layout (~/Code/client-work/..., /home/alice/.ssh/config) for no benefit to the reader.
# Every absolute path is masked. RELATIVE paths are kept on purpose: "paper/main.tex" says which
# file changed and reveals nothing about the machine.


@pytest.mark.parametrize(
    "text",
    [
        "Check this repository ~/metacognition-compression.",
        "Edited ~/metacognition-compression/paper/main.tex",
        "/home/shiqiangwang/agitrack/agitrack/proxy/runner.py:11001",
        "see ~/Code/metacognition-compression for context",
        "Read /home/alice/.ssh/config",
        "/Users/alice/Desktop/notes.md",
        "ran /usr/bin/python3 -m pytest",
        "cat /etc/hosts",
        "log at /tmp/build.log",
        r"C:\Users\alice\project\main.py",
        "C:/Users/alice/project/main.py",
        r"\\fileserver\share\docs\spec.md",
        "~alice/notes",
    ],
)
def test_absolute_paths_are_masked(text):
    masked = mask_paths(text)
    assert PATH_MASK in masked
    for leak in ("shiqiangwang", "alice", "/home/", "/Users/", "~/", "/etc/", "/tmp/", "fileserver"):
        assert leak not in masked, f"{leak!r} survived in {masked!r}"


@pytest.mark.parametrize(
    "text",
    [
        # Relative paths are the useful, non-identifying case — always kept.
        "Edited paper/main.tex",
        "src/agitrack/proxy/runner.py needs work",
        # URLs are not filesystem paths.
        "https://github.com/core-aix/agitrack",
        "http://agitrack.core-aix.org/ is the site",
        "git@github.com:core-aix/agitrack.git",
        # Backend slash commands look like one-segment absolute paths but are not; they appear
        # verbatim in prompts and commit subjects and must survive.
        "/compact",
        "ran /model then /clear",
        # Ordinary prose containing slashes.
        "use and/or here",
        "input/output/buffer sizes",
        "24/7 uptime",
        "see 2026/07/21 notes",
    ],
)
def test_non_paths_are_left_alone(text):
    assert mask_paths(text) == text


def test_sentence_punctuation_survives_masking():
    # "see ~/notes." keeps its full stop: the trailing '.' ends the sentence, not the path.
    assert mask_paths("see ~/notes.") == f"see {PATH_MASK}."
    assert mask_paths("(see /etc/hosts)") == f"(see {PATH_MASK})"
    assert mask_paths("~/a, ~/b; ~/c") == f"{PATH_MASK}, {PATH_MASK}; {PATH_MASK}"


def test_paths_are_masked_throughout_a_built_commit_message():
    message = build_agent_commit_message(
        latest_prompt="Check ~/proj and fix /home/alice/proj/app.py",
        trace=[
            {"role": "user", "content": "Look at ~/Code/notes/todo.md and /etc/hosts"},
            {
                "role": "agent",
                "content": "Edited paper/main.tex; read /home/alice/.ssh/config.\nSee https://github.com/core-aix/agitrack",
            },
        ],
        backend="claude",
        backend_session_id="ses-1",
        agitrack_session_id="agit-1",
        model="m1",
        session_name="s1",
    )
    assert message.startswith(f"<aGiTrack> Check {PATH_MASK} and fix {PATH_MASK}")
    assert "alice" not in message and "~/" not in message and "/etc/" not in message
    assert "paper/main.tex" in message  # the relative path survives — it's the useful part
    assert "https://github.com/core-aix/agitrack" in message  # URLs untouched


def test_paths_are_masked_in_an_applied_summary():
    base = build_agent_commit_message(
        latest_prompt="do the thing",
        trace=[{"role": "user", "content": "do the thing"}],
        backend="claude",
        backend_session_id="ses-1",
        agitrack_session_id="agit-1",
        model="m1",
        session_name="s1",
    )
    amended = apply_summary_to_message(base, "Fixed ~/agitrack/agitrack/proxy/runner.py")
    assert amended.splitlines()[0] == f"<aGiTrack> Fixed {PATH_MASK}"


def test_path_masking_does_not_disturb_secret_redaction():
    # Both run over the same text; the path pass must not chew up a [REDACTED] marker.
    masked = _mask_secrets_for_test("token=ghp_abcdefghijklmnopqrstuvwxyz0123 in ~/.netrc")
    assert SECRET_MASK in masked and PATH_MASK in masked
    assert "ghp_" not in masked and "~/" not in masked


def test_tilde_approximation_is_not_a_path():
    # "~123/second" is a tilde meaning "approximately", not a home path — seen verbatim in
    # this project's own history as "(~123/second)".
    assert mask_paths("(~123/second)") == "(~123/second)"
    assert mask_paths("about ~50/day") == "about ~50/day"


def test_masking_stops_at_markdown_backticks():
    # The closing backtick must survive: "`~/agitrack` editable" had been mangled to
    # "`[PATH] editable" when backticks counted as path characters.
    assert mask_paths("pipx resolves to `~/agitrack` editable") == f"pipx resolves to `{PATH_MASK}` editable"
    assert mask_paths("`/home/alice/x/y` (editable)") == f"`{PATH_MASK}` (editable)"


def test_route_like_strings_are_masked_by_design():
    # KNOWN, DELIBERATE trade-off: an HTTP route is textually identical to an absolute path,
    # so "/learn/state" is masked too. Masking a route only costs readability; the alternative
    # (a carve-out for extension-less lowercase paths) would let real paths through.
    assert mask_paths("the `/learn/state` endpoint") == f"the `{PATH_MASK}` endpoint"


class TestCarriesAiHistory:
    """`carries_ai_history` separates "aGiTrack was present" from "AI work is recorded here".

    Conflating them is a real source of confusion: a repo whose only aGiTrack commit is a plain
    USER commit reads as 100% tracked while its dashboard is empty, and `--backtrace commit`
    declines to annotate anything.
    """

    USER_ONLY = (
        "Ini\n\n# aGiTrack Metadata\ncommit_type: user\nbackend: agit\n"
        "agitrack_session_id: agitrack-23c1\nsystem: macOS 15.7.3\nagitrack_version: 0.6.5\n"
    )
    AGENT = (
        "Do a thing\n\n# aGiTrack Metadata\ncommit_type: agent\nbackend: claude\ntokens_since_last_commit_output: 42\n"
    )

    def test_an_attribution_only_user_block_is_not_ai_history(self):
        assert carries_ai_history(self.USER_ONLY) is False

    def test_a_commit_with_no_metadata_at_all_is_not(self):
        assert carries_ai_history("Initial commit") is False

    def test_an_agent_block_is(self):
        assert carries_ai_history(self.AGENT) is True

    def test_a_block_with_no_commit_type_is_agent_work(self):
        """Matches the dashboard's classifier, which reads metadata without a commit_type as agent."""
        assert carries_ai_history("x\n\n# aGiTrack Metadata\nbackend: claude\n") is True

    def test_an_in_flight_block_is_not_yet(self):
        """Its trace and tokens are still to come; the completed turn still owes this commit."""
        body = "x\n\n# aGiTrack Metadata\ncommit_type: agent\nin_flight: true\nbackend: claude\n"
        assert carries_ai_history(body) is False

    def test_a_user_commit_that_FOLDED_turns_still_counts(self):
        """Manual mode: the user's own commit leads with a user block and carries each turn's
        agent block. Demoting that would lose real history."""
        assert carries_ai_history(self.USER_ONLY.rstrip() + "\n\n" + self.AGENT) is True

    def test_explicit_zero_tokens_do_not_count_as_work(self):
        body = "x\n\n# aGiTrack Metadata\ncommit_type: user\ntokens_since_last_commit_output: 0\n"
        assert carries_ai_history(body) is False

    def test_a_trace_that_QUOTES_a_metadata_line_is_not_mistaken_for_one(self):
        """aGiTrack's own repo discusses these field names constantly, so a following turn's trace
        must not be read as the preceding block's own record — blocks end at the first blank line."""
        body = (
            self.USER_ONLY.rstrip()
            + "\n\n# Interaction Trace\n\n## User\n\nwhy is tokens_since_last_commit_output: 500 wrong?\n"
        )
        assert carries_ai_history(body) is False

    def test_a_real_user_commit_from_the_wild_is_not_history(self):
        """The exact block that made `--backtrace commit` dead-end on a fresh repo."""
        assert carries_ai_history(build_user_commit_message(message="Ini", agitrack_session_id="s-1")) is False


def _interrupted_trace():
    # What a cancelled turn actually leaves behind: the request, and the agent's opening
    # statement of INTENT. The tool calls that followed are not in the transcript.
    return [
        {"role": "user", "content": "Create ten files named f1.txt through f10.txt."},
        {"role": "agent", "content": "I'll create the ten files one at a time."},
    ]


def test_an_interrupted_turn_names_the_files_its_commit_actually_carries():
    """The trace of a cancelled turn cannot account for the diff, and the summarizer's only
    input is the trace: it read "I'll create the ten files" plus a note saying the changes
    made so far were recorded here, and produced `(interrupted) Requested creation of
    f1.txt–f10.txt was interrupted before any writes` — on a commit adding f1.txt..f8.txt.
    """
    rendered = render_interaction_trace(
        _interrupted_trace(),
        trace_turn_limit=10,
        interrupted=True,
        changed_paths=["f1.txt", "f2.txt", "f3.txt"],
    )

    assert "NOT completed" in rendered  # the interruption itself, as before
    unwrapped = " ".join(rendered.replace("> ", "").split())
    assert "it changes f1.txt, f2.txt, f3.txt." in unwrapped
    assert rendered.index("this commit itself") < rendered.index("## User")  # a lead-in note


def test_a_long_change_list_is_counted_rather_than_dumped():
    paths = [f"f{i}.txt" for i in range(20)]

    rendered = render_interaction_trace(
        _interrupted_trace(), trace_turn_limit=10, interrupted=True, changed_paths=paths
    )

    unwrapped = " ".join(rendered.replace("> ", "").split())
    assert "f11.txt" in unwrapped and "f12.txt" not in unwrapped  # 12 shown
    assert "and 8 more" in unwrapped


def test_the_change_list_is_only_for_interrupted_turns():
    """Every other commit keeps the trace as the summarizer's sole input, by design."""
    rendered = render_interaction_trace(
        _interrupted_trace(), trace_turn_limit=10, interrupted=False, changed_paths=["only_in_the_diff.txt"]
    )

    assert "only_in_the_diff.txt" not in rendered


def test_an_interrupted_turn_with_no_readable_change_list_still_says_it_was_interrupted():
    rendered = render_interaction_trace(_interrupted_trace(), trace_turn_limit=10, interrupted=True)

    assert "NOT completed" in rendered
    assert "this commit itself" not in rendered


def test_the_commit_body_carries_the_same_change_list_as_the_summarizer_input():
    """One note, both readers — the human reading `git show` and the summarizer."""
    message = _base_message(interrupted=True, changed_paths=["a.txt", "b.txt"])

    unwrapped = " ".join(message.replace("> ", "").split())
    assert "it changes a.txt, b.txt." in unwrapped
    assert message.index("a.txt, b.txt") < message.index("## User")
