"""
Unit tests for TemplateMetadata viewset, serializer, and URL routing.
DB interaction is mostly mocked; TestTemplateMetadataBulkUpdate uses the test DB.
"""

from unittest.mock import MagicMock, patch

import pytest
from django.test import TestCase
from django.urls import resolve
from rest_framework import status
from rest_framework.test import APIRequestFactory

from apps.dashboard_reports.models import (
    DEFAULT_TIME_TAKEN_TO_CREATE_AUTOMATION_MINUTES,
    DEFAULT_TIME_TAKEN_TO_MANUALLY_EXECUTE_MINUTES,
    JobData,
    TemplateMetadata,
)
from apps.dashboard_reports.serializers import TemplateMetadataDefaultsSerializer, TemplateMetadataSerializer
from apps.dashboard_reports.viewsets.template_metadata import TemplateMetadataViewSet

# =============================================================================
# Helpers
# =============================================================================


def _make_mock_instance(
    pk: int = 1,
    template_id: int = 42,
    time_taken_manually_execute_minutes: int | None = 30,
    time_taken_create_automation_minutes: int | None = 60,
) -> MagicMock:
    instance = MagicMock(spec=TemplateMetadata)
    instance.pk = pk
    instance.template_id = template_id
    instance.time_taken_manually_execute_minutes = time_taken_manually_execute_minutes
    instance.time_taken_create_automation_minutes = time_taken_create_automation_minutes
    return instance


def _make_request_user(is_authenticated: bool = True) -> MagicMock:
    user = MagicMock()
    user.is_authenticated = is_authenticated
    return user


# =============================================================================
# Serializer tests
# =============================================================================


@pytest.mark.unit
class TestTemplateMetadataSerializer(TestCase):
    def _serializer_data(self, **kwargs) -> dict:
        return TemplateMetadataSerializer(_make_mock_instance(**kwargs)).data

    def test_contains_expected_fields(self):
        assert set(self._serializer_data().keys()) == {
            "template_id",
            "time_taken_manually_execute_minutes",
            "time_taken_create_automation_minutes",
        }

    def test_maps_values_correctly(self):
        data = self._serializer_data(
            template_id=99,
            time_taken_manually_execute_minutes=15,
            time_taken_create_automation_minutes=45,
        )
        assert data["template_id"] == 99
        assert data["time_taken_manually_execute_minutes"] == 15
        assert data["time_taken_create_automation_minutes"] == 45

    def test_allows_null_time_fields(self):
        data = self._serializer_data(
            time_taken_manually_execute_minutes=None,
            time_taken_create_automation_minutes=None,
        )
        assert data["time_taken_manually_execute_minutes"] is None
        assert data["time_taken_create_automation_minutes"] is None

    def test_template_id_is_read_only(self):
        assert TemplateMetadataSerializer().fields["template_id"].read_only is True

    def test_put_payload_cannot_change_template_id(self):
        payload = {
            "template_id": 999,
            "time_taken_manually_execute_minutes": 20,
            "time_taken_create_automation_minutes": 40,
        }
        serializer = TemplateMetadataSerializer(data=payload)
        assert serializer.is_valid(), serializer.errors
        assert "template_id" not in serializer.validated_data

    def test_valid_with_complete_payload(self):
        serializer = TemplateMetadataSerializer(
            data={
                "time_taken_manually_execute_minutes": 30,
                "time_taken_create_automation_minutes": 60,
            }
        )
        assert serializer.is_valid(), serializer.errors

    def test_valid_with_null_time_fields(self):
        serializer = TemplateMetadataSerializer(
            data={
                "time_taken_manually_execute_minutes": None,
                "time_taken_create_automation_minutes": None,
            }
        )
        assert serializer.is_valid(), serializer.errors

    def test_meta_model(self):
        assert TemplateMetadataSerializer.Meta.model is TemplateMetadata

    def test_meta_fields(self):
        assert set(TemplateMetadataSerializer.Meta.fields) == {
            "template_id",
            "time_taken_manually_execute_minutes",
            "time_taken_create_automation_minutes",
        }


