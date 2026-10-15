import json
from pathlib import Path

import pytest

from backtest import resolve_granularity
from model_config import validate_profile
from model_search import (EXAMPLE_SETTINGS, Breeder, Harvest, SettingsError, build_profile, expand_atoms, features,
                          lifts, load_settings, snap_hours)

ROOT = Path(__file__).parent.parent
SETTINGS = load_settings(EXAMPLE_SETTINGS)
TEMPLATE = json.loads((ROOT / SETTINGS["market"]["template_profile"]).read_text(encoding="utf-8"))
OPTIONS = {"gran": SETTINGS["market"]["granularities"], **SETTINGS["options"]}


def _atoms():
    return {atom["expr"]: atom for atom in expand_atoms(SETTINGS["atoms"])}


def _settings(**sections):
    settings = json.loads(json.dumps(SETTINGS))
    for section, values in sections.items():
        settings[section].update(values)
    return settings


def test_the_example_settings_are_complete_and_their_atoms_validate():
    atoms = _atoms()
    assert atoms
    for gran in SETTINGS["market"]["granularities"]:
        _, seconds = resolve_granularity(gran)
        for expr in atoms:
            genome = {"gran": gran, "entry": [expr], "entry_k": 1, "exit": [expr], "exit_k": 0}
            assert validate_profile(build_profile(genome, atoms, TEMPLATE, "BTC-USD"), seconds) == []


def test_missing_settings_are_all_named(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"market": {"product": "DOGE-USD"}}), encoding="utf-8")

    with pytest.raises(SettingsError) as error:
        load_settings(path)

    assert "market.prefix" in str(error.value) and "fitness.drawdown_weight" in str(error.value)
    assert "atoms" in str(error.value)
    with pytest.raises(SettingsError, match="Copy the template"):
        load_settings(tmp_path / "missing.json")


def test_atom_specs_expand_grids_cases_and_indicator_sources():
    atoms = expand_atoms([
        {"family": "cross_{type}", "expr": "{fast} > {slow}",
         "indicators": {"fast": {"type": "{type}", "period": "{fast_period}"},
                        "slow": {"type": "{type}", "period": "{slow_period}"}},
         "grid": {"type": ["SMA", "EMA"]}, "cases": [{"fast_period": "20d", "slow_period": "100d"}]},
        {"family": "obv", "expr": "{obv} > {avg}",
         "indicators": {"obv": {"type": "OBV"}, "avg": {"type": "SMA", "period": "{period}", "source": "{obv}"}},
         "grid": {"period": ["10d"]}},
        {"family": "rsi", "expr": "{rsi} > {level}", "indicators": {"rsi": {"type": "RSI", "period": "{period}"}},
         "grid": {"period": [14], "level": [55]}},
    ])
    by_expr = {atom["expr"]: atom for atom in atoms}

    assert set(by_expr) == {"sma_20d > sma_100d", "ema_20d > ema_100d", "obv > sma_10d_of_obv", "rsi_14 > 55"}
    assert by_expr["ema_20d > ema_100d"]["family"] == "cross_ema"
    assert by_expr["obv > sma_10d_of_obv"]["inds"]["sma_10d_of_obv"]["source"] == "obv"
    assert by_expr["rsi_14 > 55"]["inds"]["rsi_14"]["period"] == 14  # a whole placeholder keeps its type


def test_reference_vote_profiles_are_recovered_as_genomes():
    profile = json.loads(json.dumps(TEMPLATE))
    profile["period_granularity"] = "FIFTEEN_MINUTE"
    profile["indicators"] = {"fast": {"type": "EMA", "period": "20d"}, "slow": {"type": "EMA", "period": "100d"},
                             "lock_sma200": {"type": "SMA", "period": "200d"}}
    profile["strategy"]["entry_signal"] = {"all": [
        "lowest(((((close > fast) + (fast > slow)) >= 2) and (close > lock_sma200)), '72h') >= 1"]}
    profile["strategy"]["exit_signal"] = {"any": ["lowest((((close > fast) + (fast > slow)) <= 0), '12h') >= 1"]}
    harvest = Harvest(expand_atoms(SETTINGS["atoms"]), ["ONE_DAY"], "ONE_DAY")

    genome = harvest.add_profile("ref", profile)

    assert genome["entry"] == ["close > ema_20d", "ema_20d > ema_100d"]
    assert (genome["entry_k"], genome["exit_k"], genome["lock"]) == (2, 0, 200)
    assert (genome["entry_persist_h"], genome["exit_persist_h"]) == (72, 12)
    assert genome["gran"] == "ONE_DAY"  # outside the searched candle sizes, so it takes the fallback
    assert genome["exit_mode"] == "same"


def test_built_profiles_validate_and_persistence_snaps_to_whole_candles():
    settings = _settings(market={"granularities": ["ONE_DAY", "SIX_HOUR"]})
    breeder = Breeder(_atoms(), TEMPLATE, settings, seed=1)
    for _ in range(30):
        repaired = breeder.repair(breeder.fresh())
        if repaired:
            genome, profile = repaired
            _, seconds = resolve_granularity(genome["gran"])
            assert validate_profile(profile, seconds) == []
            assert profile["product_id"] == "BTC-USD"
    assert snap_hours(12, "ONE_DAY") == 0
    assert snap_hours(48, "ONE_DAY") == 48
    assert snap_hours(6, "SIX_HOUR") == 0


def test_mutation_and_crossover_keep_vote_thresholds_in_range():
    breeder = Breeder(_atoms(), TEMPLATE, _settings(breeding={"mutations_per_child": [1, 2, 3]}), seed=2)
    parents = [breeder.fresh() for _ in range(5)]
    for _ in range(50):
        child = breeder.mutate(breeder.crossover(*breeder.random.sample(parents, 2)))
        assert 1 <= child["entry_k"] <= len(child["entry"])
        assert 0 <= child["exit_k"] <= len(child["exit"]) - 1
        assert child["gran"] in SETTINGS["market"]["granularities"]
        assert len(child["entry"]) <= SETTINGS["breeding"]["max_atoms"] + 1


def test_lifts_favour_characteristics_of_the_best_models():
    records = [(["gran=ONE_DAY"], 1.0 + i) for i in range(10)] + [(["gran=ONE_HOUR"], -1.0 - i) for i in range(40)]

    table = lifts(records, top_fraction=0.2, shrink=5.0)

    assert table["gran=ONE_DAY"][0] > 1 > table["gran=ONE_HOUR"][0]


def test_features_bucket_settings_into_the_configured_options():
    genome = {"gran": "ONE_DAY", "entry": ["close > sma_50d"], "entry_k": 1, "exit": ["close > sma_50d"],
              "exit_k": 0, "stop": 0.07, "pos": 1.0}
    feats = features(genome, _atoms(), OPTIONS)
    assert "gran=ONE_DAY" in feats and "stop=0.1" in feats and "pos=1.0" in feats
    assert "entry_family=price_above_sma" in feats


if __name__ == "__main__":
    pytest.main([__file__])
