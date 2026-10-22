"""Offline install boundary tests: fixture streams, real filesystem/locks, no UI."""

from contextlib import contextmanager
from dataclasses import replace
import errno
import hashlib
import logging
import os
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

from app import model_download as download
from app.model_catalogue import load_catalogue
from app.model_install import InstallError, model_install_guard
from app.model_validation import ModelValidationResult


URL = "https://github.com/WAMF-Project/WAMF/releases/download/models-v1/test.tflite"
CDN = "https://release-assets.githubusercontent.com/github-production-release-asset/123/test?sig=PRIVATE-SIGNED-QUERY"


class Response:
    def __init__(self, chunks=(), *, status=200, headers=None, error=None):
        self.chunks = chunks
        self.status_code = status
        self.headers = headers or {}
        self.error = error
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def iter_content(self, chunk_size):
        assert chunk_size == download.CHUNK_BYTES
        yield from self.chunks
        if self.error:
            raise self.error


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.cookies = Mock()
        self.trust_env = True
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def get(self, url, **kwargs):
        assert self.trust_env is False
        self.calls.append((url, kwargs))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def candidate(tmp_path, monkeypatch):
    body = b"a fixture model streamed in several chunks"
    artifact = replace(load_catalogue().default_artifact, version="test-1", bundled_path=None,
                       filename="test.tflite", size_bytes=len(body),
                       sha256=hashlib.sha256(body).hexdigest(), download_url=URL)
    root = tmp_path / "models"
    root.mkdir()
    validate = Mock(return_value=ModelValidationResult(True, "valid", "Valid"))
    monkeypatch.setattr(download, "validate_model_artifact", validate)
    events = []
    return artifact, root, body, validate, events


def set_responses(monkeypatch, *responses):
    session = Session(responses)
    monkeypatch.setattr(download.requests, "Session", lambda: session)
    return session


def run(candidate, *, protected=(), artifact=None):
    original, root, _, _, events = candidate
    with model_install_guard(root):
        return download.run_install(artifact or original, root, protected, lambda phase, count: events.append((phase, count)))


def final_path(candidate):
    artifact, root, *_ = candidate
    return root / artifact.model_id / artifact.version / artifact.filename


@pytest.mark.parametrize("url", [
    None, "http://github.com/WAMF-Project/WAMF/releases/download/v1/a.tflite",
    "https://user:PRIVATE@github.com/WAMF-Project/WAMF/releases/download/v1/a.tflite",
    "https://localhost/file", "https://127.0.0.1/file", "https://[::1]/file",
    "https://10.1.2.3/file", "https://192.168.1.1/file", "https://172.16.0.1/file",
    "https://169.254.169.254/latest", "https://[fe80::1]/file",
    "https://github.com.evil.example/file", "https://evil.githubusercontent.com/file",
    "https://github.com/other/repo/releases/download/v1/a.tflite",
    "https://github.com/WAMF-Project/WAMF/archive/v1.zip",
    "https://github.com/WAMF-Project/WAMF/releases/download/../a.tflite",
    "https://github.com/WAMF-Project/WAMF/releases/download/%2e%2e/a.tflite",
    "https://github.com/WAMF-Project/WAMF/releases/download/v1/%2Fetc",
    "https://github.com:444/WAMF-Project/WAMF/releases/download/v1/a.tflite",
    "https://github.com/WAMF-Project/WAMF/releases/download/v1/a.tflite?token=PRIVATE",
    "https://github.com/WAMF-Project/WAMF/releases/download/v1/a.tflite\n",
    CDN,
])
def test_invalid_initial_sources_do_not_open_http(candidate, monkeypatch, url):
    session = Mock(side_effect=AssertionError("HTTP must not be opened"))
    monkeypatch.setattr(download.requests, "Session", session)
    result = run(candidate, artifact=replace(candidate[0], download_url=url))
    assert result.code == "invalid_catalogue_source"
    session.assert_not_called()
    assert not final_path(candidate).exists()


