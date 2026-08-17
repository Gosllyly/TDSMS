from datetime import datetime
from decimal import Decimal

from django.db.models import Q
from rest_framework.exceptions import NotFound, ValidationError


def format_datetime(value):
    return value.strftime("%Y-%m-%d %H:%M:%S") if value else None


def json_value(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return format_datetime(value)
    return value


def paginate(queryset, page, page_size):
    try:
        page, page_size = int(page or 1), int(page_size or 10)
    except (TypeError, ValueError) as exc:
        raise ValidationError("page和pageSize必须为整数") from exc
    if page < 1 or not 1 <= page_size <= 200:
        raise ValidationError("page必须大于0，pageSize必须在1到200之间")
    total = queryset.count()
    return total, queryset[(page - 1) * page_size: page * page_size], page, page_size


def owned_or_404(model, user, **filters):
    owner_field = "createdBy" if hasattr(model, "createdBy") else "createdUser"
    filters.update({owner_field: user, "isDeleted": 0})
    obj = model.objects.filter(**filters).first()
    if not obj:
        raise NotFound("数据不存在")
    return obj
