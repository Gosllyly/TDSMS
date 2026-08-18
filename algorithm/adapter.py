"""Django 与排程算法之间的适配层。

算法核心只接收文件路径和求解参数；本模块负责从业务模型准备输入文件、
同步状态与结果路径，以及清理求解结束后的临时 APS 文件和中间结果。
"""

from __future__ import annotations

import importlib
import re
import shutil
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from core.models import SolveTask
from services.aps_export_service import export_aps_archive


ENGINE_STATUS_TO_DB = {
    "PENDING": 0,
    "RUNNING": 1,
    "STOPPING": 1,
    "SUCCESS": 2,
    "FAILED": 3,
    "STOPPED": 4,
}
_ENGINE_DIR = Path(__file__).resolve().parent / "engine"
_monitoring = set()
_monitor_lock = threading.Lock()


def _engine_service():
    """延迟加载算法，避免 Django 管理命令无条件加载大型求解依赖。"""
    engine_path = str(_ENGINE_DIR)
    if engine_path not in sys.path:
        sys.path.insert(0, engine_path)
    return importlib.import_module("solver_service")


def _run_root():
    root = Path(settings.ALGORITHM_RUN_ROOT)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _input_dir(solve_task_id):
    return Path(settings.ALGORITHM_INPUT_ROOT) / str(solve_task_id)


def log_path(solve_task_id):
    engine_log = _run_root() / str(solve_task_id) / "solver.log"
    if engine_log.exists():
        return engine_log
    # 保留该回退路径，便于尚未真正启动算法时记录启动错误和接口测试日志。
    root = Path(settings.ALGORITHM_LOG_ROOT)
    root.mkdir(parents=True, exist_ok=True)
    return root / f"solve_{solve_task_id}.txt"


def append_log(solve_task_id, content):
    with log_path(solve_task_id).open("a", encoding="utf-8") as stream:
        stream.write(f"{timezone.now().isoformat()}\t{content}\n")


def _prepare_aps_file(solve):
    target_dir = _input_dir(solve.solveTaskId)
    target_dir.mkdir(parents=True, exist_ok=False)
    target = target_dir / "APS排产信息档案.xlsx"
    items = solve.task.apsArchive.items.filter(isDeleted=0).order_by("itemId")
    output = export_aps_archive(items)
    with target.open("wb") as stream:
        stream.write(output.getvalue())
    return target


def _algorithm_arguments(solve, aps_file):
    params = solve.inputParams
    rules = params["productionRules"]
    cleaning = rules["cleaningDuration"]
    conversion = rules["shiftConversion"]
    capacity = params["personnelCapacity"]
    plan_file = Path(solve.task.file.filePath)
    if not plan_file.is_file():
        raise FileNotFoundError(f"计划原始文件不存在: {plan_file}")
    return {
        "taskId": solve.solveTaskId,
        "planFile": str(plan_file),
        "apsFile": str(aps_file),
        "scheduleMonth": params["scheduleMonth"],
        "continuousRunLimitDays": rules["continuousRunLimitDays"],
        "majorCleaningDays": cleaning["majorCleaningDays"],
        "minorCleaningDays": cleaning["minorCleaningDays"],
        "periodicCleaningDays": cleaning["periodicCleaningDays"],
        "naturalDays": conversion["naturalDays"],
        "shiftCount": conversion["shiftCount"],
        "mixing": capacity["mixing"],
        "tableting": capacity["tableting"],
        "coating": capacity["coating"],
        "packaging": capacity["packaging"],
        "solverTimeLimitMinutes": params["solverTimeLimitMinutes"],
        "department": params["department"],
        "task_root": str(_run_root()),
    }


def submit_solver(solve_task_id):
    solve = SolveTask.objects.filter(solveTaskId=solve_task_id).select_related(
        "task__file", "task__apsArchive",
    ).first()
    if not solve:
        raise FileNotFoundError(f"求解任务不存在: {solve_task_id}")

    input_dir = _input_dir(solve_task_id)
    if input_dir.exists():
        shutil.rmtree(input_dir)
    try:
        aps_file = _prepare_aps_file(solve)
        result = _engine_service().run_solver(**_algorithm_arguments(solve, aps_file))
        sync_solve_task(solve, cleanup=False)
        _start_monitor(solve_task_id)
        return result
    except Exception:
        shutil.rmtree(input_dir, ignore_errors=True)
        solve.solveStatus = 3
        solve.finishReason = 4
        solve.finishTime = timezone.now()
        solve.save(update_fields=["solveStatus", "finishReason", "finishTime"])
        raise


def query_solver_state(solve_task_id):
    try:
        return _engine_service().query_solver_status(
            solve_task_id,
            task_root=str(_run_root()),
        )
    except (FileNotFoundError, OSError, ValueError):
        return None


