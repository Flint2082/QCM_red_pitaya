### Class to interface with the QCM Red Pitaya firmware, and to run measurements and calibrations.


import casperfpga
import os
import time
import src_rp.packages.TempCompAlgorithm as tca
import calendar
from collections import deque
import matplotlib.pyplot as plt
from matplotlib.widgets import Slider
import socket

def find_red_pitaya(subnet="192.168.1.", start=1, end=254, timeout=0.2):
    """Scan the subnet for a Red Pitaya by attempting to connect to the KATCP port (7147)."""
    port = 7147  # Default KATCP port for Red Pitaya CASPER builds
    for i in range(start, end + 1):
        ip = f"{subnet}{i}"
        print(f"Currencly trying {ip}")
        try:
            with socket.create_connection((ip, port), timeout=timeout):
                print(f"Found Red Pitaya at {ip}")
                return ip
        except (ConnectionRefusedError, socket.timeout, OSError):
            continue
    raise RuntimeError("Red Pitaya not found on subnet")

class QCMInterface:
    def __init__(self, RP_IP):
        directory = os.path.expanduser(
            "~/QCM_red_pitaya/model_composer/qcm_rp/outputs/"
        )
        newest_file = max(
            (os.path.join(directory, f) for f in os.listdir(directory)),
            key=os.path.getmtime
        )
        
        self.window_size = 2**14

        # self.fpga = casperfpga.CasperFpga('132.229.46.164')
        # '192.168.1.55'
        self.fpga = casperfpga.CasperFpga(RP_IP)
        print("CasperFpga connected to red pitaya")

        print("Newest file", newest_file)

        try:
            self.fpga.upload_to_ram_and_program(newest_file)
        except Exception as e:
            print(f"Failed to upload FPGA program: {e}")
            raise


    ### Function declarations

    def to_signed(self, value, bits):
        """Interpret unsigned integer as signed."""
        mask = (1 << bits) - 1
        value &= mask  # ensure it fits within the given bit width
        sign_bit = 1 << (bits - 1)
        return (value ^ sign_bit) - sign_bit

    def setFreq(self, osc_index, freq):
        self.fpga.write_int(device_name='freq_'+str(osc_index),integer=int(freq*64))

    def setProp(self, osc_index, gain):
        self.fpga.write_int(device_name='proportional_'+str(osc_index),integer=int(gain*4294967296))

    def setInt(self, osc_index, gain):
        self.fpga.write_int(device_name='integral_'+str(osc_index),integer=int(gain*4294967296))
        
    def setLDGain(self, osc_index, gain):
        self.fpga.write_int(device_name='ld_gain_'+str(osc_index),integer=int(gain*4294967296))

    def setInv(self, osc_index, inv):
        self.fpga.write('inv_fb_'+str(osc_index),(inv).to_bytes(4,'big'))
        
    def standby(self, osc_index):
        self.setFreq(osc_index,0)
        self.setInt(osc_index,0)
        self.reset()

    def reset(self):
        self.fpga.write('reset',(1).to_bytes(4,'big'))
        time.sleep(0.001)
        self.fpga.write('reset',(0).to_bytes(4,'big'))
        
    def getFreq(self, osc_index):
        lsb = self.fpga.read_int(f'frequency_out_lsb_{osc_index}') & 0xFFFFFFFF
        msb = self.fpga.read_int(f'frequency_out_msb_{osc_index}') & 0xFFFFFFFF

        raw = (msb << 32) | lsb      # reconstruct full fixed-point integer
        freq = raw / (1 << 10)       # apply fixed-point scaling

        return freq

    def getLock(self, osc_index):
        lock_val = self.to_signed(self.fpga.read_int(f'lock_detect_{osc_index}'),32)
        return lock_val/2**31
        

    def sweep(self, osc_index, start, stop, step):
        self.standby(osc_index)
        for f in range(start, stop, step):
            self.reset()
            self.setFreq(osc_index, f)
            time.sleep(1)
        
    def startup(self):
        #STARTUP AUTOMATON (TEMP)
        self.reset()
        
        """
        ## 10MHz crystal
        self.setInv(1,1)
        self.setFreq(1,3380000)                                                                                                                                                   
    
        setInt(1,0.001)
        setLDGain(1,0.01)
        time.sleep(1)
        setInt(1,0.000001)
        
        setInv(2,1)
        setFreq(2,3720000)
        setInt(2,0.001)
        setLDGain(2,0.01)
        time.sleep(1)
        setInt(2,0.0001)
        
        """
        ## 6MHz crystal
        self.setInv(1,1)
        self.setFreq(1,5975000)                                                                                                                                                   
        self.setInt(1,0.001)
        self.setLDGain(1,0.01)
        time.sleep(1.5)
        self.setInt(1,0.000001)
        
        self.setInv(2,1)
        self.setFreq(2,6555000)
        self.setInt(2,0.001)
        self.setLDGain(2,0.01)
        time.sleep(1.5)
        self.setInt(2,0.00001)
        
        
    def setReference(self):
        self.fM_start = self.getFreq(1)
        self.fT_start = self.getFreq(2)
        self.T_start = 23 # would be nice to measure this with a thermometer
        print(f"Reference set: fM={self.fM_start}, fT={self.fT_start}, T={self.T_start}")
        

    def startMeasurement(self, T = 23, moving_window = True, plot=True, debug=False):
        # Measurement routine

        # Initialize the temperature compensation algorithm with calibration and starting values
        temp_comp = tca.TempCompAlgorithm(
            coefficient_file = "data/coeffecients.csv",
            T_start=T, # would be nice to measure this with a thermometer
            fT_start= self.getFreq(2), # Hz
            fM_start= self.getFreq(1)  # Hz
        )

        # Initialize measurement loop
        if debug:
            print("Starting frequency T mode:", self.getFreq(1))
            print("Starting frequency M mode:", self.getFreq(2))
            print("Starting temperature:", T)
            print("\nStarting measurement...\n")
            print("Time \t\t Freq_T \t Freq_M \t Temp_C \t Temp_rela \t Uncomp_thick_nm \t Comp_thick_nm")
            with open('data/output.csv', mode='w') as log_file:
                log_file.write("Time,Freq_T,Freq_M,Temp_C,Uncomp_thick_nm,Comp_thick_nm\n")
        else:
            print("Temp_freq \t Mass_freq")        
            #with open('output.csv', mode='w') as log_file:
            #    log_file.write("Temp_C,Comp_thick_nm\n")
         
            
        try:
            if plot:
                # ---- Live plot setup ----
                plt.ion()

                fig, (ax1,ax2,ax3,ax4) = plt.subplots(4,1,sharex=True)
                #plt.subplots_adjust(bottom=0.30) 
                fig.set_size_inches(10,14)
                
                """
                ax_slider1 = plt.axes([0.15, 0.18, 0.7, 0.03])
                ax_slider2 = plt.axes([0.15, 0.10, 0.7, 0.03])
                
                slider1 = Slider(
                    ax=ax_slider1,
                    label="Integral control",
                    valmin=-10,
                    valmax=0,
                    valinit=1
                )

                slider2 = Slider(
                    ax=ax_slider2,
                    label="Proportional control",
                    valmin=-10,
                    valmax=0,
                    valinit=2
                )
                """
                    
                        
                ax1.set_title("Mass mode measurement")
                ax1.set_xlabel("Time [s]")
                ax1.set_ylabel("Frequency [Hz]")
                ax2.set_title("Temp mode measurement")
                ax2.set_xlabel("Time [s]")
                ax2.set_ylabel("Frequency [Hz]")
                ax3.set_title("Calculated temperature")
                ax3.set_xlabel("Time [s]")
                ax3.set_ylabel("Temperature [C]")
                ax4.set_title("Compensated thickmness")
                ax4.set_xlabel("Time [s]")
                ax4.set_ylabel("Delta thickness [nm]")
                

                # Keep a rolling window of points
                max_points = 300
                time_data = deque(maxlen=max_points)
                temp_freq_data = deque(maxlen=max_points)
                mass_freq_data = deque(maxlen=max_points)
                temp_data = deque(maxlen=max_points)
                thickness_data = deque(maxlen=max_points)
                uncomp_thick_data = deque(maxlen=max_points)

                lineFM, = ax1.plot([], [], lw=2)
                lineFT, = ax2.plot([], [], lw=2)
                lineTemp, = ax3.plot([], [], lw=2)
                lineThick, = ax4.plot([], [], lw=2, )
                lineUnThick, = ax4.plot([], [], lw=2, color='red', linestyle='dashed' )
                start_time = time.time()

                fig.tight_layout() # adjust graph spacing
                fig.show()
                fig.canvas.draw()

        
        
            ### Measurement loop
            while True:
                fT = self.getFreq(2)
                fM = self.getFreq(1)
                
                
                T_calc, uncompensated_thickness_nm, compensated_thickness_nm, compensated_m_freq = temp_comp.FreqToTemp(fT, fM)

                if(debug==True):
                    print(f"{calendar.timegm(time.gmtime())} \t {fT:.8f} \t {fM:.8f} \t {T_calc:.2f} \t {uncompensated_thickness_nm:.4f} \t {compensated_thickness_nm:.4f}")
                    with open('data/output.csv', mode='a') as log_file:
                        log_file.write(f"{calendar.timegm(time.gmtime())},{fT},{fM},{T_calc},{uncompensated_thickness_nm},{compensated_thickness_nm}\n")
                else:
                    print(f"{fT:.3f} \t {fM:.3f}")
                    
                    #with open('output.csv', mode='a') as log_file:
                    #    log_file.write(f"{T},{compensated_thickness_nm}\n")
                        
                if plot:
                    # ---- Update live plot ----
                    t_now = time.time() - start_time
                    time_data.append(t_now)
                    temp_freq_data.append(fT)
                    mass_freq_data.append(fM)
                    temp_data.append(T_calc)
                    thickness_data.append(compensated_thickness_nm)
                    uncomp_thick_data.append(uncompensated_thickness_nm)

                    lineFM.set_data(time_data, mass_freq_data)
                    ax1.relim()
                    ax1.autoscale_view()
                    
                    lineFT.set_data(time_data, temp_freq_data)
                    ax2.relim()
                    ax2.autoscale_view()
                    
                    lineTemp.set_data(time_data, temp_data)
                    ax3.relim()
                    ax3.autoscale_view()
                    
                    lineThick.set_data(time_data, thickness_data)
                    lineUnThick.set_data(time_data, uncomp_thick_data)
                    ax4.relim()
                    ax4.autoscale_view()

                    fig.canvas.draw()
                    fig.canvas.flush_events()

                        
                if moving_window:
                    self.setFreq(1, fT - (self.window_size/2))
                    self.setFreq(2, fM - (self.window_size/2))
                
                       
                time.sleep(0.1) # <-- set measurement interval here
        except KeyboardInterrupt:
            print("\nMeasurement stopped by user")
            

    def startCalibration(self, cal_file_name):
        # confirm overwrite
        if(input(f"This will overwrite {cal_file_name}. Continue? (y/n): ") != 'y'):
            print("Calibration aborted.")
            return
        
        with open(cal_file_name, mode='w') as cal_file:
                cal_file.write(f"Temp,Freq_T,Freq_M\n")
        
        # Calibration routine
        self.startup()
        while(True):
            temp = input("Current Temperature (C): ")
            if temp == '0':
                break
            freqM = self.getFreq(1)
            freqT = self.getFreq(2)
            print(f"Freq M: {freqM}, Freq T: {freqT} at Temp: {temp}")
            with open('data/calibration_data.csv', mode='a') as cal_file:
                cal_file.write(f"{temp},{freqT},{freqM}\n")


        
        
            


 
 
 







