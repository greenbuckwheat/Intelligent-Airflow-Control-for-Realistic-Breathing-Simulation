"""Automated STM32 calibration recorder for MCU-controlled valves or synthetic data.

Run GUI: python lung_calibration_experiments.py
Analyse: python lung_calibration_experiments.py --analyse PATH_TO_RUN
The dedicated calibration firmware owns PPV and routing-valve timing.
Synthetic data are explicitly labelled.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import queue
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np

FIELDS = ["trial", "source", "experiment", "direction", "opening_command",
          "repeat", "sweep_order", "mcu_time_ms", "host_monotonic_s", "time_s",
          "raw_flow_slm", "zero_bias_slm", "flow_slm", "volume_delta_ml",
          "volume_ref_ml", "source_pressure", "lung_pressure", "ppv_voltage_measured",
          "applied_opening", "ppv_command_v", "applied_mask", "sensor_status"]
READY = "READY,LUNGLOGIC_CAL_G474,2"
STAGE_IDLE, STAGE_PRECONDITION, STAGE_DEAD, STAGE_SETTLE, STAGE_RECORD, STAGE_FAULT = range(6)


def parse_flow(line):
    f = line.strip().split(",")
    if len(f) != 10 or f[0] != "CAL":
        return None
    trial, stamp, elapsed_ms, stage = map(int, f[1:5])
    flow, mask, opening, command_v = float(f[5]), int(f[7]), float(f[8]), float(f[9])
    if (not 0 <= trial <= 0xFFFFFFFF or not 0 <= stamp <= 0xFFFFFFFF
            or not 0 <= elapsed_ms <= 120000 or stage not in range(6)
            or mask not in (0, 1, 2) or not 0 <= opening <= 1 or not 0 <= command_v <= 5):
        raise ValueError("Invalid STM32 telemetry")
    status = f[6]
    if status not in ("OK", "STARTING", "I2C", "CRC"):
        raise ValueError("Invalid flow sensor status")
    if status == "OK" and not math.isfinite(flow):
        raise ValueError("Non-finite valid flow")
    if status != "OK" and not math.isnan(flow):
        raise ValueError("Invalid sensor sample must be nan")
    return trial, stamp, elapsed_ms / 1000.0, stage, flow, mask, opening, command_v, status


def make_plan(experiment, openings, repeats):
    if experiment == "full-open":
        return [(direction, 1., repeat, "full-open")
                for repeat in range(1, repeats + 1) for direction in ("ATM", "VAC")]
    return [(direction, opening, repeat, order)
            for direction in ("ATM", "VAC") for repeat in range(1, repeats + 1)
            for order, points in (("ascending", sorted(openings)),
                                  ("descending", sorted(openings, reverse=True)))
            for opening in points]


def analyse(folder):
    from fit_lung_parameters import fit_exponential
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib import pyplot as plt

    folder = Path(folder)
    with (folder / "measurements.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    summaries = []
    aborted_path = folder / "aborted_trials.txt"
    aborted = {int(v) for v in aborted_path.read_text().split()} if aborted_path.exists() else set()
    fig, axes = plt.subplots(2, 1, figsize=(10, 7), sharex=True)
    for trial in sorted({int(r["trial"]) for r in rows}):
        if trial in aborted:
            continue
        part = [r for r in rows if int(r["trial"]) == trial]
        if len(part) < 2:
            continue
        t = np.array([float(r["time_s"]) for r in part])
        q = np.array([float(r["flow_slm"]) for r in part])
        v = np.array([float(r["volume_delta_ml"]) for r in part])
        ref_available = all(r["volume_ref_ml"] != "" for r in part)
        ref = np.array([float(r["volume_ref_ml"]) for r in part]) if ref_available else None
        label = f'{trial}: {part[0]["direction"]} u={part[0]["opening_command"]}'
        axes[0].plot(t, q, label=label)
        axes[1].plot(t, v, label=label)
        window = (t >= .1) & (t <= .3)
        peak = float(max(abs(q)))
        onset_threshold = max(.5, .05 * peak)
        onset = np.flatnonzero(abs(q) >= onset_threshold)
        result = {key: part[0][key] for key in ("trial", "source", "experiment", "direction", "opening_command", "repeat", "sweep_order")}
        result.update(samples=len(t), duration_s=float(t[-1]), peak_abs_flow_slm=peak,
                      signed_volume_change_ml=float(v[-1]),
                      early_mean_flow_slm=float(q[window].mean()) if window.any() else None,
                      volume_basis="independent reference" if ref_available else "flow integral; relative change only",
                      command_to_flow_delay_s=float(t[onset[0]]) if len(onset) else None,
                      flow_onset_threshold_slm=onset_threshold)
        if ref_available:
            result["integral_reference_rmse_ml"] = float(np.sqrt(np.mean((v - (ref - ref[0])) ** 2)))
        if part[0]["experiment"] == "full-open" and len(t) >= 8:
            if np.ptp(ref if ref_available else v) > 1e-6:
                fit = fit_exponential(t, ref if ref_available else v)
                result["exponential_fit"] = {k: value for k, value in fit.items() if not isinstance(value, np.ndarray) and math.isfinite(value)}
            if ref_available and part[0]["direction"] == "ATM":
                hits = np.flatnonzero(ref >= 250)
                result["time_to_250ml_from_record_start_s"] = float(t[hits[0]]) if len(hits) else None
        summaries.append(result)
    for ax, ylabel in zip(axes, ("Corrected flow / SLM", "Integrated volume change / mL")):
        ax.set_ylabel(ylabel)
        ax.grid(True, alpha=.3)
    if len(summaries) <= 12:
        axes[0].legend(fontsize=7)
    axes[1].set_xlabel("Time from first recorded sample / s")
    fig.tight_layout()
    fig.savefig(folder / "response_curves.png", dpi=160)
    plt.close(fig)
    if summaries:
        fig, ax = plt.subplots(figsize=(8, 5))
        for direction in ("ATM", "VAC"):
            for order in ("ascending", "descending"):
                points = [s for s in summaries if s["direction"] == direction and s["sweep_order"] == order and s["early_mean_flow_slm"] is not None]
                if points:
                    ax.scatter([float(s["opening_command"]) for s in points], [abs(s["early_mean_flow_slm"]) for s in points], label=f"{direction} {order}")
        ax.set(xlabel="Opening command (not measured position)", ylabel="Absolute mean flow, 0.1–0.3 s / SLM")
        if ax.collections:
            ax.legend()
        ax.grid(True, alpha=.3)
        fig.tight_layout()
        fig.savefig(folder / "ppv_sweep.png", dpi=160)
        plt.close(fig)
    report = {"notes": ["Dedicated STM32 firmware timestamps the RECORD stage and owns all valve commands.",
                         "Synthetic rows are not physical measurements.",
                         "Integral is signed relative volume, not absolute lung volume. SLM uses standard-volume units.",
                         "Early-window flow and ascending/descending differences require matched initial volume and pressure to interpret.",
                         "Command voltage is requested voltage, not an independently measured PPV input.",
                         "No gamma fit is inferred without verified matched conditions; no automatic parameter update."], "aborted_trials_excluded": sorted(aborted), "trials": summaries}
    (folder / "analysis.json").write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    return folder / "analysis.json"


class App:
    def __init__(self, root):
        import tkinter as tk
        from tkinter import ttk
        self.root = root
        root.title("LungLogic — automated STM32 calibration experiments")
        self.events = queue.Queue()
        self.connection = None
        self.stop_reader = threading.Event()
        self.ready = False
        self.recording = False
        self.plan = []
        self.index = 0
        self.baseline = []
        self.zero_bias = None
        self.last_received = None
        self.last_ping = 0.0
        self.folder = None
        self.handle = None
        self.source = tk.StringVar(value="Synthetic")
        self.port = tk.StringVar(value="COM3")
        self.experiment = tk.StringVar(value="full-open")
        self.duration = tk.StringVar(value="5")
        self.precondition = tk.StringVar(value="5")
        self.dead_time = tk.StringVar(value="0.10")
        self.ppv_settle = tk.StringVar(value="1.0")
        self.repeats = tk.StringVar(value="3")
        self.openings = tk.StringVar(value="0,0.2,0.4,0.6,0.8,1")
        self.sign = tk.StringVar(value="1")
        self.conditions = tk.StringVar(value="Record lung ID, tubing, vacuum pressure and reference-volume method here")
        self.status = tk.StringVar(value="Hardware mode requires the dedicated automatic calibration firmware.")
        frame = ttk.Frame(root, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="两个标定实验 · STM32 自动控制 PPV/ATM/VAC", font=("Arial", 15)).grid(row=0, column=0, columnspan=3, sticky="w")
        for row, (label, variable, options) in enumerate([
            ("Data source", self.source, ["Synthetic", "STM32 calibration (automatic valves)"]),
            ("Serial port", self.port, None), ("Experiment", self.experiment, ["full-open", "ppv-sweep"]),
            ("Duration per trial / s", self.duration, None), ("Repeats", self.repeats, None),
            ("Opening commands / 0–1", self.openings, None), ("Flow sign / ±1", self.sign, None),
            ("Opposite-route precondition / s", self.precondition, None),
            ("Both-valves-closed dead time / s", self.dead_time, None),
            ("PPV settle with routes closed / s", self.ppv_settle, None),
            ("Conditions / notes", self.conditions, None)], 1):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=4)
            widget = ttk.Combobox(frame, textvariable=variable, values=options, state="readonly") if options else ttk.Entry(frame, textvariable=variable, width=65)
            widget.grid(row=row, column=1, columnspan=2, sticky="ew")
        self.buttons = []
        for row, (label, action) in enumerate([
            ("1. Connect / enable synthetic", self.connect),
            ("2. Zero: both paths closed, then wait 2 s", self.zero),
            ("3. Create experiment plan", self.prepare),
            ("4. Run next automatic trial", self.start),
            ("Stop recording / abort (MCU closes valves)", self.abort),
            ("Finish & analyse saved data", self.finish)], 12):
            button = ttk.Button(frame, text=label, command=lambda action=action: self.guarded(action))
            button.grid(row=row, column=0, columnspan=3, sticky="ew", pady=3)
        ttk.Label(frame, textvariable=self.status, wraplength=650).grid(row=18, column=0, columnspan=3, sticky="w", pady=12)
        ttk.Label(frame, text="STM32 自动执行：反向气路预处理 → 两阀关闭 → PPV 定位 → 目标气路记录 → 全部关闭。\n硬件采集不包含压力、独立参考体积或 PPV 实测电压；对应 CSV 列留空。", wraplength=650).grid(row=19, column=0, columnspan=3, sticky="w")
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(20, self.tick)

    def guarded(self, action):
        from tkinter import messagebox
        try:
            action()
        except Exception as exc:
            messagebox.showerror("Experiment", str(exc))

    def connect(self):
        if self.recording or self.handle:
            raise ValueError("Finish the current session before reconnecting")
        self.disconnect()
        while not self.events.empty():
            self.events.get_nowait()
        self.zero_bias = None
        self.mode = self.source.get()
        if self.mode == "Synthetic":
            self.ready = True
            self.status.set("Synthetic source ready; perform zero calibration next.")
            return
        import serial
        self.connection = serial.Serial(self.port.get().strip(), 115200, timeout=.1, write_timeout=.2)
        self.stop_reader = threading.Event()
        connection, stop = self.connection, self.stop_reader
        def read():
            try:
                while not stop.is_set():
                    line = connection.readline().decode("ascii", errors="replace").strip()
                    if line:
                        self.events.put((time.monotonic(), line))
            except Exception as exc:
                self.events.put((time.monotonic(), "ERROR:" + str(exc)))
        threading.Thread(target=read, daemon=True).start()
        self.handshake_due = time.monotonic() + 5
        self.connection.write(b"HELLO\n")
        self.last_ping = time.monotonic()
        self.status.set("Waiting for dedicated STM32 calibration firmware...")

    def zero(self):
        if not self.ready or self.recording or self.handle:
            raise ValueError("Connect first; zero before creating a session")
        from tkinter import messagebox
        if not messagebox.askokcancel("Zero", "MCU 将关闭 ATM/VAC 并命令 PPV 全闭。确认气路已经静止后点击 OK；程序先等待 0.5 秒，再采集 2 秒零偏。"):
            return
        if self.connection:
            self.connection.write(b"STOP\n")
        self.baseline = []
        self.zero_collect_after = time.monotonic() + .5
        self.zero_due = self.zero_collect_after + 2
        self.zero_bias = None
        self.status.set("Closing valves and waiting for zero-flow settling...")

    def prepare(self):
        if not self.ready or self.zero_bias is None or self.handle:
            raise ValueError("Connect and zero first; finish any existing session")
        duration = float(self.duration.get())
        precondition = float(self.precondition.get())
        dead_time = float(self.dead_time.get())
        ppv_settle = float(self.ppv_settle.get())
        repeats = int(self.repeats.get())
        sign = int(self.sign.get())
        openings = [float(v) for v in self.openings.get().split(",")]
        if not math.isfinite(duration) or not .5 <= duration <= 120 or not 1 <= repeats <= 100 or sign not in (-1, 1):
            raise ValueError("Use duration 0.5–120 s, repeats 1–100, sign ±1")
        if (not 0 <= precondition <= 30 or not .02 <= dead_time <= 2
                or not .1 <= ppv_settle <= 15):
            raise ValueError("Use precondition 0–30 s, dead time 0.02–2 s, PPV settle 0.1–15 s")
        if not openings or any(not math.isfinite(u) or not 0 <= u <= 1 for u in openings):
            raise ValueError("Opening commands must be between 0 and 1")
        self.seconds, self.flow_sign, self.kind = duration, sign, self.experiment.get()
        self.precondition_s, self.dead_time_s, self.ppv_settle_s = precondition, dead_time, ppv_settle
        self.plan = make_plan(self.kind, openings, repeats)
        self.index = 0
        self.folder = Path(__file__).parent / "output" / "calibration_experiments" / datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
        self.folder.mkdir(parents=True)
        metadata = {"source": self.mode, "experiment": self.kind, "duration_s": duration,
                    "precondition_s": precondition, "dead_time_s": dead_time, "ppv_settle_s": ppv_settle,
                    "zero_bias_slm": self.zero_bias, "flow_sign": sign, "notes": self.conditions.get(),
                    "created_at": datetime.now().astimezone().isoformat(),
                    "control": "STM32 owns PPV and routing-valve timing", "plan": self.plan}
        (self.folder / "session.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")
        self.handle = (self.folder / "measurements.csv").open("w", newline="", encoding="utf-8")
        self.writer = csv.DictWriter(self.handle, fieldnames=FIELDS)
        self.writer.writeheader()
        self.handle.flush()
        self.status.set(f"Plan ready: {len(self.plan)} trials. {self.folder}")

    def start(self):
        from tkinter import messagebox
        if self.recording or not self.handle or self.index >= len(self.plan) or not self.ready:
            raise ValueError("Create a plan first; only one trial can run at a time")
        direction, opening, repeat, order = self.plan[self.index]
        if not messagebox.askokcancel("Run automatic trial", f"Trial {self.index + 1}/{len(self.plan)}: {direction}, opening {opening:.0%}, repeat {repeat}, {order}\n\nSTM32 将自动预处理初始状态、关闭两路、设置 PPV、打开目标气路并在结束时关闭全部执行器。\n确认源压力和软管连接正确后点击 OK。"):
            return
        self.trial = direction, opening, repeat, order
        self.started = time.monotonic()
        self.first_stamp = self.previous_stamp = None
        self.previous_q = None
        self.volume = 0.
        self.sample_count = 0
        self.recording = True
        self.last_received = self.started
        if self.connection:
            direction_code = 1 if direction == "ATM" else -1
            command = (f"RUN,{self.index + 1},{direction_code},{round(opening * 1000)},"
                       f"{round(self.precondition_s * 1000)},{round(self.dead_time_s * 1000)},"
                       f"{round(self.ppv_settle_s * 1000)},{round(self.seconds * 1000)}")
            self.connection.write((command + "\n").encode("ascii"))
            self.status.set(f"STM32 preparing trial {self.index + 1}: {direction} {opening:.0%}")
        else:
            self.status.set(f"Recording synthetic trial {self.index + 1}: {direction} {opening:.0%}")

    def sample(self, host, stamp, raw, mask, sample_time=None, applied_opening="", command_v=""):
        self.last_received = host
        if hasattr(self, "zero_due") and host >= self.zero_collect_after:
            self.baseline.append(raw)
        if not self.recording or host < self.started:
            return
        direction, opening, repeat, order = self.trial
        q = self.flow_sign * (raw - self.zero_bias)
        if self.first_stamp is None:
            self.first_stamp = stamp
        if self.previous_stamp is not None:
            dt = ((stamp - self.previous_stamp) & 0xFFFFFFFF) / 1000.
            if dt <= 0 or dt > .25:
                self.abort()
                self.status.set("Aborted: non-monotonic timestamp or sampling gap >250 ms. Close valves manually.")
                return
            self.volume += .5 * (q + self.previous_q) * dt * 1000 / 60
        t = sample_time if sample_time is not None else ((stamp - self.first_stamp) & 0xFFFFFFFF) / 1000
        self.previous_stamp, self.previous_q = stamp, q
        ref = ""
        if self.mode == "Synthetic":
            effective = opening ** 1.15
            ref = 250 * (1 - math.exp(-effective * t / .57)) if direction == "ATM" else 250 * math.exp(-effective * t / .5)
        self.writer.writerow(dict(zip(FIELDS, [self.index + 1, self.mode, self.kind, direction, opening,
            repeat, order, stamp, host, t, raw, self.zero_bias, q, self.volume, ref, "", "", "",
            applied_opening, command_v, mask, "OK"])))
        self.sample_count += 1
        if self.sample_count % 25 == 0:
            self.handle.flush()
        self.status.set(f"Trial {self.index + 1}: {t:.2f} s, Q={q:.2f} SLM, ΔV={self.volume:.1f} mL (integrated)")

    def tick(self):
        try:
            now = time.monotonic()
            while not self.events.empty():
                host, line = self.events.get_nowait()
                if line == READY:
                    self.ready = True
                    self.last_received = host
                    self.status.set("STM32 calibration firmware ready; all outputs are safe/off.")
                elif line.startswith("STARTED,"):
                    self.status.set(f"STM32 executing automatic trial {self.index + 1}.")
                elif line.startswith("DONE,"):
                    done = int(line.split(",", 1)[1])
                    if self.recording and done == self.index + 1:
                        self.recording = False
                        self.handle.flush()
                        self.index += 1
                        self.status.set(f"Trial saved ({self.sample_count} samples). MCU closed ATM/VAC and PPV. Ready for next trial.")
                        self.root.bell()
                elif line.startswith(("ERROR:", "ERR,", "FAULT,")):
                    raise ValueError(line)
                elif line.startswith("CAL,") and self.ready:
                    trial, stamp, elapsed, stage, raw, mask, applied, command_v, status = parse_flow(line)
                    self.last_received = host
                    if status == "OK":
                        if hasattr(self, "zero_due"):
                            self.sample(host, stamp, raw, mask)
                        elif self.recording and trial == self.index + 1 and stage == STAGE_RECORD:
                            self.sample(host, stamp, raw, mask, elapsed, applied, command_v)
            if self.ready and self.mode == "Synthetic":
                raw = .08
                if self.recording:
                    direction, opening, _, _ = self.trial
                    t = now - self.started
                    effective = opening ** 1.15
                    tau = .57 if direction == "ATM" else .5
                    raw += (1 if direction == "ATM" else -1) * .06 * 250 / tau * effective * math.exp(-effective * t / tau)
                self.sample(now, int(now * 1000) & 0xFFFFFFFF, raw, 0)
            if self.connection and self.ready and now - self.last_ping >= .5:
                self.connection.write(b"PING\n")
                self.last_ping = now
            if self.connection and not self.ready and now > self.handshake_due:
                raise ValueError("Handshake timed out; requires single_lung_calibration_stm32 firmware")
            if self.ready and self.mode != "Synthetic" and self.last_received and now - self.last_received > 1:
                raise ValueError("No fresh flow data for 1 second")
            if hasattr(self, "zero_due") and now >= self.zero_due:
                del self.zero_due
                if len(self.baseline) < 20:
                    raise ValueError("Too few samples for zero calibration")
                if np.std(self.baseline) > .2:
                    raise ValueError("Unstable baseline; verify MCU outputs are closed and repeat zero calibration")
                self.zero_bias = float(np.mean(self.baseline))
                self.status.set(f"Zero bias = {self.zero_bias:.4f} SLM. Create a plan next.")
            if self.mode == "Synthetic" and self.recording and now - self.started >= self.seconds:
                self.recording = False
                self.handle.flush()
                self.index += 1
                self.status.set(f"Synthetic trial saved ({self.sample_count} samples). Prepare next trial or finish.")
                self.root.bell()
        except Exception as exc:
            self.abort()
            self.disconnect()
            self.status.set(f"Stopped: {exc}. STOP sent; MCU safe outputs requested. Data retained.")
        self.root.after(20, self.tick)

    def abort(self):
        if self.connection:
            try:
                self.connection.write(b"STOP\n")
            except Exception:
                pass
        if self.recording:
            self.recording = False
            self.index += 1
            with (self.folder / "aborted_trials.txt").open("a", encoding="utf-8") as handle:
                handle.write(f"{self.index}\n")
        if self.handle:
            self.handle.flush()
        self.status.set("Recording stopped. MCU STOP requested; partial trial retained and marked aborted.")

    def finish(self):
        if self.recording:
            raise ValueError("Stop the active MCU trial first")
        if not self.handle:
            raise ValueError("No session to finish")
        self.handle.close()
        self.handle = None
        self.status.set("Saved. Analysing...")
        path = analyse(self.folder)
        self.status.set(f"Saved CSV, session metadata, plots and analysis: {path}")

    def disconnect(self):
        self.stop_reader.set()
        if self.connection:
            try:
                self.connection.write(b"STOP\n")
            finally:
                self.connection.close()
                self.connection = None
        self.ready = False

    def close(self):
        self.abort()
        try:
            self.disconnect()
        finally:
            if self.handle:
                self.handle.close()
            self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analyse", type=Path, help="analyse a saved session directory")
    args = parser.parse_args()
    if args.analyse:
        print(analyse(args.analyse))
    else:
        import tkinter as tk
        root = tk.Tk()
        App(root)
        root.mainloop()


if __name__ == "__main__":
    main()
