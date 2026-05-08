import queue
from IPython import start_ipython

from domain.fpga_interface import FPGAInterface
from domain.qcm_interface import QCMInterface

from workers.qcm_worker import QCMWorker

from messaging.worker_command import *
from messaging.worker_event import *


# =========================================================
# Helper functions
# =========================================================

def print_events(event_queue):

    while not event_queue.empty():

        event = event_queue.get()

        print(event)


# =========================================================
# Main
# =========================================================

print("Starting QCM development shell")


# =========================================================
# Shared infrastructure
# =========================================================

worker_command_queue = queue.Queue()

worker_event_queue = queue.Queue()

app_command_queue = queue.Queue()

app_event_queue = queue.Queue()


# =========================================================
# Hardware layer
# =========================================================

fpga = FPGAInterface()

if not fpga.load_bitstream():

    print("Failed to load bitstream")

    exit(1)


# =========================================================
# QCM domain
# =========================================================

qcm = QCMInterface(fpga)


# =========================================================
# QCM worker
# =========================================================

qcm_worker = QCMWorker(
    qcm=qcm,
    command_queue=worker_command_queue,
    event_queue=worker_event_queue
)

qcm_worker.start()


# =========================================================
# Convenience helper functions
# =========================================================

# def sweep(
#     start_frequency=10e6,
#     stop_frequency=10.1e6,
#     step_size=1
# ):

#     worker_command_queue.put(
#         ExecuteSweepCommand(
#             start_frequency=start_frequency,
#             stop_frequency=stop_frequency,
#             step_size=step_size
#         )
#     )


def stop():
    pass
    # worker_command_queue.put(
    #     StopCommand()
    # )


def events():

    print_events(worker_event_queue)


# =========================================================
# Interactive shell
# =========================================================

banner = """
=========================================================
QCM Development Shell
=========================================================

Available objects:
    fpga
    qcm
    qcm_worker

Queues:
    worker_command_queue
    worker_event_queue

Helper functions:
    sweep()
    stop()
    events()

Example:
    sweep()

Exit shell with:
    exit
=========================================================
"""


try:

    start_ipython(
        argv=[],
        user_ns={
            "fpga": fpga,
            "qcm": qcm,
            "qcm_worker": qcm_worker,

            "worker_command_queue": worker_command_queue,
            "worker_event_queue": worker_event_queue,

            "app_command_queue": app_command_queue,
            "app_event_queue": app_event_queue,

            # "sweep": sweep,
            "stop": stop,
            "events": events,

            # command types
            # "ExecuteSweepCommand": ExecuteSweepCommand,
            # "StopCommand": StopCommand,
        },
        display_banner=True
    )

finally:

    print("\nStopping QCM system")

    qcm_worker.stop()

    qcm_worker.join()

    print("Shutdown complete")