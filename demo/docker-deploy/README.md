# Dockerfile ingestor + deployment tier (ADR 0007)

A tiny two-service system — an **`api`** and a **`worker`**, each a Python package with its own
Dockerfile — showing the deployment tier the `auryn-docker` plugin builds and the `deploy`
enrichment stitches onto the code graph.

```
sample/
  api/       Dockerfile (multi-stage: builder -> final)  +  app/main.py       (FROM python:3.12-slim)
  worker/    Dockerfile (single stage)                   +  worker/main.py    (FROM python:3.12-slim)
```

## Run

```bash
uv run python demo/docker-deploy/run_demo.py
```

Needs a FalkorDB: a local one on `:6379`, or the embedded extra (`pip install 'auryn-core[embedded]'`).
The demo reaps the embedded server it starts, so it leaves no process behind.

## What it shows

1. **Build stages** — one `BuildStage` node per `FROM ... AS`, with its base reference.
2. **The shared `Image` hub** — both services' `python:3.12-slim` base collapses to **one** `Image`
   node with three dependents. That one node is the key Compose/k8s/Terraform producers will join
   onto (`RUNS_IMAGE`) to connect a running service back to this build.
3. **`PACKAGES` — the code seam** — which Python modules each build stage actually ships (resolved
   from the stage's build context + `COPY` paths to the real `Module`/`File` nodes a language
   ingestor produced). This is the edge that lets a code change's blast radius reach the container.
4. **Multi-stage `COPIES_FROM`** — the `api` final stage copies from its `builder` stage.
5. **The cross-tier query** — from a function to its module to the image that ships it, in one
   traversal:

   ```cypher
   MATCH (fn:Function)<-[:HAS_FUNCTION]-(m:Module)<-[:PACKAGES]-(s:BuildStage)-[:BASED_ON]->(i:Image)
   RETURN fn.name, m.name, s.stage, i.ref
   ```

## How it maps to the tier

| What you see | Node / edge | Written by |
|---|---|---|
| `FROM ... AS base` | `BuildStage` | `auryn-docker` producer |
| `FROM python:3.12-slim` | `Image` (`resolved:false`, shared hub) | producer |
| `COPY --from=builder` | `COPIES_FROM` | producer |
| stage ⟶ the code it copies in | `PACKAGES` | `deploy` enrichment (stitch) |

The arms that light up once the **Compose/k8s/Terraform** producers land — `RUNS_IMAGE`,
`BUILDS_IMAGE` (which flips an image's `resolved` to *built-in-graph*), `SERVES`, `CONNECTS_TO`, and
the derived `SHIPS` collapse edge — are specified in
[`docs/adr/0007-deployment-infrastructure-tier.md`](../../docs/adr/0007-deployment-infrastructure-tier.md).
