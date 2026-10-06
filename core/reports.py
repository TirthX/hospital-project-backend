"""
Server-side report generation.

Produces the four print-ready reports named in the project report, in either
PDF (ReportLab) or CSV:

  hospital_summary   Hospital Operational Summary
  patient_census     Patient Census & Clinical Roster
  bed_utilization    Ward & Bed Utilisation
  ai_forecast        AI Forecast & Demand

Each report is defined once as a ``ReportDefinition`` holding a title, a
column list and a row builder. Both the PDF and the CSV writer consume that
same definition, so the two formats can never fall out of step.
"""
import csv
import io
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, List

from django.db.models import Avg, Count, F
from django.http import HttpResponse
from django.utils import timezone

from .models import (
    Alert, Bed, Doctor, HospitalMetric, Medicine, Nurse, Patient,
    PatientQueue, Prediction, Ward,
)

BRAND_NAVY = (0.06, 0.30, 0.51)
BRAND_TINT = (0.94, 0.97, 1.0)


@dataclass
class ReportDefinition:
    key: str
    title: str
    subtitle: str
    columns: List[str]
    build_rows: Callable[[], List[List[str]]]
    # Optional narrative paragraphs printed above the table in the PDF.
    build_notes: Callable[[], List[str]] = field(default=lambda: list)
    # Column widths as proportions; None lets ReportLab size them.
    column_ratios: List[float] = None


# ─── Row builders ─────────────────────────────────────────────────

def _hospital_summary_rows():
    active = Patient.objects.filter(is_discharged=False)
    today = timezone.localtime().date()

    total_beds = Bed.objects.count()
    occupied = Bed.objects.filter(status='occupied').count()
    icu_total = Bed.objects.filter(ward__ward_type='icu').count()
    icu_occupied = Bed.objects.filter(ward__ward_type='icu', status='occupied').count()

    avg_wait = round(
        PatientQueue.objects.filter(status='waiting')
        .aggregate(v=Avg('estimated_wait_minutes'))['v'] or 0, 1
    )

    def pct(part, whole):
        return f'{round(part / whole * 100, 1)}%' if whole else 'n/a'

    return [
        ['Active patients', str(active.count())],
        ['Admissions today', str(Patient.objects.filter(admitted_at__date=today).count())],
        ['Discharges today', str(Patient.objects.filter(discharged_at__date=today).count())],
        ['Emergency patients', str(active.filter(is_emergency=True).count())],
        ['Critical-risk patients', str(active.filter(risk_level='critical').count())],
        ['High-risk patients', str(active.filter(risk_level='high').count())],
        ['Total beds', str(total_beds)],
        ['Beds occupied', f'{occupied} ({pct(occupied, total_beds)})'],
        ['Beds available', str(Bed.objects.filter(status='available').count())],
        ['Beds under maintenance', str(Bed.objects.filter(status='maintenance').count())],
        ['ICU beds occupied', f'{icu_occupied} of {icu_total} ({pct(icu_occupied, icu_total)})'],
        ['Doctors on duty', str(Doctor.objects.exclude(status='off_duty').count())],
        ['Nurses on duty', str(Nurse.objects.exclude(status='off_duty').count())],
        ['Patients waiting in triage', str(PatientQueue.objects.filter(status='waiting').count())],
        ['Average waiting time', f'{avg_wait} min'],
        ['Unresolved critical alerts',
         str(Alert.objects.filter(level='critical', is_resolved=False).count())],
        ['Unresolved warnings',
         str(Alert.objects.filter(level='warning', is_resolved=False).count())],
        ['Medicines below minimum stock',
         str(Medicine.objects.filter(stock__lte=F('min_stock')).count())],
        ['Medicines out of stock', str(Medicine.objects.filter(stock__lte=0).count())],
    ]


def _hospital_summary_notes():
    active = Patient.objects.filter(is_discharged=False).count()
    icu_total = Bed.objects.filter(ward__ward_type='icu').count()
    icu_occupied = Bed.objects.filter(ward__ward_type='icu', status='occupied').count()
    icu_pct = round(icu_occupied / icu_total * 100, 1) if icu_total else 0

    notes = [f'The hospital currently holds {active} active patients with ICU '
             f'occupancy at {icu_pct}%.']
    if icu_pct >= 85:
        notes.append('ICU occupancy is above the 85% safety threshold. Elective '
                     'surgery rescheduling is recommended.')
    return notes


