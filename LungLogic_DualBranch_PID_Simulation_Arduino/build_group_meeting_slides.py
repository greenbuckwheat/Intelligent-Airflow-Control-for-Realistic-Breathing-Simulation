from __future__ import annotations

from pathlib import Path
from textwrap import wrap

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch, Rectangle


OUT = Path(__file__).resolve().parent / "group_meeting_pid_smith_slides.pdf"

plt.rcParams["font.family"] = "DejaVu Sans"
plt.rcParams["axes.unicode_minus"] = False


BG = "#f7f8fb"
INK = "#172033"
MUTED = "#596477"
BLUE = "#276ef1"
TEAL = "#0f9f8f"
ORANGE = "#e77817"
RED = "#cf3f36"
LINE = "#d5dae5"


def new_slide(pdf: PdfPages, title: str, subtitle: str | None = None):
    fig, ax = plt.subplots(figsize=(13.333, 7.5))
    fig.patch.set_facecolor(BG)
    ax.set_xlim(0, 16)
    ax.set_ylim(0, 9)
    ax.axis("off")
    ax.add_patch(Rectangle((0, 8.62), 16, 0.06, color=BLUE, lw=0))
    ax.text(0.8, 8.18, title, fontsize=25, fontweight="bold", color=INK, va="top")
    if subtitle:
        ax.text(0.82, 7.68, subtitle, fontsize=12.5, color=MUTED, va="top")
    return fig, ax


def save(pdf: PdfPages, fig):
    pdf.savefig(fig, facecolor=fig.get_facecolor(), bbox_inches="tight")
    plt.close(fig)


def bullet_list(ax, items, x=1.05, y=6.85, width=78, line_gap=0.36, item_gap=0.24, fs=15):
    cursor = y
    for item in items:
        lines = wrap(item, width=width)
        ax.text(x, cursor, u"\u2022", fontsize=fs + 2, color=BLUE, va="top")
        ax.text(x + 0.36, cursor, lines[0], fontsize=fs, color=INK, va="top")
        cursor -= line_gap
        for line in lines[1:]:
            ax.text(x + 0.36, cursor, line, fontsize=fs, color=INK, va="top")
            cursor -= line_gap
        cursor -= item_gap


def note(ax, text, x=0.9, y=0.75, width=13.9):
    ax.add_patch(
        FancyBboxPatch(
            (x, y - 0.45),
            width,
            0.64,
            boxstyle="round,pad=0.08,rounding_size=0.08",
            fc="#ffffff",
            ec=LINE,
            lw=1.0,
        )
    )
    ax.text(x + 0.22, y - 0.05, text, fontsize=11.5, color=MUTED, va="center")


def box(ax, xy, wh, title, body=None, fc="#ffffff", ec=LINE, title_color=INK):
    x, y = xy
    w, h = wh
    ax.add_patch(
        FancyBboxPatch(
            (x, y),
            w,
            h,
            boxstyle="round,pad=0.08,rounding_size=0.08",
            fc=fc,
            ec=ec,
            lw=1.3,
        )
    )
    ax.text(x + 0.22, y + h - 0.28, title, fontsize=13.5, fontweight="bold", color=title_color, va="top")
    if body:
        ax.text(x + 0.22, y + h - 0.78, body, fontsize=11.8, color=MUTED, va="top", linespacing=1.28)


def arrow(ax, start, end, color=BLUE):
    ax.add_patch(
        FancyArrowPatch(
            start,
            end,
            arrowstyle="-|>",
            mutation_scale=16,
            lw=2.0,
            color=color,
            shrinkA=6,
            shrinkB=6,
        )
    )


