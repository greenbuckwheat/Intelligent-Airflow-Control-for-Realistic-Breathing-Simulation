# LungLogic 双支路气动控制：建模、仿真与 STM32 实机

### 2026-10-09 加海绵后电压扫描模型更新

分析入口为 `analyze_sponge_voltage_sweep.py`，结果在 `output/sponge_voltage_sweep_2026-10-09/`：`segments.csv` 保留每段拟合和排除原因，`voltage_mapping.csv` 给出各电压的有效开度 a、相对导通 a^gamma、模型与实验时间常数，`analysis.json` 包含源文件 SHA256、数据质量、模型参数及夹闭电压敏感性。源实验程序和 CSV 保持 raw data。

处理采用 MCU 时间，剔除事件副本，按文件内阀门状态及实际电压共同分段。流量使用 `1.0471*(Qraw-0.0034)`，VAC 净抽气幅值使用 `max(0,-Qcal-0.931)`；ATM 不扣 VAC 漏气。按用户确认，固定电压吸排气开始前 PPV 已设置好，不加入 PPV 运动延迟，也不使用行程时间排除起始样本。4.2 V 文件中切回 0 V 的恢复单独识别，不用作 4.2 V 数据或全开基准。

部署模型为无附加纯延迟的一阶累计体积响应 `V=A*(1-exp(-t/tau))`，稳健拟合。采用 A>=500 mL、记录时长>=3tau、累计拟合 R²>=0.97 的统一筛选，并排除记录开始时已开启及同一阶段中途改电压的段。筛选阈值为本批经验规则，不能证明初态或压力相同。含额外残余流量项、含形状延迟的拟合仅作为敏感性诊断，不下发控制器。每个电压取合格段 tau 中位数；基准 tau0 采用本批 `0.csv`。拟合整条曲线时每档等权，在对数 tau 上拟合分方向 Ud、gamma。

|参数|吸气 ATM|呼气 VAC|
|---|---:|---:|
|全开 tau0 / s|2.694|1.697|
|节流平台末端 Ud / V|3.7768|4.0249|
|gamma|0.7828|0.9143|
|采用的停流点 Uc / V|4.3|4.3|

`a(U)=clip((Uc-U)/(Uc-Ud),0,1)`；相对导通为 `k=a^gamma`；有效时间常数为 `tau=tau0/k`。gamma 为每个方向的整条曲线参数，并非每档电压各设一个值；a 是经验有效坐标，不是几何阀位。模型幅度更新为吸气全开拟合中位数 2039.7 mL，不等于几何容量；250 mL 体积限值和目标体积不随之扩大。

4.18 V ATM 只有一段合格；4.18 V VAC 两段时长不足，未进入 gamma 拟合。4.2 V ATM 在恢复前只记录约 6.16 s，不能独立确定 tau；该处输出为模型外推。4.2 V VAC 三段仅一段通过覆盖筛选，结果应复测。按用户指定，模型统一将 4.3 V 及以上视为停流：两方向 a=0、导通系数=0。此为采用的建模设定，原始测量和残余流量记录保持不变。STOP 仍使用 5 V 并关闭方向阀。

此结果与此前单次 `new.csv` 的 0.78 s 吸气拟合不同：数据批次及拟合约定不同，0.78 s 来自允许形状延迟的拟合，不能直接与此次无附加延迟的 tau 比较。本次只更新 Hardware 模型及固件默认值；仿真 GUI 的假设模型不自动改成这批实测模型。协议仍为 v5；PPV 5 V 全程 2.9 s 的运动模型只用于闭环改变指令时，保持不变。

### 2026-10-09 流量与漏气校准更新（Hardware 协议 v5）

Hardware GUI 和 `firmware/single_lung_controller_stm32` 必须同时更新。当前配置统一采用 `Qcal = flow_sign × 1.0471 × (Qraw − 0.0034)`，单位 SLM；不区分吸呼气比例系数和 PPV 电压。系数来自 250 mL 针管积分实验与独立零偏实验，按用户指定直接采用针管标称体积。

