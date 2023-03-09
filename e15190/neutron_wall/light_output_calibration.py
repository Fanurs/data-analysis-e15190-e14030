import inspect
import json
import os
from pathlib import Path
from typing import Dict, Literal
import warnings
warnings.warn('This submodule is deprecated. Please refer to `e15190.neutron_wall.adc_preprocessing` instead.', DeprecationWarning)

import duckdb as dk
import matplotlib as mpl
import matplotlib.pyplot as plt
import numdifftools as nd
import numpy as np
import pandas as pd
import ruptures as rpt
from sklearn.linear_model import RANSACRegressor
from sklearn.decomposition import PCA
from sklearn.gaussian_process import GaussianProcessRegressor
from sklearn.gaussian_process.kernels import RBF, WhiteKernel
from scipy import interpolate

from e15190.runlog.query import Query
from e15190.neutron_wall.position_calibration import NWCalibrationReader
from e15190.neutron_wall.cache import RunCache
from e15190.utilities import fast_histogram as fh, misc, peak_finder, slicer, tables

class LightOutputCalibrator:
    DATABASE_DIR = '$DATABASE_DIR/neutron_wall/light_output_calibration'

    def __init__(self, AB: Literal['A', 'B']):
        self.AB = AB.upper()
        self.ab = self.AB.lower()
        self.df = None
        self.light_info = dict()

    @staticmethod
    def _get_daniele_root_files_dir(json_path=None) -> Path:
        if json_path is None:
            json_path = os.path.expandvars('$DATABASE_DIR/local_paths.json')
        with open(json_path) as file:
            return Path(json.load(file)['daniele_root_files_dir'])

    def read(self, runs, from_cache=True, **kwargs) -> pd.DataFrame:
        """Update ``self.df``"""
        rc = RunCache(
            src_path_fmt=str(self._get_daniele_root_files_dir() / r'CalibratedData_{run:04d}.root'),
            cache_path_fmt=str(Path(os.path.expandvars(self.DATABASE_DIR)) / r'cache/run-{run:04d}.db'),
        )

        kw = dict(
            from_cache=from_cache,
            sql_cmd='WHERE ' + ' AND '.join([
                'VW_multi == 0',
                f'NW{self.AB}_time_L - NW{self.AB}_time_R < 25',
                f'NW{self.AB}_time_L - NW{self.AB}_time_R > -25',
            ]),
            drop_columns=['VW_multi'],
            insert_run_index=True,
        )
        kw.update(kwargs)

        self.runs = [runs] if isinstance(runs, int) else runs
        self.df = rc.read(
            self.runs,
            {
                'VetoWall.fmulti'                           : 'VW_multi',
                f'NW{self.AB}.fnumbar'                      : f'NW{self.AB}_bar',
                f'NW{self.AB}.fLeft'                        : f'NW{self.AB}_total_L',
                f'NW{self.AB}.fRight'                       : f'NW{self.AB}_total_R',
                f'NW{self.AB}.ffastLeft'                    : f'NW{self.AB}_fast_L',
                f'NW{self.AB}.ffastRight'                   : f'NW{self.AB}_fast_R',
                f'NW{self.AB}.fTimeLeft'                    : f'NW{self.AB}_time_L',
                f'NW{self.AB}.fTimeRight'                   : f'NW{self.AB}_time_R',
            },
            **kw,
        )

        self.df = self.convert_64_to_32(self.df)
        self.df[f'NW{self.AB}_bar'] = self.df[f'NW{self.AB}_bar'].astype(np.int16)
        return self.df
    
    @staticmethod
    def convert_64_to_32(df):
        for col in df.columns:
            if df[col].dtype == np.float64:
                df[col] = df[col].astype(np.float32)
            elif df[col].dtype == np.int64:
                df[col] = df[col].astype(np.int32)
        return df
    
    def add_position(self, df=None, drop_time=True) -> pd.DataFrame:
        update_self = (df is None)
        if update_self:
            df = self.df

        bar = f'NW{self.AB}_bar'
        time_L = f'NW{self.AB}_time_L'
        time_R = f'NW{self.AB}_time_R'
        pos = f'NW{self.AB}_pos'

        calib_reader = NWCalibrationReader(self.AB)
        df_result = None
        for run, df_run in df.groupby('run'):
            pars = calib_reader(run)
            pars = pars.loc[df_run[bar]].to_numpy()
            df_run[pos] = pars[:, 0] + pars[:, 1] * (df_run[time_L] - df_run[time_R])
            df_result = pd.concat([df_result, df_run], axis=0)
        df_result = self.convert_64_to_32(df_result)

        if drop_time:
            df_result = df_result.drop([time_L, time_R], axis=1)
        if update_self:
            self.df = df_result
        return df_result
    
    @staticmethod
    def get_light_from_adc(adc, pos, a, b, c, d, e):
        result = 4.196 * adc / (a + b * pos + c * pos**2)
        result = d + result * e
        return result
    
    @staticmethod
    def get_adc_from_light(light, pos, a, b, c, d, e):
        result = (light - d) / (4.196 * e)
        result = result * (a + b * pos + c * pos**2)
        return result
    
    def read_light_calib_params(self, path=None) -> pd.DataFrame:
        if path is None:
            path = Path(os.path.expandvars(self.DATABASE_DIR)) / f'nw{self.ab}_pulse_height_calibration.dat'
        df = pd.read_csv(path, delim_whitespace=True, comment='#')
        return df.set_index('bar', drop=True)
    
    def _read_saturation_correction_params(self, path=None) -> pd.DataFrame:
        """Using Daniele's framework of Kuan's version."""
        if path is None:
            path = Path(os.path.expandvars(self.DATABASE_DIR)) / f'nw{self.ab}_saturation_correction.dat'
        df = pd.read_csv(path, delim_whitespace=True, comment='#')
        return df.set_index('bar', drop=True)

    @staticmethod
    def _randomize_columns(df, columns, seed=None):
        if isinstance(columns, str):
            columns = [columns]
        rand = np.random.RandomState(seed)
        df[columns] += rand.uniform(-0.5, 0.5, size=df[columns].shape)
        return df

    def randomize_ADC(self, seed=None):
        columns = [
            f'NW{self.AB}_total_L',
            f'NW{self.AB}_total_R',
            f'NW{self.AB}_fast_L',
            f'NW{self.AB}_fast_R',
        ]
        self.df = self._randomize_columns(self.df, columns, seed)
        self.df = self.convert_64_to_32(self.df)

    def analyze_log_of_light_ratio(self, light: Literal['total', 'fast'], bars=None, verbose=False):
        """
        """
        df = self.df.query(' & '.join([
            f'NW{self.AB}_{light}_L < 3500',
            f'NW{self.AB}_{light}_R < 3500',
        ]))[[
            f'NW{self.AB}_bar',
            f'NW{self.AB}_pos',
            f'NW{self.AB}_{light}_L',
            f'NW{self.AB}_{light}_R',
        ]]

        self.light_info.setdefault(light, dict())
        if bars is None:
            bars = range(1, 25)
        for bar in bars:
            if verbose:
                print(f'Analyzing bar-{bar:02d}...', flush=True)
            df_bar = dk.query(f'''
                SELECT * FROM df
                WHERE NW{self.AB}_bar == {bar}
            ''').df()
            self.light_info[light].setdefault(bar, dict())
            self.light_info[light][bar].update(self._analyze_log_of_light_ratio_per_bar(
                df_bar[f'NW{self.AB}_pos'],
                df_bar[f'NW{self.AB}_{light}_L'],
                df_bar[f'NW{self.AB}_{light}_R'],
            ))
        
    @staticmethod
    def _analyze_log_of_light_ratio_per_bar(pos, light_L, light_R):
        llr = LogOfLightRatio(pos, light_L, light_R)
        llr.fit_linear()
        llr.fit_wavy()
        return {
            'llr': llr,
            'attenuation_length': llr.attenuation_length,
            'gain_ratio': llr.gain_ratio,
            'log_light_ratio_spread': llr.log_light_ratio_spread,
        }
    
    @staticmethod
    def saturation_recovering(
        light_info: Dict[int, Dict[str, float]],
        bar_arr, pos, light_L, light_R,
        bars=None,
        threshold=4090,
    ):
        # populate scalars array
        scalars = np.zeros(len(bar_arr))
        if bars is None:
            bars = range(1, 25)
        for bar in bars:
            att_length = light_info[bar]['attenuation_length'][0]
            gain_ratio = light_info[bar]['gain_ratio'][0]
            scalars += (bar_arr == bar) * np.exp((2 / att_length) * pos + np.log(gain_ratio))

        # apply scalars conditionally to recover saturation
        recovered_light_L = np.where(
            (light_L > threshold) & (light_R < threshold) & (scalars > 1e-8),
            light_R / scalars,
            light_L,
        )
        recovered_light_R = np.where(
            (light_R > threshold) & (light_L < threshold) & (scalars > 1e-8),
            light_L * scalars,
            light_R,
        )
        return recovered_light_L, recovered_light_R
    
    @staticmethod
    def gain_matching(
        light_info: Dict[int, Dict[str, float]],
        bar_arr, light_L, light_R,
        bars=None,
    ):
        gain_ratios = np.zeros(len(bar_arr))
        if bars is None:
            bars = range(1, 25)
        for bar in bars:
            gain_ratio = light_info[bar]['gain_ratio'][0]
            gain_ratios += (bar_arr == bar) * gain_ratio
        gain_ratios += (gain_ratios < 1e-6) * 1.0

        matched_light_L = light_L * gain_ratios
        matched_light_R = light_R / gain_ratios
        return matched_light_L, matched_light_R
    
    def add_corrected_light(self, light: Literal['total', 'fast'], bars=None):
        light_L = self.df[f'NW{self.AB}_{light}_L']
        light_R = self.df[f'NW{self.AB}_{light}_R']

        light_L, light_R = self.saturation_recovering(
            self.light_info[light],
            self.df[f'NW{self.AB}_bar'],
            self.df[f'NW{self.AB}_pos'],
            light_L, light_R,
            bars,
        )

        light_L, light_R = self.gain_matching(
            self.light_info[light],
            self.df[f'NW{self.AB}_bar'],
            light_L, light_R,
            bars,
        )

        self.df[f'NW{self.AB}_{light}_L_corrected'] = light_L
        self.df[f'NW{self.AB}_{light}_R_corrected'] = light_R
        self.convert_64_to_32(self.df)
    
    def add_calibrated_light(self):
        """Calibrate geometric mean of corrected total ADCs into MeVee"""
        df_par = self.read_light_calib_params()
        df_pars = df_par.loc[self.df[f'NW{self.AB}_bar']]
        self.df[f'NW{self.AB}_light_GM'] = self.get_light_from_adc(
            np.sqrt(self.df[f'NW{self.AB}_total_L_corrected'] * self.df[f'NW{self.AB}_total_R_corrected']),
            np.array(self.df[f'NW{self.AB}_pos']),
            np.array(df_pars.a),
            np.array(df_pars.b),
            np.array(df_pars.c),
            np.array(df_pars.d),
            np.array(df_pars.e),
        )


