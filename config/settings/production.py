from .deployment import *  # noqa

REST_FRAMEWORK["DEFAULT_AUTHENTICATION_CLASSES"] = (  # noqa F405
    "config.authentication.CustomJWTAuthentication",
    "care_emergency.api_keys.ApiKeyAuthentication",
)

IS_PRODUCTION = True
USE_SMS = env.bool("USE_SMS", default=True)  # noqa F405