仅当当前采样对应 VAC 开、ATM 关且 Qcal 为负时，肺净流量估计为 `min(0, Qcal + 0.931)`；其他状态保持 Qcal。0.931 SLM 是原 0.889 SLM 残余抽气量按新比例系数换算后的值。跨 PPV 电压及整个 VAC 阶段使用固定漏气值是本次采用的模型假设。补偿不会把微小抽气变成虚构的正向充气。双阀关闭时约 +0.024 SLM 的观察值不作固定扣除，切换尖峰也不采用固定屏蔽时间。

PID、Smith、体积积分和 Auto PID 流量评分均使用肺净流量估计；FF 与参考流量仍以肺净流量为目标，不重复叠加漏气。流量限值检查使用补偿前的校正传感器流量。图表同时显示校正传感器流量与肺净流量估计。

CSV 中 `flow_slm` 表示校正后的传感器流量，新增 `raw_flow_slm`、`net_flow_slm`、`sampled_valve_mask`。采样阀门状态与本轮刚下发的 `valve_mask` 分别记录，便于核对切换边界。Developer Mode 的零偏、统一比例系数及 VAC 漏气参数随模型参数一起锁定，解锁后可编辑；漏气设为 0 即关闭该项补偿。运行 JSON 保存全部参数。

这次更新未更改海绵实验的肺模型时间常数。随后实测 PPV 0→5 V 与 5→0 V 均为 2.9 s，运动匀速；已将双方向等效 4.2 V 行程时间设为 2.436 s，速度为 5/2.9≈1.724 V/s。当前字段与协议继续以 4.2 V 行程定义，因此不要直接将 2.9 填入该字段。Smith 仍默认关闭，可在 Developer Mode 启用。已完成离线测试和 ARM 源码编译检查，尚未烧录或进行实机闭环验证。

本项目把上传的倒立摆范例结构迁移到当前肺模拟器：Python 数字孪生用于参数设计和离线调参，NUCLEO-G474RE 执行 100 Hz 实时闭环，Python 上位机只负责配置、心跳、监控和保存数据。串口延迟不进入实时控制环。

## 1. 当前系统边界

- 无正压源。
- 吸气/膨胀：常闭大口径电磁阀接通大气，肺体依靠弹性恢复吸入空气。
- 呼气/收缩：另一只常闭电磁阀接通经机械调压的真空源。
- 左右两支路独立，各含 2 个方向阀、1 个 PPV40V 比例夹管阀和 1 个 SFM3300 双向流量计。
- 单侧目标 250 mL；对称运行总量 500 mL。
- 单侧 1.3 s 达到 250 mL 时，平均目标流量为

  \[
  Q^*=60\frac{0.250}{1.3}=11.54\ \mathrm{SLM},
  \]

  两侧同时膨胀的总平均流量约为 23.08 SLM。

当前初始配置采用 15 breaths/min、I:E=1:2，因此吸气时间为 1.333 s，给 1.3 s 的体积跟踪留出约 33 ms 的保持余量。

## 2. 当前硬件映射

| 元件 | 数量 | 在控制系统中的作用 |
|---|---:|---|
| Buschjost 8254200.9151.02400，2/2 NC，24 VDC，G1/2 | 4 | 左/右支路的大气与真空选路；按阀体箭头安装 |
| Thinkvast PPV40V | 2 | 0–5 V 控制夹管开度；当前资料定义 0 V 全开、5 V 全闭 |
| Sensirion SFM3300 | 2 | 双向流量反馈和体积积分 |
| TCA9548A | 1 | 隔离两个同为 I²C 地址 0x40 的 SFM3300 |
| PWM + RC 低通/电平转换 | 2 | 由 STM32 D5/D3 产生左/右两路 PWM，占空比经板外电路转换为 PPV40V 的 0–5 V 命令 |
| NUCLEO-G474RE | 1 | 100 Hz 状态机、PID、保护和 ST-LINK VCP 串口协议 |

