function make_tec_controller(outdir)
%MAKE_TEC_CONTROLLER  Build the Simulink model of the Pitaya's TEC controller.
%
%   make_tec_controller          writes, next to this file:
%     tec_controller_lib.slx     a library with the masked block "TEC controller"
%     tec_controller_demo.slx    the block driving the "TEC holder" (tec_holder_lib)
%   make_tec_controller(dir)     writes them into dir instead.
%
%   The block runs the TEC loop of the Pitaya (src/domain/tec_controller.py and
%   src/workers/tec_worker.py) tick by tick: tec_controller_block.m, which this
%   script copies into a MATLAB Function block, says what is ported. It matches
%   tools/tec_simulator.py, which runs the Python code itself. A plain PID
%   Controller block does not: it has no integral band, and two of them each
%   limited to +-100 % and then added are not one PID limited once.
%
%   Inputs:  1  setpoint (degC)
%            2  PT100 (degC), read inside once every pt100_interval_s
%            3  crystal reading (degC), NaN while unlocked
%   Outputs: 1  drive (%), + heats
%            2  enabled: 1 while running, 0 after a limit trip (to the holder)
%            3  status: [derivative on the crystal; crystal/PT100 move ratio;
%                        PT100 (degC)]
%
%   Sources, as on the Pitaya: 1 PT100 (P, I and D on the PT100), 2 PT100 +
%   crystal for D (P and I on the PT100, D on the crystal while it is trusted,
%   a quarter of Kd on the PT100 otherwise).
%
%   Leaves tec_holder_lib.slx and tec_holder_demo.slx alone (make_tec_holder.m).

here = fileparts(mfilename('fullpath'));
if nargin < 1
    outdir = here;
end
lib = 'tec_controller_lib';
demo = 'tec_controller_demo';
for m = {demo, lib}
    if bdIsLoaded(m{1}), close_system(m{1}, 0); end
    % the old file would shadow the new model of the same name
    old = fullfile(outdir, [m{1} '.slx']);
    if exist(old, 'file'), delete(old); end
end
addpath(here);
load_system('simulink');

% ---------------------------------------------------------------- library
new_system(lib, 'Library');
blk = [lib '/TEC controller'];
add_block('built-in/Subsystem', blk, 'Position', [200 100 360 220]);
build_controller(blk, fileread(fullfile(here, 'tec_controller_block.m')));
mask_controller(blk);
set_param(lib, 'Lock', 'on');
save_system(lib, fullfile(outdir, [lib '.slx']));

% ---------------------------------------------------------------- demo
load_system('tec_holder_lib');
new_system(demo);
build_demo(demo, [lib '/TEC controller'], 'tec_holder_lib/TEC holder');
save_system(demo, fullfile(outdir, [demo '.slx']));
close_system(demo, 0);
close_system(lib, 0);
fprintf('Wrote %s and %s in %s\n', [lib '.slx'], [demo '.slx'], outdir);
end


function build_controller(sys, script)
b = @(name, src, pos, varargin) add_block(src, [sys '/' name], 'Position', pos, varargin{:});
b('setpoint (degC)', 'simulink/Sources/In1', [20 43 50 57], 'Port', '1');
b('PT100 (degC)', 'simulink/Sources/In1', [20 93 50 107], 'Port', '2');
b('crystal (degC)', 'simulink/Sources/In1', [20 143 50 157], 'Port', '3');
fn = [sys '/controller'];
b('controller', 'simulink/User-Defined Functions/MATLAB Function', [140 30 330 170]);
b('drive (%)', 'simulink/Sinks/Out1', [420 43 450 57], 'Port', '1');
b('enabled', 'simulink/Sinks/Out1', [420 93 450 107], 'Port', '2');
b('status', 'simulink/Sinks/Out1', [420 143 450 157], 'Port', '3');

ch = find(sfroot, '-isa', 'Stateflow.EMChart', 'Path', fn);
ch.Script = script;
ch.ChartUpdate = 'DISCRETE';
ch.SampleTime = '0.1';                       % TECWorker.TICK_S
prm = find(ch, '-isa', 'Stateflow.Data', 'Name', 'ctl_params');
prm.Scope = 'Parameter';
prm.Tunable = false;

