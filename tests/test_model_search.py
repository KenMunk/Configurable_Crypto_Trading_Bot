import json
from pathlib import Path

import pytest

from backtest import resolve_granularity
from model_config import validate_profile
from model_search import (GRANULARITIES, Breeder, Harvest, build_library, build_profile, features, lifts,
                          snap_hours)

TEMPLATE = json.loads((Path(__file__).parent.parent / "config" / ".example" / "btc_growth_09.example.json")
                      .read_text(encoding="utf-8"))


def _atoms():
    return {atom["expr"]: atom for atom in build_library()}


def test_every_library_atom_is_valid_on_daily_candles_or_too_short_for_them():
    atoms = _atoms()
    _, seconds = resolve_granularity("ONE_DAY")
    invalid = []
    for expr in atoms:
        genome = {"gran": "ONE_DAY", "entry": [expr], "entry_k": 1, "exit": [expr], "exit_k": 0}
        if validate_profile(build_profile(genome, atoms, TEMPLATE, "DOGE-USD"), seconds):
            invalid.append(expr)
    assert invalid and all("12h" in expr for expr in invalid)  # only the half-day sizes don't fit


def test_reference_vote_profiles_are_recovered_as_genomes():
    profile = json.loads(json.dumps(TEMPLATE))
    profile["indicators"] = {"fast": {"type": "EMA", "period": "20d"}, "slow": {"type": "EMA", "period": "100d"},
                             "lock_sma200": {"type": "SMA", "period": "200d"}}
    profile["strategy"]["entry_signal"] = {"all": [
        "lowest(((((close > fast) + (fast > slow)) >= 2) and (close > lock_sma200)), '72h') >= 1"]}
    profile["strategy"]["exit_signal"] = {"any": ["lowest((((close > fast) + (fast > slow)) <= 0), '12h') >= 1"]}
    harvest = Harvest(build_library())

    genome = harvest.add_profile("ref", profile)

    assert genome["entry"] == ["close > ema_20d", "ema_20d > ema_100d"]
    assert (genome["entry_k"], genome["exit_k"], genome["lock"]) == (2, 0, 200)
    assert (genome["entry_persist_h"], genome["exit_persist_h"]) == (72, 12)


def test_built_profiles_validate_and_persistence_snaps_to_whole_candles():
    atoms = _atoms()
    breeder = Breeder(atoms, TEMPLATE, "DOGE-USD", seed=1)
    for _ in range(30):
        repaired = breeder.repair(breeder.fresh())
        if repaired:
            genome, profile = repaired
            _, seconds = resolve_granularity(genome["gran"])
            assert validate_profile(profile, seconds) == []
            assert profile["product_id"] == "DOGE-USD"
    assert snap_hours(12, "ONE_DAY") == 0
    assert snap_hours(48, "ONE_DAY") == 48
    assert snap_hours(6, "SIX_HOUR") == 0


def test_mutation_and_crossover_keep_vote_thresholds_in_range():
    breeder = Breeder(_atoms(), TEMPLATE, "DOGE-USD", seed=2)
    parents = [breeder.fresh() for _ in range(5)]
    for _ in range(50):
        child = breeder.mutate(breeder.crossover(*breeder.random.sample(parents, 2)))
        assert 1 <= child["entry_k"] <= len(child["entry"])
        assert 0 <= child["exit_k"] <= len(child["exit"]) - 1
        assert child["gran"] in GRANULARITIES


def test_lifts_favour_characteristics_of_the_best_models():
    records = [(["gran=ONE_DAY"], 1.0 + i) for i in range(10)] + [(["gran=ONE_HOUR"], -1.0 - i) for i in range(40)]

    table = lifts(records)

    assert table["gran=ONE_DAY"][0] > 1 > table["gran=ONE_HOUR"][0]


def test_features_describe_settings_and_conditions():
    atoms = _atoms()
    genome = {"gran": "SIX_HOUR", "entry": ["close > sma_50d"], "entry_k": 1, "exit": ["close > sma_50d"],
              "exit_k": 0, "lock": 200, "trail": 0.15}
    feats = features(genome, atoms)
    assert "gran=SIX_HOUR" in feats and "lock=200" in feats and "trail=0.15" in feats
    assert "entry_family=price_above_sma" in feats


if __name__ == "__main__":
    pytest.main([__file__])
