from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("acceso", "0013_alter_estacionamiento_propietario")]

    operations = [
        migrations.AlterField(
            model_name="ingresolog",
            name="tipo",
            field=models.CharField(
                choices=[
                    ("visita", "Visita (documento)"),
                    ("residente", "Residente (QR propio)"),
                    ("vehiculo", "Vehículo (patente)"),
                ],
                max_length=10,
            ),
        ),
    ]
