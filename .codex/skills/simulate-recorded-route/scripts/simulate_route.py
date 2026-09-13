#!/usr/bin/env python3
"""Simulate a recorded-route JSON with the production playback state machine."""

from __future__ import annotations

import argparse
import json
import sys
import time
import types
from pathlib import Path


def project_root() -> Path:
    """Return the project root for either installed or staged skill layouts."""
    path = Path(__file__).resolve()
    for parent in path.parents:
        if (parent / "v3" / "public" / "recorded_route_player.py").is_file():
            return parent
    raise FileNotFoundError("无法定位包含 v3/public/recorded_route_player.py 的项目目录")


def resolve_route(root: Path, value: str) -> Path:
    """Resolve an absolute path, project path, or recordings filename."""
    raw = Path(value)
    for candidate in (
        raw,
        Path.cwd() / raw,
        root / raw,
        root / "v3" / "map" / "recordings" / raw.name,
    ):
        candidate = candidate.resolve()
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("找不到录制路线：{}".format(value))


def install_import_stubs() -> None:
    """Stub GUI and live minimap imports so no game or window is touched."""
    qt_core = types.ModuleType("PyQt5.QtCore")
    qt_core.QObject = type("QObject", (), {})
    qt_core.pyqtSignal = lambda *args, **kwargs: None
    pyqt5 = types.ModuleType("PyQt5")
    pyqt5.QtCore = qt_core
    sys.modules["PyQt5"] = pyqt5
    sys.modules["PyQt5.QtCore"] = qt_core

    tracking = types.ModuleType("v3.public.minimap_tracking")
    tracking.MINIMAP_MONITOR = {"top": 110, "left": 20, "width": 270, "height": 190}
    tracking.find_yellow_center = lambda *args, **kwargs: None
    sys.modules["v3.public.minimap_tracking"] = tracking


class DirectInput:
    """No-op keyboard interface used by playback helpers."""

    def keyDown(self, _key: str) -> None:
        """Accept a simulated key-down."""

    def keyUp(self, _key: str) -> None:
        """Accept a simulated key-up."""


class Runtime:
    """Minimal legacy runtime API with no external side effects."""

    zant = 0
    pydirectinput = DirectInput()

    def __init__(self, root: Path, route: Path):
        """Bind the selected route and initialize the event collector."""
        self.PROJECT_ROOT = str(root)
        self.自定义录制路线文件 = str(route)
        self.events = []

    def get_base_dir(self) -> str:
        """Return the simulated application root."""
        return self.PROJECT_ROOT

    def trace_event(self, event: str, **fields) -> None:
        """Collect state-machine events for assertions."""
        self.events.append((event, fields))

    def 可中断等待(self, *_args, **_kwargs) -> bool:
        """Complete waits immediately during simulation."""
        return True

    def 读取人物位置(self):
        """Keep the caller-provided simulated position."""
        return None

    def __getattr__(self, _name):
        """Return no-ops for movement, attack, and relocation hooks."""
        return lambda *args, **kwargs: None


def load_player(root: Path):
    """Import production route code with side-effect-free dependencies."""
    sys.path.insert(0, str(root))
    install_import_stubs()
    from v3.public import recorded_route_player

    return recorded_route_player


