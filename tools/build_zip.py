"""Build the plugin zip with its compiled dependencies for every supported Python, for one platform.

pip can fetch wheels for another Python/platform than the one running it, so a single zip serves all
Python versions: dependencies/cp311/, dependencies/cp312/, ... and the plugin puts the one matching
the running interpreter on sys.path (see dependency_checker.dependencies_dir).

Usage: python tools/build_zip.py OUT_ZIP PYTHONS PLATFORM_TAG [PLATFORM_TAG ...]
  e.g. python tools/build_zip.py out/iqt-linux.zip 3.11,3.12 manylinux2014_x86_64 manylinux_2_28_x86_64
"""
import os
import pathlib
import re
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def main(out_zip, pythons, platforms):
    build = ROOT / "build"
    shutil.rmtree(build, ignore_errors=True)
    plugin = build / "image_quality_toolset"
    shutil.copytree(ROOT / "image_quality_toolset", plugin)
    deps = plugin / "dependencies"
    versions = []
    for py in pythons.split(","):
        tag = "cp" + py.replace(".", "")
        d = deps / tag
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--target", str(d), "--only-binary=:all:", "--no-deps", "--no-compile",
             "--implementation", "cp", "--python-version", py, "--abi", tag, "-r", str(ROOT / "bundle-requirements.txt")]
            + [a for p in platforms for a in ("--platform", p)], check=True)
        shutil.rmtree(d / "bin", ignore_errors=True)  # pip's scripts are useless here
        pkgs = sorted(p.name for p in d.glob("*.dist-info") if re.match(r"(loess|scikit_image)-", p.name))
        versions.append(f"{tag}: " + ", ".join(p[:-len(".dist-info")] for p in pkgs))
    for c in build.rglob("__pycache__"):
        shutil.rmtree(c)
    (plugin / "bundled-versions.txt").write_text("\n".join(versions) + "\n")
    print("\n".join(versions))
    out = pathlib.Path(out_zip)
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.make_archive(str(out.with_suffix("")), "zip", build, "image_quality_toolset")
    print(f"{out}: {out.stat().st_size / 1e6:.0f} MB")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3:])
