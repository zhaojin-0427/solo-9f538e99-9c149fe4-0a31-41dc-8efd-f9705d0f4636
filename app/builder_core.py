"""Memory-bounded core for building the block-free handoff package.

The naive implementation materialises one dense incidence row per street
block (up to 600 000 rows of :class:`~fractions.Fraction`), which exhausts a
192 MiB address space while the request JSON is still being read or while the
package is being built.

This module keeps the memory footprint small by construction:

* Block memberships are packed into a single Python ``int`` bit mask and held
  in one ``dict`` keyed by block id (one entry per block, a few dozen bytes
  each).
* Rows with the same old-district incidence are indistinguishable for every
  linear-algebra computation here, so they are aggregated into *groups*
  described by OR/AND masks of the new-district incidence.
* Gaussian elimination runs on the distinct rows only, in sparse dictionary
  form, and the (at most 240-column) dense RREF is taken once at the end.

The math is identical to the former dense builder: the RREF of a matrix over
the rationals is unique, so the old-district kernel basis and every
particular solution of ``A c = b`` come out byte-for-byte the same.
"""
from fractions import Fraction
from typing import Any, Dict, Iterable, List, Sequence, Tuple

from .expressions import format_fraction
from .rationals import Vec, nullspace, primitive_integer, rref


class BuilderConstraintError(ValueError):
    """Semantic validation failure of a /build request (mapped to HTTP 422)."""


# --------------------------------------------------------------------------
# Streaming coverage accumulator
# --------------------------------------------------------------------------
class CoverageAccumulator:
    """Collects block memberships without storing per-district block lists.

    Each block id maps to one packed integer: bits ``0 .. n_old-1`` mark the
    old districts containing the block and bits ``n_old ..`` the new ones.
    Old-side bits can be recorded before ``n_old`` is finalised because they
    use the district index directly; ``n_new`` is supplied once the
    new-district list has been parsed.
    """

    def __init__(self, n_old: int = 0, n_new: int = 0):
        self.n_old = n_old
        self.n_new = n_new
        self._membership: Dict[str, int] = {}

    def set_dimensions(self, n_old: int, n_new: int) -> None:
        self.n_old = n_old
        self.n_new = n_new

    def add(self, block_id: str, side: str, district_index: int) -> None:
        if side == "old":
            bit = 1 << district_index
        else:
            bit = 1 << (self.n_old + district_index)
        self._membership[block_id] = self._membership.get(block_id, 0) | bit

    def __len__(self) -> int:
        return len(self._membership)

    def aggregate(self) -> Dict[int, List[int]]:
        """Group blocks by old-district signature.

        Returns ``{old_mask: [count, new_or_mask, new_and_mask]}``.  For every
        new-district column the incidence is constant inside a group exactly
        when its OR and AND bits agree; a mismatch proves the column is not a
        function of the old incidence and therefore cannot be expressed from
        old districts (the "twin blocks" rule).
        """
        old_keep = (1 << self.n_old) - 1
        groups: Dict[int, List[int]] = {}
        for packed in self._membership.values():
            old_mask = packed & old_keep
            new_mask = packed >> self.n_old
            group = groups.get(old_mask)
            if group is None:
                groups[old_mask] = [1, new_mask, new_mask]
            else:
                group[0] += 1
                group[1] |= new_mask
                group[2] &= new_mask
        return groups

    def release(self) -> None:
        """Drop the per-block table before the (small) linear-algebra phase."""
        self._membership.clear()


def _iter_bits(mask: int) -> Iterable[int]:
    while mask:
        low = mask & -mask
        yield low.bit_length() - 1
        mask ^= low


# --------------------------------------------------------------------------
# Sparse forward elimination (row echelon, pivot entries normalised to 1)
# --------------------------------------------------------------------------
class _SparseEchelon:
    def __init__(self, n_new: int):
        self.n_new = n_new
        self.pivots: Dict[int, Tuple[Dict[int, Fraction], Dict[int, Fraction]]] = {}
        self.zero_rhs_mask = 0
        self.rank = 0

    def insert(self, old_mask: int, rhs_mask: int) -> None:
        row = {c: Fraction(1) for c in _iter_bits(old_mask)}
        rhs = {j: Fraction(1) for j in _iter_bits(rhs_mask)}
        while row:
            col = min(row)
            pivot = self.pivots.get(col)
            if pivot is None:
                pivot_value = row.pop(col)
                if pivot_value != 1:
                    row = {k: v / pivot_value for k, v in row.items()}
                    rhs = {k: v / pivot_value for k, v in rhs.items()}
                self.pivots[col] = (row, rhs)
                self.rank += 1
                return
            pivot_row, pivot_rhs = pivot
            factor = row.pop(col)  # pivot entry is 1
            for k, v in pivot_row.items():
                value = row.get(k, Fraction(0)) - factor * v
                if value:
                    row[k] = value
                else:
                    row.pop(k, None)
            for k, v in pivot_rhs.items():
                value = rhs.get(k, Fraction(0)) - factor * v
                if value:
                    rhs[k] = value
                else:
                    rhs.pop(k, None)
        # The old-incidence row reduced to zero: any surviving RHS entry is a
        # witness that this new-district column is outside col(A).
        for j in rhs:
            self.zero_rhs_mask |= 1 << j


