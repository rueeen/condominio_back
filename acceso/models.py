import re
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


# ---------------------------------------------------------------------------
# Usuario con rol (reemplaza al User por defecto de Django)
# ---------------------------------------------------------------------------
class Usuario(AbstractUser):
    class Rol(models.TextChoices):
        PROPIETARIO = "propietario", "Propietario"
        GUARDIA = "guardia", "Guardia"
        ADMIN = "admin", "Administrador"

    rol = models.CharField(max_length=20, choices=Rol.choices)
    unidad = models.CharField(
        max_length=20, blank=True, null=True,
        help_text="Depto/casa, solo aplica a propietarios"
    )

    def __str__(self):
        return f"{self.get_full_name() or self.username} ({self.rol})"


# ---------------------------------------------------------------------------
# Validadores de formato chileno
# ---------------------------------------------------------------------------
RUT_REGEX = re.compile(r"^\d{7,8}-[\dkK]$")
# Formato nuevo (2007+): 4 letras + 2 números | Formato antiguo: 2 letras + 4 números
PATENTE_REGEX = re.compile(r"^([A-Z]{4}\d{2}|[A-Z]{2}\d{4})$")


def validar_rut(value):
    if not RUT_REGEX.match(value):
        raise ValidationError("RUT inválido. Formato esperado: 12345678-9")


def validar_patente(value):
    if not PATENTE_REGEX.match(value.upper()):
        raise ValidationError(
            "Patente inválida. Formatos válidos: AABB11 o AA1111")


# ---------------------------------------------------------------------------
# Visitantes (acceso temporal por RUT)
# ---------------------------------------------------------------------------
class Visitante(models.Model):
    rut = models.CharField(max_length=10, validators=[validar_rut])
    nombre = models.CharField(max_length=150)
    propietario = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="visitantes", limit_choices_to={"rol": "propietario"}
    )
    fecha_inicio = models.DateTimeField(default=timezone.now)
    fecha_fin = models.DateTimeField()
    creado_en = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        # Si no se especifica, la vigencia por defecto es 4 horas
        if not self.fecha_fin:
            self.fecha_fin = self.fecha_inicio + timedelta(hours=4)
        super().save(*args, **kwargs)

    @property
    def vigente(self):
        return self.fecha_inicio <= timezone.now() <= self.fecha_fin

    def __str__(self):
        return f"{self.nombre} ({self.rut}) - {self.propietario.unidad}"


# ---------------------------------------------------------------------------
# Vehículos (requieren aprobación de administrador)
# ---------------------------------------------------------------------------
class Vehiculo(models.Model):
    class Estado(models.TextChoices):
        PENDIENTE = "pendiente", "Pendiente"
        APROBADO = "aprobado", "Aprobado"
        RECHAZADO = "rechazado", "Rechazado"

    patente = models.CharField(max_length=6, validators=[validar_patente])
    propietario = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="vehiculos", limit_choices_to={"rol": "propietario"}
    )
    estado = models.CharField(
        max_length=10, choices=Estado.choices, default=Estado.PENDIENTE
    )
    aprobado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="vehiculos_aprobados",
        limit_choices_to={"rol": "admin"}
    )
    fecha_solicitud = models.DateTimeField(auto_now_add=True)
    fecha_resolucion = models.DateTimeField(null=True, blank=True)
    motivo_rechazo = models.CharField(max_length=255, blank=True)

    class Meta:
        # Máximo 2 patentes aprobadas o pendientes por propietario se valida en el serializer,
        # aquí solo evitamos duplicar la misma patente para el mismo propietario.
        unique_together = ("patente", "propietario")

    def save(self, *args, **kwargs):
        self.patente = self.patente.upper()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.patente} - {self.estado}"


# ---------------------------------------------------------------------------
# Log de todos los intentos de ingreso (auditoría)
# ---------------------------------------------------------------------------
class IngresoLog(models.Model):
    class Tipo(models.TextChoices):
        VISITA = "visita", "Visita (RUT)"
        VEHICULO = "vehiculo", "Vehículo (patente)"

    class Resultado(models.TextChoices):
        PERMITIDO = "permitido", "Permitido"
        DENEGADO = "denegado", "Denegado"

    tipo = models.CharField(max_length=10, choices=Tipo.choices)
    valor_ingresado = models.CharField(
        max_length=20, help_text="RUT o patente tal como se ingresó/leyó"
    )
    resultado = models.CharField(max_length=10, choices=Resultado.choices)
    detalle = models.CharField(max_length=255, blank=True)
    guardia = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, related_name="ingresos_registrados",
        limit_choices_to={"rol": "guardia"}
    )
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-timestamp"]

    def __str__(self):
        return f"[{self.timestamp:%Y-%m-%d %H:%M}] {self.tipo} {self.valor_ingresado} -> {self.resultado}"
