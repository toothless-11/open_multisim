"""Time-aware analysis of transient waveforms (numpy-vectorized)."""

from dataclasses import dataclass
import math
from typing import Optional, Sequence

import numpy as np


PASS = "PASS"
DISTORTED = "DISTORTED"
SIMULATION_ERROR = "SIMULATION_ERROR"
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
FREQUENCY_DETECTION_FAILED = "FREQUENCY_DETECTION_FAILED"
INVALID_DATA = "INVALID_DATA"


@dataclass(frozen=True)
class AnalysisResult:
    status: str
    distortion: Optional[float] = None
    frequency: Optional[float] = None
    vpp: Optional[float] = None
    maximum: Optional[float] = None
    minimum: Optional[float] = None
    mean: Optional[float] = None
    rms: Optional[float] = None
    ac_rms: Optional[float] = None
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    num_points: int = 0
    message: Optional[str] = None

    def as_dict(self):
        return {
            "status": self.status,
            "distortion": self.distortion,
            "frequency": self.frequency,
            "vpp": self.vpp,
            "maximum": self.maximum,
            "minimum": self.minimum,
            "mean": self.mean,
            "rms": self.rms,
            "ac_rms": self.ac_rms,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "num_points": self.num_points,
            "message": self.message,
        }


# ------------------------------------------------------------------
# Vectorized numerical primitives
# ------------------------------------------------------------------

def _trapezoid_weights(times):
    """Per-node trapezoidal integration weights, vectorized."""
    dt = np.diff(times)
    weights = np.empty_like(times)
    weights[0] = dt[0] * 0.5
    weights[-1] = dt[-1] * 0.5
    weights[1:-1] = (dt[:-1] + dt[1:]) * 0.5
    return weights


def _integral(times, values):
    """Trapezoidal integral of *values* over *times*, scalar return."""
    return float(np.trapezoid(values, times))


def _solve_3x3(matrix, vector):
    """Solve a 3×3 linear system via numpy."""
    return np.linalg.solve(np.array(matrix), np.array(vector))


def _fit_at_frequency(times, values, weights, frequency):
    """Weighted least-squares sinusoidal fit at a single frequency.

    Returns (normalized_error, coefficients) where coefficients is the
    [sin, cos, dc] triple.

    Fully vectorized: the per-sample sin/cos and matrix accumulation
    are done with numpy array operations instead of Python loops.
    """
    angles = 2.0 * math.pi * frequency * times
    sin_vals = np.sin(angles)
    cos_vals = np.cos(angles)
    ones = np.ones_like(times)

    # Design matrix columns: [sin, cos, 1]
    # Accumulate the 3×3 normal matrix and 3-element RHS vector.
    w = weights
    s_w = sin_vals * w
    c_w = cos_vals * w
    o_w = ones * w

    matrix = np.array([
        [np.dot(s_w, sin_vals), np.dot(s_w, cos_vals), np.sum(s_w)],
        [np.dot(c_w, sin_vals), np.dot(c_w, cos_vals), np.sum(c_w)],
        [np.dot(o_w, sin_vals), np.dot(o_w, cos_vals), np.sum(o_w)],
    ])
    rhs = np.array([
        np.dot(s_w, values),
        np.dot(c_w, values),
        np.dot(o_w, values),
    ])

    coefficients = _solve_3x3(matrix, rhs)

    # Residual and normalized error
    fitted = coefficients[0] * sin_vals + coefficients[1] * cos_vals + coefficients[2]
    residual = values - fitted
    residual_integral = float(np.trapezoid(residual * residual, times))

    mean = _integral(times, values) / (times[-1] - times[0])
    centered = values - mean
    variance_integral = float(np.trapezoid(centered * centered, times))
    if variance_integral <= 0.0:
        raise ArithmeticError("waveform has no measurable AC component")

    error = math.sqrt(max(0.0, residual_integral / variance_integral))
    return error, coefficients


def _interpolate(t0, t1, y0, y1, target):
    if y1 == y0:
        return t1, y1
    fraction = (target - y0) / (y1 - y0)
    return t0 + fraction * (t1 - t0), target


