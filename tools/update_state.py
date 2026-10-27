# -*- coding: utf-8 -*-
# 实时状态中枢·成交/状态写回工具（多窗口信息打通的唯一写入口）
#
# 作用：9个定时窗口 + 实盘对话统一读写同一份状态。
#   - 结构化源：state/state.json（小、固定、机器首选，云端任务可直接读，无历史锚点/中文markdown歧义）
#   - 人读镜像：live_state.md 顶部锚点块（<!-- LSTATE:HEAD_START --> ... <!-- LSTATE:HEAD_END -->），本脚本自动渲染
#   - 写后自动 git add/commit/push 到 GitHub（与 auction_snapshot.py 同机制，HTTP/1.1 兜底重试）
#
# 权限约定：
#   - 5个跑在本地 Mac 的任务（9:25/9:35/10:30/13:30/14:00）在「用户确认成交/状态」后调用本脚本写回；
#   - 4个云电脑任务（8:50/14:30/15:10/周末）只读不写。
#
# 用法示例：
#   买入：python3 update_state.py buy --code 601689 --name 拓普集团 --shares 100 --price 44.70 \
#            --risk 常规 --stop 43.40 --time 09:36 --note 轨1机器人中军 --cash "约7653元（待券商APP确认）"
#   卖出：python3 update_state.py sell --code 601689 --price 44.98
#   情绪：python3 update_state.py mood --text 退潮分化
#   资金：python3 update_state.py cash --text 7653
#   窗口结论：python3 update_state.py window --text "14:00 退潮无新开"
#   明日关注：python3 update_state.py tomorrow --text "东贝601956/秦安603758/大洋002249"
#   熔断：python3 update_state.py fuse --text "模式内连亏1，未触发"
#   测试：--dry 只打印不落盘；--no-push 落盘不推送
import os, re, sys, json, datetime, time, subprocess, argparse

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
STATE_DIR = os.path.join(REPO_ROOT, "state")
STATE_JSON = os.path.join(STATE_DIR, "state.json")
LIVE_MD = os.path.join(REPO_ROOT, "live_state.md")
ANCHOR_START = "<!-- LSTATE:HEAD_START"
ANCHOR_END = "<!-- LSTATE:HEAD_END -->"
GIT = "/usr/bin/git" if os.path.exists("/usr/bin/git") else "git"


def now():
    return datetime.datetime.now()


