# Single-lung simulation requirement audit

One prototype branch represents a 250 mL share of the 500 mL dual-branch target. All values below are simulated. A saved manual expiration-flow target is replaced by the phase-derived target in each scenario; the saved GUI configuration is not modified.

| Requested bpm | Control | Actual bpm | Tidal range / mL | Max error / % | 30-cycle pass | Open peak from 0 / mL |
| ---: | --- | ---: | ---: | ---: | --- | ---: |
| 15 | flow_pid | 15.00 | 214.9 to 219.8 | 14.0 | FAIL | 246.8 |
| 12 | volume_deadline_guard | 12.00 | 245.3 to 250.0 | 1.9 | PASS | 250.0 |
| 15 | volume_deadline_guard | 15.00 | 242.6 to 246.3 | 3.0 | PASS | 246.8 |
| 16 | volume_deadline_guard | 16.00 | 237.3 to 240.8 | 5.1 | FAIL | 241.4 |
| 18 | volume_deadline_guard | 18.00 | 227.2 to 231.1 | 9.1 | FAIL | 231.9 |

The model assumes a fully open atmosphere-inspiration path reaches the 250 mL capacity from 0 mL in 1.30 s. The intermediate curve, input delay, valve response, and expiration dynamics are assumptions, not physical calibration. At 18 bpm, the full-open bound follows only from the chosen 0.57 s curve shape and discretized 25 ms input delay. That model's bound is below the 237.5 mL lower tolerance and cannot be corrected by changing PID gains. A different real flow curve could change this conclusion.

This audit does not verify physical pressure/flow acquisition, host command timing, two-branch independent control, or hardware repeatability. The UI playback is precomputed and does not constitute a real-time hardware controller.
