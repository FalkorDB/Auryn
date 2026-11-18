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
    # an `image_uri` built from an ECR reference must still produce an (unresolved) Image, not vanish
    p = parse_source('resource "aws_apprunner_service" "s" { image_uri = "${aws_ecr_repository.r.url}:1.0" }')
    assert any("$" in img for img in p.blocks[0].images)


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
