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

.PHONY: help frames build push deploy render undeploy url logs status

help:
	@echo "make frames         - re-curate the VisA subset (maintainers; needs the dataset)"
	@echo "make build          - build camera-sim, analyzer and dashboard images"
	@echo "make push           - push them to \$$REGISTRY"
	@echo "make deploy         - apply the manifests (CAMERAS=$(CAMERAS))"
	@echo "make render OUT=DIR - write them to DIR instead, for GitOps delivery"
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
#
# The namespace manifest goes through sed as well. Applied verbatim it always
# created "visual-inspection" whatever NAMESPACE said, so deploying a second
# instance put its components in a namespace nothing had created and left a
# stray one behind.
deploy:
	@sed -e "s|name: visual-inspection|name: $(NAMESPACE)|" manifests/00-namespace.yaml | oc apply -f -
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

# The same substitutions as deploy, written to files instead of piped to oc.
#
# For GitOps delivery: the fleet repository holds what each cluster should be
# running, and Argo CD on that cluster applies it. Sharing the substitutions
# with deploy rather than reimplementing them in Ansible is the point -- there
# is one place that knows how a manifest becomes a deployable manifest, and the
# day this app gains a component both paths gain it together.
render:
	@test -n "$(OUT)" || { echo "usage: make render OUT=<directory>" >&2; exit 1; }
	@mkdir -p "$(OUT)"
	@rm -f "$(OUT)"/*.yaml
	@sed -e "s|name: visual-inspection|name: $(NAMESPACE)|" \
	     manifests/00-namespace.yaml > "$(OUT)/00-namespace.yaml"
	@for f in manifests/[1-5]*.yaml; do \
	   sed -e "s|REPLACED_BY_KUSTOMIZE|$(REGISTRY)|g" \
	       -e "s|registry.redhat.io/rhaiis/vllm-cuda-rhel9:3.2.1|$(VLLM_IMAGE)|g" \
	       -e "s|value: \"4\"|value: \"$(CAMERAS)\"|g" \
	       -e "s|Qwen/Qwen2.5-VL-3B-Instruct|$(MODEL)|g" \
	       -e "s|^\(metadata:\)$$|\1\n  namespace: $(NAMESPACE)|" \
	       "$$f" > "$(OUT)/$$(basename $$f)" ; \
	 done
	@echo "rendered $(NAMESPACE) into $(OUT)"

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
