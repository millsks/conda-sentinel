# Managing the inventory

Adding a package, retiring one, and correcting an identity the resolver got wrong.

---

## What the inventory is

The list of packages this product watches is a **governed database table** — `inventory`,
one row per package — changed through exactly two doors, each of which writes an audit
row in the same transaction as the change:

| Door | Who | Writes |
|---|---|---|
| The **inventory page** at `/conda-sentinel/inventory/` | a person in the `leadership` role, with a reason | one row: add, change, retire |
| The **`import-watchlist`** admin process | the command line, unattended | every row a reviewed CSV names, one transaction per row |

The reviewed CSV inside the wheel is still there, and it is the **first-run seed**:

```
src/django_apps/conda_sentinel/collectors/data/
├── watchlist.csv               ← deployed. Ships with a header and no rows.
├── watchlist-development.csv   ← what the local stack imports (148 rows)
└── README.md                   ← the column contract, beside the files
```

`import-watchlist` reads it into the table; from then on the table is the inventory
and the file is where a reviewed bulk change comes from.

### Which source ingestion reads

Ingestion reads the inventory through one declared adapter (`CPM-AD-29`), and
**`CPM_INVENTORY_SOURCE`** says which:

| Value | Ingestion reads | When |
|---|---|---|
| `watchlist` (the default) | the CSV file locality selects — `watchlist-development.csv` when `COMPONENT_RUNTIME=local`, `watchlist.csv` otherwise | a deployment that has never imported its watchlist |
| `database` | the `inventory` table's active rows | after `import-watchlist` has filled the table |

The default is the file, and that direction is the one that fails closed: a component
that has never run the import keeps reading the reviewed file it ships, and switching
to the table is an operator's declaration made *after* the import. Anything else is
refused at boot, naming the setting. The `dev` pixi environment declares `database`,
so the local stack and the test suite read the table the demo seeder fills.

### Why a table, and why the file stays

Because adding a package should be a decision somebody can make on a Tuesday afternoon
*and* leave a record of why. A CSV in the repository gets a pull request and a diff; a
governed table gets a permission, a required reason and an audit row that names the
person or the file — which is the same record, without the release. The file keeps its
job as the reviewed bulk source, and the audit trail keeps its.

---

## Adding a package, end to end

1. **Add the row.** Either on the inventory page — key, name, the two required counts,
   any of the four optional ones, and a reason — or by adding a line to the reviewed
   CSV and running the import:

    ```sh
    pixi run stack-run import_watchlist                 # the file locality selects
    pixi run stack-run import_watchlist path/to/a.csv   # a named file
    ```

    Deployed, the same command is `pixi run import-watchlist`, the admin process
    `component.toml` declares.

2. **Ingest.** `pixi run stack-run ingest_inventory` (deployed: `pixi run ingest`). The
   next ingestion creates the package **shell** at `unmapped` and its first inventory
   snapshot. Nothing is created until this runs; the page says so.

3. **Resolve its identity.** `pixi run stack-run dispatch_sweep resolve_identity`, or
   wait for the daily sweep. The resolver reads conda-forge's index and PyPI and
   establishes what the shell maps to.

4. **Collect.** The other sweeps offer the package as soon as the mapping each one reads
   is established — `pixi run stack-run dispatch_sweep --all` on day one, the beat
   schedule after that.

### The eight columns

A row — on the page or in the file — carries **exactly** these eight names, and nothing
else:

| Column | Required | Meaning |
|---|---|---|
| `source_package_key` | yes | What the inventory files it under. Becomes `associator_key` — the stable value nothing corrects. One row per key. |
| `package_name` | yes | What it is called. Becomes `canonical_name`, the one *correctable* name. |
| `internal_component_count` | yes | How many internal components use it |
| `internal_lob_count` | yes | How many internal lines of business use it |
| `apps` | no | How many applications name it |
| `platforms` | no | How many platforms it is used on |
| `downloads` | no | Internal downloads |
| `versions` | no | How many versions are in use |

A column this table does not define — a repository URL, a feedstock, a purl, a
confidence — is **refused, not ignored**. Ingestion never asserts a mapping, and a
silently dropped column is a reviewer who believes they supplied one.

!!! important "A blank optional cell is not a zero"

    Blank records that the source did not say, and is stored as NULL. `0` records
    that the source counted none. Nothing in this product collapses the two — the
    page renders NULL as an em dash, never as `0` — and neither should an edit.

    The two required counts are the internal usage breadth the priority pass ranks by,
    which is why a row cannot omit either: the page and the import both refuse one
    that does, naming the key.