def slide_1(pdf):
    fig, ax = new_slide(pdf, "LungLogic Single-Lung Flow Control", "PID, actuator mapping, Smith predictor, and calibration plan")
    box(ax, (0.95, 5.75), (4.3, 1.0), "Where is PID used?", "Inside the flow feedback loop")
    box(ax, (5.85, 5.75), (4.3, 1.0), "What is controlled?", "Measured flow tracks target flow")
    box(ax, (10.75, 5.75), (4.3, 1.0), "What is the actuator?", "PPV40V proportional pinch valve")
    box(ax, (0.95, 4.1), (4.3, 1.0), "Smith predictor", "Delay-compensated feedback estimate")
    box(ax, (5.85, 4.1), (4.3, 1.0), "PID calibration", "Kp, Ki, Kd, integral limit")
    box(ax, (10.75, 4.1), (4.3, 1.0), "Smith calibration", "Delay, model parameters, correction gain")
    note(ax, "Main message: PID controls flow through PPV opening; Smith predictor improves the timing of the feedback signal.")
    save(pdf, fig)


def slide_2(pdf):
    fig, ax = new_slide(pdf, "Control Loop Overview")
    labels = [
        ("Breathing settings", "RR, I:E, Vtarget, Tfill"),
        ("Reference flow", "Q_ref"),
        ("PID controller", "flow error -> opening"),
        ("PPV40V valve", "continuous actuator"),
        ("Lung + tubing", "pneumatic plant"),
        ("SFM3300 sensor", "measured flow"),
    ]
    xs = [0.8, 3.35, 5.9, 8.45, 11.0, 13.55]
    for x, (t, b) in zip(xs, labels):
        box(ax, (x, 4.55), (1.95, 1.25), t, b, fc="#ffffff")
    for x in xs[:-1]:
        arrow(ax, (x + 1.95, 5.17), (x + 2.55, 5.17))
    arrow(ax, (14.5, 4.55), (6.9, 3.1), color=TEAL)
    arrow(ax, (6.9, 3.1), (6.9, 4.55), color=TEAL)
    ax.text(8.25, 2.85, "feedback: measured flow, or Smith-predicted current flow", fontsize=13, color=TEAL, ha="center")
    note(ax, "The loop is flow control. Volume is obtained by integrating flow and is used as a phase stop condition.")
    save(pdf, fig)


def slide_3(pdf):
    fig, ax = new_slide(pdf, "Where PID Is Used")
    bullet_list(
        ax,
        [
            "Python simulation: single_lung_pid_gui.py -> PIDController.update().",
            "Simulation loop calls PID during INSPIRATION and EXPIRATION.",
            "Hardware path: Python sends Kp, Ki, Kd and integral_limit to STM32 through CFG.",
            "STM32 firmware runs updatePID() at 100 Hz.",
            "PID is reset at state transitions to avoid carrying integral error across breathing phases.",
        ],
        y=6.8,
    )
    box(
        ax,
        (1.15, 1.85),
        (13.7, 1.25),
        "PID law",
        "a = clip(a_ff + Kp * e + Ki * integral(e dt) + Kd * d, 0, 1)",
        fc="#eef5ff",
        ec="#b8cffb",
        title_color=BLUE,
    )
    save(pdf, fig)


def slide_4(pdf):
    fig, ax = new_slide(pdf, "What PID Controls")
    box(ax, (0.95, 5.6), (6.7, 1.3), "Controlled variable", "Flow rate Q measured by SFM3300\nnot pressure, not direct volume", fc="#ffffff")
    box(ax, (8.35, 5.6), (6.7, 1.3), "Manipulated variable", "PPV40V normalized opening a\nrange: 0 closed to 1 fully open", fc="#ffffff")
    box(ax, (0.95, 3.55), (6.7, 1.35), "Inspiration reference", "Q_in_ref = 0.06 * Vtarget / Tfill\n250 mL, 1.30 s -> 11.54 SLM", fc="#eefaf7", ec="#b8e4da", title_color=TEAL)
    box(ax, (8.35, 3.55), (6.7, 1.35), "Expiration reference", "Q_out_ref = -0.06 * Vtarget / usable expiration time\nPID tracks the magnitude of negative flow", fc="#fff5e9", ec="#f5d0a8", title_color=ORANGE)
    note(ax, "Volume is a result of controlled flow: V[k] = V[k-1] + Q[k] * 1000/60 * dt.")
    save(pdf, fig)


