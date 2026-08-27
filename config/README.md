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

**`ALLOWED_SSH_CIDR`** — defaults to `0.0.0.0/0` so a first run works. It
governs SSH plus the exposed API and ingress ports. Narrow it.
