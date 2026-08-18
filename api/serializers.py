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


class ApsArchiveItemCreateSerializer(serializers.Serializer):
    archiveId = serializers.IntegerField(min_value=1)
    productName = serializers.CharField(max_length=100)
    packageSpecification = serializers.CharField(max_length=255)
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

    def validate(self, attrs):
        nullable_text_fields = (
            "mixingLine", "tabletPress", "coatingMachine", "dividingEquipment", "packagingEquipment",
        )
        for field in nullable_text_fields:
            if attrs.get(field) == "":
                attrs[field] = None
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
    page = serializers.IntegerField(min_value=1, required=False, default=1)
    pageSize = serializers.IntegerField(min_value=1, max_value=200, required=False, default=10)

    def validate(self, attrs):
        if not (attrs.get("taskId") or attrs.get("importId")):
            raise serializers.ValidationError("taskId不能为空")
        return attrs
