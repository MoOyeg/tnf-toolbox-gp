# Resilience

What this rig does when parts of it fail — and, more usefully, what it does
*not* do that a reader might reasonably assume it does.

The short version: **TNF survives losing a control-plane node. Almost nothing
else here survives losing the thing it sits on.** That is a defensible shape for
a demo rig, but the fencing story is careful enough to imply the rest of the
stack is built to the same standard, and it is not. The findings below are
single points of failure that look like redundancy until you read the
manifests. One of the three has since been fixed and is kept here with its
fix, because the shape of the mistake is the useful part: it was a storage
decision that silently overrode a scheduling one.

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
| One TNF node | **the cluster API for ~17m**; half the GPU capacity; anything pinned to it by a node-local PVC | `*.apps` throughout; the guests' own quorum, if their machines are spread | yes, but only when the node returns — not on the survivor | observed |
| Both TNF nodes | everything on TNF, including both guest profiles | the ACM hub | no — `make tnf-recover` | config |
| **The TNF bastion** | **the whole TNF cluster's API, `api-int` and `*.apps`, *and* the ability to fence** | the nodes themselves, and workloads already scheduled | **no** | config |
| The TNF AZ | both nodes and the bastion together | the ACM site | no — rebuild | config |
| The ACM hub | GitOps, policy reconciliation, observability, the assisted installer | both guest profiles, TNF, the app | yes, when the hub returns | config |
| The ACM bastion | the hub's API and console | TNF and the guests | no | config |
| VPC peering | hub↔TNF management; metrics to the hub | each site on its own | yes | predicted |
| A vcp-1 VM | one of five nodes | the rest — the machines of its role are split across both infra nodes | yes, `running: true` | observed |
| An infra node holding vcp-1 VMs | at most two of three masters, and one of two workers; the guest's API for ~27m and the app for ~33m | the guest's quorum on the surviving master | yes, unattended, ~33m end to end | observed |
| The guest NLB | the guest's public API and console | the guest itself, and in-cluster traffic | yes, CloudFormation | config |
| An app pod | that stage of the pipeline | the rest, degraded | yes, unless GPU- or PVC-blocked | config |

## The three that were worse than they looked

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

### 2. `vcp-1`'s VMs used to all sit on one node — fixed

**Fixed**, and recorded here because the finding is what the fix is for. As
written, this section described a three-node HA control plane that was not one:
all three VMs ran on `master-1`, **observed** on the cluster this repo last
built.

The cause was storage, not scheduling. Every VM mounted one shared
`vcp-1-discovery` DataVolume, `accessModes: [ReadWriteOnce]`, on `lvms-vg1`,
which is TopoLVM — node-local and a *hard* scheduling constraint. The
`topologySpreadConstraints` beside it asked for `maxSkew: 1` with
`whenUnsatisfiable: ScheduleAnyway`, a preference. A hard constraint beats a
soft one, so all three landed wherever the discovery volume did, permanently:
nothing detaches that volume after the install.

Two changes, in
`roles/vcp-cluster/templates/policy-virtualmachines.yaml.j2`:

- **a discovery volume per machine**, so no volume pins two machines together.
  It costs one ISO import per machine instead of one per cluster, which is the
  whole price.
- **a `DoNotSchedule` constraint per role**, on top of the cluster-wide
  preference. `maxSkew: 1` over two infra nodes then admits 2/1 for three
  masters and 1/1 for two workers — so no infra node can hold a whole quorum,
  and each keeps a router.

**observed** after the rebuild: masters on `master-1`, `master-0`, `master-1`;
workers one to each.

What it costs is worth stating, because the old shape had a genuine upside: all
three masters going down together was a clean etcd restart, where a split loses
one member and repairs quorum. That trade is now taken the other way round on
purpose. The other cost is that `DoNotSchedule` can leave a machine `Pending`
rather than unbalanced if an infra node is cordoned or full — visible and
repairable, where a control plane that schedules cleanly onto one node is
neither.

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

**3. Look at where `vcp-1`'s VMs actually are.** Non-destructive, and the
regression test for §2 — the constraints are only worth what the scheduler
actually did with them.

