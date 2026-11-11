"""
Adobe Stock scraper v2 (no Apify, no API key, no login).

Step 1  Reads search / contributor result pages (Adobe's own JSON) and keeps the
        order Adobe ranks them in. With order=nb_downloads that rank is a
        best-seller ranking, even though the listing has no download numbers.
Step 2  (--details) Opens each asset's own page and looks everywhere in it
        (page JSON, background requests, HTML, visible text) for downloads,
        views and upload date. The column "detail_source" says where each
        value was found, so you can judge how trustworthy it is.

Accepts:
  contributor link  https://stock.adobe.com/ng/contributor/212328222/name
  search link       https://stock.adobe.com/search?k=cat
  asset link        https://stock.adobe.com/ng/images/some-title/349979908

Setup:
    pip install playwright
    playwright install chromium

Usage:
    python adobe_stock_scraper.py <url> [<url> ...] [--max-pages 3] [--details]
    python adobe_stock_scraper.py --file urls.txt --out results.csv --details --max-details 30

Everything the browser loads is saved in ./debug. If downloads/views/date come
back empty, upload that folder and the exact location can be found.
"""
import argparse
import csv
import json
import os
import random
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from playwright.sync_api import sync_playwright

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
DEBUG_DIR = "debug"

DL_KEYS = {"nb_downloads", "downloads", "download_count", "downloadcount", "nbdownloads"}
VIEW_KEYS = {"nb_views", "views", "view_count", "viewcount", "nbviews"}
DATE_KEYS = {"creation_date", "upload_date", "uploaddate", "datepublished", "datecreated",
             "created_at", "createdat", "date_created", "creationdate", "created"}


# ---------------------------------------------------------------- helpers
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


