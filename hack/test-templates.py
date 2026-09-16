#!/usr/bin/env python3
"""Static checks over the templates, run by 'make verify'.

None of this needs AWS, a cluster, or credentials. It catches the class of
mistake that otherwise surfaces forty minutes into a deploy: a Jinja typo, an
install-config that lost its fencing block, an ignition pointer that grew past
the CloudFormation parameter limit, a CloudFormation !Ref to a parameter nobody
declared.
"""

import base64
import glob
import json
import os
import re
import sys

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}{': ' + detail if detail else ''}")
        FAILURES.append(name)


# A representative render context. Every variable a template uses must appear
# here, because StrictUndefined turns a missing one into a failure -- which is
# the point: it catches a template referencing a variable no role ever sets.
CONTEXT = dict(
    base_domain="tnf.local",
    # Derived in group_vars from ocp_version, so role_defaults() skips it --
    # it only keeps literals, because Jinja there resolves at render time.
    guest_release_image="quay.io/openshift-release-dev/ocp-release:4.22.0-x86_64",
    cluster_name="tnf-gp",
    cluster_domain="tnf-gp.tnf.local",
    feature_set="TechPreviewNoUpgrade",
    region="us-west-2",
    bmc_username="admin",
    bmc_password="s3cret",
    fencing_base_url="https://10.0.0.5:8000/redfish/v1/Systems",
    ignition_base_url="http://10.0.0.5:8080",
    network_type="OVNKubernetes",
    subnet_cidr="10.0.0.0/24",
    cluster_network_cidr="10.128.0.0/14",
    cluster_network_host_prefix=23,
    service_network_cidr="172.30.0.0/16",
    pull_secret='{"auths":{}}',
    ssh_public_key="ssh-ed25519 AAAA test",
    bastion_private_ip="10.0.0.5",
    bootstrap_private_ip="10.0.0.9",
    node_name="master-0",
    repo_root="/repo",
    repo_slug="acme/tnf-toolbox-gp",
    redfish_shim_user="redfish",
    redfish_shim_dir="/opt/redfish-ec2",
    redfish_shim_conf_dir="/etc/redfish-ec2",
    redfish_shim_port=8000,
    ignition_port=8080,
    ignition_root="/srv/ignition",
    # A Jinja-valued role default (the bastion's own address), so role_defaults()
    # skips it -- it keeps only literals.
    lb_bind_address="10.0.0.5",
    lb_bootstrap_address="10.0.0.9",
    # Another Jinja-valued role default: the hub node the guest control plane's
    # NodePorts answer on, which role_defaults() drops for the same reason.
    # TNF's master-0. The default is {{ master0_private_ip }}: the guest's
    # services are published on a node of whichever cluster hosts the control
    # plane, and that is TNF. This was 10.1.0.10, the ACM node, while the
    # control plane ran there.
    guest_api_address="10.0.0.10",
    # loop_var for the virtual machine template
    vm={"name": "vcp-1-cp-1", "role": "control-plane",
        "cores": 8, "memory": "20Gi", "gpus": 0},
    # set_fact'd per guest inside hcp-guest's loop, so no role default declares it
    guest_name="hcp-1",
    # Another Jinja-valued role default, dropped by role_defaults() for the same
    # reason: the policy's own namespace, which is deliberately not the site's.
    vcp_policy_namespace="vcp-1-policies",
    # set_fact'd by vcp-cluster's plan-nodes.yml, for the same reason. Three
    # schedulable control-plane nodes: the compact topology the profile builds.
    vcp_nodes=[{"name": f"vcp-1-cp-{i}", "role": "control-plane",
                "cores": 8, "memory": "32Gi", "gpus": 1} for i in (1, 2, 3)],
    haproxy_stats_port=9000,
    ansible_user="ec2-user",
    include_bootstrap=True,
    control_plane_nodes=[
        {"name": "master-0", "private_ip": "10.0.0.10",
         "gpu_workload": "container", "data_volume": "vol-0aaa"},
        {"name": "master-1", "private_ip": "10.0.0.11",
         "gpu_workload": "vm-passthrough", "data_volume": "vol-0bbb"},
    ],
)


def strip_go(template):
    """A Go template with its directives removed, so YAML can be parsed from it.

    The values inside a SiteConfig template ConfigMap are Go templates, not
    YAML: `{{ if ... }}` lines and `{{ .Spec.X }}` scalars. Dropping whole
    directive lines and quoting bare substitutions leaves something close enough
    to parse, which is all these assertions need -- they check structure, not
    the values the operator will substitute.
    """
    lines = []
    for line in template.splitlines():
        if re.match(r"\s*\{\{.*\}\}\s*$", line):
            continue  # a directive or an indented block insert, not a mapping
        lines.append(re.sub(r"\{\{[^}]*\}\}", "placeholder", line))
    return "\n".join(lines)


def role_defaults():
    """Every role's defaults/main.yml plus group_vars/all.yml, merged.

    Loaded rather than restated here so that adding a variable to a role does not
    also require adding it to this file -- the failure mode being a template that
    renders fine in Ansible and fails only in the test, which teaches people to
    distrust the test.
    """
    merged = {}
    sources = sorted(glob.glob(f"{ROOT}/deploy/openshift-clusters/roles/*/defaults/main.yml"))
    sources.append(f"{ROOT}/deploy/openshift-clusters/group_vars/all.yml")
    for path in sources:
        values = yaml.safe_load(open(path, encoding="utf-8")) or {}
        # Role defaults are frequently Jinja referring to other variables; those
        # resolve at render time, so only literals are useful as context.
        merged.update({k: v for k, v in values.items()
                       if not (isinstance(v, str) and "{{" in v)})
    return merged


def extra_vars_from_wrapper():
    """Placeholder values for every extra var run-playbook.sh supplies.

    Parsed from the script rather than restated, for the same reason role
    defaults are: a variable added to the wrapper should not also have to be
    added here. Explicit CONTEXT entries still win, so anything whose exact value
    the assertions depend on is set there.
    """
    wrapper = f"{ROOT}/deploy/openshift-clusters/scripts/run-playbook.sh"
    names = re.findall(r"--arg(?:json)?\s+(\w+)", open(wrapper, encoding="utf-8").read())
    placeholders = {}
    for name in names:
        if name.endswith("_cidr"):
            placeholders[name] = "10.9.0.0/24"
        elif name.endswith("_url"):
            placeholders[name] = "http://10.9.0.9:8080"
        elif name.endswith("_ip"):
            placeholders[name] = "10.9.0.9"
        elif name.endswith("_count") or name.endswith("_replicas"):
            placeholders[name] = 1
        else:
            placeholders[name] = name
    return placeholders


def render(template_path, **overrides):
    directory, name = os.path.split(template_path)
    # trim_blocks matches ansible.builtin.template, whose default is yes. Jinja's
    # own default is no, and the difference is not cosmetic: with it on, the
    # newline after a block tag is eaten, so a `---` on the line after an
    # {% endraw %} joins the previous line and two YAML documents silently
    # become one. Rendering here with Jinja's defaults meant this harness could
    # pass a template that Ansible renders differently -- which is the one thing
    # it exists to prevent.
    env = Environment(loader=FileSystemLoader(directory), undefined=StrictUndefined,
                      trim_blocks=True)
    # Ansible's filter set, not Jinja's. A template is rendered by Ansible in
    # anger, so anything it is allowed to use has to exist here too -- otherwise
    # this harness fails a template that works, and the fix would be to make the
    # template worse.
    env.filters["b64encode"] = lambda s: base64.b64encode(
        (s if isinstance(s, bytes) else str(s).encode())).decode()
    return env.get_template(name).render(
        **{**extra_vars_from_wrapper(), **role_defaults(), **CONTEXT, **overrides})


def vcp_policy():
    """The vcp Policy file, as (all documents, the ConfigurationPolicy's objects).

    object-templates-raw is a Go template producing the object list, so it is run
    through strip_go() before parsing -- the same treatment the SiteConfig
    template ConfigMaps get, and for the same reason.
    """
    path = (f"{ROOT}/deploy/openshift-clusters/roles/vcp-cluster"
            "/templates/policy-virtualmachines.yaml.j2")
    rendered = render(path)
    docs = [d for d in yaml.safe_load_all(rendered) if d]
    policy = next(d for d in docs if d["kind"] == "Policy")
    raw = policy["spec"]["policy-templates"][0]["objectDefinition"]["spec"][
        "object-templates-raw"]
    objects = [i["objectDefinition"] for i in yaml.safe_load(strip_go(raw))]
    return rendered, docs, objects


def test_every_template_renders():
    print("\nJinja templates")
    for path in sorted(glob.glob(f"{ROOT}/deploy/**/*.j2", recursive=True)):
        rel = os.path.relpath(path, ROOT)
        # Galaxy collections install into the tree and ship their own test
        # templates, which are not ours to render.
        if "ansible_collections" in rel:
            continue
        try:
            render(path)
            check(rel, True)
        except Exception as exc:  # noqa: BLE001 - any render error is a failure
            check(rel, False, f"{type(exc).__name__}: {exc}")


def test_install_config():
    print("\ninstall-config.yaml")
    path = f"{ROOT}/deploy/openshift-clusters/roles/tnf-install/templates/install-config.yaml.j2"
    config = yaml.safe_load(render(path))

    check("controlPlane.replicas is 2", config["controlPlane"]["replicas"] == 2)
    check("compute.replicas is 0", config["compute"][0]["replicas"] == 0)
    check("platform is none", list(config["platform"]) == ["none"])

    credentials = config["controlPlane"]["fencing"]["credentials"]
    check("one fencing credential per node", len(credentials) == 2)
    # The single most consequential invariant in the whole repo: a fencing
    # hostname that does not match the node name produces a stonith device for
    # a node Pacemaker has never heard of, and it fails only when first needed.
    check(
        "fencing hostnames match the node names",
        [c["hostname"] for c in credentials]
        == [n["name"] for n in CONTEXT["control_plane_nodes"]],
    )
    check(
        "fencing addresses point at the shim's system ids",
        all(
            c["address"].endswith(f"/Systems/{c['hostname']}") for c in credentials
        ),
    )
    check(
        "certificate verification is disabled for the self-signed shim",
        all(c["certificateVerification"] == "Disabled" for c in credentials),
    )
    check(
        "machineNetwork is the subnet the nodes live in",
        config["networking"]["machineNetwork"][0]["cidr"] == CONTEXT["subnet_cidr"],
    )

    without_feature_set = yaml.safe_load(render(path, feature_set=""))
    check(
        "featureSet is omitted entirely when unset",
        "featureSet" not in without_feature_set,
    )


def test_ignition_pointers():
    print("\nIgnition pointers")
    directory = f"{ROOT}/deploy/openshift-clusters/roles/tnf-install/templates"
    for name in ("pointer-master.ign.j2", "pointer-bootstrap.ign.j2"):
        raw = render(f"{directory}/{name}")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            check(f"{name} is valid JSON", False, str(exc))
            continue
        check(f"{name} is valid JSON", True)
        # These travel as CloudFormation String parameters (4096) inside EC2
        # user-data (16 KiB). The pointer shape exists to stay under both.
        check(f"{name} fits a CloudFormation parameter", len(raw) <= 4096,
              f"{len(raw)} bytes")
        check(f"{name} declares an ignition version",
              "version" in parsed.get("ignition", {}))

    master = json.loads(render(f"{directory}/pointer-master.ign.j2"))
    # merge, not replace: replace would discard the /etc/hostname file that
    # pins the node name the fencing credentials refer to.
    check("master pointer merges rather than replaces",
          "merge" in master["ignition"]["config"])
    check("master pointer sets /etc/hostname",
          any(f["path"] == "/etc/hostname" for f in master["storage"]["files"]))


