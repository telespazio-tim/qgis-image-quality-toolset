"""Fail on Qt5-only unscoped enums that crash on QGIS 4 / Qt6 (no QGIS needed).

Run: python test/check_qt6_enums.py
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1] / "image_quality_toolset"
# unscoped Qt enum (Qt.AlignCenter; Qt.AlignmentFlag.AlignCenter is fine), QVariant types, and the
# QGIS enums that moved to Qgis.* or a nested scope
BAD = re.compile(
    r"\bQt\.[A-Z]\w*(?!\w|\.)"
    r"|\bQVariant\.(?!fromValue)[A-Z]\w*"
    r"|\bQgsWkbTypes\.[A-Z]\w*"
    r"|\bQgsProcessingAlgorithm\.Flag\w*"
    r"|\bQgsProcessingParameterFile\.(File|Folder)\b"
    r"|\bQgsProcessingParameterNumber\.(Integer|Double)\b"
)


def main():
    bad = []
    for f in ROOT.rglob("*.py"):
        if "external_code" in f.parts or "dependencies" in f.parts:
            continue
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#")[0]
            m = BAD.search(code)
            # Qt.Foo.Bar is scoped: only flag when Qt.Foo is not followed by a dot
            if m and not (m.group(0).startswith("Qt.") and code[m.end():m.end() + 1] == "."):
                bad.append(f"{f.relative_to(ROOT.parent)}:{n}: {line.strip()}")
    assert not bad, "Qt5-only enums (use the scoped form):\n" + "\n".join(bad)
    print("OK: no unscoped Qt enums")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        sys.exit(str(e))