```bash
oc get vmi -n acm-vcp-vms -o wide      # expect: masters 2/1, workers 1/1
```

**4. Fence the infra node holding two of `vcp-1`'s masters.** **Run on
2026-09-21** -- see [Fencing a TNF node, measured](#fencing-a-tnf-node-measured)
for the numbers. The guest's quorum repaired itself exactly as §2's fix bet it
would. What the experiment actually found was elsewhere: TNF's own API is down
for the whole fence, because a `g4dn.metal` takes longer to stop than the fence
agent will wait. Worth re-running after any change to the fencing path, and the
probe that measured it is four `curl`s in a loop -- the outage windows are only
believable because something was sampling them.

**5. Cut the peering.** Delete the peering route and confirm each site keeps
running on its own, then restore it. Settles the one **predicted** row in the
table.

## Fencing a TNF node, measured

Run 2026-09-21 on the five-node `vcp-1` build. `pcs stonith fence master-1`,
which held two of `vcp-1`'s three control-plane VMs and one of its two workers.
Every number below is **observed**, from a probe sampling four endpoints every
ten seconds and from Pacemaker's own logs.

| | |
|---|---|
| TNF `*.apps` | **no interruption** — 97 of 97 samples returned 200 |
| TNF API, real queries | **out for 17m31s** |
| `vcp-1` API, real queries | out for 27m17s |
| The app's route | out for 33m26s |
| Fence, issue to instance `running` | 15m32s |

```
21:03:09  fence issued
21:03:11  Pacemaker requests fencing of master-1
21:04:23  TNF /readyz answers 200 again -- while every real query still fails
21:18:13  fence attempt FAILS: "Fence agent did not complete within 15m"
21:18:36  retry succeeds, 23s later
21:18:41  master-1 reaches EC2 state running
21:20:01  master-0's etcd removes master-1 -- quorum at last
21:20:40  TNF API serves real queries
21:30:26  vcp-1 API serves real queries
21:36:35  the app answers 200
```

### The headline: a fence costs the whole API, and it is arithmetic

The blast-radius table above used to say losing one TNF node costs GPU capacity
and keeps "the cluster API, etcd on the survivor". It does not. The API served
nothing for the entire fence, and what ended the outage was **master-1 coming
back**, not master-0 taking over.

The chain is short and every link is measured:

1. A `g4dn.metal` takes **~15m30s** to reach EC2 state `stopped`. This is the
   instance type, not the shim: bare metal has a physical host to release.
2. The fence agent's timeout is **15m**, so the first attempt always expires.
   Observed twice on the same day, at 20:31 and at 21:18, both followed by a
   retry that succeeded in about twenty seconds -- because by then the instance
   had finally stopped.
3. Pacemaker will not shrink etcd to one member until a fence is *confirmed*.
   That is correct: shrinking on an unconfirmed fence is how two nodes both
   decide they are the survivor.
4. So etcd stays a two-member cluster with one member gone. Two-member etcd
   needs two votes. There is no quorum, and the API answers nothing.

The 17m31s is therefore not a slow failover. It is step 1 plus the time to
notice, and it will happen on every fence of a bare-metal node.

### The second finding: `/readyz` says yes when the API can serve nothing

Throughout the outage the API answered `/readyz` with 200. Measured on TNF,
three times in a row, at the same moment:

```
/version               OK     served from memory
/readyz                OK     <- what health checks look at
/livez                 TIMEOUT
/api/v1/nodes?limit=1  TIMEOUT
```

Readiness is meant to be the stricter of the two, and here it is the one that
lies. Anything that believes it -- an NLB health check, a TCP probe on the
NodePort, a human running `curl /readyz` -- concludes the API is fine while
every real request hangs. It is why `vcp-1`'s endpoint *flapped* between 200
and failure rather than going cleanly down, which reads as a flaky network
instead of an absent quorum.

### What recovered by itself, and what limped

Everything recovered without intervention. Worth separating how well:

- **TNF `*.apps` never dropped a request.** Ingress runs on both nodes and the
  bastion's haproxy took master-0's copy. This is the one part of the stack
  that behaved exactly as the table claims.
- **`vcp-1` repaired its own quorum.** Two of three control-plane VMs went with
  the node. The third carried the cluster, and the two returned and rejoined
  with one etcd restart between them and no degraded operator. This was the
  **predicted** row that [§2](#2-vcp-1s-vms-used-to-all-sit-on-one-node--fixed)
  bet on, and it is now observed.
- **The VMs came back on the same infra nodes**, so the 2/1 and 1/1 spread
  survived the outage rather than collapsing onto the survivor.
- **GPUs lag the node.** `cp-1` and `cp-3` returned advertising
  `nvidia.com/gpu: 0` and stayed that way for minutes, long enough for the
  analyzer's replacement pod to be rejected from four nodes at once. They
  self-corrected. A GPU workload is unschedulable for a few minutes after its
  node returns, which is longer than the node takes to report Ready.
- **The app took 33m26s** -- nearly twice the TNF API outage -- because its
  recovery is serial: node back, kubelet back, GPU re-advertised, pod
  rescheduled, model reloaded.

### An issue this found one layer up: the app's volumes pin it too

The scheduler refused the analyzer's replacement with *"1 node(s) didn't match
PersistentVolume's node affinity"*. `analyzer-models` and `vllm-models` are
ReadWriteOnce on `lvms-vg1`, which is node-local, so each of those two pods can
only ever run on the node holding its volume.

This is [§2](#2-vcp-1s-vms-used-to-all-sit-on-one-node--fixed) again, one layer
up: the same storage class making the same promise about placement that the
scheduler cannot keep. Losing a `vcp-1` node for a few minutes costs those pods
a few minutes. Losing one permanently means the volume is gone and the analyzer
has to refit its model from scratch, on whichever node it lands on next.

### What to change

| Finding | What it would take |
|---|---|
| **A fence costs the API for ~17m** | Nothing cheap. Fencing by *isolation* was the obvious idea and it does not work here -- see [Partitioning a TNF node](#partitioning-a-tnf-node-measured), which found that revoking a security group leaves established connections running. A watchdog is the mechanism that would actually help, and its cost is a shared disk; see the note below the table. |
| The first fence attempt always times out | Raise `pcmk_reboot_timeout` past the measured 15m30s, with margin. Cheap and safe, but it only saves the retry -- roughly two minutes of the seventeen. |
| `/readyz` passing without quorum | Point the load balancers' health checks at something that needs etcd, so an API without quorum is taken out of rotation instead of kept in it. |
| GPU workloads unschedulable after a node returns | Nothing, probably: it self-corrects in minutes and the alternative is trusting a device plugin that has not finished registering. Worth knowing so it is not diagnosed as a capacity problem. |
| The app's models on node-local volumes | The same trade as `vcp-1`'s discovery volumes, and the same fix if it matters: shared storage, at the cost of needing some. |

The watchdog is the option worth pricing, because it is the only one that
removes the wait rather than accommodating it. Both nodes have a real hardware
watchdog (`iTCO_wdt`, 30s) and `sbd` installed, and both are unused --
`stonith-watchdog-timeout` is 0 and `have-watchdog` is false. With SBD the
survivor stops waiting for AWS to confirm anything: it waits a known timeout
and proceeds, because the absent node's own watchdog guarantees it has reset
itself. The catch is that `corosync.conf` sets `two_node: 1`, and diskless SBD
on two nodes is unsafe -- a partition makes *both* sides self-fence -- so it
needs a shared block device as a tiebreaker. On AWS that is EBS Multi-Attach,
which both nodes could use since they share an availability zone. Whether TNF
supports SBD at all is a question for Red Hat, not one this repository can
answer.

Two notes for whoever runs this next. `pcs stonith fence` through `oc debug`
reports `error: http2: client connection lost`; the fence succeeded, and the
command lost its own session to the API blip it caused. And `power_timeout` on
the stonith devices does nothing: the fencing library zeroes it whenever the
agent is run by `pacemaker-fenced`, which makes the poll loop unbounded and
leaves `pcmk_reboot_timeout` as the only deadline that matters.

## Partitioning a TNF node, measured

Run 2026-09-21, an hour after the power-off above, to answer a different
question: a fenced node is *dead*, but a partitioned node is **alive and
isolated** -- which is the case fencing actually exists for. master-0 was the
DC and held both stonith devices when this started.

### First attempt: a security group is not a partition

Revoking master-0's security group -- replacing it with one carrying no inbound
and no outbound rules -- did not partition anything.

```
23:03:10  master-0's ENI moved to a rules-free security group
23:03:27  haproxy marks every master-0 backend DOWN (new connections refused)
23:04:51  pcs: "Online: [ master-0 master-1 ]", real API queries still succeed
23:06:36  still Online, 3.5 minutes in
```

`ss` on master-1, mid-"partition":

```
ESTAB  10.0.0.11:37612 -> 10.0.0.10:2379   kube-apiserver
corosync runtime.members.1.status = joined
corosync runtime.members.2.status = joined
```

**AWS security groups are stateful, and removing a rule does not tear down the
connections it was permitting.** New connections are refused immediately --
which is why haproxy noticed in seventeen seconds -- while the established
corosync and etcd flows carried on as though nothing had happened.

This is worth more than a note on methodology, because it kills an idea this
document previously recommended: fencing by revoking a security group, on the
grounds that isolation is confirmable in seconds where a power-off is not. It
is not isolation. A node fenced that way keeps every connection it already had,
including the etcd session it could still write through. The measurement above
is the reason that row now says "nothing cheap".

### Second attempt: dropping packets on the node

`iptables -j DROP` on master-0 for all traffic to and from master-1 and the
bastion. The bastion is included deliberately: it hosts the Redfish shim, so
blocking it stops master-0 fencing *back* and makes the test one-sided.

```
23:10:09  partition applied
23:10:28  master-1 issues ForceOff against master-0          +19s
23:14:03  master-0 observed stopping
23:25:28  first fence attempt times out (pcmk_reboot_timeout=900s)
23:25:38  retry issues ForceOff
23:26:01  EC2 reaches stopped; the agent issues On           +15m33s
23:26:11  master-0 running
23:28:09  TNF API serves real queries again                  +18m00s
```

### What it showed

**Detection and decision take nineteen seconds.** That is the whole of
Pacemaker's contribution: corosync lost the peer, master-1 took the DC role,
started a stonith device it had not been running, and issued the fence. The
other seventeen minutes are AWS stopping an instance. Whatever is wrong here,
the cluster software is not the slow part.

**The fence race resolved correctly, and not by luck.** master-0 held *both*
stonith devices and was the DC, so the naive expectation is that it wins. The
devices carry asymmetric delays -- `master-0_redfish` waits 10s,
`master-1_redfish` waits 1s -- which means that in a symmetric partition
master-0 fences master-1 first and survives. Here master-0 could not reach the
shim, so exactly one fence request was issued and master-1 won. Worth knowing
which way the asymmetry points: **in a partition where both nodes can still
reach the bastion, master-0 is the designated survivor.**

**A partition costs the same as a power-off.** 18m00s against 17m31s. From the
cluster's point of view the two are the same event, because the expensive part
is neither detection nor the failure mode -- it is waiting for EC2.

**The first fence attempt timed out for the third consecutive time.** Three
fences on this rig, three timeouts at 900s followed by a retry that succeeded
in about twenty seconds. The stop takes 15m11s to 15m33s; the timeout is 15m00s.

## If any of this should change

None of the findings above are bugs in the sense of something behaving other
than as written — each is a deliberate trade, and the smaller rig is usually the
right one for a demo. If they should stop being trades:

| Finding | What it would take |
|---|---|
| Bastion SPOF | an ASG of one with a recovery alarm, or moving `api`/`*.apps` to an NLB across two AZs and leaving only the shim on the bastion |
| `hcp-1` single etcd | nothing, on two nodes — `HighlyAvailable` needs three, so this is a property of TNF, not of the config |
| Single AZ | a second subnet, which the fencing design does not object to but the `platform:none` DNS layout would need reworking for |

The bastion is the one worth acting on: it is the only entry that takes down a
healthy cluster, and the only one where the fix does not require more hardware.
