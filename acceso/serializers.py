from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from .models import IngresoLog, Usuario, Vehiculo, Visitante, enmascarar_rut


class CondominioTokenObtainPairSerializer(TokenObtainPairSerializer):
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token["rol"] = user.rol
        token["username"] = user.username
        token["unidad"] = user.unidad
        return token


class VisitanteSerializer(serializers.ModelSerializer):
    vigente = serializers.BooleanField(read_only=True)

    class Meta:
        model = Visitante
        fields = [
            "id",
            "rut",
            "nombre",
            "propietario",
            "fecha_inicio",
            "fecha_fin",
            "creado_en",
            "vigente",
        ]
        read_only_fields = ["propietario", "creado_en", "vigente"]

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

            if vehiculos_activos.count() >= 2:
                raise serializers.ValidationError(
                    "Ya alcanzaste el máximo de 2 vehículos registrados o pendientes."
                )

        return attrs

    def create(self, validated_data):
        validated_data["propietario"] = self.context["request"].user
        return super().create(validated_data)


class VehiculoResolverSerializer(serializers.Serializer):
    aprobar = serializers.BooleanField(required=True)
    motivo_rechazo = serializers.CharField(required=False, allow_blank=True)


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
    Uso exclusivo del admin para gestionar torre/departamento. Expone lo
    mínimo indispensable — nunca contraseña, email, ni datos de sus
    visitas o vehículos.
    """
    class Meta:
        model = Usuario
        fields = ["id", "username", "first_name",
                  "last_name", "torre", "departamento"]
        read_only_fields = ["id", "username", "first_name", "last_name"]

    def validate(self, attrs):
        torre = attrs.get("torre", getattr(self.instance, "torre", None))
        departamento = attrs.get("departamento", getattr(
            self.instance, "departamento", None))
        if torre is None or departamento is None:
            raise serializers.ValidationError(
                "Un propietario debe tener torre y departamento asignados.")
        return attrs
