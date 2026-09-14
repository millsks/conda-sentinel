"""`EVIDENCE.02-AUDIT-002`: no bypass-shaped write exists in this repository's source.

`AppendOnlyModel.save()` refuses to rewrite a row, and
`AppendOnlyQuerySet.update/bulk_update/delete` refuse the queryset spellings. That
is every path those two objects can see, and it is not every path. A queryset
obtained from somewhere else, a manager on a model that forgot the base, a raw
cursor -- each reaches the table without either guard being consulted, and each
looks entirely ordinary in review. `CPM-FR-36` and `R-06` do not survive a
guard with a hole in it, so the hole is closed by a scan.

**The scan bans forms, not tables.** "Against an evidence table" is not
statically resolvable: a queryset bound to a local, passed through a helper, or
returned by a manager method defeats any AST-level attempt to prove which model
it belongs to. So the ban is on the *shapes* across the product's own source, and
exceptions are licensed by count -- the same trade `EVIDENCE.01-AUDIT-002` makes
for the clock. Stated plainly: the guarantee here is **"no new bypass-shaped
write"**, not "no bypass".

**What is matched.**

* `update`, `delete` and `raw` -- and their `a`-prefixed async spellings -- where
  the receiver chain contains `objects`, `_default_manager` or `_base_manager`.
  The marker is what makes the ban safe to state.
* `bulk_update`, `abulk_update` and `_raw_delete` on *any* receiver. These names
  belong to the ORM and to nothing else, so requiring a marker would only lose
  the `queryset.bulk_update(...)` spelling, which is the one a collector writes.
* `execute`, `executemany` and `executescript` whose SQL resolves to a leading
  `UPDATE`, `DELETE`, `TRUNCATE`, `MERGE` or `REPLACE`. `TRUNCATE` is included as
  `DELETE`'s louder sibling -- a statement that empties a table is not a
  different decision from one that empties it row by row -- and `MERGE` and
  `REPLACE INTO` are the two standard verbs that overwrite while opening with
  neither `UPDATE` nor `DELETE`.
* **`INSERT ... ON CONFLICT ... DO UPDATE`, wherever the verb it opens with.**
  This is the raw spelling of the overwrite `core/models.py` calls "`R-06`
  arriving by the front door", and it is the one a writer reaches for *after*
  `AppendOnlyQuerySet.bulk_create(update_conflicts=True)` has refused. A scan
  keyed on the leading verb alone allow-lists it, because it opens with
  `INSERT`. MySQL's `ON DUPLICATE KEY UPDATE` is the same statement and is
  matched by the same pattern.
* **A statement whose write is behind a compound opener.** `WITH ... UPDATE` is a
  data-modifying CTE -- an `UPDATE` whose statement begins with `WITH`, which is
  neither a writing verb nor unresolvable -- and `BEGIN`/`START TRANSACTION`
  followed by one is the same evasion spelled with a transaction. When the
  leading verb is one of those, the whole statement is searched for a writing
  verb.
* `execute`, `executemany` and `executescript` on a *resolved database cursor*
  whose SQL does not resolve at all, and `callproc` on one at all. Dynamic SQL
  through the product's own connection cannot be shown to be read-only, and a
  stored procedure's body is not in this repository to be read; the whole point
  of the ban is that the reviewer should not have to guess.

**`dict.update()` is the false positive designed against.** It is ordinary Python
and it is already in this repository twice --
`config/observability/logging.py` merges handler and logger tables --
so a scan keyed on the method name alone would fail on the day it was written.
The discriminator is the receiver: a manager marker somewhere in the chain. One
of the guards below asserts that `logging.py` contains real `.update(...)` calls
*and* reports no offence, so this is checked against the repository rather than
only against a fixture.

**SQL is resolved through string constants.** `PROBE_QUERY` in
`config/health/views.py` is exactly the shape -- `cursor.execute(PROBE_QUERY)`,
with `PROBE_QUERY: Final[str] = "SELECT 1"` at module level -- and resolving it
is what keeps that readiness probe from needing an exemption while a constant
spelling `"DELETE FROM ..."` still fails. Constants bound inside a function or a
class body are resolved too, because `statement = "DELETE FROM ..."` written one
line above the call is the commoner shape by far; the cost is that the table is
flat, so two same-named constants in different scopes resolve to whichever the
walk reached last. That can only ever mis-resolve one literal into another
literal, never a write into a read that this scan then trusts.

**Receivers are resolved from the imports, not from the last dotted segment**,
the way `tests/unit/django_apps/test_clock_audit.py` resolves a clock: a cursor
is recognised only when it came from `django.db.connection` or
`django.db.connections`, through any alias, through `from django import db`,
through `import django.db`, and through a local rebinding such as
`connection = connections[alias]`. Without that, `pipeline.execute(...)` against
a Redis client would be read as a database write.

**What the scan cannot resolve, stated rather than left to be found.**

* **A queryset bound to a local, for the two spellings that need a manager
  marker.** `queryset.delete()` is in this repository already, in
  `django_service/users/management/commands/prune_expired_state.py`, where the
  queryset comes in as a parameter; `queryset.update(...)` on the same local is
  equally invisible. There is no expression to resolve to a model, and a ban on
  every `.delete()` or `.update()` would fire on `cache.delete(key)`, on a file
  being removed and on every `dict.update(...)` in the tree.

  **The two spellings of one bypass therefore behave differently, and
  deliberately.** `queryset.bulk_update(...)` on that same local *is* reported,
  because `bulk_update` is an ORM name and nothing else's, so it needs no
  marker. So a collector writing `rows.bulk_update(...)` is caught and one
  writing `rows.update(...)` is not. That asymmetry is the price of not firing
  on ordinary Python, it is not an oversight, and
  `test_the_scan_cannot_see_a_queryset_bound_to_a_local` below pins both halves
  so that widening the detector is a deliberate edit here rather than a silent
  change of meaning.
* **The marker is a name, so a non-ORM `objects` attribute is a false positive.**
  `_manager_marker` matches any attribute literally spelled `objects`, which is
  what makes `Thing.objects.update(...)` recognisable without resolving the
  model -- and an S3 bucket's `bucket.objects.delete()` has the same shape. This
  repository carries a django-storages spike (`tests/spikes/`, outside this
  scan's `src/` root), so the collision is real rather than theoretical. It
  fails *toward* the ban: the report names a form somebody has to look at, and
  the entry it needs is a counted exemption rather than a widened detector.
  `test_the_scan_reports_an_objects_attribute_that_is_not_a_manager` pins it.
* SQL assembled by a helper and handed to something that is not a resolved
  cursor, and an f-string that *opens* with an interpolation --
  `f"{verb} FROM evidence"` -- whose leading word cannot be read. Both resolve to
  nothing, which is itself an offence on a resolved cursor and invisible
  anywhere else.
* A concatenation whose left half resolves and whose right half does not:
  the leading verb is read, so `"INSERT INTO x " + built` is checked as an
  `INSERT` and an `ON CONFLICT` hidden in `built` is missed.
* A leading `--` or `/* */` comment is stripped before the verb is read, so
  commenting above a `DELETE` does not hide it -- but the stripping is textual,
  so a `--` inside a string literal in the SQL truncates the rest of that line
  from the text this scan searches.
* Anything reached through `getattr`, which is where every AST-level audit in
  this repository draws its line.

**Migrations are inside this scan, unlike the clock audit's.** That scan excludes
them because `makemigrations` *generates* `django.utils.timezone.now` as a field
default, and failing a gate on generated code would mean hand-editing it forever.
Nothing generates `objects.update(...)`: a mutation in a migration is always a
hand-written data migration, which is precisely how somebody would "correct"
evidence rows. The two that exist today are recorded below as counted
exemptions, so a third fails the gate.

Reads and parses repository files and nothing else: no database, no network, no
subprocess.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from typing import TYPE_CHECKING
from typing import Final

import pytest

from tests.source_scan import SRC_ROOT
from tests.source_scan import dotted_name
from tests.source_scan import parse
from tests.source_scan import project_files

if TYPE_CHECKING:
    from pathlib import Path

#: Mutating ORM methods that need a manager marker in the receiver chain before
#: they count. Each has a common, entirely innocent namesake -- `dict.update`,
#: `cache.delete`, a raw string -- so the marker is what makes the ban safe.
MARKED_MUTATIONS: Final[frozenset[str]] = frozenset({"adelete", "aupdate", "delete", "raw", "update"})

#: Mutating ORM methods whose names belong to the ORM and to nothing else, so no
#: marker is required. `bulk_update` on a queryset held in a local is the exact
#: spelling a collector writes, and requiring a marker would lose it. `retire`
#: is this product's own (`CPM-OPERATE-S07`): the one audited door out of an
#: evidence table, which `core/retention.py` alone may call -- so a second
#: caller anywhere under `src/` is reported here and fails the gate until it is
#: recorded, whatever it holds the queryset in.
UNMISTAKABLE_MUTATIONS: Final[frozenset[str]] = frozenset({"_raw_delete", "abulk_update", "bulk_update", "retire"})

#: What makes a receiver chain a manager or a queryset drawn from one.
MANAGER_MARKERS: Final[frozenset[str]] = frozenset({"_base_manager", "_default_manager", "objects"})

#: The ways SQL text is handed to a database cursor. `executescript` is SQLite's
#: multi-statement spelling, and this repository runs on SQLite locally
#: (`.github/workflows/ci.yml`), so it is a cursor method a writer here really
#: can reach.
EXECUTE_METHODS: Final[frozenset[str]] = frozenset({"execute", "executemany", "executescript"})

#: Cursor methods whose statement is not in this repository to be read.
#: `callproc` names a stored procedure and passes parameters; the procedure's
#: body lives in the database, so no amount of resolution here can show it is a
#: read. Reported on a resolved cursor for the same reason unresolvable SQL is.
OPAQUE_EXECUTE_METHODS: Final[frozenset[str]] = frozenset({"callproc"})

#: The leading verbs that make a statement a write against existing rows.
#: `INSERT` is deliberately absent: an insert is not a mutation, and evidence is
#: written by inserting -- which is exactly why `UPSERT` below has to be matched
#: separately, since the overwrite spelling opens with `INSERT` too.
WRITING_STATEMENTS: Final[frozenset[str]] = frozenset({"DELETE", "MERGE", "REPLACE", "TRUNCATE", "UPDATE"})

#: Leading verbs that open a compound statement whose write is further in.
#: `WITH` is a data-modifying CTE, `BEGIN` and `START` a transaction wrapper;
#: each resolves to a word that is neither a write nor an absence of one, which
#: is how a statement escapes both branches of the check.
COMPOUND_OPENERS: Final[frozenset[str]] = frozenset({"BEGIN", "START", "WITH"})

#: The upsert, in both dialects this product could meet. PostgreSQL spells it
#: `ON CONFLICT (...) DO UPDATE SET ...` and MySQL `ON DUPLICATE KEY UPDATE`;
#: both are an overwrite wearing an insert's leading verb, which is the raw form
#: of what `AppendOnlyQuerySet.bulk_create(update_conflicts=True)` refuses.
#: `DO NOTHING` is deliberately not matched here: it drops an observation rather
#: than overwriting one, and it cannot be told from a legitimate idempotent write
#: against a non-evidence table without knowing the table.
UPSERT: Final = re.compile(r"\bON\s+(?:CONFLICT\b[\s\S]*?\bDO\s+UPDATE|DUPLICATE\s+KEY\s+UPDATE)\b", re.IGNORECASE)

#: The first alphabetic word of a statement, whatever punctuation precedes it.
#: Splitting on whitespace reads `BEGIN; DELETE ...` as `"BEGIN;"`, which is in
#: no table here -- so a semicolon alone would be enough to walk a write past
#: every check below.
LEADING_WORD: Final = re.compile(r"[^A-Za-z_]*([A-Za-z_]+)")

#: SQL comments, removed before the leading verb is read. A `--` line above the
#: statement is the cheapest way to make a `DELETE` resolve to a word that is in
#: no table here.
SQL_COMMENT: Final = re.compile(r"--[^\n]*|/\*[\s\S]*?\*/")

#: A writing verb anywhere in the statement, used only once a compound opener has
#: already been recognised. Applied unconditionally it would fire on
#: `SELECT ... WHERE note = 'DELETE'`.
EMBEDDED_WRITE: Final = re.compile(r"\b(DELETE|MERGE|REPLACE|TRUNCATE|UPDATE)\b", re.IGNORECASE)

#: Where a database connection can be imported from, and the attribute names each
#: source offers. One entry, read three ways: `from django.db import connection`
#: matches the key and takes the name from the offered set;
#: `from django import db` and `import django.db` both match the key by
#: *composition* and bind `db.connection` and `django.db.connection`. All three
#: appear in Django's own documentation, so all three are somebody's habit.
CONNECTION_SOURCES: Final[dict[str, frozenset[str]]] = {"django.db": frozenset({"connection", "connections"})}

# Recorded exemptions, keyed by module, by the exact form *and* by how many times
# that form may appear -- the shape `tests/unit/django_apps/test_clock_audit.py`
# established, and for the same reason: keying by form alone would licence the
# form for the whole file, so the next `objects.delete(...)` added to the same
# migration would be permitted silently. The count is one in each case.
#
# django_service/users/migrations/0003_provision_designated_groups.py -- the
# reverse of the designated-group provisioning. It deletes `auth_group` rows the
# forward operation created, which is what a reversible data migration is, and
# `auth_group` is not evidence.
# django_apps/.../core/migrations/0001_provision_role_groups.py -- the same
# operation for the product's three role groups, and
# `tests/integration/django_apps/test_role_groups.py` asserts that its reverse
# removes exactly those rows and no designated one.
# django_apps/.../core/migrations/0004_collection_run_package.py -- the data step
# that clears package references naming no package, so the foreign key
# `CPM-EVIDENCE-S09` adds can be applied to a table written under the old
# contract. `collection_runs` is a **run-ledger** table, which `CPM-AD-2` exempts
# from the append-only rule in so many words: a run row is created before the
# first outbound call and finalized afterwards, so it is mutable by construction
# and this audit's rule was never about it. Row-by-row `save()` would evade the
# scan while doing the same thing more slowly, which is the evasion
# `tests/unit/test_suite_policy.py` exists to keep out of the suite; the
# exemption is recorded instead, and
# `tests/integration/django_apps/test_run_ledger_migration.py` asserts what the
# step does to a populated table.
#
# django_apps/.../core/models.py -- the one audited door (`CPM-OPERATE-S07`).
# `AppendOnlyQuerySet.retire(*, door)` issues the parent queryset's raw delete,
# after refusing anything but the retention token `core/retention.py` alone
# constructs and after Django's collector has been consulted for `PROTECT`.
# `CPM-AD-2` admits retention as the one exception to append-only, and this is
# the whole of it: one call, counted, and every refusal on the base unchanged
# (`tests/unit/django_apps/test_append_only_model.py` pins each).
# django_apps/.../core/retention.py -- the same story's purge. Its one manager
# delete -- spelled `_default_manager.delete(...)`, which is `objects` on every
# model it serves, because the derived tables arrive as `type[Model]` from the
# reverse relations and that is the accessor Django declares on the base -- is
# the remover for the derived pass tables and the two run ledgers, none of which
# is evidence: `PolicyRun` and `CollectionRun` are mutable by construction
# (`CPM-AD-2`), and the `policies` tables are derived state that cites a run.
# Evidence rows go through the door above and nowhere else -- its one
# `retire(...)` is the second counted form here, so a second caller of the door
# in this module fails the gate as one elsewhere does. All three entries are
# counted so that a second deletion path in either module fails the gate.
RECORDED_EXEMPTIONS: Final[dict[str, dict[str, int]]] = {
    "django_apps/conda_sentinel/core/migrations/0001_provision_role_groups.py": {
        "objects.delete(...)": 1,
    },
    "django_apps/conda_sentinel/core/migrations/0004_collection_run_package.py": {
        "objects.update(...)": 1,
    },
    "django_apps/conda_sentinel/core/models.py": {"_raw_delete(...)": 1},
    "django_apps/conda_sentinel/core/retention.py": {"_default_manager.delete(...)": 1, "retire(...)": 1},
    "django_service/users/migrations/0003_provision_designated_groups.py": {"objects.delete(...)": 1},
}

#: The module whose cursor call the resolution machinery is measured against. It
#: is a real readiness probe -- `with connection.cursor() as cursor:` over a
#: connection taken from `connections[alias]` -- and it is the only cursor
#: execution in this repository, which makes it the one piece of in-tree
#: evidence that the resolution works at all.
A_MODULE_WITH_A_REAL_CURSOR: Final[str] = "config/health/views.py"

#: The module whose `dict.update(...)` calls the discriminator is measured
#: against. Two of them, merging logging tables, and neither is an offence.
A_MODULE_WITH_REAL_DICT_UPDATES: Final[str] = "config/observability/logging.py"

#: The module holding the unresolvable shape this scan admits to missing.
A_MODULE_WITH_A_QUERYSET_IN_A_LOCAL: Final[str] = "django_service/users/management/commands/prune_expired_state.py"

# Synthetic modules the detector is measured against. Source text parsed here
# rather than files on disk: a fixture module under `src/` would be found by the
# scan itself and would need an exemption of its own.
MANAGER_UPDATE = """
from thing.models import Thing

