function make_tec_holder(outdir)
%MAKE_TEC_HOLDER  Build the Simulink model of the calibration TEC holder.
%
%   make_tec_holder          writes, next to this file:
%     tec_holder_lib.slx     a library with the masked block "TEC holder"
%     tec_holder_demo.slx    the block in a PID loop on the PT100
%   make_tec_holder(dir)     writes them into dir instead.
%
%   The block is the thermal model of tools/tec_simulator.py (tools/README.md,
%   section 9): fitted on 14 Nov 2026 to an open-loop identification test on
%   the rp-f0ea58 calibration holder.
%
%   Inputs:  1  drive (%)     TEC drive, +100 = full heating, -100 = full cooling
%            2  enabled       1 = bridge enabled, 0 = bridge off (drive ignored)
%            3  room (degC)   room temperature
%   Outputs: 1  crystal (degC)
%            2  PT100 (degC)
%
%   Equations (all temperatures absolute, degC):
%     F(u)  = a*u - b*u^2  (u >= 0),   c*u + d*u^2  (u < 0)     the holding curve
%     dX1/dt = (Ta + F(u(t-theta))*(1 - kap*(X2 - Ta)) - X1)/t1   bridge enabled
%     dX1/dt = (Ta - X1)/t1_off                                   bridge off
%     dX2/dt = (X1 - X2)/t2                                       crystal = X2
%     dW/dt  = (X2 - W)/ta
%     dYp/dt = ((z/ta)*X2 + (1 - z/ta)*W - Yp)/tp                 PT100 = Yp
%   so the PT100 is the crystal through (z s + 1)/((ta s + 1)(tp s + 1)).
%   The block starts in equilibrium with the crystal at T0 in a room at Ta0
%   (the delay line holds the drive that keeps it there).

if nargin < 1
    outdir = fileparts(mfilename('fullpath'));
end
lib = 'tec_holder_lib';
demo = 'tec_holder_demo';
for m = {demo, lib}
    if bdIsLoaded(m{1}), close_system(m{1}, 0); end
    % the old file would shadow the new model of the same name
    old = fullfile(outdir, [m{1} '.slx']);
    if exist(old, 'file'), delete(old); end
end
load_system('simulink');

% ---------------------------------------------------------------- library
new_system(lib, 'Library');
blk = [lib '/TEC holder'];
add_block('built-in/Subsystem', blk, 'Position', [200 100 340 220]);
build_holder(blk);
mask_holder(blk);
set_param(lib, 'Lock', 'on');
save_system(lib, fullfile(outdir, [lib '.slx']));

% ---------------------------------------------------------------- demo
new_system(demo);
build_demo(demo, [lib '/TEC holder']);
save_system(demo, fullfile(outdir, [demo '.slx']));
close_system(demo, 0);
close_system(lib, 0);
fprintf('Wrote %s and %s in %s\n', [lib '.slx'], [demo '.slx'], outdir);
end


function build_holder(sys)
b = @(name, src, pos, varargin) add_block(src, [sys '/' name], 'Position', pos, varargin{:});
fcn = 'simulink/User-Defined Functions/Fcn';
mux = 'simulink/Signal Routing/Mux';
integ = 'simulink/Continuous/Integrator';

b('drive (%)', 'simulink/Sources/In1', [20 43 50 57], 'Port', '1');
b('enabled', 'simulink/Sources/In1', [20 163 50 177], 'Port', '2');
b('room (degC)', 'simulink/Sources/In1', [20 113 50 127], 'Port', '3');

b('limit', 'simulink/Discontinuities/Saturation', [90 35 120 65], 'UpperLimit', '100', 'LowerLimit', '-100');
b('delay', 'simulink/Continuous/Transport Delay', [150 35 190 65], 'DelayTime', 'theta', 'InitialOutput', 'u0');
b('holding curve F(u)', fcn, [230 35 450 65], ...
  'Expr', '(u(1)>=0)*(a*u(1)-b*u(1)^2)+(u(1)<0)*(c*u(1)+d*u(1)^2)');
b('as double', 'simulink/Signal Attributes/Data Type Conversion', [90 155 130 185], 'OutDataTypeStr', 'double');

% stage 1: the slow node, driven by the Peltier, relaxing to the room
b('mux1', mux, [490 20 495 200], 'Inputs', '5', 'DisplayOption', 'bar');
b('dX1dt', fcn, [530 95 850 125], ...
  'Expr', 'u(5)*(u(3)+u(1)*(1-kap*(u(2)-u(3)))-u(4))/t1+(1-u(5))*(u(3)-u(4))/t1_off');
b('X1', integ, [880 95 910 125], 'InitialCondition', 'T0');

