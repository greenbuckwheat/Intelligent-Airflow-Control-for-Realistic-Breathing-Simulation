"""Independent PPV PWM electrical experiment. Run: python "pwm exp.py".

No imports from or modifications to the original hardware application.
Calibration converts target voltage to an actual 12-bit PWM code.
"""
import csv
import json
import math
import sys
import time
from datetime import datetime
from pathlib import Path


def calibration_points(records, direction):
    points = sorted((float(r['measured_v']), int(r['pwm_code']))
                    for r in records if r['kind'] == 'sweep' and r['direction'] == direction)
    if len(points) < 2:
        raise ValueError('所选扫描方向至少需要两个实测点。')
    # Allow small meter/output tolerances around the nominal 0–5 V range.
    # Target commands remain limited to 0–5 V and interpolation never extrapolates.
    if any(not math.isfinite(v) or not 0 <= v <= 5.01 or not 0 <= c <= 4095 for v, c in points):
        raise ValueError('校准数据必须在 0–5.01 V、PWM 0–4095 范围内。')
    if any(v1 >= v2 or c1 >= c2 for (v1, c1), (v2, c2) in zip(points, points[1:])):
        raise ValueError('所选方向有重复点、平台或非单调数据，不能反向插值。')
    return points


def voltage_to_code(target, points):
    if not math.isfinite(target) or not points[0][0] <= target <= points[-1][0]:
        raise ValueError(f'目标不可达：实测范围 {points[0][0]:.4f}–{points[-1][0]:.4f} V；不外推。')
    for (v1, c1), (v2, c2) in zip(points, points[1:]):
        if v1 <= target <= v2:
            return round(c1 + (target - v1) * (c2 - c1) / (v2 - v1))
    return points[-1][1]


