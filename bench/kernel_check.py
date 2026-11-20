import sys
sys.path.insert(0, "/workspace")
from app.builder_core import build_package
from tests.test_streaming_build import _reference_build

# 1) Kernel relation + fractional coefficient on a (non-twin) derivable col.
old = [
    {"code": "O1", "blocks": ["x3", "x4"]},
    {"code": "O2", "blocks": ["x3", "x4"]},          # O1 - O2 = 0
    {"code": "O3", "blocks": ["x1", "x4"]},
    {"code": "O4", "blocks": ["x1", "x3"]},
]
new = [{"code": "N1", "blocks": ["x3"]}]
got = build_package(old, new)
ref = _reference_build(old, new)
assert got["old_relations"] == ref["old_relations"] == [{"O1": 1, "O2": -1}]
n1 = got["new_district_relations"][0]
assert n1["derivable"] is True
assert n1["coefficients"] == {"O1": "1/2", "O3": "-1/2", "O4": "1/2"}
print("kernel+ fraction exact match:", n1["coefficients"])

# 2) True twin pair: identical old signature, only one joins the new district.
old2 = [
    {"code": "O1", "blocks": ["x3", "x4"]},
    {"code": "O2", "blocks": ["x3", "x4"]},
]
new2 = [{"code": "N1", "blocks": ["x3"]}]   # x4 excluded -> must be underivable
pkg2 = build_package(old2, new2)
assert pkg2["new_district_relations"][0]["derivable"] is False
print("twin underivable OK")

# 3) Same twins, but new district covers both -> constant, still derivable.
new3 = [{"code": "N2", "blocks": ["x3", "x4"]}]
pkg3 = build_package(old2, new3)
r3 = pkg3["new_district_relations"][0]
assert r3["derivable"] is True and r3["coefficients"] == {"O1": "1"}
print("twin-constant derivable OK:", r3["coefficients"])
print("ALL KERNEL/TWIN CHECKS OK")
