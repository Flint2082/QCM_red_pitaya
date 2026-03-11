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
#rp_ip = "132.229.46.164"
#rp_ip = "192.168.1.55"
rp_ip = "rp-f0ea58.local"


node_id_base = "|var|750-8000 Basic Controller 100 2ETH ECO.Application."

if __name__ == "__main__":
    try:
        try:
            print(rp_ip)
            qcm = QCM_interface.QCMInterface(rp_ip)

            wago = wago_client.WagoClient(url)
            
            # Resolve namespace index dynamically
            uri = wago.client.application_uri
            print(uri)
            #idx = wago.client.get_namespace_index(uri)
            idx = 4
            print(idx)
            # get object nodes
            # qcm_node = wago.get_node(ua.NodeId(node_id_base +               "GVL_QCM", idx))
            
            # All the CTRL nodes
            ctrl_node = wago.get_node(ua.NodeId(node_id_base +              "GVL_QCM.CTRL", idx))
            start_meas_node = wago.get_node(ua.NodeId(node_id_base +        "GVL_QCM.CTRL.StartMeasurement", idx))
            stop_meas_node = wago.get_node(ua.NodeId(node_id_base +         "GVL_QCM.CTRL.StopMeasurement", idx))
            set_zero_node = wago.get_node(ua.NodeId(node_id_base +          "GVL_QCM.CTRL.SetZero", idx))
            sweep_node = wago.get_node(ua.NodeId(node_id_base +             "GVL_QCM.CTRL.Sweep", idx))
            
            # All the SET nodes
            # set_node = wago.get_node(ua.NodeId(node_id_base +               "GVL_QCM.SET", idx))
            density_node = wago.get_node(ua.NodeId(node_id_base +           "GVL_QCM.SET.Density", idx))
            #z_ratio_node = wago.get_node(ua.NodeId(node_id_base +           "GVL_QCM.SET.Z-ratio", idx))
            start_freq_mass_node = wago.get_node(ua.NodeId(node_id_base +   "GVL_QCM.SET.StartFreqMass", idx))
            start_freq_temp_node = wago.get_node(ua.NodeId(node_id_base +   "GVL_QCM.SET.StartFreqTemp", idx))
            ambient_temp_node = wago.get_node(ua.NodeId(node_id_base +      "GVL_QCM.SET.AmbientTemp", idx))
            coeff_node = wago.get_node(ua.NodeId(node_id_base +             "GVL_QCM.SET.Coeff", idx))
            
            # All the READ nodes
            # get_node = wago.get_node(ua.NodeId(node_id_base +               "GVL_QCM.READ", idx))
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
            
            wago.write_node(error_node, "No error")  # Initialize error node
            
            
            
            print("All nodes resolved successfully")
        except Exception as e:
            print(f"Node resolution failed: {e}")
            raise e  # Re-raise to be caught by outer block
        

        # SUPERLOOP
        while True: 
            lock_flag = False
            # wait for start measurement trigger
            if(wago.read_node(start_meas_node) == True):
                start_freq_mass = wago.read_node(start_freq_mass_node)
                start_freq_temp = wago.read_node(start_freq_temp_node) 
                qcm.startup(start_freq_mass, start_freq_temp)
                
                # wait until there is a lock
                for i in range(100): 
                    M_amp = qcm.getAmpAndPhase(1)[0]
                    T_amp = qcm.getAmpAndPhase(2)[0]
                    if M_amp > 0.01 and T_amp > 0.01:
                        print(f"Lock detected (Mass: {M_amp:.4f}, Temp: {T_amp:.4f}). Starting measurement.")
                        wago.write_node(error_node, "Lock detected, measurement started")
                        lock_flag = True
                        break
                    else:
                        print(f"Waiting for lock... (Mass: {M_amp:.4f}, Temp: {T_amp:.4f})")
                        wago.write_node(error_node, "Waiting for lock...")
                        time.sleep(0.1)
                    if i == 99:
                        print("Lock not detected after 10 seconds. Please check the system.")
                        wago.write_node(error_node, "Error: Lock not detected, check system")
                        lock_flag = False
                        break  # Exit loop if lock not detected after 10 seconds
                
                if not lock_flag:
                    wago.write_node(start_meas_node, False)  # Reset trigger
                    continue  # Skip to next iteration of superloop to wait for next start trigger
                
                print("Measurement started")
                ambient_temp = wago.read_node(ambient_temp_node)
                density = wago.read_node(density_node)
                # z_ratio = z_ratio_node.get_value() # currently unused            
                qcm.setMeasurementReference(T=ambient_temp, mat_dens = density)
                print("setting start measurement node value to False")
                wago.write_node(error_node, "Started measurement")  # Reset error state
                
                # start measurement loop
                while(True):
                    if(wago.read_node(stop_meas_node) == True):
                        print("Measurement stopped")
                        try:
                            wago.write_node(error_node, "Measurement stopped")
                            wago.write_node(stop_meas_node, False)  # Reset trigger
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
                                wago.write_node(error_node, "Warning: Lock failure")
                                
                            # Write values back to server
                            wago.write_node(freq_M_node, wago._to_variant(freq_M))
                            wago.write_node(freq_T_node, wago._to_variant(freq_T))
                            wago.write_node(amp_M_node, wago._to_variant(amp_M))
                            wago.write_node(amp_T_node, wago._to_variant(amp_T))
                            wago.write_node(temp_node, wago._to_variant(T_calc))
                            wago.write_node(uncomp_thickness_node, wago._to_variant(uncomp_thickness))
                            wago.write_node(comp_thickness_node, wago._to_variant(comp_thickness))
                            wago.write_node(Comp_M_freq_node, wago._to_variant(comp_freq_M))
                            # wago.write_node(timestamp_node, wago._to_variant(timestamp))
                            
                        except Exception as e:
                            print(f"Measurement loop error: {e}")
                            try:
                                wago.write_node(error_node, str(e))
                            except Exception as inner_e:
                                print(f"Error setting error node: {inner_e}")
                            break  # Exit inner loop on error
                        
                        time.sleep(0.01)  # Small delay to prevent busy loop when waiting for stop signal              
                    
            time.sleep(1)  # Avoid busy loop

    
    finally:
        wago.disconnect()
        print("Disconnected")





