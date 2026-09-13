from v3.public.route_recording import (
    SEGMENT_PLATFORM,
    SEGMENT_PLATFORM_TO_ROPE_RIGHT,
    SEGMENT_ROPE,
    SEGMENT_ROPE_TO_PLATFORM_RIGHT,
    SEGMENT_WALK_OFF_LEFT,
    build_v2_compatibility_points,
    build_v2_recording_graph,
)


def _segment(
    segment_id,
    segment_type,
    x_range,
    y_range,
    start,
    end,
    platform_id=None,
):
    result = {
        "id": segment_id,
        "order": segment_id,
        "type": segment_type,
        "point_count": 8,
        "x_range": list(x_range),
        "y_range": list(y_range),
        "representative_x": round(sum(x_range) / 2),
        "representative_y": round(sum(y_range) / 2),
        "start": list(start),
        "end": list(end),
    }
    if platform_id is not None:
        result["platform_id"] = platform_id
    if segment_type == SEGMENT_ROPE:
        result.update(
            {
                "rope_x": round(sum(x_range) / 2),
                "top_y": min(y_range),
                "bottom_y": max(y_range),
                "fuzzy_x_range": [min(x_range) - 1, max(x_range) + 1],
            }
        )
    return result


def _frame(index, x, y, **pressed):
    keys = {
        "left": False,
        "right": False,
        "up": False,
        "down": False,
        "jump": False,
    }
    keys.update(pressed)
    return {
        "frame_index": index,
        "x": x,
        "y": y,
        "elapsed_ms": index * 33,
        "keys": keys,
    }


def _segments():
    return [
        _segment(1, SEGMENT_PLATFORM, (0, 12), (100, 100), (0, 100), (12, 100), "平台1"),
        _segment(2, SEGMENT_ROPE, (20, 20), (50, 100), (20, 100), (20, 50)),
        _segment(3, SEGMENT_PLATFORM, (20, 42), (50, 50), (20, 50), (42, 50), "平台2"),
        {
            **_segment(
                4,
                SEGMENT_PLATFORM_TO_ROPE_RIGHT,
                (10, 20),
                (90, 100),
                (10, 100),
                (20, 90),
                "平台1",
            ),
            "recorded_from_platform_id": "平台1",
        },
        {
            **_segment(
                5,
                SEGMENT_ROPE_TO_PLATFORM_RIGHT,
                (20, 25),
                (50, 52),
                (20, 50),
                (25, 50),
                "平台2",
            ),
            "recorded_to_platform_id": "平台2",
        },
        {
            **_segment(
                6,
                SEGMENT_WALK_OFF_LEFT,
                (5, 30),
                (50, 100),
                (30, 50),
                (5, 100),
                "平台2",
            ),
            "recorded_from_platform_id": "平台2",
        },
    ]


def _closed_trial_frames():
    coordinates = []
    coordinates.extend((5, 100, {}) for _ in range(4))
    coordinates.extend(
        [
            (10, 100, {"right": True, "jump": True}),
            (15, 95, {"right": True, "up": True, "jump": True}),
        ]
    )
    coordinates.extend(
        (20, y, {"up": True}) for y in (90, 80, 70, 60, 55, 52)
    )
    coordinates.extend((x, 50, {"right": True}) for x in (20, 22, 25, 28))
    coordinates.extend((30, 50, {}) for _ in range(3))
    coordinates.extend(
        [
            (28, 55, {"left": True}),
            (20, 70, {"left": True}),
            (12, 90, {"left": True}),
        ]
    )
    coordinates.extend((5, 100, {}) for _ in range(4))
    return [
        _frame(index, x, y, **pressed)
        for index, (x, y, pressed) in enumerate(coordinates)
    ]


