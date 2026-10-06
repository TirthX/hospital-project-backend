"""
Clinical scoring engine.

Single source of truth for the two derived clinical values described in the
project report:

  * Risk level      — Low / Medium / High / Critical, from vitals + severity.
  * Priority score  — 0-100 triage score = severity + emergency flag
                      + vital anomalies + waiting time.

Both the REST API (core.views) and the simulation engine (core.simulation)
import from here so a patient never shows two different risk levels.
"""
from django.utils import timezone

# ─── Vital sign reference ranges ──────────────────────────────────
# Adult reference bands used to decide whether a vital is normal, mildly
# deviated or severely deviated.
HEART_RATE_NORMAL = (60, 100)
HEART_RATE_SEVERE = (55, 120)
TEMPERATURE_NORMAL = 99.5          # deg F
TEMPERATURE_SEVERE = 102.0
SYSTOLIC_NORMAL = (100, 140)
SYSTOLIC_SEVERE = (90, 160)
SPO2_NORMAL = 94.0                 # percent
SPO2_SEVERE = 90.0

# Risk level thresholds on the 0-100 risk score.
RISK_CRITICAL_AT = 70
RISK_HIGH_AT = 50
RISK_MEDIUM_AT = 30

# Priority score weights (must total to a sensible 0-100 range).
PRIORITY_SEVERITY_WEIGHT = 5.0     # severity 1-10  -> 5-50 points
PRIORITY_EMERGENCY_BONUS = 20.0    # emergency flag -> +20 points
PRIORITY_VITALS_WEIGHT = 2.5       # 0-10 anomaly   -> 0-25 points
PRIORITY_WAIT_CAP = 20.0           # waiting time   -> up to +20 points
PRIORITY_WAIT_MINUTES_PER_POINT = 5.0

# Alert thresholds from Table 6.6 of the report.
ICU_OCCUPANCY_ALERT_AT = 85.0      # percent
WARD_OCCUPANCY_ALERT_AT = 90.0     # percent
LONG_WAIT_ALERT_MINUTES = 45
UNSAFE_NURSE_RATIO = 4             # patients per nurse
UNSAFE_DOCTOR_RATIO = 12           # patients per doctor


def _vital_findings(patient):
    """Inspect a patient's vitals and return (score, factors).

    ``score`` contributes to the 0-100 risk score. ``factors`` is a list of
    plain-language dicts so the UI can explain *why* a patient is at risk.
    """
    score = 0
    factors = []

    # Age — older patients deteriorate faster for the same vitals.
    if patient.age >= 65:
        score += 20
        factors.append({
            'factor': 'Age 65 or above',
            'impact': 'high',
            'value': f'{patient.age} years',
        })
    elif patient.age >= 50:
        score += 10
        factors.append({
            'factor': 'Age 50-64',
            'impact': 'medium',
            'value': f'{patient.age} years',
        })

    # Heart rate.
    hr = patient.heart_rate
    if hr > HEART_RATE_SEVERE[1] or hr < HEART_RATE_SEVERE[0]:
        score += 25
        factors.append({
            'factor': 'Tachycardia' if hr > HEART_RATE_SEVERE[1] else 'Bradycardia',
            'impact': 'high',
            'value': f'{hr} BPM',
        })
    elif hr > HEART_RATE_NORMAL[1] or hr < HEART_RATE_NORMAL[0]:
        score += 10
        factors.append({
            'factor': 'Heart rate outside normal range',
            'impact': 'medium',
            'value': f'{hr} BPM',
        })

    # Temperature.
    temp = patient.temperature
    if temp >= TEMPERATURE_SEVERE:
        score += 20
        factors.append({
            'factor': 'High fever',
            'impact': 'high',
            'value': f'{temp} F',
        })
    elif temp >= TEMPERATURE_NORMAL:
        score += 8
        factors.append({
            'factor': 'Mild fever',
            'impact': 'medium',
            'value': f'{temp} F',
        })

    # Blood pressure.
    sys_bp = patient.blood_pressure_systolic
    bp_reading = f'{sys_bp}/{patient.blood_pressure_diastolic} mmHg'
    if sys_bp > SYSTOLIC_SEVERE[1] or sys_bp < SYSTOLIC_SEVERE[0]:
        score += 20
        factors.append({
            'factor': 'Hypertensive crisis' if sys_bp > SYSTOLIC_SEVERE[1] else 'Hypotension',
            'impact': 'high',
            'value': bp_reading,
        })
    elif sys_bp > SYSTOLIC_NORMAL[1] or sys_bp < SYSTOLIC_NORMAL[0]:
        score += 8
        factors.append({
            'factor': 'Blood pressure outside normal range',
            'impact': 'medium',
            'value': bp_reading,
        })

    # Oxygen saturation — the strongest single warning sign.
    spo2 = patient.oxygen_saturation
    if spo2 < SPO2_SEVERE:
        score += 30
        factors.append({
            'factor': 'Severe hypoxaemia (SpO2 below 90%)',
            'impact': 'critical',
            'value': f'{spo2}%',
        })
    elif spo2 < SPO2_NORMAL:
        score += 15
        factors.append({
            'factor': 'Low oxygen saturation',
            'impact': 'high',
            'value': f'{spo2}%',
        })

    return score, factors


