"""Lithium monitor cloud job - runs in GitHub Actions.

Feeds per run (each isolated: a failure keeps last values and the run still commits):
  fx            frankfurter.app (ECB reference, keyless)
  gfex-akshare  GFEX LC daily bar via AKShare (Sina): close, volume, open interest
  spot-99qh     100ppi (生意社) spot + dominant-contract settlement via AKShare
                (label kept as spot-99qh so existing logs stay comparable)
  equities      LTR.AX and PLS.AX daily closes via yfinance (Yahoo, unofficial)
Manual by design: GFEX receipts, SGX LICF, SC6 spodumene, SMM inventory, cathode output.
"""
import csv, json, os, sys, traceback
from datetime import datetime, timezone, timedelta

SNAP, CSV = "market_snapshot.json", "market_daily.csv"
CONTRACT = os.environ.get("GFEX_CONTRACT", "LC2701")
PER = timezone(timedelta(hours=8))
SERIES = ("smm_spot", "gfex_futures", "sgx_licf", "gfex_receipts", "spodumene", "equities")


def tofloat(x):
    try:
        v = float(str(x).replace(",", ""))
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def load():
    if os.path.exists(SNAP):
        with open(SNAP, encoding="utf-8") as f:
            s = json.load(f)
    else:
        s = {"schema": "lithium-monitor/2", "generated_at": "", "fx_usdcny": 6.6665,
             "feeds": {}, "series": {}, "context": []}
    s.setdefault("series", {})
    s.setdefault("feeds", {})
    for k in SERIES:
        s["series"].setdefault(k, [])
    return s


def upsert(series, date, **vals):
    row = next((r for r in series if r.get("date") == date), None)
    if row is None:
        row = {"date": date}
        series.append(row)
    for k, v in vals.items():
        if v is not None:
            row[k] = v
    series.sort(key=lambda r: r["date"])


def fetch_fx():
    import requests
    r = requests.get("https://api.frankfurter.app/latest?from=USD&to=CNY", timeout=20)
    return r.json()["rates"]["CNY"] if r.ok else None


def fetch_gfex_akshare(s):
    import akshare as ak
    df = ak.futures_zh_daily_sina(symbol=CONTRACT)
    for _, row in df.tail(3).iterrows():
        d = str(row["date"])[:10]
        upsert(s["series"]["gfex_futures"], d, contract=CONTRACT,
               close=tofloat(row["close"]), volume=tofloat(row.get("volume")),
               oi=tofloat(row.get("hold")),
               note="Sina daily bar via AKShare; settlement replaces close when 100ppi supplies it")


def fetch_spot_100ppi(s):
    import akshare as ak
    smm = s["series"]["smm_spot"]
    got, d = 0, datetime.now(PER).date()
    for _ in range(12):
        if got >= 5:
            break
        if d.weekday() < 5:
            df = None
            try:
                df = ak.futures_spot_price(date=d.strftime("%Y%m%d"), symbols="LC")
            except Exception as e:
                print("  100ppi", d, "skipped:", type(e).__name__, str(e)[:120])
            if df is not None and len(df):
                if "symbol" in df.columns:
                    m = df[df["symbol"].astype(str).str.upper().str.startswith("LC")]
                    df = m if len(m) else df
                row = df.iloc[0]
                pick = lambda *n: next((row[c] for c in n if c in df.columns), None)
                iso = d.isoformat()
                sp = tofloat(pick("spot_price", "sp", "现货价格"))
                dom_sym = str(pick("dominant_contract", "dom_symbol", "主力合约代码") or "").lower()
                dom_px = tofloat(pick("dominant_contract_price", "dom_price", "主力合约价格"))
                if sp:
                    got += 1
                    if not any(r["date"] == iso and r.get("avg") for r in smm):
                        upsert(smm, iso, avg=sp, note="100ppi battery-grade spot via AKShare - proxy for SMM")
                if dom_px and dom_sym.endswith(CONTRACT.lower()[-4:]):
                    upsert(s["series"]["gfex_futures"], iso, contract=CONTRACT,
                           settle=dom_px, note="dominant-contract settlement via 100ppi/AKShare")
            elif df is not None:
                print("  100ppi", d, "empty (non-trading day or no data)")
        d -= timedelta(days=1)
    if not got:
        raise RuntimeError("no 100ppi spot rows returned")
    s["feeds"]["smm_spot"] = {"status": "proxy", "last_date": smm[-1]["date"],
        "note": "Automated leg is 100ppi battery-grade spot via AKShare - a proxy for SMM. Existing exact SMM rows are never overwritten."}


def fetch_equities(s):
    import yfinance as yf
    eq = s["series"]["equities"]
    h = yf.download(["LTR.AX", "PLS.AX"], period="10d", interval="1d",
                    progress=False, auto_adjust=False)["Close"]
    n = 0
    for ts, row in h.iterrows():
        ltr, pls = tofloat(row.get("LTR.AX")), tofloat(row.get("PLS.AX"))
        if ltr is None and pls is None:
            continue
        upsert(eq, ts.strftime("%Y-%m-%d"), ltr=ltr, pls=pls)
        n += 1
    if not n:
        raise RuntimeError("no equity rows returned")
    s["feeds"]["equities"] = {"status": "ok", "last_date": eq[-1]["date"],
        "note": "LTR.AX and PLS.AX closes via yfinance (Yahoo, unofficial endpoint). The latest bar can be intraday."}


def write_csv(s):
    maps = {k: {r["date"]: r for r in s["series"].get(k, [])} for k in SERIES}
    dates = sorted({d for m in maps.values() for d in m})
    with open(CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "smm_spot_avg", "gfex_lc_settle", "gfex_lc_close", "gfex_oi",
                    "gfex_volume", "sgx_licf_usd_kg", "gfex_receipts",
                    "ltr_close", "pls_close", "spodumene_sc6_usd_t"])
        for d in dates:
            smm, gf = maps["smm_spot"].get(d, {}), maps["gfex_futures"].get(d, {})
            sg, rc = maps["sgx_licf"].get(d, {}), maps["gfex_receipts"].get(d, {})
            eq, sp = maps["equities"].get(d, {}), maps["spodumene"].get(d, {})
            w.writerow([d, smm.get("avg", ""), gf.get("settle", ""), gf.get("close", ""),
                        gf.get("oi", ""), gf.get("volume", ""), sg.get("price_usd_kg", ""),
                        rc.get("quantity", ""), eq.get("ltr", ""), eq.get("pls", ""),
                        sp.get("price_usd_t", "")])


def main():
    s = load()
    steps = [("fx", None), ("gfex-akshare", lambda: fetch_gfex_akshare(s)),
             ("spot-99qh", lambda: fetch_spot_100ppi(s)),
             ("equities", lambda: fetch_equities(s))]
    for name, fn in steps:
        try:
            if name == "fx":
                fx = fetch_fx()
                if fx:
                    s["fx_usdcny"] = round(fx, 4)
            else:
                fn()
            print(f"OK   {name}")
        except Exception:
            print(f"FAIL {name}")
            traceback.print_exc()
    s["generated_at"] = datetime.now(PER).strftime("%Y-%m-%dT%H:%M:%S+08:00")
    with open(SNAP, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2, ensure_ascii=False)
    write_csv(s)
    print("written:", SNAP, CSV)


if __name__ == "__main__":
    sys.exit(main())
