#!/usr/bin/env python3
"""A minimal Redfish endpoint that power-cycles AWS EC2 instances.

Two-Node OpenShift with Fencing drives Pacemaker's ``fence_redfish`` stonith
agent, and ``fence_redfish`` needs a Redfish service to talk to.  EC2 has no
BMC, so this process stands in for one: it serves just enough of the Redfish
schema for the fence agent to read power state and issue resets, and it
translates those into ``StartInstances`` / ``StopInstances`` calls.

Only three Redfish surfaces are implemented, because they are all the fence
agent touches:

    GET  /redfish/v1/                                          ServiceRoot
    GET  /redfish/v1/Systems                                   collection
    GET  /redfish/v1/Systems/<id>                              PowerState
    POST /redfish/v1/Systems/<id>/Actions/ComputerSystem.Reset  power action

Virtual media is deliberately absent.  Nodes in this toolbox boot from an
RHCOS AMI with ignition delivered through EC2 user-data, so nothing ever needs
to mount an ISO.

Systems are addressed by a stable logical id ("master-0"), not by EC2 instance
id.  That matters: the fencing addresses have to be written into
install-config.yaml *before* the instances exist, and a replaced instance keeps
working without an install-config edit.

State mapping is deliberately asymmetric.  A node is reported "Off" only when
EC2 says ``stopped`` or ``terminated``; every transitional state reports "On".
Reporting "Off" early would let Pacemaker conclude a fence succeeded while the
peer is still running, which is exactly the split brain fencing exists to
prevent.
"""

import argparse
import base64
import json
import logging
import os
import secrets
import ssl
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError:  # pragma: no cover - surfaced at startup, not import time
    boto3 = None
    ClientError = Exception

LOG = logging.getLogger("redfish-ec2")

# EC2 instance states that mean "this machine can still write to shared state".
# Anything not in POWERED_OFF_STATES is reported as On.
POWERED_OFF_STATES = {"stopped", "terminated"}


class Config:
    """Runtime configuration: which logical system maps to which EC2 instance."""

    def __init__(self, raw):
        self.region = raw.get("region") or os.environ.get("AWS_REGION")
        if not self.region:
            raise ValueError("region must be set in the config file or AWS_REGION")

        self.username = raw.get("username", "admin")
        self.password = raw.get("password")
        if not self.password:
            raise ValueError("password must be set in the config file")

        self.systems = raw.get("systems") or {}
        if not self.systems:
            raise ValueError("at least one entry is required under 'systems'")

        for system_id, spec in self.systems.items():
            if not spec.get("instance_id") and not spec.get("name_tag"):
                raise ValueError(
                    f"system '{system_id}' needs either 'instance_id' or 'name_tag'"
                )

    @classmethod
    def load(cls, path):
        with open(path, encoding="utf-8") as handle:
            return cls(json.load(handle))