@pytest.mark.parametrize("headers", [{}, {"Transfer-Encoding": "chunked"}, {"Content-Length": "41"}])
def test_streamed_install_and_progress(candidate, monkeypatch, headers):
    artifact, root, body, validate, events = candidate
    if "Content-Length" in headers:
        headers = {"Content-Length": str(len(body))}
    response = Response([body[:3], b"", body[3:17], body[17:]], headers=headers)
    session = set_responses(monkeypatch, response)
    def validated(path, metadata, **kwargs):
        assert path.parent == final_path(candidate).parent
        assert path.name.startswith(".test.tflite.") and path.suffix == ".part"
        assert path.read_bytes() == body
        assert path.stat().st_mode & 0o777 == 0o600
        assert metadata == artifact and kwargs["allowed_root"] == root
        assert not final_path(candidate).exists()
        return ModelValidationResult(True, "valid", "Valid")
    validate.side_effect = validated
    result = run(candidate)
    assert result.success and result.state == "installed"
    assert final_path(candidate).read_bytes() == body
    assert [phase for phase, _ in events] == ["downloading", "downloading", "verifying", "validating", "installing"]
    assert events[-1][1] == len(body)
    assert not list(root.rglob("*.part"))
    assert response.closed and session.closed
    assert session.calls[0][1] == {
        "stream": True, "allow_redirects": False, "verify": True,
        "timeout": (5.0, 15.0), "headers": {"Accept-Encoding": "identity"},
    }


@pytest.mark.parametrize("response,code", [
    (Response([b"short"]), "size_mismatch"),
    (Response([b"x" * 100]), "oversized"),
    (Response([b"x" * 42]), "checksum_mismatch"),
    (Response(headers={"Content-Length": "1"}), "size_mismatch"),
    (Response(headers={"Content-Length": "1000"}), "oversized"),
    (Response(headers={"Content-Length": "bad"}), "unexpected_response"),
    (Response(headers={"Content-Encoding": "gzip"}), "unexpected_response"),
    (Response(status=404), "http_error"),
    (Response(error=requests.exceptions.ChunkedEncodingError("PRIVATE")), "size_mismatch"),
    (requests.exceptions.ConnectionError("https://PRIVATE"), "connection_error"),
    (requests.exceptions.SSLError("https://PRIVATE"), "tls_error"),
    (requests.exceptions.ReadTimeout("https://PRIVATE"), "timeout"),
])
def test_download_failures_clean_owned_staging(candidate, monkeypatch, response, code, caplog):
    set_responses(monkeypatch, response)
    result = run(candidate)
    assert result.code == code
    assert "PRIVATE" not in repr(result) + caplog.text
    assert not final_path(candidate).exists()
    assert not list(candidate[1].rglob("*.part"))
    candidate[3].assert_not_called()


def test_hard_cap_prevents_http(candidate, monkeypatch):
    monkeypatch.setattr(download, "MAX_ARTIFACT_BYTES", 8)
    session = Mock(side_effect=AssertionError("No HTTP"))
    monkeypatch.setattr(download.requests, "Session", session)
    assert run(candidate).code == "oversized"
    session.assert_not_called()


@pytest.mark.parametrize("target", [
    "http://release-assets.githubusercontent.com/asset", "https://127.0.0.1/asset",
    "https://[::1]/asset", "https://192.168.1.1/asset", "https://169.254.169.254/asset",
    "https://localhost/asset", "https://other.example/asset",
    "https://release-assets.githubusercontent.com.evil.example/asset",
    "https://user:PRIVATE@release-assets.githubusercontent.com/asset", "/other/repo/releases/download/v1/model",
])
def test_redirect_rejection(candidate, monkeypatch, target):
    session = set_responses(monkeypatch, Response(status=302, headers={"Location": target}))
    assert run(candidate).code == "redirect_rejected"
    assert len(session.calls) == 1


def test_github_asset_redirects_and_signed_url_redaction(candidate, monkeypatch, caplog):
    body = candidate[2]
    responses = [Response(status=302, headers={"Location": CDN}), Response([body])]
    session = set_responses(monkeypatch, *responses)
    with caplog.at_level(logging.INFO):
        result = run(candidate)
    assert result.success
    assert session.calls[1][0] == CDN
    assert "PRIVATE-SIGNED-QUERY" not in caplog.text + repr(result) + repr(candidate[4])
    assert all(response.closed for response in responses)
    session.cookies.clear.assert_called_once()


