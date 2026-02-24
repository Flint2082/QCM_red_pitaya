#!/bin/bash

# === SETTINGS ===
PROJECT_DIR="$HOME/work/CASPER_repos/qcm_red_pitaya"
TARGET_DIR="$PROJECT_DIR/model_composer/qcm_rp/myproj/myproj.runs/impl_1"
REMOTE_USER="root"
#REMOTE_HOST="rp-f0ea58.local"

REMOTE_HOST="132.229.46.164"
REMOTE_PATH="/root"
BITFILE="top.bit"
BIFFILE="top.bif"
OUTPUT_BIN="top.bit.bin"
VENV_DIR="$PROJECT_DIR/cfpga_venv"
PYTHON_SCRIPT="$PROJECT_DIR/src/interactive.py"

# === SCRIPT START ===

echo "📂 Navigating to implementation directory..."
cd "$TARGET_DIR" || { echo "Directory not found: $TARGET_DIR"; exit 1; }

echo "📝 Creating BIF file..."
echo -n "all:{ $BITFILE }" > "$BIFFILE" || { echo "Failed to create BIF file"; exit 1; }

echo "⚙️ Running Bootgen..."
bootgen -image "$BIFFILE" -arch zynq -process_bitstream bin -o "$OUTPUT_BIN" -w || { echo "Bootgen failed"; exit 1; }

echo "📤 Uploading to Red Pitaya..."
scp "$OUTPUT_BIN" "$REMOTE_USER@$REMOTE_HOST:$REMOTE_PATH" || { echo "SCP upload failed"; exit 1; }

ssh root@"$REMOTE_HOST" << EOF || { echo "SSH connection failed"; exit 1; }
echo "Logged in automatically"
fpgautil -b /root/$OUTPUT_BIN
echo "Activated program"
EOF

cd "$PROJECT_DIR" || { echo "Directory not found: $PROJECT_DIR"; exit 1; }

echo "Activating CASPERFPGA venv"
#source "$VENV_DIR/bin/activate" || { echo "Failed to activate virtual environment: $VENV_DIR"; exit 1; }

echo "Starting interactive session"

.venv-rp/bin/python3 "$PYTHON_SCRIPT" || { echo "Failed to start Python script: $PYTHON_SCRIPT"; exit 1; }