"""
Rule-based operational assistant.

The report is explicit that this is rule-based natural-language matching, not
a large language model. The implementation is a small intent classifier:

  1. normalise the question (lowercase, strip punctuation, expand shorthand)
  2. score every intent by counting matched keywords and phrases
  3. run the best-scoring intent's handler against the live database

Scoring rather than a chain of ``if`` statements matters: "how many ICU beds
are free" contains both "icu" and "bed", and a first-match chain answers
whichever branch happens to be written first. Weighted scoring lets the more
specific intent win, and lets the assistant say honestly when it has not
understood instead of guessing.
"""
import re

from django.db.models import Avg, Count, F, Q
from django.utils import timezone

from .models import (
    Alert, Bed, Doctor, Medicine, Nurse, Patient, PatientQueue, Ward,
)

# Shorthand users actually type, expanded before matching.
SYNONYMS = {
    'icu': 'icu intensive care',
    'er': 'emergency',
    'ed': 'emergency',
    'ot': 'operation theatre',
    'docs': 'doctors',
    'bp': 'blood pressure',
    'meds': 'medicine',
    'pts': 'patients',
}


def _normalise(text):
    """Lowercase, strip punctuation and expand shorthand into full words."""
    cleaned = re.sub(r'[^a-z0-9\s]', ' ', (text or '').lower())
    words = [w for w in cleaned.split() if w]
    expanded = []
    for word in words:
        expanded.append(word)
        if word in SYNONYMS:
            expanded.extend(SYNONYMS[word].split())
    return expanded


# ─── Intent handlers ──────────────────────────────────────────────

def _icu_beds():
    total = Bed.objects.filter(ward__ward_type='icu').count()
    if not total:
        return 'No ICU beds are configured in the system yet.', []

    occupied = Bed.objects.filter(ward__ward_type='icu', status='occupied').count()
    available = Bed.objects.filter(ward__ward_type='icu', status='available').count()
    pct = round(occupied / total * 100, 1)

    text = (f'ICU: {available} of {total} beds are free right now '
            f'({occupied} occupied, {pct}% occupancy).')

    # Free + occupied need not equal the total: beds can also be reserved or
    # under maintenance. Saying so avoids an answer that looks like it cannot
    # add up.
    unavailable = total - occupied - available
    if unavailable > 0:
        blocked = Bed.objects.filter(ward__ward_type='icu').exclude(
            status__in=['occupied', 'available']
        ).values_list('status', flat=True)
        breakdown = {}
        for state in blocked:
            breakdown[state] = breakdown.get(state, 0) + 1
        detail = ', '.join(f'{n} {state.replace("_", " ")}'
                           for state, n in breakdown.items())
        text += (f' The remaining {unavailable} '
                 f'{"bed is" if unavailable == 1 else "beds are"} '
                 f'not admissible ({detail}).')

    if pct >= 85:
        text += ' This is above the 85% safety threshold.'
    return text, [{'label': 'Open Floor Map', 'route': '/floor-map'}]


def _bed_availability():
    total = Bed.objects.count()
    if not total:
        return 'No beds are configured in the system yet.', []

    available = Bed.objects.filter(status='available').count()
    occupied = Bed.objects.filter(status='occupied').count()

    by_ward = (
        Ward.objects.annotate(
            free=Count('beds', filter=Q(beds__status='available'))
        ).order_by('-free')[:3]
    )
    detail = ', '.join(f'{w.name}: {w.free}' for w in by_ward if w.free)

    text = (f'{available} of {total} beds are available '
            f'({occupied} occupied, {round(occupied / total * 100, 1)}% full).')
    if detail:
        text += f' Most free capacity — {detail}.'
    return text, [{'label': 'Manage Beds', 'route': '/beds'}]


def _todays_admissions():
    today = timezone.localtime().date()
    admissions = Patient.objects.filter(admitted_at__date=today).count()
    discharges = Patient.objects.filter(discharged_at__date=today).count()
    emergency = Patient.objects.filter(
        admitted_at__date=today, is_emergency=True
    ).count()

    return (
        f'Today: {admissions} admissions ({emergency} through emergency) and '
        f'{discharges} discharges. Net change {admissions - discharges:+d} patients.',
        [{'label': 'View Patients', 'route': '/patients'}],
    )


def _critical_patients():
    critical = Patient.objects.filter(
        is_discharged=False, risk_level='critical'
    ).select_related('assigned_bed')
    count = critical.count()

    if not count:
        high = Patient.objects.filter(is_discharged=False, risk_level='high').count()
        text = 'No patients are currently in critical condition.'
        if high:
            text += f' {high} are rated high risk and worth watching.'
        return text, [{'label': 'Patient Risk Board', 'route': '/patient-risk'}]

    lines = []
    for patient in critical[:5]:
        bed = getattr(patient, 'assigned_bed', None)
        lines.append(f'{patient.name} ({patient.disease}, SpO2 '
                     f'{patient.oxygen_saturation}%, '
                     f'{bed.bed_number if bed else "no bed"})')

    text = f'{count} patient{"s" if count > 1 else ""} in critical condition: ' + '; '.join(lines)
    if count > 5:
        text += f' — and {count - 5} more.'
    return text, [{'label': 'Patient Risk Board', 'route': '/patient-risk'}]


