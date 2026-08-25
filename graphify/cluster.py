"""Community detection with native Leiden and a NetworkX Louvain fallback."""
from __future__ import annotations
import contextlib
import importlib
import inspect
import io
import json
import numbers
from collections.abc import Iterable, Mapping, Sequence
from typing import TypeVar
import networkx as nx


NodeId = str | int | bool | bytes | None
NodeIdT = TypeVar("NodeIdT", bound=NodeId)


def _suppress_output():
    """Context manager to suppress stdout/stderr during library calls.

    Leiden implementations may emit ANSI escape sequences (progress bars,
    colored warnings) that corrupt PowerShell 5.1's scroll buffer on
    Windows (see issue #19). Redirecting stdout/stderr to devnull during
    the call prevents this without losing any graphify output.
    """
    return contextlib.redirect_stdout(io.StringIO())


def _canonical_node_key(node: object) -> str:
    """Return a stable, type-tagged key for a supported scalar node ID."""
    if type(node) is str:
        return f"str:{json.dumps(node, ensure_ascii=False, separators=(',', ':'))}"
    if type(node) is int:
        return f"int:{node}"
    if type(node) is bool:
        return f"bool:{int(node)}"
    if type(node) is bytes:
        return f"bytes:{node.hex()}"
    if node is None:
        return "none:"
    raise TypeError(
        "Leiden node IDs must be str, exact int, exact bool, bytes, or None; "
        f"got {type(node).__module__}.{type(node).__qualname__}"
    )


def _sorted_nodes(nodes: Iterable[NodeIdT]) -> list[NodeIdT]:
    """Return supported node IDs in their stable, type-tagged order."""
    return sorted(nodes, key=_canonical_node_key)


def _native_edges(G: nx.Graph) -> tuple[list[tuple[str, str, float]], dict[str, NodeId]]:
    """Encode a NetworkX graph as insertion-order-independent native edges."""
    keyed_nodes: list[tuple[str, NodeId]] = []
    seen_keys: set[str] = set()
    for node in G.nodes():
        key = _canonical_node_key(node)
        if key in seen_keys:
            raise ValueError(f"Leiden canonical node-key collision: {key}")
        seen_keys.add(key)
        keyed_nodes.append((key, node))

    keyed_nodes.sort(key=lambda item: item[0])
    native_by_node = {node: str(index) for index, (_, node) in enumerate(keyed_nodes)}
    original_by_native = {native_id: node for node, native_id in native_by_node.items()}
    edges: list[tuple[str, str, float]] = []
    for source, target, attrs in G.edges(data=True):
        weight = attrs.get("weight", 1.0)
        if not isinstance(weight, numbers.Real):
            raise TypeError(f"Leiden edge weight must be numeric, got {type(weight).__name__}")
        left, right = sorted((native_by_node[source], native_by_node[target]), key=int)
        edges.append((left, right, float(weight)))
    edges.sort(key=lambda edge: (int(edge[0]), int(edge[1]), edge[2]))
    return edges, original_by_native