class Ec2Power:
    """Resolves logical system ids to EC2 instances and drives their power."""

    def __init__(self, config):
        self._config = config
        self._client = boto3.client("ec2", region_name=config.region)
        # Instance ids are cached because the Name-tag lookup is an extra API
        # call on a path Pacemaker polls on every monitor interval.
        self._cache = {}
        self._lock = threading.Lock()

    def _resolve(self, system_id):
        spec = self._config.systems[system_id]
        if spec.get("instance_id"):
            return spec["instance_id"]

        with self._lock:
            if system_id in self._cache:
                return self._cache[system_id]

        name = spec["name_tag"]
        response = self._client.describe_instances(
            Filters=[
                {"Name": "tag:Name", "Values": [name]},
                # A terminated instance of the same name must not shadow the
                # live one, so filter the terminal states out of the lookup.
                {
                    "Name": "instance-state-name",
                    "Values": ["pending", "running", "stopping", "stopped"],
                },
            ]
        )
        instances = [
            i
            for reservation in response["Reservations"]
            for i in reservation["Instances"]
        ]
        if not instances:
            raise LookupError(f"no live EC2 instance tagged Name={name}")
        if len(instances) > 1:
            raise LookupError(
                f"{len(instances)} live EC2 instances tagged Name={name}; "
                "tag exactly one or pin 'instance_id' in the config"
            )

        instance_id = instances[0]["InstanceId"]
        with self._lock:
            self._cache[system_id] = instance_id
        LOG.info("resolved system %s to instance %s", system_id, instance_id)
        return instance_id

    def state(self, system_id):
        """Return the raw EC2 state name for a logical system."""
        instance_id = self._resolve(system_id)
        response = self._client.describe_instances(InstanceIds=[instance_id])
        for reservation in response["Reservations"]:
            for instance in reservation["Instances"]:
                return instance["State"]["Name"]
        raise LookupError(f"instance {instance_id} disappeared")

    def power_state(self, system_id):
        """Return the Redfish PowerState string for a logical system."""
        return "Off" if self.state(system_id) in POWERED_OFF_STATES else "On"

    def start(self, system_id):
        instance_id = self._resolve(system_id)
        LOG.warning("StartInstances %s (system %s)", instance_id, system_id)
        self._client.start_instances(InstanceIds=[instance_id])

    def stop(self, system_id, force=True):
        instance_id = self._resolve(system_id)
        LOG.warning(
            "StopInstances %s force=%s (system %s)", instance_id, force, system_id
        )
        self._client.stop_instances(InstanceIds=[instance_id], Force=force)


class SessionStore:
    """Redfish session tokens.

    ``fence_redfish`` authenticates with HTTP Basic in most builds, but some
    versions open a SessionService session first and then send ``X-Auth-Token``.
    Supporting both is a few lines and saves a confusing 401.
    """

    def __init__(self):
        self._tokens = {}
        self._lock = threading.Lock()

    def create(self, username):
        token = secrets.token_urlsafe(32)
        session_id = secrets.token_hex(8)
        with self._lock:
            self._tokens[token] = session_id
        return token, session_id

    def valid(self, token):
        with self._lock:
            return token in self._tokens

    def delete(self, session_id):
        with self._lock:
            for token, existing in list(self._tokens.items()):
                if existing == session_id:
                    del self._tokens[token]
                    return True
        return False


