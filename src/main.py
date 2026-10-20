import queue
import signal
import threading
import time
import argparse

from domain.fpga_interface import FPGAInterface
from domain.qcm_interface import QCMInterface
from domain.version import VersionInfo
from workers.qcm_worker import QCMWorker
from app.application import Application
from api.server import RestServer
from plc.wago_client import WagoClient
from plc.opc_worker import OPCUAWorker


def build_system(enable_opcua: bool = True):
    """Instantiate and wire up all system components. Returns a dict of named objects."""

    worker_command_queue = queue.Queue()
    worker_event_queue = queue.Queue()

    api_command_queue = queue.Queue()
    api_event_queue = queue.Queue()

    fpga = FPGAInterface()
    # load_bitstream reports every outcome as a dict, so the old `if not ...`
    # was false even for {"status": "error"} — a non-empty dict is truthy. A
    # failed load therefore started the server anyway, and the web interface
    # came up against whatever logic happened to already be in the FPGA: the
    # register map is read from the .fpg file rather than the hardware, so
    # every write lands somewhere plausible-looking and nothing says why the
    # instrument does not respond. Check the status the call actually returns.
    result = fpga.load_bitstream()
    if result.get("status") != "success":
        reason = result.get("stderr") or result.get("message") or "unknown error"
        print(f"FATAL: bitstream not loaded, refusing to start — {reason.strip()}")
        exit(1)

    qcm = QCMInterface(fpga)

    # What this process is running: the commit it started from and the
    # bitstream just loaded. Taken here, straight after the load, so the files it
    # describes are the ones that went into the FPGA. See domain/version.py.
    version = VersionInfo(fpga)
    print(f"INFO: software {version.software.name()}, bitstream {version.fpga['fpg']}")

    qcm_worker = QCMWorker(
        qcm=qcm,
        command_queue=worker_command_queue,
        event_queue=worker_event_queue,
        version=version,
    )

    # OPC-UA bridge (optional — system works without a PLC)
    opc_worker = None
    opc_command_queue = None
    opc_event_queue = None
    opc_status_queue = None
    if enable_opcua:
        opc_command_queue = queue.Queue()
        opc_event_queue   = queue.Queue()
        opc_status_queue  = queue.Queue()
        wago = WagoClient(auto_connect=True)
        opc_worker = OPCUAWorker(
            client=wago,
            command_queue=opc_command_queue,
            event_queue=opc_event_queue,
            status_queue=opc_status_queue,
        )

    application = Application(
        worker_command_queue=worker_command_queue,
        worker_event_queue=worker_event_queue,
        api_command_queue=api_command_queue,
        api_event_queue=api_event_queue,
        opc_command_queue=opc_command_queue,
        opc_event_queue=opc_event_queue,
        opc_status_queue=opc_status_queue,
    )

    rest_server = RestServer(
        api_command_queue=api_command_queue,
        api_event_queue=api_event_queue,
        wago_client=wago if enable_opcua else None,
        version=version,
    )
    # OPC-triggered actions take their parameters from the REST settings.
    if opc_worker:
        opc_worker.set_control(rest_server)
    rest_server.start()

    system = dict(
        fpga=fpga,
        qcm=qcm,
        version=version,
        qcm_worker=qcm_worker,
        application=application,
        rest_server=rest_server,
        worker_command_queue=worker_command_queue,
        worker_event_queue=worker_event_queue,
        api_command_queue=api_command_queue,
        api_event_queue=api_event_queue,
        opc_command_queue=opc_command_queue,
        opc_event_queue=opc_event_queue,
        opc_status_queue=opc_status_queue,
    )
    if opc_worker:
        system["opc_worker"] = opc_worker

    return system


def run_dev_shell(system: dict):
    """Drop into an IPython shell with all system objects in scope."""
    try:
        import IPython
    except ImportError:
        print("IPython not installed. Run: pip install ipython")
        exit(1)

    print("\n=== QCM Dev Shell ===")
    print("Available objects:", ", ".join(system.keys()))
    print("Hardware is initialised but worker/application threads are NOT started.")
    print("Start them manually if needed:  qcm_worker.start() / application.start()")
    print("=====================\n")

    IPython.start_ipython(argv=[], user_ns=system)


def run_normal(system: dict):
    """Start worker + application threads and run the main loop."""

    system["qcm_worker"].start()
    system["application"].start()
    if "opc_worker" in system:
        system["opc_worker"].start()

    shutdown = False

    def handle_shutdown(_signum, _frame):
        nonlocal shutdown
        print("Shutdown requested")
        shutdown = True

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    try:
        while not shutdown:
            time.sleep(1)
    finally:
        print("Stopping system")
        # Stop the REST server first — prevents new WebSocket/HTTP connections
        # while the rest of the system is shutting down
        system["rest_server"].stop()
        if "opc_worker" in system:
            system["opc_worker"].stop()
            system["opc_worker"].join()
        system["application"].stop()
        system["qcm_worker"].stop()
        system["qcm_worker"].join()
        print("Shutdown complete")


def main():
    parser = argparse.ArgumentParser(description="QCM system")
    parser.add_argument(
        "--dev",
        action="store_true",
        help="Start an interactive IPython shell instead of the main loop"
    )
    parser.add_argument(
        "--no-opcua",
        action="store_true",
        help="Disable OPC-UA PLC bridge"
    )
    args = parser.parse_args()

    print("Starting QCM system")
    system = build_system(enable_opcua=not args.no_opcua)

    if args.dev:
        run_dev_shell(system)
    else:
        run_normal(system)


if __name__ == "__main__":
    main()