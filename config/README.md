# config/

Everything user-specific lives here. Both real files are gitignored.

| File | How to create it | What it is |
|---|---|---|
| `instance.env` | `cp instance.env.template instance.env` | AWS account, cluster shape, credentials, GPU layout |
| `pull-secret.json` | download from <https://console.redhat.com/openshift/install/pull-secret> | registry credentials for the base cluster and every guest cluster |

`make` targets in [`deploy/`](../deploy/) source `instance.env` themselves, so
there is no separate sync step.

## The values that are load-bearing

**`AVAILABILITY_ZONE`** — `g4dn.metal` is not offered in every AZ, and capacity
varies. A wrong value here fails at instance-launch time, after the network
stack is already up:

```bash
aws ec2 describe-instance-type-offerings --location-type availability-zone \
  --filters Name=instance-type,Values=g4dn.metal --region "$REGION" \
  --query 'InstanceTypeOfferings[].Location' --output text
```

**`BASTION_PRIVATE_IP`** — fixed rather than assigned, because the fencing
address written into `install-config.yaml` embeds it, and that has to be
decided before any instance exists. Changing it after install means editing a
cluster secret and rebuilding the stonith devices.

**`BMC_PASSWORD`** — the password Pacemaker authenticates to the Redfish shim
with. It reaches a cluster secret and the Pacemaker CIB. Not a placeholder.

**`MASTER0_GPU_WORKLOAD` / `MASTER1_GPU_WORKLOAD`** — which node's eight T4s go
to containers and which node's go to VM passthrough. See
[docs/gpu-allocation.md](../docs/gpu-allocation.md) for why this is per node
rather than per GPU.

**`BASE_DOMAIN`** — leave empty. The toolbox discovers the account's public
Route53 zone and places the cluster at `<CLUSTER_NAME>.<that zone>`, so `api`
and `*.apps` resolve from the internet and the installer's certificates match.
The zone name is issued per account, so hard-coding it means editing config on
every new one. Set it explicitly only when an account has more than one public
zone — `make doctor` lists them if it cannot choose.

**`ALLOWED_SSH_CIDR`** — SSH to the bastion only. Defaults to `0.0.0.0/0` so a
first run works. Narrow it.

**`ALLOWED_API_CIDR`** — the cluster API and ingress. Separate from the SSH rule
so that exposing the API never widens SSH access. Defaults to open, since a
publicly resolvable cluster nothing can reach is not much use.
