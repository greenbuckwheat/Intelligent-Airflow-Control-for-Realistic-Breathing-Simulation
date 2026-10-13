# Flow Control Experiment

这是一套独立实验工具，用来适配当前 single-lung hardware，并验证原控制代码依赖的实物行为。它不修改原有 `single_lung_hardware_gui.py`、原 STM32 控制固件或 `pwm exp.py`。

## 文件

- `flow_control_test_ui.py`：Python/PyQt5 测试界面，串口控制、实时流量曲线、事件记录和 CSV 保存。
- `flow_control_exp.ino`：Arduino STM32 实验固件，手动控制 ATM/VAC/PPV，读取 SFM3300 流量并回传遥测。

## 硬件适配

沿用原 single-lung hardware profile：

- Left ATM solenoid：`D11`
- Left VAC solenoid：`D13`
- Left PPV PWM：`D5`
- E-stop：`D8`，低有效
- SFM3300：直接连接software I2C `A4/A5`，不经过Pa.Hub

PPV 约定与原控制代码一致：`0 V` 为全开，电压升高逐渐夹紧，`5 V` 为关闭。界面显示的是目标电压，不保证实际模拟输出等于该值；仍需用万用表校准 PWM、滤波和放大电路。

实验固件上电以及GUI完成连接握手后，默认保持 ATM/VAC 关闭、PPV `0 V` 全开。`STOP`、急停和串口失联保护仍会把PPV设为 `5 V` 关闭。

## 运行

```powershell
cd C:\Users\13922\Desktop\capstone\实验\flow_control_exp
python -m pip install pyserial PyQt5 matplotlib
python flow_control_test_ui.py
```

先用 Arduino IDE 打开并上传 `flow_control_exp.ino`。完成实验后，如需运行原闭环控制界面，再重新上传原项目中的 `single_lung_controller_stm32.ino`。

## 串口协议

115200 baud，换行结束。

上位机发送：

- `HELLO`
- `PING`
- `STOP`
- `ATM,0` / `ATM,1`
- `VAC,0` / `VAC,1`
- `PPV,<mV>`，范围 `0..5000`
- `ZERO_FLOW`

固件返回：

- `READY,FLOW_EXP_G474,1`
- `ACK,...`
- `FAULT,...`
- `ERR,...`
- `TEL,ms,flow_slm,atm,vac,ppv_v,ppv_pwm_code,p_model_kpa,p_syringe_kpa,sensor_status,fault`

## 实验建议

1. 零流量检查：全部关闭后观察 SFM3300 零点，必要时点击“流量归零”。
2. 阀门方向检查：PPV 设为 `0 V`，分别打开 ATM 和 VAC，确认流量符号与原控制代码约定一致。
3. PPV sweep：固定 ATM 或 VAC，依次设置 `0..5 V`，记录稳定流量；用于验证 PPV 调节流量并拟合 gamma。
4. 阶跃延迟：在某个方向阀开启条件下记录 PPV 电压阶跃，比较命令事件时间和流量响应时间。
5. tau_in/tau_out：用固定 PPV 开度进行充气/抽气阶跃，记录流量衰减过程，拟合一阶时间常数。

“停止记录”只关闭 CSV 文件，不会改变阀门或 PPV 状态；“全部电磁阀关闭 / STOP”才会关闭 ATM/VAC，并将 PPV 设为 `5 V`。

## 压力记录

代码已经预留 `pressure_model_kpa` 和 `pressure_syringe_kpa` 两列。实验固件中 `PRESSURE_ENABLED` 默认为 `0`，因此压力输出为 `nan`。接入压力传感器后，根据传感器输出范围修改：

最终 PCB 分配为左肺压力 `A3`、右肺压力 `A2`。当前单肺实验把 `A3` 作为 model/左肺压力，把 `A2` 保留为第二测点；只有当 A2 的传感器实际安装在针筒侧时，CSV 的 `pressure_syringe_kpa` 才能按“针筒侧压力”解释。

- `PRESSURE_ENABLED`
- `PIN_PRESSURE_MODEL`
- `PIN_PRESSURE_SYRINGE`
- `PRESSURE_ZERO_V`
- `PRESSURE_KPA_PER_V`

加入压力后，可以在相近压差下比较流量，拟合 gamma 会比只看电压和流量更可靠。
