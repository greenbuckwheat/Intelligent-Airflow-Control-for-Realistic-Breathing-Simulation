/*
 * LungLogic single-lung controller for NUCLEO-G474RE.
 * Protocol: READY,LUNGLOGIC_SINGLE_G474,21 / CFG / START / STOP / PING / TEL
 *
 * Pneumatic convention:
 *   inspiration = atmosphere valve open, vacuum valve closed, model Q > 0
 *   expiration  = atmosphere valve closed, vacuum valve open, model Q < 0
 *   dead time   = both directional valves closed
 *
 * IMPORTANT: 24 V solenoids and PPV40V power must not be driven directly
 * from MCU pins. Use suitable MOSFET/driver stages, flyback protection,
 * a regulated supply, and a shared ground. D5 produces a 3.3 V PWM duty
 * command; external filtering/level conversion must provide the PPV's 0-5 V.
 * The routing loads are Norgren 8254200.9151.02400, 24 VDC, 2/2 NC,
 * approximately 18 W each. GPIO drives only the PCB MOSFET stages.
 */

#include <Arduino.h>
#include "ppv_motion.h"
#include "flow_calibration.h"

#if !defined(ARDUINO_NUCLEO_G474RE)
#error "Select Nucleo-64 / Nucleo G474RE in STM32 MCU based boards"
#endif

#ifndef VALVE_ACTIVE_HIGH
#define VALVE_ACTIVE_HIGH 1
#endif

static const uint32_t BAUD = 115200;
static const uint16_t CONTROL_PERIOD_US = 10000; // 100 Hz sampling and control.
static const uint8_t TELEMETRY_DIVIDER = 2;      // 50 Hz telemetry.

// Final NUCLEO-G474RE PCB pin allocation, left branch.
static const uint32_t PIN_ATM = D11;        // Left atmosphere solenoid driver.
static const uint32_t PIN_PPV_PWM = D5;     // Left PPV PWM through external 0-5 V interface.
static const uint32_t PIN_VAC = D13;        // Left vacuum solenoid driver.
static const uint32_t SDA_PIN = A4;         // PCB-routed software I2C: PC1.
static const uint32_t SCL_PIN = A5;         // PCB-routed software I2C: PC0.
static const uint8_t VALVE_ON = VALVE_ACTIVE_HIGH ? HIGH : LOW;
static const uint8_t VALVE_OFF = VALVE_ACTIVE_HIGH ? LOW : HIGH;

static const uint8_t TCA_ADDR = 0x70;
static const uint8_t SFM_ADDR = 0x40;
static const int8_t SFM_CHANNEL = -1;        // Current bench: SFM3300 direct on A4/A5; 0 for Pa.Hub.
static const float SFM_SCALE = 120.0f;
static const float SFM_OFFSET = 32768.0f;
static const float DT = CONTROL_PERIOD_US * 1.0e-6f;
static const uint16_t SMITH_DELAY_SAMPLES = 1000000UL / CONTROL_PERIOD_US;
// Five failed reads at 100 Hz retain the 50 ms nominal failure threshold.
static const uint8_t SENSOR_FAILURE_LIMIT = 50000UL / CONTROL_PERIOD_US;
static const float ML_PER_S_PER_SLM = 1000.0f / 60.0f;
static const uint32_t HOST_WATCHDOG_MS = 5000;
// A normal stop restores the bag before isolating it.  The timeout guarantees
// that a disconnected/blocked atmosphere route cannot leave ATM energized.
static const uint32_t RECOVERY_TIMEOUT_MS = 5000;
// Afternoon high-voltage data still show plateau flow at 4.259 V.
// 4.259--4.300 V interpolation remains provisional, not a measured curve.
static const float VAC_LEAK_ROLLOFF_WIDTH_V = 0.041f;
// ATM closes on the per-cycle inspired integral. The cumulative estimate is a
// safety cap only: it carries just the previous cycle's residual, so the
// 8-35 mL/cycle integration bias seen in v8 logs cannot accumulate.
static const float VOLUME_GUARD_MARGIN_ML = 50.0f;

enum State : uint8_t;
struct Config;
struct PIDState;

// A4/A5 on the G474RE are not a hardware-I2C pair. This bounded open-drain
// implementation preserves the PCB routing and supports the Pa.Hub/TCA9548A.
static void sda(bool high) { digitalWrite(SDA_PIN, high ? HIGH : LOW); }
static void sclLow() { digitalWrite(SCL_PIN, LOW); delayMicroseconds(5); }
static bool sclHigh() {
  digitalWrite(SCL_PIN, HIGH);
  uint32_t start = micros();
  while (!digitalRead(SCL_PIN)) {
    if ((uint32_t)(micros() - start) > 1000) return false;
  }
  delayMicroseconds(5);
  return true;
}
static bool busStart() {
  sda(true);
  if (!sclHigh() || !digitalRead(SDA_PIN)) return false;
  sda(false); delayMicroseconds(5); sclLow();
  return true;
}
static void busStop() {
  sclLow(); sda(false); sclHigh(); sda(true); delayMicroseconds(5);
}
static bool busWrite(uint8_t value) {
  for (uint8_t i = 0; i < 8; ++i) {
    sda(value & 0x80); delayMicroseconds(5);
    if (!sclHigh()) return false;
    sclLow(); value <<= 1;
  }
  sda(true);
  if (!sclHigh()) return false;
  bool ack = !digitalRead(SDA_PIN);
  sclLow();
  return ack;
}
static bool busRead(uint8_t &value, bool ack) {
  value = 0; sda(true);
  for (uint8_t i = 0; i < 8; ++i) {
    if (!sclHigh()) return false;
    value = (value << 1) | (digitalRead(SDA_PIN) ? 1 : 0);
    sclLow();
  }
  sda(!ack);
  if (!sclHigh()) return false;
  sclLow(); sda(true);
  return true;
}
static bool selectSensor() {
  if (SFM_CHANNEL < 0) return true;
  bool ok = busStart() && busWrite(TCA_ADDR << 1)
            && busWrite((uint8_t)(1U << SFM_CHANNEL));
  busStop();
  return ok;
}
static bool sensorCommand(uint16_t command) {
  if (!selectSensor()) return false;
  bool ok = busStart() && busWrite(SFM_ADDR << 1)
            && busWrite(command >> 8) && busWrite(command & 0xFF);
  busStop();
  return ok;
}

enum State : uint8_t {
  IDLE = 0,
  PRECONDITION = 1,
  DEAD_TO_INSP = 2,
  INSPIRATION = 3,
  DEAD_TO_EXP = 4,
  EXPIRATION = 5,
  COMPLETE = 6,
  FAULT = 7,
  RECOVERY = 8
};