% stage 2: the crystal
b('mux2', mux, [945 90 950 130], 'Inputs', '2', 'DisplayOption', 'bar');
b('dX2dt', fcn, [980 95 1060 125], 'Expr', '(u(1)-u(2))/t2');
b('X2 (crystal)', integ, [1090 95 1120 125], 'InitialCondition', 'T0');

% PT100 behind the crystal: lead-lag, then a lag
b('mux3', mux, [1170 175 1175 215], 'Inputs', '2', 'DisplayOption', 'bar');
b('dWdt', fcn, [1205 180 1285 210], 'Expr', '(u(1)-u(2))/ta');
b('W', integ, [1315 180 1345 210], 'InitialCondition', 'T0');
b('mux4', mux, [1385 245 1390 305], 'Inputs', '3', 'DisplayOption', 'bar');
b('dYpdt', fcn, [1420 260 1560 290], 'Expr', '(kz*u(1)+(1-kz)*u(2)-u(3))/tp');
b('Yp (PT100)', integ, [1590 260 1620 290], 'InitialCondition', 'T0');

b('crystal (degC)', 'simulink/Sinks/Out1', [1680 103 1710 117], 'Port', '1');
b('PT100 (degC)', 'simulink/Sinks/Out1', [1680 268 1710 282], 'Port', '2');

L = @(a, z) add_line(sys, a, z, 'autorouting', 'smart');
L('drive (%)/1', 'limit/1');
L('limit/1', 'delay/1');
L('delay/1', 'holding curve F(u)/1');
L('holding curve F(u)/1', 'mux1/1');
L('X2 (crystal)/1', 'mux1/2');
L('room (degC)/1', 'mux1/3');
L('X1/1', 'mux1/4');
L('enabled/1', 'as double/1');
L('as double/1', 'mux1/5');
L('mux1/1', 'dX1dt/1');
L('dX1dt/1', 'X1/1');
L('X1/1', 'mux2/1');
L('X2 (crystal)/1', 'mux2/2');
L('mux2/1', 'dX2dt/1');
L('dX2dt/1', 'X2 (crystal)/1');
L('X2 (crystal)/1', 'mux3/1');
L('W/1', 'mux3/2');
L('mux3/1', 'dWdt/1');
L('dWdt/1', 'W/1');
L('X2 (crystal)/1', 'mux4/1');
L('W/1', 'mux4/2');
L('Yp (PT100)/1', 'mux4/3');
L('mux4/1', 'dYpdt/1');
L('dYpdt/1', 'Yp (PT100)/1');
L('X2 (crystal)/1', 'crystal (degC)/1');
L('Yp (PT100)/1', 'PT100 (degC)/1');
end


function mask_holder(blk)
m = Simulink.Mask.create(blk);
m.Type = 'TEC holder (rp-f0ea58)';
m.Description = [ ...
    'Thermal model of the QCM calibration holder on its Peltier, fitted on 14 Nov 2026 ' ...
    'to an open-loop PRBS test on rp-f0ea58 (tools/README.md, section 9).' newline newline ...
    'Inputs: TEC drive in % (+ heats, limited to +-100), bridge enabled (0/1), room ' ...
    'temperature (degC). Outputs: crystal and PT100 temperature (degC).' newline newline ...
    'Small signal around 40 degC: crystal = 0.47 e^(-2.3 s)/((320 s + 1)(12.2 s + 1)) ' ...
    'per % drive; PT100 = crystal (64.5 s + 1)/((80.1 s + 1)(6.7 s + 1)).'];
p = { ...
    'a',      'Heating map a (degC per %)',             '0.495'
    'b',      'Heating map b (degC per %^2)',           '0.00067'
    'c',      'Cooling map c (degC per %)',             '0.261'
    'd',      'Cooling map d (degC per %^2)',           '0.00044'
    'kap',    'Temperature term kap (1/degC)',          '-0.0014'
    't1',     'Slow pole, bridge enabled t1 (s)',       '312'
    't1_off', 'Slow pole, bridge off t1_off (s)',       '411'
    't2',     'Crystal lag t2 (s)',                     '12.2'
    'theta',  'Delay theta (s)',                        '2.3'
    'z',      'PT100 lead z (s)',                       '64.5'
    'ta',     'PT100 lag ta (s)',                       '80.1'
    'tp',     'PT100 sensor lag tp (s)',                '6.7'
    'T0',     'Initial crystal temperature T0 (degC)',  '22.2'
    'Ta0',    'Room at the start Ta0 (degC)',           '22.2'};
for i = 1:size(p, 1)
    m.addParameter('Type', 'edit', 'Name', p{i, 1}, 'Prompt', p{i, 2}, 'Value', p{i, 3}, 'Evaluate', 'on');
