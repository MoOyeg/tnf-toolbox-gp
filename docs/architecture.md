# Architecture

## Why `platform: none`

There is no `platform: aws` option for a two-node cluster. The TNF enhancement
([openshift/enhancements, `two-node-fencing/tnf.md`](https://github.com/openshift/enhancements/blob/master/enhancements/two-node-fencing/tnf.md))
says so directly: cloud support was left out to limit the number of supportable
configurations. `platform: baremetal` is also unavailable, because IPI on
bare metal needs Metal3 and Ironic to provision hosts over Redfish virtual
media, and EC2 instances cannot be provisioned that way.

That leaves `platform: none` — user-provisioned infrastructure — which is what
the OpenShift TNF UPI sample uses. The consequence is that everything the
installer would normally stand up becomes this repository's job:

| The installer would normally… | Here instead |
|---|---|
| create a keepalived VIP for `api` / `api-int` | haproxy on the bastion, with Route53 A records |
| create a VIP for `*.apps` | the same haproxy |
| provision hosts over Redfish virtual media | RHCOS AMI + ignition via EC2 user-data |
| create and destroy the bootstrap machine | a separate CloudFormation stack |
| know each node's hostname | ignition writes `/etc/hostname` |

## The bastion is not just a jump host

It carries three services that a `platform: none` cluster on EC2 has nowhere
else to put:

**haproxy** fronts `api`, `api-int`, `*.apps` on 6443 / 22623 / 80 / 443.

DNS is split-horizon, and both halves point at this host:

  * A **private** Route53 zone for `<cluster>.<base-domain>`, associated with the
    VPC, resolves all three names to the bastion's fixed *private* address.
    Cluster nodes use this; their traffic never leaves the VPC.
  * The account's **public** zone gets `api` and `*.apps` pointing at the
    bastion's *elastic* IP. Everyone else uses these.

The private zone is more specific and VPC-attached, so it wins inside the VPC.
`api-int` exists only there -- it is an internal name and nothing outside the
cluster has any business resolving it.

The base domain is a real, publicly delegated zone rather than something like
`.local`, and that is what makes the cluster usable without ceremony: the
installer issues the API certificate for `api.<cluster>.<base-domain>`, so a
fetched kubeconfig works from anywhere with no `/etc/hosts` entries and no
`--insecure-skip-tls-verify`. The zone name is discovered from the account
rather than configured, because it differs per account.

**redfish-ec2** is the Redfish endpoint Pacemaker fences through. See
[fencing-on-aws.md](fencing-on-aws.md).

**An ignition HTTP server** on 8080 serves `bootstrap.ign`. EC2 user-data is
capped at 16 KiB and `bootstrap.ign` is several hundred, so user-data carries a
pointer and the real config is fetched from here. `master.ign` is served the
same way, for uniformity and so a rebuilt node re-fetches a current config
rather than an inlined stale one.

`bootstrap.ign` is served over plain HTTP — the pattern the OpenShift UPI
documentation prescribes — and port 8080 is restricted to the VPC by security
group. It is deleted the moment `bootstrap-complete` returns. Serving it over
HTTPS with the CA embedded in the ignition pointer would be strictly better and
is a reasonable change to make; it was not done here because it adds a failure
mode that could not be tested.

**Ansible only ever targets the bastion.** `openshift-install` and every `oc`
call run there. A workstation cannot resolve `api-int`, because the hosted zone
is private to the VPC — so a workstation cannot drive an install.

## Boot and ignition

RHCOS on AWS reads its ignition config from EC2 user-data. Two shapes are used:

```jsonc
// master-N: merge, so the local storage stanza survives
{"ignition":{"version":"3.4.0",
  "config":{"merge":[{"source":"http://10.0.0.5:8080/master.ign"}]}},
 "storage":{"files":[{"path":"/etc/hostname","mode":420,"overwrite":true,
   "contents":{"source":"data:,master-0%0A"}}]}}

// bootstrap: replace, because nothing local needs to survive
{"ignition":{"version":"3.4.0",
  "config":{"replace":{"source":"http://10.0.0.5:8080/bootstrap.ign"}}}}
```

`merge` rather than `replace` for the masters is load-bearing. `replace`
discards the rest of the config, taking `/etc/hostname` with it — and that file
is what makes the node register as `master-0` rather than
`ip-10-0-0-10.us-west-2.compute.internal`.

The node name matters because `controlPlane.fencing.credentials[].hostname` has
to equal it. A mismatch produces a Pacemaker stonith device aimed at a node
name that does not exist, and nothing notices until the first real fence.
`tnf-install` asserts the two match after install rather than trusting it.

## Ordering, and the circular dependency that shapes it

The fencing address goes into `install-config.yaml`, which is consumed before
`create ignition-configs`, which happens before any instance exists. So the
fencing address cannot contain an EC2 instance id.

It does not. Systems are addressed by logical name:

```
https://10.0.0.5:8000/redfish/v1/Systems/master-0
```

and the shim resolves `master-0` to an instance by its `Name` tag at request
time. Two consequences: the address can be written before the instances exist,
and replacing an instance does not invalidate the install-config.

That is also why `BASTION_PRIVATE_IP` is fixed rather than assigned — the
address embeds it, and it has to be known before the bastion is created.

The resulting order:

```
1. network stack        VPC, subnet, security groups, private hosted zone
2. services stack       bastion, elastic IP, DNS records
3. redfish-shim         configured and proved to answer, before any install
4. haproxy              with the bootstrap backend included
5. install-config       fencing addresses point at step 3
6. ignition             published to the bastion's HTTP server
7. compute stack        two g4dn.metal, user-data = ignition pointers
8. bootstrap stack      separate, so removing it is a stack delete
9. wait bootstrap-complete, delete stack 8, drop the haproxy backend
10. wait install-complete
11. raise Pacemaker's timeouts and prove each stonith device can read power state
```

Step 3 comes before step 5 on purpose. A TNF cluster whose stonith devices
cannot reach their endpoint installs perfectly well and degrades later — the
most expensive possible moment to discover a typo in a password.

## Layers on top

```
LVM Storage      pinned to the EBS data volume, never the instance store
OpenShift Virt   permittedHostDevices: 10DE:1EB8, externalResourceProvider
ACM              MultiClusterHub, availabilityConfig Basic
  └── MCE        hypershift + hypershift-local-hosting components enabled
        └── HostedCluster / NodePool, KubeVirt provider, hostDevices: T4
```

`availabilityConfig: Basic` matters: `High` runs two replicas of every hub
component on a two-node cluster that is also carrying OpenShift Virtualization
and every guest cluster's control plane.

`externalResourceProvider: true` on the permitted host device tells KubeVirt
that the GPU Operator's sandbox device plugin already advertises the resource
and owns the `vfio-pci` binding, so the two do not fight over it.

## Failure modes specific to this rig

**A fence takes 5–15 minutes.** Stopping a bare metal instance is minutes of
work. Do not draw conclusions about TNF failover timing from measurements here.

**Instance store is wiped by a fence.** A fence is a stop/start, and stop/start
discards instance-store contents. This is why storage is pinned to EBS.

**Every guest worker VM lands on one node.** Only the node labelled
`vm-passthrough` advertises the GPU resource. Fencing that node takes every
guest cluster's workers with it.

**The bastion is a single point of failure for fencing.** If it is down,
neither node can fence the other. Acceptable for a test rig, and worth
remembering when interpreting a split-brain that looks like a TNF bug.

## Where ACM runs, and why it moved

ACM originally ran on the TNF cluster, which was also the cluster hosting the
guest clusters. That co-location is the problem: the management plane shared a
fate with the thing it was managing. Losing TNF lost the hub that was supposed to
tell you TNF was gone.

ACM now runs on its own single-node OpenShift cluster, which is itself a KubeVirt
VM on TNF. That sounds circular and is not, because the two planes fail
differently: the SNO is a workload, so it reschedules; and nothing about TNF's
own operation depends on the SNO being up.

```
TNF (bare metal)  ── infra cluster: OpenShift Virtualization, GPUs, storage
  ├── MultiCluster Engine        can still host guest clusters directly
  ├── VM: SNO                    ACM + MCE + HyperShift operator
  │     └── HostedCluster ──┐
  └── VMs: guest workers ←──┘    created on TNF via --infra-kubeconfig-file
```

**The join between them is one secret.** `hcp create cluster kubevirt` takes
`--infra-kubeconfig-file` and `--infra-namespace`: the HostedCluster object and
its control-plane pods live on the SNO, while the worker VMs are created on TNF.
`--host-device-name` is documented as exposing a PCI device *from the infra
cluster*, so GPU passthrough works across the split unchanged — the T4s are still
TNF's, bound to vfio-pci on `master-1`.

**Two HyperShift operators now exist**, one on TNF's MCE and one on the SNO. They
are kept apart by namespace: TNF hosts its own guests in `clusters`, while VMs
created by the SNO land in `sno-hosted-vms`. Without that separation both
operators would reconcile the same objects, each believing it owned them.

**Both clusters' APIs share the bastion's port 6443**, separated by TLS SNI in
haproxy without terminating the connection, so each still presents its own
certificate. The alternative — a second port — would have put a non-standard port
into every kubeconfig and DNS record.

The SNO gets its own private hosted zone rather than records in TNF's. TNF's
private zone is `<cluster>.<base-domain>`, and `api.sno.<base-domain>` is not
inside it; Route53 rejects a record that does not belong to its zone.

## Two sites

ACM does not run on the cluster it manages, and does not run *on* that cluster's
hardware either. It has its own site: a separate VPC, its own bastion, and its
own bare-metal node. The two are joined by VPC peering.

```
ACM site                                TNF site
VPC 10.1.0.0/16                         VPC 10.0.0.0/16
├── bastion  10.1.0.5                   ├── bastion  10.0.0.5
└── sno-0    10.1.0.10  m5zn.metal      ├── master-0 10.0.0.10  g4dn.metal
      ACM + MCE + HyperShift            └── master-1 10.0.0.11  g4dn.metal
      OpenShift Virtualization                MCE, OCP-V, 16x Tesla T4
                    │                                    ▲
                    └────── VPC peering ─────────────────┘
                            ACM drives TNF as its KubeVirt infra cluster
```

**Why a separate site rather than a VM on TNF.** A hub that lives on the cluster
it manages shares that cluster's failure domain: losing TNF also loses the thing
that would tell you TNF was gone. A VM on TNF is better than a namespace on TNF
but has the same property. Its own VPC and its own hardware does not.

**Why bare metal for a single-node cluster.** The ACM cluster runs OpenShift
Virtualization, which needs KVM, and KVM is not available on a virtualised EC2
instance. `m5zn.metal` is the cheapest x86 metal that fits. It needs no GPU --
the GPUs belong to the TNF site, and ACM reaches them by creating VMs there.

**The CIDRs must not overlap.** Peering will not route between VPCs whose ranges
collide, and AWS rejects the connection outright. `create-acm-infra.sh` refuses
to proceed if they match, rather than letting it fail later as silent packet
loss.

**A peering connection on its own moves nothing.** Each VPC also needs a route
pointing the other's CIDR at the connection, *and* a security group rule
admitting it -- traffic arriving over peering carries the peer's addresses, which
are outside the local VPC CIDR that the in-VPC rules cover. Both halves of both
are in the templates, because a connection that exists but does not route looks
exactly like a firewall problem. `create-peering.sh` finishes by opening a TCP
connection from one bastion to the other, since "active" is not proof.

**DNS is associated in both directions.** Without that, the ACM cluster resolves
`api.<tnf>.<domain>` through public DNS to TNF's elastic IP and the traffic
leaves the VPC and comes back over the internet -- working, but not using the
peering. Associating each site's private zone with the other's VPC keeps it on
the connection.

**Two HyperShift operators exist**, one on TNF's MCE and one on the ACM cluster.
They are separated by namespace: TNF hosts its own guests in `clusters`, while
VMs ACM creates land in `acm-hosted-vms`. Otherwise both would reconcile the same
objects, each believing it owned them.

### The single-node install

Not the bootstrap-in-place SNO flow. With `bootstrapInPlace` set,
`create single-node-ignition-config` emits only
`bootstrap-in-place-for-live-iso.ign`, which expects to run from the RHCOS live
ISO. Without it, the ordinary flow emits `bootstrap.ign` and a 1.7 KiB
`master.ign` -- exactly the shape the TNF nodes already boot from an AMI with an
ignition pointer in user-data. Both variants were checked against the 4.22.10
installer before choosing; reusing the existing launch path was worth more than
matching the canonical SNO recipe.
