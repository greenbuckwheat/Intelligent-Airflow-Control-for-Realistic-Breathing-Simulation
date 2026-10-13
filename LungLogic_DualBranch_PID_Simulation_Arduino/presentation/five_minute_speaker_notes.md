# Five-minute continuation: Python control simulation

English script for slides 11–17. Estimated duration: about 5–6 minutes at a measured speaking pace. The same text is embedded in slide notes.

## 11. Python control simulation (45 s)

I will now move from the hardware architecture to the Python control model. One branch represents a 250 millilitre share of the two-lung target. At 15 breaths per minute the period is four seconds. With an inspiration to expiration ratio of one to two, inspiration is 1.33 seconds and expiration is 2.67 seconds. A 100 hertz state machine selects atmosphere or vacuum, while the pinch valve sets the opening. The model then estimates flow, volume and a delayed sensor reading. The graph in the UI is playback of a precomputed simulation, not live hardware closed-loop data.

## 12. Model parameters I: timing and rebound (45 s)

These rows separate what we set from what we know about the real plant. Fifteen breaths per minute, a one-to-two ratio and 250 millilitres per branch are design targets, so they are verified by measuring actual cycles and delivered volume. One hundred hertz is a software setting; command timestamps will verify the real timing. The 250 millilitre capacity is configured, and needs a measured chamber volume. The 1.30 second fully open endpoint has one visual observation but is not a repeatable complete-path curve. The 0.57 second inspiration constant comes from an orifice/rebound candidate. We must measure and fit volume and flow throughout a full breath.

## 13. Model parameters II: valve and sensor (55 s)

The rest of the table describes the pathway from a valve command to a sensor reading. The expiration time constant is assumed because suction, sponge rebound and tubing restriction have not been separately measured. The pinch valve exponent approximates nonlinear flow versus opening, and needs a voltage sweep with checks for hysteresis. The command-to-lung and lung-to-sensor delays are assumed twenty-five milliseconds each; the sensor time constant is twenty milliseconds. We need synchronized command and flow traces to separate them. Noise, zero offset and eighty millisecond direction dead time also need measurement. Kp, Ki, Kd and Smith delay are controller settings, not physical lung parameters. Finally, compare thirty-cycle volume error and oscillation after the plant is calibrated.

## 14. Completed 30-cycle study (45 s)

This chart shows the minimum and maximum tidal volume over 30 simulated cycles, with the volume deadline guard enabled and internal true volume used for feedback. The requirement band starts at 237.5 millilitres, five percent below the 250 millilitre target. Twelve and fifteen breaths per minute pass. At sixteen, the minimum is 237.3, just 0.2 millilitres below the band; eighteen fails clearly. With only flat-flow PI and no deadline guard at fifteen, the range is about 215 to 220 millilitres. This is a simulated sensitivity test, not a measurement of achievable hardware performance.

## 15. Offline auto PI study (55 s)

The cited paper studies online PID auto-tuning through extremum seeking. Our implementation is a simpler offline, bounded two-sided perturbation search over proportional and integral gains; it is inspired by the paper, not an exact reproduction of its algorithm. It runs twenty iterations, scoring thirty simulated cycles at fifteen breaths per minute. Derivative gain remains zero. Without the volume guard, the tidal range improves from 212.8 to 217.8 millilitres to 215.2 to 220.2, but both miss the five percent requirement. With the guard, both reach about 243 to 246 millilitres, yet the pinch valve is fully open for almost all inspiration. Controller gains cannot make a saturated pneumatic path fill faster.

## 16. User/Developer modes and errors (40 s)

The interface now has a User mode and a Developer mode. User mode exposes breathing choices such as rate, target volume and inspiration to expiration ratio, and presents the simulated curves and status. Developer mode exposes the model's time constants, capacity, delays, PID gains, Smith predictor and volume guard. Invalid values such as impossible phase timing, fill time or flow target are checked and reported through a GUI error dialog. There is also a diagnostic warning when volume looks close to target only because the pinch valve is fully open. These checks prevent misleading simulation settings; they do not prove a hardware safety system. Displayed values are simulated, and the STM32 flow reading is not yet used by this model.

## 17. Next experiments and limitation (50 s)

The next step is to instrument the assembled pneumatic path, then run two calibration experiments: a fully open inspiration and expiration response, and a fixed-opening pinch-valve sweep. Both require synchronized flow, volume, pressure and command data. If a firmer sponge is installed, we will repeat the same fully open response under matched conditions and compare its curves. We will use these data to refit the model before repeating thirty-cycle and two-branch tests. The next slides specify the experiments; none of the measurements shown there have already been performed.

## 中文备讲要点

- 气路方向：大气通路对应吸气、肺体积增加；真空通路对应呼气、肺体积减少。
- 250 mL 是单支路目标；1.30 s 满充终点及 τ 等尚不是整机实测参数。
- 30 周期与自动 PI 结果均为离线仿真；自动调参尚未接入实时硬件或 GUI 操作按钮。
- 16 bpm 基准仿真最低 237.3 mL，比 5% 下限低 0.2 mL；不要声称硬件结论。
- 参数页的三列分别是当前值、物理意义、当前证据和验证实验；按行解释，不要只念数值。
- 需测完整气路流量-时间曲线，并尝试较硬海绵、验证回弹是否真的加快。
