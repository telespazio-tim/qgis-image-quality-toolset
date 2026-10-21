"""Build the release files: wheel packs (one per platform and Python) and the plugin zip.

  python tools/build_zip.py packs OUT_DIR PLATFORM PYTHONS PIP_TAG [PIP_TAG ...]
      -> OUT_DIR/deps-PLATFORM-cpXY.zip for each Python, e.g.
         python tools/build_zip.py packs packs linux-x64 3.11,3.12 manylinux2014_x86_64 manylinux_2_28_x86_64
  python tools/build_zip.py plugin OUT_ZIP PACKS_DIR
      -> the plugin zip, with deps-sha256.txt listing the packs found in PACKS_DIR

pip can fetch wheels for another Python/platform than the one running it. The plugin downloads the
pack matching its platform and Python from the GitHub release at first run (dependency_checker.py),
and checks it against deps-sha256.txt.
"""
import hashlib
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]


def packs(out_dir, platform, pythons, tags):
    out_dir = pathlib.Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for py in pythons.split(","):
        cp = "cp" + py.replace(".", "")
        d = pathlib.Path(tempfile.mkdtemp()) / cp
        # --no-deps: numpy/scipy/matplotlib come from QGIS, bundle-requirements.txt lists the rest
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--target", str(d), "--only-binary=:all:", "--no-deps",
             "--no-compile", "--implementation", "cp", "--python-version", py, "--abi", cp,
             "-r", str(ROOT / "bundle-requirements.txt")]
            + [a for t in tags for a in ("--platform", t)], check=True)
        shutil.rmtree(d / "bin", ignore_errors=True)  # pip's scripts are useless here
        for c in d.rglob("__pycache__"):
            shutil.rmtree(c)
        versions = sorted(p.name[:-len(".dist-info")] for p in d.glob("*.dist-info"))
        shutil.make_archive(str(out_dir / f"deps-{platform}-{cp}"), "zip", d)
        print(f"{platform} {cp}: {', '.join(versions)}")


def plugin(out_zip, packs_dir):
    build = ROOT / "build"
    shutil.rmtree(build, ignore_errors=True)
    dest = build / "image_quality_toolset"
    shutil.copytree(ROOT / "image_quality_toolset", dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "dependencies"))
    lines = [f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}" for p in sorted(pathlib.Path(packs_dir).glob("deps-*.zip"))]
    assert lines, f"no deps-*.zip in {packs_dir}"
    (dest / "deps-sha256.txt").write_text("\n".join(lines) + "\n")
    out = pathlib.Path(out_zip)
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.make_archive(str(out.with_suffix("")), "zip", build, "image_quality_toolset")
    print(f"{out}: {out.stat().st_size / 1e6:.1f} MB, {len(lines)} wheel packs listed")


if __name__ == "__main__":
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "packs":
        packs(*args[:3], args[3:])
    else:
        plugin(*args)
