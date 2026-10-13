/*
 * Flow-control experiment firmware for NUCLEO-G474RE.
 *
 * This is a separate bench-test firmware. It does not modify or replace the
 * production LungLogic controller files. It uses the same hardware profile as
 * firmware/single_lung_controller_stm32/single_lung_controller_stm32.ino:
 *
 *   Left ATM solenoid:   D11
 *   Left VAC solenoid:   D13
 *   Left PPV PWM output: D5, through external filter/0-5 V interface
 *   E-stop input:        D8, active low
 *   SFM3300 flow sensor: directly connected to software I2C A4/A5
 *
 * Serial protocol, 115200 baud, newline terminated:
 *   HELLO
 *   PING
 *   STOP
 *   ATM,0 or ATM,1
 *   VAC,0 or VAC,1
 *   PPV,<target_millivolts 0..5000>
 *   ZERO_FLOW
 *
 * Telemetry:
 *   READY,FLOW_EXP_G474,1
 *   TEL,ms,flow_slm,atm,vac,ppv_v,ppv_pwm_code,p_model_kpa,p_syringe_kpa,sensor_status,fault
 *
 * PPV convention follows the current project convention:
 *   0 V = fully open, higher voltage = gradually clamped, 5 V = closed.
 *
 * Recommended near-closure characterization sweep:
 *   3.800 V to 4.500 V in 0.050 V steps.
 *   Wait at least 3000 ms after each PPV command before using steady-state
 *   samples. Test ATM and VAC separately from comparable initial conditions.
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
static const uint32_t HOST_TIMEOUT_MS = 3000;
static const uint32_t TELEMETRY_MS = 20;
static const uint32_t INTERLOCK_US = 50000;
static const uint16_t RECOMMENDED_SWEEP_START_MV = 3800;
static const uint16_t RECOMMENDED_SWEEP_END_MV = 4500;
static const uint16_t RECOMMENDED_SWEEP_STEP_MV = 50;
static const uint16_t RECOMMENDED_SETTLE_MS = 3000;

static const uint32_t PIN_ESTOP = D8;
static const uint32_t PIN_ATM = D11;
static const uint32_t PIN_VAC = D13;
static const uint32_t PIN_PPV_PWM = D5;
static const uint32_t SDA_PIN = A4;
static const uint32_t SCL_PIN = A5;

static const uint8_t VALVE_ON = VALVE_ACTIVE_HIGH ? HIGH : LOW;
static const uint8_t VALVE_OFF = VALVE_ACTIVE_HIGH ? LOW : HIGH;

static const uint8_t SFM_ADDR = 0x40;
static const uint16_t SFM_SOFT_RESET = 0x2000;
static const uint16_t SFM_START_CONTINUOUS = 0x1000;
static const float SFM_SCALE = 120.0f;
static const float SFM_OFFSET = 32768.0f;

// Set PRESSURE_ENABLED to 1 after wiring and calibrating analog pressure sensors.
static const uint8_t PRESSURE_ENABLED = 0;
// Final PCB pressure inputs: A3 left lung, A2 right lung/second test point.
static const uint32_t PIN_PRESSURE_MODEL = A3;
static const uint32_t PIN_PRESSURE_SYRINGE = A2;
static const float ADC_REF_V = 3.3f;
static const float PRESSURE_ZERO_V = 1.65f;
static const float PRESSURE_KPA_PER_V = 20.0f;

static bool atmOpen = false;
static bool vacOpen = false;
static uint16_t ppvMv = 0;
static uint16_t ppvCode = 0;
static float flowOffsetSlm = 0.0f;
static float flowSlm = 0.0f;
static uint16_t rawFlow = 0;
static float pressureModelKpa = NAN;
static float pressureSyringeKpa = NAN;
static const char *sensorStatus = "STARTING";
static const char *faultStatus = "NONE";
static bool hostSeen = false;
static bool outputActive = false;

static uint32_t lastHostMs = 0;
static uint32_t lastTelemetryMs = 0;
static uint32_t sensorDueMs = 0;
// 0: soft reset, 1: start continuous measurement,
// 2: read and discard the first invalid sample, 3: normal measurement.
static uint8_t sensorStage = 0;

static char rx[96];
static uint8_t rxLen = 0;
static bool rxOverflow = false;

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
  sda(false);
  delayMicroseconds(5);
  sclLow();
  return true;
}

static void busStop() {
  sclLow();
  sda(false);
  sclHigh();
  sda(true);
  delayMicroseconds(5);
}

static bool busWrite(uint8_t value) {
  for (uint8_t i = 0; i < 8; ++i) {
    sda(value & 0x80);
    delayMicroseconds(5);
    if (!sclHigh()) return false;
    sclLow();
    value <<= 1;
  }
  sda(true);
  if (!sclHigh()) return false;
  bool ack = !digitalRead(SDA_PIN);
  sclLow();
  return ack;
}

static bool busRead(uint8_t &value, bool ack) {
  value = 0;
  sda(true);
  for (uint8_t i = 0; i < 8; ++i) {
    if (!sclHigh()) return false;
    value = (value << 1) | (digitalRead(SDA_PIN) ? 1 : 0);
    sclLow();
  }
  sda(!ack);
  if (!sclHigh()) return false;
  sclLow();
  sda(true);
  return true;
}

static bool selectSensor() {
  // The SFM3300 is directly visible at address 0x40 on A4/A5.
  return true;
}

static bool sensorCommand(uint16_t command) {
  if (!selectSensor()) return false;
  bool ok = busStart() && busWrite(SFM_ADDR << 1)
            && busWrite(command >> 8) && busWrite(command & 0xFF);
  busStop();
  return ok;
}

static uint8_t sfmCrc(uint8_t first, uint8_t second) {
  uint8_t crc = 0x00;
  uint8_t data[2] = {first, second};
  for (uint8_t i = 0; i < 2; ++i) {
    crc ^= data[i];
    for (uint8_t bit = 0; bit < 8; ++bit) {
      crc = (crc & 0x80) ? (uint8_t)((crc << 1) ^ 0x31) : (uint8_t)(crc << 1);
    }
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

static void writePpvMv(uint16_t mv) {
  ppvMv = mv > 5000 ? 5000 : mv;
  ppvCode = (uint16_t)lroundf((float)ppvMv * 4095.0f / 5000.0f);
  analogWrite(PIN_PPV_PWM, ppvCode);
}

static void closeValves() {
  digitalWrite(PIN_ATM, VALVE_OFF);
  digitalWrite(PIN_VAC, VALVE_OFF);
  atmOpen = false;
  vacOpen = false;
}

static void updateOutputActive() {
  outputActive = atmOpen || vacOpen || ppvMv < 5000;
}

static void safeStop(const char *reason) {
  closeValves();
  writePpvMv(5000);
  faultStatus = reason;
  updateOutputActive();
}

static void defaultIdle() {
  closeValves();
  writePpvMv(0);
  faultStatus = "NONE";
  updateOutputActive();
}

static void setAtm(bool on) {
  if (on) {
    digitalWrite(PIN_VAC, VALVE_OFF);
    vacOpen = false;
    delayMicroseconds(INTERLOCK_US);
    digitalWrite(PIN_ATM, VALVE_ON);
    atmOpen = true;
  } else {
    digitalWrite(PIN_ATM, VALVE_OFF);
    atmOpen = false;
  }
  updateOutputActive();
}

static void setVac(bool on) {
  if (on) {
    digitalWrite(PIN_ATM, VALVE_OFF);
    atmOpen = false;
    delayMicroseconds(INTERLOCK_US);
    digitalWrite(PIN_VAC, VALVE_ON);
    vacOpen = true;
  } else {
    digitalWrite(PIN_VAC, VALVE_OFF);
    vacOpen = false;
  }
  updateOutputActive();
}

static int parseIntStrict(const char *text, int low, int high, bool &ok) {
  ok = false;
  if (!text || !*text) return 0;
  long value = 0;
  const char *p = text;
  while (*p) {
    if (*p < '0' || *p > '9') return 0;
    value = value * 10 + (*p++ - '0');
    if (value > high) return 0;
  }
  if (value < low || value > high) return 0;
  ok = true;
  return (int)value;
}

static void printReady() {
  Serial.println(F("READY,FLOW_EXP_G474,1"));
  Serial.println(F("INFO,ATM=D11,VAC=D13,PPV=D5,ESTOP=D8,P_LEFT=A3,P_RIGHT=A2,SFM=A4/A5_DIRECT"));
  Serial.print(F("INFO,PPV_SWEEP_MV="));
  Serial.print(RECOMMENDED_SWEEP_START_MV);
  Serial.print(':');
  Serial.print(RECOMMENDED_SWEEP_END_MV);
  Serial.print(':');
  Serial.print(RECOMMENDED_SWEEP_STEP_MV);
  Serial.print(F(",SETTLE_MS="));
  Serial.println(RECOMMENDED_SETTLE_MS);
}

static void handleCommand() {
  if (!strcmp(rx, "HELLO")) {
    hostSeen = true;
    lastHostMs = millis();
    defaultIdle();
    printReady();
  } else if (!strcmp(rx, "PING")) {
    hostSeen = true;
    lastHostMs = millis();
    Serial.println(F("PONG"));
  } else if (!strcmp(rx, "STOP")) {
    hostSeen = true;
    lastHostMs = millis();
    safeStop("NONE");
    Serial.println(F("ACK,STOP"));
  } else if (!strncmp(rx, "ATM,", 4)) {
    bool ok = false;
    int value = parseIntStrict(rx + 4, 0, 1, ok);
    if (!ok) { Serial.println(F("ERR,ATM_FORMAT")); return; }
    if (estopActive()) { safeStop("ESTOP"); Serial.println(F("FAULT,ESTOP")); return; }
    hostSeen = true;
    lastHostMs = millis();
    faultStatus = "NONE";
    setAtm(value == 1);
    Serial.print(F("ACK,ATM,"));
    Serial.println(atmOpen ? 1 : 0);
  } else if (!strncmp(rx, "VAC,", 4)) {
    bool ok = false;
    int value = parseIntStrict(rx + 4, 0, 1, ok);
    if (!ok) { Serial.println(F("ERR,VAC_FORMAT")); return; }
    if (estopActive()) { safeStop("ESTOP"); Serial.println(F("FAULT,ESTOP")); return; }
    hostSeen = true;
    lastHostMs = millis();
    faultStatus = "NONE";
    setVac(value == 1);
    Serial.print(F("ACK,VAC,"));
    Serial.println(vacOpen ? 1 : 0);
  } else if (!strncmp(rx, "PPV,", 4)) {
    bool ok = false;
    int value = parseIntStrict(rx + 4, 0, 5000, ok);
    if (!ok) { Serial.println(F("ERR,PPV_FORMAT")); return; }
    if (estopActive()) { safeStop("ESTOP"); Serial.println(F("FAULT,ESTOP")); return; }
    hostSeen = true;
    lastHostMs = millis();
    faultStatus = "NONE";
    writePpvMv((uint16_t)value);
    updateOutputActive();
    Serial.print(F("ACK,PPV,"));
    Serial.print(ppvMv);
    Serial.print(',');
    Serial.println(ppvCode);
  } else if (!strcmp(rx, "ZERO_FLOW")) {
    if (strcmp(sensorStatus, "OK")) {
      Serial.println(F("ERR,SENSOR_NOT_READY"));
      return;
    }
    flowOffsetSlm += flowSlm;
    Serial.print(F("ACK,ZERO_FLOW,"));
    Serial.println(flowOffsetSlm, 4);
  } else if (!strcmp(rx, "STATUS")) {
    Serial.print(F("STATUS,"));
    Serial.print(atmOpen ? 1 : 0);
    Serial.print(',');
    Serial.print(vacOpen ? 1 : 0);
    Serial.print(',');
    Serial.print(ppvMv);
    Serial.print(',');
    Serial.println(sensorStatus);
  } else {
    Serial.println(F("ERR,COMMAND"));
  }
}

static void pollSerial() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      rx[rxLen] = 0;
      if (rxOverflow) Serial.println(F("ERR,LINE_TOO_LONG"));
      else if (rxLen) handleCommand();
      rxLen = 0;
      rxOverflow = false;
    } else if (c < 32 || c > 126 || rxLen >= sizeof(rx) - 1) {
      rxOverflow = true;
    } else if (!rxOverflow) {
      rx[rxLen++] = c;
    }
  }
}

static void sensorFault(const char *status, uint32_t now) {
  sensorStatus = status;
  rawFlow = 0;
  flowSlm = 0.0f;
  sensorStage = 0;
  sensorDueMs = now + 1000;
}

static void pollSensor() {
  uint32_t now = millis();
  if ((int32_t)(now - sensorDueMs) < 0) return;

  if (sensorStage == 0) {
    if (!sensorCommand(SFM_SOFT_RESET)) {
      sensorFault("I2C", now);
      return;
    }
    sensorStage = 1;
    sensorStatus = "STARTING";
    sensorDueMs = now + 100;
    return;
  }

  if (sensorStage == 1) {
    if (!sensorCommand(SFM_START_CONTINUOUS)) {
      sensorFault("I2C", now);
      return;
    }
    sensorStage = 2;
    sensorStatus = "STARTING";
    sensorDueMs = now + 100;
    return;
  }

  uint8_t a = 0, b = 0, crc = 0;
  bool ok = selectSensor() && busStart() && busWrite((SFM_ADDR << 1) | 1)
            && busRead(a, true) && busRead(b, true) && busRead(crc, false);
  busStop();

  if (!ok) {
    sensorFault("I2C", now);
    return;
  }
  if (sfmCrc(a, b) != crc) {
    sensorFault("CRC", now);
    return;
  }

  // SFM3300 specifies that the first sample after initialization is invalid.
  if (sensorStage == 2) {
    sensorStage = 3;
    sensorDueMs = now + 20;
    return;
  }

  rawFlow = ((uint16_t)a << 8) | b;
  flowSlm = ((float)rawFlow - SFM_OFFSET) / SFM_SCALE - flowOffsetSlm;
  sensorStatus = "OK";
  sensorDueMs = now + 20;
}

static float readPressureKpa(uint32_t pin) {
  if (!PRESSURE_ENABLED) return NAN;
  float volts = analogRead(pin) * ADC_REF_V / 4095.0f;
  return (volts - PRESSURE_ZERO_V) * PRESSURE_KPA_PER_V;
}

static void pollPressure() {
  pressureModelKpa = readPressureKpa(PIN_PRESSURE_MODEL);
  pressureSyringeKpa = readPressureKpa(PIN_PRESSURE_SYRINGE);
}

static void printFloatOrNan(float value, uint8_t digits) {
  if (isnan(value)) Serial.print(F("nan"));
  else Serial.print(value, digits);
}

static void telemetry() {
  Serial.print(F("TEL,"));
  Serial.print(millis());
  Serial.print(',');
  if (!strcmp(sensorStatus, "OK")) printFloatOrNan(flowSlm, 3);
  else Serial.print(F("nan"));
  Serial.print(',');
  Serial.print(atmOpen ? 1 : 0);
  Serial.print(',');
  Serial.print(vacOpen ? 1 : 0);
  Serial.print(',');
  Serial.print((float)ppvMv / 1000.0f, 3);
  Serial.print(',');
  Serial.print(ppvCode);
  Serial.print(',');
  printFloatOrNan(pressureModelKpa, 3);
  Serial.print(',');
  printFloatOrNan(pressureSyringeKpa, 3);
  Serial.print(',');
  Serial.print(sensorStatus);
  Serial.print(',');
  Serial.println(faultStatus);
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
  writePpvMv(0);

  digitalWrite(SDA_PIN, HIGH);
  pinMode(SDA_PIN, OUTPUT_OPEN_DRAIN);
  digitalWrite(SCL_PIN, HIGH);
  pinMode(SCL_PIN, OUTPUT_OPEN_DRAIN);
  for (uint8_t i = 0; i < 9; ++i) {
    sclLow();
    sclHigh();
  }
  busStop();

  analogReadResolution(12);
  Serial.begin(BAUD);
  lastHostMs = millis();
  sensorDueMs = millis() + 100;
  printReady();
}

void loop() {
  pollSerial();
  pollSensor();
  pollPressure();

  if (estopActive()) {
    if (outputActive || strcmp(faultStatus, "ESTOP")) {
      safeStop("ESTOP");
      Serial.println(F("FAULT,ESTOP"));
    }
  }

  if (hostSeen && outputActive && (uint32_t)(millis() - lastHostMs) > HOST_TIMEOUT_MS) {
    safeStop("HOST_TIMEOUT");
    Serial.println(F("FAULT,HOST_TIMEOUT"));
  }

  uint32_t now = millis();
  if ((uint32_t)(now - lastTelemetryMs) >= TELEMETRY_MS) {
    lastTelemetryMs = now;
    telemetry();
  }
}