# --------------------------------------------------------------------------
# Package assembly
# --------------------------------------------------------------------------
def build_from_coverage(
    old_codes: Sequence[str],
    new_codes: Sequence[str],
    accumulator: CoverageAccumulator,
) -> Dict[str, Any]:
    """Assemble the v1 handoff package from accumulated coverage data."""
    n_old = len(old_codes)
    n_new = len(new_codes)

    groups = accumulator.aggregate()
    accumulator.release()

    # Columns whose incidence varies between blocks sharing the exact same
    # old-district membership are necessarily underivable.
    mismatch_mask = 0
    echelon = _SparseEchelon(n_new)
    for old_mask, (_count, new_or, new_and) in groups.items():
        mismatch_mask |= new_or ^ new_and
        # Any representative RHS works on mismatching columns; their verdict
        # is already fixed.  Use the AND mask so consistent columns keep
        # their true (constant) value.
        echelon.insert(old_mask, new_and)

    # Dense RREF of the small echelon matrix, carrying every new column as
    # an augmented RHS.  At most rank x (n_old + n_new), i.e. <= 240 x 480.
    pivot_cols = sorted(echelon.pivots)
    rank = len(pivot_cols)
    augmented: List[List[Fraction]] = [
        [Fraction(0) for _ in range(n_old + n_new)] for _ in range(rank)
    ]
    for i, col in enumerate(pivot_cols):
        row, rhs = echelon.pivots[col]
        augmented[i][col] = Fraction(1)
        for k, v in row.items():
            augmented[i][k] = v
        for j, v in rhs.items():
            augmented[i][n_old + j] = v
    reduced, dense_pivots = rref(augmented)

    # ker(A): exact integer equations between old-district populations.
    old_part = [row[:n_old] for row in reduced]
    kernel = nullspace(old_part, n_old)
    old_relations: List[Dict[str, int]] = []
    for k in kernel:
        ints = primitive_integer(k)
        old_relations.append(
            {code: value for code, value in zip(old_codes, ints) if value != 0}
        )

    # A zero old-part row with a surviving RHS entry is the dense-RREF form
    # of the consistency witness collected sparsely above.
    zero_rhs_mask = echelon.zero_rhs_mask
    for row in reduced:
        if all(v == 0 for v in row[:n_old]):
            for j in range(n_new):
                if row[n_old + j] != 0:
                    zero_rhs_mask |= 1 << j

    underivable_mask = mismatch_mask | zero_rhs_mask

    new_relations = []
    for j, code in enumerate(new_codes):
        if (underivable_mask >> j) & 1:
            new_relations.append(
                {"code": code, "derivable": False, "coefficients": {}}
            )
            continue
        particular: Vec = [Fraction(0) for _ in range(n_old)]
        for i, pivot in enumerate(dense_pivots):
            if pivot < n_old:
                particular[pivot] = reduced[i][n_old + j]
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
        "old_districts": list(old_codes),
        "new_districts": list(new_codes),
        "old_relations": old_relations,
        "new_district_relations": new_relations,
    }


def build_package(
    old_districts: List[Dict[str, Any]],
    new_districts: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """In-memory counterpart of the streaming /build pipeline.

    Accepts the same ``[{"code": ..., "blocks": [...]}]`` payloads as the
    legacy entry point and performs the same semantic validation, but routes
    through the memory-bounded accumulator so the two paths cannot diverge.
    """
    old_codes = [d["code"] for d in old_districts]
    new_codes = [d["code"] for d in new_districts]
    _validate_district_lists("old_districts", old_districts)
    _validate_district_lists("new_districts", new_districts)

    accumulator = CoverageAccumulator(len(old_codes), len(new_codes))
    for i, district in enumerate(old_districts):
        for block_id in district["blocks"]:
            accumulator.add(block_id, "old", i)
    for j, district in enumerate(new_districts):
        for block_id in district["blocks"]:
            accumulator.add(block_id, "new", j)
    return build_from_coverage(old_codes, new_codes, accumulator)


def _validate_district_lists(label: str, districts: List[Dict[str, Any]]) -> None:
    if not districts:
        raise BuilderConstraintError(f"{label} must not be empty")
    codes = [d["code"] for d in districts]
    if len(set(codes)) != len(codes):
        raise BuilderConstraintError(f"duplicate district code in {label}")
    for district in districts:
        blocks = district["blocks"]
        if len(blocks) != len(set(blocks)):
            raise BuilderConstraintError(
                f"duplicate block id in district {district['code']}"
            )
