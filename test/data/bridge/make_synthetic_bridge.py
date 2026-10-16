"""Generate the synthetic bridge image and ROI used by test_algorithms.py (needs numpy, scipy, osgeo).

A 6 px wide bright bridge on a dark background, tilted by ANGLE degrees, blurred by a Gaussian PSF.
For a Gaussian PSF the true MTF is exp(-2 pi^2 sigma^2 f^2) (f in cycles/pixel), so the expected
values are known:  MTF@Nyquist = 0.169, MTF50 = 0.312 for SIGMA = 0.6.

Run: python make_synthetic_bridge.py   (writes next to this script)
"""
import os

import numpy as np
from osgeo import gdal, ogr, osr
from scipy.ndimage import gaussian_filter

N, WIDTH, ANGLE, SIGMA = 120, 6.0, 5.0, 0.6
LOW, HIGH, NOISE = 500.0, 3000.0, 5.0
HERE = os.path.dirname(os.path.abspath(__file__))
ORIGIN_X, ORIGIN_Y = 500000.0, 5000000.0  # EPSG:32632, 1 m pixels

# the bridge is rendered 8x oversampled, blurred by the Gaussian PSF, then point-sampled (no pixel
# box average), so the total PSF is exactly the Gaussian
K = 8
y, x = (np.mgrid[0:N * K, 0:N * K] + 0.5) / K
d = (x - N / 2) * np.cos(np.radians(ANGLE)) - (y - N / 2) * np.sin(np.radians(ANGLE))
fine = (np.abs(d) <= WIDTH / 2).astype(float)
img = gaussian_filter(fine, SIGMA * K, mode="nearest")[K // 2::K, K // 2::K] * (HIGH - LOW) + LOW
img = img + np.random.default_rng(0).normal(0, NOISE, img.shape)

srs = osr.SpatialReference()
srs.ImportFromEPSG(32632)
tif = os.path.join(HERE, "simu_6px.tiff")
ds = gdal.GetDriverByName("GTiff").Create(tif, N, N, 1, gdal.GDT_UInt16)
ds.SetGeoTransform((ORIGIN_X, 1, 0, ORIGIN_Y, 0, -1))
ds.SetProjection(srs.ExportToWkt())
ds.GetRasterBand(1).WriteArray(np.clip(img, 0, 65535).round().astype(np.uint16))
ds = None

m = 10  # ROI: the image minus a 10 px margin
shp = os.path.join(HERE, "shapefile", "roi.shp")
os.makedirs(os.path.dirname(shp), exist_ok=True)
drv = ogr.GetDriverByName("ESRI Shapefile")
if os.path.exists(shp):
    drv.DeleteDataSource(shp)
vs = drv.CreateDataSource(shp)
lyr = vs.CreateLayer("roi", srs, ogr.wkbPolygon)
ring = ogr.Geometry(ogr.wkbLinearRing)
for px, py in ((m, m), (N - m, m), (N - m, N - m), (m, N - m), (m, m)):
    ring.AddPoint(ORIGIN_X + px, ORIGIN_Y - py)
poly = ogr.Geometry(ogr.wkbPolygon)
poly.AddGeometry(ring)
feat = ogr.Feature(lyr.GetLayerDefn())
feat.SetGeometry(poly)
lyr.CreateFeature(feat)
vs = None
print("wrote", tif, shp)
