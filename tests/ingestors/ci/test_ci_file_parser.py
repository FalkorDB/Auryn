"""The CI (GitHub Actions) parser -> ParsedWorkflow: the images a workflow builds/pushes. Pure, no graph."""

from __future__ import annotations

from auryn.ingestors.ci.file_parser import parse_workflow


def _p(body, name="ci"):
    return parse_workflow(f"name: {name}\non: [push]\njobs:\n{body}")


def test_build_push_action_multiline_tags():
    p = _p(
        "  build:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - uses: docker/build-push-action@v5\n"
        "        with:\n          push: true\n          tags: |\n"
        "            myreg.io/app:1.0\n            myreg.io/app:latest\n"
    )
    assert p.valid and set(p.built_images) == {"myreg.io/app:1.0", "myreg.io/app:latest"}


def test_build_push_action_list_and_comma_tags():
    p = _p("  b:\n    steps:\n      - uses: docker/build-push-action@v6\n        with:\n          tags: \"a/x:1, a/x:2\"\n")
    assert set(p.built_images) == {"a/x:1", "a/x:2"}


def test_docker_build_and_push_in_run():
    p = _p(
        "  b:\n    steps:\n"
        "      - run: docker build -t myreg.io/api:2.3 .\n"
        "      - run: docker push myreg.io/api:2.3\n"
    )
    assert p.built_images == ("myreg.io/api:2.3",)  # de-duped across build + push


def test_buildx_long_flag_tag():
    p = _p("  b:\n    steps:\n      - run: docker buildx build --tag ghcr.io/org/svc:v1 --push .\n")
    assert p.built_images == ("ghcr.io/org/svc:v1",)


def test_templated_tag_is_skipped():
    # a fully-templated tag can't be keyed on the concrete hub
    p = _p(
        "  b:\n    steps:\n      - uses: docker/build-push-action@v5\n"
        "        with:\n          tags: ${{ env.REGISTRY }}/app:${{ github.sha }}\n"
    )
    assert p.built_images == ()


def test_stray_dash_t_without_docker_build_is_ignored():
    # a `-t` that isn't a docker build tag (e.g. `tar -t`) must not be mistaken for an image
    p = _p("  b:\n    steps:\n      - run: tar -t backup.tar\n")
    assert p.built_images == ()


def test_file_without_jobs_is_invalid():
    assert parse_workflow("name: x\non: [push]\n").valid is False
    assert parse_workflow("services:\n  web:\n    image: nginx\n").valid is False  # a compose file


def test_malformed_yaml_is_invalid():
    assert parse_workflow("this: : : nope").valid is False


def test_name_defaults_to_empty_when_absent():
    p = parse_workflow("on: [push]\njobs:\n  b:\n    steps:\n      - run: echo hi\n")
    assert p.valid and p.name == "" and p.built_images == ()


def test_push_with_flags_skips_them():
    p = _p("  b:\n    steps:\n      - run: docker push --quiet shop/api:1.0\n")
    assert p.built_images == ("shop/api:1.0",)


def test_dynamic_tag_substitution_is_skipped():
    assert _p("  b:\n    steps:\n      - run: docker build -t shop/api:`date` .\n").built_images == ()
    assert _p("  b:\n    steps:\n      - run: docker build -t shop/api:$(git rev-parse HEAD) .\n").built_images == ()


def test_dash_t_in_another_command_is_not_a_build_tag():
    # a `-t` from a non-build command in the same run block must not be captured as a build tag
    p = _p("  b:\n    steps:\n      - run: |\n          docker build -t shop/api:1.0 .\n          tar -t archive.tar\n")
    assert p.built_images == ("shop/api:1.0",)


def test_shell_line_continuation_is_joined():
    p = _p("  b:\n    steps:\n      - run: |\n          docker build \\\n            -t shop/api:1.0 \\\n            .\n")
    assert p.built_images == ("shop/api:1.0",)


def test_shell_comment_is_not_parsed():
    p = _p("  b:\n    steps:\n      - run: |\n          # docker push evil/x:1\n          docker push shop/api:1.0\n")
    assert p.built_images == ("shop/api:1.0",)
