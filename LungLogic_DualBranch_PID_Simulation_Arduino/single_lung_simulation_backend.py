"""Offline protocol-21 controller twin; never opens a physical serial port.

Control policies follow single_lung_controller_stm32.ino and ppv_motion.h.
The synthetic pneumatic path is an explicit nominal model, not a replay or
validation of the real apparatus. Keep this separate from shared dataclasses:
the hardware GUI imports those classes and their defaults must not change.
"""
from collections import deque
import copy
from dataclasses import asdict, dataclass
import math

import numpy as np

import single_lung_hardware_gui as hw
from single_lung_pid_gui import (
    STATE_IDLE, STATE_PRECONDITION, STATE_DEAD_TO_INSP, STATE_INSPIRATION,
    STATE_DEAD_TO_EXP, STATE_EXPIRATION, STATE_COMPLETE, STATE_FAULT,
)


def clip(value, low=0.0, high=1.0):
    return min(high, max(low, value))


DEPTH = (.10, .15, .20, .25, .30, .40, .45, .55, .85, 1.25, 2.25, 3.25)
FRACTION = (0., .256, .442, .553, .615, .738, .765, .834, .870, .936, .962, .976)


def atm_conductance(volts, p):
    depth = p.hardware_closure_v - volts
    if depth <= 0:
        return 0.0
    span = p.hardware_closure_v - p.hardware_dead_zone_in_v
    if depth <= .10:
        return (depth / span) ** p.hardware_gamma_in
    join = (.10 / span) ** p.hardware_gamma_in
    fraction = float(np.interp(depth, (*DEPTH, p.hardware_closure_v), (*FRACTION, 1.)))
    return join + (1 - join) * fraction


def atm_voltage(conductance, p):
    if conductance <= 0:
        return p.hardware_closure_v
    if conductance >= 1:
        return 0.0
    span = p.hardware_closure_v - p.hardware_dead_zone_in_v
    join = (.10 / span) ** p.hardware_gamma_in
    if conductance <= join:
        return p.hardware_closure_v - span * conductance ** (1 / p.hardware_gamma_in)
    fraction = (conductance - join) / (1 - join)
    return p.hardware_closure_v - float(np.interp(
        fraction, (*FRACTION, 1.), (*DEPTH, p.hardware_closure_v)))


def advance_position(position, target, dt, cfg):
    stroke = cfg.smith.ppv_open_time_s if target < position else cfg.smith.ppv_close_time_s
    step = 4.2 * dt / stroke
    return position + clip(target - position, -step, step)


@dataclass
class VirtualPlantSettings:
    # Independent of whether compensation is enabled or subsequently edited.
    # None resolves ONCE from the reference config, then remains fixed.
    residual_delay_s: float | None = None
    noise_slm: float = 0.0
    sensor_bias_slm: float = 0.0


