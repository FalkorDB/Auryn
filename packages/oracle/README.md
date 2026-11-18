# auryn-oracle

Oracle-stack plugins for Auryn: live-DB and file-based PL/SQL (vendored ANTLR grammar) and Redgate schema-catalog reconstruction (oraschema). (The generic relational-DB schema ingestor now lives in auryn-sql.)

Part of [Auryn](https://github.com/FalkorDB/Auryn), which turns a codebase into a queryable knowledge graph for AI agents.

## Install

```bash
pip install falkordb-auryn                  # Auryn with every plugin
pip install "auryn-core[oracle]"            # Auryn with this plugin and auryn-sql, which it uses
pip install "auryn-core[oracle-live]"       # + the oracledb driver for live databases
```

## License

MIT. See the [LICENSE](https://github.com/FalkorDB/Auryn/blob/main/packages/oracle/LICENSE). The vendored ANTLR PL/SQL grammar is Apache-2.0; its notice is in the same file.
