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

# -*- coding: utf-8 -*-
"""
Common classes and functions shared between MTF estimator modules (mtf_knife_edge, snr).
"""

import numpy as np
from scipy import optimize, ndimage
from scipy.optimize import OptimizeWarning
from scipy import interpolate, linalg
from osgeo import gdal
import warnings
import matplotlib.pyplot as plt


def sigmoid(x, a, b, l, s):
    """Sigmoid function used for edge fitting."""
    return a + b * (1 / (1 + np.power(np.e, -l * (x + s))))


class Transect:
    """
    Represents a single row transect across an edge for MTF analysis.

    Used to find edge position at subpixel precision by fitting a sigmoid function.
    """
    __X = None
    __Y = None
    __IsValid = True
    __SigmoidParams = None
    __MinPxs = np.float64(2)  # Minimum acceptable PSF half-width (Not FWHM)
    Row = None
    EdgePx = None
    EdgeSubPx = None

    def __init__(self, x, y, row, feedback, min_pxs=None):
        """
        Initialize a Transect.

        Parameters
        ----------
        x : array-like
            X coordinates (column positions)
        y : array-like
            Y values (pixel intensities)
        row : int
            Row number in the image
        feedback : QgsProcessingFeedback
            Feedback object for logging
        min_pxs : float, optional
            Minimum pixels for PSF half-width. Defaults to 2.
        """
        self.feedback = feedback

        if min_pxs is not None:
            self.__MinPxs = np.float64(min_pxs)

        # Clean nodata values
        self.__X = np.float64(x[~np.isnan(y)])
        self.__Y = np.float64(y[~np.isnan(y)])
        self.Row = np.float64(row)
        if type(self.__X) != np.ndarray or self.__X.shape[0] <= 2 * self.__MinPxs:
            self.__IsValid = False
            return None

        self.__getEdgePx()

    def console(self, message):
        self.feedback.pushInfo(message)

    def __getEdgePx(self):
        ySmooth = ndimage.filters.gaussian_filter(self.__Y, 1)

        grad = np.abs(np.diff(ySmooth) / np.diff(self.__X))
        maxPx = self.__X[:-1][grad == np.max(grad)]

        # If there are more than one
        maxPx = [np.round(np.average(maxPx))]

        if maxPx - np.min(self.__X) < self.__MinPxs or np.max(self.__X) - maxPx < self.__MinPxs:
            self.__IsValid = False
            return None

        self.EdgePx = maxPx[0]

    def sigmoidFit(self, initGuess, expert_mode=False, debug_dir=None):
        if initGuess is None:
            initGuess = [np.min(self.__Y), np.max(self.__Y), 1.0, -self.EdgePx]

        try:
            popt, pcov = optimize.curve_fit(sigmoid, self.__X, self.__Y, p0=initGuess)
            self.__SigmoidParams = popt
            self.EdgeSubPx = -self.__SigmoidParams[3]
        except OptimizeWarning:
            return False, False
        except:
            return False, False

        if expert_mode and debug_dir:
            fig, axes = plt.subplots(1, 2, figsize=(12, 5))

            # Left panel: Data and fitted sigmoid
            axes[0].scatter(self.__X, self.__Y, c='blue', s=20, alpha=0.7, label='Data points')
            x_fit = np.linspace(np.min(self.__X), np.max(self.__X), 200)
            y_fit = sigmoid(x_fit, *popt)
            axes[0].plot(x_fit, y_fit, 'g-', linewidth=2, label='Fitted sigmoid')
            # Inflection point: x = -s, y = a + b/2
            a, b, l, s = popt
            inflection_x = -s
            inflection_y = a + b / 2
            axes[0].scatter([inflection_x], [inflection_y], c='red', s=100, marker='x', zorder=5, label=f'Inflection ({inflection_x:.2f}, {inflection_y:.1f})')
            axes[0].axvline(x=self.EdgeSubPx, color='orange', linestyle=':', linewidth=1.5, label=f'Edge SubPx: {self.EdgeSubPx:.3f}')
            axes[0].axvline(x=self.EdgePx, color='purple', linestyle=':', linewidth=1.5, alpha=0.5, label=f'Edge Px: {self.EdgePx:.1f}')
            axes[0].set_xlabel('X (column position)')
            axes[0].set_ylabel('Y (pixel intensity)')
            axes[0].set_title(f'sigmoidFit - Row {int(self.Row)}\nEdge: {self.EdgeSubPx:.3f} px')
            axes[0].legend()
            axes[0].grid(True, alpha=0.3)

            # Right panel: Residuals
            y_predicted = sigmoid(self.__X, *popt)
            residuals = self.__Y - y_predicted
            axes[1].scatter(self.__X, residuals, c='blue', s=20, alpha=0.7)
            axes[1].axhline(y=0, color='green', linestyle='-', linewidth=1)
            axes[1].set_xlabel('X (column position)')
            axes[1].set_ylabel('Residuals')
            axes[1].set_title(f'Fit Residuals\nRMSE: {np.sqrt(np.mean(residuals**2)):.4f}')
            axes[1].grid(True, alpha=0.3)

            plt.tight_layout()

            import os
            os.makedirs(debug_dir, exist_ok=True)
            fig.savefig(os.path.join(debug_dir, f'sigmoid_fit_row_{int(self.Row):04d}.png'), dpi=150)
            plt.close(fig)

        return popt, pcov

    def getRefinedData(self):
        """
        Get refined transect data aligned to edge position.

        """
        if not self.__IsValid:
            raise Exception("Invalid transects")

        a, b, _, _ = self.__SigmoidParams
        return np.array([self.__X - self.EdgeSubPx, (self.__Y - a) / b])

    def isValid(self):
        return self.__IsValid

    def invalidate(self):
        self.__IsValid = False

    def getInitGuess(self):
        return self.__SigmoidParams


