#!/bin/bash

# Launch the client side application

# Set the script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Change to the script directory
cd "$SCRIPT_DIR"

# Echo the current directory
echo "Current directory: $(pwd)"

# Echo the provided red pitaya address
if [ -z "$1" ]; then
    echo "Usage: $0 <red_pitaya_ip_address>"
    exit 1
fi


# Check if the top.bit.bin exists
if [ ! -f "$HOME/top.bit.bin" ]; then
    echo "Error: top.bit.bin not found"
    exit 1
fi

# Load FPGA bitstream 
fpgautil -b $HOME/top.bit.bin


# Check if client exists
if [ ! -f "$SCRIPT_DIR/../src/interactive.py" ]; then
    echo "Error: client file interactive.py not found"
    exit 1
fi

# Start the client
echo "Starting interactive environment ..."

$SCRIPT_DIR/../.venv-rp/bin/python3 $SCRIPT_DIR/../src/interactive.py "$1"