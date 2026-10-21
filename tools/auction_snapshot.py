# -*- coding: utf-8 -*-
# 9:25 集合竞价快照·独立落盘（补复盘缺口：9:15-9:25 撤单博弈盘后无法还原，至少把 9:25 结果永久留底）
# launchd 工作日 9:26 触发；手动测试：python3 auction_snapshot.py --dry（只打印不落盘），--no-push（落盘但不推送）
# 只读同目录 limit_up_radar.py 里的 WATCH 名单（中军/持仓/一进二），不修改、不依赖雷达运行
# 落盘后自动 git add/commit/push 到 GitHub（失败不阻断，Bark 告警）
import os, re, ast, sys, csv, datetime, urllib.request, time, urllib.parse, subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)  # 仓库根（tools 的上一级）
RADAR = os.path.join(HERE, "limit_up_radar.py")  # 权威雷达脚本，WATCH 名单来源
OUTDIR = os.path.join(REPO_ROOT, "auction")  # 快照写到仓库根 auction/，GitHub raw 与云端任务可读
BARK_KEY = "XDg67Er4obRebVkeuQFwq6"
IDX = ["sh000001", "sz399001", "sz399006"]
IDX_NAME = {"000001": "上证指数", "399001": "深成指", "399006": "创业板指"}


def http_get(url, gbk=False, tries=5):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                                        "Referer": "https://gu.qq.com/"})
            raw = urllib.request.urlopen(req, timeout=15).read()
            return raw.decode("gbk", "ignore") if gbk else raw.decode("utf-8", "ignore")
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise last


def load_watch():
    w = {"中军": [], "持仓": [], "一进二": []}
    try:
        src = open(RADAR, encoding="utf-8").read()
        m = re.search(r"WATCH\s*=\s*(\{[^}]*\})", src, re.S)
        if m:
            d = ast.literal_eval(m.group(1))
            for k in w:
                w[k] = list(d.get(k, []))
    except Exception as e:
        print("解析 WATCH 失败:", e)
    return w


def tx_quotes(codes):
    """codes: 6 位代码；返回 code->dict。指数与个股同接口，按是否 6 位/前缀区分。"""
    q = []
    for c in codes:
        if c.startswith(("sh", "sz")):
            q.append(c)
        else:
            q.append(("sh" if c[0] == "6" else "sz") + c)
    txt = http_get("https://qt.gtimg.cn/q=" + ",".join(q), gbk=True)
    out = {}
    for seg in txt.split(";"):
        if "~" not in seg:
            continue
        f = seg.split("~")
        try:
            code = f[2]
            prev = float(f[4]); op = float(f[5]); price = float(f[3])
            pct = (op - prev) / prev * 100 if prev else 0.0
            now_pct = (price - prev) / prev * 100 if prev else 0.0
            vol = float(f[6]) if f[6] else 0.0                       # 成交量(手)，9:25=竞价量
            amt = float(f[37]) if len(f) > 37 and f[37] else 0.0     # 成交额(万)
            turn = float(f[38]) if len(f) > 38 and f[38] else 0.0    # 换手率%
            b1v = float(f[10]) if len(f) > 10 and f[10] else 0.0     # 买一量(手)
            out[code] = {"name": f[1], "open": op, "prev": prev, "pct": pct,
                         "now_pct": now_pct, "vol": vol, "amt": amt, "turn": turn, "b1v": b1v}
        except Exception:
            continue
    return out


def verdict_track3(p):
    """轨3 一进二竞价判定（其余条件：竞价换手≥前日5%、板块≥2只高开，9:25 需结合 rank/盘前卡）"""
    if p >= 9.5:
        return "一字不排"
    if p > 7:
        return ">7%不排"
    if 3 <= p <= 7:
        return "可打区(待核换手/板块)"
    if 0 <= p < 3:
        return "高开不足·观察"
    return "低开·放弃"


def verdict_leader(p):
    if p >= 3:
        return "强·满足激活高开"
    if 0 <= p < 3:
        return "弱高开·盯能否放量"
    return "低开·倾向撤销"


def push(title, body):
    print("📱", title, "|", body.replace("\n", " "))
    if not BARK_KEY:
        return
    try:
        u = ("https://api.day.app/" + BARK_KEY + "/" + urllib.parse.quote(title, safe="") + "/"
             + urllib.parse.quote(body, safe="")
             + "?group=A%E8%82%A1%E9%9B%B7%E8%BE%BE&sound=alarm")
        http_get(u, tries=2)
    except Exception as e:
        print("Bark 失败:", e)


def git_push_snapshot(date):
    """落盘后自动提交并推送 auction/ 到 GitHub。任何失败都不阻断落盘，返回 (ok, msg)。
    依赖系统钥匙串里的 HTTPS 凭据（不依赖 gh token）；git 在 /usr/bin/git。"""
    git = "/usr/bin/git" if os.path.exists("/usr/bin/git") else "git"
    env = dict(os.environ)
    env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:" + env.get("PATH", "")
    env["GIT_TERMINAL_PROMPT"] = "0"  # 凭据缺失时直接失败，绝不挂起等输入

    def run(args):
        r = subprocess.run([git] + args, cwd=REPO_ROOT, env=env,
                           capture_output=True, text=True, timeout=120)
        return r.returncode, (r.stdout + r.stderr).strip()

    try:
        rc, out = run(["status", "--porcelain", "auction"])
        if rc != 0:
            return False, "git status失败:" + out[-120:]
        if not out.strip():
            return True, "无新增·跳过提交"
        rc, out = run(["add", "auction"])
        if rc != 0:
            return False, "git add失败:" + out[-120:]
        rc, out = run(["commit", "-m", f"auction: {date} 9:25集合竞价快照"])
        if rc != 0 and "nothing to commit" not in out:
            return False, "commit失败:" + out[-120:]
        rc, out = run(["push", "origin", "main"])
        if rc != 0:
            return False, "push失败:" + out[-160:]
        return True, "已推送GitHub"
    except Exception as e:
        return False, "git异常:%s" % e


