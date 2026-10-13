"""Model-only checks for the 30-cycle single-branch prototype target."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from single_lung_pid_gui import SingleLungConfig
from single_lung_requirement_benchmark import evaluate


class PrototypeRequirementTests(unittest.TestCase):
    def setUp(self):
        self.cfg = SingleLungConfig()
        self.cfg.input_delay.enabled = True

    def test_nominal_15_bpm_simulated_30_cycle_tolerance_and_cadence(self):
        row = evaluate(self.cfg, 15.0, True)
        self.assertEqual(row["cycles"], 30)
        self.assertTrue(row["tidal_requirement_pass"])
        self.assertTrue(row["rate_requirement_pass"])
        self.assertGreaterEqual(row["minimum_tidal_ml"], 237.5)
        self.assertAlmostEqual(row["actual_bpm"], 15.0, places=2)

    def test_18_bpm_limit_is_reported_as_model_specific_failure(self):
        row = evaluate(self.cfg, 18.0, True)
        self.assertTrue(row["rate_requirement_pass"])
        self.assertFalse(row["tidal_requirement_pass"])
        self.assertFalse(row["model_upper_bound_meets_5pct"])
        self.assertLess(row["ideal_full_open_peak_from_zero_ml"], 237.5)


if __name__ == "__main__":
    unittest.main()
