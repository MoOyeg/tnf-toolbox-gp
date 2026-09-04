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

## Run 4 — 2026-08-31, eu-west-1, account 567296844027

The first run with the reordered pipeline. It did what it was meant to.

### 21. The reordering works

```
make infra        passed   2 min
make tnf          passed   50 min, and ended by asserting TNF health:
                           "2 nodes Ready, all operators Available, etcd not
                            degraded, Pacemaker and corosync quorate with both
                            members online"
make kubeconfig   passed   credentials on the workstation before anything risky
make iommu        passed   43 min. Both nodes rebooted serially and the pair
                           re-formed; the post-rollout gate confirmed it
make gpu          passed   master-0 nvidia.com/gpu, master-1 TU104GL_TESLA_T4
make virt-acm     passed   LVM, OpenShift Virtualization, ACM, HyperShift CRDs
```

`make iommu` is the step that split the cluster in run 3. This time the same
MachineConfig rolled out cleanly and the health gate proved it, which is the
whole point of separating it.

### 22. `deploy/clusters/` was never gitignored

Surfaced by promoting `make kubeconfig` to a pipeline stage. The ignore rule
named `deploy/openshift-clusters/clusters/`, a directory nothing writes to;
`fetch-kubeconfig.yml` fetches into `deploy/clusters/`. The hub kubeconfig, every
guest kubeconfig and the kubeadmin password sat untracked-but-unignored, one
`git add -A` from a commit.

Latent while `make kubeconfig` was an occasional manual step; reachable in
ordinary use the moment it became part of the pipeline.

*Fix:* correct rule, plus a check in `hack/test-templates.py` that reads the
destination out of the playbook and asserts it is ignored, so renaming the
target cannot quietly unprotect it.

### 23. The guest API is published as a LoadBalancer, which never provisions

`hcp create cluster kubevirt` renders `APIServer` with
`servicePublishingStrategy: LoadBalancer` and offers no flag to change it. On
bare metal with no load-balancer controller:

```
InfrastructureReady=False WaitingOnInfrastructureReady:
  kube-apiserver load balancer is not provisioned; 19m since creation
kube-apiserver  LoadBalancer  172.30.42.105  <pending>  6443:31923/TCP
```

Ignition, Konnectivity and OAuthServer were already `Route` in the same manifest.
Only the API differed.

*Fix:* the role rewrites `APIServer` to `Route` before applying and asserts the
result. `spec.services` is immutable once the HostedCluster exists, so it has to
happen at render time.

Worth noting this was listed in `vcp-guests.md` as a known gap affecting guest
*ingress*. That was mis-scoped: it also blocked the guest API, which is the
difference between a cluster with an unreachable console and no cluster at all.

### 24. A simultaneous cold stop of both nodes deadlocks etcd

The sandbox stopped all three instances again, mid-run:

```
tnf-gp-master-0  stopping  User initiated (2026-08-31 05:00:21 GMT)
tnf-gp-master-1  stopping  User initiated (2026-08-31 05:00:21 GMT)
tnf-gp-bastion   stopping  User initiated (2026-08-31 05:00:22 GMT)
```

After restarting them, corosync and Pacemaker re-formed correctly -- both nodes
online, quorate, expected votes 2, kubelet running on both. **etcd did not
start on either node:**

```
Failed Resource Actions:
  * etcd start on master-1 returned 'not configured'
    (force_new_cluster attribute is set on multiple nodes (master-0 master-1))

master-0:  force_new_cluster: master-0   revision: 109833
master-1:  force_new_cluster: master-1   revision: 109833
```

Because both nodes lost their peer at the same instant, each came back believing
it was the survivor and set itself as the etcd seed. The resource agent then
refuses to start etcd anywhere -- correctly, since seeding from both would fork
the cluster. The revisions are identical, so either is a safe seed, but nothing
chooses one.

