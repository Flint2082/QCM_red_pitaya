print("Starting source code")
print("Loading OPC-UA package")
from opcua import Client

import time

print("Loading WAGO client package")
import src_rp.packages.wago_client as wago_client

print("Loading QCM interface")
import src_rp.packages.QCM_interface as QCM_interface 



# Server endpoint (must match server)
# url = "opc.tcp://132.229.46.113:4840"
# url = "opc.tcp://192.168.1.50:4840"
url = "opc.tcp://localhost:4840" 


# Connect to server
#client = Client(url)
#client.connect()
#print("Connected to OPC UA server")

#rp_ip = QCM_interface.find_red_pitaya(subnet= "132.229.46.")
rp_ip = "132.229.46.164"

qcm = QCM_interface.QCMInterface(rp_ip)
qcm.startup()
qcm.setReference()

wago = wago_client.WagoClient(url)

node_id = "ns=4;s=|var|750-8000 Basic Controller 100 2ETH ECO.Application.GVL_OPCUA"
key = ".in.thickness"


try:
    # Resolve namespace index dynamically
    # uri = "urn:wago-client"
    #idx = client.get_namespace_index(uri)
    # idx = 2    

    #setRef = wago.get_node(f"ns={idx};s=QCM.SetRef")
    
    # get frequency window nodes
    #window_M_node = wago.get_node(f"ns={idx};s=QCM.Window.M")
    #window_T_node = wago.get_node(f"ns={idx};s=QCM.Window.T")

    # Get the Frequency node
    #freq_M_node = wago.get_node(f"ns={idx};s=QCM.Frequency.M")
    #freq_T_node = wago.get_node(f"ns={idx};s=QCM.Frequency.T")
    
    #temp_node = wago.get_node(f"ns={idx};s=QCM.Temperature")
    
    if(wago.has_node(key)):
        print("The wago has the thinkness node")
    else:
        print("ERROR: The wago does not have the thickness mode")
    
    
    thickness_node = wago.get_node(node_id + key)
    
    

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
        
        
        print(f"Thickness measurement: {thickness} nm")
        #print(f"Frequency M: {freq_M:.4f},\t Frequency T: {freq_T:.4f},\t Thickness: {thickness:.4f} nm")
                
        
        
        # time.sleep(1)


finally:
    wago.disconnect()
    print("Disconnected")





