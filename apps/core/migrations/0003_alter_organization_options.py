from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0002_remove_shared_user_content_type"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="organization",
            options={
                "permissions": [
                    ("member_organization", "User is member of this organization"),
                    ("view_automation_dashboard", "User can view the automation dashboard for this organization"),
                    ("change_automation_dashboard", "User can edit the automation dashboard for this organization"),
                ]
            },
        ),
    ]
