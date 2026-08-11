import uuid

from django.db import migrations, models


def asignar_tokens_qr(apps, schema_editor):
    Usuario = apps.get_model("acceso", "Usuario")
    for usuario in Usuario.objects.all().iterator():
        usuario.token_qr = uuid.uuid4()
        usuario.save(update_fields=["token_qr"])


class Migration(migrations.Migration):
    dependencies = [("acceso", "0010_alter_visitante_numero_documento")]

    operations = [
        migrations.AddField(
            model_name="usuario",
            name="telefono",
            field=models.CharField(blank=True, max_length=20),
        ),
        migrations.AddField(
            model_name="usuario",
            name="token_qr",
            field=models.UUIDField(null=True, editable=False),
        ),
        migrations.RunPython(asignar_tokens_qr, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="usuario",
            name="token_qr",
            field=models.UUIDField(
                default=uuid.uuid4, editable=False, unique=True, db_index=True
            ),
        ),
        migrations.AlterField(
            model_name="ingresolog",
            name="tipo",
            field=models.CharField(
                choices=[
                    ("visita", "Visita (RUT)"),
                    ("residente", "Residente (QR propio)"),
                    ("vehiculo", "Vehículo (patente)"),
                ],
                max_length=10,
            ),
        ),
    ]
