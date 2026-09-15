# openshift-clusters/

Ansible. The numbered stages of the build, plus teardown and the utilities,
each targeting a bastion rather than a cluster directly.

## Why only the bastions

`api-int` resolves through a Route53 private hosted zone associated with the
VPC. A workstation cannot resolve it, and therefore cannot run
`openshift-install wait-for` or `oc`. So each bastion carries the clients and
every command runs there.

There are two, one per site. `[bastion]` is TNF's and `[acm_bastion]` is the ACM
hub's; a playbook runs against whichever site owns the cluster it is acting on.
The hub's stages reach TNF as a *managed* cluster — over the VPC peering
connection, with a kubeconfig ACM holds — not by Ansible hopping between
bastions.

## Playbooks

| Playbook | Runs on | Roles used |
|---|---|---|
| `10-tnf-install.yml` | `bastion` | `common`, `redfish-shim`, `loadbalancer`, `tnf-install` |
| `15-iommu.yml` | `bastion` | `tnf-install` (health, before and after), `gpu-passthrough` |
| `16-tnf-recover.yml` | `bastion` | `tnf-recover` |
| `20-base-gpu.yml` | `bastion` | `nfd`, `gpu-operator` |
| `30-virt-mce.yml` | `bastion` | `lvm-storage`, `common`, `cnv`, `acm` (MCE only) |
| `36-acm-site.yml` | `acm_bastion` | `common`, `loadbalancer`, `sno-cluster`, `lvm-storage`, `acm`, `observability` |
| `37-siteconfig.yml` | `acm_bastion` | `siteconfig`, `vcp-cluster`, `common`, `hcp-guest`, `gitops` |
| `40-hcp-guests.yml` | `bastion` | `common`, `hcp-guest` — TNF's own MultiCluster Engine, used by `make all` |
| `40-hcp-guests-from-acm.yml` | `acm_bastion` | `common`, `hcp-guest` — the same topology hosted on the hub instead; needs a metal hub |
| `41-vcp-clusters-from-acm.yml` | `acm_bastion` | `common`, `vcp-cluster` |
| `42-sites.yml` | `acm_bastion` | `hcp-guest`, `vcp-cluster`, `acm` |
| `50-guest-gpu.yml` | both | `nfd`, `gpu-operator` — against each guest kubeconfig |
| `60-app.yml` | both | `app` — against each guest kubeconfig |
| `clean-guests.yml` | both | — |
| `fetch-kubeconfig.yml` | both | — |

`42-sites.yml` — `make sites` — is the one that generates rather than installs.
It writes a `ClusterInstance` per cluster into `sites/` and `sites-infra/` for
Argo CD to reconcile, puts the secrets that must not reach Git straight onto the
hub, and harvests whatever the hub has finished building. It is meant to be
re-run: a cluster that does not exist yet is skipped and picked up next time.

## Roles

| Role | What it owns |
|---|---|
| `common` | client downloads, and one reusable OLM operator install |
| `redfish-shim` | the Redfish fencing endpoint on the bastion |
| `loadbalancer` | haproxy and the ignition HTTP server |
| `tnf-install` | install-config, ignition, instance launch, bootstrap, Pacemaker timeouts |
| `tnf-recover` | the documented TNF recovery steps, least invasive first |
| `gpu-passthrough` | IOMMU kernel arguments and the per-node workload-mode labels |
| `nfd` | Node Feature Discovery — base and guest clusters alike |
| `gpu-operator` | the NVIDIA GPU Operator, in either sandbox or plain container mode |
| `lvm-storage` | LVM Storage pinned to the EBS data volume |
| `cnv` | OpenShift Virtualization and the permitted host device |
| `acm` | MultiClusterHub and the HyperShift components |
| `sno-cluster` | the single-node ACM cluster, and its credential for the TNF infra cluster |
| `observability` | MultiCluster Observability, right-sizing, and the Perses dashboard |
| `siteconfig` | the SiteConfig operator on the hub |
| `gitops` | OpenShift GitOps and the fleet ApplicationSet |
| `hcp-guest` | the `hcp` CLI, the hosted install template, and one guest cluster per iteration |
| `vcp-cluster` | the all-VM cluster: CIM, its install templates, the VMs, the discovery ISO |
| `app` | the visual inspection app, built and deployed inside a guest |

`nfd` and `gpu-operator` are deliberately cluster-agnostic: they act on whatever
`op_kubeconfig` names, which is how the same two roles serve the base cluster
and every guest.

Several roles are entered more than once in a play, for different task files
rather than the whole role — `tnf-install` for its health check either side of
the IOMMU reboots, `acm` to import a cluster after installing the hub,
`vcp-cluster` and `hcp-guest` to write install templates in `37` and site
definitions in `42`.

## Variables

Three layers, most specific winning:

1. `roles/*/defaults/main.yml` — role defaults
2. `group_vars/all.yml` — cross-role defaults and derived values
3. extra vars from `scripts/run-playbook.sh` — everything in
   `config/instance.env` plus the recorded stack outputs

Never edit the inventory or pass `-e` for something `instance.env` already
covers; the wrapper regenerates the extra vars on every run.

## Conventions

**Waits are two-stage where a resource can look finished before it starts.**
`gpu-passthrough`'s MCP wait polls for `Updating=True` first, because a pool
that has not begun still reports `Updated=True` — waiting only for the second
condition returns immediately and races the reboot.

**Failure paths collect diagnostics.** Operator installs, ClusterPolicy waits
and NodePool waits all have a `rescue` that dumps the resource, the pods, and
the events before failing. On a run that takes ninety minutes, failing with the
reason attached is worth the extra lines.

**Assertions guard invariants, not just states.** `tnf-install` asserts that
node names match the fencing credentials; `gpu-operator` asserts each node
advertises the resource its workload mode implies; `hcp-guest` asserts the
rendered manifests actually request a GPU. Each of these otherwise fails much
later and much less obviously.
