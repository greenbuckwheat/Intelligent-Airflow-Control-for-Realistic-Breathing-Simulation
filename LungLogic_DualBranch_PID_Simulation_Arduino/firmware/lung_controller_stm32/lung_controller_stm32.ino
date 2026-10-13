/*
 * LungLogic dual-branch controller for NUCLEO-G474RE.
 * Protocol: READY,LUNGLOGIC_G474,2 / CFG / START / STOP / PING / TEL
 *
 * Pneumatic convention:
 *   inspiration = atmosphere valve open, vacuum valve closed
 *   expiration  = atmosphere valve closed, vacuum valve open
 *   dead time   = both directional valves closed
 *
 * IMPORTANT: 24 V valves and PPV40 power are never driven directly by MCU pins.
 * Use four logic-level MOSFET stages with flyback protection for the solenoids.
 * D3/D5 produce 3.3 V PWM duty commands. External filtering/level conversion
 * must provide each PPV's 0-5 V input (0 V=open, 5 V=closed).
 * Routing loads are Norgren 8254200.9151.02400, 24 VDC, 2/2 NC,
 * approximately 18 W each. GPIO drives only the PCB MOSFET stages.
 */

#include <Arduino.h>

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

// Final NUCLEO-G474RE PCB pin allocation.
static const uint32_t PIN_ESTOP = D8;
static const uint32_t PIN_L_ATM = D11;
static const uint32_t PIN_L_VAC = D13;
static const uint32_t PIN_R_ATM = D10;
static const uint32_t PIN_R_VAC = D12;
static const uint32_t PIN_L_PPV_PWM = D5;
static const uint32_t PIN_R_PPV_PWM = D3;
static const uint32_t SDA_PIN = A4;       // PCB-routed software I2C: PC1.
static const uint32_t SCL_PIN = A5;       // PCB-routed software I2C: PC0.
static const uint8_t VALVE_ON = VALVE_ACTIVE_HIGH ? HIGH : LOW;
static const uint8_t VALVE_OFF = VALVE_ACTIVE_HIGH ? LOW : HIGH;

static const uint8_t TCA_ADDR = 0x70;
static const uint8_t SFM_ADDR = 0x40;
static const uint8_t SFM_LEFT_CHANNEL = 0;
static const uint8_t SFM_RIGHT_CHANNEL = 1;

static const float SFM_SCALE = 120.0f;
static const float SFM_OFFSET = 32768.0f;
static const float DT = 0.010f;
static const float ML_PER_S_PER_SLM = 1000.0f / 60.0f;
static const uint32_t HOST_WATCHDOG_MS = 5000;
static const float END_VOLUME_ML = 5.0f;

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
static bool busCommand(uint8_t address, const uint8_t *data, uint8_t count) {
  bool ok = busStart() && busWrite(address << 1);
  for (uint8_t i = 0; ok && i < count; ++i) ok = busWrite(data[i]);
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
  FAULT = 7
};

