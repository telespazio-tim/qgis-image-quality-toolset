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

import os
from typing import Optional

import matplotlib.pyplot as plt
import numpy as np
from scipy import stats
from scipy.stats import linregress
from scipy.optimize import curve_fit

from ..tools.esf_models import (
    sigmoide, esf_tanh, esf_fermi, esf_gauss_exp_param,
    esf_erf, esf_loess, esf_to_eq_space_polynomial,
)
from ..tools.oversampling_function import rotate_mat
from .mtf import Mtf
from .mtf_common import (
    Transect,
    trimLSF,
    toDisplay_FWHM,
    compute_snr,
    compute_fwhm,
    rescaleforMTF,
    esf_to_eq_space,
    ahamming,
    clean_nuage,
    fLOESS,
    knife_edge1,
    sgolayfilt,
)


def estimate_angle_from_bridge(
    image: np.ndarray,
    expert_mode: bool = False,
    debug_dir: Optional[str] = None,
) -> float:
    """
    Estimate the orientation angle of a bridge from its image.

    Steps:
      1. Normalise image to [0, 1].
      2. Binarize with Otsu threshold.
      3. Denoise with morphological opening.
      4. Skeletonize to a single-pixel line.
      5. Linear regression on skeleton pixels (cols ~ rows).
      6. Return angle = 90 - mod(arctan(slope) * 180/pi, 90),
         consistent with the convention used throughout the MTF pipeline.

    :param image: 2D float numpy array (single spectral band).
    :param expert_mode: If True and debug_dir is set, save a diagnostic figure
        for each processing step to debug_dir.
    :param debug_dir: Directory to save debug figures.
    :return: Estimated angle in degrees in [0, 90].
    :raises ImportError: if scikit-image is not installed.
    :raises ValueError: if the image is flat or produces too few skeleton pixels.
    """
    try:
        from skimage.filters import threshold_otsu
        from skimage.morphology import binary_opening, disk, skeletonize
    except ImportError as exc:
        raise ImportError(
            "scikit-image is required for estimate_angle_from_bridge. "
            "Install it with:  pip install scikit-image"
        ) from exc

    img = image.astype(np.float64)
    img_range = img.max() - img.min()
    if img_range == 0:
        raise ValueError("Image has no contrast; cannot estimate bridge angle.")
    img_norm = (img - img.min()) / img_range

    thresh = threshold_otsu(img_norm)
    binary = img_norm > thresh

    binary = binary_opening(binary, disk(2))

    skeleton = skeletonize(binary)

    rows, cols = np.where(skeleton)
    if len(rows) < 2:
        raise ValueError(
            "Not enough skeleton pixels to estimate angle. "
            "Check that the image contains a clearly visible bridge."
        )

    lr = linregress(rows, cols)
    angle = np.arctan(lr.slope) * 180 / np.pi

    if expert_mode and debug_dir:
        os.makedirs(debug_dir, exist_ok=True)

        fig, axes = plt.subplots(1, 4, figsize=(20, 5))
        fig.suptitle(f"estimate_angle_from_bridge — estimated angle: {angle:.2f}°")

        axes[0].imshow(img_norm, cmap="gray")
        axes[0].set_title("1. Normalised image")
        axes[0].axis("off")

        axes[1].imshow(img_norm > thresh, cmap="gray")
        axes[1].set_title(f"2. Otsu binary (thresh={thresh:.3f})")
        axes[1].axis("off")

        axes[2].imshow(binary, cmap="gray")
        axes[2].set_title("3. After morphological opening")
        axes[2].axis("off")

        axes[3].imshow(img_norm, cmap="gray", alpha=0.6)
        axes[3].imshow(skeleton, cmap="Reds", alpha=0.6)
        fit_rows = np.array([rows.min(), rows.max()])
        fit_cols = lr.slope * fit_rows + lr.intercept
        axes[3].plot(fit_cols, fit_rows, "b-", linewidth=2,
                     label=f"fit  slope={lr.slope:.3f}")
        axes[3].set_title(f"4. Skeleton + regression → {angle:.2f}°")
        axes[3].legend(fontsize=8)
        axes[3].axis("off")

        plt.tight_layout()
        fig.savefig(os.path.join(debug_dir, "angle_estimation_steps.png"), dpi=150, bbox_inches="tight")
        plt.close(fig)

    return float(angle)


