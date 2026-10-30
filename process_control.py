"""Running the live trader on its own: in the background, restarted after a crash, stopped on request.

The live trader needs no service manager or task scheduler on either Windows or Unix:

    python live_trader.py --background     start it detached from the terminal; returns at once
    python live_trader.py --status         is it running, since when, and where its web portal is
    python live_trader.py --stop           ask it to stop; it finishes its cycle and exits cleanly

--background starts a small *supervisor* process, detached from the terminal (a new session on Unix, a detached
process on Windows), so closing the terminal or signing out of the shell doesn't stop it. The supervisor runs
the trader itself as a *worker* process (`--headless`) and starts it again if it crashes, waiting longer after
each crash in a row. A worker that can't start for a reason a restart won't fix (bad settings, another trader
already running) ends the supervisor instead.

Everything is coordinated through files in the data folder's run/ (see data_home.py), so it works the same on
every system:

    live_trader.lock    held (an OS file lock) by whichever process is trading; a second trader can't start
    supervisor.json     the supervisor's process id and start time
    worker.json         the trading process's id, start time and web portal addresses
    stop                created by --stop; both processes watch for it

Nothing here places orders; see live_trader.py.
"""

import json
import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

LOCK_NAME = "live_trader.lock"
SUPERVISOR_FILE = "supervisor.json"
WORKER_FILE = "worker.json"
STOP_FILE = "stop"
# How a worker's exit code tells the supervisor whether to start it again.
EXIT_OK = 0
EXIT_CONFIG = 2  # the settings are wrong: starting again won't help
EXIT_ALREADY_RUNNING = 3  # another trader holds the lock
EXIT_LEDGER = 4  # the trading ledger is damaged: restore a backup first
FATAL_EXITS = (EXIT_CONFIG, EXIT_ALREADY_RUNNING, EXIT_LEDGER)
FATAL_REASONS = {EXIT_CONFIG: "settings problem", EXIT_ALREADY_RUNNING: "another trader is running",
                 EXIT_LEDGER: "the ledger is damaged"}
FIRST_BACKOFF = 10  # seconds before the first restart after a crash
MAX_BACKOFF = 300
STABLE_SECONDS = 600  # a worker that ran this long resets the backoff


class AlreadyRunning(RuntimeError):
    pass


def run_dir(base_dir):
    return Path(base_dir) / "run"


# --- one trader at a time ----------------------------------------------------------------------------

class InstanceLock:
    """An operating-system lock on run/live_trader.lock, held while a process trades. It is released when the
    process exits, however it exits, so a crash never leaves a stale lock behind."""

    def __init__(self, path):
        self.path, self.handle = Path(path), None

    def acquire(self, wait=2.0):
        """Take the lock, trying for up to `wait` seconds: a process that has just died (a crash, then an
        immediate restart) can hold it for a moment longer, while the system closes its files."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + wait
        while True:
            handle = open(self.path, "a+")
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                handle.close()
                if time.monotonic() >= deadline:
                    raise AlreadyRunning(f"Another live trader is already running on this data folder "
                                         f"(it holds {self.path}); stop it first with --stop") from None
                time.sleep(0.1)
        self.handle = handle
        return self

    def release(self):
        if self.handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self.handle.close()
        self.handle = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()


def pid_alive(pid):
    """Whether process `pid` is running, on Windows or Unix."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: it exists, owned by someone else
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# --- the files in run/ -------------------------------------------------------------------------------

def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, indent=1), encoding="utf-8")
    temporary.replace(path)


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_record(directory, name, **fields):
    _write_json(Path(directory) / name, {"pid": os.getpid(), "started": int(time.time()), **fields})


def remove_record(directory, name):
    """Remove a record this process wrote (never another process's)."""
    record = _read_json(Path(directory) / name)
    if record and record.get("pid") == os.getpid():
        try:
            (Path(directory) / name).unlink()
        except OSError:
            pass


def status(directory):
    """{"running", "supervisor", "worker"}: each record if its process is alive (stale records are ignored)."""
    directory = Path(directory)
    records = {}
    for key, name in (("supervisor", SUPERVISOR_FILE), ("worker", WORKER_FILE)):
        record = _read_json(directory / name)
        records[key] = record if record and pid_alive(record.get("pid")) else None
    records["running"] = bool(records["supervisor"] or records["worker"])
    records["stop_requested"] = stop_requested(directory)
    return records


