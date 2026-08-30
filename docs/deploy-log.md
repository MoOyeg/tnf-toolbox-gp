# Deploy log

What actually broke on real hardware, and what fixed it. Kept because the
failures are more useful than the successes: every one of these was invisible
to static checking and would have cost someone else the same hour.

## Run 1 — 2026-08-27, us-east-2, account 749168934378

`g4dn.metal` in `us-east-2b`, OCP 4.22.10, G/VT vCPU quota 768.

### 1. Bastion user-data died on a package that does not exist

`dnf install -y podman haproxy python3 python3-pip jq` under `set -e`.
**`podman` is not in the Amazon Linux 2023 repositories.** The install failed,
`set -e` aborted the script, and everything after it — `hostnamectl`, the
directory creation — never ran. cloud-init reported `status: error` and the
bastion came up with the wrong hostname and no haproxy.

Nothing needed podman: it was left over from the shim's container deployment
path, which Ansible does not use (it runs the shim from a venv under systemd).

*Fix:* user-data now does one thing, sets the hostname, and does not abort. Every
package is installed by the roles, which are idempotent and re-runnable —
whereas user-data runs once, at first boot, and fixing it means replacing the
instance. `containers.podman` was also dropped from the collection requirements.

### 2. A no-op stack update hung forever

`create_or_update_stack` piped `aws cloudformation update-stack` into
`grep -q 'No updates are to be performed'` to detect a no-op. Under
`set -o pipefail` the pipeline inherits the CLI's **non-zero** exit even when
the grep matched, so the `if !` branch ran `aws cloudformation wait
stack-update-complete` against a stack that was never going to update. It sat
there until killed.

*Fix:* capture the output and exit code separately and branch on both.

### 3. CloudFormation could not carry an ignition config at all

```
Error parsing parameter '--parameters': Expected: '=', received: '"'
ParameterKey=Master0UserData,ParameterValue={"ignition":{"version":"3.4.0",...
```

The CLI's shorthand `--parameters` syntax cannot carry a value containing `=`,
`,` or a quote. An ignition pointer is all three. This made the master node
user-data impossible to pass — the core mechanism of the whole install.

*Fix:* parameters are built as JSON with `jq` and passed as `--parameters
file://...`. Values are split on the *first* `=` only, so a password containing
one survives. Covered now by `hack/test-common-sh.sh`, which round-trips a real
ignition config and a password containing `"`, `,` and `=`.

### 4. "Already installed" was checked against a file the installer creates early

The idempotency guard skipped the install when `auth/kubeconfig` existed. But
`openshift-install create ignition-configs` writes that file — so a run that
failed *before launching a single instance* left one behind. The next run
concluded the cluster was installed, skipped the install entirely, and went
straight to configuring Pacemaker fencing on a cluster that did not exist.

*Fix:* the guard asks the cluster (`oc get clusterversion`), not the filesystem.
A related fix in `render.yml` discards ignition from an attempt that never
launched, so corrected install-config changes cannot be silently paired with
stale ignition.

### 5. `/dev/xvdb` is already in use on `g4dn.metal`

```
Invalid value '/dev/xvdb' for unixDevice. Attachment point /dev/xvdb is
already in use
```

`g4dn.metal` ships two 900 GiB instance-store NVMe drives, and the AMI's block
device mapping already claims the early names. Both instances launched fine —
only the EBS data volume attachment failed, which rolled the whole stack back.

*Fix:* the data volume attaches at `/dev/sdf`, the range AWS reserves for
additional EBS volumes, and it is a parameter now. On Nitro the name is
cosmetic anyway; the `lvm-storage` role finds the volume by id under
`/dev/disk/by-id`, which is why that design choice survived this bug unchanged.

### 6. A rolled-back stack blocked every retry

A stack in `ROLLBACK_COMPLETE` cannot be updated, only deleted and recreated, so
the retry after bug 5 would have failed with an unhelpful "is in
ROLLBACK_COMPLETE state and can not be updated".

*Fix:* `create_or_update_stack` deletes a stack found in `ROLLBACK_COMPLETE`,
`ROLLBACK_FAILED`, `CREATE_FAILED` or `REVIEW_IN_PROGRESS`, and waits out any
`*_IN_PROGRESS` state before deciding.

### 7. Ansible configuration would not load at all

