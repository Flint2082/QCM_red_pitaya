from opcua import ua, Server
import time

server = Server()
server.set_endpoint("opc.tcp://0.0.0.0:4840")

server.set_server_name("QCM OPCUA Server")

uri = "urn:wago-server"
idx = server.register_namespace(uri)

objects = server.get_objects_node()
qcm = objects.add_object(
    ua.NodeId("QCM", idx),
    "QCM"
)

freq_M_node = qcm.add_variable(
    ua.NodeId("QCM.Frequency.M", idx),
    "Frequency M", 
    0.0
)
freq_M_node.set_writable()

freq_T_node = qcm.add_variable(
    ua.NodeId("QCM.Frequency.T", idx),
    "Frequency T", 
    0.0
)
freq_T_node.set_writable()

temp_node = qcm.add_variable(
    ua.NodeId("QCM.Temperature", idx),
    "Temperature", 
    0.0
)
temp_node.set_writable()

thickness_node = qcm.add_variable(
    ua.NodeId("QCM.Thickness", idx),
    "Thickness", 
    0.0
)
thickness_node.set_writable()

    
server.start()
print("OPC UA server started")

try:
    while True:
        freq_M = freq_M_node.get_value()
        freq_T = freq_T_node.get_value()
        print(f"Frequency M: {freq_M}, Frequency T: {freq_T}")
        time.sleep(1)

finally:
    server.stop()
