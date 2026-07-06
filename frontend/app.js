const SKIP_KEYS = new Set(["valid", "army", "type"]);

// Per-field display overrides for /indicators values that are booleans or
// enable/disable flags under the hood rather than continuous measurements.
// Semantics for driving_direction_mode are inferred from WT's UI (a
// driver-selected gearbox mode), not from official docs - best-effort.
const FIELD_META = {
  gear_neutral: { type: "bool", onLabel: "NEUTRAL", offLabel: "IN GEAR" },
  has_speed_warning: { type: "bool", onLabel: "WARNING", offLabel: "OK" },
  driving_direction_mode: { type: "bool", onLabel: "REVERSE", offLabel: "FORWARD" },
  // This is a UI-capability flag ("should the client draw roll/bank indicators"),
  // not a telemetry value - it's always false for ground vehicles and isn't
  // meaningful to show as its own tile.
  roll_indicators_is_available: { hide: true },
  // These are rendered as colored Function badges instead (see FUNCTION_SPECS)
  // rather than as plain tiles, so hide their raw form to avoid duplication.
  stabilizer: { hide: true },
  cruise_control: { hide: true },
  lws: { hide: true },
  ircm: { hide: true },
  gear_lamp_down: { hide: true },
  gear_lamp_up: { hide: true },
  gear_lamp_off: { hide: true },
  weapon2: { hide: true },
  weapon3: { hide: true },
};

// Discrete on/off "systems" as opposed to continuous telemetry - each spec is
// shown as a colored badge (green = enabled & good, grey = present but not
// enabled, red = disabled/inoperable) and is only shown at all when its
// backing field(s) are present for this vehicle, per-request: don't show
// functions a given vehicle doesn't have.
//
// Semantics beyond plain on/off are inferred, not documented by WT:
// - gunner_state/driver_state nonzero is assumed to mean that crew member is
//   incapacitated, used to flag stabilizer/cruise-control as RED (disabled)
//   rather than merely off. Unconfirmed against a real wounded-crew sample.
// - LWS/IRCM have no known failure signal, so they're GREEN whenever equipped
//   (-1 = not equipped = hidden) with the raw CLEAR/WARNING or OFF/ACTIVE
//   reading kept as the note text rather than driving the color.
// - Landing gear lamps are mutually-exclusive tri-state lights; "in transit"
//   (gear_lamp_off) is treated as RED since neither locked position is
//   confirmed, which may not match how WT actually intends that lamp.
const FUNCTION_SPECS = [
  {
    id: "stabilizer",
    label: "Gun Stabilizer",
    isPresent: (s, i) => i.stabilizer !== undefined,
    evaluate: (s, i) => {
      if (i.gunner_state) return { status: "red", note: "gunner down" };
      return i.stabilizer ? { status: "green" } : { status: "grey" };
    },
  },
  {
    id: "cruise_control",
    label: "Cruise Control",
    isPresent: (s, i) => i.cruise_control !== undefined,
    evaluate: (s, i) => {
      if (i.driver_state) return { status: "red", note: "driver down" };
      return i.cruise_control ? { status: "green" } : { status: "grey" };
    },
  },
  {
    id: "lws",
    label: "Laser Warning",
    isPresent: (s, i) => i.lws !== undefined && i.lws !== -1,
    evaluate: (s, i) => ({ status: "green", note: i.lws ? "WARNING" : "clear" }),
  },
  {
    id: "ircm",
    label: "IR Countermeasures",
    isPresent: (s, i) => i.ircm !== undefined && i.ircm !== -1,
    evaluate: (s, i) => ({ status: "green", note: i.ircm ? "active" : "standby" }),
  },
  {
    id: "landing_gear",
    label: "Landing Gear",
    isPresent: (s, i) => i.gear_lamp_down !== undefined || i.gear_lamp_up !== undefined || i.gear_lamp_off !== undefined,
    evaluate: (s, i) => {
      if (i.gear_lamp_down) return { status: "green", note: "down & locked" };
      if (i.gear_lamp_up) return { status: "grey", note: "up & locked" };
      if (i.gear_lamp_off) return { status: "red", note: "in transit" };
      return { status: "grey", note: "unknown" };
    },
  },
  // weapon1/weapon2/weapon3 were previously shown here as "Weapon Station N"
  // badges (persistent equipment-present indicators), but that label doesn't
  // hold up: checked against stored telemetry across every aircraft flown
  // (MiG-29, Su-25TM, F-4EJ, J-7D, Ka-50), these fields sit at 0 for nearly
  // every sample regardless of loadout, INCLUDING Su-25TM (a real multi-
  // hardpoint attack aircraft) across 719 samples - not what a stable
  // "station N equipped" flag would look like. The one case where weapon2
  // flipped to 1 (MiG-29, 10 of 888 samples) did so in brief 1-2 second
  // flickers during hard maneuvering (high G, rapid stick/pedal/pitch
  // changes) then dropped back to 0 - consistent with a momentary "trigger
  // pulled/weapon released" pulse, not a persistent station-equipped state.
  // No official field documentation exists to confirm this either way, so
  // rather than keep a confidently-labeled badge for something we can't
  // actually confirm, it's removed until there's real evidence of what it
  // means. If this needs revisiting, the raw field is still in /indicators -
  // it's just not surfaced as a badge.

  // Component damage flags. Confirmed by scanning 6500+ stored telemetry
  // samples: these keys are OMITTED entirely from /indicators when healthy
  // and only appear (always as 1.0, never 0) once that component takes
  // damage - so unlike the equipment badges above, these default to green
  // for any ground vehicle and only flip red when the field shows up live,
  // rather than being hidden until first seen.
  {
    id: "tracks",
    label: "Tracks",
    isPresent: (s, i) => i.army === "tank",
    evaluate: (s, i) => (i.track_broken ? { status: "red", note: "broken" } : { status: "green" }),
  },
  {
    id: "turret_traverse",
    label: "Turret Traverse",
    isPresent: (s, i) => i.army === "tank",
    evaluate: (s, i) => {
      if (i.h_drive_dead) return { status: "red", note: "destroyed" };
      if (i.h_drive_broken) return { status: "red", note: "damaged" };
      return { status: "green" };
    },
  },
  {
    id: "gun_elevation",
    label: "Gun Elevation",
    isPresent: (s, i) => i.army === "tank",
    evaluate: (s, i) => (i.v_drive_broken ? { status: "red", note: "damaged" } : { status: "green" }),
  },
  {
    id: "breech",
    label: "Breech",
    isPresent: (s, i) => i.army === "tank",
    evaluate: (s, i) => {
      if (i.breach_dead) return { status: "red", note: "destroyed" };
      if (i.breech_damaged) return { status: "red", note: "damaged" };
      return { status: "green" };
    },
  },
  {
    id: "barrel",
    label: "Gun Barrel",
    isPresent: (s, i) => i.army === "tank",
    evaluate: (s, i) => (i.barrel_dead ? { status: "red", note: "destroyed" } : { status: "green" }),
  },
  {
    id: "engine",
    label: "Engine",
    isPresent: (s, i) => i.army === "tank",
    evaluate: (s, i) => {
      if (i.engine_dead) return { status: "red", note: "destroyed" };
      if (i.engine_broken) return { status: "red", note: "damaged" };
      return { status: "green" };
    },
  },
  {
    id: "transmission",
    label: "Transmission",
    isPresent: (s, i) => i.army === "tank",
    evaluate: (s, i) => (i.transmission_broken ? { status: "red", note: "damaged" } : { status: "green" }),
  },
  {
    id: "crew_repair",
    label: "Crew Repair",
    isPresent: (s, i) => !!i.is_repairing,
    evaluate: (s, i) => ({
      status: "grey",
      note: i.repair_time !== undefined ? `${Math.ceil(i.repair_time)}s left` : "in progress",
    }),
  },
];

