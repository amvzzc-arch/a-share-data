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
import urllib.request, urllib.parse, json, time, datetime, sys, os
from collections import Counter

# ==================== 配置区：你只需要改这里 ====================
# ① iPhone：App Store 装 "Bark"，打开后把 https://api.day.app/XXXX 里的 XXXX 填下面
BARK_KEY = "XDg67Er4obRebVkeuQFwq6"
# ② 安卓/想用微信：去 sct.ftqq.com 微信登录拿 SCTxxxx 填下面（和Bark二选一，两个都留空=只在电脑屏幕显示）
SERVERCHAN_KEY = ""
# ③ 板块涨停潮阈值（几只涨停就报警，建议3）
BOARD_LIMIT = 3
# ④ 轮询间隔（秒）
POLL_SEC = 25
# ⑤ 自选股（每天按8:50作战计划填"中军"，持仓填"持仓"，竞价候选填"一进二"）
WATCH = {
    # 10/26 中军（轨1右侧，9:35-9:45，优先级三花>双环>拓普，一次一只）：三花002050/双环002472/拓普601689；安乃达603350(机器人电机)；AI眼镜=水晶002273/歌尔002241(10/28苹果前，保留观察)
    "中军": ["002050", "002472", "601689", "603350", "002273", "002241"],
    # 持仓：10/23 已在24元卖出卧龙电驱，当前空仓，无持仓T+1监控
    "持仓": [],
    # 10/26 附录A 周五午后V反首板（次日不打二板、不追高开>5%、过修复三信号后回踩企稳右侧、10:30前、降级≤200/固定100股）：万向钱潮000559/山东威达002026二选一；大丰603081；派斯林600215(必核净资产为正/市值≤50亿)；罗普斯金002333(仅放量换手板才确认妖胚)；旧候选大洋电机002249/山河智能002097保留观察
    "一进二": ["000559", "002026", "603081", "600215", "002333", "002249", "002097"],
}
# ⑥ 硬筛：只要主板、股价上限（v2.8：当前1.3万阶段统一≤48）
PRICE_MAX = 48.0
# 中军涨幅提醒档位（%）
WATCH_PCT = [2.0, 5.0, 7.0, 9.8]
# ⑦ 黑名单（永不提示：违纪/退市风险/停牌核查，可自行追加）
BLACKLIST = {"600792", "002285", "600825", "002585", "000021", "000733", "002185", "002402", "603162", "002535", "603278"}  # 002535林州重机2026-10被证监会立案+虚假记载处罚告知永久拉黑；603278纯情绪高标（异动自查无实控人，只看不碰）
# ============================================================

# ==================== 轨3 v2.0·拐点接力（2026-10-27 拍板）硬红线/评分常量 ====================
GENE_WINDOW = 120      # 涨停基因窗口：近半年≈120个交易日
GENE_MIN = 3           # 近半年涨停≥3次（新主线首日硬催化/跨年妖胚豁免，脚本单列待判）
LTSZ_MIN = 20.0        # 流通市值下限（亿），仅轨3
LTSZ_MAX = 100.0       # 流通市值上限（亿），仅轨3
FUND_AMT_MIN = 10.0    # 封单额≥当日成交额10%（强）
FBT_DEADLINE = 110000  # 当日首板须11:00前封死（13:00后走附录A，不进一进二）
YIZI_FBT = 93000       # 集合竞价/9:30开盘即封死=一字板
WULIANG_FBT = 93100    # 9:30:00-9:31:00 且无换手=无量秒板
YEAR_RISE_MAX = 400.0  # 近一年涨幅≥400%（≈5倍）列高标，只看不碰
# 次日竞价口径（9:25验证，本收盘rank只给标准）
OPEN_SWEET = (2.0, 5.0)   # 高开2-5%最佳
OPEN_DOWNGRADE = 8.0      # 5-8%封板瞬间降档/秒板回封不追；>8%不追
AUCTION_AMT_MIN = 5.0     # 竞价额/昨成交额≥5%强，≥8%极强
DAILY_BFQ_CACHE = {}
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache_daily")


def _kfile_load(code, tag):
    """读日K文件缓存，返回 (rows, fetched_day)；无则 (None, None)。"""
    p = os.path.join(CACHE_DIR, "%s_%s.json" % (code, tag))
    try:
        with open(p, "r", encoding="utf-8") as f:
            obj = json.load(f)
        return obj.get("rows"), obj.get("day")
    except Exception:
        return None, None


