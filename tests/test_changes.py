import threading
import time

from messnet.changes import Changes


def test_version_moves_only_when_the_token_does():
    token = ["a"]
    ch = Changes(lambda: token[0])
    first = ch.check()
    assert ch.check() == first                       # nothing changed
    token[0] = "b"
    assert ch.check() == first + 1 and ch.check() == first + 1


def test_wait_times_out_when_nothing_changes_and_wakes_when_something_does():
    token = ["a"]
    ch = Changes(lambda: token[0], interval=0.02)
    version = ch.check()
    with ch.watching():
        t0 = time.monotonic()
        assert ch.wait(version, 0.1) == version and time.monotonic() - t0 >= 0.09
        threading.Timer(0.1, lambda: token.__setitem__(0, "b")).start()      # the watcher thread notices
        assert ch.wait(version, 5) == version + 1


def test_the_watcher_runs_only_while_someone_waits():
    calls = []
    ch = Changes(lambda: calls.append(1), interval=0.01)
    ch.check()
    n = len(calls)
    time.sleep(0.1)
    assert len(calls) == n                           # nobody waiting: nothing polls
    with ch.watching():
        time.sleep(0.1)
    assert len(calls) > n + 3
    time.sleep(0.1)
    settled = len(calls)
    time.sleep(0.1)
    assert len(calls) == settled and ch._thread is None      # and it stops again
