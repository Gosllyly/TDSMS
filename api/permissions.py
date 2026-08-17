"""
API 权限控制
基于 Django REST Framework 的权限系统
"""
from rest_framework import permissions


class IsAdmin(permissions.BasePermission):
    message = "只有管理员可以使用用户管理模块"

    def has_permission(self, request, view):
        return bool(getattr(request, "user", None) and request.user.role == "admin")