class MtfBridge(Mtf):
    """
    MTF estimation for bridge (pulse/slit) targets.

    Unlike knife-edge where the ESF is differentiated to yield the LSF,
    a bridge target produces a pulse-shaped profile that IS the LSF directly.
    MTF is computed as the FFT ratio: |FFT(measured_lsf)| / |FFT(ideal_rect)|,
    where the ideal rectangular pulse has the known physical bridge width.

    Edge detection, oversampling and ESF extraction reuse the same pipeline
    as MtfKnifeEdge, but computeLsf and computeMtf are bridge-specific.
    """

    LVarThresh2 = np.float64(3e-2)

    VALID_ESF_MODELS = (
        'sigmoid', 'esf_tanh', 'esf_fermi', 'esf_gauss_exp_param',
        'esf_erf', 'esf_loess', 'esf_to_eq_space_polynomial',
    )
    ESF_MODEL_PARAMETRIC_FUNCTIONS = {
        'sigmoid': sigmoide,
        'esf_tanh': esf_tanh,
        'esf_fermi': esf_fermi,
        'esf_gauss_exp_param': esf_gauss_exp_param,
        'esf_erf': esf_erf,
    }
    ESF_MODEL_FUNCTIONS = {
        'esf_loess': esf_loess,
        'esf_to_eq_space_polynomial': esf_to_eq_space_polynomial,
    }

    def __init__(
        self,
        roi,
        image,
        band_number,
        scale,
        offset,
        px_margin,
        bridge_width,
        edge_direction=None,
        esf_model='esf_to_eq_space_polynomial',
        sampling=0.2,
        input_angle=None,
        feedback=None,
        debug_dir=None,
        expert_mode=False,
    ):
        """
        Create MTF Bridge Target Object.

        :param bridge_width: Physical width of the bridge target in pixels (LT_w).
        :param image: Image of the bridge target.
        :param band_number: Geotiff dataset band number.
        :param scale: Radiance conversion factor (DN * scale = radiance).
        :param offset: Radiance offset.
        :param px_margin: Pixel margin when searching for inflection point.
        :param edge_direction: 'AL' or 'CT'.
        :param esf_model: ESF fitting model name (default: 'esf_to_eq_space_polynomial').
        :param sampling: Oversampling factor [0, 1] (default: 0.2).
        :param input_angle: If provided, overrides the estimated angle (degrees).
        :param debug_dir: Directory to save result and debug figures.
        :param expert_mode: If True, also save additional step-by-step debug
            figures to debug_dir (e.g. bridge angle estimation steps).
        """
        super().__init__(roi, image, feedback)

        self._debug_dir = debug_dir
        self._expert_mode = expert_mode

        if self._debug_dir:
            os.makedirs(self._debug_dir, exist_ok=True)

        if esf_model not in self.VALID_ESF_MODELS:
            raise ValueError(
                f"esf_model must be one of {self.VALID_ESF_MODELS}, got '{esf_model}'"
            )
        self.esf_model = esf_model
        self.bridge_width = bridge_width

        self.sampling = None
        self.band_number = band_number
        self.scale = scale
        self.offset = offset
        self.px_margin = int(px_margin)
        self.im_array = None
        self.resize_in_line = False
        self.resize_in_column = False
        self.edge_direction = edge_direction
        if self.edge_direction == "AL":
            self.MTF_direction = "CT"
        if self.edge_direction == "CT":
            self.MTF_direction = "AL"

        self.CT_EDGE = None
        self.AL_EDGE = None

        self.inflexion_location = None
        self.record_of_inflexion_location = None
        self.lr = None

        self.input_angle = None

        self.x = None
        self.N = None
        self.nuage = None
        self.nuage_std = None
        self.number_of_lsf = None
        self.bkg = None
        self.x_lsf_input = None
        self.y_lsf_input = None
        self.x_esf = None
        self.y_esf = None
        self.x_lsf = None
        self.y_lsf = None
        self.a3 = None
        self.f = None
        self.MTF_NYQ = None
        self.MTF30 = None
        self.MTF50 = None
        self.results = None

        # Bridge-specific FFT attributes
        self.yo_input = None
        self.xo_input = None
        self.fft_input_norm = None
        self.fft_output_norm = None

        self.inflexion_value = None
        self.RER = None
        self.fwhm = None
        #self.SNR = None
        self.GRD = None
        self.psf_extent = None
        self.gsd = self._extract_gsd()

        self.Transects = list()
        self.__RefineEdgeSubPxStep = 0

        self.im_array = np.copy(self.image) * self.scale + self.offset
        self.ligne, self.colonnes = self.image.shape
        self.resize_in_line = True

        rows, cols = self.im_array.shape
        x = np.float64(np.arange(0, cols))

        # for i in range(0, rows):
        #     r = self.im_array[i, :]
        #     t = Transect(x, r, i, self.feedback)
        #     initGuess = None
        #     if t.isValid():
        #         popt, pcov = t.sigmoidFit(initGuess)
        #         if popt is False or t.EdgeSubPx is None:
        #             t.invalidate()
        #             continue
        #         if pcov[2][2] < self.LVarThresh2:
        #             initGuess = t.getInitGuess()
        #             self.Transects.append(t)
        #         else:
        #             t.invalidate()

        # if len(self.Transects) < 2:
        #     self.console(
        #         "Not enough valid transects. Try a bigger polygon or select a different edge. Exiting."
        #     )
        #     return None
        if input_angle:
            self.input_angle = input_angle
        else:
            self.estimated_angle = estimate_angle_from_bridge(
                self.im_array, expert_mode=self._expert_mode, debug_dir=self._debug_dir
            )

        # for i in range(0, 2):
        #     self.refineEdgeSubPx()

        self.get_oversample_image(sampling=sampling, edge_direction=edge_direction, showGraphic=True)
        window_ovr_image_parameter = None
        self.get_non_eq_space_esf2(window_ovr_image_parameter)
        self.computeEsf()
        self.computeLsf()
        self.doNormalization_and_compute_metrics()
        self.computeMtf()

    @property
    def angle(self):
        return self.input_angle if self.input_angle is not None else self.estimated_angle

    def computeEsf(self):
        """
        :param filt:
        :return:
        """

        x1 = self.x
        R = self.nuage
        passpline = self.sampling

        if self.esf_model in self.ESF_MODEL_PARAMETRIC_FUNCTIONS:
            self.esf_func = self.ESF_MODEL_PARAMETRIC_FUNCTIONS[self.esf_model]
            xs, esfP, R2, RMS, x_cut, y_cut = self.esf_func(x1, R, passpline)
        elif self.esf_model in self.ESF_MODEL_FUNCTIONS:
            self.esf_func = self.ESF_MODEL_FUNCTIONS[self.esf_model]
            xs, esfP, R2, RMS = self.esf_func(x1, R)

        self.x_esf0 = xs
        self.x_esf = xs
        self.y_esf0 = esfP
        ST = {}
        ST["y1_step3"] = esfP

        self.R2 = R2
        self.RMS = RMS

        # Remove background  :
        self.bkg = np.min(ST["y1_step3"])
        self.console(f" Value of the background: {self.bkg}")
        self.y_esf = ST["y1_step3"] - np.min(ST["y1_step3"])

    def get_oversample_image(self, sampling, edge_direction="CT", showGraphic=False, saveGraphic=True):
        self.console("   Clockwise Convention for Angle definition:")
        self.console("   Input Angle is : {:0.2f} °".format(self.angle))

        if self.im_array is None:
            self.console("NO ARRAY")
            return

        self.sampling = sampling
        self.console(" -- Compute oversample matrix ")
        self.console("    Over Sampling factor  : {}".format(self.sampling))
        self.console("    Rotation Angle        : {}°".format(self.angle))
        self.edge_direction = edge_direction

        if self.edge_direction == "CT":
            im_array_rot = np.rot90(self.im_array)
            self.console("Process Cross Track Edge image")
            CT_EDGE, x, infl_pos, center_pos, im = rotate_mat(
                im_array_rot,
                self.angle,
                oversample=self.sampling,
                margin=self.px_margin,
            )
            infl_pos = np.flip(infl_pos)
            center_pos = np.flip(center_pos)
            self.CT_EDGE = np.rot90(CT_EDGE, k=-1)

        if self.edge_direction == "AL":
            self.console("Process Along Track Edge image2")
            AL_EDGE, x, infl_pos, center_pos, im = rotate_mat(
                self.im_array,
                self.angle,
                oversample=self.sampling,
                margin=self.px_margin,
            )
            self.AL_EDGE = AL_EDGE

        rms = (1 / infl_pos.shape[0]) * np.power(
            np.sum((infl_pos - center_pos) * (infl_pos - center_pos)), 0.5
        )
        self.console(
            "       RMS Inflection Point : per line estimated vs rotation  {:.3f}".format(rms)
        )

        if self.edge_direction == "CT":
            s = (self.im_array).shape[1]
            x = np.linspace(0, s - 1, s)
            y = (infl_pos[:])[:, 0] - 1
            if self.resize_in_column:
                x = np.linspace(0, s - 2, s - 1)
                y = (infl_pos[:-1])[:, 0] - 1
            self.lr = linregress(x, y)
            self.inflexion_location = y
            self.record_of_inflexion_location = x
            angle = 90.0 - np.mod(np.arctan(self.lr.slope) * 180.0 / np.pi, 90.0)

        if edge_direction == "AL":
            s = (self.im_array).shape[0]
            x = (infl_pos[:])[:, 0] - 1
            y = np.linspace(0, s - 1, s)
            if self.resize_in_line:
                x = (infl_pos[:-1])[:, 0] - 1
                y = np.linspace(0, s - 2, s - 1)
            self.lr = linregress(y, x)
            self.inflexion_location = x
            self.record_of_inflexion_location = y
            angle = 90.0 - np.mod(np.arctan(self.lr.slope) * 180.0 / np.pi, 90.0)

        x = self.inflexion_location
        y = self.record_of_inflexion_location
        v = x - ((y) * self.lr.slope + self.lr.intercept)
        m = np.nanmean(v)
        s = np.std(v)
        masque = (v < (m + 1 * s)) & (v > (m - 1 * s))
        if list(masque).count(False) > 0:
            self.console(" Remove outlier for angle estimate")
            self.inflexion_location_filtered = x[masque]
            self.record_of_inflexion_location_filtered = y[masque]
            self.lr = linregress(y, x)
        else:
            self.inflexion_location_filtered = x
            self.record_of_inflexion_location_filtered = y
            if self.edge_direction == "CT":
                angle = 90.0 - np.mod(np.arctan(self.lr.slope) * 180.0 / np.pi, 90.0)
            if self.edge_direction == "AL":
                angle = 90.0 - np.mod(np.arctan(self.lr.slope) * 180.0 / np.pi, 90.0)

        self.console(" Rotation angle {:0.2f} °".format(self.angle))

    def get_non_eq_space_esf2(self, window_ovr_image_parameter, showGraphic=False, saveGraphic=False):
        import scipy.stats as sp

        if self.MTF_direction == 'CT':
            A = self.AL_EDGE
        if self.MTF_direction == 'AL':
            A = self.CT_EDGE

        if window_ovr_image_parameter is not None:
            ul_i = window_ovr_image_parameter['line']
            ul_j = window_ovr_image_parameter['pixel']
            ln = window_ovr_image_parameter['line_number']
            px = window_ovr_image_parameter['pixel_number']
            target = A[ul_i:ul_i + ln, ul_j:ul_j + px]
        else:
            target = A

        x_row = np.arange(0, A.shape[0], 1)
        x_grid = np.vstack(A.shape[1] * [x_row]).T
        y_col = np.arange(0, A.shape[1], 1)
        y_grid = np.vstack(A.shape[0] * [y_col])

        m = A > 0
        x = x_grid[m]
        y = y_grid[m]
        v = A[x, y]

        if self.MTF_direction == 'CT':
            lsf_width = target.shape[1]
            nb_record = target.shape[0]
            bin = np.append(np.unique(y), np.max(y) + 1)
            u = y
        if self.MTF_direction == 'AL':
            lsf_width = target.shape[0]
            nb_record = target.shape[1]
            bin = np.append(np.unique(x), np.max(x) + 1)
            u = x
        self.tot_bin = np.max(bin)

        bin_means, bin_edges, binnumber = sp.binned_statistic(u, v, statistic='mean', bins=bin)
        bin_std, bin_edges, binnumber = sp.binned_statistic(u, v, statistic='std', bins=bin)
        bin_count, bin_edges, binnumber = sp.binned_statistic(u, v, statistic='count', bins=bin)
        bin_median, bin_edges, binnumber = sp.binned_statistic(u, v, statistic='median', bins=bin)

        th = 50
        self.th = th
        self.x_old = bin_edges[:-1]
        self.R_old = bin_means

        bin_edges[:-1], bin_means, bin_std, masque = clean_nuage(
            bin_edges[:-1], bin_means, bin_std, bin_count, th
        )

        self.x = bin_edges[:-1]
        self.N = bin_count
        self.nuage = bin_means
        self.nuage_std = bin_std
        self.nuage_median = np.delete(bin_median, masque == 0, 0)
        self.number_of_lsf = nb_record

        print("Number of processed BINs / Total : {} / {}   :".format(len(self.x), lsf_width))
        print("Number of Oversample Edge Profile   :", nb_record)

    @staticmethod
    def _get_discrete_cdf(values):
        values = (values - np.min(values)) / (np.max(values) - np.min(values))
        values_sort = np.sort(values)
        values_sum = np.sum(values)
        values_sums = []
        cur_sum = 0
        for it in values_sort:
            cur_sum += it
            values_sums.append(cur_sum)
        cdf = [values_sums[np.searchsorted(values_sort, it)] / values_sum for it in values]
        return cdf

    def get_psf_extent(self):
        """Compute PSF extent as the 95% encircled energy width in pixels."""
        x_c = self.x
        y_c = self.nuage
        pas = self.sampling

        cdf = self._get_discrete_cdf(y_c)
        x_p = list(zip(y_c, cdf))
        x_p.sort(key=lambda it: it[0])

        x = [it[0] for it in x_p]
        y = [it[1] for it in x_p]

        index = np.max(np.where(np.array(y) < 0.05))
        u = np.where(y_c > x[index])
        min_v = u[0][0]
        max_v = u[0][-1]
        self.psf_extent = (x_c[max_v] - x_c[min_v]) * pas
        return self.psf_extent

    def computeLsf(self):
        """
        For a bridge target the fitted ESF IS the LSF — no differentiation step.

        1. Trim the ESF to the PSF extent window.
        2. Build the ideal rectangular input pulse of width ``bridge_width``
           centred on the LSF peak, used for the FFT-ratio MTF computation.
        """
        w = self.get_psf_extent()
        L_w = 2 * w
        self.console(f'PSF Extent Radius {w:.3f} pixels')

        x_lsf_trimmed, y_lsf_trimmed = trimLSF(self.x_esf, self.y_esf, L_w, self.sampling)

        self.x_lsf = x_lsf_trimmed
        self.y_lsf = y_lsf_trimmed
        self.x_lsf_input = x_lsf_trimmed
        self.y_lsf_input = y_lsf_trimmed
        self.x_lsf_native = x_lsf_trimmed
        self.y_lsf_native = y_lsf_trimmed
        self.x_lsf_before_normalization = x_lsf_trimmed
        self.y_lsf_before_normalization = y_lsf_trimmed

        # Build the ideal rectangular input pulse centred on the LSF peak
        peak_idx = np.argmax(np.abs(self.y_lsf))
        half_width_bins = int((self.bridge_width / 2) / self.sampling)

        self.xo_input = self.x_lsf.copy()
        self.yo_input = np.zeros_like(self.y_lsf)
        start = max(peak_idx - half_width_bins, 0)
        end = min(peak_idx + half_width_bins, len(self.yo_input))
        self.yo_input[start:end] = 1

        return np.array([self.x_lsf, self.y_lsf])
    
    

    def computeMtf(self):
        """
        Compute MTF as the FFT ratio of measured LSF to ideal rectangular pulse:
            MTF(f) = |FFT(measured_lsf)| / |FFT(ideal_rect)|
        """
        self.console("-- Compute MTF (Bridge FFT ratio method) --")

        y_output = self.y_lsf

        # Normalize signals by their sum (as in do_ratio_of_modulus)
        yo_input_norm = self.yo_input / np.sum(self.yo_input)
        y_output_norm = y_output / np.sum(y_output)

        fft_input = np.absolute(np.fft.fft(yo_input_norm))
        fft_output = np.absolute(np.fft.fft(y_output_norm))

        # Frequency axis in cycles/pixel; keep all positive frequencies
        f = np.fft.fftfreq(len(y_output), self.sampling)
        m1 = f>=0
        m2 = f<=1
        mask = ( m1 ) & (m2)

        k_local_max = (2 * np.linspace(0,len(f),len(f)) + 1 ) / (2 * self.bridge_width)
        m1 = k_local_max >= 0
        m2 = k_local_max < 1
        m_k_max = (m1) & (m2)

        u = k_local_max[m_k_max]
        local_max_indice = np.array([np.argmin(np.abs(f[mask] - k)) for k in u])

        # Normalize each spectrum by its DC component before taking the ratio
        fft_input_norm = fft_input / fft_input[0]
        fft_output_norm = fft_output / fft_output[0]

        self.fft_output_masked = fft_output_norm[mask]
        self.fft_input_masked = fft_input_norm[mask]

        self.fft_input = fft_input_norm
        self.fft_output = fft_output_norm

        R_1 = np.divide(fft_output_norm, fft_input_norm)
        R_1_masked = np.absolute(R_1[mask])
        R_2_masked_max = np.absolute(R_1[local_max_indice])
        self._mtf = np.absolute(R_1_masked/np.max(R_1_masked))
        self.f_clean = f[local_max_indice]

        def mtf_sincexp(f, d, lbd):
            return (np.sinc(np.pi * d * f)**2) * np.exp(- lbd * f)

        popt, pcov = curve_fit(mtf_sincexp, self.f_clean, R_2_masked_max, p0=[2.0, 1.5],
                       bounds=([0.1, 0.0], [30.0, 30.0]),
                       maxfev=5000
                       )
        d_fit, lbd = popt

        self.display_frequencies = np.linspace(0,1,11)
        self.R2_smoothed = mtf_sincexp(self.display_frequencies, d_fit, lbd)

        self._lsf = np.fft.ifftshift(R_1)
        self._lsf_reconstructed = self.computeLsfReconstruct(R_1)
        self.f = f[mask]

        # MTF at Nyquist (0.5 cycles/pixel) via linear interpolation
        for rec, val in enumerate(self.display_frequencies):
            if val > 0.5:
                break
        a = (self.R2_smoothed[rec] - self.R2_smoothed[rec - 1]) / (self.display_frequencies[rec] - self.display_frequencies[rec - 1])
        b = self.R2_smoothed[rec] - self.display_frequencies[rec] * a
        self.MTF_NYQ = a * 0.5 + b

        # MTF30
        for rec, val in enumerate(self.R2_smoothed):
            if val < 0.3:
                break
        a = (self.display_frequencies[rec] - self.display_frequencies[rec - 1]) / (self.R2_smoothed[rec] - self.R2_smoothed[rec - 1])
        b = self.display_frequencies[rec] - self.R2_smoothed[rec] * a
        self.MTF30 = a * 0.3 + b

        # MTF50
        for rec, val in enumerate(self.R2_smoothed):
            if val < 0.5:
                break
        a = (self.display_frequencies[rec] - self.display_frequencies[rec - 1]) / (self.R2_smoothed[rec] - self.R2_smoothed[rec - 1])
        b = self.display_frequencies[rec] - self.R2_smoothed[rec] * a
        self.MTF50 = a * 0.5 + b

        return self.mtf

    def computeLsfReconstruct(self, R_1):
        """
        Reconstruct the LSF from the complex FFT ratio R_1 = FFT(output) / FFT(input).

        Steps (ported from do_lsf_reconstruct):
          1. Replace non-finite values by interpolating from neighbours.
          2. Zero out entries where |R_1| > 1 (non-physical response).
          3. Return |ifftshift(R_1_cleaned)|.
        """
        R = R_1.copy()

        # Replace inf / nan by average of neighbouring values
        bad = ~np.isfinite(R)
        bad_idx = np.where(bad)[0]
        if len(bad_idx):
            lo = np.clip(bad_idx - 1, 0, len(R) - 1)
            hi = np.clip(bad_idx + 1, 0, len(R) - 1)
            amp = (np.abs(R[lo]) + np.abs(R[hi])) / 2
            angle = (np.angle(R[lo]) + np.angle(R[hi])) / 2
            R[bad_idx] = amp * np.exp(1j * angle)

        # Zero out non-physical values
        R[np.abs(R) > 1] = 0

        return np.fft.ifftshift(R)

    def figure(self, gsd=None):
        """Generate result figures for bridge MTF."""
        if gsd is None:
            gsd = self.gsd
        sc = self.sampling

        a3_b = np.where(self.x_esf == self.a3)
        ox_esf = self.x_esf[np.take(a3_b, 0)]
        ox_lsf = self.a3

        length = len(sc * self.x_esf)

        if self.MTF_direction == 'AL':
            name = 'Along Track'
        else:
            name = 'Across Track'

        self._figure = plt.figure(figsize=(25, 15), dpi=100)
        plt.suptitle(f"{name} MTF Results (Bridge Method)", fontsize=28, fontweight='bold')

        # --- Subplot 1: Measured vs ideal rect input ---
        plt.subplot(2, 3, 1)
        x_centered = sc * (self.x_lsf - ox_lsf)
        y_norm = self.y_lsf / np.max(self.y_lsf) if np.max(self.y_lsf) != 0 else self.y_lsf
        plt.plot(x_centered, y_norm, '+-', label='Measured (output)')
        plt.plot(sc * (self.xo_input - ox_lsf), self.yo_input, '--',
                 label=f'Ideal rect (LT_w={self.bridge_width} px)')
        plt.title('Measured vs Ideal Input', fontname="Times New Roman", fontweight="bold", fontsize=20)
        plt.xlabel('Pixels')
        plt.ylabel('Normalized intensity')
        plt.legend()
        plt.grid()

        # --- Subplot 2: Results text ---
        ax_text = plt.subplot(2, 3, 2)
        ax_text.axis('off')

        if gsd is not None:
            self.GRD = gsd * self.FWHM

        self.results = {
            "method":       'Bridge FFT ratio',
            "esf_model":    self.esf_func.__name__,
            "bridge_width": self.bridge_width,
            "sampling":     self.sampling,
            "lines":        self.ligne,
            "columns":      self.colonnes,
            "esf_length":   length,
            "MTF_NYQ":      self.MTF_NYQ,
            "MTF30":        self.MTF30,
            "MTF50":        self.MTF50,
            "RER":          self.RER,
            "FWHM":         self.FWHM,
            "R2":           self.R2,
            "GRD":          self.GRD,
        }

        text_str = (
            f"Method : Bridge FFT ratio\n"
            f"Bridge width : {self.bridge_width} px\n"
            f"Sampling: {self.sampling:.2f}\n"
            f"Number of lines : {self.ligne}\n"
            f"Number of columns : {self.colonnes}\n"
            f"Rotation angle : {self.angle:.2f}\n"
            f"MTF @ Nyquist : {self.MTF_NYQ:.2f}\n"
            f"MTF 30 : {self.MTF30:.2f}\n"
            f"MTF 50 : {self.MTF50:.2f}\n"
            f"FWHM : {self.FWHM:.2f} px\n"
            + (f"GRD : {self.GRD:.2f} m" if self.GRD is not None else "")
        )

        ax_text.text(
            0.02, 0.98, text_str,
            transform=ax_text.transAxes,
            ha='left', va='top',
            fontsize=12, fontfamily='monospace',
        )

        # --- Subplot 3: MTF curve (up to Nyquist) ---
        plt.subplot(2, 3, 3)
        mask_nyq = self.f <= 1
        #plt.plot(self.f[mask_nyq], self.mtf, color='k', ls='-')
        plt.plot(self.display_frequencies, self.R2_smoothed, color='k', ls='-')
        plt.axhline(0.3, color="b", ls=':', linewidth=2, label=f"MTF30 = {self.MTF30:.2f}")
        plt.axvline(self.MTF30, color="b", ls=':', linewidth=2)
        plt.axhline(0.5, color="g", ls=':', linewidth=2, label=f"MTF50 = {self.MTF50:.2f}")
        plt.axvline(self.MTF50, color="g", ls=':', linewidth=2)
        plt.axhline(self.MTF_NYQ, color="red", ls='--', linewidth=2.5, label="MTF at Nyquist")
        plt.axvline(0.5, color="red", ls='--', linewidth=2.5)
        plt.grid(linewidth=0.5)
        plt.title('MTF (Bridge FFT ratio)', fontname="Times New Roman", fontweight="bold", fontsize=20)
        plt.xlabel('Freq (cycles/pixel)', fontname="Times New Roman", fontweight="bold", fontsize=20)
        plt.ylabel('Normalized Module', fontname="Times New Roman", fontweight="bold", fontsize=20)
        plt.legend(fontsize=10)

        # --- Subplot 4: LSF reconstruct ---
        plt.subplot(2, 3, 4)
        plt.plot(self.x_lsf[1:], np.abs(self._lsf)[1:], '+', label='LSF')
        plt.plot(self.x_lsf[1:], np.abs(self._lsf_reconstructed)[1:], label='Reconstructed LSF')
        plt.title('LSF', fontname="Times New Roman", fontweight="bold", fontsize=20)
        plt.xlabel('Pixels')
        plt.legend()
        plt.grid()

        # --- Subplot 5: FFT of input vs output ---
        plt.subplot(2, 3, 5)
        mask_nyq = self.f <= 1
        plt.plot(self.f[mask_nyq], self.fft_input_masked[mask_nyq], '+', label='FFT input (ideal rect)')
        plt.plot(self.f[mask_nyq], self.fft_output_masked[mask_nyq], 'o', label='FFT output (measured LSF)')
        plt.axvline(0.5, color="red", ls='--', linewidth=1.5, label='Nyquist')
        plt.title('FFT Input vs Output', fontname="Times New Roman", fontweight="bold", fontsize=20)
        plt.xlabel('Freq (cycles/pixel)')
        plt.ylabel('Normalized amplitude')
        plt.legend()
        plt.grid()

        plt.tight_layout(rect=[0, 0, 1, 0.95])
        return [self._figure, self.panel2()]

    def panel2(self):
        g_xlabel = 'sub pixel location (px)'
        if self.edge_direction == 'AL':
            g_ylabel = 'line number'
        else:
            g_ylabel = 'column number'

        x = self.record_of_inflexion_location
        y = self.inflexion_location
        lr = self.lr
        slope = lr.slope
        slope_err = lr.stderr
        angle = np.mod(np.degrees(np.arctan(slope)), 90.0)
        angle_err = np.degrees(slope_err / (1 + slope ** 2))
        self.error_angle = angle_err

        a3_b = np.where(self.x_esf == self.a3)
        ox_esf = self.x_esf[np.take(a3_b, 0)]

        self._figure2 = plt.figure(figsize=(25, 15), dpi=100)
        plt.subplots_adjust(hspace=0.5)

        if self.MTF_direction == 'AL':
            name = 'Along Track'
        else:
            name = 'Across Track'

        plt.suptitle(f"{name} MTF Results 2", fontsize=28, fontweight='bold')

        plt.subplot(2, 3, 1)
        plt.imshow(self.im_array, cmap='gray', aspect='auto')
        if self.edge_direction == 'AL':
            plt.plot(y, x, 'r+', label='edge subpixel location')
        if self.edge_direction == 'CT':
            plt.plot(x, y, 'r+', label='edge subpixel location')
        plt.title('Inflexion point location')
        plt.xlabel('Records')
        plt.legend()

        plt.subplot(2, 3, 2)
        if self.MTF_direction == 'CT':
            img = self.AL_EDGE
        else:
            img = self.CT_EDGE
        plt.imshow(img, cmap='gray', aspect='auto')
        h, w = img.shape
        zoom = 80
        cx = w // 2
        cy = h // 2
        plt.xlim(cx - zoom, cx + zoom)
        plt.ylim(cy + zoom, cy - zoom)
        plt.title(' Over Sample / Projected Edge Target, sampling : {} '.format(self.sampling))
        plt.colorbar()

        plt.subplot(2, 3, 3)
        plt.plot(x, y, '+', label='inflexion point location')
        plt.plot(x, x * lr.slope + lr.intercept, '-', label='Interpolation')
        plt.title(
            'Angle : {:.2f}'.format(self.angle)
        )
        plt.xlabel(g_xlabel)
        plt.ylabel(g_ylabel)
        plt.legend()
        plt.grid()

        plt.subplot(2, 3, 4)
        ax1 = plt.gca()
        bin_edges = self.x
        bin_means = self.nuage
        bin_count = self.N
        bin_std = self.nuage_std

        ax1.fill_between(bin_edges, bin_means - bin_std, bin_means + bin_std,
                         color='k', linewidth=0, zorder=2, label=r'$\pm 1\sigma$')
        ax1.plot(bin_edges, bin_means, '.', color='g', markersize=2, label='bin_value')
        ax2 = ax1.twinx()
        ax2.bar(bin_edges[:-1], bin_count[:-1], width=np.diff(bin_edges),
                alpha=0.2, color='c', label='esf_sample', align='edge')
        ax1.set_xlabel('Bin')
        ax1.set_ylabel('value', color='g')
        ax1.tick_params(axis='y', labelcolor='g')
        ax2.set_ylabel('bin sample', color='c')
        ax2.tick_params(axis='y', labelcolor='c')
        lines1, labels1 = ax1.get_legend_handles_labels()
        lines2, labels2 = ax2.get_legend_handles_labels()
        ax1.legend(lines1 + lines2, labels1 + labels2, loc='upper right', fontsize=9)
        plt.grid(True, alpha=0.15, axis='both')
        plt.title('Input signal')

        nuage_min = np.min(self.nuage)
        nuage_max = np.max(self.nuage)
        nuage_norm = (self.nuage - nuage_min) / (nuage_max - nuage_min)

        return self._figure2

    def doNormalization_and_compute_metrics(self, n_snr=3, saveFIG=True):
        m = np.argmax(np.abs(self.y_lsf))
        extrema = self.y_lsf[m]
        if extrema < 0:
            self.y_lsf = -self.y_lsf / (-extrema)
        if extrema > 0:
            self.y_lsf = self.y_lsf / np.max(self.y_lsf)

        self.inflexion_value = m
        a3 = int(self.x_lsf[self.inflexion_value])
        self.a3 = a3
        self.FWHM = compute_fwhm(self.y_lsf, self.sampling)
        self.fwhm = toDisplay_FWHM(self.y_lsf)

        self.computeRER()
        self.computeHEE()

        y = self.y_esf
        g = np.abs(y[1:] - y[:-1])
        i = np.argmax(g)
        a3 = self.x_esf[i]
        m_mask = [self.x > a3 - 2][0] & [self.x < a3 + 2][0]
        infl = self.x[m_mask][0]

        d = int(np.floor(n_snr * self.FWHM / self.sampling))
        interval1 = infl - d
        interval2 = infl + d

        v1 = self.nuage[self.x < interval1]
        v2 = self.nuage[self.x > interval2]

        if self.nuage[0] > self.nuage[-1:]:
            v1, v2 = v2, v1

        #self.SNR, self.mean_H, self.mean_B = compute_snr(v1, v2)

    def computeRER(self, showGraphic=False, saveFig=True):
        xo = self.x_esf
        yo = self.y_esf

        y = self.y_esf
        g = np.abs(y[1:] - y[:-1])
        i = np.argmax(g)
        a3 = xo[i]
        a3_b = np.where(self.x_esf == self.a3)

        yo_n = yo / np.max(yo)
        h_w = int((1 / self.sampling) / 2)
        v = xo[i - h_w:i + h_w + 1]
        Yo1 = yo_n[i - h_w:i + h_w + 1]
        self.RER = np.abs(Yo1[0] - Yo1[-1:])[0]

        x1 = (xo[i - h_w] - a3) * self.sampling
        x2 = (xo[i + h_w] - a3) * self.sampling
        y1 = np.interp(x1 / self.sampling + a3, xo, yo_n)
        y2 = np.interp(x2 / self.sampling + a3, xo, yo_n)
        self.RER_points = {"x": np.array([x1, x2]), "y": np.array([y1, y2])}

    def computeHEE(self, showGraphic=False, saveFig=True):
        xo = self.x_esf
        yo = self.y_esf
        sc = self.sampling

        y = self.y_esf
        g = np.abs(y[1:] - y[:-1])
        i = np.argmax(g)
        a3 = xo[i]

        yo_n = yo / np.max(yo)

        if yo_n[-1] > yo_n[0]:
            comparator = lambda arr, val: arr >= val
        else:
            comparator = lambda arr, val: arr <= val

        thresholds = [0.05, 0.50, 0.95]
        indices = []
        for t in thresholds:
            mask = comparator(yo_n, t)
            if not np.any(mask):
                raise ValueError(f"No value reaches threshold {t}")
            indices.append(np.argmax(mask))

        idx05, idx50, idx95 = indices
        x05 = xo[idx05]
        x50 = xo[i]
        x95 = xo[idx95]

        x05_c = sc * (x05 - a3)
        x50_c = sc * (x50 - a3)
        x95_c = sc * (x95 - a3)

        HEE_lower = x50 - x05
        HEE_upper = x95 - x50

        self.yo_n = yo_n
        self.x05 = x05_c
        self.x50 = x50_c
        self.x95 = x95_c
        self.HEE = np.abs(0.5 * (HEE_lower + HEE_upper))
        self.HEE_lower = np.abs(HEE_lower)
        self.HEE_upper = np.abs(HEE_upper)

        plt.close()

    def _extract_gsd(self):
        try:
            gt = self.raster.GetGeoTransform()
            gsd = abs(gt[1])
            return gsd if gsd > 0 else None
        except Exception:
            return None