function computeFunctions(state, indicators) {
  return FUNCTION_SPECS
    .filter((spec) => spec.isPresent(state, indicators))
    .map((spec) => ({ label: spec.label, ...spec.evaluate(state, indicators) }));
}

function renderFunctions(state, indicators) {
  const container = document.getElementById("functions-list");
  const functions = computeFunctions(state, indicators);
  if (!functions.length) {
    container.innerHTML = "";
    return;
  }
  container.innerHTML = functions.map((f) => `
    <div class="func-badge status-${f.status}">
      <span class="dot"></span>
      <span class="func-text">
        <span class="func-label">${f.label}</span>
        ${f.note ? `<span class="func-note">${f.note}</span>` : ""}
      </span>
    </div>
  `).join("");
}

function metaFormat(key, value) {
  const meta = FIELD_META[key];
  if (!meta) return null;
  if (meta.type === "bool") return value ? meta.onLabel : meta.offLabel;
  if (meta.type === "signed") {
    if (value === meta.naValue) return meta.naLabel;
    return value ? meta.onLabel : meta.offLabel;
  }
  return null;
}

// "live" = follow the current WS feed; a number = pinned to a past session's
// stored data. Session Stats / Event Log / Enemy Roster / Friendly Roster /
// minimap kill-markers all key off this. The live gauge panels (Flight State,
// Instruments, Mission, Map Info) always reflect your *current* vehicle -
// there's no full telemetry replay scrubber here, only the stats/kills/rosters
// are browsable per past session.
let viewedSessionId = "live";
let liveSessionId = null;
let lastIndicatorKeys = "";
let lastMapInfoKeys = "";

function fmtLabel(key) {
  return key.replace(/_/g, " ").replace(/,.*$/, "").trim();
}

function fmtUnit(key) {
  const m = key.match(/,\s*(.+)$/);
  return m ? m[1] : "";
}

function fmtValue(v) {
  if (typeof v === "number") {
    return Math.abs(v) >= 1000 ? v.toFixed(0) : v.toFixed(2);
  }
  if (Array.isArray(v)) {
    return v.map((n) => (typeof n === "number" ? n.toFixed(1) : n)).join(", ");
  }
  return String(v);
}

function renderTiles(container, obj, keyCache) {
  // The Instruments panel (tiles-indicators) was pulled from the page per
  // request but the render call below is left in place so it's a one-line
  // HTML uncomment to bring back - this guard just makes that call a no-op
  // instead of throwing against a missing element.
  if (!container) return;
  const keys = Object.keys(obj).filter((k) => !SKIP_KEYS.has(k) && !FIELD_META[k]?.hide);
  const keySig = keys.join(",");
  if (keySig !== keyCache.value) {
    keyCache.value = keySig;
    container.innerHTML = "";
    if (keys.length === 0) {
      container.innerHTML = '<div class="empty-note">No fields reported for this vehicle type</div>';
      return;
    }
    for (const key of keys) {
      const tile = document.createElement("div");
      tile.className = "tile";
      tile.dataset.key = key;
      const unit = FIELD_META[key] ? "" : fmtUnit(key);
      tile.innerHTML = `<div class="label">${fmtLabel(key)}</div><div class="value"><span class="v"></span><span class="unit">${unit}</span></div>`;
      container.appendChild(tile);
    }
  }
  for (const key of keys) {
    const tile = container.querySelector(`[data-key="${CSS.escape(key)}"]`);
    if (tile) {
      const overridden = metaFormat(key, obj[key]);
      tile.querySelector(".v").textContent = overridden !== null ? overridden : fmtValue(obj[key]);
    }
  }
}

// WT internal vehicle IDs look like "tankModels/ussr_t_80ud" or "ka_50". Strip any
// category path prefix, drop a leading nation code, then split remaining segments
// on letter/digit boundaries so "80ud" becomes "80 UD" - turns raw IDs into
// reasonably readable names ("T 80 UD", "KA 50") without a per-vehicle lookup table.
const NATION_CODES = new Set(["ussr", "us", "usa", "germany", "britain", "uk", "japan", "china", "italy", "france", "sweden", "israel"]);

function fmtVehicleName(type) {
  if (!type) return "-";
  const afterSlash = type.includes("/") ? type.slice(type.lastIndexOf("/") + 1) : type;
  let parts = afterSlash.split("_").filter(Boolean);
  if (parts.length > 1 && NATION_CODES.has(parts[0].toLowerCase())) {
    parts = parts.slice(1);
  }
  const tokens = parts.flatMap((p) => p.match(/[a-zA-Z]+|[0-9]+/g) || [p]);
  return tokens.map((t) => t.toUpperCase()).join(" ");
}

// Vehicle id lookup - dataset pulled from a third-party War Thunder vehicles
// API (wtvehiclesapi.duckdns.org, itself built from public game datamines)
// since neither telemetry nor the kill feed exposes a vehicle's real internal
// id, premium status, or a wiki link at all. Snapshotted to vehicles.json
// (3212 entries, every vehicle not just premiums) rather than queried live,
// so it'll drift as new vehicles release - regenerate that file to refresh
// it. Powers two things: the gold "premium" highlight, and the vehicle-name
// link to wiki.warthunder.com/unit/{id}.
//
// Matching is inherently approximate: the dataset only has WT's internal ids
// (e.g. "ussr_t_72av_turms"), not the display text the kill feed actually
// uses ("T-72AV (TURMS-T)") - those two don't always normalize to identical
// strings (the id's "turms" vs. the display's "TURMS-T", for example).
// Nation-prefix stripping + substring containment (checked both directions,
// preferring an exact match, then the closest length match) recovers most of
// these near-misses; a >=4-char floor on both sides keeps short ids (e.g.
// "M4") from matching unrelated vehicles by coincidence. This can still miss
// a vehicle or, rarely, pick the wrong one when several share a long
// substring - it's a best-effort match, not a certified id.
const VEHICLE_ID_NATION_PREFIXES = new Set([
  "ussr", "usa", "us", "germ", "germany", "uk", "britain", "fr", "france",
  "it", "italy", "jp", "japan", "cn", "china", "sw", "sweden", "il", "israel",
]);