def describe_status(state):
    if not state["running"]:
        return "The live trader is not running."
    lines = []
    for key in ("supervisor", "worker"):
        record = state[key]
        if record:
            since = datetime.fromtimestamp(record["started"], timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            label = "Supervisor" if key == "supervisor" else "Trader"
            lines.append(f"{label} running as process {record['pid']} since {since}")
    urls = (state["worker"] or {}).get("urls") or []
    if urls:
        lines.append("Web portal: " + ", ".join(url + "portal" for url in urls))
    if state["stop_requested"]:
        lines.append("A stop has been requested; it is finishing its cycle.")
    return "\n".join(lines)


def request_stop(directory):
    path = Path(directory) / STOP_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")


def stop_requested(directory):
    return (Path(directory) / STOP_FILE).exists()


def clear_stop(directory):
    try:
        (Path(directory) / STOP_FILE).unlink()
    except FileNotFoundError:
        pass


def watch_for_stop(directory, event, interval=1.0):
    """Set `event` once a stop is requested; a daemon thread checks every `interval` seconds."""
    def watch():
        while not event.is_set():
            if stop_requested(directory):
                event.set()
                return
            time.sleep(interval)
    thread = threading.Thread(target=watch, name="stop-watch", daemon=True)
    thread.start()
    return thread


def wait_for_stop(directory, timeout, clock=time.monotonic, sleep=time.sleep):
    """After request_stop: wait until nothing is running; True if it stopped within `timeout` seconds."""
    deadline = clock() + timeout
    while clock() < deadline:
        if not status(directory)["running"]:
            clear_stop(directory)
            return True
        sleep(1.0)
    return False


# --- starting in the background ----------------------------------------------------------------------

def detached_options():
    """Popen options that start a process with no terminal, outliving the one that started it."""
    options = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
               "close_fds": True}
    if os.name == "nt":
        options["creationflags"] = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP |
                                    subprocess.CREATE_NO_WINDOW)
    else:
        options["start_new_session"] = True  # its own session: closing the terminal sends it no SIGHUP
    return options


def hidden_options():
    """Popen options for the supervisor's worker: no console window of its own, on Windows."""
    options = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    return options


def start_background(command, directory, cwd=None, wait=60.0, sleep=time.sleep):
    """Start `command` (the supervisor) detached, and wait for its trader to report in. Returns status()."""
    state = status(directory)
    if state["running"]:
        raise AlreadyRunning("The live trader is already running:\n" + describe_status(state))
    clear_stop(directory)
    process = subprocess.Popen(command, cwd=cwd, **detached_options())
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        state = status(directory)
        if state["worker"] and (state["worker"].get("urls") is not None):
            return state
        if process.poll() is not None:
            break
        sleep(0.5)
    return status(directory)


# --- the supervisor ----------------------------------------------------------------------------------

def end_recorded_worker(directory):
    """End the trading process recorded in worker.json. The supervisor's own handle can be a launcher rather
    than the trader (a virtual environment's python.exe on Windows starts the real interpreter as a child)."""
    import signal
    record = _read_json(Path(directory) / WORKER_FILE)
    if record and pid_alive(record.get("pid")) and record["pid"] != os.getpid():
        try:
            os.kill(record["pid"], signal.SIGTERM)  # TerminateProcess on Windows
        except OSError:
            pass


def supervise(launch, directory, log, sleep=time.sleep, clock=time.monotonic, grace=120.0):
    """Run workers until a stop is requested: `launch()` starts one (a Popen-like object). A worker that crashes
    is started again after a backoff that doubles with each crash in a row; one that exits with a FATAL_EXITS
    code ends the supervision. Returns the last worker's exit code."""
    backoff, code = FIRST_BACKOFF, EXIT_OK
    while not stop_requested(directory):
        started = clock()
        worker = launch()
        log(f"started the trader as process {getattr(worker, 'pid', '?')}")
        stop_seen_at = None
        while (code := worker.poll()) is None:
            if stop_requested(directory):
                stop_seen_at = stop_seen_at or clock()
                if clock() - stop_seen_at > grace:  # it should have seen the stop file itself by now
                    log("the trader didn't stop in time; ending it")
                    end_recorded_worker(directory)
                    worker.terminate()
            sleep(1.0)
        if stop_requested(directory):
            log(f"the trader stopped (exit code {code})")
            break
        if code in FATAL_EXITS:
            log(f"the trader can't run (exit code {code}: "
                f"{FATAL_REASONS.get(code, 'unknown')}); not restarting")
            break
        if clock() - started >= STABLE_SECONDS:
            backoff = FIRST_BACKOFF
        log(f"the trader exited unexpectedly (exit code {code}); restarting in {backoff}s")
        waited = 0.0
        while waited < backoff and not stop_requested(directory):
            sleep(1.0)
            waited += 1.0
        backoff = min(backoff * 2, MAX_BACKOFF)
    return code
