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

from pathlib import Path
from matplotlib.pyplot import Figure
from qgis.core import QgsVectorLayer
from qgis.core import QgsProcessingFeedback
from .mtf_common import remove_nan_borders
import numpy as np

from abc import ABC, abstractmethod


class Mtf(ABC):

    """
    Abstract base class for Modulation Transfer Function (MTF) computation.

    This class defines the interface that all MTF computation classes must implement.
    It provides methods to compute MTF (Modulation Transfer Function), LSF (Line
    Spread Function), and ESF (Edge Spread Function), along with unified report generation.

    The class is designed to work both within QGIS processing framework and as a
    standalone tool for testing purposes, providing flexibility in usage contexts.

    Attributes
    ----------
    raster : raster layer
        Input image for MTF analysis
        A raster layer object that can be read by GDAL
    feedback : QgsProcessingFeedback
        Feedback object for progress reporting and logging in QGIS interface.
        Should be set to MockFeedback() when performing tests outside QGIS environment.
    parameters : dictionary
        A dictionary containing additional parameters inherent to subclasses

    Notes
    -----
    - This abstract class provides a common template for different MTF computation
      algorithms implemented in subclasses.
    """

    def __init__(self, roi, raster, feedback : QgsProcessingFeedback):

        self.roi = roi
        self.raster = raster
        self.feedback = feedback
        self.params = None

        self.image = self.readImage()

        # These parameters have associated property
        self._figure = None
        self._mtf = None
        self._lsf = None
        self._esf = None
        self._isValid = True

    @abstractmethod
    def figure(self) -> list[Figure]:
        # To be defined in subclasses
        # This method returns a list of matplotlib figures displayed in QGIS interface
        pass

    def console(self, message):
        self.feedback.pushInfo(message)

    @abstractmethod
    def computeEsf(self) -> np.ndarray:
        # To be defined in subclasses
        pass

    @property
    def esf(self):
        if self._esf is None:
            self._esf = self.computeEsf()
        return self._esf

    def isValid(self):
        """
        Return True if computed MTF is valid
        """
        return self._isValid


    @abstractmethod
    def computeLsf (self) -> np.ndarray:
        # To be defined in subclasses
        pass

    @property
    def lsf(self):
        if self._lsf is None:
            self._lsf = self.computeLsf()
        return self._lsf

    @abstractmethod
    def computeMtf(self) -> np.ndarray:
        # To be defined in subclasses
        pass

    @property
    def mtf(self):
        if self._mtf is None:
            self._mtf = self.computeMtf()
        return self._mtf

    def readImage(self) -> np.ndarray:
        rows = self.raster.RasterYSize
        cols = self.raster.RasterXSize
        band = self.raster.GetRasterBand(1)
        image = np.float64(band.ReadAsArray(0, 0, cols, rows))
        return remove_nan_borders(image)

    def getRoi(self):
        return self.roi
