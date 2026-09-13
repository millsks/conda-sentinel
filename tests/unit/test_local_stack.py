"""`CPM-PLATFORM-S03`: the local stack, and the two ways it fails without saying so.

`pixi run local-stack` brings up Redis and PostgreSQL in containers and runs the web
process, a worker, beat and flower from the pixi environment. The web process is the
**deployed** command -- gunicorn with the draining uvicorn worker -- because a stack
whose purpose is to run the product the way it actually runs should serve it the way
production does. Both of its defects
during development were **quiet** — the stack started, printed banners, and did
nothing — which is why they are cases here rather than notes.

**A bare `pixi run worker` reads deployed settings.** `COMPONENT_RUNTIME=local` comes
from `[feature.dev.activation.env]` and from nowhere else;
`tests/unit/test_locality_declaration.py` fails the gate on any task that declares it.
So a `Procfile` line without `-e dev` resolves in `default`, finds no broker URL, and
falls back to Celery's built-in `amqp://guest@localhost:5672`. The worker then starts,
drains the right queue *names*, and consumes nothing, because it is connected to a
RabbitMQ that is not running.

**The containers publish on 6380 and 5433, not 6379 and 5432.** A developer machine
very often already has a Redis on the default port, and this product calls
`cache.clear()`, which flushes an entire Redis database. A stack that talked to
"whatever was already there" would be one bad afternoon away from flushing somebody
else's project.

**None of the stack's tasks is a deployment process, and that is asserted rather than
assumed.** `component.toml` declares the process group and
`tests/unit/test_process_model.py` reconciles it in both directions. These tasks are
development conveniences: `local-stack` is not a process type this component deploys,
and flower is a monitor rather than one of `[[admin_processes]]` — that table requires
a Django management command, which flower is not. Without the cases below, the next
person to read the process-model test would "fix" the omission by declaring them.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any
from typing import Final

import pytest
import yaml

#: The repository root, from this file rather than from a layout assumption.
REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
PIXI: Final[Path] = REPO_ROOT / "pixi.toml"
PROCFILE: Final[Path] = REPO_ROOT / "Procfile"
COMPOSE: Final[Path] = REPO_ROOT / "compose.yaml"
STACK_DOWN: Final[Path] = REPO_ROOT / "scripts" / "local-stack-down.sh"

#: The tasks that make up the local stack and must stay outside the process group.
OUTSIDE_THE_PROCESS_GROUP: Final[tuple[str, ...]] = (
    "local-stack",
    "local-stack-down",
    "stack-migrate",
    "stack-personas",
    "stack-seed",
    "stack-shell",
    "stack-run",
    "flower",
    "docker-up",
    "docker-down",
    "docker-down-v",
    "docker-logs",
    "docker-ps",
    "docker-psql",
    "docker-redis",
)

#: The variable that makes a task a member of the deployment's process group.
THE_MEMBERSHIP_MARKER: Final[str] = "COMPONENT_PROCESS"

#: Every task that runs *against* the local stack's containers.
#:
#: They are a set rather than one task because `CPM-PLATFORM-S06`: `migrate`,
#: `seed-personas` and `seed-demo` run against whatever the default environment
#: resolves -- SQLite -- while `local-stack` runs against the compose PostgreSQL. Two
#: databases, and nothing said so, so the documented first-run sequence seeded one and
#: started the other: a hundred packages in a file nothing was reading, and no personas
#: in the database serving the screens, which left no way to sign in and find out.
#:
#: The fix was three tasks carrying the stack's own environment, and the risk the fix
#: creates is four copies of one database URL. That is the shape the defect had, so
#: `test_every_stack_task_names_the_same_database` reconciles them.
#:
#: `stack-shell` and `stack-run` are the fifth and sixth copies (`CPM-OPERATE-S01`):
#: the operator's shell and management command. Before them, every hand-run action
#: was a `manage.py shell -c` that had to carry the three variables by hand, or it
#: silently talked to SQLite and ran the task inline in the shell.
AGAINST_THE_STACK: Final[tuple[str, ...]] = (
    "local-stack",
    "stack-migrate",
    "stack-personas",
    "stack-seed",
    "stack-shell",
    "stack-run",
)

#: The flag that decides whether a task enqueued from a stack task runs inline.
THE_EAGER_FLAG: Final[str] = "CELERY_TASK_ALWAYS_EAGER"

#: The one stack task that runs tasks inline on purpose.
#:
#: The seeder resolves identity live and executes a policy run as part of seeding; it
#: is a batch job that must finish before it returns, not a client of the worker.
THE_DELIBERATELY_EAGER_TASK: Final[str] = "stack-seed"

#: The operator's way in: a shell and an arbitrary management command against the stack.
THE_OPERATORS_TASKS: Final[tuple[str, ...]] = ("stack-shell", "stack-run")

#: The pages that tell somebody how to run something against the stack.
#:
#: This product's pages and the README; not `docs/accelerator/`, which is inherited
#: and documents a different database. At the time of writing the sweep found
#: snippets on three pages -- `running-it.md`, `asynchronous-work.md`,
#: `onboarding.md` -- plus `operations.md`, whose `manage shell` spelling the first
#: draft of the sweep missed; hence the tuple of needles below.
INSTRUCTIONAL_PAGES: Final[tuple[Path, ...]] = (
    *sorted((REPO_ROOT / "docs" / "conda-sentinel").rglob("*.md")),
    REPO_ROOT / "docs" / "index.md",
    REPO_ROOT / "README.md",
)

#: The recipes the operator's tasks replace: an environment assembled by hand.
#:
#: Both the `export` form and the prefix form -- the recipe this story removed from
#: `running-it.md` was `DATABASE_URL="..." REDIS_URL="..." pixi run ...`, with no
#: `export` in it.
HAND_ASSEMBLED_ENVIRONMENTS: Final[tuple[str, ...]] = (
    "export DATABASE_URL",
    "export REDIS_URL",
    "DATABASE_URL=",
    "REDIS_URL=",
)

#: A shell that does not carry the stack's environment, in each spelling a page uses.
BARE_SHELLS: Final[tuple[str, ...]] = ("manage.py shell", "manage shell", "django-admin shell")

#: The word a bare shell's paragraph must carry to say it is aimed at SQLite on purpose.
THE_ANNOTATION: Final[str] = "sqlite"

#: Host ports the compose services must **not** publish on.
#:
#: The defaults. See the module docstring: a stack that reached whatever was already
#: listening would eventually flush a Redis database belonging to something else.
PORTS_TOO_LIKELY_TO_BE_TAKEN: Final[frozenset[str]] = frozenset({"6379", "5432"})


def manifest() -> dict[str, Any]:
    """Return the parsed pixi manifest.

    Returns:
        The whole manifest.

    """
    return tomllib.loads(PIXI.read_text(encoding="utf-8"))


def tasks() -> dict[str, Any]:
    """Return every declared task, across the root table and every feature.

    Returns:
        Task name to its definition.

    """
    parsed = manifest()
    found: dict[str, Any] = dict(parsed.get("tasks", {}))
    for feature in parsed.get("feature", {}).values():
        found.update(feature.get("tasks", {}))
    return found


def procfile_lines() -> dict[str, str]:
    """Return the `Procfile`'s processes.

    Returns:
        Process name to the command honcho runs for it.

    """
    found: dict[str, str] = {}
    for line in PROCFILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        name, _, command = stripped.partition(":")
        found[name.strip()] = command.strip()
    return found


# ---------------------------------------------------------------------------
# The quiet failure: a process that starts and does nothing.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("process", sorted(procfile_lines()))
def test_every_stack_process_runs_in_the_dev_environment(process: str) -> None:
    """`-e dev`, or the process reads deployed settings and connects to nothing.

    This is the defect that shipped in the first draft. A worker without it starts,
    prints a banner listing the right queue names, and consumes nothing — because
    `COMPONENT_RUNTIME` came from `default`, the settings had no broker URL, and
    Celery fell back to `amqp://guest@localhost:5672`.

    Nothing errors. The stack looks like it is running.

    Args:
        process: The `Procfile` process name.

    """
    command = procfile_lines()[process]

    assert command.startswith("pixi run -e dev "), (
        f"{process!r} runs {command!r}. Without `-e dev` it resolves in the `default` environment, reads "
        f"deployed settings, and finds no broker -- and says so nowhere."
    )


@pytest.mark.parametrize("process", sorted(procfile_lines()))
def test_every_stack_process_delegates_to_a_declared_task(process: str) -> None:
    """The `Procfile` names tasks; it does not spell commands.

    `pixi.toml` is where a process's command is declared and `component.toml`
    reconciles the deployed ones against it. A command written out here would be a
    second spelling, and the two would disagree the first time somebody changed a
    queue name or a scheduler.

    Args:
        process: The `Procfile` process name.

    """
    task = procfile_lines()[process].removeprefix("pixi run -e dev ").strip()

    assert task in tasks(), f"{process!r} runs {task!r}, which `pixi.toml` does not declare."


# ---------------------------------------------------------------------------
# The deployment contract: these are conveniences, not process types.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("task", OUTSIDE_THE_PROCESS_GROUP)
def test_no_local_stack_task_joins_the_deployment_process_group(task: str) -> None:
    """Declared exclusion, so nobody later "fixes" it by adding a declaration.

    `component.toml` declares the process group and `test_process_model.py`
    reconciles it in both directions -- no declared process may name a task that does
    not exist, and no task in the group may go undeclared. A `COMPONENT_PROCESS` on
    any of these would put a local convenience into the deployment contract.

    Flower is the one worth spelling out: it is a monitor, and the obvious home would
    be `[[admin_processes]]` -- except that table requires a Django management
    command, which flower is not.

    Args:
        task: The task that must stay outside the group.

    """
    declared = tasks()
    assert task in declared, f"{task!r} is listed here and `pixi.toml` does not declare it."

    assert THE_MEMBERSHIP_MARKER not in declared[task].get("env", {}), (
        f"{task!r} declares {THE_MEMBERSHIP_MARKER}, which makes it a member of the deployment's process group. "
        f"It is a local development convenience; the group is what this component *deploys*."
    )


def test_the_stack_runs_the_application_outside_the_containers() -> None:
    """Compose brings up infrastructure; pixi runs the application.

    A `web` service here would mean a rebuild on every edit and a second, slower way
    to run what pixi already runs -- and the pixi environment is the one runtime. It
    is also what keeps the compose file honest about being local-only: `Dockerfile`
    is what builds the deployable image.
    """
    services = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]

    assert set(services) == {"redis", "postgres"}, sorted(services)


# ---------------------------------------------------------------------------
# The port choice, which is a safety property rather than a preference.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("service", ["redis", "postgres"])
def test_no_service_publishes_on_a_port_something_else_probably_holds(service: str) -> None:
    """6379 and 5432 are taken on a developer machine more often than not.

    Publishing onto one either fails to bind or -- worse -- the stack talks to
    whatever was already there. This product calls `cache.clear()`, which flushes an
    entire Redis database, so "whatever was already there" is not a risk worth the
    convenience of a default port.

    Args:
        service: The compose service.

    """
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    published = {entry.split(":")[0] for entry in compose["services"][service]["ports"]}

    assert not published & PORTS_TOO_LIKELY_TO_BE_TAKEN, (
        f"{service} publishes on {sorted(published & PORTS_TOO_LIKELY_TO_BE_TAKEN)}, which a developer machine "
        f"very often already has something on."
    )


def test_the_stack_points_at_the_containers_it_started() -> None:
    """Explicitly, rather than by hoping the defaults match.

    `REDIS_URL` defaults to `localhost:6379` and the database defaults to SQLite. A
    stack that left both alone would talk to neither of the containers it just
    brought up.
    """
    stack = tasks()["local-stack"]
    env = stack.get("env", {})

    assert "6380" in env.get("REDIS_URL", ""), env
    assert "5433" in env.get("DATABASE_URL", ""), env
    assert env.get("CELERY_TASK_ALWAYS_EAGER") == "0", (
        "local settings run tasks inline by default, which would leave the worker, beat and flower idle while "
        "the web process quietly did their work."
    )
    assert "docker-up" in stack.get("depends-on", []), stack


@pytest.mark.parametrize("task", AGAINST_THE_STACK)
def test_every_stack_task_names_the_same_database(task: str) -> None:
    """Four copies of one URL, which is the shape the defect this prevents had.

    `migrate` and the two seeders run against the *default* environment -- SQLite --
    and `local-stack` runs against the compose PostgreSQL. Following the documented
    first-run sequence and then starting the stack produced a product with no packages
    and no personas: nothing to look at, and no way to sign in and discover that.

    A fifth task added later that seeded the wrong database would reproduce it exactly,
    and nothing else in this suite would notice -- the seeding would succeed, the stack
    would start, and the screens would simply be empty.

    Args:
        task: The task under test.

    """
    env = tasks()[task].get("env", {})

    assert "5433" in env.get("DATABASE_URL", ""), f"{task} does not run against the stack's PostgreSQL: {env}"
    assert "6380" in env.get("REDIS_URL", ""), f"{task} does not point at the stack's Redis: {env}"


def stack_tasks_that_say_whether_tasks_run_inline() -> list[str]:
    """Return the `AGAINST_THE_STACK` tasks whose `env` carries the eager flag.

    A task without it leaves the decision to the settings, which is `stack-migrate`'s
    and `stack-personas`' position: neither enqueues anything. Runs at collection
    time, so a roster name the manifest does not declare, or declares as a bare
    string, is left out here rather than raised -- `test_every_stack_task_names_the_
    same_database` is the case that names it.

    Returns:
        The task names, in roster order.

    """
    declared = tasks()
    return [
        task
        for task in AGAINST_THE_STACK
        if isinstance(declared.get(task), dict) and THE_EAGER_FLAG in declared[task].get("env", {})
    ]


def test_the_deliberate_exception_is_still_a_task_that_says() -> None:
    """`stack-seed` must stay in the eager sweep, or its `"1"` is asserted nowhere.

    The case below names the seeder as the one task allowed to run inline. If its
    flag were dropped the case would simply not be generated for it, and the
    contract that the seeder finishes before it returns would be silently gone.
    """
    assert THE_DELIBERATELY_EAGER_TASK in stack_tasks_that_say_whether_tasks_run_inline()


@pytest.mark.parametrize("task", stack_tasks_that_say_whether_tasks_run_inline())
def test_every_stack_task_that_says_whether_tasks_run_inline_agrees_with_the_stack(task: str) -> None:
    """A shell that runs tasks inline is the other half of the wrong-database defect.

    A shell carrying no environment writes to SQLite and runs every task it enqueues
    inline rather than on the stack's worker (the `pixi.toml` comment above
    `stack-shell` records the afternoon that happened). `local-stack` sets the flag
    to `"0"`; a stack task that set it any other way would enqueue to nowhere and
    quietly do the worker's work itself.

    `stack-seed` is the one deliberate exception and is named here rather than
    special-cased silently: it is a batch job that must finish before it returns.

    Args:
        task: The task under test.

    """
    declared = tasks()
    env = declared[task]["env"]
    stack_env = declared["local-stack"].get("env", {})
    assert THE_EAGER_FLAG in stack_env, f"`local-stack` no longer says whether tasks run inline: {stack_env}"

    if task == THE_DELIBERATELY_EAGER_TASK:
        assert env[THE_EAGER_FLAG] == "1", f"{task} runs its seeding inline on purpose; {env}"
        return

    assert env[THE_EAGER_FLAG] == stack_env[THE_EAGER_FLAG], (
        f"{task} disagrees with `local-stack` on {THE_EAGER_FLAG}: a task enqueued from it would run inline rather "
        f"than on the stack's worker. {env}"
    )


@pytest.mark.parametrize("task", AGAINST_THE_STACK)
def test_every_stack_task_carries_the_stacks_environment_byte_for_byte(task: str) -> None:
    """Equal to `local-stack`'s values, not merely naming the same ports.

    `test_every_stack_task_names_the_same_database` reads the port out of the URL; a
    value that drifted in some other part -- a different database name, a different
    Redis database index -- would still pass it and still put the task against a
    database the stack does not serve. The eager flag is compared wherever a task
    declares it -- `stack-migrate` and `stack-personas` enqueue nothing and leave it
    to the settings -- except on the seeder, whose `"1"` has its own case.

    Args:
        task: The task under test.

    """
    declared = tasks()
    stack_env = declared["local-stack"].get("env", {})
    for key in ("DATABASE_URL", "REDIS_URL", THE_EAGER_FLAG):
        assert key in stack_env, f"`local-stack` no longer declares {key}: {stack_env}"
    env = declared[task].get("env", {})

    compared = ["DATABASE_URL", "REDIS_URL"]
    if THE_EAGER_FLAG in env and task != THE_DELIBERATELY_EAGER_TASK:
        compared.append(THE_EAGER_FLAG)
    for key in compared:
        assert env.get(key) == stack_env[key], f"{task} differs from `local-stack` on {key}: {env.get(key)!r}"


@pytest.mark.parametrize("task", THE_OPERATORS_TASKS)
def test_the_operators_tasks_are_declared_as_the_spec_says(task: str) -> None:
    """`manage.py shell` and a bare `manage.py`, in the dev environment.

    `manage` runs in `default`, which cannot see the domain apps; a shell there fails
    with a model that "doesn't declare an explicit app_label". `stack-run` is `manage`'s
    own command -- read from it, so the two cannot drift -- so that whatever follows is
    appended by pixi the way it is for `manage`.

    Args:
        task: The operator's task.

    """
    declared = tasks()
    assert isinstance(declared.get(task), dict), f"{task!r} is not a task table in `pixi.toml`: {declared.get(task)!r}"
    assert isinstance(declared.get("manage"), dict), "`manage` is the pattern `stack-run` follows and is gone"
    expected = {"stack-shell": f"{declared['manage']['cmd']} shell", "stack-run": declared["manage"]["cmd"]}[task]

    assert declared[task]["cmd"] == expected, declared[task]
    assert declared[task].get("default-environment") == "dev", declared[task]


@pytest.mark.parametrize("task", THE_OPERATORS_TASKS)
def test_the_operators_tasks_carry_exactly_the_stacks_three_variables(task: str) -> None:
    """The three `local-stack` declares, and no fourth.

    The values are reconciled for every stack task above; this is the count. A
    fourth variable would be a setting the stack itself does not run under, so the
    shell would be against a stack that differs from the one serving the screens.

    Args:
        task: The operator's task.

    """
    env = tasks()[task].get("env", {})

    assert set(env) == {"DATABASE_URL", "REDIS_URL", THE_EAGER_FLAG}, (
        f"{task} carries more than the stack's three: {env}"
    )


@pytest.mark.parametrize("task", THE_OPERATORS_TASKS)
def test_the_operators_tasks_start_nothing(task: str) -> None:
    """No `depends-on`: a stopped stack must not come up as a side effect of a shell.

    `stack-migrate` depends on `docker-up` because a migration with no database is
    meaningless. A shell is different: Django connects lazily, so against a stopped
    stack the shell opens and PostgreSQL's refusal arrives at the first query, and a
    `.delay()` retries the broker before giving up. Both are the honest answer,
    rather than starting containers the operator did not ask for.

    Args:
        task: The operator's task.

    """
    assert "depends-on" not in tasks()[task], tasks()[task]


def test_the_stack_migrates_before_it_serves() -> None:
    """So a fresh clone's first `local-stack` finds a schema rather than no tables.

    The containers come up empty. Without this, the first command somebody runs after
    cloning starts four processes against a database with none of this product's
    fifty-two tables, and every one of them fails in a different way.
    """
    assert "stack-migrate" in tasks()["local-stack"].get("depends-on", [])


def test_seeding_is_not_something_the_stack_does_on_every_start() -> None:
    """Evidence is append-only, so seeding twice appends rather than replacing.

    A stack that seeded on start-up would add a second observation of every seeded fact
    on every restart -- which is realistic behaviour for the seeder and absurd
    behaviour for a start-up task.
    """
    assert "stack-seed" not in tasks()["local-stack"].get("depends-on", [])


# ---------------------------------------------------------------------------
# The documentation: every hand-run action is one of the operator's tasks.
# ---------------------------------------------------------------------------


def paragraphs(page: Path) -> list[tuple[int, str]]:
    """Return the page's blank-line-delimited blocks with the line each starts on.

    Args:
        page: The Markdown page.

    Returns:
        `(first line number, block text)` pairs, in page order.

    """
    found: list[tuple[int, str]] = []
    block: list[str] = []
    start = 0
    for number, line in enumerate(page.read_text(encoding="utf-8").splitlines(), start=1):
        if line.strip():
            if not block:
                start = number
            block.append(line)
        elif block:
            found.append((start, "\n".join(block)))
            block = []
    if block:
        found.append((start, "\n".join(block)))
    return found


def test_no_page_asks_the_reader_to_assemble_the_stacks_environment_by_hand() -> None:
    """`export DATABASE_URL=...`, or the prefix form, is the recipe that got the wrong database once.

    A first-run instruction that asks somebody to hand-assemble an environment
    variable is one they will get wrong; the `stack-*` tasks carry it, which is what
    tasks are for. Lists every offending line rather than the first.
    """
    offending = [
        f"{page.relative_to(REPO_ROOT)}:{number}: {line.strip()}"
        for page in INSTRUCTIONAL_PAGES
        for number, line in enumerate(page.read_text(encoding="utf-8").splitlines(), start=1)
        if any(needle in line for needle in HAND_ASSEMBLED_ENVIRONMENTS)
    ]

    assert not offending, "\n".join(offending)


def test_every_bare_shell_the_documentation_names_says_it_is_aimed_at_sqlite() -> None:
    """A `manage.py shell` aimed at the stack is `stack-shell`; any other says why not.

    One exercise on the onboarding page opens a shell against SQLite deliberately --
    it reads the collector registry, which is code rather than rows -- and says so.
    Every other mention must be the annotation of that trap, not an instance of it.
    The annotation is looked for in the paragraph rather than on the physical line,
    so a reflow cannot break it. Lists every offending paragraph rather than the
    first.
    """
    offending = [
        f"{page.relative_to(REPO_ROOT)}:{number}: {block.splitlines()[0].strip()}"
        for page in INSTRUCTIONAL_PAGES
        for number, block in paragraphs(page)
        if any(needle in block for needle in BARE_SHELLS) and THE_ANNOTATION not in block.lower()
    ]

    assert not offending, "\n".join(offending)


def test_the_sweep_read_the_pages_it_is_about() -> None:
    """So the two cases above cannot pass by sweeping nothing."""
    assert any(page.name == "running-it.md" for page in INSTRUCTIONAL_PAGES), INSTRUCTIONAL_PAGES
    assert any(page.name == "onboarding.md" for page in INSTRUCTIONAL_PAGES), INSTRUCTIONAL_PAGES
    assert all(page.is_file() for page in INSTRUCTIONAL_PAGES), INSTRUCTIONAL_PAGES


# ---------------------------------------------------------------------------
# The way down when honcho did not take its children with it.
# ---------------------------------------------------------------------------


def test_the_way_down_is_the_script_and_the_script_is_scoped_to_this_checkout() -> None:
    """`local-stack-down` finds stragglers by this checkout's environment, not by name.

    Closing the terminal, or `kill -9` on honcho, reparents gunicorn, flower, beat
    and the worker to PID 1, where they hold 8000 and 5555 until somebody finds them
    by hand. The script is what finds them -- and it must match on the interpreter
    path under *this* repository's `.pixi/envs/dev`, because the sibling repository's
    stack runs a worker with the same `-A config.celery_app`, and a match on the
    command name alone would kill that one too.
    """
    task = tasks()["local-stack-down"]
    # `as_posix()`: the `cmd` is a literal with forward slashes, and `relative_to`
    # alone renders a backslash on the Windows compatibility job.
    assert task["cmd"] == f"bash {STACK_DOWN.relative_to(REPO_ROOT).as_posix()}", task

    script = STACK_DOWN.read_text(encoding="utf-8")
    assert ".pixi/envs/dev/bin/(gunicorn|celery)" in script, "the match is on this checkout's environment path"
    assert "pkill -TERM" in script, "a warm shutdown first: an idle worker exits cleanly on TERM"
    assert "pkill -KILL" in script, "a worker's warm shutdown waits for in-flight tasks, and a stuck one waits forever"
    assert "pixi run docker-down" in script, "the containers come down last, so the worker is not left reconnecting"


def test_the_way_down_does_not_discard_the_data() -> None:
    """`docker-down`, not `docker-down-v`: stopping the stack is not resetting it.

    The seeded inventory is append-only evidence that took a command to produce.
    A stop that discarded it would make `stack-seed` a prerequisite of every
    restart, which is the trap `local-stack` itself deliberately avoids.
    """
    commands = [
        line.strip()
        for line in STACK_DOWN.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]

    assert "pixi run docker-down" in commands, commands
    assert not any("docker-down-v" in command for command in commands), commands


@pytest.mark.parametrize("path", [PIXI, PROCFILE, COMPOSE, STACK_DOWN], ids=lambda path: path.name)
def test_the_files_this_module_reads_are_where_it_thinks(path: Path) -> None:
    """So every case above cannot pass by parsing nothing.

    Args:
        path: The file that must exist.

    """
    assert path.is_file(), path
