"""Recovering after a power cut, a crash or a move to another machine (recovery.py and live_trader.py)."""
import json
import zipfile
from types import SimpleNamespace

import pytest

import live_trader
import process_control as pc
import recovery
from tests.test_live_trader import HOUR, T0, _closed, _harness, _hourly


def _unclean_restart(h, hour):
    """The machine lost power while the trader ran; it is started again at `hour` (no clean stop was recorded)."""
    h.clock.t = T0 + hour * HOUR + 31
    h.price_at(hour)
    h.trader = h.make()
    h.trader.start()


# --- outages ------------------------------------------------------------------------------------------------

def test_an_outage_is_detected_measured_and_reported(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    h.run_to(61)
    last_seen = h.ledger.get_meta("session")["heartbeat"]

    _unclean_restart(h, 64)

    outage = h.ledger.get_meta("outages")[-1]
    assert outage["last_seen"] == last_seen and outage["seconds"] == h.clock.t - last_seen
    assert any("resuming after an unexpected stop" in e["message"] for e in h.ledger.events())
    snapshot = h.trader.snapshot()["recovery"]
    assert snapshot["outage"]["seconds"] == outage["seconds"] and snapshot["last_backup"]
    assert "Last outage: offline" in live_trader.describe_recovery(h.ledger)


def test_a_clean_stop_is_not_an_outage_and_is_backed_up(tmp_path, monkeypatch):
    import threading
    h = _harness(tmp_path, monkeypatch)
    h.trader.end_session()  # the harness's first trader stops cleanly too
    stop = threading.Event()
    stop.set()
    h.trader = h.make()
    h.trader.run(stop)  # starts, then stops cleanly at once

    session = h.ledger.get_meta("session")
    assert session["clean"] and session["ended"]
    assert len(recovery.bundles(tmp_path / "paper" / "backups")) >= 1
    h.trader = h.make()
    h.trader.start()
    assert h.ledger.get_meta("outages") is None and h.trader.snapshot()["recovery"]["outage"] is None


def test_a_move_to_another_machine_is_noted(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    monkeypatch.setattr(live_trader.socket, "gethostname", lambda: "new-node")
    _unclean_restart(h, 58)
    assert h.trader.snapshot()["recovery"]["moved_from"] not in (None, "new-node")
    assert any("now running on new-node" in e["message"] for e in h.ledger.events())


def test_an_exit_missed_while_offline_is_sold_on_resuming(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    h.run_to(61)  # both BTC models bought at candle 60
    assert h.row("btc_all_in")["units"] > 0

    _unclean_restart(h, 85)  # offline through the fall: the models exited around candle 70

    for deployment_id in ("btc_all_in", "btc_half"):
        trade, = h.ledger.trades(deployment_id)
        assert trade["exit_reason"] == "EXIT_SIGNAL" and _closed(h.row(deployment_id))


def test_an_entry_missed_while_offline_is_skipped_not_chased(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    _unclean_restart(h, 64)  # the models entered at candle 60, while it was down
    assert h.row("btc_all_in")["units"] == 0
    assert any("too long ago" in e["message"] for e in h.ledger.events())


# --- profiles on a new machine ----------------------------------------------------------------------------

def test_a_deployment_trades_its_stored_profile_when_the_file_is_gone(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    h.run_to(61)
    (tmp_path / "btc_all_in.json").unlink()

    _unclean_restart(h, 62)

    deployment = h.trader.deployments["btc_all_in"]
    assert "btc_all_in" not in h.trader.blocked
    assert any("trades the copy stored in the ledger" in note for note in deployment.notes)
    assert deployment.profile["capital_allocation"]["starting_capital"] == 1000
    h.run_to(90)
    assert len(h.ledger.trades("btc_all_in")) == 1  # it carried on and exited as before


def test_a_new_deployment_still_needs_its_profile_file(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch, deployments=[{"id": "fresh", "capital": 100, "profile": "nowhere.json"}],
                 profiles={})
    assert "fresh" in h.trader.blocked and h.ledger.deployment("fresh") is None


# --- backups, restoring and moving -----------------------------------------------------------------------

def _bundle(h, tmp_path, name="move.zip"):
    destination = tmp_path / name
    manifest = recovery.write_bundle(destination, tmp_path, h.ledger.path, h.config_path, h.clock.t,
                                     tax_profile=tmp_path / "tax" / "tax_profile.json")
    return destination, manifest


def test_a_bundle_holds_what_a_new_machine_needs_and_never_the_keys(tmp_path, monkeypatch):
    (tmp_path / "tax").mkdir()
    (tmp_path / "tax" / "tax_profile.json").write_text('{"filing_status": "single"}')
    (tmp_path / ".env").write_text("COINBASE_API_KEY_SECRET=secret")
    h = _harness(tmp_path, monkeypatch)
    h.run_to(61)
    (h.config_path.parent / "HALT").write_text("")
    path, manifest = _bundle(h, tmp_path)

    names = set(zipfile.ZipFile(path).namelist())
    assert {"manifest.json", "ledger.db", "live_config/live_trading.json", "live_config/HALT",
            "tax/tax_profile.json", "profiles/btc_all_in.json", "profiles/eth_flat.json"} <= names
    assert not any(".env" in name for name in names)
    assert {d["id"] for d in manifest["deployments"]} == {"btc_all_in", "btc_half", "eth_flat"}
    assert b"secret" not in path.read_bytes()


def test_restoring_on_a_new_machine_carries_on_where_it_stopped(tmp_path, monkeypatch):
    old = tmp_path / "old"
    old.mkdir()
    h = _harness(old, monkeypatch)
    h.run_to(61)
    held = h.row("btc_all_in")["units"]
    bundle, _ = _bundle(h, tmp_path)

    new = tmp_path / "new"
    new.mkdir()
    (new / "live_config").mkdir()
    (new / "live_config" / "live_trading.json").write_text("{}")  # something already there is kept aside
    code = live_trader.main(["--data-dir", str(new), "--config", str(new / "live_config" / "live_trading.json"),
                             "--restore", str(bundle)])
    assert code == 0
    assert list((new / "paper" / "backups").glob("replaced_*/live_config/live_trading.json"))
    restored = live_trader.TradingLedger(new / "paper" / "ledger" / "trading_ledger.db")
    assert restored.deployment("btc_all_in")["units"] == pytest.approx(held)

    # The settings name the old machine's profile paths; on the new one the stored copies carry on.
    config = live_trader.load_live_config(new / "live_config" / "live_trading.json")
    moved = live_trader.LiveTrader(config, None, restored, h.env, base_dir=new, now=h.clock,
                                   market_data=h.market, live_broker=h.live_broker or SimpleNamespace())
    (old / "btc_all_in.json").unlink()
    moved.start()
    assert "btc_all_in" in moved.deployments and moved.deployments["btc_all_in"].in_position


def test_restore_refuses_while_the_trader_runs_and_rejects_bad_bundles(tmp_path, capsys):
    run = pc.run_dir(tmp_path)
    pc.write_record(run, pc.WORKER_FILE, urls=[])
    try:
        assert live_trader.main(["--data-dir", str(tmp_path), "--restore", "x.zip"]) == pc.EXIT_ALREADY_RUNNING
    finally:
        pc.remove_record(run, pc.WORKER_FILE)
    (tmp_path / "junk.zip").write_bytes(b"not a zip")
    assert live_trader.main(["--data-dir", str(tmp_path), "--restore", str(tmp_path / "junk.zip")]) == 1
    assert "not a live trader recovery bundle" in capsys.readouterr().out


def test_backups_run_on_schedule_and_old_ones_are_pruned(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    h.trader.settings.update(backup_hours=6, backup_keep=3)
    for hour in range(56, 90):
        h.run_to(hour)
    found = recovery.bundles(tmp_path / "paper" / "backups")
    assert len(found) == 3  # one at start-up plus one every 6 hours, pruned to the newest 3
    assert recovery.read_manifest(found[0])["created"] > recovery.read_manifest(found[-1])["created"]
    assert h.ledger.get_meta("last_backup")["path"] == str(found[0])


def test_the_backup_command_writes_a_bundle(tmp_path, monkeypatch, capsys):
    h = _harness(tmp_path, monkeypatch)
    assert live_trader.main(["--data-dir", str(tmp_path), "--config", str(h.config_path), "--backup"]) == 0
    assert "PAPER recovery bundle written" in capsys.readouterr().out
    assert recovery.bundles(tmp_path / "paper" / "backups")


# --- what must stop it --------------------------------------------------------------------------------------

def test_a_damaged_ledger_is_refused_with_the_way_back(tmp_path, monkeypatch, capsys):
    h = _harness(tmp_path, monkeypatch)
    h.run_to(58)
    h.ledger.close()
    ledger = tmp_path / "paper" / "ledger" / "trading_ledger.db"
    data = bytearray(ledger.read_bytes())
    data[:100] = b"\0" * 100  # a power cut that wrecked the file's header
    ledger.write_bytes(bytes(data))

    assert recovery.integrity_problem(ledger)
    # Its own configuration file, and a view-only key, without asking Coinbase.
    (tmp_path / "live_trader.env").write_text("FORCE_PAPER_TRADING=false\n")
    import engine
    monkeypatch.setattr(engine.QuantitativeTradingEngine, "create_coinbase_client", lambda self, *a, **k: None)
    monkeypatch.setattr(live_trader, "determine_mode", lambda *a, **k: ("paper", {"reason": "view-only key",
                                                                                   "can_transfer": False}))
    code = live_trader.main(["--data-dir", str(tmp_path), "--once"])
    assert code == pc.EXIT_LEDGER == 4 and code in pc.FATAL_EXITS
    out = capsys.readouterr().out
    assert "damaged" in out and "--restore" in out and "live_trader_backup_" in out


def test_trading_waits_for_a_correct_clock(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch)
    trader = h.make()
    trader.client = SimpleNamespace(get_unix_time=lambda: {"epoch_seconds": str(h.clock.t - 3600)})
    with pytest.raises(RuntimeError, match="clock is \\+3600s off"):
        trader.start()
    trader.client = SimpleNamespace(get_unix_time=lambda: {"epoch_seconds": str(h.clock.t + 5)})
    trader.start()  # within the tolerance


def test_a_missing_tax_profile_is_warned_about(tmp_path, monkeypatch):
    import taxes
    taxed = _hourly(1.0)
    taxed["friction_model"].update(tax_model="progressive", tax_buffer_rate=0.3)
    monkeypatch.setattr(taxes, "PROFILE_FILE", tmp_path / "missing_tax_profile.json")
    h = _harness(tmp_path, monkeypatch, deployments=[{"id": "taxed", "capital": 100}], profiles={"taxed": taxed})
    assert any("tax reserves for taxed use the example" in e["message"] for e in h.ledger.events())


def test_the_supervisor_does_not_restart_on_a_damaged_ledger(tmp_path):
    log = []
    worker = SimpleNamespace(pid=1, poll=lambda: pc.EXIT_LEDGER)
    assert pc.supervise(lambda: worker, pc.run_dir(tmp_path), log.append, sleep=lambda s: None) == pc.EXIT_LEDGER
    assert "the ledger is damaged" in log[-1]


def test_bundles_from_a_newer_version_are_refused(tmp_path):
    path = tmp_path / "future.zip"
    with zipfile.ZipFile(path, "w") as bundle:
        bundle.writestr("manifest.json", json.dumps({"bundle_version": recovery.BUNDLE_VERSION + 1}))
        bundle.writestr("ledger.db", b"")
    with pytest.raises(recovery.RecoveryError, match="newer version"):
        recovery.read_manifest(path)
