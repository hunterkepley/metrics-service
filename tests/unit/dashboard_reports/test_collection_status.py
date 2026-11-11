"""Unit tests for the DashboardCollectionStatusViewSet."""

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from django.urls import reverse
from rest_framework.test import APIRequestFactory

from apps.dashboard_reports.urls import router
from apps.dashboard_reports.viewsets.collection_status import DashboardCollectionStatusViewSet
from apps.tasks.models import TaskExecution

PATCH_FLAG = "apps.dashboard_reports.viewsets.collection_status.get_feature_enabled_from_db"
PATCH_TASK = "apps.dashboard_reports.viewsets.collection_status.Task"
PATCH_TASK_EXECUTION = "apps.dashboard_reports.viewsets.collection_status.TaskExecution"
PATCH_LATEST_SYNC = (
    "apps.dashboard_reports.viewsets.collection_status.DashboardCollectionStatusViewSet."
    "_get_latest_completed_hourly_sync"
)
PATCH_PERM = "ansible_base.rbac.api.permissions.IsSystemAdminOrAuditor.has_permission"
PATCH_DASHBOARD_SCOPE = "apps.dashboard_reports.viewsets.collection_status.dashboard_view_org_ids"
PATCH_MIN_TS = "apps.dashboard_reports.viewsets.collection_status.JobData.min_timestamp"
PATCH_SETTING = "apps.dashboard_reports.viewsets.collection_status.Setting"
INITIAL_COMPLETED_AT = datetime(2026, 9, 29, 10, 0, 0, tzinfo=UTC)
HOURLY_COMPLETED_AT = datetime(2026, 9, 29, 11, 3, 6, tzinfo=UTC)
factory = APIRequestFactory()
view = DashboardCollectionStatusViewSet.as_view({"get": "list"})
create_view = DashboardCollectionStatusViewSet.as_view({"post": "create"})