The full file contract — bounds, encoding, what a malformed row does — is in
`src/django_apps/conda_sentinel/collectors/data/README.md`, deliberately beside the
files rather than only here.

### Every change is audited, in the same transaction

Each add, change or retirement writes one `inventory_changes` row beside it: the prior
and new value of every changeable field, who (a person, or the file an unattended
import read), why, and when. The row and its audit row commit together or not at all —
a change with no reason is refused before anything is written, and so is a change by
somebody without the `collectors.change_inventory` permission, which the
[leadership role](authorization.md) holds. The page shows the newest twenty beneath
the table.

A file import writes no audit row for an entry it names exactly as the table already
holds it, so a repeated import of the same file is a no-op that says so. One more rule
every door enforces: **one active row per name**. The name becomes the package's
canonical name, which is unique, so a second active row carrying a name another key
already carries is refused naming that key — retire the other row first, or name this
one differently. A retired row keeps its name and is outside the rule.

### The whole file is validated before anything is written

An import parses the file through the same parser a file-sourced ingestion uses, so
every refusal about the file applies — a bad header, a blank line, a ragged row, a
count the column will not hold, a repeated key — naming the file and the line, before
a single row is written. A row the *service* refuses stops the import at that row: the
rows before it stay committed, each with its audit row, and the message names the key.

---

## Retiring a package

**Retire the row** — the button on the page, with a reason — or import a file that no
longer names it with `--replace`:

```sh
pixi run stack-run import_watchlist path/to/shorter.csv --replace
```

`--replace` retires every active row the file does not name, with a reason naming the
file (composed after `--reason`, when one is given). Without it, rows the file does not
name are left alone, which is what importing a *partial* file means. The scheduled
`import-watchlist` admin process runs with `--replace`, because the reviewed file is the
whole inventory. A file naming no packages at all is refused with `--replace` rather
than retiring everything.

**An import never reactivates a retired row.** A row somebody retired on the page is a
decision with a reason; a file that still names it has not caught up, and the import
leaves it retired and reports it as `retired kept`. Reactivation is a person's change
on the page — the edit form on a retired row — audited as the `retired` transition.