def _patient_census_rows():
    patients = (
        Patient.objects.filter(is_discharged=False)
        .select_related('attending_doctor', 'assigned_bed', 'assigned_bed__ward')
        .order_by('-risk_level', 'name')
    )
    rows = []
    for patient in patients:
        bed = getattr(patient, 'assigned_bed', None)
        rows.append([
            patient.name,
            str(patient.age),
            patient.gender,
            patient.disease,
            f'{patient.disease_severity}/10',
            patient.risk_level.title(),
            f'{patient.heart_rate}',
            f'{patient.oxygen_saturation}%',
            f'{patient.blood_pressure_systolic}/{patient.blood_pressure_diastolic}',
            bed.bed_number if bed else 'Unassigned',
            patient.attending_doctor.name if patient.attending_doctor else 'Unassigned',
            timezone.localtime(patient.admitted_at).strftime('%d-%m-%Y %H:%M'),
        ])
    return rows


def _patient_census_notes():
    active = Patient.objects.filter(is_discharged=False)
    by_risk = dict(
        active.values_list('risk_level').annotate(n=Count('id'))
    )
    breakdown = ', '.join(
        f'{level.title()}: {by_risk.get(level, 0)}'
        for level in ('critical', 'high', 'medium', 'low')
    )
    unassigned = active.filter(attending_doctor__isnull=True).count()

    notes = [f'Risk distribution across {active.count()} active patients — {breakdown}.']
    if unassigned:
        notes.append(f'{unassigned} patients have no attending doctor recorded.')
    return notes


def _bed_utilization_rows():
    rows = []
    for ward in Ward.objects.order_by('floor', 'name'):
        beds = ward.beds.all()
        total = beds.count()
        occupied = beds.filter(status='occupied').count()
        available = beds.filter(status='available').count()
        maintenance = beds.filter(status='maintenance').count()
        reserved = beds.filter(status='reserved').count()
        pct = round(occupied / total * 100, 1) if total else 0.0

        rows.append([
            ward.name,
            ward.get_ward_type_display(),
            f'Floor {ward.floor}',
            str(total),
            str(occupied),
            str(available),
            str(maintenance),
            str(reserved),
            f'{pct}%',
            'Critical' if pct >= 90 else 'High' if pct >= 85 else 'Normal',
        ])

    # Hospital-wide total row.
    all_beds = Bed.objects.all()
    total = all_beds.count()
    occupied = all_beds.filter(status='occupied').count()
    rows.append([
        'ALL WARDS', '—', '—', str(total), str(occupied),
        str(all_beds.filter(status='available').count()),
        str(all_beds.filter(status='maintenance').count()),
        str(all_beds.filter(status='reserved').count()),
        f'{round(occupied / total * 100, 1) if total else 0}%',
        '—',
    ])
    return rows


def _bed_utilization_notes():
    pressured = [
        f'{w.name} ({w.occupancy_percentage}%)'
        for w in Ward.objects.all() if w.capacity and w.occupancy_percentage >= 85
    ]
    if pressured:
        return ['Wards at or above 85% occupancy: ' + ', '.join(pressured) + '.']
    return ['No ward is currently above its 85% occupancy threshold.']


def _ai_forecast_rows():
    from ai_engine.ml import forecast_next_24h

    forecast = forecast_next_24h()
    if not forecast.get('available'):
        # Fall back to whatever forecasts were last persisted, so the report
        # still produces real stored numbers rather than a stub table.
        return [
            [str(p.date), p.get_metric_type_display(), str(round(p.predicted_value, 2)),
             f'{p.confidence}%', p.trend, p.recommendation[:60]]
            for p in Prediction.objects.order_by('date')[:48]
        ] or [['—', 'Models not loaded', '—', '—', '—',
               'Run train_models.py to generate forecasts']]

    rows = []
    for point in forecast['points']:
        rows.append([
            point['hour_label'],
            f"+{point['hours_ahead']}h",
            f"{point['predicted_inflow']:.2f}",
            f"{point['predicted_icu_occupancy']:.2f}",
            f"{point['predicted_icu_percentage']}%",
            f"{point['confidence']}%",
        ])
    return rows


def _ai_forecast_notes():
    from ai_engine.ml import forecast_next_24h, model_status

    status = model_status()
    if not status.get('available'):
        return ['Forecasting models are not loaded. Run '
                'backend/ai_engine/train_models.py to train them.']

    forecast = forecast_next_24h()
    notes = [
        f"Model: RandomForestRegressor with {status.get('n_estimators')} trees, "
        f"trained on {status.get('trained_on_rows'):,} hourly observations."
    ]

    icu_metrics = status.get('icu_occupancy', {})
    inflow_metrics = status.get('patient_inflow', {})
    if icu_metrics and inflow_metrics:
        notes.append(
            f"Held-out accuracy — ICU occupancy MAE {icu_metrics.get('mae')} beds "
            f"(R2 {icu_metrics.get('r2')}); patient inflow MAE "
            f"{inflow_metrics.get('mae')} patients/hour."
        )

    if forecast.get('available'):
        summary = forecast['summary']
        notes.append(
            f"Next 24 hours: {summary['total_predicted_admissions']} projected "
            f"admissions, peak arrival rate {summary['peak_inflow']}/hour at "
            f"{summary['peak_inflow_at']}, peak ICU occupancy "
            f"{summary['peak_icu_percentage']}% at {summary['peak_icu_at']} "
            f"({summary['trend']} trend)."
        )
    return notes


