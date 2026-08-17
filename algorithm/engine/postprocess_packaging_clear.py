import argparse
import sys

import pandas as pd

from input_data import build_schedule_inputs, get_pack_spec

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


DEFAULT_INPUT = "排产结果明细.xlsx"
DEFAULT_OUTPUT = "排产结果明细_packclear.xlsx"
DEFAULT_DEMO = "./输入/demo2.xlsx"
DEFAULT_APS = "./输入/副本APS排产信息-4.30.xlsx"


BATCH_SHEET = "批次工序计划"
PACK_SHEET = "包装及摘要"
CLEAR_SHEET = "设备清场明细"
REPORT_SHEET = "包装定期清场后处理"
CHECK_SHEET = "包装连续运行校验"


def _to_num(series):
    return pd.to_numeric(series, errors="coerce")


def _duration(proc_time, item, machine, fallback=0.0):
    value = proc_time.get((item, machine), fallback)
    try:
        return float(value)
    except Exception:
        return float(fallback)


def _stage_ready_time(batch_row, p2, p3):
    item = batch_row["药品规格"]
    tab_machine = batch_row["压片设备"]
    tab_start = float(batch_row["压片开工(班时)"])
    tab_end = tab_start + _duration(p2, item, tab_machine)

    coat_start = pd.to_numeric(pd.Series([batch_row.get("包衣开工(班时)")]), errors="coerce").iloc[0]
    if pd.notna(coat_start):
        coat_machine = batch_row["包衣设备"]
        return float(coat_start) + _duration(p3, item, coat_machine)
    return tab_end


def _schedule_pack_lines(pack_df, batch_df, p2, p3, p4, due, max_run=11.0, clear_duration=1.0):
    pack_new = pack_df.copy()
    batch_new = batch_df.copy()

    pack_new["_orig_index"] = pack_new.index
    batch_new["_orig_index"] = batch_new.index

    pack_new["包装开工(班时)"] = _to_num(pack_new["包装开工(班时)"])
    pack_new["包装完工(班时)"] = _to_num(pack_new["包装完工(班时)"])
    pack_new["总批次"] = _to_num(pack_new["总批次"]).astype(int)

    batch_new["批次号"] = _to_num(batch_new["批次号"]).astype(int)
    batch_new["配料开工(班时)"] = _to_num(batch_new["配料开工(班时)"])
    batch_new["批次周期上限(班时)"] = _to_num(batch_new["批次周期上限(班时)"])
    batch_new["批次真实生产周期(班时)"] = _to_num(batch_new["批次真实生产周期(班时)"]).astype(float)
    batch_new["批次超周期(班时)"] = _to_num(batch_new["批次超周期(班时)"]).astype(float)

    pack_batch_tasks = []
    inserted_clears = []

    for line, line_tasks in pack_new.sort_values(["分装铝塑设备", "包装开工(班时)", "_orig_index"]).groupby("分装铝塑设备", sort=False):
        cumulative_shift = 0.0
        current_time = None
        continuous_run = 0.0

        for _, pack_row in line_tasks.iterrows():
            item = pack_row["药品规格"]
            orig_start = float(pack_row["包装开工(班时)"])
            orig_end = float(pack_row["包装完工(班时)"])
            total_batches = int(pack_row["总批次"])
            if total_batches <= 0:
                continue

            fallback_pack_duration = (orig_end - orig_start) / total_batches if orig_end >= orig_start else 0.0
            batch_duration = _duration(p4, item, line, fallback=fallback_pack_duration)
            if batch_duration <= 0:
                batch_duration = fallback_pack_duration

            scheduled_start = orig_start + cumulative_shift
            if current_time is not None:
                gap = scheduled_start - current_time
                if gap >= clear_duration - 1e-9:
                    continuous_run = 0.0
                if scheduled_start < current_time:
                    scheduled_start = current_time

            task_start = scheduled_start
            t = scheduled_start

            item_batches = batch_new[batch_new["药品规格"] == item].sort_values("批次号")
            if len(item_batches) != total_batches:
                print(
                    f"警告: {item} 在包装摘要中的总批次={total_batches}, "
                    f"批次工序计划中批次数={len(item_batches)}",
                    file=sys.stderr,
                )

            for _, batch_row in item_batches.iterrows():
                batch_no = int(batch_row["批次号"])
                ready_time = _stage_ready_time(batch_row, p2, p3)
                if t < ready_time - 1e-9:
                    t = ready_time
                    continuous_run = 0.0

                if continuous_run > 1e-9 and continuous_run + batch_duration > max_run + 1e-9:
                    clear_start = t
                    clear_end = t + clear_duration
                    inserted_clears.append(
                        {
                            "包装线": line,
                            "清场开始(班时)": round(clear_start, 2),
                            "清场结束(班时)": round(clear_end, 2),
                            "清场时长": clear_duration,
                            "清场前连续运行(班时)": round(continuous_run, 2),
                            "后续药品规格": item,
                            "后续批次": batch_no,
                            "原因": f"包装线连续运行超过{max_run:g}班时前插入定期清场",
                        }
                    )
                    t = clear_end
                    cumulative_shift += clear_duration
                    continuous_run = 0.0

                batch_start = t
                batch_end = batch_start + batch_duration
                pack_batch_tasks.append(
                    {
                        "line": line,
                        "item": item,
                        "batch": batch_no,
                        "start": batch_start,
                        "end": batch_end,
                        "duration": batch_duration,
                        "feature": get_pack_spec(item),
                    }
                )

                batch_idx = batch_new.index[
                    (batch_new["药品规格"] == item) & (batch_new["批次号"] == batch_no)
                ]
                if len(batch_idx) > 0:
                    idx = batch_idx[0]
                    cycle = batch_end - float(batch_new.at[idx, "配料开工(班时)"])
                    limit = float(batch_new.at[idx, "批次周期上限(班时)"])
                    batch_new.at[idx, "批次真实生产周期(班时)"] = round(cycle, 2)
                    batch_new.at[idx, "批次超周期(班时)"] = round(max(0.0, cycle - limit), 2)

                t = batch_end
                if batch_duration > max_run + 1e-9:
                    inserted_clears.append(
                        {
                            "包装线": line,
                            "清场开始(班时)": None,
                            "清场结束(班时)": None,
                            "清场时长": None,
                            "清场前连续运行(班时)": round(batch_duration, 2),
                            "后续药品规格": item,
                            "后续批次": batch_no,
                            "原因": f"单批包装时长{batch_duration:.2f}超过{max_run:g}班时，后处理未拆分单批",
                        }
                    )
                    continuous_run = batch_duration
                else:
                    continuous_run += batch_duration

            current_time = t
            pack_idx = pack_new.index[pack_new["_orig_index"] == pack_row["_orig_index"]][0]
            pack_new.at[pack_idx, "包装开工(班时)"] = round(task_start, 2)
            pack_new.at[pack_idx, "包装完工(班时)"] = round(t, 2)
            pack_new.at[pack_idx, "延误班时"] = round(max(0.0, t - float(due.get(item, 0.0))), 2)

    pack_new = pack_new.drop(columns=["_orig_index"])
    batch_new = batch_new.drop(columns=["_orig_index"])
    return pack_new, batch_new, pd.DataFrame(pack_batch_tasks), pd.DataFrame(inserted_clears)


