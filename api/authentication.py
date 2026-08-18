from datetime import timedelta
from uuid import uuid4

import jwt
from django.conf import settings
from django.utils import timezone
from rest_framework import authentication
from rest_framework.exceptions import AuthenticationFailed

from core.models import SysUser


class Authentication(authentication.BaseAuthentication):
    keyword = "Bearer"

    def authenticate(self, request):
        path = (request.path or "").rstrip("/")
        if path.endswith("/tdsms/auth/login"):
            return None

        auth_header = authentication.get_authorization_header(request).decode("utf-8")
        if not auth_header:
            raise AuthenticationFailed("登录状态已失效，请重新登录")
        parts = auth_header.split()
        if len(parts) != 2 or parts[0] != self.keyword:
            raise AuthenticationFailed("登录状态已失效，请重新登录")

        token = parts[1]
        try:
            payload = decode_jwt_token(token)
        except jwt.PyJWTError as exc:
            raise AuthenticationFailed("登录状态已失效，请重新登录") from exc

        user = SysUser.objects.filter(
            userId=payload.get("userId"),
            isDeleted=0,
            status=1,
        ).first()
        if not user:
            raise AuthenticationFailed("登录状态已失效，请重新登录")
        if user.expireTime and user.expireTime < timezone.now():
            raise AuthenticationFailed("登录状态已失效，请重新登录")
        # 数据库无 token：已登出或未登录
        if not user.loginToken:
            raise AuthenticationFailed("登录状态已失效，请重新登录")
        # 前端 token 与库中不一致：账号已在别处重新登录
        if user.loginToken != token:
            raise AuthenticationFailed("账号已被登陆，请重新登陆")
        return user, token


def decode_jwt_token(token):
    return jwt.decode(
        token,
        settings.JWT_SECRET_KEY,
        algorithms=[settings.JWT_ALGORITHM],
    )


def build_jwt_for_user(user):
    now = timezone.now()
    payload = {
        "userId": user.userId,
        "username": user.username,
        "jti": uuid4().hex,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.JWT_EXPIRE_MINUTES)).timestamp()),
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
