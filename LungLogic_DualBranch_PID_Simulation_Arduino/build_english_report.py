from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch, Rectangle


OUT = Path(__file__).resolve().parent / "single_lung_control_design_report.pdf"
plt.rcParams["font.family"] = "DejaVu Sans"
plt.rcParams["axes.unicode_minus"] = False


SLIDES = [
    (
        "Single-Lung Pneumatic PID Control Design",
        [
            "Purpose: emulate one 250 mL dynamic lung using a pneumatic circuit, control logic, and UI.",
            "The UI converts respiratory requirements into valve commands and flow targets.",
            "Hardware abstraction: one atmosphere solenoid, one vacuum solenoid, and one proportional pinch valve.",
            "Control objective: fill the lung to the target tidal volume, then exhaust it with repeatable timing.",
        ],
    ),
    (
        "From UI Parameters to Breathing Targets",
        [
            "Respiratory rate defines the cycle period: Tcycle = 60 / RR.",
            "I:E ratio splits the cycle into inspiration time and expiration time.",
            "Target tidal volume defines the desired volume change per breath, default 250 mL.",
            "Target fill time defines the inspiration flow reference: Qin = 0.06 x Vtarget / Tfill.",
            "Example: Vtarget = 250 mL and Tfill = 1.30 s gives Qin = 11.54 SLM.",
            "Expiration reference is displayed as negative flow because air is leaving the lung.",
        ],
    ),
    (
        "State Machine",
        [
            "PRECONDITION: exhaust the model before the formal breathing cycles start.",
            "DEAD_TO_INSP: both solenoids closed before switching to inspiration.",
            "INSPIRATION: atmosphere solenoid open, vacuum solenoid closed, pinch valve modulates inflow.",
            "DEAD_TO_EXP: both solenoids closed before switching to expiration.",
            "EXPIRATION: atmosphere solenoid closed, vacuum solenoid open, pinch valve modulates exhaust flow.",
            "COMPLETE: stop after the configured number of cycles.",
        ],
    ),
    (
        "Solenoid Valve Simulation",
        [
            "A compact valve_mask represents the simulated digital outputs.",
            "bit 0 = atmosphere solenoid command.",
            "bit 1 = vacuum solenoid command.",
            "mask 00: both closed, used during dead time and stop states.",
            "mask 01: atmosphere path open for inspiration.",
            "mask 10: vacuum path open for expiration/preconditioning.",
            "This can be mapped directly to STM32 digital output pins later.",
        ],
    ),
    (
        "Pinch Valve and PID Control",
        [
            "The solenoid valves choose flow direction; the pinch valve controls flow magnitude.",
            "Normalized opening a ranges from 0 closed to 1 fully open.",
            "The valve command is Ucmd = 5(1 - a) V, so 0 V is fully open and 5 V is closed.",
            "The controller runs at 100 Hz by default and compares target flow magnitude with measured flow magnitude.",
            "PID law: a = clip(a_ff + Kp e + Ki integral(e dt) + Kd d, 0, 1).",
            "Feedforward estimates the approximate opening first; PID corrects model and sensor errors.",
        ],
    ),
    (
        "Single-Lung Plant Model",
        [
            "The model is a first-order grey-box pneumatic lung model.",
            "Inspiration: dV/dt = a^gamma (Vmax - V) / tau_in.",
            "Expiration: dV/dt = -a^gamma V / tau_out.",
            "Flow conversion: Q = 0.06 dV/dt in SLM.",
            "Transport delay and sensor noise are added to make the measured flow more realistic.",
            "The UI displays volume, measured flow, reference flow, pinch opening, command voltage, and valve state.",
        ],
    ),
    (
        "Summary for Presentation",
        [
            "The system is a state-machine-based closed-loop pneumatic controller.",
            "UI parameters define timing and target flow; the state machine selects the pneumatic path.",
            "Solenoid valves provide direction control: atmosphere for filling, vacuum for exhausting.",
            "The proportional pinch valve provides continuous flow regulation.",
            "A feedforward plus PID controller tracks the desired flow and produces a 0-5 V valve command.",
            "The current software is a simulation/digital twin, but the same valve_mask and command voltage can be mapped to hardware.",
        ],
    ),
]


