"""
Unit tests for FilterSetsViewSet, FilterSetSerializer, and URL routing.

Covers:
- FilterSetSerializer: field exposure, read-only constraints, validation
- FilterSetsViewSet configuration: mixins, permissions, pagination, queryset scoping
- ViewSet actions: list, create, update, partial_update, destroy
- Single-default-per-user enforcement (setting a new default clears the previous one)
- User isolation: users can only access their own filter sets
- URL routing and router registration
"""

from types import SimpleNamespace

import pytest
from django.test import TestCase
from django.urls import resolve, reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.core.models import User
from apps.dashboard_reports.models import FilterSet
from apps.dashboard_reports.serializers import FilterSetSerializer
from apps.dashboard_reports.urls import router
from apps.dashboard_reports.viewsets import FilterSetsViewSet
from tests.test_utils import get_test_password

# =============================================================================
# Helpers
# =============================================================================

_FILTERS_A = {"organization_id": [1], "date_range": "last_30_days"}
_FILTERS_B = {"organization_id": [2], "date_range": "last_7_days"}


def _create_user(username: str = "user_a", superuser: bool = True) -> User:
    # FilterSetsViewSet is now gated on dashboard edit permission. These tests cover
    # CRUD mechanics and user isolation, so a superuser keeps the permission layer out
    # of the way; the role/permission matrix is covered separately.
    if superuser:
        return User.objects.create_superuser(
            username=username, email=f"{username}@example.com", password=get_test_password()
        )
    return User.objects.create_user(username=username, email=f"{username}@example.com", password=get_test_password())


def _create_filter_set(
    user: User, name: str = "My Filters", filters: dict | None = None, is_default: bool = False
) -> FilterSet:
    return FilterSet.objects.create(
        user=user,
        name=name,
        filters=filters or _FILTERS_A,
        is_default=is_default,
    )


# =============================================================================
# Serializer tests
# =============================================================================


@pytest.mark.unit
class TestFilterSetSerializer(TestCase):
    def setUp(self) -> None:
        self.user = _create_user()

    def tearDown(self) -> None:
        FilterSet.objects.all().delete()
        User.objects.all().delete()

    def test_meta_model(self) -> None:
        assert FilterSetSerializer.Meta.model is FilterSet

    def test_exposes_expected_fields(self) -> None:
        instance = _create_filter_set(self.user)
        data = FilterSetSerializer(instance).data
        assert set(data.keys()) == {"id", "name", "filters", "is_default"}

    def test_id_is_read_only(self) -> None:
        assert FilterSetSerializer().fields["id"].read_only is True

    def test_valid_payload_is_accepted(self) -> None:
        serializer = FilterSetSerializer(
            data={"name": "Work Filters", "filters": _FILTERS_A, "is_default": False},
            context={"request": SimpleNamespace(user=self.user)},
        )
        assert serializer.is_valid(), serializer.errors

    def test_name_is_required(self) -> None:
        serializer = FilterSetSerializer(data={"filters": _FILTERS_A, "is_default": False})
        assert not serializer.is_valid()
        assert "name" in serializer.errors

    def test_filters_is_required(self) -> None:
        serializer = FilterSetSerializer(data={"name": "Work Filters", "is_default": False})
        assert not serializer.is_valid()
        assert "filters" in serializer.errors

    def test_is_default_defaults_to_false_when_omitted(self) -> None:
        serializer = FilterSetSerializer(
            data={"name": "Work Filters", "filters": _FILTERS_A},
            context={"request": SimpleNamespace(user=self.user)},
        )
        assert serializer.is_valid(), serializer.errors
        # When omitted, is_default is absent from validated_data (DRF applies the
        # default=False at save time, not in validated_data). Either absent or False is correct.
        assert serializer.validated_data.get("is_default", False) is False

    def test_maps_values_correctly(self) -> None:
        instance = _create_filter_set(self.user, name="Engineering", filters=_FILTERS_A, is_default=True)
        data = FilterSetSerializer(instance).data
        assert data["name"] == "Engineering"
        assert data["filters"] == _FILTERS_A
        assert data["is_default"] is True

    def test_filters_accepts_arbitrary_json(self) -> None:
        complex_filters = {"organization_id": [1, 2, 3], "date_range": "custom", "labels": [10, 20]}
        serializer = FilterSetSerializer(
            data={"name": "Complex", "filters": complex_filters, "is_default": False},
            context={"request": SimpleNamespace(user=self.user)},
        )
        assert serializer.is_valid(), serializer.errors

    def test_validate_rejects_duplicate_name_for_same_user(self) -> None:
        _create_filter_set(self.user, name="Existing")
        serializer = FilterSetSerializer(
            data={"name": "Existing", "filters": _FILTERS_A, "is_default": False},
            context={"request": SimpleNamespace(user=self.user)},
        )
        assert not serializer.is_valid()
        assert "name" in serializer.errors

    def test_validate_allows_same_name_for_different_user(self) -> None:
        other_user = _create_user("other")
        _create_filter_set(other_user, name="Shared Name")
        serializer = FilterSetSerializer(
            data={"name": "Shared Name", "filters": _FILTERS_A, "is_default": False},
            context={"request": SimpleNamespace(user=self.user)},
        )
        assert serializer.is_valid(), serializer.errors

    def test_validate_allows_updating_instance_without_changing_name(self) -> None:
        instance = _create_filter_set(self.user, name="Unchanged")
        serializer = FilterSetSerializer(
            instance,
            data={"name": "Unchanged", "filters": _FILTERS_B, "is_default": False},
            context={"request": SimpleNamespace(user=self.user)},
        )
        assert serializer.is_valid(), serializer.errors


