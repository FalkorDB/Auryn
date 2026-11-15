class Connection:
    def execute(self, sql: str):
        ...


def get_connection() -> Connection:
    ...
