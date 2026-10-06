from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.http import HttpResponse, JsonResponse
from django.urls import include, path

def root_status_view(request):
    if request.method == 'HEAD':
        return HttpResponse(status=200)
    return JsonResponse({
        'status': 'healthy',
        'service': 'Hospital Intelligence Platform Backend',
        'api_root': '/api/',
        'admin_root': '/admin/',
        'version': '1.0.0',
    })

urlpatterns = [
    path('', root_status_view, name='root_status'),
    path('admin/', admin.site.urls),
    path('api/', include('core.urls')),
    path('api/ai/', include('ai_engine.urls')),
    path('api/automation/', include('automation.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
