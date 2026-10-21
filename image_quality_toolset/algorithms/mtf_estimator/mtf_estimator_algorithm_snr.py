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

from qgis.core import QgsProcessingParameterNumber
from qgis.PyQt.QtCore import QCoreApplication
from image_quality_toolset.result_dockwidget import MTFEvent

from .base_mtf_estimator_algorithm import BaseMTFEstimatorAlgorithm
from .snr import SNR
from ..tools.raster_tools import roi_extraction


class MTFEstimatorAlgorithmSNR(BaseMTFEstimatorAlgorithm):
    """SNR Estimator."""

    ALGORITHM_NAME = 'SNREstimator'
    DISPLAY_NAME = 'Variogram Method SNR'
    GROUP_NAME = 'SNR'
    GROUP_ID = 'snr'

    WINDOW_SIZE = 'WINDOW_SIZE'
    SNR_PRECISION = 'SNR_PRECISION'
    L_MIN = 'L_MIN'
    L_MAX = 'L_MAX'
    SCALE = 'SCALE'
    OFFSET = 'OFFSET'

    def createInstance(self):
        return MTFEstimatorAlgorithmSNR(self.result_widget)
    
    def shortHelpString(self):
        return self.tr('The Variogram Method is a noise-estimation technique used mainly in remote sensing image quality assessment. It analyzes the spatial autocorrelation of pixel values in homogeneous areas of an image. By computing the variogram—a function describing how pixel value differences increase with distance—the method estimates the noise variance from the variogram’s nugget, which represents the random, uncorrelated component attributed to sensor noise. From this noise estimate, the Signal-to-Noise Ratio (SNR) is derived.')

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterNumber(
                self.WINDOW_SIZE,
                self.tr("Window size"),
                type=QgsProcessingParameterNumber.Type.Integer,
                defaultValue=5
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.SNR_PRECISION,
                self.tr("SNR precision"),
                type=QgsProcessingParameterNumber.Type.Double,
                defaultValue=1.0
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.L_MIN,
                self.tr("L min (radiance minimum)"),
                type=QgsProcessingParameterNumber.Type.Double,
                defaultValue=0.0
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.L_MAX,
                self.tr("L max (radiance maximum)"),
                type=QgsProcessingParameterNumber.Type.Double,
                defaultValue=1.0
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.SCALE,
                self.tr("Scale"),
                type=QgsProcessingParameterNumber.Type.Double,
                defaultValue=1.0
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.OFFSET,
                self.tr("Offset"),
                type=QgsProcessingParameterNumber.Type.Double,
                defaultValue=0.0
            )
        )
        super().initAlgorithm(config=config)

    def processAlgorithm(self, parameters, context, feedback):
        window_size = self.parameterAsInt(parameters, self.WINDOW_SIZE, context)
        snr_precision = self.parameterAsDouble(parameters, self.SNR_PRECISION, context)
        l_min = self.parameterAsDouble(parameters, self.L_MIN, context)
        l_max = self.parameterAsDouble(parameters, self.L_MAX, context)

        # Get user-provided scale and offset parameters
        user_scale = self.parameterAsDouble(parameters, self.SCALE, context)
        user_offset = self.parameterAsDouble(parameters, self.OFFSET, context)

        # Convert 0.0 to None for L_min/L_max parameters
        l_min = l_min if l_min != 0.0 else None
        l_max = l_max if l_max != 0.0 else None

        raster_layer = self.parameterAsRasterLayer(parameters, self.RASTER, context)
        band_n = self.parameterAsInt(parameters, self.BAND, context)
        vlayer = self.parameterAsVectorLayer(parameters, self.ROI, context)

        memraster = roi_extraction(raster_layer, band_n, vlayer, context, feedback)

        # Read scale and offset from raster metadata
        band = memraster.GetRasterBand(1)
        scale = band.GetScale()
        offset = band.GetOffset()

        # Use user-provided values if metadata values are not available
        if scale is None:
            scale = user_scale
            feedback.pushInfo(f"Using user-provided scale: {scale}")
        else:
            feedback.pushInfo(f"Using scale from raster metadata: {scale}")

        if offset is None:
            offset = user_offset
            feedback.pushInfo(f"Using user-provided offset: {offset}")
        else:
            feedback.pushInfo(f"Using offset from raster metadata: {offset}")

        mtf = self.create_mtf(vlayer, memraster, band_n, window_size, snr_precision, l_min, l_max, scale, offset, feedback)

        if not mtf.feedback.isCanceled():
            QCoreApplication.postEvent(self.result_widget, MTFEvent(self.id(), mtf, parameters))

        return {}

    def create_mtf(self, vlayer, memraster, band_n, window_size, snr_precision, l_min, l_max, scale, offset, feedback):
        rows = memraster.RasterYSize
        cols = memraster.RasterXSize
        band = memraster.GetRasterBand(1)
        image = np.float64(band.ReadAsArray(0, 0, cols, rows))

        # Validate image data
        if image.size == 0:
            raise ValueError("Empty image data. The ROI extraction may have failed.")

        # Check for NaN or Inf values
        if np.all(np.isnan(image)):
            raise ValueError("Image contains only NaN values. Check the input raster and ROI.")

        if np.all(np.isinf(image)):
            raise ValueError("Image contains only infinite values. Check the input raster.")

        # Warn if significant NaN content
        nan_ratio = np.sum(np.isnan(image)) / image.size
        if nan_ratio > 0.5:
            feedback.pushWarning(f"Image contains {nan_ratio*100:.1f}% NaN values. Results may be unreliable.")

        snr = SNR(
            roi=vlayer,
            image=image,
            band_number=band_n,
            window_size=window_size,
            snr_precision=snr_precision,
            L_min=l_min,
            L_max=l_max,
            scale=scale,
            offset=offset,
            feedback=feedback
        )
        snr.variogram_snr()
        snr.compute_peak_snr()
        snr.second_method()
        return snr
