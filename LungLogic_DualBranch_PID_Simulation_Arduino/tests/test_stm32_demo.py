"""Offline Qt/serial integration checks; no connected board is needed."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PyQt5 import QtWidgets
import single_lung_stm32_demo_gui as demo


class FakeSerial:
    def __init__(self):
        self.received = bytearray()
        self.sent = []
        self.closed = False

    @property
    def in_waiting(self):
        return len(self.received)

    def read(self, count):
        data = bytes(self.received[:count])
        del self.received[:count]
        return data

    def write(self, data):
        self.sent.append(data)
        return len(data)

    def close(self):
        self.closed = True


class DemoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        cls.save_patch = patch.object(demo.sim, "save_config")
        cls.load_patch = patch.object(demo.sim, "load_config", return_value=demo.sim.SingleLungConfig())
        cls.save_patch.start()
        cls.load_patch.start()
        cls.window = demo.MainWindow()
        cls.window.io_timer.stop()
        cls.window.chart_timer.stop()

    @classmethod
    def tearDownClass(cls):
        cls.window.close()
        cls.save_patch.stop()
        cls.load_patch.stop()

    def setUp(self):
        self.w = self.window
        self.w.stop_playback()
        self.w.disconnect_serial()
        self.w.swap.setChecked(False)
        self.w.play_index = 0
        self.w.display_index = 0
        self.w.samples.clear()
        self.port = FakeSerial()
        self.w.connection = self.port
        self.w.connected_at = self.w.last_rx = time.monotonic()

    def handshake(self):
        self.port.received.extend((demo.READY + "\n").encode())
        self.w._poll_serial()
        self.assertTrue(self.w.ready)

    def test_fragmented_handshake_and_real_reading(self):
        self.port.received.extend(b"READY,LUNGLOGIC_")
        self.w._poll_serial()
        self.assertFalse(self.w.ready)
        self.port.received.extend(b"DEMO_G474,1\nFLOW,100,1.000,OK,1,0,32888\n")
        self.w._poll_serial()
        self.assertTrue(self.w.ready)
        self.assertEqual(self.w.flow_label.text(), "+1.000 SLM")
        self.assertEqual(self.w.user_sensor_flow.text(), "+1.000 SLM")
        self.assertIn("ATM ON", self.w.applied_label.text())
        self.assertEqual(self.port.sent[-1], b"LED,0\n")

    def test_every_simulation_mask_and_pause_stop(self):
        self.handshake()
        self.w.playing = True
        result = self.w.result
        seen = set()
        for i, mask in enumerate(result.valve_mask):
            if int(mask) in seen:
                continue
            seen.add(int(mask))
            self.w._show_index(i)
            self.assertEqual(self.port.sent[-1], f"LED,{mask}\n".encode())
            self.w.swap.setChecked(True)
            self.assertEqual(self.port.sent[-1], f"LED,{demo.output_mask(result, i, True, True)}\n".encode())
            self.w.swap.setChecked(False)
        self.assertEqual(seen, {0, 1, 2})
        self.w.toggle_play()
        self.assertEqual(self.port.sent[-1], b"LED,0\n")
        self.w.stop_playback()
        self.assertEqual(self.port.sent[-1], b"LED,0\n")

    def test_sensor_failure_does_not_replace_simulation(self):
        self.handshake()
        result = self.w.result
        self.w._handle_line("FLOW,200,nan,CRC,2,0,32888")
        self.assertEqual(self.w.flow_label.text(), "— SLM")
        self.assertIs(self.w.result, result)
        self.assertTrue(self.w.ready)
        self.w._draw_real_flow()

    def test_corrupt_reading_does_not_refresh_timeout(self):
        self.handshake()
        stamp = self.w.last_rx
        self.w._handle_line("FLOW,300,nan,OK,0,0,32768")
        self.w._handle_line("FLOW,300,999,OK,0,0,32768")
        self.assertEqual(self.w.last_rx, stamp)
        self.w.last_rx = time.monotonic() - 2
        self.w._poll_serial()
        self.assertIsNone(self.w.connection)
        self.assertTrue(self.port.closed)
        self.assertEqual(self.port.sent[-1], b"STOP\n")

    def test_wrong_firmware_and_heartbeat(self):
        self.w._handle_line("READY,LUNGLOGIC_SINGLE_G474,2")
        self.assertFalse(self.w.ready)
        self.handshake()
        count = len(self.port.sent)
        self.w.last_tx = time.monotonic() - 0.2
        self.w._poll_serial()
        self.assertEqual(len(self.port.sent), count + 1)

    def test_reset_and_completion_close_outputs(self):
        self.handshake()
        self.w.playing = True
        self.w.reset_playback()
        self.assertEqual(self.port.sent[-1], b"LED,0\n")
        self.w.playing = True
        self.w.play_index = self.w.result.time_s.size - 1
        self.w._advance()
        self.assertFalse(self.w.playing)
        self.assertEqual(self.port.sent[-1], b"LED,0\n")


if __name__ == "__main__":
    unittest.main()
