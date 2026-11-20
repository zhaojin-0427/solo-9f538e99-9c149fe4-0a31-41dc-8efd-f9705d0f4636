"""Cross-check sparse/streaming builder against the dense reference."""
import sys
sys.path.insert(0, "/workspace")
from app.builder_core import build_from_coverage, CoverageAccumulator
from tests.test_streaming_build import _reference_build, _large_manifest

# Scale the dense Fraction implementation can finish quickly.
old, new = _large_manifest(n_old=20, n_new=20, n_blocks=2000, blocks_per_district=120)
ref = _reference_build(old, new)

acc = CoverageAccumulator(len(old), len(new))
for i, d in enumerate(old):
    for b in d["blocks"]:
        acc.add(b, "old", i)
for j, d in enumerate(new):
    for b in d["blocks"]:
        acc.add(b, "new", j)
got = build_from_coverage([d["code"] for d in old], [d["code"] for d in new], acc)

assert got == ref, "MISMATCH against dense reference"
print("cross-check OK: sparse output == dense reference")
print("  old_relations:", len(got["old_relations"]))
print("  derivable:", sum(1 for r in got["new_district_relations"] if r["derivable"]))
