"""
Django settings for tdsms project.
支持通过环境变量配置（Docker 部署时使用）
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# 自建任务上传文件根目录（可通过环境变量覆盖）位于项目的外层目录
TASK_UPLOAD_ROOT = os.getenv(
    'TASK_UPLOAD_ROOT',
    os.getenv('SELF_BUILD_UPLOAD_ROOT', os.path.join(BASE_DIR.parent, 'create_task_uploads')),
)
SELF_BUILD_UPLOAD_ROOT = TASK_UPLOAD_ROOT
RESULT_EXCEL_ROOT = os.getenv(
    'RESULT_EXCEL_ROOT',
    os.path.join(BASE_DIR.parent, 'results_excel'),
)

# ==========================================
# 从环境变量读取配置
# ==========================================
SECRET_KEY = os.getenv('DJANGO_SECRET_KEY', os.getenv('SECRET_KEY', 'django-insecure-change-this-in-production'))
DEBUG = os.getenv('DJANGO_DEBUG', 'true').lower() in {'1', 'true', 'yes', 'on'}

ALLOWED_HOSTS = os.getenv('ALLOWED_HOSTS', '*').split(',')

# 静态文件目录（Docker 部署时使用）
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')

# 请求体内存限制：
# None = 不启用 Django 这一层的请求体大小限制
DATA_UPLOAD_MAX_MEMORY_SIZE = None

# Application definition
INSTALLED_APPS = [
    'django.contrib.admin',#内置的后台管理系统（/admin/）
    'django.contrib.auth',#内置的认证系统（用户、组、权限）。权限系统的地基。
    'django.contrib.contenttypes',#Django 内部用来追踪所有 models 的系统（auth 依赖它）。
    'django.contrib.sessions',#Session 会话系统（admin 依赖它）
    'django.contrib.messages',#一次性消息通知系统（admin 依赖它）。
    'django.contrib.staticfiles',#管理 CSS/JS 文件的系统（admin 依赖它）。
    'rest_framework',
    'core',
    'api',
]
AUTHENTICATION_BACKENDS = [
    'django.contrib.auth.backends.ModelBackend',
]
MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

# 跨域配置 - 根据您的实际环境配置
CORS_ALLOWED_ORIGINS = [
    "http://192.168.3.72:8086",  # 前端访问地址1
    "http://192.168.3.61:8086",  # 前端访问地址2
    "http://60.205.199.162:7010",  # 服务器地址，待更新
]

# 允许的 HTTP 方法
CORS_ALLOW_METHODS = [
    "DELETE",
    "GET",
    "OPTIONS",
    "PATCH",
    "POST",
    "PUT",
]

# 允许的 headers
CORS_ALLOW_HEADERS = [
    "accept",
    "authorization",
    "content-type",
    "user-agent",
    "x-csrftoken",
    "x-requested-with",
]

# 允许携带凭证（cookies, authorization headers等）
CORS_ALLOW_CREDENTIALS = True

# 预检请求缓存时间（秒）
CORS_PREFLIGHT_MAX_AGE = 86400

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        # 在项目根目录下的 templates/ 目录中查找模板
        'DIRS': [os.path.join(BASE_DIR, 'templates')],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

# ==========================================
# Database - MySQL配置（支持环境变量）
# ==========================================
DB_ENGINE = os.getenv('DB_ENGINE', 'django.db.backends.mysql')
try:
    import pymysql

    pymysql.install_as_MySQLdb()
except Exception:
    pass

DATABASES = {
    'default': {
        'ENGINE': DB_ENGINE,
        'NAME': os.getenv('DB_NAME', 'tdsms'),
        'USER': os.getenv('DB_USER', 'root'),
        'PASSWORD': os.getenv('DB_PASSWORD', 'bjtu@8401A'),
        'HOST': os.getenv('DB_HOST', '60.205.199.162'),
        'PORT': os.getenv('DB_PORT', '3310'),

            # ===== 连接池与断线重连 =====
        'CONN_MAX_AGE': 600,

        'CONN_HEALTH_CHECKS': True,

        'OPTIONS': {
            'charset': 'utf8mb4',
            'init_command': "SET sql_mode='STRICT_TRANS_TABLES'",
            'connect_timeout': 10,
            # Excel 大批量导入使用 LOAD DATA LOCAL INFILE
            'local_infile': True,
        },
    }
}

# Internationalization
LANGUAGE_CODE = 'zh-hans'
TIME_ZONE = 'Asia/Shanghai'
USE_I18N = True
USE_TZ = False

# Static files (CSS, JavaScript, Images)
STATIC_URL = 'static/'
MEDIA_URL = '/media/'
MEDIA_ROOT = os.path.join(BASE_DIR, 'media')

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# REST Framework
REST_FRAMEWORK = {
    'DEFAULT_RENDERER_CLASSES': [
        'rest_framework.renderers.JSONRenderer',
    ],
    'DEFAULT_PARSER_CLASSES': [
        'rest_framework.parsers.JSONParser',
        'rest_framework.parsers.MultiPartParser',
        'rest_framework.parsers.FormParser',
    ],
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'api.authentication.Authentication',  # 使用自定义认证类
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    # ===== 全局异常处理器 =====
    # 确保所有 API 异常都返回统一 JSON 格式，前端永远不会收到 HTML 错误页
    # 实现位于 api/exception_handler.py
    'EXCEPTION_HANDLER': 'api.exception_handler.custom_exception_handler',
    'UNAUTHENTICATED_USER': None,
}

JWT_SECRET_KEY = os.getenv('JWT_SECRET_KEY', SECRET_KEY)
JWT_ALGORITHM = os.getenv('JWT_ALGORITHM', 'HS256')
JWT_EXPIRE_MINUTES = int(os.getenv('JWT_EXPIRE_MINUTES', str(24 * 60)))

USE_MOCK_ALGORITHM = os.getenv('USE_MOCK_ALGORITHM', 'false').lower() in {'1', 'true', 'yes', 'on'}
ALGORITHM_SERVICE_URL = os.getenv('ALGORITHM_SERVICE_URL', 'http://127.0.0.1:9000')
ALGORITHM_TIMEOUT_SECONDS = int(os.getenv('ALGORITHM_TIMEOUT_SECONDS', '30'))
# 同时进行中的求解子进程上限；默认等于 CPU 核数，可用环境变量覆盖
_MAX_CONCURRENT_SOLVES_ENV = os.getenv('MAX_CONCURRENT_SOLVES', '').strip()
MAX_CONCURRENT_SOLVES = (
    max(1, int(_MAX_CONCURRENT_SOLVES_ENV))
    if _MAX_CONCURRENT_SOLVES_ENV
    else max(1, os.cpu_count() or 1)
)

# ==========================================
# 日志配置
# ==========================================
# 设计原则：
#   - 开发环境 (DEBUG=True):  仅输出到控制台，方便调试
#   - 生产环境 (DEBUG=False): 同时输出到控制台 + 文件，文件按天自动轮转
#
# 日志文件说明：
#   - logs/app.log      : 所有 INFO 及以上级别的日志（正常业务记录）
#   - logs/error.log    : 仅 ERROR 及以上级别的日志（便于快速定位故障）
#   - 日志按天自动分割，app.log 保留 30 天，error.log 保留 90 天
#
# 确保项目根目录下存在 logs/ 文件夹（已通过 logs/.gitkeep 纳入版本管理）

# 日志目录
LOG_DIR = os.path.join(BASE_DIR, 'logs')
REFRESH_LOG_DIR = os.path.join(BASE_DIR.parent, 'refresh_logs')
os.makedirs(REFRESH_LOG_DIR, exist_ok=True)

# 根据 DEBUG 模式决定使用哪些 handler
# 开发环境：只用 console；生产环境：console + file + error_file
_LOG_HANDLERS = ['console'] if DEBUG else ['console', 'file', 'error_file']

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,

    # ===== 日志格式 =====
    'formatters': {
        # 详细格式：用于文件日志，包含时间、模块、行号，便于定位问题
        'verbose': {
            'format': '[{levelname}] {asctime} {module}:{lineno} | {message}',
            'style': '{',
        },
        # 简洁格式：用于控制台，开发时阅读方便
        'simple': {
            'format': '{levelname} {message}',
            'style': '{',
        },
        # 专用于 JSONL 审计日志（每行完整 JSON）
        'jsonl': {
            'format': '{message}',
            'style': '{',
        },
    },

    # ===== 日志处理器 =====
    'handlers': {
        # 控制台输出（开发和生产都使用）
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'simple',
        },

        # 全量日志文件（仅生产环境使用）
        # TimedRotatingFileHandler: 按时间自动轮转
        #   - when='midnight': 每天午夜分割出新文件
        #   - backupCount=30: 保留最近 30 天的日志文件
        #   - 旧日志自动命名为 app.log.2026-02-05 等
        'file': {
            'class': 'logging.handlers.TimedRotatingFileHandler',
            'filename': os.path.join(LOG_DIR, 'app.log'),
            'when': 'midnight',
            'backupCount': 30,
            'encoding': 'utf-8',
            'formatter': 'verbose',
        },

        # 错误日志文件（仅生产环境使用）
        # 只记录 ERROR 及以上级别，便于快速定位严重问题
        'error_file': {
            'class': 'logging.handlers.TimedRotatingFileHandler',
            'filename': os.path.join(LOG_DIR, 'error.log'),
            'when': 'midnight',
            'backupCount': 90,          # 错误日志保留更久（90天）
            'encoding': 'utf-8',
            'formatter': 'verbose',
            'level': 'ERROR',           # 此 handler 只接收 ERROR 及以上级别
        },
        # 刷新审计日志（JSONL），写入项目根目录上一级 refresh_logs
        'refresh_audit_file': {
            'class': 'logging.handlers.TimedRotatingFileHandler',
            'filename': os.path.join(REFRESH_LOG_DIR, 'refresh_audit.log'),
            'when': 'midnight',
            'backupCount': 180,
            'encoding': 'utf-8',
            'formatter': 'jsonl',
            'level': 'INFO',
        },
    },

    # ===== 根 Logger =====
    'root': {
        'handlers': _LOG_HANDLERS,
        'level': 'INFO',
    },

    # ===== 模块级 Logger =====
    'loggers': {
        # Django 框架日志
        'django': {
            'handlers': _LOG_HANDLERS,
            'level': os.getenv('DJANGO_LOG_LEVEL', 'INFO'),
            'propagate': False,
        },
        # Django 数据库查询日志
        # 为避免大量 SQL 日志刷屏，这里统一设为 WARNING，
        # 仅在出现数据库警告/错误时才输出，不再记录每一条 SQL。
        'django.db.backends': {
            'handlers': ['console'] if DEBUG else ['file'],
            'level': 'WARNING',
            'propagate': False,
        },
        # 定时刷新专用审计 logger（独立文件，不向 root 传播，避免格式混杂）
        'refresh_audit': {
            'handlers': ['refresh_audit_file'],
            'level': 'INFO',
            'propagate': False,
        },
    },
}
