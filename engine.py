"""Trader foundation: the runtime pieces a live trader will build on.

It loads settings from .env, reads the model profiles in config/, opens the SQLite
ledger, authenticates against Coinbase (read-only), serves a JSON status dashboard on
the LAN, opens an optional serial link and offers a GPU smoothing helper. It does not
place orders: COINBASE_ALLOW_TRADING and LIVE_EXECUTION_ACTIVE are read and reported,
but nothing acts on them yet.
"""

import base64
import gc
import hmac
import ipaddress
import json
import logging
import os
import socket
import subprocess
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen

DEFAULT_ENV_PATH = ".env"
LOGGER = logging.getLogger("quant_trading_bot")
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


def _coerce_env_value(raw_value):
    """Strip matching quotes, then turn true/false/yes/no into bools and numbers into int/float."""
    value = raw_value.strip()
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    elif value.startswith("'") and value.endswith("'"):
        value = value[1:-1]

    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"yes", "no"}:
        return lowered == "yes"

    try:
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        return value


def ensure_coinbase_sdk(prompt=None, path_prompt=None):
    """Load coinbase-advanced-py, optionally asking before installing it into this interpreter."""
    path_configured = ensure_python_path(path_prompt)
    try:
        import coinbase.rest  # noqa: F401

        if not path_configured:
            LOGGER.info("[RUNTIME] coinbase-advanced-py is usable through the active interpreter despite PATH")
        return True
    except ImportError:
        pass

    if prompt is None:
        if not sys.stdin.isatty():
            LOGGER.error("[DEPENDENCY] coinbase-advanced-py is missing and no interactive prompt is available")
            return False
        answer = input("Coinbase coinbase-advanced-py is missing. Install it now? [y/N] ").strip().lower()
        prompt = lambda: answer in {"y", "yes"}

    if not prompt():
        LOGGER.warning("[DEPENDENCY] coinbase-advanced-py installation declined")
        return False

    LOGGER.info("[DEPENDENCY] Installing coinbase-advanced-py with %s", sys.executable)
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "coinbase-advanced-py"],
        check=False,
    )
    if result.returncode != 0:
        LOGGER.error("[DEPENDENCY] coinbase-advanced-py installation failed with exit code %s", result.returncode)
        return False

    try:
        import coinbase.rest  # noqa: F401

        LOGGER.info("[DEPENDENCY] coinbase-advanced-py installed successfully")
        return True
    except ImportError as exc:
        LOGGER.error("[DEPENDENCY] coinbase-advanced-py remains unavailable after installation: %s", exc)
        return False


def ensure_python_path(prompt=None):
    """Offer to add the active Python directory to the current and user PATH."""
    python_directory = os.path.dirname(sys.executable)
    path_entries = os.environ.get("PATH", "").split(os.pathsep)
    normalized_entries = {os.path.normcase(os.path.normpath(entry)) for entry in path_entries if entry}
    normalized_python_directory = os.path.normcase(os.path.normpath(python_directory))
    if normalized_python_directory in normalized_entries:
        LOGGER.info("[RUNTIME] Active Python directory is already on PATH: %s", python_directory)
        return True

    LOGGER.warning("[RUNTIME] Active Python directory is not on PATH: %s", python_directory)
    if prompt is None:
        if not sys.stdin.isatty():
            LOGGER.warning("[RUNTIME] PATH was not changed because no interactive prompt is available")
            return False
        answer = input(f"Add {python_directory} to your user PATH? [y/N] ").strip().lower()
        prompt = lambda: answer in {"y", "yes"}

    if not prompt():
        LOGGER.info("[RUNTIME] User declined the PATH update")
        return False

    os.environ["PATH"] = python_directory + os.pathsep + os.environ.get("PATH", "")
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                "Environment",
                0,
                winreg.KEY_READ | winreg.KEY_WRITE,
            ) as key:
                try:
                    user_path, value_type = winreg.QueryValueEx(key, "Path")
                except FileNotFoundError:
                    user_path, value_type = "", winreg.REG_EXPAND_SZ
                user_entries = user_path.split(os.pathsep) if user_path else []
                normalized_user_entries = {
                    os.path.normcase(os.path.normpath(entry)) for entry in user_entries if entry
                }
                if normalized_python_directory not in normalized_user_entries:
                    updated_user_path = os.pathsep.join([python_directory, user_path] if user_path else [python_directory])
                    winreg.SetValueEx(key, "Path", 0, value_type, updated_user_path)
            LOGGER.info("[RUNTIME] Added active Python directory to user PATH")
        except OSError as exc:
            LOGGER.error("[RUNTIME] Could not persist PATH update: %s", exc)
            return False
    else:
        LOGGER.info("[RUNTIME] Added active Python directory to PATH for this process")
    return True


