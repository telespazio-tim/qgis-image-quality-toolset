"""Check a plugin zip against what QGIS and the plugins.qgis.org repository require (stdlib only).

Run: python test/check_zip.py out/image_quality_toolset-1.0-linux-x64-qgis3.zip
"""
import configparser
import re
import sys
import zipfile

REQUIRED = ("name", "qgisMinimumVersion", "description", "about", "version", "author", "email", "repository")
FORBIDDEN = re.compile(r"(^|/)(\.git|__pycache__|\.DS_Store)(/|$)|\.pyc$")


def check(path):
    z = zipfile.ZipFile(path)
    assert z.testzip() is None, "corrupt zip"
    names = z.namelist()
    roots = {n.split("/")[0] for n in names}
    assert len(roots) == 1, f"need exactly one root folder, got {sorted(roots)[:5]}"
    root = roots.pop()
    assert re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", root), f"root folder {root!r} is not a valid python package name"
    assert f"{root}/__init__.py" in names, "no __init__.py"
    assert "def classFactory" in z.read(f"{root}/__init__.py").decode(), "no classFactory in __init__.py"
    bad = [n for n in names if FORBIDDEN.search(n)]
    assert not bad, f"files that must not ship: {bad[:5]}"

    meta = configparser.ConfigParser(interpolation=None)
    meta.read_string(z.read(f"{root}/metadata.txt").decode("utf-8"))
    g = meta["general"]
    missing = [k for k in REQUIRED if not g.get(k, "").strip()]
    assert not missing, f"metadata.txt is missing {missing}"
    assert f"{root}/{g['icon']}" in names, f"icon {g['icon']} not in the zip"
    # dual license: GPL v3 (license_file, required by plugins.qgis.org) and Apache 2.0 (core algorithms)
    for license_file in (g.get("license_file", "LICENSE"), "LICENSE-APACHE"):
        assert f"{root}/{license_file}" in names, f"{license_file} not in the zip (plugins.qgis.org rejects the upload)"
    assert re.fullmatch(r"\d+(\.\d+)*", g["qgisMinimumVersion"]), "bad qgisMinimumVersion"
    # QGIS assumes <major>.99 when there is no maximum: the plugin would be disabled on QGIS 4
    assert float(g.get("qgisMaximumVersion", "0").rsplit(".", 1)[0] or 0) >= 4, "qgisMaximumVersion must cover QGIS 4 (4.99)"

    mb = sum(i.file_size for i in z.infolist()) / 1e6
    print(f"OK: {path}: {root} {g['version']}, {len(names)} files, {mb:.0f} MB unpacked")


if __name__ == "__main__":
    for p in sys.argv[1:]:
        try:
            check(p)
        except AssertionError as e:
            sys.exit(f"{p}: {e}")