def _kfile_save(code, tag, rows, day):
    """写日K文件缓存（自然日；当天重跑直接复用，避免反复拉腾讯触发501限流）。"""
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        p = os.path.join(CACHE_DIR, "%s_%s.json" % (code, tag))
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"day": day, "rows": rows}, f, ensure_ascii=False)
    except Exception:
        pass

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
            u = ("https://api.day.app/" + BARK_KEY + "/"
                 + urllib.parse.quote(title, safe='') + "/"
                 + urllib.parse.quote(body, safe='')
                 + "?group=A%E8%82%A1%E9%9B%B7%E8%BE%BE&sound=alarm")
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


# ==================== 轨4：新主线首次共振·中军信号卡（10:00-11:00，v2.5） ====================
_BOARD_MAP = None
_BOARD_MAP_TS = 0
GUI4_WINDOW = (10 * 60, 11 * 60)   # 10:00-11:00，只做第一次共振
GUI4_LOT_MAX = 3000.0             # 轨4 1手红线≤3000


BOARD_HOSTS = ["88.push2", "1.push2", "2.push2", "82.push2", "83.push2", "3.push2", "push2"]
BOARD_MAP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "board_industry_map.json")


def board_clist(fs, fields, fid="f3", pz=300):
    """东财clist接口多域名故障转移（东财对单域名会随机断连），返回diff列表。"""
    api = ("api/qt/clist/get?pn=1&pz=%d&po=1&np=1&fltt=2&invt=2&fid=%s&fs=%s&fields=%s"
           % (pz, fid, fs, fields))
    last = None
    for h in BOARD_HOSTS:
        for _ in range(2):
            try:
                d = json.loads(http_get("https://%s.eastmoney.com/%s" % (h, api), timeout=8))
                diff = (d.get("data") or {}).get("diff") or []
                if isinstance(diff, dict):
                    diff = list(diff.values())
                if diff:
                    return diff
            except Exception as e:
                last = e
                time.sleep(0.4)
    log("板块接口全域名失败:", last)
    return []


def get_board_map():
    """行业板块名→BK代码：本地静态文件兜底+联网更新（BK代码长期不变），30分钟缓存。"""
    global _BOARD_MAP, _BOARD_MAP_TS
    if _BOARD_MAP and time.time() - _BOARD_MAP_TS < 1800:
        return _BOARD_MAP
    if _BOARD_MAP_TS and not _BOARD_MAP and time.time() - _BOARD_MAP_TS < 60:
        return {}
    m = {}
    try:
        with open(BOARD_MAP_FILE, encoding="utf-8") as fp:
            m = json.load(fp)
    except Exception:
        m = {}
    online = {}
    for x in board_clist("m:90+t:2", "f12,f14", pz=300):
        online[x.get("f14")] = x.get("f12")
    if online:
        m.update(online)
        try:
            with open(BOARD_MAP_FILE, "w", encoding="utf-8") as fp:
                json.dump(m, fp, ensure_ascii=False)
        except Exception:
            pass
    _BOARD_MAP_TS = time.time()
    if m:
        _BOARD_MAP = m
    return _BOARD_MAP or {}


def resolve_board(name):
    """行业名→BK代码：先精确，再包含匹配（涨停池简称如『汽车零部』→板块榜『汽车零部件』）。"""
    m = get_board_map()
    if name in m:
        return m[name]
    cands = [(f14, bk) for f14, bk in m.items() if name in f14 or f14 in name]
    if cands:
        cands.sort(key=lambda x: abs(len(x[0]) - len(name)))
        return cands[0][1]
    return None


def board_zhongjun(bk, exclude):
    """板块成分里找未涨停中军：主板、+1~5%、成交≥1亿、现价>今开、价≤100。按成交额排序取前3。"""
    if not bk:
        return []
    diff = board_clist("b:%s" % bk, "f2,f3,f6,f12,f14,f17", fid="f6", pz=300)
    if not diff:
        return []
    out = []
    for x in diff:
        c = x.get("f12")
        if not c or c in exclude or not is_main(c) or is_st(x.get("f14", "")):
            continue
        pct, price, amt, op = x.get("f3") or 0, x.get("f2") or 0, x.get("f6") or 0, x.get("f17") or 0
        if price <= 0 or price > PRICE_MAX:
            continue
        if not (1.0 <= pct <= 5.0):       # 轨4中军+1~5%，>5%不追
            continue
        if amt < 1e8:                      # 成交≥1亿
            continue
        if op > 0 and price < op:          # 现价低于今开=弱势，排除
            continue
        out.append((x.get("f14"), c, price, pct, amt))
    out.sort(key=lambda z: -z[4])
    return out[:3]


def sh_pct():
    """上证实时涨跌幅(%)。"""
    try:
        f = http_get("https://qt.gtimg.cn/q=sh000001", gbk=True).split("~")
        return (float(f[3]) - float(f[4])) / float(f[4]) * 100
    except Exception:
        return 0.0