def test_too_many_redirects(candidate, monkeypatch):
    responses = [Response(status=302, headers={"Location": CDN}) for _ in range(6)]
    session = set_responses(monkeypatch, *responses)
    assert run(candidate).code == "too_many_redirects"
    assert len(session.calls) == 6


@pytest.mark.parametrize("code", ["input_contract", "invalid_labels", "timeout", "worker_failed"])
def test_m1_validation_failure_prevents_promotion(candidate, monkeypatch, code):
    set_responses(monkeypatch, Response([candidate[2]]))
    candidate[3].return_value = ModelValidationResult(False, code, "PRIVATE model details")
    result = run(candidate)
    assert result.code == "validation_failed" and result.validation_code == code
    assert "PRIVATE" not in repr(result)
    assert not final_path(candidate).exists()
    assert not list(candidate[1].rglob("*.part"))


def test_already_installed_never_downloads(candidate, monkeypatch):
    path = final_path(candidate)
    path.parent.mkdir(parents=True)
    path.write_bytes(candidate[2])
    session = Mock(side_effect=AssertionError("No HTTP"))
    monkeypatch.setattr(download.requests, "Session", session)
    assert run(candidate).code == "already_installed"
    assert path.read_bytes() == candidate[2]
    session.assert_not_called()


def test_repair_preserves_previous_inode_until_atomic_promotion(candidate, monkeypatch):
    path = final_path(candidate)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"corrupt existing artifact")
    previous_inode = path.stat().st_ino
    old_handle = path.open("rb")
    def validated(candidate_path, *args, **kwargs):
        if candidate_path == path:
            return ModelValidationResult(False, "checksum_mismatch", "Bad hash")
        assert path.read_bytes() == b"corrupt existing artifact"
        assert path.stat().st_ino == previous_inode
        return ModelValidationResult(True, "valid", "Valid")
    candidate[3].side_effect = validated
    set_responses(monkeypatch, Response([candidate[2]]))
    try:
        assert run(candidate, protected=()).success
        assert path.read_bytes() == candidate[2] and path.stat().st_ino != previous_inode
        assert old_handle.read() == b"corrupt existing artifact"
    finally:
        old_handle.close()


@pytest.mark.parametrize("protected", [None, "selected"])
def test_selected_or_unknown_running_model_cannot_be_repaired(candidate, protected):
    path = final_path(candidate)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"old")
    candidate[3].return_value = ModelValidationResult(False, "checksum_mismatch", "Invalid")
    paths = [str(path)] if protected == "selected" else None
    assert run(candidate, protected=paths).code == "repair_blocked"
    assert path.read_bytes() == b"old"


def test_failed_repair_preserves_previous_artifact(candidate, monkeypatch):
    path = final_path(candidate)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"old")
    candidate[3].return_value = ModelValidationResult(False, "checksum_mismatch", "Invalid")
    set_responses(monkeypatch, Response([b"bad"]))
    assert run(candidate).code == "size_mismatch"
    assert path.read_bytes() == b"old"


@pytest.mark.parametrize("failure", ["write", "file_sync", "replace", "permission"])
def test_filesystem_failures_leave_no_final_or_staging(candidate, monkeypatch, failure):
    set_responses(monkeypatch, Response([candidate[2]]))
    if failure == "write":
        original = os.fdopen
        @contextmanager
        def replacement(descriptor, *args, **kwargs):
            with original(descriptor, *args, **kwargs) as handle:
                handle.write = Mock(side_effect=OSError(errno.ENOSPC, "PRIVATE disk"))
                yield handle
        monkeypatch.setattr(download.os, "fdopen", replacement)
    elif failure == "file_sync":
        monkeypatch.setattr(download.os, "fsync", Mock(side_effect=OSError(errno.ENOSPC, "PRIVATE")))
    elif failure == "replace":
        monkeypatch.setattr(download.os, "replace", Mock(side_effect=OSError(errno.EIO, "PRIVATE")))
    else:
        monkeypatch.setattr(download, "_open_directory", Mock(side_effect=PermissionError("PRIVATE")))
    result = run(candidate)
    assert result.code == ("permission_error" if failure == "permission" else "filesystem_error")
    assert not final_path(candidate).exists()
    assert not list(candidate[1].rglob("*.part"))


