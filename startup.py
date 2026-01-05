import casperfpga
import os
import time
import TempCompAlgorithm as tca
import calendar




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

def sweep(start, stop, step):
    for f in range((start*64), (stop*64), (step*64)):
        avg = 0
        fpga.write_int(device_name='freq_adj',integer=int(f))
        for i in range(100):
            #time.sleep(0.0001)
            avg += to_signed(fpga.read_int('phase_out'),29)
        avg = avg / 100
        print(f/64, ", ", avg)

def setFreq(osc_index, freq):
    fpga.write_int(device_name='freq_'+str(osc_index),integer=int(freq*64))

def setGain(osc_index, gain):
    fpga.write_int(device_name='filter_gain_'+str(osc_index),integer=int(gain*4294967296))

def setInv(osc_index, inv):
    fpga.write('inv_fb_'+str(osc_index),(inv).to_bytes(4,'big'))

def reset():
    fpga.write('reset',(1).to_bytes(4,'big'))
    time.sleep(0.001)
    fpga.write('reset',(0).to_bytes(4,'big'))
    
def getFreq(osc_index):
    return fpga.read_int('frequency_out_'+str(osc_index))/64


def startMeasurement(T = 23,debug=False):
    # Initialize the temperature compensation algorithm with calibration and starting values
    temp_comp = tca.TempCompAlgorithm(
        parameter_file='calParams.csv',
        T_start=T, # would be nice to measure this with a thermocouple
        fT_start=3735744/1000000,  #getFreq(1),
        fM_start=99997581/1000000  #getFreq(2)
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
        print("Temp_C \t Comp_thick_nm")
        with open('output.csv', mode='w') as log_file:
            log_file.write("Temp_C,Comp_thick_nm\n")
            
    try:
        # Measurement loop
        while True:
            fT = 3734505/1000000 #getFreq(1)
            fM = 99998471/1000000#getFreq(2)
            T, T_rel, uncompensated_thickness_nm, compensated_thickness_nm = temp_comp.FreqToTemp(fT, fM)
            if(debug==True):
                print(f"{calendar.timegm(time.gmtime())} \t {fT:.8f} \t {fM:.8f} \t {T:.2f} \t {T_rel:.2f} \t {uncompensated_thickness_nm:.4f} \t {compensated_thickness_nm:.4f}")
                with open('output.csv', mode='a') as log_file:
                    log_file.write(f"{calendar.timegm(time.gmtime())},{fT},{fM},{T},{uncompensated_thickness_nm},{compensated_thickness_nm}\n")
            else:
                print(f"{T:.2f} \t {compensated_thickness_nm:.4f}")
                with open('output.csv', mode='a') as log_file:
                    log_file.write(f"{T},{compensated_thickness_nm}\n")
                    
            time.sleep(0.1) # <-- set measurement interval here
    except KeyboardInterrupt:
        print("\nMeasurement stopped by user")
        

 
 
 
 
 
### STARTUP AUTOMATON (TEMP)
 
setFreq(1,3772000)                                                                                                                                                      

setFreq(2,10100000)                                                                                                                                                     

setGain(1,0.00000001)                                                                                                                                                   

setGain(2,0.00000001) 
 
 
 
 
 
 
 
 
 
 
 
