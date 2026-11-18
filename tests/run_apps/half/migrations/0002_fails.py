from django.db import migrations


def fail(apps: object, schema_editor: object) -> None:
    raise RuntimeError("the second migration fails on purpose")


class Migration(migrations.Migration):
    dependencies = [("half", "0001_initial")]
    operations = [migrations.RunPython(fail)]
