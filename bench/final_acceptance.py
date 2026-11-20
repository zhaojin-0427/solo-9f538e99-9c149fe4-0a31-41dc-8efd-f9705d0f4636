"""One-shot acceptance of every contractual requirement."""
import sys
sys.path.insert(0, "/workspace")
from fastapi.testclient import TestClient
from app.builder_app import app as ba
from app.converter_app import app as ca

b, c = TestClient(ba), TestClient(ca)
ok = []

# R1: streaming build, exact fractions, all old relations, twin rule.
old = [
    {"code": "O1", "blocks": ["t1", "t2", "s"]},
    {"code": "O2", "blocks": ["t1", "t2"]},          # t1,t2 twins (same old sig)
]
new = [
    {"code": "N_split", "blocks": ["t1"]},           # twin split -> underivable
    {"code": "N_same", "blocks": ["t1", "t2"]},      # constant -> derivable
]
pkg = b.post("/build", json={"old_districts": old, "new_districts": new}).json()
rels = {r["code"]: r for r in pkg["new_district_relations"]}
assert rels["N_split"]["derivable"] is False
assert rels["N_same"]["derivable"] is True
assert all(isinstance(v, str) for r in pkg["new_district_relations"]
           for v in r["coefficients"].values())
ok.append("R1 streaming build + exact fractions + twin rule")

# R2: read to end, tail duplicate block id -> whole request 4xx with reason.
r = b.post("/build", json={
    "old_districts": [{"code": "O1", "blocks": ["a"]}],
    "new_districts": [{"code": "N1", "blocks": ["z", "z"]}],
})
assert r.status_code == 422 and "duplicate block id" in r.json()["detail"]
ok.append("R2 tail duplicate rejected with reason")

# R3: corrupt JSON tail -> 400 invalid_json.
r = b.post("/build", content=b'{"old_districts":[{"code":"O1","blocks":[]}],"new_districts":[{"code":"N1","blocks":[]}]}###',
           headers={"content-type": "application/json"})
assert r.status_code == 400 and r.json()["error"] == "invalid_json"
ok.append("R3 corrupt tail rejected")

# R4: freshly built v1 package converts on the converter.
r = c.post("/convert", json={"package": pkg, "old_populations": {"O1": 4, "O2": 2}})
assert r.status_code == 200, r.text
assert next(x for x in r.json()["missing"] if x["code"] == "N_split")
ok.append("R4 fresh v1 package usable at /convert")

# R5: a pre-existing/hand-built v1 package converts identically.
legacy = {
    "schema_version": 1,
    "old_districts": ["O1", "O2"],
    "new_districts": ["N1"],
    "old_relations": [],
    "new_district_relations": [
        {"code": "N1", "derivable": True, "coefficients": {"O1": "1/3", "O2": "2/3"}}
    ],
}
r = c.post("/convert", json={"package": legacy, "old_populations": {"O1": 3, "O2": 6}})
assert r.status_code == 200 and r.json()["computed"][0]["population"] == 5
ok.append("R5 existing v1 package usable at /convert")

# R6: docs present on both instances.
assert b.get("/docs").status_code == 200 and c.get("/docs").status_code == 200
ok.append("R6 /docs available on both services")

for i, msg in enumerate(ok, 1):
    print(f"  [{i}] PASS: {msg}")
print("ALL ACCEPTANCE CRITERIA PASS")
