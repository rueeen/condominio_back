import re
import uuid
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
# Validadores de documentos y patentes
# ---------------------------------------------------------------------------
RUT_REGEX = re.compile(r"^\d{7,8}-[\dK]$")
DOCUMENTO_EXTRANJERO_REGEX = re.compile(r"^[\w./ -]+$", re.UNICODE)
# Patente genérica: solo letras y números (sin guiones, puntos ni espacios),
# para aceptar tanto formatos chilenos (AABB11 / AA1111) como extranjeros.
PATENTE_REGEX = re.compile(r"^[A-Z0-9]{4,10}$")


def normalizar_rut(value):
    """Normaliza y valida un RUT chileno mediante el algoritmo módulo 11."""
    rut = re.sub(r"[.\s]", "", str(value or "")).upper()
    if not RUT_REGEX.fullmatch(rut):
        raise ValidationError("RUT inválido. Formato esperado: 12345678-9")

    cuerpo, dv_ingresado = rut.split("-")
    suma = sum(int(digito) * factor for digito, factor in zip(
        reversed(cuerpo), (2, 3, 4, 5, 6, 7) * 2
    ))
    resultado = 11 - suma % 11
    dv_esperado = "0" if resultado == 11 else "K" if resultado == 10 else str(resultado)
    if dv_ingresado != dv_esperado:
        raise ValidationError("RUT inválido: dígito verificador incorrecto.")
    return rut


def validar_rut(value):
    normalizar_rut(value)


def normalizar_documento(tipo_documento, numero_documento):
    """Normaliza un documento sin imponer formatos nacionales inexistentes."""
    tipo = str(tipo_documento or "").strip().lower()
    if tipo == Visitante.TipoDocumento.RUT:
        return normalizar_rut(numero_documento)

    numero = " ".join(str(numero_documento or "").strip().split()).upper()
    if not numero:
        raise ValidationError("El número de documento es obligatorio.")
    if not 3 <= len(numero) <= 40:
        raise ValidationError("El documento debe tener entre 3 y 40 caracteres.")
    if not DOCUMENTO_EXTRANJERO_REGEX.fullmatch(numero):
        raise ValidationError(
            "El documento solo puede contener letras, números, espacios, puntos, guiones o '/'."
        )
    return numero


def validar_patente(value):
    if not PATENTE_REGEX.fullmatch(str(value or "").upper()):
        raise ValidationError(
            "Patente inválida. Usa solo letras y números, sin guiones ni espacios "
            "(entre 4 y 10 caracteres)."
        )


def normalizar_patente(value):
    """Elimina separadores y normaliza una patente a mayúsculas."""
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def enmascarar_rut(rut: str) -> str:
    """
    Oculta parte del cuerpo del RUT para logs/auditoría, dejando el inicio
    y el dígito verificador visibles (ej. '12345678-9' -> '12****78-9').
    El RUT es un identificador único nacional (dato personal) y quien
    aparece en el log de ingresos suele ser una visita, no un usuario del
    sistema — no hay razón operativa para mostrarlo completo por defecto.
    """
    if not rut or not RUT_REGEX.fullmatch(rut):
        return rut
    cuerpo, dv = rut.split("-", 1)
    if len(cuerpo) <= 4:
        return rut
    return f"{cuerpo[:2]}{'*' * (len(cuerpo) - 4)}{cuerpo[-2:]}-{dv}"


# ---------------------------------------------------------------------------
# Visitantes (acceso temporal por documento de identidad)
# ---------------------------------------------------------------------------
class Visitante(models.Model):
    class TipoDocumento(models.TextChoices):
        RUT = "rut", "RUT"
        PASAPORTE = "pasaporte", "Pasaporte"
        DNI = "dni", "DNI"
        OTRO = "otro", "Otro"

    tipo_documento = models.CharField(max_length=12, choices=TipoDocumento.choices)
    numero_documento = models.CharField(max_length=40)
    pais_documento = models.CharField(max_length=100, blank=True)
    token_qr = models.UUIDField(
        default=uuid.uuid4, editable=False, unique=True, db_index=True
    )
    nombre = models.CharField(max_length=150)
    propietario = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="visitantes", limit_choices_to={"rol": "propietario"}
    )
    fecha_inicio = models.DateTimeField(default=timezone.now)
    fecha_fin = models.DateTimeField(blank=True)
    creado_en = models.DateTimeField(auto_now_add=True)

    @classmethod
    def validar_vigencia(cls, fecha_inicio=None, fecha_fin=None):
        """Completa y valida el rango de vigencia de una visita."""
        fecha_inicio = fecha_inicio or timezone.now()
        fecha_fin = fecha_fin or fecha_inicio + timedelta(hours=4)

        errores = {}
        if not timezone.is_aware(fecha_inicio):
            errores["fecha_inicio"] = "La fecha de inicio debe incluir zona horaria."
        if not timezone.is_aware(fecha_fin):
            errores["fecha_fin"] = "La fecha de fin debe incluir zona horaria."
        if not errores and fecha_fin <= fecha_inicio:
            errores["fecha_fin"] = (
                "La fecha de fin debe ser estrictamente posterior a la fecha de inicio."
            )
        if errores:
            raise ValidationError(errores)

        return fecha_inicio, fecha_fin

    def save(self, *args, **kwargs):
        self.numero_documento = normalizar_documento(
            self.tipo_documento, self.numero_documento
        )
        self.pais_documento = " ".join((self.pais_documento or "").strip().split())
        self.fecha_inicio, self.fecha_fin = self.validar_vigencia(
            self.fecha_inicio, self.fecha_fin
        )
        super().save(*args, **kwargs)

    @property
    def vigente(self):
        return self.fecha_inicio <= timezone.now() <= self.fecha_fin

    def __str__(self):
        return f"{self.nombre} ({self.tipo_documento}: {self.numero_documento}) - {self.propietario.unidad}"


# ---------------------------------------------------------------------------
# Estacionamientos (cada propietario puede tener uno o más)
# ---------------------------------------------------------------------------

# Cuántas patentes activas (pendientes o aprobadas) se permiten por cada
# estacionamiento asignado. Ajustable acá si el condominio cambia la
# política (ej. subirlo a 3 para unidades con más de un vehículo por
# espacio en distintos horarios).
MAX_PATENTES_POR_ESTACIONAMIENTO = 2


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

    patente = models.CharField(max_length=10, validators=[validar_patente])
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
        self.patente = normalizar_patente(self.patente)
        validar_patente(self.patente)
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
        max_length=40, help_text="Documento normalizado o patente tal como se ingresó/leyó"
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
