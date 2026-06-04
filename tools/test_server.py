"""
Interactive OPC-UA test server that emulates the WAGO PLC node structure.

Start with:  python tools/test_server.py [host] [port]
  host  default: localhost
  port  default: 4840

Then use the IPython shell to read/write node values and trigger QCM commands.
"""

import sys
import time

from opcua import Server, ua

# -----------------------------------------------------------------------
# Server setup
# -----------------------------------------------------------------------

HOST = sys.argv[1] if len(sys.argv) > 1 else "localhost"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 4840

server = Server()
# Use the real host in the endpoint URL — clients use this address to connect.
# '0.0.0.0' would be advertised verbatim and is not a valid client destination.
server.set_endpoint(f"opc.tcp://{HOST}:{PORT}")
server.set_server_name("QCM OPCUA Test Server")

# Accept any username/password (mirrors the real PLC behaviour for local testing).
try:
    from opcua.server.user_manager import UserManager
    class _AcceptAll(UserManager):
        def check_user_token(self, isession, user_token):
            return True
    server.user_manager = _AcceptAll()
except Exception:
    pass  # older python-opcua versions handle this differently

# Register the namespace so WagoClient can resolve it by URL.
# Keep this URI in sync with plc/wago_client.py DEFAULT_NAMESPACE_URL.
NAMESPACE_URI = "urn:WAGO:UA:PlcRuntime"
idx = server.register_namespace(NAMESPACE_URI)

node_id_base = "|var|750-8000 Basic Controller 100 2ETH ECO.Application."

objects = server.get_objects_node()

# Root QCM object
qcm = objects.add_object(ua.NodeId(node_id_base + "GVL_QCM", idx), "GVL_QCM")

# -----------------------------------------------------------------------
# SET nodes  (PLC → QCM, writable by PLC or test shell)
# -----------------------------------------------------------------------
qcm_set = qcm.add_object(ua.NodeId(node_id_base + "GVL_QCM.SET", idx), "SET")

def _var(parent, key, default, vtype, writable=True):
    node = parent.add_variable(ua.NodeId(node_id_base + key, idx), key.split(".")[-1], default, varianttype=vtype)
    if writable:
        node.set_writable()
    return node

density_node          = _var(qcm_set, "GVL_QCM.SET.Density",       19320.0,    ua.VariantType.Float)
start_freq_mass_node  = _var(qcm_set, "GVL_QCM.SET.StartFreqMass", 5975000.0,  ua.VariantType.Float)
start_freq_temp_node  = _var(qcm_set, "GVL_QCM.SET.StartFreqTemp", 6571000.0,  ua.VariantType.Float)
ambient_temp_node     = _var(qcm_set, "GVL_QCM.SET.AmbientTemp",   22.0,       ua.VariantType.Float)
coeff_node            = _var(qcm_set, "GVL_QCM.SET.Coeffecients",  [0.0] * 8,  ua.VariantType.Float)

# -----------------------------------------------------------------------
# CTRL nodes  (PLC → QCM, writable by PLC or test shell)
# -----------------------------------------------------------------------
qcm_ctrl = qcm.add_object(ua.NodeId(node_id_base + "GVL_QCM.CTRL", idx), "CTRL")

start_meas_node = _var(qcm_ctrl, "GVL_QCM.CTRL.StartMeasurement", False, ua.VariantType.Boolean)
stop_meas_node  = _var(qcm_ctrl, "GVL_QCM.CTRL.StopMeasurement",  False, ua.VariantType.Boolean)
set_zero_node   = _var(qcm_ctrl, "GVL_QCM.CTRL.SetZero",          False, ua.VariantType.Boolean)
sweep_node      = _var(qcm_ctrl, "GVL_QCM.CTRL.Sweep",            False, ua.VariantType.Boolean)

# -----------------------------------------------------------------------
# READ nodes  (QCM → PLC, written by QCM system; read-only from PLC side)
# -----------------------------------------------------------------------
qcm_read = qcm.add_object(ua.NodeId(node_id_base + "GVL_QCM.READ", idx), "READ")

