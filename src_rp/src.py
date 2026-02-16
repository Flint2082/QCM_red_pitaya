from opcua import Client
import time

import wago_client
import QCM_interface 

# Server endpoint (must match server)
url = "opc.tcp://132.229.46.113:4840"

# Connect to server
client = Client(url)
client.connect()
print("Connected to OPC UA server")

qcm = QCM_interface.QCMInterface()
qcm.startup()

wago = wago_client.WagoClient(url = url)

# node_id = "ns=4;s=|var|750-8000 Basic Controller 100 2ETH ECO.Application.GVL_OPCUA.in."


try:
    # Resolve namespace index dynamically
    # idx = client.get_namespace_index(client.application_uri)
    idx = 4    
    

    # Get the Frequency node
    freq_M_node = wago.get_node(f"ns={idx};s=QCM.Frequency.M")
    freq_T_node = wago.get_node(f"ns={idx};s=QCM.Frequency.T")
    

    # Read values in a loop
    while True: 
        freq_M = qcm.getFreq(1)
        freq_T = qcm.getFreq(2)
        
        
        wago.write_node(freq_M_node, freq_M)
        wago.write_node(freq_T_node, freq_T)
        
        
        print(f"Frequency M: {freq_M}, Frequency T: {freq_T}")
                
        
        
        time.sleep(1)

finally:
    client.disconnect()
    print("Disconnected")





