import uuid
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.views import APIView

from api.serializers import HistoryImportSerializer
from api.utils import ApiResponse, attachment_file_response, get_request_params
from api.views.common import format_datetime, json_value, paginate
from core.models import ApsArchive, TaskImportRecord, UploadFile, UploadFileItem
from services.excel_service import (
    ExcelValidationError,
    PLAN_TEMPLATE_FILENAME,
    parse_plan_file,
    resolve_media_template,
)


def task_data(task):
    return {
        "taskId": task.taskId, "importId": task.taskId, "sourceType": task.sourceType,
        "sourceTaskId": task.sourceTask_id, "originalFileName": task.file.originalName,
        "apsName": task.apsArchive.archiveName,
        "fileId": task.file_id,
        "apsArchive": {"archiveId": task.apsArchive_id, "archiveName": task.apsArchive.archiveName},
        "remark": task.remark, "dataCount": task.file.items.filter(isDeleted=0).count(),
        "importStatus": task.importStatus, "createdBy": task.createdBy.realName,
        "createTime": format_datetime(task.createTime), "updateTime": format_datetime(task.updateTime),
    }


def plan_item_data(item):
    return {
        "itemId": item.itemId, "fileId": item.file_id, "departmentName": item.departmentName,
        "materialCode": item.materialCode, "inventoryName": item.inventoryName,
        "specification": item.specification, "u8CurrentStock": json_value(item.u8CurrentStock),
        "monthlyProductionPlan": json_value(item.monthlyProductionPlan), "submittedTotal": json_value(item.submittedTotal),
        "createTime": format_datetime(item.createTime),
    }


PLAN_FILTER_FIELDS = {
    "departmentNames": "departmentName",
    "monthlyProductionPlans": "monthlyProductionPlan",
    "inventoryNames": "inventoryName",
}

PLAN_BLANKABLE_CHAR_FIELDS = {"departmentName", "inventoryName"}
_NULL_TOKENS = {"null", "none", "undefined"}
# 与 UploadFileItem.monthlyProductionPlan(decimal_places=4) 对齐
_PLAN_DECIMAL_QUANT = Decimal("0.0001")


def _is_blank_filter_value(value):
    """空字符串表示筛选该字段无值的数据（用于文本筛选项）。"""
    return value is None or (isinstance(value, str) and value.strip() == "")


def _is_monthly_null_token(value):
    """兼容前端传 JSON null、字符串 'null'、或 ['null']。"""
    if value is None:
        return True
    if isinstance(value, str) and value.strip().lower() in _NULL_TOKENS:
        return True
    return False


def _normalize_monthly_plan_value(value):
    """
    将前端传入的产量计划值规范为 Decimal(4位小数)。

    JSON 数字会先变成 float，例如 6333.3333；若直接 Decimal(float) 会变成
    6333.333300000000...，与库中 Decimal('6333.3333') 对不上，导致 __in 漏筛。
    必须先经 str(float) 再转 Decimal。
    """
    try:
        if isinstance(value, bool):
            raise InvalidOperation
        if isinstance(value, Decimal):
            decimal_value = value
        elif isinstance(value, int):
            decimal_value = Decimal(value)
        elif isinstance(value, float):
            decimal_value = Decimal(str(value))
        elif isinstance(value, str):
            decimal_value = Decimal(value.strip())
        else:
            raise InvalidOperation
        return decimal_value.quantize(_PLAN_DECIMAL_QUANT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError, ArithmeticError) as exc:
        raise ValidationError("筛选条件格式不正确") from exc


def _blank_field_condition(field_name):
    if field_name == "monthlyProductionPlan":
        return Q(**{f"{field_name}__isnull": True})
    if field_name in PLAN_BLANKABLE_CHAR_FIELDS:
        return Q(**{field_name: ""}) | Q(**{f"{field_name}__isnull": True})
    return Q(**{f"{field_name}__isnull": True}) | Q(**{field_name: ""})


def apply_plan_item_filters(queryset, params, skip_option=None):
    for option, field_name in PLAN_FILTER_FIELDS.items():
        if option == skip_option:
            continue
        if option not in params:
            continue
        values = params.get(option)

        # monthlyProductionPlans: null / "null" 表示筛选该字段为空的数据
        if option == "monthlyProductionPlans" and _is_monthly_null_token(values):
            queryset = queryset.filter(_blank_field_condition(field_name))
            continue

        if not isinstance(values, (list, tuple)):
            values = [values] if values is not None else []
        if not values:
            continue

        blank_requested = False
        concrete_values = []
        for value in values:
            if option == "monthlyProductionPlans":
                if _is_monthly_null_token(value) or _is_blank_filter_value(value):
                    blank_requested = True
                    continue
                concrete_values.append(_normalize_monthly_plan_value(value))
            elif _is_blank_filter_value(value):
                blank_requested = True
            else:
                concrete_values.append(value)

        condition = Q()
        if concrete_values:
            try:
                condition |= Q(**{f"{field_name}__in": concrete_values})
            except (TypeError, ValueError) as exc:
                raise ValidationError("筛选条件格式不正确") from exc
        if blank_requested:
            condition |= _blank_field_condition(field_name)
        if condition:
            queryset = queryset.filter(condition)
    return queryset


class TaskTemplateView(APIView):
    def get(self, request):
        path = resolve_media_template(PLAN_TEMPLATE_FILENAME)
        return attachment_file_response(open(path, "rb"), PLAN_TEMPLATE_FILENAME)


