"""Demo: drive the QCM Red Pitaya from a Python script.

Run the control software on the Red Pitaya first, then point this at it:

    python tools/qcm_demo.py 192.168.1.50

Needs: pip install requests websocket-client
"""

import sys
import time

from qcm_client import QCMClient


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    qcm = QCMClient(host)

    print(f"Connecting to {qcm.base} …")
    print("Current state:", qcm.state())

    # 1) Inspect / configure via REST (these are the same calls the web UI makes).
    print("Active crystal:", qcm.crystals().get("active"))
    qcm.set_lpf_freq(1, 200)          # demodulator LPF cutoff (Hz)
    qcm.set_lock_detect(amp_threshold=0.1, phase_tolerance=0.05)

    # 2) Lock the PLLs, then start a measurement.
    print("Locking …")
    qcm.get_lock()
    time.sleep(2)
    print("State after lock:", qcm.state())

    qcm.start_measurement(ambient_temp=23.0, mat_dens=19320.0, z_ratio=0.381)

    # 3) Stream a few live measurements over the WebSocket.
    print("\nStreaming 10 measurements:")
    print(f"{'thickness (nm)':>16} {'temp (°C)':>12} {'mass f (Hz)':>14} {'lock':>6}")
    try:
        for m in qcm.stream_measurements(limit=10):
            locked = "yes" if (m.lock_mass and m.lock_temp) else "no"
            print(f"{m.calculated_thickness:16.4f} {m.calculated_temp:12.3f} "
                  f"{m.freq_mass_mode:14.2f} {locked:>6}")
    except RuntimeError as e:
        print(f"(streaming unavailable: {e})")

    # 4) Stop and grab the complete server-side CSV.
    qcm.stop_measurement()
    try:
        path = qcm.download_latest_run("qcm_demo_run.csv")
        print(f"\nSaved server-side run to {path}")
    except Exception as e:
        print(f"(could not download run: {e})")

    # 5) Show the OPC node tree (so it can be mirrored on a PLC's OPC server).
    nodes = qcm.opc_nodes()
    print(f"\nOPC base node: {nodes['base_node_path']}")
    for n in nodes["read"] + nodes["ctrl"]:
        print(f"  [{n['direction']}] {n['node_id']}")


if __name__ == "__main__":
    main()