def _build_pack_clear_records(pack_batch_tasks, clear_time_matrices, max_run=11.0):
    if pack_batch_tasks.empty:
        return pd.DataFrame()

    records = []
    pack_clear = clear_time_matrices.get(4, {})

    for line, tasks in pack_batch_tasks.sort_values(["line", "start", "end", "item", "batch"]).groupby("line", sort=False):
        rows = list(tasks.to_dict("records"))
        if len(rows) < 2:
            continue

        running_total = rows[0]["duration"]
        for prev_task, curr_task in zip(rows, rows[1:]):
            required_clear = float(pack_clear.get((prev_task["item"], curr_task["item"]), 0.0))
            actual_gap = float(curr_task["start"] - prev_task["end"])
            effective_clear = required_clear
            clear_type = "小清场" if required_clear > 0 else "无清场"

            if actual_gap >= 1.0 - 1e-9 and required_clear == 0:
                effective_clear = 1.0
                clear_type = "定期清场"
            elif actual_gap >= 1.0 - 1e-9 and running_total >= max_run - 1e-9:
                effective_clear = max(required_clear, 1.0)
                clear_type = "定期清场" if required_clear == 0 else "小清场+定期清场"

            if actual_gap >= 1.0 - 1e-9 or required_clear > 0:
                running_total = curr_task["duration"]
            else:
                running_total += curr_task["duration"]

            records.append(
                {
                    "工序": "4. 包装",
                    "设备": line,
                    "前任务药品规格": prev_task["item"],
                    "前任务批次": prev_task["batch"],
                    "前任务开始(班时)": round(prev_task["start"], 2),
                    "前任务结束(班时)": round(prev_task["end"], 2),
                    "后任务药品规格": curr_task["item"],
                    "后任务批次": curr_task["batch"],
                    "后任务开始(班时)": round(curr_task["start"], 2),
                    "清场开始(班时)": round(prev_task["end"], 2),
                    "清场结束(班时)": round(prev_task["end"] + effective_clear, 2),
                    "理论清场时长": round(effective_clear, 2),
                    "实际间隔": round(actual_gap, 2),
                    "清场是否满足": actual_gap + 1e-9 >= effective_clear,
                    "前任务特征": prev_task["feature"],
                    "后任务特征": curr_task["feature"],
                    "特征是否相同": prev_task["feature"] == curr_task["feature"],
                    "连续无清场累计(班时)": round(running_total, 2),
                    "是否超过11班时": running_total > max_run + 1e-9,
                    "清场类型": clear_type,
                }
            )

    return pd.DataFrame(records)


