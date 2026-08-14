import uuid

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from rest_framework import serializers
from rest_framework.validators import UniqueValidator
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from .models import (
    MAX_PATENTES_POR_ESTACIONAMIENTO,
    Estacionamiento,
    IngresoLog,
    Usuario,
    Vehiculo,
    Visitante,
    enmascarar_documento,
    normalizar_documento,
    normalizar_patente,
    validar_patente,
)


EMAIL_UNICO = UniqueValidator(
    queryset=Usuario.objects.all(),
    message="Ya existe una cuenta con ese correo",
)


class EmailOpcionalUnicoMixin(serializers.Serializer):
    """Normaliza el email opcional para que los valores vacíos se guarden como NULL."""

    email = serializers.EmailField(
        required=False,
        allow_blank=True,
        allow_null=True,
        validators=[EMAIL_UNICO],
    )

    def validate_email(self, value):
        return value or None


class CondominioTokenObtainPairSerializer(TokenObtainPairSerializer):
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token["rol"] = user.rol
        token["username"] = user.username
        token["unidad"] = user.unidad
        return token


class GuardiaSerializer(EmailOpcionalUnicoMixin, serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, min_length=8)

    class Meta:
        model = Usuario
        fields = ["id", "username", "first_name", "last_name", "email", "password"]

    def create(self, validated_data):
        password = validated_data.pop("password")
        usuario = Usuario(rol=Usuario.Rol.GUARDIA, **validated_data)
        usuario.set_password(password)
        usuario.save()
        return usuario


class EstacionamientoSerializer(serializers.ModelSerializer):
    propietario = serializers.PrimaryKeyRelatedField(
        queryset=Usuario.objects.filter(rol=Usuario.Rol.PROPIETARIO),
        allow_null=True,
        required=False,
    )

    class Meta:
        model = Estacionamiento
        fields = ["id", "numero", "propietario"]
        read_only_fields = ["id"]
        extra_kwargs = {
            "numero": {
                "validators": [
                    UniqueValidator(
                        queryset=Estacionamiento.objects.all(),
                        message="Ya existe un estacionamiento con este número.",
                    )
                ]
            }
        }


class AsignarEstacionamientoSerializer(serializers.Serializer):
    numero = serializers.CharField(max_length=10)
    propietario = serializers.PrimaryKeyRelatedField(
        queryset=Usuario.objects.filter(rol=Usuario.Rol.PROPIETARIO)
    )


class VisitanteSerializer(serializers.ModelSerializer):
    """Serializa visitas; envíe ``permanente: true`` para una visita sin vencimiento."""

    vigente = serializers.BooleanField(read_only=True)
    permanente = serializers.BooleanField(
        write_only=True, required=False, default=False
    )
    renovada = serializers.SerializerMethodField()

    class Meta:
        model = Visitante
        fields = [
            "id",
            "tipo_documento",
            "numero_documento",
            "pais_documento",
            "nombre",
            "token_qr",
            "propietario",
            "fecha_inicio",
            "fecha_fin",
            "creado_en",
            "vigente",
            "permanente",
            "renovada",
        ]
        read_only_fields = ["token_qr", "propietario", "creado_en", "vigente"]
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
        if self.instance:
            duplicados = Visitante.objects.filter(
                tipo_documento=tipo_documento,
                numero_documento=attrs["numero_documento"],
                propietario=propietario,
            ).exclude(pk=self.instance.pk)
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
        permanente = attrs.get(
            "permanente", self.instance.fecha_fin is None if self.instance else False
        )
        try:
            fecha_inicio, fecha_fin = Visitante.validar_vigencia(
                fecha_inicio, fecha_fin, permanente=permanente
            )
        except DjangoValidationError as error:
            raise serializers.ValidationError(error.message_dict) from error

        attrs["fecha_inicio"] = fecha_inicio
        attrs["fecha_fin"] = fecha_fin
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        permanente = validated_data.pop("permanente", False)
        propietario = self.context["request"].user
        candidato = (
            Visitante.objects.select_for_update()
            .filter(
                propietario=propietario,
                tipo_documento=validated_data["tipo_documento"],
                numero_documento=validated_data["numero_documento"],
            )
            .order_by("-fecha_inicio", "-pk")
            .first()
        )
        if candidato:
            # Se conserva la fila para que el historial de ingresos siga ligado a
            # la misma autorización, en vez de duplicar a una visita recurrente.
            for atributo in ("fecha_inicio", "fecha_fin", "nombre", "pais_documento"):
                if atributo in validated_data:
                    setattr(candidato, atributo, validated_data[atributo])
            candidato.token_qr = uuid.uuid4()
            candidato.save(permanente=permanente)
            candidato._renovada = True
            return candidato

        validated_data["propietario"] = propietario
        visitante = Visitante(**validated_data)
        visitante.save(permanente=permanente)
        visitante._renovada = False
        return visitante

    def get_renovada(self, obj):
        return getattr(obj, "_renovada", False)

    def update(self, instance, validated_data):
        permanente = validated_data.pop("permanente", instance.fecha_fin is None)
        for atributo, valor in validated_data.items():
            setattr(instance, atributo, valor)
        instance.save(permanente=permanente)
        return instance


