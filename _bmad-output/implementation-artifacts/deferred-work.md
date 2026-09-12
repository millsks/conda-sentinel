- source_spec: `_bmad-output/implementation-artifacts/stories/cpm-identity-s08-resolution-finds-its-mappings.md`
  summary: The bounded second call in `feedstock.py` is retried by the mounted transport, so its soft-limit arithmetic (and `test_feedstock.py:349-363`) understates the ceiling by a full retried call.
  evidence: `RequestsTransport` mounts `Retry(total=retries)` from the collector's own `retries`; `worst_case_call_seconds(timeout, retries)` applies to every fetch through it, not only the base's. S08 fixed its own instance of the same misreading.
- source_spec: `_bmad-output/implementation-artifacts/stories/cpm-identity-s08-resolution-finds-its-mappings.md`
  summary: A collector's second fetch inside `translate` is neither cached, conditional nor charged to the allowance, and now three collectors make one (`feedstock`, `source_release`, `resolve_identity`); the resolver's un-metered host is pypi.org, the busier of its two.
  evidence: `feedstock.py:1726-1728` already records the gap as deferred; the base has one cache/limiter seam keyed on the single `source`. A base-level "bounded second source" would close it for all three.
- source_spec: `_bmad-output/implementation-artifacts/stories/cpm-identity-s08-resolution-finds-its-mappings.md`
  summary: The resolution written by `record_resolution` and the snapshot row inserted by `_write_evidence` commit as two transactions, so a database failure between them leaves identity mutated with no evidence row.
  evidence: `collection_run` deliberately opens no transaction; `translate` cannot insert its own row. Closing it needs a base seam that lets a collector run a callable inside `_write_evidence`'s block.
- source_spec: `_bmad-output/implementation-artifacts/stories/cpm-identity-s08-resolution-finds-its-mappings.md`
  summary: Automated resolution never records `not_applicable` for the release-ecosystem mapping, so a conda-only package (`_libgcc_mutex`) is `not_found` on PyPI rather than "not a Python package", and `python_readiness`'s `not_applicable` branch is unreachable through automation.
  evidence: Deciding "not applicable" needs a package-type source (conda-forge's recipe `noarch`/language, or the inventory), which no collector reads yet; the resolver has only a name.
- source_spec: `_bmad-output/implementation-artifacts/stories/cpm-identity-s08-resolution-finds-its-mappings.md`
  summary: The collector name `resolve_identity` is also a work-type vocabulary value (`policies/outcomes.py`, `surface/tone.py:143`), so the two share one derived label and tone on different screens.
  evidence: Both vocabularies are keyed by bare value and `test_no_label_carries_the_slug_separator` sweeps them into one set, so the collision is invisible to the tests; renaming either is a vocabulary change with its own migration.
- source_spec: `_bmad-output/implementation-artifacts/stories/cpm-platform-s08-the-demo-asserts-nothing-it-never-observed.md`
  summary: A name-keyed resolution reads whatever PyPI project shares a conda package's name, so `nodejs`, `ffmpeg` and `ripgrep` (unrelated PyPI projects exist) can be recorded with an unrelated repository and `pkg:pypi/<name>`.
  evidence: The resolver asks PyPI by the canonical name and has no package-type or cross-ecosystem source to refuse a coincidence; the demo roster's native packages make it visible. Closing it is the cross-ecosystem mapping (`CROSS_ECOSYSTEM`, today always `not_found`) or a package-type source, neither of which a seeder change can supply.
