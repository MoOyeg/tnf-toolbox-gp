# aws-infra/

CloudFormation, and the scripts that drive it. All of this runs on your
workstation with your AWS credentials — the bastion's instance profile is
deliberately limited to starting and stopping this cluster's own nodes.

## Stacks

| Template | Creates | Lifetime |
|---|---|---|
| `network-stack.yaml` | VPC, public subnet, two security groups, private hosted zone | the environment |
| `services-stack.yaml` | bastion, elastic IP, IAM role, the `api` / `api-int` / `*.apps` records | the environment |
| `compute-stack.yaml` | two `g4dn.metal` nodes and their EBS data volumes | the cluster |
| `bootstrap-stack.yaml` | the temporary bootstrap node | ~30 minutes |

The bootstrap node is a separate stack so removing it is a stack delete rather
than a stack update — the control-plane nodes are never touched.

A single public subnet is deliberate. A `platform: none` cluster needs no
AWS-managed load balancer, node addresses are private and fixed, and the only
inbound path is the bastion. A private subnet would add a NAT gateway and its
hourly cost to solve a problem this topology does not have.

## Scripts

| Script | Purpose |
|---|---|
| `doctor.sh` | read-only preflight: tools, config, credentials, AZ capacity, vCPU quota |
| `create-network.sh` | the network stack |
| `create-services.sh` | the bastion stack |
| `create-compute.sh` | the metal nodes — called by the `tnf-install` role once ignition exists |
| `create-bootstrap.sh` | the bootstrap node — same |
| `destroy-bootstrap.sh` | drop the bootstrap node after `bootstrap-complete` |
| `destroy-cluster.sh` | drop the cluster, keep the network and bastion |
| `destroy.sh` | drop everything (prompts for the cluster name) |
| `inventory.sh` | regenerate the Ansible inventory from recorded state |
| `status.sh` | stacks, instance power, bastion services, cluster health |
| `ssh.sh` | shell on the bastion |

`create-compute.sh` and `create-bootstrap.sh` take the RHCOS AMI and the
ignition pointer files as arguments. They are not meant to be run by hand: the
pointers only exist once `openshift-install create ignition-configs` has run on
the bastion.

## State

`instance-data/` (gitignored) holds one small file per recorded value — stack
ids, addresses, the fencing base URL, instance ids, data volume ids. Both
`common.sh` and the Ansible wrapper read from here, so the two halves of the
deploy cannot drift.

## The `doctor` checks worth reading

**`g4dn.metal` in your AZ** — it is not offered everywhere, and capacity
varies. Without this check the failure surfaces at instance launch, after the
network stack is already up.

**G/VT vCPU quota ≥ 192** — `g4dn.metal` is 96 vCPU, and two of them is a large
ask against a default quota.

**`BMC_PASSWORD` is not the placeholder** — it ends up in a cluster secret and
the Pacemaker CIB, and changing it after install means rebuilding the stonith
devices.