def _partition(G: nx.Graph, resolution: float = 1.0) -> dict[NodeId, int]:
    """Run community detection. Returns {node_id: community_id}.

    Tries native Leiden first — best quality.
    Falls back to Louvain only if graspologic-native is not installed.

    resolution > 1.0 → more, smaller communities.
    resolution < 1.0 → fewer, larger communities.

    Output from Leiden is suppressed to prevent ANSI escape codes
    from corrupting terminal scroll buffers on Windows PowerShell 5.1.
    """
    stable = nx.Graph()
    stable.add_nodes_from(_sorted_nodes(G.nodes()))
    edge_rows = sorted(
        G.edges(data=True),
        key=lambda row: (
            _canonical_node_key(row[0]),
            _canonical_node_key(row[1]),
            json.dumps(row[2], sort_keys=True, ensure_ascii=False, default=str),
        ),
    )
    for src, tgt, attrs in edge_rows:
        stable.add_edge(src, tgt, **attrs)

    try:
        native_module = importlib.import_module("graspologic_native")
    except ModuleNotFoundError as error:
        if error.name != "graspologic_native":
            raise
    else:
        leiden = native_module.leiden
        edges, original_by_native = _native_edges(stable)
        with _suppress_output(), contextlib.redirect_stderr(io.StringIO()):
            _, native_partition = leiden(
                edges,
                resolution=resolution,
                randomness=0.001,
                iterations=1,
                use_modularity=True,
                seed=42,
                trials=1,
            )
        if set(native_partition) != set(original_by_native):
            raise RuntimeError("Native Leiden returned an incomplete or unknown node mapping")
        return {original_by_native[node]: int(cid) for node, cid in native_partition.items()}

    # Fallback: networkx louvain (available since networkx 2.7).
    # Inspect kwargs to stay compatible across NetworkX versions — max_level
    # was added in a later release and prevents hangs on large sparse graphs.
    kwargs: dict = {"seed": 42, "threshold": 1e-4, "resolution": resolution}
    if "max_level" in inspect.signature(nx.community.louvain_communities).parameters:
        kwargs["max_level"] = 10
    communities = nx.community.louvain_communities(stable, **kwargs)
    return {node: cid for cid, nodes in enumerate(communities) for node in nodes}


_MAX_COMMUNITY_FRACTION = 0.25   # communities larger than 25% of graph get split
_MIN_SPLIT_SIZE = 10             # only split if community has at least this many nodes
_COHESION_SPLIT_THRESHOLD = 0.05 # re-split communities with cohesion below this
_COHESION_SPLIT_MIN_SIZE = 50    # only cohesion-split if community has at least this many nodes


def label_communities_by_hub(
    G: nx.Graph, communities: dict[int, list[NodeId]]
) -> dict[int, str]:
    """Deterministic, LLM-free community labels: name each community after its
    highest-degree member — the structural hub — so a report reads ``auth`` /
    ``log_action`` instead of ``Community 70``. Degree is measured on the full graph
    ``G``; ties break by node id for run-to-run stability. A community whose members
    are all absent from ``G`` falls back to ``Community {cid}``.

    Used as the default (no-backend) labeler; an LLM naming pass, when configured,
    overrides these with richer names.
    """
    labels: dict[int, str] = {}
    for cid, members in communities.items():
        present = [n for n in members if n in G]
        if not present:
            labels[cid] = f"Community {cid}"
            continue
        # highest degree wins; ties broken by node id (ascending) for determinism
        hub = min(present, key=lambda n: (-G.degree(n), _canonical_node_key(n)))
        name = str(G.nodes[hub].get("label") or hub).strip()
        if name.endswith("()"):
            name = name[:-2]
        labels[cid] = name or f"Community {cid}"
    return labels


def community_member_sigs(communities: dict[int, list[NodeId]]) -> dict[int, str]:
    """Per-community membership fingerprints: ``{cid: sha256(sorted member ids)}``.

    Persisted next to ``.graphify_labels.json`` so a later ``cluster-only`` can tell
    which communities actually changed since labeling. A cid whose members no longer
    hash the same is a different community — reusing its old (LLM) label there is the
    "stale label after re-scoping" bug this guards against. Deterministic; independent
    of cid index, node order, and machine.
    """
    import hashlib

    sigs: dict[int, str] = {}
    for cid, members in communities.items():
        h = hashlib.sha256()
        if all(type(member) is str for member in members):
            encoded_members = sorted(member for member in members if isinstance(member, str))
        else:
            encoded_members = [_canonical_node_key(member) for member in _sorted_nodes(members)]
        for encoded_member in encoded_members:
            h.update(encoded_member.encode("utf-8", "replace"))
            h.update(b"\x00")
        sigs[cid] = h.hexdigest()[:16]
    return sigs


