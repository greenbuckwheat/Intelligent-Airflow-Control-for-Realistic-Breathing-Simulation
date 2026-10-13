#pragma once
#include <math.h>

// Normal regulation ends at measured stop-flow voltage, not fault voltage 5 V.
static float ppvCommandVoltage(float opening, float deadZone, float closure) {
  if (opening <= 0.0f) return closure;
  if (opening >= 1.0f) return 0.0f;
  return closure - opening * (closure - deadZone);
}

// Blend the motion prediction into measured flow without attenuating it.
static float ppvSmithFeedback(float measured, float ideal, float actuator, float gain) {
  return measured + gain * (ideal - actuator);
}

// Position in equivalent command volts, not measured valve feedback.
// Calibration stroke is always 0 <-> 4.2 V, independent of flow closure fit.
static float ppvTravelSeconds(float position, float target, float openS, float closeS) {
  return fabsf(target - position) * (target < position ? openS : closeS) / 4.2f;
}

static float ppvAdvance(float position, float target, float dt, float openS, float closeS) {
  float strokeS = target < position ? openS : closeS;
  if (strokeS <= 0.0f) return target;  // Uncalibrated, Smith must remain disabled.
  float step = 4.2f * dt / strokeS;
  float delta = target - position;
  if (fabsf(delta) <= step) return target;
  return position + (delta > 0.0f ? step : -step);
}

static float ppvEffectiveOpening(float volts, float deadZone, float closure) {
  if (volts <= deadZone) return 1.0f;
  if (volts >= closure) return 0.0f;
  return (closure - volts) / (closure - deadZone);
}
