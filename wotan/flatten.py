"""Wotan is a free and open source algorithm to automagically remove stellar trends
from light curves for exoplanet transit detection.
"""

from __future__ import print_function, division
import numpy
from numpy import array, isnan, float32, append, full, where, nan, ones, inf, median
from scipy.signal import savgol_filter, medfilt

# wotan
import wotan.constants as constants
from wotan.cofiam import detrend_cofiam
from wotan.gp import make_gp
from wotan.huber_spline import detrend_huber_spline
from wotan.slider import running_segment, running_segment_huber
from wotan.gaps import get_gaps_indexes
from wotan.t14 import t14
from wotan.pspline import pspline


def flatten(time, flux, window_length=None, edge_cutoff=0, break_tolerance=None,
            cval=None, return_trend=False, method='biweight', kernel=None,
            kernel_size=None, kernel_period=None, proportiontocut=0.1):
    """``flatten`` removes low frequency trends in time-series data.
    Parameters
    ----------
    time : array-like
        Time values
    flux : array-like
        Flux values for every time point
    window_length : float
        The length of the filter window in units of ``time`` (usually days), or in
        cadences (for cadence-based sliders ``savgol`` and ``medfilt``).
    method : string, default: `biweight`
        Determines detrending method and location estimator. A time-windowed slider is
        invoked for location estimators `median`, `biweight`, `hodges`, `welsch`,
        `huber`, `andrewsinewave`, `mean`, `trim_mean`, or `winsorize`. Spline-based
        detrending is performed for `hspline` and `untrendy`. A locally weighted
        scatterplot smoothing is performed for `lowess`. The Savitzky-Golay filter is
        run for ``savgol``. A cadence-based sliding median is performed for ``medfilt``.
    break_tolerance : float, default: window_length/2
        If there are large gaps in time (larger than ``window_length``/2), flatten will
        split the flux into several sub-lightcurves and apply the filter to each
        individually. ``break_tolerance`` must be in the same unit as ``time`` (usually
        days). To disable this feature, set ``break_tolerance`` to 0. If the method is
        ``supersmoother`` and no ``break_tolerance`` is provided, it will be taken as
        `1` in units of ``time``.
    edge_cutoff : float, default: None
        Trends near edges are less robust. Depending on the data, it may be beneficial
        to remove edges. The ``edge_cutoff`` defines the length (in units of time) to be
        cut off each edge. Default: Zero. Cut off is maximally ``window_length``/2, as
        this fills the window completely. Applicable only for time-windowed sliders.
    cval : float or int
        Tuning parameter for the robust estimators. Default values are 5 (`biweight` and
        `lowess`), 1.339 (`andrewsinewave`), 2.11 (`welsch`), 1.5 (``huber``). A
        ``cval`` of 6 for the biweight includes data up to 4 standard deviations from
        the central location and has an efficiency of 98%. Another typical value for the
        biweight is 4.685 with 95% efficiency. Larger values for make the estimate more
        efficient but less robust. For the super-smoother, cval determines the bass
        enhancement (smoothness) and can be `None` or in the range 0 < ``cval`` < 10.
        For the ``savgol``, ``cval`` determines the (integer) polynomial order
        (default: 2).
    proportiontocut : float, default: 0.1
        Fraction to cut off (or filled) of both tails of the distribution using methods
        ``trim_mean`` (or ``winsorize``)
    kernel : str, default: `squared_exp`
        Choice of `squared_exp` (squared exponential), `matern`, `periodic`,
        `periodic_auto`.
    kernel_size : float, default: 1
        The length scale of the Gaussian Process kernel.
    kernel_period = float
        The periodicity of the Gaussian Process kernel (in units of ``time``). Must be
        provided for the kernel `periodic`. Can not be specified for the
        `periodic_auto`, for which it is determined automatically using a Lomb-Scargle
        periodogram pre-search.
    return_trend : bool, default: False
        If `True`, the method will return a tuple of two elements
        (``flattened_flux``, ``trend_flux``) where ``trend_flux`` is the removed trend.
    Returns
    -------
    flatten_flux : array-like
        Flattened flux.
    trend_flux : array-like
        Trend in the flux. Only returned if ``return_trend`` is `True`.
    """
    
    if method not in constants.methods:
        raise ValueError('Unknown detrending method')

    # Numba can't handle strings, so we're passing the location estimator as an int:
    if method == 'biweight':
        method_code = 1
    elif method == 'andrewsinewave':
        method_code = 2
    elif method == 'welsch':
        method_code = 3
    elif method == 'hodges':
        method_code = 4
    elif method == 'median':
        method_code = 5
    elif method == 'mean':
        method_code = 6
    elif method == 'trim_mean':
        method_code = 7
    elif method == 'winsorize':
        method_code = 8

    error_text = 'proportiontocut must be >0 and <0.5'
    if not isinstance(proportiontocut, float):
        raise ValueError(error_text)
    if proportiontocut >= 0.5 or proportiontocut <= 0:
        raise ValueError(error_text)

    # Default cval values for robust location estimators
    if cval is None:
        if method == 'biweight':
            cval = 5
        elif method == 'andrewsinewave':
            cval = 1.339
        elif method == 'welsch':
            cval = 2.11
        elif method == 'huber':
            cval = 1.5
        elif method in ['trim_mean', 'winsorize']:
            cval = proportiontocut
        elif method == 'savgol':  # polyorder
            cval = 2  # int
        else:
            cval = 0  # avoid numba type inference error: None type multi with float

    if cval is not None and method == 'supersmoother':
        if cval > 0 and cval < 10:
            supersmoother_alpha = cval
        else:
            supersmoother_alpha = None

    # Maximum gap in time should be half a window size.
    # Any larger is nonsense,  because then the array has a full window of data
    if window_length is None:
        window_length = 2  # so that break_tolerance = 1 in the supersmoother case
    if break_tolerance is None:
        break_tolerance = window_length / 2
    if break_tolerance == 0:
        break_tolerance = inf

    # Numba is very fast, but doesn't play nicely with NaN values
    # Therefore, we make new time-flux arrays with only the floating point values
    # All calculations are done within these arrays
    # Afterwards, the trend is transplanted into the original arrays (with the NaNs)
    time = array(time, dtype=float32)
    flux = array(flux, dtype=float32)
    mask = isnan(time * flux)
    time_compressed = numpy.ma.compressed(numpy.ma.masked_array(time, mask))
    flux_compressed = numpy.ma.compressed(numpy.ma.masked_array(flux, mask))

    # Get the indexes of the gaps
    gaps_indexes = get_gaps_indexes(time_compressed, break_tolerance=break_tolerance)
    trend_flux = array([])
    trend_segment = array([])

    # Iterate over all segments
    for i in range(len(gaps_indexes) - 1):
        time_view = time_compressed[gaps_indexes[i]:gaps_indexes[i+1]]
        flux_view = flux_compressed[gaps_indexes[i]:gaps_indexes[i+1]]
        methods = ["biweight", "andrewsinewave", "welsch", "hodges", "median", "mean",
            "trim_mean", "winsorize"]
        if method in methods:
            trend_segment = running_segment(
                time_view,
                flux_view,
                window_length,
                edge_cutoff,
                cval,
                method_code)
        elif method == 'huber':
            trend_segment = running_segment_huber(
                time_view,
                flux_view,
                window_length,
                edge_cutoff,
                cval
                )
        elif method == 'lowess':
            try:
                import statsmodels.api
            except:
                raise ImportError('Could not import statsmodels')
            duration = numpy.max(time_compressed) - numpy.min(time_compressed)
            trend_segment = statsmodels.api.nonparametric.lowess(
                endog=flux_view,
                exog=time_view,
                frac=window_length / duration,
                missing='none',
                return_sorted=False
                )
        elif method == 'hspline':
            trend_segment = detrend_huber_spline(
                time_view,
                flux_view,
                knot_distance=window_length)
        elif method == 'supersmoother':
            try:
                from supersmoother import SuperSmoother as supersmoother
            except:
                raise ImportError('Could not import supersmoother')
            win = window_length / (max(time)-min(time))
            trend_segment = supersmoother(
                alpha=supersmoother_alpha,
                primary_spans=(
                    constants.primary_span_lower * win, 
                    win,
                    constants.primary_span_upper * win
                    ),
                middle_span=constants.middle_span * win,
                final_span=constants.upper_span * win
                ).fit(time_view, flux_view,).predict(time_view)
        elif method == 'cofiam':
            trend_segment = detrend_cofiam(
                time_view, flux_view, ones(len(time_view)), window_length)
        elif method == 'savgol':
            if window_length%2 == 0:
                window_length += 1
            trend_segment = savgol_filter(flux_view, window_length, polyorder=int(cval))
        elif method == 'medfilt':
            trend_segment = medfilt(flux_view, window_length)
        elif method == 'gp':
            trend_segment = make_gp(
                time_view,
                flux_view,
                kernel,
                kernel_size,
                kernel_period
                )
        elif method == 'untrendy':
            try:
                from untrendy import fit_trend
            except:
                raise ImportError('Could not import untrendy')
            # untrendy needs flux near unity, otherwise it crashes in scipy/interpolate
            # So, we normalize by some constant (the median) and later transform back
            scale_factor = median(flux_view)
            call_trend = fit_trend(time_view, flux_view / scale_factor, dt=window_length)
            trend_segment = call_trend(time_view) * scale_factor
        elif method == 'pspline':
            print('Segment', i + 1, 'of', len(gaps_indexes) - 1)
            trend_segment = pspline(time_view, flux_view)

        trend_flux = append(trend_flux, trend_segment)

    # Insert results of non-NaNs into original data stream
    trend_lc = full(len(time), nan)
    mask = where(~mask)[0]
    for idx in range(len(mask)):
        trend_lc[mask[idx]] = trend_flux[idx]

    flatten_lc = flux / trend_lc
    if return_trend:
        return flatten_lc, trend_lc
    return flatten_lc
