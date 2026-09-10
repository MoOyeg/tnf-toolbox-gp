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
  --name hcp-1 --namespace clusters \
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
oc --kubeconfig ~/clusters/tnf-gp/guests/hcp-1.kubeconfig \
  debug node/<worker> -- chroot /host lspci -d 10de: -nn
```

A `NodePool` that never reaches its replica count is almost always a VM that
cannot schedule, and on this rig that is almost always the GPU request — only
the `vm-passthrough` node advertises the resource. The failure path dumps the
NodePool, the VMIs, any Pending pods, and the per-node GPU capacity.

## Ingress: how it is published, and what it took

**Guest cluster APIs and `*.apps` both work.** This was the last thing in the
build to come right, and the way it failed is worth keeping.

### The control-plane services

`hcp create cluster kubevirt` renders the API with
`servicePublishingStrategy: LoadBalancer`. On `platform: none` nothing
provisions one, so it stays `<pending>` and the cluster never comes up.

Switching it to `Route` removes that load balancer and creates another:
HyperShift then deploys a private router to serve the route and exposes *that*
with a `LoadBalancer`, which is equally stuck. The control-plane-operator
reconciles the Service back within seconds of being patched, so it cannot be
worked around after creation.

The documented answer is MetalLB on the hosting cluster, which does not fit
here: MetalLB's L2 mode announces over ARP, and a VPC does not honour
gratuitous ARP for addresses it has not assigned to an ENI.

So all four services are rewritten to **`NodePort`** at render time, on the hub
node's own address. No load balancer is needed, and with no `Route`-strategy
service left HyperShift never creates the private router at all. The guest's
worker VMs run on the infra cluster and reach that address across the VPC
peering connection.

### The `*.apps` wildcard

HyperShift handles this itself, and better than expected: it creates a wildcard
`Subdomain` route on the **infra** cluster —
`https.apps.<guest>.apps.<infra domain>` — pointing at a passthrough service for
the guest's router. DNS already resolves, because a wildcard record synthesises
for deeper names.

What it cannot do is admit that route. An OpenShift IngressController refuses
wildcard routes unless told otherwise, so both routes sat at `RouteNotAdmitted`
and nothing said so anywhere useful. `30-virt-mce.yml` sets:

```yaml
routeAdmission:
  wildcardPolicy: WildcardsAllowed
```

Until it did, the consequences surfaced two clusters away and looked unrelated:
guest ingress unreachable, so the guest's console operator never went Available,
so its ClusterVersion stayed `state=Partial`, so the NVIDIA GPU Operator inside
the guest refused to start with `failed to find Completed Cluster Version` and
never installed a driver. One `wildcardPolicy` on the infra cluster cleared the
whole chain.

### Checking it

```bash
# on the infra cluster: both routes should be Admitted=True
oc get route -n <infra namespace> -o custom-columns=\
HOST:.spec.host,ADMITTED:.status.ingress[0].conditions[0].status

# from anywhere: the guest console answers on the infra cluster's wildcard
curl -sk -o /dev/null -w '%{http_code}\n' \
  https://console-openshift-console.apps.<guest>.apps.<infra domain>/
```

The guest API is a NodePort on a private address, so it answers from the
bastions rather than from a workstation; the consoles answer from anywhere.

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
oc get vmi -n clusters-hcp-1                 # the worker VMs
oc get pods -n clusters-hcp-1                # the guest's control plane
oc get vmi -n clusters-hcp-1 -o yaml | grep -A5 hostDevices
```

The last one is the direct answer to "did this guest really get a GPU".
