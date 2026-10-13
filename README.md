# Intelligent Airflow Control for Realistic Breathing Simulation

基于 STM32 和 Python 的肺模体气流控制项目，通过大气进气、真空排气和比例夹管阀调节，实现可重复的吸气与呼气过程。仓库当前以**单肺硬件闭环控制**为主，包含控制界面、STM32 固件、阀门测试工具、原始实验数据和模型辨识脚本。

项目仍处于台架实验与参数调整阶段。流量积分得到的容量是估计值，模型参数需要随肺袋、海绵、管路和夹管位置变化重新校准。

## 工作原理

- **吸气（ATM）**：大气方向阀打开，肺模体依靠弹性恢复吸入空气。
- **呼气（VAC）**：真空方向阀打开，经调压的真空源使肺模体收缩。
- **流量调节**：PPV40V 比例夹管阀改变气路导通程度，SFM3300 提供双向流量反馈。
- **控制分工**：STM32 执行实时控制；Python GUI 负责参数设置、监控、绘图和数据保存。

ATM 与 VAC 使用不同的电压—有效开度模型。软件中的 `opening` 是方向模型的归一化坐标，不是直接测得的机械开口比例；相同电压在吸气和呼气时也不代表相同流量。

## 主要硬件

以下数量对应当前单肺支路，具体接线以固件和实际 PCB 为准。

| 硬件 | 数量 | 用途 |
|---|---:|---|
| **ST NUCLEO-G474RE**（STM32G474） | 1 | 执行状态机、流量 PI/PID 和串口通信 |
| **Thinkvast PPV40V 比例夹管阀** | 1 | 通过外部 0–5 V 指令调节气路；0 V 为全开方向，电压升高逐渐夹紧 |
| **Norgren / Buschjost 8254200.9151.02400**，24 VDC、2/2 常闭电磁阀 | 2 | 分别连接大气 ATM 和真空 VAC |
| **Sensirion SFM3300 双向流量传感器** | 1 | 测量进出气流量，用于反馈和容量积分 |
| 真空源及机械调压器 | 1 套 | 提供可调真空排气条件 |
| 肺袋／弹性肺模体、海绵及连接管路 | 1 套 | 形成实际充排气负载；更换后需重新辨识 |
| MOSFET／电磁阀驱动及续流保护电路 | 1 套 | 驱动 24 V 电磁阀负载 |
| PWM 滤波及 0–5 V 电平转换电路 | 1 路 | 将 STM32 的 3.3 V PWM 转为 PPV 控制电压 |
| 配套电源、急停开关、USB 数据线 | 按台架配置 | 供电、急停输入及上位机通信 |

24 V 负载通过外部驱动电路连接，不能直接由 STM32 GPIO 驱动。PPV 输入电压需要通过滤波与电平转换实现，单靠 RC 滤波不能把 3.3 V PWM 提升到 5 V。

### 当前单肺固件引脚

| 功能 | NUCLEO 引脚 |
|---|---|
| ATM 电磁阀驱动 | D11 |
| VAC 电磁阀驱动 | D13 |
| PPV PWM 输出 | D5 |
| 急停输入，低电平有效 | D8 |
| SFM3300 软件 I²C SDA | A4 / PC1 |
| SFM3300 软件 I²C SCL | A5 / PC0 |

当前 SFM3300 直接连接 A4/A5，不要求使用 TCA9548A / Pa.Hub。实验工具预留了压力记录接口，但本批数据未提供有效压力测量，当前主控制不依赖压力闭环。

## 软件与控制功能

- STM32 控制频率 **100 Hz**，遥测频率 **50 Hz**，串口波特率 **115200**。
- 吸气使用模型前馈与 PI/PID 修正；呼气使用流量反馈与分阶段开度限制。
- 支持 Smith 预测补偿、PPV 运动估计及可选的逐次运行 PI 调参。
- 支持呼吸频率、吸呼比、目标容量和循环次数设置。
- 流量经过零偏与比例校准，VAC 方向另有漏气补偿。
- CSV 保存流量、容量估计、阀门状态和 PPV 指令；JSON 保存运行配置。

