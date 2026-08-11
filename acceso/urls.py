from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .ocr import LeerPatenteView
from .views import (
    EstacionamientoViewSet,
    GuardiaViewSet,
    IngresoLogViewSet,
    MisEstacionamientosView,
    PerfilView,
    PropietarioViewSet,
    RegenerarQrPerfilView,
    VehiculoViewSet,
    VerificarDocumentoView,
    VerificarPatenteView,
    VerificarQrView,
    VisitanteViewSet,
)

router = DefaultRouter()
router.register("visitantes", VisitanteViewSet, basename="visitante")
router.register("vehiculos", VehiculoViewSet, basename="vehiculo")
router.register("ingresos", IngresoLogViewSet, basename="ingreso")
router.register("propietarios", PropietarioViewSet, basename="propietario")
router.register("estacionamientos", EstacionamientoViewSet,
                basename="estacionamiento")
router.register("guardias", GuardiaViewSet, basename="guardia")

urlpatterns = [
    path("", include(router.urls)),

    path("mis-estacionamientos/", MisEstacionamientosView.as_view(),
         name="mis-estacionamientos"),
    path("perfil/", PerfilView.as_view(), name="perfil"),
    path("perfil/regenerar-qr/", RegenerarQrPerfilView.as_view(),
         name="regenerar-qr-perfil"),

    # Guardia
    path("guardia/verificar-documento/", VerificarDocumentoView.as_view(),
         name="verificar-documento"),
    # Alias temporal para clientes que todavía usan el nombre histórico.
    path("guardia/verificar-rut/", VerificarDocumentoView.as_view(), name="verificar-rut"),
    path("guardia/verificar-qr/", VerificarQrView.as_view(), name="verificar-qr"),
    path("guardia/verificar-patente/",
         VerificarPatenteView.as_view(), name="verificar-patente"),
    path("ocr/leer-patente/", LeerPatenteView.as_view(), name="leer-patente"),
]

# Rutas resultantes, resumen:
# GET/POST   /visitantes/                 (propietario)
# GET/PATCH  /visitantes/{id}/            (propietario)
# GET/POST   /vehiculos/                  (propietario crea, admin lista todo)
# POST       /vehiculos/{id}/resolver/    (admin aprueba/rechaza)
# GET        /ingresos/                   (admin, historial/auditoría — documentos protegidos)
# GET/POST   /propietarios/               (admin, lista y crea propietarios)
# PATCH      /propietarios/{id}/          (admin, edita torre/departamento)
# GET/POST   /estacionamientos/           (admin, CRUD completo)
# GET/PATCH/DELETE /estacionamientos/{id}/ (admin)
# GET        /mis-estacionamientos/       (propietario, solo lectura: sus propios números)
# GET/PATCH  /perfil/                      (usuario autenticado, perfil propio)
# POST       /perfil/regenerar-qr/         (propietario, revoca y renueva su QR)
# POST       /guardia/verificar-documento/ (guardia)
# POST       /guardia/verificar-rut/       (guardia, alias compatible temporal)
# POST       /guardia/verificar-qr/       (guardia)
# POST       /guardia/verificar-patente/  (guardia)
# POST       /ocr/leer-patente/           (guardia, sube foto -> lee y devuelve la patente)
