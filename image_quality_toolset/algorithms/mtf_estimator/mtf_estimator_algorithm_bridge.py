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

from qgis.core import QgsProcessingParameterNumber, QgsProcessingParameterEnum
from .base_mtf_estimator_algorithm import BaseMTFEstimatorAlgorithm
from .mtf_bridge import MtfBridge
from ..tools.raster_tools import roi_extraction

from qgis.PyQt.QtCore import QCoreApplication
from image_quality_toolset.result_dockwidget import MTFEvent


class MTFEstimatorAlgorithmBridge(BaseMTFEstimatorAlgorithm):
    """MTF Estimator using MtfBridge."""

    ALGORITHM_NAME = 'MTFEstimatorBridge'
    DISPLAY_NAME = 'Bridge Method MTF'
    GROUP_NAME = 'MTF'
    GROUP_ID = 'mtf'

    SCALE = 'SCALE'
    OFFSET = 'OFFSET'
    PX_MARGIN = 'PX_MARGIN'
    BRIDGE_WIDTH = 'BRIDGE_WIDTH'
    EDGE_DIRECTION = 'EDGE_DIRECTION'
    SAMPLING = 'SAMPLING'
    INPUT_ROTATION = 'INPUT_ROTATION'

    EDGE_DIRECTION_OPTIONS = ['Along Track', 'Across Track']
    EDGE_DIRECTION_VALUES = ['AL', 'CT']

    ESF_MODEL = 'esf_to_eq_space_polynomial'


    def createInstance(self):
        return MTFEstimatorAlgorithmBridge(self.result_widget)
    
    def shortHelpString(self):
        return self.tr('The Bridge Method is an MTF measurement technique that uses a bridge-shaped test target consisting of two closely spaced edges forming a narrow “bridge” or slit. By imaging this structure, the system produces an intensity profile that combines the responses of the two edges. From this profile, the Line Spread Function (LSF) can be derived, and its Fourier transform yields the Modulation Transfer Function (MTF).')

    def create_mtf(self, vlayer, memraster, band_n, scale, offset, px_margin, bridge_width, edge_direction, sampling, input_angle, feedback, debug_dir=None):
        return MtfBridge(vlayer, memraster, band_n, scale, offset, px_margin, bridge_width, edge_direction, self.ESF_MODEL, sampling, input_angle, feedback, debug_dir=debug_dir)

    def initAlgorithm(self, config=None):

        self.addParameter(
            QgsProcessingParameterNumber(
                self.SCALE,
                self.tr("Scale"),
                type=QgsProcessingParameterNumber.Type.Double,
                defaultValue=1.00
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
        self.addParameter(
            QgsProcessingParameterNumber(
                self.PX_MARGIN,
                self.tr("Pixel margin"),
                type=QgsProcessingParameterNumber.Type.Integer,
                defaultValue=0
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.BRIDGE_WIDTH,
                self.tr("Bridge width (pixels)"),
                type=QgsProcessingParameterNumber.Type.Double,
                defaultValue=6.0
            )
        )
        self.addParameter(
            QgsProcessingParameterEnum(
                self.EDGE_DIRECTION,
                self.tr("Edge direction"),
                options=self.EDGE_DIRECTION_OPTIONS,
                defaultValue=0
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.SAMPLING,
                self.tr("Sampling (oversampling factor)"),
                type=QgsProcessingParameterNumber.Type.Double,
                defaultValue=0.1,
                minValue=0.0,
                maxValue=1.0
            )
        )
        input_rotation_param = QgsProcessingParameterNumber(
            self.INPUT_ROTATION,
            self.tr("Input rotation angle (degrees)"),
            type=QgsProcessingParameterNumber.Type.Double,
            optional=True
        )
        input_rotation_param.setHelp(self.tr("If left empty, the rotation angle is automatically estimated from the edge."))
        self.addParameter(input_rotation_param)
        super().initAlgorithm(config=config)
        
    def processAlgorithm(self, parameters, context, feedback):
        scale = self.parameterAsDouble(parameters, self.SCALE, context)
        offset = self.parameterAsDouble(parameters, self.OFFSET, context)
        px_margin = self.parameterAsInt(parameters, self.PX_MARGIN, context)
        bridge_width = self.parameterAsDouble(parameters, self.BRIDGE_WIDTH, context)
        edge_direction_index = self.parameterAsEnum(parameters, self.EDGE_DIRECTION, context)
        edge_direction = self.EDGE_DIRECTION_VALUES[edge_direction_index]
        sampling = self.parameterAsDouble(parameters, self.SAMPLING, context)
        # optional: None when left empty (parameterAsDouble would return 0, a valid angle)
        input_angle = self.parameterAsDouble(parameters, self.INPUT_ROTATION, context) if parameters.get(self.INPUT_ROTATION) is not None else None
        raster_layer = self.parameterAsRasterLayer(parameters, self.RASTER, context)
        band_n = self.parameterAsInt(parameters, self.BAND, context)
        vlayer = self.parameterAsVectorLayer(parameters, self.ROI, context)

        output_directory = self.parameterAsFile(parameters, self.OUTPUT_DIRECTORY, context)

        memraster = roi_extraction(raster_layer, band_n, vlayer, context, feedback)

        mtf = self.create_mtf(vlayer, memraster, band_n, scale, offset, px_margin, bridge_width, edge_direction, sampling, input_angle, feedback, output_directory)

        self.process_results( mtf, parameters, context, feedback )

        return {}
