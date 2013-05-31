import time
import os
import json
import logging
import re

from landscape.accumulate import Accumulator
from landscape.lib.monitor import CoverageMonitor
from landscape.lib.command import run_command, CommandError
from landscape.lib.persist import Persist
from landscape.manager.plugin import ManagerPlugin

ACCUMULATOR_KEY = "ceph-usage-accumulator"

EXP = re.compile(".*pgmap.*data, (\d+) MB used, (\d+) MB / (\d+) MB avail.*",
                 flags=re.S)


class CephUsage(ManagerPlugin):
    """
    Plugin that captures Ceph usage information. This only works if the client
    runs on one of the Ceph monitor nodes, and it noops otherwise.
    """
    persist_name = "ceph-usage"
    # Prevent the Plugin base-class from scheduling looping calls.
    run_interval = None

    def __init__(self, interval=30, exchange_interval=60 * 60,
                 create_time=time.time):
        self._interval = interval
        self._exchange_interval = exchange_interval
        self._ceph_usage_points = []
        self._ceph_ring_id = None
        self._create_time = create_time
        self._ceph_config = None

    def register(self, registry):
        super(CephUsage, self).register(registry)
        self._ceph_config = os.path.join(
            self.registry.config.data_path, "ceph-client",
            "ceph.landscape-client.conf")

        self.registry.reactor.call_every(self._interval, self.run)

        self._persist_filename = os.path.join(self.registry.config.data_path,
                                              "ceph.bpickle")
        self._persist = Persist(filename=self._persist_filename)

        self._accumulate = Accumulator(self._persist, self._interval)

        self._monitor = CoverageMonitor(self._interval, 0.8,
                                        "Ceph usage snapshot",
                                        create_time=self._create_time)
        self.registry.reactor.call_every(self._exchange_interval,
                                         self._monitor.log)
        self.registry.reactor.call_on("stop", self._monitor.log, priority=2000)
        self.call_on_accepted("ceph-usage", self.send_message, True)
        self.registry.reactor.call_on("resynchronize", self._resynchronize)
        self.registry.reactor.call_every(self.registry.config.flush_interval,
                                         self.flush)

    def _resynchronize(self):
        self._persist.remove(self.persist_name)

    def flush(self):
        self._persist.save(self._persist_filename)

    def create_message(self):
        ceph_points = self._ceph_usage_points
        ring_id = self._ceph_ring_id
        self._ceph_usage_points = []
        return {"type": "ceph-usage", "ceph-usages": ceph_points,
                "ring-id": ring_id}

    def send_message(self, urgent=False):
        message = self.create_message()
        if message["ceph-usages"] and message["ring-id"] is not None:
            self.registry.broker.send_message(message, urgent=urgent)

    def exchange(self, urgent=False):
        self.registry.broker.call_if_accepted("ceph-usage",
                                              self.send_message, urgent)

    def run(self):
        self._monitor.ping()

        # Check if a ceph config file is available. If it's not , it's not a
        # ceph machine or ceph is set up yet. No need to run anything in this
        # case.
        if self._ceph_config is None or not os.path.exists(self._ceph_config):
            return None

        # Extract the ceph ring Id and cache it.
        if self._ceph_ring_id is None:
            self._ceph_ring_id = self._get_ceph_ring_id()

        new_timestamp = int(self._create_time())
        new_ceph_usage = self._get_ceph_usage()

        step_data = None
        if new_ceph_usage is not None:
            step_data = self._accumulate(
                new_timestamp, new_ceph_usage, ACCUMULATOR_KEY)
        if step_data is not None:
            self._ceph_usage_points.append(step_data)

    def _get_ceph_usage(self):
        """
        Grab the ceph usage data by parsing the output of the "ceph status"
        command output.
        """
        output = self._get_status_command_output()

        if output is None:
            return None

        result = EXP.match(output)

        if not result:
            logging.error("Could not parse command output: '%s'." % output)
            return None

        (used, available, total) = result.groups()
        # Note: used + available is NOT equal to total (there is some used
        # space for duplication and system info etc...)

        filled = int(total) - int(available)

        return filled / float(total)

    def _get_status_command_output(self):
        return self._run_ceph_command("status")

    def _get_ceph_ring_id(self):
        output = self._get_quorum_command_output()
        if output is None:
            return None
        try:
            quorum_status = json.loads(output)
            ring_id = quorum_status["monmap"]["fsid"]
        except:
            logging.error(
                "Could not get ring_id from output: '%s'." % output)
            return None
        return ring_id

    def _get_quorum_command_output(self):
        return self._run_ceph_command("quorum_status")

    def _run_ceph_command(self, *args):
        """
        Run the ceph command with the specified options using landscape ceph
        key The keyring is expected to contain a key for the
        "client.landscape-client" id.
        """
        command = [
            "ceph", "--conf", self._ceph_config, "--id", "landscape-client"]
        command.extend(args)
        try:
            output = run_command(" ".join(command))
        except (OSError, CommandError):
            # If the command line client isn't available, we assume it's not a
            # ceph monitor machine.
            return None
        return output
