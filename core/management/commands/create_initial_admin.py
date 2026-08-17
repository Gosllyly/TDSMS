from django.contrib.auth.hashers import make_password
from django.core.management.base import BaseCommand

from core.models import SysUser


class Command(BaseCommand):
    help = "创建或更新初始管理员"

    def add_arguments(self, parser):
        parser.add_argument("--username", default="admin")
        parser.add_argument("--password", default="admin123")

    def handle(self, *args, **options):
        user, created = SysUser.objects.update_or_create(
            username=options["username"],
            defaults={"password": make_password(options["password"]), "role": "admin", "status": 1, "isDeleted": 0},
        )
        action = "创建" if created else "更新"
        self.stdout.write(self.style.SUCCESS(f"已{action}管理员：{user.username}"))