def slide_5(pdf):
    fig, ax = new_slide(pdf, "Actuators")
    box(ax, (1.0, 5.15), (4.3, 1.55), "Continuous actuator", "PPV40V proportional pinch valve\nPID output controls opening", fc="#eef5ff", ec="#b8cffb", title_color=BLUE)
    box(ax, (5.9, 5.15), (4.3, 1.55), "Direction actuator", "Atmosphere solenoid\nON during inspiration", fc="#eefaf7", ec="#b8e4da", title_color=TEAL)
    box(ax, (10.8, 5.15), (4.3, 1.55), "Direction actuator", "Vacuum solenoid\nON during expiration", fc="#fff5e9", ec="#f5d0a8", title_color=ORANGE)
    box(ax, (1.0, 2.85), (6.8, 1.35), "PPV voltage mapping", "U_cmd = 5 * (1 - a)\na = 1 -> 0 V -> fully open\na = 0 -> 5 V -> fully closed")
    box(ax, (8.4, 2.85), (6.7, 1.35), "Solenoid interlock", "Inspiration: ATM open, VAC closed\nExpiration: ATM closed, VAC open\nDead time: both closed")
    note(ax, "The solenoids switch the pneumatic route; PID continuously modulates the pinch valve.")
    save(pdf, fig)


def slide_6(pdf):
    fig, ax = new_slide(pdf, "Smith Predictor")
    bullet_list(
        ax,
        [
            "Problem: pneumatic transport and sensor response create delayed feedback.",
            "Delayed feedback can make PID overshoot, oscillate, or respond conservatively.",
            "Smith predictor estimates the current flow using the plant model.",
            "The delayed real measurement is still used to correct model error.",
        ],
        y=6.7,
    )
    box(
        ax,
        (1.05, 1.95),
        (13.9, 1.35),
        "Feedback estimate used by PID",
        "Q_feedback = Q_model_now + gain * (Q_measured_delayed - Q_model_delayed)",
        fc="#eefaf7",
        ec="#b8e4da",
        title_color=TEAL,
    )
    save(pdf, fig)


def slide_7(pdf):
    fig, ax = new_slide(pdf, "Smith Predictor Position")
    box(ax, (1.0, 5.7), (4.2, 1.0), "PPV command", "opening a")
    box(ax, (6.0, 5.7), (4.2, 1.0), "Plant model", "Q_model_now")
    box(ax, (11.0, 5.7), (4.2, 1.0), "Delay line", "Q_model_delayed")
    arrow(ax, (5.2, 6.2), (6.0, 6.2))
    arrow(ax, (10.2, 6.2), (11.0, 6.2))
    box(ax, (1.0, 3.5), (4.2, 1.0), "Sensor", "Q_measured_delayed")
    box(ax, (6.0, 3.5), (4.2, 1.0), "Correction", "measured - model")
    box(ax, (11.0, 3.5), (4.2, 1.0), "PID feedback", "predicted current flow")
    arrow(ax, (5.2, 4.0), (6.0, 4.0), color=TEAL)
    arrow(ax, (11.0, 4.0), (10.2, 4.0), color=TEAL)
    arrow(ax, (8.1, 5.7), (11.0, 4.35), color=TEAL)
    note(ax, "Smith predictor changes only the feedback signal. PID law and the actuator remain the same.")
    save(pdf, fig)


def slide_8(pdf):
    fig, ax = new_slide(pdf, "Parameters Needed")
    box(
        ax,
        (0.9, 4.8),
        (4.55, 2.0),
        "PID parameters",
        "Kp\nKi\nKd\nintegral_limit\ncontrol_dt",
        fc="#eef5ff",
        ec="#b8cffb",
        title_color=BLUE,
    )
    box(
        ax,
        (5.75, 4.8),
        (4.55, 2.0),
        "Feedforward model",
        "capacity_ml\ntau_in_s\ntau_out_s\nvalve_exponent",
        fc="#ffffff",
    )
    box(
        ax,
        (10.6, 4.8),
        (4.55, 2.0),
        "Smith predictor",
        "delay_s\ncorrection_gain\nsame plant model\nflow_sign",
        fc="#eefaf7",
        ec="#b8e4da",
        title_color=TEAL,
    )
    box(
        ax,
        (1.4, 2.1),
        (13.2, 1.15),
        "Current status",
        "These are currently initial values for simulation and early hardware testing; they should be identified from bench data before final validation.",
        fc="#fff5e9",
        ec="#f5d0a8",
        title_color=ORANGE,
    )
    save(pdf, fig)


