"""Install smoke test: copy the plugin into a QGIS profile plugins dir, load it, check algorithms register.

Run headless: QT_QPA_PLATFORM=offscreen python test/test_install.py
"""
import configparser
import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qgis.core import QgsApplication  # noqa: E402

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "image_quality_toolset")
NAME = "image_quality_toolset"


def main():
    # "install": same copy as `make install`, into a throwaway profile
    plugins = os.path.join(tempfile.mkdtemp(), "python", "plugins")
    dest = os.path.join(plugins, NAME)
    shutil.copytree(SRC, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    meta = configparser.ConfigParser()
    meta.read(os.path.join(dest, "metadata.txt"), encoding="utf-8")
    assert meta["general"]["version"], "metadata.txt has no version"

    app = QgsApplication([], False)
    app.initQgis()
    try:
        # the "processing" plugin is not on sys.path outside a running QGIS, and the
        # plugin package imports it at load time (see result_dockwidget.py)
        sys.path.append(os.path.join(QgsApplication.pkgDataPath(), "python", "plugins"))
        sys.path.insert(0, plugins)
        import image_quality_toolset as plugin

        # dependencies are installed by the CI environment, not by the plugin's background checker
        provider = plugin.ImageQualityToolSetProvider(None)
        assert app.processingRegistry().addProvider(provider)
        n = len(list(provider.algorithms()))
        assert n == 3, f"expected 3 algorithms, got {n}"
        assert callable(plugin.classFactory)
        print(f"OK: {NAME} {meta['general']['version']} installed, {n} algorithms")
    finally:
        app.exitQgis()


if __name__ == "__main__":
    main()