struct Config {
  float rrBpm;
  float ieIn;
  float ieOut;
  float targetMl;
  float fillTimeS;
  uint16_t deadTimeMs;
  float kp;
  float ki;
  float kd;
  float integralLimit;
  float preconditionS;
  float maxAbsFlowSlm;
  float maxVolumeMl;
  int8_t flowSign;
  float capacityMl;
  float tauInS;
  float tauOutS;
  float valveExponent;
  bool smithEnabled;
  float smithDelayS;
  float smithCorrectionGain;
  float gammaOut;
  float deadZoneInV;
  float deadZoneOutV;
  float closureV;
  float ppvOpenTimeS;
  float ppvCloseTimeS;
  float flowOffsetSlm;
  float flowGain;
  float vacLeakSlm;
  bool ffInspirationEnabled;
  bool ffExpirationEnabled;
  float expirationIntegralLimit;
  float expirationKp;
  float expirationKi;
  float expirationKd;
  float inspirationMinV;  // Lowest inspiration PPV voltage; 0 allows full open.
  float transitionDwellMinS;   // Optional floor: age of the PPV close command before VAC.
  float transitionOverCloseV;  // Optional transition target above closureV; 0 = off.
  bool deadTimeWithinCycle;    // Transition time is deducted from expiration.
  float inspirationDwellMinS;  // Minimum both-closed time after VAC before ATM opens.
  float transitionSettleS;     // Wait after the estimated PPV reaches the close target.
  float expirationClosureV;    // VAC zero opening (seal); inspiration keeps closureV.
};

struct PIDState {
  float integral;
  float previousMeasurement;
  bool hasPreviousMeasurement;
  float bias;
  float heldOpening;
  bool hasBias;
};

static Config gCfg = {
  6.0f, 1.0f, 2.0f, 250.0f, 3.333f, 0,
  0.01f, 0.04f, 0.0f, 12.0f, 2.0f, 220.0f, 250.0f, 1,
  // October 14 afternoon high-range fit; inherited 0 V tau.
  // Low-range extrapolation and dynamic closure still require bench validation.
  // Model amplitude 320.3 mL (fitted).
  // Mechanical stroke remains 2.436 s per 4.2 V in both directions.
  // Smith residual delay 0.2 s: measured VAC command-to-flow lag beyond the
  // stroke model (2026-10-22).
  320.3f, 0.5853f, 0.2549f, 1.7861f, true, 0.2f, 0.6f,
  1.9915f, 4.1200f, 4.1535f, 4.30f, 2.436f, 2.436f, 0.0034f, 1.0471f, 1.0122f,
  // Expiration PI restores the v8 opening-coordinate gains and integral limit.
  true, false, 4.0f, 0.01f, 0.06f, 0.0f,
  3.0f,
  // Protocol 17 transition (2026-10-22): 1.0 s since the close command, target
  // closureV; transitions deducted from expiration so the cycle stays 60/bpm.
  1.0f, 0.0f, true,
  // VAC tail after closing: a -3..-7 SLM plateau of ~17 mL that ends 180-300 ms
  // after VAC closes (2026-10-22 runs with 0.5 s); 0.08 s left 2-3 samples.
  0.35f,
  // Settle after estimated arrival: 0 = off (protocol 17 exit). 0.3 s with a
  // 4.5/5.0 V target sealed at VAC opening but snapped open mid-expiration.
  0.0f,
  // VAC zero opening. 4.30 V sealed in the morning of 2026-10-22 but leaked
  // from ~15:00 (flow at 4.294-4.300 V); 4.32 V gave the best runs (J ~0.10).
  4.32f
};

static bool gConfigValid = false;
static bool gRunning = false;
static State gState = IDLE;
static uint32_t gRunId = 0;
static uint16_t gRequestedCycles = 1;
static uint16_t gCycle = 0;
static uint32_t gRunStartMs = 0;
static uint32_t gStateStartMs = 0;
static uint32_t gLastHostMs = 0;
static uint32_t gNextControlUs = 0;
static uint32_t gStep = 0;
static uint8_t gSensorFailures = 0;
static bool gRecoveryRequestedByStop = false;
static bool gPpvCloseCommanded = false;  // Close target issued this cycle (ATM cut or DEAD_TO_EXP).
static uint32_t gPpvCloseCommandMs = 0;
static bool gPpvCloseArrived = false;    // Estimated PPV reached the close target.
static uint32_t gPpvCloseArrivedMs = 0;
static uint32_t gCycleStartMs = 0;       // DEAD_TO_INSP entry; first cycle: INSPIRATION entry.
static uint32_t gExpirationDurationMs = 0;  // Fixed at EXPIRATION entry; phase timer.
// Protocol 21: inspiration length and its fill time are fixed at INSPIRATION
// entry; with within-cycle timing both phases share the transition time by I:E.
static uint32_t gInspirationDurationMs = 0;
static float gInspirationFillS = 1.0f;       // Reference/FF fill time of this cycle.
// Last cycle's close command -> VAC opening. Not the DEAD_TO_EXP state time:
// after an early ATM cut part of the wait falls inside inspiration, and using
// the shorter state time over-sized the next inspiration (15 bpm run 1647106051).
static uint32_t gLastDeadToExpMs = 0;
static float gExpirationDurationS = 0.0f;   // Same duration for reference and cap.

static float gFlowSlm = 0.0f;  // Calibrated sensor flow, before leakage compensation.
static float gRawFlowSlm = 0.0f;
static float gNetFlowSlm = 0.0f;
static uint8_t gSampledValveMask = 0;
static float gVolumeMl = 0.0f;
static float gFlowRefSlm = 0.0f;
static float gControlFeedbackSlm = 0.0f;
static float gOpening = 0.0f;
static float gCommandV = 5.0f;
static float gCycleInspiredMl = 0.0f;
static float gCycleExpiredMl = 0.0f;      // Leak-compensated; telemetry only.
// Uncompensated extraction for the residual estimate. The near-closure leak
// model is unreliable; subtracting it inflated the residual by 50-100 mL/cycle
// and tripped the volume guard (runs 1111906052/1111993584/1112064565).
static float gCycleExpiredRawMl = 0.0f;
static float gExpirationBudgetMl = 0.0f;  // Telemetry only; not a control input.
static uint8_t gValveMask = 0;
static PIDState gPid = {0.0f, 0.0f, false};
static float gSmithVolumeMl = 0.0f;
static float gSmithActuatorVolumeMl = 0.0f;
static float gSmithIdealFlowSlm = 0.0f;
static float gSmithDelayedFlowSlm = 0.0f;
static float gEstimatedPpvV = 5.0f;  // Initial closed-position assumption.
static uint32_t gPpvUpdateUs = 0;
static float gSmithDelayLine[SMITH_DELAY_SAMPLES];
static uint16_t gSmithDelayCount = 0;
static uint16_t gSmithDelayIndex = 0;
static char gRx[320];
static uint16_t gRxLen = 0;

static float clampf(float value, float low, float high) {
  if (value < low) return low;
  if (value > high) return high;
  return value;
}

static uint16_t voltsToPwm(float volts) {
  return (uint16_t)lroundf(clampf(volts, 0.0f, 5.0f) * 4095.0f / 5.0f);
}

