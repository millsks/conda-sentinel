# The watchlist

The inventory's **first-run seed** and its reviewed bulk source (`CPM-AD-29`,
`CPM-FR-42`, `CPM-OPERATE-S03`). Two reviewed CSV files, read by
`collectors/watchlist.py`'s parser -- either directly by the file adapter, when
`CPM_INVENTORY_SOURCE=watchlist`, or by `manage.py import_watchlist`, which
brings the governed `inventory` table into line with a file one audited
transaction per row. Once imported, the table is what ingestion reads
(`CPM_INVENTORY_SOURCE=database`) and what the inventory page changes one row at
a time; the file is where a reviewed bulk change comes from.

The contract cannot live in the files themselves: the header must be **exactly**
the declared columns, so a `#` comment line is a refused row. It lives here, and
the same eight columns are the table's and the page's.

| File | Selected when | Contents |
|---|---|---|
| `watchlist.csv` | `COMPONENT_RUNTIME` is anything but `local` | The production watchlist. Ships with its header and no rows. |
| `watchlist-development.csv` | `COMPONENT_RUNTIME=local` | The development inventory: 148 rows, every name the demo roster carries among them. What `stack-seed` imports. |

Selection **fails closed toward production**: absent, empty and unrecognized
`COMPONENT_RUNTIME` values all read `watchlist.csv`. A deployed component that
read the development subset would find every package outside it missing and
record each one as absent, permanently, in a log nothing may correct. The
source setting fails closed the same way: absent means the file, and a
component that never ran `import-watchlist` keeps reading what it ships.

## Columns

The header is exactly these eight names, in any order, and nothing else. A column
this table does not define — a repository URL, a feedstock, a purl, a confidence
— is **refused**, not ignored: ingestion never asserts a mapping (`CPM-FR-1`),
and a silently dropped column is a reviewer who believes they supplied one.

| Column | Required | Meaning |
|---|---|---|
| `source_package_key` | yes | What this inventory files the package under. Becomes the package's `associator_key` — the stable value a later resolution matches on, and the one nothing corrects. Unique within the file; a repeat fails the run. Up to 512 characters. |
| `package_name` | yes | What the package is called. Becomes `canonical_name`, the one *correctable* name. Up to 128 characters. Two rows with different keys and the same name are a collision: the second fails, the sweep carries on, and the run finalizes `partial`. |
| `internal_component_count` | yes | How many internal components use it. |
| `internal_lob_count` | yes | How many internal lines of business use it. |
| `apps` | no | How many applications name it. |
| `platforms` | no | How many platforms it is used on. |
| `downloads` | no | Internal downloads. |
| `versions` | no | How many versions are in use. |

Together the two required counts are the internal usage breadth `CPM-FR-4` ranks
by, which is why a row cannot omit either.

**A blank optional cell is not a zero.** Blank records that the source did not
say and is stored as NULL; `0` records that the source counted none. Nothing in
this product collapses the two, and neither should an edit to these files.

Every count is a whole number written in ASCII digits, from `0` to
`2147483647`. A sign, a decimal point, a thousands separator and a non-ASCII
digit are all refused — `int()` would accept most of them, and a watchlist is
reviewed by reading it.

## Editing

The whole file is validated before any row is written: a malformed file fails the
run -- or refuses the import -- and leaves no package, no snapshot and no
inventory row behind (`CPM-FR-42`). Every refusal names this file and, where a
row is at fault, the line.

* No blank lines. A blank line after the header is refused rather than skipped.
* Every row has exactly eight cells.
* Save as UTF-8. A byte-order mark is tolerated, so a file round-tripped through
  Excel still reads.

## The development inventory's counts are illustrative

They are not observations. Nobody measured how many internal components use
`numpy`; the numbers are derived from a rough ubiquity ordering so that the file
is *internally coherent* — a package nearly every component pulls in carries a
larger breadth than a specialist one — and so that a developer running the
product sees a ranking that looks like a ranking. The specialist rows leave the
four optional signals blank on purpose, so the subset exercises the
missing-versus-zero distinction rather than only the populated case.

The two `internal-*` rows are keyed `internal/<name>` rather than
`conda-forge/<name>`: they are not conda-forge packages, and the key says so.
The resolver records `not_found` for them, which is the demo's honest `unmapped`
pair.

The production watchlist's content is an organizational decision and is populated
by review, then imported. Until it is, ingestion in a deployed component fails
loudly — which is the intended behaviour, not a gap: an inventory naming nothing
is indistinguishable from a source that has broken, and a sweep that accepted one
would record every package as departed.
