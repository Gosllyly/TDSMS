"""求解前计划数据与 APS 档案的品种、规格匹配规则。"""

from core.mapping import name_mapping, spec_mapping

REASON_MISSING_SPEC = "APS中未找到对应的包装规格"
REASON_MISSING_PRODUCT = "APS中未找到对应的品种"
REASON_MISSING_BOTH = "APS中未找到对应的品种+包装规格"


def _clean(value):
    return str(value or "").strip()


def _candidates(value, mapping):
    cleaned = _clean(value)
    mapped = _clean(mapping.get(cleaned, ""))
    if mapped and mapped != cleaned:
        return (cleaned, mapped)
    return (cleaned,)


def _unmatched_reason(name_candidates, spec_candidates, aps_names, aps_specs):
    name_found = any(name in aps_names for name in name_candidates)
    spec_found = any(spec in aps_specs for spec in spec_candidates)
    if name_found and spec_found:
        return REASON_MISSING_BOTH
    if name_found:
        return REASON_MISSING_SPEC
    if spec_found:
        return REASON_MISSING_PRODUCT
    return REASON_MISSING_BOTH


def compare_task_plan_with_aps(task, department=None, require_positive_plan=True):
    plan_items = task.file.items.filter(isDeleted=0)
    if require_positive_plan:
        plan_items = plan_items.filter(monthlyProductionPlan__gt=0)
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
    aps_names = {product_name for product_name, _ in aps_pairs}
    aps_specs = {package_specification for _, package_specification in aps_pairs}

    matched_records = []
    unmatched_records = []
    department_statistics = {}
    for item in plan_items:
        department_name = _clean(item["departmentName"])
        inventory_name = _clean(item["inventoryName"])
        specification = _clean(item["specification"])
        name_candidates = _candidates(inventory_name, name_mapping)
        spec_candidates = _candidates(specification, spec_mapping)
        aps_product_name = name_candidates[-1]
        aps_package_specification = spec_candidates[-1]
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
        if any((name, spec) in aps_pairs for name in name_candidates for spec in spec_candidates):
            matched_records.append(record)
            statistics["matchedCount"] += 1
        else:
            record["reason"] = _unmatched_reason(
                name_candidates, spec_candidates, aps_names, aps_specs,
            )
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


def match_check_data(task, page=1, page_size=10, department=None):
    result = compare_task_plan_with_aps(
        task, department=department, require_positive_plan=False,
    )
    missing_data = []
    seen = set()
    for record in result["unmatchedRecords"]:
        key = (record["inventoryName"], record["specification"])
        if key in seen:
            continue
        seen.add(key)
        missing_data.append({
            "inventoryName": record["inventoryName"],
            "specification": record["specification"],
            "reason": record["reason"],
        })
    start = (page - 1) * page_size
    return {
        "status": not missing_data,
        "total": len(missing_data),
        "page": page,
        "pageSize": page_size,
        "missingData": missing_data[start:start + page_size],
    }