def _last_three_cycles(times, values):
    """Extract the final three complete cycles from a waveform.

    Returns (selected_times, selected_values, frequency, start, end).
    """
    duration = times[-1] - times[0]
    if duration <= 0.0:
        raise ValueError("time range must be positive")
    mean = _integral(times, values) / duration

    # Vectorized rising-crossing detection: find indices where the
    # signal crosses upward through the mean.
    below = values[:-1] < mean
    above = values[1:] >= mean
    crossing_mask = below & above
    crossing_indices = np.nonzero(crossing_mask)[0]

    if len(crossing_indices) < 4:
        raise LookupError("fewer than four rising crossings; three full cycles unavailable")

    # Interpolate exact crossing times (vectorized)
    idx = crossing_indices
    t0 = times[idx]
    t1 = times[idx + 1]
    y0 = values[idx]
    y1 = values[idx + 1]
    # Avoid division by zero
    dy = y1 - y0
    safe_dy = np.where(dy == 0, 1.0, dy)
    fractions = (mean - y0) / safe_dy
    crossings = t0 + fractions * (t1 - t0)

    # Ensure strictly increasing
    crossings = np.unique(crossings)
    if len(crossings) < 4:
        raise LookupError("fewer than four rising crossings; three full cycles unavailable")

    selected_crossings = crossings[-4:]
    periods = np.diff(selected_crossings)
    if np.any(periods <= 0.0):
        raise LookupError("invalid crossing intervals")
    frequency = 3.0 / float(np.sum(periods))

    start = float(selected_crossings[0])
    end = float(selected_crossings[-1])

    # Extract samples within [start, end] with interpolated boundaries
    # Find the insertion point for start
    start_idx = np.searchsorted(times, start, side="right") - 1
    start_idx = max(0, min(start_idx, len(times) - 2))
    _, start_value = _interpolate(
        float(times[start_idx]), float(times[start_idx + 1]),
        float(values[start_idx]), float(values[start_idx + 1]),
        mean,
    )

    # Find the insertion point for end
    end_idx = np.searchsorted(times, end, side="right") - 1
    end_idx = max(0, min(end_idx, len(times) - 2))
    _, end_value = _interpolate(
        float(times[end_idx]), float(times[end_idx + 1]),
        float(values[end_idx]), float(values[end_idx + 1]),
        mean,
    )

    # Interior points strictly between start and end
    interior_mask = (times > start) & (times < end)
    interior_times = times[interior_mask]
    interior_values = values[interior_mask]

    # Assemble: [start] + interior + [end]
    selected_times = np.concatenate(([start], interior_times, [end]))
    selected_values = np.concatenate(([start_value], interior_values, [end_value]))

    if len(selected_times) < 4:
        raise LookupError("not enough samples in the final three cycles")

    return selected_times, selected_values, frequency, start, end


def estimate_frequency(times, values):
    """Estimate frequency from the final four rising mean crossings."""
    times = np.asarray(times, dtype=float)
    values = np.asarray(values, dtype=float)
    if len(times) != len(values) or len(times) < 4:
        raise ValueError("waveform data is insufficient")
    if not np.all(np.isfinite(times)) or not np.all(np.isfinite(values)):
        raise ValueError("waveform contains non-finite data")
    if np.any(np.diff(times) <= 0):
        raise ValueError("time values must be strictly increasing")
    return _last_three_cycles(times, values)[2]


