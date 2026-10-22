#-----------------------------------------------------------
# Copyright (C) 2025 Telespazio
#-----------------------------------------------------------
# Licensed under the terms of GNU GPL 2
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#---------------------------------------------------------------------

import os

from qgis.PyQt.QtCore import (
    QCoreApplication,
    QDateTime,
    QDir,
    QSettings,
    QMetaType )

from qgis.PyQt.QtGui import QIcon
from qgis.core import (
    Qgis,
    QgsCoordinateTransformContext,
    QgsFeature,
    QgsField,
    QgsFieldConstraints,
    QgsFields,
    QgsMultiPolygon,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterBand,
    QgsProcessingParameterFile,
    QgsProcessingParameterNumber,
    QgsProcessingParameterRasterLayer,
    QgsProcessingParameterVectorLayer,
    QgsProviderRegistry,
    QgsVectorFileWriter,
    QgsVectorLayer,
    )

import functools
import traceback

import image_quality_toolset
from image_quality_toolset.result_dockwidget import PlotWindow, MTFEvent
from ..tools.raster_tools import roi_extraction


def _safe_process(process):
    """Turn any error of processAlgorithm into a QgsProcessingException (message in the dialog,
    traceback in the log) instead of a bare Python traceback."""
    @functools.wraps(process)
    def wrapper(self, parameters, context, feedback):
        try:
            return process(self, parameters, context, feedback)
        except QgsProcessingException:
            raise
        except Exception as e:
            feedback.pushDebugInfo(traceback.format_exc())
            raise QgsProcessingException(f"{type(e).__name__}: {e}") from e
    return wrapper


