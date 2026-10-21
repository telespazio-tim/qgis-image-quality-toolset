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
    print(f"OK: plugin started, {n} algorithms")
    check_two_runs_in_the_dock(p)
    p.unload()


def check_two_runs_in_the_dock(p):
    """Two runs shown in the real result dock, the ROI given as a file path: Processing opens a
    temporary layer and deletes it after the run, so the second result used to fail with
    "wrapped C/C++ object of type QgsVectorLayer has been deleted" (disconnecting the old ROI)."""
    import tempfile
    import traceback
    from qgis.core import QgsProcessingContext, QgsProcessingFeedback, QgsProject

    errors = []
    sys.excepthook = lambda *exc: errors.append(exc)  # exceptions raised in Qt's event() land here
    data = os.path.join(HERE, "data", "target2")
    params = dict(RASTER=os.path.join(data, "S100199aI_002_2_PAN_L0R_mtf_target.tif"), BAND=1,
                  ROI=os.path.join(data, "shapefile", "roi_pan_edge.shp"), SCALE=0.01, OFFSET=1, PX_MARGIN=0,
                  EDGE_DIRECTION=0, ESF_MODEL=0, SAMPLING=0.2)
    for _ in range(2):
        alg = p.provider.algorithm("MTFEstimatorKnifeEdge").create()
        context, feedback = QgsProcessingContext(), QgsProcessingFeedback()
        context.setProject(QgsProject.instance())
        run = dict(params, OUTPUT_DIRECTORY=tempfile.mkdtemp())
        alg.prepare(run, context, feedback)
        assert alg.run(run, context, feedback)[1], "knife edge failed"
        del context  # deletes the temporary ROI layer, as after a run in QGIS
        QCoreApplication.sendPostedEvents()  # the result event reaches the dock
        QCoreApplication.processEvents()
    sys.excepthook = sys.__excepthook__
    for exc in errors:
        traceback.print_exception(*exc)
    assert not errors, f"result dock raised: {errors[0][1]!r}"
    assert p.dock_widget.mtf is not None, "no result shown in the dock"
    print("OK: two runs in the result dock (ROI from a file path)")


if __name__ == "__main__":
    main()
    sys.stdout.flush()
    os._exit(0)
