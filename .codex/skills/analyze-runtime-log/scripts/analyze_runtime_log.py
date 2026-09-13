#!/usr/bin/env python3
r"""Read-only summary for D:\PythonProject4 JSONL runtime traces."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_LOG_DIR = PROJECT_ROOT / "logs"
IMPORTANT_PREFIXES = (
    "attack_",
    "combat_",
    "smart_chase",
    "recorded_route_",
    "rope_",
    "rest_",
    "stuck_",
    "session_",
)
IMPORTANT_EXACT = {
    "detection_frame",
    "character_trajectory",
    "template_detection_performance",
}


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def load_events(path: Path) -> tuple[list[dict[str, Any]], int]:
    events: list[dict[str, Any]] = []
    invalid = 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                invalid += 1
                continue
            if isinstance(item, dict):
                events.append(item)
            else:
                invalid += 1
    return events, invalid


def latest_log(log_dir: Path) -> Path:
    candidates = list(log_dir.glob("运行轨迹_*.log"))
    if not candidates:
        candidates = list(log_dir.glob("*.log"))
    if not candidates:
        raise FileNotFoundError(f"No .log files found under {log_dir}")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def in_window(events: list[dict[str, Any]], minutes: float | None) -> list[dict[str, Any]]:
    if not events or minutes is None:
        return events
    end = next((stamp for stamp in reversed([parse_time(e.get("time")) for e in events]) if stamp), None)
    if end is None:
        return events
    start = end - timedelta(minutes=max(0.0, minutes))
    return [event for event in events if (parse_time(event.get("time")) or end) >= start]


def first(event: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in event and event[key] is not None:
            return event[key]
    return default


def pair(value: Any) -> list[float] | None:
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        try:
            return [round(float(value[0]), 1), round(float(value[1]), 1)]
        except (TypeError, ValueError):
            return None
    return None


def relative_monsters(event: dict[str, Any]) -> list[list[float]]:
    direct = event.get("mr")
    if isinstance(direct, list):
        result = [coords for value in direct if (coords := pair(value)) is not None]
        if result:
            return result
    person = pair(first(event, "p", "person_position", "self_position"))
    monsters = event.get("m")
    if person and isinstance(monsters, list):
        result = []
        for value in monsters:
            coords = pair(value)
            if coords:
                result.append([round(coords[0] - person[0], 1), round(coords[1] - person[1], 1)])
        return result
    return []


def compact_detection(event: dict[str, Any]) -> dict[str, Any]:
    left_right = event.get("lr")
    if not isinstance(left_right, list):
        left_right = [first(event, "left", default=0), first(event, "right", default=0)]
    return {
        "t": event.get("time"),
        "ms": event.get("elapsed_ms"),
        "p": first(event, "p", "person_position", "self_position"),
        "mr": relative_monsters(event),
        "n": first(event, "n", "targets", default=0),
        "a": first(event, "a", "nearby", default=0),
        "c": first(event, "c", "chase_targets", default=0),
        "lr": left_right,
        "cd": first(event, "cd", "chase_direction"),
        "dd": first(event, "dd", "decision_direction"),
        "cs": first(event, "cs", "combat_state"),
        "hb": first(event, "hb", "health_bar_targets", default=0),
    }


def important(event: dict[str, Any]) -> bool:
    name = str(event.get("event", ""))
    return name in IMPORTANT_EXACT or name.startswith(IMPORTANT_PREFIXES)


def summarize(events: list[dict[str, Any]], invalid: int, path: Path) -> dict[str, Any]:
    counts = Counter(str(event.get("event", "<missing>")) for event in events)
    detections = [compact_detection(event) for event in events if event.get("event") == "detection_frame"]
    with_monsters = [item for item in detections if (item.get("n") or 0) > 0]
    with_attackable = [item for item in detections if (item.get("a") or 0) > 0]
    with_chase = [item for item in detections if (item.get("c") or 0) > 0]
    relative_frames = [item for item in detections if item.get("mr")]
    closest_dx = None
    if relative_frames:
        distances = [abs(coords[0]) for item in relative_frames for coords in item["mr"]]
        closest_dx = min(distances) if distances else None

    timeline = []
    for event in events:
        if not important(event) or event.get("event") in {
            "detection_frame",
            "template_detection_performance",
            "character_trajectory",
        }:
            continue
        timeline.append({
            "t": event.get("time"),
            "ms": event.get("elapsed_ms"),
            "e": event.get("event"),
            "dir": first(event, "direction", "target_direction", "desired_direction"),
            "act": event.get("action"),
            "reason": first(event, "reason", "block_reason"),
        })

    return {
        "log": str(path),
        "size_bytes": path.stat().st_size,
        "events": len(events),
        "invalid_lines": invalid,
        "range": {
            "start": events[0].get("time") if events else None,
            "end": events[-1].get("time") if events else None,
        },
        "top_events": counts.most_common(20),
        "detection": {
            "frames": len(detections),
            "frames_with_monsters": len(with_monsters),
            "frames_attackable": len(with_attackable),
            "frames_chaseable": len(with_chase),
            "frames_with_coordinates": len(relative_frames),
            "closest_abs_dx": closest_dx,
            "samples": detections[-20:],
        },
        "timeline_tail": timeline[-80:],
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarize a runtime JSONL log without modifying it.")
    parser.add_argument("--log", type=Path, help="Specific runtime log; defaults to the latest log.")
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--minutes", type=float, help="Keep the last N minutes relative to the log end.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")
    args = parser.parse_args(list(argv) if argv is not None else None)

    path = args.log.resolve() if args.log else latest_log(args.log_dir.resolve())
    events, invalid = load_events(path)
    selected = in_window(events, args.minutes)
    report = summarize(selected, invalid, path)
    print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None, separators=None if args.pretty else (",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