static float directionGamma(int8_t direction) {
  return direction < 0 ? gCfg.gammaOut : gCfg.valveExponent;
}

// Protocol 20: the VAC map is shifted so that zero opening sits at the
// expiration closure; volts per unit opening stay closureV - deadZoneOutV, so
// the v8 cap and the PI gains keep their meaning. Inspiration keeps closureV.
static float expirationDeadZoneV() {
  return gCfg.deadZoneOutV + gCfg.expirationClosureV - gCfg.closureV;
}

// ATM: relative conductance, continuous down to 0 V. VAC: linear model opening
// from the expiration closure. Idle: linear map on closureV.
static float openingCommandV(float opening, int8_t direction) {
  opening = clampf(opening, 0.0f, 1.0f);
  if (direction > 0)
    return ppvAtmCommandVoltage(opening, gCfg.deadZoneInV, gCfg.closureV, gCfg.valveExponent);
  if (direction < 0)
    return ppvCommandVoltage(opening, expirationDeadZoneV(), gCfg.expirationClosureV);
  return ppvCommandVoltage(opening, gCfg.deadZoneInV, gCfg.closureV);
}

// Conductance at the configured lowest inspiration voltage; PI and FF limit.
static float inspirationMaxOpening() {
  if (gCfg.inspirationMinV <= 0.0f) return 1.0f;
  return ppvAtmConductance(gCfg.inspirationMinV, gCfg.deadZoneInV, gCfg.closureV,
                           gCfg.valveExponent);
}

static float inspirationFeedforwardOpening(float volumeMl) {
  if (!gCfg.ffInspirationEnabled) return 0.0f;
  float qRef = 0.06f * gCfg.targetMl / gInspirationFillS;
  return min(inspirationMaxOpening(),
             ppvFeedforwardConductance(qRef, gCfg.capacityMl - volumeMl, gCfg.tauInS));
}

static void advancePpvPosition() {
  uint32_t now = micros();
  float dt = (uint32_t)(now - gPpvUpdateUs) * 1.0e-6f;
  gPpvUpdateUs = now;
  gEstimatedPpvV = ppvAdvance(gEstimatedPpvV, gCommandV, dt,
                              gCfg.ppvOpenTimeS, gCfg.ppvCloseTimeS);
}

static void writePinchCommand(float opening, int8_t direction = 0) {
  advancePpvPosition();  // Finish elapsed motion toward the OLD command first.
  gOpening = clampf(opening, 0.0f, 1.0f);
  // Opening is the directional model coordinate (ATM: relative conductance).
  // Normal regulation closes at closureV. Fault closure uses 5 V separately.
  gCommandV = openingCommandV(gOpening, direction);
  analogWrite(PIN_PPV_PWM, voltsToPwm(gCommandV));
}

static float transitionCloseV() {
  return gCfg.expirationClosureV + gCfg.transitionOverCloseV;
}

// Issue the transition close target once per cycle; the dwell timer starts at
// the first command, so time gained by closing at the ATM cut is counted.
static void commandTransitionClose() {
  if (gPpvCloseCommanded) return;
  advancePpvPosition();
  gOpening = 0.0f;
  gCommandV = transitionCloseV();
  analogWrite(PIN_PPV_PWM, voltsToPwm(gCommandV));
  gPpvCloseCommanded = true;
  gPpvCloseCommandMs = millis();
  gPpvCloseArrived = false;
}

// Protocol 19: time the settle from the moment the estimated PPV reaches the
// close target, which may already happen during inspiration after an ATM cut.
static void trackTransitionArrival() {
  if (!gPpvCloseCommanded || gPpvCloseArrived) return;
  if (fabsf(gEstimatedPpvV - gCommandV) <= 0.0001f) {
    gPpvCloseArrived = true;
    gPpvCloseArrivedMs = millis();
  }
}

static void closeDirectionalValves() {
  digitalWrite(PIN_ATM, VALVE_OFF);
  digitalWrite(PIN_VAC, VALVE_OFF);
  gValveMask = 0;
}

static void writePpvFullClose() {
  advancePpvPosition();
  gOpening = 0.0f;
  gCommandV = 5.0f;
  analogWrite(PIN_PPV_PWM, voltsToPwm(gCommandV));
}

static void safeOutputs() {
  closeDirectionalValves();
  writePpvFullClose();
}

// Normal idle differs from fault shutdown: solenoids closed, PPV fully open.
static void idleOutputs() {
  closeDirectionalValves();
  writePinchCommand(1.0f);
}

static void resetPID() {
  gPid.integral = 0.0f;
  gPid.previousMeasurement = 0.0f;
  gPid.hasPreviousMeasurement = false;
  gPid.bias = 0.0f;
  gPid.heldOpening = gOpening;
  gPid.hasBias = false;
}

static void resetSmith(float volumeMl) {
  gSmithVolumeMl = clampf(volumeMl, 0.0f, gCfg.capacityMl);
  gSmithActuatorVolumeMl = gSmithVolumeMl;
  gSmithIdealFlowSlm = 0.0f;
  gSmithDelayedFlowSlm = 0.0f;
  // Never reset physical position on a breathing phase change.
  gSmithDelayIndex = 0;
  gSmithDelayCount = (uint16_t)min((int)SMITH_DELAY_SAMPLES,
                                 max(0, (int)lroundf(gCfg.smithDelayS / DT)));
  for (uint16_t i = 0; i < SMITH_DELAY_SAMPLES; ++i) gSmithDelayLine[i] = 0.0f;
  gControlFeedbackSlm = gNetFlowSlm;
}

static float smithModelFlow(float volts, int8_t direction, float &volume) {
  float effective = direction > 0
      ? ppvAtmConductance(volts, gCfg.deadZoneInV, gCfg.closureV, gCfg.valveExponent)
      : powf(ppvEffectiveOpening(volts, expirationDeadZoneV(), gCfg.expirationClosureV),
             directionGamma(direction));
  float dvdt = 0.0f;
  if (direction > 0) dvdt = effective * (gCfg.capacityMl - volume) / gCfg.tauInS;
  else if (direction < 0) dvdt = -effective * volume / gCfg.tauOutS;
  volume = clampf(volume + dvdt * DT, 0.0f, gCfg.capacityMl);
  // Measurement is already sign-corrected into ATM positive / VAC negative.
  return 0.06f * dvdt;
}

static float updateSmith(int8_t direction, float measured) {
  float ideal = smithModelFlow(gCommandV, direction, gSmithVolumeMl);
  float actuator = smithModelFlow(gEstimatedPpvV, direction, gSmithActuatorVolumeMl);
  float delayed = actuator;
  if (gSmithDelayCount > 0) {
    delayed = gSmithDelayLine[gSmithDelayIndex];
    gSmithDelayLine[gSmithDelayIndex] = actuator;
    gSmithDelayIndex = (uint16_t)((gSmithDelayIndex + 1) % gSmithDelayCount);
  }
  gSmithIdealFlowSlm = ideal;
  gSmithDelayedFlowSlm = delayed;
  return ppvSmithFeedback(measured, ideal, delayed, gCfg.smithCorrectionGain);
}