def test_redfish_shim_config():
    print("\nRedfish shim configuration")
    path = f"{ROOT}/deploy/openshift-clusters/roles/redfish-shim/templates/config.json.j2"
    config = json.loads(render(path))
    check("one system per control-plane node",
          sorted(config["systems"]) == ["master-0", "master-1"])
    check("systems resolve by EC2 Name tag",
          config["systems"]["master-0"]["name_tag"] == "tnf-gp-master-0")


def test_haproxy_config():
    print("\nhaproxy configuration")
    path = f"{ROOT}/deploy/openshift-clusters/roles/loadbalancer/templates/haproxy.cfg.j2"

    with_bootstrap = render(path, include_bootstrap=True)
    without = render(path, include_bootstrap=False)

    check("bootstrap is a backend during install",
          with_bootstrap.count("server bootstrap") == 2)

    # Each site has its own bastion fronting exactly one cluster, so the same
    # template serves TNF's pair and the ACM cluster's single node purely from
    # the control_plane_nodes it is handed.
    one_node = render(path, include_bootstrap=False, control_plane_nodes=[
        {"name": "sno-0", "private_ip": "10.1.0.10",
         "gpu_workload": "container", "data_volume": ""}])
    check("a single-node site gets one API backend",
          one_node.count("server sno-0") >= 1)
    check("no cluster is reachable through another's backend",
          "master-0" not in one_node)
    check("bootstrap is dropped afterwards",
          "server bootstrap" not in without)
    # No router runs on the bootstrap node, so putting it behind the ingress
    # frontends would blackhole requests.
    ingress = without.split("frontend ingress-http")[1]
    check("ingress never routes to bootstrap", "bootstrap" not in ingress)
    for port in ("6443", "22623", ":80", ":443"):
        check(f"port {port} is fronted", f"bind *:{port.lstrip(':')}" in without)


def _cfn_load(path):
    """Parse a CloudFormation template, ignoring its short-form tags."""

    class Loader(yaml.SafeLoader):
        pass

    def passthrough(loader, suffix, node):
        if isinstance(node, yaml.ScalarNode):
            return {f"Fn::{suffix}": loader.construct_scalar(node)}
        if isinstance(node, yaml.SequenceNode):
            return {f"Fn::{suffix}": loader.construct_sequence(node)}
        return {f"Fn::{suffix}": loader.construct_mapping(node)}

    Loader.add_multi_constructor("!", passthrough)
    with open(path, encoding="utf-8") as handle:
        return yaml.load(handle, Loader=Loader)


def _collect_refs(node, out):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "Fn::Ref" and isinstance(value, str):
                out.add(value)
            elif key == "Fn::GetAtt" and isinstance(value, str):
                out.add(value.split(".", 1)[0])
            _collect_refs(value, out)
    elif isinstance(node, list):
        for item in node:
            _collect_refs(item, out)


def test_cloudformation():
    print("\nCloudFormation templates")
    # AWS supplies these; they are never declared in the template.
    pseudo = {"AWS::Region", "AWS::AccountId", "AWS::StackName",
              "AWS::StackId", "AWS::Partition", "AWS::URLSuffix",
              "AWS::NoValue"}

    for path in sorted(glob.glob(f"{ROOT}/deploy/aws-infra/templates/*.yaml")):
        rel = os.path.relpath(path, ROOT)
        template = _cfn_load(path)
        declared = set(template.get("Parameters", {})) | set(template.get("Resources", {})) | pseudo

        refs = set()
        _collect_refs(template.get("Resources", {}), refs)
        _collect_refs(template.get("Outputs", {}), refs)
        dangling = refs - declared
        check(f"{rel}: every Ref and GetAtt resolves", not dangling,
              f"undeclared: {sorted(dangling)}")
        check(f"{rel}: declares outputs", bool(template.get("Outputs")))


def test_jsonpath_filters_are_shell_quoted():
    """A jsonpath filter passed to ansible.builtin.command must be single-quoted.

    The command module splits its argument with shlex, which strips bare double
    quotes. `jsonpath={.status.conditions[?(@.type=="Available")].status}`
    therefore reaches oc as `@.type==Available`, matches nothing, and returns an
    empty string -- so a wait on it never succeeds and the play hangs until it
    times out. Wrapping the whole jsonpath in single quotes preserves them.
    """
    print("\njsonpath quoting")
    offenders = []
    playbook_dir = f"{ROOT}/deploy/openshift-clusters"
    for path in sorted(glob.glob(f"{playbook_dir}/**/*.yml", recursive=True)):
        if "ansible_collections" in path:
            continue
        for lineno, line in enumerate(open(path, encoding="utf-8"), 1):
            if "jsonpath={" in line and '"' in line.split("jsonpath={", 1)[1]:
                offenders.append(f"{os.path.relpath(path, ROOT)}:{lineno}")
    check(
        "every jsonpath filter containing a quote is single-quoted",
        not offenders,
        f"unquoted at {offenders}",
    )


def test_shell_commands_are_not_split_by_stray_newlines():
    """A folded YAML scalar must not put a bare newline between shell arguments.

    In a `>-` scalar, a *more-indented* continuation line keeps its newline
    instead of folding to a space. Inside quotes that is harmless. Between
    arguments it is fatal: bash treats the newline as a command separator, so

        oc get nodes -o json | jq -r --arg r "nvidia.com/..."
          '.items[] | select(...)'

    runs jq with no filter, then tries to execute the filter as a program. The
    task fails with a bare "non-zero return code" that says nothing about why.

    A newline is fine when it is inside quotes, or escaped with a trailing
    backslash, or part of a deliberate multi-line `|` script whose next line
    starts a new command. Only a newline followed by a quoted argument is
    reported.
    """
    print("\nshell command continuation")

    def offending_fragment(command):
        quote = None
        for i, ch in enumerate(command):
            if quote:
                if ch == quote:
                    quote = None
            elif ch in "'\"":
                quote = ch
            elif ch == "\n":
                if command[i - 1:i] == "\\":
                    continue  # escaped: a real shell line continuation
                if command[i + 1:].lstrip(" ")[:1] in ("'", '"'):
                    return command[max(0, i - 40):i + 30].replace("\n", "\\n")
        return None

    def walk(node, path, found):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("ansible.builtin.shell", "ansible.builtin.command",
                           "shell", "command") and isinstance(value, str):
                    fragment = offending_fragment(value)
                    if fragment:
                        found.append(f"{path}: ...{fragment}...")
                walk(value, path, found)
        elif isinstance(node, list):
            for item in node:
                walk(item, path, found)

    found = []
    playbook_dir = f"{ROOT}/deploy/openshift-clusters"
    for path in sorted(glob.glob(f"{playbook_dir}/**/*.yml", recursive=True)):
        if "ansible_collections" in path:
            continue
        try:
            document = yaml.safe_load(open(path, encoding="utf-8"))
        except yaml.YAMLError:
            continue
        walk(document, os.path.relpath(path, ROOT), found)

    check("no shell command is split by a stray newline", not found,
          "; ".join(found))