Thing.objects.update(fact="rewritten")
"""

FILTERED_UPDATE = """
from thing.models import Thing

Thing.objects.filter(package="numpy").update(fact="rewritten")
"""

QUERYSET_DELETE = """
from thing.models import Thing

Thing.objects.all().delete()
"""

DEFAULT_MANAGER_UPDATE = """
def rewrite(model):
    model._default_manager.update(fact="rewritten")
"""

BASE_MANAGER_UPDATE = """
def rewrite(model):
    model._base_manager.filter(package="numpy").update(fact="rewritten")
"""

BULK_UPDATE_ON_A_LOCAL = """
def rewrite(queryset, rows):
    queryset.bulk_update(rows, ["fact"])
"""

A_SECOND_USER_OF_THE_DOOR = """
from conda_sentinel.core import retention
from thing.models import Thing

def tidy(rows):
    rows.retire(door=retention._DOOR)
"""

RAW_QUERY = """
from thing.models import Thing

rows = Thing.objects.raw("SELECT * FROM thing")
"""

ASYNC_UPDATE = """
from thing.models import Thing

pending = Thing.objects.aupdate(fact="rewritten")
"""

ASYNC_DELETE = """
from thing.models import Thing

pending = Thing.objects.filter(package="numpy").adelete()
"""

ASYNC_BULK_UPDATE = """
def rewrite(queryset, rows):
    return queryset.abulk_update(rows, ["fact"])