static uint8_t sfmCrc(const uint8_t *data, uint8_t count) {
  uint8_t crc = 0x00;
  for (uint8_t i = 0; i < count; ++i) {
    crc ^= data[i];
    for (uint8_t bit = 0; bit < 8; ++bit) {
      crc = (crc & 0x80) ? (uint8_t)((crc << 1) ^ 0x31) : (uint8_t)(crc << 1);
    }
  }
  return crc;
}

static bool sfmStart() {
  if (!sensorCommand(0x2000)) return false;
  delay(100);
  return sensorCommand(0x1000);
}

static bool sfmRead(float &flowSlm) {
  uint8_t bytes[3];
  if (!selectSensor()) return false;
  bool ok = busStart() && busWrite((SFM_ADDR << 1) | 1)
            && busRead(bytes[0], true) && busRead(bytes[1], true)
            && busRead(bytes[2], false);
  busStop();
  if (!ok) return false;
  if (sfmCrc(bytes, 2) != bytes[2]) return false;
  uint16_t raw = ((uint16_t)bytes[0] << 8) | bytes[1];
  flowSlm = ((float)raw - SFM_OFFSET) / SFM_SCALE;
  return true;
}

static float updatePID(float reference, float measurement, float feedforward,
                       float outputLimit = 1.0f,
                       float integralLimit = -1.0f) {
  float kp = gState == EXPIRATION ? gCfg.expirationKp : gCfg.kp;
  float ki = gState == EXPIRATION ? gCfg.expirationKi : gCfg.ki;
  float kd = gState == EXPIRATION ? 0.0f : gCfg.kd;  // Expiration is PI only.
  if (integralLimit <= 0.0f) integralLimit = gCfg.integralLimit;
  float error = reference - measurement;
  if (!gPid.hasBias) {
    gPid.bias = gPid.heldOpening - feedforward - kp * error;
    gPid.hasBias = true;
    gPid.previousMeasurement = measurement;
    gPid.hasPreviousMeasurement = true;
    return gPid.heldOpening;
  }
  float candidate = clampf(gPid.integral + error * DT,
                           -integralLimit, integralLimit);
  float derivative = gPid.hasPreviousMeasurement ? -(measurement - gPid.previousMeasurement) / DT : 0.0f;
  gPid.previousMeasurement = measurement;
  gPid.hasPreviousMeasurement = true;
  // Bumpless command transition; this is not a measured physical PPV slew rate.
  gPid.bias -= clampf(gPid.bias, -50.0f * DT, 50.0f * DT);
  float raw = gPid.bias + feedforward + kp * error + ki * candidate + kd * derivative;
  float output = clampf(raw, 0.0f, outputLimit);
  if ((raw > 0.0f && raw < outputLimit)
      || (raw <= 0.0f && error > 0.0f)
      || (raw >= outputLimit && error < 0.0f)) {
    gPid.integral = candidate;
  }
  return output;
}

static float cyclePeriodS() {
  return 60.0f / gCfg.rrBpm;
}

static float inspirationS() {
  return cyclePeriodS() * gCfg.ieIn / (gCfg.ieIn + gCfg.ieOut);
}

static float expirationS() {
  return cyclePeriodS() * gCfg.ieOut / (gCfg.ieIn + gCfg.ieOut);
}

static float volumeGuardMl() {
  return gCfg.maxVolumeMl + VOLUME_GUARD_MARGIN_ML;
}

static void setState(State next) {
  closeDirectionalValves();
  if (next == DEAD_TO_INSP) {
    writePinchCommand(inspirationFeedforwardOpening(gVolumeMl), 1);
  } else if (next == DEAD_TO_EXP) {
    // v8 path from the open side to closureV (+ optional over-close). Usually
    // already issued at the ATM cut; the minimum dwell is enforced on exit.
    commandTransitionClose();
  }
  resetPID();
  resetSmith(gVolumeMl);
  State previous = gState;
  gState = next;
  if (next == DEAD_TO_INSP) gCycleStartMs = millis();
  if (next == INSPIRATION) {
    // The startup positioning after PRECONDITION is not part of cycle 0.
    if (previous == DEAD_TO_INSP && gCycle == 0) gCycleStartMs = millis();
    gPpvCloseCommanded = false;
    gPpvCloseArrived = false;
    // Safety estimate restarts from the last cycle's unexhaled residual only.
    // Both integrals are zero before the first cycle (after PRECONDITION).
    gVolumeMl = clampf(gCycleInspiredMl - gCycleExpiredRawMl, 0.0f, volumeGuardMl());
    gCycleInspiredMl = 0.0f;
    // Protocol 21: share the transitions by I:E (expected DEAD_TO_EXP = last
    // cycle's measured one); the reference keeps the volume over the shorter phase.
    gInspirationDurationMs = ppvInspirationDurationMs(
        gCfg.deadTimeWithinCycle, (uint32_t)lroundf(cyclePeriodS() * 1000.0f),
        (uint32_t)(inspirationS() * 1000.0f), millis() - gCycleStartMs,
        gLastDeadToExpMs, gCfg.ieIn, gCfg.ieOut);
    gInspirationFillS = min(gCfg.fillTimeS, gInspirationDurationMs * 0.001f);
  }
  if (next == EXPIRATION) {
    if (previous == DEAD_TO_EXP) gLastDeadToExpMs = millis() - gPpvCloseCommandMs;
    gExpirationBudgetMl = min(gCfg.targetMl, gCycleInspiredMl);
    gCycleExpiredMl = 0.0f;
    gCycleExpiredRawMl = 0.0f;
    uint32_t elapsedMs = millis() - gCycleStartMs;
    uint32_t periodMs = (uint32_t)lroundf(cyclePeriodS() * 1000.0f);
    gExpirationDurationMs = ppvExpirationDurationMs(
        gCfg.deadTimeWithinCycle, periodMs, (uint32_t)(expirationS() * 1000.0f), elapsedMs);
    // Off: identical to protocol 16 (nominal I:E expiration).
    gExpirationDurationS = gCfg.deadTimeWithinCycle
        ? gExpirationDurationMs * 0.001f : expirationS();
    if (gCfg.deadTimeWithinCycle && elapsedMs + 250 > periodMs) {
      // Transition overran the period; the 0.25 s floor lengthens this cycle.
      Serial.print(F("WARN,EXP_FLOOR,"));
      Serial.print(gRunId); Serial.print(',');
      Serial.println(gCycle);
    }
  }
  gStateStartMs = millis();
}

// Immediate shutdown is reserved for reconfiguration and safety paths.  User
// STOP and normal completion use beginRecovery() below.
static void stopRun() {
  gRunning = false;
  gState = IDLE;
  idleOutputs();
  resetPID();
}

