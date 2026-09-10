# Build and deploy the visual inspection app.
#
# Targets whichever cluster KUBECONFIG points at -- the guest cluster is the
# interesting one, since its GPUs are physical T4s passed through KubeVirt.

REGISTRY   ?= image-registry.openshift-image-registry.svc:5000/visual-inspection
NAMESPACE  ?= visual-inspection
CAMERAS    ?= 4
MODEL      ?= Qwen/Qwen2.5-VL-3B-Instruct
VLLM_IMAGE ?= registry.redhat.io/rhaiis/vllm-cuda-rhel9:3.2.1
ENGINE     ?= podman

.PHONY: help frames build push deploy undeploy url logs status

help:
	@echo "make frames         - re-curate the VisA subset (maintainers; needs the dataset)"
	@echo "make build          - build camera-sim, analyzer and dashboard images"
	@echo "make push           - push them to \$$REGISTRY"
	@echo "make deploy         - apply the manifests (CAMERAS=$(CAMERAS))"
	@echo "make url            - print the dashboard route"
	@echo "make status         - pods, GPU allocation and route in one view"
	@echo "make undeploy       - delete the namespace"

# The committed subset under camera-sim/frames/ is what the image is built
# from; this only needs running when the selection itself should change.
# VISA is the extracted dataset root -- see camera-sim/curate-visa.py.
frames:
	@test -n "$(VISA)" || { echo "set VISA=/path/to/extracted/VisA"; exit 1; }
	@cd camera-sim && ./curate-visa.py --visa "$(VISA)" --out frames

build:
	$(ENGINE) build -t $(REGISTRY)/camera-sim:latest ./camera-sim
	$(ENGINE) build -t $(REGISTRY)/analyzer:latest   ./analyzer
	$(ENGINE) build -t $(REGISTRY)/dashboard:latest  ./dashboard

push:
	$(ENGINE) push $(REGISTRY)/camera-sim:latest
	$(ENGINE) push $(REGISTRY)/analyzer:latest
	$(ENGINE) push $(REGISTRY)/dashboard:latest

# sed rather than kustomize so this needs nothing but oc.
deploy:
	@oc apply -f manifests/00-namespace.yaml
	@for f in manifests/[1-5]*.yaml; do \
	   sed -e "s|REPLACED_BY_KUSTOMIZE|$(REGISTRY)|g" \
	       -e "s|registry.redhat.io/rhaiis/vllm-cuda-rhel9:3.2.1|$(VLLM_IMAGE)|g" \
	       -e "s|value: \"4\"|value: \"$(CAMERAS)\"|g" \
	       -e "s|Qwen/Qwen2.5-VL-3B-Instruct|$(MODEL)|g" \
	       "$$f" | oc apply -n $(NAMESPACE) -f - ; \
	 done
	@echo
	@echo "vLLM pulls the model on first start; give it a few minutes."
	@echo "  oc logs -n $(NAMESPACE) deploy/vllm -f"

url:
	@echo "https://$$(oc get route dashboard -n $(NAMESPACE) -o jsonpath='{.spec.host}')"

status:
	@oc get pods -n $(NAMESPACE) -o wide
	@echo
	@oc get nodes -o custom-columns=NODE:.metadata.name,GPU:.status.allocatable.'nvidia\.com/gpu' --no-headers
	@echo
	@$(MAKE) --no-print-directory url

logs:
	@oc logs -n $(NAMESPACE) deploy/analyzer -f

undeploy:
	@oc delete namespace $(NAMESPACE) --wait=false
