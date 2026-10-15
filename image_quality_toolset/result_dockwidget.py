# -------------------------------------------------------------------------
# Copyright (C) 2026 Telespazio TIM
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

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.backends.backend_qtagg import NavigationToolbar2QT as NavigationToolbar
from processing.tools import dataobjects
from qgis.core import (
    QgsApplication,
    QgsProcessingAlgRunnerTask,
    QgsProcessingFeedback,
)
from qgis.gui import QgsDockWidget
from qgis.PyQt.QtCore import QEvent, QRectF, QSize, Qt
from qgis.PyQt.QtGui import QPainter
from qgis.PyQt.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .algorithms.mtf_estimator.mtf import Mtf


class MTFEvent(QEvent):
    EVENT_TYPE = QEvent.registerEventType()

    def __init__(self, algorithmId : str, mtf: Mtf, parameters: dict):
        super().__init__(MTFEvent.EVENT_TYPE)
        self.mtf = mtf
        self.parameters = parameters
        self.algorithmId = algorithmId

    def getMTF(self):
        return self.mtf

    def getParameters(self):
        return self.parameters

    def getAlgorithmId(self):
        return self.algorithmId


class BackgroundWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.logo_renderer = None
        self.logo_path = os.path.join(
            os.path.dirname(__file__), "icons", "telespazio.svg"
        )

        # Load the SVG
        from qgis.PyQt.QtSvg import QSvgRenderer

        self.logo_renderer = QSvgRenderer(self.logo_path)

    def paintEvent(self, event):
        painter = QPainter(self)

        # Draw the background
        painter.fillRect(self.rect(), self.palette().window())

        if self.logo_renderer:
            # Scale the logo to 100x100
            scaled_size = QSize(100, 100)

            x = (self.width() - scaled_size.width()) // 2
            y = (self.height() - scaled_size.height()) // 2

            # Set opacity to 50%
            painter.setOpacity(0.2)

            # Render the SVG
            target_rect = QRectF(x, y, scaled_size.width(), scaled_size.height())
            self.logo_renderer.render(painter, target_rect)

            # Reset opacity for other elements
            painter.setOpacity(1.0)