def add_text_slide(pdf, title, bullets):
    fig = plt.figure(figsize=(11.69, 8.27))
    ax = fig.add_axes([0, 0, 1, 1])
    ax.axis("off")
    ax.text(0.06, 0.90, title, fontsize=24, fontweight="bold", color="#17345c")
    y = 0.80
    for bullet in bullets:
        words = bullet.split()
        wrapped = []
        line = ""
        for word in words:
            test = (line + " " + word).strip()
            if len(test) > 92:
                wrapped.append(line)
                line = word
            else:
                line = test
        if line:
            wrapped.append(line)
        ax.text(0.08, y, "- " + wrapped[0], fontsize=15, va="top", color="#10243f")
        for extra in wrapped[1:]:
            y -= 0.045
            ax.text(0.105, y, extra, fontsize=15, va="top", color="#10243f")
        y -= 0.075
    ax.text(0.06, 0.05, "Single-Lung Pneumatic Control System | 250 mL Model", fontsize=10, color="#667")
    pdf.savefig(fig)
    plt.close(fig)


def draw_input_row(ax, panel_x, row_y, label, value, label_size=9.0, value_size=8.6):
    if value == "checked":
        ax.add_patch(Rectangle((panel_x + 0.040, row_y - 0.013), 0.015, 0.015, fc="white", ec="#777", lw=0.8))
        ax.text(panel_x + 0.0475, row_y - 0.005, "x", fontsize=7, ha="center", va="center")
        ax.text(panel_x + 0.065, row_y - 0.005, label, fontsize=label_size, va="center", color="#111")
        return
    ax.text(panel_x + 0.040, row_y, label, fontsize=label_size, va="center", color="#111")
    box_x, box_w = panel_x + 0.350, 0.255
    ax.add_patch(Rectangle((box_x, row_y - 0.016), box_w, 0.028, fc="white", ec="#a8a8a8", lw=0.8))
    ax.text(box_x + 0.010, row_y - 0.002, value, fontsize=value_size, va="center", color="#111")
    ax.text(box_x + box_w - 0.016, row_y - 0.002, "^/v", fontsize=5.8, va="center", ha="center", color="#555")


def add_ui_parameters_slide(pdf):
    fig = plt.figure(figsize=(11.69, 8.27))
    ax = fig.add_axes([0, 0, 1, 1])
    ax.axis("off")
    ax.text(
        0.06,
        0.92,
        "UI Parameters Used in the Single-Lung Controller",
        fontsize=22,
        fontweight="bold",
        color="#17345c",
    )
    ax.text(
        0.06,
        0.865,
        "This page reproduces the operator-facing parameters from the PyQt GUI.",
        fontsize=12.5,
        color="#10243f",
    )

    panel_x, panel_y, panel_w, panel_h = 0.18, 0.075, 0.64, 0.74
    ax.add_patch(Rectangle((panel_x, panel_y), panel_w, panel_h, fc="#f4f4f4", ec="#c8c8c8", lw=1.0))

    groups = [
        (
            "Breathing Parameters",
            [
                ("Respiratory rate / bpm", "15.00"),
                ("I:E inspiration", "1.00"),
                ("I:E expiration", "2.00"),
                ("Target tidal volume / mL", "250.0"),
                ("Target fill time / s", "1.30"),
                ("Simulation cycles", "5"),
                ("Playback speed", "1.00"),
            ],
        ),
        (
            "Flow PID Control",
            [
                ("Kp / opening per SLM", "0.0220"),
                ("Ki / opening per SLM s", "0.2100"),
                ("Kd / opening s per SLM", "0.0000"),
                ("Integral limit", "2.00"),
                ("Enable model feedforward", "checked"),
            ],
        ),
        (
            "Lung and Valve Model",
            [
                ("Lung Vmax / mL", "250.0"),
                ("Tau_in / s", "0.570"),
                ("Tau_out / s", "0.500"),
                ("Pinch valve exponent gamma", "1.15"),
                ("Transport delay / s", "0.025"),
                ("Flow noise sigma / SLM", "0.10"),
                ("Direction-change dead time / s", "0.080"),
                ("Precondition time / s", "1.50"),
            ],
        ),
    ]

    y = panel_y + panel_h - 0.040
    for title, rows in groups:
        group_h = 0.048 + len(rows) * 0.038 + 0.026
        ax.text(panel_x + 0.022, y + 0.014, title, fontsize=10.2, color="#111", va="center")
        ax.add_patch(
            Rectangle((panel_x + 0.022, y - group_h + 0.012), panel_w - 0.044, group_h, fc="#eeeeee", ec="#d2d2d2", lw=0.8)
        )
        row_y = y - 0.026
        for label, value in rows:
            draw_input_row(ax, panel_x, row_y, label, value)
            row_y -= 0.038
        y -= group_h + 0.024

    ax.add_patch(Rectangle((panel_x + 0.022, panel_y + 0.015), panel_w - 0.044, 0.031, fc="#eef8ff", ec="#b8cde0", lw=0.8))
    ax.text(panel_x + panel_w / 2, panel_y + 0.030, "Save and Apply Parameters", fontsize=9.3, fontweight="bold", ha="center", va="center")
    ax.text(0.06, 0.04, "Single-Lung Pneumatic Control System | UI Parameters", fontsize=10, color="#667")
    pdf.savefig(fig)
    plt.close(fig)