Two problems at once: `stdout_callback = yaml` resolves to
`community.general.yaml`, **removed in community.general 12**, and the
`community.general` version Galaxy installed does not support ansible-core
2.14. Every playbook failed before running a task.

Neither `community.general` nor `amazon.aws` was used anywhere —
CloudFormation is driven by the scripts, not by Ansible.

*Fix:* the requirements list only `kubernetes.core` and `ansible.posix`, and
`ansible.cfg` uses the built-in default callback with `result_format = yaml`.

### 8. `kubernetes.core` had no client library to import

`kubernetes.core.k8s` executes on the target, so the `kubernetes` Python
library has to be on the bastion. It was not installed anywhere, and would have
failed at the first k8s task in stage 2 — three stages after the mistake.

*Fix:* the `common` role installs it and then asserts the interpreter Ansible
actually uses can import it.

### 9. The fix for bug 6 had its own bug

The ROLLBACK_COMPLETE recovery chained three `aws cloudformation wait` calls to
settle any in-flight operation:

```bash
aws cloudformation wait stack-create-complete   ... || true
aws cloudformation wait stack-rollback-complete ... || true
aws cloudformation wait stack-delete-complete   ... || true   # <-- hangs here
```

Once the rollback finished, the third call sat waiting for a deletion nobody
had requested — and `wait stack-delete-complete` polls for an hour before
giving up. The deploy hung for twenty minutes on a stack that was ready to be
deleted immediately.

*Fix:* poll `describe-stacks` until the status stops ending in `_IN_PROGRESS`,
then decide what to do. `REVIEW_IN_PROGRESS` is excluded from that test, since
it is a resting state rather than an operation in flight.

Worth stating plainly: this bug was introduced *by a fix* and caught by the very
next run. Chained `|| true` waits look defensive and are not — each one is an
unconditional hour-long timer for a condition that may never arrive.

### 10. Not a bug: bare metal is slow in both directions

Worth knowing before planning a debugging session around it.

| Operation | `g4dn.metal` | a normal instance |
|---|---|---|
| launch to `running` | 2-4 min | ~30 s |
| terminate to `terminated` | **10-20 min** | ~1 min |

A failed stack therefore takes 10-20 minutes to roll back before it can even be
deleted and retried, and CloudFormation will not let you start over until it
finishes. Two consequences:

- The retry logic added for transient launch failures costs ~20 minutes per
  attempt, almost all of it teardown. That is still far cheaper than losing a
  healthy node and rebuilding by hand.
- The same slowness is what makes fencing take 5-15 minutes on this rig, since
  a fence is a stop and a start. See
  [fencing-on-aws.md](fencing-on-aws.md).

## Where run 1 stopped

Torn down deliberately mid-run, not because of a failure. Stages reached:

| Stage | Result |
|---|---|
| `make infra` | **passed** — VPC, private hosted zone, bastion, all three bastion services |
| `make tnf` | **reached bootstrap** — both `g4dn.metal` up, ignition served, `wait-for bootstrap-complete` in progress when it was stopped |
| `make gpu` | not reached |
| `make virt-acm` | not reached |
| `make guests` | not reached |
| `make guest-gpu` | not reached |

Confirmed working against real hardware:

- **The Redfish → EC2 fencing shim.** Both nodes resolved by `Name` tag and
  reported live power state: `master-0 {"PowerState":"On","ec2":"running"}`.
  The BMC credentials were accepted. This is the piece of the repo with no
  upstream equivalent, and it works.
- Both `g4dn.metal` launched with data volumes attached at `/dev/sdf`.
- `openshift-install` produced a **302 KB `bootstrap.ign`** against a 16 KiB
  user-data limit, which is the whole reason for the pointer indirection. Good
  to have that confirmed rather than assumed.
- haproxy validated its own config and came up; the ignition server served both
  configs.
- The bootstrap node reached the point of answering the Kubernetes API.

Still unproven, in order of risk:

1. `bootstrap-complete` and `install-complete` on a two-node `platform: none`
   cluster — in particular whether the nodes register as `master-0` / `master-1`
   from the ignition `/etc/hostname`, which `finish.yml` asserts.
2. Whether the TNF controller creates `fence_redfish` stonith devices pointing
   at the shim, and whether `pcs stonith status` reports them Started.
3. Everything from `make gpu` onward: the IOMMU MachineConfig rollout on a
   two-node control plane, the GPU Operator's sandbox workload split, LVM
   Storage finding the EBS volume by id, ACM on two nodes, and the
   `hcp --host-device-name` flag.

