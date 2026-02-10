#!/usr/bin/env python3
import sys
import subprocess
from pathlib import Path

# ---- Resolve project root ----
PROJECT_ROOT = Path(__file__).resolve().parent

# ---- Configuration ----
VENV_DIR = PROJECT_ROOT / ".venv-server"
SERVER_ENTRY = PROJECT_ROOT / "src_server" / "server.py"

# ---- Resolve python inside venv ----
if sys.platform.startswith("win"):
    PYTHON = VENV_DIR / "Scripts" / "python.exe"
else:
    PYTHON = VENV_DIR / "bin" / "python"

# ---- Sanity checks ----
if not PYTHON.exists():
    print("ERROR: Virtual environment Python not found:")
    print(f"  {PYTHON}")
    print("\nCreate it with:")
    print("  python -m venv .venv-server")
    print("  pip install -r src_server/requirements.txt")
    sys.exit(1)

if not SERVER_ENTRY.exists():
    print("ERROR: Server entry point not found:")
    print(f"  {SERVER_ENTRY}")
    sys.exit(1)

# ---- Run server ----
print("Starting server")
print(f"  Python: {PYTHON}")
print(f"  Entry : {SERVER_ENTRY}")
print()

subprocess.run([str(PYTHON), str(SERVER_ENTRY), *sys.argv[1:]], check=True)
