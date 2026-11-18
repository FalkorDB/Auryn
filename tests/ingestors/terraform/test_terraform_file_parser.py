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


def test_malformed_hcl_is_empty():
    assert parse_source('resource "aws_x" "y" { = = =').blocks == ()
