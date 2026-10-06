"""
Automated alerting engine.

Implements the eight threshold rules in Table 6.6 of the project report. The
engine reads live hospital metrics after every simulation tick (and on demand
via the API) and raises an Alert the moment a rule is broken.

  icu_high         ICU bed occupancy exceeds 85%
  bed_shortage     General ward occupancy exceeds 90%
  medicine_low     Medicine stock drops below its minimum safety threshold
  staff_shortage   Patient-to-doctor/nurse ratio becomes unsafe
  patient_critical Patient vitals fall into the critical range
  long_wait        Triage queue waiting time exceeds 45 minutes
  emergency_surge  Sudden spike in emergency room volume
  system           Infrastructure or database connectivity events

De-duplication
--------------
A naive implementation re-raises the same alert on every tick and buries the
staff in noise. Each rule therefore suppresses a repeat of the same
``alert_type`` while an unresolved one from the last ``COOLDOWN_MINUTES`` is
still open, so an alert appears once, stays until resolved, and only returns
after the condition has genuinely recurred.
"""
import logging
from datetime import timedelta

from django.db.models import Avg, Count, F
from django.utils import timezone

from core.clinical import (
    ICU_OCCUPANCY_ALERT_AT,
    LONG_WAIT_ALERT_MINUTES,
    UNSAFE_DOCTOR_RATIO,
    UNSAFE_NURSE_RATIO,
    WARD_OCCUPANCY_ALERT_AT,
)
from core.models import (
    Alert, Bed, Doctor, HospitalMetric, Medicine, Nurse, Patient,
    PatientQueue, Ward,
)

logger = logging.getLogger(__name__)

# How long the same alert type stays suppressed after being raised.
COOLDOWN_MINUTES = 20

# An emergency count this many times the recent average counts as a surge.
EMERGENCY_SURGE_MULTIPLIER = 1.6
EMERGENCY_SURGE_FLOOR = 4          # never call 2 -> 4 patients a "surge"


def _recently_alerted(alert_type, within_minutes=COOLDOWN_MINUTES):
    """True if an unresolved alert of this type was raised recently."""
    cutoff = timezone.now() - timedelta(minutes=within_minutes)
    return Alert.objects.filter(
        alert_type=alert_type,
        is_resolved=False,
        timestamp__gte=cutoff,
    ).exists()


def _raise_alert(alert_type, level, message, description, source):
    """Create an alert unless an equivalent one is already open."""
    if _recently_alerted(alert_type):
        return None

    alert = Alert.objects.create(
        alert_type=alert_type,
        level=level,
        message=message,
        description=description,
        source=source,
    )
    logger.info('Alert raised [%s/%s] %s', level, alert_type, message)
    return alert


# ─── Rule 1: ICU occupancy ────────────────────────────────────────

def check_icu_occupancy():
    icu_total = Bed.objects.filter(ward__ward_type='icu').count()
    if not icu_total:
        return None

    icu_occupied = Bed.objects.filter(ward__ward_type='icu', status='occupied').count()
    pct = round(icu_occupied / icu_total * 100, 1)

    if pct < ICU_OCCUPANCY_ALERT_AT:
        return None

    remaining = icu_total - icu_occupied
    return _raise_alert(
        'icu_high',
        'critical' if pct >= 95 else 'warning',
        f'ICU occupancy at {pct}%',
        (f'{icu_occupied} of {icu_total} ICU beds are occupied — only {remaining} '
         f'remaining. Threshold is {ICU_OCCUPANCY_ALERT_AT}%. Consider '
         'rescheduling elective surgeries and preparing step-down beds.'),
        'ICU Monitor',
    )


# ─── Rule 2: General ward bed shortage ────────────────────────────