闭环固件统一采用最终 PCB 分配：D11 左大气阀、D13 左真空阀、D10 右大气阀、D12 右真空阀、D5 左 PPV PWM、D3 右 PPV PWM、D8 急停输入；A3、A2 分别为左、右肺压力输入。A4/PC1（SDA）与 A5/PC0（SCL）连接 TCA9548A/PCA9548A Pa.Hub；这两个 G474RE 引脚不是硬件 I²C 配对，因此固件使用开漏软件 I²C。Pa.Hub 通道 0、1 分别接左、右 SFM3300。

24 V 线圈不能直接接 STM32。每个线圈必须使用额定值足够的逻辑级 MOSFET/驱动模块和续流保护；24 V 电源、PPV40V 控制电路与 STM32 需要共地。G474RE GPIO/PWM 高电平为 3.3 V，不能仅靠 RC 低通产生 5 V；PPV 输入前必须有已验证的 3.3 V PWM 到 0–5 V 转换级。机械真空调压器是一级保护，软件限位不能代替它。

PPV40V 的 0 V 是全开，因此系统上电时必须先把 D5/D3 对应的外部模拟命令置为约 5 V，再允许任何方向阀开启。四只方向阀为常闭，所以控制器失电时气路进入隔离态。

## 3. 建模方法

每个肺体先采用可由台架数据辨识的一阶灰箱模型：

\[
\dot V_i=
\begin{cases}
a_i^\gamma (V_{\max,i}-V_i)/\tau_{\mathrm{in},i}, & \text{接大气},\\
-a_i^\gamma V_i/\tau_{\mathrm{out},i}, & \text{接真空},\\
0, & \text{隔离},
\end{cases}
\]

\[
Q_i=0.06\dot V_i\quad[\mathrm{SLM}],
\qquad
V_i[k]=V_i[k-1]+\frac{1000}{60}Q_i[k]\Delta t.
\]

其中 `a=0` 表示夹管阀闭合，`a=1` 表示全开；对应 PPV40V 电压命令为 `u=5(1-a)` V。模型还包括管路传输延迟、流量计一阶动态、噪声以及左右肺参数不一致。

`Orifice.mlx` 以恢复时间构造 10 mm 管的归一化一阶曲线，给出 `tau_in≈0.57 s` 的候选值；其约 558 mL 容积为几何估算，并非大气吸气整条气路的实测容量或动态。右支路暂设为 0.60 s，用于检验左右独立闭环。`tau_out` 尚未由真空排气原始数据确认，当前 0.50/0.54 s 只能作为仿真初值。

## 4. 控制结构

状态机依次执行：真空预排气 → 全关死区 → 大气吸气 → 全关死区 → 真空排气。任何时刻，同一支路的大气阀和真空阀均不允许同时开启。正常换向时方向阀关闭，PPV 保留上一开度；新阶段的 PID 从该开度接续调节。停止、故障和运行结束时 PPV 关闭。

流量控制采用“模型前馈 + PID 修正”：

\[
a_i=\operatorname{sat}_{[0,1]}\{b_i+a_{ff,i}+K_p e_i+K_i\int e_i dt+K_d d_i\},
\quad e_i=Q_i^*-|Q_i|,\quad d_i=-\frac{d|Q_i|}{dt}.
\]

初值为 `Kp=0.022 opening/SLM`、`Ki=0.16 opening/(SLM·s)`、`Kd=0.0 opening·s/SLM`，带条件积分抗饱和。`b_i` 是换向时的无扰切换偏置：新阶段第一条 PPV 命令等于上一条命令，随后偏置按每秒最多 50 个归一化开度单位回到零；这是软件过渡设定，不代表实测阀门动作速度。D 项使用测量值导数而不是误差导数，以减少参考流量阶跃时的 derivative kick；默认 `Kd=0` 时行为与原 PI 控制一致。吸气期间左右支路独立控制；某侧达到 250 mL 后立即关闭该侧方向阀，另一侧可继续运行。呼气在估计容积小于 5 mL 时停止。

故障条件包括：连续 5 次 SFM3300 读取失败、绝对流量超限、估计容积超限、上位机心跳丢失超过 5 s、软件发现同侧两个方向阀同时请求。故障动作是关闭四个方向阀并把两个 PPV40V 命令置为 5 V。

## 5. 文件

