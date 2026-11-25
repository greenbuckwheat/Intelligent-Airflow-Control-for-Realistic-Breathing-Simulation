#include "../flow_zero.h"
#include <assert.h>

int main() {
  FlowZeroAverage z;
  for (int i=0; i<49; ++i) {
    assert(z.add(i % 2 ? 0.08f : 0.12f));
    assert(!z.complete());
  }
  assert(z.add(0.08f));
  assert(z.complete());
  assert(fabsf(z.mean()-0.1f) < 1e-6f);
  z.reset();
  assert(!z.add(1.0f));  // Actual flow: reject, do not turn it into zero.
  z.reset();
  assert(z.add(-0.2f));
  assert(!z.add(0.2f));  // Unstable samples, although each is below 0.5 SLM.
  z.reset();
  assert(!z.add(NAN));
  z.reset();
  for (int i=0; i<50; ++i) assert(z.add(-0.05f));
  assert(fabsf(z.mean()+0.05f) < 1e-6f);  // Second zero is absolute, not cumulative.
}
