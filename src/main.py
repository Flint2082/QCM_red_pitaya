import queue
import signal
import threading
import time

from domain.fpga_interface import FPGAInterface

from domain.qcm_interface import QCMInterface

from workers.qcm_worker import QCMWorker

from app.application import Application

from api.server import RestServer

from opcua.client import OPCUAClient

from app.state import SystemState


def main():

    print("Starting QCM system")

    # ==================================================
    # Shared infrastructure
    # ==================================================

    worker_command_queue = queue.Queue()
    worker_event_queue = queue.Queue()

    app_command_queue = queue.Queue()
    app_event_queue = queue.Queue()

    system_state = SystemState()

    # ==================================================
    # Hardware layer
    # ==================================================

    fpga = FPGAInterface()
    if not fpga.load_bitstream(): # TODO: define bitstream path as a parameter or config
        print("Failed to load bitstream")
        exit(1)

    # ==================================================
    # QCM domain
    # ==================================================

    qcm = QCMInterface(fpga)

    # ==================================================
    # QCM worker thread
    # ==================================================

    qcm_worker = QCMWorker(
        qcm=qcm,
        command_queue=worker_command_queue,
        event_queue=worker_event_queue
    )

    qcm_worker.start()

    # ==================================================
    # Application layer
    # ==================================================

    application = Application(
        command_queue=app_command_queue,
        event_queue=app_event_queue,
        system_state=system_state
    )

    application.start()

    # ==================================================
    # OPC UA
    # ==================================================

    opcua_client = OPCUAClient(
        command_queue=app_command_queue,
        event_queue=app_event_queue,
        system_state=system_state
    )

    opcua_client.start()

    # ==================================================
    # REST API
    # ==================================================

    rest_server = RestServer(
        command_queue=app_command_queue,
        event_queue=app_event_queue,
        system_state=system_state
    )

    rest_server.start()

    # ==================================================
    # Shutdown handling
    # ==================================================

    shutdown = False

    def handle_shutdown(signum, frame):
        nonlocal shutdown

        print("Shutdown requested")

        shutdown = True

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    # ==================================================
    # Main loop
    # ==================================================

    try:

        while not shutdown:

            time.sleep(1)

    finally:

        print("Stopping system")

        rest_server.stop()

        opcua_client.stop()

        application.stop()

        qcm_worker.stop()

        qcm_worker.join()

        fpga.disconnect()

        print("Shutdown complete")


if __name__ == "__main__":
    main()