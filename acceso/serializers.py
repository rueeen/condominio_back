from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from .models import (
    MAX_PATENTES_POR_ESTACIONAMIENTO,
    Estacionamiento,
    IngresoLog,
    Usuario,
    Vehiculo,
    Visitante,
    enmascarar_rut,
    normalizar_documento,
)


class CondominioTokenObtainPairSerializer(TokenObtainPairSerializer):
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token["rol"] = user.rol
        token["username"] = user.username
        token["unidad"] = user.unidad
        return token


class EstacionamientoSerializer(serializers.ModelSerializer):
    class Meta:
        model = Estacionamiento
        fields = ["id", "numero", "propietario"]
        read_only_fields = ["id"]


class VisitanteSerializer(serializers.ModelSerializer):
    vigente = serializers.BooleanField(read_only=True)

    class Meta:
        model = Visitante
        fields = [
            "id",
            "tipo_documento",
            "numero_documento",
            "pais_documento",
            "nombre",
            "propietario",
            "fecha_inicio",
            "fecha_fin",
            "creado_en",
            "vigente",
        ]
        read_only_fields = ["propietario", "creado_en", "vigente"]
        extra_kwargs = {
            "fecha_inicio": {"required": False},
            "fecha_fin": {"required": False},
        }

    def validate(self, attrs):
        tipo_documento = attrs.get(
            "tipo_documento", self.instance.tipo_documento if self.instance else None
        )
        numero_documento = attrs.get(
            "numero_documento", self.instance.numero_documento if self.instance else None
        )
        try:
            attrs["numero_documento"] = normalizar_documento(
                tipo_documento, numero_documento
            )
        except DjangoValidationError as error:
            raise serializers.ValidationError(
                {"numero_documento": error.messages}
            ) from error

        propietario = (
            self.instance.propietario
            if self.instance
            else self.context["request"].user
        )
        duplicados = Visitante.objects.filter(
            tipo_documento=tipo_documento,
            numero_documento=attrs["numero_documento"],
            propietario=propietario,
        )
        if self.instance:
            duplicados = duplicados.exclude(pk=self.instance.pk)
        if duplicados.exists():
            raise serializers.ValidationError(
                {"numero_documento": "Ya existe una autorización para este documento."}
            )

        fecha_inicio = attrs.get(
            "fecha_inicio",
            self.instance.fecha_inicio if self.instance else None,
        )
        fecha_fin = attrs.get(
            "fecha_fin",
            self.instance.fecha_fin if self.instance else None,
        )
        try:
            fecha_inicio, fecha_fin = Visitante.validar_vigencia(
                fecha_inicio, fecha_fin
            )
        except DjangoValidationError as error:
            raise serializers.ValidationError(error.message_dict) from error

        attrs["fecha_inicio"] = fecha_inicio
        attrs["fecha_fin"] = fecha_fin
        return attrs

    def create(self, validated_data):
        validated_data["propietario"] = self.context["request"].user
        return super().create(validated_data)


