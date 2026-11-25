#pragma once
#include <math.h>
#include <stdint.h>

// v8 expiration opening cap (2026-10-14 PM build, uniform 6/8 bpm runs).
// Model-opening coordinate: 0.1883 for 250 ms, linear to 0.3451 at 750 ms,
// then linear to 0.4707 at phase end. With Uc=4.30 V and Ud=4.1535 V these
// are 4.2724/4.2499/4.2310 V. This is a control policy, not a calibration.
static float ppvExpirationOpeningLimit(uint32_t elapsedMs, float durationMs) {
  const float initial = .1883f, mid = .3451f, final = .4707f;
  const float hold = 250.0f, ramp = 750.0f;
  float t = (float)elapsedMs;
  if (t <= hold) return initial;
  if (t <= ramp) return initial + (mid - initial) * (t - hold) / (ramp - hold);
  float late = (t - ramp) / fmaxf(1.0f, fmaxf(250.0f, durationMs) - ramp);
  return mid + fminf(1.0f, fmaxf(0.0f, late)) * (final - mid);
}

// Inspiration FF in relative-conductance units (1 = 0 V full open).
// Inverse of Q = 0.06 * availableVolume / tau0 * conductance.
static float ppvFeedforwardConductance(float referenceSlm, float availableVolumeMl,
                                       float tau0) {
  if (referenceSlm <= 0.0f) return 0.0f;
  if (availableVolumeMl <= 0.0f) return 1.0f;
  float ratio = referenceSlm / (0.06f * availableVolumeMl / tau0);
  return ratio >= 1.0f ? 1.0f : ratio;
}

// ATM conductance relative to 0 V, continuous over the whole 0..closure range.
// Depth = closure - U. Up to ATM_JOIN_DEPTH_V the configured 2026-10-14 PM
// power law applies (fitted 4.20-4.25 V). Deeper, the 2026-10-14 morning
// 0-4.20 V sweep (G = tau(0 V)/tau(U)) is used, shifted +0.05 V onto the PM
// batch and normalised so that it joins continuously and reaches 1 at 0 V.
static const float ATM_JOIN_DEPTH_V = 0.10f;
static const uint8_t ATM_LOW_POINTS = 12;
static const float ATM_LOW_DEPTH_V[ATM_LOW_POINTS] =
    {.10f, .15f, .20f, .25f, .30f, .40f, .45f, .55f, .85f, 1.25f, 2.25f, 3.25f};
static const float ATM_LOW_FRACTION[ATM_LOW_POINTS] =
    {0.0f, .256f, .442f, .553f, .615f, .738f, .765f, .834f, .870f, .936f, .962f, .976f};

static float ppvAtmJoinConductance(float deadZone, float closure, float gamma) {
  return powf(ATM_JOIN_DEPTH_V / (closure - deadZone), gamma);
}

static float ppvAtmConductance(float volts, float deadZone, float closure, float gamma) {
  float depth = closure - volts;
  if (depth <= 0.0f) return 0.0f;
  if (depth <= ATM_JOIN_DEPTH_V) return powf(depth / (closure - deadZone), gamma);
  float join = ppvAtmJoinConductance(deadZone, closure, gamma);
  float fraction = 1.0f;  // Beyond the last table point: linear to 1 at 0 V.
  for (uint8_t i = 1; i <= ATM_LOW_POINTS; ++i) {
    float d0 = ATM_LOW_DEPTH_V[i - 1], f0 = ATM_LOW_FRACTION[i - 1];
    float d1 = i < ATM_LOW_POINTS ? ATM_LOW_DEPTH_V[i] : closure;
    float f1 = i < ATM_LOW_POINTS ? ATM_LOW_FRACTION[i] : 1.0f;
    if (depth <= d1) { fraction = f0 + (f1 - f0) * (depth - d0) / (d1 - d0); break; }
  }
  return join + (1.0f - join) * fraction;
}

