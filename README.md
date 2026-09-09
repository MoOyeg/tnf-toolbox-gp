# tnf-toolbox-gp

Automation for a **Two-Node OpenShift with Fencing (TNF) 4.22 cluster on AWS
`g4dn.metal` bare metal**, which then hosts **virtualized control planes** —
guest OpenShift clusters whose worker VMs have real NVIDIA T4 GPUs passed
through to them.

Structure and conventions follow
[openshift-eng/two-node-toolbox](https://github.com/openshift-eng/two-node-toolbox);
no code is vendored from it (see [NOTICE](NOTICE)). The target is different
enough to be a separate repository: upstream runs the cluster as VMs on a
hypervisor instance, and this runs it on the metal so the GPUs are real PCI
devices on the OpenShift nodes.

## What it builds

```
ACM site  VPC 10.1.0.0/16              TNF site  VPC 10.0.0.0/16
├── bastion 10.1.0.5                   ├── bastion 10.0.0.5
│                                      │     ├── haproxy      api / api-int / *.apps
│                                      │     ├── redfish-ec2  Redfish → EC2 Stop/Start
│                                      │     └── ignition server
└── sno-0  m5zn.metal  10.1.0.10       ├── master-0  g4dn.metal  8× T4 → vfio-pci
      single-node OpenShift            └── master-1  g4dn.metal  8× T4 → vfio-pci
      ├── ACM + MCE + HyperShift             TNF 4.22, platform:none
      └── OpenShift Virtualization           ├── NFD + NVIDIA GPU Operator
                  │                          ├── LVM Storage on EBS gp3
                  │                          ├── OpenShift Virtualization
                  │                          └── MultiCluster Engine
                  │                                    ▲
                  └────────── VPC peering ─────────────┘
                    ACM drives TNF as its KubeVirt infra cluster:
                    HostedCluster + control-plane pods on the ACM cluster,
                    worker VMs and their GPUs on TNF. TNF is also imported
                    into the hub as a managed cluster, so ACM sees the
                    infrastructure and not just the clusters on it.
```

No nested virtualization anywhere. OpenShift Virtualization runs on the metal,
so a GPU travels exactly one hop — host PCI device → guest worker VM.

## Quick start

```bash
cp config/instance.env.template config/instance.env
$EDITOR config/instance.env
cp ~/Downloads/pull-secret.json config/           # must be config/pull-secret.json

cd deploy/
make doctor        # read-only preflight -- run this first, it is cheap
make all           # about three and a half hours, mostly waiting
```

Four things in `instance.env` need attention; the rest have working defaults:

| | |
|---|---|
| `REGION` / `AVAILABILITY_ZONE` | must agree, and `g4dn.metal` has to be offered there — `make doctor` checks both |
| `SSH_KEY_NAME` | the EC2 key pair. It does not have to exist: `make keypair` creates it, and the local key alongside it |
| `BMC_PASSWORD` | ships as `CHANGE-ME`, which `make doctor` rejects. It is the password Pacemaker fences with |
| `ALLOWED_SSH_CIDR` | ships as `0.0.0.0/0`. Narrow it to your own address |

`BASE_DOMAIN` is left blank on purpose — it is filled from the account's public
Route53 zone, so a sandbox that hands you a new domain needs no edit.

`make all` runs the stages below in order. Each is independently re-runnable and
idempotent, so a failure part-way through is resumed by re-running that stage
rather than starting over.

| Stage | What it does | Time |
|---|---|---|
| `make keypair` | create the SSH key pair from `instance.env`, or check the existing one matches | seconds |
| `make infra` | TNF site: VPC, subnet, split-horizon DNS, bastion, fencing endpoint | 2 min |
| `make acm-infra` | ACM site: its own VPC, subnet and bastion | 2 min |
| `make peering` | the VPC peering connection, and the private zones each side needs | 1 min |
| `make tnf` | install TNF 4.22 on two `g4dn.metal` **with the IOMMU enabled from first boot**, then verify it is healthy *as TNF* | 50 min |
| `make kubeconfig` | fetch every cluster's credentials and write `deploy/clusters/access.md` | seconds |
| `make iommu` | optional — checks the IOMMU and applies nothing on a cluster this repo built | seconds |
| `make gpu` | NFD + NVIDIA GPU Operator on the base cluster | 10 min |
| `make virt-mce` | LVM Storage, OpenShift Virtualization, MultiCluster Engine | 15 min |
| `make acm-site` | single-node OpenShift at the ACM site, then ACM on it, then import TNF into the hub | 56 min |
| `make hcp-make-guests-from-acm` | guest clusters created by ACM: control plane as pods on the hub, worker VMs and GPUs on TNF | 40 min |
| `make guest-gpu` | NFD + GPU Operator inside each guest | 33 min |

Timings are measured, from a full run in `eu-west-1`. Most of `make tnf` is
`wait-for bootstrap-complete` (31 min) and `install-complete` (14 min), and most
of `make acm-site` is the same two waits for the ACM cluster.

`make tnf` and `make acm-site` build different sites and can run at the same
time if you are in a hurry; `make acm-site` waits for TNF's kubeconfig at the one
point it needs it.

Two other guest targets exist and are not in `make all`:

- **`make guests`** — the same hosted topology, but created by TNF's own
  MultiCluster Engine instead of by ACM. `make all` uses the ACM one, because
  keeping the hub off the cluster it manages is the point of the two-site layout.
- **`make vcp-make-guests-from-acm`** — a *standalone* cluster whose every node
  is a VM, control plane included. Different topology and different mechanism:
  ACM installs it with the assisted installer rather than HyperShift, so the VMs
  boot a discovery ISO and are installed the way bare metal would be, and
  nothing of the cluster runs on the hub afterwards.

The two are worth telling apart:

| | control plane | workers | built by |
|---|---|---|---|
| `hcp-make-guests-from-acm` | pods on the ACM hub | KubeVirt VMs on TNF | HyperShift |
| `vcp-make-guests-from-acm` | KubeVirt VMs on TNF | KubeVirt VMs on TNF | assisted installer |

A hosted control plane is cheaper — it is pods, and it shares the hub's etcd
machinery. A standalone one costs three more VMs before a single workload runs,
and in exchange it survives the hub going away.

**`make iommu` is optional now.** `make tnf` writes the IOMMU MachineConfig
into the install manifests, so both nodes boot with `intel_iommu=on iommu=pt`
and there is no rollout to survive. On a cluster built by this repo the stage
finds the arguments already in place and applies nothing:

```
$ make iommu
intel_iommu=on iommu=pt already on every node and
100-master-gpu-passthrough is in place. Nothing to apply, and no reboot.
```

It is kept for a cluster built before that change, where it still does the day-2
rollout. Which is worth avoiding: adding those arguments to a *running* two-node
cluster reboots each node in turn, and a node that comes back is not reliably
re-added to the Pacemaker pair. The survivor rewrites corosync to a single-node
cluster to keep quorum — designed behaviour — and does not always undo it. The
returning node has no quorum, Pacemaker starts nothing on it, and neither side
can re-form the pair. That cost three clusters here before the arguments moved
to day 1; [docs/deploy-log.md](docs/deploy-log.md) defect 20 has the anatomy and
`make tnf-recover` is what to run when it happens.

**The rest of the ordering is still deliberate.** Credentials are fetched before
anything risky, and the stages that reboot or reconfigure re-check TNF health
afterwards, because a cluster that cannot re-form a Pacemaker pair is a failure
of *that* stage, not a mysterious GPU problem two stages later.

## When the cluster does not come back

A MachineConfig rollout reboots the two nodes one at a time, and the pair does
not always re-form. The surviving node rewrites corosync to a single-node
cluster to keep quorum — that part is designed — and does not always re-add the
peer when it returns. The node that comes back then has no quorum, Pacemaker
starts nothing on it, and its kubelet never reports. It has happened three times
here; `docs/deploy-log.md` defect 20 has the anatomy.

```bash
cd deploy/ && make tnf-recover
```

This works through the procedure in the OpenShift documentation — *Two-node
with Fencing → Post-installation troubleshooting and recovery → Manually
recovering from a disruption event when automated recovery is unavailable* — in
the order it gives, least invasive first, re-checking the cluster after each
step and stopping at the first one that fixes it:

| | step | runs when |
|---|---|---|
| 1 | `pcs resource cleanup` | always |
| 2 | `pcs resource cleanup etcd` | always |
| 3 | `pcs quorum unblock` | the peer is genuinely unreachable |
| 4 | `pcs stonith confirm <node>` | opt-in **and** the peer is unreachable |
| 5 | restore etcd from `/var/lib/etcd-backup` | opt-in |

Steps 3 and 4 are gated on the missing node actually being gone, which the role
checks by pinging it from the survivor rather than taking the cluster's word for
it. The documentation is blunt about why: confirming a fence for a node that is
in fact running leaves both halves believing they own etcd, which loses data
rather than time. Steps 4 and 5 need saying so as well:

```bash
make tnf-recover EXTRA_ARGS='-e tnf_recover_allow_fence_confirm=true'
make tnf-recover EXTRA_ARGS='-e tnf_recover_allow_etcd_restore=true'
```

It runs from whichever node is still `Ready`, not from a fixed one — the node
that is down is as likely to be the first as the second, and `oc debug` against
a `NotReady` node hangs for minutes before failing.

It gathers nothing for a support bundle — only what a step needs in order to
decide. The expensive probe (Pacemaker, via `oc debug`, about a minute) is
skipped whenever a one-call check has already shown the cluster is unwell, which
matters because the checks run after every step.

If every step leaves it broken the run fails, prints the state, and names the
commands to look further with. Two things it will not do unattended: replace a
node, and
restore the surviving node's saved `/etc/corosync/corosync.conf.<timestamp>`
over its single-node one. The second is the repair when the two nodes simply
disagree about membership and neither is down — which is not a case the
documented ladder covers.

## Credentials

`make kubeconfig` collects every cluster's credentials onto your workstation and
writes **`deploy/clusters/access.md`** -- one page listing, for each cluster, the
console URL, the API URL, the login, and the absolute path to its kubeconfig:

```
deploy/clusters/
├── access.md                     <- start here
├── tnf-gp/{kubeconfig,kubeadmin-password}
└── acm/{kubeconfig,kubeadmin-password}
    ├── vcp-1.kubeconfig, vcp-1-kubeadmin-password
    └── vcp-2.kubeconfig, vcp-2-kubeadmin-password
```

Guest credentials live under the hub that created them, because that is where
they come from: a guest cluster has no `auth/` directory, and HyperShift keeps
its kubeadmin password in a secret on the hub.

It is safe to re-run at any point, and skips any site whose cluster does not
answer -- so running it early, before the ACM site exists, collects TNF alone and
says so. The whole directory is gitignored; the page holds kubeadmin passwords in
plain text and is written `0600`.

Guest cluster consoles resolve publicly, on the infra cluster's apps wildcard one
level down (`...apps.<guest>.apps.<base cluster domain>`). Their APIs do not: they
are NodePorts on the hub's private address, reachable from the bastions.

`make status` summarises stacks, instance power state, bastion services and
cluster health at any point. `make destroy` removes everything.

The cluster is **publicly resolvable**. `BASE_DOMAIN` is left empty and the
toolbox discovers the account's public Route53 zone, putting the cluster at
`<cluster>.<that zone>` — so the fetched kubeconfig works from anywhere with no
`/etc/hosts` entries and no `--insecure-skip-tls-verify`, because the installer
issues the API certificate for exactly that name. Cluster nodes still resolve
the same names privately inside the VPC. `ALLOWED_API_CIDR` governs who can
reach the API and ingress and defaults to open; `ALLOWED_SSH_CIDR` is separate
and should stay narrow.

## The three decisions that shape this repo

### Fencing has to be invented, because EC2 has no BMC

TNF needs Pacemaker to be able to kill a misbehaving peer. The only stonith
agent OpenShift wires up is `fence_redfish` — `cluster-etcd-operator`'s
`pkg/tnf/pkg/pcs/fencing.go` hardcodes `FencingDeviceType: "fence_redfish"`, and
there is no code path that creates any other agent. (`fence_aws` appears in that
operator only in the status collector, as a resource type it will display if you
built one by hand.)

EC2 bare metal instances have no BMC and no Redfish service. So
[`tools/redfish-ec2/`](tools/redfish-ec2/) is a small Redfish endpoint that
serves the slice of the schema `fence_redfish` actually uses and turns resets
into `StopInstances` / `StartInstances`. It runs on the bastion, and the
fencing addresses in `install-config.yaml` point at it.

Consequence worth knowing before you measure anything: **a fence takes 5–15
minutes here**, because stopping a bare metal instance takes minutes where a BMC
cuts power in seconds. The playbooks raise Pacemaker's timeouts to match.
Details in [docs/fencing-on-aws.md](docs/fencing-on-aws.md).

### GPUs are assigned per node, not per GPU

The NVIDIA GPU Operator assigns a whole node to one mode: its GPUs are bound
either to the NVIDIA driver (containers) or to `vfio-pci` (VM passthrough).
There is no way to split a single node between the two — the operator's driver
container unbinds `vfio-pci` from every device on a node it manages, so a
per-GPU split is undone minutes after it is made. That was measured here, not
assumed; defect 37 in [docs/deploy-log.md](docs/deploy-log.md) has the evidence.

Both nodes default to `vm-passthrough`, so all sixteen T4s go to virtual
machines and a guest cluster's workers spread across both machines instead of
piling onto whichever node held the GPUs. The cost is that no pod on the base
cluster can request a GPU. Set `MASTER0_GPU_WORKLOAD=container` in
`config/instance.env` for one node each way instead.
[docs/gpu-allocation.md](docs/gpu-allocation.md) covers the trade-off.

### Storage is pinned to EBS, deliberately

`g4dn.metal` ships two 900 GiB instance-store NVMe drives, and LVM Storage would
happily claim them if left to discover its own disks. Instance store does not
survive a stop/start — and **fencing is a stop/start**. The guest clusters'
hosted control-plane etcd lives on this storage, so the `lvm-storage` role pins
the volume group to the EBS data volume by its volume id and never touches the
instance store.

## Cost

Bare metal is the entire bill for practical purposes:

| | |
|---|---|
| 2 x `g4dn.metal` (TNF) | ~$15.70/hour |
| 1 x `m5zn.metal` (ACM site) | ~$4.00/hour |
| 2 x `m5.large` bastions | ~$0.20/hour |

Roughly **$20/hour on demand** before storage and transfer, for both sites.

`make destroy` removes the TNF site and `make destroy-acm` the ACM one — run
both. `make clean` keeps the network and bastion, and therefore the fencing
address, while removing the expensive part.

The G/VT vCPU quota needs to be at least 192 for the two `g4dn.metal`.
`make doctor` checks that, along with whether the instance types are offered in
your AZ at all.

## Repository layout

```
config/                  instance.env + pull secret (both gitignored)
deploy/
  aws-infra/             CloudFormation and the stack lifecycle scripts
    templates/           network / services / compute / bootstrap / peering /
                         sno-compute stacks, for both sites
    scripts/             create, destroy, doctor, status, inventory, ssh
  openshift-clusters/    Ansible: the numbered stages, plus teardown
    roles/               one role per layer, reused across sites and guests
docs/                    architecture, fencing, GPU allocation, guest clusters
hack/                    lint and static template checks
tools/redfish-ec2/       the Redfish → EC2 fencing shim, with unit tests
```

Ansible targets exactly one host, the bastion. It is inside the VPC and so the
only host that can resolve `api-int` through the private hosted zone, which
means it is the only host that can drive an install.

## What is verified, and what is not

`make test` runs without AWS, a cluster, or credentials:

- **19 unit tests** for the Redfish shim — routing, both auth modes, the
  power-state mapping including every transitional EC2 state, and reset-type
  translation. The EC2 layer is stubbed.
- **70 static checks** over the templates — every Jinja template renders under
  `StrictUndefined`, the rendered `install-config.yaml` has the fencing shape
  TNF requires, the ignition pointers are valid JSON inside the CloudFormation
  parameter limit, haproxy drops bootstrap from the ingress backends, and every
  CloudFormation `Ref`/`GetAtt` resolves to something declared.
- **Playbook syntax checks** for all ten playbooks.

**This has been run end to end against real hardware in AWS**, and the whole
pipeline completes: both sites, GPUs reaching guest cluster workers, and
`nvidia.com/gpu` schedulable inside the guests.

Getting there found **37 defects** that no amount of static checking would have
caught — a CLI that cannot encode an ignition config as a parameter, a device
name already claimed by instance store, an Ansible precedence rule that silently
discards a role parameter sharing a name with an extra var, an ingress setting on
one cluster that stops a GPU operator two clusters away. Each one, its symptom
and its fix is written up in [docs/deploy-log.md](docs/deploy-log.md), which is
worth reading before a first run.

Guest cluster ingress **works**, and was the last thing to. The KubeVirt
provider expects a `LoadBalancer` for it, which needs MetalLB, whose L2 mode
does not work in a VPC — so the guest's control-plane services are published on
`NodePort` instead, and its `*.apps` wildcard is served by a passthrough route on
the infra cluster. That route has to be *admitted*, which needs
`wildcardPolicy: WildcardsAllowed` on the infra cluster's IngressController;
without it the guest console never comes up, its ClusterVersion never completes,
and the NVIDIA GPU Operator inside the guest refuses to start. See
[docs/hcp-guests.md](docs/hcp-guests.md).

## Documentation

- [docs/architecture.md](docs/architecture.md) — how the pieces fit, and why
  `platform: none`
- [docs/fencing-on-aws.md](docs/fencing-on-aws.md) — the shim, timeouts, and
  how to test a fence. `make tnf-recover` is what to run when a fence or a
  reboot leaves the pair unable to re-form
- [docs/gpu-allocation.md](docs/gpu-allocation.md) — why the split is per node,
  what both-nodes-passthrough costs, and how to change it
- [docs/hcp-guests.md](docs/hcp-guests.md) — guest clusters, GPU passthrough,
  and how their ingress is published
- [tools/redfish-ec2/README.md](tools/redfish-ec2/README.md) — the shim in
  detail
- [docs/deploy-log.md](docs/deploy-log.md) — what actually broke on real
  hardware, and what fixed it
