import re
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
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
    torre = models.PositiveSmallIntegerField(
        null=True, blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(25)],
        help_text="Torre del condominio (1 a 25). Solo aplica a propietarios.",
    )
    departamento = models.PositiveSmallIntegerField(
        null=True, blank=True,
        help_text="Número de departamento dentro de la torre. Solo aplica a propietarios.",
    )

    def clean(self):
        super().clean()
        if self.rol == self.Rol.PROPIETARIO and (self.torre is None or self.departamento is None):
            raise ValidationError(
                "Un propietario debe tener torre y departamento asignados.")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["torre", "departamento"],
                condition=models.Q(rol="propietario"),
                name="unidad_unica_por_propietario",
            )
        ]

    @property
    def unidad(self):
        """Representación legible 'Torre X, Depto Y', usada en logs y respuestas de la API."""
        if self.torre and self.departamento:
            return f"Torre {self.torre}, Depto {self.departamento}"
        return ""

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


def enmascarar_rut(rut: str) -> str:
    """
    Oculta parte del cuerpo del RUT para logs/auditoría, dejando el inicio
    y el dígito verificador visibles (ej. '12345678-9' -> '12****78-9').
    El RUT es un identificador único nacional (dato personal) y quien
    aparece en el log de ingresos suele ser una visita, no un usuario del
    sistema — no hay razón operativa para mostrarlo completo por defecto.
    """
    if not rut or "-" not in rut:
        return rut
    cuerpo, dv = rut.split("-", 1)
    if len(cuerpo) <= 4:
        return rut
    return f"{cuerpo[:2]}{'*' * (len(cuerpo) - 4)}{cuerpo[-2:]}-{dv}"


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
# Estacionamientos (cada propietario puede tener uno o más)
# ---------------------------------------------------------------------------
class Estacionamiento(models.Model):
    # Único a nivel global: un mismo estacionamiento físico no puede
    # quedar asignado por error a dos propietarios distintos.
    numero = models.CharField(max_length=10, unique=True)
    propietario = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="estacionamientos", limit_choices_to={"rol": "propietario"}
    )

    class Meta:
        ordering = ["numero"]

    def __str__(self):
        return f"Estacionamiento {self.numero} - {self.propietario.unidad}"


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
        # Una misma patente no puede tener más de una solicitud activa
        # (pendiente o aprobada) en todo el sistema — pero si fue
        # rechazada, se puede volver a intentar (no aplica la restricción
        # sobre filas con estado='rechazado').
        constraints = [
            models.UniqueConstraint(
                fields=["patente"],
                condition=models.Q(estado__in=["pendiente", "aprobado"]),
                name="patente_unica_activa",
            )
        ]

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
