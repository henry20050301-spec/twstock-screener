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


def get(url, tries=3, timeout=30):
    import time
    for i in range(tries):
        try:
            r = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            print(f"請求失敗（第 {i + 1}/{tries} 次）：{url[:60]} {type(e).__name__}")
            if i == tries - 1:
                raise
            time.sleep(3)


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


INST = D / "inst.json"
OPEN = "https://openapi.twse.com.tw/v1/opendata"
TPEX_OPEN = "https://www.tpex.org.tw/openapi/v1"


def fld(r, *subs):
    """依欄位名稱關鍵字取值（容忍欄位名稱些微差異）"""
    for k, v in r.items():
        if all(x in k for x in subs):
            return v
    return None


def fetch_rev():
    """月營收：年增率、月增率。回傳 {code: dict(rv, rm, rym)}"""
    out = {}
    for url in (f"{OPEN}/t187ap05_L", f"{TPEX_OPEN}/mopsfin_t187ap05_O"):
        try:
            for r in get(url):
                c = str(fld(r, "公司代號") or "").strip()
                if c:
                    out[c] = dict(rv=num(fld(r, "去年同月增減")), rm=num(fld(r, "上月比較增減")),
                                  rym=str(fld(r, "資料年月") or ""))
        except Exception as e:
            print("警告：月營收抓取失敗：", url[-20:], e)
    print(f"月營收：{len(out)} 檔")
    return out


def fetch_eps():
    """最新一期財報的基本每股盈餘。回傳 {code: dict(eps, eq)}"""
    out = {}
    urls = [f"{OPEN}/t187ap06_L_{k}" for k in ("ci", "basi", "bd", "fh", "ins", "mim")]
    urls += [f"{TPEX_OPEN}/mopsfin_t187ap06_O_{k}" for k in ("ci", "basi", "bd", "fh", "ins", "mim")]
    for url in urls:
        try:
            for r in get(url, tries=2):
                c = str(fld(r, "公司代號") or "").strip()
                e = num(fld(r, "基本每股盈餘"))
                if c and e is not None:
                    out[c] = dict(eps=e, eq=f"{fld(r, '年度')}Q{fld(r, '季別')}")
        except Exception as e:
            print("略過 EPS 來源：", url[-24:], type(e).__name__)
    print(f"EPS：{len(out)} 檔")
    return out


def fetch_inst(ds):
    """證交所三大法人買賣超（上市），回傳 {code: (外資股數, 投信股數)}；無資料回傳 None"""
    j = get(f"https://www.twse.com.tw/rwd/zh/fund/T86?date={ds}&selectType=ALLBUT0999&response=json")
    if j.get("stat") != "OK" or not j.get("data"):
        return None
    f = j["fields"]
    ic = f.index("證券代號")
    fi = next(i for i, k in enumerate(f) if k.startswith("外") and "買賣超" in k)
    ti = next(i for i, k in enumerate(f) if k.startswith("投信") and "買賣超" in k)
    return {r[ic].strip(): (int(num(r[fi]) or 0), int(num(r[ti]) or 0)) for r in j["data"]}


def update_inst(date):
    """累積最近 20 個交易日的法人買賣超；首次執行回補約 3 週"""
    import time
    inst = json.loads(INST.read_text("utf-8")) if INST.exists() else {}
    d = dt.date.fromisoformat(date)
    days = [date]
    if not inst:
        days = [(d - dt.timedelta(days=k)).isoformat() for k in range(21, -1, -1)]
        days = [x for x in days if dt.date.fromisoformat(x).weekday() < 5]
    for day in days:
        try:
            m = fetch_inst(day.replace("-", ""))
        except Exception as e:
            print("法人資料失敗：", day, type(e).__name__)
            continue
        if m:
            for c, (a, b) in m.items():
                h = inst.setdefault(c, [])
                if not h or h[-1][0] < day:
                    h.append([day, a, b])
                    del h[:-20]
        if len(days) > 1:
            time.sleep(3)  # 回補時放慢，避免被證交所限流
    INST.write_text(json.dumps(inst, ensure_ascii=False, separators=(",", ":")), "utf-8")
    return inst