# ─── Report registry ──────────────────────────────────────────────

REPORTS = {
    'hospital_summary': ReportDefinition(
        key='hospital_summary',
        title='Hospital Operational Summary',
        subtitle='Live snapshot of capacity, staffing, triage and inventory',
        columns=['Metric', 'Value'],
        build_rows=_hospital_summary_rows,
        build_notes=_hospital_summary_notes,
        column_ratios=[0.62, 0.38],
    ),
    'patient_census': ReportDefinition(
        key='patient_census',
        title='Patient Census & Clinical Roster',
        subtitle='All currently admitted patients with vitals and assignments',
        columns=['Patient', 'Age', 'Gender', 'Diagnosis', 'Severity', 'Risk',
                 'HR', 'SpO2', 'BP', 'Bed', 'Attending Doctor', 'Admitted'],
        build_rows=_patient_census_rows,
        build_notes=_patient_census_notes,
        column_ratios=[0.13, 0.04, 0.06, 0.13, 0.06, 0.07, 0.04,
                       0.05, 0.07, 0.06, 0.15, 0.14],
    ),
    'bed_utilization': ReportDefinition(
        key='bed_utilization',
        title='Ward & Bed Utilisation',
        subtitle='Occupancy of every ward with bed state breakdown',
        columns=['Ward', 'Type', 'Floor', 'Total', 'Occupied', 'Available',
                 'Maintenance', 'Reserved', 'Occupancy', 'Status'],
        build_rows=_bed_utilization_rows,
        build_notes=_bed_utilization_notes,
        column_ratios=[0.19, 0.15, 0.07, 0.07, 0.09, 0.09, 0.11, 0.09, 0.09, 0.09],
    ),
    'ai_forecast': ReportDefinition(
        key='ai_forecast',
        title='AI Forecast & Demand',
        subtitle='24-hour Random Forest projection of arrivals and ICU load',
        columns=['Hour', 'Horizon', 'Predicted Arrivals/h',
                 'Predicted ICU Beds', 'ICU Occupancy', 'Confidence'],
        build_rows=_ai_forecast_rows,
        build_notes=_ai_forecast_notes,
        column_ratios=[0.12, 0.13, 0.22, 0.2, 0.18, 0.15],
    ),
}

# Legacy keys accepted so older frontend builds keep working.
REPORT_ALIASES = {
    'patient_report': 'patient_census',
    'prediction_report': 'ai_forecast',
    'bed_report': 'bed_utilization',
    'summary': 'hospital_summary',
}


def resolve_report(report_type):
    """Return the ReportDefinition for a key, or None if unknown."""
    key = REPORT_ALIASES.get(report_type, report_type)
    return REPORTS.get(key)


def available_reports():
    """Catalogue for the Reports page."""
    return [
        {
            'key': definition.key,
            'title': definition.title,
            'subtitle': definition.subtitle,
            'columns': definition.columns,
            'formats': ['pdf', 'csv'],
        }
        for definition in REPORTS.values()
    ]


# ─── CSV writer ───────────────────────────────────────────────────

def generate_csv(definition):
    response = HttpResponse(content_type='text/csv')
    filename = f'{definition.key}_{date.today().isoformat()}.csv'
    response['Content-Disposition'] = f'attachment; filename="{filename}"'

    writer = csv.writer(response)
    writer.writerow([definition.title])
    writer.writerow([f'Generated {timezone.localtime().strftime("%d-%m-%Y %H:%M")}'])
    writer.writerow([])
    writer.writerow(definition.columns)
    writer.writerows(definition.build_rows())

    notes = definition.build_notes()
    if notes:
        writer.writerow([])
        writer.writerow(['Notes'])
        for note in notes:
            writer.writerow([note])

    return response


# ─── PDF writer ───────────────────────────────────────────────────

