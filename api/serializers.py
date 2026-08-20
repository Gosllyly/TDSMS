from rest_framework import serializers


class LoginSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=50)
    password = serializers.CharField()


class AdminCreateSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=50)
    password = serializers.CharField(min_length=6)
    validDays = serializers.IntegerField(min_value=1)
    realName = serializers.CharField(max_length=50, required=False, allow_blank=True, allow_null=True)
    departmentName = serializers.CharField(max_length=100, required=False, allow_blank=True, allow_null=True)


class AdminExpireSerializer(serializers.Serializer):
    userId = serializers.IntegerField(min_value=1)
    validDays = serializers.IntegerField(min_value=1)


class AdminStatusSerializer(serializers.Serializer):
    userId = serializers.IntegerField(min_value=1)
    status = serializers.ChoiceField(choices=[0, 1])


class HistoryImportSerializer(serializers.Serializer):
    taskId = serializers.IntegerField(min_value=1)


class ApsUpdateNameSerializer(serializers.Serializer):
    archiveId = serializers.IntegerField(min_value=1)
    archiveName = serializers.CharField(max_length=100)
    remark = serializers.CharField(max_length=500, required=False, allow_blank=True, allow_null=True)

    def validate_archiveName(self, value):
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("archiveName不能为空")
        return value

    def validate_remark(self, value):
        if value in ("", None):
            return None
        return value.strip() or None


def first_serializer_error(errors, fallback="参数校验失败"):
    if isinstance(errors, dict):
        for value in errors.values():
            message = first_serializer_error(value, "")
            if message:
                return message
        return fallback
    if isinstance(errors, list):
        for value in errors:
            message = first_serializer_error(value, "")
            if message:
                return message
        return fallback
    if errors in (None, ""):
        return fallback
    return str(errors)


class ApsArchiveItemCreateSerializer(serializers.Serializer):
    archiveId = serializers.IntegerField(min_value=1)
    productName = serializers.CharField(
        max_length=100,
        error_messages={
            "required": "productName不能为空",
            "blank": "productName不能为空",
            "null": "productName不能为空",
        },
    )
    packageSpecification = serializers.CharField(
        max_length=255, required=False, allow_blank=True, allow_null=True,
    )
    mixingLine = serializers.CharField(max_length=100, required=False, allow_blank=True, allow_null=True)
    mixingBatchQuantity = serializers.DecimalField(max_digits=14, decimal_places=4, min_value=0, required=False, allow_null=True)
    mixingShiftOutput = serializers.DecimalField(max_digits=14, decimal_places=4, min_value=0, required=False, allow_null=True)
    mixingWorkerCount = serializers.IntegerField(min_value=0, required=False, allow_null=True)
    tabletPress = serializers.CharField(max_length=100, required=False, allow_blank=True, allow_null=True)
    tabletingShiftOutput = serializers.DecimalField(max_digits=14, decimal_places=4, min_value=0, required=False, allow_null=True)
    tabletingWorkerCount = serializers.IntegerField(min_value=0, required=False, allow_null=True)
    coatingMachine = serializers.CharField(max_length=100, required=False, allow_blank=True, allow_null=True)
    coatingShiftOutput = serializers.DecimalField(max_digits=14, decimal_places=4, min_value=0, required=False, allow_null=True)
    coatingWorkerCount = serializers.IntegerField(min_value=0, required=False, allow_null=True)
    dividingEquipment = serializers.CharField(max_length=100, required=False, allow_blank=True, allow_null=True)
    dividingShiftOutput = serializers.DecimalField(max_digits=14, decimal_places=4, min_value=0, required=False, allow_null=True)
    dividingWorkerCount = serializers.IntegerField(min_value=0, required=False, allow_null=True)
    packagingEquipment = serializers.CharField(max_length=100, required=False, allow_blank=True, allow_null=True)
    packagingShiftOutput = serializers.DecimalField(max_digits=14, decimal_places=4, min_value=0, required=False, allow_null=True)
    manualPackagingOutput = serializers.DecimalField(max_digits=14, decimal_places=4, min_value=0, required=False, allow_null=True)
    packagingWorkerCount = serializers.IntegerField(min_value=0, required=False, allow_null=True)
    productionCycleDays = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=0, required=False, allow_null=True)
    centralizedProcurement = serializers.ChoiceField(choices=[0, 1], required=False, allow_null=True)
    annualSales = serializers.DecimalField(max_digits=18, decimal_places=4, min_value=0, required=False, allow_null=True)

    _empty_as_null_fields = (
        "mixingLine", "tabletPress", "coatingMachine", "dividingEquipment", "packagingEquipment",
        "mixingBatchQuantity", "mixingShiftOutput", "mixingWorkerCount",
        "tabletingShiftOutput", "tabletingWorkerCount",
        "coatingShiftOutput", "coatingWorkerCount",
        "dividingShiftOutput", "dividingWorkerCount",
        "packagingShiftOutput", "manualPackagingOutput", "packagingWorkerCount",
        "productionCycleDays", "centralizedProcurement", "annualSales",
        "packageSpecification",
    )

    def to_internal_value(self, data):
        data = data.copy() if hasattr(data, "copy") else dict(data)
        for field in self._empty_as_null_fields:
            if field in data and data[field] in ("", []):
                data[field] = None
        return super().to_internal_value(data)

    def validate_productName(self, value):
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("productName不能为空")
        return value

    def validate(self, attrs):
        for field in (
            "mixingLine", "tabletPress", "coatingMachine", "dividingEquipment", "packagingEquipment",
        ):
            if attrs.get(field) == "":
                attrs[field] = None
        # 库表 packageSpecification 不允许 NULL，空值落库为空串
        if "packageSpecification" in attrs or self.__class__ is ApsArchiveItemCreateSerializer:
            attrs["packageSpecification"] = attrs.get("packageSpecification") or ""
        return attrs


