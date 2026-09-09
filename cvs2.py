"""
Step 2 (Phase A): For every city page in cvs_cities.csv, extract every
                  individual store link (href containing "storeid=").
Step 3 (Phase B): Visit every store link and parse the embedded JSON-LD
                  block for full store details.

Both phases are checkpointed to disk, so you can Ctrl+C and re-run without
losing progress or re-hitting pages you already fetched.

Inputs:
  cvs_cities.csv          (from step1_discover_cities.py)

Outputs:
  cvs_store_links.csv     [state, city, store_url]           (Phase A)
  cvs_store_details.csv   full parsed store record per row    (Phase B)
  _checkpoint_cities_done.txt   (Phase A resume marker)
  _checkpoint_stores_done.txt   (Phase B resume marker)
"""

import csv
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup

BASE = "https://www.cvs.com"
CITIES_CSV = "cvs_cities.csv"
STORE_LINKS_CSV = "cvs_store_links.csv"
STORE_DETAILS_CSV = "cvs_store_details.csv"
CITIES_CHECKPOINT = "_checkpoint_cities_done.txt"
STORES_CHECKPOINT = "_checkpoint_stores_done.txt"

REQUEST_DELAY_SECONDS = 0.3   # per-thread delay
PHASE_A_WORKERS = 8           # city pages are light, more parallelism is fine
PHASE_B_WORKERS = 6           # be gentler on individual store pages

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

STORE_LINK_PATTERN = re.compile(r"/store-locator/[^\"'\s]+storeid=\d+")

# ---------- shared helpers ----------

def get_html(url: str, retries: int = 3, backoff: float = 1.5):
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as e:
            time.sleep(backoff * attempt)
    return None