L = @(a, z) add_line(sys, a, z, 'autorouting', 'smart');
L('setpoint (degC)/1', 'controller/1');
L('PT100 (degC)/1', 'controller/2');
L('crystal (degC)/1', 'controller/3');
L('controller/1', 'drive (%)/1');
L('controller/2', 'enabled/1');
L('controller/3', 'status/1');
end


function mask_controller(blk)
m = Simulink.Mask.create(blk);
m.Type = 'TEC controller (Pitaya)';
m.Description = [ ...
    'The TEC loop of the QCM Pitaya, ported tick by tick from src/domain/tec_controller.py and ' ...
    'src/workers/tec_worker.py (tools/simulink/tec_controller_block.m says what is in it). ' ...
    'Steps on each fresh reading at least 0.5 s apart: every 1 s on the PT100, every 0.5 s ' ...
    'with the derivative on the crystal.' newline newline ...
    'P and I act on the PT100. Kd acts on the PT100, or on the crystal with "PT100 + crystal ' ...
    'for D" (about 600 on the PT100, 2000 on the crystal; the crystal''s slope multiplies it).'];
m.addParameter('Type', 'popup', 'Name', 'source', 'Prompt', 'Feedback source', ...
    'TypeOptions', {'PT100', 'PT100 + crystal for D'}, ...
    'Value', 'PT100 + crystal for D', 'Evaluate', 'on');
p = { ...
    'kp',          'Kp (%/degC), on the PT100',                     '39.9'
    'ki',          'Ki (%/(degC s)), on the PT100',                 '0.196'
    'kd',          'Kd (% s/degC), on the PT100 or the crystal',    '2000'
    'i_band',      'Integral band i_band (degC, 0 = always)',       '1'
    'max_output',  'Maximum drive (%)',                             '100'
    't_min',       'Safety limit t_min (degC)',                     '5'
    't_max',       'Safety limit t_max (degC)',                     '75'
    'pt100_s',     'PT100 read interval (s)',                       '1'
    'i0',          'Integral at the start (%, e.g. the holding drive)', '0'};
for i = 1:size(p, 1)
    m.addParameter('Type', 'edit', 'Name', p{i, 1}, 'Prompt', p{i, 2}, 'Value', p{i, 3}, 'Evaluate', 'on');
end
m.Initialization = 'ctl_params = [source kp ki kd i_band max_output t_min t_max pt100_s i0];';
m.Display = strjoin({ ...
    'disp(''TEC controller\nPitaya'');'
    'port_label(''input'', 1, ''setpoint'');'
    'port_label(''input'', 2, ''PT100'');'
    'port_label(''input'', 3, ''crystal'');'
    'port_label(''output'', 1, ''drive (%)'');'
    'port_label(''output'', 2, ''enabled'');'
    'port_label(''output'', 3, ''status'');'}, newline);
m.IconOpaque = 'opaque';
end


function build_demo(sys, ctlblk, holderblk)
b = @(name, src, pos, varargin) add_block(src, [sys '/' name], 'Position', pos, varargin{:});
set_param(sys, 'StopTime', '23400', 'Solver', 'ode45', 'MaxStep', '0.5', 'RelTol', '1e-6');
set_param(sys, 'InitFcn', strjoin({ ...
    '% Define any of these in the base workspace to change them.'
    '% Setpoints [t (s), degC]: the standard 15-65 degC calibration sequence, 30 min a step.'
    'if ~exist(''tec_setpoints'', ''var''), tec_setpoints = [(0:12)''*1800, [25 15 25 35 45 55 65 60 50 40 30 20 25]'']; end'
    '% The crystal reading the loop gets: crystal_scale*(T - 25) + 25 + crystal_offset.'
    '% A profile from another crystal reads changes crystal_scale times too large,'
    '% which multiplies Kd; the offset does nothing.'
    'if ~exist(''crystal_scale'', ''var''), crystal_scale = 1; end'
    'if ~exist(''crystal_offset'', ''var''), crystal_offset = 0; end'
    '% The PT100 reading noise (degC rms, the LTC2986 on rp-f0ea58 shows ~0.013).'
    'if ~exist(''pt100_noise'', ''var''), pt100_noise = 0.013; end'
    '% The drive that holds the first setpoint (the holding curve of the TEC holder block),'
    '% so the loop starts settled.'
    'tec_T0 = tec_setpoints(1, 2);'
    'tec_w = (tec_T0 - 22.2)/(1 + 0.0014*(tec_T0 - 22.2));'
    'if tec_w >= 0, tec_u0 = (0.495 - sqrt(0.495^2 - 4*0.00067*tec_w))/(2*0.00067);'
    'else, tec_u0 = (-0.261 + sqrt(0.261^2 + 4*0.00044*tec_w))/(2*0.00044); end'}, newline));
