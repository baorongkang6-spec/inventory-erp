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

BASE_DIR = Path(__file__).resolve().parent.parent

# 启用日（期初基准日）。SPEC §8.1：2026-06-01，可经环境变量覆盖。
OPENING_DATE = datetime.date.fromisoformat(os.environ.get("ERP_OPENING_DATE", "2026-06-01"))


def env_bool(key: str, default: bool) -> bool:
    val = os.environ.get(key)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


# --- 安全与调试 ---------------------------------------------------------------
SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "django-insecure-dev-only-change-me-in-production-0_+swf7@id^$zd",
)
DEBUG = env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")


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


# --- 静态文件 -----------------------------------------------------------------
STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
