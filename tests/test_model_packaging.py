"""Reproducible offline associated-file packaging; no upstream downloads."""

import hashlib
import zipfile

import pytest

from scripts import package_aiy_birds as packaging


@pytest.fixture
def upstream(tmp_path, monkeypatch):
    # Small stand-in tests archive mechanics, not ML compatibility. The real
    # pinned artifact is independently checked by M1 before publication.
    path = tmp_path / "upstream.tflite"
    path.write_bytes(b"unchanged model payload")
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("inat_bird_labels.txt", b"original upstream labels")
    raw = path.read_bytes()
    monkeypatch.setattr(packaging, "UPSTREAM_SIZE", len(raw))
    monkeypatch.setattr(packaging, "UPSTREAM_SHA256", hashlib.sha256(raw).hexdigest())
    return path


def test_reproducible_packaging_preserves_upstream_and_bundled_labels(upstream, tmp_path):
    first, second = tmp_path / "first.tflite", tmp_path / "second.tflite"
    result = packaging.package_model(upstream, first)
    assert packaging.package_model(upstream, second) == result
    assert first.read_bytes() == second.read_bytes()
    assert first.read_bytes().startswith(upstream.read_bytes())
    assert result == (first.stat().st_size, hashlib.sha256(first.read_bytes()).hexdigest())
    with zipfile.ZipFile(first) as archive, zipfile.ZipFile(packaging.ROOT / "model.tflite") as bundled:
        assert archive.read("inat_bird_labels.txt") == b"original upstream labels"
        for name in packaging.LABEL_SHA256:
            assert archive.read(name) == bundled.read(name)
            assert archive.getinfo(name).date_time == (1980, 1, 1, 0, 0, 0)
        assert archive.read("LICENSE") == (packaging.ROOT / "models/licenses/Apache-2.0.txt").read_bytes()
        notice = archive.read("NOTICE").decode()
        assert "110 and 758" in notice and "No classes were reordered" in notice


def test_wrong_upstream_identity_is_rejected_without_output(tmp_path):
    raw, output = tmp_path / "raw", tmp_path / "output"
    raw.write_bytes(b"not the pinned model")
    with pytest.raises(ValueError, match="pinned Coral CPU"):
        packaging.package_model(raw, output)
    assert not output.exists()


def test_changed_labels_are_rejected_without_output(upstream, tmp_path):
    bundled, output = tmp_path / "custom.tflite", tmp_path / "output"
    with zipfile.ZipFile(bundled, "w") as archive:
        archive.writestr("probability-labels-en.txt", b"changed labels")
    with pytest.raises(ValueError, match="Bundled label identity"):
        packaging.package_model(upstream, output, bundled=bundled)
    assert not output.exists()


def test_existing_output_and_upstream_cannot_be_overwritten(upstream, tmp_path):
    output = tmp_path / "output"
    output.write_bytes(b"existing")
    original = upstream.read_bytes()
    for path in (output, upstream):
        with pytest.raises(FileExistsError):
            packaging.package_model(upstream, path)
    assert output.read_bytes() == b"existing"
    assert upstream.read_bytes() == original
