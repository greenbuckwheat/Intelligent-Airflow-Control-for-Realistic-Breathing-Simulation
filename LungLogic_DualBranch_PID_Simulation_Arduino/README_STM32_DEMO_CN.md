# Simulation + STM32 桌面演示

纯模拟入口：`single_lung_pid_gui.py`。STM32 演示入口：`single_lung_stm32_demo_gui.py`。固件：`firmware/single_lung_stm32_demo/single_lung_stm32_demo.ino`。

两个入口启动时默认显示**用户模式**，左上角可切换到**开发者模式**。开发者模式保留原模型、PID、Smith predictor、参数面板、动态模型、模拟曲线、独立 Live Monitor 和播放行为。点击 **Modeling & Control Guide** 可查看物理参数映射、延迟路径、控制公式、前馈及 Smith predictor 的启用条件与校准顺序。两个入口共用 `single_lung_config.gui.json`；应用设置会保存配置。

用户模式可设置呼吸频率（bpm）、目标潮气量（mL）、吸呼比和吸气／呼气目标流量（SLM）。点击“Apply Settings”后再启动；界面并排显示动态肺模型和模拟实时流量曲线，还显示当前模拟容积及最后一周期的模拟潮气量。界面标签统一使用英文；窗口缩小时，动态模型保持固定大小并提供滚动条。目标吸气流量必须能在吸气时间内完成目标充气；呼气流量也必须足以在呼气时间内排空至模型阈值，否则设置会被拒绝。模型的响应速度、肺容量和阀门限幅也会影响实际达到的容积，因此应查看显示的模拟结果。

开发者模式的 **Live Curves** 默认随播放显示约三个呼吸周期，便于查看 30 周期仿真中的单次呼吸细节；勾选 **Show full run in Live Curves** 可查看完整时间轴。启用 **volume deadline guard** 时，设定的平坦目标流量只是请求值；若模型判断需要更大开度才能赶上容积目标，PPV 会进一步打开，实际流量可能高于虚线。界面会显示吸气期间 PPV 全开的比例和峰值流量；当 PPV 已持续全开时，调 PID 增益不能消除该流量峰值。

STM32 演示入口在用户模式中另显示 SFM3300 的**真实流量曲线**和连接状态，可直接选择串口并连接。真实流量和模拟流量分开标注；真实读数不进入 PID，STM32 固件不控制 PPV，仍是桌面演示。

电脑运行 simulation，串口只发送当前画面中的方向阀开关。STM32 读取 SFM3300-AW 并回报 GPIO 状态，不运行 PID；真实流量不会改变模拟肺容积或模拟反馈。此版本是桌面 LED / 传感器接口演示，不是实体肺闭环控制固件。

## 启动

在项目目录运行：

```powershell
python -m pip install -r requirements.txt
python single_lung_stm32_demo_gui.py
```

1. 在 Arduino IDE 的 Boards Manager 安装 **STM32 MCU based boards**（已用本机 2.12.0 编译）。
2. 打开新 `.ino`；选择 **Nucleo-64 → Nucleo G474RE**，通过 ST-LINK 上传。使用默认硬件 Serial / ST-LINK Virtual COM Port；无需启用 USB CDC。
3. 先按下文核实 `PCB_PIN_PROFILE`，以及 `PAHUB_CHANNEL`。上传后关闭 Serial Monitor，避免占用串口。
4. 在用户模式或开发者模式刷新串口列表，选择 ST-LINK COM 口并连接。连接使用 115200、8N1，匹配专用固件协议后才发送 LED 状态。
5. 应用呼吸设置并启动播放。用户模式同时显示动态模型、模拟流量和真实流量；开发者模式的 **Live Flow Meter (Hardware)** 标签页显示真实 SLM、原始计数、传感器状态、MCU GPIO 回报及最近 15 秒真实曲线。

无 STM32 时也可照常播放 simulation。连上 STM32 后，流量计采集始终运行，包括暂停 simulation 时。没有流量计时显示 I2C / 无读数，LED 演示仍可工作。

## 仅用现有资料比较充气模型假设