def add_ui_conversion_slide(pdf):
    fig = plt.figure(figsize=(11.69, 8.27))
    ax = fig.add_axes([0, 0, 1, 1])
    ax.axis("off")
    ax.text(0.06, 0.91, "How UI Parameters Become Control Actions", fontsize=22, fontweight="bold", color="#17345c")
    ax.text(
        0.06,
        0.855,
        "The GUI values are converted into timing, flow references, state-machine outputs, and PPV voltage.",
        fontsize=12.5,
        color="#10243f",
    )

    ax.text(0.08, 0.775, "Parameter Conversion", fontsize=16, fontweight="bold", color="#17345c")
    left_items = [
        ("Breathing timing", "RR and I:E calculate inspiration and expiration duration."),
        ("Flow reference", "Target volume and fill time define the desired inflow."),
        ("Valve direction", "The state machine selects ATM open or VAC open."),
    ]
    right_items = [
        ("Flow regulation", "Kp, Ki, Kd and feedforward compute PPV opening."),
        ("Plant realism", "Vmax, tau, gamma, delay and noise simulate the lung."),
        ("Safety timing", "Dead time closes both solenoids during direction changes."),
    ]

    def draw_items(items, x, y):
        for head, body in items:
            ax.text(x, y, "- " + head + ":", fontsize=13.2, fontweight="bold", va="top", color="#10243f")
            ax.text(x + 0.025, y - 0.034, body, fontsize=12.2, va="top", color="#10243f")
            y -= 0.105

    draw_items(left_items, 0.08, 0.715)
    draw_items(right_items, 0.54, 0.715)

    ax.text(0.08, 0.385, "Example Calculation", fontsize=16, fontweight="bold", color="#17345c")
    ax.add_patch(Rectangle((0.08, 0.135), 0.84, 0.215, fc="#eef5fb", ec="#aac2d8", lw=1.0))
    example_lines = [
        ("RR = 15 bpm", "Tcycle = 60 / 15 = 4 s"),
        ("I:E = 1:2", "Tin = 4 x 1/(1+2) = 1.33 s    Tout = 4 x 2/(1+2) = 2.67 s"),
        ("Vtarget = 250 mL, Tfill = 1.30 s", "Qin = 0.06 x 250 / 1.30 = 11.54 SLM"),
    ]
    y = 0.305
    for left, right in example_lines:
        ax.text(0.115, y, left, fontsize=12.5, fontweight="bold", color="#10243f", va="center")
        ax.text(0.425, y, right, fontsize=12.5, color="#10243f", va="center")
        y -= 0.062

    ax.text(0.08, 0.095, "Result: the controller knows when to switch states, which solenoid to open, and what flow to track.", fontsize=12.5, color="#10243f")
    ax.text(0.06, 0.05, "Single-Lung Pneumatic Control System | UI Parameter Conversion", fontsize=10, color="#667")
    pdf.savefig(fig)
    plt.close(fig)


def add_plant_realism_details_slide(pdf):
    rows_1 = [
        (
            "Lung Vmax",
            "Maximum physical lung volume.\nHere it is set to 250 mL because\nactual single-lung capacity is 250 mL.",
            "Based on phantom geometry\nor measured maximum volume.",
        ),
        (
            "tau_in",
            "Inspiration time constant.\nSmaller tau means the lung fills faster\nfor the same valve opening.",
            "Fitted from inflation data:\nV(t)=Vmax(1-exp(-t/tau_in)).",
        ),
        (
            "tau_out",
            "Expiration time constant.\nSmaller tau means the lung exhausts\nfaster under vacuum.",
            "Fitted from exhaust data:\nV(t)=V0 exp(-t/tau_out).",
        ),
        (
            "gamma",
            "Pinch valve nonlinearity.\nThe model uses:\neffective opening = opening^gamma.",
            "Fitted from valve calibration:\ncommand/opening versus measured flow.",
        ),
    ]
    rows_2 = [
        (
            "transport delay",
            "Delay between a valve command change\nand the flow sensor response.",
            "Measured from step-response tests.\nIncludes tubing and sensor/filter delay.",
        ),
        (
            "flow noise sigma",
            "Standard deviation of random flow noise\nadded to the simulated sensor.",
            "Estimated from steady zero-flow\nor constant-flow sensor readings.",
        ),
        (
            "dead time",
            "Time when both solenoids are closed\nduring direction changes.",
            "Chosen from solenoid response time\nand safety margin to avoid ATM-VAC\ncross-connection.",
        ),
        (
            "precondition time",
            "Initial exhaust period before formal\nbreathing cycles begin.",
            "Chosen experimentally so each run starts\nfrom a consistent low lung volume.",
        ),
    ]

    add_plant_table_slide(pdf, "Plant Realism Parameters", rows_1, "Model parameters describing lung volume response and pinch-valve nonlinearity.")
    add_plant_table_slide(pdf, "Plant Realism Parameters Continued", rows_2, "Model parameters describing sensor realism, safe valve switching, and repeatable initial conditions.")


