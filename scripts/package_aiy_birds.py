"""Offline, reproducible packaging of the pinned Coral CPU bird classifier.

Run with .venv312/bin/python scripts/package_aiy_birds.py UPSTREAM OUTPUT.
No downloads, catalogue edits, installation or activation are performed.
"""

import argparse
import hashlib
from pathlib import Path
import zipfile


ROOT = Path(__file__).resolve().parent.parent
UPSTREAM_SHA256 = "350fcd8cf1df1560060d464595dfed8b174b05792788052896004848d9ad04f9"
UPSTREAM_SIZE = 3579500
LABEL_SHA256 = {
    "probability-labels-en.txt": "9931977128c4df7b8f01741509d907f0792bb8687eae1b5eca5068dafe5f3df2",
    "probability-labels.txt": "24b64c5ed5cff98055d06e9705012c53c232a4aaf2e5cef821ac62282fac012d",
}


def package_model(upstream, output, *, bundled=ROOT / "model.tflite"):
    """Preserve every upstream byte and append checked labels/licence notices.

    The original associated inat_bird_labels.txt is retained unchanged. Fixed
    ZIP timestamps, permissions, member order and no compression make the
    resulting artifact reproducible without a metadata/FlatBuffer rewrite.
    """
    upstream, output = Path(upstream), Path(output)
    raw = upstream.read_bytes()
    if len(raw) != UPSTREAM_SIZE or hashlib.sha256(raw).hexdigest() != UPSTREAM_SHA256:
        raise ValueError("Upstream artifact does not match the pinned Coral CPU model")
    members = {}
    with zipfile.ZipFile(bundled) as archive:
        for name, expected in LABEL_SHA256.items():
            content = archive.read(name)
            if hashlib.sha256(content).hexdigest() != expected:
                raise ValueError(f"Bundled label identity mismatch: {name}")
            members[name] = content
    members["LICENSE"] = (ROOT / "models/licenses/Apache-2.0.txt").read_bytes()
    members["NOTICE"] = (ROOT / "models/aiy-v1.NOTICE.txt").read_bytes()
    # Exclusive output creation prevents overwriting any input/existing artifact.
    with output.open("x+b") as handle:
        handle.write(raw)
        with zipfile.ZipFile(handle, "a", compression=zipfile.ZIP_STORED) as archive:
            # Append normally replaces the old central directory. Retain that
            # too, while the new directory still indexes the original member.
            archive.start_dir = len(raw)
            handle.seek(len(raw))
            for name, content in members.items():
                if name in archive.namelist():
                    raise ValueError(f"Associated file already exists: {name}")
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                archive.writestr(info, content)
    packaged = output.read_bytes()
    if packaged[:len(raw)] != raw:
        raise ValueError("Packaging changed upstream bytes")
    return len(packaged), hashlib.sha256(packaged).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("upstream", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    size, digest = package_model(args.upstream, args.output)
    print(f"{args.output.name}: size_bytes={size} sha256={digest}")


if __name__ == "__main__":
    main()
