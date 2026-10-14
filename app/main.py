from fastapi import FastAPI
from .db import Base, engine
from . import models  # noqa: F401

Base.metadata.create_all(engine)
app = FastAPI(title="Equipment Maintenance Triage Assistant")


@app.get("/health")
def health():
    return {"status": "ok"}
