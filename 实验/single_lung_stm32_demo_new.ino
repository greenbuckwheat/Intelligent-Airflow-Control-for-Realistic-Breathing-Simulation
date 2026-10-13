/*
 * LungLogic - Left Lung Hardware Demonstration
 * Target: NUCLEO-G474RE
 *
 * Demo purpose:
 *   1) Python UI sends the current left-lung valve command over Serial.
 *   2) NUCLEO drives two breadboard LEDs as temporary stand-ins for:
 *        - Left ATM solenoid valve
 *        - Left VAC solenoid valve
 *   3) NUCLEO continuously reads one SFM3300-250-D flow meter directly
 *      (no Pa.Hub) and sends real flow telemetry back to the UI.
 *
 * Serial protocol kept compatible with the current Python GUI:
 *   PC  -> HELLO
 *   MCU -> READY,LUNGLOGIC_DEMO_G474,1
 *   PC  -> LED,0        both outputs OFF
 *   PC  -> LED,1        left ATM output ON
 *   PC  -> LED,2        left VAC output ON
 *   PC  -> STOP
 *
 * For the next GUI version this firmware also accepts:
 *   VALVE,0 / VALVE,1 / VALVE,2
 *
 * Telemetry:
 *   FLOW,<millis>,<flow_slm>,<status>,<mask>,<watchdog>,<raw>
 *
 * Final PCB pin allocation used here:
 *   D11 = Left ATM solenoid / demo LED
 *   D13 = Left VAC solenoid / demo LED
 *   A4  = SFM3300 SDA
 *   A5  = SFM3300 SCL
 *
 * Breadboard LED wiring (active HIGH):
 *   D11 -> 220-470 ohm -> LED -> GND
 *   D13 -> 220-470 ohm -> LED -> GND
 *
 * SFM3300-250-D:
 *   VDD = 5 V
 *   GND = common GND
 *   SDA/SCL use external pull-ups to 3.3 V (e.g. 4.7 kOhm)
 *   I2C address = 0x40
 */

#include <Arduino.h>
#include <string.h>

#if !defined(ARDUINO_NUCLEO_G474RE)
#error "Select Nucleo-64 / Nucleo G474RE in STM32 MCU based boards"
#endif

// -----------------------------------------------------------------------------
// Final left-lung pin mapping
// -----------------------------------------------------------------------------
static const uint32_t ATM_L = D11;
static const uint32_t VAC_L = D13;

static const uint32_t SDA_PIN = A4;
static const uint32_t SCL_PIN = A5;

// -----------------------------------------------------------------------------
// Serial / safety
// -----------------------------------------------------------------------------
static const uint32_t HOST_TIMEOUT_MS = 750;
static const uint32_t TELEMETRY_PERIOD_MS = 20;  // 50 Hz

// -----------------------------------------------------------------------------
// SFM3300-250-D
// -----------------------------------------------------------------------------
static const uint8_t SFM_ADDR = 0x40;
static const uint16_t SFM_SOFT_RESET = 0x2000;
static const uint16_t SFM_START_CONTINUOUS = 0x1000;

// G474RE A4/A5 are kept exactly as routed in the final PCB design.
// They are handled here by a bounded software I2C implementation.
// External SDA/SCL pull-ups should go to 3.3 V.
static void sda(bool high) {
  digitalWrite(SDA_PIN, high ? HIGH : LOW);  // HIGH releases open-drain line
}

static void sclLow() {
  digitalWrite(SCL_PIN, LOW);
  delayMicroseconds(5);
}

static bool sclHigh() {
  digitalWrite(SCL_PIN, HIGH);               // release SCL
  const uint32_t start = micros();
  while (!digitalRead(SCL_PIN)) {
    // Bounded clock-stretch wait prevents a dead bus from hanging the firmware.
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
    sda((value & 0x80) != 0);
    delayMicroseconds(5);
    if (!sclHigh()) return false;
    sclLow();
    value <<= 1;
  }

  // ACK bit from slave.
  sda(true);
  if (!sclHigh()) return false;
  const bool ack = !digitalRead(SDA_PIN);
  sclLow();
  return ack;
}

