import json
import sqlite3
from urllib.parse import parse_qs, urlsplit

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
                "COINBASE_API_KEY=coinbase-key",
                "COINBASE_API_SECRET=coinbase-secret",
                "COINBASE_PASSPHRASE=coinbase-passphrase",
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

    assert config["coinbase"]["api_key"] == "coinbase-key"
    assert config["coinbase"]["passphrase"] == "coinbase-passphrase"
    assert config["kraken"]["base_url"] == "https://api.kraken.com"
    assert config["lan_port"] == 9090


def test_environment_config_loads_configurable_coinbase_oauth_scopes(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "COINBASE_AUTH_MODE=oauth",
                "COINBASE_OAUTH_CLIENT_ID=client-id",
                "COINBASE_OAUTH_REDIRECT_URI=http://localhost:8080/oauth/callback",
                "COINBASE_OAUTH_SCOPES=view,trade",
            ]
        ),
        encoding="utf-8",
    )

    config = load_environment_config(str(env_file))

    assert config["coinbase"]["auth_mode"] == "oauth"
    assert config["coinbase"]["oauth_client_id"] == "client-id"
    assert config["coinbase"]["oauth_scopes"] == ["view", "trade"]


def test_environment_config_loads_ed25519_secret_api_key_settings(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "COINBASE_AUTH_MODE=secret_api_key",
                "COINBASE_API_KEY_NAME=organizations/org/apiKeys/key",
                "COINBASE_API_KEY_SECRET=ed25519-secret",
                "COINBASE_REQUIRED_PERMISSIONS=wallet:accounts:read,wallet:buys:create",
                "COINBASE_ALLOW_TRADING=false",
            ]
        ),
        encoding="utf-8",
    )

    config = load_environment_config(str(env_file))

    assert config["coinbase"]["auth_mode"] == "secret_api_key"
    assert config["coinbase"]["api_key_name"].endswith("/key")
    assert config["coinbase"]["api_key_secret"] == "ed25519-secret"
    assert config["coinbase"]["required_permissions"] == [
        "wallet:accounts:read",
        "wallet:buys:create",
    ]
    assert config["coinbase"]["allow_trading"] is False


def test_engine_reports_missing_secret_api_key_credentials(tmp_path, caplog):
    env_file = tmp_path / ".env"
    env_file.write_text("COINBASE_AUTH_MODE=secret_api_key\n", encoding="utf-8")

    engine = QuantitativeTradingEngine(
        base_directory=str(tmp_path), serial_port="/dev/null", env_file=str(env_file)
    )

    with caplog.at_level("ERROR", logger="quant_trading_bot"):
        result = engine.authenticate_coinbase()

    assert result["authenticated"] is False
    assert "required" in result["message"]
    assert result["auth_mode"] == "secret_api_key"
    assert result["account_count"] == 0
    assert result["checked_at_utc"] > 0
    assert "Authentication failed" in caplog.text


def test_engine_reports_outbound_ip_for_unauthorized_response(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "COINBASE_AUTH_MODE=secret_api_key",
                "COINBASE_API_KEY_NAME=organizations/org/apiKeys/key",
                "COINBASE_API_KEY_SECRET=ed25519-secret",
            ]
        ),
        encoding="utf-8",
    )
    engine = QuantitativeTradingEngine(
        base_directory=str(tmp_path), serial_port="/dev/null", env_file=str(env_file)
    )

    monkeypatch.setattr(engine, "create_coinbase_jwt", lambda *args, **kwargs: "test-token")
    monkeypatch.setattr(engine, "_get_outbound_ip", lambda: "203.0.113.10")

    from urllib.error import HTTPError

    def unauthorized_request(*args, **kwargs):
        raise HTTPError(
            url="https://api.coinbase.com/api/v3/brokerage/accounts",
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=None,
        )

    monkeypatch.setattr("engine.urlopen", unauthorized_request)
    result = engine.authenticate_coinbase()

    assert result["authenticated"] is False
    assert result["outbound_ip"] == "203.0.113.10"
    assert "203.0.113.10" in result["message"]


def test_engine_builds_coinbase_oauth_authorization_url(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "COINBASE_OAUTH_CLIENT_ID=client-id",
                "COINBASE_OAUTH_REDIRECT_URI=http://localhost:8080/oauth/callback",
                "COINBASE_OAUTH_SCOPES=view,trade",
            ]
        ),
        encoding="utf-8",
    )

    engine = QuantitativeTradingEngine(
        base_directory=str(tmp_path), serial_port="/dev/null", env_file=str(env_file)
    )
    authorization_url = engine.create_coinbase_oauth_authorization_url()
    query = parse_qs(urlsplit(authorization_url).query)

    assert urlsplit(authorization_url).path == "/oauth2/auth"
    assert query["client_id"] == ["client-id"]
    assert query["scope"] == ["view,trade"]
    assert query["state"]


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
