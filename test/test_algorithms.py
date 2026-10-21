"""End-to-end tests: run the plugin algorithms headless on the sample data.

Run headless: QT_QPA_PLATFORM=offscreen python test/test_algorithms.py

The algorithms must declare FlagNoThreading (asserted below): they crashed on macOS when run
in a Qt pool thread, where numpy/OpenBLAS overflows the 512 KB thread stack.
"""
import math
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
PARENT = os.environ.get("IQT_PLUGIN_PARENT") or os.path.join(HERE, "..")  # see test_install.py
sys.path.insert(0, PARENT)
# as the plugin does at startup (see dependency_checker.dependencies_dir)
sys.path.append(os.path.join(PARENT, "image_quality_toolset", "dependencies", "cp%d%d" % sys.version_info[:2]))

from qgis.core import (  # noqa: E402
    QgsApplication, QgsProcessingAlgorithm, QgsProcessingContext,
    QgsProcessingFeedback, QgsProject, QgsRasterLayer, QgsVectorLayer)
from qgis.PyQt.QtCore import QObject  # noqa: E402

# name: (algorithm name, raster, roi, extra parameters, expected results)
# Reference values come from a known good run; REL_TOL absorbs numpy/scipy/BLAS differences across
# platforms. If a dependency upgrade legitimately moves them, check the change, then update here.
REL_TOL = 0.05
CASES = {
    "knife_edge_target1": ("MTFEstimatorKnifeEdge",
        "target1/T32TNR_20250619T101559_B04_10m_mtf_roi.tiff", "target1/shapefile/mtf_al_bagnolo.shp",
        dict(SCALE=0.01, OFFSET=0, PX_MARGIN=0, EDGE_DIRECTION=0, ESF_MODEL=0, SAMPLING=0.2),
        {"MTF_NYQ": 0.022, "MTF50": 0.172}),
    "knife_edge_loess": ("MTFEstimatorKnifeEdge",  # needs the bundled loess
        "target1/T32TNR_20250619T101559_B04_10m_mtf_roi.tiff", "target1/shapefile/mtf_al_bagnolo.shp",
        dict(SCALE=0.01, OFFSET=0, PX_MARGIN=0, EDGE_DIRECTION=0, ESF_MODEL=5, SAMPLING=0.2),
        {"MTF_NYQ": 0.118, "MTF50": 0.174}),
    # 6 px bridge simulation (50x50, georeferenced afterwards, EPSG:32632, 1 m pixels):
    # the true MTF is unknown, values come from a known good run;
    # the bridge method uses scikit-image
    "bridge_synthetic": ("MTFEstimatorBridge",
        "bridge/simu_6px.tiff", "bridge/shapefile/roi.shp",
        dict(SCALE=0.01, OFFSET=0, PX_MARGIN=0, BRIDGE_WIDTH=6.0, EDGE_DIRECTION=0, SAMPLING=0.1),
        {"MTF_NYQ": 0.0787, "MTF50": 0.2839}),
    # SCALE=1 (the default, raw DN): the MTF is normalised, so the same values as above. The bin
    # cleaning threshold used to be absolute: in DN it removed bins, which crashed both methods.
    "knife_edge_target1_dn": ("MTFEstimatorKnifeEdge",
        "target1/T32TNR_20250619T101559_B04_10m_mtf_roi.tiff", "target1/shapefile/mtf_al_bagnolo.shp",
        dict(SCALE=1, OFFSET=0, PX_MARGIN=0, EDGE_DIRECTION=0, ESF_MODEL=0, SAMPLING=0.2),
        {"MTF_NYQ": 0.022, "MTF50": 0.172}),
    "bridge_synthetic_dn": ("MTFEstimatorBridge",
        "bridge/simu_6px.tiff", "bridge/shapefile/roi.shp",
        dict(SCALE=1, OFFSET=0, PX_MARGIN=0, BRIDGE_WIDTH=6.0, EDGE_DIRECTION=0, SAMPLING=0.1),
        {"MTF_NYQ": 0.0787, "MTF50": 0.2839}),
    # raw image (no CRS, pixel coordinates) and a ROI with no CRS either (no .prj), so that QGIS
    # draws them on top of each other: the window of config_files/pan_target_MTF.ini
    "knife_edge_target2_nocrs": ("MTFEstimatorKnifeEdge",
        "target2/S100199aI_002_2_PAN_L0R_mtf_target.tif", "target2/shapefile/mtf_mire.shp",
        dict(SCALE=0.01, OFFSET=1, PX_MARGIN=0, EDGE_DIRECTION=0, ESF_MODEL=0, SAMPLING=0.2),
        {"MTF_NYQ": 0.011, "MTF50": 0.153}),
    "snr_target4": ("SNREstimator",
        "target4/LC09_L1TP_181040_20211121_20230505_02_T1_B5_rad_crop_2.tif", "target4/shapefile/ROI.shp",
        dict(WINDOW_SIZE=5, SNR_PRECISION=0.1, L_MIN=100, L_MAX=160),
        {"peak_snr_value": 25.204, "snr_formulation_1": 30.180}),  # SNR exposes attributes, not .results
}