def gui4_window_open():
    hm = datetime.datetime.now().hour * 60 + datetime.datetime.now().minute
    return GUI4_WINDOW[0] <= hm <= GUI4_WINDOW[1]


def gui4_signal(hy, n_hy, stocks, zb_hy, zbr, leader):
    """板块首次≥3涨停（10:00-11:00）时，按轨4七条件硬核对并推信号卡。每板块每天一次。"""
    zb_in = sum(1 for x in zb_hy if x.get("hybk") == hy)
    sh = sh_pct()
    # 硬闸门：板块内0炸板 / 全市场炸板率<40% / 上证跌<1% / 龙头封死
    gate_ok = (zb_in == 0 and zbr < 40 and sh > -1 and (leader.get("fund") or 0) > 0)
    reason = []
    if zb_in != 0:
        reason.append("板块内%d只炸板" % zb_in)
    if zbr >= 40:
        reason.append("全市场炸板率%.0f%%≥40%%" % zbr)
    if sh <= -1:
        reason.append("上证%.1f%%崩盘" % sh)
    if not ((leader.get("fund") or 0) > 0):
        reason.append("龙头未封死")
    bk = resolve_board(hy)
    zj = board_zhongjun(bk, {x["c"] for x in stocks}) if gate_ok else []
    if not gate_ok:
        # 不满足硬闸门静默（避免窗口打开瞬间多板块刷屏），仅本地留痕
        log("轨4关闭·%s：%s" % (hy, "、".join(reason) or "无"))
        return
    if not zj:
        push("🟡轨4共振·%s（%d涨停/0炸板）但无合格中军" % (hy, n_hy),
             "板块达标，但没有主板+1~5%、放量、站上今开、价≤100的中军（多已超5%/非主板）→不追")
        return
    lines = []
    for nm, c, p, pct, amt in zj:
        stop = round(p * 0.97, 2)          # 破分时均线/-3%量化止损
        lot = round(p * 100)
        risk = round((p - stop) * 100)
        tag = "✅" if lot <= GUI4_LOT_MAX and risk <= 250 else "⚠️1手超线"
        lines.append("%s%s(%s) 现%.2f(+%.1f%%) 买%.2f-%.2f 100股%d元 止损%.2f 风险%d元 %s"
                     % (tag, nm, c, p, pct, round(p * 0.995, 2), round(p * 1.005, 2),
                        lot, stop, risk, "" if lot <= GUI4_LOT_MAX else "轨4放弃"))
    push("🟢轨4共振·%s（%d只涨停/板块内0炸板/炸板率%.0f%%）" % (hy, n_hy, zbr),
         "龙头：%s(%s)%s板封单%.1f亿 上证%.1f%%\n"
         % (leader["n"], leader["c"], leader.get("lbc", 1), leader.get("fund", 0) / 1e8, sh)
         + "\n".join(lines)
         + "\n窗口10:00-11:00｜先人工确认『新题材首日/发酵1-2天』(老热点反抽不做)｜>5%不追、破分时均线/-3%走、1手≤3000、风险≤250")


def today_str():
    return datetime.datetime.now().strftime("%Y%m%d")


# ==================== 板前动作（凯心法）· 首板前5~8日 洗/试/控 ====================
PRE_CACHE = {}


def em_index_dates(n=25):
    """东财上证指数日K交易日历（YYYYMMDD 时间正序），盘前最后一根=昨日、盘后=今天；腾讯限流时的可靠主源。"""
    try:
        u = ("http://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.000001"
             "&fields1=f1,f2,f3&fields2=f51,f52,f53&klt=101&fqt=1&beg=0&end=20500000&lmt=%d" % n)
        d = json.loads(http_get(u))
        kl = (d.get("data") or {}).get("klines") or []
        return [s.split(",")[0].replace("-", "") for s in kl]
    except Exception:
        return []


def last_trade_date():
    """最近一个已收盘交易日 YYYYMMDD（东财指数日K为主；收盘后=今天，盘前=昨天）。"""
    ds = em_index_dates(10)
    if ds:
        return ds[-1]
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


