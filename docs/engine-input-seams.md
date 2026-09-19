# Scoped engine input interfaces

These opt-in library interfaces support a future workspace adapter. They do not
implement workspace commands, persisted manifests, certification, publication,
compatibility tuples, or source snapshot isolation.

```python
from graphify.detect import detect
from graphify.extract import extract
from graphify.source_io import SourceIO

with SourceIO(source_root) as inputs:
    inventory = detect(source_root, source_io=inputs, quiet=True)
    result = extract(inventory["files"]["code"], source_io=inputs, quiet=True)
    evidence = inputs.evidence
```

`SourceIO` is a synchronous, bounded, descriptor-based input context. Its
`read_bytes`, `probe`, and `listdir` methods are the reader, pinner and directory
listing interfaces. Paths are resolved beneath explicitly declared directory
roots, without following symlinks. The default source root also bounds ancestor
policy and resolver searches. A caller admitting external Git/policy directories
must supply an explicit `extra_roots={label: absolute_directory}` allowlist;
metadata found in source files never expands that allowlist. An adapter supplying
its own implementations must preserve these authority, bound, evidence, and
failure-latching contracts. The context must be open before engine entry.

`read_bytes(path, max_bytes=...)` can tighten the context's per-file limit for a
bounded extractor. It returns complete bytes or refuses with `SourceTooLarge`;
it never records a truncated prefix as a successful read. Refusal still poisons
the context even when an extractor translates it to its usual size error.
Custom reader implementations must support this optional bound as well.

Evidence contains rooted labels, file hashes and identities, negative probes,
directory bindings, and complete consulted memberships. Atime is excluded.
Repeated input observations must agree. Directory contents, file identities and
bytes may still change between observations: this is observed-current evidence,
not an atomic snapshot or a guarantee against changes reverted between reads.
The future adapter must reobserve the complete evidence and bind it to its own
lifecycle. Evidence alone grants no reopen or publication authority.

Scoped detection disables word-count caching, Office conversion, Google export,
query-memory enrollment, and ambient output/transaction selection. It reports
original Office/media/document paths and consumes their original bytes. Word
counts are zero, not computed counts. Ignored/sensitive paths retain the existing
v8 admission rules. Native operational-tree recognition reuses the existing
transaction marker validators through observation-only capabilities; it never
repairs or creates transaction state. Unrelated output file contents are not read;
consulted output identities and recognition markers remain part of the evidence.

Scoped extraction always uses strict, synchronous dispatch and bypasses persistent
AST/resolver caches. Ordinary calls retain their default cache, parallelism,
fallback and diagnostic behavior. `source_root` controls IDs and resolver roots
independently of `cache_root`; the latter retains its ordinary cache destination.
`quiet=True` suppresses engine diagnostics. `ambient_output=False` disables
persistent extraction caching; on detection it also implies read-only conversion
behavior. Use `source_io` for complete input evidence and scoped authority.

A successful scoped extraction adds `outcomes`: each selected input is `success`
or `empty`. `empty` is an explicit successful extractor result with no nodes.
Failures raise `ExtractionIncomplete` with structured outcomes: missing parser,
unsupported extractor/input, inconsistent input, failed read, partial enumeration,
or failed extraction. Successful parsing may tolerate syntax errors according to
the existing extractor contract. Missing selected extras are not empty success;
working documented fallbacks, such as Pascal's regex extractor, remain available.
A scoped I/O failure poisons its context even if a tolerant extractor catches it.
On interruption, completed per-input results retain their status and inputs not
yet extracted are `not_processed`. The triggering input receives the source error;
`ExtractionIncomplete.failure` also describes the batch failure. A later shared
resolution failure preserves completed extraction outcomes and is reported in
`failure`, without falsely attributing it to every input. These completed outcomes
do not make the incomplete batch graph usable for certification.

## Reader inventory

