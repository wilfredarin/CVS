"""
Step 1: Discover all state -> city URLs from the CVS store locator.

Hierarchy:
  /store-locator/cvs-pharmacy-locations                    -> all states
  /store-locator/cvs-pharmacy-locations/{State-Name}        -> cities in that state

Output: cvs_cities.csv with columns [state, city, city_url]
This is the input list for Step 2 (pulling store links out of each city page).
"""

import csv
import re
import time

import requests
from bs4 import BeautifulSoup

BASE = "https://www.cvs.com"
STATE_INDEX_URL = f"{BASE}/store-locator/cvs-pharmacy-locations"
OUTPUT_CSV = "cvs_cities.csv"
REQUEST_DELAY_SECONDS = 1.0  # be polite; adjust if you're getting throttled/blocked

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


def get_soup(url: str, retries: int = 3, backoff: float = 1.5):
    """Fetch a URL and return a BeautifulSoup object, retrying on failure."""
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "html.parser")
        except requests.RequestException as e:
            print(f"    [retry {attempt}/{retries}] {url} -> {e}")
            time.sleep(backoff * attempt)
    print(f"    [FAILED] giving up on {url}")
    return None


def get_state_links():
    """Parse the state index page for links to each state's page."""
    soup = get_soup(STATE_INDEX_URL)
    if soup is None:
        raise RuntimeError("Could not fetch the state index page at all — check URL/network first.")

    # Matches hrefs like /store-locator/cvs-pharmacy-locations/Georgia
    # (exactly one path segment after ".../cvs-pharmacy-locations/")
    pattern = re.compile(r"^/store-locator/cvs-pharmacy-locations/[^/]+/?$")

    found = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if pattern.match(href):
            state_slug = href.rstrip("/").split("/")[-1]
            full_url = BASE + href if href.startswith("/") else href
            found[state_slug] = full_url

    return sorted(found.items())  # list of (state_slug, state_url)


def get_city_links(state_url: str):
    """Parse a state page for links to each city's page."""
    soup = get_soup(state_url)
    if soup is None:
        return []

    state_path = state_url.replace(BASE, "").rstrip("/")
    # Matches hrefs like /store-locator/cvs-pharmacy-locations/Georgia/Duluth
    pattern = re.compile(re.escape(state_path) + r"/[^/]+/?$")

    found = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if pattern.match(href):
            city_slug = href.rstrip("/").split("/")[-1]
            full_url = BASE + href if href.startswith("/") else href
            found[city_slug] = full_url

    return sorted(found.items())  # list of (city_slug, city_url)


def main():
    print("Fetching state index...")
    states = get_state_links()
    print(f"Found {len(states)} state pages (expect ~51: 50 states + DC)\n")

    if not states:
        print("No state links found — the page structure may differ from what this script expects.")
        print("Save the raw HTML of the state index page and inspect it before proceeding:")
        print(f'  curl -A "Mozilla/5.0" "{STATE_INDEX_URL}" -o state_index.html')
        return

    total_cities = 0
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["state", "city", "city_url"])

        for i, (state_slug, state_url) in enumerate(states, 1):
            print(f"[{i}/{len(states)}] {state_slug} ...", end=" ")
            cities = get_city_links(state_url)
            print(f"{len(cities)} cities")

            for city_slug, city_url in cities:
                writer.writerow([state_slug, city_slug, city_url])
            total_cities += len(cities)

            f.flush()  # write progress to disk as we go
            time.sleep(REQUEST_DELAY_SECONDS)

    print(f"\nDone. {total_cities} city pages written to {OUTPUT_CSV}")


if __name__ == "__main__":
    main()