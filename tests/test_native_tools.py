import pytest
import server
from conftest import run


def test_switch_verifies_active_session(bridge, term, monkeypatch):
    original = bridge.call
    async def call(fn, *args):
        if fn == "activateSession":
            bridge.active = args[0]
            return {"requested": True}
        return await original(fn, *args)
    monkeypatch.setattr(bridge, "call", call)
    assert "active" in run(server.switch_terminal(term.id))


def test_close_requires_claim_and_refuses_busy_session(bridge, term):
    with pytest.raises(RuntimeError, match="claim_terminal"):
        run(server.close_terminal(term.id))
    run(server.claim_terminal(term.id))
    term.busy = True
    with pytest.raises(RuntimeError, match="busy|prompt"):
        run(server.close_terminal(term.id))


def test_close_confirms_removal_not_just_dispatch(bridge, term, monkeypatch):
    run(server.claim_terminal(term.id))
    original = bridge.call
    async def call(fn, *args):
        if fn == "closeSession":
            del bridge.terms[args[0]]
            return {"requested": True}
        return await original(fn, *args)
    monkeypatch.setattr(bridge, "call", call)
    assert "closed" in run(server.close_terminal(term.id))
    assert server._read_claim(term.id) is None


def test_upload_rejects_non_file_and_relative_destination(bridge, tmp_path):
    f = tmp_path / "sample.txt"; f.write_text("abc")
    with pytest.raises(ValueError, match="absolute"):
        run(server.sftp_upload(str(f), "remote", "relative"))
    with pytest.raises(ValueError, match="regular file"):
        run(server.sftp_upload(str(tmp_path), "remote", "/upload"))


def test_upload_uses_size_path_and_returns_terminal_error(bridge, monkeypatch, tmp_path):
    f = tmp_path / "sample.txt"; f.write_text("abc")
    requests = []
    async def call(fn, *args):
        if fn == "startUpload":
            requests.append(args[0]); return {"status": "running"}
        if fn == "transferStatus":
            return {"transfer_id": args[0], "status": "failed", "error": "disk full"}
        raise AssertionError(fn)
    monkeypatch.setattr(bridge, "call", call)
    result = run(server.sftp_upload(str(f), "remote-a", "/upload", timeout=1))
    assert result["status"] == "failed"
    assert result["error"] == "disk full"
    assert requests[0]["local_path"] == str(f)
    assert requests[0]["size"] == 3
    assert len(requests) == 1


def test_sftp_multiple_windows_refused_before_upload(bridge, monkeypatch, tmp_path):
    f = tmp_path / "a.txt"; f.write_text("a")
    bridge.windows = 2
    with pytest.raises(RuntimeError, match="windows"):
        run(server.sftp_upload(str(f), "remote", "/upload"))
