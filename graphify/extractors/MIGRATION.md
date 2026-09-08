# Migrating a language extractor out of extract.py

`graphify/extract.py` is being split into this package, one language per PR
(upstream issue #1212). This is the playbook for porting ONE language. It is
written so an AI agent can execute it in a single session.

## Status

| module | migrated |
|---|---|
| blade | yes |
| zig | yes |
| elixir | yes |
| razor | yes |
| dart | yes |
| rust | yes |
| go | yes |
| powershell (ps1 + psd1 manifest) | yes |
| fortran | yes |
| sql | yes |
| dm (dm/dmm/dmi/dmf) | yes |
| bash | yes |
| apex | yes |
| terraform | yes |
| sln | yes |
| pascal_forms (dfm + lfm) | yes |
| json_config | yes |
| julia | yes |
| verilog | yes |
| markdown | yes |
| objc | yes |
| pascal | yes |
| shared engine / models / resolution | yes — `engine.py`, `models.py`, `resolution.py` |
| language wrappers/configuration: python, js, java, c, cpp, ruby, csharp, kotlin, scala, php, lua, swift, groovy; embedded-language extractors: vue, svelte, astro, xaml | remain in `extract.py` |
| other bespoke: csproj, slnx, lazarus_package | no |

The shared `_extract_generic` engine, `LanguageConfig` models, and shared
resolution helpers have moved into this package and are re-imported by the
`extract.py` facade. Language wrappers and configuration still remain in the
facade. This playbook continues to cover one bespoke extractor at a time;
wrapper/configuration migration requires a separately coordinated batch.

## Invariants (non-negotiable)

1. **Verbatim moves only.** No renames, no docstring edits, no reformatting,
   no added annotations, no "improvements". Verify: save the block before
   cutting and confirm the pasted block is byte-identical.
2. **One language per PR.** Small diffs keep review trivial and avoid
   conflicts with other in-flight ports.
3. **Facade re-export is mandatory.** `extract.py` must keep exporting every
   moved name (`from graphify.extractors.<mod> import extract_<lang>  # noqa: F401`
   in the marked migration block, kept alphabetical). Existing importers
   (`__main__.py`, `watch.py`, `pg_introspect.py`, tests) must not change.
4. **Never import from `graphify.extract` inside this package.** Import
   direction is strictly extract.py -> extractors/. If you need a helper that
   lives in extract.py, classify it (below) and move it.
5. **Zero test edits** outside `tests/test_extractors_registry.py`. The
   untouched language tests passing IS the proof of behavior preservation.

## Helper classification

For every `_name` your function references that is defined OUTSIDE it:

- run `grep -c '_name' graphify/extract.py` AFTER your candidate move;
- remaining uses > 0 -> **shared**: move it to `base.py` and add it to the
  facade re-import in extract.py;
- remaining uses = 0 -> **private**: move it into your language module.

Closures, constants, and `import` statements defined INSIDE your function
move with it for free — leave them exactly where they are. Only add a
module-header import for names the pasted code references at module scope
that are not satisfied internally, and verify each header import is used.

## Pre-flight

1. Check upstream for conflicts: open PRs/issues mentioning your language,
   and churn: `git log --oneline --since="3 months ago" upstream/<default> | grep -i <lang>`.
   High churn -> pick another language.
2. Confirm your extractor is bespoke (its `extract_<lang>` is a full function,
   not a 5-line `_extract_generic(path, LanguageConfig(...))` wrapper).
3. Check whether tests/ exercises your language's behavior (grep for
   `test_<lang>`). If it has no behavioral tests, the byte-identity check in
   step 3 below is the ENTIRE proof of preservation — include the
   `git diff --color-moved` evidence in your PR description.

## Steps

1. Append a failing test to `tests/test_extractors_registry.py`:
   module import + facade identity + registry identity (copy an existing
   `test_<lang>_migrated` as the template).
2. `grep -n 'def extract_<lang>' graphify/extract.py`; the span ends at the
   line before the next top-level statement (`^def ` or `^_CONST`). Beware
   neighbors: top-level constants AFTER your function may belong to the NEXT
   function (e.g. `_CONFIG_JSON_*` sit after where extract_razor used to be
   but were never razor's).
3. Save the span to a temp file. Create `graphify/extractors/<lang>.py` with
   module docstring (`"""<Lang> extractor. Moved verbatim from graphify/extract.py."""`),
   `from __future__ import annotations`, minimal stdlib imports, base imports,
   then paste the function. Verify byte-identity against the temp file.
4. Delete the span from extract.py, leaving exactly two blank lines between
   the now-adjacent top-level definitions; add the facade re-import; add the
   registry entry in `__init__.py` (alphabetical); update the Status table
   above.
5. `uv run --frozen pytest tests/ -q --tb=short` -> 0 failures, no test file changed except the registry
   test. If ImportError/NameError: a helper was misclassified — go to
   Helper classification.
6. One commit: `refactor(extract): move extract_<lang> to extractors/<lang>.py (verbatim)`.

## What NOT to do

- Do not rewire dispatch, add classes, or add lazy imports — mechanism layers
  come later, by separate agreement (see #1212 discussion).
- Do not port two languages in one PR "while you're at it".
- Do not touch `__main__.py`.
