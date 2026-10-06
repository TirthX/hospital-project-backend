from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import (
    HospitalUser, Ward, Bed, Patient, Doctor, Nurse,
    Appointment, Medicine, MedicineLog, PatientTimeline,
    PatientQueue, Alert, HospitalMetric, Prediction, ActivityLog
)


class HospitalUserAdmin(UserAdmin):
    model = HospitalUser
    list_display = ['username', 'email', 'role', 'department', 'is_staff']
    list_filter = ['role', 'department', 'is_staff']
    search_fields = ['username', 'email']
    fieldsets = UserAdmin.fieldsets + (
        ('Hospital Info', {'fields': ('role', 'phone', 'department', 'avatar_color')}),
    )
    add_fieldsets = UserAdmin.add_fieldsets + (
        ('Hospital Info', {'fields': ('role', 'phone', 'department')}),
    )


class WardAdmin(admin.ModelAdmin):
    list_display = ['name', 'ward_type', 'floor', 'capacity', 'current_patients', 'occupancy_percentage']
    list_filter = ['ward_type', 'floor']
    search_fields = ['name']


class BedAdmin(admin.ModelAdmin):
    list_display = ['bed_number', 'ward', 'status', 'patient']
    list_filter = ['status', 'ward']
    search_fields = ['bed_number']


class PatientAdmin(admin.ModelAdmin):
    list_display = ['name', 'age', 'gender', 'disease', 'risk_level', 'is_emergency', 'is_discharged', 'department']
    list_filter = ['risk_level', 'is_emergency', 'is_discharged', 'disease', 'department', 'gender']
    search_fields = ['name', 'phone']
    readonly_fields = ['admitted_at']


class DoctorAdmin(admin.ModelAdmin):
    list_display = ['name', 'specialization', 'department', 'status', 'shift', 'today_patients']
    list_filter = ['specialization', 'status', 'shift', 'department']
    search_fields = ['name']


class NurseAdmin(admin.ModelAdmin):
    list_display = ['name', 'department', 'status', 'shift', 'today_patients']
    list_filter = ['status', 'shift', 'department']
    search_fields = ['name']


class AppointmentAdmin(admin.ModelAdmin):
    list_display = ['patient', 'doctor', 'appointment_date', 'status', 'reason']
    list_filter = ['status', 'doctor']
    search_fields = ['patient__name', 'doctor__name']


class MedicineAdmin(admin.ModelAdmin):
    list_display = ['name', 'category', 'stock', 'min_stock', 'expiry_date', 'supplier']
    list_filter = ['category']
    search_fields = ['name', 'supplier']


class MedicineLogAdmin(admin.ModelAdmin):
    list_display = ['medicine', 'patient', 'quantity', 'timestamp']
    list_filter = ['timestamp']
    search_fields = ['medicine__name', 'patient__name']


class PatientTimelineAdmin(admin.ModelAdmin):
    list_display = ['patient', 'event_type', 'timestamp', 'created_by']
    list_filter = ['event_type']
    search_fields = ['patient__name']


class PatientQueueAdmin(admin.ModelAdmin):
    list_display = ['patient', 'priority_score', 'waiting_since', 'doctor_assigned', 'status']
    list_filter = ['status', 'department']
    search_fields = ['patient__name']


class AlertAdmin(admin.ModelAdmin):
    list_display = ['level', 'alert_type', 'message', 'timestamp', 'is_resolved']
    list_filter = ['level', 'alert_type', 'is_resolved']
    search_fields = ['message']


class HospitalMetricAdmin(admin.ModelAdmin):
    list_display = ['timestamp', 'total_patients', 'icu_occupancy', 'emergency_patients', 'available_beds']
    list_filter = ['timestamp']


class PredictionAdmin(admin.ModelAdmin):
    list_display = ['metric_type', 'date', 'predicted_value', 'confidence', 'trend']
    list_filter = ['metric_type', 'date']


class ActivityLogAdmin(admin.ModelAdmin):
    list_display = ['action', 'user', 'target_model', 'timestamp']
    list_filter = ['action', 'target_model']
    search_fields = ['action', 'details']


admin.site.register(HospitalUser, HospitalUserAdmin)
admin.site.register(Ward, WardAdmin)
admin.site.register(Bed, BedAdmin)
admin.site.register(Patient, PatientAdmin)
admin.site.register(Doctor, DoctorAdmin)
admin.site.register(Nurse, NurseAdmin)
admin.site.register(Appointment, AppointmentAdmin)
admin.site.register(Medicine, MedicineAdmin)
admin.site.register(MedicineLog, MedicineLogAdmin)
admin.site.register(PatientTimeline, PatientTimelineAdmin)
admin.site.register(PatientQueue, PatientQueueAdmin)
admin.site.register(Alert, AlertAdmin)
admin.site.register(HospitalMetric, HospitalMetricAdmin)
admin.site.register(Prediction, PredictionAdmin)
admin.site.register(ActivityLog, ActivityLogAdmin)

admin.site.site_header = 'Hospital Command Center Admin'
admin.site.site_title = 'HCC Admin'
admin.site.index_title = 'Hospital Management'
