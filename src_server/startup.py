
import casperfpga
import os
import time
import TempCompAlgorithm as tca
import calendar
import matplotlib.pyplot as plt
from matplotlib.widgets import Slider
from collections import deque


directory = os.path.expanduser(
    "~/work/CASPER_repos/qcm_red_pitaya/model_composer/qcm_rp/outputs/"
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

def setProp(osc_index, gain):
    fpga.write_int(device_name='proportional_'+str(osc_index),integer=int(gain*4294967296))

def setInt(osc_index, gain):
    fpga.write_int(device_name='integral_'+str(osc_index),integer=int(gain*4294967296))
    
def setLDGain(osc_index, gain):
    fpga.write_int(device_name='ld_gain_'+str(osc_index),integer=int(gain*4294967296))

def setInv(osc_index, inv):
    fpga.write('inv_fb_'+str(osc_index),(inv).to_bytes(4,'big'))
    
def standby(osc_index):
    setFreq(osc_index,0)
    setInt(osc_index,0)
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
    for f in range(start, stop, step):
        reset()
        setFreq(osc_index, f)
        time.sleep(1)
	
def startup():
    #STARTUP AUTOMATON (TEMP)
    reset()
    
    """
    ## 10MHz crystal
    setInv(1,1)
    setFreq(1,3380000)                                                                                                                                                   
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
    setInv(1,1)
    setFreq(1,5975000)                                                                                                                                                   
    setInt(1,0.001)
    setLDGain(1,0.01)
    time.sleep(1.5)
    setInt(1,0.000001)
    
    setInv(2,1)
    setFreq(2,6555000)
    setInt(2,0.001)
    setLDGain(2,0.01)
    time.sleep(1.5)
    setInt(2,0.00001)
    

def startMeasurement(T = 23,debug=False):
    # Measurement routine

    # Initialize the temperature compensation algorithm with calibration and starting values
    temp_comp = tca.TempCompAlgorithm(
        coefficient_file = "data/coeffecients.csv",
        T_start=T, # would be nice to measure this with a thermometer
        fT_start= getFreq(2), # Hz
        fM_start= getFreq(1)  # Hz
    )

    # Initialize measurement loop
    if debug:
        print("Starting frequency T mode:", getFreq(1))
        print("Starting frequency M mode:", getFreq(2))
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

    
        # Measurement loop
        while True:
            fT = getFreq(2)
            fM = getFreq(1)
            
            
            T_calc, uncompensated_thickness_nm, compensated_thickness_nm, compensated_m_freq = temp_comp.FreqToTemp(fT, fM)

            if(debug==True):
                print(f"{calendar.timegm(time.gmtime())} \t {fT:.8f} \t {fM:.8f} \t {T_calc:.2f} \t {uncompensated_thickness_nm:.4f} \t {compensated_thickness_nm:.4f}")
                with open('data/output.csv', mode='a') as log_file:
                    log_file.write(f"{calendar.timegm(time.gmtime())},{fT},{fM},{T_calc},{uncompensated_thickness_nm},{compensated_thickness_nm}\n")
            else:
                print(f"{fT:.3f} \t {fM:.3f}")
                
                #with open('output.csv', mode='a') as log_file:
                #    log_file.write(f"{T},{compensated_thickness_nm}\n")
                    
            
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

                    
                    
            time.sleep(0.1) # <-- set measurement interval here
    except KeyboardInterrupt:
        print("\nMeasuremenFt stopped by user")
        

def startCalibration(cal_file_name):
    # confirm overwrite
    if(input(f"This will overwrite {cal_file_name}. Continue? (y/n): ") != 'y'):
        print("Calibration aborted.")
        return
    
    with open(cal_file_name, mode='w') as cal_file:
            cal_file.write(f"Temp,Freq_T,Freq_M\n")
    
    # Calibration routine
    startup()
    while(True):
        temp = input("Current Temperature (C): ")
        if temp == '0':
            break
        freqM = getFreq(1)
        freqT = getFreq(2)
        print(f"Freq M: {freqM}, Freq T: {freqT} at Temp: {temp}")
        with open('data/calibration_data.csv', mode='a') as cal_file:
            cal_file.write(f"{temp},{freqT},{freqM}\n")


    
            



startup()
 
 
 







