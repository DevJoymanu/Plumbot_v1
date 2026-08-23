"""
Django settings for Plumbing_CRM project - PRODUCTION VERSION
"""

from pathlib import Path
import os
from django.contrib.messages import constants as messages
import dj_database_url
from dotenv import load_dotenv

# Local .env values must land in os.environ before anything below reads them.
load_dotenv()




# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = os.environ.get('SECRET_KEY', 'django-insecure-CHANGE-THIS-IN-PRODUCTION')

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = os.environ.get('DEBUG', 'False') == 'True'

# Canonical public host. The Railway-generated *.up.railway.app host stays in
# the list so the app keeps answering there while DNS propagates and so
# Railway's internal health checks never 400.
PRIMARY_HOST = os.environ.get('PRIMARY_HOST', 'plumbot.homexmedia.com').strip()


def _clean_host(value):
    """Normalise one ALLOWED_HOSTS entry.

    Django matches the bare Host header, so an entry copied from a browser
    ('https://plumbot.homexmedia.com/') never matches and the site 400s with
    no clue why. Strip the scheme, any path and any port.
    """
    host = value.strip()
    if '//' in host:
        host = host.split('//', 1)[1]
    host = host.split('/', 1)[0]
    if host.count(':') == 1:  # host:port, not a bare IPv6 literal
        host = host.split(':', 1)[0]
    return host.strip().lower()


# Hosts we must answer on no matter what the environment says. A wrong or
# stale ALLOWED_HOSTS variable in Railway used to take the canonical domain
# offline (every request 400'd with DisallowedHost); the env var may now only
# ADD hosts, never remove these.
_REQUIRED_HOSTS = [
    PRIMARY_HOST,
    '.homexmedia.com',
    '.railway.app',
    '.up.railway.app',
    'localhost',
    '127.0.0.1',
]

ALLOWED_HOSTS = []
for _h in _REQUIRED_HOSTS + os.environ.get('ALLOWED_HOSTS', '').split(','):
    _h = _clean_host(_h)
    if _h and _h not in ALLOWED_HOSTS:
        ALLOWED_HOSTS.append(_h)

# Django 4 requires the scheme here; a bare host is rejected at startup.
# Same rule as ALLOWED_HOSTS: the environment may add origins, not drop ours.
CSRF_TRUSTED_ORIGINS = []
for _o in [
    f'https://{PRIMARY_HOST}',
    'https://*.homexmedia.com',
    'https://*.railway.app',
] + os.environ.get('CSRF_TRUSTED_ORIGINS', '').split(','):
    _o = _o.strip().rstrip('/')
    if _o and '://' not in _o:
        _o = f'https://{_o}'
    if _o and _o not in CSRF_TRUSTED_ORIGINS:
        CSRF_TRUSTED_ORIGINS.append(_o)

# Printed on every boot so a deploy log shows what the process actually
# resolved -- the fastest way to tell a bad env var from a proxy problem.
print(f'[settings] ALLOWED_HOSTS={ALLOWED_HOSTS}', flush=True)

# Application definition
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'bot',
    'django_cron',
    'storages'  # Add this

]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',  # Add WhiteNoise for static files
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'bot.middleware.TenantMiddleware',  # pins request.tenant (after auth)
]

ROOT_URLCONF = 'Plumbing_CRM.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'bot.context_processors.plumbot_shell',
            ],
        },
    },
]

WSGI_APPLICATION = 'Plumbing_CRM.wsgi.application'

# Database - Use Railway's DATABASE_URL if available, otherwise use environment variables
# NOTE: SECRET_KEY / DEBUG / ALLOWED_HOSTS are defined once at the top of this
# file. They used to be re-read here, which silently overwrote the host list
# with [''] whenever ALLOWED_HOSTS was unset in the environment -> every
# request 400'd with DisallowedHost. Never redefine them below this point.

DATABASES = {
    "default": dj_database_url.config(default=os.getenv("DATABASE_URL"))
}

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")


# Static & Media files
STATIC_URL = '/static/'
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')

MEDIA_URL = '/media/'
MEDIA_ROOT = os.path.join(BASE_DIR, 'media')

# WhiteNoise configuration for efficient static file serving
STORAGES = {
    'default': {
        'BACKEND': 'django.core.files.storage.FileSystemStorage',
    },
    'staticfiles': {
        'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage',
    },
}

# Google Service Account - Use environment variable in production
GOOGLE_CREDENTIALS_PATH = os.environ.get(
    'GOOGLE_CREDENTIALS_PATH',
    os.path.join(BASE_DIR, 'crested-epoch-460808-q2-b009dc5f66dd.json')
)

# Google Calendar ID
GOOGLE_CALENDAR_ID = os.environ.get('GOOGLE_CALENDAR_ID', 'joymanu49@gmail.com')

# Plumber WhatsApp
PLUMBER_WHATSAPP_NUMBER = os.environ.get('PLUMBER_WHATSAPP_NUMBER', 'whatsapp:+27610318200')
PLUMBER_NOTIFICATION_EMAILS = [
    'jones86xi@gmail.com',
    'homebsconstruction@gmail.com',
]

