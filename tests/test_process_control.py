"""Running the live trader on its own (process_control.py): one at a time, in the background, stopped on request,
restarted after a crash. These use real processes, so they check this platform's behaviour (Windows or Unix)."""
import json
import subprocess
import sys
import textwrap
import threading
import time

import pytest

import live_trader
import process_control as pc

ROOT = str(pc.Path(__file__).resolve().parent.parent)


def _hold_lock(path):
    """A separate process holding the trader's lock until killed."""
    code = f"import sys; sys.path.insert(0, {ROOT!r}); import process_control as pc, time\n" \
           f"lock = pc.InstanceLock({str(path)!r}).acquire(); print('held', flush=True); time.sleep(60)"
    child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    assert child.stdout.readline().strip() == "held"
    return child


def test_only_one_trader_can_hold_the_lock_and_a_dead_one_releases_it(tmp_path):
    path = tmp_path / "run" / pc.LOCK_NAME
    child = _hold_lock(path)
    try:
        with pytest.raises(pc.AlreadyRunning, match="already running"):
            pc.InstanceLock(path).acquire()
    finally:
        child.kill()  # a crash, not a clean exit: the system still frees the lock
        child.wait()
    with pc.InstanceLock(path):
        pass


def test_the_trader_refuses_to_start_beside_another(tmp_path, capsys):
    child = _hold_lock(pc.run_dir(tmp_path) / pc.LOCK_NAME)
    try:
        code = live_trader.main(["--data-dir", str(tmp_path), "--once", "--config", str(tmp_path / "x.json")])
    finally:
        child.kill()
        child.wait()
    assert code == pc.EXIT_ALREADY_RUNNING and "already running" in capsys.readouterr().out


def test_a_settings_problem_exits_with_its_own_code(tmp_path):
    code = live_trader.main(["--data-dir", str(tmp_path), "--once", "--config", str(tmp_path / "missing.json")])
    assert code == pc.EXIT_CONFIG


def test_process_checks_and_stale_records(tmp_path):
    assert pc.pid_alive(pc.os.getpid())
    finished = subprocess.Popen([sys.executable, "-c", "pass"])
    finished.wait()
    assert not pc.pid_alive(finished.pid) and not pc.pid_alive(0)

    run = pc.run_dir(tmp_path)
    run.mkdir()
    (run / pc.WORKER_FILE).write_text(json.dumps({"pid": finished.pid, "started": 0}))
    assert pc.status(run)["running"] is False  # a record left by a process that died doesn't count
    assert pc.describe_status(pc.status(run)) == "The live trader is not running."

    pc.write_record(run, pc.WORKER_FILE, urls=["http://10.0.0.5:8080/"])
    state = pc.status(run)
    assert state["running"] and "http://10.0.0.5:8080/portal" in pc.describe_status(state)
    pc.remove_record(run, pc.WORKER_FILE)
    assert not (run / pc.WORKER_FILE).exists()


def test_a_stop_request_reaches_a_running_trader(tmp_path):
    run = pc.run_dir(tmp_path)
    stop = threading.Event()
    pc.watch_for_stop(run, stop, interval=0.05)
    assert not stop.wait(0.2)
    pc.request_stop(run)
    assert stop.wait(2)
    pc.clear_stop(run)
    assert not pc.stop_requested(run)


class FakeWorker:
    def __init__(self, codes, pid):
        self.codes, self.pid, self.terminated = list(codes), pid, False

    def poll(self):
        return self.codes.pop(0) if self.codes else None

    def terminate(self):
        self.terminated = True
        self.codes = [1]


def test_the_supervisor_restarts_a_crashed_trader_until_stopped(tmp_path):
    run = pc.run_dir(tmp_path)
    launched, log, slept = [], [], []

    def launch():
        launched.append(1)
        if len(launched) == 3:
            pc.request_stop(run)  # the third trader is asked to stop, and does
            return FakeWorker([None, 0], pid=3)
        return FakeWorker([None, None, 1], pid=len(launched))  # crashes after two seconds

    code = pc.supervise(launch, run, log.append, sleep=slept.append)
    assert len(launched) == 3 and code == 0
    assert sum("restarting in 10s" in line for line in log) == 1 and any("in 20s" in line for line in log)
    assert log[-1] == "the trader stopped (exit code 0)"


@pytest.mark.parametrize("fatal", pc.FATAL_EXITS)
def test_the_supervisor_gives_up_when_a_restart_cannot_help(tmp_path, fatal):
    launched, log = [], []
    code = pc.supervise(lambda: launched.append(1) or FakeWorker([fatal], 1), pc.run_dir(tmp_path), log.append,
                        sleep=lambda s: None)
    assert code == fatal and len(launched) == 1 and "not restarting" in log[-1]


def test_a_trader_that_ignores_a_stop_is_ended(tmp_path):
    run = pc.run_dir(tmp_path)
    clock = [0.0]
    worker = FakeWorker([None] * 1000, pid=1)

    def launch():
        pc.request_stop(run)
        return worker

    def sleep(seconds):
        clock[0] += seconds

    pc.supervise(launch, run, lambda m: None, sleep=sleep, clock=lambda: clock[0], grace=5)
    assert worker.terminated


def test_a_background_process_runs_detached_and_stops_on_request(tmp_path):
    """The real detaching on this platform, with a stand-in for the trader that reports in like one."""
    run = pc.run_dir(tmp_path)
    stand_in = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {ROOT!r})
        import process_control as pc
        run = pc.Path({str(run)!r})
        pc.write_record(run, pc.SUPERVISOR_FILE)
        pc.write_record(run, pc.WORKER_FILE, urls=["http://127.0.0.1:1/"])
        while not pc.stop_requested(run):
            time.sleep(0.1)
        pc.remove_record(run, pc.WORKER_FILE)
        pc.remove_record(run, pc.SUPERVISOR_FILE)
    """)
    state = pc.start_background([sys.executable, "-c", stand_in], run, wait=30)
    try:
        assert state["running"] and state["worker"]["pid"] != pc.os.getpid()
        with pytest.raises(pc.AlreadyRunning):
            pc.start_background([sys.executable, "-c", "pass"], run)
    finally:
        pc.request_stop(run)
        stopped = pc.wait_for_stop(run, timeout=30)
    assert stopped and not pc.stop_requested(run)


def test_stop_when_nothing_runs(tmp_path, capsys):
    assert live_trader.main(["--data-dir", str(tmp_path), "--stop"]) == 0
    assert "not running" in capsys.readouterr().out


def test_background_arguments_are_passed_on_with_absolute_paths(tmp_path):
    args = live_trader._parser().parse_args(["--config", "cfg.json", "--base-dir", ".", "--port", "9000",
                                             "--host", "127.0.0.1", "--ledger", "l.db"])
    passed = live_trader._passthrough(args, live_trader.Locations(args))
    assert passed[passed.index("--port") + 1] == "9000" and passed[passed.index("--host") + 1] == "127.0.0.1"
    for flag in ("--data-dir", "--config", "--base-dir", "--env-file", "--ledger"):
        assert pc.Path(passed[passed.index(flag) + 1]).is_absolute()
    assert time.time()  # keeps the import used on every platform
