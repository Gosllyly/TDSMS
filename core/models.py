from django.db import models


class SysUser(models.Model):
    userId = models.BigAutoField(primary_key=True)
    username = models.CharField(max_length=50, unique=True)
    password = models.CharField(max_length=255)
    role = models.CharField(max_length=20, default="user")
    realName = models.CharField(max_length=50, null=True, blank=True)
    departmentName = models.CharField(max_length=100, null=True, blank=True)
    status = models.SmallIntegerField(default=1)
    expireTime = models.DateTimeField(null=True, blank=True)
    lastLoginTime = models.DateTimeField(null=True, blank=True)
    loginToken = models.CharField(max_length=512, null=True, blank=True)
    createTime = models.DateTimeField(auto_now_add=True)
    updateTime = models.DateTimeField(auto_now=True)
    isDeleted = models.SmallIntegerField(default=0)

    @property
    def is_authenticated(self):
        return True

    class Meta:
        db_table = "sysUser"


class ApsArchive(models.Model):
    archiveId = models.BigAutoField(primary_key=True)
    archiveName = models.CharField(max_length=100)
    createdBy = models.ForeignKey(SysUser, db_column="createdBy", on_delete=models.PROTECT, related_name="apsArchives")
    createTime = models.DateTimeField(auto_now_add=True)
    updateTime = models.DateTimeField(auto_now=True)
    isDeleted = models.SmallIntegerField(default=0)

    class Meta:
        db_table = "apsArchive"
        indexes = [models.Index(fields=["createdBy", "isDeleted"])]


class ApsArchiveItem(models.Model):
    itemId = models.BigAutoField(primary_key=True)
    archive = models.ForeignKey(ApsArchive, db_column="archiveId", on_delete=models.CASCADE, related_name="items")
    productName = models.CharField(max_length=100)
    packageSpecification = models.CharField(max_length=255)
    mixingLine = models.CharField(max_length=100, null=True, blank=True)
    mixingBatchQuantity = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    mixingShiftOutput = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    mixingWorkerCount = models.IntegerField(null=True, blank=True)
    tabletPress = models.CharField(max_length=100, null=True, blank=True)
    tabletingShiftOutput = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    tabletingWorkerCount = models.IntegerField(null=True, blank=True)
    coatingMachine = models.CharField(max_length=100, null=True, blank=True)
    coatingShiftOutput = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    coatingWorkerCount = models.IntegerField(null=True, blank=True)
    dividingEquipment = models.CharField(max_length=100, null=True, blank=True)
    dividingShiftOutput = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    dividingWorkerCount = models.IntegerField(null=True, blank=True)
    packagingEquipment = models.CharField(max_length=100, null=True, blank=True)
    packagingShiftOutput = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    manualPackagingOutput = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    packagingWorkerCount = models.IntegerField(null=True, blank=True)
    productionCycleDays = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    centralizedProcurement = models.SmallIntegerField(null=True, blank=True)
    annualSales = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    createTime = models.DateTimeField(auto_now_add=True)
    isDeleted = models.SmallIntegerField(default=0)

    class Meta:
        db_table = "apsArchiveItem"
        indexes = [models.Index(fields=["archive", "isDeleted"])]


class UploadFile(models.Model):
    fileId = models.BigAutoField(primary_key=True)
    originalName = models.CharField(max_length=255)
    fileName = models.CharField(max_length=255)
    filePath = models.CharField(max_length=255)
    uploadUser = models.ForeignKey(SysUser, db_column="uploadUserId", on_delete=models.PROTECT, related_name="uploadFiles")
    uploadTime = models.DateTimeField(auto_now_add=True)
    parseStatus = models.SmallIntegerField(default=0)
    parseMessage = models.CharField(max_length=1000, null=True, blank=True)
    isDeleted = models.SmallIntegerField(default=0)

    class Meta:
        db_table = "uploadFile"


class UploadFileItem(models.Model):
    itemId = models.BigAutoField(primary_key=True)
    file = models.ForeignKey(UploadFile, db_column="fileId", on_delete=models.CASCADE, related_name="items")
    departmentName = models.CharField(max_length=100)
    materialCode = models.CharField(max_length=50)
    inventoryName = models.CharField(max_length=255)
    specification = models.CharField(max_length=255)
    u8CurrentStock = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    monthlyProductionPlan = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    submittedTotal = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    createTime = models.DateTimeField(auto_now_add=True)
    isDeleted = models.SmallIntegerField(default=0)

    class Meta:
        db_table = "uploadFileItem"
        indexes = [models.Index(fields=["file", "materialCode"])]


class TaskImportRecord(models.Model):
    taskId = models.BigAutoField(primary_key=True)
    sourceType = models.SmallIntegerField(default=1)
    sourceTask = models.ForeignKey("self", db_column="sourceTaskId", null=True, blank=True, on_delete=models.SET_NULL)
    apsArchive = models.ForeignKey(ApsArchive, db_column="apsArchiveId", on_delete=models.PROTECT, related_name="tasks")
    file = models.ForeignKey(UploadFile, db_column="fileId", on_delete=models.PROTECT, related_name="tasks")
    remark = models.CharField(max_length=500, null=True, blank=True)
    importStatus = models.SmallIntegerField(default=0)
    createdBy = models.ForeignKey(SysUser, db_column="createdBy", on_delete=models.PROTECT, related_name="tasks")
    createTime = models.DateTimeField(auto_now_add=True)
    updateTime = models.DateTimeField(auto_now=True)
    isDeleted = models.SmallIntegerField(default=0)

    class Meta:
        db_table = "taskImportRecord"
        indexes = [models.Index(fields=["createdBy", "isDeleted", "createTime"])]


class SolveTask(models.Model):
    solveTaskId = models.BigAutoField(primary_key=True)
    task = models.ForeignKey(TaskImportRecord, db_column="taskId", on_delete=models.PROTECT, related_name="solveTasks")
    inputParams = models.JSONField()
    solveStatus = models.SmallIntegerField(default=0)
    finishReason = models.SmallIntegerField(null=True, blank=True)
    startTime = models.DateTimeField(null=True, blank=True)
    finishTime = models.DateTimeField(null=True, blank=True)
    resultFilePath = models.CharField(max_length=255, null=True, blank=True)
    partialResultFilePath = models.CharField(max_length=255, null=True, blank=True)
    createdUser = models.ForeignKey(SysUser, db_column="createdUserId", on_delete=models.PROTECT, related_name="solveTasks")
    createTime = models.DateTimeField(auto_now_add=True)
    isDeleted = models.SmallIntegerField(default=0)

    class Meta:
        db_table = "solveTask"
        indexes = [models.Index(fields=["createdUser", "solveStatus"])]