The cluster is left with a working API on each node locally, kubelet up,
Pacemaker healthy, and no etcd. It does not recover on its own.

Recovery is to leave exactly one seed:

```bash
pcs node attribute master-1 force_new_cluster=
pcs resource cleanup etcd
```

This is distinct from defect 20, which was a *staggered* reboot leaving a
corosync split. This one is a *simultaneous* stop leaving an etcd seed conflict,
and it is what a real double power failure would produce. Both are worth raising
upstream.

The `tnf-install/health.yml` gate detects this state -- etcd degraded, and the
verdict names it -- but the toolbox does not attempt the repair, because
choosing an etcd seed is a decision with data-loss consequences when the
revisions differ.

## Run 5 — 2026-09-03, eu-west-1, account 521937384928

The first run of the two-site layout: TNF in `10.0.0.0/16`, the ACM cluster in
its own VPC at `10.1.0.0/16`, joined by VPC peering. `make infra`, `make
acm-infra` and `make peering` all came up first time, including the
cross-associated private zones and a verified path from the ACM side to
`10.0.0.5`. `make tnf` and `make acm-site` were then run at the same time, which
is what the two-site split is supposed to allow -- and `make acm-site` failed
four times in a row, each on a different consequence of the same refactor.

### 25. Role defaults are invisible to other roles

`make acm-site` died on `'ignition_root' is undefined`. `ignition_root` was a
default of the `loadbalancer` role, and `sno-cluster` read it. Role defaults are
scoped to the role that declares them, so this can never work -- but it looked
fine in review, and the template tests passed, because
`hack/test-templates.py` merges *every* role's defaults into one namespace
before rendering. The test harness encoded exactly the assumption that does not
hold at runtime.

The same audit turned up `acm_infra_namespace` (needed by `vcp-guest` as well as
`sno-cluster`) and three names -- `sno_install_dir`, `sno_cluster_name`,
`sno_infra_namespace` -- that were left behind when ACM moved off TNF and were
by then defined nowhere at all.

Variables two roles both need belong in `group_vars/all.yml`.
`test_roles_do_not_borrow_other_roles_defaults` now checks the real scoping
rule; reintroducing the bug fails it.

### 26. The ACM play read a file that only exists on the other bastion

`infra-credentials.yml` runs on the ACM bastion and slurped `{{ kubeconfig }}`
-- TNF's, which is produced by and stays on the *TNF* bastion. It also used the
credential one task before writing it. It now slurps from the TNF bastion with
`delegate_to`, writes the copy, and only then makes the TNF-side calls. Because
the two stages are meant to run concurrently, it also waits for that kubeconfig
to appear rather than assuming the other site has finished.

### 27. `/srv/ignition` did not exist yet

`Destination directory /srv/ignition does not exist`. The `loadbalancer` role
creates it, and `10-tnf-install.yml` includes that role at play level *before*
`tnf-install` for precisely this reason. `36-acm-site.yml` went straight to
`sno-cluster`, whose render step publishes ignition two tasks before its launch
step includes `loadbalancer`. Fixed by mirroring the TNF play.

### 28. A short PATH fallback silently removes /usr/sbin for a whole play

`haproxy -c -f ...` failed with `[Errno 2] No such file or directory:
b'haproxy'` -- with haproxy installed, four seconds earlier, in the same play,
and `/usr/sbin` present in the login PATH.

A play's `environment:` applies to the implicit `gather_facts` task too. On that
first task `ansible_env` is not yet defined, so

```yaml
PATH: "{{ bin_dir }}:{{ ansible_env.PATH | default('/usr/bin:/bin') }}"
```

resolves to the fallback -- and the setup module, running under it, then reports
*that* truncated value back as `ansible_env.PATH` for every task in the play.
The fallback is not a safety net; it is the value. `/usr/sbin` was gone for the
whole run.

