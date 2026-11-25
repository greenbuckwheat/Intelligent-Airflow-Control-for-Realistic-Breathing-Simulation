#pragma once
#include <math.h>
#include <stdint.h>

// Bench zero acceptance limits in uncorrected SLM; not a flow calibration.
struct FlowZeroAverage {
  static const uint16_t SAMPLES = 50;
  uint16_t count = 0;
  float sum = 0, low = 0, high = 0;
  void reset() { count = 0; sum = low = high = 0; }
  bool add(float rawSlm) {
    if (!isfinite(rawSlm) || fabsf(rawSlm) > 0.5f) return false;
    if (!count) low = high = rawSlm;
    if (rawSlm < low) low = rawSlm;
    if (rawSlm > high) high = rawSlm;
    if (high - low > 0.3f) return false;
    sum += rawSlm;
    ++count;
    return true;
  }
  bool complete() const { return count >= SAMPLES; }
  float mean() const { return count ? sum / count : 0; }
};
