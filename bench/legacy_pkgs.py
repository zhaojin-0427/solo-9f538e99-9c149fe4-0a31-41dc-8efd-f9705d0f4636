import sys
sys.path.insert(0, "/workspace")
from fastapi.testclient import TestClient
from app.converter_app import app as ca
t = TestClient(ca)

# A package produced independently (e.g. an older builder), with sparse
# coefficient dicts and integer-typed relation values.
legacy = {
    "schema_version": 1,
    "old_districts": ["A", "B", "C"],
    "new_districts": ["X", "Y"],
    "old_relations": [{"A": 1, "B": -1}],
    "new_district_relations": [
        {"code": "X", "derivable": True, "coefficients": {"A": "1"}},
        {"code": "Y", "derivable": False, "coefficients": {}},
    ],
}
r = t.post("/convert", json={"package": legacy, "old_populations": {"A": 5, "B": 5, "C": 7}})
assert r.status_code == 200, r.text
b = r.json()
assert {x["code"]: x["status"] for x in b["results"]} == {"X": "computed", "Y": "missing"}
assert b["results"][0]["population"] == 5
print("legacy package accepted; Y missing reason:",
      next(x for x in b["missing"] if x["code"]=="Y")["missing_reason"])

# schema_version other than 1 should be tolerated by the parser as long as
# the structure validates? Check current behavior explicitly.
v2 = dict(legacy, schema_version=2)
r2 = t.post("/convert", json={"package": v2, "old_populations": {"A":5,"B":5,"C":7}})
print("schema_version=2 acceptance:", r2.status_code)