function normalizeVehicleId(raw) {
  const parts = (raw || "").split("_").filter(Boolean);
  if (parts.length > 1 && VEHICLE_ID_NATION_PREFIXES.has(parts[0].toLowerCase())) {
    parts.shift();
  }
  return parts.join("").replace(/[^A-Za-z0-9]/g, "").toUpperCase();
}

function normalizeDisplayName(name) {
  return (name || "").replace(/[^A-Za-z0-9]/g, "").toUpperCase();
}

let vehicleIndex = [];

fetch("/static/vehicles.json")
  .then((r) => r.json())
  .then((list) => {
    // Floor of 3 (not 4) so short-but-real candidate ids aren't dropped
    // before matching ever gets a chance - "Q-5L" normalizes to just "Q5L"
    // and was silently unmatchable while this sat at 4.
    vehicleIndex = list
      .map((v) => ({ id: v.id, norm: normalizeVehicleId(v.id), premium: !!v.premium, country: v.country || null }))
      .filter((e) => e.norm.length >= 3);
  })
  .catch(() => {
    // Best-effort feature - if the dataset fails to load, vehicles simply
    // don't get highlighted/linked rather than breaking the roster.
  });

// Manual overrides for cases normalization can never bridge, because the
// kill feed's display text and the id are just different strings for the
// same vehicle rather than punctuation/spacing variants of one string:
// - export/NATO reporting names vs. the internal GRAU-style designation
//   ("Osa-AKM" vs. "9A33BM3" - no shared substring at all), which can also
//   be a genuine multi-nation tie (several nations field the exact same
//   named export vehicle) - value is a { country: id } map for those, or a
//   plain id string when there's only one real answer
// - a trials-unit/event name where the wiki has no separate page, so the
//   correct link is the base vehicle it's actually built on ("Leopard 2
//   (OTCo)" -> the same page as "Leopard 2A4NL")
// - a nickname alone, missing the base vehicle name entirely and using a
//   different transliteration to boot ("Ra'am Sagol" vs. the id's "raam
//   segol", part of "Merkava Mk.3 Raam Segol")
// Add entries here as they're reported rather than trying to guess at fuzzy
// matching for cases like this - each one is a one-off, not a pattern.
const VEHICLE_NAME_ALIASES = {
  OSAAKM: { ussr: "ussr_9a33bm3", italy: "it_9a33bm3" },
  LEOPARD2OTCO: "fr_leopard_2a4nl_les",
  RAAMSAGOL: "il_merkava_mk_3_raam_segol",
};

// Returns every plausible candidate for a display name, not just one - lets
// callers disambiguate (e.g. by the player's other, unambiguous vehicles)
// rather than baking in a single guess. Length-1 means "confident" (an exact
// match, or an alias with only one real answer); more than that means a real
// tie a caller should try to break with context if it has any.
function vehicleCandidates(displayName) {
  const norm = normalizeDisplayName(displayName);
  const alias = VEHICLE_NAME_ALIASES[norm];
  if (alias) {
    const ids = typeof alias === "string" ? [alias] : Object.values(alias);
    return ids.map((id) => vehicleIndex.find((e) => e.id === id) || { id, norm, premium: false, country: null });
  }
  // Floor is on the DISPLAY name only enough to reject 1-2 char noise ("T",
  // "88") - genuine vehicle designators are frequently this short ("2S6",
  // "T34", "IS2", "KV1"), and rejecting them at <4 chars was a real bug (2S6
  // never matched anything, silently). The vehicleIndex entries are already
  // pre-filtered at load time, which is what actually guards against short
  // substrings like "M4" matching coincidentally - a short display name can
  // still only match a *longer* candidate id containing it, never the
  // reverse.
  if (norm.length < 3) return [];
  const matches = [];
  for (const entry of vehicleIndex) {
    if (entry.norm === norm) return [entry]; // exact match - unambiguous, stop here
    if (norm.includes(entry.norm) || entry.norm.includes(norm)) {
      matches.push({ entry, diff: Math.abs(entry.norm.length - norm.length) });
    }
  }
  if (!matches.length) return [];
  matches.sort((a, b) => a.diff - b.diff);
  const bestDiff = matches[0].diff;
  return matches.filter((m) => m.diff === bestDiff).map((m) => m.entry);
}

// preferredCountry: an already-confident vehicle elsewhere in the same
// player's lineup, if any - see inferPlayerCountry. Only matters when
// vehicleCandidates() comes back with a genuine tie.
function findVehicleMatch(displayName, preferredCountry) {
  const candidates = vehicleCandidates(displayName);
  if (!candidates.length) return null;
  if (candidates.length === 1 || !preferredCountry) return candidates[0];
  return candidates.find((c) => c.country === preferredCountry) || candidates[0];
}

// A player's own vehicle lineup is always a single nation (you can't mix
// e.g. USSR and Italian vehicles in one lineup), even though a TEAM can mix
// nations across players - so if any other vehicle this player has driven
// resolves unambiguously, that nation is a reliable tiebreaker for an
// otherwise-ambiguous one (like an Osa-AKM matching both a ussr and an it id).
function inferPlayerCountry(vehicles) {
  for (const v of vehicles || []) {
    const candidates = vehicleCandidates(v.vehicle);
    if (candidates.length === 1 && candidates[0].country) {
      return candidates[0].country;
    }
  }
  return null;
}

function isPremiumVehicle(displayName, preferredCountry) {
  const match = findVehicleMatch(displayName, preferredCountry);
  return !!(match && match.premium);
}

function vehicleWikiUrl(displayName, preferredCountry) {
  const match = findVehicleMatch(displayName, preferredCountry);
  return match ? `https://wiki.warthunder.com/unit/${match.id}` : null;
}

// WT nicknames never contain whitespace themselves, so a clan tag (however
// it's bracketed - "[TAG]", "^TAG^", "=TAG=", ".TAG.", etc - is reliably
// whatever precedes the LAST whitespace-separated token; the token itself is
// the real nickname the community profile URL needs.
function playerProfileUrl(name) {
  const trimmed = (name || "").trim();
  if (!trimmed) return null;
  const nick = trimmed.split(/\s+/).pop();
  if (!nick) return null;
  return `https://warthunder.com/en/community/userinfo/?nick=${encodeURIComponent(nick)}`;
}

// Only these types are actual vehicles. capture_zone/respawn_base_*/airfield
// are static map features colored red or blue depending on which side owns
// them - treating them as classifiable "vehicles" was a real bug (a red
// capture_zone got tracked as a stale "enemy contact" alongside real units).
const VEHICLE_TYPES = new Set(["ground_model", "aircraft", "ship"]);

