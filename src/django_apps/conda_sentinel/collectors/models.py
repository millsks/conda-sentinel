"""What this application's collectors observed, as evidence, and how it is read.

`CPM-AD-25`: "the internal inventory is observed by a collector like any other
source ... and writes `inventory_snapshots` -- append-only rows carrying the
source's package key, the internal usage signals as observed, `observed_at`, and
the run's correlation identifiers." This module is that table, the one read
against it, and -- since `CPM-CURRENCY-S01` through `CPM-CURRENCY-S04`,
`CPM-SECURITY-S01` through `CPM-SECURITY-S03`, `CPM-PY314-S01`, `CPM-PY314-S02`
and `CPM-IDENTITY-S08` -- the ten tables beside it: upstream releases, PyPI
releases, conda-forge feedstocks, published conda packages, advisory matches, KEV
cross-references, licence findings, static Python-readiness assessments,
verified Python build results and package-identity resolutions.

**One module, eleven tables, and no shared columns beyond the ones every evidence
row carries.** `CPM-AD-7` gives each collector its own evidence table, which is a
rule about tables rather than about files: `inventory_snapshots`,
`source_release_snapshots`, `pypi_release_snapshots`, `feedstock_snapshots`,
`conda_package_snapshots`, `vulnerability_findings`, `kev_findings`,
`license_findings`, `python_readiness_assessments`,
`python_verification_results` and `identity_resolution_snapshots` are written by
eleven collectors that share nothing but the log.

**The last is evidence of a resolution rather than of a surface.** `CPM-FR-1`'s
resolver (`CPM-IDENTITY-S08`) reads two public documents and hands what it found
to `identity`'s `record_resolution`; what the package row then says is identity,
and what each document said and what was chosen is this table's append-only
row. The mapping outcomes live on `identity.PackageMapping`, because they are
what is *currently concluded*; this row is what one run *observed*, and it is
what gives the review queue's outcomes their evidence.

**The last two are one requirement split across two tables, deliberately.**
`CPM-FR-14` reads a package's *declared* metadata and, separately and on demand,
*builds* it. `CPM-AD-7` would give them separate tables in any case; what makes
the separation load-bearing rather than merely conformant is that the epic's
whole subject is the two staying distinguishable. Two tables, two vocabularies,
two prefixes on the determinate values -- so a reader who joins them can still
say which answer came from a claim and which from an execution, and one who reads
only one of them cannot be misled about which they are holding. They live together because Django auto-imports
`<app>.models` and no other module, so a model declared elsewhere in this
application is registered only by whatever happens to import it -- which is a
table that exists on a developer's machine and not in a migration.

**One collector reads a table it does not write, and the exception is licensed by
an object rather than by this paragraph.** `CPM-AD-7` also says a collector "never
reads another collector's evidence table". This module used to assert that none
did; `collectors/kev.py` now does, because `CPM-FR-12` is *defined* as a
cross-reference of what `CPM-FR-11` recorded and a KEV row that carried no link
would fail its acceptance criterion outright. The read is one table, read-only,
reached through this module rather than by importing the sibling collector, and
never written. `CPM-SECURITY-S02`'s Spec Change Log argues the alternatives and
hands the judgement to review -- and because a prose clause is not something a
second reader trips over, `tests/unit/django_apps/test_collector_base_audit.py`
carries `MODULES_PERMITTED_TO_READ_ANOTHER_COLLECTORS_EVIDENCE`, sweeps every
registered collector in both directions, and fails when a second one takes the
same read or when the licensed one stops needing it.

**The first evidence model in this repository.** `core/models.py` has carried
`AppendOnlyModel` since `CPM-EVIDENCE-S02` with nothing inheriting it, and three
audits have swept an empty set ever since. They now have something to sweep, and
none of them needed an edit to start mattering: `tests/model_registry.py`
classifies this as evidence by all three of its marks, so the inheritance,
constraint and outcome-field sweeps reach it by construction.

**Presence is `state`, not a boolean.** `CPM-AD-5` bans boolean status fields
outright, and the base's `sentinel_evidence` hook already requires an
`OutcomeState` value carried verbatim in a concrete field. So absence is
expressible through machinery `core` owns: a package the source listed is `ok`,
and one it stopped listing is `not_found`. A `present = BooleanField()` beside it
would be a second vocabulary for one fact, and it would put this model outside
the sentinel path the base checks.

**The counts are required of a present observation and of nothing else.** PRD
Open Question 3b makes `internal_component_count` and `internal_lob_count`
required on every inventory *record* -- together they are the internal usage
breadth `CPM-FR-4` ranks by -- while `apps`, `platforms`, `downloads` and
`versions` are nullable score inputs. But an *absence* row observes no counts at
all, and a `not_found` sentinel observes nothing whatever, so a NOT NULL column
would make the two unwritable. The requirement is therefore a
`CheckConstraint` reading "both counts are present exactly when `state` is `ok`",
which is where the rule is actually true. A `CheckConstraint` is permitted on an
evidence model; a `UniqueConstraint` is not, and
`tests/unit/django_apps/test_evidence_constraint_audit.py` is what keeps it that
way -- a constraint spanning the observed fact would turn a re-observation into
an `IntegrityError` (`CPM-AD-2`, `CPM-AD-7`).

**NULL means missing and `0` means zero.** PRD Appendix A.1's data rules say
blank means missing and values are never invented, and Open Question 3b says the
four optional signals stay distinguishable from zero. That is the whole reason
they are nullable integers rather than integers defaulting to `0`: a package with
no recorded download count and a package nobody downloaded are different facts,
and a score built on them must be able to tell.

**`PROTECT`, and it is required rather than preferred** (`EVIDENCE.02-AUDIT-001`).
Django's deletion collector issues its `DELETE` through `sql.DeleteQuery`, never
through `QuerySet.delete()` or `Model.delete()`, so a `CASCADE` from here would
destroy observations when a package went and every append-only refusal in
`core/models.py` would be bypassed on the way. `PROTECT` makes the database
refuse instead -- which is also `CPM-AD-25`'s "no package row is ever deleted",
enforced rather than intended.

**Reading is cut-off bound, and `snapshot_as_of` is the only supported way
to read `inventory_snapshots`.**
`CPM-AD-25`: "a policy reading a usage signal reads the latest snapshot at or
before its run's cut-off, never the current value", which is what makes
`CPM-FR-22`'s replay reproduce identical results. A caller that reached for
`.latest()` or `.first()` would read whatever the most recent sweep happened to
write, and a replay of the same policy version at the same stated cut-off would
then conclude something different every time the inventory changed.

**On the `AD-` prefix.** A bare `AD-n` in this repository is an *inherited*
platform decision; a decision from this product's own architecture spine always
carries the `CPM-` prefix.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import ClassVar
from typing import Final

from django.db import models
from django.utils.translation import gettext_lazy as _

from conda_sentinel.collectors.match_confidence import MatchConfidence
from conda_sentinel.collectors.outcomes import INFERRED_COMPATIBLE
from conda_sentinel.collectors.outcomes import INFERRED_INCOMPATIBLE
from conda_sentinel.collectors.outcomes import KEV_NOT_APPLICABLE
from conda_sentinel.collectors.outcomes import KEV_UNKNOWN
from conda_sentinel.collectors.outcomes import LICENSE_NOT_APPLICABLE
from conda_sentinel.collectors.outcomes import LISTED
from conda_sentinel.collectors.outcomes import MATCHED
from conda_sentinel.collectors.outcomes import NORMALIZED
from conda_sentinel.collectors.outcomes import NOT_LISTED
from conda_sentinel.collectors.outcomes import READINESS_NOT_APPLICABLE
from conda_sentinel.collectors.outcomes import VERIFICATION_FAILED
from conda_sentinel.collectors.outcomes import VERIFICATION_NOT_APPLICABLE
from conda_sentinel.collectors.outcomes import VERIFIED_COMPATIBLE
from conda_sentinel.collectors.outcomes import VULNERABILITY_NOT_APPLICABLE
from conda_sentinel.collectors.outcomes import KevOutcome
from conda_sentinel.collectors.outcomes import LicenseOutcome
from conda_sentinel.collectors.outcomes import PythonReadinessOutcome
from conda_sentinel.collectors.outcomes import PythonVerificationOutcome
from conda_sentinel.collectors.outcomes import VulnerabilityOutcome
from conda_sentinel.collectors.spdx import DetectionMethod
from conda_sentinel.collectors.specifiers import DecidingSignal
from conda_sentinel.core.clock import is_aware
from conda_sentinel.core.finding_keys import FindingKeyed
from conda_sentinel.core.models import AppendOnlyError
from conda_sentinel.core.models import AppendOnlyModel
from conda_sentinel.core.outcomes import OutcomeState
from conda_sentinel.identity.models import IdentityConfidence
from conda_sentinel.identity.models import Package

if TYPE_CHECKING:
    from datetime import datetime

__all__ = [
    "CHANNEL_AND_PLATFORM_CONSTRAINT",
    "CONDA_PACKAGE_FACTS_CONSTRAINT",
    "CONDA_PACKAGE_PAIR_INDEX",
    "CONDA_PACKAGE_READ_INDEX",
    "COUNTS_PRESENT_CONSTRAINT",
    "ESTABLISHED_ABSENCE_CONSTRAINT",
    "FEEDSTOCK_FACTS_CONSTRAINT",
    "FEEDSTOCK_READ_INDEX",
    "IDENTITY_RESOLUTION_FACTS_CONSTRAINT",
    "IDENTITY_RESOLUTION_READ_INDEX",
    "KEV_APPLICABILITY_CONSTRAINT",
    "KEV_FACTS_CONSTRAINT",
    "KEV_READ_INDEX",
    "LICENSE_APPLICABILITY_CONSTRAINT",
    "LICENSE_CHANNEL_CONSTRAINT",
    "LICENSE_FACTS_CONSTRAINT",
    "LICENSE_READ_INDEX",
    "PYPI_FACTS_CONSTRAINT",
    "PYPI_READ_INDEX",
    "READINESS_READ_INDEX",
    "READINESS_REASON_CONSTRAINT",
    "READINESS_SERIES_CONSTRAINT",
    "READINESS_SIGNAL_CONSTRAINT",
    "RELEASE_FACTS_CONSTRAINT",
    "RELEASE_READ_INDEX",
    "SNAPSHOT_KEY_INDEX",
    "SNAPSHOT_READ_INDEX",
    "STAGED_RECIPE_CONSTRAINT",
    "VERIFICATION_EVIDENCE_CONSTRAINT",
    "VERIFICATION_READ_INDEX",
    "VERIFICATION_REASON_CONSTRAINT",
    "VERIFICATION_SERIES_CONSTRAINT",
    "VULNERABILITY_APPLICABILITY_CONSTRAINT",
    "VULNERABILITY_FACTS_CONSTRAINT",
    "VULNERABILITY_READ_INDEX",
    "CondaPackageSnapshot",
    "FeedstockSnapshot",
    "IdentityResolutionSnapshot",
    "InventoryReadError",
    "InventorySnapshot",
    "KevFinding",
    "LicenseFinding",
    "PyPIReleaseSnapshot",
    "PythonReadinessAssessment",
    "PythonVerificationResult",
    "SourceReleaseSnapshot",
    "VulnerabilityFinding",
    "snapshot_as_of",
]

#: How wide the source's own package key column is. Sized as an identifier
#: rather than as a name, on the same terms `identity/models.py` sizes
#: `associator_key`: it is a key some other system chose, not a word a person
#: picked, and neither its shape nor its bound is this product's to guess at.
_KEY_LENGTH: Final[int] = 512

#: How wide the `state` column is. `OutcomeState`'s longest value is
#: `not_applicable`, fourteen characters, and the rest is headroom. Not sized
#: against `core/models.py`'s `_STATUS_LENGTH`, which is 16 and holds `RunState`
#: -- two vocabularies, two widths, each argued from its own longest value.
_STATE_LENGTH: Final[int] = 32

#: How wide the correlation identifier is. `CPM-AD-15` takes it from the active
#: span formatted `032x`, so it is exactly 32 hexadecimal digits, which is the
#: same number `core/models.py` declares for `CollectionRun.trace_id`. Restated
#: here rather than imported: that constant is private to its module, and one
#: spelling shared by import would still be two column declarations. What keeps
#: them in step is that both are 32 because the format says 32, and
#: `tests/integration/django_apps/test_run_ledger.py` pins the format itself.
_TRACE_ID_LENGTH: Final[int] = 32

#: The name of the constraint that makes the counts required where they are
#: required. Named here because the model declares it and the case that asserts
#: the database refuses a violation names it too, and a string spelled twice is
#: a constraint that can be renamed on one side only.
COUNTS_PRESENT_CONSTRAINT: Final[str] = "inventory_counts_present_exactly_when_observed"

#: The two indexes this table's two access paths need, by name.
#:
#: Indexes are permitted on an evidence model and unique *constraints* are not,
#: and the difference is exactly the one `CPM-AD-2` draws: an index makes a read
#: cheaper and changes no write, while a unique constraint turns a re-observation
#: into an `IntegrityError`. `tests/unit/django_apps/test_evidence_constraint_audit.py`
#: reads `Meta.constraints` and the field flags, never `Meta.indexes`.
#:
#: `SNAPSHOT_READ_INDEX` serves `snapshot_as_of`, which is every policy pass's
#: read: filter by package, order by `observed_at` descending, take one. Without
#: it that is a scan of one package's whole observation history on every read, and
#: the history grows daily and is never pruned. `SNAPSHOT_KEY_INDEX` serves the
#: ingestion collector's absence derivation, which excludes on
#: `source_package_key` across the whole table once per sweep.
#:
#: Django caps an index name at 30 characters, which is why neither spells out
#: `inventory_snapshot`.
SNAPSHOT_READ_INDEX: Final[str] = "inv_snapshot_pkg_observed"
SNAPSHOT_KEY_INDEX: Final[str] = "inv_snapshot_source_key"

#: How wide the upstream version column is. A release tag is a string a project
#: chose rather than a name a person picked -- `v1.2.3`, `2024.11.0rc1`,
#: `release/2025-06-11`, and occasionally a whole sentence -- so it is sized like
#: an identifier rather than like the 128 `identity/models.py` gives a name. It is
#: its own constant rather than `_KEY_LENGTH` reused: the two answer to different
#: sources, an inventory's key format and an upstream project's tagging habits,
#: and one of them changing is not a reason to move the other.
_VERSION_LENGTH: Final[int] = 512

#: How wide the locator column is. A locator is a URL this collector built from
#: an owner and a repository, so it is bounded by the same reasoning `_KEY_LENGTH`
#: carries -- a machine-generated string, not a name a person picked -- and by the
#: same number, because both are sized as identifiers rather than ordered against
#: each other.
_LOCATOR_LENGTH: Final[int] = 512

#: The name of the constraint that makes the release facts present exactly where
#: they are true, and the read index the freshness query needs. Named here for the
#: reason the two above are: the model declares them and the cases that assert the
#: database refuses a violation name them too, and a string spelled twice is a
#: constraint that can be renamed on one side only.
#:
#: Django caps an index name at 30 characters, which is why the index does not
#: spell out `source_release_snapshot`.
RELEASE_FACTS_CONSTRAINT: Final[str] = "release_facts_present_exactly_when_observed"
RELEASE_READ_INDEX: Final[str] = "src_release_pkg_observed"

#: How wide the `Requires-Python` column is. A version specifier is a short
#: expression a project wrote -- `>=3.9`, `>=3.8, <4`, `!=3.0.*, >=2.7` -- so it
#: is sized like a name rather than like an identifier: 128 is a whole order of
#: magnitude above anything the specifier grammar produces in practice, and a
#: specifier wider than it is refused where it enters rather than truncated into
#: a row nothing may correct.
_SPECIFIER_LENGTH: Final[int] = 128

#: The name of the constraint that makes the PyPI facts present exactly where
#: they are true, and the read index the freshness query needs, on the terms the
#: two release names above are declared. The index does not spell out
#: `pypi_release_snapshot` for the same 30-character reason.
PYPI_FACTS_CONSTRAINT: Final[str] = "pypi_facts_present_exactly_when_observed"
PYPI_READ_INDEX: Final[str] = "pypi_release_pkg_observed"

#: How wide the feedstock-name column is.
#:
#: A feedstock's name is a name a recipe author chose rather than a
#: machine-generated identifier, so it is sized like `identity/models.py` sizes
#: `Feedstock.name` -- **and then wider**, which is the part that matters. This
#: column stores the *repository*, which is the stored name plus conda-forge's
#: `-feedstock` suffix, so a column merely equal to `identity`'s would leave a
#: band of names that `feedstocks` accepts and this table can never record: a
#: 119-character mapping is legally storable there and permanently uncollectable
#: here, and the refusal would fire on every run for ever. 160 is `identity`'s
#: 128 plus the ten-character suffix with headroom, so the suffixing can never be
#: what makes an observation unwritable.
#:
#: Restated rather than imported -- that constant is private to its module, and
#: one spelling shared by import would still be two column declarations -- and
#: `tests/unit/django_apps/test_feedstock.py` asserts the *relation* the code
#: actually needs rather than the number, so a change to either width that broke
#: it fails there.
_FEEDSTOCK_NAME_LENGTH: Final[int] = 160

#: The names of the feedstock table's two constraints and its one read index, on
#: the terms every name above is declared: the model declares them and the cases
#: that assert the database refuses a violation name them too.
#:
#: There are *two* constraints here where the other evidence tables have one, and
#: the second is `CPM-FR-9`'s AC 2 made into a database rule rather than a
#: convention: a staged recipe is what a package with **no** feedstock has, so a
#: row that found a feedstock may not also carry one.
FEEDSTOCK_FACTS_CONSTRAINT: Final[str] = "feedstock_facts_present_exactly_when_observed"
STAGED_RECIPE_CONSTRAINT: Final[str] = "staged_recipe_only_when_absent"
ESTABLISHED_ABSENCE_CONSTRAINT: Final[str] = "absence_established_only_on_an_absence"
FEEDSTOCK_READ_INDEX: Final[str] = "feedstock_pkg_observed"

#: How wide the channel column is. A channel is one path segment a channel host
#: serves a package under -- `conda-forge`, `bioconda`, an internal mirror's name
#: -- so it is a name an operator declared rather than a machine-generated
#: identifier, and is sized like `identity/models.py` sizes a name. Restated
#: rather than imported, on the terms `_FEEDSTOCK_NAME_LENGTH` states.
_CHANNEL_LENGTH: Final[int] = 128

#: How wide the platform column is. A conda subdir is a short, closed-vocabulary
#: word -- `linux-64`, `osx-arm64`, `win-64`, `noarch` -- so 64 is two orders of
#: magnitude above anything conda produces and is a bound rather than an
#: expectation. Its own constant rather than `_CHANNEL_LENGTH` reused: a channel
#: is a name somebody chose and a subdir is a value conda defines, and one of
#: them changing is not a reason to move the other.
_PLATFORM_LENGTH: Final[int] = 64

#: How wide the build-string column is. A build string is machine-generated from
#: a recipe's variant -- `py312h5f2b1e0_0`, and longer where a variant hash and a
#: dependency pin are folded into it -- so it is sized like an identifier rather
#: than like a name, and 256 is an order of magnitude above what conda-build
#: produces.
_BUILD_STRING_LENGTH: Final[int] = 256

#: The names of the conda-package table's two constraints and its one read index,
#: on the terms every name above is declared.
#:
#: The second constraint is this table's own, and it is `CPM-FR-10`'s "channels
#: are never merged" made into a database rule: **every** row names the channel
#: and the platform it is about, sentinel rows included. A row that could not say
#: which `(channel, platform)` pair it observed would not be an observation at
#: all -- it would be a fact about "somewhere", in a table whose entire purpose is
#: keeping the surfaces apart.
CONDA_PACKAGE_FACTS_CONSTRAINT: Final[str] = "conda_package_facts_present_exactly_when_observed"
CHANNEL_AND_PLATFORM_CONSTRAINT: Final[str] = "conda_package_names_channel_and_platform"
CONDA_PACKAGE_READ_INDEX: Final[str] = "conda_pkg_pkg_observed"

#: The index the read this table exists to serve actually needs, and the one
#: index no sibling table has an analogue of.
#:
#: Every stated purpose of `conda_package_snapshots` is a question about one
#: `(package, channel, platform)` -- what is published on this channel for this
#: platform, newest first. `CONDA_PACKAGE_READ_INDEX` serves the package-wide
#: read every evidence table has; without this one the per-pair read scans every
#: row a package has accumulated across **all** its pairs, and this table grows
#: `channels x platforms` times faster than any sibling, so the scan is the one
#: that degrades first and worst.
CONDA_PACKAGE_PAIR_INDEX: Final[str] = "conda_pkg_pair_observed"

#: How wide the advisory identifier column is.
#:
#: An advisory identifier is a key some catalogue issued -- `CVE-2024-23334`,
#: `GHSA-5h86-8mv2-jq9f`, `PYSEC-2022-42969`, `RUSTSEC-2021-0079` -- so it is
#: sized like an identifier rather than like a name, and 128 is an order of
#: magnitude above every scheme in use. Which catalogues those are is PRD Open
#: Question 1 and is not decided here, which is exactly why the bound is generous
#: rather than fitted to one scheme's shape.
_ADVISORY_ID_LENGTH: Final[int] = 128

#: How wide the severity column is.
#:
#: **Sized against the widest thing a source calls a severity, measured rather
#: than guessed.** `CPM-FR-11` records "severity as the source stated it" and this
#: product ranks nothing (`CPM-AD-8`), so what lands here is whatever the source
#: said: a word (`high`, `MODERATE`), a number (`7.5`), or a whole CVSS vector
#: string.
#:
#: The measured worst case is a **CVSS v4.0 vector with every optional metric
#: present**, which the specification's own metric list makes 176 characters --
#: the four Base groups, the Threat metric, the four Environmental
#: Confidentiality/Integrity/Availability requirements, the eleven Modified Base
#: metrics and the six Supplemental metrics, each an abbreviation, a colon, a
#: value and a slash. 256 is that with room for a longer scheme identifier or a
#: vector a later revision extends.
#:
#: **The number moved because 128 was a permanent failure rather than a tight
#: fit.** A source stating a full v4.0 vector was refused, and this collector
#: refuses the *document* rather than the field -- so one such advisory discarded
#: every other finding for that package, on every run, for ever. That is the
#: blast radius the story's `deferred:` entry records, and it is why a width here
#: is measured against the worst thing a real source states rather than against
#: the common one.
_SEVERITY_LENGTH: Final[int] = 256

#: How wide each of the two range columns is.
#:
#: A range is an expression the advisory source wrote in its ecosystem's own
#: grammar, and the two shapes it takes are orders of magnitude apart. A
#: *constraint* is short -- `>=1.0.5,<3.9.2`, `< 2.4.0 || >= 3.0.0, < 3.1.2` --
#: while an **enumerated list of affected versions** is one entry per release, and
#: advisories against long-lived packages routinely enumerate scores of them:
#: a hundred entries at eight characters and a separator is nine hundred
#: characters before any prefix.
#:
#: 1024 is measured against that enumeration rather than against the constraint
#: form, for the reason `_SEVERITY_LENGTH` is measured against a full CVSS vector:
#: a width that fits the common shape and refuses the uncommon one does not
#: truncate a value, it discards every finding in the document that carried it,
#: on every run.
#:
#: Its own constant rather than `_VERSION_LENGTH` reused: a version is one
#: project's tag and a range is another project's expression *over* those tags,
#: and one of them changing is not a reason to move the other.
_RANGE_LENGTH: Final[int] = 1024

#: How wide the match-confidence column is. `MatchConfidence`'s longest value is
#: `exact-version`, thirteen characters, and the rest is headroom -- the same
#: shape `_STATE_LENGTH` has and for the same reason.
_MATCH_CONFIDENCE_LENGTH: Final[int] = 32

#: How wide *this* table's locator column is, and it is wider than every
#: sibling's on purpose.
#:
#: The locator a vulnerability run names is a fixed scheme prefix followed by the
#: package's whole `primary_purl` -- the identity the adapter is asked about --
#: and `identity/models.py` sizes that column at 512. A locator column merely
#: equal to the siblings' 512 would therefore leave a band of perfectly storable
#: purls whose locator can never be recorded, and the refusal would fire on every
#: run of those packages for ever. That is the trap `_FEEDSTOCK_NAME_LENGTH`
#: records, reached by the same route: a column sized against the value rather
#: than against the value it is *composed from*. 768 is 512 plus the prefix with
#: room to spare, and `tests/unit/django_apps/test_vulnerability.py` asserts the
#: relation rather than the number.
_ADVISORY_LOCATOR_LENGTH: Final[int] = 768

#: The name of the constraint that makes the advisory facts present exactly where
#: they are true, and the read index the freshness query needs, on the terms every
#: name above is declared: the model declares them and the cases that assert the
#: database refuses a violation name them too.
#:
#: There are **two** constraints and no unique constraint of any kind
#: (`CPM-AD-2`): two observations of one advisory are two rows, and a
#: re-observation must insert. The second constraint is this table's own --
#: `not_applicable` is a value the vocabulary carries by construction and this
#: table may never hold, because an advisory question applies to every package --
#: and it is here rather than only in the collector's `sentinel_evidence` because
#: a rule enforced in one writer is a convention, while a rule the table enforces
#: holds against every writer this product ever grows. The index does not spell
#: out `vulnerability_finding` for the same 30-character reason the siblings' do
#: not spell out their tables.
VULNERABILITY_FACTS_CONSTRAINT: Final[str] = "vulnerability_facts_present_exactly_when_matched"
VULNERABILITY_APPLICABILITY_CONSTRAINT: Final[str] = "vulnerability_applies_to_every_package"
VULNERABILITY_READ_INDEX: Final[str] = "vuln_finding_pkg_observed"

#: The names of the two constraints `kev_findings` carries, and the read index its
#: freshness query needs, declared on the terms every name above is: the model
#: declares them and the cases that assert the database refuses a violation name
#: them too.
#:
#: Two constraints and **no unique constraint of any kind** (`CPM-AD-2`): two
#: cross-references of one advisory are two rows, and a re-observation must
#: insert. The first constraint is the biconditional that makes the catalog facts
#: present exactly where they are true -- the link to the vulnerability finding on
#: every row derived from one, the catalog date only where the catalog listed the
#: advisory. The second is this table's own, and is the sibling's rule reached for
#: the same reason: `not_applicable` is a value the vocabulary carries by
#: construction and this table may never hold.
#:
#: Django caps an index name at 30 characters, which is why it does not spell out
#: `kev_finding` twice.
KEV_FACTS_CONSTRAINT: Final[str] = "kev_facts_present_exactly_when_cross_referenced"
KEV_APPLICABILITY_CONSTRAINT: Final[str] = "kev_applies_to_every_package"
KEV_READ_INDEX: Final[str] = "kev_finding_pkg_observed"

#: How wide the raw-licence column is.
#:
#: A licence string is whatever a channel's artifact metadata states, which is a
#: value a recipe author typed rather than an identifier a registry issued: `MIT`,
#: `Apache License, Version 2.0`, `LGPL-2.1-or-later AND MIT`, and occasionally a
#: whole sentence pointing at a file. So it is sized as an identifier rather than
#: as a name -- 512, the same order of magnitude the sibling locators take -- and a
#: value wider than it is **refused where it enters** rather than truncated, which
#: `CPM-SECURITY-S03`'s matrix states in as many words: a truncated licence is a
#: different licence, and one written permanently into a row nothing may correct.
_RAW_LICENSE_LENGTH: Final[int] = 512

#: How wide the normalized-expression column is, and it is deliberately four times
#: the raw column rather than equal to it.
#:
#: **Normalization expands.** `bsd-3` is five characters and `BSD-3-Clause` is
#: twelve; an expression of such operands grows by the same factor throughout. A
#: normalized column merely equal to the raw one would therefore leave a band of
#: perfectly storable raw values whose *expression* can never be recorded -- and
#: the refusal would fire on those packages on every run for ever. That is the trap
#: `_FEEDSTOCK_NAME_LENGTH` records, reached by the same route: a column sized
#: against a value rather than against the value it is composed from. Four times is
#: comfortably above the widest expansion `collectors/spdx.py`'s table can produce,
#: and `tests/unit/django_apps/test_license.py` asserts the *relation* against that
#: table rather than the number -- so a spelling added with a longer identifier
#: fails there rather than at an insert.
_NORMALIZED_LICENSE_LENGTH: Final[int] = 2048

#: How wide the detection-method column is. `DetectionMethod`'s longest value is
#: `recognised-spelling`, nineteen characters, and the rest is headroom -- the same
#: shape `_MATCH_CONFIDENCE_LENGTH` has and for the same reason.
_DETECTION_METHOD_LENGTH: Final[int] = 32

#: The names of the three constraints `license_findings` carries, and the read
#: index its freshness query needs, declared on the terms every name above is: the
#: model declares them and the cases that assert the database refuses a violation
#: name them too.
#:
#: Three constraints and **no unique constraint of any kind** (`CPM-AD-2`): two
#: observations of one package's licence on one channel are two rows, and a
#: re-observation must insert.
#:
#: The first is **deliberately asymmetric**, and the asymmetry is the whole of
#: `CPM-FR-13`'s AC 1. It requires the normalized expression and the detection
#: method on a determinate row and forbids them on every other -- and it says
#: nothing whatever about `raw_license`, which is permitted on every row there is.
#: A row that failed to normalize *must* be able to carry the raw string: that is
#: what makes it a review item somebody can act on rather than an absence of
#: information, and a constraint that tidied it away would delete the evidence AC 1
#: exists to record.
#:
#: The second is the sibling security tables' rule, reached for the same reason:
#: `not_applicable` arrives in `LicenseOutcome` by construction and every package a
#: monitored channel could serve is licensed under something, so nothing may ever
#: write it here.
#:
#: The third is `conda_package_snapshots`' rule, reached because this table
#: observes the same *set* of surfaces: every row names the channel it is about,
#: sentinel rows included, because a row that could not say which channel stated a
#: licence would have merged the channels that disagree -- and channels disagreeing
#: is a fact this story exists to record rather than resolve.
#:
#: Django caps an index name at 30 characters, which is why it does not spell out
#: `license_finding` twice.
LICENSE_FACTS_CONSTRAINT: Final[str] = "license_facts_present_exactly_when_normalized"
LICENSE_APPLICABILITY_CONSTRAINT: Final[str] = "license_applies_to_every_package"
LICENSE_CHANNEL_CONSTRAINT: Final[str] = "license_names_the_channel_it_is_about"
LICENSE_READ_INDEX: Final[str] = "license_finding_pkg_observed"

#: How wide the assessed-series column is. `3.14` is four characters and this is
#: the shape `_STATE_LENGTH` has: a bound comfortably above every value the code
#: can produce, so the column is never the reason an honest row is refused.
_PYTHON_SERIES_LENGTH: Final[int] = 32

#: How wide the column holding a project's declared `Requires-Python` is.
#:
#: Its own constant rather than `_SPECIFIER_LENGTH` reused, on the terms
#: `_FEEDSTOCK_NAME_LENGTH` and `_VERSION_LENGTH` state: the two answer to
#: different sources. `pypi_release_snapshots.requires_python` is sized against what
#: `CPM-FR-8` records for a currency comparison; this one is sized against what
#: `CPM-PY314-S01` has to store **verbatim** for an incompatible row to be
#: argue-able, and a value wider than it is refused where it enters rather than
#: truncated -- a truncated specifier is a different specifier, which is the matrix
#: row this constant exists for. The same 128 today; reconciled by
#: `tests/unit/django_apps/test_python_readiness.py` against the model rather than
#: against a number restated in the collector.
_REQUIRES_PYTHON_LENGTH: Final[int] = 128

#: How wide the matching-classifier column is.
#:
#: A classifier is a fixed namespace plus a version --
#: `Programming Language :: Python :: 3.14` is thirty-eight characters -- so this is
#: headroom rather than a measurement, sized against the longest classifier PyPI's
#: own list carries rather than against the one this collector looks for.
_CLASSIFIER_LENGTH: Final[int] = 256

#: How wide the deciding-signal column is. `DecidingSignal`'s longest value is
#: `requires-python`, fifteen characters -- the same shape
#: `_DETECTION_METHOD_LENGTH` has and for the same reason.
_DECIDING_SIGNAL_LENGTH: Final[int] = 32

#: The names of the three constraints `python_readiness_assessments` carries, and
#: the read index its freshness query needs.
#:
#: Three constraints and **no unique constraint of any kind** (`CPM-AD-2`): a
#: project that declares 3.14 support tomorrow is a new row and the old one stands,
#: which is the whole of how a reader sees *when* a project became ready.
#:
#: The first is the sibling tables' biconditional, over the one column that is a
#: judgement rather than a transcription: the deciding signal is present exactly on
#: a determinate row. A row that inferred something and cannot say **which** piece
#: of metadata inferred it is a claim about somebody's package with no argument
#: attached, and a row that named a deciding signal without reaching a verdict
#: would be claiming a decision the run never made. `requires_python` and
#: `matching_classifier` appear in neither half, deliberately and on the terms
#: `LICENSE_FACTS_CONSTRAINT` states its own asymmetry: the specifier is what makes
#: an `unknown` row reviewable and an `inferred_incompatible` row argue-able, so it
#: is permitted on every row there is.
#:
#: The second is the **opposite** of the sibling security tables' applicability
#: rule, and it is the one place this table differs from all three of them.
#: `vulnerability_findings`, `kev_findings` and `license_findings` refuse
#: `not_applicable` outright because their questions apply to every package;
#: `CPM-PY314-S01` AC 2 asks for it in as many words, because a package with no
#: release ecosystem has no Python metadata to assess. What this table refuses
#: instead is a `not_applicable` row that does not say *why* -- `detail` is required
#: on it -- because the only honest reason is one `identity` established, and a row
#: that could not name it would be exactly the absence-read-as-inapplicability this
#: story exists to prevent.
#:
#: The third is `license_findings`' channel rule reached for the same reason: every
#: row names the Python series it assessed, sentinel rows included. `CPM-PY314-S02`
#: writes a *verified* result about a series, `CPM-PY314-S03` reduces both, and a
#: row that could not say which Python it was about would make an assessment of 3.14
#: indistinguishable from an assessment of whatever comes next.
#:
#: Django caps an index name at 30 characters, which is why it does not spell out
#: `python_readiness` twice.
READINESS_SIGNAL_CONSTRAINT: Final[str] = "readiness_signal_present_exactly_when_inferred"
READINESS_REASON_CONSTRAINT: Final[str] = "readiness_not_applicable_states_its_reason"
READINESS_SERIES_CONSTRAINT: Final[str] = "readiness_names_the_series_it_assessed"
READINESS_READ_INDEX: Final[str] = "py_readiness_pkg_observed"

#: How wide the column recording the platform a verification **ran on** is.
#:
#: Its own constant rather than `_PLATFORM_LENGTH` reused, on the terms
#: `_REQUIRES_PYTHON_LENGTH` states its own separation from `_SPECIFIER_LENGTH`:
#: the two answer to different sources. `conda_package_snapshots.platform` holds a
#: conda subdir an operator declared -- `linux-64`, `osx-arm64` -- and is sized
#: against that vocabulary. This one holds whatever the execution backend says it
#: ran on, and no backend is shipped (`CPM-PY314-S02`), so there is no vocabulary
#: to size against at all: 64 is headroom for a platform string a runner reports,
#: not a measurement of one. The two being the same number today is a coincidence
#: this comment exists to stop a later reader from reading as a shared decision.
_VERIFICATION_PLATFORM_LENGTH: Final[int] = 64

#: How wide the column recording the architecture a verification ran on is.
#:
#: Sized beside the platform and for the same reason: `x86_64`, `aarch64`,
#: `arm64`, `ppc64le` are what a runner reports, and a backend nobody has chosen
#: may report something longer. Separate from the platform's constant because the
#: two are separate facts -- `CPM-PY314-S02`'s AC 1 names them separately, and a
#: row that could name only one of them would be a row that cannot say where it
#: ran.
_ARCHITECTURE_LENGTH: Final[int] = 64

#: How wide the log-reference column is.
#:
#: Sized as a locator rather than as a name, on the terms `_ADVISORY_LOCATOR_LENGTH`
#: is: what goes in it is a URL, an object-store key or a run identifier some other
#: system minted, and none of those is a word a person picked. 768 rather than
#: `_LOCATOR_LENGTH`'s 512 for the reason the advisory locator takes it: a
#: reference this product did not build is one it cannot bound by construction.
#:
#: **A reference wider than this is refused where it enters and never truncated.**
#: A truncated log reference resolves to nothing, so a row carrying one would be a
#: determinate verification result whose evidence cannot be opened -- which is the
#: same failure as a row with no reference at all, wearing a value that looks
#: fine. `collectors/py314_verification.py` refuses the document instead.
_LOG_REFERENCE_LENGTH: Final[int] = 768

#: The names of the three constraints `python_verification_results` carries, and
#: the read index its freshness query needs.
#:
#: Three constraints and **no unique constraint of any kind** (`CPM-AD-2`), on the
#: terms `python_readiness_assessments` states: a package verified twice is two
#: rows and the first one stands, which is `CPM-PY314-S02`'s matrix row saying the
#: second trigger does not replace the first. The tuple that looks unique --
#: `(package, python_series, platform, architecture)` -- is exactly the tuple a
#: re-verification repeats.
#:
#: **The first is `CPM-PY314-S02`'s AC 1, made structural.** A verified row records
#: the platform, the architecture and a log reference; a row that is not determinate
#: records none of the three. Written as one biconditional over three columns rather
#: than as three separate rules, because "records where it ran" is one fact: a row
#: naming a platform and no architecture has not said where it ran either, and
#: letting the database hold two of three would make the acceptance criterion
#: partially satisfiable. A verification that cannot say where it ran is not
#: verification, and this is the database saying so rather than a collector
#: promising to.
#:
#: The second is `python_readiness_assessments`' reason rule, reached for the same
#: reason and with a sharper edge: `not_applicable` here means this product need
#: never *build* this package, and the only thing that can establish that is
#: `identity`. A row carrying the state and no reason would be indistinguishable
#: from one written out of an absence of identity.
#:
#: The third is the series rule, and it is what makes `CPM-PY314-S03` possible at
#: all: that policy reduces this table and the static one together, so a row that
#: could not say which Python it verified would make a 3.14 build indistinguishable
#: from a build of whatever comes next.
#:
#: Django caps an index name at 30 characters, which is why it does not spell out
#: `python_verification` twice.
VERIFICATION_EVIDENCE_CONSTRAINT: Final[str] = "verification_says_where_it_ran"
VERIFICATION_REASON_CONSTRAINT: Final[str] = "verification_not_applicable_states_its_reason"
VERIFICATION_SERIES_CONSTRAINT: Final[str] = "verification_names_the_series_it_ran"
VERIFICATION_READ_INDEX: Final[str] = "py_verify_pkg_observed"

#: How wide the `project_urls` key column is. A key is a label a project author
#: typed -- `Source`, `Source Code`, `Homepage` -- so it is sized as a name, on
#: the terms `identity/models.py` sizes one; `collectors/resolve_identity.py`
#: records only a key from its own short precedence list, so the bound is never
#: approached.
_REPOSITORY_KEY_LENGTH: Final[int] = 128

#: How wide the recorded-confidence column is: the same 32 `identity/models.py`
#: gives `Package.confidence`, restated rather than imported on the terms
#: `_TRACE_ID_LENGTH` states. Its longest value is `inventory-derived`,
#: seventeen characters.
_CONFIDENCE_LENGTH: Final[int] = 32

#: The name of the constraint `identity_resolution_snapshots` carries, and the
#: read index its freshness query needs.
#:
#: One biconditional and **no unique constraint of any kind** (`CPM-AD-2`): an
#: `ok` row is one on which `record_resolution` was reached, so it records the
#: confidence the package holds afterwards; a row that is not `ok` never reached
#: the recorder, so it records no confidence, no repository, no key, no PyPI
#: question, answer or locator, and no held-back claim. `feedstocks` is deliberately outside the
#: constraint: a JSON column compares differently on the two backends this
#: product runs against, and an empty list on a sentinel row is what the writer
#: guarantees rather than what the database checks.
#:
#: Django caps an index name at 30 characters, which is why it does not spell out
#: `identity_resolution`.
IDENTITY_RESOLUTION_FACTS_CONSTRAINT: Final[str] = "resolution_facts_present_exactly_when_recorded"
IDENTITY_RESOLUTION_READ_INDEX: Final[str] = "id_resolution_pkg_observed"


class InventoryReadError(ValueError):
    """An inventory read was asked for in terms it cannot be answered in.

    A `ValueError` subclass, matching `core/freshness.py`'s `FreshnessError` and
    `core/collection.py`'s `CollectorConfigurationError`: every "this input
    cannot describe what it claims to" in this product is a `ValueError`, so a
    caller catching one catches them all.
    """


class InventorySnapshot(AppendOnlyModel):
    """One observation of one package by the inventory source. Table `inventory_snapshots`.

    See the module docstring for why presence is `state`, why the counts are a
    constraint rather than NOT NULL columns, and why the relation is `PROTECT`.

    `observed_at` and `objects` come from `AppendOnlyModel`: the instant is
    supplied by the writer from an injected `Clock` (`CPM-AD-26`) and the manager
    is the one that offers no `update()` and no `delete()` (`CPM-AD-2`).
    """

    #: The package this observation is about, by the integer primary key
    #: `CPM-AD-3` fixes -- a real `ForeignKey`, as every reference to a package
    #: in this product now is (`core.CollectionRun`'s became one in
    #: `CPM-EVIDENCE-S09`).
    #:
    #: Non-nullable, which is the difference that matters here: an observation is
    #: always *about* a package, and this table's writer creates the package
    #: before it writes the row -- the shell and the snapshot commit together
    #: (`CPM-AD-25`, `CPM-AD-23`). A run ledger's reference is nullable because a
    #: sweep is scoped to no package; an observation of no package is not a thing
    #: that happens.
    package = models.ForeignKey(
        Package,
        on_delete=models.PROTECT,
        related_name="inventory_snapshots",
        verbose_name=_("package"),
    )

    #: The key the inventory source used for this package, kept so a row can be
    #: traced back to the record that produced it (`CPM-FR-42`). Stored on every
    #: row including an absence one, whose key is the one the source last used:
    #: "this key stopped appearing" is the observation, and a row that could not
    #: say which key would not be one.
    source_package_key = models.CharField(_("source package key"), max_length=_KEY_LENGTH)

    #: What the source said about this package, over `OutcomeState` and emitted
    #: verbatim (`CPM-AD-24`). `ok` when the source listed it, `not_found` when a
    #: source that listed it before no longer does, and `error` for the base's
    #: sentinel path. Never a boolean (`CPM-AD-5`).
    state = models.CharField(_("state"), max_length=_STATE_LENGTH, choices=OutcomeState.choices)

    #: How many internal components use this package. Required of a present
    #: observation and absent from every other kind -- see `Meta.constraints`.
    internal_component_count = models.PositiveIntegerField(
        _("internal component count"),
        null=True,
        blank=True,
        default=None,
    )

    #: How many internal lines of business use it. The other half of the
    #: "internal usage breadth" `CPM-FR-4` ranks by, and required on the same
    #: terms.
    internal_lob_count = models.PositiveIntegerField(_("internal LOB count"), null=True, blank=True, default=None)

    #: How many applications name it. A nullable score input (Open Question 3b):
    #: NULL means the source did not say, which stays distinguishable from a
    #: stored `0`.
    apps = models.PositiveIntegerField(_("apps"), null=True, blank=True, default=None)

    #: How many platforms it is used on. Nullable on the same terms as `apps`.
    platforms = models.PositiveIntegerField(_("platforms"), null=True, blank=True, default=None)

    #: How many times it was downloaded internally. Nullable on the same terms as
    #: `apps`, and the signal the missing-versus-zero distinction was argued
    #: about: a package nobody downloaded is not a package nobody counted.
    downloads = models.PositiveIntegerField(_("downloads"), null=True, blank=True, default=None)

    #: How many versions of it are in use. Nullable on the same terms as `apps`.
    versions = models.PositiveIntegerField(_("versions"), null=True, blank=True, default=None)

    #: What the collector or the base had to say about this observation -- the
    #: sentinel path's reason, or the words that go with an absence. Empty on an
    #: ordinary present observation, which needs no explanation.
    detail = models.TextField(_("detail"), blank=True, default="")

    #: The `trace_id` of the task that made this observation, formatted `032x`
    #: exactly as `config/observability/logging.py` formats it for every log line
    #: (`CPM-AD-15`). Empty when no span was active, which never blocks a write:
    #: an uncorrelated observation is worth more than no observation.
    trace_id = models.CharField(_("trace id"), max_length=_TRACE_ID_LENGTH, blank=True, default="")

    class Meta:
        """The table PRD Appendix A.2 names, not the `collectors_inventorysnapshot` Django derives.

        Rejected for the reason `core/models.py` and `identity/models.py` reject
        theirs: the tables in this product are named by the architecture and the
        PRD, and a derived name would make the schema depend on which
        application happened to declare the model.

        **No unique constraint of any kind** (`CPM-AD-2`, `CPM-AD-7`). Two
        observations of one package by one source are two rows, and idempotency
        is the run ledger's property rather than this table's -- a constraint
        spanning the observed fact would turn a re-observation into an
        `IntegrityError`, which is the same history loss arriving as a crash
        instead of an overwrite.
        """

        db_table = "inventory_snapshots"
        verbose_name = _("inventory snapshot")
        verbose_name_plural = _("inventory snapshots")
        indexes = [
            # The cut-off-bound read's shape, in the order it asks for it: one
            # package, newest observation first. Django's automatic foreign-key
            # index covers the filter alone and leaves the sort to a scan of that
            # package's whole history.
            models.Index(fields=["package", "-observed_at"], name=SNAPSHOT_READ_INDEX),
            # The absence derivation's, which asks about a key rather than about
            # a package -- it is the source's own identifier that stops appearing,
            # and the package is what that resolves to.
            models.Index(fields=["source_package_key"], name=SNAPSHOT_KEY_INDEX),
        ]
        constraints = [
            # The biconditional, and both halves are load bearing. A present
            # observation missing a count is a record the source should have
            # been refused for (`CPM-FR-42`); an absence row *carrying* counts
            # is a row claiming to have observed usage for a package the source
            # did not list, which is exactly the invented value Appendix A.1's
            # data rules forbid. `state` is NOT NULL and an `IS NULL` test is
            # never itself NULL, so this expression is always true or false and
            # never the third thing a SQL CHECK can be.
            models.CheckConstraint(
                condition=(
                    models.Q(
                        state=OutcomeState.OK,
                        internal_component_count__isnull=False,
                        internal_lob_count__isnull=False,
                    )
                    | (
                        ~models.Q(state=OutcomeState.OK)
                        & models.Q(internal_component_count__isnull=True, internal_lob_count__isnull=True)
                    )
                ),
                name=COUNTS_PRESENT_CONSTRAINT,
            ),
        ]

    def __str__(self) -> str:
        """Return the source's key, the state and when it was observed.

        Returns:
            A one-line summary. Read off `source_package_key` and `package_id`
            rather than off `package`, because the related object of an unsaved
            instance raises `RelatedObjectDoesNotExist` -- and a `__str__` that
            raises breaks the two places a half-built object is most likely to be
            rendered, a debugger and a traceback.

        """
        key = self.source_package_key or "(no source package key)"
        scope = "no package" if self.package_id is None else f"package {self.package_id}"
        when = "never" if self.observed_at is None else self.observed_at.isoformat()
        return f"{key} for {scope}: {self.state} at {when}"


def snapshot_as_of(*, package_id: int, cutoff: datetime) -> InventorySnapshot | None:
    """Return the latest observation of one package at or before a stated cut-off.

    `CPM-AD-25`'s cut-off-bound read, and the only supported one. A policy pass
    reads a usage signal *as of* its run's stated cut-off rather than as of now,
    which is what makes `CPM-FR-22`'s replay reproduce identical results: the row
    this returns for a given `(package, cutoff)` pair does not change when a
    later sweep writes another one.

    Args:
        package_id: The package being asked about, by the integer primary key
            `CPM-AD-3` fixes.
        cutoff: The instant to read as of, aware. `CPM-AD-21` makes it the
            `finished_at` of a completed collection run, so a pass never reads
            evidence written by a run that is still `running`.

    Returns:
        The newest snapshot whose `observed_at` is at or before the cut-off, or
        `None` when the package had none by then. `None` is not an error: a
        cut-off earlier than a package's first observation is an ordinary
        question with an ordinary answer, and inventing a row would be the
        clean-looking result `CPM-NFR-3` forbids.

        Ties are broken by descending primary key. Two rows can share an
        `observed_at` -- one sweep writes every row it produces with the run's
        one instant (`CPM-AD-7`) -- so an unordered tie would make the answer
        depend on the database's own arbitrary row order, and a replay would
        stop being a replay.

    Raises:
        InventoryReadError: When `cutoff` is naive. Refused rather than
            converted, on the same terms `core/freshness.py` refuses one: there
            is no offset to convert from, `USE_TZ` is on so Django would read it
            as if it were UTC, and a cut-off silently shifted by the reader's
            offset selects a different evidence set on every replay -- which is
            the opposite of what `CPM-FR-22` promises.

    """
    if not is_aware(cutoff):
        message = (
            f"an inventory snapshot cannot be read as of the naive cutoff {cutoff!r}. Every instant comes "
            f"from a Clock, which always answers in UTC (CPM-AD-26); a naive value has no offset to "
            f"interpret, so the read would be silently shifted by whichever offset the reader happened to "
            f"be in and the replay CPM-FR-22 promises would return a different set each time."
        )
        raise InventoryReadError(message)
    return (
        InventorySnapshot.objects.filter(package_id=package_id, observed_at__lte=cutoff)
        .order_by("-observed_at", "-pk")
        .first()
    )


class SourceReleaseSnapshot(AppendOnlyModel):
    """One observation of a package's upstream releases. Table `source_release_snapshots`.

    PRD Appendix A.2 gives this table four facts -- "upstream latest version,
    release date, repository activity, lookup status" -- and `CPM-FR-7` is where
    they come from: "latest release or tag, its date, and a repository activity
    signal from the package's source repository", with the lookup status recorded
    explicitly.

    **The lookup status is `state`, over `OutcomeState`, and never a boolean**
    (`CPM-AD-5`). `ok` is a version this run read; `not_found` is the source
    answering that there is none -- because the repository could not be read, or
    because it publishes no releases *and* lists no tags; `error` is a look that
    failed. Which of the two `not_found` means is in `detail`, and the reason it
    is there rather than in a second status column is that `OutcomeState` says
    what may be claimed about the package rather than why the run went the way it
    did. The
    column is called `state` rather than `*_status` for the reason
    `InventorySnapshot.state` is: `tests/unit/django_apps/test_outcome_field_audit.py`
    sweeps the derived-status *names*, and a collector's observation of what a
    source said is not a status a policy derived (`CPM-AD-8`).

    **A `not_found` row is the point of the table rather than an edge of it.**
    `CPM-FR-7` says a repository that publishes no releases "records that fact
    rather than reporting stale", and this is where that fact lives: a row, with
    this run's `observed_at`, carrying `not_found` and no version. Recording it as
    a missing observation instead would make the package read as unobserved --
    which `core/freshness.py` reports as `unknown` and which ages into stale --
    and the difference between "we have not looked" and "we looked and there is
    nothing to find" is exactly what `CPM-FR-6` exists to keep.

    **`releases_seen` is nullable because zero is an answer.** A row written from
    a document the collector actually read carries the number of releases that
    document listed, and `0` is a real observation. A sentinel row -- the base's
    `error` or `not_found` for a call that produced no document -- carries NULL,
    because nothing was counted. That is PRD Appendix A.1's "blank means missing;
    values are never invented" applied to the one column where missing and zero
    are both reachable.

    **`released_at` and `last_activity_at` are two facts, not one written twice.**
    The first is when the latest *release* -- the newest entry that is neither a
    draft nor a prerelease -- was published, which is what a currency comparison
    (`CPM-FR-16`) is made against. The second is the most recent instant the
    source showed any release activity at all, prereleases included, which is the
    repository activity signal `CPM-FR-7` asks for and is what distinguishes a
    project that stopped from one that is mid-cycle. They differ exactly when a
    project is actively cutting prereleases, which is the case the distinction was
    made for.

    **A determinate row may carry no date, and `source` is what says why.**
    `CPM-FR-7` asks for the latest release **or tag**, and a tag carries no
    publication date -- the endpoint that lists them supplies none. So a row whose
    version came from a tag is `ok`, names the version, and leaves `released_at`
    NULL; the constraint below permits exactly that and nothing looser. What tells
    a reader which happened is the `source` column, which names the locator the
    observation came from -- a releases endpoint or a tags one -- and which is
    also what keeps two observations of *different* repositories apart in an
    append-only history: `Package.source_repository_url` is mutable, so a package
    can be resolved to one repository and later corrected to another, and without
    this column nothing on the rows would say which was read.

    **A `not_found` row is weaker than it looks while no credential is
    configured.** `core/transport.py` reads `404` and `410` as "the source says
    this does not exist", and GitHub answers `404` identically for an absent
    repository, a private one, one that has moved and one that is blocked -- by
    design, so an unauthenticated reader cannot enumerate private repositories.
    The collector sends no credential, so it records the caveat in `detail` rather
    than claiming a fact it cannot support, and a reader comparing these rows must
    treat `not_found` as "absent **or** unreadable" until authentication lands.

    **`PROTECT`, and it is required rather than preferred**
    (`EVIDENCE.02-AUDIT-001`), on the terms `InventorySnapshot.package` states:
    Django's deletion collector issues its `DELETE` through `sql.DeleteQuery` and
    would go past every append-only refusal in `core/models.py` on the way.

    `observed_at` and `objects` come from `AppendOnlyModel`: the instant is
    supplied by the writer from an injected `Clock` (`CPM-AD-26`) and the manager
    is the one that offers no `update()` and no `delete()` (`CPM-AD-2`).
    """

    #: The package this observation is about, by the integer primary key
    #: `CPM-AD-3` fixes. Non-nullable: an observation is always about a package,
    #: and this collector is only ever asked about one that already has a row.
    package = models.ForeignKey(
        Package,
        on_delete=models.PROTECT,
        related_name="source_release_snapshots",
        verbose_name=_("package"),
    )

    #: The locator this observation was read from -- a releases endpoint or a tags
    #: one, naming the owner and repository that answered.
    #:
    #: Recorded on the row rather than left to the run ledger, because the ledger
    #: is not what a read surface queries: a policy comparing a package's
    #: observation history reads this table, and `Package.source_repository_url`
    #: is mutable, so the history can legitimately hold rows read from two
    #: different repositories with nothing else on them to tell which. Blank only
    #: on a row written by a caller that reached `sentinel_evidence` without
    #: asking for a locator first, which the base never does -- blank means
    #: missing, as it does everywhere else here.
    source = models.CharField(_("source"), max_length=_LOCATOR_LENGTH, blank=True, default="")

    #: What the lookup concluded, over `OutcomeState` and emitted verbatim
    #: (`CPM-AD-24`). See the class docstring for what each value means here.
    state = models.CharField(_("state"), max_length=_STATE_LENGTH, choices=OutcomeState.choices)

    #: The tag the latest upstream release carries, exactly as the source spelled
    #: it. Stored raw rather than normalised: `CPM-FR-16`'s comparison is a policy
    #: pass's (`CPM-AD-8`), and a collector that normalised a version would be
    #: deriving a value into an append-only row nothing may correct. Blank on every
    #: row that is not a determinate observation -- see `Meta.constraints`.
    latest_version = models.CharField(_("latest version"), max_length=_VERSION_LENGTH, blank=True, default="")

    #: When that release was published. NULL on every row that is not a
    #: determinate observation -- and NULL on a determinate one whose version came
    #: from a *tag*, because a tag carries no publication date. The constraint
    #: below says exactly that: a determinate row names a version, and a date is
    #: something only a release can supply.
    released_at = models.DateTimeField(_("released at"), null=True, blank=True, default=None)

    #: The repository activity signal: the most recent release activity the source
    #: showed, prereleases included. NULL when the source showed none, and
    #: permitted on a `not_found` row -- a repository that has cut only prereleases
    #: has no latest release and is plainly active, and a column that could not say
    #: both would lose the more interesting half.
    last_activity_at = models.DateTimeField(_("last activity at"), null=True, blank=True, default=None)

    #: How many releases the document this run read listed. `0` is an observation
    #: and NULL is the absence of one; see the class docstring. It stays `0` on a
    #: row whose version came from a tag, because the fallback is reached only
    #: when the release list was empty and that is the fact this column records.
    releases_seen = models.PositiveIntegerField(_("releases seen"), null=True, blank=True, default=None)

    #: What the collector or the base had to say about this observation -- the
    #: sentinel path's reason, or the words that go with a repository publishing
    #: nothing. Empty on an ordinary determinate observation, which needs no
    #: explanation.
    detail = models.TextField(_("detail"), blank=True, default="")

    #: The `trace_id` of the task that made this observation, formatted `032x`
    #: exactly as `config/observability/logging.py` formats it for every log line
    #: (`CPM-AD-15`). Empty when no span was active, which never blocks a write.
    trace_id = models.CharField(_("trace id"), max_length=_TRACE_ID_LENGTH, blank=True, default="")

    class Meta:
        """The table PRD Appendix A.2 names, not the `collectors_sourcereleasesnapshot` Django derives.

        Rejected for the reason `InventorySnapshot.Meta` rejects its own: the
        tables in this product are named by the architecture and the PRD, and a
        derived name would make the schema depend on which application happened to
        declare the model.

        **No unique constraint of any kind** (`CPM-AD-2`, `CPM-AD-7`). Two
        observations of one package's releases are two rows, and idempotency is
        the run ledger's property rather than this table's.
        """

        db_table = "source_release_snapshots"
        verbose_name = _("source release snapshot")
        verbose_name_plural = _("source release snapshots")
        indexes = [
            # `core/freshness.py`'s `latest_observation` reads exactly this: one
            # package, newest observation first. Django's automatic foreign-key
            # index covers the filter alone and leaves the sort to a scan of that
            # package's whole observation history, which grows daily and is never
            # pruned.
            models.Index(fields=["package", "-observed_at"], name=RELEASE_READ_INDEX),
        ]
        constraints = [
            # The biconditional, and all three conjuncts are load bearing.
            #
            # A determinate observation with no version is a row saying "there is
            # a latest version" while declining to say which -- the guess
            # `CPM-FR-1`'s sibling rule forbids in identity, and worse here
            # because nothing may correct it. A row that is *not* determinate and
            # carries a version is claiming to have observed something the run
            # never saw; one that carries a *date* without a version is claiming
            # a release date for a release it cannot name.
            #
            # A date is deliberately not required of a determinate row.
            # `CPM-FR-7` asks for the latest release **or tag**, and the endpoint
            # that lists tags supplies no date, so a tagged observation is `ok`,
            # names its version, and dates nothing. Requiring the date here would
            # make the honest answer unwritable and would push the collector into
            # inventing one.
            #
            # `state` is NOT NULL and an `IS NULL` test is never itself NULL, so
            # this expression is always true or false and never the third thing a
            # SQL CHECK can be.
            models.CheckConstraint(
                condition=(
                    (models.Q(state=OutcomeState.OK) & ~models.Q(latest_version=""))
                    | (~models.Q(state=OutcomeState.OK) & models.Q(latest_version="", released_at__isnull=True))
                ),
                name=RELEASE_FACTS_CONSTRAINT,
            ),
        ]

    def __str__(self) -> str:
        """Return the version, the state and when it was observed.

        Returns:
            A one-line summary. Read off `package_id` rather than off `package`,
            because the related object of an unsaved instance raises
            `RelatedObjectDoesNotExist` -- and a `__str__` that raises breaks the
            two places a half-built object is most likely to be rendered, a
            debugger and a traceback.

        """
        version = self.latest_version or "(no release)"
        scope = "no package" if self.package_id is None else f"package {self.package_id}"
        when = "never" if self.observed_at is None else self.observed_at.isoformat()
        return f"{version} for {scope}: {self.state} at {when}"


class PyPIReleaseSnapshot(AppendOnlyModel):
    """One observation of a package's PyPI project. Table `pypi_release_snapshots`.

    PRD Appendix A.2 gives this table its facts -- "PyPI existence, latest
    version and date, `Requires-Python`" -- and `CPM-FR-8` is where they come
    from: "project existence, latest version and date, and `Requires-Python`
    metadata", with a package that has no PyPI presence recording `not_found`
    and a non-Python package recording `not_applicable`.

    **Existence is `state`, over `OutcomeState`, and never a boolean**
    (`CPM-AD-5`). `ok` is a project this run read, with the version PyPI itself
    calls latest; `not_found` is the source answering that there is no such
    project -- or a project that lists no release, which `detail` says; `error`
    is a look that failed; and `not_applicable` is the row `CPM-FR-8` asks for
    when the package is not a Python package at all. The column is called
    `state` rather than `*_status` for the reason `SourceReleaseSnapshot.state`
    is: a collector's observation of what a source said is not a status a policy
    derived (`CPM-AD-8`).

    **The `not_applicable` row is the point of the table rather than an edge of
    it.** `CPM-FR-8` says a package "is never marked stale against PyPI merely
    for not being published there", and this is where that promise lives: a
    row, with this run's `observed_at`, carrying `not_applicable` and no facts.
    Recording nothing instead would make the package read as unobserved -- which
    `core/freshness.py` reports as `unknown` and which ages into stale -- and
    the difference between "we have not looked", "we looked and it is not
    there" and "the question is not about this package" is exactly what
    `CPM-FR-6` exists to keep. Applicability is not this row's to decide: it is
    read from what resolution recorded (`identity.PackageMapping`) and never
    inferred from a name (`CPM-FR-1`).

    **`released_at` is the moment the latest version became installable**: the
    earliest usable upload instant among that version's files. A version whose
    files carry no usable instant is still `ok` -- the version is a fact PyPI
    stated -- and dates nothing, with `detail` saying so; the constraint below
    permits exactly that, on the terms `SourceReleaseSnapshot` permits a tagged
    row with no date.

    **`requires_python` is blank when the project declares none**, which is PRD
    Appendix A.1's "blank means missing" applied to the one text fact here that
    a project may legitimately leave out. It is stored trimmed of surrounding
    whitespace and otherwise as the project spelled it: comparing it against an
    interpreter version is `CPM-FR-16`'s policy pass (`CPM-AD-8`), and a
    collector that normalised it would be deriving a value into a row nothing
    may correct.

    **A `not_found` row carries no caveat**, unlike its `source_release_snapshots`
    counterpart. PyPI is a public index and answers `404` for a project that
    does not exist and for nothing else -- there are no private projects for an
    unauthenticated reader to be shut out of -- so the row claims exactly what
    the source said.

    **`PROTECT`, and it is required rather than preferred**
    (`EVIDENCE.02-AUDIT-001`), on the terms `InventorySnapshot.package` states.

    `observed_at` and `objects` come from `AppendOnlyModel`: the instant is
    supplied by the writer from an injected `Clock` (`CPM-AD-26`) and the manager
    is the one that offers no `update()` and no `delete()` (`CPM-AD-2`).
    """

    #: The package this observation is about, by the integer primary key
    #: `CPM-AD-3` fixes. Non-nullable: an observation is always about a package.
    package = models.ForeignKey(
        Package,
        on_delete=models.PROTECT,
        related_name="pypi_release_snapshots",
        verbose_name=_("package"),
    )

    #: The locator this observation was read from, naming the project that
    #: answered. Recorded on the row for the reason `SourceReleaseSnapshot.source`
    #: is: `Package.primary_purl` is mutable, so an append-only history can hold
    #: rows read from two different projects. Blank on a `not_applicable` row,
    #: because no locator was ever built -- the question was never asked -- and
    #: blank means missing, as it does everywhere else here.
    source = models.CharField(_("source"), max_length=_LOCATOR_LENGTH, blank=True, default="")

    #: What the lookup concluded, over `OutcomeState` and emitted verbatim
    #: (`CPM-AD-24`). See the class docstring for what each value means here.
    state = models.CharField(_("state"), max_length=_STATE_LENGTH, choices=OutcomeState.choices)

    #: The version PyPI itself reports as the project's latest, trimmed of
    #: surrounding whitespace and otherwise exactly as the source spelled it.
    #: Stored unnormalised (`CPM-AD-8`). Blank on every row that is not a
    #: determinate observation -- see `Meta.constraints`.
    latest_version = models.CharField(_("latest version"), max_length=_VERSION_LENGTH, blank=True, default="")

    #: When that version became installable: the earliest upload instant among
    #: its files. NULL on every row that is not a determinate observation, and
    #: NULL on a determinate one whose files the source dated with nothing usable.
    released_at = models.DateTimeField(_("released at"), null=True, blank=True, default=None)

    #: The `Requires-Python` specifier the project declares, trimmed of
    #: surrounding whitespace and otherwise exactly as spelled. Blank when it
    #: declares none, and blank on every row that is not a determinate
    #: observation -- a sentinel row observed no metadata.
    requires_python = models.CharField(_("requires python"), max_length=_SPECIFIER_LENGTH, blank=True, default="")

    #: What the collector or the base had to say about this observation -- the
    #: sentinel path's reason, the words that go with a project listing no
    #: release, or with a version the source dated nothing for. Empty on an
    #: ordinary determinate observation, which needs no explanation.
    detail = models.TextField(_("detail"), blank=True, default="")

    #: The `trace_id` of the task that made this observation, formatted `032x`
    #: (`CPM-AD-15`). Empty when no span was active, which never blocks a write.
    trace_id = models.CharField(_("trace id"), max_length=_TRACE_ID_LENGTH, blank=True, default="")

    class Meta:
        """The table PRD Appendix A.2 names, not the `collectors_pypireleasesnapshot` Django derives.

        **No unique constraint of any kind** (`CPM-AD-2`, `CPM-AD-7`). Two
        observations of one package's PyPI project are two rows, and idempotency
        is the run ledger's property rather than this table's.
        """

        db_table = "pypi_release_snapshots"
        verbose_name = _("PyPI release snapshot")
        verbose_name_plural = _("PyPI release snapshots")
        indexes = [
            # `core/freshness.py`'s `latest_observation` reads exactly this, on
            # the terms `RELEASE_READ_INDEX` states.
            models.Index(fields=["package", "-observed_at"], name=PYPI_READ_INDEX),
        ]
        constraints = [
            # The biconditional, and all four conjuncts are load bearing.
            #
            # A determinate observation with no version is a row saying "there
            # is a latest version" while declining to say which. A row that is
            # *not* determinate and carries a version, a date or a specifier is
            # claiming to have observed something the run never saw -- and for
            # the `not_applicable` row in particular, claiming a fact about a
            # project the package does not have.
            #
            # A date is deliberately not required of a determinate row: PyPI
            # states a version even when it dates none of that version's files,
            # and requiring the date here would push the collector into
            # inventing one. A specifier is not required either, because a
            # project may declare no `Requires-Python` at all, and blank is how
            # PRD Appendix A.1 spells "missing".
            #
            # `state` is NOT NULL and an `IS NULL` test is never itself NULL, so
            # this expression is always true or false and never the third thing
            # a SQL CHECK can be.
            models.CheckConstraint(
                condition=(
                    (models.Q(state=OutcomeState.OK) & ~models.Q(latest_version=""))
                    | (
                        ~models.Q(state=OutcomeState.OK)
                        & models.Q(latest_version="", released_at__isnull=True, requires_python="")
                    )
                ),
                name=PYPI_FACTS_CONSTRAINT,
            ),
        ]

    def __str__(self) -> str:
        """Return the version, the state and when it was observed.

        Returns:
            A one-line summary, read off `package_id` rather than off `package`
            for the reason `SourceReleaseSnapshot.__str__` gives: the related
            object of an unsaved instance raises, and a `__str__` that raises
            breaks a debugger and a traceback alike.

        """
        # "(no version)" rather than "(no release)": every sentinel row lacks a
        # version, and a `not_applicable` row is not a claim that nothing was
        # released -- it is a claim that the question was not asked.
        version = self.latest_version or "(no version)"
        scope = "no package" if self.package_id is None else f"package {self.package_id}"
        when = "never" if self.observed_at is None else self.observed_at.isoformat()
        return f"{version} on PyPI for {scope}: {self.state} at {when}"


class FeedstockSnapshot(AppendOnlyModel):
    """One observation of a package's conda-forge feedstock. Table `feedstock_snapshots`.

    PRD Appendix A.2 gives this table "feedstock existence, recipe version,
    recipe activity, build/test outputs", and `CPM-FR-9` is where the first three
    come from: "feedstock existence, recipe version, recipe metadata, and recent
    recipe activity", with absence recorded as an observation carrying a
    timestamp and staged-recipe state recorded separately from an existing
    feedstock.

    **The build and test outputs are deliberately not here.** They are
    `CPM-EP-PY314`'s -- a build this product *performed* rather than a fact
    conda-forge stated -- and a column added for them now would be one nothing
    writes and nothing may correct.

    **Existence is `state`, over `OutcomeState`, and never a boolean**
    (`CPM-AD-5`). `ok` is a feedstock this run read; `not_found` is conda-forge
    answering that there is none -- either the feedstock resolution named is
    gone, or resolution established none and the conventional repository has
    none either; `error` is a look that failed; `not_applicable` is a package
    whose feedstock mapping resolution recorded as inapplicable. The column is
    called `state` rather than `*_status` for the reason
    `SourceReleaseSnapshot.state` is: a collector's observation of what a source
    said is not a status a policy derived (`CPM-AD-8`).

    **An absence row is the point of the table rather than an edge of it.**
    `CPM-FR-9` says "absence of a feedstock is recorded as an observation with a
    timestamp, not as a null", and this is where that lives: a row, with this
    run's `observed_at`, carrying `not_found` and no feedstock fact. A missing
    row would make the package read as unobserved -- `unknown`, ageing into
    stale -- and the difference between "we have not looked" and "we looked and
    conda-forge has nothing" is exactly what `CPM-FR-6` exists to keep.

    **`absence_established` is what makes a `not_found` row readable by a
    policy.** `not_found` is reachable four ways -- the repository answered
    absent, the repository could not be read, the queue could not be read, and
    the queue answered ambiguously or overflowed its page -- and only the first
    is evidence that there is nothing there. Until `CPM-CURRENCY-S07` the
    distinction lived in `detail` alone, which is prose; the column says the same
    thing structurally so a reader outside `collectors/` can tell an established
    absence from a failure to find out without matching on a sentence. See the
    field for why a boolean here is not the boolean `CPM-AD-5` bans.

    **`staged_recipe_url` is a fact about a package that has no feedstock, and
    the database says so.** AC 2 asks for staged-recipe state "recorded
    separately from an existing feedstock", and `staged_recipe_only_when_absent`
    is that separation expressed as a rule a writer can be held to rather than a
    convention a later collector could quietly break: a row carrying `ok` -- a
    feedstock was found -- may not also claim a recipe is queued to create one.

    **`last_recipe_activity_at` is an instant and not a verdict.** PRD Open
    Question 10 asks what counts as recipe activity; `CPM-CURRENCY-S03` answers
    "a push to the feedstock repository" and records the instant. What makes a
    gap *inactivity* is `CPM-FR-40`'s policy with a versioned threshold
    (`CPM-CURRENCY-S07`), and a collector that derived one here would be writing
    a derived status into an append-only row (`CPM-AD-8`).

    **`recipe_version` may be blank on a determinate row**, and that is the
    honest shape rather than a gap. The feedstock's existence is what `state`
    claims; the recipe is a second document, read by a second call whose failure
    never fails the collection, and a recipe that computes its version in a way
    this collector does not read leaves the column blank with `detail` saying
    so. Stored unnormalised (`CPM-AD-8`).

    **`PROTECT`, and it is required rather than preferred**
    (`EVIDENCE.02-AUDIT-001`), on the terms `InventorySnapshot.package` states.

    `observed_at` and `objects` come from `AppendOnlyModel`: the instant is
    supplied by the writer from an injected `Clock` (`CPM-AD-26`) and the manager
    is the one that offers no `update()` and no `delete()` (`CPM-AD-2`).
    """

    #: The package this observation is about, by the integer primary key
    #: `CPM-AD-3` fixes. Non-nullable: an observation is always about a package.
    package = models.ForeignKey(
        Package,
        on_delete=models.PROTECT,
        related_name="feedstock_snapshots",
        verbose_name=_("package"),
    )

    #: The locator this observation was read from -- a feedstock repository, or
    #: the staged-recipes search this collector asks about a package resolution
    #: established no feedstock for. Recorded on the row for the reason
    #: `SourceReleaseSnapshot.source` is: which question was asked is not
    #: recoverable from the answer, and a package's feedstock mapping is mutable.
    #: Blank on a `not_applicable` row, because no locator was ever built.
    source = models.CharField(_("source"), max_length=_LOCATOR_LENGTH, blank=True, default="")

    #: What the lookup concluded, over `OutcomeState` and emitted verbatim
    #: (`CPM-AD-24`). See the class docstring for what each value means here.
    state = models.CharField(_("state"), max_length=_STATE_LENGTH, choices=OutcomeState.choices)

    #: The feedstock's name on conda-forge, as the repository that answered
    #: spells it. Present on exactly the determinate rows -- see
    #: `Meta.constraints` -- because "a feedstock exists" and "this is which one"
    #: are one fact, and a row claiming the first without the second would be an
    #: existence claim nothing could check.
    feedstock_name = models.CharField(_("feedstock name"), max_length=_FEEDSTOCK_NAME_LENGTH, blank=True, default="")

    #: Where that feedstock lives, as the repository stated it rather than as
    #: this collector composed it: the locator asked was an API endpoint, and the
    #: URL a reader wants is the one a person can open.
    feedstock_url = models.CharField(_("feedstock URL"), max_length=_LOCATOR_LENGTH, blank=True, default="")

    #: The version the recipe pins, exactly as the recipe spelled it and
    #: unnormalised (`CPM-AD-8`). Blank when the recipe could not be read or
    #: names its version some way this collector does not read -- with `detail`
    #: saying which -- and blank on every row that is not determinate.
    recipe_version = models.CharField(_("recipe version"), max_length=_VERSION_LENGTH, blank=True, default="")

    #: The build number the recipe declares. NULL means missing and `0` means
    #: zero, which is PRD Appendix A.1's rule applied to the one integer here
    #: where both are reachable: a first build of a version is `0`, and a recipe
    #: whose build number this collector could not read is not a first build.
    recipe_build_number = models.PositiveIntegerField(_("recipe build number"), null=True, blank=True, default=None)

    #: Where the recipe metadata this run read lives. Blank when the recipe was
    #: not read at all, which is the honest value for a row whose `state` rests
    #: on the repository alone.
    recipe_metadata_url = models.CharField(
        _("recipe metadata URL"),
        max_length=_LOCATOR_LENGTH,
        blank=True,
        default="",
    )

    #: When the feedstock was last pushed to -- the recipe activity signal
    #: `CPM-FR-9` asks for, recorded as an instant and never as a verdict. NULL
    #: when the source stated none this collector could read, and NULL on every
    #: row that is not determinate.
    last_recipe_activity_at = models.DateTimeField(
        _("last recipe activity at"),
        null=True,
        blank=True,
        default=None,
    )

    #: The open staged-recipes pull request that would create this package's
    #: feedstock, where exactly one was found. Recordable only on a `not_found`
    #: row -- see `Meta.constraints` -- and blank where the search matched
    #: nothing, or matched more than one and was refused rather than picked.
    staged_recipe_url = models.CharField(_("staged recipe URL"), max_length=_LOCATOR_LENGTH, blank=True, default="")

    #: Whether this run **established** the absence it records, as opposed to
    #: failing to find out.
    #:
    #: **The column exists because `not_found` is reachable four ways and only
    #: one of them is evidence of absence.** `collectors/feedstock.py` writes it
    #: when conda-forge answered that the conventional repository is not there
    #: *and* the staged-recipes queue answered conclusively; it writes `False`
    #: when the repository could not be read, when the queue could not be read,
    #: when the queue held more than one open pull request so none was recorded,
    #: and when the search overflowed its page so a blank match is not evidence of
    #: none. Every one of those already produced a distinct `detail`, and `detail`
    #: is prose: `CPM-CURRENCY-S07`'s policy pass has to tell an established
    #: absence from an unestablished one to decide whether to report a gap
    #: somebody should go and fill, and matching on a sentence across an
    #: application boundary is a coupling that breaks silently on a reword.
    #:
    #: **A boolean, and `CPM-AD-5` is not violated by it.** That decision bans a
    #: boolean *status*: the five-state vocabulary is what `state` above carries,
    #: and this is not a fifth state or a second axis on one. It is a fact about
    #: the observation -- did this run finish asking -- of exactly the kind
    #: `truncated` and `matched` are inside `collectors/feedstock.py`'s own
    #: `StagedRecipe`, and it has two values because the question has two
    #: answers.
    #:
    #: `False` on every row that is not an absence, which `Meta.constraints`
    #: makes a database rule: an `ok` row establishes a feedstock's *presence*,
    #: and a row claiming to have established an absence while naming a feedstock
    #: would be saying both things at once.
    absence_established = models.BooleanField(_("absence established"), default=False)

    #: What the collector or the base had to say about this observation -- the
    #: sentinel path's reason, why the recipe could not be read, how many
    #: feedstocks the mapping held, or that both a feedstock and a staged recipe
    #: were looked for and neither found.
    detail = models.TextField(_("detail"), blank=True, default="")

    #: The `trace_id` of the task that made this observation, formatted `032x`
    #: (`CPM-AD-15`). Empty when no span was active, which never blocks a write.
    trace_id = models.CharField(_("trace id"), max_length=_TRACE_ID_LENGTH, blank=True, default="")

    class Meta:
        """The table PRD Appendix A.2 names, not the `collectors_feedstocksnapshot` Django derives.

        **No unique constraint of any kind** (`CPM-AD-2`, `CPM-AD-7`). Two
        observations of one package's feedstock are two rows, and idempotency is
        the run ledger's property rather than this table's.
        """

        db_table = "feedstock_snapshots"
        verbose_name = _("feedstock snapshot")
        verbose_name_plural = _("feedstock snapshots")
        indexes = [
            # `core/freshness.py`'s `latest_observation` reads exactly this, on
            # the terms `RELEASE_READ_INDEX` states.
            models.Index(fields=["package", "-observed_at"], name=FEEDSTOCK_READ_INDEX),
        ]
        constraints = [
            # The biconditional, and every conjunct is load bearing.
            #
            # A determinate observation that names no feedstock is a row saying
            # "conda-forge has one" while declining to say which. A row that is
            # *not* determinate and carries any feedstock fact -- a name, a URL,
            # a recipe version, a build number, a metadata URL or an activity
            # instant -- is claiming to have observed something about a
            # feedstock the run did not find, and for the `not_applicable` row
            # in particular a fact about a package nobody asked about.
            #
            # A recipe version is deliberately *not* required of a determinate
            # row: the feedstock's existence is what `state` claims, the recipe
            # is a second document read by a second call, and requiring it here
            # would make a real answer unwritable and push the collector into
            # inventing one.
            #
            # `state` is NOT NULL and an `IS NULL` test is never itself NULL, so
            # this expression is always true or false and never the third thing
            # a SQL CHECK can be.
            models.CheckConstraint(
                condition=(
                    (models.Q(state=OutcomeState.OK) & ~models.Q(feedstock_name=""))
                    | (
                        ~models.Q(state=OutcomeState.OK)
                        & models.Q(
                            feedstock_name="",
                            feedstock_url="",
                            recipe_version="",
                            recipe_build_number__isnull=True,
                            recipe_metadata_url="",
                            last_recipe_activity_at__isnull=True,
                        )
                    )
                ),
                name=FEEDSTOCK_FACTS_CONSTRAINT,
            ),
            # AC 2, as a database rule. A staged recipe is a proposal to create
            # a feedstock that does not exist, so it belongs only on the row
            # that says one does not: `not_found`. An `ok` row carrying one
            # would say both things at once, and an `error` or `not_applicable`
            # row carrying one would claim a search this run never made.
            models.CheckConstraint(
                condition=models.Q(staged_recipe_url="") | models.Q(state=OutcomeState.NOT_FOUND),
                name=STAGED_RECIPE_CONSTRAINT,
            ),
            # An absence can only be established by a row that records one. An
            # `ok` row says a feedstock exists; an `error` or `not_applicable`
            # row says nobody found out. A row claiming `absence_established`
            # beside any of those would be two opposite statements about the same
            # observation, and a reader deciding whether to report a gap somebody
            # should fill would have no way to tell which half to believe.
            #
            # `state` is NOT NULL and `absence_established` is NOT NULL, so this
            # expression is always true or false and never the third thing a SQL
            # CHECK can be.
            models.CheckConstraint(
                condition=models.Q(absence_established=False) | models.Q(state=OutcomeState.NOT_FOUND),
                name=ESTABLISHED_ABSENCE_CONSTRAINT,
            ),
        ]

    def __str__(self) -> str:
        """Return the feedstock, the state and when it was observed.

        Returns:
            A one-line summary, read off `package_id` rather than off `package`
            for the reason `SourceReleaseSnapshot.__str__` gives: the related
            object of an unsaved instance raises, and a `__str__` that raises
            breaks a debugger and a traceback alike.

        """
        # "(no feedstock)" rather than "(no recipe)": what a row without a name
        # lacks is the feedstock itself, and a `not_applicable` row is not a
        # claim that a recipe is missing.
        feedstock = self.feedstock_name or "(no feedstock)"
        scope = "no package" if self.package_id is None else f"package {self.package_id}"
        when = "never" if self.observed_at is None else self.observed_at.isoformat()
        return f"{feedstock} for {scope}: {self.state} at {when}"


class CondaPackageSnapshot(AppendOnlyModel):
    """One observation of one package on one channel and one platform. Table `conda_package_snapshots`.

    PRD Appendix A.2 gives this table "published version, channel, build string",
    and `CPM-FR-10` is where they come from: "published version, build string,
    and channel for each monitored channel", with each monitored channel
    producing its own observation and channels never merged.

    **One row per `(channel, platform)`, and that shape is the whole of AC 1.**
    A build string is a property of a *build*, and a build is per platform, so a
    row that named a channel and one build string would already have merged the
    platforms to produce it. Splitting on both is the only shape in which no row
    stands for two of anything -- and it is what makes "installable on `linux-64`
    but not on `osx-arm64`" expressible, which is what a packaging engineer
    reading this table is looking for.

    **`channel` and `platform` are required of every row, sentinel rows
    included**, and `conda_package_names_channel_and_platform` is the database
    saying so. This is the one column pair the other three evidence tables have
    no analogue of: the surfaces they observe are singular -- a repository, a
    project, a feedstock -- while this one observes a *set* of surfaces that a
    reader must be able to tell apart for ever. A row that could not say which
    pair it was about would be an observation of "somewhere", which is precisely
    the merge `CPM-FR-10` forbids.

    **The published version is the one the channel itself states as latest**, and
    nothing here compares it to anything. `CPM-FR-16`'s currency comparison is a
    policy pass (`CPM-AD-8`), so a collector that ranked or normalised a version
    would be writing a derived status into an append-only row.

    **Presence is `state`, over `OutcomeState`, and never a boolean**
    (`CPM-AD-5`). `ok` is a published artifact this run read; `not_found` is the
    channel answering that there is none for this pair -- either the channel does
    not serve the package at all, or its latest version has no file on this
    platform; `error` is a look that failed. The column is called `state` rather
    than `*_status` for the reason `SourceReleaseSnapshot.state` is.

    **A `not_found` row is the point of the table rather than an edge of it.** A
    package that is current upstream, current on PyPI and current in the recipe
    while nothing is installable on a platform is exactly the gap `CPM-FR-10`
    exists to surface, and it is only visible if the absence is a row carrying
    this run's instant rather than a missing one.

    **`build_string` and `build_number` are not required of a determinate row**,
    on the terms `SourceReleaseSnapshot` does not require a date of one. The fact
    `state` claims is that the channel publishes this version for this platform;
    the build a channel states beside it is a second fact it may state poorly or
    not at all, and requiring it here would make the honest answer unwritable and
    push the collector into inventing one. `detail` says why it is blank.

    **NULL means missing and `0` means zero** for `build_number`: a first build of
    a version is `0`, and a build whose number the channel did not state is not a
    first build. PRD Appendix A.1's rule, applied to the one integer here where
    both are reachable.

    **`PROTECT`, and it is required rather than preferred**
    (`EVIDENCE.02-AUDIT-001`), on the terms `InventorySnapshot.package` states.

    `observed_at` and `objects` come from `AppendOnlyModel`: the instant is
    supplied by the writer from an injected `Clock` (`CPM-AD-26`) and the manager
    is the one that offers no `update()` and no `delete()` (`CPM-AD-2`).
    """

    #: The package this observation is about, by the integer primary key
    #: `CPM-AD-3` fixes. Non-nullable: an observation is always about a package.
    package = models.ForeignKey(
        Package,
        on_delete=models.PROTECT,
        related_name="conda_package_snapshots",
        verbose_name=_("package"),
    )

    #: The locator this observation was read from -- the channel's own package
    #: document. Recorded on the row for the reason `SourceReleaseSnapshot.source`
    #: is, and for one this table adds: a run reads several locators and writes
    #: several rows, so `source` is what ties each row to the answer it came from
    #: rather than to the one the run happened to start with.
    source = models.CharField(_("source"), max_length=_LOCATOR_LENGTH, blank=True, default="")

    #: What the lookup concluded, over `OutcomeState` and emitted verbatim
    #: (`CPM-AD-24`). See the class docstring for what each value means here.
    state = models.CharField(_("state"), max_length=_STATE_LENGTH, choices=OutcomeState.choices)

    #: The channel this row is about, as the operator declared it and lower-cased.
    #: Required of every row -- see `Meta.constraints`.
    channel = models.CharField(_("channel"), max_length=_CHANNEL_LENGTH)

    #: The conda subdir this row is about -- `linux-64`, `osx-arm64`, `noarch`.
    #: Required of every row, for the same reason and by the same constraint.
    platform = models.CharField(_("platform"), max_length=_PLATFORM_LENGTH)

    #: The version the channel itself states as this package's latest, exactly as
    #: spelled and unnormalised (`CPM-AD-8`). Blank on every row that is not a
    #: determinate observation -- see `Meta.constraints`.
    published_version = models.CharField(
        _("published version"),
        max_length=_VERSION_LENGTH,
        blank=True,
        default="",
    )

    #: The build string of the file that publishes that version on this platform,
    #: exactly as the channel spelled it. May be blank on a determinate row -- see
    #: the class docstring -- and is blank on every row that is not one.
    build_string = models.CharField(_("build string"), max_length=_BUILD_STRING_LENGTH, blank=True, default="")

    #: That file's build number. NULL means the channel stated none and `0` means
    #: the first build; NULL on every row that is not a determinate observation.
    build_number = models.PositiveIntegerField(_("build number"), null=True, blank=True, default=None)

    #: What the collector or the base had to say about this observation -- which
    #: version exists on another platform, that the channel named no latest
    #: version at all, or why one channel's answer could not be read while
    #: another's was.
    detail = models.TextField(_("detail"), blank=True, default="")

    #: The `trace_id` of the task that made this observation, formatted `032x`
    #: (`CPM-AD-15`). Empty when no span was active, which never blocks a write.
    trace_id = models.CharField(_("trace id"), max_length=_TRACE_ID_LENGTH, blank=True, default="")

    class Meta:
        """The table PRD Appendix A.2 names, not the `collectors_condapackagesnapshot` Django derives.

        **No unique constraint of any kind** (`CPM-AD-2`, `CPM-AD-7`). Two
        observations of one package on one channel and platform are two rows, and
        idempotency is the run ledger's property rather than this table's -- and
        here a unique constraint would be worse than elsewhere, because the pair
        that looks unique (`package`, `channel`, `platform`, `observed_at`) is
        exactly the tuple a re-observation repeats.
        """

        db_table = "conda_package_snapshots"
        verbose_name = _("conda package snapshot")
        verbose_name_plural = _("conda package snapshots")
        indexes = [
            # `core/freshness.py`'s `latest_observation` reads exactly this, on
            # the terms `RELEASE_READ_INDEX` states.
            models.Index(fields=["package", "-observed_at"], name=CONDA_PACKAGE_READ_INDEX),
            # The read this table exists for: one package, one channel, one
            # platform, newest first. See `CONDA_PACKAGE_PAIR_INDEX`.
            models.Index(
                fields=["package", "channel", "platform", "-observed_at"],
                name=CONDA_PACKAGE_PAIR_INDEX,
            ),
        ]
        constraints = [
            # The biconditional, and every conjunct is load bearing.
            #
            # A determinate observation with no version is a row saying "this
            # channel publishes this package for this platform" while declining
            # to say what. A row that is *not* determinate and carries a version,
            # a build string or a build number is claiming to have observed a
            # published artifact the run never found.
            #
            # A build string is deliberately *not* required of a determinate row:
            # the fact `state` claims is that the version is published here, and
            # a channel that states the version while stating the build poorly is
            # an answer rather than a defect. Requiring it would push the
            # collector into inventing one.
            #
            # `state` is NOT NULL and an `IS NULL` test is never itself NULL, so
            # this expression is always true or false and never the third thing a
            # SQL CHECK can be.
            models.CheckConstraint(
                condition=(
                    (models.Q(state=OutcomeState.OK) & ~models.Q(published_version=""))
                    | (
                        ~models.Q(state=OutcomeState.OK)
                        & models.Q(published_version="", build_string="", build_number__isnull=True)
                    )
                ),
                name=CONDA_PACKAGE_FACTS_CONSTRAINT,
            ),
            # AC 1, as a database rule. Every row names the channel and the
            # platform it is about -- including the sentinel rows the base writes,
            # which is the half a convention would have missed. A blank in either
            # column is a row that has merged every monitored surface into one,
            # which is the merge `CPM-FR-10` forbids.
            models.CheckConstraint(
                condition=~models.Q(channel="") & ~models.Q(platform=""),
                name=CHANNEL_AND_PLATFORM_CONSTRAINT,
            ),
        ]

    def __str__(self) -> str:
        """Return the version, the pair it is about, the state and when it was observed.

        Returns:
            A one-line summary, read off `package_id` rather than off `package`
            for the reason `SourceReleaseSnapshot.__str__` gives: the related
            object of an unsaved instance raises, and a `__str__` that raises
            breaks a debugger and a traceback alike.

        """
        version = self.published_version or "(nothing published)"
        where = f"{self.channel or '(no channel)'}/{self.platform or '(no platform)'}"
        scope = "no package" if self.package_id is None else f"package {self.package_id}"
        when = "never" if self.observed_at is None else self.observed_at.isoformat()
        return f"{version} on {where} for {scope}: {self.state} at {when}"


class VulnerabilityFinding(AppendOnlyModel, FindingKeyed):
    """One advisory match, or one statement that nothing was matched. Table `vulnerability_findings`.

    PRD Appendix A.2 gives this table "Advisory ID, affected and fixed ranges,
    severity, matched version, source, match confidence", and `CPM-FR-11` is
    where they come from: "every finding records advisory ID, severity, affected
    range, fixed range, matched version, source, and match confidence", and "a
    package the collector could not match records `unknown`, never clean".

    **One row per matched advisory, and never one row for several.** Three
    advisories about one package at one version are three rows, each carrying its
    own identifier and its own ranges, because a reviewer's question is about one
    advisory at a time -- is *this* finding real, is it fixed, is it ours -- and a
    row standing for several could not answer it for any of them.

    **A package nothing matched still gets a row, and it carries `unknown`.**
    That is `CPM-SM-2` ("zero findings present an unknown as clean") made
    structural rather than intended: a package with no row reads as unobserved,
    which `core/freshness.py` reports as `unknown` and which then ages into
    stale, and there would be nothing anywhere distinguishing "we read the source
    and it matched nothing" from "nobody looked". `unknown` rather than
    `not_found` is the whole of the difference: `not_found` is an informative
    negative -- we looked, and the thing is not there -- which for advisories
    reads as *clean*, and neither this collector nor its source is in a position
    to say that. What the row does say is what happened, in `detail`.

    **Presence is `state`, over `VulnerabilityOutcome`, and never a boolean**
    (`CPM-AD-5`). `matched` is one advisory this run matched; `unknown` is a run
    that established nothing about this package's exposure; `error` is a look that
    failed; `not_found` is the advisory source reporting that the locator itself
    does not exist, which is a misconfigured or withdrawn endpoint rather than a
    clean package. The column is called `state` rather than `*_status` for the
    reason `SourceReleaseSnapshot.state` is.

    **The determinate value is `matched` and emphatically not `ok`**, and this is
    the one place in this product where that distinction is load-bearing. On every
    sibling table a determinate row is a surface answering normally; here it is an
    advisory *against this package*. `core`'s single precedence order ranks `ok`
    best of five and `CPM-AD-24` carries a state's value verbatim onto every read
    surface, so a table using `ok` would render exactly the vulnerable packages as
    the clean ones and rank a matched advisory above a package nothing matched.
    `collectors/outcomes.py` composes the vocabulary that fixes it and argues the
    choice at length.

    **Nothing here is ranked, normalised or compared.** `severity` is the string
    the source stated, `affected_range` and `fixed_range` are the expressions the
    source wrote in its own ecosystem's grammar, and `match_confidence` is the
    source's own claim about how it matched. Every verdict over them is
    `CPM-FR-17`'s policy pass (`CPM-AD-8`), which is `CPM-SECURITY-S04`; a
    collector that ranked a severity would be writing a derived status into a row
    nothing may correct, and one that evaluated a range would be inventing
    version semantics for an ecosystem it does not own.

    **`matched_version` is on the row because everything it could be read back
    from is mutable.** `Package.primary_purl` is corrected by resolution and
    every published version moves, so a finding that named no version would be a
    permanent claim whose subject changes underneath it -- and "does this apply to
    the version we actually ship" is the entire question this story exists to
    answer.

    **`fixed_range` and `severity` may be blank on a determinate row**, on the
    terms `SourceReleaseSnapshot` does not require a date of one: a real advisory
    with no fix published yet has no fixed range, and a source that states no
    severity has not stated one. Blank means *missing* (PRD Appendix A.1), it is
    never inferred, and `detail` names **each** blank the source left --
    `collectors/vulnerability.py` writes one clause per absent fact, so a reader
    is never left to work out which of the two a blank column is. `advisory_id`,
    `affected_range`, `matched_version` and `match_confidence` are required of a
    determinate row by `Meta.constraints`, because a finding missing any of them
    cannot say which advisory it is, what it applies to, what it was checked
    against, or how surely.

    **`PROTECT`, and it is required rather than preferred**
    (`EVIDENCE.02-AUDIT-001`), on the terms `InventorySnapshot.package` states.

    `observed_at` and `objects` come from `AppendOnlyModel`: the instant is
    supplied by the writer from an injected `Clock` (`CPM-AD-26`) and the manager
    is the one that offers no `update()` and no `delete()` (`CPM-AD-2`).
    """

    #: `CPM-AD-22`'s worked example, and the one the decision spells out:
    #: `package + advisory_id + affected_range`.
    #:
    #: **`matched_version` is deliberately not part of it.** A package upgraded from
    #: 3.9.1 to 3.9.2 while an advisory is still open would otherwise produce a
    #: *second* finding for the same advisory -- and the reviewer who accepted the
    #: first would meet it again as new work, which is exactly the failure the key
    #: exists to prevent. The affected range is the advisory's claim about which
    #: versions are exposed and does not move when the installed one does.
    #:
    #: `severity` is not part of it either: a source that re-scores an advisory from
    #: high to critical has changed how urgent one finding is, not created another.
    FINDING_KEY_FIELDS: ClassVar[tuple[str, ...]] = ("advisory_id", "affected_range")

    #: The package this observation is about, by the integer primary key
    #: `CPM-AD-3` fixes. Non-nullable: an observation is always about a package.
    package = models.ForeignKey(
        Package,
        on_delete=models.PROTECT,
        related_name="vulnerability_findings",
        verbose_name=_("package"),
    )

    #: Where this observation came from -- the locator the declared advisory
    #: source adapter recorded for the answer it gave, or the locator this run
    #: asked about where no answer was read. It is the `source` `CPM-FR-11`
    #: requires of every finding, and it is what makes the source pluggable
    #: without the policy layer learning which one is active (`CPM-AD-29`): a
    #: policy reads this column like any other evidence's.
    source = models.CharField(_("source"), max_length=_ADVISORY_LOCATOR_LENGTH, blank=True, default="")

    #: What the lookup concluded, over `VulnerabilityOutcome` and emitted verbatim
    #: (`CPM-AD-24`). See the class docstring for what each value means here, and
    #: `collectors/outcomes.py` for why the determinate value is `matched`.
    state = models.CharField(_("state"), max_length=_STATE_LENGTH, choices=VulnerabilityOutcome.choices)

    #: The advisory's own identifier, exactly as the source spelled it. Required
    #: of a determinate row and blank on every other -- see `Meta.constraints`.
    advisory_id = models.CharField(_("advisory id"), max_length=_ADVISORY_ID_LENGTH, blank=True, default="")

    #: The severity as the source stated it, unranked and unnormalised. May be
    #: blank on a determinate row, which means the source stated none.
    severity = models.CharField(_("severity"), max_length=_SEVERITY_LENGTH, blank=True, default="")

    #: The versions the source says the advisory affects, as the source expressed
    #: them. Required of a determinate row: a finding that cannot say what it
    #: applies to cannot be judged against anything.
    affected_range = models.CharField(_("affected range"), max_length=_RANGE_LENGTH, blank=True, default="")

    #: The versions the source says carry the fix, as the source expressed them.
    #: Blank means the source stated none -- an advisory with no published fix is
    #: an ordinary state, and `detail` says which case this is.
    fixed_range = models.CharField(_("fixed range"), max_length=_RANGE_LENGTH, blank=True, default="")

    #: The version this run asked the source about, taken from the package's
    #: identity at the moment of the observation. Required of a determinate row --
    #: see the class docstring for why it is stored rather than re-derived.
    matched_version = models.CharField(_("matched version"), max_length=_VERSION_LENGTH, blank=True, default="")

    #: How the source says it matched that version -- see
    #: `collectors/match_confidence.py`, and note that it is emphatically not the
    #: package-identity `confidence` `CPM-AD-4` governs. Required of a determinate
    #: row and blank on every other.
    #:
    #: **It is the source's claim about its own work, and nothing on this row
    #: corroborates it.** The row records the version *this product* asked about
    #: (`matched_version`) and the certainty the source asserted, and it does not
    #: carry what the source actually compared -- so an adapter that matched on
    #: name alone and stated `exact-version` produces a row indistinguishable from
    #: one that did the work. That is a property of a pluggable source and is why
    #: `CPM-FR-17`'s policy pass must *weigh* this column rather than trust it.
    match_confidence = models.CharField(
        _("match confidence"),
        max_length=_MATCH_CONFIDENCE_LENGTH,
        choices=MatchConfidence.choices,
        blank=True,
        default="",
    )

    #: What the collector or the base had to say about this observation -- that
    #: the source was read and matched nothing, that it could not identify the
    #: package, that this package's identity names no version to match against, or
    #: that the source stated no fix.
    detail = models.TextField(_("detail"), blank=True, default="")

    #: The `trace_id` of the task that made this observation, formatted `032x`
    #: (`CPM-AD-15`). Empty when no span was active, which never blocks a write.
    trace_id = models.CharField(_("trace id"), max_length=_TRACE_ID_LENGTH, blank=True, default="")

    class Meta:
        """The table PRD Appendix A.2 names, not the `collectors_vulnerabilityfinding` Django derives.

        **No unique constraint of any kind** (`CPM-AD-2`, `CPM-AD-7`). Two
        observations of one advisory about one package are two rows, and
        idempotency is the run ledger's property rather than this table's. Here
        the tuple that looks unique -- `(package, advisory_id, matched_version)`
        -- is exactly the tuple a re-observation repeats, and a constraint over it
        would turn every second daily run into an `IntegrityError`.
        """

        db_table = "vulnerability_findings"
        verbose_name = _("vulnerability finding")
        verbose_name_plural = _("vulnerability findings")
        indexes = [
            # `core/freshness.py`'s `latest_observation` reads exactly this, on
            # the terms `RELEASE_READ_INDEX` states.
            models.Index(fields=["package", "-observed_at"], name=VULNERABILITY_READ_INDEX),
        ]
        constraints = [
            # The biconditional, and every conjunct is load bearing.
            #
            # A determinate row is a *finding*, and CPM-FR-11 fixes what a
            # finding says: which advisory, what it affects, what version was
            # checked, and how surely. A row missing any of the four is a finding
            # that cannot be acted on or argued with -- and a row missing
            # `match_confidence` in particular is the one CPM-SM-2 measures,
            # because it presents a match whose certainty nobody stated.
            #
            # A row that is *not* determinate and carries any advisory fact is
            # claiming to have matched something the run never matched: on the
            # `unknown` row that is a finding underneath a statement that nothing
            # was found out, and on an `error` or `not_found` row it is a fact
            # about a source that never answered.
            #
            # `severity` and `fixed_range` are absent from the determinate half on
            # purpose -- see the class docstring -- and present in the other half,
            # because a row that established nothing may state neither.
            #
            # `state` is NOT NULL and every column tested here is NOT NULL, so
            # this expression is always true or false and never the third thing a
            # SQL CHECK can be.
            models.CheckConstraint(
                condition=(
                    (
                        models.Q(state=MATCHED)
                        & ~models.Q(advisory_id="")
                        & ~models.Q(affected_range="")
                        & ~models.Q(matched_version="")
                        & ~models.Q(match_confidence="")
                    )
                    | (
                        ~models.Q(state=MATCHED)
                        & models.Q(
                            advisory_id="",
                            severity="",
                            affected_range="",
                            fixed_range="",
                            matched_version="",
                            match_confidence="",
                        )
                    )
                ),
                name=VULNERABILITY_FACTS_CONSTRAINT,
            ),
            # The one value this vocabulary carries and this table may not hold.
            #
            # `not_applicable` arrives in `VulnerabilityOutcome` by construction --
            # `outcome_type` supplies all four sentinels and refuses a type that
            # drops one -- and an advisory question applies to every package, so
            # nothing may ever write it here. `VulnerabilityCollector` refuses it
            # at `sentinel_evidence` and `inapplicability` never answers a reason,
            # but both of those are one writer's rules; this is the table's, and
            # it holds against every writer this product grows. A row carrying it
            # would say the question was never about this package, which is a
            # claim no advisory source and no collector is in a position to make.
            #
            # `state` is NOT NULL, so this expression is always true or false and
            # never the third thing a SQL CHECK can be.
            models.CheckConstraint(
                condition=~models.Q(state=VULNERABILITY_NOT_APPLICABLE),
                name=VULNERABILITY_APPLICABILITY_CONSTRAINT,
            ),
        ]

    def __str__(self) -> str:
        """Return the advisory, the version it was matched against, the state and when it was observed.

        Returns:
            A one-line summary, read off `package_id` rather than off `package`
            for the reason `SourceReleaseSnapshot.__str__` gives: the related
            object of an unsaved instance raises, and a `__str__` that raises
            breaks a debugger and a traceback alike.

        """
        advisory = self.advisory_id or "(no advisory matched)"
        against = self.matched_version or "(no version)"
        scope = "no package" if self.package_id is None else f"package {self.package_id}"
        when = "never" if self.observed_at is None else self.observed_at.isoformat()
        return f"{advisory} against {against} for {scope}: {self.state} at {when}"


class KevFinding(AppendOnlyModel):
    """One cross-reference of one advisory against the KEV catalog. Table `kev_findings`.

    PRD Appendix A.2 gives this table exactly two facts -- "link to the
    vulnerability finding, KEV catalog date added" -- and `CPM-FR-12` is where they
    come from: "a KEV finding links to the vulnerability finding it derives from
    and records the catalog date added".

    **The link is the whole point, and it is a real foreign key.** A row keyed by
    advisory identifier alone would leave a reviewer to re-derive which observation
    it came from, against a table that accumulates several rows per advisory --
    and nothing else in this product would ever create that link. The relation is
    `PROTECT` on the same terms `package` is (`EVIDENCE.02-AUDIT-001`): Django's
    deletion collector bypasses every append-only refusal in `core/models.py`, so
    `CASCADE` here would destroy a KEV observation when the advisory observation it
    derives from went.

    **It is nullable, and the rows that leave it null are the rows that derive from
    no finding** -- the `unknown` one a package with nothing to cross-reference
    gets, and the `error` and `not_found` rows the base writes. Inventing a link to
    an unrelated finding would be worse than leaving it absent, and
    `Meta.constraints` makes that a rule rather than a convention. An `unknown` row
    *may* carry one: a finding whose advisory identifier uses a scheme the catalog
    never states is an advisory the catalog cannot speak to, and that row is about
    one specific advisory and names it.

    **The linked finding must be about this same package, and that is not a check
    constraint** -- because SQL cannot make it one. A `CHECK` is evaluated per row
    against that row's own columns; the predicate here spans two tables, and Django
    offers no joined constraint. The one relational spelling that would work is a
    composite foreign key on `(package, vulnerability_finding)`, which needs a
    `UniqueConstraint` over `(package, id)` on `vulnerability_findings` --
    `EVIDENCE.02-AUDIT-003` bans a unique constraint on an evidence model outright,
    for `CPM-AD-2`'s reasons, and this is not the story to reopen that. So the rule
    is held in three places instead, and each closes a different writer:
    `save()` below refuses the mismatch, which is every write that constructs an
    instance -- `objects.create()` included; `collectors/kev.py` takes the row's
    package *from the finding* rather than from the run, so the collector's own
    `bulk_create` cannot produce one; and raw SQL is closed by
    `EVIDENCE.02-AUDIT-002`. What is left uncovered is a hand-written `bulk_create`
    by some future writer, and it is named here rather than left to be discovered.

    **One row per current advisory, and never one row for several.** Three current
    findings are three cross-references and three rows, each linking to its own
    finding, because a reviewer's question is about one advisory at a time -- is
    *this* one being used against people -- and a row standing for several could
    not answer it for any of them.

    **A package with nothing to cross-reference still gets a row, and it carries
    `unknown`.** `CPM-FR-6` and `CPM-SM-2` again: a package this product has no
    advisory for has not been shown to be free of known-exploited vulnerabilities,
    it has been shown that there was nothing here to ask about. A package with no
    row would read as never-observed, and there would be nothing anywhere
    distinguishing "we cross-referenced and nothing is listed" from "nobody
    looked".

    **The determinate values are `listed` and `not_listed`, and emphatically not
    `ok`.** On this table a determinate row is either an advisory the catalog says
    is being exploited or an advisory it says it is not -- and `core`'s single
    precedence order ranks `ok` best of five while `CPM-AD-24` carries a state's
    value verbatim onto every read surface. A table using `ok` for the listed half
    would render exactly the known-exploited packages as the clean ones, which is
    the correction `CPM-SECURITY-S01` was patched for and which is made here by
    construction. `collectors/outcomes.py` composes the vocabulary and argues both
    values at length.

    **`catalog_date_added` may be blank on a `listed` row, and blank means
    missing** (PRD Appendix A.1). Two things reach that state and `detail` says
    which: the catalog listed the advisory and stated no date, or it stated one
    this collector could not read as an aware instant. Neither is guessed -- a date
    inferred from a catalog's own publication, or a naive value assumed to be UTC,
    would be a permanent claim about when an advisory became known-exploited that
    nobody made (`CPM-AD-26`).

    **Nothing here is ranked, weighed or rolled up.** What a KEV hit *means* for a
    package -- how it ranks against a severity, whether it leads a queue -- is
    `CPM-FR-17`'s policy pass (`CPM-AD-8`), which is `CPM-SECURITY-S04`.

    `observed_at` and `objects` come from `AppendOnlyModel`: the instant is
    supplied by the writer from an injected `Clock` (`CPM-AD-26`) and the manager
    is the one that offers no `update()` and no `delete()` (`CPM-AD-2`).
    """

    #: The package this observation is about, by the integer primary key
    #: `CPM-AD-3` fixes. Non-nullable: an observation is always about a package.
    #:
    #: Carried even on a row that also names a vulnerability finding, which names
    #: the same package. Not redundant: every read of this table is per package and
    #: newest-first (`core/freshness.py`), and reaching the package through the
    #: nullable link would make that read a join that no row without a link could
    #: satisfy at all.
    package = models.ForeignKey(
        Package,
        on_delete=models.PROTECT,
        related_name="kev_findings",
        verbose_name=_("package"),
    )

    #: The vulnerability finding this cross-reference derives from -- AC 1's link,
    #: as a foreign key rather than as a copied identifier. NULL exactly on a row
    #: that derives from no finding; see the class docstring and `Meta.constraints`.
    vulnerability_finding = models.ForeignKey(
        VulnerabilityFinding,
        on_delete=models.PROTECT,
        related_name="kev_findings",
        null=True,
        blank=True,
        default=None,
        verbose_name=_("vulnerability finding"),
    )

    #: Where this observation came from -- the locator the declared KEV source
    #: adapter recorded for the catalog it served, or the locator this run asked
    #: about where no catalog answer was read. It is what makes the source pluggable
    #: without the policy layer learning which one is active (`CPM-AD-29`): a policy
    #: reads this column like any other evidence's.
    source = models.CharField(_("source"), max_length=_LOCATOR_LENGTH, blank=True, default="")

    #: What the cross-reference concluded, over `KevOutcome` and emitted verbatim
    #: (`CPM-AD-24`). See the class docstring for what each value means here, and
    #: `collectors/outcomes.py` for why neither determinate value is `ok`.
    state = models.CharField(_("state"), max_length=_STATE_LENGTH, choices=KevOutcome.choices)

    #: When the catalog says it added the advisory -- AC 1's second fact, as the
    #: catalog stated it. NULL on every row that is not `listed`, and NULL on a
    #: `listed` row whose catalog stated no date or stated one this collector could
    #: not read; `detail` says which.
    catalog_date_added = models.DateTimeField(_("catalog date added"), null=True, blank=True, default=None)

    #: What the collector or the base had to say about this observation -- that the
    #: catalog does not list this advisory, that it listed it and stated no date,
    #: that it stated a date that could not be read, or that this package had no
    #: current vulnerability finding to cross-reference at all.
    detail = models.TextField(_("detail"), blank=True, default="")

    #: The `trace_id` of the task that made this observation, formatted `032x`
    #: (`CPM-AD-15`). Empty when no span was active, which never blocks a write.
    trace_id = models.CharField(_("trace id"), max_length=_TRACE_ID_LENGTH, blank=True, default="")

    class Meta:
        """The table PRD Appendix A.2 names, not the `collectors_kevfinding` Django derives.

        **No unique constraint of any kind** (`CPM-AD-2`, `CPM-AD-7`). Two
        cross-references of one advisory are two rows, and idempotency is the run
        ledger's property rather than this table's. Here the tuple that looks
        unique -- `(package, vulnerability_finding)` -- is exactly the tuple a
        re-observation repeats, every day, for as long as that finding stays the
        current one for its advisory.
        """

        db_table = "kev_findings"
        verbose_name = _("KEV finding")
        verbose_name_plural = _("KEV findings")
        indexes = [
            # `core/freshness.py`'s `latest_observation` reads exactly this, on
            # the terms `VULNERABILITY_READ_INDEX` states. Django's automatic
            # foreign-key index covers the filter alone and leaves the sort to a
            # scan of that package's whole cross-reference history, which grows
            # daily -- and grows once per current advisory rather than once per
            # package, so it is the faster-growing history of the two.
            models.Index(fields=["package", "-observed_at"], name=KEV_READ_INDEX),
        ]
        constraints = [
            # The biconditional, and every conjunct is load bearing.
            #
            # A determinate row is a *cross-reference*, and CPM-FR-12 fixes what
            # one says: which vulnerability finding it derives from, and -- where
            # the catalog listed the advisory -- the date it was added. A
            # determinate row with no link is the row AC 1 exists to forbid: a
            # claim about an advisory nothing ties to the observation that found
            # it, in a table nothing may correct.
            #
            # An `unknown` row may carry a link or not, and the two are different
            # facts rather than a loosened rule. One is about a *specific*
            # advisory the catalog cannot speak to -- it states no identifier in
            # that advisory's scheme, so its silence establishes nothing -- and it
            # names which advisory, because a reader has to know. The other is a
            # package with nothing to cross-reference at all, which derives from no
            # finding and names none.
            #
            # An `error` or `not_found` row carries no link, because the catalog
            # never answered: a link there would be a fact about a document that
            # does not exist.
            #
            # A catalog date is permitted only on a `listed` row, and that half is
            # the one a later edit is likeliest to lose. A date on a `not_listed`
            # row would say the catalog both does and does not list the advisory;
            # a date on an `unknown` or sentinel row would be a date for an
            # advisory nothing named.
            #
            # `state` is NOT NULL and an `IS NULL` test is never itself NULL, so
            # this expression is always true or false and never the third thing a
            # SQL CHECK can be.
            models.CheckConstraint(
                condition=(
                    (models.Q(state=LISTED) & models.Q(vulnerability_finding__isnull=False))
                    | (
                        models.Q(state=NOT_LISTED)
                        & models.Q(vulnerability_finding__isnull=False, catalog_date_added__isnull=True)
                    )
                    | (models.Q(state=KEV_UNKNOWN) & models.Q(catalog_date_added__isnull=True))
                    | (
                        ~models.Q(state=LISTED)
                        & ~models.Q(state=NOT_LISTED)
                        & ~models.Q(state=KEV_UNKNOWN)
                        & models.Q(vulnerability_finding__isnull=True, catalog_date_added__isnull=True)
                    )
                ),
                name=KEV_FACTS_CONSTRAINT,
            ),
            # The one value this vocabulary carries and this table may not hold,
            # on the terms `VULNERABILITY_APPLICABILITY_CONSTRAINT` states: it
            # arrives in `KevOutcome` by construction because `outcome_type`
            # supplies all four sentinels and refuses a type that drops one, and a
            # KEV question applies to every package that could have an advisory
            # against it -- which is every package. `KevCollector` refuses it at
            # `sentinel_evidence` and `inapplicability` never answers a reason, but
            # both of those are one writer's rules; this is the table's, and it
            # holds against every writer this product ever grows.
            #
            # `state` is NOT NULL, so this expression is always true or false and
            # never the third thing a SQL CHECK can be.
            models.CheckConstraint(
                condition=~models.Q(state=KEV_NOT_APPLICABLE),
                name=KEV_APPLICABILITY_CONSTRAINT,
            ),
        ]

    def save(
        self,
        *args: object,
        **kwargs: object,
    ) -> None:
        """Insert the cross-reference, refusing one that names another package's observation.

        The rule the class docstring argues cannot be a check constraint, held at
        the one place every write that constructs an instance passes -- which is
        `objects.create()` and every hand-written `save()`. It costs one primary-key
        read, and only on a row that carries a link.

        Args:
            args: Passed to `AppendOnlyModel.save`, which refuses a positional
                caller by name. Not read here.
            kwargs: Passed to `AppendOnlyModel.save` unchanged.

        Raises:
            AppendOnlyError: When the linked vulnerability finding is about a
                different package. Raised as the base's own error rather than a new
                class, because it is the same kind of defect every other refusal on
                this write path is: a row that would misrepresent an observation,
                permanently, in a table nothing may correct.

        """
        if self.vulnerability_finding_id is not None:
            about = (
                VulnerabilityFinding.objects.filter(pk=self.vulnerability_finding_id)
                .values_list("package_id", flat=True)
                .first()
            )
            if about is not None and about != self.package_id:
                message = (
                    f"this kev_findings row is about package {self.package_id} and links to vulnerability "
                    f"finding {self.vulnerability_finding_id}, which is an observation of package {about}. A "
                    f"cross-reference derives from an advisory recorded against the package it is about "
                    f"(CPM-FR-12); a row pairing two would say the catalog listed an advisory against a package "
                    f"nobody matched it to."
                )
                raise AppendOnlyError(message, model_label="collectors.KevFinding", pk=self.pk)
        super().save(*args, **kwargs)  # type: ignore[arg-type]

    def __str__(self) -> str:
        """Return the finding this derives from, the state and when it was observed.

        Returns:
            A one-line summary, read off `vulnerability_finding_id` and
            `package_id` rather than off the related objects, for the reason
            `VulnerabilityFinding.__str__` gives: the related object of an unsaved
            instance raises, and a `__str__` that raises breaks a debugger and a
            traceback alike.

        """
        derives = (
            "no vulnerability finding"
            if self.vulnerability_finding_id is None
            else f"vulnerability finding {self.vulnerability_finding_id}"
        )
        scope = "no package" if self.package_id is None else f"package {self.package_id}"
        when = "never" if self.observed_at is None else self.observed_at.isoformat()
        return f"{derives} for {scope}: {self.state} at {when}"


class LicenseFinding(AppendOnlyModel, FindingKeyed):
    """One channel's statement of one package's licence, raw and normalized. Table `license_findings`.

    PRD Appendix A.2 names this table and `CPM-FR-13` is where its facts come
    from: the licence a monitored channel states, the SPDX expression this product
    normalized it to, and the method that did it.

    **The raw string and the normalized expression sit side by side, and that is
    the whole of AC 1.** A compliance reviewer's question is "what did
    normalization do before I trust its result", and it is only answerable if both
    columns survive. It matters most on the rows that *failed*: an `unknown` row
    carrying the raw string is a review item somebody can act on, while an
    `unknown` row carrying nothing is an absence of information.

    **So the constraint is deliberately asymmetric.**
    `license_facts_present_exactly_when_normalized` requires the expression and
    the method on a determinate row and forbids them everywhere else, and says
    nothing at all about `raw_license` -- which is permitted on every row there is.
    See `LICENSE_FACTS_CONSTRAINT`.

    **The raw value is recorded verbatim, exactly as the source stated it.**
    Stripped of surrounding whitespace and refused if it is wider than its column
    or carries a control character -- and otherwise untouched. Nothing here
    lower-cases it, expands it, or rewrites it toward the identifier it was
    normalized to: the two columns are only worth having if one of them is the
    source's own words.

    **`unknown` is what an unrecognised licence records, and never a permissive
    value.** `CPM-SECURITY-S03`'s AC 2 says so in as many words -- "records
    `unknown` and routes to manual review, never `allowed`" -- and `CPM-SM-2`
    measures this product on zero findings presenting an unknown as clean. Two
    things reach it and `detail` says which: the channel stated no licence at all,
    or it stated something `collectors/spdx.py` will not normalize without
    guessing.

    **There is no compliance verdict on this table and no column for one.** PRD
    Appendix A.2 lists a policy result here; a collector may not compute a derived
    status (`CPM-AD-8`) and a policy pass writes only its own per-domain derived
    table (`CPM-AD-21`), so no component this architecture permits could write that
    column. `CPM-SECURITY-S05` owns the licence policy and will write its own
    table. `CPM-SECURITY-S03`'s Spec Change Log records the column as not-taken
    rather than smuggled onto an evidence row.

    **The determinate value is `normalized` and emphatically not `ok`.** On a
    licence table `ok` reads as "this licence is fine", which is exactly the
    verdict the paragraph above says nothing here may reach -- and `CPM-AD-24`
    carries a state's value verbatim onto every read surface while `core`'s single
    precedence order ranks `ok` best of five. `collectors/outcomes.py` composes the
    vocabulary and argues the choice at length.

    **`channel` is required of every row, sentinel rows included.** Several
    monitored channels may state different licences for one package, and each
    states its own: one row per channel, never merged into a verdict, on the terms
    `CondaPackageSnapshot` keeps its pairs apart. Disagreement is a fact to record.

    **Nothing here is compared, ranked or resolved.** Which of two disagreeing
    channels is right, and whether either licence is acceptable, are judgements
    this collector does not make.

    `observed_at` and `objects` come from `AppendOnlyModel`: the instant is
    supplied by the writer from an injected `Clock` (`CPM-AD-26`) and the manager
    is the one that offers no `update()` and no `delete()` (`CPM-AD-2`).
    """

    #: What makes a licence question *this* licence question: the normalised licence
    #: this package was found to carry, on the channel it was found on.
    #:
    #: **`raw_license` is not part of it.** Two spellings of one licence -- `Apache
    #: 2.0` and `Apache-2.0` -- are the same compliance question, and keying on the
    #: raw form would open a second review item the day a source tidied its metadata.
    #: Normalising is what `detection_method` records having done, and the normal
    #: form is what a reviewer decided about.
    #:
    #: The channel *is* part of it: the same package on two channels can carry two
    #: licences, and that is two questions rather than one.
    FINDING_KEY_FIELDS: ClassVar[tuple[str, ...]] = ("normalized_license", "channel")

    #: The package this observation is about, by the integer primary key
    #: `CPM-AD-3` fixes. Non-nullable: an observation is always about a package.
    package = models.ForeignKey(
        Package,
        on_delete=models.PROTECT,
        related_name="license_findings",
        verbose_name=_("package"),
    )

    #: The locator this observation was read from -- the channel's own package
    #: document. Recorded on the row for the reason `CondaPackageSnapshot.source`
    #: is: a run reads one locator per channel and writes one row per channel, so
    #: `source` is what ties each row to the answer it came from rather than to
    #: the one the run happened to start with.
    source = models.CharField(_("source"), max_length=_LOCATOR_LENGTH, blank=True, default="")

    #: What the observation concluded, over `LicenseOutcome` and emitted verbatim
    #: (`CPM-AD-24`). See the class docstring for what each value means here, and
    #: `collectors/outcomes.py` for why the determinate value is not `ok`.
    state = models.CharField(_("state"), max_length=_STATE_LENGTH, choices=LicenseOutcome.choices)

    #: The channel this row is about, as the operator declared it and lower-cased.
    #: Required of every row -- see `Meta.constraints`.
    channel = models.CharField(_("channel"), max_length=_CHANNEL_LENGTH)

    #: The licence exactly as the channel stated it, on **every** row that has one
    #: -- including the rows normalization refused. Blank means the source stated
    #: none, or that no answer was read at all; `detail` says which.
    raw_license = models.CharField(_("raw license"), max_length=_RAW_LICENSE_LENGTH, blank=True, default="")

    #: The SPDX expression the raw string was normalized to. Present exactly on a
    #: determinate row -- see `Meta.constraints`. Blank means missing (PRD Appendix
    #: A.1) and never "no restrictions".
    normalized_license = models.CharField(
        _("normalized license"),
        max_length=_NORMALIZED_LICENSE_LENGTH,
        blank=True,
        default="",
    )

    #: How the expression was established, over `DetectionMethod`. Present exactly
    #: on a determinate row, for the reason the expression is: a normalized value
    #: nobody can say how this product arrived at is the half of `CPM-SM-2` that
    #: makes a finding argue-able.
    detection_method = models.CharField(
        _("detection method"),
        max_length=_DETECTION_METHOD_LENGTH,
        choices=DetectionMethod.choices,
        blank=True,
        default="",
    )

    #: What the collector or the base had to say about this observation -- that the
    #: channel stated no licence, that it stated one this product will not normalize
    #: without guessing and which part of it stopped, or why one channel's answer
    #: could not be read while another's was.
    detail = models.TextField(_("detail"), blank=True, default="")

    #: The `trace_id` of the task that made this observation, formatted `032x`
    #: (`CPM-AD-15`). Empty when no span was active, which never blocks a write.
    trace_id = models.CharField(_("trace id"), max_length=_TRACE_ID_LENGTH, blank=True, default="")

    class Meta:
        """The table PRD Appendix A.2 names, not the `collectors_licensefinding` Django derives.

        **No unique constraint of any kind** (`CPM-AD-2`, `CPM-AD-7`). Two
        observations of one package's licence on one channel are two rows, and
        idempotency is the run ledger's property rather than this table's. Here
        the tuple that looks unique -- `(package, channel, raw_license)` -- is
        exactly the tuple a re-observation repeats, every day, for as long as the
        channel keeps stating the same licence.
        """

        db_table = "license_findings"
        verbose_name = _("license finding")
        verbose_name_plural = _("license findings")
        indexes = [
            # `core/freshness.py`'s `latest_observation` reads exactly this, on
            # the terms `KEV_READ_INDEX` states. One index rather than the two
            # `conda_package_snapshots` carries: this table grows once per
            # *channel* rather than once per `channel x platform`, and no read
            # this story adds is per channel -- a reviewer's question is about a
            # package and the channels are what one answer holds.
            models.Index(fields=["package", "-observed_at"], name=LICENSE_READ_INDEX),
        ]
        constraints = [
            # The biconditional, and it is deliberately asymmetric -- see
            # LICENSE_FACTS_CONSTRAINT and the class docstring.
            #
            # A determinate row is a *normalization*, and CPM-FR-13 fixes what one
            # says: the SPDX expression, and the method that produced it. A row
            # missing either is a normalized licence nobody can check.
            #
            # A row that is not determinate and carries an expression or a method
            # is claiming a normalization the run never performed: on the `unknown`
            # row that is an SPDX expression underneath a statement that the
            # licence was not recognised, and on an `error` or `not_found` row it
            # is a fact about a channel that never answered.
            #
            # `raw_license` appears in neither half, and that is the load-bearing
            # omission: the raw string is permitted on every row, because an
            # `unknown` row carrying it is the review item AC 2 asks for.
            #
            # `state` is NOT NULL and every column tested here is NOT NULL, so
            # this expression is always true or false and never the third thing a
            # SQL CHECK can be.
            models.CheckConstraint(
                condition=(
                    (models.Q(state=NORMALIZED) & ~models.Q(normalized_license="") & ~models.Q(detection_method=""))
                    | (~models.Q(state=NORMALIZED) & models.Q(normalized_license="", detection_method=""))
                ),
                name=LICENSE_FACTS_CONSTRAINT,
            ),
            # The one value this vocabulary carries and this table may not hold, on
            # the terms `KEV_APPLICABILITY_CONSTRAINT` states: it arrives in
            # `LicenseOutcome` by construction because `outcome_type` supplies all
            # four sentinels and refuses a type that drops one, and every package a
            # monitored channel could serve is licensed under something.
            # `LicenseCollector` refuses it at `sentinel_evidence` and
            # `inapplicability` never answers a reason, but both of those are one
            # writer's rules; this is the table's, and it holds against every
            # writer this product ever grows.
            models.CheckConstraint(
                condition=~models.Q(state=LICENSE_NOT_APPLICABLE),
                name=LICENSE_APPLICABILITY_CONSTRAINT,
            ),
            # Every row names the channel it is about, including the sentinel rows
            # the base writes -- the half a convention would have missed. A blank
            # channel is a row that has merged every monitored channel into one,
            # which is precisely the merge this table exists to prevent: two
            # channels stating different licences are two facts.
            models.CheckConstraint(
                condition=~models.Q(channel=""),
                name=LICENSE_CHANNEL_CONSTRAINT,
            ),
        ]

    def __str__(self) -> str:
        """Return the licence as stated, the channel, the state and when it was observed.

        Returns:
            A one-line summary, read off `package_id` rather than off `package`
            for the reason `KevFinding.__str__` gives: the related object of an
            unsaved instance raises, and a `__str__` that raises breaks a debugger
            and a traceback alike.

        """
        stated = self.raw_license or "(no license stated)"
        where = self.channel or "(no channel)"
        scope = "no package" if self.package_id is None else f"package {self.package_id}"
        when = "never" if self.observed_at is None else self.observed_at.isoformat()
        return f"{stated} on {where} for {scope}: {self.state} at {when}"


class PythonReadinessAssessment(AppendOnlyModel):
    """What a package's *declared* metadata says about one Python series. Table `python_readiness_assessments`.

    `CPM-FR-14` splits Python readiness into a cheap static pass and an expensive
    verification pass, and `CPM-PY314-S01` is the cheap one. This table holds what a
    project's published metadata **claims**, and nothing whatever about what a build
    did: `CPM-PY314-S02` writes that, on its own queue, into its own table.

    **The determinate values name the inference, and that is `CPM-FR-14`'s
    "distinct recorded states" made structural.** `inferred_compatible` and
    `inferred_incompatible`, never `ok` and never a bare `compatible` --
    `CPM-AD-24` carries a state's value verbatim onto every read surface, so a value
    called `compatible` here would sit on a queue beside a verified result and read
    identically. `collectors/outcomes.py` composes the vocabulary and argues both
    halves.

    **A metadata silence is `unknown` and never a claim of incompatibility.** This
    is the single property `CPM-PY314-S01` turns on. Most projects have not declared
    3.14 support; if "declares nothing" were recorded as "excludes it", this table
    would report most of an inventory as incompatible on no evidence and send
    `CPM-PY314-S02`'s expensive verification exactly where it is least warranted --
    the opposite of what the story exists to do. So three outcomes rather than two:
    the metadata admits the series, the metadata cannot admit it, or the metadata
    said nothing either way.

    **`not_applicable` has exactly one path to it, and `detail` has to name it.**
    `CPM-PY314-S01` AC 2 asks for the state, and the only thing that can establish it
    is `identity`: a `release_ecosystem` mapping resolution recorded
    `not_applicable`, which says the package has no release ecosystem a Python
    question could be asked of. A mapping that is `unknown`, `error` or `not_found`
    establishes **nothing** and never reaches this state -- reading an unresolved
    identity as an inapplicable question is a determinate claim made from an
    absence, which is the defect class the preceding epic met in every story.
    `READINESS_REASON_CONSTRAINT` is the database saying so.

    **The specifier is stored verbatim, and it is permitted on every row.** An
    `inferred_incompatible` row is a claim about somebody's package, and the only
    thing that makes it argue-able is the specifier that produced it sitting beside
    it; an `unknown` row carrying a specifier this product would not read is a review
    item somebody can act on. So the signal constraint is asymmetric on the terms
    `license_findings`' is: it governs the deciding signal and says nothing about
    the two transcribed columns.

    **Two static signals, and their disagreement is recorded rather than
    resolved.** `requires_python` is what the project declared as a range and
    `matching_classifier` is the classifier that names the series, if it declared
    one. A classifier list is positive-only, so its silence excludes nothing -- but a
    project whose specifier admits the series while its classifiers enumerate
    Python versions without naming it has said two different things, and this table
    records that as the `unknown` it is rather than picking a winner.

    **Nothing here is a derived status.** Whether a package is *ready* is
    `CPM-FR-19`'s readiness policy (`CPM-PY314-S03`), which reads this table and
    `CPM-PY314-S02`'s beside it and writes its own derived table (`CPM-AD-8`,
    `CPM-AD-21`).

    `observed_at` and `objects` come from `AppendOnlyModel`: the instant is
    supplied by the writer from an injected `Clock` (`CPM-AD-26`) and the manager
    is the one that offers no `update()` and no `delete()` (`CPM-AD-2`).
    """

    #: The package this observation is about, by the integer primary key
    #: `CPM-AD-3` fixes. Non-nullable: an observation is always about a package.
    package = models.ForeignKey(
        Package,
        on_delete=models.PROTECT,
        related_name="python_readiness_assessments",
        verbose_name=_("package"),
    )

    #: The locator this observation was read from -- the release ecosystem's own
    #: project document. Blank on the rows no locator was built for, which is the
    #: `not_applicable` path: identity said there is no ecosystem to ask.
    source = models.CharField(_("source"), max_length=_LOCATOR_LENGTH, blank=True, default="")

    #: What the assessment concluded, over `PythonReadinessOutcome` and emitted
    #: verbatim (`CPM-AD-24`). See the class docstring for what each value means,
    #: and `collectors/outcomes.py` for why the determinate values name inference.
    state = models.CharField(_("state"), max_length=_STATE_LENGTH, choices=PythonReadinessOutcome.choices)

    #: The Python series this row assessed, dotted -- `3.14`. Required of every
    #: row, sentinel rows included: a later story writes a verified result about a
    #: series and a policy reduces both, and a row that could not say which Python
    #: it was about would make the two indistinguishable.
    python_series = models.CharField(_("python series"), max_length=_PYTHON_SERIES_LENGTH)

    #: The `Requires-Python` specifier the project declared, **exactly as stated**
    #: and stripped only of the whitespace around it. Blank means the project
    #: declared none, which is a silence rather than a range. Permitted on every
    #: row -- see the class docstring.
    requires_python = models.CharField(
        _("requires python"),
        max_length=_REQUIRES_PYTHON_LENGTH,
        blank=True,
        default="",
    )

    #: The version classifier naming this series, exactly as the project stated it,
    #: or blank where it declared none. Blank means missing (PRD Appendix A.1) and
    #: never "the project denies this version": a classifier list is positive-only.
    matching_classifier = models.CharField(
        _("matching classifier"),
        max_length=_CLASSIFIER_LENGTH,
        blank=True,
        default="",
    )

    #: Which declared signal reached the verdict, over `DecidingSignal`. Present
    #: exactly on a determinate row -- see `Meta.constraints`. An inference that
    #: cannot say what it inferred from is a claim with no argument attached.
    deciding_signal = models.CharField(
        _("deciding signal"),
        max_length=_DECIDING_SIGNAL_LENGTH,
        choices=DecidingSignal.choices,
        blank=True,
        default="",
    )

    #: What the collector or the base had to say -- which silence an `unknown` row
    #: is, why a specifier was not read, what the two signals disagreed about, or
    #: what `identity` established for a `not_applicable` row.
    detail = models.TextField(_("detail"), blank=True, default="")

    #: The `trace_id` of the task that made this observation, formatted `032x`
    #: (`CPM-AD-15`). Empty when no span was active, which never blocks a write.
    trace_id = models.CharField(_("trace id"), max_length=_TRACE_ID_LENGTH, blank=True, default="")

    class Meta:
        """The table `CPM-PY314-S01` adds, not the `collectors_pythonreadinessassessment` Django derives.

        **No unique constraint of any kind** (`CPM-AD-2`, `CPM-AD-7`). A project
        that declares 3.14 support later is a new row and the old one stands, and
        the tuple that looks unique -- `(package, python_series)` -- is exactly the
        tuple a re-observation repeats every day.
        """

        db_table = "python_readiness_assessments"
        verbose_name = _("python readiness assessment")
        verbose_name_plural = _("python readiness assessments")
        indexes = [
            # `core/freshness.py`'s `latest_observation` reads exactly this, on the
            # terms `LICENSE_READ_INDEX` states. One index and not two: this table
            # grows once per package per run, and the reads this story adds are all
            # about a package.
            models.Index(fields=["package", "-observed_at"], name=READINESS_READ_INDEX),
        ]
        constraints = [
            # The biconditional, over the one column that is a judgement rather
            # than a transcription. A determinate row says which declared signal
            # decided it; a row that is not determinate decided nothing and may not
            # name one.
            #
            # `requires_python` and `matching_classifier` appear in neither half on
            # purpose: they are what the source stated, and an `unknown` row
            # carrying them is the review item this table exists to leave behind.
            #
            # `state` is NOT NULL and every column tested here is NOT NULL, so this
            # expression is always true or false and never the third thing a SQL
            # CHECK can be.
            models.CheckConstraint(
                condition=(
                    (models.Q(state__in=(INFERRED_COMPATIBLE, INFERRED_INCOMPATIBLE)) & ~models.Q(deciding_signal=""))
                    | (~models.Q(state__in=(INFERRED_COMPATIBLE, INFERRED_INCOMPATIBLE)) & models.Q(deciding_signal=""))
                ),
                name=READINESS_SIGNAL_CONSTRAINT,
            ),
            # The opposite of the three security tables' applicability rule, and
            # the one place this table differs from all of them: `not_applicable`
            # is a state CPM-PY314-S01 AC 2 asks for, so it is permitted -- and it
            # is permitted only with a reason. The sole thing that can establish it
            # is identity's own `not_applicable` mapping, so a row that carried the
            # state and said nothing would be indistinguishable from one written
            # out of an *absence* of identity, which is precisely what this story
            # forbids.
            models.CheckConstraint(
                condition=~models.Q(state=READINESS_NOT_APPLICABLE) | ~models.Q(detail=""),
                name=READINESS_REASON_CONSTRAINT,
            ),
            # Every row names the series it assessed, sentinel rows included, on
            # the terms `license_findings` requires its channel.
            models.CheckConstraint(
                condition=~models.Q(python_series=""),
                name=READINESS_SERIES_CONSTRAINT,
            ),
        ]

    def __str__(self) -> str:
        """Return what this row records, for an admin list and a debugger.

        Returns:
            The series, the state and the instant, with the package it is about.

        """
        scope = "no package" if self.package_id is None else f"package {self.package_id}"
        when = "never" if self.observed_at is None else self.observed_at.isoformat()
        series = self.python_series or "(no series)"
        return f"Python {series} for {scope}: {self.state} at {when}"


class PythonVerificationResult(AppendOnlyModel):
    """What a build and an import actually did under one Python series. Table `python_verification_results`.

    `CPM-FR-14` splits Python readiness into a cheap static pass and an expensive
    verification pass, and `CPM-PY314-S02` is the expensive one. This table holds
    what an **execution** did, and nothing whatever about what a project claimed:
    `python_readiness_assessments` holds that, written by a different collector, on
    a different queue, into a different table with a different vocabulary.

    **The determinate values name proof, and that is `CPM-FR-14`'s "distinct
    recorded states" made structural from the other side.** `verified_compatible`
    and `verification_failed`, never `ok` and never a bare `compatible` --
    `CPM-AD-24` carries a state's value verbatim onto every read surface, so a
    value called `compatible` here would sit on a queue beside
    `inferred_compatible` and read as the same kind of answer.
    `collectors/outcomes.py` composes the vocabulary and argues why the negative
    verdict is `verification_failed` rather than `verified_incompatible`.

    **Every determinate row says where it ran, and the database enforces it.**
    `CPM-PY314-S02`'s AC 1 requires the platform, the architecture and a log
    reference, and `VERIFICATION_EVIDENCE_CONSTRAINT` refuses a determinate row
    missing any of the three. This is the one table in this module whose facts are
    about the **observer** rather than about the package: a build succeeds on a
    platform, and a row that said "it builds" without saying where would be a claim
    about every platform made from an execution on one.

    **A failed build is a result and not an error.** The two are separate states
    with a column between them: `verification_failed` means the backend ran and the
    build did not come out, and it carries its log reference like any other
    determinate row; `error` means the backend itself raised, so nothing was
    verified and there is nothing to open. Folding the first into the second would
    lose the row an engineer most wants to read.

    **`not_applicable` has exactly one path to it**, and it is
    `python_readiness_assessments`' path: `identity` recorded this package's
    release-ecosystem mapping as `not_applicable`. A mapping that is `unknown`,
    `error` or `not_found` establishes **nothing** and never reaches this state.
    `VERIFICATION_REASON_CONSTRAINT` is the database saying so.

    **Most packages have no row here at all, permanently and on purpose.**
    `CPM-PY314-S02`'s AC 3 makes verification a triggered capability that is never
    swept across the inventory, so an absence here is the ordinary state rather than
    a gap. `core/freshness.py` reports such a package `unknown` for want of an
    observation, which is the honest answer: nobody has built it.

    **Nothing here is a derived status.** Whether a package is *ready*, and which
    kind of evidence produced that answer, is `CPM-FR-19`'s readiness policy
    (`CPM-PY314-S03`), which reads this table and the static one beside it and
    writes its own (`CPM-AD-8`, `CPM-AD-21`).

    `observed_at` and `objects` come from `AppendOnlyModel`: the instant is
    supplied by the writer from an injected `Clock` (`CPM-AD-26`) and the manager
    is the one that offers no `update()` and no `delete()` (`CPM-AD-2`).
    """

    #: The package this verification was about, by the integer primary key
    #: `CPM-AD-3` fixes. Non-nullable: an execution is always about a package.
    package = models.ForeignKey(
        Package,
        on_delete=models.PROTECT,
        related_name="python_verification_results",
        verbose_name=_("package"),
    )

    #: The locator handed to the execution backend, which names the package and
    #: never a host: which backend runs a build is a declared adapter and the
    #: locator's job is to name what was asked about (`CPM-AD-29`). Blank on the
    #: rows no locator was built for, which is the `not_applicable` path.
    source = models.CharField(_("source"), max_length=_LOCATOR_LENGTH, blank=True, default="")

    #: What the execution concluded, over `PythonVerificationOutcome` and emitted
    #: verbatim (`CPM-AD-24`). See the class docstring for what each value means,
    #: and `collectors/outcomes.py` for why the determinate values name proof.
    state = models.CharField(_("state"), max_length=_STATE_LENGTH, choices=PythonVerificationOutcome.choices)

    #: The Python series this row verified, dotted -- `3.14`. Required of every row,
    #: sentinel rows included, on the terms `python_readiness_assessments` requires
    #: its own: `CPM-PY314-S03` reduces both tables, and a row that could not say
    #: which Python it built against would make the two indistinguishable.
    python_series = models.CharField(_("python series"), max_length=_PYTHON_SERIES_LENGTH)

    #: The platform the execution ran on, exactly as the backend reported it.
    #: Present on every determinate row and blank on every other, by
    #: `VERIFICATION_EVIDENCE_CONSTRAINT`. Never inferred from the runner this
    #: process happens to be on: what a row records is where the *build* ran, and
    #: the only thing that knows that is the backend that ran it.
    platform = models.CharField(
        _("platform"),
        max_length=_VERIFICATION_PLATFORM_LENGTH,
        blank=True,
        default="",
    )

    #: The architecture the execution ran on, exactly as the backend reported it.
    #: A second column rather than half of the platform string, because
    #: `CPM-PY314-S02`'s AC 1 names them separately and a reader filtering "what
    #: have we proved on aarch64" should not be parsing a compound.
    architecture = models.CharField(
        _("architecture"),
        max_length=_ARCHITECTURE_LENGTH,
        blank=True,
        default="",
    )

    #: Where the log of this execution can be read, exactly as the backend stated
    #: it -- a URL, an object-store key, a run identifier. Present on every
    #: determinate row and blank on every other.
    #:
    #: **Stored verbatim and never truncated.** A truncated reference resolves to
    #: nothing, so a row carrying one would claim evidence a reader cannot open;
    #: a reference too wide for this column is refused where it enters.
    log_reference = models.CharField(
        _("log reference"),
        max_length=_LOG_REFERENCE_LENGTH,
        blank=True,
        default="",
    )

    #: What the backend or the base had to say -- why a build failed, what the
    #: backend raised, or what `identity` established for a `not_applicable` row.
    detail = models.TextField(_("detail"), blank=True, default="")

    #: The `trace_id` of the task that ran this verification, formatted `032x`
    #: (`CPM-AD-15`). Empty when no span was active, which never blocks a write.
    trace_id = models.CharField(_("trace id"), max_length=_TRACE_ID_LENGTH, blank=True, default="")

    class Meta:
        """The table `CPM-PY314-S02` adds, not the `collectors_pythonverificationresult` Django derives.

        **No unique constraint of any kind** (`CPM-AD-2`, `CPM-AD-7`). Verifying a
        package a second time is a new row and the old one stands -- which is how a
        reader sees that a package that failed in June builds in September, and is
        the matrix row saying a second trigger does not replace the first.
        """

        db_table = "python_verification_results"
        verbose_name = _("python verification result")
        verbose_name_plural = _("python verification results")
        indexes = [
            # `core/freshness.py`'s `latest_observation` reads exactly this, on the
            # terms `READINESS_READ_INDEX` states. One index and not two: this
            # table grows only when somebody triggers a verification, and every
            # read this story adds is about a package.
            models.Index(fields=["package", "-observed_at"], name=VERIFICATION_READ_INDEX),
        ]
        constraints = [
            # AC 1 as a database rule: a determinate row names where it ran, all
            # three parts of it, and a row that is not determinate names none of
            # them.
            #
            # One biconditional over three columns rather than three rules, for
            # the reason VERIFICATION_EVIDENCE_CONSTRAINT's comment gives: "says
            # where it ran" is one fact, and a row holding two of the three parts
            # has not said it.
            #
            # `state` is NOT NULL and every column tested here is NOT NULL, so this
            # expression is always true or false and never the third thing a SQL
            # CHECK can be.
            models.CheckConstraint(
                condition=(
                    (
                        models.Q(state__in=(VERIFIED_COMPATIBLE, VERIFICATION_FAILED))
                        & ~models.Q(platform="")
                        & ~models.Q(architecture="")
                        & ~models.Q(log_reference="")
                    )
                    | (
                        ~models.Q(state__in=(VERIFIED_COMPATIBLE, VERIFICATION_FAILED))
                        & models.Q(platform="")
                        & models.Q(architecture="")
                        & models.Q(log_reference="")
                    )
                ),
                name=VERIFICATION_EVIDENCE_CONSTRAINT,
            ),
            # `python_readiness_assessments`' reason rule, reached for the same
            # reason: the sole thing that can establish `not_applicable` is
            # identity's own `not_applicable` mapping, so a row that carried the
            # state and said nothing would be indistinguishable from one written
            # out of an *absence* of identity.
            models.CheckConstraint(
                condition=~models.Q(state=VERIFICATION_NOT_APPLICABLE) | ~models.Q(detail=""),
                name=VERIFICATION_REASON_CONSTRAINT,
            ),
            # Every row names the series it verified, sentinel rows included, on
            # the terms `python_readiness_assessments` requires its own.
            models.CheckConstraint(
                condition=~models.Q(python_series=""),
                name=VERIFICATION_SERIES_CONSTRAINT,
            ),
        ]

    def __str__(self) -> str:
        """Return what this row records, for an admin list and a debugger.

        Returns:
            The series, the state, where it ran and the instant, with the package
            it is about.

        """
        scope = "no package" if self.package_id is None else f"package {self.package_id}"
        when = "never" if self.observed_at is None else self.observed_at.isoformat()
        series = self.python_series or "(no series)"
        where = f"{self.platform}/{self.architecture}" if self.platform and self.architecture else "(nowhere named)"
        return f"Python {series} for {scope} on {where}: {self.state} at {when}"


class IdentityResolutionSnapshot(AppendOnlyModel):
    """One run of the identity resolver over one package. Table `identity_resolution_snapshots`.

    `CPM-FR-1` resolves each package to a source repository, its release-ecosystem
    identity and zero or more feedstocks, and `CPM-IDENTITY-S08` is the collector
    that does it. The mappings it establishes are written to the package row and
    to `identity.PackageMapping` through `record_resolution` -- identity is
    mutated through that door and no other (`CPM-AD-14`) -- and this row is the
    evidence behind them: what each source said, which `project_urls` key was
    chosen, and what confidence the package held once the recorder had finished.

    **`state` is over `OutcomeState` and is about the *run*, never about the
    package** (`CPM-AD-5`). `ok` is a run that read conda-forge's index and
    reached the recorder -- whatever the recorder concluded, including that
    nothing was established and the package stays `unmapped`; `not_found` is
    the index answering that it has no entry, on which PyPI was never asked and
    nothing was recorded; `error` is a look that failed or a document that could
    not be read; `not_applicable` is the row shape every collector must be able
    to write and this one never asks for, because resolution applies to every
    package.

    **`confidence_recorded` is what the package holds after the run, not what
    the resolver claimed.** The recorder holds a lower claim back from a
    `verified` package while still recording its findings, and
    `downgrade_refused` says when that happened. A reader comparing the two
    columns can tell "resolved to inventory-derived" from "found things, and a
    person's verification stood".

    **`PROTECT`, and it is required rather than preferred**
    (`EVIDENCE.02-AUDIT-001`), on the terms `InventorySnapshot.package` states.

    `observed_at` and `objects` come from `AppendOnlyModel`: the instant is
    supplied by the writer from an injected `Clock` (`CPM-AD-26`) and the manager
    is the one that offers no `update()` and no `delete()` (`CPM-AD-2`).
    """

    #: The package this resolution is about, by the integer primary key
    #: `CPM-AD-3` fixes. Non-nullable: a resolution is always of a package.
    package = models.ForeignKey(
        Package,
        on_delete=models.PROTECT,
        related_name="identity_resolution_snapshots",
        verbose_name=_("package"),
    )

    #: The locator the base read -- conda-forge's feedstock-outputs index entry
    #: for this package. The PyPI locator the bounded second call read is named
    #: in `detail` when it matters. Blank on a row where no locator was built.
    source = models.CharField(_("source"), max_length=_LOCATOR_LENGTH, blank=True, default="")

    #: What the run concluded, over `OutcomeState` and emitted verbatim
    #: (`CPM-AD-24`). See the class docstring for what each value means here.
    state = models.CharField(_("state"), max_length=_STATE_LENGTH, choices=OutcomeState.choices)

    #: The source repository the run chose and normalised, in the one form the
    #: upstream-release collector reads, or blank when none was established.
    #: As wide as `Package.source_repository_url`, which is what the recorder
    #: writes the same value to; `_LOCATOR_LENGTH` is the same 512 and the same
    #: kind of string -- a URL this collector built.
    repository_url = models.URLField(_("repository URL"), max_length=_LOCATOR_LENGTH, blank=True, default="")

    #: Which `project_urls` key won the documented precedence, as the project
    #: spelled it, or blank when no key did. Populated whether or not the value
    #: under it was readable: a `Source` link that was rejected is still the key
    #: the project labelled its source with, and `repository_url` beside it says
    #: whether anything came of it -- so a reader can see that `Homepage` rather
    #: than `Source` is what resolved this package, or that `Source` named
    #: something this product does not read.
    repository_key = models.CharField(_("repository key"), max_length=_REPOSITORY_KEY_LENGTH, blank=True, default="")

    #: Whether PyPI answered at all -- `200` or `404` -- as opposed to a call
    #: that failed, a `304` to an unconditional request, or a document that
    #: could not be read. A fact about the observation rather than a status, on
    #: the terms `FeedstockSnapshot`'s `absence_established` states, and the
    #: machine-readable half of what `detail` says in prose. `False` on a
    #: sentinel row, where PyPI was never reached.
    pypi_asked = models.BooleanField(_("PyPI asked"), default=False)

    #: Whether PyPI holds a project under the package's name. `False` both when
    #: PyPI said no and when PyPI could not be asked; `pypi_asked` and the
    #: `release_ecosystem` mapping's outcome tell those apart.
    pypi_found = models.BooleanField(_("PyPI found"), default=False)

    #: The PyPI locator the bounded second call used, or blank when none could be
    #: built -- a conda name that is not a PyPI name -- and blank on a sentinel
    #: row, where PyPI was never reached.
    pypi_source = models.CharField(_("PyPI source"), max_length=_LOCATOR_LENGTH, blank=True, default="")

    #: The feedstock names conda-forge's index listed, in its order, stripped and
    #: lower-cased, with a name that appears twice -- or twice under two
    #: spellings of one repository -- listed once. The `-feedstock` suffix is
    #: not here: it lives on `identity.Feedstock.name`, which records the
    #: repository. Empty on a sentinel row and on an entry that lists none.
    feedstocks = models.JSONField(_("feedstocks"), default=list, blank=True)

    #: The package-identity confidence the package holds once the recorder has
    #: finished, in `IdentityConfidence`'s own spelling. Blank on every row that
    #: never reached the recorder -- see `Meta.constraints`.
    confidence_recorded = models.CharField(
        _("confidence recorded"),
        max_length=_CONFIDENCE_LENGTH,
        choices=IdentityConfidence.choices,
        blank=True,
        default="",
    )

    #: Whether the recorder held this run's confidence claim back because the
    #: package is `verified`. A fact about the write, not a status. `False` on
    #: every row that never reached the recorder.
    downgrade_refused = models.BooleanField(_("downgrade refused"), default=False)

    #: What the collector or the base had to say -- the sentinel path's reason,
    #: the rejected repository URL and why, that PyPI had no project or could not
    #: be asked, that the index lists no feedstock and the rows already recorded
    #: were kept. Empty when both documents answered and everything was
    #: established.
    detail = models.TextField(_("detail"), blank=True, default="")

    #: The `trace_id` of the task that made this observation, formatted `032x`
    #: (`CPM-AD-15`). Empty when no span was active, which never blocks a write.
    trace_id = models.CharField(_("trace id"), max_length=_TRACE_ID_LENGTH, blank=True, default="")

    class Meta:
        """The table `CPM-IDENTITY-S08` adds, not the `collectors_identityresolutionsnapshot` Django derives.

        **No unique constraint of any kind** (`CPM-AD-2`, `CPM-AD-7`). Two runs
        over one package are two rows, which is how a reader sees what the
        sources said last week beside what they say today.
        """

        db_table = "identity_resolution_snapshots"
        verbose_name = _("identity resolution snapshot")
        verbose_name_plural = _("identity resolution snapshots")
        indexes = [
            # `core/freshness.py`'s `latest_observation` reads exactly this, on
            # the terms `RELEASE_READ_INDEX` states.
            models.Index(fields=["package", "-observed_at"], name=IDENTITY_RESOLUTION_READ_INDEX),
        ]
        constraints = [
            # The biconditional: a row that reached the recorder records the
            # confidence the package holds afterwards, and a row that did not
            # records none of the recorder's facts. `state` and every column
            # tested are NOT NULL, so this is always true or false and never the
            # third thing a SQL CHECK can be.
            models.CheckConstraint(
                condition=(
                    (models.Q(state=OutcomeState.OK) & ~models.Q(confidence_recorded=""))
                    | (
                        ~models.Q(state=OutcomeState.OK)
                        & models.Q(
                            confidence_recorded="",
                            repository_url="",
                            repository_key="",
                            pypi_asked=False,
                            pypi_found=False,
                            pypi_source="",
                            downgrade_refused=False,
                        )
                    )
                ),
                name=IDENTITY_RESOLUTION_FACTS_CONSTRAINT,
            ),
        ]

    def __str__(self) -> str:
        """Return the state, the confidence and when it was observed.

        Returns:
            A one-line summary, read off `package_id` rather than off `package`
            for the reason `SourceReleaseSnapshot.__str__` gives.

        """
        confidence = self.confidence_recorded or "nothing recorded"
        scope = "no package" if self.package_id is None else f"package {self.package_id}"
        when = "never" if self.observed_at is None else self.observed_at.isoformat()
        return f"identity resolution for {scope}: {self.state}, {confidence}, at {when}"
