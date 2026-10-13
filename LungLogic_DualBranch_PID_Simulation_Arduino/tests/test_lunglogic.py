import unittest

import numpy as np

from fit_lung_parameters import fit_exponential
from lung_model import STATE_EXPIRATION, STATE_INSPIRATION, STATE_PRECONDITION, ExperimentConfig, run_simulation


class LungLogicTests(unittest.TestCase):
    def test_timing_and_flow_design_point(self):
        cfg = ExperimentConfig()
        self.assertAlmostEqual(cfg.inspiration_s, 4.0 / 3.0, places=6)
        self.assertAlmostEqual(0.06 * cfg.left_target_ml / cfg.target_fill_time_s, 11.53846, places=4)

    def test_simulation_reaches_both_targets_without_interlock_violation(self):
        cfg = ExperimentConfig(cycles=2)
        result = run_simulation(cfg)
        first_inspiration = (result.cycle == 0) & (result.state == STATE_INSPIRATION)
        index = np.flatnonzero(first_inspiration)
        t0 = result.time_s[index[0]]
        for volume, target in ((result.v_left_ml, cfg.left_target_ml), (result.v_right_ml, cfg.right_target_ml)):
            hit = index[volume[index] >= 0.99 * target]
            self.assertGreater(hit.size, 0)
            self.assertLessEqual(result.time_s[hit[0]] - t0, cfg.target_fill_time_s)
        self.assertFalse(np.any((result.valve_mask & 3) == 3))
        self.assertFalse(np.any((result.valve_mask & 12) == 12))
        inspiration = result.state == STATE_INSPIRATION
        expiration = result.state == STATE_EXPIRATION
        precondition = result.state == STATE_PRECONDITION
        self.assertTrue(np.any(result.valve_mask[inspiration] == 5))
        self.assertTrue(np.any(result.valve_mask[expiration] == 10))
        self.assertFalse(np.any(result.valve_mask[inspiration] & 10))
        self.assertFalse(np.any(result.valve_mask[expiration | precondition] & 5))
        transitions = np.flatnonzero(np.diff(result.state) != 0) + 1
        for i in transitions:
            if result.state[i] == 6:  # COMPLETE closes the PPVs.
                continue
            self.assertAlmostEqual(result.opening_left[i], result.opening_left[i - 1])
            self.assertAlmostEqual(result.opening_right[i], result.opening_right[i - 1])
        self.assertLessEqual(np.max(result.v_left_ml), cfg.max_branch_volume_ml)
        self.assertLessEqual(np.max(result.v_right_ml), cfg.max_branch_volume_ml)

    def test_parameter_fit_recovers_tau(self):
        time_s = np.linspace(0.0, 3.0, 151)
        tau = 0.57
        volume = 558.0 * (1.0 - np.exp(-time_s / tau))
        result = fit_exponential(time_s, volume)
        self.assertAlmostEqual(result["tau_s"], tau, delta=0.01)
        self.assertGreater(result["r_squared"], 0.9999)


if __name__ == "__main__":
    unittest.main()
