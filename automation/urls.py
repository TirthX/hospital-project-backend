from django.urls import path

from .views import AlertEngineRunView, ThresholdMonitorView

urlpatterns = [
    path('run/', AlertEngineRunView.as_view(), name='automation-run'),
    path('thresholds/', ThresholdMonitorView.as_view(), name='automation-thresholds'),
]