def main():
    import serial
    from serial.tools import list_ports
    from PyQt5 import QtCore, QtWidgets
    from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg
    from matplotlib.figure import Figure

    class Window(QtWidgets.QWidget):
        def __init__(self):
            super().__init__()
            self.setWindowTitle('PWM exp — PPV 电气输出验证 / NUCLEO-G474RE')
            self.resize(1180, 820)
            self.setMinimumSize(850, 600)
            self.setStyleSheet('''
                QWidget { font-family: "Microsoft YaHei"; font-size: 13px; }
                QGroupBox { font-weight: bold; border: 1px solid #cbd5e1;
                    border-radius: 6px; margin-top: 12px; padding-top: 14px; }
                QGroupBox::title { subcontrol-origin: margin; left: 10px; }
                QPushButton { padding: 6px 10px; }
                QDoubleSpinBox, QComboBox { padding: 4px; }
            ''')
            self.conn = None
            self.ready = False
            self.frequency = None
            self.records = []
            self.points = None
            self.rx = bytearray()
            self.pending = None
            self.current = None
            self.ack_time = 0
            self.ping_time = 0
            self.last_rx = 0
            self.sequence = list(range(0, 101, 10)) + list(range(100, -1, -10))
            self.index = -1
            layout = QtWidgets.QVBoxLayout(self)
            intro = QtWidgets.QLabel('实验顺序：连接设备 → 扫描 PWM 并录入读数 → 建立校准表 → 输入目标电压验证。\n'
                '保持 PPV 正常连接、无针筒负压。万用表 DC V：红笔接 PPV 控制输入，黑笔接信号地。')
            intro.setWordWrap(True)
            layout.addWidget(intro)
            connection = QtWidgets.QGroupBox('1 · 连接设备与停止输出')
            connection_layout = QtWidgets.QVBoxLayout(connection)
            bar = QtWidgets.QHBoxLayout()
            self.ports = QtWidgets.QComboBox()
            bar.addWidget(self.ports)
            self.button(bar, '刷新串口', self.refresh)
            self.button(bar, '连接', self.connect)
            self.button(bar, '断开', self.disconnect)
            stop_button = self.button(bar, '停止输出（100% PWM）', self.stop)
            stop_button.setStyleSheet('color: #b91c1c; font-weight: bold;')
            connection_layout.addLayout(bar)
            self.status = QtWidgets.QLabel('未连接')
            self.status.setWordWrap(True)
            connection_layout.addWidget(self.status)
            connection_layout.addWidget(QtWidgets.QLabel('需先烧录 pwm_exp.ino。左 ATM D11 / VAC D13 始终关闭，左 PPV 使用 D5；0% PWM 为开，100% PWM 为闭。'))
            layout.addWidget(connection)
            splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
            layout.addWidget(splitter, 1)
            scroll = QtWidgets.QScrollArea()
            scroll.setWidgetResizable(True)
            controls = QtWidgets.QWidget()
            left = QtWidgets.QVBoxLayout(controls)
            left.setContentsMargins(4, 4, 10, 4)
            scroll.setWidget(controls)
            splitter.addWidget(scroll)

            def section(title, explanation):
                group = QtWidgets.QGroupBox(title)
                box = QtWidgets.QVBoxLayout(group)
                hint = QtWidgets.QLabel(explanation)
                hint.setWordWrap(True)
                hint.setStyleSheet('color: #475569; font-weight: normal;')
                box.addWidget(hint)
                left.addWidget(group)
                return box

            scan = section('2 · 扫描占空比：测原始电气关系',
                '第一次实验从这里开始。占空比就是 PWM 为高电平的时间比例。'
                '逐点测量 0%、10%…100%，再从 100% 返回 0%。每次发送后，在第 3 区记录万用表读数。')
            row = QtWidgets.QHBoxLayout()
            self.button(row, '开始 / 下一扫描点', self.next_point)
            self.button(row, '重置扫描游标', self.reset_sweep)
            scan.addLayout(row)
            row = QtWidgets.QHBoxLayout()
            self.duty = self.spin(0, 100, 1, 3)
            row.addWidget(QtWidgets.QLabel('手动占空比 (%)'))
            row.addWidget(self.duty)
            self.button(row, '手动发送', self.send_duty)
            scan.addLayout(row)
            scan.addWidget(self.hint('手动发送用于额外检查，记录不参与自动建表。扫描记录才参与建表。'))

            measurement = section('3 · 录入当前点的万用表读数',
                '设备确认输出后至少等待 1 秒，以万用表读数稳定为准。'
                '这里输入实际测到的电压，不要输入期望值。扫描和目标电压验证都在这里保存读数。')
            row = QtWidgets.QHBoxLayout()
            self.measured = self.spin(-1, 10, 0.01, 4)
            self.measured.setSpecialValueText('输入万用表读数')
            self.measured.setValue(-1)
            row.addWidget(QtWidgets.QLabel('实测电压 V'))
            row.addWidget(self.measured)
            measurement.addLayout(row)
            row = QtWidgets.QHBoxLayout()
            self.save_point_button = self.button(row, '保存当前测量点', self.record)
            self.save_point_button.setEnabled(False)
            measurement.addLayout(row)

            calibration = section('4 · 建立校准表与保存文件',
                '扫描完成后检查数据，再建立“目标电压 → 占空比”的换算表。'
                'up 是升序扫描，down 是降序扫描；两次数据用于检查重复性，建表时选择其中一次。')
            row = QtWidgets.QHBoxLayout()
            self.direction = QtWidgets.QComboBox()
            self.direction.addItems(['up', 'down'])
            row.addWidget(QtWidgets.QLabel('建表使用的扫描方向'))
            row.addWidget(self.direction)
            self.button(row, '检查并建立校准表', self.build)
            calibration.addLayout(row)
            row = QtWidgets.QHBoxLayout()
            self.button(row, '保存校准 JSON', self.save_json)
            self.button(row, '保存记录 CSV（可命名）', self.save_csv)
            self.button(row, '加载校准 JSON', self.load)
            calibration.addLayout(row)
            calibration.addWidget(self.hint('两个文件分别保存并分别命名：JSON 用于以后加载校准；CSV 用 Excel 查看测量记录。'))

            verification = section('5 · 输入目标电压：验证输出准确度',
                '预期电压是你希望 PPV 输入端达到的电压。原理想关系用于检查现有 hardware；'
                '实测校准表用于检查校准后的效果。发送后回第 3 区录入读数。')
            self.mode = QtWidgets.QComboBox()
            self.mode.addItems(['原 hardware 理想关系：占空比 = 电压 / 5', '实测校准表：分段线性插值'])
            verification.addWidget(self.mode)
            row = QtWidgets.QHBoxLayout()
            self.target = self.spin(0, 5, 0.1, 3)
            row.addWidget(QtWidgets.QLabel('预期电压 (V)'))
            row.addWidget(self.target)
            self.button(row, '发送目标电压', self.send_target)
            verification.addLayout(row)
            verification.addWidget(self.hint('例如原理想关系中，2 V 对应 40% PWM。校准模式需要先建表或加载文件，超出实测范围会拒绝发送。'))
            left.addStretch()

            results = QtWidgets.QWidget()
            right = QtWidgets.QVBoxLayout(results)
            right.setContentsMargins(6, 4, 4, 4)
            splitter.addWidget(results)
            splitter.setSizes([470, 690])
            result_box = QtWidgets.QGroupBox('测量记录 · 每保存一个点增加一行')
            result_layout = QtWidgets.QVBoxLayout(result_box)
            result_layout.addWidget(self.hint('误差 = 实测 − 目标。扫描时“目标”是原理想关系算出的电压。PWM 码值是设备使用的 0–4095 整数。'))
            self.table = QtWidgets.QTableWidget(0, 7)
            self.table.setHorizontalHeaderLabels(['类型', '方向', 'PWM码值', '实际占空比 %', '目标 V', '实测 V', '误差 V'])
            self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
            self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
            result_layout.addWidget(self.table)
            right.addWidget(result_box, 3)
            chart_box = QtWidgets.QGroupBox('关系曲线 · 检查实测是否接近理想直线')
            chart_layout = QtWidgets.QVBoxLayout(chart_box)
            chart_layout.addWidget(self.hint('横轴为占空比，纵轴为实测电压；虚线是理想关系，up/down 是升序/降序实测曲线。'))
            self.figure = Figure(figsize=(7, 3), tight_layout=True)
            self.canvas = FigureCanvasQTAgg(self.figure)
            chart_layout.addWidget(self.canvas)
            right.addWidget(chart_box, 3)
            log_box = QtWidgets.QGroupBox('设备消息与校准检查结果')
            log_layout = QtWidgets.QVBoxLayout(log_box)
            self.log = QtWidgets.QPlainTextEdit()
            self.log.setReadOnly(True)
            log_layout.addWidget(self.log)
            right.addWidget(log_box, 1)
            self.timer = QtCore.QTimer(self)
            self.timer.timeout.connect(self.poll)
            self.timer.start(50)
            self.refresh()
            self.plot()

        def hint(self, text):
            label = QtWidgets.QLabel(text)
            label.setWordWrap(True)
            label.setStyleSheet('color: #475569; font-weight: normal; font-size: 12px;')
            return label

        def button(self, row, text, action):
            b = QtWidgets.QPushButton(text)
            b.clicked.connect(lambda: self.guarded(action))
            row.addWidget(b)
            return b

        def spin(self, low, high, step, decimals):
            s = QtWidgets.QDoubleSpinBox()
            s.setRange(low, high); s.setDecimals(decimals); s.setSingleStep(step)
            return s

        def guarded(self, action):
            try:
                action()
            except Exception as exc:
                self.log.appendPlainText(str(exc))
                QtWidgets.QMessageBox.warning(self, '实验提示', str(exc))

        def refresh(self):
            previous = self.ports.currentData()
            self.ports.clear()
            ports = list(list_ports.comports())
            preferred_index = -1
            for index, port in enumerate(ports):
                self.ports.addItem(f'{port.device} — {port.description}', port.device)
                is_stlink = ('stlink' in port.description.lower()
                             or (port.vid == 0x0483 and port.pid == 0x374E))
                if port.device == previous or (previous is None and is_stlink):
                    preferred_index = index
            if preferred_index >= 0:
                self.ports.setCurrentIndex(preferred_index)

        def write(self, command):
            if not self.conn:
                raise ValueError('请先连接实验固件。')
            self.conn.write((command + '\n').encode('ascii'))

        def connect(self):
            port = self.ports.currentData()
            if not port:
                raise ValueError('未选择串口。')
            self.disconnect()
            self.conn = serial.Serial(port, 115200, timeout=0, write_timeout=0.5)
            self.conn.reset_input_buffer()
            self.rx.clear()
            self.last_rx = time.monotonic()
            self.write('HELLO')
            self.ping_time = time.monotonic()
            self.status.setText(f'等待 PWM_EXP 专用固件握手… 当前串口：{port}')

        def disconnect(self):
            if self.conn:
                try:
                    if self.ready:
                        self.write('STOP')
                finally:
                    self.conn.close()
                    self.conn = None
            self.ready = False; self.pending = None; self.current = None
            self.rx.clear()
            self.status.setText('未连接；请确认实物输出状态')

        def stop(self):
            if not self.ready:
                raise ValueError('实验固件尚未握手。')
            self.write('STOP')
            self.pending = None; self.current = None

        def send(self, code, target, kind, direction=''):
            if not self.ready:
                raise ValueError('请连接并等待 PWM_EXP 专用固件握手。')
            if self.pending:
                raise ValueError('上一条 PWM 命令尚未确认。')
            self.current = None
            self.pending = dict(pwm_code=code, target_v=target, kind=kind,
                                direction=direction, sent=time.monotonic())
            self.write(f'PWM,{code}')
            self.status.setText(f'等待确认 PWM={code}（{code / 4095 * 100:.3f}%）')

        def send_target(self):
            target = self.target.value()
            if self.mode.currentIndex() == 1:
                if not self.points:
                    raise ValueError('先建立或加载实测校准表。')
                code = voltage_to_code(target, self.points)
                kind = 'verify_calibrated'
            else:
                code = round(target / 5 * 4095)
                kind = 'verify_ideal'
            self.send(code, target, kind)

        def send_duty(self):
            code = round(self.duty.value() / 100 * 4095)
            self.send(code, code / 4095 * 5, 'manual')

        def next_point(self):
            if self.current and self.current['kind'] == 'sweep':
                raise ValueError('请先保存当前扫描点的实测读数。')
            nxt = self.index + 1
            if nxt >= len(self.sequence):
                raise ValueError('升序与降序扫描已完成。')
            duty = self.sequence[nxt]
            code = round(duty / 100 * 4095)
            self.send(code, code / 4095 * 5, 'sweep', 'up' if nxt < 11 else 'down')
            self.index = nxt
            self.duty.setValue(duty)

        def reset_sweep(self):
            if self.pending:
                raise ValueError('等待命令确认后重置。')
            self.index = -1; self.current = None
            self.log.appendPlainText('扫描游标已重置；已有记录保留。重复点需另存文件后重新启动实验。')

        def poll(self):
            self.save_point_button.setEnabled(bool(self.current) and time.monotonic() - self.ack_time >= 1)
            if not self.conn:
                return
            try:
                now = time.monotonic()
                if now - self.ping_time >= 0.7:
                    self.write('PING' if self.ready else 'HELLO')
                    self.ping_time = now
                self.rx.extend(self.conn.read(self.conn.in_waiting))
                if len(self.rx) > 8192:
                    raise ValueError('串口数据异常。')
                while b'\n' in self.rx:
                    raw, _, rest = self.rx.partition(b'\n'); self.rx = bytearray(rest)
                    line = raw.decode('ascii', errors='replace').strip()
                    self.last_rx = now
                    if line == 'PONG':
                        continue
                    self.log.appendPlainText(line)
                    f = line.split(',')
                    if f[:3] == ['READY', 'PWM_EXP_G474', '1'] and len(f) == 6:
                        if f[3:] != ['1000', '12', 'D5']:
                            raise ValueError('固件频率/分辨率/通道与此实验不一致。')
                        was_ready = self.ready
                        if was_ready and (self.pending or self.current):
                            self.log.appendPlainText('设备重新握手；已取消尚未保存的当前测量点。')
                        self.pending = None; self.current = None
                        self.frequency = int(f[3]); self.ready = True
                        self.write('STOP')
                        self.status.setText('已连接：左 PPV D5 / 1000 Hz / 12 bit；ATM D11、VAC D13 关闭')
                    elif f[:2] == ['ACK', 'PWM']:
                        if not self.pending or int(f[2]) != self.pending['pwm_code']:
                            raise ValueError('PWM 确认与发送不一致，请重新连接。')
                        self.current = self.pending; self.pending = None; self.ack_time = now
                        self.measured.setValue(-1)
                        self.status.setText(f"PWM 已确认：{self.current['pwm_code'] / 4095 * 100:.3f}%；等待读数稳定后录入")
                    elif line.startswith(('FAULT,', 'ERR,')):
                        self.pending = None; self.current = None
                        self.write('STOP'); self.status.setText(line)
                    elif line.startswith('STOPPED,'):
                        self.status.setText('已连接，当前已停止：ATM/VAC 关闭；PWM 100%（名义 5 V）')
                if self.pending and now - self.pending['sent'] > 2:
                    raise ValueError('PWM 命令确认超时。')
                if now - self.last_rx > 4:
                    raise ValueError('串口握手/心跳超时；设备将执行失联停止。')
            except Exception as exc:
                self.log.appendPlainText(str(exc))
                self.disconnect()

        def record(self):
            if not self.current or time.monotonic() - self.ack_time < 1:
                raise ValueError('先发送命令并等待确认后至少 1 秒。')
            measured = self.measured.value()
            if measured < 0:
                raise ValueError('请录入万用表读数。')
            r = {k: v for k, v in self.current.items() if k != 'sent'}
            r.update(measured_v=measured, duty_percent=r['pwm_code'] / 4095 * 100,
                     error_v=measured - r['target_v'], timestamp=datetime.now().astimezone().isoformat())
            self.records.append(r); self.current = None
            if r['kind'] == 'sweep':
                self.points = None
            self.render()
            if measured > 5.01:
                # This is an electrical-output fault: reduce PWM instead of using
                # STOP, because STOP intentionally commands 100% / nominal 5 V.
                self.write('PWM,0')
                self.pending = None; self.current = None
                raise ValueError('实测超过 5.01 V，已将 PWM 降为 0%。请立即切断 PPV 控制电路电源并检查电路。')

        def render(self):
            self.table.setRowCount(len(self.records))
            names = {'sweep': 'PWM扫描', 'manual': '手动PWM',
                     'verify_ideal': '原关系验证', 'verify_calibrated': '校准后验证'}
            for i, r in enumerate(self.records):
                values = [names.get(r['kind'], r['kind']), {'up': '升序', 'down': '降序'}.get(r['direction'], '—'), r['pwm_code'], f"{r['duty_percent']:.3f}",
                          f"{r['target_v']:.4f}", f"{r['measured_v']:.4f}", f"{r['error_v']:+.4f}"]
                for j, value in enumerate(values):
                    self.table.setItem(i, j, QtWidgets.QTableWidgetItem(str(value)))
            self.plot()

        def plot(self):
            self.figure.clear(); ax = self.figure.add_subplot(111)
            ax.plot([0, 100], [0, 5], '--', label='Ideal V = 5D')
            for direction in ['up', 'down']:
                rows = sorted([r for r in self.records if r['kind'] == 'sweep' and r['direction'] == direction], key=lambda r: r['pwm_code'])
                if rows:
                    ax.plot([r['duty_percent'] for r in rows], [r['measured_v'] for r in rows], 'o-', label=direction)
            ax.set_xlabel('Actual PWM duty (%)'); ax.set_ylabel('Measured voltage (V)')
            ax.grid(True); ax.legend(); self.canvas.draw_idle()

        def build(self):
            self.points = calibration_points(self.records, self.direction.currentText())
            rows = [r for r in self.records if r['kind'] == 'sweep' and r['direction'] == self.direction.currentText()]
            errors = [r['measured_v'] - r['pwm_code'] / 4095 * 5 for r in rows]
            self.log.appendPlainText(f'校准范围 {self.points[0][0]:.4f}–{self.points[-1][0]:.4f} V；'
                f'相对原理想关系最大误差 {max(map(abs, errors)):.4f} V。')
            common = {}
            for r in self.records:
                if r['kind'] == 'sweep':
                    common.setdefault(r['pwm_code'], {})[r['direction']] = r['measured_v']
            differences = [abs(d['up'] - d['down']) for d in common.values() if 'up' in d and 'down' in d]
            if differences:
                self.log.appendPlainText(f'升降序同点最大差 {max(differences):.4f} V；请判断重复性后使用单方向表。')

        def suggested_name(self, suffix):
            has_verification = any(r['kind'].startswith('verify_') for r in self.records)
            stage = '校准验证' if has_verification else 'PWM扫描'
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            return Path(__file__).parent / f'PPV40_{stage}_{timestamp}{suffix}'

        def calibration_data(self):
            return dict(schema='pwm_exp_v1', channel='D5', board='NUCLEO-G474RE',
                        calibration_date=datetime.now().astimezone().isoformat(), pwm_frequency_hz=self.frequency,
                        resolution_bits=12, direction=self.direction.currentText(), records=self.records,
                        calibration_points=self.points,
                        measured_min_v=min(r['measured_v'] for r in self.records),
                        measured_max_v=max(r['measured_v'] for r in self.records))

        def save_json(self):
            if not self.records:
                raise ValueError('没有测量记录。')
            # Always validate and embed the selected sweep before saving so the
            # JSON cannot silently contain a null calibration table.
            self.points = calibration_points(self.records, self.direction.currentText())
            filename, _ = QtWidgets.QFileDialog.getSaveFileName(
                self, '命名并保存校准 JSON', str(self.suggested_name('.json')), 'JSON 文件 (*.json)')
            if not filename:
                return
            path = Path(filename).with_suffix('.json')
            path.write_text(json.dumps(self.calibration_data(), ensure_ascii=False, indent=2), encoding='utf-8')
            self.log.appendPlainText(f'已保存校准 JSON：{path}')

        def save_csv(self):
            if not self.records:
                raise ValueError('没有测量记录。')
            filename, _ = QtWidgets.QFileDialog.getSaveFileName(
                self, '命名并保存测量记录 CSV', str(self.suggested_name('.csv')), 'CSV 文件 (*.csv)')
            if not filename:
                return
            path = Path(filename).with_suffix('.csv')
            with path.open('w', newline='', encoding='utf-8-sig') as handle:
                writer = csv.DictWriter(handle, fieldnames=list(self.records[0]))
                writer.writeheader(); writer.writerows(self.records)
            self.log.appendPlainText(f'已保存测量记录 CSV：{path}')

        def load(self):
            if self.records or self.pending or self.current:
                raise ValueError('加载请使用新启动且没有测量记录的窗口。')
            filename, _ = QtWidgets.QFileDialog.getOpenFileName(self, '加载校准', str(Path(__file__).parent), 'JSON (*.json)')
            if not filename:
                return
            d = json.loads(Path(filename).read_text(encoding='utf-8'))
            if (d.get('schema'), d.get('channel'), d.get('pwm_frequency_hz'), d.get('resolution_bits')) != ('pwm_exp_v1', 'D5', 1000, 12):
                raise ValueError('校准文件版本、通道、频率或分辨率不匹配。')
            points = calibration_points(d['records'], d['direction'])
            self.records = d['records']; self.points = points; self.frequency = 1000
            self.direction.setCurrentText(d['direction']); self.mode.setCurrentIndex(1)
            self.render()
            self.log.appendPlainText('已加载校准表。原 hardware 固件不会自动使用此文件。')

        def closeEvent(self, event):
            self.guarded(self.disconnect)
            event.accept()

    app = QtWidgets.QApplication(sys.argv)
    window = Window(); window.show()
    sys.exit(app.exec_())


if __name__ == '__main__':
    main()