class PlotWindow(QgsDockWidget):
    def __init__(self, parent=None):
        super().__init__(parent)

        self.mtf = None
        self.parameters = None
        self.algorithmId = None
        self._tasks = {}
        self._all_messages = []
        self._last_error_message = None

        self.setWindowTitle("MTF Tool statistics")
        self.setObjectName("ImageQualityToolset_Results")

        # Create the main placeholder widget with background
        placeholder_widget = BackgroundWidget()
        self.content_layout = QVBoxLayout()

        # Add stretch to center the content
        self.content_layout.addStretch()

        # Create the main instruction label
        self.placeholder_label = QLabel()
        self.placeholder_label.setText(
            "This plugin provides Modulation Transfer Function (MTF) estimation.\n\n"
            "To view results, run the Image Quality Toolset processing algorithm\n"
            "from the Processing Toolbox (Menu > Processing > Toolbox)"
        )
        self.placeholder_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.placeholder_label.setWordWrap(True)
        self.content_layout.addWidget(self.placeholder_label)

        # Add stretch to center the content
        self.content_layout.addStretch()

        # Progress bar container (initially hidden)
        self.progress_container = QWidget()
        progress_layout = QVBoxLayout()
        self.progress_container.setLayout(progress_layout)
        progress_layout.setContentsMargins(0, 5, 0, 5)
        progress_layout.setSpacing(4)

        # Progress bar (indeterminate)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)  # Indeterminate
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setMaximumHeight(6)
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                background-color: #e0e0e0;
                border: none;
                border-radius: 3px;
            }
            QProgressBar::chunk {
                background-color: #1976D2;
                border-radius: 3px;
            }
        """)
        progress_layout.addWidget(self.progress_bar)

        # Status label
        self.progress_label = QLabel("Checking dependencies...")
        self.progress_label.setStyleSheet("font-size: 10px; color: #666;")
        self.progress_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        progress_layout.addWidget(self.progress_label)

        # Error button (hidden by default)
        self.error_button = QPushButton("⚠ View Details")
        self.error_button.setMaximumWidth(120)
        self.error_button.clicked.connect(self._show_error_log)
        self.error_button.hide()
        self.error_button.setStyleSheet("font-size: 10px; color: #d32f2f;")
        progress_layout.addWidget(self.error_button)
        progress_layout.setAlignment(self.error_button, Qt.AlignmentFlag.AlignCenter)

        self.content_layout.addWidget(self.progress_container)
        self.progress_container.hide()

        # Add the company attribution at the bottom
        company_label = QLabel("<i>by Telespazio France</i>")
        company_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.content_layout.addWidget(company_label)

        placeholder_widget.setLayout(self.content_layout)
        self.setWidget(placeholder_widget)

    def _show_error_log(self):
        """Show the full dependency check/install log."""
        dialog = QDialog(self)
        dialog.setWindowTitle("Dependency Check/Installation Log")
        dialog.setMinimumSize(600, 500)

        layout = QVBoxLayout()
        dialog.setLayout(layout)

        layout.addWidget(QLabel("<b>Full Log</b>"))

        log_text = QTextEdit()
        log_text.setReadOnly(True)
        log_text.setStyleSheet("font-family: monospace; font-size: 11px;")
        log_text.setText("\n".join(self._all_messages))
        layout.addWidget(log_text)

        dialog.exec()

    def set_progress_visible(self, visible: bool):
        """Show or hide the progress bar."""
        if hasattr(self, "progress_container"):
            self.progress_container.setVisible(visible)

    def set_progress_status(self, message: str, is_error: bool = False):
        """Update the progress status message."""
        if hasattr(self, "progress_label"):
            self.progress_label.setText(message)
            # Store message for log
            if hasattr(self, "_all_messages"):
                self._all_messages.append(message)

            if is_error:
                self.progress_label.setStyleSheet(
                    "font-size: 10px; color: #d32f2f; font-weight: bold;"
                )
                self.error_button.show()
                # Hide progress bar on error, keep message and button
                if hasattr(self, "progress_bar"):
                    self.progress_bar.hide()
            else:
                self.progress_label.setStyleSheet("font-size: 10px; color: #666;")
                self.error_button.hide()

    def set_error_message(self, message: str):
        """Store the last error message for display."""
        if hasattr(self, "_all_messages"):
            self._all_messages.append(message)
            self._last_error_message = "\n".join(self._all_messages)

    def event(self, event: QEvent):
        if event.type() != MTFEvent.EVENT_TYPE:
            return super(PlotWindow, self).event(event)

        if self.mtf is not None:
            self.mtf.getRoi().geometryChanged.disconnect(self.on_roi_changed)

        widget = QWidget()

        self.mtf = event.getMTF()
        self.parameters = event.getParameters()
        self.algorithmId = event.getAlgorithmId()

        self.mtf.getRoi().geometryChanged.connect(self.on_roi_changed)

        figure = self.mtf.figure()[0]
        canvas = FigureCanvas(figure)
        toolbar = NavigationToolbar(canvas, widget)

        layout = QVBoxLayout()
        layout.addWidget(toolbar)
        layout.addWidget(canvas)
        widget.setLayout(layout)

        # TODO do we have to delete old QWidget ?
        self.setWidget(widget)

        # Hide the placeholder label when real results are available
        if hasattr(self, "placeholder_label"):
            self.placeholder_label.setParent(None)

        self.show()

        return True

    def on_roi_changed(self):
        """
        Called whenever the algorithm region of interest has changed
        """
        feedback = QgsProcessingFeedback()
        context = dataobjects.createContext(feedback)
        algorithm = QgsApplication.processingRegistry().createAlgorithmById(
            self.algorithmId
        )

        task = QgsProcessingAlgRunnerTask(algorithm, self.parameters, context, feedback)
        self.mtf.getRoi().geometryChanged.connect(feedback.cancel)

        self._tasks[task] = (context, feedback, algorithm)

        def onTaskFinished():
            del self._tasks[task]

        task.taskCompleted.connect(onTaskFinished)
        task.taskTerminated.connect(onTaskFinished)

        QgsApplication.taskManager().addTask(task)