def load_environment_config(env_path=DEFAULT_ENV_PATH):
    """Load local runtime secrets and external API settings from a .env file.

    Returns the defaults below, overridden by any recognised keys in the file (keys are
    case-insensitive; unknown keys are ignored). A missing file just gives the defaults.
    """
    env_file = env_path or DEFAULT_ENV_PATH
    config = {
        "coinbase": {
            "api_key_name": "",
            "api_key_secret": "",
            "key_file": "",
            "required_permissions": ["view"],
            "allow_trading": False,
            "base_url": "https://api.coinbase.com",
            "enabled": False,
        },
        "kraken": {
            "api_key": "",
            "api_secret": "",
            "base_url": "https://api.kraken.com",
            "enabled": False,
        },
        "lan_port": 8080,
        # The dashboard and web portal: the address to listen on (all interfaces, so other machines on the network
        # can reach it), an optional password, and whether clients outside private networks may connect at all.
        "dashboard_host": "0.0.0.0",
        "dashboard_password": "",
        "dashboard_allow_public": False,
        "live_execution_active": False,
        # The live trader paper-trades even with an API key that can trade (live_trader.py --paper does the same).
        "force_paper_trading": False,
    }

    if not os.path.exists(env_file):
        return config

    with open(env_file, "r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = [segment.strip() for segment in line.split("=", 1)]

            normalized_key = key.upper()
            value = _coerce_env_value(value)

            if normalized_key == "COINBASE_API_KEY_NAME":
                config["coinbase"]["api_key_name"] = str(value)
            elif normalized_key == "COINBASE_API_KEY_SECRET":
                # A PEM key fits on one .env line only with its newlines escaped as \n.
                config["coinbase"]["api_key_secret"] = str(value).replace("\\n", "\n")
            elif normalized_key == "COINBASE_KEY_FILE":
                config["coinbase"]["key_file"] = str(value)
            elif normalized_key == "COINBASE_REQUIRED_PERMISSIONS":
                config["coinbase"]["required_permissions"] = [
                    permission.strip()
                    for permission in str(value).split(",")
                    if permission.strip()
                ]
            elif normalized_key == "COINBASE_ALLOW_TRADING":
                config["coinbase"]["allow_trading"] = bool(value)
            elif normalized_key == "COINBASE_BASE_URL":
                config["coinbase"]["base_url"] = str(value)
            elif normalized_key == "COINBASE_ENABLED":
                config["coinbase"]["enabled"] = bool(value)

            elif normalized_key == "KRAKEN_API_KEY":
                config["kraken"]["api_key"] = str(value)
            elif normalized_key == "KRAKEN_API_SECRET":
                config["kraken"]["api_secret"] = str(value)
            elif normalized_key == "KRAKEN_BASE_URL":
                config["kraken"]["base_url"] = str(value)
            elif normalized_key == "KRAKEN_ENABLED":
                config["kraken"]["enabled"] = bool(value)

            elif normalized_key == "DASHBOARD_HOST":
                config["dashboard_host"] = str(value) or "0.0.0.0"
            elif normalized_key == "DASHBOARD_PASSWORD":
                config["dashboard_password"] = str(value)
            elif normalized_key == "DASHBOARD_ALLOW_PUBLIC":
                config["dashboard_allow_public"] = bool(value)
            elif normalized_key == "LAN_PORT":
                config["lan_port"] = int(value)
            elif normalized_key == "FORCE_PAPER_TRADING":
                config["force_paper_trading"] = bool(value)
            elif normalized_key == "LIVE_EXECUTION_ACTIVE":
                config["live_execution_active"] = bool(value)

    return config


try:
    import serial
except ImportError:  # pragma: no cover - optional runtime dependency
    serial = None

try:
    import cupy as cp
except ImportError:  # pragma: no cover - optional runtime dependency
    cp = None


class QuantitativeStorageController:
    """The SQLite ledger file; creates its directory and sets connection pragmas."""

    def __init__(self, db_path):
        self.db_path = db_path
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._initialize_core_infrastructure()

    def _initialize_core_infrastructure(self):
        """Switch the database to WAL mode and apply speed/low-disk-wear pragmas.

        Only WAL persists in the file; the other pragmas last for this one connection.
        No tables are created here.
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.execute("PRAGMA temp_store=MEMORY;")
        cursor.execute("PRAGMA cache_size=-50000;")
        conn.commit()
        conn.close()

    def get_connection(self, read_only=False):
        """Return a new (connection, cursor); the caller closes it. read_only sets query_only."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        if read_only:
            cursor.execute("PRAGMA query_only=TRUE;")
        return conn, cursor


class IndustrialHardwareInterlock:
    """Optional serial link (pyserial) to external hardware; only opened, never written to yet."""

    def __init__(self, port_device="/dev/ttyS0", baudrate=9600):
        self.port_device = port_device
        self.baudrate = baudrate
        self.ser = None

    def initialize_channel(self):
        """Open the port at 8N1; return False if pyserial is missing or the port won't open."""
        if serial is None:
            return False
        try:
            self.ser = serial.Serial(
                port=self.port_device,
                baudrate=self.baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=1.5,
            )
            return True
        except Exception as exc:  # pragma: no cover - hardware dependent
            print(f"⚠️ [SERIAL CONTROLLER] Physical channel unmapped: {exc}")
            return False


def client_allowed(address, allow_public=False):
    """Whether a client at `address` may use the dashboard: this machine and private networks (home and office
    LANs, link-local addresses) always may; addresses on the internet only with DASHBOARD_ALLOW_PUBLIC."""
    if allow_public:
        return True
    try:
        ip = ipaddress.ip_address(str(address).split("%", 1)[0])
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_private or ip.is_link_local


def lan_addresses():
    """This machine's addresses on its local networks, for telling other machines where the dashboard is."""
    found = set()
    try:  # the address of the interface that routes outwards; nothing is sent
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            found.add(probe.getsockname()[0])
    except OSError:
        pass
    try:
        found.update(info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET))
    except OSError:
        pass
    return sorted(address for address in found if not address.startswith("127."))