class VirtualController:
    dt = .01

    def __init__(self, cfg, plant_settings=None):
        self.settings = copy.deepcopy(plant_settings or VirtualPlantSettings())
        if self.settings.residual_delay_s is None:
            self.settings.residual_delay_s = cfg.smith.delay_s
        self.set_plant_settings(self.settings)
        self.rng = np.random.default_rng(cfg.random_seed)
        self.position = 0.0  # Same open PPV as normal firmware idle.
        self.command = 0.0
        self.opening = 1.0
        self.mask = 0
        self.true_volume = 0.0
        self.sensor_delay = deque()
        self.running = False
        self.state = STATE_IDLE
        self.now_ms = 0
        self.run_id = 0
        self.configure(cfg)

    def set_plant_settings(self, settings):
        for name in ('residual_delay_s', 'noise_slm', 'sensor_bias_slm'):
            value = getattr(settings, name)
            if value is None or not math.isfinite(value):
                raise ValueError(f'{name} must be finite')
        if not 0 <= settings.residual_delay_s <= 1 or settings.noise_slm < 0:
            raise ValueError('Virtual delay must be 0..1 s and noise non-negative')
        self.settings = copy.deepcopy(settings)
        if hasattr(self, 'sensor_delay'):
            self.sensor_delay.clear()

    def configure(self, cfg):
        hw.config_command(cfg, 0)  # Exactly the hardware validation and mapping contract.
        self.cfg = copy.deepcopy(cfg)

    @property
    def guard(self):
        return self.cfg.max_lung_volume_ml + 50.0

    @property
    def max_opening(self):
        return atm_conductance(self.cfg.hardware_inspiration_min_v, self.cfg.lung)

    def _ff(self):
        if not self.cfg.hardware_ff_inspiration_enabled:
            return 0.0
        available = self.cfg.lung.hardware_model_volume_ml - self.volume
        qref = .06 * self.cfg.target_volume_ml / self.fill_s
        return min(self.max_opening, clip(qref / max(1e-12, .06 * available / self.cfg.lung.tau_in_s)))

    def _reset_control(self):
        self.integral = 0.0
        self.previous_measurement = None
        self.bias = 0.0
        self.held_opening = self.opening
        self.has_bias = False
        self.ideal_volume = self.volume
        self.actuator_volume = self.volume
        self.model_delay = deque([0.0] * int(self.cfg.smith.delay_s / self.dt + .5))
        self.ideal_flow = self.delayed_flow = 0.0

    def _write(self, opening, direction):
        self.opening = clip(opening)
        p = self.cfg.lung
        if direction > 0:
            self.command = atm_voltage(self.opening, p)
        elif direction < 0:
            self.command = self.cfg.hardware_expiration_closure_v - self.opening * (
                p.hardware_closure_v - p.hardware_dead_zone_out_v)
        else:
            self.command = p.hardware_closure_v - self.opening * (
                p.hardware_closure_v - p.hardware_dead_zone_in_v)

    def _close_transition(self):
        if self.close_command_ms is None:
            self.opening = 0.0
            self.command = self.cfg.hardware_expiration_closure_v + self.cfg.hardware_transition_overclose_v
            self.close_command_ms = self.now_ms
            self.close_arrival_ms = None

    def _set_state(self, state):
        previous = self.state
        self.mask = 0
        if state == STATE_DEAD_TO_INSP:
            self._write(self._ff(), 1)
        elif state == STATE_DEAD_TO_EXP:
            self._close_transition()
        self._reset_control()
        self.state = state
        c = self.cfg
        period_ms = int(c.cycle_period_s * 1000 + .5)
        if state == STATE_DEAD_TO_INSP:
            self.cycle_start_ms = self.now_ms
        if state == STATE_INSPIRATION:
            if previous == STATE_DEAD_TO_INSP and self.cycle == 0:
                self.cycle_start_ms = self.now_ms
            self.close_command_ms = self.close_arrival_ms = None
            self.volume = clip(self.inspired - self.expired_raw, 0, self.guard)
            self.inspired = 0.0
            nominal_ms = int(c.inspiration_s * 1000)
            if c.dead_time_within_cycle:
                available = max(500, period_ms - (self.now_ms-self.cycle_start_ms) - self.last_transition_ms)
                ti = int(available * c.ie_inspiration / (c.ie_inspiration+c.ie_expiration) + .5)
                self.inspiration_ms = min(nominal_ms, max(250, ti))
            else:
                self.inspiration_ms = nominal_ms
            self.fill_s = min(c.target_fill_time_s, self.inspiration_ms / 1000)
        if state == STATE_EXPIRATION:
            self.last_transition_ms = self.now_ms - self.close_command_ms
            self.budget = min(c.target_volume_ml, self.inspired)
            self.expired = self.expired_raw = 0.0
            self.expiration_ms = (max(250, period_ms - (self.now_ms-self.cycle_start_ms))
                                  if c.dead_time_within_cycle else int(c.expiration_s*1000))
        self.state_start_ms = self.now_ms

    def start(self, run_id, cycles):
        self.run_id = run_id
        self.cycles = max(1, cycles)
        self.cycle = 0
        self.now_ms = self.step_count = 0
        self.volume = self.inspired = self.expired = self.expired_raw = self.budget = 0.0
        self.close_command_ms = self.close_arrival_ms = None
        self.last_transition_ms = int(max(self.cfg.hardware_transition_dwell_min_s,
                                          self.cfg.hardware_transition_settle_s)*1000+.5)
        self.inspiration_ms = int(self.cfg.inspiration_s*1000)
        self.fill_s = self.cfg.target_fill_time_s
        self.stopped_by_user = False
        self.running = True
        self._set_state(STATE_PRECONDITION)
        return [f'STARTED,{run_id}']

    def stop(self):
        if not self.running:
            self.mask = 0
            self._write(1.0, 0)
            return [f'STOPPED,{self.run_id}']
        if self.state == hw.STATE_RECOVERY:
            self.stopped_by_user = True
            return [f'RECOVERING,{self.run_id},STOP']
        return self._begin_recovery(True)

    def _begin_recovery(self, stopped):
        self.mask = 0
        self._write(1., 1)
        self._reset_control()
        self.state = hw.STATE_RECOVERY
        self.state_start_ms = self.now_ms
        self.stopped_by_user = stopped
        return [f'RECOVERING,{self.run_id},{"STOP" if stopped else "DONE"}']

    def _flow_model(self, volts, direction, volume):
        p = self.cfg.lung
        if direction > 0:
            dvdt = atm_conductance(volts, p) * (p.hardware_model_volume_ml-volume) / p.tau_in_s
        else:
            opening = clip((self.cfg.hardware_expiration_closure_v-volts) /
                           (p.hardware_closure_v-p.hardware_dead_zone_out_v))
            dvdt = -opening ** p.hardware_gamma_out * volume / p.tau_out_s
        return .06*dvdt, clip(volume+dvdt*self.dt, 0, p.hardware_model_volume_ml)

    def _leak(self):
        c = self.cfg
        return c.lung.hardware_vac_leak_slm * clip((c.hardware_expiration_closure_v-self.position)/.041)

    def _sample(self):
        true = 0.0
        if self.mask:
            # Flow equals the actual model volume change, including clipping.
            _, next_volume = self._flow_model(self.position, 1 if self.mask == 1 else -1, self.true_volume)
            true = .06*(next_volume-self.true_volume)/self.dt
            self.true_volume = next_volume
        calibrated = true - (self._leak() if self.mask == 2 else 0.)
        self.sensor_delay.append(calibrated)
        samples = int(self.settings.residual_delay_s/self.dt+.5)
        calibrated = self.sensor_delay.popleft() if len(self.sensor_delay)>samples else 0.0
        calibrated += self.settings.sensor_bias_slm + self.rng.normal(0, self.settings.noise_slm)
        p = self.cfg.lung
        raw = calibrated/(p.flow_sign*p.hardware_flow_gain) + p.hardware_flow_offset_slm
        # Same calibration and VAC compensation, based on route at sampling time.
        measured = p.flow_sign*p.hardware_flow_gain*(raw-p.hardware_flow_offset_slm)
        net = min(0.0, measured+self._leak()) if self.mask == 2 and measured < 0 else measured
        return raw, measured, net

    def _pid(self, reference, measured, ff, limit):
        c = self.cfg
        expiration = self.state == STATE_EXPIRATION
        kp = c.hardware_expiration_kp if expiration else c.pi.kp
        ki = c.hardware_expiration_ki if expiration else c.pi.ki
        kd = 0.0 if expiration else c.pi.kd
        ilim = c.hardware_expiration_integral_limit if expiration else c.pi.integral_limit
        error = reference-measured
        if not self.has_bias:
            self.bias = self.held_opening-ff-kp*error
            self.has_bias = True
            self.previous_measurement = measured
            return self.held_opening
        candidate = clip(self.integral+error*self.dt, -ilim, ilim)
        derivative = -(measured-self.previous_measurement)/self.dt
        self.previous_measurement = measured
        self.bias -= clip(self.bias, -50*self.dt, 50*self.dt)
        raw = self.bias+ff+kp*error+ki*candidate+kd*derivative
        result = clip(raw, 0, limit)
        if 0 < raw < limit or raw <= 0 and error > 0 or raw >= limit and error < 0:
            self.integral = candidate
        return result

    def step(self):
        self.now_ms += 10
        self.position = advance_position(self.position, self.command, self.dt, self.cfg)
        raw, measured, net = self._sample()
        if not self.running:
            return []
        c = self.cfg
        sampled_mask = self.mask
        if self.state == STATE_INSPIRATION and sampled_mask == 1:
            self.inspired += max(0, net)*self.dt/.06
        if self.state == STATE_EXPIRATION and sampled_mask == 2:
            self.expired += max(0, -net)*self.dt/.06
            self.expired_raw += max(0, -measured)*self.dt/.06
        feedback = net
        if c.smith.enabled and self.state in (STATE_INSPIRATION, STATE_EXPIRATION) and self.mask:
            direction = 1 if self.mask == 1 else -1
            self.ideal_flow, self.ideal_volume = self._flow_model(self.command, direction, self.ideal_volume)
            actuator, self.actuator_volume = self._flow_model(self.position, direction, self.actuator_volume)
            self.model_delay.append(actuator)
            self.delayed_flow = self.model_delay.popleft()
            feedback = net+c.smith.correction_gain*(self.ideal_flow-self.delayed_flow)
        else:
            self.ideal_flow = self.delayed_flow = 0.0
        if abs(measured) > c.max_abs_flow_slm:
            self.running = False
            self.state, self.mask, self.command, self.opening = STATE_FAULT, 0, 5., 0.
            return [f'FAULT,{self.run_id},FLOW_LIMIT']
        if self.close_command_ms is not None and self.close_arrival_ms is None and abs(self.position-self.command)<=.0001:
            self.close_arrival_ms = self.now_ms
        elapsed = self.now_ms-self.state_start_ms
        previous_state = self.state
        events = []
        if self.state == STATE_PRECONDITION and elapsed >= int(c.precondition_s*1000):
            self.volume = 0.0
            self._set_state(STATE_DEAD_TO_INSP)
        elif self.state == STATE_DEAD_TO_INSP and abs(self.position-self.command)<=.0001 and elapsed>=c.hardware_inspiration_dwell_min_s*1000:
            self._set_state(STATE_INSPIRATION)
        elif self.state == STATE_INSPIRATION and elapsed >= self.inspiration_ms:
            self._set_state(STATE_DEAD_TO_EXP)
        elif self.state == STATE_DEAD_TO_EXP and self.close_arrival_ms is not None and self.now_ms-self.close_arrival_ms>=c.hardware_transition_settle_s*1000 and self.now_ms-self.close_command_ms>=c.hardware_transition_dwell_min_s*1000:
            self._set_state(STATE_EXPIRATION)
        elif self.state == STATE_EXPIRATION and elapsed >= self.expiration_ms:
            self.cycle += 1
            if self.cycle >= self.cycles:
                events += self._begin_recovery(False)
            else:
                self._set_state(STATE_DEAD_TO_INSP)
        elif self.state == hw.STATE_RECOVERY and (self.volume>=c.target_volume_ml or elapsed>=5000):
            if self.volume < c.target_volume_ml:
                events.append(f'WARN,RECOVERY_TIMEOUT,{self.run_id}')
            self.running = False
            self.state = STATE_IDLE if self.stopped_by_user else STATE_COMPLETE
            self.mask = 0
            self._write(1., 0)
            events.append(f'{"STOPPED" if self.stopped_by_user else "DONE"},{self.run_id}')
            return events
        phase_changed = previous_state != self.state
        if phase_changed:
            feedback = net  # resetSmith() resets feedback on firmware state entry.
        qref = 0.0
        direction = 0
        if self.state == STATE_PRECONDITION:
            direction = -1
            self._write(1., direction)
        elif self.state == STATE_INSPIRATION:
            self.volume = clip(self.volume+max(0, net)*self.dt/.06, 0, self.guard)
            qref = .06*c.target_volume_ml/self.fill_s
            if self.inspired<c.target_volume_ml and self.volume<self.guard:
                direction = 1
                opening = self.opening if phase_changed else self._pid(qref, max(0, feedback), self._ff(), self.max_opening)
                self._write(opening, direction)
            else:
                self._close_transition()
        elif self.state == STATE_EXPIRATION:
            self.volume = clip(self.volume+min(0, measured)*self.dt/.06, 0, self.guard)
            qref = -.06*c.target_volume_ml/max(.25, self.expiration_ms/1000)
            direction = -1
            opening = self.opening if phase_changed else self._pid(-qref, max(0, -feedback), 0.,
                hw.expiration_opening_cap(self.now_ms-self.state_start_ms, self.expiration_ms))
            self._write(opening, direction)
        elif self.state == hw.STATE_RECOVERY:
            self.volume = clip(self.volume+max(0, net)*self.dt/.06, 0, self.guard)
            if self.volume < c.target_volume_ml:
                direction = 1
                self._write(1., direction)
        self.mask = 1 if direction>0 else 2 if direction<0 else 0
        if self.step_count % 2 == 0:
            row = hw.HardwareTelemetry(self.run_id, self.now_ms, self.state, self.cycle,
                measured, self.volume, qref, self.opening, self.command, self.mask,
                self.position, abs(self.command-self.position)*(
                    c.smith.ppv_open_time_s if self.command<self.position else c.smith.ppv_close_time_s)/4.2,
                raw, net, sampled_mask, feedback, self.ideal_flow, self.delayed_flow,
                self.inspired, self.expired, self.budget, False)
            fields = list(asdict(row).values())
            fields[-1] = int(fields[-1])
            events.append('TEL,'+','.join(map(str, fields)))
        self.step_count += 1
        return events