def check_bed_shortage():
    raised = None
    for ward in Ward.objects.filter(ward_type='general'):
        if not ward.capacity or ward.occupancy_percentage < WARD_OCCUPANCY_ALERT_AT:
            continue
        raised = _raise_alert(
            'bed_shortage',
            'critical' if ward.occupancy_percentage >= 98 else 'warning',
            f'{ward.name} at {ward.occupancy_percentage}% occupancy',
            (f'{ward.current_patients} of {ward.capacity} beds in use. Threshold is '
             f'{WARD_OCCUPANCY_ALERT_AT}%. Plan discharges or open overflow capacity.'),
            'Bed Monitor',
        )
        if raised:
            break
    return raised


# ─── Rule 3: Medicine low stock ───────────────────────────────────

def check_medicine_stock():
    out_of_stock = list(
        Medicine.objects.filter(stock__lte=0).values_list('name', flat=True)[:5]
    )
    low_stock = list(
        Medicine.objects.filter(stock__gt=0, stock__lte=F('min_stock'))
        .values_list('name', flat=True)[:5]
    )

    if not out_of_stock and not low_stock:
        return None

    if out_of_stock:
        level = 'critical'
        message = f'{len(out_of_stock)} medicines out of stock'
        detail = 'Out of stock: ' + ', '.join(out_of_stock) + '. '
    else:
        level = 'warning'
        message = f'{len(low_stock)} medicines below minimum stock'
        detail = ''

    if low_stock:
        detail += 'At or below minimum: ' + ', '.join(low_stock) + '.'

    return _raise_alert(
        'medicine_low', level, message,
        detail + ' Raise a purchase order with the supplier.',
        'Inventory Monitor',
    )


# ─── Rule 4: Staff shortage ───────────────────────────────────────

def check_staff_ratio():
    active_patients = Patient.objects.filter(is_discharged=False).count()
    if not active_patients:
        return None

    doctors = Doctor.objects.exclude(status='off_duty').count()
    nurses = Nurse.objects.exclude(status='off_duty').count()

    # No staff on duty at all is the most serious form of this alert.
    if not doctors or not nurses:
        return _raise_alert(
            'staff_shortage', 'critical',
            'No clinical staff recorded on duty',
            (f'{active_patients} active patients with {doctors} doctors and '
             f'{nurses} nurses on duty. Update the duty roster immediately.'),
            'Roster Monitor',
        )

    nurse_ratio = round(active_patients / nurses, 1)
    doctor_ratio = round(active_patients / doctors, 1)

    breaches = []
    if nurse_ratio > UNSAFE_NURSE_RATIO:
        breaches.append(f'nurse-to-patient 1:{nurse_ratio} (safe limit 1:{UNSAFE_NURSE_RATIO})')
    if doctor_ratio > UNSAFE_DOCTOR_RATIO:
        breaches.append(f'doctor-to-patient 1:{doctor_ratio} (safe limit 1:{UNSAFE_DOCTOR_RATIO})')

    if not breaches:
        return None

    return _raise_alert(
        'staff_shortage',
        'critical' if len(breaches) > 1 else 'warning',
        'Unsafe staff-to-patient ratio',
        (f'{active_patients} active patients against {doctors} doctors and '
         f'{nurses} nurses on duty. Breached: ' + '; '.join(breaches) + '.'),
        'Roster Monitor',
    )


# ─── Rule 5: Patient in critical condition ────────────────────────

def check_critical_patients():
    critical = Patient.objects.filter(is_discharged=False, risk_level='critical')
    count = critical.count()
    if not count:
        return None

    names = ', '.join(critical.values_list('name', flat=True)[:5])
    unattended = critical.filter(attending_doctor__isnull=True).count()

    detail = f'Patients scored Critical by the risk engine: {names}.'
    if unattended:
        detail += f' {unattended} have no attending doctor assigned.'

    return _raise_alert(
        'patient_critical',
        'critical',
        f'{count} patient{"s" if count > 1 else ""} in critical condition',
        detail,
        'Patient Monitor',
    )


# ─── Rule 6: Long waiting time ────────────────────────────────────

