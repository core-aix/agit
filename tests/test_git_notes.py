from pathlib import Path
from agit.git import GitRepo


def test_notes_add_and_show(tmp_path: Path) -> None:
    repo = GitRepo.init(tmp_path)
    (tmp_path / "file.txt").write_text("test")
    repo.stage_paths(["file.txt"])
    repo.commit("initial commit")
    sha = repo.rev_parse("HEAD")
    repo.notes_add(sha, "This is a test note", namespace="test")
    note = repo.notes_show(sha, namespace="test")
    assert note is not None
    assert "This is a test note" in note


def test_notes_show_no_note(tmp_path: Path) -> None:
    repo = GitRepo.init(tmp_path)
    sha = repo.rev_parse("HEAD")
    note = repo.notes_show(sha, namespace="test")
    assert note is None


def test_notes_list(tmp_path: Path) -> None:
    repo = GitRepo.init(tmp_path)
    (tmp_path / "file1.txt").write_text("test")
    repo.stage_paths(["file1.txt"])
    repo.commit("first commit")
    sha1 = repo.rev_parse("HEAD")
    repo.notes_add(sha1, "Note 1", namespace="test")
    (tmp_path / "file2.txt").write_text("test")
    repo.stage_paths(["file2.txt"])
    repo.commit("second commit")
    sha2 = repo.rev_parse("HEAD")
    repo.notes_add(sha2, "Note 2", namespace="test")
    notes = repo.notes_list(namespace="test")
    assert len(notes) == 2
    messages = [msg for _, msg in notes]
    assert "Note 1" in messages
    assert "Note 2" in messages


def test_notes_list_empty(tmp_path: Path) -> None:
    repo = GitRepo.init(tmp_path)
    notes = repo.notes_list(namespace="test")
    assert notes == []


def test_notes_add_overwrite(tmp_path: Path) -> None:
    repo = GitRepo.init(tmp_path)
    (tmp_path / "file.txt").write_text("test")
    repo.stage_paths(["file.txt"])
    repo.commit("initial commit")
    sha = repo.rev_parse("HEAD")
    repo.notes_add(sha, "First note", namespace="test")
    repo.notes_add(sha, "Second note", namespace="test")
    note = repo.notes_show(sha, namespace="test")
    assert note is not None
    assert "Second note" in note
    assert "First note" not in note
