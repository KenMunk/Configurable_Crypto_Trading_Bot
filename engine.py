import gc
import json
import os
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

DEFAULT_ENV_PATH = ".env"


def _coerce_env_value(raw_value):
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


def load_environment_config(env_path=DEFAULT_ENV_PATH):
    """Load local runtime secrets and external API settings from a .env file."""
    env_file = env_path or DEFAULT_ENV_PATH
    config = {
        "coinbase": {
            "api_key": "",
            "api_secret": "",
            "passphrase": "",
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
        "live_execution_active": False,
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

            if normalized_key == "COINBASE_API_KEY":
                config["coinbase"]["api_key"] = str(value)
            elif normalized_key == "COINBASE_API_SECRET":
                config["coinbase"]["api_secret"] = str(value)
            elif normalized_key == "COINBASE_PASSPHRASE":
                config["coinbase"]["passphrase"] = str(value)
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

            elif normalized_key == "LAN_PORT":
                config["lan_port"] = int(value)
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
    def __init__(self, db_path):
        self.db_path = db_path
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._initialize_core_infrastructure()

    def _initialize_core_infrastructure(self):
        """Sets up a low-wear, optimized transaction ledger matrix."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.execute("PRAGMA temp_store=MEMORY;")
        cursor.execute("PRAGMA cache_size=-50000;")
        conn.commit()
        conn.close()

    def get_connection(self, read_only=False):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        if read_only:
            cursor.execute("PRAGMA query_only=TRUE;")
        return conn, cursor


class IndustrialHardwareInterlock:
    def __init__(self, port_device="/dev/ttyS0", baudrate=9600):
        self.port_device = port_device
        self.baudrate = baudrate
        self.ser = None

    def initialize_channel(self):
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


class LocalDashboardHandler(BaseHTTPRequestHandler):
    """LAN-isolated monitoring status router."""

    def log_message(self, format, *args):
        return

    def do_GET(self):
        if self.path in ("/", "/api/status"):
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
            self.wfile.write(json.dumps(status_payload, indent=4).encode("utf-8"))
        else:
            self.send_error(404, "Endpoint Out of Scope")


class QuantitativeTradingEngine:
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

    def get_exchange_connection(self, exchange_name):
        exchange_name = exchange_name.lower()
        if exchange_name not in {"coinbase", "kraken"}:
            raise ValueError(f"Unsupported exchange: {exchange_name}")
        return self.api_config.get(exchange_name, {})

    def get_runtime_configuration(self):
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
        return self.loaded_models.get(ticker)

    def initialize_node_cluster(self, lan_port=8080):
        self.ingest_configuration_profiles()
        self.serial_link_active = self.interlock.initialize_channel()
        self.spin_up_lan_isolated_dashboard(lan_port)

    def ingest_configuration_profiles(self):
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

    def spin_up_lan_isolated_dashboard(self, port):
        try:
            self.web_server = HTTPServer(("0.0.0.0", port), LocalDashboardHandler)
            self.web_server.engine_ref = self
            server_thread = threading.Thread(target=self.web_server.serve_forever, daemon=True)
            server_thread.start()
            print(f"📡 [LAN SERVER] Private status dashboard active at http://localhost:{port}")
        except Exception as exc:  # pragma: no cover - environment-specific
            print(f"❌ [LAN SERVER] Interface socket bind failure: {exc}")

    def execute_isolated_gpu_simulation(self, device_id, target_architecture, raw_prices, window_size):
        """
        Runs localized simulations with an explicit Hardware Mutual Exclusion Guard.
        Strictly prevents modern RTX and legacy GTX architectures from running at the same time.
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
    engine = QuantitativeTradingEngine(base_directory="./quant_system", serial_port="/dev/ttyS0", node_is_primary=True)
    engine.initialize_node_cluster(lan_port=8080)