# =============================================================================
# ViewSet configuration tests
# =============================================================================


@pytest.mark.unit
class TestTemplateMetadataViewSetConfig(TestCase):
    def test_uses_correct_serializer_class(self):
        assert TemplateMetadataViewSet.serializer_class is TemplateMetadataSerializer

    def test_versioning_class_is_none(self):
        assert TemplateMetadataViewSet.versioning_class is None

    def test_pagination_class_is_none(self):
        assert TemplateMetadataViewSet.pagination_class is None

    def test_has_is_system_admin_or_auditor_permission(self):
        from ansible_base.rbac.api.permissions import IsSystemAdminOrAuditor

        from apps.dashboard_reports.permissions import IsDashboardViewer

        viewset = TemplateMetadataViewSet()
        viewset.request = MagicMock(method="GET")
        assert isinstance(viewset.get_permissions()[0], IsDashboardViewer)
        viewset.request = MagicMock(method="PUT")
        assert isinstance(viewset.get_permissions()[0], IsSystemAdminOrAuditor)

    def test_has_retrieve_mixin(self):
        from rest_framework.mixins import RetrieveModelMixin

        assert issubclass(TemplateMetadataViewSet, RetrieveModelMixin)

    def test_has_update_mixin(self):
        from rest_framework.mixins import UpdateModelMixin

        assert issubclass(TemplateMetadataViewSet, UpdateModelMixin)

    def test_does_not_have_list_mixin(self):
        from rest_framework.mixins import ListModelMixin

        assert not issubclass(TemplateMetadataViewSet, ListModelMixin)

    def test_does_not_have_create_mixin(self):
        from rest_framework.mixins import CreateModelMixin

        assert not issubclass(TemplateMetadataViewSet, CreateModelMixin)

    @patch(
        "apps.dashboard_reports.viewsets.template_metadata.permitted_awx_view_org_ids",
        return_value=None,
    )
    @patch("apps.dashboard_reports.viewsets.template_metadata.TemplateMetadata")
    def test_get_queryset_calls_objects_all(self, mock_model, mock_org_ids):
        mock_qs = MagicMock()
        mock_model.objects.all.return_value = mock_qs
        viewset = TemplateMetadataViewSet()
        viewset.request = MagicMock()
        viewset.kwargs = {}
        viewset.format_kwarg = None
        assert viewset.get_queryset() is mock_qs
        mock_model.objects.all.assert_called_once()


# =============================================================================
# ViewSet action tests  (get_object + perform_* mocked — no DB)
# =============================================================================


