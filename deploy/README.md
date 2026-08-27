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
| `make infra` | *(CloudFormation, no playbook)* | 5 min |
| `make tnf` | `10-tnf-install.yml` | 60–90 min |
| `make gpu` | `20-base-gpu.yml` | 30–45 min |
| `make virt-acm` | `30-virt-acm.yml` | 45–60 min |
| `make guests` | `40-vcp-guests.yml` | 30–45 min |
| `make guest-gpu` | `50-guest-gpu.yml` | 30 min |

Each is re-runnable. `make tnf` skips the install if `auth/kubeconfig` already
exists on the bastion, but still reapplies the Pacemaker timeouts — which is
what you want after the TNF controller has recreated a stonith device.

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
