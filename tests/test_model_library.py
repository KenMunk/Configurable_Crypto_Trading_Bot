"""The model library's currency folders, and the simulator choosing each currency's best model."""

import json
from pathlib import Path

from model_library import currency_folders, discard_folders, organize, profile_paths, read_best, update_best
from simulator import best_by_currency


def _profile(product):
    return {"product_id": product, "strategy": {"entry_signal": {"all": ["close > 1"]}}}


def _write(folder, name, profile):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.json").write_text(json.dumps(profile), encoding="utf-8")


def test_the_library_is_the_top_folder_plus_one_folder_per_currency(tmp_path):
    _write(tmp_path, "old", _profile("BTC-USD"))
    _write(tmp_path / "DOGE-USD", "doge_1", _profile("DOGE-USD"))
    _write(tmp_path / "DOGE-USD" / "discard", "doge_0", _profile("DOGE-USD"))
    _write(tmp_path / ".example", "template", _profile("BTC-USD"))
    _write(tmp_path / "discard", "older", _profile("BTC-USD"))
    (tmp_path / "DOGE-USD" / "best_model.json").write_text("{}", encoding="utf-8")

    assert [folder.name for folder in currency_folders(tmp_path)] == ["DOGE-USD"]
    assert [path.stem for path in profile_paths(tmp_path)] == ["old", "doge_1"]
    assert [folder.relative_to(tmp_path).as_posix() for folder in discard_folders(tmp_path)] == [
        "discard", "DOGE-USD/discard"]


def test_organize_moves_profiles_into_their_currency_folders(tmp_path):
    _write(tmp_path, "btc_1", _profile("BTC-USD"))
    _write(tmp_path, "doge_1", _profile("DOGE-USD"))
    _write(tmp_path / "discard", "doge_0", _profile("DOGE-USD"))
    _write(tmp_path / "DOGE-USD", "clash", _profile("DOGE-USD"))
    _write(tmp_path, "clash", _profile("DOGE-USD"))
    (tmp_path / "notes.json").write_text("{}", encoding="utf-8")

    moved, skipped = organize(tmp_path)

    assert sorted(name for name, _ in moved) == ["btc_1", "doge_0", "doge_1"]
    assert (tmp_path / "BTC-USD" / "btc_1.json").exists() and (tmp_path / "DOGE-USD" / "doge_1.json").exists()
    assert (tmp_path / "DOGE-USD" / "discard" / "doge_0.json").exists()
    assert sorted(name for name, _ in skipped) == ["clash", "notes"]  # name taken / not a profile
    assert (tmp_path / "clash.json").exists() and not (tmp_path / ".models.lock").exists()


def test_a_currencys_best_model_is_replaced_only_by_a_better_one(tmp_path):
    _write(tmp_path / "DOGE-USD", "a", _profile("DOGE-USD"))
    _write(tmp_path / "DOGE-USD", "b", _profile("DOGE-USD"))
    a, b = tmp_path / "DOGE-USD" / "a.json", tmp_path / "DOGE-USD" / "b.json"

    assert update_best("DOGE-USD", "a", a, 10.0, tmp_path)
    assert not update_best("DOGE-USD", "b", b, 8.0, tmp_path)  # worse than the recorded best
    assert read_best(tmp_path)["DOGE-USD"]["model"] == "a"
    assert update_best("DOGE-USD", "a", a, 7.0, tmp_path)  # the recorded model's own newer result counts
    assert update_best("DOGE-USD", "b", b, 8.0, tmp_path)
    record = read_best(tmp_path)["DOGE-USD"]
    assert record["model"] == "b" and record["total_return"] == 8.0 and Path(record["path"]) == b

    b.unlink()
    assert read_best(tmp_path) == {}  # a record whose model is gone doesn't count


def test_the_best_result_of_each_currency():
    results = [{"product_id": "DOGE-USD", "total_return": 5}, {"product_id": "BTC-USD", "total_return": 2},
               {"product_id": "DOGE-USD", "total_return": 9}, {"product_id": "BTC-USD", "total_return": 1}]

    assert best_by_currency(results) == {"DOGE-USD": 2, "BTC-USD": 1}
