
import casperfpga
import os
import time
import TempCompAlgorithm as tca
import calendar
import matplotlib.pyplot as plt
from collections import deque


directory = os.path.expanduser(
    "~/work/CASPER_repos/qcm_red_pitaya/qcm_rp/outputs/"
)

newest_file = max(
	(os.path.join(directory, f) for f in os.listdir(directory)),
	key=os.path.getmtime
)

fpga = casperfpga.CasperFpga('rp-f0ea58.local')

print("Newest file", newest_file)

fpga.upload_to_ram_and_program(newest_file)

### Function declarations

def to_signed(value, bits):
    """Interpret unsigned integer as signed."""
    mask = (1 << bits) - 1
    value &= mask  # ensure it fits within the given bit width
    sign_bit = 1 << (bits - 1)
    return (value ^ sign_bit) - sign_bit

def setFreq(osc_index, freq):
    fpga.write_int(device_name='freq_'+str(osc_index),integer=int(freq*64))

def setGain(osc_index, gain):
    fpga.write_int(device_name='filter_gain_'+str(osc_index),integer=int(gain*4294967296))
    
def setLDGain(osc_index, gain):
    fpga.write_int(device_name='ld_gain_'+str(osc_index),integer=int(gain*4294967296))

def setInv(osc_index, inv):
    fpga.write('inv_fb_'+str(osc_index),(inv).to_bytes(4,'big'))
    
def standby(osc_index):
    setFreq(osc_index)
    setGain(osc_index)
    reset()

def reset():
    fpga.write('reset',(1).to_bytes(4,'big'))
    time.sleep(0.001)
    fpga.write('reset',(0).to_bytes(4,'big'))
    
def getFreq(osc_index):
    lsb = fpga.read_int(f'frequency_out_lsb_{osc_index}') & 0xFFFFFFFF
    msb = fpga.read_int(f'frequency_out_msb_{osc_index}') & 0xFFFFFFFF

    raw = (msb << 32) | lsb      # reconstruct full fixed-point integer
    freq = raw / (1 << 10)       # apply fixed-point scaling

    return freq

def getLock(osc_index):
    lock_val = to_signed(fpga.read_int(f'lock_detect_{osc_index}'),32)
    return lock_val/2**31
    

def sweep(osc_index, start, stop, step):
    standby(2)
    setGain(osc_index, 0.01)
    for f in range(start, stop, step):
        reset()
        setFreq(osc_index, f)
        time.sleep(1)
	
def startup():
    #STARTUP AUTOMATON (TEMP)
    reset()
    
    ## 10 MHz crystal
    #setFreq(1,3730000)                                                                                                                                                   
    #setGain(1,0.001)
    #time.sleep(3)
    #setGain(1,0.00001)
    #setFreq(2,9990000)
    #setGain(2,0.001)
    #time.sleep(3)
    #setGain(2,0.00001)

    ## 6MHz crystal
    setFreq(1,5975000)                                                                                                                                                   
    setGain(1,0.001)
    setLDGain(1,0.01)
    time.sleep(1)
    setGain(1,0.000001)
    setFreq(2,6562000)
    setGain(2,0.001)
    setLDGain(2,0.01)
    time.sleep(1)
    setGain(2,0.0001)
    

def startMeasurement(T = 23,debug=False):
    # Measurement routine
    startup()
    
    # Initialize the temperature compensation algorithm with calibration and starting values
    temp_comp = tca.TempCompAlgorithm(
        parameter_file='calParams.csv',
        T_start=T, # would be nice to measure this with a thermocouple
        fT_start= getFreq(2),
        fM_start= getFreq(1)
    )

    # Initialize measurement loop
    if debug:
        print("Starting frequency T mode:", getFreq(1))
        print("Starting frequency M mode:", getFreq(2))
        print("Starting temperature:", T)
        print("\nStarting measurement...\n")
        print("Time \t\t Freq_T \t Freq_M \t Temp_C \t Temp_rela \t Uncomp_thick_nm \t Comp_thick_nm")
        with open('output.csv', mode='w') as log_file:
            log_file.write("Time,Freq_T,Freq_M,Temp_C,Uncomp_thick_nm,Comp_thick_nm\n")
    else:
        print("Temp_freq \t Mass_freq")        
        #with open('output.csv', mode='w') as log_file:
        #    log_file.write("Temp_C,Comp_thick_nm\n")
            
    try:
        # ---- Live plot setup ----
        plt.ion()

        fig, (ax1,ax2) = plt.subplots(2,1,sharex=True, hspace=0.4)
        ax1.set_title("Mass mode measurement")
        ax1.set_xlabel("Time [s]")
        ax1.set_ylabel("Delta Frequency [Hz]")
        ax2.set_title("Temp mode measurement")
        ax2.set_xlabel("Time [s]")
        ax2.set_ylabel("Delta Frequency [Hz]")
        

        # Keep a rolling window of points
        max_points = 1000
        time_data = deque(maxlen=max_points)
        temp_freq_data = deque(maxlen=max_points)
        mass_freq_data = deque(maxlen=max_points)
        thickness_data = deque(maxlen=max_points)

        lineM, = ax1.plot([], [], lw=2)
        lineT, = ax2.plot([], [], lw=2)
        start_time = time.time()

        fig.show()
        fig.canvas.draw()

    
        # Measurement loop
        while True:
            fT = getFreq(2)
            fM = getFreq(1)
            
            
            T, uncompensated_thickness_nm, compensated_thickness_nm = temp_comp.FreqToTemp(fT, fM)
            if(debug==True):
                print(f"{calendar.timegm(time.gmtime())} \t {fT:.8f} \t {fM:.8f} \t {T:.2f} \t {uncompensated_thickness_nm:.4f} \t {compensated_thickness_nm:.4f}")
                with open('output.csv', mode='a') as log_file:
                    log_file.write(f"{calendar.timegm(time.gmtime())},{fT},{fM},{T},{uncompensated_thickness_nm},{compensated_thickness_nm}\n")
            else:
                print(f"{fT:.3f} \t {fM:.3f}")
                
                #with open('output.csv', mode='a') as log_file:
                #    log_file.write(f"{T},{compensated_thickness_nm}\n")
                    
            
            # ---- Update live plot ----
            t_now = time.time() - start_time
            time_data.append(t_now)
            temp_freq_data.append(fT)
            mass_freq_data.append(fM)
            thickness_data.append(compensated_thickness_nm)

            lineM.set_data(time_data, mass_freq_data)
            ax1.relim()
            ax1.autoscale_view()
            
            lineT.set_data(time_data, temp_freq_data)
            ax2.relim()
            ax2.autoscale_view()

            fig.canvas.draw()
            fig.canvas.flush_events()

                    
                    
            time.sleep(0.1) # <-- set measurement interval here
    except KeyboardInterrupt:
        print("\nMeasuremenFt stopped by user")
        

def startCalibration():
    # confirm overwrite
    if(input("This will overwrite 'calibration_data.csv'. Continue? (y/n): ") != 'y'):
        print("Calibration aborted.")
        return
    
    with open('calibration_data.csv', mode='w') as cal_file:
            cal_file.write(f"Temp,Freq_T,Freq_M\n")
    
    # Calibration routine
    startup()
    while(True):
        temp = input("Current Temperature (C): ")
        if temp == 0:
            break
        freqM = getFreq(1)
        freqT = getFreq(2)
        print(f"Freq M: {freqM}, Freq T: {freqT} at Temp: {temp}")
        with open('calibration_data.csv', mode='a') as cal_file:
            cal_file.write(f"{temp},{freqT},{freqM}\n")


    
            


 
 
 







