"""每日抓取台股（上市+上櫃）行情，累積歷史，計算指標，輸出 data/screener.json
用法：python fetch_data.py            # 每日更新
      python fetch_data.py --backfill # 首次執行：用 yfinance 回補約 6 個月歷史
"""
import json, re, sys, datetime as dt
from pathlib import Path
import requests

D = Path("data"); D.mkdir(exist_ok=True)
HIST, OUT = D / "history.json", D / "screener.json"
KEEP = 130  # 保留最近 130 個交易日

TWSE_PRICE = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TWSE_PE = "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL"
TPEX_PRICE = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
TPEX_PE = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis"


def get(url):
    r = requests.get(url, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    return r.json()


def num(x):
    try:
        return float(str(x).replace(",", "").replace("+", ""))
    except (ValueError, TypeError):
        return None


def iso(d):
    """民國日期 1141126 -> 2025-11-26；其他格式則退回今天（台北時間）"""
    s = re.sub(r"\D", "", str(d or ""))
    if len(s) in (6, 7):
        return f"{int(s[:-4]) + 1911}-{s[-4:-2]}-{s[-2:]}"
    if len(s) == 8:
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return (dt.datetime.utcnow() + dt.timedelta(hours=8)).strftime("%Y-%m-%d")


def fetch_today():
    rows, val = {}, {}
    for r in get(TWSE_PRICE):
        c = r.get("Code", "")
        if re.fullmatch(r"\d{4}", c) and num(r.get("ClosingPrice")):
            rows[c] = dict(n=r["Name"], m="上市", date=iso(r.get("Date")),
                           close=num(r["ClosingPrice"]), chg=num(r.get("Change")),
                           vol=int(num(r.get("TradeVolume")) or 0))
    for r in get(TPEX_PRICE):
        c = r.get("SecuritiesCompanyCode", "")
        if re.fullmatch(r"\d{4}", c) and num(r.get("Close")):
            rows[c] = dict(n=r["CompanyName"], m="上櫃", date=iso(r.get("Date")),
                           close=num(r["Close"]), chg=num(r.get("Change")),
                           vol=int(num(r.get("TradingShares")) or 0))
    for r in get(TWSE_PE):
        val[r.get("Code")] = (num(r.get("PEratio")), num(r.get("DividendYield")), num(r.get("PBratio")))
    for r in get(TPEX_PE):
        val[r.get("SecuritiesCompanyCode")] = (num(r.get("PriceEarningRatio")),
                                                num(r.get("YieldRatio")), num(r.get("PriceBookRatio")))
    return rows, val


def backfill(hist, rows):
    import yfinance as yf
    items = [(f"{c}.{'TW' if v['m'] == '上市' else 'TWO'}", c) for c, v in rows.items()]
    for i in range(0, len(items), 100):
        batch = items[i:i + 100]
        df = yf.download([t for t, _ in batch], period="6mo", auto_adjust=False,
                         group_by="ticker", progress=False, threads=True)
        for t, c in batch:
            try:
                s = df[t][["Close", "Volume"]].dropna()
            except KeyError:
                continue
            hist[c] = {"n": rows[c]["n"], "m": rows[c]["m"],
                       "d": [[ix.strftime("%Y-%m-%d"), round(float(a), 2), int(b)]
                             for ix, (a, b) in s.iterrows()]}
        print(f"回補 {min(i + 100, len(items))}/{len(items)}")


def rsi(cl, n=14):
    if len(cl) < n + 1:
        return None
    g = l = 0
    for a, b in zip(cl[-n - 1:-1], cl[-n:]):
        g += max(b - a, 0); l += max(a - b, 0)
    return 100.0 if l == 0 else round(100 - 100 / (1 + g / l), 1)


def build(code, h, today, v):
    d = h["d"]; cl = [x[1] for x in d]; vo = [x[2] for x in d]
    ma = lambda n: round(sum(cl[-n:]) / n, 2) if len(cl) >= n else None
    vr = None
    if len(vo) >= 6 and sum(vo[-6:-1]) > 0:
        vr = round(vo[-1] / (sum(vo[-6:-1]) / 5), 2)
    hi = max(cl[-21:-1]) if len(cl) >= 21 else None
    prev = today["close"] - (today["chg"] or 0)
    pct = round((today["chg"] or 0) / prev * 100, 2) if prev else 0
    pe, dy, pb = v.get(code, (None, None, None))
    return dict(c=code, n=h["n"], m=h["m"], close=today["close"], pct=pct, vol=vo[-1],
                pe=pe, dy=dy, pb=pb, ma5=ma(5), ma20=ma(20), ma60=ma(60), vr=vr,
                rsi=rsi(cl), brk=bool(hi and cl[-1] > hi), spark=cl[-30:])


def main():
    rows, val = fetch_today()
    if not rows:
        sys.exit("今日無資料")
    hist = json.loads(HIST.read_text("utf-8")) if HIST.exists() else {}
    if "--backfill" in sys.argv or not hist:
        backfill(hist, rows)
    date = max(r["date"] for r in rows.values())
    for c, r in rows.items():
        h = hist.setdefault(c, {"n": r["n"], "m": r["m"], "d": []})
        h["n"], h["m"] = r["n"], r["m"]
        if h["d"] and h["d"][-1][0] == r["date"]:
            h["d"][-1] = [r["date"], r["close"], r["vol"]]
        elif not h["d"] or h["d"][-1][0] < r["date"]:
            h["d"].append([r["date"], r["close"], r["vol"]])
        h["d"] = h["d"][-KEEP:]
    HIST.write_text(json.dumps(hist, ensure_ascii=False, separators=(",", ":")), "utf-8")
    stocks = [build(c, hist[c], r, val) for c, r in rows.items()]
    OUT.write_text(json.dumps({"updated": date, "stocks": stocks},
                              ensure_ascii=False, separators=(",", ":")), "utf-8")
    print(f"完成：{date}，共 {len(stocks)} 檔")


if __name__ == "__main__":
    main()
