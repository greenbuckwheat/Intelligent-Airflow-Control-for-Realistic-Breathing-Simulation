"""Checks that hypothetical scans separate plant limits from control misses."""

from pathlib import Path
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from single_lung_pid_gui import SingleLungConfig
from single_lung_requirement_benchmark import evaluate
from single_lung_uncertainty_sweep import installed_path_limits, run_sweep


class ModelUncertaintySweepTests(unittest.TestCase):
    def test_30_cycle_scan_classifies_baseline_without_changing_saved_settings(self):
        cfg = SingleLungConfig()
        cfg.target_expiration_flow_slm = 5.799  # Invalid at 18 bpm if copied unchanged.
        cfg.input_delay.enabled = False
        rows = run_sweep(
            cfg, rates=(15.0, 16.0, 18.0), fill_shapes=(0.57,),
            input_delays=(0.025,), expiration_taus=(0.50,),
        )
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["status"], "simulated_pass")
        self.assertEqual(rows[1]["status"], "controlled_fail")
        self.assertEqual(rows[2]["status"], "model_bound_fail")
        self.assertEqual(rows[0]["cycles"], 30)
        self.assertEqual(cfg.target_expiration_flow_slm, 5.799)
        self.assertFalse(cfg.input_delay.enabled)

    def test_hypothetical_slower_installed_path_cannot_increase_full_open_limit(self):
        cfg = SingleLungConfig()
        rows = run_sweep(
            cfg, rates=(15.0,), fill_shapes=(0.57,),
            input_delays=(0.025,), expiration_taus=(0.50,),
        )
        limits = installed_path_limits(rows, cfg, np.array([1.30, 1.60]))
        for shape in (0.35, 0.57, 0.85):
            values = [row["full_open_tidal_limit_from_zero_ml"] for row in limits
                      if row["assumed_fill_shape_tau_s"] == shape]
            self.assertEqual(len(values), 2)
            self.assertGreaterEqual(values[0], values[1])

    def test_fixed_controller_model_exposes_plant_model_mismatch(self):
        plant = SingleLungConfig()
        plant.lung.tau_in_s = 0.85
        plant.lung.tau_out_s = 1.0
        plant.input_delay.enabled = True
        plant.input_delay.delay_s = 0.06
        nominal_model = SingleLungConfig().lung
        matched = evaluate(plant, 15.0, True)
        mismatched = evaluate(
            plant, 15.0, True, controller_lung=nominal_model,
            controller_input_delay_s=0.025,
        )
        self.assertGreater(
            abs(matched["minimum_tidal_ml"] - mismatched["minimum_tidal_ml"]), 1.0
        )


if __name__ == "__main__":
    unittest.main()
