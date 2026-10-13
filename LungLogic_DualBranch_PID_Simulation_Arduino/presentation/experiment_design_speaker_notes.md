# Experiment-design addendum (slides 18–20)

These are planned measurements. The existing STM32 demo only reads the SFM3300 and displays simulated commands; it does not drive PPV or use physical feedback.

## 18. Two experiments: purpose and outputs

We propose exactly two calibration experiments. First, open the pinch valve fully and measure the entire atmosphere-inspiration and vacuum-expiration responses. This determines the actual fill endpoint, the two curve shapes, peak flow and residual volume. Any future sponge replacement is compared by repeating this same experiment under the same conditions. Second, hold the pinch valve at several fixed commands in each flow direction. This identifies the relationship between command and flow, including dead zone and hysteresis. Pressure and starting volume must be controlled or recorded because they also change the flow. These are proposed measurements, not results we already have.

## 19. Fully open response: setup, sequence and calculations

Once the whole atmosphere and vacuum path is assembled, a proposed Python experiment program will select test conditions and save time-stamped data. A microcontroller or DAQ should execute the timed valve sequence and acquire signals; the present STM32 demo cannot yet command the pinch valve or provide physical PID feedback. Measure flow with SFM3300, pressure at the source and lung, and lung volume independently, for example by calibrated displacement. First confirm zero flow and sign. Start from the same collapsed volume; open the ATM route and fully open PPV until full, then open VAC to empty. Repeat at the intended source pressures. Find time to 250 millilitres from measured volume, fit inspiration and expiration curves, and compare integrated flow against independent volume. Log command and actual electrical transitions to estimate delays. A new sponge can be assessed by repeating this protocol.

## 20. PPV sweep: setup, sequence and calculations

The same acquisition setup can run the second experiment. The proposed Python program schedules a sequence of fixed PPV commands, while the MCU or DAQ applies them and records time-stamped signals. For each command, start from a matched lung volume and source pressure, hold the ATM or VAC route fixed, then collect the initial flow response and a defined measurement window. Test both rising and falling commands, with repeats, to reveal dead zone and hysteresis. Fit the model exponent from flow versus opening under matched conditions; compare early and late flow because sponge volume changes during each run. Timestamp the valve voltage and the first flow response to estimate total command-to-flow delay. One flow sensor alone cannot separate valve motion, gas transport and sensor lag, so that decomposition needs electrical signals and an independent lung-end reference flow. The current GUI/firmware do not yet perform this experiment automatically.

## 中文备讲要点

- 只计划两个实验：全开吸气／呼气响应，固定 PPV 开度扫描。更换海绵后重复第一个实验，不另列对比实验。
- 完整回路接好后，Python 负责设定方案和保存数据；MCU／DAQ 负责有确定时间戳的阀门执行与传感器采样。当前演示固件还没有这些功能。
- 每个试验至少同步保存 Q(t)、独立 Vref(t)、气源及肺端 P(t)、PPV 命令／实测电压、ATM/VAC 命令及实际电信号。
- 仅有一个流量计只能估计总命令到读数延迟；要拆分阀门、气路与传感器延迟，需电信号和快速肺端参考流量或体积传感器。
