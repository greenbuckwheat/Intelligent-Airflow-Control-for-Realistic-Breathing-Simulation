# Single-branch prototype requirement check

The original slide describes a 500 mL, two-branch system. This prototype has one
250 mL fake lung. The breathing rate (12-18 bpm), nominal rate (15 bpm), I:E
(1:2), and 30-cycle repeatability goal are useful prototype targets. Passing a
250 mL single-branch simulation does not establish the original 500 mL or
independent left/right requirements.

## Current simulated result

Run `python single_lung_requirement_benchmark.py` from this directory. The
script uses the saved configuration as a starting point, simulates 30 cycles
at each rate, and writes the measured ranges to
`output/requirement_benchmark/single_lung_30_cycle_benchmark.md` and `.csv`.
It does not alter the saved configuration or connect to hardware.

With the current assumed finite-fill model (250 mL from 0 mL in 1.30 s with a
fully open atmosphere inspiration path), 80 ms direction-change dead time included
in each nominal cycle, and 25 ms assumed input delay. Normal direction changes
close both routing solenoids while holding the PPV command opening; the next
phase starts from that opening. Stop, fault and completion close the PPV:

| Rate | Simulated 30-cycle tidal range | Cadence | 250 mL +/-5% |
| ---: | ---: | ---: | --- |
| 12 bpm | 245.2-250.0 mL | 12.00 bpm | Pass |
| 15 bpm | 242.6-246.3 mL | 15.00 bpm | Pass |
| 16 bpm | 237.3-240.8 mL | 16.00 bpm | Fail (0.2 mL below lower limit) |
| 18 bpm | 227.2-231.1 mL | 18.00 bpm | Fail |

The simulation also checks cycle-to-cycle spread against 5% of the target.
The flow PID at 15 bpm gives 214.9-219.8 mL with the same nominal
cycle timing. The optional *volume deadline guard* increases modeled pinch
valve opening when its assumed plant model predicts that the current opening
will miss the end-of-inspiration volume. It uses the simulator's true lung
volume, so it cannot be transferred directly to the physical controller.

## Flow setpoint and missing calibration

The dashed flow line in Live Curves and Live Monitor is the controller's flat
setpoint, calculated from tidal volume divided by the selected fill time. The
selected bpm and I:E determine whether that fill time fits in the inspiration
phase. This calculation specifies an average flow; it does not identify the
instantaneous flow shape or prove that the valve can track it. `Orifice.mlx`
provides recovery times and a fitted normalized curve, while the budget PDF
provides a design limit and a model prediction. Neither contains a measured,
time-stamped flow trace through the assembled atmosphere-inspiration path. A
physical tracking reference needs synchronized flow, volume, valve command,
and pressure traces for full-open atmosphere inspiration and vacuum expiration
before its shape and feasible range can be established.

## Uncertainty sweep before hardware is available

Run `python single_lung_uncertainty_sweep.py` from this directory. Its
[report](output/uncertainty_sweep/uncertainty_report.md),
[30-cycle map](output/uncertainty_sweep/controlled_outcome_map.png), and CSVs
record 108 hypothetical 30-cycle runs (27 at each of 12, 15, 16 and 18 bpm). The assumed atmosphere-path 1.30 s endpoint stays
fixed while the unknown fill-curve shape, assumed command delay, and expiration
time constant change. The controller keeps one nominal model (0.57 s inspiration
shape, 0.50 s expiration constant, 25 ms assumed delay), while the simulated
plant varies. The current saved PID/Smith gains are used, but a manual
expiration-flow target is replaced by a rate-derived one for comparison. The
script never saves these changes to the GUI configuration.

| Rate | Hypothetical cells passing simulation | Cells with insufficient full-open model bound | Other control failures |
| ---: | ---: | ---: | ---: |
| 12 bpm | 18 / 27 | 0 | 9 |
| 15 bpm | 18 / 27 | 0 | 9 |
| 16 bpm | 8 / 27 | 6 | 13 |
| 18 bpm | 3 / 27 | 18 | 6 |

These counts are **not probabilities**. The parameter ranges are illustrative,
not measured confidence limits. The separate
[installed-path plot](output/uncertainty_sweep/installed_path_limit.png) shows
analytic full-open limits if the final assembled path takes longer than the
assumed 1.30 s full-open atmosphere path. Its slower reach times are hypothetical, and it
does not simulate a controller. Together, these results show which assumptions
must be measured before treating any PID tuning result as physically useful.
The three simulated passes at 18 bpm use full valve opening throughout
inspiration and reach the lower tolerance by less than 1 mL in their weakest
cycles, so they provide no useful modeled throttling margin.

