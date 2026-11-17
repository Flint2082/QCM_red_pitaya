function [u, enabled, status] = tec_controller_block(setpoint, pt100, crystal, ctl_params)
%TEC_CONTROLLER_BLOCK  The Pitaya's TEC loop, as the "TEC controller" block runs it.
%
%   The script of the MATLAB Function block inside tec_controller_lib/TEC controller
%   (make_tec_controller.m copies this file into it). Called once per 0.1 s tick,
%   like TECWorker._tick in src/workers/tec_worker.py, and a port of:
%     domain/tec_controller.PID           parallel PID, P and I on the PT100, the
%                                         derivative on the measurement through a
%                                         5 s filter, conditional integration,
%                                         integral only within i_band of the
%                                         setpoint (or when stalled short of it),
%                                         output +-max
%     domain/tec_controller.CrystalCheck  with "PT100 + crystal for D": the
%                                         derivative on the crystal, set aside on a
%                                         jump or a crystal moving the wrong way or
%                                         far off scale, back after steady readings
%                                         once a move has measured it; meanwhile a
%                                         quarter of Kd on the PT100
%     workers/tec_worker.py               a step on each fresh reading, at least
%                                         0.5 s apart (the PT100 reads once a
%                                         second, the crystal ten times); the
%                                         derivative restarts when the crystal joins
%                                         or leaves; the TEC switches off when the
%                                         PT100 leaves t_min..t_max
%   Not ported: the relay autotune, stale-reading handling (the readings here are
%   always current; a NaN crystal counts as unlocked).
%
%   Inputs:  setpoint (degC), PT100 (degC), crystal reading (degC, NaN = unlocked)
%   ctl_params: [source kp ki kd i_band max_output t_min t_max pt100_interval_s
%                initial_integral], source 1 = PT100, 2 = PT100 + crystal for D
%   Outputs: u (% drive, + heats), enabled (0 after a limit trip),
%            status = [derivative on the crystal; crystal/PT100 move ratio;
%                      PT100 (degC)]

TICK = 0.1;          % s, TECWorker.TICK_S
MIN_STEP = 0.5;      % s, TECWorker.MIN_STEP_S
MAX_DT = 5.0;        % s, TECWorker.MAX_DT
KD_SHARE = 0.25;     % CrystalCheck.PT100_KD_SHARE
NBUF = 1300;         % ring buffer for the move check (> STEADY_S/TICK + 1)

persistent S
if isempty(S)
    S = init_state(pt100, ctl_params, NBUF);
end
P = ctl_params;
crystal_d = (P(1) == 2);
k = S.k;

% The LTC2986 reads the PT100 once every pt100_interval_s.
nP = max(1, round(P(9) / TICK));
if mod(k, nP) == 0
    S.p = pt100;
    S.p_k = k;
end

% TECWorker._read: (timestamp in ticks, PT100, what the derivative acts on).
ts_k = S.p_k;
pvd = S.p;
if crystal_d
    S = check_update(S, S.p, k, crystal, ~isnan(crystal), TICK, NBUF);
    if S.c_in_use
        ts_k = k;
        pvd = crystal;
    end
end

% TECWorker._watch_crystal: the derivative restarts when it changes reading.
on_crystal = crystal_d && S.c_in_use;
if on_crystal ~= S.on_crystal_prev
    S.on_crystal_prev = on_crystal;
    S.has_last = false;
    S.slope = 0;
end

% TECWorker._tick / _step
min_k = round(MIN_STEP / TICK);
fresh = ts_k >= 0 && (~S.has_last_ts || (ts_k ~= S.last_ts && ts_k - S.last_ts >= min_k));
if S.running && fresh
    if S.has_last_ts
        dt = min((ts_k - S.last_ts) * TICK, MAX_DT);
    else
        dt = 0;
    end
    S.last_ts = ts_k;
    S.has_last_ts = true;
    kd = P(4);
    if crystal_d && ~on_crystal
        kd = P(4) * KD_SHARE;
    end
    if S.p < P(7) || S.p > P(8)
        S.running = false;       % TECWorker._trip: TEC off
        S.u = 0;
    else
        [S, S.u] = pid_step(S, setpoint, S.p, pvd, dt, P(2), P(3), kd, P(5), P(6));
    end
elseif fresh
    S.last_ts = ts_k;
    S.has_last_ts = true;
end

u = S.u;
enabled = double(S.running);
status = [double(on_crystal); S.scale; S.p];
S.k = k + 1;
end


