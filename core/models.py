from django.db import models
from django.contrib.auth.models import AbstractUser
from django.utils import timezone


class HospitalUser(AbstractUser):
    ROLE_CHOICES = (
        ('admin', 'Admin'),
        ('doctor', 'Doctor'),
        ('nurse', 'Nurse'),
        ('receptionist', 'Receptionist'),
    )
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='receptionist')
    phone = models.CharField(max_length=15, blank=True, default='')
    department = models.CharField(max_length=50, blank=True, default='')
    avatar_color = models.CharField(max_length=7, default='#2c9ed6')

    def __str__(self):
        return f"{self.username} ({self.get_role_display()})"


class Ward(models.Model):
    WARD_TYPES = (
        ('emergency', 'Emergency Ward'),
        ('general', 'General Ward'),
        ('icu', 'Intensive Care Unit'),
        ('ot', 'Operation Theatre'),
        ('reception', 'Reception'),
        ('laboratory', 'Laboratory'),
    )
    name = models.CharField(max_length=100)
    ward_type = models.CharField(max_length=20, choices=WARD_TYPES)
    floor = models.IntegerField(default=1)
    capacity = models.IntegerField(default=20)
    description = models.TextField(blank=True, default='')

    @property
    def current_patients(self):
        return self.beds.filter(status='occupied').count()

    @property
    def occupancy_percentage(self):
        if self.capacity == 0:
            return 0
        return round((self.current_patients / self.capacity) * 100, 1)

    @property
    def occupancy_color(self):
        pct = self.occupancy_percentage
        if pct >= 85:
            return 'red'
        elif pct >= 60:
            return 'yellow'
        return 'green'

    def __str__(self):
        return f"{self.name} ({self.get_ward_type_display()})"


class Bed(models.Model):
    STATUS_CHOICES = (
        ('available', 'Available'),
        ('occupied', 'Occupied'),
        ('maintenance', 'Under Maintenance'),
        ('reserved', 'Reserved'),
    )
    bed_number = models.CharField(max_length=20, unique=True)
    ward = models.ForeignKey(Ward, on_delete=models.CASCADE, related_name='beds')
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='available')
    patient = models.OneToOneField(
        'Patient', on_delete=models.SET_NULL, null=True, blank=True, related_name='assigned_bed'
    )

    def __str__(self):
        return f"Bed {self.bed_number} - {self.ward.name}"


class Patient(models.Model):
    GENDER_CHOICES = (
        ('Male', 'Male'),
        ('Female', 'Female'),
        ('Other', 'Other'),
    )
    RISK_LEVELS = (
        ('low', 'Low Risk'),
        ('medium', 'Medium Risk'),
        ('high', 'High Risk'),
        ('critical', 'Critical'),
    )
    DISEASE_CHOICES = (
        ('Cardiac Arrest', 'Cardiac Arrest'),
        ('Pneumonia', 'Pneumonia'),
        ('Diabetes', 'Diabetes'),
        ('Fracture', 'Fracture'),
        ('Dengue', 'Dengue'),
        ('Malaria', 'Malaria'),
        ('COVID-19', 'COVID-19'),
        ('Typhoid', 'Typhoid'),
        ('Asthma', 'Asthma'),
        ('Hypertension', 'Hypertension'),
        ('Stroke', 'Stroke'),
        ('Appendicitis', 'Appendicitis'),
        ('Kidney Stone', 'Kidney Stone'),
        ('Liver Cirrhosis', 'Liver Cirrhosis'),
        ('Tuberculosis', 'Tuberculosis'),
        ('Other', 'Other'),
    )

    name = models.CharField(max_length=100)
    age = models.IntegerField()
    gender = models.CharField(max_length=10, choices=GENDER_CHOICES)
    phone = models.CharField(max_length=15, blank=True, default='')
    address = models.TextField(blank=True, default='')

    # Clinical
    disease = models.CharField(max_length=50, choices=DISEASE_CHOICES, default='Other')
    disease_severity = models.IntegerField(default=3, help_text='1-10 scale')
    condition = models.TextField(blank=True, default='Stable')

    # Vitals
    heart_rate = models.IntegerField(default=75, help_text='BPM')
    temperature = models.FloatField(default=98.6, help_text='Fahrenheit')
    blood_pressure_systolic = models.IntegerField(default=120)
    blood_pressure_diastolic = models.IntegerField(default=80)
    oxygen_saturation = models.FloatField(default=98.0, help_text='SpO2 %')

    # Status
    risk_level = models.CharField(max_length=10, choices=RISK_LEVELS, default='low')
    risk_confidence = models.FloatField(default=0.0, help_text='0-100 confidence %')
    is_emergency = models.BooleanField(default=False)
    is_discharged = models.BooleanField(default=False)

    # Timestamps
    admitted_at = models.DateTimeField(auto_now_add=True)
    discharged_at = models.DateTimeField(null=True, blank=True)
    expected_discharge = models.DateTimeField(null=True, blank=True)

    # Relations
    department = models.CharField(max_length=50, default='General')
    attending_doctor = models.ForeignKey(
        'Doctor', on_delete=models.SET_NULL, null=True, blank=True, related_name='patients'
    )

    class Meta:
        ordering = ['-admitted_at']

    def __str__(self):
        return f"{self.name} (Age: {self.age})"


