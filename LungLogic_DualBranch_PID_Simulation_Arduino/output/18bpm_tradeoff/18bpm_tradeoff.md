# 18 bpm single-lung simulation tradeoffs

All rows are 30-cycle **simulations**, using a 250 mL capacity, finite-fill plant, sensor-integrated flow feedback, volume deadline guard, and a single controller model fixed to the saved nominal parameters. A saved manual expiratory flow target is replaced by a rate-derived target. The saved GUI config is not modified.

| What-if change | Target / mL | Active Tin / s | Tidal range / mL | Full-open peak from zero / mL | Inspiration full-open / % | 30-cycle ±5% pass |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| Baseline: 1:2, 250 mL | 250 | 1.031 | 227.7–231.1 | 231.9 | 100 | FAIL |
| I:E 1:1.7 | 250 | 1.155 | 236.4–240.1 | 240.8 | 100 | FAIL |
| I:E 1:1.6 | 250 | 1.202 | 239.2–243.3 | 243.9 | 100 | PASS |
| I:E 1:1.5 | 250 | 1.253 | 241.6–246.3 | 246.8 | 100 | PASS |
| I:E 1:1.4 | 250 | 1.309 | 243.9–249.0 | 249.5 | 100 | PASS |
| I:E 1:1.3 | 250 | 1.369 | 243.1–250.0 | 250.0 | 79 | PASS |
| I:E 1:1.2 | 250 | 1.435 | 242.1–250.0 | 250.0 | 54 | PASS |
| Zero assumed switch dead time | 250 | 1.111 | 233.1–236.6 | 237.3 | 100 | FAIL |
| Zero assumed input delay | 250 | 1.031 | 229.2–232.8 | 233.5 | 100 | FAIL |
| 235 mL target, I:E 1:2 | 235 | 1.031 | 227.7–231.1 | 231.9 | 100 | PASS |
| 1.10 s full-open reach, I:E 1:2 | 250 | 1.031 | 239.4–242.7 | 243.6 | 100 | PASS |
| 1.50 s installed path, I:E 1:1.4 | 250 | 1.309 | 236.1–240.9 | 241.4 | 100 | FAIL |
| 1.50 s installed path, I:E 1:1.2 | 250 | 1.435 | 238.5–246.1 | 247.1 | 55 | PASS |
| 1.80 s installed path, I:E 1:1.2 | 250 | 1.435 | 230.9–238.7 | 239.5 | 56 | FAIL |

The full-open peak is a model-only upper bound from an empty lung; real tidal volume can be lower because expiration leaves residual volume, the controller throttles, and the sensor estimate lags. A case at 100% inspiration opening has no simulated throttling margin. Changing I:E from 1:2 changes the original nominal timing requirement; lowering the target changes the 250 mL requirement. A faster full-open reach time cannot be claimed from the assumed 1.30 s atmosphere-path endpoint. The assembled path, flow waveform, and delays are unmeasured.
