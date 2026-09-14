# deploy/

Everything is driven from here.

```bash
make help          # full command reference
make doctor        # read-only preflight -- run this first
make all           # the whole stack, end to end
```

## Two halves

**`aws-infra/`** — CloudFormation and the scripts that drive it. Runs on your
workstation, using your AWS credentials.

**`openshift-clusters/`** — Ansible, five numbered stages. Runs against the
bastion, which is the only host that can resolve `api-int`.

The seam between them is `deploy/aws-infra/instance-data/`: the infra scripts
record stack outputs there, and `openshift-clusters/scripts/run-playbook.sh`
folds them into every playbook as extra vars alongside `config/instance.env`.
So there is one source of truth, and playbooks never re-derive a value a script
already decided.

## Stages

| Target | Playbook | Roughly |
|---|---|---|
| `make keypair` | *(AWS CLI, no playbook)* | seconds |
| `make infra` | *(CloudFormation, no playbook)* | 5 min |
| `make acm-infra` | *(CloudFormation, no playbook)* | 2 min |
| `make peering` | *(CloudFormation, no playbook)* | 1 min |
| `make tnf` | `10-tnf-install.yml` | 50 min |
| `make kubeconfig` | `fetch-kubeconfig.yml` | seconds |
| `make iommu` | `15-iommu.yml` | seconds — optional, see below |
| `make gpu` | `20-base-gpu.yml` | 10 min |
| `make virt-mce` | `30-virt-mce.yml` | 15 min |
| `make acm-site` | `36-acm-site.yml` | 56 min |
| `make siteconfig` | `37-siteconfig.yml` | 10 min |
| `make hcp-make-guests-from-acm` | `40-hcp-guests-from-acm.yml` | 40 min |
| `make guest-gpu` | `50-guest-gpu.yml` | 33 min |

Not in `make all`:

| Target | Playbook | For |
|---|---|---|
| `make guests` | `40-hcp-guests.yml` | guests hosted by TNF's own MCE instead of ACM |
| `make vcp-make-guests-from-acm` | `41-vcp-clusters-from-acm.yml` | a whole cluster on VMs, control plane included |
| `make siteconfig` | `37-siteconfig.yml` | SiteConfig operator, install templates, OpenShift GitOps |
| `make sites` | `42-sites.yml` | write the fleet's site definitions, harvest what exists |
| `make tnf-recover` | `16-tnf-recover.yml` | a TNF pair that did not re-form after a reboot |
| `make app` | `60-app.yml` | the visual inspection app, in every guest cluster |

Each is re-runnable. `make tnf` skips the install if `auth/kubeconfig` already
exists on the bastion, but still reapplies the Pacemaker timeouts — which is
what you want after the TNF controller has recreated a stonith device.
`make tnf` writes the IOMMU MachineConfig into the install manifests, so the
nodes boot with `intel_iommu=on iommu=pt` and `make iommu` has no *kernel
arguments* to apply on a cluster this repo built — it detects that and skips the
rollout, so no reboot happens. It is still required, and is not an optional
stage: it is also the only thing that labels the nodes
`nvidia.com/gpu.workload.config=vm-passthrough`. Without that label the GPU
Operator leaves them on `sandboxWorkloads.defaultWorkload` (container), the GPUs
stay bound to the NVIDIA driver instead of `vfio-pci`, no passthrough resource
is ever advertised, and `make virt-mce` fails minutes later at
`discover-gpu-resource` with an error that points at GPUs rather than at a
missing label.

On a cluster built before the MachineConfig moved into the install manifests it
also performs the day-2 rollout — two serial reboots, and the operation that has
split the Pacemaker pair three times.

`make tnf-recover` is the one to reach for when a stage stops with "the cluster
is not healthy as a two-node fencing cluster". It works through the documented
recovery steps in order and stops at the first that helps; the destructive ones
are opt-in and are skipped anyway while the missing node still answers. See the
README's "When the cluster does not come back".

## Two ways to build a cluster

Every cluster except TNF can be described as a `ClusterInstance` and built by
the SiteConfig operator from Git, instead of by the Ansible role that creates
the install resources directly. Both paths still exist and produce the same
clusters:

| | Ansible builds it | Git builds it |
|---|---|---|
| hosted guests | `make hcp-make-guests-from-acm` | `make siteconfig` then `make sites` |
| a whole cluster on VMs | `make vcp-make-guests-from-acm` | `make siteconfig` then `make sites` |

`make siteconfig` is the one-time setup: it enables the SiteConfig operator on
the hub, enables Central Infrastructure Management and the release
`ClusterImageSet` every site definition names, creates the install template each
profile is rendered from, and installs OpenShift GitOps with two ApplicationSets
— one watching `sites/` for the hub, one watching `sites-infra/` for the infra
cluster.

`make sites` then writes one folder per cluster under each, and puts on the
clusters the things that must not be committed: each site's secrets, and the
DataVolume holding the discovery ISO. **Committing and pushing those folders is
the last step and it is yours** — the hub reconciles against Git, not against
the run that generated the files.

`make sites` is re-runnable and expects to be re-run. On the first pass a
cluster does not exist yet, so there is no kubeconfig to harvest, no agents to
approve and no InfraEnv to import an ISO from; it does what it can and says what
it skipped. Run it again once Argo CD has caught up and it picks up the rest —
harvesting each guest's kubeconfig into `clusters/guests/` so `make guest-gpu`
and `make app` can find it, approving the machines that registered themselves,
and finishing the import that makes a cluster show as Ready in ACM.

TNF is deliberately not in this table. It is installed by
`openshift-baremetal-install` onto EC2 metal, and the assisted installer cannot
reach that hardware: a `BareMetalHost` needs Ironic to attach a discovery ISO
over Redfish virtual media, EC2 has no virtual media to attach, and the Redfish
shim on the bastion serves power actions only. TNF is also the infra cluster
every one of these guests runs on, so it has to exist first either way.

## Teardown

| Target | Removes | Keeps |
|---|---|---|
| `make clean-guests` | the guest clusters | everything else |
| `make clean` | the OpenShift cluster and both metal nodes | VPC, DNS, bastion, fencing address |
| `make destroy` | everything, including the EBS data volumes | nothing |

`make clean` keeps the bastion deliberately: its private address is embedded in
every fencing address, so keeping it means a rebuild does not need a new
install-config.

## Passing extra arguments

```bash
make gpu   EXTRA_ARGS="-vv"
make tnf   EXTRA_ARGS="--start-at-task='Wait for install-complete'"
make guests EXTRA_ARGS="-e guest_gpus_per_node=4"
```
