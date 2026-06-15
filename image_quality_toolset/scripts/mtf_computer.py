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

import argparse
import os
from pathlib import Path
import configparser
import ast
from datetime import datetime, timezone

import numpy as np
import matplotlib.pyplot as plt
from osgeo import gdal

from qgis.core import QgsRasterLayer,QgsVectorLayer,QgsProcessingContext,QgsProcessingFeedback
from algorithms.mtf_estimator.mtf_knife_edge import MtfKnifeEdge
from algorithms.mtf_estimator.mtf_bridge import MtfBridge
from algorithms.mtf_estimator.snr import SNR
from algorithms.tools.raster_tools import roi_extraction, rasterize

# Algorithm ids matching BaseMTFEstimatorAlgorithm.id() (provider id + algorithm name)
# used to build debug figure filenames the same way as the QGIS algorithms.
ALGORITHM_IDS = {
    'MTF': 'imagequalitytoolset_MTFEstimatorKnifeEdge',
    'MTF_BRIDGE': 'imagequalitytoolset_MTFEstimatorBridge',
    'SNR': 'imagequalitytoolset_SNREstimator',
}


def save_debug_figures(mtf, debug_dir, method, feedback):
    """
    Save result figures to debug_dir, using the same naming convention as the
    QGIS processing algorithms (BaseMTFEstimatorAlgorithm.process_results):
    figure_<alg_id>_<yyyyMMdd_hhmmss>.png, figure2_<alg_id>_<yyyyMMdd_hhmmss>.png, ...
    """
    os.makedirs(debug_dir, exist_ok=True)
    alg_id = ALGORITHM_IDS[method]
    current_datetime_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    for idx, fig in enumerate(mtf.figure()):
        suffix = "" if idx == 0 else str(idx + 1)
        fig_filename = f"figure{suffix}_{alg_id}_{current_datetime_str}.png"
        fig_filepath = os.path.join(debug_dir, fig_filename)
        fig.savefig(fig_filepath)
        feedback.pushInfo(f"Saved debug figure to: {fig_filepath}")
        plt.close(fig)


