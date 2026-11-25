"""Render the audited sweep results; no controller configuration changes."""
from pathlib import Path
import json
import pandas as pd

OUT=Path(__file__).resolve().parent/'analysis/sweep_2026-10-14'
fit=json.loads((OUT/'fit.json').read_text())
s=pd.read_csv(OUT/'voltage_summary.csv')
r=pd.read_csv(OUT/'segments.csv')
g=r[r.accepted & (r.voltage<=4.15)]
atm=g[g.direction.eq('ATM')]
vac=g[g.direction.eq('VAC')]
lines=['# 2026-10-14 PPV 开度实验分析', '',
       '本报告只分析用户指定的 22 个 CSV，未修改硬件控制器、固件或参考配置。', '',
       '## 结论', '',
       '本次实测全开有效时间常数：ATM **0.585 s**、VAC **0.255 s**。高电压敏感区的响应显著慢于现用模型。',
       '以完整吸气交换量估算，传感器读数尺度约 **306 mL**；沿用既有独立流量增益 1.0471 后约 **320 mL**。',
       '这是该试验压力/初始状态下的可交换标准气量，不是实测几何容积，也不保证任意呼吸频率下每周期都能交换这么多。', '',
       '## 数据筛选与计算定义', '',
       '- `0.csv` 实际为 5 V、ESTOP 的旧协议记录，整体排除。真正的 0 V 数据是 `0_1.csv`。',
       '- 其余 21 个文件均有 v2 字段；有效遥测中未见传感器/故障异常。用采样时刻、采样时阀状态和实际记录的目标电压分段。',
       '- 排除事件行，按采样时间去重；所有记录中的归零偏置均为 0。原始文件保持不变，SHA256 在 fit.json。',
       '- 切阀附近最大采样间隔为 68–84 ms。积分用实际采样时间差；命令目标电压不代表实测端子电压。',
       '- 共 192 段候选呼吸相位，171 段用于统计。剔除未知起点、记录中途结束、很小交换量、覆盖不足 3τ、动态累计曲线 R²<0.95 等片段；逐段原因见 segments.csv。',
       '- 累计流量模型：`V(t)=A*(1-exp(-t/tau))+b*t*1000/60`。ATM 主拟合 b=0；VAC 同时拟合持续尾流 b，而非直接套用旧的 0.814 SLM 扣除。',
       '- 主结果采用无额外延迟模型以与现用模型结构相近；不是机械执行器固有时间常数。附加延迟模型为诊断：0 V 时 ATM τ约0.429 s、VAC τ约0.200 s，说明起始瞬态会影响τ定义。',
       '- 所有体积先用新文件的传感器 SLM 计算；另列乘以旧独立校准增益 1.0471 的值。新开度试验没有重新识别流量增益。旧零偏 0.0034 SLM 未叠加到新归零流程中。', '',
       '## 各电压时间常数与经验通流能力', '',
       '以下为通过筛选片段的中位数。n 是 ATM/VAC 重复段数。相对通流能力 G=τ(0V)/τ(U)，不是机械开度百分比；略大于100%来自重复实验差异。', '',
       '|目标电压 V|n ATM/VAC|ATM τ s|VAC τ s|ATM G %|VAC G %|',
       '|---:|---:|---:|---:|---:|---:|']
for u in sorted(s.voltage.unique()):
    a=s[(s.direction=='ATM') & (s.voltage==u)].iloc[0]
    v=s[(s.direction=='VAC') & (s.voltage==u)].iloc[0]
    lines.append(f'|{u:.2f}|{int(a.n)}/{int(v.n)}|{a.tau_s:.3f}|{v.tau_s:.3f}|{100*a.relative_conductance_observed:.1f}|{100*v.relative_conductance_observed:.1f}|')
lines += ['|4.25|0/0|不可可靠识别|不可可靠识别|不可可靠识别|不可可靠识别|', '',
          '4.25 V 的 ATM 只测得约 12–16 mL 小幅交换；VAC 有约 0.6 SLM 持续流量，但约 9–10 秒记录不足以区分长时间常数、气袋交换与旁路尾流。',
          '因此不能把优化器返回的数十至上百秒时间常数当成可靠物理结果，也不能声称4.25 V已经完全密封。', '',
          '## PPV 有效开度模型（候选，未部署）', '',
          '保留当前控制器形式：`a(U)=clip((Uc-U)/(Uc-Ud),0,1)`，`tau(U)=tau0/a(U)^gamma`。',
          '这里 a 是经验模型坐标，不是阀门实际行程。与上表的 G 不同：模型中 G=a^gamma。',
          '为了生成可比较参数，暂取 Uc=4.25 V 作为近关闭建模锚点；并未测得准确完全关闭电压。固定tau0为本次0 V中位数，对各电压中位数等权拟合。', '',
          '|参数|ATM|VAC|', '|---|---:|---:|']
for label,key in [('tau0 (s)','tau0_s'),('Ud (V)','plateau_end_v'),('gamma','gamma'),('Uc (V，暂定)','closure_v'),('最大相对τ误差','max_relative_error')]:
    lines.append(f"|{label}|{fit['models']['ATM'][key]:.4f}|{fit['models']['VAC'][key]:.4f}|")
