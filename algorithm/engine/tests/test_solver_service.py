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
            naturalDays=31,
            shiftCount=2,
            mixing=4,
            tableting=3,
            coating=2,
            packaging=5,
            solverTimeLimitMinutes=20,
        )

        self.assertEqual(options["schedule_start_date"], datetime(2026, 8, 1))
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
            naturalDays=31,
            shiftCount=2,
            mixing=4,
            tableting=3,
            coating=2,
            packaging=5,
            solverTimeLimitMinutes=20,
            department="Workshop-A",
        )

        self.assertEqual(options["department"], "Workshop-A")

    def test_allocate_solver_seconds_keeps_exact_total(self):
        from solver_service import allocate_solver_seconds

        self.assertEqual(allocate_solver_seconds(20), (750, 225, 225))
        self.assertEqual(sum(allocate_solver_seconds(1)), 60)

    def test_stage_budget_caps_to_remaining_deadline(self):
        from run_schedule_hybrid import _stage_budget
        import time

        self.assertEqual(_stage_budget(100, None), 100.0)
        self.assertEqual(_stage_budget(100, time.monotonic() - 1), 0.0)


class SolverServiceTaskTests(unittest.TestCase):
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
                    1001,
                    "plan.xlsx",
                    "aps.xlsx",
                    "2026年08月",
                    2,
                    0.5,
                    0.25,
                    0.5,
                    31,
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
            self.assertEqual(result["taskId"], "1001")
            self.assertEqual(result["status"], "SUCCESS")
            self.assertEqual(status["status"], "SUCCESS")
            self.assertTrue((task_root / result["taskId"] / "status.json").exists())
            self.assertTrue((task_root / result["taskId"] / "solver.log").exists())
            self.assertTrue(status["resultFiles"]["visualBoard"].endswith("可排产结果可视化.xlsx"))
            self.assertFalse(status["stopRequested"])
            self.assertTrue(status["resultReady"])

    def test_time_limit_finishes_success_instead_of_stopped(self):
        from solver_service import query_solver_status, run_solver

        clock = {"now": 0.0}

        def monotonic():
            return clock["now"]

        def fake_pipeline(**kwargs):
            clock["now"] = 10 ** 9
            self.assertTrue(kwargs["stop_checker"]())
            self.assertFalse(kwargs["user_stop_checker"]())
            Path(kwargs["visual_output_file"]).write_text("visual", encoding="utf-8")
            kwargs["on_result_exported"]("final")
            return {"cp_status": "FEASIBLE", "stopped": False, "timed_out": True, "files_exported": True}

        with tempfile.TemporaryDirectory() as tmp:
            task_root = Path(tmp)
            with mock.patch("solver_service.time.monotonic", side_effect=monotonic), mock.patch(
                "solver_service.run_full_schedule_pipeline", side_effect=fake_pipeline
            ):
                result = run_solver(
                    1002,
                    "plan.xlsx",
                    "aps.xlsx",
                    "2026年08月",
                    2, 0.5, 0.25, 0.5, 31, 2, 4, 3, 2, 5, 20,
                    department="Workshop-A",
                    task_root=task_root,
                    run_inline=True,
                )

            status = query_solver_status(result["taskId"], task_root=task_root)
            self.assertEqual(status["status"], "SUCCESS")
            self.assertFalse(status["stopRequested"])
            self.assertTrue(status["resultReady"])
            self.assertIsNone(status.get("stopMode"))

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
                    1002,
                    "plan.xlsx",
                    "aps.xlsx",
                    "2026年08月",
                    2,
                    0.5,
                    0.25,
                    0.5,
                    31,
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
            self.assertEqual(result["taskId"], "1002")
            self.assertEqual(result["status"], "STOPPED")
            self.assertEqual(status["status"], "STOPPED")
            self.assertTrue(status["stopRequested"])
            self.assertTrue(status["resultReady"])

    def test_inline_worker_records_partial_result_only_after_visual_file_exists(self):
        from solver_service import query_solver_status, run_solver

        with tempfile.TemporaryDirectory() as tmp:
            task_root = Path(tmp)
            seen = {}

            def fake_pipeline(**kwargs):
                visual = Path(kwargs["visual_output_file"])
                kwargs["on_result_exported"]("partial")
                status_before = json.loads((visual.parent / "status.json").read_text(encoding="utf-8"))
                seen["before"] = status_before
                visual.write_text("visual", encoding="utf-8")
                kwargs["on_result_exported"]("partial")
                seen["after"] = json.loads((visual.parent / "status.json").read_text(encoding="utf-8"))
                return {"final_objective": 1}

            with mock.patch("solver_service.run_full_schedule_pipeline", side_effect=fake_pipeline):
                result = run_solver(
                    1003,
                    "plan.xlsx",
                    "aps.xlsx",
                    "2026年08月",
                    2,
                    0.5,
                    0.25,
                    0.5,
                    31,
                    2,
                    4,
                    3,
                    2,
                    5,
                    20,
                    task_root=task_root,
                    run_inline=True,
                )

            self.assertNotEqual(seen["before"].get("resultKind"), "partial")
            self.assertFalse(seen["before"].get("resultFiles"))
            self.assertEqual(seen["after"]["resultKind"], "partial")
            self.assertTrue(seen["after"]["resultReady"])
            self.assertTrue(seen["after"]["resultFiles"]["visualBoard"].endswith("可排产结果可视化.xlsx"))
            status = query_solver_status(result["taskId"], task_root=task_root)
            self.assertEqual(status["status"], "SUCCESS")
            self.assertEqual(status["resultKind"], "final")