def test_directory_fsync_is_best_effort_after_promotion(candidate, monkeypatch):
    set_responses(monkeypatch, Response([candidate[2]]))
    original = os.fsync
    def sync(descriptor):
        import stat
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError(errno.EINVAL, "unsupported")
        original(descriptor)
    monkeypatch.setattr(download.os, "fsync", sync)
    result = run(candidate)
    assert result.success and result.durability_warning
    assert final_path(candidate).read_bytes() == candidate[2]


def test_exclusive_staging_and_narrow_stale_cleanup(candidate, monkeypatch):
    path = final_path(candidate)
    path.parent.mkdir(parents=True)
    stale = path.parent / (".test.tflite." + "a" * 32 + ".part")
    stale.write_bytes(b"abandoned")
    unrelated = path.parent / ".unrelated.part"
    unrelated.write_bytes(b"keep")
    target = path.parent / "manual"
    target.write_bytes(b"manual")
    link = path.parent / (".test.tflite." + "c" * 32 + ".part")
    link.symlink_to(target)
    set_responses(monkeypatch, Response([candidate[2]]))
    assert run(candidate).success
    assert not stale.exists()
    assert unrelated.read_bytes() == b"keep" and link.is_symlink() and target.read_bytes() == b"manual"


@pytest.mark.parametrize("level", ["model", "version", "file"])
def test_destination_symlink_escape(candidate, tmp_path, level):
    path = final_path(candidate)
    outside = tmp_path / "outside"
    outside.mkdir()
    if level == "model":
        (candidate[1] / "bird").symlink_to(outside)
    elif level == "version":
        path.parent.parent.mkdir(parents=True)
        path.parent.symlink_to(outside)
    else:
        path.parent.mkdir(parents=True)
        path.symlink_to(outside / "model.tflite")
    assert run(candidate).code == "unsafe_path"
    assert not list(outside.iterdir())


def test_lock_is_nonblocking_and_released_after_failure(candidate, monkeypatch):
    with model_install_guard(candidate[1]):
        with pytest.raises(InstallError, match="install_in_progress"):
            with model_install_guard(candidate[1]):
                pytest.fail("second writer")
    set_responses(monkeypatch, requests.exceptions.ConnectionError("private"))
    assert run(candidate).code == "connection_error"
    with model_install_guard(candidate[1]):
        pass


def test_multiple_versions_coexist(candidate, monkeypatch):
    set_responses(monkeypatch, Response([candidate[2]]))
    assert run(candidate).success
    original = final_path(candidate)
    artifact = replace(candidate[0], version="test-2")
    set_responses(monkeypatch, Response([candidate[2]]))
    assert run(candidate, artifact=artifact).success
    assert original.read_bytes() == candidate[2]
    assert (candidate[1] / "bird/test-2/test.tflite").read_bytes() == candidate[2]


def test_m1_real_validation_reused_without_public_download(tmp_path, monkeypatch):
    from app.model_validation import validate_model_artifact
    artifact = replace(load_catalogue().default_artifact, version="fixture", bundled_path=None, download_url=URL)
    body = (Path(__file__).resolve().parent.parent / "model.tflite").read_bytes()
    monkeypatch.setattr(download, "validate_model_artifact", validate_model_artifact)
    set_responses(monkeypatch, Response([body[:100], body[100:]]))
    root = tmp_path / "models"
    with model_install_guard(root):
        result = download.run_install(artifact, root, (), lambda *args: None)
    assert result.success
    assert (root / "bird/fixture/model.tflite").read_bytes() == body


