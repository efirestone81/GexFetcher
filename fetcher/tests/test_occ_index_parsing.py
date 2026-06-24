"""
Validate OCC symbol parsing against real SPX/NDX index option formats.

Index options have multiple roots:
  SPX   — AM-settled, 3rd-Friday monthly
  SPXW  — PM-settled weeklies + dailies (includes all 0DTE)
  SPXQ  — quarterlies (rare)
  NDX   — Nasdaq-100 AM-settled monthly
  NDXP  — Nasdaq-100 PM-settled weeklies
  NQX   — mini-NDX (1/40th) — should be EXCLUDED

The OCC 21-char format is: ROOT (up to 6, padded) + YYMMDD + C/P + 8-digit strike.
But CBOE CDN JSON typically uses the *unpadded* root form, e.g.:
  SPXW260717C05000000
  SPX260619C05000000
  NDXP260717P20000000

Strike encoding: 8 digits, 3 implied decimals.
  05000000 → 5000.000
  20000000 → 20000.000  (NDX scale)
"""
import re

# Current parser regex from gex_engine.py
OCC_RE = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")


def parse_occ(symbol):
    m = OCC_RE.match(symbol)
    if not m:
        return None
    root, yymmdd, cp, strike_raw = m.groups()
    strike = int(strike_raw) / 1000.0
    return root, yymmdd, cp == "C", strike


# Real-world SPX/NDX index option symbol samples
SAMPLES = [
    # (symbol, expected_root, expected_is_call, expected_strike)
    ("SPX260619C05000000", "SPX", True, 5000.0),       # AM monthly call
    ("SPX260619P05000000", "SPX", False, 5000.0),      # AM monthly put
    ("SPXW260717C07400000", "SPXW", True, 7400.0),     # weekly call
    ("SPXW260624P07300000", "SPXW", False, 7300.0),    # 0DTE put
    ("SPXW260717C07500000", "SPXW", True, 7500.0),     # call wall area
    ("SPXQ260930C07000000", "SPXQ", True, 7000.0),     # quarterly
    ("NDX260619C29000000", "NDX", True, 29000.0),      # NDX monthly call
    ("NDXP260717P28000000", "NDXP", False, 28000.0),   # NDX weekly put
    ("NDX260619C30000000", "NDX", True, 30000.0),      # high strike
    # Fractional strike (some SPX strikes have .5 — encoded as 500 in last 3)
    ("SPXW260717C07425500", "SPXW", True, 7425.5),     # 7425.50 strike
]

# Symbols that should be EXCLUDED (mini contracts)
EXCLUDE_SAMPLES = [
    "NQX260717C07000000",   # mini-NDX — parses but we filter by root
    "XSP260717C00740000",   # mini-SPX — parses but we filter by root
]


def test_all_index_roots_parse():
    print("=== Parsing index option symbols ===")
    all_ok = True
    for sym, exp_root, exp_call, exp_strike in SAMPLES:
        result = parse_occ(sym)
        if result is None:
            print(f"  FAIL: {sym} did not parse")
            all_ok = False
            continue
        root, yymmdd, is_call, strike = result
        ok = (root == exp_root and is_call == exp_call and abs(strike - exp_strike) < 0.001)
        status = "PASS" if ok else "FAIL"
        if not ok:
            all_ok = False
        print(f"  {status}: {sym} -> root={root} call={is_call} strike={strike}")
        if not ok:
            print(f"        expected root={exp_root} call={exp_call} strike={exp_strike}")
    assert all_ok, "Some index symbols failed to parse correctly"


def test_mini_contracts_parse_but_identifiable():
    """NQX and XSP parse fine structurally; we exclude them by root name."""
    print("\n=== Mini contracts (to be excluded by root filter) ===")
    for sym in EXCLUDE_SAMPLES:
        result = parse_occ(sym)
        assert result is not None, f"{sym} should still parse"
        root, _, _, _ = result
        print(f"  {sym} -> root={root} (will be excluded by root filter)")
        assert root in ("NQX", "XSP")


def test_strike_scale_ndx():
    """NDX strikes are ~20000-30000; verify the /1000 scaling is right."""
    result = parse_occ("NDX260619C29000000")
    _, _, _, strike = result
    assert strike == 29000.0, f"NDX strike scale wrong: {strike}"
    print(f"\n=== NDX strike scaling OK: 29000000 -> {strike} ===")


if __name__ == "__main__":
    test_all_index_roots_parse()
    test_mini_contracts_parse_but_identifiable()
    test_strike_scale_ndx()
    print("\nAll OCC parsing checks passed.")