### Interpreting the 15-16 bpm boundary

The current default assumption passes 15 bpm, while at 16 bpm the weakest of
30 cycles reaches 237.3 mL, just below the 237.5 mL lower limit. The 16 bpm
full-open model bound is 241.4 mL, so this particular 0.2 mL miss is a
controlled-run miss rather than a hard full-open capacity limit. It is still
too close to the threshold to treat 16 bpm as a demonstrated operating range.

At 15 bpm, all nine cases with a 1.00 s expiration time constant fail, while
all 18 cases with 0.30 or 0.50 s expiration constants pass. At 16 bpm, the
slowest assumed inspiration shape (tau_in = 0.85 s) has no passing case and
six cases whose fully open model bound is insufficient. The two faster shapes
pass in eight of 18 cases. A 1.00 s expiration constant fails all nine
16 bpm cases. The detailed report separates each parameter value and failure
type; varying one parameter also varies the other two, so these counts do not
establish a measured sensitivity or a probability of success.

This stress study uses the saved controller settings and its default internal
true-volume feedback. The sensor-integrated scan below has two additional
numerical passes at 16 bpm. Different feedback assumptions can therefore move
a case across the 237.5 mL boundary. The nominal 15 bpm target remains the
supported simulation result; 16 bpm is a boundary case to investigate, not a
reliable physical capability claim. The assumed atmosphere-path 1.30 s endpoint is
held fixed throughout this grid; the installed path may fill more slowly.

## Sensor-integrated feedback comparison

Run `python single_lung_sensor_feedback_study.py`. Its
[report](output/sensor_feedback_study/sensor_feedback_report.md),
[comparison map](output/sensor_feedback_study/sensor_feedback_map.png), and CSVs
compare the same 81 modeled plant cases with internal true-volume feedback and
zero-calibrated integration of simulated sensor flow. The latter includes the
existing transport delay, sensor lag, sample noise and an optional fixed zero
bias. Its Smith predictor uses an assumed command delay rather than the
simulator's actual delayed valve command.

| Rate | True-volume feedback passes | Sensor-integrated feedback passes |
| ---: | ---: | ---: |
| 12 bpm | 18 / 27 | 18 / 27 |
| 15 bpm | 18 / 27 | 18 / 27 |
| 16 bpm | 8 / 27 | 10 / 27 |
| 18 bpm | 3 / 27 | 4 / 27 |

The additional 18 bpm numerical pass is a boundary case near the 237.5 mL
lower tolerance, with almost continuous full valve opening. It is not evidence
that a delayed sensor improves the physical system. Under a selected longer
sensor-lag scenario, the simulated end-of-inspiration volume estimate differs
from the internal lung state by as much as 10.4 mL at 15 bpm. The estimator
assumes an initially empty lung, zero flow during precondition, fixed sensor
bias, and comparable standard and geometric volumes. Its output has not been
validated against a physical reference volume.

To try this in the GUI, set **Volume feedback source** to **Integrate simulated
sensor flow** in Developer Mode and apply. **Flow sensor zero bias / SLM** is
an optional simulation stress input. The User Mode tidal-volume readout still
uses the simulated lung state to audit what actually happened in the model.
The Developer Mode volume plot overlays the sensor-integrated estimate as a
dashed curve, and Live Monitor lists both values.

The 18 bpm failure depends on the assumed intermediate flow curve. At the
current 0.57 s shape parameter, a fully open path from 0 mL has an estimated
231.9 mL ceiling in the available inspiration window, below the 237.5 mL
lower tolerance. The 1.30 s full-fill endpoint is a simulation assumption;
`Orifice.mlx` uses 1.30 s as a design limit and predicts 250 mL in about
0.34 s for a 10 mm path under a different 558 mL capacity model. The
installed flow curve, delays, valve response, and expiration behavior
have not been measured. Thus the result does not prove a physical 18 bpm
limit, and the simulated 12/15 bpm passes are not hardware acceptance results.

## What would make 18 bpm pass in this simulation?

Run `python single_lung_18bpm_tradeoff.py` for the reproducible
[30-cycle what-if report](output/18bpm_tradeoff/18bpm_tradeoff.md) and CSV. It
uses sensor-integrated volume feedback and a fixed nominal controller model,
so changing a hypothetical plant parameter does not silently retune the
controller. It does not change the saved GUI settings.