def streak(h, i):
    n = 0
    for x in reversed(h):
        if x[i] > 0:
            n += 1
        else:
            break
    return n


def twse_web():
    """證交所網站版盤後 API：可指定日期，收盤後即有資料。從今天往回找最近有資料的一天。"""
    base = "https://www.twse.com.tw/rwd/zh/afterTrading"
    tag = lambda s: re.sub(r"<[^>]*>", "", str(s))
    day = dt.datetime.utcnow() + dt.timedelta(hours=8)
    for _ in range(10):
        ds = day.strftime("%Y%m%d")
        day -= dt.timedelta(days=1)
        j = get(f"{base}/MI_INDEX?date={ds}&type=ALLBUT0999&response=json")
        if j.get("stat") != "OK":
            continue
        t = next((t for t in j.get("tables", [])
                  if "證券代號" in t.get("fields", []) and "收盤價" in t["fields"]), None)
        if not t or not t.get("data"):
            continue
        f = t["fields"]
        ic, inm, iv, ip, id_ = (f.index(k) for k in ("證券代號", "證券名稱", "成交股數", "收盤價", "漲跌價差"))
        isg = next(i for i, k in enumerate(f) if k.startswith("漲跌("))
        rows = {}
        for r in t["data"]:
            c, close = r[ic], num(r[ip])
            if re.fullmatch(r"\d{4}", c) and close:
                sign = -1 if "-" in tag(r[isg]) else 1
                rows[c] = dict(n=r[inm].strip(), m="上市", date=iso(ds), close=close,
                               chg=sign * abs(num(r[id_]) or 0), vol=int(num(r[iv]) or 0))
        val = {}
        try:
            p = get(f"{base}/BWIBBU_d?date={ds}&selectType=ALL&response=json")
            if p.get("stat") == "OK":
                pf = p["fields"]
                a, b, y, d = (pf.index(k) for k in ("證券代號", "本益比", "殖利率(%)", "股價淨值比"))
                for r in p["data"]:
                    val[r[a]] = (num(r[b]), num(r[y]), num(r[d]))
        except Exception as e:
            print("本益比資料抓取失敗：", e)
        print(f"證交所網站版：{ds}，{len(rows)} 檔")
        return rows, val
    raise RuntimeError("近 10 天找不到證交所資料")


def fetch_today():
    rows, val = {}, {}
    try:
        w_rows, w_val = twse_web()
        rows.update(w_rows); val.update(w_val)
    except Exception as e:
        print("證交所網站版失敗，改用 openapi：", e)
        for r in get(TWSE_PRICE):
            c = r.get("Code", "")
            if re.fullmatch(r"\d{4}", c) and num(r.get("ClosingPrice")):
                rows[c] = dict(n=r["Name"], m="上市", date=iso(r.get("Date")),
                               close=num(r["ClosingPrice"]), chg=num(r.get("Change")),
                               vol=int(num(r.get("TradingVolume") or r.get("TradeVolume")) or 0))
    try:
        for r in get(TPEX_PRICE):
            c = r.get("SecuritiesCompanyCode", "")
            if re.fullmatch(r"\d{4}", c) and num(r.get("Close")):
                rows[c] = dict(n=r["CompanyName"], m="上櫃", date=iso(r.get("Date")),
                               close=num(r["Close"]), chg=num(r.get("Change")),
                               vol=int(num(r.get("TradingShares")) or 0))
    except Exception as e:
        print("警告：櫃買中心行情抓取失敗，上櫃沿用上次資料：", e)
    try:
        for r in get(TWSE_PE):
            val.setdefault(r.get("Code"), (num(r.get("PEratio")), num(r.get("DividendYield")), num(r.get("PBratio"))))
    except Exception as e:
        print("警告：證交所本益比(openapi)抓取失敗：", e)
    try:
        for r in get(TPEX_PE):
            val[r.get("SecuritiesCompanyCode")] = (num(r.get("PriceEarningRatio")),
                                                    num(r.get("YieldRatio")), num(r.get("PriceBookRatio")))
    except Exception as e:
        print("警告：櫃買中心本益比抓取失敗：", e)
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