Nothing is deleted, from the table or from the database. Retirement is a column
(`retired_at`); the adapter answers active rows only; the next ingestion notices the
package is no longer listed and writes an **absence observation** — an inventory
snapshot saying `not_found`, carrying that run's timestamp. The package, its evidence,
its history and its inventory row all stay exactly where they are, and the row can be
reactivated by changing it (the page's edit form, or a later import that names it).

### What absence does

The `not_found` snapshot is what every downstream reader reads, **as of a policy run's
evidence cut-off** — never the `retired_at` column, which is governed data and not
cut-off bound. At the next policy run, a package the inventory has recorded `not_found` at the
cut-off with no `ok` observation since — the date it left is the *first* such
row, and only a later listing ends the absence; a failed look in between does not:

- **leaves the identity review queue.** The selection leaves it out rather than
  ranking it last, and the queue page states how many packages it is not offering
  for that reason.
- **keeps its rollup row**, stamped with `inventory_absent_since` and
  `inventory_last_listed`. Its statuses are computed exactly as before — absence
  gates nothing; an unmapped package is still gated by its confidence, never by its
  absence.
- **opens no new work**, in any queue, and **has its open items closed** by the
  product: each becomes `resolved` by a transition with `origin=system`, no actor,
  and a justification naming the absence and the last-listed date. A claimed item is
  released. Items already finished are left alone, and so are the items of a package
  whose rollup row failed to write this run — the closer reads every package with
  open work, not only the rows the run wrote
  ([the queues](the-queues.md#the-products-own-table-one-move-one-circumstance)).
- **is labelled wherever it appears** — the package page, the health list, a queue
  row, a report row — with "absent from the inventory since *date* (last listed
  *date*)", read from the rollup row. The last-listed clause is omitted when no
  `ok` snapshot survives — the nightly purge keeps a package's newest row per
  table and can remove every earlier listing, leaving the `not_found` alone — and
  the product's closing reason omits it on the same terms: the date it left is a
  fact the log still holds; the date it was last listed is not. The two surfaces
  that exclude it — the feedstock-gap report and the health list filtered to
  `feedstock=absent` — state the count and the reason above their rows; the health
  list also states how many `unmapped` packages report `unknown` rather than
  `absent` and are therefore never listed there.

Collectors still select on its mappings and go on observing it; absence is about the
queues and the labels, not about evidence.

**Reactivating the row reverses all of it, with no manual step.** The next ingestion
writes an `ok` snapshot, and the next policy run clears the two columns, drops the
label, offers the package to the identity queue again and opens *fresh* items — in
the identity queue, and in the remediation and compliance queues for any advisory or
licence still matching — while the items the product closed stay closed, because
each new item's key names the instant the new listing began. An evidence-backed key
is the advisory or the licence, the same row before and after, so without that
epoch a remediation item closed for absence would never re-open. A package never
gets a second *open* identity item, whatever its keys: if the purge later erodes the
history so the epoch is forgotten, the next run finds the open item and opens nothing.

Because every read is bound to the run's cut-off, a replayed run selects, stamps,
skips and closes exactly what the run it replays did, and closes nothing twice.

!!! warning "Absences are recorded only by a run that observed something"

    A run whose every record failed read a document it could not act on at all.
    Writing absences off the back of it would record every package the source *did*
    still list as departed — permanently, in a log nothing may correct.

    This is also why an **empty** inventory — a header-only file, or a table with no
    active row — is refused one step earlier and nothing is marked absent. An inventory
    naming nothing is indistinguishable from a source that has broken.

---

## A first deployment sees nothing, on purpose

`watchlist.csv` ships with its header and **no rows**, and the `inventory` table starts
empty. Which packages your organisation tracks is your decision, so nothing is invented
for you.

!!! warning "A local stack seeded before the table existed must be reset once"

    The demo seeder used to file its own package shells under a `pypi:<name>` key;
    the product's ingestion files them under the watchlist's key, and no package row
    is ever deleted. So a stack seeded before `CPM-OPERATE-S03` still holds the old
    shells, they collide with the new ones on `canonical_name`, and the seeder refuses
    before writing anything, saying so. `pixi run docker-down-v` discards the volume;
    `pixi run -e dev stack-seed` then seeds cleanly.

The consequence to plan for: **inventory ingestion fails on every run until the
inventory has something in it.** Reading the file, the task raises an
`ImproperlyConfigured` naming it; reading the table, it refuses the empty document. The
run's ledger row finalises `failed`, and no package and no snapshot is written.

Populate it by review — rows into `watchlist.csv`, then `import-watchlist`, then
`CPM_INVENTORY_SOURCE=database` — or one row at a time on the inventory page. A loud
failure on day one is the alternative to a silently corrupted evidence log.

---

## Identity, and correcting it

Ingestion creates a package **shell** at confidence `unmapped`. It asserts no mapping —
no repository, no purl, no feedstock — because an inventory row knows what a package
is called internally and nothing about what it *is*.

Resolution is what promotes it, and until it does, the confidence gate writes `unknown`
for every verdict about that package. Those packages are what the identity review queue
holds.

### The identity override

When the resolver gets it wrong, one endpoint corrects it:

```
POST /conda-sentinel/api/v1/packages/<package_id>/identity-override/
```

- Requires the **`leadership`** role.
- Requires a **reason**. Not optional, and not defaulted.
- Records **who** did it.

It is the only way to correct an identity. A wrong identity is corrected through the
override, never through the inventory: changing a row's `package_name` changes what the
*next* shell would be called, not what this package is.

!!! note "Two governed writes, three obligations each"

    `CPM-FR-3` names two human writes that mutate governed reference data — the
    identity override and the inventory — and puts the same three obligations on each:
    a permission, a required reason, and an audit row in the same transaction. There
    is no third. An admin form or a management command that changed either table with
    a different audit story would be a second place the rule lived.

!!! note "`associator_key` is not correctable and `canonical_name` is"

    The key is the stable value a later ingestion matches on. If it changed, the next
    ingestion would treat the row as a different package, create a second shell, and
    record the first as departed. The *name* is the correctable one.

---

## Checking your work

After changing the inventory, the questions worth asking in order:

| Ask | Where |
|---|---|
| Is the row there, and active? | `/conda-sentinel/inventory/` — and the audit row beneath says who and why |
| Did ingestion run, and how did it end? | The run ledger — `ok`, `partial` or `failed` |
| If `partial`, which rows failed? | The run's log names each one |
| Is the new package there? | `/conda-sentinel/packages/?q=<name>` |
| Is it identified yet? | Its confidence chip; `unmapped` means the gate is armed |
| Is anything being collected about it? | Its detail page — each verdict traces to the observation behind it |
| Which collectors *cannot* be asked about it yet? | The Coverage screen |

Locally you can do the whole loop without waiting for a schedule: add the row, then
`pixi run stack-run ingest_inventory`, `pixi run stack-run dispatch_sweep resolve_identity`
and `pixi run stack-run run_policy`. [How](running-it.md#running-a-policy-pass-yourself).
