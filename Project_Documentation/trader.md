# Trader Guide (engine.py)

`engine.py` is the trader: the headless runtime meant to run around the clock on a small, always-on machine on your local network. That machine is the "headless node" in the [quant infrastructure blueprint](../README.md#the-quant-infrastructure-blueprint).

> **Trading lives in `live_trader.py`.** It builds on this engine (settings, Coinbase client, dashboard) and runs any number of models on any number of currencies. See the [Live trading guide](live_trading.md). This page covers the engine foundation it uses.
>
> **The engine on its own does not trade.**
>
> Today the engine does four things:
>
> - loads its settings and model profiles
> - checks your Coinbase access
> - opens its ledger and serial link
> - reports its status over HTTP
>
> It does not stream prices, evaluate strategies live or place orders. No setting in this project can make it place an order, because that code does not exist yet. To test strategies, use the [simulator](simulator.md).

---

## Contents

1. [What the engine does today](#1-what-the-engine-does-today)
2. [Setting up the .env file](#2-setting-up-the-env-file)
3. [Creating a Coinbase API key](#3-creating-a-coinbase-api-key)
4. [Running the engine](#4-running-the-engine)
5. [The status dashboard](#5-the-status-dashboard)
6. [Model profiles in the engine](#6-model-profiles-in-the-engine)
7. [The ledger database](#7-the-ledger-database)
8. [Serial interlock and node roles](#8-serial-interlock-and-node-roles)
9. [GPU simulation helper](#9-gpu-simulation-helper)
10. [Trading safety switches](#10-trading-safety-switches)
11. [Using the engine from Python](#11-using-the-engine-from-python)
12. [Troubleshooting](#12-troubleshooting)

---

## 1. What the engine does today

| Capability | Status |
|---|---|
| Load settings and secrets from `.env` | Working |
| Load model profiles from `<base directory>/config/` | Working; the profiles are not validated |
| Open the SQLite ledger with low-wear settings | Working; there are no tables yet |
| Read-only Coinbase authentication check | Working |
| Status dashboard (JSON over HTTP) | Working |
| Serial link to a partner node | Opens the port only; there is no protocol yet |
| GPU moving average with a mixed-GPU guard | Working; nothing uses it yet |
| Live market data, live signal evaluation, order placement, trade records | Not built yet |

At start-up, `initialize_node_cluster()` runs four steps in order:

1. It logs which Python interpreter is running.
2. It loads the model profiles.
3. It tries to open the serial link.
4. It starts the dashboard in a background thread.

---

## 2. Setting up the .env file

The engine and the simulator both read secrets and runtime settings from a `.env` file in the **current working directory**. The live trader does not: it has its own configuration file, `live_trader.env` in its data folder (see [Its own configuration file](live_trading.md#its-own-configuration-file)), so the simulator's key never decides whether it trades real money. Git ignores `.env`, so your keys stay out of the repository. Create it from the template:

```bash
cp .env.example .env
```

In PowerShell: `Copy-Item .env.example .env`

### Settings

| Setting | Default | Used by | Meaning |
|---|---|---|---|
| `COINBASE_API_KEY_NAME` | empty | Engine, simulator | The key name, in the form `organizations/{org_id}/apiKeys/{key_id}`. |
| `COINBASE_API_KEY_SECRET` | empty | Engine, simulator | The private key: a base64 Ed25519 secret, or an ECDSA PEM key on one line with its line breaks written as `\n`, inside double quotes. |
| `COINBASE_KEY_FILE` | empty | Engine, simulator | The path to a key JSON file, used only when the name or the secret above is empty. Both values then come from the file. A relative path resolves against the engine's base directory: the current directory for the simulator, but `quant_system/` for `python engine.py`. An absolute path is safest. |
| `COINBASE_REQUIRED_PERMISSIONS` | `view` | Reports | A comma-separated list of the permissions you expect the key to have. It is documentation only: authentication results repeat it, but it grants nothing. |
| `COINBASE_BASE_URL` | `https://api.coinbase.com` | Engine, simulator | The API host. |
| `COINBASE_ALLOW_TRADING` | `false` | Not used | No longer gates trading: the live trader trades live when its API key can trade. |
| `COINBASE_ENABLED` | `false` | Not used yet | Loaded, but nothing checks it. Authentication works either way. |
| `KRAKEN_API_KEY`, `KRAKEN_API_SECRET`, `KRAKEN_BASE_URL`, `KRAKEN_ENABLED` | empty, empty, `https://api.kraken.com`, `false` | Not used yet | Placeholders for a future Kraken connection. |
| `LAN_PORT` | `8080` | Your launcher | Loaded, but `python engine.py` always uses port 8080. The [launcher script](#keeping-the-engine-running) below uses this value. It must be a whole number: any other value stops both the engine and the simulator from starting. |
| `DASHBOARD_HOST`, `DASHBOARD_PASSWORD`, `DASHBOARD_ALLOW_PUBLIC` | `0.0.0.0`, empty, `false` | Engine, live trader | Who can reach the dashboard and the live trader's web portal; see [the web portal](live_trading.md#who-can-see-it). |
| `LIVE_EXECUTION_ACTIVE` | `false` | Not used | No longer gates trading (see `FORCE_PAPER_TRADING`). |
| `FORCE_PAPER_TRADING` | `false` | Live trader, in `live_trader.env` only | `true` paper-trades even with an API key that can trade. |

### How the file is read

- Each line holds one `KEY=value`. Blank lines, lines starting with `#` and lines without `=` are skipped. Keys are not case-sensitive.
- Surrounding single or double quotes are removed from values.
- `true`, `false`, `yes` and `no` become booleans, in any case. Numbers become numbers.
- For the on/off settings, `1` and any other non-empty text also mean on. `0` and an empty value mean off.
- **Comments must be on their own line.** An inline comment becomes part of the value, which can quietly invert a setting. For example, `COINBASE_ALLOW_TRADING=false  # off` reads as **true**, and `LAN_PORT=8080 # dashboard` stops the engine and the simulator from starting.
- Unknown keys are ignored. Without a `.env` file, the defaults apply and no credentials are set.

### Using a key file instead

`COINBASE_KEY_FILE` can point to a JSON file. The file must hold the key name as `name` or `api_key`, and the private key as `privateKey` or `api_secret`:

```json
{
  "name": "organizations/your-org-id/apiKeys/your-key-id",
  "privateKey": "-----BEGIN EC PRIVATE KEY-----\n...\n-----END EC PRIVATE KEY-----\n"
}
```

If your downloaded file uses different field names, copy the values into `COINBASE_API_KEY_NAME` and `COINBASE_API_KEY_SECRET` instead.

Git ignores files named `cdp_api_key*.json`. If you keep a key file inside the repository folder, give it a name of that form.

---

## 3. Creating a Coinbase API key

1. Sign in to the Coinbase Developer Platform. Its documentation is at <https://docs.cdp.coinbase.com/>.
2. Create a **Secret API key** for the Advanced Trade API. The `.env` template supports both the ECDSA and the Ed25519 signature algorithm.
3. Grant only the **View** permission for the simulator, and for paper trading. A view-only key guarantees that no bug can move your money. The live trader trades **real money** when its key also has **Trade**; never grant **Transfer**.
4. *(Optional)* Restrict the key to your public IP address. If authentication fails with HTTP 401, the result includes your outbound IP address to help you check this.
5. Put the key in `.env`, either as the name and secret or as a key file (see [section 2](#2-setting-up-the-env-file)).
6. Test the key with the simulator's **Test Coinbase Authentication** button, or with the engine's [`/api/coinbase/authenticate`](#5-the-status-dashboard) endpoint.

### The authentication check

The check uses the official `coinbase-advanced-py` SDK. It lists your accounts, which is a read-only request that the SDK signs with a short-lived JWT. The key and secret are never logged. A successful result looks like this:

```json
{
  "authenticated": true,
  "message": "Read-only Coinbase account request succeeded",
  "account_count": 16,
  "required_permissions": ["view"],
  "checked_at_utc": 1791865757
}
```

On failure, `authenticated` is `false`, `account_count` is `0` and `message` explains why:

- **HTTP 401:** check the key name and private key, and that the key has not expired. The result also gets an `outbound_ip` field, looked up from api.ipify.org, and the address is added to `message`. If the lookup fails, the field reads `unavailable`.
- **HTTP 403:** the key was accepted but lacks permission. Enable **View** on the key.
- **No connection:** `message` starts with `Could not reach the Coinbase API:`.
- **No key:** `message` is the missing-key or missing-file error listed in [Troubleshooting](#12-troubleshooting).

The check never raises an error, so a failed check cannot stop the engine.

After reading the key and before creating a client, the engine runs two checks:

- **Python on `PATH`.** If the running Python's folder isn't on `PATH`, the engine offers to add it. On Windows, accepting writes the folder to your user `PATH` and applies it to the running process. On other systems, it applies only to the running process. Declining is harmless when the right interpreter is already running.
- **The Coinbase SDK.** If `coinbase-advanced-py` can't be imported, the engine offers to `pip install` it into the running interpreter.

In a terminal, the engine asks these questions with a `[y/N]` prompt. This includes checks started from the dashboard: the request waits until you answer in the terminal. Without a terminal, such as under a service manager, it declines automatically and logs why. The simulator asks with dialog boxes instead.

---

## 4. Running the engine

```bash
python engine.py
```

With this command, the engine uses the following settings:

| Setting | Value |
|---|---|
| Base directory | `./quant_system` |
| Model profiles | `quant_system/config/*.json`; the folder is ignored by Git. If it is missing, it is created and no profiles load. |
| Ledger | `quant_system/ledger/fortress.db` |
| `.env` | read from the current directory |
| Serial port | `/dev/ttyS0` |
| Node role | primary |
| Dashboard | port 8080, on all network interfaces |

### Keeping the engine running

> **Known limitation:** `python engine.py` exits as soon as start-up finishes. The dashboard runs in a background (daemon) thread, so it stops when the process ends, which is almost immediately.

Until the engine has a main loop of its own, keep it running with a small launcher script. Save it in the repository root as `run_engine.py`:

```python
"""Keep the engine and its dashboard running until Ctrl+C."""
import time

from engine import QuantitativeTradingEngine, load_environment_config

settings = load_environment_config()
engine = QuantitativeTradingEngine(
    base_directory=".",       # use the repository's config/ and ledger/, as the simulator does
    node_is_primary=True,     # serial_port defaults to /dev/ttyS0; on Windows pass e.g. "COM1"
)
engine.initialize_node_cluster(lan_port=settings["lan_port"])

try:
    while True:
        time.sleep(60)
except KeyboardInterrupt:
    if engine.web_server is not None:
        engine.web_server.shutdown()
```

Run it with:

```bash
python run_engine.py
```

The dashboard is then available at <http://localhost:8080/> (or whichever `LAN_PORT` you set). Press Ctrl+C to stop the engine.

### Logging to a file on Windows

The engine prints status lines that contain emoji. When its output is redirected to a file or a pipe on Windows, Python's default console encoding can't write them, and the engine stops with `UnicodeEncodeError: 'charmap' codec can't encode character`. To avoid this, run Python in UTF-8 mode:

```bash
python -X utf8 run_engine.py > engine.log 2>&1
```

Or set `PYTHONIOENCODING=utf-8` in the environment first. In PowerShell: `$env:PYTHONIOENCODING = "utf-8"`.

### Running as a Linux service (example)

The blueprint's node is a headless Linux machine. A minimal systemd unit for the launcher above looks like this; adjust the paths to your system:

```ini
[Unit]
Description=Configurable Crypto Trading Bot engine
After=network-online.target

[Service]
WorkingDirectory=/opt/trading-bot
ExecStart=/opt/trading-bot/.venv/bin/python run_engine.py
Environment=PYTHONIOENCODING=utf-8
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Install the requirements into that virtual environment beforehand. A service has no terminal, so the engine cannot ask to install anything.

---

## 5. The status dashboard

The dashboard is a small JSON API served by Python's built-in HTTP server.

| Request | Response |
|---|---|
| `GET /` or `GET /api/status` | `200` with the node status (below). |
| `GET /api/coinbase/authenticate` | Runs the read-only [authentication check](#the-authentication-check) immediately and returns its result: `200` if it succeeded, `502` if it failed. |
| Any other path | `404 Endpoint Out of Scope`. Paths must match exactly, so `/api/status?x=1` also gets `404`. |
| Any other method, such as `POST` | `501 Unsupported method` |

The server handles one request at a time, so status requests wait while an authentication check runs. The dashboard does not log requests.

```bash
curl http://localhost:8080/api/status
```

```json
{
    "system_status": "OPERATIONAL",
    "node_execution_state": "PRIMARY_ACTIVE",
    "serial_interlock_connected": false,
    "timestamp_utc": 1791865757,
    "monitored_assets": [
        "BTC"
    ]
}
```

| Field | Meaning |
|---|---|
| `system_status` | Always `OPERATIONAL` while the server answers. It is not a health check. |
| `node_execution_state` | `PRIMARY_ACTIVE` or `SECONDARY_STANDBY`, from the `node_is_primary` argument (see [section 8](#8-serial-interlock-and-node-roles)). |
| `serial_interlock_connected` | Whether the serial port opened at start-up. |
| `timestamp_utc` | The server time, in Unix seconds. |
| `monitored_assets` | The `ticker` of every loaded profile. |

### Security

The dashboard listens on **all network interfaces** (`0.0.0.0`, or `DASHBOARD_HOST`), over plain HTTP. It answers only clients on private networks (unless `DASHBOARD_ALLOW_PUBLIC=true`) and asks for `DASHBOARD_PASSWORD` when one is set; without a password, anyone on your network who can reach the port can read the status, the [live trader's portal](live_trading.md#9-the-web-portal-watching-from-any-machine-on-your-network), and trigger an authentication check with your key. The check returns only success or failure, an account count and, on HTTP 401, your outbound IP address. It never returns the key.

- Block the port with a firewall so that only your local network can reach it, and never forward it from your router. The blueprint's `ufw` rules for Linux are in section 4, "Production Security & Local LAN Isolation Rules", of [quant_infrastructure_blueprint.md](quant_infrastructure_blueprint.md).
- On Windows, if Windows Defender Firewall asks whether to allow Python, allow it on private networks only.

---

## 6. Model profiles in the engine

The engine reads model profiles in the same JSON format as the simulator (see [model_configuration.md](model_configuration.md)):

- **Which files:** every `*.json` file directly in `<base directory>/config/`, in alphabetical order, once at start-up. Subfolders, such as `config/.example/`, are not read. The extension is case-sensitive, so `.JSON` files are skipped.
- **The `ticker` key:** each profile is stored under its `ticker`. If a later file has the same ticker, it replaces the earlier one, so the engine holds **one profile per ticker**. For example, with the repository's `config/` folder, only the alphabetically last `BTC` profile stays loaded.
- **Bad files:** a file that isn't valid JSON, or has no `ticker`, is skipped with a message such as `Parsing failure on my_model.json: 'ticker'`. In that message, `'ticker'` means the field is missing.
- **No validation:** the engine does **not** validate indicators or rules. Check every profile with the simulator's **Load Profile** button before deploying it.
- **Changes:** after changing profiles, restart the engine, or call `ingest_configuration_profiles()` again. A reload adds and replaces profiles but does not remove ones whose files were deleted.

The blueprint's workflow is to develop and test profiles on a workstation with the simulator, then copy the finished JSON files to the node's `config/` folder, for example with `scp`. Today that copy is a manual step.

---

## 7. The ledger database

At start-up, the engine creates `<base directory>/ledger/fortress.db`, an SQLite database configured for low disk wear:

- Write-ahead logging (`journal_mode=WAL`). This setting is stored in the database file, so it persists.
- `synchronous=NORMAL`, an in-memory temp store, and a page cache of about 50 MB (`cache_size=-50000`, measured in KiB). SQLite applies these three settings **per connection**. They are set while the database is created, but connections opened later through `get_connection()` don't reapply them yet.

The ledger has no tables yet. `storage.get_connection(read_only=True)` returns a query-only connection and cursor for future readers.

The two empty ledger files (`ledger/fortress.db`, created by the simulator, and `quant_system/ledger/fortress.db`) are currently tracked by Git. Git does not ignore the `fortress.db-wal` and `fortress.db-shm` files that SQLite may create beside them while a connection is open. Once the ledger holds real trading records, keep it out of the repository.

---

## 8. Serial interlock and node roles

In the blueprint, two nodes are joined by a point-to-point serial cable, which they use to check each other's health and to hand over live trading safely when hardware is replaced.

Today the engine does the following:

- If the optional `pyserial` package is installed (`pip install pyserial`; it is not in `requirements.txt`), the engine opens `serial_port` at 9600 baud, 8 data bits, no parity, 1 stop bit. Linux names ports like `/dev/ttyS0`; Windows names them like `COM1`.
- If `pyserial` is missing, or the port can't be opened, the engine carries on without the link. It prints a warning only when opening the port fails.
- `serial_interlock_connected` on the dashboard reports whether the port opened.
- The `node_is_primary` argument sets `node_execution_state` (`PRIMARY_ACTIVE` or `SECONDARY_STANDBY`). For now the state is only reported; it changes nothing else.

No messages are exchanged over the link yet. The handover protocol is planned; see section 5, "Execution Protocol for Staged Hardware Cutover", of the [blueprint](quant_infrastructure_blueprint.md).

---

## 9. GPU simulation helper

`execute_isolated_gpu_simulation(device_id, target_architecture, raw_prices, window_size)` computes a simple moving average of `raw_prices` over `window_size` values on an NVIDIA GPU, using the optional `cupy` package (it is not in `requirements.txt`). It returns a numpy array with one value per full window, so `len(raw_prices) - window_size + 1` values. It returns `None` in any of these cases:

- `cupy` is not installed, or the computation on the GPU fails.
- **The mutual exclusion guard:** the machine has both an RTX-generation and a GTX-generation card. The blueprint forbids mixing GPU generations in one run, to protect the driver stack.
- `target_architecture`, such as `"RTX"`, does not appear in the name of device `device_id`.

```python
averages = engine.execute_isolated_gpu_simulation(0, "RTX", prices, 20)
```

The device checks are not guarded. If `cupy` is installed but no CUDA device or driver is available, or `device_id` does not exist, the call raises an error instead of returning `None`.

The simulator does not use this helper; its backtests run on the CPU with numpy.

---

## 10. Trading safety switches

The [live trader](live_trading.md) decides from its **API key**. With a key that can trade it places real orders; with a view-only key it paper-trades (see [Paper and live](live_trading.md#5-paper-and-live)). The settings that used to gate live trading no longer do:

| Setting | Where | Now |
|---|---|---|
| `FORCE_PAPER_TRADING=true` | `live_trader.env` | Paper-trades even with a key that can trade, as `live_trader.py --paper` does |
| `COINBASE_ALLOW_TRADING`, `LIVE_EXECUTION_ACTIVE` | `.env` | Not used by the live trader (which doesn't read `.env` at all) |
| `"live"` | A deployment in the settings | Not used; the deployment's notes say so |
| `execution.live_execution_active`, `safeguards.live_execution_active` | Model profiles | Not used by the live trader |

The strongest protection is the API key itself. A **view-only** key can't trade, whatever any setting says, and it is all that paper trading needs.

---

## 11. Using the engine from Python

```python
from engine import QuantitativeTradingEngine

engine = QuantitativeTradingEngine(base_directory=".", node_is_primary=False)
print(engine.authenticate_coinbase())       # the read-only check, as a dictionary
client = engine.create_coinbase_client()    # an authenticated coinbase.rest.RESTClient
```

| Member | Purpose |
|---|---|
| `QuantitativeTradingEngine(base_directory, serial_port="/dev/ttyS0", node_is_primary=True, env_file=".env")` | Creates the engine. It creates the ledger and loads `env_file` immediately. It does not load profiles, open the serial port or start the dashboard. |
| `initialize_node_cluster(lan_port=8080)` | Loads the profiles, opens the serial link and starts the dashboard. |
| `ingest_configuration_profiles()` | Re-reads `<base directory>/config/*.json`. |
| `loaded_models`, `get_model_configuration(ticker)` | The loaded profiles, keyed by ticker. |
| `authenticate_coinbase(dependency_prompt=None, path_prompt=None, client=None)` | Runs the read-only check and stores it in `last_coinbase_authentication`. The prompts are functions that return `True` to approve an SDK install or a `PATH` change. |
| `create_coinbase_client(dependency_prompt=None, path_prompt=None)` | An authenticated Advanced Trade `RESTClient`. Unlike `authenticate_coinbase()`, it raises an error if the key or the SDK is missing. |
| `load_coinbase_credentials()` | The resolved `(key_name, private_key)`. |
| `get_runtime_configuration()`, `get_exchange_connection(name)` | The loaded settings. `name` is `coinbase` or `kraken`; anything else raises `ValueError`. **These include your API secrets**, so never log, print or serve them. |
| `last_coinbase_authentication`, `serial_link_active`, `is_active_executor` | The last check result (or `None`), whether the serial port opened, and the `node_is_primary` value. |
| `web_server` | The running `HTTPServer`, or `None`. Call `web_server.shutdown()` to stop it. |
| `storage.get_connection(read_only=False)` | An `(connection, cursor)` pair for the ledger. |
| `load_environment_config(env_path=".env")` | A module-level function that reads `.env` into a dictionary, without creating an engine. |
| `ensure_python_path(prompt=None)`, `ensure_coinbase_sdk(prompt=None, path_prompt=None)` | Module-level functions that run the `PATH` and SDK checks. Each returns `True` when its check passes. |

---

## 12. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `python engine.py` returns immediately | A known limitation. Use the [launcher script](#keeping-the-engine-running). |
| `UnicodeEncodeError: 'charmap' codec can't encode character` | Output is redirected on Windows. Run `python -X utf8 ...` or set `PYTHONIOENCODING=utf-8` (see [Logging to a file on Windows](#logging-to-a-file-on-windows)). |
| `[LAN SERVER] Interface socket bind failure: ...` | The port is in use, for example by another running engine. Stop the other process or set a different `LAN_PORT` for the launcher. The engine keeps running, but without a dashboard. |
| `ValueError: invalid literal for int()` at start-up | `LAN_PORT` has an inline comment or a non-number value. Put comments on their own line. |
| `Parsing failure on x.json: 'ticker'` | Add a `ticker` field to the profile. |
| `Parsing failure on x.json: Expecting ...` | A JSON syntax error. The message gives the line and column. |
| `coinbase-advanced-py is missing and no interactive prompt is available` | Install the requirements into the interpreter that runs the engine: `pip install -r requirements.txt`. |
| `Coinbase API key name and private key are required ...` | `.env` wasn't found (run from the folder that contains it) or is missing the key. |
| `Coinbase key file not found: ...` | `COINBASE_KEY_FILE` points to a missing file. A relative path is resolved against the base directory, so use an absolute path. |
| `The coinbase-advanced-py package is unavailable to ...` | The SDK install was declined or failed. Install the requirements into the interpreter named in the message. |
| `Could not reach the Coinbase API: ...` | There is no network connection, or `COINBASE_BASE_URL` is wrong. |
| Authentication fails with HTTP 401 or 403 | See [The authentication check](#the-authentication-check). |
| The dashboard works locally but not from another machine | A firewall is blocking the port. Allow it for your local network only. |