class RunSchedulePipelineStopTests(unittest.TestCase):
    def test_solver_reports_clear_error_when_department_has_no_aps_matches(self):
        from run_schedule_hybrid import solve_pharmaceutical_schedule_cp_hybrid

        empty_inputs = (
            [], [], [], [], [], {}, {}, {}, {}, {}, {}, {}, {}, {},
            {}, {}, {}, {}, 11,
        )
        with mock.patch(
            "run_schedule_hybrid.core.build_schedule_inputs",
            return_value=empty_inputs,
        ):
            with self.assertRaisesRegex(ValueError, "没有可排产数据"):
                solve_pharmaceutical_schedule_cp_hybrid(
                    "plan.xlsx",
                    "aps.xlsx",
                    department="302车间",
                )

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

    def test_solver_exports_first_solution_before_search_and_final_after(self):
        from run_schedule_hybrid import _solve_pharmaceutical_schedule_cp_hybrid_impl

        exports = []

        def fake_inputs(*args, **kwargs):
            return (
                ["item-1"], ["mix-1"], ["tab-1"], ["coat-1"], ["pack-1"],
                {"item-1": [1]}, {}, {}, {}, {}, {}, {}, {}, {},
                {}, {}, {}, {}, 11,
            )

        def fake_pool(*args, **kwargs):
            return 10, "auto:risk", {"t1": {("item-1", 1): 0}}

        def fake_alns(*args, **kwargs):
            self.assertEqual(exports, ["partial"])
            return {"t1": {("item-1", 1): 1}}, None, None, 8

        def fake_exporter(snapshot, kind, model_data):
            exports.append(kind)

        logger = mock.Mock()
        with mock.patch("run_schedule_hybrid.core.build_schedule_inputs", side_effect=fake_inputs), mock.patch(
            "run_schedule_hybrid._build_initial_pool", side_effect=fake_pool
        ), mock.patch("run_schedule_hybrid.core._run_alns", side_effect=fake_alns), mock.patch(
            "run_schedule_hybrid.core._left_shift_pack_snapshot",
            return_value=({"t1": {("item-1", 1): 1}}, 8, []),
        ), mock.patch(
            "run_schedule_hybrid.core._build_model",
            return_value={"model": mock.Mock(), "vars": {"t1": {}, "t4": {}, "x4": {}, "E": {}}},
        ), mock.patch("run_schedule_hybrid.core._add_snapshot_hints"), mock.patch(
            "run_schedule_hybrid.core._snapshot_to_name_map", return_value={}
        ), mock.patch("run_schedule_hybrid.core._SnapshotValueSolver"):
            result = _solve_pharmaceutical_schedule_cp_hybrid_impl(
                "plan.xlsx",
                "aps.xlsx",
                stage1_sec=1,
                stage2_sec=0,
                cp_sec=0,
                progress_logger=logger,
                result_exporter=fake_exporter,
            )

        self.assertEqual(exports, ["partial", "final"])
        self.assertTrue(result["files_exported"])
        logger.on_first_solution_found.assert_called_once()
        logger.on_time_limit.assert_called_once()

    def test_wall_clock_timeout_stops_search_without_user_stop(self):
        from run_schedule_hybrid import _solve_pharmaceutical_schedule_cp_hybrid_impl

        logger = mock.Mock()

        def fake_inputs(*args, **kwargs):
            return (
                ["item-1"], ["mix-1"], ["tab-1"], ["coat-1"], ["pack-1"],
                {"item-1": [1]}, {}, {}, {}, {}, {}, {}, {}, {},
                {}, {}, {}, {}, 11,
            )

        with mock.patch("run_schedule_hybrid.core.build_schedule_inputs", side_effect=fake_inputs), mock.patch(
            "run_schedule_hybrid._build_initial_pool"
        ) as build_pool, mock.patch("run_schedule_hybrid.core._run_alns") as run_alns:
            result = _solve_pharmaceutical_schedule_cp_hybrid_impl(
                "plan.xlsx",
                "aps.xlsx",
                stage1_sec=30,
                stage2_sec=30,
                cp_sec=30,
                progress_logger=logger,
                stop_checker=lambda: True,
                user_stop_checker=lambda: False,
                deadline_monotonic=0,
            )

        self.assertTrue(result["timed_out"])
        self.assertFalse(result["stopped"])
        build_pool.assert_not_called()
        run_alns.assert_not_called()
        logger.on_time_limit.assert_called_once()
        logger.on_stop_requested.assert_not_called()

    def test_pipeline_skips_fallback_postprocess_when_solver_already_exported(self):
        from run_schedule_hybrid import run_full_schedule_pipeline

        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp)

            def fake_solve(*args, **kwargs):
                self.assertIsNotNone(kwargs.get("result_exporter"))
                return {"stopped": False, "result_ready": True, "files_exported": True}

            with mock.patch(
                "run_schedule_hybrid.solve_pharmaceutical_schedule_cp_hybrid",
                side_effect=fake_solve,
            ), mock.patch("run_schedule_hybrid.postprocess_pack_clear") as pack_clear, mock.patch(
                "run_schedule_hybrid.postprocess_detail_for_sundays"
            ) as sunday, mock.patch("run_schedule_hybrid.generate_template_schedule_board") as visual:
                run_full_schedule_pipeline(
                    plan_file="plan.xlsx",
                    aps_file="aps.xlsx",
                    schedule_month="2026-08",
                    raw_output_file=task_dir / "raw.xlsx",
                    pack_clear_output_file=task_dir / "pack.xlsx",
                    sunday_rest_output_file=task_dir / "sunday.xlsx",
                    visual_output_file=task_dir / "visual.xlsx",
                )

        pack_clear.assert_not_called()
        sunday.assert_not_called()
        visual.assert_not_called()


if __name__ == "__main__":
    unittest.main()
