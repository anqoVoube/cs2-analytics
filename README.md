# CS2 Scout

Self-contained Counter-Strike 2 demo analytics. Parse FACEIT/HLTV demos locally and get a
full opponent scouting report — positions, T/CT tactics, kill timing & context, rotations,
heatmaps, and a rule-based battle plan vs any 5 players. **No AI at runtime** — everything is
computed from the demos with pandas.

## Features

- **Player report** — K/D, ADR, KAST, HS%, opening duels, clutches, first-bullet accuracy,
  kill timing & context (holding vs pushing, through-smoke, flash-assisted), weapons, utility.
- **Battle plan** — pick 5 enemies, get separate advice for your T side and CT side: who to
  flash, angles to pre-aim, utility timing to wait out, rotations, entries, lurks, economy —
  with their positions drawn on the map.
- **Auto-scout (FACEIT)** — paste a match link; it finds your team by nickname, scouts the
  other team on the room's map, downloads their recent demos, parses, and preps the plan.
- **Team tactics & heatmaps** — round-by-round buy/execute/plant patterns and team heatmaps.

## Run (Windows)

```
python -m venv .venv
.venv\Scripts\pip install -e .
.venv\Scripts\python -m streamlit run src\scout\ui\app.py
```

Or double-click `run_website.bat`. Everything runs locally: drop `.dem` files into
`data/demos/` (any subfolder) and **Parse**, or use **🔎 Auto-scout** — log into FACEIT once
in the browser, paste a match link, and it signs the demo links, downloads them here, parses
them (in parallel across cores), and opens the battle plan.

## Notes

- Fully local — no server, no login gate, no AI at runtime.
- Paths are relative to the project folder (override with the `SCOUT_HOME` env var).
- Your FACEIT API key and nickname are saved under `data/` and are **gitignored**.
- Demos load from the parsed cache, so you can delete the big `.dem` files after parsing to
  save disk space; the analysis still works.
