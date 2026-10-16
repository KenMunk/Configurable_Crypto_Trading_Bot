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


# --- variety, naming and settings for parallel searches ------------------------------

from model_search import (outperforming_buy_and_hold, select_diverse, signature, similarity, unique_by_behaviour,
                          with_optional_settings, worker_count, write_models)  # noqa: E402


def _scored(key, entry, exits, gran="ONE_DAY", ret=1.0, buy_and_hold=0.0, trades=5, fitness=0.0):
    summary = {"return": ret, "buy_and_hold": buy_and_hold, "max_drawdown": 0.3, "trades": trades}
    return {"key": key, "fitness": fitness, "train": summary, "holdout": summary, "profile": {"key": key},
            "genome": {"gran": gran, "entry": entry, "exit": exits, "confirm": []}}


def test_models_that_trade_identically_count_once():
    best = _scored("a", ["x"], ["y"], fitness=2)
    same_trades = _scored("b", ["x", "z"], ["y"], fitness=1)  # a redundant extra condition
    different = _scored("c", ["x"], ["y"], ret=2.0)

    assert signature(best) == signature(same_trades) != signature(different)
    assert [m["key"] for m in unique_by_behaviour([best, same_trades, different])] == ["a", "c"]


def test_similarity_is_the_share_of_conditions_in_common():
    a = {"gran": "ONE_DAY", "entry": ["p", "q"], "exit": ["r"]}
    assert similarity(a, {"gran": "ONE_DAY", "entry": ["p"], "exit": ["r", "s"]}) == 0.5
    assert similarity(a, {**a, "gran": "SIX_HOUR"}) == 0.0
    assert similarity(a, a) == 1.0


def test_the_kept_models_are_diverse():
    models = [_scored("a", ["p", "q", "r"], ["s"], ret=4.0), _scored("b", ["p", "q", "r"], ["t"], ret=3.0),
              _scored("c", ["u"], ["v"], ret=2.0), _scored("d", ["p", "q", "r"], ["s", "w"], ret=1.0)]

    assert [m["key"] for m in select_diverse(models, 10, max_similarity=0.5)] == ["a", "c"]  # b shares 3 of 5
    assert [m["key"] for m in select_diverse(models, 10, max_similarity=1.0)] == ["a", "b", "c", "d"]
    assert [m["key"] for m in select_diverse(models, 1, max_similarity=1.0)] == ["a"]


def test_buy_and_hold_gate_keeps_only_training_outperformers():
    models = [_scored("beats", ["a"], ["b"], ret=0.2, buy_and_hold=0.1),
              _scored("ties", ["c"], ["d"], ret=0.1, buy_and_hold=0.1),
              _scored("trails", ["e"], ["f"], ret=0.05, buy_and_hold=0.1)]

    assert [model["key"] for model in outperforming_buy_and_hold(models)] == ["beats"]


def test_new_models_take_the_next_unused_names_and_skip_existing_profiles(tmp_path):
    config, log = tmp_path / "config", tmp_path / "model_performance.csv"
    (config / "discard").mkdir(parents=True)
    (config / "eth_002.json").write_text(json.dumps({"key": "old"}), encoding="utf-8")
    (config / "discard" / "eth_007.json").write_text(json.dumps({"key": "dropped"}), encoding="utf-8")
    log.write_text("model\neth_009\n", encoding="utf-8")  # a name only the log remembers
    models = [_scored("new1", [], []), _scored("dropped", [], []), _scored("new2", [], [])]

    written = write_models(models, "eth", config, log)

    assert [m["name"] for m in written] == ["eth_010", "eth_011"]  # "dropped" is already in the discard folder
    assert json.loads((config / "eth_011.json").read_text(encoding="utf-8")) == {"key": "new2"}
    assert not (config / ".models.lock").exists()


