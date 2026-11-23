"""Lithium monitor cloud job - runs in GitHub Actions.

Feeds (each isolated: a failure keeps last values, is logged in run_log, and the run still commits):
  fx            USD/CNY ECB reference series via Frankfurter (dated, 45-day backfill)
  gfex-akshare  selected-contract daily bar via AKShare (Sina): close, volume, OI, OI change
  curve         every listed LC contract (next ~13 delivery months) via AKShare (Sina), last 8 sessions
  receipts      GFEX registered warehouse receipts via AKShare futures_receipt (fallback: GFEX bulletin function)
  spot-99qh     100ppi spot + dominant-contract settlement via AKShare (label kept for log continuity)
  equities      LTR.AX and PLS.AX closes via yfinance; bars still trading are flagged intraday
Manual inputs (SGX, SC6, SMM inventory, GWh, evidence ...) live in the Google Sheet, not here.
"""
import csv, json, os, sys, traceback
from datetime import datetime, timezone, timedelta

SNAP, CSV = "market_snapshot.json", "market_daily.csv"
CONTRACT = os.environ.get("GFEX_CONTRACT", "LC2701")
PER = timezone(timedelta(hours=8))
SERIES = ("smm_spot", "gfex_futures", "sgx_licf", "gfex_receipts", "spodumene", "equities", "fx", "futures_curve")


def tofloat(x):
    try:
        v = float(str(x).replace(",", ""))
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def now_iso():
    return datetime.now(PER).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def load():
    if os.path.exists(SNAP):
        with open(SNAP, encoding="utf-8") as f:
            s = json.load(f)
    else:
        s = {"schema": "lithium-monitor/6", "generated_at": "", "fx_usdcny": 6.6665,
             "feeds": {}, "series": {}, "context": []}
    s.setdefault("series", {})
    s.setdefault("feeds", {})
    s.setdefault("run_log", {})
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


def upsert_contract(series, date, contract, **vals):
    row = next((r for r in series if r.get("date") == date and r.get("contract") == contract), None)
    if row is None:
        row = {"date": date, "contract": contract}
        series.append(row)
    for k, v in vals.items():
        if v is not None:
            row[k] = v
    series.sort(key=lambda r: (r["date"], r["contract"]))


def fetch_fx(s):
    """CNY per USD, stored with its observation date. Raises if no plausible rate."""
    import requests
    start = (datetime.now(PER).date() - timedelta(days=45)).isoformat()
    rates, src = {}, ""
    try:
        r = requests.get(f"https://api.frankfurter.app/{start}..",
                         params={"from": "USD", "to": "CNY"}, timeout=20)
        r.raise_for_status()
        for d, v in (r.json().get("rates") or {}).items():
            rates[d] = tofloat(v.get("CNY"))
        src = "ECB reference via Frankfurter v1"
    except Exception as e:
        print("  fx v1 failed:", type(e).__name__, str(e)[:100])
    if not rates:
        r = requests.get("https://api.frankfurter.dev/v2/providers/ecb/rates",
                         params={"base": "USD", "quotes": "CNY", "from": start}, timeout=20)
        r.raise_for_status()
        j = r.json()
        rows = j if isinstance(j, list) else (j.get("rates") or j.get("data") or [])
        for row in rows:
            if isinstance(row, dict) and row.get("date"):
                rates[row["date"]] = tofloat(row.get("rate"))
        src = "ECB via Frankfurter v2 (response shape unverified)"
    good = {d: v for d, v in rates.items() if v and 5.0 < v < 9.0}
    if not good:
        raise RuntimeError("no plausible USD/CNY rates returned")
    fx = s["series"]["fx"]
    for d, v in good.items():
        upsert(fx, d, usdcny=round(v, 4), source=src)
    s["fx_usdcny"], s["fx_date"] = fx[-1]["usdcny"], fx[-1]["date"]
    s["feeds"]["fx"] = {"status": "ok", "last_date": fx[-1]["date"],
        "note": "ECB reference rate, published ~16:00 CET: the 17:37 AWST job normally sees the previous ECB business day. Reference rate, not a live quote."}


def _bar_rows(df, n):
    t = df.tail(n).reset_index(drop=True)
    for i, row in t.iterrows():
        hold = tofloat(row.get("hold"))
        prev = tofloat(t.loc[i - 1, "hold"]) if i > 0 else None
        yield (str(row["date"])[:10], tofloat(row.get("close")), tofloat(row.get("settle")),
               tofloat(row.get("volume")), hold,
               (hold - prev) if (hold is not None and prev is not None) else None)