def em_daily(code, fqt=0, n=250):
    """东财日K（push2his，主源）：fqt=0不复权/1前复权；返回 [{date,o,c,h,l,v(手)}...] 时间正序。
    与涨停池同源、稳定不封IP；内存+文件缓存（自然日），网络失败回退旧缓存兜底。"""
    tag = "em%d" % fqt
    ck = (code, tag)
    if ck in DAILY_BFQ_CACHE:
        return DAILY_BFQ_CACHE[ck]
    today = datetime.datetime.now().strftime("%Y%m%d")
    fc, fday = _kfile_load(code, tag)
    if fc and fday == today:
        DAILY_BFQ_CACHE[ck] = fc
        return fc
    secid = ("1." if code[0] == "6" else "0.") + code
    u = ("http://push2his.eastmoney.com/api/qt/stock/kline/get?secid=%s"
         "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
         "&klt=101&fqt=%d&beg=0&end=20500000&lmt=%d" % (secid, fqt, n + 20))
    out = []
    for attempt in range(3):
        try:
            d = json.loads(http_get(u))
            kl = (d.get("data") or {}).get("klines") or []
            for s in kl:
                p = s.split(",")
                try:
                    out.append({"date": p[0], "o": float(p[1]), "c": float(p[2]),
                                "h": float(p[3]), "l": float(p[4]), "v": float(p[5])})
                except Exception:
                    continue
            if out:
                out = out[-n:]
                _kfile_save(code, tag, out, today)
                break
        except Exception as e:
            if attempt < 2 and "501" in str(e):
                time.sleep(1.2 * (attempt + 1))
                out = []
                continue
            log("东财日K失败", code, e)
            break
    if not out and fc:
        log("东财日K失败，回退本地缓存", code, "末日", fc[-1]["date"])
        out = fc
    DAILY_BFQ_CACHE[ck] = out
    return out


def _tx_daily(code, n, adjust, days_back):
    """腾讯日K兜底（adjust='bfq'不复权/'qfq'前复权）；内存+文件缓存。"""
    tag = "tx%s%d" % (adjust, n)
    ck = (code, tag)
    if ck in DAILY_BFQ_CACHE:
        return DAILY_BFQ_CACHE[ck]
    today = datetime.datetime.now().strftime("%Y%m%d")
    fc, fday = _kfile_load(code, tag)
    if fc and fday == today:
        DAILY_BFQ_CACHE[ck] = fc
        return fc
    q = ("sh" if code[0] == "6" else "sz") + code
    end = datetime.datetime.now().strftime("%Y-%m-%d")
    start = (datetime.datetime.now() - datetime.timedelta(days=days_back)).strftime("%Y-%m-%d")
    u = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,%s,%s,%d,%s"
         % (q, start, end, n + 12, adjust))
    out = []
    for attempt in range(3):
        try:
            d = json.loads(http_get(u))
            node = d["data"][q]
            ks = node.get("day") or node.get("qfqday") or []
            for k in ks:
                try:
                    out.append({"date": k[0], "o": float(k[1]), "c": float(k[2]),
                                "h": float(k[3]), "l": float(k[4]), "v": float(k[5])})
                except Exception:
                    continue
            if out:
                _kfile_save(code, tag, out, today)
                break
        except Exception as e:
            if attempt < 2 and "501" in str(e):
                time.sleep(1.5 * (attempt + 1))
                out = []
                continue
            log("腾讯日K失败", code, e)
            break
    if not out and fc:
        log("腾讯日K限流，回退本地缓存", code, "末日", fc[-1]["date"])
        out = fc
    DAILY_BFQ_CACHE[ck] = out
    return out


def tencent_daily(code, n=30):
    """前复权日K（板前动作用）：东财主源、腾讯兜底。返回 [{date,o,c,h,l,v(手)}...] 时间正序。"""
    rows = em_daily(code, 1, n)
    if rows:
        return rows
    return _tx_daily(code, n, "qfq", 120)


def daily_bfq(code, n=250):
    """【不复权】日K（涨停基因/近一年涨幅必须用不复权，否则除权后历史涨停失真）：
    东财主源、腾讯兜底；返回 [{date,o,c,h,l,v}...] 时间正序，约 n 根（默认一年）。"""
    rows = em_daily(code, 0, n)
    if rows:
        return rows
    return _tx_daily(code, n, "bfq", 400)


def limit_up_gene(code, limit_date=None):
    """轨3 v2.0 涨停基因：近 GENE_WINDOW 个交易日内【封死】涨停次数（主板10%，留容差9.5~10.8%）。
    要求收盘≈当日最高（c≈h）才算封死，排除盘中触板回落。
    limit_date：只统计到该日（含）；收盘rank传当日=含今日首板。
    返回 {count, dates, hit}。"""
    ks = daily_bfq(code, 250)
    if limit_date:
        ld = limit_date[:4] + "-" + limit_date[4:6] + "-" + limit_date[6:8]
        ks = [k for k in ks if k["date"] <= ld]
    if not ks:
        return {"count": None, "dates": [], "hit": False}
    ks = ks[-GENE_WINDOW:]
    dates = []
    for i in range(1, len(ks)):
        pct = (ks[i]["c"] / ks[i - 1]["c"] - 1) * 100
        seal = abs(ks[i]["c"] - ks[i]["h"]) / ks[i]["c"] < 0.005   # 收盘≈最高=封死
        if 9.5 <= pct <= 10.8 and seal:
            dates.append(ks[i]["date"])
    return {"count": len(dates), "dates": dates, "hit": len(dates) >= GENE_MIN}


