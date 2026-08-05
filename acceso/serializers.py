from rest_framework import serializers

from .models import IngresoLog, Vehiculo, Visitante


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
    class Meta:
        model = Vehiculo
        fields = [
            "id",
            "patente",
            "propietario",
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
                vehiculos_activos = vehiculos_activos.exclude(pk=self.instance.pk)

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
