from ansible_base.lib.abstract_models.organization import AbstractOrganization
from ansible_base.resource_registry.fields import AnsibleResourceField
from django.db import models


class Organization(AbstractOrganization):
    """
    Organization model using DAB's AbstractOrganization.

    Organizations serve as the top-level container for teams and resources.
    """

    objects = models.Manager()

    # Exposes the organization's Resource Registry row as `.resource`, which
    # DAB's RBAC API and ansible_id filters join against.
    resource = AnsibleResourceField(primary_key_field="id")

    class Meta:
        permissions = [
            ("member_organization", "User is member of this organization"),
            ("view_dashboard_organization", "User can view the automation dashboard for this organization"),
            ("edit_dashboard_organization", "User can edit the automation dashboard for this organization"),
        ]
