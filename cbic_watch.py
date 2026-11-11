#!/usr/bin/env python3
"""
CBIC Tax Information portal watcher.
Finds new documents on https://taxinformation.cbic.gov.in/, downloads the PDF,
and sends it to a Make.com webhook (which summarises with OpenAI and emails).
Each document's CBIC ID is remembered in state/seen.json, so nothing is sent twice.
The first run only records what already exists and sends nothing.
"""
import argparse
import asyncio
import base64
import io
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from playwright.async_api import async_playwright
from pypdf import PdfReader

BASE = os.getenv("BASE_URL", "https://taxinformation.cbic.gov.in/")
STATE_FILE = Path(os.getenv("STATE_FILE", "state/seen.json"))
WATCH_URLS = [u.strip() for u in (os.getenv("WATCH_URLS") or BASE).split(",") if u.strip()]
# Empty = all categories. Example: "notifications,circulars,instructions,orders"
CATEGORIES = {c.strip().lower() for c in os.getenv("CATEGORIES", "").split(",") if c.strip()}
MAX_PER_RUN = int(os.getenv("MAX_PER_RUN", "15"))
MAX_PDF_MB = float(os.getenv("MAX_PDF_MB", "4"))
MAX_TEXT_CHARS = int(os.getenv("MAX_TEXT_CHARS", "40000"))
WEBHOOK = os.getenv("MAKE_WEBHOOK_URL", "")
STALE_DAYS = int(os.getenv("STALE_DAYS", "7"))
NAV_TIMEOUT = 60_000

LINK_RE = re.compile(r"/view-pdf/(\d+)/ENG/([A-Za-z_-]+)", re.I)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return None


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    tmp.replace(STATE_FILE)


async def settle(page):
    try:
        await page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT)
    except Exception:
        pass
    for _ in range(4):
        await page.mouse.wheel(0, 4000)
        await page.wait_for_timeout(800)
    await page.wait_for_timeout(1500)