`10-tnf-install.yml` had no `| default(...)`, which is why the TNF side never
showed this. The fallbacks now list the system directories, the haproxy
validation uses an absolute path, and
`test_play_path_fallbacks_keep_the_system_directories` fails any play whose
fallback drops `/usr/sbin` or `/sbin`.

### 29. `make verify` could not pass on any machine that had run a deploy

Unrelated to the ACM work, found while checking the fixes. `shellcheck` and
`yamlfmt` both walk the tree themselves, and neither reads `.gitignore`, so both
were linting the Ansible collections vendored into
`deploy/openshift-clusters/collections/ansible_collections/` at deploy time --
hundreds of findings in other people's files. `hack/yamlfmt.sh` was worse: its
in-container branch used a bash array, and the yamlfmt image runs `sh`, so it
exited on a syntax error before formatting anything. It had therefore never run.

Both now skip the vendored tree, and the yamlfmt branch is POSIX. yamlfmt
reports a repo-wide formatting backlog, to be applied when no deploy is in
flight -- Ansible reads its task files lazily, so reformatting them mid-run
changes what a running playbook is about to execute.

### 30. The ACM site's ignition server bound to the TNF bastion's address

`make acm-site` reached `wait-for bootstrap-complete` and sat there for twenty
minutes before failing with:

```
Failed waiting for Kubernetes API. This error usually happens when there is a
problem on the bootstrap host that prevents creating a temporary control plane.
```

There was no problem on the bootstrap host. Both ACM instances answered ping
with 6443 closed, and `/srv/ignition` on the ACM bastion held the right files
but nothing was listening on 8080:

```
ExecStart=/usr/bin/python3 -m http.server 8080 --bind 10.0.0.5
  (code=exited, status=1/FAILURE)
```

`10.0.0.5` is the *TNF* bastion. The unit template used
`{{ bastion_private_ip }}`, which is a TNF-site extra var, so on the ACM
bastion -- `10.1.0.5` -- the bind failed, systemd restarted it every five
seconds, and the nodes booted with no ignition to fetch. RHCOS with no config
brings up networking and nothing else, which is exactly what "answers ping,
6443 closed" looks like.

The `loadbalancer` role already took `control_plane_nodes` and
`bootstrap_private_ip` as caller overrides, so it was *almost* site-agnostic.
The bind address is now derived from the host the role is configuring
(`ansible_default_ipv4.address`) rather than passed in, because a fourth
override across four call sites is one more thing to forget. A check fails the
role's templates if they name a single site's variables again.

The failure cost twenty minutes because the symptom is reported against the
bootstrap host, and the cause was on the bastion. When bootstrap times out with
nothing on 6443, check the ignition server before the node.

### 31. Extra vars outrank include_role parameters, silently

The ACM site's second bootstrap attempt also failed, but further along and for a
different reason. Both nodes fetched their ignition this time -- the bastion's
own access log proves it:

```
10.1.0.10 - - "GET /master.ign HTTP/1.1" 200 -
10.1.0.9  - - "GET /bootstrap.ign HTTP/1.1" 200 -
```

The bootstrap node came up and served `/readyz` with 200 on 10.1.0.9:6443. But
`openshift-install` got EOF from `api.acm...:6443`, which resolves to the
bastion's haproxy, and haproxy's stats said:

```
api bootstrap DOWN * L4TOUT
api sno-0     DOWN   L4CON
api BACKEND   DOWN
```

`L4TOUT` is a TCP connect timeout, from a host that could reach the same
address with curl a second earlier. The rendered config explained it:

```
server bootstrap 10.0.0.9:6443 check ...
```

`10.0.0.9` is the *TNF* bootstrap, in the other VPC, long since destroyed.

`sno-cluster/launch.yml` does pass the right value:

```yaml
- ansible.builtin.include_role:
    name: loadbalancer
  vars:
    bootstrap_private_ip: "{{ acm_bootstrap_private_ip }}"
```

