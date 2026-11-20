"""End-to-end: build over real streamed HTTP, then convert, incl. legacy pkg."""
import sys
sys.path.insert(0, "/workspace")
from fastapi.testclient import TestClient
from app.builder_app import app as builder_app
from app.converter_app import app as converter_app

b, c = TestClient(builder_app), TestClient(converter_app)

# 1. Stream a build through the ASGI endpoint in small chunks.
manifest = {
    "old_districts": [
        {"code": "O1", "blocks": ["x1", "x2"]},
        {"code": "O2", "blocks": ["x2", "x3"]},
        {"code": "O3", "blocks": ["x1", "x3"]},
        {"code": "O4", "blocks": ["x1", "x2", "x3"]},
    ],
    "new_districts": [{"code": "N1", "blocks": ["x1", "x2", "x3"]}],
}
import json
body = json.dumps(manifest).encode()
def gen():
    for i in range(0, len(body), 13):
        yield body[i:i+13]
r = b.post("/build", content=gen(), headers={"content-type": "application/json"})
assert r.status_code == 200, r.text
pkg = r.json()
print("build OK, relations:", pkg["old_relations"])

# 2. Convert the freshly built package.
r = c.post("/convert", json={"package": pkg, "old_populations": {"O1":5,"O2":7,"O3":6,"O4":9}})
assert r.status_code == 200, r.text
print("convert fresh pkg:", r.json()["computed"][0]["population"])

# 3. A pre-existing/hand-written v1 package (legacy shape) must still work.
legacy = {
    "schema_version": 1,
    "old_districts": ["O1", "O2"],
    "new_districts": ["N1"],
    "old_relations": [{"O1": 1, "O2": -1}],
    "new_district_relations": [
        {"code": "N1", "derivable": True, "coefficients": {"O1": "1/2", "O2": "1/2"}}
    ],
}
r = c.post("/convert", json={"package": legacy, "old_populations": {"O1": 4, "O2": 4}})
assert r.status_code == 200, r.text
print("convert legacy pkg:", r.json()["computed"][0]["population"], r.json()["computed"][0]["expression"])

# 4. Underivable package from twin rule flows into converter as missing.
twin = {
    "old_districts": [{"code": "O1", "blocks": ["b1", "b2"]}],
    "new_districts": [{"code": "N1", "blocks": ["b1"]}],
}
r = b.post("/build", json=twin)
pkg2 = r.json()
r = c.post("/convert", json={"package": pkg2, "old_populations": {"O1": 5}})
assert r.status_code == 200
assert r.json()["missing"][0]["missing_reason"] == "not_expressible_from_old_districts"
print("twin underivable -> missing OK")
print("ALL E2E OK")
