"""ViewSet for the main dashboard report endpoint, providing job run metrics and cost analytics."""

import csv
import decimal
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from functools import wraps
from typing import Any

from ansible_base.rest_pagination import DefaultPaginator
from dateutil.relativedelta import relativedelta
from django.db import models
from django.db.models import Case, Count, F, OuterRef, Q, QuerySet, Subquery, Sum, Value, When
from django.db.models.functions import Cast, Coalesce, Trunc
from django.http import HttpResponse, JsonResponse
from django_generate_series.models import generate_series  # PostgreSQL-only; revisit if other DB support is added
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view, inline_serializer
from rest_framework import filters, serializers, status
from rest_framework.decorators import action
from rest_framework.renderers import BaseRenderer
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.settings import api_settings
from rest_framework.viewsets import ReadOnlyModelViewSet

from apps.dashboard_reports.filters import CustomReportFilter, DateFilter, validate_custom_period_dates
from apps.dashboard_reports.models import JobData, JobHostSummary, JobLabel, JobStatusChoices, SubscriptionCost
from apps.dashboard_reports.permissions import IsDashboardViewer, permitted_awx_view_org_ids
from apps.dashboard_reports.serializers import (
    ReportDetailSerializer,
    ReportSerializer,
)
from apps.dashboard_reports.utils import sec2time
from apps.tasks.api_utils import build_error_response

logger = logging.getLogger(__name__)


class AliasedOrderingFilter(filters.OrderingFilter):
    """OrderingFilter that also accepts `order_by` as an alias for the standard `ordering` query parameter.

    Also remaps field names via the view's `ordering_field_aliases` dict (if set) before validation,
    so a view can expose a user-facing field name that differs from the actual queryset column/path.
    """

    alias_ordering_param = "order_by"

    def get_ordering(self, request: Request, queryset: QuerySet, view: Any) -> Sequence[str] | None:
        """Return the ordering fields from `ordering`, falling back to `order_by`, then the view default.

        If both `ordering` and `order_by` are supplied, `ordering` wins (matches DRF's canonical
        param name). This reimplements DRF's OrderingFilter.get_ordering() body in full rather than
        delegating to super() after rewriting the param, because request.query_params is a read-only
        QueryDict — patching it in place would mean reaching into the private request._request
        attribute, which is more fragile than duplicating a few lines of long-stable DRF logic.
        """
        params = request.query_params.get(self.ordering_param) or request.query_params.get(self.alias_ordering_param)
        if params:
            field_aliases = getattr(view, "ordering_field_aliases", {})
            fields = [self._resolve_field_alias(param.strip(), field_aliases) for param in params.split(",")]
            ordering = self.remove_invalid_fields(queryset, fields, view, request)
            if ordering:
                return ordering
        return self.get_default_ordering(view)

    @staticmethod
    def _resolve_field_alias(field: str, field_aliases: dict[str, str]) -> str:
        """Remap `field` (preserving a leading `-` for descending order) through `field_aliases`."""
        prefix, name = ("-", field[1:]) if field.startswith("-") else ("", field)
        return prefix + field_aliases.get(name, name)


