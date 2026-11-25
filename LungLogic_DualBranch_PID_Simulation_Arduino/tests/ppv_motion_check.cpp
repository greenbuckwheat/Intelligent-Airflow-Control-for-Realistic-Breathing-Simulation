#include <assert.h>
#include <math.h>
#include <initializer_list>
#include "../firmware/single_lung_controller_stm32/ppv_motion.h"

static bool near(float a, float b) { return fabsf(a - b) < 1e-5f; }
int main() {
  // v8 expiration cap: .1883 to 250 ms, .3451 at 750 ms, .4707 at phase end.
  assert(near(ppvExpirationOpeningLimit(0,6667),.1883f));
  assert(near(ppvExpirationOpeningLimit(250,6667),.1883f));
  assert(near(ppvExpirationOpeningLimit(750,6667),.3451f));
  assert(near(ppvExpirationOpeningLimit(6667,6667),.4707f));
  assert(near(ppvExpirationOpeningLimit(8000,6667),.4707f));
  assert(near(ppvExpirationOpeningLimit(800,100),.4707f)); // Short phase: 250 ms floor.
  // Zero output must be continuous with tiny positive openings, in both routes.
  for (float deadZone : {4.15f, 4.08f}) {
    assert(near(ppvCommandVoltage(0, deadZone, 4.35f), 4.35f));
    assert(near(ppvCommandVoltage(-1, deadZone, 4.35f), 4.35f));
    assert(near(ppvCommandVoltage(0.000001f, deadZone, 4.35f), 4.35f));
    assert(near(ppvCommandVoltage(1, deadZone, 4.3f), 0));
  }
  // Closing anticipation lowers positive inspiratory feedback, while opening
  // anticipation raises it. Signed expiration must work in the opposite direction.
  assert(near(ppvSmithFeedback(10, 0, 10, .6f), 4));
  assert(near(ppvSmithFeedback(0, 10, 0, .6f), 6));
  assert(near(ppvSmithFeedback(-10, 0, -10, .6f), -4));
  assert(near(ppvSmithFeedback(7, 4, 4, .6f), 7));
  assert(near(ppvSmithFeedback(7, 0, 10, 0), 7));
  // Afternoon data: 4.25 and 4.259 V still conduct, 4.30 V is the new anchor.
  for (float deadZone : {4.1200f, 4.1535f}) {
    assert(ppvEffectiveOpening(4.25f, deadZone, 4.30f) > 0);
    assert(ppvEffectiveOpening(4.259f, deadZone, 4.30f) > 0);
    assert(near(ppvEffectiveOpening(4.30f, deadZone, 4.30f), 0));
    assert(near(ppvCommandVoltage(0, deadZone, 4.30f), 4.30f));
    float lastV = 4.30f;
    for (int step = 1; step <= 1000; ++step) {
      float opening = step / 1000.f;
      float v = ppvCommandVoltage(opening, deadZone, 4.30f);
      assert(v <= lastV);
      assert(near(ppvEffectiveOpening(v, deadZone, 4.30f), opening));
      lastV = v;
    }
  }
  // Preserved opening caps now map ABOVE the disproven 4.25 V closure point.
  assert(ppvCommandVoltage(.1883f, 4.1535f, 4.30f) > 4.27f);
  // Inspiration FF is a relative conductance and reproduces the net flow.
  for (float volume : {50.f, 100.f, 250.f}) {
    float fi = ppvFeedforwardConductance(4.5f, 320.3f-volume, .5853f);
    assert(near(.06f*(320.3f-volume)/.5853f*fi,4.5f));
  }
  assert(near(ppvFeedforwardConductance(0,100,.5853f),0));
  assert(near(ppvFeedforwardConductance(2.25f,0,.5853f),1));
  assert(near(ppvFeedforwardConductance(200,1,.5853f),1));
  // Continuous ATM map: PM power law near closure, measured curve below.
  {
    const float Ud=4.12f, Uc=4.30f, g=1.7861f;
    assert(near(ppvAtmCommandVoltage(0,Ud,Uc,g),Uc));
    assert(near(ppvAtmCommandVoltage(1,Ud,Uc,g),0));
    for (float v : {4.30f,4.28f,4.25f,4.21f,4.20f})  // Unchanged near closure.
      assert(near(ppvAtmConductance(v,Ud,Uc,g),
                  powf(ppvEffectiveOpening(v,Ud,Uc),g)));
    assert(fabsf(ppvAtmConductance(4.05f,Ud,Uc,g)-.70f)<.02f); // AM 4.00 V: .698
    assert(fabsf(ppvAtmConductance(3.75f,Ud,Uc,g)-.89f)<.02f); // AM 3.70 V: .888
    assert(ppvAtmConductance(4.12f,Ud,Uc,g) < .7f);  // No plateau at Ud any more.
    // Default 3.0 V inspiration floor: ~4% below full-open conductance.
    float umax=ppvAtmConductance(3.0f,Ud,Uc,g);
    assert(umax>.95f && umax<.97f);
    assert(near(ppvAtmCommandVoltage(umax,Ud,Uc,g),3.0f));
    float lastV=Uc;
    for (int i=1;i<=20000;++i) {
      float c=i/20000.f, v=ppvAtmCommandVoltage(c,Ud,Uc,g);
      assert(v<=lastV+1e-6f);
      assert(lastV-v < .01f);  // No jump anywhere, including near 4.12 V.
      assert(fabsf(ppvAtmConductance(v,Ud,Uc,g)-c)<1e-4f);
      lastV=v;
    }
    assert(near(ppvAtmCommandVoltage(.999999f,Ud,Uc,g),ppvAtmCommandVoltage(1,Ud,Uc,g)) ||
           ppvAtmCommandVoltage(.999999f,Ud,Uc,g) < .01f);
  }
  assert(near(ppvEffectiveOpening(4.35f, 4.15f, 4.35f), 0));
  assert(near(ppvEffectiveOpening(5, 4.08f, 4.35f), 0));
  // 2026-10-09 measured constant-speed 0 <-> 5 V stroke: 2.9 s.
  const float measuredEquivalent = 2.436f;
  assert(near(ppvTravelSeconds(0, 5, measuredEquivalent, measuredEquivalent), 2.9f));
  assert(near(ppvTravelSeconds(5, 0, measuredEquivalent, measuredEquivalent), 2.9f));
  assert(near(ppvTravelSeconds(3, 4, measuredEquivalent, measuredEquivalent), .58f));
  assert(near(ppvAdvance(0, 5, 1.45f, measuredEquivalent, measuredEquivalent), 2.5f));
  assert(near(ppvAdvance(5, 0, 2.9f, measuredEquivalent, measuredEquivalent), 0));
  // 2.1 s closing calibration -> 2 V/s; a 1 V move takes 0.5 s.
  assert(near(ppvTravelSeconds(3, 4, 4.2f, 2.1f), 0.5f));
  float p = ppvAdvance(3, 4, 0.2f, 4.2f, 2.1f);
  assert(near(p, 3.4f));
  assert(near(ppvTravelSeconds(p, 4, 4.2f, 2.1f), 0.3f));
  // Reversal starts from actual estimated position, not previous target 4 V.
  p = ppvAdvance(p, 3, 0.1f, 4.2f, 2.1f);
  assert(near(p, 3.3f));
  assert(near(ppvAdvance(p, 3, 10, 4.2f, 2.1f), 3));
  assert(near(ppvAdvance(0, 4.2f, 2.1f, 4.2f, 2.1f), 4.2f));
  assert(near(ppvAdvance(4.2f, 0, 4.2f, 4.2f, 2.1f), 0));
  assert(near(ppvAdvance(3, 4, 0, 4.2f, 2.1f), 3));
  assert(near(ppvAdvance(3, 3, 1, 4.2f, 2.1f), 3));
  // Full-open flow plateau does not erase travel through that region.
  assert(near(ppvAdvance(0, 4, 1, 4.2f, 2.1f), 2));
  assert(near(ppvEffectiveOpening(2, 3.73f, 4.2f), 1));
  assert(near(ppvEffectiveOpening(5, 3.73f, 4.2f), 0));
  assert(near(ppvEffectiveOpening(4, 3.73f, 4.2f), 0.2f / 0.47f));
  assert(near(ppvEffectiveOpening(4, 3.98f, 4.2f), 0.2f / 0.22f));
  assert(near(ppvAdvance(5, 0, 1, 4.2f, 2.1f), 4));
  // Protocol 17 transition: arrival AND minimum dwell since the close command.
  assert(!ppvTransitionReady(4.30f, 4.30f, 340, 1000));  // Run 1109152100 exit.
  assert(!ppvTransitionReady(4.20f, 4.30f, 5000, 1000)); // Dwell alone is not enough.
  assert(ppvTransitionReady(4.30f, 4.30f, 1000, 1000));
  assert(ppvTransitionReady(4.40f, 4.40f, 0, 0));        // Zero dwell = protocol 16.
  // Protocol 19: settle counted from the estimated arrival, not the command.
  assert(!ppvTransitionSettled(false, 5000, 300, 5000, 0));  // Not arrived.
  assert(!ppvTransitionSettled(true, 299, 300, 900, 0));
  assert(ppvTransitionSettled(true, 300, 300, 970, 0));      // 3.15 V end: 0.67 + 0.3 s.
  assert(!ppvTransitionSettled(true, 300, 300, 400, 600));   // Optional floor.
  // Within-cycle expiration: 10 bpm, I:E 1:2 -> 6 s period, 4 s nominal.
  assert(ppvExpirationDurationMs(false, 6000, 4000, 3300) == 4000);
  assert(ppvExpirationDurationMs(true, 6000, 4000, 3040) == 2960);
  assert(ppvExpirationDurationMs(true, 6000, 4000, 5900) == 250);  // Floor.
  assert(ppvExpirationDurationMs(true, 6000, 4000, 7000) == 250);  // No unsigned wrap.
}
