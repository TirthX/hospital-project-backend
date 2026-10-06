"""
Lightweight real-time simulation engine.

The report's original design used Celery workers with a Redis broker. That is
heavy to set up on a student laptop and unreliable during a live
demonstration, so the background work runs here instead: one ``simulation_tick``
call performs a single step of hospital activity and is driven either by the
frontend's polling timer or by ``python manage.py run_simulation``.

Each tick:
  1. admits new patients following a realistic hourly arrival rate
  2. discharges recovered patients and frees their beds
  3. drifts the vitals of admitted patients and re-scores their risk
  4. advances the triage queue and recalculates priority scores
  5. rotates doctor and nurse shifts against the clock
  6. consumes medicine stock and logs administrations
  7. records a HospitalMetric snapshot for the trend charts
  8. runs the eight-rule automation alert engine

All derived clinical values come from core.clinical, so a patient's risk level
is identical whether it was set here or by the REST API.
"""
import logging
import math
import random

from django.db import transaction
from django.db.models import Count, F
from django.utils import timezone
from datetime import timedelta

from .clinical import (
    HEART_RATE_NORMAL,
    SPO2_NORMAL,
    SYSTOLIC_NORMAL,
    TEMPERATURE_NORMAL,
    apply_risk_assessment,
    assess_patient,
    estimate_wait_minutes,
    priority_score,
)
from .models import (
    Alert, Bed, Doctor, HospitalMetric, Medicine, MedicineLog, Nurse,
    Patient, PatientQueue, PatientTimeline, Ward,
)

logger = logging.getLogger(__name__)

# Keeping the simulated hospital from growing without bound: past this many
# active patients the engine stops admitting and lets discharges catch up.
MAX_ACTIVE_PATIENTS = 140

FIRST_NAMES = [
    'Aarav', 'Vivaan', 'Aditya', 'Arjun', 'Saanvi', 'Aanya', 'Aadhya',
    'Ananya', 'Kabir', 'Diya', 'Rohit', 'Priya', 'Sai', 'Meera', 'Ishaan',
    'Kavya', 'Dhruv', 'Riya', 'Advik', 'Anika', 'Reyansh', 'Myra',
    'Vihaan', 'Prisha', 'Krishna', 'Siya', 'Neel', 'Tara',
]
LAST_NAMES = [
    'Sharma', 'Patel', 'Singh', 'Kumar', 'Verma', 'Gupta', 'Reddy', 'Joshi',
    'Nair', 'Mehta', 'Das', 'Shah', 'Iyer', 'Bhatt', 'Chauhan', 'Desai',
]

# Diagnoses with a realistic severity band and whether they usually arrive
# through the emergency department. Sampling from this keeps the generated
# case mix clinically plausible instead of uniformly random.
DISEASE_PROFILES = [
    ('Pneumonia',       (4, 8),  0.30),
    ('Fracture',        (2, 6),  0.55),
    ('Dengue',          (3, 7),  0.20),
    ('Asthma',          (2, 6),  0.35),
    ('Hypertension',    (2, 5),  0.10),
    ('Cardiac Arrest',  (8, 10), 0.95),
    ('COVID-19',        (3, 8),  0.25),
    ('Typhoid',         (3, 6),  0.15),
    ('Malaria',         (3, 6),  0.15),
    ('Diabetes',        (2, 5),  0.08),
    ('Stroke',          (7, 10), 0.90),
    ('Appendicitis',    (5, 8),  0.70),
    ('Kidney Stone',    (3, 6),  0.30),
    ('Tuberculosis',    (4, 7),  0.12),
    ('Liver Cirrhosis', (5, 8),  0.20),
]

SHIFT_FOR_HOUR = (
    ('morning', range(6, 14)),
    ('afternoon', range(14, 22)),
)


def _current_shift(hour):
    """Which of the three duty shifts covers the given hour."""
    for name, hours in SHIFT_FOR_HOUR:
        if hour in hours:
            return name
    return 'night'


def _arrival_rate(now):
    """Expected admissions this tick, following the day/night curve.

    Mirrors the shape used to build the training dataset so the live data and
    the model's expectations stay consistent.
    """
    hour = now.hour
    morning_peak = math.exp(-((hour - 11) ** 2) / 18.0)
    evening_peak = math.exp(-((hour - 19) ** 2) / 12.0)
    time_factor = 0.35 + 1.25 * max(morning_peak, evening_peak * 0.95)
    weekend_factor = 1.22 if now.weekday() >= 5 else 1.0
    # Scaled down to roughly 0-2 admissions per tick so a demo stays legible.
    return 0.55 * time_factor * weekend_factor