- `lung_model.py`：左右肺模型、状态机、PID 和 JSON 参数。
- `lung_pid_simulation.py`：运行多周期数字孪生，输出 CSV 和四联图。
- `fit_lung_parameters.py`：从同学的体积或流量 CSV 拟合时间常数。
- `lung_hardware_monitor.py`：STM32 串口配置、心跳、记录和结果绘图。
- `firmware/lung_controller_stm32/lung_controller_stm32.ino`：双支路 NUCLEO-G474RE 实时固件。
- `firmware/single_lung_controller_stm32/single_lung_controller_stm32.ino`：单支路 NUCLEO-G474RE 实时固件，对应 `single_lung_hardware_gui.py`。
- `config.example.json`：仿真和实机共用的参数集。
- `tests/test_lunglogic.py`：目标时间、阀互锁和辨识算法测试。

## 6. 先运行仿真

```bash
cd lunglogic_pid
python -m pip install -r requirements.txt
python lung_pid_simulation.py --config config.example.json --output-dir output --show
```

基于当前模型默认值的五周期仿真，左右支路约在 1.15–1.25 s 达到各自 99% 目标，峰值约 252 mL。输出位于 `output/lung_simulation.csv` 和 `output/lung_simulation.png`。这只是控制软件的初始可运行点，不是最终模型验证结果。

## 7. 用初测数据替换模型初值

推荐每侧分别进行至少三类试验：大气全开吸气、真空全开排气、PPV40V 多个固定电压阶跃。CSV 至少要有单调递增的 `time_s`，再提供体积或流量列。

若已经由流量计记录 `left_flow_slm`：

```bash
python fit_lung_parameters.py test.csv \
  --time-column time_s --flow-column left_flow_slm --flow-sign 1 \
  --output output/left_inspiration_fit.json
```

若已有 `left_volume_ml`：

```bash
python fit_lung_parameters.py test.csv \
  --time-column time_s --volume-column left_volume_ml \
  --output output/left_inspiration_fit.json
```

将输出的 `tau_s` 写入 `config.example.json` 对应的 `tau_in_s` 或 `tau_out_s`。多电压阶跃用于拟合 `valve_exponent`；第一版可先保留 1.15，再根据“稳态流量—归一化开度”曲线回归。

流量积分前必须做零流量偏置校正。建议每个周期在四个方向阀全闭的死区测量偏置，并从随后的 SFM3300 读数中扣除；本初版固件尚未自动估计偏置，因此首次硬件试验应先检查零点漂移。

## 8. STM32 与上位机

在 Arduino IDE 中选择 **Nucleo-64 → Nucleo G474RE** 和 **OpenOCD STLink**，把 `firmware/lung_controller_stm32/lung_controller_stm32.ino` 上传到开发板。单肺闭环则上传 `firmware/single_lung_controller_stm32/single_lung_controller_stm32.ino`。固件使用 SFM3300 地址 0x40、软复位命令 0x2000、连续测量命令 0x1000、3 字节读取、CRC-8 初值 0x00/多项式 0x31，以及 `(raw-32768)/120` SLM 换算。采购型号若带不同后缀，必须先按该型号数据手册核对命令、比例系数和压力范围。

也可在项目目录编译验证：

```powershell
arduino-cli compile --fqbn STMicroelectronics:stm32:Nucleo_64:pnum=NUCLEO_G474RE firmware/lung_controller_stm32
arduino-cli compile --fqbn STMicroelectronics:stm32:Nucleo_64:pnum=NUCLEO_G474RE firmware/single_lung_controller_stm32
```

只打印将发送的协议，不连接硬件：

```bash
python lung_hardware_monitor.py --config config.example.json --dry-run
```

连接实机：

```bash
python lung_hardware_monitor.py --port COM5 --config config.example.json
```

Linux/macOS 端口示例为 `/dev/ttyACM0`。上位机先执行握手，再原子发送全部参数，收到确认后才 `START`；运行中每秒发送 `PING`，以 50 Hz 接收遥测并保存 CSV。

## 9. 首次实机验证顺序