class BaseMTFEstimatorAlgorithm(QgsProcessingAlgorithm):
    """Base class for MTF estimator algorithms."""

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if "processAlgorithm" in cls.__dict__:  # each algorithm overriding it is protected
            cls.processAlgorithm = _safe_process(cls.__dict__["processAlgorithm"])

    RASTER = 'RASTER'
    BAND = 'BAND'
    ROI = 'ROI'
    OUTPUT_DIRECTORY = 'OUTPUT_DIRECTORY'
    
    REPORT_FILENAME = "report.gpkg"

    OUTPUT_DIRECTORY_SETTING = "image_quality_toolset/output_directory"
    
    # Subclasses must define these
    MTF_CLASS = None
    ALGORITHM_NAME = 'MTFEstimator'
    DISPLAY_NAME = 'MTF Estimator'
    GROUP_NAME = 'MTF'
    GROUP_ID = 'mtf'

    def __init__(self, result_widget: PlotWindow):
        super().__init__()
        self.result_widget = result_widget
        self._icon = QIcon(os.path.join(os.path.dirname(image_quality_toolset.__file__), 'icons/mtf.png'))

    def tr(self, string):
        return QCoreApplication.translate('ImageQualityToolSet', string)

    def name(self):
        return self.ALGORITHM_NAME

    def displayName(self):
        return self.tr(self.DISPLAY_NAME)

    def group(self):
        return self.GROUP_NAME

    def groupId(self):
        return self.GROUP_ID

    def shortHelpString(self):
        return self.tr('MTF Method')

    def icon(self) -> QIcon:
        return self._icon

    def flags(self):
        # Run on the main thread: numpy/OpenBLAS overflows the 512 KB stack of
        # Qt pool threads on macOS (SIGBUS "Thread stack size exceeded").
        return super().flags() | Qgis.ProcessingAlgorithmFlag.NoThreading

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterRasterLayer(self.RASTER, self.tr("Raster layer")))
        self.addParameter(QgsProcessingParameterBand(self.BAND, self.tr("Band number"), 1, self.RASTER))
        self.addParameter(QgsProcessingParameterVectorLayer(self.ROI, self.tr("Area of interest")))
        self.addParameter(QgsProcessingParameterFile(self.OUTPUT_DIRECTORY, self.tr('Analysis output directory'), behavior=Qgis.ProcessingFileParameterBehavior.Folder,
                                                      optional=True, defaultValue=QSettings().value('', None)))


    @_safe_process
    def processAlgorithm(self, parameters, context, feedback):
        raster_layer = self.parameterAsRasterLayer(parameters, self.RASTER, context)
        band_n = self.parameterAsInt(parameters, self.BAND, context)
        vlayer = self.parameterAsVectorLayer(parameters, self.ROI, context)
        
        memraster = roi_extraction(raster_layer, band_n, vlayer, context, feedback)
        
        mtf = self.create_mtf(vlayer, memraster, band_n, feedback)

        self.process_results( mtf, parameters, context, feedback )
        
        return {}

    def create_mtf(self, vlayer, memraster, band_n, feedback):
        """Create the MTF object. Subclasses should override this method."""
        raise NotImplementedError("Subclasses must implement create_mtf()")

    def fields_and_values(self, current_datetime, figure_filename, parameters, context, feedback, mtf=None ):
        """
        Returns fields and values according to given parameters
        """

        fields = QgsFields ()
        values = []

        # fid
        fid_field = QgsField( "fid", QMetaType.Type.LongLong )

        constraints = QgsFieldConstraints()
        constraints.setConstraint( QgsFieldConstraints.Constraint.ConstraintNotNull, QgsFieldConstraints.ConstraintOrigin.ConstraintOriginProvider )
        constraints.setConstraint( QgsFieldConstraints.Constraint.ConstraintUnique, QgsFieldConstraints.ConstraintOrigin.ConstraintOriginProvider )
        constraints.setConstraintStrength( QgsFieldConstraints.Constraint.ConstraintNotNull, QgsFieldConstraints.ConstraintStrength.ConstraintStrengthHard );
        constraints.setConstraintStrength( QgsFieldConstraints.Constraint.ConstraintUnique, QgsFieldConstraints.ConstraintStrength.ConstraintStrengthHard );
        fid_field.setConstraints(constraints)
        
        fields.append( fid_field )
        values.append( None ) # automatically generated by provider

        # current date
        fields.append( QgsField( "date", QMetaType.Type.QDateTime ) )
        values.append( current_datetime )

        # algorithm log
        fields.append( QgsField( "log", QMetaType.Type.QString ) )
        values.append( feedback.textLog() )

        # algorithm log
        fields.append( QgsField( "figure_filename", QMetaType.Type.QString ) )
        values.append( figure_filename )
        
        for parameterDefinition in self.parameterDefinitions():

            if parameterDefinition.name() in [ self.ROI, self.OUTPUT_DIRECTORY ]:
                continue
            
            field_type = QMetaType.Type.QString
            if parameterDefinition.type() == "band" or ( parameterDefinition.type() == "number" and parameterDefinition.dataType() == QgsProcessingParameterNumber.Type.Integer ):
                fields.append( QgsField( parameterDefinition.name(), QMetaType.Type.Int ) )
                values.append( self.parameterAsInt( parameters, parameterDefinition.name(), context ) ) 
                
            elif parameterDefinition.type() == "number" and parameterDefinition.dataType() == QgsProcessingParameterNumber.Type.Double:
                fields.append( QgsField( parameterDefinition.name(), QMetaType.Type.Double ) )
                values.append( self.parameterAsDouble( parameters, parameterDefinition.name(), context ) )

            elif parameterDefinition.type() == "raster":
                raster_layer = self.parameterAsRasterLayer(parameters, self.RASTER, context)
                fields.append( QgsField( parameterDefinition.name(), QMetaType.Type.QString ) )
                values.append( raster_layer.dataProvider().dataSourceUri() )
                
            else:
                fields.append( QgsField( parameterDefinition.name(), QMetaType.Type.QString ) )
                values.append( self.parameterAsString( parameters, parameterDefinition.name(), context ) )
            
        # MTF results (float metrics only)
        _float_keys = {"esf_length", "MTF_NYQ", "MTF30", "MTF50",
                       "SNR", "RER", "HEE_upper", "HEE_lower", "FWHM", "R2"}
        if mtf is not None and mtf.results is not None:
            for key in _float_keys:
                val = mtf.results.get(key)
                fields.append(QgsField(key, QMetaType.Type.Double))
                values.append(float(val) if val is not None else None)
        return fields, values


    def process_results(self, mtf, parameters, context, feedback ):
        """
        Process results to:
        - generate a GPKG report
        - Send a MTF Event to update the UI
        """
        
        outputDirectory = self.parameterAsFile(parameters, self.OUTPUT_DIRECTORY, context)
        if outputDirectory:
            QSettings().setValue(self.OUTPUT_DIRECTORY_SETTING, outputDirectory )
        
        vlayer = self.parameterAsVectorLayer(parameters, self.ROI, context)
        
        reportLayer = None
        fields = None
        values = None
        reportFilePath = None
        
        outputDir = QDir( outputDirectory )
        reportLayer = None
        if not outputDirectory:
            feedback.pushInfo(self.tr("No GPKG report would be generated because there is no output directory "))
            
        elif not outputDir.exists() :
            feedback.reportError(self.tr(f"Failed to generate report : output directory '{outputDirectory}' doesn't exist"), True )
            
        elif not mtf.feedback.isCanceled() and mtf.isValid():

            current_datetime = QDateTime.currentDateTimeUtc()
            current_datetime_str = current_datetime.toString("yyyyMMdd_hhmmss")
            
            alg_id = self.id().replace(":", "_" )
            figure_filename = None
            figures = mtf.figure()
            for idx, fig in enumerate(figures):
                suffix = "" if idx == 0 else str(idx + 1)
                fig_filename = f"figure{suffix}_{alg_id}_{current_datetime_str}.png"
                try:
                    fig_filepath = outputDir.filePath( fig_filename )
                    fig.savefig( fig_filepath )
                    if idx == 0:
                        figure_filename = fig_filename
                except Exception as e:
                    feedback.reportError(self.tr(f"Error while saving result image '{fig_filepath}' : {e}"))
            
            fields, values = self.fields_and_values( current_datetime, figure_filename, parameters, context, feedback, mtf=mtf )
            reportFilePath = outputDir.filePath( self.REPORT_FILENAME )
            if outputDir.exists( self.REPORT_FILENAME ):
                reportLayer = QgsVectorLayer( reportFilePath, "report", "ogr" )
                feedback.pushInfo(f"reportLayerfieldsCount={reportLayer.fields().count()} fieldsCount={fields.count()}")
                if not reportLayer.isValid() :
                    feedback.reportError(self.tr(f"Report file '{reportFilePath}' exists and is invalid"))
                    reportLayer = None

                # TODO need to test also that geometry type is MultiPolygon and crs is the same as vlayer
                elif reportLayer.fields() != fields:
                    feedback.reportError(self.tr(f"Report file '{reportFilePath}' exists and fields are incompatible with algorithm "))
                    reportLayer = None

            else:
                hasError = False
                # Create en empty layer
                options = QgsVectorFileWriter.SaveVectorOptions()
                options.driverName = "GPKG"
                fw = QgsVectorFileWriter.create(reportFilePath, fields, Qgis.WkbType.Polygon, vlayer.crs(), QgsCoordinateTransformContext(), options )
                if fw.hasError() :
                    feedback.reportError(f"Failed to create report file '{reportFilePath}' : {fw.errorMessage()}")
                    hasError = True
                del fw

                if not hasError :
                    reportLayer = QgsVectorLayer( reportFilePath, "report", "ogr" )
                    if not reportLayer.isValid():
                        feedback.reportError(f"Failed to read report file '{reportFilePath}'")
                        reportLayer = None
            
        if not mtf.feedback.isCanceled():

            # Write in the report layer
            if reportLayer:
                feat = QgsFeature( fields )
                feat.setAttributes( values )

                multiPolygon = QgsMultiPolygon()
                multiPolygon.addGeometries( [ feature.geometry().constGet().clone() for feature in vlayer.getFeatures() ] )
                feat.setGeometry( multiPolygon )
                
                if not reportLayer.dataProvider().addFeature( feat ):
                    feedback.reportError(self.tr(f"Failed to add new feature to report file '{reportFilePath}' : {reportLayer.dataProvider().lastError()}"))                
            
            # Post event to refresh widget in main thread
            QCoreApplication.postEvent(self.result_widget, MTFEvent(self.id(), mtf, parameters))