@transaction.atomic
def simulation_tick():
    """Execute one simulation step. Returns a summary of what happened."""
    from automation.engine import run_alert_engine

    now = timezone.localtime()
    events = []

    admitted = _admit_patients(now, events)
    discharged = _discharge_patients(events)
    _drift_vitals(events)
    _advance_queue()
    _rotate_shifts(now)
    _consume_medicines(events)

    snapshot = _record_metrics_snapshot(admitted)
    alert_result = run_alert_engine()

    for alert in alert_result['alerts']:
        events.append(f"Alert [{alert['level']}] {alert['message']}")

    return {
        'timestamp': now.isoformat(),
        'shift': _current_shift(now.hour),
        'events': events,
        'new_admissions': admitted,
        'discharges': discharged,
        'alerts_raised': alert_result['alerts_raised'],
        'total_active_patients': snapshot.total_patients,
        'available_beds': snapshot.available_beds,
        'icu_occupancy': snapshot.icu_occupancy,
    }


# ─── 1. Admissions ────────────────────────────────────────────────

def _admit_patients(now, events):
    """Admit new patients at the current hour's arrival rate."""
    active_count = Patient.objects.filter(is_discharged=False).count()
    if active_count >= MAX_ACTIVE_PATIENTS:
        return 0

    rate = _arrival_rate(now)
    # Poisson arrivals, capped so one tick cannot flood the ward.
    count = min(3, _poisson(rate), MAX_ACTIVE_PATIENTS - active_count)

    for _ in range(count):
        patient = _admit_one_patient()
        events.append(f'Admitted {patient.name} — {patient.disease} '
                      f'({patient.risk_level} risk)')
    return count


def _poisson(lam):
    """Small Poisson sampler (Knuth's method) — avoids a numpy import here."""
    if lam <= 0:
        return 0
    target, k, product = math.exp(-lam), 0, 1.0
    while True:
        product *= random.random()
        if product <= target:
            return k
        k += 1
        if k > 10:                      # practical guard for tiny lambdas
            return k