class LocalDashboardHandler(BaseHTTPRequestHandler):
    """Read-only JSON status endpoints and the live trader's web portal.

    GET / or /api/status returns the node state; GET /api/coinbase/authenticate runs a
    fresh read-only Coinbase check; /portal and /api/live... are the live trader's portal
    (web_portal.py). The server listens on every interface so other machines on the
    network can use it, answers only private-network clients unless DASHBOARD_ALLOW_PUBLIC
    is set, and asks for DASHBOARD_PASSWORD when one is set. It is plain HTTP: keep it on
    networks you trust.
    """

    def log_message(self, format, *args):
        # Silence the default per-request stderr logging.
        return

    def _authorized(self):
        """Refuse clients from outside private networks, and ask for the password when one is set."""
        config = self.server.engine_ref.api_config
        if not client_allowed(self.client_address[0], config.get("dashboard_allow_public", False)):
            self.send_error(403, "Only machines on private networks may use this dashboard")
            return False
        password = config.get("dashboard_password") or ""
        if not password:
            return True
        header = self.headers.get("Authorization", "")
        supplied = ""
        if header.startswith("Basic "):
            try:
                supplied = base64.b64decode(header[6:], validate=True).decode("utf-8").partition(":")[2]
            except (ValueError, UnicodeDecodeError):
                supplied = ""
        if hmac.compare_digest(supplied.encode("utf-8"), password.encode("utf-8")):
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Trading node", charset="UTF-8"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def do_GET(self):
        if not self._authorized():
            return
        from web_portal import SECURITY_HEADERS, route
        answer = route(self.path, getattr(self.server.engine_ref, "live_trader", None))
        if answer is not None:
            status, content_type, body, headers = answer
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for name, value in {**SECURITY_HEADERS, **headers}.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/coinbase/authenticate":
            result = self.server.engine_ref.authenticate_coinbase()
            self.send_response(200 if result["authenticated"] else 502)
            self.send_header("Content-Type", "application/json")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(json.dumps(result).encode("utf-8"))
        elif self.path in ("/", "/api/status"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()

            status_payload = {
                "system_status": "OPERATIONAL",
                "node_execution_state": "PRIMARY_ACTIVE" if self.server.engine_ref.is_active_executor else "SECONDARY_STANDBY",
                "serial_interlock_connected": self.server.engine_ref.serial_link_active,
                "timestamp_utc": int(time.time()),
                "monitored_assets": list(self.server.engine_ref.loaded_models.keys()),
            }
            trader = getattr(self.server.engine_ref, "live_trader", None)
            if trader is not None:
                # Each currency the live trader runs, with how many deployments trade it; details at /api/live.
                live = trader.snapshot()
                status_payload["monitored_assets"] = live.get("currencies", [])
                status_payload["live_trading"] = {
                    "mode": live.get("mode"), "system_live": live.get("system_live"),
                    "halt_file": live.get("halt_file"),
                    "deployments": len(live.get("deployments", [])),
                    "needs_review": [row["id"] for row in live.get("deployments", [])
                                     if row.get("status") == "NEEDS_REVIEW"],
                }
            self.wfile.write(json.dumps(status_payload, indent=4).encode("utf-8"))
        else:
            self.send_error(404, "Endpoint Out of Scope")


class QuantitativeTradingEngine:
    """Ties settings, profiles, ledger, Coinbase auth, dashboard and serial link together.

    node_is_primary only labels the node in the status payload; this class places no orders.
    """

    def __init__(self, base_directory, serial_port="/dev/ttyS0", node_is_primary=True, env_file=DEFAULT_ENV_PATH):
        self.base_dir = base_directory
        self.config_dir = os.path.join(base_directory, "config")
        self.db_path = os.path.join(base_directory, "ledger/fortress.db")
        self.storage = QuantitativeStorageController(self.db_path)
        self.interlock = IndustrialHardwareInterlock(port_device=serial_port)

        self.api_config = load_environment_config(env_file)
        self.loaded_models = {}
        self.is_active_executor = node_is_primary
        self.serial_link_active = False
        self.web_server = None
        self.last_coinbase_authentication = None
        self.live_trader = None  # set by live_trader.py; the dashboard then serves /api/live

    def get_exchange_connection(self, exchange_name):
        """Return the .env settings for "coinbase" or "kraken" (a dict, not a live connection)."""
        exchange_name = exchange_name.lower()
        if exchange_name not in {"coinbase", "kraken"}:
            raise ValueError(f"Unsupported exchange: {exchange_name}")
        return self.api_config.get(exchange_name, {})

    def get_runtime_configuration(self):
        """Paths and .env settings as a dict; the exchange entries include the API secrets."""
        return {
            "base_directory": self.base_dir,
            "config_dir": self.config_dir,
            "db_path": self.db_path,
            "live_execution_active": self.api_config.get("live_execution_active", False),
            "lan_port": self.api_config.get("lan_port", 8080),
            "exchange_connections": {
                "coinbase": self.api_config.get("coinbase", {}),
                "kraken": self.api_config.get("kraken", {}),
            },
        }

    def get_model_configuration(self, ticker):
        """The loaded profile for `ticker`, or None."""
        return self.loaded_models.get(ticker)

    def _coinbase_key_label(self):
        """The last path segment of the key name, safe to log."""
        key_name = self.api_config["coinbase"].get("api_key_name", "")
        return key_name.rsplit("/", 1)[-1] if key_name else "unconfigured"

    def load_coinbase_credentials(self):
        """Resolve the CDP API key name and private key from .env or a key file.

        The key file is used only when .env lacks the name or the secret; a relative path is
        resolved against base_directory. Raises if neither source supplies both values.
        """
        coinbase = self.api_config["coinbase"]
        key_name = coinbase.get("api_key_name", "")
        key_secret = coinbase.get("api_key_secret", "")
        key_file = coinbase.get("key_file", "")

        if key_file and not (key_name and key_secret):
            if not os.path.isabs(key_file):
                key_file = os.path.join(self.base_dir, key_file)
            if not os.path.exists(key_file):
                raise FileNotFoundError(f"Coinbase key file not found: {key_file}")
            with open(key_file, "r", encoding="utf-8") as handle:
                key_data = json.load(handle)
            # Accept both the CDP download format and the api_key/api_secret format.
            key_name = key_data.get("api_key") or key_data.get("name") or ""
            key_secret = key_data.get("api_secret") or key_data.get("privateKey") or ""
            coinbase["api_key_name"] = key_name

        if not key_name or not key_secret:
            raise ValueError(
                "Coinbase API key name and private key are required "
                "(set COINBASE_API_KEY_NAME and COINBASE_API_KEY_SECRET, or COINBASE_KEY_FILE)"
            )
        return key_name, key_secret

    def create_coinbase_client(self, dependency_prompt=None, path_prompt=None):
        """Create an Advanced Trade REST client; the SDK signs each request with a fresh JWT."""
        key_name, key_secret = self.load_coinbase_credentials()

        if not ensure_coinbase_sdk(dependency_prompt, path_prompt):
            raise RuntimeError(
                f"The coinbase-advanced-py package is unavailable to {sys.executable}"
            )

        from coinbase.rest import RESTClient

        base_url = self.api_config["coinbase"].get("base_url", "https://api.coinbase.com")
        LOGGER.info("[COINBASE AUTH] Creating REST client for key %s", self._coinbase_key_label())
        return RESTClient(
            api_key=key_name,
            api_secret=key_secret,
            base_url=base_url.split("://", 1)[-1],
        )

    def authenticate_coinbase(self, dependency_prompt=None, path_prompt=None, client=None):
        """Run a read-only Coinbase authentication check by listing accounts.

        Pass `client` to test a client that was already created. Never raises: the result
        dict says whether it worked and why not, and is also kept in
        last_coinbase_authentication. On a 401 it adds the public outbound IP, since a key
        with an IP allowlist rejects other addresses.
        """
        try:
            from requests.exceptions import ConnectionError as RequestsConnectionError
            from requests.exceptions import HTTPError
        except ImportError:  # requests ships with coinbase-advanced-py
            RequestsConnectionError = HTTPError = type("_Unavailable", (Exception,), {})

        try:
            client = client or self.create_coinbase_client(dependency_prompt, path_prompt)
            response = client.get_accounts()
            account_count = len(response.accounts or [])
            LOGGER.info("[COINBASE AUTH] Authentication succeeded; accounts returned: %d", account_count)
            result = self._authentication_result(
                authenticated=True,
                message="Read-only Coinbase account request succeeded",
                account_count=account_count,
            )
        except HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            message = f"HTTP Error {status}: {exc}"
            if status == 401:
                message += "; check the API key name and private key, and that the key is not expired"
            elif status == 403:
                message += "; the key was accepted but lacks permission (enable 'View' on the key)"
            LOGGER.error("[COINBASE AUTH] Authentication failed with HTTP %s", status)
            result = self._authentication_result(authenticated=False, message=message)
            if status == 401:
                result["outbound_ip"] = self._get_outbound_ip()
                result["message"] += f"; detected outbound IP: {result['outbound_ip']}"
        except RequestsConnectionError as exc:
            LOGGER.error("[COINBASE AUTH] Could not reach the Coinbase API: %s", exc)
            result = self._authentication_result(
                authenticated=False, message=f"Could not reach the Coinbase API: {exc}"
            )
        except Exception as exc:  # Authentication diagnostics must not stop the engine.
            # The SDK's JWT signer raises a plain Exception when the private key can't be loaded.
            LOGGER.error("[COINBASE AUTH] Authentication failed: %s", exc)
            result = self._authentication_result(authenticated=False, message=str(exc))

        self.last_coinbase_authentication = result
        return result

    def _authentication_result(self, authenticated, message, account_count=0):
        return {
            "authenticated": authenticated,
            "message": message,
            "account_count": account_count,
            "required_permissions": self.api_config["coinbase"].get("required_permissions", []),
            "checked_at_utc": int(time.time()),
        }

    def _get_outbound_ip(self):
        """Determine the public egress IP without sending Coinbase credentials."""
        try:
            request = Request(
                "https://api.ipify.org?format=text",
                headers={"User-Agent": "ConfigurableCryptoTradingBot/1.0"},
            )
            with urlopen(request, timeout=10) as response:
                outbound_ip = response.read().decode("utf-8").strip()
            LOGGER.info("[NETWORK] Detected public outbound IP: %s", outbound_ip)
            return outbound_ip
        except Exception as exc:
            LOGGER.warning("[NETWORK] Could not determine public outbound IP: %s", exc)
            return "unavailable"

    def initialize_node_cluster(self, lan_port=8080):
        """Load profiles, try the serial link and start the status dashboard on `lan_port`."""
        LOGGER.info("[RUNTIME] Python interpreter: %s", sys.executable)
        self.ingest_configuration_profiles()
        self.serial_link_active = self.interlock.initialize_channel()
        self.spin_up_lan_isolated_dashboard(lan_port)

    def ingest_configuration_profiles(self):
        """Load every config/*.json profile into loaded_models, keyed by its "ticker".

        Profiles sharing a ticker overwrite each other (the last file name wins); a file
        that fails to parse is reported and skipped. Creates config/ if it is missing.
        """
        if not os.path.exists(self.config_dir):
            os.makedirs(self.config_dir, exist_ok=True)
            return
        for file_name in sorted(os.listdir(self.config_dir)):
            if file_name.endswith(".json"):
                path = os.path.join(self.config_dir, file_name)
                try:
                    with open(path, "r", encoding="utf-8") as handle:
                        cfg = json.load(handle)
                    self.loaded_models[cfg["ticker"]] = cfg
                except Exception as exc:  # pragma: no cover - config validation is runtime-driven
                    print(f"❌ [ORCHESTRATOR] Parsing failure on {file_name}: {exc}")
        gc.collect()

    def spin_up_lan_isolated_dashboard(self, port, host=None):
        """Serve LocalDashboardHandler at `port` from daemon threads, one per request.

        `host` defaults to DASHBOARD_HOST (all interfaces), so other machines on the network can reach it; use
        127.0.0.1 to keep it to this machine. Returns the URLs it can be reached at ([] if it couldn't start).
        """
        host = host or self.api_config.get("dashboard_host") or "0.0.0.0"
        try:
            self.web_server = ThreadingHTTPServer((host, port), LocalDashboardHandler)
        except OSError as exc:  # pragma: no cover - environment-specific
            LOGGER.error("[LAN SERVER] Could not listen on %s:%s: %s", host, port, exc)
            return []
        self.web_server.daemon_threads = True
        self.web_server.engine_ref = self
        port = self.web_server.server_address[1]
        server_thread = threading.Thread(target=self.web_server.serve_forever, daemon=True)
        server_thread.start()
        if host in ("0.0.0.0", ""):  # every interface: this machine and its network addresses
            hosts = ["localhost"] + lan_addresses()
        elif host in ("127.0.0.1", "localhost"):
            hosts = ["localhost"]
        else:
            hosts = [host]
        urls = [f"http://{name}:{port}/" for name in hosts]
        LOGGER.info("[LAN SERVER] Dashboard listening on %s:%s; open %s", host, port,
                    ", ".join(url + "portal" for url in urls))
        if self.api_config.get("dashboard_password"):
            LOGGER.info("[LAN SERVER] The dashboard asks for DASHBOARD_PASSWORD")
        return urls

    def execute_isolated_gpu_simulation(self, device_id, target_architecture, raw_prices, window_size):
        """Return a simple moving average of raw_prices (window_size) computed on one GPU.

        Returns None if CuPy is missing, if the machine has both RTX and GTX cards (the
        mixed-architecture guard), if device_id's name doesn't contain target_architecture,
        or if the GPU call fails.
        """
        if cp is None:
            return None

        device_count = cp.cuda.runtime.getDeviceCount()
        detected_architectures = []

        for device_index in range(device_count):
            props = cp.cuda.runtime.getDeviceProperties(device_index)
            name = props["name"].decode("utf-8").upper()
            if "RTX" in name:
                detected_architectures.append("RTX_ADA_AMPERE")
            if "GTX" in name:
                detected_architectures.append("GTX_PASCAL")

        detected_architectures = list(set(detected_architectures))

        if len(detected_architectures) > 1:
            print("🛑 [HARDWARE BLOCK] Mixed GPU architecture detected (RTX + GTX inside same loop)!")
            print("   -> Execution halted by mutual exclusion guard to protect driver stack stability.")
            return None

        selected_device_name = cp.cuda.runtime.getDeviceProperties(device_id)["name"].decode("utf-8").upper()
        if target_architecture.upper() not in selected_device_name:
            print(
                f"🛑 [HARDWARE LOCK] Request architecture '{target_architecture}' does not match Device {device_id} ({selected_device_name})."
            )
            return None

        try:
            with cp.cuda.Device(device_id):
                vram_vector = cp.array(raw_prices, dtype=cp.float32)
                smoothing_filter = cp.ones(window_size) / window_size
                gpu_output = cp.convolve(vram_vector, smoothing_filter, mode="valid")
                result_numpy = cp.asnumpy(gpu_output)

                del vram_vector, gpu_output
                cp.get_default_memory_pool().free_all_blocks()
                return result_numpy
        except Exception as exc:  # pragma: no cover - GPU runtime dependent
            print(f"❌ [GPU COMPUTE FAULT] Device context instantiation dropped: {exc}")
            return None


if __name__ == "__main__":
    # Starts the status node (profiles, serial link, dashboard); it does not trade.
    engine = QuantitativeTradingEngine(base_directory="./quant_system", serial_port="/dev/ttyS0", node_is_primary=True)
    engine.initialize_node_cluster(lan_port=8080)