"""

A_BUCKET_THAT_IS_NOT_A_MANAGER = """
def empty(bucket):
    bucket.objects.delete()
"""

CURSOR_UPDATE = """
from django.db import connection

with connection.cursor() as cursor:
    cursor.execute("UPDATE evidence SET fact = 'rewritten'")
"""

CURSOR_DELETE_THROUGH_A_CONSTANT = """
from django.db import connection

PURGE = "DELETE FROM evidence"

with connection.cursor() as cursor:
    cursor.execute(PURGE)
"""

CURSOR_DELETE_THROUGH_A_CONSTANT_IN_A_FUNCTION = """
from django.db import connection


def purge():
    statement = "DELETE FROM evidence"
    with connection.cursor() as cursor:
        cursor.execute(statement)
"""

CURSOR_UPDATE_THROUGH_AN_FSTRING = """
from django.db import connection

table = "evidence"

with connection.cursor() as cursor:
    cursor.execute(f"UPDATE {table} SET fact = 'rewritten'")
"""

CURSOR_SQL_OPENING_WITH_AN_INTERPOLATION = """
from django.db import connection


def run(verb, table):
    with connection.cursor() as cursor:
        cursor.execute(f"{verb} FROM evidence")
"""

CURSOR_UPSERT = """
from django.db import connection

with connection.cursor() as cursor:
    cursor.execute(
        "INSERT INTO evidence (package, fact) VALUES (%s, %s) "
        "ON CONFLICT (package) DO UPDATE SET fact = EXCLUDED.fact"
    )
"""

CURSOR_WRITING_CTE = """
from django.db import connection

with connection.cursor() as cursor:
    cursor.execute(
        "WITH stale AS (SELECT id FROM evidence WHERE observed_at < %s) "
        "UPDATE evidence SET fact = 'rewritten' FROM stale WHERE evidence.id = stale.id"
    )
