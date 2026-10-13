#include <assert.h>
#include <math.h>
#include "../firmware/single_lung_controller_stm32/flow_calibration.h"
static bool near(float a, float b) { return fabsf(a-b) < 1e-5f; }
int main() {
  assert(near(calibratedFlow(.0034f, .0034f, 1.0471f, 1), 0));
  assert(near(calibratedFlow(10.0034f, .0034f, 1.0471f, 1), 10.471f));
  assert(near(calibratedFlow(10.0034f, .0034f, 1.0471f, -1), -10.471f));
  assert(near(vacLeakAtVoltage(.814f, 4.00f, 4.30f, 4.35f), .814f));
  assert(near(vacLeakAtVoltage(.814f, 4.30f, 4.30f, 4.35f), .814f));
  assert(near(vacLeakAtVoltage(.814f, 4.325f, 4.30f, 4.35f), .407f));
  assert(near(vacLeakAtVoltage(.814f, 4.35f, 4.30f, 4.35f), 0));
  assert(near(vacLeakAtVoltage(.814f, 5.00f, 4.30f, 4.35f), 0));
  assert(near(netLungFlow(-10, 2, .814f), -9.186f));
  assert(near(netLungFlow(-.814f, 2, .814f), 0));
  assert(near(netLungFlow(-.2f, 2, .814f), 0));
  assert(near(netLungFlow(2, 2, .814f), 2));
  assert(near(netLungFlow(-10, 0, .814f), -10));
  assert(near(netLungFlow(-10, 1, .814f), -10));
  assert(near(netLungFlow(-10, 2, 0), -10));
}