正常 `STOP` 和循环完成会进入容量恢复流程；急停、故障处理与正常停止不同。

## 仓库结构

```text
.
├── LungLogic_DualBranch_PID_Simulation_Arduino/
│   ├── single_lung_hardware_gui.py        # 单肺硬件控制界面
│   ├── single_lung_hardware_reference.json # 当前默认控制与模型参数
│   ├── single_lung_pid_gui.py             # 单肺模型及硬件 GUI 共用组件
│   ├── firmware/
│   │   ├── single_lung_controller_stm32/  # 主闭环固件
│   │   └── single_lung_calibration_stm32/ # 独立校准固件
│   ├── fit_hardware_directional_reference.py
│   ├── analyze_*.py                      # 零偏、容量及电压扫描分析
│   ├── output/                           # 硬件实验记录与校准结果
│   └── tests/                            # GUI、校准及 PPV 逻辑检查
└── 实验/
    ├── flow_control_exp/
    │   ├── flow_control_test_ui.py        # 手动 ATM/VAC/PPV 实验界面
    │   ├── flow_control_exp.ino           # 对应实验固件
    │   ├── data/                         # 原始测量 CSV
    │   └── analysis/                     # 实验分析结果
    ├── pwm exp.py                        # PPV 电气输出测试
    └── pwm exp/                          # 对应 PWM 实验固件
```

主目录沿用早期项目名称，但当前仓库重点是单肺硬件。`single_lung_pid_gui.py` 仍被硬件 GUI 导入，运行时需要保留。部分子目录文档属于历史记录，可能提及已移出仓库的双路或演示程序；当前入口以本页为准。

## 运行单肺硬件 GUI

在安装了 Python 的环境中，从仓库根目录执行：

```powershell
cd LungLogic_DualBranch_PID_Simulation_Arduino
python -m pip install -r requirements.txt
python single_lung_hardware_gui.py
```

在 Arduino IDE 中安装 STM32 开发板支持，选择 **Nucleo-64 / Nucleo G474RE**，打开并上传：

[`single_lung_controller_stm32.ino`](LungLogic_DualBranch_PID_Simulation_Arduino/firmware/single_lung_controller_stm32/single_lung_controller_stm32.ino)

随后在 GUI 中选择对应串口，连接并检查参数，再开始实验。硬件 GUI 与手动实验工具使用不同固件，切换工具时需使用对应版本。

不连接硬件、只检查配置生成的串口命令：

```powershell
python single_lung_hardware_gui.py --dry-run
```

## 校准与实验数据

原始电压扫描、流量积分校准及零偏数据位于 [`实验/flow_control_exp/data`](实验/flow_control_exp/data)。电压扫描通常以目标电压命名，分析时还需核对文件内电压、阀门状态和记录日期。

当前硬件参考使用 **2026-10-12 更换硬件后的实验**，ATM 与 VAC 分别拟合。2026-10-09 的旧硬件结果保留作对照，不直接混入新模型。模型交换容量与 250 mL 控制上限是两个不同参数。

重新生成当前方向模型拟合结果，需要额外安装 SciPy：

```powershell
# 在 LungLogic_DualBranch_PID_Simulation_Arduino 目录内
python -m pip install scipy
python fit_hardware_directional_reference.py
```

该脚本写入审计结果，不自动改写控制配置。拟合方法、源文件哈希和限制见 [`fit.json`](LungLogic_DualBranch_PID_Simulation_Arduino/output/directional_reference_2026-10-12/fit.json)。拟合结果是台架模型初值，不等于已完成闭环性能验证。

手动阀门实验的详细说明见 [`flow_control_exp/README.md`](实验/flow_control_exp/README.md)。

## 离线测试

```powershell
# 在 LungLogic_DualBranch_PID_Simulation_Arduino 目录内
python -m pip install pytest scipy
python -m pytest tests/test_single_lung_hardware_gui.py tests/test_single_lung_user_gui.py tests/test_calibration_experiments.py -q
```

离线测试验证软件逻辑和配置，不代替实机流量、阀门动作及容量测量验证。