# =============================================================================
# ViewSet configuration tests
# =============================================================================


@pytest.mark.unit
class TestFilterSetsViewSetConfig(TestCase):
    def test_uses_correct_serializer_class(self) -> None:
        assert FilterSetsViewSet.serializer_class is FilterSetSerializer

    def test_versioning_class_is_none(self) -> None:
        assert FilterSetsViewSet.versioning_class is None

    def test_requires_authentication(self) -> None:
        from apps.dashboard_reports.permissions import IsDashboardEditor

        assert IsDashboardEditor in FilterSetsViewSet.permission_classes

    def test_has_list_mixin(self) -> None:
        from rest_framework.mixins import ListModelMixin

        assert issubclass(FilterSetsViewSet, ListModelMixin)

    def test_has_create_mixin(self) -> None:
        from rest_framework.mixins import CreateModelMixin

        assert issubclass(FilterSetsViewSet, CreateModelMixin)

    def test_has_update_mixin(self) -> None:
        from rest_framework.mixins import UpdateModelMixin

        assert issubclass(FilterSetsViewSet, UpdateModelMixin)

    def test_has_destroy_mixin(self) -> None:
        from rest_framework.mixins import DestroyModelMixin

        assert issubclass(FilterSetsViewSet, DestroyModelMixin)

    def test_does_not_have_retrieve_mixin(self) -> None:
        from rest_framework.mixins import RetrieveModelMixin

        assert not issubclass(FilterSetsViewSet, RetrieveModelMixin)

    def test_pagination_class_is_set(self) -> None:
        from ansible_base.rest_pagination import DefaultPaginator

        assert FilterSetsViewSet.pagination_class is DefaultPaginator


# =============================================================================
# Authentication / permission gate
# =============================================================================


@pytest.mark.unit
class TestFilterSetsPermissions(APITestCase):
    """Unauthenticated requests must be rejected on every endpoint."""

    def setUp(self) -> None:
        self.user = _create_user()
        self.filter_set = _create_filter_set(self.user)

    def tearDown(self) -> None:
        FilterSet.objects.all().delete()
        User.objects.all().delete()

    def test_list_denied_for_unauthenticated(self) -> None:
        url = reverse("v1:filter_sets-list")
        response = self.client.get(url)
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_create_denied_for_unauthenticated(self) -> None:
        url = reverse("v1:filter_sets-list")
        response = self.client.post(url, {"name": "X", "filters": {}, "is_default": False}, format="json")
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_update_denied_for_unauthenticated(self) -> None:
        url = reverse("v1:filter_sets-detail", kwargs={"pk": self.filter_set.pk})
        response = self.client.put(url, {"name": "X", "filters": {}, "is_default": False}, format="json")
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_partial_update_denied_for_unauthenticated(self) -> None:
        url = reverse("v1:filter_sets-detail", kwargs={"pk": self.filter_set.pk})
        response = self.client.patch(url, {"name": "X"}, format="json")
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_delete_denied_for_unauthenticated(self) -> None:
        url = reverse("v1:filter_sets-detail", kwargs={"pk": self.filter_set.pk})
        response = self.client.delete(url)
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_list_accessible_for_authenticated_user(self) -> None:
        self.client.force_authenticate(user=self.user)
        url = reverse("v1:filter_sets-list")
        response = self.client.get(url)
        assert response.status_code == status.HTTP_200_OK