def test_parallel_writers_never_share_a_name(tmp_path):
    import threading
    config, log = tmp_path / "config", tmp_path / "model_performance.csv"
    config.mkdir()
    results = []
    threads = [threading.Thread(target=lambda n=n: results.extend(
        m["name"] for m in write_models([_scored(f"{n}-{i}", [], []) for i in range(10)], "doge", config, log)))
               for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == len(set(results)) == 40
    assert len(list(config.glob("doge_*.json"))) == 40


def test_older_settings_files_get_the_new_sections_by_default():
    settings = with_optional_settings({"variety": {"max_similarity": 0.5}})

    assert settings["variety"] == {
        "max_similarity": 0.5, "min_fitness": None, "require_train_outperformance": False}
    assert settings["registry"] == {"enabled": True, "migrants": 10}
    assert settings["ranking"] == {"enabled": True} and settings["goals"] is None


def test_auto_workers_share_the_cpus_between_searches(monkeypatch):
    import parallel_backtest
    monkeypatch.setattr(parallel_backtest, "cpu_count", lambda: 12)
    assert worker_count("auto") == 10
    assert worker_count("auto", instances=3) == 3
    assert worker_count("auto", instances=20) == 1
    assert worker_count(4) == worker_count("4") == 4
    assert worker_count("auto", gpu_slots=2) == 12
    assert worker_count("auto", instances=3, gpu_slots=2) == 4
    assert worker_count(4, gpu_slots=2) == 6


def test_a_search_can_learn_from_chosen_profiles_and_write_to_its_own_folder(tmp_path, monkeypatch):
    import model_search
    monkeypatch.setattr(model_search, "ROOT", tmp_path)
    monkeypatch.setattr(model_search, "CONFIG_DIR", tmp_path / "config")
    (tmp_path / "config").mkdir()
    for name in ("doge_001", "doge_002", "btc_001"):
        (tmp_path / "config" / f"{name}.json").write_text("{}", encoding="utf-8")

    assert [p.stem for p in model_search.reference_paths({})] == ["btc_001", "doge_001", "doge_002"]
    chosen = model_search.reference_paths({"references": ["config/doge_*.json", "config/btc_001.json"]})
    assert [p.stem for p in chosen] == ["doge_001", "doge_002", "btc_001"]
    with pytest.raises(SettingsError, match="matches no profile"):
        model_search.reference_paths({"references": ["config/eth_*.json"]})

    (tmp_path / "config" / "DOGE-USD").mkdir()
    (tmp_path / "config" / "DOGE-USD" / "doge_003.json").write_text("{}", encoding="utf-8")
    assert [p.stem for p in model_search.reference_paths({})] == ["btc_001", "doge_001", "doge_002", "doge_003"]
    assert model_search.models_dir({"market": {"product": "DOGE-USD"}}) == tmp_path / "config" / "DOGE-USD"
    # A reference written before currency folders still finds its model after the move.
    assert [p.stem for p in model_search.reference_paths({"references": ["config/doge_003.json"]})] == ["doge_003"]
    assert [p.stem for p in model_search.reference_paths({"references": ["config/doge_00*.json"]})] == [
        "doge_001", "doge_002"]  # a glob matching at the top level isn't widened
    assert model_search.models_dir({"models_dir": "work/doge"}) == tmp_path / "work" / "doge"


def test_fitness_can_mark_down_too_many_trades_and_is_recomputed_from_summaries():
    from model_search import rescore, summary_fitness
    fitness = {"drawdown_weight": 0.5, "min_trades": 4, "trade_penalty": 0.1}
    summary = {"return": 3.0, "max_drawdown": 0.3, "trades": 20, "years": 2.0}

    base = summary_fitness(summary, fitness)
    assert summary_fitness(summary, {**fitness, "max_trades": 15}) == pytest.approx(base - 0.5)
    assert summary_fitness({**summary, "trades": 2}, fitness) == pytest.approx(
        summary_fitness({**summary, "trades": 4}, fitness) - 0.2)

    scores = rescore({"train": dict(summary, validation=dict(summary, trades=1)), "holdout": dict(summary)}, fitness)
    assert scores["train"]["fitness"] == pytest.approx(base) and scores["holdout"]["fitness"] == pytest.approx(base)
    assert scores["train"]["validation"]["fitness"] < base


def test_selection_needs_both_training_and_validation_and_ties_prefer_simpler_models():
    from model_search import rank_key, selection_fitness
    assert selection_fitness({"train": {"fitness": 0.8}}) == 0.8
    assert selection_fitness({"train": {"fitness": 0.8, "validation": {"fitness": -0.2}}}) == -0.2

    simple = _scored("simple", ["a"], ["b"], fitness=0.8)
    complex_ = _scored("complex", ["a", "c", "d"], ["b"], fitness=0.8)
    fewer_trades = _scored("calm", ["a"], ["b"], fitness=0.8, trades=3)
    better = _scored("better", ["a", "c", "d"], ["b"], fitness=0.9)
    ranked = sorted([complex_, simple, better, fewer_trades], key=rank_key, reverse=True)

    assert [m["key"] for m in ranked] == ["better", "calm", "simple", "complex"]


def test_a_search_built_model_with_a_regime_lock_is_recovered_as_a_reference():
    atoms = _atoms()
    exprs = sorted(atoms)[:3]
    genome = {"gran": "ONE_DAY", "entry": exprs[:2], "entry_k": 2, "exit": exprs[:2], "exit_k": 0, "confirm": [],
              "confirm_k": 0, "lock": 100, "entry_persist_h": 0, "exit_persist_h": 0, "trail": 0, "target": 0,
              "stop": 0, "dd_limit": 0, "pos": 1.0}
    profile = build_profile(genome, atoms, TEMPLATE, "BTC-USD")
    assert "close > sma_100d" in profile["strategy"]["entry_signal"]["all"]

    harvest = Harvest(atoms.values(), ["ONE_DAY"], "ONE_DAY")
    recovered = harvest.add_profile("locked", profile)

    assert harvest.skipped == [] and recovered["lock"] == 100
    assert sorted(recovered["entry"]) == sorted(exprs[:2])


def test_genomes_recorded_before_a_setting_existed_breed_with_it_off():
    breeder = Breeder(_atoms(), TEMPLATE, SETTINGS, seed=3)
    old_a, old_b = breeder.fresh(), breeder.fresh()
    for genome in (old_a, old_b):
        genome.pop("target_trail", None)  # as stored in the registry before target_trail existed

    child = breeder.crossover(old_a, old_b)

    assert child["target_trail"] == 0
    features(child, _atoms(), breeder.options)  # used to fail comparing None with a number
    repaired = breeder.repair({**child, "target_trail": None})
    assert repaired is None or repaired[0]["target_trail"] == 0


def test_a_template_path_from_before_currency_folders_is_found_in_its_folder(tmp_path, monkeypatch):
    import model_search
    monkeypatch.setattr(model_search, "ROOT", tmp_path)
    (tmp_path / "config" / "ETH-USD").mkdir(parents=True)
    (tmp_path / "config" / "ETH-USD" / "eth_template.json").write_text("{}", encoding="utf-8")

    assert model_search.profile_path("config/eth_template.json") == tmp_path / "config" / "ETH-USD" / "eth_template.json"
    assert model_search.profile_path("config/missing.json") == tmp_path / "config" / "missing.json"
