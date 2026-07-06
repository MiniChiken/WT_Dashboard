import json
import math
import re
from collections import defaultdict, deque
from pathlib import Path

import db

# Shared with the frontend's premium-highlight/wiki-link matching (see
# vehicleIndex in frontend/app.js) - one dataset, refreshed by
# scripts/update_vehicles.py, used by both sides for their own purpose.
_VEHICLES_JSON_PATH = Path(__file__).parent.parent / "frontend" / "vehicles.json"
_AIR_VEHICLE_TYPES = {"fighter", "bomber", "assault", "attack_helicopter", "utility_helicopter"}
# Some ids in the dataset use full nation words ("ussr_t_80ud"), others use
# abbreviated ones ("germ_", "cn_", "fr_", "sw_", "it_", "jp_", "il_") - a
# different (and inconsistent) convention from WT's own live telemetry
# (always full words, e.g. indicators.army). Both are handled here since this
# lookup is built from the dataset's ids, not from telemetry.
_VEHICLE_ID_NATION_PREFIXES = {
    "ussr", "usa", "us", "germ", "germany", "uk", "britain", "fr", "france",
    "it", "italy", "jp", "japan", "cn", "china", "sw", "sweden", "il", "israel",
}
_known_aircraft_keys = None


def _normalize_dataset_id(raw):
    parts = [p for p in (raw or "").split("_") if p]
    if len(parts) > 1 and parts[0].lower() in _VEHICLE_ID_NATION_PREFIXES:
        parts = parts[1:]
    return re.sub(r"[^A-Za-z0-9]", "", "".join(parts)).upper()


