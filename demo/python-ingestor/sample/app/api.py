from fastapi import FastAPI

from app.db import get_connection
from app.widget import Widget

app = FastAPI()


@app.post("/widgets")
def create_widget(name: str):
    conn = get_connection()
    conn.execute(name)
    return {"created": name}


@app.get("/widgets/{widget_id}")
def read_widget(widget_id: int):
    return Widget(widget_id, "demo")
