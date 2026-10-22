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


def test_lock_dir_open_to_others_is_refused(tmp_path, monkeypatch):
    """A pre-existing directory others can enter (e.g. created first by another local
    user under /tmp) is refused rather than used."""
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o755)
    monkeypatch.setattr(server, "LOCK_DIR", shared)
    with pytest.raises(RuntimeError, match="mode 0700"):
        with server._claims_mutex():
            pass
    with pytest.raises(RuntimeError, match="mode 0700"):
        server._write_claim(TAB)
    assert list(shared.iterdir()) == []


def test_lock_dir_symlink_is_refused(tmp_path, monkeypatch):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real)
    monkeypatch.setattr(server, "LOCK_DIR", link)
    with pytest.raises(RuntimeError, match="not a symlink"):
        with server._claims_mutex():
            pass
    assert list(real.iterdir()) == []


def test_planted_symlinks_are_never_written_through(lockdir, tmp_path):
    """Regression: claim and opened-tab records were written via fixed temp names
    (<tab>.tmp / <tab>.otmp) that followed a planted symlink and overwrote its target."""
    victim = tmp_path / "victim.txt"
    victim.write_text("victim")
    for name in (f"{TAB}.tmp", f"{TAB}.otmp"):
        (lockdir / name).symlink_to(victim)
    server._write_claim(TAB)
    server._mark_opened(TAB)
    assert victim.read_text() == "victim"
    assert server._mine(server._read_claim(TAB))
    assert server._read_opened(TAB)["token"] == server.OWNER["token"]


def test_symlinked_claim_file_is_refused_not_followed(lockdir, tmp_path):
    forged = tmp_path / "forged.json"
    forged.write_text(json.dumps({**server.OWNER, "since": time.time(), "last": time.time()}))
    server._claim_path(TAB).symlink_to(forged)
    with pytest.raises(OSError):
        server._require_claim(S)
    assert json.loads(forged.read_text())["token"] == server.OWNER["token"]


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


# ---- owner label --------------------------------------------------------------

def _owner_session(label):
    env = {k: v for k, v in os.environ.items() if k != "TERMIUS_MCP_SESSION_LABEL"}
    env["TMPDIR"] = "/var/folders/xx/yy/T/"          # macOS shape: basename "T" for everyone
    if label is not None:
        env["TERMIUS_MCP_SESSION_LABEL"] = label
    res = subprocess.run([sys.executable, "-W", "ignore", "-c",
                          "import server; print(server.OWNER['session'])"],
                         cwd=os.path.dirname(server.__file__), env=env, timeout=60,
                         capture_output=True, text=True, check=True)
    return res.stdout.strip().splitlines()[-1]


def test_owner_label_names_the_client_process_not_tmpdir():
    assert _owner_session(None) == f"client-pid-{os.getpid()}"


def test_owner_label_can_be_set_and_is_bounded():
    assert _owner_session("codex: deploy box") == "codex: deploy box"
    assert _owner_session("x" * 200) == "x" * 64
    assert _owner_session("   ") == f"client-pid-{os.getpid()}"