end
m.Initialization = strjoin({ ...
    '% Drive that holds the crystal at T0 in a room at Ta0: the delay line starts with it.'
    'dT = T0 - Ta0;'
    'w = dT/(1 - kap*dT);'
    'if w >= 0'
    '    if b == 0, u0 = w/a; else, u0 = (a - sqrt(max(a^2 - 4*b*w, 0)))/(2*b); end'
    'else'
    '    if d == 0, u0 = w/c; else, u0 = (-c + sqrt(max(c^2 + 4*d*w, 0)))/(2*d); end'
    'end'
    'kz = z/ta;'}, newline);
m.Display = strjoin({ ...
    'disp(''TEC holder\nrp-f0ea58'');'
    'port_label(''input'', 1, ''drive (%)'');'
    'port_label(''input'', 2, ''enabled'');'
    'port_label(''input'', 3, ''room'');'
    'port_label(''output'', 1, ''crystal'');'
    'port_label(''output'', 2, ''PT100'');'}, newline);
m.IconOpaque = 'opaque';
end


function build_demo(sys, libblk)
b = @(name, src, pos, varargin) add_block(src, [sys '/' name], 'Position', pos, varargin{:});
set_param(sys, 'StopTime', '9000', 'Solver', 'ode45', 'MaxStep', '0.5', 'RelTol', '1e-6');
set_param(sys, 'InitFcn', strjoin({ ...
    '% Setpoint schedule [t (s), setpoint (degC)]; define tec_setpoints in the base workspace to change it.'
    'if ~exist(''tec_setpoints'', ''var''), tec_setpoints = [0 25; 1800 45; 3600 25; 5400 60; 7200 40]; end'}, newline));
set_param(sys, 'Description', [ ...
    'The TEC holder block in a PID loop on the PT100, like the TEC worker with source pt100: ' ...
    'parallel PID with the derivative on the measurement (c = 0) through a 5 s filter (N = 0.2), ' ...
    'clamping anti-windup, output limited to +-100 %, stepping every 0.5 s on a PT100 read once a ' ...
    'second. The worker also holds its integral outside i_band (1 degC) of the setpoint, which ' ...
    'this PID block does not do, and the PT100 + crystal (fused) loop is not modelled here: use ' ...
    'tools/tec_simulator.py for those.']);

b('setpoint', 'simulink/Sources/From Workspace', [30 95 130 125], 'VariableName', 'tec_setpoints', ...
  'Interpolate', 'off', 'OutputAfterFinalValue', 'Holding final value');
b('PID (PT100 gains)', 'simulink/Continuous/PID Controller (2DOF)', [200 90 300 150], ...
  'Form', 'Parallel', 'TimeDomain', 'Discrete-time', 'SampleTime', '0.5', ...
  'P', '39.9', 'I', '0.196', 'D', '585', 'N', '0.2', 'b', '1', 'c', '0', ...
  'LimitOutput', 'on', 'UpperSaturationLimit', '100', 'LowerSaturationLimit', '-100', ...
  'AntiWindupMode', 'clamping');
b('enabled', 'simulink/Sources/Constant', [340 175 370 195], 'Value', '1');
b('room', 'simulink/Sources/Constant', [340 215 370 235], 'Value', '22.2');
add_block(libblk, [sys '/TEC holder'], 'Position', [420 100 560 240]);
b('PT100 read (1 s)', 'simulink/Discrete/Zero-Order Hold', [620 215 660 245], 'SampleTime', '1');
b('mux', 'simulink/Signal Routing/Mux', [700 60 705 160], 'Inputs', '3', 'DisplayOption', 'bar');
b('temperatures', 'simulink/Sinks/Scope', [760 90 800 130]);
b('drive', 'simulink/Sinks/Scope', [420 20 460 50]);
b('log', 'simulink/Sinks/To Workspace', [760 230 840 260], 'VariableName', 'tec_log', 'SaveFormat', 'Timeseries');
b('mux log', 'simulink/Signal Routing/Mux', [700 205 705 285], 'Inputs', '4', 'DisplayOption', 'bar');

L = @(a, z) add_line(sys, a, z, 'autorouting', 'smart');
L('setpoint/1', 'PID (PT100 gains)/1');
L('PT100 read (1 s)/1', 'PID (PT100 gains)/2');
L('PID (PT100 gains)/1', 'TEC holder/1');
L('enabled/1', 'TEC holder/2');
L('room/1', 'TEC holder/3');
L('TEC holder/2', 'PT100 read (1 s)/1');
L('setpoint/1', 'mux/1');
L('TEC holder/1', 'mux/2');
L('TEC holder/2', 'mux/3');
L('mux/1', 'temperatures/1');
L('PID (PT100 gains)/1', 'drive/1');
L('setpoint/1', 'mux log/1');
L('TEC holder/1', 'mux log/2');
L('TEC holder/2', 'mux log/3');
L('PID (PT100 gains)/1', 'mux log/4');
L('mux log/1', 'log/1');
end
