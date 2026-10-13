/* Independent electrical PPV calibration, NUCLEO-G474RE.
 * Left ATM D11 and left VAC D13 remain OFF.
 * Left PPV PWM D5 -> external filter/amplifier -> PPV.
 * 0% duty means nominal 0 V / open; STOP means 100% / nominal 5 V.
 * Commands: HELLO, PING, STOP, PWM,<integer 0..4095> (newline terminated).
 */
#include <Arduino.h>
#if !defined(ARDUINO_NUCLEO_G474RE)
#error "Select Nucleo-64 / Nucleo G474RE"
#endif
#ifndef VALVE_ACTIVE_HIGH
#define VALVE_ACTIVE_HIGH 1
#endif
#ifndef USE_D8_ESTOP_INPUT
#define USE_D8_ESTOP_INPUT 1
#endif
static const uint32_t PWM_HZ = 1000; // Matches installed STM32 core default.
static const uint32_t TIMEOUT_MS = 3000;
static uint32_t lastHost;
static uint16_t code = 4095;
static bool active = false;
static char rx[48];
static uint8_t length = 0;
static bool overflow = false;
static bool estop() {
#if USE_D8_ESTOP_INPUT
  return digitalRead(D8) == LOW;
#else
  return false;
#endif
}
static void apply(uint16_t value) {
  digitalWrite(D11, VALVE_ACTIVE_HIGH ? LOW : HIGH);
  digitalWrite(D13, VALVE_ACTIVE_HIGH ? LOW : HIGH);
  code = value;
  analogWrite(D5, code);
}
static void stop() { active = false; apply(4095); }
static void ready() {
  Serial.print("READY,PWM_EXP_G474,1,"); Serial.print(PWM_HZ);
  Serial.println(",12,D5");
}
static void command() {
  if (!strcmp(rx, "HELLO")) { lastHost = millis(); ready(); }
  else if (!strcmp(rx, "PING")) { lastHost = millis(); Serial.println("PONG"); }
  else if (!strcmp(rx, "STOP")) { lastHost = millis(); stop(); Serial.println("STOPPED,4095"); }
  else if (!strncmp(rx, "PWM,", 4)) {
    const char *p = rx + 4;
    if (!*p) { Serial.println("ERR,PWM_FORMAT"); return; }
    uint32_t value = 0;
    while (*p) {
      if (*p < '0' || *p > '9') { Serial.println("ERR,PWM_FORMAT"); return; }
      value = value * 10 + (*p++ - '0');
      if (value > 4095) { Serial.println("ERR,PWM_RANGE"); return; }
    }
    if (estop()) { stop(); Serial.println("FAULT,ESTOP"); return; }
    lastHost = millis(); active = true; apply(value);
    Serial.print("ACK,PWM,"); Serial.println(code);
  } else Serial.println("ERR,COMMAND");
}
void setup() {
  digitalWrite(D11, VALVE_ACTIVE_HIGH ? LOW : HIGH);
  digitalWrite(D13, VALVE_ACTIVE_HIGH ? LOW : HIGH);
  pinMode(D11, OUTPUT); pinMode(D13, OUTPUT); pinMode(D5, OUTPUT);
#if USE_D8_ESTOP_INPUT
  pinMode(D8, INPUT_PULLUP);
#endif
  analogWriteResolution(12); analogWriteFrequency(PWM_HZ); stop();
  Serial.begin(115200); lastHost = millis(); ready();
}
void loop() {
  if (active && estop()) { stop(); Serial.println("FAULT,ESTOP"); }
  if (active && (uint32_t)(millis() - lastHost) > TIMEOUT_MS) {
    stop(); Serial.println("FAULT,HOST_TIMEOUT");
  }
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      rx[length] = 0;
      if (overflow) Serial.println("ERR,LINE_TOO_LONG");
      else if (length) command();
      length = 0; overflow = false;
    } else if (length < sizeof(rx) - 1 && !overflow) rx[length++] = c;
    else overflow = true;
  }
}