class VehiculoSerializer(serializers.ModelSerializer):
    # Se declara explícitamente para normalizar antes de aplicar la validación
    # del modelo; así "AB-1234" y "AB1234" representan la misma patente.
    patente = serializers.CharField()
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

    def validate_patente(self, value):
        patente = normalizar_patente(value)
        validar_patente(patente)
        vehiculos_activos = Vehiculo.objects.filter(
            patente=patente,
            estado__in=[Vehiculo.Estado.PENDIENTE, Vehiculo.Estado.APROBADO],
        )
        if self.instance:
            vehiculos_activos = vehiculos_activos.exclude(pk=self.instance.pk)
        if vehiculos_activos.exists():
            raise serializers.ValidationError(
                "Ya existe una solicitud activa para esta patente."
            )
        return patente

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
    # Todo documento de una visita es un dato personal y se enmascara; la
    # patente identifica al vehículo y por decisión de negocio queda visible.
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
            return enmascarar_documento(obj.valor_ingresado)
        return obj.valor_ingresado


class PropietarioSerializer(EmailOpcionalUnicoMixin, serializers.ModelSerializer):
    """
    Uso exclusivo del admin para gestionar torre/departamento y nombre.
    Nunca expone contraseña, documentos de identidad, ni datos de sus visitas o
    vehículos. Los estacionamientos se listan (solo lectura, números)
    para que el admin vea de un vistazo cuántos le corresponden; se
    administran aparte vía /api/estacionamientos/.
    """
    estacionamientos = serializers.SlugRelatedField(
        slug_field="numero", many=True, read_only=True)

    class Meta:
        model = Usuario
        fields = ["id", "username", "first_name", "last_name", "email",
                  "telefono", "torre", "departamento", "estacionamientos"]
        read_only_fields = ["id", "username", "estacionamientos"]

    def validate(self, attrs):
        torre = attrs.get("torre", getattr(self.instance, "torre", None))
        departamento = attrs.get("departamento", getattr(
            self.instance, "departamento", None))
        if torre is None or departamento is None:
            raise serializers.ValidationError(
                "Un propietario debe tener torre y departamento asignados.")
        return attrs


class PerfilSerializer(EmailOpcionalUnicoMixin, serializers.ModelSerializer):
    unidad = serializers.CharField(read_only=True)

    class Meta:
        model = Usuario
        fields = [
            "username", "first_name", "last_name", "email", "telefono",
            "torre", "departamento", "unidad", "token_qr",
        ]
        read_only_fields = [
            "username", "first_name", "last_name", "torre", "departamento",
            "unidad", "token_qr",
        ]

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if instance.rol != Usuario.Rol.PROPIETARIO:
            data.pop("token_qr", None)
        return data


class PropietarioAltaSerializer(EmailOpcionalUnicoMixin, serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, min_length=8)

    class Meta:
        model = Usuario
        fields = [
            "id",
            "username",
            "first_name",
            "last_name",
            "email",
            "password",
            "torre",
            "departamento",
        ]

    def validate(self, attrs):
        torre = attrs.get("torre")
        departamento = attrs.get("departamento")
        if torre is None or departamento is None:
            raise serializers.ValidationError(
                "Un propietario debe tener torre y departamento asignados."
            )
        return attrs

    def create(self, validated_data):
        password = validated_data.pop("password")
        usuario = Usuario(rol=Usuario.Rol.PROPIETARIO, **validated_data)
        usuario.set_password(password)
        try:
            usuario.full_clean(exclude=["password"])
            usuario.save()
        except DjangoValidationError as error:
            detail = (
                error.message_dict
                if hasattr(error, "message_dict")
                else error.messages
            )
            raise serializers.ValidationError(detail) from error
        return usuario
