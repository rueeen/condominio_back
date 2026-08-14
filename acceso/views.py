import uuid

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView

from .models import (
    MAX_PATENTES_POR_ESTACIONAMIENTO,
    Estacionamiento,
    IngresoLog,
    Usuario,
    Vehiculo,
    Visitante,
    normalizar_patente,
    validar_patente,
)
from .permissions import EsAdmin, EsGuardia, EsPropietario
from .serializers import (
    AsignarEstacionamientoSerializer,
    CondominioTokenObtainPairSerializer,
    DocumentoVerificacionSerializer,
    EstacionamientoSerializer,
    GuardiaSerializer,
    IngresoLogSerializer,
    PerfilSerializer,
    PropietarioAltaSerializer,
    PropietarioSerializer,
    VehiculoResolverSerializer,
    VehiculoSerializer,
    VisitanteSerializer,
)


class CondominioTokenObtainPairView(TokenObtainPairView):
    serializer_class = CondominioTokenObtainPairSerializer


class HealthCheckView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        return Response({"status": "ok"})


class PerfilView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(PerfilSerializer(request.user).data)

    def patch(self, request):
        serializer = PerfilSerializer(request.user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class RegenerarQrPerfilView(APIView):
    permission_classes = [IsAuthenticated, EsPropietario]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "regenerar_qr"

    def post(self, request):
        request.user.token_qr = uuid.uuid4()
        request.user.save(update_fields=["token_qr"])
        return Response({"token_qr": str(request.user.token_qr)})


class GuardiaViewSet(viewsets.ModelViewSet):
    """
    Solo admin. Crea y lista cuentas de guardia — no permite editar ni
    borrar por ahora (para eso sigue estando /admin/ de Django).
    """
    serializer_class = GuardiaSerializer
    permission_classes = [IsAuthenticated, EsAdmin]
    http_method_names = ["get", "post", "head"]
    queryset = Usuario.objects.filter(
        rol=Usuario.Rol.GUARDIA
    ).order_by("username")


# ---------------------------------------------------------------------------
# Propietario: gestiona sus propias visitas
# ---------------------------------------------------------------------------
class VisitanteViewSet(viewsets.ModelViewSet):
    serializer_class = VisitanteSerializer
    permission_classes = [IsAuthenticated, EsPropietario]

    def get_queryset(self):
        # Cada propietario solo ve/edita sus propias visitas
        return Visitante.objects.filter(propietario=self.request.user).order_by("-creado_en")

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        headers = self.get_success_headers(serializer.data)
        codigo = status.HTTP_200_OK if serializer.data["renovada"] else status.HTTP_201_CREATED
        return Response(serializer.data, status=codigo, headers=headers)


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
        queryset = Vehiculo.objects.select_related(
            "propietario").order_by("-fecha_solicitud")
        if user.rol != "admin":
            queryset = queryset.filter(propietario=user)

        estado = self.request.query_params.get("estado")
        if estado in dict(Vehiculo.Estado.choices):
            queryset = queryset.filter(estado=estado)
        return queryset

    def get_permissions(self):
        if self.action in ("create", "destroy"):
            return [IsAuthenticated(), EsPropietario()]
        return [IsAuthenticated()]

    def perform_create(self, serializer):
        # La fila del propietario funciona como el lock común para altas,
        # resoluciones y cambios de estacionamientos de esa unidad.
        with transaction.atomic():
            propietario = Usuario.objects.select_for_update().get(
                pk=self.request.user.pk
            )
            estacionamientos = Estacionamiento.objects.filter(
                propietario=propietario
            ).count()
            activos = Vehiculo.objects.filter(
                propietario=propietario,
                estado__in=[Vehiculo.Estado.PENDIENTE,
                            Vehiculo.Estado.APROBADO],
            ).count()
            limite = estacionamientos * MAX_PATENTES_POR_ESTACIONAMIENTO
            if activos >= limite:
                raise ValidationError(
                    {"detail": f"La unidad alcanzó el límite de {limite} patentes activas."}
                )
            serializer.save(propietario=propietario)

    @action(detail=True, methods=["post"], permission_classes=[IsAuthenticated, EsAdmin])
    def resolver(self, request, pk=None):
        """Endpoint para que el admin apruebe/rechace: POST /vehiculos/{id}/resolver/"""
        serializer = VehiculoResolverSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            vehiculo = Vehiculo.objects.select_for_update().get(pk=pk)
            propietario = Usuario.objects.select_for_update().get(
                pk=vehiculo.propietario_id
            )
            if vehiculo.estado != Vehiculo.Estado.PENDIENTE:
                raise ValidationError(
                    {"detail": "Solo se puede resolver un vehículo pendiente."}
                )

            if serializer.validated_data["aprobar"]:
                activos = Vehiculo.objects.filter(
                    propietario=propietario,
                    estado__in=[Vehiculo.Estado.PENDIENTE,
                                Vehiculo.Estado.APROBADO],
                ).count()
                limite = propietario.estacionamientos.count() * MAX_PATENTES_POR_ESTACIONAMIENTO
                if activos > limite:
                    raise ValidationError(
                        {"detail": f"La aprobación excedería el límite de {limite} patentes activas."}
                    )
                vehiculo.estado = Vehiculo.Estado.APROBADO
                vehiculo.motivo_rechazo = ""
            else:
                vehiculo.estado = Vehiculo.Estado.RECHAZADO
                vehiculo.motivo_rechazo = serializer.validated_data["motivo_rechazo"].strip(
                )

            vehiculo.aprobado_por = request.user
            vehiculo.fecha_resolucion = timezone.now()
            vehiculo.save()
        return Response(VehiculoSerializer(vehiculo).data)


# ---------------------------------------------------------------------------
# Guardia: verificación de documentos (visitas)
# ---------------------------------------------------------------------------
class VerificarDocumentoView(APIView):
    permission_classes = [IsAuthenticated, EsGuardia]

    def post(self, request):
        serializer = DocumentoVerificacionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        tipo_documento = serializer.validated_data["tipo_documento"]
        numero_documento = serializer.validated_data["numero_documento"]
        pais_documento = serializer.validated_data["pais_documento"]

        ahora = timezone.now()
        visitas = Visitante.objects.select_related("propietario").filter(
            tipo_documento=tipo_documento,
            numero_documento=numero_documento,
            fecha_inicio__lte=ahora,
        ).filter(Q(fecha_fin__isnull=True) | Q(fecha_fin__gte=ahora))
        if pais_documento:
            visitas = visitas.filter(pais_documento__iexact=pais_documento)
        visitas = list(visitas.order_by("fecha_fin", "pk"))

        resultado = IngresoLog.Resultado.PERMITIDO if visitas else IngresoLog.Resultado.DENEGADO
        IngresoLog.objects.create(
            tipo=IngresoLog.Tipo.VISITA,
            valor_ingresado=numero_documento,
            resultado=resultado,
            guardia=request.user,
            detalle=(
                f"{len(visitas)} autorización(es) vigente(s)"
                if visitas else "Sin autorización vigente"
            ),
        )

        if len(visitas) == 1:
            visita = visitas[0]
            return Response({
                "permitido": True,
                "id_autorizacion": visita.pk,
                "nombre": visita.nombre,
                "unidad": visita.propietario.unidad,
                "fecha_fin": visita.fecha_fin,
            })
        if visitas:
            return Response({
                "permitido": True,
                "requiere_seleccion": True,
                "autorizaciones": [
                    {
                        "id_autorizacion": visita.pk,
                        "nombre": visita.nombre,
                        "unidad": visita.propietario.unidad,
                        "vigencia": visita.fecha_fin,
                    }
                    for visita in visitas
                ],
            })
        return Response({"permitido": False, "detalle": "No hay autorización vigente para este documento"},
                        status=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Guardia: verificación del token opaco de una autorización de visita
# ---------------------------------------------------------------------------
class VerificarQrView(APIView):
    permission_classes = [IsAuthenticated, EsGuardia]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "guardia_qr"

    @staticmethod
    def _registrar(request, visita, resultado, detalle):
        IngresoLog.objects.create(
            tipo=IngresoLog.Tipo.VISITA,
            # Un intento sin autorización identificable no tiene documento
            # que registrar; el token nunca se persiste en el historial.
            valor_ingresado=visita.numero_documento if visita else "",
            resultado=resultado,
            guardia=request.user,
            detalle=detalle,
        )

    @staticmethod
    def _registrar_residente(request, residente):
        IngresoLog.objects.create(
            tipo=IngresoLog.Tipo.RESIDENTE,
            valor_ingresado=residente.unidad or residente.username,
            resultado=IngresoLog.Resultado.PERMITIDO,
            guardia=request.user,
            detalle="Residente verificado vía QR propio",
        )

    def post(self, request):
        try:
            token = uuid.UUID(str(request.data.get("token", "")))
        except (AttributeError, TypeError, ValueError):
            self._registrar(
                request, None, IngresoLog.Resultado.DENEGADO,
                "Token QR inválido",
            )
            return Response(
                {"permitido": False, "detalle": "Token QR inválido"},
                status=status.HTTP_200_OK,
            )

        ahora = timezone.now()
        visita = Visitante.objects.select_related("propietario").filter(
            token_qr=token
        ).first()
        visita_vigente = visita and Visitante.objects.filter(
            pk=visita.pk,
            fecha_inicio__lte=ahora,
        ).filter(Q(fecha_fin__isnull=True) | Q(fecha_fin__gte=ahora)).exists()

        if not visita_vigente and visita is None:
            residente = Usuario.objects.filter(
                token_qr=token, rol=Usuario.Rol.PROPIETARIO
            ).first()
            if residente:
                self._registrar_residente(request, residente)
                return Response({
                    "permitido": True,
                    "tipo": "residente",
                    "nombre": residente.get_full_name() or residente.username,
                    "unidad": residente.unidad,
                })

        if not visita_vigente:
            self._registrar(
                request, visita, IngresoLog.Resultado.DENEGADO,
                "Sin autorización vigente vía QR",
            )
            return Response(
                {"permitido": False, "detalle": "No hay autorización vigente para este código QR"},
                status=status.HTTP_200_OK,
            )

        self._registrar(
            request, visita, IngresoLog.Resultado.PERMITIDO,
            "Autorización vigente verificada vía QR",
        )
        return Response({
            "permitido": True,
            "tipo": "visita",
            "id_autorizacion": visita.pk,
            "nombre": visita.nombre,
            "unidad": visita.propietario.unidad,
            "fecha_fin": visita.fecha_fin,
        })


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
        patente = normalizar_patente(request.data.get("patente", ""))
        if not patente:
            return Response({"detail": "Falta la patente"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            validar_patente(patente)
        except DjangoValidationError as error:
            raise ValidationError({"patente": error.messages}) from error

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
    Solo admin. Crea, lista y edita cuentas de propietario. No expone RUT
    ni datos de las visitas o vehículos del propietario.
    """
    permission_classes = [IsAuthenticated, EsAdmin]
    http_method_names = ["get", "post", "patch", "head"]
    queryset = Usuario.objects.filter(
        rol=Usuario.Rol.PROPIETARIO).prefetch_related("estacionamientos").order_by("torre", "departamento")

    def get_queryset(self):
        queryset = super().get_queryset()
        buscar = self.request.query_params.get("buscar")
        if buscar:
            queryset = queryset.filter(
                Q(username__icontains=buscar)
                | Q(first_name__icontains=buscar)
                | Q(last_name__icontains=buscar)
                | Q(torre__icontains=buscar)
                | Q(departamento__icontains=buscar)
            )
        torre = self.request.query_params.get("torre")
        if torre:
            queryset = queryset.filter(torre=torre)
        if self.request.query_params.get("sin_estacionamiento", "").lower() == "true":
            queryset = queryset.filter(estacionamientos__isnull=True)
        return queryset

    def get_serializer_class(self):
        if self.action == "create":
            return PropietarioAltaSerializer
        return PropietarioSerializer


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
    queryset = Estacionamiento.objects.select_related("propietario").all()

    def get_queryset(self):
        queryset = super().get_queryset()
        asignado = self.request.query_params.get("asignado", "").lower()
        if asignado == "true":
            queryset = queryset.filter(propietario__isnull=False)
        elif asignado == "false":
            queryset = queryset.filter(propietario__isnull=True)
        propietario = self.request.query_params.get("propietario")
        if propietario:
            queryset = queryset.filter(propietario_id=propietario)
        buscar = self.request.query_params.get("buscar")
        if buscar:
            queryset = queryset.filter(numero__icontains=buscar)
        return queryset

    @action(detail=False, methods=["get"])
    def resumen(self, request):
        cantidades = Estacionamiento.objects.aggregate(
            total=Count("id"),
            asignados=Count("id", filter=Q(propietario__isnull=False)),
            libres=Count("id", filter=Q(propietario__isnull=True)),
        )
        cantidades["propietarios_sin_estacionamiento"] = Usuario.objects.filter(
            rol=Usuario.Rol.PROPIETARIO,
            estacionamientos__isnull=True,
        ).count()
        return Response(cantidades)

    @action(detail=False, methods=["post"])
    def asignar(self, request):
        serializer = AsignarEstacionamientoSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        numero = serializer.validated_data["numero"]
        propietario = serializer.validated_data["propietario"]

        with transaction.atomic():
            self._lock_propietarios(propietario.pk)
            estacionamiento = Estacionamiento.objects.select_for_update().filter(
                numero=numero
            ).first()
            if estacionamiento is None:
                estacionamiento = Estacionamiento.objects.create(
                    numero=numero, propietario=propietario
                )
                codigo = status.HTTP_201_CREATED
            elif estacionamiento.propietario_id is None:
                estacionamiento.propietario = propietario
                estacionamiento.save(update_fields=["propietario"])
                codigo = status.HTTP_200_OK
            elif estacionamiento.propietario_id == propietario.pk:
                codigo = status.HTTP_200_OK
            else:
                actual = estacionamiento.propietario
                return Response(
                    {
                        "detalle": "El estacionamiento ya está asignado a otro propietario.",
                        "propietario_actual": {
                            "id": actual.pk,
                            "torre": actual.torre,
                            "departamento": actual.departamento,
                        },
                    },
                    status=status.HTTP_409_CONFLICT,
                )
        return Response(EstacionamientoSerializer(estacionamiento).data, status=codigo)

    @staticmethod
    def _lock_propietarios(*propietario_ids):
        # Un estacionamiento libre no tiene una fila de propietario que bloquear.
        ids = sorted({pk for pk in propietario_ids if pk is not None})
        return {
            propietario.pk: propietario
            for propietario in Usuario.objects.select_for_update().filter(pk__in=ids)
        }

    def perform_update(self, serializer):
        with transaction.atomic():
            estacionamiento = Estacionamiento.objects.select_for_update().get(
                pk=serializer.instance.pk
            )
            anterior_id = estacionamiento.propietario_id
            nuevo = serializer.validated_data.get(
                "propietario", estacionamiento.propietario
            )
            nuevo_id = nuevo.pk if nuevo else None
            self._lock_propietarios(anterior_id, nuevo_id)
            # Una desvinculación no elimina ni rechaza patentes existentes. Si el
            # propietario queda sobre el nuevo límite, VehiculoSerializer y
            # perform_create impiden nuevas solicitudes hasta recuperar cupo.
            serializer.instance = estacionamiento
            serializer.save()

    def perform_destroy(self, instance):
        with transaction.atomic():
            estacionamiento = Estacionamiento.objects.select_for_update().get(pk=instance.pk)
            self._lock_propietarios(estacionamiento.propietario_id)
            estacionamiento.delete()


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
