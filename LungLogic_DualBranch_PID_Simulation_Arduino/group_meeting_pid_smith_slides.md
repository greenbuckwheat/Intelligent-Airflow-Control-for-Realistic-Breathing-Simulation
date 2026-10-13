# LungLogic Single-Lung Flow Control

## Slide 1. 本次汇报重点

- 当前单肺控制代码中的 PID 用在哪里
- PID 控制的物理量是什么
- 执行器是什么
- Smith predictor 在系统中做什么
- PID 参数需要如何获得
- Smith predictor 参数需要如何校准

Speaker notes:
这次不重点讲完整 GUI 或机械结构，只讲控制闭环本身。当前单肺版本的控制目标是让肺模体按目标流量完成吸气和呼气，体积是通过流量积分得到的结果。

---

## Slide 2. 当前系统闭环结构

```text
Breathing settings
RR, I:E, Vtarget, Tfill
        |
        v
Reference flow Q_ref
        |
        v
PID controller
        |
        v
PPV40V opening command
        |
        v
Single-lung pneumatic plant
        |
        v
SFM3300 measured flow
        |
        +---- feedback to PID
```

- PID 闭环对象：flow rate
- PID 输出：normalized pinch valve opening, `0...1`
- 传感器反馈：SFM3300 bidirectional flow sensor
- 体积估计：由 measured flow 积分得到

Speaker notes:
这里的闭环不是直接控制压力，也不是直接控制体积。PID 每 10 ms 左右更新一次阀门开度，让测得流量跟踪目标流量。体积目标用于停止吸气或呼气阶段。

---

## Slide 3. PID 在哪里用到

代码位置：

- `single_lung_pid_gui.py`
- `PIDController.update()`
- `run_single_lung_simulation()`

核心控制律：

```text
e = Q_ref - |Q_feedback|

a = clip(
    a_ff + Kp * e + Ki * integral(e dt) + Kd * d,
    0,
    1
)
```

- `a` 是 PPV40V 开度
- `a_ff` 是模型前馈开度
- PID 修正模型误差、阀门误差和传感器误差
- 代码中有 integral limit 和 conditional integration，用于抗积分饱和

Speaker notes:
PID 不是单独从零开始控制，而是叠加在模型前馈上。前馈先估计一个大概阀门开度，PID 再根据实际流量误差做修正。

---

## Slide 4. PID 控制的是什么

PID 控制目标：

```text
Measured flow Q_measured tracks reference flow Q_ref
```

吸气目标流量：

```text
Q_in_ref = 0.06 * Vtarget / Tfill
```

默认例子：

```text
Vtarget = 250 mL
Tfill = 1.3 s
Q_in_ref = 11.54 SLM
```

呼气目标流量：

```text
Q_out_ref = -0.06 * Vtarget / usable_expiration_time
```

- 吸气时控制正向流量
- 呼气时控制负向流量的绝对值
- 体积不是 PID 直接变量，而是由流量积分得到

Speaker notes:
流量单位 SLM 是 standard litre per minute。代码里 `0.06` 来自 mL/s 到 SLM 的换算，因为 `1 mL/s = 0.06 SLM`。

---

## Slide 5. 执行器是什么

连续控制执行器：

- Thinkvast PPV40V proportional pinch valve
- PID 输出控制 PPV40V 开度
- 开度范围：`a = 0...1`

电压映射：

```text
U_cmd = 5 * (1 - a)
```

- `a = 1` -> `U_cmd = 0 V` -> fully open
- `a = 0` -> `U_cmd = 5 V` -> fully closed

方向选择执行器：

- Atmosphere solenoid valve
- Vacuum solenoid valve

```text
Inspiration: atmosphere valve open, vacuum valve closed
Expiration : atmosphere valve closed, vacuum valve open
Dead time  : both valves closed
```

Speaker notes:
需要区分两类执行器。电磁阀是开关量，只负责接大气还是接真空。真正被 PID 连续调节的是比例夹管阀 PPV40V。

