import decimal
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.http import QueryDict
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory

from apps.dashboard_reports.viewsets import DashboardReportViewSet
from apps.dashboard_reports.viewsets.dashboard_report import parse_period_param
from tests.unit.dashboard_reports.conftest import DummyView


# Unit tests for parse_period_param function
@pytest.mark.unit
class TestParsePeriodParam:
    # Test valid ISO date string
    @patch("apps.dashboard_reports.filters.DateFilter.to_start_date_end_date")
    def test_valid_iso_date(self, mock_to_dates):
        mock_start, mock_end = (
            datetime(2026, 3, 4, 0, 0, tzinfo=UTC),
            datetime(2026, 3, 11, 0, 0, tzinfo=UTC),
        )
        mock_to_dates.return_value = mock_start, mock_end
        period_str = "last_7_days"

        start_date, end_date, msg = parse_period_param(period_str, "period", "UTC", None, None)
        assert isinstance(start_date, datetime)
        assert isinstance(end_date, datetime)
        assert msg == ""
        assert start_date == mock_start
        assert end_date == mock_end

    # Test invalid date string
    def test_invalid_period(self):
        period_str = "not-a-period"
        start_date, end_date, msg = parse_period_param(period_str, "period", "UTC", None, None)
        assert start_date is None
        assert end_date is None
        assert (
            "Must be one of: ['last_7_days', 'last_14_days', 'last_30_days', 'last_60_days', 'last_90_days', 'custom']"
            in msg
        )

    # Test empty string
    def test_empty_string(self):
        start_date, end_date, msg = parse_period_param("", "period", "UTC", None, None)
        assert start_date is None
        assert end_date is None
        assert msg == "period is required"

    # Test None as input
    def test_none_string(self):
        start_date, end_date, msg = parse_period_param(None, "period", "UTC", None, None)
        assert start_date is None
        assert end_date is None
        assert msg == "period is required"

    # Test leap year date
    @patch("apps.dashboard_reports.filters.DateFilter.to_start_date_end_date")
    def test_leap_year(self, mock_to_dates):
        mock_start, mock_end = (
            datetime(2024, 2, 22, 0, 0, tzinfo=UTC),
            datetime(2024, 2, 29, 0, 0, tzinfo=UTC),  # leap day as end date
        )
        mock_to_dates.return_value = mock_start, mock_end
        period_str = "last_7_days"

        start_date, end_date, msg = parse_period_param(period_str, "period", "UTC", None, None)
        assert isinstance(start_date, datetime)
        assert isinstance(end_date, datetime)
        assert msg == ""
        assert start_date == mock_start
        assert end_date == mock_end

    # Test ISO date with timezone
    @patch("apps.dashboard_reports.filters.DateFilter.to_start_date_end_date")
    def test_timezone(self, mock_to_dates):
        """Timezone-aware datetimes returned from DateFilter are passed through correctly."""
        mock_start, mock_end = (
            datetime(2026, 3, 4, 0, 0, tzinfo=UTC),
            datetime(2026, 3, 11, 12, 34, 56, tzinfo=UTC),
        )
        mock_to_dates.return_value = mock_start, mock_end

        period_str = "last_7_days"
        start_date, end_date, msg = parse_period_param(period_str, "period", "UTC", None, None)
        assert isinstance(start_date, datetime)
        assert isinstance(end_date, datetime)
        assert msg == ""
        assert start_date == mock_start
        assert end_date == mock_end

    # Test boundary date
    @patch("apps.dashboard_reports.filters.DateFilter.to_start_date_end_date")
    def test_boundary(self, mock_to_dates):
        mock_start, mock_end = (
            datetime(1970, 1, 1, 0, 0, tzinfo=UTC),
            datetime(1960, 1, 1, 0, 0, tzinfo=UTC),
        )
        mock_to_dates.return_value = mock_start, mock_end
        period_str = "last_90_days"

        start_date, end_date, msg = parse_period_param(period_str, "period", "UTC", None, None)
        assert isinstance(start_date, datetime)
        assert isinstance(end_date, datetime)
        assert msg == ""
        assert start_date == mock_start
        assert end_date == mock_end

    @patch("apps.dashboard_reports.filters.DateFilter.custom_range_to_start_date_end_date")
    def test_custom_period_with_valid_dates(self, mock_custom_range):
        """parse_period_param returns dates when period=custom with valid start/end."""
        mock_start = datetime(2024, 6, 1, 0, 0, tzinfo=UTC)
        mock_end = datetime(2024, 6, 30, 23, 59, 59, tzinfo=UTC)
        mock_custom_range.return_value = (mock_start, mock_end)

        start_date, end_date, msg = parse_period_param("custom", "period", "UTC", "2024-06-01", "2024-06-30")

        assert start_date == mock_start
        assert end_date == mock_end
        assert msg == ""
        mock_custom_range.assert_called_once_with(
            start_date_str="2024-06-01", end_date_str="2024-06-30", tz_string="UTC"
        )

    def test_custom_period_missing_start_date(self):
        """parse_period_param returns error when period=custom and start_date is None."""
        start_date, end_date, msg = parse_period_param("custom", "period", "UTC", None, "2024-06-30")

        assert start_date is None
        assert end_date is None
        assert "start_date and end_date are required" in msg

    def test_custom_period_missing_end_date(self):
        """parse_period_param returns error when period=custom and end_date is None."""
        start_date, end_date, msg = parse_period_param("custom", "period", "UTC", "2024-06-01", None)

        assert start_date is None
        assert end_date is None
        assert "start_date and end_date are required" in msg

    def test_custom_period_missing_both_dates(self):
        """parse_period_param returns error when period=custom and both dates are None."""
        start_date, end_date, msg = parse_period_param("custom", "period", "UTC", None, None)

        assert start_date is None
        assert end_date is None
        assert "start_date and end_date are required" in msg

    def test_custom_period_invalid_date_format_returns_error(self):
        """parse_period_param returns error tuple when start_date has invalid format."""
        start_date, end_date, msg = parse_period_param("custom", "period", "UTC", "not-a-date", "2024-06-30")

        assert start_date is None
        assert end_date is None
        assert "Invalid start_date or end_date" in msg
        assert "Invalid date format" in msg

    def test_custom_period_invalid_timezone_returns_error(self):
        """parse_period_param returns error tuple when timezone is invalid."""
        start_date, end_date, msg = parse_period_param("custom", "period", "Bad/Zone", "2024-06-01", "2024-06-30")

        assert start_date is None
        assert end_date is None
        assert "Invalid start_date or end_date" in msg
        assert "Invalid timezone" in msg

    def test_non_custom_period_invalid_timezone_returns_error(self):
        """parse_period_param returns error tuple when timezone is invalid for non-custom period."""
        start_date, end_date, msg = parse_period_param("last_7_days", "period", "Invalid/TZ", None, None)

        assert start_date is None
        assert end_date is None
        assert "Invalid period: last_7_days" in msg
        assert "Error:" in msg
        assert "Invalid timezone" in msg