def cluster(
    G: nx.Graph,
    resolution: float = 1.0,
    exclude_hubs_percentile: float | None = None,
) -> dict[int, list[NodeId]]:
    """Run Leiden community detection. Returns {community_id: [node_ids]}.

    Community IDs are stable across runs: 0 = largest community after splitting.
    Oversized communities (> 25% of graph nodes, min 10) are split by running
    a second Leiden pass on the subgraph.

    Accepts directed or undirected graphs. DiGraphs are converted to undirected
    internally since Louvain/Leiden require undirected input.

    resolution: passed to Leiden/Louvain. >1.0 = more smaller communities,
        <1.0 = fewer larger communities. Default 1.0.
    exclude_hubs_percentile: if set (0-100), nodes whose degree exceeds this
        percentile are excluded from partitioning and reattached to their
        majority-vote neighbour community afterwards. Useful for staging/utility
        super-hubs that inflate god-node rankings (#919).
    """
    if G.number_of_nodes() == 0:
        return {}
    if G.is_directed():
        G = G.to_undirected()
    if G.number_of_edges() == 0:
        return {i: [n] for i, n in enumerate(_sorted_nodes(G.nodes()))}

    # Compute hub exclusion set before removing anything so degree is based on full graph
    hub_nodes: set[NodeId] = set()
    if exclude_hubs_percentile is not None:
        degrees = sorted(d for _, d in G.degree())
        if degrees:
            idx = max(0, int(len(degrees) * exclude_hubs_percentile / 100) - 1)
            threshold = degrees[idx]
            hub_nodes = {n for n, d in G.degree() if d > threshold}

    # Leiden warns and drops isolates - handle them separately
    # Also exclude hub nodes from partitioning so they don't pull unrelated
    # subsystems into the same community
    partition_nodes = [n for n in G.nodes() if n not in hub_nodes]
    partition_graph = G.subgraph(partition_nodes)
    isolates = sorted(
        (n for n, degree in partition_graph.degree() if degree == 0),
        key=_canonical_node_key,
    )
    connected_nodes = [n for n, degree in partition_graph.degree() if degree > 0]
    connected = partition_graph.subgraph(connected_nodes)

    raw: dict[int, list[NodeId]] = {}
    if connected.number_of_nodes() > 0:
        partition = _partition(connected, resolution=resolution)
        for node, cid in partition.items():
            raw.setdefault(cid, []).append(node)

    # Each isolate becomes its own single-node community
    next_cid = max(raw.keys(), default=-1) + 1
    for node in isolates:
        raw[next_cid] = [node]
        next_cid += 1

    # Reattach excluded hubs by majority-vote neighbour community
    if hub_nodes:
        node_community: dict[NodeId, int] = {n: cid for cid, nodes in raw.items() for n in nodes}
        for hub in _sorted_nodes(hub_nodes):
            votes: dict[int, int] = {}
            for nb in G.neighbors(hub):
                cid = node_community.get(nb)
                if cid is not None:
                    votes[cid] = votes.get(cid, 0) + 1
            if votes:
                best = min(
                    votes,
                    key=lambda c: (-votes[c], tuple(map(_canonical_node_key, _sorted_nodes(raw[c])))),
                )
                raw.setdefault(best, []).append(hub)
                node_community[hub] = best
            else:
                raw[next_cid] = [hub]
                node_community[hub] = next_cid
                next_cid += 1

    # Split oversized communities
    max_size = max(_MIN_SPLIT_SIZE, int(G.number_of_nodes() * _MAX_COMMUNITY_FRACTION))
    final_communities: list[list[NodeId]] = []
    for nodes in raw.values():
        if len(nodes) > max_size:
            final_communities.extend(_split_community(G, nodes))
        else:
            final_communities.append(nodes)

    # Second pass: re-split low-cohesion communities caused by doc-hub nodes
    # that bridge otherwise-unrelated subsystems (e.g. CLAUDE.md connected to everything).
    second_pass: list[list[NodeId]] = []
    for nodes in final_communities:
        if len(nodes) >= _COHESION_SPLIT_MIN_SIZE and cohesion_score(G, nodes) < _COHESION_SPLIT_THRESHOLD:
            splits = _split_community(G, nodes)
            second_pass.extend(splits if len(splits) > 1 else [nodes])
        else:
            second_pass.append(nodes)
    final_communities = second_pass

    # Re-index by size descending. The tuple(sorted(nodes)) tiebreak makes this a
    # TOTAL order, so an identical grouping always gets identical community IDs.
    # Without it, the hundreds of equal-sized small communities are ordered by the
    # partitioner's (not seed-stable) enumeration order, so their integer IDs
    # permute run-to-run - which reads as massive "community churn" in a per-node
    # cid diff even though the actual grouping is reproducible (#1090 follow-up).
    final_communities.sort(
        key=lambda nodes: (-len(nodes), tuple(map(_canonical_node_key, _sorted_nodes(nodes))))
    )
    return {i: _sorted_nodes(nodes) for i, nodes in enumerate(final_communities)}