async def collect_items(context, discover=False):
    items = {}
    page = await context.new_page()
    api_log = []

    if discover:
        Path("discover").mkdir(exist_ok=True)

        async def log_response(resp):
            ct = resp.headers.get("content-type", "")
            if resp.request.resource_type in ("xhr", "fetch"):
                entry = {"url": resp.url, "status": resp.status, "content_type": ct}
                if "json" in ct:
                    try:
                        entry["sample"] = (await resp.text())[:3000]
                    except Exception:
                        pass
                api_log.append(entry)
        page.on("response", lambda r: asyncio.ensure_future(log_response(r)))

    for n, url in enumerate(WATCH_URLS):
        print(f"[scan] {url}")
        await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
        await settle(page)

        anchors = await page.evaluate("""() =>
            Array.from(document.querySelectorAll('a[href*="view-pdf"]')).map(a => {
                const row = a.closest('tr, li');
                const t = (a.innerText || a.title || '').trim();
                return {href: a.href, text: (t.length > 8 ? t : (row ? row.innerText : t)).trim()};
            })""")
        html = await page.content()

        for a in anchors:
            m = LINK_RE.search(a["href"])
            if m:
                add_item(items, m, a["text"])
        for m in LINK_RE.finditer(html):
            add_item(items, m, "")

        if discover:
            await page.screenshot(path=f"discover/page{n}.png", full_page=True)
            Path(f"discover/page{n}.html").write_text(html, encoding="utf-8")

    if discover:
        await page.wait_for_timeout(1000)
        Path("discover/api_calls.json").write_text(json.dumps(api_log, indent=1), encoding="utf-8")
        Path("discover/links_found.json").write_text(json.dumps(items, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"[discover] {len(api_log)} API calls, {len(items)} document links -> ./discover/")

    await page.close()
    return items


def add_item(items, m, text):
    doc_id, cat = m.group(1), m.group(2)
    if CATEGORIES and cat.lower() not in CATEGORIES:
        return
    key = f"{cat.lower()}/{doc_id}"
    title = " ".join(text.split())[:500]
    if key in items:
        if title and not items[key]["title"]:
            items[key]["title"] = title
        return
    items[key] = {"id": doc_id, "category": cat, "title": title,
                  "url": urljoin(BASE, f"view-pdf/{doc_id}/ENG/{cat}")}


async def fetch_pdf(context, item):
    page = await context.new_page()
    seen_urls = []

    def on_response(resp):
        ct = resp.headers.get("content-type", "").lower()
        if "pdf" in ct or "octet-stream" in ct or resp.url.lower().endswith(".pdf"):
            seen_urls.append(resp.url)
    page.on("response", on_response)

    try:
        await page.goto(item["url"], wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
    except Exception as e:
        print(f"  [warn] goto: {e}")
    await settle(page)

    try:
        embedded = await page.evaluate("""() =>
            Array.from(document.querySelectorAll('iframe[src], embed[src], object[data]'))
                 .map(e => e.src || e.data)""")
        seen_urls.extend(u for u in embedded if u)
    except Exception:
        pass

    data = None
    for u in dict.fromkeys(seen_urls):
        try:
            if u.startswith("blob:"):
                b64 = await page.evaluate("""async (u) => {
                    const b = await (await fetch(u)).arrayBuffer();
                    let s = ''; const a = new Uint8Array(b);
                    for (let i = 0; i < a.length; i++) s += String.fromCharCode(a[i]);
                    return btoa(s); }""", u)
                body = base64.b64decode(b64)
            else:
                resp = await context.request.get(u, timeout=NAV_TIMEOUT)
                body = await resp.body()
            if body[:5] == b"%PDF-":
                data = body
                break
        except Exception as e:
            print(f"  [warn] pdf fetch {u}: {e}")

    await page.close()
    return data


def pdf_text(data: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(data))
        text = "\n".join((p.extract_text() or "") for p in reader.pages)
        return re.sub(r"[ \t]+", " ", text).strip()[:MAX_TEXT_CHARS]
    except Exception as e:
        print(f"  [warn] text extraction failed: {e}")
        return ""


def send_to_make(item, pdf, text, test=False):
    pdf_ok = pdf is not None and len(pdf) <= MAX_PDF_MB * 1024 * 1024
    key = f"{item['category'].lower()}/{item['id']}"
    payload = {
        "doc_key": f"test-{now()}/{key}" if test else key,
        "id": item["id"],
        "category": item["category"],
        "title": item["title"],
        "url": item["url"],
        "detected_at": now(),
        "text": text,
        "text_available": bool(text),
        "pdf_attached": pdf_ok,
        "pdf_filename": f"CBIC_{item['category']}_{item['id']}.pdf",
        "pdf_base64": base64.b64encode(pdf).decode() if pdf_ok else "",
    }
    r = requests.post(WEBHOOK, json=payload, timeout=90)
    r.raise_for_status()


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--discover", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args()

    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=os.getenv("CHROME_PATH") or None)
        context = await browser.new_context(user_agent=UA, accept_downloads=False,
                                            locale="en-IN", timezone_id="Asia/Kolkata")
        found = await collect_items(context, discover=args.discover)
        print(f"[scan] {len(found)} document links on watched pages")

        if args.discover:
            await browser.close()
            return

        if not found:
            print("[error] no document links found - run with --discover to inspect")
            await browser.close()
            sys.exit(2)

        if args.test:
            item = max(found.values(), key=lambda v: int(v["id"]))
            print(f"[test] sending newest document {item['id']}: {item['title'][:90]}")
            pdf = await fetch_pdf(context, item)
            text = pdf_text(pdf) if pdf else ""
            print(f"     pdf={'%d KB' % (len(pdf)//1024) if pdf else 'NOT FOUND'} text={len(text)} chars")
            send_to_make(item, pdf, text, test=True)
            print("[test] sent to Make. Check your inbox.")
            await browser.close()
            return

        state = load_state()
        if state is None:
            state = {"seeded_at": now(), "items": {
                k: {"title": v["title"], "first_seen": now(), "sent": False, "seeded": True}
                for k, v in found.items()}}
            save_state(state)
            print(f"[seed] first run: recorded {len(found)} existing documents, sent nothing")
            await browser.close()
            return

        fresh = [v for k, v in found.items() if k not in state["items"]]
        fresh.sort(key=lambda v: int(v["id"]))
        print(f"[new] {len(fresh)} new document(s)")
        if len(fresh) > MAX_PER_RUN:
            print(f"[new] capping at {MAX_PER_RUN} this run; the rest go next run")
            fresh = fresh[:MAX_PER_RUN]

        failures = 0
        for item in fresh:
            key = f"{item['category'].lower()}/{item['id']}"
            print(f"  -> {key}  {item['title'][:90]}")
            if args.dry_run:
                continue
            pdf = await fetch_pdf(context, item)
            text = pdf_text(pdf) if pdf else ""
            print(f"     pdf={'%d KB' % (len(pdf)//1024) if pdf else 'NOT FOUND'} text={len(text)} chars")
            try:
                send_to_make(item, pdf, text)
            except Exception as e:
                failures += 1
                print(f"     [error] Make webhook failed, will retry next run: {e}")
                continue
            state["items"][key] = {"title": item["title"], "first_seen": now(),
                                   "sent": True, "pdf": bool(pdf)}
            state["last_new_at"] = now()
            save_state(state)

        await browser.close()
        if failures:
            sys.exit(1)

        last = datetime.fromisoformat(state.get("last_new_at") or state["seeded_at"])
        quiet_days = (datetime.now(timezone.utc) - last).days
        if quiet_days >= STALE_DAYS:
            print(f"[watchdog] no new documents for {quiet_days} days - please check the site/setup")
            sys.exit(3)


if __name__ == "__main__":
    if not WEBHOOK and "--discover" not in sys.argv and "--dry-run" not in sys.argv:
        sys.exit("MAKE_WEBHOOK_URL is not set")
    asyncio.run(main())
