import logging
from typing import Any

from ansible_base.rest_pagination import DefaultPaginator
from django.db import IntegrityError, transaction
from django.db.models import QuerySet
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.mixins import CreateModelMixin, DestroyModelMixin, ListModelMixin, UpdateModelMixin
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from apps.dashboard_reports.models import FilterSet
from apps.dashboard_reports.permissions import IsDashboardEditor, user_can_save_report
from apps.dashboard_reports.serializers import FilterSetSerializer

logger = logging.getLogger(__name__)


@extend_schema_view(
    list=extend_schema(
        summary="Get a list of filter sets from metrics service database (with pagination).",
        description="Returns a list of job templates from metrics service database.",
    ),
    create=extend_schema(
        summary="Create a filter set.",
        description="Create a new filter set object.",
        request=FilterSetSerializer,
    ),
    update=extend_schema(
        summary="Update a specific filter set by ID.",
        description="Update a specific filter set by ID.",
    ),
    partial_update=extend_schema(
        summary="Partially update a specific filter set by ID.",
        description="Partially update a specific filter set by ID.",
    ),
    destroy=extend_schema(
        summary="Delete a specific filter set by ID.",
        description="Delete a specific filter set record by ID.",
    ),
)
class FilterSetsViewSet(ListModelMixin, CreateModelMixin, UpdateModelMixin, DestroyModelMixin, GenericViewSet):
    """
    ViewSet for retrieving and modifying filter sets from metrics service database.

    Provides listing and updating filter sets' entries. This allows users
    (with correct permissions) to view and modify the filter sets as needed.

    Each user can only see and modify their own filter sets.
    Only one filter set per user can be marked as default.

    Endpoints:
        GET    api/v1/dashboard_reports/filter_sets - list all filter sets (with pagination)
        POST   api/v1/dashboard_reports/filter_sets - create a new filter set entry
        PUT    api/v1/dashboard_reports/filter_sets/{id} - update a filter set entry
        PATCH  api/v1/dashboard_reports/filter_sets/{id} - partially update a filter set entry
        DELETE api/v1/dashboard_reports/filter_sets/{id} - delete a filter set entry

    Query Parameters:
        id (int): ID of the filter set entry to edit
        page (int): Page number for pagination
        page_size (int): Number of items per page for pagination
    """

    versioning_class = None  # Disable versioning for this viewset
    permission_classes = [IsDashboardEditor]
    serializer_class = FilterSetSerializer
    pagination_class = DefaultPaginator

    def get_queryset(self) -> QuerySet[FilterSet]:
        """Return filter sets belonging to the currently authenticated user."""
        return FilterSet.objects.filter(user=self.request.user)

    def _clear_other_defaults(self, exclude_pk: int | None = None) -> None:
        """Clear is_default on the user's other filter sets, optionally excluding one by pk."""
        queryset = FilterSet.objects.filter(user=self.request.user, is_default=True)
        if exclude_pk is not None:
            queryset = queryset.exclude(pk=exclude_pk)
        queryset.update(is_default=False)

    def _ensure_can_save(self, filters) -> None:
        """Reject a report whose selected organizations are outside the caller's edit scope."""
        if not user_can_save_report(self.request.user, filters):
            raise PermissionDenied("You need change permission on the selected organizations to save reports.")

    def perform_create(self, serializer: FilterSetSerializer) -> None:
        """Create a filter set, clearing any existing default if the new one is marked default."""
        self._ensure_can_save(serializer.validated_data.get("filters"))
        try:
            with transaction.atomic():
                if serializer.validated_data.get("is_default", False):
                    self._clear_other_defaults()
                serializer.save(user=self.request.user)
        except IntegrityError as exc:
            raise ValidationError({"is_default": "Unable to set this filter set as default."}) from exc

    def perform_update(self, serializer: FilterSetSerializer) -> None:
        """Update a filter set, clearing any existing default if the new one is marked default."""
        filters = serializer.validated_data.get("filters", serializer.instance.filters)
        self._ensure_can_save(filters)
        try:
            with transaction.atomic():
                if serializer.validated_data.get("is_default", False):
                    self._clear_other_defaults(exclude_pk=serializer.instance.pk)
                serializer.save(user=self.request.user)
        except IntegrityError as exc:
            raise ValidationError({"is_default": "Unable to set this filter set as default."}) from exc

    def destroy(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """
        Deletes the filter set. (DELETE method)
        """
        instance = self.get_object()
        self.perform_destroy(instance)
        return Response(status=status.HTTP_204_NO_CONTENT)

    def perform_destroy(self, instance: FilterSet) -> None:
        """Delete the filter set instance after logging the action."""
        logger.info(f"Deleting filter set with id {instance.id} and name {instance.name}")
        instance.delete()
