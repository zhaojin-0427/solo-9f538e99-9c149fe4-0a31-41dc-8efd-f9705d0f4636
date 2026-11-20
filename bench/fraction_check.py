import sys, random
sys.path.insert(0, "/workspace")
from app.builder_core import build_package
from tests.test_streaming_build import _reference_build

rng = random.Random(123)
# Dense overlaps: many blocks shared by 2 old and 2 new districts so that
# kernel relations and fractional particular solutions actually arise.
blocks = [f"b{i:04d}" for i in range(300)]
old, new = [], []
for d in range(8):
    chosen = {b for b in blocks if rng.random() < 0.4}
    old.append({"code": f"O{d}", "blocks": sorted(chosen)})
for d in range(8):
    chosen = {b for b in blocks if rng.random() < 0.4}
    new.append({"code": f"N{d}", "blocks": sorted(chosen)})

got = build_package(old, new)
ref = _reference_build(old, new)

# Math (relations + coefficients) must match after twin-rule underivable override.
assert got["old_relations"] == ref["old_relations"], "old relations differ"
for g, r in zip(got["new_district_relations"], ref["new_district_relations"]):
    if not g["derivable"]:
        # Underivable either from twin rule or genuine inconsistency; the
        # reference marks the latter False too.
        assert g["code"] == r["code"]
        if r["derivable"]:
            continue  # twin-rule override: expected
    else:
        assert g == r, (g, r)
print("old relations:", got["old_relations"])
fracs = [c for r in got["new_district_relations"] if r["derivable"]
         for c in r["coefficients"].values() if "/" in c]
print("fractional coefficients present:", fracs[:6])
print("fraction/relation cross-check OK")