# Absolute base for every link we put in a WhatsApp message or email.
SITE_URL = os.environ.get('SITE_URL', f'https://{PRIMARY_HOST}').rstrip('/')

# The platform owner account(s) — the ONLY logins allowed to delete past
# conversations (bot/decorators.py owner_required). Superuser alone is not
# enough: a second admin account must not be able to destroy transcripts.
# Comma-separated usernames or email addresses, matched case-insensitively.
# An empty value falls back to "any superuser" so a mis-set env var can never
# lock the owner out of their own platform.
PLATFORM_OWNER_ACCOUNTS = [
    entry.strip()
    for entry in os.environ.get(
        'PLATFORM_OWNER_ACCOUNTS', 'adminJ,jones86xi@gmail.com'
    ).split(',')
    if entry.strip()
]

# DeepSeek API (replacing OpenAI)
DEEPSEEK_API_KEY = os.environ.get('DEEPSEEK_API_KEY', '')
# Model is env-overridable so it can be switched without a redeploy. Note:
# DeepSeek's current API models are 'deepseek-v4-flash' (non-thinking) and
# 'deepseek-v4-pro' (thinking); the legacy names 'deepseek-chat'/'deepseek-reasoner'
# were deprecated 2026-07-24. If the configured model returns empty/garbled
# completions, set DEEPSEEK_MODEL to 'deepseek-v4-flash' to fall back to a
# known-stable model.
DEEPSEEK_MODEL = os.environ.get('DEEPSEEK_MODEL', 'deepseek-v4-flash')

# Twilio Configuration
# Twilio removed — WhatsApp goes through the Meta Cloud API per tenant.

# Email configuration
# IPv4-forcing backend avoids "Network is unreachable" on hosts without
# IPv6 egress (Railway). See bot/email_backends.py.
EMAIL_BACKEND = os.environ.get('EMAIL_BACKEND', 'bot.email_backends.IPv4SMTPBackend')
# Self-correct: if the plain SMTP backend is configured (via a stale env var),
# transparently upgrade it to the IPv4-forcing one so we don't depend on the
# operator updating EMAIL_BACKEND by hand.
if EMAIL_BACKEND == 'django.core.mail.backends.smtp.EmailBackend':
    EMAIL_BACKEND = 'bot.email_backends.IPv4SMTPBackend'
