import asyncio
import time
import httpx

import analysis
import db

WT_BASE = "http://localhost:8111"

STATE_INTERVAL = 0.1
MAP_INTERVAL = 1.0
HUDMSG_INTERVAL = 0.5
MAP_IMAGE_INTERVAL = 5.0
AIR_WARNING_TTL = 20.0  # seconds a "you're being engaged from the air" warning stays live
# After switching vehicles (which, mid-match, almost always means we just died
# and respawned), keep matching air attacks against the vehicle we just left
# for this long - a fatal air kill's kill-feed message names the vehicle we
# died in but arrives after our telemetry already flipped to the new vehicle.
RESPAWN_MATCH_WINDOW = 15.0
# How far apart (wall seconds) a kill-feed death message and our own telemetry
# respawn can be and still be treated as the same death, for death-correlation
# identity resolution. Generous because you can sit dead for a while before
# picking your next vehicle - the message fires at death, the telemetry switch
# only at respawn. Validated offline: this width resolved the right player in
# every death case and never a same-vehicle teammate.
DEATH_CORRELATION_WINDOW = 30.0
# Wait this long (wall seconds) after detecting our death before deciding who
# it was, so near-simultaneous same-vehicle deaths (e.g. one bomb catching two
# of the same tank) are all collected first and correctly seen as ambiguous
# rather than the first-arriving one being locked in prematurely.
DEATH_SETTLE_DELAY = 3.0
# Confirmed "who am I" resolution (self._my_name) ONLY finalizes on a vehicle
# switch or match end - both are the only points where we've genuinely seen
# every kill-feed mention of that vehicle for the match, so a second name
# sharing it would already have been caught as ambiguous by then. A previous
# version also tentative-locked after a wall-clock timeout in the same
# vehicle with no ambiguity seen YET - that's unsound, not just slow: a
# confirmed real match had another player also driving the exact same
# vehicle (T-80UD) whose name reached the kill feed before the local
# player's own name did, so the timer locked onto the WRONG name, and once
# _my_name is set it never un-sets for the rest of the session. Real-time
# features that need an identity before the match ends (air warnings) use
# _provisional_my_name instead, which keeps re-evaluating and withdraws
# itself the moment a vehicle turns out ambiguous - wrong-but-temporary
# beats wrong-and-permanent.
# map_info.valid is the reliable "actually in a match" signal (see is_valid
# computation in run()) - but WT's local API doesn't clear map_obj on
# returning to hangar, it just freezes the last match's object list forever
# (confirmed live: 150 stale entries kept reappearing on every poll, 20+
# seconds into sitting in the hangar, while map_info.valid correctly read
# false the entire time) - so match-end detection can't lean on map_obj at
# all despite it looking like a reasonable second signal. This debounce
# guards the other direction: require map_info.valid to read false for this
# long before ending the match, in case it ever blips false for a single
# poll mid-match without an actual hangar return.
MATCH_END_DEBOUNCE = 3.0
# "who am I" resolution only trusts a kill-feed mention of one of our vehicles
# if it happened while we were ACTUALLY in that vehicle (see
# _maybe_resolve_my_name). Kill-feed match_time (game clock, integer seconds)
# and our own occupancy windows (wall-clock relative to session start) line up
# closely in normal play but not exactly - sampling is ~1-2s and a match that
# we joined at its very start keeps the two clocks within a few seconds. This
# slack absorbs that drift at the window edges. Validated offline against real
# sessions: with this slack every session resolved to the correct player and
# none to a same-vehicle teammate.
OWN_WINDOW_SLACK = 8.0


