"""ViewSet for retrieving and updating AWX job template metadata (time estimates)."""

from ansible_base.rbac.api.permissions import IsSystemAdminOrAuditor
from django.db.models import QuerySet
from django.utils import timezone
from drf_spectacular.utils import extend_schema, extend_schema_view, inline_serializer
from rest_framework import serializers, status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.mixins import RetrieveModelMixin, UpdateModelMixin
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from apps.dashboard_reports.models import (
    DEFAULT_TIME_TAKEN_TO_CREATE_AUTOMATION_MINUTES,
    DEFAULT_TIME_TAKEN_TO_MANUALLY_EXECUTE_MINUTES,
    JobData,
    TemplateMetadata,
)
from apps.dashboard_reports.permissions import (
    IsDashboardViewer,
    dashboard_edit_org_ids,
    permitted_awx_edit_org_ids,
)
from apps.dashboard_reports.serializers import (
    TemplateMetadataBulkUpdateSerializer,
    TemplateMetadataDefaultsSerializer,
    TemplateMetadataSerializer,
)


@extend_schema_view(
    retrieve=extend_schema(
        summary="Get specific template metadata ID.",
        description="Returns a specific template metadata by ID.",
    ),
    update=extend_schema(
        summary="Update a specific template metadata by ID.",
        description="Update a specific template metadata by ID.",
    ),
    partial_update=extend_schema(
        summary="Partially update a specific template metadata by ID.",
        description="Partially update a specific template metadata by ID.",
    ),
)
class TemplateMetadataViewSet(RetrieveModelMixin, UpdateModelMixin, GenericViewSet):
    """
    ViewSet for retrieving template metadata from metrics service database.

    Endpoints:
        GET    api/v1/dashboard_reports/template_metadata/{id}/ - Get template metadata
        PUT    api/v1/dashboard_reports/template_metadata/{id}/ - Update template metadata
        PATCH  api/v1/dashboard_reports/template_metadata/{id}/ - Partially update template metadata
        PATCH  api/v1/dashboard_reports/template_metadata/ - Partially update ALL template metadata
        GET    api/v1/dashboard_reports/template_metadata/defaults/ - Get default time estimates
    """

    versioning_class = None
    pagination_class = None
    serializer_class = TemplateMetadataSerializer

    def get_permissions(self):
        """Allow dashboard viewers to read estimates; writes remain system-admin only.

        TODO(ANSTRAT-2508): settings are deferred in the PoC. Once the org-shared
        settings model is confirmed, organization Editors should write per-org
        overrides here rather than the global TemplateMetadata row.
        """
        if self.request.method in ("GET", "HEAD", "OPTIONS"):
            return [IsDashboardViewer()]
        return [IsSystemAdminOrAuditor()]

    def get_queryset(self) -> QuerySet[TemplateMetadata]:
        """Return all TemplateMetadata records."""
        return TemplateMetadata.objects.all()

    def perform_update(self, serializer):
        """Org-scoped write check: editors may only update templates whose jobs ran in their organizations."""
        awx_edit_org_ids = permitted_awx_edit_org_ids(self.request.user)
        if awx_edit_org_ids is not None:
            template_org_ids = set(
                JobData.objects.filter(template_metadata_id=serializer.instance.pk).values_list(
                    "organization_id", flat=True
                )
            )
            if not template_org_ids & awx_edit_org_ids:
                raise PermissionDenied("User does not have dashboard editor access to this template's organization.")
        serializer.save()

    @extend_schema(
        operation_id="dashboard_reports_template_metadata_partial_update_all",
        summary="Partially update all template metadata.",
        description=(
            "Applies the given time estimates (minutes) to every template metadata record, overwriting existing "
            "values. Only the provided fields are changed. Null values are not allowed."
        ),
        extensions={"x-ai-description": "Partially update the time estimates of all template metadata records"},
        request=TemplateMetadataBulkUpdateSerializer,
        responses=inline_serializer(
            "TemplateMetadataUpdateAllResponse",
            {"updated": serializers.IntegerField(help_text="Number of template metadata records updated")},
        ),
    )
    def partial_update_all(self, request: Request) -> Response:
        """Apply the given time estimates to all template metadata records."""
        if dashboard_edit_org_ids(request.user) is not None:
            # Bulk update is a global operation: system administrators only
            raise PermissionDenied("Bulk template metadata updates require system administrator.")
        serializer = TemplateMetadataBulkUpdateSerializer(
            data=request.data, partial=True, context=self.get_serializer_context()
        )
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        if not values:
            return Response({"detail": "Provide at least one time estimate field."}, status=status.HTTP_400_BAD_REQUEST)
        updated = self.get_queryset().update(**values, modified=timezone.now(), modified_by=request.user)
        return Response({"updated": updated})

    @extend_schema(
        operation_id="dashboard_reports_template_metadata_defaults",
        summary="Get default template time estimates.",
        description=(
            "Returns the default time estimates (minutes) for manual execution and automation creation. "
            "These are suggested values shown when setting time estimates for all templates."
        ),
        extensions={"x-ai-description": "Get the default manual execution and automation creation time estimates"},
        responses=TemplateMetadataDefaultsSerializer,
    )
    @action(detail=False, methods=["get"], url_path="defaults", serializer_class=TemplateMetadataDefaultsSerializer)
    def defaults(self, request: Request) -> Response:
        """Return the default time estimates for template metadata."""
        serializer = self.get_serializer(
            {
                "time_taken_manually_execute_minutes": DEFAULT_TIME_TAKEN_TO_MANUALLY_EXECUTE_MINUTES,
                "time_taken_create_automation_minutes": DEFAULT_TIME_TAKEN_TO_CREATE_AUTOMATION_MINUTES,
            }
        )
        return Response(serializer.data)