@pytest.mark.unit
class TestDashboardCollectionStatusViewSet:
    """Tests for DashboardCollectionStatusViewSet.list()."""

    @pytest.fixture(autouse=True)
    def bypass_permissions(self):
        with patch(PATCH_PERM, return_value=True), patch(PATCH_DASHBOARD_SCOPE, return_value=None):
            yield

    @pytest.fixture(autouse=True)
    def no_sync_by_default(self):
        """Avoid a real DB hit in tests that don't care about last_sync."""
        with patch(PATCH_LATEST_SYNC, return_value=None):
            yield

    def _get(self):
        request = factory.get("/api/v1/dashboard_reports/collection_status/")
        request.user = MagicMock()
        return view(request)

    @patch(PATCH_FLAG, return_value=False)
    @patch(PATCH_TASK)
    def test_flag_disabled_returns_nulls(self, mock_task_class, mock_flag):
        """When disabled, all fields except enabled are null and DB is not queried."""
        response = self._get()
        assert response.status_code == 200
        assert response.data == {
            "enabled": False,
            "next_run": None,
            "last_sync": None,
            "initial_collection_status": None,
            "min_collection_timestamp": None,
            "show_leaderboard": False,
            "show_dashboard": False,
            "scope": "global",
            "organizations": [],
        }
        mock_task_class.objects.filter.assert_not_called()

    @patch(PATCH_FLAG, return_value=False)
    @patch(PATCH_TASK)
    def test_min_timestamp_not_queried_when_disabled(self, mock_task_class, mock_flag):
        """JobData.min_timestamp() should NOT be called when feature flag is disabled."""
        with patch(PATCH_MIN_TS) as mock_min_ts:
            self._get()
            mock_min_ts.assert_not_called()

    @patch(PATCH_MIN_TS, return_value=datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC))
    @patch(PATCH_FLAG, return_value=True)
    @patch(PATCH_TASK)
    def test_flag_enabled_tasks_exist(self, mock_task_class, mock_flag, mock_min_ts):
        mock_data_task = MagicMock()
        mock_data_task.get_next_run_time.return_value = "2026-04-15T06:00:00+00:00"
        mock_initial_task = MagicMock()
        mock_initial_task.status = "completed"
        mock_initial_task.completed_at = None
        mock_task_class.objects.filter.return_value.first.side_effect = [
            mock_data_task,
            mock_initial_task,
        ]
        response = self._get()
        assert response.status_code == 200
        assert response.data["enabled"] is True
        assert response.data["next_run"] == "2026-04-15T06:00:00+00:00"
        assert response.data["initial_collection_status"] == "completed"
        assert response.data["min_collection_timestamp"] is not None

    @patch(PATCH_MIN_TS, return_value=None)
    @patch(PATCH_FLAG, return_value=True)
    @patch(PATCH_TASK)
    def test_flag_enabled_tasks_not_found(self, mock_task_class, mock_flag, mock_min_ts):
        mock_task_class.objects.filter.return_value.first.return_value = None
        response = self._get()
        assert response.status_code == 200
        assert response.data == {
            "enabled": True,
            "next_run": None,
            "last_sync": None,
            "initial_collection_status": None,
            "min_collection_timestamp": None,
            "show_leaderboard": True,
            "show_dashboard": True,
            "scope": "global",
            "organizations": [],
        }

    @patch(PATCH_MIN_TS, return_value=datetime(2024, 3, 1, 8, 30, 0, tzinfo=UTC))
    @patch(PATCH_FLAG, return_value=True)
    @patch(PATCH_TASK)
    def test_min_timestamp_queried_when_enabled(self, mock_task_class, mock_flag, mock_min_ts):
        """JobData.min_timestamp() IS called when feature flag is enabled."""
        mock_task_class.objects.filter.return_value.first.return_value = None
        self._get()
        mock_min_ts.assert_called_once()

    @patch(PATCH_MIN_TS, return_value=datetime(2024, 3, 1, 8, 30, 0, tzinfo=UTC))
    @patch(PATCH_FLAG, return_value=True)
    @patch(PATCH_TASK)
    def test_min_timestamp_returned_in_response(self, mock_task_class, mock_flag, mock_min_ts):
        """min_collection_timestamp in response matches JobData.min_timestamp() value."""
        mock_task_class.objects.filter.return_value.first.return_value = None
        expected = datetime(2024, 3, 1, 8, 30, 0, tzinfo=UTC)
        response = self._get()
        assert response.data["min_collection_timestamp"] == expected

    @patch(PATCH_MIN_TS, return_value=None)
    @patch(PATCH_FLAG, return_value=True)
    @patch(PATCH_TASK)
    def test_initial_status_running(self, mock_task_class, mock_flag, mock_min_ts):
        mock_data_task = MagicMock()
        mock_data_task.get_next_run_time.return_value = None
        mock_initial_task = MagicMock()
        mock_initial_task.status = "running"
        mock_task_class.objects.filter.return_value.first.side_effect = [
            mock_data_task,
            mock_initial_task,
        ]
        response = self._get()
        assert response.data["initial_collection_status"] == "running"

    @patch(PATCH_MIN_TS, return_value=None)
    @patch(PATCH_FLAG, return_value=True)
    @patch(PATCH_TASK)
    def test_initial_status_failed(self, mock_task_class, mock_flag, mock_min_ts):
        mock_data_task = MagicMock()
        mock_data_task.get_next_run_time.return_value = None
        mock_initial_task = MagicMock()
        mock_initial_task.status = "failed"
        mock_task_class.objects.filter.return_value.first.side_effect = [
            mock_data_task,
            mock_initial_task,
        ]
        response = self._get()
        assert response.data["initial_collection_status"] == "failed"

    @patch(PATCH_MIN_TS, return_value=None)
    @patch(PATCH_FLAG, return_value=True)
    @patch(PATCH_TASK)
    def test_queries_correct_tasks(self, mock_task_class, mock_flag, mock_min_ts):
        """next_run comes from hourly_unified_jobs (the hook driver), not the removed data task."""
        mock_task_class.objects.filter.return_value.first.return_value = None
        self._get()
        calls = mock_task_class.objects.filter.call_args_list
        assert len(calls) == 2
        assert calls[0].kwargs == {
            "name": "hourly_unified_jobs",
            "is_system_task": True,
        }
        assert calls[1].kwargs == {
            "function_name": "collect_dashboard_reports_initial_data",
            "is_system_task": True,
        }

    def test_permission_class(self):
        """The viewset no longer gates all actions via class-level permission_classes;
        IsSystemAdminOrAuditor is checked manually inside list()/create() instead."""
        from rest_framework.permissions import IsAuthenticated

        assert DashboardCollectionStatusViewSet.permission_classes == [IsAuthenticated]


