from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView

from .models import MAX_PATENTES_POR_ESTACIONAMIENTO, Estacionamiento, IngresoLog, Usuario, Vehiculo, Visitante
from .permissions import EsAdmin, EsGuardia, EsPropietario
from .serializers import (
    CondominioTokenObtainPairSerializer,
    EstacionamientoSerializer,
    IngresoLogSerializer,
    PropietarioSerializer,
    VehiculoResolverSerializer,
    VehiculoSerializer,
    VisitanteSerializer,
)


class CondominioTokenObtainPairView(TokenObtainPairView):
    serializer_class = CondominioTokenObtainPairSerializer


# ---------------------------------------------------------------------------
# Propietario: gestiona sus propias visitas
# ---------------------------------------------------------------------------
class VisitanteViewSet(viewsets.ModelViewSet):
    serializer_class = VisitanteSerializer
    permission_classes = [IsAuthenticated, EsPropietario]

    def get_queryset(self):
        # Cada propietario solo ve/edita sus propias visitas
        return Visitante.objects.filter(propietario=self.request.user)


# ---------------------------------------------------------------------------
# Propietario: solicita registro de patente / Admin: aprueba o rechaza
# ---------------------------------------------------------------------------
class VehiculoViewSet(viewsets.ModelViewSet):
    serializer_class = VehiculoSerializer
    permission_classes = [IsAuthenticated]
    # sin update directo (PATCH/PUT) — el estado solo cambia vía /resolver/;
    # "delete" sí se permite para que el propietario pueda eliminar/liberar
    # una patente propia (el queryset ya lo limita a las suyas).
    http_method_names = ["get", "post", "delete", "head"]

    def get_queryset(self):
        user = self.request.user
        queryset = Vehiculo.objects.select_related("propietario")
        if user.rol == "admin":
            return queryset
        return queryset.filter(propietario=user)

    def get_permissions(self):
        if self.action in ("create", "destroy"):
            return [IsAuthenticated(), EsPropietario()]
        return [IsAuthenticated()]

    @action(detail=True, methods=["post"], permission_classes=[IsAuthenticated, EsAdmin])
    def resolver(self, request, pk=None):
        """Endpoint para que el admin apruebe/rechace: POST /vehiculos/{id}/resolver/"""
        vehiculo = self.get_object()
        serializer = VehiculoResolverSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        if serializer.validated_data["aprobar"]:
            vehiculo.estado = Vehiculo.Estado.APROBADO
        else:
            vehiculo.estado = Vehiculo.Estado.RECHAZADO
            vehiculo.motivo_rechazo = serializer.validated_data.get(
                "motivo_rechazo", "")

        vehiculo.aprobado_por = request.user
        vehiculo.fecha_resolucion = timezone.now()
        vehiculo.save()
        return Response(VehiculoSerializer(vehiculo).data)


# ---------------------------------------------------------------------------
# Guardia: verificación de RUT (visitas)
# ---------------------------------------------------------------------------
class VerificarRutView(APIView):
    permission_classes = [IsAuthenticated, EsGuardia]

    def post(self, request):
        rut = request.data.get("rut", "").strip()
        if not rut:
            return Response({"detail": "Falta el RUT"}, status=status.HTTP_400_BAD_REQUEST)

        ahora = timezone.now()
        visita = Visitante.objects.filter(
            rut=rut, fecha_inicio__lte=ahora, fecha_fin__gte=ahora
        ).first()

        resultado = IngresoLog.Resultado.PERMITIDO if visita else IngresoLog.Resultado.DENEGADO
        IngresoLog.objects.create(
            tipo=IngresoLog.Tipo.VISITA,
            valor_ingresado=rut,
            resultado=resultado,
            guardia=request.user,
            detalle=f"Visita a unidad {visita.propietario.unidad}" if visita else "Sin autorización vigente",
        )

        if visita:
            return Response({
                "permitido": True,
                "nombre": visita.nombre,
                "unidad": visita.propietario.unidad,
                "vigente_hasta": visita.fecha_fin,
            })
        return Response({"permitido": False, "detalle": "No hay autorización vigente para este RUT"},
                        status=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Guardia: verificación de patente (vehículos)
# ---------------------------------------------------------------------------
class VerificarPatenteView(APIView):
    """
    Recibe la patente ya extraída (por el paso de OCR, ver ocr.py) y valida
    contra la BD. Este endpoint NO hace OCR, solo verifica el texto.
    Para leer la patente desde una foto, primero se llama a /ocr/leer-patente/.
    """
    permission_classes = [IsAuthenticated, EsGuardia]

    def post(self, request):
        patente = request.data.get("patente", "").strip().upper()
        if not patente:
            return Response({"detail": "Falta la patente"}, status=status.HTTP_400_BAD_REQUEST)

        vehiculo = Vehiculo.objects.filter(
            patente=patente, estado=Vehiculo.Estado.APROBADO
        ).first()

        resultado = IngresoLog.Resultado.PERMITIDO if vehiculo else IngresoLog.Resultado.DENEGADO
        IngresoLog.objects.create(
            tipo=IngresoLog.Tipo.VEHICULO,
            valor_ingresado=patente,
            resultado=resultado,
            guardia=request.user,
            detalle=f"Unidad {vehiculo.propietario.unidad}" if vehiculo else "Patente no registrada/aprobada",
        )

        if vehiculo:
            return Response({
                "permitido": True,
                "unidad": vehiculo.propietario.unidad,
            })
        return Response({"permitido": False, "detalle": "Patente no registrada o no aprobada"},
                        status=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Admin: historial de ingresos (solo lectura)
# ---------------------------------------------------------------------------
class IngresoLogViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = IngresoLogSerializer
    permission_classes = [IsAuthenticated, EsAdmin]
    queryset = IngresoLog.objects.all()


# ---------------------------------------------------------------------------
# Admin: gestión de torre/departamento de los propietarios
# ---------------------------------------------------------------------------
class PropietarioViewSet(viewsets.ModelViewSet):
    """
    Solo lista y edita torre/departamento — no crea ni elimina cuentas
    (eso se sigue haciendo desde /admin/ de Django, donde también se
    define usuario/contraseña). No expone RUT, email ni datos de las
    visitas o vehículos del propietario.
    """
    serializer_class = PropietarioSerializer
    permission_classes = [IsAuthenticated, EsAdmin]
    http_method_names = ["get", "patch", "head"]
    queryset = Usuario.objects.filter(
        rol=Usuario.Rol.PROPIETARIO).prefetch_related("estacionamientos").order_by("torre", "departamento")


# ---------------------------------------------------------------------------
# Admin: gestión de estacionamientos (uno o más por propietario)
# ---------------------------------------------------------------------------
class EstacionamientoViewSet(viewsets.ModelViewSet):
    """
    CRUD completo, solo admin. La cantidad de estacionamientos de un
    propietario determina cuántas patentes puede registrar
    (ver VehiculoSerializer.validate).
    """
    serializer_class = EstacionamientoSerializer
    permission_classes = [IsAuthenticated, EsAdmin]
    queryset = Estacionamiento.objects.all()


# ---------------------------------------------------------------------------
# Propietario: consulta (solo lectura) de sus propios estacionamientos
# ---------------------------------------------------------------------------
class MisEstacionamientosView(APIView):
    permission_classes = [IsAuthenticated, EsPropietario]

    def get(self, request):
        numeros = list(
            request.user.estacionamientos.values_list("numero", flat=True))
        return Response({
            "estacionamientos": numeros,
            "limite_patentes": len(numeros) * MAX_PATENTES_POR_ESTACIONAMIENTO,
        })
