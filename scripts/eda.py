"""Offline EDA of the public MOEX snapshot; never accesses brokerage credentials."""

import hashlib
import json
import os
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", "/tmp/invest-eda-mpl")
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "datasets/raw/moex_bonds.json"
OUT = ROOT / "reports"
OUT.mkdir(exist_ok=True)
(OUT / "figures").mkdir(exist_ok=True)
raw = json.loads(RAW.read_text())
tables = {k: pd.DataFrame(v["data"], columns=v["columns"]) for k, v in raw.items()}
s, m, y = (tables[k] for k in ("securities", "marketdata", "marketdata_yields"))
keys = ["SECID", "BOARDID"]
for name, table in tables.items():
    assert not table.duplicated(keys).any(), f"duplicate keys: {name}"
assert len(m.merge(s[keys], on=keys, how="left", indicator=True).query('_merge != "both"')) == 0
frame = s.merge(m, on=keys, how="left", suffixes=("", "_market"), validate="one_to_one")
frame = frame.merge(y, on=keys, how="left", suffixes=("", "_yields"), validate="one_to_one")
fields = [
    "ISIN",
    "FACEUNIT",
    "MATDATE",
    "BID",
    "OFFER",
    "LAST",
    "VALTODAY_RUR",
    "EFFECTIVEYIELDWAPRICE",
    "DURATIONWAPRICE",
    "TRADEMOMENT",
]
missing = frame[fields].isna().mean().sort_values()
fig, ax = plt.subplots(figsize=(10, 5))
missing.mul(100).plot.barh(ax=ax, color="#4477AA")
ax.set(xlabel="Missing values, % of instrument-board rows", title="MOEX snapshot: field coverage")
fig.tight_layout()
fig.savefig(OUT / "figures/missingness.png", dpi=160)
plt.close(fig)
turnover = pd.to_numeric(frame.VALTODAY_RUR, errors="coerce")
bid = pd.to_numeric(frame.BID, errors="coerce")
ask = pd.to_numeric(frame.OFFER, errors="coerce")
valid_quote = (bid > 0) & (ask >= bid)
rub = frame.FACEUNIT.isin(["RUB", "SUR"])
yield_ = pd.to_numeric(frame.EFFECTIVEYIELDWAPRICE, errors="coerce")
duration = pd.to_numeric(frame.DURATIONWAPRICE, errors="coerce")
fig, axes = plt.subplots(1, 2, figsize=(12, 4))
frame.FACEUNIT.fillna("missing").value_counts().plot.bar(ax=axes[0], color="#4477AA")
axes[0].set(title="Face-value currency", ylabel="Instrument-board rows", xlabel="")
counts = [
    len(frame),
    int(rub.sum()),
    int((rub & (turnover > 0)).sum()),
    int((rub & (turnover > 0) & yield_.notna() & (duration > 0)).sum()),
    int((rub & (turnover > 0) & yield_.notna() & (duration > 0) & valid_quote).sum()),
]
axes[1].barh(
    ["All rows", "RUB", "+ turnover > 0", "+ yield & duration", "+ two-sided quote"], counts
)
axes[1].set(title="Sequential data-availability filters", xlabel="Rows (not buy recommendations)")
fig.tight_layout()
fig.savefig(OUT / "figures/coverage.png", dpi=160)
plt.close(fig)
mask = rub & yield_.notna() & (duration > 0)
fig, axes = plt.subplots(1, 2, figsize=(12, 4))
# Display range is explicit; extreme observations remain in summary and CSV.
shown = mask & yield_.between(-10, 100)
axes[0].scatter(duration[shown] / 365.25, yield_[shown], s=9, alpha=0.4)
axes[0].set(
    title=f"RUB yield vs duration: {shown.sum()} rows; {(mask & ~shown).sum()} outside view",
    xlabel="Duration, years",
    ylabel="Effective yield at WAP, %",
)
spread = ((ask - bid) / ((ask + bid) / 2) * 100)[valid_quote & rub]
if spread.empty:
    axes[1].text(
        0.5, 0.5, "No two-sided quotes in this snapshot", ha="center", transform=axes[1].transAxes
    )
else:
    axes[1].hist(spread.clip(upper=5), bins=40)
axes[1].set(
    title=f"RUB relative spread; {(spread > 5).sum()} rows capped at 5%",
    xlabel="Spread / midpoint, %",
    ylabel="Rows",
)
fig.tight_layout()
fig.savefig(OUT / "figures/yield_liquidity.png", dpi=160)
plt.close(fig)
summary = {
    "sha256": hashlib.sha256(RAW.read_bytes()).hexdigest(),
    "table_rows": {k: len(v) for k, v in tables.items()},
    "unique_secid": int(s.SECID.nunique()),
    "unique_isin": int(s.ISIN.nunique()),
    "repeated_secid_rows": int(s.SECID.duplicated().sum()),
    "missing_percent": (missing * 100).round(3).to_dict(),
    "funnel_counts": counts,
    "valid_two_sided_quotes": int(valid_quote.sum()),
    "crossed_quotes": int(((bid > 0) & (ask > 0) & (ask < bid)).sum()),
    "rub_yield_quantiles": yield_[mask].quantile([0, 0.25, 0.5, 0.75, 0.95, 1]).to_dict(),
    "rub_spread_quantiles_percent": (
        {str(k): float(v) for k, v in spread.quantile([0, 0.5, 0.95, 1]).items()}
        if not spread.empty
        else {}
    ),
    "trade_moment_min": str(frame.TRADEMOMENT.dropna().min()),
    "trade_moment_max": str(frame.TRADEMOMENT.dropna().max()),
    "system_time_min": str(m.SYSTIME.dropna().min()),
    "system_time_max": str(m.SYSTIME.dropna().max()),
    "currency_counts": s.FACEUNIT.value_counts(dropna=False).to_dict(),
}
(OUT / "eda_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
frame.to_csv(OUT / "bond_snapshot.csv", index=False)
print(json.dumps(summary, ensure_ascii=False, indent=2))
