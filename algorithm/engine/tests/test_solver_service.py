import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class SolverServiceConversionTests(unittest.TestCase):
    def test_parse_schedule_month_accepts_chinese_month(self):
        from solver_service import parse_schedule_month

        self.assertEqual(parse_schedule_month("2026年08月"), datetime(2026, 8, 1))

    def test_parse_schedule_month_accepts_dash_and_slash_month(self):
        from solver_service import parse_schedule_month

        self.assertEqual(parse_schedule_month("2026-08"), datetime(2026, 8, 1))
        self.assertEqual(parse_schedule_month("2026/08"), datetime(2026, 8, 1))

    def test_build_solver_options_converts_days_to_shifts_and_maps_staff(self):
        from solver_service import build_solver_options

        options = build_solver_options(
            scheduleMonth="2026年08月",
            continuousRunLimitDays=2,
            majorCleaningDays=0.5,
            minorCleaningDays=0.25,
            periodicCleaningDays=0.5,
            naturalDays=1,
            shiftCount=2,
            mixing=4,
            tableting=3,
            coating=2,
            packaging=5,
            solverTimeLimitMinutes=20,
        )

        self.assertEqual(options["schedule_start_date"], datetime(2026, 8, 1))
        self.assertEqual(options["shifts_per_day"], 2.0)
        self.assertEqual(options["continuous_run_shifts"], 4.0)
        self.assertEqual(options["major_cleaning_shifts"], 1.0)
        self.assertEqual(options["minor_cleaning_shifts"], 0.5)
        self.assertEqual(options["periodic_cleaning_shifts"], 1.0)
        self.assertEqual(options["stage_staff_limits"], {1: 4, 2: 3, 3: 2, 4: 5})

    def test_build_solver_options_keeps_selected_department(self):
        from solver_service import build_solver_options

        options = build_solver_options(
            scheduleMonth="2026-08",
            continuousRunLimitDays=2,
            majorCleaningDays=0.5,
            minorCleaningDays=0.25,
            periodicCleaningDays=0.5,
            naturalDays=1,
            shiftCount=2,
            mixing=4,
            tableting=3,
            coating=2,
            packaging=5,
            solverTimeLimitMinutes=20,
            department="Workshop-A",
        )

        self.assertEqual(options["department"], "Workshop-A")

    def test_build_solver_options_derives_three_shifts_per_day(self):
        from solver_service import build_solver_options

        options = build_solver_options(
            scheduleMonth="2026-08",
            continuousRunLimitDays=2,
            majorCleaningDays=0.5,
            minorCleaningDays=0.25,
            periodicCleaningDays=0.5,
            naturalDays=1,
            shiftCount=3,
            mixing=4,
            tableting=3,
            coating=2,
            packaging=5,
            solverTimeLimitMinutes=20,
        )

        self.assertEqual(options["shifts_per_day"], 3.0)
        self.assertEqual(options["continuous_run_shifts"], 6.0)
        self.assertEqual(options["major_cleaning_shifts"], 1.5)

    def test_build_solver_options_derives_five_shifts_per_day(self):
        from solver_service import build_solver_options

        options = build_solver_options(
            scheduleMonth="2026-08",
            continuousRunLimitDays=2,
            majorCleaningDays=0.5,
            minorCleaningDays=0.25,
            periodicCleaningDays=0.5,
            naturalDays=1,
            shiftCount=5,
            mixing=4,
            tableting=3,
            coating=2,
            packaging=5,
            solverTimeLimitMinutes=20,
        )

        self.assertEqual(options["shifts_per_day"], 5)
        self.assertEqual(options["continuous_run_shifts"], 10.0)
        self.assertEqual(options["major_cleaning_shifts"], 2.5)

    def test_allocate_solver_seconds_keeps_exact_total(self):
        from solver_service import allocate_solver_seconds

        self.assertEqual(allocate_solver_seconds(20), (750, 225, 225))
        self.assertEqual(sum(allocate_solver_seconds(1)), 60)


