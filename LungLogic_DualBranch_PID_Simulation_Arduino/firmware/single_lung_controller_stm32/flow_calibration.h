#pragma once
#include <math.h>
#include <stdint.h>

static float calibratedFlow(float raw, float offset, float gain, int8_t sign) {
  return sign * gain * (raw - offset);
}

// The October 14 afternoon data give a calibrated VAC plateau near 1.0122 SLM.
// Final-interval linear roll-off is a provisional model convention; precise
// near-closure bypass was not separately identified by the sweep.
static float vacLeakAtVoltage(float plateauLeak, float ppvVolts,
                              float rolloffStartVolts, float closureVolts) {
  if (plateauLeak <= 0.0f || ppvVolts >= closureVolts) return 0.0f;
  if (ppvVolts <= rolloffStartVolts) return plateauLeak;
  if (closureVolts <= rolloffStartVolts) return 0.0f;
  float fraction = (closureVolts - ppvVolts)
                   / (closureVolts - rolloffStartVolts);
  return plateauLeak * fraction;
}

// Net lung-flow estimate. Leakage applies only to active vacuum extraction.
// Limit compensation to measured extraction: never invent positive filling.
static float netLungFlow(float calibrated, uint8_t sampledValveMask, float leak) {
  if (sampledValveMask != 2 || calibrated >= 0.0f) return calibrated;
  float net = calibrated + leak;
  return net < 0.0f ? net : 0.0f;
}
