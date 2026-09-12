# Maintaining it

How a change gets in, what has to be true before it does, and why the suite refuses
some things that look fine.

## The gate

`pixi run ci` is the whole of it, in fail-fast order — static checks before the suite,
so a type error surfaces without paying for the tests:

```text
1  pixi run precommit    ruff format, ruff, mypy, over everything tracked
2  pixi run build        the package is distributable
3  pixi run typecheck    mypy over the full module graph, strict
4  pixi run lint         ruff, zero warnings
5  pixi run test-cov     the suite, coverage ≥ 90%, templates included
```

!!! warning "It cannot complete on macOS"

    `tests/integration/test_image_payload.py` shells out to `docker build`, and the
    child zombies. **This reproduces on unmodified `main`** — it is not your change.

    The substitute is the five steps individually, with that module deselected:

    ```bash
    pixi run precommit && pixi run build && pixi run typecheck && pixi run lint
    pixi run -e dev python -m pytest tests/ \
      --ignore=tests/integration/test_image_payload.py \
      --cov=src --cov-fail-under=90
    ```

    CI runs the real thing on Linux. Say in the pull request that you did this — every
    story in this repository does.

Two more that are not in `ci` and matter:

```bash
pixi run docs      # mkdocs build --strict — a broken doc link is a failure
pixi run format    # before staging, so pre-commit does not rewrite under you
```

## Running the product while you change it

`pixi run runserver` serves one process against SQLite with tasks running inline —
the right loop for a template or a view.

`pixi run local-stack` runs the product the way it actually runs: Redis and PostgreSQL
in containers, and gunicorn, a worker, beat and flower together. Use it when the
change touches the request boundary, a collector, a policy pass or an export — the
inline default hides every consequence of `CPM-AD-9`. See
[Running it](running-it.md#the-full-local-stack).

!!! note "Stage before you gate"

    `pre-commit` only sees **tracked** files. A new file that is not `git add`ed is
    not linted, not type-checked, and green locally. `git add -A` first.

## The local gate cannot see the CI matrix

You are on macOS. CI runs `ubuntu-latest`, `windows-latest` and `macos-latest`. A
green local run says nothing about a platform assumption, and that class of defect
reaches the pull request every time.

The one that recurs, twice in a single day at one point:

```python
str(path.relative_to(root))  # "a\b" on Windows, "a/b" everywhere else
path.relative_to(root).as_posix()  # always "a/b"
```

**Any test comparing a path as a string uses `as_posix()`.** Same for `parametrize`
ids. Check it before pushing any audit that walks the filesystem — it is invisible
locally and costs a full matrix round trip.

## The audits, and what each prevents

A dozen sweeps enforce rules no requirement asked for. They exist because **a rule
that is only written down decays**, and each is worth reading as the failure it
prevents rather than the rule it applies:

| Audit | Prevents |
|---|---|
| `test_confidence_gate_audit` | a pass claiming something about a package nobody identified |
| `test_derived_status_writability_audit` | a view editing a verdict instead of re-running the policy |
| `test_app_layering_audit` | `core` importing a domain app's tables, inverting the registry |
| `test_pagination_audit` | one endpoint returning `CPM-NFR-1`'s ten thousand packages |
| `test_permission_audit` | a surface deciding authorization for itself |
| `test_request_boundary_audit` | a page that hangs on a rate-limited third party |
| `test_api_contract_audit` | a `ModelViewSet` quietly adding a third write |
| `test_url_mounting_audit` | a route escaping the application's own prefix |
| `test_display_vocabulary` | a database column reaching a reader |
| `test_clock_audit` | a wall-clock read making an audit row untestable |
| `test_evidence_constraint_audit` | evidence that can be written without saying when it was observed |
| `test_documentation_references` | a documentation pointer that silently stops resolving |
| `test_stylesheet_overflow` | a wide table scrolling the page instead of itself |
| `test_local_stack` | a local stack whose processes start and connect to nothing |
| `test_documentation_commands` | a documented command that does not exist |

Two more govern the suite itself:

- **`test_suite_policy`** bans `pytest.skip` in a test body. A skipped case reads in a
  report as a gate that ran.
- **`test_coverage_policy`** bans `# pragma: no cover`. A line excluded from the floor
  is a line nobody has to justify.

### When an audit blocks you

It is usually right. Before working around one, read the failure message — they are
written to name the *failure*, not the rule, so the message usually contains the
argument for doing it the other way.

If it genuinely is wrong, the pattern is a **recorded exemption**: an entry naming
the thing and why, plus a companion case asserting the exemption is still real.
`RECORDED_UNGATED_SURFACES` in `test_permission_audit` is the shape — one entry, and
a case that fails if it stops being needed. A stale exemption is one the next thing
in that module inherits.

## Every change carries a test

New behaviour gets a case that exercises it. Changed behaviour updates the cases that
covered it. Deleted behaviour deletes them.

Two habits that make the rest of this codebase readable:

**Assert the requirement, not the count.** `assert slugs == ["kev", "feedstock-lag", …]`
survives a refactor that drops one; `assert len(reports) == 6` passes at five after
somebody adds one and removes two.

**Say what the failure would be.** A docstring that explains *what breaks* when the
case fails is what tells the next person whether to fix the code or the test.

## Render it and run it

Two of four defects in one epic were found by opening the page or starting the server
— not by any test. A green suite proves the code does what the tests say. It does not
prove the page says something true.

Before shipping a change to a surface: start it, open it, read it.

## Shipping a story

1. Branch — `feature/`, `bugfix/` or `hotfix/`.
2. Build it, with tests.
3. Run the gate (the substitute procedure above).
4. Write the Dev Agent Record into the story file and flip its status.
5. Update `sprint-status.yaml` **in the same commit** — two of these needed a
   follow-up chore PR for two lines before that became the rule.
6. Conventional commit; push; open a pull request that says what you ran.
7. Watch the checks. Merge on green.
8. Sync `main`, delete the branch, then start the next story.

## Where the rules live

| Question | Answer |
|---|---|
| what the product must do | `_bmad-output/planning-artifacts/prds/` |
| why it is built this way | the architecture spine, distilled in [How it is built](architecture.md) |
| what a story delivered | `_bmad-output/implementation-artifacts/stories/` |
| what is next | `_bmad-output/implementation-artifacts/sprint-status.yaml` |

The story records are **dated**: one names a file at line numbers that were true when
it ran. They are a record of the past, not a pointer to the present, which is why the
documentation-reference audit deliberately does not sweep them.
