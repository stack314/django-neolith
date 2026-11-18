from django.db import migrations


def fail(apps: object, schema_editor: object) -> None:
    raise RuntimeError("this migration fails on purpose")


class Migration(migrations.Migration):
    dependencies = [("contenttypes", "0002_remove_content_type_name")]
    operations = [migrations.RunPython(fail)]