## Resuming

The environment was destroyed, so start from scratch:

```bash
cd deploy/
make doctor      # confirm quota and AZ capacity again -- both can change
make infra       # ~5 minutes
make tnf         # picks up from a clean slate
```

`config/instance.env` is gitignored and was left in place, as is the
`tnf-gp-key` EC2 key pair. Nothing else survives.

Budget roughly 20 minutes of teardown into any retry cycle: a failed stack
cannot be recreated until its rollback finishes, and rolling back two
`g4dn.metal` is most of that time.

## What this says about the static checks

They caught nothing here, and that is the honest lesson. Every one of these
bugs lived in the seam between components — the CLI's parameter encoding, a
package repository's contents, a device-name namespace, a file's meaning.

Two are now covered by tests that would have caught them
(`hack/test-common-sh.sh` for the parameter encoding, and the same suite could
be extended). The rest are the kind only a real deploy finds.

## Run 2 — 2026-08-27, eu-west-1, account 687604608198

A different sandbox account. Two more defects, and one environmental blocker
worth writing down because it cost nothing to detect and would have cost an
hour to debug.

### 11. Not a bug: the G/VT quota is per region, and mostly 4

`make doctor` refused to proceed in us-east-2: the "Running On-Demand G and VT
instances" quota was **64 vCPU**, and one `g4dn.metal` is 96. Not two — one.

Checking every region turned up the real picture:

| region | G/VT vCPU quota |
|---|---|
| us-east-1 | 4 |
| us-east-2 | 64 |
| us-west-1 / us-west-2 | 4 |
| ap-southeast-1 | 4 |
| **eu-west-1** | **768** |

`g4dn.metal` is also the only GPU bare-metal type offered in any of them, so
there is no smaller fallback. The fix was a two-line config change to
`eu-west-1`, which is exactly what `config/instance.env` is for.

The lesson for the toolbox: `make doctor`'s quota check earns its place. It
turned a would-be failure ~40 minutes into a deploy into a five-second refusal
before anything was created.

### 12. Ansible does not template a dictionary *key*

The node labelling task wrote the label name as a templated key:

```yaml
labels:
  "{{ gpu_device_label }}": "{{ item.gpu_workload }}"
```

The value templated; the key did not. It reached the API server as the literal
string `{{ gpu_device_label }}` and was rejected:

```
Node "master-0" is invalid: metadata.labels: Invalid value:
"{{ gpu_device_label }}": name part must consist of alphanumeric characters...
```

*Fix:* build the whole mapping inside one expression, so the key is evaluated
as part of it:

```yaml
labels: "{{ {gpu_device_label: item.gpu_workload} }}"
```

This was the only templated dictionary key in the repository. Worth grepping
for (`'^\s*"{{.*}}":'`) after adding any k8s object with a dynamic key.

### 13. The MachineConfigPool wait could pass without a rollout

Watching the `Updating` condition to confirm a rollout had begun turned out to
be both too slow and unsound.

Too slow: on a two-node TNF cluster the MCO must coordinate with Pacemaker
before draining a node that runs half of etcd. `Updating=True` did not appear
within the five-minute window, so that stage timed out.

Unsound: the stage was written with `failed_when: false`, and the stage after it
waits for `machineCount == updatedMachineCount` — which is **trivially true
before a rollout starts**. Had the pool genuinely never picked up the change,
the wait would have reported success for a MachineConfig applied to nothing.

*Fix:* stop watching a transient condition. Read the pool's target
(`.spec.configuration.name`) and wait until every node's
`machineconfiguration.openshift.io/currentConfig` annotation equals it and its
state is `Done`. That cannot be true before the rollout starts and cannot be
missed by polling too slowly. The failure path now dumps the pool, the per-node
annotations and the machine-config-daemon logs.

### 14. Not a bug: a metal reboot is ~17 minutes

Observed during the IOMMU rollout:

```
16:59  master-0 cordoned
17:02  master-0 NotReady          <- reboot begins
17:19  master-0 Ready             <- 17 minutes later
17:20  uncordoned, pool Updated
```

A two-node pool reboots serially, so **any MachineConfig change costs ~40
minutes**. Worth knowing before adding kernel arguments casually.

### 15. `unarchive`'s include filter needs the archive's exact member name

