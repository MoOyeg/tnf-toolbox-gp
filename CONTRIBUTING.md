# Contributing

```bash
make test      # fast: template checks, shim unit tests, playbook syntax
make verify    # the above plus shellcheck, yamlfmt, ansible-lint (needs podman)
make install-pre-commit
```

Read [AGENTS.md](AGENTS.md) for the invariants that are easy to break, and
[docs/architecture.md](docs/architecture.md) for why the pieces sit where they
do.

## Adding a layer

Most new work is a role plus a numbered playbook. If the layer applies to both
the base cluster and the guest clusters, take `op_kubeconfig` as a variable
rather than reading `kubeconfig` directly — that is what lets `nfd` and
`gpu-operator` serve both.

Add static checks to `hack/test-templates.py` for anything that has a shape
worth asserting. A check that runs in a second is worth a great deal on a stack
where the alternative is a ninety-minute feedback loop.

## Reporting a real run

Deploy reports are the most useful contribution this repository can receive,
because nothing in it has been exercised against real hardware yet. What broke,
at which stage, and what the fix was.