function S = init_state(pt100, P, NBUF)
S.k = 0;
S.running = true;
S.u = 0;
S.i = P(10);                 % the integral starts at the drive given
S.has_last = false; S.last = 0; S.slope = 0;
S.has_last_ts = false; S.last_ts = 0;
S.p = pt100; S.p_k = 0;
S.on_crystal_prev = false;
S.c_in_use = false; S.c_has_last = false; S.c_last = 0; S.c_steady_since = NaN;
S.m_ts = zeros(NBUF, 1); S.m_p = zeros(NBUF, 1); S.m_c = zeros(NBUF, 1);
S.m_head = 1; S.m_n = 0;
S.ml_has = false; S.ml_t = 0; S.ml_v = 0;
S.ref_has = false; S.ref_p = 0; S.ref_c = 0;
S.scale = NaN;
end


function [S, out] = pid_step(S, sp, pvp, pvd, dt, kp, ki, kd, iband, umax)
% domain/tec_controller.PID.step, with P and I on pvp and D on pvd.
D_FILTER = 5.0;
e = sp - pvp;
if S.has_last && dt > 0
    raw = (pvd - S.last) / dt;
    S.slope = S.slope + dt / (dt + D_FILTER) * (raw - S.slope);
end
S.last = pvd;
S.has_last = true;
p = kp * e;
d = -kd * S.slope;
if iband <= 0 || abs(e) <= iband || ki <= 0 || kp <= 0
    integ = true;
else
    if e > 0, toward = S.slope; else, toward = -S.slope; end
    integ = toward * (kp / ki) < abs(e);
end
if integ
    inew = clampv(S.i + ki * e * dt, umax);
else
    inew = clampv(S.i, umax);
end
uu = p + inew + d;
if (uu > umax && e > 0) || (uu < -umax && e < 0)
    inew = clampv(S.i, umax);
    uu = p + inew + d;
end
S.i = inew;
out = clampv(uu, umax);
end


function y = clampv(x, lim)
y = min(max(x, -lim), lim);
end


function S = check_update(S, p, c_k, c, valid, TICK, NBUF)
% domain/tec_controller.CrystalCheck.update: whether the derivative may use the crystal.
JUMP = 0.5; REJOIN = 30.0;
lo = 0.25; hi = 2.0;
c_ts = c_k * TICK;
if ~valid
    S = cdrop(S);
    return
end
prev_has = S.c_has_last; prev_c = S.c_last;
S.c_has_last = true; S.c_last = c;
if prev_has && abs(c - prev_c) > JUMP
    % a mode hop is no move of the crystal's temperature, and the crystal may
    % now read another mode: measure it afresh before it is used
    S.m_n = 0; S.m_head = 1; S.ml_has = false; S.ref_has = false; S.scale = NaN;
    S = cdrop(S);
    return
end
S = compare_moves(S, c_ts, p, c, NBUF);
if ~isnan(S.scale) && ~(S.scale >= lo && S.scale <= hi)
    S = cdrop(S);
    return
end
if isnan(S.c_steady_since)
    S.c_steady_since = c_ts;
end
if ~S.c_in_use
    if c_ts - S.c_steady_since < REJOIN || isnan(S.scale)
        return
    end
    S.c_in_use = true;
end
end


function S = cdrop(S)
S.c_in_use = false;
S.c_steady_since = NaN;
end


function S = compare_moves(S, ts, p, c, NBUF)
% CrystalCheck._compare_moves: the crystal's change as a multiple of the PT100's,
% from where both last held still.
STEADY = 120.0; STEADY_PT = 0.1; STEADY_CR = 0.4; MOVE = 1.0; LAG = 20.0;
if ~S.ml_has
    lagged = c;
else
    dt = max(ts - S.ml_t, 0);
    lagged = S.ml_v + dt / (dt + LAG) * (c - S.ml_v);
end
S.ml_has = true; S.ml_t = ts; S.ml_v = lagged;
if S.m_n == NBUF                     % full: drop the oldest
    S.m_head = mod(S.m_head, NBUF) + 1;
    S.m_n = S.m_n - 1;
end
idx = mod(S.m_head - 1 + S.m_n, NBUF) + 1;
S.m_ts(idx) = ts; S.m_p(idx) = p; S.m_c(idx) = lagged;
S.m_n = S.m_n + 1;
while S.m_n > 1 && ts - S.m_ts(S.m_head) > STEADY
    S.m_head = mod(S.m_head, NBUF) + 1;
    S.m_n = S.m_n - 1;
end
pmin = inf; pmax = -inf; cmin = inf; cmax = -inf;
for j = 0:S.m_n - 1
    q = mod(S.m_head - 1 + j, NBUF) + 1;
    pmin = min(pmin, S.m_p(q)); pmax = max(pmax, S.m_p(q));
    cmin = min(cmin, S.m_c(q)); cmax = max(cmax, S.m_c(q));
end
if ts - S.m_ts(S.m_head) >= 0.9 * STEADY && pmax - pmin <= STEADY_PT && cmax - cmin <= STEADY_CR
    S.ref_has = true; S.ref_p = p; S.ref_c = lagged;
elseif S.ref_has && abs(p - S.ref_p) >= MOVE
    S.scale = (lagged - S.ref_c) / (p - S.ref_p);
end
end
