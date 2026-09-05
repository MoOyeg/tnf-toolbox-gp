# GPU allocation

Each `g4dn.metal` carries **eight NVIDIA Tesla T4s** (PCI `10DE:1EB8`), so the
cluster has sixteen. They cannot all do both jobs.

## The constraint: a GPU has one driver, and the operator takes the node

A GPU is bound either to the NVIDIA kernel driver, which lets containers use it,
or to `vfio-pci`, which lets a virtual machine take it whole. Not both.

The NVIDIA GPU Operator models that per **node**, not per GPU. With
`sandboxWorkloads.enabled: true` a node is labelled

```
nvidia.com/gpu.workload.config=container        # NVIDIA driver, pods
nvidia.com/gpu.workload.config=vm-passthrough   # vfio-pci, KubeVirt VMs
```

and its `vfio-manager` binds *all* of that node's GPUs accordingly.

Splitting a single node between the two does not work, and it is worth knowing
exactly why, because the failure is quiet. A systemd unit can bind half the GPUs
to `vfio-pci` at boot, before kubelet, and it does -- the split is visibly
correct on a freshly rebooted node. Then the operator's driver container starts
and logs:

```
msg=Unbinding vfio-pci driver from all devices
```

It takes every GPU on the node, because it assumes it owns them. Nothing errors;
the node simply stops advertising passthrough GPUs some minutes later. Rebinding
afterwards is not possible either -- the unbind blocks while the driver holds the
device. This was measured on this rig, not inferred; defect 37 in
`docs/deploy-log.md` has the evidence.

So the choice is per node, and the only question is which mode each node is in.

## What this toolbox does: both nodes to passthrough

```
master-0   8x T4 -> vfio-pci
master-1   8x T4 -> vfio-pci
```

All sixteen T4s go to virtual machines, and **no pod on the base cluster can
request a GPU**. That is the deliberate trade: the workloads that want GPUs run
in the guest clusters, and giving both nodes to passthrough means a guest's
worker VMs can spread across both machines instead of all landing on the one
node that happened to hold the GPUs.

The alternative -- one node each way -- keeps GPU pods on the base cluster but
concentrates every guest worker on a single machine, so fencing it takes all of
them. Set `MASTER0_GPU_WORKLOAD=container` in `config/instance.env` if you want
that instead; the roles handle either.

NFD and the GPU Operator still install on the base cluster in both cases. With
both nodes on passthrough the operator runs its sandbox stack rather than the
container stack, so what the nodes advertise is
`nvidia.com/TU104GL_TESLA_T4`, not `nvidia.com/gpu`.

## Sizing the guests against it

Sixteen passthrough GPUs is the budget:

```bash
# config/instance.env
export GUEST_CLUSTER_COUNT=1         # guest clusters
export GUEST_NODEPOOL_REPLICAS=3     # workers each
export GUEST_GPUS_PER_NODE=2         # GPUs per worker
```

1 x 3 x 2 = 6 of the 16. `make guests-from-acm` checks that sum against what the
infra cluster actually advertises before it creates anything, because the
alternative is a NodePool that sits Pending for its whole timeout with the real
reason buried in a pod event.

## Where guest workers land

Both nodes advertise passthrough GPUs, so guest worker VMs can schedule on
either and the scheduler spreads them. That is the main reason for giving both
nodes to passthrough:

- A worker VM is not pinned to one machine by where the GPUs happen to be.
- Fencing a node costs half the passthrough capacity rather than all of it, and
  the workers that were not on it keep running. Their control planes are pods on
  the ACM hub, at the other site, and are unaffected either way.

A worker VM still cannot migrate while holding a passed-through GPU -- that is a
property of passthrough, not of this layout -- so fencing a node does destroy the
workers that were on it. The difference is that it is now some of them rather
than all of them.

## What runs where

```
master-0 and master-1, both vm-passthrough

  NVIDIA GPU Operator, sandboxWorkloads on
    vfio-manager binds all 8 T4s on each node to vfio-pci
    sandbox device plugin advertises them
    nvidia.com/TU104GL_TESLA_T4: 8 per node, 16 in total
    no nvidia.com/gpu anywhere -- no pod here can request a GPU

  OpenShift Virtualization
    permits 10DE:1EB8 with externalResourceProvider: true, so the operator's
    plugin is the one publishing them and KubeVirt only permits the resource
      -> KubeVirt VMs request it through spec.domain.devices.hostDevices
      -> guest NodePools request it through platform.kubevirt.hostDevices

inside each guest cluster
  the T4 is an ordinary PCI device on the VM
  NFD + GPU Operator in plain container mode (sandboxWorkloads off)
  nvidia.com/gpu: 2   -> pods in the guest cluster can request one
```

The GPU Operator runs twice, on two levels, in two different modes: sandbox on
the metal, where its job is to hand GPUs to virtual machines, and plain container
mode inside the guest, where the T4 is already an ordinary PCI device and there
is nothing left to pass through. That is the part most worth holding onto.

## Checking a GPU end to end

Not on the base cluster: with both nodes on passthrough it advertises no
`nvidia.com/gpu`, so this pod would stay Pending. What to check there is that
the passthrough resource exists:

```bash
oc get nodes -o custom-columns=\
NODE:.metadata.name,T4:.status.allocatable.'nvidia\.com/TU104GL_TESLA_T4'
```

The real test is inside a guest cluster, where the T4 is an ordinary PCI device:

```bash
export KUBECONFIG=deploy/clusters/acm/vcp-1.kubeconfig
oc run cuda-check --rm -it --restart=Never \
  --image=nvcr.io/nvidia/cuda:12.4.1-base-ubi9 \
  --overrides='{"spec":{"containers":[{"name":"cuda-check",
    "image":"nvcr.io/nvidia/cuda:12.4.1-base-ubi9","command":["nvidia-smi"],
    "resources":{"limits":{"nvidia.com/gpu":"1"}}}]}}'
```

If that lists a Tesla T4, a physical GPU on a bare metal EC2 instance has been
bound to vfio-pci, passed through a KubeVirt VM into a guest OpenShift cluster
at another site, and driven by a container there.