def year_rise(code, limit_date=None):
    """近一年涨幅(%)（用约250根不复权日K首尾），≥400%（≈5倍）列高标；数据不足返回None。"""
    ks = daily_bfq(code, 250)
    if limit_date:
        ld = limit_date[:4] + "-" + limit_date[4:6] + "-" + limit_date[6:8]
        ks = [k for k in ks if k["date"] <= ld]
    if len(ks) < 60:
        return None
    base, now = ks[0]["c"], ks[-1]["c"]
    return (now / base - 1) * 100 if base else None


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


# ==================== 轨3 v2.0 指标：封单额/一字/封板40分/板块25分/接力红盘率 ====================
def fund_amt_ratio(x):
    """封单额/当日成交额(%)。fund、amount 单位均为元。≥10%为强（v2.0 拍板）。"""
    fund, amt = x.get("fund", 0) or 0, x.get("amount", 0) or 0
    return fund / amt * 100 if amt else 0.0


def is_yizi(x):
    """一字/无量秒板（v2.0 不做、不排）：①集合竞价/9:30开盘即封死=一字；
    ②9:30:00-9:31:00 且0炸板、换手<2%=无量秒板。返回 (是否, 原因)。"""
    fbt = x.get("fbt", 0) or 0
    zbc = x.get("zbc", 0) or 0
    hs = x.get("hs", 0) or 0
    if fbt <= YIZI_FBT:
        return True, "一字板"
    if fbt <= WULIANG_FBT and zbc == 0 and hs < 2:
        return True, "无量秒板"
    return False, ""


def seal40(x):
    """封板质量 40 分制（v2.0 打分权重①）：首板时间12 + 炸板次数10 + 封单额/成交额10 + 换手8。
    炸板≥3次在硬红线直接否决；==2 在调用方标降档。"""
    fbt = x.get("fbt", 0) or 0
    t = fbt if fbt < 240000 else 95900
    s_t = 12 if t <= 93500 else 10 if t <= 94500 else 8 if t <= 101500 else \
        6 if t <= 110000 else 3
    zbc = x.get("zbc", 0) or 0
    s_z = 10 if zbc == 0 else 7 if zbc == 1 else 4 if zbc == 2 else 0
    fr = fund_amt_ratio(x)
    s_f = 10 if fr >= 20 else 8 if fr >= FUND_AMT_MIN else 5 if fr >= 5 else 2
    hs = x.get("hs", 0) or 0
    s_h = 8 if 7 <= hs <= 15 else 6 if (5 <= hs < 7 or 15 < hs <= 20) else \
        4 if 3 <= hs < 5 else 2
    return s_t + s_z + s_f + s_h, {"t": s_t, "z": s_z, "f": s_f, "h": s_h,
                                   "fr": round(fr, 0), "tstr": fbt_str(fbt)}


def board25(tier):
    """板块共振 25 分制（v2.0 打分权重②）：板块当日涨停只数 tier；tier<2 孤板在硬红线剔除。"""
    return 25 if tier >= 5 else 21 if tier == 4 else 17 if tier == 3 else 12 if tier == 2 else 0


def prev_trade_date(ref):
    """ref(YYYYMMDD) 之前最近一个已收盘交易日。东财指数日K为主、腾讯兜底。"""
    ds = em_index_dates(25)
    pri = [s for s in ds if s < ref]
    if pri:
        return pri[-1]
    try:
        end = datetime.datetime.now().strftime("%Y-%m-%d")
        start = (datetime.datetime.now() - datetime.timedelta(days=25)).strftime("%Y-%m-%d")
        u = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh000001,day,%s,%s,25,qfq"
             % (start, end))
        d = json.loads(http_get(u))
        ks = (d["data"]["sh000001"].get("day")
              or d["data"]["sh000001"].get("qfqday") or [])
        ds2 = [k[0].replace("-", "") for k in ks]
        pri2 = [s for s in ds2 if s < ref]
        return pri2[-1] if pri2 else None
    except Exception:
        return None


