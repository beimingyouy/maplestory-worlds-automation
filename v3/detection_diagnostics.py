def log_template_detection_snapshot(
    frame_number,
    map_name,
    search_range,
    vertical_range,
    shared_position_before,
    shared_position_after,
    player_location_description,
    player_center,
    attack_reference,
    total_targets,
    target_details,
    total_nearby,
    direction_counts,
    potion_coordinates,
    wheel_confidence,
    wheel_center,
    state_before,
    state_after,
    performance,
):
    """输出一帧模板检测的完整攻击判定链路。"""
    if total_nearby == 0:
        conclusion = "不攻击：没有同时通过纵向和横向过滤的怪物"
    elif state_after[1] == 3:
        conclusion = "攻击：附近目标不少于 2 且已开启群攻（攻击=3）"
    elif direction_counts["左边"] > 0 and direction_counts["右边"] > 0:
        conclusion = "攻击：左右都有目标；现有逻辑最终选择右侧（攻击=2）"
    elif direction_counts["左边"] > 0:
        conclusion = "攻击：左侧存在目标（攻击=1）"
    else:
        conclusion = "攻击：右侧存在目标（攻击=2）"

    frame_ms = performance["frame_ms"]
    estimated_fps = 1000.0 / max(frame_ms, 50.0, 0.001)
    print(
        "[模板诊断] 帧{} {}｜目标{}/近怪{}（左{}右{}）｜人物={}｜状态{}→{}｜{}｜"
        "模板{}张 匹配{:.1f}ms 人物{:.1f}ms 整帧{:.1f}ms FPS{:.1f}".format(
            frame_number,
            map_name,
            total_targets,
            total_nearby,
            direction_counts["左边"],
            direction_counts["右边"],
            player_location_description,
            state_before[0],
            state_after[0],
            conclusion,
            performance["template_count"],
            performance["monster_ms"],
            performance["person_ms"],
            frame_ms,
            estimated_fps,
        )
    )
