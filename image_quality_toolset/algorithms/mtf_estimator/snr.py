# -------------------------------------------------------------------------
# Copyright (C) 2025 Telespazio
# -------------------------------------------------------------------------
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# -------------------------------------------------------------------------

from osgeo import gdal
from matplotlib.figure import Figure
import numpy as np
from scipy import ndimage
from scipy.stats import binned_statistic
from .variogram import analyze_image_variogram

class SNR :
    def __init__(
        self,
        roi,
        image,
        band_number,
        window_size,
        snr_precision,
        L_min,
        L_max,
        scale=1.0,
        offset=0.0,
        feedback=None,
    ):
        """
        SNR estimation using multiple methods:
        - Variogram nugget effect analysis
        - Local statistics (mean/std) in uniform regions
        - Peak histogram method

        Args:
            roi: Region of interest vector layer
            image: Input image data (DN values)
            band_number: Band number in the raster
            window_size: Window size for local statistics
            snr_precision: SNR precision for histogram binning
            L_min: Minimum radiance threshold
            L_max: Maximum radiance threshold
            scale: Scale  factor to convert DN to radiance (default: 1.0)
            offset: Offset factor to convert DN to radiance (default: 0.0)
            feedback: QGIS feedback object
        """
        self._isValid = True
        self.feedback = feedback
        self.roi = roi
        self.band_number = band_number

        self.scale = scale
        self.offset = offset

        self.mask = None

        # Histogram data for reports
        self.bin_edges = None
        self.bin_av = None
        self.bin_std = None
        self.bin_med = None
        self.bin_count = None

        self.peak_snr_histo_bin_value = None
        self.peak_snr_histo_value = None

        # SNR results:
        self.peak_snr_value = None
        self.peak_ref_radiance = None
        self.snr_formulation_1 = None
        self.snr_formulation_1_reference_radiance = None
        self.snr_formulation_2 = None

        self.hist = None
        self.pas = None
        self.maximum_loc = None
        self.v_max = None

        # Radiance range filter:
        self.L_min = L_min
        self.L_max = L_max

        self.console(' Band Number in the processing              : {:.2f}'.format(band_number))
        self.console(' Scale                                      : {:.6f}'.format(scale))
        self.console(' Offset                                     : {:.6f}'.format(offset))

        # Input parameters:
        self.window_size = window_size

        # Convert DN to radiance: radiance = DN * scale + offset
        self.im = np.copy(image) * self.scale + self.offset

        self.sobel_image = None
        self.threshold = None
        self.percent_Of_selected_pixels = 0.0
        self.mask = None

        # Uniform filtering of the input image (NaN-aware):
        mask_valid = np.isfinite(self.im).astype(np.float64)
        im_filled = np.nan_to_num(self.im, nan=0.0)

        # Count of valid pixels in window
        count = ndimage.uniform_filter(mask_valid, size=self.window_size) * (self.window_size**2)
        # Sum of valid pixels in window
        sum_im = ndimage.uniform_filter(im_filled, size=self.window_size) * (self.window_size**2)

        # Calculate local mean
        self.smooth_image = np.divide(sum_im, count, out=np.full_like(self.im, np.nan), where=count > 0)

        # Local variance calculation: E[X^2] - (E[X])^2
        sum_im_sq = ndimage.uniform_filter(im_filled**2, size=self.window_size) * (self.window_size**2)
        mean_sq = np.divide(sum_im_sq, count, out=np.full_like(self.im, np.nan), where=count > 0)

        self.std = np.power(np.maximum(0, mean_sq - self.smooth_image**2), 0.5)
        std = self.std
        # Local SNR per pixel
        self.snr_image = np.divide(self.smooth_image, std, out=np.zeros_like(self.smooth_image), where=(std > 0))

        self.snr_value = None

        # Define SNR precision (bin width = 2 * precision)
        self.snr_precision = snr_precision
        self.c = 2 * snr_precision

        self.console(' Input SNR precision                      : {}'.format(self.snr_precision))
        self.console(' Width of the histogram bin is 2*c        : {}'.format(2 * self.snr_precision))

        self.im_array_rad_m = None
        self.sample_mean_value = None  # Mean of selected pixels
        self.sample_std_value = None  # Standard deviation of selected pixels

        self.variogram_data = None

    def variogram_snr(self, samples=5000, lag=25, plot=True):
        """Compute SNR using variogram analysis of the nugget effect.

        Args:
            samples: Number of points to sample (recommended: 3000-5000)
            lag: Number of lag classes for variogram
            plot: Whether to display plots

        Returns:
            snr: Signal-to-noise ratio computed from variogram
        """
        # Create mask based on L_min/L_max and finite values
        mask = np.isfinite(self.im)

        # Mask pixels with negative radiance
        mask[self.im < 0] = 0
        if self.L_min is not None and self.L_max is not None:
            mask[self.im <= self.L_min] = 0
            mask[self.im >= self.L_max] = 0

        self.im_array_rad_m = np.copy(self.im[mask == 1])
        # Temporal array for variogram with NaN where masked
        im_temp = np.copy(self.im)
        im_temp[mask == 0] = np.nan
        self.mask = mask

        self.variogram_data = analyze_image_variogram(im_temp, n_samples=samples, lag=lag, plot=plot)

    def second_method(self):
        """Compute SNR statistics using binned radiance analysis.

        Note: Assumes mask is already set (e.g., via variogram_snr).
        """
        im_array_rad = self.im

        # self.smooth_image: input image with uniform filter applied
        arr_smooth = self.smooth_image

        # Min/Max radiance of selected pixels
        self.console(' Minimum / Maximum Radiance of selected pixels in input: {:.2f} / {:.2f}'
                     .format(np.nanmin(im_array_rad), np.nanmax(im_array_rad)))

        # Local residual (noise estimate)
        arr_diff = (im_array_rad - arr_smooth)

        # Apply mask to residuals and radiance
        arr_diff_m = np.copy(arr_diff[self.mask == 1]).flatten()
        im_array_rad_m = np.copy(im_array_rad[self.mask == 1]).flatten()

        # Filter out NaN and inf values
        valid_mask = np.isfinite(im_array_rad_m) & np.isfinite(arr_diff_m)
        im_array_rad_m = im_array_rad_m[valid_mask]
        arr_diff_m = arr_diff_m[valid_mask]

        if len(im_array_rad_m) == 0:
            self.console("Error: No valid pixel values after filtering NaN/inf")
            return

        # Selection Statistics
        self.total_pixels = np.count_nonzero(im_array_rad)
        self.selected_pixels = np.count_nonzero(self.mask == 1)
        percent_Of_selected_pixels = np.double(np.divide(
            np.count_nonzero(self.mask == 1),
            float(self.total_pixels))) * 100
        self.percent_Of_selected_pixels = percent_Of_selected_pixels
        self.console(" Percentage of selected pixels: {:.2f} %".format(self.percent_Of_selected_pixels))
        self.console(' Minimum / Maximum Radiance of filtered images: {:.2f} / {:.2f}'
                     .format(np.min(im_array_rad_m), np.max(im_array_rad_m)))

        # Update L_min / L_max to match filtered data range
        self.L_min = np.min(im_array_rad_m)
        self.L_max = np.max(im_array_rad_m)

        b_max = np.min([np.max(im_array_rad_m), self.L_max])
        b_min = np.max([np.min(im_array_rad_m), self.L_min])

        # Analysis of residual per bin (default 1 W/(m².str.µm))
        self.console(' Statistics per bin of 1 W')
        step = 1

        bin_number = (int((b_max - b_min + 1)) * step)

        # X: Radiance values
        # Y: Residuals (difference from local mean)
        x = im_array_rad_m
        y = arr_diff_m

        # Per-bin Statistics:
        #   - average per bin
        #   - std (noise) per bin
        #   - median per bin
        #   - pixel count per bin
        bin_means, bin_edges, _ = \
            binned_statistic(x, y, statistic='mean', bins=bin_number)
        bin_std, bin_edges, _ = \
            binned_statistic(x, y, statistic='std', bins=bin_number)
        bin_med, bin_edges, _ = \
            binned_statistic(x, y, statistic='median', bins=bin_number)
        bin_count, bin_edges, _ = \
            binned_statistic(x, x, statistic='count', bins=bin_number)

        self.bin_av = bin_means
        self.bin_std = bin_std
        self.bin_med = bin_med
        self.bin_count = bin_count
        self.bin_edges = bin_edges

        # Quadratic error between mean and median to check bias
        sq = (np.sum(((bin_med - bin_means) *
                      (bin_med - bin_means)))) / (bin_number - 1)

        self.console(' Histogram BIN Number        : {:.2f}'.format(bin_number))
        self.console(' Quadratic Error (med / mean) : {:.2f}'.format(sq))

        # SNR formulation 1: average of radiance/std over all bins
        self.snr_formulation_1_reference_radiance = (self.L_max + self.L_min) / 2.0
        bin_centers = (self.bin_edges[1:] + self.bin_edges[:-1]) / 2.0
        valid_std_mask = self.bin_std > 0
        if np.any(valid_std_mask):
            snr_per_bin = bin_centers[valid_std_mask] / self.bin_std[valid_std_mask]
            self.snr_formulation_1 = np.mean(snr_per_bin)
        else:
            self.snr_formulation_1 = np.nan
            self.console("Warning: No valid bin_std values for SNR formulation 1")

        self.do_snr_formulation_2()

    def do_snr_formulation_2(self):
        """SNR -> Second formulation: power((var_signal / var_noise) - 1; 0.5)"""
        if self.bin_std is not None and len(self.bin_std) > 0 and np.any(np.isfinite(self.bin_std)):
            # Global image variance as signal proxy
            var_signal = np.nanvar(self.im)
            # Average of binned variances as noise estimate
            var_noise = np.mean(self.bin_std * self.bin_std)

            if var_noise > 0 and np.isfinite(var_signal) and np.isfinite(var_noise):
                snr_2 = np.power(max(0, (var_signal / var_noise) - 1), 0.5)
            else:
                snr_2 = np.nan
                self.console("Warning: Invalid variance values for SNR formulation 2")
        else:
            snr_2 = np.nan
            self.console("Warning: No valid bin_std values for SNR formulation 2")
        self.snr_formulation_2 = snr_2

    def print_output(self):
        """Print calculated SNR results."""
        self.console('Peak SNR Value         : {:.2f} @ {:.2f}'.
                     format(self.peak_snr_value, self.peak_ref_radiance))
        self.console('Quantitative SNR Value : {:.2f} @ {:.2f}'.
                     format(self.snr_formulation_1 if self.snr_formulation_1 is not None else np.nan,
                            self.snr_formulation_1_reference_radiance if self.snr_formulation_1_reference_radiance is not None else np.nan))
        self.console('SNR Value (Formulation 2) : {:.2f}'.
                     format(self.snr_formulation_2 if self.snr_formulation_2 is not None else np.nan))

    def figure(self):
        """Plot SNR analysis results in a 2x2 grid."""
        fig = Figure(figsize=(20, 16))
        axs = fig.subplots(2, 2)
        props = dict(boxstyle='round', facecolor='wheat', alpha=0.5)

        # ---------------------------------------------------------------
        # (1) Top-left: Input image + mask
        # ---------------------------------------------------------------
        ax = axs[0, 0]
        masked_im = np.where(self.mask == 1, self.im, np.nan)
        im = ax.imshow(self.im, cmap='gray')
        ax.imshow(masked_im, cmap='terrain', alpha=0.6)
        ax.set_title('SNR Points (Image + Mask)')
        ax.set_xlabel('Pixel X')
        ax.set_ylabel('Pixel Y')
        fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
        textstr = 'Selected pixels: {:.1f}%'.format(self.percent_Of_selected_pixels)
        ax.text(0.05, 0.95, textstr, transform=ax.transAxes,
                fontsize=11, verticalalignment='top', bbox=props)

        # ---------------------------------------------------------------
        # (2) Top-right: Variogram
        # ---------------------------------------------------------------
        ax = axs[0, 1]
        if self.variogram_data is not None:
            vd = self.variogram_data
            V = vd['V']
            ax.plot(V.bins, V.experimental, 'o-', markersize=4,
                    linewidth=2, label='Experimental', color='steelblue')
            ax.plot(V.bins, V.fitted_model(V.bins), '-', linewidth=3,
                    label=f'Model ({V.model.__name__})', color='coral')
            ax.axvline(x=vd['snr'], color='black', linestyle='--',
                       alpha=0.0, label=f'SNR = {vd["snr"]:.4f}')
            ax.legend(fontsize=9)
        ax.set_xlabel('Distance (pixels)', fontsize=11)
        ax.set_ylabel('Semi-variance γ(h)', fontsize=11)
        ax.set_title('Variogram', fontsize=12, fontweight='bold')
        ax.grid(True, alpha=0.3)

        # ---------------------------------------------------------------
        # (3) Bottom-left: Image histogram
        # ---------------------------------------------------------------
        ax = axs[1, 0]
        if self.bin_edges is not None and self.bin_count is not None:
            bin_edges = self.bin_edges
            ax.hlines(self.bin_count,
                      bin_edges[:-1], bin_edges[1:],
                      colors='k', lw=2,
                      label='Binned pixel count')
            ax.grid()
            ax.set_title('Radiance Distribution')
            ax.set_xlabel('Radiance (W/(m².str.µm)')
            ax.set_ylabel('Count')
            if self.im_array_rad_m is not None and len(self.im_array_rad_m) > 0:
                textstr = '\n '.join([
                    'Mean Radiance: {:.2f}'.format(np.mean(self.im_array_rad_m)),
                    'Std Radiance: {:.2f}'.format(np.std(self.im_array_rad_m))])
            else:
                textstr = 'No valid radiance data'
            ax.text(0.5, 0.95, textstr, transform=ax.transAxes,
                    fontsize=10, verticalalignment='top', horizontalalignment='center', bbox=props)
        else:
            ax.text(0.5, 0.5, 'Radiance data not available',
                    transform=ax.transAxes, ha='center', va='center')
            ax.set_title('Image Radiance Distribution (N/A)')

        # ---------------------------------------------------------------
        # (4) Bottom-right: SNR peak histogram
        # ---------------------------------------------------------------
        ax = axs[1, 1]
        if self.peak_snr_histo_value is not None and self.peak_snr_histo_bin_value is not None:
            ax.hlines(self.peak_snr_histo_value,
                      self.peak_snr_histo_bin_value[:-1],
                      self.peak_snr_histo_bin_value[1:],
                      colors='g', lw=2, label='SNR Histogram')
            ax.legend()
            ax.set_xlabel('SNR')
            ax.set_ylabel('Count')
            ax.set_title('Peak SNR Histogram')
            ax.grid(True, alpha=0.3)
            textstr = '\n '.join([
                'Peak SNR : {:.2f}'.format(self.peak_snr_value),
                ' @ {:.2f} W/(m².str.µm)'.format(self.peak_ref_radiance)])
            ax.text(0.5, 0.95, textstr, transform=ax.transAxes,
                    fontsize=10, verticalalignment='top', horizontalalignment='center', bbox=props)
        else:
            ax.text(0.5, 0.5, 'Peak SNR data not available',
                    transform=ax.transAxes, ha='center', va='center')
            ax.set_title('Peak SNR Histogram (N/A)')

        return [fig]

    def getRoi(self):
        return self.roi

    def console(self, message):
        print(message)
        if self.feedback:
            self.feedback.pushInfo(message)

    def isValid(self):
        """Return True if computed SNR/MTF is valid."""
        return self._isValid

    def compute_peak_snr(self, precision_v=None):
        """
        Compute SNR using the peak-histogram method (histogram peak of local SNR).

        Args:
            precision_v: Precision of SNR calculation (bin width).
                         Defaults to self.snr_precision if None.
        """
        if precision_v is None:
            precision_v = self.snr_precision

        # Masked SNR image and smoothed radiance
        snr_image_m = np.copy(self.snr_image[self.mask == 1]).flatten()
        im_array_rad_m = np.copy(self.smooth_image[self.mask == 1]).flatten()

        # Filter out invalid and zero SNR values
        valid_mask = np.isfinite(snr_image_m) & (snr_image_m > 0) & np.isfinite(im_array_rad_m)
        snr_image_m = snr_image_m[valid_mask]
        im_array_rad_m = im_array_rad_m[valid_mask]

        if len(snr_image_m) == 0:
            self.console("Error: No valid SNR values after filtering NaN/inf/zeros")
            self.peak_snr_value = np.nan
            self.peak_ref_radiance = np.nan
            return

        # Filter out very small SNR values (< 1) - typically edges or noise
        snr_min_threshold = 1.0
        snr_mask = snr_image_m >= snr_min_threshold
        snr_image_m = snr_image_m[snr_mask]
        im_array_rad_m = im_array_rad_m[snr_mask]

        if len(snr_image_m) == 0:
            self.console("Error: No valid SNR values above threshold")
            self.peak_snr_value = np.nan
            self.peak_ref_radiance = np.nan
            return

        # Filter outliers: support modern sensors with high SNR
        snr_max_reasonable = 1000.0
        outlier_mask = snr_image_m <= snr_max_reasonable
        snr_image_m = snr_image_m[outlier_mask]
        im_array_rad_m = im_array_rad_m[outlier_mask]

        if len(snr_image_m) == 0:
            self.console("Error: No valid SNR values after outlier filtering")
            self.peak_snr_value = np.nan
            self.peak_ref_radiance = np.nan
            return

        # Compute histogram with fixed bin size
        snr_min = max(0, np.min(snr_image_m))
        snr_max = min(snr_max_reasonable, np.max(snr_image_m))
        bin_width = precision_v * 2
        bin_number = max(10, int((snr_max - snr_min) / bin_width))

        # Perform binned statistics (count SNR occurrences)
        peak_snr_histo_value, peak_snr_histo_bin_value, _4 = \
            binned_statistic(snr_image_m, snr_image_m,
                             statistic='count',
                             bins=bin_number,
                             range=(snr_min, snr_max))

        # Peak of the histogram is the estimated SNR
        hist = peak_snr_histo_value
        maximum_loc = np.where(hist == max(hist))[0]
        self.maximum_loc = maximum_loc[0]
        c = peak_snr_histo_bin_value[1] - peak_snr_histo_bin_value[0]
        self.peak_snr_value = peak_snr_histo_bin_value[maximum_loc][0] + (c / 2.0)
        self.peak_snr_histo_value = peak_snr_histo_value
        self.peak_snr_histo_bin_value = peak_snr_histo_bin_value

        # Compute reference radiance from pixels within the peak SNR bin
        # to avoid overestimation bias.
        bin_lower = peak_snr_histo_bin_value[self.maximum_loc]
        bin_upper = peak_snr_histo_bin_value[self.maximum_loc + 1]
        peak_bin_mask = (snr_image_m >= bin_lower) & (snr_image_m < bin_upper)

        if np.any(peak_bin_mask):
            self.peak_ref_radiance = np.mean(im_array_rad_m[peak_bin_mask])
        else:
            self.peak_ref_radiance = np.nanmean(im_array_rad_m)
