import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lung_calibration_experiments import analyse, make_plan, parse_flow, FIELDS


class CalibrationTests(unittest.TestCase):
    def test_sensor_corruption_is_rejected(self):
        self.assertEqual(
            parse_flow("CAL,7,100,20,4,1.000,OK,1,0.800,1.000"),
            (7, 100, .02, 4, 1., 1, .8, 1., "OK"),
        )
        for line in ("CAL,7,100,20,4,1.000,CRC,1,0.8,1", "CAL,7,100,20,9,1.000,OK,1,0.8,1", "CAL,7,100,20,4,nan,OK,1,0.8,1"):
            with self.assertRaises(ValueError):
                parse_flow(line)

    def test_both_directions_orders_and_repeats(self):
        plan = make_plan("ppv-sweep", [0., .5, 1.], 2)
        self.assertEqual(len(plan), 24)
        self.assertEqual([p[1] for p in plan[:6]], [0., .5, 1., 1., .5, 0.])
        self.assertEqual({p[0] for p in plan}, {"ATM", "VAC"})

    def test_analysis_excludes_aborted_and_keeps_relative_volume(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            with (folder / "measurements.csv").open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=FIELDS)
                writer.writeheader()
                for trial in (1, 2):
                    for i in range(20):
                        writer.writerow({"trial": trial, "source": "Synthetic", "experiment": "ppv-sweep",
                                         "direction": "VAC", "opening_command": .5, "repeat": 1,
                                         "sweep_order": "ascending", "time_s": i * .02,
                                         "flow_slm": -6, "volume_delta_ml": -2 * i})
            (folder / "aborted_trials.txt").write_text("2\n")
            report = json.loads(analyse(folder).read_text())
            self.assertEqual(len(report["trials"]), 1)
            self.assertEqual(report["trials"][0]["signed_volume_change_ml"], -38)
            self.assertEqual(report["trials"][0]["command_to_flow_delay_s"], 0.0)
            self.assertIn("relative", report["trials"][0]["volume_basis"])
            self.assertTrue((folder / "ppv_sweep.png").exists())


if __name__ == "__main__":
    unittest.main()