def check_waiting_times():
    long_waits = PatientQueue.objects.filter(
        status='waiting', estimated_wait_minutes__gt=LONG_WAIT_ALERT_MINUTES
    )
    count = long_waits.count()
    if not count:
        return None

    worst = long_waits.order_by('-estimated_wait_minutes').first()
    avg_wait = round(
        PatientQueue.objects.filter(status='waiting')
        .aggregate(v=Avg('estimated_wait_minutes'))['v'] or 0, 1
    )

    return _raise_alert(
        'long_wait',
        'critical' if count > 5 else 'warning',
        f'{count} patients waiting over {LONG_WAIT_ALERT_MINUTES} minutes',
        (f'Longest wait: {worst.patient.name} at {worst.estimated_wait_minutes} minutes. '
         f'Queue average is {avg_wait} minutes. Consider opening another '
         'consultation room.'),
        'Queue Monitor',
    )


# ─── Rule 7: Emergency surge ──────────────────────────────────────

def check_emergency_surge():
    """Compare current emergency load against the recent rolling average.

    A fixed cut-off would fire permanently in a busy hospital and never in a
    quiet one, so the rule is relative to that hospital's own baseline.
    """
    current = Patient.objects.filter(is_discharged=False, is_emergency=True).count()
    if current < EMERGENCY_SURGE_FLOOR:
        return None

    baseline = HospitalMetric.objects.order_by('-timestamp')[:12].aggregate(
        v=Avg('emergency_patients')
    )['v']
    if not baseline:
        return None

    if current < baseline * EMERGENCY_SURGE_MULTIPLIER:
        return None

    return _raise_alert(
        'emergency_surge',
        'critical' if current >= baseline * 2 else 'warning',
        f'Emergency volume spike: {current} active emergency patients',
        (f'Current emergency load of {current} is above the recent average of '
         f'{baseline:.1f}. Activate the secondary triage bay and alert the '
         'on-call trauma team.'),
        'Emergency Monitor',
    )


# ─── Rule 8: System / data integrity ──────────────────────────────

def check_system_health():
    """Flag infrastructure and data-integrity problems.

    Catches the two conditions that silently break the dashboard: the
    simulation engine having stopped writing metric snapshots, and beds whose
    status contradicts their patient link.
    """
    latest = HospitalMetric.objects.order_by('-timestamp').first()
    if latest:
        stale_minutes = (timezone.now() - latest.timestamp).total_seconds() / 60
        if stale_minutes > 120:
            return _raise_alert(
                'system', 'warning',
                'Metric snapshots are stale',
                (f'No hospital metric snapshot has been recorded for '
                 f'{stale_minutes:.0f} minutes. The simulation engine may have '
                 'stopped — check the live data feed.'),
                'System Monitor',
            )

    # A bed marked occupied with no patient (or vice versa) makes every
    # occupancy figure wrong, so it is worth surfacing.
    orphan_occupied = Bed.objects.filter(status='occupied', patient__isnull=True).count()
    orphan_assigned = Bed.objects.exclude(status='occupied').filter(
        patient__isnull=False
    ).count()

    if orphan_occupied or orphan_assigned:
        return _raise_alert(
            'system', 'warning',
            'Bed records are inconsistent',
            (f'{orphan_occupied} beds are marked occupied with no patient and '
             f'{orphan_assigned} hold a patient without occupied status. '
             'Occupancy figures may be inaccurate until this is corrected.'),
            'System Monitor',
        )

    return None


# ─── Engine entry point ───────────────────────────────────────────

ALERT_RULES = (
    ('icu_high', check_icu_occupancy),
    ('bed_shortage', check_bed_shortage),
    ('medicine_low', check_medicine_stock),
    ('staff_shortage', check_staff_ratio),
    ('patient_critical', check_critical_patients),
    ('long_wait', check_waiting_times),
    ('emergency_surge', check_emergency_surge),
    ('system', check_system_health),
)


