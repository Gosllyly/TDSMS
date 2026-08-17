import argparse
from datetime import datetime, timedelta
import sys

import pandas as pd

from input_data import build_schedule_inputs

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


DEFAULT_INPUT = "排产结果明细_packclear.xlsx"
DEFAULT_OUTPUT = "排产结果明细_packclear_sundayrest.xlsx"
DEFAULT_DEMO = "./输入/demo2.xlsx"
DEFAULT_APS = "./输入/副本APS排产信息-4.30.xlsx"
DEFAULT_START_DATE = "2026-07-01"

BATCH_SHEET = "批次工序计划"
PACK_SHEET = "包装及摘要"
CLEAR_SHEET = "设备清场明细"
REPORT_SHEET = "周日休息后处理"


TIME_COLUMNS = {
    BATCH_SHEET: ["配料开工(班时)", "压片开工(班时)", "包衣开工(班时)"],
    PACK_SHEET: ["包装开工(班时)", "包装完工(班时)"],
    CLEAR_SHEET: [
        "前任务开始(班时)",
        "前任务结束(班时)",
        "后任务开始(班时)",
        "清场开始(班时)",
        "清场结束(班时)",
    ],
}


def _is_sunday(day_index, start_date):
    return (start_date + timedelta(days=day_index)).weekday() == 6


def working_to_calendar_shift(value, start_date, shifts_per_day=2):
    if pd.isna(value):
        return value
    remaining = float(value)
    if remaining < 0:
        return remaining

    calendar_time = 0.0
    day_index = 0
    while True:
        if _is_sunday(day_index, start_date):
            calendar_time += shifts_per_day
            day_index += 1
            continue
        if remaining <= 1e-9:
            return calendar_time
        if remaining < shifts_per_day - 1e-9:
            return calendar_time + remaining
        remaining -= shifts_per_day
        calendar_time += shifts_per_day
        day_index += 1


def _map_time_column(df, column, start_date):
    if column not in df.columns:
        return
    values = pd.to_numeric(df[column], errors="coerce")
    mask = values.notna()
    df.loc[mask, column] = values[mask].map(lambda x: round(working_to_calendar_shift(x, start_date), 2))


def _update_actual_gap(clear_df):
    needed = {"后任务开始(班时)", "前任务结束(班时)", "理论清场时长"}
    if not needed.issubset(clear_df.columns):
        return
    clear_df["实际间隔"] = (
        pd.to_numeric(clear_df["后任务开始(班时)"], errors="coerce")
        - pd.to_numeric(clear_df["前任务结束(班时)"], errors="coerce")
    ).round(2)
    clear_df["清场是否满足"] = (
        pd.to_numeric(clear_df["实际间隔"], errors="coerce")
        + 1e-9
        >= pd.to_numeric(clear_df["理论清场时长"], errors="coerce").fillna(0)
    )


