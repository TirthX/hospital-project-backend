"""
DRF serializers.

A few notes on deliberate choices:

  * Patient has two serializers. The list view omits the heavy fields so a
    100-row table is not carrying full addresses and notes.
  * Computed model properties (occupancy, stock status) are exposed as
    read-only fields so the frontend never recomputes business rules.
  * Vitals and severity are range-validated here rather than only in the UI,
    because an API client can post anything.
"""
from rest_framework import serializers

from .models import (
    ActivityLog, Alert, Appointment, Bed, Doctor, HospitalMetric, HospitalUser,
    Medicine, MedicineLog, Nurse, Patient, PatientQueue, PatientTimeline,
    Prediction, Ward,
)


class UserSerializer(serializers.ModelSerializer):
    role_display = serializers.CharField(source='get_role_display', read_only=True)
    full_name = serializers.SerializerMethodField()

    class Meta:
        model = HospitalUser
        fields = ['id', 'username', 'email', 'role', 'role_display',
                  'first_name', 'last_name', 'full_name', 'department',
                  'phone', 'avatar_color', 'last_login', 'is_active']
        read_only_fields = ['id', 'username', 'role', 'last_login', 'is_active']

    def get_full_name(self, obj):
        return obj.get_full_name() or obj.username


class WardSerializer(serializers.ModelSerializer):
    ward_type_display = serializers.CharField(
        source='get_ward_type_display', read_only=True
    )
    current_patients = serializers.ReadOnlyField()
    occupancy_percentage = serializers.ReadOnlyField()
    occupancy_color = serializers.ReadOnlyField()
    available_beds = serializers.SerializerMethodField()
    total_beds = serializers.SerializerMethodField()

    class Meta:
        model = Ward
        fields = ['id', 'name', 'ward_type', 'ward_type_display', 'floor',
                  'capacity', 'description', 'current_patients',
                  'occupancy_percentage', 'occupancy_color',
                  'available_beds', 'total_beds']

    def get_available_beds(self, obj):
        return obj.beds.filter(status='available').count()

    def get_total_beds(self, obj):
        return obj.beds.count()


