"""
Role-based access control.

The report defines four roles and what each may do:

  Admin         full control of every module
  Doctor        patient care, triage queue, clinical timeline, appointments
  Nurse         vitals, beds, medicine administration
  Receptionist  registration, bed allocation, appointments

These permission classes are attached to the API views so that a request
carrying a Receptionist's token cannot reach an Admin-only endpoint.
"""
from rest_framework.permissions import BasePermission, SAFE_METHODS

ADMIN = 'admin'
DOCTOR = 'doctor'
NURSE = 'nurse'
RECEPTIONIST = 'receptionist'

ALL_ROLES = (ADMIN, DOCTOR, NURSE, RECEPTIONIST)
CLINICAL_ROLES = (ADMIN, DOCTOR, NURSE)
FRONT_DESK_ROLES = (ADMIN, RECEPTIONIST)


class HasRole(BasePermission):
    """Base class: allow only the roles listed in ``allowed_roles``.

    Admins are always allowed, so subclasses never need to list ``admin``.
    """
    allowed_roles = ALL_ROLES
    message = 'Your role does not have permission to perform this action.'

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if user.is_superuser or user.role == ADMIN:
            return True
        return user.role in self.allowed_roles


class IsAdmin(HasRole):
    """Admin-only endpoints: staff management, settings, audit trail."""
    allowed_roles = (ADMIN,)
    message = 'This action is restricted to administrators.'


class IsClinicalStaff(HasRole):
    """Doctors and nurses — anything touching vitals or clinical records."""
    allowed_roles = CLINICAL_ROLES
    message = 'This action is restricted to clinical staff.'


class IsDoctor(HasRole):
    """Doctor-only actions such as completing a consultation."""
    allowed_roles = (DOCTOR,)
    message = 'This action is restricted to doctors.'


class CanManagePatients(HasRole):
    """Read for everyone; registration and edits for the roles that do them.

    A receptionist registers patients, clinical staff update them, and a
    doctor amends the clinical record — but a nurse cannot delete a patient.
    """
    allowed_roles = ALL_ROLES

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if user.is_superuser or user.role == ADMIN:
            return True
        if request.method in SAFE_METHODS:
            return user.role in ALL_ROLES
        if request.method == 'DELETE':
            return False
        # POST (register) is front-desk work; PUT/PATCH is clinical work.
        if request.method == 'POST':
            return user.role in (RECEPTIONIST, DOCTOR, NURSE)
        return user.role in CLINICAL_ROLES


class CanManageBeds(HasRole):
    """Bed allocation, transfer and release."""
    allowed_roles = (DOCTOR, NURSE, RECEPTIONIST)
    message = 'This action is restricted to staff who manage bed allocation.'


class CanManageMedicines(HasRole):
    """Pharmacy stock: nurses administer, admins restock."""
    allowed_roles = (NURSE, DOCTOR)
    message = 'This action is restricted to clinical and pharmacy staff.'


class CanManageAppointments(HasRole):
    """Appointment scheduling is front-desk and doctor work."""
    allowed_roles = (RECEPTIONIST, DOCTOR)
    message = 'This action is restricted to reception and doctors.'


class CanResolveAlerts(HasRole):
    """Any logged-in member of staff may acknowledge an alert."""
    allowed_roles = ALL_ROLES


# Map used by the frontend so the sidebar can hide modules the role cannot
# open. Kept here next to the permission classes so the two cannot drift.
ROLE_MODULES = {
    ADMIN: [
        'dashboard', 'floor-map', 'beds', 'patients', 'queue', 'patient-risk',
        'emergency', 'staff', 'appointments', 'medicines', 'alerts',
        'disease-analytics', 'ai-forecast', 'recommendations', 'explanations',
        'analytics', 'reports', 'activity', 'wards', 'predictions',
        'settings', 'profile',
    ],
    DOCTOR: [
        'dashboard', 'floor-map', 'patients', 'queue', 'patient-risk',
        'emergency', 'appointments', 'medicines', 'alerts',
        'disease-analytics', 'ai-forecast', 'recommendations', 'explanations',
        'analytics', 'reports', 'wards', 'predictions', 'profile',
    ],
    NURSE: [
        'dashboard', 'floor-map', 'beds', 'patients', 'queue', 'patient-risk',
        'emergency', 'medicines', 'alerts', 'wards', 'profile',
    ],
    RECEPTIONIST: [
        'dashboard', 'floor-map', 'beds', 'patients', 'appointments',
        'queue', 'alerts', 'wards', 'profile',
    ],
}
