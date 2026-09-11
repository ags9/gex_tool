"""Per-point GEX units (public convention) + spot-marker tolerance fix."""
from pathlib import Path
p = Path("gexbot/levels.py")
src = p.read_text()

a1 = "def build_profile(contracts: list[dict], spot: float, *,\n                  model: str = \"naive\","
a2 = "        gex = gamma * oi * 100 * spot * spot * 0.01"
a3 = "        if abs(k - spot) < 12.5:"
for k, a in (("A1", a1), ("A2", a2), ("A3", a3)):
    assert a in src, f"ANCHOR {k} NOT FOUND"

src = src.replace(a1, a1.replace("model: str = \"naive\",",
                                 "model: str = \"naive\", per_point: bool = True,"), 1)
src = src.replace(a2,
"""        # per-point (public GEX convention): gamma * OI * 100 * spot
        # per-1%: multiply by spot * 0.01 as well
        gex = gamma * oi * 100 * spot
        if not per_point:
            gex *= spot * 0.01""", 1)
src = src.replace(a3, "        if abs(k - spot) <= 2.5:", 1)

# thread the flag through show_levels
src = src.replace(
    "def show_levels(underlying: str = \"I:SPX\", model: str = \"naive\",\n                expiries: int = 2, window: float = 0.06) -> None:",
    "def show_levels(underlying: str = \"I:SPX\", model: str = \"naive\",\n                expiries: int = 2, window: float = 0.06,\n                per_point: bool = True) -> None:", 1)
src = src.replace(
    "    render(build_profile(contracts, spot, model=model,\n                         expiries=expiries, strike_window=window), model)",
    "    render(build_profile(contracts, spot, model=model, per_point=per_point,\n                         expiries=expiries, strike_window=window), model)", 1)
src = src.replace('console.rule(f"[bold]SPX GEX — {dt.datetime.now():%Y-%m-%d %H:%M} "',
                  'console.rule(f"[bold]SPX GEX — {dt.datetime.now():%Y-%m-%d %H:%M} "', 1)
p.write_text(src)

m = Path("gexbot/__main__.py")
s = m.read_text()
a4 = '    lv.add_argument("--window", type=float, default=0.06,'
assert a4 in s, "ANCHOR A4 NOT FOUND"
s = s.replace(a4, '    lv.add_argument("--per-1pct", action="store_true",\n'
                  '                    help="quote $GEX per 1%% move instead of per point")\n' + a4, 1)
s = s.replace("        show_levels(args.underlying, args.model, args.expiries, args.window)",
              "        show_levels(args.underlying, args.model, args.expiries, args.window,\n"
              "                    per_point=not args.per_1pct)", 1)
m.write_text(s)
print("units fixed:", "per_point" in src, "| cli:", "per-1pct" in s)
