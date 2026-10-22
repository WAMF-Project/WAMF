"""Private install-worker operations: approved HTTP, same-filesystem staging.

Only model_install's owned child calls this boundary in production. All paths
come from M1 metadata/storage helpers, never request-supplied destinations.
"""

from contextlib import ExitStack
import errno
import hashlib
import logging
import os
from pathlib import Path
import re
import secrets
import stat
from urllib.parse import unquote, urljoin, urlsplit

import requests
from urllib3.exceptions import ReadTimeoutError

from app.model_catalogue import safe_component
from app.model_install import (
    CHUNK_BYTES, CONNECT_TIMEOUT_SECONDS, InstallError, MAX_ARTIFACT_BYTES,
    MAX_REDIRECTS, READ_TIMEOUT_SECONDS, install_result,
)
from app.model_registry import artifact_path
from app.model_validation import validate_model_artifact
from wamf_paths import ModelPathError, get_model_artifact_path


logger = logging.getLogger(__name__)
RELEASE_REPOSITORY = ("wamf-project", "wamf")
# GitHub's documented release/runner asset hosts, deliberately not a wildcard:
# https://docs.github.com/en/actions/reference/runners/github-hosted-runners
ASSET_HOSTS = frozenset({
    "release-assets.githubusercontent.com", "objects.githubusercontent.com",
    "objects-origin.githubusercontent.com", "github-releases.githubusercontent.com",
})


def validate_source(url, *, redirect=False):
    """Exact host allowlisting prevents literal/private/localhost destinations.

    No separate DNS preflight or IP pinning: TLS authenticates these fixed
    GitHub hosts. Environment proxies/netrc are disabled by the HTTP session.
    """
    code = "redirect_rejected" if redirect else "invalid_catalogue_source"
    try:
        if not isinstance(url, str) or not url or any(ord(c) <= 32 or ord(c) == 127 for c in url):
            raise ValueError
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443) or parsed.fragment or "\\" in url):
            raise ValueError
        if parsed.hostname == "github.com":
            parts = parsed.path.split("/")
            if (len(parts) != 7 or tuple(part.lower() for part in parts[1:3]) != RELEASE_REPOSITORY
                    or parts[3:5] != ["releases", "download"] or parsed.query):
                raise ValueError
            safe_component(unquote(parts[5]), "release tag")
            safe_component(unquote(parts[6]), "release asset")
        elif not redirect or parsed.hostname not in ASSET_HOSTS:
            raise ValueError
        if not parsed.path or parsed.path == "/":
            raise ValueError
    except (ValueError, TypeError) as exc:
        raise InstallError(code) from exc


def _download(artifact, handle, emit):
    validate_source(artifact.download_url)
    if artifact.size_bytes > MAX_ARTIFACT_BYTES:
        raise InstallError("oversized")
    digest = hashlib.sha256()
    received = 0
    next_report = 1024 * 1024
    emit("downloading", 0)
    try:
        with requests.Session() as session:
            session.trust_env = False
            url = artifact.download_url
            for redirects in range(MAX_REDIRECTS + 1):
                with session.get(
                    url, stream=True, allow_redirects=False, verify=True,
                    timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
                    headers={"Accept-Encoding": "identity"},
                ) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("Location")
                        if not location:
                            raise InstallError("redirect_rejected")
                        target = urljoin(url, location)
                        validate_source(target, redirect=True)
                        if redirects == MAX_REDIRECTS:
                            raise InstallError("too_many_redirects")
                        session.cookies.clear()
                        url = target
                        continue
                    if response.status_code != 200:
                        raise InstallError("http_error")
                    if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                        raise InstallError("unexpected_response")
                    length = response.headers.get("Content-Length")
                    if length is not None:
                        if not re.fullmatch(r"[0-9]+", length):
                            raise InstallError("unexpected_response")
                        if int(length) > min(artifact.size_bytes, MAX_ARTIFACT_BYTES):
                            raise InstallError("oversized")
                        if int(length) != artifact.size_bytes:
                            raise InstallError("size_mismatch")
                    for chunk in response.iter_content(chunk_size=CHUNK_BYTES):
                        if not chunk:
                            continue
                        received += len(chunk)
                        if received > min(artifact.size_bytes, MAX_ARTIFACT_BYTES):
                            raise InstallError("oversized")
                        handle.write(chunk)
                        digest.update(chunk)
                        if received >= next_report or received == artifact.size_bytes:
                            emit("downloading", received)
                            next_report = received + 1024 * 1024
                    break
            emit("verifying", received)
            if received != artifact.size_bytes:
                raise InstallError("size_mismatch")
            if digest.hexdigest() != artifact.sha256:
                raise InstallError("checksum_mismatch")
    except requests.exceptions.SSLError as exc:
        raise InstallError("tls_error") from exc
    except (requests.exceptions.Timeout, ReadTimeoutError) as exc:
        raise InstallError("timeout") from exc
    except requests.exceptions.ChunkedEncodingError as exc:
        raise InstallError("size_mismatch") from exc
    except requests.exceptions.ConnectionError as exc:
        code = "timeout" if exc.args and isinstance(exc.args[0], ReadTimeoutError) else "connection_error"
        raise InstallError(code) from exc
    except requests.exceptions.RequestException as exc:
        raise InstallError("connection_error") from exc


