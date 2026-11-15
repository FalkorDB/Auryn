"""Enable ``python -m auryn`` -- used to spawn the detached local FalkorDB daemon
(``python -m auryn serve``) with the exact interpreter/venv of the caller, regardless of
whether the ``auryn`` console script is on PATH. See core/server_manager.py."""

from auryn.cli import cli

if __name__ == "__main__":
    cli()