def _busiest_ward():
    wards = [w for w in Ward.objects.all() if w.capacity]
    if not wards:
        return 'No wards are configured yet.', []

    busiest = max(wards, key=lambda w: w.occupancy_percentage)
    quietest = min(wards, key=lambda w: w.occupancy_percentage)

    return (
        f'Busiest ward: {busiest.name} at {busiest.occupancy_percentage}% '
        f'({busiest.current_patients} of {busiest.capacity} beds). '
        f'Quietest: {quietest.name} at {quietest.occupancy_percentage}%.',
        [{'label': 'Open Floor Map', 'route': '/floor-map'}],
    )


def _staff_on_duty():
    doctors_on = Doctor.objects.exclude(status='off_duty').count()
    doctors_free = Doctor.objects.filter(status='available').count()
    nurses_on = Nurse.objects.exclude(status='off_duty').count()
    active_patients = Patient.objects.filter(is_discharged=False).count()

    ratio = f'1:{round(active_patients / nurses_on, 1)}' if nurses_on else 'n/a'

    return (
        f'{doctors_on} doctors on duty ({doctors_free} free right now) and '
        f'{nurses_on} nurses on duty. Nurse-to-patient ratio is {ratio} '
        f'for {active_patients} active patients.',
        [{'label': 'Duty Roster', 'route': '/staff'}],
    )


def _medicine_stock():
    out_of_stock = list(
        Medicine.objects.filter(stock__lte=0).values_list('name', flat=True)[:5]
    )
    low = list(
        Medicine.objects.filter(stock__gt=0, stock__lte=F('min_stock'))
        .values_list('name', flat=True)[:5]
    )
    near_expiry = Medicine.objects.filter(
        expiry_date__lte=timezone.localtime().date() + timezone.timedelta(days=30),
        expiry_date__gt=timezone.localtime().date(),
    ).count()

    if not out_of_stock and not low and not near_expiry:
        return 'Pharmacy stock is healthy — nothing below minimum or near expiry.', []

    parts = []
    if out_of_stock:
        parts.append(f'out of stock: {", ".join(out_of_stock)}')
    if low:
        parts.append(f'below minimum: {", ".join(low)}')
    if near_expiry:
        parts.append(f'{near_expiry} expiring within 30 days')

    return ('Pharmacy needs attention — ' + '; '.join(parts) + '.',
            [{'label': 'Medicine Inventory', 'route': '/medicines'}])


def _active_alerts():
    critical = Alert.objects.filter(level='critical', is_resolved=False).count()
    warnings = Alert.objects.filter(level='warning', is_resolved=False).count()
    info = Alert.objects.filter(level='info', is_resolved=False).count()

    if not (critical or warnings or info):
        return 'No unresolved alerts — all monitored thresholds are within limits.', []

    latest = Alert.objects.filter(is_resolved=False).order_by('-timestamp').first()
    return (
        f'{critical} critical, {warnings} warning and {info} info alerts are open. '
        f'Most recent: "{latest.message}" from {latest.source}.',
        [{'label': 'Alert Centre', 'route': '/alerts'}],
    )


def _queue_status():
    waiting = PatientQueue.objects.filter(status='waiting')
    count = waiting.count()

    if not count:
        in_consult = PatientQueue.objects.filter(status='in_consultation').count()
        return (f'Nobody is waiting in triage. {in_consult} patients are in '
                'consultation.'), [{'label': 'Triage Queue', 'route': '/queue'}]

    avg_wait = round(waiting.aggregate(v=Avg('estimated_wait_minutes'))['v'] or 0, 1)
    long_waits = waiting.filter(estimated_wait_minutes__gt=45).count()
    next_up = waiting.select_related('patient').order_by(
        '-priority_score', 'waiting_since'
    ).first()

    text = (f'{count} patients waiting, average estimated wait {avg_wait} minutes. '
            f'Next to be seen: {next_up.patient.name} '
            f'(priority {next_up.priority_score}/100).')
    if long_waits:
        text += f' {long_waits} have waited beyond 45 minutes.'
    return text, [{'label': 'Triage Queue', 'route': '/queue'}]


