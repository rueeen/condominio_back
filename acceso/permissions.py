from rest_framework.permissions import BasePermission


class EsPropietario(BasePermission):
    def has_permission(self, request, view):
        return bool(request.user and request.user.rol == "propietario")


class EsGuardia(BasePermission):
    def has_permission(self, request, view):
        return bool(request.user and request.user.rol == "guardia")


class EsAdmin(BasePermission):
    def has_permission(self, request, view):
        return bool(request.user and request.user.rol == "admin")


class EsPropietarioOAdmin(BasePermission):
    def has_permission(self, request, view):
        return bool(request.user and request.user.rol in ("propietario", "admin"))