At 18 bpm and I:E 1:2, the 1.111 s inspiration slot contains an assumed
80 ms direction-change dead time and 25 ms command delay. With the current
1.30 s finite-fill curve, even full opening reaches only 231.9 mL from an
empty lung in that slot. The 30-cycle tidal range is 227.7-231.1 mL. PID
gain changes cannot raise flow beyond the full-open model limit. Removing
only the dead time gives at most 237.3 mL from empty and still fails the
237.5 mL lower tolerance. Removing only the input delay also fails.

If I:E is adjustable, I:E 1:1.6 reaches 239.2-243.3 mL and passes the
250 mL +/-5% simulation check, but the valve stays fully open throughout
inspiration. I:E 1:1.2 reaches 242.1-250.0 mL with about 48% of inspiration
samples at full opening. This gives modeled control room but shortens
expiration, so residual volume and the 30-cycle tidal range must still be
checked. I:E 1:1.2 with a hypothetical 1.50 s installed-path full-open reach
still passes narrowly (238.5-246.1 mL); 1.80 s fails. These longer path times
are stress cases, not observations.

If I:E 1:2 and 250 mL must both hold at 18 bpm, this particular plant model
needs more full-open flow than its current assumptions provide. Setting the
model's full-open reach to 1.10 s produces a simulated pass, but the assumed
1.30 s endpoint gives no basis for claiming such a faster physical path.
Reducing the target to 235 mL also produces a pass while changing the volume
requirement. None of these what-if changes should be reported as a validated
hardware solution. The most useful next simulation improvement is to retain
an uncertainty range for the unmeasured installed-path reach, intermediate
flow curve, delay, and expiratory dynamics, and report the robust passing
region rather than selecting one favorable trace.

## PID optimization design informed by extremum seeking

Killingsworth and Krstic, *Auto-Tuning of PID Controllers via Extremum
Seeking* (ACC 2005), use discrete extremum seeking (ES) to adjust PID
parameters after each completed closed-loop step-response experiment. The
experiment produces one cost value; small parameter perturbations let ES
estimate which direction lowers that cost. Their examples also show that the
choice of cost function changes the resulting transient response, and that
actuator saturation and integral windup matter. The paper's numerical gains,
perturbation amplitudes, and step-response objective are **not** calibration
data for this pneumatic system.

For this prototype, ES is a candidate **offline simulation tuner** for the
existing flow controller, starting from the current stable Kp and Ki with
Kd = 0. Use the program's direct coefficients (Kp, Ki, Kd), not the paper's
(K, Ti, Td) values; their forms correspond to Kp = K, Ki = K/Ti and Kd = K*Td.
Keep the lung model, I:E, target volume, Smith setting, feedforward setting,
and deadline-guard setting fixed during each comparison. Do not tune these
plant assumptions to make the controller score look better.

One candidate evaluation should reset to the same initial state and random
seed, simulate at least 30 breaths at each selected rate, and score the full
cycle record rather than one flow step. A project-specific candidate cost is

    J = 4 * mean(abs(tidal_mL - 250)) / 250
      + 2 * max(abs(tidal_mL - 250)) / 250
      + 2 * (max(tidal_mL) - min(tidal_mL)) / 250
      + 0.10 * flow_RMSE_SLM / 12
      + 0.01 * mean(inspiration_opening >= 0.999)
      + 10 * max(0, max(abs(tidal_mL - 250)) / 250 - 0.05)

The constants above are declared project choices and may be revised in a
separate study. Cycle-to-cycle tidal spread, valve saturation, residual volume,
and the worst tidal error
must also be reported separately so a good average cannot hide a failed
breath. Reject candidates violating the 250 mL +/-5% check, cadence, or
other chosen operating constraints. This multi-term cost is a **proposed
project adaptation**; the paper uses step-response ISE, IAE and other
time-weighted or windowed errors, and does not prescribe this lung objective.

Tune at nominal 15 bpm first, then evaluate the same gains at 12 and boundary
16 bpm. Repeat the comparison over the documented illustrative plant grid
(fill-curve shape, input delay, expiration time constant), keeping the
controller's assumed plant fixed. Favor a candidate whose worst cases improve
over the starting gains; do not treat the fraction of hypothetical grid cells
passing as a probability. Compare ES against the unchanged baseline and a
small manual/grid search under identical scenarios. Start with Kp and Ki;
introduce Kd only if its benefit exceeds the effect of noisy delayed flow.

