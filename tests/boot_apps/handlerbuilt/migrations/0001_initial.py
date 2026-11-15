from django.core.handlers.wsgi import WSGIHandler
from django.db import migrations

# A handler built while Boot reads the migrations, which Boot refuses
# instead of waiting on itself.
WSGIHandler()


class Migration(migrations.Migration):
    dependencies = []
    operations = []