class VehiculoSerializer(serializers.ModelSerializer):
    # Solo lo esencial para identificar la unidad en la tabla del admin —
    # nunca se expone el propietario completo (nombre, email, etc.) acá.
    propietario_torre = serializers.IntegerField(
        source="propietario.torre", read_only=True)
    propietario_departamento = serializers.IntegerField(
        source="propietario.departamento", read_only=True)

    class Meta:
        model = Vehiculo
        fields = [
            "id",
            "patente",
            "propietario",
            "propietario_torre",
            "propietario_departamento",
            "estado",
            "aprobado_por",
            "fecha_solicitud",
            "fecha_resolucion",
            "motivo_rechazo",
        ]
        read_only_fields = [
            "propietario",
            "estado",
            "aprobado_por",
            "fecha_solicitud",
            "fecha_resolucion",
        ]

    def validate(self, attrs):
        request = self.context.get("request")
        user = getattr(request, "user", None)

        if user and user.is_authenticated:
            vehiculos_activos = Vehiculo.objects.filter(propietario=user).exclude(
                estado=Vehiculo.Estado.RECHAZADO
            )
            if self.instance:
                vehiculos_activos = vehiculos_activos.exclude(
                    pk=self.instance.pk)

            num_estacionamientos = user.estacionamientos.count()
            if num_estacionamientos == 0:
                raise serializers.ValidationError(
                    "Tu unidad no tiene estacionamientos asignados — no puedes "
                    "registrar vehículos. Contacta al administrador si esto es un error."
                )
            limite = num_estacionamientos * MAX_PATENTES_POR_ESTACIONAMIENTO
            if vehiculos_activos.count() >= limite:
                raise serializers.ValidationError(
                    f"Ya alcanzaste el máximo de {limite} vehículo(s) registrado(s) "
                    f"o pendiente(s) ({MAX_PATENTES_POR_ESTACIONAMIENTO} por cada uno de "
                    f"tus {num_estacionamientos} estacionamiento(s))."
                )

        return attrs

    def create(self, validated_data):
        validated_data["propietario"] = self.context["request"].user
        return super().create(validated_data)


class VehiculoResolverSerializer(serializers.Serializer):
    aprobar = serializers.BooleanField(required=True)
    motivo_rechazo = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        if not attrs["aprobar"] and not attrs.get("motivo_rechazo", "").strip():
            raise serializers.ValidationError({
                "motivo_rechazo": "Debes indicar el motivo del rechazo."
            })
        return attrs


class DocumentoVerificacionSerializer(serializers.Serializer):
    tipo_documento = serializers.ChoiceField(choices=Visitante.TipoDocumento.choices)
    numero_documento = serializers.CharField(allow_blank=False, trim_whitespace=False)
    pais_documento = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=False, max_length=100
    )

    def validate(self, attrs):
        try:
            attrs["numero_documento"] = normalizar_documento(
                attrs["tipo_documento"], attrs["numero_documento"]
            )
        except DjangoValidationError as error:
            raise serializers.ValidationError(
                {"numero_documento": error.messages}
            ) from error
        attrs["pais_documento"] = " ".join(
            attrs.get("pais_documento", "").strip().split()
        )
        return attrs


class IngresoLogSerializer(serializers.ModelSerializer):
    # El valor mostrado se enmascara cuando es un RUT (dato personal de una
    # visita, no de un usuario del sistema) — la patente no se enmascara.
    valor_ingresado = serializers.SerializerMethodField()

    class Meta:
        model = IngresoLog
        fields = [
            "id",
            "tipo",
            "valor_ingresado",
            "resultado",
            "detalle",
            "guardia",
            "timestamp",
        ]
        read_only_fields = fields

    def get_valor_ingresado(self, obj):
        if obj.tipo == IngresoLog.Tipo.VISITA:
            return enmascarar_rut(obj.valor_ingresado)
        return obj.valor_ingresado


class PropietarioSerializer(serializers.ModelSerializer):
    """
    Uso exclusivo del admin para gestionar torre/departamento y nombre.
    Nunca expone contraseña, email, RUT, ni datos de sus visitas o
    vehículos. Los estacionamientos se listan (solo lectura, números)
    para que el admin vea de un vistazo cuántos le corresponden; se
    administran aparte vía /api/estacionamientos/.
    """
    estacionamientos = serializers.SlugRelatedField(
        slug_field="numero", many=True, read_only=True)

    class Meta:
        model = Usuario
        fields = ["id", "username", "first_name",
                  "last_name", "torre", "departamento", "estacionamientos"]
        read_only_fields = ["id", "username", "estacionamientos"]

    def validate(self, attrs):
        torre = attrs.get("torre", getattr(self.instance, "torre", None))
        departamento = attrs.get("departamento", getattr(
            self.instance, "departamento", None))
        if torre is None or departamento is None:
            raise serializers.ValidationError(
                "Un propietario debe tener torre y departamento asignados.")
        return attrs
