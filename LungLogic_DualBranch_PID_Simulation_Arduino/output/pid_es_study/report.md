# Offline single-lung PI optimization study

This study implements a bounded, two-sided perturbation search inspired by extremum seeking. It is not the paper's exact discrete ES filter. Tuning uses 30 simulated breaths at 15 bpm with sensor-integrated volume feedback, fixed model feedforward and Smith settings, and **no volume deadline guard**. Kd remains zero. No GUI settings or physical hardware are changed.

The score is 4 x mean relative tidal error + 2 x worst relative tidal error + 2 x relative cycle spread + 0.10 x flow RMSE / 12 SLM + 0.01 x inspiration saturation fraction, with a penalty of 10 x any worst tidal error beyond 5%. These weights are project choices, not values from the paper. The score is dimensionless and lower is better. Acceptance still uses the separate 250 mL +/-5% and spread gates.

Starting gains: Kp=0.01000, Ki=0.06000; selected gains: Kp=0.01198, Ki=0.24000.

| Gains | Guard | Rate | Tidal range / mL | Worst error / % | Spread / % | Flow RMSE / SLM | PPV full-open / % | Pass | Score |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: |
| baseline | off | 12 | 234.3-244.5 | 6.27 | 4.06 | 2.54 | 47 | FAIL | 0.6025 |
| baseline | off | 15 | 212.8-217.8 | 14.88 | 2.00 | 2.17 | 39 | FAIL | 1.9331 |
| baseline | off | 16 | 206.9-211.6 | 17.22 | 1.85 | 2.30 | 39 | FAIL | 2.2976 |
| baseline | on | 12 | 245.5-250.0 | 1.82 | 1.82 | 4.89 | 55 | PASS | 0.1790 |
| baseline | on | 15 | 243.1-246.3 | 2.74 | 1.26 | 6.19 | 100 | PASS | 0.2246 |
| baseline | on | 16 | 237.8-240.8 | 4.88 | 1.19 | 6.32 | 100 | PASS | 0.3578 |
| selected | off | 12 | 235.6-245.8 | 5.76 | 4.10 | 2.58 | 49 | FAIL | 0.5234 |
| selected | off | 15 | 215.2-220.2 | 13.90 | 2.00 | 2.20 | 38 | FAIL | 1.7769 |
| selected | off | 16 | 209.2-214.2 | 16.31 | 1.99 | 2.33 | 37 | FAIL | 2.1496 |
| selected | on | 12 | 245.5-250.0 | 1.80 | 1.80 | 4.88 | 52 | PASS | 0.1775 |
| selected | on | 15 | 243.2-246.3 | 2.73 | 1.24 | 6.19 | 100 | PASS | 0.2247 |
| selected | on | 16 | 237.8-240.8 | 4.86 | 1.17 | 6.32 | 100 | PASS | 0.3575 |

## Hypothetical plant grid, guard off

Each gain set is checked over 27 combinations per rate: tau_in = 0.35/0.57/0.85 s, input delay = 0/25/60 ms, tau_out = 0.30/0.50/1.00 s. The 1.30 s atmosphere-path endpoint is fixed. Counts are illustrative scenarios, not probabilities.

| Gains | Rate | Pass / 27 | Full-open bound fail | Other control fail | Minimum tidal in grid / mL |
| --- | ---: | ---: | ---: | ---: | ---: |
| baseline | 12 | 8 / 27 | 0 | 19 | 143.4 |
| baseline | 15 | 0 / 27 | 0 | 27 | 139.9 |
| baseline | 16 | 0 / 27 | 6 | 21 | 137.9 |
| selected | 12 | 9 / 27 | 0 | 18 | 173.8 |
| selected | 15 | 0 / 27 | 0 | 27 | 165.3 |
| selected | 16 | 0 / 27 | 6 | 21 | 162.3 |

The guard-on comparison above is diagnostic: if it drives the PPV fully open, PID gain changes cannot exceed the full-open plant limit. The sensor-integrated feedback is still simulated and is not a calibrated physical volume measurement. A lower composite score can coexist with worse flow RMSE; inspect each metric before selecting a controller. The guard-on 16 bpm result can also differ from the earlier true-volume-feedback benchmark because feedback source is a separate model assumption. These gains are research candidates, not settings validated for an assembled pneumatic path.
