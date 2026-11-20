"""Tests for the streaming, memory-bounded builder and the new rules."""
import json
import random

import pytest
from fastapi.testclient import TestClient

from app.builder_app import app as builder_app
from app.builder_core import (
    BuilderConstraintError,
    CoverageAccumulator,
    build_from_coverage,
    build_package,
)
from app.converter_app import app as converter_app
from app.streaming import StreamParseError, StreamingBuildParser

builder = TestClient(builder_app)
converter = TestClient(converter_app)


# --------------------------------------------------------------------------
# Reference implementation: the original dense, block-sorted builder, kept
# here purely as an oracle for cross-checking the sparse/streaming pipeline.
# --------------------------------------------------------------------------
def _reference_build(old_districts, new_districts):
    from fractions import Fraction

    from app.rationals import nullspace, primitive_integer, solve
    from app.expressions import format_fraction

    old_codes = [d["code"] for d in old_districts]
    new_codes = [d["code"] for d in new_districts]
    n_old = len(old_codes)
    blocks = sorted(
        {b for d in old_districts + new_districts for b in d["blocks"]}
    )

    def incidence(districts):
        return [
            [
                Fraction(1 if block in district["blocks"] else 0)
                for district in districts
            ]
            for block in blocks
        ]

    a = incidence(old_districts)
    b = incidence(new_districts)
    old_relations = []
    for k in nullspace(a, n_old):
        ints = primitive_integer(k)
        old_relations.append(
            {code: val for code, val in zip(old_codes, ints) if val != 0}
        )
    new_relations = []
    for j, code in enumerate(new_codes):
        col = [row[j] for row in b]
        particular = solve(a, col, n_old)
        if particular is None:
            new_relations.append(
                {"code": code, "derivable": False, "coefficients": {}}
            )
        else:
            coefficients = {
                old_codes[i]: format_fraction(particular[i])
                for i in range(n_old)
                if particular[i] != 0
            }
            new_relations.append(
                {"code": code, "derivable": True, "coefficients": coefficients}
            )
    return {
        "schema_version": 1,
        "old_districts": old_codes,
        "new_districts": new_codes,
        "old_relations": old_relations,
        "new_district_relations": new_relations,
    }


