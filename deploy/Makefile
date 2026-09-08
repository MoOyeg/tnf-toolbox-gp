ANSIBLE := ./openshift-clusters/scripts/run-playbook.sh

.PHONY: all keypair infra acm-infra peering tnf kubeconfig iommu gpu virt-mce \
        acm-site guests hcp-make-guests-from-acm vcp-make-guests-from-acm \
        guest-gpu doctor ssh acm-ssh status \
        inventory clean clean-guests destroy destroy-acm help

# ---------------------------------------------------------------- end to end
# Ordering is deliberate. The cluster must be fully up and healthy *as TNF*
# before anything reboots it, and its credentials must be on your workstation
# before the one stage that can break it.
all: keypair infra acm-infra peering tnf kubeconfig iommu gpu virt-mce acm-site \
     hcp-make-guests-from-acm guest-gpu
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

# The ACM cluster itself: single-node OpenShift on bare metal at the ACM site.
acm-site:
	@$(ANSIBLE) 36-acm-site.yml $(EXTRA_ARGS)

# Guests hosted by TNF's own MultiCluster Engine.
guests:
	@$(ANSIBLE) 40-hcp-guests.yml $(EXTRA_ARGS)

# Hosted control planes: the guest's control plane runs as pods on the ACM hub
# and only its workers are KubeVirt VMs on TNF. This is what 'make all' uses.
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
	@echo "  make acm-site      - the ACM cluster: single-node OpenShift on bare metal"
	@echo "                       at the ACM site, plus OCP-V, ACM, and the credential"
	@echo "                       that lets it drive TNF as its infra cluster"
	@echo "  make guests          - guests hosted by TNF's own MultiCluster Engine"
	@echo "  make hcp-make-guests-from-acm - hosted control planes: CP pods on the hub, worker VMs on TNF"
	@echo "  make vcp-make-guests-from-acm - full clusters on VMs: control-plane VMs and worker VMs on TNF"
	@echo "  make guest-gpu     - NFD + GPU Operator inside each guest cluster"
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
