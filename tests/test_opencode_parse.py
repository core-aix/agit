import io
from pathlib import Path

from agit.backends.opencode import OpenCodeBackend


def test_opencode_parse_prefers_final_response():
    backend = OpenCodeBackend(Path("."))
    output = "\n".join(
        [
            '{"type":"message","content":"partial"}',
            '{"type":"thinking","content":"secret"}',
            '{"type":"final","content":"done","sessionID":"ses-1","model":"m"}',
        ]
    )

    final, session_id, model, _tokens = backend._read_events(io.StringIO(output))

    assert final == "done"
    assert session_id == "ses-1"
    assert model == "m"


def test_opencode_parse_nested_text_part():
    backend = OpenCodeBackend(Path("."))
    output = "\n".join(
        [
            '{"type":"step_start","sessionID":"ses-1","part":{"type":"step-start"}}',
            '{"type":"text","sessionID":"ses-1","part":{"type":"text","text":"Hi. What would you like to work on?","metadata":{"openai":{"phase":"final_answer"}}}}',
            '{"type":"step_finish","sessionID":"ses-1","part":{"type":"step-finish"}}',
        ]
    )

    final, session_id, model, _tokens = backend._read_events(io.StringIO(output))

    assert final == "Hi. What would you like to work on?"
    assert session_id == "ses-1"
    assert model is None


def test_opencode_parse_token_usage():
    backend = OpenCodeBackend(Path("."))
    parsed = backend._parse_event_line(
        '{"type":"step_finish","sessionID":"ses-1","part":{"type":"step-finish","tokens":{"total":8883,"input":8869,"output":14,"reasoning":0,"cache":{"write":3,"read":2}}}}'
    )

    assert parsed is not None
    _display, _final, session_id, _model, tokens = parsed
    assert session_id == "ses-1"
    assert tokens.context == 8869
    assert tokens.total == 14
    assert tokens.input == 8869
    assert tokens.output == 14
    assert tokens.cache_write == 3
    assert tokens.cache_read == 2