// Best-effort faction read from a map_obj entry's color - mirrors
// backend/analysis.py's classify_faction(). Untested against real allied units
// (Test Drive has none), so "friendly" here really means "not you, not red".
function classifyFaction(obj) {
  if (!VEHICLE_TYPES.has(obj.type)) return "unknown";
  if (obj.icon === "Player") return "self";
  const rgb = obj["color[]"];
  if (!rgb || rgb.length < 3) return "unknown";
  const [r, g, b] = rgb;
  if (r > 180 && g < 100 && b < 100) return "enemy";
  return "friendly";
}

// map_obj has no unique per-vehicle ID, so "the same enemy across frames" is
// inferred by nearest-position matching each tick - same limitation as the
// kill-location heuristic elsewhere. When a previously-seen enemy has no
// match in the current frame, its last known position is kept as a "stale"
// contact (different color + an elapsed-time label) until it either
// reappears nearby (revives back to live) or ages out.
const trackedEnemyContacts = new Map();
let nextContactId = 1;
const STALE_MATCH_RADIUS = 0.02; // fraction-of-map distance to call it "the same" contact
const STALE_CONTACT_TTL_MS = 90000; // stop showing a contact 90s after last seen

function updateEnemyContactTracking(mapObj) {
  const now = Date.now();
  const currentEnemies = mapObj.filter((o) => o.x !== undefined && classifyFaction(o) === "enemy");
  const matchedIds = new Set();

  for (const enemy of currentEnemies) {
    let bestId = null, bestDist = Infinity;
    for (const [id, c] of trackedEnemyContacts) {
      if (matchedIds.has(id)) continue;
      const d = Math.hypot(enemy.x - c.x, enemy.y - c.y);
      if (d < STALE_MATCH_RADIUS && d < bestDist) { bestDist = d; bestId = id; }
    }
    const id = bestId !== null ? bestId : nextContactId++;
    trackedEnemyContacts.set(id, {
      x: enemy.x, y: enemy.y, icon: enemy.icon, color: enemy.color,
      lastSeenTs: now, live: true,
    });
    matchedIds.add(id);
  }

  for (const [id, c] of trackedEnemyContacts) {
    if (!matchedIds.has(id)) c.live = false;
    if (now - c.lastSeenTs > STALE_CONTACT_TTL_MS) trackedEnemyContacts.delete(id);
  }
}

function drawStaleContacts(ctx, W, H) {
  const now = Date.now();
  for (const c of trackedEnemyContacts.values()) {
    if (c.live) continue;
    const px = c.x * W, py = c.y * H;
    const ageSec = Math.round((now - c.lastSeenTs) / 1000);

    ctx.strokeStyle = "#8a7050";
    ctx.setLineDash([3, 3]);
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.arc(px, py, 5, 0, Math.PI * 2);
    ctx.stroke();
    ctx.setLineDash([]);

    ctx.font = "9px sans-serif";
    ctx.fillStyle = "#c99a5b";
    ctx.shadowColor = "#000000";
    ctx.shadowBlur = 2;
    ctx.fillText(`${c.icon && c.icon !== "none" ? c.icon : "contact"} (${ageSec}s ago)`, px + 7, py + 3);
    ctx.shadowBlur = 0;
  }
}

function renderHeader(data) {
  const dot = document.getElementById("conn-dot");
  const label = document.getElementById("conn-label");
  if (data.connected) {
    dot.classList.add("live");
    label.textContent = data.in_match ? "In Match" : "Connected (hangar)";
  } else {
    dot.classList.remove("live");
    label.textContent = "War Thunder not detected";
  }

  document.getElementById("veh-army").textContent = data.indicators.army || "-";
  document.getElementById("veh-type").textContent = fmtVehicleName(data.indicators.type);

  if (data.session_id !== liveSessionId) {
    trackedEnemyContacts.clear();
  }
  liveSessionId = data.session_id;
  if (viewedSessionId === "live") {
    updateSessionSelectValue();
  }
}

// Running max-concurrent-count per (faction, icon) seen this live connection -
// there's no unique vehicle ID in map_obj, so this is "how many of this type
// have been visible at once", not a true roster of distinct vehicles.
const liveFriendlyCounts = new Map();

function accumulateLiveFriendlies(mapObj) {
  const currentCounts = new Map();
  for (const o of mapObj) {
    if (classifyFaction(o) !== "friendly") continue;
    const key = o.icon && o.icon !== "none" ? o.icon : o.type;
    currentCounts.set(key, (currentCounts.get(key) || 0) + 1);
  }
  for (const [key, count] of currentCounts) {
    liveFriendlyCounts.set(key, Math.max(liveFriendlyCounts.get(key) || 0, count));
  }
}

// The only team attribution we trust is "whoever damages you is hostile" -
// see poller.py's _maybe_flag_air_warning - so this only ever fires for an
// air unit actually engaging the player, never a neutral "plane spotted"
// notice, since we can't reliably tell a third party's team.
function renderAirWarning(warning) {
  const banner = document.getElementById("air-warning-banner");
  if (!warning) {
    banner.classList.remove("active");
    banner.textContent = "No air threats detected";
    return;
  }
  banner.classList.add("active");
  const attacker = warning.attacker_name || "Unknown";
  const vehicle = warning.vehicle || "aircraft";
  const verb = warning.verb || "engaging";
  banner.textContent = `⚠ AIR THREAT: ${attacker} (${vehicle}) ${verb} you`;
}

function renderLive(data) {
  renderHeader(data);
  renderAirWarning(data.air_warning);
  renderFunctions(data.state || {}, data.indicators || {});
  // /state is raw flight telemetry (aileron/elevator/gear/IAS/TAS/RPM/mach/
  // altitude/etc) - never shown as numeric tiles, for ANY vehicle type,
  // matching the equipment-badge style ground vehicles already use. This was
  // previously shown for aircraft (gated on army === "air"), but that just
  // moved the same "instrumentation soup" complaint from tanks to planes -
  // the panel should show equipment/damage STATE (Function badges, from
  // /indicators) consistently, not raw gauges, regardless of what you're
  // sitting in. Aircraft genuinely expose less indicator detail than tanks
  // (no confirmed engine/control-surface damage flags in /indicators, only
  // landing gear + weapon station + crew fields), so the badge list is just
  // sparser for planes - that's a real API limitation, not something to work
  // around with guessed heuristics.
  const stateTiles = document.getElementById("tiles-state");
  if (stateTiles) stateTiles.innerHTML = "";
  renderTiles(document.getElementById("tiles-indicators"), data.indicators, { get value() { return lastIndicatorKeys; }, set value(v) { lastIndicatorKeys = v; } });
  renderTiles(document.getElementById("tiles-mapinfo"), data.map_info || {}, { get value() { return lastMapInfoKeys; }, set value(v) { lastMapInfoKeys = v; } });
  renderMission(data.mission || {});
  renderMapObjSummary(data.map_obj || []);

  if (viewedSessionId === "live") {
    accumulateLiveFriendlies(data.map_obj || []);
    renderFriendlyTiles(liveFriendlyCounts);
    updateEnemyContactTracking(data.map_obj || []);
    renderMinimap(data.map_obj || [], liveKillMarkers);
  }
}

