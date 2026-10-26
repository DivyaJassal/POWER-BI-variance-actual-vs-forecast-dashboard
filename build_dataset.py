
import glob, os
import pandas as pd

LAST_QUARTER = "2026Q3"   # calendar quarter of the newest result (Jul-Sep 2026 = 2026Q3)
os.makedirs("clean", exist_ok=True)
os.makedirs("model", exist_ok=True)

def fiscal_label(ts):
    fy = ts.year + 1 if ts.month >= 4 else ts.year
    q = {6: 1, 9: 2, 12: 3, 3: 4}[ts.month]
    return f"Q{q} FY{str(fy)[-2:]}"

def read_company(path):
    xl = pd.ExcelFile(path)
    sheet = [s for s in xl.sheet_names if s.lower().startswith(("quat", "quar"))][0]
    raw = xl.parse(sheet, header=None)
    raw[0] = raw[0].astype(str).str.strip()
    def row(label):
        vals = raw.loc[raw[0] == label].iloc[0, 1:]
        return pd.to_numeric(vals, errors="coerce").dropna().tolist()
    sales, ebitda, pat = row("Sales"), row("Operating Profit"), row("Net profit")
    n = len(sales)
    assert n == len(ebitda) == len(pat), f"{path}: row lengths differ"
    # Screener's date header is unreliable after copy/paste, so derive quarter ends
    # counting backwards from the latest reported quarter.
    ends = pd.period_range(end=LAST_QUARTER, periods=n, freq="Q").to_timestamp(how="end").normalize()
    df = pd.DataFrame({"company": os.path.splitext(os.path.basename(path))[0],
                       "quarter_end": ends, "revenue": sales, "ebitda": ebitda, "pat": pat})
    df["fiscal_quarter"] = df.quarter_end.map(fiscal_label)
    return df

master = pd.concat([read_company(f) for f in sorted(glob.glob("raw/*.xlsx"))], ignore_index=True)

# ---- data-quality checks ----
checks = {
    "no missing values": master[["revenue", "ebitda", "pat"]].notna().all().all(),
    "no duplicate company-quarter": not master.duplicated(["company", "quarter_end"]).any(),
    "revenue > 0": (master.revenue > 0).all(),
    "EBITDA margin within 15-30%": ((master.ebitda / master.revenue).between(0.15, 0.30)).all(),
    "equal quarter count per company": master.groupby("company").size().nunique() == 1,
}
print("DATA-QUALITY CHECKS")
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'}  {k}")
master.to_csv("clean/master.csv", index=False)

# ---- backtest + forward forecast ----
test_rows, fwd_rows = [], []
for c, g in master.groupby("company"):
    g = g.sort_values("quarter_end").reset_index(drop=True)
    for t in range(5, len(g)):
        yoy_prev = g.revenue[t-1] / g.revenue[t-5] - 1
        fcA = g.revenue[t-4] * (1 + yoy_prev)               # Method A: seasonal growth
        fcB = g.revenue[t-1]                                 # Method B: last quarter
        ebitda_m = (g.ebitda[t-4:t] / g.revenue[t-4:t]).mean()
        pat_m = (g.pat[t-4:t] / g.revenue[t-4:t]).mean()
        test_rows.append([c, g.quarter_end[t], g.fiscal_quarter[t], g.revenue[t], fcA, fcB,
                          g.ebitda[t], fcA * ebitda_m, g.pat[t], fcA * pat_m])
    # next 2 quarters (Method A, last observed YoY growth held constant)
    last_yoy = g.revenue.iloc[-1] / g.revenue.iloc[-5] - 1
    hist = g.copy()
    for step in range(2):
        nxt_end = (hist.quarter_end.iloc[-1] + pd.offsets.QuarterEnd(1)).normalize()
        rev = hist.revenue.iloc[-4] * (1 + last_yoy)
        em = (hist.ebitda.iloc[-4:] / hist.revenue.iloc[-4:]).mean()
        pm = (hist.pat.iloc[-4:] / hist.revenue.iloc[-4:]).mean()
        fwd_rows.append([c, nxt_end, fiscal_label(nxt_end), rev, rev * em, rev * pm])
        hist = pd.concat([hist, pd.DataFrame({"company": [c], "quarter_end": [nxt_end],
                          "revenue": [rev], "ebitda": [rev*em], "pat": [rev*pm]})], ignore_index=True)

v = pd.DataFrame(test_rows, columns=["company", "quarter_end", "fiscal_quarter", "actual_rev", "fc_rev_A",
                                     "fc_rev_B", "actual_ebitda", "fc_ebitda_A", "actual_pat", "fc_pat_A"])
for m in ("A", "B"):
    v[f"var_rev_{m}"] = v.actual_rev - v[f"fc_rev_{m}"]
    v[f"var_pct_rev_{m}"] = v[f"var_rev_{m}"] / v[f"fc_rev_{m}"] * 100
v["var_pct_ebitda_A"] = (v.actual_ebitda - v.fc_ebitda_A) / v.fc_ebitda_A * 100
v["var_pct_pat_A"] = (v.actual_pat - v.fc_pat_A) / v.fc_pat_A * 100
v.round(2).to_csv("model/variance.csv", index=False)
pd.DataFrame(fwd_rows, columns=["company", "quarter_end", "fiscal_quarter", "fc_revenue", "fc_ebitda", "fc_pat"]
             ).round(0).to_csv("model/next_forecast.csv", index=False)

print("\nBACKTEST:", len(v), "company-quarters")
print("  Revenue MAPE  Method A:", round(v.var_pct_rev_A.abs().mean(), 2), "%")
print("  Revenue MAPE  Method B:", round(v.var_pct_rev_B.abs().mean(), 2), "%")
print("  EBITDA  MAPE  Method A:", round(v.var_pct_ebitda_A.abs().mean(), 2), "%")
print("  PAT     MAPE  Method A:", round(v.var_pct_pat_A.abs().mean(), 2), "%")
print("  Quarters beyond +/-5% revenue variance (A):", int((v.var_pct_rev_A.abs() > 5).sum()))
print("\n", v[["company", "fiscal_quarter", "actual_rev", "fc_rev_A", "var_pct_rev_A", "fc_rev_B", "var_pct_rev_B"]].round(2).to_string(index=False))
print("\nNEXT 2 QUARTERS:\n", pd.read_csv("model/next_forecast.csv").to_string(index=False))