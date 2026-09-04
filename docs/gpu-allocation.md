# GPU allocation

Each `g4dn.metal` carries **eight NVIDIA Tesla T4s** (PCI `10DE:1EB8`), so the
cluster has sixteen. They cannot all do both jobs.

## The constraint: a GPU has one driver

A GPU is bound either to the NVIDIA kernel driver, which lets containers use it,
or to `vfio-pci`, which lets a virtual machine take it whole. Not both. That part
is real and unavoidable.

What is *not* forced is doing it a whole node at a time. The NVIDIA GPU Operator
models the choice per **node**: with `sandboxWorkloads.enabled: true` a node is
labelled

```
nvidia.com/gpu.workload.config=container        # NVIDIA driver, pods
nvidia.com/gpu.workload.config=vm-passthrough   # vfio-pci, KubeVirt VMs
```

and its `vfio-manager` binds *all* of that node's GPUs accordingly. Following
that model with two nodes means one machine's eight GPUs serve containers and
the other's eight serve VMs -- and then every GPU-bearing VM in the environment
is on one machine. Losing it loses all of them, and no VM can ever be scheduled
anywhere else.

## What this toolbox does instead: half of every node

Each node keeps half its GPUs for containers and passes the other half through:

```
master-0   4x T4 -> NVIDIA driver     4x T4 -> vfio-pci
master-1   4x T4 -> NVIDIA driver     4x T4 -> vfio-pci
```

The totals are identical -- eight of each -- but neither capability is stranded
on one machine, and guest worker VMs can be spread across both.

The operator cannot express this, so it is not asked to. `sandboxWorkloads` is
disabled, and a systemd unit shipped by the IOMMU MachineConfig
(`gpu-vfio-split.service`) binds the passthrough half at boot, before kubelet
starts:

1. enumerate this node's GPUs from `/sys/bus/pci/devices` by vendor and device id
2. sort by PCI address and take the first half
3. unbind anything already holding them, set `driver_override=vfio-pci`, re-probe

It computes the half itself rather than being handed a list of PCI addresses,
because a MachineConfig applies to a whole pool: one unit has to be correct on
every node without knowing any node's addresses.

The GPU Operator's driver container then loads `nvidia.ko` and finds only the
GPUs that are still free, so it advertises `nvidia.com/gpu: 4` per node. KubeVirt
discovers the `vfio-pci`-bound ones and advertises them under
`permittedHostDevices` as `nvidia.com/TU104GL_TESLA_T4: 4` per node -- which is
why `externalResourceProvider` is **false**: with the operator's sandbox device
plugin switched off, KubeVirt is the one doing the advertising.

Both halves are verified rather than assumed. `make iommu` counts the drivers
actually bound on each node and fails if either half is empty, and `make
virt-mce` fails if any node advertises no passthrough GPUs.

## Sizing the guests against it

Eight passthrough GPUs is the budget, and the guest clusters spend it:

```bash
# config/instance.env
export GUEST_CLUSTER_COUNT=1         # guest clusters
export GUEST_NODEPOOL_REPLICAS=3     # workers each
export GUEST_GPUS_PER_NODE=2         # GPUs per worker
```

1 x 3 x 2 = 6 of the 8. `make guests-from-acm` checks that sum against what the
infra cluster actually advertises before it creates anything, because the
alternative is a NodePool that sits Pending for its whole timeout with the real
reason buried in a pod event.

The three variables trade against each other inside that budget: a second guest
cluster at this size would need 12 and fail the check, but two clusters of three
workers with one GPU each fits, as would one cluster of four workers with two.

## Changing the split

The split is computed on the node, as half of whatever it finds, so there is no
per-node setting to edit. To change the ratio, change the arithmetic in
`roles/gpu-passthrough/templates/gpu-vfio-split.sh.j2` -- it is three lines --
and re-run:

```bash
cd deploy/ && make iommu
```

That reapplies the MachineConfig and reboots both nodes serially, which is why
it lives in the IOMMU stage rather than `make gpu`.

**Do it before creating guest clusters, not after.** Rebinding a GPU that a
running worker VM is holding will not go well, and the VM cannot follow its GPU
to another machine.

To give everything to containers, drop the systemd unit from the MachineConfig;
`make guests` then fails its budget check before creating anything, because
nothing advertises the passthrough resource.

## Where guest workers land

Both nodes advertise passthrough GPUs, so guest worker VMs can schedule on
either, and the scheduler spreads them. This is the main practical gain over
dedicating one node:

- A worker VM is no longer pinned to one machine by the location of its GPUs.
- Fencing a node costs half the passthrough capacity rather than all of it, and
  the guest workers that were not on it keep running. Their control planes are
  pods on the ACM hub, at the other site, and are unaffected either way.

A worker VM still cannot migrate while holding a passed-through GPU -- that is a
property of passthrough, not of this layout -- so fencing a node does destroy the
workers that were on it. The difference is that it is now some of them rather
than all of them.

## What runs where

```
master-0 and master-1, both the same

  gpu-vfio-split.service, before kubelet
    binds the first 4 T4s by PCI address to vfio-pci

  NVIDIA GPU Operator, sandboxWorkloads off
    driver daemonset, device plugin, DCGM exporter
    finds only the 4 GPUs still free
    nvidia.com/gpu: 4   -> any pod on the base cluster can request one

  OpenShift Virtualization
    permits 10DE:1EB8 with externalResourceProvider: false, so KubeVirt's own
    device plugin advertises the vfio-pci-bound ones
    nvidia.com/TU104GL_TESLA_T4: 4
      -> KubeVirt VMs request it through spec.domain.devices.hostDevices
      -> guest NodePools request it through platform.kubevirt.hostDevices

inside each guest cluster
  the T4 is an ordinary PCI device on the VM
  NFD + GPU Operator in plain container mode (sandboxWorkloads off)
  nvidia.com/gpu: N   -> pods in the guest cluster can request one
```

The GPU Operator runs on two levels for two different reasons, and on neither of
them does it own the passthrough half: on the base cluster a systemd unit takes
those GPUs before the operator starts, and inside a guest there is nothing left
to pass through. That is the part most worth holding onto.

## Checking a GPU end to end

On the base cluster:

```bash
oc run cuda-check --rm -it --restart=Never \
  --image=nvcr.io/nvidia/cuda:12.4.1-base-ubi9 \
  --overrides='{"spec":{"containers":[{"name":"cuda-check",
    "image":"nvcr.io/nvidia/cuda:12.4.1-base-ubi9","command":["nvidia-smi"],
    "resources":{"limits":{"nvidia.com/gpu":"1"}}}]}}'
```

Inside a guest cluster, the same command with that guest's kubeconfig. If it
lists a Tesla T4, a physical GPU on a bare metal EC2 instance has been passed
through a KubeVirt VM into a guest OpenShift cluster and driven by a container
there.