mass_freq_node        = _var(qcm_read, "GVL_QCM.READ.MassFrequency",           0.0, ua.VariantType.Float,  writable=False)
temp_freq_node        = _var(qcm_read, "GVL_QCM.READ.TempFrequency",           0.0, ua.VariantType.Float,  writable=False)
mass_amp_node         = _var(qcm_read, "GVL_QCM.READ.MassAmplitude",           0.0, ua.VariantType.Float,  writable=False)
temp_amp_node         = _var(qcm_read, "GVL_QCM.READ.TempAmplitude",           0.0, ua.VariantType.Float,  writable=False)
temperature_node      = _var(qcm_read, "GVL_QCM.READ.Temperature",             0.0, ua.VariantType.Float,  writable=False)
comp_thick_node       = _var(qcm_read, "GVL_QCM.READ.CompensatedThickness",    0.0, ua.VariantType.Float,  writable=False)
uncomp_thick_node     = _var(qcm_read, "GVL_QCM.READ.UncompensatedThickness",  0.0, ua.VariantType.Float,  writable=False)
comp_rate_node        = _var(qcm_read, "GVL_QCM.READ.CompensatedRate",         0.0, ua.VariantType.Float,  writable=False)
uncomp_rate_node      = _var(qcm_read, "GVL_QCM.READ.UncompensatedRate",       0.0, ua.VariantType.Float,  writable=False)
comp_mass_freq_node   = _var(qcm_read, "GVL_QCM.READ.CompensatedMassFrequency",0.0, ua.VariantType.Float,  writable=False)
timestamp_node        = _var(qcm_read, "GVL_QCM.READ.Timestamp",               0,   ua.VariantType.Int64,  writable=False)
error_code_node       = _var(qcm_read, "GVL_QCM.READ.ErrorCode",               "",  ua.VariantType.String, writable=False)

# -----------------------------------------------------------------------
# Helper functions available in the shell
# -----------------------------------------------------------------------

def read_all():
    """Print the current values of all READ nodes."""
    print(
        f"  Timestamp:        {timestamp_node.get_value()}\n"
        f"  MassFrequency:    {mass_freq_node.get_value():.3f} Hz\n"
        f"  TempFrequency:    {temp_freq_node.get_value():.3f} Hz\n"
        f"  MassAmplitude:    {mass_amp_node.get_value():.6f}\n"
        f"  TempAmplitude:    {temp_amp_node.get_value():.6f}\n"
        f"  Temperature:      {temperature_node.get_value():.2f} °C\n"
        f"  CompThickness:    {comp_thick_node.get_value():.4f} nm\n"
        f"  UncompThickness:  {uncomp_thick_node.get_value():.4f} nm\n"
        f"  CompRate:         {comp_rate_node.get_value():.4f} nm/s\n"
        f"  UncompRate:       {uncomp_rate_node.get_value():.4f} nm/s\n"
        f"  ErrorCode:        {error_code_node.get_value()!r}"
    )

def read_set():
    """Print the current values of all SET nodes."""
    print(
        f"  Density:          {density_node.get_value()} kg/m³\n"
        f"  StartFreqMass:    {start_freq_mass_node.get_value():.0f} Hz\n"
        f"  StartFreqTemp:    {start_freq_temp_node.get_value():.0f} Hz\n"
        f"  AmbientTemp:      {ambient_temp_node.get_value():.1f} °C"
    )

def start_measurement():
    """Send a rising edge on StartMeasurement."""
    start_meas_node.set_value(ua.DataValue(ua.Variant(True, ua.VariantType.Boolean)))
    time.sleep(0.1)
    start_meas_node.set_value(ua.DataValue(ua.Variant(False, ua.VariantType.Boolean)))
    print("[TEST] StartMeasurement pulse sent")

def stop_measurement():
    """Send a rising edge on StopMeasurement."""
    stop_meas_node.set_value(ua.DataValue(ua.Variant(True, ua.VariantType.Boolean)))
    time.sleep(0.1)
    stop_meas_node.set_value(ua.DataValue(ua.Variant(False, ua.VariantType.Boolean)))
    print("[TEST] StopMeasurement pulse sent")