class RedfishHandler(BaseHTTPRequestHandler):
    """Routes the handful of Redfish requests a fence agent makes."""

    server_version = "redfish-ec2/1.0"
    protocol_version = "HTTP/1.1"

    # Injected by make_server()
    config = None
    power = None
    sessions = None

    # -- plumbing ---------------------------------------------------------

    def log_message(self, fmt, *args):
        LOG.debug("%s - %s", self.address_string(), fmt % args)

    def _send_json(self, status, payload, extra_headers=None):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("OData-Version", "4.0")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status, message):
        LOG.warning("%s %s -> %s: %s", self.command, self.path, status, message)
        self._send_json(
            status,
            {
                "error": {
                    "code": "Base.1.0.GeneralError",
                    "message": message,
                }
            },
        )

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError):
            return {}

    def _authenticated(self):
        token = self.headers.get("X-Auth-Token")
        if token and self.sessions.valid(token):
            return True

        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
            username, _, password = decoded.partition(":")
        except (ValueError, UnicodeDecodeError):
            return False
        # secrets.compare_digest keeps the comparison constant-time.
        return secrets.compare_digest(username, self.config.username) and \
            secrets.compare_digest(password, self.config.password)

    # -- routing ----------------------------------------------------------

    def do_GET(self):  # noqa: N802 - name mandated by BaseHTTPRequestHandler
        path = urlparse(self.path).path.rstrip("/") or "/"

        # Unauthenticated so an operator (or a monitoring probe) can confirm
        # the shim is alive without holding BMC credentials.
        if path == "/healthz":
            self._send_json(200, {"status": "ok", "systems": sorted(self.config.systems)})
            return

        if not self._authenticated():
            self._send_error(401, "authentication required")
            return

        if path == "/redfish/v1":
            self._send_json(200, self._service_root())
        elif path == "/redfish/v1/Systems":
            self._send_json(200, self._systems_collection())
        elif path.startswith("/redfish/v1/Systems/"):
            self._handle_system_get(path.rsplit("/", 1)[-1])
        else:
            self._send_error(404, f"no such resource: {path}")

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"

        if not self._authenticated():
            self._send_error(401, "authentication required")
            return

        if path == "/redfish/v1/SessionService/Sessions":
            token, session_id = self.sessions.create(self.config.username)
            self._send_json(
                201,
                {
                    "@odata.id": f"/redfish/v1/SessionService/Sessions/{session_id}",
                    "Id": session_id,
                    "Name": "User Session",
                    "UserName": self.config.username,
                },
                extra_headers={
                    "X-Auth-Token": token,
                    "Location": f"/redfish/v1/SessionService/Sessions/{session_id}",
                },
            )
            return

        if path.endswith("/Actions/ComputerSystem.Reset"):
            system_id = path.split("/redfish/v1/Systems/", 1)[-1].split("/", 1)[0]
            self._handle_reset(system_id)
            return

        self._send_error(404, f"no such action: {path}")

    def do_DELETE(self):  # noqa: N802
        path = urlparse(self.path).path.rstrip("/")
        if not self._authenticated():
            self._send_error(401, "authentication required")
            return
        if path.startswith("/redfish/v1/SessionService/Sessions/"):
            self.sessions.delete(path.rsplit("/", 1)[-1])
            self._send_json(200, {"status": "deleted"})
            return
        self._send_error(404, f"no such resource: {path}")

    # -- resources --------------------------------------------------------

    def _service_root(self):
        return {
            "@odata.id": "/redfish/v1/",
            "@odata.type": "#ServiceRoot.v1_5_0.ServiceRoot",
            "Id": "RootService",
            "Name": "redfish-ec2",
            "RedfishVersion": "1.6.0",
            "Systems": {"@odata.id": "/redfish/v1/Systems"},
            "SessionService": {"@odata.id": "/redfish/v1/SessionService"},
            "Links": {
                "Sessions": {"@odata.id": "/redfish/v1/SessionService/Sessions"}
            },
        }

    def _systems_collection(self):
        members = [
            {"@odata.id": f"/redfish/v1/Systems/{system_id}"}
            for system_id in sorted(self.config.systems)
        ]
        return {
            "@odata.id": "/redfish/v1/Systems",
            "@odata.type": "#ComputerSystemCollection.ComputerSystemCollection",
            "Name": "Computer System Collection",
            "Members": members,
            "Members@odata.count": len(members),
        }

    def _handle_system_get(self, system_id):
        if system_id not in self.config.systems:
            self._send_error(404, f"unknown system: {system_id}")
            return
        try:
            ec2_state = self.power.state(system_id)
        except (ClientError, LookupError) as exc:
            # 500 rather than a guessed state: the fence agent must retry, not
            # act on a power state we could not actually read.
            self._send_error(500, f"could not read EC2 state: {exc}")
            return

        power_state = "Off" if ec2_state in POWERED_OFF_STATES else "On"
        self._send_json(
            200,
            {
                "@odata.id": f"/redfish/v1/Systems/{system_id}",
                "@odata.type": "#ComputerSystem.v1_13_0.ComputerSystem",
                "Id": system_id,
                "Name": system_id,
                "PowerState": power_state,
                "Status": {
                    "State": "Enabled" if power_state == "On" else "StandbyOffline",
                    "Health": "OK",
                },
                # Surfaced for humans debugging a fence; not read by the agent.
                "Oem": {"Ec2": {"State": ec2_state}},
                "Actions": {
                    "#ComputerSystem.Reset": {
                        "target": (
                            f"/redfish/v1/Systems/{system_id}"
                            "/Actions/ComputerSystem.Reset"
                        ),
                        "ResetType@Redfish.AllowableValues": [
                            "On",
                            "ForceOff",
                            "GracefulShutdown",
                            "ForceRestart",
                        ],
                    }
                },
            },
        )

    def _handle_reset(self, system_id):
        if system_id not in self.config.systems:
            self._send_error(404, f"unknown system: {system_id}")
            return

        reset_type = self._read_body().get("ResetType")
        LOG.warning("reset request: system=%s type=%s", system_id, reset_type)

        try:
            if reset_type == "On":
                self.power.start(system_id)
            elif reset_type == "ForceOff":
                self.power.stop(system_id, force=True)
            elif reset_type == "GracefulShutdown":
                self.power.stop(system_id, force=False)
            elif reset_type in ("ForceRestart", "PowerCycle", "GracefulRestart"):
                # EC2 has RebootInstances, but it is a guest-OS-cooperative
                # reboot that a hung host will ignore -- useless for fencing.
                # A forced stop is what actually guarantees the node is down;
                # Pacemaker issues the matching "On" itself.
                self.power.stop(system_id, force=True)
            else:
                self._send_error(400, f"unsupported ResetType: {reset_type}")
                return
        except (ClientError, LookupError) as exc:
            self._send_error(500, f"EC2 power action failed: {exc}")
            return

        self._send_json(200, {"status": "accepted", "ResetType": reset_type})


