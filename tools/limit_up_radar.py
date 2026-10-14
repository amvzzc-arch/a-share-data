#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A股涨停雷达 · 三轨短线版（零依赖，只用 Python 标准库）
盘中自动盯盘，触发以下信号秒推手机：
  1) 板块涨停潮（某行业新达≥3只涨停，列出龙头/梯队）——专治"漏主线"
  2) 新连板/市场最高标（龙头高度）
  3) 主板新首板（明日一进二候选，自动聚合）
  4) 龙头/主线炸板（板块走弱信号）
  5) 市场情绪冰点 / 炸板率飙升
  6) 自选中军/持仓/一进二候选的涨幅跨档、跌破开盘价
运行：python3 limit_up_radar.py
自测：python3 limit_up_radar.py --test   （非交易时间拉今日快照验证）
"""
import urllib.request, urllib.parse, json, time, datetime, sys
from collections import Counter

# ==================== 配置区：你只需要改这里 ====================
# ① iPhone：App Store 装 "Bark"，打开后把 https://api.day.app/XXXX 里的 XXXX 填下面
BARK_KEY = ""
# ② 安卓/想用微信：去 sct.ftqq.com 微信登录拿 SCTxxxx 填下面（和Bark二选一，两个都留空=只在电脑屏幕显示）
SERVERCHAN_KEY = ""
# ③ 板块涨停潮阈值（几只涨停就报警，建议3）
BOARD_LIMIT = 3
# ④ 轮询间隔（秒）
POLL_SEC = 25
# ⑤ 自选股（每天按8:50作战计划填"中军"，持仓填"持仓"，竞价候选填"一进二"）
WATCH = {
    "中军": ["002436", "002636"],   # 例：兴森科技、金安国纪——每天按盘前计划改
    "持仓": [],
    "一进二": [],
}
# ⑥ 硬筛：只要主板、股价上限
PRICE_MAX = 100.0
# 中军涨幅提醒档位（%）
WATCH_PCT = [2.0, 5.0, 7.0, 9.8]
# ⑦ 黑名单（永不提示：违纪/退市风险/停牌核查，可自行追加）
BLACKLIST = {"600792", "002285", "600825", "002585", "000021", "000733", "002185", "002402", "603162"}
# ============================================================

EM_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
UT = "7eea3edcaed734bea9cbfc24409ed989"


def log(*a):
    print("[%s] %s" % (datetime.datetime.now().strftime("%H:%M:%S"),
          " ".join(str(x) for x in a)), flush=True)


def http_get(url, timeout=12, gbk=False, retries=2):
    last = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=EM_HEADERS)
            raw = urllib.request.urlopen(req, timeout=timeout).read()
            return raw.decode("gbk", "ignore") if gbk else raw.decode("utf-8", "ignore")
        except Exception as e:
            last = e
            time.sleep(1.2)
    raise last


def push(title, body):
    """手机推送；没配 key 就只在电脑屏幕打印。"""
    log("📱", title, "|", body.replace("\n", " "))
    if BARK_KEY:
        try:
            u = ("https://api.day.app/%s/%s/%s?group=A%E8%82%A1%E9%9B%B7%E8%BE%BE&sound=alarm"
                 % (BARK_KEY, urllib.parse.quote(title, safe=''),
                    urllib.parse.quote(body, safe='')))
            http_get(u, timeout=8)
        except Exception as e:
            log("Bark失败:", e)
    if SERVERCHAN_KEY:
        try:
            data = urllib.parse.urlencode({"title": title, "desp": body}).encode()
            req = urllib.request.Request("https://sctapi.ftqq.com/%s.send" % SERVERCHAN_KEY,
                                         data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
            urllib.request.urlopen(req, timeout=8).read()
        except Exception as e:
            log("Server酱失败:", e)


def is_main(c):
    return c[:2] == "60" or c[:3] in ("000", "001", "002", "003")


def is_st(n):
    u = n.upper()
    return ("ST" in u) or ("退" in n)


def em_pool(kind, date):
    # 东财坑：跌停池 pagesize=10000 会返回空 pool，必须用小分页
    cfg = {
        "zt": ("getTopicZTPool", "10000", "fbt%3Aasc"),
        "zb": ("getTopicZBPool", "10000", "zbc%3Adesc"),
        "dt": ("getTopicDTPool", "2000", "fund%3Aasc"),
    }
    ep, ps, sort = cfg[kind]
    ts = int(time.time() * 1000)
    url = ("https://push2ex.eastmoney.com/%s?ut=%s&dpt=wz.ztzt&Pageindex=0&pagesize=%s"
           "&sort=%s&date=%s&_=%s" % (ep, UT, ps, sort, date, ts))
    d = json.loads(http_get(url))
    return ((d.get("data") or {}).get("pool")) or []


def tencent_quotes(codes):
    if not codes:
        return {}
    q = [("sh" if c[0] == "6" else "sz") + c for c in codes]
    out = {}
    try:
        txt = http_get("https://qt.gtimg.cn/q=" + ",".join(q), gbk=True)
    except Exception as e:
        log("腾讯行情失败:", e)
        return {}
    for seg in txt.split(";"):
        if "~" not in seg:
            continue
        f = seg.split("~")
        try:
            price, prev, op = float(f[3]), float(f[4]), float(f[5])
            pct = (price - prev) / prev * 100 if prev else 0.0
            amount = float(f[37]) if len(f) > 37 and f[37] else 0.0  # 万元
            pe = None
            if len(f) > 39 and f[39]:
                try:
                    pe = float(f[39])
                except Exception:
                    pe = None
            out[f[2]] = {"name": f[1], "price": price, "prev": prev, "open": op,
                         "high": float(f[33]), "low": float(f[34]),
                         "pct": pct, "amount": amount, "pe": pe}
        except Exception:
            continue
    return out


def today_str():
    return datetime.datetime.now().strftime("%Y%m%d")


# ==================== 板前动作（凯心法）· 首板前5~8日 洗/试/控 ====================
PRE_CACHE = {}


def last_trade_date():
    """最近一个已收盘交易日 YYYYMMDD（取上证指数日K最后一根；收盘后=今天，盘前=昨天）。"""
    try:
        end = datetime.datetime.now().strftime("%Y-%m-%d")
        start = (datetime.datetime.now() - datetime.timedelta(days=15)).strftime("%Y-%m-%d")
        u = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh000001,day,%s,%s,10,qfq"
             % (start, end))
        d = json.loads(http_get(u))
        node = d["data"]["sh000001"]
        ks = node.get("day") or node.get("qfqday") or []
        return ks[-1][0].replace("-", "")
    except Exception:
        return today_str()


def tencent_daily(code, n=30):
    """腾讯前复权日K（零依赖）：返回 [{date,o,c,h,l,v}...] 时间正序，v单位手。"""
    q = ("sh" if code[0] == "6" else "sz") + code
    end = datetime.datetime.now().strftime("%Y-%m-%d")
    start = (datetime.datetime.now() - datetime.timedelta(days=120)).strftime("%Y-%m-%d")
    u = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,%s,%s,%d,qfq"
         % (q, start, end, n + 12))
    try:
        d = json.loads(http_get(u))
        node = d["data"][q]
        ks = node.get("qfqday") or node.get("day") or []
        out = []
        for k in ks:
            try:
                out.append({"date": k[0], "o": float(k[1]), "c": float(k[2]),
                            "h": float(k[3]), "l": float(k[4]), "v": float(k[5])})
            except Exception:
                continue
        return out
    except Exception as e:
        log("日K失败", code, e)
        return []


def board_pre_action(code, limit_date=None):
    """凯心「板前动作」：首板启动前 5~8 个交易日有没有完整的 洗/试/控。
    返回 dict：pre 总分(0-100)、ctrl/test/wash 小分、days 平台天数、tags、veto、grade。"""
    if code in PRE_CACHE:
        return PRE_CACHE[code]
    res = {"code": code, "pre": None, "ctrl": None, "test": None, "wash": None,
           "days": 0, "box": None, "tags": [], "veto": "", "grade": "", "note": ""}
    ks = tencent_daily(code, 30)
    if limit_date:  # 剔除首板当天及之后（收盘后跑时当日K已生成，必须去掉）
        ld = limit_date[:4] + "-" + limit_date[4:6] + "-" + limit_date[6:8]
        ks = [k for k in ks if k["date"] < ld]
    elif len(ks) >= 2:  # 诊断首板票：自动剔除最后一根涨停K，只看首板"之前"
        if (ks[-1]["c"] / ks[-2]["c"] - 1) * 100 >= 9.7:
            ks = ks[:-1]
    if len(ks) < 9:
        res["note"] = "日K不足(次新?)"
        PRE_CACHE[code] = res
        return res
    W = ks[-8:]                                          # 板前窗口 8 根
    W20 = ks[-20:]
    O = [k["o"] for k in W]; C = [k["c"] for k in W]
    H = [k["h"] for k in W]; L = [k["l"] for k in W]; V = [k["v"] for k in W]
    allC = [k["c"] for k in ks]
    vma20 = sum(k["v"] for k in W20) / len(W20)
    vavg = sum(V) / 8
    Hmax, Lmin = max(H), min(L)
    box = (Hmax - Lmin) / Lmin * 100                     # 箱体幅度%
    v_late = sum(V[-3:]) / 3
    pct = [(C[i] / (C[i - 1] if i > 0 else O[i]) - 1) * 100 for i in range(8)]
    avg_abs = sum(abs(p) for p in pct) / 8
    ma5 = sum(C[-5:]) / 5
    ma10 = sum(C[-10:]) / 10
    glue = abs(ma5 - ma10) / ma10 * 100                  # 均线粘合度%
    ma20c = sum(allC[-20:]) / 20 if len(allC) >= 20 else sum(allC) / len(allC)

    # ---- A 控（横盘缩量筑底）40：箱体窄幅15 + 缩量15 + 均线粘合/小阴小阳10 ----
    a_box = 15 if box <= 12 else 11 if box <= 18 else 6 if box <= 25 else 0
    vr = v_late / vma20 if vma20 else 9
    a_vol = 15 if vr <= 0.8 else 11 if vr <= 1.0 else 7 if vr <= 1.2 else 2
    a_ma = 10 if (avg_abs <= 2.0 and glue <= 3.0) else 6 if avg_abs <= 3.0 else 2
    ctrl = a_box + a_vol + a_ma
    if box <= 18:
        res["tags"].append("横盘%.0f%%" % box)
    if vr <= 1.0:
        res["tags"].append("缩量")
    if glue <= 3:
        res["tags"].append("均线粘合")

    # ---- B 试（放量长上影试盘针，冲高回落不破位）30 ----
    tests, soft = [], 0
    for i in range(8):
        o, c, h, l, v = O[i], C[i], H[i], L[i], V[i]
        rng = h - l
        if rng <= 0:
            continue
        upper = h - max(o, c)
        upper_pct = upper / c * 100
        prevc = C[i - 1] if i > 0 else o
        rush = (h / prevc - 1) * 100
        if upper / rng >= 0.45 and upper_pct >= 2.5 and v >= vavg * 1.3 and l >= Lmin * 0.995:
            tests.append(i)
        elif rush >= 4 and upper_pct >= 1.5 and c < h * 0.985:
            soft += 1
    test = 30 if len(tests) >= 2 else 22 if len(tests) == 1 else 12 if soft >= 1 else 0
    if tests:
        res["tags"].append("试盘针x%d" % len(tests))
    elif soft:
        res["tags"].append("冲高回落")

    # ---- C 洗（缩量回踩/挖坑、长下影、不破20日线）30 ----
    li = min(range(8), key=lambda i: L[i])
    lk = W[li]
    rng = lk["h"] - lk["l"]
    lower = min(lk["o"], lk["c"]) - lk["l"]
    pullback = (Hmax - lk["l"]) / Hmax * 100
    held = lk["c"] >= ma20c * 0.97
    shrink = lk["v"] <= vavg * 0.95
    lower_shadow = rng > 0 and lower / rng >= 0.4
    if pullback >= 4 and shrink and held and lower_shadow:
        wash = 30
    elif pullback >= 4 and shrink and held:
        wash = 20
    elif lower_shadow and held:
        wash = 14
    elif pullback >= 3 and held:
        wash = 12
    else:
        wash = 8
    if pullback >= 4 and held:
        res["tags"].append("回踩%.0f%%不破" % pullback)

    # ---- D 平台时长加分（准备越久高度越高）----
    days = 0
    for i in range(len(ks) - 1, -1, -1):
        k = ks[i]
        if (k["c"] <= Hmax * 1.005 and k["c"] >= Lmin * 0.995
                and (k["h"] - k["l"]) / k["l"] * 100 <= box + 3):
            days += 1
        else:
            break
    days = min(days, 20)
    bonus = 10 if days >= 15 else 7 if days >= 10 else 4 if days >= 8 else 2 if days >= 5 else 0

    pre = min(ctrl + test + wash + bonus, 100)

    # ---- 挖坑反转识别（首板前一日放量下探→阳线反包收回=洗盘结束；非横盘，单列）----
    lastk = W[-1]
    prevc = C[-2] if len(C) >= 2 else lastk["o"]
    lrng = lastk["h"] - lastk["l"]
    close_pos = (lastk["c"] - lastk["l"]) / lrng if lrng > 0 else 0
    dig_down = (prevc - lastk["l"]) / prevc * 100
    dig = (lastk["v"] >= vavg * 1.3 and lastk["c"] > lastk["o"]
           and (lastk["c"] / prevc - 1) * 100 >= 3 and dig_down >= 3 and close_pos >= 0.6)

    # ---- 否决/降级（裸拉识别）----
    trend = (C[-1] / C[0] - 1) * 100
    veto = ""
    dig_rev = False
    if trend <= -8:
        if dig:
            dig_rev = True
            wash = max(wash, 24)
            pre = min(ctrl + test + wash + bonus, 60)
            veto = "挖坑反转(非横盘)"
            res["tags"].append("挖坑放量反包")
        else:
            veto = "阴跌未企稳(%.0f%%)" % trend
            pre = min(pre, 35)
    base = min(k["c"] for k in W20)
    rise = (C[-1] / base - 1) * 100
    if rise >= 50:
        veto = (veto + " " if veto else "") + "高位(月涨%.0f%%)" % rise
        pre = min(pre, 52)

    if dig_rev:
        grade = "挖坑反转"
    elif pre >= 75:
        grade = "板前完整"
    elif pre >= 60:
        grade = "有准备"
    elif pre >= 45:
        grade = "准备不足"
    else:
        grade = "裸拉/杂毛"

    res.update(pre=pre, ctrl=ctrl, test=test, wash=wash, days=days, box=round(box, 1),
               veto=veto.strip(), grade=grade)
    PRE_CACHE[code] = res
    return res


def seal_score(x):
    """封板当天 100 分：首板时间30 + 炸板25 + 封单厚度20 + 换手15 + 板型10。"""
    fbt = x.get("fbt", 0) or 0
    zbc = x.get("zbc", 0) or 0
    fund, ltsz = x.get("fund", 0) or 0, x.get("ltsz", 0) or 0
    hs = x.get("hs", 0) or 0
    t = fbt if fbt < 240000 else 95900
    s_t = 30 if t <= 94500 else 26 if t <= 101500 else 22 if t <= 110000 else \
        15 if t <= 133000 else 9 if t <= 143000 else 4
    s_z = 25 if zbc == 0 else 17 if zbc == 1 else 10 if zbc == 2 else 0
    ratio = fund / ltsz * 100 if ltsz else 0
    s_f = 20 if ratio >= 5 else 16 if ratio >= 3 else 12 if ratio >= 1.5 else \
        8 if ratio >= 0.8 else 4
    s_h = 15 if 7 <= hs <= 15 else 12 if 5 <= hs < 7 else 9 if 15 < hs <= 20 else \
        8 if 3 <= hs < 5 else 6
    yizi = (fbt <= 93000 and zbc == 0)
    s_p = 8 if yizi else 10
    return s_t + s_z + s_f + s_h + s_p, \
        {"t": s_t, "z": s_z, "f": s_f, "h": s_h, "p": s_p,
         "fr": round(ratio, 1), "yizi": yizi,
         "tstr": fbt_str(fbt)}


def fbt_str(fbt):
    if not fbt:
        return "--"
    return "%02d:%02d" % (fbt // 10000, (fbt // 100) % 100)


def mkt_phase():
    """返回 竞价/早盘/午休/午后/收盘/休市"""
    now = datetime.datetime.now()
    if now.weekday() >= 5:
        return "休市"
    t = now.hour * 100 + now.minute
    if 915 <= t < 930:
        return "竞价"
    if 930 <= t <= 1130:
        return "早盘"
    if 1130 < t < 1300:
        return "午休"
    if 1300 <= t <= 1500:
        return "午后"
    return "收盘"


def sleep_to_next():
    now = datetime.datetime.now()
    t = now.hour * 100 + now.minute
    target = None
    if now.weekday() >= 5:
        target = now + datetime.timedelta(days=7 - now.weekday())
        target = target.replace(hour=9, minute=15, second=0, microsecond=0)
    elif t < 915:
        target = now.replace(hour=9, minute=15, second=0, microsecond=0)
    elif 1130 < t < 1300:
        target = now.replace(hour=13, minute=0, second=0, microsecond=0)
    elif t > 1500:
        target = (now + datetime.timedelta(days=1)).replace(hour=9, minute=15, second=0, microsecond=0)
        if target.weekday() >= 5:
            target += datetime.timedelta(days=7 - target.weekday())
    if target:
        s = (target - now).total_seconds()
        log("非交易时段，休眠到", target.strftime("%m-%d %H:%M"), "（按Ctrl+C退出）")
        time.sleep(min(max(s, 1), 600))  # 每10分钟醒一次，便于跨天/对时
    else:
        time.sleep(20)


class Radar:
    def __init__(self):
        self.date = today_str()
        self.seen_zt = set()
        self.seen_zb = set()
        self.board_tier = {}
        self.height = 0
        self.watch_level = {}
        self.sent_sentiment = set()
        self.inited = False

    def reset_if_new_day(self):
        d = today_str()
        if d != self.date:
            self.__init__()
            push("新交易日雷达已启动", d)

    def analyze_pools(self, zt, zb, dt, baseline=False):
        cnt = Counter(x.get("hybk", "其他") for x in zt)
        nzt, nzb, ndt = len(zt), len(zb), len(dt)
        zbr = nzb / (nzt + nzb) * 100 if (nzt + nzb) else 0

        # ---- 1) 板块涨停潮 ----
        tiers = [3, 5, 7, 9, 12, 15]
        for hy, n in cnt.items():
            tier = max([t for t in tiers if n >= t], default=0)
            if tier < BOARD_LIMIT:
                continue
            old = self.board_tier.get(hy, 0)
            if tier > old:
                self.board_tier[hy] = tier
                stocks = [x for x in zt if x.get("hybk") == hy]
                leader = max(stocks, key=lambda x: x.get("lbc", 1))
                top = sorted(stocks, key=lambda x: -x.get("lbc", 1))
                names = "、".join("%s%s板" % (x["n"], x.get("lbc", 1)) for x in top[:8])
                if baseline:
                    head = "当前最强板块·%s（%d只）" % (hy, n)
                else:
                    head = ("🟢板块涨停潮·%s（%d只）" % (hy, n)) if tier == 3 else ("🔥板块强化·%s（%d只）" % (hy, n))
                push(head, "龙头：%s(%s) %s连板，封单%.1f亿\n梯队：%s"
                     % (leader["n"], leader["c"], leader.get("lbc", 1),
                        leader.get("fund", 0) / 1e8, names))

        # ---- 2) 新连板 + 3) 主板新首板 ----
        cur = {x["c"]: x for x in zt}
        new_first, new_link = [], []
        for c, x in cur.items():
            if c in self.seen_zt:
                continue
            self.seen_zt.add(c)
            if baseline:
                continue
            if c in BLACKLIST:
                continue
            ok = is_main(c) and not is_st(x["n"])
            price = x.get("p", 0) / 1000
            if x.get("lbc", 1) >= 2:
                new_link.append((x, ok))
            elif ok and price <= PRICE_MAX:
                new_first.append(x)
        for x, ok in sorted(new_link, key=lambda y: -y[0].get("lbc", 1)):
            tag = "🔗" if ok else "⚠️非主板"
            push("%s%s连板·%s" % (tag, x.get("lbc", 1), x["n"]),
                 "代码%s 价%.2f 封单%.1f亿 换手%.1f%% 行业%s"
                 % (x["c"], x.get("p", 0) / 1000, x.get("fund", 0) / 1e8,
                    x.get("hs", 0), x.get("hybk", "")))
        if new_first:
            def _gs(p):
                if p is None:
                    return "?"
                return "完整" if p >= 75 else "有备" if p >= 60 else "不足" if p >= 45 else "裸拉"
            scored = []
            for x in new_first:
                pre = board_pre_action(x["c"])
                scored.append((x, pre))
            scored.sort(key=lambda y: -(y[1]["pre"] or 0))
            lines = []
            for x, pre in scored[:12]:
                p = pre["pre"]
                icon = "🟢" if (p is not None and p >= 75) else "🟡" if (p is not None and p >= 45) else "⚪"
                veto = "⛔" if pre.get("veto") else ""
                ps = ("%d%s" % (p, _gs(p))) if p is not None else "?"
                lines.append("%s%s(%s)板前%s%s" % (icon, x["n"], x["c"], ps, veto))
            push("🆕主板新首板%d只·按板前动作排序（明日一进二）" % len(new_first),
                 "\n".join(lines) + "\n🟢完整≥75 🟡≥45可看 ⚪裸拉剔除；收盘看--rank排序")

        # ---- 市场最高标 ----
        h = max((x.get("lbc", 1) for x in zt), default=0)
        if h > self.height:
            self.height = h
            if not baseline and h >= 2:
                leaders = [x for x in zt if x.get("lbc") == h]
                names = "、".join("%s(%s)" % (x["n"], x["c"]) for x in leaders[:3])
                push("🏆市场最高标：%d连板" % h, names)

        # ---- 4) 炸板（高标/主线/自选才单独推） ----
        watch_codes = {c for v in WATCH.values() for c in v}
        for x in zb:
            c = x["c"]
            if c in self.seen_zb:
                continue
            self.seen_zb.add(c)
            if baseline:
                continue
            lbc = (x.get("zttj") or {}).get("ct", 1)
            strong = (lbc and lbc >= 2) or (cnt.get(x.get("hybk", ""), 0) >= BOARD_LIMIT) or (c in watch_codes)
            if strong:
                push("💥炸板·%s（%s）" % (x["n"], x.get("hybk", "")),
                     "代码%s 曾%s连板，炸板%s次——板块情绪走弱，该方向不追/不接"
                     % (c, lbc, x.get("zbc", "?")))

        # ---- 5) 情绪极端 ----
        if not baseline:
            if nzt < 20 and ndt > nzt and "ice" not in self.sent_sentiment:
                self.sent_sentiment.add("ice")
                push("⛔情绪冰点", "涨停%d 跌停%d 炸板率%.0f%%——空仓观望，不打板" % (nzt, ndt, zbr))
            elif nzt >= 20 and zbr >= 40 and "zbhi" not in self.sent_sentiment:
                self.sent_sentiment.add("zbhi")
                push("⚠️炸板率飙升", "涨停%d 炸板%d，炸板率%.0f%%——打板/半路风险大" % (nzt, nzb, zbr))
            if nzt >= 30 and zbr < 20 and "hot" not in self.sent_sentiment:
                self.sent_sentiment.add("hot")
                push("🟢情绪回暖", "涨停%d 跌停%d 炸板率%.0f%%——主线可按计划执行" % (nzt, ndt, zbr))
        return nzt, nzb, ndt, zbr

    def analyze_watch(self, baseline=False):
        allc = [c for v in WATCH.values() for c in v]
        if not allc:
            return
        q = tencent_quotes(sorted(set(allc)))
        for grp, codes in WATCH.items():
            for c in codes:
                d = q.get(c)
                if not d:
                    continue
                lvset = self.watch_level.setdefault(c, set())
                if baseline:
                    # 启动快照：只报当前档位一次，并把已跨过的档标记，避免启动后重复
                    hit = [lv for lv in WATCH_PCT if d["pct"] >= lv]
                    if hit:
                        for lv in hit:
                            lvset.add(lv)
                        push("👀%s·%s 现%.1f%%（启动快照）" % (grp, d["name"], d["pct"]),
                             "现价%.2f 今开%.2f 成交%.1f亿" % (d["price"], d["open"], d["amount"] / 1e4))
                    if d["open"] > 0 and d["price"] < d["open"]:
                        lvset.add("below")
                    continue
                for lv in WATCH_PCT:
                    if d["pct"] >= lv and lv not in lvset:
                        lvset.add(lv)
                        push("👀%s·%s 涨%.1f%%" % (grp, d["name"], d["pct"]),
                             "现价%.2f 今开%.2f 成交%.1f亿（%s%g%%档，对照盘前计划）"
                             % (d["price"], d["open"], d["amount"] / 1e4, grp, lv))
                if d["open"] > 0 and d["price"] < d["open"] and "below" not in lvset:
                    lvset.add("below")
                    push("🔻%s·%s 跌破开盘价" % (grp, d["name"]),
                         "现价%.2f 今开%.2f——开盘第一波不成立，轨1不接" % (d["price"], d["open"]))

    def tick(self):
        self.reset_if_new_day()
        date = today_str()
        zt, zb, dt = em_pool("zt", date), em_pool("zb", date), em_pool("dt", date)
        if not self.inited:
            nzt, nzb, ndt, zbr = self.analyze_pools(zt, zb, dt, baseline=True)
            best = Counter(x.get("hybk", "其他") for x in zt).most_common(3)
            best_s = "、".join("%s%d只" % (h, n) for h, n in best)
            push("✅雷达启动·当前快照",
                 "%s 涨停%d 跌停%d 炸板率%.0f%%\n最强：%s\n（之后只报新发生的信号）"
                 % (date, nzt, ndt, zbr, best_s or "无"))
            self.analyze_watch(baseline=True)
            self.inited = True
        else:
            self.analyze_pools(zt, zb, dt, baseline=False)
            self.analyze_watch(baseline=False)


def collect_first_boards(date):
    """收集当日主板首板，返回 (合格候选list, 剔除list)。三层漏斗：板前动作+封板质量+板块梯队。"""
    zt = em_pool("zt", date)
    zb = em_pool("zb", date)
    zbr = len(zb) / (len(zt) + len(zb)) * 100 if (len(zt) + len(zb)) else 0
    board_cnt = Counter(x.get("hybk", "其他") for x in zt)
    cand, drop = [], []
    raws = [x for x in zt if x.get("lbc", 1) == 1 and x.get("c") not in BLACKLIST
            and is_main(x["c"]) and not is_st(x["n"])]
    qmap = tencent_quotes([x["c"] for x in raws])
    for x in raws:
        c = x["c"]
        price = x.get("p", 0) / 1000
        amt = x.get("amount", 0) / 1e8
        hy = x.get("hybk", "其他")
        tier = board_cnt.get(hy, 1)
        pre = board_pre_action(c, date)
        seal, sd = seal_score(x)
        pe = (qmap.get(c) or {}).get("pe")
        lot = round(price * 100)
        risk = round(price * 100 * 0.05)
        row = {"x": x, "code": c, "name": x["n"], "hy": hy, "tier": tier,
               "price": price, "amt": amt, "pe": pe, "lot": lot, "risk": risk,
               "pre": pre, "seal": seal, "sd": sd}
        # ---- 硬门槛 ----
        reasons = []
        if price <= 0 or price > PRICE_MAX:
            reasons.append("股价>%g" % PRICE_MAX)
        if amt < 1.0:
            reasons.append("成交<1亿")
        if pe is not None and pe <= 0:
            reasons.append("PE为负")
        if tier < 2:
            reasons.append("孤板(行业仅1)")
        if pre["pre"] is None:
            reasons.append(pre.get("note") or "无板前数据")
        elif pre["pre"] < 45:
            reasons.append("板前%d裸拉" % pre["pre"])
        if "阴跌" in (pre.get("veto") or ""):
            reasons.append(pre["veto"])
        if reasons:
            row["reasons"] = reasons
            drop.append(row)
        else:
            comp = round((pre["pre"] or 0) * 0.45 + seal * 0.55, 1)
            row["comp"] = comp
            cand.append(row)
    cand.sort(key=lambda r: -r["comp"])
    return cand, drop, {"zt": len(zt), "zb": len(zb), "zbr": zbr}


def cmd_rank(push_too=True, date=None):
    date = date or last_trade_date()
    cand, drop, stat = collect_first_boards(date)
    L = ["📋 一进二候选排序（%s 首板·三层漏斗）" % date[4:6] + "-" + date[6:8],
         "涨停%d 炸板率%.0f%% ｜ 主板首板合格%d只" % (stat["zt"], stat["zbr"], len(cand))]
    if cand:
        L.append("【✅可打·按综合分，前5】")
        for i, r in enumerate(cand[:5], 1):
            p, sd = r["pre"], r["sd"]
            hi = "高位" if "高位" in (p.get("veto") or "") else ""
            L.append(
                "%d %s%s｜%s梯队%d\n"
                "  板前%d(%s 洗%d/试%d/控%d·%d天%s) 封板%d(%s/炸%d/封单%.1f%%/换手%.0f%%) 综合%.0f\n"
                "  收%.2f 1手%d元 -5%%风险%d元 PE%s"
                % (i, r["name"], r["code"], r["hy"], r["tier"],
                   p["pre"], p["grade"], p["wash"], p["test"], p["ctrl"], p["days"],
                   ("/" + hi if hi else ""), r["seal"], sd["tstr"],
                   r["x"].get("zbc", 0), sd["fr"], r["x"].get("hs", 0), r["comp"],
                   r["price"], r["lot"], r["risk"],
                   ("%.0f" % r["pe"]) if r["pe"] else "?"))
    else:
        L.append("【✅无合格候选——空仓，不硬凑】")
    if drop:
        watch = [r for r in drop if (r["pre"]["pre"] or 0) >= 70
                 and any("孤板" in x for x in r["reasons"])]
        if watch:
            L.append("【⚪板前好但孤板·明天看板块是否跟上】" + "、".join(
                "%s%s(%s·板前%d)" % (r["name"], r["code"], r["hy"], r["pre"]["pre"])
                for r in watch[:5]))
        other = [r for r in drop if r not in watch]
        if other:
            L.append("【❌剔除】" + "；".join(
                "%s(%s)" % (r["name"], r["reasons"][0]) for r in other[:12]))
    L.append("⏭明早第三层：高开3-7%、竞价换手≥前日5%、板块≥2只高开；一字/>7%不排，打不到不等回踩。")
    msg = "\n".join(L)
    print(msg)
    if push_too:
        push("📋一进二候选排序（%s）" % date[4:], "\n".join(L[1:]))
    return cand, drop


def cmd_board(code):
    pre = board_pre_action(code, None)
    q = tencent_quotes([code]).get(code)
    print("==== %s 板前动作诊断 ====" % code)
    if q:
        print("名称%s 现价%.2f 涨幅%.1f%% PE=%s" % (q["name"], q["price"], q["pct"], q["pe"]))
    if pre["pre"] is None:
        print("无法评分：", pre.get("note"))
        return
    print("板前总分 %d / %s（平台%d天，箱体%.1f%%）" % (pre["pre"], pre["grade"], pre["days"], pre["box"] or 0))
    print("  控盘%d 试盘%d 洗盘%d" % (pre["ctrl"], pre["test"], pre["wash"]))
    print("  信号：%s" % ("、".join(pre["tags"]) or "无"))
    print("  否决：%s" % (pre["veto"] or "无"))
    print("  判读：" + ("≥75板前完整=主力蓄谋，可入一进二池；60-74有准备；45-59勉强；<45裸拉杂毛，不做"
                       if pre["pre"] >= 45 else "<45=无准备裸拉，不进一进二"))


def main():
    if "--test" in sys.argv:
        log("自测模式：拉取今日快照（不循环）")
        r = Radar()
        r.inited = False
        r.tick()
        return
    if "--rank" in sys.argv:
        log("收盘一进二候选排序（板前动作+封板质量）")
        d = None
        if "--date" in sys.argv:
            d = sys.argv[sys.argv.index("--date") + 1]
        cmd_rank(push_too=True, date=d)
        return
    if "--board" in sys.argv:
        i = sys.argv.index("--board")
        if i + 1 < len(sys.argv):
            cmd_board(sys.argv[i + 1])
        else:
            print("用法：python3 limit_up_radar.py --board 002552")
        return
    log("涨停雷达启动，轮询间隔%ds；配置 BARK_KEY 或 SERVERCHAN_KEY 可推送到手机。" % POLL_SEC)
    r = Radar()
    while True:
        phase = mkt_phase()
        if phase in ("竞价", "早盘", "午后"):
            try:
                r.tick()
            except Exception as e:
                log("本轮异常（已跳过）:", repr(e))
            time.sleep(POLL_SEC)
        else:
            sleep_to_next()


if __name__ == "__main__":
    main()