function renderMission(mission) {
  const container = document.getElementById("tiles-mission");
  if (!container) return;
  const objectives = mission.objectives;
  let html = `<div class="tile"><div class="label">status</div><div class="value"><span class="v">${mission.status || "-"}</span></div></div>`;
  if (Array.isArray(objectives) && objectives.length) {
    html += objectives.map((o) => `<div class="tile"><div class="label">objective</div><div class="value"><span class="v" style="font-size:12px">${JSON.stringify(o)}</span></div></div>`).join("");
  } else {
    html += '<div class="empty-note">No objectives reported (not populated in this game mode)</div>';
  }
  container.innerHTML = html;
}

function renderMapObjSummary(mapObj) {
  const container = document.getElementById("tiles-mapobj-summary");
  if (!container) return;
  const counts = {};
  for (const o of mapObj) {
    const key = o.icon && o.icon !== "none" ? o.icon : o.type;
    counts[key] = (counts[key] || 0) + 1;
  }
  const keys = Object.keys(counts).sort();
  if (!keys.length) {
    container.innerHTML = '<div class="empty-note">No map objects reported</div>';
    return;
  }
  container.innerHTML = keys.map((k) => `<div class="tile"><div class="label">${fmtLabel(k)}</div><div class="value"><span class="v">${counts[k]}</span></div></div>`).join("");
}

function renderFriendlyTiles(countsMap) {
  const container = document.getElementById("tiles-friendlies");
  if (!container) return;
  if (!countsMap.size) {
    container.innerHTML = '<div class="empty-note">No friendly vehicles seen on the map yet</div>';
    return;
  }
  const keys = [...countsMap.keys()].sort();
  container.innerHTML = keys.map((k) => `<div class="tile"><div class="label">${fmtLabel(k)}</div><div class="value"><span class="v">${countsMap.get(k)}</span></div></div>`).join("");
}

// Minimap panel was removed from the page (see index.html) - its core value
// was live enemy positions, which WT's local API doesn't expose in
// multiplayer. Everything below is left in place and null-guarded rather
// than deleted, so re-adding the panel later is just an HTML uncomment.
const minimapCanvas = document.getElementById("minimap-canvas");
const minimapCtx = minimapCanvas ? minimapCanvas.getContext("2d") : null;

const terrainImg = new Image();
let terrainImgLoaded = false;
terrainImg.onload = () => { terrainImgLoaded = true; };
terrainImg.onerror = () => { terrainImgLoaded = false; };

function refreshTerrainImage() {
  if (viewedSessionId !== "live" || !minimapCanvas) return;
  terrainImg.src = `/api/map-image?t=${Date.now()}`;
}
refreshTerrainImage();
setInterval(refreshTerrainImage, 6000);

function resizeMinimapIfNeeded() {
  if (!minimapCanvas) return;
  const rect = minimapCanvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const w = Math.round(rect.width * dpr);
  const h = Math.round(rect.height * dpr);
  if (minimapCanvas.width !== w || minimapCanvas.height !== h) {
    minimapCanvas.width = w;
    minimapCanvas.height = h;
  }
}

function drawHeadingArrow(ctx, x, y, dx, dy, len, color) {
  const mag = Math.hypot(dx, dy) || 1;
  const ux = dx / mag, uy = dy / mag;
  ctx.strokeStyle = color;
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.moveTo(x, y);
  ctx.lineTo(x + ux * len, y + uy * len);
  ctx.stroke();
}

function drawDotLabel(ctx, px, py, text) {
  if (!text) return;
  ctx.font = "10px sans-serif";
  ctx.fillStyle = "#ffffff";
  ctx.shadowColor = "#000000";
  ctx.shadowBlur = 3;
  ctx.fillText(text, px + 6, py + 3);
  ctx.shadowBlur = 0;
}

// killMarkers: [{ target, x, y, seq }] where seq is this kill's 1-based index
// among kills of the same target name this session (1 -> "/", 2+ -> "X").
function drawKillMarkers(ctx, W, H, killMarkers) {
  for (const k of killMarkers) {
    const px = k.x * W, py = k.y * H;
    ctx.fillStyle = "#c94a4a";
    ctx.beginPath();
    ctx.arc(px, py, 4, 0, Math.PI * 2);
    ctx.fill();
    ctx.strokeStyle = "#ffffff";
    ctx.lineWidth = 1;
    ctx.stroke();

    const badge = k.seq >= 2 ? "X" : "/";
    ctx.font = "bold 13px sans-serif";
    ctx.fillStyle = k.seq >= 2 ? "#ff5a5a" : "#2fbf5a";
    ctx.shadowColor = "#000000";
    ctx.shadowBlur = 3;
    ctx.fillText(badge, px - 4, py - 6);
    ctx.font = "10px sans-serif";
    ctx.fillStyle = "#e8edf2";
    ctx.fillText(k.target, px + 6, py + 9);
    ctx.shadowBlur = 0;
  }
}