set_param(sys, 'Description', [ ...
    'The TEC controller block (the Pitaya''s TEC loop, tec_controller_block.m) driving the TEC ' ...
    'holder block. Choose the source and the gains in the controller''s mask; crystal_scale, ' ...
    'crystal_offset, pt100_noise and tec_setpoints in the base workspace (see the InitFcn).']);

b('setpoint', 'simulink/Sources/From Workspace', [30 95 130 125], 'VariableName', 'tec_setpoints', ...
  'Interpolate', 'off', 'OutputAfterFinalValue', 'Holding final value');
add_block(ctlblk, [sys '/TEC controller'], 'Position', [200 80 340 200], 'i0', 'tec_u0');
b('room', 'simulink/Sources/Constant', [380 205 410 225], 'Value', '22.2');
add_block(holderblk, [sys '/TEC holder'], 'Position', [450 90 590 230], 'T0', 'tec_T0', 'Ta0', '22.2');
b('PT100 noise', 'simulink/Sources/Random Number', [600 300 630 330], 'Mean', '0', ...
  'Variance', 'pt100_noise^2', 'Seed', '1', 'SampleTime', '1');
b('PT100 read', 'simulink/Math Operations/Sum', [670 255 690 275], 'Inputs', '++');
b('crystal reading', 'simulink/User-Defined Functions/Fcn', [650 340 830 370], ...
  'Expr', 'crystal_scale*(u(1)-25)+25+crystal_offset');
b('status', 'simulink/Signal Routing/Demux', [380 300 385 360], 'Outputs', '3', 'DisplayOption', 'bar');
b('mux', 'simulink/Signal Routing/Mux', [720 40 725 140], 'Inputs', '3', 'DisplayOption', 'bar');
b('temperatures', 'simulink/Sinks/Scope', [770 70 810 110]);
b('drive', 'simulink/Sinks/Scope', [450 20 490 50]);
b('mux log', 'simulink/Signal Routing/Mux', [900 160 905 300], 'Inputs', '6', 'DisplayOption', 'bar');
b('log', 'simulink/Sinks/To Workspace', [950 215 1030 245], 'VariableName', 'tec_log', 'SaveFormat', 'Timeseries');
b('terminator', 'simulink/Sinks/Terminator', [420 345 440 365]);

L = @(a, z) add_line(sys, a, z, 'autorouting', 'smart');
L('setpoint/1', 'TEC controller/1');
L('PT100 read/1', 'TEC controller/2');
L('crystal reading/1', 'TEC controller/3');
L('TEC controller/1', 'TEC holder/1');
L('TEC controller/2', 'TEC holder/2');
L('room/1', 'TEC holder/3');
L('TEC holder/2', 'PT100 read/1');
L('PT100 noise/1', 'PT100 read/2');
L('TEC holder/1', 'crystal reading/1');
L('TEC controller/3', 'status/1');
L('setpoint/1', 'mux/1');
L('TEC holder/1', 'mux/2');
L('TEC holder/2', 'mux/3');
L('mux/1', 'temperatures/1');
L('TEC controller/1', 'drive/1');
L('setpoint/1', 'mux log/1');
L('TEC holder/1', 'mux log/2');
L('TEC holder/2', 'mux log/3');
L('TEC controller/1', 'mux log/4');
L('status/1', 'mux log/5');
L('status/2', 'mux log/6');
L('status/3', 'terminator/1');
L('mux log/1', 'log/1');
end