static void beginRecovery(bool requestedByStop) {
  // Route atmosphere only, then command the PPV fully open.  Closing both
  // routing valves first preserves the ATM/VAC interlock during transition.
  closeDirectionalValves();
  writePinchCommand(1.0f, 1);
  resetPID();
  resetSmith(gVolumeMl);
  gRecoveryRequestedByStop = requestedByStop;
  gState = RECOVERY;
  gStateStartMs = millis();
  gRunning = true;
  Serial.print(F("RECOVERING,"));
  Serial.print(gRunId);
  Serial.print(',');
  Serial.println(requestedByStop ? F("STOP") : F("DONE"));
}

static void finishRecovery(bool timedOut) {
  bool stoppedByUser = gRecoveryRequestedByStop;
  gRunning = false;
  gState = stoppedByUser ? IDLE : COMPLETE;
  // idleOutputs leaves the PPV fully open but isolates the bag by closing
  // both ATM and VAC, as required after volume restoration.
  idleOutputs();
  resetPID();
  if (timedOut) {
    Serial.print(F("WARN,RECOVERY_TIMEOUT,"));
    Serial.println(gRunId);
  }
  Serial.print(stoppedByUser ? F("STOPPED,") : F("DONE,"));
  Serial.println(gRunId);
}

static void enterFault(const __FlashStringHelper *reason) {
  gRunning = false;
  gState = FAULT;
  safeOutputs();
  Serial.print(F("FAULT,"));
  Serial.print(gRunId);
  Serial.print(',');
  Serial.println(reason);
}

static void applyValves(bool atmOpen, bool vacOpen) {
  if (atmOpen && vacOpen) {
    enterFault(F("VALVE_INTERLOCK"));
    return;
  }
  digitalWrite(PIN_ATM, atmOpen ? VALVE_ON : VALVE_OFF);
  digitalWrite(PIN_VAC, vacOpen ? VALVE_ON : VALVE_OFF);
  gValveMask = (atmOpen ? 1 : 0) | (vacOpen ? 2 : 0);
}

static bool validateConfig(const Config &c) {
  // Only physical/structural limits are enforced; tunable values have no
  // artificial upper bound (host GUI mirrors this).
  if (!(c.rrBpm > 0.0f && isfinite(c.rrBpm))) return false;
  if (!(c.ieIn > 0.0f && c.ieOut > 0.0f)) return false;
  if (!(c.targetMl > 0.0f && c.targetMl <= c.maxVolumeMl)) return false;
  float ti = (60.0f / c.rrBpm) * c.ieIn / (c.ieIn + c.ieOut);
  if (!(c.fillTimeS > 0.0f && c.fillTimeS <= ti)) return false;
  if (c.deadTimeMs != 0) return false; // PPV positioning replaces fixed dead time.
  if (!(c.kp >= 0.0f && c.ki >= 0.0f && c.kd >= 0.0f && c.integralLimit > 0.0f)) return false;
  if (!(c.expirationIntegralLimit > 0.0f)) return false;
  if (!(c.expirationKp >= 0 && c.expirationKi >= 0 && c.expirationKd >= 0)) return false;
  if (!(c.preconditionS >= 0.0f)) return false;
  if (!(c.maxAbsFlowSlm > 0.0f)) return false;
  if (!(c.flowSign == 1 || c.flowSign == -1)) return false;
  // Model amplitude and integrated-volume guard are separate quantities.
  if (!(c.capacityMl >= c.targetMl && isfinite(c.capacityMl))) return false;
  if (!(c.tauInS > 0.0f && c.tauOutS > 0.0f)) return false;
  if (!(c.valveExponent > 0.0f && c.gammaOut > 0.0f)) return false;
  if (!(c.deadZoneInV >= 0.0f && c.deadZoneInV < c.closureV)) return false;
  // The continuous ATM map needs the fitted law to cover the join depth and the
  // measured low-voltage table to lie inside 0..closure.
  if (!(c.closureV - c.deadZoneInV > ATM_JOIN_DEPTH_V && c.closureV > 3.5f)) return false;
  if (!(c.inspirationMinV >= 0.0f && c.inspirationMinV < c.closureV)) return false;
  if (!(c.deadZoneOutV >= 0.0f && c.deadZoneOutV < c.closureV)) return false;
  if (!(c.closureV > 0.0f && c.closureV <= 5.0f)) return false;  // 0-5 V PPV drive.
  // Delay line holds 1 s of samples; longer delays would be silently clipped.
  if (!(c.smithDelayS >= 0.0f && c.smithDelayS <= 1.0f)) return false;
  if (!(c.smithCorrectionGain >= 0.0f)) return false;
  if (!(c.ppvOpenTimeS > 0.0f && c.ppvCloseTimeS > 0.0f)) return false;
  if (!isfinite(c.flowOffsetSlm)) return false;
  if (!(c.flowGain > 0.0f && c.vacLeakSlm >= 0.0f)) return false;
  if (!(c.transitionDwellMinS >= 0.0f)) return false;
  if (!(c.expirationClosureV > c.deadZoneOutV && c.expirationClosureV <= 5.0f
        && isfinite(c.expirationClosureV))) return false;
  if (!(c.transitionOverCloseV >= 0.0f
        && c.expirationClosureV + c.transitionOverCloseV <= 5.0f)) return false;
  if (!(c.inspirationDwellMinS >= 0.0f && isfinite(c.inspirationDwellMinS))) return false;
  if (!(c.transitionSettleS >= 0.0f && isfinite(c.transitionSettleS))) return false;
  return true;
}

static uint8_t splitCsv(const String &line, String fields[], uint8_t capacity) {
  uint8_t count = 0;
  uint16_t start = 0;
  for (uint16_t i = 0; i <= line.length() && count < capacity; ++i) {
    if (i == line.length() || line.charAt(i) == ',') {
      fields[count++] = line.substring(start, i);
      start = i + 1;
    }
  }
  return count;
}

