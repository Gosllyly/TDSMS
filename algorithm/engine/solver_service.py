import json
import math
import multiprocessing
import os
import signal
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

from run_schedule_hybrid import run_full_schedule_pipeline


TASK_ROOT = Path(__file__).resolve().parent / "runs"
RUNNING_PROCESSES = {}
STATUS_PENDING = "PENDING"
STATUS_RUNNING = "RUNNING"
STATUS_STOPPING = "STOPPING"
STATUS_SUCCESS = "SUCCESS"
STATUS_FAILED = "FAILED"
STATUS_STOPPED = "STOPPED"
STOP_REQUEST_FILE = "stop.requested"


def parse_schedule_month(scheduleMonth):
    from run_schedule_hybrid import _coerce_schedule_start_date

    return _coerce_schedule_start_date(scheduleMonth)


def allocate_solver_seconds(solverTimeLimitMinutes):
    total_sec = int(round(float(solverTimeLimitMinutes) * 60))
    weights = (20, 6, 6)
    weight_sum = sum(weights)
    raw = [total_sec * weight / weight_sum for weight in weights]
    seconds = [int(math.floor(value)) for value in raw]
    remainder = total_sec - sum(seconds)
    order = sorted(range(len(raw)), key=lambda idx: (raw[idx] - seconds[idx], -idx), reverse=True)
    for idx in order[:remainder]:
        seconds[idx] += 1
    return tuple(seconds)


def build_solver_options(
    scheduleMonth,
    continuousRunLimitDays,
    majorCleaningDays,
    minorCleaningDays,
    periodicCleaningDays,
    naturalDays,
    shiftCount,
    mixing,
    tableting,
    coating,
    packaging,
    solverTimeLimitMinutes,
    department="210车间",
):
    natural_days = float(naturalDays)
    if natural_days <= 0:
        raise ValueError("naturalDays must be greater than zero")
    shifts_per_day = float(shiftCount) / natural_days
    if shifts_per_day <= 0 or abs(shifts_per_day - round(shifts_per_day)) > 1e-9:
        raise ValueError("shiftCount / naturalDays must be a positive integer")
    shifts_per_day = int(round(shifts_per_day))
    stage1_sec, stage2_sec, cp_sec = allocate_solver_seconds(solverTimeLimitMinutes)
    return {
        "schedule_start_date": parse_schedule_month(scheduleMonth),
        "natural_days": natural_days,
        "shifts_per_day": shifts_per_day,
        "continuous_run_shifts": float(continuousRunLimitDays) * shifts_per_day,
        "major_cleaning_shifts": float(majorCleaningDays) * shifts_per_day,
        "minor_cleaning_shifts": float(minorCleaningDays) * shifts_per_day,
        "periodic_cleaning_shifts": float(periodicCleaningDays) * shifts_per_day,
        "stage_staff_limits": {
            1: int(mixing),
            2: int(tableting),
            3: int(coating),
            4: int(packaging),
        },
        "department": str(department),
        "stage_seconds": {
            "stage1_sec": stage1_sec,
            "stage2_sec": stage2_sec,
            "cp_sec": cp_sec,
        },
    }


def run_solver(
    planFile,
    apsFile,
    scheduleMonth,
    continuousRunLimitDays,
    majorCleaningDays,
    minorCleaningDays,
    periodicCleaningDays,
    naturalDays,
    shiftCount,
    mixing,
    tableting,
    coating,
    packaging,
    solverTimeLimitMinutes,
    department="210车间",
    taskId=None,
    task_root=None,
    run_inline=False,
):
    task_root = _task_root(task_root)
    task_root.mkdir(parents=True, exist_ok=True)
    # Django 适配层传入 solveTaskId，保证运行目录与业务任务 ID 一致。
    task_id = str(taskId) if taskId is not None else _new_task_id()
    task_dir = task_root / task_id
    task_dir.mkdir(parents=True, exist_ok=False)

    options = build_solver_options(
        scheduleMonth,
        continuousRunLimitDays,
        majorCleaningDays,
        minorCleaningDays,
        periodicCleaningDays,
        naturalDays,
        shiftCount,
        mixing,
        tableting,
        coating,
        packaging,
        solverTimeLimitMinutes,
        department=department,
    )
    params = {
        "planFile": str(planFile),
        "apsFile": str(apsFile),
        "scheduleMonth": str(scheduleMonth),
        "continuousRunLimitDays": continuousRunLimitDays,
        "majorCleaningDays": majorCleaningDays,
        "minorCleaningDays": minorCleaningDays,
        "periodicCleaningDays": periodicCleaningDays,
        "naturalDays": naturalDays,
        "shiftCount": shiftCount,
        "mixing": mixing,
        "tableting": tableting,
        "coating": coating,
        "packaging": packaging,
        "solverTimeLimitMinutes": solverTimeLimitMinutes,
        "department": str(department),
        "options": _jsonable_options(options),
    }
    _write_json(task_dir / "params.json", params)
    _write_status(task_dir, _initial_status(task_id, task_dir, params))
    (task_dir / "solver.log").touch()

    worker_args = (task_id, str(task_dir), params)
    if run_inline:
        _solver_worker(*worker_args)
        return {"taskId": task_id, "status": query_solver_status(task_id, task_root=task_root)["status"]}

    process = multiprocessing.Process(target=_solver_worker, args=worker_args, daemon=False)
    process.start()
    RUNNING_PROCESSES[task_id] = process
    _update_status(task_dir, status=STATUS_RUNNING, pid=process.pid, startedAt=_now())
    return {"taskId": task_id, "status": STATUS_RUNNING}


