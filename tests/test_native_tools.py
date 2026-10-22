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


# ---- upload source policy ---------------------------------------------------

@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv("TERMIUS_UPLOAD_ROOTS", raising=False)
    return h


@pytest.fixture
def uploads(bridge, monkeypatch):
    started = []

    async def call(fn, *args):
        if fn == "startUpload":
            started.append(args[0]); return {"status": "running"}
        if fn == "transferStatus":
            return {"transfer_id": args[0], "status": "completed"}
        raise AssertionError(fn)
    monkeypatch.setattr(bridge, "call", call)
    return started


def _file(path, text="secret"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.mark.parametrize("rel", [".ssh/id_ed25519", ".aws/credentials", ".netrc", ".codex/auth.json",
                                 "Library/Keychains/login.keychain-db", ".SSH/id_rsa",
                                 "backup/.ssh/id_rsa", "old/.gnupg/secring.gpg"])
def test_upload_refuses_credential_stores(home, uploads, rel):
    f = _file(home / rel)
    with pytest.raises(ValueError, match="upload refused"):
        run(server.sftp_upload(str(f), "remote", "/upload"))
    assert uploads == []


def test_upload_refuses_a_key_reached_through_a_symlinked_directory(home, uploads):
    _file(home / ".ssh" / "id_ed25519")
    (home / "innocent").symlink_to(home / ".ssh")
    with pytest.raises(ValueError, match="protected ~/.ssh"):
        run(server.sftp_upload(str(home / "innocent" / "id_ed25519"), "remote", "/upload"))
    assert uploads == []


def test_upload_roots_limit_sources(home, uploads, monkeypatch):
    monkeypatch.setenv("TERMIUS_UPLOAD_ROOTS", f"{home / 'uploads'}:{home / 'more'}")
    ok = _file(home / "uploads" / "report.txt")
    other = _file(home / "docs" / "notes.txt")
    with pytest.raises(ValueError, match="outside TERMIUS_UPLOAD_ROOTS"):
        run(server.sftp_upload(str(other), "remote", "/upload"))
    assert uploads == []
    assert run(server.sftp_upload(str(ok), "remote", "/upload"))["status"] == "completed"
    assert [u["filename"] for u in uploads] == ["report.txt"]


def test_protected_locations_stay_refused_inside_upload_roots(home, uploads, monkeypatch):
    monkeypatch.setenv("TERMIUS_UPLOAD_ROOTS", str(home))
    f = _file(home / ".ssh" / "id_ed25519")
    with pytest.raises(ValueError, match="upload refused"):
        run(server.sftp_upload(str(f), "remote", "/upload"))
    assert uploads == []


def test_relative_upload_root_is_a_configuration_error(home, uploads, monkeypatch):
    monkeypatch.setenv("TERMIUS_UPLOAD_ROOTS", "uploads")
    f = _file(home / "uploads" / "report.txt")
    with pytest.raises(ValueError, match="not absolute"):
        run(server.sftp_upload(str(f), "remote", "/upload"))
    assert uploads == []


def test_ordinary_file_uploads_without_roots(home, uploads):
    f = _file(home / "projects" / "build.tar", "data")
    assert run(server.sftp_upload(str(f), "remote", "/upload"))["status"] == "completed"
    assert len(uploads) == 1


@pytest.mark.parametrize("value", [":", " : ", "::", "   "])
def test_upload_roots_naming_no_directory_is_a_configuration_error(home, uploads, monkeypatch, value):
    """Regression: these parsed to an empty list, which meant 'no limit configured'."""
    monkeypatch.setenv("TERMIUS_UPLOAD_ROOTS", value)
    f = _file(home / "docs" / "notes.txt")
    with pytest.raises(ValueError, match="names no directory"):
        run(server.sftp_upload(str(f), "remote", "/upload"))
    assert uploads == []


def test_empty_upload_roots_means_unset(home, uploads, monkeypatch):
    monkeypatch.setenv("TERMIUS_UPLOAD_ROOTS", "")
    f = _file(home / "docs" / "notes.txt")
    assert run(server.sftp_upload(str(f), "remote", "/upload"))["status"] == "completed"