def relay_sentiment(today=None):
    """接力红盘率（v2.0 接力情绪总开关）：昨日主板首板，今日竞价/当前的红盘率、晋级率、平均高开。
    返回 dict；档由四档规则给建议（最终档结合当日炸板率/龙头是否闷杀由窗口确认）。"""
    today = today or last_trade_date()
    prev = prev_trade_date(today)
    out = {"today": today, "prev": prev, "n": 0, "open_red": None, "now_red": None,
           "promote": None, "avg_open": None, "tier": "数据不足", "note": ""}
    if not prev:
        out["note"] = "取不到上一交易日"
        return out
    zt_prev = em_pool("zt", prev)
    yfb = [x for x in zt_prev if x.get("lbc", 1) == 1 and is_main(x["c"])
           and not is_st(x["n"]) and x["c"] not in BLACKLIST]
    if not yfb:
        out["note"] = "昨日无主板首板"
        return out
    q = tencent_quotes([x["c"] for x in yfb])
    zt_today = {x["c"] for x in em_pool("zt", today)}
    valid = open_red = now_red = prom = 0
    opens = []
    for x in yfb:
        d = q.get(x["c"])
        if not d or not d.get("prev"):
            continue
        valid += 1
        op = (d["open"] / d["prev"] - 1) * 100
        opens.append(op)
        if op > 0:
            open_red += 1
        if d.get("pct", 0) > 0:
            now_red += 1
        if x["c"] in zt_today:
            prom += 1
    if not valid:
        out["note"] = "昨日首板今日行情取不到"
        return out
    orr = open_red / valid * 100
    out.update(n=valid, open_red=round(orr, 1), now_red=round(now_red / valid * 100, 1),
               promote=round(prom / valid * 100, 1),
               avg_open=round(sum(opens) / len(opens), 2))
    if orr < 40:
        out["tier"] = "冰点(<40%)→空仓转模拟"
    elif orr < 60:
        out["tier"] = "混沌(40-60%)→仅新主线首日·降级≤200"
    elif orr >= 70 and out["promote"] >= 25:
        out["tier"] = "发酵(高开/晋级高)→常规≤250"
    else:
        out["tier"] = "拐点候选(>60%)→待核龙头不闷杀+炸板率<30%·降级≤200"
    return out


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
        self.gui4_sent = set()
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

        # ---- 1.5) 轨4首次共振·中军信号卡（10:00-11:00，v2.5，每板块一次） ----
        if not baseline and gui4_window_open():
            for hy2, n2 in cnt.items():
                if n2 >= BOARD_LIMIT and hy2 not in self.gui4_sent:
                    stocks2 = [x for x in zt if x.get("hybk") == hy2]
                    leader2 = max(stocks2, key=lambda x: x.get("lbc", 1))
                    self.gui4_sent.add(hy2)
                    gui4_signal(hy2, n2, stocks2, zb, zbr, leader2)

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
    """轨3 v2.0·拐点接力：收集当日主板首板（主板60/00/003、价≤48）。
    硬红线：流通市值20-100亿、成交≥1亿、11:00前封死、非一字/无量秒板、板块≥2只、
           炸板≤2、近一年涨幅<400%、非黑名单；不看PE/净利润、板前不硬否决。
    涨停基因近半年≥3为硬红线，未达标但其余全过者单列 exempt（新主线首日硬催化/跨年妖胚豁免，人工确认）。
    打分：封板40 + 板块25 + 板前15（竞价20次日9:25补），满分80。
    返回 (cand, exempt, drop, stat)。"""
    zt = em_pool("zt", date)
    zb = em_pool("zb", date)
    zbr = len(zb) / (len(zt) + len(zb)) * 100 if (len(zt) + len(zb)) else 0
    board_cnt = Counter(x.get("hybk", "其他") for x in zt)
    cand, exempt, drop = [], [], []
    raws = [x for x in zt if x.get("lbc", 1) == 1 and x.get("c") not in BLACKLIST
            and is_main(x["c"]) and not is_st(x["n"])]
    qmap = tencent_quotes([x["c"] for x in raws])
    for x in raws:
        c = x["c"]
        price = x.get("p", 0) / 1000
        amt = x.get("amount", 0) / 1e8
        ltsz = x.get("ltsz", 0) / 1e8
        hy = x.get("hybk", "其他")
        tier = board_cnt.get(hy, 1)
        fbt = x.get("fbt", 0) or 0
        zbc = x.get("zbc", 0) or 0
        hs = x.get("hs", 0) or 0
        fr = fund_amt_ratio(x)
        yizi, yk = is_yizi(x)
        seal, sd = seal40(x)
        bscore = board25(tier)
        pe = (qmap.get(c) or {}).get("pe")
        row = {"x": x, "code": c, "name": x["n"], "hy": hy, "tier": tier,
               "price": price, "amt": amt, "ltsz": ltsz, "pe": pe,
               "lot": round(price * 100), "risk5": round(price * 100 * 0.05),
               "risk3": round(price * 100 * 0.03), "pre": None, "seal": seal,
               "sd": sd, "bscore": bscore, "pscore": 0, "gene": None, "yr": None,
               "fr": fr, "zbc": zbc, "fbt": fbt, "hs": hs, "yizi": yizi, "yk": yk,
               "downgrade": [], "comp": 0}

        # ---- 第一层：纯池子字段硬红线（0次日K请求，先砍掉绝大多数）----
        reasons = []
        if price <= 0 or price > PRICE_MAX:
            reasons.append("股价>%g" % PRICE_MAX)
        if ltsz < LTSZ_MIN or ltsz > LTSZ_MAX:
            reasons.append("流通%.0f亿超20-100" % ltsz)
        if amt < 1.0:
            reasons.append("成交<1亿")
        if fbt > FBT_DEADLINE:
            reasons.append("%s后封板(走附录A)" % fbt_str(fbt))
        if yizi:
            reasons.append(yk)
        if tier < 2:
            reasons.append("孤板(行业仅1)")
        if zbc >= 3:
            reasons.append("炸板%d次" % zbc)
        if reasons:
            row["reasons"] = reasons
            drop.append(row)
            continue

        # ---- 第二层：不复权日K（涨停基因 + 近一年涨幅），1次请求 ----
        time.sleep(0.15)
        gene = limit_up_gene(c, date)
        yr = year_rise(c, date)
        row["gene"], row["yr"] = gene, yr
        if gene["count"] is None:
            row["reasons"] = ["日K缺失(基因取不到)"]
            drop.append(row)
            continue
        if yr is not None and yr >= YEAR_RISE_MAX:
            row["reasons"] = ["年涨%.0f%%高标" % yr]
            drop.append(row)
            continue

        # ---- 第三层：前复权板前动作（仅前两层全过才拉，1次请求）----
        time.sleep(0.15)
        pre = board_pre_action(c, date)
        row["pre"] = pre
        pscore = round((pre["pre"] or 0) * 0.15, 1) if pre["pre"] is not None else 0
        row["pscore"] = pscore
        dg = []
        if zbc == 2:
            dg.append("炸2次降档")
        if fr < FUND_AMT_MIN:
            dg.append("封单%.0f%%<10%%" % fr)
        if pre["pre"] is not None and pre["pre"] < 45:
            dg.append("板前%d裸拉" % pre["pre"])
        row["downgrade"] = dg
        row["comp"] = round(seal + bscore + pscore, 1)
        if not gene["hit"]:
            row["reasons"] = ["基因%d次(需新主线首日/妖胚豁免)" % gene["count"]]
            exempt.append(row)
        else:
            cand.append(row)
    cand.sort(key=lambda r: -r["comp"])
    exempt.sort(key=lambda r: -r["comp"])
    return cand, exempt, drop, {"zt": len(zt), "zb": len(zb), "zbr": zbr}


