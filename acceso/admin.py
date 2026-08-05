from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import IngresoLog, Usuario, Vehiculo, Visitante


@admin.register(Usuario)
class UsuarioAdmin(UserAdmin):
    list_display = ('username', 'email', 'first_name', 'last_name', 'rol', 'unidad', 'is_staff')
    list_filter = (*UserAdmin.list_filter, 'rol')
    fieldsets = UserAdmin.fieldsets + (
        ('Condominio', {'fields': ('rol', 'unidad')}),
    )
    add_fieldsets = UserAdmin.add_fieldsets + (
        ('Condominio', {'fields': ('rol', 'unidad')}),
    )


@admin.register(Visitante)
class VisitanteAdmin(admin.ModelAdmin):
    list_display = ('rut', 'nombre', 'propietario', 'fecha_inicio', 'fecha_fin', 'creado_en')
    list_filter = ('fecha_inicio', 'fecha_fin', 'creado_en')
    search_fields = ('rut', 'nombre', 'propietario__username', 'propietario__unidad')


@admin.register(Vehiculo)
class VehiculoAdmin(admin.ModelAdmin):
    list_display = ('patente', 'propietario', 'estado', 'aprobado_por', 'fecha_solicitud', 'fecha_resolucion')
    list_filter = ('estado', 'fecha_solicitud', 'fecha_resolucion')
    search_fields = ('patente', 'propietario__username', 'propietario__unidad')


@admin.register(IngresoLog)
class IngresoLogAdmin(admin.ModelAdmin):
    list_display = ('tipo', 'valor_ingresado', 'resultado', 'guardia', 'timestamp')
    list_filter = ('tipo', 'resultado', 'timestamp')
    search_fields = ('valor_ingresado', 'detalle', 'guardia__username')
