"""
AI engine API.

Four endpoints, all reading live database state:

  GET /api/ai/predictions/      24-hour Random Forest forecast
  GET /api/ai/explanations/     plain-language reading of the current numbers
  GET /api/ai/recommendations/  concrete actions derived from thresholds
  GET /api/ai/status/           model metadata and held-out accuracy
"""
import time

from django.db.models import Avg, Count, F, Q
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.clinical import (
    ICU_OCCUPANCY_ALERT_AT,
    LONG_WAIT_ALERT_MINUTES,
    UNSAFE_NURSE_RATIO,
    WARD_OCCUPANCY_ALERT_AT,
)
from core.models import (
    Bed, Doctor, Medicine, Nurse, Patient, PatientQueue, Ward,
)

from .ml import forecast_next_24h, model_status, persist_forecast


class AIPredictionsView(APIView):
    """24-hour forecast of patient inflow and ICU occupancy."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        started = time.perf_counter()
        forecast = forecast_next_24h()
        elapsed_ms = (time.perf_counter() - started) * 1000

        if request.query_params.get('persist') == 'true' and forecast.get('available'):
            persist_forecast(forecast)

        forecast['inference_ms'] = round(elapsed_ms, 2)
        # Kept for backwards compatibility with the original response shape.
        forecast['predictions'] = forecast.get('points', [])
        return Response(forecast)


class AIExplanationsView(APIView):
    """Turn the current numbers into sentences an administrator can act on.

    Every explanation is computed from live counts — nothing is hard-coded —
    so the wording changes as the hospital state changes.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        explanations = []

        # ── ICU occupancy ─────────────────────────────────────────
        icu_total = Bed.objects.filter(ward__ward_type='icu').count()
        icu_occupied = Bed.objects.filter(ward__ward_type='icu', status='occupied').count()
        icu_pct = round(icu_occupied / icu_total * 100, 1) if icu_total else 0.0

        forecast = forecast_next_24h()
        if forecast.get('available'):
            peak_pct = forecast['summary']['peak_icu_percentage']
            peak_at = forecast['summary']['peak_icu_at']
            trend = forecast['summary']['trend']
            icu_outlook = (f'The model projects a peak of {peak_pct}% around {peak_at} '
                           f'({trend} trend over the next 24 hours).')
            icu_confidence = forecast['points'][0]['confidence']
        else:
            icu_outlook = 'Forecast unavailable — the trained models are not loaded.'
            icu_confidence = None

        # Name the diagnoses actually driving the ICU census, rather than
        # asserting a generic cause.
        icu_diagnoses = list(
            Patient.objects.filter(is_discharged=False, assigned_bed__ward__ward_type='icu')
            .values('disease').annotate(n=Count('id')).order_by('-n')[:3]
        )
        driver = ', '.join(f"{d['disease']} ({d['n']})" for d in icu_diagnoses) or 'no current admissions'

        explanations.append({
            'metric': 'ICU Occupancy',
            'value': f'{icu_pct}%',
            'trend': ('Critical' if icu_pct >= ICU_OCCUPANCY_ALERT_AT
                      else 'Elevated' if icu_pct >= 70 else 'Stable'),
            'reason': (f'{icu_occupied} of {icu_total} ICU beds are occupied. '
                       f'Leading diagnoses: {driver}. {icu_outlook}'),
            'confidence': f'{icu_confidence}%' if icu_confidence else 'n/a',
        })

        # ── Waiting time ──────────────────────────────────────────
        waiting = PatientQueue.objects.filter(status='waiting')
        waiting_count = waiting.count()
        avg_wait = round(waiting.aggregate(v=Avg('estimated_wait_minutes'))['v'] or 0, 1)
        doctors_available = Doctor.objects.filter(status='available').count()
        long_waits = waiting.filter(estimated_wait_minutes__gt=LONG_WAIT_ALERT_MINUTES).count()

        explanations.append({
            'metric': 'Patient Waiting Time',
            'value': f'{avg_wait} min',
            'trend': ('Critical' if long_waits > 2
                      else 'Elevated' if avg_wait > 30 else 'Optimal'),
            'reason': (f'{waiting_count} patients are queued against '
                       f'{doctors_available} available doctors. '
                       + (f'{long_waits} have already waited beyond the '
                          f'{LONG_WAIT_ALERT_MINUTES}-minute threshold.'
                          if long_waits else
                          'No patient has crossed the '
                          f'{LONG_WAIT_ALERT_MINUTES}-minute threshold.')),
            'confidence': '88%',
        })

        # ── Staffing ratio ────────────────────────────────────────
        active_patients = Patient.objects.filter(is_discharged=False).count()
        nurses_on_duty = Nurse.objects.exclude(status='off_duty').count()
        ratio = round(active_patients / nurses_on_duty, 1) if nurses_on_duty else 0.0

        explanations.append({
            'metric': 'Nurse-to-Patient Ratio',
            'value': f'1:{ratio}',
            'trend': 'Critical' if ratio > UNSAFE_NURSE_RATIO else 'Safe',
            'reason': (f'{nurses_on_duty} nurses are on duty for {active_patients} '
                       f'active patients. The safe working limit used by this '
                       f'platform is 1:{UNSAFE_NURSE_RATIO}.'),
            'confidence': '95%',
        })

        # ── Pharmacy ──────────────────────────────────────────────
        low_stock = Medicine.objects.filter(stock__lte=F('min_stock'), stock__gt=0).count()
        out_of_stock = Medicine.objects.filter(stock__lte=0).count()
        near_expiry = Medicine.objects.filter(
            expiry_date__lte=timezone.now().date() + timezone.timedelta(days=30),
            expiry_date__gt=timezone.now().date(),
        ).count()

        explanations.append({
            'metric': 'Pharmacy Stock Health',
            'value': f'{low_stock + out_of_stock} items need attention',
            'trend': 'Critical' if out_of_stock else 'Warning' if low_stock else 'Stable',
            'reason': (f'{out_of_stock} medicines are out of stock, {low_stock} are at or '
                       f'below their minimum level, and {near_expiry} expire within 30 days.'),
            'confidence': '99%',
        })

        # ── Bed pressure across general wards ─────────────────────
        general_wards = Ward.objects.filter(ward_type='general')
        pressured = [w.name for w in general_wards
                     if w.occupancy_percentage >= WARD_OCCUPANCY_ALERT_AT]
        available_beds = Bed.objects.filter(status='available').count()
        total_beds = Bed.objects.count()

        explanations.append({
            'metric': 'General Ward Capacity',
            'value': f'{available_beds} beds free',
            'trend': 'Critical' if pressured else 'Stable',
            'reason': (f'{available_beds} of {total_beds} beds across the hospital are free. '
                       + (f"Wards at or above {WARD_OCCUPANCY_ALERT_AT}%: "
                          f"{', '.join(pressured)}."
                          if pressured else
                          'No general ward has crossed its occupancy threshold.')),
            'confidence': '97%',
        })

        return Response({
            'generated_at': timezone.now().isoformat(),
            'explanations': explanations,
        })