def query_solver_status(taskId, task_root=None):
    task_dir = _task_dir(taskId, task_root)
    return _read_json(task_dir / "status.json")


def query_solver_log(taskId, offset=0, limit=None, task_root=None):
    log_path = _task_dir(taskId, task_root) / "solver.log"
    if not log_path.exists():
        return {"taskId": taskId, "offset": int(offset), "content": "", "nextOffset": int(offset)}
    text = log_path.read_text(encoding="utf-8", errors="replace")
    start = max(0, int(offset or 0))
    end = len(text) if limit is None else min(len(text), start + int(limit))
    return {"taskId": taskId, "offset": start, "content": text[start:end], "nextOffset": end}


def stop_solver(taskId, task_root=None):
    task_dir = _task_dir(taskId, task_root)
    (task_dir / STOP_REQUEST_FILE).write_text(_now(), encoding="utf-8")
    visual_file = Path(_result_files(task_dir)["visualBoard"])
    if not visual_file.is_file():
        return _force_stop_solver(taskId, task_root=task_root)
    _update_status(
        task_dir,
        status=STATUS_STOPPING,
        stopRequested=True,
        stopMode="graceful",
        progressMessage="收到停止请求，正在导出当前最佳方案",
        error=None,
    )
    return {"taskId": taskId, "status": STATUS_STOPPING}


def export_solver_result(taskId, task_root=None):
    status = query_solver_status(taskId, task_root=task_root)
    visual_file = status.get("resultFiles", {}).get("visualBoard")
    if not visual_file:
        raise FileNotFoundError(f"任务 {taskId} 没有可导出的可视化结果")
    path = Path(visual_file)
    if not path.exists():
        raise FileNotFoundError(f"可视化结果文件不存在: {visual_file}")
    return {"taskId": taskId, "filePath": str(path)}


def _solver_worker(task_id, task_dir_text, params):
    task_dir = Path(task_dir_text)
    _update_status(task_dir, status=STATUS_RUNNING, pid=os.getpid(), startedAt=_now())
    # 限时从算法开始计算起算，严格遵循前端传入的 solverTimeLimitMinutes。
    deadline = time.monotonic() + float(params["solverTimeLimitMinutes"]) * 60

    def user_stop_checker():
        return _stop_requested(task_dir)

    def stop_checker():
        return user_stop_checker() or time.monotonic() >= deadline

    try:
        def emit(message):
            _append_log(task_dir, str(message))
            _update_status(task_dir, progressMessage=str(message))

        def on_result_exported(kind):
            visual_file = Path(_result_files(task_dir)["visualBoard"])
            if not visual_file.is_file():
                return
            _update_status(
                task_dir,
                resultReady=True,
                resultKind=kind,
                resultFiles=_result_files(task_dir),
            )

        result = _run_pipeline_for_task(
            task_dir,
            params,
            emit,
            stop_checker=stop_checker,
            user_stop_checker=user_stop_checker,
            deadline_monotonic=deadline,
            on_result_exported=on_result_exported,
        )
        status = _read_json(task_dir / "status.json")
        if user_stop_checker():
            visual_file = Path(_result_files(task_dir)["visualBoard"])
            result_ready = visual_file.exists()
            _update_status(
                task_dir,
                status=STATUS_STOPPED,
                finishedAt=_now(),
                stopRequested=True,
                stopMode="graceful",
                resultReady=result_ready,
                resultKind="partial" if result_ready else None,
                error=None if result_ready else "未生成可行解",
                result=result,
                resultFiles=_result_files(task_dir) if result_ready else {},
                progressMessage=(
                    "已停止搜索并输出当前最佳方案"
                    if result_ready else
                    "已停止搜索，未生成可行解"
                ),
            )
        elif status.get("status") != STATUS_STOPPED:
            visual_ready = Path(_result_files(task_dir)["visualBoard"]).exists()
            _update_status(
                task_dir,
                status=STATUS_SUCCESS,
                finishedAt=_now(),
                error=None,
                stopRequested=False,
                resultReady=visual_ready,
                resultKind="final" if visual_ready else None,
                result=result,
                resultFiles=_result_files(task_dir) if visual_ready else {},
            )
    except BaseException as exc:
        status = _read_json(task_dir / "status.json")
        if status.get("status") != STATUS_STOPPED:
            _update_status(task_dir, status=STATUS_FAILED, finishedAt=_now(), error=str(exc))
        raise


