"""Offline checks for the single-lung hardware GUI and host-side Auto PID."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# Never write logs or reports into the real output folder.
import tempfile
os.environ.setdefault("LUNGLOGIC_OUTPUT_DIR", tempfile.mkdtemp(prefix="lunglogic_test_output_"))

from pathlib import Path
import sys
import unittest
import csv
import json
import tempfile
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PyQt5 import QtWidgets
import single_lung_hardware_gui as hardware
import single_lung_pid_gui as sim


def scored_cycle(cycle=0, offset=0, flow_in=6.0, flow_out=-6.0):
    # 1 second at +6 SLM = 100 mL. Three inspiration samples versus one
    # expiration sample intentionally test equal phase weighting.
    return [hardware.HardwareTelemetry(
        1, offset + t, state, cycle + advance, flow, 0.0, ref, opening, 4.0, 0,
        net_flow_slm=flow,
    ) for t, state, flow, ref, opening, advance in (
        (0, 2, 0, 0, 0, 0),
        (20, 3, flow_in, 6, 1, 0),
        (520, 3, flow_in, 6, 0.5, 0),
        (1000, 3, flow_in, 6, 0.5, 0),
        (1020, 4, flow_in, 0, 0, 0),
        (1040, 5, flow_out, -6, 0.5, 0),
        (2040, 2, 0, 0, 0, 1),
    )]


class HardwareLogicTests(unittest.TestCase):
    def test_auto_pid_selects_best_using_full_objective(self):
        tuner=hardware.CoordinateSearchTuner((.01,.04,.002,.004),rounds=1)
        baseline=tuner.next_candidate()
        tuner.record({'score':.1,'J_Q_in':0.,'J_Q_out':0.,'J_V':0.,'J_V_out':0.})
        candidate=tuner.next_candidate()
        # Better single-phase terms must not win against a lower full J.
        tuner.record({'score':.2,'J_Q_in':5.,'J_Q_out':5.,'J_V':5.,'J_V_out':5.})
        self.assertEqual(tuner.best_gains,baseline)
        self.assertNotEqual(baseline,candidate)
        self.assertEqual(tuner.best_score,.1)
        self.assertEqual(tuner.history[-1]['score'],.2)
        self.assertEqual(tuner.history[-1]['kp_in'],candidate[0])
        self.assertEqual(tuner.history[-1]['ki_out'],candidate[3])

    def test_each_round_perturbs_both_phases_then_recenters(self):
        tuner=hardware.CoordinateSearchTuner((.01,.04,.002,.004),rounds=2)
        baseline=tuner.next_candidate()
        tuner.record({'score':1.})
        round_1=[]
        for _ in range(8):
            gains=tuner.next_candidate()
            round_1.append((gains,tuner.current_stage))
            # Make the expiration Ki increase the best candidate of round 1.
            tuner.record({'score':.5 if tuner.current_stage=='round_1_ki_out_plus' else 1.})
        stages=[stage for _,stage in round_1]
        for name in ('kp_in','ki_in','kp_out','ki_out'):
            self.assertIn(f'round_1_{name}_plus',stages)
            self.assertIn(f'round_1_{name}_minus',stages)
        best=dict((stage,gains) for gains,stage in round_1)['round_1_ki_out_plus']
        self.assertEqual(tuner.best_gains,best)
        # Round 2 is centered on the round-1 winner with a halved step.
        first_round_2=tuner.next_candidate()
        self.assertEqual(tuner.current_stage,'round_2_kp_in_plus')
        self.assertEqual(first_round_2[1:],best[1:])
        self.assertAlmostEqual(first_round_2[0],best[0]*hardware.math.exp(.06))
        self.assertNotEqual(best,baseline)

    def test_terminal_expiration_zero_reference_is_scored(self):
        cfg=sim.SingleLungConfig(target_volume_ml=100.0)
        rows=scored_cycle()
        terminal=rows[-2]
        terminal.flow_reference_slm=0
        terminal.net_flow_slm=0
        terminal.opening=0
        terminal.command_v=5
        terminal.expiration_exhausted=True
        # Retain one active-reference sample before the terminal hold.
        active=hardware.copy.deepcopy(terminal)
        active.time_ms=1030
        active.flow_reference_slm=-6
        active.net_flow_slm=-6
        active.expiration_exhausted=False
        rows.insert(-2,active)
        self.assertAlmostEqual(hardware.hardware_run_metrics(rows,cfg)['J_Q'],0)
        terminal.net_flow_slm=-3
        self.assertGreater(hardware.hardware_run_metrics(rows,cfg)['J_Q'],0)

    def test_cycle_budget_telemetry_is_parsed(self):
        row=hardware.HardwareTelemetry.parse('TEL,1,100,5,0,0,10,0,0,5,2,5,0,0,0,2,0,0,0,250,240,250,1')
        self.assertEqual(row.cycle_inspired_ml,250)
        self.assertEqual(row.cycle_expired_ml,240)
        self.assertEqual(row.expiration_budget_ml,250)
        self.assertTrue(row.expiration_exhausted)
    def test_separate_integral_limits_are_serialized_and_validated(self):
        cfg=hardware.load_config(hardware.CONFIG_PATH)
        cfg.pi.integral_limit=9.0
        cfg.hardware_expiration_integral_limit=18.0
        fields=hardware.config_command(cfg,1).split(',')
        self.assertEqual(float(fields[12]),9.0)
        self.assertEqual(float(fields[35]),18.0)
        self.assertEqual([float(v) for v in fields[36:39]],[.01,.12,0.0])
        cfg.hardware_expiration_integral_limit=400.0  # No upper cap.
        self.assertEqual(float(hardware.config_command(cfg,1).split(',')[35]),400.0)
        for value in (0,-1,float('nan')):
            cfg.hardware_expiration_integral_limit=value
            with self.assertRaises(ValueError): hardware.config_command(cfg,1)

    def test_transition_fields_are_serialized_and_validated(self):
        cfg=hardware.load_config(hardware.CONFIG_PATH)
        cfg.hardware_transition_dwell_min_s=0.6
        cfg.hardware_transition_overclose_v=0.1
        cfg.dead_time_within_cycle=True
        cfg.hardware_inspiration_dwell_min_s=0.12
        cfg.hardware_transition_settle_s=0.25
        cfg.hardware_expiration_closure_v=4.35
        fields=hardware.config_command(cfg,1).split(',')
        self.assertEqual([float(fields[40]),float(fields[41]),fields[42],float(fields[43]),
                          float(fields[44]),float(fields[45])],
                         [0.6,0.1,"1",0.12,0.25,4.35])
        for name,bad in (('hardware_transition_dwell_min_s',-0.1),
                         ('hardware_transition_dwell_min_s',float('nan')),
                         ('hardware_transition_overclose_v',-0.01),
                         ('hardware_inspiration_dwell_min_s',-0.01),
                         ('hardware_inspiration_dwell_min_s',float('nan')),
                         ('hardware_transition_settle_s',-0.01),
                         ('hardware_transition_settle_s',float('nan')),
                         ('hardware_expiration_closure_v',4.1),
                         ('hardware_expiration_closure_v',5.01)):
            candidate=hardware.copy.deepcopy(cfg)
            setattr(candidate,name,bad)
            with self.assertRaises(ValueError): hardware.config_command(candidate,1)
        cfg.hardware_expiration_closure_v=4.9
        cfg.hardware_transition_overclose_v=0.2  # 5.1 V target is not writable.
        with self.assertRaises(ValueError): hardware.config_command(cfg,1)

    def test_transition_controls_round_trip_through_developer_ui(self):
        app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        w=hardware.MainWindow(hardware.CONFIG_PATH)
        try:
            self.assertEqual(w.inputs['transition_dwell'].value(),1.0)
            self.assertEqual(w.inputs['transition_settle'].value(),0.0)
            self.assertEqual(w.inputs['exp_closure'].value(),4.32)
            w.inputs['exp_closure'].setValue(4.355)
            self.assertEqual(w.cfg_from_ui().hardware_expiration_closure_v,4.355)
            self.assertEqual(w.inputs['insp_dwell'].value(),0.35)
            w.inputs['transition_dwell'].setValue(0.6)
            w.inputs['overclose'].setValue(0.1)
            w.inputs['insp_dwell'].setValue(0.1)
            w.within_cycle.setChecked(True)
            cfg=w.cfg_from_ui()
            self.assertEqual((cfg.hardware_transition_dwell_min_s,cfg.hardware_transition_overclose_v,
                              cfg.dead_time_within_cycle,cfg.hardware_inspiration_dwell_min_s),
                             (0.6,0.1,True,0.1))
            w.cfg=cfg
            w.within_cycle.setChecked(False)
            w._sync_developer_from_config()
            self.assertTrue(w.within_cycle.isChecked())
        finally:
            w.close()

    def test_named_log_path_defaults_to_output_and_adds_csv_suffix(self):
        path = hardware.resolve_log_path("named_trial", 42)
        self.assertEqual(path, hardware.output_dir() / "named_trial.csv")
        automatic = hardware.resolve_log_path("", 42)
        self.assertEqual(
            automatic,
            hardware.output_dir() / "single_lung_hardware_run_42.csv",
        )
        # Tests are redirected; without the override the folder is output/.
        self.assertNotEqual(hardware.output_dir(), hardware.SCRIPT_DIR / "output")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LUNGLOGIC_OUTPUT_DIR", None)
            self.assertEqual(hardware.output_dir(), hardware.SCRIPT_DIR / "output")
        candidate = hardware.auto_pid_log_path(path, "round_1_kp_plus", 99)
        self.assertEqual(candidate.name, "named_trial_round_1_kp_plus_99.csv")

    def test_calibration_protocol_telemetry_and_validation(self):
        cfg = hardware.load_config(hardware.CONFIG_PATH)
        fields = hardware.config_command(cfg, 1).split(",")
        self.assertEqual([float(x) for x in fields[30:33]], [0.0034, 1.0471, 1.0122])
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
        self.assertEqual(len(fields), 46)
        self.assertEqual(float(fields[39]), 3.0)
        # Reference: protocol 17 transition (1.0 s dwell, target closureV),
        # transitions deducted so cycle = 60/bpm; settle 0 = off.
        self.assertEqual([float(fields[40]), float(fields[41]), fields[42]], [1.0, 0.0, "1"])
        self.assertEqual(float(fields[22]), 0.2)  # Smith residual delay.
        self.assertEqual(float(fields[43]), 0.35)
        self.assertEqual(float(fields[44]), 0.0)
        self.assertEqual(float(fields[45]), 4.32)
        self.assertEqual(fields[:3], ["CFG", "21", "123"])
        self.assertEqual(fields[33:35], ["1", "0"])
        # 0.06 * 8 > 0.4707: the v8 opening cap, not I, bounds expiration.
        self.assertEqual(float(fields[35]), 8.0)
        self.assertEqual(float(fields[6]), 250.0)
        self.assertEqual(float(fields[7]), 3.333)
        self.assertEqual(float(fields[15]), 250.0)
        self.assertEqual(float(fields[17]), 320.3)
        self.assertEqual([float(v) for v in fields[18:20]], [0.5853, 0.2549])
        self.assertEqual(float(fields[20]), 1.7861)
        self.assertEqual(float(fields[24]), 1.9915)
        self.assertEqual([float(x) for x in fields[25:28]], [4.1200, 4.1535, 4.30])
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
        # Independent afternoon high-voltage medians, plus inherited full-open tau.
        # The earlier 4.20 V batch is incompatible and is not a validation target.
        for voltage, tau_in, tau_out in ((0.0, .5853, .2549), (4.2, 1.7426, .5736),
                                         (4.22, 2.3873, .8202), (4.23, 3.1386, 1.0660),
                                         (4.245, 4.6518, 1.7693), (4.251, 6.3208, 2.2903)):
            for measured, tau0, ud, gamma in (
                (tau_in, p.tau_in_s, p.hardware_dead_zone_in_v, p.hardware_gamma_in),
                (tau_out, p.tau_out_s, p.hardware_dead_zone_out_v, p.hardware_gamma_out),
            ):
                a = min(1.0, (p.hardware_closure_v-voltage)/(p.hardware_closure_v-ud))
                predicted = tau0 / a**gamma
                self.assertLess(abs(predicted/measured-1), .065)
        self.assertEqual(p.hardware_model_volume_ml, 320.3)

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
        tuner = hardware.CoordinateSearchTuner((0.02, 0.20, 0.002, 2.0), rounds=2)
        candidates = []
        while True:
            gains = tuner.next_candidate()
            if gains is None:
                break
            candidates.append(gains)
            tuner.record({"score": sum(gains)})

        self.assertEqual(len(candidates), 17)
        self.assertEqual(tuner.total_candidates, 17)
        self.assertTrue(all(0.005 <= g[0] <= 0.08 for g in candidates))
        self.assertTrue(all(0.05 <= g[1] <= 0.80 for g in candidates))
        self.assertTrue(all(0.0005 <= g[2] <= 0.008 for g in candidates))
        # 4x would exceed the firmware/spin-box Ki cap of 5.
        self.assertTrue(all(0.5 <= g[3] <= 5.0 for g in candidates))
        self.assertEqual(len(tuner.history), 17)
        self.assertIn(tuner.best_gains, candidates)

    def test_selected_objective_exact_value_and_equal_phase_weight(self):
        cfg = sim.SingleLungConfig(target_volume_ml=100.0)
        rows = scored_cycle(flow_out=6.0)
        metrics = hardware.hardware_run_metrics(rows, cfg)
        self.assertAlmostEqual(metrics["score"], 1.3)
        self.assertAlmostEqual(metrics["J_Q"], 2.0)
        self.assertAlmostEqual(metrics["J_V"], 0.0)
        self.assertAlmostEqual(metrics["mean_tidal_ml"], 100.0)
        self.assertAlmostEqual(metrics["saturation_fraction"], 0.25)
        self.assertEqual((metrics["N_in"], metrics["N_out"], metrics["N_c"]), (3, 1, 1))

    def test_per_cycle_squared_volume_error_does_not_cancel(self):
        cfg = sim.SingleLungConfig(target_volume_ml=100.0)
        rows = scored_cycle(flow_in=3.0) + scored_cycle(1, 3000, flow_in=9.0)
        for row in rows:
            if row.state == 3:
                row.flow_reference_slm = row.net_flow_slm
        metrics = hardware.hardware_run_metrics(rows, cfg)
        self.assertAlmostEqual(metrics["J_Q"], 0)
        self.assertAlmostEqual(metrics["J_V"], 0.25)
        self.assertAlmostEqual(metrics["score"], 0.0875)
        self.assertAlmostEqual(metrics["cycle_0_inspired_ml"], 50)
        self.assertAlmostEqual(metrics["cycle_1_inspired_ml"], 150)

    def test_saturation_is_diagnostic_and_net_flow_is_scored(self):
        cfg = sim.SingleLungConfig(target_volume_ml=100.0)
        rows = scored_cycle()
        for row in rows:
            row.flow_slm = 999
            row.pid_feedback_slm = -999
            row.opening = 1
        metrics = hardware.hardware_run_metrics(rows, cfg)
        self.assertAlmostEqual(metrics["score"], 0)
        self.assertEqual(metrics["saturation_fraction"], 1)

    def test_incomplete_and_invalid_telemetry_cannot_win(self):
        cfg = sim.SingleLungConfig(target_volume_ml=100.0, cycles=1)
        with self.assertRaises(ValueError):
            hardware.hardware_run_metrics(scored_cycle()[:-1], cfg)
        self.assertEqual(hardware.hardware_run_metrics(scored_cycle()[:-1], cfg, run_completed=True)["N_c"], 1)
        for field, value in (("net_flow_slm", float("nan")), ("flow_reference_slm", 0),
                             ("flow_reference_slm", -6), ("time_ms", 0)):
            rows = scored_cycle()
            setattr(rows[1], field, value)
            with self.assertRaises(ValueError):
                hardware.hardware_run_metrics(rows, cfg)
        tuner = hardware.CoordinateSearchTuner((0.01, 0.06, 0.002, 0.004))
        tuner.next_candidate()
        with self.assertRaises(ValueError):
            tuner.record({"score": float("nan")})
        self.assertEqual(tuner.history, [])

    def test_partial_cycle_is_excluded(self):
        cfg = sim.SingleLungConfig(target_volume_ml=100.0)
        rows = scored_cycle() + scored_cycle(1, 3000, flow_in=99)[:-1]
        metrics = hardware.hardware_run_metrics(rows, cfg)
        self.assertEqual(metrics["N_c"], 1)
        self.assertEqual(metrics["score"], 0)


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

    def test_inspiration_minimum_voltage_is_developer_adjustable(self):
        w=self.window
        self.assertEqual(w.inputs['insp_min_v'].value(),3.0)  # Default cap.
        self.assertTrue(w.inputs['insp_min_v'].isEnabled())
        w.inputs['insp_min_v'].setValue(0.0)  # Allow full open.
        cfg=w.cfg_from_ui()
        self.assertEqual(cfg.hardware_inspiration_min_v,0.0)
        self.assertEqual(float(hardware.config_command(cfg,1).split(',')[39]),0.0)
        w.cfg=cfg
        w._sync_developer_from_config()
        self.assertEqual(w.inputs['insp_min_v'].value(),0.0)
        cfg.hardware_inspiration_min_v=cfg.lung.hardware_closure_v-0.05  # Above old 4.0 V cap: accepted.
        hardware.config_command(cfg,1)
        for bad in (-0.1,cfg.lung.hardware_closure_v,float('nan')):
            cfg.hardware_inspiration_min_v=bad
            with self.assertRaises(ValueError): hardware.config_command(cfg,1)

    def test_phase_integral_limits_survive_ui_config_sync(self):
        w=self.window
        w.inputs['ilim'].setValue(9)
        w.inputs['ilim_out'].setValue(18)
        cfg=w.cfg_from_ui()
        self.assertEqual(cfg.pi.integral_limit,9)
        self.assertEqual(cfg.hardware_expiration_integral_limit,18)
        w.cfg=cfg
        w._sync_developer_from_config()
        self.assertEqual(w.inputs['ilim_out'].value(),18)

    def test_auto_pid_candidates_and_result_cover_both_phase_gains(self):
        w=self.window
        w.auto_base_cfg=hardware.copy.deepcopy(w.cfg)
        w.auto_base_cfg.pi.integral_limit=9
        w.auto_base_cfg.hardware_expiration_integral_limit=18
        w.auto_tuner=hardware.CoordinateSearchTuner((.02,.03,.002,.004),rounds=1)
        w.auto_tuner.next_candidate()
        w.auto_tuner.record({'score':1})
        w.auto_tune_active=True
        w._launch_run=Mock(return_value=True)
        w._start_next_auto_candidate()
        candidate=w._launch_run.call_args.args[0]
        self.assertGreater(candidate.pi.kp,.02)
        self.assertEqual((candidate.pi.ki,candidate.hardware_expiration_kp,
                          candidate.hardware_expiration_ki),(.03,.002,.004))
        # Separate phase integral limits are not tuned.
        self.assertEqual((candidate.pi.integral_limit,
                          candidate.hardware_expiration_integral_limit),(9,18))
        w.auto_tuner.best_gains=(.025,.035,.003,.005)
        w._save_auto_pid_report=Mock(return_value=Path('auto.csv'))
        w._finish_auto_pid()
        self.assertEqual((w.cfg.pi.kp,w.cfg.pi.ki),(.025,.035))
        self.assertEqual((w.cfg.hardware_expiration_kp,w.cfg.hardware_expiration_ki),(.003,.005))
        self.assertEqual((w.inputs['kp'].value(),w.inputs['ki'].value(),
                          w.inputs['kp_out'].value(),w.inputs['ki_out'].value()),
                         (.025,.035,.003,.005))

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

    def test_auto_pid_start_requires_confirmation(self):
        w=self.window
        w.auto_method.setCurrentIndex(1)  # Coordinate search.
        w.connection=Mock(); w.firmware_ready=True
        w.user_inputs['cycles'].setValue(3)
        w.user_auto_pid.setChecked(True)
        Box=QtWidgets.QMessageBox
        with patch.object(Box,'question',return_value=Box.No) as ask:
            w.start_run()
        self.assertIn('17 hardware runs',ask.call_args.args[2])
        w.connection.write.assert_not_called()
        self.assertFalse(w.auto_tune_active)
        self.assertIsNone(w.auto_tuner)
        with patch.object(Box,'question',return_value=Box.Yes):
            w.start_run()
        sent=[c.args[0] for c in w.connection.write.call_args_list]
        self.assertTrue(sent[0].startswith(b'CFG,') and sent[1].startswith(b'START,'))
        self.assertTrue(w.auto_tune_active)
        w._cancel_auto_pid('test')

    def test_auto_pid_rounds_and_plan_shown_in_both_modes(self):
        w=self.window
        w.auto_method.setCurrentIndex(1)  # Coordinate search.
        self.assertIn('Up to 17 runs',w.user_auto_plan.text())
        w.user_auto_rounds.setValue(1)
        self.assertEqual(w.inputs['auto_rounds'].value(),1)
        w.user_inputs['cycles'].setValue(6)
        self.assertIn('Up to 9 runs x 6 cycles',w.user_auto_plan.text())
        self.assertEqual(w.user_auto_plan.text(),w.auto_pid_plan.text())
        w.inputs['auto_rounds'].setValue(3)
        self.assertEqual(w.user_auto_rounds.value(),3)
        self.assertIn('Up to 25 runs',w.auto_pid_plan.text())
        w.user_inputs['cycles'].setValue(2)
        self.assertIn('at least 3 cycles',w.user_auto_plan.text())
        # The rounds chosen in User Mode are what Start uses.
        w.user_inputs['cycles'].setValue(3); w.user_auto_rounds.setValue(1)
        w.connection=Mock(); w.firmware_ready=True; w.user_auto_pid.setChecked(True)
        with patch.object(QtWidgets.QMessageBox,'question',return_value=QtWidgets.QMessageBox.Yes) as ask:
            w.start_run()
        self.assertIn('9 hardware runs',ask.call_args.args[2])
        w._cancel_auto_pid('test')

    def test_normal_start_is_not_asked(self):
        w=self.window
        w.connection=Mock(); w.firmware_ready=True
        with patch.object(QtWidgets.QMessageBox,'question') as ask:
            w.start_run()
        ask.assert_not_called()
        self.assertTrue(w.connection.write.call_args_list[-1].args[0].startswith(b'START,'))

    def test_auto_pid_run_estimate(self):
        cfg=sim.SingleLungConfig(respiratory_rate_bpm=10,cycles=6,precondition_s=2.0,dead_time_s=0.0)
        cfg.hardware_transition_dwell_min_s=1.0
        # Both dwells lengthen the cycle: 1.0 s before VAC, 0.35 s before ATM.
        self.assertAlmostEqual(hardware.auto_pid_run_estimate_s(cfg),2+2.5+6*7.35+5)
        cfg.dead_time_within_cycle=True
        self.assertAlmostEqual(hardware.auto_pid_run_estimate_s(cfg),2+2.5+6*6+5)

    def test_expiration_ff_forced_off_across_modes_config_sync_and_serialization(self):
        w = self.window
        self.assertTrue(w.ff_inspiration_enabled.isChecked())
        self.assertFalse(w.ff_expiration_enabled.isChecked())
        self.assertFalse(w.ff_expiration_enabled.isEnabled())
        for inspiration, expiration in ((False, True), (True, False), (False, False), (True, True)):
            with self.subTest(inspiration=inspiration, expiration=expiration):
                w.mode_combo.setCurrentIndex(1)
                w.ff_inspiration_enabled.setChecked(inspiration)
                w.ff_expiration_enabled.setChecked(expiration)
                w.mode_combo.setCurrentIndex(0)
                self.assertTrue(w._apply_user_settings())
                fields = hardware.config_command(w.cfg, 123).split(",")
                self.assertEqual(fields[33:35], [str(int(inspiration)), "0"])
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "ff.json"
                    sim.save_config(w.cfg, path)
                    # The GUI suite mocks hardware.load_config, not sim.load_config.
                    restored = sim.load_config(path)
                self.assertEqual(restored.hardware_ff_inspiration_enabled, inspiration)
                self.assertFalse(restored.hardware_ff_expiration_enabled)
                w.ff_inspiration_enabled.setChecked(not inspiration)
                w.ff_expiration_enabled.setChecked(not expiration)
                w._sync_developer_from_config()
                self.assertEqual(w.ff_inspiration_enabled.isChecked(), inspiration)
                self.assertFalse(w.ff_expiration_enabled.isChecked())

    def test_csv_name_is_available_and_synchronized_in_both_modes(self):
        self.assertEqual(len(self.window.log_name_edits), 2)
        self.window.log_name_edits[0].setText("smith_trial_01.csv")
        self.assertEqual(self.window.log_name_edits[1].text(), "smith_trial_01.csv")
        self.window.log_name_edits[1].setText("developer_trial")
        self.assertEqual(self.window.log_name_edits[0].text(), "developer_trial")

    def test_saved_files_preserve_run_parameters_after_best_gains_change(self):
        w = self.window
        w.rows = scored_cycle()
        w.run_cfg = sim.SingleLungConfig(target_volume_ml=100, cycles=1)
        w.run_cfg.pi.kp = 0.0123
        w.cfg.pi.kp = 0.9876
        w.run_stage = "baseline"
        w.run_status = "completed"
        w._log("> CFG,test")
        with tempfile.TemporaryDirectory() as directory:
            w.log_path_text = str(Path(directory) / "trial.csv")
            path = w.save_current_log()
            self.assertIn("baseline", path.name)
            metadata = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["configuration"]["pi"]["kp"], 0.0123)
            self.assertEqual(metadata["metrics"]["score"], 0)
            self.assertEqual(metadata["events"][0]["message"], "> CFG,test")
            # Auto PID candidates are saved as compact CSV; the frozen
            # configuration and objective live in the JSON pair.
            self.assertEqual(metadata["csv_layout"], "telemetry_only")
            self.assertIn("formula", metadata["objective"])
            with path.open(encoding="utf-8", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertIn("smith_actuator_flow_slm", rows[0])
            self.assertNotIn("config.pi.kp", rows[0])
            # A manual run keeps the full context columns.
            w.run_stage = None
            w.log_path_text = str(Path(directory) / "manual.csv")
            manual = w.save_current_log()
            with manual.open(encoding="utf-8", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertTrue(all(float(r["config.pi.kp"]) == 0.0123 for r in rows))
            self.assertEqual(float(rows[0]["evaluation.J_Q"]), 0)
            self.assertIn("config.lung.hardware_flow_gain", rows[0])
            self.assertIn("config.smith.ppv_open_time_s", rows[0])
            self.assertIn("formula", json.loads(rows[0]["objective_json"]))
        w.rows = []

    def test_mid_run_save_marks_missing_score_and_keeps_candidate_name(self):
        w = self.window
        w.rows = scored_cycle()[:3]
        w.run_cfg = sim.SingleLungConfig(target_volume_ml=100)
        w.run_stage = "baseline"
        w.run_status = "stopped"
        with tempfile.TemporaryDirectory() as directory:
            w.log_path_text = str(Path(directory) / "partial.csv")
            path = w.save_current_log()
            data = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
            self.assertEqual(data["score_status"], "unavailable")
            self.assertIsNone(data["metrics"])
            self.assertIn("baseline", path.name)
        w.rows = []

    def test_parameters_cannot_be_sent_during_run(self):
        self.window.connection = Mock()
        self.window.firmware_ready = True
        self.window.hardware_running = True
        self.assertFalse(self.window._write_config(self.window.cfg))
        self.window.connection.write.assert_not_called()
        self.window.connection = None

    def test_reusing_filename_preserves_previous_run(self):
        w = self.window
        w.rows = scored_cycle()
        w.run_cfg = sim.SingleLungConfig(target_volume_ml=100, cycles=1)
        w.run_status = "completed"
        with tempfile.TemporaryDirectory() as directory:
            w.log_path_text = str(Path(directory) / "trial.csv")
            original = w.save_current_log()
            for row in w.rows:
                row.run_id = 2
            second = w.save_current_log()
            self.assertNotEqual(original, second)
            self.assertEqual(json.loads(original.with_suffix(".json").read_text())["run_id"], 1)
            self.assertEqual(w.save_current_log(), second)
        w.rows = []

    def test_next_run_name_never_receives_the_previous_run(self):
        # Operator order: run 1 named 8bpm finishes, the name is changed to
        # 10bpm, Start is pressed (which re-saves leftover rows), run 2 ends.
        w = self.window
        w.run_cfg = sim.SingleLungConfig(target_volume_ml=100, cycles=1)
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            w.rows, w.current_run_id, w.run_status = scored_cycle(), 1, "running"
            w._set_log_name(str(out / "8bpm"))
            w._handle_line("DONE,1")
            w._set_log_name(str(out / "10bpm"))
            with patch.object(w, "_write_config", return_value=False):
                self.assertFalse(w._launch_run(w.run_cfg))
            w.rows = scored_cycle()
            for row in w.rows:
                row.run_id = 2
            w.current_run_id, w.run_status = 2, "running"
            w._handle_line("DONE,2")
            w._handle_line("STOPPED,2")  # A later automatic save keeps the pair.
            saved = sorted(f.name for f in out.glob("*.csv"))
            self.assertEqual(saved, ["10bpm_2.csv", "8bpm_1.csv"])
            for name, run_id in (("8bpm_1", 1), ("10bpm_2", 2)):
                data = json.loads((out / f"{name}.json").read_text(encoding="utf-8"))
                self.assertEqual(data["run_id"], run_id)
        w.rows, w.current_run_id = [], None

    def test_run_id_suffix_is_added_once_and_save_button_works(self):
        w = self.window
        w.rows = scored_cycle()
        w.run_cfg = sim.SingleLungConfig(target_volume_ml=100, cycles=1)
        w.run_status = "completed"
        with tempfile.TemporaryDirectory() as directory:
            w._set_log_name(str(Path(directory) / "trial_1.csv"))
            self.assertEqual(w.save_current_log().name, "trial_1.csv")
            w._set_log_name(str(Path(directory) / "button"))
            buttons = [b for b in w.findChildren(QtWidgets.QPushButton)
                       if b.text() == "Save Log"]
            self.assertTrue(buttons)
            buttons[0].click()  # clicked(bool) must not reach the path argument.
            self.assertTrue((Path(directory) / "button_1.csv").exists())
        w.rows = []

    def test_firmware_and_gui_agree_on_protocol(self):
        # Protocol 21 shipped with READY bumped but the CFG check still at 20,
        # so every CFG was rejected with ERR,CFG_FORMAT.
        import re
        src = (Path(hardware.__file__).parent / "firmware" / "single_lung_controller_stm32"
               / "single_lung_controller_stm32.ino").read_text(encoding="utf-8")
        version = hardware.PROTOCOL_VERSION
        self.assertIn(f'F("{hardware.READY}")', src)
        check = re.search(r"if \(count != (\d+) \|\| f\[1\]\.toInt\(\) != (\d+)\)", src)
        fields = hardware.config_command(hardware.load_config(hardware.CONFIG_PATH), 1).split(",")
        self.assertEqual((int(check.group(1)), int(check.group(2))), (len(fields), version))
        self.assertEqual(f"{version}", fields[1])

    def test_atm_cut_samples_are_scored_against_zero(self):
        # Inspiration reaches the volume early: ATM cut (mask 1 -> 0) for the
        # rest of the phase while the MCU still transmits +6 SLM.
        def run(after_cut_flow):
            rows = [hardware.HardwareTelemetry(1, t, s, c, f, 0.0, ref, 0.5, 4.0, m, net_flow_slm=f)
                    for t, s, c, f, ref, m in (
                        (0, 2, 0, 0.0, 0, 0), (20, 3, 0, 6.0, 6, 1), (520, 3, 0, 6.0, 6, 1),
                        (1000, 3, 0, after_cut_flow, 6, 0), (1500, 3, 0, after_cut_flow, 6, 0),
                        (1520, 4, 0, 0.0, 0, 0), (1540, 5, 0, -6.0, -6, 2), (2040, 5, 0, -6.0, -6, 2),
                        (2060, 2, 1, 0.0, 0, 0))]
            return hardware.hardware_run_metrics(rows, sim.SingleLungConfig(target_volume_ml=100),
                                                 run_completed=True)
        self.assertAlmostEqual(run(0.0)["J_Q_in"], 0.0)       # Zero flow after the cut is correct.
        self.assertAlmostEqual(run(3.0)["J_Q_in"], 2 * 0.25 / 4)  # Leak after the cut still counts.
        self.assertEqual(hardware.OBJECTIVE["version"], "signed_flow_tidal_mse_v4_atm_cut_zero_ref")

    def test_late_stopped_without_telemetry_saves_nothing(self):
        # After reconnecting, a STOPPED for the previous run arrived with no
        # rows and used to write an empty <name>_0 pair.
        w = self.window
        w.rows, w.current_run_id = [], None
        with tempfile.TemporaryDirectory() as directory:
            w._set_log_name(str(Path(directory) / "late"))
            w._handle_line("STOPPED,123")
            self.assertEqual(list(Path(directory).iterdir()), [])
            self.assertIsNone(w.save_current_log())

    def test_positive_user_fields_cannot_round_to_zero(self):
        # 0.01 minimum with 1 decimal used to round to 0: 0 bpm crashed the timing label.
        w = self.window
        for key in ("rr", "ie_in", "ie_out"):
            w.user_inputs[key].setValue(0)
            self.assertAlmostEqual(w.user_inputs[key].value(), 0.1)
        w._update_user_timing()
        self.assertEqual(hardware.displayable_minimum(0.01, 2), 0.01)
        self.assertEqual(hardware.displayable_minimum(0.0, 1), 0.0)
        w._sync_user_from_config()

    def test_done_records_full_candidate_and_ignores_duplicate(self):
        w = self.window
        w.rows = scored_cycle()
        w.run_cfg = sim.SingleLungConfig(target_volume_ml=100, cycles=1)
        w.run_status = "running"
        w.current_run_id = 1
        w.auto_tune_active = True
        w.auto_tuner = hardware.CoordinateSearchTuner((0.01, 0.06, 0.002, 0.004))
        w.auto_tuner.next_candidate()
        # The synthetic cycle exhales 50 of 100 mL, which the leak-compensated
        # emptying check rejects; this test is about DONE handling only.
        with patch.object(w, "save_current_log", return_value=Path("candidate.csv")), \
             patch.object(w, "_save_auto_pid_report") as report, \
             patch.object(hardware, "auto_pid_health_issues", return_value=[]), \
             patch.object(hardware.QtCore.QTimer, "singleShot") as timer:
            w._handle_line("DONE,1")
            w._handle_line("DONE,1")
        self.assertEqual(len(w.auto_tuner.history), 1)
        self.assertEqual(w.auto_tuner.history[0]["J_Q"], 0)
        self.assertEqual(w.auto_tuner.history[0]["config.target_volume_ml"], 100)
        self.assertEqual(w.auto_tuner.history[0]["log_json"], "candidate.json")
        report.assert_called_once()
        timer.assert_called_once()
        w.auto_tune_active = False
        w.rows = []

    def test_stale_stop_does_not_interrupt_current_run(self):
        w = self.window
        w.current_run_id = 10
        w.hardware_running = True
        w.run_status = "running"
        with patch.object(w, "save_current_log") as save:
            w._handle_line("STOPPED,9")
        self.assertTrue(w.hardware_running)
        self.assertEqual(w.run_status, "running")
        save.assert_not_called()

    def test_start_rejection_clears_running_without_retry(self):
        w = self.window
        w.connection = Mock()
        w.hardware_running = True
        w.run_status = "running"
        w.auto_tune_active = True
        w._handle_line("ERR,CONFIG_REQUIRED")
        self.assertFalse(w.hardware_running)
        self.assertFalse(w.auto_tune_active)
        self.assertEqual(w.run_status, "error")
        self.assertEqual(w.statusBar().currentMessage(), "ERR,CONFIG_REQUIRED")
        self.assertEqual(w.manual_status.text(), "ERR,CONFIG_REQUIRED")
        w.connection.write.assert_not_called()
        w.connection = None

    def test_auto_candidate_uses_frozen_experiment_settings(self):
        w = self.window
        w.auto_tune_active = True
        w.auto_tuner = hardware.CoordinateSearchTuner((0.01, 0.06, 0.002, 0.004))
        w.auto_base_cfg = sim.SingleLungConfig(target_volume_ml=100, cycles=3)
        w.inputs["volume"].setValue(200)
        with patch.object(w, "_launch_run", return_value=True) as launch:
            w._start_next_auto_candidate()
        self.assertEqual(launch.call_args.args[0].target_volume_ml, 100)
        w.auto_tune_active = False

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
        self.assertEqual(fields[1], "21")
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

    def test_live_telemetry_updates_table_and_metrics_with_phase_units(self):
        for state, opening, expected in (
            (hardware.STATE_INSPIRATION, 0.5, "50.0 %"),
            (hardware.STATE_EXPIRATION, 0.1883, "18.8 %"),
            (hardware.STATE_RECOVERY, 1.0, "100.0 %"),
        ):
            with self.subTest(state=state):
                self.window._handle_line(
                    f"TEL,7,1250,{state},1,4.0,120.0,0.0,{opening},4.35,2"
                )
                i = self.window.telemetry_table.rowCount() - 1
                self.assertEqual(self.window.telemetry_table.item(i, 5).text(), expected)
                self.assertEqual(self.window.metrics["opening"].text(), expected)
                self.assertEqual(self.window.user_metrics["opening"].text(), expected)

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
        self.window._handle_line("READY,LUNGLOGIC_SINGLE_G474,8")
        self.assertFalse(self.window.firmware_ready)
        self.window._handle_line(hardware.READY)
        self.assertTrue(self.window.firmware_ready)


if __name__ == "__main__":
    unittest.main()
