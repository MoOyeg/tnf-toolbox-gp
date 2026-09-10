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
| `make hcp-make-guests-from-acm` | `40-hcp-guests-from-acm.yml` | 40 min |
| `make guest-gpu` | `50-guest-gpu.yml` | 33 min |

Not in `make all`:

| Target | Playbook | For |
|---|---|---|
| `make guests` | `40-hcp-guests.yml` | guests hosted by TNF's own MCE instead of ACM |
| `make vcp-make-guests-from-acm` | `41-vcp-clusters-from-acm.yml` | a whole cluster on VMs, control plane included |
| `make tnf-recover` | `16-tnf-recover.yml` | a TNF pair that did not re-form after a reboot |
| `make app` | `60-app.yml` | the visual inspection app, in every guest cluster |

Each is re-runnable. `make tnf` skips the install if `auth/kubeconfig` already
exists on the bastion, but still reapplies the Pacemaker timeouts — which is
what you want after the TNF controller has recreated a stonith device.
`make tnf` writes the IOMMU MachineConfig into the install manifests, so the
nodes boot with `intel_iommu=on iommu=pt` and `make iommu` has nothing to do on
a cluster this repo built. It is kept for clusters built before that, where it
still performs the day-2 rollout — two serial reboots, and the operation that
has split the Pacemaker pair three times.

`make tnf-recover` is the one to reach for when a stage stops with "the cluster
is not healthy as a two-node fencing cluster". It works through the documented
recovery steps in order and stops at the first that helps; the destructive ones
are opt-in and are skipped anyway while the missing node still answers. See the
README's "When the cluster does not come back".

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
