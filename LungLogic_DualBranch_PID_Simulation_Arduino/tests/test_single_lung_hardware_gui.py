"""Offline checks for the single-lung hardware GUI and host-side Auto PID."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import sys
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PyQt5 import QtWidgets
import single_lung_hardware_gui as hardware
import single_lung_pid_gui as sim


class HardwareLogicTests(unittest.TestCase):
    def test_named_log_path_defaults_to_output_and_adds_csv_suffix(self):
        path = hardware.resolve_log_path("named_trial", 42)
        self.assertEqual(path, hardware.SCRIPT_DIR / "output" / "named_trial.csv")
        automatic = hardware.resolve_log_path("", 42)
        self.assertEqual(
            automatic,
            hardware.SCRIPT_DIR / "output" / "single_lung_hardware_run_42.csv",
        )
        candidate = hardware.auto_pid_log_path(path, "round_1_kp_plus", 99)
        self.assertEqual(candidate.name, "named_trial_round_1_kp_plus_99.csv")

    def test_calibration_protocol_telemetry_and_validation(self):
        cfg = hardware.load_config(hardware.CONFIG_PATH)
        fields = hardware.config_command(cfg, 1).split(",")
        self.assertEqual([float(x) for x in fields[30:]], [0.0034, 1.0471, 0.814])
        row = hardware.HardwareTelemetry.parse("TEL,1,20,5,0,-10,100,-9,0.5,4,0,3,0.5,-9.55,-9.069,2")
        self.assertEqual(row.flow_slm, -10)
        self.assertEqual(row.control_flow_slm, -9.069)
        self.assertEqual(row.raw_flow_slm, -9.55)
        self.assertEqual(row.sampled_valve_mask, 2)
        self.assertEqual(row.valve_mask, 0)
        for name, value in (("hardware_flow_gain", 0), ("hardware_flow_offset_slm", float("nan")),
                            ("hardware_vac_leak_slm", -1)):
            bad = hardware.copy.deepcopy(cfg)
            setattr(bad.lung, name, value)
            with self.assertRaises(ValueError):
                hardware.config_command(bad, 1)

    def test_reference_protocol_separates_model_amplitude_and_volume_guard(self):
        cfg = hardware.load_config(hardware.CONFIG_PATH)
        fields = hardware.config_command(cfg, 123).split(",")
        self.assertEqual(len(fields), 33)
        self.assertEqual(fields[:3], ["CFG", "8", "123"])
        self.assertEqual(float(fields[6]), 250.0)
        self.assertEqual(float(fields[7]), 3.333)
        self.assertEqual(float(fields[15]), 250.0)
        self.assertEqual(float(fields[17]), 323.9)
        self.assertEqual([float(v) for v in fields[18:20]], [0.743, 0.2678])
        self.assertEqual(float(fields[20]), 1.496)
        self.assertEqual(float(fields[24]), 2.0621)
        self.assertEqual([float(x) for x in fields[25:28]], [4.0986, 4.1779, 4.35])
        self.assertTrue(cfg.smith.enabled)
        self.assertEqual([float(x) for x in fields[28:30]], [2.436, 2.436])
        self.assertEqual(cfg.cycles, 1)
        cfg.validate()

    def test_ppv_calibration_required_and_serialized(self):
        cfg = hardware.load_config(hardware.CONFIG_PATH)
        cfg.smith.enabled = False
        cfg.smith.ppv_open_time_s = 0
        with self.assertRaises(ValueError):
            hardware.config_command(cfg, 1)
        cfg.smith.ppv_open_time_s = 1.2
        cfg.smith.ppv_close_time_s = 2.1
        self.assertEqual(hardware.config_command(cfg, 1).split(",")[28:30], ["1.2", "2.1"])
        cfg.smith.ppv_close_time_s = float("nan")
        with self.assertRaises(ValueError):
            hardware.config_command(cfg, 1)

    def test_reference_tracks_replacement_hardware_directional_sweep(self):
        p = hardware.load_config(hardware.CONFIG_PATH).lung
        # Independent measured voltage medians from Oct12, not the old 2 L plant.
        for voltage, tau_in, tau_out in ((4.15, 1.052, .309), (4.2, 1.349, .386),
                                         (4.25, 2.49, .721), (4.3, 9.689, 3.594)):
            for measured, tau0, ud, gamma in (
                (tau_in, p.tau_in_s, p.hardware_dead_zone_in_v, p.hardware_gamma_in),
                (tau_out, p.tau_out_s, p.hardware_dead_zone_out_v, p.hardware_gamma_out),
            ):
                a = min(1.0, (p.hardware_closure_v-voltage)/(p.hardware_closure_v-ud))
                predicted = tau0 / a**gamma
                self.assertLess(abs(predicted/measured-1), .25)
        self.assertTrue(300 < p.hardware_model_volume_ml < 350)

    def test_smith_telemetry_does_not_replace_measured_volume_or_score_flow(self):
        row = hardware.HardwareTelemetry.parse(
            "TEL,1,20,3,0,10,123,4.5,0,4.3,1,4.25,0.029,9.55,10,1,4,0,10"
        )
        self.assertEqual(row.pid_feedback_slm, 4)
        self.assertEqual(row.smith_ideal_flow_slm, 0)
        self.assertEqual(row.smith_actuator_flow_slm, 10)
        self.assertEqual(row.control_flow_slm, 10)
        self.assertEqual(row.volume_ml, 123)

    def test_motion_telemetry(self):
        row = hardware.HardwareTelemetry.parse("TEL,1,20,3,0,2,10,3,0.5,4,1,3,0.5")
        self.assertEqual(row.estimated_ppv_v, 3)
        self.assertEqual(row.ppv_remaining_s, 0.5)

    def test_auto_pid_search_is_bounded_and_finite(self):
        tuner = hardware.AutoPIDTuner(0.02, 0.20, rounds=2)
        candidates = []
        while True:
            gains = tuner.next_candidate()
            if gains is None:
                break
            candidates.append(gains)
            tuner.record({"score": gains[0] + gains[1]})

        self.assertEqual(len(candidates), 9)
        self.assertTrue(all(0.005 <= kp <= 0.08 for kp, _ in candidates))
        self.assertTrue(all(0.05 <= ki <= 0.80 for _, ki in candidates))
        self.assertEqual(len(tuner.history), 9)
        self.assertIn(tuner.best_gains, candidates)

    def test_hardware_score_uses_flow_volume_and_saturation(self):
        cfg = sim.SingleLungConfig(target_volume_ml=100.0)
        rows = []
        for i, (flow, volume, opening) in enumerate(
            [(10.0, 0.0, 0.4), (9.0, 50.0, 0.5), (10.0, 100.0, 1.0)]
        ):
            rows.append(hardware.HardwareTelemetry(
                1, i * 20, sim.STATE_INSPIRATION, 0, flow, volume,
                10.0, opening, 5.0 * (1.0 - opening), 1,
            ))
        metrics = hardware.hardware_run_metrics(rows, cfg)
        self.assertGreater(metrics["score"], 0.0)
        self.assertAlmostEqual(metrics["mean_tidal_ml"], 100.0)
        self.assertAlmostEqual(metrics["saturation_fraction"], 1.0 / 3.0)


class HardwareGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        cls.load_patch = patch.object(
            hardware, "load_config", return_value=sim.SingleLungConfig()
        )
        cls.load_patch.start()

    @classmethod
    def tearDownClass(cls):
        cls.load_patch.stop()

    def setUp(self):
        self.window = hardware.MainWindow()

    def tearDown(self):
        self.app.processEvents()
        self.window.close()
        self.app.processEvents()

    def test_user_and_developer_modes_share_settings_and_auto_pid_switch(self):
        self.assertEqual(self.window.mode_stack.count(), 2)
        self.assertEqual(self.window.mode_stack.currentIndex(), 0)
        self.window.user_inputs["volume"].setValue(180.0)
        self.assertNotIn("flow", self.window.user_inputs)
        self.assertTrue(self.window._apply_user_settings())
        self.assertAlmostEqual(self.window.inputs["fill_time"].value(), 1.333, places=3)
        self.assertEqual(self.window.inputs["volume"].value(), 180.0)
        self.assertIn("8.10", self.window.user_derived_flow.text())

        self.window.user_auto_pid.setChecked(True)
        self.assertTrue(self.window.auto_pid_enabled.isChecked())
        self.window.auto_pid_enabled.setChecked(False)
        self.assertFalse(self.window.user_auto_pid.isChecked())

    def test_csv_name_is_available_and_synchronized_in_both_modes(self):
        self.assertEqual(len(self.window.log_name_edits), 2)
        self.window.log_name_edits[0].setText("smith_trial_01.csv")
        self.assertEqual(self.window.log_name_edits[1].text(), "smith_trial_01.csv")
        self.window.log_name_edits[1].setText("developer_trial")
        self.assertEqual(self.window.log_name_edits[0].text(), "developer_trial")

    def test_guide_explains_modes_auto_pid_and_safety(self):
        guide = hardware.HardwareGuideDialog(self.window)
        tabs = guide.findChild(QtWidgets.QTabWidget)
        self.assertEqual(tabs.count(), 5)
        text = "\n".join(tabs.widget(i).toPlainText() for i in range(tabs.count()))
        for topic in ("User Mode", "Developer Mode", "Optional bounded Auto PID",
                      "0.25x to 4x", "not a medical device",
                      "Real delays, not a delay simulation", "τin", "τout",
                      "single_lung_pid_gui.py", "locked by default"):
            self.assertIn(topic, text)
        guide.close()

    def test_measured_model_parameters_are_locked_until_explicitly_unlocked(self):
        protected = ("capacity", "tau_in", "tau_out", "gamma", "smith_delay", "ppv_open_time", "ppv_close_time", "flow_offset", "flow_gain", "vac_leak", "dead")
        self.assertFalse(self.window.remodel_enabled.isChecked())
        self.assertTrue(all(not self.window.inputs[key].isEnabled() for key in protected))
        self.assertTrue(self.window.inputs["kp"].isEnabled())
        self.assertTrue(self.window.inputs["smith_gain"].isEnabled())
        self.window.remodel_enabled.setChecked(True)
        self.assertTrue(all(self.window.inputs[key].isEnabled() for key in protected if key not in ("dead", "smith_delay")))
        self.window.remodel_enabled.setChecked(False)
        self.assertTrue(all(not self.window.inputs[key].isEnabled() for key in protected))

    def test_directional_fields_round_trip_without_expanding_volume_guard(self):
        self.window.remodel_enabled.setChecked(True)
        self.window.inputs["flow_gain"].setValue(1.05)
        self.window.inputs["flow_offset"].setValue(0.004)
        self.window.inputs["vac_leak"].setValue(0.94)
        self.window.inputs["gamma_out"].setValue(1.8)
        self.window.inputs["model_volume"].setValue(1600)
        self.window.inputs["ppv_open_time"].setValue(1.2)
        self.window.inputs["ppv_close_time"].setValue(2.1)
        cfg = self.window.cfg_from_ui()
        self.assertEqual(cfg.lung.hardware_flow_gain, 1.05)
        self.assertEqual(cfg.lung.hardware_flow_offset_slm, 0.004)
        self.assertEqual(cfg.lung.hardware_vac_leak_slm, 0.94)
        self.assertEqual(cfg.smith.ppv_open_time_s, 1.2)
        self.assertEqual(cfg.smith.ppv_close_time_s, 2.1)
        self.assertEqual(cfg.lung.hardware_gamma_out, 1.8)
        self.assertEqual(cfg.lung.hardware_model_volume_ml, 1600)
        self.assertEqual(cfg.max_lung_volume_ml, 250)
        self.window.cfg = cfg
        self.window._sync_developer_from_config()
        self.assertEqual(self.window.inputs["gamma_out"].value(), 1.8)
        self.window.remodel_enabled.setChecked(False)
        self.assertFalse(self.window.inputs["gamma_out"].isEnabled())
        self.assertFalse(self.window.inputs["model_volume"].isEnabled())

    def test_manual_commands_require_idle_connection_and_show_ack(self):
        self.assertFalse(self.window.send_manual_command("MANUAL,ATM,OPEN"))
        serial = Mock()
        self.window.connection = serial
        self.window.firmware_ready = True
        self.assertTrue(self.window.send_manual_command("MANUAL,ATM,OPEN"))
        serial.write.assert_called_once_with(b"MANUAL,ATM,OPEN\n")
        serial.write.reset_mock()
        self.window.hardware_running = True
        self.assertFalse(self.window.send_manual_command("MANUAL,VAC,OPEN"))
        serial.write.assert_not_called()
        self.window.hardware_running = False
        self.window.auto_tune_active = True
        self.assertFalse(self.window.send_manual_command("MANUAL,PPV,OPEN"))
        serial.write.assert_not_called()
        self.window.auto_tune_active = False
        self.window._handle_line("ACK,MANUAL,2,0.000")
        self.assertIn("ATM closed | VAC open", self.window.manual_status.text())
        self.window.connection = None

    def test_phase_positioning_allows_smith_but_disables_fixed_dead_time(self):
        self.assertTrue(self.window.smith_enabled.isEnabled())
        self.window.smith_enabled.setChecked(True)
        cfg = self.window.cfg_from_ui()
        self.assertTrue(cfg.smith.enabled)
        self.assertEqual(cfg.dead_time_s, 0)
        fields = hardware.config_command(cfg, 1).split(",")
        self.assertEqual(fields[1], "8")
        self.assertEqual(fields[8], "0")
        self.assertEqual(fields[21], "1")
        cfg.smith.ppv_close_time_s = 0
        with self.assertRaises(ValueError):
            hardware.config_command(cfg, 1)

    def test_identified_gamma_and_dead_zones_keep_four_decimal_precision(self):
        self.window.remodel_enabled.setChecked(True)
        self.window.inputs["gamma"].setValue(0.7828)
        self.window.inputs["gamma_out"].setValue(0.9143)
        self.window.inputs["dead_zone_in"].setValue(3.7768)
        self.window.inputs["dead_zone_out"].setValue(4.0249)
        cfg = self.window.cfg_from_ui()
        self.assertEqual(cfg.lung.hardware_gamma_in, 0.7828)
        self.assertEqual(cfg.lung.hardware_gamma_out, 0.9143)
        self.assertEqual(cfg.lung.hardware_dead_zone_in_v, 3.7768)
        self.assertEqual(cfg.lung.hardware_dead_zone_out_v, 4.0249)

    def test_dynamic_model_exists_in_both_modes_and_uses_telemetry(self):
        self.assertEqual(
            [self.window.developer_tabs.tabText(i)
             for i in range(self.window.developer_tabs.count())],
            ["Dynamic Model", "Live Hardware"],
        )
        row = hardware.HardwareTelemetry(
            7, 1250, sim.STATE_INSPIRATION, 1, 10.5, 180.0,
            11.5, 0.72, 1.4, 1,
        )
        self.window._draw_dynamic_model(row)
        user_text = "\n".join(text.get_text() for text in self.window.user_model_canvas.ax.texts)
        self.assertIn("V =  180.0 mL", user_text)
        self.assertIn("Q = +10.50 SLM", user_text)

        self.window.rows.append(row)
        self.window.mode_combo.setCurrentIndex(1)
        developer_text = "\n".join(
            text.get_text() for text in self.window.model_canvas.ax.texts
        )
        self.assertIn("State = INSPIRATION", developer_text)
        self.assertEqual(
            self.window.model_canvas.ax.get_title(),
            "Single-Lung Live Hardware Dynamic Model",
        )

    def test_recovery_telemetry_renders_without_crashing(self):
        row = hardware.HardwareTelemetry(
            7, 1250, hardware.STATE_RECOVERY, 1, 4.0, 120.0,
            0.0, 1.0, 0.0, 1,
        )
        self.window._draw_dynamic_model(row)
        text = "\n".join(
            item.get_text() for item in self.window.user_model_canvas.ax.texts
        )
        self.assertIn("State = RECOVERY", text)

    def test_only_single_lung_g474_firmware_enables_commands(self):
        self.assertFalse(self.window.firmware_ready)
        self.window._handle_line("READY,LUNGLOGIC_DEMO_G474,1")
        self.assertFalse(self.window.firmware_ready)
        self.window._handle_line(hardware.READY)
        self.assertTrue(self.window.firmware_ready)


if __name__ == "__main__":
    unittest.main()
