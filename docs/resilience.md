# Resilience

What this rig does when parts of it fail — and, more usefully, what it does
*not* do that a reader might reasonably assume it does.

The short version: **TNF survives losing a control-plane node. Almost nothing
else here survives losing the thing it sits on.** That is a defensible shape for
a demo rig, but the fencing story is careful enough to imply the rest of the
stack is built to the same standard, and it is not. Three of the findings below
are single points of failure that look like redundancy until you read the
manifests.

## How sure each claim is

Nothing here was established by breaking a live cluster. A fence on
`g4dn.metal` costs 5–15 minutes, a full rebuild costs about three and a half
hours, and the environment was torn down before this was written. Each claim is
tagged:

| tag | means |
|---|---|
| **config** | read out of this repo's manifests, and the file is cited |
| **observed** | seen on the cluster this repo last built |
| **predicted** | neither — inferred from how the components are documented to behave |

A **predicted** row is the one to distrust. [Testing this for
real](#testing-it-for-real) lists the experiments that would settle them.

## The one thing to understand first

On this rig the common node failure is a *fence*, and a fence is an EC2
`StopInstances` followed by `StartInstances` — see
[fencing-on-aws.md](fencing-on-aws.md). The node comes back in 5–15 minutes with
its EBS volumes intact, because `lvm-storage` deliberately pins LVM to the EBS
data volume and not to the instance-store NVMe that a stop would wipe
(`roles/lvm-storage/tasks/main.yml`).

So most of what follows is an **availability** problem measured in minutes, not
a data-loss problem. The exception is permanent loss of a node — an AZ event, a
terminated instance — where node-local storage does not come back, and several
things below cannot recover at all without a rebuild.

## Blast radius

| What fails | What goes down | What keeps running | Self-heals? | Basis |
|---|---|---|---|---|
| One TNF node | half the GPU capacity; anything pinned to it by a node-local PVC | the cluster API, etcd on the survivor, pods that can reschedule | yes, after the fence completes | config |
| Both TNF nodes | everything on TNF, including both guest profiles | the ACM hub | no — `make tnf-recover` | config |
| **The TNF bastion** | **the whole TNF cluster's API, `api-int` and `*.apps`, *and* the ability to fence** | the nodes themselves, and workloads already scheduled | **no** | config |
| The TNF AZ | both nodes and the bastion together | the ACM site | no — rebuild | config |
| The ACM hub | GitOps, policy reconciliation, observability, the assisted installer | both guest profiles, TNF, the app | yes, when the hub returns | config |
| The ACM bastion | the hub's API and console | TNF and the guests | no | config |
| VPC peering | hub↔TNF management; metrics to the hub | each site on its own | yes | predicted |
| A vcp-1 VM | one of three control-plane nodes | the other two, if the node holding them survives | yes, `running: true` | config |
| The guest NLB | the guest's public API and console | the guest itself, and in-cluster traffic | yes, CloudFormation | config |
| An app pod | that stage of the pipeline | the rest, degraded | yes, unless GPU- or PVC-blocked | config |

## The three that are worse than they look

### 1. The bastion is a single point of failure for the entire TNF cluster

`platform:none` means the installer stands up no load balancer, so `api`,
`api-int`, `:22623` and `*.apps` all terminate on haproxy on the bastion
(`roles/loadbalancer/templates/haproxy.cfg.j2`). The same host runs the Redfish
shim that Pacemaker fences through, and the ignition server.

It is one `AWS::EC2::Instance` with an Elastic IP
(`aws-infra/templates/services-stack.yaml:100`). No Auto Scaling group, no
health check, no recovery. If it stops:

- both TNF nodes stay up and keep running every workload on them, but
- nothing can reach the cluster API, including `oc`, Argo CD and the hub, and
- **neither node can fence the other.**

That last one is the compounding failure. Pacemaker will not let a survivor take
over etcd until it has fenced its peer, and it cannot fence without the shim. So
*bastion down, then a node down* is not two independent faults — it is a cluster
that stops and will not restart itself, and the recovery ladder in
`make tnf-recover` cannot help either, because it reaches the nodes through the
same API. The fencing doc lists this in one line at the bottom of its failure
table; the API consequence is the larger half and is not stated anywhere.

Recovering means starting the bastion again — the EIP and private IP are fixed,
so haproxy and the shim come back at the addresses DNS already points at. The
real exposure is not the outage, it is that this rig has no monitor that would
tell you the bastion is why the cluster went away.

### 2. `vcp-1`'s three control-plane VMs all sit on one node

This one reads as a three-node HA control plane and is not one. **observed** on
the last cluster built, and forced by config:

- all three VMs mount the shared `vcp-1-discovery` DataVolume,
  `accessModes: [ReadWriteOnce]`
  (`roles/vcp-cluster/templates/policy-virtualmachines.yaml.j2:256`);
- that volume is on `lvms-vg1`, which is TopoLVM — node-local, unreplicated,
  and a *hard* scheduling constraint;
- the `topologySpreadConstraints` a few lines down asks for `maxSkew: 1` but
  with `whenUnsatisfiable: ScheduleAnyway`, which is a preference.

A hard constraint beats a soft one, so all three land wherever the discovery
volume did. Nothing detaches that volume after the install, so the pinning is
permanent, not just a first-boot artefact.

Consequence: losing the node that holds them loses the **entire** `vcp-1`
cluster at once, rather than one member of three. On the upside, all three
coming back together is a clean etcd restart rather than a quorum repair — a
full-cluster stop is much easier to recover from than a split one.

This is a known trade: splitting the discovery volume per VM was raised and
deliberately declined. It is recorded here because the VM count implies an
HA claim that the storage layout withdraws, and anyone demoing `vcp-1` as
"three control-plane nodes" should know which of those words is load-bearing.

### 3. `hcp-1`'s control plane is a single etcd replica on node-local storage

`controllerAvailabilityPolicy: SingleReplica`
(`roles/hcp-guest/defaults/main.yml:62`), with etcd on `storage_class`, which is
again `lvms-vg1` — node-local
(`roles/hcp-guest/templates/sites-infra-hostedcluster.yaml.j2:41`).

The `SingleReplica` choice is correct and well-reasoned in the defaults file:
`HighlyAvailable` wants three nodes with anti-affinity, TNF has two, and the
hosted control plane simply never comes up. But it means:

- one etcd pod, pinned by its PVC to one of the two TNF nodes;
- if that node is fenced, the pod cannot move — the PV is not on the other node —
  so `hcp-1`'s API is down for the entire fence window, 5–15 minutes;
- `infrastructureAvailabilityPolicy: SingleReplica` puts the guest's router in
  the same position.

TNF itself stays up throughout. The guest does not. A demo that fences a node to
show TNF surviving will, on the same stroke, take the guest cluster offline for
a quarter of an hour if the etcd pod was on the wrong node — which is a coin
flip.

## What is genuinely resilient

Worth stating, because the list above is one-sided:

- **etcd on TNF.** Fencing is wired correctly, the timeouts are raised for EC2's
  slow metal stops, and the shim refuses to report `Off` during any transitional
  state — the one decision that would otherwise trade downtime for split brain.
- **Storage across a fence.** Pinning LVM to the EBS data volume rather than
  letting it discover instance-store NVMe is the difference between a fence
  costing minutes and a fence silently destroying every guest cluster's etcd.
- **The guests outliving the hub.** Neither profile needs the ACM hub at
  runtime. `hcp-1` is hosted by TNF's own MCE; `vcp-1` is a standalone cluster
  on its own disks. Losing the hub costs fleet management, not workloads.
- **`make tnf-recover`.** The documented recovery ladder, least invasive first,
  with the two destructive steps gated behind both an explicit flag *and* a
  live check that the peer is really gone. That check is the thing that stops a
  recovery attempt turning downtime into data loss.

## What is not resilient, by design

Fine for a demo; listed so nobody discovers them during one.

- Single AZ, single subnet per site (`aws-infra/templates/network-stack.yaml`).
- Every app component is `replicas: 1`, and `analyzer` and `vllm` each hold a
  GPU and a node-local PVC, so they cannot move.
- The ACM hub is single-node OpenShift.
- No alerting on any of the single points above.

## Testing it for real

Each experiment states what would confirm or refute the claim. They need a live
environment; the destructive ones need a rebuild afterwards.

**1. Fence a node, and watch the guest rather than the host.** The interesting
measurement is not that TNF survives — it is what `hcp-1` does.

```bash
oc get pods -n clusters-hcp-1 -o wide          # note which node etcd is on
oc debug node/master-0 -- chroot /host pcs stonith fence master-1
```

Confirms or refutes §3. Watch the guest's API from outside throughout. Allow 15
minutes.

**2. Stop the bastion while both nodes are healthy.** The cheapest and most
informative test here, and fully reversible.

```bash
aws ec2 stop-instances --instance-ids "$(...bastion...)"
oc get nodes                                    # expect: API unreachable
```

Confirms §1's API half. Start it again and the cluster should return without
intervention — worth proving, since it decides whether this is a five-minute
problem or a rebuild.

**3. Look at where `vcp-1`'s VMs actually are.** Non-destructive, and settles §2
in one command.

```bash
oc get vmi -n acm-vcp-vms -o wide               # expect: all three, one node
```

**4. Fence the node holding `vcp-1`.** Destructive to the guest, and the
honest version of "what does losing a node cost". Expect the whole guest to go,
and to come back when the node does.

**5. Cut the peering.** Delete the peering route and confirm each site keeps
running on its own, then restore it. Settles the one **predicted** row in the
table.

## If any of this should change

None of the findings above are bugs in the sense of something behaving other
than as written — each is a deliberate trade, and the smaller rig is usually the
right one for a demo. If they should stop being trades:

| Finding | What it would take |
|---|---|
| Bastion SPOF | an ASG of one with a recovery alarm, or moving `api`/`*.apps` to an NLB across two AZs and leaving only the shim on the bastion |
| `vcp-1` co-location | one discovery DataVolume per VM, so the spread constraint is the only constraint left |
| `hcp-1` single etcd | nothing, on two nodes — `HighlyAvailable` needs three, so this is a property of TNF, not of the config |
| Single AZ | a second subnet, which the fencing design does not object to but the `platform:none` DNS layout would need reworking for |

The bastion is the one worth acting on: it is the only entry that takes down a
healthy cluster, and the only one where the fix does not require more hardware.