def assess_patient(patient):
    """Return a full risk assessment dict for one patient.

    Keys: ``risk_level``, ``risk_score`` (0-100), ``confidence`` (0-100),
    ``factors``, ``vital_anomalies`` (0-10) and ``vitals``.
    """
    score, factors = _vital_findings(patient)

    # Disease severity is clinician-entered on a 1-10 scale.
    score += patient.disease_severity * 3
    if patient.disease_severity >= 8:
        factors.append({
            'factor': 'Severe primary diagnosis',
            'impact': 'critical',
            'value': f'{patient.disease} (severity {patient.disease_severity}/10)',
        })

    # An emergency arrival is escalated by one band.
    if patient.is_emergency:
        score += 10
        factors.append({
            'factor': 'Emergency admission',
            'impact': 'high',
            'value': patient.disease,
        })

    score = max(0, min(100, score))

    if score >= RISK_CRITICAL_AT:
        risk_level = 'critical'
    elif score >= RISK_HIGH_AT:
        risk_level = 'high'
    elif score >= RISK_MEDIUM_AT:
        risk_level = 'medium'
    else:
        risk_level = 'low'

    # Confidence rises with the number of abnormal findings: a verdict backed
    # by five abnormal vitals is more trustworthy than one backed by none.
    confidence = min(98.0, 72.0 + len(factors) * 4.5 + score * 0.1)

    # Count of abnormal vitals, normalised to 0-10 for the priority score.
    vital_anomalies = min(10, len([f for f in factors if f['impact'] in ('high', 'critical')]) * 2)

    return {
        'risk_level': risk_level,
        'risk_score': round(score, 1),
        'confidence': round(confidence, 1),
        'factors': factors,
        'vital_anomalies': vital_anomalies,
        'vitals': {
            'heart_rate': patient.heart_rate,
            'temperature': patient.temperature,
            'blood_pressure': f'{patient.blood_pressure_systolic}/{patient.blood_pressure_diastolic}',
            'oxygen_saturation': patient.oxygen_saturation,
        },
    }


def apply_risk_assessment(patient, save=True):
    """Recalculate and store ``risk_level`` / ``risk_confidence``.

    Returns the assessment dict so callers can reuse the factor list.
    """
    assessment = assess_patient(patient)
    patient.risk_level = assessment['risk_level']
    patient.risk_confidence = assessment['confidence']
    if save:
        patient.save(update_fields=['risk_level', 'risk_confidence'])
    return assessment


def priority_score(patient, waiting_since=None, assessment=None):
    """Compute the 0-100 triage priority score for a patient.

    Combines four inputs exactly as described in the report: disease severity,
    the emergency flag, vital sign anomalies and how long the patient has
    already been waiting. Waiting time is capped so that a stable patient
    eventually gets seen, but never overtakes a deteriorating one.
    """
    if assessment is None:
        assessment = assess_patient(patient)

    score = patient.disease_severity * PRIORITY_SEVERITY_WEIGHT
    if patient.is_emergency:
        score += PRIORITY_EMERGENCY_BONUS
    score += assessment['vital_anomalies'] * PRIORITY_VITALS_WEIGHT

    if waiting_since is not None:
        waited_minutes = (timezone.now() - waiting_since).total_seconds() / 60.0
        score += min(PRIORITY_WAIT_CAP, waited_minutes / PRIORITY_WAIT_MINUTES_PER_POINT)

    return round(max(0.0, min(100.0, score)), 1)


def estimate_wait_minutes(position, doctors_available):
    """Estimate how long the patient at ``position`` in the queue will wait.

    Assumes an average 15-minute consultation shared across the doctors who
    are currently free; with nobody free the queue is served by one clinician.
    """
    effective_doctors = max(1, doctors_available)
    return int(max(1, round((position / effective_doctors) * 15)))
