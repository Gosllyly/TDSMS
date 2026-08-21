import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from ortools.sat.python import cp_model

import scheduling_core as core
from output_weekly_template import generate_template_schedule_board
from postprocess_packaging_clear import postprocess_pack_clear
from postprocess_sunday_calendar import postprocess_detail_for_sundays
from solve_progress_logger import CpSearchProgressCallback, ScheduleSolveProgressLogger


def _remaining_seconds(deadline_monotonic):
    if deadline_monotonic is None:
        return None
    return max(0.0, deadline_monotonic - time.monotonic())


def _stage_budget(planned, deadline_monotonic):
    left = _remaining_seconds(deadline_monotonic)
    if left is None:
        return max(0.0, float(planned))
    return max(0.0, min(float(planned), left))


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _build_initial_pool(
    I, J1, J2, J3, J4, B, p1, p2, p3, p4, d, T, w, scale,
    stage_staff_limits, clear_time_matrices, machine_available_time, release_time,
    max_continuous_run, initial_solution_mode, initial_solution_file,
    periodic_cleaning_time=1.0,
):
    mode = (initial_solution_mode or "auto").lower()
    pool = []

    if mode in ("auto", "best"):
        order_modes = ("priority", "due", "risk", "pack_heavy", "random")
        for idx, order_mode in enumerate(order_modes):
            rng = random.Random(100 + idx)
            snapshot = core._build_greedy_snapshot(
                I, J1, J2, J3, J4, B, p1, p2, p3, p4, d, T, w, scale,
                stage_staff_limits, clear_time_matrices, machine_available_time, release_time,
                max_continuous_run=max_continuous_run, order_mode=order_mode, rng=rng,
                periodic_cleaning_time=periodic_cleaning_time,
            )
            obj = core._snapshot_objective(snapshot, I, B, w, cycle_over_penalty_factor=1000)
            pool.append((obj, f"auto:{order_mode}", snapshot))
            print(f"   初解[auto:{order_mode}] objective={obj}")

    if mode in ("excel", "best"):
        path = Path(initial_solution_file)
        if not path.exists():
            msg = f"初始解文件不存在: {initial_solution_file}"
            if mode == "excel":
                raise FileNotFoundError(msg)
            print(f"   初解[excel] {msg}, 已忽略。")
        else:
            try:
                snapshot = core._load_snapshot_from_excel(
                    str(path), I, J1, J2, J3, J4, B, p1, p2, p3, p4, d, T, w, scale
                )
                violations, obj = core._validate_snapshot_constraints(
                    snapshot, I, J1, J2, J3, J4, B, p1, p2, p3, p4, d, T, w, scale,
                    stage_staff_limits, clear_time_matrices, machine_available_time,
                    release_time, max_continuous_run,
                    periodic_cleaning_time=periodic_cleaning_time,
                )
                if violations:
                    print(f"   初解[excel] 存在 {len(violations)} 条硬约束违反。")
                    for row in violations[:10]:
                        print(f"     - {row['类型']}: {row['说明']}")
                    if mode == "excel":
                        raise ValueError("Excel 初始解不满足硬约束，无法作为 ALNS 初始解。")
                else:
                    pool.append((obj, "excel", snapshot))
                    print(f"   初解[excel] objective={obj}")
            except Exception as exc:
                if mode == "excel":
                    raise
                print(f"   初解[excel] 读取失败，已忽略: {exc}")

    if not pool:
        raise ValueError("没有可用初始解。")

    return min(pool, key=lambda x: x[0])


