import json
from pathlib import Path

import numpy as np
import pytest

from backtest import resolve_granularity
from model_config import CompiledModel, ProfileError, normalize_profile, validate_profile
from rules import Rule, RuleError
from tests.test_indicators import FIFTEEN_MINUTES, _candles

NAMES = ["close", "open", "fast", "slow", "rsi", "macd.line", "macd.signal"]


def _series(**arrays):
    length = len(next(iter(arrays.values())))
    series = {name: np.full(length, np.nan) for name in NAMES}
    series.update({name.replace("__", "."): np.asarray(values, dtype=float) for name, values in arrays.items()})
    return series


def _rule(text):
    return Rule(text, NAMES, FIFTEEN_MINUTES)


def test_comparisons_arithmetic_and_boolean_logic():
    series = _series(close=[1, 2, 3, 4], fast=[1, 3, 2, 5], slow=[2, 2, 2, 2], rsi=[10, 50, 80, 60])

    assert _rule("fast > slow").evaluate(series).tolist() == [False, True, False, True]
    assert _rule("fast > slow and rsi < 70").evaluate(series).tolist() == [False, True, False, True]
    assert _rule("fast > slow or rsi < 20").evaluate(series).tolist() == [True, True, False, True]
    assert _rule("not fast > slow").evaluate(series).tolist() == [True, False, True, False]
    assert _rule("(fast - slow) / slow >= 0.5").evaluate(series).tolist() == [False, True, False, True]
    assert _rule("30 < rsi < 70").evaluate(series).tolist() == [False, True, False, True]


def test_unknown_values_never_trigger_a_rule():
    series = _series(close=[1, 2, 3], fast=[np.nan, 3, 1], slow=[2, 2, 2])

    assert _rule("fast > slow").evaluate(series).tolist() == [False, True, False]
    assert _rule("not fast > slow").evaluate(series).tolist() == [False, False, True]
    assert _rule("fast > slow or close > 0").evaluate(series).tolist() == [True, True, True]


def test_cross_and_history_functions():
    series = _series(close=[10, 11, 9, 12, 12],
                     macd__line=[-1, 1, -1, 1, 2], macd__signal=[0, 0, 0, 0, 0])

    assert _rule("crosses_above(macd.line, macd.signal)").evaluate(series).tolist() == [False, True, False, True, False]
    assert _rule("crosses_below(macd.line, macd.signal)").evaluate(series).tolist() == [False, False, True, False, False]
    assert _rule("close > previous(close)").evaluate(series).tolist() == [False, True, False, True, False]
    assert _rule("change(close, 2) == 1").evaluate(series).tolist() == [False, False, False, True, False]
    assert _rule("close >= highest(close, 3)").evaluate(series).tolist() == [False, False, False, True, True]
    assert _rule("pct_change(close) > 0.3").evaluate(series).tolist() == [False, False, False, True, False]
    assert _rule("abs(macd.line) >= max(1, macd.signal)").evaluate(series).tolist() == [True, True, True, True, True]


def test_durations_work_as_function_periods():
    rule = _rule("close > previous(close, '1h')")
    series = _series(close=list(range(10)))
    assert rule.evaluate(series).tolist() == [False] * 4 + [True] * 6


@pytest.mark.parametrize("text, message", [
    ("fast >", "not a valid formula"),
    ("fsat > slow", "did you mean fast"),
    ("__import__('os').system('echo hi')", "unknown function"),
    ("close.__class__", "unknown name"),
    ("[close][0] > 1", "is not allowed"),
    ("lambda: 1", "is not allowed"),
    ("close > 'high'", "is not a number"),
    ("previous(close, slow)", "must be a number of candles"),
    ("crosses_above(fast)", "takes 2 arguments"),
    ("highest(close, n=3)", "positional arguments only"),
])
def test_unsafe_or_invalid_rules_are_rejected_with_a_clear_message(text, message):
    with pytest.raises(RuleError, match=message):
        _rule(text)


def _profile(**changes):
    profile = {
        "product_id": "BTC-USD",
        "period_granularity": "FIFTEEN_MINUTE",
        "indicators": {
            "fast": {"type": "EMA", "period": "2h"},
            "slow": {"type": "EMA", "period": "12h"},
            "rsi": {"type": "RSI", "period": 14},
        },
        "strategy": {
            "entry_signal": {"all": ["fast > slow", "rsi < 70"]},
            "exit_signal": {"any": ["crosses_below(fast, slow)", "rsi > 80"]},
        },
    }
    profile.update(changes)
    return profile


