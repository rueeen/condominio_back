from django.db import migrations, models


def convertir_emails_vacios_a_null(apps, schema_editor):
    Usuario = apps.get_model("acceso", "Usuario")
    Usuario.objects.filter(email="").update(email=None)


class Migration(migrations.Migration):
    dependencies = [
        ("acceso", "0011_usuario_perfil_qr_ingresolog_residente"),
    ]

    operations = [
        migrations.AlterField(
            model_name="usuario",
            name="email",
            field=models.EmailField(blank=True, max_length=254, null=True),
        ),
        migrations.RunPython(
            convertir_emails_vacios_a_null,
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="usuario",
            name="email",
            field=models.EmailField(
                blank=True,
                max_length=254,
                null=True,
                unique=True,
            ),
        ),
    ]