def run_alert_engine():
    """Evaluate all eight rules once. Returns a summary of what fired.

    A failure inside one rule must not stop the other seven, so each is
    guarded individually and its error reported.
    """
    raised, errors = [], []

    for rule_name, rule in ALERT_RULES:
        try:
            alert = rule()
            if alert is not None:
                raised.append({
                    'id': alert.id,
                    'alert_type': alert.alert_type,
                    'level': alert.level,
                    'message': alert.message,
                })
        except Exception as exc:                      # noqa: BLE001
            logger.exception('Alert rule %s failed', rule_name)
            errors.append({'rule': rule_name, 'error': str(exc)})

    return {
        'evaluated_at': timezone.now().isoformat(),
        'rules_evaluated': len(ALERT_RULES),
        'alerts_raised': len(raised),
        'alerts': raised,
        'errors': errors,
    }


def threshold_summary():
    """Current value of every monitored metric against its threshold.

    Powers the Alert Centre's "monitored thresholds" panel so staff can see
    how close each metric is to firing, not only what has already fired.
    """
    icu_total = Bed.objects.filter(ward__ward_type='icu').count()
    icu_occupied = Bed.objects.filter(ward__ward_type='icu', status='occupied').count()
    icu_pct = round(icu_occupied / icu_total * 100, 1) if icu_total else 0.0

    general_wards = Ward.objects.filter(ward_type='general')
    worst_general = max(
        (w.occupancy_percentage for w in general_wards if w.capacity), default=0.0
    )

    active_patients = Patient.objects.filter(is_discharged=False).count()
    nurses = Nurse.objects.exclude(status='off_duty').count()
    doctors = Doctor.objects.exclude(status='off_duty').count()
    nurse_ratio = round(active_patients / nurses, 1) if nurses else 0.0
    doctor_ratio = round(active_patients / doctors, 1) if doctors else 0.0

    avg_wait = round(
        PatientQueue.objects.filter(status='waiting')
        .aggregate(v=Avg('estimated_wait_minutes'))['v'] or 0, 1
    )
    low_stock = Medicine.objects.filter(stock__lte=F('min_stock')).count()
    critical_patients = Patient.objects.filter(
        is_discharged=False, risk_level='critical'
    ).count()
    emergency_patients = Patient.objects.filter(
        is_discharged=False, is_emergency=True
    ).count()

    def row(alert_type, label, value, threshold, unit, breached):
        return {
            'alert_type': alert_type,
            'label': label,
            'value': value,
            'threshold': threshold,
            'unit': unit,
            'breached': breached,
            # Percentage of the way to firing, for the progress bars.
            'utilisation': round(min(100.0, (value / threshold * 100) if threshold else 0), 1),
        }

    return [
        row('icu_high', 'ICU occupancy', icu_pct, ICU_OCCUPANCY_ALERT_AT, '%',
            icu_pct >= ICU_OCCUPANCY_ALERT_AT),
        row('bed_shortage', 'Busiest general ward', worst_general,
            WARD_OCCUPANCY_ALERT_AT, '%', worst_general >= WARD_OCCUPANCY_ALERT_AT),
        row('long_wait', 'Average queue wait', avg_wait, LONG_WAIT_ALERT_MINUTES, 'min',
            avg_wait > LONG_WAIT_ALERT_MINUTES),
        row('staff_shortage', 'Nurse-to-patient ratio', nurse_ratio,
            UNSAFE_NURSE_RATIO, ':1', nurse_ratio > UNSAFE_NURSE_RATIO),
        row('staff_shortage', 'Doctor-to-patient ratio', doctor_ratio,
            UNSAFE_DOCTOR_RATIO, ':1', doctor_ratio > UNSAFE_DOCTOR_RATIO),
        row('medicine_low', 'Medicines below minimum', low_stock, 1, 'items',
            low_stock > 0),
        row('patient_critical', 'Critical patients', critical_patients, 1, 'patients',
            critical_patients > 0),
        row('emergency_surge', 'Active emergency patients', emergency_patients,
            EMERGENCY_SURGE_FLOOR, 'patients', emergency_patients >= EMERGENCY_SURGE_FLOOR),
    ]
