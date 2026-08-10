import uuid

from django.db import migrations, models


def asignar_tokens_qr(apps, schema_editor):
    Visitante = apps.get_model("acceso", "Visitante")
    for visitante in Visitante.objects.all().iterator():
        visitante.token_qr = uuid.uuid4()
        visitante.save(update_fields=["token_qr"])


class Migration(migrations.Migration):
    dependencies = [("acceso", "0006_documentos_visitante")]

    operations = [
        migrations.AddField(
            model_name="visitante",
            name="token_qr",
            field=models.UUIDField(null=True, editable=False),
        ),
        migrations.RunPython(asignar_tokens_qr, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="visitante",
            name="token_qr",
            field=models.UUIDField(
                default=uuid.uuid4, editable=False, unique=True, db_index=True
            ),
        ),
    ]
