from opcua import Client
import time

import QCM_interface 

# Server endpoint (must match server)
url = "opc.tcp://132.229.46.113:4840"

# Connect to server
client = Client(url)
client.connect()
print("Connected to OPC UA server")

qcm = QCM_interface.QCMInterface()

try:
    # Resolve namespace index dynamically
    uri = "urn:qcm:redpitaya"
    idx = client.get_namespace_index(uri)

    # Get the Frequency node
    freq_node = client.get_node(f"ns={idx};s=QCM.Frequency")

    # Read values in a loop
    while True:
        cur_freq = qcm.getFreq(1)
        print(f"Current Frequency: {cur_freq}")
        freq_node.set_value(cur_freq)
        time.sleep(1)

finally:
    client.disconnect()
    print("Disconnected")





