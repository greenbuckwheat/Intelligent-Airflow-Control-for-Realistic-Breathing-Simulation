/*
 * Automated single-lung calibration firmware for NUCLEO-G474RE.
 * Protocol: READY,LUNGLOGIC_CAL_G474,2 / RUN / STOP / PING / CAL
 *
 * Final PCB allocation (left branch)
 *   ATM D11, VAC D13, PPV PWM D5, optional active-low E-stop D8
 *   SFM3300 through Pa.Hub channel 0 on software-I2C A4/A5
 *
 * Norgren 8254200.9151.02400 routing valves are 24 VDC, 2/2 NC loads.
 * GPIO drives only the PCB MOSFET stages. Thinkvast PPV40V uses a separate
 * 12-24 V supply and a 0-5 V analog control input; D5 supplies PWM duty to
 * the PCB's external low-pass/3.3-to-5 V interface.
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
static const uint32_t PIN_ATM = D11;
static const uint32_t PIN_VAC = D13;
static const uint32_t PIN_PPV_PWM = D5;
static const uint32_t PIN_ESTOP = D8;
static const uint32_t SDA_PIN = A4;
static const uint32_t SCL_PIN = A5;
static const uint8_t TCA_ADDR = 0x70;
static const uint8_t SFM_ADDR = 0x40;
static const uint8_t SFM_CHANNEL = 0;
static const uint32_t HOST_TIMEOUT_MS = 2000;
static const float MAX_ABS_FLOW_SLM = 240.0f;
static const uint8_t VALVE_ON = VALVE_ACTIVE_HIGH ? HIGH : LOW;
static const uint8_t VALVE_OFF = VALVE_ACTIVE_HIGH ? LOW : HIGH;

enum Stage : uint8_t { IDLE = 0, PRECONDITION = 1, DEAD_TIME = 2,
                       PPV_SETTLE = 3, RECORD = 4, FAULT = 5 };

static Stage stage = IDLE;
static uint32_t trialId = 0, stageStartedMs = 0, recordStartedMs = 0;
static uint32_t lastHostMs = 0, lastTelemetryMs = 0, sensorDueMs = 0;
static uint32_t preconditionMs = 0, deadTimeMs = 0, settleMs = 0, recordMs = 0;
static int8_t trialDirection = 1;
static float requestedOpening = 0.0f, appliedOpening = 0.0f, commandV = 5.0f;
static uint8_t appliedMask = 0, sensorStage = 0, sensorFailures = 0;
static uint16_t rawFlow = 0;
static float flowSlm = 0.0f;
static const char *sensorStatus = "STARTING";
static char rx[160];
static uint8_t rxLen = 0;
static bool rxOverflow = false;

static float clampf(float value, float low, float high) {
  return value < low ? low : (value > high ? high : value);
}

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
static uint8_t sfmCrc(uint8_t first, uint8_t second) {
  uint8_t crc = 0;
  const uint8_t data[] = {first, second};
  for (uint8_t value : data) {
    crc ^= value;
    for (uint8_t bit = 0; bit < 8; ++bit)
      crc = (crc & 0x80) ? (uint8_t)((crc << 1) ^ 0x31) : (uint8_t)(crc << 1);
  }
  return crc;
}

static bool estopActive() {
#if USE_D8_ESTOP_INPUT
  return digitalRead(PIN_ESTOP) == LOW;
#else
  return false;
#endif
}

static void setPpvOpening(float opening) {
  appliedOpening = clampf(opening, 0.0f, 1.0f);
  commandV = 5.0f * (1.0f - appliedOpening);  // PPV40V: 0 V open, 5 V closed.
  analogWrite(PIN_PPV_PWM, (uint16_t)lroundf(commandV * 4095.0f / 5.0f));
}

static void closeDirectionalValves() {
  digitalWrite(PIN_ATM, VALVE_OFF);
  digitalWrite(PIN_VAC, VALVE_OFF);
  appliedMask = 0;
}

static bool setDirectional(int8_t direction) {
  closeDirectionalValves();  // break before make
  delayMicroseconds(50);
  if (direction > 0) {
    digitalWrite(PIN_ATM, VALVE_ON); appliedMask = 1;
  } else if (direction < 0) {
    digitalWrite(PIN_VAC, VALVE_ON); appliedMask = 2;
  }
  return direction >= -1 && direction <= 1;
}

static void safeOutputs() {
  closeDirectionalValves();
  setPpvOpening(0.0f);
}

static void enterFault(const char *reason) {
  safeOutputs();
  stage = FAULT;
  Serial.print("FAULT,"); Serial.print(trialId); Serial.print(','); Serial.println(reason);
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

static void startTrial(const String &line) {
  String f[9];
  uint8_t count = splitCsv(line, f, 9);
  if (count != 8) { Serial.println("ERR,RUN_FORMAT"); return; }
  if (stage != IDLE) { Serial.println("ERR,BUSY"); return; }
  if (strcmp(sensorStatus, "OK")) { Serial.println("ERR,SENSOR_NOT_READY"); return; }
  if (estopActive()) { Serial.println("ERR,ESTOP_ACTIVE"); return; }

  uint32_t id = (uint32_t)f[1].toInt();
  int direction = f[2].toInt();
  int openingPermille = f[3].toInt();
  uint32_t pre = (uint32_t)f[4].toInt();
  uint32_t dead = (uint32_t)f[5].toInt();
  uint32_t settle = (uint32_t)f[6].toInt();
  uint32_t record = (uint32_t)f[7].toInt();
  if (!id || (direction != 1 && direction != -1) || openingPermille < 0
      || openingPermille > 1000 || pre > 30000 || dead < 20 || dead > 2000
      || settle < 100 || settle > 15000 || record < 500 || record > 120000) {
    Serial.println("ERR,RUN_VALUE"); return;
  }

  trialId = id;
  trialDirection = (int8_t)direction;
  requestedOpening = openingPermille / 1000.0f;
  preconditionMs = pre;
  deadTimeMs = dead;
  settleMs = settle;
  recordMs = record;
  sensorFailures = 0;
  lastHostMs = millis();
  stageStartedMs = millis();
  stage = PRECONDITION;
  setPpvOpening(1.0f);
  if (preconditionMs) setDirectional(-trialDirection);
  else closeDirectionalValves();
  Serial.print("STARTED,"); Serial.println(trialId);
}

static void handleLine(const String &raw) {
  String line = raw;
  line.trim();
  if (!line.length()) return;
  lastHostMs = millis();
  if (line == "HELLO") {
    safeOutputs(); stage = IDLE; trialId = 0;
    Serial.println("READY,LUNGLOGIC_CAL_G474,2");
    Serial.println("INFO,ATM=D11,VAC=D13,PPV=D5,ESTOP=D8,SFM_CH=0");
  } else if (line == "PING") {
    Serial.println("PONG");
  } else if (line == "STOP") {
    uint32_t stopped = trialId;
    safeOutputs(); stage = IDLE; trialId = 0;
    Serial.print("STOPPED,"); Serial.println(stopped);
  } else if (line.startsWith("RUN,")) {
    startTrial(line);
  } else if (line == "STATUS") {
    Serial.print("STATUS,"); Serial.print((uint8_t)stage); Serial.print(',');
    Serial.print(appliedMask); Serial.print(','); Serial.println(sensorStatus);
  } else {
    Serial.println("ERR,COMMAND");
  }
}

static void pollSerial() {
  for (uint8_t n = 0; n < 96 && Serial.available(); ++n) {
    char c = (char)Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      if (rxOverflow) { safeOutputs(); stage = IDLE; Serial.println("ERR,OVERFLOW"); }
      else { rx[rxLen] = 0; handleLine(String(rx)); }
      rxLen = 0; rxOverflow = false;
    } else if (c < 32 || c > 126 || rxLen >= sizeof(rx) - 1) {
      rxOverflow = true; safeOutputs(); stage = IDLE;
    } else if (!rxOverflow) rx[rxLen++] = c;
  }
}

static void pollSensor() {
  uint32_t now = millis();
  if ((int32_t)(now - sensorDueMs) < 0) return;
  if (sensorStage < 2) {
    if (!sensorCommand(sensorStage == 0 ? 0x2000 : 0x1000)) {
      sensorStatus = "I2C"; sensorStage = 0; sensorDueMs = now + 1000; return;
    }
    ++sensorStage; sensorStatus = "STARTING"; sensorDueMs = now + 100; return;
  }
  uint8_t a = 0, b = 0, crc = 0;
  bool ok = selectSensor() && busStart() && busWrite((SFM_ADDR << 1) | 1)
            && busRead(a, true) && busRead(b, true) && busRead(crc, false);
  busStop();
  if (!ok || sfmCrc(a, b) != crc) {
    sensorStatus = ok ? "CRC" : "I2C";
    sensorStage = 0; sensorDueMs = now + 1000; return;
  }
  if (sensorStage == 2) {
    sensorStage = 3; sensorDueMs = now + 20; return;
  }
  rawFlow = ((uint16_t)a << 8) | b;
  flowSlm = ((float)rawFlow - 32768.0f) / 120.0f;
  sensorStatus = "OK";
  sensorDueMs = now + 20;
}

static void updateTrial() {
  if (stage == IDLE || stage == FAULT) return;
  uint32_t now = millis();
  if (estopActive()) { enterFault("ESTOP"); return; }
  if ((uint32_t)(now - lastHostMs) > HOST_TIMEOUT_MS) { enterFault("HOST_TIMEOUT"); return; }
  if (strcmp(sensorStatus, "OK")) {
    if (++sensorFailures >= 5) enterFault("SFM_READ");
    return;
  }
  sensorFailures = 0;
  if (fabsf(flowSlm) > MAX_ABS_FLOW_SLM) { enterFault("FLOW_LIMIT"); return; }

  uint32_t elapsed = now - stageStartedMs;
  if (stage == PRECONDITION && elapsed >= preconditionMs) {
    closeDirectionalValves();
    setPpvOpening(requestedOpening);
    stage = DEAD_TIME; stageStartedMs = now;
  } else if (stage == DEAD_TIME && elapsed >= deadTimeMs) {
    stage = PPV_SETTLE; stageStartedMs = now;
  } else if (stage == PPV_SETTLE && elapsed >= settleMs) {
    setDirectional(trialDirection);
    stage = RECORD; stageStartedMs = recordStartedMs = now;
  } else if (stage == RECORD && elapsed >= recordMs) {
    uint32_t done = trialId;
    safeOutputs(); stage = IDLE; trialId = 0;
    Serial.print("DONE,"); Serial.println(done);
  }
}

static void telemetry() {
  uint32_t now = millis();
  uint32_t recordElapsed = stage == RECORD ? now - recordStartedMs : 0;
  Serial.print("CAL,"); Serial.print(trialId); Serial.print(',');
  Serial.print(now); Serial.print(','); Serial.print(recordElapsed); Serial.print(',');
  Serial.print((uint8_t)stage); Serial.print(',');
  if (!strcmp(sensorStatus, "OK")) Serial.print(flowSlm, 3); else Serial.print("nan");
  Serial.print(','); Serial.print(sensorStatus); Serial.print(',');
  Serial.print(appliedMask); Serial.print(','); Serial.print(appliedOpening, 3); Serial.print(',');
  Serial.println(commandV, 3);
}

void setup() {
  pinMode(PIN_ATM, OUTPUT);
  pinMode(PIN_VAC, OUTPUT);
  pinMode(PIN_PPV_PWM, OUTPUT);
  analogWriteResolution(12);
  safeOutputs();
#if USE_D8_ESTOP_INPUT
  pinMode(PIN_ESTOP, INPUT_PULLUP);
#endif
  digitalWrite(SDA_PIN, HIGH); pinMode(SDA_PIN, OUTPUT_OPEN_DRAIN);
  digitalWrite(SCL_PIN, HIGH); pinMode(SCL_PIN, OUTPUT_OPEN_DRAIN);
  for (uint8_t i = 0; i < 9; ++i) { sclLow(); sclHigh(); }
  busStop();
  Serial.begin(BAUD);
  lastHostMs = millis();
  sensorDueMs = millis() + 100;
  Serial.println("READY,LUNGLOGIC_CAL_G474,2");
}

void loop() {
  pollSerial();
  pollSensor();
  updateTrial();
  uint32_t now = millis();
  if ((uint32_t)(now - lastTelemetryMs) >= 20) {
    lastTelemetryMs = now;
    telemetry();
  }
}