class ParamIO:
    @classmethod
    def save_params(cls, calib):
        path = ParamIO.get_path(calib.runs, subdir='calib_params')
        path.parent.mkdir(parents=True, exist_ok=True)
        bar_info = dict()
        if path.is_file():
            bar_info = cls.df_to_dict(cls.read(path))
        for bar, info in calib.light_info['total'].items():
            bar_info[bar] = {
                'bar': bar,
                'att_length': info['attenuation_length'][0],
                'att_length_err': info['attenuation_length'][1],
                'gain_ratio': info['gain_ratio'][0],
                'gain_ratio_err': info['gain_ratio'][1],
                'log_light_ratio_spread': info['log_light_ratio_spread'],
            }
        bar_info = dict(sorted(bar_info.items(), key=lambda x: x[0]))
        df = pd.DataFrame(bar_info).T
        tables.to_fwf(
            df,
            path,
            comment=inspect.cleandoc(f'''
            # NW{calib.AB}
            # run(s): {calib.runs}
            # units:
            #     att_length: cm
            #     gain_ratio: unitless
            #     log_light_ratio_spread: unitless
            '''),
        )

    @staticmethod
    def read(path):
        return pd.read_csv(path, delim_whitespace=True, comment='#')
    
    @staticmethod
    def df_to_dict(df):
        df.set_index('bar', inplace=True, drop=True)
        return {bar: dict(bar=bar, **df.loc[bar]) for bar in df.index}

    @staticmethod
    def get_path(runs, subdir):
        path = Path(os.path.expandvars(LightOutputCalibrator.DATABASE_DIR)) / subdir
        if len(runs) == 1:
            path /= f'run-{runs[0]:04d}.dat'
        else:
            path /= f'{misc.runs_hash(runs)}.dat'
        return path
    
    @staticmethod
    def get_good_runs(min_run=None, max_run=None):
        min_run = min_run or 0
        max_run = max_run or 9999

        runs = np.array(list(range(min_run, max_run + 1)))
        return list(runs[Query.are_good(runs)])
    
    @classmethod
    def get_df_bars(cls, bars=None, runs=None, not_found_okay=True):
        bars = bars or range(1, 25)
        df_bars = {bar: [] for bar in bars}
        runs = runs or ParamIO.get_good_runs()
        for run in runs:
            path = cls.get_path([run], subdir='calib_params')
            if not path.is_file():
                if not not_found_okay:
                    raise FileNotFoundError(f'{path} does not exist')
                continue
            df_run = cls.read(path)
            df_run = df_run.set_index('bar', drop=True)
            for bar, holder in df_bars.items():
                holder.append([run, *df_run.loc[bar]])
        for bar, holder in df_bars.items():
            df_bars[bar] = pd.DataFrame(holder, columns=['run', *df_run.columns])
        return df_bars
    
    @staticmethod
    def median50_average(x):
        x = np.array(x)
        mid_range = np.quantile(x, [0.25, 0.75])
        mask = (x > mid_range[0]) & (x < mid_range[1])
        if sum(mask) == 0:
            return np.mean(x)
        return np.mean(x[mask])

    @staticmethod
    def median50_std(x):
        x = np.array(x)
        mid_range = np.quantile(x, [0.25, 0.75])
        mask = (x > mid_range[0]) & (x < mid_range[1])
        if sum(mask) < 4:
            return np.std(x)
        return np.std(x[mask])
    
    @classmethod
    def create_breakpoint_for_bar(cls, df_bar):
        data = np.array(df_bar[['att_length', 'gain_ratio', 'log_light_ratio_spread']].values)
        cpd = rpt.KernelCPD('rbf', min_size=1).fit(data)
        bp_indices = cpd.predict(pen=3)
        
        df_bp = []
        prev_i = 0
        for i_bp in bp_indices:
            rows = df_bar.iloc[prev_i:i_bp]
            att_length = cls.median50_average(rows['att_length'])
            gain_ratio = cls.median50_average(rows['gain_ratio'])
            llr_spread = cls.median50_average(rows['log_light_ratio_spread'])
            att_length_std = cls.median50_std(rows['att_length'])
            gain_ratio_std = cls.median50_std(rows['gain_ratio'])
            llr_spread_std = cls.median50_std(rows['log_light_ratio_spread'])
            df_bp.append([
                min(rows.run), max(rows.run),
                att_length, att_length_std,
                gain_ratio, gain_ratio_std,
                llr_spread, llr_spread_std,
            ])
            prev_i = i_bp
        return pd.DataFrame(
            df_bp,
            columns=[
                'run_start', 'run_stop',
                'att_length', 'att_length_std',
                'gain_ratio', 'gain_ratio_std',
                'log_light_ratio_spread', 'log_light_ratio_spread_std',
            ],
        )
    
    @classmethod
    def create_breakpoint_for_bars(cls, df_bars):
        result = None
        for bar, df_bar in df_bars.items():
            cuts = ['run < 3000', 'run > 3999']
            dfs_bp_bar = None
            for cut in cuts:
                df = df_bar.query(cut)
                if len(df) < 2: continue
                df_bp = cls.create_breakpoint_for_bar(df)
                df_bp.insert(0, 'bar', bar)
                dfs_bp_bar = pd.concat([dfs_bp_bar, df_bp])
            dfs_bp_bar = cls.fill_run_gaps(dfs_bp_bar)
            result = pd.concat([result, dfs_bp_bar])
        return result

    @staticmethod
    def fill_run_gaps(dfs_bp_bar):
        run_starts = dfs_bp_bar['run_start']
        run_stops = dfs_bp_bar['run_stop']
        run_ranges = np.vstack([run_starts, run_stops]).T
        for i in range(1, len(run_ranges)):
            if run_ranges[i, 0] != run_ranges[i - 1, 1] + 1:
                run_ranges[i, 0] = run_ranges[i - 1, 1] + 1
        
        # 3500 split
        i_4xxx = np.where(run_ranges[:, 0] > 3999)[0][0]
        if i_4xxx > 0:
            run_ranges[i_4xxx, 0] = 3501
            run_ranges[i_4xxx - 1, 1] = 3500
        
        result = dfs_bp_bar.copy()
        result['run_start'] = run_ranges[:, 0]
        result['run_stop'] = run_ranges[:, 1]
        return result
    
    @classmethod
    def create_and_save_breakpoints(cls, AB):
        df = cls.create_breakpoint_for_bars(cls.get_df_bars())
        path = Path(os.path.expandvars(LightOutputCalibrator.DATABASE_DIR)) / f'nw{AB.tolower()}_saturation_recovery.csv'
        df.to_csv(path, index=False)


