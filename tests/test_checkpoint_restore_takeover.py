"""Takeover of a later generation retains its checkpoint restore origin."""
import json
from pathlib import Path

from graphify import checkpoint_restore as recovery
from graphify import transaction as tx
from tests.test_checkpoint_restore import fixture, tree  # noqa: F401 -- shared restore fixture
from tests.test_recovery_authority import modeled  # noqa: F401 -- shared modeled custody fixture


def test_later_restore_generation_takeover_keeps_usable_owner(fixture):
    store, root, original, request, _ = fixture
    before = tree(original), tree(store)
    preview = recovery.preview(request)
    recovery.execute(request, preview["preview_sha256"])
    output = Path(request["output"])
    provenance = (output / recovery.PROVENANCE).read_bytes()
    graph = json.loads((output / "graph.json").read_bytes())

    owner = tx.begin_transaction("full", root, output=output, now=0.0)
    tx.stage_transaction_handoff(owner)
    owner = tx.resume_transaction(owner.id, root, output=output)
    assert owner.generation == 2
    assert owner.restore_binding is not None

    successor = tx.takeover_drainer(output, now=100.0)
    token_path = tx.active_transaction_token_path(output)
    token = json.loads(token_path.read_bytes())
    assert token["restore_binding"] == owner.restore_binding
    assert token["id"] != owner.id
    assert successor.claim_epoch == owner.drainer.claim_epoch + 1
    assert not (output / tx.TOKEN_TRANSITION_FILE).exists()
    status = tx.transaction_status(output)
    assert status["generation"] == 2
    assert status["transaction"]["id"] == token["id"]
    assert status["token_transition"] is None
    graph["graph"][tx.GRAPH_WATERMARK_KEY]["generation"] = 2
    payloads = {
        "graph.json": json.dumps(graph).encode(), "manifest.json": b"{}",
    }
    tx.run_token(token_path, ["-c", "\n".join([
        "from graphify import transaction as tx",
        "owner = tx.current_transaction()",
        "assert owner.generation == 2",
        f"tx.commit_publication_plan(owner, tx.PublicationPlan({payloads!r}))",
        "tx.finish_transaction(owner)",
    ])])
    assert tx.open_graph_snapshot(output / "graph.json", purpose="test").generation == 2
    assert tx.transaction_status(output)["protocol_state"] == "COMPLETE"
    assert (output / recovery.PROVENANCE).read_bytes() == provenance
    assert before == (tree(original), tree(store))
