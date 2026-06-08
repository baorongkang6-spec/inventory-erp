"""
关联企业库存商品 ERP —— Django 配置。

设计原则（见 SPEC §10）：
- 涉及金额/库存的操作必须在数据库事务内完成 → 全局开启 ATOMIC_REQUESTS。
- 开发用 SQLite，生产切 PostgreSQL：通过环境变量切换，代码不改。
- 密钥、调试开关均走环境变量，仓库内只留安全的开发默认值。
"""

import datetime
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# 加载项目根 .env（存在则用；开发期可不建，走安全默认值）。生产由 .env 提供配置。
load_dotenv(BASE_DIR / ".env")

# 启用日（期初基准日）。SPEC §8.1：2026-06-01，可经环境变量覆盖。
OPENING_DATE = datetime.date.fromisoformat(os.environ.get("ERP_OPENING_DATE", "2026-06-01"))


def env_bool(key: str, default: bool) -> bool:
    val = os.environ.get(key)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


# --- 安全与调试 ---------------------------------------------------------------
# PRODUCTION：生产开关（生产 .env 置 1）。控制 HTTPS/安全 Cookie/HSTS 等加固，
# 与 DEBUG 解耦——因为 Django 跑测试时默认 DEBUG=False，若用 not DEBUG 当开关会把
# 测试请求 301 到 https 而全部失败。详见 docs/DEV_NOTES.md。
PRODUCTION = env_bool("DJANGO_PRODUCTION", False)

SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "django-insecure-dev-only-change-me-in-production-0_+swf7@id^$zd",
)
DEBUG = env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = [h.strip() for h in os.environ.get(
    "DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]
# CSRF 受信任来源（花生壳域名等），生产经 env 注入，含协议：https://xxx.example.com
CSRF_TRUSTED_ORIGINS = [o.strip() for o in os.environ.get(
    "DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()]

# 开发期（DEBUG 且非生产）：放开 ALLOWED_HOSTS，便于同一 WiFi 下手机用局域网 IP 直接访问。
# 生产仍严格（走 DJANGO_ALLOWED_HOSTS env）。
if DEBUG and not PRODUCTION:
    ALLOWED_HOSTS = ["*"]

# 生产 fail-fast：禁止用开发默认密钥 / 关 DEBUG
if PRODUCTION:
    from django.core.exceptions import ImproperlyConfigured

    if SECRET_KEY.startswith("django-insecure"):
        raise ImproperlyConfigured("生产环境必须设置 DJANGO_SECRET_KEY（不能用开发默认值）")
    if DEBUG:
        raise ImproperlyConfigured("生产环境必须 DJANGO_DEBUG=0")
    if not CSRF_TRUSTED_ORIGINS:
        raise ImproperlyConfigured("生产环境必须设置 DJANGO_CSRF_TRUSTED_ORIGINS（花生壳域名）")


# --- 应用 ---------------------------------------------------------------------
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # 业务应用
    "apps.core",
    "apps.accounts",
    "apps.masterdata",
    "apps.inventory",
    "apps.purchasing",
    "apps.sales",
    "apps.finance",
    "apps.opening",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # WhiteNoise：生产由 Waitress 直接服务静态文件（紧跟 SecurityMiddleware）
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.core.context_processors.company_scope",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"


# --- 数据库 -------------------------------------------------------------------
# 默认 SQLite（开发）。DB_ENGINE 设为 postgres/postgresql 时切到 PostgreSQL（生产）。
# 同时接受两种写法，避免「配了 postgres 却静默回退 SQLite」的上线事故（见 DEPLOY.md §4）。
if os.environ.get("DB_ENGINE", "sqlite").lower() in {"postgres", "postgresql"}:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("DB_NAME", "inventory_erp"),
            "USER": os.environ.get("DB_USER", "postgres"),
            "PASSWORD": os.environ.get("DB_PASSWORD", ""),
            "HOST": os.environ.get("DB_HOST", "127.0.0.1"),
            "PORT": os.environ.get("DB_PORT", "5432"),
            "ATOMIC_REQUESTS": True,
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
            "ATOMIC_REQUESTS": True,
        }
    }


# --- 认证 ---------------------------------------------------------------------
AUTH_USER_MODEL = "accounts.User"
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "home"
LOGOUT_REDIRECT_URL = "login"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# --- 国际化 / 本地化 ----------------------------------------------------------
LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "Asia/Shanghai"
USE_I18N = True
USE_TZ = True
# 数字显示千分位（金额/数量等只读展示分组为 1,234.56）。
# 表单数字输入框不分组（Django 对 localize=False 的字段不加分隔符），不影响录入。
# zh-hans 内置把 THOUSAND_SEPARATOR 设为空、分组为 4；用自定义格式模块覆盖为标准千分位。
USE_THOUSAND_SEPARATOR = True
FORMAT_MODULE_PATH = ["apps.formats"]


# --- 静态文件 -----------------------------------------------------------------
STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

# 生产用 WhiteNoise 压缩 + 带 hash 的 manifest 存储（须先 collectstatic）。
# 开发不启用 manifest，避免未 collect 时 {% static %} 报错。
if PRODUCTION:
    STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# --- 登录防爆破（自实现，SEC-2）----------------------------------------------
# 同一用户名+IP 连续失败达上限即锁定一段时间。用 Django 缓存计数（单机部署足够）。
LOGIN_FAILURE_LIMIT = int(os.environ.get("LOGIN_FAILURE_LIMIT", "5"))
LOGIN_LOCKOUT_SECONDS = int(os.environ.get("LOGIN_LOCKOUT_SECONDS", "900"))  # 15 分钟


# --- 生产安全加固（仅 PRODUCTION 生效，SEC-1）--------------------------------
# 经花生壳/反向代理暴露公网 + 财务数据，硬性要求（DEPLOY §2）。
if PRODUCTION:
    SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", True)
    # 反向代理（花生壳）转发时用此头识别原始 https，避免重定向死循环
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = int(os.environ.get("DJANGO_HSTS_SECONDS", "2592000"))  # 30 天
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = "same-origin"
    SESSION_COOKIE_HTTPONLY = True
    X_FRAME_OPTIONS = "DENY"
    # 会话与 CSRF 失败更严格
    SESSION_EXPIRE_AT_BROWSER_CLOSE = env_bool("DJANGO_SESSION_EXPIRE_ON_CLOSE", False)
