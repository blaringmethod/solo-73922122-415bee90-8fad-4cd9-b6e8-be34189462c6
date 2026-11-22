#!/usr/bin/env python3
"""End-to-end demo of batch splitting against a running server.

Usage: python3 demo/demo_split.py [base_url] [output_dir]

Speaks only HTTP, like demo/demo.py. Scenario:

1. Import two batches B01/B02 and one combined sheet S1 covering both.
2. Check both batches -> the sheet total is reported.
3. Split B01 into B01A/B01B (separate re-scans); the old sheet stays usable
   and the page shows original code next to the batches it now covers.
4. Check all leaves -> same total; check one child alone -> no number.
5. Revise one child -> the old sheet goes stale and the whole check fails.
6. Full re-import reusing batch codes -> lineage resets, checks work again.
"""

from __future__ import annotations

import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1:8000"
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "demo/out-split")

TRANSCRIPT: list[str] = []


def say(message: str = "") -> None:
    print(message)
    TRANSCRIPT.append(message)


class _StatusParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.status = None
        self.total = None
        self.base_id = None
        self.coverage_rows: list[str] = []
        self._capture = None
        self._in_base = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("id") == "result":
            self.status = attrs.get("data-status")
        if attrs.get("id") == "page-total":
            self._capture = "total"
        if tag == "input" and attrs.get("name") == "base_id":
            self.base_id = attrs.get("value")
        if "coverage" in attrs.get("class", ""):
            self._capture = "coverage"

    def handle_endtag(self, tag):
        if self._capture == "total" and tag == "p":
            self._capture = None
        if self._capture == "coverage" and tag == "div":
            self._capture = None

    def handle_data(self, data):
        text = data.strip()
        if not text:
            return
        if self._capture == "total":
            match = re.search(r"\d+", text)
            if match:
                self.total = int(match.group())
        elif self._capture == "coverage":
            self.coverage_rows.append(text)


