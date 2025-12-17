#!/bin/bash

# === SETTINGS ===
PROJECT_DIR="$HOME/work/CASPER_repos/qcm_red_pitaya"
TARGET_DIR=PROJECT_DIR + "/qcm_rp/myproj/myproj.runs/impl_1"
REMOTE_USER="root"
REMOTE_HOST="rp-f0ea58.local"
REMOTE_PATH="/root"
BITFILE="top.bit"
BIFFILE="top.bif"
OUTPUT_BIN="top.bit.bin"
VENV_DIR="$HOME/work/cfpga_venv"
PYTHON_SCRIPT="$HOME/work/CASPER_repos/qcm_red_pitaya/startup.py"

# === SCRIPT START ===

echo "📂 Navigating to implementation directory..."
cd "$TARGET_DIR" || { echo "Directory not found: $TARGET_DIR"; exit 1; }

echo "📝 Creating BIF file..."
echo -n "all:{ $BITFILE }" > "$BIFFILE"

echo "⚙️ Running Bootgen..."
bootgen -image "$BIFFILE" -arch zynq -process_bitstream bin -o "$OUTPUT_BIN" -w

echo "📤 Uploading to Red Pitaya..."
scp "$OUTPUT_BIN" "$REMOTE_USER@$REMOTE_HOST:$REMOTE_PATH"

ssh root@rp-f0ea58.local << EOF
echo "Logged in automatically"
fpgautil -b /root/$OUTPUT_BIN
echo "Activated program"
EOF

cd PROJECT_DIR

echo "Activating CASPERFPGA venv"
source "$VENV_DIR/bin/activate"

echo "Starting IPython session"
ipython -i "$PYTHON_SCRIPT"
