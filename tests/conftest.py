"""Keeps tests away from the user's real files: settings, plans, reports and stored data."""
import hashlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
# Files a test must never change. The session fails if one does (see pytest_sessionfinish).
GUARDED = ["data/simulator_options.json", "data/model_performance.csv", "data/model_lab/queue.json",
           "scenarios/*.json", "config/*/best_model.json"]


def _snapshot():
    files = {}
    for pattern in GUARDED:
        for path in sorted(ROOT.glob(pattern)):
            files[str(path.relative_to(ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def pytest_sessionstart(session):
    session.config.guarded_files = _snapshot()


def pytest_sessionfinish(session, exitstatus):
    before, after = session.config.guarded_files, _snapshot()
    changed = sorted(name for name in set(before) | set(after) if before.get(name) != after.get(name))
    if changed:
        session.config.get_terminal_writer().line(
            "Tests changed the user's real files: " + ", ".join(changed), red=True)
        session.exitstatus = 1


@pytest.fixture(autouse=True)
def user_files_in_tmp(tmp_path, monkeypatch):
    """Point every module's real settings, plans, reports and synthetic store at this test's folder."""
    import scenario_editor
    import stress_test
    import synthetic
    monkeypatch.setattr(synthetic, "SYNTHETIC_DIR", tmp_path / "synthetic")
    monkeypatch.setattr(scenario_editor, "OPTIONS_FILE", tmp_path / "simulator_options.json")
    monkeypatch.setattr(stress_test, "PLAN_FILE", tmp_path / "stress_plan.json")
    monkeypatch.setattr(stress_test, "NIGHTMARE_SET", tmp_path / "nightmares.json")
    monkeypatch.setattr(stress_test, "REPORT_DIR", tmp_path / "stress_reports")
