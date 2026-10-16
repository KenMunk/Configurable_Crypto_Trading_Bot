import threading

from search_registry import SearchRegistry, context_key


def _evaluation(key, fitness):
    summary = {"return": 1.0, "max_drawdown": 0.3, "trades": 5, "fitness": fitness}
    return {"key": key, "genome": {"gran": "ONE_DAY", "entry": [key]}, "train": summary, "holdout": summary}


def test_context_keys_change_with_any_condition():
    base = dict(product="ETH-USD", windows={"train_start": "2020-01-01"}, fees={"maker": 0.004})
    assert context_key(**base) == context_key(**dict(reversed(list(base.items()))))
    assert context_key(**base) != context_key(**{**base, "fees": {"maker": 0.006}})


def test_evaluations_are_shared_between_searches_in_one_context_only(tmp_path):
    path = tmp_path / "registry.db"
    first, second = SearchRegistry(path, "ctx", "run-1"), SearchRegistry(path, "ctx", "run-2")
    elsewhere = SearchRegistry(path, "other", "run-3")

    first.record([_evaluation("a", 0.2), _evaluation("b", 0.5)])
    first.record([_evaluation("a", 9.9)])  # already stored: kept as it was

    assert second.lookup(["a", "c"]) == {"a": {"train": _evaluation("a", 0.2)["train"],
                                               "holdout": _evaluation("a", 0.2)["holdout"]}}
    assert elsewhere.lookup(["a"]) == {} and second.count() == 2
    assert [r["key"] for r in second.best(5)] == ["b", "a"]  # the other search's, best first
    assert first.best(5) == [] and [r["key"] for r in first.best(1, others_only=False)] == ["b"]
    assert second.best(1)[0]["instance"] == "run-1" and second.best(1)[0]["genome"]["entry"] == ["b"]


def test_many_searches_can_record_at_once(tmp_path):
    path = tmp_path / "registry.db"
    threads = [threading.Thread(target=lambda n=n: SearchRegistry(path, "ctx", f"run-{n}").record(
        [_evaluation(f"{n}-{i}", i) for i in range(50)])) for n in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert SearchRegistry(path, "ctx", "x").count() == 300
    assert len(SearchRegistry(path, "ctx", "x").lookup(f"{n}-{i}" for n in range(6) for i in range(50))) == 300


def test_the_best_can_be_reranked_by_the_caller(tmp_path):
    registry = SearchRegistry(tmp_path / "registry.db", "ctx", "run-1")
    registry.record([_evaluation("a", 0.9), _evaluation("b", 0.5), {**_evaluation("c", 0.1), "fitness": 0.7}])
    other = SearchRegistry(tmp_path / "registry.db", "ctx", "run-2")

    assert [r["key"] for r in other.best(3)] == ["a", "c", "b"]  # the stored "fitness" orders the rows
    assert [r["key"] for r in other.best(2, key=lambda r: -r["train"]["fitness"])] == ["c", "b"]