"""

CURSOR_DELETE_BEHIND_A_COMMENT = """
from django.db import connection

with connection.cursor() as cursor:
    cursor.execute("-- tidy up the duplicates\\nDELETE FROM evidence WHERE id = %s", [row_id])
"""

CURSOR_MERGE = """
from django.db import connection

with connection.cursor() as cursor:
    cursor.execute("MERGE INTO evidence USING incoming ON evidence.id = incoming.id")
"""

CURSOR_REPLACE = """
from django.db import connection

with connection.cursor() as cursor:
    cursor.execute("REPLACE INTO evidence (id, fact) VALUES (%s, %s)", [row_id, fact])
"""

CURSOR_WRITE_INSIDE_A_TRANSACTION = """
from django.db import connection

with connection.cursor() as cursor:
    cursor.execute("BEGIN; DELETE FROM evidence WHERE observed_at < %s; COMMIT", [cutoff])
"""

CURSOR_WRITE_AFTER_START_TRANSACTION = """
from django.db import connection

with connection.cursor() as cursor:
    cursor.execute("START TRANSACTION; UPDATE evidence SET fact = 'rewritten'; COMMIT")
"""

CURSOR_TRUNCATE = """
from django.db import connection

with connection.cursor() as cursor:
    cursor.execute("TRUNCATE TABLE evidence")
"""

CURSOR_EXECUTESCRIPT = """
from django.db import connection

with connection.cursor() as cursor:
    cursor.executescript("DELETE FROM evidence")
"""

CURSOR_CALLPROC = """
from django.db import connection

with connection.cursor() as cursor:
    cursor.callproc("purge_evidence", [30])
"""

CURSOR_EXECUTEMANY_DELETE = """
from django.db import connection

with connection.cursor() as cursor:
    cursor.executemany("DELETE FROM evidence WHERE id = %s", ids)
"""

CONNECTION_THROUGH_THE_DB_PACKAGE = """
from django import db


def run(build):
    with db.connection.cursor() as cursor:
        cursor.execute(build())
"""

CONNECTION_THROUGH_A_MODULE_IMPORT = """
import django.db


def run(build):
    with django.db.connection.cursor() as cursor:
        cursor.execute(build())
"""

CURSOR_WITH_DYNAMIC_SQL = """
from django.db import connection


def run(build):
    with connection.cursor() as cursor:
        cursor.execute(build())
"""

ALIASED_CONNECTION_WITH_DYNAMIC_SQL = """
from django.db import connection as db


def run(build):
    with db.cursor() as cursor:
        cursor.execute(build())
"""

CONNECTION_TAKEN_FROM_THE_ALIAS_MAP = """
from django.db import connections


def run(alias, build):
    connection = connections[alias]
    with connection.cursor() as cursor:
        cursor.execute(build())
"""

A_DICT_UPDATE = """
handlers = {}
handlers.update({"console": {}})
"""

A_DICT_UPDATE_THROUGH_AN_ATTRIBUTE = """
def merge(config, extra):
    config.handlers.update(extra)
"""

A_SET_UPDATE = """
def widen(seen, more):
    seen.update(more)
"""

AN_INSERT = """
from thing.models import Thing

Thing.objects.create(fact="observed")
Thing.objects.bulk_create(rows)
"""

A_READ = """
from thing.models import Thing

rows = Thing.objects.filter(package="numpy").values_list("fact", flat=True)
one = Thing.objects.get(pk=1)
"""

A_CURSOR_SELECT = """
from django.db import connection

with connection.cursor() as cursor:
    cursor.execute("SELECT 1")
"""

A_CURSOR_SELECT_THROUGH_A_CONSTANT = """
from django.db import connections

PROBE_QUERY = "SELECT 1"


def probe(alias):
    connection = connections[alias]
    with connection.cursor() as cursor:
        cursor.execute(PROBE_QUERY)
"""

A_METHOD_NAMED_UPDATE = """
class Registry:
    def update(self, **fields):
        self.fields = fields

    def delete(self):
        self.fields = {}
"""

A_NON_DATABASE_EXECUTE = """
def flush(pipeline):
    pipeline.execute()
"""

A_PLAIN_INSERT_THROUGH_A_CURSOR = """
from django.db import connection

with connection.cursor() as cursor:
    cursor.execute("INSERT INTO evidence (fact) VALUES (%s)", [fact])
    cursor.execute("INSERT INTO evidence (fact) VALUES (%s) ON CONFLICT (fact) DO NOTHING", [fact])
"""

A_CURSOR_SELECT_BEHIND_A_COMMENT = """
from django.db import connection

with connection.cursor() as cursor:
    cursor.execute("/* readiness */ SELECT 1")
"""

A_QUERYSET_IN_A_LOCAL = """
def prune(queryset):
    return queryset.delete()
"""

AN_UPDATE_ON_A_QUERYSET_IN_A_LOCAL = """
def rewrite(queryset):
    return queryset.update(fact="rewritten")