def test_compiled_model_produces_entry_and_exit_signals():
    candles = _candles()
    model = CompiledModel(_profile(), FIFTEEN_MINUTES)

    series, entry, exit_signal = model.evaluate(candles)

    expected_entry = (series["fast"] > series["slow"]) & (series["rsi"] < 70)
    assert entry.tolist() == expected_entry.tolist()
    assert exit_signal.any() and entry.any()
    assert [meta["label"] for meta in model.indicator_meta()] == ["fast (EMA 8)", "slow (EMA 48)", "rsi (RSI 14)"]


def test_validation_reports_every_problem_at_once():
    profile = _profile(indicators={
        "fast": {"type": "EMA"},
        "close": {"type": "SMA", "period": 5},
        "bands": {"type": "BOLINGER", "period": 20},
        "trend": {"type": "ADX", "period": 14, "smoothing": 3},
    })
    profile["strategy"]["entry_signal"]["all"].append("macd > 0")

    errors = validate_profile(profile, FIFTEEN_MINUTES)

    assert any("'fast' (EMA) needs \"period\"" in error for error in errors)
    assert any("'close' is reserved" in error for error in errors)
    assert any("unknown type 'BOLINGER'" in error for error in errors)
    assert any("does not take smoothing" in error for error in errors)
    assert any("unknown name 'macd'" in error for error in errors)
    assert any("unknown name 'slow'" in error for error in errors)
    with pytest.raises(ProfileError):
        CompiledModel(profile, FIFTEEN_MINUTES)


def test_entry_rules_are_required_but_exit_rules_are_optional():
    profile = _profile()
    profile["strategy"] = {"entry_signal": {}, "exit_signal": {"profit_lock_target": 0.05}}

    errors = validate_profile(profile, FIFTEEN_MINUTES)

    assert errors == ["strategy.entry_signal needs at least one rule in \"all\" or \"any\""]


def test_legacy_profiles_are_translated_to_indicators_and_rules():
    legacy = {
        "indicators": {"fast_window_periods": 30, "slow_window_periods": 180,
                       "emergency_ema_periods": 4, "price_source": "hlc3"},
        "strategy": {
            "entry_signal": {"fast_ema_gt_slow_ema": True, "emergency_ema_gt_fast_ema": True,
                             "minimum_trend_strength": 0.01},
            "exit_signal": {"fast_ema_lt_slow_ema": True, "profit_lock_target": 0.08},
        },
    }

    profile = normalize_profile(legacy)

    assert profile["indicators"]["slow_ema"] == {"type": "EMA", "period": 180, "source": "hlc3"}
    assert profile["strategy"]["entry_signal"]["all"] == [
        "fast_ema > slow_ema", "emergency_ema > fast_ema", "(fast_ema - slow_ema) / slow_ema >= 0.01"]
    assert profile["strategy"]["exit_signal"]["any"] == ["fast_ema < slow_ema"]
    assert legacy["indicators"]["fast_window_periods"] == 30  # the original is untouched


def test_division_by_zero_is_unknown_rather_than_infinite():
    series = _series(close=[1, 2, 3], fast=[1, 1, 1], slow=[0, 1, 0])

    assert _rule("fast / slow > 0").evaluate(series).tolist() == [False, True, False]
    assert _rule("not fast / slow > 0").evaluate(series).tolist() == [False, False, False]


def test_indicator_sources_are_validated_before_running():
    profile = _profile(indicators={
        "a": {"type": "SMA", "period": 5, "source": "b"},
        "b": {"type": "EMA", "period": 5, "source": "a"},
        "smooth": {"type": "SMA", "period": 5, "source": "rsii"},
        "macd": {"type": "MACD"},
        "macd_smooth": {"type": "EMA", "period": 5, "source": "macd"},
        "fast": {"type": "EMA", "period": 2}, "slow": {"type": "EMA", "period": 5},
        "rsi": {"type": "RSI"},
    })

    errors = validate_profile(profile, FIFTEEN_MINUTES)

    assert "Indicator sources form a loop: a -> b -> a" in errors
    assert any("source 'rsii'" in error for error in errors)
    assert any("'macd' has several outputs, so use one of: macd.line" in error for error in errors)
    assert len([error for error in errors if "loop" in error]) == 1


def test_all_example_profiles_are_valid():
    examples = sorted((Path(__file__).parent.parent / "config" / ".example").glob("*.json"))
    assert len(examples) >= 2
    for example in examples:
        profile = json.loads(example.read_text(encoding="utf-8"))
        _, granularity = resolve_granularity(profile["period_granularity"])
        assert validate_profile(profile, granularity) == [], example.name