def request(path: str, data: dict | None = None):
    url = BASE + path
    body = None
    headers = {}
    if data is not None:
        body = urllib.parse.urlencode(data, doseq=True).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(
        url, data=body, headers=headers,
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, response.read().decode(), response.geturl()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(), url


def parse(html: str) -> _StatusParser:
    parser = _StatusParser()
    parser.feed(html)
    return parser


def flash_messages(html: str) -> list[str]:
    return [
        re.sub(r"<[^>]+>", "", m).strip()
        for m in re.findall(r'<li class="flash [^"]+">(.*?)</li>', html, re.S)
    ]


def save(name: str, html: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(html, encoding="utf-8")


def check(label: str, batches: list[str]):
    query = urllib.parse.urlencode(
        [("check", "1")] + [("batch", c) for c in batches]
    )
    status, html, _ = request("/?" + query)
    result = parse(html)
    say(
        f"- 核对 {'、'.join(batches)} → HTTP {status}，状态 `{result.status}`"
        + (f"，报数 {result.total} 页" if result.total is not None else "")
    )
    return result, html


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    say("# 复扫页数核对 · 拆批联机演示记录")
    say()
    say(f"服务地址：`{BASE}`")
    say()

    status, _html, _ = request("/healthz")
    say(f"- 健康检查 `GET /healthz` → HTTP {status}")
    if status != 200:
        say("\n服务未就绪，演示终止。")
        return 1

    say("")
    say("## 第 1 步：整套导入（B01、B02 合在一张汇总单 S1 上）")
    say()
    batches = "B01 r2\nB02 r2\n"
    sheets = "S1 100\nB01 r2\nB02 r2\n"
    status, html, url = request(
        "/import", {"batches": batches, "sheets": sheets}
    )
    save("01-import.html", html)
    say(f"- `POST /import` → HTTP {status}，落点 `{url}`")
    for message in flash_messages(html):
        say(f"- 页面提示：{message}")

    say("")
    say("## 第 2 步：拆批前核对 B01+B02")
    result, html = check("before", ["B01", "B02"])
    save("02-check-before-split.html", html)
    say("- 期望：S1 整单覆盖两批，报 100 页。")

    say("")
    say("## 第 3 步：把 B01 拆成 B01A、B01B（分开补扫，沿用 r2）")
    base_id = parse(html).base_id
    if not base_id:
        say("- 未能在页面找到当前提交编号，演示终止。")
        return 2
    status, html, url = request(
        "/split",
        {"base_id": base_id, "code": "B01", "child_a": "B01A", "child_b": "B01B"},
    )
    save("03-split.html", html)
    say(f"- `POST /split` → HTTP {status}，落点 `{url}`")
    for message in flash_messages(html):
        say(f"- 页面提示：{message}")
    parsed = parse(html)
    say(f"- 页面并排显示原批次与当前覆盖：{' '.join(parsed.coverage_rows)}")
    say("- 期望：S1 行显示 B01 → B01A、B01B，状态仍为“可用”。")

    say("")
    say("## 第 4 步：拆批后的核对规则")
    result, html = check("all leaves", ["B01A", "B01B", "B02"])
    save("04-check-all-leaves.html", html)
    ok_all = result.status == "ok" and result.total == 100
    say("- 期望：三个现行批次一起核对仍报 100 页（旧单随拆批展开）。")

    result, html = check("one child", ["B01A"])
    save("05-check-one-child.html", html)
    ok_one = result.status == "none"
    say("- 期望：只选 B01A 时无法出数——整单合计不能给单个子批报数。")

    say("")
    say("## 第 5 步：只给 B01A 更版到 r3，旧单应失效")
    base_id = parse(html).base_id
    status, html, url = request(
        "/revision", {"base_id": base_id, "code": "B01A", "revision": "3"}
    )
    save("06-child-revision.html", html)
    say(f"- `POST /revision` → HTTP {status}，落点 `{url}`")
    for message in flash_messages(html):
        say(f"- 页面提示：{message}")
    stale_lines = re.findall(r"<li>([^<]*不一致[^<]*)</li>", html)
    for line in stale_lines:
        say(f"- 不采用理由：{line.strip()}")

    result, html = check("after revision", ["B01A", "B01B", "B02"])
    save("07-check-after-revision.html", html)
    ok_stale = result.status == "none"
    say("- 期望：S1 的后代 B01A 已为 r3，与单上 r2 不吻合，整张单不采用、无法出数。")

    say("")
    say("## 第 6 步：整套重导（即使重用批次号 B01、B01A），按新清单核对")
    status, html, url = request(
        "/import",
        {
            "batches": "B01 r3\nB01A r3\nB02 r2\n",
            "sheets": "T1 120\nB01 r3\nB01A r3\n\nT2 30\nB02 r2\n",
        },
    )
    save("08-reimport.html", html)
    say(f"- `POST /import` → HTTP {status}，落点 `{url}`")
    for message in flash_messages(html):
        say(f"- 页面提示：{message}")
    result, html = check("after reimport", ["B01", "B01A", "B02"])
    save("09-check-after-reimport.html", html)
    ok_reimport = result.status == "ok" and result.total == 150
    say("- 期望：谱系随整套导入重置，B01/B01A 按新清单作为独立批次，T1+T2 报 150 页。")

    say("")
    say("## 结论")
    say()
    outcomes = {
        "拆批后整单仍可核对": ok_all,
        "单选子批不能报数": ok_one,
        "子批更版使旧单失效": ok_stale,
        "整套重导后恢复核对": ok_reimport,
    }
    for name, passed in outcomes.items():
        say(f"- {'✅' if passed else '❌'} {name}")
    say(f"- 原始响应保存在 `{OUT}/`。")

    (OUT / "transcript.md").write_text("\n".join(TRANSCRIPT) + "\n", encoding="utf-8")
    return 0 if all(outcomes.values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
