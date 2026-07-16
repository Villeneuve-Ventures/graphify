# Installation, compensation, and offline rollback

P1 defines and proves an isolated transaction shape. It does not install the
candidate into the real user home, publish the P5 candidate root, quiesce live
writers, switch services, activate repo routes, or perform the P9 portfolio
cutover.

## Transaction records

`graphify.workspace.installer_transaction` binds a transaction UUID, isolated
`HOME` and `CODEX_HOME`, phase, frozen candidate manifest, before/after hashes
for each switched item, compensation-plan hash, and an invariant that retained
generations remain untouched. Both declared roots must be non-root canonical
absolute paths so containment cannot collapse to the filesystem root.

`graphify.workspace.compensation_plan` freezes restore order, candidate-created
paths to remove, offline artifacts required for restoration, and the same
generation-preservation invariant. Plan arrays are unique, at least one restore
or removal action is required, and at least one offline artifact must be named.
Each restore action has an explicit ordered `path` to `offline_artifact`
mapping; array-position coincidence is not a valid mapping.

`graphify.workspace.offline_rollback` lists the self-contained regular files,
hashes, modes, and restore order inside the rollback bundle. It has no network
dependency.

The normative Python cross-document validator binds all three records: the
transaction IDs and canonical compensation-plan SHA-256 must match; installer
item paths must be unique and contained under declared `HOME`/`CODEX_HOME`;
every pre-existing item appears exactly once in `restore_order`; every newly
created item appears exactly once in `remove_if_created`; action classes cannot
overlap or name unrelated paths; and each required offline artifact must be an
entry in the parsed rollback contract. Each mapped rollback digest must equal
the corresponding installer preimage digest. `remove_if_created` may be empty
when every installer item existed before the transaction.

The disposable executor stages only transaction-declared files, audits the
changed-file set against that transaction, removes and restores strictly in
compensation-plan order, and verifies every rollback member before writing it.
It does not delete the skill tree wholesale or follow the rollback bundle's
independent inventory order. The proof records canonical transaction and plan
preimages plus the executor's ordered action receipt, so their published hashes
are independently reproducible.

## P1 proof boundary

The proof harness uses disposable absolute homes, explicitly sets `HOME`,
`CODEX_HOME`, `XDG_*`, `UV_TOOL_DIR`, and `UV_TOOL_BIN_DIR`, and scrubs ambient
pip/uv package-source settings in favor of the declared PyPI index. It installs
the candidate wheel and refreshes the Codex skill only there. It proves:

1. two clean homes resolve the same sorted dependency manifest;
2. the candidate binary and skill resolve to the candidate tuple inside each
   isolated home;
3. a real installer transaction, compensation plan, and parsed offline rollback
   document pass the normative cross-document validator before staging;
4. every staged or created file is transaction-declared and exactly classified
   as restore or remove by its preimage state;
5. a failure after binary/skill staging triggers plan-ordered, fixture-backed
   compensation;
6. compensation succeeds with network disabled;
7. the prior binary, dependency manifest, complete skill tree, and service return
   byte-for-byte; and
8. an isolated generations sentinel remains unchanged.

The proof is not evidence for live service restart, writer quiescence,
mixed-process fencing, stable-route switching, or portfolio-wide compensation.
Those remain P5/P9 gates.

The ordinary shell route must continue resolving
`/Users/lisrel.claw/.local/bin/graphify` version `0.9.16` throughout P1.