def _open_directory(parent_fd, name):
    try:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise InstallError("unsafe_path") from exc
        raise


def _final_identity(directory_fd, filename):
    try:
        info = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise InstallError("unsafe_path")
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _cleanup_stale(directory_fd, filename):
    """Only this exact artifact's recognized abandoned staging names, under lock."""
    pattern = re.compile(r"\." + re.escape(filename) + r"\.[0-9a-f]{32}\.part")
    with os.scandir(directory_fd) as entries:
        for entry in entries:
            if pattern.fullmatch(entry.name) and entry.is_file(follow_symlinks=False):
                os.unlink(entry.name, dir_fd=directory_fd)


def _sync_directory(descriptor):
    # Match config persistence: after atomic replacement, directory durability
    # is best effort on filesystems that do not support directory fsync.
    try:
        os.fsync(descriptor)
        return True
    except OSError:
        logger.warning("Model directory synchronization could not complete")
        return False


def _assert_directory(root, parent, root_fd, directory_fd):
    if root.resolve() != root or parent.resolve() != parent:
        raise InstallError("unsafe_path")
    for path, descriptor in ((root, root_fd), (parent, directory_fd)):
        info, opened = path.stat(), os.fstat(descriptor)
        if (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino):
            raise InstallError("unsafe_path")


def install_artifact(artifact, root, protected_paths, emit):
    """Private worker boundary; caller must hold model_install_guard throughout.

    None means no authoritative running identity: repair is deferred. Atomic
    replacement always writes a different inode; existing bytes are never
    truncated, including during a failed repair.
    """
    root = Path(root)
    if artifact.bundled_path is not None:
        path = artifact_path(artifact)
        emit("validating", 0)
        validation = validate_model_artifact(path, artifact)
        if not validation.valid:
            raise InstallError("validation_failed", validation_code=validation.code)
        return install_result("already_available_bundled")

    validate_source(artifact.download_url)
    if artifact.size_bytes > MAX_ARTIFACT_BYTES:
        raise InstallError("oversized")
    config = {"storage": {"models_path": str(root)}}
    final = get_model_artifact_path(artifact.model_id, artifact.version, artifact.filename, config)
    expected = root / artifact.model_id / artifact.version / artifact.filename
    if final != expected:
        # Installer is stricter than read-only discovery: no directory aliases.
        raise InstallError("unsafe_path")
    with ExitStack() as stack:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        stack.callback(os.close, root_fd)
        model_fd = _open_directory(root_fd, artifact.model_id)
        stack.callback(os.close, model_fd)
        directory_fd = _open_directory(model_fd, artifact.version)
        stack.callback(os.close, directory_fd)
        _assert_directory(root, final.parent, root_fd, directory_fd)
        previous = _final_identity(directory_fd, artifact.filename)
        _cleanup_stale(directory_fd, artifact.filename)
        if previous is not None:
            emit("validating", 0)
            existing = validate_model_artifact(final, artifact, allowed_root=root)
            if existing.valid:
                return install_result("already_installed")
            if protected_paths is None or str(final) in protected_paths:
                raise InstallError("repair_blocked", validation_code=existing.code)
        staging = f".{artifact.filename}.{secrets.token_hex(16)}.part"
        descriptor = os.open(
            staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600, dir_fd=directory_fd,
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                _download(artifact, handle, emit)
                handle.flush()
                os.fsync(handle.fileno())
            emit("validating", artifact.size_bytes)
            staged_identity = _final_identity(directory_fd, staging)
            validation = validate_model_artifact(final.parent / staging, artifact, allowed_root=root)
            if not validation.valid:
                raise InstallError("validation_failed", validation_code=validation.code)
            emit("installing", artifact.size_bytes)
            _assert_directory(root, final.parent, root_fd, directory_fd)
            if _final_identity(directory_fd, artifact.filename) != previous:
                raise InstallError("filesystem_error")
            if _final_identity(directory_fd, staging) != staged_identity:
                raise InstallError("filesystem_error")
            os.replace(staging, artifact.filename, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            durable = all([_sync_directory(fd) for fd in (directory_fd, model_fd, root_fd)])
            return install_result("installed", durability_warning=not durable)
        finally:
            try:
                os.unlink(staging, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
            except OSError:
                logger.warning("An owned model staging file could not be removed")


def run_install(artifact, root, protected_paths, emit):
    """Translate only boundary failures; no raw exception/URL reaches callers."""
    try:
        return install_artifact(artifact, root, protected_paths, emit)
    except InstallError as exc:
        return install_result(exc.code, validation_code=exc.validation_code)
    except (ModelPathError, ValueError, RuntimeError):
        return install_result("unsafe_path")
    except PermissionError:
        return install_result("permission_error")
    except OSError:
        return install_result("filesystem_error")
