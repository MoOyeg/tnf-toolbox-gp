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


def render(template_path, **overrides):
    directory, name = os.path.split(template_path)
    env = Environment(loader=FileSystemLoader(directory), undefined=StrictUndefined)
    return env.get_template(name).render(**{**CONTEXT, **overrides})


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
