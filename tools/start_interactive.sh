#!/bin/bash

# Launch the client side application

# Set the script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Change to the script directory
cd "$SCRIPT_DIR"


# Check if the top.bit.bin exists
if [ ! -f "$HOME/top.bit.bin" ]; then
    echo "Error: top.bit.bin not found"
    exit 1
fi

# Load FPGA bitstream 
fpgautil -b $HOME/top.bit.bin


# Check if client exists
if [ ! -f "src/interactive.py" ]; then
    echo "Error: client file interactive.py not found"
    exit 1
fi

# Start the client
echo "Starting interactive environment ..."

.venv-rp/bin/python3 src/interactive.py