@pytest.mark.unit
class TestCollectionStatusOrgScope:
    """Access-discovery response for an organization-scoped dashboard user."""

    @patch(PATCH_MIN_TS, return_value=None)
    @patch(PATCH_FLAG, return_value=True)
    @patch(PATCH_TASK)
    @patch(
        "apps.dashboard_reports.viewsets.collection_status.fetch_organizations",
        return_value=([{"id": 10, "name": "Org A"}], 1),
    )
    @patch("apps.dashboard_reports.viewsets.collection_status.get_db_connection")
    @patch("apps.dashboard_reports.viewsets.collection_status.permitted_awx_edit_org_ids", return_value={10})
    @patch("apps.dashboard_reports.viewsets.collection_status.permitted_awx_view_org_ids", return_value={10})
    @patch(PATCH_DASHBOARD_SCOPE, return_value={1})
    @patch(PATCH_LATEST_SYNC, return_value=None)
    def test_org_scope_response(  # noqa: PLR0917
        self,
        mock_latest_sync,
        mock_scope,
        mock_view_org_ids,
        mock_edit_org_ids,
        mock_conn,
        mock_fetch_orgs,
        mock_task,
        mock_flag,
        mock_min_ts,
    ):
        mock_task.objects.filter.return_value.first.return_value = None
        request = factory.get("/api/v1/dashboard_reports/collection_status/")
        request.user = MagicMock()
        response = view(request)
        assert response.status_code == 200
        assert response.data["scope"] == "organization"
        assert response.data["organizations"] == [{"id": 10, "name": "Org A", "can_edit": True}]
        assert response.data["show_dashboard"] is True

    @patch(PATCH_MIN_TS, return_value=None)
    @patch(PATCH_FLAG, return_value=True)
    @patch(PATCH_TASK)
    @patch(PATCH_DASHBOARD_SCOPE, return_value=set())
    @patch(PATCH_LATEST_SYNC, return_value=None)
    def test_no_access_response(
        self,
        mock_latest_sync,
        mock_scope,
        mock_task,
        mock_flag,
        mock_min_ts,
    ):
        mock_task.objects.filter.return_value.first.return_value = None
        request = factory.get("/api/v1/dashboard_reports/collection_status/")
        request.user = MagicMock()
        response = view(request)
        assert response.status_code == 200
        assert response.data["scope"] == "none"
        assert response.data["organizations"] == []
        assert response.data["show_dashboard"] is False


