ANSIBLE := ./openshift-clusters/scripts/run-playbook.sh

.PHONY: all keypair infra acm-infra peering siteconfig sites tnf kubeconfig iommu gpu virt-mce \
        tnf-recover \
        acm-site guests hcp-make-guests-from-acm vcp-make-guests-from-acm \
        guest-gpu app observability-bucket guest-lb guest-lbs doctor ssh acm-ssh status \
        inventory clean clean-guests destroy destroy-acm help

# ---------------------------------------------------------------- end to end
# Ordering is deliberate. The cluster must be fully up and healthy *as TNF*
# before anything reboots it, and its credentials must be on your workstation
# before the one stage that can break it.
all: keypair infra acm-infra peering tnf kubeconfig iommu gpu virt-mce acm-site \
     guest-lbs guests guest-gpu
	@echo ""
	@echo "Everything is up. 'make status' for a summary."

# ---------------------------------------------------------------- stages
# Creates whichever half of the SSH key pair is missing -- the local files or
# the EC2 key pair CloudFormation stamps onto every instance -- and refuses to
# continue if the two do not match. Idempotent, and cheap enough to leave in
# 'all': a sandbox account that gets reissued keeps the local key and loses the
# EC2 one, and the mismatch would otherwise surface as an unloggable bastion.
keypair:
	@./aws-infra/scripts/create-keypair.sh

infra:
	@./aws-infra/scripts/create-network.sh
	@./aws-infra/scripts/create-services.sh
	@./aws-infra/scripts/inventory.sh

tnf:
	@$(ANSIBLE) 10-tnf-install.yml $(EXTRA_ARGS)

iommu:
	@$(ANSIBLE) 15-iommu.yml $(EXTRA_ARGS)

# Not part of 'all'. For when a stage has stopped because the cluster is not
# healthy: works through the documented TNF recovery steps in order, least
# invasive first, re-checking after each and stopping at the one that works.
# The two destructive steps are opt-in:
#   EXTRA_ARGS='-e tnf_recover_allow_fence_confirm=true'
#   EXTRA_ARGS='-e tnf_recover_allow_etcd_restore=true'
tnf-recover:
	@$(ANSIBLE) 16-tnf-recover.yml $(EXTRA_ARGS)

gpu:
	@$(ANSIBLE) 20-base-gpu.yml $(EXTRA_ARGS)

virt-mce:
	@$(ANSIBLE) 30-virt-mce.yml $(EXTRA_ARGS)

# The ACM site: its own VPC and bastion, peered to TNF.
acm-infra:
	@./aws-infra/scripts/create-acm-infra.sh
	@./aws-infra/scripts/inventory.sh

peering:
	@./aws-infra/scripts/create-peering.sh

# The hub's ability to build clusters from Git: the SiteConfig operator, the
# install templates for each profile, and OpenShift GitOps with the fleet
# ApplicationSet. Not TNF -- that is installed by openshift-baremetal-install
# onto EC2 metal, which the assisted installer cannot reach.
siteconfig:
	@$(ANSIBLE) 37-siteconfig.yml $(EXTRA_ARGS)

# One folder per cluster, written into the fleet repository. Ansible generates;
# Git is what the hub reconciles against, so committing is the last step and it
# is yours.
sites:
	@$(ANSIBLE) 42-sites.yml $(EXTRA_ARGS)

# The ACM cluster itself: single-node OpenShift on bare metal at the ACM site.
# The S3 bucket and scoped IAM user that MultiClusterObservability keeps its
# Thanos blocks in. Separate from acm-site because it is AWS, not Ansible, and
# because losing the bucket would lose the metric history.
observability-bucket:
	@./aws-infra/scripts/create-observability-bucket.sh

# A public entry point for one guest's control plane. Build it before the guest:
# the NodePorts are pinned so it can be, and spec.services cannot be changed once
# the HostedCluster exists. Set GUEST_PUBLIC_CONTROL_PLANE=true for the guest
# stage to actually publish against it.
#
# SITE says which cluster hosts the control plane, and so which cluster's nodes
# answer the NodePorts: tnf for a guest from 'make guests', acm for one from
# 'make hcp-make-guests-from-acm'. The default follows 'make all'.
guest-lb:
	@test -n "$(GUEST)" || { echo "usage: make guest-lb GUEST=hcp-1 [SITE=tnf]"; exit 1; }
	@./aws-infra/scripts/create-guest-lb.sh "$(GUEST)" "$(or $(SITE),tnf)"

# Every guest's entry point, for 'make all'. A no-op unless
# GUEST_PUBLIC_CONTROL_PLANE is on, so the default path costs nothing; when it
# is on, this has to happen before 'guests' or the guest stage stops dead,
# because the address is baked into the HostedCluster at creation and
# spec.services is immutable afterwards.
guest-lbs:
	@set -eu; . ../config/instance.env; \
	if [ "$${GUEST_PUBLIC_CONTROL_PLANE:-false}" != "true" ]; then \
	  echo "==> GUEST_PUBLIC_CONTROL_PLANE is off; no guest load balancers needed"; \
	else \
	  i=1; while [ "$$i" -le "$${GUEST_CLUSTER_COUNT:-1}" ]; do \
	    ./aws-infra/scripts/create-guest-lb.sh "$${GUEST_CLUSTER_PREFIX:-hcp}-$$i" tnf; \
	    i=$$((i + 1)); \
	  done; \
	fi

acm-site: observability-bucket
	@$(ANSIBLE) 36-acm-site.yml $(EXTRA_ARGS)