function renderMinimap(mapObj, killMarkers) {
  if (!minimapCanvas || !minimapCtx) return;
  resizeMinimapIfNeeded();
  const W = minimapCanvas.width, H = minimapCanvas.height;
  minimapCtx.clearRect(0, 0, W, H);

  if (terrainImgLoaded && terrainImg.naturalWidth) {
    minimapCtx.drawImage(terrainImg, 0, 0, W, H);
  } else {
    // faint reference grid shown only until real terrain image loads
    minimapCtx.strokeStyle = "#1c232c";
    minimapCtx.lineWidth = 1;
    for (let i = 1; i < 10; i++) {
      const p = (i / 10);
      minimapCtx.beginPath();
      minimapCtx.moveTo(p * W, 0); minimapCtx.lineTo(p * W, H);
      minimapCtx.moveTo(0, p * H); minimapCtx.lineTo(W, p * H);
      minimapCtx.stroke();
    }
  }

  if (viewedSessionId === "live") {
    drawStaleContacts(minimapCtx, W, H);
  }

  for (const obj of mapObj) {
    const color = obj.color || "#ffffff";

    // Respawn zones are reported as dozens of individual points covering the
    // whole spawn area (e.g. 64 separate "respawn_base_tank" entries in one
    // match) - drawing a dot+label per point produces an illegible stacked
    // pile. They're static zone markers, not vehicles, and already summarized
    // as a count in the Map Info panel, so skip them here entirely.
    if (obj.type && obj.type.startsWith("respawn_base")) continue;

    if (obj.type === "airfield" && obj.sx !== undefined) {
      minimapCtx.strokeStyle = color;
      minimapCtx.lineWidth = 3;
      minimapCtx.beginPath();
      minimapCtx.moveTo(obj.sx * W, obj.sy * H);
      minimapCtx.lineTo(obj.ex * W, obj.ey * H);
      minimapCtx.stroke();
      continue;
    }

    if (obj.x === undefined || obj.y === undefined) continue;
    const px = obj.x * W, py = obj.y * H;
    const isPlayer = obj.icon === "Player";

    if (obj.type === "bombing_point") {
      minimapCtx.fillStyle = color;
      minimapCtx.fillRect(px - 3, py - 3, 6, 6);
      continue;
    }

    const radius = isPlayer ? 5 : 3;
    minimapCtx.fillStyle = color;
    minimapCtx.beginPath();
    minimapCtx.arc(px, py, radius, 0, Math.PI * 2);
    minimapCtx.fill();

    if (isPlayer) {
      minimapCtx.strokeStyle = "#ffffff";
      minimapCtx.lineWidth = 2;
      minimapCtx.beginPath();
      minimapCtx.arc(px, py, radius + 3, 0, Math.PI * 2);
      minimapCtx.stroke();
    } else {
      // map_obj has no per-vehicle name, only a category icon (e.g. "MediumTank",
      // "Fighter") - that's the closest thing to a "name" available for live,
      // still-alive contacts. Actual model names only exist for kills (below).
      drawDotLabel(minimapCtx, px, py, obj.icon && obj.icon !== "none" ? obj.icon : obj.type);
    }

    if (obj.dx !== undefined && obj.dy !== undefined) {
      drawHeadingArrow(minimapCtx, px, py, obj.dx, obj.dy, isPlayer ? 16 : 10, color);
    }
  }

  if (killMarkers && killMarkers.length) {
    drawKillMarkers(minimapCtx, W, H, killMarkers);
  }
}

function connectWs() {
  const ws = new WebSocket(`ws://${location.host}/ws/live`);
  ws.onmessage = (ev) => renderLive(JSON.parse(ev.data));
  ws.onclose = () => setTimeout(connectWs, 1500);
  ws.onerror = () => ws.close();
}

// Combat messages are now parsed server-side (backend/analysis.py) at ingestion
// time and stored per-event as verb/actor_name/target_name, since real matches
// need the actual player names (not just vehicle types) to tell "my kill" apart
// from "an ally's kill of the same enemy vehicle type" - something WT's own
// "enemy" flag on /hudmsg turned out NOT to reliably indicate (confirmed live:
// it read false for every message in a real match, including enemy-on-ally
// kills). Older events recorded before this fix will have null verb/actor_name/
// target_name and simply won't classify - that's an accepted one-time gap for
// pre-existing test sessions, not something worth back-filling.
function isLethalVerb(verb) {
  return /destroyed|shot down|knocked out|wrecked|sunk|crashed/i.test(verb || "");
}

// Kill markers currently shown on the live minimap, refreshed periodically from
// the live session's own kill-locations endpoint.
let liveKillMarkers = [];

function killLocationsToMarkers(locations) {
  const seqByTarget = new Map();
  return locations
    .slice()
    .sort((a, b) => (a.match_time ?? 0) - (b.match_time ?? 0))
    .map((loc) => {
      const seq = (seqByTarget.get(loc.target) || 0) + 1;
      seqByTarget.set(loc.target, seq);
      return { target: loc.target, x: loc.x, y: loc.y, seq };
    });
}

async function refreshLiveKillMarkers() {
  if (viewedSessionId !== "live" || !liveSessionId) {
    liveKillMarkers = [];
    return;
  }
  try {
    const res = await fetch(`/api/stats/sessions/${liveSessionId}/kill-locations`);
    const locations = await res.json();
    liveKillMarkers = killLocationsToMarkers(locations);
  } catch {
    // transient network hiccup - keep showing the last known markers
  }
}

// Session Stats and Match History panels were pulled from the page (see
// index.html) but their render calls are left in place rather than deleted -
// this no-ops cleanly against a missing element instead of throwing, so
// re-adding either panel later is just an HTML uncomment.
function setHTML(id, html) {
  const el = document.getElementById(id);
  if (el) el.innerHTML = html;
}

async function refreshStats() {
  if (viewedSessionId !== "live") return;
  if (!liveSessionId) {
    setHTML("stat-summary", "");
    setHTML("event-log", "");
    setHTML("targets-list", "");
    return;
  }
  const res = await fetch(`/api/stats/sessions/${liveSessionId}`);
  const data = await res.json();
  if (data.error) return;
  renderSessionPanels(data.events, data.session.my_name);
}

// myName is resolved server-side by matching a kill-feed message's actor/target
// vehicle against whatever vehicle the player is currently in (see
// poller.py's _maybe_resolve_my_name) - there's no "sender" field or roster
// endpoint to read it from directly. Until a message involving the player's
// current vehicle shows up, myName is null and personal Kills/Deaths can't be
// computed yet (shows as 0/0 rather than counting ally actions as "mine").
function renderSessionPanels(events, myName) {
  const dealt = myName ? events.filter((e) => e.kind === "damage" && e.actor_name === myName) : [];
  const taken = myName ? events.filter((e) => e.kind === "damage" && e.target_name === myName) : [];

  const kills = dealt.filter((e) => isLethalVerb(e.verb)).length;
  const deaths = taken.filter((e) => isLethalVerb(e.verb)).length;
  const damageEvents = events.filter((e) => e.kind === "damage").length;

  setHTML("stat-summary", `
    <div class="stat"><span class="n">${kills}</span><span class="l">Kills</span></div>
    <div class="stat"><span class="n">${deaths}</span><span class="l">Deaths</span></div>
    <div class="stat"><span class="n">${damageEvents}</span><span class="l">Combat Msgs</span></div>
  `);

  setHTML("event-log", events.slice(-30).reverse().map((e) => {
    const text = e.msg || JSON.stringify(JSON.parse(e.raw_json));
    let cls = "other";
    if (myName && e.actor_name === myName) cls = "self";
    else if (myName && e.target_name === myName) cls = "enemy";
    return `<div class="event-row ${e.kind} ${cls}">${text}</div>`;
  }).join(""));

  renderEnemyRoster(dealt);
}

// Enemy roster grouped by the actual player name when the kill feed gives us
// one (real matches include it - only AI opponents in Test Drive/solo modes are
// nameless, in which case this falls back to grouping by vehicle type).
function renderEnemyRoster(dealtEvents) {
  const container = document.getElementById("targets-list");
  if (!container) return;
  const byTarget = new Map();
  for (const e of dealtEvents) {
    const key = e.target_name || e.target_vehicle || "(unparsed message)";
    if (!byTarget.has(key)) byTarget.set(key, []);
    byTarget.get(key).push(e);
  }
  if (!byTarget.size) {
    container.innerHTML = '<div class="empty-note">No damage dealt yet this session</div>';
    return;
  }
  container.innerHTML = [...byTarget.entries()].map(([target, hits]) => {
    const killCount = hits.filter((h) => isLethalVerb(h.verb)).length;
    const hitLog = hits.map((h) => `${h.verb || "?"}@${Math.round(h.match_time ?? 0)}s`).join(", ");
    let statusClass = "damaged", statusText = "DAMAGED";
    if (killCount === 1) { statusClass = "kill-1"; statusText = "/"; }
    else if (killCount >= 2) { statusClass = "kill-2"; statusText = "X"; }
    return `<div class="target-card">
      <div class="target-name"><span>${target}</span><span class="status ${statusClass}">${statusText}</span></div>
      <div class="hit-log">${hits.length} hit(s): ${hitLog}</div>
    </div>`;
  }).join("");
}