def set_freqs(mass_hz: float, temp_hz: float):
    """Set PLL start frequencies (Hz)."""
    start_freq_mass_node.set_value(ua.DataValue(ua.Variant(float(mass_hz), ua.VariantType.Float)))
    start_freq_temp_node.set_value(ua.DataValue(ua.Variant(float(temp_hz), ua.VariantType.Float)))
    print(f"[TEST] StartFreqMass={mass_hz:.0f} Hz, StartFreqTemp={temp_hz:.0f} Hz")

def set_amb_temp(t: float):
    """Set ambient temperature (°C)."""
    ambient_temp_node.set_value(ua.DataValue(ua.Variant(float(t), ua.VariantType.Float)))
    print(f"[TEST] AmbientTemp={t:.1f} °C")

def watch(interval: float = 1.0, count: int = 10):
    """Print read_all() every `interval` seconds for `count` iterations."""
    for _ in range(count):
        read_all()
        print()
        time.sleep(interval)

# -----------------------------------------------------------------------
# Start server and drop into IPython
# -----------------------------------------------------------------------

BANNER = f"""
╔══════════════════════════════════════════════════════════╗
║          QCM OPC-UA Test Server — IPython Shell          ║
╚══════════════════════════════════════════════════════════╝
Namespace URI : {NAMESPACE_URI}
Namespace idx : {idx}
Endpoint      : opc.tcp://{HOST}:{PORT}

Node variables (all writable unless noted):
  SET  : density_node, start_freq_mass_node, start_freq_temp_node,
         ambient_temp_node, coeff_node
  CTRL : start_meas_node, stop_meas_node, set_zero_node, sweep_node
  READ : mass_freq_node, temp_freq_node, mass_amp_node, temp_amp_node,
         temperature_node, comp_thick_node, uncomp_thick_node,
         comp_rate_node, uncomp_rate_node, timestamp_node, error_code_node

Helper functions:
  read_all()               — print current READ values
  read_set()               — print current SET values
  start_measurement()      — pulse StartMeasurement (rising edge)
  stop_measurement()       — pulse StopMeasurement
  set_freqs(mass, temp)    — update start frequencies
  set_amb_temp(t)          — update ambient temperature
  watch(interval, count)   — poll read_all() N times

To set a node value directly:
  mass_freq_node.set_value(ua.DataValue(ua.Variant(5983000.0, ua.VariantType.Float)))
"""

server.start()
print(f"[TEST] OPC-UA server started at opc.tcp://{HOST}:{PORT} (ns={idx})")

try:
    import IPython
    ns = dict(
        server=server, ua=ua,
        # SET nodes
        density_node=density_node,
        start_freq_mass_node=start_freq_mass_node,
        start_freq_temp_node=start_freq_temp_node,
        ambient_temp_node=ambient_temp_node,
        coeff_node=coeff_node,
        # CTRL nodes
        start_meas_node=start_meas_node,
        stop_meas_node=stop_meas_node,
        set_zero_node=set_zero_node,
        sweep_node=sweep_node,
        # READ nodes
        mass_freq_node=mass_freq_node,
        temp_freq_node=temp_freq_node,
        mass_amp_node=mass_amp_node,
        temp_amp_node=temp_amp_node,
        temperature_node=temperature_node,
        comp_thick_node=comp_thick_node,
        uncomp_thick_node=uncomp_thick_node,
        comp_rate_node=comp_rate_node,
        uncomp_rate_node=uncomp_rate_node,
        comp_mass_freq_node=comp_mass_freq_node,
        timestamp_node=timestamp_node,
        error_code_node=error_code_node,
        # Helpers
        read_all=read_all,
        read_set=read_set,
        start_measurement=start_measurement,
        stop_measurement=stop_measurement,
        set_freqs=set_freqs,
        set_amb_temp=set_amb_temp,
        watch=watch,
    )
    print(BANNER)
    IPython.start_ipython(argv=["--no-banner"], user_ns=ns)
except ImportError:
    print("IPython not installed. Install with: pip install ipython")
    print("Falling back to simple print loop. Press Ctrl+C to stop.")
    while True:
        try:
            read_all()
            time.sleep(2)
        except KeyboardInterrupt:
            break
finally:
    server.stop()
    print("[TEST] Server stopped")
