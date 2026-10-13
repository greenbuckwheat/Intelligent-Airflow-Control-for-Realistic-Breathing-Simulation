# LungLogic UI 医疗组汇报辅导

建议总时长：6–7 分钟。主线只有一句话：

> The UI makes the requested breath visible and testable, but the current 15 bpm result is a marginal simulation pass, not yet a verified hardware capability.

## 每页怎么讲

### Slide 1 — Title（15 秒）

> Today I will focus on the LungLogic interface: what a user can do, what evidence the simulation provides, and which requirements still need agreement before hardware validation.

中文提醒：开场不要先讲 PID、Smith predictor 或公式。先告诉医疗组，这个 UI 能帮助他们“设定呼吸、观察响应、判断是否达标”。

### Slide 2 — Objective and requirements（45 秒）

> Our objective is to convert a breathing target into a visible and repeatable test. The system target is 500 millilitres across two branches. This prototype represents one 250 millilitre branch. The nominal rate is 15 breaths per minute with an I:E ratio of one to two, and the proposed acceptance check is 30 breaths within plus or minus five percent.

必须强调：250 mL 单支路仿真通过，不等于 500 mL 双支路系统通过。

### Slide 3 — UI transition（5 秒）

> The interface has two layers: a simple user workflow and a technical diagnostic workflow.

### Slide 4 — User Mode（50 秒）

按图上的 1–5 顺序讲：

> First, the user enters rate, volume, I:E ratio, target flow and cycle count. Applying the settings recalculates the simulation. The centre shows the active pneumatic route and lung state. The graph compares simulated measured flow with the requested setpoint. The left panel reports the current frame and the last-breath volume.

最后补一句：

> This is playback of a precomputed simulation; it is not live hardware feedback.

### Slide 5 — Developer Mode（50 秒）

> Developer Mode explains why a case passes or fails. It exposes the PID settings, model feedforward, Smith prediction, delays and the simulation-only volume guard. The plots align volume, flow, valve command and state timing. This prevents us from calling a volume result successful when it was achieved only by saturation or a large flow pulse.

面向医疗组不要逐项解释所有参数。只需要说明：User Mode 看结果，Developer Mode 查原因和做校准。

### Slide 6 — Current status transition（5 秒）

> I will now separate completed software from physical-system evidence.

### Slide 7 — Implemented versus unverified（40 秒）

> The UI workflow, checks, plots, configuration and repeated simulation are implemented. The physical closed loop is not. The current STM32 demo does not yet drive the pinch valve with this controller, and its flow reading is not yet fed into this PID. Hardware repeatability and independent two-branch operation remain to be tested.

这页最重要的词是 **not yet demonstrated**，不要说“没有做”或“失败”。

### Slide 8 — 30-cycle result（55 秒）

> Under the current assumed plant, 12 and 15 breaths per minute stay within the plus-or-minus-five-percent band for 30 simulated breaths. Sixteen is a boundary case and 18 fails. However, the 15 bpm pass uses full pinch-valve opening for almost all inspiration. It therefore has almost no control margin.

不要只说 “15 bpm passes”。完整说法应为：

> Fifteen bpm passes the simulation tolerance, but it is a marginal pass with near-continuous saturation.

### Slide 9 — Physical limit and question for 4DMedical（70 秒）

> At 15 bpm, the full cycle is four seconds. With a one-to-two I:E ratio, inspiration is 1.333 seconds. After the switching dead time and assumed input delay, only about 1.23 seconds remains for pneumatic response. Our current model assumes 1.30 seconds to reach full volume from empty. This is why the controller saturates. Once the valve is fully open, changing PID gains cannot create more flow.

接着直接向 4DMedical 提问：

> We need your guidance on the priority of the constraints. Must 15 bpm, 250 millilitres and I:E one-to-two all be held simultaneously? Is plus or minus five percent required for every breath, and is a high early-flow pulse acceptable? If the path saturates, should the design change timing, target volume or pneumatic capacity?

18 bpm 的一句话：

> Under the current assumed plant, 18 bpm reaches only about 227 to 231 millilitres, so PID retuning alone cannot solve it.

### Slide 10 — Future transition（5 秒）

> The next step is to replace model assumptions with measurements.

### Slide 11 — Future work（45 秒）

> First, measure the complete atmosphere and vacuum response and perform a fixed-opening pinch-valve sweep, with synchronized flow, independent volume, pressure and command data. Second, close the real hardware loop. Third, run the agreed 30-cycle acceptance test and then extend it to two independent branches.

结束句：

> Our next milestone is not another favourable simulation trace; it is a measured 30-cycle result against a requirement agreed with 4DMedical.

## 最容易被问的五个问题

### 1. “Does the UI control the hardware now?”

> Not yet. It currently computes and plays back a simulation. The STM32 flow-sensor demo exists, but the measured flow is not yet closing this PID loop and the PPV is not driven by it.

### 2. “Have you achieved 15 bpm?”

> In simulation, the 30-cycle tidal volume is within plus or minus five percent. However, the valve is almost continuously fully open, and the installed pneumatic path has not been calibrated. We therefore call it a marginal simulated pass, not a verified hardware achievement.

### 3. “Why does the flow exceed the flat setpoint?”

> The simulation-only volume deadline guard prioritizes reaching the target volume before inspiration ends. It can request more flow than the flat setpoint, which creates the high early-flow pulse.

### 4. “Can better PID tuning make 18 bpm pass?”

> Not when the valve is already fully open. At saturation, the limiting factor is available pneumatic flow and inspiration time, not controller gain.

### 5. “What do you need from 4DMedical?”

> We need agreement on which requirements are fixed, the per-breath acceptance tolerance, the acceptable flow waveform and whether changing timing or pneumatic capacity is in scope.

## 现场用词边界

推荐说法：

- simulated result / assumed plant / model prediction
- marginal pass / no meaningful control margin
- not yet demonstrated on hardware
- proposed acceptance criterion

避免说法：

- “The system achieves 15 bpm.”
- “The sensor data proves the PID works.”
- “The 1.30 second fill time is the physical limit.”
- “Auto-tuning will solve the flow problem.”

1.30 s 是当前模型采用的观测/假设值；只有完整安装后的同步测量才能确认真实物理上限。
