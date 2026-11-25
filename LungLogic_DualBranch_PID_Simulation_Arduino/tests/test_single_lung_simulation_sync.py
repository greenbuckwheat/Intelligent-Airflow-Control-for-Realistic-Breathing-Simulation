"""Hardware-reference UI parity and protocol-21 virtual controller behavior."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import copy
from dataclasses import asdict
import json
from pathlib import Path
import queue
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from PyQt5 import QtWidgets

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import single_lung_hardware_gui as hw
from single_lung_simulation_backend import (
    VirtualController, VirtualPlantSettings, VirtualSerial, atm_conductance,
    atm_voltage, advance_position, run_hardware_aligned_simulation,
)
from single_lung_simulation_gui import HardwareAlignedSimulationWindow


class ControllerTwinTests(unittest.TestCase):
    def setUp(self):
        self.cfg = hw.load_config(hw.CONFIG_PATH)

    def test_continuous_atm_map_roundtrips_across_measured_and_join_regions(self):
        p = self.cfg.lung
        for volts in np.linspace(0, p.hardware_closure_v, 151):
            self.assertAlmostEqual(atm_voltage(atm_conductance(volts, p), p), volts, places=10)
        join = p.hardware_closure_v-.1
        self.assertLess(abs(atm_conductance(join-1e-7, p)-atm_conductance(join+1e-7, p)), 1e-5)

    def test_calibrated_motion_and_independent_physical_delay(self):
        c = self.cfg
        self.assertAlmostEqual(advance_position(0, 4.2, .01, c), 4.2*.01/2.436)
        settings = VirtualPlantSettings(residual_delay_s=.17)
        v = VirtualController(c, settings)
        c.smith.enabled = False
        c.smith.delay_s = .8
        v.configure(c)
        self.assertEqual(v.settings.residual_delay_s, .17)
        self.assertFalse(v.cfg.smith.enabled)

    def test_complete_run_is_scored_on_hardware_objective(self):
        self.cfg.cycles = 3
        rows, events = run_hardware_aligned_simulation(self.cfg)
        self.assertIn('DONE,1', events)
        self.assertTrue(all(b.time_ms-a.time_ms==20 for a,b in zip(rows, rows[1:])))
        metrics = hw.hardware_run_metrics(rows, self.cfg, run_completed=True)
        self.assertEqual(metrics['N_c'], 3)
        self.assertAlmostEqual(metrics['score'], .65*metrics['J_Q']+.35*metrics['J_V'])
        self.assertTrue(all(r.valve_mask in (0,1,2) for r in rows))
        exp = [r for r in rows if r.state == hw.STATE_EXPIRATION]
        self.assertTrue(all(r.valve_mask==2 and r.flow_reference_slm<0 for r in exp))
        self.assertTrue(all(r.command_v >= self.cfg.hardware_inspiration_min_v-1e-10
            for r in rows if r.state==hw.STATE_INSPIRATION and r.valve_mask==1))
        # Firmware's blended correction preserves the measured feedback coefficient at 1.
        for r in rows:
            if r.state in (hw.STATE_INSPIRATION, hw.STATE_EXPIRATION) and r.smith_ideal_flow_slm:
                expected = r.net_flow_slm+self.cfg.smith.correction_gain*(
                    r.smith_ideal_flow_slm-r.smith_actuator_flow_slm)
                self.assertAlmostEqual(r.pid_feedback_slm, expected)

    def test_positioning_waits_dwell_and_protocol21_period(self):
        self.cfg.cycles = 3
        rows, _ = run_hardware_aligned_simulation(self.cfg)
        starts = [next(r.time_ms for r in rows if r.state==hw.STATE_INSPIRATION and r.cycle==c)
                  for c in range(3)]
        # Onset-to-onset includes changes in startup transition accounting;
        # DEAD_TO_INSP-to-DEAD_TO_INSP is the period for subsequent cycles.
        epochs = [next(r.time_ms for r in rows if r.state==hw.STATE_DEAD_TO_INSP and r.cycle==c)
                  for c in range(3)]
        self.assertLessEqual(abs(epochs[2]-epochs[1]-self.cfg.cycle_period_s*1000), 30)
        for c in range(3):
            self.assertGreaterEqual(starts[c]-epochs[c]+20, self.cfg.hardware_inspiration_dwell_min_s*1000)
        self.cfg.dead_time_within_cycle = False
        outside, _ = run_hardware_aligned_simulation(self.cfg)
        self.assertGreater(outside[-1].time_ms, rows[-1].time_ms)

    def test_separate_expiration_pi_affects_expiration(self):
        self.cfg.cycles = 1
        base, _ = run_hardware_aligned_simulation(self.cfg)
        self.cfg.hardware_expiration_kp = 0
        self.cfg.hardware_expiration_ki = 0
        changed, _ = run_hardware_aligned_simulation(self.cfg)
        base_in = [r.command_v for r in base if r.state==hw.STATE_INSPIRATION]
        changed_in = [r.command_v for r in changed if r.state==hw.STATE_INSPIRATION]
        np.testing.assert_allclose(base_in, changed_in)
        self.assertGreater(max(r.opening for r in base if r.state==hw.STATE_EXPIRATION), 0)
        self.assertEqual(max(r.opening for r in changed if r.state==hw.STATE_EXPIRATION), 0)

    def test_manual_interlock_and_stop_does_not_restart_recovery_timeout(self):
        q = queue.Queue()
        serial = VirtualSerial(q, self.cfg)
        for command, expected in [('MANUAL,ATM,OPEN',1),('MANUAL,VAC,OPEN',2),
                                  ('MANUAL,ATM,CLOSE',2),('MANUAL,VAC,CLOSE',0)]:
            serial.write((command+'\n').encode())
            self.assertEqual(serial.controller.mask, expected)
        serial.write(b'START,2,1\n')
        serial.advance(50)
        serial.write(b'STOP\n')
        start = serial.controller.state_start_ms
        serial.advance(10)
        serial.write(b'STOP\n')
        self.assertEqual(serial.controller.state_start_ms, start)
        self.assertTrue(serial.controller.stopped_by_user)


class SyncedGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='lunglogic_sync_')
        self.env = patch.dict(os.environ, {'LUNGLOGIC_OUTPUT_DIR': self.tmp.name})
        self.env.start()
        # Any accidental physical serial access must fail the test.
        self.serial_patch = patch.object(hw.serial, 'Serial', side_effect=AssertionError('physical serial access')) if hw.serial else None
        if self.serial_patch:
            self.serial_patch.start()
        self.hardware = hw.MainWindow()
        self.virtual = HardwareAlignedSimulationWindow()
        self.hardware.timer.stop()
        self.virtual.timer.stop()

    def tearDown(self):
        self.app.processEvents()
        self.virtual.close()
        self.hardware.close()
        self.app.processEvents()
        if self.serial_patch:
            self.serial_patch.stop()
        self.env.stop()
        self.tmp.cleanup()

    def test_default_configs_controls_ranges_precision_and_locks_match(self):
        self.assertEqual(asdict(self.virtual.cfg), asdict(self.hardware.cfg))
        for collection in ('inputs', 'user_inputs'):
            actual, reference = getattr(self.virtual, collection), getattr(self.hardware, collection)
            self.assertEqual(set(actual), set(reference))
            for key in reference:
                for name in ('value','minimum','maximum','decimals','singleStep','isEnabled'):
                    self.assertEqual(getattr(actual[key],name)(),getattr(reference[key],name)(), (collection,key,name))
        self.assertTrue(self.virtual.firmware_ready)
        self.assertLessEqual(self.virtual.minimumSizeHint().width(), 1360)
        self.assertFalse(self.virtual.ff_expiration_enabled.isEnabled())
        self.assertFalse(self.virtual.ff_expiration_enabled.isChecked())
        self.assertFalse(self.virtual.inputs['kd_out'].isEnabled())
        self.assertEqual(asdict(self.virtual.cfg_from_ui()), asdict(self.hardware.cfg_from_ui()))

    def test_controller_changes_reach_virtual_device_without_touching_hardware(self):
        self.virtual.mode_combo.setCurrentIndex(1)
        self.virtual.inputs['ki_out'].setValue(.21)
        self.virtual.inputs['ilim_out'].setValue(9)
        self.virtual.smith_enabled.setChecked(False)
        self.assertTrue(self.virtual.send_config())
        c = self.virtual.connection.controller.cfg
        self.assertEqual(c.hardware_expiration_ki, .21)
        self.assertEqual(c.hardware_expiration_integral_limit, 9)
        self.assertFalse(c.smith.enabled)
        self.assertEqual(self.virtual.connection.controller.settings.residual_delay_s, .2)
        self.assertEqual(self.hardware.cfg.hardware_expiration_ki, .12)

    def test_both_guides_include_current_policies_and_simulation_scope(self):
        for window in (self.hardware, self.virtual):
            guide = hw.HardwareGuideDialog(window)
            tabs = guide.findChild(QtWidgets.QTabWidget)
            text = '\n'.join(tabs.widget(i).toPlainText() for i in range(tabs.count()))
            for topic in ('Protocol 21 timing','expiration 8','0.35 s','Resume probe',
                          'Frequency analysis','Nyquist','volume-dependent feedforward'):
                self.assertIn(topic, text)
            self.assertEqual('SIMULATION /' in text, window is self.virtual)
            guide.close()

    def test_complete_gui_run_exports_explicitly_simulated_data_and_metrics(self):
        self.virtual.start_run()
        v = self.virtual.connection
        while v.controller.running:
            v.advance(200)
            # Drain without advancing the wall clock.
            hw.MainWindow._tick(self.virtual)
        self.assertEqual(self.virtual.run_status, 'completed')
        self.assertGreater(len(self.virtual.rows), 100)
        path = self.virtual.saved_log_paths[self.virtual.current_run_id]
        self.assertTrue(path.name.startswith('SIMULATED_'))
        self.assertEqual(path.parent.name, 'simulation')
        meta = json.loads(path.with_suffix('.json').read_text(encoding='utf-8'))
        self.assertEqual(meta['data_source'], 'simulation')
        self.assertEqual(meta['score_status'], 'complete')
        self.assertEqual(meta['metrics']['N_c'], 1)
        self.assertEqual(meta['simulation']['control_hz'], 100)

    def test_sensitivity_sequence_uses_virtual_runs_and_simulated_reports(self):
        w = self.virtual
        w.user_inputs['cycles'].setValue(3)
        w.user_auto_pid.setChecked(True)
        w.es_probe.setChecked(True)
        for box in w.health_override_boxes:
            box.setChecked(True)
        callbacks = []
        with patch.object(w, '_confirm_auto_pid', return_value=True), patch.object(
                hw.QtCore.QTimer, 'singleShot', side_effect=lambda delay, callback: callbacks.append(callback)):
            w.start_run()
            iterations = 0
            while w.auto_tune_active:
                iterations += 1
                self.assertLess(iterations, 1500, 'Auto PID sequence failed to finish')
                w.connection.advance(200)
                hw.MainWindow._tick(w)
                while callbacks:
                    callbacks.pop(0)()
        self.assertIsInstance(w.auto_tuner, hw.SensitivityProbe)
        self.assertTrue(w.auto_tuner.finished)
        self.assertTrue(w.auto_report_path.name.startswith('SIMULATED_'))
        summary = json.loads(w.auto_report_path.with_name(
            w.auto_report_path.stem+'_summary.json').read_text(encoding='utf-8'))
        self.assertTrue(summary['simulated'])
        self.assertEqual(summary['data_source'], 'simulation')
        self.assertEqual(w.cfg.pi.kp, .01)
        self.assertEqual(w.cfg.pi.ki, .04)


if __name__ == '__main__':
    unittest.main()