| Input path family | Engine owner and accounting |
| --- | --- |
| Detection admission | `detect`: shebang prefix, paper-text sniff, original admitted bytes, nested ignore/include files, Git marker/commondir/info-exclude, snapshot-directory globs and full traversal use source helpers. |
| Shared parser | `extractors/engine`: generic tree-sitter source reads; inline extractors in `extract` also use the reader. Header/Objective-C/Groovy sniffing and supplemental JS/Python rationale reads are accounted. |
| Language-specific readers | Apex, Bash, Blade, Dart, DM/DMI/DMM/DMF, Elixir, Fortran, Go, JSON config, Julia, Markdown, Objective-C, Pascal/forms, PowerShell, Razor, Rust, SLN, SQL, Terraform, Verilog and Zig use the reader. Other registered languages use the shared parser. |
| Package and MCP ingest | `manifest_ingest`, `mcp_ingest`: size admission and full bytes, including bounded JSON/MCP prefix paths; no provider invocation. |
| JS/TS/component resolution | `extractors/resolution` and component extractors: tsconfig/extends, pnpm/package workspace declarations and globs, package exports, target candidates/indexes, supporting source files; missing candidates are negative probes. Scoped calls bypass metadata caches. |
| Python, Java, PHP and other resolution | `extractors/resolution`: source rereads, target probes and reference normalization share the same context; Lua/Pascal ancestor searches stop at the source root. |
| XAML/.NET | `extract`: codebehind candidates and fallback sibling listings, project-marker enumeration, C# discovery and reads, generated member inference, project references. Cache/ID roots remain separate. |
| Native operational exclusions | `transaction._OperationalCorpusScan`: selected output binding, owner/prepared/retired/transaction markers, child identities and revalidation use the scoped reader; existing recognition predicates are shared. No transaction publication path is invoked. |

The dispatch-inventory regression iterates every registered suffix, plus special
manifest/MCP/shebang/Blade dispatch, with raw Path input operations and subprocess
invocation forbidden. Minimal payloads do not prove language semantics; separate
JS/workspace/XAML/CJS/SQL/PHP fixtures and the ordinary regression suite provide
that proof. Changes adding built-in I/O must update this inventory and its guards.

Explicit refusals include symlinks/non-regular inputs, out-of-allowlist I/O,
inconsistent or unavailable inputs, resource bounds, partial enumeration, Google
shortcuts in read-only detection, unadapted extractor callbacks, and capital-F
Fortran external `cpp` preprocessing. Lowercase Fortran remains available.
`collect_files` is an ordinary convenience API and refuses inside an active scoped
context; use `detect(source_io=...)` for scoped enumeration. No subset is silently
certified when an input is unsupported.

## Serialization and queries

`export.write_json(graph, communities, stream, built_at_commit=...)` writes the
existing v8 JSON representation to a caller-owned text stream. It performs no
path, Git, temp-file, flush, close, or publication operation. A caller can wrap its
controlled descriptor with a text stream. `to_json` retains its ordinary shrink
protection, Git discovery, staged serialization, and file behavior.

`serve.memory_query_segmenter()` supplies a private optional segmenter to
`_query_graph_text(..., segmenter=...)` and `_query_terms`. With qualified jieba
0.42.1 it initializes from the installed dictionary in memory and preserves its
segmentation/HMM behavior without using ambient caches or modifying the global
tokenizer. Without the extra, it preserves the existing bigram fallback. An
unqualified installed version or initialization failure refuses the memory path.
Ordinary queries still use their default tokenizer.

The caller must launch Python with `-B` or `PYTHONDONTWRITEBYTECODE=1` **before
imports** when startup must not write bytecode. S1 supplies no new command launcher.
Cold subprocess tests install audit interception before candidate imports, deny
filesystem mutations and network/subprocess effects, compare tokens/ranking with
the same pinned dictionary, and exercise absent and misleading ambient caches.
They also cover the missing-extra path. Cross-process text comparison fixes the
hash seed because existing traversal formatting may iterate sets.
