"""Offline protocol/CSV regression tests; no serial hardware or GUI required."""
import ast
import csv
import io
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import flow_control_test_ui as ui

BASE = 'TEL,120,0.025,0,0,4.300,3522,nan,nan,OK,NONE'
V2 = BASE + ',118,32783,0.125000,0.100000,0,0,4.300'


def window_methods():
    # The application currently defines Window inside main(). Exercise its
    # actual recording methods without starting QApplication/serial polling.
    tree = ast.parse(Path(ui.__file__).read_text(encoding='utf-8'))
    cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == 'Window')
    names = {'csv_fields', 'csv_base_row', 'write_csv_event', 'write_csv_telemetry', 'start_recording'}
    module = ast.Module(body=[n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in names], type_ignores=[])
    scope = dict(vars(ui))
    exec(compile(ast.fix_missing_locations(module), ui.__file__, 'exec'), scope)
    return type('RecordingHarness', (), {n: scope[n] for n in names})


class RecordingTests(unittest.TestCase):
    def setUp(self):
        self.w = window_methods()()
        self.w.latest = ui.Telemetry.parse(V2)
        self.w.csv_file = io.StringIO()
        self.w.writer = csv.DictWriter(self.w.csv_file, fieldnames=self.w.csv_fields())
        self.w.writer.writeheader()

    def test_v2_keeps_raw_offset_and_independent_sample_timestamp(self):
        r = self.w.latest
        self.assertEqual((r.mcu_ms, r.sample_ms, r.sensor_raw), (120, 118, 32783))
        self.assertAlmostEqual(r.uncorrected_flow_slm-r.zero_offset_slm, r.flow_slm)
        self.w.write_csv_telemetry(r)
        row = list(csv.DictReader(io.StringIO(self.w.csv_file.getvalue())))[0]
        self.assertEqual(row['sample_ms'], '118')
        self.assertEqual(row['sensor_raw'], '32783')
        self.assertEqual(row['zero_offset_slm'], '0.1')

    def test_events_do_not_duplicate_samples_and_preserve_ack(self):
        self.w.write_csv_telemetry(self.w.latest)
        self.w.write_csv_event('RX', 'ACK,ZERO_FLOW,DONE,118,0.100000,50,0.020000')
        rows = list(csv.DictReader(io.StringIO(self.w.csv_file.getvalue())))
        self.assertEqual(rows[1]['record_type'], 'event')
        self.assertEqual(rows[1]['flow_slm'], '')
        self.assertEqual(rows[1]['mcu_ms'], '')
        self.assertEqual(rows[1]['sample_ms'], '')
        self.assertTrue(rows[1]['raw_line'].startswith('ACK,ZERO_FLOW,DONE'))

    def test_legacy_does_not_invent_raw_measurements(self):
        self.w.latest = ui.Telemetry.parse(BASE)
        row = self.w.csv_base_row()
        self.assertEqual(row['zero_offset_slm'], '')
        self.assertEqual(row['sensor_raw'], '')

    def test_fault_does_not_export_stale_sample_as_valid(self):
        line = 'TEL,140,nan,0,0,4.300,3522,nan,nan,CRC,NONE,nan,nan,nan,0.100000,0,0,4.300'
        self.w.latest = ui.Telemetry.parse(line)
        row = self.w.csv_base_row()
        self.assertEqual(row['sample_ms'], '')
        self.assertEqual(row['flow_slm'], '')
        self.assertEqual(row['zero_offset_slm'], 0.1)

    def test_new_recording_does_not_overwrite_same_name(self):
        w = self.w
        w.writer = None
        w.filename_edit = Mock()
        w.filename_edit.text.return_value = 'trial.csv'
        w.rows = []
        w.canvas = Mock()
        w.record_label = Mock()
        w.log = Mock()
        stream = io.StringIO()
        with patch.object(Path, 'mkdir'), patch.object(Path, 'open', side_effect=[FileExistsError(), stream]) as opened:
            w.start_recording()
        self.assertEqual(w.record_path.name, 'trial_1.csv')
        self.assertEqual(opened.call_args.args[0], 'x')
        self.assertIn('RECORD_START', stream.getvalue())


if __name__ == '__main__':
    unittest.main()
