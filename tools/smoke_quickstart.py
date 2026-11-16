"""Exercise the installed quickstart, including its embedded graph and viewer assets."""

from pathlib import Path
import tempfile
from unittest.mock import patch

from click.testing import CliRunner

from auryn.cli import cli
from auryn.core.graph_client import connect_client


def check_viewer(app, **kwargs):
    client = app.test_client()
    page = client.get("/")
    assert page.status_code == 200, page.data
    response = client.get("/api/graphs/auryn/search?q=charge_card&label=Function")
    assert response.status_code == 200, response.data
    assert any(node["name"] == "charge_card" for node in response.json), response.json


def main() -> None:
    clients = []

    def connect(config):
        client = connect_client(config)
        clients.append(client)
        return client

    with tempfile.TemporaryDirectory(prefix="auryn-quickstart-") as directory:
        # Replace only the blocking server loop and track real DB clients for cleanup.
        # Ingestion, plugin discovery, and HTTP handlers run from the installed wheels.
        try:
            with (
                patch("flask.Flask.run", autospec=True, side_effect=check_viewer) as serve,
                patch("auryn.cli.connect_client", side_effect=connect),
            ):
                result = CliRunner().invoke(
                    cli, ["quickstart", "--no-open", "--db-path", str(Path(directory) / "graph.db")]
                )
                print(result.output)
                if result.exception:
                    raise result.exception
                assert result.exit_code == 0, result.output
                serve.assert_called_once()
        finally:
            for client in clients:
                client.connection.shutdown()
    print("Installed quickstart ingested charge_card and served the viewer successfully.")


if __name__ == "__main__":
    main()