def _parse_engine_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _finish_reason(state):
    status = state.get("status")
    if status == "STOPPED":
        return 3
    if status == "FAILED":
        return 4
    if status == "SUCCESS":
        result = state.get("result") or {}
        return 1 if result.get("cp_status") == "OPTIMAL" else 2
    return None


def _is_finished_state(state):
    status = state.get("status")
    return status in {"SUCCESS", "FAILED"} or (
        status == "STOPPED" and bool(state.get("finishedAt"))
    )


def sync_solve_task(solve_or_id, cleanup=True):
    solve = (
        solve_or_id
        if isinstance(solve_or_id, SolveTask)
        else SolveTask.objects.filter(solveTaskId=solve_or_id).first()
    )
    if not solve:
        return None, None
    state = query_solver_state(solve.solveTaskId)
    if not state:
        return solve, None

    engine_status = state.get("status")
    # STOPPED without finishedAt is compatible with an older engine state file:
    # the stop signal was accepted, but result post-processing is not finished yet.
    if engine_status == "STOPPED" and not state.get("finishedAt"):
        solve.solveStatus = 1
    else:
        solve.solveStatus = ENGINE_STATUS_TO_DB.get(engine_status, solve.solveStatus)
    solve.startTime = _parse_engine_time(state.get("startedAt")) or solve.startTime
    solve.finishTime = _parse_engine_time(state.get("finishedAt")) or solve.finishTime
    solve.finishReason = _finish_reason(state) if _is_finished_state(state) else None

    visual_path = (state.get("resultFiles") or {}).get("visualBoard")
    visual = Path(visual_path) if visual_path else None
    if visual and visual.is_file():
        kind = state.get("resultKind")
        status = state.get("status")
        if status == "SUCCESS" or kind == "final":
            solve.resultFilePath = str(visual)
        if status == "STOPPED" or kind == "partial":
            solve.partialResultFilePath = str(visual)

    solve.save(update_fields=[
        "solveStatus", "startTime", "finishTime", "finishReason",
        "resultFilePath", "partialResultFilePath",
    ])
    if cleanup and _is_finished_state(state):
        _cleanup_finished_files(solve.solveTaskId, state)
    return solve, state


def stop_solver(solve_task_id):
    state = query_solver_state(solve_task_id)
    if not state:
        # 兼容真实算法接入前遗留的占位任务：这类任务只有数据库记录，
        # 没有算法运行目录，因此无法发送文件停止信号。
        updated = SolveTask.objects.filter(
            solveTaskId=solve_task_id,
            isDeleted=0,
            solveStatus__in=[0, 1],
        ).update(
            solveStatus=4,
            finishReason=3,
            finishTime=timezone.now(),
        )
        if not updated:
            raise FileNotFoundError(f"运行中的求解任务不存在: {solve_task_id}")
        append_log(solve_task_id, "遗留求解任务缺少算法运行记录，已由系统标记为停止")
        shutil.rmtree(_input_dir(solve_task_id), ignore_errors=True)
        return {"taskId": solve_task_id, "status": "STOPPED", "orphaned": True}

    result = _engine_service().stop_solver(
        solve_task_id,
        task_root=str(_run_root()),
    )
    sync_solve_task(solve_task_id, cleanup=False)
    _start_monitor(solve_task_id)
    return result


def _cleanup_finished_files(solve_task_id, state):
    shutil.rmtree(_input_dir(solve_task_id), ignore_errors=True)
    result_files = state.get("resultFiles") or {}
    for key in ("rawDetail", "packClearDetail", "sundayRestDetail"):
        value = result_files.get(key)
        if value:
            Path(value).unlink(missing_ok=True)


def _start_monitor(solve_task_id):
    with _monitor_lock:
        if solve_task_id in _monitoring:
            return
        _monitoring.add(solve_task_id)
    thread = threading.Thread(
        target=_monitor_solver,
        args=(solve_task_id,),
        name=f"solve-monitor-{solve_task_id}",
        daemon=True,
    )
    thread.start()


def _monitor_solver(solve_task_id):
    try:
        while True:
            close_old_connections()
            solve, state = sync_solve_task(solve_task_id, cleanup=False)
            if not solve or (state and _is_finished_state(state)):
                if state:
                    _cleanup_finished_files(solve_task_id, state)
                return
            time.sleep(1)
    finally:
        close_old_connections()
        with _monitor_lock:
            _monitoring.discard(solve_task_id)


_ENGINE_LOG_PATTERN = re.compile(r"^\[([^\]]+)]\s*(.*)$")


def parse_log_line(line):
    if "\t" in line:
        created, content = line.split("\t", 1)
        return created, content
    matched = _ENGINE_LOG_PATTERN.match(line)
    if matched:
        return matched.group(1), matched.group(2)
    return None, line