static void applyConfig(const String &line) {
  String f[47];
  uint8_t count = splitCsv(line, f, 47);
  if (count != 46 || f[1].toInt() != 21) {
    Serial.println(F("ERR,CFG_FORMAT"));
    return;
  }
  Config c;
  uint32_t token = (uint32_t)f[2].toInt();
  c.rrBpm = f[3].toFloat();
  c.ieIn = f[4].toFloat();
  c.ieOut = f[5].toFloat();
  c.targetMl = f[6].toFloat();
  c.fillTimeS = f[7].toFloat();
  c.deadTimeMs = (uint16_t)f[8].toInt();
  c.kp = f[9].toFloat();
  c.ki = f[10].toFloat();
  c.kd = f[11].toFloat();
  c.integralLimit = f[12].toFloat();
  c.preconditionS = f[13].toFloat();
  c.maxAbsFlowSlm = f[14].toFloat();
  c.maxVolumeMl = f[15].toFloat();
  c.flowSign = (int8_t)f[16].toInt();
  c.capacityMl = f[17].toFloat();
  c.tauInS = f[18].toFloat();
  c.tauOutS = f[19].toFloat();
  c.valveExponent = f[20].toFloat();
  c.smithEnabled = f[21].toInt() != 0;
  c.smithDelayS = f[22].toFloat();
  c.smithCorrectionGain = f[23].toFloat();
  c.gammaOut = f[24].toFloat();
  c.deadZoneInV = f[25].toFloat();
  c.deadZoneOutV = f[26].toFloat();
  c.closureV = f[27].toFloat();
  c.ppvOpenTimeS = f[28].toFloat();
  c.ppvCloseTimeS = f[29].toFloat();
  c.flowOffsetSlm = f[30].toFloat();
  c.flowGain = f[31].toFloat();
  c.vacLeakSlm = f[32].toFloat();
  if ((f[33] != "0" && f[33] != "1") || (f[34] != "0" && f[34] != "1")) {
    Serial.println(F("ERR,CFG_VALUE"));
    return;
  }
  c.ffInspirationEnabled = f[33] == "1";
  c.ffExpirationEnabled = false;  // Legacy wire field retained; VAC never uses FF.
  c.expirationIntegralLimit = f[35].toFloat();
  c.expirationKp = f[36].toFloat();
  c.expirationKi = f[37].toFloat();
  c.expirationKd = 0.0f;  // Legacy field retained; VAC uses PI.
  c.inspirationMinV = f[39].toFloat();
  c.transitionDwellMinS = f[40].toFloat();
  c.transitionOverCloseV = f[41].toFloat();
  if (f[42] != "0" && f[42] != "1") {
    Serial.println(F("ERR,CFG_VALUE"));
    return;
  }
  c.deadTimeWithinCycle = f[42] == "1";
  c.inspirationDwellMinS = f[43].toFloat();
  c.transitionSettleS = f[44].toFloat();
  c.expirationClosureV = f[45].toFloat();
  if (!validateConfig(c)) {
    Serial.println(F("ERR,CFG_VALUE"));
    return;
  }
  if (gRunning) stopRun();
  advancePpvPosition();
  gCfg = c;
  gConfigValid = true;
  Serial.print(F("ACK,CFG,"));
  Serial.println(token);
}

static void startRun(uint32_t runId, uint16_t cycles) {
  if (!gConfigValid) {
    Serial.println(F("ERR,CONFIG_REQUIRED"));
    return;
  }
  if (gRunning) {
    Serial.println(F("ERR,ALREADY_RUNNING"));
    return;
  }
  gRunId = runId;
  gRequestedCycles = max(cycles, (uint16_t)1);
  gCycle = 0;
  gStep = 0;
  gVolumeMl = 0.0f;
  // Integrals from a previous run must not seed cycle 0's residual estimate.
  gCycleInspiredMl = 0.0f;
  gCycleExpiredMl = 0.0f;
  gCycleExpiredRawMl = 0.0f;
  gPpvCloseCommanded = false;
  gPpvCloseArrived = false;
  // First cycle: expect the configured transition until one is measured.
  gLastDeadToExpMs = (uint32_t)lroundf(
      max(gCfg.transitionDwellMinS, gCfg.transitionSettleS) * 1000.0f);
  gInspirationDurationMs = (uint32_t)(inspirationS() * 1000.0f);
  gInspirationFillS = gCfg.fillTimeS;
  gRunStartMs = millis();
  gLastHostMs = millis();
  gRunning = true;
  setState(PRECONDITION);
  Serial.print(F("STARTED,"));
  Serial.println(gRunId);
}

static void printReady() {
  Serial.println(F("READY,LUNGLOGIC_SINGLE_G474,21"));
  Serial.println(F("INFO,MODEL_REV=2026-10-14_PM_UC430,FF=SELECTABLE_ATM,PPV_STROKE_UNCHANGED"));
  Serial.println(F("INFO,ESTOP_ENABLED=0"));
  Serial.println(F("INFO,CONTROL_HZ=100,TELEMETRY_HZ=50,BAUD=115200"));
  Serial.println(F("INFO,PI_GAINS=CFG_SEPARATE_INSP_EXP,INSP_UNIT=REL_CONDUCTANCE_0V,EXP_UNIT=MODEL_OPENING"));
  Serial.println(F("INFO,ATM_MAP=CONTINUOUS,PM_LAW_DEPTH_V=0.10,LOW=2026-10-14_AM_SWEEP_SHIFT_0.05V,INSP_MIN_V=CFG"));
  Serial.println(F("INFO,ATM_CUT=CYCLE_INSPIRED,VOLUME_GUARD=MAX+50ML,RESIDUAL=LAST_CYCLE_ONLY"));
  Serial.println(F("INFO,GUARD_RESIDUAL_EXP_FLOW=UNCOMPENSATED,PI_FEEDBACK=LEAK_COMPENSATED"));
  Serial.println(F("INFO,EXP_SCHEME=V8,EXP_TRANSITION=CLOSE_AT_ATM_CUT,EXIT=ARRIVAL_AND_DWELL_CFG"));
  Serial.println(F("INFO,OVERCLOSE_V=CFG,CYCLE_TIMING=CFG_WITHIN_CYCLE,SPLIT=IE_AFTER_TRANSITIONS"));
  Serial.println(F("INFO,INSP_TRANSITION=ARRIVAL_AND_DWELL_CFG"));
  Serial.println(F("INFO,EXP_TRANSITION_EXIT=ARRIVAL_PLUS_SETTLE_CFG,FLOOR=DWELL_CFG"));
  Serial.println(F("INFO,EXP_CLOSURE=CFG_SHIFTED_VAC_MAP,INSP_CLOSURE=CLOSURE_V"));
  Serial.println(F("INFO,EXP_LIMIT=OPENING,CAP=0.1883:0.3451:0.4707,MS=250:750:END"));
  Serial.println(F("INFO,VAC_FLOW_GUARD=0,EXP_TAPER=0,EXP_SLEW=0"));
}

