# -------------------------------------------------------------------------
# Copyright (C) 2025 Telespazio
# -------------------------------------------------------------------------
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
# -------------------------------------------------------------------------

import numpy as np
import osgeo
from osgeo import gdal, ogr, osr
from qgis.core import QgsProcessingException


def compute_raster_extent(memlayer, gt, xsize, ysize, pxoffset=5):
    """Compute extent in raster coordinates and return sub-raster parameters."""
    e = np.array(memlayer.GetExtent()).copy()
    e = np.reshape(e, [2, 2])
    e = np.array(np.meshgrid(e[0], e[1]))
    E = e.T.reshape(-1, 2)
    m = np.reshape(np.array(gt).copy(), [2, 3])
    A = m[:, 0]
    m = m[:, 1:]
    M = np.linalg.inv(m)
    col_list, row_list = np.matmul(M, (E-A).T)

    col_min = int(np.max([np.floor(np.min(col_list)) - pxoffset, 1]))
    col_max = int(np.min([np.ceil(np.max(col_list)) + pxoffset, xsize - 1]))
    row_min = int(np.max([np.floor(np.min(row_list)) - pxoffset, 1]))
    row_max = int(np.min([np.ceil(np.max(row_list)) + pxoffset, ysize - 1]))

    sub_gt = gt.copy()
    sub_gt[0] = gt[0] + gt[1] * col_min + gt[2] * row_min
    sub_gt[3] = gt[3] + gt[4] * col_min + gt[5] * row_min
    sub_xsize = int(col_max - col_min)
    sub_ysize = int(row_max - row_min)

    return col_min, row_min, sub_xsize, sub_ysize, sub_gt


def rasterize(image_array, geotransform=None, projection=None, datatype=None):
    """
    Convert a numpy array to a GDAL memory raster.

    Args:
        image_array: 2D numpy array representing the image
        geotransform: Optional GDAL geotransform tuple (default: identity transform)
        projection: Optional projection WKT string
        datatype: Optional GDAL data type (default: auto-detect from array dtype)

    Returns:
        GDAL memory raster containing the image data
    """
    if len(image_array.shape) != 2:
        raise ValueError("Input array must be 2D")

    rows, cols = image_array.shape

    # Auto-detect GDAL datatype if not provided
    if datatype is None:
        if image_array.dtype == np.uint8:
            datatype = gdal.GDT_Byte
        elif image_array.dtype == np.uint16:
            datatype = gdal.GDT_UInt16
        elif image_array.dtype == np.int16:
            datatype = gdal.GDT_Int16
        elif image_array.dtype == np.uint32:
            datatype = gdal.GDT_UInt32
        elif image_array.dtype == np.int32:
            datatype = gdal.GDT_Int32
        elif image_array.dtype == np.float32:
            datatype = gdal.GDT_Float32
        elif image_array.dtype == np.float64:
            datatype = gdal.GDT_Float64
        else:
            datatype = gdal.GDT_Float64

    # Create memory raster
    memraster_drv = gdal.GetDriverByName("MEM")
    memraster = memraster_drv.Create("", cols, rows, 1, datatype)

    # Set geotransform if provided
    if geotransform is not None:
        memraster.SetGeoTransform(geotransform)

    # Set projection if provided
    if projection is not None:
        memraster.SetProjection(projection)

    # Write array data to raster band
    memband = memraster.GetRasterBand(1)
    memband.WriteArray(image_array)

    return memraster


