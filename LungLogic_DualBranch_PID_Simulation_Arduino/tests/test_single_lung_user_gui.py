"""Offline checks for the single-lung user and developer views."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PyQt5 import QtWidgets
import single_lung_pid_gui as sim


class UserGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        cls.save_patch = patch.object(sim, "save_config")
        cls.load_patch = patch.object(sim, "load_config", return_value=sim.SingleLungConfig())
        cls.save_patch.start()
        cls.load_patch.start()

    @classmethod
    def tearDownClass(cls):
        cls.save_patch.stop()
        cls.load_patch.stop()

    def setUp(self):
        self.w = sim.MainWindow()

    def tearDown(self):
        self.app.processEvents()  # Flush queued Matplotlib draws before Qt deletes their canvases.
        self.w.close()
        self.app.processEvents()

    def test_user_flow_settings_reach_simulation_and_developer_view(self):
        self.assertEqual(self.w.mode_stack.currentIndex(), 0)
        self.w.user_inputs["rr"].setValue(12)
        self.w.user_inputs["volume"].setValue(180)
        self.w.user_inputs["in_flow"].setValue(9)
        self.w.user_inputs["out_flow"].setValue(6)
        self.w._apply_user_settings()

        self.assertEqual(self.w.cfg.respiratory_rate_bpm, 12)
        self.assertEqual(self.w.cfg.target_volume_ml, 180)
        self.assertAlmostEqual(self.w.cfg.target_fill_time_s, 1.2, places=3)
        self.assertEqual(self.w.cfg.target_expiration_flow_slm, 6)
        self.assertEqual(sim._phase_reference(self.w.cfg, sim.STATE_EXPIRATION), 6)
        self.assertTrue(any(self.w.result.flow_reference_slm == -6))
        self.assertFalse(self.w.exp_flow_auto.isChecked())
        self.assertIn("mL", self.w.user_readouts["tidal"].text())

        self.w.mode_combo.setCurrentIndex(1)
        self.assertEqual(self.w.mode_stack.currentIndex(), 1)
        self.assertEqual(self.w.inputs["rr"].value(), 12)
        self.assertEqual(self.w.inputs["exp_flow"].value(), 6)
        self.assertEqual(self.w.tabs.count(), 2)
        self.w.mode_combo.setCurrentIndex(0)
        self.assertEqual(self.w.mode_stack.currentIndex(), 0)

    def test_user_apply_uses_last_applied_developer_model(self):
        original_kp = self.w.cfg.pi.kp
        self.w.inputs["kp"].setValue(0.5)  # Left pending in developer mode.
        self.w.user_inputs["volume"].setValue(180)
        self.w._apply_user_settings()
        self.assertEqual(self.w.cfg.pi.kp, original_kp)
        self.assertEqual(self.w.inputs["kp"].value(), original_kp)

    def test_user_can_set_number_of_simulated_cycles(self):
        self.assertEqual(self.w.user_inputs["cycles"].value(), 5)
        self.w.user_inputs["cycles"].setValue(3)
        self.w._apply_user_settings()

        self.assertEqual(self.w.cfg.cycles, 3)
        self.assertEqual(self.w.inputs["cycles"].value(), 3)
        simulated_cycles = set(self.w.result.cycle[self.w.result.state == sim.STATE_INSPIRATION])
        self.assertEqual(simulated_cycles, {0, 1, 2})

        self.w.inputs["cycles"].setValue(4)
        self.w.apply_and_reset()
        self.assertEqual(self.w.user_inputs["cycles"].value(), 4)

    def test_model_canvas_keeps_its_size_when_window_shrinks(self):
        self.w.show()
        self.app.processEvents()
        original = self.w.user_model_canvas.size()
        self.w.resize(900, 650)
        self.app.processEvents()
        self.assertEqual(self.w.user_model_canvas.size(), original)
        self.assertLess(self.w.user_model_view.viewport().width(), original.width())
        self.assertTrue(self.w.user_model_view.horizontalScrollBar().isVisible())
        self.assertEqual(self.w.model_canvas.size(), original)

    def test_developer_guide_covers_model_delays_and_controller_use(self):
        guide = sim.FormulaDialog(self.w)
        tabs = guide.findChild(QtWidgets.QTabWidget)
        self.assertEqual(tabs.count(), 5)
        pages = "\n".join(tabs.widget(i).toPlainText() for i in range(tabs.count()))
        for topic in ("Lung Vmax", "Input delay", "Transport delay",
                      "Model feedforward: when to enable", "Smith predictor: what it changes"):
            self.assertIn(topic, pages)
        guide.close()

    def test_developer_can_apply_finite_fill_assumption(self):
        self.w.inputs["full_open_fill_time"].setValue(1.45)
        self.w.apply_and_reset()
        self.assertTrue(self.w.inputs["full_open_fill_time"].isEnabled())
        self.assertFalse(hasattr(self.w, "inspiration_model_combo"))
        self.assertAlmostEqual(self.w.cfg.lung.full_open_fill_time_s, 1.45)
        self.assertIn("atmosphere path", self.w.user_model_assumption.text())
        self.assertIn("assumed finite fill", self.w.summary.toPlainText())
        self.assertIn("1.45 s", self.w.summary.toPlainText())
        self.assertLessEqual(float(self.w.result.volume_ml.max()), 250.0)

    def test_developer_can_enable_timed_cycle_and_simulation_volume_guard(self):
        self.w.dead_time_within_cycle.setChecked(True)
        self.w.volume_deadline_guard.setChecked(True)
        self.w.apply_and_reset()
        self.assertTrue(self.w.cfg.dead_time_within_cycle)
        self.assertTrue(self.w.cfg.volume_deadline_guard)
        self.assertIn("included in the requested bpm period", self.w.summary.toPlainText())
        self.assertIn("simulation only", self.w.summary.toPlainText())
        self.assertIn("may request more flow", self.w.user_model_assumption.text())
        self.assertIn("fully open", self.w.user_flow_note.text())

    def test_live_curves_follow_a_short_window_with_full_run_option(self):
        self.w.inputs["cycles"].setValue(30)
        self.w.apply_and_reset()
        canvas = self.w.plot_canvas
        self.assertLess(canvas.axes[-1].get_xlim()[1], 15.0)
        canvas.set_time(60.0)
        left, right = canvas.axes[-1].get_xlim()
        self.assertLess(left, 60.0)
        self.assertGreater(right, 60.0)
        self.assertLess(right - left, 15.0)
        canvas.set_overview(True)
        self.assertGreater(canvas.axes[-1].get_xlim()[1], 120.0)

    def test_developer_can_select_sensor_integrated_volume_feedback(self):
        self.w.volume_deadline_guard.setChecked(True)
        self.w.volume_feedback_combo.setCurrentIndex(
            self.w.volume_feedback_combo.findData("sensor_integral")
        )
        self.w.inputs["sensor_bias"].setValue(0.25)
        self.w.apply_and_reset()
        self.assertEqual(self.w.cfg.volume_feedback_source, "sensor_integral")
        self.assertAlmostEqual(self.w.cfg.lung.sensor_bias_slm, 0.25)
        self.assertIn("zero-calibrated", self.w.user_model_assumption.text())
        self.assertGreater(self.w.result.estimated_volume_ml.size, 0)
        self.assertIn(
            "Sensor-integrated estimate",
            [line.get_label() for line in self.w.plot_canvas.axes[0].lines],
        )

    def test_infeasible_inspiration_flow_does_not_apply(self):
        original = self.w.cfg
        self.w.user_inputs["rr"].setValue(30)
        self.w.user_inputs["in_flow"].setValue(1)
        with patch.object(QtWidgets.QMessageBox, "warning") as warning:
            self.w._apply_user_settings()
        warning.assert_called_once()
        self.assertIs(self.w.cfg, original)

    def test_atmosphere_inspiration_and_vacuum_expiration_routing(self):
        result = self.w.result
        inspiration = result.state == sim.STATE_INSPIRATION
        expiration = result.state == sim.STATE_EXPIRATION
        self.assertTrue(any(result.valve_mask[inspiration] == 1))
        self.assertTrue(any(result.valve_mask[expiration] == 2))
        self.assertFalse(any(result.valve_mask[inspiration] & 2))
        self.assertFalse(any(result.valve_mask[expiration] & 1))

    def test_ppv_holds_opening_during_direction_change(self):
        for guard in (False, True):
            cfg = sim.SingleLungConfig(cycles=2, volume_deadline_guard=guard)
            result = sim.run_single_lung_simulation(cfg)
            transitions = [i for i in range(1, result.state.size)
                           if result.state[i] != result.state[i - 1]]
            for i in transitions:
                if result.state[i] == sim.STATE_COMPLETE:
                    self.assertEqual(result.opening[i], 0.0)
                    continue
                self.assertAlmostEqual(result.opening[i], result.opening[i - 1])
                if result.state[i] in (sim.STATE_DEAD_TO_INSP, sim.STATE_DEAD_TO_EXP):
                    self.assertEqual(result.valve_mask[i], 0)


if __name__ == "__main__":
    unittest.main()
