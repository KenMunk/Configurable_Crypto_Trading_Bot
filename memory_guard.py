"""Keep a simulation run's memory use under a budget, so a big run can't exhaust the computer's memory.

Every run keeps each result's chart data (price, model value and indicator series), and a stress test
of many models makes hundreds of thousands of results: enough to fill the memory and stop the computer
responding. A MemoryGuard gives the run a budget, by default 80% of the memory that was free when the
simulator launched, and measures the run against it as the fall in free memory since then (so the
worker processes count too). As the run approaches the budget, the guard steps in twice:

    at THIN_AT of the budget   the results keep THIN_POINTS evenly spaced points of chart data each,
                               the ones already collected included; returns, trades and reports are
                               unchanged, as only the charts read the series
    at the budget              the run stops starting new work, keeps what it has, and says why

Free memory is read from the operating system (no extra package is needed): GlobalMemoryStatusEx on
Windows, /proc/meminfo on Linux, sysconf elsewhere. Where it can't be read, the guard does nothing.
"""

import ctypes
import os
import sys

DEFAULT_PERCENT = 80
THIN_AT = 0.75
THIN_POINTS = 2000


def available_memory():
    """Bytes of physical memory free now, or None where it can't be read."""
    if sys.platform == "win32":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total_physical", ctypes.c_ulonglong),
                        ("available_physical", ctypes.c_ulonglong), ("total_page_file", ctypes.c_ulonglong),
                        ("available_page_file", ctypes.c_ulonglong), ("total_virtual", ctypes.c_ulonglong),
                        ("available_virtual", ctypes.c_ulonglong), ("available_extended", ctypes.c_ulonglong)]
        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        return status.available_physical if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)) else None
    try:
        with open("/proc/meminfo", encoding="ascii") as meminfo:
            for line in meminfo:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    try:
        return os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, OSError, ValueError):
        return None


def format_bytes(count):
    return f"{count / 2 ** 30:.1f} GB"


def thin_series(result, points=None):
    """Keep `points` (default THIN_POINTS) evenly spaced candles, the last always, of the result's chart
    series, in place."""
    points = points or THIN_POINTS
    series = result.get("series")
    if not series or len(series.get("time", ())) <= points:
        return result
    length = len(series["time"])
    keep = sorted({round(i * (length - 1) / (points - 1)) for i in range(points)})
    pick = lambda values: [values[i] for i in keep]  # noqa: E731  (lists and numpy arrays alike)
    for key, values in list(series.items()):
        if key == "indicators":
            series[key] = {name: pick(line) for name, line in values.items()}
        elif len(values) == length:
            series[key] = pick(values)
    result["series_thinned"] = True
    return result


class MemoryGuard:
    """A run's memory budget: `percent` of `baseline`, the bytes free when the simulator launched
    (measured now when not given). `measure` reads the bytes free now (see available_memory)."""

    def __init__(self, percent=DEFAULT_PERCENT, baseline=None, measure=available_memory):
        if not 0 < percent <= 100:
            raise ValueError(f"The memory limit must be a percentage above 0 and at most 100, not {percent!r}")
        self.measure = measure
        self.percent = percent
        self.baseline = baseline if baseline is not None else measure()
        self.budget = None if not self.baseline else self.baseline * percent / 100
        self.thinned = False  # results keep only THIN_POINTS of chart data from here on
        self.stopped = None  # why the run stopped early, if it did
        self.peak = 0

    def used(self):
        """Bytes the free memory has fallen by since launch (0 when it can't be read)."""
        now = self.measure()
        if now is None or not self.baseline:
            return 0
        used = max(0, self.baseline - now)
        self.peak = max(self.peak, used)
        return used

    def check(self):
        """"ok", "thin" (past THIN_AT of the budget) or "stop" (at the budget), from memory in use now."""
        if not self.budget:
            return "ok"
        share = self.used() / self.budget
        return "stop" if share >= 1 else "thin" if share >= THIN_AT else "ok"

    def admit(self, new_results, collected=()):
        """Check the memory after results came in: thin `new_results` (and, the first time, the results
        `collected` before them) once past THIN_AT, and return False when the run must stop."""
        state = self.check()
        if state != "ok" and not self.thinned:
            self.thinned = True
            for result in collected:
                thin_series(result)
        if self.thinned:
            for result in new_results:
                thin_series(result)
        if state == "stop" and self.stopped is None:
            self.stopped = (f"stopped at the memory limit: {format_bytes(self.used())} in use of the "
                            f"{format_bytes(self.budget)} allowed ({self.percent:g}% of the "
                            f"{format_bytes(self.baseline)} free at launch)")
        return state != "stop"

    def describe(self):
        """A note for the run's status line, or "" when the guard didn't step in."""
        notes = []
        if self.thinned:
            notes.append(f"charts kept {THIN_POINTS:,} points per result to stay under the memory limit")
        if self.stopped:
            notes.append(self.stopped)
        return "; ".join(notes)