static bool busRead(uint8_t &value, bool ack) {
  value = 0;
  sda(true);

  for (uint8_t i = 0; i < 8; ++i) {
    if (!sclHigh()) return false;
    value = (uint8_t)((value << 1) | (digitalRead(SDA_PIN) ? 1 : 0));
    sclLow();
  }

  // Master ACKs first bytes and NACKs the final byte.
  sda(!ack);
  if (!sclHigh()) return false;
  sclLow();
  sda(true);
  return true;
}

static bool sensorCommand(uint16_t command) {
  bool ok = busStart()
         && busWrite((uint8_t)(SFM_ADDR << 1))
         && busWrite((uint8_t)(command >> 8))
         && busWrite((uint8_t)(command & 0xFF));
  busStop();
  return ok;
}

static uint8_t sfmCrc(uint8_t first, uint8_t second) {
  // SFM3xxx CRC: init 0x00, polynomial 0x31.
  uint8_t crc = 0x00;
  const uint8_t data[2] = {first, second};

  for (uint8_t i = 0; i < 2; ++i) {
    crc ^= data[i];
    for (uint8_t bit = 0; bit < 8; ++bit) {
      crc = (crc & 0x80)
              ? (uint8_t)((crc << 1) ^ 0x31)
              : (uint8_t)(crc << 1);
    }
  }
  return crc;
}

// -----------------------------------------------------------------------------
// Runtime state
// -----------------------------------------------------------------------------
static uint8_t appliedMask = 0;
static bool hostActive = false;
static bool hostTimedOut = false;

static uint32_t lastHost = 0;
static uint32_t lastTelemetry = 0;
static uint32_t sensorDue = 100;

// Sensor state machine:
//   0 = send soft reset
//   1 = start continuous measurement
//   2 = read and discard first sample
//   3 = normal measurement
static uint8_t sensorStage = 0;

static uint16_t rawFlow = 0;
static float flowSlm = 0.0f;
static const char *sensorStatus = "STARTING";

static char rx[48];
static uint8_t rxLen = 0;
static bool rxOverflow = false;

// -----------------------------------------------------------------------------
// Left-lung output control
// -----------------------------------------------------------------------------
static void setOutputs(uint8_t mask) {
  // Break-before-make: never allow ATM and VAC outputs ON simultaneously.
  digitalWrite(ATM_L, LOW);
  digitalWrite(VAC_L, LOW);

  if (mask == 1) {
    digitalWrite(ATM_L, HIGH);
  } else if (mask == 2) {
    digitalWrite(VAC_L, HIGH);
  } else {
    mask = 0;
  }

  appliedMask = mask;
}

static void stopHost() {
  hostActive = false;
  setOutputs(0);
}

// -----------------------------------------------------------------------------
// Serial command handling
// -----------------------------------------------------------------------------
static bool isOutputCommand(const char *line, const char *prefix, uint8_t &mask) {
  const size_t n = strlen(prefix);
  if (strncmp(line, prefix, n) != 0) return false;
  if (line[n] < '0' || line[n] > '2' || line[n + 1] != '\0') return false;
  mask = (uint8_t)(line[n] - '0');
  return true;
}

static void handleLine(const char *line) {
  if (!strcmp(line, "HELLO")) {
    stopHost();
    hostTimedOut = false;

    Serial.println("READY,LUNGLOGIC_DEMO_G474,1");
    Serial.println("INFO,LEFT LUNG DEMO | ATM=D11 | VAC=D13 | SFM3300-250-D direct @0x40");
    return;
  }

  if (!strcmp(line, "STOP")) {
    stopHost();
    hostTimedOut = false;
    return;
  }

  uint8_t mask = 0;
  if (isOutputCommand(line, "LED,", mask) ||
      isOutputCommand(line, "VALVE,", mask)) {
    lastHost = millis();
    hostActive = true;
    hostTimedOut = false;
    setOutputs(mask);
    return;
  }

  // Any malformed / unsupported command puts outputs in the safe OFF state.
  stopHost();
  Serial.println("ERR,COMMAND");
}

static void pollHost() {
  // Limit serial work each loop so the watchdog and sensor acquisition keep running.
  for (uint8_t n = 0; n < 64 && Serial.available(); ++n) {
    const char c = (char)Serial.read();

    if (c == '\r') continue;

    if (c == '\n') {
      if (rxOverflow) {
        stopHost();
        Serial.println("ERR,OVERFLOW");
      } else {
        rx[rxLen] = '\0';
        handleLine(rx);
      }
      rxLen = 0;
      rxOverflow = false;
      continue;
    }

    if (c < 32 || c > 126 || rxLen >= sizeof(rx) - 1) {
      rxOverflow = true;
      stopHost();
    } else if (!rxOverflow) {
      rx[rxLen++] = c;
    }
  }

  if (hostActive && (uint32_t)(millis() - lastHost) >= HOST_TIMEOUT_MS) {
    stopHost();
    hostTimedOut = true;
  }
}