MCE packages the `hcp` CLI as `./hcp`. The role asked for `include: [hcp]`,
which matches nothing, and the module reported a misleading "Failed to find
handler ... Make sure the required command to extract the file is installed" —
as though gzip were missing.

*Fix:* drop the filter. The tarball holds only the binary, so extracting all of
it is simpler and immune to the leading `./` appearing or disappearing.

### 16. ACM reports the HyperShift addon Available while it is broken

The role waited for `Available=True` on the `hypershift-addon`
ManagedClusterAddOn. It got it — while the addon was also `Degraded=True` with
`reason=HypershiftDeployed msg=OperatorNotFound`. Available only means the
addon's *agent* is running; it says nothing about whether the HyperShift
operator was installed.

The stage passed, and the failure surfaced one stage later as:

```
no matches for kind "HostedCluster" in version "hypershift.openshift.io/v1beta1"
ensure CRDs are installed first
```

*Fix:* require `Available=True Degraded=False`, then wait for the
`hostedclusters.hypershift.openshift.io` CRD to be established — the thing that
actually has to exist. The failure path dumps the addon conditions and the
install job log.

### 17. ACM 2.17's HyperShift installer lacks RBAC for OCP 4.22's Cluster CAPI API

With the addon's real state visible, the install job's failure was:

```
ClusterAPI API detected, coordinating with Cluster CAPI Operator
failed to apply ClusterAPI config: clusterapis.operator.openshift.io "cluster"
is forbidden: User "system:serviceaccount:open-cluster-management-agent-addon:
hypershift-addon-agent-sa" cannot patch resource "clusterapis"
```

OCP 4.22 ships the Cluster CAPI Operator, which owns
`clusterapis.operator.openshift.io`. ACM 2.17's addon service account has no
rule for it.

*Fix:* the `acm` role grants that one permission. Removable once ACM ships the
rule itself.

### 18. The real blocker: TechPreviewNoUpgrade makes HyperShift wait forever

With the RBAC granted, the install job got further and then stopped:

```
Applied ClusterAPI config with 54 unmanaged CRDs
Waiting for Cluster CAPI Operator to sync...
```

It never syncs. The operator's own log says why:

```
capi-operator: MachineAPIMigration not implemented for platform None,
               nothing to do. Waiting for termination signal.
```

On `platform: none` the Cluster CAPI Operator does nothing and never writes
status to the `clusterapis` resource, so HyperShift waits indefinitely. The job
times out, is recreated, and repeats.

The chain that produces it:

1. The toolbox set `featureSet: TechPreviewNoUpgrade`, believing TNF needed it.
2. That enables the `ClusterAPIMachineManagement` gate.
3. Which creates `clusterapis.operator.openshift.io/cluster`.
4. Which makes HyperShift's installer coordinate with the Cluster CAPI Operator.
5. Which is a no-op on `platform: none` and never reports sync.
6. So the HyperShift operator is never installed and no guest cluster can exist.

**Two-node fencing is GA in 4.22 and does not need the feature set.** Verified
directly: `openshift-install create manifests` accepts an install-config with
`controlPlane.fencing.credentials` and `controlPlane.replicas: 2` and no
`featureSet` key at all.

*Fix:* `FEATURE_SET` now defaults to empty, with the reasoning recorded in
`config/instance.env.template`. Set it only on 4.21 or older.

The feature set is **irreversible on a running cluster**, so correcting it means
reinstalling. That is the cost of this particular mistake, and the reason the
template now argues against setting it "just in case".

There is no workaround on an already-TechPreview cluster: the `hcp` binary MCE
ships has no `install` subcommand (only `create`, `destroy`, `version`), so the
addon's install job cannot be bypassed.

### 19. Not a bug: the sandbox account stops every instance out from under you

Mid-way through `install-complete`, all three instances stopped at once:

```
tnf-gp-bastion   stopped  User initiated (2026-08-28 00:06:31 GMT)
tnf-gp-master-0  stopped  User initiated (2026-08-28 00:06:31 GMT)
tnf-gp-master-1  stopped  User initiated (2026-08-28 00:06:32 GMT)
```

Same second, no instance tag naming a schedule, and not triggered from here —
the account-level cost control in a Red Hat demo sandbox. Worth knowing before
planning a multi-hour run in one: the CloudFormation stacks survive, the EBS
volumes survive, and `aws ec2 start-instances` brings everything back, but any
`openshift-install wait-for` running over SSH dies with the bastion.

