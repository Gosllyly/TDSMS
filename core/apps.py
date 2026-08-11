import os

from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"

    def ready(self):
        # Django runserver 热重载时父进程也会调用 ready，仅在实际工作进程执行回收
        if os.environ.get("RUN_MAIN") == "false":
            return
        try:
            from services import algorithm_client

            algorithm_client.register_process_exit_hooks()
            algorithm_client.recover_orphaned_running_solves()
        except Exception:
            # migrate / 首次启动库未就绪时忽略
            pass
