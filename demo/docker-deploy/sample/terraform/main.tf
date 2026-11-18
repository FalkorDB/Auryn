# The cloud: an ECS task runs the SAME shop/api:1.0 the Dockerfile builds and compose + k8s run (one
# hub image, FOUR producers). An RDS database and an ECR registry round out the topology, and the
# service depends_on the task definition (an interpolation reference -> DEPENDS_ON).
resource "aws_ecr_repository" "api" {
  name = "shop/api"
}

resource "aws_db_instance" "db" {
  engine     = "postgres"
  identifier = "shop-db"
}

resource "aws_ecs_task_definition" "api" {
  family                = "api"
  container_definitions = jsonencode([{ name = "api", image = "shop/api:1.0" }])
}

resource "aws_ecs_service" "api" {
  name            = "api"
  task_definition = aws_ecs_task_definition.api.arn
}