# Utility functions from mtf_knife_edge.py

def read_image(image, band_number, scale, offset=1.0):
    """Read image from file and convert to radiance values."""
    sds = gdal.Open(image, gdal.GA_ReadOnly)
    im_array = (sds.GetRasterBand(int(band_number))).ReadAsArray()
    im_array = im_array * scale + offset
    return im_array


def save_array(array, f_path):
    """Save numpy array to GeoTiff file."""
    cols = array.shape[1]
    rows = array.shape[0]
    driver = gdal.GetDriverByName("GTiff")
    outRaster = driver.Create(f_path, cols, rows, 1, gdal.GDT_UInt16)
    outband = outRaster.GetRasterBand(1)
    outband.WriteArray(array)
    outband.FlushCache()


def cutLSF(lsfx, lsf, BIN_number):
    """
    Symmetrize LSF around inflection point and clip to remove tails.

    Parameters
    ----------
    lsfx : array
        X values for LSF
    lsf : array
        LSF values
    BIN_number : int
        Number of bins to consider on each side of inflection point
    """
    lsfy = np.float64(lsf)
    maximum = np.max(np.absolute(lsf))
    inflex = np.take(np.where(np.absolute(lsf) == maximum), 0)

    if lsf.shape[0] <= 2 * BIN_number:
        raise ValueError(
            f"LSF is too short to cut: length {lsf.shape[0]} <= 2 * BIN_number ({2 * BIN_number})"
        )

    delta = BIN_number

    if inflex == lsf.shape[0] / 2:
        lsfOut = lsf
        lsfxCut = lsfx
    else:
        lsfOut = lsf[inflex - int(delta) : inflex + int(delta) + 1]
        lsfxCut = lsfx[inflex - int(delta) : inflex + int(delta) + 1]

    return lsfxCut, lsfOut


def trimLSF(x, y, L_w, pas):
    """
    Trim LSF to a window of L_w pixels centered on the LSF peak.

    Parameters
    ----------
    x : array
        X positions (bin indices)
    y : array
        LSF values
    L_w : float
        Full window length in pixels (L_w = 2 * PSF_extent_radius)
    pas : float
        Sampling step (pixels per bin)

    Returns
    -------
    x_o, y_o : trimmed arrays
    """
    peak_idx = np.argmax(np.abs(y))
    half_bins = int((L_w / 2) / pas)
    a = max(peak_idx - half_bins, 0)
    b = min(peak_idx + half_bins, len(y))
    return x[a:b], y[a:b]


def toDisplay_FWHM(lsfy_res):
    """Create display array for FWHM visualization."""
    ech = []
    x = lsfy_res.shape[0]
    a = np.max(lsfy_res)
    for k in range(x):
        if lsfy_res[k] / a >= 0.5:
            e = 0.5 * a
        else:
            e = 0
        ech.append(e)
    return ech


