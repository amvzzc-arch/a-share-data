#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
打板晋级率/封板结构统计（零依赖，东方财富公开接口）
用途：用真实历史数据量化 1进2/2进3/3进4... 晋级率、炸板率、封板结构，
     替代"打板有确定性"的主观判断。
阶段1：拉取最近 N 个交易日的涨停池/炸板池并缓存
阶段2：统计各级晋级率与封板/炸板结构
"""
import urllib.request, json, time, os, sys

UT = "7eea3edcaed734bea9cbfc24409ed989"
H = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "board_stats_cache.json")
N_DAYS = 80


def http_get(url, timeout=15, retries=3):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=H)
            return urllib.request.urlopen(req, timeout=timeout).read()
        except Exception as e:
            last = e
            time.sleep(1.2 * (i + 1))
    raise last


def trade_dates(n):
    # 腾讯日K（规避东财 push2his 限流），返回最近100个交易日
    u = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh000001,day,,,100,qfq"
    d = json.loads(http_get(u))
    kl = d["data"]["sh000001"]["day"]
    dates = [x[0].replace("-", "") for x in kl]
    return dates[-n:]


def pool(ep, date, ps="10000", sort="fbt%3Aasc"):
    ts = int(time.time() * 1000)
    u = (f"https://push2ex.eastmoney.com/{ep}?ut={UT}&dpt=wz.ztzt&Pageindex=0"
         f"&pagesize={ps}&sort={sort}&date={date}&_={ts}")
    d = json.loads(http_get(u))
    return ((d.get("data") or {}).get("pool")) or []


def slim(rec):
    """精简字段，p 由千分价转元"""
    return {
        "c": rec["c"], "n": rec["n"], "p": rec["p"] / 1000.0,
        "zdp": rec.get("zdp"), "lbc": rec.get("lbc"),
        "zbc": rec.get("zbc", 0), "fund": rec.get("fund"),
        "fbt": rec.get("fbt"), "lbt": rec.get("lbt"),
        "hs": rec.get("hs"), "ltsz": rec.get("ltsz"),
        "amount": rec.get("amount"), "hybk": rec.get("hybk"),
    }


def stage1():
    dates = trade_dates(N_DAYS)
    data = {}
    if os.path.exists(CACHE):
        data = json.load(open(CACHE))
    for i, dt in enumerate(dates):
        if dt in data and "zt" in data[dt]:
            continue
        try:
            zt = [slim(r) for r in pool("getTopicZTPool", dt)]
            time.sleep(0.35)
            zb = [slim(r) for r in pool("getTopicZBPool", dt, sort="zbc%3Adesc")]
            data[dt] = {"zt": zt, "zb": zb}
            json.dump(data, open(CACHE, "w"), ensure_ascii=False)
            print(f"[{i+1}/{len(dates)}] {dt} 涨停{len(zt)} 炸板{len(zb)}")
            time.sleep(0.35)
        except Exception as e:
            print("失败", dt, e)
            break
    json.dump(data, open(CACHE, "w"), ensure_ascii=False)
    print("缓存完成，交易日:", len(data))


def stage2():
    data = json.load(open(CACHE))
    alldates = sorted(data.keys())
    valid = [d for d in alldates if data[d]["zt"] or data[d]["zb"]]
    ztmap = {d: {r["c"]: r for r in data[d]["zt"]} for d in alldates}

    print("=" * 60)
    print(f"有效区间：{valid[0]} ~ {valid[-1]}（{len(valid)}个交易日）")
    print("=" * 60)

    # ---- 1. 各级晋级率 ----
    attempts, success = {}, {}
    for i in range(len(alldates) - 1):
        t, t1 = alldates[i], alldates[i + 1]
        for code, r in ztmap[t].items():
            k = r["lbc"]
            attempts[k] = attempts.get(k, 0) + 1
            if code in ztmap[t1] and ztmap[t1][code]["lbc"] == k + 1:
                success[k] = success.get(k, 0) + 1

    print("\n【各级晋级率：当天第k板，次日成功封第k+1板的比例】")
    name = {1: "1进2", 2: "2进3", 3: "3进4", 4: "4进5", 5: "5进6", 6: "6进7"}
    for k in sorted(attempts):
        a, s = attempts[k], success.get(k, 0)
        print(f"  {name.get(k, str(k)+'进'+str(k+1)):<5}: {s:>3}/{a:<3} = {s/a*100:5.1f}%  "
              f"(失败 {a-s})")

    # ---- 2. 每日封板/炸板结构 ----
    print("\n【每日封板结构】")
    tot_zt = tot_zb = tot_touch = 0
    for d in valid:
        z, b = len(data[d]["zt"]), len(data[d]["zb"])
        tot_zt += z; tot_zb += b; tot_touch += z + b
        print(f"  {d}: 涨停{z:>3} 炸板{b:>3} 封板率{z/(z+b)*100:5.1f}%")
    print(f"  合计：涨停{tot_zt} 炸板{tot_zb} 触板{tot_touch} "
          f"整体封板率{tot_zt/tot_touch*100:.1f}% 炸板率{tot_zb/tot_touch*100:.1f}%")

    # ---- 3. 涨停里 0炸占比 / 首板vs连板 ----
    allzt = [r for d in valid for r in data[d]["zt"]]
    zero = [r for r in allzt if r["zbc"] == 0]
    first = [r for r in allzt if r["lbc"] == 1]
    print(f"\n【涨停板质量】")
    print(f"  涨停样本{len(allzt)}，其中0炸板{len(zero)} = {len(zero)/len(allzt)*100:.1f}%")
    print(f"  首板{len(first)}（{len(first)/len(allzt)*100:.1f}%），"
          f"连板{len(allzt)-len(first)}（{(len(allzt)-len(first))/len(allzt)*100:.1f}%）")


KL_CACHE = os.path.join(HERE, "kline_cache.json")


def tx_kline(code, n=60):
    pre = "sh" if code[0] == "6" else "sz"
    u = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
         f"param={pre}{code},day,,,{n},")
    d = json.loads(http_get(u))
    node = d["data"][pre + code]
    kl = node.get("day")
    # {date(YYYY-MM-DD): [o,c,h,l]}
    return {x[0]: [float(x[1]), float(x[2]), float(x[3]), float(x[4])] for x in kl}


def sina_kline(code, n=60):
    """新浪日K（不复权），沪深通用，作为腾讯限流时的兜底"""
    pre = "sh" if code[0] == "6" else "sz"
    u = (f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
         f"CN_MarketData.getKLineData?symbol={pre}{code}&scale=240&ma=no&datalen={n}")
    raw = http_get(u)
    arr = json.loads(raw.decode("utf-8"))
    # 新浪字段 open/high/low/close -> 统一 [o,c,h,l]
    return {x["day"]: [float(x["open"]), float(x["close"]),
                       float(x["high"]), float(x["low"])] for x in arr}


def stage3_fetch():
    data = json.load(open(CACHE))
    valid = sorted(d for d in data if data[d]["zt"] or data[d]["zb"])
    codes = sorted({r["c"] for d in valid for r in data[d]["zt"]})
    kd = json.load(open(KL_CACHE)) if os.path.exists(KL_CACHE) else {}
    for i, code in enumerate(codes):
        if code in kd:
            continue
        rec = None
        # 先腾讯（深市），失败用新浪兜底
        try:
            rec = tx_kline(code)
        except Exception:
            try:
                rec = sina_kline(code)
            except Exception as e:
                print("  fail", code, e)
                time.sleep(0.8)
                continue
        kd[code] = rec
        json.dump(kd, open(KL_CACHE, "w"), ensure_ascii=False)
        if (i + 1) % 25 == 0:
            print(f"  K线 {i+1}/{len(codes)}")
        time.sleep(0.25)
    json.dump(kd, open(KL_CACHE, "w"), ensure_ascii=False)
    print("K线缓存完成:", len(kd), "/", len(codes))


def _median(v):
    v = sorted(v); n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def _line(sub, key, label):
    n = len(sub)
    r = [x[key] for x in sub]
    win = [x for x in r if x > 0]
    loss = [x for x in r if x <= 0]
    print(f"  {label:<14} n={n:<4} 胜率{len(win)/n*100:5.1f}% | "
          f"均收益{sum(r)/n:+6.2f}% 中位{_median(r):+6.2f}% | "
          f"平均赚{sum(win)/len(win) if win else 0:+5.2f}% "
          f"平均亏{sum(loss)/len(loss) if loss else 0:+5.2f}%")


def stage3_analyze():
    data = json.load(open(CACHE)); kd = json.load(open(KL_CACHE))
    alldates = sorted(data.keys())
    rows = []
    for i in range(len(alldates) - 1):
        t, t1 = alldates[i], alldates[i + 1]
        t1f = t1[:4] + "-" + t1[4:6] + "-" + t1[6:]
        adv = {x["c"] for x in data[t1]["zt"]}
        for r in data[t]["zt"]:
            code, cost = r["c"], r["p"]
            k = kd.get(code)
            if not k or t1f not in k:
                continue
            o, c, h, l = k[t1f]
            stop = cost * 0.97  # 硬止损-3%
            # 综合策略：T+1 跌破止损按止损卖，否则开盘兑现
            rs = (-3.0) if l <= stop else (o - cost) / cost * 100
            rows.append({"lbc": r["lbc"], "adv": code in adv,
                         "ro": (o - cost) / cost * 100, "rc": (c - cost) / cost * 100,
                         "rh": (h - cost) / cost * 100, "rl": (l - cost) / cost * 100,
                         "rs": rs})
    print("=" * 70)
    print(f"打板盈亏模拟（T日涨停价买入，T+1卖出）有效样本 {len(rows)} 笔")
    print("=" * 70)

    print("\n【口径对比 · 全体】ro=开盘卖 rc=收盘卖 rh=最高(理想) rs=带-3%止损")
    for key, lab in [("ro", "次日开盘卖"), ("rc", "次日收盘卖"),
                     ("rh", "次日最高(理想)"), ("rs", "止损+开盘(实战)")]:
        _line(rows, key, lab)

    print("\n【分买入层级 · 实战口径(rs)】")
    for lv in (1, 2, 3):
        sub = [x for x in rows if x["lbc"] == lv]
        if sub:
            _line(sub, "rs", {1: "打首板(1进2)", 2: "打2板(2进3)", 3: "打3板(3进4)"}[lv])

    print("\n【晋级 vs 未晋级 · 实战口径(rs)】")
    _line([x for x in rows if x["adv"]], "rs", "次日成功封板")
    _line([x for x in rows if not x["adv"]], "rs", "次日未封板")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "2":
        stage2()
    elif len(sys.argv) > 1 and sys.argv[1] == "3":
        stage3_fetch()
    elif len(sys.argv) > 1 and sys.argv[1] == "4":
        stage3_analyze()
    else:
        stage1()