lines += ['', '该简单模型的 ATM 最大时间常数偏差约22%，VAC约14.5%；建议优先使用电压—时间常数查表，避免把有限的幂律参数当作精确机械标定。',
          'fit.json 提供 Uc=4.23/4.25/4.27/4.30/4.35 V 下的参数敏感性；gamma 与 Uc 联动，不能只替换 gamma 而保留旧关闭区间。', '',
          '## 目前的 tidal volume / 可交换气量', '',
          f'- 0–4.15 V，ATM {len(atm)} 段：A中位数 {atm.amplitude_sensor_ml.median():.1f} mL；沿用旧增益后 {atm.amplitude_prior_gain_ml.median():.1f} mL，10–90百分位 {atm.amplitude_prior_gain_ml.quantile(.1):.1f}–{atm.amplitude_prior_gain_ml.quantile(.9):.1f} mL。',
          '- 0 V 单独估计：ATM约307.4 mL；沿用旧增益后321.9 mL。',
          f'- VAC扣除拟合持续尾流后，{len(vac)} 段瞬态气量中位数 {vac.amplitude_sensor_ml.median():.1f} mL；沿用旧增益后 {vac.amplitude_prior_gain_ml.median():.1f} mL。',
          '- ATM与VAC仍有约5.6%的幅值差异，可能包含方向性测量偏差、压力与管路瞬态、初始状态差异；本组无压力数据，不能确定各原因的贡献。不能直接把两个方向平均成精确真实容积。',
          '- 建议描述为“本条件下完整吸气可交换气量约320 mL（沿用旧增益），250 mL是控制目标”。模型幅值与目标/上限是不同参数，不应因为估计320 mL就自动提高250 mL限制。', '',
          '## 与当前硬件参考参数比较', '',
          '比较基准为 single_lung_hardware_reference.json，以及此前946799596实验中发送的同组模型参数；不是仿真GUI中的另一组tau。', '',
          '|参数|现用|本次结果|', '|---|---:|---|',
          '|全开ATM tau|0.743 s|0.585 s，约降低21.2%|',
          '|全开VAC tau|0.2678 s|0.255 s，约降低4.8%|',
          '|模型交换气量|323.9 mL|约320 mL（沿用旧增益），差约−1.1%|',
          '|目标气量/体积上限|250/250 mL|不是本次自动更新项目|',
          '|ATM Ud / gamma|4.0986 V / 1.496|候选3.8708 V / 0.9685，与暂定Uc=4.25配套|',
          '|VAC Ud / gamma|4.1779 V / 2.0621|候选4.0695 V / 1.6262，与暂定Uc=4.25配套|',
          '|关闭电压模型|4.35 V|4.20至4.25 V已进入很强的衰减区；精确密封点未识别|',
          f'|VAC持续尾流|0.814 SLM（校准尺度）|0–4.15 V拟合中位数{vac.bypass_sensor_slm.median():.3f} SLM，乘旧增益后{vac.bypass_sensor_slm.median()*1.0471:.3f} SLM|',
          '|PPV开/关行程时间|2.436/2.436 s|本次定电压试验不能重新识别|',
          '|Kp/Ki/Kd|0.01/0.06/0|本次没有PID寻优，不生成新的最优增益|', '',
          '|电压 V|ATM旧模型τ / 新实测τ s|VAC旧模型τ / 新实测τ s|', '|---:|---:|---:|']
for u in [4.0,4.1,4.15,4.2]:
    a=s[(s.direction=='ATM')&(s.voltage==u)].iloc[0]
    v=s[(s.direction=='VAC')&(s.voltage==u)].iloc[0]
    lines.append(f'|{u:.2f}|{a.old_model_tau_s:.3f} / {a.tau_s:.3f}|{v.old_model_tau_s:.3f} / {v.tau_s:.3f}|')
lines += ['', '4.20 V处，当前模型把ATM响应估计快约3.28倍、VAC快约5.94倍。主要失配在高电压近关闭区，而不是气袋总交换量从320 mL变成250 mL。',
          '这支持此前4.305 V指令处于过于关闭的工作区的解释，但本批未直接测4.305 V，不能量化其精确时间常数。此前PI积分限幅造成开度停住的问题仍独立存在。', '',
          '## 使用限制与后续建议', '',
          '- 采用本批逐电压时间常数作为新模型依据，先固定PI做复核，再开展Auto-PID。未自动覆盖现用配置。',
          '- 若需要可靠Uc及近闭合漏流曲线，补测4.20–4.30 V的小步长和两个移动方向；4.25 V应更长记录并有独立初始气量/压力证据。',
          '- 拟合尾流是观测模型项，不等于已证明的物理漏孔流量；临近关闭区不宜无条件套用同一常数。',
          '- 压力列为空，无法分别识别肺阻力R、顺应性C、真实几何容量或完整气动压差；机械行程比例也不能由SLM和电压唯一恢复。',
          '- Synthetic recovery检查用已知A=320mL、tau=0.6s、VAC b=1SLM生成数据，拟合恢复tau=0.6s、A约320.03mL、b=1SLM。逐段曲线同时人工检查。', '',
          '## 文件', '',
          '- `voltage_summary.csv`：每档电压中位数、四分位数、n、有效开度、相对通流能力及旧模型对比。',
          '- `segments.csv`：全部相位的拟合、接纳/剔除原因，包含诊断性延迟拟合。',
          '- `fit.json`：来源哈希、采样审计、候选模型及关闭电压敏感性。',
          '- `comparison.png`、`ATM_segment_fits.png`、`VAC_segment_fits.png`：总览和逐段拟合图。',
          '- 复现：运行 `analyze_new_sweep.py`，再运行 `write_sweep_report.py`。', '']
(OUT/'report_cn.md').write_text('\n'.join(lines),encoding='utf-8')
print(OUT/'report_cn.md')