The existing conditional integration and opening limits should remain active
in every trial. Record saturation explicitly: when the modeled PPV is fully
open and the full-open volume bound is below the target, gain tuning cannot
recover missing pneumatic capacity. In the current baseline, 16 bpm is a
model-sensitive boundary; ES should not be presented as a way to guarantee
250 mL there. Actual online tuning would require validated flow-to-volume
feedback, controllable PPV hardware, repeatable cycle starts, and bounded
parameter trials; those conditions are not yet available.

### Completed offline tuning trial

Run `python single_lung_pid_es_study.py --iterations 20` for the reproducible
[report](output/pid_es_study/report.md), nominal comparison, perturbation
history, plant-grid CSV and exact input snapshot. The script uses a bounded
two-sided perturbation search inspired by ES, rather than the paper's exact
discrete filter. It does not change the saved GUI gains. Tuning uses simulated
sensor-integrated volume feedback and leaves the volume deadline guard off so
the PI contribution remains visible. Smith and model feedforward stay fixed.

The selected trial changes Kp from 0.01000 to 0.01198 and Ki from 0.06000 to
0.24000, with Kd held at zero. At nominal 15 bpm and without the guard, the
30-cycle tidal range rises from 212.8-217.8 to 215.2-220.2 mL, but **both
fail** the 237.5 mL lower tolerance. Flow RMSE increases from 2.17 to
2.20 SLM. Across the 27 illustrative plant cases at 15 bpm, neither gain
set passes without the guard. At 12 bpm, the tuned set passes 9/27 versus
8/27 for the baseline; at 16 bpm neither passes any of the 27 cases.

With the guard enabled, nominal 15 bpm reaches 243.1-246.3 mL for baseline
and 243.2-246.3 mL for tuned gains; the PPV command is fully open for almost
all inspiration samples in both cases. The tiny difference is not useful evidence of an
improved controller. In this study, sensor-integrated feedback produces a
narrow nominal 16 bpm pass with the guard, unlike the earlier true-volume
benchmark. This reinforces that 16 bpm is sensitive to feedback assumptions.
The tuned gains remain a research candidate and are not recommended as a
replacement for the current saved gains.

### Decision after inspecting the 15 bpm flow trace

The GUI still uses Kp=0.0100, Ki=0.0600 and Kd=0; the offline ES-inspired
candidate was never applied to its saved configuration. A 30-cycle ablation
using that saved 15 bpm, 250 mL, I:E 1:2 configuration gave:

| Change from saved configuration | Tidal range / mL | Peak inspiration flow / SLM | Inspiration PPV fully open |
| --- | ---: | ---: | ---: |
| None; guard, feedforward and Smith on | 242.6-246.3 | 26.0 | 100% |
| Guard off | 214.9-219.8 | 13.6 | 35% |
| Feedforward off, guard on | 242.4-246.3 | 26.0 | 99% |
| Smith off, guard on | 242.6-246.3 | 26.0 | 100% |

Thus the visible high-flow pulse is primarily the volume deadline guard
requesting full opening, rather than the new gain search or Smith prediction.
At 15 bpm, I:E 1:2 provides a 1.333 s inspiration slot; 80 ms of direction
dead time and about 20 ms of sampled input delay leave roughly 1.23 s of
flow response. The requested flat flow of about 11.54 SLM cannot provide
250 mL in that time, and the modeled full-open flow falls as volume rises.
The current controller cannot simultaneously hold that flat flow and reach
the 250 mL target. A physically validated faster path, more inspiration time,
or a lower volume target would change this tradeoff; changing PI gains alone
cannot remove it.

Keep the offline tuning script as an analysis tool, but do not adopt its
candidate gains as the default or present the current guard-on trace as
successful flat-flow tracking. Keep the guard available as an explicit
volume-priority simulation option and report its peak flow and saturation.

## Reproduce in the GUI

1. In Developer Mode, set **Full-open reach time** to 1.30 s. The only
   inspiratory response is the assumed finite-fill model.
2. Check **Include direction dead time within bpm period** and **Enable volume
   deadline guard (simulation)**.
3. Set **Simulation cycles** to 30, **Respiratory rate** to 15 bpm, **I:E** to
   1:2, and **Target tidal volume** to 250 mL. Apply the parameters.
4. Switch to User Mode to play the modeled lung and flow curve. The curve is
   precomputed simulation playback. The benchmark report evaluates all 30
   cycles; the GUI's status readout currently shows the last cycle.

The STM32 demo can display an SFM3300 flow reading, but that reading is not
fed into the PID model. The demo firmware does not drive the pinch valve, and
there is no implemented real-time pressure acquisition or physical closed-loop
volume control. Those requirements, physical 30-cycle repeatability, and
two-branch independence remain unverified for the original system.