def slide_9(pdf):
    fig, ax = new_slide(pdf, "PID Calibration Experiments")
    bullet_list(
        ax,
        [
            "Open-loop PPV step tests: 20%, 40%, 60%, 80%, 100% opening for inspiration and expiration.",
            "Estimate maximum flow, response speed, tau_in, tau_out, and valve nonlinearity.",
            "Closed-loop PI tuning: set Kd = 0, increase Kp until response is fast but not oscillatory.",
            "Increase Ki to remove steady flow error and reduce tidal volume error.",
            "Validate under multiple RR, I:E ratio, target volume, and target fill time settings.",
        ],
        y=6.85,
    )
    note(ax, "Evaluation: rise time, overshoot, steady-state flow error, tidal volume error, and cycle-to-cycle repeatability.")
    save(pdf, fig)


def slide_10(pdf):
    fig, ax = new_slide(pdf, "Smith Predictor Calibration Experiments")
    bullet_list(
        ax,
        [
            "Delay identification: apply a PPV opening step and measure command-to-flow response delay.",
            "Model alignment: compare delayed model output with measured flow after fitting tau and gamma.",
            "Correction gain sweep: test gain values such as 0.5, 0.75, 1.0, and 1.25.",
            "Smith on/off A-B test with the same PID and breathing parameters.",
            "Compare flow overshoot, oscillation, time to target volume, tidal volume error, and stability.",
        ],
        y=6.85,
    )
    note(ax, "Smith predictor is useful only if delay and plant model are close enough to the real pneumatic system.")
    save(pdf, fig)


def slide_11(pdf):
    fig, ax = new_slide(pdf, "Basic Hardware Calibration Checklist")
    bullet_list(
        ax,
        [
            "SFM3300 zero-flow offset: all valves closed, estimate offset and noise.",
            "Flow direction: verify inspiration and expiration signs, then set flow_sign.",
            "PPV40V voltage-flow mapping: confirm 0 V fully open and 5 V fully closed.",
            "Inspiration step response: fit tau_in and valve exponent.",
            "Expiration step response: fit tau_out under regulated vacuum.",
            "Safety validation: emergency stop, flow limit, host watchdog, and valve interlock.",
        ],
        y=6.85,
    )
    save(pdf, fig)


def slide_12(pdf):
    fig, ax = new_slide(pdf, "Summary")
    bullet_list(
        ax,
        [
            "PID is used in the flow feedback loop.",
            "PID controls measured flow, not pressure or direct volume.",
            "The continuous actuator is the PPV40V proportional pinch valve.",
            "Solenoid valves only select atmosphere or vacuum path.",
            "Smith predictor provides delay-compensated feedback to the PID.",
            "Calibration should identify sensor offset, flow sign, PPV mapping, model time constants, PID gains, and Smith delay.",
        ],
        y=6.9,
        fs=16,
    )
    box(
        ax,
        (1.1, 1.45),
        (13.8, 1.0),
        "One-sentence takeaway",
        "State machine chooses direction, PID modulates PPV opening, Smith predictor improves feedback timing.",
        fc="#eef5ff",
        ec="#b8cffb",
        title_color=BLUE,
    )
    save(pdf, fig)


def main() -> None:
    with PdfPages(OUT) as pdf:
        slide_1(pdf)
        slide_2(pdf)
        slide_3(pdf)
        slide_4(pdf)
        slide_5(pdf)
        slide_6(pdf)
        slide_7(pdf)
        slide_8(pdf)
        slide_9(pdf)
        slide_10(pdf)
        slide_11(pdf)
        slide_12(pdf)
    print(OUT)


if __name__ == "__main__":
    main()
