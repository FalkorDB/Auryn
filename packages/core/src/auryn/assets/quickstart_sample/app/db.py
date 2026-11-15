"""Thin database layer. A real driver would open a socket here; for the quickstart
it's a stub -- the point is the CALLS edges from billing into `Connection.execute`.
"""


class Connection:
    def execute(self, sql: str, params: tuple = ()) -> None:
        """Run a statement. Stubbed -- a real driver would talk to the database."""
        _ = (sql, params)


def connect() -> Connection:
    return Connection()