class Doctor(models.Model):
    STATUS_CHOICES = (
        ('available', 'Available'),
        ('busy', 'Busy'),
        ('off_duty', 'Off Duty'),
    )
    SHIFT_CHOICES = (
        ('morning', 'Morning (6 AM - 2 PM)'),
        ('afternoon', 'Afternoon (2 PM - 10 PM)'),
        ('night', 'Night (10 PM - 6 AM)'),
    )
    SPECIALIZATION_CHOICES = (
        ('Cardiology', 'Cardiology'),
        ('Neurology', 'Neurology'),
        ('Orthopedics', 'Orthopedics'),
        ('Pediatrics', 'Pediatrics'),
        ('General Medicine', 'General Medicine'),
        ('Surgery', 'Surgery'),
        ('Emergency Medicine', 'Emergency Medicine'),
        ('Pulmonology', 'Pulmonology'),
        ('Gastroenterology', 'Gastroenterology'),
        ('Dermatology', 'Dermatology'),
    )

    user = models.OneToOneField(HospitalUser, on_delete=models.CASCADE, null=True, blank=True)
    name = models.CharField(max_length=100)
    specialization = models.CharField(max_length=50, choices=SPECIALIZATION_CHOICES, default='General Medicine')
    department = models.CharField(max_length=50, default='General')
    phone = models.CharField(max_length=15, blank=True, default='')
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='available')
    shift = models.CharField(max_length=15, choices=SHIFT_CHOICES, default='morning')
    today_patients = models.IntegerField(default=0)
    experience_years = models.IntegerField(default=5)

    def __str__(self):
        return f"Dr. {self.name} ({self.specialization})"


class Nurse(models.Model):
    STATUS_CHOICES = (
        ('available', 'Available'),
        ('busy', 'Busy'),
        ('off_duty', 'Off Duty'),
    )
    SHIFT_CHOICES = (
        ('morning', 'Morning (6 AM - 2 PM)'),
        ('afternoon', 'Afternoon (2 PM - 10 PM)'),
        ('night', 'Night (10 PM - 6 AM)'),
    )

    user = models.OneToOneField(HospitalUser, on_delete=models.CASCADE, null=True, blank=True)
    name = models.CharField(max_length=100)
    department = models.CharField(max_length=50, default='General')
    phone = models.CharField(max_length=15, blank=True, default='')
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='available')
    shift = models.CharField(max_length=15, choices=SHIFT_CHOICES, default='morning')
    today_patients = models.IntegerField(default=0)

    def __str__(self):
        return f"Nurse {self.name} ({self.department})"