class VirtualSerial:
    """Minimal in-process transport for the unmodified hardware UI protocol."""
    def __init__(self, messages, cfg, settings=None):
        self.messages = messages
        self.controller = VirtualController(cfg, settings)
        self.pending_config = copy.deepcopy(cfg)
        self.closed = False

    def emit(self, lines):
        for line in lines:
            self.messages.put(line)

    def write(self, data):
        if self.closed:
            raise OSError('Virtual device disconnected')
        fields = data.decode('ascii').strip().split(',')
        command = fields[0]
        if command == 'HELLO':
            self.emit([hw.READY, 'INFO,SIMULATED_DATA=1,CONTROL_HZ=100,TELEMETRY_HZ=50'])
        elif command == 'CFG':
            self.controller.configure(self.pending_config)
            self.emit([f'ACK,CFG,{fields[2]}'])
        elif command == 'START':
            self.emit(self.controller.start(int(fields[1]), int(fields[2])))
        elif command == 'STOP':
            self.emit(self.controller.stop())
        elif command == 'MANUAL':
            if self.controller.running:
                self.emit(['ERR,MANUAL_RUNNING'])
            elif self.controller.state == STATE_FAULT:
                self.emit(['ERR,MANUAL_FAULT'])
            else:
                c = self.controller
                action = ','.join(fields[1:])
                if action == 'ATM,OPEN':
                    c.mask = 1
                elif action == 'VAC,OPEN':
                    c.mask = 2
                elif action == 'ATM,CLOSE':
                    c.mask &= 2
                elif action == 'VAC,CLOSE':
                    c.mask &= 1
                elif action == 'PPV,OPEN':
                    c._write(1., 0)
                else:
                    self.emit(['ERR,MANUAL_FORMAT'])
                    return len(data)
                self.emit([f'ACK,MANUAL,{c.mask},{c.command}'])
        elif command != 'PING':
            self.emit(['ERR,UNKNOWN_COMMAND'])
        return len(data)

    def advance(self, steps):
        for _ in range(steps):
            self.emit(self.controller.step())

    def close(self):
        self.closed = True
        self.controller.running = False
        self.controller.mask = 0


