import csv
from datetime import datetime, timezone

import pytest

from backtest import run_backtest
from performance_log import COLUMNS, describe_update, profile_fingerprint, update_performance_log
from tests.test_backtest import _candle, _profile

PRICES = [100] * 6 + [101, 102, 103, 104, 105] + [100, 95, 90, 85]
FIRST = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 10, 15, 9, 30, tzinfo=timezone.utc)


def _run(name, prices=PRICES, profile=None):
    profile = profile or _profile()
    result = run_backtest([_candle(i * 900, price) for i, price in enumerate(prices)], profile)
    result["model_name"], result["granularity"] = name, "FIFTEEN_MINUTE"
    return result, profile


def _rows(path):
    with path.open(newline="", encoding="utf-8") as handle:
        return {row["model"]: row for row in csv.DictReader(handle)}


def test_new_models_are_added_with_their_metrics(tmp_path):
    path = tmp_path / "model_performance.csv"

    outcome = update_performance_log([_run("b"), _run("a")], path, now=FIRST)

    assert outcome == {"added": ["b", "a"], "updated": [], "unchanged": []}
    rows = _rows(path)
    assert list(rows) == ["a", "b"]  # sorted by model
    result, profile = _run("a")
    growth, held = 1 + result["total_return"], 1 + result["buy_and_hold_return"]
    row = rows["a"]
    assert float(row["growth_multiple"]) == pytest.approx(growth)
    assert float(row["buy_and_hold_multiple"]) == pytest.approx(held)
    assert float(row["vs_buy_and_hold_pct"]) == pytest.approx((growth / held - 1) * 100)
    assert row["buy_and_hold_start"] == "1970-01-01 01:00"  # where the 5-candle EMA has warmed up
    assert row["first_buy"] == "1970-01-01 01:30"
    assert row["profile_fingerprint"] == profile_fingerprint(profile)
    assert row["first_recorded_utc"] == row["last_changed_utc"] == "2026-10-14 12:00:00 UTC"
    with path.open(newline="", encoding="utf-8") as handle:
        assert next(csv.reader(handle)) == COLUMNS


def test_an_identical_run_leaves_the_file_untouched(tmp_path):
    path = tmp_path / "model_performance.csv"
    update_performance_log([_run("a")], path, now=FIRST)
    before = path.read_bytes()

    outcome = update_performance_log([_run("a")], path, now=LATER)

    assert outcome == {"added": [], "updated": [], "unchanged": ["a"]}
    assert path.read_bytes() == before


def test_a_run_with_different_results_replaces_only_that_models_row(tmp_path):
    path = tmp_path / "model_performance.csv"
    update_performance_log([_run("a"), _run("b")], path, now=FIRST)

    outcome = update_performance_log([_run("a", PRICES[:-1] + [80])], path, now=LATER)

    assert outcome == {"added": [], "updated": ["a"], "unchanged": []}
    rows = _rows(path)
    assert rows["a"]["buy_and_hold_multiple"] != rows["b"]["buy_and_hold_multiple"]
    assert rows["a"]["first_recorded_utc"] == "2026-10-14 12:00:00 UTC"
    assert rows["a"]["last_changed_utc"] == "2026-10-15 09:30:00 UTC"
    assert rows["b"]["last_changed_utc"] == "2026-10-14 12:00:00 UTC"


def test_a_changed_profile_updates_its_row_even_with_the_same_results(tmp_path):
    path = tmp_path / "model_performance.csv"
    update_performance_log([_run("a")], path, now=FIRST)
    edited = _profile()
    edited["strategy"]["entry_signal"]["description"] = "same rules, new note"

    outcome = update_performance_log([_run("a", profile=edited)], path, now=LATER)

    assert outcome["updated"] == ["a"]
    assert _rows(path)["a"]["profile_fingerprint"] == profile_fingerprint(edited)


def test_an_older_file_with_fewer_columns_is_upgraded(tmp_path):
    path = tmp_path / "model_performance.csv"
    path.write_text("model,growth_multiple\nold,1.5\na,1.0\n", encoding="utf-8")

    outcome = update_performance_log([_run("a")], path, now=FIRST)

    assert outcome["updated"] == ["a"]
    rows = _rows(path)
    assert rows["old"]["growth_multiple"] == "1.5" and rows["old"]["test_start"] == ""
    assert rows["a"]["first_recorded_utc"] == "2026-10-14 12:00:00 UTC"


def test_describe_update():
    outcome = {"added": ["a"], "updated": ["b", "c"], "unchanged": []}
    assert describe_update(outcome, "data/model_performance.csv") == "model_performance.csv: 1 added, 2 updated"
    assert describe_update({"added": [], "updated": [], "unchanged": ["a"]}) == "model_performance.csv: 1 unchanged"
