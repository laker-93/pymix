import threading
from unittest import mock

from pymix.clients.beets_exec import BeetsExec


def test_execute_splits_a_str_command_before_calling_docker_execute():
    beets_exec = BeetsExec()
    with mock.patch("pymix.clients.beets_exec.docker") as mock_docker:
        mock_docker.execute.return_value = "some result"
        result = beets_exec.execute("beetsfoo", "beet stats")
    mock_docker.execute.assert_called_once_with("beetsfoo", ["beet", "stats"], stream=False)
    assert result == "some result"


def test_execute_passes_a_list_command_through_unsplit():
    beets_exec = BeetsExec()
    command = ["beet", "list", "-f", "$subbox_id", "subbox_id::a", ",", "subbox_id::b"]
    with mock.patch("pymix.clients.beets_exec.docker") as mock_docker:
        beets_exec.execute("beetsfoo", command, stream=True)
    mock_docker.execute.assert_called_once_with("beetsfoo", command, stream=True)


def test_write_lock_serializes_writes_for_the_same_container():
    beets_exec = BeetsExec()
    events = []
    holder_acquired = threading.Event()
    release_holder = threading.Event()

    def holder():
        with beets_exec.write_lock("beetsfoo"):
            events.append("holder-acquired")
            holder_acquired.set()
            release_holder.wait(timeout=5)
        events.append("holder-released")

    def waiter():
        holder_acquired.wait(timeout=5)
        with beets_exec.write_lock("beetsfoo"):
            events.append("waiter-acquired")

    t_holder = threading.Thread(target=holder)
    t_waiter = threading.Thread(target=waiter)
    t_holder.start()
    holder_acquired.wait(timeout=5)
    t_waiter.start()

    # give the waiter every chance to (wrongly) acquire the lock while the
    # holder is still inside it
    t_waiter.join(timeout=0.2)
    assert events == ["holder-acquired"], "a second writer must not interleave with an in-flight one"

    release_holder.set()
    t_holder.join(timeout=5)
    t_waiter.join(timeout=5)
    assert events == ["holder-acquired", "holder-released", "waiter-acquired"]


def test_write_lock_does_not_contend_across_different_containers():
    beets_exec = BeetsExec()
    barrier = threading.Barrier(2, timeout=5)
    results = []

    def worker(container_name):
        with beets_exec.write_lock(container_name):
            # only reachable by both threads at once if the locks are
            # actually independent per container name
            barrier.wait()
            results.append(container_name)

    t_alice = threading.Thread(target=worker, args=("beetsalice",))
    t_bob = threading.Thread(target=worker, args=("beetsbob",))
    t_alice.start()
    t_bob.start()
    t_alice.join(timeout=5)
    t_bob.join(timeout=5)
    assert set(results) == {"beetsalice", "beetsbob"}


def test_execute_never_takes_the_write_lock_itself():
    """execute() must be lock-free: a composite job holds write_lock across
    several execute() calls, and a reentrant acquire from inside execute()
    would deadlock that same thread (#73)."""
    beets_exec = BeetsExec()
    completed = threading.Event()

    def run():
        with mock.patch("pymix.clients.beets_exec.docker") as mock_docker:
            mock_docker.execute.return_value = ""
            with beets_exec.write_lock("beetsfoo"):
                beets_exec.execute("beetsfoo", "beet stats")
        completed.set()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout=2)
    assert completed.is_set(), "execute() must not acquire the write lock itself"
