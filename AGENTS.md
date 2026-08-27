# Working in this repository

Notes for anyone — human or agent — changing this code.

## What this is

Automation for a TNF 4.22 cluster on two AWS `g4dn.metal` bare metal instances,
which then hosts virtualized control-plane guest clusters with GPUs passed
through into their worker VMs. Read [README.md](README.md) first, then
[docs/architecture.md](docs/architecture.md).

## Before changing anything

```bash
make test
```

Runs in seconds, needs no AWS, no cluster, no credentials: 19 unit tests for
the Redfish shim, 40 static checks over the templates, and a syntax check on
every playbook. It catches the class of mistake that otherwise surfaces forty
minutes into a deploy.

`make verify` additionally runs shellcheck, yamlfmt and ansible-lint in
containers.

## Invariants that are easy to break

**The fencing hostname must equal the node name.** `install-config.yaml`'s
`controlPlane.fencing.credentials[].hostname` has to match what the node reports
to Kubernetes, which ignition pins via `/etc/hostname`. A mismatch produces a
Pacemaker stonith device aimed at a node that does not exist, and nothing
notices until the first real fence. Asserted in `tnf-install/tasks/finish.yml`
and in `hack/test-templates.py`.

**The master ignition pointer must `merge`, not `replace`.** `replace` discards
the local storage stanza, taking `/etc/hostname` with it, which breaks the
invariant above.

**Systems are addressed by logical name, never by instance id.** The fencing
address goes into `install-config.yaml` before any instance exists. Addressing
by instance id would be circular.

**`POWERED_OFF_STATES` must stay conservative.** Only `stopped` and
`terminated` map to `PowerState: Off`. Adding a transitional state to that set
would let Pacemaker conclude a fence succeeded against a node still running.

**LVM Storage must stay pinned to the EBS volume.** `g4dn.metal`'s
instance-store NVMe drives do not survive a stop/start, and a fence is a
stop/start. Letting LVM Storage auto-discover disks would put the guest
clusters' etcd on storage that a fence destroys.

**`BASTION_PRIVATE_IP` is fixed on purpose.** The fencing address embeds it.

## Conventions

- Config lives in `config/instance.env`. Playbooks receive it through
  `scripts/run-playbook.sh`, never by re-deriving it.
- Roles that can act on more than one cluster take `op_kubeconfig`. `nfd` and
  `gpu-operator` are used against both the base cluster and every guest.
- Long waits get a `rescue` that dumps the resource, its pods and recent events
  before failing.
- Waits are two-stage where a resource can report finished before it starts.
- Prefer asserting an invariant over asserting a state.

## Comments

Explain *why*, especially where a choice looks arbitrary or wrong. The reader
who needs the comment is the one wondering why the obvious thing was not done —
"instance store does not survive the stop/start that fencing performs" earns its
line; "create the namespace" does not.

## Things that have not been run against hardware

The whole thing. See the README section "What is verified, and what is not" for
the specific parts most likely to need adjustment on a first real deploy. If you
run it, please record what actually broke.