static void handleCommand(const String &raw) {
  String line = raw;
  line.trim();
  if (!line.length()) return;
  gLastHostMs = millis();
  if (line == "HELLO") {
    if (!gRunning && gState != FAULT) idleOutputs();
    printReady();
  } else if (line == "PING") {
    Serial.println(F("PONG"));
  } else if (line == "STOP") {
    uint32_t run = gRunId;
    if (gRunning && gState != FAULT) {
      if (gState == RECOVERY) {
        // A Stop press during automatic end recovery changes only the final
        // acknowledgement; do not restart the recovery timeout.
        gRecoveryRequestedByStop = true;
        Serial.print(F("RECOVERING,"));
        Serial.print(run);
        Serial.println(F(",STOP"));
      } else {
        beginRecovery(true);
      }
    } else {
      stopRun();
      Serial.print(F("STOPPED,"));
      Serial.println(run);
    }
  } else if (line.startsWith("CFG,")) {
    applyConfig(line);
  } else if (line.startsWith("START,")) {
    String f[4];
    uint8_t count = splitCsv(line, f, 4);
    if (count != 3) Serial.println(F("ERR,START_FORMAT"));
    else startRun((uint32_t)f[1].toInt(), (uint16_t)f[2].toInt());
  } else if (line.startsWith("MANUAL,")) {
    if (gRunning) { Serial.println(F("ERR,MANUAL_RUNNING")); return; }
    if (gState == FAULT) {
      safeOutputs(); Serial.println(F("ERR,MANUAL_FAULT")); return;
    }
    // Always close the opposite route before opening a solenoid.
    if (line == "MANUAL,ATM,OPEN") { closeDirectionalValves(); applyValves(true, false); }
    else if (line == "MANUAL,VAC,OPEN") { closeDirectionalValves(); applyValves(false, true); }
    else if (line == "MANUAL,ATM,CLOSE") applyValves(false, (gValveMask & 2) != 0);
    else if (line == "MANUAL,VAC,CLOSE") applyValves((gValveMask & 1) != 0, false);
    else if (line == "MANUAL,PPV,OPEN") writePinchCommand(1.0f);
    else { Serial.println(F("ERR,MANUAL_FORMAT")); return; }
    Serial.print(F("ACK,MANUAL,")); Serial.print(gValveMask);
    Serial.print(','); Serial.println(gCommandV, 3);
  } else if (line == "STATUS") {
    Serial.print(F("STATUS,"));
    Serial.print(gConfigValid ? 1 : 0);
    Serial.print(',');
    Serial.print(gRunning ? 1 : 0);
    Serial.print(',');
    Serial.print((uint8_t)gState);
    Serial.print(',');
    Serial.println(gCycle);
  } else {
    Serial.println(F("ERR,UNKNOWN_COMMAND"));
  }
}

static void readSerial() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n') {
      gRx[gRxLen] = '\0';
      handleCommand(String(gRx));
      gRxLen = 0;
    } else if (c != '\r') {
      if (gRxLen + 1 < sizeof(gRx)) gRx[gRxLen++] = c;
      else {
        gRxLen = 0;
        Serial.println(F("ERR,LINE_TOO_LONG"));
      }
    }
  }
}

static void printTelemetry() {
  Serial.print(F("TEL,"));
  Serial.print(gRunId); Serial.print(',');
  Serial.print(millis() - gRunStartMs); Serial.print(',');
  Serial.print((uint8_t)gState); Serial.print(',');
  Serial.print(gCycle); Serial.print(',');
  Serial.print(gFlowSlm, 4); Serial.print(',');
  Serial.print(gVolumeMl, 1); Serial.print(',');
  Serial.print(gFlowRefSlm, 2); Serial.print(',');
  Serial.print(gOpening, 3); Serial.print(',');
  Serial.print(gCommandV, 3); Serial.print(',');
  Serial.print(gValveMask); Serial.print(',');
  Serial.print(gEstimatedPpvV, 3); Serial.print(',');
  Serial.print(ppvTravelSeconds(gEstimatedPpvV, gCommandV,
                                 gCfg.ppvOpenTimeS, gCfg.ppvCloseTimeS), 3);
  Serial.print(','); Serial.print(gRawFlowSlm, 4);
  Serial.print(','); Serial.print(gNetFlowSlm, 4);
  Serial.print(','); Serial.print(gSampledValveMask);
  Serial.print(','); Serial.print(gControlFeedbackSlm, 4);
  Serial.print(','); Serial.print(gSmithIdealFlowSlm, 4);
  Serial.print(','); Serial.print(gSmithDelayedFlowSlm, 4);
  Serial.print(','); Serial.print(gCycleInspiredMl, 1);
  Serial.print(','); Serial.print(gCycleExpiredMl, 1);
  Serial.print(','); Serial.print(gExpirationBudgetMl, 1);
  Serial.print(','); Serial.println(0);  // Former exhausted flag; v8 VAC never latches closed.
}

static void updateStateMachine() {
  uint32_t elapsedMs = millis() - gStateStartMs;
  trackTransitionArrival();
  switch (gState) {
    case PRECONDITION:
      if (elapsedMs >= (uint32_t)(gCfg.preconditionS * 1000.0f)) {
        gVolumeMl = 0.0f;
        setState(DEAD_TO_INSP);
      }
      break;
    case DEAD_TO_INSP:
      // Both routes closed since entry: also wait out the VAC flow tail.
      if (ppvTransitionReady(gEstimatedPpvV, gCommandV, elapsedMs,
                             gCfg.inspirationDwellMinS * 1000.0f))
        setState(INSPIRATION);
      break;
    case INSPIRATION:
      if (elapsedMs >= gInspirationDurationMs) setState(DEAD_TO_EXP);
      break;
    case DEAD_TO_EXP:
      // Response-based: stroke actually needed (estimated arrival) + settle.
      if (ppvTransitionSettled(gPpvCloseArrived, millis() - gPpvCloseArrivedMs,
                               gCfg.transitionSettleS * 1000.0f,
                               millis() - gPpvCloseCommandMs,
                               gCfg.transitionDwellMinS * 1000.0f))
        setState(EXPIRATION);
      break;
    case EXPIRATION: {
      bool expirationTimeReached = elapsedMs >= gExpirationDurationMs;
      // The integrated volume is a model estimate, not an end-of-phase switch.
      // Keep the user-selected expiration duration even when it reaches zero.
      if (expirationTimeReached) {
        gCycle++;
        if (gCycle >= gRequestedCycles) {
          beginRecovery(false);
        } else {
          setState(DEAD_TO_INSP);
        }
      }
      break;
    }
    case RECOVERY:
      if (gVolumeMl >= gCfg.targetMl || elapsedMs >= RECOVERY_TIMEOUT_MS) {
        finishRecovery(elapsedMs >= RECOVERY_TIMEOUT_MS
                       && gVolumeMl < gCfg.targetMl);
      }
      break;
    default:
      break;
  }
}

