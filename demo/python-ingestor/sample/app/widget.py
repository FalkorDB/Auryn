import json

from app.db import Connection, get_connection
from app.models import Entity


class Widget(Entity):
    def __init__(self, id, name: str):
        super().__init__(id)
        self.name = name
        self.tags = []

    def render(self):
        conn = get_connection()
        conn.execute(self.name)
        getattr(self, self.name)()
        json.dumps(self.tags)
