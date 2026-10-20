# -*- coding: utf-8 -*-
# 复盘数据一键拉取（五步复盘第三步·全天逐帧数据源）
# 用法: python3 pull_review_data.py [YYYYMMDD] [代码1 代码2 ...]
#   不带代码：默认拉当日涨停池“最高标3只+首板7只”
# 数据通道：腾讯分时(稳定,自算单分钟量与均价) + 东财涨停/炸板池 + 东财主力净占比
# 注意：特大/大/中/小单“精确净额”免费接口盘后不稳定，一律以中信证券APP为准，本脚本只给主力净占比参考
import urllib.request, json, time, sys, datetime
from collections import Counter

def get(url, ref='https://gu.qq.com/', gbk=False):
    last = None
    for i in range(5):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Referer': ref})
            raw = urllib.request.urlopen(req, timeout=15).read()
            return raw.decode('gbk', 'ignore') if gbk else json.loads(raw.decode('utf-8', 'ignore'))
        except Exception as e:
            last = e; time.sleep(1.5 * (i + 1))
    raise last

def pref(c):  return 'sh' if c[0] == '6' else 'sz'
def esecid(c): return ('1.' if c[0] == '6' else '0.') + c

def em_pool(kind, date):
    api = {'zt': 'getTopicZTPool', 'zb': 'getTopicZBPool', 'dt': 'getTopicDTPool'}[kind]
    u = (f'https://push2ex.eastmoney.com/{api}?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt'
         f'&Pageindex=0&pagesize=400&sort=fbt%3Aasc&date={date}&_={int(time.time()*1000)}')
    return (get(u, 'https://quote.eastmoney.com/').get('data') or {}).get('pool') or []

def snap(c):
    f = get(f'https://qt.gtimg.cn/q={pref(c)}{c}', gbk=True).split('~')
    return {'name': f[1], 'price': float(f[3]), 'pre': float(f[4]), 'open': float(f[5]),
            'high': float(f[33]), 'low': float(f[34]), 'amt_wan': float(f[37]), 'turn': f[38]}

def minute_tx(c):
    j = get(f'https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={pref(c)}{c}')
    arr = j['data'][pref(c) + c]['data']['data']
    raw = []
    for s in arr:
        p = s.split()
        if len(p) >= 4 and len(p[0]) == 4 and '0930' <= p[0] <= '1500':
            raw.append((p[0], float(p[1]), float(p[2]), float(p[3])))  # 时间 价 累计量(手) 累计额(元)
    out = []
    for i, (t, pr, cv, ca) in enumerate(raw):
        pv = cv - (raw[i-1][2] if i else 0)
        pa = ca - (raw[i-1][3] if i else 0)
        avg = ca / (cv * 100) if cv else pr
        out.append((t, pr, pv, pa, avg))  # 时间 价 单分钟量 单分钟额 均价
    return out

def minute_em(c):  # 东财兜底：逗号分隔 时间,开,收,高,低,单分钟量,单分钟额,均价
    u = (f'https://push2his.eastmoney.com/api/qt/stock/trends2/get?secid={esecid(c)}'
         '&fields1=f1,f2,f3,f4,f5,f6,f7,f8&fields2=f51,f52,f53,f54,f55,f56,f57,f58&iscr=0&ndays=1')
    tr = get(u, 'https://quote.eastmoney.com/')['data']['trends']
    out = []
    for s in tr:
        p = s.split(','); t = p[0][-5:].replace(':', '')
        if '0930' <= t <= '1500':
            out.append((t, float(p[2]), float(p[5]), float(p[6]), float(p[8])))
    return out

def minute(c):
    try:
        return minute_tx(c)
    except Exception:
        return minute_em(c)

def main_pct(c):
    try:
        d = (get(f'https://push2.eastmoney.com/api/qt/stock/get?secid={esecid(c)}&fields=f184',
                 'https://quote.eastmoney.com/').get('data') or {})
        return d.get('f184')
    except Exception:
        return None  # 盘后限流时容错；精确大单净额一律以中信证券APP为准

def divergences(m, lp):
    marks = []
    for i in range(1, len(m)):
        t, pr, pv, pa, avg = m[i]; pc = m[i-1][1]
        chg = (pr - pc) / pc * 100
        ma5 = sum(x[3] for x in m[max(0, i-5):i]) / max(1, min(5, i))
        if pa > ma5 * 2.5 and i > 3:
            marks.append(f"{t} 放量{pa/ma5:.1f}倍 {pr:.2f}{'站上' if pr > avg else '跌破'}均价")
        elif abs(chg) >= 0.7:
            marks.append(f"{t} {'急拉' if chg > 0 else '急杀'}{chg:+.1f}%")
    touch = [x for x in m if x[1] >= lp - 0.001]
    if touch and m[-1][1] < lp - 0.01:
        marks.append(f"{touch[-1][0]} 触涨停{lp:.2f}后未封住(收{m[-1][1]:.2f})")
    # 去重相邻同类
    return marks[:12]

def main():
    args = sys.argv[1:]
    date = args[0] if args and args[0].isdigit() else datetime.datetime.now().strftime('%Y%m%d')
    codes = [a for a in (args[1:] if args and args[0].isdigit() else args) if not a.isdigit() or len(a) == 6]
    zt, zb, dt = em_pool('zt', date), em_pool('zb', date), em_pool('dt', date)
    zbr = len(zb) / (len(zt) + len(zb)) * 100 if (len(zt) + len(zb)) else 0
    maxh = max([(x.get('zttj') or {}).get('days', 1) for x in zt] + [1])
    bk = Counter(x.get('hybk', '其他') for x in zt).most_common(5)
    print(f"===== {date} 情绪快照 =====")
    print(f"涨停{len(zt)} 跌停{len(dt)} 炸板{len(zb)} 炸板率{zbr:.1f}% 最高{maxh}连板")
    print("板块TOP5:", "、".join(f"{b}{n}" for b, n in bk))
    if not codes:
        lead = sorted(zt, key=lambda x: -(x.get('zttj') or {}).get('days', 1))[:3]
        fb = [x for x in zt if (x.get('zttj') or {}).get('days', 1) == 1][:7]
        codes = [x['c'] for x in lead + fb]
    print(f"\n===== 核心候选全天逐帧（{len(codes)}只；9:30-15:00）=====")
    for c in codes[:12]:
        try:
            s = snap(c); m = minute(c); pct = main_pct(c)
            lp = round(s['pre'] * 1.1 + 1e-6, 2)
            chg = (s['price'] - s['pre']) / s['pre'] * 100
            dg = divergences(m, lp)
            print(f"\n● {s['name']} {c} 收{s['price']:.2f} {chg:+.2f}% 开{s['open']:.2f} 高{s['high']:.2f} 低{s['low']:.2f} 换手{s['turn']}% 额{s['amt_wan']/1e4:.2f}亿")
            print(f"  主力净占比 {f'{pct}%' if pct is not None else '盘后未取到（以中信证券APP为准）'}；精确特大/大/中/小单净额以中信APP为准")
            print("  分歧点:", " | ".join(dg) if dg else "无显著异动")
        except Exception as e:
            print(f"● {c} FAIL {e!r}")

if __name__ == '__main__':
    main()