def _solve_pharmaceutical_schedule_cp_hybrid_impl(
    demo_file,
    aps_file,
    speed_preset="balanced",
    scale=None,
    stage1_sec=60,
    stage2_sec=300,
    cp_sec=1800,
    num_workers=8,
    seeds=(11, 23, 37),
    output_file="排产结果明细.xlsx",
    initial_solution_mode="auto",
    initial_solution_file=None,
    progress_logger=None,
    progress_interval_seconds=120,
    progress_emit=None,
    stage_staff_limits_override=None,
    max_continuous_run_override=None,
    major_cleaning_time_override=None,
    minor_cleaning_time_override=None,
    shifts_per_day=2,
    periodic_cleaning_time=1.0,
    stop_checker=None,
    user_stop_checker=None,
    deadline_monotonic=None,
    department="210车间",
    result_exporter=None,
):
    if stop_checker is not None and stop_checker():
        is_user_stop = user_stop_checker is None or user_stop_checker()
        if is_user_stop:
            progress_logger.on_stop_requested()
        else:
            progress_logger.on_time_limit()
        return {
            "alns_objective": None,
            "final_objective": None,
            "final_seed": None,
            "cp_status": "UNKNOWN",
            "stopped": is_user_stop,
            "timed_out": not is_user_stop,
            "result_ready": False,
            "files_exported": False,
            "stop_stage": "initial",
        }

    print("1. 读取输入数据...")
    (
        I, J1, J2, J3, J4, B, p1, p2, p3, p4, p5, d, T, w,
        stage_staff_limits, clear_time_matrices, machine_available_time,
        release_time, max_continuous_run
    ) = core.build_schedule_inputs(
        demo_file,
        aps_file,
        stage_staff_limits_override=stage_staff_limits_override,
        max_continuous_run_override=max_continuous_run_override,
        major_cleaning_time_override=major_cleaning_time_override,
        minor_cleaning_time_override=minor_cleaning_time_override,
        shifts_per_day=shifts_per_day,
        department=department,
    )

    if not I:
        raise ValueError(
            f"部门[{department}]没有可排产数据：请确认该部门存在月份生产计划大于0的记录，"
            "并且计划中的存货名称和规格能在当前APS排产信息档案中匹配。"
        )

    if scale is None:
        scale = core._scale_from_preset(speed_preset)

    status_map = {
        cp_model.OPTIMAL: "OPTIMAL",
        cp_model.FEASIBLE: "FEASIBLE",
        cp_model.INFEASIBLE: "INFEASIBLE",
        cp_model.MODEL_INVALID: "MODEL_INVALID",
        cp_model.UNKNOWN: "UNKNOWN",
    }

    t0 = time.time()

    print(f"2. 构造ALNS初解池，preset={speed_preset}, scale={scale}, mode={initial_solution_mode}...")
    initial_obj, initial_mode, initial_snapshot = _build_initial_pool(
        I, J1, J2, J3, J4, B, p1, p2, p3, p4, d, T, w, scale,
        stage_staff_limits, clear_time_matrices, machine_available_time, release_time,
        max_continuous_run, initial_solution_mode, initial_solution_file,
        periodic_cleaning_time=periodic_cleaning_time,
    )
    progress_logger.on_first_solution_found()
    print(f"   选用初解={initial_mode}, objective={initial_obj}, time={time.time() - t0:.2f}s")
    model_data = _snapshot_export_model_data(
        I, J1, J2, J3, J4, B, p1, p2, p3, p4, T, w, scale,
        clear_time_matrices, max_continuous_run,
    )
    files_exported = _export_snapshot_if_needed(
        result_exporter, initial_snapshot, "partial", model_data,
    )

    seed_list = list(seeds) if seeds else [17]
    best_snapshot = initial_snapshot
    best_obj = initial_obj
    best_seed = initial_mode
    stopped = False
    timed_out = False
    stop_stage = "none"

    def aborted():
        return stopped or timed_out

    def should_stop(stage):
        nonlocal stopped, timed_out, stop_stage
        if stop_checker is None or not stop_checker():
            return False
        is_user_stop = user_stop_checker is None or user_stop_checker()
        if is_user_stop:
            if not stopped:
                progress_logger.on_stop_requested()
            stopped = True
        else:
            timed_out = True
        stop_stage = stage
        return True

    should_stop("initial")

    stage1_budget = _stage_budget(stage1_sec, deadline_monotonic)
    if stage1_budget > 0 and not aborted():
        print(f"3. Stage-1 ALNS预热 ({stage1_budget}s)...")
        stage1_snapshot, _, _, stage1_obj = core._run_alns(
            I, J1, J2, J3, J4, B, p1, p2, p3, p4, d, T, w, scale,
            stage_staff_limits, clear_time_matrices, machine_available_time, release_time,
            max_continuous_run, best_snapshot, best_obj, stage1_budget, num_workers,
            rng_seed=seed_list[0],
            progress_logger=progress_logger,
            periodic_cleaning_time=periodic_cleaning_time,
            stop_checker=stop_checker,
        )
        print(f"   Stage-1 best objective={stage1_obj}")
        if stage1_obj < best_obj:
            progress_logger.on_better_solution_found()
            best_snapshot = stage1_snapshot
            best_obj = stage1_obj
            best_seed = f"warmup-{seed_list[0]}"
        should_stop("stage1")

    stage2_budget = _stage_budget(stage2_sec, deadline_monotonic)
    if stage2_budget > 0 and not aborted():
        print(f"4. Stage-2 ALNS优化 ({stage2_budget}s)...")
        remaining_budget = int(stage2_budget)
        for idx, seed in enumerate(seed_list):
            if should_stop("stage2"):
                break
            remaining_budget = int(_stage_budget(remaining_budget, deadline_monotonic))
            if remaining_budget <= 0:
                timed_out = True
                stop_stage = "stage2"
                break
            seeds_left = len(seed_list) - idx
            run_budget = max(1, remaining_budget // max(1, seeds_left))
            if idx == len(seed_list) - 1:
                run_budget = max(1, remaining_budget)

            run_snapshot, _, _, run_obj = core._run_alns(
                I, J1, J2, J3, J4, B, p1, p2, p3, p4, d, T, w, scale,
                stage_staff_limits, clear_time_matrices, machine_available_time, release_time,
                max_continuous_run, best_snapshot, best_obj, run_budget, num_workers,
                rng_seed=seed,
                progress_logger=progress_logger,
                periodic_cleaning_time=periodic_cleaning_time,
                stop_checker=stop_checker,
            )
            print(f"   seed={seed}, budget={run_budget}s, best objective={run_obj}")
            if run_obj < best_obj:
                progress_logger.on_better_solution_found()
                best_snapshot = run_snapshot
                best_obj = run_obj
                best_seed = seed

            remaining_budget -= run_budget
            if should_stop("stage2") or remaining_budget <= 0:
                break

    if not aborted():
        refined_snapshot, refined_obj, moved_specs = core._left_shift_pack_snapshot(
            best_snapshot, I, J4, B, p4, d, T, w, scale,
            clear_time_matrices, machine_available_time, stage_staff_limits,
        )
        if refined_obj < best_obj:
            progress_logger.on_better_solution_found()
            best_snapshot = refined_snapshot
            best_obj = refined_obj
            best_seed = f"{best_seed}+packshift"
            if moved_specs:
                print(
                    "   ALNS pack_left_shift=" +
                    ",".join(
                        f"{str(spec)}:{round(old / max(1, scale), 2)}->{round(new / max(1, scale), 2)}"
                        for spec, old, new in moved_specs[:6]
                    )
                )
        should_stop("packshift")

    print("5. 将ALNS初始解放入完整CP-SAT模型继续求解...")
    cp_ctx = core._build_model(
        I, J1, J2, J3, J4, B, p1, p2, p3, p4, d, T, w, scale,
        stage_staff_limits=stage_staff_limits,
        clear_time_matrices=clear_time_matrices,
        machine_available_time=machine_available_time,
        release_time=release_time,
        max_continuous_run=max_continuous_run,
        periodic_cleaning_time=periodic_cleaning_time,
    )
    cp_model_obj, cp_vars = cp_ctx["model"], cp_ctx["vars"]
    core._add_snapshot_hints(cp_model_obj, cp_vars, best_snapshot)

    cp_solver = None
    cp_status = cp_model.UNKNOWN
    cp_snapshot = None
    cp_obj = None
    cp_budget = _stage_budget(cp_sec, deadline_monotonic)
    if cp_budget > 0 and not should_stop("cp"):
        cp_solver = core._make_solver(cp_budget, num_workers, seed=seed_list[0], log=True)
        def _cp_log_callback(_message):
            progress_logger.maybe_emit_periodic()
            if stop_checker is not None and stop_checker():
                try:
                    cp_solver.StopSearch()
                except Exception:
                    pass

        cp_solver.log_callback = _cp_log_callback
        cp_status = cp_solver.Solve(cp_model_obj, CpSearchProgressCallback(progress_logger, stop_checker=stop_checker))
        should_stop("cp")
        print(f"   CP status: {status_map.get(cp_status, cp_status)}")
        if cp_status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            cp_snapshot = core._extract_solution_snapshot(cp_solver, cp_vars)
            cp_obj = cp_solver.ObjectiveValue()
            print(f"   CP objective={cp_obj}")

    final_snapshot = best_snapshot
    final_obj = best_obj
    final_solver = core._SnapshotValueSolver(core._snapshot_to_name_map(cp_vars, best_snapshot))
    final_seed = best_seed

    if cp_snapshot is not None and cp_obj is not None and cp_obj <= best_obj:
        progress_logger.on_better_solution_found()
        final_snapshot = cp_snapshot
        final_obj = cp_obj
        final_solver = cp_solver
        final_seed = "alns+cp"

    vars_dict = cp_vars
    total_time = time.time() - t0
    print(f"6. 采用最优seed={final_seed}, objective={final_obj}, total_time={total_time:.2f}s")

    result_kind = "partial" if stopped else "final"
    if result_exporter is not None:
        files_exported = _export_snapshot_if_needed(
            result_exporter, final_snapshot, result_kind, model_data,
        ) or files_exported
    else:
        core.results_to_excel_cp(
            I, B, J1, J2, J3, J4, p1, p2, p3, p4, T,
            vars_dict, final_solver, scale, w, output_file,
            clear_time_matrices=clear_time_matrices,
            max_continuous_run=max_continuous_run,
            periodic_cleaning_time=periodic_cleaning_time,
        )
        print(f"已导出: {output_file}")
        files_exported = True

    if stopped:
        progress_logger.on_stopped_with_result()
    elif timed_out or cp_status != cp_model.OPTIMAL:
        if cp_status == cp_model.INFEASIBLE and final_snapshot is None:
            progress_logger.on_infeasible()
        else:
            progress_logger.on_time_limit()
    else:
        progress_logger.on_optimal_found()

    return {
        "alns_objective": best_obj,
        "final_objective": final_obj,
        "final_seed": final_seed,
        "cp_status": status_map.get(cp_status, cp_status),
        "stopped": stopped,
        "timed_out": timed_out,
        "result_ready": True,
        "files_exported": bool(files_exported),
        "stop_stage": stop_stage,
    }


def solve_pharmaceutical_schedule_cp_hybrid(
    demo_file,
    aps_file,
    speed_preset="balanced",
    scale=None,
    stage1_sec=60,
    stage2_sec=300,
    cp_sec=1800,
    num_workers=8,
    seeds=(11, 23, 37),
    output_file="排产结果明细.xlsx",
    initial_solution_mode="auto",
    initial_solution_file=None,
    progress_logger=None,
    progress_interval_seconds=120,
    progress_emit=None,
    stage_staff_limits_override=None,
    max_continuous_run_override=None,
    major_cleaning_time_override=None,
    minor_cleaning_time_override=None,
    shifts_per_day=2,
    periodic_cleaning_time=1.0,
    stop_checker=None,
    user_stop_checker=None,
    deadline_monotonic=None,
    department="210车间",
    result_exporter=None,
):
    progress_logger = progress_logger or ScheduleSolveProgressLogger(
        interval_seconds=progress_interval_seconds,
        emit=progress_emit,
    )
    progress_logger.on_start()
    try:
        return _solve_pharmaceutical_schedule_cp_hybrid_impl(
            demo_file,
            aps_file,
            speed_preset=speed_preset,
            scale=scale,
            stage1_sec=stage1_sec,
            stage2_sec=stage2_sec,
            cp_sec=cp_sec,
            num_workers=num_workers,
            seeds=seeds,
            output_file=output_file,
            initial_solution_mode=initial_solution_mode,
            initial_solution_file=initial_solution_file,
            progress_logger=progress_logger,
            progress_interval_seconds=progress_interval_seconds,
            progress_emit=progress_emit,
            stage_staff_limits_override=stage_staff_limits_override,
            max_continuous_run_override=max_continuous_run_override,
            major_cleaning_time_override=major_cleaning_time_override,
            minor_cleaning_time_override=minor_cleaning_time_override,
            shifts_per_day=shifts_per_day,
            periodic_cleaning_time=periodic_cleaning_time,
            stop_checker=stop_checker,
            user_stop_checker=user_stop_checker,
            deadline_monotonic=deadline_monotonic,
            department=department,
            result_exporter=result_exporter,
        )
    except Exception:
        progress_logger.on_exception()
        raise


def _delete_if_exists(file_path):
    path = Path(file_path)
    if path.exists():
        path.unlink()


def _snapshot_export_model_data(
    I, J1, J2, J3, J4, B, p1, p2, p3, p4, T, w, scale,
    clear_time_matrices, max_continuous_run,
):
    return {
        "I": I, "J1": J1, "J2": J2, "J3": J3, "J4": J4, "B": B,
        "p1": p1, "p2": p2, "p3": p3, "p4": p4, "T": T, "w": w, "scale": scale,
        "clear_time_matrices": clear_time_matrices,
        "max_continuous_run": max_continuous_run,
    }


def _export_snapshot_if_needed(result_exporter, snapshot, kind, model_data):
    if result_exporter is None or snapshot is None:
        return False
    result_exporter(snapshot, kind, model_data)
    return True


def _export_solution_bundle(snapshot, kind, model_data, export_spec):
    core.results_to_excel_from_snapshot(
        model_data["I"], model_data["B"], model_data["J1"], model_data["J2"],
        model_data["J3"], model_data["J4"], model_data["p1"], model_data["p2"],
        model_data["p3"], model_data["p4"], model_data["T"], snapshot,
        model_data["scale"], model_data["w"], export_spec["raw_output_file"],
        clear_time_matrices=model_data["clear_time_matrices"],
        max_continuous_run=model_data["max_continuous_run"],
        periodic_cleaning_time=export_spec["periodic_cleaning_time"],
    )
    print(f"已导出: {export_spec['raw_output_file']}")
    postprocess_pack_clear(
        export_spec["raw_output_file"],
        export_spec["pack_clear_output_file"],
        export_spec["plan_file"],
        export_spec["aps_file"],
        max_run=export_spec["max_run"],
        clear_duration=export_spec["periodic_cleaning_time"],
        minor_cleaning_time_override=export_spec["minor_cleaning_time_override"],
        department=export_spec["department"],
    )
    postprocess_detail_for_sundays(
        export_spec["pack_clear_output_file"],
        export_spec["sunday_rest_output_file"],
        export_spec["plan_file"],
        export_spec["aps_file"],
        export_spec["schedule_start_date"],
        export_spec["department"],
        shifts_per_day=export_spec["shifts_per_day"],
    )
    visual_file = generate_template_schedule_board(
        export_spec["sunday_rest_output_file"],
        export_spec["plan_file"],
        export_spec["aps_file"],
        export_spec["visual_output_file"],
        export_spec["schedule_start_date"],
        export_spec["department"],
        shifts_per_day=export_spec["shifts_per_day"],
    )
    callback = export_spec.get("on_result_exported")
    if callback:
        callback(kind)
    return visual_file


def _coerce_schedule_start_date(schedule_month):
    if isinstance(schedule_month, datetime):
        return datetime(schedule_month.year, schedule_month.month, 1)
    text = str(schedule_month).strip()
    match = re.match(r"^(\d{4})\s*年\s*(\d{1,2})\s*月?$", text)
    if not match:
        match = re.match(r"^(\d{4})[-/](\d{1,2})(?:[-/]\d{1,2})?$", text)
    if not match:
        raise ValueError(f"无法解析排产月份: {schedule_month}")
    return datetime(int(match.group(1)), int(match.group(2)), 1)


def run_full_schedule_pipeline(
    plan_file,
    aps_file,
    schedule_month,
    raw_output_file,
    pack_clear_output_file,
    sunday_rest_output_file,
    visual_output_file,
    speed_preset="fast",
    scale=None,
    stage1_sec=200,
    stage2_sec=60,
    cp_sec=60,
    num_workers=8,
    seeds=(11, 23, 37),
    initial_solution_mode="best",
    initial_solution_file=None,
    progress_emit=None,
    stage_staff_limits_override=None,
    max_continuous_run_override=None,
    major_cleaning_time_override=None,
    minor_cleaning_time_override=None,
    periodic_cleaning_time=1.0,
    stop_checker=None,
    user_stop_checker=None,
    deadline_monotonic=None,
    department="210车间",
    shifts_per_day=2,
    on_result_exported=None,
):
    schedule_start_date = _coerce_schedule_start_date(schedule_month)
    if initial_solution_mode in ("excel", "best") and initial_solution_file is None:
        initial_solution_file = str(Path(__file__).resolve().parent / "initial_solution.xlsx")

    export_spec = {
        "plan_file": plan_file,
        "aps_file": aps_file,
        "raw_output_file": raw_output_file,
        "pack_clear_output_file": pack_clear_output_file,
        "sunday_rest_output_file": sunday_rest_output_file,
        "visual_output_file": visual_output_file,
        "schedule_start_date": schedule_start_date,
        "max_run": max_continuous_run_override if max_continuous_run_override is not None else 11.0,
        "periodic_cleaning_time": periodic_cleaning_time,
        "minor_cleaning_time_override": minor_cleaning_time_override,
        "department": department,
        "shifts_per_day": shifts_per_day,
        "on_result_exported": on_result_exported,
    }

    def result_exporter(snapshot, kind, model_data):
        _export_solution_bundle(snapshot, kind, model_data, export_spec)

    solve_result = solve_pharmaceutical_schedule_cp_hybrid(
        plan_file,
        aps_file,
        speed_preset=speed_preset,
        scale=scale,
        stage1_sec=stage1_sec,
        stage2_sec=stage2_sec,
        cp_sec=cp_sec,
        num_workers=num_workers,
        seeds=seeds,
        output_file=raw_output_file,
        initial_solution_mode=initial_solution_mode,
        initial_solution_file=initial_solution_file,
        progress_emit=progress_emit,
        stage_staff_limits_override=stage_staff_limits_override,
        max_continuous_run_override=max_continuous_run_override,
        major_cleaning_time_override=major_cleaning_time_override,
        minor_cleaning_time_override=minor_cleaning_time_override,
        shifts_per_day=shifts_per_day,
        periodic_cleaning_time=periodic_cleaning_time,
        stop_checker=stop_checker,
        user_stop_checker=user_stop_checker,
        deadline_monotonic=deadline_monotonic,
        department=department,
        result_exporter=result_exporter,
    )
    if not solve_result.get("result_ready", True):
        return {
            **solve_result,
            "raw_output_file": str(raw_output_file),
            "pack_clear_output_file": None,
            "sunday_rest_output_file": None,
            "visual_output_file": None,
        }

    if not solve_result.get("files_exported"):
        postprocess_pack_clear(
            raw_output_file,
            pack_clear_output_file,
            plan_file,
            aps_file,
            max_run=max_continuous_run_override if max_continuous_run_override is not None else 11.0,
            clear_duration=periodic_cleaning_time,
            minor_cleaning_time_override=minor_cleaning_time_override,
            department=department,
        )
        postprocess_detail_for_sundays(
            pack_clear_output_file,
            sunday_rest_output_file,
            plan_file,
            aps_file,
            schedule_start_date,
            department,
            shifts_per_day=shifts_per_day,
        )
        visual_file = generate_template_schedule_board(
            sunday_rest_output_file,
            plan_file,
            aps_file,
            visual_output_file,
            schedule_start_date,
            department,
            shifts_per_day=shifts_per_day,
        )
        if on_result_exported:
            on_result_exported("final" if not solve_result.get("stopped") else "partial")
    else:
        visual_file = visual_output_file
    return {
        **solve_result,
        "raw_output_file": str(raw_output_file),
        "pack_clear_output_file": str(pack_clear_output_file),
        "sunday_rest_output_file": str(sunday_rest_output_file),
        "visual_output_file": str(visual_file),
    }


if __name__ == "__main__":
    DEMO_FILE = "./输入/药业车间分解编排计划模板.xlsx"
    APS_FILE = "./输入/APS排产信息模板.xlsx"
    RAW_OUTPUT_FILE = "排产结果明细_日志test.xlsx"
    PACK_CLEAR_OUTPUT_FILE = "排产结果明细_packclear__日志test.xlsx"
    INITIAL_SOLUTION_FILE = "initial_solution.xlsx"
    TEMPLATE_VISUAL = "可排产结果可视化_日志test.xlsx"
    SCHEDULE_START_DATE = datetime(2026, 7, 1)

    solve_pharmaceutical_schedule_cp_hybrid(
        DEMO_FILE,
        APS_FILE,
        speed_preset="fast",
        scale=None,
        stage1_sec=200,
        stage2_sec=60,
        cp_sec=60,
        num_workers=8,
        seeds=(11, 23, 37),
        output_file=RAW_OUTPUT_FILE,
        initial_solution_mode="best",  # auto / excel / best
        initial_solution_file=INITIAL_SOLUTION_FILE,
    )

    SUNDAY_REST_OUTPUT_FILE = str(Path(PACK_CLEAR_OUTPUT_FILE).with_name(f"{Path(PACK_CLEAR_OUTPUT_FILE).stem}_sundayrest.xlsx"))


    print("7. 包装工序定期清场后处理...")
    postprocess_pack_clear(RAW_OUTPUT_FILE, PACK_CLEAR_OUTPUT_FILE, DEMO_FILE, APS_FILE)

    print("8. 周日休息后处理...")
    postprocess_detail_for_sundays(
        PACK_CLEAR_OUTPUT_FILE,
        SUNDAY_REST_OUTPUT_FILE,
        DEMO_FILE,
        APS_FILE,
        SCHEDULE_START_DATE,
    )

    print("9. 生成模板式周计划看板...")
    generate_template_schedule_board(
        SUNDAY_REST_OUTPUT_FILE,
        DEMO_FILE,
        APS_FILE,
        TEMPLATE_VISUAL,
        SCHEDULE_START_DATE,
    )

    _delete_if_exists(RAW_OUTPUT_FILE)
    print(f"最终明细文件: {PACK_CLEAR_OUTPUT_FILE}")
    print(f"周日处理明细文件: {SUNDAY_REST_OUTPUT_FILE}")
    print(f"最终可视化文件: {TEMPLATE_VISUAL}")
