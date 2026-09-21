import json
import os
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import ttk, messagebox
except ImportError:  # pragma: no cover - GUI is optional in headless env
    tk = None
    ttk = None
    messagebox = None


class SimulationProfile:
    @staticmethod
    def from_json(path):
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload


class SimulationWindow:
    def __init__(self, root):
        self.root = root
        self.root.title("Quant Bot Simulator")
        self.root.geometry("900x620")
        self.root.minsize(760, 500)

        self.config_path_var = tk.StringVar(value="config/btc_macro_horizon.json")
        self.result_var = tk.StringVar(value="Simulation status: idle")

        self._build_ui()

    def _build_ui(self):
        main = ttk.Frame(self.root, padding=12)
        main.pack(fill="both", expand=True)

        ttk.Label(main, text="Configuration File", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        top_row = ttk.Frame(main)
        top_row.pack(fill="x", pady=(6, 12))

        ttk.Entry(top_row, textvariable=self.config_path_var, width=80).pack(side="left", fill="x", expand=True)
        ttk.Button(top_row, text="Browse", command=self._browse_config).pack(side="left", padx=(8, 0))

        action_row = ttk.Frame(main)
        action_row.pack(fill="x", pady=(0, 12))
        ttk.Button(action_row, text="Load Profile", command=self._load_profile).pack(side="left")
        ttk.Button(action_row, text="Run Simulation", command=self._run_simulation).pack(side="left", padx=(8, 0))

        ttk.Label(main, text="Metrics", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.metrics_text = tk.Text(main, height=18, wrap="word", bg="#f5f5f5")
        self.metrics_text.pack(fill="both", expand=True, pady=(6, 10))

        ttk.Label(main, text="Status", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        ttk.Label(main, textvariable=self.result_var, foreground="#1a5d1a").pack(anchor="w", pady=(4, 0))

    def _browse_config(self):
        from tkinter import filedialog

        chosen = filedialog.askopenfilename(
            title="Select simulation config",
            defaultextension=".json",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
        )
        if chosen:
            self.config_path_var.set(chosen)

    def _load_profile(self):
        try:
            config_path = Path(self.config_path_var.get())
            profile = SimulationProfile.from_json(config_path)
            summary = json.dumps(profile, indent=2)
            self.metrics_text.delete("1.0", tk.END)
            self.metrics_text.insert("1.0", summary)
            self.result_var.set("Simulation status: loaded profile")
        except Exception as exc:
            self.result_var.set(f"Simulation status: error - {exc}")
            if messagebox is not None:
                messagebox.showerror("Simulation Load Error", str(exc))

    def _run_simulation(self):
        try:
            config_path = Path(self.config_path_var.get())
            profile = SimulationProfile.from_json(config_path)
            ticker = profile.get("ticker", "UNKNOWN")
            strategy = profile.get("strategy_horizon", "UNSPECIFIED")
            indicators = profile.get("indicators", {})
            safeguards = profile.get("safeguards", {})

            metrics = [
                f"Ticker: {ticker}",
                f"Strategy Horizon: {strategy}",
                "",
                "Indicators:",
            ]
            for key, value in indicators.items():
                metrics.append(f"- {key}: {value}")
            metrics.append("")
            metrics.append("Safeguards:")
            for key, value in safeguards.items():
                metrics.append(f"- {key}: {value}")

            self.metrics_text.delete("1.0", tk.END)
            self.metrics_text.insert("1.0", "\n".join(metrics))
            self.result_var.set(f"Simulation status: running {ticker} profile")
        except Exception as exc:
            self.result_var.set(f"Simulation status: error - {exc}")
            if messagebox is not None:
                messagebox.showerror("Simulation Run Error", str(exc))


def main():
    if tk is None:
        raise RuntimeError("tkinter is required for the simulator GUI.")

    root = tk.Tk()
    SimulationWindow(root)
    root.mainloop()


if __name__ == "__main__":
    main()
