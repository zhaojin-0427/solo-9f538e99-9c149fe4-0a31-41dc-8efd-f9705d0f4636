"""Memory/correctness benchmark at the full contractual scale.

Run under a hard 192 MiB address-space limit:
    python3 -I bench/bench_mem.py
"""
import json
import os
import resource
import sys
import time

sys.path.insert(0, "/workspace")
from app.builder_core import build_from_coverage  # noqa: E402
from app.streaming import StreamingBuildParser  # noqa: E402

N_OLD = N_NEW = 240
N_BLOCKS = 600_000
BLOCKS_PER_DISTRICT = 5000  # 240 * 5000 = 1.2M memberships per side


def gen_stream(path):
    """Stream-generate a legal manifest without holding it all in memory."""
    with open(path, "w") as f:
        f.write('{"old_districts":[')
        for d in range(N_OLD):
            if d:
                f.write(",")
            start = (d * 7919) % N_BLOCKS
            ids = [f"blk{(start + k) % N_BLOCKS:08d}" for k in range(BLOCKS_PER_DISTRICT)]
            f.write(json.dumps({"code": f"OLD{d:03d}", "blocks": ids}))
        f.write('],"new_districts":[')
        for d in range(N_NEW):
            if d:
                f.write(",")
            start = (d * 104729) % N_BLOCKS
            ids = [f"blk{(start + k) % N_BLOCKS:08d}" for k in range(BLOCKS_PER_DISTRICT)]
            f.write(json.dumps({"code": f"NEW{d:03d}", "blocks": ids}))
        f.write("]}")


def main():
    # Hard 192 MiB virtual-address-space ceiling (the contract's limit).
    limit = 192 * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (limit, limit))

    path = "/workspace/bench/manifest.json"
    t0 = time.time()
    gen_stream(path)
    size = os.path.getsize(path)
    print(f"manifest generated: {size / 1e6:.1f} MiB in {time.time()-t0:.1f}s")

    parser = StreamingBuildParser(N_OLD, N_NEW)
    t0 = time.time()
    # Mimic ASGI: 64 KiB chunks.
    with open(path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            parser.feed(chunk)
    acc = parser.finish()
    print(f"parsed in {time.time()-t0:.1f}s; distinct blocks: {len(acc._membership)}")

    t0 = time.time()
    package = build_from_coverage(parser.old_codes, parser.new_codes, acc)
    print(f"package built in {time.time()-t0:.1f}s")
    print(
        "old_relations:", len(package["old_relations"]),
        "new_relations:", len(package["new_district_relations"]),
    )
    underivable = sum(
        1 for r in package["new_district_relations"] if not r["derivable"]
    )
    print("underivable new districts:", underivable)
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    print(f"peak RSS: {peak / 1024:.1f} MiB (limit 192 MiB)")
    assert peak / 1024 < 192, "EXCEEDED MEMORY LIMIT"
    with open("/workspace/bench/package.json", "w") as f:
        json.dump(package, f)
    print("package size:", os.path.getsize("/workspace/bench/package.json"), "bytes")
    print("OK")


if __name__ == "__main__":
    main()
