from opcua import ua, Server
import time

server = Server()
server.set_endpoint("opc.tcp://0.0.0.0:4840")

server.set_server_name("QCM OPCUA Server")

uri = "urn:qcm:redpitaya"
idx = server.register_namespace(uri)

objects = server.get_objects_node()
qcm = objects.add_object(
    ua.NodeId("QCM", idx),
    "QCM"
)

freq_node = qcm.add_variable(
    ua.NodeId("QCM.Frequency", idx),
    "Frequency", 
    0.0
)
freq_node.set_writable()

server.start()
print("OPC UA server started")

try:
    while True:
        freq = freq_node.get_value()
        print(f"Frequency: {freq}")
        time.sleep(1)

finally:
    server.stop()