class LogOfLightRatio:
    def __init__(self, pos, light_L, light_R):
        """Class to help analyzing the logarithm of light ratio.

        Data are assumed to be coming from one single NW bar. Experimental runs
        of the same nuclear reactions that are closeby in time could be chained
        together for more statistics.

        Both ``self.x`` and ``self.y`` will be defined as dimensionless
        quantities. The scaling factors are chosen empirically to make the
        ranges of x and y roughly the same.

        Parameters
        ----------
        light_L : array-like
            The light output of detected at the left side of the bar in ADC.
        light_R : array-like
            The light output of detected at the right side of the bar in ADC.
        pos : array-like
            The hit positions on the bar in centimeter.
        """
        self.x_scale = 100.0
        self.y_scale = 2.0
        mask = (light_L > 0) & (light_R > 0) & (pos > -150) & (pos < 150)
        self.df = pd.DataFrame({
            'x': pos[mask] / self.x_scale,
            'y': np.log(light_R[mask] / light_L[mask]) / self.y_scale,
        })
    
    @staticmethod
    def rotate(x, y, angle):
        return (
            x * np.cos(angle) + y * np.sin(angle),
            -x * np.sin(angle) + y * np.cos(angle),
        )
    
    def decorate_descale_xy(self, func):
        return lambda x: func(x / self.x_scale) * self.y_scale

    def fit_linear(self):
        """A robust linear fit on (x, y) using PCA and RANSAC.

        To improve robustness against outliers, PCA is first performed so that
        data points with 2nd components far from zero are removed. The remaining
        data are fed to RANSAC to find the most robust linear fit, updated as
        ``self.linear_fit``.
        """
        df = self.df[['x', 'y']].reset_index(drop=True)
        pca = PCA(n_components=2).fit(df)
        pca_xy = pca.transform(self.df[['x', 'y']])
        mask = (pca_xy[:, 1] > -0.4) & (pca_xy[:, 1] < 0.4)
        mask &= (self.df['x'] > -0.6) & (self.df['x'] < 0.6)
        ransac = RANSACRegressor(min_samples=0.5)
        ransac.fit(self.df[['x']][mask], self.df['y'][mask])
        self._linear_fit = np.polynomial.Polynomial([
            float(ransac.estimator_.intercept_),
            float(ransac.estimator_.coef_),
        ])
        self.linear_fit = self.decorate_descale_xy(self._linear_fit)
    
    def flatten(self, x, y):
        """Rotates (x, y) to zero slope of the linear fit.

        Rotation center is at origin, not the intersection between the linear
        fit and the y-axis. This function is only used intermediately for
        fitting the "wavy" relation.
        """
        angle = np.arctan(self._linear_fit.coef[1])
        return self.rotate(x, y, angle)
    
    def deflatten(self, x, y):
        """Rotates flattened (x, y) back to the original points.
        """
        angle = np.arctan(self._linear_fit.coef[1])
        return self.rotate(x, y, -angle)

    def fit_wavy_on_flat(self, return_xy=False):
        df = pd.DataFrame(
            np.vstack(self.flatten(self.df['x'], self.df['y'])).T,
            columns=['x_flat', 'y_flat'],
        )
        df = df.query('y_flat > -0.5 & y_flat < 0.5')

        xf, yf = [], []
        for x_range in slicer.create_ranges(-2.2, 2.2, width=0.1, step=0.02):
            arr = dk.query(f'''
                SELECT y_flat FROM df
                WHERE x_flat > {x_range[0]} AND x_flat < {x_range[1]}
            ''').df().y_flat
            if len(arr) < 30:
                continue
            finder = peak_finder.PeakFinderGaus1D(arr, hist_range=[-1, 1], hist_bins=200)
            xf.append(np.mean(x_range))
            yf.append(finder.get_highest_peak()[1])
        xf, yf = np.array(xf), np.array(yf)
        
        kernel = RBF(
            length_scale=1e-1,
            length_scale_bounds=(1e-3, 1e3),
        )
        kernel += WhiteKernel(
            noise_level=1e-5,
            noise_level_bounds=(1e-10, 1e-1),
        )
        self._gpr = GaussianProcessRegressor(kernel=kernel, n_restarts_optimizer=5)
        self._gpr.fit(xf[:, None], yf)
        self._wavy_fit_on_flat = lambda x: self._gpr.predict(x[:, None])
        return xf, yf if return_xy else None
    
    @staticmethod
    def extract_longest_increasing_subarray(x):
        n = len(x)
        i_longest, longest = 0, 0
        i_start, length = 0, 0
        for i in range(n - 1):
            if x[i] < x[i + 1]:
                length += 1
            else:
                if length > longest:
                    i_longest, longest = i_start, length
                i_start, length = i + 1, 0
        if length > longest:
            i_longest, longest = i_start, length
        return i_longest, i_longest + longest
    
    def fit_wavy(self):
        xf, _ = self.fit_wavy_on_flat(return_xy=True)
        xf = np.linspace(xf[0], xf[-1], 500)
        yf = self._wavy_fit_on_flat(xf)
        x, y = self.deflatten(xf, yf)

        # select longest monotonic x range
        i0, i1 = self.extract_longest_increasing_subarray(x)
        x = x[i0 : i1 + 1]
        y = y[i0 : i1 + 1]

        # extrapolate both ends linearly following the linear fit slope
        slope = self._linear_fit.coef[1]
        extrapolate = lambda x, m, x0, y0: m * (x - x0) + y0
        ext_L = lambda x_: extrapolate(x_, slope, x[0], y[0])
        ext_R = lambda x_: extrapolate(x_, slope, x[-1], y[-1])

        # interpolate the wavy curve
        x = np.hstack([x[0] - 1, x, x[-1] + 1])
        y = np.hstack([ext_L(x[0] - 1), y, ext_R(x[-1] + 1)])
        self._wavy_fit = interpolate.PchipInterpolator(x, y, extrapolate=True)
        self.wavy_fit = self.decorate_descale_xy(self._wavy_fit)

    def get_wavy_slopes(self, x):
        return nd.Derivative(self.wavy_fit, n=1)(x)
    
    @staticmethod
    def slope_to_attenuation_length(slope):
        return 2 / slope
    
    @staticmethod
    def intercept_to_gain_ratio(intercept):
        return np.exp(intercept)

    @property
    def attenuation_length_from_wavy(self):
        """In unit of centimeter"""
        x = np.linspace(-25, 25, 1000)
        slopes = self.get_wavy_slopes(x)
        att_lengths = self.slope_to_attenuation_length(slopes)
        return (
            np.mean(att_lengths),
            np.std(att_lengths),
        )

    @property
    def attenuation_length_from_linear(self):
        """In unit of centimeter"""
        slope = self.linear_fit(1) - self.linear_fit(0)
        return (
            self.slope_to_attenuation_length(slope),
            0.0
        )
    
    @property
    def attenuation_length(self):
        wavy = self.attenuation_length_from_wavy
        linear = self.attenuation_length_from_linear
        if np.allclose(wavy[0], linear[0], rtol=0.05):
            return wavy
        return (linear[0], wavy[1])
    
    @property
    def gain_ratio(self):
        """Gain right divided by gain left"""
        intercept = self.wavy_fit(0)
        xf, yf = self.flatten(0, intercept / self.y_scale)
        yf2, yf_err = self._gpr.predict([[xf]], return_std=True)
        if not np.isclose(yf, yf2, atol=1e-6):
            warnings.warn(f'GPR slope is not close to the linear slope at x = 0 cm')
        intercept_err = abs(intercept * yf_err / yf)

        gain_ratio = self.intercept_to_gain_ratio(intercept)
        gain_ratio_err = float(np.exp(gain_ratio) * intercept_err)

        return gain_ratio, gain_ratio_err
    
    @property
    def log_light_ratio_spread(self):
        y = self.df.query('x > -0.05 & x < 0.05').y
        return np.std(y) * self.y_scale

    def add_straightened_xy(self):
        dy = self._wavy_fit(self.df.x) - self._linear_fit(self.df.x)
        self.df['x_straight'] = self.df.x
        self.df['y_straight'] = self.df.y - dy
    
    def straighten(self, pos, log_light_ratio):
        return log_light_ratio - (self.wavy_fit(pos) - self.linear_fit(pos))