单肺 GUI 仅使用 **Assumed finite fill** 充气响应。吸气走大气、呼气走真空。模型把 `Lung Vmax` 当成容积限位，假设从 0 mL 开始、大气气路且 PPV 全开时，在 **Full-open reach time** 到达该限位。默认示例时间为 1.30 s；`Orifice.mlx` 中的 1.30 s 是设计时限，**并非完整电磁阀、PPV、流量计和真空系统的实测响应**。`Tau_in` 决定这个终点之间的曲线形状；只知道终点无法唯一确定真实流量曲线。仿真正流量表示模型容积增加，真实 SFM3300 的极性必须实测校准。旧配置中的 `inspiration_model` 字段在加载时移除；旧值为原有两种模式时，都按有限充气假设计算。

在项目目录运行下列命令，比较三个不同的曲线形状。脚本不会连接硬件，也不会更改 GUI 保存的配置：

```powershell
python single_lung_assumption_study.py
```

图表和逐情景指标保存在 `output/assumption_study/fill_curve_sensitivity.png`、`output/assumption_study/fill_curve_sensitivity.csv`。上排表示无阀门及传感器延迟的全开直连假设；下排保持当前保存的 PID、前馈、Smith 和延迟设置，显示最后一次吸气的模拟结果。三条假设曲线均约在 1.30 s 到达同一物理限位，但控制器的开度与潮气量可以不同。所得数值用于识别模型不确定性，不能作为实体系统的性能保证。

`Orifice.mlx` 以外形长宽高之差估算 558 mL，并把 250 mL 作为其中 44.8% 的目标；文件中的 1.30 s 是设计时限，图中的完整曲线根据恢复时间与一阶假设生成，不是逐时刻测得的容积曲线。这里不把该 558 mL 外形估算直接覆盖当前 250 mL 单肺限位，也不把旧模型的 `tau_in≈0.57 s` 当成已识别的唯一实体参数。

## LED 对应关系

默认选择左肺分支，右分支输出保持 LOW。默认遵循 simulation 的 `valve_mask`，不是只判断呼吸阶段名称：

| 画面里的命令 | 左 ATM / LED2 | 左 VAC / LED3 |
|---|---|---|
| ATM 开（mask=1） | 亮 | 灭 |
| VAC 开（mask=2） | 灭 | 亮 |
| 两阀关闭（mask=0） | 灭 | 灭 |

吸气用 ATM（mask=1），呼气及有实际排空动作的预处理用 VAC（mask=2）；达到目标容积或排空阈值后，即使阶段名称未切换，也可能关闭阀门。LED 跟随仿真阀门命令。输入延迟模型不改变此处的命令来源。

本项目按“大气吸气、真空呼气”的气路定义显示 LED。**Swap PCB ATM / VAC (wiring check only)** 仅用于检查接线标签是否颠倒；勾选会交换发送到 PCB 的两位输出。GUI 的 Requested PCB 显示发送命令，MCU GPIO 显示固件实际写入的输出；后者不代表检测到 LED 发光或真实阀门位置。

Pause / Stop / Reset / Save and Apply / 播放结束 / 关闭窗口都会关闭方向阀输出。断开连接发送 STOP；MCU 超过 750 ms 没收到有效 LED 命令也会关闭输出。GUI 每约 100 ms 重发当前状态，1 秒无有效遥测后断开。播放速度沿用原程序：同步的是当前显示帧，受 Qt 绘图和串口延迟影响，不保证硬实时或高倍速下每个短暂阶段都可见。

## 最终 PCB 引脚分配

演示固件已按最终接线固定使用 D11 左 ATM、D13 左 VAC，不再提供旧的 `PCB_PIN_PROFILE` 选择。完整系统分配如下：

| 信号 | 引脚 |
|---|---|
| 左 ATM / 演示 LED | D11 |
| 左 VAC / 演示 LED | D13 |
| 右 ATM | D10 |
| 右 VAC | D12 |
| 左 PPV PWM | D5 |
| 右 PPV PWM | D3 |
| 急停输入 | D8 |
| 左/右肺压力 | A3 / A2 |
| Pa.Hub SDA/SCL | A4 / A5 |

本固件不配置或驱动 PPV 引脚，PPV 调制仅在 simulation 中演示。截图 CN5 的 PPV 和 GND 位置也与标准 Nucleo 排针不匹配，需要核对 PCB 符号与实际 footprint 的编号，不能直接据此确认兼容。软件看门狗仅处理通信中断，物理急停仍通过切断 `+24V_VALVE_SW` 工作。

