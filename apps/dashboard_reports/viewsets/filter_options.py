"""Base ViewSet for AWX filter dropdown endpoints used by the dashboard reports UI."""

import logging
from collections.abc import Callable
from typing import Any

from ansible_base.rest_pagination import DefaultPaginator
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from apps.dashboard_reports.models import JobData
from apps.dashboard_reports.permissions import IsDashboardViewer, permitted_awx_view_org_ids
from apps.dashboard_reports.serializers import (
    FilterOptionWithIdSerializer,
)
from apps.tasks.api_utils import build_error_response
from apps.tasks.utils import get_db_connection

logger = logging.getLogger(__name__)


class FilterOptionsViewSet(GenericViewSet):
    """
    Base ViewSet for AWX filter dropdowns (labels, organizations, projects, job templates).
    Handles pagination, search, error handling, and response formatting.
    """

    awx_query_function: Callable[..., tuple[list[dict[str, Any]], int]] | None = None  # To be defined in subclasses
    versioning_class = None  # Disable versioning for this viewset
    pagination_class = DefaultPaginator
    permission_classes = [IsDashboardViewer]

    list_error_msg = "Failed to fetch records"
    retrieve_error_msg = "Failed to fetch record"

    def not_found_msg(self, pk: int) -> str:
        """Returns a formatted not found message for a missing record."""
        return f"Record with id {pk} not found"

    def get_queryset(self) -> None:
        """Override to disable queryset (not used for filter dropdowns)."""
        return JobData.objects.none()

    @staticmethod
    def search(request: Request) -> str | None:
        """Extracts search query from request parameters."""
        return request.query_params.get("search", "").strip() or None

    @staticmethod
    def retrieve_response(data: list[dict[str, Any]], error_msg: str) -> Response:
        """
        Returns a single filter dropdown item or error if not found.
        """
        if not data:
            error_response = build_error_response(error_msg, status_code=404)
            return Response(error_response, status=status.HTTP_404_NOT_FOUND)
        serializer = FilterOptionWithIdSerializer(data[0])
        return Response(serializer.data, status=status.HTTP_200_OK)

    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """
        Returns paginated filter dropdown data from AWX database.

        Pagination happens at the DB layer: limit and offset are computed from the paginator
        and forwarded to awx_query_function, which applies them via SQL LIMIT/OFFSET and also
        runs a COUNT(*) query.  The paginator is then initialised with a range() of the total
        count so that next/previous links are built correctly without slicing an in-memory list.
        """
        try:
            db_connection = get_db_connection("awx")
            page_size = self.paginator.get_page_size(request)
            try:
                page_num = int(request.query_params.get(self.paginator.page_query_param, 1))
            except (ValueError, TypeError):
                page_num = 1
            offset = (page_num - 1) * (page_size or 0)
            # Dashboard role-holders only see filter options for their organizations
            awx_org_ids = permitted_awx_view_org_ids(request.user)
            scope_kwargs = {} if awx_org_ids is None else {"org_ids": sorted(awx_org_ids)}
            items, total = self.awx_query_function(
                db_connection=db_connection,
                search_str=FilterOptionsViewSet.search(request),
                limit=page_size,
                offset=offset,
                **scope_kwargs,
            )
            # Initialise paginator state (count, page, request) using a zero-cost range so
            # that get_paginated_response can build correct next/previous links and the count
            # field — without slicing the full result set in Python.
            self.paginate_queryset(range(total))
            return self.get_paginated_response(items)
        except Exception:
            logger.exception(self.list_error_msg)
            error_response = build_error_response(self.list_error_msg, status_code=500)
            return Response(error_response, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def retrieve(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """
        Returns a single filter dropdown item by ID from AWX database.
        """
        try:
            pk = int(kwargs.get("pk"))
        except (TypeError, ValueError):
            pk = None
        if pk is None:
            error_response = build_error_response("Invalid ID", status_code=400)
            return Response(error_response, status=status.HTTP_400_BAD_REQUEST)
        if pk <= 0:
            error_response = build_error_response(self.not_found_msg(pk), status_code=404)
            return Response(error_response, status=status.HTTP_404_NOT_FOUND)

        try:
            db_connection = get_db_connection("awx")
            # Dashboard role-holders only see filter options for their organizations
            awx_org_ids = permitted_awx_view_org_ids(request.user)
            scope_kwargs = {} if awx_org_ids is None else {"org_ids": sorted(awx_org_ids)}
            data, _ = self.awx_query_function(db_connection=db_connection, pk=pk, **scope_kwargs)
            return self.retrieve_response(data, error_msg=self.not_found_msg(pk))
        except Exception:
            logger.exception(self.retrieve_error_msg)
            error_response = build_error_response(self.retrieve_error_msg, status_code=500)
            return Response(error_response, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