def run_hardware_aligned_simulation(cfg=None, *, plant_settings=None, run_id=1,
                                    max_duration_s=None):
    """Batch the current controller twin; return (50 Hz telemetry, events).

    Unlike the historical finite-fill API, this defaults to the hardware
    reference and applies separate phase PI, empirical mapping and motion.
    """
    cfg = copy.deepcopy(cfg if cfg is not None else hw.load_config(hw.CONFIG_PATH))
    controller = VirtualController(cfg, plant_settings)
    events = controller.start(run_id, cfg.cycles)
    rows = []
    if max_duration_s is None:
        max_duration_s = cfg.precondition_s + cfg.cycles * (
            cfg.cycle_period_s + cfg.smith.ppv_open_time_s + cfg.smith.ppv_close_time_s
            + cfg.hardware_inspiration_dwell_min_s + cfg.hardware_transition_dwell_min_s
            + cfg.hardware_transition_settle_s) + 15
    for _ in range(math.ceil(max_duration_s / controller.dt)):
        for line in controller.step():
            if line.startswith('TEL,'):
                rows.append(hw.HardwareTelemetry.parse(line))
            else:
                events.append(line)
        if not controller.running:
            return rows, events
    raise RuntimeError('Virtual run exceeded its duration bound; check timing or increase max_duration_s')