class LogOfLightRatioPlotter:
    def __init__(self, llr):
        self.llr = llr

    @property
    def x_plt(self):
        return np.linspace(-120, 120, 200)
    
    def _plot_hist2d(self, ax, df=None, straighten=False, **kwargs):
        if df is None:
            df = self.llr.df
        kw = dict(
            range=[[-150, 150], [-4, 4]],
            bins=[100, 100],
            cmap=plt.cm.viridis,
            norm=mpl.colors.LogNorm(vmin=1),
        )
        kw.update(kwargs)
        if straighten:
            x, y = 'x_straight', 'y_straight'
        else:
            x, y = 'x', 'y'
        fh.plot_histo2d(
            ax.hist2d,
            df[x] * self.llr.x_scale,
            df[y] * self.llr.y_scale,
            **kw,
        )
        return ax

    def _plot_linear(self, ax, **kwargs):
        kw = dict(
            color='purple',
            linewidth=1,
            linestyle='dashed',
        )
        kw.update(kwargs)
        ax.plot(self.x_plt, self.llr.linear_fit(self.x_plt), **kw)
        return ax
    
    def _plot_wavy(self, ax, **kwargs):
        kw = dict(
            color='red',
            linewidth=1,
        )
        kw.update(kwargs)
        ax.plot(self.x_plt, self.llr.wavy_fit(self.x_plt), **kw)
        return ax

    def plot(self, ax=None, df=None, straighten=False, wavy_fit=False, linear_fit=False):
        if ax is None:
            ax = plt.gca()
        self._plot_hist2d(ax, df=df, straighten=straighten)
        if wavy_fit:
            self._plot_wavy(ax)
        if linear_fit:
            self._plot_linear(ax)
        ax.set_xlabel('NW bar position (cm)')
        ax.set_ylabel(r'Log of light ratio $\ln(\mathcal{L}_R/\mathcal{L}_L)$')
        ax.set_xlim(-130, 130)
        ax.set_ylim(-3.5, 3.5)
        ax.axhline(0, color='gray', linestyle='dotted', linewidth=0.6)
        ax.axvline(0, color='gray', linestyle='dotted', linewidth=0.6)
        ax.annotate(
            '\n'.join([
            r'$\lambda = {:.1f} \pm {:.1f}$ cm'.format(*self.llr.attenuation_length),
            r'$g_R / g_L = {:.2f} \pm {:.2f}$'.format(*self.llr.gain_ratio),
            r'$\sigma_y = {:.2f}$'.format(self.llr.log_light_ratio_spread),
            ]),
            (0.05, 0.95),
            xycoords='axes fraction',
            ha='left',
            va='top',
            bbox=dict(boxstyle='round', facecolor='white', linewidth=0.3),
        )
        return ax

    @staticmethod
    def save_plots(calib, verbose=True):
        import matplotlib as mpl
        mpl.use('Agg')
        directory = Path(os.path.expandvars(calib.DATABASE_DIR)) / 'gallery'
        if len(calib.runs) == 1:
            run_title = f'run-{calib.runs[0]:04d}'
            directory /= f'{run_title}/'
        else:
            run_title = f'{misc.run_hash(calib.runs)}'
            directory /= f'{run_title}/'
            run_title = run_title.split('-h')[0]
        directory.mkdir(parents=True, exist_ok=True)

        for bar, info in calib.light_info['total'].items():
            if verbose:
                print(f'Plotting NW{calib.AB}-{bar:02d}...')
            path = directory / f'NW{calib.AB}-bar{bar:02d}.png'
            
            fig, ax = plt.subplots(figsize=(5, 4), facecolor='white', constrained_layout=True)
            llr = LogOfLightRatioPlotter(info['llr'])
            llr.plot(ax=ax, wavy_fit=True, linear_fit=True)
            ax.set_title(f'{run_title} NW{calib.AB}-{bar:02d}')
            fig.savefig(path, dpi=150, bbox_inches='tight')
            plt.draw()
            plt.close(fig)


