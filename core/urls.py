"""
Core API routes. Everything is mounted under /api/ by hospital_backend.urls.
"""
from django.urls import path

from .views import (
    ActivityLogListView,
    AdmissionTrendView,
    AlertBulkResolveView,
    AlertDetailView,
    AlertListView,
    AppointmentDetailView,
    AppointmentListView,
    BedAssignView,
    BedListView,
    BedReleaseView,
    BedStatusView,
    BedTransferView,
    ChangePasswordView,
    ChatAssistantView,
    DashboardStatsView,
    DiseaseAnalyticsView,
    DoctorDetailView,
    DoctorListView,
    FloorMapView,
    LoginView,
    LogoutView,
    MedicineDetailView,
    MedicineDispenseView,
    MedicineListView,
    MedicineLogListView,
    MedicineRestockView,
    MetricListView,
    NurseDetailView,
    NurseListView,
    OverviewAnalyticsView,
    PatientDetailView,
    PatientDischargeView,
    PatientListView,
    PatientQueueView,
    PatientRiskBoardView,
    PatientRiskView,
    PatientTimelineView,
    PatientVitalsView,
    PowerBIConfigView,
    PredictionListView,
    QueueEntryActionView,
    QueueRecalculateView,
    ReportCatalogueView,
    ReportGenerateView,
    SimulationTickView,
    StaffOverviewView,
    TokenRefreshView,
    UserInfoView,
    UserListView,
    WardDetailView,
    WardListView,
)

urlpatterns = [
    # ── Authentication ──────────────────────────────────────────
    path('login/', LoginView.as_view(), name='login'),
    path('logout/', LogoutView.as_view(), name='logout'),
    path('token/refresh/', TokenRefreshView.as_view(), name='token-refresh'),
    path('user/', UserInfoView.as_view(), name='user-info'),
    path('user/password/', ChangePasswordView.as_view(), name='change-password'),
    path('users/', UserListView.as_view(), name='user-list'),

    # ── Dashboard ───────────────────────────────────────────────
    path('dashboard/stats/', DashboardStatsView.as_view(), name='dashboard-stats'),
    path('dashboard/trend/', AdmissionTrendView.as_view(), name='dashboard-trend'),

    # ── Patients ────────────────────────────────────────────────
    path('patients/', PatientListView.as_view(), name='patient-list'),
    path('patients/risk-board/', PatientRiskBoardView.as_view(), name='patient-risk-board'),
    path('patients/<int:pk>/', PatientDetailView.as_view(), name='patient-detail'),
    path('patients/<int:pk>/timeline/', PatientTimelineView.as_view(), name='patient-timeline'),
    path('patients/<int:pk>/risk/', PatientRiskView.as_view(), name='patient-risk'),
    path('patients/<int:pk>/vitals/', PatientVitalsView.as_view(), name='patient-vitals'),
    path('patients/<int:pk>/discharge/', PatientDischargeView.as_view(), name='patient-discharge'),

    # ── Wards & floor map ───────────────────────────────────────
    path('wards/', WardListView.as_view(), name='ward-list'),
    path('wards/<int:pk>/', WardDetailView.as_view(), name='ward-detail'),
    path('floor-map/', FloorMapView.as_view(), name='floor-map'),

    # ── Beds ────────────────────────────────────────────────────
    path('beds/', BedListView.as_view(), name='bed-list'),
    path('beds/<int:pk>/assign/', BedAssignView.as_view(), name='bed-assign'),
    path('beds/<int:pk>/release/', BedReleaseView.as_view(), name='bed-release'),
    path('beds/<int:pk>/transfer/', BedTransferView.as_view(), name='bed-transfer'),
    path('beds/<int:pk>/status/', BedStatusView.as_view(), name='bed-status'),

    # ── Triage queue ────────────────────────────────────────────
    path('queue/', PatientQueueView.as_view(), name='patient-queue'),
    path('queue/recalculate/', QueueRecalculateView.as_view(), name='queue-recalculate'),
    path('queue/<int:pk>/action/', QueueEntryActionView.as_view(), name='queue-action'),

    # ── Staff ───────────────────────────────────────────────────
    path('doctors/', DoctorListView.as_view(), name='doctor-list'),
    path('doctors/<int:pk>/', DoctorDetailView.as_view(), name='doctor-detail'),
    path('nurses/', NurseListView.as_view(), name='nurse-list'),
    path('nurses/<int:pk>/', NurseDetailView.as_view(), name='nurse-detail'),
    path('staff/overview/', StaffOverviewView.as_view(), name='staff-overview'),

    # ── Appointments ────────────────────────────────────────────
    path('appointments/', AppointmentListView.as_view(), name='appointment-list'),
    path('appointments/<int:pk>/', AppointmentDetailView.as_view(), name='appointment-detail'),

    # ── Pharmacy ────────────────────────────────────────────────
    path('medicines/', MedicineListView.as_view(), name='medicine-list'),
    path('medicines/logs/', MedicineLogListView.as_view(), name='medicine-log-list'),
    path('medicines/<int:pk>/', MedicineDetailView.as_view(), name='medicine-detail'),
    path('medicines/<int:pk>/dispense/', MedicineDispenseView.as_view(), name='medicine-dispense'),
    path('medicines/<int:pk>/restock/', MedicineRestockView.as_view(), name='medicine-restock'),

    # ── Alerts ──────────────────────────────────────────────────
    path('alerts/', AlertListView.as_view(), name='alert-list'),
    path('alerts/bulk-resolve/', AlertBulkResolveView.as_view(), name='alert-bulk-resolve'),
    path('alerts/<int:pk>/', AlertDetailView.as_view(), name='alert-detail'),

    # ── Metrics & analytics ─────────────────────────────────────
    path('metrics/', MetricListView.as_view(), name='metric-list'),
    path('analytics/disease/', DiseaseAnalyticsView.as_view(), name='disease-analytics'),
    path('analytics/overview/', OverviewAnalyticsView.as_view(), name='overview-analytics'),
    path('predictions/', PredictionListView.as_view(), name='prediction-list'),
    path('activity/', ActivityLogListView.as_view(), name='activity-log-list'),

    # ── Simulation ──────────────────────────────────────────────
    path('simulate/tick/', SimulationTickView.as_view(), name='simulation-tick'),

    # ── Reports ─────────────────────────────────────────────────
    path('reports/', ReportCatalogueView.as_view(), name='report-catalogue'),
    path('reports/generate/', ReportGenerateView.as_view(), name='report-generate'),

    # ── Assistant & BI ──────────────────────────────────────────
    path('chat/', ChatAssistantView.as_view(), name='chat-assistant'),
    path('powerbi/config/', PowerBIConfigView.as_view(), name='powerbi-config'),
]