# Unit tests for require_date_range decorator
@pytest.mark.unit
class TestRequireDateRange:
    @pytest.fixture
    def factory(self):
        return APIRequestFactory()

    # Test valid start/end dates
    @patch("apps.dashboard_reports.filters.DateFilter.to_start_date_end_date")
    def test_valid_periods(self, mock_to_dates, factory):
        """A valid period passes validation and calls the wrapped view."""
        view = DummyView()
        start_date = "2026-03-11T12:00:00"
        end_date = "2026-03-12T12:00:00"
        mock_start, mock_end = (
            start_date,
            end_date,
        )
        mock_to_dates.return_value = mock_start, mock_end

        request = factory.get(
            "/", {"period": "last_7_days", "tz": "UTC", "start_date": start_date, "end_date": end_date}
        )
        view.request = request
        response = view.view(request)
        assert response.status_code == 200
        assert response.data["success"] is True
        assert view.called

    # Test missing period defaults to last_30_days
    def test_missing_period_defaults_to_last_30_days(self, factory):
        """A missing period defaults to last_30_days with a 30-day window."""
        view = DummyView()
        request = factory.get("/")
        view.request = request
        response = view.view(request)
        assert response.status_code == 200
        assert view.called
        assert view.kwargs["period"] == "last_30_days"
        assert (view.kwargs["end_date"] - view.kwargs["start_date"]).days == 30

    # Test empty period defaults to last_30_days
    def test_empty_period_defaults_to_last_30_days(self, factory):
        """An empty period defaults to last_30_days."""
        view = DummyView()
        request = factory.get("/", {"period": ""})
        view.request = request
        response = view.view(request)
        assert response.status_code == 200
        assert view.kwargs["period"] == "last_30_days"

    # Test explicit period is not replaced by the default
    def test_explicit_period_is_not_replaced_by_default(self, factory):
        """An explicit period keeps its own value and window instead of the default."""
        view = DummyView()
        request = factory.get("/", {"period": "last_7_days"})
        view.request = request
        response = view.view(request)
        assert response.status_code == 200
        assert view.kwargs["period"] == "last_7_days"
        assert (view.kwargs["end_date"] - view.kwargs["start_date"]).days == 7

    # Test invalid period format
    def test_invalid_period_format(self, factory):
        """An unknown period returns 400 listing the allowed values and skips the view."""
        view = DummyView()
        request = factory.get("/", {"period": "invalid-period"})
        view.request = request
        response = view.view(request)
        assert response.status_code == 400
        assert (
            "Must be one of: ['last_7_days', 'last_14_days', 'last_30_days', 'last_60_days', 'last_90_days', 'custom']"
            in response.data["error"]
        )
        assert not view.called

    # Test leap year dates
    @patch("apps.dashboard_reports.filters.DateFilter.to_start_date_end_date")
    def test_leap_year(self, mock_to_dates, factory):
        view = DummyView()
        start_date, end_date = "2024-02-29T00:00:00", "2024-03-01T00:00:00"
        mock_start, mock_end = start_date, end_date
        mock_to_dates.return_value = mock_start, mock_end

        request = factory.get(
            "/",
            {
                "period": "last_7_days",
                "tz": "UTC",
                "start_date": start_date,
                "end_date": end_date,
            },
        )
        view.request = request
        response = view.view(request)
        assert response.status_code == 200
        assert response.data["success"] is True
        assert view.called

    # Test timezone-aware dates
    @patch("apps.dashboard_reports.filters.DateFilter.to_start_date_end_date")
    def test_timezone(self, mock_to_dates, factory):
        view = DummyView()
        start_date, end_date = "2026-03-11T12:00:00+00:00", "2026-03-12T12:00:00+00:00"
        mock_start, mock_end = start_date, end_date
        mock_to_dates.return_value = mock_start, mock_end

        request = factory.get(
            "/",
            {
                "period": "last_7_days",
                "tz": "UTC",
                "start_date": start_date,
                "end_date": end_date,
            },
        )
        view.request = request
        response = view.view(request)
        assert response.status_code == 200
        assert response.data["success"] is True
        assert view.called

    # Test tz param missing
    @patch("apps.dashboard_reports.filters.DateFilter.to_start_date_end_date")
    def test_tz_param_empty(self, mock_to_dates, factory):
        view = DummyView()
        start_date, end_date = "2026-03-11T12:00:00+00:00", "2026-03-12T12:00:00+00:00"
        mock_start, mock_end = start_date, end_date
        mock_to_dates.return_value = mock_start, mock_end

        request = factory.get(
            "/",
            {
                "period": "last_7_days",
                "tz": "",
                "start_date": start_date,
                "end_date": end_date,
            },
        )
        view.request = request
        response = view.view(request)
        assert response.status_code == 200
        assert response.data["success"] is True
        assert view.called
        mock_to_dates.assert_called_once_with(value="last_7_days", tz_string="UTC")

    @patch("apps.dashboard_reports.filters.DateFilter.to_start_date_end_date")
    def test_tz_param_missing(self, mock_to_dates, factory):
        view = DummyView()
        start_date, end_date = "2026-03-11T12:00:00+00:00", "2026-03-12T12:00:00+00:00"
        mock_start, mock_end = start_date, end_date
        mock_to_dates.return_value = mock_start, mock_end

        request = factory.get(
            "/",
            {
                "period": "last_7_days",
                "start_date": start_date,
                "end_date": end_date,
            },
        )
        view.request = request
        response = view.view(request)
        assert response.status_code == 200
        assert response.data["success"] is True
        assert view.called
        mock_to_dates.assert_called_once_with(value="last_7_days", tz_string="UTC")