1. 断开真空和 24 V，仅检查 NUCLEO-G474RE、TCA9548A/Pa.Hub 与两个 SFM3300；确认两个通道都有有效读数和正确 CRC。
2. 保持四个方向阀不通电，确认 D5/D3 经外部低通和电平转换后的左/右 PPV40V 控制电压均为约 5 V。
3. 单独点动每个 MOSFET 驱动，核对阀体流向和“常闭”行为；同侧两阀不能同时通电。
4. 只接大气，从低目标流量开始验证吸气阀门映射、SFM3300 正负号、体积积分和 250 mL 截止。
5. 接入机械调压后的真空，从较弱真空开始，核对 SFM3300 的绝对压力工作范围和呼气积分；不要仅依据泵铭牌真空度。
6. 分别拟合左右 `tau_in`、`tau_out`，更新 JSON 后重新仿真。
7. 在仿真中逐步增加参数不确定性和传感器偏置，再做 1–3 个低风险实机周期。

在没有独立压力测量/开关前，不应把“流量正常”视为真空压力一定安全。真空调压器、软管抗塌陷裕度和传感器允许绝对压力需要分别验证。

## 10. 与倒立摆范例的对应关系

| 倒立摆项目 | LungLogic 项目 |
|---|---|
| 非线性机械模型 | 左右肺一阶顺应性模型 |
| 双环 PID | 左右独立流量 PID + 体积终止逻辑 |
| 200 Hz STM32 控制 | 100 Hz NUCLEO-G474RE 控制 |
| PWM 电机输入 | 0–5 V PPV40V 命令 + 24 V 方向阀 |
| 编码器/电位器反馈 | 两个 SFM3300 流量反馈及积分体积 |
| Swing/PID 状态切换 | 预收缩/吸气/死区/呼气状态机 |
| Python 参数面板与记录 | Python 串口配置、心跳、CSV 与绘图 |

本项目已保持“仿真参数—固件参数—实验日志”一一对应，下一步只需要把初测 CSV 的真实列名和数据导入辨识脚本，再替换当前占位的排气参数。
### 2026-10-09：Hardware Smith 的 PPV 恒速运动模型（协议 v4）

Hardware GUI 与 `firmware/single_lung_controller_stm32` 须同时更新；旧 v3 固件不能接收新增参数。仿真 GUI 的算法及 FF 保持原样。

在 Developer Mode 解锁模型参数后可编辑 `Equivalent 0 to 4.2 V time`（关闭）和 `Equivalent 4.2 to 0 V time`（打开），单位秒。两项当前均为 2.436，来自双方向 5 V 行程各 2.9 s 的实测结果。0 表示未测量；任一为 0 时不允许开启 Hardware Smith。

设估计位置为 x（等效电压，不是阀位传感器读数），目标为 u。移动速度为 4.2/T V/s，剩余运动时间为 |u-x|T/4.2；当前等价于 |u-x|×0.58 s/V。每次更新从估计当前位置继续，限幅到目标，反向指令不从上一目标重算。模型跟踪在空闲、预处理和方向阀关闭期间继续，吸呼气切换不清零估计阀位。启动初始位置假设为 5 V 关闭端，因此上电后须让阀实际关闭到位；无位置反馈时不能自动确认初始位置。现有 STOP 保持 5 V，4.2–5 V 区域已包含在本次全程匀速测量范围内。

这是运动时间，不是纯死区延迟。Smith 使用两个独立体积状态：理想支路按目标电压计算流量，执行器支路按估计位置及已有双方向电压映射计算流量。反馈为 Qideal + gain × (Qmeasured - Qactuator_delayed)。残余 transport/sensor delay 只填写独立确认的管路/采样延迟，不叠加 PPV 全程运动时间。当前采样与上一控制区间的指令及方向阀状态比较，随后才生成新指令。

遥测与 CSV 新增 `estimated_ppv_v`、`ppv_remaining_s`；未校准时这两个字段不能作为真实阀位或运动时间证据。配置和运行日志保留两个全程时间。当前 reference 的 Smith 仍为关闭状态。此次完成软件模型与离线检查，未通过实机证明控制改善；已有大体积实验模型在小潮气量下的偏差、FF 和关阀尾流仍需独立验证。

