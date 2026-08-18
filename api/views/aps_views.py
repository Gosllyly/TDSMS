from pathlib import Path
import re

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.views import APIView

from api.serializers import ApsArchiveItemBatchDeleteSerializer, ApsArchiveItemCreateSerializer
from api.utils import ApiResponse, attachment_file_response, get_request_params
from api.views.common import format_datetime, json_value
from core.models import ApsArchive, ApsArchiveItem
from services.aps_export_service import export_aps_archive
from services.excel_service import ExcelValidationError, parse_aps_file


def item_data(item):
    fields = [
        "itemId", "productName", "packageSpecification", "mixingLine", "mixingBatchQuantity",
        "mixingShiftOutput", "mixingWorkerCount", "tabletPress", "tabletingShiftOutput",
        "tabletingWorkerCount", "coatingMachine", "coatingShiftOutput", "coatingWorkerCount",
        "dividingEquipment", "dividingShiftOutput", "dividingWorkerCount", "packagingEquipment",
        "packagingShiftOutput", "manualPackagingOutput", "packagingWorkerCount", "productionCycleDays",
        "centralizedProcurement", "annualSales", "createTime",
    ]
    data = {field: json_value(getattr(item, field)) for field in fields}
    data["archiveId"] = item.archive_id
    return data


class ApsTemplateView(APIView):
    def get(self, request):
        path = Path(settings.MEDIA_ROOT) / "templates" / "APS排产信息模板.xlsx"
        return attachment_file_response(open(path, "rb"), path.name)


class ApsListView(APIView):
    def get(self, request):
        queryset = ApsArchive.objects.filter(createdBy=request.user, isDeleted=0).order_by("-updateTime")
        return ApiResponse([{
            "archiveId": item.archiveId, "archiveName": item.archiveName,
            "createTime": format_datetime(item.createTime), "updateTime": format_datetime(item.updateTime),
        } for item in queryset], message="查询成功")


class ApsInfoView(APIView):
    def get(self, request):
        params = get_request_params(request)
        archive_id = params.get("archiveId")
        if not archive_id:
            raise ValidationError("archiveId不能为空")
        archive = ApsArchive.objects.filter(archiveId=archive_id, createdBy=request.user, isDeleted=0).first()
        if not archive:
            raise NotFound("APS方案不存在")
        queryset = ApsArchiveItem.objects.filter(archive=archive, isDeleted=0).order_by("itemId")
        keyword = (params.get("keyword") or "").strip()
        if keyword:
            queryset = queryset.filter(Q(productName__icontains=keyword) | Q(packageSpecification__icontains=keyword))
        return ApiResponse({"total": queryset.count(), "records": [item_data(x) for x in queryset]}, message="查询成功")


class ApsCreateView(APIView):
    def post(self, request):
        name = (request.data.get("archiveName") or "").strip()
        uploaded = request.FILES.get("file")
        if not name or not uploaded:
            raise ValidationError("方案名称和Excel文件不能为空")
        if ApsArchive.objects.filter(createdBy=request.user, archiveName=name, isDeleted=0).exists():
            raise ValidationError("方案名称已存在")
        try:
            rows = parse_aps_file(uploaded)
        except ExcelValidationError as exc:
            raise ValidationError(str(exc)) from exc
        with transaction.atomic():
            archive = ApsArchive.objects.create(archiveName=name, createdBy=request.user)
            ApsArchiveItem.objects.bulk_create([ApsArchiveItem(archive=archive, **row) for row in rows], batch_size=500)
        return ApiResponse({"archiveId": archive.archiveId, "archiveName": archive.archiveName, "dataCount": len(rows), "createTime": format_datetime(archive.createTime)}, message="APS方案导入成功")


class ApsDeleteView(APIView):
    def post(self, request):
        archive_id = get_request_params(request).get("archiveId")
        archive = ApsArchive.objects.filter(archiveId=archive_id, createdBy=request.user, isDeleted=0).first()
        if not archive:
            raise NotFound("APS方案不存在")
        archive.isDeleted = 1
        archive.save(update_fields=["isDeleted", "updateTime"])
        ApsArchiveItem.objects.filter(archive=archive).update(isDeleted=1)
        return ApiResponse(message="删除成功")


class ApsItemCreateView(APIView):
    def post(self, request):
        serializer = ApsArchiveItemCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        archive_id = data.pop("archiveId")
        archive = ApsArchive.objects.filter(
            archiveId=archive_id, createdBy=request.user, isDeleted=0
        ).first()
        if not archive:
            raise NotFound("APS方案不存在")

        with transaction.atomic():
            item = ApsArchiveItem.objects.create(archive=archive, **data)
            archive.save(update_fields=["updateTime"])
        return ApiResponse(item_data(item), message="APS明细新增成功")


class ApsItemDeleteView(APIView):
    def post(self, request):
        serializer = ApsArchiveItemBatchDeleteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        archive = ApsArchive.objects.filter(
            archiveId=data["archiveId"], createdBy=request.user, isDeleted=0
        ).first()
        if not archive:
            raise NotFound("APS方案不存在")

        if data["batchMode"]:
            requested_names = data["productNames"]
            queryset = ApsArchiveItem.objects.filter(
                archive=archive, productName__in=requested_names, isDeleted=0
            )
            matched_names = set(queryset.values_list("productName", flat=True).distinct())
            deleted_names = [name for name in requested_names if name in matched_names]
            with transaction.atomic():
                deleted_count = queryset.update(isDeleted=1)
                if deleted_count:
                    archive.save(update_fields=["updateTime"])
            return ApiResponse({
                "batchMode": True,
                "deletedProductCount": len(deleted_names),
                "deletedItemCount": deleted_count,
                "productNames": deleted_names,
            }, message="批量删除成功")

        item = ApsArchiveItem.objects.filter(
            archive=archive, itemId=data["itemId"], isDeleted=0
        ).first()
        if not item:
            raise NotFound("APS明细不存在")
        with transaction.atomic():
            item.isDeleted = 1
            item.save(update_fields=["isDeleted"])
            archive.save(update_fields=["updateTime"])
        return ApiResponse({
            "batchMode": False,
            "archiveId": archive.archiveId,
            "itemId": item.itemId,
        }, message="删除成功")


class ApsExportView(APIView):
    def get(self, request):
        archive_id = get_request_params(request).get("archiveId")
        if not archive_id:
            raise ValidationError("archiveId不能为空")
        archive = ApsArchive.objects.filter(
            archiveId=archive_id, createdBy=request.user, isDeleted=0
        ).first()
        if not archive:
            raise NotFound("APS方案不存在")
        items = ApsArchiveItem.objects.filter(archive=archive, isDeleted=0).order_by("itemId")
        stream = export_aps_archive(items)
        safe_name = re.sub(r'[\\/:*?"<>|]+', "_", archive.archiveName).strip() or f"APS方案{archive.archiveId}"
        return attachment_file_response(
            stream,
            f"{safe_name}_APS排产信息.xlsx",
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