class ApsArchiveItemUpdateSerializer(ApsArchiveItemCreateSerializer):
    itemId = serializers.IntegerField(min_value=1, required=False)
    itemIId = serializers.IntegerField(min_value=1, required=False)

    def to_internal_value(self, data):
        data = data.copy() if hasattr(data, "copy") else dict(data)
        if not data.get("itemId") and data.get("itemIId"):
            data["itemId"] = data.get("itemIId")
        if data.get("itemId") in ("", []):
            data["itemId"] = None
        return super().to_internal_value(data)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        item_id = attrs.get("itemId") or attrs.pop("itemIId", None)
        if not item_id:
            raise serializers.ValidationError({"itemId": "itemId不能为空"})
        attrs["itemId"] = item_id
        attrs.pop("itemIId", None)
        return attrs


class ApsArchiveItemBatchDeleteSerializer(serializers.Serializer):
    archiveId = serializers.IntegerField(min_value=1)
    batchMode = serializers.BooleanField()
    itemId = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    productNames = serializers.ListField(
        child=serializers.CharField(max_length=100),
        required=False,
        allow_empty=True,
        allow_null=True,
    )

    def to_internal_value(self, data):
        data = data.copy() if hasattr(data, "copy") else dict(data)
        if data.get("itemId") in ("", []):
            data["itemId"] = None
        if data.get("productNames") in ("", None):
            data["productNames"] = []
        return super().to_internal_value(data)

    def validate_productNames(self, values):
        if not values:
            return []
        return list(dict.fromkeys(values))

    def validate(self, attrs):
        if attrs["batchMode"]:
            if not attrs.get("productNames"):
                raise serializers.ValidationError({"productNames": "批量删除时productNames不能为空"})
        elif not attrs.get("itemId"):
            raise serializers.ValidationError({"itemId": "单个删除时itemId不能为空"})
        return attrs


class SolveStartSerializer(serializers.Serializer):
    taskId = serializers.IntegerField(min_value=1, required=False)
    importId = serializers.IntegerField(min_value=1, required=False)
    department = serializers.CharField(max_length=100)
    scheduleMonth = serializers.RegexField(r"^\d{4}-(0[1-9]|1[0-2])$")
    unmatchedItemPolicy = serializers.ChoiceField(
        choices=["BLOCK", "SKIP"],
        default="BLOCK",
    )
    productionRules = serializers.DictField()
    personnelCapacity = serializers.DictField()
    solverTimeLimitMinutes = serializers.IntegerField(min_value=1, max_value=1440)

    def validate(self, attrs):
        if not (attrs.get("taskId") or attrs.get("importId")):
            raise serializers.ValidationError("taskId不能为空")
        rules = attrs["productionRules"]
        required_rules = {"continuousRunLimitDays", "cleaningDuration", "shiftConversion"}
        if not required_rules.issubset(rules):
            raise serializers.ValidationError("productionRules配置不完整")
        capacity = attrs["personnelCapacity"]
        for process in ("mixing", "tableting", "coating", "packaging"):
            value = capacity.get(process)
            if not isinstance(value, int) or value < 0:
                raise serializers.ValidationError(
                    f"personnelCapacity.{process}必须为非负整数"
                )
        return attrs


class SolveMatchCheckSerializer(serializers.Serializer):
    taskId = serializers.IntegerField(min_value=1, required=False)
    importId = serializers.IntegerField(min_value=1, required=False)
    departmentNames = serializers.CharField(max_length=100)
    page = serializers.IntegerField(min_value=1, required=False, default=1)
    pageSize = serializers.IntegerField(min_value=1, max_value=200, required=False, default=10)

    def validate(self, attrs):
        if not (attrs.get("taskId") or attrs.get("importId")):
            raise serializers.ValidationError("taskId不能为空")
        attrs["departmentNames"] = attrs["departmentNames"].strip()
        if not attrs["departmentNames"]:
            raise serializers.ValidationError({"departmentNames": "departmentNames不能为空"})
        return attrs
