# WT Dashboard

A local telemetry dashboard for War Thunder. Polls the game's built-in local
API (`localhost:8111`, enabled automatically whenever a match is running) and
serves a live Friendly/Enemy Roster, vehicle equipment/damage state, and
per-session stats in your browser.

This only works while War Thunder itself is running on the **same machine** -
it has no way to see a match remotely.

## Running it

### Easiest: download the exe

Grab `wt-dashboard.exe` from the [Releases](../../releases) page, put it
anywhere, and double-click it. A console window opens (that's the server;
closing it stops the dashboard) and your browser opens to the dashboard
automatically. No install, no Python required.

Match history is saved to `%LOCALAPPDATA%\WTDashboard\stats.db`.

### From source

Requires Python 3.11+.

```
pip install -r backend/requirements.txt
python -m uvicorn main:app --app-dir backend --port 8765
```

Then open `http://localhost:8765`.

## Building the exe yourself

```
./build.ps1
```

Produces `dist/wt-dashboard.exe` (PyInstaller, one-file bundle including the
frontend). See `build.ps1` for the exact flags if you need to reproduce it
without PowerShell.

## Notes

- `frontend/vehicles.json` is a snapshot of vehicle ids/premium status pulled
  from a third-party War Thunder vehicles API
  ([wtvehiclesapi.duckdns.org](https://wtvehiclesapi.duckdns.org)) for the
  premium-vehicle gold highlight and wiki links in the roster. It'll drift as
  new vehicles release - there's no automated refresh, regenerate it by
  re-pulling `GET /api/vehicles` from that API.
- The Friendly/Enemy Roster is built entirely from the in-game kill feed
  (there's no API for a real player list or map positions in multiplayer) -
  see the comments on `build_player_roster` in `backend/analysis.py` for how
  team/identity is inferred and what its coverage limits are.
