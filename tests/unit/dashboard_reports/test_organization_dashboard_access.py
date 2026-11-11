"""Unit tests for organization-scoped dashboard permission helpers."""

import json
from unittest.mock import MagicMock, patch

import pytest

from apps.dashboard_reports.permissions import (
    IsDashboardEditor,
    dashboard_edit_org_ids,
    dashboard_view_org_ids,
    filter_set_organization_ids,
    user_can_save_report,
)


@pytest.mark.unit
class TestFilterSetOrganizationIds:
    def test_no_filter_returns_none(self):
        assert filter_set_organization_ids({}) is None
        assert filter_set_organization_ids(None) is None
        assert filter_set_organization_ids("") is None

    def test_organization_key(self):
        assert filter_set_organization_ids({"organization": ["1", 2]}) == [1, 2]

    def test_organization_id_key(self):
        assert filter_set_organization_ids({"organization_id": [3]}) == [3]

    def test_organizations_key(self):
        assert filter_set_organization_ids({"organizations": [4, "5"]}) == [4, 5]

    def test_json_string(self):
        filters = json.dumps({"organization": [7]})
        assert filter_set_organization_ids(filters) == [7]

    def test_empty_selection_returns_empty_list(self):
        assert filter_set_organization_ids({"organization": []}) == []

    def test_invalid_values_are_skipped(self):
        assert filter_set_organization_ids({"organization": ["x", 1, None]}) == [1]


@pytest.mark.unit
class TestUserCanSaveReport:
    @patch("apps.dashboard_reports.permissions.permitted_awx_view_org_ids")
    @patch("apps.dashboard_reports.permissions.permitted_awx_edit_org_ids")
    def test_admin_can_save_anything(self, mock_edit, mock_view):
        mock_edit.return_value = None
        assert user_can_save_report(MagicMock(), {"organization": [999]}) is True

    @patch("apps.dashboard_reports.permissions.permitted_awx_view_org_ids")
    @patch("apps.dashboard_reports.permissions.permitted_awx_edit_org_ids")
    def test_editor_can_save_selected_editable_org(self, mock_edit, mock_view):
        mock_edit.return_value = {1, 2}
        assert user_can_save_report(MagicMock(), {"organization": [1]}) is True

    @patch("apps.dashboard_reports.permissions.permitted_awx_view_org_ids")
    @patch("apps.dashboard_reports.permissions.permitted_awx_edit_org_ids")
    def test_editor_cannot_save_out_of_scope_org(self, mock_edit, mock_view):
        mock_edit.return_value = {1}
        assert user_can_save_report(MagicMock(), {"organization": [2]}) is False

    @patch("apps.dashboard_reports.permissions.permitted_awx_view_org_ids")
    @patch("apps.dashboard_reports.permissions.permitted_awx_edit_org_ids")
    def test_editor_cannot_save_empty_selection(self, mock_edit, mock_view):
        mock_edit.return_value = {1}
        assert user_can_save_report(MagicMock(), {"organization": []}) is False

    @patch("apps.dashboard_reports.permissions.permitted_awx_view_org_ids")
    @patch("apps.dashboard_reports.permissions.permitted_awx_edit_org_ids")
    def test_no_org_filter_requires_all_visible_orgs_editable(self, mock_edit, mock_view):
        mock_edit.return_value = {1, 2}
        mock_view.return_value = {1, 2}
        assert user_can_save_report(MagicMock(), {"period": ["last_7_days"]}) is True

        mock_view.return_value = {1, 2, 3}
        assert user_can_save_report(MagicMock(), {"period": ["last_7_days"]}) is False


@pytest.mark.unit
class TestIsDashboardEditor:
    @patch("apps.dashboard_reports.permissions.dashboard_edit_org_ids", return_value=set())
    @patch("apps.dashboard_reports.permissions.has_super_permission")
    def test_platform_auditor_cannot_write(self, mock_super, mock_edit):
        # Auditor has the global "view" bypass but not the admin superuser flag.
        mock_super.side_effect = lambda user, codename=None: codename == "view"
        request = MagicMock(method="POST")
        assert IsDashboardEditor().has_permission(request, None) is False

    @patch("apps.dashboard_reports.permissions.has_super_permission")
    def test_system_admin_can_write(self, mock_super):
        mock_super.return_value = True
        request = MagicMock(method="PUT")
        assert IsDashboardEditor().has_permission(request, None) is True

    @patch("apps.dashboard_reports.permissions.dashboard_view_org_ids", return_value={1})
    @patch("apps.dashboard_reports.permissions.has_super_permission", return_value=False)
    def test_viewer_can_read(self, mock_super, mock_view_orgs):
        request = MagicMock(method="GET")
        assert IsDashboardEditor().has_permission(request, None) is True

    @patch("apps.dashboard_reports.permissions.dashboard_view_org_ids", return_value=set())
    @patch("apps.dashboard_reports.permissions.has_super_permission", return_value=False)
    def test_no_access_cannot_read(self, mock_super, mock_view_orgs):
        request = MagicMock(method="GET")
        assert IsDashboardEditor().has_permission(request, None) is False


@pytest.mark.django_db
@pytest.mark.unit
class TestDashboardRoleMapping:
    """Verify the managed roles map to the expected dashboard permissions."""

    @pytest.fixture
    def org(self):
        from apps.core.models import Organization

        return Organization.objects.create(name="Dashboard Test Org")

    @pytest.fixture
    def user(self):
        from apps.core.models import User

        return User.objects.create_user(username="dashboard-user", email="dash@example.com", password="test")

    def _assign(self, user, org, role_name):
        from ansible_base.rbac.models import RoleDefinition

        RoleDefinition.objects.get(name=role_name).give_permission(user, org)

    def test_organization_admin_gets_view_and_edit(self, user, org):
        self._assign(user, org, "Organization Admin")
        assert dashboard_view_org_ids(user) == {org.pk}
        assert dashboard_edit_org_ids(user) == {org.pk}

    def test_dashboard_viewer_gets_view_only(self, user, org):
        self._assign(user, org, "Automation Dashboard Viewer")
        assert dashboard_view_org_ids(user) == {org.pk}
        assert dashboard_edit_org_ids(user) == set()

    def test_dashboard_editor_gets_view_and_edit(self, user, org):
        self._assign(user, org, "Automation Dashboard Editor")
        assert dashboard_view_org_ids(user) == {org.pk}
        assert dashboard_edit_org_ids(user) == {org.pk}

    def test_organization_member_gets_no_dashboard_access(self, user, org):
        self._assign(user, org, "Organization Member")
        assert dashboard_view_org_ids(user) == set()
        assert dashboard_edit_org_ids(user) == set()
