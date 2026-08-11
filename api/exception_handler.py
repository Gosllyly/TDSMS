"""
全局异常处理器
=============
所有 DRF API 的异常都会经过此处理器，确保：
1. 任何异常都返回统一的 JSON 格式，前端永远不会收到 HTML 错误页面
2. 未捕获的异常会被记录到日志，便于排查
3. 生产环境不会向前端暴露内部错误细节

在 settings.py 中注册：
    REST_FRAMEWORK = {
        'EXCEPTION_HANDLER': 'api.exception_handler.custom_exception_handler',
    }
"""

import logging
import traceback

from rest_framework.views import exception_handler
from rest_framework.response import Response
from rest_framework import status

logger = logging.getLogger('django')


def custom_exception_handler(exc, context):
    """
    DRF 全局异常处理入口。

    处理流程：
    1. 先调用 DRF 默认的 exception_handler，它能处理大部分标准异常
       (如 ValidationError, AuthenticationFailed, NotFound, PermissionDenied 等)
    2. 如果 DRF 默认处理器返回了 response，说明是已知异常，统一格式后返回
    3. 如果返回 None，说明是未被 DRF 识别的异常 (如数据库崩溃、代码 bug)，
       记录错误日志，返回通用 500 错误信息
    """

    # ====== 第一步：尝试 DRF 默认处理 ======
    response = exception_handler(exc, context)

    if response is not None:
        # DRF 已识别的异常 (400/401/403/404/405/429 等)
        # 统一封装为 { success, code, message, data } 格式
        response.data = {
            'success': False,
            'code': response.status_code,
            'message': _translate_message(_extract_message(response.data)),
            'data': None
        }
        return response

    # ====== 第二步：未捕获的异常（严重错误）======
    # 获取出错的 View 名称，便于日志定位
    view = context.get('view', None)
    view_name = view.__class__.__name__ if view else 'UnknownView'
    request = context.get('request', None)
    request_path = request.path if request else 'Unknown'

    # 记录完整的堆栈信息到错误日志
    logger.error(
        f"[未捕获异常] View={view_name}, Path={request_path}, "
        f"Exception={exc.__class__.__name__}: {exc}\n"
        f"{traceback.format_exc()}"
    )

    # 返回通用错误信息（不暴露内部细节）
    return Response(
        {
            'success': False,
            'code': 500,
            'message': '服务器内部错误，请联系管理员',
            'data': None
        },
        status=status.HTTP_500_INTERNAL_SERVER_ERROR
    )


def _extract_message(data):
    """
    从 DRF 默认错误响应数据中提取人类可读的错误消息。

    DRF 的错误格式不统一，可能是：
    - dict: {"field_name": ["error1", "error2"]}
    - list: ["error1", "error2"]
    - str:  "error message"
    
    本函数将它们统一转为单个字符串。
    """
    if isinstance(data, dict):
        messages = []
        for key, value in data.items():
            if key == 'detail':
                # DRF 标准错误字段，直接取值
                return str(value)
            if isinstance(value, list):
                messages.append(f"{key}: {', '.join(str(v) for v in value)}")
            else:
                messages.append(f"{key}: {value}")
        return '; '.join(messages) if messages else '请求错误'

    if isinstance(data, list):
        return '; '.join(str(item) for item in data)

    return str(data)


def _translate_message(message):
    """
    将 DRF / Django / 第三方库抛出的常见英文错误统一翻译成中文。

    重点覆盖：
    - JSON 解析失败
    - 常见认证/权限/方法错误
    - 404 / 405 / 节流等默认英文文案
    """
    text = str(message or '').strip()
    if not text:
        return '请求错误'

    exact_map = {
        'Authentication credentials were not provided.': '未提供认证信息',
        'Invalid token.': '登录状态无效，请重新登录',
        'Invalid token header.': '认证信息格式错误',
        'User inactive or deleted.': '用户已停用或已删除',
        'You do not have permission to perform this action.': '没有权限执行此操作',
        'Not found.': '未找到对应资源',
        'Method "GET" not allowed.': '不允许使用 GET 请求',
        'Method "POST" not allowed.': '不允许使用 POST 请求',
        'Method "PUT" not allowed.': '不允许使用 PUT 请求',
        'Method "PATCH" not allowed.': '不允许使用 PATCH 请求',
        'Method "DELETE" not allowed.': '不允许使用 DELETE 请求',
        'Unsupported media type "application/json" in request.': '不支持当前请求内容类型',
        'Request was throttled.': '请求过于频繁，请稍后再试',
    }
    if text in exact_map:
        return exact_map[text]

    if text.startswith('JSON parse error'):
        return '请求体 JSON 格式错误，请检查双引号、逗号和字段名格式'
    if text.startswith('Parse error'):
        return '请求体解析失败，请检查请求内容格式'
    if text.startswith('Unsupported media type'):
        return '不支持当前请求内容类型'
    if text.startswith('Method "') and text.endswith('" not allowed.'):
        method = text.split('"')[1]
        return '不允许使用 %s 请求' % method
    if text.startswith('Could not satisfy the request Accept header.'):
        return '不支持当前响应格式'
    if text.startswith('Request was throttled'):
        return '请求过于频繁，请稍后再试'

    return text
