"""Tab claims (who may type) and per-tab call locks (one input call at a time)."""
import fcntl
import json
import os
import subprocess
import sys
import time

import pytest

import server
from conftest import foreign_claim, run

TAB = "aaaa1111-0000-0000-0000-000000000001"
S = {"id": TAB, "name": "Local Terminal"}


# ---- claims ---------------------------------------------------------------

def test_unclaimed_tab_is_refused(lockdir):
    with pytest.raises(RuntimeError, match="not claimed"):
        server._require_claim(S)


def test_own_claim_is_accepted_and_refreshes_idle_timer(lockdir):
    server._write_claim(TAB)
    old = json.loads(server._claim_path(TAB).read_text())
    old["last"] -= 100
    server._claim_path(TAB).write_text(json.dumps(old))
    server._require_claim(S)
    assert time.time() - server._read_claim(TAB)["last"] < 5


def test_claim_held_by_another_live_process_is_refused(lockdir):
    foreign_claim(TAB)                       # owner pid = our parent, alive
    with pytest.raises(RuntimeError, match="claimed by session runtime-other"):
        server._require_claim(S)


def test_claim_of_dead_process_does_not_count(lockdir):
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    foreign_claim(TAB, pid=p.pid)
    assert server._read_claim(TAB) is None


def test_idle_claim_expires(lockdir, monkeypatch):
    foreign_claim(TAB, last=time.time() - server.CLAIM_IDLE - 1)
    assert server._read_claim(TAB) is None


def test_claim_just_inside_idle_limit_still_counts(lockdir):
    foreign_claim(TAB, last=time.time() - server.CLAIM_IDLE + 30)
    assert server._read_claim(TAB) is not None


def test_corrupt_claim_file_is_treated_as_unclaimed(lockdir):
    server._claim_path(TAB).write_text("{not json")
    assert server._read_claim(TAB) is None


def test_mine_compares_token_not_pid(lockdir):
    c = foreign_claim(TAB, pid=os.getpid(), token="not-our-token")
    assert not server._mine(c)
    server._write_claim(TAB)
    assert server._mine(server._read_claim(TAB))


def test_idle_limit_outlasts_longest_run_command():
    assert server.CLAIM_IDLE > 600


def test_claim_path_cannot_escape_lock_dir(lockdir):
    p = server._claim_path("../../etc/passwd")
    assert p.parent == lockdir


def test_lock_dir_is_created_private(tmp_path, monkeypatch):
    fresh = tmp_path / "fresh"
    monkeypatch.setattr(server, "LOCK_DIR", fresh)
    with server._claims_mutex():
        pass
    assert oct(fresh.stat().st_mode & 0o777) == "0o700"


# ---- per-tab call lock ----------------------------------------------------

def test_second_input_call_on_same_tab_is_refused_not_queued(lockdir):
    async def go():
        async with server._tab_lock(S):
            with pytest.raises(RuntimeError, match="busy with another input call"):
                async with server._tab_lock(S):
                    pytest.fail("second call must not enter")
    run(go())


def test_lock_is_released_after_the_call(lockdir):
    async def go():
        async with server._tab_lock(S):
            pass
        async with server._tab_lock(S):
            return True
    assert run(go())


def test_lock_is_released_when_the_call_raises(lockdir):
    async def go():
        with pytest.raises(ValueError):
            async with server._tab_lock(S):
                raise ValueError("boom")
        async with server._tab_lock(S):
            return True
    assert run(go())


def test_different_tabs_do_not_block_each_other(lockdir):
    async def go():
        async with server._tab_lock(S):
            async with server._tab_lock({"id": "other-tab", "name": "web-1"}):
                return True
    assert run(go())


def test_lock_is_exclusive_across_processes(lockdir):
    holder = subprocess.Popen([sys.executable, "-c", (
        "import fcntl,os,sys,time;"
        f"fd=os.open({str(lockdir / (TAB + '.lock'))!r},os.O_RDWR|os.O_CREAT);"
        "fcntl.flock(fd,fcntl.LOCK_EX);print('held',flush=True);time.sleep(30)")],
        stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"

        async def go():
            async with server._tab_lock(S):
                pytest.fail("must not enter while another process holds the tab")
        with pytest.raises(RuntimeError, match="busy"):
            run(go())
    finally:
        holder.kill()
        holder.wait()


def test_lock_survives_holder_crash(lockdir):
    """flock is released by the kernel when the holder dies, so no stale lock file."""
    holder = subprocess.Popen([sys.executable, "-c", (
        "import fcntl,os;"
        f"fd=os.open({str(lockdir / (TAB + '.lock'))!r},os.O_RDWR|os.O_CREAT);"
        "fcntl.flock(fd,fcntl.LOCK_EX);os._exit(0)")])
    holder.wait()

    async def go():
        async with server._tab_lock(S):
            return True
    assert run(go())
