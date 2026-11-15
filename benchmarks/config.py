"""Benchmark configuration -- environment-overridable so this suite can be pointed
at any repo/ref without editing code. Defaults target a large, real, actively
maintained public C# repository -- see benchmarks/README.md for why this one and
for the exact tags `cold_ref`/`warm_ref` point at.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class BenchmarkConfig:
    repo_uri: str
    project_name: str
    cold_ref: str
    warm_ref: str
    workers: int | None

    @classmethod
    def from_env(cls) -> BenchmarkConfig:
        workers_env = os.environ.get("BENCHMARK_WORKERS")
        return cls(
            repo_uri=os.environ.get("BENCHMARK_REPO_URI", "https://github.com/dotnet/aspnetcore.git"),
            project_name=os.environ.get("BENCHMARK_PROJECT_NAME", "AspNetCore"),
            cold_ref=os.environ.get("BENCHMARK_COLD_REF", "v9.0.17"),
            warm_ref=os.environ.get("BENCHMARK_WARM_REF", "v9.0.18"),
            workers=int(workers_env) if workers_env else None,
        )