def analyze_waveform(times, values, max_distortion):
    """Analyze the final three complete cycles and classify the fit residual.

    Parameters
    ----------
    times : array-like
        Time points (strictly increasing).
    values : array-like
        Waveform values at each time point.
    max_distortion : float
        Normalized sine-fit residual threshold (e.g. 0.02 for 2%).

    Returns
    -------
    AnalysisResult
    """
    try:
        times = np.asarray(times, dtype=float)
        values = np.asarray(values, dtype=float)
        if len(times) != len(values):
            raise ValueError("time and value arrays have different lengths")
        if len(times) < 4:
            return AnalysisResult(INSUFFICIENT_DATA, message="at least four samples are required")
        threshold = float(max_distortion)
        if not math.isfinite(threshold) or threshold <= 0.0:
            raise ValueError("distortion threshold must be a finite positive number")
        if not np.all(np.isfinite(times)) or not np.all(np.isfinite(values)):
            raise ValueError("waveform contains non-finite data")
        if np.any(np.diff(times) <= 0):
            raise ValueError("time values must be strictly increasing")
    except (TypeError, ValueError, OverflowError) as error:
        return AnalysisResult(INVALID_DATA, message=str(error))

    try:
        fit_times, fit_values, initial_frequency, start, end = _last_three_cycles(
            times, values
        )
    except LookupError as error:
        status = INSUFFICIENT_DATA if "samples" in str(error) else FREQUENCY_DETECTION_FAILED
        return AnalysisResult(status, message=str(error))
    except (ValueError, ZeroDivisionError, OverflowError) as error:
        return AnalysisResult(INVALID_DATA, message=str(error))

    weights = _trapezoid_weights(fit_times)
    low = initial_frequency * 0.9
    high = initial_frequency * 1.1
    try:
        # Coarse frequency grid scan — vectorized batch evaluation
        grid_freqs = np.linspace(low, high, 61)
        grid_errors = np.empty(len(grid_freqs))
        grid_errors.fill(np.inf)
        for i, freq in enumerate(grid_freqs):
            try:
                err, _ = _fit_at_frequency(fit_times, fit_values, weights, float(freq))
                grid_errors[i] = err
            except (ArithmeticError, np.linalg.LinAlgError):
                continue

        valid = np.isfinite(grid_errors)
        if not np.any(valid):
            return AnalysisResult(
                FREQUENCY_DETECTION_FAILED,
                message="sinusoidal fit could not determine a reliable frequency",
            )

        best_idx = int(np.argmin(grid_errors))
        best_error = float(grid_errors[best_idx])

        # Golden-section refinement around the best grid point
        left = float(grid_freqs[max(0, best_idx - 1)])
        right = float(grid_freqs[min(len(grid_freqs) - 1, best_idx + 1)])
        ratio = (math.sqrt(5.0) - 1.0) / 2.0
        x1 = right - ratio * (right - left)
        x2 = left + ratio * (right - left)
        for _ in range(45):
            try:
                e1, _ = _fit_at_frequency(fit_times, fit_values, weights, x1)
            except (ArithmeticError, np.linalg.LinAlgError):
                e1 = float("inf")
            try:
                e2, _ = _fit_at_frequency(fit_times, fit_values, weights, x2)
            except (ArithmeticError, np.linalg.LinAlgError):
                e2 = float("inf")
            if e1 <= e2:
                right, x2 = x2, x1
                x1 = right - ratio * (right - left)
            else:
                left, x1 = x1, x2
                x2 = left + ratio * (right - left)

        best_frequency = (left + right) / 2.0
        best_error, coefficients = _fit_at_frequency(
            fit_times, fit_values, weights, best_frequency
        )
    except (ArithmeticError, ValueError, OverflowError, np.linalg.LinAlgError) as error:
        return AnalysisResult(FREQUENCY_DETECTION_FAILED, message=str(error))

    duration = end - start
    mean = _integral(fit_times, fit_values) / duration
    mean_square = _integral(fit_times, fit_values * fit_values) / duration
    minimum = float(np.min(fit_values))
    maximum = float(np.max(fit_values))
    status = PASS if best_error <= threshold else DISTORTED
    return AnalysisResult(
        status=status,
        distortion=best_error,
        frequency=best_frequency,
        vpp=maximum - minimum,
        maximum=maximum,
        minimum=minimum,
        mean=mean,
        rms=math.sqrt(max(0.0, mean_square)),
        ac_rms=math.sqrt(max(0.0, mean_square - mean * mean)),
        start_time=start,
        end_time=end,
        num_points=len(fit_times),
    )
