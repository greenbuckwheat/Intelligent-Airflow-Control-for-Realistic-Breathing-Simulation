"""Build a Chinese group-meeting PowerPoint for the two PPV bench experiments."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib import font_manager
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Inches, Pt


HERE = Path(__file__).resolve().parent
EXPERIMENT = HERE.parent
DATA = HERE / "data"
ANALYSIS = HERE / "analysis"
ASSETS = ANALYSIS / "ppt_assets"
OUTPUT = EXPERIMENT / "PPV40V_两组实验_组会汇报_2026-10-06.pptx"

ELECTRICAL_1 = EXPERIMENT / "PPV40_PWM扫描_up1.csv"
ELECTRICAL_2 = EXPERIMENT / "PPV40_PWM扫描_2.csv"
TRIAL_AUDIT = ANALYSIS / "ppv_detailed_trial_audit.csv"
VOLTAGE_SUMMARY = ANALYSIS / "ppv_detailed_voltage_summary.csv"
MODEL_RESULTS = ANALYSIS / "ppv_detailed_model_results.json"


BG = "F6F8FC"
WHITE = "FFFFFF"
NAVY = "102A43"
BLUE = "2364AA"
TEAL = "169C89"
ORANGE = "F28E2B"
RED = "D1495B"
GREEN = "2E7D32"
MUTED = "52606D"
LIGHT = "D9E2EC"
PALE_BLUE = "EAF2FB"
PALE_TEAL = "E7F6F3"
PALE_ORANGE = "FFF3E6"
PALE_RED = "FCEBEC"


def rgb(hex_color: str) -> RGBColor:
    return RGBColor.from_string(hex_color)


def choose_chinese_font() -> str:
    candidates = ["Microsoft YaHei", "Microsoft YaHei UI", "SimHei", "Noto Sans CJK SC"]
    names = {font.name for font in font_manager.fontManager.ttflist}
    return next((name for name in candidates if name in names), "DejaVu Sans")


CHINESE_FONT = choose_chinese_font()
plt.rcParams.update(
    {
        "font.family": CHINESE_FONT,
        "axes.unicode_minus": False,
        "axes.edgecolor": "#9FB3C8",
        "axes.labelcolor": "#243B53",
        "xtick.color": "#486581",
        "ytick.color": "#486581",
        "grid.color": "#D9E2EC",
        "grid.alpha": 0.75,
    }
)


def add_text(
    slide,
    x: float,
    y: float,
    w: float,
    h: float,
    text: str,
    size: float = 16,
    color: str = NAVY,
    bold: bool = False,
    align=PP_ALIGN.LEFT,
    valign=MSO_ANCHOR.TOP,
    margin: float = 0.04,
):
    box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    box.text_frame.clear()
    box.text_frame.margin_left = Inches(margin)
    box.text_frame.margin_right = Inches(margin)
    box.text_frame.margin_top = Inches(margin)
    box.text_frame.margin_bottom = Inches(margin)
    box.text_frame.vertical_anchor = valign
    p = box.text_frame.paragraphs[0]
    p.alignment = align
    run = p.add_run()
    run.text = text
    run.font.name = "Microsoft YaHei"
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.color.rgb = rgb(color)
    return box


def add_bullets(slide, x, y, w, h, items, size=16, color=NAVY, spacing=8):
    box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = box.text_frame
    tf.clear()
    tf.word_wrap = True
    tf.margin_left = Inches(0.08)
    tf.margin_right = Inches(0.03)
    tf.margin_top = Inches(0.02)
    for index, item in enumerate(items):
        p = tf.paragraphs[0] if index == 0 else tf.add_paragraph()
        p.text = f"• {item}"
        p.level = 0
        p.font.name = "Microsoft YaHei"
        p.font.size = Pt(size)
        p.font.color.rgb = rgb(color)
        p.space_after = Pt(spacing)
        p.line_spacing = 1.14
    return box


def add_rect(slide, x, y, w, h, fill=WHITE, line=LIGHT, radius=True):
    kind = MSO_SHAPE.ROUNDED_RECTANGLE if radius else MSO_SHAPE.RECTANGLE
    shape = slide.shapes.add_shape(kind, Inches(x), Inches(y), Inches(w), Inches(h))
    shape.fill.solid()
    shape.fill.fore_color.rgb = rgb(fill)
    shape.line.color.rgb = rgb(line)
    shape.line.width = Pt(1)
    return shape


def add_card(slide, x, y, w, h, value, label, accent=BLUE, note=None):
    add_text(slide, x + 0.22, y + 0.18, w - 0.35, 0.48, value, 23, accent, True)
    add_text(slide, x + 0.22, y + 0.68, w - 0.35, 0.36, label, 12.5, MUTED, True)
    if note:
        add_text(slide, x + 0.22, y + 1.02, w - 0.35, h - 1.10, note, 10.5, MUTED)


def add_title(slide, title, subtitle=None, number=None):
    add_text(slide, 0.65, 0.30, 11.8, 0.55, title, 27, NAVY, True)
    if subtitle:
        add_text(slide, 0.68, 0.88, 11.8, 0.34, subtitle, 12.5, MUTED)
    line = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0.65), Inches(1.25), Inches(12.0), Inches(0.035))
    line.fill.solid()
    line.fill.fore_color.rgb = rgb(BLUE)
    line.line.fill.background()
    if number is not None:
        add_text(slide, 12.15, 7.08, 0.55, 0.23, str(number), 9.5, MUTED, align=PP_ALIGN.RIGHT)


def add_takeaway(slide, text, color=BLUE):
    # Keep slide conclusions as plain text.  A bordered callout competes with
    # the chart and makes the layout look like a dashboard/card UI.
    add_text(slide, 0.72, 6.76, 11.85, 0.28, text, 13.2, color, True, valign=MSO_ANCHOR.MIDDLE)


def add_arrow(slide, x1, y1, x2, y2, color=BLUE, width=2.0):
    line = slide.shapes.add_connector(
        MSO_CONNECTOR.STRAIGHT, Inches(x1), Inches(y1), Inches(x2), Inches(y2)
    )
    line.line.color.rgb = rgb(color)
    line.line.width = Pt(width)
    line.line.end_arrowhead = True
    return line


def set_slide_bg(slide):
    background = slide.background
    background.fill.solid()
    background.fill.fore_color.rgb = rgb(BG)


def new_slide(prs):
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    set_slide_bg(slide)
    return slide


def add_picture(slide, path: Path, x, y, w, h=None):
    kwargs = {"left": Inches(x), "top": Inches(y), "width": Inches(w)}
    if h is not None:
        kwargs["height"] = Inches(h)
    return slide.shapes.add_picture(str(path), **kwargs)


def load_flow(path: Path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = [row for row in csv.reader(handle)][1:]
    rows = [row for row in rows if len(row) >= 13 and row[1] and row[2]]
    stamp = np.asarray([float(row[1]) / 1000.0 for row in rows])
    flow = np.asarray([float(row[2]) for row in rows])
    atm = np.asarray([int(row[3]) for row in rows])
    start = int(np.flatnonzero(atm == 1)[0])
    baseline = float(np.median(flow[max(0, start - 100) : start]))
    t = stamp[start:] - stamp[start]
    q = flow[start:] - baseline
    kernel = np.ones(5) / 5.0
    smooth = np.convolve(q, kernel, mode="same")
    return t, smooth


def make_calibration_accuracy(a: pd.DataFrame, b: pd.DataFrame, out: Path):
    fig, axes = plt.subplots(2, 1, figsize=(10.2, 6.0), sharex=True, gridspec_kw={"height_ratios": [2.2, 1]})
    fig.patch.set_facecolor("#F6F8FC")
    for ax in axes:
        ax.set_facecolor("#FFFFFF")
        ax.grid(True)
    axes[0].plot([0, 5], [0, 5], "--", color="#7B8794", lw=1.5, label="理想值")
    styles = [
        (a, "up1 升扫", "#169C89", "o"),
        (b[b.direction == "up"], "scan2 升扫", "#2364AA", "s"),
        (b[b.direction == "down"], "scan2 降扫", "#F28E2B", "^"),
    ]
    for data, label, color, marker in styles:
        axes[0].plot(data.target_v, data.measured_v, marker=marker, ms=5.5, lw=1.6, color=color, label=label)
        axes[1].plot(data.target_v, (data.measured_v - data.target_v) * 1000, marker=marker, ms=5, lw=1.3, color=color, label=label)
    axes[0].set_ylabel("实测电压 / V")
    axes[0].legend(frameon=False, ncol=2, loc="upper left")
    axes[0].set_xlim(-0.10, 5.10)
    axes[0].set_ylim(-0.10, 5.12)
    axes[1].axhline(0, color="#7B8794", lw=1)
    axes[1].set_ylabel("误差 / mV")
    axes[1].set_xlabel("理想目标电压 / V")
    axes[1].set_ylim(-9, 9)
    fig.tight_layout(pad=1.0)
    fig.savefig(out, dpi=220, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)


def make_calibration_repeatability(a: pd.DataFrame, b: pd.DataFrame, out: Path):
    up = b[b.direction == "up"].set_index("pwm_code").sort_index()
    down = b[b.direction == "down"].set_index("pwm_code").sort_index()
    up1 = a.set_index("pwm_code").sort_index()
    x = up.target_v.to_numpy()
    delta_h = (up.measured_v - down.measured_v).to_numpy() * 1000
    delta_r = (up.measured_v - up1.measured_v).to_numpy() * 1000
    fig, axes = plt.subplots(1, 2, figsize=(10.2, 4.4), sharex=True)
    fig.patch.set_facecolor("#F6F8FC")
    for ax in axes:
        ax.set_facecolor("#FFFFFF")
        ax.axhline(0, color="#7B8794", lw=1)
        ax.grid(True, axis="y")
        ax.set_xlabel("目标电压 / V")
    axes[0].bar(x, delta_h, width=0.28, color="#169C89")
    axes[0].set_title("升扫 − 降扫")
    axes[0].set_ylabel("同码值电压差 / mV")
    axes[0].set_ylim(-6, 6)
    axes[1].bar(x, delta_r, width=0.28, color="#2364AA")
    axes[1].set_title("第二次升扫 − 第一次升扫")
    axes[1].set_ylim(-6, 6)
    fig.tight_layout(pad=1.0)
    fig.savefig(out, dpi=220, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)


def make_representative_flows(audit: pd.DataFrame, summary: pd.DataFrame, out: Path):
    targets = [0.0, 3.2, 3.5, 3.7, 3.9, 4.0, 4.1]
    colors = plt.cm.viridis(np.linspace(0.04, 0.92, len(targets)))
    fig, ax = plt.subplots(figsize=(10.2, 5.6))
    fig.patch.set_facecolor("#F6F8FC")
    ax.set_facecolor("#FFFFFF")
    for voltage, color in zip(targets, colors):
        group = audit[(audit.voltage_v == voltage) & (audit.excluded_reason.isna())].copy()
        median_tau = float(summary.loc[summary.voltage_v == voltage, "tau_median_s"].iloc[0])
        row = group.iloc[(group.fitted_tau_s - median_tau).abs().argmin()]
        t, q = load_flow(DATA / row.file)
        keep = t <= 20
        ax.plot(t[keep], q[keep], color=color, lw=2.0, label=f"{voltage:g} V")
    row42 = audit[audit.file == "42.csv"].iloc[0]
    t, q = load_flow(DATA / row42.file)
    keep = t <= 20
    ax.plot(t[keep], q[keep], color="#111111", lw=2.0, ls="--", label="4.2 V 夹闭")
    ax.axhline(0, color="#7B8794", lw=1)
    ax.set_xlim(0, 20)
    ax.set_ylim(-3, 65)
    ax.set_xlabel("ATM 打开后时间 / s")
    ax.set_ylabel("零点校正流量 / SLM")
    ax.grid(True)
    ax.legend(frameon=False, ncol=4, loc="upper right")
    fig.tight_layout(pad=1.0)
    fig.savefig(out, dpi=220, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)


def make_tau_fit(audit: pd.DataFrame, summary: pd.DataFrame, model: dict, out: Path):
    fig, ax = plt.subplots(figsize=(10.2, 5.4))
    fig.patch.set_facecolor("#F6F8FC")
    ax.set_facecolor("#FFFFFF")
    valid = audit[audit.excluded_reason.isna()]
    ax.scatter(valid.voltage_v, valid.fitted_tau_s, s=38, alpha=0.46, color="#F28E2B", label="单次实验")
    ax.plot(summary.voltage_v, summary.tau_median_s, "o-", color="#A64B00", lw=2.0, ms=6.5, label="各电压中位数")
    piece = model["piecewise_fitted_dead_zone"]
    dense = np.linspace(0, 4.1, 400)
    opening = np.where(
        dense <= piece["dead_zone_end_v"],
        1.0,
        (piece["closure_v"] - dense) / (piece["closure_v"] - piece["dead_zone_end_v"]),
    )
    predicted = piece["tau0_s"] / opening ** piece["gamma"]
    ax.plot(dense, predicted, "--", color="#102A43", lw=2.1, label="分段物理模型")
    ax.axvspan(0, piece["dead_zone_end_v"], color="#2364AA", alpha=0.08, label="低电压平台")
    ax.axvline(4.2, color="#D1495B", ls=":", lw=2, label="4.2 V 夹闭")
    ax.set_xlim(-0.05, 4.35)
    ax.set_ylim(1.3, 10.2)
    ax.set_xlabel("PPV 命令电压 / V")
    ax.set_ylabel("有效吸气时间常数 / s")
    ax.grid(True)
    ax.legend(frameon=False, ncol=2, loc="upper left")
    fig.tight_layout(pad=1.0)
    fig.savefig(out, dpi=220, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)


def make_opening_mapping(model: dict, out: Path):
    piece = model["piecewise_fitted_dead_zone"]
    u = np.linspace(0, 5, 500)
    naive = np.clip(1 - u / 5, 0, 1)
    effective = np.where(
        u <= piece["dead_zone_end_v"],
        1.0,
        np.where(u >= piece["closure_v"], 0.0, (piece["closure_v"] - u) / (piece["closure_v"] - piece["dead_zone_end_v"])),
    )
    fig, ax = plt.subplots(figsize=(8.0, 4.7))
    fig.patch.set_facecolor("#F6F8FC")
    ax.set_facecolor("#FFFFFF")
    ax.plot(u, naive, "--", lw=2, color="#7B8794", label=r"原假设 $a=1-U/5$")
    ax.plot(u, effective, lw=3, color="#2364AA", label=r"实测分段 $a_{eff}$")
    ax.axvline(piece["dead_zone_end_v"], color="#169C89", ls=":", lw=2)
    ax.axvline(piece["closure_v"], color="#D1495B", ls=":", lw=2)
    ax.text(piece["dead_zone_end_v"] - 0.06, 0.52, f"Ud={piece['dead_zone_end_v']:.2f} V", rotation=90, ha="right", color="#169C89")
    ax.text(piece["closure_v"] + 0.06, 0.52, "Uc=4.20 V", rotation=90, ha="left", color="#D1495B")
    ax.set_xlabel("PPV 命令电压 / V")
    ax.set_ylabel("归一化有效开度")
    ax.set_xlim(0, 5)
    ax.set_ylim(-0.05, 1.05)
    ax.grid(True)
    ax.legend(frameon=False, loc="lower left")
    fig.tight_layout(pad=1.0)
    fig.savefig(out, dpi=220, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)


def make_repeatability(summary: pd.DataFrame, out: Path):
    repeated = summary[summary.n > 1].copy()
    fig, ax = plt.subplots(figsize=(9.0, 4.6))
    fig.patch.set_facecolor("#F6F8FC")
    ax.set_facecolor("#FFFFFF")
    colors = ["#D1495B" if x >= 12 else "#169C89" for x in repeated.tau_cv_percent]
    bars = ax.bar(repeated.voltage_v.astype(str), repeated.tau_cv_percent, color=colors)
    for bar, n in zip(bars, repeated.n):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.5, f"n={int(n)}", ha="center", fontsize=8.5, color="#52606D")
    ax.axhline(10, color="#F28E2B", ls="--", lw=1.5, label="10% 参考线")
    ax.set_ylabel(r"时间常数变异系数 $CV$ / %")
    ax.set_xlabel("PPV 电压 / V")
    ax.set_ylim(0, 24)
    ax.grid(True, axis="y")
    ax.legend(frameon=False)
    fig.tight_layout(pad=1.0)
    fig.savefig(out, dpi=220, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)


def prepare_assets():
    ASSETS.mkdir(parents=True, exist_ok=True)
    electrical1 = pd.read_csv(ELECTRICAL_1)
    electrical2 = pd.read_csv(ELECTRICAL_2)
    audit = pd.read_csv(TRIAL_AUDIT)
    summary = pd.read_csv(VOLTAGE_SUMMARY)
    model = json.loads(MODEL_RESULTS.read_text(encoding="utf-8"))
    make_calibration_accuracy(electrical1, electrical2, ASSETS / "electrical_accuracy.png")
    make_calibration_repeatability(electrical1, electrical2, ASSETS / "electrical_repeatability.png")
    make_representative_flows(audit, summary, ASSETS / "representative_flow_curves.png")
    make_tau_fit(audit, summary, model, ASSETS / "tau_voltage_fit.png")
    make_opening_mapping(model, ASSETS / "effective_opening_mapping.png")
    make_repeatability(summary, ASSETS / "tau_repeatability_cv.png")
    return electrical1, electrical2, audit, summary, model


def title_slide(prs):
    slide = new_slide(prs)
    top = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0), Inches(0), Inches(13.333), Inches(0.16))
    top.fill.solid(); top.fill.fore_color.rgb = rgb(BLUE); top.line.fill.background()
    add_text(slide, 0.92, 0.72, 11.6, 0.40, "LungLogic Capstone Project", 15, BLUE, True)
    add_text(slide, 0.90, 1.48, 11.6, 1.20, "PPV40V 实物实验结果", 37, NAVY, True)
    add_text(slide, 0.94, 2.68, 11.0, 0.55, "PWM—电压校准与 fake lung 吸气开度辨识", 21, MUTED)
    line = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0.94), Inches(3.50), Inches(4.05), Inches(0.08))
    line.fill.solid(); line.fill.fore_color.rgb = rgb(TEAL); line.line.fill.background()
    add_text(slide, 0.95, 4.18, 5.4, 0.38, "Group Meeting", 17, NAVY, True)
    add_text(slide, 0.95, 4.66, 5.4, 0.32, "2026-10-06", 14, MUTED)
    add_text(slide, 7.98, 4.03, 3.80, 0.38, "两组实验", 19, BLUE, True)
    add_text(slide, 7.98, 4.56, 3.80, 0.34, "1. PCB/Arduino PWM → 电压", 14.5, NAVY)
    add_text(slide, 7.98, 5.04, 3.80, 0.34, "2. PPV 电压 → 吸气流量", 14.5, NAVY)
    add_text(slide, 0.95, 6.76, 11.45, 0.25, "Preliminary parameter identification and test-quality review", 11.5, MUTED)


def experiment_map_slide(prs, n):
    slide = new_slide(prs); add_title(slide, "两组实验分别回答什么？", "先区分电气命令链和气动被控对象", n)
    add_text(slide, 1.05, 1.90, 5.1, 0.45, "实验 1：PWM—电压扫描", 22, BLUE, True)
    boxes = [(1.08, 2.70, "PWM code", "0–4095"), (2.77, 2.70, "滤波/电平转换", "1 kHz, 12 bit"), (4.75, 2.70, "万用表", "0–5 V")]
    for x, y, t, b in boxes:
        add_rect(slide, x, y, 1.35 if x != 2.77 else 1.65, 1.10, fill=WHITE, line=LIGHT)
        add_text(slide, x + 0.08, y + 0.17, 1.2 if x != 2.77 else 1.5, 0.28, t, 12.5, NAVY, True, align=PP_ALIGN.CENTER)
        add_text(slide, x + 0.08, y + 0.59, 1.2 if x != 2.77 else 1.5, 0.24, b, 11, MUTED, align=PP_ALIGN.CENTER)
    add_arrow(slide, 2.43, 3.25, 2.72, 3.25); add_arrow(slide, 4.43, 3.25, 4.70, 3.25)
    add_bullets(slide, 1.08, 4.20, 4.9, 1.35, ["线性、稳态误差、升降扫重复性", "不测阀位、流量或动态延迟"], 14.5)

    add_text(slide, 7.13, 1.90, 5.1, 0.45, "实验 2：PPV—fake lung 吸气", 22, TEAL, True)
    boxes = [(7.17, 2.70, "PPV 电压", "0–4.2 V"), (8.92, 2.70, "ATM 开启", "吸气阶跃"), (10.67, 2.70, "SFM3300", "50 Hz 流量")]
    for x, y, t, b in boxes:
        add_rect(slide, x, y, 1.35, 1.10, fill=WHITE, line=LIGHT)
        add_text(slide, x + 0.08, y + 0.17, 1.19, 0.28, t, 12.5, NAVY, True, align=PP_ALIGN.CENTER)
        add_text(slide, x + 0.08, y + 0.59, 1.19, 0.24, b, 11, MUTED, align=PP_ALIGN.CENTER)
    add_arrow(slide, 8.52, 3.25, 8.87, 3.25, TEAL); add_arrow(slide, 10.27, 3.25, 10.62, 3.25, TEAL)
    add_bullets(slide, 7.17, 4.20, 4.9, 1.35, ["一阶时间常数、有效调节区、夹闭点、γ", "PPV 已提前设定，不能隔离 PPV 动作延迟"], 14.5)
    add_takeaway(slide, "联合分析才能判断：流量死区不是电压输出失真造成的。", TEAL)


def electrical_method_slide(prs, n):
    slide = new_slide(prs); add_title(slide, "实验 1：PWM—控制电压扫描", "PPV 正常连接，ATM/VAC 关闭，万用表读取稳态电压", n)
    steps = [
        ("1", "设置 PWM", "0%, 10% … 100%"),
        ("2", "等待稳定", "ACK 后至少 1 s"),
        ("3", "读取电压", "万用表 DC V"),
        ("4", "反向扫描", "100% → 0%"),
    ]
    for i, (num, title, body) in enumerate(steps):
        x = 0.85 + i * 3.02
        add_rect(slide, x, 2.05, 2.40, 1.55, fill=WHITE, line=BLUE)
        circle = slide.shapes.add_shape(MSO_SHAPE.OVAL, Inches(x + 0.14), Inches(2.27), Inches(0.55), Inches(0.55))
        circle.fill.solid(); circle.fill.fore_color.rgb = rgb(BLUE); circle.line.fill.background()
        add_text(slide, x + 0.14, 2.35, 0.55, 0.25, num, 14, WHITE, True, align=PP_ALIGN.CENTER)
        add_text(slide, x + 0.83, 2.20, 1.42, 0.35, title, 15.5, NAVY, True)
        add_text(slide, x + 0.83, 2.72, 1.42, 0.43, body, 12.2, MUTED)
        if i < len(steps) - 1:
            add_arrow(slide, x + 2.42, 2.83, x + 2.95, 2.83)
    add_card(slide, 1.05, 4.45, 2.50, 1.35, "1 kHz", "PWM 频率", BLUE)
    add_card(slide, 3.90, 4.45, 2.50, 1.35, "12 bit", "PWM 分辨率", TEAL)
    add_card(slide, 6.75, 4.45, 2.50, 1.35, "1.221 mV", "理论单码电压", ORANGE)
    add_card(slide, 9.60, 4.45, 2.50, 1.35, "0–5 V", "命令输出范围", RED)
    add_takeaway(slide, "这是稳态电气校准；每个时间戳是人工保存读数的时刻，不是响应完成时刻。", RED)


def electrical_results_slide(prs, n):
    slide = new_slide(prs); add_title(slide, "电气结果：高线性、毫伏级误差", "两次升扫 + 一次降扫", n)
    add_picture(slide, ASSETS / "electrical_accuracy.png", 0.70, 1.48, 8.35, 4.90)
    add_card(slide, 9.35, 1.64, 2.90, 1.25, "R² = 0.999993", "线性回归", BLUE)
    add_card(slide, 9.35, 3.05, 2.90, 1.25, "4.63 mV", "全点 RMSE", TEAL)
    add_card(slide, 9.35, 4.46, 2.90, 1.25, "7.4 mV", "最大绝对误差", ORANGE, "= 0.148% 满量程")
    add_takeaway(slide, "PWM—滤波/放大—PPV 输入电压的稳态映射已被验证，简单线性换算已足够准确。", BLUE)


def electrical_repeatability_slide(prs, n):
    slide = new_slide(prs); add_title(slide, "电气重复性：升降扫差异基本不可见", "差异量级与一个 12-bit PWM 码值相当", n)
    add_picture(slide, ASSETS / "electrical_repeatability.png", 0.85, 1.60, 8.25, 4.45)
    add_card(slide, 9.35, 1.75, 2.85, 1.25, "1.0 mV", "升/降扫最大差", TEAL)
    add_card(slide, 9.35, 3.15, 2.85, 1.25, "0.27 mV", "升/降扫平均绝对差", BLUE)
    add_card(slide, 9.35, 4.55, 2.85, 1.25, "0.65 mV", "两次升扫平均差", ORANGE)
    add_takeaway(slide, "电气端没有可见滞回；但这不代表 PPV/软管的机械开度没有滞回。", TEAL)


def electrical_focus_slide(prs, n):
    slide = new_slide(prs)
    add_title(
        slide,
        "实验 1：经过 PCB/Arduino 后，PWM 与输出电压基本一致",
        "D5 1 kHz、12-bit PWM → PCB 滤波/电平转换 → PPV 0–5 V 控制输入",
        n,
    )
    add_picture(slide, ASSETS / "electrical_accuracy.png", 0.62, 1.46, 7.65, 4.92)
    add_text(slide, 8.58, 1.58, 3.72, 0.32, "实验方法", 17, BLUE, True)
    add_text(slide, 8.58, 2.02, 3.72, 0.30, "0–100% 升扫，再 100–0% 降扫", 13.2, NAVY)
    add_text(slide, 8.58, 2.68, 3.72, 0.32, "数据结果", 17, BLUE, True)
    add_text(slide, 8.58, 3.14, 3.72, 1.70,
             "• 线性 R²：0.999993\n• RMSE：4.63 mV\n• 最大误差：7.4 mV\n• 升/降扫最大差：1.0 mV",
             14.2, NAVY)
    add_text(slide, 8.58, 5.22, 3.72, 0.80, "实验结论\n最大误差仅 0.148% FS，线性换算已足够。", 13.0, TEAL, True)
    add_takeaway(slide, "因此第二个实验中的流量非线性不是 PCB/Arduino 电压输出偏差造成的。", BLUE)


def identification_pipeline_slide(prs, n):
    slide = new_slide(prs)
    add_title(slide, "a. 初步建模参数如何从流量数据得到？", "每次试验给出一条 Q(t)，同一电压的重复试验再取稳健汇总", n)
    stages = [
        ("Q(t)", "50 Hz 流量", BLUE),
        ("零点校正", "ATM 前 2 s 中位数", TEAL),
        ("V(t)=∫Qdt", "累积吸入体积", ORANGE),
        ("一阶拟合", "V∞、td、τeff", RED),
        ("分电压汇总", "τeff 中位数", GREEN),
        ("PPV 模型", "Ud、Uc、γ", BLUE),
    ]
    for i, (title, body, color) in enumerate(stages):
        x = 0.48 + i * 2.10
        add_rect(slide, x, 2.05, 1.70, 1.35, fill=WHITE, line=color)
        add_text(slide, x + 0.10, 2.28, 1.50, 0.30, title, 14.5, color, True, align=PP_ALIGN.CENTER)
        add_text(slide, x + 0.10, 2.78, 1.50, 0.32, body, 10.8, MUTED, align=PP_ALIGN.CENTER)
        if i < len(stages) - 1:
            add_arrow(slide, x + 1.72, 2.72, x + 2.04, 2.72, color)
    add_text(slide, 0.95, 4.20, 5.25, 0.30, "单次实验可得到", 17, ORANGE, True)
    add_text(slide, 0.95, 4.68, 5.25, 0.92,
             "• 峰值/早期流量、累积体积 V(t)\n• τeff、拟合体积 V∞、综合延迟 td",
             13.5, NAVY)
    add_text(slide, 6.85, 4.20, 5.25, 0.30, "多电压数据可进一步得到", 17, TEAL, True)
    add_text(slide, 6.85, 4.68, 5.25, 0.92,
             "• 全开基础时间常数 τin\n• 有效调节起点 Ud、夹闭点 Uc、开度指数 γ",
             13.5, NAVY)
    add_takeaway(slide, "峰值流量对初始压扁程度很敏感；因此 γ 主要根据一阶时间常数辨识。", ORANGE)


def inspiration_method_slide(prs, n):
    slide = new_slide(prs); add_title(slide, "实验 2：固定 PPV 电压下的 fake lung 吸气", "每次手动压扁 fake lung，再用 ATM 开启作为吸气阶跃", n)
    steps = [
        ("1", "压扁 fake lung", "手动初始状态"),
        ("2", "ATM/VAC 关闭", "隔离气路"),
        ("3", "设置 PPV", "0–4.2 V"),
        ("4", "打开 ATM", "正常吸气"),
        ("5", "记录流量", "SFM3300, 50 Hz"),
    ]
    for i, (num, title, body) in enumerate(steps):
        x = 0.55 + i * 2.53
        add_rect(slide, x, 2.00, 2.05, 1.48, fill=WHITE, line=TEAL)
        add_text(slide, x + 0.14, 2.17, 0.36, 0.30, num, 14, TEAL, True, align=PP_ALIGN.CENTER)
        add_text(slide, x + 0.47, 2.14, 1.45, 0.34, title, 14.2, NAVY, True)
        add_text(slide, x + 0.18, 2.72, 1.68, 0.38, body, 11.6, MUTED, align=PP_ALIGN.CENTER)
        if i < 4:
            add_arrow(slide, x + 2.07, 2.73, x + 2.46, 2.73, TEAL)
    add_text(slide, 0.92, 4.35, 2.55, 0.30, "40 份 CSV", 19, BLUE, True)
    add_text(slide, 0.92, 4.78, 2.55, 0.58, "包含不同电压和重复试验", 12.3, MUTED)
    add_text(slide, 3.78, 4.35, 2.55, 0.30, "38 次动态拟合", 19, TEAL, True)
    add_text(slide, 3.78, 4.78, 2.55, 0.58, "37.csv 剔除；42.csv 作为夹闭点", 12.3, MUTED)
    add_text(slide, 6.64, 4.35, 2.55, 0.30, "R² 中位数 0.9994", 19, ORANGE, True)
    add_text(slide, 6.64, 4.78, 2.55, 0.58, "累积体积的一阶拟合", 12.3, MUTED)
    add_text(slide, 9.50, 4.35, 2.55, 0.30, "未记录压力", 19, RED, True)
    add_text(slide, 9.50, 4.78, 2.55, 0.58, "不能分离阀阻与 fake lung 顺应性", 12.3, MUTED)
    add_takeaway(slide, "可识别整个“PPV—管路—fake lung”的有效动态，不是 PPV 独立流量系数。", TEAL)


def flow_curves_slide(prs, n):
    slide = new_slide(prs); add_title(slide, "代表性流量曲线：3.3 V 后逐渐变慢", "每个电压选取最接近该组中位时间常数的一次实验", n)
    add_picture(slide, ASSETS / "representative_flow_curves.png", 0.72, 1.45, 8.75, 5.05)
    add_bullets(
        slide,
        9.70,
        1.75,
        2.65,
        4.15,
        [
            "0–3.2 V：衰减时间接近，阀限制较弱",
            "3.5–3.9 V：峰值下降，吸气持续时间增加",
            "4.1 V：峰值约 5.6 SLM，响应显著变慢",
            "4.2 V：流量与零点噪声相当，确认夹闭",
            "峰值流量受手动压扁程度影响，不用于单独辨识 γ",
        ],
        14.0,
    )
    add_takeaway(slide, "曲线展示了明确的整体趋势，但局部非单调主要来自初始机械状态差异。", ORANGE)


def tau_identification_slide(prs, n):
    slide = new_slide(prs); add_title(slide, "一阶时间常数辨识", "对零点校正流量积分，拟合累积吸入体积", n)
    add_picture(slide, ASSETS / "tau_voltage_fit.png", 0.65, 1.42, 8.65, 5.12)
    add_text(slide, 9.55, 1.72, 2.82, 0.30, "拟合模型", 17, ORANGE, True)
    add_text(slide, 9.55, 2.18, 2.82, 0.72, "V(t)=V∞[1−exp(−(t−td)/τeff)]", 14.0, NAVY, True)
    add_text(slide, 9.55, 3.22, 2.82, 0.30, "初步结果", 17, TEAL, True)
    add_text(slide, 9.55, 3.72, 2.82, 1.38,
             "• 0–3.2 V：τ 中位数 1.91 s\n• 4.1 V：τ = 9.41 s\n• 3.9 V 以上增长很快",
             14.0, NAVY)
    add_takeaway(slide, "全开基础时间常数可取 τin ≈ 1.9 s；3.9 V 以上时间常数快速增大。", TEAL)


def gamma_slide(prs, n, model):
    slide = new_slide(prs); add_title(slide, "PPV 非线性：需要分段有效开度", "低电压死区 + 4.2 V 提前夹闭，不适合全程 a=1−U/5", n)
    add_picture(slide, ASSETS / "effective_opening_mapping.png", 0.72, 1.55, 6.65, 4.65)
    piece = model["piecewise_fitted_dead_zone"]
    add_text(slide, 7.72, 1.66, 4.40, 0.30, "分段模型拟合结果", 18, BLUE, True)
    add_text(slide, 7.72, 2.16, 4.40, 1.62,
             f"• τin = {piece['tau0_s']:.2f} s\n"
             f"• 平台终点 Ud = {piece['dead_zone_end_v']:.2f} V\n"
             f"• 有效开度 γ = {piece['gamma']:.3f}\n"
             f"• 时间常数拟合 R² = {piece['r_squared_tau']:.3f}",
             14.2, NAVY)
    add_text(slide, 7.72, 4.15, 4.40, 0.34, "τeff = τin / aeff^γ", 19, ORANGE, True)
    add_text(slide, 7.72, 4.72, 4.40, 0.64, "重复选择敏感范围：γ = 0.60–0.68", 13.2, MUTED)
    add_takeaway(slide, "建议用 γ≈0.65 + 分段 aeff；不建议将局部拟合的 γ≈2.3–2.6 当作全局物理参数。", ORANGE)


def repeatability_slide(prs, n):
    slide = new_slide(prs); add_title(slide, "重复性和异常点", "主要不确定性来自每次 fake lung 的手动初始压缩", n)
    add_picture(slide, ASSETS / "tau_repeatability_cv.png", 0.75, 1.58, 7.20, 4.65)
    add_text(slide, 8.55, 1.88, 3.45, 0.35, "需要重点解释的文件", 17, NAVY, True)
    add_bullets(
        slide,
        8.50,
        2.42,
        3.40,
        3.30,
        [
            "37.csv：文件名 3.7 V，遥测为 0 V，剔除",
            "33.csv：峰值偏低，后段有反向波动",
            "35.2.csv：峰值偏低，结尾约 −3 SLM",
            "36.3.csv：τ=3.61 s，高于同组其余试验",
            "375.csv：τ=2.97 s，低于两次重复的约 3.76 s",
        ],
        13.3,
        spacing=5,
    )
    add_takeaway(slide, "3.3、3.5、3.6、3.75 V 的τ变异系数为 13%–20%；参数可用，但应保留敏感性范围。", RED)


def data_quality_slide(prs, n):
    slide = new_slide(prs)
    add_title(slide, "b. 数据筛选与优化", "不把所有偏差都当作阀门特性：先分离记录错误、夹闭点和低重复性试验", n)
    add_picture(slide, ASSETS / "tau_repeatability_cv.png", 0.65, 1.54, 6.15, 4.75)
    items = [
        ("37.csv", "文件名 3.7 V；遥测 PPV 全程 0 V。", "剔除", RED),
        ("42.csv", "反复 ATM_ON 仍无流量；4.2 V 确认夹闭。", "保留闭点", TEAL),
        ("33.csv", "与 33.1 差异大；后段有反向波动。", "低信心度", ORANGE),
        ("35.2/35.3.csv", "记录后才设 3.5 V；结果偏低。", "低信心度", ORANGE),
    ]
    for i, (name, body, status, color) in enumerate(items):
        y = 1.48 + i * 1.20
        add_text(slide, 7.28, y + 0.10, 4.65, 0.28, name, 14.2, color, True)
        add_text(slide, 7.28, y + 0.48, 3.90, 0.40, body, 11.0, NAVY)
        add_text(slide, 11.23, y + 0.64, 0.98, 0.22, status, 9.7, color, True, align=PP_ALIGN.CENTER)
    add_text(slide, 7.18, 6.30, 5.05, 0.22, "35.2/35.3 在 ATM 开启前已稳定约 3.26/3.77 s，不因操作顺序直接删除。", 9.4, MUTED)
    add_takeaway(slide, "当前最大的实验误差源是每次 fake lung 初始压扁程度和机械状态不一致。", RED)


def error_analysis_slide(prs, n):
    slide = new_slide(prs)
    add_title(slide, "c. 误差分析：本次只辨识了吸气过程", "当前的 τ 是 ATM 吸气路径的 τin/τeff，并没有得到真空呼气路径的 τout", n)
    columns = [
        (0.78, TEAL, "已得到", ["吸气流量 Q(t) 和积分体积 V(t)", "全开 τin 与部分开度 τeff", "PPV 有效调节区、夹闭点和 γ"]),
        (4.58, RED, "未得到", ["呼气/真空收缩时间常数 τout", "PPV 命令到流量的独立纯延迟", "阀前后压差：不能分离阀阻与顺应性"]),
        (8.38, ORANGE, "工程影响", ["真空源可调，τout 对当前吸气建模优先级较低", "但要准确评估呼吸频率、I:E 和循环一致性，仍必须测 τout", "建议在多个 vacuum 设定下分别辨识"]),
    ]
    for x, color, title, bullets in columns:
        add_text(slide, x, 1.78, 3.20, 0.38, title, 20, color, True)
        add_bullets(slide, x, 2.48, 3.15, 3.25, bullets, 13.7, spacing=12)
    add_takeaway(slide, "可调 vacuum 可用来修正呼气速度，但不能替代 τout 标定；完整控制模型仍需要吸气和呼气两组参数。", ORANGE)


def preliminary_results_slide(prs, n):
    slide = new_slide(prs)
    add_title(slide, "初步实验结果：数据不完美，但足以给出可用的初值", "参数用于初步模型和敏感性分析，不应当作最终高精度标定", n)
    summaries = [
        (0.74, "τin ≈ 1.9 s", "全开吸气；1.8–2.1 s", TEAL),
        (3.20, "Ud ≈ 3.1 V", "开始明显调节", BLUE),
        (5.66, "Uc = 4.2 V", "确认完全夹闭", RED),
        (8.12, "γ ≈ 0.65", "敏感范围 0.60–0.70", ORANGE),
        (10.58, "R² = 0.986", "τ—电压拟合", GREEN),
    ]
    for x, value, label, color in summaries:
        add_text(slide, x, 1.72, 2.08, 0.34, value, 20, color, True)
        add_text(slide, x, 2.20, 2.08, 0.44, label, 11.3, MUTED)
    add_text(slide, 1.12, 3.68, 5.10, 0.34, "建议的吸气初步模型", 18, NAVY, True)
    add_text(slide, 1.12, 4.25, 5.10, 0.35, "τeff = τin / aeff(U)^γ", 20, BLUE, True, align=PP_ALIGN.CENTER)
    add_text(slide, 1.15, 4.90, 5.05, 0.48, "aeff=1（U≤3.1 V）；3.1–4.2 V 线性降至 0；U≥4.2 V 夹闭。", 12.8, MUTED, align=PP_ALIGN.CENTER)
    add_text(slide, 7.12, 3.68, 5.10, 0.34, "该结果可以支持", 18, NAVY, True)
    add_bullets(slide, 7.10, 4.25, 4.95, 1.15, ["初步前馈模型参数化", "控制器中的参数敏感性测试", "后续硬件循环实验的基准组"], 14.0, spacing=6)
    add_takeaway(slide, "关键是使用“分段有效开度 + γ≈0.65”，不是保留 a=1−U/5 后只修改 γ。", BLUE)


def future_work_slide(prs, n):
    slide = new_slide(prs)
    add_title(slide, "Future work：今天下午的测试计划", "今天已拿到 vacuum source；下午将补齐呼气参数，并观察整个控制在多个呼吸循环中的表现", n)
    plans = [
        ("1", "Vacuum/呼气实验", "在多个负压设定下记录流量，辨识 τout 和收缩体积。", RED),
        ("2", "PPV 阶跃与延迟", "ATM 已打开、压差稳定时单独阶跃 PPV，同步记录命令和流量。", ORANGE),
        ("3", "整个呼吸循环", "运行多个连续吸气/呼气循环，检查相位切换和循环间重复性。", TEAL),
        ("4", "控制性能记录", "同步保存目标/实测流量、积分体积、PPV 命令及 ATM/VAC 状态。", BLUE),
    ]
    for i, (num, title, body, color) in enumerate(plans):
        x = 0.75 + (i % 2) * 6.10
        y = 1.62 + (i // 2) * 2.35
        add_text(slide, x + 0.22, y + 0.28, 0.68, 0.34, num + ".", 17, color, True, align=PP_ALIGN.CENTER)
        add_text(slide, x + 1.12, y + 0.28, 4.18, 0.34, title, 17, color, True)
        add_text(slide, x + 1.12, y + 0.82, 4.18, 0.68, body, 12.7, NAVY)
    add_takeaway(slide, "下午的成功标志：获得 τout，并在多个呼吸循环中同时看到稳定的流量跟踪、体积和阀门切换。", TEAL)


def delay_slide(prs, n):
    slide = new_slide(prs); add_title(slide, "能否从现有实验看到延迟？", "结论：两组实验都不能隔离 PPV 命令→流量的纯延迟", n)
    add_text(slide, 1.08, 1.93, 5.15, 0.40, "现有数据缺少的关键观测", 20, RED, True)
    add_bullets(slide, 1.05, 2.58, 4.95, 2.45, ["PWM 扫描只保存稳态万用表读数", "无命令后连续电压波形；相邻保存点间隔 13–53 s 是人工时间", "吸气实验在 ATM 打开前已经设好 PPV", "拟合 td 混合 ATM、管路、SFM3300 与模型误差"], 14.2)
    add_text(slide, 7.12, 1.93, 5.10, 0.40, "正确的延迟测量", 20, TEAL, True)
    stages = [("PWM", "原始波形"), ("Uppv", "滤波电压"), ("PPV", "机械夹管"), ("Q", "流量")]
    for i, (name, desc) in enumerate(stages):
        x = 7.10 + i * 1.28
        add_rect(slide, x, 2.75, 1.00, 1.05, fill=WHITE, line=TEAL)
        add_text(slide, x + 0.08, 2.91, 0.84, 0.26, name, 13.5, NAVY, True, align=PP_ALIGN.CENTER)
        add_text(slide, x + 0.08, 3.32, 0.84, 0.24, desc, 9.8, MUTED, align=PP_ALIGN.CENTER)
        if i < 3:
            add_arrow(slide, x + 1.01, 3.28, x + 1.24, 3.28, TEAL)
    add_bullets(slide, 7.08, 4.25, 4.75, 0.98, ["示波器测电气阶跃；恒定压差下单独阶跃 PPV", "建议流量采样 ≥200 Hz，升/降方向各重复 ≥10 次"], 13.0, spacing=4)
    add_takeaway(slide, "现有 td 中位数约 0.14 s 只能称为“ATM 开启后的综合响应延迟”，不能当作 PPV 延迟。", RED)


def conclusion_slide(prs, n):
    slide = new_slide(prs); add_title(slide, "结论与下一步", "当前数据足以用于初步建模和控制器敏感性测试", n)
    add_card(slide, 0.85, 1.60, 2.25, 1.36, "< 0.15% FS", "PWM—电压链路最大误差", BLUE)
    add_card(slide, 3.30, 1.60, 2.25, 1.36, "τin ≈ 1.9 s", "全开吸气动态", TEAL)
    add_card(slide, 5.75, 1.60, 2.25, 1.36, "Ud ≈ 3.1 V", "开始有效调节", ORANGE)
    add_card(slide, 8.20, 1.60, 2.25, 1.36, "Uc = 4.2 V", "实测夹闭", RED)
    add_card(slide, 10.65, 1.60, 1.85, 1.36, "γ ≈ 0.65", "分段开度", GREEN)
    add_text(slide, 1.15, 3.67, 5.05, 0.35, "控制器中的建议初值", 18, NAVY, True)
    add_bullets(slide, 1.10, 4.25, 4.95, 1.38, ["τin = 1.9 s，敏感范围 1.8–2.1 s", "γ = 0.65，敏感范围 0.60–0.70", "必须将γ与分段 aeff(U) 一起使用"], 14.3)
    add_text(slide, 7.08, 3.67, 5.05, 0.35, "最高优先级的后续实验", 18, NAVY, True)
    add_bullets(slide, 7.03, 4.25, 5.05, 1.38, ["固定 fake lung 初始体积，随机化电压顺序", "补测 3.3、3.5、3.6、3.75、4.0、4.1 V", "示波器测电气延迟；恒压流量台测 PPV 机械延迟"], 14.3)
    add_takeaway(slide, "建议下一阶段从“参数扫描”转向“可重复初始状态 + 同步阶跃响应”。", BLUE)


def appendix_table_slide(prs, n, summary: pd.DataFrame):
    slide = new_slide(prs); add_title(slide, "附录：各电压的时间常数汇总", "表内范围为同一电压的最小—最大单次拟合值", n)
    columns = ["电压/V", "n", "τ中位/s", "τ范围/s", "峰值流量/SLM", "体积中位/mL"]
    rows = []
    for _, r in summary.iterrows():
        rows.append(
            [
                f"{r.voltage_v:g}",
                str(int(r.n)),
                f"{r.tau_median_s:.2f}",
                f"{r.tau_min_s:.2f}–{r.tau_max_s:.2f}",
                f"{r.peak_flow_median_slm:.1f}",
                f"{r.volume_median_ml:.0f}",
            ]
        )
    rows.append(["4.2", "1", "夹闭", "—", "≈0", "—"])
    shape = slide.shapes.add_table(len(rows) + 1, len(columns), Inches(0.72), Inches(1.50), Inches(11.90), Inches(5.35))
    table = shape.table
    widths = [1.05, 0.55, 1.35, 1.55, 2.00, 2.05]
    scale = 11.90 / sum(widths)
    for i, width in enumerate(widths):
        table.columns[i].width = Inches(width * scale)
    for j, col in enumerate(columns):
        cell = table.cell(0, j)
        cell.text = col
        cell.fill.solid(); cell.fill.fore_color.rgb = rgb(BLUE)
    for i, row in enumerate(rows, start=1):
        for j, value in enumerate(row):
            cell = table.cell(i, j)
            cell.text = value
            cell.fill.solid(); cell.fill.fore_color.rgb = rgb(WHITE if i % 2 else "EEF3F8")
    for i in range(len(rows) + 1):
        for j in range(len(columns)):
            cell = table.cell(i, j)
            cell.margin_left = Inches(0.04); cell.margin_right = Inches(0.04)
            cell.margin_top = Inches(0.01); cell.margin_bottom = Inches(0.01)
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            for p in cell.text_frame.paragraphs:
                p.alignment = PP_ALIGN.CENTER
                for run in p.runs:
                    run.font.name = "Microsoft YaHei"
                    run.font.size = Pt(10.2)
                    run.font.bold = i == 0
                    run.font.color.rgb = rgb(WHITE if i == 0 else NAVY)


def build_presentation(summary: pd.DataFrame, model: dict):
    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    prs.core_properties.title = "PPV40V 电气校准与吸气动态辨识"
    prs.core_properties.subject = "PWM—电压扫描和 fake lung 吸气流量实验"
    prs.core_properties.author = "LungLogic Capstone Team"
    title_slide(prs)
    experiment_map_slide(prs, 2)
    electrical_focus_slide(prs, 3)
    inspiration_method_slide(prs, 4)
    identification_pipeline_slide(prs, 5)
    flow_curves_slide(prs, 6)
    tau_identification_slide(prs, 7)
    gamma_slide(prs, 8, model)
    data_quality_slide(prs, 9)
    error_analysis_slide(prs, 10)
    preliminary_results_slide(prs, 11)
    future_work_slide(prs, 12)
    appendix_table_slide(prs, 13, summary)
    prs.save(OUTPUT)
    return OUTPUT


def main():
    _, _, _, summary, model = prepare_assets()
    output = build_presentation(summary, model)
    print(output)


if __name__ == "__main__":
    main()