// Inverse of ppvAtmConductance: no jump between the fitted and plateau regions.
static float ppvAtmCommandVoltage(float conductance, float deadZone, float closure,
                                  float gamma) {
  if (conductance <= 0.0f) return closure;
  if (conductance >= 1.0f) return 0.0f;
  float join = ppvAtmJoinConductance(deadZone, closure, gamma);
  if (conductance <= join)
    return closure - (closure - deadZone) * powf(conductance, 1.0f / gamma);
  float fraction = (conductance - join) / (1.0f - join);
  for (uint8_t i = 1; i <= ATM_LOW_POINTS; ++i) {
    float d0 = ATM_LOW_DEPTH_V[i - 1], f0 = ATM_LOW_FRACTION[i - 1];
    float d1 = i < ATM_LOW_POINTS ? ATM_LOW_DEPTH_V[i] : closure;
    float f1 = i < ATM_LOW_POINTS ? ATM_LOW_FRACTION[i] : 1.0f;
    if (fraction <= f1) return closure - (d0 + (fraction - f0) * (d1 - d0) / (f1 - f0));
  }
  return 0.0f;
}

// VAC keeps the linear model-opening coordinate used by the v8 expiration cap.
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

// DEAD_TO_EXP exit (protocol 17). The estimated position is open-loop, and run
// 1109152100 surged with it already at closure, so arrival alone is not enough:
// the close command must also be at least dwellMinMs old. Protocol 18 uses the
// same rule for DEAD_TO_INSP, timed from VAC closing (state entry).
static bool ppvTransitionReady(float estimatedV, float targetV,
                               uint32_t sinceCloseCommandMs, float dwellMinMs) {
  return fabsf(estimatedV - targetV) <= 0.0001f
         && (float)sinceCloseCommandMs >= dwellMinMs;
}

// DEAD_TO_EXP exit (protocol 19): VAC opens settleMs after the estimated PPV
// reached the close target, so the wait follows the stroke actually needed
// (deep inspiration ends wait longer) plus the PPV lag beyond the stroke
// model; dwellMinMs since the close command is an optional floor.
static bool ppvTransitionSettled(bool arrived, uint32_t sinceArrivalMs, float settleMs,
                                 uint32_t sinceCloseCommandMs, float dwellMinMs) {
  return arrived && (float)sinceArrivalMs >= settleMs
         && (float)sinceCloseCommandMs >= dwellMinMs;
}

// Expiration length in integer ms. Within-cycle mode deducts the time already
// spent since the cycle start (positioning, inspiration, transition) from
// 60/bpm, so the transition no longer lengthens the cycle. 250 ms floor as in
// the reference. Integer ms keep the cycle boundary exact.
// Protocol 21 within-cycle split: the period minus the measured DEAD_TO_INSP
// (elapsedMs at INSPIRATION entry) and the expected DEAD_TO_EXP is shared by
// I:E, so the transitions no longer come out of expiration alone. Expiration
// still takes the exact remainder (ppvExpirationDurationMs), so the period
// stays exact and an estimate error only shifts a little time into it.
static uint32_t ppvInspirationDurationMs(bool withinCycle, uint32_t periodMs,
                                         uint32_t nominalMs, uint32_t elapsedMs,
                                         uint32_t expectedDeadToExpMs,
                                         float ieIn, float ieOut) {
  if (!withinCycle) return nominalMs;
  int32_t available = (int32_t)periodMs - (int32_t)elapsedMs
                      - (int32_t)expectedDeadToExpMs;
  if (available < 500) available = 500;  // 0.25 s for each phase at least.
  uint32_t ti = (uint32_t)lroundf(available * ieIn / (ieIn + ieOut));
  if (ti < 250) ti = 250;
  return ti < nominalMs ? ti : nominalMs;
}

static uint32_t ppvExpirationDurationMs(bool withinCycle, uint32_t periodMs,
                                        uint32_t nominalMs, uint32_t elapsedMs) {
  if (!withinCycle) return nominalMs;
  return elapsedMs + 250 >= periodMs ? 250 : periodMs - elapsedMs;
}

static float ppvEffectiveOpening(float volts, float deadZone, float closure) {
  if (volts <= deadZone) return 1.0f;
  if (volts >= closure) return 0.0f;
  return (closure - volts) / (closure - deadZone);
}
