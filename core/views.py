"""
Core REST API.

Every endpoint requires a valid JWT except ``login/`` and ``token/refresh/``.
Role restrictions come from core.permissions, so a Receptionist token cannot
reach an Admin-only route.

Derived clinical values (risk level, triage priority) are computed in
core.clinical; report files in core.reports; the chat assistant in
core.chatbot. This module stays focused on request handling.
"""
from django.contrib.auth import authenticate
from django.db import transaction
from django.db.models import Avg, Count, F, Q
from django.utils import timezone
from datetime import datetime, timedelta
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from . import reports as report_engine
from .chatbot import answer as chatbot_answer, suggested_questions
from .clinical import (
    apply_risk_assessment,
    assess_patient,
    estimate_wait_minutes,
    priority_score,
)
from .models import (
    ActivityLog, Alert, Appointment, Bed, Doctor, HospitalMetric, HospitalUser,
    Medicine, MedicineLog, Nurse, Patient, PatientQueue, PatientTimeline,
    Prediction, Ward,
)
from .permissions import (
    ROLE_MODULES,
    CanManageAppointments,
    CanManageBeds,
    CanManageMedicines,
    CanManagePatients,
    CanResolveAlerts,
    IsAdmin,
    IsClinicalStaff,
)
from .serializers import (
    ActivityLogSerializer, AlertSerializer, AppointmentSerializer,
    BedSerializer, DoctorSerializer, HospitalMetricSerializer,
    MedicineLogSerializer, MedicineSerializer, NurseSerializer,
    PatientDetailSerializer, PatientListSerializer, PatientQueueSerializer,
    PatientTimelineSerializer, PredictionSerializer, UserSerializer,
    WardSerializer,
)
from .simulation import simulation_tick

# Cap on list endpoints so a huge table cannot stall the dashboard.
DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 500


def log_activity(request, action, target_model='', target_id=None, details='',
                 user=None):
    """Write an audit-trail entry for a state-changing request.

    ``user`` is normally taken from the request, but the login endpoint must
    pass it explicitly: authentication has not happened yet at that point, so
    ``request.user`` is still anonymous and the entry would lose its author.
    """
    if user is None:
        user = request.user if request.user.is_authenticated else None
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    ip = forwarded.split(',')[0].strip() if forwarded else request.META.get('REMOTE_ADDR', '')

    ActivityLog.objects.create(
        user=user,
        action=action,
        target_model=target_model,
        target_id=target_id,
        details=details,
        ip_address=ip[:45],
    )


def _month_start(year, month):
    """First moment of a calendar month, in the project's local timezone."""
    return timezone.make_aware(datetime(year, month, 1))


def _page_size(request, default=DEFAULT_PAGE_SIZE):
    """Read and clamp a ``limit`` query parameter."""
    try:
        requested = int(request.query_params.get('limit', default))
    except (TypeError, ValueError):
        return default
    return max(1, min(MAX_PAGE_SIZE, requested))


# ─── Authentication ───────────────────────────────────────────────

