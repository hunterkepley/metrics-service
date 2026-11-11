"""
Core App Settings

This file contains settings specific to the core app.
These settings configure Django and DAB to use the core app's models.
"""

AUTH_USER_MODEL = "core.User"
ANSIBLE_BASE_TEAM_MODEL = "core.Team"
ANSIBLE_BASE_ORGANIZATION_MODEL = "core.Organization"

# Resource Registry Configuration
ANSIBLE_BASE_RESOURCE_CONFIG_MODULE = "apps.core.resource_api"

ANSIBLE_BASE_USER_VIEWSET = "apps.core.v1.viewsets.user.UserViewSet"

# Authentication - insert JWT auth at position 0
REST_FRAMEWORK__DEFAULT_AUTHENTICATION_CLASSES = "@insert 0 apps.core.authentication.ServiceJWTAuthentication"

# Login/Logout URLs for DRF browsable API
LOGIN_URL = "/api-auth/login/"
LOGOUT_URL = "/api-auth/logout/"

# Use custom renderer for correct breadcrumbs with SCRIPT_NAME prefix
REST_FRAMEWORK__DEFAULT_RENDERER_CLASSES = [
    "rest_framework.renderers.JSONRenderer",
    "apps.core.renderers.ServiceBrowsableAPIRenderer",
]
# Middleware - ServicePrefix at start, NullByte guard early, APIRootView at end
MIDDLEWARE = [
    "dynaconf_merge_unique",
    "apps.core.middleware.ServicePrefixMiddleware",
    "apps.core.middleware.NullByteQueryParamMiddleware",
    "apps.core.middleware.APIRootViewMiddleware",
]


# RBAC Configuration
ANSIBLE_BASE_ALLOW_SINGLETON_USER_ROLES = True
ANSIBLE_BASE_ALLOW_SINGLETON_TEAM_ROLES = True
ALLOW_SHARED_RESOURCE_CUSTOM_ROLES = False
ALLOW_LOCAL_ASSIGNING_JWT_ROLES = True  # Set to False with resource server
# Models to register with DAB RBAC - these are registered automatically by DAB
ANSIBLE_BASE_RBAC_MODEL_REGISTRY = {
    "core.Organization": {"parent_field_name": None},
    "core.Team": {"parent_field_name": "organization"},
}

# Default RBAC roles - created automatically on `python manage.py migrate`
ANSIBLE_BASE_MANAGED_ROLE_REGISTRY = {
    "sys_auditor": {"name": "Platform Auditor"},  # View-only, system-wide
    "org_admin": {},  # Organization Admin - all perms on org + children
    "org_member": {},  # Organization Member - member perm on org
    "team_admin": {},  # Team Admin - all perms on team
    "team_member": {},  # Team Member - member perm on team
}

# Configure which roles can be synced via JWT from gateway
ANSIBLE_BASE_JWT_MANAGED_ROLES = [
    "Platform Auditor",
    "Organization Admin",
    "Organization Member",
    "Team Admin",
    "Team Member",
]

# Resource Server Configuration
RESOURCE_SERVER: dict[str, str | bool | None] = {
    # 'URL': 'https://aap-gw-proxy-1:9080',
    # 'SECRET_KEY': '<service key>',
    # 'VALIDATE_HTTPS': False,
}
RESOURCE_SERVER_SYNC_ENABLED = False

SERVICE_TYPE = "metrics-service"
SERVICE_ID = "metrics-service-instance-id"
