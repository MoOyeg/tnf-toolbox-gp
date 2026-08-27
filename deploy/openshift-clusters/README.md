# openshift-clusters/

Ansible. Five numbered stages plus teardown, all targeting one host: the
bastion.

## Why only the bastion

`api-int` resolves through a Route53 private hosted zone associated with the
VPC. A workstation cannot resolve it, and therefore cannot run
`openshift-install wait-for` or `oc`. So the bastion carries the clients and
every command runs there.

## Playbooks

| Playbook | Roles used |
|---|---|
| `10-tnf-install.yml` | `common`, `redfish-shim`, `loadbalancer`, `tnf-install` |
| `20-base-gpu.yml` | `gpu-passthrough`, `nfd`, `gpu-operator` |
| `30-virt-acm.yml` | `lvm-storage`, `cnv`, `acm` |
| `40-vcp-guests.yml` | `vcp-guest` |
| `50-guest-gpu.yml` | `nfd`, `gpu-operator` — against each guest kubeconfig |
| `clean-guests.yml` | — |
| `fetch-kubeconfig.yml` | — |

## Roles

| Role | What it owns |
|---|---|
| `common` | client downloads, and one reusable OLM operator install |
| `redfish-shim` | the Redfish fencing endpoint on the bastion |
| `loadbalancer` | haproxy and the ignition HTTP server |
| `tnf-install` | install-config, ignition, instance launch, bootstrap, Pacemaker timeouts |
| `gpu-passthrough` | IOMMU kernel arguments and the per-node workload-mode labels |
| `nfd` | Node Feature Discovery — base and guest clusters alike |
| `gpu-operator` | the NVIDIA GPU Operator, in either sandbox or plain container mode |
| `lvm-storage` | LVM Storage pinned to the EBS data volume |
| `cnv` | OpenShift Virtualization and the permitted host device |
| `acm` | MultiClusterHub and the HyperShift components |
| `vcp-guest` | the `hcp` CLI, and one guest cluster per iteration |

`nfd` and `gpu-operator` are deliberately cluster-agnostic: they act on whatever
`op_kubeconfig` names, which is how the same two roles serve the base cluster
and every guest.

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
advertises the resource its workload mode implies; `vcp-guest` asserts the
rendered manifests actually request a GPU. Each of these otherwise fails much
later and much less obviously.
