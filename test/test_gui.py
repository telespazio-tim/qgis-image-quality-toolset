"""Startup test: load the plugin like QGIS does (initGui, dock, background dependency check).

Run headless: QT_QPA_PLATFORM=offscreen python test/test_gui.py
With IQT_PLUGIN_PARENT pointing at an unzipped release zip (and IQT_PACK_BASE_URL at the packs),
the dependencies must come from the zip's dependency bundle or the test fails.
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HERE = os.path.dirname(os.path.abspath(__file__))
PARENT = os.environ.get("IQT_PLUGIN_PARENT") or os.path.join(HERE, "..")

from qgis.core import QgsApplication  # noqa: E402
from qgis.PyQt.QtCore import QCoreApplication  # noqa: E402


class Iface:
    """Just enough of QgisInterface for the plugin."""

    def addDockWidget(self, area, widget):
        pass

    def __getattr__(self, name):
        return lambda *a, **k: None


def main():
    app = QgsApplication([], True)
    app.initQgis()
    sys.path.append(os.path.join(QgsApplication.pkgDataPath(), "python", "plugins"))
    sys.path.insert(0, PARENT)
    import image_quality_toolset as plugin

    p = plugin.classFactory(Iface())
    p.initGui()
    end = time.time() + 120
    while time.time() < end and not p.provider:
        QCoreApplication.processEvents()
        time.sleep(0.1)
    assert p.dependencies_ok and p.provider, "dependencies not satisfied: provider not registered"
    n = len(list(p.provider.algorithms()))
    assert n == 3, f"expected 3 algorithms, got {n}"
    if os.environ.get("IQT_PLUGIN_PARENT"):
        import loess
        import skimage
        deps = os.path.join(PARENT, "image_quality_toolset", "dependencies")  # holds one cpXY folder per Python
        for m in (loess, skimage):
            assert os.path.abspath(m.__file__).startswith(os.path.abspath(deps)), f"{m.__name__} not from the zip: {m.__file__}"
    p.unload()
    print(f"OK: plugin started, {n} algorithms")


if __name__ == "__main__":
    main()
    sys.stdout.flush()
    os._exit(0)
