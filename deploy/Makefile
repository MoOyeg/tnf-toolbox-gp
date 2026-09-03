ANSIBLE := ./openshift-clusters/scripts/run-playbook.sh

.PHONY: all infra tnf kubeconfig iommu gpu virt-acm sno guests guests-from-acm \
        guest-gpu doctor ssh status inventory clean clean-guests destroy help

# ---------------------------------------------------------------- end to end
# Ordering is deliberate. The cluster must be fully up and healthy *as TNF*
# before anything reboots it, and its credentials must be on your workstation
# before the one stage that can break it.
all: infra tnf kubeconfig iommu gpu virt-acm sno guests-from-acm guest-gpu
	@echo ""
	@echo "Everything is up. 'make status' for a summary."

# ---------------------------------------------------------------- stages
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

virt-acm:
	@$(ANSIBLE) 30-virt-acm.yml $(EXTRA_ARGS)

sno:
	@$(ANSIBLE) 35-sno-acm.yml $(EXTRA_ARGS)

# Guests hosted by TNF's own MultiCluster Engine.
guests:
	@$(ANSIBLE) 40-vcp-guests.yml $(EXTRA_ARGS)

# Guests created by ACM on the SNO, with their VMs and GPUs on TNF.
guests-from-acm:
	@$(ANSIBLE) 40-vcp-guests-from-acm.yml $(EXTRA_ARGS)

guest-gpu:
	@$(ANSIBLE) 50-guest-gpu.yml $(EXTRA_ARGS)

# ---------------------------------------------------------------- utilities
doctor:
	@./aws-infra/scripts/doctor.sh

ssh:
	@./aws-infra/scripts/ssh.sh

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

destroy:
	@./aws-infra/scripts/destroy.sh

help:
	@echo "End to end:"
	@echo "  make all           - infra -> tnf -> gpu -> virt-acm -> guests -> guest-gpu"
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
	@echo "  make virt-acm      - LVM Storage, OpenShift Virtualization, MCE/ACM on TNF"
	@echo "  make sno           - a single-node OpenShift VM on TNF running ACM, and"
	@echo "                       the credential that lets it use TNF as infra"
	@echo "  make guests          - guests hosted by TNF's own MultiCluster Engine"
	@echo "  make guests-from-acm - guests created by ACM on the SNO, VMs and GPUs on TNF"
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