def _emergency_status():
    emergency = Patient.objects.filter(is_discharged=False, is_emergency=True)
    count = emergency.count()

    if not count:
        return 'No emergency patients are currently active.', []

    critical = emergency.filter(risk_level='critical').count()
    emergency_beds_free = Bed.objects.filter(
        ward__ward_type='emergency', status='available'
    ).count()

    return (
        f'{count} active emergency patients ({critical} critical). '
        f'{emergency_beds_free} emergency beds are free.',
        [{'label': 'Emergency Board', 'route': '/emergency'}],
    )


def _top_diseases():
    top = list(
        Patient.objects.filter(is_discharged=False)
        .values('disease').annotate(n=Count('id')).order_by('-n')[:5]
    )
    if not top:
        return 'There are no active patients to analyse.', []

    summary = ', '.join(f"{d['disease']} ({d['n']})" for d in top)
    return (f'Most common diagnoses among active patients: {summary}.',
            [{'label': 'Disease Analytics', 'route': '/disease-analytics'}])


def _patient_count():
    active = Patient.objects.filter(is_discharged=False)
    total = Patient.objects.count()
    by_risk = dict(active.values_list('risk_level').annotate(n=Count('id')))

    breakdown = ', '.join(
        f'{level.title()} {by_risk.get(level, 0)}'
        for level in ('critical', 'high', 'medium', 'low')
    )
    return (
        f'{active.count()} active patients ({total} on record including '
        f'discharged). Risk breakdown — {breakdown}.',
        [{'label': 'View Patients', 'route': '/patients'}],
    )


def _forecast():
    from ai_engine.ml import forecast_next_24h

    forecast = forecast_next_24h()
    if not forecast.get('available'):
        return ('The forecasting models are not loaded, so I cannot give a '
                'prediction right now.'), []

    summary = forecast['summary']
    return (
        f"Next 24 hours: about {summary['total_predicted_admissions']:.0f} "
        f"admissions expected, peaking near {summary['peak_inflow_at']} at "
        f"{summary['peak_inflow']} patients/hour. ICU is projected to reach "
        f"{summary['peak_icu_percentage']}% around {summary['peak_icu_at']} "
        f"({summary['trend']} trend).",
        [{'label': 'AI Forecast', 'route': '/ai-forecast'}],
    )


def _occupancy_overview():
    total = Bed.objects.count()
    occupied = Bed.objects.filter(status='occupied').count()
    icu_total = Bed.objects.filter(ward__ward_type='icu').count()
    icu_occupied = Bed.objects.filter(ward__ward_type='icu', status='occupied').count()

    return (
        f'Hospital-wide occupancy is '
        f'{round(occupied / total * 100, 1) if total else 0}% '
        f'({occupied}/{total} beds). ICU is at '
        f'{round(icu_occupied / icu_total * 100, 1) if icu_total else 0}%.',
        [{'label': 'Command Center', 'route': '/dashboard'}],
    )


def _help():
    return (
        'I can answer questions about live hospital state. Try:\n'
        '• How many ICU beds are available?\n'
        '• How many beds are free?\n'
        '• Who are the critical patients?\n'
        '• Which ward is busiest?\n'
        "• What are today's admissions?\n"
        '• How long is the queue?\n'
        '• Which medicines are low on stock?\n'
        '• How many staff are on duty?\n'
        '• What is the forecast for tomorrow?\n'
        '• Are there any active alerts?',
        [],
    )


# ─── Intent table ─────────────────────────────────────────────────
# Each intent lists strong keywords (weight 2), supporting keywords
# (weight 1) and phrases (weight 3) that mark it unambiguously.

