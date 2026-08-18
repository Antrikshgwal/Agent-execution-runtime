from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()

created: dict[str, str] = {}


class ProvisionRequest(BaseModel):
    resource_name: str
    spec: str
    idempotency_key: str


@app.post("/provision")
def provision(req: ProvisionRequest):
    if req.idempotency_key in created:
        return {"status": "already_done", "result": created[req.idempotency_key]}

    resource_id = f"i-{len(created) + 1:07d}"
    created[req.idempotency_key] = resource_id
    return {"status": "created", "result": resource_id}


@app.get("/created")
def get_created():
    return created
