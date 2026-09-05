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
$EDITOR config/instance.env                       # AZ, key pair, BMC password
cp ~/Downloads/pull-secret.json config/

cd deploy/
make doctor        # read-only preflight -- run this first, it is cheap
make all           # infra -> tnf -> gpu -> virt-mce -> acm-site -> guests -> guest-gpu
```

Each stage is independently re-runnable:

| Stage | What it does | Measured |
|---|---|---|
| `make infra` | VPC, subnet, private DNS, bastion, fencing endpoint | 2 min |
| `make tnf` | install TNF 4.22 on two `g4dn.metal`, then verify it is healthy *as TNF* | 52 min |
| `make kubeconfig` | save every cluster's credentials locally and write `deploy/clusters/access.md` | seconds |
| `make iommu` | the IOMMU MachineConfig — **reboots both nodes** | 40 min |
| `make gpu` | NFD + NVIDIA GPU Operator (no reboots) | 10 min |
| `make virt-mce` | LVM Storage, OpenShift Virtualization, MultiCluster Engine | 15 min |
| `make guests` | the virtualized control-plane guest clusters | 30–45 min* |
| `make guest-gpu` | NFD + GPU Operator inside each guest | 30 min* |

Timings are from real runs in `eu-west-1`; `*` are still estimates. Most of
`make tnf` is `wait-for bootstrap-complete` (31 min) and `install-complete`
(14 min).

**The ordering is deliberate.** `make iommu` is the only stage that reboots the
cluster, and on two nodes each reboot takes it down to one. So it runs by
itself, after the cluster is confirmed healthy as TNF and after its credentials
are safely on your workstation — and it re-checks TNF health afterwards, because
a rollout that leaves the pair unable to re-form is a failure of *that* stage,
not a mysterious GPU problem two stages later. See
[docs/deploy-log.md](docs/deploy-log.md) for the run where that distinction was
learned.

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

### GPUs split per node, not per GPU

The NVIDIA GPU Operator assigns a whole node to one mode: its GPUs are bound
either to the NVIDIA driver (containers) or to `vfio-pci` (VM passthrough).
There is no supported way to split a single node's GPUs between the two.

With two nodes and eight T4s each, the default gives `master-0` to container
workloads on the base cluster and `master-1`'s eight T4s to the guest clusters.
Change it in `config/instance.env`. [docs/gpu-allocation.md](docs/gpu-allocation.md)
covers the trade-off, including what it means that every guest worker VM lands
on one node.

### Storage is pinned to EBS, deliberately

`g4dn.metal` ships two 900 GiB instance-store NVMe drives, and LVM Storage would
happily claim them if left to discover its own disks. Instance store does not
survive a stop/start — and **fencing is a stop/start**. The guest clusters'
hosted control-plane etcd lives on this storage, so the `lvm-storage` role pins
the volume group to the EBS data volume by its volume id and never touches the
instance store.

## Cost

Two `g4dn.metal` are roughly **$15.70/hour on demand** before storage and
transfer, and they are the entire bill for practical purposes. `make destroy`
when you are done; `make clean` keeps the network and bastion (and therefore the
fencing address) while removing the expensive part.

The G/VT vCPU quota needs to be at least 192 for two of them. `make doctor`
checks that, along with whether `g4dn.metal` is offered in your AZ at all.

## Repository layout

```
config/                  instance.env + pull secret (both gitignored)
deploy/
  aws-infra/             CloudFormation and the stack lifecycle scripts
    templates/           network / services / compute / bootstrap stacks
    scripts/             create, destroy, doctor, status, inventory, ssh
  openshift-clusters/    Ansible: five numbered stages plus teardown
    roles/               one role per layer, reused across base and guest clusters
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
- **40 static checks** over the templates — every Jinja template renders under
  `StrictUndefined`, the rendered `install-config.yaml` has the fencing shape
  TNF requires, the ignition pointers are valid JSON inside the CloudFormation
  parameter limit, haproxy drops bootstrap from the ingress backends, and every
  CloudFormation `Ref`/`GetAtt` resolves to something declared.
- **Playbook syntax checks** for all seven playbooks.

**This has now been run against real `g4dn.metal` hardware in AWS**, which
found eight bugs that no amount of static checking would have caught — a
package missing from a repository, a CLI that cannot encode an ignition config
as a parameter, a device name already claimed by instance store, an
idempotency guard checking a file the installer creates too early. Each one and
its fix is written up in [docs/deploy-log.md](docs/deploy-log.md).

Progress of that run is recorded there too, including which stages are
confirmed working end to end and which are still unproven.

Guest cluster **ingress is a known gap** — the KubeVirt provider expects a
`LoadBalancer` service, which needs MetalLB, and MetalLB's L2 mode does not work
in a VPC. Guest APIs are published through the hub's ingress with the `Route`
strategy and work; guest `*.apps` does not. The workaround is in
[docs/vcp-guests.md](docs/vcp-guests.md).

## Documentation

- [docs/architecture.md](docs/architecture.md) — how the pieces fit, and why
  `platform: none`
- [docs/fencing-on-aws.md](docs/fencing-on-aws.md) — the shim, timeouts, and
  how to test a fence
- [docs/gpu-allocation.md](docs/gpu-allocation.md) — the per-node split and how
  to change it
- [docs/vcp-guests.md](docs/vcp-guests.md) — guest clusters, GPU passthrough,
  and the ingress gap
- [tools/redfish-ec2/README.md](tools/redfish-ec2/README.md) — the shim in
  detail
- [docs/deploy-log.md](docs/deploy-log.md) — what actually broke on real
  hardware, and what fixed it