and it is silently ignored. `bootstrap_private_ip` is one of the extra vars
run-playbook.sh supplies with `-e`, and **extra vars outrank include_role
parameters** -- so the caller's override is accepted, discarded, and TNF's
address is rendered instead. Nothing warns.

`control_plane_nodes` in the same block *does* work, because it is defined in
group_vars rather than passed with `-e`. That is the whole difference, and it is
invisible at the call site: two adjacent lines in one `vars:` block, one
effective and one not.

The role's input is now `lb_bootstrap_address`, a name nothing passes with `-e`,
defaulting to `bootstrap_private_ip` so the TNF call sites are unchanged. The
site-agnostic check now derives its banned list from run-playbook.sh itself, so
any name that becomes an extra var later is rejected in the loadbalancer's
templates automatically.

The general rule: a role parameter meant to be overridden must not share a name
with an extra var. This is the same precedence trap as defect 28's PATH and the
ACM data volume -- extra vars win over both set_fact and role params, and in
every case the symptom appeared far from the cause.

### 32. The guest API was published on TNF's wildcard from the ACM hub

`make guests-from-acm` created the HostedCluster and then sat at
`InfrastructureReady=False` until it timed out, with only three pods in the
hosted control-plane namespace and `capi-provider` stuck in `Init:0/1` probing a
`kube-apiserver` that was never created.

The routes said why:

```
konnectivity-server  konnectivity-server-clusters-vcp-1.apps.acm.sandbox807...
oauth                oauth-clusters-vcp-1.apps.acm.sandbox807...
kube-apiserver       api-vcp-1.apps.tnf-gp.sandbox807...
```

The two the operator created itself are on the ACM wildcard, because that is
where the control plane runs. The third is ours, and it is on TNF's. The ACM
hub's router does not answer for `*.apps.tnf-gp...`, so the API was never
reachable and infrastructure never became ready.

`vcp-guest` built the hostname from `cluster_domain`, which was right while TNF
was the only hub. With ACM hosting the control planes and TNF providing only the
VMs, the API route belongs to whichever cluster's router serves it -- the hub --
and the VMs' location is irrelevant to it.

The role now takes `guest_api_domain`, defaulting to `cluster_domain` so the
TNF-hosted path is unchanged, and `40-vcp-guests-from-acm.yml` sets it to the
ACM cluster's domain. As with defect 31, the parameter deliberately does not
reuse the extra var's name, or the override would be discarded in silence.

The site-agnostic check now covers `vcp-guest` as well as `loadbalancer`: both
run against whichever site is hosting, so neither may name TNF's variables.

### 33. The guest control plane defaulted to three etcd replicas on a one-node hub

With NodePort publishing the control plane finally started deploying -- nine
pods where there had been three -- and then stalled:

```
etcd-0            3/4  CrashLoopBackOff  11 times
etcd-1            0/4  Pending
etcd-2            0/4  Pending
data-etcd-1       Pending  lvms-vg1
data-etcd-2       Pending  lvms-vg1
etcd-recovery-*   Error    (eight of them)
```

`hcp create cluster` defaults `--control-plane-availability-policy` to
`HighlyAvailable`, which runs etcd as a three-replica StatefulSet with
anti-affinity across three nodes. The ACM hub is a single node, so two replicas
could never be scheduled, their PVCs stayed unbound behind
WaitForFirstConsumer, and etcd-0 crashlooped because it could not reach quorum
on its own. HyperShift's own etcd-recovery jobs then failed in a loop.

TNF would not have satisfied it either, at two nodes.

*Fix:* `SingleReplica` for both the control plane and the guest's own
infrastructure services, as role defaults, since every hub this toolbox builds
is smaller than three nodes.

This one was only reachable after defect 32: while the private router blocked
InfrastructureReady, etcd was never created at all, so the topology mismatch had
nothing to surface through.

### 34. The single-node hub ran out of pods, not capacity

