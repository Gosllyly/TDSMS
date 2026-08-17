from datetime import timedelta
from math import ceil

from django.contrib.auth.hashers import check_password, make_password
from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import AuthenticationFailed, NotFound, ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from api.authentication import build_jwt_for_user, is_valid_login_token
from api.permissions import IsAdmin
from api.serializers import AdminCreateSerializer, AdminExpireSerializer, AdminStatusSerializer, LoginSerializer
from api.utils import ApiResponse, get_request_params
from api.views.common import format_datetime, paginate
from core.models import SysUser


def user_data(user, include_profile=True):
    data = {
        "userId": user.userId, "username": user.username, "status": user.status,
        "expireTime": format_datetime(user.expireTime), "createTime": format_datetime(user.createTime),
    }
    if include_profile:
        data.update({
            "role": user.role, "realName": user.realName, "departmentName": user.departmentName,
            "lastLoginTime": format_datetime(user.lastLoginTime), "updateTime": format_datetime(user.updateTime),
        })
    if user.expireTime:
        data["remainingDays"] = max(0, ceil((user.expireTime - timezone.now()).total_seconds() / 86400))
    else:
        data["remainingDays"] = None
    return data


class LoginView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            user = SysUser.objects.select_for_update().filter(
                username=serializer.validated_data["username"],
                isDeleted=0,
            ).first()
            if not user or not check_password(serializer.validated_data["password"], user.password):
                raise AuthenticationFailed("账户或密码错误")
            if user.status != 1:
                return ApiResponse(None, status.HTTP_403_FORBIDDEN, "该账户已被禁用，请联系管理员")
            if user.expireTime and user.expireTime < timezone.now():
                return ApiResponse(None, status.HTTP_403_FORBIDDEN, "当前账户已超过有效期，无法登录")
            if is_valid_login_token(user, user.loginToken):
                return ApiResponse(
                    status=status.HTTP_409_CONFLICT,
                    message="当前账号已经登录，请勿重复登录",
                )

            token = build_jwt_for_user(user)
            user.loginToken = token
            user.lastLoginTime = timezone.now()
            user.save(update_fields=["loginToken", "lastLoginTime", "updateTime"])
            return ApiResponse({"token": token, "userInfo": user_data(user)}, message="登录成功")


class LogoutView(APIView):
    def post(self, request):
        with transaction.atomic():
            user = SysUser.objects.select_for_update().get(userId=request.user.userId)
            if user.loginToken == request.auth:
                user.loginToken = None
                user.save(update_fields=["loginToken", "updateTime"])
        return ApiResponse(message="退出登录成功")


class AdminCreateView(APIView):
    permission_classes = [IsAdmin]

    def post(self, request):
        serializer = AdminCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if SysUser.objects.filter(username=data["username"], isDeleted=0).exists():
            raise ValidationError("用户账号已存在")
        user = SysUser.objects.create(
            username=data["username"], password=make_password(data["password"]), role="user",
            realName=data.get("realName") or None, departmentName=data.get("departmentName") or None,
            expireTime=timezone.now() + timedelta(days=data["validDays"]),
        )
        return ApiResponse(user_data(user), message="用户创建成功")


class AdminQueryView(APIView):
    permission_classes = [IsAdmin]

    def get(self, request):
        params = get_request_params(request)
        queryset = SysUser.objects.filter(role="user", isDeleted=0).order_by("-createTime")
        total, records, page, page_size = paginate(queryset, params.get("page"), params.get("pageSize"))
        return ApiResponse({"total": total, "page": page, "pageSize": page_size, "records": [user_data(x, False) for x in records]}, message="查询成功")


class AdminExpireUpdateView(APIView):
    permission_classes = [IsAdmin]

    def post(self, request):
        serializer = AdminExpireSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = SysUser.objects.filter(userId=serializer.validated_data["userId"], role="user", isDeleted=0).first()
        if not user:
            raise NotFound("用户不存在或已删除")
        user.expireTime = timezone.now() + timedelta(days=serializer.validated_data["validDays"])
        user.save(update_fields=["expireTime", "updateTime"])
        return ApiResponse(user_data(user, False), message="有效期更新成功")


class AdminStatusUpdateView(APIView):
    permission_classes = [IsAdmin]

    def post(self, request):
        serializer = AdminStatusSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = SysUser.objects.filter(userId=serializer.validated_data["userId"], role="user", isDeleted=0).first()
        if not user:
            raise NotFound("用户不存在或已删除")
        user.status = serializer.validated_data["status"]
        update_fields = ["status", "updateTime"]
        if user.status == 0:
            user.loginToken = None
            update_fields.append("loginToken")
        user.save(update_fields=update_fields)
        return ApiResponse(user_data(user, False), message="用户状态更新成功")
