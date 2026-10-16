import numpy as np
import pytest

cp = pytest.importorskip("cupy")

from gpu_indicators import compute_moving_average, require_devices
from indicators import IndicatorContext, ema


DEVICES = require_devices()


def _cpu_compute(indicator_type, values, periods):
    candles = [
        {"start": index * 900, "open": value, "high": value, "low": value, "close": value, "volume": 1.0}
        for index, value in enumerate(values)
    ]
    context = IndicatorContext(candles, {"ma": {"type": indicator_type, "period": periods}}, 900)
    return context.series("ma")


@pytest.mark.parametrize(
    "indicator_type", ["SMA", "EMA", "WMA", "DEMA", "TEMA", "HMA"]
)
@pytest.mark.parametrize("device_id", DEVICES)
def test_gpu_moving_average_matches_cpu(indicator_type, device_id):
    values = np.asarray([2, 4, 8, 3, 6, 9, 5, 12, 11, 7, 15], dtype=float)
    periods = 3

    actual = compute_moving_average(indicator_type, values, periods, device_id=device_id)
    expected = _cpu_compute(indicator_type, values, periods)

    np.testing.assert_allclose(actual, expected, rtol=1e-10, atol=1e-10, equal_nan=True)


def test_gpu_ema_matches_cpu_with_nan_warmup_and_gaps():
    values = np.asarray([np.nan, np.nan, 2, 4, np.nan, 8, 3, 6, 9, 5], dtype=float)

    actual = compute_moving_average("EMA", values, 3, device_id=DEVICES[0])
    expected = ema(values, 3)

    np.testing.assert_allclose(actual, expected, rtol=1e-10, atol=1e-10, equal_nan=True)


@pytest.mark.parametrize("indicator_type", ["WMA", "HMA"])
def test_gpu_weighted_moving_averages_match_cpu_with_nan_gaps(indicator_type):
    values = np.asarray([np.nan, 2, 4, 8, 3, np.nan, 6, 9, 5, 12, 11, 7, 15], dtype=float)

    actual = compute_moving_average(indicator_type, values, 3, device_id=DEVICES[0])
    expected = _cpu_compute(indicator_type, values, 3)

    np.testing.assert_allclose(actual, expected, rtol=1e-10, atol=1e-10, equal_nan=True)


def test_gpu_device_validation_rejects_unknown_device():
    with pytest.raises(ValueError, match="not available"):
        compute_moving_average("SMA", np.arange(5, dtype=float), 2, device_id=max(DEVICES) + 1)