class _Benchmark:
    """A development class to check if the code gives identical result to Daniele's framework."""
    def __init__(self):
        pass

    @staticmethod
    def read_daniele_root_file(path, tree_name='E15190'):
        import uproot
        branches = {
            'NWB.fnumbar': 'bar',
            'NWB.fLeft': 'total_L',
            'NWB.fRight': 'total_R',
            'NWB.fXcm': 'pos',
            'NWB.fGeoMeanSaturationCorrected': 'light_GM',
        }

        with uproot.open(str(path) + ':' + tree_name) as tree:
            df = tree.arrays(
                list(branches.keys()),
                entry_stop=10000,
                library='pd',
            )
        df.columns = list(branches.values())
        return df
    
    @staticmethod
    def get_saturation_corrected(df):
        """
        https://github.com/nscl-hira/E15190-Unified-Analysis-Framework/blob/837d3ad7a30a2678991bb5e6f9d7ddf0bc890473/NWPulseHeightCalibration.cpp#L223-L242
        """
        lo = LightOutputCalibrator('B')
        df_pul = lo.read_light_calib_params()
        df_sat = lo.read_saturation_correction_params()
        light_bm = []
        for _, entry in df.iterrows():
            bar = int(entry['bar'])
            p_sat = df_sat.loc[bar]
            if entry.total_L > 4090 and entry.total_R > 4090:
                light = np.sqrt(entry.total_L * entry.total_R)
            elif entry.total_L > 4090:
                light = np.exp(p_sat.p0 + p_sat.p1 * entry.pos + p_sat.p2 * entry.pos**2)
                light = np.sqrt(entry.total_R**2 / light)
            elif entry.total_R > 4090:
                light = np.exp(p_sat.p0 + p_sat.p1 * entry.pos + p_sat.p2 * entry.pos**2)
                light = np.sqrt(entry.total_L**2 * light)
            else:
                light = np.sqrt(entry.total_L * entry.total_R)
        
            p_pul = df_pul.loc[bar]
            light += np.random.uniform(-0.5, 0.5)
            light /= p_pul.a + p_pul.b * entry.pos + p_pul.c * entry.pos**2
            light = p_pul.d + 4.196 * p_pul.e * light

            light_bm.append(light)
        df['light_bm'] = light_bm
        return df
    
    @staticmethod
    def check_daniele_kuan(plot=False):
        from e15190 import DATABASE_DIR
        import json
        with open(DATABASE_DIR / 'local_paths.json') as file:
            directory = Path(json.load(file)['daniele_root_files_dir'])

        df = _Benchmark.read_daniele_root_file(directory / 'CalibratedData_4083.root')
        df = _Benchmark.get_saturation_corrected(df)

        diff = df.light_bm - df.light_GM
        diff = diff[diff < 100]

        if plot:
            fig, ax = plt.subplots()
            ax.scatter(df.light_GM, df.light_bm, s=0.5)
            ax.set_xlim(0, 100)
            ax.set_ylim(0, 100)
            plt.show()

        assert np.max(np.abs(diff)) < 2


