"""Check the wheel packs import under the running Python (no QGIS needed; numpy, scipy installed).

Run: python test/check_bundle_imports.py packs/deps-*.zip
Packs for other Pythons are skipped; at least one pack must match the running Python.
"""
import subprocess
import sys
import tempfile
import zipfile

TAG = "cp%d%d" % sys.version_info[:2]
CODE = ("import sys; sys.path.insert(0, sys.argv[1]); import loess.loess_1d, skimage; "
        "from skimage.filters import threshold_otsu; from skimage.morphology import binary_opening, disk, skeletonize; "
        "print(skimage.__version__, skimage.__file__)")


def main(packs):
    tested = 0
    for path in packs:
        if not path.endswith(f"-{TAG}.zip"):
            continue
        d = tempfile.mkdtemp()
        zipfile.ZipFile(path).extractall(d)
        r = subprocess.run([sys.executable, "-c", CODE, d], capture_output=True, text=True)
        assert r.returncode == 0, f"{path}: {r.stderr[-800:]}"
        print(f"OK {path}: {r.stdout.strip()}")
        tested += 1
    assert tested, f"no pack for {TAG}"


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except AssertionError as e:
        sys.exit(str(e))