LED2/3 位于 MOSFET 负载端，不是独立 MCU LED 引脚。**只演示 LED 时不接实际电磁阀、PPV 负载**；否则相同输出也会驱动接入的硬件。按图给 PCB 提供 24 V、打开阀门电源/急停回路，使 `+24V_VALVE_SW` 有电，否则 MCU 回报 ON 也不会亮灯。MCU 从 USB 供电，PCB 按设计共地。

## 流量计接线与参数

采用 SFM3300-AW，空气 / N₂ 默认换算：`SLM = (raw - 32768) / 120`，7 位 I²C 地址 `0x40`。正负号保留传感器安装方向，独立于 simulation 的流量符号。

* PCB 的 A4 = PC1（SDA），A5 = PC0（SCL）。这对 G474RE 引脚不是硬件 I²C 配对，因此固件实现开漏软件 I²C，保留 PCB 走线；时钟拉伸等待有超时。
* 此左肺演示固件遵循 `single_lung_stm32_demo_new.ino`，使用一只 SFM3300 直接连接 A4/A5，不经过 Pa.Hub。正式单肺/双肺控制固件仍通过 Pa.Hub 选择通道；两种接法不能混用。
* **SFM3300-AW 要求 5 V ±5% 供电，而截图 U24 第 2 脚标为 +3V3。不能把该 3.3 V 当作传感器 5 V 电源。** 使用正确的 5 V 传感器供电，不能把 5 V 倒灌进 U24 的 +3V3。Pa.Hub 的标准供电也为 5 V，使用时检查实际适配线和上拉连接；MCU 侧 SDA/SCL 保持图中的 3.3 V 上拉，并共地。
* 启动/故障重试发送软复位 `0x2000`，等待 100 ms，发送连续测量 `0x1000`，再等待 100 ms；丢弃第一次读取。每次读取两个数据字节与一个 CRC 字节，CRC 初值 `0x00`、多项式 `0x31`。
* 约 50 Hz 遥测，GUI 约 5 Hz 刷新真实流量曲线。I2C 或 CRC 错误时发送 `nan` 与错误状态，约 1 秒后复位重试；不会用旧流量冒充新读数。

核对依据：[Sensirion SFM3300 数据表](https://sensirion.com/resource/datasheet/sfm3300)、[SFM3xxx I²C 协议](https://sensirion.com/media/documents/BE7405C4/62D13098/Sensirion_I2C_Functional_Description_SFM3xxx.pdf)、[CRC 说明（含 BEEF → 13 示例）](https://storage.googleapis.com/com-sensirion-development/documents/1E3CD1FF/6165AFE4/Sensirion_GF_AN_SFM-04_CRC_Checksum_D1.pdf)、[M5Stack Pa.Hub](https://docs.m5stack.com/en/unit/pahub)。G474RE 引脚功能另外核对了本机 STM32 core 2.12.0 的 `variant_NUCLEO_G474RE.h` 和 `PeripheralPins.c`。

## 串口协议 v1

ASCII，每行以 LF 结束，支持接收 CRLF。演示协议与 G474 闭环控制协议分开，不能混用。

```text
PC  -> HELLO
MCU -> READY,LUNGLOGIC_DEMO_G474,1
MCU -> INFO,...当前引脚配置及 Pa.Hub 通道...
PC  -> LED,0     # 两阀关闭
PC  -> LED,1     # 左 ATM 开
PC  -> LED,2     # 左 VAC 开
PC  -> STOP
MCU -> FLOW,1234,1.000,OK,1,0,32888
```

FLOW 字段依次为：`FLOW,MCU毫秒,真实SLM,传感器状态,实际输出mask,看门狗超时标志,原始16位计数`。传感器状态为 OK / STARTING / I2C / CRC。无效命令（包括双阀同时开 LED,3）关闭输出并返回 ERR；看门狗由有效 LED 命令刷新。

## 验证

```powershell
python -m unittest discover -s tests -p test_stm32_demo.py -v
arduino-cli compile --fqbn STMicroelectronics:stm32:Nucleo_64:pnum=NUCLEO_G474RE --build-path output/stm32_demo_build firmware/single_lung_stm32_demo
```

测试覆盖分段串口握手、实际读数解析、模拟阀门同步与对调、暂停/停止/重置/结束、心跳、错误数据及遥测超时。软件验证不能替代实板验收：核实脚位及供电后，依次检查 ATM/VAC 灯、暂停灭灯、拔线后熄灭以及真实流量计吹气/反向读数。当前未执行实板烧录或流量计联调。
