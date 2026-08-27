# redfish-ec2

A Redfish power endpoint backed by the EC2 API.

## Why this exists

Two-Node OpenShift with Fencing needs Pacemaker to be able to kill a
misbehaving peer, and the only stonith agent OpenShift wires up for you is
`fence_redfish`. In `cluster-etcd-operator`, `pkg/tnf/pkg/pcs/fencing.go`
reads `controlPlane.fencing.credentials` out of the install-config and creates
exactly one kind of device:

```go
FencingDeviceType: "fence_redfish",
```

There is no code path that creates any other agent. `fence_aws` appears in the
operator only in the status collector, as a resource type it will *display* if
you created it by hand — never one it creates.

EC2 bare metal instances have no BMC and no Redfish service. So to run TNF on
real `g4dn.metal` hardware, something has to answer Redfish on their behalf.
That is this process: it speaks the small slice of Redfish that `fence_redfish`
uses and turns it into `StartInstances` / `StopInstances`.

## What it implements

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/redfish/v1/` | ServiceRoot |
| `GET` | `/redfish/v1/Systems` | system collection |
| `GET` | `/redfish/v1/Systems/<id>` | `PowerState` — what the fence agent polls |
| `POST` | `/redfish/v1/Systems/<id>/Actions/ComputerSystem.Reset` | power action |
| `POST` | `/redfish/v1/SessionService/Sessions` | session auth (HTTP Basic also works) |
| `GET` | `/healthz` | unauthenticated liveness |

Virtual media is deliberately **not** implemented. Nodes here boot from an
RHCOS AMI with ignition delivered through EC2 user-data, so nothing mounts an
ISO. That is the main way this differs from [FakeFish](https://github.com/openshift-kni/fakefish),
which exists to make Metal3 *provision* over virtual media.

## Design decisions worth knowing

**Systems are addressed by logical id, not instance id.** A fencing address
looks like `https://<bastion>:8000/redfish/v1/Systems/master-0`. The shim
resolves `master-0` to an instance by its `Name` tag at request time.

This is not cosmetic. Fencing addresses must be written into
`install-config.yaml` *before* `openshift-install create ignition-configs`, and
therefore before the instances exist. Addressing by instance id would be
circular. It also means replacing an instance does not invalidate the
install-config.

**A transitional instance reports `On`, never `Off`.** Only `stopped` and
`terminated` map to `Off`:

```python
POWERED_OFF_STATES = {"stopped", "terminated"}
```

`fence_redfish` treats a successful "off" as proof the peer can no longer write
to shared state. If the shim reported `Off` while EC2 was still `stopping`,
Pacemaker would conclude the fence succeeded against a node that is still
running — precisely the split brain fencing exists to prevent. Reporting `On`
too long only costs time; reporting `Off` too early costs data.

**`ForceRestart` becomes a forced stop.** EC2 does have `RebootInstances`, but
it is a guest-OS-cooperative reboot that a hung host will ignore. Pacemaker's
reboot action is implemented as off-then-on anyway, and it issues the `On`
itself.

**A read failure is a 500, not a guess.** If `DescribeInstances` fails the shim
returns an error rather than a fabricated power state, so the fence agent
retries instead of acting on made-up information.

## Fencing latency

Stopping a bare metal EC2 instance takes minutes, not the seconds a real BMC
takes to cut power. Pacemaker's defaults are far too short for this. The
`tnf-install` role raises them after install:

```bash
pcs property set stonith-timeout=600s
pcs stonith update <node>_redfish power_timeout=600 pcmk_reboot_timeout=900
```

Plan for TNF recovery on this rig taking **5–15 minutes**, versus well under a
minute on hardware with a real BMC. That is a property of EC2, not of the shim,
and it is worth remembering before drawing conclusions from failover timings
measured here.

## Configuration

```json
{
  "region": "us-west-2",
  "username": "admin",
  "password": "CHANGE-ME",
  "systems": {
    "master-0": {"name_tag": "tnf-gp-master-0"},
    "master-1": {"name_tag": "tnf-gp-master-1"}
  }
}
```

Use `"instance_id": "i-0abc..."` instead of `"name_tag"` to pin an instance
explicitly. AWS credentials come from the normal boto3 chain — on the bastion
that is the instance profile, so no keys are stored on disk.

The instance profile needs only:

```
ec2:DescribeInstances
ec2:StartInstances
ec2:StopInstances
```

## Running

Under systemd on the bastion (what the `redfish-shim` Ansible role sets up):

```bash
podman run -d --name redfish-ec2 --network=host \
  -v /etc/redfish-ec2:/etc/redfish-ec2:Z \
  quay.io/<org>/redfish-ec2:latest \
  --config /etc/redfish-ec2/config.json --port 8000
```

Directly, for a quick test:

```bash
pip install boto3
./redfish_ec2.py --config config.json --port 8000
```

A self-signed certificate is generated on first start if `--cert-file` and
`--key-file` do not exist. The install-config sets
`certificateVerification: Disabled`, so the fence agent runs with
`--ssl-insecure` and the certificate only has to exist, not chain.

## Verifying

Reachability, no credentials needed:

```bash
curl -sk https://<bastion>:8000/healthz | jq
```

Power state:

```bash
curl -sk -u admin:<password> \
  https://<bastion>:8000/redfish/v1/Systems/master-0 | jq .PowerState
```

The check that actually matters — the real fence agent, exactly as Pacemaker
will invoke it:

```bash
dnf install -y fence-agents-redfish
fence_redfish --ip <bastion> --ipport 8000 \
  --systems-uri /redfish/v1/Systems/master-0 \
  --username admin --password <password> --ssl-insecure \
  --action status
```

Do this **before** starting an install. A fencing address that does not answer
produces a cluster that comes up and then degrades later, which is a much more
expensive way to find the same problem.

## Tests

```bash
cd tools/redfish-ec2 && python3 -m unittest test_redfish_ec2 -v
```

19 tests covering routing, both auth modes, the power-state mapping (including
every transitional state), and the reset-type translation. The EC2 layer is
stubbed, so they need no AWS account. They do not prove the shim works against
a real `fence_redfish` binary — run the command above for that.
