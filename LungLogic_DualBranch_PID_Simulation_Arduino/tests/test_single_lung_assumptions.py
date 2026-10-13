"""Checks for the explicitly hypothetical finite-fill model."""

from dataclasses import asdict
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import mock_open, patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from single_lung_assumption_study import full_open_trace, run_study
from single_lung_pid_gui import SingleLungConfig, SmithPredictor, load_config


class FiniteFillAssumptionTests(unittest.TestCase):
    def test_different_curves_share_endpoint_and_conserve_volume(self):
        initial_flows = []
        for tau in (0.35, 0.57, 0.85):
            cfg = SingleLungConfig()
            cfg.lung.tau_in_s = tau
            cfg.lung.sensor_noise_slm = 0.0
            time_s, volume_ml, flow_slm = full_open_trace(cfg)
            reached = np.flatnonzero(volume_ml >= cfg.lung.capacity_ml - 1e-6)
            self.assertGreater(reached.size, 0)
            self.assertAlmostEqual(time_s[reached[0]], cfg.lung.full_open_fill_time_s, places=8)
            self.assertAlmostEqual(float(np.sum(flow_slm) * cfg.dt / 0.06), cfg.lung.capacity_ml, places=6)
            self.assertTrue(np.all(flow_slm[reached[0] + 1:] == 0.0))
            initial_flows.append(flow_slm[1])
        self.assertGreater(initial_flows[0] - initial_flows[-1], 15.0)

    def test_smith_model_stops_predicting_flow_at_capacity(self):
        cfg = SingleLungConfig()
        smith = SmithPredictor(cfg.lung, 0.0, 0.0)
        estimate = 0.0
        for _ in range(150):
            estimate = smith.update(1.0, 1, 0.0, cfg.dt)
        self.assertAlmostEqual(smith.volume_ml, cfg.lung.capacity_ml, places=6)
        self.assertEqual(estimate, 0.0)

    def test_study_reports_control_sensitivity_for_equal_endpoints(self):
        cfg = SingleLungConfig(cycles=2)
        cfg.pi.kp = 0.01
        cfg.pi.ki = 0.06
        cfg.pi.integral_limit = 0.5
        cfg.smith.enabled = True
        cfg.smith.correction_gain = 0.6
        cfg.input_delay.enabled = True
        rows, _ = run_study(cfg, [0.35, 0.57, 0.85])
        for row in rows:
            self.assertAlmostEqual(row["ideal_reach_s"], 1.3, places=8)
            self.assertGreater(row["tidal_error_ml"], 0.0)
        self.assertGreater(
            rows[-1]["inspiration_saturation_fraction"]
            - rows[0]["inspiration_saturation_fraction"],
            0.05,
        )

    def test_legacy_model_setting_loads_as_assumed_finite_fill(self):
        values = asdict(SingleLungConfig())
        values["lung"]["inspiration_model"] = "asymptotic"
        with patch.object(Path, "exists", return_value=True), patch.object(
            Path, "open", mock_open(read_data=json.dumps(values))
        ):
            loaded = load_config(Path("legacy.json"))
        self.assertNotIn("inspiration_model", asdict(loaded)["lung"])
        self.assertGreater(loaded.lung.inspiration_equilibrium_ml(), loaded.lung.capacity_ml)


if __name__ == "__main__":
    unittest.main()