def main():
    dry = "--dry" in sys.argv
    no_push = "--no-push" in sys.argv
    now = datetime.datetime.now()
    stamp = now.strftime("%Y-%m-%d %H:%M")
    date, hm = now.strftime("%Y%m%d"), now.strftime("%H%M")

    watch = load_watch()
    stocks = sorted({c for k in watch for c in watch[k]})
    q = tx_quotes(IDX + stocks)

    # 指数
    idx_lines = []
    for full in IDX:
        c = full[2:]
        d = q.get(c)
        if d:
            idx_lines.append(f"| {IDX_NAME.get(c, c)} | {d['open']:.2f} | {d['pct']:+.2f}% |")

    # 分组观察名单
    rows, csv_rows = [], []
    t3_can, t3_no, t3_skip = [], [], []
    lead = []
    for role in ("中军", "持仓", "一进二"):
        for code in watch[role]:
            d = q.get(code)
            if not d:
                rows.append(f"| {role} | (取数失败) | {code} | - | - | - | - | - | - |")
                continue
            if role == "一进二":
                v = verdict_track3(d["pct"])
                if "可打" in v:
                    t3_can.append(f"{d['name']}({code}) {d['pct']:+.1f}%")
                elif "不排" in v:
                    t3_no.append(f"{d['name']}({code}) {d['pct']:+.1f}%")
                else:
                    t3_skip.append(f"{d['name']}({code}) {d['pct']:+.1f}% {v}")
            else:
                v = verdict_leader(d["pct"])
                lead.append(f"{d['name']}({code}) {d['pct']:+.1f}% {v}")
            rows.append(f"| {role} | {d['name']} | {code} | {d['open']:.2f} | {d['pct']:+.2f}% | "
                        f"{int(d['vol'])} | {d['amt']:.0f} | {d['turn']:.2f}% | {v} |")
            csv_rows.append([stamp, role, d["name"], code, d["prev"], d["open"],
                             round(d["pct"], 2), int(d["vol"]), round(d["amt"], 1),
                             round(d["turn"], 2), v])

    md = [f"# {stamp} 集合竞价快照（雷达独立落盘）", "",
          "> 9:15-9:25 撤单过程免费接口不可还原；本快照保存 9:25 竞价结果（开盘价/竞价量/额/换手），供复盘第三步与 9:25 验证。",
          "", "## 指数竞价", "", "| 指数 | 竞价开盘 | 竞价涨跌 |", "|---|---|---|"] + idx_lines
    md += ["", "## 观察名单（角色：中军/持仓/一进二）", "",
           "| 角色 | 名称 | 代码 | 竞价开盘 | 竞价涨跌 | 竞价量(手) | 竞价额(万) | 竞价换手 | 判定 |",
           "|---|---|---|---|---|---|---|---|---|"] + rows
    md += ["", "## 轨3 一进二竞价结论", "",
           f"- **可打区(高开3-7%)**：{('、'.join(t3_can) or '无')}（仍需竞价换手≥前日5%、板块≥2只高开）",
           f"- **一字/>7% 不排**：{('、'.join(t3_no) or '无')}",
           f"- **高开不足/低开放弃**：{('、'.join(t3_skip) or '无')}",
           "", "## 轨1 中军/龙头/持仓", "",
           "\n".join(f"- {x}" for x in lead) if lead else "- 无",
           "", "数据：腾讯行情；精确大单以中信证券APP为准。仅为投研参考、不构成投资建议。"]
    text = "\n".join(md)
    print("\n" + text + "\n")

    if dry:
        print("[--dry] 不落盘。")
        return

    os.makedirs(OUTDIR, exist_ok=True)
    md_path = os.path.join(OUTDIR, f"{date}_{hm}_竞价快照.md")
    csv_path = os.path.join(OUTDIR, "auction_history.csv")
    open(md_path, "w", encoding="utf-8").write(text)
    new = not os.path.exists(csv_path)
    with open(csv_path, "a", newline="", encoding="utf-8-sig") as fp:
        w = csv.writer(fp)
        if new:
            w.writerow(["时间", "角色", "名称", "代码", "昨收", "竞价开盘", "竞价涨跌%",
                        "竞价量(手)", "竞价额(万)", "竞价换手%", "判定"])
        w.writerows(csv_rows)
    print("已落盘:", md_path)

    # 精简 Bark（只给结论，不重复云端 9:25 任务的长篇）
    body = ("指数 " + " ".join(x.split("|")[1].strip() + x.split("|")[2].strip()
                               for x in idx_lines) + "\n"
            "轨3可打: " + ("、".join(t3_can) or "无") + "\n"
            "不排/放弃: " + ("、".join(t3_no + t3_skip) or "无"))
    push("9:26 竞价快照已落盘", body)

    # 自动提交推送 GitHub（失败不阻断落盘，Bark 告知）
    if not no_push:
        ok, msg = git_push_snapshot(date)
        print(("✅ git推送：" if ok else "⚠️ git推送：") + msg)
        if not ok:
            push("⚠️竞价快照推送失败", msg)
    else:
        print("[--no-push] 已跳过 git 推送。")


if __name__ == "__main__":
    main()
