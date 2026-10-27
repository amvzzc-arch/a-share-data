# 集合竞价快照目录（auction/）

由 `tools/auction_snapshot.py` 在每个工作日 9:26（launchd 定时 `com.amv.ashare.auction-snapshot`）自动生成并提交推送到 GitHub。

> 时序：快照 9:26 生成并 push，云端「9:25 竞价警戒」任务改到 **9:27 触发**读取，避免抢跑 404；文件名统一 ASCII（见下），raw 读取带 `?v=YYYYMMDD` 时间戳。

## 用途
9:15–9:25 集合竞价的撤单博弈过程盘后任何免费接口都无法还原，本目录至少把 **9:25 竞价结果**（开盘价/竞价量/竞价额/换手）永久留底，供：
- 9:25 竞价验证（轨1是否激活、轨3二板是否在可打区）；
- 收盘复盘第三步、次日 8:50 盘前卡回查；
- 云端定时任务通过 GitHub raw 直接读取（公开文件免登录）。

## 文件
- `auction_YYYY-MM-DD_HHMM.md`：当日快照（**ASCII 文件名**，如 `auction_2026-10-27_0926.md`；指数 + 观察名单中军/持仓/一进二 + 轨3竞价结论；历史中文名快照已统一改为 ASCII）。
- 云端读取（防缓存）：`https://raw.githubusercontent.com/amvzzc-arch/a-share-data/main/auction/auction_YYYY-MM-DD_0926.md?v=YYYYMMDD`；jsDelivr 中文路径会 404，故禁止中文名。
- `auction_history.csv`：历史竞价明细（追加写，供统计）。

## 观察名单来源
脚本只读 `tools/limit_up_radar.py` 顶部的 `WATCH` 字典（中军/持仓/一进二），每日 8:50 作战计划后更新；脚本不修改雷达、不依赖雷达运行。

## 数据
腾讯行情 `qt.gtimg.cn`；精确大单以券商 APP 为准。仅为投研参考、不构成投资建议。
