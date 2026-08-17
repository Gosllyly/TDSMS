"""求解前计划数据与 APS 档案的品种、规格匹配规则。"""


NAME_MAPPING = {
    "阿司匹林肠溶片（过评）": "阿司匹林肠溶片100mg",
    "左氧氟沙星片(片剂)": "左氧氟沙星片",
    "琥珀酸美托洛尔缓释胶囊(硬胶囊)": "琥珀酸美托洛尔胶囊",
    "缬沙坦氨氯地平片(片剂)": "缬沙坦氨氯地平片",
    "盐酸伊托必利片(片剂)": "盐酸伊托必利片",
    "缬沙坦氢氯噻嗪片（过评）": "缬沙坦氢氯噻嗪片",
    "瑞巴派特片(片剂)": "瑞巴派特片",
    "艾司奥美拉唑镁肠溶胶囊(硬胶囊)": "艾美",
    "盐酸二甲双胍缓释片（过评）": "盐酸二甲双胍缓释片",
    "非洛地平缓释片": "非诺地平缓释片",
}

SPEC_MAPPING = {
    "47.5mg×14粒×2板×400盒": "47.5mg×14片×2板×400盒",
    "10mg×100片×600瓶/10瓶*60包": "100片/瓶*600瓶/箱",
    "80mg×7片×4板×400盒": "7片×4板×400盒",
    "50mg×10片×2板×400盒": "10片×2板×400盒",
    "80mg×14片×2板×400盒": "（80mg:12.5mg）14片×2板×400盒",
    "0.5gx30片x400瓶": "30片/瓶×400盒/箱",
    "40mg*7粒/板*4板/盒*200盒": "40mg×7粒×4板×200盒",
    "20mg*7粒/板*4板/盒*200盒": "20mg×7粒×4板×200盒",
    "0.2g×10片×1板×400盒": "10粒×1板×400盒",
    "0.2g×10片×2板×400盒": "10粒×2板×400盒",
    "0.5mg×12粒×2板×400盒": "0.5mg×12片×2板×400盒",
    "25mg（按C21H29N6O5P计）×15片×4板×400盒": "15片×4板×400盒",
    "23.75mg×14粒×2板×400盒": "23.75mg×14片×2板×400盒",
    "5mg*10片/板*4板*400盒": "10片×4板×400盒",
}


def _clean(value):
    return str(value or "").strip()


def compare_task_plan_with_aps(task, department=None):
    plan_items = task.file.items.filter(
        isDeleted=0,
        monthlyProductionPlan__gt=0,
    )
    if department is not None:
        plan_items = plan_items.filter(departmentName=department)
    plan_items = plan_items.values(
        "departmentName", "inventoryName", "specification",
    ).distinct().order_by(
        "departmentName", "inventoryName", "specification",
    )
    aps_pairs = {
        (_clean(product_name), _clean(package_specification))
        for product_name, package_specification in task.apsArchive.items.filter(
            isDeleted=0,
        ).values_list("productName", "packageSpecification")
    }

    matched_records = []
    unmatched_records = []
    department_statistics = {}
    for item in plan_items:
        department_name = _clean(item["departmentName"])
        inventory_name = _clean(item["inventoryName"])
        specification = _clean(item["specification"])
        aps_product_name = NAME_MAPPING.get(inventory_name, inventory_name)
        aps_package_specification = SPEC_MAPPING.get(specification, specification)
        record = {
            "departmentName": department_name,
            "inventoryName": inventory_name,
            "specification": specification,
            "apsProductName": aps_product_name,
            "apsPackageSpecification": aps_package_specification,
            "nameMapped": aps_product_name != inventory_name,
            "specificationMapped": aps_package_specification != specification,
        }
        statistics = department_statistics.setdefault(
            department_name,
            {"departmentName": department_name, "totalCount": 0, "matchedCount": 0, "unmatchedCount": 0},
        )
        statistics["totalCount"] += 1
        if (aps_product_name, aps_package_specification) in aps_pairs:
            matched_records.append(record)
            statistics["matchedCount"] += 1
        else:
            unmatched_records.append(record)
            statistics["unmatchedCount"] += 1

    total_count = len(matched_records) + len(unmatched_records)
    can_start_solve = total_count > 0 and not unmatched_records
    result = {
        "taskId": task.taskId,
        "importId": task.taskId,
        "apsArchiveId": task.apsArchive_id,
        "apsArchiveName": task.apsArchive.archiveName,
        "departmentCount": len(department_statistics),
        "departmentStatistics": list(department_statistics.values()),
        "totalCount": total_count,
        "matchedCount": len(matched_records),
        "unmatchedCount": len(unmatched_records),
        "canStartSolve": can_start_solve,
        "matchedRecords": matched_records,
        "unmatchedRecords": unmatched_records,
    }
    if department is not None:
        result["department"] = department
    return result