# Guests hosted by TNF's own MultiCluster Engine: the control plane runs as
# pods on TNF and its workers are KubeVirt VMs on TNF, so the whole guest is on
# the cluster that has the GPUs. This is what 'make all' uses.
#
# The hub is left as a management hub -- ACM, observability, SiteConfig, GitOps
# -- with no workload of its own, which is why it no longer needs OpenShift
# Virtualization and no longer needs to be bare metal.
guests:
	@$(ANSIBLE) 40-hcp-guests.yml $(EXTRA_ARGS)

# The same guest, hosted the other way round: its control plane runs as pods on
# the ACM hub and only its workers are KubeVirt VMs on TNF. Kept because it is
# the topology the SiteConfig template set renders, but not part of 'make all'
# -- running it needs OpenShift Virtualization and a data volume on the hub,
# which a non-metal hub no longer has.
hcp-make-guests-from-acm:
	@$(ANSIBLE) 40-hcp-guests-from-acm.yml $(EXTRA_ARGS)

# The whole cluster on virtual machines: control plane VMs as well as worker
# VMs, all on TNF. Nothing of it runs on the hub -- ACM installs it with the
# Assisted Installer rather than HyperShift, so it is a standalone cluster that
# ACM happens to have created, not a hosted one.
vcp-make-guests-from-acm:
	@$(ANSIBLE) 41-vcp-clusters-from-acm.yml $(EXTRA_ARGS)

guest-gpu:
	@$(ANSIBLE) 50-guest-gpu.yml $(EXTRA_ARGS)

app:
	@$(ANSIBLE) 60-app.yml $(EXTRA_ARGS)

# ---------------------------------------------------------------- utilities
doctor:
	@./aws-infra/scripts/doctor.sh

ssh:
	@./aws-infra/scripts/ssh.sh

acm-ssh:
	@./aws-infra/scripts/ssh.sh --acm

status:
	@./aws-infra/scripts/status.sh

inventory:
	@./aws-infra/scripts/inventory.sh

kubeconfig:
	@$(ANSIBLE) fetch-kubeconfig.yml $(EXTRA_ARGS)

# ---------------------------------------------------------------- teardown
clean-guests:
	@$(ANSIBLE) clean-guests.yml $(EXTRA_ARGS)

clean:
	@./aws-infra/scripts/destroy-cluster.sh

destroy-acm:
	@./aws-infra/scripts/destroy-acm.sh

destroy:
	@./aws-infra/scripts/destroy.sh

help:
	@echo "End to end:"
	@echo "  make keypair       - create the SSH key pair from instance.env"
	@echo "  make tnf-recover   - work through the documented TNF recovery steps in order"
	@echo "  make all           - infra -> tnf -> gpu -> virt-mce -> acm-site -> guests -> guest-gpu"
	@echo ""
	@echo "Stages (each is resumable and safe to re-run):"
	@echo "  make infra         - VPC, subnet, DNS, bastion (haproxy + Redfish shim)"
	@echo "  make tnf           - install TNF 4.22 on 2x g4dn.metal, then verify it is"
	@echo "                       healthy as TNF (Pacemaker quorate, both etcd members)"
	@echo "  make kubeconfig    - save the cluster credentials locally, before anything"
	@echo "                       risky runs against the cluster"
	@echo "  make iommu         - the IOMMU MachineConfig. Reboots BOTH nodes serially;"
	@echo "                       gated on TNF health before and after"
	@echo "  make gpu           - NFD + NVIDIA GPU Operator (no reboots)"
	@echo "  make virt-mce      - LVM Storage, OpenShift Virtualization, MultiCluster Engine on TNF"
	@echo "  make acm-infra     - the ACM site: its own VPC, subnet, DNS and bastion"
	@echo "  make peering       - VPC peering between the two sites, plus routes and"
	@echo "                       cross-VPC DNS so each resolves the other privately"
	@echo "  make siteconfig    - the SiteConfig operator, the install templates for"
	@echo "                       each profile, and OpenShift GitOps on the hub"
	@echo "  make sites         - write one site definition per cluster into the fleet"
	@echo "                       repository (commit and push them; Argo CD builds)"
	@echo "  make observability-bucket - S3 + scoped IAM user for the hub's Thanos"
	@echo "  make guest-lb GUEST=hcp-1 - public NLB for a guest's API and console login"
	@echo "  make acm-site      - the ACM cluster: single-node OpenShift on bare metal"
	@echo "                       at the ACM site, plus OCP-V, ACM, and the credential"
	@echo "                       that lets it drive TNF as its infra cluster"
	@echo "  make guests          - guests hosted by TNF's own MultiCluster Engine"
	@echo "  make hcp-make-guests-from-acm - hosted control planes: CP pods on the hub, worker VMs on TNF"
	@echo "  make vcp-make-guests-from-acm - full clusters on VMs: control-plane VMs and worker VMs on TNF"
	@echo "  make guest-gpu     - NFD + GPU Operator inside each guest cluster"
	@echo "  make app           - build and deploy the visual inspection app in"
	@echo "                       each guest cluster (needs 2 GPUs per guest)"
	@echo ""
	@echo "Utilities:"
	@echo "  make doctor        - read-only preflight (run this first)"
	@echo "  make status        - stacks, instances, bastion services, cluster health"
	@echo "  make ssh           - shell on the bastion"
	@echo "  make kubeconfig    - copy the base cluster kubeconfig to ./clusters/"
	@echo "  make inventory     - regenerate the Ansible inventory"
	@echo ""
	@echo "Teardown:"
	@echo "  make clean-guests  - delete the guest clusters only"
	@echo "  make clean         - delete the OpenShift cluster, keep the network + bastion"
	@echo "  make destroy       - delete everything, including the EBS data volumes"
	@echo ""
	@echo "Pass extra ansible arguments with EXTRA_ARGS, e.g.:"
	@echo "  make gpu EXTRA_ARGS=\"-vv -e gpu_operator_channel=v25.3\""
