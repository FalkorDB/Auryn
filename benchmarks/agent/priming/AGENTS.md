# Auryn

This repository is indexed as a queryable **code graph**, exposed by the `auryn` MCP
server. For any structural question — where a symbol is defined, who calls it, what it
calls, the path between two functions, the blast radius of a change, or what an
endpoint/handler does end to end — **query the graph instead of grepping and reading whole
files.** One query returns the precise answer (with node ids) without pulling files into
context.

Rules:
- Before answering an architecture / call-graph / impact / "where is X" question, call
  `auryn_status` first. If `has_data` is false, run `index_repository(path=".")` once
  (a one-time cost; queries afterward are cheap), then query.
- Use `locate(name)` to turn a symbol name into an id, then the smallest tool that answers
  the question: `who_calls`, `callees`, `find_path`, `impact_of`, `behavioral_inventory`,
  or `get_context`. `get_schema` + `run_cypher` are the escape hatch.
- Prefer graph answers over reading many files, and cite the node ids / file paths the
  graph returns as your evidence.