EMAIL_HOST = os.environ.get('EMAIL_HOST', '')
EMAIL_PORT = int(os.environ.get('EMAIL_PORT', '587'))
EMAIL_HOST_USER = os.environ.get('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.environ.get('EMAIL_HOST_PASSWORD', '')
EMAIL_USE_TLS = os.environ.get('EMAIL_USE_TLS', 'True') == 'True'
EMAIL_USE_SSL = os.environ.get('EMAIL_USE_SSL', 'False') == 'True'
# Normalize TLS/SSL to the port so a mismatched env var (e.g. SSL on 587)
# can't cause a handshake failure once the network path is up.
#   port 465 → implicit SSL    port 587 → STARTTLS
if EMAIL_PORT == 465:
    EMAIL_USE_SSL, EMAIL_USE_TLS = True, False
elif EMAIL_PORT == 587:
    EMAIL_USE_TLS, EMAIL_USE_SSL = True, False
# Keep SMTP failures short. Customer-facing WhatsApp replies are no longer
# blocked by delay emails, but a dropped SMTP port should still fail quickly.
EMAIL_TIMEOUT = int(os.environ.get('EMAIL_TIMEOUT', '5'))
_from_address = os.environ.get('EMAIL_FROM_ADDRESS', EMAIL_HOST_USER or 'info@homebaseplumbers.co.zw')
_from_name    = os.environ.get('EMAIL_FROM_NAME', 'HomeBase Plumbers')
DEFAULT_FROM_EMAIL = f"{_from_name} <{_from_address}>"
SERVER_EMAIL = os.environ.get('SERVER_EMAIL', DEFAULT_FROM_EMAIL)
EMAIL_REPLY_TO = os.environ.get('EMAIL_REPLY_TO', _from_address)
EMAIL_DOMAIN = os.environ.get('EMAIL_DOMAIN', 'homebaseplumbers.co.zw')

# Platform sending domain. INTERNAL notifications (the ones that go to the
# operator and to the tenant's own inbox) are sent as <tenant-slug>@ this
# domain, so every tenant's alerts are visibly theirs while the platform owns
# the sending identity.
#
# ONE authenticated domain, per-tenant local part -- deliberately not
# <slug>.homexmedia.com. Every distinct domain must be separately
# SPF/DKIM-authenticated with the mail provider, and provider plans cap how
# many you may authenticate; a per-tenant subdomain would need a new
# authenticated domain (and new DNS records) for every tenant onboarded.
# This shape needs exactly one set of records, forever.
#
# CUSTOMER-facing mail is sent from the tenant's OWN domain address
# (TenantProfile.customer_from_email) and only falls back to this sender when
# the tenant has not configured one.
PLATFORM_EMAIL_DOMAIN = os.environ.get('PLATFORM_EMAIL_DOMAIN', 'notifications.homexmedia.com')

# Email transport over HTTP (port 443). Railway blocks all outbound SMTP egress,
# so an HTTPS send API is the only path that delivers from this host; the SMTP
# block above is a fallback for environments that permit it. Transport
# precedence (see bot/plumber_notifications.py): Brevo → SendGrid → SMTP.
#
# Brevo (ex-Sendinblue) — primary transport. 300 emails/day on the free-forever
# plan; replaced SendGrid after its time-limited trial ended.
BREVO_API_KEY = os.environ.get('BREVO_API_KEY', '')
BREVO_FROM_EMAIL = os.environ.get('BREVO_FROM_EMAIL', '') or _from_address

# SendGrid HTTP API — legacy transport, kept as a fallback when BREVO_API_KEY
# is unset but SENDGRID_API_KEY is still configured.
SENDGRID_API_KEY = os.environ.get('SENDGRID_API_KEY', '')
SENDGRID_FROM_EMAIL = os.environ.get('SENDGRID_FROM_EMAIL', '') or _from_address

APPEND_SLASH = False

# Authentication settings
LOGIN_URL = 'login'
LOGIN_REDIRECT_URL = 'dashboard'
LOGOUT_REDIRECT_URL = 'login'

# Session settings
SESSION_COOKIE_AGE = 86400  # 24 hours in seconds
SESSION_SAVE_EVERY_REQUEST = True
SESSION_EXPIRE_AT_BROWSER_CLOSE = False

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
        'OPTIONS': {
            'min_length': 8,
        }
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

MESSAGE_TAGS = {
    messages.DEBUG: 'debug',
    messages.INFO: 'info',
    messages.SUCCESS: 'success',
    messages.WARNING: 'warning',
    messages.ERROR: 'danger',
}

# Initial staff users (use management command to create)
INITIAL_STAFF_USERS = [
    {
        'username': 'admin',
        'email': 'admin@plumbingcompany.com',
        'password': 'changeme123',
        'is_staff': True,
        'is_superuser': True,
    },
]

# Internationalization
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Africa/Johannesburg'
USE_I18N = True
USE_TZ = True

# Static files
STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"



# Default primary key field type
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Security settings for production
if not DEBUG:
    # Don't force SSL redirect - Railway handles this
    SECURE_SSL_REDIRECT = False
    
    # Trust Railway's proxy headers
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_BROWSER_XSS_FILTER = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = 'SAMEORIGIN'

# Logging configuration
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{levelname} {asctime} {module} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'INFO',
    },
    'loggers': {
        'django': {
            'handlers': ['console'],
            'level': os.environ.get('DJANGO_LOG_LEVEL', 'INFO'),
            'propagate': False,
        },
    },
}

# ===== CLOUDFLARE R2 / AWS S3 STORAGE CONFIGURATION =====
import os

USE_S3 = os.getenv("USE_S3", "FALSE").upper() == "TRUE"

if USE_S3:
    # Cloudflare R2 / S3 storage
    STORAGES = {
        "default": {
            "BACKEND": "storages.backends.s3boto3.S3Boto3Storage",
            "OPTIONS": {
                "access_key": os.getenv("AWS_ACCESS_KEY_ID"),
                "secret_key": os.getenv("AWS_SECRET_ACCESS_KEY"),
                "bucket_name": os.getenv("AWS_STORAGE_BUCKET_NAME"),
                "endpoint_url": os.getenv("AWS_S3_ENDPOINT_URL"),
                "custom_domain": os.getenv("AWS_S3_CUSTOM_DOMAIN") or None,
                # R2 doesn't use AWS regions — this suppresses boto3 warnings
                "region_name": "auto",
                # Don't overwrite files with the same name
                "file_overwrite": False,
                # Generate presigned URLs when no custom_domain is set
                "querystring_auth": not bool(os.getenv("AWS_S3_CUSTOM_DOMAIN")),
            },
        },
        "staticfiles": {
            "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
        },
    }
else:
    # Local dev fallback
    STORAGES = {
        "default": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
        },
        "staticfiles": {
            "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
        },
    }


# ===== TEST MODE =====
# `manage.py test` must never touch the production database or the R2 bucket:
# run the suite on an in-memory SQLite DB, local filesystem storage, and plain
# (non-manifest) staticfiles so templates render without collectstatic.
import sys
TESTING = 'test' in sys.argv
if TESTING:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": ":memory:",
        }
    }
    STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
    MEDIA_ROOT = os.path.join(BASE_DIR, 'test_media')
    # Fast password hashing — login in tests shouldn't burn CPU on PBKDF2.
    PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
    # bot's migration history contains Postgres-only RunSQL (ALTER COLUMN /
    # plpgsql DO blocks) that SQLite can't execute. Build the test schema
    # straight from the current models instead — models are the source of
    # truth, and it's much faster than replaying every migration.
    MIGRATION_MODULES = {'bot': None}
