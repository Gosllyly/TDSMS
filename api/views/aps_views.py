from pathlib import Path
import re

from django.conf import settings
from django.db import transaction
from django.db.models import CharField, Q
from django.db.models.functions import Cast
from rest_framework import status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.views import APIView

from api.serializers import (
    ApsArchiveItemBatchDeleteSerializer,
    ApsArchiveItemCreateSerializer,
    ApsArchiveItemUpdateSerializer,
    ApsUpdateNameSerializer,
    first_serializer_error,
)
from api.utils import ApiResponse, attachment_file_response, get_request_params
from api.views.common import format_datetime, json_value
from core.models import ApsArchive, ApsArchiveItem
from services.aps_export_service import export_aps_archive
from services.excel_service import (
    APS_TEMPLATE_FILENAME,
    ExcelValidationError,
    parse_aps_file,
    resolve_media_template,
)


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
        path = resolve_media_template(APS_TEMPLATE_FILENAME, ascii_prefix="APS")
        return attachment_file_response(open(path, "rb"), APS_TEMPLATE_FILENAME)


class ApsListView(APIView):
    def get(self, request):
        queryset = ApsArchive.objects.filter(createdBy=request.user, isDeleted=0).order_by("-updateTime")
        return ApiResponse([{
            "archiveId": item.archiveId,
            "archiveName": item.archiveName,
            "remark": item.remark,
            "createTime": format_datetime(item.createTime),
            "updateTime": format_datetime(item.updateTime),
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
            filters = (
                Q(productName__icontains=keyword)
                | Q(packageSpecification__icontains=keyword)
                | Q(productionCycleDaysText__icontains=keyword)
                | Q(annualSalesText__icontains=keyword)
                | Q(tabletPress__icontains=keyword)
                | Q(coatingMachine__icontains=keyword)
                | Q(dividingEquipment__icontains=keyword)
                | Q(packagingEquipment__icontains=keyword)
            )
            # 是否集采：支持 是/否、1/0
            centralized_keyword_map = {"是": 1, "1": 1, "否": 0, "0": 0}
            if keyword in centralized_keyword_map:
                filters |= Q(centralizedProcurement=centralized_keyword_map[keyword])
            queryset = queryset.annotate(
                productionCycleDaysText=Cast("productionCycleDays", CharField()),
                annualSalesText=Cast("annualSales", CharField()),
            ).filter(filters)
        return ApiResponse({"total": queryset.count(), "records": [item_data(x) for x in queryset]}, message="查询成功")


class ApsUpdateNameView(APIView):
    def post(self, request):
        serializer = ApsUpdateNameSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        archive = ApsArchive.objects.filter(
            archiveId=data["archiveId"], createdBy=request.user, isDeleted=0,
        ).first()
        if not archive:
            raise NotFound("APS方案不存在")
        name = data["archiveName"]
        if ApsArchive.objects.filter(
            createdBy=request.user, archiveName=name, isDeleted=0,
        ).exclude(archiveId=archive.archiveId).exists():
            raise ValidationError("方案名称已存在")
        archive.archiveName = name
        update_fields = ["archiveName", "updateTime"]
        if "remark" in data:
            archive.remark = data["remark"]
            update_fields.append("remark")
        archive.save(update_fields=update_fields)
        return ApiResponse({
            "archiveId": archive.archiveId,
            "archiveName": archive.archiveName,
            "remark": archive.remark,
            "updateTime": format_datetime(archive.updateTime),
        }, message="方案名称修改成功")


class ApsCreateView(APIView):
    def post(self, request):
        name = (request.data.get("archiveName") or "").strip()
        uploaded = request.FILES.get("file")
        archive_id = request.data.get("archiveId")
        has_remark = "remark" in request.data
        remark = None
        if has_remark:
            raw_remark = request.data.get("remark")
            if raw_remark in ("", None):
                remark = None
            else:
                remark = str(raw_remark).strip() or None
                if remark and len(remark) > 500:
                    raise ValidationError("备注长度不能超过500")
        if archive_id in ("", None, []):
            archive_id = None
        else:
            try:
                archive_id = int(archive_id)
            except (TypeError, ValueError) as exc:
                raise ValidationError("archiveId格式不正确") from exc
            if archive_id < 1:
                raise ValidationError("archiveId格式不正确")

        if not name or not uploaded:
            raise ValidationError("方案名称和Excel文件不能为空")

        name_queryset = ApsArchive.objects.filter(
            createdBy=request.user, archiveName=name, isDeleted=0,
        )
        if archive_id is not None:
            name_queryset = name_queryset.exclude(archiveId=archive_id)
        if name_queryset.exists():
            raise ValidationError("方案名称已存在")

        try:
            rows = parse_aps_file(uploaded)
        except ExcelValidationError as exc:
            raise ValidationError(str(exc)) from exc

        with transaction.atomic():
            if archive_id is not None:
                archive = ApsArchive.objects.filter(
                    archiveId=archive_id, createdBy=request.user, isDeleted=0,
                ).first()
                if not archive:
                    raise NotFound("APS方案不存在")
                # 逻辑删除旧明细，再用新文档数据替换
                ApsArchiveItem.objects.filter(archive=archive).update(isDeleted=1)
                archive.archiveName = name
                update_fields = ["archiveName", "updateTime"]
                if has_remark:
                    archive.remark = remark
                    update_fields.append("remark")
                archive.save(update_fields=update_fields)
                message = "APS方案替换成功"
            else:
                create_kwargs = {"archiveName": name, "createdBy": request.user}
                if has_remark:
                    create_kwargs["remark"] = remark
                archive = ApsArchive.objects.create(**create_kwargs)
                message = "APS方案导入成功"
            ApsArchiveItem.objects.bulk_create(
                [ApsArchiveItem(archive=archive, **row) for row in rows],
                batch_size=500,
            )
        return ApiResponse({
            "archiveId": archive.archiveId,
            "archiveName": archive.archiveName,
            "remark": archive.remark,
            "dataCount": len(rows),
            "createTime": format_datetime(archive.createTime),
        }, message=message)


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
        if not serializer.is_valid():
            return ApiResponse(
                {},
                status=status.HTTP_400_BAD_REQUEST,
                message=first_serializer_error(serializer.errors, "APS明细新增失败"),
            )
        data = dict(serializer.validated_data)
        archive_id = data.pop("archiveId")
        archive = ApsArchive.objects.filter(
            archiveId=archive_id, createdBy=request.user, isDeleted=0
        ).first()
        if not archive:
            return ApiResponse({}, status=status.HTTP_400_BAD_REQUEST, message="APS方案不存在")
        try:
            with transaction.atomic():
                ApsArchiveItem.objects.create(archive=archive, **data)
                archive.save(update_fields=["updateTime"])
        except Exception:
            return ApiResponse({}, status=status.HTTP_400_BAD_REQUEST, message="APS明细新增失败")
        return ApiResponse({}, message="APS明细新增成功")


class ApsItemUpdateView(APIView):
    def post(self, request):
        serializer = ApsArchiveItemUpdateSerializer(data=request.data)
        if not serializer.is_valid():
            return ApiResponse(
                {},
                status=status.HTTP_400_BAD_REQUEST,
                message=first_serializer_error(serializer.errors, "APS明细修改失败"),
            )
        data = dict(serializer.validated_data)
        archive_id = data.pop("archiveId")
        item_id = data.pop("itemId")
        archive = ApsArchive.objects.filter(
            archiveId=archive_id, createdBy=request.user, isDeleted=0
        ).first()
        item = ApsArchiveItem.objects.filter(
            archive=archive, itemId=item_id, isDeleted=0
        ).first() if archive else None
        if not archive or not item:
            return ApiResponse({}, status=status.HTTP_400_BAD_REQUEST, message="APS明细不存在")
        try:
            with transaction.atomic():
                for field, value in data.items():
                    setattr(item, field, value)
                item.save()
                archive.save(update_fields=["updateTime"])
        except Exception:
            return ApiResponse({}, status=status.HTTP_400_BAD_REQUEST, message="APS明细修改失败")
        return ApiResponse({}, message="APS明细修改成功")


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