@pytest.mark.unit
class TestLastSync:
    """Tests for last_sync in DashboardCollectionStatusViewSet.list()."""

    @pytest.fixture(autouse=True)
    def bypass_permissions(self):
        with patch(PATCH_PERM, return_value=True), patch(PATCH_DASHBOARD_SCOPE, return_value=None):
            yield

    def _get(self):
        request = factory.get("/api/v1/dashboard_reports/collection_status/")
        request.user = MagicMock()
        return view(request)

    @patch(PATCH_MIN_TS, return_value=None)
    @patch(PATCH_FLAG, return_value=False)
    @patch(PATCH_TASK)
    def test_not_queried_when_disabled(self, mock_task_class, mock_flag, mock_min_ts):
        """_get_latest_completed_hourly_sync() is NOT called when DASHBOARD_COLLECTION is disabled."""
        with patch(PATCH_LATEST_SYNC) as mock_latest_sync:
            response = self._get()
            mock_latest_sync.assert_not_called()
        assert response.data["last_sync"] is None

    @patch(PATCH_MIN_TS, return_value=None)
    @patch(PATCH_FLAG, return_value=True)
    @patch(PATCH_TASK)
    def test_no_sync_yet_when_none_found(self, mock_task_class, mock_flag, mock_min_ts):
        """last_sync is 'None' when no completed sync execution exists."""
        mock_task_class.objects.filter.return_value.first.return_value = None
        with patch(PATCH_LATEST_SYNC, return_value=None):
            response = self._get()
        assert response.data["last_sync"] is None

    @patch(PATCH_MIN_TS, return_value=None)
    @patch(PATCH_FLAG, return_value=True)
    @patch(PATCH_TASK)
    def test_returns_completed_at_of_latest_sync(self, mock_task_class, mock_flag, mock_min_ts):
        """last_sync reflects the completed_at of the latest completed sync execution."""
        mock_task_class.objects.filter.return_value.first.return_value = None
        completed_at = datetime(2026, 9, 29, 11, 3, 6, tzinfo=UTC)
        mock_execution = MagicMock(completed_at=completed_at)
        with patch(PATCH_LATEST_SYNC, return_value=mock_execution):
            response = self._get()
        assert response.data["last_sync"] == completed_at

    @pytest.mark.parametrize(
        ("hourly_completed_at", "initial_status", "initial_completed_at", "expected"),
        [
            # no hourly sync (e.g. METRICS_COLLECTION disabled) -> initial collection's completed_at
            (None, "completed", INITIAL_COMPLETED_AT, INITIAL_COMPLETED_AT),
            # hourly sync finished after the initial collection -> hourly wins
            (HOURLY_COMPLETED_AT, "completed", INITIAL_COMPLETED_AT, HOURLY_COMPLETED_AT),
            # hourly sync older than the initial collection -> initial wins
            (INITIAL_COMPLETED_AT, "completed", HOURLY_COMPLETED_AT, HOURLY_COMPLETED_AT),
            # initial collection not (yet) completed -> ignored
            (None, "running", INITIAL_COMPLETED_AT, None),
            # completed without completed_at (row from before it was preserved) -> ignored
            (None, "completed", None, None),
        ],
    )
    @patch(PATCH_MIN_TS, return_value=None)
    @patch(PATCH_FLAG, return_value=True)
    @patch(PATCH_TASK)
    def test_initial_collection_completed_at(  # noqa: PLR0913
        self,
        mock_task_class,
        mock_flag,
        mock_min_ts,
        hourly_completed_at,
        initial_status,
        initial_completed_at,
        expected,
    ):
        """last_sync is the later of the latest hourly sync and the completed initial collection's completed_at."""
        mock_task_class.objects.filter.return_value.first.return_value = MagicMock(
            status=initial_status, completed_at=initial_completed_at
        )
        latest_sync = MagicMock(completed_at=hourly_completed_at) if hourly_completed_at else None
        with patch(PATCH_LATEST_SYNC, return_value=latest_sync):
            response = self._get()
        assert response.data["last_sync"] == expected

    @patch(PATCH_TASK_EXECUTION)
    def test_query_filters_on_completed_status_and_sync_function(self, mock_task_execution_class):
        """The query filters on status=completed and function_name=sync_dashboard_job_records,
        ordered by the most recent completed_at."""
        mock_task_execution_class.DoesNotExist = TaskExecution.DoesNotExist
        mock_qs = mock_task_execution_class.objects.filter.return_value
        DashboardCollectionStatusViewSet._get_latest_completed_hourly_sync()
        mock_task_execution_class.objects.filter.assert_called_once_with(
            status="completed",
            task__function_name="sync_dashboard_job_records",
        )
        mock_qs.latest.assert_called_once_with("completed_at")

    @patch(PATCH_TASK_EXECUTION)
    def test_returns_none_when_no_completed_execution_exists(self, mock_task_execution_class):
        """DoesNotExist is caught and None is returned (no completed sync execution yet)."""
        mock_task_execution_class.DoesNotExist = TaskExecution.DoesNotExist
        mock_task_execution_class.objects.filter.return_value.latest.side_effect = TaskExecution.DoesNotExist
        result = DashboardCollectionStatusViewSet._get_latest_completed_hourly_sync()
        assert result is None


