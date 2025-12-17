import casperfpga
import os
import time


directory = os.path.expanduser(
    "~/work/CASPER_repos/qcm_red_pitaya/qcm_rp/outputs/"
)

newest_file = max(
	(os.path.join(directory, f) for f in os.listdir(directory)),
	key=os.path.getmtime
)


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

def setFreq(target, osc_index, freq):
    target.write_int(device_name='freq_'+str(osc_index),integer=int(freq*64))

def setGain(target, gain):
    target.write_int(device_name='filter_gain',integer=int(gain*4294967296))

def setInv(target, inv):
    target.write('inv_fb',(inv).to_bytes(4,'big'))

def reset(target):
    target.write('reset',(1).to_bytes(4,'big'))
    time.sleep(0.001)
    target.write('reset',(0).to_bytes(4,'big'))

fpga = casperfpga.CasperFpga('rp-f0ea58.local')



print("Newest file", newest_file)

fpga.upload_to_ram_and_program(newest_file)

