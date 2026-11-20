"""Differential test: sparse echelon pipeline vs dense rationals, many random matrices."""
import sys, random
sys.path.insert(0, "/workspace")
from fractions import Fraction
from app.builder_core import _SparseEchelon, _iter_bits
from app.rationals import rref, nullspace, solve

rng = random.Random(2024)
trials = 400
for t in range(trials):
    n_old = rng.randint(1, 7)
    n_new = rng.randint(1, 6)
    n_rows = rng.randint(1, 12)
    # random 0/1 matrix A (rows) and B (rhs columns), with repeated rows allowed
    A = [[rng.randint(0, 1) for _ in range(n_old)] for _ in range(n_rows)]
    B = [[rng.randint(0, 1) for _ in range(n_new)] for _ in range(n_rows)]

    # Dense reference: for each new column j, solve A c = b_j consistency.
    dense_underivable = set()
    for j in range(n_new):
        col = [Fraction(B[r][j]) for r in range(n_rows)]
        if solve(A, col, n_old) is None:
            dense_underivable.add(j)

    # Sparse pipeline (without twin rule; that is tested separately).
    ech = _SparseEchelon(n_new)
    for r in range(n_rows):
        om = sum(1 << c for c in range(n_old) if A[r][c])
        rm = sum(1 << j for j in range(n_new) if B[r][j])
        ech.insert(om, rm)
    pivot_cols = sorted(ech.pivots)
    rank = len(pivot_cols)
    aug = [[Fraction(0) for _ in range(n_old + n_new)] for _ in range(rank)]
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

    assert sparse_underivable == dense_underivable, (
        f"trial {t}: sparse {sorted(sparse_underivable)} != dense {sorted(dense_underivable)}"
    )

    # Kernel dimension must agree too.
    ker = nullspace(A, n_old)
    assert len(ker) == n_old - rank, f"trial {t} kernel dim mismatch"

print(f"differential test OK over {trials} random 0/1 systems")