INTENTS = [
    {
        'name': 'icu_beds',
        'handler': _icu_beds,
        'strong': ['icu', 'intensive'],
        'support': ['bed', 'beds', 'available', 'free', 'capacity', 'occupancy'],
        'phrases': ['icu bed', 'icu beds', 'intensive care bed'],
    },
    {
        'name': 'emergency',
        'handler': _emergency_status,
        'strong': ['emergency', 'trauma', 'casualty'],
        'support': ['patients', 'active', 'how', 'many'],
        'phrases': ['emergency patients', 'emergency room'],
    },
    {
        'name': 'critical_patients',
        'handler': _critical_patients,
        'strong': ['critical', 'deteriorating', 'unstable', 'serious'],
        'support': ['patient', 'patients', 'who', 'risk', 'condition'],
        'phrases': ['critical patient', 'critical patients', 'high risk patients'],
    },
    {
        'name': 'queue',
        'handler': _queue_status,
        'strong': ['queue', 'waiting', 'wait', 'triage'],
        'support': ['time', 'long', 'how', 'many', 'next'],
        'phrases': ['waiting time', 'how long is the queue', 'triage queue'],
    },
    {
        'name': 'medicine',
        'handler': _medicine_stock,
        'strong': ['medicine', 'medicines', 'stock', 'pharmacy', 'inventory', 'drug'],
        'support': ['low', 'out', 'expiry', 'expire', 'reorder'],
        'phrases': ['low stock', 'out of stock', 'medicine stock'],
    },
    {
        'name': 'staff',
        'handler': _staff_on_duty,
        'strong': ['doctor', 'doctors', 'nurse', 'nurses', 'staff', 'roster', 'shift'],
        'support': ['duty', 'available', 'on', 'how', 'many', 'ratio'],
        'phrases': ['on duty', 'staff on duty', 'how many doctors'],
    },
    {
        'name': 'admissions',
        'handler': _todays_admissions,
        'strong': ['admission', 'admissions', 'admitted', 'discharge', 'discharges'],
        'support': ['today', 'many', 'how'],
        'phrases': ['today admissions', 'admissions today', 'how many admitted'],
    },
    {
        'name': 'busiest_ward',
        'handler': _busiest_ward,
        'strong': ['busiest', 'busy', 'ward'],
        'support': ['which', 'most', 'full', 'crowded'],
        'phrases': ['which ward is busiest', 'busiest ward'],
    },
    {
        'name': 'alerts',
        'handler': _active_alerts,
        'strong': ['alert', 'alerts', 'warning', 'warnings', 'notification'],
        'support': ['active', 'open', 'unresolved', 'any'],
        'phrases': ['active alerts', 'any alerts'],
    },
    {
        'name': 'forecast',
        'handler': _forecast,
        'strong': ['forecast', 'predict', 'prediction', 'tomorrow', 'expect', 'projected'],
        'support': ['next', 'hours', 'future', 'will', 'demand'],
        'phrases': ['next 24 hours', 'what is the forecast', 'predicted admissions'],
    },
    {
        'name': 'diseases',
        'handler': _top_diseases,
        'strong': ['disease', 'diseases', 'diagnosis', 'diagnoses', 'illness'],
        'support': ['top', 'common', 'most', 'which'],
        'phrases': ['top diseases', 'most common disease'],
    },
    {
        'name': 'beds',
        'handler': _bed_availability,
        'strong': ['bed', 'beds'],
        'support': ['available', 'free', 'empty', 'many', 'vacant'],
        'phrases': ['beds available', 'available beds', 'free beds'],
    },
    {
        'name': 'occupancy',
        'handler': _occupancy_overview,
        'strong': ['occupancy', 'utilisation', 'utilization', 'full'],
        'support': ['hospital', 'overall', 'total', 'percentage'],
        'phrases': ['hospital occupancy', 'overall occupancy'],
    },
    {
        'name': 'patient_count',
        'handler': _patient_count,
        'strong': ['patients', 'patient'],
        'support': ['total', 'count', 'many', 'active', 'how'],
        'phrases': ['how many patients', 'total patients'],
    },
    {
        'name': 'help',
        'handler': _help,
        'strong': ['help', 'commands'],
        'support': ['what', 'can', 'you', 'do', 'ask', 'options'],
        'phrases': ['what can you do', 'help me'],
    },
]

# A question must reach this score for the assistant to claim it understood.
CONFIDENCE_FLOOR = 2


def _score_intent(intent, words, raw_text):
    """Weighted keyword/phrase score for one intent."""
    word_set = set(words)
    score = 0

    for phrase in intent.get('phrases', []):
        if phrase in raw_text:
            score += 3

    score += 2 * len(word_set & set(intent.get('strong', [])))
    score += 1 * len(word_set & set(intent.get('support', [])))
    return score


def answer(message):
    """Answer one question. Returns a dict for the chat API response."""
    words = _normalise(message)
    raw_text = ' '.join(words)

    if not words:
        return {
            'response': 'Ask me something about the hospital — type "help" for examples.',
            'intent': 'empty',
            'confidence': 0,
            'actions': [],
        }

    scored = sorted(
        ((_score_intent(i, words, raw_text), i) for i in INTENTS),
        key=lambda pair: pair[0],
        reverse=True,
    )
    best_score, best_intent = scored[0]

    if best_score < CONFIDENCE_FLOOR:
        suggestions, _ = _help()
        return {
            'response': ("I did not understand that one. I answer questions about "
                         "live hospital state.\n\n" + suggestions),
            'intent': 'unknown',
            'confidence': 0,
            'actions': [],
        }

    text, actions = best_intent['handler']()

    # Report confidence as a percentage of a comfortably strong match.
    confidence = min(99, int(best_score / 6 * 100))

    return {
        'response': text,
        'intent': best_intent['name'],
        'confidence': confidence,
        'actions': actions,
    }


def suggested_questions():
    """Starter prompts shown in the chat widget before the first message."""
    return [
        'How many ICU beds are available?',
        'Who are the critical patients?',
        'How long is the triage queue?',
        'Which medicines are low on stock?',
        "What are today's admissions?",
        'What is the forecast for the next 24 hours?',
    ]