def _split_community(G: nx.Graph, nodes: list[NodeId]) -> list[list[NodeId]]:
    """Run a second Leiden pass on a community subgraph to split it further."""
    subgraph = G.subgraph(nodes)
    if subgraph.number_of_edges() == 0:
        # No edges - split into individual nodes
        return [[n] for n in _sorted_nodes(nodes)]
    sub_partition = _partition(subgraph)
    sub_communities: dict[int, list[NodeId]] = {}
    for node, cid in sub_partition.items():
        sub_communities.setdefault(cid, []).append(node)
    if len(sub_communities) <= 1:
        return [_sorted_nodes(nodes)]
    return [_sorted_nodes(v) for v in sub_communities.values()]


def cohesion_score(G: nx.Graph, community_nodes: list[NodeId]) -> float:
    """Ratio of actual intra-community edges to maximum possible."""
    n = len(community_nodes)
    if n <= 1:
        return 1.0
    subgraph = G.subgraph(community_nodes)
    actual = subgraph.number_of_edges()
    possible = n * (n - 1) / 2
    return actual / possible if possible > 0 else 0.0


def score_all(G: nx.Graph, communities: dict[int, list[NodeId]]) -> dict[int, float]:
    return {cid: cohesion_score(G, nodes) for cid, nodes in communities.items()}


def remap_communities_to_previous(
    communities: Mapping[int, Sequence[NodeIdT]],
    previous_node_community: Mapping[NodeIdT, int],
) -> dict[int, list[NodeIdT]]:
    """Remap community IDs to maximize overlap with a previous assignment.

    Uses greedy one-to-one matching by intersection size, then assigns fresh IDs
    to unmatched communities in deterministic order (size desc, lexical tie-break).
    """
    if not communities:
        return {}

    new_sets = {cid: set(nodes) for cid, nodes in communities.items()}
    old_sets: dict[int, set[NodeIdT]] = {}
    for node, old_cid in previous_node_community.items():
        old_sets.setdefault(old_cid, set()).add(node)

    overlaps: list[tuple[int, int, int]] = []
    for old_cid, old_nodes in old_sets.items():
        for new_cid, new_nodes in new_sets.items():
            overlap = len(old_nodes & new_nodes)
            if overlap > 0:
                overlaps.append((overlap, old_cid, new_cid))
    overlaps.sort(key=lambda x: (-x[0], x[1], x[2]))

    new_to_final: dict[int, int] = {}
    used_old_ids: set[int] = set()
    matched_new_ids: set[int] = set()
    for _overlap, old_cid, new_cid in overlaps:
        if old_cid in used_old_ids or new_cid in matched_new_ids:
            continue
        new_to_final[new_cid] = old_cid
        used_old_ids.add(old_cid)
        matched_new_ids.add(new_cid)

    unmatched = [cid for cid in communities if cid not in matched_new_ids]
    unmatched.sort(
        key=lambda cid: (
            -len(communities[cid]),
            tuple(map(_canonical_node_key, _sorted_nodes(communities[cid]))),
        )
    )
    next_id = 0
    for new_cid in unmatched:
        while next_id in used_old_ids:
            next_id += 1
        new_to_final[new_cid] = next_id
        used_old_ids.add(next_id)
        next_id += 1

    remapped: dict[int, list[NodeIdT]] = {}
    for new_cid, nodes in communities.items():
        remapped[new_to_final[new_cid]] = _sorted_nodes(nodes)
    return dict(sorted(remapped.items(), key=lambda kv: kv[0]))
