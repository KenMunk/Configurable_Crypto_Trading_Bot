"""The live trader's data folder (data_home.py): outside the program, so removing the program keeps your data."""
import csv
import json
import os
import shutil

import pytest

import data_home
import live_trader
import process_control as pc
import recovery
from data_home import adopt_program_folder_data as real_adopt  # conftest stubs it out for every other test
from tests.test_live_trader import _harness


def test_the_folder_is_chosen_by_flag_then_variable_then_remembered_choice_then_default(tmp_path, monkeypatch):
    monkeypatch.delenv(data_home.ENV_VAR)
    default, how = data_home.resolve_data_dir()
    assert default == data_home.default_data_dir() and "standard place" in how
    assert live_trader.ROOT not in default.parents  # never inside the program

    data_home.remember(tmp_path / "chosen")
    assert data_home.resolve_data_dir()[0] == (tmp_path / "chosen").resolve()
    monkeypatch.setenv(data_home.ENV_VAR, str(tmp_path / "from_env"))
    assert data_home.resolve_data_dir()[0] == (tmp_path / "from_env").resolve()
    assert data_home.resolve_data_dir(tmp_path / "flag")[0] == (tmp_path / "flag").resolve()


def test_the_default_is_the_systems_place_for_application_data():
    folder = str(data_home.default_data_dir())
    if os.name == "nt":
        assert folder.startswith(os.environ.get("LOCALAPPDATA", "~")) and "OneDrive" not in folder
    else:
        assert folder.startswith(str(data_home.Path.home()))
    assert "ConfigurableCryptoTradingBot" in folder or "configurable-crypto-trading-bot" in folder


def test_a_new_data_folder_explains_itself(tmp_path):
    paths = data_home.DataPaths(tmp_path / "data").create()
    text = (paths.root / "README.txt").read_text(encoding="utf-8")
    assert "removing, updating or reinstalling the program never" in text and "--set-data-dir" in text
    assert "lose your entire investment" in text


def _deploy(tmp_path, monkeypatch, data):
    """A trader whose program folder is tmp_path and whose data folder is `data`, after a round trip."""
    h = _harness(tmp_path, monkeypatch)
    h.program_backups = len(recovery.bundles(tmp_path / "paper" / "backups"))  # the harness's own first trader's
    trader = live_trader.LiveTrader(h.trader.config, None, h.ledger, h.env, base_dir=tmp_path, now=h.clock,
                                    market_data=h.market, live_broker=h.trader.live_broker, data_dir=data)
    h.trader = trader
    trader.start()
    h.run_to(90)
    return h


def test_backups_and_readable_reports_go_to_the_data_folder_not_the_program(tmp_path, monkeypatch):
    data = tmp_path / "data"
    h = _deploy(tmp_path, monkeypatch, data)
    h.trader.end_session()

    assert recovery.bundles(data / "paper" / "backups")
    assert len(recovery.bundles(tmp_path / "paper" / "backups")) == h.program_backups  # nothing more there
    reports = {path.name for path in (data / "paper" / "reports").glob("*.csv")}
    assert reports == set(recovery.REPORTS)
    with open(data / "paper" / "reports" / "trades.csv", encoding="utf-8-sig", newline="") as handle:
        trades = list(csv.DictReader(handle))
    assert {t["deployment_id"] for t in trades} == {"btc_all_in", "btc_half"}
    assert trades[0]["exit_reason"] == "EXIT_SIGNAL" and float(trades[0]["pnl"]) > 0
    assert trades[0]["exit_utc"].startswith("2024-")  # readable dates, not Unix seconds
    with open(data / "paper" / "reports" / "orders.csv", encoding="utf-8-sig", newline="") as handle:
        assert len(list(csv.DictReader(handle))) == 4
    assert h.trader.snapshot()["data_dir"] == str(data)


def test_a_report_open_in_a_spreadsheet_is_written_next_time(tmp_path, monkeypatch):
    data = tmp_path / "data"
    h = _deploy(tmp_path, monkeypatch, data)
    reports = data / "paper" / "reports"
    with open(reports / "trades.csv", encoding="utf-8"):  # Excel holds the file open like this
        skipped = recovery.export_reports(h.ledger.path, reports)
    assert skipped == (["trades.csv"] if os.name == "nt" else [])  # Windows locks an open file; Unix doesn't
    assert not list(reports.glob("*.tmp"))
    assert recovery.export_reports(h.ledger.path, reports) == []


