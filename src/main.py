print("Starting source code")
print("Loading OPC-UA package")
from opcua import Client
from opcua import ua

import time

print("Loading WAGO client package")
import QCM_package.wago_client as wago_client

print("Loading QCM interface")
import QCM_package.QCM_interface as QCM_interface 



# Server endpoint (must match server)
url = "opc.tcp://132.229.46.113:4840"
# url = "opc.tcp://192.168.1.50:4840"
# url = "opc.tcp://localhost:4840" 


#rp_ip = QCM_interface.find_red_pitaya(subnet= "132.229.46.")
rp_ip = "132.229.46.164"
#rp_ip = "192.168.1.55"

qcm = QCM_interface.QCMInterface(rp_ip)
qcm.startup()
qcm.setReference()

wago = wago_client.WagoClient(url)

node_id_base = "|var|750-8000 Basic Controller 100 2ETH ECO.Application.GVL_OPCUA."


try:
    # Resolve namespace index dynamically
    uri = wago.client.application_uri
    idx = wago.client.get_namespace_index(uri)


    #setRef = wago.get_node(f"ns={idx};s=QCM.SetRef")
    
    # get object nodes
    qcm_node = wago.get_node(ua.NodeId(node_id_base +               "QCM", idx))
    
    # All the CTRL nodes
    ctrl_node = wago.get_node(ua.NodeId(node_id_base +              "QCM.CTRL", idx))
    start_meas_node = wago.get_node(ua.NodeId(node_id_base +        "QCM.CTRL.StartMeasurement", idx))
    stop_meas_node = wago.get_node(ua.NodeId(node_id_base +         "QCM.CTRL.StopMeasurement", idx))
    set_zero_node = wago.get_node(ua.NodeId(node_id_base +          "QCM.CTRL.SetZero", idx))
    sweep_node = wago.get_node(ua.NodeId(node_id_base +             "QCM.CTRL.Sweep", idx))
    
    # All the SET nodes
    set_node = wago.get_node(ua.NodeId(node_id_base +               "QCM.SET", idx))
    density_node = wago.get_node(ua.NodeId(node_id_base +           "QCM.SET.Density", idx))
    z_ratio_node = wago.get_node(ua.NodeId(node_id_base +           "QCM.SET.Z-ratio", idx))
    start_freq_node = wago.get_node(ua.NodeId(node_id_base +        "QCM.SET.StartFreq", idx))
    ambient_temp_node = wago.get_node(ua.NodeId(node_id_base +      "QCM.SET.AmbientTemp", idx))
    coeff_node = wago.get_node(ua.NodeId(node_id_base +             "QCM.SET.Coeff", idx))
    
    # All the READ nodes
    get_node = wago.get_node(ua.NodeId(node_id_base +               "QCM.READ", idx))
    freq_M_node = wago.get_node(ua.NodeId(node_id_base +            "QCM.READ.MassFrequency", idx))
    freq_T_node = wago.get_node(ua.NodeId(node_id_base +            "QCM.READ.TempFrequency", idx))
    temp_node = wago.get_node(ua.NodeId(node_id_base +              "QCM.READ.Temperature", idx))
    comp_thickness_node = wago.get_node(ua.NodeId(node_id_base +    "QCM.READ.CompensatedThickness", idx))
    uncomp_thickness_node = wago.get_node(ua.NodeId(node_id_base +  "QCM.READ.UncompensatedThickness", idx))
    comp_rate_node = wago.get_node(ua.NodeId(node_id_base +         "QCM.READ.CompensatedRate", idx))
    uncomp_rate_node = wago.get_node(ua.NodeId(node_id_base +       "QCM.READ.UncompensatedRate", idx))
    Comp_M_freq_node = wago.get_node(ua.NodeId(node_id_base +       "QCM.READ.CompensatedMassFrequency", idx))
    timestamp_node = wago.get_node(ua.NodeId(node_id_base +         "QCM.READ.Timestamp", idx))
    error_node = wago.get_node(ua.NodeId(node_id_base +             "QCM.READ.Error", idx))

    
    

    # Read values in a loop
    while True: 
        #if setRef.get_value():
        #    qcm.setReference()
        #    setRef.set_value(False)  # Reset trigger
        
        #window_M = qcm.setFreq(1, wago.read_node(window_M_node))
        #window_T = qcm.setFreq(2, wago.read_node(window_T_node))
        #freq_M = qcm.getFreq(1)
        #freq_T = qcm.getFreq(2)
        
        thickness = 6
        
        
        #wago.write_node(freq_M_node, freq_M)
        #wago.write_node(freq_T_node, freq_T)
        wago.write_node(uncomp_thickness_node, thickness)
        
        
        print(f"Thickness measurement: {thickness} nm")
        #print(f"Frequency M: {freq_M:.4f},\t Frequency T: {freq_T:.4f},\t Thickness: {thickness:.4f} nm")
                
        
        
        time.sleep(1)

finally:
    wago.disconnect()
    print("Disconnected")





