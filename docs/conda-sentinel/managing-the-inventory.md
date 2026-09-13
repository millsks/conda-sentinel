# Managing the inventory

Adding a package, removing one, and correcting an identity the resolver got wrong.

---

## What the inventory is

The list of packages this product watches is a **reviewed CSV file shipped inside the
wheel** — not a database table, not an admin screen, and not an API.

```
src/django_apps/conda_sentinel/collectors/data/
├── watchlist.csv               ← deployed. Ships with a header and no rows.
├── watchlist-development.csv   ← read only when COMPONENT_RUNTIME=local
└── README.md                   ← the column contract, beside the files
```

Which file is read is selected by **locality**, and it fails closed toward production:
`COMPONENT_RUNTIME=local` reads the development subset, and *everything else* — absent,
empty, or a value like `dev` — reads `watchlist.csv`.

That asymmetry is not fussiness. A deployed component that read the development subset
would find every package outside that subset missing and record each one as **absent**,
permanently, in an append-only log nothing may correct.

### Why a file and not a table

Because adding a package is a decision somebody should review. A CSV in the repository
gets a pull request, a diff, an approver and a history; an admin form gets a Tuesday
afternoon and no record of why. The inventory is governed reference data, and governed
reference data has one write path.

---

## Adding a package

1. Add a row to `watchlist.csv` (or, for local work, `watchlist-development.csv`).
2. Open a pull request. The gate validates the file.
3. Once deployed, the next inventory ingestion creates the package shell and its
   inventory snapshot.
4. From then on, each collector's sweep offers it as soon as its identity resolution
   has reached the mapping that collector reads.

### The eight columns

The header is **exactly** these eight names, in any order, and nothing else:

| Column | Required | Meaning |
|---|---|---|
| `source_package_key` | yes | What the inventory files it under. Becomes `associator_key` — the stable value nothing corrects. Unique within the file. |
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
    that the source counted none. Nothing in this product collapses the two, and
    neither should an edit to these files.

    The two required counts are the internal usage breadth the priority pass ranks by,
    which is why a row cannot omit either.

The full contract — bounds, encoding, what a malformed row does — is in
`src/django_apps/conda_sentinel/collectors/data/README.md`, deliberately beside the
files rather than only here.

### The whole file is validated before anything is written

A malformed file fails the run and leaves **no package and no snapshot** behind. Every
refusal names the file and, where a row is at fault, the line.

Two rows with different keys and the same `package_name` are a collision: the second
row fails, the sweep carries on, and the run finalises `partial`. That is the
difference between a bad row and a bad file — one loses a package, the other loses
nothing.

---

## Removing a package

**Delete its row and open a pull request.** What happens next is the part worth
understanding.

Nothing is deleted from the database. The next ingestion notices the package is no
longer listed and writes an **absence observation** — a row saying "the source no
longer lists this package", carrying this run's timestamp. The package, its evidence
and its history all stay exactly where they are.

That is not a compromise. Evidence is append-only: what a source said at an instant
cannot stop being true, and a product that deleted the record of a package it used to
watch could not answer "what did we know about this in March".

!!! warning "Absences are recorded only by a run that observed something"

    A run whose every record failed read a document it could not act on at all.
    Writing absences off the back of it would record every package the source *did*
    still list as departed — permanently, in a log nothing may correct.

    This is also why an **empty** inventory document is refused one step earlier, and
    why `watchlist.csv` shipping with no rows makes ingestion fail loudly rather than
    quietly succeed. An inventory naming nothing is indistinguishable from a source
    that has broken.

---

## A first deployment sees nothing, on purpose

`watchlist.csv` ships with its header and **no rows**. Which packages your organisation
tracks is your decision, so nothing is invented for you.

The consequence to plan for: **inventory ingestion fails on every run until that file
is reviewed in.** The task raises an `ImproperlyConfigured` naming the file, the run's
ledger row finalises `failed`, and no package and no snapshot is written.

A loud failure on day one is the alternative to a silently corrupted evidence log.

---

## Identity, and correcting it

Ingestion creates a package **shell** at confidence `unmapped`. It asserts no mapping —
no repository, no purl, no feedstock — because an inventory file knows what a package
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

It is the only way to correct an identity, and it is deliberately narrow. `CPM-AD-14`
gives governed reference data exactly one write path; a second one — an admin form, a
management command — would be a second place identity could change with a different
audit story.

!!! note "`associator_key` is not correctable and `canonical_name` is"

    The key is the stable value a later resolution matches on. If it changed, the next
    ingestion would treat the row as a different package, create a second shell, and
    record the first as departed. The *name* is the correctable one.

---

## Checking your work

After changing the watchlist, the questions worth asking in order:

| Ask | Where |
|---|---|
| Did ingestion run, and how did it end? | The run ledger — `ok`, `partial` or `failed` |
| If `partial`, which rows failed? | The run's log names each one |
| Is the new package there? | `/conda-sentinel/packages/?q=<name>` |
| Is it identified yet? | Its confidence chip; `unmapped` means the gate is armed |
| Is anything being collected about it? | Its detail page — each verdict traces to the observation behind it |
| Which collectors *cannot* be asked about it yet? | The Coverage screen |

Locally you can do the whole loop without waiting for a schedule: edit
`watchlist-development.csv`, then `pixi run stack-run ingest_inventory` and
`pixi run stack-run run_policy`. [How](running-it.md#running-a-policy-pass-yourself).