class _MainUtilities:
    @staticmethod
    def get_args():
        parser = argparse.ArgumentParser(
            description='Calibrate the light output for neutron wall.',
            formatter_class=argparse.RawTextHelpFormatter,
        )
        parser.add_argument(
            'AB',
            type=str,
            choices=['A', 'B'],
            help='"A" or "B". This selects "NWA" or "NWB".',
        )
        parser.add_argument(
            'runs',
            nargs='+',
            help=inspect.cleandoc('''
                Runs to calibrate.

                Consecutive runs can be specified as a range. Here is an
                example:
                    > ./light_output_calibration.py B 8-10 11 20 2-5
                This will calibrate the runs 8, 9, 10, 11, 20, 2, 3, 4, 5, on
                NWB.
            '''),
        )
        parser.add_argument(
            '-b', '--bars',
            nargs='+',
            help=misc.MainUtilities.wrap('''
                The bar number(s) to calibrate. If not specified, all bars from
                bar 1 to 24 will be analyzed. Similar to "runs", dash can be
                used to specify ranges.
            '''),
            default=['1-24'],
        )
        parser.add_argument(
            '-c', '--no-cache',
            help=misc.MainUtilities.wrap('''
                When this option is given, the script will ignore the caches.
                All data will hence be read from the (Daniele's) ROOT files. New
                cache files will then be created, overwritting the old ones.
            '''),
            action='store_true',
        )
        parser.add_argument(
            '-d', '--debug',
            help=misc.MainUtilities.wrap('''
                When this option is given, no output would be saved. This
                includes both calibration parameters and gallery. The cache
                files, however, will still be updated.
            '''),
            action='store_true',
        )
        parser.add_argument(
            '-o', '--output',
            help=misc.MainUtilities.wrap('''
                The output directory for resultant ROOT files. If not given, the default is
                "$DATABASE_DIR/neutron_wall/light_output_calibration/".
            '''),
            default="$DATABASE_DIR/neutron_wall/light_output_calibration/",
        )
        parser.add_argument(
            '-s', '--silence',
            help='To silent all status messages.',
            action='store_true',
        )
        args = parser.parse_args()
        args.runs = misc.MainUtilities.parse_runs(
            args.runs,
            Query.are_good,
            verbose=(not args.silence),
        )
        args.bars = misc.MainUtilities.parse_bars(args.bars)
        return args

if __name__ == '__main__':
    import argparse
    args = _MainUtilities.get_args()

    LightOutputCalibrator.DATABASE_DIR = args.output
    calib = LightOutputCalibrator(args.AB)
    for run in args.runs:
        try:
            calib.read(
                run,
                from_cache=(not args.no_cache),
                verbose=(not args.silence),
            )
            calib.add_position()
            calib.randomize_ADC()
            calib.analyze_log_of_light_ratio(
                'total',
                bars=args.bars,
                verbose=(not args.silence),
            )
            if not args.debug:
                ParamIO.save_params(calib)
                LogOfLightRatioPlotter.save_plots(calib)
        except Exception as err:
            print(f'Error in run {run}:\n{err}')