def _run_pipeline_for_task(
    task_dir,
    params,
    emit,
    stop_checker=None,
    user_stop_checker=None,
    deadline_monotonic=None,
    on_result_exported=None,
):
    options = params["options"]
    stage_seconds = options["stage_seconds"]
    raw_file = task_dir / "排产结果明细.xlsx"
    pack_file = task_dir / "排产结果明细_packclear.xlsx"
    sunday_file = task_dir / "排产结果明细_packclear_sundayrest.xlsx"
    visual_file = task_dir / "可排产结果可视化.xlsx"
    return run_full_schedule_pipeline(
        plan_file=params["planFile"],
        aps_file=params["apsFile"],
        schedule_month=params["scheduleMonth"],
        raw_output_file=raw_file,
        pack_clear_output_file=pack_file,
        sunday_rest_output_file=sunday_file,
        visual_output_file=visual_file,
        stage1_sec=stage_seconds["stage1_sec"],
        stage2_sec=stage_seconds["stage2_sec"],
        cp_sec=stage_seconds["cp_sec"],
        progress_emit=emit,
        stage_staff_limits_override={int(k): v for k, v in options["stage_staff_limits"].items()},
        max_continuous_run_override=options["continuous_run_shifts"],
        major_cleaning_time_override=options["major_cleaning_shifts"],
        minor_cleaning_time_override=options["minor_cleaning_shifts"],
        shifts_per_day=options["shifts_per_day"],
        periodic_cleaning_time=options["periodic_cleaning_shifts"],
        stop_checker=stop_checker,
        user_stop_checker=user_stop_checker,
        deadline_monotonic=deadline_monotonic,
        department=options.get("department", "210车间"),
        on_result_exported=on_result_exported,
    )


def _initial_status(task_id, task_dir, params):
    return {
        "taskId": task_id,
        "status": STATUS_PENDING,
        "pid": None,
        "createdAt": _now(),
        "startedAt": None,
        "finishedAt": None,
        "progressMessage": None,
        "error": None,
        "stopRequested": False,
        "stopMode": None,
        "resultReady": False,
        "resultKind": None,
        "paramsFile": str(task_dir / "params.json"),
        "logFile": str(task_dir / "solver.log"),
        "resultFiles": {},
        "request": params,
    }


def _result_files(task_dir):
    return {
        "rawDetail": str(task_dir / "排产结果明细.xlsx"),
        "packClearDetail": str(task_dir / "排产结果明细_packclear.xlsx"),
        "sundayRestDetail": str(task_dir / "排产结果明细_packclear_sundayrest.xlsx"),
        "visualBoard": str(task_dir / "可排产结果可视化.xlsx"),
    }


def _task_root(task_root=None):
    return Path(task_root) if task_root is not None else TASK_ROOT


def _task_dir(task_id, task_root=None):
    task_dir = _task_root(task_root) / str(task_id)
    if not task_dir.exists():
        raise FileNotFoundError(f"任务不存在: {task_id}")
    return task_dir


def _new_task_id():
    return f"{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}"


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _jsonable_options(options):
    result = dict(options)
    result["schedule_start_date"] = options["schedule_start_date"].isoformat()
    return result


def _write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _write_status(task_dir, status):
    _write_json(task_dir / "status.json", status)


def _update_status(task_dir, **updates):
    path = task_dir / "status.json"
    status = _read_json(path) if path.exists() else {}
    status.update(updates)
    _write_json(path, status)
    return status


def _append_log(task_dir, message):
    log_path = task_dir / "solver.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log_file:
        log_file.write(f"[{_now()}] {message}\n")
        log_file.flush()


def _stop_requested(task_dir):
    return (task_dir / STOP_REQUEST_FILE).exists()


def _force_stop_solver(taskId, task_root=None):
    task_dir = _task_dir(taskId, task_root)
    status = _read_json(task_dir / "status.json")
    process = RUNNING_PROCESSES.pop(taskId, None)
    if process is not None:
        process.terminate()
        process.join(timeout=5)
    elif status.get("pid"):
        try:
            os.kill(int(status["pid"]), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
    _update_status(task_dir, status=STATUS_STOPPED, finishedAt=_now(), stopRequested=True, stopMode="force")
    return {"taskId": taskId, "status": STATUS_STOPPED}


if __name__ == "__main__":
    print("solver_service provides Python APIs; import and call run_solver/query/stop/export.", file=sys.stderr)
