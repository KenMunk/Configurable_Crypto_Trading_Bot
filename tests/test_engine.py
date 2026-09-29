import json
import sqlite3
from types import SimpleNamespace

import pytest
from requests import HTTPError, Response

from engine import (
    QuantitativeStorageController,
    QuantitativeTradingEngine,
    load_environment_config,
)


def test_storage_controller_initializes_database(tmp_path):
    db_path = tmp_path / "ledger" / "fortress.db"

    controller = QuantitativeStorageController(str(db_path))

    assert db_path.exists()
    with sqlite3.connect(str(db_path)) as conn:
        result = conn.execute("PRAGMA journal_mode").fetchone()
        assert result[0].upper() == "WAL"

    conn, cursor = controller.get_connection()
    assert conn is not None
    assert cursor is not None
    conn.close()


def test_engine_ingests_configuration_profiles(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    profile = {
        "ticker": "BTC",
        "product_id": "BTC-USD",
        "strategy_horizon": "MACRO_TREND",
        "execution_platform": "COINBASE_ADVANCED",
        "period_granularity": "FIFTEEN_MINUTE",
        "indicators": {
            "fast_window_periods": 30,
            "slow_window_periods": 180,
            "emergency_ema_periods": 4,
        },
        "safeguards": {
            "live_execution_active": False,
            "use_hard_stop": True,
            "hard_stop_percentage": 0.03,
            "minimum_net_profit_gate": 0.05,
        },
    }

    (config_dir / "btc_macro_horizon.json").write_text(json.dumps(profile), encoding="utf-8")

    engine = QuantitativeTradingEngine(base_directory=str(tmp_path), serial_port="/dev/null")
    engine.ingest_configuration_profiles()

    assert engine.loaded_models["BTC"]["product_id"] == "BTC-USD"
    assert engine.loaded_models["BTC"]["safeguards"]["hard_stop_percentage"] == 0.03


def test_environment_config_loads_api_credentials_from_env_file(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "COINBASE_API_KEY_NAME=organizations/org/apiKeys/key",
                'COINBASE_API_KEY_SECRET="-----BEGIN EC PRIVATE KEY-----\\nabc\\n-----END EC PRIVATE KEY-----\\n"',
                "COINBASE_REQUIRED_PERMISSIONS=view,trade",
                "COINBASE_ALLOW_TRADING=false",
                "COINBASE_BASE_URL=https://api.coinbase.com",
                "KRAKEN_API_KEY=kraken-key",
                "KRAKEN_API_SECRET=kraken-secret",
                "KRAKEN_BASE_URL=https://api.kraken.com",
                "LAN_PORT=9090",
            ]
        ),
        encoding="utf-8",
    )

    config = load_environment_config(str(env_file))

    assert config["coinbase"]["api_key_name"].endswith("/key")
    assert config["coinbase"]["api_key_secret"] == (
        "-----BEGIN EC PRIVATE KEY-----\nabc\n-----END EC PRIVATE KEY-----\n"
    )
    assert config["coinbase"]["required_permissions"] == ["view", "trade"]
    assert config["coinbase"]["allow_trading"] is False
    assert config["kraken"]["base_url"] == "https://api.kraken.com"
    assert config["lan_port"] == 9090


