/*
 * LungLogic single-lung controller for NUCLEO-G474RE.
 * Protocol: READY,LUNGLOGIC_SINGLE_G474,8 / CFG / START / STOP / PING / TEL
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
#ifndef USE_D8_ESTOP_INPUT
#define USE_D8_ESTOP_INPUT 1
#endif

static const uint32_t BAUD = 115200;
static const uint16_t CONTROL_PERIOD_US = 10000;  // 100 Hz
static const uint8_t TELEMETRY_DIVIDER = 2;       // 50 Hz

// Final NUCLEO-G474RE PCB pin allocation, left branch.
static const uint32_t PIN_ESTOP = D8;       // Active-low emergency stop input.
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
static const float DT = 0.010f;
static const float ML_PER_S_PER_SLM = 1000.0f / 60.0f;
static const uint32_t HOST_WATCHDOG_MS = 5000;
// A normal stop restores the bag before isolating it.  The timeout guarantees
// that a disconnected/blocked atmosphere route cannot leave ATM energized.
static const uint32_t RECOVERY_TIMEOUT_MS = 5000;
static const float END_VOLUME_ML = 5.0f;
static const uint32_t MIN_EXPIRATION_MS = 250;
// 40 kPa bench data: suppress the initial VAC transient for 250 ms, release
// useful flow authority by 750 ms, then compensate for the falling pressure
// gradient through the remainder of expiration.
// Preserve the existing voltage envelope when the VAC model is recalibrated.
// These were openings .12/.22/.30 under Ud=4.08, Uc=4.35.
static const float INITIAL_EXPIRATION_MIN_V = 4.3176f;
static const float MID_EXPIRATION_MIN_V = 4.2906f;
static const float FINAL_EXPIRATION_MIN_V = 4.2690f;
static const uint32_t EXPIRATION_LIMIT_HOLD_MS = 250;
static const uint32_t EXPIRATION_LIMIT_RAMP_MS = 750;
static const float EXPIRATION_INTEGRAL_LIMIT = 4.0f;
// Measured VAC bypass stays flat until the last 50 mV before full closure.
static const float VAC_LEAK_ROLLOFF_WIDTH_V = 0.05f;

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
  0.01f, 0.06f, 0.0f, 4.0f, 2.0f, 220.0f, 250.0f, 1,
  // October 12 replacement hardware; independent directional fits.
  // Plateau time constants are extrapolated, not measured full-open values.
  323.9f, 0.743f, 0.2678f, 1.496f, true, 0.0f, 0.6f,
  2.0621f, 4.0986f, 4.1779f, 4.35f, 2.436f, 2.436f, 0.0034f, 1.0471f, 0.814f
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

static float gFlowSlm = 0.0f;  // Calibrated sensor flow, before leakage compensation.
static float gRawFlowSlm = 0.0f;
static float gNetFlowSlm = 0.0f;
static uint8_t gSampledValveMask = 0;
static float gVolumeMl = 0.0f;
static float gFlowRefSlm = 0.0f;
static float gControlFeedbackSlm = 0.0f;
static float gOpening = 0.0f;
static float gCommandV = 5.0f;
static uint8_t gValveMask = 0;
static PIDState gPid = {0.0f, 0.0f, false};
static float gSmithVolumeMl = 0.0f;
static float gSmithActuatorVolumeMl = 0.0f;
static float gSmithIdealFlowSlm = 0.0f;
static float gSmithDelayedFlowSlm = 0.0f;
static float gEstimatedPpvV = 5.0f;  // Initial closed-position assumption.
static uint32_t gPpvUpdateUs = 0;
static float gSmithDelayLine[120];
static uint8_t gSmithDelayCount = 0;
static uint8_t gSmithDelayIndex = 0;
static char gRx[320];
static uint16_t gRxLen = 0;

static float clampf(float value, float low, float high) {
  if (value < low) return low;
  if (value > high) return high;
  return value;
}

static bool estopActive() {
#if USE_D8_ESTOP_INPUT
  return digitalRead(PIN_ESTOP) == LOW;
#else
  return false;
#endif
}

static uint16_t voltsToPwm(float volts) {
  return (uint16_t)lroundf(clampf(volts, 0.0f, 5.0f) * 4095.0f / 5.0f);
}

static float directionGamma(int8_t direction) {
  return direction < 0 ? gCfg.gammaOut : gCfg.valveExponent;
}

static float openingCommandV(float opening, int8_t direction) {
  opening = clampf(opening, 0.0f, 1.0f);
  float deadZone = direction < 0 ? gCfg.deadZoneOutV : gCfg.deadZoneInV;
  return ppvCommandVoltage(opening, deadZone, gCfg.closureV);
}

static float inspirationFeedforwardOpening(float volumeMl) {
  float qRef = 0.06f * gCfg.targetMl / gCfg.fillTimeS;
  float qMax = max(0.1f, 0.06f * max(0.0f, gCfg.capacityMl - volumeMl) / gCfg.tauInS);
  return powf(min(1.0f, qRef / qMax), 1.0f / directionGamma(1));
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
  // Opening is the effective coordinate of the directional empirical model.
  // Normal regulation closes at measured closureV. Fault closure uses 5 V separately.
  gCommandV = openingCommandV(gOpening, direction);
  analogWrite(PIN_PPV_PWM, voltsToPwm(gCommandV));
}

static void closeDirectionalValves() {
  digitalWrite(PIN_ATM, VALVE_OFF);
  digitalWrite(PIN_VAC, VALVE_OFF);
  gValveMask = 0;
}

static void safeOutputs() {
  closeDirectionalValves();
  advancePpvPosition();
  gOpening = 0.0f;
  gCommandV = 5.0f;
  analogWrite(PIN_PPV_PWM, voltsToPwm(gCommandV));
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
  gSmithDelayCount = (uint8_t)min(119, max(0, (int)lroundf(gCfg.smithDelayS / DT)));
  for (uint8_t i = 0; i < 120; ++i) gSmithDelayLine[i] = 0.0f;
  gControlFeedbackSlm = gNetFlowSlm;
}

static float smithModelFlow(float volts, int8_t direction, float &volume) {
  float deadZone = direction < 0 ? gCfg.deadZoneOutV : gCfg.deadZoneInV;
  float effective = powf(ppvEffectiveOpening(volts, deadZone, gCfg.closureV),
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
    gSmithDelayIndex = (uint8_t)((gSmithDelayIndex + 1) % gSmithDelayCount);
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
  if (integralLimit <= 0.0f) integralLimit = gCfg.integralLimit;
  float error = reference - measurement;
  if (!gPid.hasBias) {
    gPid.bias = gPid.heldOpening - feedforward - gCfg.kp * error;
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
  float raw = gPid.bias + feedforward + gCfg.kp * error + gCfg.ki * candidate + gCfg.kd * derivative;
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

static void setState(State next) {
  closeDirectionalValves();
  if (next == DEAD_TO_INSP) {
    writePinchCommand(inspirationFeedforwardOpening(gVolumeMl), 1);
  } else if (next == DEAD_TO_EXP) {
    // Start expiration from the measured stop-flow position.  The former
    // volume-dependent feedforward increased opening as volume fell, but the
    // bench VAC flow does not follow that model and the lung emptied much
    // faster than the requested reference.  Let measured-flow feedback open
    // the PPV conservatively from zero instead.
    writePinchCommand(0.0f, -1);
  }
  resetPID();
  resetSmith(gVolumeMl);
  gState = next;
  gStateStartMs = millis();
}

// Immediate shutdown is reserved for reconfiguration and safety paths.  User
// STOP and normal completion use beginRecovery() below.
static void stopRun() {
  gRunning = false;
  gState = IDLE;
  if (estopActive()) safeOutputs();
  else idleOutputs();
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
  if (!(c.rrBpm >= 1.0f && c.rrBpm <= 40.0f)) return false;
  if (!(c.ieIn > 0.0f && c.ieOut > 0.0f)) return false;
  if (!(c.targetMl > 0.0f && c.targetMl <= c.maxVolumeMl)) return false;
  float ti = (60.0f / c.rrBpm) * c.ieIn / (c.ieIn + c.ieOut);
  if (!(c.fillTimeS > 0.0f && c.fillTimeS <= ti)) return false;
  if (c.deadTimeMs != 0) return false; // PPV positioning replaces fixed dead time.
  if (!(c.kp >= 0.0f && c.ki >= 0.0f && c.kd >= 0.0f && c.integralLimit > 0.0f)) return false;
  if (!(c.preconditionS >= 0.0f && c.preconditionS <= 10.0f)) return false;
  if (!(c.maxAbsFlowSlm > 0.0f && c.maxAbsFlowSlm <= 250.0f)) return false;
  if (!(c.flowSign == 1 || c.flowSign == -1)) return false;
  // Model amplitude and integrated-volume guard are separate quantities.
  if (!(c.capacityMl >= c.targetMl && c.capacityMl <= 5000.0f)) return false;
  if (!(c.tauInS > 0.0f && c.tauOutS > 0.0f)) return false;
  if (!(c.valveExponent >= 0.05f && c.valveExponent <= 5.0f)) return false;
  if (!(c.gammaOut >= 0.05f && c.gammaOut <= 5.0f)) return false;
  if (!(c.deadZoneInV >= 0.0f && c.deadZoneInV < c.closureV)) return false;
  if (!(c.deadZoneOutV >= 0.0f && c.deadZoneOutV < c.closureV)) return false;
  if (!(c.closureV > 0.0f && c.closureV <= 5.0f)) return false;
  if (!(c.smithDelayS >= 0.0f && c.smithDelayS <= 1.0f)) return false;
  if (!(c.smithCorrectionGain >= 0.0f && c.smithCorrectionGain <= 2.0f)) return false;
  if (!(c.ppvOpenTimeS > 0.0f && c.ppvOpenTimeS <= 60.0f)) return false;
  if (!(c.ppvCloseTimeS > 0.0f && c.ppvCloseTimeS <= 60.0f)) return false;
  if (!(c.flowOffsetSlm >= -5.0f && c.flowOffsetSlm <= 5.0f)) return false;
  if (!(c.flowGain >= 0.1f && c.flowGain <= 5.0f)) return false;
  if (!(c.vacLeakSlm >= 0.0f && c.vacLeakSlm <= 20.0f)) return false;
  if (c.smithEnabled && (c.ppvOpenTimeS <= 0.0f || c.ppvCloseTimeS <= 0.0f)) return false;
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
  String f[34];
  uint8_t count = splitCsv(line, f, 34);
  if (count != 33 || f[1].toInt() != 8) {
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
  if (estopActive()) {
    Serial.println(F("ERR,ESTOP_ACTIVE"));
    safeOutputs();
    return;
  }
  if (!gConfigValid) {
    Serial.println(F("ERR,CONFIG_REQUIRED"));
    return;
  }
  if (gRunning) {
    Serial.println(F("ERR,ALREADY_RUNNING"));
    return;
  }
  gRunId = runId;
  gRequestedCycles = constrain(cycles, 1, 1000);
  gCycle = 0;
  gStep = 0;
  gVolumeMl = 0.0f;
  gRunStartMs = millis();
  gLastHostMs = millis();
  gRunning = true;
  setState(PRECONDITION);
  Serial.print(F("STARTED,"));
  Serial.println(gRunId);
}

static void handleCommand(const String &raw) {
  String line = raw;
  line.trim();
  if (!line.length()) return;
  gLastHostMs = millis();
  if (line == "HELLO") {
    if (!gRunning && gState != FAULT && !estopActive()) idleOutputs();
    Serial.println(F("READY,LUNGLOGIC_SINGLE_G474,8"));
  } else if (line == "PING") {
    Serial.println(F("PONG"));
  } else if (line == "STOP") {
    uint32_t run = gRunId;
    if (gRunning && !estopActive() && gState != FAULT) {
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
    if (estopActive() || gState == FAULT) {
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
  Serial.print(','); Serial.println(gSmithDelayedFlowSlm, 4);
}

static void updateStateMachine() {
  uint32_t elapsedMs = millis() - gStateStartMs;
  switch (gState) {
    case PRECONDITION:
      if (elapsedMs >= (uint32_t)(gCfg.preconditionS * 1000.0f)) {
        gVolumeMl = 0.0f;
        setState(DEAD_TO_INSP);
      }
      break;
    case DEAD_TO_INSP:
      if (fabsf(gEstimatedPpvV - gCommandV) <= 0.0001f) setState(INSPIRATION);
      break;
    case INSPIRATION:
      if (elapsedMs >= (uint32_t)(inspirationS() * 1000.0f)) setState(DEAD_TO_EXP);
      break;
    case DEAD_TO_EXP:
      if (fabsf(gEstimatedPpvV - gCommandV) <= 0.0001f) setState(EXPIRATION);
      break;
    case EXPIRATION: {
      bool expirationTimeReached = elapsedMs >= (uint32_t)(expirationS() * 1000.0f);
      bool lungEmptyAfterMinimumTime =
          elapsedMs >= MIN_EXPIRATION_MS && gVolumeMl <= END_VOLUME_ML;
      if (expirationTimeReached || lungEmptyAfterMinimumTime) {
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
  if (estopActive()) {
    safeOutputs();
    if (gState != FAULT) enterFault(F("ESTOP"));
    return;
  }

  // Idle manual valves also require a live host heartbeat.
  if (!gRunning && gValveMask && millis() - gLastHostMs > HOST_WATCHDOG_MS) {
    enterFault(F("HOST_TIMEOUT"));
    return;
  }
  float rawFlow = 0.0f;
  if (!sfmRead(rawFlow)) {
    if (++gSensorFailures >= 5) enterFault(F("SFM_READ"));
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
        gCfg.closureV - VAC_LEAK_ROLLOFF_WIDTH_V, gCfg.closureV);
  }
  gNetFlowSlm = netLungFlow(
      gFlowSlm, gSampledValveMask, effectiveVacLeakSlm);
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
    gVolumeMl = clampf(gVolumeMl, 0.0f, gCfg.maxVolumeMl);
    float qRef = 0.06f * gCfg.targetMl / gCfg.fillTimeS;
    gFlowRefSlm = qRef;
    if (gVolumeMl < gCfg.targetMl) {
      atmOpen = true;
      direction = 1;
      float ff = inspirationFeedforwardOpening(gVolumeMl);
      float feedback = gCfg.smithEnabled ? gControlFeedbackSlm : gNetFlowSlm;
      if (!phaseJustStarted) opening = updatePID(qRef, max(0.0f, feedback), ff);
    }
  } else if (gState == EXPIRATION) {
    gVolumeMl += min(0.0f, gNetFlowSlm) * ML_PER_S_PER_SLM * DT;
    gVolumeMl = clampf(gVolumeMl, 0.0f, gCfg.maxVolumeMl);
    float usable = max(0.25f, expirationS());
    float qRef = 0.06f * gCfg.targetMl / usable;
    gFlowRefSlm = -qRef;
    if (gVolumeMl > END_VOLUME_ML) {
      vacOpen = true;
      direction = -1;
      float feedback = gCfg.smithEnabled ? gControlFeedbackSlm : gNetFlowSlm;
      if (!phaseJustStarted) {
        float elapsedMs = (float)(millis() - gStateStartMs);
        float expirationMs = max(250.0f, expirationS() * 1000.0f);
        float initialLimit = ppvEffectiveOpening(INITIAL_EXPIRATION_MIN_V, gCfg.deadZoneOutV, gCfg.closureV);
        float midLimit = ppvEffectiveOpening(MID_EXPIRATION_MIN_V, gCfg.deadZoneOutV, gCfg.closureV);
        float finalLimit = ppvEffectiveOpening(FINAL_EXPIRATION_MIN_V, gCfg.deadZoneOutV, gCfg.closureV);
        float openingLimit;
        if (elapsedMs <= (float)EXPIRATION_LIMIT_HOLD_MS) {
          // Keep the measured 0-0.2 s vacuum spike from opening the PPV early.
          openingLimit = initialLimit;
        } else if (elapsedMs <= (float)EXPIRATION_LIMIT_RAMP_MS) {
          // Once the transient has passed, reach the experimentally indicated
          // Directional model maps this opening limit to voltage without a step.
          float releaseProgress = (elapsedMs - EXPIRATION_LIMIT_HOLD_MS)
              / (float)(EXPIRATION_LIMIT_RAMP_MS - EXPIRATION_LIMIT_HOLD_MS);
          openingLimit = initialLimit + releaseProgress * (midLimit - initialLimit);
        } else {
          // Continue releasing authority as bag pressure and VAC flow fall.
          float lateProgress = clampf(
              (elapsedMs - EXPIRATION_LIMIT_RAMP_MS)
                  / max(1.0f, expirationMs - EXPIRATION_LIMIT_RAMP_MS),
              0.0f, 1.0f);
          openingLimit = midLimit + lateProgress * (finalLimit - midLimit);
        }
        opening = updatePID(qRef, max(0.0f, -feedback), 0.0f,
                            openingLimit, EXPIRATION_INTEGRAL_LIMIT);
      }
    }
  } else if (gState == RECOVERY) {
    // Restore only the missing bag volume.  Negative/noisy samples cannot
    // reduce the recovery estimate, and the state timeout remains authoritative.
    gVolumeMl += max(0.0f, gNetFlowSlm) * ML_PER_S_PER_SLM * DT;
    gVolumeMl = clampf(gVolumeMl, 0.0f, gCfg.maxVolumeMl);
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
#if USE_D8_ESTOP_INPUT
  pinMode(PIN_ESTOP, INPUT_PULLUP);
#endif
  digitalWrite(PIN_ATM, VALVE_OFF);
  digitalWrite(PIN_VAC, VALVE_OFF);
  pinMode(PIN_ATM, OUTPUT);
  pinMode(PIN_VAC, OUTPUT);
  pinMode(PIN_PPV_PWM, OUTPUT);
  analogWriteResolution(12);
  if (estopActive()) safeOutputs();
  else idleOutputs();
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
  Serial.println(F("READY,LUNGLOGIC_SINGLE_G474,8"));
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
