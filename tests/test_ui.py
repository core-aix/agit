from prompt_toolkit.document import Document

from agit.shell.ui import _AgitCompleter


def test_agit_completer_provides_async_interface():
    completer = _AgitCompleter()

    assert hasattr(completer, "get_completions_async")


def test_agit_completer_suggests_colon_commands():
    completer = _AgitCompleter()

    completions = list(completer.get_completions(Document(":he"), None))

    assert [completion.text for completion in completions] == [":help"]


def test_agit_completer_suggests_slash_commands():
    completer = _AgitCompleter()

    completions = list(completer.get_completions(Document("/he"), None))

    assert [completion.text for completion in completions] == ["/help"]
