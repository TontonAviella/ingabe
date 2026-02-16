"""
Apache Superset configuration for Mundi.ai Rwanda Agriculture GeoAI Platform
Development configuration with embedding support
"""
import os
from typing import Optional


# Row limit for SQL queries
ROW_LIMIT = 5000

# Secret key for session management and CSRF protection
SECRET_KEY = os.environ.get("SUPERSET_SECRET_KEY", "superset-secret-key-change-in-prod")

# Database configuration - PostgreSQL backend for Superset metadata
SQLALCHEMY_DATABASE_URI = (
    f"postgresql://{os.environ.get('DATABASE_USER', 'mundiuser')}:"
    f"{os.environ.get('DATABASE_PASSWORD', 'gdalpassword')}@"
    f"{os.environ.get('DATABASE_HOST', 'postgresdb')}:"
    f"{os.environ.get('DATABASE_PORT', '5432')}/"
    f"{os.environ.get('DATABASE_DB', 'superset')}"
)

# Redis cache configuration
CACHE_CONFIG = {
    "CACHE_TYPE": "RedisCache",
    "CACHE_DEFAULT_TIMEOUT": 300,
    "CACHE_KEY_PREFIX": "superset_",
    "CACHE_REDIS_HOST": os.environ.get("REDIS_HOST", "redis"),
    "CACHE_REDIS_PORT": int(os.environ.get("REDIS_PORT", "6379")),
    "CACHE_REDIS_DB": 1,
}

# Redis data cache for query results
DATA_CACHE_CONFIG = {
    "CACHE_TYPE": "RedisCache",
    "CACHE_DEFAULT_TIMEOUT": 86400,  # 1 day
    "CACHE_KEY_PREFIX": "superset_data_",
    "CACHE_REDIS_HOST": os.environ.get("REDIS_HOST", "redis"),
    "CACHE_REDIS_PORT": int(os.environ.get("REDIS_PORT", "6379")),
    "CACHE_REDIS_DB": 2,
}

# Embedding configuration
GUEST_ROLE_NAME = "Gamma"
GUEST_TOKEN_JWT_ALGO = "HS256"
GUEST_TOKEN_HEADER_NAME = "X-GuestToken"
PUBLIC_ROLE_LIKE = "Gamma"

# Feature flags
FEATURE_FLAGS = {
    "EMBEDDED_SUPERSET": True,
    "ENABLE_TEMPLATE_PROCESSING": True,
    "DASHBOARD_NATIVE_FILTERS": True,
    "DASHBOARD_CROSS_FILTERS": True,
    "DASHBOARD_FILTERS_EXPERIMENTAL": True,
    "ENABLE_EXPLORE_JSON_CSRF_PROTECTION": False,  # For dev API access
}

# Security configuration - Development mode settings
TALISMAN_ENABLED = False  # Disable Talisman for development
WTF_CSRF_ENABLED = False  # Disable CSRF for development API access

# CORS configuration - Allow all origins in development
ENABLE_CORS = True
CORS_OPTIONS = {
    "supports_credentials": True,
    "allow_headers": ["*"],
    "resources": ["*"],
    "origins": ["*"],  # For production, replace with specific origins
}

# Session cookie configuration
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = False  # Set to True in production with HTTPS
SESSION_COOKIE_HTTPONLY = True

# Enable proxy fix for Docker environments
ENABLE_PROXY_FIX = True

# Webdriver configuration for charts/thumbnails
WEBDRIVER_TYPE = "chrome"
WEBDRIVER_OPTION_ARGS = [
    "--headless",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
]

# SQL Lab configuration
SQLLAB_ASYNC_TIME_LIMIT_SEC = 300
SQLLAB_TIMEOUT = 300
SUPERSET_WEBSERVER_TIMEOUT = 300

# Email configuration (optional - configure if needed)
SMTP_HOST: Optional[str] = None
SMTP_STARTTLS: bool = False
SMTP_SSL: bool = False
SMTP_USER: Optional[str] = None
SMTP_PORT: int = 25
SMTP_PASSWORD: Optional[str] = None
SMTP_MAIL_FROM: str = "superset@localhost"

# Mapbox API key (optional - for geospatial visualizations)
MAPBOX_API_KEY = os.environ.get("MAPBOX_API_KEY", "")

# Additional configuration for Rwanda Agriculture platform
# You can add custom configurations here as needed
