#!/usr/bin/env python3
import queue
import sys
import os
import threading
import logging
import json
import datetime

import requests
import ratelimitqueue
from flask import Flask, request, jsonify
from waitress import serve

# Global constants
PD_EVENTS_API = "https://events.pagerduty.com/v2/enqueue"
PD_RATE_LIMIT_CALLS_PER = 60
PD_RATE_LIMIT_CALLS_COUNT = 120

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s | %(levelname)s | %(module)s | %(message)s"
)
logging.Formatter.formatTime = (
    lambda self, record, datefmt: datetime.datetime.fromtimestamp(
        record.created, datetime.timezone.utc
    )
    .astimezone()
    .isoformat()
)


class PDEventHandler:
    def __init__(self):
        self.logger = logging.getLogger()
        self.hostname = os.environ.get("HOSTNAME")
        self.routing_key = os.environ.get("ROUTING_KEY")
        self.session = requests.Session()
        self.rlq = ratelimitqueue.RateLimitQueue(
            calls=PD_RATE_LIMIT_CALLS_COUNT, per=PD_RATE_LIMIT_CALLS_PER
        )
        self.rlq_thread = threading.Thread(
            name="ratelimitqueue", target=self.__process_queue
        )

        # Additional initialisation routines
        self.__verify_routing_key()

    # Verify if PD routing key is valid via dummy resolve message; exit application if invalid
    def __verify_routing_key(self):
        try:
            res = self.session.post(
                url=PD_EVENTS_API,
                json={
                    "routing_key": self.routing_key,
                    "dedup_key": "pd_event_handler",
                    "event_action": "resolve",
                },
            )
            if res.status_code != 202:
                raise RuntimeError()
            else:
                self.logger.info("Routing key verified")
        except RuntimeError:
            self.logger.critical("Invalid routing key provided - terminating server")
            sys.exit(1)

    # Function to send queued events via PD Events v2 API
    def __pd_send_event(self, pd_event_data):
        # Take existing payload and dynamically update the correct routing key
        pd_event_data["routing_key"] = self.routing_key
        self.logger.info("Sending PD event: %s", pd_event_data)

        # Attempt sending event to PD; failed requests will be sent back to top of queue
        res = None
        try:
            res = self.session.post(url=PD_EVENTS_API, json=pd_event_data)
            self.logger.info("PD server response: %s", res.json())
        except requests.exceptions.RequestException as e:
            self.logger.warning(
                "Unable to process request (%s) - pushing to the back of the queue",
                e,
            )
            self.rlq.put(pd_event_data)
        except json.decoder.JSONDecodeError:
            self.logger.warning(
                "Unable to process request (Events API Status Code: %s) - pushing to the back of the queue",
                res.status_code,
            )
            self.rlq.put(pd_event_data)

    # Internal function to handle RLQ using background thread
    def __process_queue(self):
        self.logger.info("Queue is empty - currently awaiting requests")

        while True:
            if self.rlq.qsize() > 0:
                self.logger.info("Current queue size: %s", self.rlq.qsize())

            try:
                pd_event_data = self.rlq.get(timeout=PD_RATE_LIMIT_CALLS_PER * 2)
                self.__pd_send_event(pd_event_data)
                self.rlq.task_done()
            except queue.Empty:
                self.logger.info("Queue is empty - still awaiting requests")
            except Exception as e:
                self.logger.critical(e)

    # Handler entrypoint
    def start(self):
        # Initialise Flask server
        app = Flask(__name__)

        @app.route("/health", methods=["GET"])
        def __health():
            if self.rlq_thread.isAlive():
                return ("OK", 200)

            return ("Not alive", 500)

        # Default route for enqueuing requests
        @app.route("/", methods=["POST"])
        def __enqueue_request():
            pd_event_data = request.get_json()
            self.rlq.put(pd_event_data)
            self.logger.info("Enqueued event: %s", pd_event_data)
            return (
                jsonify(
                    {
                        "status": "enqueued",
                        "data": pd_event_data,
                        "target_routing_key": self.routing_key,
                    },
                ),
                202,
            )

        # Start RLQ and Flask server (under WSGI framework)
        self.rlq_thread.start()
        serve(app, host="0.0.0.0", port=5000)


# Application entrypoint
if __name__ == "__main__":
    pd_event_handler = PDEventHandler()
    pd_event_handler.start()