class ResultSink(QObject):
    """Stands in for the result dock: keeps the MTFEvents the algorithms post."""

    def __init__(self):
        super().__init__()
        self.events = []

    def event(self, e):
        if hasattr(e, "getMTF"):
            self.events.append(e)
            return True
        return super().event(e)


def run(alg, params):
    context, feedback = QgsProcessingContext(), QgsProcessingFeedback()
    context.setProject(QgsProject.instance())
    alg.prepare(params, context, feedback)
    return alg.run(params, context, feedback)[1]


def main():
    app = QgsApplication([], False)
    app.initQgis()
    try:
        # the "processing" plugin is not on sys.path outside a running QGIS
        sys.path.append(os.path.join(QgsApplication.pkgDataPath(), "python", "plugins"))
        # the bridge method's compiled dependency (no bridge sample data, so import it here)
        from skimage.filters import threshold_otsu  # noqa: F401
        from skimage.morphology import binary_opening, disk, skeletonize  # noqa: F401
        import image_quality_toolset as plugin
        from processing.core.Processing import Processing
        Processing.initialize()

        widget = ResultSink()
        provider = plugin.ImageQualityToolSetProvider(widget)
        assert app.processingRegistry().addProvider(provider)

        for name, (alg_name, raster, roi, extra, keys) in CASES.items():
            alg = provider.algorithm(alg_name).create()
            assert alg.flags() & QgsProcessingAlgorithm.FlagNoThreading, f"{alg_name}: must run on the main thread"

            rlayer = QgsRasterLayer(os.path.join(DATA, raster), "r")
            vlayer = QgsVectorLayer(os.path.join(DATA, roi), "roi", "ogr")
            assert rlayer.isValid() and vlayer.isValid(), f"{name}: cannot open test data"
            out = tempfile.mkdtemp()
            params = dict(RASTER=rlayer, BAND=1, ROI=vlayer, OUTPUT_DIRECTORY=out, **extra)

            assert run(alg, params), f"{name}: algorithm failed"

            QgsApplication.processEvents()
            assert widget.events, f"{name}: no result posted to the dock"
            mtf = widget.events.pop().getMTF()
            results = getattr(mtf, "results", None) or vars(mtf)
            for key, expected in keys.items():
                val = results.get(key)
                assert val is not None and math.isfinite(float(val)), f"{name}: {key}={val}"
                assert math.isclose(float(val), expected, rel_tol=REL_TOL), f"{name}: {key}={float(val):.4f}, expected {expected}"
            if alg_name != "SNREstimator":  # the SNR algorithm writes no report
                assert os.path.exists(os.path.join(out, "report.gpkg")), f"{name}: no report written"
            print(f"OK: {name} " + ", ".join(f"{k}={float(results[k]):.3f}" for k in keys))
        check_bridge_angle_with_nan()
    finally:
        # no app.exitQgis(): tearing down after matplotlib/GDAL use can segfault (exit code 139)
        pass


def check_bridge_angle_with_nan():
    """The ROI cut-out is NaN outside the polygon: the bridge angle estimation must ignore those
    pixels (all-NaN normalisation made threshold_otsu raise "autodetected range of [nan, nan]")."""
    import numpy as np
    from osgeo import gdal
    from image_quality_toolset.algorithms.mtf_estimator.mtf_bridge import estimate_angle_from_bridge

    image = gdal.Open(os.path.join(DATA, "bridge", "simu_6px.tiff")).ReadAsArray().astype(float)
    masked = image.copy()
    rows, cols = np.indices(masked.shape)
    corner = 15  # cut two opposite corners, as a polygon ROI does in its bounding box
    masked[(rows + cols < corner) | (rows + cols > rows.max() + cols.max() - corner)] = np.nan
    assert np.isnan(masked).any()
    angle, angle_masked = estimate_angle_from_bridge(image), estimate_angle_from_bridge(masked)
    assert math.isclose(angle, angle_masked, abs_tol=0.5), f"bridge angle {angle_masked:.2f} with NaN, {angle:.2f} without"
    print(f"OK: bridge_angle_with_nan angle={angle_masked:.2f} ({int(np.isnan(masked).sum())} NaN pixels)")


if __name__ == "__main__":
    main()  # an assertion failure raises before the line below, giving a non-zero exit
    sys.stdout.flush()
    os._exit(0)
