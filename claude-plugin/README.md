# Auryn plugin for Claude Code

Auryn indexes your repository into a code graph and gives Claude Code tools that answer structural questions in one call: where a symbol is defined, who calls it, what it calls, what breaks if it changes, and how an endpoint runs end to end. Claude uses the graph instead of grepping and reading whole files.

The plugin contains two things:

- the `auryn` MCP server, with tools to index a repository and query its graph (the query tools issue only read-only graph queries), and
- the `auryn` skill, which tells Claude when to use the graph and how.

## Install

In Claude Code:

```text
/plugin marketplace add FalkorDB/Auryn
/plugin install auryn@auryn
```

Then ask Claude a structural question, for example "What breaks if I change the signature of `charge_card`?". The first time, Claude indexes the repository (`index_repository`), which takes seconds for a small repo and a few minutes for a large one.

## Requirements

- [uv](https://docs.astral.sh/uv/getting-started/installation/) on your `PATH`. The plugin starts the server with `uvx`.
- macOS or Linux with a recent glibc. The graph runs in a local FalkorDB that ships with the plugin. On native Windows or an older Linux, install with `auryn install --backend remote` instead (see the [main README](https://github.com/FalkorDB/Auryn#connect-your-agent)).

If you already ran `auryn install`, run `auryn uninstall` first, or Claude Code will load both copies of the server.

## What it runs and downloads

- **On first start**, `uvx` downloads `falkordb-auryn` (pinned to the version in `.mcp.json`) and its dependencies from PyPI, plus Python 3.12 if you don't have it, and uv caches them. Later starts take a few seconds. On a slow connection the first start can take longer than Claude Code's 30-second limit; if `/mcp` shows the server as failed, run the command from `.mcp.json` once in a terminal, then reconnect it from `/mcp` (Claude Code keeps skipping a server that failed until it reconnects).
- **On first index**, the background embedder downloads a local embedding model (`jinaai/jina-embeddings-v2-base-code`) from Hugging Face, used by `search_code`.
- **When you index a repository**, Auryn starts a local FalkorDB server (bound to 127.0.0.1) and stores graphs and config in `~/.auryn`. It installs git hooks (`post-commit`, `post-checkout`, `post-merge`, `post-rewrite`) in that repository so the graph resyncs after each commit. The hooks call the Python that `uvx` cached for this plugin version, so after a plugin update ask Claude to index each repository once more to point them at the new version. To remove a repository's graph and its hooks, run `uvx --python 3.12 --from "falkordb-auryn[embedded]" auryn forget <path>`.

Parsing runs on your machine. Your code and the graph stay on your machine. Auryn has no hosted service and sends no telemetry.

## Learn more

- [Auryn on GitHub](https://github.com/FalkorDB/Auryn): every tool, supported languages, CLI reference
- License: MIT
