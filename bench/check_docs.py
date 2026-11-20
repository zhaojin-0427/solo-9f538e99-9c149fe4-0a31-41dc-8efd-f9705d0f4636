import sys
sys.path.insert(0, "/workspace")
from fastapi.testclient import TestClient
from app.builder_app import app as ba
from app.converter_app import app as ca
for name, app, path in (("builder", ba, "/build"), ("converter", ca, "/convert")):
    t = TestClient(app)
    assert t.get("/docs").status_code == 200
    assert t.get("/health").status_code == 200
    spec = t.get("/openapi.json").json()
    assert path in spec["paths"]
    assert spec["paths"][path]["post"].get("requestBody")
    print(name, "/docs + openapi OK; schemas:", sorted(spec["components"]["schemas"]))
