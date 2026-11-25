"""Hardware-aligned simulation UI using an in-process virtual controller.

The hardware application is reused, never monkey-patched. Shared controls,
validation, tuning and guide therefore have one authoritative implementation.
"""
import copy
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import queue
import time

from PyQt5 import QtCore, QtWidgets

import single_lung_hardware_gui as hw
from single_lung_simulation_backend import VirtualPlantSettings, VirtualSerial


class HardwareAlignedSimulationWindow(hw.MainWindow):
    def __init__(self, config_path=None):
        # Deliberately use the hardware reference, not the historical simulation
        # JSON. Neither configuration file is overwritten by this application.
        self.virtual_settings = VirtualPlantSettings()
        self._clock = time.monotonic()
        self._step_budget = 0.0
        super().__init__(config_path=config_path or hw.CONFIG_PATH)
        self.virtual_settings.residual_delay_s = self.cfg.smith.delay_s
        self.plant_delay.setValue(self.virtual_settings.residual_delay_s)
        self.setWindowTitle('LungLogic Simulation | Hardware-aligned protocol 21 | SYNTHETIC DATA')
        self.developer_tabs.setTabText(1, 'Live Simulation')
        self.flow_canvas.ax.set_title('Simulated Flow Rate')
        self.user_flow_canvas.ax.set_title('Simulated Flow Rate')
        self.connect_serial()
        self._tick()

    def _build_serial_bar(self):
        group = QtWidgets.QGroupBox('Virtual Device / Simulation Assumptions')
        layout = QtWidgets.QGridLayout(group)
        connection_row = QtWidgets.QHBoxLayout()
        self.port_combo = QtWidgets.QComboBox()
        self.port_combo.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.port_combo.setMinimumContentsLength(20)
        self.connect_button = QtWidgets.QPushButton('Reconnect')
        self.connect_button.clicked.connect(self.connect_serial)
        disconnect = QtWidgets.QPushButton('Disconnect')
        disconnect.clicked.connect(self.disconnect_serial)
        self.connection_label = QtWidgets.QLabel('Disconnected')
        connection_row.addWidget(self.port_combo, 1)
        connection_row.addWidget(self.connect_button)
        connection_row.addWidget(disconnect)
        connection_row.addWidget(self.connection_label)
        layout.addLayout(connection_row, 0, 0)
        self.speed = QtWidgets.QDoubleSpinBox()
        self.speed.setRange(.1, 20)
        self.speed.setValue(1)
        self.speed.setSuffix(' x')
        self.speed.setToolTip('Wall-clock playback speed only; control remains 100 Hz in simulated time.')
        self.plant_delay = QtWidgets.QDoubleSpinBox()
        self.plant_delay.setDecimals(3)
        self.plant_delay.setRange(0, 1)
        self.plant_delay.setSingleStep(.01)
        self.plant_delay.setToolTip('Synthetic physical delay; independent of Smith compensation. Applied at next Start.')
        self.plant_noise = QtWidgets.QDoubleSpinBox()
        self.plant_noise.setDecimals(3)
        self.plant_noise.setRange(0, 10)
        assumptions_row = QtWidgets.QHBoxLayout()
        for label, widget in (('Playback', self.speed), ('Plant delay / s', self.plant_delay),
                              ('Noise std / SLM', self.plant_noise)):
            widget.setMaximumWidth(100)
            assumptions_row.addWidget(QtWidgets.QLabel(label))
            assumptions_row.addWidget(widget)
        assumptions_row.addStretch(1)
        layout.addLayout(assumptions_row, 1, 0)
        notice = QtWidgets.QLabel(
            'SIMULATION ONLY: all plots, telemetry, scores and Auto PID results are synthetic. '
            'Uses the hardware controls and nominal empirical model; no physical serial port is opened. '
            'Simulation is not validation of pneumatic switching or closed-loop bandwidth.')
        notice.setWordWrap(True)
        notice.setStyleSheet('background:#fff1d6; color:#684d19; padding:6px; font-weight:600')
        layout.addWidget(notice, 2, 0)
        return group

    def refresh_ports(self):
        self.port_combo.clear()
        self.port_combo.addItem('Virtual STM32 (offline)', 'virtual')

    def connect_serial(self):
        if self.connection is not None:
            self.disconnect_serial(save=True)
        self.messages = queue.Queue()
        self.connection = VirtualSerial(self.messages, self.cfg, self.virtual_settings)
        self.connection.write(b'HELLO\n')
        self._clock = time.monotonic()
        self._step_budget = 0.
        self.connection_label.setText('Connected: SYNTHETIC')

    def _draw_dynamic_model(self, row, both=False):
        super()._draw_dynamic_model(row, both=both)
        for canvas in (self.user_model_canvas, self.model_canvas):
            canvas.ax.set_title('Single-Lung Simulated Dynamic Model', fontsize=15,
                                fontweight='bold', pad=10)
            canvas.draw_idle()

    def _write_config(self, cfg):
        if isinstance(self.connection, VirtualSerial):
            self.connection.pending_config = copy.deepcopy(cfg)
        return super()._write_config(cfg)

    def _launch_run(self, cfg):
        self.virtual_settings = VirtualPlantSettings(
            residual_delay_s=self.plant_delay.value(), noise_slm=self.plant_noise.value())
        if isinstance(self.connection, VirtualSerial):
            self.connection.controller.set_plant_settings(self.virtual_settings)
        self.run_virtual_settings = asdict(self.virtual_settings)
        self._clock = time.monotonic()
        self._step_budget = 0.
        return super()._launch_run(cfg)

    def _tick(self):
        now = time.monotonic()
        if isinstance(self.connection, VirtualSerial):
            self._step_budget += max(0, now-self._clock) * self.speed.value() * 100
            steps = min(200, int(self._step_budget))
            self._step_budget -= steps
            self.connection.advance(steps)
        self._clock = now
        super()._tick()

    def _simulation_dir(self):
        path = hw.output_dir() / 'simulation'
        path.mkdir(parents=True, exist_ok=True)
        return path

    def save_current_log(self, path=None):
        if not self.rows:
            self._log('No simulated telemetry to save.')
            return None
        run_id = self.current_run_id or self.rows[0].run_id
        if path is None:
            raw = Path(self.log_path_text.strip()) if self.log_path_text.strip() else Path('run.csv')
            folder = raw.parent if raw.is_absolute() else self._simulation_dir() / raw.parent
            name = f'SIMULATED_{raw.stem}_{run_id}'
            if self.run_stage:
                name += '_' + self.run_stage
            path = folder / (name + '.csv')
        path = Path(path)
        # Never replace an unrelated pre-existing file, even if explicitly chosen.
        if path.exists() and self.saved_log_paths.get(run_id) != path:
            path = path.with_name(f'{path.stem}_{time.time_ns()}{path.suffix}')
        run_cfg = self.run_cfg or self.cfg
        compact = self.compact_csv or bool(self.run_stage)
        meta = dict(schema_version=2, data_source='simulation', simulated=True,
            protocol_version=hw.PROTOCOL_VERSION, run_id=run_id, run_status=self.run_status,
            saved_at_utc=datetime.now(timezone.utc).isoformat(), configuration=asdict(run_cfg),
            configuration_source='SIMULATED_frozen_run_configuration',
            objective=hw.OBJECTIVE, auto_pid_stage=self.run_stage,
            auto_pid_settings=getattr(self, 'run_tuning', None),
            auto_pid_warmup_cycles=self.run_warmup,
            recovery_timeout=self.run_recovery_timeout,
            previous_run_recovery_timeout=self.previous_recovery_timeout,
            events=self.run_events, csv_layout='telemetry_only' if compact else 'telemetry_plus_context',
            simulation=dict(model='protocol21_nominal_empirical_twin_v1',
                plant_settings=getattr(self, 'run_virtual_settings', asdict(self.virtual_settings)),
                control_hz=100, telemetry_hz=50,
                limitations='Nominal model; no identified switching transients, sensor faults or hardware timing jitter. Not physical validation.'))
        try:
            meta['metrics'] = hw.hardware_run_metrics(self.rows, run_cfg,
                run_completed=self.run_status=='completed', warmup_cycles=self.run_warmup)
            complete = self.run_status=='completed' and meta['metrics']['N_c']==run_cfg.cycles-self.run_warmup
            meta['score_status'] = 'complete' if complete else 'partial_complete_cycles_only'
            if complete:
                self.last_run_metrics = hw.auto_pid_repeat_metrics(self.rows, run_cfg, meta['metrics'], self.run_warmup)
                self.last_run_cfg = copy.deepcopy(run_cfg) if self.last_run_metrics else None
        except ValueError as exc:
            meta.update(metrics=None, score_status='unavailable', score_error=str(exc))
        try:
            hw.save_log(self.rows, path, meta, include_context=not compact)
            path.with_suffix('.json').write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding='utf-8')
        except (OSError, ValueError) as exc:
            self._log(f'Could not save simulated data: {exc}')
            return None
        self.saved_log_paths[run_id] = path
        self.saved_log_rows[run_id] = len(self.rows)
        self._set_log_status(f'SIMULATED data saved: {path}')
        return path

    def _save_auto_pid_report(self, tuner):
        if self.auto_report_path is None or not self.auto_report_path.name.startswith('SIMULATED_'):
            self.auto_report_path = self._simulation_dir() / f'SIMULATED_auto_pid_{time.time_ns()}.csv'
        path = super()._save_auto_pid_report(tuner)
        summary_path = path.with_name(path.stem + '_summary.json')
        summary = json.loads(summary_path.read_text(encoding='utf-8'))
        summary.update(data_source='simulation', simulated=True,
            simulation_plant_settings=getattr(self, 'run_virtual_settings', asdict(self.virtual_settings)))
        summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str), encoding='utf-8')
        return path

    def _handle_line(self, line):
        super()._handle_line(line)
        if line == hw.READY:
            self.statusBar().showMessage('Virtual controller ready. All data are SIMULATED.')


def create_window(config_path=None):
    return HardwareAlignedSimulationWindow(config_path)
