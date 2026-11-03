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
import sys

import matplotlib.pyplot as plt
import numpy as np
from scipy import stats
from scipy.interpolate import CubicSpline
from scipy.stats import linregress

root_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(root_dir)

from ..tools.esf_models import sigmoide, esf_tanh, esf_fermi, esf_gauss_exp_param, esf_erf, esf_loess, esf_to_eq_space_polynomial
from ..tools.oversampling_function import rotate_mat
from .mtf import Mtf
from .mtf_common import (
    Transect,
    cutLSF,
    trimLSF,
    toDisplay_FWHM,
    compute_snr,
    compute_fwhm,
    rescaleforMTF,
    esf_to_eq_space,
    ahamming,
    clean_nuage,
    clean_threshold,
    fLOESS,
    knife_edge1,
    sgolayfilt,
    save_array,
)


class MtfKnifeEdge(Mtf):  # M Sample origin for statistics

    LVarThresh2 = np.float64(3e-2)  # Squared variance threshold for l

    VALID_ESF_MODELS = ('sigmoid', 'esf_tanh', 'esf_fermi', 'esf_gauss_exp_param', 'esf_erf', 'esf_loess', 'esf_to_eq_space_polynomial')
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
        edge_direction=None,
        esf_model='sigmoid',
        sampling=0.2,
        input_angle=None,
        feedback=None,
        debug_dir=None,
        expert_mode=False,
    ):
        """
        Create MTF Target Object
        Compute Rotated Image for MTF radiance image expected
        :param image: Image of MTF Target
        :param label:
        :param sampling: Oversampling Factor [0,1] (default: 0.2)
        :param rotation_angle: rotation angle (dd)
        :param band_number: The geotiff dataset number (multi image GeoTiff).
        :param scale: Conversion to Radiance , Image_DN * scale = Image_Radiance
        :param px_margin: To cut image line / column when searching inflextion point.
        :param input_angle: If provided, skips the angle estimation from refineEdgeSubPx and uses this value instead (degrees).
        :param debug_dir: Directory to save result and debug figures.
        :param expert_mode: If True, also save additional step-by-step debug
            figures to debug_dir (e.g. per-row sigmoid edge fits).
        """

        super().__init__(
            roi,
            image,
            feedback
        )

        self._debug_dir = debug_dir
        self._expert_mode = expert_mode

        if self._debug_dir:
            os.makedirs(self._debug_dir, exist_ok=True)

        if esf_model not in self.VALID_ESF_MODELS:
            raise ValueError(
                f"esf_model must be one of {self.VALID_ESF_MODELS}, got '{esf_model}'"
            )
        self.esf_model = esf_model


        self.sampling = None
        self.band_number = band_number
        self.scale = scale
        self.offset = offset
        self.px_margin = int(px_margin)
        self.im_array = None
        self.resize_in_line = False
        self.resize_in_column = False
        self.edge_direction = edge_direction
        # CT / AL MTF
        if self.edge_direction == "AL":
            self.MTF_direction = "CT"
        if self.edge_direction == "CT":
            self.MTF_direction = "AL"

        self.CT_EDGE = None
        self.AL_EDGE = None

        # Angle: orientation of the target
        self.inflexion_location = None  # infl. location for every record in the image
        self.record_of_inflexion_location = (
            None  # record in the input image (line numer or column number)
        )
        self.lr = (
            None  # linear regression results between infl.location and record number
        )

        self.estimated_angle = None  # Angle computed by refineEdgeSubPx (arctan)
        self.estimated_angle_refined = None  # Angle independently estimated from inflexion points
        self.input_angle = None  # Angle as specified by user (overrides estimated_angle in rotate_mat)

        self.x = None
        self.N = None
        self.nuage = None
        self.nuage_std = None
        self.number_of_lsf = None
        self.bkg = None  # ESF Minimal Radiance Value
        self.x_lsf_input = None  # First derive LSF without CUT for MTF
        self.y_lsf_input = None  # First derive LSF without CUT for MTF
        self.x_esf = None
        self.y_esf = None
        self.x_lsf = None
        self.y_lsf = None
        self.a3 = None  # a3 : x_value of lsf maximumm (inflexion point)
        self.f = None
        self.MTF_NYQ = None
        self.MTF30 = None
        self.MTF50 = None
        self.results = None

        # Metrics:
        self.inflexion_value = None  # index of the LSF maximum
        self.RER = None
        self.fwhm = None
        self.SNR = None  # SNR of filtered interpolated ESF/ LSF.
        self.GRD = None
        self.psf_extent = None
        self.gsd = self._extract_gsd()

        self.Transects = list()

        self.im_array = np.copy(self.image) * self.scale + self.offset
        self.ligne, self.colonnes = self.image.shape
        self.resize_in_line = True

        rows, cols = self.im_array.shape
        x = np.float64(np.arange(0, cols))

        for i in range(0, rows):
            r = self.im_array[i, :]
            t = Transect(
                x,
                r,
                i,
                self.feedback
            )

            # Find subpx edge position
            initGuess = None
            if t.isValid():
                popt, pcov = t.sigmoidFit(initGuess, expert_mode=self._expert_mode, debug_dir=self._debug_dir)

                if popt is False or t.EdgeSubPx is None:
                    t.invalidate()
                    continue

                if pcov[2][2] < self.LVarThresh2:
                    initGuess = t.getInitGuess()
                    self.Transects.append(t)
                else:
                    t.invalidate()

        if len(self.Transects) < 2:
            self.console(
                "Not enough valid transects. Try a bigger polygon or select a different edge. Exiting."
            )
            return None
        self.input_angle = input_angle

        self.refineEdgeSubPx()

        self.get_oversample_image(
        # input_rotation_angle=rotation_angle,
        sampling=sampling,
        edge_direction=edge_direction,
        showGraphic=True,
        )
        window_ovr_image_parameter = None
        self.get_non_eq_space_esf2(window_ovr_image_parameter)
        self.computeEsf()
        self.computeLsf()
        self.doNormalization_and_compute_metrics()
        self.computeMtf()

    @property
    def angle(self):
        return self.input_angle if self.input_angle is not None else self.estimated_angle

    def refineEdgeSubPx(self):
        # Pass 1: fit and remove outliers
        x = np.array([t.Row for t in self.Transects])
        y = np.array([t.EdgeSubPx for t in self.Transects])

        b, a, r, p, stderr = stats.linregress(x, y)
        std = np.std(y - (a + b * x))

        transects = []
        for t in self.Transects:
            if np.abs(a + b * t.Row - t.EdgeSubPx) > 1.75 * std:
                t.invalidate()
            else:
                transects.append(t)
        self.Transects = transects

        if len(self.Transects) < 5:
            raise Exception("Not enough transects")

        # Pass 2: refit on cleaned transects and snap edge positions
        x = np.array([t.Row for t in self.Transects])
        y = np.array([t.EdgeSubPx for t in self.Transects])

        b, a, r, p, stderr = stats.linregress(x, y)

        for t in self.Transects:
            t.EdgeSubPx = a + b * t.Row

        self.estimated_angle = np.arctan(b) * 180 / np.pi

    def get_oversample_image(
        self,
        sampling,
        edge_direction="CT",
        showGraphic=False,
        saveGraphic=True,
    ):
        """

        :param input_rotation_angle:
        :param sampling:
        :param edge_direction: "CT" / "AL"
        :param showGraphic:
        :return:
        """

        self.console("   Clockwise Convention for Angle definition:")
        self.console("   Input Angle is : {:0.2f} °".format(self.estimated_angle))

        if self.im_array is None:
            self.console("NO ARRAY")
            return

        self.sampling = sampling
        self.console(" -- Compute oversample matrix ")
        self.console("    Over Sampling factor  : {}".format(self.sampling))
        self.console("    Rotation Angle        : {}°".format(self.estimated_angle))
        # f.rotate_mat process line by line - CT edge need to be 90° rotated
        #                                    Results are -90° rotated
        self.edge_direction = edge_direction
        if self.edge_direction == "CT":
            # to process cross track Edge
            im_array_rot = np.rot90(self.im_array)
            self.console("Process Cross Track Edge image")
            # Over sample Image - Process Cross Track EDGE Image rotate image :
            # Inflection point coordinates in the image on
            # which the 90° rotation is applied
            #

            CT_EDGE, x, infl_pos, center_pos, im = rotate_mat(
                im_array_rot,
                self.angle,
                oversample=self.sampling,
                margin=self.px_margin,
            )
            # Re oriente CT EDGE
            infl_pos = np.flip(infl_pos)
            center_pos = np.flip(center_pos)
            self.CT_EDGE = np.rot90(CT_EDGE, k=-1)
            # infl_pos = np.flip(infl_pos)

        if self.edge_direction == "AL":
            # Over sample Image - Process Along Track edge image :
            self.console("Process Along Track Edge image2")
            AL_EDGE, x, infl_pos, center_pos, im = rotate_mat(
                self.im_array,
                self.angle,
                oversample=self.sampling,
                margin=self.px_margin,
            )
            self.AL_EDGE = AL_EDGE

        # infl_pos   :  line by line location of inflextion point (in pixel)
        # center_pos :  computed using rotation (-rotation angle) at the center of image

        rms = (1 / infl_pos.shape[0]) * np.power(
            np.sum((infl_pos - center_pos) * (infl_pos - center_pos)), 0.5
        )

        self.console(
            "       RMS Inflection Point : per line estimated vs rotation  {:.3f}".format(
                rms
            )
        )
        # If Across track edge: these are row coordinates
        if self.edge_direction == "CT":
            # One inflection point position per column
            s = (self.im_array).shape[1]
            x = np.linspace(0, s - 1, s)  # Colonne
            y = (infl_pos[:])[:, 0] - 1

            if self.resize_in_column:
                x = np.linspace(0, s - 2, s - 1)  # Colonne
                y = (infl_pos[:-1])[:, 0] - 1
            # regress fractional position (y) from column index (x)
            self.lr = linregress(x, y)
            self.inflexion_location = y  # Inflection point location in px
            self.record_of_inflexion_location = x  # Column index

            angle = 90.0 - np.mod(np.arctan(self.lr.slope) * 180.0 / np.pi, 90.0)

        # If Along track edge: these are column coordinates
        if edge_direction == "AL":

            s = (self.im_array).shape[0]
            x = (infl_pos[:])[:, 0] - 1

            # Column (row index from 0 to s-1)
            y = np.linspace(0, s - 1, s)

            # If a row was added for rotation, we must remove
            # the outlier point
            if self.resize_in_line:
                x = (infl_pos[:-1])[:, 0] - 1
                y = np.linspace(0, s - 2, s - 1)
            # regress fractional position (x) from row index (y)
            self.lr = linregress(y, x)
            self.inflexion_location = x  # Inflection point location in px
            self.record_of_inflexion_location = y  # Row index

            angle = 90.0 - np.mod(np.arctan(self.lr.slope) * 180.0 / np.pi, 90.0)

        # Remove outlier for consistent estimate of rotation angle:

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
            # regress fractional position from row/column index
            self.lr = linregress(y, x)
        else:
            self.inflexion_location_filtered = x
            self.record_of_inflexion_location_filtered = y

            if self.edge_direction == "CT":
                angle = 90.0 - np.mod(np.arctan(self.lr.slope) * 180.0 / np.pi, 90.0)
            if self.edge_direction == "AL":
                angle = 90.0 - np.mod(np.arctan(self.lr.slope) * 180.0 / np.pi, 90.0)

        self.estimated_angle_refined = angle
        self.console(" Rotation angle {:0.2f} °".format(self.angle))
        self.console(" Estimate angle {:0.2f} °".format(self.estimated_angle_refined))
        if self.input_angle is not None:
            # edge angles are equivalent modulo 90°: compare on that circle
            gap = abs((self.input_angle - angle + 45.0) % 90.0 - 45.0)
            if gap > 5.0:
                self.console(
                    " WARNING: the estimated edge angle differs by {:0.1f} ° from the input angle: "
                    "check the input angle, the edge direction and that the ROI contains a clear edge.".format(gap))

    def get_non_eq_space_esf(
        self, window_ovr_image_parameter):
        """
        window_ovr_image_parameter (dic) : ul, px,ln
        ul    : upper left coordinate of bounding box [line,pixel]
        px,ln : number of pixels, lines in the bounding box
        MTF direction : ('AL' / 'AC')

        To compute MTF in Cross Track Direction, Along Track (AT) Edge required
        To compute MTF in Along Track Direction, aCross Track (CT) Edge required

        1- Clip Rotated Image according to Bounding BOX according to ul
        2- Get Non Eq space ESF
        3- Clean Non Eq space ESF

        Return
        """

        if self.MTF_direction == "CT":
            A = self.AL_EDGE
            self.console(" -- MTF AC Track         --")
        if self.MTF_direction == "AL":
            A = self.CT_EDGE
            self.console(" -- MTF ALong Track       --")

        if window_ovr_image_parameter is not None:
            # Clip Rotated Image according to Bounding BOX according to ul:
            ul_i = window_ovr_image_parameter["line"]
            ul_j = window_ovr_image_parameter["pixel"]
            ln = window_ovr_image_parameter["line_number"]
            px = window_ovr_image_parameter["pixel_number"]

            target = A[ul_i : ul_i + ln, ul_j : ul_j + px]
        else:
            target = A
        self.console(f"TARGET : {target}")
        # Get Non Eq space ESF - Clean
        [a, b] = target.shape
        x1 = []
        R = []
        R_std = []
        N = []

        if self.MTF_direction == "CT":
            lsf_width = b
            nb_record = a
            indice = np.arange(0, b, 1)

            for i in indice:

                v = target[:, i]
                v = np.delete(v, v == 0, 0)
                v = v.reshape(-1, 1)
                if len(v) != 0:
                    R = np.hstack((R, np.nanmean(v)))
                    if np.std(v) == 0:
                        R_std = np.hstack((R_std, 1))
                    else:
                        R_std = np.hstack((R_std, np.std(v, ddof=1)))
                    x1 = np.hstack((x1, i))
                    [m, n] = v.shape
                    N = np.hstack((N, m))
        else:
            # MTF A Long
            lsf_width = a
            nb_record = b
            indice = np.arange(0, a, 1)
            for i in indice:
                v = target[i, :]
                v = np.delete(v, v == 0, 0)
                v = v.reshape(-1, 1)
                if len(v) != 0:
                    R = np.hstack((R, np.nanmean(v)))
                    if np.std(v) == 0:
                        R_std = np.hstack((R_std, 1))
                    else:
                        R_std = np.hstack((R_std, np.std(v, ddof=1)))
                    x1 = np.hstack((x1, i))
                    [m, n] = v.shape
                    N = np.hstack((N, m))

        th = 50

        xclean, Rclean, R_std_clean = clean_nuage(x1, R, R_std, N, th)

        # Set values to MTF instance :
        self.x = xclean
        self.N = N
        self.nuage = Rclean
        self.nuage_std = R_std_clean
        self.number_of_lsf = nb_record

        self.console(
            "Number of processed BINs / Total : {} / {}   :".format(
                len(xclean), lsf_width
            )
        )
        self.console(f"Number of Oversample Edge Profile: {nb_record}")


    def get_non_eq_space_esf2(self,
                             window_ovr_image_parameter,
                             showGraphic=False,
                             saveGraphic=False):

        '''
        window_ovr_image_parameter (dic) : ul, px,ln
        ul    : upper left coordinate of bounding box [line,pixel]
        px,ln : number of pixels, lines in the bounding box
        MTF direction : ('AL' / 'AC')

        To compute MTF in Cross Track Direction, Along Track (AT) Edge required
        To compute MTF in Along Track Direction, aCross Track (CT) Edge required

        1- Clip Rotated Image according to Bounding BOX according to ul
        2- Get Non Eq space ESF
        3- Clean Non Eq space ESF

        Return
        '''

        if self.MTF_direction == 'CT':
            A = self.AL_EDGE
            print(" -- MTF AC Track         --")
        if self.MTF_direction == 'AL':
            A = self.CT_EDGE
            print(" -- MTF ALong Track       --")

        if window_ovr_image_parameter is not None:
            # Clip Rotated Image according to Bounding BOX according to ul:
            ul_i = window_ovr_image_parameter['line']
            ul_j = window_ovr_image_parameter['pixel']
            ln = window_ovr_image_parameter['line_number']
            px = window_ovr_image_parameter['pixel_number']

            target = A[ul_i:ul_i + ln, ul_j:ul_j + px]
        else:
            target = A

        # Get Non Eq space ESF - Clean
        [a, b] = target.shape
        x1 = []
        R = []
        R_std = []
        N = []

        # New implementation
        # TODO: Ensure this works for CT / AL case, Left / Right profile

        x_row = (np.arange(0, A.shape[0], 1))
        x_grid = np.vstack(A.shape[1] * [x_row]).T

        y_col = (np.arange(0, A.shape[1], 1))
        y_grid = np.vstack(A.shape[0] * [y_col])

        m = (A > 0)
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
        '''
        <arr_diff_m> Statistics: 
                      - average par bin
                      - std par bin
                      - median par bin
                      - count par bin
        '''
        import scipy.stats as sp
        # bin = np.append(np.unique(y),np.max(y)+1)
        bin_means, bin_edges, binnumber = \
            sp.binned_statistic(u, v,
                                statistic='mean',
                                bins=bin)
        bin_std, bin_edges, binnumber = \
            sp.binned_statistic(u, v,
                                statistic='std',
                                bins=bin)
        bin_count, bin_edges, binnumber = \
            sp.binned_statistic(u, v,
                                statistic='count',
                                bins=bin)
        bin_median, bin_edges, binnumber = \
            sp.binned_statistic(u, v,
                                statistic='median',
                                bins=bin)

        # # TODO: Graphic box plot ? a revoir
        # fig, ax1 = plt.subplots()
        # ax1.plot(bin_edges[:-1], bin_means, '+', color='g', label='esf_bin_value')  # do not touch
        # ax2 = ax1.twinx()
        # #ax2.plot(bin_edges[:-1], bin_count, 'c', label='esf_bin_sample')   # we want a histogram in the background
        # ax2.bar(bin_edges[:-1],
        #         bin_count,
        #         width=np.diff(bin_edges),
        #         alpha=0.2,
        #         color='c',
        #         label='esf_bin_sample',
        #         align='edge')
        # ax2.plot(bin_edges[:-1], bin_std, 'k+', label='esf_bin_std')   # represents the standard deviation between values... a box plot?

        # ax1.set_xlabel('Bin')
        # ax1.set_ylabel('esf value', color='g')
        # ax2.set_ylabel('esf bin sample, std', color='k')

        # plt.legend()

        # plt.show()

        # Rough cleanup of the point cloud
        th = clean_threshold(bin_means)
        self.th = th
        # xclean, Rclean, R_std_clean = clean_nuage(x1, R, R_std, N, th)

        self.x_old = bin_edges[:-1]
        self.R_old = bin_means

        # the cleaned arrays are shorter when bins are removed: new arrays, not bin_edges[:-1]
        x_clean, bin_means, bin_std, masque = clean_nuage(bin_edges[:-1],
                                                          bin_means,
                                                          bin_std,
                                                          bin_count,
                                                          th)
        # New variables obtained with scipy
        self.x = x_clean
        self.N = bin_count
        self.nuage = bin_means
        self.nuage_std = bin_std
        self.nuage_median = np.delete(bin_median, masque == 0, 0)
        self.number_of_lsf = nb_record

        # Set values to MTF instance :
        # self.x = xclean
        # self.N = N
        # self.nuage = Rclean
        # self.nuage_std = R_std_clean
        # self.number_of_lsf = nb_record

        print("Number of processed BINs / Total : {} / {}   :".format(
            len(self.x), lsf_width))
        print("Number of Oversample Edge Profile   :", nb_record)

        ''''
        R_a, x_a, self.nuage, self.x, self.nuage_BIN_STD, self.nuage_BIN_CARD, self.number_of_records = recupR(
                    self.target, self.direction)
        
        '''

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
        """
        Compute PSF extent as the 95% encircled energy width in pixels.
        Uses the non-equidistant ESF (self.x, self.nuage) from get_non_eq_space_esf2.
        Result stored in self.psf_extent.
        """
        x_c = self.x
        y_c = self.nuage
        pas = self.sampling

        cdf = self._get_discrete_cdf(y_c)
        x_p = list(zip(y_c, cdf))
        x_p.sort(key=lambda it: it[0])

        x = [it[0] for it in x_p]
        y = [it[1] for it in x_p]

        if not np.any(np.array(y) < 0.05):
            raise ValueError("Cannot compute the PSF extent: the edge profile is degenerate (no usable edge in the ROI).")
        index = np.max(np.where(np.array(y) < 0.05))
        u = np.where(y_c > x[index])
        min_v = u[0][0]
        max_v = u[0][-1]
        self.psf_extent = (x_c[max_v] - x_c[min_v]) * pas
        return self.psf_extent

    def computeEsf(self):
        """
        :param filt:
        :return:
        """

        x1 = self.x
        R = self.nuage
        passpline = self.sampling

        # A ROI without a usable edge gives a degenerate edge profile (few bins, NaN, flat):
        # fail with a clear message instead of an IndexError / LinAlgError in the ESF fit.
        if len(x1) < 10 or not np.all(np.isfinite(R)) or np.ptp(R) == 0:
            raise ValueError(
                "No usable edge found in the ROI (edge profile has {} points, "
                "estimated angle too far from the input angle?). Draw a larger ROI "
                "centred on a straight, high-contrast edge.".format(len(x1)))

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

    def non_param_esf(self):
        """'
        Warning : Sgolay (sgolyfit): applied on LSF and not ESF

        """

        # Step 1  with linear interpolation
        # Build y1_ech as R with equaly space sampling
        # R Missing values replaced with 0

        # Set Variables :
        x1 = self.x
        R = self.nuage
        ST = {}
        x1_ech, y1_ech, self.esf_n_norm = esf_to_eq_space(x1, R)

        self.x_esf0 = x1_ech
        self.y_esf0 = y1_ech

        # Denoising the equally spaced ESF with fLOESS:
        self.console("[FLOESS]  remove noise")
        noisy = np.vstack(
            (x1_ech.conj().transpose(), y1_ech.conj().transpose())
        ).transpose()

        span = 8 / noisy.shape[0]
        v = fLOESS(noisy, span)
        ST["x1_step3"] = x1_ech
        ST["y1_step3"] = v.conj().transpose()

        self.x_esf = ST["x1_step3"]
        return ST

    def report_line(self):

        report_file = os.path.join(self.out_dir, "report.txt")
        self.console("  Relative Edge reponse : {:.4f}".format(self.RER))
        self.console("  SNR                  : {:.4f}".format(self.SNR))
        self.console("  FWHM                 : {:.4f}".format(self.FWHM))
        self.console("  MTF@Nyquit           : {:.4f}".format(self.MTF_NYQ))
        with open(report_file, "w") as f:
            ch = "  Relative Edge reponse : {:.4f}".format(self.RER)
            f.write(ch + "\n")
            ch = "  SNR                  : {:.4f}".format(self.SNR)
            f.write(ch + "\n")
            ch = "  FWHM                 : {:.4f}".format(self.FWHM)
            f.write(ch + "\n")
            ch = "  MTF@Nyq              : {:.4f}".format(self.MTF_NYQ)
            f.write(ch + "\n")

    def report(self, product_name, site):

        report_file = os.path.join(self.out_dir, "report.txt")
        ch = " ".join(
            [
                product_name,
                site,
                self.MTF_direction,
                self.band_number,
                str(self.sampling),
                str(self.estimated_angle),
                str(self.estimated_angle_refined),
                str(self.RER),
                str(self.SNR),
                str(self.FWHM),
                str(self.MTF_NYQ),
                str(self.MTF50),
                str(self.MTF30),
            ]
        )
        with open(report_file, "w") as f:
            f.write(
                "Product site MTF_direction band_number  sampling input_rotation_angle estimate_rotation_angle"
                " RER SNR FWH MTF@Nyq MTF50 MTF30 \n"
            )
            f.write(ch + "\n")

    def computeLsf(self):
        """
        get lst:
        1. Apply derivative filter to esf
        2. Cut lsf based on FWHM
        3. Normalize lsf
        4. Apply hamming window
        5. Compute SNR

        :param n: LSF Cut to be in interval [-n*FWHM , n*FWHM]
               sgolay_K :
               sgolay_F :

        The    polynomial order, K, must be a integer less than window size, F,
        which must be an odd integer.
        If the polynomial order, K, equals F-1, no smoothing will
          occur. Each of the K+1 columns of G is a differentiation filter for
        derivatives of order P-1 where P is the column index.
         Used in G = sgolayfilt(K,F)

        :return:


        """
        sgolay_K=4
        sgolay_F=11
        DO_SGOLAY=True
        n_FWHM=2

        self.sgolay_K = sgolay_K
        self.sgolay_F = sgolay_F
        # init Variabiles :
        passpline = self.sampling
        # Compute  FWHM of LSF
        # Compute Line Spread Function from Edge Spread Function.
        # Computes first derivative via FIR (1xn) filter.
        # Edge effects are suppressed and vector size is preserved.
        esf = self.y_esf
        H = [0.5, 0, -0.5]
        lsf_conv = np.convolve(esf, H, "valid")
        v1 = lsf_conv[0]
        v2 = lsf_conv[lsf_conv.shape[0] - 1]
        lsf = np.append(np.insert(lsf_conv, 0, v1), v2)

        #     --- Step 5 : Cut LSF for symetry / remove queue     #
        u = self.x_esf
        lsfx = u[0 : lsf.shape[0]]
        lsfy = lsf
        self.x_lsf_native = lsfx
        self.y_lsf_native = lsf

        # Clip/Cut LSF on both side and ensure symetry
        #     delta=n*fwhm

        # FWHM express in pixels
        FWHM = compute_fwhm(lsfy, passpline)
        self.console(" Initial FWHM : {:0.2f} (pixels)".format(FWHM))

        # Keep original LSF for visual inspection
        self.x_lsf_input = lsfx
        self.y_lsf_input = lsfy

        # # Define number of BIN to cut lsf (for MTF)
        # BIN_NUMBER = n_FWHM * FWHM / self.sampling
        # self.console(
        #     " Number of BINS on each side to get LSF \n : n x FWHM x 1/sampling : {} x {:0.2f} x {:0.2f} = {:0.1f} BINS ".format(
        #         n_FWHM, FWHM, 1 / self.sampling, BIN_NUMBER
        #     )
        # )

        # lsfxCut, lsfyCut = cutLSF(lsfx, lsfy, BIN_NUMBER)

        w = self.get_psf_extent()
        print('PSF Extent Radius {} pixels'.format(str(w)))

        L_w = 2 * w

        # Check L_w >> len(m.x_esf*pas)
        # Trim LSF:
        lsfxCut, lsfyCut = trimLSF(self.x_lsf_input, self.y_lsf_input, L_w, self.sampling)


        self.console(
            " if cutLSF is failed, check symetry of lsf (inflexion point middle of esf)"
        )
        self.console(
            " Length of input LSF  (pixels) : {} ".format(lsfx.shape[0] * self.sampling)
        )
        self.console(
            " Length of output LSF (pixels) : {} ".format(
                lsfxCut.shape[0] * self.sampling
            )
        )

        # rescaleforMTF : Adjust size of LSF
        self.x_lsf, self.y_lsf = rescaleforMTF(lsfxCut, lsfyCut, passpline)

        if DO_SGOLAY:
            # Unoise with SGOLAY Filtering:
            y = self.y_lsf
            x1_ech = self.x_lsf
            G = sgolayfilt(sgolay_K, sgolay_F)
            npad = G[:, 0].transpose().shape[0] - 1
            u_padded = np.pad(y, (npad // 2, npad - npad // 2), mode="constant")
            yG0 = np.convolve(u_padded, G[:, 0].T, "valid")

            u = np.floor(sgolay_F / 2) + 1
            self.x_lsf = x1_ech[int(u) : yG0.shape[0] - int(u)]
            self.y_lsf = yG0[int(u) : yG0.shape[0] - int(u)]

        self.x_lsf_before_normalization = self.x_lsf
        self.y_lsf_before_normalization = self.y_lsf

        # Align Cut LSF with ESF
        st = np.where(self.x_esf == self.x_lsf[0])
        end = np.where(self.x_esf == self.x_lsf[-1:])
        if st[0].size == 0 or end[0].size == 0:
            raise ValueError("Cannot align the LSF with the ESF: no usable edge in the ROI.")
        self.x_esf = self.x_esf[st[0][0] : end[0][0] + 1]
        self.y_esf = self.y_esf[st[0][0] : end[0][0] + 1]

        return np.array([self.x_esf, self.y_esf])

    def doNormalization_and_compute_metrics(self, n_snr=3, saveFIG=True):
        """
        :param n_snr: n_snr * FWHM a part from inflextion point to compute SNR
                      from two vector (dark , bright) take min lengthone
        :param saveFIG:
        :return:
        """

        # Normalization of LSF :
        m = np.argmax(np.abs(self.y_lsf))
        extrema = self.y_lsf[m]
        if extrema < 0:
            self.y_lsf = -self.y_lsf / (-extrema)

        if extrema > 0:
            self.y_lsf = (self.y_lsf) / np.max((self.y_lsf))

        self.inflexion_value = m
        a3 = int(self.x_lsf[self.inflexion_value])
        # x_lsf value at the inflection point
        self.a3 = a3
        # Final FWHM :
        self.FWHM = compute_fwhm(self.y_lsf, self.sampling)
        self.fwhm = toDisplay_FWHM(self.y_lsf)

        # Final RER :
        self.computeRER()

        self.computeHEE()
        # Final SNR
        # Use ESF To Compute SNR on not Equaly spaced ESF
        #    self.x (xclean), self.nuage(Rclean)
        # Selection of SNR plateaus at 2 px away from the inflection point
        # defined with start /   with 2 pixels a part from
        #  the inflextion point (a3)
        # step * FWHM on each side

        # Location of Inflextion point in the model
        # Compute ESF inflexion point
        y = self.y_esf
        g = np.abs(y[1:] - y[:-1])
        i = np.argmax(g)
        a3 = self.x_esf[i]
        # As non eq space value can be missing, look for into window
        m = [self.x > a3 - 2][0] & [self.x < a3 + 2][0]
        # Indice in the orginal ESF
        if not np.any(m):
            raise ValueError("Cannot locate the edge inflection point: no usable edge in the ROI.")
        infl = self.x[m][0]
        indice = (np.where(self.x == infl))[0][0]

        # Saple number appart from inflextion point
        d = int(np.floor((n_snr) * self.FWHM / self.sampling))
        interval1 = infl - d
        interval2 = infl + d

        v1 = self.nuage[self.x < interval1]
        v2 = self.nuage[self.x > interval2]
        v10 = self.x[self.x < interval1]
        v20 = self.x[self.x > interval2]

        bright_radiance = v2
        background_dc = v1
        bright_radiance_x = v20
        background_dc_x = v10

        if self.nuage[0] > self.nuage[-1:]:
            bright_radiance = v1
            background_dc = v2
            bright_radiance_x = v10
            background_dc_x = v20

        self.console(" ")
        self.console("  - Compute SNR on filtered ESF - ")
        self.console(
            "    - ESF Inflexion point (a3) at x value / pixel = {:2d} / {:0.2f} pixel".format(
                int(a3), int(a3) * self.sampling
            )
        )
        self.console("    - Full Width at Half Maximum = {:0.2f} pixels".format(self.FWHM))
        self.console(
            "    - Compute SNR at {:0.2f} pixels from inflexion point".format(
                d * self.sampling
            )
        )
        self.console("    - Bright Level vector size :  {:0.2f} ".format(v2.shape[0]))
        self.console("    - Dark Level vector size   :  {:0.2f} ".format(v1.shape[0]))

        self.SNR, self.mean_H, self.mean_B = compute_snr(v1, v2)

    def hamming(self):
        # Hamming windows :
        n_l = self.y_lsf.shape[0]
        win = ahamming(n_l, (n_l + 1) / 2)  # centered Hamming window
        yf = self.y_lsf
        self.y_lsf = yf * win.conj().transpose()

    def computeMtf(self):
        self.console("-- Compute MTF --")
        # init Variables :
        passpline = self.sampling
        l = self.y_lsf.shape[0]
        # compute the MTF for a non-parametric ESF:
        self.f, self._mtf, self.MTF_NYQ = knife_edge1(
            self.x_lsf, self.y_lsf, self.sampling
        )
        f1 = 1 / (l * self.sampling)
        L_w = passpline * (self.y_lsf.shape[0])
        # Sortie Console
        self.console(
            "Frequency step (1 / (L_w * sampling)): \n 1/{:0.2f}*{:0.2f} = {:2f} ".format(
                L_w, self.sampling, f1
            )
        )

        for rec, i in enumerate(self.mtf):
            if i < 0.3:
                break
        a = (self.f[rec] - self.f[rec - 1]) / (self.mtf[rec] - self.mtf[rec - 1])
        b = self.f[rec] - self.mtf[rec] * a
        self.MTF30 = a * 0.3 + b

        for rec, i in enumerate(self.mtf):
            if i < 0.5:
                break
        a = (self.f[rec] - self.f[rec - 1]) / (self.mtf[rec] - self.mtf[rec - 1])
        b = self.f[rec] - self.mtf[rec] * a
        self.MTF50 = a * 0.5 + b

        for rec, i in enumerate(self.f):
            if i > 0.5:
                break
        a = (self.mtf[rec] - self.mtf[rec - 1]) / (self.f[rec] - self.f[rec - 1])
        b = self.mtf[rec] - self.f[rec] * a
        MTF_NYQ = a * 0.5 + b

        return self.mtf

    def write_results(self):
        path_results = self.path_results
        savename = self.direction + "stat_results"
        log_file_name = path_results + "/" + savename + ".txt"
        self.console(f"Save into log file: {log_file_name}")
        fid = open(log_file_name, "w")
        fid.write("Number of records")
        fid.write("\t" + str(self.number_of_records))
        fid.write("\nNumber of bins")
        fid.write("\t" + str(self.x.shape[0]))
        fid.write("\nSNR")
        fid.write("\t" + str(self.SNR))
        fid.write("\nRER")
        fid.write("\t" + str(self.RER))
        fid.write("\nfwhm")
        fid.write("\t" + str(self.FWHM))
        fid.write("\nInflection point position")
        fid.write("\t" + str(self.inflexion_xvalue))
        fid.write("\nTarget sampling step")
        fid.write("\t" + str(self.sampling))
        fid.write("\nMTF30")
        fid.write("\t" + str(self.MTF30))
        fid.write("\nMTF50")
        fid.write("\t" + str(self.MTF50))
        fid.write("\nMTF @ Nyquist (interpolated)")
        fid.write("\t" + str(self.MTF_NYQ))

        fid.close()

    def computeRER(self, showGraphic=False,
                   saveFig=True):
        # Compute Relative Edge Response
        # Methode : https://ntrs.nasa.gov/api/citations/20070038233/downloads/20070038233.pdf
        # RER estimate effective slope of the imaging system's edge response, since the
        # distance between the points for which the differences are calculated eis always
        # equal to GSD.
        # One GSD Match with  1/passpline values
        # Perform on Normalize edge Response
        xo = self.x_esf
        yo = self.y_esf

        # Compute ESF inflexion point
        y = self.y_esf
        g = np.abs(y[1:] - y[:-1])
        i = np.argmax(g)
        a3 = xo[i]
        a3_b = np.where(self.x_esf == self.a3)
        ox_esf = self.x_esf[np.take(a3_b, 0)]

        # Normalize ESF:
        yo_n = yo / np.max(yo)
        print('- Compute RER on normalize Edge Spread Function')

        # Take 1/2 GSD on each side of the inflection point:
        h_w = int((1 / self.sampling) / 2)

        # X value for seleced area
        # Check if Eq sampling of ESF (in case ...)
        v = xo[i - h_w:i + h_w + 1]
        # msk of missing position in x0,  (v[1:] - v[:-1] <=> (n+1 - n)
        msk = [np.abs(v[1:] - v[:-1]) == 1]
        valid_px = np.sum(msk)
        if valid_px != len(v) - 1:
            print(' [Error]  Not Equally spaced  ESF')
        # Extract corresponding yo1 portion
        Yo1 = yo_n[i - h_w:i + h_w + 1]

        # RER is computed in both directions  RER = power((RERx)(RERy), 0.5)

        self.RER = np.abs(Yo1[0] - Yo1[-1:])[0]

        # Coordinates of the 2 points used for the RER
        x1 = (xo[i - h_w] - a3) * self.sampling
        x2 = (xo[i + h_w] - a3) * self.sampling

        #y1 = yo_n[i - h_w]
        #y2 = yo_n[i + h_w]

        y1 = np.interp(x1 / self.sampling + a3, xo, yo_n)  # convert x1 back to xo coordinate
        y2 = np.interp(x2 / self.sampling + a3, xo, yo_n)

        # Stockage
        self.RER_points = {
            "x": np.array([x1, x2]),
            "y": np.array([y1, y2])
        }

    def computeHEE(self, showGraphic=False,
                   saveFig=True):
        # Compute Half Edge Extent
        # Methode : https://www.i2rcorp.com/about-us/resources/imagery-guideline
        # horizontal distance (meters or feet) between 5% and 50% edge response points (lower),
        # or the 50% and 95% edge response points (upper)
        # = contrast transition distance on either side of an edge
        # comes from an asymmetry in the system PSF

        xo = self.x_esf
        yo = self.y_esf

        sc = self.sampling
        # centring on zero value :
        a3_b = np.where(self.x_esf == self.a3)
        ox_esf = self.x_esf[np.take(a3_b, 0)]

        # Compute ESF inflexion point
        y = self.y_esf
        g = np.abs(y[1:] - y[:-1])
        i = np.argmax(g)
        a3 = xo[i]


        # Normalize ESF:
        yo_n = yo / np.max(yo)
        print('- Compute HEE on normalize Edge Spread Function')

        # --- Determine the direction of the ESF
        if yo_n[-1] > yo_n[0]:
            # Increasing ESF: 0 -> 1
            comparator = lambda arr, val: arr >= val
        else:
            # Decreasing ESF: 1 -> 0
            comparator = lambda arr, val: arr <= val

        # --- Seuils
        thresholds = [0.05, 0.50, 0.95]
        indices = []
        for t in thresholds:
            mask = comparator(yo_n, t)
            if not np.any(mask):
                raise ValueError(f"No value reaches threshold {t}")
            idx = np.argmax(mask)  # first point satisfying the condition
            indices.append(idx)

        idx05, idx50, idx95 = indices


        x05 = xo[idx05]

        #x50 = xo[idx50]
        x50 = xo[i]

        x95 = xo[idx95]


        x05_c = sc * (x05 - a3)
        x50_c = sc * (x50 - a3)
        x95_c = sc * (x95 - a3)

        # Distances
        HEE_lower = x50 - x05
        HEE_upper = x95 - x50
        HEE = 0.5 * (HEE_lower + HEE_upper)

        self.yo_n = yo_n
        self.x05 = x05_c
        self.x50 = x50_c
        self.x95 = x95_c
        self.HEE = np.abs(HEE)
        self.HEE_lower = np.abs(HEE_lower)
        self.HEE_upper = np.abs(HEE_upper)

        plt.close()

    def _extract_gsd(self):
        """Extract GSD (in CRS units) from the raster geotransform, or None if not georeferenced."""
        try:
            gt = self.raster.GetGeoTransform()
            gsd = abs(gt[1])
            return gsd if gsd > 0 else None
        except Exception:
            return None

    def figure(self, gsd=None):
        if gsd is None:
            gsd = self.gsd
        sc = self.sampling
        i = 1  ## the index of matlab starts 1 but python starts 0
        # centring on zero value :
        a3_b = np.where(self.x_esf == self.a3)
        ox_esf = self.x_esf[np.take(a3_b, 0)]

        ox_lsf = self.a3

        length = len(sc * self.x_esf)


        #x_plot = (self.RER_points["x"] - ox_lsf) * self.sampling
        x_plot = self.RER_points["x"]
        y_plot = self.RER_points["y"]

        rer_pt1 = f"({x_plot[0]:.2f}, {y_plot[0]:.3f})"
        rer_pt2 = f"({x_plot[1]:.2f}, {y_plot[1]:.3f})"

        # Compute ESF inflexion point
        xo = self.x_esf
        y = self.y_esf
        g = np.abs(y[1:] - y[:-1])
        i = np.argmax(g)
        a3 = xo[i]



        # build the Heaviside function

        '''u = np.zeros([self.x_esf.shape[0], self.x_esf.shape[0]])
        if self.x_esf[0] > a3:
            u[0:np.take(a3_b, 0) + i, 0] = 1    # gauche = 1
        else:
            u[np.take(a3_b, 0):u.shape[0] + i, 0] = 1  # droite = 1'''

        y = self.y_esf
        n = len(y)

        u = np.zeros(n)

        # Transition position (e.g., inflection point)
        idx_trans = int(np.take(a3_b, 0))

        # Direction of variation
        if y[-1] > y[0]:
            # Increasing ESF (B -> W): 0 -> 1
            u[idx_trans:] = 1
        else:
            # Decreasing ESF (W -> B): 1 -> 0
            u[:idx_trans] = 1



        if self.MTF_direction == 'AL':
            name = 'Along Track'
        else:
            name = 'Across Track'

        self._figure = plt.figure(figsize=(25, 15), dpi=100)

        plt.suptitle(
            f"{name} MTF Results",
            fontsize=28,
            fontweight='bold'
        )

        plt.subplot(2, 3, 1)
        y_normalize = self.y_esf / np.max(self.y_esf)
        #            plot.plot(sc*(self.x - ox_esf) , self.nuage,'o')
        plt.plot(sc * (self.x_esf - ox_esf),
                 y_normalize,
                 '+-', label="ESF")
        plt.plot(sc * (self.x_esf[np.take(a3_b, 0)] - ox_esf),
                 self.y_esf[np.take(a3_b, 0)] / np.max(self.y_esf), '+', label="Inflexion nt")
        plt.plot(sc * (self.x_lsf - ox_lsf), self.y_lsf / np.max(self.y_lsf), '-', label="LSF")
        plt.plot(sc * (self.x_lsf - ox_esf), np.array(self.fwhm) / np.max(self.y_lsf), '-', label="FWHM")
        #plt.plot(sc * (self.x_esf - ox_esf), u[:, 0], '-', label="Heaviside")
        plt.plot(sc * (self.x_esf - ox_esf), u, '-', label="Heaviside")

        plt.rcParams['axes.labelsize'] = 12
        plt.rcParams['axes.labelweight'] = 'bold'

        if self.RER > 0:
            loc_word = 'northwest'
        else:
            loc_word = 'northeast'

        #           legend(' ESF ', ' Inflexion nt', 'LSF','FWHM','Heaviside','location',loc_word)

        plt.title(' ESF / LSF / MTF ',
                  fontname="Times New Roman", fontweight="bold", fontsize=20)
        plt.xlabel(' Pixels ')
        plt.ylabel(' Intensity (minus bkg)')
        plt.legend()
        plt.grid()

        # Data
        ax_text = plt.subplot(2, 3, 2)
        ax_text.axis('off')

        # on the left and smaller
        if gsd is not None:
            self.GRD = gsd * self.FWHM

        self.results = {
            "method":       self.esf_func.__name__,
            "sampling":     self.sampling,
            "lines":        self.ligne,
            "columns":      self.colonnes,
            "esf_length":   length,
            "MTF_NYQ":      self.MTF_NYQ,
            "MTF30":        self.MTF30,
            "MTF50":        self.MTF50,
            "SNR":          self.SNR,
            "RER":          self.RER,
            "RER_pt1":      rer_pt1,
            "RER_pt2":      rer_pt2,
            "HEE_upper":    self.HEE_upper,
            "HEE_lower":    self.HEE_lower,
            "FWHM":         self.FWHM,
            "R2":           self.R2,
            "GRD":          self.GRD,
        }

        text_str = (
            #f"Resolution : TBC \n"
            f"Method : {self.esf_func.__name__ }\n"
            f"Sampling: {self.sampling:.2f}\n"
            f"Number of lines : {self.ligne}\n"
            f"Number of columns : {self.colonnes}\n"
            f"Rotation angle : {self.angle:.2f}\n" 
            f"Length of the ESF : {length}\n"
            f"MTF @ Nyquist : {self.MTF_NYQ:.2f}\n"
            f"MTF 30 : {self.MTF30:.2f}\n"
            f"MTF 50 : {self.MTF50:.2f}\n"
            f"SNR : {self.SNR:.2f}\n"
            f"RER : {self.RER:.2f}\n"
            f"RER points : P1 {rer_pt1} and P2 {rer_pt2}\n"
            f"HEE upper : {self.HEE_upper:.2f}\n"
            f"HEE lower : {self.HEE_lower:.2f}\n"
            f"FWHM : {self.FWHM:.2f} px\n"
            f"R2 : {self.R2:.2f}\n"
            + (f"GRD : {self.GRD:.2f} m" if self.GRD is not None else "")
        )

        ax_text.text(
            0.02, 0.98,
            text_str,
            transform=ax_text.transAxes,
            ha='left',
            va='top',
            fontsize=12,
            fontfamily='monospace'
        )

        # MTF
        plt.subplot(2, 3, 3)

        #plt.plot(self.f, self.mtf, color="k", marker='o')

        plt.plot(self.f, self.mtf, color="k", ls='-')

        plt.axhline(0.3, color="b", ls=':', linewidth=2, label="MTF30 = {:.2f}".format(self.MTF30))
        plt.axvline(self.MTF30, color="b", ls=':', linewidth=2)

        plt.axhline(y=0.5, color="g", ls=':', linewidth=2, label="MTF50 = {:.2f}".format(self.MTF50))
        plt.axvline(self.MTF50, color="g", ls=':', linewidth=2)

        plt.axhline(self.MTF_NYQ, color="red", ls='--', linewidth=2.5, label="MTF at Nyquist")
        plt.axvline(x=0.5, color="red", ls='--', linewidth=2.5)

        plt.grid(linewidth=0.5)
        self.direction_code = 'XXXX'
        plt.title(' MTF ', fontname="Times New Roman", fontweight="bold", fontsize=20)
        plt.xlabel(' Freq ', fontname="Times New Roman", fontweight="bold", fontsize=20)
        plt.ylabel(' Normalized Module ', fontname="Times New Roman", fontweight="bold", fontsize=20)


        plt.legend(fontsize=10)


        # FIT ESF
        plt.subplot(2, 3, 4)
        plt.plot(sc * (self.x - ox_esf), self.nuage, 'o', label="orginal esf")
        plt.plot(sc * (self.x_esf0 - ox_esf), self.y_esf0, '+', label="interpolated esf")

        #            title(['esf, 2nd order poly interpolation, win size , 2 ou 1 - '  self.direction ' direction (' self.direction_code ' )'])
        plt.title(' ESF ', fontname="Times New Roman",
                  fontweight="bold", fontsize=20)
        #            legend('orginal esf', '2nd order esf','location','east')

        plt.xlabel(' Pixels ')
        plt.legend()
        plt.grid()

        # HEE ESF
        plt.subplot(2, 3, 5)
        plt.plot(sc * (self.x_esf - a3),
                 self.yo_n, label='Normalized ESF')
        plt.axvline(self.x05, color="b", ls='--', label="5%")
        plt.axvline(self.x50, color="r", ls='--', linewidth=2, label="50%")
        plt.axvline(self.x95, color="y", ls='--', label="95%")
        plt.axvspan(self.x05, self.x50, alpha=0.2, color='blue')
        plt.axvspan(self.x50, self.x95, alpha=0.2, color='yellow')
        plt.scatter(x_plot, y_plot, zorder=5, label='RER points')
        plt.xlabel(' Pixel ')
        plt.ylabel(' Normalized Edge values ')
        plt.legend(loc='center left', bbox_to_anchor=(0.7, 0.5))
        plt.grid()
        plt.title(f'Half Edge Extent values\nHEE lower = {self.HEE_lower:.2f} subpx | HEE upper = {self.HEE_upper:.2f} subpx',
                  fontname="Times New Roman", fontweight="bold", fontsize=20)
        plt.tight_layout(rect=[0, 0, 1, 0.95])

        # LSF
        plt.subplot(2, 3, 6)
        # imagesc(self.target)
        #m = self.x_lsf[int(np.floor(self.x_lsf.shape[0] / 2)) - i]
        plt.plot(sc * (self.x_lsf - ox_lsf), self.y_lsf / np.max(self.y_lsf), '--', label="lsf")
        plt.plot(sc * (self.x_lsf - ox_lsf), self.y_lsf / np.max(self.y_lsf), 'o')

        plt.title(' LSF ',
                  fontname="Times New Roman", fontweight="bold", fontsize=20)
        plt.xlabel(' Pixels ')
        plt.grid()
        return [self._figure, self.panel2()]

    def panel2(self):
        # Parameters plot 1 and 5
        g_xlabel = 'sub pixel location (px)'
        if self.edge_direction == 'AL':
            g_ylabel = 'line number'
        else:
            g_ylabel = 'column number'

        x = self.record_of_inflexion_location
        y = self.inflexion_location

        lr = self.lr
        # uncertainty on the angle estimate
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

        plt.suptitle(
            f"{name} MTF Results 2",
            fontsize=28,
            fontweight='bold'
        )

        # VIEW OF THE TARGET

        plt.subplot(2, 3, 1)
        plt.imshow(self.im_array, cmap='gray', aspect='auto')
        if self.edge_direction == 'AL':
            plt.plot(y, x, 'r+', label='edge subpixel location')  # X-1 to be in image coordinate
        if self.edge_direction == 'CT':
            plt.plot(x, y, 'r+', label='edge subpixel location')  # X-1 to be in image coordinate

        plt.title('Inflexion point location')
        plt.xlabel('Records')
        ch = 'linear interpolation,angle : {:.2f} °'.format(self.estimated_angle)
        plt.legend()

        # VIEW OF THE NUAGE

        plt.subplot(2, 3, 2)
        if self.MTF_direction == 'CT':
            img = self.AL_EDGE
            print(' Show AL Edge')
        else:
            img = self.CT_EDGE
            print(' Show CT Edge')

        plt.imshow(img, cmap='gray', aspect='auto')

        h, w = img.shape

        zoom = 80  # size of the displayed area

        cx = w // 2
        cy = h // 2

        plt.xlim(cx - zoom, cx + zoom)
        plt.ylim(cy + zoom, cy - zoom)

        plt.title(' Over Sample / Projected Edge Target, sampling : {} '.format(self.sampling))
        plt.colorbar()

        # REFINE EDGE

        plt.subplot(2, 3, 3)

        # Refine edge graph

        plt.plot(x, y, '+', label='inflexion point location')
        plt.plot(x, x * lr.slope + lr.intercept, '-', label='Interpolation')
        plt.title(' Orientation angle estimate / input : {:.2f} / {:.2f}  degrees \n Uncertainty : {:.2f} degrees'
                  .format(self.estimated_angle, self.estimated_angle_refined, angle_err))
        plt.xlabel(g_xlabel)
        plt.ylabel(g_ylabel)
        plt.legend()
        plt.grid()

        # STATISTICS

        plt.subplot(2, 3, 4)

        ax1 = plt.gca()

        bin_edges = self.x
        bin_means = self.nuage
        bin_count = self.N
        bin_std = self.nuage_std

        # --- 1. ±1σ envelope (widest zone, high transparency) ---
        ax1.fill_between(
            bin_edges,
            bin_means - bin_std,
            bin_means + bin_std,
            color='k',
            linewidth=0,
            zorder=2,
            label=r'$\pm 1\sigma$'
        )

        ax1.plot(bin_edges, bin_means, '.', color='g', markersize=2, label='esf_bin_value')

        ax2 = ax1.twinx()
        # ax2.plot(bin_edges[:-1], bin_count, 'c', label='esf_bin_sample')   # we want a histogram in the background
        ax2.bar(bin_edges[:-1],
                bin_count[:-1],
                width=np.diff(bin_edges),
                alpha=0.2,
                color='c',
                label='esf_bin_sample',
                align='edge')

        ax1.set_xlabel('Bin')
        ax1.set_ylabel('esf value', color='g')
        ax1.tick_params(axis='y', labelcolor='g')

        ax2.set_ylabel('esf bin sample', color='c')
        ax2.tick_params(axis='y', labelcolor='c')

        # Combined legend for both axes
        lines1, labels1 = ax1.get_legend_handles_labels()
        lines2, labels2 = ax2.get_legend_handles_labels()
        ax1.legend(lines1 + lines2, labels1 + labels2, loc='upper right', fontsize=9)

        plt.grid(True, alpha=0.15, axis='both')

        plt.title('Input ESF')

        # ESF CLEAN

        # Normalize self.nuage from 0 to 1
        nuage_min = np.min(self.nuage)
        nuage_max = np.max(self.nuage)
        nuage_norm = (self.nuage - nuage_min) / (nuage_max - nuage_min)

        plt.subplot(2, 3, 5)

        # Detect transition direction (based on extremities)
        if nuage_norm[0] > nuage_norm[-1]:
            # Descending ESF: starts high → ends low
            high_level = 1.0
            low_level = 0.0
            transition_up = False
        else:
            # Ascending ESF: starts low → ends high
            low_level = 0.0
            high_level = 1.0
            transition_up = True

        # Transition position
        idx_trans5 = np.argmin(np.abs(self.x - self.a3))
        ox5_esf = self.x[idx_trans5]

        # Normalized Heaviside
        if transition_up:
            # Rising: low → high
            u5 = np.where(self.x >= ox5_esf, high_level, low_level)
        else:
            # Falling: high → low
            u5 = np.where(self.x >= ox5_esf, low_level, high_level)

        plt.scatter( (self.x - ox_esf) , nuage_norm, c='green', s=10, alpha=0.7, label='Cleaned ESF points')
        plt.plot((self.x - ox_esf) , u5, color='orange', label="Heaviside")
        #plt.errorbar(self.x, self.nuage, yerr=self.nuage_std, fmt='none', alpha=0.3, color='green')
        plt.xlabel('Position (bins)')
        plt.ylabel('Mean Radiance')
        plt.title(f'Normalized Input ESF ( Cleaned Non-Eq Space ESF )\n{len(self.x)} bins (threshold N>{self.th})')
        plt.legend()
        plt.annotate(
            f'Total possible bins: {self.tot_bin}\n'
            f'Processed / kept bins: {len(self.x)}',
            xy=(0.98, 0.5), xycoords='axes fraction',
            ha='right', va='center', fontsize=9,
            bbox=dict(facecolor='white', alpha=0.7, edgecolor='none')
        )
        plt.grid(True, alpha=0.3)

        # ESF INTERPOLATED

        plt.subplot(2, 3, 6)

        y6 = self.y_esf

        # Normalize to [0,1]
        y6_min = np.min(y6)
        y6_max = np.max(y6)
        y6_norm = (y6 - y6_min) / (y6_max - y6_min)

        # Detect direction
        if y6_norm[0] > y6_norm[-1]:
            # Descending: starts high → ends low
            high_level = 1.0
            low_level = 0.0
            transition_up = False
        else:
            # Ascending: starts low → ends high
            low_level = 0.0
            high_level = 1.0
            transition_up = True

        # Transition position
        idx_trans6 = int(np.take(a3_b, 0))  # or self.a3 if equivalent

        # Normalized Heaviside
        if transition_up:
            u6 = np.where(np.arange(len(y6_norm)) >= idx_trans6, high_level, low_level)
        else:
            u6 = np.where(np.arange(len(y6_norm)) >= idx_trans6, low_level, high_level)

        # Display
        plt.scatter((self.x_esf - self.a3), y6_norm, c='red', s=10, alpha=0.7,
                    label='Normalized Interpolated ESF points')
        plt.plot((self.x_esf - ox_esf), u6, color='orange', label="Heaviside")
        plt.xlabel('Position (bins)')
        plt.ylabel('Mean Radiance')
        plt.title(f'Normalized Interpolated ESF ( Cut )')
        plt.legend()
        plt.grid(True, alpha=0.3)

        return self._figure2
