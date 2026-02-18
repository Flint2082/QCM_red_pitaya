
from opcua import ua, Server
import time

server = Server()
# server.set_endpoint("opc.tcp://192.168.1.50:4840")
server.set_endpoint("opc.tcp://localhost:4840")

server.set_server_name("QCM OPCUA Server")

node_id_base = "ns=4;s=|var|750-8000 Basic Controller 100 2ETH ECO.Application.GVL_OPCUA"

uri = "urn:wago-client"
idx = server.register_namespace(uri)

objects = server.get_objects_node()
qcm = objects.add_object(
    ua.NodeId(node_id_base + ".QCM", idx),
    "QCM"
)

freq_M_node = qcm.add_variable(
    ua.NodeId(node_id_base + ".QCM.Frequency.M", idx),
    "Frequency M", 
    0.0
)
freq_M_node.set_writable()

freq_T_node = qcm.add_variable(
    ua.NodeId(node_id_base + ".QCM.Frequency.T", idx),
    "Frequency T", 
    0.0
)
freq_T_node.set_writable()

window_M_node = qcm.add_variable(
    ua.NodeId(node_id_base + ".QCM.Window.M", idx),
    "Window M", 
    0.0
)
window_M_node.set_writable()

window_T_node = qcm.add_variable(
    ua.NodeId(node_id_base + ".QCM.Window.T", idx),
    "Window T", 
    0.0
)
window_T_node.set_writable()

temp_node = qcm.add_variable(
    ua.NodeId(node_id_base + ".Temperature", idx),
    "Temperature", 
    0.0
)
temp_node.set_writable()

thickness_node = qcm.add_variable(
    ua.NodeId(node_id_base + ".in.Thickness", idx),
    "Thickness", 
    0.0
)
thickness_node.set_writable()

setRef = qcm.add_variable(
    ua.NodeId(node_id_base + ".QCM.SetRef", idx),
    "Set Reference", 
    False
)

    
server.start()
print("OPC UA server started")

window_M_node.set_value(5975000)
window_T_node.set_value(6565000)

setRef.set_value(True)  # Trigger reference setting in client

try:
    while True:
        freq_M = freq_M_node.get_value()
        freq_T = freq_T_node.get_value()
        temp = temp_node.get_value()
        thickness = thickness_node.get_value()
        print(f"Frequency M: {freq_M}, Frequency T: {freq_T}, Temperature: {temp}, Thickness: {thickness}")
        time.sleep(1)

finally:
    server.stop()
