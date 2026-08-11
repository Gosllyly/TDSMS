"""
API 权限控制
基于 Django REST Framework 的权限系统
"""
# from rest_framework import permissions


# class IsAdmin(permissions.BasePermission):
#     """
#     仅管理员可访问。
#     """
#     message = "只有管理员可以使用测试用户管理模块"

#     def has_permission(self, request, view):
#         user = getattr(request, "user", None)
#         return bool(user and getattr(user, "is_authenticated", False) and getattr(user, "isAdmin", 0) == 1)

