# GPU allocation

Each `g4dn.metal` carries **eight NVIDIA Tesla T4s** (PCI `10DE:1EB8`), so the
cluster has sixteen. They cannot all do both jobs.

## The constraint: a GPU has one driver

A GPU is bound either to the NVIDIA kernel driver, which lets containers use it,
or to `vfio-pci`, which lets a virtual machine take it whole. Not both.

The NVIDIA GPU Operator models this per **node**, not per GPU. With
`sandboxWorkloads.enabled: true`, a node is labelled:

```
nvidia.com/gpu.workload.config=container        # NVIDIA driver, pods
nvidia.com/gpu.workload.config=vm-passthrough   # vfio-pci, KubeVirt VMs
```

and the operator's `vfio-manager` binds *all* of that node's GPUs accordingly.
There is no supported per-GPU split within a node.

So with two nodes, the split is: one node's eight GPUs for containers, the
other node's eight for virtual machines.

## The default

```bash
# config/instance.env
export MASTER0_GPU_WORKLOAD=container        # 8x T4 -> pods on the base cluster
export MASTER1_GPU_WORKLOAD=vm-passthrough   # 8x T4 -> guest cluster worker VMs
```

Which gives:

| Node | Mode | Advertises | Consumed by |
|---|---|---|---|
| `master-0` | `container` | `nvidia.com/gpu: 8` | pods on the base cluster |
| `master-1` | `vm-passthrough` | `nvidia.com/TU104GL_Tesla_T4: 8` | KubeVirt VMs, i.e. guest cluster workers |

The two modes advertise **different resource names**, which is the quickest way
to tell whether the split took:

```bash
oc get nodes -o json | jq -r '.items[]
  | .metadata.name as $n
  | .status.allocatable | to_entries[]
  | select(.key | startswith("nvidia.com/"))
  | "\($n) \(.key)=\(.value)"'
```

A node labelled `vm-passthrough` that advertises `nvidia.com/gpu` means
`vfio-manager` did not take the GPUs, and every `hostDevices` request will fail
to schedule. The `gpu-operator` role asserts this rather than leaving it to be
discovered later.

## Changing the split

Edit `config/instance.env` and re-run:

```bash
cd deploy/ && make gpu
```

Relabelling a node makes the GPU Operator rebind its GPUs, which restarts the
driver daemonset on that node. Do it before creating guest clusters, not after
— a guest worker VM holding a GPU on a node being flipped to `container` mode
will not survive.

**Both nodes to `vm-passthrough`** gives 16 GPUs for guest clusters and none for
containers on the base cluster. Reasonable if the base cluster is purely a
hosting platform, but it means `make gpu`'s container-mode verification has
nothing to check.

**Both nodes to `container`** gives no passthrough GPUs, so `make guests` fails
its budget check before creating anything.

## The GPU budget

`make guests` does the arithmetic up front, because creating clusters that
cannot possibly schedule wastes half an hour before failing:

```
GUEST_CLUSTER_COUNT x GUEST_NODEPOOL_REPLICAS x GUEST_GPUS_PER_NODE
  <= allocatable nvidia.com/TU104GL_Tesla_T4
```

Defaults are 2 clusters x 1 worker x 2 GPUs = 4 of 8. Room for another two
clusters, or for four GPUs per worker.

## Where guest workers land

Only the `vm-passthrough` node advertises the passthrough resource, so **every
guest cluster worker VM schedules on that one node**. Two consequences:

- All guest workers compete for one machine's 96 vCPU and 384 GiB. The default
  worker is 8 cores and 32 GiB, so the node is not the binding constraint until
  roughly a dozen workers.
- Fencing that node takes every guest cluster's workers with it. The guest
  *control planes* run as pods and can reschedule to the other node; the
  workers cannot, because their GPUs are physically on the fenced machine.

That second point is worth knowing before running a fencing test against a rig
with guest clusters on it. It is a property of GPU passthrough, not a bug.

## What runs where

```
master-0  container mode
  NVIDIA driver daemonset, device plugin, DCGM exporter
  nvidia.com/gpu: 8   -> any pod on the base cluster can request one

master-1  vm-passthrough mode
  vfio-manager binds all 8 T4s to vfio-pci
  sandbox device plugin advertises nvidia.com/TU104GL_Tesla_T4: 8
  OpenShift Virtualization permits 10DE:1EB8 with externalResourceProvider: true
     -> KubeVirt VMs request it through spec.domain.devices.hostDevices
     -> guest NodePools request it through platform.kubevirt.hostDevices

inside each guest cluster
  the T4 is an ordinary PCI device on the VM
  NFD + GPU Operator in plain container mode (sandboxWorkloads off)
  nvidia.com/gpu: N   -> pods in the guest cluster can request one
```

The GPU Operator runs twice on two different levels, in two different modes, for
two different reasons — that is the part most worth holding onto.

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
