# Migration contract boundary

P1 produces no repository enrollment or migration. It freezes the package that
later repo plans consume.

## Repo-owned input

Each repository will own one small `.graphify/workspace.toml` containing:

- the required `contract = "graphify.workspace.config"` discriminator;
- `schema_version = 1`;
- an immutable canonical `repo_uuid` label; and
- freshness, semantic-capability, network-egress, and backend policy.

The file cannot select the global state root, registry path, generations,
pointers, leases, journals, or services. An inherited UUID in a separate Git
common directory must fail collision until an audited operator-authorized
adoption succeeds.

## Later migration requirements

P4 executes the published `0.9.16` adapter and defines no pre-workspace-state
reader. P6-P8 shadow migrations must build and certify a new `0.9.16`
generation from source rather than import or rewrite an existing `graphify-out`
tree. A new workspace lease cannot fence a legacy writer, so those later phases
must still quiesce legacy writers before stable-route activation.

Market Trend Radar, mac-mini-trading-os, and Aletheia remain untouched by P1.
Their shadow migrations will pin the exact P5 candidate binary, runtime, skill,
contract, and fixture digests outside ordinary `PATH` and user `CODEX_HOME`.
They will leave stable routes unchanged until P9.

No legacy deletion is authorized by a migration. Pruning is a separate
post-cutover decision after the quantitative observation window.