# =============================================================================
# GET /filter-sets — list
# =============================================================================


@pytest.mark.unit
class TestFilterSetsListEndpoint(APITestCase):
    def setUp(self) -> None:
        self.user = _create_user()
        self.client.force_authenticate(user=self.user)

    def tearDown(self) -> None:
        FilterSet.objects.all().delete()
        User.objects.all().delete()

    def test_list_returns_200(self) -> None:
        url = reverse("v1:filter_sets-list")
        assert self.client.get(url).status_code == status.HTTP_200_OK

    def test_list_returns_paginated_response(self) -> None:
        url = reverse("v1:filter_sets-list")
        response = self.client.get(url)
        assert "results" in response.data
        assert "count" in response.data

    def test_list_returns_empty_when_no_filter_sets(self) -> None:
        url = reverse("v1:filter_sets-list")
        response = self.client.get(url)
        assert response.data["count"] == 0
        assert response.data["results"] == []

    def test_list_returns_only_own_filter_sets(self) -> None:
        """GET /filter-sets must return only the requesting user's filter sets."""
        other_user = _create_user("other")
        _create_filter_set(self.user, "Mine")
        _create_filter_set(other_user, "Not Mine")

        url = reverse("v1:filter_sets-list")
        response = self.client.get(url)

        assert response.data["count"] == 1
        assert response.data["results"][0]["name"] == "Mine"

    def test_list_response_contains_expected_fields(self) -> None:
        _create_filter_set(self.user)
        url = reverse("v1:filter_sets-list")
        response = self.client.get(url)
        entry = response.data["results"][0]
        assert set(entry.keys()) == {"id", "name", "filters", "is_default"}

    def test_list_returns_correct_count_for_multiple_filter_sets(self) -> None:
        _create_filter_set(self.user, "Set 1")
        _create_filter_set(self.user, "Set 2")
        url = reverse("v1:filter_sets-list")
        response = self.client.get(url)
        assert response.data["count"] == 2

    def test_list_shows_correct_is_default_values(self) -> None:
        _create_filter_set(self.user, "Default", is_default=True)
        _create_filter_set(self.user, "Not Default", is_default=False)
        url = reverse("v1:filter_sets-list")
        response = self.client.get(url)
        defaults = [r["is_default"] for r in response.data["results"]]
        assert True in defaults
        assert False in defaults


# =============================================================================
# POST /filter-sets — create
# =============================================================================


