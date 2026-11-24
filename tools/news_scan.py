#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A股消息扫描工具（DSH 协作专用）
- 数据源：新浪财经 7x24 快讯（无需签名，稳定）
- 用法：
    python3 news_scan.py                    # 输出最近 50 条快讯
    python3 news_scan.py "新华传媒,监管,异动"  # 关键词过滤（逗号分隔）
    python3 news_scan.py "" 100             # 指定条数
"""
import sys, json, urllib.request

def fetch_news(page_size=80):
    url = (f'https://zhibo.sina.com.cn/api/zhibo/feed?page=1&page_size={page_size}'
           f'&zhibo_id=152&tag_id=0&dire=f&dpc=1&type=0')
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
        'Referer': 'https://finance.sina.com.cn/7x24/'})
    r = urllib.request.urlopen(req, timeout=20).read().decode('utf-8', 'ignore')
    d = json.loads(r)
    feed = d.get('result', {}).get('data', {}).get('feed', {})
    return feed.get('list', [])

def main():
    keywords = []
    size = 80
    if len(sys.argv) > 1 and sys.argv[1].strip():
        keywords = [k.strip() for k in sys.argv[1].split(',') if k.strip()]
    if len(sys.argv) > 2:
        size = int(sys.argv[2])
    items = fetch_news(size)
    print(f'=== 新浪 7x24 快讯（最近 {len(items)} 条）===')
    for it in items:
        text = (it.get('rich_text') or it.get('content') or '').strip()
        t = (it.get('create_time') or '')[:16]
        if keywords:
            if not any(k in text for k in keywords):
                continue
        print(f'- {t} {text[:150]}')

if __name__ == '__main__':
    main()