---

## Slide 6. Smith Predictor 做什么

问题：

- 气路传输存在延迟
- SFM3300 和信号处理存在响应滞后
- PID 如果直接使用延迟反馈，容易过冲、振荡或反应变慢

Smith predictor 的作用：

```text
Use model to estimate current flow
Use delayed sensor measurement to correct model error
```

代码中的反馈估计：

```text
Q_feedback =
    Q_model_now
    + gain * (Q_measured_delayed - Q_model_delayed)
```

- `Q_model_now`：模型预测的当前流量
- `Q_model_delayed`：模型预测流量的延迟版本
- `Q_measured_delayed`：传感器实际测到的延迟流量
- `gain`：测量误差修正强度

Speaker notes:
Smith predictor 本身不是执行器，也不是 PID 的替代品。它是在 PID 前面提供一个更接近当前真实流量的反馈信号，让 PID 少受延迟影响。

---

## Slide 7. Smith Predictor 在闭环中的位置

不开 Smith predictor：

```text
PID feedback = measured flow
```

开启 Smith predictor：

```text
PID feedback = predicted current flow
```

完整链路：

```text
PPV opening command
        |
        v
Pneumatic model
        |
        +--> Q_model_now
        |
        +--> delay line --> Q_model_delayed

SFM3300 measurement --> Q_measured_delayed

Q_model_now + gain * error --> PID feedback
```

- Smith predictor 只改变反馈信号
- PID 控制律和执行器不变
- 模型不准时，Smith predictor 可能反而引入错误预测

Speaker notes:
这也是为什么 Smith predictor 一定要先校准模型参数。它对延迟系统很有用，但前提是模型能大致反映真实气动响应。

---

## Slide 8. 当前模型参数

肺模体与阀门模型：

```text
capacity_ml       = 250 mL
tau_in_s          = 0.57 s
tau_out_s         = 0.50 s
valve_exponent    = 1.15
transport_delay_s = 0.025 s
sensor_tau_s      = 0.020 s
sensor_noise_slm  = 0.10 SLM
flow_sign         = 1
```

PID 参数：

```text
Kp = 0.022
Ki = 0.16 or 0.21
Kd = 0.0
integral_limit = 2.0
```

Smith predictor 参数：

```text
enabled = false by default
delay_s = 0.025 s
correction_gain = 0.85...1.0
```

Speaker notes:
这些目前更像仿真和早期硬件调试初值，不应该在汇报里说成最终实测参数。迁移到真实系统时，需要用实验重新辨识。

---

## Slide 9. PID 需要哪些参数

PID 控制器本身需要：

- `Kp`：比例增益
- `Ki`：积分增益
- `Kd`：微分增益
- `integral_limit`：积分限幅
- `control_dt`：控制周期，当前约 `0.01 s`

前馈 + PID 还需要模型参数：

- `capacity_ml`
- `tau_in_s`
- `tau_out_s`
- `valve_exponent`

因为前馈开度来自：

```text
a_ff = (Q_ref / Q_max)^(1 / gamma)
```

其中：

```text
Q_max,in  = 0.06 * (Vmax - V) / tau_in
Q_max,out = 0.06 * V / tau_out
```

Speaker notes:
如果只讲 PID，参数就是 Kp Ki Kd。但在当前代码里 PID 和模型前馈是一起工作的，所以 tau 和 gamma 也会影响控制效果。

---

## Slide 10. PID 参数校准实验

实验 1：开环阶跃实验

- 固定 PPV 开度，例如 20%, 40%, 60%, 80%, 100%
- 分别测试吸气和呼气
- 记录 SFM3300 流量和积分体积
- 用于估计系统响应速度、最大流量和阀门非线性

实验 2：低风险闭环 PI 调参

- 先设 `Kd = 0`
- 从小 `Kp` 开始增加，直到响应足够快但不过冲明显
- 再增加 `Ki`，消除稳态误差
- 观察流量跟踪误差和体积误差

