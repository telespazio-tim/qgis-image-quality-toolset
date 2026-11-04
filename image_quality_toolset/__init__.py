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

import os
import sys
import logging

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QWidget, QVBoxLayout, QLabel, QProgressBar, QPushButton, QTextEdit, QDialog
from qgis.core import (Qgis, QgsProcessingProvider, QgsApplication)

from .algorithms.mtf_estimator.mtf_estimator_algorithm_knife_edge import MTFEstimatorAlgorithmKnifeEdge
from .algorithms.mtf_estimator.mtf_estimator_algorithm_bridge import MTFEstimatorAlgorithmBridge
from .algorithms.mtf_estimator.mtf_estimator_algorithm_snr import MTFEstimatorAlgorithmSNR
from .result_dockwidget import PlotWindow, MTFEvent

# Setup logger
logger = logging.getLogger("ImageQualityToolset")

if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


class ImageQualityToolSet:

    def __init__(self, iface):
        self.iface = iface
        self.dependencies_ok = False
        self.dock_widget = None
        self.provider = None
        self._background_checker = None
        self._initialized = False

    def initGui(self):
        """Initialize GUI - called by QGIS automatically."""
        if self._initialized:
            return

        print("=" * 80)
        print("ImageQualityToolSet: initGui() called")
        print("=" * 80)

        # 1. Create the single dock container immediately
        if not self.dock_widget:
            self.dock_widget = PlotWindow()
            self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock_widget)

        # 2. Show the dock and start dependency check
        self.dock_widget.show()
        print("ImageQualityToolSet: Starting dependency check...")
        self._start_background_dependency_check()

        self._initialized = True

    def _start_background_dependency_check(self):
        """Start dependency checking in a background thread."""
        from .dependency_checker import DependencyInfo, check_dependencies_background

        plugin_dir = os.path.normpath(os.path.dirname(__file__))
        logger.info("Starting background dependency check...")

        # Define plugin dependencies
        IMAGE_QUALITY_TOOLSET_DEPENDENCIES = [
            DependencyInfo(name="NumPy", import_name="numpy", min_version="1.20.0"),
            DependencyInfo(name="SciPy", import_name="scipy", min_version="1.7.0"),
            DependencyInfo(name="Matplotlib", import_name="matplotlib", min_version="3.4.0"),
            DependencyInfo(name="loess", import_name="loess", min_version="2.1.2"),
            DependencyInfo(name="scikit-image", import_name="skimage", package="scikit-image"),
        ]

        # Start background check
        logger.info(f"Checking {len(IMAGE_QUALITY_TOOLSET_DEPENDENCIES)} dependencies...")
        self._background_checker = check_dependencies_background(
            self.iface,
            plugin_dir,
            IMAGE_QUALITY_TOOLSET_DEPENDENCIES,
            on_complete=self.on_dependencies_ready,
            result_dockwidget=self.dock_widget,
        )

    def on_dependencies_ready(self, success: bool):
        """Callback when background dependency check completes."""
        print("=" * 80)
        print(f"ImageQualityToolSet: Dependencies ready - success={success}")
        print("=" * 80)
        logger.info(f"Dependencies ready: success={success}")
        self.dependencies_ok = success

        if success:
            logger.info("All dependencies satisfied - registering provider...")
            self._register_provider()
            logger.info("Provider registered - algorithms available")
        else:
            logger.warning("Dependency installation failed - see result panel for details")

    def _register_provider(self):
        """Register the provider with QGIS processing registry."""
        print("=" * 80)
        print("ImageQualityToolSet: _register_provider() called")
        print("=" * 80)

        if not self.provider:
            print("Creating new provider...")
            self.provider = ImageQualityToolSetProvider(self.dock_widget)

        # Remove any existing provider first
        existing_provider = QgsApplication.processingRegistry().providerById('imagequalitytoolset')
        if existing_provider:
            print("Removing existing provider...")
            QgsApplication.processingRegistry().removeProvider(existing_provider)

        print("Adding provider to registry...")
        result = QgsApplication.processingRegistry().addProvider(self.provider)
        print(f"Provider add result: {result}")
        print(f"Provider ID: {self.provider.id()}")
        print(f"Provider name: {self.provider.name()}")
        print(f"Algorithms count: {len(list(self.provider.algorithms()))}")
        print("Provider registration complete")
        print("=" * 80)

    def unload(self):
        """Clean up plugin resources."""
        if self.provider:
            QgsApplication.processingRegistry().removeProvider(self.provider)
            self.provider = None

        if self.dock_widget:
            self.iface.removeDockWidget(self.dock_widget)
            self.dock_widget = None

        self._initialized = False


def classFactory(iface):
    """QGIS plugin entry point."""
    return ImageQualityToolSet(iface)


class ImageQualityToolSetProvider(QgsProcessingProvider):

    def __init__(self, result_dockwidget):
        super().__init__()
        self.result_dockwidget = result_dockwidget
        self._icon = QIcon(os.path.join(os.path.dirname(__file__), 'icons', 'tpz.png'))

    def loadAlgorithms(self):
        self.addAlgorithm(MTFEstimatorAlgorithmKnifeEdge(self.result_dockwidget))
        self.addAlgorithm(MTFEstimatorAlgorithmBridge(self.result_dockwidget))
        self.addAlgorithm(MTFEstimatorAlgorithmSNR(self.result_dockwidget))

    def id(self) -> str:
        return 'imagequalitytoolset'

    def name(self) -> str:
        return self.tr('ImageQualityToolSet')

    def icon(self) -> QIcon:
        return self._icon
