# Fencing on AWS

## The constraint

TNF is a two-node cluster, which means it cannot use quorum to decide which
node survives a partition. It uses fencing instead: Pacemaker kills the peer
before taking over its work, so there is never a moment when both nodes believe
they own etcd.

OpenShift wires that up for you, and it wires up exactly one agent.
`cluster-etcd-operator`, `pkg/tnf/pkg/pcs/fencing.go`:

```go
config := &fencingConfig{
    NodeName:          nodeName,
    FencingID:         fmt.Sprintf("%s_%s", nodeName, "redfish"),
    FencingDeviceType: "fence_redfish",
    ...
}
```

That is the only agent the operator constructs, from
`controlPlane.fencing.credentials` in the install-config. `fence_aws` does
appear in the same operator — but only in `pkg/tnf/pkg/pacemaker/statuscollector.go`,
as a resource type it will *display* if you created one by hand:

```go
ResourceAgentFenceAWS = "fence_aws"
```

Nothing creates it. So on EC2, which has no BMC and no Redfish service,
something has to answer Redfish on the nodes' behalf.

## The shim

[`tools/redfish-ec2/`](../tools/redfish-ec2/) implements the handful of Redfish
resources `fence_redfish` touches — ServiceRoot, the systems collection, a
system's `PowerState`, and `ComputerSystem.Reset` — and turns resets into
`StopInstances` / `StartInstances`. It runs under systemd on the bastion, gets
its AWS credentials from the instance profile (nothing on disk), and serves a
self-signed certificate.

`install-config.yaml` sets `certificateVerification: Disabled`, so
`fence_redfish` runs with `--ssl-insecure` and the certificate only has to
exist, not chain.

The IAM policy is scoped by tag, so a stray fence request cannot reach anything
outside this cluster:

```yaml
- Effect: Allow
  Action: [ec2:StartInstances, ec2:StopInstances]
  Resource: '*'
  Condition:
    StringEquals:
      'ec2:ResourceTag/tnf-cluster': !Ref ClusterName
```

## The one design decision that matters for correctness

Only `stopped` and `terminated` map to `PowerState: Off`. Every transitional
state — `pending`, `stopping`, `shutting-down` — reports `On`:

```python
POWERED_OFF_STATES = {"stopped", "terminated"}
```

`fence_redfish` treats a successful off as proof the peer can no longer write to
shared state. If the shim reported `Off` while EC2 was still `stopping`,
Pacemaker would conclude the fence succeeded against a node that is still
running — exactly the split brain fencing exists to prevent.

Reporting `On` for too long costs time. Reporting `Off` too early costs data.

There is a unit test for this, covering each transitional state
(`test_transitional_states_report_on`).

## Timeouts

A real BMC cuts power in seconds. `StopInstances` on a bare metal instance takes
minutes — AWS has to tear down a physical host. Pacemaker's defaults assume the
former, so without adjustment a fence that is merely slow gets recorded as a
fence that failed, and Pacemaker escalates.

`tnf-install` raises them after install:

```bash
pcs property set stonith-timeout=600s
pcs stonith update master-0_redfish \
    power_timeout=600 pcmk_reboot_timeout=900 pcmk_off_timeout=600
```

Tune with `stonith_timeout`, `stonith_power_timeout` and
`stonith_reboot_timeout` in `deploy/openshift-clusters/group_vars/all.yml`.

The TNF controller owns these devices and reconciles them. `pcs stonith update`
leaves options it was not given alone, so these survive a normal reconcile — but
if a device is ever recreated from scratch, re-run `make tnf`, which reapplies
them and is otherwise a no-op on an installed cluster.

**Expect a real fence to take 5–15 minutes on this rig.** That is a property of
EC2, not of TNF. Failover timings measured here do not transfer to hardware
with a real BMC.

## Verifying before you install

The install takes 60–90 minutes and a broken fencing path does not stop it —
the cluster comes up and degrades later. Check first.

Reachability, no credentials needed:

```bash
curl -sk https://10.0.0.5:8000/healthz | jq
# {"status":"ok","systems":["master-0","master-1"]}
```

Power state:

```bash
curl -sk -u admin:"$BMC_PASSWORD" \
  https://10.0.0.5:8000/redfish/v1/Systems/master-0 | jq '{PowerState, Oem}'
```

The check that actually matters — the real agent, invoked as Pacemaker will:

```bash
sudo dnf install -y fence-agents-redfish
fence_redfish --ip 10.0.0.5 --ipport 8000 \
  --systems-uri /redfish/v1/Systems/master-0 \
  --username admin --password "$BMC_PASSWORD" --ssl-insecure \
  --action status
```

`make tnf` performs the equivalent HTTP checks automatically: the
`redfish-shim` role proves the endpoint answers and accepts the BMC password
before the install starts, and `tnf-install` proves each stonith device can read
its target's power state afterwards.

## Verifying after install

```bash
oc debug node/master-0 -- chroot /host pcs stonith config
oc debug node/master-0 -- chroot /host pcs stonith status
oc debug node/master-0 -- chroot /host pcs property config stonith-timeout
```

`Started` only means Pacemaker started the resource agent. To prove the whole
path end to end, fence a node for real:

```bash
oc debug node/master-0 -- chroot /host pcs stonith fence master-1
```

`master-1` should stop in EC2, Pacemaker should record the fence, and etcd
should keep serving from `master-0` throughout. Allow 15 minutes.

## Failure modes

| Symptom | Likely cause |
|---|---|
| stonith device `Stopped`, agent logs 401 | `BMC_PASSWORD` differs between `instance.env` and the cluster secret |
| stonith device `Stopped`, agent logs connection refused | `redfish-ec2.service` is down, or the security group blocks 8000 |
| shim returns 500 on a system | the `Name` tag does not match, or two live instances share it |
| fence reported failed but the node did stop | timeouts still at their defaults — re-run `make tnf` |
| stonith device exists for an unknown node | node hostname does not match the fencing credential; check `/etc/hostname` |
| neither node can fence the other | the bastion is down; it is a single point of failure for fencing *and* for the cluster API — see [resilience.md](resilience.md#1-the-bastion-is-a-single-point-of-failure-for-the-entire-tnf-cluster) |
