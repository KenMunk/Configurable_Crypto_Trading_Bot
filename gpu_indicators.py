"""Optional CuPy acceleration for moving-average indicators."""

import importlib.util
import os
import sysconfig
from pathlib import Path

import numpy as np


SUPPORTED_INDICATORS = frozenset({"SMA", "EMA", "WMA", "DEMA", "TEMA", "HMA"})
_DLL_DIRECTORY_HANDLES = {}


def _load_cupy():
    if os.name == "nt":
        roots = {}
        for component in ("cuda_runtime", "cuda_nvrtc"):
            spec = importlib.util.find_spec(f"nvidia.{component}")
            if spec and spec.submodule_search_locations:
                roots[component] = Path(next(iter(spec.submodule_search_locations)))
        if "cuda_runtime" in roots:
            os.environ.setdefault("CUDA_PATH", str(roots["cuda_runtime"]))
        dll_directories = []
        for root in roots.values():
            dll_directory = root / "bin"
            key = str(dll_directory)
            if dll_directory.is_dir():
                dll_directories.append(key)
                if key not in _DLL_DIRECTORY_HANDLES:
                    _DLL_DIRECTORY_HANDLES[key] = os.add_dll_directory(key)
        if dll_directories:
            path_entries = os.environ.get("PATH", "").split(os.pathsep)
            os.environ["PATH"] = os.pathsep.join(
                [*dll_directories, *(entry for entry in path_entries if entry not in dll_directories)]
            )
    import cupy as cp

    return cp


def available_devices():
    """Return visible CUDA device IDs, or an empty list if CuPy is unavailable."""
    try:
        cp = _load_cupy()
    except ImportError:
        return []
    return list(range(cp.cuda.runtime.getDeviceCount()))


def require_devices():
    """Return visible CUDA device IDs or raise an actionable error."""
    devices = available_devices()
    if not devices:
        raise RuntimeError(
            "CUDA indicator computation requires CuPy and a visible CUDA device. "
            "Install the optional dependencies in requirements-gpu.txt and check the NVIDIA driver."
        )
    cp = _load_cupy()
    try:
        cp.cuda.runtime.runtimeGetVersion()
        cp.cuda.nvrtc.getVersion()
    except (OSError, RuntimeError) as exc:
        raise RuntimeError(
            "CUDA is visible, but its runtime or NVRTC kernel compiler could not be loaded. "
            "Reinstall the optional dependencies in requirements-gpu.txt or install a matching CUDA Toolkit."
        ) from exc
    return devices


def _recursive_ema(cp, values, alpha):
    kernel = cp.RawKernel(
        r"""
        extern "C" __global__
        void recursive_ema(const double* input, double* output, const long long length, const double alpha) {
            if (blockIdx.x != 0 || threadIdx.x != 0) return;
            double value = nan("");
            bool initialized = false;
            for (long long i = 0; i < length; ++i) {
                const double current = input[i];
                if (!isnan(current)) {
                    if (!initialized) {
                        value = current;
                        initialized = true;
                    } else {
                        value += alpha * (current - value);
                    }
                }
                output[i] = value;
            }
        }
        """,
        "recursive_ema",
    )
    output = cp.empty_like(values)
    kernel((1,), (1,), (values, output, np.int64(len(values)), np.float64(alpha)))
    return output


def _first_valid(cp, values):
    valid = cp.flatnonzero(~cp.isnan(values))
    return int(valid[0].item()) if valid.size else len(values)


def _rolling_sum(cp, values, periods):
    output = cp.full(len(values), cp.nan, dtype=cp.float64)
    start = _first_valid(cp, values)
    valid = values[start:]
    if len(valid) < periods:
        return output
    offset = valid[0]
    totals = cp.concatenate((cp.zeros(1, dtype=cp.float64), cp.cumsum(valid - offset)))
    output[start + periods - 1:] = totals[periods:] - totals[:-periods] + offset * periods
    return output


def _sma(cp, values, periods):
    return _rolling_sum(cp, values, periods) / periods


def _wma(cp, values, periods):
    output = cp.full(len(values), cp.nan, dtype=cp.float64)
    start = _first_valid(cp, values)
    valid = values[start:]
    if len(valid) < periods:
        return output
    kernel = cp.RawKernel(
        r"""
        extern "C" __global__
        void rolling_wma(const double* input, double* output, const long long length, const long long periods) {
            if (blockIdx.x != 0 || threadIdx.x != 0) return;
            const double divisor = periods * (periods + 1) / 2.0;
            long long valid_count = 0;
            double sum = 0.0;
            double weighted_sum = 0.0;
            for (long long i = 0; i < length; ++i) {
                const double current = input[i];
                if (isnan(current)) {
                    valid_count = 0;
                    sum = 0.0;
                    weighted_sum = 0.0;
                    output[i] = nan("");
                } else if (valid_count < periods) {
                    ++valid_count;
                    sum += current;
                    weighted_sum += valid_count * current;
                    output[i] = valid_count == periods ? weighted_sum / divisor : nan("");
                } else {
                    const double oldest = input[i - periods];
                    weighted_sum = weighted_sum - sum + periods * current;
                    sum += current - oldest;
                    output[i] = weighted_sum / divisor;
                }
            }
        }
        """,
        "rolling_wma",
    )
    kernel(
        (1,),
        (1,),
        (valid, output[start:], np.int64(len(valid)), np.int64(periods)),
    )
    return output


def _ema(cp, values, periods):
    output = _recursive_ema(cp, values, 2.0 / (periods + 1))
    start = _first_valid(cp, values)
    output[start:start + periods - 1] = cp.nan
    return output


def _calculate(cp, indicator_type, values, periods):
    if indicator_type == "SMA":
        return _sma(cp, values, periods)
    if indicator_type == "EMA":
        return _ema(cp, values, periods)
    if indicator_type == "WMA":
        return _wma(cp, values, periods)
    if indicator_type == "DEMA":
        first = _ema(cp, values, periods)
        return 2 * first - _ema(cp, first, periods)
    if indicator_type == "TEMA":
        first = _ema(cp, values, periods)
        second = _ema(cp, first, periods)
        return 3 * first - 3 * second + _ema(cp, second, periods)
    if indicator_type == "HMA":
        half = max(periods // 2, 1)
        root = max(int(np.sqrt(periods)), 1)
        raw = 2 * _wma(cp, values, half) - _wma(cp, values, periods)
        return _wma(cp, raw, root)
    raise ValueError(f"Unsupported GPU indicator {indicator_type!r}")


def compute_moving_average(indicator_type, source, periods, device_id=0):
    """Compute a supported moving average on one CUDA device and return a NumPy array."""
    cp = _load_cupy()

    if indicator_type not in SUPPORTED_INDICATORS:
        raise ValueError(f"Unsupported GPU indicator {indicator_type!r}")
    devices = require_devices()
    if device_id not in devices:
        raise ValueError(f"CUDA device {device_id} is not available; visible devices are {devices}")
    with cp.cuda.Device(device_id):
        values = cp.asarray(source, dtype=cp.float64)
        result = _calculate(cp, indicator_type, values, periods)
        return cp.asnumpy(result)
