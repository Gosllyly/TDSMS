import uuid
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.http import FileResponse
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.views import APIView

from api.serializers import HistoryImportSerializer
from api.utils import ApiResponse, get_request_params
from api.views.common import format_datetime, json_value, paginate
from core.models import ApsArchive, TaskImportRecord, UploadFile, UploadFileItem
from services.excel_service import ExcelValidationError, parse_plan_file


def task_data(task):
    return {
        "taskId": task.taskId, "importId": task.taskId, "sourceType": task.sourceType,
        "sourceTaskId": task.sourceTask_id, "originalFileName": task.file.originalName,
        "fileId": task.file_id,
        "apsArchive": {"archiveId": task.apsArchive_id, "archiveName": task.apsArchive.archiveName},
        "remark": task.remark, "dataCount": task.file.items.filter(isDeleted=0).count(),
        "importStatus": task.importStatus, "createdBy": task.createdBy_id,
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


class TaskTemplateView(APIView):
    def get(self, request):
        path = Path(settings.MEDIA_ROOT) / "templates" / "药业车间分解编排计划表模板.xlsx"
        return FileResponse(open(path, "rb"), as_attachment=True, filename=path.name)


class TaskHistoryView(APIView):
    def get(self, request):
        params = get_request_params(request)
        queryset = TaskImportRecord.objects.filter(createdBy=request.user, isDeleted=0).select_related("file", "apsArchive").order_by("-createTime")
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
        source = TaskImportRecord.objects.filter(taskId=serializer.validated_data["taskId"], createdBy=request.user, isDeleted=0, importStatus=1).select_related("file", "apsArchive").first()
        if not source:
            raise NotFound("历史导入记录不存在")
        if source.apsArchive.isDeleted:
            raise ValidationError("该历史记录关联的APS方案已不存在，无法导入")
        task = TaskImportRecord.objects.create(sourceType=2, sourceTask=source, apsArchive=source.apsArchive, file=source.file, remark=source.remark, importStatus=1, createdBy=request.user)
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
        queryset = UploadFileItem.objects.filter(file=task.file, isDeleted=0).order_by("itemId")
        keyword = (params.get("keyword") or "").strip()
        if keyword:
            queryset = queryset.filter(Q(materialCode__icontains=keyword) | Q(inventoryName__icontains=keyword) | Q(specification__icontains=keyword))

        filters = {
            "departmentName__in": params.get("departmentNames") or [],
            "monthlyProductionPlan__in": params.get("monthlyProductionPlans") or [],
            "inventoryName__in": params.get("inventoryNames") or [],
        }
        for field_lookup, values in filters.items():
            if not isinstance(values, (list, tuple)):
                values = [values]
            if values:
                try:
                    queryset = queryset.filter(**{field_lookup: values})
                except (TypeError, ValueError) as exc:
                    raise ValidationError("筛选条件格式不正确") from exc

        total, records, page, page_size = paginate(queryset, params.get("page"), params.get("pageSize"))
        return ApiResponse({"total": total, "page": page, "pageSize": page_size, "records": [plan_item_data(x) for x in records]}, message="查询成功")


class TaskDetailFilterOptionsView(APIView):
    OPTION_FIELDS = {
        "departmentNames": "departmentName",
        "monthlyProductionPlans": "monthlyProductionPlan",
        "inventoryNames": "inventoryName",
    }

    def get(self, request):
        params = get_request_params(request)
        task_id = params.get("taskId")
        option = params.get("option")
        if not task_id:
            raise ValidationError("taskId不能为空")
        if option not in self.OPTION_FIELDS:
            raise ValidationError(
                "option必须为departmentNames、monthlyProductionPlans或inventoryNames"
            )

        task = TaskImportRecord.objects.filter(
            taskId=task_id, createdBy=request.user, isDeleted=0
        ).select_related("file").first()
        if not task:
            raise NotFound("任务记录不存在")

        field_name = self.OPTION_FIELDS[option]
        queryset = UploadFileItem.objects.filter(file=task.file, isDeleted=0)
        queryset = queryset.exclude(**{f"{field_name}__isnull": True})
        if field_name in {"departmentName", "inventoryName"}:
            queryset = queryset.exclude(**{field_name: ""})
        values = queryset.order_by(field_name).values_list(field_name, flat=True).distinct()
        return ApiResponse([json_value(value) for value in values], message="查询成功")
