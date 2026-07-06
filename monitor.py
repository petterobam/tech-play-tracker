#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
科技比赛 / 补贴专项 · 监控器
==================================================
抓取 RSS 与政府通知页 → 关键词匹配 → 哈希去重 → 多渠道推送。

用法:
    python monitor.py --config sources.yaml              # 正式运行（命中即推送）
    python monitor.py --config sources.yaml --dry-run    # 只打印，不推送/不入库
    python monitor.py --config sources.yaml --show-all   # 打印抓到的所有条目（调试用）
    python monitor.py --config sources.yaml --source 科技部·科技管理信息系统  # 只跑指定源

环境变量（覆盖 sources.yaml 的 notify 配置，推荐 CI 中用）:
    FEISHU_WEBHOOK / DINGTALK_WEBHOOK / DINGTALK_SECRET
    WECOM_WEBHOOK / SERVERCHAN_KEY
    SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASS / MAIL_FROM / MAIL_TO
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import os
import re
import sqlite3
import sys
import time
import urllib.parse
from dataclasses import dataclass
from typing import Callable

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup

UA = "Mozilla/5.0 (compatible; policy-monitor/1.0)"
TIMEOUT = 20


@dataclass
class Item:
    source: str
    title: str
    url: str
    published: str = ""

    @property
    def key(self) -> str:
        return hashlib.md5(f"{self.title}|{self.url}".encode("utf-8")).hexdigest()


# ----------------------------- 配置 -----------------------------
def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def build_matcher(keywords: list[str]) -> Callable[[str], bool]:
    """编译关键词为正则，返回匹配函数（命中任一即 True）。"""
    pats = [re.compile(k, re.IGNORECASE) for k in keywords or []]

    def _match(text: str) -> bool:
        return any(p.search(text or "") for p in pats)

    return _match


# ----------------------------- 抓取 -----------------------------
def fetch_rss(name: str, url: str, limit: int) -> list[Item]:
    feed = feedparser.parse(url, request_headers={"User-Agent": UA})
    items = []
    for e in feed.entries[:limit]:
        items.append(Item(
            source=name,
            title=(getattr(e, "title", "") or "").strip(),
            url=(getattr(e, "link", "") or "").strip(),
            published=getattr(e, "published", ""),
        ))
    return items


def fetch_page(name: str, url: str, limit: int,
               item_selector: str | None = None, link_selector: str = "a") -> list[Item]:
    """通用通知页抓取：提取 <a> 文字与链接。item_selector 可限定区块。"""
    resp = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
    resp.encoding = resp.apparent_encoding or resp.encoding
    soup = BeautifulSoup(resp.text, "lxml")
    root = soup.select_one(item_selector) if item_selector else soup
    if root is None:
        root = soup
    items, seen = [], set()
    for a in root.select(link_selector)[:200]:
        title = a.get_text(strip=True)
        href = a.get("href", "").strip()
        if len(title) < 6 or not href:
            continue
        href = urllib.parse.urljoin(url, href)
        if href in seen:
            continue
        seen.add(href)
        items.append(Item(source=name, title=title, url=href))
    return items[:limit]


# ----------------------------- 去重 -----------------------------
def init_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE IF NOT EXISTS seen (key TEXT PRIMARY KEY, ts TEXT)")
    return conn


def filter_new(items: list[Item], conn: sqlite3.Connection) -> list[Item]:
    keys = [i.key for i in items]
    if not keys:
        return []
    q = f"SELECT key FROM seen WHERE key IN ({','.join('?' * len(keys))})"
    seen = {r[0] for r in conn.execute(q, keys)}
    return [i for i in items if i.key not in seen]


def mark_seen(items: list[Item], conn: sqlite3.Connection) -> None:
    conn.executemany(
        "INSERT OR IGNORE INTO seen(key, ts) VALUES(?, ?)",
        [(i.key, i.published or "") for i in items],
    )
    conn.commit()


# ----------------------------- 推送 -----------------------------
def _post_json(url: str, payload: dict) -> None:
    requests.post(url, json=payload, timeout=TIMEOUT)


def _feishu(text: str, webhook: str) -> None:
    _post_json(webhook, {"msg_type": "text", "content": {"text": text}})


def _dingtalk(text: str, webhook: str, secret: str = "") -> None:
    url = webhook
    if secret:
        ts = str(round(time.time() * 1000))
        sign_str = f"{ts}\n{secret}"
        sign_code = hmac.new(secret.encode("utf-8"), sign_str.encode("utf-8"), digestmod=hashlib.sha256).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(sign_code))
        url = f"{webhook}&timestamp={ts}&sign={sign}"
    _post_json(url, {"msgtype": "text", "text": {"content": text}})


def _wecom(text: str, webhook: str) -> None:
    _post_json(webhook, {"msgtype": "text", "text": {"content": text}})


