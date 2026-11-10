"""
Get title, link, views and downloads for Adobe Stock assets by URL,
using the Apify actor igolaizola/adobe-stock-scraper (looks up exact asset IDs).

Needs an Apify API token in the APIFY_TOKEN environment variable.
No extra packages required (standard library only).

Usage:
    python apify_adobe.py https://stock.adobe.com/images/some-title/349979908
    python apify_adobe.py url1 url2 --out results.csv
    python apify_adobe.py --file urls.txt
"""
import argparse
import csv
import json
import os
import re
import sys
import urllib.error
import urllib.request

ACTOR = "igolaizola~adobe-stock-scraper"
ENDPOINT = f"https://api.apify.com/v2/acts/{ACTOR}/run-sync-get-dataset-items"


def asset_id(text):
    """Adobe Stock URLs end with the numeric asset ID. Bare IDs also work."""
    text = text.strip()
    if text.isdigit():
        return int(text)
    path = text.split("?")[0].split("#")[0].rstrip("/")
    m = re.search(r"/(\d{5,})$", path)
    return int(m.group(1)) if m else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("urls", nargs="*", help="Adobe Stock asset URLs (or bare asset IDs)")
    ap.add_argument("--file", help="text file, one URL per line")
    ap.add_argument("--out", default="adobe_assets.csv")
    args = ap.parse_args()

    inputs = list(args.urls)
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            inputs += [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]

    ids, bad = [], []
    for item in inputs:
        i = asset_id(item)
        if i is None:
            bad.append(item)
        elif i not in ids:
            ids.append(i)
    for b in bad:
        print(f"Could not find an asset ID in: {b}", file=sys.stderr)
    if not ids:
        sys.exit("No valid Adobe Stock asset URLs/IDs given.")

    token = os.environ.get("APIFY_TOKEN")
    if not token:
        sys.exit("Set the APIFY_TOKEN environment variable.")

    body = json.dumps({"maxItems": len(ids), "assetIds": ids}).encode()
    req = urllib.request.Request(
        ENDPOINT, data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            items = json.load(resp)
    except urllib.error.HTTPError as e:
        sys.exit(f"Apify error {e.code}: {e.read().decode(errors='replace')[:500]}")

    by_id = {it.get("id"): it for it in items}
    rows = []
    for i in ids:
        it = by_id.get(i)
        if it:
            rows.append({
                "title": it.get("title", ""),
                "link": it.get("details_url") or f"https://stock.adobe.com/{i}",
                "views": it.get("nb_views"),
                "downloads": it.get("nb_downloads"),
                "creator": it.get("creator_name", ""),
                "asset_id": i,
            })
        else:
            rows.append({"title": "", "link": f"https://stock.adobe.com/{i}",
                         "views": None, "downloads": None, "creator": "",
                         "asset_id": i})
            print(f"Asset {i} was not returned (removed or unavailable).", file=sys.stderr)

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["title", "link", "views", "downloads", "creator", "asset_id"])
        w.writeheader()
        w.writerows(rows)
    for r in rows:
        print(f"{r['asset_id']}: {r['title'][:50]!r} views={r['views']} downloads={r['downloads']}")
    print(f"Saved {len(rows)} rows to {args.out}")


if __name__ == "__main__":
    main()