def _recompute_cycles(batch_df, pack_df, clear_df, due):
    batch_df["配料开工(班时)"] = pd.to_numeric(batch_df["配料开工(班时)"], errors="coerce")
    batch_df["批次周期上限(班时)"] = pd.to_numeric(batch_df["批次周期上限(班时)"], errors="coerce")

    finish_map = {}
    if {"工序", "前任务药品规格", "前任务批次", "前任务结束(班时)", "后任务药品规格", "后任务批次", "后任务开始(班时)"}.issubset(clear_df.columns):
        pack_clear = clear_df[clear_df["工序"] == "4. 包装"]
        for _, row in pack_clear.iterrows():
            for prefix in ("前", "后"):
                item_col = f"{prefix}任务药品规格"
                batch_col = f"{prefix}任务批次"
                end_col = f"{prefix}任务结束(班时)" if prefix == "前" else None
                item = row.get(item_col)
                batch_no = row.get(batch_col)
                if pd.isna(item) or pd.isna(batch_no) or str(batch_no) == "-":
                    continue
                if prefix == "前":
                    finish = pd.to_numeric(pd.Series([row.get(end_col)]), errors="coerce").iloc[0]
                else:
                    # The current row does not store current task end; it will appear as a
                    # previous task in the next adjacency row. Single/final tasks are handled below.
                    finish = None
                if finish is not None and pd.notna(finish):
                    finish_map[(item, int(batch_no))] = float(finish)

    for _, row in pack_df.iterrows():
        item = row["药品规格"]
        total_batches = int(row["总批次"])
        pack_start = float(row["包装开工(班时)"])
        pack_end = float(row["包装完工(班时)"])
        per_batch = (pack_end - pack_start) / total_batches if total_batches else 0
        for batch_no in range(1, total_batches + 1):
            finish_map.setdefault((item, batch_no), pack_start + batch_no * per_batch)

    for idx, row in batch_df.iterrows():
        key = (row["药品规格"], int(row["批次号"]))
        finish = finish_map.get(key)
        if finish is None:
            continue
        cycle = finish - float(row["配料开工(班时)"])
        limit = float(row["批次周期上限(班时)"])
        batch_df.at[idx, "批次真实生产周期(班时)"] = round(cycle, 2)
        batch_df.at[idx, "批次超周期(班时)"] = round(max(0.0, cycle - limit), 2)

    for idx, row in pack_df.iterrows():
        item = row["药品规格"]
        pack_df.at[idx, "延误班时"] = round(max(0.0, float(row["包装完工(班时)"]) - float(due.get(item, 0.0))), 2)


def postprocess_detail_for_sundays(input_file, output_file, demo_file, aps_file, start_date, department="210车间"):
    (
        _I, _J1, _J2, _J3, _J4, _B, _p1, _p2, _p3, _p4, _p5, due, _T, _w,
        _stage_staff_limits, _clear_time_matrices, _machine_available_time,
        _release_time, _max_continuous_run,
    ) = build_schedule_inputs(demo_file, aps_file, department=department)

    batch_df = pd.read_excel(input_file, sheet_name=BATCH_SHEET)
    pack_df = pd.read_excel(input_file, sheet_name=PACK_SHEET)
    clear_df = pd.read_excel(input_file, sheet_name=CLEAR_SHEET)

    for sheet_name, df in [(BATCH_SHEET, batch_df), (PACK_SHEET, pack_df), (CLEAR_SHEET, clear_df)]:
        for column in TIME_COLUMNS[sheet_name]:
            _map_time_column(df, column, start_date)

    _update_actual_gap(clear_df)
    _recompute_cycles(batch_df, pack_df, clear_df, due)

    report = pd.DataFrame(
        [
            {
                "说明": "所有班时已从连续工作班时映射到自然日历班时，周日两班为空档，后续方案整体顺延。",
                "起始日期": start_date.strftime("%Y-%m-%d"),
                "周日": "不加工",
            }
        ]
    )

    with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
        batch_df.to_excel(writer, sheet_name=BATCH_SHEET, index=False)
        pack_df.to_excel(writer, sheet_name=PACK_SHEET, index=False)
        clear_df.to_excel(writer, sheet_name=CLEAR_SHEET, index=False)
        report.to_excel(writer, sheet_name=REPORT_SHEET, index=False)


def main():
    parser = argparse.ArgumentParser(description="Post-process a schedule so Sundays are non-working days.")
    parser.add_argument("--input", default=DEFAULT_INPUT, help="Input detail Excel.")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="Output detail Excel.")
    parser.add_argument("--demo", default=DEFAULT_DEMO, help="Demo/monthly plan Excel.")
    parser.add_argument("--aps", default=DEFAULT_APS, help="APS capacity Excel.")
    parser.add_argument("--start-date", default=DEFAULT_START_DATE, help="Schedule start date, YYYY-MM-DD.")
    args = parser.parse_args()

    start_date = datetime.strptime(args.start_date, "%Y-%m-%d")
    postprocess_detail_for_sundays(args.input, args.output, args.demo, args.aps, start_date)
    print(f"已输出周日休息明细: {args.output}")


if __name__ == "__main__":
    main()
