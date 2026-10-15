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

#! /usr/bin/env python
import numpy as np
from scipy.optimize import curve_fit
from scipy.special import erf
from scipy import interpolate, linalg


# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────────

def _find_edge_window(x_samples, y_samples, subpixel_step):
    """
    Detect the edge inflection point and select a symmetric window around it.

    The inflection point is where the absolute first-difference of the ESF is
    maximum (steepest slope = edge centre). The window is then made as wide as
    possible while remaining symmetric about that centre.

    Parameters
    ----------
    x_samples : ndarray
        Irregularly-spaced sample positions along the edge profile.
    y_samples : ndarray
        Raw ESF values at each sample position.
    subpixel_step : float
        Subpixel oversampling step (number of samples per pixel).

    Returns
    -------
    x_edge : float
        Position of the inflection point (edge centre).
    x_window : ndarray
        Sample positions within the symmetric window.
    y_window : ndarray
        ESF values within the symmetric window.
    """
    gradient = np.abs(y_samples[1:] - y_samples[:-1])
    edge_idx = np.nanargmax(gradient)
    x_edge = x_samples[edge_idx]

    # Largest symmetric window that fits within the profile
    half_width_px = np.minimum(x_edge, x_samples[-1] - x_edge) * subpixel_step
    half_width_samples = half_width_px / subpixel_step
    idx_left  = np.where(x_samples > x_edge - half_width_samples)[0][0]
    idx_right = np.where(x_samples < x_edge + half_width_samples)[0][-1]

    x_window = x_samples[idx_left:idx_right]
    y_window = y_samples[idx_left:idx_right]

    return x_edge, x_window, y_window


def _compute_errors(x_samples, y_samples, x_regular, esf_fitted):
    """
    Compute L2 norm and RMS error between the fitted ESF and the raw samples.

    Both signals are normalised to [0, 1] before computing residuals so that
    the metrics are comparable across images with different DN ranges.

    The fitted ESF (defined on a regular grid) is interpolated back onto the
    original irregular sample positions before computing the residuals.

    Parameters
    ----------
    x_samples : ndarray
        Original irregular sample positions.
    y_samples : ndarray
        Original raw ESF values.
    x_regular : ndarray
        Regular grid on which esf_fitted is defined.
    esf_fitted : ndarray
        Fitted ESF values on the regular grid.

    Returns
    -------
    l2_error : float
        L2 norm of the normalised residuals.
    rms_error : float
        Root-mean-square of the normalised residuals.
    """
    y_min, y_max   = np.min(y_samples), np.max(y_samples)
    y_norm         = (y_samples - y_min) / (y_max - y_min)
    esf_norm       = (esf_fitted - y_min) / (y_max - y_min)
    y_resampled    = np.interp(x_samples, x_regular, esf_norm)
    residuals      = y_resampled - y_norm
    l2_error       = np.sqrt(np.sum(residuals ** 2))
    rms_error      = np.sqrt(np.mean(residuals ** 2))
    return l2_error, rms_error


# ─────────────────────────────────────────────────────────────────────────────
# ESF models
# ─────────────────────────────────────────────────────────────────────────────

def fun(x, a1, a2, a3, lambda_f):
    return a1 / (1 + np.exp(-(lambda_f) * (x - a3))) + (a2)