def norm_date(v):
    """Epoch numbers -> ISO date; strings are kept as Adobe wrote them."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)) and v > 10**8:
        if v > 10**11:  # milliseconds
            v = v / 1000
        return datetime.fromtimestamp(v, tz=timezone.utc).strftime("%Y-%m-%d")
    s = str(v).strip()
    return s or None


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


def with_params(url, **params):
    u = urlparse(url)
    q = parse_qs(u.query, keep_blank_values=True)
    for k, v in params.items():
        q[k] = [str(v)]
    return urlunparse(u._replace(query=urlencode(q, doseq=True)))


def has_param(url, name):
    return name in parse_qs(urlparse(url).query)


def find_items(obj):
    """Yield dicts that look like search-result assets, wherever they sit."""
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
    cat = it.get("category")
    return {
        "title": it.get("title", ""),
        "link": link,
        "creator": it.get("creator") or it.get("author") or it.get("creator_name") or "",
        "creator_id": it.get("creator_id"),
        "asset_id": it.get("content_id") or it.get("id"),
        "category": cat.get("name") if isinstance(cat, dict) else "",
        "asset_type": it.get("asset_type", ""),
        "is_ai": it.get("is_gentech"),
        "is_premium": it.get("is_premium"),
        "order_key": it.get("order_key"),
        # filled in only if the listing happens to include them
        "downloads": to_int(it.get("nb_downloads")),
        "views": to_int(it.get("nb_views")),
        "upload_date": norm_date(it.get("creation_date")),
        "detail_source": "listing" if it.get("nb_downloads") is not None else "",
    }


# ---------------------------------------------------------------- browser
def load_and_capture(page, url, tag):
    """Open url; return (json blobs, html, visible text). Saves debug files."""
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

    # JSON embedded in the HTML (ld+json, __NEXT_DATA__, plain JSON scripts)
    for node in page.query_selector_all("script"):
        try:
            text = (node.inner_text() or "").strip()
        except Exception:
            continue
        if len(text) > 2 and text[0] in "{[":
            try:
                blobs.append(("embedded-script", json.loads(text)))
            except Exception:
                pass

    html = page.content()
    try:
        visible = page.inner_text("body")
    except Exception:
        visible = ""

    os.makedirs(DEBUG_DIR, exist_ok=True)
    with open(os.path.join(DEBUG_DIR, f"{tag}_responses.json"), "w", encoding="utf-8") as f:
        json.dump([{"url": u, "data": d} for u, d in blobs], f)
    with open(os.path.join(DEBUG_DIR, f"{tag}_page.html"), "w", encoding="utf-8") as f:
        f.write(html[:1_500_000])
    with open(os.path.join(DEBUG_DIR, f"{tag}_text.txt"), "w", encoding="utf-8") as f:
        f.write(visible[:200_000])
    return blobs, html, visible


# ---------------------------------------------------------------- detail extraction
def dicts_for_asset(obj, asset_id):
    """Yield dicts that describe THIS asset (so similar-image cards are ignored)."""
    if isinstance(obj, dict):
        ident = obj.get("content_id", obj.get("id"))
        if ident is not None and str(ident) == str(asset_id):
            yield obj
        for v in obj.values():
            yield from dicts_for_asset(v, asset_id)
    elif isinstance(obj, list):
        for v in obj:
            yield from dicts_for_asset(v, asset_id)


def pull(d, keys, conv):
    for k, v in d.items():
        if k.lower() in keys:
            val = conv(v)
            if val is not None:
                return val
    return None


def walk_any(obj, keys, conv):
    """Last resort: first matching key anywhere in the JSON."""
    if isinstance(obj, dict):
        got = pull(obj, keys, conv)
        if got is not None:
            return got
        for v in obj.values():
            got = walk_any(v, keys, conv)
            if got is not None:
                return got
    elif isinstance(obj, list):
        for v in obj:
            got = walk_any(v, keys, conv)
            if got is not None:
                return got
    return None


def extract_details(asset_id, blobs, html, visible):
    out = {"downloads": None, "views": None, "upload_date": None, "detail_source": ""}
    sources = []

    # 1. JSON that describes this exact asset
    for _, data in blobs:
        for d in dicts_for_asset(data, asset_id):
            for field, keys, conv in (("downloads", DL_KEYS, to_int),
                                      ("views", VIEW_KEYS, to_int),
                                      ("upload_date", DATE_KEYS, norm_date)):
                if out[field] is None:
                    val = pull(d, keys, conv)
                    if val is not None:
                        out[field] = val
                        sources.append(f"{field}:json-asset")

    # 2. schema.org style JSON-LD dates anywhere (uploadDate, datePublished...)
    if out["upload_date"] is None:
        for _, data in blobs:
            val = walk_any(data, DATE_KEYS, norm_date)
            if val is not None:
                out["upload_date"] = val
                sources.append("upload_date:json-any")
                break

    # 3. raw HTML key regex
    patterns = {
        "downloads": (r'"(?:nb_downloads|download_?count)"\s*:\s*"?([\d,\.]+)', to_int),
        "views": (r'"(?:nb_views|view_?count)"\s*:\s*"?([\d,\.]+)', to_int),
        "upload_date": (r'"(?:creation_date|upload_?date|date_?created|datePublished)"\s*:\s*"([^"]+)"', norm_date),
    }
    for field, (pat, conv) in patterns.items():
        if out[field] is None:
            # Only trust the raw-HTML match when the page contains exactly one
            # distinct value. If several differ, the page is showing other
            # assets (similar images, carousels) and we cannot tell which is ours.
            vals = {conv(v) for v in re.findall(pat, html, re.I)}
            vals.discard(None)
            if len(vals) == 1:
                out[field] = vals.pop()
                sources.append(f"{field}:html-regex")
            elif len(vals) > 1:
                sources.append(f"{field}:html-ambiguous-skipped")

    out["detail_source"] = ";".join(sources)
    return out


def fetch_details(page, row, tag):
    link = row.get("link")
    if not link:
        return
    blobs, html, visible = load_and_capture(page, link, tag)
    title = (page.title() or "").lower()
    if "denied" in title or "captcha" in title or "robot" in visible.lower()[:500]:
        print("    blocked by Adobe (access denied / captcha) - slow down or retry later")
        return
    found = extract_details(row["asset_id"], blobs, html, visible)
    for k in ("downloads", "views", "upload_date"):
        if found[k] is not None:
            row[k] = found[k]
    if found["detail_source"]:
        row["detail_source"] = found["detail_source"]


# ---------------------------------------------------------------- scrape
def scrape(page, url, args, tag):
    kind = kind_of(url)
    rows = {}

    if kind == "asset":
        aid = asset_id_from_url(url)
        rows[aid] = {"title": "", "link": url, "creator": "", "creator_id": None,
                     "asset_id": aid, "category": "", "asset_type": "", "is_ai": None,
                     "is_premium": None, "order_key": None, "rank": None,
                     "downloads": None, "views": None, "upload_date": None,
                     "detail_source": ""}
        return rows

    base = url
    if not has_param(base, "order") and not args.keep_order:
        base = with_params(base, order="nb_downloads")  # best sellers first

    rank = 0
    for n in range(1, args.max_pages + 1):
        target = with_params(base, search_page=n)
        blobs, _, _ = load_and_capture(page, target, f"{tag}_p{n}")
        page_items = []
        for _, data in blobs:
            for it in find_items(data):
                page_items.append(to_row(it))
        # Adobe's order_key is the position inside the page
        page_items.sort(key=lambda r: (r["order_key"] is None, r["order_key"] or 0))
        new = 0
        for r in page_items:
            if r["asset_id"] and r["asset_id"] not in rows:
                rank += 1
                r["rank"] = rank
                rows[r["asset_id"]] = r
                new += 1
        print(f"  page {n}: {new} new assets")
        if new == 0:
            break
        time.sleep(random.uniform(2, 4))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("urls", nargs="*")
    ap.add_argument("--file")
    ap.add_argument("--out", default="adobe_assets.csv")
    ap.add_argument("--max-pages", type=int, default=3,
                    help="pages per contributor/search link (about 100 assets each)")
    ap.add_argument("--details", action="store_true",
                    help="open every asset page to look for downloads/views/upload date")
    ap.add_argument("--max-details", type=int, default=50,
                    help="cap on asset pages opened per run (be gentle with Adobe)")
    ap.add_argument("--keep-order", action="store_true",
                    help="do not force order=nb_downloads on search/contributor links")
    args = ap.parse_args()

    urls = list(args.urls)
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            urls += [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    if not urls:
        ap.error("give at least one URL or --file")

    all_rows = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True, args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(user_agent=UA, locale="en-US",
                                  viewport={"width": 1366, "height": 900})
        page = ctx.new_page()

        for i, url in enumerate(urls, 1):
            print(f"[{i}/{len(urls)}] {url}")
            try:
                rows = list(scrape(page, url, args, f"url{i}").values())
            except Exception as e:
                print(f"  error: {e}")
                rows = []
            if not rows:
                print("  nothing found (see the debug folder)")
                continue

            is_asset = kind_of(url) == "asset"
            if args.details or is_asset:
                limit = len(rows) if is_asset else args.max_details
                for j, r in enumerate(rows[:limit], 1):
                    print(f"  details {j}/{min(limit, len(rows))}: {r['asset_id']}")
                    try:
                        fetch_details(page, r, f"url{i}_asset{r['asset_id']}")
                    except Exception as e:
                        print(f"    error: {e}")
                    time.sleep(random.uniform(2, 4))
            all_rows += rows
        browser.close()

    fields = ["rank", "asset_id", "title", "link", "creator", "creator_id", "category",
              "asset_type", "is_ai", "is_premium", "downloads", "views",
              "upload_date", "detail_source"]
    with open(args.out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(all_rows)

    got_dl = sum(1 for r in all_rows if r.get("downloads") is not None)
    got_date = sum(1 for r in all_rows if r.get("upload_date"))
    print(f"Saved {len(all_rows)} rows to {args.out} "
          f"(downloads found: {got_dl}, upload dates found: {got_date})")
    if args.details and not got_dl:
        print("No download counts found. Upload the debug folder so the exact "
              "location (or its absence) can be checked.")


if __name__ == "__main__":
    main()
