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

## Run locally (Windows)

```
python -m venv .venv
.venv\Scripts\pip install -e .
.venv\Scripts\python -m streamlit run src\scout\ui\app.py
```

Or double-click `run_website.bat`. Drop `.dem` files into `data/demos/` (any subfolder) or
upload them in the **Demos** tab, then **Parse**.

## Run on a server (Amsterdam VPS)

If your network can't reach FACEIT's EU demo CDN, run the whole tool on a European VPS.
See **[DEPLOY.md](DEPLOY.md)** for step-by-step instructions.

## Notes

- Paths are relative to the project folder (override with the `SCOUT_HOME` env var), so the
  same code runs on Windows and Linux.
- Your FACEIT API key and nickname are saved under `data/` and are **gitignored** — they never
  leave your machine.
