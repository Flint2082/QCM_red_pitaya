print("Starting source code")
print("Loading OPC-UA package")
from opcua import Client
from opcua import ua

import time
import socket

print("Loading WAGO client package")
import QCM_package.wago_client as wago_client

print("Loading QCM interface")
import QCM_package.QCM_interface as QCM_interface 

import QCM_package.TempCompAlgorithm as tca

# Server endpoint (must match server)
url = "opc.tcp://132.229.46.113:4840"
#url = "opc.tcp://192.168.1.50:4840"
# url = "opc.tcp://localhost:4840" 


#rp_ip = QCM_interface.find_red_pitaya(subnet= "132.229.46.")
# rp_ip = socket.gethostbyname(socket.gethostname())
#rp_ip = "192.168.1.55"
rp_ip = "132.229.46.164"
#rp_ip = "192.168.1.55"



node_id_base = "|var|750-8000 Basic Controller 100 2ETH ECO.Application."

if __name__ == "__main__":
    try:
        try:
            print(rp_ip)
            qcm = QCM_interface.QCMInterface(rp_ip)

            wago = wago_client.WagoClient(url)
            
            wago.connect()
            
            # Resolve namespace index dynamically
            uri = wago.client.application_uri
            print(uri)
            #idx = wago.client.get_namespace_index(uri)
            idx = 4
            print(idx)
            # get object nodes
            qcm_node = wago.get_node(ua.NodeId(node_id_base +               "GVL_QCM", idx))
            
            # All the CTRL nodes
            ctrl_node = wago.get_node(ua.NodeId(node_id_base +              "GVL_QCM.CTRL", idx))
            start_meas_node = wago.get_node(ua.NodeId(node_id_base +        "GVL_QCM.CTRL.StartMeasurement", idx))
            stop_meas_node = wago.get_node(ua.NodeId(node_id_base +         "GVL_QCM.CTRL.StopMeasurement", idx))
            set_zero_node = wago.get_node(ua.NodeId(node_id_base +          "GVL_QCM.CTRL.SetZero", idx))
            sweep_node = wago.get_node(ua.NodeId(node_id_base +             "GVL_QCM.CTRL.Sweep", idx))
            
            # All the SET nodes
            set_node = wago.get_node(ua.NodeId(node_id_base +               "GVL_QCM.SET", idx))
            density_node = wago.get_node(ua.NodeId(node_id_base +           "GVL_QCM.SET.Density", idx))
            #z_ratio_node = wago.get_node(ua.NodeId(node_id_base +           "GVL_QCM.SET.Z-ratio", idx))
            start_freq_mass_node = wago.get_node(ua.NodeId(node_id_base +   "GVL_QCM.SET.StartFreqMass", idx))
            start_freq_temp_node = wago.get_node(ua.NodeId(node_id_base +   "GVL_QCM.SET.StartFreqTemp", idx))
            ambient_temp_node = wago.get_node(ua.NodeId(node_id_base +      "GVL_QCM.SET.AmbientTemp", idx))
            coeff_node = wago.get_node(ua.NodeId(node_id_base +             "GVL_QCM.SET.Coeff", idx))
            
            # All the READ nodes
            get_node = wago.get_node(ua.NodeId(node_id_base +               "GVL_QCM.READ", idx))
            freq_M_node = wago.get_node(ua.NodeId(node_id_base +            "GVL_QCM.READ.MassFrequency", idx))
            freq_T_node = wago.get_node(ua.NodeId(node_id_base +            "GVL_QCM.READ.TempFrequency", idx))
            amp_M_node = wago.get_node(ua.NodeId(node_id_base +             "GVL_QCM.READ.MassAmplitude", idx))
            amp_T_node = wago.get_node(ua.NodeId(node_id_base +             "GVL_QCM.READ.TempAmplitude", idx))
            temp_node = wago.get_node(ua.NodeId(node_id_base +              "GVL_QCM.READ.Temperature", idx))
            comp_thickness_node = wago.get_node(ua.NodeId(node_id_base +    "GVL_QCM.READ.CompensatedThickness", idx))
            uncomp_thickness_node = wago.get_node(ua.NodeId(node_id_base +  "GVL_QCM.READ.UncompensatedThickness", idx))
            comp_rate_node = wago.get_node(ua.NodeId(node_id_base +         "GVL_QCM.READ.CompensatedRate", idx))
            uncomp_rate_node = wago.get_node(ua.NodeId(node_id_base +       "GVL_QCM.READ.UncompensatedRate", idx))
            Comp_M_freq_node = wago.get_node(ua.NodeId(node_id_base +       "GVL_QCM.READ.CompensatedMassFrequency", idx))
            timestamp_node = wago.get_node(ua.NodeId(node_id_base +         "GVL_QCM.READ.Timestamp", idx))
            error_node = wago.get_node(ua.NodeId(node_id_base +             "GVL_QCM.READ.ErrorCode", idx))
            
            error_node.set_value("No error")
            
            print("All nodes resolved successfully")
        except Exception as e:
            print(f"Node resolution failed: {e}")
            raise e  # Re-raise to be caught by outer block
        

        # SUPERLOOP
        while True: 
            lock_flag = False
            # wait for start measurement trigger
            if(start_meas_node.get_value()):
                start_freq_mass = start_freq_mass_node.get_value()
                start_freq_temp = start_freq_temp_node.get_value()
                qcm.startup(start_freq_mass, start_freq_temp)
                
                # wait until there is a lock
                for i in range(100): 
                    M_amp = qcm.getAmpAndPhase(1)[0]
                    T_amp = qcm.getAmpAndPhase(2)[0]
                    if M_amp > 0.01 and T_amp > 0.01:
                        print(f"Lock detected (Mass: {M_amp:.4f}, Temp: {T_amp:.4f}). Starting measurement.")
                        error_node.set_value("Lock detected, measurement started")
                        lock_flag = True
                        break
                    else:
                        print(f"Waiting for lock... (Mass: {M_amp:.4f}, Temp: {T_amp:.4f})")
                        error_node.set_value("Waiting for lock...")
                        time.sleep(0.1)
                    if i == 99:
                        print("Lock not detected after 10 seconds. Please check the system.")
                        error_node.set_value("Error: Lock not detected, check system")
                        lock_flag = False
                        break  # Exit loop if lock not detected after 10 seconds
                
                if not lock_flag:
                    start_meas_node.set_value(False)  # Reset trigger
                    continue  # Skip to next iteration of superloop to wait for next start trigger
                
                print("Measurement started")
                ambient_temp = ambient_temp_node.get_value()
                density = density_node.get_value()
                # z_ratio = z_ratio_node.get_value() # currently unused            
                qcm.setMeasurementReference(T=ambient_temp, mat_dens = density)
                print("setting start measurement node value to False")
                start_meas_node.set_value(wago._to_variant(False))  # Reset trigger
                error_node.set_value("Started measurement")  # Reset error state
                
                # start measurement loop
                while(True):
                    if(stop_meas_node.get_value()):
                        print("Measurement stopped")
                        try:
                            error_node.set_value("Measurement stopped")
                            stop_meas_node.set_value(wago._to_variant(False))  # Reset trigger
                        except Exception as e:
                            print(f"Error resetting stop trigger: {e}")
                        break
                    else: 
                        
                        try:
                            # Read sensor data
                            timestamp =0.0 # time.time()
                            freq_M, freq_T, T_calc, uncomp_thickness, comp_thickness, comp_freq_M = qcm.getMeasurement()
                            amp_M, amp_T = qcm.getAmpAndPhase(1)[0], qcm.getAmpAndPhase(2)[0]
                            qcm.moveWindow(freq_M, freq_T)  # Move window to current frequencies
                            
                            if amp_M < 0.01 or amp_T < 0.01:
                                print(f"Warning: No lock detected (Mass: {amp_M:.4f}, Temp: {amp_T:.4f}).")
                                error_node.set_value("Warning: Lock failure")
                                
                            # Write values back to server
                            freq_M_node.set_value(wago._to_variant(freq_M))
                            freq_T_node.set_value(wago._to_variant(freq_T))
                            amp_M_node.set_value(wago._to_variant(amp_M))
                            amp_T_node.set_value(wago._to_variant(amp_T))
                            temp_node.set_value(wago._to_variant(T_calc))
                            uncomp_thickness_node.set_value(wago._to_variant(uncomp_thickness))
                            comp_thickness_node.set_value(wago._to_variant(comp_thickness))
                            Comp_M_freq_node.set_value(wago._to_variant(comp_freq_M))
                            # timestamp_node.set_value(timestamp)
                            
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





