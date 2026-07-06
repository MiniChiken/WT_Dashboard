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
# "who am I" resolution normally waits for a vehicle switch (or match end) to
# close a vehicle's candidate window before trusting it - see
# _maybe_resolve_my_name. This is a timeliness fallback for matches where the
# player never switches: if enough time has passed in the same vehicle with
# no ambiguity ever surfacing, go ahead and lock rather than staying
# unresolved for the whole match.
TENTATIVE_LOCK_DELAY = 90.0
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
        self._own_vehicle_first_seen: dict[str, float] = {}
        self._last_own_vehicle: str | None = None
        # Per-vehicle candidate pool for "who am I" resolution - see
        # _maybe_resolve_my_name for why this replaced a simpler first-match approach.
        self._vehicle_candidates: dict[str, set[str]] = {}
        self._ambiguous_vehicles: set[str] = set()

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
            self._own_vehicle_first_seen = {}
            self._last_own_vehicle = None
            self._vehicle_candidates = {}
            self._ambiguous_vehicles = set()
        elif was_valid and not is_valid and self._session_id is not None:
            # Fallback for a match that ends before we ever switched vehicles
            # (so _track_own_vehicle's move-away trigger never fired) - close
            # out whatever vehicle we were last in using whatever candidates
            # were collected for it.
            if self._last_own_vehicle:
                norm = analysis.normalize_vehicle_id(self._last_own_vehicle)
                if norm:
                    self._finalize_vehicle_candidates(norm)
            db.end_session(self._session_id)
            self._session_id = None

    def _finalize_vehicle_candidates(self, norm: str):
        # Called once we're confident we've seen every kill-feed mention of a
        # vehicle we personally drove (i.e. we've since moved on to a
        # different one, closing that vehicle's observation window). Only
        # resolves if EXACTLY one distinct name ever showed up driving it -
        # if a second name appeared before this point, _maybe_resolve_my_name
        # already flagged it ambiguous and it's permanently unusable.
        if self._my_name is not None or norm in self._ambiguous_vehicles:
            return
        candidates = self._vehicle_candidates.get(norm)
        if candidates and len(candidates) == 1:
            self._my_name = next(iter(candidates))
            if self._session_id is not None:
                db.set_session_my_name(self._session_id, self._my_name)

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
        for norm, candidates in self._vehicle_candidates.items():
            if norm in self._ambiguous_vehicles or len(candidates) != 1:
                continue
            name = next(iter(candidates))
            if name != self._provisional_my_name:
                self._provisional_my_name = name
                if self._session_id is not None:
                    db.set_session_provisional_my_name(self._session_id, name)
            return
        self._provisional_my_name = None

    def _maybe_tentative_lock(self):
        if self._my_name is not None or not self._last_own_vehicle or self._session_start_wall is None:
            return
        norm = analysis.normalize_vehicle_id(self._last_own_vehicle)
        started = self._own_vehicle_first_seen.get(norm) if norm else None
        if norm and started is not None:
            elapsed_in_vehicle = (time.time() - self._session_start_wall) - started
            if elapsed_in_vehicle > TENTATIVE_LOCK_DELAY:
                self._finalize_vehicle_candidates(norm)

    def _track_own_vehicle(self):
        current_type = self.latest.get("indicators", {}).get("type")
        if not current_type or current_type == self._last_own_vehicle or self._session_start_wall is None:
            return
        if self._last_own_vehicle:
            prev_norm = analysis.normalize_vehicle_id(self._last_own_vehicle)
            if prev_norm:
                self._finalize_vehicle_candidates(prev_norm)
        self._last_own_vehicle = current_type
        norm = analysis.normalize_vehicle_id(current_type)
        if norm and norm not in self._own_vehicle_first_seen:
            self._own_vehicle_first_seen[norm] = time.time() - self._session_start_wall

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
            if norm not in self._own_vehicle_first_seen or norm in self._ambiguous_vehicles:
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
        # Only fires once "my name" is resolved, since it's the one thing we
        # CAN say for certain: whoever damages you is hostile, regardless of
        # the broader (unreliable) team attribution problem elsewhere here.
        if not self._my_name or parsed.get("target_name") != self._my_name:
            return
        actor_vehicle = parsed.get("actor_vehicle")
        if not analysis.is_air_vehicle(actor_vehicle, self._known_armies):
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
                        self._maybe_tentative_lock()
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
