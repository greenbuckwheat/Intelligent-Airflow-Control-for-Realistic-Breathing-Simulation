"""Checks the optimization study evaluates closed-loop breaths consistently."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from single_lung_pid_gui import SingleLungConfig
from single_lung_pid_es_study import MAX_GAIN_FACTOR, MIN_GAIN_FACTOR, metrics, optimize


class PIDESStudyTests(unittest.TestCase):
    def test_nominal_scoring_is_reproducible_and_flags_saturation(self):
        cfg = SingleLungConfig()
        gains = (cfg.pi.kp, cfg.pi.ki)
        first = metrics(cfg, 15.0, gains, guard=True)
        second = metrics(cfg, 15.0, gains, guard=True)
        self.assertEqual(first, second)
        self.assertGreater(first["inspiration_saturation_fraction"], 0.9)
        self.assertEqual(first["tidal_pass"], first["worst_volume_error_pct"] <= 5.0
                         and first["cycle_spread_pct"] <= 5.0)

    def test_search_keeps_gains_bounded_and_does_not_mutate_config(self):
        cfg = SingleLungConfig()
        original = (cfg.pi.kp, cfg.pi.ki)
        selected, history = optimize(cfg, iterations=1)
        self.assertGreaterEqual(len(history), 6)
        for chosen, starting in zip(selected, original):
            self.assertGreaterEqual(chosen, starting * MIN_GAIN_FACTOR)
            self.assertLessEqual(chosen, starting * MAX_GAIN_FACTOR)
        self.assertEqual((cfg.pi.kp, cfg.pi.ki), original)
        self.assertLessEqual(min(row["score"] for row in history), history[0]["score"])


if __name__ == "__main__":
    unittest.main()