def test_the_data_survives_removing_the_program(tmp_path, monkeypatch, capsys):
    program, data = tmp_path / "program", tmp_path / "data"
    program.mkdir()
    data.mkdir()
    h = _harness(data, monkeypatch)  # its records and settings are in the data folder
    trader = live_trader.LiveTrader(h.trader.config, None, h.ledger, h.env, base_dir=program, now=h.clock,
                                    market_data=h.market, live_broker=h.trader.live_broker, data_dir=data)
    h.trader = trader
    trader.start()
    h.run_to(61)
    trader.end_session()
    h.ledger.close()
    shutil.rmtree(program)  # the program is removed

    assert live_trader.main(["--data-dir", str(data), "--status"]) == 0
    out = capsys.readouterr().out
    assert f"Data folder: {data}" in out and "PAPER records" in out and "btc_all_in" in out
    paths = data_home.DataPaths(data, "paper")
    assert paths.settings.exists() and recovery.bundles(paths.backups) and (paths.reports / "trades.csv").exists()


def test_set_data_dir_moves_the_data_and_is_remembered(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(data_home.ENV_VAR)
    old, new = tmp_path / "old", tmp_path / "new"
    data_home.remember(old)
    paths = data_home.DataPaths(old).create()
    paths.settings.parent.mkdir(parents=True)
    paths.settings.write_text(json.dumps({"deployments": []}))
    live_trader.TradingLedger(paths.for_mode("live").ledger).close()

    assert live_trader.main(["--set-data-dir", str(new)]) == 0
    assert "Moved" in capsys.readouterr().out
    assert data_home.resolve_data_dir()[0] == new.resolve()
    assert data_home.DataPaths(new, "live").ledger.exists() and not paths.for_mode("live").ledger.exists()


def test_set_data_dir_picks_up_existing_data_after_a_reinstall(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(data_home.ENV_VAR)
    kept = data_home.DataPaths(tmp_path / "kept").create()
    live_trader.TradingLedger(kept.for_mode("paper").ledger).close()  # data from before the program was reinstalled
    assert live_trader.main(["--set-data-dir", str(kept.root)]) == 0
    assert "Using the trading data already in" in capsys.readouterr().out
    assert data_home.resolve_data_dir()[0] == kept.root.resolve()


def test_set_data_dir_waits_for_the_trader_to_stop(tmp_path):
    paths = data_home.DataPaths(data_home.resolve_data_dir()[0]).create()
    pc.write_record(paths.run, pc.WORKER_FILE, urls=[])
    try:
        assert live_trader.main(["--set-data-dir", str(tmp_path / "elsewhere")]) == pc.EXIT_ALREADY_RUNNING
    finally:
        pc.remove_record(paths.run, pc.WORKER_FILE)


def test_moving_refuses_to_merge_two_sets_of_trading_data(tmp_path):
    a, b = data_home.DataPaths(tmp_path / "a").create(), data_home.DataPaths(tmp_path / "b").create()
    live_trader.TradingLedger(a.for_mode("paper").ledger).close()
    live_trader.TradingLedger(b.for_mode("live").ledger).close()
    with pytest.raises(ValueError, match="already holds trading data"):
        data_home.move_contents(a.root, b.root)


def test_data_an_earlier_version_kept_in_the_program_folder_is_moved_once(tmp_path):
    program = tmp_path / "program"
    (program / "ledger").mkdir(parents=True)
    (program / "live_config").mkdir()
    (program / "backups").mkdir()
    live_trader.TradingLedger(program / "ledger" / "trading_ledger.db").close()
    (program / "live_config" / "live_trading.json").write_text("{}")
    (program / "live_config" / "HALT").write_text("")
    (program / "backups" / "live_trader_backup_1.zip").write_bytes(b"zip")
    paths = data_home.DataPaths(tmp_path / "data").create()

    moved = real_adopt(paths, program)
    paper = paths.for_mode("paper")  # a ledger with no deployments yet is paper's
    assert paper.ledger.exists() and paths.settings.exists() and paths.halt.exists()
    assert (paper.backups / "live_trader_backup_1.zip").exists() and len(moved) >= 4
    assert not (program / "ledger" / "trading_ledger.db").exists()
    assert real_adopt(paths, program) == []  # the data folder has data now: never again


def test_the_tax_profile_and_price_history_are_kept_in_the_data_folder(tmp_path, monkeypatch):
    import taxes
    program_tax = tmp_path / "program_tax.json"
    program_tax.write_text('{"filing_status": "single"}')
    monkeypatch.setattr(taxes, "PROFILE_FILE", program_tax)
    (tmp_path / "data").mkdir()
    live_trader.TradingLedger(tmp_path / "data" / "candles.db").close()  # stands in for the simulator's cache
    args = live_trader._parser().parse_args(["--data-dir", str(tmp_path / "trader"), "--base-dir", str(tmp_path)])
    where = live_trader.Locations(args)

    assert live_trader._active_tax_profile(where) == where.paths.tax_profile
    assert taxes.PROFILE_FILE == where.paths.tax_profile and where.paths.tax_profile.read_text() == \
        program_tax.read_text()
    live_trader._seed_price_cache(args, where)
    assert (where.paths.cache / "candles.db").exists()


def test_env_keys_kept_in_the_data_folder_are_used(tmp_path):
    data = data_home.DataPaths(tmp_path / "data").create()
    data.env.write_text("LAN_PORT=9191\n")
    where = live_trader.Locations(live_trader._parser().parse_args(["--data-dir", str(data.root)]))
    assert where.env_file == data.env


# --- the live trader's own configuration file, never the simulator's ----------------------------------------

def test_the_live_trader_uses_its_own_configuration_file_not_the_simulators(tmp_path):
    data = data_home.DataPaths(tmp_path / "data").create()
    where = live_trader.Locations(live_trader._parser().parse_args(["--data-dir", str(data.root)]))
    assert where.env_file == data.root / "live_trader.env"
    assert where.env_file != (live_trader.ROOT / ".env").resolve() and not where.simulator_env
    assert "missing" in live_trader.check_env_file(where) and "--init" in live_trader.check_env_file(where)

    data.env.write_text("FORCE_PAPER_TRADING=true\n")
    assert live_trader.check_env_file(where) is None


def test_the_simulators_configuration_file_is_refused(tmp_path, capsys):
    simulator_env = live_trader.ROOT / ".env"
    args = live_trader._parser().parse_args(["--data-dir", str(tmp_path / "data"), "--env-file", str(simulator_env)])
    where = live_trader.Locations(args)
    assert where.simulator_env and "is the simulator's configuration file" in live_trader.check_env_file(where)

    settings = data_home.DataPaths(tmp_path / "data").settings
    settings.parent.mkdir(parents=True)
    settings.write_text('{"deployments": []}')
    code = live_trader.main(["--data-dir", str(tmp_path / "data"), "--env-file", str(simulator_env), "--once"])
    assert code == pc.EXIT_CONFIG and "simulator's configuration file" in capsys.readouterr().out


def test_init_writes_the_live_traders_own_configuration_template(tmp_path, capsys):
    data = tmp_path / "data"
    assert live_trader.main(["--data-dir", str(data), "--init"]) == 0
    env = data_home.DataPaths(data).env
    text = env.read_text(encoding="utf-8")
    assert "COINBASE_API_KEY_NAME=" in text and "FORCE_PAPER_TRADING=false" in text
    assert "The simulator reads the .env file in the program folder and never this" in text
    env.write_text("COINBASE_API_KEY_NAME=mine\n")
    live_trader.main(["--data-dir", str(data), "--init"])
    assert env.read_text() == "COINBASE_API_KEY_NAME=mine\n"  # never overwritten
    assert "left as it is" in capsys.readouterr().out


def test_a_relative_key_file_starts_from_the_data_folder(tmp_path, monkeypatch):
    import engine
    data = data_home.DataPaths(tmp_path / "data").create()
    data.settings.parent.mkdir(parents=True)
    data.settings.write_text('{"deployments": []}')
    data.env.write_text("COINBASE_KEY_FILE=cdp_api_key.json\n")
    seen = {}

    def stop_at_client(self, dependency_prompt=None, path_prompt=None):
        seen["key_file"] = self.api_config["coinbase"]["key_file"]
        raise SystemExit(0)

    monkeypatch.setattr(engine.QuantitativeTradingEngine, "create_coinbase_client", stop_at_client)
    with pytest.raises(SystemExit):
        live_trader.main(["--data-dir", str(data.root), "--base-dir", str(tmp_path / "program"), "--once"])
    assert seen["key_file"] == str(data.root / "cdp_api_key.json")