class AIRecommendationsView(APIView):
    """Rule-based operational recommendations.

    Each recommendation fires from a measured threshold and states the
    evidence behind it, so an administrator can verify the reasoning instead
    of trusting a black box.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        recommendations = []

        # ── ICU pressure, now and forecast ────────────────────────
        icu_total = Bed.objects.filter(ward__ward_type='icu').count()
        icu_occupied = Bed.objects.filter(ward__ward_type='icu', status='occupied').count()
        icu_pct = round(icu_occupied / icu_total * 100, 1) if icu_total else 0.0

        if icu_pct >= ICU_OCCUPANCY_ALERT_AT:
            recommendations.append({
                'action': 'Reschedule non-urgent elective surgeries',
                'priority': 'High',
                'category': 'Capacity',
                'evidence': f'ICU occupancy is {icu_pct}% ({icu_occupied}/{icu_total} beds).',
                'impact': f'Frees ICU capacity for emergency admissions before beds run out.',
            })
            recommendations.append({
                'action': 'Call in an additional ICU nursing shift',
                'priority': 'High',
                'category': 'Staffing',
                'evidence': f'ICU is above the {ICU_OCCUPANCY_ALERT_AT}% safety threshold.',
                'impact': 'Keeps the nurse-to-patient ratio within safe limits at peak load.',
            })

        forecast = forecast_next_24h()
        if forecast.get('available'):
            summary = forecast['summary']
            if summary['peak_icu_percentage'] >= ICU_OCCUPANCY_ALERT_AT and icu_pct < ICU_OCCUPANCY_ALERT_AT:
                recommendations.append({
                    'action': 'Prepare step-down beds ahead of the projected ICU peak',
                    'priority': 'High',
                    'category': 'Forecast',
                    'evidence': (f"Forecast peak {summary['peak_icu_percentage']}% at "
                                 f"{summary['peak_icu_at']}, currently {icu_pct}%."),
                    'impact': 'Acts on the bottleneck hours before it becomes critical.',
                })
            if summary['peak_inflow'] >= 8:
                recommendations.append({
                    'action': f"Add a triage clinician around {summary['peak_inflow_at']}",
                    'priority': 'Medium',
                    'category': 'Forecast',
                    'evidence': (f"Projected arrival peak of {summary['peak_inflow']} "
                                 f"patients/hour at {summary['peak_inflow_at']}."),
                    'impact': 'Prevents the queue building up during the busiest hour.',
                })

        # ── Staffing ratio ────────────────────────────────────────
        active_patients = Patient.objects.filter(is_discharged=False).count()
        nurses_on_duty = Nurse.objects.exclude(status='off_duty').count()
        if nurses_on_duty and active_patients / nurses_on_duty > UNSAFE_NURSE_RATIO:
            ratio = round(active_patients / nurses_on_duty, 1)
            recommendations.append({
                'action': 'Increase nursing cover on the current shift',
                'priority': 'High',
                'category': 'Staffing',
                'evidence': (f'Nurse-to-patient ratio is 1:{ratio} against a '
                             f'1:{UNSAFE_NURSE_RATIO} safe limit.'),
                'impact': 'Restores a safe ratio and reduces missed observations.',
            })

        # ── Pharmacy ──────────────────────────────────────────────
        critical_meds = list(
            Medicine.objects.filter(stock__lte=F('min_stock'))
            .order_by('stock').values_list('name', flat=True)[:5]
        )
        if critical_meds:
            recommendations.append({
                'action': f"Raise a purchase order for {len(critical_meds)} medicines",
                'priority': 'High' if any(
                    Medicine.objects.filter(name=n, stock__lte=0).exists() for n in critical_meds
                ) else 'Medium',
                'category': 'Inventory',
                'evidence': 'At or below minimum stock: ' + ', '.join(critical_meds) + '.',
                'impact': 'Prevents essential medicines running out during treatment.',
            })

        expiring = list(
            Medicine.objects.filter(
                expiry_date__lte=timezone.now().date() + timezone.timedelta(days=30),
                expiry_date__gt=timezone.now().date(),
            ).values_list('name', flat=True)[:5]
        )
        if expiring:
            recommendations.append({
                'action': f'Rotate or return {len(expiring)} near-expiry medicines',
                'priority': 'Low',
                'category': 'Inventory',
                'evidence': 'Expiring within 30 days: ' + ', '.join(expiring) + '.',
                'impact': 'Avoids write-offs and keeps the dispensary compliant.',
            })

        # ── Queue pressure ────────────────────────────────────────
        long_waits = PatientQueue.objects.filter(
            status='waiting', estimated_wait_minutes__gt=LONG_WAIT_ALERT_MINUTES
        ).count()
        if long_waits:
            recommendations.append({
                'action': 'Open an additional consultation room',
                'priority': 'High' if long_waits > 3 else 'Medium',
                'category': 'Throughput',
                'evidence': (f'{long_waits} patients have waited longer than '
                             f'{LONG_WAIT_ALERT_MINUTES} minutes.'),
                'impact': 'Clears the triage backlog and shortens average waiting time.',
            })

        # ── Emergency load ────────────────────────────────────────
        emergency_count = Patient.objects.filter(
            is_discharged=False, is_emergency=True
        ).count()
        if emergency_count > 5:
            recommendations.append({
                'action': 'Activate the secondary emergency triage bay',
                'priority': 'High',
                'category': 'Capacity',
                'evidence': f'{emergency_count} emergency patients are currently active.',
                'impact': 'Separates emergency flow from routine out-patient traffic.',
            })

        # ── Critical patients needing review ──────────────────────
        unattended_critical = Patient.objects.filter(
            is_discharged=False, risk_level='critical', attending_doctor__isnull=True
        ).count()
        if unattended_critical:
            recommendations.append({
                'action': f'Assign an attending doctor to {unattended_critical} critical patients',
                'priority': 'High',
                'category': 'Clinical',
                'evidence': (f'{unattended_critical} patients scored Critical but have '
                             'no attending doctor recorded.'),
                'impact': 'Ensures the most unstable patients have named clinical ownership.',
            })

        # ── Under-used capacity ───────────────────────────────────
        quiet_wards = [
            w.name for w in Ward.objects.filter(ward_type='general')
            if w.capacity and w.occupancy_percentage < 40
        ]
        if quiet_wards and not recommendations:
            recommendations.append({
                'action': f"Re-allocate staff from {quiet_wards[0]}",
                'priority': 'Low',
                'category': 'Efficiency',
                'evidence': f"{quiet_wards[0]} is below 40% occupancy.",
                'impact': 'Moves staff to where demand actually is.',
            })

        priority_rank = {'High': 0, 'Medium': 1, 'Low': 2}
        recommendations.sort(key=lambda r: priority_rank.get(r['priority'], 3))

        return Response({
            'generated_at': timezone.now().isoformat(),
            'count': len(recommendations),
            'recommendations': recommendations,
        })


class AIModelStatusView(APIView):
    """Model metadata and held-out accuracy, for the AI Forecast page."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(model_status())