def ensure_certificate(cert_path, key_path):
    """Generate a self-signed certificate if one was not supplied.

    The fencing credentials in install-config.yaml set
    ``certificateVerification: Disabled``, so the fence agent runs with
    ``--ssl-insecure`` and the certificate only has to exist, not chain.
    """
    if os.path.exists(cert_path) and os.path.exists(key_path):
        return cert_path, key_path

    LOG.info("generating self-signed certificate at %s", cert_path)
    subprocess.run(
        [
            "openssl", "req", "-x509", "-nodes", "-newkey", "rsa:2048",
            "-days", "3650",
            "-keyout", key_path,
            "-out", cert_path,
            "-subj", "/CN=redfish-ec2",
        ],
        check=True,
        capture_output=True,
    )
    os.chmod(key_path, 0o600)
    return cert_path, key_path


def make_server(config, bind, port, cert_path, key_path, use_tls=True, power=None):
    """Build the HTTP server.

    ``power`` is injectable so the routing and auth layers can be tested
    without an AWS account.
    """
    handler = type(
        "BoundRedfishHandler",
        (RedfishHandler,),
        {
            "config": config,
            "power": power if power is not None else Ec2Power(config),
            "sessions": SessionStore(),
        },
    )

    httpd = ThreadingHTTPServer((bind, port), handler)
    if use_tls:
        cert_path, key_path = ensure_certificate(cert_path, key_path)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_path, key_path)
        httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    return httpd


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Redfish power endpoint backed by the EC2 API"
    )
    parser.add_argument(
        "-c", "--config",
        default=os.environ.get("REDFISH_EC2_CONFIG", "/etc/redfish-ec2/config.json"),
        help="path to the JSON config file",
    )
    parser.add_argument("-b", "--bind", default="0.0.0.0", help="listen address")
    parser.add_argument("-p", "--port", type=int, default=8000, help="listen port")
    parser.add_argument("--cert-file", default="/etc/redfish-ec2/cert.pem")
    parser.add_argument("--key-file", default="/etc/redfish-ec2/key.pem")
    parser.add_argument(
        "--no-tls",
        action="store_true",
        help="serve plain HTTP (for local testing only)",
    )
    parser.add_argument("-d", "--debug", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if boto3 is None:
        LOG.error("boto3 is not installed; install it with 'pip install boto3'")
        return 1

    try:
        config = Config.load(args.config)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        LOG.error("could not load %s: %s", args.config, exc)
        return 1

    httpd = make_server(
        config,
        args.bind,
        args.port,
        args.cert_file,
        args.key_file,
        use_tls=not args.no_tls,
    )
    scheme = "http" if args.no_tls else "https"
    LOG.info(
        "serving %s://%s:%s/redfish/v1/Systems/{%s} in region %s",
        scheme, args.bind, args.port, ",".join(sorted(config.systems)), config.region,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        LOG.info("shutting down")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
