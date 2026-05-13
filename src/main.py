import queue
import signal
import threading
import time
import argparse

from domain.fpga_interface import FPGAInterface
from domain.qcm_interface import QCMInterface
from workers.qcm_worker import QCMWorker
from app.application import Application
from api.server import RestServer
# from opcua.client import OPCUAClient
# from app.state import SystemState


def build_system():
    """Instantiate and wire up all system components. Returns a dict of named objects."""

    worker_command_queue = queue.Queue()
    worker_event_queue = queue.Queue()

    app_command_queue = queue.Queue()
    app_event_queue = queue.Queue()

    fpga = FPGAInterface()
    if not fpga.load_bitstream():
        print("Failed to load bitstream")
        exit(1)

    qcm = QCMInterface(fpga)

    qcm_worker = QCMWorker(
        qcm=qcm,
        command_queue=worker_command_queue,
        event_queue=worker_event_queue
    )

    application = Application(
        worker_command_queue=worker_command_queue,
        worker_event_queue=worker_event_queue,
        api_command_queue=app_command_queue,
        api_event_queue=app_event_queue,
    )
    
    rest_server = RestServer(
        app_command_queue=app_command_queue,
        app_event_queue=app_event_queue,
    )
    rest_server.start()

    return dict(
        fpga=fpga,
        qcm=qcm,
        qcm_worker=qcm_worker,
        application=application,
        rest_server=rest_server,
        worker_command_queue=worker_command_queue,
        worker_event_queue=worker_event_queue,
        app_command_queue=app_command_queue,
        app_event_queue=app_event_queue,
    )


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

    shutdown = False

    def handle_shutdown(signum, frame):
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
        # system["rest_server"].stop()
        # system["opcua_client"].stop()
        # system["application"].stop()
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
    args = parser.parse_args()

    print("Starting QCM system")
    system = build_system()

    if args.dev:
        run_dev_shell(system)
    else:
        run_normal(system)


if __name__ == "__main__":
    main()