def cmd_rank(push_too=True, date=None):
    date = date or last_trade_date()
    cand, exempt, drop, stat = collect_first_boards(date)
    relay = relay_sentiment(date)
    L = ["📋 一进二候选·拐点接力 v2.0（%s-%s 首板）" % (date[4:6], date[6:8]),
         "涨停%d 炸板率%.0f%% ｜ 合格%d只 基因豁免待判%d只"
         % (stat["zt"], stat["zbr"], len(cand), len(exempt))]
    if relay.get("n"):
        L.append("【接力情绪·%s首板%d只→%s】竞价红盘%.0f%% 收盘红盘%.0f%% 晋级%.0f%% 均高开%.1f%%"
                 % (relay["prev"], relay["n"], relay["today"][4:], relay["open_red"],
                    relay["now_red"], relay["promote"], relay["avg_open"]))
        L.append("→ %s（拐点/发酵还需结合当日炸板率、龙头是否闷杀）" % relay["tier"])
    elif relay.get("note"):
        L.append("【接力情绪】" + relay["note"])
    if cand:
        L.append("【✅可打·封板40+板块25+板前15，竞价20明早补，前5】")
        for i, r in enumerate(cand[:5], 1):
            p, sd = r["pre"], r["sd"]
            pv = p["pre"] if p["pre"] is not None else 0
            dg = ("｜降档:" + "/".join(r["downgrade"])) if r["downgrade"] else ""
            L.append(
                "%d %s%s｜%s梯队%d 流通%.0f亿\n"
                "  封板%d/40(首%s/炸%d/封单%.0f%%/换手%.0f%%) 板块%d/25 板前%d/15(%s) 基因%d 综合%d/80\n"
                "  收%.2f 成交%.1f亿 1手%d元 -5%%风险%d元 -3%%风险%d元%s\n"
                "  明早：高开2-5%%、竞价额/昨成交≥5%%、封单≥10%%、一字不排、>8%%不追"
                % (i, r["name"], r["code"], r["hy"], r["tier"], r["ltsz"],
                   r["seal"], sd["tstr"], r["zbc"], r["fr"], r["hs"],
                   r["bscore"], r["pscore"], p["grade"], r["gene"]["count"], r["comp"],
                   r["price"], r["amt"], r["lot"], r["risk5"], r["risk3"], dg))
    else:
        L.append("【✅无合格候选——空仓，不硬凑】")
    if exempt:
        L.append("【⚪仅差涨停基因·新主线首日硬催化/跨年妖胚可豁免(人工确认)】" + "、".join(
            "%s%s(%s·基因%d次/综合%d)" % (r["name"], r["code"], r["hy"],
                                         r["gene"]["count"], r["comp"])
            for r in exempt[:8]))
    if drop:
        L.append("【❌剔除】" + "；".join(
            "%s(%s)" % (r["name"], r["reasons"][0]) for r in drop[:15]))
    L.append("⏭明日9:25：先看接力红盘率四档定风险档；高开2-5%最佳、竞价额/昨成交额≥5%(≥8%极强)、"
             "封单≥10%、不做一字/无量秒板、>8%不追、打不到不等回踩。")
    msg = "\n".join(L)
    print(msg)
    if push_too:
        push("📋一进二候选v2.0（%s）" % date[4:], "\n".join(L[1:]))
    return cand, exempt, drop