def compute_snr(v1, v2):
    """
    Compute Signal-to-Noise Ratio from ESF plateaus.

    Parameters
    ----------
    v1 : array
        High plateau values
    v2 : array
        Low plateau values

    Returns
    -------
    SNR : float
        Signal-to-noise ratio
    mean_H : float
        Mean high intensity
    mean_B : float
        Mean low intensity
    """
    l_1 = v1.shape[0]
    l_2 = v2.shape[0]
    minimum_size = min(l_1, l_2)

    v1 = v1[:minimum_size]
    v2 = v2[:minimum_size]

    mean1 = np.mean(v1)
    mean2 = np.mean(v2)

    DN_difference = np.absolute(mean1 - mean2)

    sig1 = np.sqrt(np.sum((v1 - mean1) ** 2))
    sig2 = np.sqrt(np.sum((v2 - mean2) ** 2))

    Ecart_moyen_du_bruit = (sig1 + sig2) / 2

    SNR = DN_difference / Ecart_moyen_du_bruit

    mean_H = mean2
    mean_B = mean1

    return SNR, mean_H, mean_B


def compute_fwhm(lsf, passpline):
    """Compute Full Width at Half Maximum of LSF."""
    # Case of decreasing ESF
    if np.absolute(np.min(lsf)) > np.absolute(np.max(lsf)):
        lsf = -lsf

    # Normalization
    n = lsf / np.max(lsf)
    # Count bins above 0.5
    s = np.sum(n > 0.5)
    fwhm = s * passpline

    return fwhm


def rescaleforMTF(x, y, passpline):
    """
    Rescale LSF for MTF computation.

    Ensures LSF size is a multiple of 1/passpline and even.
    """
    l_y = y.shape[0]

    if np.mod(l_y, 1 / passpline) == 0:
        l = y.shape[0]
        xf = np.hstack((x[0:l], x[l - 1] + 1))
        yf = np.hstack((y[0:l], y[l - 1]))
    else:
        i = 0
        while np.mod(l_y - i, 1 / passpline) != 0:
            i = i + 1

        delta_left = np.floor(i / 2)
        delta_right = i - delta_left

        if i != 0:
            xf = x[int(delta_left) : l_y - int(delta_right)]
            yf = y[int(delta_left) : l_y - int(delta_right)]

    yf[0] = yf[1]
    n = yf.shape[0]
    yf[n - 1] = yf[n - 2]

    return xf, yf


def esf_to_eq_space(x1, R):
    """
    Convert non-equally spaced ESF to equally spaced using 2nd order polynomial interpolation.

    The script interpolates locally missing values.

    Returns
    -------
    x1_ech : array
        Equally spaced x values
    y1_ech_new : array
        Interpolated y values
    n_norm : float
        Normalized residual error
    """
    x1 = x1 + 1
    x1_ech = np.arange(np.min(x1), np.max(x1) + 1, 1)
    y1_ech = []

    i = 0
    for k in x1_ech:
        I = np.where(x1 == k)
        if I[0].size == 0:
            y1_ech.append(0)
        else:
            y1_ech.append(R[i])
            i = i + 1

    y1_ech = np.array(y1_ech)

    # Remove zeros at beginning and end
    M = np.array([], dtype="i")
    i = 0
    while y1_ech[i] == 0:
        i = i + 1
        M = np.hstack((M, i))
    i = 1
    while y1_ech[len(y1_ech) - i] == 0:
        i = i + 1
        M = np.hstack((M, i - 1))

    if len(M) != 0:
        x1_ech = np.delete(x1_ech, M, 0)
        y1_ech = np.delete(y1_ech, M, 0)

    y1_ech_new = np.copy(y1_ech)

    try:
        I = np.where(y1_ech == 0)
        n_norm = 0
        for k in I[0]:
            Le = np.where(np.logical_and(x1_ech < k + 1, y1_ech > 0))
            Ri = np.where(np.logical_and(x1_ech > k + 1, y1_ech > 0))
            h_w_le = np.minimum(2, Le[0].size)
            h_w_ri = np.minimum(2, Ri[0].size)
            w = np.hstack((Le[0][Le[0].size - h_w_le :], Ri[0][0:h_w_ri]))
            x = x1_ech[w]
            y = y1_ech[w]
            if x.size < 3:
                p, res, _, _, _ = np.polyfit(x, y, 8, full=True)
            else:
                p, res, _, _, _ = np.polyfit(x, y, 2, full=True)

            if res.size == 0:
                norm_res = 0
            else:
                predict = np.polyval(p, x)
                residual_variables = predict - y
                norm_res = np.sqrt(np.sum(residual_variables**2))

            n_norm = n_norm + norm_res
            in_ = x1_ech[k]
            y1_ech_new[k] = np.polyval(p, in_)
        n_norm = n_norm / I[0].size
    except:
        n_norm = 0

    return x1_ech, y1_ech_new, n_norm