def fetch_gfex_akshare(s):
    import akshare as ak
    df = ak.futures_zh_daily_sina(symbol=CONTRACT)
    for d, close, _settle, vol, hold, chg in _bar_rows(df, 4):
        upsert(s["series"]["gfex_futures"], d, contract=CONTRACT, close=close, volume=vol,
               oi=hold, oi_change=chg,
               note="Sina daily bar via AKShare; settlement supplied separately by 100ppi")


def contract_codes(n_months=13):
    t = datetime.now(PER).date()
    out = []
    for k in range(n_months):
        m = t.month - 1 + k
        out.append(f"LC{(t.year + m // 12) % 100:02d}{m % 12 + 1:02d}")
    return out


def fetch_curve(s):
    """One daily series per listed LC contract. Unlisted codes simply fail and are skipped."""
    import akshare as ak
    cur, got = s["series"]["futures_curve"], 0
    for code in contract_codes():
        try:
            df = ak.futures_zh_daily_sina(symbol=code)
        except Exception as e:
            print("  curve", code, "skipped:", type(e).__name__)
            continue
        if df is None or not len(df):
            continue
        got += 1
        for d, close, settle, vol, hold, chg in _bar_rows(df, 8):
            upsert_contract(cur, d, code, close=close, settle=settle, volume=vol, oi=hold, oi_change=chg)
    if not got:
        raise RuntimeError("no LC contracts returned")
    cutoff = (datetime.now(PER).date() - timedelta(days=400)).isoformat()
    s["series"]["futures_curve"] = [r for r in cur if r["date"] >= cutoff]
    s["feeds"]["curve"] = {"status": "ok", "last_date": cur[-1]["date"],
        "note": f"{got} contracts. Uses the Sina settle column when present, else close. Expiry dates are not modelled."}


def fetch_receipts(s):
    import akshare as ak
    rc = s["series"]["gfex_receipts"]
    t = datetime.now(PER).date()
    start, end = (t - timedelta(days=20)).strftime("%Y%m%d"), t.strftime("%Y%m%d")
    n = 0
    try:
        df = ak.futures_receipt(start_date=start, end_date=end, symbols="LC")
        if df is not None and len(df):
            for _, row in df.iterrows():
                if str(row.get("var", "")).upper() != "LC":
                    continue
                q = tofloat(row.get("receipt"))
                if q is None or q < 0 or q > 1e6:
                    continue
                ds = str(row.get("date"))
                iso = f"{ds[:4]}-{ds[4:6]}-{ds[6:8]}" if len(ds) == 8 and ds.isdigit() else ds[:10]
                upsert(rc, iso, quantity=q, change=tofloat(row.get("receipt_chg")), derived=False,
                       source="AKShare futures_receipt (exchange daily report)")
                n += 1
    except Exception as e:
        print("  futures_receipt failed:", type(e).__name__, str(e)[:120])
    if not n:
        res = ak.futures_gfex_warehouse_receipt(date=end)
        frames = res.items() if isinstance(res, dict) else [("", res)]
        for key, df in frames:
            if "碳酸锂" not in str(key) and "LC" not in str(key).upper():
                continue
            print("  gfex bulletin columns:", list(df.columns))
            col = next((c for c in df.columns if "今日" in str(c) and "仓单" in str(c)), None)
            if col is None:
                continue
            tot = sum(v for v in (tofloat(x) for x in df[col]) if v is not None)
            upsert(rc, t.isoformat(), quantity=tot, derived=False,
                   source="AKShare GFEX bulletin function (shape unverified)")
            n += 1
    if not n:
        raise RuntimeError("no receipt rows returned")
    s["feeds"]["receipts"] = {"status": "ok", "last_date": rc[-1]["date"],
        "note": "Automated from exchange daily report; units assumed tonnes, confirm against a GFEX bulletin once."}


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
                        upsert(smm, iso, avg=sp, verified=False, source="100ppi",
                               note="100ppi battery-grade spot via AKShare - proxy for SMM")
                if dom_px and dom_sym.endswith(CONTRACT.lower()[-4:]):
                    upsert(s["series"]["gfex_futures"], iso, contract=CONTRACT, settle=dom_px,
                           note="dominant-contract settlement via 100ppi/AKShare")
            elif df is not None:
                print("  100ppi", d, "empty (non-trading day or no data)")
        d -= timedelta(days=1)
    if not got:
        raise RuntimeError("no 100ppi spot rows returned")
    s["feeds"]["smm_spot"] = {"status": "proxy", "last_date": smm[-1]["date"],
        "note": "Automated leg is 100ppi battery-grade spot - a proxy for SMM. Existing SMM rows are never overwritten."}