实验 3：多目标验证

- 不同 RR
- 不同 I:E ratio
- 不同 target volume
- 不同 target fill time

评价指标：

- rise time
- overshoot
- steady-state flow error
- tidal volume error
- cycle-to-cycle repeatability

Speaker notes:
建议先调 PI，不急着用 D。当前 `Kd = 0` 是合理的，因为流量信号有噪声，D 项容易放大噪声。

---

## Slide 11. Smith Predictor 需要哪些参数

Smith predictor 需要：

- `delay_s`
- `correction_gain`
- same plant model as feedforward:
  - `capacity_ml`
  - `tau_in_s`
  - `tau_out_s`
  - `valve_exponent`
  - `flow_sign`

最关键的是：

```text
delay_s = command change -> measured flow response delay
```

`correction_gain` 的意义：

- `0`：完全相信模型，不用测量误差修正
- `1`：完整加入测量误差修正
- 太大：可能放大噪声或模型误差
- 太小：延迟修正不明显

Speaker notes:
Smith predictor 的参数不是随便调出来的。它首先依赖真实系统延迟，其次依赖一阶模型能否预测吸气和呼气流量。

---

## Slide 12. Smith Predictor 校准实验

实验 1：延迟辨识

- 在固定初始体积下给 PPV 开度阶跃
- 同步记录：
  - command timestamp
  - valve command voltage
  - measured flow
- 测量从命令变化到流量开始明显变化的时间
- 得到 `transport_delay_s` / `smith_delay_s`

实验 2：模型延迟对比

- 用拟合好的模型生成 `Q_model_now`
- 将模型输出延迟 `delay_s`
- 与实测 `Q_measured` 对齐
- 检查 `Q_model_delayed` 是否能贴近实测曲线

实验 3：Smith on/off A-B test

- 相同 PID 参数
- 相同呼吸参数
- 分别关闭和开启 Smith predictor
- 比较：
  - flow overshoot
  - flow oscillation
  - time to target volume
  - tidal volume error
  - stability across cycles

Speaker notes:
Smith predictor 最有说服力的实验是 on/off 对比图。同一组参数下，开启后如果过冲减小、响应更稳，就说明补偿有效。

---

## Slide 13. 必做基础校准实验

1. SFM3300 零点校准

- 所有阀关闭
- 记录零流量偏置和噪声
- 后续积分体积前先扣除 offset

2. 流量方向校准

- 打开大气路径，确认吸气符号
- 打开真空路径，确认呼气符号
- 必要时修改 `flow_sign`

3. PPV40V 电压-开度-流量校准

- 输入多个电压
- 测稳态流量
- 确认 `0 V fully open, 5 V fully closed`

4. 吸气/呼气阶跃响应

- 拟合 `tau_in_s`
- 拟合 `tau_out_s`
- 拟合 `valve_exponent`

5. 闭环安全验证

- 急停
- 超流量保护
- 串口 watchdog
- 阀门互锁

Speaker notes:
这些实验是把仿真参数迁移到硬件的基础。没有零点校准和方向校准，体积积分会漂移，PID 甚至可能朝错误方向调节。

---

## Slide 14. 汇报总结

- PID 用在流量闭环中，控制目标是 `Q_measured -> Q_ref`
- PID 输出 PPV40V 比例夹管阀开度
- 电磁阀只负责大气/真空路径切换
- Smith predictor 用模型预测当前流量，补偿气路和传感器延迟
- PID 校准重点是 `Kp`, `Ki`, `Kd`, `integral_limit`
- Smith predictor 校准重点是 `delay_s`, `correction_gain` 和一阶肺模型参数
- 实机迁移前必须完成传感器零点、方向、阀门映射、阶跃响应和闭环 A-B 对比实验

Speaker notes:
最终可以把系统概括为：state machine chooses pneumatic direction, PID modulates proportional valve opening, and Smith predictor improves feedback timing when delay becomes significant.