def _load_known_aircraft_keys():
    # Lazy + cached: read once per process, not once per message. If the
    # dataset is missing or unreadable, this quietly degrades to "no extra
    # coverage" rather than breaking message parsing.
    global _known_aircraft_keys
    if _known_aircraft_keys is not None:
        return _known_aircraft_keys
    try:
        data = json.loads(_VEHICLES_JSON_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _known_aircraft_keys = []
        return _known_aircraft_keys
    keys = set()
    for v in data:
        if v.get("type") in _AIR_VEHICLE_TYPES:
            norm = _normalize_dataset_id(v.get("id"))
            if len(norm) >= 3:
                keys.add(norm)
    _known_aircraft_keys = sorted(keys)
    return _known_aircraft_keys


def is_known_aircraft_name(vehicle_name):
    """Checks a kill-feed display name against every fighter/bomber/assault/
    attack_helicopter/utility_helicopter in the vehicles.json dataset (3000+
    vehicles) - the same substring-matching approach used by the frontend's
    wiki-link lookup, since the dataset only has WT's internal ids, not the
    display text the kill feed actually uses (see frontend/app.js's
    vehicleCandidates for the full reasoning). This is what actually makes
    fixed-wing detection viable: confirmed live, a real air kill (an A-7E
    against the local player) was missed entirely because the old fallback
    only recognized planes the local player had personally flown before -
    zero coverage for anything else. This dataset-backed check alone matches
    1400+ aircraft/helicopters regardless of what the player has flown."""
    norm = normalize_vehicle_name(vehicle_name)
    if len(norm) < 3:
        return False
    for key in _load_known_aircraft_keys():
        if norm == key or norm in key or key in norm:
            return True
    return False

# Mirrors the verb list in frontend/app.js's COMBAT_VERB_PATTERN - kept in sync by
# hand since WT's exact vocabulary of hit-log verbs isn't documented.
COMBAT_VERB_RE = re.compile(
    r"\b(destroyed|shot down|critically damaged|severely damaged|"
    r"set afire|knocked out|wrecked|sunk|damaged|crashed)\b",
    re.IGNORECASE,
)
# "crashed" counts as losing the vehicle for roster/death-count purposes, same
# as destroyed/shot down/etc - it's just never something another player did to
# you (see CRASHED_RE below), so it's excluded from is_air_vehicle-style
# attacker-side logic elsewhere by construction (no actor to attribute it to).
LETHAL_RE = re.compile(r"destroyed|shot down|knocked out|wrecked|sunk|crashed", re.IGNORECASE)
POSITION_MATCH_RADIUS = 0.01  # fraction-of-map distance; "same object" between samples

# "<name> (<vehicle>) has been wrecked" is passive voice with no attacker at all
# (environmental loss - ramming, drowning, flipping, etc.), unlike the active
# "<actor> (<vehicle>) destroyed <target> (<vehicle>)" form. Confirmed from a
# real battle log; without detecting this, the named vehicle was wrongly
# parsed as a targetless "actor", which showed up as "(unparsed message)" in
# the Enemy Roster whenever it happened to be the local player.
PASSIVE_MARKER_RE = re.compile(r"\bhas been\s*$", re.IGNORECASE)

# "crashed" has no attacker by definition (you crash your own vehicle) and
# doesn't necessarily use "has been" phrasing the way "wrecked" does - WT's
# exact wording isn't documented, so rather than guess the precise phrasing,
# this always treats it as passive regardless of what (if anything) precedes
# the verb. _split_name_and_vehicle only looks at text between the first "("
# and last ")" anyway, so stray leading words ("has", "has been", nothing at
# all) are harmless either way.
CRASHED_RE = re.compile(r"\bcrashed\b", re.IGNORECASE)

NATION_CODES = {"ussr", "us", "usa", "germany", "britain", "uk", "japan", "china", "italy", "france", "sweden", "israel"}


def _split_name_and_vehicle(segment):
    """'Mattmacouille (Leopard 2 (PzBtl 123))' -> ('Mattmacouille', 'Leopard 2 (PzBtl 123)').
    Vehicle descriptions can themselves contain parens, so this takes the first
    '(' as the opening bound and the LAST ')' in the segment as the closing bound,
    rather than naively matching the first balanced pair. AI-controlled vehicles
    in solo modes have no player name at all, e.g. 'Leopard 2A4' with no parens -
    that returns (None, segment).
    """
    segment = segment.strip()
    first_paren = segment.find("(")
    last_paren = segment.rfind(")")
    if first_paren == -1 or last_paren == -1 or last_paren < first_paren:
        return None, segment or None
    name = segment[:first_paren].strip() or None
    vehicle = segment[first_paren + 1:last_paren].strip() or None
    return name, vehicle


def parse_combat_message(msg):
    """Returns None for non-combat messages (achievements, disconnects, etc.)
    that don't contain one of our known verbs. Otherwise returns a dict with
    verb/actor_name/actor_vehicle/target_name/target_vehicle - any of the name
    fields may be None for AI opponents that have no player name."""
    if not msg:
        return None
    m = COMBAT_VERB_RE.search(msg)
    if not m:
        return None
    verb = m.group(0).lower()
    left = msg[:m.start()].strip()
    right = msg[m.end():].strip()

    passive = PASSIVE_MARKER_RE.search(left)
    if passive or CRASHED_RE.search(verb):
        # No attacker - the named vehicle is the victim, not an "actor". For a
        # plain "crashed" (no "has been" marker), prefix is the whole left
        # segment; _split_name_and_vehicle ignores anything outside the
        # first "(" / last ")" bounds regardless, so this is safe either way.
        prefix = left[:passive.start()] if passive else left
        target_name, target_vehicle = _split_name_and_vehicle(prefix.strip())
        actor_name, actor_vehicle = None, None
    else:
        actor_name, actor_vehicle = _split_name_and_vehicle(left)
        target_name, target_vehicle = _split_name_and_vehicle(right)

    return {
        "verb": verb,
        "actor_name": actor_name,
        "actor_vehicle": actor_vehicle,
        "target_name": target_name,
        "target_vehicle": target_vehicle,
    }


# Helicopters are a small, distinctly-named set (unlike fixed-wing aircraft,
# which number in the hundreds and don't follow one naming convention), so a
# keyword list is reasonably reliable here. This is necessarily incomplete -
# expand it if a real enemy helicopter is ever missed.
HELICOPTER_KEYWORDS = (
    "mi-", "ka-", "ah-", "uh-", "oh-", "ch-", "wz-", "z-9", "z-10", "z-11", "z-19",
    "mangusta", "apache", "cobra", "tiger", "wildcat", "gazelle", "alouette",
    "lynx", "merlin", "rooivalk", "cheetah", "hind", "hokum", "havoc", "hip",
    "bo 105", "bo105", "a129", "sa330", "sa342", "s-70", "s-58", "s-55",
)


def is_helicopter_name(name):
    n = (name or "").lower()
    return any(k in n for k in HELICOPTER_KEYWORDS)


# UCAVs/drones (MQ-1, MQ-9, etc.) are a distinct air-threat category that
# helicopter keywords don't cover at all - confirmed missed live (MQ-1 tested
# False against is_air_vehicle before this was added). US "MQ-"/"RQ-" drone
# naming is the family most likely to show up in WT; non-US UCAV names aren't
# covered here and would need adding if one's ever missed.
DRONE_PREFIXES = ("mq-", "rq-")


def is_drone_name(name):
    n = (name or "").lower()
    return any(n.startswith(p) for p in DRONE_PREFIXES)


def is_lethal(verb):
    return bool(verb and LETHAL_RE.search(verb))


def normalize_vehicle_name(name):
    """Strip all non-alphanumerics and uppercase, e.g. 'BMD-4M' -> 'BMD4M'."""
    return re.sub(r"[^A-Za-z0-9]", "", name or "").upper()


def normalize_vehicle_id(type_str):
    """Same normalized form as normalize_vehicle_name, but starting from WT's raw
    internal id ('tankModels/ussr_bmd_4m') instead of the display text a kill-feed
    message uses ('BMD-4M') - strips the category path and a leading nation code
    first so both forms converge on the same normalized string for comparison."""
    if not type_str:
        return ""
    after_slash = type_str.split("/")[-1]
    parts = [p for p in after_slash.split("_") if p]
    if len(parts) > 1 and parts[0].lower() in NATION_CODES:
        parts = parts[1:]
    return normalize_vehicle_name("".join(parts))


def is_air_vehicle(vehicle_name, known_armies=None):
    """Best-effort "is this named vehicle an aircraft, helicopter, or drone"
    check for warning purposes. Helicopters and drones/UCAVs are matched
    reliably via name keywords; fixed-wing aircraft are checked against the
    vehicles.json dataset (see is_known_aircraft_name) - real, broad coverage
    (1400+ aircraft/helicopters) that isn't limited to what the local player
    has personally flown. known_armies (built from the player's own session
    history) is kept as a fallback for anything the dataset doesn't have or
    that's since gone stale - cheap extra coverage, not the primary path
    anymore."""
    if is_helicopter_name(vehicle_name) or is_drone_name(vehicle_name):
        return True
    if is_known_aircraft_name(vehicle_name):
        return True
    if not known_armies:
        return False
    norm = normalize_vehicle_name(vehicle_name)
    for vehicle_type, army in known_armies.items():
        if army == "air" and normalize_vehicle_id(vehicle_type) == norm:
            return True
    return False


# Only these types represent actual vehicles. capture_zone/respawn_base_*/
# airfield/bombing_point are static map features that are ALSO colored red or
# blue depending on which side owns them - treating them as classifiable
# "vehicles" was a real bug: the kill-location algorithm picked up a red
# capture_zone as the "nearest enemy" and reported its fixed position as a
# kill location, which is meaningless and produced a bogus low-confidence
# result that looked like a real (if uncertain) enemy sighting.
VEHICLE_TYPES = {"ground_model", "aircraft", "ship"}


def classify_faction(obj):
    if obj.get("type") not in VEHICLE_TYPES:
        return "unknown"
    if obj.get("icon") == "Player":
        return "self"
    rgb = obj.get("color[]")
    if not rgb or len(rgb) < 3:
        return "unknown"
    r, g, b = rgb[:3]
    # Observed enemy markers are pure/near-pure red (e.g. #f00000, #fa0000); the
    # player's own marker is gold (high G too), so low G+B is the enemy signal.
    # Untested against real allied units - this is a best-effort heuristic, not
    # a confirmed color contract from the game.
    if r > 180 and g < 100 and b < 100:
        return "enemy"
    return "friendly"


def _dist(a, b):
    return math.hypot(a.get("x", 0) - b.get("x", 0), a.get("y", 0) - b.get("y", 0))


def compute_kill_locations(session_id: int):
    start_ts = db.get_session_start_ts(session_id)
    if start_ts is None:
        return []

    my_name = db.get_session_my_name(session_id)
    session_data = db.get_session(session_id)
    events = session_data["events"] if session_data else []
    samples = db.get_session_telemetry(session_id)
    parsed_samples = [
        (row["ts"], json.loads(row["map_obj_json"] or "[]")) for row in samples
    ]
    parsed_samples.sort(key=lambda s: s[0])

    results = []
    for e in events:
        if e["kind"] != "damage" or not my_name or e["actor_name"] != my_name:
            continue
        if not is_lethal(e["verb"]) or e["match_time"] is None:
            continue
        target = e["target_name"] or e["target_vehicle"]
        if not target:
            continue

        approx_wall = start_ts + e["match_time"]
        before, after = None, None
        for ts, objs in parsed_samples:
            if ts <= approx_wall:
                before = (ts, objs)
            else:
                after = (ts, objs)
                break
        if before is None:
            continue

        player_pos = next((o for o in before[1] if o.get("icon") == "Player"), None)
        enemies_before = [o for o in before[1] if "x" in o and classify_faction(o) == "enemy"]
        if not enemies_before:
            continue

        candidates = enemies_before
        confidence = "low"
        if after is not None:
            enemies_after = [o for o in after[1] if "x" in o and classify_faction(o) == "enemy"]
            disappeared = [
                o for o in enemies_before
                if not any(_dist(o, o2) < POSITION_MATCH_RADIUS for o2 in enemies_after)
            ]
            if disappeared:
                candidates = disappeared
                confidence = "high"

        if player_pos:
            candidates = sorted(candidates, key=lambda o: _dist(o, player_pos))
        chosen = candidates[0]

        results.append({
            "event_id": e["id"],
            "target": target,
            "verb": e["verb"],
            "match_time": e["match_time"],
            "x": chosen["x"],
            "y": chosen["y"],
            "confidence": confidence,
        })

    return results


def build_player_roster(session_id: int):
    """Per-player friendly/enemy roster built entirely from the kill feed - no
    map_obj/position data needed, which matters since real enemy positions
    aren't exposed there at all (confirmed live: absent even at the exact
    moment enemies were visible in-game).

    Team is inferred by 2-coloring the "who fought whom" graph starting from
    the player's own resolved name: standard WT team modes disable friendly
    fire, so every combat interaction between two named players is between
    OPPOSING teams by construction. This is more reliable than /hudmsg's own
    "enemy" flag, which was confirmed to read false for every message in a
    real match regardless of actual team. Coverage is incomplete for players
    who never fought (directly or transitively) anyone connected back to the
    local player - those come back as "unknown".

    Each player spawns in with their own vehicle lineup, and each vehicle in
    it gets one normal life plus one paid "premium token" respawn of the same
    vehicle - so a given vehicle can die at most twice before it's gone from
    their lineup for the match. Deaths are tracked PER VEHICLE (not a flat
    total across all of a player's vehicles) so the UI can mark each vehicle
    with how many of its (up to two) lives have been spent.
    """
    my_name = db.get_session_my_name(session_id)
    provisional = not my_name
    if provisional:
        # Confirmed identity hasn't locked in yet (waits for a vehicle switch,
        # a timer, or match end - see poller.py) - fall back to the fast,
        # non-authoritative guess so the roster isn't sitting completely empty
        # for a long stretch of most matches. Rebuilt fresh from stored events
        # every fetch, so this self-corrects the moment the confirmed name
        # lands, with no stale cache to invalidate.
        my_name = db.get_session_provisional_my_name(session_id)
    if not my_name:
        return {"friendly": [], "enemy": [], "unknown": [], "provisional": provisional}

    session_data = db.get_session(session_id)
    events = [e for e in (session_data["events"] if session_data else []) if e["kind"] == "damage"]
    events.sort(key=lambda e: (e["match_time"] is None, e["match_time"] or 0))

    graph = defaultdict(set)
    for e in events:
        a, t = e["actor_name"], e["target_name"]
        if a and t:
            graph[a].add(t)
            graph[t].add(a)

    team = {my_name: "friendly"}
    queue = deque([my_name])
    while queue:
        node = queue.popleft()
        opposite = "enemy" if team[node] == "friendly" else "friendly"
        for neighbor in graph[node]:
            if neighbor not in team:
                team[neighbor] = opposite
                queue.append(neighbor)

    players = {}
    for e in events:
        verb = e["verb"]
        lethal = is_lethal(verb)
        match_time = e["match_time"]
        for role, name, vehicle in (
            ("actor", e["actor_name"], e["actor_vehicle"]),
            ("target", e["target_name"], e["target_vehicle"]),
        ):
            if not name:
                continue
            p = players.setdefault(name, {
                "vehicles": {}, "current_vehicle": None, "last_time": None,
            })
            if vehicle:
                p["vehicles"].setdefault(vehicle, 0)
                p["current_vehicle"] = vehicle
            if role == "target" and lethal and vehicle:
                p["vehicles"][vehicle] += 1
            if match_time is not None:
                p["last_time"] = match_time

    result = {"friendly": [], "enemy": [], "unknown": []}
    for name, info in players.items():
        # Local player included too (per request) - lands in "friendly" like
        # anyone else since team[my_name] is seeded as "friendly" below, using
        # the exact same event-derived vehicle/death data as every other
        # player rather than a special case.
        bucket = team.get(name, "unknown")
        vehicles = [{"vehicle": v, "deaths": d} for v, d in info["vehicles"].items()]
        result[bucket].append({
            "name": name,
            "vehicles": vehicles,
            "current_vehicle": info["current_vehicle"],
            "last_time": info["last_time"],
        })

    for bucket in result.values():
        bucket.sort(key=lambda p: -(p["last_time"] or 0))

    result["provisional"] = provisional
    return result