def ahamming(n, mid):
    """Compute asymmetric Hamming window centered at mid."""
    data = np.zeros(n)
    wid1 = mid - 1
    wid2 = n - mid
    wid = np.maximum(wid1, wid2)
    pie = np.pi

    for i in range(n):
        if i == 0:
            arg = 1 - mid
            data[i] = np.cos(pie * arg / wid)
        else:
            arg = i + 1 - mid
            data[i] = np.cos(pie * arg / wid)

    data = 0.54 + 0.46 * data

    return data


CLEAN_MAX_JUMP = 0.5  # largest bin-to-bin ESF jump kept by clean_nuage, as a fraction of the ESF range


def clean_threshold(R, max_jump=CLEAN_MAX_JUMP):
    """
    Outlier threshold of clean_nuage for the binned ESF values R: a fraction of their range, so
    that the cleaning does not depend on the radiometric scale (an absolute threshold removed
    the edge transition itself in DN, and nothing at all in small radiance units). Real
    bin-to-bin steps of the test ESFs stay below 20 % of the range.
    """
    return max_jump * (np.nanmax(R) - np.nanmin(R))


def clean_nuage(x1, R, R_std, N, th=500):
    """
    Clean point cloud by removing outliers.

    Parameters
    ----------
    x1 : array
        X coordinates
    R : array
        Values
    R_std : array
        Standard deviations
    N : array
        Counts
    th : float
        Threshold for outlier detection
    """
    M = np.ones(R.shape)
    for i in range(1, len(x1)):
        if np.absolute(R[i] - R[i - 1]) > th:
            M[i] = 0

    xclean = x1
    Rclean = R
    R_std_clean = R_std

    clean_mode = 2
    if clean_mode == 1:
        xclean = np.delete(xclean, np.logical_or(M == 0, N == 1), 0)
        Rclean = np.delete(Rclean, np.logical_or(M == 0, N == 1), 0)
        R_std_clean = np.delete(R_std_clean, np.logical_or(M == 0, N == 1), 0)

    if clean_mode == 2:
        xclean = np.delete(xclean, M == 0, 0)
        Rclean = np.delete(Rclean, M == 0, 0)
        R_std_clean = np.delete(R_std_clean, M == 0, 0)

    return xclean[~np.isnan(Rclean)], Rclean[~np.isnan(Rclean)], R_std_clean[~np.isnan(Rclean)], M

def fLOESS(noisy, span):
    """
    LOESS smoothing (locally weighted regression using 2nd order polynomial).

    Author: Gabriel Marsh

    Parameters
    ----------
    noisy : array
        (nx1) vector or (nx2) array with x-data in first column
    span : float
        Fraction of data to use (minimum span = 4/n)

    Returns
    -------
    smoothed : array
        Smoothed data points
    """
    if noisy.shape[1] < 2:
        noisy = np.concatenate(
            (np.arange(0, len(noisy)).reshape(1, -1).conj().transpose(), noisy), axis=1
        )

    x = noisy[:, 0]
    y = noisy[:, 1]
    n = noisy.shape[0]
    r = x[-1] - x[0]
    hlims = np.array(
        [
            [span, x[0]],
            [span / 2, x[0] + r * span / 2],
            [span / 2, x[0] + r * (1 - span / 2)],
            [span, x[-1]],
        ]
    )
    smoothed = np.zeros(n)

    for i in range(0, n):
        h = interpolate.interp1d(hlims[:, 1], hlims[:, 0])
        h = h(x[i])
        w = (1 - np.absolute((x / np.max(x) - x[i] / np.max(x)) / h) ** 3) ** 3

        w_idx = w > 0
        w_ = w[w_idx]
        x_ = x[w_idx]
        y_ = y[w_idx]

        XX = np.array(
            [
                [np.nansum(w_ * x_**0), np.nansum(w_ * x_**1), np.nansum(w_ * x_**2)],
                [np.nansum(w_ * x_**1), np.nansum(w_ * x_**2), np.nansum(w_ * x_**3)],
                [np.nansum(w_ * x_**2), np.nansum(w_ * x_**3), np.nansum(w_ * x_**4)],
            ]
        )

        YY = np.array(
            [
                [np.nansum(w_ * y_ * (x_**0))],
                [np.nansum(w_ * y_ * (x_**1))],
                [np.nansum(w_ * y_ * (x_**2))],
            ]
        )
        warnings.filterwarnings("ignore")
        CC = linalg.solve(XX, YY).ravel()

        smoothed[i] = CC[0] + CC[1] * x[i] + CC[2] * x[i] ** 2

    return smoothed