def process_algorithm(config_file, feedback=None):
    """
    Process MTF algorithm using configuration file.

    The ROI type is determined from the 'ROI' field in [input]:
    - If ROI is a shapefile path: uses vector layer for ROI extraction
    - If ROI is a window_parameter dict: crops image using window parameters
    - If ROI is absent: processes the full image

    Args:
        config_file: Path to INI configuration file
        feedback: Optional QgsProcessingFeedback for logging

    Returns:
        MTF object (MtfKnifeEdge or SNR depending on method)
    """
    if feedback is None:
        feedback = QgsProcessingFeedback()

    # Load configuration
    config = load_config(config_file)

    # Extract parameters
    image_path = config.get('image_path')
    if image_path is None:
        raise ValueError("image_path is required in [input] section of config file")

    method = config.get('method')
    band_n = config.get('band_n')
    debug_dir = config.get('debug_dir')
    expert_mode = config.get('expert_mode', False)

    # Method 2 specific parameters
    scale = config.get('scale')
    offset = config.get('offset')
    px_margin = config.get('px_margin')
    edge_direction = config.get('edge_direction')
    esf_model = config.get('esf_model')

    # Method 3 (SNR) specific parameters
    window_size = config.get('window_size')
    snr_precision = config.get('snr_precision')
    L_min = config.get('L_min')
    L_max = config.get('L_max')
    nb_samples = config.get('nb_samples', 5000)
    lag = config.get('lag', 25)
    sampling = config.get('sampling')
    input_angle = config.get('input_angle')
    bridge_width = config.get('bridge_width')

    # ROI parameters
    window_parameter = config.get('window_parameter')
    shape_file = config.get('shape_file')

    # Determine ROI type and process accordingly
    if shape_file:
        # Use vector layer for ROI extraction
        feedback.pushInfo(f"Using shape file for ROI: {shape_file}")
        raster_layer = QgsRasterLayer(image_path, "Raster Layer")
        vlayer = QgsVectorLayer(shape_file, "ROI Layer", "ogr")
        context = QgsProcessingContext()

        memraster = roi_extraction(raster_layer, band_n, vlayer, context, feedback)

        if method == 'MTF':
            mtf = MtfKnifeEdge(vlayer, memraster, band_n, scale, offset, px_margin,
                         edge_direction, esf_model, sampling, input_angle=input_angle,
                         feedback=feedback, debug_dir=debug_dir, expert_mode=expert_mode)
        elif method == 'MTF_BRIDGE':
            mtf = MtfBridge(vlayer, memraster, band_n, scale, offset, px_margin,
                         bridge_width, edge_direction, esf_model, sampling,
                         input_angle=input_angle, feedback=feedback, debug_dir=debug_dir, expert_mode=expert_mode)
        elif method == 'SNR':
            rows = memraster.RasterYSize
            cols = memraster.RasterXSize
            band = memraster.GetRasterBand(1)
            image = np.float64(band.ReadAsArray(0, 0, cols, rows))
            mtf = SNR(vlayer, image, band_n, window_size, snr_precision,
                      L_min, L_max, feedback=feedback)
            mtf.variogram_snr(samples=nb_samples, lag=lag, plot=False)
            mtf.compute_peak_snr()
            mtf.second_method()
            mtf.print_output()
            # Optional: Save figure to file
            if debug_dir:
                os.makedirs(debug_dir, exist_ok=True)
                fig = mtf.figure()[0]
                fig.savefig(os.path.join(debug_dir, 'snr_analysis.png'), dpi=150, bbox_inches='tight')
                plt.close(fig)
        else:
            raise ValueError(f"Unknown method: {method}. Must be 'MTF', 'MTF_BRIDGE' or 'SNR'")

    elif window_parameter:
        # Crop image using window parameters
        feedback.pushInfo(f"Cropping image with window: {window_parameter}")
        img_array = crop_image(image_path, window_parameter, band_n)
        memraster = rasterize(img_array)

        if method == 'MTF':
            mtf = MtfKnifeEdge(None, memraster, band_n, scale, offset, px_margin,
                         edge_direction, esf_model, sampling, input_angle=input_angle,
                         feedback=feedback, debug_dir=debug_dir, expert_mode=expert_mode)
        elif method == 'MTF_BRIDGE':
            mtf = MtfBridge(None, memraster, band_n, scale, offset, px_margin,
                         bridge_width, edge_direction, esf_model, sampling,
                         input_angle=input_angle, feedback=feedback, debug_dir=debug_dir, expert_mode=expert_mode)
        elif method == 'SNR':
            mtf = SNR(img_array/np.nanmax(img_array), band_number=band_n, feedback=feedback, roi=None)
            mtf.variogram_snr(samples=nb_samples, lag=lag, plot=True)
            mtf.compute_peak_snr()
            mtf.second_method()
            mtf.print_output()
        else:
            raise ValueError(f"Unknown method: {method}. Must be 'MTF', 'MTF_BRIDGE' or 'SNR'")

    else:
        # No ROI, process the full image
        feedback.pushInfo("Processing full image (no ROI specified)")
        gdal_layer = gdal.Open(image_path, gdal.GA_ReadOnly)
        if gdal_layer is None:
            raise ValueError(f"Could not open image: {image_path}")

        band = gdal_layer.GetRasterBand(band_n)
        cols = gdal_layer.RasterXSize
        rows = gdal_layer.RasterYSize
        img_array = band.ReadAsArray(0, 0, cols, rows).astype(np.float64)
        memraster = rasterize(img_array)

        if method == 'MTF':
            mtf = MtfKnifeEdge(None, memraster, band_n, scale, offset, px_margin,
                         edge_direction, esf_model, sampling, input_angle=input_angle,
                         feedback=feedback, debug_dir=debug_dir, expert_mode=expert_mode)
        elif method == 'MTF_BRIDGE':
            mtf = MtfBridge(None, memraster, band_n, scale, offset, px_margin,
                         bridge_width, edge_direction, esf_model, sampling,
                         input_angle=input_angle, feedback=feedback, debug_dir=debug_dir, expert_mode=expert_mode)
        elif method == 'SNR':
            mtf = SNR(img_array/np.nanmax(img_array), band_number=band_n, feedback=feedback, roi=None)

            mtf.variogram_snr(samples=nb_samples, lag=lag, plot=True)
            mtf.compute_peak_snr()
            mtf.second_method()
            mtf.print_output()
        else:
            raise ValueError(f"Unknown method: {method}. Must be 'MTF', 'MTF_BRIDGE' or 'SNR'")

    if debug_dir:
        save_debug_figures(mtf, debug_dir, method, feedback)

    return mtf


