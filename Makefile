.PHONY: shellcheck yamlfmt ansible-lint test test-templates test-shim test-common \
        syntax-check verify install-pre-commit uninstall-pre-commit help

shellcheck:
	@./hack/shellcheck.sh

yamlfmt:
	@./hack/yamlfmt.sh

ansible-lint:
	@./hack/ansible-lint.sh

# Static checks over the Jinja and CloudFormation templates. No AWS, no
# cluster, no credentials -- they catch the mistakes that otherwise surface
# forty minutes into a deploy.
test-templates:
	@python3 hack/test-templates.py

# Routing, auth and power-state mapping for the Redfish fencing shim, with the
# EC2 layer stubbed.
test-shim:
	@cd tools/redfish-ec2 && python3 -m unittest test_redfish_ec2 -v

# Shell helpers whose failure modes are expensive: CloudFormation parameter
# encoding, which once silently could not carry an ignition config at all.
test-common:
	@bash hack/test-common-sh.sh

syntax-check:
	@for pb in deploy/openshift-clusters/*.yml; do \
		printf '%-46s' "$$pb"; \
		ansible-playbook --syntax-check \
			-i deploy/openshift-clusters/inventory.ini.sample "$$pb" >/dev/null \
			&& echo OK || exit 1; \
	done

test: test-templates test-shim test-common syntax-check

verify:
	VALIDATE_ONLY=true $(MAKE) shellcheck
	VALIDATE_ONLY=true $(MAKE) yamlfmt
	$(MAKE) ansible-lint
	$(MAKE) test

install-pre-commit:
	@ln -sf ../../hack/pre-commit .git/hooks/pre-commit
	@echo "Pre-commit hook installed."

uninstall-pre-commit:
	@rm -f .git/hooks/pre-commit
	@echo "Pre-commit hook removed."

help:
	@echo "Repository-level targets (deployment lives in deploy/ -- run 'make help' there):"
	@echo "  verify              - shellcheck + yamlfmt + ansible-lint"
	@echo "  shellcheck          - lint shell scripts"
	@echo "  yamlfmt             - format YAML (VALIDATE_ONLY=true to check only)"
	@echo "  ansible-lint        - ansible-lint"
	@echo "  test                - template checks, shim unit tests, playbook syntax"
	@echo "  test-templates      - render every Jinja template and assert its shape"
	@echo "  test-shim           - unit tests for the Redfish fencing shim"
	@echo "  test-common         - unit tests for the deploy/common.sh helpers"
	@echo "  install-pre-commit  - run 'make verify' automatically before each commit"