def simulate_ropes(rr, runtime, variant):
    """Exercise every rope from one-pixel top stabilization through exit."""
    results = []
    failures = []
    for entry_index, point in enumerate(variant.points):
        if point.segment_type != "rope_entry":
            continue
        geometry = rr._normalized_rope_geometry(point)
        exit_index, exit_direction = rr._next_rope_exit(variant, entry_index)
        if geometry is None or exit_index is None or exit_direction not in ("left", "right"):
            failures.append({
                "type": "invalid_rope",
                "entry_index": entry_index,
                "geometry": geometry,
                "exit_index": exit_index,
                "exit_direction": exit_direction,
            })
            continue
        rope_x, top_y, bottom_y = geometry
        toward_rope = (
            (point.x < rope_x and point.horizontal == "right")
            or (point.x > rope_x and point.horizontal == "left")
            or point.x == rope_x
        )
        state = rr.RecordedRouteState(
            route_index=entry_index,
            active_rope_direction=point.horizontal,
            active_rope_x=rope_x,
            active_rope_top_y=top_y,
            active_rope_bottom_y=bottom_y,
            active_rope_started_at=time.monotonic() - 2.0,
            active_rope_best_y=top_y + 1,
            active_rope_progress_at=time.monotonic() - 1.0,
            active_rope_contacted=True,
            active_rope_exit_direction=exit_direction,
            active_rope_exit_index=exit_index,
            next_rope_rest_at=time.monotonic() + 9999.0,
        )
        runtime.events.clear()
        edge = rr._apply_active_rope(runtime, state, (rope_x, top_y + 1))
        below = rr._apply_rope_top_exit(
            runtime, state, variant, (rope_x, top_y + 1)
        )
        exit_delta_x = 2 if exit_direction == "right" else -2
        exited = rr._apply_rope_top_exit(
            runtime,
            state,
            variant,
            (rope_x + exit_delta_x, top_y + 1),
        )
        relatch_state = rr.RecordedRouteState(route_index=exit_index)
        top_platform_relatched = rr._latch_rope_from_current_position(
            runtime,
            relatch_state,
            variant,
            (rope_x, top_y + 1),
        )
        midbody_state = rr.RecordedRouteState()
        midbody_latched = rr._latch_rope_from_current_position(
            runtime,
            midbody_state,
            variant,
            (rope_x, min(top_y + 2, bottom_y - 1)),
        )
        top_event = next((fields for event, fields in runtime.events if event == "recorded_route_rope_top_reached"), None)
        exit_event = next((fields for event, fields in runtime.events if event == "recorded_route_rope_top_exit_completed"), None)
        passed = (
            toward_rope
            and edge
            and below
            and exited is False
            and not top_platform_relatched
            and midbody_latched
            and not state.rope_top_exit_pending
            and state.route_index == exit_index
            and top_event is not None
            and top_event.get("confirmation_reason") == "stable_one_pixel_top_edge"
            and exit_event is not None
            and exit_event.get("confirmation_reason") == "top_platform_x_changed"
        )
        result = {
            "entry_index": entry_index,
            "geometry": geometry,
            "exit_index": exit_index,
            "exit_direction": exit_direction,
            "passed": passed,
        }
        results.append(result)
        if not passed:
            failures.append({"type": "rope_flow", **result})
    return results, failures


def simulate_cursor(rr, runtime, variant, laps: int, jitter: int):
    """Run multiple laps through the production route-index selector."""
    points = variant.points
    total = len(points)
    state = rr.RecordedRouteState(next_rope_rest_at=time.monotonic() + 9999.0)
    previous = None
    failures = []
    def platform_range_index(route_index):
        return next(
            (
                index
                for index, platform in enumerate(variant.platform_ranges)
                if platform.start_index <= int(route_index) <= platform.end_index
            ),
            None,
        )

    offsets = [(-jitter, 0), (0, jitter), (jitter, 0), (0, -jitter), (jitter, jitter), (-jitter, -jitter)] if jitter else [(0, 0)]
    for lap in range(laps):
        for expected, point in enumerate(points):
            if point.segment_type in rr.RECORDED_ROUTE_CONNECTION_SEGMENT_TYPES:
                if not state.connection_priority_active:
                    source_index = rr._preceding_platform_range_index(
                        variant,
                        expected,
                    )
                    state.connection_priority_active = source_index is not None
                    state.connection_priority_source_platform_index = source_index
                    state.connection_priority_route_index = expected
                    state.connection_priority_started_at = time.monotonic()
            if point.segment_type in ("walk_off_left", "walk_off_right"):
                # Production playback latches a directional walk-off at its first
                # point and keeps that direction until the recorded target platform
                # coordinate is reached. Nearest-point selection is intentionally
                # bypassed while airborne, just like active rope traversal below.
                state.route_index = expected
                previous = expected
                continue
            if point.segment_type == "rope_entry":
                geometry = rr._normalized_rope_geometry(point)
                if geometry:
                    state.active_rope_direction = point.horizontal
                    state.active_rope_x, state.active_rope_top_y, state.active_rope_bottom_y = geometry
                # 正式主循环在 active_rope 存在时优先执行爬绳，不调用路线游标
                # 选择器。绳身和绳顶由 simulate_ropes 单独覆盖，这里只同步游标。
                state.route_index = expected
                previous = expected
                continue
            if point.segment_type == "rope" and state.active_rope_x is not None:
                state.route_index = expected
                previous = expected
                continue
            if point.segment_type == "rope_exit":
                rr._clear_active_rope(state)
                state.route_index = expected
                previous = expected
                continue
            if (
                point.segment_type in rr.RECORDED_ROUTE_CONNECTION_SEGMENT_TYPES
                and point.segment_type not in ("rope_entry", "rope", "rope_exit")
            ):
                state.route_index = expected
                previous = expected
                continue
            delta_x, delta_y = offsets[(expected + lap) % len(offsets)]
            position = (point.x + delta_x, point.y + delta_y)
            selected = rr._select_route_index(runtime, state, variant, position)
            if state.connection_priority_active and point.segment_type == "platform":
                rr._update_connection_priority(
                    runtime,
                    state,
                    variant,
                    position,
                    selected,
                )
            if previous is not None:
                forward = (selected - previous) % total
                backward = (previous - selected) % total
                stayed_on_same_platform = (
                    platform_range_index(previous) is not None
                    and platform_range_index(previous) == platform_range_index(selected)
                )
                if (
                    not stayed_on_same_platform
                    and forward > rr.ROUTE_LOOKAHEAD_POINTS
                    and backward > rr.ROUTE_BACKTRACK_POINTS
                ):
                    failures.append({
                        "type": "cursor_jump",
                        "lap": lap,
                        "expected_index": expected,
                        "previous_index": previous,
                        "selected_index": selected,
                        "position": position,
                        "segment_type": point.segment_type,
                    })
            previous = selected
    return failures