@pytest.mark.unit
class TestDashboardCollectionStatusURL:
    """Tests for URL registration and reversal."""

    def test_url_reverse(self):
        url = reverse("v1:collection_status-list")
        assert "collection_status" in url

    def test_viewset_registered_in_router(self):
        registered = [reg[1] for reg in router.registry]
        assert DashboardCollectionStatusViewSet in registered


@pytest.mark.unit
class TestDashboardCollectionStatusCreate:
    """Tests for DashboardCollectionStatusViewSet.create() (POST show_leaderboard toggle)."""

    def _post(self, data):
        request = factory.post("/api/v1/dashboard_reports/collection_status/", data, format="json")
        request.user = MagicMock()
        return create_view(request)

    @patch(PATCH_PERM, return_value=False)
    def test_non_admin_forbidden(self, mock_perm):
        """Non admin/auditor users get 403 and no Setting write is attempted."""
        with patch(PATCH_SETTING) as mock_setting:
            response = self._post({"show_leaderboard": True})
            mock_setting.objects.update_or_create.assert_not_called()
        assert response.status_code == 403

    @patch(PATCH_PERM, return_value=True)
    def test_non_boolean_value_rejected(self, mock_perm):
        """Non-boolean show_leaderboard value returns 400 and does not touch the DB."""
        with patch(PATCH_SETTING) as mock_setting:
            response = self._post({"show_leaderboard": "true"})
            mock_setting.objects.update_or_create.assert_not_called()
        assert response.status_code == 400
        assert "show_leaderboard" in response.data

    @patch(PATCH_PERM, return_value=True)
    def test_missing_value_rejected(self, mock_perm):
        """Missing show_leaderboard key returns 400."""
        response = self._post({})
        assert response.status_code == 400

    @patch(PATCH_PERM, return_value=True)
    def test_sets_flag_true(self, mock_perm):
        """POST with True persists SHOW_LEADERBOARD=true via update_or_create."""
        with patch(PATCH_SETTING) as mock_setting:
            response = self._post({"show_leaderboard": True})
            mock_setting.objects.update_or_create.assert_called_once()
            _, kwargs = mock_setting.objects.update_or_create.call_args
            assert kwargs["setting_key"] == "SHOW_LEADERBOARD"
            assert kwargs["defaults"]["current_value"] == json.dumps(True)
        assert response.status_code == 200
        assert response.data == {"show_leaderboard": True}

    @patch(PATCH_PERM, return_value=True)
    def test_sets_flag_false(self, mock_perm):
        """POST with False persists SHOW_LEADERBOARD=false via update_or_create."""
        with patch(PATCH_SETTING) as mock_setting:
            response = self._post({"show_leaderboard": False})
            _, kwargs = mock_setting.objects.update_or_create.call_args
            assert kwargs["defaults"]["current_value"] == json.dumps(False)
        assert response.status_code == 200
        assert response.data == {"show_leaderboard": False}

    @patch(PATCH_PERM, return_value=True)
    def test_last_modified_by_set_to_request_user(self, mock_perm):
        """The requesting user is recorded as last_modified_by."""
        request = factory.post(
            "/api/v1/dashboard_reports/collection_status/", {"show_leaderboard": True}, format="json"
        )
        sentinel_user = MagicMock()
        request.user = sentinel_user
        with patch(PATCH_SETTING) as mock_setting:
            create_view(request)
            _, kwargs = mock_setting.objects.update_or_create.call_args
            assert kwargs["defaults"]["last_modified_by"] is sentinel_user
