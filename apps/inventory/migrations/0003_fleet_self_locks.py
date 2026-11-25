"""Fleet watches its own dependency locks (they were left empty in 0002)."""

from django.db import migrations

PYTHON_LOCKS = ["Foxugly/Fleet_server:requirements.txt"]
NPM_LOCKS = ["Foxugly/Fleet_frontend:package-lock.json"]


def set_locks(apps, schema_editor):
    App = apps.get_model("inventory", "App")
    app = App.objects.filter(slug="fleet").first()
    if app is None:
        return
    # Only fill what is still empty: never overwrite a value set from the SPA.
    if not app.python_locks:
        app.python_locks = PYTHON_LOCKS
    if not app.npm_locks:
        app.npm_locks = NPM_LOCKS
    app.save(update_fields=["python_locks", "npm_locks"])


def unset_locks(apps, schema_editor):
    App = apps.get_model("inventory", "App")
    app = App.objects.filter(slug="fleet").first()
    if app is None:
        return
    if app.python_locks == PYTHON_LOCKS:
        app.python_locks = []
    if app.npm_locks == NPM_LOCKS:
        app.npm_locks = []
    app.save(update_fields=["python_locks", "npm_locks"])


class Migration(migrations.Migration):
    dependencies = [("inventory", "0002_import_fleet_yaml")]

    operations = [migrations.RunPython(set_locks, unset_locks)]