# Unit tests for require_date_range decorator — custom period branch (new in this branch)
@pytest.mark.unit
class TestRequireDateRangeCustomPeriod:
    @pytest.fixture
    def factory(self):
        return APIRequestFactory()

    @patch("apps.dashboard_reports.filters.DateFilter.custom_range_to_start_date_end_date")
    def test_custom_period_with_valid_dates_returns_200(self, mock_custom_range, factory):
        """require_date_range passes through and injects dates when period=custom with valid dates."""
        mock_start = datetime(2024, 6, 1, 0, 0, tzinfo=UTC)
        mock_end = datetime(2024, 6, 30, 23, 59, 59, tzinfo=UTC)
        mock_custom_range.return_value = (mock_start, mock_end)

        view = DummyView()
        request = factory.get(
            "/", {"period": "custom", "start_date": "2024-06-01", "end_date": "2024-06-30", "tz": "UTC"}
        )
        view.request = request
        response = view.view(request)

        assert response.status_code == 200
        assert view.called

    def test_custom_period_missing_start_date_returns_400(self, factory):
        """require_date_range returns 400 when period=custom and start_date is missing."""
        view = DummyView()
        request = factory.get("/", {"period": "custom", "end_date": "2024-06-30"})
        view.request = request
        response = view.view(request)

        assert response.status_code == 400
        assert "start_date and end_date are required" in response.data["error"]
        assert not view.called

    def test_custom_period_missing_end_date_returns_400(self, factory):
        """require_date_range returns 400 when period=custom and end_date is missing."""
        view = DummyView()
        request = factory.get("/", {"period": "custom", "start_date": "2024-06-01"})
        view.request = request
        response = view.view(request)

        assert response.status_code == 400
        assert "start_date and end_date are required" in response.data["error"]
        assert not view.called

    def test_custom_period_missing_both_dates_returns_400(self, factory):
        """require_date_range returns 400 when period=custom and both dates are missing."""
        view = DummyView()
        request = factory.get("/", {"period": "custom"})
        view.request = request
        response = view.view(request)

        assert response.status_code == 400
        assert not view.called