def roi_extraction(raster_layer, band_n, vlayer, context, feedback):
    """
    Extract a region of interest (ROI) from a raster layer based on a vector layer.

    Args:
        raster_layer: QgsRasterLayer object
        band_n: Band number to extract
        vlayer: QgsVectorLayer defining the ROI
        context: QgsProcessingContext
        feedback: QgsProcessingFeedback for logging

    Returns:
        GDAL memory raster containing the masked ROI data
    """
    from qgis.core import QgsProcessingFeatureSource

    if raster_layer is None:
        raise QgsProcessingException("Invalid input raster: the layer could not be loaded.")
    if vlayer is None:
        raise QgsProcessingException("Invalid ROI: the vector layer could not be loaded.")
    featureSource = QgsProcessingFeatureSource(vlayer, context)
    gdal_layer = gdal.Open(raster_layer.source(), gdal.GA_ReadOnly)
    if gdal_layer is None:
        raise QgsProcessingException(f"GDAL cannot open the raster: {raster_layer.source()}")
    if not 1 <= band_n <= gdal_layer.RasterCount:
        raise QgsProcessingException(
            f"Band {band_n} does not exist: the raster has {gdal_layer.RasterCount} band(s).")
    gt = list(gdal_layer.GetGeoTransform())
    xsize = gdal_layer.RasterXSize
    ysize = gdal_layer.RasterYSize
    band = gdal_layer.GetRasterBand(band_n)
    raster_srs = osr.SpatialReference()
    raster_proj = gdal_layer.GetProjection()
    if raster_proj:
        raster_srs.ImportFromWkt(raster_proj)
    elif raster_layer.crs().isValid() and raster_layer.crs().authid():
        # GDAL saw no projection but QGIS resolved one (e.g. from RPC
        # metadata). Adopt QGIS's CRS and synthesize a geotransform from
        # the QGIS extent — an affine approximation of the RPC mapping.
        raster_srs.ImportFromWkt(raster_layer.crs().toWkt())
        extent = raster_layer.extent()
        gt = [
            extent.xMinimum(),
            extent.width() / xsize,
            0.0,
            extent.yMaximum(),
            0.0,
            -extent.height() / ysize,
        ]
        raster_proj = raster_srs.ExportToWkt()
        feedback.pushInfo(
            f"Raster has no native CRS; using QGIS-resolved CRS "
            f"{raster_layer.crs().authid()} with synthesized geotransform."
        )
    vector_srs = osr.SpatialReference()
    if featureSource.sourceCrs().isValid():  # a ROI may have no CRS (no .prj), like a raw image
        vector_srs.ImportFromWkt(featureSource.sourceCrs().toWkt())

    # https://gdal.org/tutorials/osr_api_tut.html#crs-and-axis-order
    if int(osgeo.__version__[0]) >= 3:
        # GDAL 3 changes axis order: https://github.com/OSGeo/gdal/issues/1546
        raster_srs.SetAxisMappingStrategy(osgeo.osr.OAMS_TRADITIONAL_GIS_ORDER)
        vector_srs.SetAxisMappingStrategy(osgeo.osr.OAMS_TRADITIONAL_GIS_ORDER)

    if str(raster_srs) == "":
        coord_transform = None
        feedback.pushInfo("WARNING: Raster with no CRS")
        gt[5] = -1 * gt[5]
    elif str(vector_srs) == "":
        coord_transform = None
        feedback.pushInfo("WARNING: ROI with no CRS, its coordinates are taken in the raster CRS")
    else:
        coord_transform = osr.CoordinateTransformation(vector_srs, raster_srs)

    feedback.pushInfo(f"vector srs: {vector_srs.GetName()}")
    feedback.pushInfo(f"raster srs: {raster_srs.GetName()}")

    memlayer_drv = ogr.GetDriverByName("Memory")
    memlayer_ds = memlayer_drv.CreateDataSource("")
    memlayer = memlayer_ds.CreateLayer("aoi", raster_srs, geom_type=ogr.wkbPolygon)
    memlayer.CreateField(ogr.FieldDefn("id", ogr.OFTInteger))
    featureDefn = memlayer.GetLayerDefn()

    for qgs_feature in featureSource.getFeatures():
        featureDefn = memlayer.GetLayerDefn()
        memfeat = ogr.Feature(featureDefn)
        geom = qgs_feature.geometry()
        if geom.isNull() or geom.isEmpty():
            continue
        feedback.pushInfo(geom.asWkt())
        geom = geom.asWkb()
        geom = ogr.CreateGeometryFromWkb(geom)
        if not coord_transform is None:
            try:
                geom.Transform(coord_transform)
            except RuntimeError as e:
                feedback.reportError(
                    f"Coordinate transformation failed: {e}\n"
                    f"This may be caused by a CRS mismatch between the vector and raster layers.\n"
                    f"Vector CRS: {vector_srs.GetName()}\n"
                    f"Raster CRS: {raster_srs.GetName()}\n"
                    f"Please ensure the vector layer has the correct CRS defined."
                )
                raise
        feedback.pushInfo(f"Geometry: {geom}")

        memfeat.SetGeometry(geom)
        memlayer.CreateFeature(memfeat)

    if memlayer.GetFeatureCount() == 0:
        raise QgsProcessingException("The ROI layer has no feature with a geometry.")

    # Compute extent in raster coordinates
    col_min, row_min, sub_xsize, sub_ysize, sub_gt = compute_raster_extent(
        memlayer, gt, xsize, ysize
    )

    if sub_xsize <= 0 or sub_ysize <= 0:
        raise QgsProcessingException(
            "The ROI does not overlap the raster. Check that the ROI covers the image "
            "(and that its CRS is defined correctly).")

    memraster_drv = gdal.GetDriverByName("MEM")
    memraster = memraster_drv.Create("", sub_xsize, sub_ysize, 1, gdal.GDT_Float64)

    memraster.SetProjection(raster_proj)
    memraster.SetGeoTransform(sub_gt)
    memband = memraster.GetRasterBand(1)
    memband.WriteArray(np.zeros([sub_ysize, sub_xsize]))
    gdal.RasterizeLayer(memraster, [1], memlayer, burn_values=[1])
    mask = memband.ReadAsArray(0, 0, sub_xsize, sub_ysize)
    data = band.ReadAsArray(col_min, row_min, sub_xsize, sub_ysize).astype(np.float64)
    data[mask == 0] = np.nan
    if np.all(np.isnan(data)):
        raise QgsProcessingException("The ROI contains no pixel of the raster.")
    memband.WriteArray(data)
    mask = None

    return memraster
