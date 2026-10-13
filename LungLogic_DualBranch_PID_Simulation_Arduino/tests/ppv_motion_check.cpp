#include <assert.h>
#include <math.h>
#include <initializer_list>
#include "../firmware/single_lung_controller_stm32/ppv_motion.h"

static bool near(float a, float b) { return fabsf(a - b) < 1e-5f; }
int main() {
  // Zero output must be continuous with tiny positive openings, in both routes.
  for (float deadZone : {4.15f, 4.08f}) {
    assert(near(ppvCommandVoltage(0, deadZone, 4.35f), 4.35f));
    assert(near(ppvCommandVoltage(-1, deadZone, 4.35f), 4.35f));
    assert(near(ppvCommandVoltage(0.000001f, deadZone, 4.35f), 4.35f));
    assert(near(ppvCommandVoltage(1, deadZone, 4.3f), 0));
  }
  // Closing anticipation lowers positive inspiratory feedback, while opening
  // anticipation raises it. Signed expiration must work in the opposite direction.
  assert(near(ppvSmithFeedback(10, 0, 10, .6f), 4));
  assert(near(ppvSmithFeedback(0, 10, 0, .6f), 6));
  assert(near(ppvSmithFeedback(-10, 0, -10, .6f), -4));
  assert(near(ppvSmithFeedback(7, 4, 4, .6f), 7));
  assert(near(ppvSmithFeedback(7, 0, 10, 0), 7));
  // October 12 directional fits: identical voltage is not identical conductance.
  const float ain = ppvEffectiveOpening(4.2f, 4.0986f, 4.35f);
  const float aout = ppvEffectiveOpening(4.2f, 4.1779f, 4.35f);
  assert(powf(ain, 1.496f) > .45f && powf(ain, 1.496f) < .47f);
  assert(powf(aout, 2.0621f) > .74f && powf(aout, 2.0621f) < .76f);
  // Recalibration must not tighten the established expiratory voltage envelope.
  for (float v : {4.3176f, 4.2906f, 4.269f}) {
    float limit = ppvEffectiveOpening(v, 4.1779f, 4.35f);
    assert(near(ppvCommandVoltage(limit, 4.1779f, 4.35f), v));
  }
  assert(near(ppvEffectiveOpening(4.35f, 4.15f, 4.35f), 0));
  assert(near(ppvEffectiveOpening(5, 4.08f, 4.35f), 0));
  // 2026-10-09 measured constant-speed 0 <-> 5 V stroke: 2.9 s.
  const float measuredEquivalent = 2.436f;
  assert(near(ppvTravelSeconds(0, 5, measuredEquivalent, measuredEquivalent), 2.9f));
  assert(near(ppvTravelSeconds(5, 0, measuredEquivalent, measuredEquivalent), 2.9f));
  assert(near(ppvTravelSeconds(3, 4, measuredEquivalent, measuredEquivalent), .58f));
  assert(near(ppvAdvance(0, 5, 1.45f, measuredEquivalent, measuredEquivalent), 2.5f));
  assert(near(ppvAdvance(5, 0, 2.9f, measuredEquivalent, measuredEquivalent), 0));
  // 2.1 s closing calibration -> 2 V/s; a 1 V move takes 0.5 s.
  assert(near(ppvTravelSeconds(3, 4, 4.2f, 2.1f), 0.5f));
  float p = ppvAdvance(3, 4, 0.2f, 4.2f, 2.1f);
  assert(near(p, 3.4f));
  assert(near(ppvTravelSeconds(p, 4, 4.2f, 2.1f), 0.3f));
  // Reversal starts from actual estimated position, not previous target 4 V.
  p = ppvAdvance(p, 3, 0.1f, 4.2f, 2.1f);
  assert(near(p, 3.3f));
  assert(near(ppvAdvance(p, 3, 10, 4.2f, 2.1f), 3));
  assert(near(ppvAdvance(0, 4.2f, 2.1f, 4.2f, 2.1f), 4.2f));
  assert(near(ppvAdvance(4.2f, 0, 4.2f, 4.2f, 2.1f), 0));
  assert(near(ppvAdvance(3, 4, 0, 4.2f, 2.1f), 3));
  assert(near(ppvAdvance(3, 3, 1, 4.2f, 2.1f), 3));
  // Full-open flow plateau does not erase travel through that region.
  assert(near(ppvAdvance(0, 4, 1, 4.2f, 2.1f), 2));
  assert(near(ppvEffectiveOpening(2, 3.73f, 4.2f), 1));
  assert(near(ppvEffectiveOpening(5, 3.73f, 4.2f), 0));
  assert(near(ppvEffectiveOpening(4, 3.73f, 4.2f), 0.2f / 0.47f));
  assert(near(ppvEffectiveOpening(4, 3.98f, 4.2f), 0.2f / 0.22f));
  assert(near(ppvAdvance(5, 0, 1, 4.2f, 2.1f), 4));
}
