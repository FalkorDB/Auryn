"""The Terraform HCL parser -> ParsedTf. Pure, no graph."""

from __future__ import annotations

from auryn.ingestors.terraform.file_parser import parse_source

_TF = """
resource "aws_ecs_task_definition" "api" {
  container_definitions = jsonencode([{ name = "api", image = "shop/api:1.0" }])
  execution_role_arn    = aws_iam_role.exec.arn
  tags                  = { db = module.net.id }
}
resource "aws_db_instance" "db" {
  engine = "postgres"
}
resource "aws_iam_role" "exec" { name = "exec" }
data "aws_ami" "ubuntu" { most_recent = true }
module "net" { source = "./net" }
"""


def _addrs(p):
    return sorted(b.address for b in p.blocks)


def test_blocks_and_types():
    p = parse_source(_TF)
    assert _addrs(p) == [
        "aws_db_instance.db", "aws_ecs_task_definition.api", "aws_iam_role.exec",
        "data.aws_ami.ubuntu", "module.net",
    ]


def test_references_become_refs():
    api = next(b for b in parse_source(_TF).blocks if b.address == "aws_ecs_task_definition.api")
    # the role arn and the module.net interpolation; var./local. would NOT be refs
    assert set(api.refs) == {"aws_iam_role.exec", "module.net"}


def test_image_from_container_definitions():
    api = next(b for b in parse_source(_TF).blocks if b.address == "aws_ecs_task_definition.api")
    assert api.images == ("shop/api:1.0",)


def test_direct_image_uri_attribute():
    p = parse_source('resource "aws_lambda_function" "fn" { image_uri = "123.dkr.ecr/app:2" }')
    assert p.blocks[0].images == ("123.dkr.ecr/app:2",)


def test_var_and_local_are_not_refs():
    p = parse_source('resource "aws_x" "y" { a = var.foo\n b = local.bar }')
    assert p.blocks[0].refs == ()


def test_literal_string_is_not_a_reference():
    # a plain string literal that merely LOOKS like an address must not fabricate a DEPENDS_ON -- only
    # a `${...}` interpolation is a real reference.
    p = parse_source('resource "aws_x" "y" { description = "see aws_iam_role.exec for details" }')
    assert p.blocks[0].refs == ()


def test_nested_image_attribute_is_found():
    p = parse_source(
        'resource "google_cloud_run_v2_service" "s" {\n'
        '  template {\n    containers {\n      image = "gcr.io/app:1"\n    }\n  }\n}\n'
    )
    assert p.blocks[0].images == ("gcr.io/app:1",)


def test_interpolated_image_uri_is_kept():
    # a Lambda container `image_uri` built from an ECR reference must still produce an (unresolved)
    # Image -- and the WHOLE ref is kept (dropping the `:1.0` tag or an extra image is a regression)
    p = parse_source('resource "aws_lambda_function" "fn" { image_uri = "${aws_ecr_repository.r.url}:1.0" }')
    assert p.blocks[0].images == ("${aws_ecr_repository.r.url}:1.0",)


def test_apprunner_image_identifier_is_found():
    # App Runner declares its image at source_configuration.image_repository.image_identifier
    p = parse_source(
        'resource "aws_apprunner_service" "s" {\n'
        '  source_configuration {\n    image_repository {\n'
        '      image_identifier = "123.dkr.ecr/app:2"\n    }\n  }\n}\n'
    )
    assert p.blocks[0].images == ("123.dkr.ecr/app:2",)


def test_init_container_image_is_found():
    # the k8s provider repeats `init_container` alongside `container` -- its image counts too
    p = parse_source(
        'resource "kubernetes_pod_v1" "p" {\n'
        '  spec {\n    init_container {\n      image = "busybox:1.36"\n    }\n  }\n}\n'
    )
    assert p.blocks[0].images == ("busybox:1.36",)


def test_storage_container_type_is_not_a_container_runtime():
    # a resource type that ENDS with `_container` but manages storage (azurerm_storage_container) must
    # NOT have a `metadata.image` read as a container image
    p = parse_source('resource "azurerm_storage_container" "c" { metadata = { image = "example/app:1" } }')
    assert p.blocks[0].images == ()


def test_image_like_line_in_user_data_is_not_an_image():
    # a stray `image = "..."` inside a non-container string (a user_data script) must not be scraped
    p = parse_source('resource "aws_instance" "vm" { user_data = "docker pull x\\nimage = \\"evil/x:1\\"" }')
    assert p.blocks[0].images == ()


def test_interpolated_image_in_blob_is_kept():
    # an interpolated image inside a container_definitions jsonencode blob must survive too (the blob
    # extractor used to reject any `${...}` value)
    p = parse_source(
        'resource "aws_ecs_task_definition" "api" {\n'
        '  container_definitions = jsonencode([{ name = "api", image = "${aws_ecr_repository.r.url}:1.0" }])\n}\n'
    )
    assert p.blocks[0].images == ("${aws_ecr_repository.r.url}:1.0",)


def test_container_typed_resource_collects_top_level_image():
    # the Docker provider's `docker_container` has a bare top-level `image` and NO nested container
    # block -- the resource TYPE is what marks the context
    p = parse_source('resource "docker_container" "c" { image = "nginx:1.25" }')
    assert p.blocks[0].images == ("nginx:1.25",)


def test_container_substring_key_does_not_enable_image_context():
    # a key that merely CONTAINS "container" (e.g. `container_owner`) must NOT turn on the container
    # context and mis-collect a nested `image`
    p = parse_source('resource "aws_x" "y" { metadata = { container_owner = { image = "example/app:1" } } }')
    assert p.blocks[0].images == ()


def test_quoted_literal_argument_is_not_a_reference():
    # a string ARGUMENT inside an interpolation that looks like an address must not fabricate a ref
    p = parse_source('resource "aws_x" "y" { v = "${lookup(local.m, "aws_iam_role.exec")}" }')
    assert p.blocks[0].refs == ()


def test_vm_boot_disk_image_is_not_a_container_image():
    p = parse_source(
        'resource "google_compute_instance" "vm" {\n'
        '  boot_disk {\n    initialize_params {\n      image = "debian-cloud/debian-12"\n    }\n  }\n}\n'
    )
    assert p.blocks[0].images == ()  # a VM boot image under boot_disk, NOT a container


def test_nested_brace_interpolation_keeps_reference():
    # the ref lives after a nested `{...}` -- brace-balancing must not stop at the inner `}`
    p = parse_source('resource "aws_x" "y" { v = "${merge({a = 1}, {b = aws_iam_role.exec.arn})}" }')
    assert "aws_iam_role.exec" in p.blocks[0].refs


def test_escaped_interpolation_is_not_a_reference():
    p = parse_source('resource "aws_x" "y" { note = "literal $${aws_iam_role.exec.arn}" }')
    assert p.blocks[0].refs == ()


def test_malformed_hcl_is_empty():
    assert parse_source('resource "aws_x" "y" { = = =').blocks == ()