"""

PROSE_ONLY = '''
"""Nothing here calls queryset.update() or cursor.execute("DELETE FROM x"); it only says so."""
'''


def string_constants(tree: ast.Module) -> dict[str, str]:
    """Return every name bound to a string literal anywhere in a module.

    Args:
        tree: The parsed module.

    Returns:
        Name to value, for plain and annotated assignments at any depth. This is
        what resolves `cursor.execute(PROBE_QUERY)`; a constant is where the SQL
        of a real call site usually lives, so a scan that read only inline
        literals would see almost none of them.

        Function and class bodies are walked as well as module level, because
        `statement = "DELETE FROM ..."` one line above the call is the commoner
        shape by far and a module-level-only table cannot see it. The table is
        flat, so two same-named constants in different scopes resolve to
        whichever the walk reached last -- which can only ever swap one literal
        for another, never turn an unresolved statement into a trusted read.

    """
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        value = node.value if isinstance(node, ast.Assign | ast.AnnAssign) else None
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                found[target.id] = value.value
    return found


def statement_text(node: ast.expr, constants: dict[str, str]) -> str | None:
    """Return the resolvable SQL text of an expression, or `None`.

    Args:
        node: The expression handed to `execute` as its statement.
        constants: The module's string constants, for a name to resolve through.

    Returns:
        The text for a literal, for an f-string *whose first segment is literal*,
        for a concatenation whose left side resolves, and for a name bound to a
        string constant. `None` where the statement cannot be read at all.

        The f-string rule is about the leading verb: `f"{verb} FROM evidence"`
        opens with an interpolation, so nothing can be said about what it does,
        and reading its *later* literal segments would resolve it to `"FROM"` --
        a word that is neither a write nor an absence of one, which escapes both
        branches of the check. Once the first segment is literal the remaining
        literal segments are joined in, so an `ON CONFLICT` after an interpolated
        table name is still seen.

    """
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr):
        return _interpolated_text(node)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _concatenated_text(node, constants)
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    return None


def _interpolated_text(node: ast.JoinedStr) -> str | None:
    """Return the literal segments of an f-string, or `None`.

    Args:
        node: The f-string handed to `execute`.

    Returns:
        Every literal segment joined by a space, but only when the *first*
        segment is literal. See `statement_text` for why the first one decides:
        `f"{verb} FROM evidence"` would otherwise resolve to `"FROM"`, a word
        that is neither a write nor an unreadable statement.

    """
    first = node.values[0] if node.values else None
    if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
        return None
    return " ".join(
        part.value for part in node.values if isinstance(part, ast.Constant) and isinstance(part.value, str)
    )


def _concatenated_text(node: ast.BinOp, constants: dict[str, str]) -> str | None:
    """Return the resolvable text of a `+` concatenation, or `None`.

    Args:
        node: The concatenation handed to `execute`.
        constants: The module's string constants.

    Returns:
        Both halves joined where both resolve, the left half alone where the
        right one does not, and `None` where the left one does not -- because the
        left half is where the leading verb is, and a statement whose verb cannot
        be read is unresolved rather than partially read.

    """
    left = statement_text(node.left, constants)
    if left is None:
        return None
    right = statement_text(node.right, constants)
    return left if right is None else f"{left} {right}"


def _readable(text: str) -> str:
    """Return SQL with its comments removed.

    Args:
        text: The resolved statement.

    Returns:
        The same text with `--` line comments and `/* */` blocks blanked out, so
        that a comment above the statement cannot hide the verb underneath it.
        Textual rather than lexical: a `--` inside a string literal in the SQL
        blanks the rest of that line too, which the module docstring records.

    """
    return SQL_COMMENT.sub(" ", text)


def _leading_word(readable: str) -> str | None:
    """Return the first alphabetic word of a statement, upper-cased.

    Args:
        readable: The statement, comments already stripped.

    Returns:
        The first run of letters, ignoring whatever punctuation precedes it, or
        `None` for a statement with no word in it at all. Splitting on
        whitespace alone would read `BEGIN; DELETE FROM ...` as `"BEGIN;"` and
        `(SELECT ...)` as `"(SELECT"`, neither of which is in any table here --
        so a semicolon would be enough to walk a `DELETE` past the check.

    """
    found = LEADING_WORD.match(readable)
    return found.group(1).upper() if found is not None else None


def leading_keyword(node: ast.expr, constants: dict[str, str]) -> str | None:
    """Return the first SQL word of an expression, upper-cased, or `None`.

    Args:
        node: The expression handed to `execute` as its statement.
        constants: The module's string constants, for a name to resolve through.

    Returns:
        The leading word of whatever `statement_text` resolved, comments
        stripped first. `None` when the statement cannot be resolved at all,
        which is a finding in its own right rather than an absence of one.

    """
    text = statement_text(node, constants)
    return None if text is None else _leading_word(_readable(text))


def writing_form(node: ast.expr, constants: dict[str, str]) -> str | None:
    """Return the form of the write an executed statement performs, or `None`.

    Args:
        node: The expression handed to `execute` as its statement.
        constants: The module's string constants, for a name to resolve through.

    Returns:
        The leading verb for a plain write; `"UPSERT"` for
        `ON CONFLICT ... DO UPDATE`, wherever the verb it opens with; and
        `"WITH ... UPDATE"` and its siblings for a write behind a compound
        opener. `None` for a statement that resolves to something this scan can
        read and finds is not a write -- which is not the same answer as
        `leading_keyword` returning `None`, and the two are asked separately for
        exactly that reason: a resolved `SELECT` is fine and an unresolved
        statement on a cursor is not.

    """
    text = statement_text(node, constants)
    if text is None:
        return None
    readable = _readable(text)
    keyword = _leading_word(readable)
    if keyword in WRITING_STATEMENTS:
        return keyword
    if UPSERT.search(readable):
        return "UPSERT"
    if keyword in COMPOUND_OPENERS and (embedded := EMBEDDED_WRITE.search(readable)) is not None:
        return f"{keyword} ... {embedded.group(1).upper()}"
    return None


def _is_connection(node: ast.expr, bound: set[str]) -> bool:
    """Report whether an expression is a database connection.

    Args:
        node: The expression to classify.
        bound: The local names that resolve to a connection.

    Returns:
        True for a bound name and for a subscript of one, which is how
        `connections[alias]` is spelled.

    """
    current = node
    while isinstance(current, ast.Subscript):
        current = current.value
    return dotted_name(current) in bound


def _imported_connections(node: ast.Import | ast.ImportFrom) -> set[str]:
    """Return the connection spellings one import statement binds.

    Args:
        node: The import to read.

    Returns:
        The bound spellings. `from django.db import connection` binds the name
        the module offered, under whatever alias it was given;
        `from django import db` and `import django.db` both bind the *module*, so
        what they contribute is the dotted `db.connection` and
        `django.db.connection`. A relative import binds nothing: `node.module` is
        then a suffix rather than a package path, and this repository's own
        modules offer no connection.

    """
    if isinstance(node, ast.Import):
        return {
            f"{alias.asname or alias.name}.{attribute}"
            for alias in node.names
            for attribute in CONNECTION_SOURCES.get(alias.name, ())
        }
    if node.level != 0 or node.module is None:
        return set()
    offered = CONNECTION_SOURCES.get(node.module)
    if offered is not None:
        return {alias.asname or alias.name for alias in node.names if alias.name in offered}
    return {
        f"{alias.asname or alias.name}.{attribute}"
        for alias in node.names
        for attribute in CONNECTION_SOURCES.get(f"{node.module}.{alias.name}", ())
    }


def connection_bindings(tree: ast.Module) -> set[str]:
    """Return every local spelling that resolves to a database connection.

    Resolved from the imports rather than from the name, so `pipeline` or a
    variable somebody called `connection` binds nothing, and
    `from django.db import connection as db` is still seen.

    Three import spellings reach the same object and all three are in Django's
    own documentation: `from django.db import connection` (the name is offered by
    the module), `from django import db` (the *module* is the imported name, and
    the connection is an attribute of it), and `import django.db` (the same, by
    its full dotted path).

    Args:
        tree: The parsed module.

    Returns:
        The bound spellings, including names rebound from an already-bound one --
        `connection = connections[alias]` is the form `config/health/views.py`
        uses, and without the second pass its cursor would be invisible.

    """
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            bound |= _imported_connections(node)
    widened = True
    while widened:
        widened = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id not in bound and _is_connection(node.value, bound):
                bound.add(target.id)
                widened = True
    return bound


def _is_cursor_call(node: ast.expr, connections: set[str]) -> bool:
    """Report whether an expression is `<connection>.cursor()`.

    Args:
        node: The expression to classify.
        connections: The local names that resolve to a connection.

    Returns:
        True for a call to `cursor` on a resolved connection.

    """
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "cursor"
        and _is_connection(node.func.value, connections)
    )


def cursor_bindings(tree: ast.Module) -> set[str]:
    """Return every local name holding a database cursor.

    Args:
        tree: The parsed module.

    Returns:
        The names bound by `with <connection>.cursor() as name:` and by a plain
        assignment of the same call. The `with` form is the one Django's own
        documentation uses and the only one in this repository.

    """
    connections = connection_bindings(tree)
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.With | ast.AsyncWith):
            bound.update(
                item.optional_vars.id
                for item in node.items
                if isinstance(item.optional_vars, ast.Name) and _is_cursor_call(item.context_expr, connections)
            )
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and _is_cursor_call(node.value, connections)
        ):
            bound.add(node.targets[0].id)
    return bound


def cursor_executions(tree: ast.Module) -> list[tuple[int, str | None]]:
    """Return every statement executed against a resolved database cursor.

    Args:
        tree: The parsed module.

    Returns:
        One `(line, leading keyword)` pair per call, the keyword `None` where the
        statement could not be resolved. Separate from `mutation_paths` because
        the *recognition* is what an anti-vacuity guard needs to assert against
        real in-tree code, independently of whether the statement was an offence.

    """
    constants = string_constants(tree)
    cursors = cursor_bindings(tree)
    connections = connection_bindings(tree)
    return [
        (node.lineno, leading_keyword(node.args[0], constants) if node.args else None)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in EXECUTE_METHODS
        and (
            (isinstance(node.func.value, ast.Name) and node.func.value.id in cursors)
            or _is_cursor_call(node.func.value, connections)
        )
    ]


def _manager_marker(node: ast.expr) -> str | None:
    """Return the manager marker in a receiver chain, if there is one.

    Walks through calls and subscripts, so `Thing.objects.filter(...).exclude(...)`
    still resolves: the chain is what makes a receiver a queryset, and every
    intermediate step is a call.

    Args:
        node: The receiver expression.

    Returns:
        The marker found -- `objects`, `_default_manager`, `_base_manager` -- or
        `None` for a receiver that is not manager-rooted, which is what keeps
        `dict.update()` out of the report.

    """
    current = node
    while True:
        if isinstance(current, ast.Attribute):
            if current.attr in MANAGER_MARKERS:
                return current.attr
            current = current.value
        elif isinstance(current, ast.Call):
            current = current.func
        elif isinstance(current, ast.Subscript):
            current = current.value
        elif isinstance(current, ast.Name):
            return current.id if current.id in MANAGER_MARKERS else None
        else:
            return None


def mutation_paths(tree: ast.Module) -> list[str]:
    """Return every bypass-shaped write in one module, as `line: form` strings.

    Args:
        tree: The parsed module.

    Returns:
        One entry per offence, the form spelled canonically --
        `objects.update(...)`, `bulk_update(...)`, `execute(<UPDATE>)`,
        `execute(<UPSERT>)`, `execute(<unresolved>)`, `callproc(<opaque>)` -- so
        an exemption licenses the *form* that was reviewed rather than a line
        number that moves whenever the file is edited.

    """
    constants = string_constants(tree)
    cursors = cursor_bindings(tree)
    connections = connection_bindings(tree)

    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        method = node.func.attr
        receiver = node.func.value
        if method in EXECUTE_METHODS or method in OPAQUE_EXECUTE_METHODS:
            is_cursor = (isinstance(receiver, ast.Name) and receiver.id in cursors) or _is_cursor_call(
                receiver,
                connections,
            )
            if method in OPAQUE_EXECUTE_METHODS:
                if is_cursor:
                    found.append(f"{node.lineno}: {method}(<opaque>)")
                continue
            form = writing_form(node.args[0], constants) if node.args else None
            keyword = leading_keyword(node.args[0], constants) if node.args else None
            if form is not None:
                found.append(f"{node.lineno}: {method}(<{form}>)")
            elif keyword is None and is_cursor:
                found.append(f"{node.lineno}: {method}(<unresolved>)")
        elif method in UNMISTAKABLE_MUTATIONS:
            found.append(f"{node.lineno}: {method}(...)")
        elif method in MARKED_MUTATIONS and (marker := _manager_marker(receiver)) is not None:
            found.append(f"{node.lineno}: {marker}.{method}(...)")
    return sorted(found, key=lambda entry: int(entry.split(":", 1)[0]))


def paths_in(path: Path) -> list[str]:
    """Return every bypass-shaped write in one file.

    Args:
        path: The module to scan.

    Returns:
        One `line: form` string per offence.

    """
    return mutation_paths(parse(path))


#: Every module under `src/` the rule applies to. Migrations included -- see the
#: module docstring for why this scan differs from the clock audit's there.
SUBJECT_MODULES: Final[tuple[Path, ...]] = project_files(SRC_ROOT)


# ---------------------------------------------------------------------------
# The sweep, and the guards that it is looking at the right things.
# ---------------------------------------------------------------------------


def test_the_scan_reaches_the_modules_the_guards_below_name() -> None:
    """The anti-vacuity guard: the files this module reasons about are in view.

    A scan that had stopped reaching them -- an exclusion widened, a walk that
    lost a directory -- would report a clean repository and pass every assertion
    below while proving nothing.
    """
    relative = {path.relative_to(SRC_ROOT).as_posix() for path in SUBJECT_MODULES}

    assert SUBJECT_MODULES != (), f"the scan reached no module at all under {SRC_ROOT}"
    for named in (
        A_MODULE_WITH_A_REAL_CURSOR,
        A_MODULE_WITH_REAL_DICT_UPDATES,
        A_MODULE_WITH_A_QUERYSET_IN_A_LOCAL,
        *RECORDED_EXEMPTIONS,
    ):
        assert named in relative, named


@pytest.mark.parametrize(
    "path",
    SUBJECT_MODULES,
    ids=lambda path: str(path.relative_to(SRC_ROOT)),
)
def test_no_module_writes_around_the_append_only_guard(path: Path) -> None:
    """`EVIDENCE.02-AUDIT-002`, one case per module so a violation names its file.

    The exemption above is spent per occurrence rather than per form: a module
    that has used its one recorded mutation gets no second one for free.
    """
    relative = path.relative_to(SRC_ROOT).as_posix()
    exempted = RECORDED_EXEMPTIONS.get(relative, {})
    found = paths_in(path)
    counted = Counter(entry.split(": ", 1)[1] for entry in found)
    over_quota = {form for form, count in counted.items() if count > exempted.get(form, 0)}
    offences = [entry for entry in found if entry.split(": ", 1)[1] in over_quota]

    assert offences == [], (
        f"{relative} writes around the append-only guard: {offences}. "
        f"Evidence is re-observed by inserting a new row (CPM-AD-2, CPM-AD-7)."
    )


def test_the_exemption_table_has_entries_to_check() -> None:
    """The parametrize below means nothing if the table it reads is empty."""
    assert RECORDED_EXEMPTIONS != {}


@pytest.mark.parametrize("relative", sorted(RECORDED_EXEMPTIONS), ids=str)
def test_every_recorded_exemption_still_describes_the_file(relative: str) -> None:
    """An exemption that no longer applies is a licence nobody meant to leave open.

    Checked in the same direction the exemption is granted: the module has to be
    one the scan reaches -- a rename would otherwise leave the entry green while
    the file it licenses went unscanned -- and it has to still contain the
    recorded form exactly as many times as the table records. Remove the reverse
    from a migration and this fails until its entry goes with it; add a second
    mutation and it fails from the other side.
    """
    module = SRC_ROOT / relative

    assert module in SUBJECT_MODULES, f"{relative} is exempted but is not a module the scan reaches"

    counted = Counter(entry.split(": ", 1)[1] for entry in paths_in(module))
    recorded = RECORDED_EXEMPTIONS[relative]
    mismatched = {
        form: (counted.get(form, 0), expected)
        for form, expected in recorded.items()
        if counted.get(form, 0) != expected
    }

    assert mismatched == {}, f"{relative}: recorded exemptions no longer match, found vs recorded {mismatched}"


# ---------------------------------------------------------------------------
# The detector, measured against this repository's own code.
# ---------------------------------------------------------------------------


def test_the_detector_recognises_the_cursor_this_repository_actually_opens() -> None:
    """The resolution machinery, proved against real code rather than a fixture.

    `config/health/views.py` reaches its cursor the long way -- `connections` is
    imported, an alias is subscripted out of it into a local, and the cursor
    comes from a `with` statement -- so a scan that recognised only
    `connection.cursor()` would miss it. It is also the case that would fail if
    the constant resolution broke: the statement is `PROBE_QUERY`, and it must
    resolve to a `SELECT` rather than to nothing.
    """
    executions = cursor_executions(parse(SRC_ROOT / A_MODULE_WITH_A_REAL_CURSOR))

    assert [keyword for _, keyword in executions] == ["SELECT"]
    assert paths_in(SRC_ROOT / A_MODULE_WITH_A_REAL_CURSOR) == []


def test_the_detector_ignores_the_dict_updates_this_repository_actually_makes() -> None:
    """`dict.update()` is ordinary Python, and it is already here twice.

    Asserted against the file rather than only against a fixture, because the
    fixture proves the discriminator works on code written to demonstrate it and
    this proves it works on code written for another purpose entirely. The count
    is checked first: a module that had lost its `.update(...)` calls would
    satisfy "reports nothing" for the wrong reason.
    """
    tree = parse(SRC_ROOT / A_MODULE_WITH_REAL_DICT_UPDATES)
    updates = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "update"
    ]

    assert updates != [], A_MODULE_WITH_REAL_DICT_UPDATES
    assert mutation_paths(tree) == []


def test_the_scan_cannot_see_a_queryset_bound_to_a_local() -> None:
    """The limit this audit admits to, pinned so that widening it is deliberate.

    `prune_expired_state.py` takes a queryset as a parameter and calls `delete()`
    on it. There is no expression to resolve to a model, and banning every
    `.delete()` would fire on `cache.delete(key)` and on a file being removed.
    The guarantee is "no new bypass-shaped write", not "no bypass" -- and a
    limit stated in a docstring alone goes stale, so it is stated here where it
    fails if it changes.

    **The asymmetry is pinned alongside it.** `queryset.update(...)` on that same
    local is invisible for the same reason, while `queryset.bulk_update(...)` is
    reported, because `bulk_update` needs no manager marker. Two spellings of one
    bypass behave differently, and a reader who found that out from a failing
    audit rather than from here would reasonably think one of them was a bug.
    """
    assert paths_in(SRC_ROOT / A_MODULE_WITH_A_QUERYSET_IN_A_LOCAL) == []
    assert mutation_paths(ast.parse(A_QUERYSET_IN_A_LOCAL)) == []
    assert mutation_paths(ast.parse(AN_UPDATE_ON_A_QUERYSET_IN_A_LOCAL)) == []
    assert mutation_paths(ast.parse(BULK_UPDATE_ON_A_LOCAL)) != []


def test_a_second_user_of_the_door_is_reported_whatever_holds_the_queryset() -> None:
    """`CPM-OPERATE-S07`: `retire` needs no manager marker, so a local hides nothing.

    The door is licensed to `core/retention.py` by count. A probe module that
    reaches it through a queryset held in a parameter -- the shape the scan
    admits it cannot see for `delete()` -- is still reported, because `retire`
    is a name that belongs to this product's evidence base and to nothing else.
    """
    assert [entry.split(": ", 1)[1] for entry in mutation_paths(ast.parse(A_SECOND_USER_OF_THE_DOOR))] == [
        "retire(...)"
    ]


def test_the_scan_reports_an_objects_attribute_that_is_not_a_manager() -> None:
    """The false positive this detector accepts, pinned rather than discovered.

    `_manager_marker` matches any attribute literally spelled `objects`, which is
    what lets `Thing.objects.update(...)` be recognised without resolving the
    model -- and an S3 bucket's `bucket.objects.delete()` is the same shape. This
    repository carries a django-storages spike, so the collision is real; the
    spike lives under `tests/spikes/` and this scan stops at `src/`, which is why
    nothing fails today.

    It fails *toward* the ban, which is the right direction: the report names a
    form a reviewer looks at once and licenses with a counted exemption, rather
    than the detector being widened to resolve receivers it cannot resolve.
    """
    assert [entry.split(": ", 1)[1] for entry in mutation_paths(ast.parse(A_BUCKET_THAT_IS_NOT_A_MANAGER))] == [
        "objects.delete(...)",
    ]


# ---------------------------------------------------------------------------
# The detector, measured against every spelling of the ban.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        MANAGER_UPDATE,
        FILTERED_UPDATE,
        QUERYSET_DELETE,
        DEFAULT_MANAGER_UPDATE,
        BASE_MANAGER_UPDATE,
        BULK_UPDATE_ON_A_LOCAL,
        A_SECOND_USER_OF_THE_DOOR,
        RAW_QUERY,
        ASYNC_UPDATE,
        ASYNC_DELETE,
        ASYNC_BULK_UPDATE,
        CURSOR_UPDATE,
        CURSOR_DELETE_THROUGH_A_CONSTANT,
        CURSOR_DELETE_THROUGH_A_CONSTANT_IN_A_FUNCTION,
        CURSOR_UPDATE_THROUGH_AN_FSTRING,
        CURSOR_SQL_OPENING_WITH_AN_INTERPOLATION,
        CURSOR_UPSERT,
        CURSOR_WRITING_CTE,
        CURSOR_DELETE_BEHIND_A_COMMENT,
        CURSOR_MERGE,
        CURSOR_REPLACE,
        CURSOR_WRITE_INSIDE_A_TRANSACTION,
        CURSOR_WRITE_AFTER_START_TRANSACTION,
        CURSOR_TRUNCATE,
        CURSOR_EXECUTESCRIPT,
        CURSOR_CALLPROC,
        CURSOR_EXECUTEMANY_DELETE,
        CURSOR_WITH_DYNAMIC_SQL,
        ALIASED_CONNECTION_WITH_DYNAMIC_SQL,
        CONNECTION_TAKEN_FROM_THE_ALIAS_MAP,
        CONNECTION_THROUGH_THE_DB_PACKAGE,
        CONNECTION_THROUGH_A_MODULE_IMPORT,
    ],
    ids=[
        "manager-update",
        "filtered-update",
        "queryset-delete",
        "default-manager",
        "base-manager",
        "bulk-update-local",
        "second-user-of-the-door",
        "raw",
        "async-update",
        "async-delete",
        "async-bulk-update",
        "cursor-update",
        "cursor-constant",
        "cursor-constant-in-a-function",
        "cursor-fstring",
        "cursor-fstring-leading-interpolation",
        "cursor-upsert",
        "cursor-writing-cte",
        "cursor-commented-delete",
        "cursor-merge",
        "cursor-replace",
        "cursor-transaction-delete",
        "cursor-start-transaction-update",
        "cursor-truncate",
        "cursor-executescript",
        "cursor-callproc",
        "cursor-executemany",
        "cursor-dynamic",
        "cursor-aliased",
        "cursor-alias-map",
        "connection-db-package",
        "connection-module-import",
    ],
)
def test_the_detector_matches_every_banned_form(source: str) -> None:
    """Every spelling of the same bypass, because a scan that knew one has a door in it.

    One case per entry in every ban table this module declares, which is the
    property `--cov=src` cannot give: coverage measures the *product's* source,
    so a marker, a verb or an async spelling that no fixture exercises can be
    deleted with the suite still green and the ban silently narrowed.

    The f-string and the constant are the two that a literal-only scan misses,
    and they are what somebody writes after being told not to inline the SQL.
    The upsert and the data-modifying CTE are the two that open with a verb this
    scan permits. The cursor cases with unresolvable SQL -- dynamic, opaque, and
    the f-string that opens with an interpolation -- are the ones that make the
    ban a ban rather than a text search.
    """
    assert mutation_paths(ast.parse(source)) != []


@pytest.mark.parametrize(
    "source",
    [
        A_DICT_UPDATE,
        A_DICT_UPDATE_THROUGH_AN_ATTRIBUTE,
        A_SET_UPDATE,
        AN_INSERT,
        A_READ,
        A_CURSOR_SELECT,
        A_CURSOR_SELECT_THROUGH_A_CONSTANT,
        A_CURSOR_SELECT_BEHIND_A_COMMENT,
        A_METHOD_NAMED_UPDATE,
        A_NON_DATABASE_EXECUTE,
        PROSE_ONLY,
    ],
    ids=[
        "dict-update",
        "dict-update-attribute",
        "set-update",
        "insert",
        "read",
        "cursor-select",
        "cursor-select-constant",
        "cursor-select-commented",
        "definition",
        "non-database-execute",
        "prose",
    ],
)
def test_the_detector_ignores_what_is_not_a_bypass(source: str) -> None:
    """The negative control, and the whole point of parsing rather than grepping.

    `create` and `bulk_create` are how evidence is written and must stay
    untouched. A `SELECT` through a cursor is a read. `pipeline.execute()` is not
    a database call at all -- the receiver resolves to nothing this scan imported
    -- and a method *definition* named `update` is the opposite of a call to one.
    A text search for `.update(` or `.delete(` flags most of these.
    """
    assert mutation_paths(ast.parse(source)) == []


def test_the_reported_form_distinguishes_a_resolved_statement_from_an_unresolved_one() -> None:
    """An exemption licenses the shape that was reviewed, not any write in the file.

    `cursor.execute("UPDATE ...")` and `cursor.execute(build())` are different
    decisions -- one was read and found to be a write, the other could not be
    read at all -- so a file licensed for one must not be silently licensed for
    the other.
    """
    resolved = mutation_paths(ast.parse(CURSOR_UPDATE))
    unresolved = mutation_paths(ast.parse(CURSOR_WITH_DYNAMIC_SQL))

    assert [entry.split(": ", 1)[1] for entry in resolved] == ["execute(<UPDATE>)"]
    assert [entry.split(": ", 1)[1] for entry in unresolved] == ["execute(<unresolved>)"]


def test_the_reported_form_names_the_write_hiding_behind_a_permitted_verb() -> None:
    """The three statements that are writes without opening with a writing verb.

    `INSERT ... ON CONFLICT DO UPDATE` opens with `INSERT`, which this scan
    permits because inserting is how evidence is written; a data-modifying CTE
    opens with `WITH`; a stored procedure names nothing at all. Each is reported
    under its own form rather than folded into `execute(<UPDATE>)`, because an
    exemption licenses the shape somebody reviewed -- and "we read the SQL and it
    upserts", "we read it and the write is in the CTE" and "there is no SQL here
    to read" are three different reviews.
    """
    upsert = mutation_paths(ast.parse(CURSOR_UPSERT))
    cte = mutation_paths(ast.parse(CURSOR_WRITING_CTE))
    procedure = mutation_paths(ast.parse(CURSOR_CALLPROC))

    assert [entry.split(": ", 1)[1] for entry in upsert] == ["execute(<UPSERT>)"]
    assert [entry.split(": ", 1)[1] for entry in cte] == ["execute(<WITH ... UPDATE>)"]
    assert [entry.split(": ", 1)[1] for entry in procedure] == ["callproc(<opaque>)"]


def test_an_insert_that_is_only_an_insert_stays_permitted() -> None:
    """The other side of the upsert rule, so it is not a ban on `INSERT`.

    Evidence is written by inserting, and `ON CONFLICT DO NOTHING` is
    deliberately not matched either: it drops an observation rather than
    overwriting one, and at this level it cannot be told from a legitimate
    idempotent write against a table that is not evidence. `bulk_create` is where
    that form is refused, with the model in hand.
    """
    assert mutation_paths(ast.parse(A_PLAIN_INSERT_THROUGH_A_CURSOR)) == []


def test_the_reported_form_names_the_manager_marker_it_matched_on() -> None:
    """The form has to say why the receiver counted, or the exemption table is unreadable.

    `objects.delete(...)` and `_default_manager.update(...)` are the two shapes a
    reviewer has to tell apart when deciding whether a recorded exemption still
    describes the code it licenses.
    """
    marked = mutation_paths(ast.parse(QUERYSET_DELETE))
    internal = mutation_paths(ast.parse(DEFAULT_MANAGER_UPDATE))

    assert [entry.split(": ", 1)[1] for entry in marked] == ["objects.delete(...)"]
    assert [entry.split(": ", 1)[1] for entry in internal] == ["_default_manager.update(...)"]