class SolverServiceTaskTests(unittest.TestCase):
    def test_frontend_log_contains_only_progress_messages_with_timestamps(self):
        from solver_service import query_solver_log, run_solver

        with tempfile.TemporaryDirectory() as tmp:
            task_root = Path(tmp)

            def fake_pipeline(**kwargs):
                print("普通算法输出不应展示给前端")
                kwargs["progress_emit"]("开始计算排产方案...")
                kwargs["progress_emit"]("已找到一个排产方案，正在进一步计算搜索...")
                Path(kwargs["visual_output_file"]).write_text("visual", encoding="utf-8")
                return {"final_objective": 1}

            with mock.patch("solver_service.run_full_schedule_pipeline", side_effect=fake_pipeline):
                result = run_solver(
                    "plan.xlsx",
                    "aps.xlsx",
                    "2026-08",
                    2,
                    0.5,
                    0.25,
                    0.5,
                    1,
                    2,
                    4,
                    3,
                    2,
                    5,
                    20,
                    task_root=task_root,
                    run_inline=True,
                )

            log = query_solver_log(result["taskId"], task_root=task_root)

            self.assertRegex(
                log["content"],
                r"^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\] 开始计算排产方案\.\.\.\n",
            )
            self.assertIn("已找到一个排产方案，正在进一步计算搜索...", log["content"])
            self.assertNotIn("任务创建", log["content"])
            self.assertNotIn("求解子进程启动", log["content"])
            self.assertNotIn("普通算法输出不应展示给前端", log["content"])

    def test_run_solver_creates_task_files_and_finishes_success_with_inline_worker(self):
        from solver_service import query_solver_status, run_solver

        with tempfile.TemporaryDirectory() as tmp:
            task_root = Path(tmp)

            def fake_pipeline(**kwargs):
                self.assertEqual(kwargs["department"], "Workshop-A")
                Path(kwargs["visual_output_file"]).write_text("visual", encoding="utf-8")
                return {"final_objective": 1}

            with mock.patch("solver_service.run_full_schedule_pipeline", side_effect=fake_pipeline):
                result = run_solver(
                    "plan.xlsx",
                    "aps.xlsx",
                    "2026年08月",
                    2,
                    0.5,
                    0.25,
                    0.5,
                    1,
                    2,
                    4,
                    3,
                    2,
                    5,
                    20,
                    department="Workshop-A",
                    task_root=task_root,
                    run_inline=True,
                )

            status = query_solver_status(result["taskId"], task_root=task_root)
            self.assertEqual(result["status"], "SUCCESS")
            self.assertEqual(status["status"], "SUCCESS")
            self.assertTrue((task_root / result["taskId"] / "status.json").exists())
            self.assertTrue((task_root / result["taskId"] / "solver.log").exists())
            self.assertTrue(status["resultFiles"]["visualBoard"].endswith("可排产结果可视化.xlsx"))
            self.assertFalse(status["stopRequested"])
            self.assertTrue(status["resultReady"])

    def test_export_solver_result_returns_only_visual_board(self):
        from solver_service import export_solver_result

        with tempfile.TemporaryDirectory() as tmp:
            task_root = Path(tmp)
            task_dir = task_root / "task-1"
            task_dir.mkdir()
            visual = task_dir / "可排产结果可视化.xlsx"
            visual.write_text("visual", encoding="utf-8")
            (task_dir / "status.json").write_text(
                json.dumps(
                    {
                        "taskId": "task-1",
                        "status": "SUCCESS",
                        "resultFiles": {"visualBoard": str(visual)},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            result = export_solver_result("task-1", task_root=task_root)

            self.assertEqual(result, {"taskId": "task-1", "filePath": str(visual)})

    def test_export_solver_result_allows_stopped_task_with_visual_board(self):
        from solver_service import export_solver_result

        with tempfile.TemporaryDirectory() as tmp:
            task_root = Path(tmp)
            task_dir = task_root / "task-1"
            task_dir.mkdir()
            visual = task_dir / "可排产结果可视化.xlsx"
            visual.write_text("visual", encoding="utf-8")
            (task_dir / "status.json").write_text(
                json.dumps(
                    {
                        "taskId": "task-1",
                        "status": "STOPPED",
                        "resultReady": True,
                        "resultFiles": {"visualBoard": str(visual)},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            result = export_solver_result("task-1", task_root=task_root)

            self.assertEqual(result, {"taskId": "task-1", "filePath": str(visual)})

    def test_export_solver_result_rejects_stopped_task_without_visual_board(self):
        from solver_service import export_solver_result

        with tempfile.TemporaryDirectory() as tmp:
            task_root = Path(tmp)
            task_dir = task_root / "task-1"
            task_dir.mkdir()
            (task_dir / "status.json").write_text(
                json.dumps(
                    {
                        "taskId": "task-1",
                        "status": "STOPPED",
                        "resultReady": False,
                        "resultFiles": {"visualBoard": str(task_dir / "可排产结果可视化.xlsx")},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with self.assertRaises(FileNotFoundError):
                export_solver_result("task-1", task_root=task_root)

    def test_stop_solver_force_stops_when_no_visual_result_exists(self):
        from solver_service import query_solver_status, stop_solver

        with tempfile.TemporaryDirectory() as tmp:
            task_root = Path(tmp)
            task_dir = task_root / "task-1"
            task_dir.mkdir()
            (task_dir / "status.json").write_text(
                json.dumps(
                    {
                        "taskId": "task-1",
                        "status": "RUNNING",
                        "pid": 12345,
                        "stopRequested": False,
                        "resultReady": False,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            fake_process = mock.Mock()
            with mock.patch.dict("solver_service.RUNNING_PROCESSES", {"task-1": fake_process}):
                result = stop_solver("task-1", task_root=task_root)

            self.assertEqual(result["status"], "STOPPED")
            self.assertTrue((task_dir / "stop.requested").exists())
            status = query_solver_status("task-1", task_root=task_root)
            self.assertEqual(status["status"], "STOPPED")
            self.assertTrue(status["stopRequested"])
            self.assertEqual(status["stopMode"], "force")
            fake_process.terminate.assert_called_once()
            fake_process.join.assert_called_once()

    def test_stop_solver_requests_graceful_stop_without_terminating_process(self):
        from solver_service import query_solver_status, stop_solver

        with tempfile.TemporaryDirectory() as tmp:
            task_root = Path(tmp)
            task_dir = task_root / "task-1"
            task_dir.mkdir()
            (task_dir / "可排产结果可视化.xlsx").write_text("visual", encoding="utf-8")
            (task_dir / "status.json").write_text(
                json.dumps(
                    {
                        "taskId": "task-1",
                        "status": "RUNNING",
                        "pid": 12345,
                        "stopRequested": False,
                        "resultReady": True,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            fake_process = mock.Mock()
            with mock.patch.dict("solver_service.RUNNING_PROCESSES", {"task-1": fake_process}):
                result = stop_solver("task-1", task_root=task_root)

            self.assertEqual(result["status"], "STOPPING")
            self.assertTrue((task_dir / "stop.requested").exists())
            status = query_solver_status("task-1", task_root=task_root)
            self.assertEqual(status["status"], "STOPPING")
            self.assertTrue(status["stopRequested"])
            self.assertEqual(status["stopMode"], "graceful")
            fake_process.terminate.assert_not_called()
            fake_process.join.assert_not_called()

    def test_inline_worker_marks_stopped_with_result_when_stop_requested_and_visual_exists(self):
        from solver_service import query_solver_status, run_solver

        with tempfile.TemporaryDirectory() as tmp:
            task_root = Path(tmp)

            def fake_pipeline(**kwargs):
                task_dir = Path(kwargs["visual_output_file"]).parent
                (task_dir / "stop.requested").write_text("stop", encoding="utf-8")
                Path(kwargs["visual_output_file"]).write_text("visual", encoding="utf-8")
                return {"stopped": True, "result_ready": True}

            with mock.patch("solver_service.run_full_schedule_pipeline", side_effect=fake_pipeline):
                result = run_solver(
                    "plan.xlsx",
                    "aps.xlsx",
                    "2026年08月",
                    2,
                    0.5,
                    0.25,
                    0.5,
                    1,
                    2,
                    4,
                    3,
                    2,
                    5,
                    20,
                    task_root=task_root,
                    run_inline=True,
                )

            status = query_solver_status(result["taskId"], task_root=task_root)
            self.assertEqual(result["status"], "STOPPED")
            self.assertEqual(status["status"], "STOPPED")
            self.assertTrue(status["stopRequested"])
            self.assertTrue(status["resultReady"])


class RunSchedulePipelineStopTests(unittest.TestCase):
    def test_pipeline_skips_postprocess_when_solver_stops_without_feasible_result(self):
        from run_schedule_hybrid import run_full_schedule_pipeline

        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp)
            with mock.patch(
                "run_schedule_hybrid.solve_pharmaceutical_schedule_cp_hybrid",
                return_value={"stopped": True, "result_ready": False, "stop_stage": "initial"},
            ), mock.patch("run_schedule_hybrid.postprocess_pack_clear") as pack_clear, mock.patch(
                "run_schedule_hybrid.postprocess_detail_for_sundays"
            ) as sunday, mock.patch("run_schedule_hybrid.generate_template_schedule_board") as visual:
                result = run_full_schedule_pipeline(
                    plan_file="plan.xlsx",
                    aps_file="aps.xlsx",
                    schedule_month="2026年08月",
                    raw_output_file=task_dir / "raw.xlsx",
                    pack_clear_output_file=task_dir / "pack.xlsx",
                    sunday_rest_output_file=task_dir / "sunday.xlsx",
                    visual_output_file=task_dir / "visual.xlsx",
                    stop_checker=lambda: True,
                )

        self.assertTrue(result["stopped"])
        self.assertFalse(result["result_ready"])
        pack_clear.assert_not_called()
        sunday.assert_not_called()
        visual.assert_not_called()

    def test_pipeline_passes_department_to_solver_postprocess_and_visual_board(self):
        from run_schedule_hybrid import run_full_schedule_pipeline

        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp)
            with mock.patch(
                "run_schedule_hybrid.solve_pharmaceutical_schedule_cp_hybrid",
                return_value={"stopped": False, "result_ready": True},
            ) as solve, mock.patch("run_schedule_hybrid.postprocess_pack_clear") as pack_clear, mock.patch(
                "run_schedule_hybrid.postprocess_detail_for_sundays"
            ) as sunday, mock.patch(
                "run_schedule_hybrid.generate_template_schedule_board",
                return_value=str(task_dir / "visual.xlsx"),
            ) as visual:
                run_full_schedule_pipeline(
                    plan_file="plan.xlsx",
                    aps_file="aps.xlsx",
                    schedule_month="2026-08",
                    raw_output_file=task_dir / "raw.xlsx",
                    pack_clear_output_file=task_dir / "pack.xlsx",
                    sunday_rest_output_file=task_dir / "sunday.xlsx",
                    visual_output_file=task_dir / "visual.xlsx",
                    department="Workshop-A",
                )

        self.assertEqual(solve.call_args.kwargs["department"], "Workshop-A")
        self.assertEqual(pack_clear.call_args.kwargs["department"], "Workshop-A")
        self.assertEqual(sunday.call_args.args[5], "Workshop-A")
        self.assertEqual(visual.call_args.args[5], "Workshop-A")

    def test_pipeline_passes_shifts_per_day_to_solver_postprocess_and_visual_board(self):
        from run_schedule_hybrid import run_full_schedule_pipeline

        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp)
            with mock.patch(
                "run_schedule_hybrid.solve_pharmaceutical_schedule_cp_hybrid",
                return_value={"stopped": False, "result_ready": True},
            ) as solve, mock.patch("run_schedule_hybrid.postprocess_pack_clear"), mock.patch(
                "run_schedule_hybrid.postprocess_detail_for_sundays"
            ) as sunday, mock.patch(
                "run_schedule_hybrid.generate_template_schedule_board",
                return_value=str(task_dir / "visual.xlsx"),
            ) as visual:
                run_full_schedule_pipeline(
                    plan_file="plan.xlsx",
                    aps_file="aps.xlsx",
                    schedule_month="2026-08",
                    raw_output_file=task_dir / "raw.xlsx",
                    pack_clear_output_file=task_dir / "pack.xlsx",
                    sunday_rest_output_file=task_dir / "sunday.xlsx",
                    visual_output_file=task_dir / "visual.xlsx",
                    shifts_per_day=3,
                )

        self.assertEqual(solve.call_args.kwargs["shifts_per_day"], 3)
        self.assertEqual(sunday.call_args.kwargs["shifts_per_day"], 3)
        self.assertEqual(visual.call_args.kwargs["shifts_per_day"], 3)


if __name__ == "__main__":
    unittest.main()
