"""Cross-repo linkage keys for the C# graph (ADR 0004).

C#'s node ids are project-scoped (see config.py / resolution.py) -- rooted under
`csharp://<solution>/<project>` -- so two repos that both define, say, `Acme.Billing.Charge`
keep distinct real nodes and never accidentally merge on id. The *linkage key* is the
complementary, project-INDEPENDENT handle: the fully-qualified name a consumer writes at a
call site (`Namespace.Type.Method`, e.g. `System.Console.WriteLine` or `Acme.Billing.Charge`).
It is stamped as the `linkage_key` property on every externally-visible real Method, and used
as the id of the placeholder Method that stands in for an unresolved call. When the defining
repo is later ingested into the same graph, the placeholder and the real method agree on this
one string and reconciliation folds them (core/linkage.py).

Name-based only: a consumer can't know a callee's parameter types, so the key never embeds
them (unlike the project-scoped Method id, whose overload disambiguation does)."""

from __future__ import annotations


def extern_method_key(qualified_name: str) -> str:
    return f"extern://csharp/{qualified_name}"