def _parse_stream(payload: bytes, limit=240):
    parser = StreamingBuildParser(limit, limit)
    # Feed in deliberately awkward, tiny chunks (including 1-byte slices) to
    # exercise every cross-chunk boundary of the incremental parser.
    step = max(1, len(payload) // 37)
    for i in range(0, len(payload), step):
        parser.feed(payload[i : i + step])
    return parser.finish(), parser


# --------------------------------------------------------------------------
# 1. Streaming output equals the dense reference on randomized manifests.
# --------------------------------------------------------------------------
@pytest.mark.parametrize("seed", range(8))
def test_streaming_matches_dense_reference(seed):
    rng = random.Random(seed)
    n_blocks = rng.randint(1, 60)
    blocks = [f"b{i}" for i in range(n_blocks)]
    old, new = [], []
    for side, n_dist, prefix in ((old, rng.randint(1, 5), "O"), (new, rng.randint(1, 5), "N")):
        for d in range(n_dist):
            # each block appears in at most two districts of each side
            chosen = [b for b in blocks if rng.random() < 0.35]
            side.append({"code": f"{prefix}{d}", "blocks": chosen})
    payload = json.dumps(
        {"old_districts": old, "new_districts": new}
    ).encode()

    acc, parser = _parse_stream(payload)
    got = build_from_coverage(parser.old_codes, parser.new_codes, acc)
    expected = _reference_build(old, new)
    assert got == expected


def test_http_build_matches_reference():
    old = [
        {"code": "O1", "blocks": ["a", "b"]},
        {"code": "O2", "blocks": ["b", "c"]},
        {"code": "O3", "blocks": ["a", "c"]},
        {"code": "O4", "blocks": ["a", "b", "c"]},
    ]
    new = [{"code": "N1", "blocks": ["a", "c"]}]
    resp = builder.post(
        "/build",
        content=json.dumps({"old_districts": old, "new_districts": new}),
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == _reference_build(old, new)


# --------------------------------------------------------------------------
# 2. Twin-block rule: identical old membership, different new membership.
# --------------------------------------------------------------------------
def test_twin_blocks_marks_new_district_underivable():
    # b1 and b2 belong to exactly the same old district O1; N1 takes only b1.
    old = [{"code": "O1", "blocks": ["b1", "b2"]}]
    new = [{"code": "N1", "blocks": ["b1"]}]
    resp = builder.post(
        "/build", json={"old_districts": old, "new_districts": new}
    )
    assert resp.status_code == 200, resp.text
    rel = resp.json()["new_district_relations"][0]
    assert rel["code"] == "N1"
    assert rel["derivable"] is False
    assert rel["coefficients"] == {}

    # And the converter flags it accordingly.
    conv = converter.post(
        "/convert",
        json={
            "package": resp.json(),
            "old_populations": {"O1": 5},
        },
    )
    assert conv.status_code == 200
    assert conv.json()["missing"][0]["missing_reason"] == (
        "not_expressible_from_old_districts"
    )


def test_twin_blocks_only_affects_the_diverging_new_district():
    # N1 covers only b1 (underivable), N2 covers both b1 and b2 (= O1).
    old = [{"code": "O1", "blocks": ["b1", "b2"]}]
    new = [
        {"code": "N1", "blocks": ["b1"]},
        {"code": "N2", "blocks": ["b1", "b2"]},
    ]
    resp = builder.post(
        "/build", json={"old_districts": old, "new_districts": new}
    )
    assert resp.status_code == 200, resp.text
    rels = {d["code"]: d for d in resp.json()["new_district_relations"]}
    assert rels["N1"]["derivable"] is False
    assert rels["N2"]["derivable"] is True
    assert rels["N2"]["coefficients"] == {"O1": "1"}


def test_twin_blocks_with_two_old_districts():
    # b1, b2 share the two-district old membership O1+O2; N1 splits them.
    old = [
        {"code": "O1", "blocks": ["b1", "b2"]},
        {"code": "O2", "blocks": ["b1", "b2"]},
    ]
    new = [{"code": "N1", "blocks": ["b2"]}]
    resp = builder.post(
        "/build", json={"old_districts": old, "new_districts": new}
    )
    assert resp.status_code == 200, resp.text
    rel = resp.json()["new_district_relations"][0]
    assert rel["derivable"] is False


# --------------------------------------------------------------------------
# 3. The parser reads to the end of the request and fails atomically.
# --------------------------------------------------------------------------
def test_duplicate_block_id_in_late_district_errors_with_reason():
    payload = (
        b'{"old_districts":[{"code":"O1","blocks":["a","b"]}],'
        b'"new_districts":[{"code":"N1","blocks":["a","a"]}]}'
    )
    parser = StreamingBuildParser(8, 8)
    parser.feed(payload[:30])
    parser.feed(payload[30:])
    with pytest.raises(BuilderConstraintError) as exc:
        parser.finish()
    assert "duplicate block id" in str(exc.value)

    resp = builder.post(
        "/build",
        content=payload,
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"] == "invalid_request"
    assert "duplicate block id" in resp.json()["detail"]


def test_corrupt_json_tail_errors_with_reason():
    good = (
        b'{"old_districts":[{"code":"O1","blocks":["a"]}],'
        b'"new_districts":[{"code":"N1","blocks":["a"]}]'
    )
    resp = builder.post(
        "/build",
        content=good + b",,,,}",
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_json"
    assert resp.json()["detail"]


def test_truncated_body_is_rejected():
    payload = (
        b'{"old_districts":[{"code":"O1","blocks":["a","b"]}],'
        b'"new_districts":[{"code":"N1","blocks"'
    )
    resp = builder.post(
        "/build",
        content=payload,
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400
    assert "truncated" in resp.json()["detail"] or "malformed" in resp.json()["detail"]


def test_trailing_data_rejected():
    payload = (
        b'{"old_districts":[{"code":"O1","blocks":["a"]}],'
        b'"new_districts":[{"code":"N1","blocks":["a"]}]}GARBAGE'
    )
    resp = builder.post(
        "/build",
        content=payload,
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400
    assert "trailing data" in resp.json()["detail"]


def test_trailing_garbage_after_whitespace_rejected_bytewise():
    # The junk sits behind whitespace and arrives one byte at a time.
    payload = (
        b'{"old_districts":[{"code":"O1","blocks":["a"]}],'
        b'"new_districts":[{"code":"N1","blocks":["a"]}]}  \n @x'
    )
    parser = StreamingBuildParser(8, 8)
    for i in range(len(payload)):
        parser.feed(payload[i : i + 1])
    with pytest.raises(StreamParseError, match="trailing data"):
        parser.finish()


def test_trailing_whitespace_accepted():
    payload = (
        b'  {"old_districts":[{"code":"O1","blocks":["a"]}],'
        b'"new_districts":[{"code":"N1","blocks":["a"]}]} \r\n '
    )
    parser = StreamingBuildParser(8, 8)
    for i in range(len(payload)):
        parser.feed(payload[i : i + 1])
    acc = parser.finish()
    assert set(acc._membership) == {"a"}


def test_duplicate_district_code_and_empty_lists_rejected():
    resp = builder.post(
        "/build",
        json={
            "old_districts": [
                {"code": "O1", "blocks": []},
                {"code": "O1", "blocks": []},
            ],
            "new_districts": [{"code": "N1", "blocks": []}],
        },
    )
    assert resp.status_code == 422
    assert "duplicate district code" in resp.json()["detail"]

    resp = builder.post(
        "/build",
        json={
            "old_districts": [],
            "new_districts": [{"code": "N1", "blocks": []}],
        },
    )
    assert resp.status_code == 422


def test_malformed_utf8_rejected():
    resp = builder.post(
        "/build",
        content=b'{"old_districts":[{"code":"O1","blocks":["\xff"]}],'
        b'"new_districts":[{"code":"N1","blocks":[]}]}',
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400
    assert "UTF-8" in resp.json()["detail"]


def test_malformed_roots_and_empty_body_rejected():
    for payload in (b"", b"   ", b"[]", b"null", b"123"):
        resp = builder.post(
            "/build",
            content=payload,
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 400, payload
        assert resp.json()["error"] == "invalid_json"

    # A well-formed object missing one required list is a semantic 422.
    resp = builder.post(
        "/build",
        content=b'{"old_districts":[{"code":"O1","blocks":[]}]}',
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 422
    assert "missing required field" in resp.json()["detail"]


def test_all_empty_block_lists_build_zero_package():
    resp = builder.post(
        "/build",
        json={
            "old_districts": [{"code": "O1", "blocks": []}],
            "new_districts": [{"code": "N1", "blocks": []}],
        },
    )
    assert resp.status_code == 200, resp.text
    rel = resp.json()["new_district_relations"][0]
    assert rel["derivable"] is True
    assert rel["coefficients"] == {}


def test_block_id_wrong_type_rejected():
    resp = builder.post(
        "/build",
        json={
            "old_districts": [{"code": "O1", "blocks": [1]}],
            "new_districts": [{"code": "N1", "blocks": []}],
        },
    )
    assert resp.status_code in (400, 422)
    assert "string" in resp.json()["detail"]


def test_parser_consumes_body_even_after_error():
    # Even after an early semantic error, finish() must not raise until EOF;
    # here the whole body is fed and the first recorded error is returned.
    payload = (
        b'{"old_districts":[{"code":"O1","blocks":["a","a"]}],'
        b'"new_districts":[{"code":"N1","blocks":[{"bad":1}]}]}'
    )
    parser = StreamingBuildParser(8, 8)
    parser.feed(payload)
    with pytest.raises(BuilderConstraintError) as exc:
        parser.finish()
    assert "duplicate block id" in str(exc.value)


# --------------------------------------------------------------------------
# 4. Large-scale manifest: scale of the real contract, streamed in chunks.
# --------------------------------------------------------------------------
def _large_manifest(n_old=240, n_new=240, n_blocks=120_000, blocks_per_district=500):
    old, new = [], []
    for d in range(n_old):
        start = (d * 7919) % n_blocks
        ids = [f"blk{(start + k) % n_blocks:08d}" for k in range(blocks_per_district)]
        old.append({"code": f"OLD{d:03d}", "blocks": ids})
    for d in range(n_new):
        start = (d * 104729) % n_blocks
        ids = [f"blk{(start + k) % n_blocks:08d}" for k in range(blocks_per_district)]
        new.append({"code": f"NEW{d:03d}", "blocks": ids})
    return old, new


def test_large_manifest_streaming_end_to_end():
    old, new = _large_manifest()
    payload = json.dumps(
        {"old_districts": old, "new_districts": new}
    ).encode()
    assert len(payload) > 3_000_000

    parser = StreamingBuildParser(240, 240)
    # 64 KiB chunks, like an ASGI stream.
    for i in range(0, len(payload), 65536):
        parser.feed(payload[i : i + 65536])
    acc = parser.finish()
    package = build_from_coverage(parser.old_codes, parser.new_codes, acc)

    assert package["schema_version"] == 1
    assert len(package["old_districts"]) == 240
    assert len(package["new_districts"]) == 240
    assert len(package["new_district_relations"]) == 240
    # Deterministic district ordering is preserved.
    assert package["old_districts"][0] == "OLD000"
    assert package["new_districts"][-1] == "NEW239"

    # The package is accepted by the converter unchanged.
    resp = converter.post(
        "/convert",
        json={
            "package": package,
            "old_populations": {f"OLD{d:03d}": 1000 for d in range(240)},
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["results"]) == 240


def test_district_code_ordering_preserved():
    # Non-alphabetical codes must come back in request order.
    old = [
        {"code": "Z9", "blocks": ["a"]},
        {"code": "A1", "blocks": ["b"]},
        {"code": "M5", "blocks": ["a", "b"]},
    ]
    new = [
        {"code": "n3", "blocks": ["a"]},
        {"code": "N1", "blocks": ["b"]},
        {"code": "x0", "blocks": []},
    ]
    resp = builder.post(
        "/build", json={"old_districts": old, "new_districts": new}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["old_districts"] == ["Z9", "A1", "M5"]
    assert body["new_districts"] == ["n3", "N1", "x0"]
    assert [r["code"] for r in body["new_district_relations"]] == [
        "n3",
        "N1",
        "x0",
    ]


# --------------------------------------------------------------------------
# 5. Builder internals: accumulator aggregation and caps.
# --------------------------------------------------------------------------
def test_accumulator_groups_by_old_signature():
    acc = CoverageAccumulator(2, 2)
    for block, side, idx in (
        ("b1", "old", 0),
        ("b2", "old", 0),
        ("b3", "old", 1),
        ("b1", "new", 0),
        ("b2", "new", 1),
    ):
        acc.add(block, side, idx)
    groups = acc.aggregate()
    # old signature {O1}: two blocks, new incidence OR={N1,N2} AND=empty
    g = groups[1]
    assert g[0] == 2
    assert g[1] == 0b11
    assert g[2] == 0b00


def test_district_count_cap_enforced():
    many_old = [
        {"code": f"O{i}", "blocks": ["a"]} for i in range(9)
    ]
    payload = json.dumps(
        {
            "old_districts": many_old,
            "new_districts": [{"code": "N1", "blocks": ["a"]}],
        }
    ).encode()
    parser = StreamingBuildParser(8, 8)
    parser.feed(payload)
    with pytest.raises(BuilderConstraintError):
        parser.finish()


def test_in_memory_wrapper_still_validates():
    with pytest.raises(BuilderConstraintError):
        build_package(
            [{"code": "O1", "blocks": ["a", "a"]}],
            [{"code": "N1", "blocks": []}],
        )


def test_openapi_documents_request_body():
    schema = builder.get("/openapi.json").json()
    assert "BuildRequest" in schema["components"]["schemas"]
    post = schema["paths"]["/build"]["post"]
    assert "requestBody" in post


# --------------------------------------------------------------------------
# 6. Parser robustness: every byte boundary, escapes, whitespace, unicode.
# --------------------------------------------------------------------------
def test_parser_survives_every_single_byte_boundary():
    payload = json.dumps(
        {
            "old_districts": [
                {"code": "O1", "blocks": ["a", "b", "c"]},
                {"code": "O2", "blocks": ["b", "d"]},
            ],
            "new_districts": [
                {"code": "N1", "blocks": ["a", "d"]},
            ],
        }
    ).encode()
    expected_membership = {
        "a": (1 << 0) | (1 << (2 + 0)),
        "b": (1 << 0) | (1 << 1),
        "c": 1 << 0,
        "d": (1 << 1) | (1 << (2 + 0)),
    }
    for cut in range(1, len(payload)):
        parser = StreamingBuildParser(8, 8)
        parser.feed(payload[:cut])
        parser.feed(payload[cut:])
        acc = parser.finish()
        assert acc._membership == expected_membership, cut

    # And fully byte-at-a-time, with leading/trailing whitespace.
    parser = StreamingBuildParser(8, 8)
    padded = b"  \n " + payload + b" \r\n"
    for i in range(len(padded)):
        parser.feed(padded[i : i + 1])
    acc = parser.finish()
    assert acc._membership == expected_membership


def test_parser_handles_json_escapes_and_unicode():
    payload = (
        b'{"old_districts":[{"code":"O\\"1","blocks":["a\\nb","\\u00e9",'
        b'"back\\\\slash"]}],"new_districts":[{"code":"N1","blocks":[]}]}'
    )
    parser = StreamingBuildParser(8, 8)
    # byte at a time to force escape sequences across chunk boundaries
    for i in range(len(payload)):
        parser.feed(payload[i : i + 1])
    acc = parser.finish()
    assert parser.old_codes == ['O"1']
    assert set(acc._membership) == {"a\nb", "é", "back\\slash"}


def test_random_chunking_matches_reference():
    rng = random.Random(99)
    old = [
        {"code": "O1", "blocks": ["p", "q", "r"]},
        {"code": "O2", "blocks": ["q", "s"]},
    ]
    new = [{"code": "N1", "blocks": ["p", "s"]}, {"code": "N2", "blocks": []}]
    payload = json.dumps(
        {"old_districts": old, "new_districts": new}
    ).encode()
    for _ in range(50):
        parser = StreamingBuildParser(16, 16)
        i = 0
        while i < len(payload):
            step = rng.randint(1, 7)
            parser.feed(payload[i : i + step])
            i += step
        acc = parser.finish()
        package = build_from_coverage(
            parser.old_codes, parser.new_codes, acc
        )
        assert package == _reference_build(old, new)


# --------------------------------------------------------------------------
# 7. Differential test: sparse echelon vs dense rational linear algebra.
# --------------------------------------------------------------------------
def test_sparse_echelon_matches_dense_solve_on_random_systems():
    from fractions import Fraction

    from app.builder_core import _SparseEchelon, _iter_bits
    from app.rationals import rref, nullspace, solve

    rng = random.Random(2024)
    for _ in range(300):
        n_old = rng.randint(1, 7)
        n_new = rng.randint(1, 6)
        n_rows = rng.randint(1, 12)
        a = [[rng.randint(0, 1) for _ in range(n_old)] for _ in range(n_rows)]
        b = [[rng.randint(0, 1) for _ in range(n_new)] for _ in range(n_rows)]

        dense_underivable = set()
        for j in range(n_new):
            col = [Fraction(b[r][j]) for r in range(n_rows)]
            if solve(a, col, n_old) is None:
                dense_underivable.add(j)

        ech = _SparseEchelon(n_new)
        for r in range(n_rows):
            old_mask = sum(1 << c for c in range(n_old) if a[r][c])
            rhs_mask = sum(1 << j for j in range(n_new) if b[r][j])
            ech.insert(old_mask, rhs_mask)
        pivot_cols = sorted(ech.pivots)
        rank = len(pivot_cols)
        aug = [
            [Fraction(0) for _ in range(n_old + n_new)] for _ in range(rank)
        ]
        for i, col in enumerate(pivot_cols):
            prow, prhs = ech.pivots[col]
            aug[i][col] = Fraction(1)
            for k, v in prow.items():
                aug[i][k] = v
            for j, v in prhs.items():
                aug[i][n_old + j] = v
        reduced, _ = rref(aug)
        sparse_underivable = set(_iter_bits(ech.zero_rhs_mask))
        for row in reduced:
            if all(v == 0 for v in row[:n_old]):
                for j in range(n_new):
                    if row[n_old + j] != 0:
                        sparse_underivable.add(j)

        assert sparse_underivable == dense_underivable
        assert len(nullspace(a, n_old)) == n_old - rank
