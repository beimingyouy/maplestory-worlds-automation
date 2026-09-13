"""Pure navigation decisions shared by the recorded-route player.

This module deliberately does not press keys or read the game.  It only turns
route geometry and the currently observed platform into small, testable
decisions.  The player remains responsible for combat, input and tracing.
"""

from dataclasses import dataclass
from typing import Optional, Sequence


WALK_OFF_SEGMENT_DIRECTIONS = {
    "walk_off_left": "left",
    "walk_off_right": "right",
}


@dataclass(frozen=True)
class WalkOffPlan:
    """Describe the platform expected after a directional walk-off."""

    route_index: int
    direction: str
    source_platform_index: int
    target_platform_index: Optional[int]
    source_platform_number: Optional[int]
    target_platform_number: Optional[int]
    landing_x: int
    landing_y: int


@dataclass(frozen=True)
class WalkOffDecision:
    """State of an already-started directional walk-off."""

    completed: bool
    keep_direction: bool
    current_platform_index: Optional[int]
    reason: str


@dataclass(frozen=True)
class PlatformRotationDecision:
    """Decision for the one-minute, multi-platform soft rotation policy."""

    start_rotation: bool = False
    complete_rotation: bool = False
    reset_dwell: bool = False
    current_platform_key: Optional[int] = None
    elapsed_seconds: float = 0.0


def _forward_distance(total: int, start: int, target: int, closed_loop: bool):
    if target >= start:
        return target - start
    if closed_loop and total > 0:
        return total - start + target
    return None


def infer_walk_off_plan(
    points: Sequence[object],
    platform_ranges: Sequence[object],
    route_index: int,
    source_platform_index: int,
    closed_loop: bool,
) -> Optional[WalkOffPlan]:
    """Find the first different platform recorded after a walk-off segment."""

    if not (0 <= int(route_index) < len(points)):
        return None
    point = points[int(route_index)]
    direction = WALK_OFF_SEGMENT_DIRECTIONS.get(
        str(getattr(point, "segment_type", ""))
    )
    if direction is None:
        return None
    if not (0 <= int(source_platform_index) < len(platform_ranges)):
        return None

    source = platform_ranges[int(source_platform_index)]
    source_number = getattr(source, "platform_number", None)
    candidates = []
    for platform_index, platform in enumerate(platform_ranges):
        if int(platform_index) == int(source_platform_index):
            continue
        distance = _forward_distance(
            len(points),
            int(route_index) + 1,
            int(getattr(platform, "start_index")),
            bool(closed_loop),
        )
        if distance is None:
            continue
        target_number = getattr(platform, "platform_number", None)
        if (
            source_number is not None
            and target_number is not None
            and int(target_number) == int(source_number)
        ):
            continue
        candidates.append((int(distance), int(platform_index)))

    target_index = min(candidates)[1] if candidates else None
    target_number = (
        getattr(platform_ranges[target_index], "platform_number", None)
        if target_index is not None
        else None
    )
    # The last point of the contiguous walk-off segment is the coordinate that
    # was actually observed when recording the landing.  It is more precise
    # than merely being within the platform match margin.
    landing_point = point
    for offset in range(len(points)):
        raw_index = int(route_index) + offset
        if raw_index >= len(points):
            if not closed_loop:
                break
            raw_index %= len(points)
        candidate = points[raw_index]
        if str(getattr(candidate, "segment_type", "")) not in (
            "walk_off_left",
            "walk_off_right",
        ):
            break
        if WALK_OFF_SEGMENT_DIRECTIONS.get(
            str(getattr(candidate, "segment_type", ""))
        ) != direction:
            break
        landing_point = candidate
    return WalkOffPlan(
        route_index=int(route_index),
        direction=direction,
        source_platform_index=int(source_platform_index),
        target_platform_index=target_index,
        source_platform_number=(
            int(source_number) if source_number is not None else None
        ),
        target_platform_number=(
            int(target_number) if target_number is not None else None
        ),
        landing_x=int(getattr(landing_point, "x")),
        landing_y=int(getattr(landing_point, "y")),
    )


def evaluate_walk_off(
    plan: WalkOffPlan,
    current_platform_index: Optional[int],
    platform_ranges: Sequence[object],
    position=None,
    landing_tolerance_x: int = 2,
    landing_tolerance_y: int = 6,
) -> WalkOffDecision:
    """Keep walking in one direction until the recorded target platform lands."""

    if current_platform_index is None:
        return WalkOffDecision(False, True, None, "airborne_or_unmatched")

    current_index = int(current_platform_index)
    current_number = None
    if 0 <= current_index < len(platform_ranges):
        current_number = getattr(
            platform_ranges[current_index], "platform_number", None
        )
    target_matches = (
        plan.target_platform_number is not None
        and current_number is not None
        and int(current_number) == int(plan.target_platform_number)
    ) or (
        plan.target_platform_number is None
        and plan.target_platform_index is not None
        and current_index == int(plan.target_platform_index)
    )
    coordinate_matches = True
    if position is not None:
        position_x = int(position[0])
        position_y = int(position[1])
        if plan.direction == "right":
            horizontal_matches = position_x >= int(plan.landing_x) - int(
                landing_tolerance_x
            )
        else:
            horizontal_matches = position_x <= int(plan.landing_x) + int(
                landing_tolerance_x
            )
        vertical_matches = abs(position_y - int(plan.landing_y)) <= int(
            landing_tolerance_y
        )
        coordinate_matches = horizontal_matches and vertical_matches
    if target_matches and coordinate_matches:
        return WalkOffDecision(True, False, current_index, "target_platform")
    return WalkOffDecision(
        False,
        True,
        current_index,
        "target_coordinate_not_reached" if target_matches else "target_not_reached",
    )


def platform_identity(platform_index: int, platform_ranges: Sequence[object]) -> int:
    """Use the recorded platform number when available, otherwise range index."""

    platform = platform_ranges[int(platform_index)]
    number = getattr(platform, "platform_number", None)
    return int(number) if number is not None else -(int(platform_index) + 1)


def evaluate_platform_rotation(
    *,
    current_platform_index: Optional[int],
    platform_ranges: Sequence[object],
    active_platform_count: int,
    tracked_platform_key: Optional[int],
    dwell_started_at: float,
    rotation_active: bool,
    rotation_source_key: Optional[int],
    now: float,
    max_dwell_seconds: float,
) -> PlatformRotationDecision:
    """Start soft rotation after the same brush platform has lasted one minute."""

    if active_platform_count <= 1 or current_platform_index is None:
        return PlatformRotationDecision()
    current_key = platform_identity(current_platform_index, platform_ranges)
    if rotation_active:
        if rotation_source_key is not None and current_key != rotation_source_key:
            return PlatformRotationDecision(
                complete_rotation=True,
                reset_dwell=True,
                current_platform_key=current_key,
            )
        return PlatformRotationDecision(current_platform_key=current_key)
    if tracked_platform_key != current_key or dwell_started_at <= 0:
        return PlatformRotationDecision(
            reset_dwell=True,
            current_platform_key=current_key,
        )
    elapsed = max(0.0, float(now) - float(dwell_started_at))
    return PlatformRotationDecision(
        start_rotation=elapsed >= float(max_dwell_seconds),
        current_platform_key=current_key,
        elapsed_seconds=elapsed,
    )