def load_state():
    if os.path.exists(STATE_JSON):
        try:
            with open(STATE_JSON, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print("⚠️ 读取 state.json 失败，按空状态重建:", e)
    return {
        "updated": "", "date": "", "version": "v3.1", "mood": "",
        "position": None, "cash": "", "fuse": "", "tomorrow": "",
        "windows": {},
    }


def roll_day(st):
    d = now().strftime("%Y-%m-%d")
    st["date"] = d
    st.setdefault("windows", {})
    st["windows"].setdefault(d, [])
    # 只保留最近3天窗口结论，避免无限增长
    keys = sorted(st["windows"].keys())[-3:]
    st["windows"] = {k: st["windows"][k] for k in keys}
    return st


def render_md(st):
    pos = st.get("position")
    if pos:
        pos_line = (f"{pos['name']} {pos['code']}｜{pos['shares']}股｜成本{pos['price']:.2f}"
                    f"｜风险档{pos.get('risk','')}｜止损{pos.get('stop','')}"
                    f"｜买入{pos.get('time','')}｜{pos.get('note','')}")
        pos_status = "持仓中"
    else:
        pos_line = "空仓"
        pos_status = "空仓"
    wins = st.get("windows", {}).get(st.get("date", ""), [])
    win_text = "；".join(wins) if wins else "（今日暂无）"
    L = [
        ANCHOR_START + " 本区块由 tools/update_state.py 自动维护，9个窗口统一读这里/state/state.json 获取最新持仓/资金/情绪/结论；勿手工编辑，改它用脚本；体系规则见下方v3.1块与《A股实战体系v3.1·五轨短线版.md》 -->",
        "## 🔴 实时状态头 · 唯一权威（机器优先读 state/state.json）",
        f"- 更新时间：{st.get('updated','')}",
        f"- 体系版本：{st.get('version','v3.1')}",
        f"- 情绪周期：{st.get('mood') or '（待更新）'}",
        f"- 持仓状态：{pos_status}",
        f"- 持仓明细：{pos_line}",
        f"- 可用资金：{st.get('cash') or '（待券商APP确认）'}",
        f"- 熔断状态：{st.get('fuse') or '（未记录）'}",
        f"- 今日窗口结论：{win_text}",
        f"- 明日关注：{st.get('tomorrow') or '（待更新）'}",
        ANCHOR_END,
    ]
    return "\n".join(L)


def write_live(st):
    block = render_md(st)
    with open(LIVE_MD, encoding="utf-8") as f:
        text = f.read()
    if ANCHOR_START in text and ANCHOR_END in text:
        text = re.sub(
            re.escape(ANCHOR_START) + r".*?" + re.escape(ANCHOR_END),
            lambda m: block, text, count=1, flags=re.S)
    else:
        # 插入到第一个一级标题行之后；没有标题则置顶
        lines = text.split("\n")
        for i, ln in enumerate(lines):
            if ln.startswith("# "):
                lines.insert(i + 1, "\n" + block + "\n")
                break
        else:
            lines = [block, ""] + lines
        text = "\n".join(lines)
    with open(LIVE_MD, "w", encoding="utf-8") as f:
        f.write(text)


def save(st, dry, no_push, msg):
    st["updated"] = now().strftime("%Y-%m-%d %H:%M")
    block = render_md(st)
    print("\n" + block + "\n")
    if dry:
        print("[--dry] 不落盘。")
        return
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_JSON, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    write_live(st)
    print("已写入 state/state.json 与 live_state.md 状态头")
    if not no_push:
        ok, out = git_push(msg)
        print(("✅ git推送：" if ok else "⚠️ git推送：") + out)
    else:
        print("[--no-push] 已跳过推送。")


def git_push(msg):
    env = dict(os.environ)
    env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:" + env.get("PATH", "")
    env["GIT_TERMINAL_PROMPT"] = "0"

    def run(args):
        r = subprocess.run([GIT] + args, cwd=REPO_ROOT, env=env,
                           capture_output=True, text=True, timeout=120)
        return r.returncode, (r.stdout + r.stderr).strip()

    try:
        run(["add", "state", "live_state.md"])
        rc, out = run(["commit", "-m", "状态中枢: " + msg])
        if rc != 0 and "nothing to commit" not in out:
            return False, "commit失败:" + out[-160:]
        for attempt in range(3):
            if attempt == 0:
                rc, out = run(["push", "origin", "main"])
            else:
                rc, out = run(["-c", "http.version=HTTP/1.1", "push", "origin", "main"])
            if rc == 0:
                return True, "已推送GitHub"
            time.sleep(3)
        return False, "push失败(已重试HTTP/1.1):" + out[-200:]
    except Exception as e:
        return False, "git异常:%s" % e


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["buy", "sell", "mood", "cash", "window",
                                       "tomorrow", "fuse", "version", "init"])
    ap.add_argument("--code"); ap.add_argument("--name"); ap.add_argument("--shares", type=int)
    ap.add_argument("--price", type=float); ap.add_argument("--risk"); ap.add_argument("--stop", type=float)
    ap.add_argument("--time"); ap.add_argument("--note")
    ap.add_argument("--text"); ap.add_argument("--cash")
    ap.add_argument("--dry", action="store_true"); ap.add_argument("--no-push", action="store_true")
    a = ap.parse_args()

    st = roll_day(load_state())
    t = now().strftime("%H:%M")
    msg = ""

    if a.action == "buy":
        if not (a.code and a.name and a.shares and a.price):
            print("❌ buy 需要 --code --name --shares --price"); sys.exit(1)
        st["position"] = {
            "name": a.name, "code": a.code, "shares": a.shares, "price": a.price,
            "risk": a.risk or "常规", "stop": a.stop if a.stop is not None else "",
            "time": a.time or t, "note": a.note or "",
        }
        if a.cash:
            st["cash"] = a.cash
        st["windows"][st["date"]].append(
            f"{t} 买入{a.name}({a.code}) {a.shares}股@{a.price} 风险档{a.risk or '常规'} 止损{a.stop}")
        msg = f"{st['date']} 买入{a.name}{a.code} {a.shares}股@{a.price}"

    elif a.action == "sell":
        pos = st.get("position")
        nm = pos["name"] if pos else (a.name or a.code)
        st["windows"][st["date"]].append(
            f"{t} 卖出{nm}({a.code or (pos and pos['code'])})"
            + (f"@{a.price}" if a.price is not None else ""))
        st["position"] = None
        if a.cash:
            st["cash"] = a.cash
        msg = f"{st['date']} 卖出{nm} 回到空仓"

    elif a.action in ("mood", "cash", "fuse", "tomorrow", "version"):
        key = {"mood": "mood", "cash": "cash", "fuse": "fuse",
               "tomorrow": "tomorrow", "version": "version"}[a.action]
        st[key] = a.text if a.text is not None else (a.cash or "")
        msg = f"更新{key}={st[key]}"

    elif a.action == "window":
        st["windows"][st["date"]].append(f"{t} {a.text or ''}")
        msg = f"追加{st['date']}窗口结论"

    elif a.action == "init":
        msg = "初始化/刷新实时状态头"

    save(st, a.dry, a.no_push, msg or "状态刷新")


if __name__ == "__main__":
    main()
