import queue
import signal
import threading
import time
from IPython import start_ipython

from domain.fpga_interface import FPGAInterface
from domain.qcm_interface import QCMInterface
from workers.qcm_worker import QCMWorker
from app.application import Application
# from api.server import RestServer
# from opcua.client import OPCUAClient
# from app.state import SystemState
from messaging.worker_command import *
from messaging.worker_event import *


def main():

    print("Starting QCM system")

    # ==================================================
    # Shared infrastructure
    # ==================================================

    worker_command_queue = queue.Queue()
    worker_event_queue = queue.Queue()

    app_command_queue = queue.Queue()
    app_event_queue = queue.Queue()

    # system_state = SystemState()

    # ==================================================
    # Hardware layer
    # ==================================================

    fpga = FPGAInterface()

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
        worker_command_queue=worker_command_queue,
        worker_event_queue=worker_event_queue,
        system_state=None # placeholder - we can add a shared state object later if needed
    )

    application.run()

    # ==================================================
    # OPC UA
    # ==================================================

    # opcua_client = OPCUAClient(
    #     command_queue=app_command_queue,
    #     event_queue=app_event_queue,
    #     system_state=system_state
    # )

    # opcua_client.start()

    # ==================================================
    # REST API
    # ==================================================

    # rest_server = RestServer(
    #     command_queue=app_command_queue,
    #     event_queue=app_event_queue,
    #     system_state=system_state
    # )

    # rest_server.start()

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

        start_ipython(
            argv=[],
            user_ns={
                "qcm": qcm,
                "fpga": fpga,
                "worker": qcm_worker,
                "worker_command_queue": worker_command_queue,
                "worker_event_queue": worker_event_queue,
                "app_command_queue": app_command_queue,
                "app_event_queue": app_event_queue,
            }
        )

    finally:

        print("Stopping system")

        qcm_worker.stop()
        qcm_worker.join()

        print("Shutdown complete")


if __name__ == "__main__":
    main()