def cmd_board(code):
    pre = board_pre_action(code, None)
    q = tencent_quotes([code]).get(code)
    print("==== %s 板前动作诊断（v2.0）====" % code)
    if q:
        print("名称%s 现价%.2f 涨幅%.1f%% PE=%s（v2.0不看PE）"
              % (q["name"], q["price"], q["pct"], q["pe"]))
    gene = limit_up_gene(code)
    print("涨停基因：近半年封板%d次 %s（%s）"
          % (gene["count"], "✅达标" if gene["hit"] else "✗<3（仅新主线首日/妖胚可豁免）",
             "、".join(gene["dates"][-6:])))
    yr = year_rise(code)
    if yr is not None:
        print("近一年涨幅：%.0f%% %s" % (yr, "✗≥400%高标，只看不碰" if yr >= YEAR_RISE_MAX else "✅"))
    x = next((z for z in em_pool("zt", last_trade_date()) if z["c"] == code), None)
    if x:
        yizi, yk = is_yizi(x)
        print("封板：%s首封 炸%d次 封单/成交%.0f%% 换手%.1f%% 流通%.1f亿 成交%.1f亿 %s"
              % (fbt_str(x.get("fbt")), x.get("zbc", 0), fund_amt_ratio(x),
                 x.get("hs", 0), x.get("ltsz", 0) / 1e8, x.get("amount", 0) / 1e8,
                 ("⛔" + yk) if yizi else "✅换手板"))
    if pre["pre"] is None:
        print("无法评分：", pre.get("note"))
        return
    print("板前总分 %d / %s（平台%d天，箱体%.1f%%，v2.0权重15%%不硬否决）"
          % (pre["pre"], pre["grade"], pre["days"], pre["box"] or 0))
    print("  控盘%d 试盘%d 洗盘%d" % (pre["ctrl"], pre["test"], pre["wash"]))
    print("  信号：%s" % ("、".join(pre["tags"]) or "无"))
    print("  否决/提示：%s" % (pre["veto"] or "无"))
    print("  判读：板前完整≥75优先；45-59一般；<45裸拉仅在有板块共振时保留、不单独否决。")


def cmd_relay(date=None):
    r = relay_sentiment(date)
    if not r.get("n"):
        print("接力情绪取不到：", r.get("note") or "未知")
        return
    print("==== 接力红盘率（%s 首板 → %s）====" % (r["prev"], r["today"]))
    print("昨日主板首板：%d只" % r["n"])
    print("今日竞价红盘率：%.0f%%（开盘高开占比）" % r["open_red"])
    print("今日收盘红盘率：%.0f%%" % r["now_red"])
    print("今日晋级率（再涨停）：%.0f%%" % r["promote"])
    print("平均竞价高开：%.2f%%" % r["avg_open"])
    print("建议档位：%s" % r["tier"])


def main():
    if "--test" in sys.argv:
        log("自测模式：拉取今日快照（不循环）")
        r = Radar()
        r.inited = False
        r.tick()
        return
    if "--rank" in sys.argv:
        log("收盘一进二候选排序（轨3 v2.0 拐点接力）")
        d = None
        if "--date" in sys.argv:
            d = sys.argv[sys.argv.index("--date") + 1]
        cmd_rank(push_too=True, date=d)
        return
    if "--relay" in sys.argv:
        d = None
        if "--date" in sys.argv:
            d = sys.argv[sys.argv.index("--date") + 1]
        log("接力红盘率（昨日首板→今日表现）")
        cmd_relay(d)
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