@pytest.mark.unit
class TestTemplateMetadataViewSetActions(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.instance = _make_mock_instance()
        self.none_qs = TemplateMetadata.objects.none()

    def _view(self, method_map: dict):
        return TemplateMetadataViewSet.as_view(method_map)

    def _call(self, view, request, pk: int = 1):
        with patch.object(TemplateMetadataViewSet, "get_queryset", return_value=self.none_qs):
            return view(request, pk=pk)

    # ---- GET (retrieve) ----

    @patch("apps.dashboard_reports.permissions.has_super_permission", return_value=True)
    @patch.object(TemplateMetadataViewSet, "get_object")
    def test_retrieve_returns_200(self, mock_get_object, mock_super_perm):
        mock_get_object.return_value = self.instance
        request = self.factory.get("/fake/")
        request.user = _make_request_user()
        response = self._call(self._view({"get": "retrieve"}), request)
        assert response.status_code == status.HTTP_200_OK

    @patch("apps.dashboard_reports.permissions.has_super_permission", return_value=True)
    @patch.object(TemplateMetadataViewSet, "get_object")
    def test_retrieve_calls_get_object_once(self, mock_get_object, mock_super_perm):
        mock_get_object.return_value = self.instance
        request = self.factory.get("/fake/")
        request.user = _make_request_user()
        self._call(self._view({"get": "retrieve"}), request)
        mock_get_object.assert_called_once()

    # ---- PUT (update) ----

    @patch("ansible_base.rbac.api.permissions.has_super_permission", return_value=True)
    @patch("apps.dashboard_reports.permissions.has_super_permission", return_value=True)
    @patch.object(TemplateMetadataViewSet, "get_object")
    @patch.object(TemplateMetadataViewSet, "perform_update")
    def test_update_returns_200(self, mock_perform_update, mock_get_object, mock_super_perm, mock_dab_super_perm):
        mock_get_object.return_value = self.instance
        self.instance.template_id = 42
        request = self.factory.put(
            "/fake/",
            data={
                "time_taken_manually_execute_minutes": 20,
                "time_taken_create_automation_minutes": 50,
            },
            format="json",
        )
        request.user = _make_request_user()
        response = self._call(self._view({"put": "update"}), request)
        assert response.status_code == status.HTTP_200_OK

    @patch("ansible_base.rbac.api.permissions.has_super_permission", return_value=True)
    @patch("apps.dashboard_reports.permissions.has_super_permission", return_value=True)
    @patch.object(TemplateMetadataViewSet, "get_object")
    @patch.object(TemplateMetadataViewSet, "perform_update")
    def test_update_calls_perform_update_once(
        self, mock_perform_update, mock_get_object, mock_super_perm, mock_dab_super_perm
    ):
        mock_get_object.return_value = self.instance
        self.instance.template_id = 42
        request = self.factory.put(
            "/fake/",
            data={
                "time_taken_manually_execute_minutes": 20,
                "time_taken_create_automation_minutes": 50,
            },
            format="json",
        )
        request.user = _make_request_user()
        self._call(self._view({"put": "update"}), request)
        mock_perform_update.assert_called_once()

    @patch("ansible_base.rbac.api.permissions.has_super_permission", return_value=True)
    @patch("apps.dashboard_reports.permissions.has_super_permission", return_value=True)
    @patch.object(TemplateMetadataViewSet, "get_object")
    @patch.object(TemplateMetadataViewSet, "perform_update")
    def test_update_with_null_time_fields(
        self, mock_perform_update, mock_get_object, mock_super_perm, mock_dab_super_perm
    ):
        mock_get_object.return_value = self.instance
        self.instance.template_id = 42
        request = self.factory.put(
            "/fake/",
            data={
                "time_taken_manually_execute_minutes": None,
                "time_taken_create_automation_minutes": None,
            },
            format="json",
        )
        request.user = _make_request_user()
        response = self._call(self._view({"put": "update"}), request)
        assert response.status_code == status.HTTP_200_OK

    def test_patch_mapped_to_partial_update(self):
        match = resolve("/api/v1/dashboard_reports/template_metadata/1/")
        assert match.func.actions.get("patch") == "partial_update"

    # ---- permission gate ----

    def test_all_methods_return_403_for_unauthenticated_user(self):
        """Unauthenticated requests must be denied with 403 (IsSystemAdminOrAuditor)."""
        unauthenticated_user = _make_request_user(is_authenticated=False)
        for method, factory_fn, map_ in [
            ("GET", self.factory.get, {"get": "retrieve"}),
            ("PUT", lambda u, **k: self.factory.put(u, data={}, format="json", **k), {"put": "update"}),
        ]:
            with self.subTest(method=method):
                request = factory_fn("/fake/")
                request.user = unauthenticated_user
                response = self._call(self._view(map_), request)
                assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_non_admin_authenticated_user_receives_403_on_update(self):
        """Authenticated non-admin users must receive 403 when attempting to update template metadata."""
        request = self.factory.put(
            "/fake/",
            data={
                "time_taken_manually_execute_minutes": 20,
                "time_taken_create_automation_minutes": 50,
            },
            format="json",
        )
        request.user = _make_request_user(is_authenticated=True)
        with patch("ansible_base.rbac.api.permissions.has_super_permission", return_value=False):
            response = self._call(self._view({"put": "update"}), request)
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_non_admin_authenticated_user_receives_403_on_partial_update(self):
        """Authenticated non-admin users must receive 403 when attempting to partially update template metadata."""
        request = self.factory.patch(
            "/fake/",
            data={"time_taken_manually_execute_minutes": None},
            format="json",
        )
        request.user = _make_request_user(is_authenticated=True)
        with patch("ansible_base.rbac.api.permissions.has_super_permission", return_value=False):
            response = self._call(self._view({"patch": "partial_update"}), request)
        assert response.status_code == status.HTTP_403_FORBIDDEN


# =============================================================================
# Organization scope
# =============================================================================


@pytest.mark.django_db
@pytest.mark.unit
class TestTemplateMetadataOrgScope:
    """Template metadata reads must follow the caller's dashboard organization scope."""

    @staticmethod
    def _create_data():
        in_scope = TemplateMetadata.objects.create(template_name="In Scope", template_id=1001)
        out_of_scope = TemplateMetadata.objects.create(template_name="Out of Scope", template_id=1002)
        JobData.objects.create(
            job_id=1001,
            template_name="In Scope",
            template_metadata=in_scope,
            organization_id=10,
            elapsed=1,
        )
        JobData.objects.create(
            job_id=1002,
            template_name="Out of Scope",
            template_metadata=out_of_scope,
            organization_id=20,
            elapsed=1,
        )
        return in_scope, out_of_scope

    @staticmethod
    def _viewset():
        viewset = TemplateMetadataViewSet()
        viewset.request = MagicMock()
        viewset.kwargs = {}
        viewset.format_kwarg = None
        return viewset

    @patch(
        "apps.dashboard_reports.viewsets.template_metadata.permitted_awx_view_org_ids",
        return_value={10},
    )
    def test_org_viewer_only_sees_templates_in_scope(self, mock_org_ids):
        in_scope, _ = self._create_data()
        assert list(self._viewset().get_queryset()) == [in_scope]

    @patch(
        "apps.dashboard_reports.viewsets.template_metadata.permitted_awx_view_org_ids",
        return_value=None,
    )
    def test_global_viewer_sees_all_templates(self, mock_org_ids):
        in_scope, out_of_scope = self._create_data()
        assert set(self._viewset().get_queryset()) == {in_scope, out_of_scope}


# =============================================================================
# URL routing tests
# =============================================================================


@pytest.mark.unit
class TestTemplateMetadataUrls(TestCase):
    def test_url_resolves_to_metadata_viewset(self):
        match = resolve("/api/v1/dashboard_reports/template_metadata/1/")
        assert match.func.cls is TemplateMetadataViewSet

    def test_get_mapped_to_retrieve(self):
        match = resolve("/api/v1/dashboard_reports/template_metadata/1/")
        assert match.func.actions.get("get") == "retrieve"

    def test_put_mapped_to_update(self):
        match = resolve("/api/v1/dashboard_reports/template_metadata/1/")
        assert match.func.actions.get("put") == "update"

    def test_delete_not_mapped(self):
        match = resolve("/api/v1/dashboard_reports/template_metadata/1/")
        assert "delete" not in match.func.actions

    def test_post_not_mapped(self):
        match = resolve("/api/v1/dashboard_reports/template_metadata/1/")
        assert "post" not in match.func.actions


@pytest.mark.unit
@pytest.mark.django_db
class TestTemplateMetadataBulkUpdate:
    url = "/api/v1/dashboard_reports/template_metadata/"

    def test_updates_all_partially(self, admin_client):
        for i in (1, 2):
            TemplateMetadata.objects.create(
                template_id=i,
                template_name=f"t{i}",
                time_taken_manually_execute_minutes=1,
                time_taken_create_automation_minutes=2,
            )
        response = admin_client.patch(self.url, {"time_taken_manually_execute_minutes": 30}, format="json")
        assert response.status_code == 200
        assert response.json() == {"updated": 2}
        for row in TemplateMetadata.objects.all():
            assert row.time_taken_manually_execute_minutes == 30
            assert row.time_taken_create_automation_minutes == 2

    def test_updates_modified_and_modified_by(self, admin_client, admin_user):
        row = TemplateMetadata.objects.create(template_id=1, template_name="t1")
        old_modified = row.modified
        response = admin_client.patch(self.url, {"time_taken_manually_execute_minutes": 30}, format="json")
        assert response.status_code == 200
        row.refresh_from_db()
        assert row.modified > old_modified
        assert row.modified_by == admin_user

    def test_empty_body_rejected(self, admin_client):
        assert admin_client.patch(self.url, {}, format="json").status_code == 400

    def test_negative_rejected(self, admin_client):
        response = admin_client.patch(self.url, {"time_taken_create_automation_minutes": -1}, format="json")
        assert response.status_code == 400

    def test_null_rejected(self, admin_client):
        row = TemplateMetadata.objects.create(template_id=1, template_name="t1", time_taken_manually_execute_minutes=5)
        response = admin_client.patch(self.url, {"time_taken_manually_execute_minutes": None}, format="json")
        assert response.status_code == 400
        row.refresh_from_db()
        assert row.time_taken_manually_execute_minutes == 5

    def test_put_not_allowed(self, admin_client):
        assert admin_client.put(self.url, {}, format="json").status_code == 405

    def test_regular_user_forbidden(self, api_client, rando):
        api_client.force_authenticate(rando)
        response = api_client.patch(self.url, {"time_taken_manually_execute_minutes": 30}, format="json")
        assert response.status_code == 403

    def test_platform_auditor_forbidden(self, api_client, platform_auditor):
        api_client.force_authenticate(platform_auditor)
        response = api_client.patch(self.url, {"time_taken_manually_execute_minutes": 30}, format="json")
        assert response.status_code == 403


@pytest.mark.unit
@pytest.mark.django_db
class TestTemplateMetadataDefaults:
    url = "/api/v1/dashboard_reports/template_metadata/defaults/"

    def test_url_resolves_to_defaults_action(self):
        match = resolve(self.url)
        assert match.func.cls is TemplateMetadataViewSet
        assert match.func.actions == {"get": "defaults"}

    def test_returns_default_values_from_model_constants(self, admin_client):
        assert DEFAULT_TIME_TAKEN_TO_MANUALLY_EXECUTE_MINUTES == 30
        assert DEFAULT_TIME_TAKEN_TO_CREATE_AUTOMATION_MINUTES == 60
        response = admin_client.get(self.url)
        assert response.status_code == 200
        assert response.json() == {
            "time_taken_manually_execute_minutes": DEFAULT_TIME_TAKEN_TO_MANUALLY_EXECUTE_MINUTES,
            "time_taken_create_automation_minutes": DEFAULT_TIME_TAKEN_TO_CREATE_AUTOMATION_MINUTES,
        }

    def test_action_uses_defaults_serializer(self):
        assert resolve(self.url).func.initkwargs["serializer_class"] is TemplateMetadataDefaultsSerializer

    def test_platform_auditor_allowed(self, api_client, platform_auditor):
        api_client.force_authenticate(platform_auditor)
        assert api_client.get(self.url).status_code == 200

    def test_regular_user_forbidden(self, api_client, rando):
        api_client.force_authenticate(rando)
        assert api_client.get(self.url).status_code == 403

    def test_unauthenticated_forbidden(self, api_client):
        assert api_client.get(self.url).status_code == 403

    def test_post_not_allowed(self, admin_client):
        assert admin_client.post(self.url, {}, format="json").status_code == 405