class Appointment(models.Model):
    STATUS_CHOICES = (
        ('upcoming', 'Upcoming'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
        ('missed', 'Missed'),
    )

    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name='appointments')
    doctor = models.ForeignKey(Doctor, on_delete=models.CASCADE, related_name='appointments')
    appointment_date = models.DateTimeField()
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='upcoming')
    reason = models.CharField(max_length=200, blank=True, default='')
    notes = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-appointment_date']

    def __str__(self):
        return f"{self.patient.name} → Dr. {self.doctor.name} ({self.status})"


class Medicine(models.Model):
    CATEGORY_CHOICES = (
        ('antibiotic', 'Antibiotic'),
        ('painkiller', 'Painkiller'),
        ('antiviral', 'Antiviral'),
        ('cardiac', 'Cardiac'),
        ('respiratory', 'Respiratory'),
        ('gastrointestinal', 'Gastrointestinal'),
        ('supplement', 'Supplement'),
        ('anesthetic', 'Anesthetic'),
        ('antiseptic', 'Antiseptic'),
        ('other', 'Other'),
    )

    name = models.CharField(max_length=100)
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default='other')
    stock = models.IntegerField(default=100)
    min_stock = models.IntegerField(default=20)
    expiry_date = models.DateField()
    supplier = models.CharField(max_length=100, blank=True, default='')
    unit_price = models.FloatField(default=0.0)
    description = models.TextField(blank=True, default='')

    @property
    def stock_status(self):
        if self.stock <= 0:
            return 'out_of_stock'
        if self.stock <= self.min_stock:
            return 'low_stock'
        if self.expiry_date <= timezone.now().date():
            return 'expired'
        if (self.expiry_date - timezone.now().date()).days <= 30:
            return 'near_expiry'
        return 'adequate'

    def __str__(self):
        return f"{self.name} (Stock: {self.stock})"


class MedicineLog(models.Model):
    medicine = models.ForeignKey(Medicine, on_delete=models.CASCADE, related_name='logs')
    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name='medicine_logs')
    quantity = models.IntegerField(default=1)
    administered_by = models.CharField(max_length=100, blank=True, default='')
    timestamp = models.DateTimeField(auto_now_add=True)
    notes = models.TextField(blank=True, default='')

    def __str__(self):
        return f"{self.medicine.name} → {self.patient.name}"


class PatientTimeline(models.Model):
    EVENT_TYPES = (
        ('registered', 'Registered'),
        ('vitals_recorded', 'Vitals Recorded'),
        ('doctor_assigned', 'Doctor Assigned'),
        ('lab_test', 'Lab Test Ordered'),
        ('lab_result', 'Lab Results Received'),
        ('medicine_given', 'Medicine Administered'),
        ('surgery', 'Surgery Performed'),
        ('shifted_icu', 'Shifted to ICU'),
        ('shifted_ward', 'Shifted to Ward'),
        ('condition_update', 'Condition Updated'),
        ('recovered', 'Recovered'),
        ('discharged', 'Discharged'),
    )

    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name='timeline_events')
    event_type = models.CharField(max_length=20, choices=EVENT_TYPES)
    description = models.TextField()
    timestamp = models.DateTimeField(auto_now_add=True)
    created_by = models.CharField(max_length=100, blank=True, default='System')

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f"{self.patient.name}: {self.get_event_type_display()}"


