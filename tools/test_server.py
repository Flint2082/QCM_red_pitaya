
from opcua import ua, Server
import time

server = Server()
server.set_endpoint("opc.tcp://132.229.46.113:4840")
# server.set_endpoint("opc.tcp://localhost:4840")

server.set_server_name("QCM OPCUA Server")

node_id_base = "|var|750-8000 Basic Controller 100 2ETH ECO.Application.GVL_OPCUA."

uri = "urn:wago-client"
idx = server.register_namespace(uri)

objects = server.get_objects_node()

# Create main QCM object
qcm = objects.add_object(
    ua.NodeId(node_id_base + "QCM", idx),
    "QCM"
)

# ===== QCM.SET - Settings folder =====
qcm_set = qcm.add_object(
    ua.NodeId(node_id_base + "QCM.SET", idx),
    "SET"
)

density_node = qcm_set.add_variable(
    ua.NodeId(node_id_base + "QCM.SET.Density", idx),
    "Density",
    0.0,
    varianttype=ua.VariantType.Float
)
density_node.set_writable()

z_ratio_node = qcm_set.add_variable(
    ua.NodeId(node_id_base + "QCM.SET.Z-ratio", idx),
    "Z-ratio",
    0.0,
    varianttype=ua.VariantType.Float
)
z_ratio_node.set_writable()

start_freq_node = qcm_set.add_variable(
    ua.NodeId(node_id_base + "QCM.SET.StartFreq", idx),
    "StartFreq",
    0.0,
    varianttype=ua.VariantType.Float
)
start_freq_node.set_writable()

ambient_temp_node = qcm_set.add_variable(
    ua.NodeId(node_id_base + "QCM.SET.AmbientTemp", idx),
    "AmbientTemp",
    0.0,
    varianttype=ua.VariantType.Float
)
ambient_temp_node.set_writable()

coeff_node = qcm_set.add_variable(
    ua.NodeId(node_id_base + "QCM.SET.Coeffecients", idx),
    "Coeffecients",
    [0.0] * 8,
    varianttype=ua.VariantType.Float
)
coeff_node.set_writable()

# ===== QCM.CTRL - Control folder =====
qcm_ctrl = qcm.add_object(
    ua.NodeId(node_id_base + "QCM.CTRL", idx),
    "CTRL"
)

start_meas_node = qcm_ctrl.add_variable(
    ua.NodeId(node_id_base + "QCM.CTRL.StartMeasurement", idx),
    "StartMeasurement",
    False,
    varianttype=ua.VariantType.Boolean
)
start_meas_node.set_writable()

stop_meas_node = qcm_ctrl.add_variable(
    ua.NodeId(node_id_base + "QCM.CTRL.StopMeasurement", idx),
    "StopMeasurement",
    False,
    varianttype=ua.VariantType.Boolean
)
stop_meas_node.set_writable()

set_zero_node = qcm_ctrl.add_variable(
    ua.NodeId(node_id_base + "QCM.CTRL.SetZero", idx),
    "SetZero",
    False,
    varianttype=ua.VariantType.Boolean
)
set_zero_node.set_writable()

sweep_node = qcm_ctrl.add_variable(
    ua.NodeId(node_id_base + "QCM.CTRL.Sweep", idx),
    "Sweep",
    False,
    varianttype=ua.VariantType.Boolean
)
sweep_node.set_writable()

# ===== QCM.READ - Reading folder =====
qcm_read = qcm.add_object(
    ua.NodeId(node_id_base + "QCM.READ", idx),
    "READ"
)

mass_freq_node = qcm_read.add_variable(
    ua.NodeId(node_id_base + "QCM.READ.MassFrequency", idx),
    "MassFrequency",
    0.0,
    varianttype=ua.VariantType.Float
)

temp_freq_node = qcm_read.add_variable(
    ua.NodeId(node_id_base + "QCM.READ.TempFrequency", idx),
    "TempFrequency",
    0.0,
    varianttype=ua.VariantType.Float
)

temperature_node = qcm_read.add_variable(
    ua.NodeId(node_id_base + "QCM.READ.Temperature", idx),
    "Temperature",
    0.0,
    varianttype=ua.VariantType.Float
)

comp_thickness_node = qcm_read.add_variable(
    ua.NodeId(node_id_base + "QCM.READ.CompensatedThickness", idx),
    "CompensatedThickness",
    0.0,
    varianttype=ua.VariantType.Float
)

uncomp_thickness_node = qcm_read.add_variable(
    ua.NodeId(node_id_base + "QCM.READ.UncompensatedThickness", idx),
    "UncompensatedThickness",
    0.0,
    varianttype=ua.VariantType.Float
)

comp_rate_node = qcm_read.add_variable(
    ua.NodeId(node_id_base + "QCM.READ.CompensatedRate", idx),
    "CompensatedRate",
    0.0,
    varianttype=ua.VariantType.Float
)

uncomp_rate_node = qcm_read.add_variable(
    ua.NodeId(node_id_base + "QCM.READ.UncompensatedRate", idx),
    "UncompensatedRate",
    0.0,
    varianttype=ua.VariantType.Float
)

comp_mass_freq_node = qcm_read.add_variable(
    ua.NodeId(node_id_base + "QCM.READ.CompensatedMassFrequency", idx),
    "CompensatedMassFrequency",
    0.0,
    varianttype=ua.VariantType.Float
)

timestamp_node = qcm_read.add_variable(
    ua.NodeId(node_id_base + "QCM.READ.Timestamp", idx),
    "Timestamp",
    0,
    varianttype=ua.VariantType.Int64
)

error_code_node = qcm_read.add_variable(
    ua.NodeId(node_id_base + "QCM.READ.ErrorCode", idx),
    "ErrorCode",
    0,
    varianttype=ua.VariantType.Int64
)

server.start()
print("OPC UA server started on opc.tcp://132.229.46.113:4840")

try:
    while True:
        try:
            print(
                f"Time: {timestamp_node.get_value()}\n"
                f"  Freq_M: {mass_freq_node.get_value()}\n"
                f"  Freq_T: {temp_freq_node.get_value()}\n"
                f"  Temp: {temperature_node.get_value()}\n"
                f"  CompThick: {comp_thickness_node.get_value()}\n"
                f"  UncompThick: {uncomp_thickness_node.get_value()}\n"
                f"  CompRate: {comp_rate_node.get_value()}\n"
                f"  UncompRate: {uncomp_rate_node.get_value()}\n"
                f"  CompMassFreq: {comp_mass_freq_node.get_value()}\n"
                f"  ErrorCode: {error_code_node.get_value()}"
            )  
            time.sleep(1)
        except:
            print("\nServer shutting down...")
            break
        
        

finally:
    server.stop()