// -----------------------------------------------------------------------------
// SFM3300 acquisition
// -----------------------------------------------------------------------------
static void sensorFault(const char *status, uint32_t now) {
  sensorStatus = status;
  rawFlow = 0;
  flowSlm = 0.0f;
  sensorStage = 0;
  sensorDue = now + 1000;
}

static void pollSensor() {
  const uint32_t now = millis();
  if ((int32_t)(now - sensorDue) < 0) return;

  if (sensorStage == 0) {
    if (!sensorCommand(SFM_SOFT_RESET)) {
      sensorFault("I2C", now);
      return;
    }
    sensorStage = 1;
    sensorStatus = "STARTING";
    sensorDue = now + 100;
    return;
  }

  if (sensorStage == 1) {
    if (!sensorCommand(SFM_START_CONTINUOUS)) {
      sensorFault("I2C", now);
      return;
    }
    sensorStage = 2;
    sensorStatus = "STARTING";
    sensorDue = now + 100;
    return;
  }

  uint8_t msb = 0;
  uint8_t lsb = 0;
  uint8_t crc = 0;

  bool ok = busStart()
         && busWrite((uint8_t)((SFM_ADDR << 1) | 1))
         && busRead(msb, true)
         && busRead(lsb, true)
         && busRead(crc, false);
  busStop();

  if (!ok) {
    sensorFault("I2C", now);
    return;
  }

  if (sfmCrc(msb, lsb) != crc) {
    sensorFault("CRC", now);
    return;
  }

  // Datasheet: the first measurement after initialization is not valid.
  if (sensorStage == 2) {
    sensorStage = 3;
    sensorDue = now + 20;
    return;
  }

  rawFlow = ((uint16_t)msb << 8) | lsb;
  flowSlm = ((float)rawFlow - 32768.0f) / 120.0f;
  sensorStatus = "OK";
  sensorDue = now + 20;
}

// -----------------------------------------------------------------------------
// Telemetry
// -----------------------------------------------------------------------------
static void sendTelemetry() {
  Serial.print("FLOW,");
  Serial.print(millis());
  Serial.print(',');

  if (!strcmp(sensorStatus, "OK")) {
    Serial.print(flowSlm, 3);
  } else {
    Serial.print("nan");
  }

  Serial.print(',');
  Serial.print(sensorStatus);
  Serial.print(',');
  Serial.print(appliedMask);
  Serial.print(',');
  Serial.print(hostTimedOut ? 1 : 0);
  Serial.print(',');
  Serial.println(rawFlow);
}

// -----------------------------------------------------------------------------
// Arduino entry points
// -----------------------------------------------------------------------------
void setup() {
  // Demo LEDs / future solenoid MOSFET control signals.
  digitalWrite(ATM_L, LOW);
  pinMode(ATM_L, OUTPUT);
  digitalWrite(VAC_L, LOW);
  pinMode(VAC_L, OUTPUT);

  // Software-I2C lines in open-drain mode.
  digitalWrite(SDA_PIN, HIGH);
  pinMode(SDA_PIN, OUTPUT_OPEN_DRAIN);
  digitalWrite(SCL_PIN, HIGH);
  pinMode(SCL_PIN, OUTPUT_OPEN_DRAIN);

  // Try to release an SFM3300/I2C bus left mid-transfer by a previous MCU reset.
  for (uint8_t i = 0; i < 9; ++i) {
    sclLow();
    if (!sclHigh()) break;
  }
  busStop();

  Serial.begin(115200);  // ST-LINK Virtual COM Port on NUCLEO-G474RE.

  setOutputs(0);
  sensorStatus = "STARTING";
  sensorStage = 0;
  sensorDue = millis() + 100;
}

void loop() {
  pollHost();
  pollSensor();

  const uint32_t now = millis();
  if ((uint32_t)(now - lastTelemetry) >= TELEMETRY_PERIOD_MS) {
    lastTelemetry = now;
    sendTelemetry();
  }
}
