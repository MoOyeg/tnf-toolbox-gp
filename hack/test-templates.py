#!/usr/bin/env python3
"""Static checks over the templates, run by 'make verify'.

None of this needs AWS, a cluster, or credentials. It catches the class of
mistake that otherwise surfaces forty minutes into a deploy: a Jinja typo, an
install-config that lost its fencing block, an ignition pointer that grew past
the CloudFormation parameter limit, a CloudFormation !Ref to a parameter nobody
declared.
"""

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
    # loop_var for the virtual machine template
    vm={"name": "vcp-1-cp-1", "role": "control-plane",
        "cores": 8, "memory": "20Gi", "gpus": 0},
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
    env = Environment(loader=FileSystemLoader(directory), undefined=StrictUndefined)
    return env.get_template(name).render(
        **{**extra_vars_from_wrapper(), **role_defaults(), **CONTEXT, **overrides})


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
    test_vcp_virtual_machine()
    test_site_agnostic_roles_name_no_single_site()
    test_fetched_credentials_are_gitignored()

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
