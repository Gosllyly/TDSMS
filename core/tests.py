import json
import shutil
import tempfile
from datetime import timedelta
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote
from unittest.mock import Mock, patch

from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone
import jwt
from openpyxl import load_workbook
from rest_framework.test import APIClient, APITestCase

from algorithm.adapter import append_log
from core.models import (
    ApsArchive, ApsArchiveItem, SolveTask, SysUser, TaskImportRecord, UploadFile, UploadFileItem,
)
from services.excel_service import parse_aps_file
from services.solve_match_service import compare_task_plan_with_aps


class ApiIntegrationTests(APITestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.temp_dir = tempfile.mkdtemp(prefix="tdsms-tests-")
        cls.override = override_settings(
            TASK_UPLOAD_ROOT=str(Path(cls.temp_dir) / "uploads"),
            RESULT_EXCEL_ROOT=str(Path(cls.temp_dir) / "results"),
            ALGORITHM_LOG_ROOT=str(Path(cls.temp_dir) / "logs"),
            ALGORITHM_RUN_ROOT=str(Path(cls.temp_dir) / "algorithm_runs"),
            ALGORITHM_INPUT_ROOT=str(Path(cls.temp_dir) / "algorithm_inputs"),
            USE_MOCK_ALGORITHM=False,
        )
        cls.override.enable()

    @classmethod
    def tearDownClass(cls):
        cls.override.disable()
        shutil.rmtree(cls.temp_dir, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.user = SysUser.objects.create(username="user01", password=make_password("123456"), realName="张三")
        self.other = SysUser.objects.create(username="user02", password=make_password("123456"))
        self.admin = SysUser.objects.create(username="admin", password=make_password("admin123"), role="admin")
        self.client = APIClient()

    def login(self, username="user01", password="123456"):
        response = self.client.post("/tdsms/auth/login", {"username": username, "password": password}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['data']['token']}")
        return response

    def create_archive(self):
        self.login()
        template = Path(settings.MEDIA_ROOT) / "templates" / "APS排产信息模板.xlsx"
        with template.open("rb") as stream:
            response = self.client.post("/tdsms/aps/create", {"archiveName": "方案一", "file": stream}, format="multipart")
        self.assertEqual(response.status_code, 200, response.data)
        return ApsArchive.objects.get(archiveId=response.data["data"]["archiveId"])

    def create_task(self):
        archive = self.create_archive()
        template = Path(settings.MEDIA_ROOT) / "templates" / "药业车间分解编排计划表模板.xlsx"
        with template.open("rb") as stream:
            response = self.client.post("/tdsms/task/import", {"apsArchiveId": archive.archiveId, "remark": "测试", "file": stream}, format="multipart")
        self.assertEqual(response.status_code, 200, response.data)
        return TaskImportRecord.objects.get(taskId=response.data["data"]["taskId"])

    def solvable_department(self, task):
        departments = task.file.items.filter(isDeleted=0).values_list(
            "departmentName", flat=True,
        ).distinct()
        for department in departments:
            if compare_task_plan_with_aps(task, department)["matchedCount"]:
                return department
        self.fail("测试模板中不存在可与APS档案匹配的部门")

    def test_authentication_and_admin_permission(self):
        response = self.login()
        self.assertEqual(response.data["data"]["userInfo"]["role"], "user")
        denied = self.client.get("/tdsms/admin/query")
        self.assertEqual(denied.status_code, 403)
        self.client.credentials()
        self.login("admin", "admin123")
        created = self.client.post("/tdsms/admin/create", {"username": "newuser", "password": "123456", "validDays": 30}, format="json")
        self.assertEqual(created.status_code, 200, created.data)

    def test_single_login_logout_and_old_token_invalidation(self):
        first = self.client.post(
            "/tdsms/auth/login",
            {"username": "user01", "password": "123456"},
            format="json",
        )
        self.assertEqual(first.status_code, 200, first.data)
        token = first.data["data"]["token"]
        self.user.refresh_from_db()
        self.assertEqual(self.user.loginToken, token)

        repeated = self.client.post(
            "/tdsms/auth/login",
            {"username": "user01", "password": "123456"},
            format="json",
        )
        self.assertEqual(repeated.status_code, 409, repeated.data)
        self.assertEqual(repeated.data["message"], "当前账号已经登录，请勿重复登录")

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        logged_out = self.client.post("/tdsms/auth/logout", {}, format="json")
        self.assertEqual(logged_out.status_code, 200, logged_out.data)
        self.user.refresh_from_db()
        self.assertIsNone(self.user.loginToken)

        rejected = self.client.get("/tdsms/aps/listQuery")
        self.assertEqual(rejected.status_code, 403, rejected.data)

        self.client.credentials()
        logged_in_again = self.client.post(
            "/tdsms/auth/login",
            {"username": "user01", "password": "123456"},
            format="json",
        )
        self.assertEqual(logged_in_again.status_code, 200, logged_in_again.data)
        self.assertNotEqual(logged_in_again.data["data"]["token"], token)

    def test_expired_stored_token_is_replaced_on_login(self):
        expired_token = jwt.encode(
            {
                "userId": self.user.userId,
                "username": self.user.username,
                "exp": int((timezone.now() - timedelta(minutes=1)).timestamp()),
            },
            settings.JWT_SECRET_KEY,
            algorithm=settings.JWT_ALGORITHM,
        )
        self.user.loginToken = expired_token
        self.user.save(update_fields=["loginToken"])

        response = self.client.post(
            "/tdsms/auth/login",
            {"username": "user01", "password": "123456"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.user.refresh_from_db()
        self.assertNotEqual(self.user.loginToken, expired_token)

    def test_disabling_user_clears_login_token_immediately(self):
        user_client = APIClient()
        login = user_client.post(
            "/tdsms/auth/login",
            {"username": "user01", "password": "123456"},
            format="json",
        )
        self.assertEqual(login.status_code, 200, login.data)
        user_token = login.data["data"]["token"]

        self.login("admin", "admin123")
        disabled = self.client.post(
            "/tdsms/admin/statusUpdate",
            {"userId": self.user.userId, "status": 0},
            format="json",
        )
        self.assertEqual(disabled.status_code, 200, disabled.data)
        self.user.refresh_from_db()
        self.assertEqual(self.user.status, 0)
        self.assertIsNone(self.user.loginToken)

        user_client.credentials(HTTP_AUTHORIZATION=f"Bearer {user_token}")
        rejected = user_client.get("/tdsms/aps/listQuery")
        self.assertEqual(rejected.status_code, 403, rejected.data)

    def test_template_downloads(self):
        self.login()
        cases = [
            ("/tdsms/aps/template", "APS排产信息模板.xlsx"),
            ("/tdsms/task/template", "药业车间分解编排计划表模板.xlsx"),
        ]
        for url, filename in cases:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertIn(filename, unquote(response.headers["Content-Disposition"]))
            self.assertGreater(sum(len(chunk) for chunk in response.streaming_content), 0)

    def test_aps_and_plan_import_history_reuse_and_isolation(self):
        task = self.create_task()
        self.assertGreater(task.file.items.count(), 700)
        self.assertTrue(Path(task.file.filePath).is_file())
        history = self.client.get("/tdsms/task/historyQuery", {"page": 1, "pageSize": 10})
        self.assertEqual(history.status_code, 200)
        self.assertEqual(history.data["data"]["total"], 1)
        reused = self.client.post("/tdsms/tasks/historyImport", {"taskId": task.taskId}, format="json")
        self.assertEqual(reused.status_code, 200, reused.data)
        self.assertEqual(reused.data["data"]["sourceType"], 2)
        self.client.credentials()
        self.login("user02", "123456")
        hidden = self.client.get("/tdsms/task/detailQuery", {"importId": task.taskId})
        self.assertEqual(hidden.status_code, 404)

    def test_plan_detail_json_filters_and_single_filter_option_query(self):
        task = self.create_task()
        task.file.items.update(isDeleted=1)
        first = UploadFileItem.objects.create(
            file=task.file,
            departmentName="302车间",
            materialCode="MAT-001",
            inventoryName="阿司匹林片",
            specification="100mg*24片",
            monthlyProductionPlan=100,
        )
        UploadFileItem.objects.create(
            file=task.file,
            departmentName="303车间",
            materialCode="MAT-002",
            inventoryName="维生素C片",
            specification="100mg*100片",
            monthlyProductionPlan=200,
        )

        payload = {
            "taskId": task.taskId,
            "keyword": "MAT",
            "departmentNames": ["302车间", "303车间"],
            "monthlyProductionPlans": [100, 200],
            "inventoryNames": ["阿司匹林片"],
            "page": 1,
            "pageSize": 10,
        }
        response = self.client.generic(
            "GET",
            "/tdsms/task/detailQuery",
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["total"], 1)
        self.assertEqual(response.data["data"]["records"][0]["itemId"], first.itemId)

        options = self.client.get(
            "/tdsms/task/detailFilterOptions",
            {"taskId": task.taskId, "option": "departmentNames"},
        )
        self.assertEqual(options.status_code, 200, options.data)
        self.assertEqual(options.data["data"], ["302车间", "303车间"])

        invalid = self.client.get(
            "/tdsms/task/detailFilterOptions",
            {"taskId": task.taskId, "option": "unknown"},
        )
        self.assertEqual(invalid.status_code, 400)

    def test_aps_item_create_batch_delete_and_export(self):
        archive = self.create_archive()
        create_payload = {
            "archiveId": archive.archiveId,
            "productName": "接口测试品种",
            "packageSpecification": "10mg×10片",
            "mixingLine": "配料线A",
            "mixingBatchQuantity": 50,
            "mixingWorkerCount": 3,
            "centralizedProcurement": 1,
            "annualSales": 1200,
        }
        created = self.client.post("/tdsms/aps/itemCreate", create_payload, format="json")
        self.assertEqual(created.status_code, 200, created.data)
        self.assertEqual(created.data["data"]["productName"], "接口测试品种")
        self.assertEqual(created.data["data"]["centralizedProcurement"], 1)

        second = dict(create_payload)
        second["packageSpecification"] = "20mg×10片"
        created_second = self.client.post("/tdsms/aps/itemCreate", second, format="json")
        self.assertEqual(created_second.status_code, 200, created_second.data)

        deleted = self.client.post("/tdsms/aps/itemDelete", {
            "archiveId": archive.archiveId,
            "productNames": ["接口测试品种", "接口测试品种", "不存在品种"],
        }, format="json")
        self.assertEqual(deleted.status_code, 200, deleted.data)
        self.assertEqual(deleted.data["data"]["deletedProductCount"], 1)
        self.assertEqual(deleted.data["data"]["deletedItemCount"], 2)
        self.assertEqual(deleted.data["data"]["productNames"], ["接口测试品种"])

        exported = self.client.get("/tdsms/aps/export", {"archiveId": archive.archiveId})
        self.assertEqual(exported.status_code, 200)
        self.assertEqual(
            exported.headers["Content-Type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        content = b"".join(exported.streaming_content)
        workbook = load_workbook(BytesIO(content), read_only=True, data_only=True)
        self.assertEqual(workbook.worksheets[0].max_column, 22)
        expected_count = archive.items.filter(isDeleted=0).count()
        self.assertEqual(workbook.worksheets[0].max_row, expected_count + 2)

        uploaded = SimpleUploadedFile("export.xlsx", content)
        parsed_rows = parse_aps_file(uploaded)
        self.assertEqual(len(parsed_rows), expected_count)
        self.assertNotIn("接口测试品种", {row["productName"] for row in parsed_rows})

        self.client.credentials()
        self.login("user02", "123456")
        hidden = self.client.get("/tdsms/aps/export", {"archiveId": archive.archiveId})
        self.assertEqual(hidden.status_code, 404)

    @patch("api.views.solve_views.stop_solver")
    @patch("api.views.solve_views.submit_solver")
    def test_solve_lifecycle_and_txt_logs(self, submit_solver_mock, stop_solver_mock):
        task = self.create_task()
        department = self.solvable_department(task)
        payload = {
            "taskId": task.taskId,
            "department": department,
            "scheduleMonth": "2025-06",
            "unmatchedItemPolicy": "SKIP",
            "productionRules": {
                "continuousRunLimitDays": 5.5,
                "cleaningDuration": {"majorCleaningDays": 0.5, "minorCleaningDays": 0.25, "periodicCleaningDays": 0.5},
                "shiftConversion": {"naturalDays": 1, "shiftCount": 2},
            },
            "personnelCapacity": {
                "mixing": 3,
                "tableting": 2,
                "coating": 2,
                "packaging": 4,
            },
            "solverTimeLimitMinutes": 20,
        }
        invalid_department = self.client.post(
            "/tdsms/solve/start",
            {**payload, "department": "不存在车间"},
            format="json",
        )
        self.assertEqual(invalid_department.status_code, 400, invalid_department.data)
        started = self.client.post("/tdsms/solve/start", payload, format="json")
        self.assertEqual(started.status_code, 202, started.data)
        solve_id = started.data["data"]["solveTaskId"]
        submit_solver_mock.assert_called_once_with(solve_id)
        self.assertEqual(
            SolveTask.objects.get(solveTaskId=solve_id).inputParams["personnelCapacity"],
            {"mixing": 3, "tableting": 2, "coating": 2, "packaging": 4},
        )
        self.assertEqual(
            SolveTask.objects.get(solveTaskId=solve_id).inputParams["department"],
            department,
        )
        append_log(solve_id, "正在计算搜索...")
        logs = self.client.get("/tdsms/solve/logs", {"solveTaskId": solve_id})
        self.assertIn(
            "正在计算搜索...",
            [record["logContent"] for record in logs.data["data"]],
        )
        status_response = self.client.get("/tdsms/solve/query", {"solveTaskId": solve_id})
        self.assertEqual(status_response.data["data"]["solveStatus"], 0)
        stop_solver_mock.return_value = {"taskId": solve_id, "status": "STOPPING"}
        stopped = self.client.post("/tdsms/solve/stop", {"solveTaskId": solve_id}, format="json")
        self.assertEqual(stopped.status_code, 202)
        self.assertTrue(stopped.data["data"]["stopRequested"])
        self.assertEqual(SolveTask.objects.get(solveTaskId=solve_id).solveStatus, 0)

    def test_algorithm_adapter_prepares_inputs_and_maps_solver_parameters(self):
        from algorithm.adapter import submit_solver

        task = self.create_task()
        department = self.solvable_department(task)
        solve = SolveTask.objects.create(
            task=task,
            createdUser=self.user,
            inputParams={
                "department": department,
                "scheduleMonth": "2025-06",
                "unmatchedItemPolicy": "SKIP",
                "productionRules": {
                    "continuousRunLimitDays": 5.5,
                    "cleaningDuration": {
                        "majorCleaningDays": 0.5,
                        "minorCleaningDays": 0.25,
                        "periodicCleaningDays": 0.5,
                    },
                    "shiftConversion": {"naturalDays": 1, "shiftCount": 2},
                },
                "personnelCapacity": {
                    "mixing": 3,
                    "tableting": 2,
                    "coating": 2,
                    "packaging": 4,
                },
                "solverTimeLimitMinutes": 20,
            },
        )
        engine = Mock()
        engine.run_solver.return_value = {"taskId": str(solve.solveTaskId), "status": "RUNNING"}
        engine.query_solver_status.return_value = {
            "taskId": str(solve.solveTaskId),
            "status": "RUNNING",
            "startedAt": "2026-08-16T12:00:00",
            "finishedAt": None,
            "resultFiles": {},
        }
        with patch("algorithm.adapter._engine_service", return_value=engine), patch(
            "algorithm.adapter._start_monitor"
        ):
            result = submit_solver(solve.solveTaskId)

        self.assertEqual(result["taskId"], str(solve.solveTaskId))
        arguments = engine.run_solver.call_args.kwargs
        self.assertEqual(arguments["taskId"], solve.solveTaskId)
        self.assertEqual(arguments["planFile"], task.file.filePath)
        self.assertEqual(arguments["department"], department)
        self.assertEqual(arguments["mixing"], 3)
        self.assertTrue(Path(arguments["apsFile"]).is_file())
        workbook = load_workbook(arguments["apsFile"], read_only=True, data_only=True)
        self.assertGreater(workbook.worksheets[0].max_row, 2)
        workbook.close()
        solve.refresh_from_db()
        self.assertEqual(solve.solveStatus, 1)

        run_dir = Path(settings.ALGORITHM_RUN_ROOT) / str(solve.solveTaskId)
        run_dir.mkdir(parents=True, exist_ok=True)
        visual = run_dir / "可排产结果可视化.xlsx"
        visual.unlink(missing_ok=True)
        visual.write_bytes(b"result")
        intermediate = run_dir / "排产结果明细.xlsx"
        intermediate.write_bytes(b"temporary")
        engine.query_solver_status.return_value = {
            "taskId": str(solve.solveTaskId),
            "status": "SUCCESS",
            "startedAt": "2026-08-16T12:00:00",
            "finishedAt": "2026-08-16T12:20:00",
            "result": {"cp_status": "OPTIMAL"},
            "resultFiles": {
                "rawDetail": str(intermediate),
                "visualBoard": str(visual),
            },
        }
        with patch("algorithm.adapter._engine_service", return_value=engine):
            from algorithm.adapter import sync_solve_task

            sync_solve_task(solve)

        solve.refresh_from_db()
        self.assertEqual(solve.solveStatus, 2)
        self.assertEqual(solve.finishReason, 1)
        self.assertEqual(solve.resultFilePath, str(visual))
        self.assertTrue(visual.is_file())
        self.assertFalse(intermediate.exists())
        self.assertFalse(Path(arguments["apsFile"]).exists())

    def test_solve_match_check_applies_mappings_and_reports_unmatched_items(self):
        task = self.create_task()
        task.file.items.update(isDeleted=1)
        task.apsArchive.items.update(isDeleted=1)
        plan_rows = [
            {
                "departmentName": "210车间",
                "materialCode": "M-001",
                "inventoryName": "阿司匹林肠溶片（过评）",
                "specification": "47.5mg×14粒×2板×400盒",
                "monthlyProductionPlan": 100,
            },
            {
                "departmentName": "210车间",
                "materialCode": "M-002",
                "inventoryName": "阿德福韦酯片",
                "specification": "10mg×28片×100瓶",
                "monthlyProductionPlan": 200,
            },
            {
                "departmentName": "210车间",
                "materialCode": "M-003",
                "inventoryName": "未维护药品",
                "specification": "20mg×10片",
                "monthlyProductionPlan": 300,
            },
            {
                "departmentName": "210车间",
                "materialCode": "M-004",
                "inventoryName": "零计划药品",
                "specification": "1mg×10片",
                "monthlyProductionPlan": 0,
            },
            {
                "departmentName": "302车间",
                "materialCode": "M-005",
                "inventoryName": "阿德福韦酯片",
                "specification": "10mg×28片×100瓶",
                "monthlyProductionPlan": 50,
            },
            {
                "departmentName": "303车间",
                "materialCode": "M-006",
                "inventoryName": "完全未匹配药品",
                "specification": "30mg×10片",
                "monthlyProductionPlan": 60,
            },
        ]
        UploadFileItem.objects.bulk_create([
            UploadFileItem(file=task.file, **row) for row in plan_rows
        ])
        ApsArchiveItem.objects.bulk_create([
            ApsArchiveItem(
                archive=task.apsArchive,
                productName="阿司匹林肠溶片100mg",
                packageSpecification="47.5mg×14片×2板×400盒",
            ),
            ApsArchiveItem(
                archive=task.apsArchive,
                productName="阿德福韦酯片",
                packageSpecification="10mg×28片×100瓶",
            ),
        ])

        response = self.client.post(
            "/tdsms/solve/matchCheck",
            {"taskId": task.taskId},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        result = response.data["data"]
        self.assertFalse(result["canStartSolve"])
        self.assertEqual(result["totalCount"], 5)
        self.assertEqual(result["matchedCount"], 3)
        self.assertEqual(result["unmatchedCount"], 2)
        self.assertEqual(result["departmentCount"], 3)
        statistics = {
            record["departmentName"]: record
            for record in result["departmentStatistics"]
        }
        self.assertEqual(statistics["210车间"]["totalCount"], 3)
        self.assertEqual(statistics["302车间"]["matchedCount"], 1)
        self.assertEqual(statistics["303车间"]["unmatchedCount"], 1)
        mapped = next(
            record for record in result["matchedRecords"]
            if record["inventoryName"] == "阿司匹林肠溶片（过评）"
        )
        self.assertEqual(mapped["apsProductName"], "阿司匹林肠溶片100mg")
        self.assertEqual(mapped["apsPackageSpecification"], "47.5mg×14片×2板×400盒")
        self.assertTrue(mapped["nameMapped"])
        self.assertTrue(mapped["specificationMapped"])
        self.assertEqual(result["unmatchedRecords"][0]["inventoryName"], "未维护药品")

        start_payload = {
            "taskId": task.taskId,
            "department": "210车间",
            "scheduleMonth": "2025-06",
            "productionRules": {
                "continuousRunLimitDays": 5.5,
                "cleaningDuration": {
                    "majorCleaningDays": 0.5,
                    "minorCleaningDays": 0.25,
                    "periodicCleaningDays": 0.5,
                },
                "shiftConversion": {"naturalDays": 1, "shiftCount": 2},
            },
            "personnelCapacity": {
                "mixing": 3,
                "tableting": 2,
                "coating": 2,
                "packaging": 4,
            },
            "solverTimeLimitMinutes": 20,
        }
        blocked = self.client.post("/tdsms/solve/start", start_payload, format="json")
        self.assertEqual(blocked.status_code, 409, blocked.data)
        self.assertEqual(blocked.data["data"]["matchedCount"], 2)
        self.assertEqual(blocked.data["data"]["unmatchedCount"], 1)

        with patch("api.views.solve_views.submit_solver"):
            skipped = self.client.post(
                "/tdsms/solve/start",
                {**start_payload, "unmatchedItemPolicy": "SKIP"},
                format="json",
            )
        self.assertEqual(skipped.status_code, 202, skipped.data)
        self.assertEqual(skipped.data["data"]["matchedCount"], 2)
        self.assertEqual(skipped.data["data"]["skippedCount"], 1)
        solve = SolveTask.objects.get(solveTaskId=skipped.data["data"]["solveTaskId"])
        self.assertEqual(solve.inputParams["unmatchedItemPolicy"], "SKIP")
        self.assertEqual(solve.inputParams["skippedCount"], 1)
        solve.solveStatus = 2
        solve.save(update_fields=["solveStatus"])

        all_missing = self.client.post(
            "/tdsms/solve/start",
            {
                **start_payload,
                "department": "303车间",
                "unmatchedItemPolicy": "SKIP",
            },
            format="json",
        )
        self.assertEqual(all_missing.status_code, 409, all_missing.data)
        self.assertEqual(all_missing.data["data"]["matchedCount"], 0)

        self.client.credentials()
        self.login("user02", "123456")
        hidden = self.client.post(
            "/tdsms/solve/matchCheck",
            {"taskId": task.taskId},
            format="json",
        )
        self.assertEqual(hidden.status_code, 404)

    def test_legacy_running_solve_without_algorithm_directory_can_be_stopped(self):
        from algorithm.adapter import stop_solver

        task = self.create_task()
        solve = SolveTask.objects.create(
            task=task,
            createdUser=self.user,
            inputParams={},
            solveStatus=1,
        )
        result = stop_solver(solve.solveTaskId)

        solve.refresh_from_db()
        self.assertEqual(result["status"], "STOPPED")
        self.assertTrue(result["orphaned"])
        self.assertEqual(solve.solveStatus, 4)
        self.assertEqual(solve.finishReason, 3)
        self.assertIsNotNone(solve.finishTime)

    def test_stopping_task_is_not_marked_stopped_until_result_postprocessing_finishes(self):
        from algorithm.adapter import sync_solve_task

        task = self.create_task()
        solve = SolveTask.objects.create(
            task=task,
            createdUser=self.user,
            inputParams={},
            solveStatus=1,
        )
        run_dir = Path(settings.ALGORITHM_RUN_ROOT) / str(solve.solveTaskId)
        run_dir.mkdir(parents=True, exist_ok=True)
        visual = run_dir / "可排产结果可视化.xlsx"
        visual.unlink(missing_ok=True)
        engine = Mock()
        engine.query_solver_status.return_value = {
            "taskId": str(solve.solveTaskId),
            "status": "STOPPING",
            "startedAt": "2026-08-17T12:00:00",
            "finishedAt": None,
            "stopRequested": True,
            "resultReady": False,
            "progressMessage": "收到停止请求，正在导出当前最优方案",
            "resultFiles": {"visualBoard": str(visual)},
        }

        with patch("algorithm.adapter._engine_service", return_value=engine):
            sync_solve_task(solve, cleanup=False)

        solve.refresh_from_db()
        self.assertEqual(solve.solveStatus, 1)
        self.assertIsNone(solve.finishReason)
        self.assertFalse(solve.partialResultFilePath)

        visual.write_bytes(b"result")
        engine.query_solver_status.return_value.update({
            "status": "STOPPED",
            "finishedAt": "2026-08-17T12:00:05",
            "resultReady": True,
            "progressMessage": "已停止求解，当前最优结果已生成",
        })
        with patch("algorithm.adapter._engine_service", return_value=engine):
            sync_solve_task(solve, cleanup=False)

        solve.refresh_from_db()
        self.assertEqual(solve.solveStatus, 4)
        self.assertEqual(solve.finishReason, 3)
        self.assertEqual(solve.partialResultFilePath, str(visual))

    def test_export_returns_conflict_while_stopped_result_is_being_generated(self):
        task = self.create_task()
        solve = SolveTask.objects.create(
            task=task,
            createdUser=self.user,
            inputParams={},
            solveStatus=1,
        )
        state = {
            "status": "STOPPING",
            "finishedAt": None,
            "stopRequested": True,
            "resultReady": False,
        }
        with patch("api.views.solve_views.sync_solve_task", return_value=(solve, state)):
            response = self.client.post(
                "/tdsms/solve/result",
                {"solveTaskId": solve.solveTaskId},
                format="json",
            )

        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.data["success"])
        self.assertIn("结果文件尚未生成", response.data["message"])

    @patch("api.views.solve_views.submit_solver")
    def test_each_user_can_only_run_one_solve_while_different_users_can_run_concurrently(
        self,
        submit_solver_mock,
    ):
        task = self.create_task()
        department = self.solvable_department(task)

        def payload(task_id):
            return {
                "taskId": task_id,
                "department": department,
                "scheduleMonth": "2025-06",
                "unmatchedItemPolicy": "SKIP",
                "productionRules": {
                    "continuousRunLimitDays": 5.5,
                    "cleaningDuration": {
                        "majorCleaningDays": 0.5,
                        "minorCleaningDays": 0.25,
                        "periodicCleaningDays": 0.5,
                    },
                    "shiftConversion": {"naturalDays": 1, "shiftCount": 2},
                },
                "personnelCapacity": {
                    "mixing": 3,
                    "tableting": 2,
                    "coating": 2,
                    "packaging": 4,
                },
                "solverTimeLimitMinutes": 20,
            }

        first = self.client.post("/tdsms/solve/start", payload(task.taskId), format="json")
        self.assertEqual(first.status_code, 202, first.data)
        first_solve_id = first.data["data"]["solveTaskId"]

        another_task = TaskImportRecord.objects.create(
            sourceType=2,
            sourceTask=task,
            apsArchive=task.apsArchive,
            file=task.file,
            importStatus=1,
            createdBy=self.user,
        )
        blocked = self.client.post(
            "/tdsms/solve/start",
            payload(another_task.taskId),
            format="json",
        )
        self.assertEqual(blocked.status_code, 409, blocked.data)
        self.assertEqual(blocked.data["data"]["solveTaskId"], first_solve_id)
        self.assertEqual(
            blocked.data["message"],
            "您还有模型计算正在运行，无法提交新的求解任务",
        )

        SolveTask.objects.filter(solveTaskId=first_solve_id).update(solveStatus=2)
        resumed = self.client.post(
            "/tdsms/solve/start",
            payload(another_task.taskId),
            format="json",
        )
        self.assertEqual(resumed.status_code, 202, resumed.data)

        other_task = TaskImportRecord.objects.create(
            sourceType=2,
            sourceTask=task,
            apsArchive=task.apsArchive,
            file=task.file,
            importStatus=1,
            createdBy=self.other,
        )
        self.client.credentials()
        self.login("user02", "123456")
        other_started = self.client.post(
            "/tdsms/solve/start",
            payload(other_task.taskId),
            format="json",
        )
        self.assertEqual(other_started.status_code, 202, other_started.data)
        self.assertEqual(submit_solver_mock.call_count, 3)
