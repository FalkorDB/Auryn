# Demo: Chinook

A real, well-known sample database to see the SQL ingestor (and the graph viewer)
work against something more substantial than a two-table toy schema.

## What this is

[Chinook](https://github.com/lerocha/chinook-database) models a digital media
store: 11 tables, roughly 6,700 rows total. `chinook.sqlite` is the unmodified
SQLite build from the project's
[v1.4.5 release](https://github.com/lerocha/chinook-database/releases/tag/v1.4.5) --
nothing in it has been altered.

| Table | Rows | Notes |
|---|---|---|
| `Artist` | 275 | |
| `Album` | 347 | FK -> `Artist` |
| `Track` | 3,503 | FKs -> `Album`, `Genre`, `MediaType`; 3 indexes |
| `Genre` | 25 | |
| `MediaType` | 5 | |
| `Playlist` | 18 | |
| `PlaylistTrack` | 8,715 | composite primary key (`PlaylistId`, `TrackId`); FKs -> `Playlist`, `Track` |
| `Customer` | 59 | FK -> `Employee` (`SupportRepId`) |
| `Employee` | 8 | self-referencing FK: `ReportsTo -> Employee.EmployeeId` |
| `Invoice` | 412 | FK -> `Customer` |
| `InvoiceLine` | 2,240 | FKs -> `Invoice`, `Track` |

That gives the ingestor a real dependency graph to work with -- 11 foreign keys
in total, including one composite key and one self-reference -- rather than the
two-table toy example in the main README.

## What ingesting it produces

Running the SQL ingestor against `chinook.sqlite` writes **98 nodes / 130 edges**:

- 1 `Database`, 1 `Schema`, 11 `Table`, 64 `Column`
- 10 `Index` (all on FK columns, e.g. `Track.AlbumId`)
- 11 `Constraint` (one `PRIMARY_KEY` per table -- `PlaylistTrack`'s is the
  composite-key case: one `Constraint` node with two ordered `CONSTRAINS` edges,
  not two separate constraints)
- 11 `REFERENCES` edges (the foreign keys)

Chinook has no views or sequences, so this demo doesn't exercise those entity
types -- see the main [README](../../README.md) for what the ingestor supports there.
This is also SQL-only (SQLite); see [demo/plsql/](../plsql/) for a demo combining the
SQL and PL/SQL ingestors against the same Oracle database.

## Run it

From the repo root, with a FalkorDB instance reachable (see the main
[README](../../README.md) for local-vs-Docker options):

```bash
uv run auryn run sql \
    --dsn "sqlite:///demo/chinook/chinook.sqlite" \
    --database-name chinook

uv run auryn view
```

Then open `http://127.0.0.1:8080`, pick the `auryn` graph, and explore. A few
things worth clicking into specifically:

- **`Track`**: expands to 3 indexes and fans out to `Album`, `Genre`, and
  `MediaType` -- the busiest node in the graph.
- **`Employee`**: has a `REFERENCES` edge back to itself (`ReportsTo`) -- a good
  test of whether a node can link to its own label without breaking the layout.
- **`PlaylistTrack`**: expand its `Constraint` node to see both `CONSTRAINS`
  edges (to `PlaylistId` and `TrackId`) coming from the *same* node -- that's
  the composite-primary-key case the `Constraint` entity was built for.

Re-running the `auryn run sql` command is safe -- it's idempotent, so you'll
still see 98 nodes / 130 edges rather than duplicates.