def test_bundled_modified_file_is_not_overwritten(candidate, monkeypatch):
    path = candidate[1] / "modified.tflite"
    path.write_bytes(b"modified")
    monkeypatch.setattr(download, "artifact_path", lambda artifact: path)
    candidate[3].return_value = ModelValidationResult(False, "checksum_mismatch", "Modified")
    result = run(candidate, artifact=replace(candidate[0], bundled_path="model.tflite", download_url=None))
    assert result.code == "validation_failed" and result.validation_code == "checksum_mismatch"
    assert path.read_bytes() == b"modified"


@pytest.mark.parametrize("host", sorted(download.ASSET_HOSTS))
def test_documented_asset_hosts_allowed_only_as_redirects(host):
    download.validate_source(f"https://{host}/asset?sig=PRIVATE", redirect=True)
    with pytest.raises(InstallError, match="invalid_catalogue_source"):
        download.validate_source(f"https://{host}/asset?sig=PRIVATE")


def test_relative_release_redirect_and_server_filename_ignored(candidate, monkeypatch, tmp_path):
    session = set_responses(monkeypatch,
        Response(status=302, headers={"Location": "/WAMF-Project/WAMF/releases/download/v2/other.tflite"}),
        Response([candidate[2]], headers={"Content-Disposition": 'attachment; filename="../../escape.tflite"'}),
    )
    assert run(candidate).success
    assert final_path(candidate).is_file() and len(session.calls) == 2
    assert not (tmp_path / "escape.tflite").exists()


def test_staging_creation_cannot_follow_or_overwrite_collision(candidate, monkeypatch):
    target = candidate[1] / "unrelated"
    target.write_bytes(b"keep")
    collision = None
    def raced_name(length):
        nonlocal collision
        collision = final_path(candidate).parent / (".test.tflite." + "e" * 32 + ".part")
        collision.symlink_to(target)
        return "e" * 32
    monkeypatch.setattr(download.secrets, "token_hex", raced_name)
    assert run(candidate).code == "filesystem_error"
    assert target.read_bytes() == b"keep" and collision.is_symlink()


def test_destination_change_during_download_is_not_overwritten(candidate, monkeypatch):
    path = final_path(candidate)
    def validated(*args, **kwargs):
        path.write_bytes(b"concurrent manual file")
        return ModelValidationResult(True, "valid", "Valid")
    candidate[3].side_effect = validated
    set_responses(monkeypatch, Response([candidate[2]]))
    assert run(candidate).code == "filesystem_error"
    assert path.read_bytes() == b"concurrent manual file"


def test_in_root_symlink_alias_is_not_writable_install_target(candidate):
    root = candidate[1]
    (root / "other").mkdir()
    (root / "bird").symlink_to(root / "other")
    assert run(candidate).code == "unsafe_path"


def test_stream_read_timeout_wrapped_by_requests(candidate, monkeypatch):
    from urllib3.exceptions import ReadTimeoutError
    response = Response(error=requests.exceptions.ConnectionError(ReadTimeoutError(None, "PRIVATE", "timeout")))
    set_responses(monkeypatch, response)
    assert run(candidate).code == "timeout"


def test_directory_replaced_by_symlink_before_promotion_is_rejected(candidate, monkeypatch, tmp_path):
    path = final_path(candidate)
    moved = tmp_path / "moved"
    def swapped(*args, **kwargs):
        path.parent.rename(moved)
        path.parent.symlink_to(moved)
        return ModelValidationResult(True, "valid", "Valid")
    candidate[3].side_effect = swapped
    set_responses(monkeypatch, Response([candidate[2]]))
    assert run(candidate).code == "unsafe_path"
    assert not (moved / "test.tflite").exists()


def test_staging_changed_during_validation_is_not_promoted(candidate, monkeypatch):
    def changed(path, *args, **kwargs):
        path.write_bytes(b"unverified replacement")
        return ModelValidationResult(True, "valid", "Valid")
    candidate[3].side_effect = changed
    set_responses(monkeypatch, Response([candidate[2]]))
    assert run(candidate).code == "filesystem_error"
    assert not final_path(candidate).exists()
