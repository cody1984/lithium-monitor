"""Lithium monitor cloud job - runs in GitHub Actions, never on your machine.

Feeds per run:
  1. GFEX futures (LC2701)      AKShare -> sina daily bar: close, volume, OI
  2. Spot (99qh via AKShare)    spot trend for 碳酸锂 - flagged PROXY for SMM BG carbonate
  3. Indicative GFEX USD price  metalcharts free API (optional, needs METALCHARTS_KEY)
  4. FX                         frankfurter.app (ECB reference, keyless)
Not automated (by design):
  - GFEX registered receipts: no free feed. Bundled prints stay until updated manually
    (X transcriptions or GFEX Chinese bulletin). Exact per-warehouse: metalcharts Ultimate.
  - SGX LICF: delayed/thin; update occasionally by hand.
Failures never abort the run: a feed that fails keeps its last values; gaps stay gaps.
"""
import csv, json, os, sys, traceback
from datetime import datetime, timezone, timedelta

SNAP, CSV = "market_snapshot.json", "market_daily.csv"
CONTRACT = os.environ.get("GFEX_CONTRACT", "LC2701")
KEY = os.environ.get("METALCHARTS_KEY", "")
PER = timezone(timedelta(hours=8))

def tofloat(x):
    try:
        v = float(str(x).replace(",", ""))
        return v if v == v else None  # NaN guard
    except (TypeError, ValueError):
        return None

def load():
    if os.path.exists(SNAP):
        with open(SNAP, encoding="utf-8") as f:
            s = json.load(f)
    else:
        s = {"schema": "lithium-monitor/1", "generated_at": "", "fx_usdcny": 6.6665,
             "feeds": {}, "series": {"smm_spot": [], "gfex_futures": [], "sgx_licf": [], "gfex_receipts": []},
             "context": []}
    for k in ("smm_spot", "gfex_futures", "sgx_licf", "gfex_receipts"):
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
               oi=tofloat(row.get("hold")) or None,
               note="sina daily via AKShare; settlement replaces close when GFEX EOD is loaded")

def fetch_spot_100ppi(s):
    import akshare as ak
    from datetime import timedelta
    smm = s["series"]["smm_spot"]
    got, d = 0, datetime.now(PER).date()
    for _ in range(12):
        if got >= 5:
            break
        if d.weekday() < 5:
            try:
                df = ak.futures_spot_price(date=d.strftime("%Y%m%d"), vars_list=["LC"])
            except Exception as e:
                print("  100ppi", d, "skipped:", type(e).__name__)
                df = None
            if df is not None and len(df):
                row = df.iloc[0]
                pick = lambda *n: next((row[c] for c in n if c in df.columns), None)
                iso = d.isoformat()
                sp = tofloat(pick("spot_price", "sp", "现货价格"))
                dom_sym = str(pick("dom_symbol", "主力合约代码") or "").lower()
                dom_px = tofloat(pick("dom_price", "主力合约价格"))
                if sp:
                    got += 1
                    if not any(r["date"] == iso and r.get("avg") for r in smm):
                        upsert(smm, iso, avg=sp, note="100ppi battery-grade spot via AKShare - proxy for SMM")
                if dom_px and dom_sym.endswith(CONTRACT.lower()[-4:]):
                    upsert(s["series"]["gfex_futures"], iso, contract=CONTRACT,
                           settle=dom_px, note="dominant-contract settlement via 100ppi/AKShare")
        d -= timedelta(days=1)
    if not got:
        raise RuntimeError("no 100ppi spot rows returned")
    s["feeds"]["smm_spot"] = {"status": "proxy", "last_date": smm[-1]["date"],
        "note": "Automated leg is 100ppi (生意社) battery-grade spot via AKShare - a proxy for SMM. Existing exact SMM rows are never overwritten."}

def fetch_metalcharts(s, fx):
    if not KEY or not fx:
        return
    import requests
    price = None
    for sym in ("LC", "LI"):
        r = requests.get(f"https://api.metalcharts.org/v1/prices?symbols={sym}",
                         headers={"Authorization": f"Bearer {KEY}"}, timeout=20)
        if r.ok:
            data = r.json().get("data") or {}
            entry = data.get(sym) or next(iter(data.values()), None)
            if entry and entry.get("price"):
                price = float(entry["price"])
                break
    if price:
        d = datetime.now(PER).strftime("%Y-%m-%d")
        have = next((r for r in s["series"]["gfex_futures"] if r["date"] == d), None)
        if not have or have.get("close") is None:
            upsert(s["series"]["gfex_futures"], d, contract=CONTRACT,
                   close=round(price * fx), note="INDICATIVE - metalcharts live quote converted at ECB FX")

def write_csv(s):
    maps = {k: {r["date"]: r for r in s["series"][k]} for k in s["series"]}
    dates = sorted({d for m in maps.values() for d in m})
    with open(CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "smm_spot_avg", "gfex_lc_settle", "gfex_lc_close",
                    "gfex_oi", "gfex_volume", "sgx_licf_usd_kg", "gfex_receipts"])
        for d in dates:
            smm, gf = maps["smm_spot"].get(d, {}), maps["gfex_futures"].get(d, {})
            sg, rec = maps["sgx_licf"].get(d, {}), maps["gfex_receipts"].get(d, {})
            w.writerow([d, smm.get("avg", ""), gf.get("settle", ""), gf.get("close", ""),
                        gf.get("oi", ""), gf.get("volume", ""), sg.get("price_usd_kg", ""),
                        rec.get("quantity", "")])

def main():
    s = load()
    fx = None
    for name, fn in [("fx", fetch_fx), ("gfex-akshare", lambda: fetch_gfex_akshare(s)),
                     ("spot-99qh", lambda: fetch_spot_100ppi(s))]:
        try:
            out = fn()
            if name == "fx":
                fx = out
                if fx:
                    s["fx_usdcny"] = round(fx, 4)
            print(f"OK   {name}")
        except Exception:
            print(f"FAIL {name}")
            traceback.print_exc()
    try:
        fetch_metalcharts(s, fx)
        print("OK   metalcharts" if KEY else "SKIP metalcharts (no key)")
    except Exception:
        print("FAIL metalcharts")
        traceback.print_exc()
    s["generated_at"] = datetime.now(PER).strftime("%Y-%m-%dT%H:%M:%S+08:00")
    with open(SNAP, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2, ensure_ascii=False)
    write_csv(s)
    print("written:", SNAP, CSV)

if __name__ == "__main__":
    sys.exit(main())
