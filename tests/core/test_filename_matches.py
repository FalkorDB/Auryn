"""GitLanguage basename-glob detection — ADR 0007 §7 `filename_matches`: `file_globs` + `list_files_glob`
let detection match basename variants (`Dockerfile.prod`) and exact anchors (`Chart.yaml`) that a bare
`endswith` suffix can't express."""

from __future__ import annotations

import fnmatch

from auryn.core.local_source import LocalDirectorySource
from auryn.core.registry import GitLanguage, match_patterns


def test_match_patterns_free_function():
    assert match_patterns((".py",)) == ("*.py",)                       # a suffix -> `*<suffix>` glob
    assert match_patterns(("Dockerfile",), ("Dockerfile.*",)) == ("*Dockerfile", "Dockerfile.*")
    assert match_patterns((".tf",), ("*.tf",)) == ("*.tf",)             # order-preserving de-dup


def test_gitlanguage_match_patterns_unions_suffixes_and_globs():
    gl = GitLanguage(language="x", file_suffixes=(".tf",), file_globs=("*.tofu",),
                     excluded_dir_names=frozenset(), sync=lambda **k: None)
    assert gl.match_patterns() == ("*.tf", "*.tofu")
    # back-compat: a suffix-only language still produces globs
    gl2 = GitLanguage(language="y", file_suffixes=(".cs",), excluded_dir_names=frozenset(), sync=lambda **k: None)
    assert gl2.match_patterns() == ("*.cs",)


def _src(tmp_path, files):
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return LocalDirectorySource(str(tmp_path))


def test_list_files_glob_basename_variants(tmp_path):
    src = _src(tmp_path, {
        "Dockerfile": "FROM x", "svc/Dockerfile.prod": "FROM y", "web.Dockerfile": "FROM z",
        "app.py": "x = 1", "charts/a/Chart.yaml": "name: a", "charts/a/values.yaml": "k: v",
    })
    sha = src.tree_sha()
    got = sorted(p for p, _ in src.list_files_glob(sha, ("*Dockerfile", "Dockerfile.*"), set()))
    assert got == ["Dockerfile", "svc/Dockerfile.prod", "web.Dockerfile"]
    # an EXACT-basename anchor matches only Chart.yaml, not the sibling values.yaml or app.py
    assert sorted(p for p, _ in src.list_files_glob(sha, ("Chart.yaml",), set())) == ["charts/a/Chart.yaml"]


def test_list_files_glob_respects_excluded_dirs(tmp_path):
    src = _src(tmp_path, {"a/Dockerfile": "FROM x", "vendor/Dockerfile": "FROM y"})
    got = sorted(p for p, _ in src.list_files_glob(src.tree_sha(), ("*Dockerfile",), {"vendor"}))
    assert got == ["a/Dockerfile"]


def test_docker_detects_dockerfile_variants():
    from auryn.ingestors.docker.plugin import MATCH_PATTERNS
    for name in ("Dockerfile", "web.Dockerfile", "Dockerfile.prod", "Dockerfile.dev", "Containerfile"):
        assert any(fnmatch.fnmatchcase(name, pat) for pat in MATCH_PATTERNS), name
    assert not any(fnmatch.fnmatchcase("app.py", pat) for pat in MATCH_PATTERNS)


def test_helm_detection_is_chart_related_only():
    from auryn.ingestors.helm.plugin import FILE_GLOBS, FILE_SUFFIXES
    assert FILE_SUFFIXES == ()                 # no broad `*.yaml` -> helm isn't woken by arbitrary YAML
    # chart manifest + its values (values is watched + keeps cleanup working after Chart.yaml is removed)
    for name in ("Chart.yaml", "values.yaml", "values.yml"):
        assert any(fnmatch.fnmatchcase(name, pat) for pat in FILE_GLOBS), name
    # but NOT a plain k8s manifest / compose file / arbitrary config
    for name in ("deploy.yaml", "docker-compose.yml", "config.yaml"):
        assert not any(fnmatch.fnmatchcase(name, pat) for pat in FILE_GLOBS), name


def test_terraform_detects_tofu():
    from auryn.ingestors.terraform.plugin import MATCH_PATTERNS
    assert any(fnmatch.fnmatchcase("main.tofu", pat) for pat in MATCH_PATTERNS)
    assert any(fnmatch.fnmatchcase("main.tf", pat) for pat in MATCH_PATTERNS)