async function refreshHistory() {
  const res = await fetch("/api/stats/sessions");
  const sessions = await res.json();

  setHTML("history-list", sessions.map((s) => {
    const start = new Date(s.start_ts * 1000).toLocaleTimeString();
    const dur = s.end_ts ? `${Math.round(s.end_ts - s.start_ts)}s` : "active";
    return `<div class="history-row"><span>#${s.id} ${s.army || ""} ${s.vehicle_type || ""}</span><span>${start} (${dur})</span><a href="/api/stats/sessions/${s.id}/export" download>export</a></div>`;
  }).join(""));

  populateSessionSelect(sessions);
}

function populateSessionSelect(sessions) {
  const select = document.getElementById("session-select");
  const prevValue = select.value;
  select.innerHTML = '<option value="live">Live</option>' + sessions.map((s) => {
    const start = new Date(s.start_ts * 1000).toLocaleTimeString();
    return `<option value="${s.id}">#${s.id} ${s.army || "?"} ${fmtVehicleName(s.vehicle_type)} - ${start}</option>`;
  }).join("");
  select.value = prevValue && [...select.options].some((o) => o.value === prevValue) ? prevValue : "live";
  updateSessionSelectValue();
}

function updateSessionSelectValue() {
  const select = document.getElementById("session-select");
  if (viewedSessionId === "live" && select.value !== "live") {
    // stay on "Live" entry; the label itself doesn't need the live session id
  }
}

async function onSessionSelectChange() {
  const select = document.getElementById("session-select");
  const value = select.value;
  if (value === "live") {
    viewedSessionId = "live";
    refreshTerrainImage();
    refreshStats();
    refreshRoster();
    return;
  }
  viewedSessionId = Number(value);
  await loadHistoricalSession(viewedSessionId);
}

async function loadHistoricalSession(sessionId) {
  const [sessionRes, killLocRes] = await Promise.all([
    fetch(`/api/stats/sessions/${sessionId}`),
    fetch(`/api/stats/sessions/${sessionId}/kill-locations`),
  ]);
  const sessionData = await sessionRes.json();
  const killLocations = await killLocRes.json();

  if (sessionData.error) return;
  renderSessionPanels(sessionData.events, sessionData.session.my_name);
  refreshRoster();

  const historicalFriendlies = await aggregateHistoricalFriendlies(sessionId);
  renderFriendlyTiles(historicalFriendlies);

  const markers = killLocationsToMarkers(killLocations);
  terrainImgLoaded = false;
  const img = new Image();
  img.onload = () => {
    if (viewedSessionId !== sessionId) return;
    terrainImg.src = img.src;
    terrainImgLoaded = true;
    renderMinimap([], markers);
  };
  img.onerror = () => {
    renderMinimap([], markers);
  };
  img.src = `/api/stats/sessions/${sessionId}/map-image?t=${Date.now()}`;
}

async function aggregateHistoricalFriendlies(sessionId) {
  const res = await fetch(`/api/stats/sessions/${sessionId}/telemetry`);
  const samples = await res.json();
  const maxCounts = new Map();
  for (const sample of samples) {
    let mapObj;
    try {
      mapObj = JSON.parse(sample.map_obj_json || "[]");
    } catch {
      continue;
    }
    const currentCounts = new Map();
    for (const o of mapObj) {
      if (classifyFaction(o) !== "friendly") continue;
      const key = o.icon && o.icon !== "none" ? o.icon : o.type;
      currentCounts.set(key, (currentCounts.get(key) || 0) + 1);
    }
    for (const [key, count] of currentCounts) {
      maxCounts.set(key, Math.max(maxCounts.get(key) || 0, count));
    }
  }
  return maxCounts;
}

// Main focus: per-player Friendly/Enemy Roster, built entirely from the kill
// feed via backend/analysis.py's build_player_roster() (team inferred by
// 2-coloring the "who fought whom" graph from the player's own resolved
// name - see that function's docstring for why this is more reliable than
// /hudmsg's own broken "enemy" flag). Works the same for live and historical
// sessions since it's just one fetch, no live polling loop of its own beyond
// the periodic refresh below.
// side: "friendly" or "enemy" - controls two mirrored layout choices per
// request: the enemy panel is right-aligned as a whole (friendly stays
// left, as before), and within each row the player's still-living vehicle
// and their spent-lineup ("dead") vehicles sit on opposite sides, mirrored
// between the two panels so the "still alive" vehicles for both sides face
// each other across the two panels rather than both defaulting the same way.
function renderRosterList(containerId, players, emptyMessage, side) {
  const container = document.getElementById(containerId);
  if (!container) return;
  if (!players.length) {
    container.innerHTML = `<div class="empty-note">${emptyMessage}</div>`;
    return;
  }
  container.innerHTML = players.map((p) => {
    // Each vehicle in a player's lineup gets one normal life plus one paid
    // "premium token" respawn of the same vehicle, so it can die at most
    // twice before it's gone for the match: "/" marks one death, "X" marks
    // two (used up). Every distinct vehicle seen this match is listed, not
    // just the current one - the current vehicle is bolded.
    const vehicles = p.vehicles || [];
    // A single player's own lineup is always one nation, so if any of their
    // OTHER vehicles resolves confidently, that nation breaks ties for
    // otherwise-ambiguous ones (e.g. Osa-AKM) - see inferPlayerCountry.
    const playerCountry = inferPlayerCountry(vehicles);
    const fmtEntry = (v) => {
      const marker = v.deaths === 1 ? ' <span class="death-marker one">/</span>'
        : v.deaths >= 2 ? ' <span class="death-marker two">X</span>'
        : "";
      const isCurrent = v.vehicle === p.current_vehicle;
      const classes = [];
      if (isCurrent) classes.push("roster-vehicle-current");
      if (isPremiumVehicle(v.vehicle, playerCountry)) classes.push("premium-vehicle");
      const classAttr = classes.length ? ` class="${classes.join(" ")}"` : "";
      const wikiUrl = vehicleWikiUrl(v.vehicle, playerCountry);
      const label = wikiUrl
        ? `<a href="${wikiUrl}" target="_blank" rel="noopener"${classAttr}>${v.vehicle}</a>`
        : (classAttr ? `<span${classAttr}>${v.vehicle}</span>` : v.vehicle);
      return label + marker;
    };
    // "Last known living vehicle" = whichever one they're currently in
    // (current_vehicle); everything else in their lineup is grouped as
    // "dead" for layout purposes, whether it's actually exhausted (X) or
    // just not the one they're driving anymore.
    const living = vehicles.find((v) => v.vehicle === p.current_vehicle);
    const dead = vehicles.filter((v) => v.vehicle !== p.current_vehicle);
    const livingHtml = living ? fmtEntry(living) : "";
    const deadHtml = dead.length ? dead.map(fmtEntry).join(", ") : "";
    const groups = side === "enemy"
      ? `<span class="roster-vehicle living">${livingHtml}</span><span class="roster-vehicle dead">${deadHtml}</span>`
      : `<span class="roster-vehicle dead">${deadHtml}</span><span class="roster-vehicle living">${livingHtml}</span>`;
    const vehicleList = vehicles.length ? `<div class="roster-vehicle-list">${groups}</div>` : `<span class="roster-vehicle">-</span>`;
    const profileUrl = playerProfileUrl(p.name);
    const nameHtml = profileUrl
      ? `<a class="roster-name" href="${profileUrl}" target="_blank" rel="noopener">${p.name}</a>`
      : `<span class="roster-name">${p.name}</span>`;
    return `<div class="roster-row${side === "enemy" ? " align-right" : ""}">
      <div class="roster-main">
        ${nameHtml}
        ${vehicleList}
      </div>
    </div>`;
  }).join("");
}

