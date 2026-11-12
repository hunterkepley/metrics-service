"""Dashboard access enforcement.

System admins/auditors keep their existing access. Users holding a dashboard role
(managed roles registered in ANSIBLE_BASE_MANAGED_ROLE_REGISTRY in apps/core/settings.py)
get org-scoped access instead: the permission classes deny access unless the user has
the role on at least one organization, and the viewsets restrict responses to the
organizations the role is assigned on.

Role assignments reference mservice-local Organization ids, while JobData and the
AWX filter-dropdown queries are keyed by AWX organization ids. The two id spaces are
bridged through the shared ansible_id (DAB resource registry): the permitted local
orgs' ansible_ids are resolved in the mservice database, then mapped to AWX org ids
through the AWX database's resource registry table.

.. TODO(ANSTRAT-2508): PoC shortcut — this maps to the Controller's numeric
   organization ID at request time. The production design stores the stable
   ``organization_ansible_id`` (DAB Resource.ansible_id UUID) on each JobData row
   via metrics-utility and the backfill command, so report queries do not need a
   live AWX lookup. Replace this mapping before the production implementation.
"""

import json
import logging

from ansible_base.rbac.evaluations import has_super_permission
from ansible_base.resource_registry.models import Resource
from django.contrib.contenttypes.models import ContentType
from rest_framework.permissions import SAFE_METHODS, BasePermission

from apps.core.models import Organization
from apps.tasks.utils import get_db_connection

logger = logging.getLogger(__name__)

VIEW_AUTOMATION_DASHBOARD_PERMISSION = "view_automation_dashboard"
CHANGE_AUTOMATION_DASHBOARD_PERMISSION = "change_automation_dashboard"


def dashboard_view_org_ids(user) -> set[int] | None:
    """mservice Organization ids the user can view dashboard data for; None means unrestricted (admin/auditor)."""
    if not (user and user.is_authenticated):
        return set()
    if has_super_permission(user, "view"):
        return None
    if not hasattr(user, "_dashboard_view_org_ids"):
        user._dashboard_view_org_ids = {
            int(pk)
            for pk in Organization.access_qs(user, VIEW_AUTOMATION_DASHBOARD_PERMISSION).values_list("id", flat=True)
        }
    return user._dashboard_view_org_ids


def dashboard_edit_org_ids(user) -> set[int] | None:
    """mservice Organization ids the user can edit dashboard data for; None means unrestricted (admins only)."""
    if not (user and user.is_authenticated):
        return set()
    if has_super_permission(user):  # superuser flags only: auditors stay read-only
        return None
    if not hasattr(user, "_dashboard_edit_org_ids"):
        user._dashboard_edit_org_ids = {
            int(pk)
            for pk in Organization.access_qs(user, CHANGE_AUTOMATION_DASHBOARD_PERMISSION).values_list("id", flat=True)
        }
    return user._dashboard_edit_org_ids


def _map_to_awx_org_ids(ansible_ids: list[str]) -> set[int]:
    """Map ansible_ids to AWX organization ids via the AWX database's resource registry."""
    if not ansible_ids:
        return set()
    connection = get_db_connection("awx")
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT r.object_id "
            "FROM dab_resource_registry_resource r "
            "JOIN django_content_type ct ON ct.id = r.content_type_id "
            "WHERE r.ansible_id = ANY(%s) AND ct.app_label = 'main' AND ct.model = 'organization'",
            [ansible_ids],
        )
        return {int(row[0]) for row in cursor.fetchall()}


def _permitted_awx_org_ids(user, org_ids: set[int] | None, cache_attr: str) -> set[int] | None:
    """AWX organization ids for the given mservice org ids; None means unrestricted."""
    if org_ids is None:
        return None
    cached = getattr(user, cache_attr, None)
    if cached is None:
        ansible_ids = list(
            Resource.objects.filter(
                content_type=ContentType.objects.get_for_model(Organization),
                object_id__in={str(org_id) for org_id in org_ids},
            ).values_list("ansible_id", flat=True)
        )
        cached = _map_to_awx_org_ids([str(ansible_id) for ansible_id in ansible_ids])
        setattr(user, cache_attr, cached)
    return cached


def permitted_awx_view_org_ids(user) -> set[int] | None:
    """AWX organization ids the user can view dashboard data for; None means unrestricted."""
    return _permitted_awx_org_ids(user, dashboard_view_org_ids(user), "_dashboard_awx_view_org_ids")


def permitted_awx_edit_org_ids(user) -> set[int] | None:
    """AWX organization ids the user can edit dashboard data for; None means unrestricted."""
    return _permitted_awx_org_ids(user, dashboard_edit_org_ids(user), "_dashboard_awx_edit_org_ids")


def filter_set_organization_ids(filters) -> list[int] | None:
    """Return the organization IDs selected by a saved report, or None when it spans all visible orgs.

    The dashboard UI stores the organization filter under ``organization``; older saved
    reports and tests may use ``organization_id`` or ``organizations``. An explicitly
    empty selection returns ``[]`` (distinct from "no filter").
    """
    if not filters:
        return None
    if isinstance(filters, str):
        try:
            filters = json.loads(filters)
        except (TypeError, ValueError):
            return None
    if not isinstance(filters, dict):
        return None
    for key in ("organization", "organization_id", "organizations"):
        if key in filters:
            raw = filters[key]
            if raw is None:
                return None
            if not isinstance(raw, (list, tuple)):
                raw = [raw]
            ids = []
            for value in raw:
                try:
                    ids.append(int(value))
                except (TypeError, ValueError):
                    continue
            return ids
    return None


def user_can_save_report(user, filters) -> bool:
    """Return whether the user may save a report covering the given organization filters.

    A system administrator may save anything. An organization-scoped editor may save a
    report only when every selected organization is one they can edit; a report with no
    organization filter spans every visible organization, so all of those must be editable.
    """
    edit_ids = permitted_awx_edit_org_ids(user)
    if edit_ids is None:
        return True
    selected = filter_set_organization_ids(filters)
    if selected is None:
        view_ids = permitted_awx_view_org_ids(user)
        return bool(view_ids) and view_ids.issubset(edit_ids)
    return bool(selected) and all(org_id in edit_ids for org_id in selected)


class IsDashboardViewer(BasePermission):
    """Dashboard read access: system admins/auditors, or users with a dashboard role on any organization."""

    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False
        if request.method not in SAFE_METHODS:
            return has_super_permission(
                request.user
            )  # writes on read-gated endpoints are system-admin only; auditors stay read-only
        org_ids = dashboard_view_org_ids(request.user)
        return org_ids is None or bool(org_ids)


class IsDashboardEditor(BasePermission):
    """Dashboard cost-input access: reads like IsDashboardViewer, writes for admins or dashboard editors."""

    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False
        if request.method in SAFE_METHODS:
            org_ids = dashboard_view_org_ids(request.user)
            return org_ids is None or bool(org_ids)
        if has_super_permission(request.user):
            return True
        org_ids = dashboard_edit_org_ids(request.user)
        return org_ids is None or bool(org_ids)