def _build_continuous_run_check(pack_batch_tasks, max_run=11.0):
    if pack_batch_tasks.empty:
        return pd.DataFrame()

    rows = []
    for line, tasks in pack_batch_tasks.sort_values(["line", "start", "end"]).groupby("line", sort=False):
        segment_start = None
        segment_end = None
        segment_run = 0.0

        for task in tasks.to_dict("records"):
            if segment_start is None:
                segment_start = task["start"]
                segment_end = task["end"]
                segment_run = task["duration"]
                continue

            gap = task["start"] - segment_end
            if gap >= 1.0 - 1e-9:
                rows.append(
                    {
                        "包装线": line,
                        "连续段开始": round(segment_start, 2),
                        "连续段结束": round(segment_end, 2),
                        "连续运行时长": round(segment_run, 2),
                        "是否超过11班时": segment_run > max_run + 1e-9,
                    }
                )
                segment_start = task["start"]
                segment_run = task["duration"]
            else:
                segment_run += task["duration"]
            segment_end = task["end"]

        if segment_start is not None:
            rows.append(
                {
                    "包装线": line,
                    "连续段开始": round(segment_start, 2),
                    "连续段结束": round(segment_end, 2),
                    "连续运行时长": round(segment_run, 2),
                    "是否超过11班时": segment_run > max_run + 1e-9,
                }
            )

    return pd.DataFrame(rows)


def postprocess_pack_clear(
    input_file,
    output_file,
    demo_file,
    aps_file,
    max_run=11.0,
    clear_duration=1.0,
    minor_cleaning_time_override=None,
    department="210车间",
):
    (
        _I, _J1, _J2, _J3, _J4, _B, _p1, p2, p3, p4, _p5, d, _T, _w,
        _stage_staff_limits, clear_time_matrices, _machine_available_time,
        _release_time, _max_continuous_run,
    ) = build_schedule_inputs(
        demo_file,
        aps_file,
        max_continuous_run_override=max_run,
        minor_cleaning_time_override=minor_cleaning_time_override,
        department=department,
    )

    batch_df = pd.read_excel(input_file, sheet_name=BATCH_SHEET)
    pack_df = pd.read_excel(input_file, sheet_name=PACK_SHEET)
    clear_df = pd.read_excel(input_file, sheet_name=CLEAR_SHEET)

    pack_new, batch_new, pack_batch_tasks, inserted_clears = _schedule_pack_lines(
        pack_df, batch_df, p2, p3, p4, d, max_run=max_run, clear_duration=clear_duration
    )

    non_pack_clear = clear_df[clear_df["工序"] != "4. 包装"].copy()
    pack_clear = _build_pack_clear_records(pack_batch_tasks, clear_time_matrices, max_run=max_run)
    clear_new = pd.concat([non_pack_clear, pack_clear], ignore_index=True)
    check_df = _build_continuous_run_check(pack_batch_tasks, max_run=max_run)

    with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
        batch_new.to_excel(writer, sheet_name=BATCH_SHEET, index=False)
        pack_new.to_excel(writer, sheet_name=PACK_SHEET, index=False)
        clear_new.to_excel(writer, sheet_name=CLEAR_SHEET, index=False)
        inserted_clears.to_excel(writer, sheet_name=REPORT_SHEET, index=False)
        check_df.to_excel(writer, sheet_name=CHECK_SHEET, index=False)

    return inserted_clears, check_df


def main():
    parser = argparse.ArgumentParser(description="Post-process packaging lines with periodic 1-shift clear after 11 shifts.")
    parser.add_argument("--input", default=DEFAULT_INPUT, help="Input schedule Excel.")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="Output schedule Excel.")
    parser.add_argument("--demo", default=DEFAULT_DEMO, help="Demo/monthly plan Excel used by input_data.")
    parser.add_argument("--aps", default=DEFAULT_APS, help="APS capacity Excel used by input_data.")
    parser.add_argument("--max-run", type=float, default=11.0, help="Maximum continuous packaging run in shifts.")
    parser.add_argument("--clear-duration", type=float, default=1.0, help="Periodic clear duration in shifts.")
    args = parser.parse_args()

    inserted_clears, check_df = postprocess_pack_clear(
        args.input,
        args.output,
        args.demo,
        args.aps,
        max_run=args.max_run,
        clear_duration=args.clear_duration,
    )

    over_count = int(check_df["是否超过11班时"].sum()) if not check_df.empty else 0
    print(f"已输出: {args.output}")
    print(f"插入定期清场次数: {len(inserted_clears)}")
    print(f"包装连续运行超限段数: {over_count}")


if __name__ == "__main__":
    main()
