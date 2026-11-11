#!/usr/bin/env python3
"""
CBIC Tax Information portal watcher (v2 - reads the site's own data feed).
Reads the "Latest Updates" feed for GST, Customs, Central Excise, Service Tax and
HSNS Cess, finds documents not sent before, downloads the PDF and sends it to Make.
Remembers every sent document in state/seen.json so nothing is ever sent twice.
First run records everything and sends nothing.
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
TAX_IDS = {"1000001": "GST", "1000002": "Customs", "1000003": "Central Excise",
           "1000004": "Service Tax", "100005": "HSNS Cess"}
UPDATES_API = "api/cbic-notification-msts/fetchUpdatesByTaxId/{}"
CATEGORIES = {c.strip().lower() for c in os.getenv("CATEGORIES", "").split(",") if c.strip()}
TAXES = {t.strip().lower() for t in os.getenv("TAXES", "").split(",") if t.strip()}
MAX_PER_RUN = int(os.getenv("MAX_PER_RUN", "20"))
MAX_PDF_MB = float(os.getenv("MAX_PDF_MB", "4"))
MAX_TEXT_CHARS = int(os.getenv("MAX_TEXT_CHARS", "40000"))
WEBHOOK = os.getenv("MAKE_WEBHOOK_URL", "")
STALE_DAYS = int(os.getenv("STALE_DAYS", "7"))
ARCHIVE_GAP = int(os.getenv("ARCHIVE_GAP", "300"))
NAV_TIMEOUT = 60_000
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


def normalise(rec: dict, tax: str):
    if not isinstance(rec, dict) or "id" not in rec:
        return None
    rtype = (rec.get("updateType") or "Notification").strip()
    plural = rtype if rtype.endswith("s") else rtype + "s"
    number = (rec.get("notificationNo") or "").strip()
    name = (rec.get("notificationName") or rec.get("notificationNameToBeDisplay") or "").strip()
    title = " - ".join(x for x in (number, name) if x)
    return {
        "id": str(rec["id"]),
        "type": rtype,
        "category": plural,
        "tax": tax,
        "sub_category": rec.get("updateCategory") or "",
        "number": number,
        "title": " ".join(title.split()),
        "date": rec.get("updatedDate") or "",
        "doc_path": (rec.get("docFilePath") or "").replace("\\", "/"),
        "url": urljoin(BASE, f"view-pdf/{rec['id']}/ENG/{plural}"),
    }


def keep(item) -> bool:
    if CATEGORIES and item["category"].lower() not in CATEGORIES:
        return False
    if TAXES and item["tax"].lower() not in TAXES:
        return False
    return True


async def collect_items(context, discover=False):
    page = await context.new_page()
    captured, token, api_log = {}, {"v": None}, []

    async def on_response(resp):
        url = resp.url
        try:
            if "authenticate-token" in url:
                token["v"] = (await resp.json()).get("id_token")
            m = re.search(r"fetchUpdatesByTaxId/(\d+)", url)
            if m and resp.status == 200:
                captured[m.group(1)] = await resp.json()
            if discover and resp.request.resource_type in ("xhr", "fetch"):
                e = {"url": url, "status": resp.status, "content_type": resp.headers.get("content-type", "")}
                if "json" in e["content_type"]:
                    e["sample"] = (await resp.text())[:3000]
                api_log.append(e)
        except Exception:
            pass
    page.on("response", lambda r: asyncio.ensure_future(on_response(r)))

    print(f"[scan] opening {BASE}")
    await page.goto(BASE, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
    try:
        await page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT)
    except Exception:
        pass
    await page.wait_for_timeout(2000)

    headers = {"Accept": "application/json"}
    if token["v"]:
        headers["Authorization"] = f"Bearer {token['v']}"
    for tax_id in TAX_IDS:
        if tax_id in captured:
            continue
        try:
            r = await context.request.get(urljoin(BASE, UPDATES_API.format(tax_id)),
                                          headers=headers, timeout=NAV_TIMEOUT)
            if r.ok:
                captured[tax_id] = await r.json()
            else:
                print(f"[warn] feed {TAX_IDS[tax_id]} returned HTTP {r.status}")
        except Exception as e:
            print(f"[warn] feed {TAX_IDS[tax_id]}: {e}")

    items = {}
    for tax_id, records in captured.items():
        tax = TAX_IDS.get(tax_id, tax_id)
        n = 0
        for rec in records if isinstance(records, list) else []:
            it = normalise(rec, tax)
            if it and keep(it):
                items[f"{it['category'].lower()}/{it['id']}"] = it
                n += 1
        print(f"[scan] {tax:<15} {n} document(s) in Latest Updates")

    if discover:
        Path("discover").mkdir(exist_ok=True)
        for extra in ("content-page/explore-notification", "content-page/explore-circulars"):
            try:
                await page.goto(urljoin(BASE, extra), wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
                await page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT)
            except Exception:
                pass
        await page.wait_for_timeout(1500)
        await page.screenshot(path="discover/last_page.png", full_page=True)
        Path("discover/api_calls.json").write_text(json.dumps(api_log, indent=1, ensure_ascii=False), encoding="utf-8")
        Path("discover/links_found.json").write_text(json.dumps(items, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"[discover] {len(items)} documents, {len(api_log)} API calls -> ./discover/")

    await page.close()
    return items


async def fetch_pdf(context, item):
    candidates = []
    if item.get("doc_path"):
        candidates.append(urljoin(BASE, item["doc_path"]))

    async def try_get(u):
        try:
            r = await context.request.get(u, timeout=NAV_TIMEOUT)
            body = await r.body()
            return body if body[:5] == b"%PDF-" else None
        except Exception:
            return None

    for u in candidates:
        data = await try_get(u)
        if data:
            return data

    page = await context.new_page()
    seen = []
    page.on("response", lambda r: seen.append(r.url) if (
        "pdf" in r.headers.get("content-type", "").lower()
        or "octet-stream" in r.headers.get("content-type", "").lower()) else None)
    try:
        await page.goto(item["url"], wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
        await page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT)
    except Exception as e:
        print(f"  [warn] viewer: {e}")
    await page.wait_for_timeout(2000)
    try:
        seen += await page.evaluate("""() => Array.from(
            document.querySelectorAll('iframe[src], embed[src], object[data]')).map(e => e.src || e.data)""")
    except Exception:
        pass

    data = None
    for u in dict.fromkeys(x for x in seen if x):
        if u.startswith("blob:"):
            try:
                b64 = await page.evaluate("""async (u) => {
                    const a = new Uint8Array(await (await fetch(u)).arrayBuffer());
                    let s = ''; for (let i = 0; i < a.length; i++) s += String.fromCharCode(a[i]);
                    return btoa(s); }""", u)
                body = base64.b64decode(b64)
                data = body if body[:5] == b"%PDF-" else None
            except Exception:
                data = None
        else:
            data = await try_get(u)
        if data:
            break
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
    safe_no = re.sub(r"[^A-Za-z0-9]+", "_", item["number"]).strip("_")[:60] or item["id"]
    payload = {
        "doc_key": f"test-{now()}/{key}" if test else key,
        "id": item["id"],
        "type": item["type"],
        "category": item["category"],
        "tax": item["tax"],
        "sub_category": item["sub_category"],
        "number": item["number"],
        "title": item["title"],
        "date": item["date"],
        "url": item["url"],
        "detected_at": now(),
        "text": text,
        "text_available": bool(text),
        "pdf_attached": pdf_ok,
        "pdf_filename": f"CBIC_{item['tax'].replace(' ', '')}_{safe_no}.pdf",
        "pdf_base64": base64.b64encode(pdf).decode() if pdf_ok else "",
    }
    r = requests.post(WEBHOOK, json=payload, timeout=90)
    r.raise_for_status()


async def process(context, item, test=False):
    pdf = await fetch_pdf(context, item)
    text = pdf_text(pdf) if pdf else ""
    print(f"     pdf={'%d KB' % (len(pdf) // 1024) if pdf else 'NOT FOUND'} text={len(text)} chars")
    send_to_make(item, pdf, text, test=test)
    return pdf


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
        print(f"[scan] {len(found)} documents in total")

        if args.discover:
            await browser.close()
            return

        if not found:
            print("[error] the CBIC feed returned nothing - site may be down or changed")
            await browser.close()
            sys.exit(2)

        if args.test:
            item = max(found.values(), key=lambda v: int(v["id"]))
            print(f"[test] sending newest: {item['tax']} | {item['title'][:90]}")
            await process(context, item, test=True)
            print("[test] sent to Make - check your inbox")
            await browser.close()
            return

        state = load_state()
        if state is None:
            state = {"seeded_at": now(), "items": {
                k: {"title": v["title"], "tax": v["tax"], "first_seen": now(), "sent": False, "seeded": True}
                for k, v in found.items()}}
            save_state(state)
            print(f"[seed] first run: recorded {len(found)} existing documents, sent nothing")
            await browser.close()
            return

        max_seen = {}
        for k in state["items"]:
            cat, _, i = k.partition("/")
            if i.isdigit():
                max_seen[cat] = max(max_seen.get(cat, 0), int(i))

        fresh = []
        for k, v in found.items():
            if k in state["items"]:
                continue
            ceiling = max_seen.get(v["category"].lower(), 0)
            if ceiling and int(v["id"]) < ceiling - ARCHIVE_GAP:
                print(f"  [skip] old archive item {k} - recorded, not emailed")
                state["items"][k] = {"title": v["title"], "tax": v["tax"], "first_seen": now(),
                                     "sent": False, "archive": True}
                continue
            fresh.append(v)
        save_state(state)

        fresh.sort(key=lambda v: int(v["id"]))
        print(f"[new] {len(fresh)} new document(s)")
        fresh = fresh[:MAX_PER_RUN]

        failures = 0
        for item in fresh:
            key = f"{item['category'].lower()}/{item['id']}"
            print(f"  -> {item['tax']} | {key} | {item['title'][:90]}")
            if args.dry_run:
                continue
            try:
                pdf = await process(context, item)
            except Exception as e:
                failures += 1
                print(f"     [error] could not send, will retry next run: {e}")
                continue
            state["items"][key] = {"title": item["title"], "tax": item["tax"], "first_seen": now(),
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
