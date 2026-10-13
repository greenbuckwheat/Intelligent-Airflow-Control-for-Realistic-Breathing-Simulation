# 2026-10-07 单肺控制参考试跑

本次经验参考适用于当前管路、fake lung和负压源40–45 kPa负压幅度。
tau_in=2.53s、gamma_in=0.96、Ud_in=3.73V；tau_out=1.31s、gamma_out=1.69、Ud_out=3.98V；Uc=4.20V。
gamma基于分段有效开度，不再使用1-U/5；目标0V仍表示全开，STOP仍使用5V。
1500mL只是前馈/Smith模型幅度，原积分体积限制250mL保持独立。Smith关闭。
SFM3300按今日实验的A4/A5直连接线设置，SFM_CHANNEL=-1；若改回Pa.Hub需恢复通道0。

## 运行

1. 关闭实验GUI以释放串口；重新上传 firmware/single_lung_controller_stm32/single_lung_controller_stm32.ino。
   Arduino IDE选择Nucleo G474RE；新协议为 READY,LUNGLOGIC_SINGLE_G474,3，旧v2固件不能配合新版界面。
2. 在项目目录执行 `python single_lung_hardware_gui.py`。
3. 连接串口，确认v3握手；保持负压源当前40–45kPa设置。
4. 初始配置为1循环、6次/分钟、I:E=1:2、150mL目标，预抽气8s。User Mode 不再独立设置 SLM：按整个吸气阶段计算充气时间约3.333s，对应吸气参考约2.70SLM；参考值不是保证能达到的实测流量。
   预抽气现在使用0V全开；不会由程序自动识别阀门半开，也不保证含泄漏系统完全排空。
5. 保持Auto PID未勾选，按Start。结束时自动保存 output/single_lung_hardware_run_<run_id>.csv 和同名JSON配置记录。

参考配置在single_lung_hardware_reference.json，模拟界面的原配置未替换。
Kp=0.01、Ki=0.06、Kd=0、积分限幅0.5保留原配置值，是试跑初值，不是本次扫描辨识结果。
方向等待80ms保留原值，也不是新辨识结果。硬件急停、失联与流量保护保留。
没有自动刷写或运行实物；实际动作由用户上传后点击Start触发。

## 结果检查

重点比较吸/呼气参考与实测流量、目标体积是否达到、呼气结束残余积分体积、PPV目标电压及VAC弱抽气。
CSV的opening现表示方向模型的有效开度，不能再用5*(1-opening)解释电压；以command_v列为准。
若第一轮表现正常，再增加循环数检查累计漂移。模型暂未补偿泄漏，初始状态误差和弱开阀仍会影响控制。