def sigmoide(x, R, passpline):

    x1 = x
    y = R
    R2 = []

    g = np.abs(y[1:] - y[:-1])

    xinflexion = np.where([g >= np.max(g)])
    a3 = x1[int(xinflexion[1][0])]
    v = y[int(xinflexion[1][0])]

    pixel_half_width = (x1[-1:][0] * passpline) / 2

    pixel_half_width = np.minimum(a3, x1[-1:][0] - a3) * passpline
    I1 = np.where([x1 > a3 - pixel_half_width * 1. / passpline])
    bmin = I1[1][0]
    I2 = np.where([x1 < a3 + pixel_half_width * 1. / passpline])
    bmax = I2[1][-1:][0]

    x_cut = x1[bmin:bmax]
    y_cut = y[bmin:bmax]

    i = int(np.where(x_cut == a3)[0][0])
    nb_measurement_left = len(x_cut[bmin: i])
    nb_measurement_right = len(x_cut[i: bmax])

    k = 10
    a2 = np.mean(y_cut[1:k])
    l = len(y_cut)
    a1 = np.mean(y_cut[l - k:l]) - a2
    lambdafin, Err2 = recherche_lambda(a1, a2, a3,
                                    y, x1,
                                    0.0, 0.5, passpline)
    R2.append(Err2)
    a3i = a3
    a3, Err2 = recherche_a3(a3, a1, a2, lambdafin,
                         y_cut, x_cut, passpline, 20)
    R2.append(Err2)

    bmin = lambdafin - 0.2
    bmax = lambdafin + 0.2
    pas = 0.001

    lambdafin, Err2 = recherche_lambda(a1, a2, a3, y_cut, x_cut, bmin, bmax, pas)
    R2.append(Err2)

    l = len(x)
    lmax = x1[-1:]

    x_ech = np.linspace(1, x1[-1:][0], int(x1[-1:][0]))
    esfP_0 = a1 / (1 + np.exp(-(lambdafin) * (x_ech - a3))) + (a2)

    R2.append(norme_L2(R, a1 / (1 + np.exp(-(lambdafin) * (x - a3))) + (a2)))

    param = [a1, a2, a3, lambdafin]
    popt, pcov = curve_fit(fun, x, y, p0=param)

    x_ech = np.linspace(1, x1[-1:][0], int(x1[-1:][0]))
    a1 = popt[0]
    a2 = popt[1]
    a3 = popt[2]
    lambdafin = popt[3]

    esfP = a1 / (1 + np.exp(-lambdafin * (x_ech - a3))) + a2
    R2.append(norme_L2(R, a1 / (1 + np.exp(-(lambdafin) * (x - a3))) + (a2)))

    R_min, R_max = np.min(R), np.max(R)
    R_norm = (R - R_min) / (R_max - R_min)
    esfP_norm = (esfP - R_min) / (R_max - R_min)
    R2_norm = np.sqrt(np.sum((np.interp(x, x_ech, esfP_norm) - R_norm) ** 2))
    RMSE_norm = np.sqrt(np.mean((np.interp(x, x_ech, esfP_norm) - R_norm) ** 2))

    R_int = a1 / (1 + np.exp(-(lambdafin) * (x - a3))) + (a2)
    RMS = np.sqrt(np.mean((R_int - R) ** 2))

    L2 = R2[-1]

    return x_ech, esfP, R2_norm, RMSE_norm, x_cut, y_cut


