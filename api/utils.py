from dataclasses import asdict, is_dataclass
from typing import Any, Optional
from urllib.parse import quote

from django.http import FileResponse
from rest_framework.response import Response
from rest_framework import status as drf_status


def attachment_content_disposition(filename: str) -> str:
    """
    同时提供 filename 与 filename*，保证中文下载名正确。
    - filename= 使用百分号编码（无 utf-8'' 前缀），避免出现「utf-8药业...」
    - filename*=UTF-8''... 供标准浏览器解析出原始中文名
    """
    encoded = quote(filename, safe="")
    return f"attachment; filename=\"{encoded}\"; filename*=UTF-8''{encoded}"


def attachment_file_response(fileobj, filename: str, content_type: Optional[str] = None) -> FileResponse:
    """返回带兼容中文文件名的附件下载响应。"""
    kwargs = {"as_attachment": False}
    if content_type:
        kwargs["content_type"] = content_type
    response = FileResponse(fileobj, **kwargs)
    # 不传 filename 给 FileResponse，避免 Django 覆盖 Content-Disposition
    response["Content-Disposition"] = attachment_content_disposition(filename)
    return response


def ApiResponse(
    data: Any = None, 
    status: int = drf_status.HTTP_200_OK,
    message: str = "", 
    code: Optional[int] = None,
    success: Optional[bool] = None
) -> Response:
    """
    通用 API 响应辅助函数
    
    统一响应格式:
    {
        "success": bool,
        "code": int,
        "message": str,
        "data": any
    }
    """
    
    # 1. 自动推导 success
    if success is None:
        # 2xx 状态码默认为成功，其他为失败
        success = status >= 200 and status < 300
        
    # 2. 自动推导 code
    if code is None:
        code = status
        
    # 3. 自动推导 message (如果未提供)
    if not message:
        if success:
            message = "success"
        else:
            # 尝试从 data 中提取错误信息
            if isinstance(data, dict) and 'error' in data:
                message = str(data['error'])
                # 如果 data 只包含 error，则不再返回 data
                if len(data) == 1:
                    data = None
            else:
                message = "failed"

    # 4. 处理 data (dataclass 序列化)
    final_data = None
    if data is not None:
        if isinstance(data, list):
            processed_list = []
            for item in data:
                if is_dataclass(item):
                    processed_list.append(asdict(item))
                else:
                    processed_list.append(item)
            final_data = processed_list
        elif is_dataclass(data):
            final_data = asdict(data)
        else:
            final_data = data

    # 5. 构造标准响应结构
    response_body = {
        'success': success,
        'code': code,
        'message': message,
        'data': final_data
    }
    
    return Response(response_body, status=status)


def get_request_params(request):
    data = {}
    data.update(request.query_params.dict())
    if isinstance(request.data, dict):
        data.update(request.data)
    return data