`vcp-1` came up -- control plane available on the ACM hub, worker VM running on
TNF with two T4s attached. `vcp-2`'s control plane never scheduled:

```
0/1 nodes are available: 1 Too many pods.
```

Not CPU, not memory. The node wanted 279 pods against the kubelet's default
ceiling of 250, with 47 of 48 cores and 187 GiB of memory idle:

```
pods on sno-0: 279
capacity=250 allocatable=250
cpu=47500m mem=196529124Ki
```

A single-node hub carrying ACM, MultiCluster Engine, OpenShift Virtualization
and one hosted control plane per guest is simply a lot of pods. The first guest
fits; the second does not.

*Fix:* a KubeletConfig raising maxPods to 500, applied whether or not the run
installed the cluster, since a hub built before the change needs it too. The MCO
reboots the node to apply it and on a single-node cluster that takes the API
with it, so the task waits for the pool to settle rather than letting the next
stage fail against an API on its way down.

Worth noting the message names the symptom and not the cause: "Too many pods"
reads like a capacity problem on a machine with 47 idle cores.

### 35. Run 5 completed

```
make infra        passed   TNF network + services, public zone
make acm-infra    passed   ACM VPC 10.1.0.0/16, its own bastion
make peering      passed   pcx-0beb09ea6c5a64e66, both private zones cross-associated
make tnf          passed   97 tasks. "2 nodes Ready, all operators Available,
                           etcd not degraded, Pacemaker and corosync quorate
                           with both members online"
make kubeconfig   passed   credentials fetched, verified from the workstation
                           over the public name with no TLS flags
make iommu        passed   intel_iommu=on iommu=pt on both nodes, health gate
                           green after the reboots
make gpu          passed   master-0 nvidia.com/gpu=8 (container)
                           master-1 nvidia.com/TU104GL_TESLA_T4=8 (vfio-pci)
make virt-mce     passed   LVM lvms-vg1 default, OpenShift Virtualization with
                           GPU host devices, MultiCluster Engine 2.17.2.
                           No MultiClusterHub on TNF -- verified absent
make acm-site     passed   111 tasks. SNO 4.22.10 on m5zn.metal at its own site,
                           MultiClusterHub 2.17.1 Running, LVM, and a credential
                           that reaches TNF's API across the peering
make guests-from-acm passed  vcp-1 and vcp-2, control planes on the ACM hub,
                           worker VMs on TNF, NodePools 1/1
make guest-gpu    passed   nvidia.com/gpu=2 in each guest, ClusterPolicy ready
```

The shape the whole thing was built for, confirmed on the infra cluster:

```
acm-hosted-vms/vcp-1-c4rvb-xqtlk  node=master-1
  hostDevices=[nvidia.com/TU104GL_TESLA_T4, nvidia.com/TU104GL_TESLA_T4]
acm-hosted-vms/vcp-2-5s5xz-dqdqs  node=master-1
  hostDevices=[nvidia.com/TU104GL_TESLA_T4, nvidia.com/TU104GL_TESLA_T4]
```

ACM never runs on the cluster it manages. The hub is a separate site in a
separate VPC; TNF keeps MultiCluster Engine and provides the VMs and the GPUs;
the two are joined only by the peering connection and one kubeconfig.

Two things this run proved about the topology, neither of them planned:

The sandbox stopped all six instances mid-run, at 18:31, in the same second --
defect 19 again, across both sites this time. TNF came back with no
intervention: clusterversion stayed Available on the surviving node throughout,
etcd re-formed on its own, and the MachineConfig rollout that was half-applied
resumed and finished. That is the second time this two-node cluster has survived
losing *both* control-plane nodes at once, which is what fencing does to one of
them.

And the reordering held again. `make iommu` rebooted both nodes serially with
the pair re-forming each time, and the post-rollout health gate proved it --
the failure from run 3 has not recurred in two runs.
