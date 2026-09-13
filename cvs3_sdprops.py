"""
Phase B (v2): For every store URL in cvs_store_links.csv, fetch the page and
pull the `sd-props` attribute off the <cvs-store-details-page> custom element.
This is the actual Stencil hydration props object -- the literal data the
page was rendered from -- so it matches the visible UI exactly (unlike the
schema.org JSON-LD block, which was found to disagree with the UI for
pharmacy hours on at least one store).

Input:
  cvs_store_links.csv        [state, city, store_url]   (from Phase A)

Output:
  cvs_store_details_v2.csv   one full row per store
  _checkpoint_stores_v2.txt  resume marker
"""

import csv
import html
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

STORE_LINKS_CSV = "cvs_store_links.csv"
OUTPUT_CSV = "cvs_store_details_v2.csv"
CHECKPOINT = "_checkpoint_stores_v2.txt"

REQUEST_DELAY_SECONDS = 0.3
WORKERS = 6

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

# Captures the raw (still HTML-entity-escaped) JSON string inside the attribute.
SD_PROPS_PATTERN = re.compile(r'sd-props="(.*?)"', re.DOTALL)

WEEKDAY_ORDER = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]


def get_html_text(url: str, retries: int = 3, backoff: float = 1.5):
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException:
            time.sleep(backoff * attempt)
    return None


def extract_sd_props(page_html: str):
    m = SD_PROPS_PATTERN.search(page_html)
    if not m:
        return None
    raw = html.unescape(m.group(1))
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def format_dept_hours(regHours):
    """[{'weekday':'MON','startTime':'08:00 AM','endTime':'08:00 PM','breakStart':...}] -> string"""
    if not regHours:
        return ""
    by_day = {}
    for entry in regHours:
        wd = entry.get("weekday", "")
        start = entry.get("startTime", "")
        end = entry.get("endTime", "")
        part = f"{start}-{end}"
        bs, be = entry.get("breakStart"), entry.get("breakEnd")
        if bs and be:
            part += f" (break {bs}-{be})"
        by_day[wd] = part
    return " | ".join(f"{d.title()}: {by_day[d]}" for d in WEEKDAY_ORDER if d in by_day)


def parse_store(page_html: str, store_url: str):
    row = {
        "store_id": "", "store_url": store_url, "landing_page_url": store_url,
        "full_address": "", "street_address": "", "city": "", "state": "", "zip": "",
        "country": "", "latitude": "", "longitude": "",
        "phone_pharmacy": "", "phone_photo": "", "phone_retail": "", "fax": "",
        "timezone": "", "store_hours": "", "pharmacy_hours": "",
        "services": "", "indicators_raw": "",
        "parse_status": "", "raw_sd_props": "",
    }

    props = extract_sd_props(page_html)
    if props is None:
        row["parse_status"] = "NO_SD_PROPS_FOUND"
        return row

    row["raw_sd_props"] = json.dumps(props)
    row["parse_status"] = "ok"

    details = props.get("cvsStoreDetails", {}) or {}
    addr = details.get("address", {}) or {}
    info = details.get("storeInfo", {}) or {}
    hours = details.get("hours", {}) or {}

    row["store_id"] = info.get("storeId", "")
    row["full_address"] = props.get("cvsStoreAddressTitle", "")
    row["street_address"] = addr.get("street", "")
    row["city"] = addr.get("city", "")
    row["state"] = addr.get("state", "")
    row["zip"] = addr.get("zip", "")
    row["country"] = addr.get("country", "")

    row["latitude"] = info.get("latitude", "")
    row["longitude"] = info.get("longitude", "")
    row["fax"] = info.get("faxNumber", "")

    phones = info.get("phoneNumbers", [{}])
    if phones:
        row["phone_pharmacy"] = phones[0].get("pharmacy", "")
        row["phone_photo"] = phones[0].get("photo", "")
        row["phone_retail"] = phones[0].get("retail", "")

    row["timezone"] = hours.get("timeZone", "")
    for dept in hours.get("departments", []) or []:
        name = dept.get("name", "")
        formatted = format_dept_hours(dept.get("regHours", []))
        if name == "retail":
            row["store_hours"] = formatted
        elif name == "pharmacy":
            row["pharmacy_hours"] = formatted

    services = props.get("cvsStoreServicesProps", []) or []
    row["services"] = ", ".join(s.get("displayText", "") for s in services if s.get("displayText"))

    row["indicators_raw"] = ", ".join(details.get("indicators", []) or [])

    return row


FIELDNAMES = [
    "store_id", "store_url", "landing_page_url", "full_address", "street_address",
    "city", "state", "zip", "country", "latitude", "longitude",
    "phone_pharmacy", "phone_photo", "phone_retail", "fax", "timezone",
    "store_hours", "pharmacy_hours", "services", "indicators_raw",
    "parse_status", "raw_sd_props",
]


def load_checkpoint(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return set(line.strip() for line in f if line.strip())
    except FileNotFoundError:
        return set()


def append_checkpoint(path, value):
    with open(path, "a", encoding="utf-8") as f:
        f.write(value + "\n")


def main():
    with open(STORE_LINKS_CSV, newline="", encoding="utf-8") as f:
        all_links = sorted(set(r["store_url"] for r in csv.DictReader(f)))

    done = load_checkpoint(CHECKPOINT)
    todo = [u for u in all_links if u not in done]
    print(f"{len(all_links)} stores total, {len(todo)} remaining")

    out_exists = False
    try:
        with open(OUTPUT_CSV, "r", encoding="utf-8"):
            out_exists = True
    except FileNotFoundError:
        pass

    out_f = open(OUTPUT_CSV, "a", newline="", encoding="utf-8")
    writer = csv.DictWriter(out_f, fieldnames=FIELDNAMES)
    if not out_exists:
        writer.writeheader()

    def worker(url):
        page_html = get_html_text(url)
        time.sleep(REQUEST_DELAY_SECONDS)
        if page_html is None:
            return url, None
        return url, parse_store(page_html, url)

    completed = 0
    failed = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(worker, u) for u in todo]
        for fut in as_completed(futures):
            url, row = fut.result()
            if row is None:
                failed += 1
                print(f"  [FAILED] {url}")
                continue
            writer.writerow(row)
            append_checkpoint(CHECKPOINT, url)
            out_f.flush()
            completed += 1
            if completed % 200 == 0:
                print(f"  {completed}/{len(todo)} done ({failed} failed so far)")

    out_f.close()
    print(f"Done. {completed} stores written to {OUTPUT_CSV}, {failed} failed fetches.")


if __name__ == "__main__":
    main()