struct Config {
  float rrBpm;
  float ieIn;
  float ieOut;
  float targetLeftMl;
  float targetRightMl;
  float fillTimeS;
  uint16_t deadTimeMs;
  float kp;
  float ki;
  float kd;
  float integralLimit;
  float preconditionS;
  float maxAbsFlowSlm;
  float maxVolumeMl;
  int8_t leftFlowSign;
  int8_t rightFlowSign;
  float capacityLeftMl;
  float capacityRightMl;
  float tauInLeftS;
  float tauInRightS;
  float tauOutLeftS;
  float tauOutRightS;
  float valveExponent;
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
  15.0f, 1.0f, 2.0f, 250.0f, 250.0f, 1.30f, 80,
  0.022f, 0.16f, 0.0f, 2.0f, 1.50f, 220.0f, 320.0f, 1, 1,
  558.0f, 558.0f, 0.57f, 0.60f, 0.50f, 0.54f, 1.15f
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

static float gQLeft = 0.0f;
static float gQRight = 0.0f;
static float gVLeft = 0.0f;
static float gVRight = 0.0f;
static float gQRefLeft = 0.0f;
static float gQRefRight = 0.0f;
static float gOpeningLeft = 0.0f;
static float gOpeningRight = 0.0f;
static float gCmdLeftV = 5.0f;
static float gCmdRightV = 5.0f;
static uint8_t gValveMask = 0;
static PIDState gPidLeft = {0.0f, 0.0f, false};
static PIDState gPidRight = {0.0f, 0.0f, false};
static char gRx[220];
static uint8_t gRxLen = 0;

static float clampf(float value, float low, float high) {
  if (value < low) return low;
  if (value > high) return high;
  return value;
}

static void closeAllDirectionalValves() {
  digitalWrite(PIN_L_ATM, VALVE_OFF);
  digitalWrite(PIN_L_VAC, VALVE_OFF);
  digitalWrite(PIN_R_ATM, VALVE_OFF);
  digitalWrite(PIN_R_VAC, VALVE_OFF);
  gValveMask = 0;
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

static void writePinchCommands(float leftOpening, float rightOpening) {
  gOpeningLeft = clampf(leftOpening, 0.0f, 1.0f);
  gOpeningRight = clampf(rightOpening, 0.0f, 1.0f);
  // PPV40V convention supplied for this project: 0 V fully open, 5 V fully closed.
  gCmdLeftV = 5.0f * (1.0f - gOpeningLeft);
  gCmdRightV = 5.0f * (1.0f - gOpeningRight);
  analogWrite(PIN_L_PPV_PWM, voltsToPwm(gCmdLeftV));
  analogWrite(PIN_R_PPV_PWM, voltsToPwm(gCmdRightV));
}

static void safeOutputs() {
  closeAllDirectionalValves();
  writePinchCommands(0.0f, 0.0f);
}

static bool tcaSelect(uint8_t channel) {
  if (channel > 7) return false;
  uint8_t value = (uint8_t)(1U << channel);
  return busCommand(TCA_ADDR, &value, 1);
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

static bool sfmStart(uint8_t muxChannel) {
  if (!tcaSelect(muxChannel)) return false;
  const uint8_t reset[] = {0x20, 0x00};
  if (!busCommand(SFM_ADDR, reset, 2)) return false;
  delay(100);
  if (!tcaSelect(muxChannel)) return false;
  const uint8_t start[] = {0x10, 0x00};
  return busCommand(SFM_ADDR, start, 2);
}

static bool sfmRead(uint8_t muxChannel, float &flowSlm) {
  if (!tcaSelect(muxChannel)) return false;
  uint8_t bytes[3];
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

static void resetPID() {
  gPidLeft.integral = 0.0f;
  gPidLeft.previousMeasurement = 0.0f;
  gPidLeft.hasPreviousMeasurement = false;
  gPidLeft.bias = 0.0f;
  gPidLeft.heldOpening = gOpeningLeft;
  gPidLeft.hasBias = false;
  gPidRight.integral = 0.0f;
  gPidRight.previousMeasurement = 0.0f;
  gPidRight.hasPreviousMeasurement = false;
  gPidRight.bias = 0.0f;
  gPidRight.heldOpening = gOpeningRight;
  gPidRight.hasBias = false;
}

static float updatePID(PIDState &pid, float reference, float measurement, float feedforward) {
  float error = reference - measurement;
  if (!pid.hasBias) {
    pid.bias = pid.heldOpening - feedforward - gCfg.kp * error;
    pid.hasBias = true;
    pid.previousMeasurement = measurement;
    pid.hasPreviousMeasurement = true;
    return pid.heldOpening;
  }
  float candidate = clampf(pid.integral + error * DT, -gCfg.integralLimit, gCfg.integralLimit);
  float derivative = pid.hasPreviousMeasurement ? -(measurement - pid.previousMeasurement) / DT : 0.0f;
  pid.previousMeasurement = measurement;
  pid.hasPreviousMeasurement = true;
  // Bumpless command transition; this is not a measured physical PPV slew rate.
  pid.bias -= clampf(pid.bias, -50.0f * DT, 50.0f * DT);
  float raw = pid.bias + feedforward + gCfg.kp * error + gCfg.ki * candidate + gCfg.kd * derivative;
  float output = clampf(raw, 0.0f, 1.0f);
  if ((raw > 0.0f && raw < 1.0f) || (raw <= 0.0f && error > 0.0f) || (raw >= 1.0f && error < 0.0f)) {
    pid.integral = candidate;
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
  closeAllDirectionalValves();
  resetPID();
  gState = next;
  gStateStartMs = millis();
}

static void stopRun() {
  gRunning = false;
  gState = IDLE;
  safeOutputs();
  resetPID();
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

static void applyValves(bool leftAtm, bool leftVac, bool rightAtm, bool rightVac) {
  // Software interlock. The physical design should also prevent unsafe cross-flow.
  if ((leftAtm && leftVac) || (rightAtm && rightVac)) {
    enterFault(F("VALVE_INTERLOCK"));
    return;
  }
  digitalWrite(PIN_L_ATM, leftAtm ? VALVE_ON : VALVE_OFF);
  digitalWrite(PIN_L_VAC, leftVac ? VALVE_ON : VALVE_OFF);
  digitalWrite(PIN_R_ATM, rightAtm ? VALVE_ON : VALVE_OFF);
  digitalWrite(PIN_R_VAC, rightVac ? VALVE_ON : VALVE_OFF);
  gValveMask = (leftAtm ? 1 : 0) | (leftVac ? 2 : 0) | (rightAtm ? 4 : 0) | (rightVac ? 8 : 0);
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
  gVLeft = 0.0f;
  gVRight = 0.0f;
  gRunStartMs = millis();
  gLastHostMs = millis();
  gRunning = true;
  setState(PRECONDITION);
  Serial.print(F("STARTED,"));
  Serial.println(gRunId);
}

static bool validateConfig(const Config &c) {
  if (!(c.rrBpm >= 1.0f && c.rrBpm <= 40.0f)) return false;
  if (!(c.ieIn > 0.0f && c.ieOut > 0.0f)) return false;
  if (!(c.targetLeftMl > 0.0f && c.targetLeftMl <= c.maxVolumeMl)) return false;
  if (!(c.targetRightMl > 0.0f && c.targetRightMl <= c.maxVolumeMl)) return false;
  float ti = (60.0f / c.rrBpm) * c.ieIn / (c.ieIn + c.ieOut);
  if (!(c.fillTimeS > 0.0f && c.fillTimeS <= ti)) return false;
  if (c.deadTimeMs > 500) return false;
  if (!(c.kp >= 0.0f && c.ki >= 0.0f && c.kd >= 0.0f && c.integralLimit > 0.0f)) return false;
  if (!(c.preconditionS >= 0.0f && c.preconditionS <= 10.0f)) return false;
  if (!(c.maxAbsFlowSlm > 0.0f && c.maxAbsFlowSlm <= 250.0f)) return false;
  if (!(c.leftFlowSign == 1 || c.leftFlowSign == -1)) return false;
  if (!(c.rightFlowSign == 1 || c.rightFlowSign == -1)) return false;
  if (!(c.capacityLeftMl > c.maxVolumeMl && c.capacityRightMl > c.maxVolumeMl)) return false;
  if (!(c.tauInLeftS > 0.0f && c.tauInRightS > 0.0f)) return false;
  if (!(c.tauOutLeftS > 0.0f && c.tauOutRightS > 0.0f)) return false;
  if (!(c.valveExponent >= 0.5f && c.valveExponent <= 3.0f)) return false;
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
  String f[28];
  uint8_t count = splitCsv(line, f, 28);
  if (count != 26 || f[1].toInt() != 2) {
    Serial.println(F("ERR,CFG_FORMAT"));
    return;
  }
  Config c;
  uint32_t token = (uint32_t)f[2].toInt();
  c.rrBpm = f[3].toFloat();
  c.ieIn = f[4].toFloat();
  c.ieOut = f[5].toFloat();
  c.targetLeftMl = f[6].toFloat();
  c.targetRightMl = f[7].toFloat();
  c.fillTimeS = f[8].toFloat();
  c.deadTimeMs = (uint16_t)f[9].toInt();
  c.kp = f[10].toFloat();
  c.ki = f[11].toFloat();
  c.kd = f[12].toFloat();
  c.integralLimit = f[13].toFloat();
  c.preconditionS = f[14].toFloat();
  c.maxAbsFlowSlm = f[15].toFloat();
  c.maxVolumeMl = f[16].toFloat();
  c.leftFlowSign = (int8_t)f[17].toInt();
  c.rightFlowSign = (int8_t)f[18].toInt();
  c.capacityLeftMl = f[19].toFloat();
  c.capacityRightMl = f[20].toFloat();
  c.tauInLeftS = f[21].toFloat();
  c.tauInRightS = f[22].toFloat();
  c.tauOutLeftS = f[23].toFloat();
  c.tauOutRightS = f[24].toFloat();
  c.valveExponent = f[25].toFloat();
  if (!validateConfig(c)) {
    Serial.println(F("ERR,CFG_VALUE"));
    return;
  }
  if (gRunning) stopRun();
  gCfg = c;
  gConfigValid = true;
  Serial.print(F("ACK,CFG,"));
  Serial.println(token);
}

static void handleCommand(const String &raw) {
  String line = raw;
  line.trim();
  if (!line.length()) return;
  gLastHostMs = millis();
  if (line == "HELLO") {
    Serial.println(F("READY,LUNGLOGIC_G474,2"));
  } else if (line == "PING") {
    Serial.println(F("PONG"));
  } else if (line == "STOP") {
    uint32_t run = gRunId;
    stopRun();
    Serial.print(F("STOPPED,"));
    Serial.println(run);
  } else if (line.startsWith("CFG,")) {
    applyConfig(line);
  } else if (line.startsWith("START,")) {
    String f[4];
    uint8_t count = splitCsv(line, f, 4);
    if (count != 3) Serial.println(F("ERR,START_FORMAT"));
    else startRun((uint32_t)f[1].toInt(), (uint16_t)f[2].toInt());
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
  Serial.print(gQLeft, 2); Serial.print(',');
  Serial.print(gQRight, 2); Serial.print(',');
  Serial.print(gVLeft, 1); Serial.print(',');
  Serial.print(gVRight, 1); Serial.print(',');
  Serial.print(gQRefLeft, 2); Serial.print(',');
  Serial.print(gQRefRight, 2); Serial.print(',');
  Serial.print(gOpeningLeft, 3); Serial.print(',');
  Serial.print(gOpeningRight, 3); Serial.print(',');
  Serial.print(gCmdLeftV, 3); Serial.print(',');
  Serial.print(gCmdRightV, 3); Serial.print(',');
  Serial.println(gValveMask);
}

static void updateStateMachine() {
  uint32_t elapsedMs = millis() - gStateStartMs;
  switch (gState) {
    case PRECONDITION:
      if (elapsedMs >= (uint32_t)(gCfg.preconditionS * 1000.0f)) {
        gVLeft = 0.0f;
        gVRight = 0.0f;
        setState(DEAD_TO_INSP);
      }
      break;
    case DEAD_TO_INSP:
      if (elapsedMs >= gCfg.deadTimeMs) setState(INSPIRATION);
      break;
    case INSPIRATION:
      if (elapsedMs >= (uint32_t)(inspirationS() * 1000.0f)) setState(DEAD_TO_EXP);
      break;
    case DEAD_TO_EXP:
      if (elapsedMs >= gCfg.deadTimeMs) setState(EXPIRATION);
      break;
    case EXPIRATION:
      if (elapsedMs >= (uint32_t)(expirationS() * 1000.0f)) {
        gCycle++;
        if (gCycle >= gRequestedCycles) {
          gState = COMPLETE;
          gRunning = false;
          safeOutputs();
          Serial.print(F("DONE,"));
          Serial.println(gRunId);
        } else {
          setState(DEAD_TO_INSP);
        }
      }
      break;
    default:
      break;
  }
}

static void controlStep() {
  if (estopActive()) {
    safeOutputs();
    if (gState != FAULT) enterFault(F("ESTOP"));
    return;
  }
  float rawLeft = 0.0f;
  float rawRight = 0.0f;
  bool leftOk = sfmRead(SFM_LEFT_CHANNEL, rawLeft);
  bool rightOk = sfmRead(SFM_RIGHT_CHANNEL, rawRight);
  if (!leftOk || !rightOk) {
    if (++gSensorFailures >= 5) enterFault(F("SFM_READ"));
    return;
  }
  gSensorFailures = 0;
  gQLeft = gCfg.leftFlowSign * rawLeft;
  gQRight = gCfg.rightFlowSign * rawRight;
  if (!gRunning) return;
  if (millis() - gLastHostMs > HOST_WATCHDOG_MS) {
    enterFault(F("HOST_TIMEOUT"));
    return;
  }
  if (fabsf(gQLeft) > gCfg.maxAbsFlowSlm || fabsf(gQRight) > gCfg.maxAbsFlowSlm) {
    enterFault(F("FLOW_LIMIT"));
    return;
  }

  updateStateMachine();
  if (!gRunning) return;

  gQRefLeft = 0.0f;
  gQRefRight = 0.0f;
  bool lAtm = false, lVac = false, rAtm = false, rVac = false;
  float openL = gOpeningLeft, openR = gOpeningRight;

  if (gState == PRECONDITION) {
    // Gentle fixed command: this establishes the collapsed zero-volume reference.
    lVac = rVac = true;
    openL = openR = 0.35f;
  } else if (gState == INSPIRATION) {
    gVLeft += max(0.0f, gQLeft) * ML_PER_S_PER_SLM * DT;
    gVRight += max(0.0f, gQRight) * ML_PER_S_PER_SLM * DT;
    gQRefLeft = 0.06f * gCfg.targetLeftMl / gCfg.fillTimeS;
    gQRefRight = 0.06f * gCfg.targetRightMl / gCfg.fillTimeS;
    if (gVLeft < gCfg.targetLeftMl) {
      lAtm = true;
      float qMax = max(0.1f, 0.06f * (gCfg.capacityLeftMl - gVLeft) / gCfg.tauInLeftS);
      float ff = powf(min(1.0f, gQRefLeft / qMax), 1.0f / gCfg.valveExponent);
      openL = updatePID(gPidLeft, gQRefLeft, max(0.0f, gQLeft), ff);
    }
    if (gVRight < gCfg.targetRightMl) {
      rAtm = true;
      float qMax = max(0.1f, 0.06f * (gCfg.capacityRightMl - gVRight) / gCfg.tauInRightS);
      float ff = powf(min(1.0f, gQRefRight / qMax), 1.0f / gCfg.valveExponent);
      openR = updatePID(gPidRight, gQRefRight, max(0.0f, gQRight), ff);
    }
  } else if (gState == EXPIRATION) {
    gVLeft += min(0.0f, gQLeft) * ML_PER_S_PER_SLM * DT;
    gVRight += min(0.0f, gQRight) * ML_PER_S_PER_SLM * DT;
    gVLeft = max(0.0f, gVLeft);
    gVRight = max(0.0f, gVRight);
    float usable = max(0.25f, expirationS() - 0.001f * gCfg.deadTimeMs);
    gQRefLeft = 0.06f * gCfg.targetLeftMl / usable;
    gQRefRight = 0.06f * gCfg.targetRightMl / usable;
    if (gVLeft > END_VOLUME_ML) {
      lVac = true;
      float qMax = max(0.1f, 0.06f * gVLeft / gCfg.tauOutLeftS);
      float ff = powf(min(1.0f, gQRefLeft / qMax), 1.0f / gCfg.valveExponent);
      openL = updatePID(gPidLeft, gQRefLeft, max(0.0f, -gQLeft), ff);
    }
    if (gVRight > END_VOLUME_ML) {
      rVac = true;
      float qMax = max(0.1f, 0.06f * gVRight / gCfg.tauOutRightS);
      float ff = powf(min(1.0f, gQRefRight / qMax), 1.0f / gCfg.valveExponent);
      openR = updatePID(gPidRight, gQRefRight, max(0.0f, -gQRight), ff);
    }
  }

  if (gVLeft > gCfg.maxVolumeMl || gVRight > gCfg.maxVolumeMl) {
    enterFault(F("VOLUME_LIMIT"));
    return;
  }
  writePinchCommands(openL, openR);
  applyValves(lAtm, lVac, rAtm, rVac);
  if ((gStep++ % TELEMETRY_DIVIDER) == 0) printTelemetry();
}

void setup() {
#if USE_D8_ESTOP_INPUT
  pinMode(PIN_ESTOP, INPUT_PULLUP);
#endif
  digitalWrite(PIN_L_ATM, VALVE_OFF);
  digitalWrite(PIN_L_VAC, VALVE_OFF);
  digitalWrite(PIN_R_ATM, VALVE_OFF);
  digitalWrite(PIN_R_VAC, VALVE_OFF);
  pinMode(PIN_L_ATM, OUTPUT);
  pinMode(PIN_L_VAC, OUTPUT);
  pinMode(PIN_R_ATM, OUTPUT);
  pinMode(PIN_R_VAC, OUTPUT);
  pinMode(PIN_L_PPV_PWM, OUTPUT);
  pinMode(PIN_R_PPV_PWM, OUTPUT);
  analogWriteResolution(12);
  safeOutputs();
  digitalWrite(SDA_PIN, HIGH); pinMode(SDA_PIN, OUTPUT_OPEN_DRAIN);
  digitalWrite(SCL_PIN, HIGH); pinMode(SCL_PIN, OUTPUT_OPEN_DRAIN);
  for (uint8_t i = 0; i < 9; ++i) { sclLow(); sclHigh(); }
  busStop();
  Serial.begin(BAUD);
  bool leftStarted = sfmStart(SFM_LEFT_CHANNEL);
  delay(10);
  bool rightStarted = sfmStart(SFM_RIGHT_CHANNEL);
  delay(100);
  float discarded = 0.0f;
  if (leftStarted) sfmRead(SFM_LEFT_CHANNEL, discarded);
  if (rightStarted) sfmRead(SFM_RIGHT_CHANNEL, discarded);
  if (!leftStarted || !rightStarted) Serial.println(F("WARN,SFM_START"));
  gNextControlUs = micros() + CONTROL_PERIOD_US;
  gLastHostMs = millis();
  Serial.println(F("READY,LUNGLOGIC_G474,2"));
}

void loop() {
  readSerial();
  uint32_t now = micros();
  if ((int32_t)(now - gNextControlUs) >= 0) {
    gNextControlUs += CONTROL_PERIOD_US;
    // If the loop is badly delayed, resynchronize instead of executing a burst.
    if ((int32_t)(now - gNextControlUs) > (int32_t)(3UL * CONTROL_PERIOD_US)) {
      gNextControlUs = now + CONTROL_PERIOD_US;
    }
    controlStep();
  }
}