def _serverchan(title: str, desp: str, key: str) -> None:
    requests.post(f"https://sctapi.ftqq.com/{key}.send",
                  data={"title": title[:32], "desp": desp}, timeout=TIMEOUT)


def _email(subject: str, body: str, cfg: dict) -> None:
    import smtplib
    from email.mime.text import MIMEText
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = subject, cfg["from"], cfg["to"]
    with smtplib.SMTP_SSL(cfg["smtp_host"], int(cfg["smtp_port"])) as s:
        s.login(cfg["smtp_user"], cfg["smtp_pass"])
        s.sendmail(cfg["from"], cfg["to"].split(","), msg.as_string())


def notify_all(cfg: dict, hits: list[Item]) -> None:
    title = f"🔔 政策/赛事监控：命中 {len(hits)} 条新通知"
    lines = [title, ""]
    for i, h in enumerate(hits, 1):
        lines += [f"{i}. [{h.source}] {h.title}", f"   {h.url}"]
    text = "\n".join(lines)

    notify = cfg.get("notify", {}) or {}
    env = os.environ

    wh = env.get("FEISHU_WEBHOOK") or (notify.get("feishu") or {}).get("webhook")
    if wh:
        _feishu(text, wh)
    dw = env.get("DINGTALK_WEBHOOK") or (notify.get("dingtalk") or {}).get("webhook")
    if dw:
        _dingtalk(text, dw, env.get("DINGTALK_SECRET") or (notify.get("dingtalk") or {}).get("secret", ""))
    ww = env.get("WECOM_WEBHOOK") or (notify.get("wecom") or {}).get("webhook")
    if ww:
        _wecom(text, ww)
    sk = env.get("SERVERCHAN_KEY") or (notify.get("serverchan") or {}).get("key")
    if sk:
        _serverchan(title, text, sk)
    em = notify.get("email") or {}
    if env.get("SMTP_HOST") or em.get("smtp_host"):
        _email(title, text, {
            "smtp_host": env.get("SMTP_HOST", em.get("smtp_host")),
            "smtp_port": env.get("SMTP_PORT", em.get("smtp_port", 465)),
            "smtp_user": env.get("SMTP_USER", em.get("smtp_user")),
            "smtp_pass": env.get("SMTP_PASS", em.get("smtp_pass")),
            "from": env.get("MAIL_FROM", em.get("from")),
            "to": env.get("MAIL_TO", em.get("to")),
        })


# ----------------------------- 编排 -----------------------------
def collect(cfg: dict, only: str | None = None) -> list[Item]:
    fetch = cfg.get("fetch", {}) or {}
    limit = int(fetch.get("per_source_limit", 30))
    items: list[Item] = []

    for src in cfg.get("rss", []) or []:
        if only and src.get("name") != only:
            continue
        try:
            items += fetch_rss(src["name"], src["url"], limit)
        except Exception as e:
            print(f"[warn] rss {src.get('name')}: {e}", file=sys.stderr)

    for src in cfg.get("pages", []) or []:
        if only and src.get("name") != only:
            continue
        try:
            items += fetch_page(
                src["name"], src["url"], limit,
                src.get("item_selector"), src.get("link_selector", "a"),
            )
        except Exception as e:
            print(f"[warn] page {src.get('name')}: {e}", file=sys.stderr)
    return items


def main() -> None:
    ap = argparse.ArgumentParser(description="科技比赛/补贴专项监控器")
    ap.add_argument("--config", default="sources.yaml")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不推送/不入库")
    ap.add_argument("--source", default=None, help="只跑指定名称的源")
    ap.add_argument("--show-all", action="store_true", help="打印抓取到的所有条目（调试）")
    args = ap.parse_args()

    cfg = load_config(args.config)
    matcher = build_matcher(cfg.get("keywords", []))

    items = collect(cfg, only=args.source)
    print(f"[info] 共抓取 {len(items)} 条")

    if args.show_all:
        for i in items:
            print(f"  - [{i.source}] {i.title}")
        return

    matched = [i for i in items if matcher(i.title)]
    print(f"[info] 关键词命中 {len(matched)} 条")
    if not matched:
        print("[info] 无命中，结束。")
        return

    conn = init_db(cfg.get("db", "seen.db"))
    new = filter_new(matched, conn)
    print(f"[info] 去重后新增 {len(new)} 条")
    if not new:
        print("[info] 全部已推送过，结束。")
        return

    for i in new:
        print(f"  ✅ [{i.source}] {i.title}\n     {i.url}")

    if args.dry_run:
        print("[info] dry-run，不推送、不入库。")
        return

    try:
        notify_all(cfg, new)
        mark_seen(new, conn)
        print(f"[info] 已推送 {len(new)} 条并入库。")
    except Exception as e:
        print(f"[error] 推送失败：{e}", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