class BedSerializer(serializers.ModelSerializer):
    ward_name = serializers.CharField(source='ward.name', read_only=True)
    ward_type = serializers.CharField(source='ward.ward_type', read_only=True)
    ward_floor = serializers.IntegerField(source='ward.floor', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    patient_name = serializers.CharField(source='patient.name', read_only=True, default=None)
    patient_age = serializers.IntegerField(source='patient.age', read_only=True, default=None)
    patient_disease = serializers.CharField(source='patient.disease', read_only=True, default=None)
    patient_risk_level = serializers.CharField(
        source='patient.risk_level', read_only=True, default=None
    )
    patient_is_emergency = serializers.BooleanField(
        source='patient.is_emergency', read_only=True, default=None
    )
    patient_admission_date = serializers.DateTimeField(
        source='patient.admitted_at', read_only=True, default=None
    )
    patient_expected_discharge = serializers.DateTimeField(
        source='patient.expected_discharge', read_only=True, default=None
    )

    class Meta:
        model = Bed
        fields = ['id', 'bed_number', 'ward', 'ward_name', 'ward_type', 'ward_floor',
                  'status', 'status_display', 'patient', 'patient_name', 'patient_age',
                  'patient_disease', 'patient_risk_level', 'patient_is_emergency',
                  'patient_admission_date', 'patient_expected_discharge']


class _PatientVitalValidationMixin:
    """Range checks for clinical fields, shared by both patient serializers.

    Values outside these bands are data-entry errors rather than real
    observations, and letting them through would corrupt the risk score.
    """

    VITAL_RANGES = {
        'age': (0, 120, 'years'),
        'heart_rate': (20, 250, 'BPM'),
        'temperature': (85.0, 115.0, 'deg F'),
        'blood_pressure_systolic': (50, 260, 'mmHg'),
        'blood_pressure_diastolic': (30, 180, 'mmHg'),
        'oxygen_saturation': (30.0, 100.0, '%'),
        'disease_severity': (1, 10, 'severity points'),
    }

    def validate(self, attrs):
        errors = {}
        for field, (low, high, unit) in self.VITAL_RANGES.items():
            value = attrs.get(field)
            if value is None:
                continue
            if not (low <= value <= high):
                errors[field] = (
                    f'Must be between {low} and {high} {unit}. Received {value}.'
                )

        systolic = attrs.get('blood_pressure_systolic')
        diastolic = attrs.get('blood_pressure_diastolic')
        if systolic is not None and diastolic is not None and diastolic >= systolic:
            errors['blood_pressure_diastolic'] = (
                'Diastolic pressure must be lower than systolic pressure.'
            )

        if errors:
            raise serializers.ValidationError(errors)
        return attrs


class PatientListSerializer(serializers.ModelSerializer):
    attending_doctor_name = serializers.CharField(
        source='attending_doctor.name', read_only=True, default=None
    )
    bed_number = serializers.CharField(
        source='assigned_bed.bed_number', read_only=True, default=None
    )
    ward_name = serializers.CharField(
        source='assigned_bed.ward.name', read_only=True, default=None
    )
    risk_level_display = serializers.CharField(
        source='get_risk_level_display', read_only=True
    )

    class Meta:
        model = Patient
        fields = ['id', 'name', 'age', 'gender', 'phone', 'disease',
                  'disease_severity', 'condition', 'risk_level',
                  'risk_level_display', 'risk_confidence', 'is_emergency',
                  'is_discharged', 'department', 'admitted_at', 'discharged_at',
                  'attending_doctor', 'attending_doctor_name',
                  'bed_number', 'ward_name',
                  'heart_rate', 'temperature', 'blood_pressure_systolic',
                  'blood_pressure_diastolic', 'oxygen_saturation']


class PatientDetailSerializer(_PatientVitalValidationMixin, serializers.ModelSerializer):
    attending_doctor_name = serializers.CharField(
        source='attending_doctor.name', read_only=True, default=None
    )
    attending_doctor_specialization = serializers.CharField(
        source='attending_doctor.specialization', read_only=True, default=None
    )
    risk_level_display = serializers.CharField(
        source='get_risk_level_display', read_only=True
    )
    assigned_bed_info = serializers.SerializerMethodField()
    days_admitted = serializers.SerializerMethodField()

    class Meta:
        model = Patient
        fields = '__all__'
        # Risk is derived by the clinical engine, never accepted from a
        # client — otherwise a caller could mark a critical patient as low.
        read_only_fields = ['risk_level', 'risk_confidence', 'admitted_at']

    def get_assigned_bed_info(self, obj):
        bed = getattr(obj, 'assigned_bed', None)
        if bed is None:
            return None
        return {
            'id': bed.id,
            'bed_number': bed.bed_number,
            'ward': bed.ward.name,
            'ward_type': bed.ward.ward_type,
            'floor': bed.ward.floor,
        }

    def get_days_admitted(self, obj):
        from django.utils import timezone
        end = obj.discharged_at or timezone.now()
        return round((end - obj.admitted_at).total_seconds() / 86400, 1)


class DoctorSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    shift_display = serializers.CharField(source='get_shift_display', read_only=True)
    active_patients = serializers.SerializerMethodField()

    class Meta:
        model = Doctor
        fields = '__all__'

    def get_active_patients(self, obj):
        return obj.patients.filter(is_discharged=False).count()


class NurseSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    shift_display = serializers.CharField(source='get_shift_display', read_only=True)

    class Meta:
        model = Nurse
        fields = '__all__'


class AppointmentSerializer(serializers.ModelSerializer):
    patient_name = serializers.CharField(source='patient.name', read_only=True)
    patient_phone = serializers.CharField(source='patient.phone', read_only=True)
    doctor_name = serializers.CharField(source='doctor.name', read_only=True)
    doctor_specialization = serializers.CharField(
        source='doctor.specialization', read_only=True
    )
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = Appointment
        fields = ['id', 'patient', 'patient_name', 'patient_phone',
                  'doctor', 'doctor_name', 'doctor_specialization',
                  'appointment_date', 'status', 'status_display',
                  'reason', 'notes', 'created_at']


class MedicineSerializer(serializers.ModelSerializer):
    stock_status = serializers.ReadOnlyField()
    category_display = serializers.CharField(
        source='get_category_display', read_only=True
    )
    days_to_expiry = serializers.SerializerMethodField()
    stock_value = serializers.SerializerMethodField()

    class Meta:
        model = Medicine
        fields = ['id', 'name', 'category', 'category_display', 'stock',
                  'min_stock', 'expiry_date', 'days_to_expiry', 'supplier',
                  'unit_price', 'stock_value', 'description', 'stock_status']

    def get_days_to_expiry(self, obj):
        from django.utils import timezone
        return (obj.expiry_date - timezone.localtime().date()).days

    def get_stock_value(self, obj):
        return round(obj.stock * obj.unit_price, 2)

    def validate_stock(self, value):
        if value < 0:
            raise serializers.ValidationError('Stock cannot be negative.')
        return value

    def validate_min_stock(self, value):
        if value < 0:
            raise serializers.ValidationError('Minimum stock cannot be negative.')
        return value


class MedicineLogSerializer(serializers.ModelSerializer):
    medicine_name = serializers.CharField(source='medicine.name', read_only=True)
    medicine_category = serializers.CharField(
        source='medicine.category', read_only=True
    )
    patient_name = serializers.CharField(source='patient.name', read_only=True)

    class Meta:
        model = MedicineLog
        fields = ['id', 'medicine', 'medicine_name', 'medicine_category',
                  'patient', 'patient_name', 'quantity', 'administered_by',
                  'timestamp', 'notes']


class PatientTimelineSerializer(serializers.ModelSerializer):
    event_type_display = serializers.CharField(
        source='get_event_type_display', read_only=True
    )

    class Meta:
        model = PatientTimeline
        fields = ['id', 'patient', 'event_type', 'event_type_display',
                  'description', 'timestamp', 'created_by']


class PatientQueueSerializer(serializers.ModelSerializer):
    patient_name = serializers.CharField(source='patient.name', read_only=True)
    patient_age = serializers.IntegerField(source='patient.age', read_only=True)
    patient_gender = serializers.CharField(source='patient.gender', read_only=True)
    patient_disease = serializers.CharField(source='patient.disease', read_only=True)
    patient_severity = serializers.IntegerField(
        source='patient.disease_severity', read_only=True
    )
    patient_risk_level = serializers.CharField(
        source='patient.risk_level', read_only=True
    )
    patient_is_emergency = serializers.BooleanField(
        source='patient.is_emergency', read_only=True
    )
    patient_oxygen = serializers.FloatField(
        source='patient.oxygen_saturation', read_only=True
    )
    patient_heart_rate = serializers.IntegerField(
        source='patient.heart_rate', read_only=True
    )
    doctor_name = serializers.CharField(
        source='doctor_assigned.name', read_only=True, default=None
    )
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    waited_minutes = serializers.SerializerMethodField()

    class Meta:
        model = PatientQueue
        fields = ['id', 'patient', 'patient_name', 'patient_age', 'patient_gender',
                  'patient_disease', 'patient_severity', 'patient_risk_level',
                  'patient_is_emergency', 'patient_oxygen', 'patient_heart_rate',
                  'priority_score', 'waiting_since', 'waited_minutes',
                  'doctor_assigned', 'doctor_name', 'estimated_wait_minutes',
                  'estimated_consultation_minutes', 'status', 'status_display',
                  'department']

    def get_waited_minutes(self, obj):
        from django.utils import timezone
        return int((timezone.now() - obj.waiting_since).total_seconds() / 60)


class AlertSerializer(serializers.ModelSerializer):
    level_display = serializers.CharField(source='get_level_display', read_only=True)
    alert_type_display = serializers.CharField(
        source='get_alert_type_display', read_only=True
    )
    age_minutes = serializers.SerializerMethodField()

    class Meta:
        model = Alert
        fields = ['id', 'timestamp', 'level', 'level_display', 'alert_type',
                  'alert_type_display', 'message', 'description', 'source',
                  'is_resolved', 'resolved_at', 'age_minutes']

    def get_age_minutes(self, obj):
        from django.utils import timezone
        return int((timezone.now() - obj.timestamp).total_seconds() / 60)


class HospitalMetricSerializer(serializers.ModelSerializer):
    class Meta:
        model = HospitalMetric
        fields = '__all__'


class PredictionSerializer(serializers.ModelSerializer):
    metric_type_display = serializers.CharField(
        source='get_metric_type_display', read_only=True
    )

    class Meta:
        model = Prediction
        fields = ['id', 'date', 'metric_type', 'metric_type_display',
                  'predicted_value', 'confidence', 'trend',
                  'recommendation', 'model_version', 'created_at']


class ActivityLogSerializer(serializers.ModelSerializer):
    username = serializers.SerializerMethodField()
    user_role = serializers.CharField(source='user.role', read_only=True, default=None)

    class Meta:
        model = ActivityLog
        fields = ['id', 'user', 'username', 'user_role', 'action',
                  'target_model', 'target_id', 'details', 'timestamp',
                  'ip_address']

    def get_username(self, obj):
        if obj.user is None:
            return 'System'
        return obj.user.get_full_name() or obj.user.username
