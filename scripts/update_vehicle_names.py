"""Build the offline English combat-log name map from a pinned WT datamine.

Usage: python scripts/update_vehicle_names.py [--ref COMMIT_OR_BRANCH]
Only the short `_1` labels are used: full/shop labels can describe the same
vehicle differently from the combat log. Missing short labels stay unmapped.
"""

import argparse
import csv
import hashlib
import io
import json
import re
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path


REPOSITORY = "gszabi99/War-Thunder-Datamine"
LOCALIZATION_PATH = "lang.vromfs.bin_u/lang/units.csv"
CATALOG_PATH = "char.vromfs.bin_u/config/wpcost.blkx"
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "frontend"
VEHICLE_MOVE_TYPES = {
    "air", "helicopter", "tank", "heavy_tank", "wheeled_vehicle",
    "ship", "fast_ship", "slow_ship",
}


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": "wt-dashboard-name-updater"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def resolve_ref(ref):
    if re.fullmatch(r"[0-9a-fA-F]{40}", ref):
        return ref.lower()
    url = f"https://api.github.com/repos/{REPOSITORY}/commits/{urllib.parse.quote(ref, safe='')}"
    return json.loads(fetch(url))["sha"]


def build_names(catalog, localization):
    reader = csv.DictReader(io.StringIO(localization.lstrip("\ufeff")), delimiter=";")
    if not {"<ID|readonly|noverify>", "<English>"}.issubset(reader.fieldnames or []):
        raise ValueError("Localization file is missing its ID or English column")
    short_names = {}
    for row in reader:
        key = row["<ID|readonly|noverify>"]
        if not key.endswith("_1"):
            continue
        name = (row.get("<English>") or "").strip()
        if name:
            unit_id = key[:-2]
            if unit_id in short_names and short_names[unit_id] != name:
                raise ValueError(f"Conflicting short names for {unit_id}")
            short_names[unit_id] = name

    vehicles = sorted(
        key for key, value in catalog.items()
        if isinstance(value, dict) and value.get("unitMoveType") in VEHICLE_MOVE_TYPES
    )
    names = {key: short_names[key] for key in vehicles if key in short_names}
    if not names:
        raise ValueError("No vehicle names found; refusing to replace the existing snapshot")
    missing = [key for key in vehicles if key not in names]
    return names, missing


def write_json(path, value):
    # A failed download or interrupted write must not leave a partial JSON file.
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.chmod(0o644)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default="master", help="Datamine commit, tag, or branch (default: master)")
    args = parser.parse_args()
    commit = resolve_ref(args.ref)
    raw_base = f"https://raw.githubusercontent.com/{REPOSITORY}/{commit}"
    localization = fetch(f"{raw_base}/{LOCALIZATION_PATH}")
    catalog = fetch(f"{raw_base}/{CATALOG_PATH}")
    version = fetch(f"{raw_base}/version").decode("utf-8").strip()
    names, missing = build_names(json.loads(catalog), localization.decode("utf-8-sig"))
    if len(names) / (len(names) + len(missing)) < 0.95:
        raise ValueError("Fewer than 95% of catalog vehicles have short names; keeping the existing snapshot")
    metadata = {
        "repository": f"https://github.com/{REPOSITORY}",
        "commit": commit,
        "game_version": version,
        "language": "English",
        "label_suffix": "_1",
        "sources": {
            LOCALIZATION_PATH: hashlib.sha256(localization).hexdigest(),
            CATALOG_PATH: hashlib.sha256(catalog).hexdigest(),
        },
        "catalog_vehicle_count": len(names) + len(missing),
        "mapped_vehicle_count": len(names),
        "missing_short_names": missing,
    }
    write_json(OUTPUT_DIR / "vehicle_names.json", names)
    write_json(OUTPUT_DIR / "vehicle_names.meta.json", metadata)
    print(f"Wrote {len(names)} English vehicle names from {commit} (game {version})")
    if missing:
        print(f"{len(missing)} vehicles lack a short label; generic normalization remains in use:")
        print("\n".join(missing))


if __name__ == "__main__":
    main()