def esf_tanh(x, R, passpline):
    from scipy.special import expit
    from scipy.optimize import curve_fit
    import numpy as np

    # ---- Inflection point detection (identical to sigmoid)
    g = np.abs(R[1:] - R[:-1])
    idx0 = np.argmax(g)
    x0 = x[idx0]

    pixel_half_width = (x[-1:][0] * passpline) / 2

    pixel_half_width = np.minimum(x0, x[-1:][0] - x0) * passpline
    I1 = np.where([x > x0 - pixel_half_width * 1. / passpline])
    bmin = I1[1][0]
    I2 = np.where([x < x0 + pixel_half_width * 1. / passpline])
    bmax = I2[1][-1:][0]

    x_cut = x[bmin:bmax]
    y_cut = R[bmin:bmax]

    # ---- TZANNES & MOONEY ESF Model
    def esf_model(x, A, B_x, x0_fit, D):
        return A * np.tanh((x - x0_fit) / B_x) + D

    # ---- Separation before / after inflection point
    i = np.argmin(np.abs(x_cut - x0))

    # Direction detection + normalization (before p0)
    n_side = min(20, len(y_cut) // 10)
    left_mean = np.mean(y_cut[:n_side])
    right_mean = np.mean(y_cut[-n_side:])

    if right_mean > left_mean:
        low, high = left_mean, right_mean
    else:
        low, high = right_mean, left_mean

    amplitude = high - low

    # ---- Parameter initialization
    A = amplitude
    B_x = 100
    k = 10
    D = np.mean(y_cut[:k])
    x0_fit = x0
    p0 = [A, B_x, x0_fit, D]

    popt, _ = curve_fit(esf_model, x_cut, y_cut, p0=p0, maxfev=10000)

    # ---- Regular ESF reconstruction
    x_ech = np.linspace(1, x[-1], int(x[-1]))
    esfP = esf_model(x_ech, *popt)

    # ---- L2 error
    R_interp = np.interp(x, x_ech, esfP)
    R2 = np.linalg.norm(R_interp - R)
    R2 = float(R2)

    # ---- RMS
    RMS = np.sqrt(np.mean((R_interp - R)**2))

    # --- L2 error after normalization
    R_min, R_max = np.min(R), np.max(R)
    R_norm = (R - R_min) / (R_max - R_min)
    esfP_norm = (esfP - R_min) / (R_max - R_min)
    R2_norm = [np.sqrt(np.sum((np.interp(x, x_ech, esfP_norm) - R_norm) ** 2))]
    R2_norm = R2_norm[0]
    RMSE_norm = np.sqrt(np.mean((np.interp(x, x_ech, esfP_norm) - R_norm)**2))

    return x_ech, esfP, R2_norm, RMSE_norm, x_cut, y_cut


def esf_fermi(x, R, passpline):
    from scipy.optimize import curve_fit
    import numpy as np
    from scipy.special import expit

    # ---- Inflection point detection
    g = np.abs(R[1:] - R[:-1])
    idx0 = np.argmax(g)
    x0 = x[idx0]

    pixel_half_width = np.minimum(x0, x[-1] - x0) * passpline
    I1 = np.where(x > x0 - pixel_half_width / passpline)[0]
    I2 = np.where(x < x0 + pixel_half_width / passpline)[0]
    bmin, bmax = I1[0], I2[-1]

    x_cut = x[bmin:bmax]
    y_cut = R[bmin:bmax]

    # ---- Stable ESF model
    def esf_model(x, a1, b1, c1, a2, b2, c2, a3, b3, c3, D):
        f1 = a1 / (1 + np.exp(-b1 * (x - c1)))
        f2 = a2 / (1 + np.exp(-b2 * (x - c2)))
        f3 = a3 / (1 + np.exp(-b3 * (x - c3)))
        return f1 + f2 + f3 + D

    left_mean = np.mean(y_cut[:20])
    right_mean = np.mean(y_cut[-20:])

    if right_mean > left_mean:
        low_level = left_mean
        high_level = right_mean
        transition_sign = +1
    else:
        low_level = right_mean
        high_level = left_mean
        transition_sign = -1

    amplitude_total = high_level - low_level

    if transition_sign < 0:
        y_normalized = high_level + low_level - y_cut
    else:
        y_normalized = y_cut

    mid_value = (low_level + high_level) / 2

    idx = np.where(y_normalized >= mid_value)[0]
    if len(idx) > 0:
        i = idx[0]
        if i > 0:
            frac = (mid_value - y_normalized[i - 1]) / (y_normalized[i] - y_normalized[i - 1])
            x0 = x_cut[i - 1] + frac * (x_cut[i] - x_cut[i - 1])
        else:
            x0 = x_cut[0]
    else:
        x0 = np.mean(x_cut)

    side_n = max(10, len(y_normalized) // 10)

    D_init = np.mean(y_normalized[:side_n])
    a_total = np.mean(y_normalized[-side_n:]) - D_init

    a1_init = 0.70 * a_total
    a2_init = 0.15 * a_total
    a3_init = 0.15 * a_total

    c1_init = x0
    c2_init = x0 + 0.8
    c3_init = x0 - 0.8

    b_init = 6.0
    b1_init = b_init
    b2_init = b_init * 0.4
    b3_init = b_init * 0.4

    p0 = [a1_init, b1_init, c1_init,
          a2_init, b2_init, c2_init,
          a3_init, b3_init, c3_init,
          D_init]

    popt, _ = curve_fit(esf_model, x_cut, y_cut, p0=p0, maxfev=20000)

    # ---- Regular ESF reconstruction
    x_ech = np.linspace(1, x[-1], int(x[-1]))
    esfP = esf_model(x_ech, *popt)

    # ---- L2 error
    R2 = [np.sqrt(np.sum((np.interp(x, x_ech, esfP) - R)**2))]
    R2 = R2[0]

    # ---- RMS
    RMS = np.sqrt(np.mean((np.interp(x, x_ech, esfP) - R)**2))

    # --- L2 error after normalization
    R_min, R_max = np.min(R), np.max(R)
    R_norm = (R - R_min) / (R_max - R_min)
    esfP_norm = (esfP - R_min) / (R_max - R_min)
    R2_norm = [np.sqrt(np.sum((np.interp(x, x_ech, esfP_norm) - R_norm) ** 2))]
    R2_norm = R2_norm[0]
    RMSE_norm = np.sqrt(np.mean((np.interp(x, x_ech, esfP_norm) - R_norm)**2))

    return x_ech, esfP, R2_norm, RMSE_norm, x_cut, y_cut


def esf_gauss_exp_param(x, R, passpline):
    from scipy.special import erf
    from scipy.optimize import curve_fit
    import numpy as np

    # ---- Inflection point detection (identical to sigmoid)
    g = np.abs(R[1:] - R[:-1])
    idx0 = np.argmax(g)
    x0 = x[idx0]

    pixel_half_width = np.minimum(x0, x[-1:][0] - x0) * passpline
    I1 = np.where([x > x0 - pixel_half_width * 1. / passpline])
    bmin = I1[1][0]
    I2 = np.where([x < x0 + pixel_half_width * 1. / passpline])
    bmax = I2[1][-1:][0]

    x_cut = x[bmin:bmax]
    y_cut = R[bmin:bmax]

    # ---- ESF Model Yin et al.
    def esf_model(x, A, x0, sigma, B, lambd, C):
        gauss = 0.5 * A * (1 + erf((x - x0) / (np.sqrt(2) * sigma)))
        exp = B * np.exp(-np.abs(x - x0) / lambd)
        return gauss + exp + C

    # Direction detection + normalization (before p0)
    n_side = min(20, len(y_cut) // 10)
    left_mean = np.mean(y_cut[:n_side])
    right_mean = np.mean(y_cut[-n_side:])

    if right_mean > left_mean:
        low, high = left_mean, right_mean
    else:
        low, high = right_mean, left_mean

    amplitude = high - low

    A0 = amplitude
    sigma0 = max(1.0, pixel_half_width / 5.0)
    B0 = 0.08 * A0
    lambda0 = pixel_half_width / 3.0
    k = 10
    C0 = np.mean(y_cut[:k])

    p0 = [A0, x0, sigma0, B0, lambda0, C0]

    popt, _ = curve_fit(esf_model, x_cut, y_cut, p0=p0, maxfev=10000)

    # ---- Regular ESF reconstruction
    x_ech = np.linspace(1, x[-1], int(x[-1]))
    esfP = esf_model(x_ech, *popt)

    # ---- L2 error
    R2 = [np.sqrt(np.sum((np.interp(x, x_ech, esfP) - R)**2))]
    R2 = R2[0]

    # ---- RMS
    R_interp = np.interp(x, x_ech, esfP)
    RMS = np.sqrt(np.mean((R_interp - R)**2))

    # --- L2 error after normalization
    R_min, R_max = np.min(R), np.max(R)
    R_norm = (R - R_min) / (R_max - R_min)
    esfP_norm = (esfP - R_min) / (R_max - R_min)
    R2_norm = [np.sqrt(np.sum((np.interp(x, x_ech, esfP_norm) - R_norm) ** 2))]
    R2_norm = R2_norm[0]
    RMSE_norm = np.sqrt(np.mean((np.interp(x, x_ech, esfP_norm) - R_norm)**2))

    return x_ech, esfP, R2_norm, RMSE_norm, x_cut, y_cut


def esf_loess(x, R, degree=2, frac=0.01):
    from loess.loess_1d import loess_1d

    xnew = np.arange(np.min(x), np.max(x)+1, 1)
    xout, y_smooth, wout = loess_1d(x, R, xnew=xnew, degree=degree, frac=frac)
    x_ech = xout
    esfP = y_smooth

    # ---- L2 error
    R2 = [np.sqrt(np.sum((np.interp(x, x_ech, esfP) - R)**2))]
    R2 = R2[0]

    # ---- RMS
    RMS = np.sqrt(np.mean((np.interp(x, x_ech, esfP) - R)**2))

    # --- L2 error after normalization
    R_min, R_max = np.min(R), np.max(R)
    R_norm = (R - R_min) / (R_max - R_min)
    esfP_norm = (esfP - R_min) / (R_max - R_min)
    R2_norm = [np.sqrt(np.sum((np.interp(x, x_ech, esfP_norm) - R_norm) ** 2))]
    R2_norm = R2_norm[0]
    RMSE_norm = np.sqrt(np.mean((np.interp(x, x_ech, esfP_norm) - R_norm)**2))

    return x_ech, esfP, R2_norm, RMSE_norm


def esf_modified_sgolay(x_regular, y_regular, x_samples, y_samples):
    """
    Smooth a pre-sampled ESF using fLOESS (locally weighted 2nd-order regression).

    Note: despite the name, this function does NOT use Savitzky-Golay filtering.
    It applies the internal fLOESS smoother to a regularly-sampled input.
    The caller is responsible for building the regular grid before calling this.

    Parameters
    ----------
    x_regular : ndarray
        Pre-built regularly-spaced sample positions (input to fLOESS).
    y_regular : ndarray
        ESF values on the regular grid (may contain noise).
    x_samples : ndarray
        Original irregular sample positions (used only for error computation).
    y_samples : ndarray
        Original raw ESF values (used only for error computation).

    Returns
    -------
    x_regular : ndarray
        Same as input x_regular.
    esf_fitted : ndarray
        fLOESS-smoothed ESF on the regular grid.
    l2_error : float
        L2 norm of the residuals against the raw samples.
    rms_error : float
        RMS of the residuals against the raw samples.
    """
    noisy      = np.vstack((x_regular, y_regular)).T
    span       = 8 / noisy.shape[0]
    esf_fitted = fLOESS(noisy, span)

    l2_error, rms_error = _compute_errors(x_samples, y_samples, x_regular, esf_fitted)

    return x_regular, esf_fitted, l2_error, rms_error, None, None


def esf_to_eq_space_polynomial(x1, R):
    # 2 nd order polynomial interpolation of input R , to produce
    # equally spaced series

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

    M = np.array([], dtype='i')
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
            w = np.hstack((Le[0][Le[0].size - h_w_le:], Ri[0][0:h_w_ri]))
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
                norm_res = np.sqrt(np.sum(residual_variables ** 2))

            n_norm = n_norm + norm_res
            in_ = x1_ech[k]
            y1_ech_new[k] = np.polyval(p, in_)
        n_norm = n_norm / I[0].size
    except:
        n_norm = 0

    x1_ech = x1_ech
    y1_ech = y1_ech_new
    noisy = np.vstack((x1_ech.conj().transpose(),
                       y1_ech.conj().transpose())).transpose()

    span = 8 / noisy.shape[0]
    v = fLOESS(noisy, span)

    esfP = v.conj().transpose()
    x_cut = x1_ech
    y_cut = esfP

    # ---- L2 error
    R2 = [np.sqrt(np.sum((np.interp(x1, x1_ech, esfP) - R) ** 2))]
    R2 = R2[0]

    # ---- RMS
    RMS = np.sqrt(np.mean((np.interp(x1, x1_ech, esfP) - R) ** 2))

    # --- L2 error after normalization
    R_min, R_max = np.min(R), np.max(R)
    R_norm = (R - R_min) / (R_max - R_min)
    esfP_norm = (esfP - R_min) / (R_max - R_min)
    R2_norm = [np.sqrt(np.sum((np.interp(x1, x1_ech, esfP_norm) - R_norm) ** 2))]
    R2_norm = R2_norm[0]
    RMSE_norm = np.sqrt(np.mean((np.interp(x1, x1_ech, esfP_norm) - R_norm)**2))

    return x1_ech, esfP, R2_norm, RMSE_norm


def esf_erf(x, R, passpline):
    from scipy.special import erf
    from scipy.optimize import curve_fit
    import numpy as np

    # ---- Inflection point detection (identical to sigmoid)
    g = np.abs(R[1:] - R[:-1])
    idx0 = np.argmax(g)
    x0 = x[idx0]

    pixel_half_width = (x[-1:][0] * passpline) / 2

    pixel_half_width = np.minimum(x0, x[-1:][0] - x0) * passpline
    I1 = np.where([x > x0 - pixel_half_width * 1. / passpline])
    bmin = I1[1][0]
    I2 = np.where([x < x0 + pixel_half_width * 1. / passpline])
    bmax = I2[1][-1:][0]

    x_cut = x[bmin:bmax]
    y_cut = R[bmin:bmax]

    # ---- Turkey BILSAT ESF Model
    def esf_model(x, x0, a, b, c1, c2, c3, s, sigma):
        gauss = erf((x - x0) / (np.sqrt(2) * sigma))
        w = 0.5 * (1 + np.cos((2 * np.pi * (x - x0)) / s))
        f1 = c1 * (x - x0)
        f2 = ((x - x0) ** 3) * c2
        f3 = ((x - x0) ** 5) * c3
        return a + b * gauss + w * (f1 + f2 + f3)

    # ---- Parametres
    k = 10
    a = np.mean(y_cut[:k])
    b = (np.max(y_cut) - np.min(y_cut)) / 2
    c1 = c2 = c3 = 0.15
    s = 2 * pixel_half_width
    sigma0 = pixel_half_width / 2.0

    p0 = [x0, a, b, c1, c2, c3, s, sigma0]

    popt, _ = curve_fit(esf_model, x_cut, y_cut, p0=p0, maxfev=10000)

    # ---- Regular ESF reconstruction
    x_ech = np.linspace(1, x[-1], int(x[-1]))
    esfP = esf_model(x_ech, *popt)

    # ---- L2 error
    R2 = [np.sqrt(np.sum((np.interp(x, x_ech, esfP) - R)**2))]
    R2 = R2[0]

    # ---- RMS
    RMS = np.sqrt(np.mean((np.interp(x, x_ech, esfP) - R)**2))

    # --- L2 error after normalization
    R_min, R_max = np.min(R), np.max(R)
    R_norm = (R - R_min) / (R_max - R_min)
    esfP_norm = (esfP - R_min) / (R_max - R_min)
    R2_norm = [np.sqrt(np.sum((np.interp(x, x_ech, esfP_norm) - R_norm) ** 2))]
    R2_norm = R2_norm[0]
    RMSE_norm = np.sqrt(np.mean((np.interp(x, x_ech, esfP_norm) - R_norm)**2))

    return x_ech, esfP, R2_norm, RMSE_norm, x_cut, y_cut


# ─────────────────────────────────────────────────────────────────────────────
# Utility functions
# ─────────────────────────────────────────────────────────────────────────────

def fLOESS(noisy, span):
    #
    # Author; Gabriel Marsh
    # Latest revision;
    # 22/02/2016    Included non-uniformly spaced x-data capability
    #
    ## DESCRIPTION
    # Function fLOESS performs LOESS (locally weighted regression fitting using
    # a 2nd order polynomial) to one dimensional data. This might be considered
    # a better approach to LOWESS, which produces a locally weighted regression
    # using a linear fit.
    #
    ## INPUTS
    # noisy     =   An (nx_samples) vector containing the noisy data to be smoothed,
    #               or, and(nx2) array containing x-data in the first column
    #               (increasing values) and noisy y-data in the second column.
    # span      =   A value specifying the fraction of data to use with the
    #               fitting procedure. Minimum value is span = 4/n
    #
    ## OUTPUTS
    # smoothed  =   An (nx_samples) vector of smoothed data points
    #

    ## Error checking

    # Default x-data

    if noisy.shape[1] < 2:
        noisy = np.concatenate((np.arange(0, len(noisy)).reshape(1, -1).conj().transpose(), noisy), axis=1)

    ## Smooth the data points

    x = noisy[:, 0]
    y = noisy[:, 1]
    n = noisy.shape[0]
    r = x[-1] - x[0]
    hlims    = np.array([[span, x[0]], [span / 2, x[0] + r * span / 2],
                          [span / 2, x[0] + r * (1 - span / 2)], [span, x[-1]]])
    smoothed = np.zeros(n)

    for i in range(0, n):
        h = interpolate.interp1d(hlims[:, 1], hlims[:, 0])
        h = h(x[i])
        w = (1 - np.absolute((x / np.max(x) - x[i] / np.max(x)) / h) ** 3) ** 3

        w_idx = w > 0
        w_ = w[w_idx]
        x_ = x[w_idx]
        y_ = y[w_idx]

        XX = np.array([[np.nansum(w_ * x_ ** 0), np.nansum(w_ * x_ ** 1), np.nansum(w_ * x_ ** 2)],
                       [np.nansum(w_ * x_ ** 1), np.nansum(w_ * x_ ** 2), np.nansum(w_ * x_ ** 3)],
                       [np.nansum(w_ * x_ ** 2), np.nansum(w_ * x_ ** 3), np.nansum(w_ * x_ ** 4)]])

        YY = np.array(
            [[np.nansum(w_ * y_ * (x_ ** 0))], [np.nansum(w_ * y_ * (x_ ** 1))], [np.nansum(w_ * y_ * (x_ ** 2))]])

        CC = linalg.solve(XX, YY)

        smoothed[i] = CC[0] + CC[1] * x[i] + CC[2] * x[i] ** 2

    return smoothed


def norme_L2(y, ychapeau2):
    v  = ychapeau2 - y
    R2 = np.power(np.sum(v * v), 0.5)
    return R2


def recherche_a3(a3i, a1, a2, lambda_v, y, x_samples, pas, amplitude):
    """Grid search for the best x_center (a3) of the sigmoid."""
    residus   = []
    bmin      = a3i - amplitude
    bmax      = a3i + amplitude
    step_count = (bmax - bmin) / pas
    u = np.linspace(bmin, bmax, int(step_count) + 1)

    for i in u:
        ychapeau2 = a1 / (1 + np.exp(-lambda_v * (x_samples - i))) + a2
        residus.append(norme_L2(y, ychapeau2))

    if np.isnan(residus).all():
        return np.nan, np.nan

    residu = np.nanmin(residus)
    value  = u[np.nanargmin(residus)]
    return value, residu


def recherche_lambda(a1, a2, a3, y, x_samples, bmin, bmax, pas):
    """Grid search for the best slope (lambda) of the sigmoid."""
    residus    = []
    step_count = (bmax - bmin) / pas
    u = np.linspace(bmin, bmax, int(step_count) + 1)

    for lambda_v in u:
        ychapeau2 = a1 / (1 + np.exp(-lambda_v * (x_samples - a3))) + a2
        residus.append(norme_L2(y, ychapeau2))

    if np.isnan(residus).all():
        return np.nan, np.nan

    residu = np.nanmin(residus)
    value  = u[np.nanargmin(residus)]
    return value, residu