@pytest.mark.django_db
@pytest.mark.unit
class TestDashboardReportViewSet:
    @pytest.fixture(autouse=True)
    def bypass_org_scope(self):
        """These tests exercise aggregation mechanics; the org scope is covered separately."""
        with patch("apps.dashboard_reports.viewsets.dashboard_report.permitted_awx_view_org_ids", return_value=None):
            yield

    @pytest.fixture
    def factory(self):
        return APIRequestFactory()

    @pytest.fixture
    def viewset(self):
        return DashboardReportViewSet()

    # Test default serializer class
    def test_get_serializer_class_default(self, viewset):
        viewset.action = None
        assert viewset.get_serializer_class() == viewset.serializer_class

    # Test details serializer class
    def test_get_serializer_class_details(self, viewset):
        viewset.action = "details"
        from apps.dashboard_reports.serializers import ReportDetailSerializer

        assert viewset.get_serializer_class() == ReportDetailSerializer

    # Test get_queryset with mocks
    @patch("apps.dashboard_reports.viewsets.dashboard_report.SubscriptionCost.get")
    @patch("apps.dashboard_reports.viewsets.dashboard_report.JobData.objects")
    def test_get_queryset(self, mock_jobdata, mock_subcost, viewset):
        mock_subcost.return_value.cost_employee_per_minute = 1
        mock_subcost.return_value.per_second_subscription_cost.return_value = 0.01
        mock_subcost.return_value.include_template_creation_time_in_costs = True
        # get_queryset calls .filter() then chains three .annotate() calls on the base queryset
        mock_jobdata.filter.return_value.values.return_value.annotate.return_value.annotate.return_value.annotate.return_value = [
            "mocked"
        ]
        now = datetime.now(tz=UTC)
        viewset.kwargs = {
            "period": "last_7_days",
            "tz": "UTZ",
            "start_date": now - timedelta(days=7),
            "end_date": now,
        }
        viewset.request = MagicMock()
        # Patch _filter_raw_jobdata_queryset to return mock_jobdata so that
        # _build_aggregated_queryset receives it and the mock chain above resolves.
        with patch.object(viewset, "_filter_raw_jobdata_queryset", return_value=mock_jobdata):
            result = viewset.get_queryset()
        assert result == ["mocked"]

    # Test _get_date_range_and_kind with None
    def test__get_date_range_and_kind_none(self, viewset):
        viewset.kwargs = {}
        start, end, kind = viewset._get_date_range_and_kind()
        assert start is None and end is None and kind is None

    # Test _get_date_range_and_kind for day (14 days <= 45, same year)
    def test__get_date_range_and_kind_day(self, viewset):
        # _get_date_range_and_kind reads start_date/end_date directly from kwargs
        viewset.kwargs = {
            "period": "last_14_days",
            "start_date": datetime(2026, 3, 1, 0, 0, tzinfo=UTC),
            "end_date": datetime(2026, 3, 11, 15, 30, tzinfo=UTC),
        }
        start, end, kind = viewset._get_date_range_and_kind()
        assert kind == "day"
        assert start == datetime(2026, 3, 1, 0, 0, tzinfo=UTC)
        # end_date is advanced to midnight of the *next* day (Mar 12) so Mar 11's bucket is included.
        assert end == datetime(2026, 3, 12, 0, 0, tzinfo=UTC)

    # Test _get_date_range_and_kind for month (60 days > 45 threshold -> else branch)
    def test__get_date_range_and_kind_month(self, viewset):
        viewset.kwargs = {
            "period": "last_60_days",
            "start_date": datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
            "end_date": datetime(2026, 3, 11, 15, 30, tzinfo=UTC),
        }
        start, end, kind = viewset._get_date_range_and_kind()
        assert kind == "month"
        assert start == datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        # end_date advances to the 1st of the *next* month (Apr 1) so the March bucket is included.
        assert end == datetime(2026, 4, 1, 0, 0, tzinfo=UTC)

    # Test _get_date_range_and_kind for year
    def test__get_date_range_and_kind_year(self, viewset):
        viewset.kwargs = {
            "period": "last_90_days",
            "start_date": datetime(2024, 12, 1, 0, 0, tzinfo=UTC),
            "end_date": datetime(2026, 1, 31, 0, 0, tzinfo=UTC),
        }
        start, end, kind = viewset._get_date_range_and_kind()
        assert kind == "year"
        assert start == datetime(2024, 1, 1, 0, 0, tzinfo=UTC)
        # end_date advances to Jan 1 of the *next* year (2027) so the 2026 bucket is included.
        assert end == datetime(2027, 1, 1, 0, 0, tzinfo=UTC)

    # Test that the 'year' kind end_date no longer truncates to Jan 1 of the *same* year,
    # which would cause the last year's data to have no chart bucket.
    def test__get_date_range_and_kind_year_end_date_advanced_not_truncated(self, viewset):
        """Regression: end_date Dec 2025 must NOT become Jan 1 2025 (same year)."""
        viewset.kwargs = {
            "period": "last_90_days",
            "start_date": datetime(2023, 11, 1, 0, 0, tzinfo=UTC),
            "end_date": datetime(2025, 12, 31, 23, 59, tzinfo=UTC),
        }
        start, end, kind = viewset._get_date_range_and_kind()
        assert kind == "year"
        # end_date must be Jan 1 2026 (next year), NOT Jan 1 2025 (same year as input).
        assert end == datetime(2026, 1, 1, 0, 0, tzinfo=UTC)

    # Test _prepare_chart_querysets with mocks
    @patch("apps.dashboard_reports.filters.DateFilter.to_start_date_end_date")
    @patch("apps.dashboard_reports.viewsets.dashboard_report.JobData.objects")
    def test__prepare_chart_querysets(self, mock_jobdata, mock_to_dates, viewset, factory):
        mock_start_date, mock_end_date = (
            datetime(2026, 3, 11, 0, 0, tzinfo=UTC),
            datetime(2026, 3, 12, 0, 0, tzinfo=UTC),
        )
        mock_to_dates.return_value = mock_start_date, mock_end_date

        mock_qs = MagicMock()
        mock_jobdata.all.return_value = mock_qs
        mock_qs.values.return_value.filter.return_value = mock_qs
        mock_qs.annotate.return_value.values.return_value.order_by.return_value = mock_qs
        # Use MagicMock for request with QueryDict for query_params
        mock_request = MagicMock()
        mock_request.query_params = QueryDict()
        viewset.request = mock_request

        viewset.kwargs = {
            "period": "last_7_days",
            "tz": "UTC",
            "start_date": mock_start_date,
            "end_date": mock_end_date,
        }
        job_qs, host_qs = viewset._prepare_chart_querysets("day")
        assert isinstance(job_qs, MagicMock | list)
        assert isinstance(host_qs, MagicMock | list)

    # Test _format_chart_result
    def test__format_chart_result(self, viewset):
        class Dummy:
            def __init__(self, term, runs, hosts):
                self.term = term
                self.runs = runs
                self.hosts = hosts

        data = [Dummy("2026-03-11", 5, 10), Dummy("2026-03-12", 3, 7)]
        result = viewset._format_chart_result(data)
        assert result["job_chart"]["items"][0]["label"] == "2026-03-11"
        assert result["job_chart"]["items"][0]["value"] == 5
        assert result["host_chart"]["items"][1]["value"] == 7

    # Test get_chart_data with mocks
    @patch("apps.dashboard_reports.filters.DateFilter.to_start_date_end_date")
    @patch("apps.dashboard_reports.viewsets.dashboard_report.generate_series")
    @patch.object(DashboardReportViewSet, "_prepare_chart_querysets")
    def test_get_chart_data(self, mock_prepare, mock_gen, mock_to_dates, viewset):
        mock_start_date, mock_end_date = (
            datetime(2026, 3, 11, 0, 0, tzinfo=UTC),
            datetime(2026, 3, 12, 0, 0, tzinfo=UTC),
        )
        mock_to_dates.return_value = mock_start_date, mock_end_date

        viewset.kwargs = {
            "period": "last_7_days",
            "tz": "UTC",
            "start_date": mock_start_date,
            "end_date": mock_end_date,
        }
        # Mock QuerySet-like objects for Subquery
        mock_job_qs = MagicMock()
        mock_host_qs = MagicMock()
        mock_job_qs.query = MagicMock()
        mock_host_qs.query = MagicMock()
        mock_prepare.return_value = (mock_job_qs, mock_host_qs)
        dummy_seq = [MagicMock(term="2026-03-11", runs=5, hosts=10)]
        mock_gen.return_value.annotate.return_value = dummy_seq
        result = viewset.get_chart_data()
        assert result["job_chart"]["items"][0]["label"] == "2026-03-11"
        assert result["job_chart"]["items"][0]["value"] == 5

    # Test details endpoint logic with mocks
    @patch("apps.dashboard_reports.viewsets.dashboard_report.SubscriptionCost.get")
    @patch("apps.dashboard_reports.viewsets.dashboard_report.JobData.objects")
    @patch("apps.dashboard_reports.viewsets.dashboard_report.JobHostSummary.objects")
    @patch.object(DashboardReportViewSet, "get_chart_data")
    @patch.object(DashboardReportViewSet, "get_serializer")
    def test_details(
        self, mock_serializer, mock_chart, mock_host_objects, mock_jobdata, mock_subcost, viewset, factory
    ):
        class TestViewSet(DashboardReportViewSet):
            def details(self, request, *args, **kwargs):
                return super().details.__wrapped__(self, request, *args, **kwargs)

        test_viewset = TestViewSet()
        mock_subcost.return_value.cost_employee_per_minute = 1
        mock_subcost.return_value.per_second_subscription_cost.return_value = decimal.Decimal("0.001")
        mock_subcost.return_value.include_template_creation_time_in_costs = True
        mock_jobdata.all.return_value = MagicMock()
        # unique hosts: JobHostSummary.objects.filter(...).values(...).distinct().count()
        mock_host_objects.filter.return_value.values.return_value.distinct.return_value.count.return_value = 2
        mock_chart.return_value = {
            "job_chart": {"kind": "day", "items": []},
            "host_chart": {"kind": "day", "items": []},
        }
        mock_serializer.return_value.data = {
            "total_runs": 10,
            "top_users": [],
            "top_projects": [],
            "total_number_of_unique_hosts": 2,
        }

        dt_start = datetime(2026, 3, 11, 0, 0, tzinfo=UTC)
        dt_end = datetime(2026, 3, 12, 0, 0, tzinfo=UTC)
        test_viewset.kwargs = {
            "period": "last_7_days",
            "tz": "UTC",
            "start_date": dt_start,
            "end_date": dt_end,
        }
        request = factory.get(
            "/", {"period": "last_7_days", "tz": "UTC", "start": dt_start.isoformat(), "end": dt_end.isoformat()}
        )

        # Use MagicMock to wrap request since query_params is a read-only property
        mock_request = MagicMock()
        mock_request.GET = request.GET
        mock_request.query_params = request.GET
        test_viewset.request = mock_request
        response = test_viewset.details(mock_request, start_date=dt_start, end_date=dt_end)
        assert isinstance(response, Response)
        assert response.status_code == 200
        assert response.data["total_number_of_unique_hosts"] == 2
