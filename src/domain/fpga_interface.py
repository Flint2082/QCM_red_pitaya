import subprocess
import os
import mmap
import struct

# Base address (must be page-aligned!)
BASE_ADDR = 0x40000000 # TODO: make this automatically determined based on the .fpg file
PAGE_SIZE = mmap.PAGESIZE
MAP_SIZE = PAGE_SIZE
BITSTREAM_PATH = os.path.join("/root", "top.bit.bin")

class FPGAInterface:
    def __init__(self, base_addr=BASE_ADDR, map_size=MAP_SIZE, bitstream_path=BITSTREAM_PATH):
        self.base_addr = base_addr
        self.map_size = map_size
        self.BITSTREAM_PATH = bitstream_path

        with open("/dev/mem", "r+b") as f:
            self.mem = mmap.mmap(
                f.fileno(),
                self.map_size,
                offset=self.base_addr
            )
            
    def load_bitstream(self):
        print(f"INFO: Loading bitstream from {self.BITSTREAM_PATH}...")
        try:
            result = subprocess.run(
                ["/opt/redpitaya/bin/fpgautil", "-b", self.BITSTREAM_PATH],
                capture_output=True,
                text=True,
                timeout=10
            )

            if result.returncode != 0:
                print(f"ERROR: Failed to load bitstream: {result.stderr}")
                return {"status": "error", "stderr": result.stderr}
            print(f"INFO: Bitstream loaded successfully: {result.stdout}")
            return {"status": "success", "stdout": result.stdout}

        except Exception as e:
            print(f"ERROR: Failed to load bitstream: {e}")
            return {"status": "error","message": str(e)} 
            

    def test_fpga_interface(self, test_register):
        try:
            # Attempt to read from the first register (assuming it's a known register)
            test_value = self.read_register(test_register)  # Replace with an actual register name from your map
            self.write_register(test_register, test_value+1)  # Try writing back a modified value
            self.write_register(test_register, test_value)  # Write the original value back
            return True
        except Exception as e:
            print(f"Test failed: {e}")
            return False
    
    # Parse the .fpg file to get register names and offsets, and store in a dictionary
    def load_register_map(self, register_map_dir, debug=False):
        try:
            with open(register_map_dir, "r", errors = "ignore") as f:
                self.register_map = {}
                for line in f:
                    if debug:
                        print(line)
                    if line.strip() and line.startswith("?register"):
                        data = line.split("\t")
                        self.register_map[data[1].strip()] = int(data[2].strip(), 0)
                    elif line.strip() and line.startswith("?quit"):
                        if debug:
                            print("Successfully finished parsing")
                        break
            return True
        except Exception as e:
            print(f"Error loading register map: {e}")
            return False
    
    # Parse the .fpg file to get the FPGA clock frequency from the metadata
    # ?meta	RED_PITAYA1	xps:xsg	clk_rate	125   
    def get_clock_freq(self, register_map_dir):
        try:
            with open(register_map_dir, "r", errors = "ignore") as f:
                for line in f:
                    if line.strip() and line.startswith("?meta"):
                        data = line.split("\t")
                        if data[3].strip() == "clk_rate":
                            return int(data[4].strip()) * 1e6  # Convert MHz to Hz
            raise ValueError("Clock frequency not found in register map file")
        except Exception as e:
            print(f"Error getting clock frequency: {e}")
            return None
            
    def show_register_map(self):
        for reg_name, reg_addr in self.register_map.items():
            print(f"{reg_name}: {hex(reg_addr)}")        
        
    def write_register(self, register_name, value):
        if register_name not in self.register_map:
            raise ValueError(f"Register {register_name} not found in register map")
        mem_loc = self.register_map[register_name]
        relative_loc = mem_loc - self.base_addr
        self.mem[relative_loc:relative_loc+4] = struct.pack("<I", value)
        
        # checking that the value was written correctly
        read_value = self.read_register(register_name)
        if read_value != value:
            raise ValueError(f"Value {value} was not written correctly to register {register_name}, read back {read_value}")
        else:
            return True

    def read_register(self, register_name):
        if register_name not in self.register_map:
            raise ValueError(f"Register {register_name} not found in register map")
        mem_loc = self.register_map[register_name]
        relative_loc = mem_loc - self.base_addr
        return struct.unpack("<I", self.mem[relative_loc:relative_loc+4])[0]
    
    