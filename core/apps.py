from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"

    def ready(self):
        # 不在应用初始化阶段访问数据库；求解状态会在查询接口中按任务同步。
        from services import algorithm_client

        algorithm_client.register_process_exit_hooks()
