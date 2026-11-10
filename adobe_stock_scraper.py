"""
Adobe Stock scraper (no Apify, no API key, no login).

Opens Adobe Stock pages in a real headless browser and reads the JSON data
the website itself loads (the same data Apify actors use), then saves
title, link, views and downloads to a CSV.

Accepts:
  * contributor link  https://stock.adobe.com/ng/contributor/212328222/name
  * search link       https://stock.adobe.com/search?k=cat
  * asset link        https://stock.adobe.com/images/some-title/349979908

Setup:
    pip install playwright
    playwright install chromium

Usage:
    python adobe_stock_scraper.py <url> [<url> ...] [--max-pages 3]
    python adobe_stock_scraper.py --file urls.txt --out results.csv

Everything the browser loads is also saved in ./debug so you can see exactly
what Adobe sent (useful if a column comes back empty).
"""
import argparse
import csv
import json
import os
import re
import time
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from playwright.sync_api import sync_playwright

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
DEBUG_DIR = "debug"


def to_int(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    raw = str(v).strip().replace(",", "").replace(" ", "")
    mult = 1
    if raw and raw[-1] in "Kk":
        mult, raw = 1000, raw[:-1]
    elif raw and raw[-1] in "Mm":
        mult, raw = 1_000_000, raw[:-1]
    try:
        return int(float(raw) * mult)
    except ValueError:
        return None


def kind_of(url):
    path = urlparse(url).path
    if re.search(r"/contributor/\d+", path):
        return "contributor"
    if re.search(r"/\d{5,}/?$", path):
        return "asset"
    return "search"


def asset_id_from_url(url):
    m = re.search(r"/(\d{5,})/?$", urlparse(url).path)
    return int(m.group(1)) if m else None


def with_page(url, n):
    u = urlparse(url)
    q = parse_qs(u.query, keep_blank_values=True)
    q["search_page"] = [str(n)]
    return urlunparse(u._replace(query=urlencode(q, doseq=True)))


def find_items(obj):
    """Yield dicts that look like assets, wherever they sit in the JSON."""
    if isinstance(obj, dict):
        has_id = "id" in obj or "content_id" in obj
        looks_asset = any(k in obj for k in
                          ("nb_downloads", "nb_views", "content_url", "details_url"))
        if has_id and "title" in obj and looks_asset:
            yield obj
            return
        for v in obj.values():
            yield from find_items(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from find_items(v)


def to_row(it):
    link = it.get("content_url") or it.get("details_url") or ""
    if link.startswith("/"):
        link = "https://stock.adobe.com" + link
    return {
        "title": it.get("title", ""),
        "link": link,
        "views": to_int(it.get("nb_views")),
        "downloads": to_int(it.get("nb_downloads")),
        "creator": it.get("creator_name") or it.get("author") or "",
        "asset_id": it.get("content_id") or it.get("id"),
    }


def load_and_capture(page, url, tag):
    """Open url and return every JSON blob the page loaded or embedded."""
    blobs = []

    def on_response(resp):
        try:
            if "json" in (resp.headers.get("content-type") or ""):
                blobs.append((resp.url, resp.json()))
        except Exception:
            pass

    page.on("response", on_response)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        time.sleep(1)
    finally:
        page.remove_listener("response", on_response)

    # JSON embedded in the HTML
    for node in page.query_selector_all("script"):
        try:
            text = node.inner_text()
        except Exception:
            continue
        if text and text.lstrip()[:1] in "{[" and ("nb_" in text or "title" in text):
            try:
                blobs.append(("embedded-script", json.loads(text)))
            except Exception:
                pass

    # save for debugging
    os.makedirs(DEBUG_DIR, exist_ok=True)
    with open(os.path.join(DEBUG_DIR, f"{tag}_responses.json"), "w", encoding="utf-8") as f:
        json.dump([{"url": u, "data": d} for u, d in blobs], f)
    with open(os.path.join(DEBUG_DIR, f"{tag}_page.html"), "w", encoding="utf-8") as f:
        f.write(page.content()[:1_000_000])
    return blobs


def scrape(page, url, max_pages, tag):
    kind = kind_of(url)
    pages = 1 if kind == "asset" else max_pages
    want_id = asset_id_from_url(url) if kind == "asset" else None
    seen = {}
    for n in range(1, pages + 1):
        target = url if kind == "asset" else with_page(url, n)
        blobs = load_and_capture(page, target, f"{tag}_p{n}")
        new = 0
        for _, data in blobs:
            for it in find_items(data):
                r = to_row(it)
                if want_id and str(r["asset_id"]) != str(want_id):
                    continue
                if r["asset_id"] and r["asset_id"] not in seen:
                    seen[r["asset_id"]] = r
                    new += 1
        print(f"  page {n}: {new} new assets")
        if new == 0:
            break
        time.sleep(2)  # be polite
    return list(seen.values())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("urls", nargs="*")
    ap.add_argument("--file")
    ap.add_argument("--out", default="adobe_assets.csv")
    ap.add_argument("--max-pages", type=int, default=3,
                    help="pages to read per contributor/search link (about 100 assets each)")
    args = ap.parse_args()

    urls = list(args.urls)
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            urls += [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    if not urls:
        ap.error("give at least one URL or --file")

    rows = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True, args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(user_agent=UA, locale="en-US",
                                  viewport={"width": 1366, "height": 900})
        page = ctx.new_page()
        for i, url in enumerate(urls, 1):
            print(f"[{i}/{len(urls)}] {url}")
            try:
                found = scrape(page, url, args.max_pages, f"url{i}")
            except Exception as e:
                print(f"  error: {e}")
                found = []
            if not found:
                print("  nothing found (see the debug folder for what the page returned)")
            rows += found
        browser.close()

    fields = ["title", "link", "views", "downloads", "creator", "asset_id"]
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    print(f"Saved {len(rows)} rows to {args.out}")


if __name__ == "__main__":
    main()