def add_plant_table_slide(pdf, title, rows, subtitle):
    fig = plt.figure(figsize=(11.69, 8.27))
    ax = fig.add_axes([0, 0, 1, 1])
    ax.axis("off")
    ax.text(0.06, 0.91, title, fontsize=22, fontweight="bold", color="#17345c")
    ax.text(0.06, 0.855, subtitle, fontsize=12.5, color="#10243f")

    x0 = 0.06
    y = 0.770
    col_w = [0.18, 0.43, 0.33]
    table_w = sum(col_w)
    ax.add_patch(Rectangle((x0, y + 0.015), table_w, 0.048, fc="#17345c", ec="#17345c", lw=0))
    ax.text(x0 + 0.014, y + 0.039, "Parameter", fontsize=12, fontweight="bold", color="white", va="center")
    ax.text(x0 + col_w[0] + 0.014, y + 0.039, "Meaning in the model", fontsize=12, fontweight="bold", color="white", va="center")
    ax.text(x0 + col_w[0] + col_w[1] + 0.014, y + 0.039, "Basis / how to determine it", fontsize=12, fontweight="bold", color="white", va="center")

    y -= 0.018
    row_h = 0.130
    for i, (name, meaning, basis) in enumerate(rows):
        fc = "#f3f7fb" if i % 2 == 0 else "white"
        ax.add_patch(Rectangle((x0, y - row_h), table_w, row_h, fc=fc, ec="#c8d3df", lw=0.7))
        ax.text(x0 + 0.014, y - 0.026, name, fontsize=11.5, fontweight="bold", color="#10243f", va="top")
        ax.text(x0 + col_w[0] + 0.014, y - 0.024, meaning, fontsize=11.0, color="#10243f", va="top", linespacing=1.35)
        ax.text(x0 + col_w[0] + col_w[1] + 0.014, y - 0.024, basis, fontsize=11.0, color="#10243f", va="top", linespacing=1.35)
        y -= row_h

    ax.text(
        0.06,
        0.085,
        "Current tau, gamma, delay and noise values are initial estimates; they should be refined using bench-test data.",
        fontsize=11.2,
        color="#10243f",
    )
    ax.text(0.06, 0.045, "Single-Lung Pneumatic Control System | Plant Realism", fontsize=10, color="#667")
    pdf.savefig(fig)
    plt.close(fig)


def add_ui_conversion_slide_old_unused(pdf):
    fig = plt.figure(figsize=(11.69, 8.27))
    ax = fig.add_axes([0, 0, 1, 1])
    ax.axis("off")
    ax.text(0.06, 0.90, "How the UI Parameters Become Control Actions", fontsize=22, fontweight="bold", color="#17345c")
    ax.text(
        0.06,
        0.84,
        "The GUI values are not sent directly to the valves. They are first converted into timing, flow references, and state-machine outputs.",
        fontsize=12.5,
        color="#10243f",
    )

    text_x = 0.08
    ax.text(text_x, 0.76, "Parameter conversion", fontsize=16, fontweight="bold", color="#17345c")
    bullets = [
        ("Breathing timing", "RR and I:E calculate inspiration and expiration duration."),
        ("Flow reference", "250 mL and 1.30 s produce an inspiration target of 11.54 SLM."),
        ("Valve direction", "The state machine selects ATM open for inspiration or VAC open for expiration."),
        ("Flow regulation", "Kp, Ki, Kd and feedforward compute PPV opening from measured flow error."),
        ("Plant realism", "Vmax, tau, gamma, delay and noise simulate pneumatic lung and sensor response."),
        ("Safety timing", "Dead time closes both solenoids during direction changes."),
    ]
    y2 = 0.70
    for head, body in bullets:
        ax.text(text_x, y2, "- " + head + ":", fontsize=13.5, fontweight="bold", va="top", color="#10243f")
        ax.text(text_x + 0.025, y2 - 0.035, body, fontsize=13.0, va="top", color="#10243f")
        y2 -= 0.095

    formula_x = 0.62
    ax.add_patch(Rectangle((formula_x - 0.02, 0.22), 0.32, 0.48, fc="#eef5fb", ec="#aac2d8", lw=1.0))
    ax.text(formula_x, 0.64, "Example values", fontsize=15, fontweight="bold", color="#17345c")
    ax.text(formula_x, 0.58, "RR = 15 bpm", fontsize=12.5, color="#10243f")
    ax.text(formula_x, 0.53, "Tcycle = 60 / 15 = 4 s", fontsize=12.5, color="#10243f")
    ax.text(formula_x, 0.48, "I:E = 1:2", fontsize=12.5, color="#10243f")
    ax.text(formula_x, 0.43, "Tin = 1.33 s", fontsize=12.5, color="#10243f")
    ax.text(formula_x, 0.38, "Tout = 2.67 s", fontsize=12.5, color="#10243f")
    ax.text(formula_x, 0.32, "Qin = 0.06 x 250 / 1.30", fontsize=12.5, color="#10243f")
    ax.text(formula_x, 0.27, "Qin = 11.54 SLM", fontsize=12.5, fontweight="bold", color="#10243f")
    ax.text(0.06, 0.05, "Single-Lung Pneumatic Control System | UI Parameter Conversion", fontsize=10, color="#667")
    pdf.savefig(fig)
    plt.close(fig)