def remove_nan_borders(image):
    """
    Remove rows and columns that contain only NaN values.

    Parameters
    ----------
    image : np.ndarray
        2D array from which to remove all-NaN rows and columns.

    Returns
    -------
    np.ndarray
        Trimmed 2D array with all-NaN rows and columns removed.
    """
    mask_rows = ~np.all(np.isnan(image), axis=1)
    mask_cols = ~np.all(np.isnan(image), axis=0)
    return image[np.ix_(mask_rows, mask_cols)]


def knife_edge1(x, lsf1, passpline):
    # S.Saunier 02 Mars 2017
    # knife_edge1 applies LSF normalization and FFT
    # MTF computation at Nyquist frequency
    # I :   x         : Spatial Values
    #       lsf1      : Corresponding LSF Values
    #       passpline : LSF sampling factor
    # O :
    #       f      : Frequency Values
    #       mtf    : Corresponding MTF Values
    #       mtfnyq : MTF Value @ nyquist

    # LSF normalization:
    lsf = lsf1 / np.absolute(np.max(lsf1))

    # FFT of the normalized LSF:
    fft_lsf = np.absolute(np.fft.fft(lsf))

    # FFT normalization:
    n_fft_lsf = fft_lsf / fft_lsf[0]

    # Input signal Nyquist frequency:
    L_w = passpline * (lsf.shape[0])
    print('   - Edge width (period)      : {:.2f} pixels'.format(L_w))
    print('   - Nyquist @ L_w/2          : {:.2f} pixels'.format(L_w / 2))
    nyquist = L_w / 2
    # nyquist = (passpline * n_fft_lsf.shape[0] / 2) + 1

    nn = lsf1.shape[0]
    freq = np.zeros((nn, 1))
    del_ = 1  # del_ is used because del is python function
    for n in range(0, nn):
            freq[n] = ((n + 1) - 1) / (del_ * nn * passpline)  # ((n+1)-1) is used instead of (n-1) because of python index

    f = freq.conj().transpose()

    f = f[0, :int(np.round(nyquist * 2) - 1)]
    mtf = n_fft_lsf[:int(np.round(nyquist * 2) - 1)]

    # Find MTF @ Nyquist frequency:
    s2 = np.where(f > 0.5)
    if s2[0].size == 0 or not np.any(f <= 0.5):
        raise ValueError("Cannot find the MTF at Nyquist: the frequency axis is too short (ROI too small or no usable edge).")
    i_2 = s2[0][0]
    s1 = np.where(f <= 0.5)
    i_1 = s1[0][-1:][0]  # used for python index
    f2 = f[i_2]
    f1 = f[i_1]
    mtf2 = mtf[i_2]
    mtf1 = mtf[i_1]

    pente = (mtf2 - mtf1) / (f2 - f1)
    ord_ = mtf2 - pente * f2  # "ord_" is used instead of "ord" because of python index
    mtfnyq = pente * 0.5 + ord_

    # Display:
    for k, rec in enumerate(f):
        print('Frequence / MTF  : {:.4f} / {:.4f}'.format(rec, mtf[k]))
    # print('Frequencies                                    : ', f)
    # print('MTF at fn                                      : ', mtf)
    print('MTF at Nyquist frequency fn=fe/2 (interpolated) : {:.2f}'.format(mtfnyq))

    return f, mtf, mtfnyq


def sgolayfilt(k, f):
    """
    Savitzky-Golay differentiation filters.

    Parameters
    ----------
    k : int
        Polynomial order (must be less than f)
    f : int
        Window size (must be odd)

    Returns
    -------
    G : array
        Matrix of differentiation filters
    """
    s = np.vander(np.arange(0.5 * (1 - f), 0.5 * (1 + f)))
    S = s[:, f : f - k - 2 : -1]
    _, R = np.linalg.qr(S)
    G = np.linalg.solve(R.conj().transpose().T, np.linalg.solve(R.T, S.T).T.T).T

    return G