class TelemetryPoller:
    def __init__(self):
        self.latest = {
            "connected": False,
            "in_match": False,
            "state": {},
            "indicators": {},
            "map_obj": [],
            "map_info": {},
            "mission": {},
            "air_warning": None,
        }
        self.subscribers: set[asyncio.Queue] = set()
        self._session_id: int | None = None
        self._last_evt = 0
        self._last_dmg = 0
        self._my_name: str | None = None
        # Fast, non-authoritative guess used to populate the roster immediately,
        # since waiting for _my_name's strict confirmation (a vehicle switch, the
        # 90s tentative-lock timer, or match end) meant the roster stayed
        # completely empty for a long stretch of most matches - confirmed live,
        # it only ever populated right around the player's first kill/damage
        # event, since that's the first time ANY candidate data exists at all.
        # Refreshed continuously as messages arrive; superseded by _my_name the
        # moment that resolves for real. Can be wrong if a later message reveals
        # a collision _my_name's stricter bar would have caught - the roster
        # self-corrects on the next fetch once _my_name locks in, since it's
        # rebuilt fresh from stored events every time rather than cached.
        self._provisional_my_name: str | None = None
        self._client: httpx.AsyncClient | None = None
        self.map_image: bytes | None = None
        self.map_image_type: str = "image/jpeg"
        self._air_warning: dict | None = None
        self._match_invalid_since: float | None = None
        self._known_armies: dict = {}
        self._session_start_wall: float | None = None
        # Per normalized-vehicle-id, the list of [start, end] time windows (in
        # seconds relative to session start) during which WE personally drove
        # it - a vehicle re-entered later gets a second interval. The current
        # vehicle's newest interval stays open (end = None) until we switch
        # away. Used to time-gate "who am I" candidates: a kill-feed mention of
        # one of our vehicles only counts as possibly-us if it happened while
        # we were actually in it (see _maybe_resolve_my_name).
        self._own_vehicle_intervals: dict[str, list[list[float | None]]] = {}
        self._last_own_vehicle: str | None = None
        self._current_own_norm: str | None = None
        # The vehicle we were in immediately before the current one, and the
        # wall-clock time we switched. Used by the air warning: a fatal air
        # hit's kill-feed message names the vehicle we DIED in, but by the time
        # it arrives our telemetry already shows us respawned into the next
        # vehicle - so matching only the current vehicle would miss every fatal
        # air kill (confirmed against real data). Matching the just-previous
        # vehicle for a short window after a switch closes that gap.
        self._prev_own_norm: str | None = None
        self._prev_own_switch_wall: float | None = None
        # Per-vehicle candidate pool for "who am I" resolution - see
        # _maybe_resolve_my_name for why this replaced a simpler first-match approach.
        self._vehicle_candidates: dict[str, set[str]] = {}
        self._ambiguous_vehicles: set[str] = set()
        # Death-correlation: the highest-confidence "who am I" signal. When our
        # telemetry shows we died (vehicle switched mid-match = we respawned),
        # the kill-feed's destroyed-message that names the vehicle we died in
        # names US - anchored to our ACTUAL death, not just "someone in our
        # vehicle type." _recent_lethal buffers recent lethal messages (wall
        # time, victim name, victim vehicle norm); _pending_deaths records
        # deaths awaiting a matching message. See _correlate_deaths.
        self._recent_lethal: list[tuple[float, str, str]] = []
        self._pending_deaths: list[list] = []  # [switch_wall, norm, resolved]

    def subscribe(self) -> asyncio.Queue:
        q = asyncio.Queue(maxsize=1)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        self.subscribers.discard(q)

    async def _broadcast(self):
        for q in list(self.subscribers):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            await q.put(self.latest)

    async def _get(self, path: str, params: dict | None = None):
        try:
            resp = await self._client.get(f"{WT_BASE}{path}", params=params, timeout=1.0)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError):
            return None

    async def _poll_map_image(self):
        try:
            resp = await self._client.get(f"{WT_BASE}/map.img", timeout=2.0)
            resp.raise_for_status()
            self.map_image = resp.content
            self.map_image_type = resp.headers.get("content-type", "image/jpeg")
        except httpx.HTTPError:
            pass

    def _handle_match_transition(self, was_valid: bool, is_valid: bool, indicators: dict):
        # Note: /hudmsg's lastEvt/lastDmg ids are cumulative for the whole game client
        # run, not reset per match, so the cursor must never be reset back to 0 here -
        # doing so previously caused old, already-seen kills to be replayed into new
        # sessions. The cursor only advances forward, across session boundaries.
        if is_valid and not was_valid:
            self._session_id = db.start_session(
                indicators.get("army"), indicators.get("type"),
                map_image=self.map_image, map_image_type=self.map_image_type,
            )
            self._my_name = None
            self._provisional_my_name = None
            self._air_warning = None
            self._known_armies = db.get_known_vehicle_armies()
            self._session_start_wall = time.time()
            self._own_vehicle_intervals = {}
            self._last_own_vehicle = None
            self._current_own_norm = None
            self._prev_own_norm = None
            self._prev_own_switch_wall = None
            self._vehicle_candidates = {}
            self._ambiguous_vehicles = set()
            self._recent_lethal = []
            self._pending_deaths = []
        elif was_valid and not is_valid and self._session_id is not None:
            # Fallback for a match that ends before we ever switched vehicles
            # (so _track_own_vehicle's move-away trigger never fired) - close
            # out whatever vehicle we were last in using whatever candidates
            # were collected for it.
            if self._current_own_norm:
                self._finalize_vehicle_candidates(self._current_own_norm)
            db.end_session(self._session_id)
            self._session_id = None

    def _confirm_my_name(self, name: str):
        # Lock the confirmed identity and persist it. Also overrides a
        # provisional guess (which may have been a same-vehicle teammate) so
        # the roster corrects immediately.
        if not name or self._my_name == name:
            return
        self._my_name = name
        self._provisional_my_name = name
        if self._session_id is not None:
            db.set_session_my_name(self._session_id, name)
            db.set_session_provisional_my_name(self._session_id, name)

    def _record_lethal(self, parsed: dict):
        # Buffer a lethal kill-feed message's victim (name + vehicle) for
        # death-correlation, then see if it completes a pending death.
        name = parsed.get("target_name")
        vehicle = parsed.get("target_vehicle")
        if not name or not vehicle:
            return
        norm = analysis.normalize_vehicle_name(vehicle)
        if not norm:
            return
        now = time.time()
        self._recent_lethal.append((now, name, norm))
        # Prune anything too old to still match a pending death.
        cutoff = now - DEATH_CORRELATION_WINDOW - 5.0
        self._recent_lethal = [e for e in self._recent_lethal if e[0] >= cutoff]
        self._correlate_deaths()

    def _note_own_death(self, norm: str):
        # Called when telemetry shows we left a real vehicle mid-match (a
        # respawn, i.e. we died in `norm`). Record it and try to correlate
        # against lethal messages already seen (the death message often arrives
        # just before the respawn switch). "DUMMYPLANE" is the spawn-select
        # placeholder, not a real vehicle we could have died in - skip it.
        if not norm or norm == "DUMMYPLANE":
            return
        self._pending_deaths.append([time.time(), norm, False])
        self._correlate_deaths()

    def _correlate_deaths(self):
        # A pending death resolves when EXACTLY one distinct victim name appears
        # in the lethal buffer for the vehicle we died in, within the time
        # window. That name is us. Uniqueness is required so a same-vehicle
        # teammate dying near the same moment can't produce a false lock (it
        # just leaves the death unresolved). Higher confidence than the
        # candidate-pool path, so it's allowed to override.
        #
        # Called both when messages/deaths arrive AND every poll tick, so a
        # death still settles even if no further messages come in. A death is
        # only decided once DEATH_SETTLE_DELAY has passed since we detected it,
        # so all near-simultaneous same-vehicle deaths are in hand before we
        # judge uniqueness (otherwise the first-arriving name would lock in
        # before a second could reveal the ambiguity).
        if self._my_name is not None:
            self._pending_deaths = [d for d in self._pending_deaths if not d[2]]
            return
        now = time.time()
        for death in self._pending_deaths:
            switch_wall, norm, resolved = death
            if resolved or now - switch_wall < DEATH_SETTLE_DELAY:
                continue
            names = {
                nm for (w, nm, vn) in self._recent_lethal
                if vn == norm and abs(w - switch_wall) <= DEATH_CORRELATION_WINDOW
            }
            if not names:
                # Nothing matched yet - keep waiting (our death message may
                # still arrive) until the death ages out of the window below.
                continue
            death[2] = True  # decided (whether or not it was unique)
            if len(names) == 1:
                self._confirm_my_name(next(iter(names)))
                break
        # Drop decided deaths and ones too old to ever collect a match.
        oldest_ok = now - DEATH_CORRELATION_WINDOW - 5.0
        self._pending_deaths = [
            d for d in self._pending_deaths if not d[2] and d[0] >= oldest_ok
        ]

    def _finalize_vehicle_candidates(self, norm: str):
        # Called once we're confident we've seen every kill-feed mention of a
        # vehicle we personally drove (i.e. we've since moved on to a
        # different one, closing that vehicle's observation window). Only
        # resolves if EXACTLY one distinct name ever showed up driving it -
        # if a second name appeared before this point, _maybe_resolve_my_name
        # already flagged it ambiguous and it's permanently unusable. This is
        # the LOWER-confidence fallback: death-correlation (_correlate_deaths)
        # runs first on a switch and wins when it can.
        if self._my_name is not None or norm in self._ambiguous_vehicles:
            return
        candidates = self._vehicle_candidates.get(norm)
        if candidates and len(candidates) == 1:
            self._confirm_my_name(next(iter(candidates)))

    def _update_provisional_name(self):
        # Best current guess at "who am I", refreshed on every candidate-pool
        # change - unlike _finalize_vehicle_candidates this does NOT wait for a
        # vehicle switch/timer/match end, so it's available the moment the
        # first unambiguous candidate shows up (e.g. right after the player's
        # first kill or first time taking damage). Used only as a roster
        # fallback when _my_name hasn't confirmed yet; if a later message turns
        # a singleton bucket ambiguous, the guess is withdrawn (roster falls
        # back to empty for that name rather than keep showing a guess a
        # collision just proved wrong).
        # Prefer the current vehicle's candidate - with time-gating every clean
        # candidate should be the same name (us), but if the player just
        # switched vehicles the current one is the freshest, most relevant read.
        norms = list(self._vehicle_candidates.keys())
        if self._current_own_norm in self._vehicle_candidates:
            norms.remove(self._current_own_norm)
            norms.insert(0, self._current_own_norm)
        for norm in norms:
            candidates = self._vehicle_candidates.get(norm, set())
            if norm in self._ambiguous_vehicles or len(candidates) != 1:
                continue
            name = next(iter(candidates))
            if name != self._provisional_my_name:
                self._provisional_my_name = name
                if self._session_id is not None:
                    db.set_session_provisional_my_name(self._session_id, name)
            return
        self._provisional_my_name = None

    def _in_own_window(self, norm: str, match_time: float | None) -> bool:
        # Was the player actually driving `norm` at kill-feed `match_time`?
        # The current vehicle's interval is left open (end None) so anything
        # from when we entered it up to now counts. match_time None can't be
        # placed on the timeline, so it never counts (safer to skip than to
        # guess).
        if match_time is None:
            return False
        for start, end in self._own_vehicle_intervals.get(norm, ()):
            lo = start - OWN_WINDOW_SLACK
            hi = (end if end is not None else float("inf")) + OWN_WINDOW_SLACK
            if lo <= match_time <= hi:
                return True
        return False

    def _track_own_vehicle(self):
        current_type = self.latest.get("indicators", {}).get("type")
        if not current_type or current_type == self._last_own_vehicle or self._session_start_wall is None:
            return
        now_rel = time.time() - self._session_start_wall
        # Close out the interval for the vehicle we're leaving, and remember it
        # as the previous vehicle (with the switch time) for air-warning
        # respawn matching.
        if self._current_own_norm:
            intervals = self._own_vehicle_intervals.get(self._current_own_norm)
            if intervals and intervals[-1][1] is None:
                intervals[-1][1] = now_rel
            # Leaving a real vehicle mid-match is a respawn (we died in it).
            # Death-correlation gets first crack at confirming our name (it's
            # higher confidence); the candidate-pool fallback only fires if
            # that couldn't resolve.
            self._note_own_death(self._current_own_norm)
            self._finalize_vehicle_candidates(self._current_own_norm)
            self._prev_own_norm = self._current_own_norm
            self._prev_own_switch_wall = time.time()
        self._last_own_vehicle = current_type
        norm = analysis.normalize_vehicle_id(current_type)
        self._current_own_norm = norm
        if norm:
            # Open a fresh interval for the vehicle we're entering (a re-entry
            # gets its own interval rather than reopening the old one).
            self._own_vehicle_intervals.setdefault(norm, []).append([now_rel, None])

    def _maybe_resolve_my_name(self, parsed: dict, match_time: float | None):
        # We have no reliable "who am I" signal from the API (sender is always
        # blank, and the "enemy" flag turns out not to indicate team at all -
        # see _poll_hudmsg). Bootstrap by watching kill-feed mentions of
        # vehicles we've personally driven this session.
        #
        # A first cut just locked onto the FIRST name seen driving our current
        # vehicle, gated by "did this happen after we started driving it" -
        # that failed live: the player's very first vehicle of the match
        # starts at match_time~0, so that gate rejects nothing, and another
        # player who also happened to drive the same popular vehicle (T-80UD)
        # got locked in as "us" instead, while the real name sat unresolved in
        # the same dataset.
        #
        # Instead, every name seen driving a vehicle we ourselves have driven
        # goes into a per-vehicle candidate pool. If a SECOND distinct name
        # ever shows up for the same vehicle, that vehicle is flagged
        # ambiguous and permanently unusable for resolution - a genuine
        # collision is common enough (popular vehicles) that "first match
        # wins" isn't safe, but "was the only name ever seen driving it" is a
        # much stronger bar. Resolution is finalized once we've moved away
        # from that vehicle (see _finalize_vehicle_candidates), so a
        # late-arriving second candidate for our CURRENT vehicle still has a
        # chance to invalidate a would-be false lock before it's used.
        if self._my_name is not None:
            return
        for name, vehicle in (
            (parsed.get("actor_name"), parsed.get("actor_vehicle")),
            (parsed.get("target_name"), parsed.get("target_vehicle")),
        ):
            if not name or not vehicle:
                continue
            norm = analysis.normalize_vehicle_name(vehicle)
            # Time-gate: only trust this as possibly-us if the mention happened
            # while we were actually in that vehicle. Without this, a teammate
            # driving a vehicle type we used EARLIER (but had already switched
            # away from) polluted the candidate pool - confirmed live, that
            # picked a teammate's name as the roster identity while our own
            # (correct, in-window) mention sat unused.
            if norm in self._ambiguous_vehicles or not self._in_own_window(norm, match_time):
                continue
            bucket = self._vehicle_candidates.setdefault(norm, set())
            bucket.add(name)
            if len(bucket) > 1:
                # A second distinct name driving this exact vehicle showed up -
                # this is precisely the collision case. Deliberately NOT locking
                # here even if we'd already have looked "unique" a moment ago;
                # resolution only finalizes once we've moved away from the
                # vehicle (or the match ends), specifically so a late-arriving
                # second candidate like this one still gets a chance to veto a
                # premature lock.
                self._ambiguous_vehicles.add(norm)
                self._vehicle_candidates.pop(norm, None)
        self._update_provisional_name()

    def _maybe_flag_air_warning(self, parsed: dict):
        # Fires when an aircraft/helicopter/drone attacks US. Deliberately does
        # NOT depend on resolving our player NAME: name resolution fails
        # exactly in the cases that matter here (a shared vehicle where we
        # never appear in the feed resolves to a teammate, or to nobody), which
        # left this silent through whole matches where planes were killing us.
        #
        # Instead we key off our VEHICLE, which telemetry always tells us for
        # certain. An air attack counts as "on us" if its target vehicle
        # matches the one we're in now, or the one we were in moments ago (a
        # fatal hit's message names the vehicle we died in but lands just after
        # we've respawned - see RESPAWN_MATCH_WINDOW). A confidently-resolved
        # name still counts too, as a belt-and-suspenders path.
        #
        # Tradeoff: if a same-vehicle teammate is air-attacked we may warn when
        # it wasn't strictly us. For a "check the sky" threat cue that's a fine
        # price - a spurious glance costs nothing, a missed warning can cost the
        # vehicle - and it's a far better failure mode than the total silence
        # the name-only version produced.
        actor_vehicle = parsed.get("actor_vehicle")
        if not analysis.is_air_vehicle(actor_vehicle, self._known_armies):
            return

        target_name = parsed.get("target_name")
        target_vehicle = parsed.get("target_vehicle")

        effective_name = self._my_name or self._provisional_my_name
        name_match = bool(effective_name and target_name == effective_name)

        veh_match = False
        if target_vehicle:
            tnorm = analysis.normalize_vehicle_name(target_vehicle)
            if tnorm and tnorm == self._current_own_norm:
                veh_match = True
            elif (
                tnorm
                and tnorm == self._prev_own_norm
                and self._prev_own_switch_wall is not None
                and time.time() - self._prev_own_switch_wall < RESPAWN_MATCH_WINDOW
            ):
                veh_match = True

        if not (name_match or veh_match):
            return
        self._air_warning = {
            "attacker_name": parsed.get("actor_name"),
            "vehicle": actor_vehicle,
            "verb": parsed.get("verb"),
            "at": time.time(),
        }

    async def _poll_hudmsg(self):
        # NOTE: hudmsg is a match-wide kill-feed ticker (every player's kills,
        # not just yours), and its "enemy" boolean is NOT a reliable team flag -
        # confirmed live, it read false for every message in a real match
        # including ones where an enemy killed an ally. Team/self attribution
        # is done via name-matching (see _maybe_resolve_my_name) instead.
        data = await self._get(
            "/hudmsg", params={"lastEvt": self._last_evt, "lastDmg": self._last_dmg}
        )
        if not data:
            return
        for evt in data.get("events", []):
            self._last_evt = max(self._last_evt, evt.get("id", self._last_evt + 1))
            if self._session_id is not None:
                parsed = analysis.parse_combat_message(evt.get("msg")) or {}
                self._maybe_resolve_my_name(parsed, evt.get("time"))
                db.log_event(
                    self._session_id, "event", evt,
                    msg=evt.get("msg"), match_time=evt.get("time"), is_enemy=evt.get("enemy"),
                    **parsed,
                )
        for dmg in data.get("damage", []):
            self._last_dmg = max(self._last_dmg, dmg.get("id", self._last_dmg + 1))
            if self._session_id is not None:
                parsed = analysis.parse_combat_message(dmg.get("msg")) or {}
                self._maybe_resolve_my_name(parsed, dmg.get("time"))
                if analysis.is_lethal(parsed.get("verb")):
                    self._record_lethal(parsed)
                self._maybe_flag_air_warning(parsed)
                db.log_event(
                    self._session_id, "damage", dmg,
                    msg=dmg.get("msg"), match_time=dmg.get("time"), is_enemy=dmg.get("enemy"),
                    **parsed,
                )

    async def run(self):
        self._client = httpx.AsyncClient()
        last_map_poll = 0.0
        last_hudmsg_poll = 0.0
        last_map_image_poll = 0.0
        was_valid = False
        try:
            while True:
                state = await self._get("/state")
                indicators = await self._get("/indicators")

                connected = state is not None and indicators is not None
                # /state is flight telemetry only and reports valid=false for ground/naval
                # vehicles, so match/session detection must key off /indicators alone - but
                # indicators.valid on its own turned out to ALSO read true while just sitting
                # in the hangar with a vehicle loaded in the preview (confirmed live: a
                # hangar-only stretch produced indicators.valid=true, army/type populated,
                # for its entire duration while map_info.valid stayed false throughout - a
                # real match never showed that combination). map_obj is NOT a usable
                # secondary signal here despite looking like one: WT's local API doesn't
                # clear it on returning to hangar, it just freezes the last match's object
                # list forever (confirmed live: 150 stale entries kept reappearing 20+
                # seconds into sitting in the hangar). map_info only refreshes once a
                # second (see MAP_INTERVAL below), so this reads the last-polled value off
                # self.latest rather than fetching fresh every 0.1s loop tick - up to ~1s of
                # lag at a real match's start, which is a fine trade for not misreporting
                # "in match" for an entire hangar session.
                map_info = self.latest.get("map_info") or {}
                raw_valid = bool(connected and indicators.get("valid") and map_info.get("valid"))

                now = time.time()
                if raw_valid or not was_valid:
                    self._match_invalid_since = None
                    is_valid = raw_valid
                else:
                    # Debounce match-end: don't drop out of "in match" until
                    # map_info.valid has read false for MATCH_END_DEBOUNCE
                    # seconds straight, in case of a single transient blip.
                    if self._match_invalid_since is None:
                        self._match_invalid_since = now
                    is_valid = (now - self._match_invalid_since) < MATCH_END_DEBOUNCE

                if connected:
                    self._handle_match_transition(was_valid, is_valid, indicators or {})
                    was_valid = is_valid
                    self.latest["state"] = state or {}
                    self.latest["indicators"] = indicators or {}
                    if is_valid:
                        self._track_own_vehicle()
                        # Settle any pending death-correlation even when no new
                        # kill-feed messages are arriving to trigger it.
                        if self._my_name is None and self._pending_deaths:
                            self._correlate_deaths()
                else:
                    was_valid = False
                    self._match_invalid_since = None
                    self.latest["state"] = {}
                    self.latest["indicators"] = {}

                self.latest["connected"] = connected
                self.latest["in_match"] = is_valid
                self.latest["session_id"] = self._session_id

                now = time.time()
                if self._air_warning and now - self._air_warning["at"] < AIR_WARNING_TTL:
                    self.latest["air_warning"] = self._air_warning
                else:
                    self.latest["air_warning"] = None

                if connected and now - last_map_poll >= MAP_INTERVAL:
                    last_map_poll = now
                    map_obj = await self._get("/map_obj.json")
                    mission = await self._get("/mission.json")
                    map_info = await self._get("/map_info.json")
                    self.latest["map_obj"] = map_obj or []
                    self.latest["mission"] = mission or {}
                    if map_info:
                        self.latest["map_info"] = map_info

                    if self._session_id is not None:
                        db.log_telemetry_sample(
                            self._session_id,
                            state=self.latest["state"],
                            indicators=self.latest["indicators"],
                            map_obj=self.latest["map_obj"],
                            mission=self.latest["mission"],
                            map_info=self.latest["map_info"],
                        )

                if connected and now - last_hudmsg_poll >= HUDMSG_INTERVAL:
                    last_hudmsg_poll = now
                    await self._poll_hudmsg()

                if connected and now - last_map_image_poll >= MAP_IMAGE_INTERVAL:
                    last_map_image_poll = now
                    await self._poll_map_image()

                await self._broadcast()
                await asyncio.sleep(STATE_INTERVAL)
        finally:
            await self._client.aclose()
