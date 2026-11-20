import sys, random
sys.path.insert(0, "/workspace")
from app.builder_core import build_package
from tests.test_streaming_build import _reference_build

rng = random.Random(7)
# Construct overlaps so old relations AND twin-block splits both occur.
old = [
    {"code": "O1", "blocks": ["b1", "b2", "b3"]},
    {"code": "O2", "blocks": ["b2", "b3", "b4"]},   # b2,b3 twins under O1+O2
    {"code": "O3", "blocks": ["b1", "b2", "b3", "b4"]},
]
new = [
    {"code": "N1", "blocks": ["b2"]},               # splits the b2/b3 twins
    {"code": "N2", "blocks": ["b2", "b3"]},         # constant on the twins
    {"code": "N3", "blocks": ["b1", "b4"]},
    {"code": "N4", "blocks": []},                   # zero district
]
got = build_package(old, new)
ref = _reference_build(old, new)
# Dense reference has no twin rule, so compare the relation/derivable math
# after applying the twin override manually.
print("sparse package:")
import json; print(json.dumps(got, indent=1, ensure_ascii=False))
# N1 must be underivable due to twin rule; check against direct linear algebra.
n1 = next(r for r in got["new_district_relations"] if r["code"] == "N1")
assert n1["derivable"] is False, "twin rule failed"
n2 = next(r for r in got["new_district_relations"] if r["code"] == "N2")
assert n2["derivable"] is True
# old relations must still be complete: verify count matches dense ker(A).
assert got["old_relations"] == ref["old_relations"], "old relations diverged"
print("old relations match dense reference:", got["old_relations"])
print("twin + relations cross-check OK")
