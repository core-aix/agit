from agit.merge_queue import MergeCoordinator, MergeState


class FakeParticipant:
    def __init__(self, name, *, clean=True):
        self.name = name
        self.clean = clean
        self.unmerged = not clean
        self.completed = False
        self.injected = []
        self.finalized = 0
        self.aborted = 0

    def merge_base_into_turn(self):
        return self.clean

    def has_unmerged(self):
        return self.unmerged

    def conflict_prompt(self):
        return f"resolve {self.name}"

    def inject_prompt(self, text):
        self.injected.append(text)

    def turn_just_completed(self):
        return self.completed

    def finalize(self):
        self.finalized += 1

    def abort_merge(self):
        self.aborted += 1


def _coord(*participants):
    mapping = {p.name: p for p in participants}
    clock = iter(range(1, 1000))
    return MergeCoordinator(mapping, clock=lambda: next(clock)), mapping


def test_clean_merge_finalizes_immediately():
    a = FakeParticipant("a", clean=True)
    coord, _ = _coord(a)
    coord.enqueue("a")
    coord.tick()
    assert a.finalized == 1
    assert coord.state is MergeState.IDLE
    assert coord.is_busy() is False


def test_serialized_completion_order():
    a = FakeParticipant("a", clean=True)
    b = FakeParticipant("b", clean=True)
    coord, _ = _coord(a, b)
    coord.enqueue("a")
    coord.enqueue("b")
    coord.tick()  # a integrates
    assert a.finalized == 1 and b.finalized == 0
    coord.tick()  # then b
    assert b.finalized == 1


def test_enqueue_is_idempotent():
    a = FakeParticipant("a")
    coord, _ = _coord(a)
    coord.enqueue("a")
    coord.enqueue("a")
    assert coord.pending() == ["a"]


def test_conflict_injects_prompt_then_finalizes_on_resolution():
    a = FakeParticipant("a", clean=False)
    coord, _ = _coord(a)
    coord.enqueue("a")
    coord.tick()  # conflict -> inject, RESOLVING
    assert a.injected == ["resolve a"]
    assert coord.state is MergeState.RESOLVING
    coord.tick()  # agent not done yet
    assert coord.state is MergeState.RESOLVING
    # agent finishes and resolved the conflict
    a.completed = True
    a.unmerged = False
    coord.tick()
    assert a.finalized == 1
    assert coord.state is MergeState.IDLE


def test_unresolved_conflict_pauses_for_user():
    a = FakeParticipant("a", clean=False)
    coord, _ = _coord(a)
    coord.enqueue("a")
    coord.tick()  # conflict
    a.completed = True  # agent finished its turn...
    a.unmerged = True   # ...but conflict remains
    coord.tick()
    assert coord.state is MergeState.PAUSED
    assert "unresolved" in coord.paused_reason
    # tick is a no-op while paused
    coord.tick()
    assert coord.state is MergeState.PAUSED
    # user fixes it by hand, then resolves
    a.unmerged = False
    coord.resolve_paused()
    assert a.finalized == 1
    assert coord.state is MergeState.IDLE


def test_skip_paused_aborts_and_advances():
    a = FakeParticipant("a", clean=False)
    b = FakeParticipant("b", clean=True)
    coord, _ = _coord(a, b)
    coord.enqueue("a")
    coord.enqueue("b")
    coord.tick()
    a.completed = True
    a.unmerged = True
    coord.tick()  # paused on a
    assert coord.state is MergeState.PAUSED
    coord.skip_paused()
    assert a.aborted == 1
    coord.tick()  # now b integrates
    assert b.finalized == 1


def test_drop_removes_queued_and_current():
    a = FakeParticipant("a", clean=False)
    b = FakeParticipant("b", clean=True)
    coord, _ = _coord(a, b)
    coord.enqueue("a")
    coord.enqueue("b")
    coord.drop("b")
    assert "b" not in coord.pending()
    coord.tick()  # a -> conflict -> resolving
    coord.drop("a")
    assert a.aborted == 1
    assert coord.state is MergeState.IDLE