def load_config(config_file):
    """
    Load configuration from INI file.

    Expected structure:
        [input]
        image_path = /path/to/image.tif
        ROI = /path/to/roi.shp  OR  {'line': 0, 'pixel': 0, 'line_number': 100, 'pixel_number': 100}

        [parameters]
        method = MTF  # or SNR
        band = 1
        # method 2 specific:
        scale = 0.01
        ...

        [debug]
        dir = /path/to/debug
        expert_mode = false

    Args:
        config_file: Path to INI configuration file

    Returns:
        Dictionary with configuration parameters
    """
    config = configparser.ConfigParser(inline_comment_prefixes=('#', ';'))
    config.read(config_file)

    params = {}

    # Load input parameters
    if 'input' in config:
        image_path = config['input'].get('image_path')
        if image_path is not None:
            image_path = os.path.expandvars(image_path)
        params['image_path'] = image_path

        # Parse ROI: can be a shapefile path or a window_parameter dict
        roi_str = config['input'].get('ROI')
        if roi_str is not None:
            roi_str = roi_str.strip()
            try:
                # Try to parse as a dict (window_parameter)
                roi_value = ast.literal_eval(roi_str)
                if isinstance(roi_value, dict):
                    params['window_parameter'] = roi_value
                    params['shape_file'] = None
                else:
                    raise ValueError("ROI dict must be a dictionary")
            except (ValueError, SyntaxError):
                # Not a dict, treat as a shapefile path
                params['shape_file'] = os.path.expandvars(roi_str)
                params['window_parameter'] = None
        else:
            params['window_parameter'] = None
            params['shape_file'] = None

    # Load parameters
    if 'parameters' in config:
        params['method'] = config['parameters'].get('method')
        params['band_n'] = config['parameters'].getint('band')

        # Method 2 specific parameters
        if config['parameters'].get('scale') is not None:
            params['scale'] = config['parameters'].getfloat('scale')
        if config['parameters'].get('offset') is not None:
            params['offset'] = config['parameters'].getfloat('offset')
        if config['parameters'].get('px_margin') is not None:
            params['px_margin'] = config['parameters'].getint('px_margin')
        if config['parameters'].get('edge_direction') is not None:
            params['edge_direction'] = config['parameters'].get('edge_direction')
        if config['parameters'].get('esf_model') is not None:
            params['esf_model'] = config['parameters'].get('esf_model')
        if config['parameters'].get('sampling') is not None:
            params['sampling'] = config['parameters'].getfloat('sampling')
        if config['parameters'].get('input_angle') is not None:
            params['input_angle'] = config['parameters'].getfloat('input_angle')
        if config['parameters'].get('bridge_width') is not None:
            params['bridge_width'] = config['parameters'].getfloat('bridge_width')

        # Method 3 (SNR) specific parameters
        if config['parameters'].get('window_size') is not None:
            params['window_size'] = config['parameters'].getint('window_size')
        if config['parameters'].get('snr_precision') is not None:
            params['snr_precision'] = config['parameters'].getfloat('snr_precision')
        if config['parameters'].get('L_min') is not None:
            params['L_min'] = config['parameters'].getfloat('L_min')
        if config['parameters'].get('L_max') is not None:
            params['L_max'] = config['parameters'].getfloat('L_max')
        if config['parameters'].get('nb_samples') is not None:
            params['nb_samples'] = config['parameters'].getint('nb_samples')
        if config['parameters'].get('lag') is not None:
            params['lag'] = config['parameters'].getint('lag')

    # Load debug parameters
    if 'debug' in config:
        debug_dir = config['debug'].get('dir')
        if debug_dir is not None:
            debug_dir = os.path.expandvars(debug_dir)
        params['debug_dir'] = debug_dir
        params['expert_mode'] = config['debug'].getboolean('expert_mode', fallback=False)

    return params


def crop_image(image_path, window_parameter, band_n=1):
    """
    Crop image based on window parameters.

    Args:
        image_path: Path to the image file
        window_parameter: Dictionary with keys 'line', 'pixel', 'line_number', 'pixel_number'
        band_n: Band number to read

    Returns:
        Cropped image as numpy array
    """
    # Load image using GDAL
    gdal_layer = gdal.Open(image_path, gdal.GA_ReadOnly)
    if gdal_layer is None:
        raise ValueError(f"Could not open image: {image_path}")

    # Extract window parameters
    start_line = window_parameter['line']
    start_pixel = window_parameter['pixel']
    num_lines = window_parameter['line_number']
    num_pixels = window_parameter['pixel_number']

    # Read the specified window from the band
    band = gdal_layer.GetRasterBand(band_n)
    img_array = band.ReadAsArray(start_pixel, start_line, num_pixels, num_lines).astype(np.float64)

    return img_array


def main():
    parser = argparse.ArgumentParser(description="A script to run MTF or SNR estimator")
    parser.add_argument("--config_file", required=True, help="Path to INI config file")

    args = parser.parse_args()

    mtf = process_algorithm(args.config_file)

    mtf.figure()
    plt.show()
    
if __name__ == "__main__":
    main()