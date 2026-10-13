"""Create poster-ready figures from the PPV inspiration experiments."""

from __future__ import annotations

import csv
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np


HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
ANALYSIS = HERE / "analysis"
OUT = ANALYSIS / "poster_figures"

SELECTED_FILES = {
    0.0: ["0.csv"],
    3.5: ["35.csv", "35.1.csv", "35.2.csv", "35.3.csv", "35.4.csv"],
    3.7: ["37.1.csv", "37.2.csv"],  # 37.csv is mislabeled; telemetry stayed at 0 V.
    3.9: ["39.csv", "39.1.csv"],
    4.1: ["41.csv"],
    4.2: ["42.csv"],
}


def configure_style() -> None:
    plt.rcParams.update({
        "font.family": "DejaVu Sans",
        "font.size": 10,
        "axes.titlesize": 12,
        "axes.labelsize": 11,
        "axes.linewidth": 0.9,
        "xtick.labelsize": 9,
        "ytick.labelsize": 9,
        "legend.fontsize": 8.5,
        "lines.linewidth": 2.0,
        "savefig.facecolor": "white",
    })


def aligned_flow(path: Path) -> tuple[np.ndarray, np.ndarray]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = [row for row in list(csv.reader(handle))[1:] if len(row) >= 13 and row[1] and row[2]]
    stamp = np.array([int(row[1]) / 1000.0 for row in rows])
    flow = np.array([float(row[2]) for row in rows])
    atm = np.array([int(row[3]) for row in rows])
    start = int(np.flatnonzero(atm == 1)[0])
    baseline = float(np.median(flow[max(0, start - 100):start]))
    return stamp[start:] - stamp[start], flow[start:] - baseline


def median_curve(files: list[str], grid: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    interpolated = []
    for filename in files:
        time_s, flow_slm = aligned_flow(DATA / filename)
        values = np.interp(grid, time_s, flow_slm)
        values[grid > time_s[-1]] = np.nan
        interpolated.append(values)
    array = np.vstack(interpolated)
    count = np.sum(np.isfinite(array), axis=0)
    median = np.full(grid.shape, np.nan)
    low = np.full(grid.shape, np.nan)
    high = np.full(grid.shape, np.nan)
    valid = count > 0
    median[valid] = np.nanmedian(array[:, valid], axis=0)
    low[valid] = np.nanmin(array[:, valid], axis=0)
    high[valid] = np.nanmax(array[:, valid], axis=0)
    return median, low, high


def save_all(fig: plt.Figure, stem: str) -> None:
    for suffix in ("png", "svg", "pdf"):
        kwargs = {"dpi": 300} if suffix == "png" else {}
        fig.savefig(OUT / f"{stem}.{suffix}", bbox_inches="tight", **kwargs)


def make_flow_figure() -> None:
    grid = np.arange(0.0, 20.001, 0.02)
    colors = plt.get_cmap("viridis")(np.linspace(0.05, 0.92, len(SELECTED_FILES)))
    fig, ax = plt.subplots(figsize=(7.2, 4.2))
    for color, (voltage, files) in zip(colors, SELECTED_FILES.items()):
        median, low, high = median_curve(files, grid)
        ax.plot(grid, median, color=color, label=f"{voltage:g} V", zorder=3)
        if len(files) > 1:
            ax.fill_between(grid, low, high, color=color, alpha=0.13, linewidth=0)
    ax.axvline(0, color="#555555", lw=0.9, ls="--")
    ax.axhline(0, color="#555555", lw=0.7)
    ax.set_xlim(0, 20)
    ax.set_ylim(-2, 56)
    ax.set_xlabel("Time after ATM opening (s)")
    ax.set_ylabel("Baseline-corrected flow (SLM)")
    ax.set_title("Measured inspiration flow at different PPV voltages", weight="bold")
    ax.grid(True, alpha=0.25)
    ax.legend(title="PPV command", ncol=2, frameon=True, loc="upper right")
    fig.tight_layout()
    save_all(fig, "measured_flow_vs_ppv_voltage")
    plt.close(fig)


def read_summary() -> list[dict[str, str]]:
    path = ANALYSIS / "ppv_inspiration_voltage_summary.csv"
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def make_tau_figure() -> None:
    rows = read_summary()
    voltage = np.array([float(row["voltage_v"]) for row in rows])
    tau = np.array([float(row["tau_median_s"]) for row in rows])
    low = np.array([float(row["tau_min_s"]) for row in rows])
    high = np.array([float(row["tau_max_s"]) for row in rows])

    fig, ax = plt.subplots(figsize=(7.2, 4.2))
    ax.axvspan(0, 3.2, color="#94a3b8", alpha=0.13)
    ax.axvspan(3.2, 4.2, color="#22c55e", alpha=0.09)
    ax.errorbar(
        voltage, tau, yerr=np.vstack([tau - low, high - tau]),
        fmt="o", color="#0f6f9f", ecolor="#78aeca", capsize=3.5,
        markersize=5.5, label="Median and repeat range", zorder=4,
    )

    fit_voltage = np.linspace(3.4, 4.1, 200)
    fit_tau = 0.14627904936275105 * (1.0 - fit_voltage / 5.0) ** (-2.30316662671284)
    ax.plot(fit_voltage, fit_tau, "--", color="#d97706", lw=2,
            label=r"Active-range fit: $\gamma_\tau=2.30$")
    ax.axvline(4.2, color="#b91c1c", ls=":", lw=1.6)
    ax.scatter([4.2], [0], marker="x", s=70, linewidth=2, color="#b91c1c", zorder=5)
    ax.annotate("Measured closure\n(4.2 V)", xy=(4.2, 0), xytext=(3.58, 0.75),
                arrowprops={"arrowstyle": "->", "color": "#b91c1c"},
                color="#991b1b", fontsize=9)
    ax.text(0.20, 7.95,
            "Fully open: 1.91 s\n4.0 V: 5.38 s\n4.1 V: 9.41 s\n"
            r"Preliminary combined $\gamma \approx 2.6$",
            fontsize=9,
            bbox={"boxstyle": "round,pad=0.4", "facecolor": "white", "edgecolor": "#cbd5e1", "alpha": 0.94})
    ax.set_xlim(-0.08, 4.32)
    ax.set_ylim(0, 10.6)
    ax.set_xlabel("PPV command voltage (V)")
    ax.set_ylabel(r"Identified inspiration time constant $\tau_{in}$ (s)")
    ax.set_title("Identified first-order dynamics of the physical branch", weight="bold")
    ax.grid(True, alpha=0.25)
    ax.text(1.25, 10.15, "Weak regulation", ha="center", color="#64748b", fontsize=8.5)
    ax.text(3.68, 10.15, "Effective regulation", ha="center", color="#15803d", fontsize=8.5)
    ax.legend(loc="upper center", bbox_to_anchor=(0.62, 0.98), fontsize=8.2)
    fig.tight_layout()
    save_all(fig, "identified_time_constant_vs_ppv_voltage")
    plt.close(fig)


def main() -> None:
    configure_style()
    OUT.mkdir(parents=True, exist_ok=True)
    make_flow_figure()
    make_tau_figure()
    print(OUT)


if __name__ == "__main__":
    main()
