#!/usr/bin/env python3
"""Tests for the Redfish shim's routing, auth, and power-state mapping.

The EC2 layer is stubbed, so these run anywhere -- no AWS account, no
credentials.  Run with: python3 -m unittest discover -s tools/redfish-ec2
"""

import base64
import json
import threading
import unittest
import urllib.error
import urllib.request

import redfish_ec2


class StubPower:
    """Stands in for Ec2Power, recording the actions asked of it."""

    def __init__(self):
        self.states = {"master-0": "running", "master-1": "stopped"}
        self.calls = []

    def state(self, system_id):
        return self.states[system_id]

    def power_state(self, system_id):
        return "Off" if self.state(system_id) in redfish_ec2.POWERED_OFF_STATES else "On"

    def start(self, system_id):
        self.calls.append(("start", system_id))
        self.states[system_id] = "pending"

    def stop(self, system_id, force=True):
        self.calls.append(("stop", system_id, force))
        self.states[system_id] = "stopping"


class ShimTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = redfish_ec2.Config(
            {
                "region": "us-west-2",
                "username": "admin",
                "password": "secret",
                "systems": {
                    "master-0": {"name_tag": "tnf-gp-master-0"},
                    "master-1": {"name_tag": "tnf-gp-master-1"},
                },
            }
        )
        cls.power = StubPower()
        cls.httpd = redfish_ec2.make_server(
            cls.config, "127.0.0.1", 0, "", "", use_tls=False, power=cls.power
        )
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def request(self, path, method="GET", body=None, auth=("admin", "secret"),
                token=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data:
            req.add_header("Content-Type", "application/json")
        if auth:
            raw = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
            req.add_header("Authorization", f"Basic {raw}")
        if token:
            req.add_header("X-Auth-Token", token)
        try:
            with urllib.request.urlopen(req) as response:
                return response.status, json.loads(response.read()), dict(response.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read()), dict(exc.headers)

    # -- auth -------------------------------------------------------------

    def test_unauthenticated_request_is_rejected(self):
        status, _, _ = self.request("/redfish/v1/Systems/master-0", auth=None)
        self.assertEqual(status, 401)

    def test_wrong_password_is_rejected(self):
        status, _, _ = self.request(
            "/redfish/v1/Systems/master-0", auth=("admin", "wrong")
        )
        self.assertEqual(status, 401)

    def test_session_token_is_accepted(self):
        status, _, headers = self.request(
            "/redfish/v1/SessionService/Sessions", method="POST", body={}
        )
        self.assertEqual(status, 201)
        token = headers["X-Auth-Token"]
        status, payload, _ = self.request(
            "/redfish/v1/Systems/master-0", auth=None, token=token
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["PowerState"], "On")

    def test_healthz_needs_no_credentials(self):
        status, payload, _ = self.request("/healthz", auth=None)
        self.assertEqual(status, 200)
        self.assertEqual(payload["systems"], ["master-0", "master-1"])

    # -- discovery --------------------------------------------------------

    def test_service_root_links_to_systems(self):
        status, payload, _ = self.request("/redfish/v1/")
        self.assertEqual(status, 200)
        self.assertEqual(payload["Systems"]["@odata.id"], "/redfish/v1/Systems")

    def test_systems_collection_lists_both_nodes(self):
        status, payload, _ = self.request("/redfish/v1/Systems")
        self.assertEqual(status, 200)
        self.assertEqual(payload["Members@odata.count"], 2)

    def test_unknown_system_is_404(self):
        status, _, _ = self.request("/redfish/v1/Systems/master-9")
        self.assertEqual(status, 404)

    # -- power state ------------------------------------------------------

    def test_running_instance_reports_on(self):
        _, payload, _ = self.request("/redfish/v1/Systems/master-0")
        self.assertEqual(payload["PowerState"], "On")

    def test_stopped_instance_reports_off(self):
        _, payload, _ = self.request("/redfish/v1/Systems/master-1")
        self.assertEqual(payload["PowerState"], "Off")

    def test_transitional_states_report_on(self):
        """A stopping node must never be reported Off -- that is the split
        brain the fencing exists to prevent."""
        for state in ("pending", "stopping", "shutting-down", "rebooting"):
            with self.subTest(state=state):
                self.power.states["master-0"] = state
                _, payload, _ = self.request("/redfish/v1/Systems/master-0")
                self.assertEqual(payload["PowerState"], "On")
        self.power.states["master-0"] = "running"

    def test_reset_action_is_advertised_on_the_system(self):
        _, payload, _ = self.request("/redfish/v1/Systems/master-0")
        target = payload["Actions"]["#ComputerSystem.Reset"]["target"]
        self.assertEqual(
            target, "/redfish/v1/Systems/master-0/Actions/ComputerSystem.Reset"
        )

    # -- power actions ----------------------------------------------------

    def _reset(self, system_id, reset_type):
        return self.request(
            f"/redfish/v1/Systems/{system_id}/Actions/ComputerSystem.Reset",
            method="POST",
            body={"ResetType": reset_type},
        )

    def test_force_off_issues_a_forced_stop(self):
        self.power.calls.clear()
        status, _, _ = self._reset("master-0", "ForceOff")
        self.assertEqual(status, 200)
        self.assertEqual(self.power.calls, [("stop", "master-0", True)])
        self.power.states["master-0"] = "running"

    def test_on_issues_a_start(self):
        self.power.calls.clear()
        status, _, _ = self._reset("master-1", "On")
        self.assertEqual(status, 200)
        self.assertEqual(self.power.calls, [("start", "master-1")])
        self.power.states["master-1"] = "stopped"

    def test_graceful_shutdown_is_not_forced(self):
        self.power.calls.clear()
        self._reset("master-0", "GracefulShutdown")
        self.assertEqual(self.power.calls, [("stop", "master-0", False)])
        self.power.states["master-0"] = "running"

    def test_force_restart_stops_rather_than_rebooting(self):
        """EC2 RebootInstances is guest-cooperative and a hung node ignores it,
        so a restart has to become a forced stop."""
        self.power.calls.clear()
        self._reset("master-0", "ForceRestart")
        self.assertEqual(self.power.calls, [("stop", "master-0", True)])
        self.power.states["master-0"] = "running"

    def test_unsupported_reset_type_is_rejected(self):
        self.power.calls.clear()
        status, _, _ = self._reset("master-0", "Nmi")
        self.assertEqual(status, 400)
        self.assertEqual(self.power.calls, [])


class ConfigTestCase(unittest.TestCase):
    def test_password_is_required(self):
        with self.assertRaises(ValueError):
            redfish_ec2.Config({"region": "us-west-2", "systems": {"a": {"name_tag": "x"}}})

    def test_systems_are_required(self):
        with self.assertRaises(ValueError):
            redfish_ec2.Config({"region": "us-west-2", "password": "p", "systems": {}})

    def test_system_needs_an_instance_selector(self):
        with self.assertRaises(ValueError):
            redfish_ec2.Config(
                {"region": "us-west-2", "password": "p", "systems": {"a": {}}}
            )


if __name__ == "__main__":
    unittest.main()