@pytest.mark.unit
class TestFilterSetsCreateEndpoint(APITestCase):
    def setUp(self) -> None:
        self.user = _create_user()
        self.client.force_authenticate(user=self.user)

    def tearDown(self) -> None:
        FilterSet.objects.all().delete()
        User.objects.all().delete()

    def test_create_returns_201(self) -> None:
        url = reverse("v1:filter_sets-list")
        data = {"name": "New Set", "filters": _FILTERS_A, "is_default": False}
        assert self.client.post(url, data, format="json").status_code == status.HTTP_201_CREATED

    def test_create_persists_to_db(self) -> None:
        url = reverse("v1:filter_sets-list")
        self.client.post(url, {"name": "Persisted", "filters": _FILTERS_A, "is_default": False}, format="json")
        assert FilterSet.objects.filter(user=self.user, name="Persisted").exists()

    def test_create_associates_filter_set_with_requesting_user(self) -> None:
        """POST must set user=request.user — not require the caller to supply it."""
        url = reverse("v1:filter_sets-list")
        response = self.client.post(
            url, {"name": "Auto-owned", "filters": _FILTERS_A, "is_default": False}, format="json"
        )
        pk = response.data["id"]
        assert FilterSet.objects.get(pk=pk).user == self.user

    def test_create_response_contains_expected_fields(self) -> None:
        url = reverse("v1:filter_sets-list")
        response = self.client.post(
            url, {"name": "Fields Check", "filters": _FILTERS_A, "is_default": False}, format="json"
        )
        assert set(response.data.keys()) == {"id", "name", "filters", "is_default"}

    def test_create_with_is_default_true_sets_as_default(self) -> None:
        url = reverse("v1:filter_sets-list")
        response = self.client.post(
            url, {"name": "My Default", "filters": _FILTERS_A, "is_default": True}, format="json"
        )
        assert response.status_code == status.HTTP_201_CREATED
        assert FilterSet.objects.get(pk=response.data["id"]).is_default is True

    def test_create_new_default_clears_previous_default(self) -> None:
        """POST with is_default=True must clear the previous default for the same user."""
        old_default = _create_filter_set(self.user, "Old Default", is_default=True)
        url = reverse("v1:filter_sets-list")
        self.client.post(url, {"name": "New Default", "filters": _FILTERS_A, "is_default": True}, format="json")

        old_default.refresh_from_db()
        assert old_default.is_default is False
        assert FilterSet.objects.filter(user=self.user, is_default=True).count() == 1

    def test_create_new_default_does_not_affect_other_users_default(self) -> None:
        """Setting a new default for user A must not clear user B's default."""
        other_user = _create_user("other")
        other_default = _create_filter_set(other_user, "Other Default", is_default=True)

        url = reverse("v1:filter_sets-list")
        self.client.post(url, {"name": "My Default", "filters": _FILTERS_A, "is_default": True}, format="json")

        other_default.refresh_from_db()
        assert other_default.is_default is True

    def test_create_without_name_returns_400(self) -> None:
        url = reverse("v1:filter_sets-list")
        response = self.client.post(url, {"filters": _FILTERS_A, "is_default": False}, format="json")
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_create_without_filters_returns_400(self) -> None:
        url = reverse("v1:filter_sets-list")
        response = self.client.post(url, {"name": "No Filters", "is_default": False}, format="json")
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_create_with_duplicate_name_for_same_user_returns_400(self) -> None:
        _create_filter_set(self.user, name="Duplicate")
        url = reverse("v1:filter_sets-list")
        response = self.client.post(
            url, {"name": "Duplicate", "filters": _FILTERS_A, "is_default": False}, format="json"
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "name" in response.data

    def test_create_with_duplicate_name_for_different_user_returns_201(self) -> None:
        other_user = _create_user("other")
        _create_filter_set(other_user, name="Shared Name")
        url = reverse("v1:filter_sets-list")
        response = self.client.post(
            url, {"name": "Shared Name", "filters": _FILTERS_A, "is_default": False}, format="json"
        )
        assert response.status_code == status.HTTP_201_CREATED


# =============================================================================
# PUT /filter-sets/{id} — full update
# =============================================================================


@pytest.mark.unit
class TestFilterSetsUpdateEndpoint(APITestCase):
    def setUp(self) -> None:
        self.user = _create_user()
        self.client.force_authenticate(user=self.user)
        self.filter_set = _create_filter_set(self.user, "Original", _FILTERS_A, is_default=False)

    def tearDown(self) -> None:
        FilterSet.objects.all().delete()
        User.objects.all().delete()

    def test_update_returns_200(self) -> None:
        url = reverse("v1:filter_sets-detail", kwargs={"pk": self.filter_set.pk})
        response = self.client.put(url, {"name": "Updated", "filters": _FILTERS_B, "is_default": False}, format="json")
        assert response.status_code == status.HTTP_200_OK

    def test_update_persists_name(self) -> None:
        url = reverse("v1:filter_sets-detail", kwargs={"pk": self.filter_set.pk})
        self.client.put(url, {"name": "Renamed", "filters": _FILTERS_A, "is_default": False}, format="json")
        self.filter_set.refresh_from_db()
        assert self.filter_set.name == "Renamed"

    def test_update_persists_filters(self) -> None:
        url = reverse("v1:filter_sets-detail", kwargs={"pk": self.filter_set.pk})
        self.client.put(url, {"name": "Original", "filters": _FILTERS_B, "is_default": False}, format="json")
        self.filter_set.refresh_from_db()
        assert self.filter_set.filters == _FILTERS_B

    def test_update_setting_is_default_true_clears_previous_default(self) -> None:
        """PUT with is_default=True must clear the existing default for this user."""
        old_default = _create_filter_set(self.user, "Old Default", is_default=True)
        url = reverse("v1:filter_sets-detail", kwargs={"pk": self.filter_set.pk})
        self.client.put(url, {"name": "Original", "filters": _FILTERS_A, "is_default": True}, format="json")

        old_default.refresh_from_db()
        self.filter_set.refresh_from_db()
        assert old_default.is_default is False
        assert self.filter_set.is_default is True

    def test_update_returns_404_for_other_users_filter_set(self) -> None:
        """PUT on another user's filter set must return 404 (not 403 — no information leak)."""
        other_user = _create_user("other")
        other_fs = _create_filter_set(other_user, "Their Set")
        url = reverse("v1:filter_sets-detail", kwargs={"pk": other_fs.pk})
        response = self.client.put(url, {"name": "Hijacked", "filters": _FILTERS_A, "is_default": False}, format="json")
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_update_returns_404_for_nonexistent_id(self) -> None:
        url = reverse("v1:filter_sets-detail", kwargs={"pk": 999999})
        response = self.client.put(url, {"name": "X", "filters": _FILTERS_A, "is_default": False}, format="json")
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_update_response_contains_expected_fields(self) -> None:
        url = reverse("v1:filter_sets-detail", kwargs={"pk": self.filter_set.pk})
        response = self.client.put(url, {"name": "Updated", "filters": _FILTERS_B, "is_default": False}, format="json")
        assert set(response.data.keys()) == {"id", "name", "filters", "is_default"}

    def test_update_does_not_change_owner(self) -> None:
        url = reverse("v1:filter_sets-detail", kwargs={"pk": self.filter_set.pk})
        self.client.put(url, {"name": "Updated", "filters": _FILTERS_B, "is_default": False}, format="json")
        self.filter_set.refresh_from_db()
        assert self.filter_set.user == self.user

    def test_update_rejects_rename_to_existing_name(self) -> None:
        """PUT that renames to a name already used by another of the user's filter sets must fail."""
        _create_filter_set(self.user, name="Taken")
        url = reverse("v1:filter_sets-detail", kwargs={"pk": self.filter_set.pk})
        response = self.client.put(url, {"name": "Taken", "filters": _FILTERS_A, "is_default": False}, format="json")
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "name" in response.data

    def test_update_allows_keeping_its_own_name(self) -> None:
        """PUT that keeps the same name on the same instance must not be treated as a duplicate."""
        url = reverse("v1:filter_sets-detail", kwargs={"pk": self.filter_set.pk})
        response = self.client.put(url, {"name": "Original", "filters": _FILTERS_B, "is_default": False}, format="json")
        assert response.status_code == status.HTTP_200_OK


# =============================================================================
# DELETE /filter-sets/{id} — destroy
# =============================================================================


@pytest.mark.unit
class TestFilterSetsDeleteEndpoint(APITestCase):
    def setUp(self) -> None:
        self.user = _create_user()
        self.client.force_authenticate(user=self.user)
        self.filter_set = _create_filter_set(self.user, "To Delete")

    def tearDown(self) -> None:
        FilterSet.objects.all().delete()
        User.objects.all().delete()

    def test_delete_returns_204(self) -> None:
        url = reverse("v1:filter_sets-detail", kwargs={"pk": self.filter_set.pk})
        assert self.client.delete(url).status_code == status.HTTP_204_NO_CONTENT

    def test_delete_removes_from_db(self) -> None:
        pk = self.filter_set.pk
        url = reverse("v1:filter_sets-detail", kwargs={"pk": pk})
        self.client.delete(url)
        assert not FilterSet.objects.filter(pk=pk).exists()

    def test_delete_returns_404_for_other_users_filter_set(self) -> None:
        """DELETE on another user's filter set must return 404."""
        other_user = _create_user("other")
        other_fs = _create_filter_set(other_user, "Not Mine")
        url = reverse("v1:filter_sets-detail", kwargs={"pk": other_fs.pk})
        response = self.client.delete(url)
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_delete_returns_404_for_nonexistent_id(self) -> None:
        url = reverse("v1:filter_sets-detail", kwargs={"pk": 999999})
        assert self.client.delete(url).status_code == status.HTTP_404_NOT_FOUND

    def test_delete_does_not_remove_other_users_filter_set(self) -> None:
        """A failed 404 delete must not affect another user's record."""
        other_user = _create_user("other")
        other_fs = _create_filter_set(other_user, "Safe")
        url = reverse("v1:filter_sets-detail", kwargs={"pk": other_fs.pk})
        self.client.delete(url)
        assert FilterSet.objects.filter(pk=other_fs.pk).exists()


# =============================================================================
# User isolation (end-to-end multi-user scenario)
# =============================================================================


@pytest.mark.unit
class TestFilterSetsUserIsolation(APITestCase):
    """
    Validates the full end-to-end user isolation story:
        - User A's filter sets are invisible to User B and vice versa.
        - Setting a default for User A does not affect User B's default.
    """

    def setUp(self) -> None:
        self.user_a = _create_user("user_a")
        self.user_b = _create_user("user_b")

    def tearDown(self) -> None:
        FilterSet.objects.all().delete()
        User.objects.all().delete()

    def test_user_b_cannot_see_user_a_filter_sets(self) -> None:
        _create_filter_set(self.user_a, "A's Set")
        self.client.force_authenticate(user=self.user_b)
        url = reverse("v1:filter_sets-list")
        response = self.client.get(url)
        assert response.data["count"] == 0

    def test_user_a_cannot_see_user_b_filter_sets(self) -> None:
        _create_filter_set(self.user_b, "B's Set")
        self.client.force_authenticate(user=self.user_a)
        url = reverse("v1:filter_sets-list")
        response = self.client.get(url)
        assert response.data["count"] == 0

    def test_each_user_sees_only_their_own(self) -> None:
        _create_filter_set(self.user_a, "A1")
        _create_filter_set(self.user_a, "A2")
        _create_filter_set(self.user_b, "B1")

        self.client.force_authenticate(user=self.user_a)
        url = reverse("v1:filter_sets-list")
        assert self.client.get(url).data["count"] == 2

        self.client.force_authenticate(user=self.user_b)
        assert self.client.get(url).data["count"] == 1

    def test_default_for_user_a_does_not_clear_default_for_user_b(self) -> None:
        b_default = _create_filter_set(self.user_b, "B Default", is_default=True)

        self.client.force_authenticate(user=self.user_a)
        url = reverse("v1:filter_sets-list")
        self.client.post(url, {"name": "A Default", "filters": _FILTERS_A, "is_default": True}, format="json")

        b_default.refresh_from_db()
        assert b_default.is_default is True

    def test_two_users_can_each_have_one_default(self) -> None:
        _create_filter_set(self.user_a, "A Default", is_default=True)
        _create_filter_set(self.user_b, "B Default", is_default=True)

        assert FilterSet.objects.filter(user=self.user_a, is_default=True).count() == 1
        assert FilterSet.objects.filter(user=self.user_b, is_default=True).count() == 1


# =============================================================================
# Single-default-per-user enforcement
# =============================================================================


@pytest.mark.unit
class TestFilterSetsSingleDefault(APITestCase):
    """Only one filter set per user may have is_default=True at any time."""

    def setUp(self) -> None:
        self.user = _create_user()
        self.client.force_authenticate(user=self.user)

    def tearDown(self) -> None:
        FilterSet.objects.all().delete()
        User.objects.all().delete()

    def test_creating_second_default_clears_first(self) -> None:
        url = reverse("v1:filter_sets-list")
        r1 = self.client.post(url, {"name": "First", "filters": _FILTERS_A, "is_default": True}, format="json")
        self.client.post(url, {"name": "Second", "filters": _FILTERS_B, "is_default": True}, format="json")

        assert FilterSet.objects.get(pk=r1.data["id"]).is_default is False
        assert FilterSet.objects.filter(user=self.user, is_default=True).count() == 1

    def test_at_most_one_default_after_multiple_creates(self) -> None:
        url = reverse("v1:filter_sets-list")
        for i in range(3):
            self.client.post(url, {"name": f"Set {i}", "filters": _FILTERS_A, "is_default": True}, format="json")
        assert FilterSet.objects.filter(user=self.user, is_default=True).count() == 1

    def test_updating_to_default_true_via_put_clears_existing(self) -> None:
        existing_default = _create_filter_set(self.user, "Existing Default", is_default=True)
        target = _create_filter_set(self.user, "Will Become Default", is_default=False)

        url = reverse("v1:filter_sets-detail", kwargs={"pk": target.pk})
        self.client.put(url, {"name": "Will Become Default", "filters": _FILTERS_A, "is_default": True}, format="json")

        existing_default.refresh_from_db()
        target.refresh_from_db()
        assert existing_default.is_default is False
        assert target.is_default is True

    def test_non_default_create_does_not_affect_existing_default(self) -> None:
        existing_default = _create_filter_set(self.user, "Keep As Default", is_default=True)
        url = reverse("v1:filter_sets-list")
        self.client.post(url, {"name": "Not Default", "filters": _FILTERS_B, "is_default": False}, format="json")

        existing_default.refresh_from_db()
        assert existing_default.is_default is True


# =============================================================================
# URL routing tests
# =============================================================================


@pytest.mark.unit
class TestFilterSetsUrls(TestCase):
    def test_filter_sets_viewset_is_registered_in_router(self) -> None:
        assert FilterSetsViewSet in [reg[1] for reg in router.registry]

    def test_list_url_resolves_to_filter_sets_viewset(self) -> None:
        assert resolve("/api/v1/dashboard_reports/filter_sets/").func.cls is FilterSetsViewSet

    def test_detail_url_resolves_to_filter_sets_viewset(self) -> None:
        assert resolve("/api/v1/dashboard_reports/filter_sets/1/").func.cls is FilterSetsViewSet

    def test_get_mapped_to_list(self) -> None:
        assert resolve("/api/v1/dashboard_reports/filter_sets/").func.actions.get("get") == "list"

    def test_post_mapped_to_create(self) -> None:
        assert resolve("/api/v1/dashboard_reports/filter_sets/").func.actions.get("post") == "create"

    def test_put_mapped_to_update(self) -> None:
        assert resolve("/api/v1/dashboard_reports/filter_sets/1/").func.actions.get("put") == "update"

    def test_patch_mapped_to_partial_update(self) -> None:
        assert resolve("/api/v1/dashboard_reports/filter_sets/1/").func.actions.get("patch") == "partial_update"

    def test_delete_mapped_to_destroy(self) -> None:
        assert resolve("/api/v1/dashboard_reports/filter_sets/1/").func.actions.get("delete") == "destroy"

    def test_list_url_can_be_reversed(self) -> None:
        assert "filter_sets" in reverse("v1:filter_sets-list")

    def test_detail_url_can_be_reversed(self) -> None:
        url = reverse("v1:filter_sets-detail", kwargs={"pk": 1})
        assert "filter_sets" in url and "1" in url


# =============================================================================
# Import / smoke tests
# =============================================================================


@pytest.mark.unit
class TestFilterSetsImports(TestCase):
    def test_viewset_is_importable(self) -> None:
        assert callable(FilterSetsViewSet)

    def test_serializer_is_importable(self) -> None:
        assert callable(FilterSetSerializer)

    def test_viewset_exported_from_viewsets_init(self) -> None:
        from apps.dashboard_reports.viewsets import FilterSetsViewSet as ImportedFilterSetsViewSet

        assert ImportedFilterSetsViewSet is FilterSetsViewSet
