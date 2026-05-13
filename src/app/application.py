# Responsible for:

# orchestration
# state updates
# interpreting events
# job tracking
# coordination logic

import queue
import threading


class Application(threading.Thread):
    def __init__(self, worker_command_queue, worker_event_queue, system_state):
        super().__init__(daemon=True)

        self.command_queue = worker_command_queue
        self.event_queue = worker_event_queue
        self.system_state = system_state
        self.running = True

    def stop(self):
        self.running = False

    def run(self):
        print("Application started")

        while self.running:
            try:
                event = self.event_queue.get(timeout=0.1)
                print(f"Application received event: {event}")

            except queue.Empty:
                pass

        print("Application stopped")