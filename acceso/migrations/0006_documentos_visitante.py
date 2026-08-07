from django.db import migrations, models


def migrar_ruts(apps, schema_editor):
    Visitante = apps.get_model("acceso", "Visitante")
    for visitante in Visitante.objects.all().iterator():
        visitante.tipo_documento = "rut"
        visitante.numero_documento = visitante.rut
        visitante.save(update_fields=["tipo_documento", "numero_documento"])


class Migration(migrations.Migration):
    dependencies = [("acceso", "0005_alter_visitante_fecha_fin")]

    operations = [
        migrations.AddField(
            model_name="visitante",
            name="tipo_documento",
            field=models.CharField(
                blank=True,
                choices=[
                    ("rut", "RUT"),
                    ("pasaporte", "Pasaporte"),
                    ("dni", "DNI"),
                    ("otro", "Otro"),
                ],
                max_length=12,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="visitante",
            name="numero_documento",
            field=models.CharField(blank=True, max_length=40, null=True),
        ),
        migrations.AddField(
            model_name="visitante",
            name="pais_documento",
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.RunPython(migrar_ruts, migrations.RunPython.noop),
        migrations.RemoveField(model_name="visitante", name="rut"),
        migrations.AlterField(
            model_name="visitante",
            name="tipo_documento",
            field=models.CharField(
                choices=[
                    ("rut", "RUT"),
                    ("pasaporte", "Pasaporte"),
                    ("dni", "DNI"),
                    ("otro", "Otro"),
                ],
                max_length=12,
            ),
        ),
        migrations.AlterField(
            model_name="visitante",
            name="numero_documento",
            field=models.CharField(max_length=40),
        ),
        migrations.AlterField(
            model_name="ingresolog",
            name="valor_ingresado",
            field=models.CharField(
                help_text="Documento normalizado o patente tal como se ingresó/leyó",
                max_length=40,
            ),
        ),
    ]
