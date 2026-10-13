"""Reproducible ATM/VAC presentation figures from the 2026-10-07 sweep.

Uses previously screened cumulative-volume fits; never invents missing tails.
"""
import csv
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "analysis" / "today_hardware"
OUT = ROOT / "analysis" / "ppt_assets_today_atm_vac"
VOLTAGES = [0, 1, 2, 3, 3.2, 3.5, 3.6, 3.7, 3.75, 3.85, 3.9, 4, 4.1]
COLORS = ["#7040a0", "#1976b8", "#2ca02c", "#ed1c24", "#ff8300",
          "#91c7df", "#b55a24", "#159d83", "#e83091", "#8178ba",
          "#dc6700", "#6ca51b", "#e5ac00"]


def save(fig, name):
    fig.savefig(OUT / f"{name}.png", dpi=220, facecolor=fig.get_facecolor())
    fig.savefig(OUT / f"{name}.svg", facecolor=fig.get_facecolor())
    plt.close(fig)


def samples(row):
    with (ROOT / "data" / row["file"]).open(encoding="utf-8-sig", newline="") as f:
        data = [r for r in csv.DictReader(f) if not r["操作事件"] and r["MCU时间戳ms"]]
    t = np.array([int(r["MCU时间戳ms"]) for r in data]) / 1000
    t -= t[0]
    q = np.array([float(r["流量SLM"]) for r in data])
    mask = (t >= row["start_s"] - 1e-7) & (t <= row["end_s"] + 1e-7)
    return t[mask] - row["start_s"], q[mask] - row["baseline_slm"]


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    with (SOURCE / "screened_segments.csv").open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        for key in row:
            if key not in ("file", "usable", "reason"):
                row[key] = float(row[key]) if row[key] else None
        row["usable"] = row["usable"] == "True"
    summary = json.loads((SOURCE / "model_summary.json").read_text(encoding="utf-8"))
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 13,
                         "axes.labelcolor": "#29435c", "xtick.color": "#4a6883",
                         "ytick.color": "#4a6883", "axes.edgecolor": "#9db3cc",
                         "grid.color": "#dbe5ef", "svg.fonttype": "none"})
    manifest = []
    for state, valve, direction in ((1, "ATM", "inspiration"), (2, "VAC", "expiration")):
        usable = [r for r in rows if r["state"] == state and r["usable"]]
        fig, ax = plt.subplots(figsize=(14, 7.3), facecolor="#f5f8fc")
        for voltage, color in zip(VOLTAGES, COLORS):
            candidates = [r for r in usable if abs(r["voltage"] - voltage) < 1e-6]
            if not candidates:
                continue
            median = float(np.median([r["tau_s"] for r in candidates]))
            representative = min(candidates, key=lambda r: (abs(r["tau_s"] - median), -r["duration_s"]))
            t, q = samples(representative)
            ax.plot(t, q, color=color, lw=2.1, label=f"{voltage:g} V")
            manifest.append(dict(direction=direction, voltage=voltage, file=representative["file"],
                                 segment=int(representative["segment"]), start_s=representative["start_s"],
                                 duration_s=representative["duration_s"], tau_s=representative["tau_s"],
                                 median_tau_s=median, baseline_slm=representative["baseline_slm"]))
        closed = [r for r in rows if r["state"] == state and r["voltage"] == 4.2]
        if closed:
            t, q = samples(max(closed, key=lambda r: r["duration_s"]))
            ax.plot(t, q, "--", color="#333333", lw=2, label="4.2 V: short closed observation")
        ax.axhline(0, color="#7b8d9e", lw=.8)
        ax.set(xlim=(0, 20), xlabel=f"Time after {valve} ON telemetry / s",
               ylabel="Baseline-corrected signed flow / SLM",
               title=f"{valve} opening — {direction.capitalize()} | 7 Oct 2026")
        ax.grid(True, alpha=.9)
        ax.legend(ncol=4, fontsize=10, loc="best", framealpha=.95)
        fig.text(.075, .025, "One screened trial per voltage, nearest median tau; raw flow, no smoothing. "
                 "Recorded segments only; 4.15 V excluded.", fontsize=10, color="#526b82")
        fig.tight_layout(rect=(0, .055, 1, 1))
        save(fig, f"representative_flow_curves_{valve.lower()}")

        model = next(m for m in summary["models"][direction]["closure_sensitivity"]
                     if m["assumed_closure_v"] == 4.2)
        fig, ax = plt.subplots(figsize=(13, 7), facecolor="#f5f8fc")
        u = np.array([r["voltage"] for r in usable])
        tau = np.array([r["tau_s"] for r in usable])
        ax.axvspan(0, model["dead_zone_end_v"], color="#dfe9f4", alpha=.6,
                   label="Fitted low-voltage plateau")
        ax.scatter(u, tau, s=38, color="#ffac64", edgecolors="#ed9a46", alpha=.5,
                   label=f"Screened individual trials (n={len(usable)})")
        voltages = sorted(set(u))
        medians = [np.median(tau[u == v]) for v in voltages]
        ax.plot(voltages, medians, "o-", color="#aa4d00", lw=2.4, ms=7, label="Median at each voltage")
        dense = np.linspace(0, max(voltages), 600)
        opening = np.clip((4.2 - dense) / (4.2 - model["dead_zone_end_v"]), 1e-6, 1)
        ax.plot(dense, model["tau0_s"] / opening ** model["gamma"], "--",
                color="#17374e", lw=2.5, label="Piecewise descriptive fit (Uc fixed at 4.2 V)")
        ax.axvline(4.2, color="#e14968", ls=":", lw=2.5, label="4.2 V: short closed observation")
        ax.set(xlim=(-.05, 4.35), ylim=(0, max(max(medians)*1.18, max(tau)*1.08)),
               xlabel="PPV command voltage / V", ylabel=f"Effective {direction} time constant / s",
               title=f"{valve} opening — {direction.capitalize()} time constant | 7 Oct 2026")
        ax.grid(True, alpha=.9)
        ax.legend(fontsize=10, loc="upper left")
        fig.text(.075, .025,
                 f"tau0 = {model['tau0_s']:.2f} s; Ud = {model['dead_zone_end_v']:.2f} V; "
                 f"gamma = {model['gamma']:.2f}. 4.15 / 4.2 V: no reliable tau included. "
                 "Effective model, not isolated valve travel delay.", fontsize=10, color="#526b82")
        fig.tight_layout(rect=(0, .055, 1, 1))
        save(fig, f"tau_voltage_fit_{valve.lower()}")
    (OUT / "representative_trials.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (OUT / "README.md").write_text(
        "# 2026-10-07 ATM / VAC figures\n\n"
        "使用今天29份开环电压扫描数据，复用 today_hardware/screened_segments.csv 的筛选和累积体积拟合。"
        "并非闭环 hardware run 的瞬态波形。\n\n"
        "ATM为正流量吸气；VAC为负流量呼气，负压源约40–45 kPa（用户提供，未实测压力）。"
        "两方向同一电压颜色一致。代表曲线选取该电压合格段中最接近时间常数中位数的单段，"
        "不是平均曲线；原始流量仅减去初始基线，不平滑、不延长未记录尾部。横轴零点为首次ON遥测，"
        "不是真实阀芯到位时间。波形显示前20秒，完整段时长见代表段清单。\n\n"
        "筛选：ATM积分≥800 mL、末0.5 s流量≤1 SLM且时长≥3tau；VAC积分幅值≥800 mL，"
        "早期流量幅值中位数在4 V以下≥30 SLM、4–4.1 V≥8 SLM。"
        "此筛选不能证明阀门已完全打开。4.15 V泄漏/初态影响大，不用于拟合；"
        "4.2 V仅绘制实际记录的短时近零流量，不虚构无限长零流量或有限tau。\n\n"
        "时间常数模型：tau=tau0/a_eff^gamma，a_eff=clip((Uc-U)/(Uc-Ud),0,1)，"
        "固定Uc=4.2 V，吸呼气独立拟合。参数依赖泄漏、初态和闭合电压假设，不代表纯PPV执行延迟。\n\n"
        "PNG适合PPT，SVG为可缩放版本。representative_trials.json保留每条曲线的文件及段索引。\n",
        encoding="utf-8")
    print(OUT)
    print(f"Generated 4 PNG + 4 SVG; {len(manifest)} representative traces.")


if __name__ == "__main__":
    main()