def load_checkpoint(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return set(line.strip() for line in f if line.strip())
    except FileNotFoundError:
        return set()


def append_checkpoint(path, value):
    with open(path, "a", encoding="utf-8") as f:
        f.write(value + "\n")


# ---------- Phase A: city page -> store links ----------

def extract_store_links(html: str, page_url: str):
    links = set()
    for m in STORE_LINK_PATTERN.finditer(html):
        href = m.group(0)
        full_url = href if href.startswith("http") else BASE + href
        links.add(full_url)
    return links


def phase_a():
    done_cities = load_checkpoint(CITIES_CHECKPOINT)

    with open(CITIES_CSV, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    todo = [r for r in rows if r["city_url"] not in done_cities]
    print(f"Phase A: {len(rows)} city pages total, {len(todo)} remaining")

    out_f = open(STORE_LINKS_CSV, "a", newline="", encoding="utf-8")
    writer = csv.writer(out_f)
    if out_f.tell() == 0:
        writer.writerow(["state", "city", "store_url"])

    def worker(row):
        html = get_html(row["city_url"])
        time.sleep(REQUEST_DELAY_SECONDS)
        if html is None:
            return row, None
        return row, extract_store_links(html, row["city_url"])

    completed = 0
    with ThreadPoolExecutor(max_workers=PHASE_A_WORKERS) as pool:
        futures = [pool.submit(worker, r) for r in todo]
        for fut in as_completed(futures):
            row, links = fut.result()
            if links is None:
                print(f"  [FAILED] {row['city_url']}")
                continue
            for link in sorted(links):
                writer.writerow([row["state"], row["city"], link])
            append_checkpoint(CITIES_CHECKPOINT, row["city_url"])
            out_f.flush()
            completed += 1
            if completed % 100 == 0:
                print(f"  {completed}/{len(todo)} city pages done")

    out_f.close()
    print(f"Phase A done. Store links written to {STORE_LINKS_CSV}\n")


# ---------- Phase B: store page -> full details ----------

DAY_ORDER = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def format_hours(spec_list):
    """Turn openingHoursSpecification list into 'Mon: 08:00-22:00 | Tue: ...' """
    if not spec_list:
        return ""
    by_day = {}
    for entry in spec_list:
        if not isinstance(entry, dict):
            continue
        days = entry.get("dayOfWeek", [])
        if isinstance(days, str):
            days = [days]
        opens = entry.get("opens", "")
        closes = entry.get("closes", "")
        for d in days:
            short = d.split("/")[-1] if "/" in d else d  # handle schema.org URL form
            by_day[short] = f"{opens}-{closes}"
    return " | ".join(f"{d[:3]}: {by_day[d]}" for d in DAY_ORDER if d in by_day)


def parse_store_page(html: str, store_url: str):
    soup = BeautifulSoup(html, "html.parser")
    scripts = soup.find_all("script", type="application/ld+json")

    main_block = None
    for s in scripts:
        try:
            data = json.loads(s.string or s.text)
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = data if isinstance(data, list) else [data]
        for block in candidates:
            if isinstance(block, dict) and block.get("@type") not in (None, "BreadcrumbList"):
                # prefer blocks that actually look like the store record
                if "address" in block or "Pharmacy" in str(block.get("@type", "")):
                    main_block = block
                    break
        if main_block:
            break

    row = {
        "store_url": store_url,
        "store_id": "",
        "name": "",
        "phone": "",
        "pharmacy_phone": "",
        "street_address": "",
        "city": "",
        "state": "",
        "zip": "",
        "country": "",
        "latitude": "",
        "longitude": "",
        "google_maps_url": "",
        "store_hours": "",
        "pharmacy_hours": "",
        "amenities": "",
        "services": "",
        "same_as": "",
        "landing_page_url": "",
        "parse_status": "ok" if main_block else "NO_JSONLD_FOUND",
        "raw_jsonld": json.dumps(main_block) if main_block else "",
    }

    m = re.search(r"storeid=(\d+)", store_url)
    if m:
        row["store_id"] = m.group(1)

    if not main_block:
        return row

    row["name"] = main_block.get("name", "")
    row["phone"] = main_block.get("telephone", "")
    row["landing_page_url"] = main_block.get("url", store_url)
    row["google_maps_url"] = main_block.get("hasMap", "")

    addr = main_block.get("address", {}) or {}
    if isinstance(addr, dict):
        row["street_address"] = addr.get("streetAddress", "")
        row["city"] = addr.get("addressLocality", "")
        row["state"] = addr.get("addressRegion", "")
        row["zip"] = addr.get("postalCode", "")
        row["country"] = addr.get("addressCountry", "")
        if not row["phone"]:
            row["phone"] = addr.get("telephone", "")  # fallback seen on some pages

    geo = main_block.get("geo", {}) or {}
    if isinstance(geo, dict):
        row["latitude"] = geo.get("latitude", "")
        row["longitude"] = geo.get("longitude", "")

    row["store_hours"] = format_hours(main_block.get("openingHoursSpecification", []))

    departments = main_block.get("department", []) or []
    if isinstance(departments, dict):
        departments = [departments]
    pharmacy_hours_parts = []
    for dept in departments:
        if not isinstance(dept, dict):
            continue
        if not row["pharmacy_phone"]:
            row["pharmacy_phone"] = dept.get("telephone", "")
        hrs = format_hours(dept.get("openingHoursSpecification", []))
        if hrs:
            pharmacy_hours_parts.append(f"{dept.get('name', 'Pharmacy')}: {hrs}")
    row["pharmacy_hours"] = " || ".join(pharmacy_hours_parts)

    amenities = main_block.get("amenityFeature", []) or []
    row["amenities"] = ", ".join(
        a.get("name", "") for a in amenities if isinstance(a, dict) and a.get("value")
    )

    offers = main_block.get("makesOffer", []) or []
    services = []
    for o in offers:
        if isinstance(o, dict):
            item = o.get("itemOffered", {})
            if isinstance(item, dict) and item.get("name"):
                services.append(item["name"])
    row["services"] = ", ".join(services)

    same_as = main_block.get("sameAs", []) or []
    row["same_as"] = ", ".join(same_as) if isinstance(same_as, list) else str(same_as)

    return row


FIELDNAMES = [
    "store_id", "store_url", "landing_page_url", "name", "phone", "pharmacy_phone",
    "street_address", "city", "state", "zip", "country", "latitude", "longitude",
    "google_maps_url", "store_hours", "pharmacy_hours", "amenities", "services",
    "same_as", "parse_status", "raw_jsonld",
]


def phase_b():
    done_stores = load_checkpoint(STORES_CHECKPOINT)

    with open(STORE_LINKS_CSV, newline="", encoding="utf-8") as f:
        all_links = sorted(set(row["store_url"] for row in csv.DictReader(f)))

    todo = [u for u in all_links if u not in done_stores]
    print(f"Phase B: {len(all_links)} store pages total, {len(todo)} remaining")

    out_exists = False
    try:
        with open(STORE_DETAILS_CSV, "r", encoding="utf-8"):
            out_exists = True
    except FileNotFoundError:
        pass

    out_f = open(STORE_DETAILS_CSV, "a", newline="", encoding="utf-8")
    writer = csv.DictWriter(out_f, fieldnames=FIELDNAMES)
    if not out_exists:
        writer.writeheader()

    def worker(url):
        html = get_html(url)
        time.sleep(REQUEST_DELAY_SECONDS)
        if html is None:
            return url, None
        return url, parse_store_page(html, url)

    completed = 0
    failed = 0
    with ThreadPoolExecutor(max_workers=PHASE_B_WORKERS) as pool:
        futures = [pool.submit(worker, u) for u in todo]
        for fut in as_completed(futures):
            url, row = fut.result()
            if row is None:
                failed += 1
                print(f"  [FAILED] {url}")
                continue
            writer.writerow(row)
            append_checkpoint(STORES_CHECKPOINT, url)
            out_f.flush()
            completed += 1
            if completed % 200 == 0:
                print(f"  {completed}/{len(todo)} store pages done ({failed} failed so far)")

    out_f.close()
    print(f"Phase B done. {completed} stores written to {STORE_DETAILS_CSV}, {failed} failed fetches.\n")


if __name__ == "__main__":
    # phase_a()
    phase_b()