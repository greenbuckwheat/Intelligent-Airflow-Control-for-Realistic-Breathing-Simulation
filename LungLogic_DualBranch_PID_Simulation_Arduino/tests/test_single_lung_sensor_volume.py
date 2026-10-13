"""Meaningful checks for the simulated sensor-derived volume feedback path."""

from pathlib import Path
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from single_lung_pid_gui import SingleLungConfig, run_single_lung_simulation
from single_lung_sensor_feedback_study import compare_grids


class SensorVolumeTests(unittest.TestCase):
    def test_zero_calibration_and_flow_integral_recover_ideal_volume(self):
        cfg = SingleLungConfig(cycles=3)
        cfg.volume_deadline_guard = True
        cfg.dead_time_within_cycle = True
        cfg.volume_feedback_source = "sensor_integral"
        cfg.lung.sensor_bias_slm = 0.25
        cfg.lung.sensor_noise_slm = 0.0
        cfg.lung.transport_delay_s = 0.0
        cfg.lung.sensor_tau_s = 0.001
        result = run_single_lung_simulation(cfg)
        self.assertAlmostEqual(result.estimated_sensor_bias_slm, 0.25, places=8)
        self.assertLess(np.max(np.abs(result.estimated_volume_ml - result.volume_ml)), 0.01)

    def test_delayed_sensor_changes_commands_and_exposes_estimation_error(self):
        cfg = SingleLungConfig(cycles=3)
        cfg.volume_deadline_guard = True
        cfg.dead_time_within_cycle = True
        cfg.lung.sensor_noise_slm = 0.0
        cfg.lung.transport_delay_s = 0.10
        cfg.lung.sensor_tau_s = 0.08
        cfg.input_delay.enabled = True
        cfg.volume_feedback_source = "sensor_integral"
        sensor_result = run_single_lung_simulation(cfg)
        cfg.volume_feedback_source = "oracle"
        oracle_result = run_single_lung_simulation(cfg)
        self.assertGreater(
            np.max(np.abs(sensor_result.estimated_volume_ml - sensor_result.volume_ml)), 10.0
        )
        self.assertGreater(np.max(np.abs(sensor_result.opening - oracle_result.opening)), 0.03)

    def test_paired_scan_changes_only_controller_information_mode(self):
        cfg = SingleLungConfig()
        oracle, sensor, paired = compare_grids(
            cfg, rates=(15.0,), fill_shapes=(0.57,),
            input_delays=(0.025,), expiration_taus=(0.50,),
        )
        self.assertEqual(len(paired), 1)
        self.assertEqual(oracle[0]["volume_feedback_source"], "oracle")
        self.assertEqual(sensor[0]["volume_feedback_source"], "sensor_integral")
        self.assertAlmostEqual(
            paired[0]["sensor_minus_oracle_minimum_tidal_ml"],
            sensor[0]["minimum_tidal_ml"] - oracle[0]["minimum_tidal_ml"],
        )

    def test_assumed_input_delay_is_independent_of_plant_input_delay(self):
        cfg = SingleLungConfig(cycles=2)
        cfg.lung.sensor_noise_slm = 0.0
        cfg.volume_deadline_guard = True
        cfg.volume_feedback_source = "sensor_integral"
        cfg.input_delay.enabled = False
        immediate_assumption = run_single_lung_simulation(cfg, controller_input_delay_s=0.0)
        delayed_assumption = run_single_lung_simulation(cfg, controller_input_delay_s=0.08)
        self.assertGreater(
            np.max(np.abs(immediate_assumption.opening - delayed_assumption.opening)), 0.01
        )


if __name__ == "__main__":
    unittest.main()
