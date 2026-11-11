"""ViewSet for dashboard collection status."""

import json
import logging

from ansible_base.rbac.api.permissions import IsSystemAdminOrAuditor
from drf_spectacular.utils import extend_schema, extend_schema_view, inline_serializer
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.viewsets import ViewSet

from apps.dashboard_reports.awx_queries import fetch_organizations
from apps.dashboard_reports.models import JobData
from apps.dashboard_reports.permissions import (
    dashboard_view_org_ids,
    permitted_awx_edit_org_ids,
    permitted_awx_view_org_ids,
)
from apps.dynamic_settings.models import Setting
from apps.tasks.models import Task, TaskExecution
from apps.tasks.task_groups import get_feature_enabled_from_db
from apps.tasks.utils import get_db_connection

logger = logging.getLogger(__name__)


@extend_schema_view(
    create=extend_schema(
        summary="Toggle the show_leaderboard feature flag.",
        description="Sets the runtime-toggleable show_leaderboard setting. Requires system admin or auditor "
        "permissions. Takes effect immediately without a service restart.",
        request=inline_serializer(
            name="DashboardCollectionPostRequest",
            fields={
                "show_leaderboard": serializers.BooleanField(),
            },
        ),
        responses={
            200: inline_serializer(
                name="DashboardCollectionPostResponse",
                fields={
                    "show_leaderboard": serializers.BooleanField(),
                },
            ),
        },
    ),
    list=extend_schema(
        summary="Return dashboard collection feature flag status and task state.",
        description="When enabled is false, next_run, initial_collection_status and min_collection_timestamp are null. initial_collection_status reflects the status of the one-shot initial collection task: 'pending', 'running', 'completed', 'failed' or 'canceled'.",
        responses={
            200: inline_serializer(
                name="DashboardCollectionListResponse",
                fields={
                    "enabled": serializers.BooleanField(),
                    "next_run": serializers.CharField(allow_null=True),
                    "last_sync": serializers.DateTimeField(allow_null=True),
                    "initial_collection_status": serializers.CharField(allow_null=True),
                    "min_collection_timestamp": serializers.DateTimeField(allow_null=True),
                    "show_leaderboard": serializers.BooleanField(),
                    "show_dashboard": serializers.BooleanField(),
                    "scope": serializers.ChoiceField(choices=["global", "organization", "none"]),
                    "organizations": inline_serializer(
                        name="DashboardCollectionOrganization",
                        fields={
                            "id": serializers.IntegerField(help_text="AWX organization ID"),
                            "name": serializers.CharField(allow_null=True),
                            "can_edit": serializers.BooleanField(),
                        },
                        many=True,
                    ),
                },
            ),
        },
    ),
)
class DashboardCollectionStatusViewSet(ViewSet):
    """Returns the enabled state and task status for the dashboard reports collection pipeline."""

    # GET is an access-discovery endpoint: any authenticated user may ask whether
    # they can see the dashboard. POST (settings toggle) stays admin-only.
    permission_classes = [IsAuthenticated]

    def get_permissions(self):
        """Allow reads to any authenticated user; keep create restricted to system admins."""
        if self.request.method in ("GET", "HEAD", "OPTIONS"):
            return [IsAuthenticated()]
        return [IsSystemAdminOrAuditor()]

    @staticmethod
    def _get_latest_completed_hourly_sync() -> TaskExecution | None:
        """Return the most recent completed sync_dashboard_job_records execution, or None."""
        # Tracks sync_dashboard_job_records (JobData), not sync_dashboard_host_summaries:
        # the latter can silently skip records with no retry (see docs/dashboard-sync.md
        # "Ordering constraint"), so a completed run there doesn't guarantee fresh data.
        latest = None
        try:
            latest = TaskExecution.objects.filter(
                status="completed",
                task__function_name="sync_dashboard_job_records",
            ).latest("completed_at")
        except TaskExecution.DoesNotExist:
            logger.debug("No sync found")
        return latest

    def create(self, request: Request, *args, **kwargs) -> Response:
        is_system_admin_or_auditor = IsSystemAdminOrAuditor().has_permission(request, self)
        if not is_system_admin_or_auditor:
            raise PermissionDenied

        new_show_leaderboard = request.data.get("show_leaderboard")
        if not isinstance(new_show_leaderboard, bool):
            raise ValidationError({"show_leaderboard": "Value must be a boolean: true/false"})

        Setting.objects.update_or_create(
            setting_key="SHOW_LEADERBOARD",
            defaults={"current_value": json.dumps(new_show_leaderboard), "last_modified_by": request.user},
        )

        return Response(
            {
                "show_leaderboard": new_show_leaderboard,
            }
        )

    @staticmethod
    def _accessible_organizations(user) -> list[dict]:
        """Return the caller's AWX organizations with their dashboard edit capability.

        Best-effort: if the AWX DB is unavailable, return the IDs we already know with a
        null name rather than failing the whole access-discovery request.
        """
        view_ids = permitted_awx_view_org_ids(user)
        if not view_ids:
            return []
        edit_ids = permitted_awx_edit_org_ids(user)
        try:
            organizations, _ = fetch_organizations(
                db_connection=get_db_connection("awx"),
                org_ids=sorted(view_ids),
            )
        except Exception:
            logger.exception("Unable to resolve accessible dashboard organizations from AWX")
            return [
                {"id": org_id, "name": None, "can_edit": edit_ids is None or org_id in edit_ids}
                for org_id in sorted(view_ids)
            ]
        return [
            {
                "id": organization["id"],
                "name": organization["name"],
                "can_edit": edit_ids is None or organization["id"] in edit_ids,
            }
            for organization in organizations
        ]

    def list(self, request: Request, *args, **kwargs) -> Response:
        """Return dashboard collection feature flag status, task state, and caller access scope.

        When enabled is false, next_run and initial_collection_status are null.
        initial_collection_status reflects the status of the one-shot initial collection task:
        "pending", "running", "completed", "failed", or "cancelled".
        """
        enabled = get_feature_enabled_from_db("DASHBOARD_COLLECTION", default=True)
        show_leaderboard = get_feature_enabled_from_db("SHOW_LEADERBOARD", default=True)

        view_org_ids = dashboard_view_org_ids(request.user)
        if view_org_ids is None:
            scope = "global"
        elif view_org_ids:
            scope = "organization"
        else:
            scope = "none"
        organizations = self._accessible_organizations(request.user) if scope == "organization" else []
        show_dashboard = get_feature_enabled_from_db("SHOW_DASHBOARD", default=True) and scope != "none"
        last_sync = None

        next_run = None
        initial_collection_status = None
        min_collection_timestamp = None

        if enabled:
            min_collection_timestamp = JobData.min_timestamp()
            latest_hourly_sync = self._get_latest_completed_hourly_sync()
            if latest_hourly_sync is not None:
                last_sync = latest_hourly_sync.completed_at

            # Incremental dashboard sync is driven by the hourly_unified_jobs hook,
            # so next_run reflects when that collector will next fire.
            hourly_task = Task.objects.filter(
                name="hourly_unified_jobs",
                is_system_task=True,
            ).first()
            if hourly_task:
                next_run = hourly_task.get_next_run_time()

            initial_task = Task.objects.filter(
                function_name="collect_dashboard_reports_initial_data",
                is_system_task=True,
            ).first()
            if initial_task:
                initial_collection_status = initial_task.status
                # Task.completed_at (not its TaskExecution) survives init-system-tasks and cleanup_old_tasks,
                # so it still reports the initial collection when no hourly sync has completed since
                # (e.g. METRICS_COLLECTION disabled), including a collection that wrote zero jobs.
                if initial_collection_status == "completed" and initial_task.completed_at:
                    last_sync = max(filter(None, (last_sync, initial_task.completed_at)))

        return Response(
            {
                "enabled": enabled,
                "next_run": next_run,
                "last_sync": last_sync,  # will be None if no sync was done yet
                "initial_collection_status": initial_collection_status,
                "min_collection_timestamp": min_collection_timestamp,
                "show_leaderboard": show_leaderboard,  # toggle-able by admins/system-auditors
                "show_dashboard": show_dashboard,
                "scope": scope,
                "organizations": organizations,
            }
        )