class LoginView(APIView):
    """Exchange username/password for a JWT access + refresh token pair."""
    permission_classes = [AllowAny]

    def post(self, request):
        username = (request.data.get('username') or '').strip()
        password = request.data.get('password') or ''

        if not username or not password:
            return Response(
                {'error': 'Username and password are both required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = authenticate(username=username, password=password)
        if user is None:
            return Response(
                {'error': 'Invalid credentials. Please check your username and password.'},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        if not user.is_active:
            return Response(
                {'error': 'This account has been deactivated.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        refresh = RefreshToken.for_user(user)
        # Carry the role in the token so the frontend can render the correct
        # navigation before its first authenticated API call returns.
        refresh['role'] = user.role

        user.last_login = timezone.now()
        user.save(update_fields=['last_login'])
        log_activity(request, 'Signed in', 'HospitalUser', user.id,
                     f'Role: {user.get_role_display()}', user=user)

        return Response({
            'user': UserSerializer(user).data,
            'access': str(refresh.access_token),
            'refresh': str(refresh),
            'modules': ROLE_MODULES.get(user.role, []),
        })


class TokenRefreshView(APIView):
    """Issue a fresh access token from a valid refresh token."""
    permission_classes = [AllowAny]

    def post(self, request):
        token = request.data.get('refresh')
        if not token:
            return Response({'error': 'A refresh token is required.'},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            refresh = RefreshToken(token)
            return Response({'access': str(refresh.access_token)})
        except Exception:                                 # noqa: BLE001
            return Response({'error': 'Refresh token is invalid or has expired.'},
                            status=status.HTTP_401_UNAUTHORIZED)


class LogoutView(APIView):
    """Record the sign-out in the audit trail."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        log_activity(request, 'Signed out', 'HospitalUser', request.user.id)
        return Response({'message': 'Signed out successfully.'})


class UserInfoView(APIView):
    """Current user, their role and the modules that role may open."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({
            **UserSerializer(request.user).data,
            'modules': ROLE_MODULES.get(request.user.role, []),
        })

    def put(self, request):
        """Update the signed-in user's own profile fields."""
        user = request.user
        for field in ('first_name', 'last_name', 'email', 'phone', 'avatar_color'):
            if field in request.data:
                setattr(user, field, request.data[field])
        user.save()
        log_activity(request, 'Updated own profile', 'HospitalUser', user.id)
        return Response(UserSerializer(user).data)


class ChangePasswordView(APIView):
    """Let a signed-in user change their own password."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        current = request.data.get('current_password') or ''
        new_password = request.data.get('new_password') or ''

        if not request.user.check_password(current):
            return Response({'error': 'Current password is incorrect.'},
                            status=status.HTTP_400_BAD_REQUEST)
        if len(new_password) < 8:
            return Response({'error': 'New password must be at least 8 characters.'},
                            status=status.HTTP_400_BAD_REQUEST)

        request.user.set_password(new_password)
        request.user.save()
        log_activity(request, 'Changed password', 'HospitalUser', request.user.id)
        return Response({'message': 'Password updated successfully.'})


# ─── Dashboard ────────────────────────────────────────────────────

class DashboardStatsView(APIView):
    """Executive KPIs for the Live Command Center."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        now = timezone.localtime()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        yesterday_start = today_start - timedelta(days=1)

        active = Patient.objects.filter(is_discharged=False)

        today_admissions = Patient.objects.filter(admitted_at__gte=today_start).count()
        yesterday_admissions = Patient.objects.filter(
            admitted_at__gte=yesterday_start, admitted_at__lt=today_start
        ).count()
        admission_trend = round(
            (today_admissions - yesterday_admissions) / yesterday_admissions * 100, 1
        ) if yesterday_admissions else 0.0

        # One grouped query instead of four counts.
        bed_counts = {
            row['status']: row['n']
            for row in Bed.objects.values('status').annotate(n=Count('id'))
        }
        total_beds = sum(bed_counts.values())
        occupied_beds = bed_counts.get('occupied', 0)

        icu_total = Bed.objects.filter(ward__ward_type='icu').count()
        icu_occupied = Bed.objects.filter(ward__ward_type='icu', status='occupied').count()

        risk_counts = {
            row['risk_level']: row['n']
            for row in active.values('risk_level').annotate(n=Count('id'))
        }

        return Response({
            'total_patients': active.count(),
            'today_admissions': today_admissions,
            'today_discharges': Patient.objects.filter(
                discharged_at__gte=today_start
            ).count(),
            'admission_trend': admission_trend,

            'icu_occupied': icu_occupied,
            'icu_total': icu_total,
            'icu_percentage': round(icu_occupied / icu_total * 100, 1) if icu_total else 0.0,

            'total_beds': total_beds,
            'occupied_beds': occupied_beds,
            'available_beds': bed_counts.get('available', 0),
            'maintenance_beds': bed_counts.get('maintenance', 0),
            'bed_occupancy_percentage': round(
                occupied_beds / total_beds * 100, 1
            ) if total_beds else 0.0,

            'emergency_patients': active.filter(is_emergency=True).count(),
            'critical_patients': risk_counts.get('critical', 0),
            'high_risk_patients': risk_counts.get('high', 0),
            'medium_risk_patients': risk_counts.get('medium', 0),
            'low_risk_patients': risk_counts.get('low', 0),

            'doctors_on_duty': Doctor.objects.exclude(status='off_duty').count(),
            'doctors_available': Doctor.objects.filter(status='available').count(),
            'nurses_on_duty': Nurse.objects.exclude(status='off_duty').count(),

            'queue_waiting': PatientQueue.objects.filter(status='waiting').count(),
            'queue_in_consultation': PatientQueue.objects.filter(
                status='in_consultation'
            ).count(),
            'avg_wait_time': round(
                PatientQueue.objects.filter(status='waiting')
                .aggregate(v=Avg('estimated_wait_minutes'))['v'] or 0, 1
            ),

            'critical_alerts': Alert.objects.filter(
                level='critical', is_resolved=False
            ).count(),
            'warning_alerts': Alert.objects.filter(
                level='warning', is_resolved=False
            ).count(),

            'low_stock_medicines': Medicine.objects.filter(
                stock__lte=F('min_stock')
            ).count(),
            'out_of_stock_medicines': Medicine.objects.filter(stock__lte=0).count(),

            'server_time': now.isoformat(),
        })


class AdmissionTrendView(APIView):
    """Hourly admissions vs discharges for the dashboard trend chart.

    Built from the Patient table rather than metric snapshots so the chart is
    correct even on a database that has never run the simulation.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            hours = max(1, min(168, int(request.query_params.get('hours', 24))))
        except (TypeError, ValueError):
            hours = 24

        now = timezone.localtime().replace(minute=0, second=0, microsecond=0)
        window_start = now - timedelta(hours=hours - 1)

        admissions = {}
        for row in Patient.objects.filter(admitted_at__gte=window_start):
            key = timezone.localtime(row.admitted_at).replace(
                minute=0, second=0, microsecond=0
            )
            admissions[key] = admissions.get(key, 0) + 1

        discharges = {}
        for row in Patient.objects.filter(discharged_at__gte=window_start):
            key = timezone.localtime(row.discharged_at).replace(
                minute=0, second=0, microsecond=0
            )
            discharges[key] = discharges.get(key, 0) + 1

        points = []
        for offset in range(hours):
            bucket = window_start + timedelta(hours=offset)
            points.append({
                'timestamp': bucket.isoformat(),
                'hour_label': bucket.strftime('%H:00'),
                'admissions': admissions.get(bucket, 0),
                'discharges': discharges.get(bucket, 0),
            })

        return Response({'hours': hours, 'points': points})


# ─── Patients ─────────────────────────────────────────────────────

class PatientListView(APIView):
    """List and filter patients, or register a new admission."""
    permission_classes = [CanManagePatients]

    def get(self, request):
        queryset = Patient.objects.select_related(
            'attending_doctor', 'assigned_bed', 'assigned_bed__ward'
        )

        params = request.query_params
        if params.get('department'):
            queryset = queryset.filter(department=params['department'])
        if params.get('risk_level'):
            queryset = queryset.filter(risk_level__in=params['risk_level'].split(','))
        if params.get('disease'):
            queryset = queryset.filter(disease=params['disease'])
        if params.get('discharged') is not None and params.get('discharged') != '':
            queryset = queryset.filter(
                is_discharged=params['discharged'].lower() == 'true'
            )
        if params.get('emergency') == 'true':
            queryset = queryset.filter(is_emergency=True)
        if params.get('unassigned') == 'true':
            queryset = queryset.filter(assigned_bed__isnull=True, is_discharged=False)
        if params.get('search'):
            term = params['search']
            queryset = queryset.filter(
                Q(name__icontains=term)
                | Q(disease__icontains=term)
                | Q(phone__icontains=term)
                | Q(department__icontains=term)
            )

        ordering = params.get('ordering', '-admitted_at')
        allowed_ordering = {
            'name', '-name', 'age', '-age', 'admitted_at', '-admitted_at',
            'disease_severity', '-disease_severity', 'risk_level', '-risk_level',
        }
        if ordering in allowed_ordering:
            queryset = queryset.order_by(ordering)

        total = queryset.count()
        limit = _page_size(request)
        return Response({
            'count': total,
            'results': PatientListSerializer(queryset[:limit], many=True).data,
        })

    @transaction.atomic
    def post(self, request):
        serializer = PatientDetailSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        patient = serializer.save()

        # Score the new patient immediately so triage ordering is correct from
        # the moment of registration.
        assessment = apply_risk_assessment(patient)

        PatientTimeline.objects.create(
            patient=patient, event_type='registered',
            description=(f'Registered in {patient.department} with '
                         f'{patient.disease} (severity {patient.disease_severity}/10).'),
            created_by=request.user.get_full_name() or request.user.username,
        )
        PatientTimeline.objects.create(
            patient=patient, event_type='vitals_recorded',
            description=(f'HR {patient.heart_rate} BPM, Temp {patient.temperature} F, '
                         f'BP {patient.blood_pressure_systolic}/'
                         f'{patient.blood_pressure_diastolic}, '
                         f'SpO2 {patient.oxygen_saturation}%. '
                         f"Risk assessed as {assessment['risk_level'].title()}."),
            created_by=request.user.get_full_name() or request.user.username,
        )

        # Emergency and high-risk arrivals join the triage queue automatically.
        if patient.is_emergency or assessment['risk_level'] in ('high', 'critical'):
            waiting = PatientQueue.objects.filter(status='waiting').count()
            PatientQueue.objects.create(
                patient=patient,
                priority_score=priority_score(patient, assessment=assessment),
                doctor_assigned=patient.attending_doctor,
                estimated_wait_minutes=estimate_wait_minutes(
                    waiting + 1, Doctor.objects.filter(status='available').count()
                ),
                department=patient.department,
            )

        log_activity(request, 'Registered patient', 'Patient', patient.id, patient.name)

        return Response(
            {**PatientDetailSerializer(patient).data, 'assessment': assessment},
            status=status.HTTP_201_CREATED,
        )


class PatientDetailView(APIView):
    """Retrieve, update or discharge one patient."""
    permission_classes = [CanManagePatients]

    def _get(self, pk):
        return Patient.objects.select_related(
            'attending_doctor', 'assigned_bed', 'assigned_bed__ward'
        ).filter(pk=pk).first()

    def get(self, request, pk):
        patient = self._get(pk)
        if patient is None:
            return Response({'error': 'Patient not found.'},
                            status=status.HTTP_404_NOT_FOUND)
        return Response({
            **PatientDetailSerializer(patient).data,
            'assessment': assess_patient(patient),
        })

    def put(self, request, pk):
        patient = self._get(pk)
        if patient is None:
            return Response({'error': 'Patient not found.'},
                            status=status.HTTP_404_NOT_FOUND)

        previous_risk = patient.risk_level
        serializer = PatientDetailSerializer(patient, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)

        patient = serializer.save()
        assessment = apply_risk_assessment(patient)

        if assessment['risk_level'] != previous_risk:
            PatientTimeline.objects.create(
                patient=patient, event_type='condition_update',
                description=(f"Risk level changed from {previous_risk.title()} to "
                             f"{assessment['risk_level'].title()} after record update."),
                created_by=request.user.get_full_name() or request.user.username,
            )

        log_activity(request, 'Updated patient', 'Patient', patient.id, patient.name)
        return Response({
            **PatientDetailSerializer(patient).data,
            'assessment': assessment,
        })


class PatientVitalsView(APIView):
    """Record a new set of vitals and re-score the patient.

    Restricted to clinical staff — a receptionist must not be able to alter
    the observations that drive triage priority.
    """
    permission_classes = [IsClinicalStaff]

    VITAL_FIELDS = (
        'heart_rate', 'temperature', 'blood_pressure_systolic',
        'blood_pressure_diastolic', 'oxygen_saturation',
    )

    @transaction.atomic
    def post(self, request, pk):
        patient = Patient.objects.filter(pk=pk).first()
        if patient is None:
            return Response({'error': 'Patient not found.'},
                            status=status.HTTP_404_NOT_FOUND)
        if patient.is_discharged:
            return Response({'error': 'Cannot record vitals for a discharged patient.'},
                            status=status.HTTP_400_BAD_REQUEST)

        provided = {f: request.data[f] for f in self.VITAL_FIELDS if f in request.data}
        if not provided:
            return Response(
                {'error': f'Provide at least one of: {", ".join(self.VITAL_FIELDS)}.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = PatientDetailSerializer(patient, data=provided, partial=True)
        serializer.is_valid(raise_exception=True)

        previous_risk = patient.risk_level
        patient = serializer.save()
        assessment = apply_risk_assessment(patient)

        PatientTimeline.objects.create(
            patient=patient, event_type='vitals_recorded',
            description=(f'HR {patient.heart_rate} BPM, Temp {patient.temperature} F, '
                         f'BP {patient.blood_pressure_systolic}/'
                         f'{patient.blood_pressure_diastolic}, '
                         f'SpO2 {patient.oxygen_saturation}%. '
                         f"Risk: {assessment['risk_level'].title()}."),
            created_by=request.user.get_full_name() or request.user.username,
        )

        # Keep the triage queue honest: new vitals change the priority score.
        entry = PatientQueue.objects.filter(patient=patient, status='waiting').first()
        if entry:
            entry.priority_score = priority_score(
                patient, waiting_since=entry.waiting_since, assessment=assessment
            )
            entry.save(update_fields=['priority_score'])

        if assessment['risk_level'] == 'critical' and previous_risk != 'critical':
            Alert.objects.create(
                level='critical', alert_type='patient_critical',
                message=f'{patient.name} has deteriorated to critical',
                description=(f"Newly recorded vitals scored {assessment['risk_score']}/100. "
                             f"SpO2 {patient.oxygen_saturation}%, "
                             f"HR {patient.heart_rate} BPM."),
                source='Vitals Monitor',
            )

        log_activity(request, 'Recorded vitals', 'Patient', patient.id, patient.name)
        return Response({
            'patient': PatientDetailSerializer(patient).data,
            'assessment': assessment,
            'risk_changed': assessment['risk_level'] != previous_risk,
        })


class PatientDischargeView(APIView):
    """Discharge a patient, free their bed and close their queue entry."""
    permission_classes = [IsClinicalStaff]

    @transaction.atomic
    def post(self, request, pk):
        patient = Patient.objects.filter(pk=pk).first()
        if patient is None:
            return Response({'error': 'Patient not found.'},
                            status=status.HTTP_404_NOT_FOUND)
        if patient.is_discharged:
            return Response({'error': 'This patient is already discharged.'},
                            status=status.HTTP_400_BAD_REQUEST)

        assessment = assess_patient(patient)
        # Discharging a critical patient needs an explicit override so it
        # cannot happen by a misclick.
        if assessment['risk_level'] == 'critical' and not request.data.get('override'):
            return Response(
                {
                    'error': 'This patient is currently rated Critical.',
                    'detail': ('Discharge requires override=true to confirm the '
                               'decision was taken deliberately.'),
                    'assessment': assessment,
                },
                status=status.HTTP_409_CONFLICT,
            )

        notes = request.data.get('notes', 'Cleared for discharge.')

        patient.is_discharged = True
        patient.discharged_at = timezone.now()
        patient.condition = request.data.get('condition', 'Recovered — Discharged')
        patient.save(update_fields=['is_discharged', 'discharged_at', 'condition'])

        released = list(
            Bed.objects.filter(patient=patient).values_list('bed_number', flat=True)
        )
        Bed.objects.filter(patient=patient).update(status='available', patient=None)
        PatientQueue.objects.filter(patient=patient).exclude(
            status='completed'
        ).update(status='completed')

        PatientTimeline.objects.create(
            patient=patient, event_type='discharged',
            description=(f'Discharged. {notes}'
                         + (f' Bed {", ".join(released)} released.' if released else '')),
            created_by=request.user.get_full_name() or request.user.username,
        )

        log_activity(request, 'Discharged patient', 'Patient', patient.id, patient.name)
        return Response({
            'message': f'{patient.name} discharged successfully.',
            'beds_released': released,
            'patient': PatientDetailSerializer(patient).data,
        })


class PatientTimelineView(APIView):
    """Chronological clinical event history for one patient."""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        if not Patient.objects.filter(pk=pk).exists():
            return Response({'error': 'Patient not found.'},
                            status=status.HTTP_404_NOT_FOUND)

        events = PatientTimeline.objects.filter(patient_id=pk).order_by('-timestamp')
        return Response({
            'count': events.count(),
            'results': PatientTimelineSerializer(events[:200], many=True).data,
        })

    def post(self, request, pk):
        """Let clinical staff add a note to the timeline."""
        if request.user.role not in ('admin', 'doctor', 'nurse'):
            return Response({'error': 'Only clinical staff may add timeline entries.'},
                            status=status.HTTP_403_FORBIDDEN)

        patient = Patient.objects.filter(pk=pk).first()
        if patient is None:
            return Response({'error': 'Patient not found.'},
                            status=status.HTTP_404_NOT_FOUND)

        description = (request.data.get('description') or '').strip()
        if not description:
            return Response({'error': 'A description is required.'},
                            status=status.HTTP_400_BAD_REQUEST)

        valid_types = {choice[0] for choice in PatientTimeline.EVENT_TYPES}
        event_type = request.data.get('event_type', 'condition_update')
        if event_type not in valid_types:
            return Response(
                {'error': f'event_type must be one of: {", ".join(sorted(valid_types))}.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        event = PatientTimeline.objects.create(
            patient=patient, event_type=event_type, description=description,
            created_by=request.user.get_full_name() or request.user.username,
        )
        log_activity(request, 'Added timeline entry', 'Patient', patient.id, event_type)
        return Response(PatientTimelineSerializer(event).data,
                        status=status.HTTP_201_CREATED)


class PatientRiskView(APIView):
    """Explainable risk assessment for one patient."""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        patient = Patient.objects.filter(pk=pk).first()
        if patient is None:
            return Response({'error': 'Patient not found.'},
                            status=status.HTTP_404_NOT_FOUND)

        assessment = assess_patient(patient)
        return Response({
            'patient_id': patient.id,
            'patient_name': patient.name,
            'disease': patient.disease,
            'disease_severity': patient.disease_severity,
            'is_emergency': patient.is_emergency,
            'priority_score': priority_score(patient, assessment=assessment),
            **assessment,
        })


class PatientRiskBoardView(APIView):
    """All active patients ranked by risk, for the Patient Risk page."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        patients = Patient.objects.filter(is_discharged=False).select_related(
            'attending_doctor', 'assigned_bed', 'assigned_bed__ward'
        )

        rows = []
        for patient in patients:
            assessment = assess_patient(patient)
            bed = getattr(patient, 'assigned_bed', None)
            rows.append({
                'id': patient.id,
                'name': patient.name,
                'age': patient.age,
                'gender': patient.gender,
                'disease': patient.disease,
                'disease_severity': patient.disease_severity,
                'department': patient.department,
                'is_emergency': patient.is_emergency,
                'bed_number': bed.bed_number if bed else None,
                'ward_name': bed.ward.name if bed else None,
                'attending_doctor_name': (
                    patient.attending_doctor.name if patient.attending_doctor else None
                ),
                'risk_level': assessment['risk_level'],
                'risk_score': assessment['risk_score'],
                'confidence': assessment['confidence'],
                'factors': assessment['factors'],
                'vitals': assessment['vitals'],
            })

        rows.sort(key=lambda r: r['risk_score'], reverse=True)

        distribution = {level: 0 for level in ('critical', 'high', 'medium', 'low')}
        for row in rows:
            distribution[row['risk_level']] += 1

        return Response({
            'count': len(rows),
            'distribution': distribution,
            'results': rows,
        })


# ─── Wards ────────────────────────────────────────────────────────

class WardListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        wards = Ward.objects.all().order_by('floor', 'name')
        if request.query_params.get('ward_type'):
            wards = wards.filter(ward_type=request.query_params['ward_type'])
        if request.query_params.get('floor'):
            wards = wards.filter(floor=request.query_params['floor'])
        return Response(WardSerializer(wards, many=True).data)


class WardDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        ward = Ward.objects.filter(pk=pk).first()
        if ward is None:
            return Response({'error': 'Ward not found.'},
                            status=status.HTTP_404_NOT_FOUND)

        beds = Bed.objects.filter(ward=ward).select_related(
            'patient', 'ward'
        ).order_by('bed_number')
        patients = Patient.objects.filter(
            is_discharged=False, assigned_bed__ward=ward
        ).select_related('attending_doctor')

        return Response({
            'ward': WardSerializer(ward).data,
            'beds': BedSerializer(beds, many=True).data,
            'patients': PatientListSerializer(patients, many=True).data,
        })


class FloorMapView(APIView):
    """Complete floor-by-floor bed map for the interactive 2D floor plan.

    Returned as one payload so the floor map renders in a single request
    instead of one call per ward.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        beds = Bed.objects.select_related('ward', 'patient').order_by(
            'ward__floor', 'ward__name', 'bed_number'
        )

        floors = {}
        for bed in beds:
            floor_key = bed.ward.floor
            floor = floors.setdefault(floor_key, {'floor': floor_key, 'wards': {}})
            ward_entry = floor['wards'].setdefault(bed.ward.id, {
                'id': bed.ward.id,
                'name': bed.ward.name,
                'ward_type': bed.ward.ward_type,
                'ward_type_display': bed.ward.get_ward_type_display(),
                'capacity': bed.ward.capacity,
                'occupancy_percentage': bed.ward.occupancy_percentage,
                'occupancy_color': bed.ward.occupancy_color,
                'beds': [],
            })

            patient = bed.patient
            ward_entry['beds'].append({
                'id': bed.id,
                'bed_number': bed.bed_number,
                'status': bed.status,
                'patient_id': patient.id if patient else None,
                'patient_name': patient.name if patient else None,
                'patient_risk_level': patient.risk_level if patient else None,
                'patient_disease': patient.disease if patient else None,
                'patient_is_emergency': patient.is_emergency if patient else False,
                'admitted_at': patient.admitted_at.isoformat() if patient else None,
            })

        payload = []
        for floor_key in sorted(floors):
            floor = floors[floor_key]
            payload.append({
                'floor': floor['floor'],
                'wards': list(floor['wards'].values()),
            })

        return Response({
            'floors': payload,
            'legend': {
                'occupancy': [
                    {'color': 'green', 'label': 'Below 60%'},
                    {'color': 'yellow', 'label': '60% to 84%'},
                    {'color': 'red', 'label': '85% and above'},
                ],
                'bed_status': [
                    {'status': s[0], 'label': s[1]} for s in Bed.STATUS_CHOICES
                ],
            },
        })


# ─── Beds ─────────────────────────────────────────────────────────

class BedListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        queryset = Bed.objects.select_related('ward', 'patient').order_by(
            'ward__floor', 'bed_number'
        )

        params = request.query_params
        if params.get('ward'):
            queryset = queryset.filter(ward_id=params['ward'])
        if params.get('ward_type'):
            queryset = queryset.filter(ward__ward_type=params['ward_type'])
        if params.get('status'):
            queryset = queryset.filter(status__in=params['status'].split(','))
        if params.get('search'):
            queryset = queryset.filter(
                Q(bed_number__icontains=params['search'])
                | Q(patient__name__icontains=params['search'])
            )

        return Response(BedSerializer(queryset, many=True).data)


class BedAssignView(APIView):
    """Assign a patient to a bed."""
    permission_classes = [CanManageBeds]

    @transaction.atomic
    def post(self, request, pk):
        bed = Bed.objects.select_related('ward').filter(pk=pk).first()
        if bed is None:
            return Response({'error': 'Bed not found.'},
                            status=status.HTTP_404_NOT_FOUND)

        if bed.status == 'maintenance':
            return Response({'error': f'Bed {bed.bed_number} is under maintenance.'},
                            status=status.HTTP_400_BAD_REQUEST)
        if bed.status == 'occupied' and bed.patient_id:
            return Response(
                {'error': f'Bed {bed.bed_number} is already occupied by '
                          f'{bed.patient.name}.'},
                status=status.HTTP_409_CONFLICT,
            )

        patient_id = request.data.get('patient_id')
        if not patient_id:
            return Response({'error': 'patient_id is required.'},
                            status=status.HTTP_400_BAD_REQUEST)

        patient = Patient.objects.filter(pk=patient_id).first()
        if patient is None:
            return Response({'error': 'Patient not found.'},
                            status=status.HTTP_404_NOT_FOUND)
        if patient.is_discharged:
            return Response({'error': 'Cannot assign a bed to a discharged patient.'},
                            status=status.HTTP_400_BAD_REQUEST)

        # Free whatever bed the patient currently holds, so a patient can
        # never appear in two beds at once.
        previous = Bed.objects.filter(patient=patient).exclude(pk=bed.pk)
        previous_numbers = list(previous.values_list('bed_number', flat=True))
        previous.update(status='available', patient=None)

        bed.status = 'occupied'
        bed.patient = patient
        bed.save(update_fields=['status', 'patient'])

        PatientTimeline.objects.create(
            patient=patient,
            event_type='shifted_icu' if bed.ward.ward_type == 'icu' else 'shifted_ward',
            description=(f'Assigned to bed {bed.bed_number} in {bed.ward.name}'
                         + (f' (released {", ".join(previous_numbers)})'
                            if previous_numbers else '') + '.'),
            created_by=request.user.get_full_name() or request.user.username,
        )

        log_activity(request, 'Assigned bed', 'Bed', bed.id,
                     f'{patient.name} -> {bed.bed_number}')
        return Response({
            'message': f'{patient.name} assigned to bed {bed.bed_number}.',
            'bed': BedSerializer(bed).data,
            'released': previous_numbers,
        })


class BedReleaseView(APIView):
    """Free a bed without discharging the patient."""
    permission_classes = [CanManageBeds]

    @transaction.atomic
    def post(self, request, pk):
        bed = Bed.objects.select_related('patient', 'ward').filter(pk=pk).first()
        if bed is None:
            return Response({'error': 'Bed not found.'},
                            status=status.HTTP_404_NOT_FOUND)

        patient = bed.patient
        bed.status = request.data.get('status', 'available')
        if bed.status not in {choice[0] for choice in Bed.STATUS_CHOICES}:
            return Response({'error': 'Invalid bed status.'},
                            status=status.HTTP_400_BAD_REQUEST)
        bed.patient = None
        bed.save(update_fields=['status', 'patient'])

        if patient:
            PatientTimeline.objects.create(
                patient=patient, event_type='condition_update',
                description=f'Released from bed {bed.bed_number} in {bed.ward.name}.',
                created_by=request.user.get_full_name() or request.user.username,
            )

        log_activity(request, 'Released bed', 'Bed', bed.id, bed.bed_number)
        return Response({
            'message': f'Bed {bed.bed_number} released.',
            'bed': BedSerializer(bed).data,
        })


class BedTransferView(APIView):
    """Move a patient from one bed to another."""
    permission_classes = [CanManageBeds]

    @transaction.atomic
    def post(self, request, pk):
        source = Bed.objects.select_related('patient', 'ward').filter(pk=pk).first()
        if source is None:
            return Response({'error': 'Source bed not found.'},
                            status=status.HTTP_404_NOT_FOUND)

        target_id = request.data.get('target_bed_id')
        if not target_id:
            return Response({'error': 'target_bed_id is required.'},
                            status=status.HTTP_400_BAD_REQUEST)

        target = Bed.objects.select_related('ward').filter(pk=target_id).first()
        if target is None:
            return Response({'error': 'Target bed not found.'},
                            status=status.HTTP_404_NOT_FOUND)
        if target.pk == source.pk:
            return Response({'error': 'Source and target bed are the same.'},
                            status=status.HTTP_400_BAD_REQUEST)
        if target.status != 'available':
            return Response(
                {'error': f'Bed {target.bed_number} is not available '
                          f'({target.get_status_display()}).'},
                status=status.HTTP_409_CONFLICT,
            )

        patient = source.patient
        if patient is None:
            return Response({'error': 'There is no patient in the source bed.'},
                            status=status.HTTP_400_BAD_REQUEST)

        source.status = 'available'
        source.patient = None
        source.save(update_fields=['status', 'patient'])

        target.status = 'occupied'
        target.patient = patient
        target.save(update_fields=['status', 'patient'])

        PatientTimeline.objects.create(
            patient=patient,
            event_type='shifted_icu' if target.ward.ward_type == 'icu' else 'shifted_ward',
            description=(f'Transferred from {source.bed_number} ({source.ward.name}) '
                         f'to {target.bed_number} ({target.ward.name}).'),
            created_by=request.user.get_full_name() or request.user.username,
        )

        log_activity(request, 'Transferred patient', 'Bed', target.id,
                     f'{patient.name}: {source.bed_number} -> {target.bed_number}')
        return Response({
            'message': (f'{patient.name} transferred from {source.bed_number} '
                        f'to {target.bed_number}.'),
            'source': BedSerializer(source).data,
            'target': BedSerializer(target).data,
        })


class BedStatusView(APIView):
    """Change a bed's status, e.g. mark it for maintenance."""
    permission_classes = [CanManageBeds]

    def post(self, request, pk):
        bed = Bed.objects.select_related('patient').filter(pk=pk).first()
        if bed is None:
            return Response({'error': 'Bed not found.'},
                            status=status.HTTP_404_NOT_FOUND)

        new_status = request.data.get('status')
        valid = {choice[0] for choice in Bed.STATUS_CHOICES}
        if new_status not in valid:
            return Response({'error': f'status must be one of: {", ".join(sorted(valid))}.'},
                            status=status.HTTP_400_BAD_REQUEST)
        if new_status != 'occupied' and bed.patient_id:
            return Response(
                {'error': f'Bed {bed.bed_number} still holds {bed.patient.name}. '
                          'Release or transfer the patient first.'},
                status=status.HTTP_409_CONFLICT,
            )

        bed.status = new_status
        bed.save(update_fields=['status'])
        log_activity(request, 'Changed bed status', 'Bed', bed.id,
                     f'{bed.bed_number} -> {new_status}')
        return Response(BedSerializer(bed).data)


# ─── Triage queue ─────────────────────────────────────────────────

class PatientQueueView(APIView):
    """Priority-ordered triage queue."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        queue = PatientQueue.objects.filter(
            status__in=['waiting', 'in_consultation']
        ).select_related('patient', 'doctor_assigned')

        if request.query_params.get('department'):
            queue = queue.filter(department=request.query_params['department'])
        if request.query_params.get('status'):
            queue = queue.filter(status=request.query_params['status'])

        queue = queue.order_by('-priority_score', 'waiting_since')
        entries = PatientQueueSerializer(queue, many=True).data

        # Rank is the whole point of the smart queue, so it is served rather
        # than left for the client to compute.
        for index, entry in enumerate(entries, start=1):
            entry['rank'] = index

        waiting = [e for e in entries if e['status'] == 'waiting']
        return Response({
            'count': len(entries),
            'waiting': len(waiting),
            'in_consultation': len(entries) - len(waiting),
            'avg_wait_minutes': round(
                sum(e['estimated_wait_minutes'] for e in waiting) / len(waiting), 1
            ) if waiting else 0.0,
            'longest_wait_minutes': max(
                (e['estimated_wait_minutes'] for e in waiting), default=0
            ),
            'results': entries,
        })

    @transaction.atomic
    def post(self, request):
        """Add a patient to the triage queue."""
        patient_id = request.data.get('patient_id')
        patient = Patient.objects.filter(pk=patient_id, is_discharged=False).first()
        if patient is None:
            return Response({'error': 'Active patient not found.'},
                            status=status.HTTP_404_NOT_FOUND)

        existing = PatientQueue.objects.filter(
            patient=patient, status__in=['waiting', 'in_consultation']
        ).first()
        if existing:
            return Response(
                {'error': f'{patient.name} is already in the queue.',
                 'entry': PatientQueueSerializer(existing).data},
                status=status.HTTP_409_CONFLICT,
            )

        assessment = assess_patient(patient)
        waiting = PatientQueue.objects.filter(status='waiting').count()
        entry = PatientQueue.objects.create(
            patient=patient,
            priority_score=priority_score(patient, assessment=assessment),
            doctor_assigned=patient.attending_doctor,
            estimated_wait_minutes=estimate_wait_minutes(
                waiting + 1, Doctor.objects.filter(status='available').count()
            ),
            department=request.data.get('department', patient.department),
        )

        log_activity(request, 'Added to triage queue', 'PatientQueue', entry.id,
                     patient.name)
        return Response(PatientQueueSerializer(entry).data,
                        status=status.HTTP_201_CREATED)


class QueueEntryActionView(APIView):
    """Advance one queue entry: call, complete or assign a doctor."""
    permission_classes = [IsClinicalStaff]

    @transaction.atomic
    def post(self, request, pk):
        entry = PatientQueue.objects.select_related('patient').filter(pk=pk).first()
        if entry is None:
            return Response({'error': 'Queue entry not found.'},
                            status=status.HTTP_404_NOT_FOUND)

        action = request.data.get('action')

        if action == 'call':
            if entry.status != 'waiting':
                return Response({'error': 'Only a waiting patient can be called.'},
                                status=status.HTTP_400_BAD_REQUEST)
            entry.status = 'in_consultation'
            entry.save(update_fields=['status'])
            PatientTimeline.objects.create(
                patient=entry.patient, event_type='doctor_assigned',
                description=(f'Called for consultation from the triage queue '
                             f'(priority {entry.priority_score}/100).'),
                created_by=request.user.get_full_name() or request.user.username,
            )

        elif action == 'complete':
            entry.status = 'completed'
            entry.save(update_fields=['status'])
            PatientTimeline.objects.create(
                patient=entry.patient, event_type='condition_update',
                description=request.data.get('notes', 'Consultation completed.'),
                created_by=request.user.get_full_name() or request.user.username,
            )

        elif action == 'assign_doctor':
            doctor = Doctor.objects.filter(pk=request.data.get('doctor_id')).first()
            if doctor is None:
                return Response({'error': 'Doctor not found.'},
                                status=status.HTTP_404_NOT_FOUND)
            entry.doctor_assigned = doctor
            entry.save(update_fields=['doctor_assigned'])

            entry.patient.attending_doctor = doctor
            entry.patient.save(update_fields=['attending_doctor'])
            PatientTimeline.objects.create(
                patient=entry.patient, event_type='doctor_assigned',
                description=f'Dr. {doctor.name} ({doctor.specialization}) assigned.',
                created_by=request.user.get_full_name() or request.user.username,
            )

        else:
            return Response(
                {'error': 'action must be one of: call, complete, assign_doctor.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        log_activity(request, f'Queue action: {action}', 'PatientQueue', entry.id,
                     entry.patient.name)
        return Response(PatientQueueSerializer(entry).data)


class QueueRecalculateView(APIView):
    """Recalculate every waiting patient's priority score and wait estimate."""
    permission_classes = [IsClinicalStaff]

    @transaction.atomic
    def post(self, request):
        doctors_available = Doctor.objects.filter(status='available').count()
        waiting = list(
            PatientQueue.objects.filter(status='waiting').select_related('patient')
        )

        for entry in waiting:
            entry.priority_score = priority_score(
                entry.patient, waiting_since=entry.waiting_since
            )

        waiting.sort(key=lambda e: (-e.priority_score, e.waiting_since))
        for position, entry in enumerate(waiting, start=1):
            entry.estimated_wait_minutes = estimate_wait_minutes(
                position, doctors_available
            )
            entry.save(update_fields=['priority_score', 'estimated_wait_minutes'])

        log_activity(request, 'Recalculated triage queue', 'PatientQueue')
        return Response({
            'message': f'Recalculated {len(waiting)} queue entries.',
            'results': PatientQueueSerializer(waiting, many=True).data,
        })


# ─── Staff ────────────────────────────────────────────────────────

class DoctorListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        queryset = Doctor.objects.all().order_by('name')
        params = request.query_params
        if params.get('department'):
            queryset = queryset.filter(department=params['department'])
        if params.get('status'):
            queryset = queryset.filter(status__in=params['status'].split(','))
        if params.get('shift'):
            queryset = queryset.filter(shift=params['shift'])
        if params.get('specialization'):
            queryset = queryset.filter(specialization=params['specialization'])
        if params.get('search'):
            queryset = queryset.filter(
                Q(name__icontains=params['search'])
                | Q(specialization__icontains=params['search'])
            )
        return Response(DoctorSerializer(queryset, many=True).data)

    def post(self, request):
        if not IsAdmin().has_permission(request, self):
            return Response({'error': 'Only administrators may add staff.'},
                            status=status.HTTP_403_FORBIDDEN)
        serializer = DoctorSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        doctor = serializer.save()
        log_activity(request, 'Added doctor', 'Doctor', doctor.id, doctor.name)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class DoctorDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def put(self, request, pk):
        doctor = Doctor.objects.filter(pk=pk).first()
        if doctor is None:
            return Response({'error': 'Doctor not found.'},
                            status=status.HTTP_404_NOT_FOUND)

        # Staff may flip their own duty status; only admins edit the record.
        editing_status_only = set(request.data.keys()) <= {'status', 'shift'}
        if not editing_status_only and not IsAdmin().has_permission(request, self):
            return Response({'error': 'Only administrators may edit staff records.'},
                            status=status.HTTP_403_FORBIDDEN)

        serializer = DoctorSerializer(doctor, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        log_activity(request, 'Updated doctor', 'Doctor', doctor.id, doctor.name)
        return Response(serializer.data)


class NurseListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        queryset = Nurse.objects.all().order_by('name')
        params = request.query_params
        if params.get('department'):
            queryset = queryset.filter(department=params['department'])
        if params.get('status'):
            queryset = queryset.filter(status__in=params['status'].split(','))
        if params.get('shift'):
            queryset = queryset.filter(shift=params['shift'])
        if params.get('search'):
            queryset = queryset.filter(name__icontains=params['search'])
        return Response(NurseSerializer(queryset, many=True).data)

    def post(self, request):
        if not IsAdmin().has_permission(request, self):
            return Response({'error': 'Only administrators may add staff.'},
                            status=status.HTTP_403_FORBIDDEN)
        serializer = NurseSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        nurse = serializer.save()
        log_activity(request, 'Added nurse', 'Nurse', nurse.id, nurse.name)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class NurseDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def put(self, request, pk):
        nurse = Nurse.objects.filter(pk=pk).first()
        if nurse is None:
            return Response({'error': 'Nurse not found.'},
                            status=status.HTTP_404_NOT_FOUND)

        editing_status_only = set(request.data.keys()) <= {'status', 'shift'}
        if not editing_status_only and not IsAdmin().has_permission(request, self):
            return Response({'error': 'Only administrators may edit staff records.'},
                            status=status.HTTP_403_FORBIDDEN)

        serializer = NurseSerializer(nurse, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        log_activity(request, 'Updated nurse', 'Nurse', nurse.id, nurse.name)
        return Response(serializer.data)


class StaffOverviewView(APIView):
    """Roster summary: shift coverage, workload and the safety ratio."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        active_patients = Patient.objects.filter(is_discharged=False).count()

        def summarise(model):
            by_shift = {}
            for shift, _label in model.SHIFT_CHOICES:
                members = model.objects.filter(shift=shift)
                by_shift[shift] = {
                    'total': members.count(),
                    'available': members.filter(status='available').count(),
                    'busy': members.filter(status='busy').count(),
                    'off_duty': members.filter(status='off_duty').count(),
                }
            return {
                'total': model.objects.count(),
                'on_duty': model.objects.exclude(status='off_duty').count(),
                'available': model.objects.filter(status='available').count(),
                'by_shift': by_shift,
            }

        doctors = summarise(Doctor)
        nurses = summarise(Nurse)

        by_department = {}
        for row in Doctor.objects.values('department').annotate(n=Count('id')):
            by_department.setdefault(row['department'], {})['doctors'] = row['n']
        for row in Nurse.objects.values('department').annotate(n=Count('id')):
            by_department.setdefault(row['department'], {})['nurses'] = row['n']

        busiest = list(
            Doctor.objects.order_by('-today_patients')
            .values('id', 'name', 'specialization', 'today_patients', 'status')[:5]
        )

        return Response({
            'active_patients': active_patients,
            'doctors': doctors,
            'nurses': nurses,
            'nurse_patient_ratio': round(
                active_patients / nurses['on_duty'], 1
            ) if nurses['on_duty'] else None,
            'doctor_patient_ratio': round(
                active_patients / doctors['on_duty'], 1
            ) if doctors['on_duty'] else None,
            'by_department': [
                {'department': dept, **counts} for dept, counts in by_department.items()
            ],
            'busiest_doctors': busiest,
        })


# ─── Appointments ─────────────────────────────────────────────────

class AppointmentListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        queryset = Appointment.objects.select_related('patient', 'doctor')
        params = request.query_params

        if params.get('status'):
            queryset = queryset.filter(status__in=params['status'].split(','))
        if params.get('doctor'):
            queryset = queryset.filter(doctor_id=params['doctor'])
        if params.get('patient'):
            queryset = queryset.filter(patient_id=params['patient'])
        if params.get('date'):
            queryset = queryset.filter(appointment_date__date=params['date'])
        if params.get('from'):
            queryset = queryset.filter(appointment_date__gte=params['from'])
        if params.get('to'):
            queryset = queryset.filter(appointment_date__lte=params['to'])
        if params.get('search'):
            queryset = queryset.filter(
                Q(patient__name__icontains=params['search'])
                | Q(doctor__name__icontains=params['search'])
                | Q(reason__icontains=params['search'])
            )

        today = timezone.localtime().date()
        return Response({
            'count': queryset.count(),
            'today_count': Appointment.objects.filter(
                appointment_date__date=today
            ).count(),
            'upcoming_count': Appointment.objects.filter(
                status='upcoming', appointment_date__gte=timezone.now()
            ).count(),
            'results': AppointmentSerializer(
                queryset.order_by('appointment_date')[:_page_size(request)], many=True
            ).data,
        })

    def post(self, request):
        if not CanManageAppointments().has_permission(request, self):
            return Response({'error': 'Your role cannot schedule appointments.'},
                            status=status.HTTP_403_FORBIDDEN)

        serializer = AppointmentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        appointment_date = serializer.validated_data['appointment_date']
        doctor = serializer.validated_data['doctor']

        # Reject a double booking: the same doctor cannot have two upcoming
        # appointments within 15 minutes of each other.
        window = timedelta(minutes=15)
        clash = Appointment.objects.filter(
            doctor=doctor, status='upcoming',
            appointment_date__gt=appointment_date - window,
            appointment_date__lt=appointment_date + window,
        ).first()
        if clash:
            return Response(
                {'error': (f'Dr. {doctor.name} already has an appointment at '
                           f'{timezone.localtime(clash.appointment_date).strftime("%H:%M")}. '
                           'Please choose another slot.')},
                status=status.HTTP_409_CONFLICT,
            )

        appointment = serializer.save()
        log_activity(request, 'Scheduled appointment', 'Appointment', appointment.id,
                     f'{appointment.patient.name} with Dr. {doctor.name}')
        return Response(AppointmentSerializer(appointment).data,
                        status=status.HTTP_201_CREATED)


class AppointmentDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def put(self, request, pk):
        appointment = Appointment.objects.select_related(
            'patient', 'doctor'
        ).filter(pk=pk).first()
        if appointment is None:
            return Response({'error': 'Appointment not found.'},
                            status=status.HTTP_404_NOT_FOUND)
        if not CanManageAppointments().has_permission(request, self):
            return Response({'error': 'Your role cannot modify appointments.'},
                            status=status.HTTP_403_FORBIDDEN)

        serializer = AppointmentSerializer(appointment, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        appointment = serializer.save()

        if appointment.status == 'completed':
            PatientTimeline.objects.create(
                patient=appointment.patient, event_type='condition_update',
                description=(f'Consultation with Dr. {appointment.doctor.name} '
                             f'completed. {appointment.notes or ""}').strip(),
                created_by=request.user.get_full_name() or request.user.username,
            )

        log_activity(request, f'Appointment {appointment.status}', 'Appointment',
                     appointment.id, appointment.patient.name)
        return Response(AppointmentSerializer(appointment).data)


# ─── Medicines ────────────────────────────────────────────────────

class MedicineListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        queryset = Medicine.objects.all().order_by('name')
        params = request.query_params

        if params.get('category'):
            queryset = queryset.filter(category__in=params['category'].split(','))
        if params.get('search'):
            queryset = queryset.filter(
                Q(name__icontains=params['search'])
                | Q(supplier__icontains=params['search'])
            )

        # stock_status is a Python property, so this filter is applied after
        # the query rather than in SQL.
        medicines = list(queryset)
        if params.get('stock_status'):
            wanted = set(params['stock_status'].split(','))
            medicines = [m for m in medicines if m.stock_status in wanted]

        counts = {}
        for medicine in Medicine.objects.all():
            counts[medicine.stock_status] = counts.get(medicine.stock_status, 0) + 1

        return Response({
            'count': len(medicines),
            'status_counts': counts,
            'total_stock_value': round(
                sum(m.stock * m.unit_price for m in Medicine.objects.all()), 2
            ),
            'results': MedicineSerializer(medicines, many=True).data,
        })

    def post(self, request):
        if not IsAdmin().has_permission(request, self):
            return Response({'error': 'Only administrators may add medicines.'},
                            status=status.HTTP_403_FORBIDDEN)
        serializer = MedicineSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        medicine = serializer.save()
        log_activity(request, 'Added medicine', 'Medicine', medicine.id, medicine.name)
        return Response(MedicineSerializer(medicine).data,
                        status=status.HTTP_201_CREATED)


class MedicineDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def put(self, request, pk):
        medicine = Medicine.objects.filter(pk=pk).first()
        if medicine is None:
            return Response({'error': 'Medicine not found.'},
                            status=status.HTTP_404_NOT_FOUND)
        if not IsAdmin().has_permission(request, self):
            return Response({'error': 'Only administrators may edit medicines.'},
                            status=status.HTTP_403_FORBIDDEN)

        serializer = MedicineSerializer(medicine, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        log_activity(request, 'Updated medicine', 'Medicine', medicine.id, medicine.name)
        return Response(serializer.data)


class MedicineDispenseView(APIView):
    """Administer a medicine to a patient, decrementing stock."""
    permission_classes = [CanManageMedicines]

    @transaction.atomic
    def post(self, request, pk):
        # select_for_update prevents two simultaneous dispenses from both
        # reading the same stock level and overselling it.
        medicine = Medicine.objects.select_for_update().filter(pk=pk).first()
        if medicine is None:
            return Response({'error': 'Medicine not found.'},
                            status=status.HTTP_404_NOT_FOUND)

        patient = Patient.objects.filter(
            pk=request.data.get('patient_id'), is_discharged=False
        ).first()
        if patient is None:
            return Response({'error': 'Active patient not found.'},
                            status=status.HTTP_404_NOT_FOUND)

        try:
            quantity = int(request.data.get('quantity', 1))
        except (TypeError, ValueError):
            return Response({'error': 'quantity must be a whole number.'},
                            status=status.HTTP_400_BAD_REQUEST)
        if quantity < 1:
            return Response({'error': 'quantity must be at least 1.'},
                            status=status.HTTP_400_BAD_REQUEST)
        if quantity > medicine.stock:
            return Response(
                {'error': f'Only {medicine.stock} units of {medicine.name} '
                          'are in stock.'},
                status=status.HTTP_409_CONFLICT,
            )

        medicine.stock -= quantity
        medicine.save(update_fields=['stock'])

        log_entry = MedicineLog.objects.create(
            medicine=medicine, patient=patient, quantity=quantity,
            administered_by=request.user.get_full_name() or request.user.username,
            notes=request.data.get('notes', ''),
        )
        PatientTimeline.objects.create(
            patient=patient, event_type='medicine_given',
            description=f'{medicine.name} x{quantity} administered.',
            created_by=request.user.get_full_name() or request.user.username,
        )

        if medicine.stock <= medicine.min_stock:
            Alert.objects.create(
                level='critical' if medicine.stock <= 0 else 'warning',
                alert_type='medicine_low',
                message=(f'{medicine.name} is out of stock' if medicine.stock <= 0
                         else f'{medicine.name} has fallen below minimum stock'),
                description=(f'{medicine.stock} units remaining against a minimum '
                             f'of {medicine.min_stock}. Supplier: '
                             f'{medicine.supplier or "not recorded"}.'),
                source='Dispensary',
            )

        log_activity(request, 'Dispensed medicine', 'Medicine', medicine.id,
                     f'{medicine.name} x{quantity} -> {patient.name}')
        return Response({
            'message': f'{medicine.name} x{quantity} administered to {patient.name}.',
            'medicine': MedicineSerializer(medicine).data,
            'log': MedicineLogSerializer(log_entry).data,
        })


class MedicineRestockView(APIView):
    """Add stock after a delivery."""
    permission_classes = [IsAdmin]

    @transaction.atomic
    def post(self, request, pk):
        medicine = Medicine.objects.select_for_update().filter(pk=pk).first()
        if medicine is None:
            return Response({'error': 'Medicine not found.'},
                            status=status.HTTP_404_NOT_FOUND)

        try:
            quantity = int(request.data.get('quantity', 0))
        except (TypeError, ValueError):
            return Response({'error': 'quantity must be a whole number.'},
                            status=status.HTTP_400_BAD_REQUEST)
        if quantity < 1:
            return Response({'error': 'quantity must be at least 1.'},
                            status=status.HTTP_400_BAD_REQUEST)

        previous = medicine.stock
        medicine.stock += quantity
        if request.data.get('expiry_date'):
            medicine.expiry_date = request.data['expiry_date']
        if request.data.get('supplier'):
            medicine.supplier = request.data['supplier']
        medicine.save()

        # Close the low-stock alerts this delivery has actually fixed.
        # The alert engine raises one aggregate alert ("N medicines below
        # minimum") rather than one per medicine, so resolving by name alone
        # would leave that aggregate open forever. Clear it only once nothing
        # is below its minimum; otherwise resolve just the alerts naming this
        # medicine, which may appear in the message or the description.
        still_low = Medicine.objects.filter(stock__lte=F('min_stock')).exists()
        open_alerts = Alert.objects.filter(
            alert_type='medicine_low', is_resolved=False
        )
        if still_low:
            open_alerts = open_alerts.filter(
                Q(message__icontains=medicine.name)
                | Q(description__icontains=medicine.name)
            )
        open_alerts.update(is_resolved=True, resolved_at=timezone.now())

        log_activity(request, 'Restocked medicine', 'Medicine', medicine.id,
                     f'{medicine.name}: {previous} -> {medicine.stock}')
        return Response({
            'message': f'{medicine.name} restocked to {medicine.stock} units.',
            'medicine': MedicineSerializer(medicine).data,
        })


class MedicineLogListView(APIView):
    """Administration ledger — which medicine went to which patient."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        queryset = MedicineLog.objects.select_related(
            'medicine', 'patient'
        ).order_by('-timestamp')

        if request.query_params.get('patient'):
            queryset = queryset.filter(patient_id=request.query_params['patient'])
        if request.query_params.get('medicine'):
            queryset = queryset.filter(medicine_id=request.query_params['medicine'])

        return Response({
            'count': queryset.count(),
            'results': MedicineLogSerializer(
                queryset[:_page_size(request)], many=True
            ).data,
        })


# ─── Alerts ───────────────────────────────────────────────────────

class AlertListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        queryset = Alert.objects.all()
        params = request.query_params

        if params.get('level'):
            queryset = queryset.filter(level__in=params['level'].split(','))
        if params.get('alert_type'):
            queryset = queryset.filter(alert_type__in=params['alert_type'].split(','))
        if params.get('resolved') not in (None, ''):
            queryset = queryset.filter(is_resolved=params['resolved'].lower() == 'true')

        unresolved = Alert.objects.filter(is_resolved=False)
        by_type = {
            row['alert_type']: row['n']
            for row in unresolved.values('alert_type').annotate(n=Count('id'))
        }

        return Response({
            'count': queryset.count(),
            'unresolved_counts': {
                'critical': unresolved.filter(level='critical').count(),
                'warning': unresolved.filter(level='warning').count(),
                'info': unresolved.filter(level='info').count(),
            },
            'by_type': by_type,
            'results': AlertSerializer(
                queryset[:_page_size(request)], many=True
            ).data,
        })

    def post(self, request):
        """Raise an alert manually."""
        valid_levels = {c[0] for c in Alert.LEVEL_CHOICES}
        valid_types = {c[0] for c in Alert.ALERT_TYPES}

        level = request.data.get('level', 'info')
        alert_type = request.data.get('alert_type', 'system')
        message = (request.data.get('message') or '').strip()

        if level not in valid_levels:
            return Response({'error': f'level must be one of: {", ".join(valid_levels)}.'},
                            status=status.HTTP_400_BAD_REQUEST)
        if alert_type not in valid_types:
            return Response(
                {'error': f'alert_type must be one of: {", ".join(sorted(valid_types))}.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not message:
            return Response({'error': 'message is required.'},
                            status=status.HTTP_400_BAD_REQUEST)

        alert = Alert.objects.create(
            level=level, alert_type=alert_type, message=message,
            description=request.data.get('description', ''),
            source=request.user.get_full_name() or request.user.username,
        )
        log_activity(request, 'Raised alert', 'Alert', alert.id, message)
        return Response(AlertSerializer(alert).data, status=status.HTTP_201_CREATED)


class AlertDetailView(APIView):
    """Resolve or reopen a single alert."""
    permission_classes = [CanResolveAlerts]

    def put(self, request, pk):
        alert = Alert.objects.filter(pk=pk).first()
        if alert is None:
            return Response({'error': 'Alert not found.'},
                            status=status.HTTP_404_NOT_FOUND)

        resolve = request.data.get('is_resolved', True)
        alert.is_resolved = bool(resolve)
        alert.resolved_at = timezone.now() if alert.is_resolved else None
        alert.save(update_fields=['is_resolved', 'resolved_at'])

        log_activity(request, 'Resolved alert' if alert.is_resolved else 'Reopened alert',
                     'Alert', alert.id, alert.message[:80])
        return Response(AlertSerializer(alert).data)


class AlertBulkResolveView(APIView):
    """Resolve many alerts at once — by id list, level or type."""
    permission_classes = [CanResolveAlerts]

    def post(self, request):
        queryset = Alert.objects.filter(is_resolved=False)

        if request.data.get('ids'):
            queryset = queryset.filter(id__in=request.data['ids'])
        elif request.data.get('level'):
            queryset = queryset.filter(level=request.data['level'])
        elif request.data.get('alert_type'):
            queryset = queryset.filter(alert_type=request.data['alert_type'])
        elif not request.data.get('all'):
            return Response(
                {'error': 'Provide ids, level, alert_type, or all=true.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        resolved = queryset.update(is_resolved=True, resolved_at=timezone.now())
        log_activity(request, 'Bulk resolved alerts', 'Alert', None,
                     f'{resolved} alerts')
        return Response({'message': f'{resolved} alerts resolved.', 'resolved': resolved})


# ─── Metrics & analytics ──────────────────────────────────────────

class MetricListView(APIView):
    """Time-series metric snapshots for the trend charts."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            limit = max(1, min(500, int(request.query_params.get('hours', 24))))
        except (TypeError, ValueError):
            limit = 24

        metrics = HospitalMetric.objects.order_by('-timestamp')[:limit]
        # Oldest first so charts read left to right.
        return Response(
            HospitalMetricSerializer(list(reversed(metrics)), many=True).data
        )


class DiseaseAnalyticsView(APIView):
    """Disease distribution and patient demographics.

    Every figure here is computed from the database. The earlier version
    injected random numbers when a month had no admissions, which made the
    charts untrustworthy; empty periods now correctly report zero.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        patients = Patient.objects.all()
        active = patients.filter(is_discharged=False)

        top_diseases = list(
            patients.values('disease')
            .annotate(count=Count('id'),
                      avg_severity=Avg('disease_severity'))
            .order_by('-count')[:10]
        )
        for row in top_diseases:
            row['avg_severity'] = round(row['avg_severity'] or 0, 1)

        age_ranges = [
            ('0-18', 0, 18), ('19-35', 19, 35), ('36-50', 36, 50),
            ('51-65', 51, 65), ('65+', 66, 200),
        ]
        age_distribution = [
            {
                'range': label,
                'count': patients.filter(age__gte=low, age__lte=high).count(),
            }
            for label, low, high in age_ranges
        ]

        gender_distribution = list(
            patients.values('gender').annotate(count=Count('id')).order_by('-count')
        )

        risk_distribution = list(
            active.values('risk_level').annotate(count=Count('id'))
        )

        # Last six calendar months, anchored on real month boundaries rather
        # than 30-day approximations.
        monthly = []
        today = timezone.localtime().date()
        for offset in range(5, -1, -1):
            year = today.year
            month = today.month - offset
            while month <= 0:
                month += 12
                year -= 1
            if month == 12:
                next_year, next_month = year + 1, 1
            else:
                next_year, next_month = year, month + 1

            period_start = _month_start(year, month)
            period_end = _month_start(next_year, next_month)

            monthly.append({
                'month': f'{year}-{month:02d}',
                'label': period_start.strftime('%b %Y'),
                'admissions': patients.filter(
                    admitted_at__gte=period_start, admitted_at__lt=period_end
                ).count(),
                'discharges': patients.filter(
                    discharged_at__gte=period_start, discharged_at__lt=period_end
                ).count(),
            })

        ward_occupancy = [
            {
                'ward': ward.name,
                'ward_type': ward.ward_type,
                'floor': ward.floor,
                'occupancy': ward.occupancy_percentage,
                'capacity': ward.capacity,
                'current': ward.current_patients,
                'color': ward.occupancy_color,
            }
            for ward in Ward.objects.order_by('floor', 'name')
        ]

        department_load = list(
            active.values('department').annotate(count=Count('id')).order_by('-count')
        )

        total_recorded = patients.count()
        discharged = patients.filter(is_discharged=True).count()
        icu_total = Bed.objects.filter(ward__ward_type='icu').count()
        icu_occupied = Bed.objects.filter(
            ward__ward_type='icu', status='occupied'
        ).count()

        # Average length of stay over completed admissions only — an ongoing
        # stay has no length yet and would bias the figure downward.
        completed = patients.filter(is_discharged=True, discharged_at__isnull=False)
        stay_hours = [
            (p.discharged_at - p.admitted_at).total_seconds() / 3600
            for p in completed[:500]
        ]
        avg_stay_days = round(sum(stay_hours) / len(stay_hours) / 24, 2) if stay_hours else 0.0

        return Response({
            'top_diseases': top_diseases,
            'age_distribution': age_distribution,
            'gender_distribution': gender_distribution,
            'risk_distribution': risk_distribution,
            'monthly_admissions': monthly,
            'ward_occupancy': ward_occupancy,
            'department_load': department_load,
            'totals': {
                'total_patients': total_recorded,
                'active_patients': active.count(),
                'discharged_patients': discharged,
                'emergency_patients': active.filter(is_emergency=True).count(),
            },
            'discharge_rate': round(
                discharged / total_recorded * 100, 1
            ) if total_recorded else 0.0,
            'avg_length_of_stay_days': avg_stay_days,
            'icu_usage': round(icu_occupied / icu_total * 100, 1) if icu_total else 0.0,
            'avg_disease_severity': round(
                active.aggregate(v=Avg('disease_severity'))['v'] or 0, 1
            ),
        })


class OverviewAnalyticsView(APIView):
    """Metric history plus stored forecasts, for the analytics workspace."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        metrics = HospitalMetric.objects.order_by('-timestamp')[:48]
        return Response({
            'metrics': HospitalMetricSerializer(
                list(reversed(metrics)), many=True
            ).data,
            'predictions': PredictionSerializer(
                Prediction.objects.order_by('date')[:48], many=True
            ).data,
        })


class PredictionListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        queryset = Prediction.objects.all().order_by('date')
        if request.query_params.get('type'):
            queryset = queryset.filter(metric_type=request.query_params['type'])
        return Response(PredictionSerializer(queryset[:96], many=True).data)


class ActivityLogListView(APIView):
    """Audit trail. Admin-only, since it records every user's actions."""
    permission_classes = [IsAdmin]

    def get(self, request):
        queryset = ActivityLog.objects.select_related('user').order_by('-timestamp')

        if request.query_params.get('user'):
            queryset = queryset.filter(user_id=request.query_params['user'])
        if request.query_params.get('action'):
            queryset = queryset.filter(action__icontains=request.query_params['action'])
        if request.query_params.get('target_model'):
            queryset = queryset.filter(target_model=request.query_params['target_model'])

        return Response({
            'count': queryset.count(),
            'results': ActivityLogSerializer(
                queryset[:_page_size(request)], many=True
            ).data,
        })


class UserListView(APIView):
    """Manage login accounts. Admin-only."""
    permission_classes = [IsAdmin]

    def get(self, request):
        users = HospitalUser.objects.all().order_by('username')
        return Response(UserSerializer(users, many=True).data)

    def post(self, request):
        username = (request.data.get('username') or '').strip()
        password = request.data.get('password') or ''
        role = request.data.get('role', 'receptionist')

        if not username or not password:
            return Response({'error': 'Username and password are required.'},
                            status=status.HTTP_400_BAD_REQUEST)
        if len(password) < 8:
            return Response({'error': 'Password must be at least 8 characters.'},
                            status=status.HTTP_400_BAD_REQUEST)
        if HospitalUser.objects.filter(username=username).exists():
            return Response({'error': f'Username "{username}" is already taken.'},
                            status=status.HTTP_409_CONFLICT)
        if role not in {c[0] for c in HospitalUser.ROLE_CHOICES}:
            return Response({'error': 'Invalid role.'},
                            status=status.HTTP_400_BAD_REQUEST)

        user = HospitalUser.objects.create_user(
            username=username, password=password, role=role,
            email=request.data.get('email', ''),
            first_name=request.data.get('first_name', ''),
            last_name=request.data.get('last_name', ''),
            phone=request.data.get('phone', ''),
            department=request.data.get('department', ''),
        )
        log_activity(request, 'Created user account', 'HospitalUser', user.id,
                     f'{username} ({role})')
        return Response(UserSerializer(user).data, status=status.HTTP_201_CREATED)


# ─── Simulation ───────────────────────────────────────────────────

class SimulationTickView(APIView):
    """Run one simulation step and the alert engine."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        return Response(simulation_tick())


# ─── Reports ──────────────────────────────────────────────────────

class ReportCatalogueView(APIView):
    """The four available reports and their formats."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({'reports': report_engine.available_reports()})


class ReportGenerateView(APIView):
    """Generate a PDF or CSV report as a file download."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        report_type = request.data.get('type', 'hospital_summary')
        report_format = (request.data.get('format') or 'pdf').lower()

        if report_format not in ('pdf', 'csv'):
            return Response({'error': 'format must be "pdf" or "csv".'},
                            status=status.HTTP_400_BAD_REQUEST)

        definition = report_engine.resolve_report(report_type)
        if definition is None:
            return Response(
                {'error': f'Unknown report type "{report_type}".',
                 'available': list(report_engine.REPORTS)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        response = report_engine.generate_report(report_type, report_format)
        log_activity(request, 'Generated report', 'Report', None,
                     f'{definition.key} ({report_format})')
        return response


# ─── Chat assistant ───────────────────────────────────────────────

class ChatAssistantView(APIView):
    """Rule-based operational assistant."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({'suggestions': suggested_questions()})

    def post(self, request):
        message = (request.data.get('message') or '').strip()
        if not message:
            return Response({'error': 'A message is required.'},
                            status=status.HTTP_400_BAD_REQUEST)

        result = chatbot_answer(message)
        return Response({
            'query': message,
            'timestamp': timezone.now().isoformat(),
            **result,
        })


# ─── Power BI ─────────────────────────────────────────────────────

class PowerBIConfigView(APIView):
    """Serve the Power BI embed configuration to the frontend.

    The report URL lives in Django settings rather than in the React bundle:
    embed credentials must not be shipped to the browser as source, and an
    administrator can repoint the dashboard without a frontend rebuild.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from django.conf import settings

        config = getattr(settings, 'POWERBI_EMBED', {})
        return Response({
            'configured': bool(config.get('report_url')),
            'report_url': config.get('report_url', ''),
            'report_name': config.get('report_name', 'Hospital Analytics'),
            'pages': config.get('pages', []),
            'setup_hint': config.get('setup_hint', ''),
        })