**The cluster recovered on its own.** Both nodes were `Ready` 2.5 minutes after
the restart, and the ClusterVersion went `Available=True` with every operator
healthy about nine minutes later, with no intervention:

```
20:33  authentication console etcd  not available
20:40  etcd                         not available
20:41  none                         cv_available=True
```

That is an accidental but real test of the topology. A simultaneous cold stop of
both control-plane nodes is precisely what fencing does to one of them, and a
two-node cluster with Pacemaker-managed etcd came back from losing *both*
without manual repair.

Re-running `make tnf` afterwards took 69 seconds: the corrected idempotency
guard (defect 4) asked the cluster whether it was installed, got yes, skipped
the install, and went straight to reapplying the Pacemaker timeouts — which is
exactly the behaviour that guard was rewritten for.

## Run 3 — 2026-08-30, eu-west-1, account 567296844027

A third sandbox account. `make infra` and `make tnf` passed first try (52 min,
fencing verified). `make gpu` exposed the most serious finding of the exercise.

### 20. A MachineConfig rollout can split a TNF cluster permanently

The IOMMU MachineConfig reboots both control-plane nodes, one at a time.
`master-1` rebooted and rejoined. `master-0` rebooted, came back healthy at every
level the infrastructure can see -- EC2 status checks ok, pings, `intel_iommu=on`
in `/proc/cmdline` -- and its kubelet never started.

The two nodes' corosync configuration had diverged:

```
master-0  /etc/corosync/corosync.conf:  master-0 + master-1,  two_node: 1
master-1  /etc/corosync/corosync.conf:  master-1 only,        two_node removed
```

When `master-0` left, `master-1` rewrote corosync to a single-node cluster so it
would keep quorum. That is TNF's designed last-man-standing behaviour. What did
not happen is the peer being re-added when it returned:

```
master-0:  Ring 1.17  Quorate: No   partition WITHOUT quorum  Online: [ master-0 ]
master-1:  Ring 2.16  Quorate: Yes  partition with quorum     Online: [ master-1 ]
master-1:  corosync bound to no UDP socket; knet link lists only itself
```

No quorum on `master-0` means Pacemaker starts nothing, so kubelet stays down
and the node sits `Ready=Unknown`. The etcd operator observes it and reports it
but does not repair it:

```
Pacemaker error: Cluster is unhealthy: Insufficient nodes in cluster (expected 2, found 1)
etcd: EtcdMembersDegraded: 1 of 2 members are available, NAME-PENDING-10.0.0.10 has not started
```

Ruled out: security groups permit all traffic between the nodes (verified on the
live SG), the nodes ping each other, and no host firewall rule mentions 5405.

This is an OpenShift/TNF issue rather than a toolbox defect. But the toolbox made
it far worse than it needed to be, in two ways worth fixing:

**It reported the wrong failure.** The split showed up as the GPU stage failing
to see IOMMU groups. The actual event -- a cluster that could not re-form after a
reboot -- was three levels below the error message.

**It did the risky thing at the wrong time.** The reboot ran before the
kubeconfig had been copied anywhere, in the middle of a stage whose name implies
GPUs, on a cluster whose TNF health had never been asserted beyond "the stonith
devices exist".

*Fix:* the pipeline is reordered and the MachineConfig is its own stage.

```
make infra -> make tnf -> make kubeconfig -> make iommu -> make gpu -> ...
```

  * `make tnf` no longer ends at `install-complete`. It ends by asserting the
    cluster is healthy **as TNF**: both nodes Ready, every operator Available and
    not Degraded, etcd not degraded, and Pacemaker *and* corosync quorate with
    both members online. "Installed" and "healthy as a two-node fencing cluster"
    are different claims, and only the second is a safe basis for rebooting.
  * `make kubeconfig` runs before anything that can break the cluster, so the
    credentials are on the workstation rather than only on the bastion.
  * `make iommu` is the sole rebooting stage. It re-runs the same health gate
    before touching anything and again after the rollout, so a pair that fails to
    re-form fails *that* stage with the corosync and Pacemaker state printed.
  * `make gpu` now installs only software and asserts the IOMMU MachineConfig
    already exists, rather than creating it.

The underlying TNF recovery gap is unchanged by any of this. What changes is that
the toolbox refuses to start a rolling reboot on a cluster that is not verifiably
healthy, and names the real failure when one occurs.
