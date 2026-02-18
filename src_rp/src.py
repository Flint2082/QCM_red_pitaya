from opcua import Client
import time

import wago_client
import QCM_interface 

mat_density = 2.7  # g/cm^3 



# Server endpoint (must match server)
# url = "opc.tcp://132.229.46.113:4840"
#url = "opc.tcp://192.168.1.50:4840" 


# Connect to server
#client = Client(url)
#client.connect()
#print("Connected to OPC UA server")

qcm = QCM_interface.QCMInterface()
qcm.startup()

qcm.setReference()

wago = wago_client.WagoClient()

node_id = "ns=4;s=|var|750-8000 Basic Controller 100 2ETH ECO.Application.GVL_OPCUA.in.thickness"


try:
    # Resolve namespace index dynamically
    uri = "urn:wago-client"
    #idx = client.get_namespace_index(uri)
    idx = 2    

    #setRef = wago.get_node(f"ns={idx};s=QCM.SetRef")
    
    # get frequency window nodes
    #window_M_node = wago.get_node(f"ns={idx};s=QCM.Window.M")
    #window_T_node = wago.get_node(f"ns={idx};s=QCM.Window.T")

    # Get the Frequency node
    #freq_M_node = wago.get_node(f"ns={idx};s=QCM.Frequency.M")
    #freq_T_node = wago.get_node(f"ns={idx};s=QCM.Frequency.T")
    
    #temp_node = wago.get_node(f"ns={idx};s=QCM.Temperature")
    thickness_node = wago.get_node(node_id)
    

    # Read values in a loop
    while True: 
        #if setRef.get_value():
        #    qcm.setReference()
        #    setRef.set_value(False)  # Reset trigger
        
        #window_M = qcm.setFreq(1, wago.read_node(window_M_node))
        #window_T = qcm.setFreq(2, wago.read_node(window_T_node))
        #freq_M = qcm.getFreq(1)
        #freq_T = qcm.getFreq(2)
        
        thickness = qcm.getThicknessUncomp()
        
        
        #wago.write_node(freq_M_node, freq_M)
        #wago.write_node(freq_T_node, freq_T)
        wago.write_node(thickness_node, thickness)
        
        print(thickness)
        #print(f"Frequency M: {freq_M:.4f},\t Frequency T: {freq_T:.4f},\t Thickness: {thickness:.4f} nm")
                
        
        
        time.sleep(1)

finally:
    wago.disconnect()
    print("Disconnected")





