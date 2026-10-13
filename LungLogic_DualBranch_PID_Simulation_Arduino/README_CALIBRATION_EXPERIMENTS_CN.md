# 独立的两个标定实验

运行 `python lung_calibration_experiments.py`，不需要启动原来的 GUI。
依赖沿用 `requirements.txt`；Tkinter 通常随 Windows Python 安装。

本项目的 STM32 固件对应关系：

| 用途 | Python 程序 | NUCLEO-G474RE 固件 |
|---|---|---|
| 单肺实体闭环 | `single_lung_hardware_gui.py` | `firmware/single_lung_controller_stm32/single_lung_controller_stm32.ino` |
| 双肺实体闭环 | `lung_hardware_monitor.py` | `firmware/lung_controller_stm32/lung_controller_stm32.ino` |
| 人工阀门标定/桌面演示 | `lung_calibration_experiments.py` / `single_lung_stm32_demo_gui.py` | `firmware/single_lung_stm32_demo/single_lung_stm32_demo.ino` |

三种固件使用不同的 `READY` 标识，Python 端会拒绝不匹配的固件。

## 当前支持范围

- Synthetic：生成明确标注的假数据，检查工作流程，不连接硬件。
- STM32 demo (manual valves)：使用现有 STM32 演示固件和 SFM3300 采集流量。
- 阀门全部人工操作。程序只发送 HELLO（握手并关闭演示输出）和 STOP；不发送方向阀或 PPV 开启命令，也不能自动关闭人工开启的气路。
- 与现有 GUI 不共享串口；连接前关闭原 GUI 和 Serial Monitor。
- 如果阀门只能由当前 PCB 的 MCU 输出控制，现有固件不能通过本程序完成自动实验；需要新增标定固件。不要把界面上的命令开度当作已发送或已实测的阀位。

## 操作

1. 先用 Synthetic 试运行。选择数据源后点击 Connect。
2. 两条气路关闭，确认没有流量，再点击 Zero。程序采集 2 秒零偏。
3. 选择 full-open 或 ppv-sweep，填写每次持续时间、重复次数和实验条件。
4. 点击 Create experiment plan。full-open 按 ATM/VAC 交替重复；ppv-sweep 分方向、递增/递减命令扫描。
5. 每次点击 Prepare manually & record next trial 前，恢复相同的初始体积和源压力。ATM 试验从收缩状态开始，VAC 试验从匹配的充气状态开始。人工设置 PPV 和路径，关闭另一路，确认后记录。
6. 到达记录时间后程序只停止记录、提示并响铃。立即人工关闭气路；恢复初始条件后再测下一点。固定的 5 秒默认值只是记录时长，不代表肺已经充满或排空。
7. Finish & analyse 生成报告。允许提前结束计划。Stop 中止的试验保留原始数据，但从分析中排除；中止后可继续下一点，重新测该点需建立新计划。

## 数据与分析

每次运行保存到 `output/calibration_experiments/独立时间目录/`：

- `session.json`：数据来源、零偏、符号、实验计划和条件备注。
- `measurements.csv`：MCU 时间、接收时间、原始/修正流量、有符号的积分体积变化、命令开度及重复/扫描方向。
- `analysis.json`：峰值、积分体积变化、0.1–0.3 秒平均流量、全开指数拟合和限制说明。
- `response_curves.png`、`ppv_sweep.png`：响应和命令—流量散点图。

硬件模式的 `volume_ref_ml`、压力及实测电压列留空。后续有同步独立测量时，可按 MCU 时间匹配后填入参考体积列，再执行：

```powershell
python lung_calibration_experiments.py --analyse output/calibration_experiments/运行目录
```

参考体积列必须为每条记录提供匹配的数值，不可拿同一份流量积分充当独立参考。
SLM 积分是标准状态气体体积变化，不自动等同于实际几何体积；未校验时只能作初步估计。
积分不钳位、不强制清零，不掩盖零偏和残余误差。

人工操作的记录起点不是电气切换起点，所以本版不识别命令到流量延迟。
全开一阶拟合是候选模型，检查 RMSE、R²及曲线后再决定是否采用。
扫描结果必须在初始体积和压力匹配时才可解释；本版不会自动拟合 γ 或覆盖已有模型参数。
它也不采集独立压力/电压、不实现自动闭环或硬件安全关阀。