def simulate_ignored_platforms(rr, runtime, variant):
    """Verify every occurrence of platform 1, 2, and 3 has a usable exit."""
    results = []
    failures = []
    total = len(variant.points)
    for number in (1, 2, 3):
        for range_index, platform in enumerate(variant.platform_ranges):
            if platform.platform_number != number:
                continue
            position = (
                (platform.minimum_x + platform.maximum_x) // 2,
                (platform.minimum_y + platform.maximum_y) // 2,
            )
            state = rr.RecordedRouteState(
                ignored_platform_numbers=(number,),
                route_index=platform.start_index,
                next_rope_rest_at=time.monotonic() + 9999.0,
            )
            expected = platform.end_index + 1
            if expected >= total:
                expected = 0 if variant.closed_loop else platform.end_index
            handled = rr._apply_ignored_platform_route(
                runtime,
                state,
                variant,
                position,
                preferred_route_index=platform.start_index,
            )
            result = {
                "platform_number": number,
                "range_index": range_index,
                "transition_index": state.route_index,
                "expected_transition": expected,
                "next_segment_type": variant.points[state.route_index].segment_type if state.route_index is not None else None,
                "passed": handled and state.route_index == expected,
            }
            results.append(result)
            if not result["passed"]:
                failures.append({"type": "ignored_platform", **result})
    return results, failures


def simulate_watchdog(rr, runtime):
    """Ensure an old session timer cannot immediately suppress a new battle."""
    snapshot = rr.combat_logic.RouteMonsterSnapshot(
        time.monotonic(), "left", 1, 1, 1, 0, 1, 0, 40.0, 10.0, True
    )
    intent = rr.combat_logic.RouteActionIntent(
        "stop", "stop", "attack", "combat", "left", 10.0,
        1, 1, 1, 0, 1, 0, 40.0, time.monotonic(),
    )
    state = rr.RecordedRouteState(
        combat_lock_started_at=time.monotonic() - 10.0,
        combat_lock_last_progress_at=time.monotonic() - 10.0,
        combat_lock_last_attack_completed_at=time.monotonic() - 10.0,
    )
    state.combat.combat_active = False
    timed_out = rr._combat_lock_timed_out(runtime, state, snapshot, intent, (50, 128))
    return {
        "passed": not timed_out,
        "premature_timeout": timed_out,
        "new_watch_age_ms": round((time.monotonic() - state.combat_lock_started_at) * 1000.0, 3),
    }