def fetch_equities(s):
    import yfinance as yf
    from zoneinfo import ZoneInfo
    syd = datetime.now(ZoneInfo("Australia/Sydney"))
    still_trading = lambda d: d == syd.date().isoformat() and (syd.hour * 60 + syd.minute) < 16 * 60 + 30
    eq = s["series"]["equities"]
    h = yf.download(["LTR.AX", "PLS.AX"], period="10d", interval="1d",
                    progress=False, auto_adjust=False)["Close"]
    n = 0
    for ts, row in h.iterrows():
        ltr, pls = tofloat(row.get("LTR.AX")), tofloat(row.get("PLS.AX"))
        if ltr is None and pls is None:
            continue
        d = ts.strftime("%Y-%m-%d")
        upsert(eq, d, ltr=ltr, pls=pls, intraday=bool(still_trading(d)))
        n += 1
    if not n:
        raise RuntimeError("no equity rows returned")
    s["feeds"]["equities"] = {"status": "ok", "last_date": eq[-1]["date"],
        "note": "LTR.AX / PLS.AX via yfinance (unofficial). Bars dated today before 16:30 Sydney are flagged intraday and excluded from comparisons."}


def write_csv(s):
    maps = {k: {r["date"]: r for r in s["series"].get(k, [])} for k in SERIES if k != "futures_curve"}
    dates = sorted({d for m in maps.values() for d in m})
    with open(CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "smm_spot_avg", "gfex_lc_settle", "gfex_lc_close", "gfex_oi",
                    "gfex_oi_change", "gfex_volume", "sgx_licf_usd_kg", "gfex_receipts",
                    "ltr_close", "pls_close", "spodumene_sc6_usd_t", "usdcny"])
        for d in dates:
            smm, gf = maps["smm_spot"].get(d, {}), maps["gfex_futures"].get(d, {})
            sg, rc = maps["sgx_licf"].get(d, {}), maps["gfex_receipts"].get(d, {})
            eq, sp, fx = maps["equities"].get(d, {}), maps["spodumene"].get(d, {}), maps["fx"].get(d, {})
            w.writerow([d, smm.get("avg", ""), gf.get("settle", ""), gf.get("close", ""),
                        gf.get("oi", ""), gf.get("oi_change", ""), gf.get("volume", ""),
                        sg.get("price_usd_kg", ""), rc.get("quantity", ""), eq.get("ltr", ""),
                        eq.get("pls", ""), sp.get("price_usd_t", ""), fx.get("usdcny", "")])
    cur = s["series"].get("futures_curve", [])
    with open("futures_curve.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "contract", "close", "settle", "volume", "oi", "oi_change"])
        for r in cur:
            w.writerow([r["date"], r["contract"], r.get("close", ""), r.get("settle", ""),
                        r.get("volume", ""), r.get("oi", ""), r.get("oi_change", "")])


def main():
    s = load()
    steps = [("fx", lambda: fetch_fx(s)), ("gfex-akshare", lambda: fetch_gfex_akshare(s)),
             ("curve", lambda: fetch_curve(s)), ("receipts", lambda: fetch_receipts(s)),
             ("spot-99qh", lambda: fetch_spot_100ppi(s)), ("equities", lambda: fetch_equities(s))]
    for name, fn in steps:
        try:
            fn()
            s["run_log"][name] = {"status": "ok", "time": now_iso(), "error": ""}
            print(f"OK   {name}")
        except Exception as e:
            prev = s["run_log"].get(name, {})
            s["run_log"][name] = {"status": "fail", "time": now_iso(),
                                  "error": f"{type(e).__name__}: {str(e)[:160]}",
                                  "last_ok": prev.get("time") if prev.get("status") == "ok" else prev.get("last_ok", "")}
            print(f"FAIL {name}")
            traceback.print_exc()
    s["schema"] = "lithium-monitor/6"
    s["generated_at"] = now_iso()
    with open(SNAP, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2, ensure_ascii=False)
    write_csv(s)
    print("written:", SNAP, CSV, "futures_curve.csv")


if __name__ == "__main__":
    sys.exit(main())