def test_nothing_reads_role_defaults_from_outside():
    """Only the role that declares a default may read it.

    Ansible scopes defaults/main.yml to the role that owns it, so this fails at
    runtime with "'x' is undefined" -- forty minutes in, on the bastion, after
    the cluster has already been half built. render() above cannot catch it
    because role_defaults() deliberately merges every role together, which is
    exactly the assumption that does not hold in a real play. Variables two roles
    both need belong in group_vars/all.yml.
    """
    print("\nvariable scoping")
    pb = f"{ROOT}/deploy/openshift-clusters"
    jinja = re.compile(r"\{\{(.*?)\}\}|\{%(.*?)%\}", re.S)
    ident = re.compile(r"(?<![\w.])([a-z_][a-z0-9_]*)")

    def keys_of(path):
        try:
            loaded = yaml.safe_load(open(path, encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return set()
        return set(loaded) if isinstance(loaded, dict) else set()

    def role_files(root):
        for dirpath, _, names in os.walk(root):
            for name in names:
                if name.endswith((".yml", ".yaml", ".j2")):
                    yield os.path.join(dirpath, name)

    # Variables a caller hands a role through `include_role: ... vars:` are in
    # scope even though the role never declares them.
    passed = {}
    for path in role_files(pb):
        text = open(path, encoding="utf-8").read()
        for match in re.finditer(r"name:\s*([a-z0-9-]+)\s*\n(.*?)(?=\n\s*-\s|\Z)", text, re.S):
            role, body = match.group(1), match.group(2)
            if "vars:" in body:
                block = body.split("vars:", 1)[1]
                passed.setdefault(role, set()).update(
                    re.findall(r"^\s{4,}([a-z_][a-z0-9_]*):", block, re.M))

    globals_ = keys_of(f"{pb}/group_vars/all.yml") | set(extra_vars_from_wrapper())
    roles = sorted({os.path.basename(p.rstrip("/")) for p in glob.glob(f"{pb}/roles/*/")})
    owned = {r: keys_of(f"{pb}/roles/{r}/defaults/main.yml")
                | keys_of(f"{pb}/roles/{r}/vars/main.yml") for r in roles}

    borrowed = []

    # Playbooks are the worse case: they can see no role's defaults at all, so
    # a variable that lives in one is undefined everywhere in the play -- both
    # in its own tasks and in its environment: block.
    for path in sorted(glob.glob(f"{pb}/*.yml")):
        text = open(path, encoding="utf-8").read()
        used, local = set(), set()
        for a, b in jinja.findall(text):
            expr = a or b
            guarded = set(re.findall(
                r"(?<![\w.])([a-z_][a-z0-9_]*)\s*(?:\||\bis\b\s*(?:not\s*)?defined)", expr))
            used |= {n for n in ident.findall(expr) if n not in guarded}
        local |= set(re.findall(r"register:\s*([a-z_][a-z0-9_]*)", text))
        for block in re.finditer(r"(?:set_fact|vars):\s*\n((?:\s{4,}.*\n)+)", text):
            local |= set(re.findall(r"^\s+([a-z_][a-z0-9_]*):", block.group(1), re.M))
        for name in sorted(used - (local | globals_)):
            owners = [o for o in roles if name in owned[o]]
            if owners:
                borrowed.append((os.path.basename(path), name, owners))

    for role in roles:
        used, local = set(), set()
        for path in role_files(f"{pb}/roles/{role}"):
            text = open(path, encoding="utf-8").read()
            for a, b in jinja.findall(text):
                expr = a or b
                # `x | default(...)` and `x is defined` supply their own value.
                guarded = set(re.findall(
                    r"(?<![\w.])([a-z_][a-z0-9_]*)\s*(?:\||\bis\b\s*(?:not\s*)?defined)", expr))
                used |= {n for n in ident.findall(expr) if n not in guarded}
            local |= set(re.findall(r"register:\s*([a-z_][a-z0-9_]*)", text))
            for block in re.finditer(r"(?:set_fact|vars):\s*\n((?:\s{4,}.*\n)+)", text):
                local |= set(re.findall(r"^\s+([a-z_][a-z0-9_]*):", block.group(1), re.M))
        known = owned[role] | local | globals_ | passed.get(role, set())
        for name in sorted(used - known):
            owners = [o for o in roles if o != role and name in owned[o]]
            if owners:
                borrowed.append((role, name, owners))

    check("nothing reads a role's defaults from outside it", not borrowed,
          "; ".join(f"{r} uses {n}, defined only in {'/'.join(o)}" for r, n, o in borrowed))


def test_play_path_fallbacks_keep_the_system_directories():
    """A play's PATH fallback must still contain the system sbin directories.

    environment: applies to the implicit gather_facts task as well, where
    ansible_env is not yet defined -- so the fallback becomes the PATH the setup
    module runs under, and setup then reports that back as ansible_env.PATH for
    every task in the play. A fallback of '/usr/bin:/bin' therefore does not
    degrade gracefully: it removes /usr/sbin for the whole run, and anything
    living there stops being found. haproxy is the one that bit.
    """
    print("\nplay PATH fallbacks")
    required = ("/usr/sbin", "/sbin")
    for path in sorted(glob.glob(f"{ROOT}/deploy/openshift-clusters/*.yml")):
        text = open(path, encoding="utf-8").read()
        for fallback in re.findall(r"ansible_env\.PATH\s*\|\s*default\('([^']*)'\)", text):
            missing = [d for d in required if d not in fallback.split(":")]
            check(f"{os.path.basename(path)} keeps the system directories",
                  not missing, f"fallback '{fallback}' is missing {missing}")


def test_iommu_is_enabled_at_install_time():
    """The IOMMU MachineConfig written into the install manifests.

    It exists so the kernel arguments arrive on the first boot rather than
    through a rollout against a live two-node cluster, which is what split the
    Pacemaker pair three times. Its name and arguments have to match what the
    day-2 stage looks for, or that stage decides there is work to do and reboots
    both nodes for a change that is already in place.
    """
    print("\nIOMMU at install time")
    path = f"{ROOT}/deploy/openshift-clusters/roles/tnf-install/templates/gpu-passthrough-machineconfig.yaml.j2"
    mc = yaml.safe_load(render(path))
    defaults = role_defaults()

    check("it is a MachineConfig", mc["kind"] == "MachineConfig")
    check("named the same thing the day-2 stage looks for",
          mc["metadata"]["name"] == defaults["iommu_machine_config_name"])
    check("labelled for the pool the day-2 stage waits on",
          mc["metadata"]["labels"]["machineconfiguration.openshift.io/role"]
          == defaults["iommu_target_mcp"])
    check("carries exactly the arguments that stage checks for",
          mc["spec"]["kernelArguments"] == defaults["iommu_kernel_args"],
          f"{mc['spec']['kernelArguments']} != {defaults['iommu_kernel_args']}")
    check("it sets nothing else, so it cannot trigger a rollout of its own",
          set(mc["spec"]) == {"kernelArguments"})


def test_vcp_virtual_machine():
    """A cluster-on-VMs node, with and without a GPU attached.

    The boot order is the load-bearing part. An assisted install writes the
    image to disk and reboots; if the discovery ISO is still first in the boot
    order the node comes back up into discovery and the cluster reinstalls
    itself forever.
    """
    print("\nvcp virtual machines")
    path = f"{ROOT}/deploy/openshift-clusters/roles/vcp-cluster/templates/virtualmachine.yaml.j2"

    cp = yaml.safe_load(render(path, vm={
        "name": "vcp-1-cp-1", "role": "control-plane",
        "cores": 8, "memory": "20Gi", "gpus": 0}))
    worker = yaml.safe_load(render(path, vm={
        "name": "vcp-1-worker-1", "role": "worker",
        "cores": 8, "memory": "24Gi", "gpus": 2}))

    disks = {d["name"]: d for d in cp["spec"]["template"]["spec"]["domain"]["devices"]["disks"]}
    check("the installed disk boots before the discovery ISO",
          disks["root"]["bootOrder"] < disks["discovery"]["bootOrder"])
    check("the discovery ISO is a read-only cdrom",
          disks["discovery"].get("cdrom", {}).get("readonly") is True)

    check("a control-plane VM is given no GPU",
          "hostDevices" not in cp["spec"]["template"]["spec"]["domain"]["devices"])
    devices = worker["spec"]["template"]["spec"]["domain"]["devices"]["hostDevices"]
    check("a worker VM gets one hostDevice per GPU asked for", len(devices) == 2)
    expected = role_defaults()["gpu_resource_name"]
    check("the GPUs are requested by the configured resource name",
          all(d["deviceName"] == expected for d in devices))

    check("every node carries the cluster label the agent selector matches",
          cp["metadata"]["labels"]["tnf-toolbox-gp/cluster"]
          == worker["metadata"]["labels"]["tnf-toolbox-gp/cluster"])
    check("roles are distinguishable, so the API service selects only the control plane",
          cp["metadata"]["labels"]["tnf-toolbox-gp/role"] == "control-plane"
          and worker["metadata"]["labels"]["tnf-toolbox-gp/role"] == "worker")


def test_virtual_machines_are_an_acm_policy():
    """The all-VM cluster's machines are created by a Policy, not committed YAML.

    Two values in a VirtualMachine here are only knowable on the clusters: the
    GPU device name, which is derived from the card's model string, and the
    discovery ISO's URL, which is a bearer token. Resolving them when this file
    is written gets the first wrong silently and the second unpublishable, so
    both are resolved when the Policy enforces instead.
    """
    print("\nvirtual machines by policy")
    rendered, docs, objects = vcp_policy()
    kinds = {d["kind"]: d for d in docs}
    roles_dir = f"{ROOT}/deploy/openshift-clusters/roles"
    vcp_ci = yaml.safe_load(render(f"{roles_dir}/vcp-cluster/templates/clusterinstance.yaml.j2"))

    # A Policy nothing binds is propagated nowhere, and reports no error for it.
    binding = kinds["PlacementBinding"]
    check("the binding names the Policy that is here",
          [sub["name"] for sub in binding["subjects"]]
          == [kinds["Policy"]["metadata"]["name"]])
    check("the binding names the Placement that is here",
          binding["placementRef"]["name"] == kinds["Placement"]["metadata"]["name"])
    check("the Placement selects the infra cluster by name",
          kinds["Placement"]["spec"]["predicates"][0]["requiredClusterSelector"]
          ["labelSelector"]["matchLabels"] == {"name": CONTEXT["cluster_name"]})
    # A Placement may only select from a set its own namespace is bound to, and
    # the gitops role's binding lives in openshift-gitops.
    # A cluster's own namespace is reserved by ACM for the replicated copies of
    # policies propagated to it; its propagator deletes any root policy there,
    # on a loop, saying so only in its own log. Every site namespace is also a
    # ManagedCluster name, so the policy cannot live beside its ClusterInstance.
    check("the policy is not in a cluster's own namespace",
          kinds["Policy"]["metadata"]["namespace"]
          != vcp_ci["metadata"]["namespace"],
          kinds["Policy"]["metadata"]["namespace"])
    check("its namespace is bound to the cluster set it selects from",
          kinds["ManagedClusterSetBinding"]["metadata"]["namespace"]
          == kinds["Placement"]["metadata"]["namespace"])

    # inform would report the machines missing and create nothing, which looks
    # exactly like a working policy until no cluster appears.
    config = kinds["Policy"]["spec"]["policy-templates"][0]["objectDefinition"]
    check("it enforces rather than informs",
          kinds["Policy"]["spec"]["remediationAction"] == "enforce"
          and config["spec"]["remediationAction"] == "enforce")
    # Deleting the policy must not take a running cluster's nodes with it.
    check("deleting it leaves the machines running",
          config["spec"]["pruneObjectBehavior"] == "None")

    # Reading the hub's ingress cert is the one lookup outside the policy's own
    # namespace, and it is the only reason a service account is needed.
    check("hub templates run as a service account",
          kinds["Policy"]["spec"]["hubTemplateOptions"]["serviceAccountName"]
          == kinds["ServiceAccount"]["metadata"]["name"])
    # Naming a service account replaces the default same-namespace access rather
    # than adding to it, so every lookup the policy makes needs its own grant --
    # the ingress cert in openshift-config-managed and the InfraEnv beside the
    # policy. Missing either leaves the template unresolved and the whole policy
    # reporting template-error, having created nothing.
    roles = [d for d in docs if d["kind"] == "Role"]
    bindings = [d for d in docs if d["kind"] == "RoleBinding"]
    granted = {r["metadata"]["namespace"] for r in roles}
    # The Roles live where the objects they grant access to are -- the ingress
    # cert in openshift-config-managed, the InfraEnv in the site's namespace --
    # which is neither of them the namespace the policy itself is in.
    check("the account can read the ingress cert and the InfraEnv",
          granted == {"openshift-config-managed", vcp_ci["metadata"]["namespace"]},
          str(granted))
    check("its extra access is Roles, not ClusterRoles",
          all(r["kind"] == "Role" for r in roles) and len(roles) == 2)
    check("every Role is bound to that account",
          {b["roleRef"]["name"] for b in bindings} == {r["metadata"]["name"] for r in roles}
          and all(sub["name"] == kinds["ServiceAccount"]["metadata"]["name"]
                  for b in bindings for sub in b["subjects"]))
    # RBAC applies resourceNames only to get, update and delete. A list request
    # carries no name to match, so naming one does not narrow the rule -- it
    # denies the list, and the lookup fails with "cannot list resource".
    check("no rule narrows a list with resourceNames",
          not [r for r in roles for rule in r["rules"]
               if rule.get("resourceNames") and "list" in rule["verbs"]])
    # watch, not just read: it is what lets a changed value re-render.
    check("with watch, so a change re-renders",
          all("watch" in rule["verbs"] for r in roles for rule in r["rules"]))

    by_kind = {o["kind"]: o for o in objects}
    vms = [o for o in objects if o["kind"] == "VirtualMachine"]
    check("one VirtualMachine per planned node",
          len(vms) == len(CONTEXT["vcp_nodes"]),
          f"{len(vms)} for {len(CONTEXT['vcp_nodes'])} nodes")
    # CDI verifies the hub's image service against this; named wrong, the import
    # fails with "certificate signed by unknown authority".
    check("the DataVolume verifies against the CA the policy creates",
          by_kind["DataVolume"]["spec"]["source"]["http"]["certConfigMap"]
          == by_kind["ConfigMap"]["metadata"]["name"])
    check("every machine boots the DataVolume the policy creates",
          all(any(v.get("dataVolume", {}).get("name")
                  == by_kind["DataVolume"]["metadata"]["name"]
                  for v in vm["spec"]["template"]["spec"]["volumes"]) for vm in vms))
    # An assisted install reboots each node to write the image to disk. Without
    # this KubeVirt calls that a crash and restarts it from the ISO, forever.
    check("every machine survives the install reboot",
          all(vm["spec"]["template"]["spec"].get("terminationGracePeriodSeconds")
              for vm in vms))

    # The two lookups, checked on the text: their whole value is that they are
    # unresolved here.
    check("the ISO URL is a hub lookup, not a literal",
          "isoDownloadURL" in rendered and "https://" not in rendered)
    # Resolved hub output lands in a replicated Policy on the hub, readable by
    # anyone who can read policies and not covered by etcd encryption.
    check("and only its ciphertext is allowed to travel",
          "| protect hub}}" in rendered)
    check("the GPU name is looked up on the infra cluster",
          'lookup "v1" "Node"' in rendered)
    # nvidia.com/gpu is the time-sliced resource; a passed-through card
    # advertises the model-derived name beside it.
    check("excluding the time-sliced resource",
          'ne $name "nvidia.com/gpu"' in rendered)
    check("nothing in the file is a credential",
          not [w for w in ("byapikey", "dockerconfigjson", "BEGIN RSA",
                           "BEGIN OPENSSH", "password") if w in rendered])


def test_vcp_nodes_get_addresses_of_their_own():
    """The all-VM cluster's nodes must not share one address.

    Masquerade hands every VM the identical guest-side address of KubeVirt's
    NAT, so three nodes registered as three copies of 10.0.2.2 and no cluster
    could form. A primary layer2 UDN gives each one a real address and keeps it
    across a reboot, which a pod IP does not.
    """
    print("\nvcp node network")
    roles = f"{ROOT}/deploy/openshift-clusters/roles"
    defaults = {**extra_vars_from_wrapper(), **role_defaults(), **CONTEXT}
    node_cidr = defaults["vcp_node_network_cidr"]

    # Every range already spoken for. An overlap here is not a render error and
    # not a validation error -- it is traffic going somewhere unexpected months
    # later, so it is computed rather than eyeballed.
    import ipaddress
    others = {
        "infra pod network": defaults["cluster_network_cidr"],
        "infra service network": defaults["service_network_cidr"],
        "guest pod network": defaults["vcp_cluster_network_cidr"],
        "guest service network": defaults["vcp_service_network_cidr"],
        "TNF VPC": "10.0.0.0/16",
        "ACM VPC": "10.1.0.0/16",
    }
    node_net = ipaddress.ip_network(node_cidr)
    clashes = [n for n, c in others.items()
               if node_net.overlaps(ipaddress.ip_network(c))]
    check(f"the node network {node_cidr} overlaps nothing else",
          not clashes, f"overlaps {clashes}")

    # The UDN has to exist before anything attaches to it, and a
    # ConfigurationPolicy applies its object-templates in order -- so being
    # first is the ordering, not a tidiness preference.
    _, _, objects = vcp_policy()
    check("the network is created before the machines that attach to it",
          objects[0]["kind"] == "UserDefinedNetwork",
          f"first object is {objects[0]['kind']}")
    udn = objects[0]
    check("it is a primary layer2 network",
          udn["spec"]["topology"] == "Layer2"
          and udn["spec"]["layer2"]["role"] == "Primary")
    check("on the node network, with addresses that survive a reboot",
          udn["spec"]["layer2"]["subnets"] == [node_cidr]
          and udn["spec"]["layer2"]["ipam"]["lifecycle"] == "Persistent")

    # The defect itself: masquerade anywhere means shared addresses again.
    vms = [o for o in objects if o["kind"] == "VirtualMachine"]
    direct = yaml.safe_load(render(f"{roles}/vcp-cluster/templates/virtualmachine.yaml.j2"))
    for name, vm in [("policy", vms[0]), ("direct", direct)]:
        iface = vm["spec"]["template"]["spec"]["domain"]["devices"]["interfaces"][0]
        check(f"the {name} path does not put a VM behind NAT",
              "masquerade" not in iface, str(iface))
        check(f"the {name} path attaches it to the user-defined network",
              iface.get("binding", {}).get("name") == "l2bridge", str(iface))

    # A UDN the namespace has not opted into is created and ignored, and the VMs
    # come up on the pod network with nothing reporting it.
    ns = yaml.safe_load(render(f"{roles}/vcp-cluster/templates/sites-infra-namespace.yaml.j2"))
    check("the namespace opts into a primary user-defined network",
          "k8s.ovn.org/primary-user-defined-network" in ns["metadata"]["labels"])

    # The installer picks a node address itself unless told which network the
    # nodes are on, and a VM on a UDN has more than one to choose from.
    acis = yaml.safe_load(strip_go(yaml.safe_load(render(
        f"{roles}/vcp-cluster/templates/vcp-cluster-templates.yaml.j2"
    ))["data"]["AgentClusterInstall"]))
    check("the install template names the network the nodes are on",
          [m["cidr"] for m in acis["spec"]["networking"]["machineNetwork"]] == [node_cidr],
          str(acis["spec"]["networking"].get("machineNetwork")))


def test_fleet_workloads_are_pulled_not_pushed():
    """The fleet ApplicationSet delivers by pull, and the pieces must agree.

    Pull and push are not variations of one thing: the generator differs, the
    destination differs, and the hub must be told to keep out of the result. Get
    one of them wrong and the symptom is silence -- no Applications, or two
    controllers reconciling the same resources against each other.
    """
    print("\nfleet workloads by pull")
    roles = f"{ROOT}/deploy/openshift-clusters/roles"
    defaults = {**extra_vars_from_wrapper(), **role_defaults(), **CONTEXT}
    appset = yaml.safe_load(render(f"{roles}/gitops/templates/applicationset-fleet.yaml.j2"))
    tpl = appset["spec"]["template"]
    ann = tpl["metadata"]["annotations"]

    # A git generator here would produce Applications with no cluster to name,
    # and every annotation below would have nothing to fill in.
    check("it generates from placement decisions, not from folders",
          list(appset["spec"]["generators"][0]) == ["clusterDecisionResource"],
          str(list(appset["spec"]["generators"][0])))
    check("through the ConfigMap the gitops role creates",
          appset["spec"]["generators"][0]["clusterDecisionResource"]["configMapRef"]
          == defaults["gitops_placement_generator"])
    # The generator and the GitOpsCluster must name the same Placement or the
    # decisions it reads belong to someone else.
    check("selecting the placement the fleet GitOpsCluster hands over",
          appset["spec"]["generators"][0]["clusterDecisionResource"]["labelSelector"]
          ["matchLabels"]["cluster.open-cluster-management.io/placement"]
          == defaults["gitops_fleet_placement"])

    # Go template form, because the ApplicationSet sets goTemplate: true. This
    # asserted the bare {{name}} for as long as the ApplicationSet carried it,
    # which is how a generator that produced nothing at all stayed green.
    check("each Application names the cluster it is destined for",
          ann.get("apps.open-cluster-management.io/ocm-managed-cluster") == "{{.name}}",
          str(ann.get("apps.open-cluster-management.io/ocm-managed-cluster")))
    check("and is labelled for the propagation controller to pick up",
          (tpl["metadata"].get("labels") or {})
          .get("apps.open-cluster-management.io/pull-to-ocm-managed-cluster") == "true",
          str(sorted(tpl["metadata"].get("labels") or {})))
    # Without this the hub's own Argo CD reconciles it too, and both models
    # fight over the same resources on the same cluster.
    check("the hub is told not to reconcile it itself",
          ann.get("argocd.argoproj.io/skip-reconcile") == "true",
          str(sorted(ann)))
    # Reconciliation happens on the managed cluster, so "local" is local to it.
    check("it applies to the cluster it lands on, not a named one",
          tpl["spec"]["destination"] == {"server": "https://kubernetes.default.svc"},
          str(tpl["spec"]["destination"]))
    # A cluster's folder holds one subdirectory per app instance.
    check("it descends into each instance folder",
          tpl["spec"]["source"]["directory"]["recurse"] is True)
    check("reading that cluster's folder in the fleet tree",
          tpl["spec"]["source"]["path"]
          == f"{defaults['gitops_fleet_repo_path']}/{{{{.name}}}}",
          tpl["spec"]["source"]["path"])

    # The operator policy, and the namespace trap the VM policy fell into.
    docs = [d for d in yaml.safe_load_all(
        render(f"{roles}/gitops/templates/policy-gitops-operator.yaml.j2")) if d]
    kinds = {d["kind"]: d for d in docs}
    check("Argo CD is installed on the fleet by policy",
          "Policy" in kinds and "OperatorPolicy" in str(kinds["Policy"]))
    check("and enforced rather than merely reported",
          kinds["Policy"]["spec"]["remediationAction"] == "enforce")
    # ACM reserves a cluster's own namespace for replicated policies and deletes
    # anything else there.
    check("the policy is not in a namespace named after a cluster",
          kinds["Policy"]["metadata"]["namespace"] == defaults["gitops_argocd_namespace"])
    # One predicate has to reach an imported cluster and a SiteConfig-built one.
    fleet_label = defaults["gitops_fleet_label"]
    selects = kinds["Placement"]["spec"]["predicates"][0]["requiredClusterSelector"]
    check("it is placed at the fleet by label",
          selects["labelSelector"]["matchLabels"] == {fleet_label: "true"},
          str(selects["labelSelector"]["matchLabels"]))
    # The label the Placement selects must be the label the clusters carry --
    # compared against the templates rather than asserted as a literal.
    cm = yaml.safe_load(render(
        f"{roles}/vcp-cluster/templates/vcp-cluster-templates.yaml.j2"))
    mc = yaml.safe_load(strip_go(cm["data"]["ManagedCluster"]))
    check("vcp-cluster's clusters carry that label",
          fleet_label in mc["metadata"]["labels"],
          str(list(mc["metadata"]["labels"])))
    # The hosted guests are not built by SiteConfig and render no ManagedCluster
    # of their own -- they are imported into the hub after TNF creates them, so
    # the import task is where their label comes from.
    imported = open(f"{roles}/acm/tasks/import-cluster.yml", encoding="utf-8").read()
    check("imported clusters carry that label too",
          fleet_label in imported,
          "a guest the Placement cannot select gets no workloads")


def test_infra_half_of_a_site_carries_only_its_namespace():
    """What is left in sites-infra/ once the Policy owns the machines.

    The virtual machines, the DataVolume they boot and the CA that verifies it
    all moved into the Policy, which resolves what it cannot know. The namespace
    stays: a ConfigurationPolicy creates namespaced objects, not the namespace
    they go in.
    """
    print("\nthe infra half of a site")
    roles = f"{ROOT}/deploy/openshift-clusters/roles"
    ns = yaml.safe_load(render(f"{roles}/vcp-cluster/templates/sites-infra-namespace.yaml.j2"))
    check("it is a Namespace", ns["kind"] == "Namespace")
    defaults = {**extra_vars_from_wrapper(), **role_defaults(), **CONTEXT}
    check("and it is the one the machines are created in",
          ns["metadata"]["name"] == defaults["vcp_vm_namespace"])
    _, _, objects = vcp_policy()
    check("which is where the policy puts every object",
          {o["metadata"]["namespace"] for o in objects} == {ns["metadata"]["name"]})


def test_site_agnostic_roles_name_no_single_site():
    """Roles that run at either site must not name one site's variables.

    Its templates are rendered on TNF's bastion and on the ACM site's. A
    variable like bastion_private_ip always holds TNF's address, so on the ACM
    bastion the ignition server bound to an address the host does not have,
    exited 1 on every restart, and the nodes booted with nothing to fetch --
    surfacing twenty minutes later as a bootstrap timeout blamed on the
    bootstrap host. Site-specific values must arrive as role variables the
    caller sets, or be derived from the host being configured.
    """
    print("\nsite-agnostic roles")
    # Extra vars outrank include_role parameters, so a template that names one
    # can never be pointed at the other site: the caller's override is accepted
    # silently and then ignored. That is how the ACM haproxy came to health-check
    # TNF's bootstrap address across the peering connection and time out on every
    # probe. Any name run-playbook.sh supplies is therefore unusable here.
    banned = tuple(extra_vars_from_wrapper()) + (
        "cluster_domain", "cluster_name", "install_dir", "acm_install_dir")
    # hcp-guest runs against whichever hub hosts the control planes, so the
    # names that differ between sites are off limits there too -- publishing the
    # guest API on TNF's wildcard while the control plane runs on the ACM hub
    # yields a route ACM's router never answers for.
    site_specific = ("cluster_domain", "cluster_name", "bastion_private_ip",
                     "bootstrap_private_ip", "install_dir", "kubeconfig",
                     "ignition_base_url", "fencing_base_url")
    roles = {"loadbalancer": banned, "hcp-guest": site_specific}
    for role_name, forbidden in roles.items():
        _check_role_names(role_name, forbidden)


def _check_role_names(role_name, banned):
    role = f"{ROOT}/deploy/openshift-clusters/roles/{role_name}"
    for sub in ("templates", "tasks"):
        for path in sorted(glob.glob(f"{role}/{sub}/*")):
            text = open(path, encoding="utf-8").read()
            # only what the templates actually interpolate, not comments
            used = set()
            for a, b in re.findall(r"\{\{(.*?)\}\}|\{%(.*?)%\}", text, re.S):
                used |= set(re.findall(r"(?<![\w.])([a-z_][a-z0-9_]*)", a or b))
            named = sorted(used & set(banned))
            check(f"{role_name}/{os.path.basename(path)} names no single site",
                  not named, f"references {named}")


def test_fetched_credentials_are_gitignored():
    """Whatever path fetch-kubeconfig writes to must be gitignored.

    The rule used to name deploy/openshift-clusters/clusters/, a directory
    nothing ever wrote to, while the playbook fetched into deploy/clusters/ --
    so kubeconfigs and the kubeadmin password sat untracked-but-visible, one
    `git add -A` from being committed. Reading the destination out of the
    playbook keeps the two from drifting apart again.
    """
    print("\ncredential paths")
    playbook = f"{ROOT}/deploy/openshift-clusters/fetch-kubeconfig.yml"
    content = open(playbook, encoding="utf-8").read()

    destinations = set(
        re.findall(r'dest:\s*"\{\{\s*repo_root\s*\}\}/([^/"]+/[^/"{]+)', content)
    )
    check("fetch-kubeconfig declares a destination", bool(destinations),
          "no dest: found")

    ignored = open(f"{ROOT}/.gitignore", encoding="utf-8").read().splitlines()
    for destination in sorted(destinations):
        prefix = destination.rstrip("/") + "/"
        check(
            f"{prefix} is gitignored",
            any(line.strip().rstrip("/") + "/" == prefix for line in ignored),
            f"add '{prefix}' to .gitignore",
        )


def test_siteconfig_install_templates():
    """The install templates the SiteConfig operator renders our clusters from.

    These are Go templates wrapped in Jinja, which is an easy thing to get
    subtly wrong, and the cost of getting it wrong is high: a ClusterInstance
    naming a template that does not exist sits Failed on the hub, and a
    HostedCluster with one Route-strategy service left in it brings back the
    private router and its LoadBalancer, which on platform:none never gets an
    address. Both surface long after the commit that caused them.
    """
    print("\nSiteConfig install templates")
    roles = f"{ROOT}/deploy/openshift-clusters/roles"
    # The same merged context render() uses: some of these live in role
    # defaults, some are supplied by the run-playbook.sh wrapper.
    defaults = {**extra_vars_from_wrapper(), **role_defaults(), **CONTEXT}

    vcp = yaml.safe_load(render(f"{roles}/vcp-cluster/templates/vcp-cluster-templates.yaml.j2"))

    # The generator writes templateRefs by name; the installer creates a
    # ConfigMap by name. Nothing joins the two but these strings agreeing.
    check("the all-VM template is named what the site definition asks for",
          vcp["metadata"]["name"] == defaults["siteconfig_vcp_cluster_template"])

    # The whole reason for a custom set rather than ai-cluster-templates-v1:
    # the shipped InfraEnv is per node and names itself after one, which would
    # build a discovery ISO per VM.
    check("the all-VM set carries its own cluster-scoped InfraEnv",
          "InfraEnv" in vcp["data"])
    check("that InfraEnv is named for the cluster, not for a node",
          ".SpecialVars.CurrentNode" not in vcp["data"]["InfraEnv"])

    aci = yaml.safe_load(strip_go(vcp["data"]["AgentClusterInstall"]))
    # Under spec.networking, not beside it. AgentClusterInstall has no
    # spec.userManagedNetworking; a structural schema prunes what it does not
    # know, so one level up the field is accepted, dropped, and never applied.
    check("the all-VM install is user-managed networking",
          aci["spec"]["networking"].get("userManagedNetworking") is True)
    check("and says so where the CRD actually has the field",
          "userManagedNetworking" not in aci["spec"],
          "spec.userManagedNetworking is pruned by the API server")
    check("and claims no VIPs, which a pod network cannot provide",
          "apiVIPs" not in aci["spec"] and "ingressVIPs" not in aci["spec"])
    # A compact cluster has no workers, so unless the control-plane nodes are
    # schedulable there is nowhere for a workload to land. The CRD has no
    # default for this, so silence means "not schedulable".
    check("a cluster with no workers makes its control plane schedulable",
          int(defaults["vcp_worker_replicas"]) != 0
          or aci["spec"].get("mastersSchedulable") is True)
    # Counted from spec.nodes by the operator rather than restated here. The
    # site definition has to populate spec.nodes anyway -- validation rejects an
    # empty list for any cluster type but HostedControlPlane -- so restating the
    # counts would be a second place for them to disagree.
    raw = vcp["data"]["AgentClusterInstall"]
    check("it waits for as many agents as spec.nodes describes",
          ".SpecialVars.ControlPlaneAgents" in raw
          and ".SpecialVars.WorkerAgents" in raw)

    hosted = yaml.safe_load(render(
        f"{roles}/hcp-guest/templates/sites-infra-hostedcluster.yaml.j2",
        guest_name="hcp-1"))
    strategies = {s["service"]: s["servicePublishingStrategy"]["type"]
                  for s in hosted["spec"]["services"]}
    check("every hosted service is published on a NodePort",
          set(strategies.values()) == {"NodePort"},
          f"found {sorted(set(strategies.values()))}")
    check("all four services a hosted cluster needs are published",
          set(strategies) == {"APIServer", "OAuthServer", "Ignition", "Konnectivity"},
          f"found {sorted(strategies)}")
    check("the workers are KubeVirt, not Agent",
          hosted["spec"]["platform"]["type"] == "KubeVirt")

    # With the public entry point on, only the two services a human touches move
    # to the load balancer. Publishing the other two there as well would send the
    # worker VMs' own traffic out through the internet and back rather than
    # across the peering, and spec.services is immutable once the cluster exists
    # so there is no correcting it afterwards.
    public = yaml.safe_load(render(
        f"{roles}/hcp-guest/templates/sites-infra-hostedcluster.yaml.j2",
        guest_name="hcp-1",
        guest_public_control_plane=True,
        guest_public_address="lb.example.com"))
    published = {s["service"]: s["servicePublishingStrategy"]["nodePort"]
                 for s in public["spec"]["services"]}
    check("a public guest puts its API and OAuth on the load balancer",
          published["APIServer"]["address"] == "lb.example.com"
          and published["OAuthServer"]["address"] == "lb.example.com")
    check("and pins the ports the load balancer was built for",
          published["APIServer"]["port"] == defaults["guest_api_nodeport"]
          and published["OAuthServer"]["port"] == defaults["guest_oauth_nodeport"])
    check("while the services only the worker VMs speak stay private",
          published["Ignition"]["address"] == CONTEXT["guest_api_address"]
          and published["Konnectivity"]["address"] == CONTEXT["guest_api_address"])

    pool = yaml.safe_load(render(
        f"{roles}/hcp-guest/templates/sites-infra-nodepool.yaml.j2",
        guest_name="hcp-1"))
    check("the node pool is KubeVirt too",
          pool["spec"]["platform"]["type"] == "KubeVirt")
    check("it asks for as many workers as the profile is configured for",
          pool["spec"]["replicas"] == defaults["guest_nodepool_replicas"])


def test_agent_cluster_install_networking_is_nested():
    """The same field, in the role that creates an AgentClusterInstall directly.

    vcp-cluster/tasks/install.yml builds this object without SiteConfig, and had
    userManagedNetworking one level too high for as long as it has existed --
    accepted by the API server, pruned on write, and never applied. It worked
    only because platformType: None makes the installer infer user-managed
    networking anyway. Both paths are checked so they cannot drift apart again.
    """
    print("\nAgentClusterInstall networking")
    path = f"{ROOT}/deploy/openshift-clusters/roles/vcp-cluster/tasks/install.yml"
    tasks = yaml.safe_load(open(path, encoding="utf-8"))
    aci = next(t["kubernetes.core.k8s"]["definition"] for t in tasks
               if t.get("kubernetes.core.k8s", {}).get("definition", {}).get("kind")
               == "AgentClusterInstall")
    check("the role agrees about a compact cluster's schedulable control plane",
          str(aci["spec"].get("mastersSchedulable", "")).lower().find("worker_replicas") != -1
          or aci["spec"].get("mastersSchedulable") is True,
          f"install.yml has mastersSchedulable={aci['spec'].get('mastersSchedulable')!r}")
    check("the role nests userManagedNetworking under networking",
          aci["spec"]["networking"].get("userManagedNetworking") is True)
    check("and does not leave a copy where it would be pruned",
          "userManagedNetworking" not in aci["spec"])


def test_both_vcp_paths_approve_their_agents():
    """An agent nobody approves is a cluster that never installs.

    Approval is manual by design in the assisted installer, and both ways of
    building an all-VM cluster have to do it: the machines register themselves
    and then wait. The GitOps path had no approval at all, so its
    AgentClusterInstall would sit waiting for a count of approved agents that
    never arrived, reporting only "insufficient agents".
    """
    print("\nagent approval")
    tasks = f"{ROOT}/deploy/openshift-clusters/roles/vcp-cluster/tasks"
    shared = "approve-agents.yml"
    check("there is one definition of what approval means",
          os.path.exists(f"{tasks}/{shared}"))
    for path, name in ((f"{tasks}/install.yml", "the direct path"),
                       (f"{tasks}/siteconfig-site.yml", "the GitOps path")):
        body = open(path, encoding="utf-8").read()
        check(f"{name} approves its agents", shared in body,
              f"{os.path.basename(path)} never includes {shared}")
    body = open(f"{tasks}/{shared}", encoding="utf-8").read()
    check("approval is scoped to this cluster's own namespace",
          "-n {{ vcp_cluster_name }}" in body)
    check("and it assigns a role rather than approving blind",
          '\\"role\\"' in body or '"role"' in body)


def test_app_instances_are_independent():
    """Two instances of the app, not one scaled up.

    Each is a whole pipeline in its own namespace and takes two GPUs, so the
    GPU precondition has to scale with the count -- otherwise a second instance
    is admitted onto a guest that cannot schedule it and its pods sit Pending on
    nvidia.com/gpu, which reads as a broken GPU stage rather than as arithmetic.
    """
    print("\napp instances")
    tasks = f"{ROOT}/deploy/openshift-clusters/roles/app/tasks"
    defaults = role_defaults()
    main = open(f"{tasks}/main.yml", encoding="utf-8").read()
    instance = open(f"{tasks}/instance.yml", encoding="utf-8").read()

    # The assertion itself, not the message beside it -- the message mentions
    # the same arithmetic, so matching the file would pass on a hardcoded check.
    assertion = [l for l in main.splitlines() if l.strip().startswith("that:")
                 and "app_gpu_total" in l]
    check("the GPU check scales with the number of instances",
          any("app_instances" in l for l in assertion),
          f"assertion is {assertion or 'missing'} -- a second instance would be "
          "admitted onto a guest that cannot schedule it")
    check("each instance gets its own namespace",
          "app_namespace_base }}-{{ app_instance }}" in instance)
    check("the images are built once, by the first instance",
          "app_instance | int == 1" in instance)
    check("and the others are allowed to pull them",
          "system:image-puller" in instance)
    # The app's own Makefile applied a hardcoded namespace whatever NAMESPACE
    # said, which put a second instance's components in a namespace nothing had
    # created.
    makefile = open(f"{ROOT}/app/Makefile", encoding="utf-8").read()
    check("the app Makefile creates the namespace it was asked for",
          "name: visual-inspection|name: $(NAMESPACE)" in makefile,
          "00-namespace.yaml is applied verbatim, so NAMESPACE is ignored")
    check("one instance is still the default",
          int(defaults["app_instances"]) == 1)


def test_guest_vms_are_spread_across_the_infra_cluster():
    """Both kinds of guest ask to be spread over the infra cluster's nodes.

    Left alone they pile onto whichever node has room first, which on a two-node
    infra cluster means one node can end up carrying a whole guest -- and losing
    that node then takes the guest with it rather than half of it.

    ScheduleAnyway rather than DoNotSchedule on purpose: an unbalanced VM is
    better than a Pending one, which is the trade that matters when a node is
    down and the remaining one is the only place anything can run.
    """
    print("\nspreading guest VMs")
    roles = f"{ROOT}/deploy/openshift-clusters/roles"

    _, _, objects = vcp_policy()
    vms = [o for o in objects if o["kind"] == "VirtualMachine"]
    for vm in vms:
        tsc = vm["spec"]["template"]["spec"].get("topologySpreadConstraints", [])
        check(f"{vm['metadata']['name']}: asks to be spread by hostname",
              any(c["topologyKey"] == "kubernetes.io/hostname" for c in tsc))
        check(f"{vm['metadata']['name']}: as evenly as the nodes allow",
              any(c.get("maxSkew") == 1 for c in tsc))
        check(f"{vm['metadata']['name']}: but is still schedulable when it cannot be",
              all(c.get("whenUnsatisfiable") == "ScheduleAnyway" for c in tsc))

    # HyperShift owns the NodePool's VM template, so the only lever is the
    # annotation that opts it into spread constraints instead of the weaker
    # preferred anti-affinity it uses by default.
    pool = yaml.safe_load(render(
        f"{roles}/hcp-guest/templates/sites-infra-nodepool.yaml.j2",
        guest_name="hcp-1"))
    check("a hosted guest's node pool opts into topology spread constraints",
          "hypershift.openshift.io/nodepool-supports-kubevirt-topology-spread-constraints"
          in pool["metadata"]["annotations"])


def test_site_definitions():
    """One ClusterInstance per cluster, and what has to be true of every one.

    spec.nodes is empty in both profiles on purpose. A NodeSpec requires a BMC
    address, a boot MAC and a credentials Secret that the operator's validator
    checks for before rendering -- none of which exists for a KubeVirt VM. An
    entry here would mean three fabrications per node and a dummy Secret on the
    hub, for fields no rendered manifest would ever read.
    """
    print("\nsite definitions")
    roles = f"{ROOT}/deploy/openshift-clusters/roles"
    defaults = {**extra_vars_from_wrapper(), **role_defaults(), **CONTEXT}
    # Only the all-VM profile. The hosted guests stopped being ClusterInstances
    # when their control planes moved to TNF: the SiteConfig operator expands a
    # ClusterInstance into the cluster it runs in, which is the hub.
    sites = {
        "vcp-cluster": defaults["siteconfig_vcp_cluster_template"],
    }
    for role, template_name in sites.items():
        ci = yaml.safe_load(render(f"{roles}/{role}/templates/clusterinstance.yaml.j2"))
        name = os.path.basename(role)
        check(f"{name}: it is a ClusterInstance", ci["kind"] == "ClusterInstance")
        check(f"{name}: it points at the template its role creates",
              [r["name"] for r in ci["spec"]["templateRefs"]] == [template_name])
        check(f"{name}: the template namespace agrees with where it is created",
              all(r["namespace"] == defaults["siteconfig_template_namespace"]
                  for r in ci["spec"]["templateRefs"]))
        # The operator's own rule, and it runs opposite ways for the two
        # profiles: a hosted control plane must have no control-plane agents,
        # and anything else must have at least one. An empty list on the wrong
        # profile is accepted by the CRD and rejected at reconcile, which is a
        # quiet way to never get a cluster.
        masters = [n for n in ci["spec"]["nodes"] if n.get("role") == "master"]
        if ci["spec"]["clusterType"] == "HostedControlPlane":
            check(f"{name}: a hosted control plane claims no control-plane agents",
                  not masters, f"found {len(masters)}")
        else:
            check(f"{name}: it declares at least one control-plane agent",
                  len(masters) >= 1, "spec.nodes has no role: master")
            check(f"{name}: every node names a template and BMC credentials",
                  all(n.get("templateRefs") and n.get("bmcCredentialsName")
                      and n.get("bmcAddress") and n.get("bootMACAddress")
                      for n in ci["spec"]["nodes"]))
        # The install templates hardcode namespace: .Spec.ClusterName, so a
        # namespace that disagrees renders manifests into a namespace that does
        # not exist.
        ns = yaml.safe_load(render(f"{roles}/{role}/templates/site-namespace.yaml.j2"))
        check(f"{name}: cluster name, namespace and folder all agree",
              ci["metadata"]["namespace"] == ci["spec"]["clusterName"]
              == ns["metadata"]["name"])


def test_guest_time_is_configured_at_install_time():
    """Each ACM-built site carries a chrony MachineConfig, by its own mechanism.

    The two profiles reach the node by different routes and a reference that
    misses is silent in both: the manifest simply never applies and the cluster
    comes up with whatever chrony the image shipped. So the name on the
    ConfigMap and the name in the thing that points at it are checked against
    each other rather than each being checked alone.
    """
    print("\nguest node time")
    roles = f"{ROOT}/deploy/openshift-clusters/roles"
    defaults = {**extra_vars_from_wrapper(), **role_defaults(), **CONTEXT}

    def chrony_of(cm, key):
        """The MachineConfig under one ConfigMap key, and its decoded chrony.conf."""
        mc = yaml.safe_load(cm["data"][key])
        source = mc["spec"]["config"]["storage"]["files"][0]["contents"]["source"]
        _, _, encoded = source.partition(";base64,")
        return mc, base64.b64decode(encoded).decode()

    # --- the all-VM profile: spec.extraManifestsRefs -> manifestsConfigMapRefs
    vcp = yaml.safe_load(render(f"{roles}/vcp-cluster/templates/extra-manifests.yaml.j2"))
    vcp_ci = yaml.safe_load(render(f"{roles}/vcp-cluster/templates/clusterinstance.yaml.j2"))
    check("vcp: the site definition references the ConfigMap beside it",
          [r["name"] for r in vcp_ci["spec"]["extraManifestsRefs"]]
          == [vcp["metadata"]["name"]])
    # The operator resolves the reference in the ClusterInstance's namespace, so
    # a ConfigMap written anywhere else is invisible to it.
    check("vcp: the ConfigMap lands in the cluster's own namespace",
          vcp["metadata"]["namespace"] == vcp_ci["metadata"]["namespace"])
    check("vcp: its keys are filenames, which is how they are ordered",
          all(k.endswith(".yaml") for k in vcp["data"]), list(vcp["data"]))
    key = f"{defaults['guest_chrony_machine_config_name']}.yaml"
    mc, conf = chrony_of(vcp, key)
    check("vcp: the embedded manifest is a MachineConfig", mc["kind"] == "MachineConfig")
    # vcp-1 is three control-plane VMs and no workers, so master is the only
    # pool a MachineConfig can land in.
    check("vcp: it targets the pool this profile actually has",
          mc["metadata"]["labels"]["machineconfiguration.openshift.io/role"] == "master")
    check("vcp: the decoded file is a chrony config",
          "server " in conf and "driftfile" in conf, conf)
    # The clock has to be right before the manifest exists: an agent validates
    # skew against the hub before installation starts.
    check("vcp: the discovery phase gets the same time source",
          vcp_ci["spec"]["additionalNTPSources"] == defaults["guest_ntp_sources"])

    # --- the hosted profile: NodePool.spec.config, a different shape entirely
    hcp = yaml.safe_load(render(
        f"{roles}/hcp-guest/templates/sites-infra-extra-manifests.yaml.j2",
        guest_name=defaults["guest_name"]))
    check("hcp: a NodePool config ConfigMap holds exactly one manifest",
          list(hcp["data"]) == ["config"], list(hcp["data"]))
    mc, conf = chrony_of(hcp, "config")
    check("hcp: the embedded manifest is a MachineConfig", mc["kind"] == "MachineConfig")
    # A hosted cluster's control plane is pods on the hub; its NodePool makes
    # workers. A MachineConfig labelled master here would apply to nothing.
    check("hcp: it targets worker, because that is all a NodePool makes",
          mc["metadata"]["labels"]["machineconfiguration.openshift.io/role"] == "worker")
    check("hcp: the decoded file is a chrony config",
          "server " in conf and "driftfile" in conf, conf)
    # Ansible renders both now, so the two names can be compared directly rather
    # than through the Go template the SiteConfig operator used to expand.
    nodepool = yaml.safe_load(render(
        f"{roles}/hcp-guest/templates/sites-infra-nodepool.yaml.j2",
        guest_name=defaults["guest_name"]))
    check("hcp: the NodePool references the ConfigMap the site writes",
          any(c["name"] == hcp["metadata"]["name"]
              for c in nodepool["spec"]["config"]),
          str(nodepool["spec"]["config"]))
    check("hcp: and looks for it in the namespace the cluster lives in",
          hcp["metadata"]["namespace"] == nodepool["metadata"]["namespace"],
          "a NodePool's config ConfigMap resolves beside the HostedCluster")

    # Both profiles write the same bytes, and the point of holding the body in
    # group_vars is that they keep doing so.
    check("both sites are given the same time configuration",
          chrony_of(vcp, key)[1] == chrony_of(hcp, "config")[1])


def test_infra_half_of_a_site_carries_no_credential():
    """The virtual machines are committed; what they boot from is not.

    The discovery ISO's URL is issued by the hub's image service and carries a
    signed token with no expiry, and the ISO it fetches contains the cluster's
    pull secret and SSH key. The repository these manifests are committed to is
    public, so the URL must never reach it -- the VMs reference a DataVolume by
    name and Ansible creates that DataVolume on the infra cluster directly.
    """
    print("\nthe infra half of a site")
    roles = f"{ROOT}/deploy/openshift-clusters/roles"
    defaults = {**extra_vars_from_wrapper(), **role_defaults(), **CONTEXT}
    rendered = render(f"{roles}/vcp-cluster/templates/sites-infra-virtualmachines.yaml.j2")
    vms = [d for d in yaml.safe_load_all(rendered) if d]

    check("one VirtualMachine per planned node",
          len(vms) == len(CONTEXT["vcp_nodes"]),
          f"{len(vms)} rendered for {len(CONTEXT['vcp_nodes'])} nodes")
    check("all of them are VirtualMachines",
          all(d["kind"] == "VirtualMachine" for d in vms))
    check("they boot the cluster's shared discovery volume",
          all(any(v.get("dataVolume", {}).get("name", "").endswith("-discovery")
                  for v in d["spec"]["template"]["spec"]["volumes"]) for d in vms))
    # The whole point: nothing here may be a credential.
    forbidden = ("isoDownloadURL", "byapikey", "dockerconfigjson",
                 "BEGIN RSA", "BEGIN OPENSSH", "password")
    leaked = [w for w in forbidden if w in rendered]
    check("nothing in them is a credential", not leaked, f"found {leaked}")
    check("no DataVolume importing over http is committed",
          "http:" not in rendered,
          "an http source here would be the ISO URL, which is a bearer token")


def test_control_planes_are_hosted_on_tnf():
    """The guest control planes run on TNF, and the hub is not bare metal.

    These two facts are one change. A hosted control plane is pods, and its
    workers are KubeVirt VMs; putting both on TNF is what leaves the hub with no
    virtual machines to run, which is what lets its node stop being a .metal
    shape. Undoing either half silently re-breaks the other, and neither is
    visible in a rendered template -- the evidence is in a Makefile target, a
    play's role list and two instance types.
    """
    print("\nwhere the guest control planes run")
    deploy = f"{ROOT}/deploy"
    makefile = open(f"{deploy}/Makefile", encoding="utf-8").read()

    # The 'all' target, with its line continuations folded back together.
    all_target = re.search(r"^all:(.*?)(?=\n\t|\n[^\s#])", makefile,
                           re.S | re.M)
    stages = all_target.group(1).replace("\\\n", " ").split() if all_target else []
    check("'make all' builds the guests on TNF's own MultiCluster Engine",
          "guests" in stages,
          f"the all target runs {stages}")
    check("'make all' does not build them on the hub",
          "hcp-make-guests-from-acm" not in stages,
          "the hub-hosted variant puts the control-plane pods on the ACM node")

    # What actually decides it: the play 'guests' runs targets the TNF bastion
    # and hands hcp no infra kubeconfig, so hcp omits --infra-kubeconfig-file
    # and both the control-plane pods and the worker VMs land on TNF.
    guests_play = open(f"{deploy}/openshift-clusters/40-hcp-guests.yml",
                       encoding="utf-8").read()
    play = yaml.safe_load(guests_play)[0]
    check("the guest play runs against the TNF bastion",
          play["hosts"] == "bastion",
          f"it targets {play['hosts']}")
    check("it hands hcp no infra kubeconfig, so the VMs stay where it runs",
          "guest_infra_kubeconfig" not in guests_play,
          "setting it would send the worker VMs to another cluster")

    # The hub keeps its storage -- the MultiClusterHub's search component and
    # observability both want PVCs -- but not virtualization.
    acm_site = open(f"{deploy}/openshift-clusters/36-acm-site.yml",
                    encoding="utf-8").read()
    roles = re.findall(r"^\s*name:\s*(\S+)\s*$", acm_site, re.M)
    check("the hub still installs LVM Storage",
          "lvm-storage" in roles,
          "search and observability request PVCs on the hub itself")

    # The hub keeps OpenShift Virtualization for ACM's virtualization views,
    # which come from the operator's console plugin and CRDs. The coupling that
    # matters is with the instance type: virt-handler needs /dev/kvm, an EC2
    # instance that is not .metal does not have it, so CNV on a non-metal hub
    # has to run emulated or HyperConverged never reaches Available.
    if "cnv" in roles:
        hub_cnv = re.search(r"name: cnv\n(.*?)(?=\n    - name:|\Z)", acm_site, re.S)
        block = hub_cnv.group(1) if hub_cnv else ""
        check("the hub's OpenShift Virtualization runs emulated",
              "cnv_use_emulation: true" in block,
              "a non-metal node has no /dev/kvm, so virt-handler cannot start")
        check("and permits no host devices",
              "cnv_permit_gpu: false" in block,
              "there are no GPUs at the ACM site to permit")

    # Bare metal was for KVM. With KVM gone the shape is ordinary, and a .metal
    # default here is roughly ten times the cost for nothing.
    env_template = open(f"{ROOT}/config/instance.env.template",
                        encoding="utf-8").read()
    declared = re.search(r"^export ACM_SNO_INSTANCE_TYPE=(\S+)", env_template, re.M)
    check("the hub's instance type is declared",
          declared is not None)
    if declared:
        check("the hub is not a bare-metal shape",
              not declared.group(1).endswith(".metal"),
              f"ACM_SNO_INSTANCE_TYPE is {declared.group(1)}")

    cnv_defaults = open(f"{deploy}/openshift-clusters/roles/cnv/defaults/main.yml",
                        encoding="utf-8").read()
    check("emulation is off by default",
          re.search(r"^cnv_use_emulation:\s*false", cnv_defaults, re.M) is not None,
          "TNF is bare metal and every VM that matters runs there")
    virt_mce = open(f"{deploy}/openshift-clusters/30-virt-mce.yml",
                    encoding="utf-8").read()
    check("TNF does not run KubeVirt emulated",
          "cnv_use_emulation: true" not in virt_mce,
          "emulating on the nodes holding the T4s would defeat the whole point")

    stack = _cfn_load(f"{deploy}/aws-infra/templates/sno-compute-stack.yaml")
    default = stack["Parameters"]["SnoInstanceType"]["Default"]
    check("the stack's own default is not bare metal either",
          not str(default).endswith(".metal"),
          f"SnoInstanceType defaults to {default}")


def test_the_guest_entry_point_follows_the_control_plane():
    """The public load balancer must target whichever cluster hosts the guest.

    A NodePort only answers on nodes of the cluster running the pods behind it.
    While the control plane lived on the ACM hub this stack was built in the ACM
    VPC against the hub's single node, and moving the control plane to TNF makes
    that silently wrong: the stack still builds, the targets never turn healthy,
    and the guest console times out with nothing to say why.

    It is also ordered. The address is baked into the HostedCluster at creation
    and spec.services is immutable afterwards, so the load balancer has to exist
    before the guest stage runs -- which is why 'all' names it.
    """
    print("\nthe guest's public entry point")
    deploy = f"{ROOT}/deploy"
    script = open(f"{deploy}/aws-infra/scripts/create-guest-lb.sh",
                  encoding="utf-8").read()

    check("it can target the TNF site",
          "master0_instance_id" in script and "master1_instance_id" in script,
          "TNF hosts the control plane, so TNF's nodes answer the NodePorts")
    check("it still knows the hub-hosted layout",
          "sno_instance_id" in script,
          "'make hcp-make-guests-from-acm' publishes on the hub's own node")
    check("it refuses a site it does not know",
          "unknown site" in script,
          "a typo must not quietly fall through to the wrong VPC")
    # Both nodes, because either can be fenced.
    check("a two-node host registers both its nodes",
          "SecondTargetInstanceId" in script)

    stack = _cfn_load(f"{deploy}/aws-infra/templates/guest-lb-stack.yaml")
    check("the stack accepts a second target",
          "SecondTargetInstanceId" in stack["Parameters"])
    check("and defaults it to empty for a single-node host",
          stack["Parameters"]["SecondTargetInstanceId"].get("Default") == "")
    check("the second target is conditional",
          "HasSecondTarget" in stack.get("Conditions", {}),
          "a single-node host must not register an empty instance id")
    for group in ("ApiTargetGroup", "OAuthTargetGroup"):
        targets = stack["Resources"][group]["Properties"]["Targets"]
        check(f"{group} registers two targets",
              len(targets) == 2, f"{len(targets)} declared")
        # Each entry carries its own Port; losing it silently sends traffic to
        # the target group's default port on the second node.
        conditional = [t for t in targets if "Fn::If" in t]
        check(f"{group}'s conditional target keeps its port",
              conditional and "Port" in conditional[0]["Fn::If"][1],
              "an entry without Port falls back to the group's port")

    makefile = open(f"{deploy}/Makefile", encoding="utf-8").read()
    all_target = re.search(r"^all:(.*?)(?=\n\t|\n[^\s#])", makefile, re.S | re.M)
    stages = all_target.group(1).replace("\\\n", " ").split() if all_target else []
    check("'make all' builds the entry points itself",
          "guest-lbs" in stages,
          "a clean account has no recorded load balancer, so 'all' must make one")
    if "guest-lbs" in stages and "guests" in stages:
        check("and builds them before the guests",
              stages.index("guest-lbs") < stages.index("guests"),
              "spec.services is immutable once the HostedCluster exists")


def test_applicationset_templates_match_their_template_engine():
    """goTemplate: true means Go syntax, in every reference.

    Argo CD has two template engines. The older fasttemplate takes {{name}};
    Go templates take {{.name}}, and mixing them fails at generation with
    'function "name" not defined'. The ApplicationSet still reports Healthy,
    generates nothing, and the workloads simply never arrive -- there is no
    error anywhere except in the ApplicationSet's own conditions.
    """
    print("\nApplicationSet templating")
    tmpl = f"{ROOT}/deploy/openshift-clusters/roles/gitops/templates"
    for path in sorted(glob.glob(f"{tmpl}/applicationset*.yaml.j2")):
        rendered = render(path)
        doc = yaml.safe_load(rendered)
        name = os.path.basename(path)
        if not doc.get("spec", {}).get("goTemplate"):
            continue
        # Everything the engine will expand, from the whole spec at once.
        body = yaml.safe_dump(doc["spec"])
        bare = re.findall(r"\{\{\s*([a-zA-Z_][\w.]*)\s*\}\}", body)
        offenders = [b for b in bare if not b.startswith(".")]
        check(f"{name}: every reference uses Go template syntax",
              not offenders,
              f"{offenders} should be dotted, e.g. {{{{.name}}}}")


def test_a_site_folder_creates_its_namespace_first():
    """Every namespace a site folder ships must sync before what goes in it.

    Argo CD reconciles RBAC with `kubectl auth reconcile`, which resolves the
    Role and RoleBinding's namespace up front. Without an explicit wave that can
    run before the namespace exists and fail the *entire* sync, not just the
    RBAC -- so a rebuild that removed the namespace could never recreate it.
    """
    print("\nsite folder ordering")
    roles = f"{ROOT}/deploy/openshift-clusters/roles"
    for path in sorted(glob.glob(f"{roles}/*/templates/*namespace*.yaml.j2")):
        doc = yaml.safe_load(render(path, guest_name="hcp-1"))
        if not doc or doc.get("kind") != "Namespace":
            continue
        name = os.path.basename(path)
        wave = (doc["metadata"].get("annotations") or {}).get(
            "argocd.argoproj.io/sync-wave")
        check(f"{name}: syncs before the objects placed in it",
              wave is not None and int(wave) < 0,
              f"sync-wave={wave}")


def test_the_vm_pods_carry_what_the_services_select():
    """expose.yml's Services select pods; the labels must be on the pod template.

    A virt-launcher pod inherits spec.template.metadata.labels, not the
    VirtualMachine's own. A role label set only on the VM matches nothing, the
    Services find no endpoints, and the NodePort listens with no backend --
    refusing every connection in milliseconds, which reads as a firewall or a
    dead API rather than as a selector that matches no pod.
    """
    print("\nall-VM pod labels")
    roles = f"{ROOT}/deploy/openshift-clusters/roles/vcp-cluster"
    expose = yaml.safe_load(open(f"{roles}/tasks/expose.yml", encoding="utf-8"))
    selectors = [t["kubernetes.core.k8s"]["definition"]["spec"]["selector"]
                 for t in expose
                 if isinstance(t.get("kubernetes.core.k8s"), dict)
                 and t["kubernetes.core.k8s"].get("definition", {}).get("kind") == "Service"]
    check("the exposure tasks declare Service selectors", bool(selectors))

    # Both templates that create the VMs must satisfy every selector key.
    for name, expr in (("virtualmachine.yaml.j2",
                        r"template:.*?metadata:.*?labels:(.*?)spec:"),
                       ("policy-virtualmachines.yaml.j2",
                        r"template:\s*\n\s*metadata:\s*\n\s*labels:(.*?)\n\s*spec:")):
        body = open(f"{roles}/templates/{name}", encoding="utf-8").read()
        m = re.search(expr, body, re.S)
        pod_labels = m.group(1) if m else ""
        for sel in selectors:
            for key in sel:
                check(f"{name}: its pod template carries {key}",
                      key in pod_labels,
                      "a Service selecting it would find no endpoints")


def test_both_vcp_paths_publish_the_cluster():
    """A cluster built from Git must be reachable, same as one built directly.

    With user-managed networking the installer builds no VIPs, and the nodes'
    own addresses are on a user-defined network only the infra cluster can
    route. Without the Services that publish the API and ingress, the cluster
    installs, reports healthy and cannot be reached -- not from a workstation,
    and not from the ACM bastion, which is where the klusterlet is installed
    from, so it cannot even be imported into the hub.
    """
    print("\nboth all-VM paths publish the cluster")
    tasks = f"{ROOT}/deploy/openshift-clusters/roles/vcp-cluster/tasks"

    def reachable_from(entry):
        """The steps an entry point runs, following one level of include."""
        body = open(f"{tasks}/{entry}", encoding="utf-8").read()
        for inc in re.findall(r"include_tasks:\s*(\S+\.yml)", body):
            path = f"{tasks}/{inc}"
            if os.path.exists(path):
                body += open(path, encoding="utf-8").read()
        return body

    for entry, label in (("main.yml", "the direct path"),
                         ("siteconfig-site.yml", "the GitOps path")):
        body = reachable_from(entry)
        for step, why in (("approve-agents.yml", "agents nothing approves are counted by nothing"),
                          ("dns.yml", "no records means every agent fails validation"),
                          ("expose.yml", "an unreachable cluster cannot even be imported")):
            check(f"{label} runs {step}", step in body, why)


def test_rendering_sites_does_not_block_on_one_guest():
    """'make sites' renders the whole fleet; one guest must not stall it.

    Importing a cluster waits for it to report Available and for two addons --
    over half an hour between them. That belongs in the stage whose job is the
    import. In the stage that renders site definitions it stands between the
    other clusters and the steps they need, which is how an all-VM cluster came
    to sit installing with no DNS records published for it.
    """
    print("\nthe sites stage")
    pb = f"{ROOT}/deploy/openshift-clusters"
    sites = open(f"{pb}/42-sites.yml", encoding="utf-8").read()
    check("it starts the guest import without waiting on it",
          "import_wait_online: false" in sites,
          "a guest still installing would stop the run before the rest")
    # The dedicated stage keeps waiting -- that is what it is for.
    dedicated = open(f"{pb}/43-import-guests.yml", encoding="utf-8").read()
    check("the dedicated import stage still waits",
          "import_wait_online: false" not in dedicated)
    guests = open(f"{pb}/40-hcp-guests.yml", encoding="utf-8").read()
    check("and so does the stage that creates the guests",
          "import_wait_online: false" not in guests)


def test_the_guest_is_built_from_git_onto_its_host():
    """hcp-1's definition is delivered to the cluster that runs it.

    It used to be a ClusterInstance under sites/, expanded by the SiteConfig
    operator. That operator runs on the hub and expands into the cluster it is
    running in, so a ClusterInstance for a hosted cluster always produces a
    control plane on the hub -- structural, not a setting. The manifests
    therefore live under sites-infra/, the half of a site applied to the infra
    cluster, which applicationset-infra.yaml.j2 already delivers there.
    """
    print("\nthe guest cluster, built from Git")
    roles = f"{ROOT}/deploy/openshift-clusters/roles"
    ctx = {**extra_vars_from_wrapper(), **role_defaults(), **CONTEXT,
           "guest_name": "hcp-1"}
    tmpl = f"{roles}/hcp-guest/templates"

    hc = yaml.safe_load(render(f"{tmpl}/sites-infra-hostedcluster.yaml.j2", **ctx))
    check("the site ships a HostedCluster, not a ClusterInstance",
          hc["kind"] == "HostedCluster", hc["kind"])

    # The whole point. This block is what sends the worker VMs to a different
    # cluster from the control plane; without it HyperShift creates them where
    # it runs, which is where the GPUs are.
    kubevirt = hc["spec"]["platform"]["kubevirt"]
    check("it names no infra credential",
          "credentials" not in kubevirt,
          "a credentials block would put the VMs on another cluster")

    # The guest's networks must not overlap the cluster hosting it. Its worker
    # VMs sit on the host's pod network and resolve through the host's DNS, so
    # a guest service network covering that resolver's address is captured by
    # the guest's own OVN as soon as it starts -- and every image pull in the
    # guest then fails against a CoreDNS that cannot exist yet.
    net = hc["spec"]["networking"]
    guest_svc = net["serviceNetwork"][0]["cidr"]
    guest_pod = net["clusterNetwork"][0]["cidr"]
    check("the guest's service network is its own, not the host's",
          guest_svc != ctx["service_network_cidr"],
          f"guest {guest_svc} collides with host {ctx['service_network_cidr']}")
    check("the guest's pod network is its own, not the host's",
          guest_pod != ctx["cluster_network_cidr"],
          f"guest {guest_pod} collides with host {ctx['cluster_network_cidr']}")

    published = {s["service"]: s["servicePublishingStrategy"]
                 for s in hc["spec"]["services"]}
    check("every service is a NodePort",
          all(p["type"] == "NodePort" for p in published.values()),
          "a LoadBalancer Service stays <pending> for ever on platform:none")
    check("the VMs reach ignition and konnectivity on the hosting node",
          published["Ignition"]["nodePort"]["address"] == CONTEXT["guest_api_address"]
          and published["Konnectivity"]["nodePort"]["address"] == CONTEXT["guest_api_address"])
    # An address that renders empty is the expensive kind of wrong:
    # spec.services is immutable, so the cluster has to be destroyed to correct
    # it. This shipped once, when the task that reads the load balancer's name
    # was removed along with the template set that used to hold it.
    for name, strategy in published.items():
        addr = strategy["nodePort"].get("address")
        check(f"{name} is published on a real address",
              bool(addr) and addr not in ("None", "~"), repr(addr))

    # And with the public entry point on, the two a human touches move to the
    # load balancer while the two only the VMs speak stay on the node.
    pub = yaml.safe_load(render(
        f"{tmpl}/sites-infra-hostedcluster.yaml.j2",
        **{**ctx, "guest_public_control_plane": True,
           "guest_public_address": "lb.example.com"}))
    pubs = {s["service"]: s["servicePublishingStrategy"]["nodePort"]
            for s in pub["spec"]["services"]}
    check("a public guest still names a real address for every service",
          all(v.get("address") for v in pubs.values()),
          str({k: v.get("address") for k, v in pubs.items()}))
    check("and pins the ports its load balancer was built for",
          pubs["APIServer"]["port"] == ctx["guest_api_nodeport"]
          and pubs["OAuthServer"]["port"] == ctx["guest_oauth_nodeport"])

    np = yaml.safe_load(render(f"{tmpl}/sites-infra-nodepool.yaml.j2", **ctx))
    check("the NodePool names the same cluster",
          np["spec"]["clusterName"] == hc["metadata"]["name"])
    check("and lands in the same namespace",
          np["metadata"]["namespace"] == hc["metadata"]["namespace"],
          "a NodePool's config ConfigMap resolves in the cluster's namespace")
    # Sync waves, because a NodePool naming a cluster that does not exist yet is
    # rejected rather than retried.
    wave = lambda d: int(d["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"])
    check("the HostedCluster syncs before the NodePool",
          wave(hc) < wave(np), f"{wave(hc)} then {wave(np)}")
    cm = yaml.safe_load(render(f"{tmpl}/sites-infra-extra-manifests.yaml.j2", **ctx))
    check("and the config it references syncs before both",
          wave(cm) < wave(hc))
    check("the NodePool references that ConfigMap by name",
          any(c["name"] == cm["metadata"]["name"] for c in np["spec"]["config"]))

    # Secrets are created on the cluster by Ansible and referenced by name.
    site = open(f"{roles}/hcp-guest/tasks/siteconfig-site.yml", encoding="utf-8").read()
    for rendered in (hc, np, cm):
        text = yaml.safe_dump(rendered)
        leaked = [w for w in ("dockerconfigjson", "BEGIN RSA", "auths") if w in text]
        check(f"no credential is committed in the {rendered['kind']}",
              not leaked, f"found {leaked}")
    check("the secrets are created on the hosting cluster, not the hub",
          "guest_infra_kubeconfig" in site and "op_kubeconfig" not in site,
          "writing them to the hub would leave the HostedCluster unable to start")


def main():
    test_every_template_renders()
    test_install_config()
    test_ignition_pointers()
    test_redfish_shim_config()
    test_haproxy_config()
    test_cloudformation()
    test_jsonpath_filters_are_shell_quoted()
    test_shell_commands_are_not_split_by_stray_newlines()
    test_nothing_reads_role_defaults_from_outside()
    test_play_path_fallbacks_keep_the_system_directories()
    test_iommu_is_enabled_at_install_time()
    test_vcp_virtual_machine()
    test_siteconfig_install_templates()
    test_agent_cluster_install_networking_is_nested()
    test_both_vcp_paths_approve_their_agents()
    test_app_instances_are_independent()
    test_guest_vms_are_spread_across_the_infra_cluster()
    test_site_definitions()
    test_guest_time_is_configured_at_install_time()
    test_virtual_machines_are_an_acm_policy()
    test_vcp_nodes_get_addresses_of_their_own()
    test_fleet_workloads_are_pulled_not_pushed()
    test_infra_half_of_a_site_carries_only_its_namespace()
    test_site_agnostic_roles_name_no_single_site()
    test_fetched_credentials_are_gitignored()
    test_control_planes_are_hosted_on_tnf()
    test_the_guest_entry_point_follows_the_control_plane()
    test_applicationset_templates_match_their_template_engine()
    test_a_site_folder_creates_its_namespace_first()
    test_the_vm_pods_carry_what_the_services_select()
    test_both_vcp_paths_publish_the_cluster()
    test_rendering_sites_does_not_block_on_one_guest()
    test_the_guest_is_built_from_git_onto_its_host()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed:")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("All template checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
