"""Check the bundled wheels import under the running Python (no QGIS needed; numpy, scipy installed).

Run: python test/check_bundle_imports.py out/*.zip
For each zip that has a dependencies/cpXY folder for this Python, extract that folder and import
what the plugin uses. Zips without a folder for this Python are skipped; at least one must be tested.
"""
import subprocess
import sys
import tempfile
import zipfile

TAG = "cp%d%d" % sys.version_info[:2]
CODE = ("import sys; sys.path.insert(0, sys.argv[1]); import loess.loess_1d, skimage; "
        "from skimage.filters import threshold_otsu; from skimage.morphology import binary_opening, disk, skeletonize; "
        "print(skimage.__version__, skimage.__file__)")


def main(zips):
    tested = 0
    for path in zips:
        z = zipfile.ZipFile(path)
        prefix = f"image_quality_toolset/dependencies/{TAG}/"
        members = [n for n in z.namelist() if n.startswith(prefix)]
        if not members:
            print(f"skip {path}: no {TAG} folder")
            continue
        d = tempfile.mkdtemp()
        z.extractall(d, members)
        r = subprocess.run([sys.executable, "-c", CODE, d + "/" + prefix], capture_output=True, text=True)
        assert r.returncode == 0, f"{path} ({TAG}): {r.stderr[-800:]}"
        print(f"OK {path} ({TAG}): {r.stdout.strip()}")
        tested += 1
    assert tested, f"no zip bundles {TAG}"


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except AssertionError as e:
        sys.exit(str(e))