static void controlStep() {
  advancePpvPosition();  // Also track motion while idle, stopped or between phases.
  // Idle manual valves also require a live host heartbeat.
  if (!gRunning && gValveMask && millis() - gLastHostMs > HOST_WATCHDOG_MS) {
    enterFault(F("HOST_TIMEOUT"));
    return;
  }
  float rawFlow = 0.0f;
  if (!sfmRead(rawFlow)) {
    if (gSensorFailures < SENSOR_FAILURE_LIMIT) ++gSensorFailures;
    if (gSensorFailures >= SENSOR_FAILURE_LIMIT) enterFault(F("SFM_READ"));
    return;
  }
  gSensorFailures = 0;
  gRawFlowSlm = rawFlow;
  gFlowSlm = calibratedFlow(rawFlow, gCfg.flowOffsetSlm, gCfg.flowGain, gCfg.flowSign);
  gSampledValveMask = gValveMask;  // Routing that produced this sample, before new outputs.
  float effectiveVacLeakSlm = 0.0f;
  if (gSampledValveMask == 2) {
    effectiveVacLeakSlm = vacLeakAtVoltage(
        gCfg.vacLeakSlm, gEstimatedPpvV,
        gCfg.expirationClosureV - VAC_LEAK_ROLLOFF_WIDTH_V, gCfg.expirationClosureV);
  }
  gNetFlowSlm = netLungFlow(
      gFlowSlm, gSampledValveMask, effectiveVacLeakSlm);
  // Independent cycle integrals use the route which produced this sample.
  // Do not infer tidal exchange from the clipped total-volume counter.
  if (gRunning && gState == INSPIRATION && gSampledValveMask == 1)
    gCycleInspiredMl += max(0.0f, gNetFlowSlm) * ML_PER_S_PER_SLM * DT;
  if (gRunning && gState == EXPIRATION && gSampledValveMask == 2) {
    gCycleExpiredMl += max(0.0f, -gNetFlowSlm) * ML_PER_S_PER_SLM * DT;
    gCycleExpiredRawMl += max(0.0f, -gFlowSlm) * ML_PER_S_PER_SLM * DT;
  }
  // Compare the current sample against the PREVIOUS interval command/routing.
  if (gCfg.smithEnabled && gRunning && (gState == INSPIRATION || gState == EXPIRATION) && gValveMask != 0) {
    int8_t previousDirection = gValveMask == 1 ? 1 : (gValveMask == 2 ? -1 : 0);
    gControlFeedbackSlm = updateSmith(previousDirection, gNetFlowSlm);
  } else {
    gControlFeedbackSlm = gNetFlowSlm;
    gSmithIdealFlowSlm = 0.0f;
    gSmithDelayedFlowSlm = 0.0f;
  }

  if (!gRunning) return;
  if (millis() - gLastHostMs > HOST_WATCHDOG_MS) {
    enterFault(F("HOST_TIMEOUT"));
    return;
  }
  if (fabsf(gFlowSlm) > gCfg.maxAbsFlowSlm) {
    enterFault(F("FLOW_LIMIT"));
    return;
  }

  State previousState = gState;
  updateStateMachine();
  if (!gRunning) return;
  bool phaseJustStarted = previousState != gState;

  gFlowRefSlm = 0.0f;
  bool atmOpen = false;
  bool vacOpen = false;
  float opening = gOpening;
  int8_t direction = 0;

  if (gState == PRECONDITION) {
    vacOpen = true;
    opening = 1.0f;
    direction = -1;
  } else if (gState == INSPIRATION) {
    gVolumeMl += max(0.0f, gNetFlowSlm) * ML_PER_S_PER_SLM * DT;
    gVolumeMl = clampf(gVolumeMl, 0.0f, volumeGuardMl());
    float qRef = 0.06f * gCfg.targetMl / gInspirationFillS;
    gFlowRefSlm = qRef;
    if (gCycleInspiredMl < gCfg.targetMl && gVolumeMl < volumeGuardMl()) {
      atmOpen = true;
      direction = 1;
      float ff = inspirationFeedforwardOpening(gVolumeMl);
      float feedback = gCfg.smithEnabled ? gControlFeedbackSlm : gNetFlowSlm;
      if (!phaseJustStarted)
        opening = updatePID(qRef, max(0.0f, feedback), ff, inspirationMaxOpening());
    } else {
      // ATM is cut: the PPV no longer affects flow, so start closing now
      // instead of holding the end-of-inspiration opening until the timer.
      commandTransitionClose();
    }
  } else if (gState == EXPIRATION) {
    // Safety estimate: uncompensated sensor flow (leak model not trusted).
    gVolumeMl += min(0.0f, gFlowSlm) * ML_PER_S_PER_SLM * DT;
    gVolumeMl = clampf(gVolumeMl, 0.0f, volumeGuardMl());
    // Same duration as the phase timer; the GUI scores this transmitted value.
    float qRef = 0.06f * gCfg.targetMl / max(0.25f, gExpirationDurationS);
    gFlowRefSlm = -qRef;
    // v8 scheme: VAC stays open for the whole timed phase. PI starts from zero
    // opening (closureV) without FF; the time-staged cap bounds its authority.
    vacOpen = true;
    direction = -1;
    float feedback = gCfg.smithEnabled ? gControlFeedbackSlm : gNetFlowSlm;
    if (!phaseJustStarted) {
      float openingLimit = ppvExpirationOpeningLimit(
          millis() - gStateStartMs, gExpirationDurationS * 1000.0f);
      opening = updatePID(qRef, max(0.0f, -feedback), 0.0f, openingLimit,
                          gCfg.expirationIntegralLimit);
    }
  } else if (gState == RECOVERY) {
    // Restore only the missing bag volume.  Negative/noisy samples cannot
    // reduce the recovery estimate, and the state timeout remains authoritative.
    gVolumeMl += max(0.0f, gNetFlowSlm) * ML_PER_S_PER_SLM * DT;
    gVolumeMl = clampf(gVolumeMl, 0.0f, volumeGuardMl());
    if (gVolumeMl < gCfg.targetMl) {
      atmOpen = true;
      opening = 1.0f;
      direction = 1;
    }
  }

  // Hold the exact voltage through positioning and closed-valve holds.
  // Remapping a VAC opening with the ATM curve here would move the target.
  if (direction != 0) writePinchCommand(opening, direction);
  applyValves(atmOpen, vacOpen);
  if ((gStep++ % TELEMETRY_DIVIDER) == 0) printTelemetry();
}

void setup() {
  digitalWrite(PIN_ATM, VALVE_OFF);
  digitalWrite(PIN_VAC, VALVE_OFF);
  pinMode(PIN_ATM, OUTPUT);
  pinMode(PIN_VAC, OUTPUT);
  pinMode(PIN_PPV_PWM, OUTPUT);
  analogWriteResolution(12);
  idleOutputs();
  digitalWrite(SDA_PIN, HIGH); pinMode(SDA_PIN, OUTPUT_OPEN_DRAIN);
  digitalWrite(SCL_PIN, HIGH); pinMode(SCL_PIN, OUTPUT_OPEN_DRAIN);
  for (uint8_t i = 0; i < 9; ++i) { sclLow(); sclHigh(); }
  busStop();
  Serial.begin(BAUD);
  bool sensorStarted = sfmStart();
  delay(100);
  float discarded = 0.0f;
  if (sensorStarted) sfmRead(discarded);
  if (!sensorStarted) Serial.println(F("WARN,SFM_START"));
  gNextControlUs = micros() + CONTROL_PERIOD_US;
  gLastHostMs = millis();
  printReady();
}

void loop() {
  readSerial();
  uint32_t now = micros();
  if ((int32_t)(now - gNextControlUs) >= 0) {
    gNextControlUs += CONTROL_PERIOD_US;
    if ((int32_t)(now - gNextControlUs) > (int32_t)(3UL * CONTROL_PERIOD_US)) {
      gNextControlUs = now + CONTROL_PERIOD_US;
    }
    controlStep();
  }
}