def simulate_variant(rr, runtime, variant, laps: int, jitter: int):
    """Aggregate topology and state-machine checks for one runtime variant."""
    points = variant.points
    gaps = []
    warnings = []
    for index, point in enumerate(points):
        next_point = points[(index + 1) % len(points)]
        gap = rr._distance(point.position, next_point.position)
        gaps.append({"distance": gap, "index": index, "from": point.position, "to": next_point.position})
        dx, dy = next_point.x - point.x, next_point.y - point.y
        if (
            (point.horizontal == "left" and dx > 1)
            or (point.horizontal == "right" and dx < -1)
            or (point.vertical == "up" and dy > 1)
            or (point.vertical == "down" and dy < -1)
        ):
            warnings.append({
                "type": "direction_transition",
                "index": index,
                "segments": [point.segment_type, next_point.segment_type],
                "from": point.position,
                "to": next_point.position,
            })
    hard_gaps = [{"type": "hard_gap", **gap} for gap in gaps if gap["distance"] > rr.ROUTE_RELOCALIZE_DISTANCE]
    ropes, rope_failures = simulate_ropes(rr, runtime, variant)
    cursor_failures = simulate_cursor(rr, runtime, variant, laps, jitter)
    ignored, ignored_failures = simulate_ignored_platforms(rr, runtime, variant)
    failures = hard_gaps + rope_failures + cursor_failures + ignored_failures
    return {
        "name": variant.name,
        "probability": variant.probability,
        "point_count": len(points),
        "closed_loop": variant.closed_loop,
        "platform_ranges": [
            {
                "platform_number": item.platform_number,
                "indices": [item.start_index, item.end_index],
                "x_range": [item.minimum_x, item.maximum_x],
                "y_range": [item.minimum_y, item.maximum_y],
            }
            for item in variant.platform_ranges
        ],
        "max_gap": max(gaps, key=lambda item: item["distance"]),
        "cursor_laps": laps,
        "cursor_jitter": jitter,
        "cursor_failure_count": len(cursor_failures),
        "ropes": ropes,
        "ignored_platforms": ignored,
        "warnings": warnings,
        "failures": failures,
    }


def print_report(report) -> None:
    """Print a compact human-readable result."""
    print("地图：{}".format(report["route_path"]))
    for variant in report["variants"]:
        print(
            "变体 {}：{} 点，闭环={}，平台段={}，最大间距={}，抖动游标失败={}".format(
                variant["name"], variant["point_count"], variant["closed_loop"],
                len(variant["platform_ranges"]), variant["max_gap"]["distance"],
                variant["cursor_failure_count"],
            )
        )
        print("  绳子：{}".format(variant["ropes"]))
        print("  忽略平台出口：{}".format(variant["ignored_platforms"]))
        print("  过渡警告：{}".format(len(variant["warnings"])))
    print("新战斗看门狗：{}".format(report["watchdog"]))
    print("RESULT {}".format("PASS" if report["passed"] else "FAIL"))
    if report["failures"]:
        print(json.dumps(report["failures"], ensure_ascii=False, indent=2))


def main() -> int:
    """Run the requested map simulation and return zero only on success."""
    parser = argparse.ArgumentParser(description="模拟录制地图是否存在确定性刷图卡点")
    parser.add_argument("route_json", help="JSON 路径或 v3/map/recordings 下的文件名")
    parser.add_argument("--laps", type=int, default=3, help="模拟圈数，默认 3")
    parser.add_argument("--jitter", type=int, default=1, help="坐标抖动像素，默认 1")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON")
    args = parser.parse_args()

    root = project_root()
    route = resolve_route(root, args.route_json)
    rr = load_player(root)
    runtime = Runtime(root, route)
    plan = rr._load_recorded_route(runtime)
    variants = [
        simulate_variant(rr, runtime, item, max(1, args.laps), max(0, args.jitter))
        for item in plan.variants
    ]
    watchdog = simulate_watchdog(rr, runtime)
    failures = [
        {"variant": variant["name"], **failure}
        for variant in variants
        for failure in variant["failures"]
    ]
    if not watchdog["passed"]:
        failures.append({"type": "watchdog_new_session", **watchdog})
    report = {
        "route_path": str(route),
        "fallback_rest_point": [plan.fallback_rest_point.x, plan.fallback_rest_point.y] if plan.fallback_rest_point else None,
        "fallback_rest_rope_geometry": plan.fallback_rest_rope_geometry,
        "variants": variants,
        "watchdog": watchdog,
        "failures": failures,
        "passed": not failures,
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=list))
    else:
        print_report(report)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