def _admit_one_patient():
    """Create one realistic new admission with vitals, bed, queue entry."""
    disease, severity_band, emergency_probability = random.choice(DISEASE_PROFILES)
    severity = random.randint(*severity_band)
    is_emergency = random.random() < emergency_probability

    # Vitals are generated *from* severity so the risk engine's verdict and
    # the recorded diagnosis agree with each other.
    if severity >= 8:
        heart_rate = random.choice([random.randint(125, 145), random.randint(40, 54)])
        temperature = round(random.uniform(100.5, 104.0), 1)
        systolic = random.choice([random.randint(165, 195), random.randint(78, 89)])
        diastolic = random.randint(50, 110)
        oxygen = round(random.uniform(82.0, 91.0), 1)
    elif severity >= 6:
        heart_rate = random.randint(100, 124)
        temperature = round(random.uniform(99.6, 102.4), 1)
        systolic = random.randint(141, 164)
        diastolic = random.randint(85, 99)
        oxygen = round(random.uniform(90.5, 94.0), 1)
    elif severity >= 4:
        heart_rate = random.randint(88, 104)
        temperature = round(random.uniform(98.6, 100.2), 1)
        systolic = random.randint(128, 145)
        diastolic = random.randint(80, 92)
        oxygen = round(random.uniform(94.0, 97.0), 1)
    else:
        heart_rate = random.randint(64, 88)
        temperature = round(random.uniform(97.4, 99.1), 1)
        systolic = random.randint(108, 130)
        diastolic = random.randint(68, 84)
        oxygen = round(random.uniform(96.5, 99.5), 1)

    if is_emergency:
        department = 'Emergency'
    elif severity >= 7:
        department = 'ICU'
    else:
        department = random.choice(['General', 'General', 'General', 'Cardiology',
                                    'Orthopedics', 'Pulmonology'])

    # Prefer a doctor who is free and matches the department.
    doctor = (
        Doctor.objects.filter(status='available', department=department).first()
        or Doctor.objects.filter(status='available').order_by('today_patients').first()
        or Doctor.objects.order_by('today_patients').first()
    )

    patient = Patient.objects.create(
        name=f'{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}',
        age=random.randint(3, 88),
        gender=random.choice(['Male', 'Female']),
        phone=f'+91 {random.randint(70000, 99999)} {random.randint(10000, 99999)}',
        disease=disease,
        disease_severity=severity,
        condition='Critical' if severity >= 8 else 'Under Observation' if severity >= 5 else 'Stable',
        heart_rate=heart_rate,
        temperature=temperature,
        blood_pressure_systolic=systolic,
        blood_pressure_diastolic=diastolic,
        oxygen_saturation=oxygen,
        is_emergency=is_emergency,
        department=department,
        attending_doctor=doctor,
        expected_discharge=timezone.now() + timedelta(days=max(1, severity // 2)),
    )

    assessment = apply_risk_assessment(patient)

    PatientTimeline.objects.create(
        patient=patient, event_type='registered',
        description=(f'Admitted to {department} with {disease} '
                     f'(severity {severity}/10).'),
        created_by='Simulation Engine',
    )
    PatientTimeline.objects.create(
        patient=patient, event_type='vitals_recorded',
        description=(f'HR {heart_rate} BPM, Temp {temperature} F, '
                     f'BP {systolic}/{diastolic}, SpO2 {oxygen}%. '
                     f"Risk assessed as {assessment['risk_level'].title()}."),
        created_by='Simulation Engine',
    )
    if doctor:
        PatientTimeline.objects.create(
            patient=patient, event_type='doctor_assigned',
            description=f'Dr. {doctor.name} ({doctor.specialization}) assigned.',
            created_by='Simulation Engine',
        )
        Doctor.objects.filter(pk=doctor.pk).update(today_patients=F('today_patients') + 1)

    _assign_bed_for(patient, assessment)

    # Critical and emergency arrivals always enter the triage queue; routine
    # admissions join it only if they need a consultation.
    if is_emergency or assessment['risk_level'] in ('high', 'critical') or random.random() < 0.35:
        PatientQueue.objects.create(
            patient=patient,
            priority_score=priority_score(patient, assessment=assessment),
            doctor_assigned=doctor,
            estimated_wait_minutes=random.randint(5, 50),
            department=department,
        )

    return patient


def _assign_bed_for(patient, assessment):
    """Place the patient in the most clinically appropriate free bed."""
    if assessment['risk_level'] == 'critical' or patient.department == 'ICU':
        preference = ['icu', 'emergency', 'general']
    elif patient.is_emergency:
        preference = ['emergency', 'icu', 'general']
    else:
        preference = ['general', 'emergency']

    for ward_type in preference:
        bed = Bed.objects.filter(status='available', ward__ward_type=ward_type).first()
        if bed:
            bed.status = 'occupied'
            bed.patient = patient
            bed.save(update_fields=['status', 'patient'])
            PatientTimeline.objects.create(
                patient=patient,
                event_type='shifted_icu' if ward_type == 'icu' else 'shifted_ward',
                description=f'Allocated bed {bed.bed_number} in {bed.ward.name}.',
                created_by='Simulation Engine',
            )
            return bed
    return None


# ─── 2. Discharges ────────────────────────────────────────────────

def _discharge_patients(events):
    """Discharge recovered patients, faster when the hospital is full.

    Eligibility is by *current* risk rather than admission severity: a
    severity-8 patient whose vitals have recovered to Low should be able to
    go home, and restricting discharge to originally-mild cases was what let
    the ward fill up with permanent residents.
    """
    candidates = Patient.objects.filter(
        is_discharged=False,
        risk_level__in=['low', 'medium'],
    ).order_by('admitted_at')

    if not candidates.exists():
        return 0

    # A busy hospital discharges harder — real wards do exactly this when
    # they need the beds, and it keeps the simulation in equilibrium instead
    # of drifting up to its patient cap.
    active_count = Patient.objects.filter(is_discharged=False).count()
    pressure = active_count / MAX_ACTIVE_PATIENTS
    rate = 0.7 + 1.6 * max(0.0, pressure - 0.5)

    count = min(3, _poisson(rate), candidates.count())
    discharged = 0

    for patient in list(candidates[:count]):
        assessment = assess_patient(patient)
        # Never discharge someone the risk engine has since escalated.
        if assessment['risk_level'] in ('high', 'critical'):
            continue

        patient.is_discharged = True
        patient.discharged_at = timezone.now()
        patient.condition = 'Recovered — Discharged'
        patient.save(update_fields=['is_discharged', 'discharged_at', 'condition'])

        Bed.objects.filter(patient=patient).update(status='available', patient=None)
        PatientQueue.objects.filter(patient=patient).exclude(status='completed').update(
            status='completed'
        )

        PatientTimeline.objects.create(
            patient=patient, event_type='recovered',
            description='Condition stable; cleared for discharge by attending doctor.',
            created_by='Simulation Engine',
        )
        PatientTimeline.objects.create(
            patient=patient, event_type='discharged',
            description='Discharged. Bed released back to the ward.',
            created_by='Simulation Engine',
        )
        events.append(f'Discharged {patient.name}')
        discharged += 1

    return discharged


# ─── 3. Vitals drift and re-scoring ───────────────────────────────

# Healthy targets that treatment pulls vitals back towards.
HEALTHY_VITALS = {
    'heart_rate': 74.0,
    'temperature': 98.4,
    'oxygen_saturation': 97.5,
    'blood_pressure_systolic': 118.0,
    'blood_pressure_diastolic': 78.0,
}

# Fraction of the gap to a healthy value that treatment closes per tick.
TREATMENT_RECOVERY_RATE = 0.10


def _drift_vitals(events):
    """Move a sample of patients' vitals and re-run the risk engine.

    Two forces act on every vital:

      * random volatility, scaled by severity, which is what lets a Medium
        patient deteriorate into High or Critical during a demonstration;
      * a treatment effect pulling the vital back towards a healthy value.

    The treatment effect is essential, not cosmetic. With symmetric random
    drift alone, admitted patients only ever accumulate abnormal readings:
    nobody recovers, discharges dry up, and after a few hours of running the
    hospital saturates with every patient rated Critical. Pulling vitals
    towards normal gives the simulation an equilibrium, so it can run
    indefinitely and still look like a working hospital.
    """
    # The sample scales with the census rather than being a fixed count.
    # With a fixed sample, a busy ward treats each patient far less often
    # than a quiet one, recovery falls behind admissions and the hospital
    # drifts towards its cap. Rounding on a constant *share* of patients
    # keeps the treatment rate per patient steady at any occupancy — which
    # is also what a real ward does.
    active_total = Patient.objects.filter(is_discharged=False).count()
    sample_size = max(6, round(active_total * 0.25))

    active = list(
        Patient.objects.filter(is_discharged=False).order_by('?')[:sample_size]
    )

    for patient in active:
        volatility = 1.0 + patient.disease_severity / 9.0
        previous_risk = patient.risk_level

        # Recovery strengthens the longer a patient has been under care.
        #
        # It is deliberately NOT weakened by severity. Scaling it down for
        # sicker patients made the treatment pull roughly eight times
        # smaller than the random noise for a severity-9 case, so those
        # patients random-walked instead of recovering, never became
        # dischargeable, and accumulated until most of the ward was
        # Critical. A sicker patient receives more intensive care, not
        # less, so the pull stays constant while only the noise scales.
        hours_admitted = max(
            0.0, (timezone.now() - patient.admitted_at).total_seconds() / 3600,
        )
        recovery = TREATMENT_RECOVERY_RATE * (
            1.0 + min(2.0, hours_admitted / 48.0)
        )

        def treated(current, field, noise):
            """Nudge one vital towards health, then add random movement."""
            target = HEALTHY_VITALS[field]
            return current + (target - current) * recovery + random.gauss(0, noise)

        patient.heart_rate = int(max(42, min(180,
            treated(patient.heart_rate, 'heart_rate', 3.0 * volatility))))
        patient.temperature = round(max(95.5, min(106.0,
            treated(patient.temperature, 'temperature', 0.2 * volatility))), 1)
        patient.oxygen_saturation = round(max(74.0, min(100.0,
            treated(patient.oxygen_saturation, 'oxygen_saturation', 0.6 * volatility))), 1)
        patient.blood_pressure_systolic = int(max(70, min(210,
            treated(patient.blood_pressure_systolic,
                    'blood_pressure_systolic', 3.5 * volatility))))
        patient.blood_pressure_diastolic = int(max(40, min(125,
            treated(patient.blood_pressure_diastolic,
                    'blood_pressure_diastolic', 2.2 * volatility))))

        # Diagnosis severity reflects the patient's *current* condition, not
        # the state they arrived in, so it steps down as they respond to
        # treatment. Without this, severity alone keeps an elderly
        # high-severity patient above the discharge threshold permanently:
        # they never leave, and the ward slowly fills with patients who can
        # never recover no matter how good their vitals become.
        updated_fields = [
            'heart_rate', 'temperature', 'oxygen_saturation',
            'blood_pressure_systolic', 'blood_pressure_diastolic',
        ]
        if patient.disease_severity > 1 and _vitals_are_settling(patient):
            if random.random() < 0.35:
                patient.disease_severity -= 1
                updated_fields.append('disease_severity')
                if patient.disease_severity <= 3 and patient.condition != 'Recovering':
                    patient.condition = 'Recovering'
                    updated_fields.append('condition')

        patient.save(update_fields=updated_fields)

        assessment = apply_risk_assessment(patient)
        new_risk = assessment['risk_level']

        if new_risk != previous_risk:
            direction = 'deteriorated' if _risk_rank(new_risk) > _risk_rank(previous_risk) else 'improved'
            PatientTimeline.objects.create(
                patient=patient, event_type='condition_update',
                description=(f'Risk {direction} from {previous_risk.title()} to '
                             f'{new_risk.title()}. SpO2 {patient.oxygen_saturation}%, '
                             f'HR {patient.heart_rate} BPM.'),
                created_by='AI Risk Engine',
            )
            events.append(f'{patient.name} {direction}: {previous_risk} -> {new_risk}')

            # A patient who becomes critical is escalated in the queue and,
            # where possible, moved to ICU.
            if new_risk == 'critical':
                _escalate_to_icu(patient, assessment)


def _vitals_are_settling(patient):
    """True when every vital sign is back inside its normal band.

    Used as the clinical signal that a patient is responding to treatment
    and their recorded severity can be stepped down.
    """
    return (
        HEART_RATE_NORMAL[0] <= patient.heart_rate <= HEART_RATE_NORMAL[1]
        and patient.temperature < TEMPERATURE_NORMAL
        and SYSTOLIC_NORMAL[0] <= patient.blood_pressure_systolic <= SYSTOLIC_NORMAL[1]
        and patient.oxygen_saturation >= SPO2_NORMAL
    )


def _risk_rank(level):
    return {'low': 0, 'medium': 1, 'high': 2, 'critical': 3}.get(level, 0)


def _escalate_to_icu(patient, assessment):
    """Move a newly critical patient to ICU and to the front of the queue."""
    current_bed = Bed.objects.filter(patient=patient).first()
    already_in_icu = current_bed and current_bed.ward.ward_type == 'icu'

    if not already_in_icu:
        icu_bed = Bed.objects.filter(status='available', ward__ward_type='icu').first()
        if icu_bed:
            if current_bed:
                current_bed.status = 'available'
                current_bed.patient = None
                current_bed.save(update_fields=['status', 'patient'])
            icu_bed.status = 'occupied'
            icu_bed.patient = patient
            icu_bed.save(update_fields=['status', 'patient'])
            PatientTimeline.objects.create(
                patient=patient, event_type='shifted_icu',
                description=(f'Escalated to ICU bed {icu_bed.bed_number} after risk '
                             'assessment returned Critical.'),
                created_by='AI Risk Engine',
            )

    entry = PatientQueue.objects.filter(patient=patient, status='waiting').first()
    if entry:
        entry.priority_score = priority_score(
            patient, waiting_since=entry.waiting_since, assessment=assessment
        )
        entry.save(update_fields=['priority_score'])


# ─── 4. Triage queue ──────────────────────────────────────────────

def _advance_queue():
    """Recalculate priorities, then move the queue forward.

    Priority scores are recomputed every tick because waiting time is one of
    their inputs, so a stable patient's position must improve as they wait.
    """
    doctors_available = Doctor.objects.filter(status='available').count()

    waiting = list(
        PatientQueue.objects.filter(status='waiting').select_related('patient')
    )
    for entry in waiting:
        entry.priority_score = priority_score(
            entry.patient, waiting_since=entry.waiting_since
        )

    # Re-rank by the new scores and update each patient's wait estimate.
    waiting.sort(key=lambda e: (-e.priority_score, e.waiting_since))
    for position, entry in enumerate(waiting, start=1):
        entry.estimated_wait_minutes = estimate_wait_minutes(position, doctors_available)
        entry.save(update_fields=['priority_score', 'estimated_wait_minutes'])

    # Call the highest-priority patients through to consultation.
    in_consultation = PatientQueue.objects.filter(status='in_consultation').count()
    free_slots = max(0, doctors_available - in_consultation)

    for entry in waiting[:free_slots]:
        if random.random() < 0.5:                    # not every free doctor calls at once
            continue
        entry.status = 'in_consultation'
        entry.save(update_fields=['status'])
        PatientTimeline.objects.create(
            patient=entry.patient, event_type='doctor_assigned',
            description=(f'Called for consultation from the triage queue '
                         f'(priority {entry.priority_score}).'),
            created_by='Simulation Engine',
        )

    # Finish some consultations already in progress.
    for entry in PatientQueue.objects.filter(status='in_consultation').select_related('patient'):
        if random.random() < 0.35:
            entry.status = 'completed'
            entry.save(update_fields=['status'])


# ─── 5. Staff shifts ──────────────────────────────────────────────

def _rotate_shifts(now):
    """Put staff whose shift covers this hour on duty, and the rest off duty.

    Driven by the clock rather than randomly, so the roster on screen always
    matches the time of day shown in the header.
    """
    active_shift = _current_shift(now.hour)

    for model in (Doctor, Nurse):
        # On-duty staff for this shift: available unless already busy.
        on_shift = model.objects.filter(shift=active_shift)
        on_shift.filter(status='off_duty').update(status='available')

        # Staff on another shift go off duty.
        model.objects.exclude(shift=active_shift).update(status='off_duty')

        # A share of on-duty staff are with patients at any moment.
        for member in on_shift.filter(status__in=['available', 'busy']):
            member.status = 'busy' if random.random() < 0.4 else 'available'
            member.save(update_fields=['status'])


# ─── 6. Pharmacy ──────────────────────────────────────────────────

def _consume_medicines(events):
    """Administer medicines to admitted patients and log each dose."""
    patients = list(Patient.objects.filter(is_discharged=False).order_by('?')[:3])
    if not patients:
        return

    for patient in patients:
        if random.random() > 0.45:
            continue

        medicine = Medicine.objects.filter(stock__gt=0).order_by('?').first()
        if not medicine:
            break

        quantity = random.randint(1, 3)
        quantity = min(quantity, medicine.stock)

        medicine.stock = medicine.stock - quantity
        medicine.save(update_fields=['stock'])

        nurse = Nurse.objects.exclude(status='off_duty').order_by('?').first()
        MedicineLog.objects.create(
            medicine=medicine, patient=patient, quantity=quantity,
            administered_by=nurse.name if nurse else 'Duty Nurse',
            notes='Administered during routine round.',
        )
        PatientTimeline.objects.create(
            patient=patient, event_type='medicine_given',
            description=f'{medicine.name} x{quantity} administered.',
            created_by=nurse.name if nurse else 'Duty Nurse',
        )

        if medicine.stock <= 0:
            events.append(f'{medicine.name} is now out of stock')


# ─── 7. Metric snapshot ───────────────────────────────────────────

def _record_metrics_snapshot(new_admissions):
    """Record the current hospital state for the trend charts and the ML
    engine's rolling features."""
    active = Patient.objects.filter(is_discharged=False)
    today = timezone.localtime().date()

    bed_counts = dict(
        Bed.objects.values_list('status').annotate(n=Count('id'))
    )

    waiting = PatientQueue.objects.filter(status='waiting')
    avg_wait = 0.0
    if waiting.exists():
        avg_wait = sum(e.estimated_wait_minutes for e in waiting) / waiting.count()

    doctors_on_duty = Doctor.objects.exclude(status='off_duty').count()
    nurses_on_duty = Nurse.objects.exclude(status='off_duty').count()

    return HospitalMetric.objects.create(
        total_patients=active.count(),
        new_admissions=new_admissions,
        discharges=Patient.objects.filter(discharged_at__date=today).count(),
        icu_occupancy=Bed.objects.filter(
            ward__ward_type='icu', status='occupied'
        ).count(),
        general_occupancy=Bed.objects.filter(
            ward__ward_type='general', status='occupied'
        ).count(),
        emergency_patients=active.filter(is_emergency=True).count(),
        available_beds=bed_counts.get('available', 0),
        avg_wait_time=round(avg_wait, 1),
        patient_inflow=new_admissions,
        doctors_on_duty=doctors_on_duty,
        nurses_on_duty=nurses_on_duty,
        staff_count=doctors_on_duty + nurses_on_duty,
        critical_alerts=Alert.objects.filter(level='critical', is_resolved=False).count(),
    )
