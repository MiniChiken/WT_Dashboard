"""Refreshes frontend/vehicles.json - the vehicle id/country/type/premium
dataset used for: the roster's gold premium highlight, the vehicle-name wiki
link, and (as of this script) the backend's aircraft/helicopter detection for
the air-threat warning.

Source: a third-party War Thunder vehicles API (wtvehiclesapi.duckdns.org,
built from public game datamines) - there's no first-party API for this.
Neither telemetry nor the kill feed exposes a vehicle's real internal id,
premium status, or category, so this snapshot is the only way any of those
three features work at all.

This is a point-in-time snapshot, not a live feed - it drifts as new
vehicles/premiums release. Re-run this script to refresh it:

    python scripts/update_vehicles.py

Rate limit on the source API is 10K requests/72h per IP - a full refresh
uses ~17 requests (200 vehicles per page), so this is safe to run as often
as you like.
"""
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API_BASE = "https://wtvehiclesapi.duckdns.org/api"
PAGE_SIZE = 200
OUTPUT_PATH = Path(__file__).parent.parent / "frontend" / "vehicles.json"


def fetch_page(page: int) -> list:
    url = f"{API_BASE}/vehicles?limit={PAGE_SIZE}&page={page}"
    req = urllib.request.Request(url, headers={"User-Agent": "wt-dashboard-updater"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)


def fetch_all_vehicles() -> list:
    combined = []
    seen = set()
    page = 0
    while True:
        try:
            batch = fetch_page(page)
        except (urllib.error.URLError, TimeoutError) as e:
            print(f"  page {page}: request failed ({e}), stopping here", file=sys.stderr)
            break
        for v in batch:
            vid = v.get("identifier")
            if not vid or vid in seen:
                continue
            seen.add(vid)
            combined.append({
                "id": vid,
                "country": v.get("country"),
                "type": v.get("vehicle_type"),
                "premium": bool(v.get("is_premium")),
            })
        print(f"  page {page}: {len(batch)} vehicles (total so far: {len(combined)})")
        if len(batch) < PAGE_SIZE:
            break
        page += 1
        time.sleep(0.2)  # polite pacing, not required by the rate limit but no reason to hammer it
    return combined


def main():
    print("Fetching vehicle list from", API_BASE)
    vehicles = fetch_all_vehicles()
    if not vehicles:
        print("No vehicles fetched - aborting without touching the existing file.", file=sys.stderr)
        sys.exit(1)

    vehicles.sort(key=lambda v: v["id"])
    OUTPUT_PATH.write_text(json.dumps(vehicles, separators=(",", ":")), encoding="utf-8")

    premium_count = sum(1 for v in vehicles if v["premium"])
    air_types = {"fighter", "bomber", "assault", "attack_helicopter", "utility_helicopter"}
    air_count = sum(1 for v in vehicles if v["type"] in air_types)
    print(f"\nWrote {len(vehicles)} vehicles to {OUTPUT_PATH}")
    print(f"  {premium_count} premium, {air_count} aircraft/helicopters")


if __name__ == "__main__":
    main()