def add_state_diagram(pdf):
    fig = plt.figure(figsize=(11.69, 8.27))
    ax = fig.add_axes([0, 0, 1, 1])
    ax.axis("off")
    ax.text(0.06, 0.90, "State Transition Diagram", fontsize=24, fontweight="bold", color="#17345c")
    nodes = [
        ("PRECONDITION", 0.13, 0.56),
        ("DEAD_TO_INSP", 0.36, 0.56),
        ("INSPIRATION", 0.59, 0.56),
        ("DEAD_TO_EXP", 0.36, 0.28),
        ("EXPIRATION", 0.59, 0.28),
        ("COMPLETE", 0.82, 0.28),
    ]
    pos = {}
    for label, x, y in nodes:
        pos[label] = (x, y)
        box = FancyBboxPatch((x - 0.085, y - 0.045), 0.17, 0.09, boxstyle="round,pad=0.012", fc="#eef5fb", ec="#2f6f9f", lw=1.5)
        ax.add_patch(box)
        ax.text(x, y, label, ha="center", va="center", fontsize=12, fontweight="bold")
    edges = [
        ("PRECONDITION", "DEAD_TO_INSP"),
        ("DEAD_TO_INSP", "INSPIRATION"),
        ("INSPIRATION", "DEAD_TO_EXP"),
        ("DEAD_TO_EXP", "EXPIRATION"),
        ("EXPIRATION", "COMPLETE"),
    ]
    for a, b in edges:
        x1, y1 = pos[a]
        x2, y2 = pos[b]
        ax.add_patch(FancyArrowPatch((x1 + 0.09, y1), (x2 - 0.09, y2), arrowstyle="-|>", mutation_scale=16, lw=1.4, color="#555"))
    ax.add_patch(
        FancyArrowPatch((0.59, 0.23), (0.36, 0.23), connectionstyle="arc3,rad=-0.35", arrowstyle="-|>", mutation_scale=16, lw=1.4, color="#555")
    )
    ax.text(0.47, 0.16, "repeat until configured cycles are complete", ha="center", fontsize=12, color="#555")
    notes = [
        "Dead-time states close both solenoids to avoid connecting atmosphere and vacuum simultaneously.",
        "Inspiration ends by elapsed inspiration time or by reaching the 250 mL target.",
        "Expiration ends by elapsed expiration time, then the next cycle starts.",
    ]
    y = 0.76
    for note in notes:
        ax.text(0.08, y, "- " + note, fontsize=14, color="#10243f")
        y -= 0.06
    ax.text(0.06, 0.05, "Single-Lung Pneumatic Control System | State Machine", fontsize=10, color="#667")
    pdf.savefig(fig)
    plt.close(fig)


with PdfPages(OUT) as pdf:
    add_text_slide(pdf, *SLIDES[0])
    add_ui_parameters_slide(pdf)
    add_ui_conversion_slide(pdf)
    add_plant_realism_details_slide(pdf)
    add_text_slide(pdf, *SLIDES[1])
    add_state_diagram(pdf)
    for slide in SLIDES[2:]:
        add_text_slide(pdf, *slide)

print(OUT)
