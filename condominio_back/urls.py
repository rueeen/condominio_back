from django.contrib import admin
from django.urls import include, path
from rest_framework_simplejwt.views import TokenRefreshView

from acceso.views import CondominioTokenObtainPairView, HealthCheckView

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/', include('acceso.urls')),
    path('api/health/', HealthCheckView.as_view(), name='health'),
    path('api/token/', CondominioTokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('api/token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
]
