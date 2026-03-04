print("Starting source code")
print("Loading OPC-UA package")
from opcua import Client
from opcua import ua

import time

print("Loading WAGO client package")
import QCM_package.wago_client as wago_client

print("Loading QCM interface")
import QCM_package.QCM_interface as QCM_interface 

import QCM_package.TempCompAlgorithm as tca



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
    try:
        # Resolve namespace index dynamically
        uri = wago.client.application_uri
        idx = wago.client.get_namespace_index(uri)
        
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
        error_node = wago.get_node(ua.NodeId(node_id_base +             "QCM.READ.ErrorCode", idx))
        
        print("All nodes resolved successfully")
    except Exception as e:
        print(f"Node resolution failed: {e}")
        raise e  # Re-raise to be caught by outer block
    
    

    # SUPERLOOP
    while True: 
        
        # wait for start measurement trigger
        if(start_meas_node.get_value()):
            print("Measurement started")
            qcm.setMeasurementReference()
            start_meas_node.set_value(False)  # Reset trigger
            
            # start measurement loop
            while(True):
                if(stop_meas_node.get_value()):
                    print("Measurement stopped")
                    try:
                        stop_meas_node.set_value(False)  # Reset trigger
                    except Exception as e:
                        print(f"Error resetting stop trigger: {e}")
                    break
                else: 
                    
                    try:
                        # Read sensor data
                        timestamp = time.time()
                        T_calc, uncomp_thickness, comp_thickness, comp_freq_M = qcm.getMeasurement()
                        
                        # Write values back to server
                        freq_M_node.set_value(qcm.getFreq(qcm.massMode))
                        freq_T_node.set_value(qcm.getFreq(qcm.tempMode))
                        temp_node.set_value(T_calc)
                        uncomp_thickness_node.set_value(uncomp_thickness)
                        comp_thickness_node.set_value(comp_thickness)
                        Comp_M_freq_node.set_value(comp_freq_M)
                        timestamp_node.set_value(timestamp)
                        
                    except Exception as e:
                        print(f"Measurement loop error: {e}")
                        try:
                            error_node.set_value(str(e))
                        except Exception as inner_e:
                            print(f"Error setting error node: {inner_e}")
                        break  # Exit inner loop on error
                    
                    time.sleep(0.01)  # Small delay to prevent busy loop when waiting for stop signal              
                
        time.sleep(1)  # Avoid busy loop

 
finally:
    wago.disconnect()
    print("Disconnected")





