# Virtualized control-plane guest clusters

## What they are

A virtualized control plane is a hosted control plane with the KubeVirt
provider. The guest cluster's control plane — API server, etcd, controllers —
runs as ordinary pods on the hub, in a namespace of its own. Its worker nodes
are KubeVirt virtual machines on the same two `g4dn.metal` machines.

That is what makes the GPU story work here. The workers are VMs on a bare metal
OpenShift node, so a T4 travels one hop: host PCI device → `vfio-pci` → guest
worker VM. Nothing is nested.

ACM brings this in: `MultiClusterHub` installs MultiCluster Engine, and the
`hypershift` and `hypershift-local-hosting` components turn the hub into a
valid hosting cluster.

## How they are created

By `hcp create cluster kubevirt --render`, not by hand-written CRs.
`HostedCluster` and `NodePool` are large and version-sensitive, and asking the
CLI that shipped with this MCE build to render them keeps the manifests matched
to the operator that will reconcile them.

```bash
hcp create cluster kubevirt \
  --name vcp-1 --namespace clusters \
  --release-image quay.io/openshift-release-dev/ocp-release:4.22.0-x86_64 \
  --pull-secret ~/clusters/tnf-gp/pull-secret.json \
  --node-pool-replicas 1 \
  --cores 8 --memory 32Gi \
  --root-volume-size 64 --root-volume-storage-class lvms-vg1 \
  --etcd-storage-class lvms-vg1 \
  --host-device-name "nvidia.com/TU104GL_Tesla_T4,count:2" \
  --render
```

`--host-device-name` is the GPU passthrough. The rendered manifests are kept at
`~/clusters/<cluster>/guest-manifests/<guest>.yaml` on the bastion — when a
NodePool sits `Pending` a week later, that file is the difference between
reading what was asked for and guessing.

The role fails early rather than quietly if the CLI cannot do this:

- it asserts `--host-device-name` exists in `hcp create cluster kubevirt --help`
- it asserts the GPU resource name appears in the rendered output

Either failing means a cluster would come up without GPUs, which is worse than
not coming up.

## What `make guests` checks

Before creating anything, it compares demand against supply:

```
GUEST_CLUSTER_COUNT x GUEST_NODEPOOL_REPLICAS x GUEST_GPUS_PER_NODE
  <= allocatable nvidia.com/TU104GL_Tesla_T4
```

After each cluster, it proves the GPU actually arrived, from inside the guest:

```bash
oc --kubeconfig ~/clusters/tnf-gp/guests/vcp-1.kubeconfig \
  debug node/<worker> -- chroot /host lspci -d 10de: -nn
```

A `NodePool` that never reaches its replica count is almost always a VM that
cannot schedule, and on this rig that is almost always the GPU request — only
the `vm-passthrough` node advertises the resource. The failure path dumps the
NodePool, the VMIs, any Pending pods, and the per-node GPU capacity.

## Ingress: a known gap

**Guest cluster APIs work. Guest cluster `*.apps` does not.**

The guest API is published through the hub's ingress with the `Route` strategy,
which needs no load balancer and works fine. `oc` against a guest kubeconfig
works, workers join, operators install.

Guest *ingress* is different. The KubeVirt provider expects the guest's ingress
to be reachable through a `LoadBalancer` service created on the hub, and the
documented way to satisfy that on bare metal is MetalLB. MetalLB's L2 mode
announces addresses over ARP, and a VPC does not honour gratuitous ARP for
addresses it has not assigned to an ENI — so L2 mode does not work on AWS
without pinning each address to an ENI as a secondary private IP, which
defeats the failover it exists to provide.

The consequence is a guest cluster whose ingress ClusterOperator is degraded and
whose console is unreachable. Everything else — the API, the nodes, NFD, the GPU
Operator, GPU workloads — is unaffected.

### Working around it

Reach a guest workload without ingress:

```bash
export KUBECONFIG=~/clusters/tnf-gp/guests/vcp-1.kubeconfig
oc port-forward -n <namespace> svc/<service> 8080:80
```

Or expose it through the hub. The KubeVirt cloud provider maps a guest
`LoadBalancer` service to a hub service in the guest's hosted namespace; patch
that to `NodePort` and add a backend to the bastion's haproxy:

```bash
# on the hub
oc -n clusters-vcp-1 patch svc <service> -p '{"spec":{"type":"NodePort"}}'
oc -n clusters-vcp-1 get svc <service> -o jsonpath='{.spec.ports[0].nodePort}'
```

then add a `listen` block to `/etc/haproxy/haproxy.cfg` on the bastion pointing
at both nodes on that NodePort. This is not automated, and it has not been
tested here.

If guest ingress matters more than the GPU work, the honest fix is to run the
hub somewhere MetalLB works — real hardware on a flat L2 network — rather than
on EC2.

## Day-2

```bash
cd deploy/
make guest-gpu        # NFD + GPU Operator inside every guest cluster
make clean-guests     # destroy the guests, free the GPUs, leave the hub alone
make kubeconfig       # copy every kubeconfig back to the workstation
```

`clean-guests` uses `hcp destroy cluster kubevirt` rather than deleting the
`HostedCluster` CR. Deleting the CR alone can orphan the VMs, their PVCs, and
the hosted control-plane namespace.

To resize, change `GUEST_*` in `config/instance.env`, then
`make clean-guests && make guests && make guest-gpu`.

## Inspecting a guest from the hub

```bash
oc get hostedcluster -n clusters
oc get nodepool -n clusters
oc get vmi -n clusters-vcp-1                 # the worker VMs
oc get pods -n clusters-vcp-1                # the guest's control plane
oc get vmi -n clusters-vcp-1 -o yaml | grep -A5 hostDevices
```

The last one is the direct answer to "did this guest really get a GPU".
