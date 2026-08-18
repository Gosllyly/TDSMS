from pathlib import Path

from django.db import transaction
from rest_framework import status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.views import APIView

from algorithm.adapter import (
    log_path,
    parse_log_line,
    stop_solver,
    submit_solver,
    sync_solve_task,
)
from api.serializers import SolveMatchCheckSerializer, SolveStartSerializer
from api.utils import ApiResponse, attachment_file_response, get_request_params
from api.views.common import format_datetime
from core.models import SolveTask, TaskImportRecord
from services.solve_match_service import match_check_data


def get_owned_solve(user, solve_id):
    task = SolveTask.objects.filter(solveTaskId=solve_id, createdUser=user, isDeleted=0).select_related("task").first()
    if not task:
        raise NotFound("求解任务不存在")
    return task


def solve_data(solve, algorithm_state=None):
    data = {
        "solveTaskId": solve.solveTaskId, "taskId": solve.task_id, "importId": solve.task_id,
        "inputParams": solve.inputParams, "solveStatus": solve.solveStatus,
        "finishReason": solve.finishReason, "startTime": format_datetime(solve.startTime),
        "finishTime": format_datetime(solve.finishTime), "createTime": format_datetime(solve.createTime),
        "hasPartialResult": bool(solve.partialResultFilePath and Path(solve.partialResultFilePath).is_file()),
        "hasFinalResult": bool(solve.resultFilePath and Path(solve.resultFilePath).is_file()),
    }
    if algorithm_state:
        data["progressMessage"] = algorithm_state.get("progressMessage")
        data["errorMessage"] = algorithm_state.get("error")
    return data


class SolveStartView(APIView):
    def post(self, request):
        serializer = SolveStartSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        task_id = data.pop("taskId", None) or data.pop("importId", None)
        task = TaskImportRecord.objects.filter(
            taskId=task_id,
            createdBy=request.user,
            isDeleted=0,
            importStatus=1,
        ).select_related("file").first()
        if not task:
            raise NotFound("任务记录不存在或尚未导入成功")
        if not task.file.items.filter(
            departmentName=data["department"],
            isDeleted=0,
        ).exists():
            raise ValidationError("所选部门在当前任务计划中不存在")
        active_queryset = SolveTask.objects.filter(
            createdUser=request.user,
            isDeleted=0,
            solveStatus__in=[0, 1],
        )
        # 防止算法已经结束、数据库监控尚未来得及同步时误拦截新任务。
        for active_solve in active_queryset:
            sync_solve_task(active_solve)

        with transaction.atomic():
            # 在支持行锁的数据库中串行化同一用户的并发提交请求。
            request.user.__class__.objects.select_for_update().get(pk=request.user.pk)
            for active_solve in SolveTask.objects.filter(
                createdUser=request.user,
                isDeleted=0,
                solveStatus__in=[0, 1],
            ).order_by("createTime"):
                try:
                    stop_solver(active_solve.solveTaskId)
                except FileNotFoundError:
                    continue
            solve = SolveTask.objects.create(task=task, inputParams=data, createdUser=request.user)
        submit_solver(solve.solveTaskId)
        response_data = {
            "solveTaskId": solve.solveTaskId,
            "taskId": task.taskId,
            "importId": task.taskId,
            "solveStatus": 0,
            "unmatchedItemPolicy": data["unmatchedItemPolicy"],
            "createTime": format_datetime(solve.createTime),
        }
        return ApiResponse(response_data, status.HTTP_200_OK, "求解任务已创建")


class SolveMatchCheckView(APIView):
    def post(self, request):
        serializer = SolveMatchCheckSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        task_id = data.get("taskId") or data.get("importId")
        task = TaskImportRecord.objects.filter(
            taskId=task_id,
            createdBy=request.user,
            isDeleted=0,
            importStatus=1,
        ).select_related("file", "apsArchive").first()
        if not task:
            raise NotFound("任务记录不存在或尚未导入成功")
        result = match_check_data(task, data["page"], data["pageSize"])
        message = (
            "计划数据与APS档案匹配通过"
            if result["status"]
            else "存在未匹配的品种或规格，请先维护APS档案"
        )
        return ApiResponse(result, message=message)


class SolveQueryView(APIView):
    def get(self, request):
        solve_id = get_request_params(request).get("solveTaskId")
        if not solve_id:
            raise ValidationError("solveTaskId不能为空")
        solve = get_owned_solve(request.user, solve_id)
        solve, algorithm_state = sync_solve_task(solve)
        return ApiResponse(solve_data(solve, algorithm_state), message="查询成功")


class SolveLogsView(APIView):
    def get(self, request):
        solve_id = get_request_params(request).get("solveTaskId")
        if not solve_id:
            raise ValidationError("solveTaskId不能为空")
        solve = get_owned_solve(request.user, solve_id)
        path = log_path(solve.solveTaskId)
        logs = []
        if path.is_file():
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                created, content = parse_log_line(line)
                if not created:
                    continue
                logs.append({
                    "logContent": content,
                    "createTime": created.replace("T", " ", 1),
                })
            logs.reverse()
            for index, record in enumerate(logs, start=1):
                record["logId"] = index
        return ApiResponse(logs, message="查询成功")


class SolveStopView(APIView):
    def post(self, request):
        solve_id = get_request_params(request).get("solveTaskId")
        solve = get_owned_solve(request.user, solve_id)
        solve, _ = sync_solve_task(solve, cleanup=False)
        if solve.solveStatus not in [0, 1]:
            raise ValidationError("当前求解任务已结束，不允许停止")
        stop_solver(solve.solveTaskId)
        solve.refresh_from_db()
        return ApiResponse(
            {
                "solveTaskId": solve.solveTaskId,
                "solveStatus": solve.solveStatus,
                "stopRequested": True,
                "hasPartialResult": bool(solve.partialResultFilePath and Path(solve.partialResultFilePath).is_file()),
            },
            status=status.HTTP_202_ACCEPTED,
            message="停止请求已接收，系统正在生成当前最优结果",
        )


class SolveResultView(APIView):
    def post(self, request):
        solve_id = get_request_params(request).get("solveTaskId")
        solve = get_owned_solve(request.user, solve_id)
        solve, algorithm_state = sync_solve_task(solve)
        if solve.solveStatus == 2 and solve.resultFilePath:
            file_path = solve.resultFilePath
        elif solve.partialResultFilePath:
            file_path = solve.partialResultFilePath
        elif algorithm_state and algorithm_state.get("stopRequested"):
            return ApiResponse(
                status=status.HTTP_409_CONFLICT,
                message="停止请求正在处理，结果文件尚未生成，请稍后重试",
            )
        else:
            return ApiResponse(
                status=status.HTTP_409_CONFLICT,
                message="当前任务尚未生成可导出的排程结果",
            )
        path = Path(file_path) if file_path else None
        if not path or not path.is_file():
            message = (
                "当前任务停止前未生成可导出的排产结果"
                if solve.solveStatus == 4
                else "结果文件不存在"
            )
            return ApiResponse(status=status.HTTP_409_CONFLICT, message=message)
        return attachment_file_response(
            open(path, "rb"),
            "可排产结果可视化.xlsx",
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
