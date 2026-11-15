# Demo: HR schema + PL/SQL (combining both ingestors)

An HR-style schema and a small but real PL/SQL layer on top of it, built to show
what you get by running the SQL and PL/SQL ingestors against the *same* database:
one connected graph, not two separate ones. Everything here is hand-authored --
`setup.py` builds it from scratch against your own Oracle instance (unlike the
[Chinook demo](../chinook/README.md), this can't be a downloaded file, since PL/SQL only
exists on Oracle and needs a live instance to even define it).

## What's in it

Tables (regions/countries/locations/departments/jobs shape follows Oracle's classic
HR sample schema; `job_history`/`audit_log` and everything procedural is original):

| Table | Notes |
|---|---|
| `regions`, `countries`, `locations` | simple reference chain, FK'd together |
| `jobs` | `min_salary`/`max_salary` -- checked by a trigger below |
| `departments` | FK's to `locations`; `manager_id` FK's to `employees` |
| `employees` | FK's to `jobs`/`departments`, self-FK's to `employees` (manager), `UNIQUE(email)`, `CHECK(salary > 0)` |
| `job_history` | composite primary key (`employee_id`, `start_date`) |
| `audit_log` | written to by the package and one of the triggers |

Plus an index (`ix_employees_last_name`), two sequences (`employees_seq`,
`audit_log_seq`), and a view (`employee_directory`) -- so the SQL ingestor side
alone already exercises every entity type it supports.

PL/SQL layer:

- **`hr_pkg`** (package): `hire_employee`, `give_raise` procedures; `get_department_headcount`,
  `get_average_salary` functions; an **overloaded** `log_change` (one variant logs to
  `job_history`, the other to `audit_log` -- two genuinely different behaviors behind
  one name, not just a token example).
- **`transfer_employee`** (standalone procedure) calls `hr_pkg.log_change` -- a
  standalone routine calling into a package.
- **`trg_employees_salary_check`** (trigger, `BEFORE INSERT OR UPDATE OF salary`):
  has a `DECLARE` section and reads `jobs` to validate the new salary is in range.
- **`trg_employees_audit_insert`** (trigger, `AFTER INSERT`): writes to `audit_log`.

## What ingesting it produces

Running both ingestors back to back writes **88 nodes / 120 edges**, and running
either one again afterward changes nothing (both are idempotent). A few things
worth looking at directly in the [viewer](../../README.md#viewing-ingested-graphs)
or via Cypher:

- **`transfer_employee` has two `DEPENDS_ON` edges to different targets that both
  describe the same real call**: a coarse one to the whole `hr_pkg` `Package`
  (`detected_via: "catalog"`, from Oracle's own `ALL_DEPENDENCIES` -- it only knows
  "this depends on that package," not which routine inside it), and a precise one
  straight to the `log_change` `Procedure` (`detected_via: "parsed", operation: "CALL"`,
  from parsing `transfer_employee`'s actual source). Seeing both side by side on the
  same node is the clearest illustration of why parsing the source is worth doing
  on top of what Oracle's catalog already gives you for free.
- **Both triggers attach directly to the pre-existing `employees` `Table` node** --
  proof the cross-ingestor id scheme actually lines up, not just that each ingestor
  produces plausible-looking output in isolation.
- **The two `log_change` overloads are distinct `Procedure` nodes** (`log_change#1`,
  `log_change#2`), each with its own single parameter (`NUMBER` vs `VARCHAR2`) and
  its own `DEPENDS_ON` edge (to `job_history` vs `audit_log` respectively).
- **The sequences show up as `catalog`-only dependencies** (no `operation`) --
  `NEXTVAL` usage isn't a SELECT/INSERT/UPDATE/DELETE/CALL our parser tracks, so
  unlike the table/procedure dependencies above, these only ever get the coarse
  signal. A real, visible example of the "parsing helps, but the catalog is the
  always-available fallback" tradeoff described in the main README.

## Run it

```bash
uv run python demo/plsql/run_demo.py
```

One command: starts Oracle and FalkorDB via Docker if nothing's already listening
on their default ports (reuses them on later runs instead of creating duplicates),
(re)builds the schema, runs both ingestors, starts the viewer, and opens
`http://127.0.0.1:8080` in your browser automatically. Docker is only required if
you don't already have Oracle/FalkorDB reachable locally -- if you do, this uses
them as-is. Ctrl-C stops the viewer when you're done; the containers (if it started
any) are left running so the next run is instant.

### Manual steps

If you'd rather drive each piece yourself (e.g. against an Oracle instance you're
already running elsewhere), `run_demo.py` is just gluing these together:

```bash
uv run python demo/plsql/setup.py --admin-dsn "system/yourpassword@yourhost:1521/yourservice"
```

This prints the exact `auryn run sql`/`auryn run plsql` commands to use
(connection details depend on the admin DSN you passed in) -- run the `sql` one
first, then `plsql`, against a reachable FalkorDB (see the main
[README](../../README.md) for local-vs-Docker options there). Then
`uv run auryn view` and open `http://127.0.0.1:8080` yourself.