@pytest.mark.parametrize(
    "key_data",
    [
        {"name": "organizations/org/apiKeys/key", "privateKey": "pem-secret"},
        {"api_key": "organizations/org/apiKeys/key", "api_secret": "pem-secret"},
    ],
)
def test_engine_loads_coinbase_credentials_from_key_file(tmp_path, key_data):
    (tmp_path / "cdp_api_key.json").write_text(json.dumps(key_data), encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text("COINBASE_KEY_FILE=cdp_api_key.json\n", encoding="utf-8")

    engine = QuantitativeTradingEngine(
        base_directory=str(tmp_path), serial_port="/dev/null", env_file=str(env_file)
    )

    assert engine.load_coinbase_credentials() == ("organizations/org/apiKeys/key", "pem-secret")


def test_engine_reports_missing_coinbase_credentials(tmp_path, caplog):
    env_file = tmp_path / ".env"
    env_file.write_text("COINBASE_ENABLED=true\n", encoding="utf-8")

    engine = QuantitativeTradingEngine(
        base_directory=str(tmp_path), serial_port="/dev/null", env_file=str(env_file)
    )

    with caplog.at_level("ERROR", logger="quant_trading_bot"):
        result = engine.authenticate_coinbase()

    assert result["authenticated"] is False
    assert "required" in result["message"]
    assert result["account_count"] == 0
    assert result["checked_at_utc"] > 0
    assert "Authentication failed" in caplog.text


def _engine_with_client(tmp_path, monkeypatch, client):
    engine = QuantitativeTradingEngine(base_directory=str(tmp_path), serial_port="/dev/null")
    monkeypatch.setattr(engine, "create_coinbase_client", lambda *args, **kwargs: client)
    return engine


def test_engine_authenticates_coinbase_through_rest_client(tmp_path, monkeypatch):
    client = SimpleNamespace(
        get_accounts=lambda: SimpleNamespace(accounts=[object(), object()])
    )
    engine = _engine_with_client(tmp_path, monkeypatch, client)

    result = engine.authenticate_coinbase()

    assert result["authenticated"] is True
    assert result["account_count"] == 2
    assert engine.last_coinbase_authentication is result


def test_engine_reports_outbound_ip_for_unauthorized_response(tmp_path, monkeypatch):
    def unauthorized():
        response = Response()
        response.status_code = 401
        raise HTTPError("401 Client Error: Unauthorized", response=response)

    engine = _engine_with_client(tmp_path, monkeypatch, SimpleNamespace(get_accounts=unauthorized))
    monkeypatch.setattr(engine, "_get_outbound_ip", lambda: "203.0.113.10")

    result = engine.authenticate_coinbase()

    assert result["authenticated"] is False
    assert "HTTP Error 401" in result["message"]
    assert result["outbound_ip"] == "203.0.113.10"
    assert "203.0.113.10" in result["message"]


def test_engine_creates_rest_client_with_configured_host(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "COINBASE_API_KEY_NAME=organizations/org/apiKeys/key",
                "COINBASE_API_KEY_SECRET=pem-secret",
                "COINBASE_BASE_URL=https://api.coinbase.com",
            ]
        ),
        encoding="utf-8",
    )
    engine = QuantitativeTradingEngine(
        base_directory=str(tmp_path), serial_port="/dev/null", env_file=str(env_file)
    )
    monkeypatch.setattr("engine.ensure_coinbase_sdk", lambda *args, **kwargs: True)

    client = engine.create_coinbase_client()

    assert client.api_key == "organizations/org/apiKeys/key"
    assert client.base_url == "api.coinbase.com"


def test_engine_exposes_tax_allocation_schedule_from_model_config(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    model = {
        "ticker": "BTC",
        "product_id": "BTC-USD",
        "tax_allocation": {
            "enabled": True,
            "rate": 0.10,
            "schedule": {
                "interval": "MONTHLY",
                "day_of_month": 1,
                "time_utc": "00:00:00",
            },
        },
    }
    (config_dir / "btc_tax_model.json").write_text(json.dumps(model), encoding="utf-8")

    engine = QuantitativeTradingEngine(base_directory=str(tmp_path), serial_port="/dev/null")
    engine.ingest_configuration_profiles()

    btc_config = engine.get_model_configuration("BTC")
    assert btc_config["tax_allocation"]["rate"] == 0.10
    assert btc_config["tax_allocation"]["schedule"]["interval"] == "MONTHLY"
    assert btc_config["tax_allocation"]["schedule"]["day_of_month"] == 1


def test_engine_accepts_yearly_tax_allocation_schedule(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    model = {
        "ticker": "BTC",
        "product_id": "BTC-USD",
        "tax_allocation": {
            "enabled": True,
            "rate": 0.10,
            "schedule": {
                "interval": "YEARLY",
                "month": 12,
                "day_of_month": 31,
                "time_utc": "00:00:00",
            },
        },
    }
    (config_dir / "btc_yearly_tax_model.json").write_text(json.dumps(model), encoding="utf-8")

    engine = QuantitativeTradingEngine(base_directory=str(tmp_path), serial_port="/dev/null")
    engine.ingest_configuration_profiles()

    btc_config = engine.get_model_configuration("BTC")
    assert btc_config["tax_allocation"]["schedule"]["interval"] == "YEARLY"
    assert btc_config["tax_allocation"]["schedule"]["month"] == 12
    assert btc_config["tax_allocation"]["schedule"]["day_of_month"] == 31


def test_engine_authenticates_a_supplied_client_without_creating_one(tmp_path, monkeypatch):
    engine = QuantitativeTradingEngine(base_directory=str(tmp_path), serial_port="/dev/null")

    def fail_create(*args, **kwargs):
        raise AssertionError("should reuse the supplied client")

    monkeypatch.setattr(engine, "create_coinbase_client", fail_create)
    client = SimpleNamespace(get_accounts=lambda: SimpleNamespace(accounts=[object()]))

    result = engine.authenticate_coinbase(client=client)

    assert result["authenticated"] is True
    assert result["account_count"] == 1
