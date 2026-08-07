from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .ocr import DetectarPatenteView, LeerPatenteView
from .views import (
    IngresoLogViewSet,
    VehiculoViewSet,
    VerificarPatenteView,
    VerificarRutView,
    VisitanteViewSet,
)

router = DefaultRouter()
router.register("visitantes", VisitanteViewSet, basename="visitante")
router.register("vehiculos", VehiculoViewSet, basename="vehiculo")
router.register("ingresos", IngresoLogViewSet, basename="ingreso")

urlpatterns = [
    path("", include(router.urls)),

    # Guardia
    path("guardia/verificar-rut/", VerificarRutView.as_view(), name="verificar-rut"),
    path("guardia/verificar-patente/",
         VerificarPatenteView.as_view(), name="verificar-patente"),
    path("ocr/leer-patente/", LeerPatenteView.as_view(), name="leer-patente"),
    path("ocr/detectar-patente/", DetectarPatenteView.as_view(),
         name="detectar-patente"),
]

# Rutas resultantes, resumen:
# GET/POST   /visitantes/                 (propietario)
# GET/PATCH  /visitantes/{id}/            (propietario)
# GET/POST   /vehiculos/                  (propietario crea, admin lista todo)
# POST       /vehiculos/{id}/resolver/    (admin aprueba/rechaza)
# GET        /ingresos/                   (admin, historial/auditoría)
# POST       /guardia/verificar-rut/      (guardia)
# POST       /guardia/verificar-patente/  (guardia)
# POST       /ocr/leer-patente/           (guardia, sube foto -> lee y devuelve la patente)
# POST       /ocr/detectar-patente/       (guardia, polling liviano -> solo detecta si hay
#                                           una región candidata, sin OCR)
