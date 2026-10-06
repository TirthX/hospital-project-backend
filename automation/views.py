"""
Automation engine API.

  POST /api/automation/run/         evaluate all eight alert rules now
  GET  /api/automation/thresholds/  live value of every monitored metric
"""
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .engine import run_alert_engine, threshold_summary


class AlertEngineRunView(APIView):
    """Run the alert engine on demand (it also runs after each sim tick)."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        return Response(run_alert_engine())


class ThresholdMonitorView(APIView):
    """Every monitored metric with its current value and threshold."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({'thresholds': threshold_summary()})
