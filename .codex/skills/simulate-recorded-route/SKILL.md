---
name: simulate-recorded-route
description: Simulate custom recorded-route JSON files in D:\PythonProject4 against the real v3/public/recorded_route_player.py state machine without starting the game. Use when the user asks to simulate a 刷图 route, check a recorded map JSON for stuck points, validate platforms, ropes, minimap jitter, ignored-platform exits, combat-lock recovery, or verify route code changes against a named map file.
---

# Simulate Recorded Route

Use the bundled simulator to validate the same rebuilt route variants and helper functions used by the application. Do not start the game or send real input.

## Run

1. Resolve the requested JSON under `v3/map/recordings`, unless the user supplied an absolute path.
2. Run:

   ```powershell
   <python> .codex/skills/simulate-recorded-route/scripts/simulate_route.py <route-json>
   ```

3. If `python` is unavailable, use the Python path returned by `codex_app__load_workspace_dependencies`.
4. Use `--laps 5 --jitter 2` for a stronger cursor stress run. Use `--json` for a machine-readable report.

The simulator checks runtime route rebuilding, adjacent coordinate gaps, multi-lap cursor progress, one-pixel minimap jitter, every rope entry and exit, ignored platforms 1/2/3, and new-combat watchdog initialization.

## Interpret

- Treat `PASS` with zero failures as no deterministic state-machine or map-topology stuck point found.
- Report warnings separately. Synthetic rope exits and walk-off landings can intentionally point opposite the next platform sweep; they fail only when the gap exceeds the relocalization limit or cursor progress breaks.
- Simulation cannot prove the live game will never stall because screenshots, focus, collision, latency, and key delivery are external. Compare live failures with `运行轨迹_*.log`.
- Identify whether a failure comes from JSON topology or playback code before editing. Do not rewrite the user's map file unless explicitly requested.

## After changes

Run the simulator again against the exact map named by the user. Report the variant name, point count, platform ranges, rope checks, ignored-platform exits, jitter laps, watchdog result, warnings, and final status.