class DashboardReportPagination(DefaultPaginator):
    """DefaultPaginator pinned to this project's REST_FRAMEWORK PAGE_SIZE.

    DefaultPaginator.__init__ reads its own DEFAULT_PAGE_SIZE/MAX_PAGE_SIZE Django settings
    (falling back to ansible_base's hardcoded 50/200) instead of this project's
    REST_FRAMEWORK['PAGE_SIZE'], so the default page size must be pinned explicitly here to stay
    consistent with the rest of the API. `page_size` and `count_disabled` query params are inherited
    from DefaultPaginator unchanged (see PAGINATION_QUERY_PARAMETERS for their documented behavior).
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.page_size = api_settings.PAGE_SIZE


PAGINATION_QUERY_PARAMETERS = [
    OpenApiParameter(
        name="page",
        type=OpenApiTypes.INT,
        default=1,
        location=OpenApiParameter.QUERY,
        required=True,
        description="Page number (default: 1)",
    ),
    OpenApiParameter(
        name="page_size",
        type=OpenApiTypes.INT,
        default=api_settings.PAGE_SIZE,
        location=OpenApiParameter.QUERY,
        required=True,
        description=f"Results per page (default: {api_settings.PAGE_SIZE}).",
    ),
    OpenApiParameter(
        name="count_disabled",
        type=OpenApiTypes.BOOL,
        location=OpenApiParameter.QUERY,
        required=False,
        description=(
            "If present (any value), omits `count`, `next`, and `previous` from the paginated "
            "response, returning only `results`. Useful to skip the COUNT query for large listings."
        ),
    ),
]


class PassthroughRenderer(BaseRenderer):
    """Renderer that returns data as-is, used for HttpResponse actions that bypass DRF serialization."""

    media_type = "text/csv"
    format = "csv"

    def render(self, data: Any, accepted_media_type: str | None = None, renderer_context: dict | None = None) -> Any:
        return data


def parse_period_param(
    period_str: str | None, param_name: str, timezone_str: str, start_date_str: str | None, end_date_str: str | None
) -> tuple[datetime | None, datetime | None, str]:
    """
    Validates ISO date string for query parameters.
    """
    if not period_str:
        return None, None, f"{param_name} is required"
    if period_str not in DateFilter.to_list():
        return None, None, f"Invalid {param_name}: {period_str}. Must be one of: {DateFilter.to_list()}"

    try:
        if period_str == DateFilter.CUSTOM.value:
            validate_custom_period_dates(start_date_str, end_date_str)
            start_date, end_date = DateFilter.custom_range_to_start_date_end_date(
                start_date_str=start_date_str,
                end_date_str=end_date_str,
                tz_string=timezone_str,
            )
        else:
            start_date, end_date = DateFilter.to_start_date_end_date(value=period_str, tz_string=timezone_str)
    except ValueError as e:
        if period_str == DateFilter.CUSTOM.value:
            msg = f"Invalid start_date or end_date: {str(e)}"
        else:
            msg = f"Invalid {param_name}: {period_str}. Error: {str(e)}"
        logger.error(msg)
        return None, None, msg

    return start_date, end_date, ""


def require_date_range(view_func):
    """Decorator that validates and injects start_date/end_date into view kwargs from the period query parameter."""

    @wraps(view_func)
    def wrapper(view, *args, **kwargs) -> Response:
        """Validate date range and delegate to the wrapped view function."""
        # Use request.GET for query parameters (works for DRF and Django)
        period = view.request.GET.get("period") or DateFilter.LAST_30_DAYS.value
        timezone_str = view.request.GET.get("tz", "UTC") or "UTC"
        start_date_str = view.request.GET.get("start_date", None)
        end_date_str = view.request.GET.get("end_date", None)

        start_date, end_date, period_err_msg = parse_period_param(
            period_str=period,
            param_name="period",
            timezone_str=timezone_str,
            start_date_str=start_date_str,
            end_date_str=end_date_str,
        )
        if start_date is None or end_date is None:
            error_response = build_error_response(period_err_msg, status_code=400)
            return Response(error_response, status=status.HTTP_400_BAD_REQUEST)

        if start_date > end_date:
            error_response = build_error_response(
                f"Invalid date range: start_date ({start_date.isoformat()}) must be before end_date ({end_date.isoformat()}).",
                status_code=400,
            )
            return Response(error_response, status=status.HTTP_400_BAD_REQUEST)

        # Inject parsed dates into kwargs for downstream use
        view.kwargs["start_date"] = start_date
        view.kwargs["end_date"] = end_date
        view.kwargs["period"] = period
        view.kwargs["tz"] = timezone_str

        return view_func(view, *args, **kwargs)

    return wrapper


DETAIL_QUERY_PARAMETERS = [
    *PAGINATION_QUERY_PARAMETERS,
    OpenApiParameter(
        name="period",
        type=OpenApiTypes.STR,
        location=OpenApiParameter.QUERY,
        required=False,
        enum=DateFilter.to_list(),
        default=DateFilter.LAST_30_DAYS.value,
        description="Filter for report period (default: last_30_days).",
    ),
    OpenApiParameter(
        name="tz",
        type=OpenApiTypes.STR,
        location=OpenApiParameter.QUERY,
        default="UTC",
        required=True,
        description="Timezone string (default: UTC)",
    ),
    OpenApiParameter(
        name="start_date",
        type=OpenApiTypes.DATE,
        location=OpenApiParameter.QUERY,
        required=False,
        description="Start date in 'YYYY-MM-DD' format. Required when period is 'custom'.",
    ),
    OpenApiParameter(
        name="end_date",
        type=OpenApiTypes.DATE,
        location=OpenApiParameter.QUERY,
        required=False,
        description="End date in 'YYYY-MM-DD' format. Required when period is 'custom'.",
    ),
    OpenApiParameter(
        name="organization",
        type=OpenApiTypes.INT,
        location=OpenApiParameter.QUERY,
        required=False,
        many=True,
        description="Filter by organization ID (multiple allowed)",
    ),
    OpenApiParameter(
        name="template",
        type=OpenApiTypes.INT,
        location=OpenApiParameter.QUERY,
        required=False,
        many=True,
        description="Filter by template ID (multiple allowed)",
    ),
    OpenApiParameter(
        name="label",
        type=OpenApiTypes.INT,
        location=OpenApiParameter.QUERY,
        required=False,
        many=True,
        description="Filter by label ID (multiple allowed)",
    ),
    OpenApiParameter(
        name="project",
        type=OpenApiTypes.INT,
        location=OpenApiParameter.QUERY,
        required=False,
        many=True,
        description="Filter by project ID (multiple allowed)",
    ),
    OpenApiParameter(
        name="job_type",
        type=OpenApiTypes.STR,
        location=OpenApiParameter.QUERY,
        required=False,
        many=True,
        description=(
            "Filter by Controller unified-job model name (for example, job, systemjob, or workflowjob; multiple allowed). "
            "Defaults to job and includes unclassified legacy rows; blank values are ignored."
        ),
    ),
    OpenApiParameter(
        name="or__organization",
        type=OpenApiTypes.INT,
        location=OpenApiParameter.QUERY,
        required=False,
        many=True,
        description="OR filter by organization ID (multiple allowed)",
    ),
    OpenApiParameter(
        name="or__template",
        type=OpenApiTypes.INT,
        location=OpenApiParameter.QUERY,
        required=False,
        many=True,
        description="OR filter by template ID (multiple allowed)",
    ),
    OpenApiParameter(
        name="or__label",
        type=OpenApiTypes.INT,
        location=OpenApiParameter.QUERY,
        required=False,
        many=True,
        description="OR filter by label ID (multiple allowed)",
    ),
    OpenApiParameter(
        name="or__project",
        type=OpenApiTypes.INT,
        location=OpenApiParameter.QUERY,
        required=False,
        many=True,
        description="OR filter by project ID (multiple allowed)",
    ),
    OpenApiParameter(
        name="ordering",
        type=OpenApiTypes.STR,
        location=OpenApiParameter.QUERY,
        required=False,
        description=(
            "Field to order by (e.g. 'template_metadata__template_name', 'successful_runs', 'savings', 'time_savings', etc.). "
            "`order_by` is accepted as an alias for this parameter; if both are given, `ordering` takes precedence."
        ),
    ),
]


@extend_schema_view(
    list=extend_schema(
        summary="Get a list of report data for dashboard (with pagination).",
        description="Returns a paginated report data for dashboard.",
        parameters=DETAIL_QUERY_PARAMETERS,
    ),
    retrieve=extend_schema(
        summary="Not supported for aggregated dashboard report data.",
        description="This endpoint is intentionally not supported and returns 405.",
        responses={
            405: inline_serializer(
                name="DashboardReportRetrieveNotAllowed",
                fields={"detail": serializers.CharField()},
            )
        },
    ),
)
class DashboardReportViewSet(ReadOnlyModelViewSet):
    """
    ViewSet for dashboard reporting and chart data aggregation.
    Provides endpoints for report data, summary, chart, and top users/projects.

    Endpoints:
        GET /api/v1/dashboard_reports/report/ - List all data (paginated)
        GET /api/v1/dashboard_reports/report/details/ - Get  report summary, chart data, and top users/projects
        GET /api/v1/dashboard_reports/report/export/ - Export report data as CSV

    Query Parameters:
        page (int): Page number (default: 1)
        page_size (int): Results per page (default: 25)
        count_disabled (bool): If present (any value), omits `count`/`next`/`previous` from the response.
        period (string): Filter for report start date. Options: 'last_7_days', 'last_14_days', 'last_30_days', 'last_60_days', 'last_90_days', 'custom' (default: 'last_30_days')
        tz (string): Timezone string (default: UTC)
        organization (int): Filter by organization ID (multiple allowed)
        template (int): Filter by job template ID (multiple allowed)
        label (int): Filter by label ID (multiple allowed)
        project (int): Filter by project ID (multiple allowed)
        job_type (string): Filter by Controller model name (e.g. systemjob or workflowjob; defaults to job, including unclassified legacy rows)
        ordering (str): Field to order by (e.g. "template_metadata__template_name", "successful_runs", "savings", "time_savings", etc.)
            `order_by` is accepted as an alias for this parameter; if both are given, `ordering` takes precedence.
        report_type (string): Type of report to export. Options: 'summary', 'roi', 'trends' (default: 'summary', used for export endpoint)
        export_format (string): Export file format. Options: 'csv' (default: 'csv', used for export endpoint)

    Pagination `next`/`previous` links are relative (e.g. `/api/v1/dashboard_reports/report/?page=2`),
    matching the other dashboard_reports endpoints (they all share DashboardReportPagination /
    DefaultPaginator), not absolute URLs.
    """

    permission_classes = [IsDashboardViewer]
    pagination_class = DashboardReportPagination

    detail_query_parameters = DETAIL_QUERY_PARAMETERS

    export_query_parameters = [
        *[p for p in detail_query_parameters if p not in PAGINATION_QUERY_PARAMETERS],
        OpenApiParameter(
            name="report_type",
            type=OpenApiTypes.STR,
            location=OpenApiParameter.QUERY,
            required=False,
            default="summary",
            enum=["summary", "roi", "trends"],
            description="Type of report to export. Options: 'summary', 'roi', 'trends'.",
        ),
        OpenApiParameter(
            name="export_format",
            type=OpenApiTypes.STR,
            location=OpenApiParameter.QUERY,
            required=False,
            default="csv",
            enum=["csv"],
            description="Export file format. Options: 'csv'.",
        ),
    ]

    versioning_class = None  # Disable versioning for this viewset
    serializer_class = ReportSerializer

    filter_backends = [CustomReportFilter, AliasedOrderingFilter]

    # Maximum number of top users/projects to return in details endpoint
    TOP_RESULTS_LIMIT = 5

    ordering_fields: list[str] = [
        "template_metadata__template_name",
        "successful_runs",
        "failed_runs",
        "num_hosts",
        "elapsed",
        "manual_time",
        "manual_costs",
        "automated_costs",
        "savings",
        "runs",
        "time_savings",
    ]

    # `template_name` is the response field name (see ReportSerializer); the aggregated queryset
    # groups by template_metadata__template_name, not JobData.template_name (a per-job, denormalized
    # snapshot from AWX at execution time). Ordering by the raw JobData.template_name column instead
    # would silently widen the GROUP BY and fragment aggregates across historical template renames.
    ordering_field_aliases = {"template_name": "template_metadata__template_name"}

    ordering = ["template_metadata__template_name"]

    def get_serializer_class(self) -> type:
        """Return ReportDetailSerializer for the details action, otherwise the default serializer."""
        if self.action == "details":
            return ReportDetailSerializer
        return super().get_serializer_class()

    def _build_aggregated_queryset(self, base_qs: QuerySet[JobData]) -> QuerySet:
        """
        Applies grouping, cost, and time annotations to a pre-filtered JobData queryset.
        Must be called after all CustomReportFilter filtering has been applied to base_qs,
        because the custom filter methods (after_date, organizations, etc.) are only available
        on JobDataQuerySet, not on the ValuesQuerySet this method returns.
        """
        subscription_cost = SubscriptionCost.get()
        average_cost_employee_minute = subscription_cost.cost_employee_per_minute
        start_date = self.kwargs.get("start_date")
        end_date = self.kwargs.get("end_date")

        aap_subscription_per_second = subscription_cost.per_second_subscription_cost(start_date, end_date)
        enable_template_creation_time = subscription_cost.include_template_creation_time_in_costs

        coalesced_manual_minutes = Coalesce(F("time_taken_manually_execute_minutes"), Value(0))
        coalesced_create_minutes = Coalesce(F("time_taken_create_automation_minutes"), Value(0))

        if enable_template_creation_time:
            automated_costs = (coalesced_create_minutes * average_cost_employee_minute) + (
                F("elapsed") * aap_subscription_per_second
            )
            time_savings = F("manual_time") - F("elapsed") - (coalesced_create_minutes * decimal.Decimal(60))
        else:
            automated_costs = F("elapsed") * aap_subscription_per_second
            time_savings = F("manual_time") - F("elapsed")

        # Manual minutes are a per-job-run estimate (see TemplateMetadata defaults); scale by runs,
        # not Sum(num_hosts), which would mis-apply one run's minute estimate across all hosts.
        manual_costs = F("runs") * coalesced_manual_minutes * average_cost_employee_minute
        manual_time = F("runs") * (coalesced_manual_minutes * 60)

        return (
            # Exclude rows without template_metadata: ReportSerializer.id sources from
            # template_metadata_id, so null rows would cause serialization to fail.
            base_qs.filter(template_metadata_id__isnull=False)
            .values(
                "template_metadata_id",
                "template_metadata__template_name",
                time_taken_manually_execute_minutes=F("template_metadata__time_taken_manually_execute_minutes"),
                time_taken_create_automation_minutes=F("template_metadata__time_taken_create_automation_minutes"),
            )
            .annotate(
                runs=Count("id"),
                successful_runs=Count("id", filter=Q(status=JobStatusChoices.SUCCESSFUL)),
                failed_runs=Count("id", filter=Q(status=JobStatusChoices.FAILED)),
                elapsed=Sum("elapsed"),
                num_hosts=Sum("num_hosts"),
            )
            .annotate(
                automated_costs=automated_costs,
                manual_costs=manual_costs,
                manual_time=manual_time,
            )
            .annotate(
                time_savings=time_savings,
                savings=(F("manual_costs") - F("automated_costs")),
            )
        )

    def get_queryset(self) -> QuerySet:
        """
        Returns the filtered, grouped, and annotated queryset for dashboard reporting.
        CustomReportFilter is applied here on the raw JobDataQuerySet (before .values()),
        so its custom manager methods (after_date, organizations, etc.) are available.
        filter_queryset() then only applies OrderingFilter to the resulting ValuesQuerySet.
        """
        base_qs = self._filter_raw_jobdata_queryset(self._scoped_jobdata_queryset())
        return self._build_aggregated_queryset(base_qs)

    def filter_queryset(self, queryset: QuerySet) -> QuerySet:
        """
        Applies only OrderingFilter to the aggregated queryset.
        CustomReportFilter is intentionally excluded here because it has already been applied
        to the raw JobDataQuerySet inside get_queryset(), before grouping and aggregation.
        Calling it again on the ValuesQuerySet would cause an AttributeError since the custom
        manager methods (after_date, organizations, etc.) are not available post-.values().
        """
        for backend in self.filter_backends:
            if backend is AliasedOrderingFilter:
                queryset = backend().filter_queryset(self.request, queryset, self)
        return queryset

    def _inject_label_ids(self, rows: list[dict], base_qs: QuerySet[JobData]) -> None:
        """Add a label_ids list to each aggregated row by querying JobLabel.

        Runs a single batch query scoped to the same filtered JobData queryset used
        to build the report, so labels only reflect jobs actually included in the
        current date range / org / project / label filters. Mutates each row dict in
        place. Avoids annotating the ValuesQuerySet (which would inflate aggregates
        via the FK JOIN).
        """
        label_map: dict[int, list[int]] = {}
        for item in (
            JobLabel.objects.filter(job_data__in=base_qs, label_id__isnull=False)
            .values("job_data__template_metadata_id", "label_id")
            .distinct()
        ):
            tid = item["job_data__template_metadata_id"]
            if tid is not None:
                label_map.setdefault(tid, []).append(item["label_id"])
        for row in rows:
            row["label_ids"] = label_map.get(row.get("template_metadata_id"), [])

    @require_date_range
    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """
        Returns paginated report data for dashboard, including label IDs per template row.
        """
        base_qs = self._filter_raw_jobdata_queryset(self._scoped_jobdata_queryset())
        queryset = self.filter_queryset(self._build_aggregated_queryset(base_qs))
        page = self.paginate_queryset(queryset)
        if page is not None:
            self._inject_label_ids(page, base_qs)
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        rows = list(queryset)
        self._inject_label_ids(rows, base_qs)
        serializer = self.get_serializer(rows, many=True)
        return Response(serializer.data)

    def retrieve(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """
        Not supported: this viewset returns aggregated-per-template data, not individual JobData records.
        Inheriting retrieve from ReadOnlyModelViewSet would cause a PostgreSQL error because get_object()
        appends .filter(pk=...) to a ValuesQuerySet grouped by template_metadata_id, and the job id
        column is absent from the GROUP BY clause.
        """
        return Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)

    def _get_date_range_and_kind(self) -> tuple[datetime | None, datetime | None, str | None]:
        """
        Determine the chart time granularity (hour/day/month/year) from the injected date range.

        Normalizes start_date and end_date to UTC and snaps start_date to the appropriate boundary
        (e.g. hour=0 for day-level, day=1 for month-level). end_date is advanced to the start of
        the *next* period boundary so that generate_series always emits a bucket for the final
        period — truncating end_date backward would omit data that falls after the truncated
        boundary (e.g. a "year" end of Dec 2025 truncated to Jan 1 2025 could lose the 2025 bucket
        when generate_series stops exactly there). Returns (start_date, end_date, kind).
        """
        start_date = self.kwargs.get("start_date")
        end_date = self.kwargs.get("end_date")
        if start_date is None or end_date is None:
            return None, None, None

        start_date = start_date.astimezone(UTC)
        end_date = end_date.astimezone(UTC)

        diff = abs(end_date - start_date)

        diff_relative = relativedelta(end_date, start_date)
        total_months = diff_relative.months + diff_relative.years * 12

        # Trigger 'year' kind for any range spanning different years
        if total_months > 12:
            kind = "year"
            start_date = start_date.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
            # Advance to Jan 1 of the *next* year so the current year's bucket is included.
            end_date = end_date.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0) + relativedelta(
                years=1
            )
        elif diff.days < 1:
            # Use hourly bucketing only for ranges UNDER 24 hours (< 1 day).
            # Exactly 24-hour ranges (diff.days == 1) fall through to daily bucketing below.
            # This is intentional: a 24h range like "June 15 10:00 → June 16 10:00" spans
            # 2 calendar days, so daily bucketing prevents data misattribution.
            kind = "hour"
            start_date = start_date.replace(minute=0, second=0, microsecond=0)
            # Advance to the start of the *next* hour so the current hour's bucket is included.
            end_date = end_date.replace(minute=0, second=0, microsecond=0) + relativedelta(hours=1)
        elif diff.days <= 45:
            kind = "day"
            start_date = start_date.replace(hour=0, minute=0, second=0, microsecond=0)
            # Advance to midnight of the *next* day so today's bucket is included.
            end_date = end_date.replace(hour=0, minute=0, second=0, microsecond=0) + relativedelta(days=1)
        else:
            kind = "month"
            start_date = start_date.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            # Advance to the 1st of the *next* month so the current month's bucket is included.
            end_date = end_date.replace(day=1, hour=0, minute=0, second=0, microsecond=0) + relativedelta(months=1)
        return start_date, end_date, kind

    def _scoped_jobdata_queryset(self) -> QuerySet[JobData]:
        """Org-scoped JobData base queryset: dashboard role-holders only see their organizations' job data."""
        qs = JobData.objects.all()
        awx_org_ids = permitted_awx_view_org_ids(self.request.user)
        if awx_org_ids is not None:
            qs = qs.filter(organization_id__in=awx_org_ids)
        return qs

    def _filter_raw_jobdata_queryset(self, queryset: QuerySet[JobData]) -> QuerySet[JobData]:
        """Apply all filter backends except OrderingFilter to the raw JobDataQuerySet."""
        for backend in self.filter_backends:
            if backend is AliasedOrderingFilter:
                continue
            queryset = backend().filter_queryset(self.request, queryset, self)
        return queryset

    def _prepare_chart_querysets(self, kind: str) -> tuple[QuerySet, QuerySet]:
        """
        Prepares job and host chart querysets for the given kind.
        Returns: (job_chart_qs, host_chart_qs)
        """
        qs = self._filter_raw_jobdata_queryset(self._scoped_jobdata_queryset())
        qs = qs.values(date=Trunc(expression="finished", kind=kind, output_field=models.DateTimeField())).filter(
            date=OuterRef("term")
        )
        job_chart_qs = qs.annotate(runs=Count("id")).values("runs").order_by()
        host_chart_qs = qs.annotate(num_hosts=Sum("num_hosts")).values("num_hosts").order_by()
        return job_chart_qs, host_chart_qs

    def _format_chart_result(self, date_sequence_queryset: QuerySet) -> dict[str, Any]:
        """
        Formats the chart result from the date sequence queryset.
        Returns: dict with host_chart and job_chart.
        """
        result = {"host_chart": {"kind": "", "items": []}, "job_chart": {"kind": "", "items": []}}
        for row in date_sequence_queryset:
            result["job_chart"]["items"].append({"label": row.term, "value": row.runs})
            result["host_chart"]["items"].append({"label": row.term, "value": row.hosts})
        return result

    def get_chart_data(self) -> dict[str, Any]:
        """
        Returns chart data for jobs and hosts over a time range, grouped by kind (hour, day, month, year).
        """
        start_date, end_date, kind = self._get_date_range_and_kind()
        if not start_date or not end_date or not kind:
            return {"host_chart": {"kind": "", "items": []}, "job_chart": {"kind": "", "items": []}}

        job_chart_qs, host_chart_qs = self._prepare_chart_querysets(kind)

        # Generate a time series from start_date to end_date using PostgreSQL's generate_series function.
        # - start/stop: Define the date range boundaries
        # - step: Interval between each point (e.g., "1 hours", "1 days", "1 months", "1 years")
        # - span: Number of intervals to include beyond the stop date (buffer for edge cases)
        # - output_field: Ensures the series generates DateTimeField values
        # The resulting queryset contains a 'term' column with each timestamp in the series.
        date_sequence_queryset = generate_series(
            start=start_date, stop=end_date, step=f"1 {kind}s", span=5, output_field=models.DateTimeField
        ).annotate(
            # For each time point in the series, fetch the count of job runs (or 0 if none)
            runs=Coalesce(Subquery(job_chart_qs), Value(0)),
            # For each time point in the series, fetch the sum of hosts (or 0 if none)
            hosts=Coalesce(Subquery(host_chart_qs), Value(0)),
        )

        result = self._format_chart_result(date_sequence_queryset)
        result["host_chart"]["kind"] = kind
        result["job_chart"]["kind"] = kind
        return result

    @staticmethod
    def _csv_safe(value: Any) -> Any:
        """
        Prepend a single quote to any string value that starts with special
        characters to prevent CSV injection.
        """
        if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
            return f"'{value}"
        return value

    @staticmethod
    def _format_currency(value: decimal.Decimal | float) -> str:
        round_value = round(value, 2)
        return f"${round_value:,.2f}" if round_value >= 0 else f"-${abs(round_value):,.2f}"

    def _write_summary_csv(self, response: HttpResponse, qs: QuerySet[JobData]) -> None:
        """
        Write summary data to CSV, including template name, number of job executions,
        hosts executed on, time taken to execute manually, time taken to create
        automation (if enabled), running time, automated costs, manual costs, and savings.
        """
        enable_template_creation_time = SubscriptionCost.get().include_template_creation_time_in_costs

        writer = csv.writer(response)
        headers = [
            "Name",
            "Number of job executions",
            "Hosts executions",
            "Time taken to manually execute (minutes)",
        ]

        if enable_template_creation_time:
            headers.append("Time taken to create automation (minutes)")

        headers += [
            "Running time (seconds)",
            "Running time",
            "Time Saved (hours)",
            "Automated costs",
            "Manual costs",
            "Savings",
        ]

        writer.writerow(headers)
        for job in qs:
            row = [
                self._csv_safe(
                    job["template_metadata__template_name"]
                ),  # Escape template name to prevent CSV injection
                job["runs"],
                job["num_hosts"],
                job["time_taken_manually_execute_minutes"],
            ]
            if enable_template_creation_time:
                row.append(job["time_taken_create_automation_minutes"])
            row += [
                job["elapsed"],
                sec2time(job["elapsed"]),
                round(job["time_savings"] / 3600, 2),
                self._format_currency(job["automated_costs"]),
                self._format_currency(job["manual_costs"]),
                self._format_currency(job["savings"]),
            ]
            writer.writerow(row)

    def _write_roi_csv(self, response: HttpResponse, qs: QuerySet[JobData]) -> None:
        """
        Write ROI data to CSV, including total cost savings, time savings, automation
        cost, manual cost equivalent, and ROI percentage.
        """
        aggregate = qs.aggregate(
            total_automated_costs=Coalesce(Sum("automated_costs"), Value(decimal.Decimal("0"))),
            total_manual_costs=Coalesce(Sum("manual_costs"), Value(decimal.Decimal("0"))),
            total_savings=Coalesce(Sum("savings"), Value(decimal.Decimal("0"))),
            total_time_savings=Coalesce(Sum("time_savings"), Value(decimal.Decimal("0"))),
        )

        automated_costs = aggregate["total_automated_costs"]
        manual_costs = aggregate["total_manual_costs"]
        savings = aggregate["total_savings"]
        time_saved_hours = round(aggregate["total_time_savings"] / 3600, 2)
        roi_percentage = round((savings / automated_costs) * 100, 2) if automated_costs else decimal.Decimal("0")
        automation_value = manual_costs + savings  # manual_cost_equivalent + cost_savings

        writer = csv.writer(response)
        writer.writerow(
            [
                "Cost Savings",
                "Time Saved (hours)",
                "Automation Cost",
                "Manual Cost Equivalent",
                "ROI Percentage",
                "Automation Value",
            ]
        )
        writer.writerow(
            [
                self._format_currency(savings),
                time_saved_hours,
                self._format_currency(automated_costs),
                self._format_currency(manual_costs),
                roi_percentage,
                self._format_currency(automation_value),
            ]
        )

    def _write_trends_csv(self, response: HttpResponse, base_qs: QuerySet[JobData]) -> None:
        """Write trends data to CSV, grouped by the time granularity derived from the date range."""
        _, _, kind = self._get_date_range_and_kind()
        if not kind:
            logger.warning("Could not determine time granularity for trends report; defaulting to 'day'.")
            kind = "day"

        trends_qs = (
            base_qs.values(date=Trunc(expression="finished", kind=kind, output_field=models.DateTimeField()))
            .annotate(
                runs=Count("id"),
                successful_runs=Count("id", filter=Q(status=JobStatusChoices.SUCCESSFUL)),
                failed_runs=Count("id", filter=Q(status=JobStatusChoices.FAILED)),
                total_elapsed=Sum("elapsed"),
                total_hosts=Sum("num_hosts"),
            )
            .order_by("date")
        )

        writer = csv.writer(response)

        date_formats = {
            "hour": "%Y-%m-%d %H:00",
            "day": "%Y-%m-%d",
            "week": "%Y-%m-%d",
            "month": "%Y-%m",
            "year": "%Y",
        }
        writer.writerow(
            [
                "Date",
                "Number of Job Executions",
                "Successful Runs",
                "Failed Runs",
                "Total Elapsed (seconds)",
                "Total Hosts",
            ]
        )
        for row in trends_qs:
            writer.writerow(
                [
                    row["date"].strftime(date_formats[kind]),
                    row["runs"],
                    row["successful_runs"],
                    row["failed_runs"],
                    row["total_elapsed"],
                    row["total_hosts"],
                ]
            )

    @extend_schema(
        summary="Returns summary, chart data, and top users/projects for dashboard",
        description="Returns summary, chart data, and top users/projects for dashboard",
        parameters=detail_query_parameters,
    )
    @action(detail=False, methods=["get"], url_path="details")
    @require_date_range
    def details(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """
        Returns summary, chart data, and top users/projects for dashboard.
         - Summary includes total runs, successful runs, failed runs, total time savings, and total cost savings.
         - Chart data provides aggregated metrics by job template for visualizations.
         - Top users and projects identify the most active users and projects in the given date range.
         All data is filtered by the provided date range and any additional filters (organization, template, label, project).
        """
        filtered_qs = self._filter_raw_jobdata_queryset(self._scoped_jobdata_queryset())

        ### TOP USERS ###
        top_users_qs = (
            filtered_qs.filter(launched_by_id__isnull=False)
            .values("launched_by_id", "launched_by_username")
            .annotate(count=Count("id"))
            .order_by("-count", "launched_by_id")[: self.TOP_RESULTS_LIMIT]
        )

        ### TOP PROJECTS ###
        top_projects_qs = (
            filtered_qs.filter(project_id__isnull=False)
            .values("project_id", "project_name")
            .annotate(count=Count("id"))
            .order_by("-count", "project_id")[: self.TOP_RESULTS_LIMIT]
        )

        ### AGGREGATED DATA ###
        # Build the aggregated queryset from the already-filtered raw queryset so that
        # CustomReportFilter's custom manager methods are not called on a ValuesQuerySet.
        qs = self._build_aggregated_queryset(filtered_qs)
        report_data_qs = qs.aggregate(
            total_num_hosts=Coalesce(Sum("num_hosts"), Value(0)),
            total_elapsed=Coalesce(Sum("elapsed"), Value(decimal.Decimal("0"))),
            total_manual_time=Coalesce(Sum("manual_time"), Value(0)),
            total_manual_costs=Coalesce(Sum("manual_costs"), Value(decimal.Decimal("0"))),
            total_automated_costs=Coalesce(Sum("automated_costs"), Value(decimal.Decimal("0"))),
            total_savings=Coalesce(Sum("savings"), Value(decimal.Decimal("0"))),
            total_time_savings=Coalesce(Sum("time_savings"), Value(decimal.Decimal("0"))),
        )

        # Card counts (total_runs/successful/failed) are computed directly on filtered_qs
        # instead of via _build_aggregated_queryset, because that method drops rows with
        # template_metadata_id=NULL (needed for cost/time annotations). Counting on the raw
        # filtered queryset keeps the summary cards consistent with the unfiltered job listing
        # (AAP-85129).
        card_counts = filtered_qs.aggregate(
            total_runs=Coalesce(Count("id"), Value(0)),
            total_successful_runs=Coalesce(Count("id", filter=Q(status=JobStatusChoices.SUCCESSFUL)), Value(0)),
            total_failed_runs=Coalesce(Count("id", filter=Q(status=JobStatusChoices.FAILED)), Value(0)),
        )
        report_data_qs.update(card_counts)

        ### Unique hosts count ###
        # Use a stable surrogate key that prefers host_id (cast to text) when present and
        # falls back to host_name, so that two hosts sharing a name aren't collapsed into one,
        # and a renamed host isn't double-counted.
        unique_hosts_count = (
            JobHostSummary.objects.filter(job_data__in=filtered_qs)
            .annotate(
                stable_host=Case(
                    When(host_id__isnull=False, then=Cast(F("host_id"), output_field=models.TextField())),
                    default=F("host_name"),
                    output_field=models.TextField(),
                )
            )
            .values("stable_host")
            .distinct()
            .count()
        )

        ### CHART DATA ###
        chart_data = self.get_chart_data()

        ### Serialize data ###
        report_data = self.get_serializer(
            {
                **report_data_qs,
                "top_users": top_users_qs,
                "top_projects": top_projects_qs,
                "total_number_of_unique_hosts": unique_hosts_count,
                **chart_data,
            }
        ).data

        return Response(report_data, status=status.HTTP_200_OK)

    @extend_schema(
        summary="Exports report data as CSV",
        description="Exports report data as CSV based on the specific report type and format query parameters.",
        parameters=export_query_parameters,
        responses={
            (200, "text/csv; charset=UTF-8"): str,
            400: inline_serializer(
                name="DashboardReportExportErrorSerializer",
                fields={
                    "detail": serializers.CharField(required=False),
                    "error": serializers.CharField(required=False),
                },
            ),
        },
    )
    @action(methods=["get"], detail=False, url_path="export", renderer_classes=[PassthroughRenderer])
    @require_date_range
    def export(self, request: Request, *args: Any, **kwargs: Any) -> HttpResponse | JsonResponse:
        """Exports report data as CSV based on the specified report type and format query parameters."""

        # Validate report_type and export_format parameters
        report_type = request.query_params.get("report_type", "summary")
        export_format = request.query_params.get("export_format", "csv")

        if export_format != "csv":
            return JsonResponse(
                {"detail": f"Invalid export format: {export_format}. Only 'csv' is supported."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if report_type not in ("summary", "roi", "trends"):
            return JsonResponse(
                {"detail": f"Invalid report_type: {report_type}. Must be one of: summary, roi, trends."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Do the export logic
        end_date = self.kwargs.get("end_date")
        filename = f"automation-dashboard-{report_type}-{end_date.date().isoformat()}.csv"
        response = HttpResponse(
            content_type="text/csv; charset=UTF-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
        base_qs = self._filter_raw_jobdata_queryset(self._scoped_jobdata_queryset())
        qs = self.filter_queryset(self._build_aggregated_queryset(base_qs))

        if export_format == "csv":  # This check is here for future extensibility when we add PDF export
            if report_type == "summary":
                self._write_summary_csv(response, qs)  # summary report is based on the aggregated queryset
            elif report_type == "roi":
                self._write_roi_csv(response, qs)  # roi report is also based on the aggregated queryset
            elif report_type == "trends":
                self._write_trends_csv(
                    response, base_qs
                )  # trends report needs to be based on the filtered but not yet aggregated queryset to group by time periods

        return response
