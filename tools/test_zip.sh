#!/usr/bin/env bash
# Test the plugin zip as a user would get it: extract out/*.zip, serve the wheel packs locally (stands in
# for the GitHub release, the plugin downloads and verifies its pack from there), then run the tests.
# usage: tools/test_zip.sh PYTHON [PACKS_DIR]   (the caller sets PYTHONPATH / QGIS_PREFIX_PATH / QT_QPA_PLATFORM)
set -euo pipefail
PY=$1
PACKS=${2:-packs}

mkdir unzipped
"$PY" -c "import glob,zipfile; zipfile.ZipFile(glob.glob('out/*.zip')[0]).extractall('unzipped')"
"$PY" -m http.server 18431 --bind 127.0.0.1 --directory "$PACKS" > /dev/null 2>&1 &
SERVER=$!
trap 'kill "$SERVER"' EXIT
export IQT_PACK_BASE_URL=http://127.0.0.1:18431
export IQT_PLUGIN_PARENT="$PWD/unzipped"
"$PY" test/test_install.py
"$PY" test/test_gui.py
"$PY" test/test_algorithms.py