class TaskHistoryView(APIView):
    def get(self, request):
        params = get_request_params(request)
        queryset = TaskImportRecord.objects.filter(createdBy=request.user, isDeleted=0).select_related("file", "apsArchive", "createdBy").order_by("-createTime")
        total, records, page, page_size = paginate(queryset, params.get("page"), params.get("pageSize"))
        return ApiResponse({"total": total, "page": page, "pageSize": page_size, "records": [task_data(x) for x in records]}, message="查询成功")


class TaskDeleteView(APIView):
    def post(self, request):
        task_id = get_request_params(request).get("importId") or get_request_params(request).get("taskId")
        task = TaskImportRecord.objects.filter(taskId=task_id, createdBy=request.user, isDeleted=0).first()
        if not task:
            raise NotFound("历史记录不存在")
        task.isDeleted = 1
        task.save(update_fields=["isDeleted", "updateTime"])
        return ApiResponse(message="删除成功")


class TaskHistoryImportView(APIView):
    def post(self, request):
        serializer = HistoryImportSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        source = TaskImportRecord.objects.filter(taskId=serializer.validated_data["taskId"], createdBy=request.user, isDeleted=0, importStatus=1).select_related("file", "apsArchive", "createdBy").first()
        if not source:
            raise NotFound("历史导入记录不存在")
        if source.apsArchive.isDeleted:
            raise ValidationError("该历史记录关联的APS方案已不存在，无法导入")
        task = TaskImportRecord.objects.create(sourceType=2, sourceTask=source, apsArchive=source.apsArchive, file=source.file, remark="复用历史记录导入", importStatus=1, createdBy=request.user)
        return ApiResponse(task_data(task), message="历史记录导入成功")


class TaskImportView(APIView):
    def post(self, request):
        archive_id = request.data.get("apsArchiveId")
        uploaded = request.FILES.get("file")
        if not archive_id or not uploaded:
            raise ValidationError("APS方案和计划文件不能为空")
        archive = ApsArchive.objects.filter(archiveId=archive_id, createdBy=request.user, isDeleted=0).first()
        if not archive:
            raise NotFound("APS方案不存在")
        try:
            rows = parse_plan_file(uploaded)
        except ExcelValidationError as exc:
            raise ValidationError(str(exc)) from exc
        root = Path(settings.TASK_UPLOAD_ROOT)
        root.mkdir(parents=True, exist_ok=True)
        stored_name = f"{uuid.uuid4().hex}{Path(uploaded.name).suffix.lower()}"
        target = root / stored_name
        with target.open("wb") as stream:
            for chunk in uploaded.chunks():
                stream.write(chunk)
        try:
            with transaction.atomic():
                file = UploadFile.objects.create(originalName=uploaded.name, fileName=stored_name, filePath=str(target), uploadUser=request.user, parseStatus=1)
                UploadFileItem.objects.bulk_create([UploadFileItem(file=file, **row) for row in rows], batch_size=500)
                task = TaskImportRecord.objects.create(apsArchive=archive, file=file, remark=(request.data.get("remark") or "").strip() or None, importStatus=1, createdBy=request.user)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        return ApiResponse(task_data(task), message="计划文件导入成功")


class TaskDetailView(APIView):
    def get(self, request):
        params = get_request_params(request)
        task_id = params.get("importId") or params.get("taskId")
        task = TaskImportRecord.objects.filter(taskId=task_id, createdBy=request.user, isDeleted=0).first()
        if not task:
            raise NotFound("任务记录不存在")
        queryset = UploadFileItem.objects.filter(file=task.file, isDeleted=0)
        keyword = (params.get("keyword") or "").strip()
        if keyword:
            queryset = queryset.filter(Q(materialCode__icontains=keyword) | Q(inventoryName__icontains=keyword) | Q(specification__icontains=keyword))

        queryset = apply_plan_item_filters(queryset, params).order_by("departmentName", "itemId")
        total, records, page, page_size = paginate(queryset, params.get("page"), params.get("pageSize"))
        return ApiResponse({"total": total, "page": page, "pageSize": page_size, "records": [plan_item_data(x) for x in records]}, message="查询成功")


class TaskDetailFilterOptionsView(APIView):
    def post(self, request):
        params = get_request_params(request)
        task_id = params.get("taskId")
        option = params.get("option")
        if not task_id:
            raise ValidationError("taskId不能为空")
        if option not in PLAN_FILTER_FIELDS:
            raise ValidationError(
                "option必须为departmentNames、monthlyProductionPlans或inventoryNames"
            )

        task = TaskImportRecord.objects.filter(
            taskId=task_id, createdBy=request.user, isDeleted=0
        ).select_related("file").first()
        if not task:
            raise NotFound("任务记录不存在")

        field_name = PLAN_FILTER_FIELDS[option]
        queryset = UploadFileItem.objects.filter(file=task.file, isDeleted=0)
        queryset = apply_plan_item_filters(queryset, params, skip_option=option)

        blank_condition = _blank_field_condition(field_name)
        has_blank = queryset.filter(blank_condition).exists()

        valued = queryset.exclude(blank_condition)
        values = list(valued.order_by(field_name).values_list(field_name, flat=True).distinct())
        result = [json_value(value) for value in values]
        if has_blank:
            # monthlyProductionPlans 用 null，其余文本筛选用 ""
            blank_token = None if field_name == "monthlyProductionPlan" else ""
            result.insert(0, blank_token)
        return ApiResponse(result, message="查询成功")
