

from opcua import Client
import time

# Server endpoint (must match server)
url = "opc.tcp://132.229.46.113:4840"

# Connect to server
client = Client(url)
client.connect()
print("Connected to OPC UA server")

try:
    # Resolve namespace index dynamically
    uri = "urn:qcm:redpitaya"
    idx = client.get_namespace_index(uri)

    # Get the Frequency node
    freq_node = client.get_node(f"ns={idx};s=QCM.Frequency")

    # Read values in a loop
    while True:
        freq = freq_node.get_value()
        print(f"Frequency: {freq}")
        time.sleep(1)

finally:
    client.disconnect()
    print("Disconnected")