// Independent per-panel since there's no reason picking an order for one
// side should force the same order on the other. "recent" mirrors the
// backend's own default (most recently active first); the other two are
// resorted client-side since the backend always returns recent-first.
let rosterSortMode = { friendly: "recent", enemy: "recent" };
let lastRosterData = { friendly: [], enemy: [], unknown: [] };

function sortRosterPlayers(players, mode) {
  const sorted = [...players];
  if (mode === "player") {
    sorted.sort((a, b) => a.name.localeCompare(b.name));
  } else if (mode === "vehicle") {
    const vehicleOf = (p) => p.current_vehicle || (p.vehicles[0] && p.vehicles[0].vehicle) || "";
    sorted.sort((a, b) => vehicleOf(a).localeCompare(vehicleOf(b)));
  } else {
    sorted.sort((a, b) => (b.last_time || 0) - (a.last_time || 0));
  }
  return sorted;
}

function renderRoster(data) {
  lastRosterData = data;
  renderRosterList("roster-friendly-list", sortRosterPlayers(data.friendly || [], rosterSortMode.friendly), "No friendly players logged yet this session", "friendly");
  renderRosterList("roster-enemy-list", sortRosterPlayers(data.enemy || [], rosterSortMode.enemy), "No enemy players logged yet this session", "enemy");
  // Identity resolution is fast-but-provisional until confirmed (see
  // build_player_roster) - surface that so a friendly/enemy swap here doesn't
  // read as a bug if it self-corrects a moment later.
  const badgeDisplay = data.provisional ? "inline-block" : "none";
  const friendlyBadge = document.getElementById("roster-friendly-provisional");
  const enemyBadge = document.getElementById("roster-enemy-provisional");
  if (friendlyBadge) friendlyBadge.style.display = badgeDisplay;
  if (enemyBadge) enemyBadge.style.display = badgeDisplay;
}

async function refreshRoster() {
  const sessionId = viewedSessionId === "live" ? liveSessionId : viewedSessionId;
  if (!sessionId) {
    renderRoster({ friendly: [], enemy: [] });
    return;
  }
  try {
    const res = await fetch(`/api/stats/sessions/${sessionId}/roster`);
    const data = await res.json();
    renderRoster(data);
  } catch {
    // transient network hiccup - keep showing the last known roster
  }
}

// Per-panel collapse state, saved in localStorage so the layout you set up for
// a given play style (e.g. hiding rosters during a quick solo round) persists
// across reloads. Purely a display toggle - collapsed panels keep updating
// underneath, so re-expanding shows current data immediately.
const COLLAPSE_STORAGE_KEY = "wt-dashboard-collapsed-panels";

function loadCollapsedSet() {
  try {
    return new Set(JSON.parse(localStorage.getItem(COLLAPSE_STORAGE_KEY) || "[]"));
  } catch {
    return new Set();
  }
}

function initCollapsiblePanels() {
  const collapsed = loadCollapsedSet();

  function save() {
    localStorage.setItem(COLLAPSE_STORAGE_KEY, JSON.stringify([...collapsed]));
  }

  document.querySelectorAll(".panel").forEach((panel) => {
    const h2 = panel.querySelector("h2");
    if (!h2 || !panel.id) return;

    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "collapse-btn";
    btn.setAttribute("aria-label", "Toggle section");
    // Panels with an info tooltip (see index.html) group it with the
    // collapse button in .h2-actions so flex layout stays title-left/
    // controls-right with exactly two items - append there when present.
    const actions = h2.querySelector(".h2-actions");
    (actions || h2).appendChild(btn);

    const apply = () => {
      const isCollapsed = collapsed.has(panel.id);
      panel.classList.toggle("collapsed", isCollapsed);
      btn.textContent = isCollapsed ? "+" : "−";
    };

    const toggle = () => {
      if (collapsed.has(panel.id)) collapsed.delete(panel.id);
      else collapsed.add(panel.id);
      save();
      apply();
    };

    h2.addEventListener("click", toggle);

    if (actions) {
      // Clicking anywhere in the actions cluster (info icon, the gap between
      // it and the button, or the button itself) must NOT also trigger the h2
      // listener above - discovered live that a click landing in the flex gap
      // between the icon and button has .h2-actions itself as its target, so
      // stopping propagation only on .info-icon wasn't enough to stop it from
      // reaching h2 and toggling unexpectedly. The button gets its own direct
      // handler instead of relying on bubbling to h2 at all.
      actions.addEventListener("click", (e) => e.stopPropagation());
      btn.addEventListener("click", toggle);
    }

    apply();
  });
}

initCollapsiblePanels();

document.getElementById("session-select").addEventListener("change", onSessionSelectChange);

document.getElementById("roster-friendly-sort").addEventListener("change", (e) => {
  rosterSortMode.friendly = e.target.value;
  renderRoster(lastRosterData);
});
document.getElementById("roster-enemy-sort").addEventListener("change", (e) => {
  rosterSortMode.enemy = e.target.value;
  renderRoster(lastRosterData);
});

connectWs();
setInterval(refreshStats, 2000);
setInterval(refreshHistory, 5000);
setInterval(refreshLiveKillMarkers, 5000);
setInterval(() => { if (viewedSessionId === "live") refreshRoster(); }, 3000);
refreshHistory();
refreshLiveKillMarkers();
refreshRoster();