class PatientQueue(models.Model):
    STATUS_CHOICES = (
        ('waiting', 'Waiting'),
        ('in_consultation', 'In Consultation'),
        ('completed', 'Completed'),
    )

    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name='queue_entries')
    priority_score = models.FloatField(default=50.0, help_text='0-100, higher = more urgent')
    waiting_since = models.DateTimeField(auto_now_add=True)
    doctor_assigned = models.ForeignKey(
        Doctor, on_delete=models.SET_NULL, null=True, blank=True, related_name='queue_patients'
    )
    estimated_wait_minutes = models.IntegerField(default=30)
    estimated_consultation_minutes = models.IntegerField(default=15)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='waiting')
    department = models.CharField(max_length=50, default='General')

    class Meta:
        ordering = ['-priority_score', 'waiting_since']

    def __str__(self):
        return f"{self.patient.name} (Priority: {self.priority_score})"


class Alert(models.Model):
    LEVEL_CHOICES = (
        ('info', 'Information'),
        ('warning', 'Warning'),
        ('critical', 'Critical'),
    )
    ALERT_TYPES = (
        ('icu_high', 'High ICU Occupancy'),
        ('emergency_surge', 'Emergency Surge'),
        ('medicine_low', 'Medicine Low Stock'),
        ('staff_shortage', 'Staff Shortage'),
        ('patient_critical', 'Patient Critical'),
        ('long_wait', 'Long Waiting Time'),
        ('bed_shortage', 'Bed Shortage'),
        ('system', 'System Alert'),
    )

    timestamp = models.DateTimeField(auto_now_add=True)
    level = models.CharField(max_length=20, choices=LEVEL_CHOICES, default='info')
    alert_type = models.CharField(max_length=20, choices=ALERT_TYPES, default='system')
    message = models.TextField()
    description = models.TextField(blank=True, default='')
    source = models.CharField(max_length=50, blank=True, default='System')
    is_resolved = models.BooleanField(default=False)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f"[{self.level.upper()}] {self.message[:50]}"


class HospitalMetric(models.Model):
    timestamp = models.DateTimeField(auto_now_add=True)
    total_patients = models.IntegerField(default=0)
    new_admissions = models.IntegerField(default=0)
    discharges = models.IntegerField(default=0)
    icu_occupancy = models.IntegerField(default=0)
    general_occupancy = models.IntegerField(default=0)
    emergency_patients = models.IntegerField(default=0)
    available_beds = models.IntegerField(default=0)
    avg_wait_time = models.FloatField(default=0.0)
    patient_inflow = models.IntegerField(default=0)
    doctors_on_duty = models.IntegerField(default=0)
    nurses_on_duty = models.IntegerField(default=0)
    staff_count = models.IntegerField(default=0)
    critical_alerts = models.IntegerField(default=0)

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f"Metrics at {self.timestamp}"


class Prediction(models.Model):
    METRIC_TYPES = (
        ('admissions', 'Admissions Forecast'),
        ('icu_occupancy', 'ICU Occupancy'),
        ('bed_availability', 'Bed Availability'),
        ('doctor_requirement', 'Doctor Requirement'),
        ('nurse_requirement', 'Nurse Requirement'),
        ('medicine_demand', 'Medicine Demand'),
    )

    date = models.DateField()
    metric_type = models.CharField(max_length=20, choices=METRIC_TYPES)
    predicted_value = models.FloatField()
    confidence = models.FloatField(default=85.0, help_text='Confidence percentage')
    trend = models.CharField(max_length=20, blank=True, default='stable')
    recommendation = models.TextField(blank=True, default='')
    model_version = models.CharField(max_length=20, default='v2.0')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['date']

    def __str__(self):
        return f"{self.get_metric_type_display()} on {self.date}: {self.predicted_value}"


class ActivityLog(models.Model):
    user = models.ForeignKey(HospitalUser, on_delete=models.SET_NULL, null=True, blank=True)
    action = models.CharField(max_length=100)
    target_model = models.CharField(max_length=50, blank=True, default='')
    target_id = models.IntegerField(null=True, blank=True)
    details = models.TextField(blank=True, default='')
    timestamp = models.DateTimeField(auto_now_add=True)
    ip_address = models.CharField(max_length=45, blank=True, default='')

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f"{self.action} at {self.timestamp}"