def ema(vals, n):
    k, e, out = 2 / (n + 1), None, []
    for v in vals:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def ind(cl):
    """MACD(12,26,9)、KD(9，以收盤價近似)、布林通道(20,2)"""
    o = {}
    if len(cl) >= 35:
        dif = [a - b for a, b in zip(ema(cl, 12), ema(cl, 26))]
        sig = ema(dif, 9)
        o.update(dif=round(dif[-1], 2), osc=round(dif[-1] - sig[-1], 2),
                 mx=bool(dif[-1] > sig[-1] and dif[-2] <= sig[-2]), mz=bool(dif[-1] > 0))
    if len(cl) >= 10:
        k = d = 50.0
        pk = pd_ = 50.0
        for i in range(8, len(cl)):
            w = cl[i - 8:i + 1]
            lo, hi = min(w), max(w)
            rsv = 50 if hi == lo else (cl[i] - lo) / (hi - lo) * 100
            pk, pd_ = k, d
            k = k * 2 / 3 + rsv / 3
            d = d * 2 / 3 + k / 3
        o.update(k=round(k, 1), d=round(d, 1), kx=bool(k > d and pk <= pd_))
    if len(cl) >= 20:
        w = cl[-20:]
        m = sum(w) / 20
        sd = (sum((x - m) ** 2 for x in w) / 20) ** 0.5
        if sd > 0:
            o.update(bp=round((cl[-1] - (m - 2 * sd)) / (4 * sd), 2), bw=round(4 * sd / m * 100, 1))
    return o


def rsi(cl, n=14):
    if len(cl) < n + 1:
        return None
    g = l = 0
    for a, b in zip(cl[-n - 1:-1], cl[-n:]):
        g += max(b - a, 0); l += max(a - b, 0)
    return 100.0 if l == 0 else round(100 - 100 / (1 + g / l), 1)


def build(code, h, today, v, ex=None):
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
                rsi=rsi(cl), brk=bool(hi and cl[-1] > hi), spark=cl[-30:], **ind(cl), **(ex or {}))


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
    def safe(fn, *a):
        try:
            return fn(*a)
        except Exception as e:
            print("警告：", fn.__name__, "失敗：", e)
            return {}
    rev, eps, inst = safe(fetch_rev), safe(fetch_eps), safe(update_inst, date)

    def extra(c):
        ex = {}
        ex.update(rev.get(c, {}))
        ex.update(eps.get(c, {}))
        h = inst.get(c)
        if h:
            ex.update(fb=round(h[-1][1] / 1000), tb=round(h[-1][2] / 1000),
                      f5=round(sum(x[1] for x in h[-5:]) / 1000),
                      fs=streak(h, 1), ts=streak(h, 2))
        return ex

    cutoff = (dt.date.fromisoformat(date) - dt.timedelta(days=5)).isoformat()
    stocks = []
    for c, h in hist.items():
        if not h["d"] or h["d"][-1][0] < cutoff:
            continue  # 已下市或久未更新
        if c in rows:
            t = rows[c]
        else:  # 今天沒抓到（例如櫃買中心連線失敗）：沿用最後一筆
            last = h["d"][-1]; prev = h["d"][-2][1] if len(h["d"]) > 1 else last[1]
            t = dict(close=last[1], chg=round(last[1] - prev, 2))
        stocks.append(build(c, h, t, val, extra(c)))
    OUT.write_text(json.dumps({"updated": date, "stocks": stocks},
                              ensure_ascii=False, separators=(",", ":")), "utf-8")
    print(f"完成：{date}，共 {len(stocks)} 檔")


if __name__ == "__main__":
    main()