def test_v2_fusion_preserves_directed_order_and_closed_loop():
    result = build_v2_recording_graph(_closed_trial_frames(), _segments())

    assert [node["id"] for node in result["nodes"]] == ["P1", "P2", "R1"]
    assert [(edge["source"], edge["target"]) for edge in result["edges"]] == [
        ("P1", "R1"),
        ("R1", "P2"),
        ("P2", "P1"),
    ]
    plan = result["route_plans"][0]
    assert plan["closed_loop"] is True
    assert plan["start_node"] == "P1"
    assert plan["final_node"] == "P1"
    assert plan["steps"] == ["E1", "E2", "E3"]


def test_v2_failed_attempt_returning_to_source_is_calibration_not_route_step():
    frames = []
    frames.extend(_frame(len(frames), 5, 100) for _ in range(3))
    frames.extend(
        [
            _frame(len(frames), 14, 90, right=True, jump=True),
            _frame(len(frames) + 1, 12, 92, left=True),
        ]
    )
    frames.extend(_frame(len(frames), 5, 100) for _ in range(3))
    frames.extend(
        [
            _frame(len(frames), 12, 96, right=True, jump=True),
            _frame(len(frames) + 1, 17, 92, right=True, up=True),
        ]
    )
    frames.extend(_frame(len(frames), 20, y, up=True) for y in (90, 80, 70, 60))

    result = build_v2_recording_graph(frames, _segments())

    assert result["route_plans"][0]["steps"] == ["E1"]
    assert len(result["calibration"]["retry_samples"]) == 1
    assert result["calibration"]["retry_samples"][0]["node_id"] == "P1"


def test_v2_compatibility_points_keep_rope_geometry_for_old_player():
    from pathlib import Path

    from v3.public import recorded_route_player

    frames = _closed_trial_frames()
    result = build_v2_recording_graph(frames, _segments())

    points = build_v2_compatibility_points(
        frames,
        result["frame_labels"],
        result["nodes"],
    )

    assert len(points) >= 2
    rope_points = [point for point in points if point["segment_type"] == "rope"]
    assert rope_points
    assert all(point["rope_x"] == 20 for point in rope_points)
    assert all(point["rope_top_y"] == 50 for point in rope_points)
    assert all(point["rope_bottom_y"] == 100 for point in rope_points)
    rope_entries = [
        point for point in points if point["segment_type"] == "rope_entry"
    ]
    assert rope_entries
    assert rope_entries[0]["command"] == "right up jump"
    parsed = recorded_route_player._parse_points(
        points,
        Path("V2测试路线.json"),
        "V2兼容路线",
    )
    parsed_entry = next(point for point in parsed if point.segment_type == "rope_entry")
    assert parsed_entry.rope_x == 20
    assert parsed_entry.rope_top_y == 50
    assert parsed_entry.rope_bottom_y == 100


def test_v2_loader_does_not_rebuild_and_overwrite_trial_order():
    import json
    import tempfile
    from pathlib import Path
    from types import SimpleNamespace
    from unittest.mock import patch

    from v3.public import recorded_route_player

    frames = _closed_trial_frames()
    fusion = build_v2_recording_graph(frames, _segments())
    points = build_v2_compatibility_points(
        frames,
        fusion["frame_labels"],
        fusion["nodes"],
    )
    with tempfile.TemporaryDirectory() as directory:
        route_path = Path(directory) / "V2路线.json"
        route_path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "points": points,
                    "route_variants": [
                        {
                            "name": "v2_trial_walk_compatibility",
                            "probability": 100,
                            "points": points,
                        }
                    ],
                    "raw_points": [{"structural": True}],
                    "segments": _segments(),
                    "route_graph": {
                        "platforms": [
                            {"platform_id": "平台1"},
                            {"platform_id": "平台2"},
                        ]
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        runtime = SimpleNamespace(
            PROJECT_ROOT=directory,
            自定义录制路线文件=str(route_path),
            get_base_dir=lambda: directory,
        )
        with patch.object(
            recorded_route_player,
            "build_route_variants_for_playback",
            side_effect=AssertionError("V2不应进入V1重建"),
        ):
            plan = recorded_route_player._load_recorded_route(runtime)

    assert plan.variants[0].name == "v2_trial_walk_compatibility"
    assert len(plan.variants[0].points) == len(points)