def generate_pdf(definition):
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
    )

    # Wide tables are unreadable on portrait A4, so those reports print
    # landscape while the two-column summary stays portrait.
    is_wide = len(definition.columns) > 4
    page_size = landscape(A4) if is_wide else A4

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=page_size,
        leftMargin=12 * mm, rightMargin=12 * mm,
        topMargin=14 * mm, bottomMargin=14 * mm,
        title=definition.title,
        author='Hospital Intelligence Platform',
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        'ReportTitle', parent=styles['Title'],
        fontSize=17, leading=21, alignment=TA_LEFT,
        textColor=colors.Color(*BRAND_NAVY), spaceAfter=2,
    )
    subtitle_style = ParagraphStyle(
        'ReportSubtitle', parent=styles['Normal'],
        fontSize=9.5, leading=13, textColor=colors.Color(0.35, 0.4, 0.47),
    )
    meta_style = ParagraphStyle(
        'ReportMeta', parent=styles['Normal'],
        fontSize=8, leading=11, textColor=colors.Color(0.45, 0.5, 0.56),
    )
    note_style = ParagraphStyle(
        'ReportNote', parent=styles['Normal'],
        fontSize=8.5, leading=12, textColor=colors.Color(0.2, 0.25, 0.32),
    )
    cell_style = ParagraphStyle(
        'Cell', parent=styles['Normal'], fontSize=7.2, leading=9,
    )

    elements = [
        Paragraph('HOSPITAL INTELLIGENCE PLATFORM', meta_style),
        Paragraph(definition.title, title_style),
        Paragraph(definition.subtitle, subtitle_style),
        Spacer(1, 4),
        Paragraph(
            f'Generated {timezone.localtime().strftime("%d %B %Y at %H:%M")} '
            f'&nbsp;·&nbsp; Report ID {definition.key.upper()}-'
            f'{timezone.now().strftime("%Y%m%d%H%M")}',
            meta_style,
        ),
        Spacer(1, 10),
    ]

    notes = definition.build_notes()
    for note in notes:
        elements.append(Paragraph(f'• {note}', note_style))
    if notes:
        elements.append(Spacer(1, 10))

    rows = definition.build_rows()
    available_width = page_size[0] - doc.leftMargin - doc.rightMargin

    # Long text is wrapped in Paragraphs so wide tables do not overflow.
    if is_wide:
        header = [Paragraph(f'<b>{c}</b>', cell_style) for c in definition.columns]
        body = [[Paragraph(str(cell), cell_style) for cell in row] for row in rows]
    else:
        header = definition.columns
        body = [[str(cell) for cell in row] for row in rows]

    col_widths = None
    if definition.column_ratios:
        col_widths = [available_width * ratio for ratio in definition.column_ratios]

    table = Table([header] + body, colWidths=col_widths, repeatRows=1)
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.Color(*BRAND_NAVY)),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 7.6 if is_wide else 9.5),
        ('TOPPADDING', (0, 0), (-1, 0), 7),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 7),
        ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 1), (-1, -1), 7.2 if is_wide else 9),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('ALIGN', (1, 1), (-1, -1), 'LEFT' if is_wide else 'RIGHT'),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1),
         [colors.white, colors.Color(*BRAND_TINT)]),
        ('GRID', (0, 0), (-1, -1), 0.4, colors.Color(0.78, 0.84, 0.90)),
        ('TOPPADDING', (0, 1), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 1), (-1, -1), 4),
        ('LEFTPADDING', (0, 0), (-1, -1), 5),
        ('RIGHTPADDING', (0, 0), (-1, -1), 5),
    ]))

    elements.append(table)
    elements.append(Spacer(1, 12))
    elements.append(Paragraph(
        f'{len(rows)} row(s). Generated automatically by the Hospital '
        'Intelligence Platform reporting engine.',
        meta_style,
    ))

    def _footer(canvas, document):
        canvas.saveState()
        canvas.setFont('Helvetica', 7.5)
        canvas.setFillColorRGB(0.5, 0.55, 0.6)
        canvas.drawString(doc.leftMargin, 8 * mm,
                          'Hospital Intelligence Platform — confidential')
        canvas.drawRightString(page_size[0] - doc.rightMargin, 8 * mm,
                               f'Page {document.page}')
        canvas.restoreState()

    doc.build(elements, onFirstPage=_footer, onLaterPages=_footer)
    buffer.seek(0)

    response = HttpResponse(buffer.read(), content_type='application/pdf')
    filename = f'{definition.key}_{date.today().isoformat()}.pdf'
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


def generate_report(report_type, report_format='pdf'):
    """Build a report response, or None if the type/format is unknown."""
    definition = resolve_report(report_type)
    if definition is None:
        return None
    if report_format == 'csv':
        return generate_csv(definition)
    if report_format == 'pdf':
        return generate_pdf(definition)
    return None
