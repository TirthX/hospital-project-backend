"""
Automated test suite.

Covers the eighteen test cases in Table 8.3 of the project report, plus the
clinical scoring rules the rest of the system depends on. Each test class
maps to one area, and TC numbers are named in the docstrings so a result here
can be traced back to the report.

Run:  python manage.py test
"""
from datetime import date, timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from automation.engine import run_alert_engine, threshold_summary
from core.clinical import assess_patient, priority_score
from core.models import (
    Alert, Appointment, Bed, Doctor, HospitalUser, Medicine, MedicineLog,
    Nurse, Patient, PatientQueue, PatientTimeline, Ward,
)


class HospitalTestCase(TestCase):
    """Shared fixture: a small hospital with all four user roles."""

    def setUp(self):
        self.admin = HospitalUser.objects.create_user(
            username='t_admin', password='admin-pass-123', role='admin',
            first_name='Test', last_name='Admin',
        )
        self.doctor_user = HospitalUser.objects.create_user(
            username='t_doctor', password='doctor-pass-123', role='doctor',
        )
        self.nurse_user = HospitalUser.objects.create_user(
            username='t_nurse', password='nurse-pass-123', role='nurse',
        )
        self.receptionist = HospitalUser.objects.create_user(
            username='t_reception', password='reception-pass-123', role='receptionist',
        )

        self.icu = Ward.objects.create(
            name='Test ICU', ward_type='icu', floor=3, capacity=4
        )
        self.general = Ward.objects.create(
            name='Test General', ward_type='general', floor=2, capacity=6
        )
        self.emergency = Ward.objects.create(
            name='Test Emergency', ward_type='emergency', floor=1, capacity=4
        )

        self.icu_beds = [
            Bed.objects.create(bed_number=f'TICU-{i}', ward=self.icu)
            for i in range(1, 5)
        ]
        self.general_beds = [
            Bed.objects.create(bed_number=f'TGEN-{i}', ward=self.general)
            for i in range(1, 7)
        ]

        self.doctor = Doctor.objects.create(
            name='Test Physician', specialization='General Medicine',
            department='General', status='available', shift='morning',
        )
        self.nurse = Nurse.objects.create(
            name='Test Nurse', department='General',
            status='available', shift='morning',
        )

        self.medicine = Medicine.objects.create(
            name='Test Paracetamol', category='painkiller',
            stock=50, min_stock=10,
            expiry_date=date.today() + timedelta(days=365),
            supplier='Test Supplier', unit_price=2.5,
        )

    # ── helpers ──────────────────────────────────────────────────

    def client_for(self, username, password):
        """Return an APIClient carrying that user's JWT."""
        client = APIClient()
        response = client.post(
            '/api/login/', {'username': username, 'password': password}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK,
                         f'Login failed for {username}: {response.data}')
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access']}")
        return client

    def admin_client(self):
        return self.client_for('t_admin', 'admin-pass-123')

    def make_patient(self, **overrides):
        """Create a patient with stable baseline vitals."""
        defaults = {
            'name': 'Stable Patient', 'age': 40, 'gender': 'Male',
            'disease': 'Diabetes', 'disease_severity': 3,
            'heart_rate': 75, 'temperature': 98.4,
            'blood_pressure_systolic': 118, 'blood_pressure_diastolic': 76,
            'oxygen_saturation': 98.0, 'department': 'General',
        }
        defaults.update(overrides)
        return Patient.objects.create(**defaults)


# ─── TC-01 to TC-04: authentication and access control ───────────

class AuthenticationTests(HospitalTestCase):

    def test_tc01_login_with_valid_credentials_issues_tokens(self):
        """TC-01: valid credentials return a JWT pair and the user's role."""
        response = self.client.post(
            '/api/login/',
            {'username': 't_admin', 'password': 'admin-pass-123'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('access', response.data)
        self.assertIn('refresh', response.data)
        self.assertEqual(response.data['user']['role'], 'admin')
        self.assertTrue(len(response.data['modules']) > 0)

    def test_tc02_login_with_wrong_password_is_rejected(self):
        """TC-02: a wrong password returns 401 and issues no token."""
        response = self.client.post(
            '/api/login/',
            {'username': 't_admin', 'password': 'definitely-wrong'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertNotIn('access', response.data)
        self.assertIn('error', response.data)

    def test_tc03_protected_endpoint_without_token_returns_401(self):
        """TC-03: /api/patients/ requires authentication."""
        response = APIClient().get('/api/patients/')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_tc03_every_listed_endpoint_requires_a_token(self):
        """No endpoint should be reachable anonymously except login/refresh."""
        anonymous = APIClient()
        protected = [
            '/api/dashboard/stats/', '/api/patients/', '/api/beds/',
            '/api/queue/', '/api/alerts/', '/api/medicines/', '/api/doctors/',
            '/api/nurses/', '/api/appointments/', '/api/metrics/',
            '/api/analytics/disease/', '/api/floor-map/', '/api/reports/',
            '/api/activity/', '/api/ai/predictions/', '/api/ai/explanations/',
            '/api/ai/recommendations/', '/api/automation/thresholds/',
        ]
        for url in protected:
            with self.subTest(url=url):
                self.assertEqual(
                    anonymous.get(url).status_code,
                    status.HTTP_401_UNAUTHORIZED,
                    f'{url} was reachable without a token',
                )

    def test_tc04_receptionist_cannot_reach_admin_only_endpoints(self):
        """TC-04: a Receptionist token is refused on Admin-only routes."""
        client = self.client_for('t_reception', 'reception-pass-123')
        for url in ['/api/activity/', '/api/users/']:
            with self.subTest(url=url):
                self.assertEqual(client.get(url).status_code,
                                 status.HTTP_403_FORBIDDEN)

    def test_tc04_receptionist_cannot_restock_pharmacy(self):
        """TC-04: restocking is an Admin action."""
        client = self.client_for('t_reception', 'reception-pass-123')
        response = client.post(
            f'/api/medicines/{self.medicine.id}/restock/',
            {'quantity': 10}, format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_receptionist_cannot_record_vitals(self):
        """Vitals drive triage priority, so only clinical staff may set them."""
        patient = self.make_patient()
        client = self.client_for('t_reception', 'reception-pass-123')
        response = client.post(
            f'/api/patients/{patient.id}/vitals/',
            {'heart_rate': 90}, format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_nurse_can_record_vitals(self):
        """The same endpoint must work for the role that owns the task."""
        patient = self.make_patient()
        client = self.client_for('t_nurse', 'nurse-pass-123')
        response = client.post(
            f'/api/patients/{patient.id}/vitals/',
            {'heart_rate': 92}, format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_refresh_token_returns_new_access_token(self):
        login = self.client.post(
            '/api/login/',
            {'username': 't_admin', 'password': 'admin-pass-123'},
            format='json',
        )
        response = self.client.post(
            '/api/token/refresh/',
            {'refresh': login.data['refresh']}, format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('access', response.data)

    def test_invalid_refresh_token_is_rejected(self):
        response = self.client.post(
            '/api/token/refresh/', {'refresh': 'not-a-real-token'}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


# ─── TC-05: patient admission ────────────────────────────────────

class PatientAdmissionTests(HospitalTestCase):

    def test_tc05_admit_patient_creates_timeline_and_risk(self):
        """TC-05: admitting a patient saves them, logs the timeline and
        calculates a risk level."""
        client = self.client_for('t_reception', 'reception-pass-123')
        response = client.post('/api/patients/', {
            'name': 'New Admission', 'age': 55, 'gender': 'Female',
            'disease': 'Pneumonia', 'disease_severity': 6,
            'heart_rate': 105, 'temperature': 101.2,
            'blood_pressure_systolic': 150, 'blood_pressure_diastolic': 92,
            'oxygen_saturation': 92.0, 'department': 'General',
        }, format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        patient = Patient.objects.get(id=response.data['id'])

        self.assertIn(patient.risk_level, ('medium', 'high', 'critical'))
        self.assertGreater(patient.risk_confidence, 0)

        events = PatientTimeline.objects.filter(patient=patient)
        self.assertTrue(events.filter(event_type='registered').exists())
        self.assertTrue(events.filter(event_type='vitals_recorded').exists())

    def test_admission_rejects_impossible_vitals(self):
        """Out-of-range vitals are a data-entry error and must not be stored."""
        client = self.client_for('t_reception', 'reception-pass-123')
        response = client.post('/api/patients/', {
            'name': 'Bad Data', 'age': 40, 'gender': 'Male',
            'oxygen_saturation': 250, 'heart_rate': 9000,
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('error', response.data)
        self.assertFalse(Patient.objects.filter(name='Bad Data').exists())

    def test_admission_rejects_diastolic_above_systolic(self):
        client = self.client_for('t_reception', 'reception-pass-123')
        response = client.post('/api/patients/', {
            'name': 'Bad BP', 'age': 40, 'gender': 'Male',
            'blood_pressure_systolic': 100, 'blood_pressure_diastolic': 130,
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_client_cannot_dictate_risk_level(self):
        """Risk is derived, never accepted from the caller."""
        client = self.client_for('t_reception', 'reception-pass-123')
        response = client.post('/api/patients/', {
            'name': 'Understated', 'age': 80, 'gender': 'Male',
            'disease': 'Cardiac Arrest', 'disease_severity': 10,
            'oxygen_saturation': 82.0, 'heart_rate': 140,
            'blood_pressure_systolic': 190, 'blood_pressure_diastolic': 110,
            'risk_level': 'low',          # a client trying to downgrade risk
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            Patient.objects.get(id=response.data['id']).risk_level, 'critical'
        )


# ─── TC-06, TC-07: bed allocation and transfer ───────────────────

class BedManagementTests(HospitalTestCase):

    def test_tc06_assign_patient_to_bed_updates_occupancy(self):
        """TC-06: assigning a bed marks it occupied and raises ward occupancy."""
        patient = self.make_patient()
        bed = self.general_beds[0]
        before = self.general.occupancy_percentage

        client = self.client_for('t_nurse', 'nurse-pass-123')
        response = client.post(
            f'/api/beds/{bed.id}/assign/', {'patient_id': patient.id}, format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        bed.refresh_from_db()
        self.assertEqual(bed.status, 'occupied')
        self.assertEqual(bed.patient, patient)
        self.general.refresh_from_db()
        self.assertGreater(self.general.occupancy_percentage, before)

    def test_tc07_transfer_releases_source_and_occupies_target(self):
        """TC-07: transferring frees the old bed and fills the new one."""
        patient = self.make_patient()
        source, target = self.general_beds[0], self.icu_beds[0]
        source.status, source.patient = 'occupied', patient
        source.save()

        client = self.client_for('t_nurse', 'nurse-pass-123')
        response = client.post(
            f'/api/beds/{source.id}/transfer/',
            {'target_bed_id': target.id}, format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        source.refresh_from_db()
        target.refresh_from_db()
        self.assertEqual(source.status, 'available')
        self.assertIsNone(source.patient)
        self.assertEqual(target.status, 'occupied')
        self.assertEqual(target.patient, patient)

    def test_assigning_an_occupied_bed_is_refused(self):
        """Two patients must never share one bed."""
        first, second = self.make_patient(name='First'), self.make_patient(name='Second')
        bed = self.general_beds[0]
        bed.status, bed.patient = 'occupied', first
        bed.save()

        client = self.client_for('t_nurse', 'nurse-pass-123')
        response = client.post(
            f'/api/beds/{bed.id}/assign/', {'patient_id': second.id}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        bed.refresh_from_db()
        self.assertEqual(bed.patient, first)

    def test_reassigning_a_patient_frees_their_previous_bed(self):
        """A patient can only ever hold one bed."""
        patient = self.make_patient()
        first, second = self.general_beds[0], self.general_beds[1]
        first.status, first.patient = 'occupied', patient
        first.save()

        client = self.client_for('t_nurse', 'nurse-pass-123')
        response = client.post(
            f'/api/beds/{second.id}/assign/', {'patient_id': patient.id}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        first.refresh_from_db()
        self.assertEqual(first.status, 'available')
        self.assertEqual(Bed.objects.filter(patient=patient).count(), 1)

    def test_transfer_to_an_unavailable_bed_is_refused(self):
        patient = self.make_patient()
        source, target = self.general_beds[0], self.general_beds[1]
        source.status, source.patient = 'occupied', patient
        source.save()
        target.status = 'maintenance'
        target.save()

        client = self.client_for('t_nurse', 'nurse-pass-123')
        response = client.post(
            f'/api/beds/{source.id}/transfer/',
            {'target_bed_id': target.id}, format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_cannot_mark_an_occupied_bed_for_maintenance(self):
        """Blocking a bed that still holds a patient would hide that patient."""
        patient = self.make_patient()
        bed = self.general_beds[0]
        bed.status, bed.patient = 'occupied', patient
        bed.save()

        client = self.admin_client()
        response = client.post(
            f'/api/beds/{bed.id}/status/', {'status': 'maintenance'}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_discharged_patient_cannot_be_assigned_a_bed(self):
        patient = self.make_patient(is_discharged=True)
        client = self.client_for('t_nurse', 'nurse-pass-123')
        response = client.post(
            f'/api/beds/{self.general_beds[0].id}/assign/',
            {'patient_id': patient.id}, format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


# ─── TC-08: risk scoring and the smart triage queue ──────────────

class RiskAndTriageTests(HospitalTestCase):

    def test_tc08_low_oxygen_produces_high_risk(self):
        """TC-08: SpO2 below 90% must escalate the patient's risk."""
        patient = self.make_patient(
            name='Hypoxic', age=68, disease='Pneumonia',
            disease_severity=7, oxygen_saturation=86.0, heart_rate=128,
        )
        assessment = assess_patient(patient)
        self.assertIn(assessment['risk_level'], ('high', 'critical'))
        self.assertTrue(
            any('SpO2' in f['factor'] or 'oxygen' in f['factor'].lower()
                for f in assessment['factors']),
            'Low oxygen saturation was not reported as a risk factor',
        )

    def test_stable_vitals_produce_low_risk(self):
        """The scoring must not call a healthy patient high risk."""
        patient = self.make_patient(age=28, disease_severity=1)
        self.assertEqual(assess_patient(patient)['risk_level'], 'low')

    def test_risk_levels_are_ordered_by_severity(self):
        """A sicker patient must never score below a healthier one."""
        mild = self.make_patient(name='Mild', age=30, disease_severity=2)
        moderate = self.make_patient(
            name='Moderate', age=55, disease_severity=5,
            oxygen_saturation=93.0, heart_rate=105,
        )
        severe = self.make_patient(
            name='Severe', age=75, disease_severity=9,
            oxygen_saturation=85.0, heart_rate=135,
            blood_pressure_systolic=180, blood_pressure_diastolic=105,
        )
        scores = [assess_patient(p)['risk_score'] for p in (mild, moderate, severe)]
        self.assertEqual(scores, sorted(scores),
                         f'Risk scores are not monotonic: {scores}')

    def test_tc08_critical_patient_ranks_above_earlier_stable_patient(self):
        """The queue is ordered by priority, not arrival — the core claim of
        the smart triage feature."""
        stable = self.make_patient(name='Stable Early', disease_severity=2)
        critical = self.make_patient(
            name='Critical Late', age=70, disease='Cardiac Arrest',
            disease_severity=9, oxygen_saturation=84.0, heart_rate=140,
            blood_pressure_systolic=190, blood_pressure_diastolic=110,
            is_emergency=True,
        )

        # The stable patient has been waiting an hour; the critical one just
        # arrived. First-come-first-served would put the stable patient first.
        stable_entry = PatientQueue.objects.create(
            patient=stable, priority_score=priority_score(stable),
        )
        PatientQueue.objects.filter(pk=stable_entry.pk).update(
            waiting_since=timezone.now() - timedelta(hours=1)
        )
        PatientQueue.objects.create(
            patient=critical, priority_score=priority_score(critical),
        )

        client = self.admin_client()
        results = client.get('/api/queue/').data['results']
        self.assertEqual(results[0]['patient_name'], 'Critical Late')
        self.assertEqual(results[0]['rank'], 1)

    def test_priority_score_stays_within_bounds(self):
        """The score is documented as 0-100 and must never exceed it."""
        extreme = self.make_patient(
            age=95, disease_severity=10, oxygen_saturation=60.0,
            heart_rate=200, blood_pressure_systolic=250,
            blood_pressure_diastolic=170, is_emergency=True,
        )
        score = priority_score(
            extreme, waiting_since=timezone.now() - timedelta(days=2)
        )
        self.assertGreaterEqual(score, 0)
        self.assertLessEqual(score, 100)

    def test_waiting_longer_raises_priority(self):
        """Waiting time must count, so a stable patient is eventually seen."""
        patient = self.make_patient()
        fresh = priority_score(patient, waiting_since=timezone.now())
        waited = priority_score(
            patient, waiting_since=timezone.now() - timedelta(hours=2)
        )
        self.assertGreater(waited, fresh)

    def test_recording_vitals_updates_the_queue_priority(self):
        patient = self.make_patient()
        entry = PatientQueue.objects.create(
            patient=patient, priority_score=priority_score(patient)
        )
        original = entry.priority_score

        client = self.client_for('t_nurse', 'nurse-pass-123')
        client.post(f'/api/patients/{patient.id}/vitals/', {
            'oxygen_saturation': 85.0, 'heart_rate': 138,
        }, format='json')

        entry.refresh_from_db()
        self.assertGreater(entry.priority_score, original)

    def test_risk_board_is_sorted_and_counted(self):
        self.make_patient(name='A', disease_severity=2)
        self.make_patient(
            name='B', age=80, disease_severity=9, oxygen_saturation=84.0,
            heart_rate=140, blood_pressure_systolic=195,
        )
        client = self.admin_client()
        data = client.get('/api/patients/risk-board/').data

        scores = [row['risk_score'] for row in data['results']]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual(sum(data['distribution'].values()), data['count'])


# ─── TC-09 to TC-11: the automated alert engine ──────────────────

class AlertEngineTests(HospitalTestCase):

    def test_tc09_icu_above_threshold_raises_critical_alert(self):
        """TC-09: ICU occupancy above 85% raises an icu_high alert."""
        for bed in self.icu_beds:                    # fill all four ICU beds
            bed.status = 'occupied'
            bed.patient = self.make_patient(name=f'ICU {bed.bed_number}')
            bed.save()

        run_alert_engine()

        alert = Alert.objects.filter(alert_type='icu_high', is_resolved=False).first()
        self.assertIsNotNone(alert, 'No icu_high alert was raised at 100% occupancy')
        self.assertEqual(alert.level, 'critical')

    def test_icu_below_threshold_raises_no_alert(self):
        """The rule must not fire when the hospital is comfortable."""
        self.icu_beds[0].status = 'occupied'
        self.icu_beds[0].patient = self.make_patient()
        self.icu_beds[0].save()

        run_alert_engine()
        self.assertFalse(
            Alert.objects.filter(alert_type='icu_high').exists(),
            'icu_high fired at only 25% occupancy',
        )

    def test_tc10_medicine_below_minimum_raises_alert(self):
        """TC-10: stock at or below min_stock raises medicine_low."""
        self.medicine.stock = 5          # min_stock is 10
        self.medicine.save()

        run_alert_engine()

        self.assertTrue(
            Alert.objects.filter(alert_type='medicine_low', is_resolved=False).exists()
        )
        self.assertEqual(self.medicine.stock_status, 'low_stock')

    def test_out_of_stock_is_critical_not_warning(self):
        self.medicine.stock = 0
        self.medicine.save()

        run_alert_engine()

        alert = Alert.objects.filter(alert_type='medicine_low').first()
        self.assertIsNotNone(alert)
        self.assertEqual(alert.level, 'critical')
        self.assertEqual(self.medicine.stock_status, 'out_of_stock')

    def test_tc11_long_queue_wait_raises_alert(self):
        """TC-11: a wait beyond 45 minutes raises long_wait."""
        PatientQueue.objects.create(
            patient=self.make_patient(), status='waiting',
            estimated_wait_minutes=75, priority_score=40,
        )

        run_alert_engine()

        self.assertTrue(
            Alert.objects.filter(alert_type='long_wait', is_resolved=False).exists()
        )

    def test_critical_patient_raises_alert(self):
        self.make_patient(
            name='Critical', age=78, disease='Stroke', disease_severity=10,
            oxygen_saturation=82.0, heart_rate=142,
            blood_pressure_systolic=200, blood_pressure_diastolic=115,
            risk_level='critical',
        )
        run_alert_engine()
        self.assertTrue(Alert.objects.filter(alert_type='patient_critical').exists())

    def test_unsafe_nurse_ratio_raises_staff_shortage(self):
        """One nurse cannot safely cover twenty patients."""
        for i in range(20):
            self.make_patient(name=f'Patient {i}')
        run_alert_engine()
        self.assertTrue(Alert.objects.filter(alert_type='staff_shortage').exists())

    def test_engine_does_not_duplicate_open_alerts(self):
        """Re-running the engine must not bury staff in repeat alerts."""
        self.medicine.stock = 2
        self.medicine.save()

        run_alert_engine()
        first_count = Alert.objects.filter(alert_type='medicine_low').count()
        run_alert_engine()
        run_alert_engine()

        self.assertEqual(
            Alert.objects.filter(alert_type='medicine_low').count(), first_count,
            'The alert engine raised duplicate alerts for one open condition',
        )

    def test_engine_reports_all_eight_rules(self):
        """The report documents eight rules; all must be evaluated."""
        result = run_alert_engine()
        self.assertEqual(result['rules_evaluated'], 8)
        self.assertEqual(result['errors'], [])

    def test_threshold_summary_covers_every_metric(self):
        summary = threshold_summary()
        self.assertTrue(len(summary) >= 8)
        for row in summary:
            self.assertIn('value', row)
            self.assertIn('threshold', row)
            self.assertIn('breached', row)

    def test_alert_can_be_resolved_and_reopened(self):
        alert = Alert.objects.create(
            level='warning', alert_type='system', message='Test alert'
        )
        client = self.admin_client()

        response = client.put(f'/api/alerts/{alert.id}/', {}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        alert.refresh_from_db()
        self.assertTrue(alert.is_resolved)
        self.assertIsNotNone(alert.resolved_at)

        client.put(f'/api/alerts/{alert.id}/', {'is_resolved': False}, format='json')
        alert.refresh_from_db()
        self.assertFalse(alert.is_resolved)

    def test_bulk_resolve_clears_alerts_by_level(self):
        for i in range(3):
            Alert.objects.create(level='warning', alert_type='system',
                                 message=f'Warning {i}')
        Alert.objects.create(level='critical', alert_type='system',
                             message='Keep me')

        client = self.admin_client()
        response = client.post(
            '/api/alerts/bulk-resolve/', {'level': 'warning'}, format='json'
        )
        self.assertEqual(response.data['resolved'], 3)
        self.assertEqual(
            Alert.objects.filter(level='critical', is_resolved=False).count(), 1
        )


# ─── TC-12: the simulation engine ────────────────────────────────

class SimulationTests(HospitalTestCase):

    def test_tc12_simulation_tick_updates_state_and_stores_metrics(self):
        """TC-12: one tick advances the hospital and records a snapshot."""
        from core.models import HospitalMetric

        self.make_patient(name='Existing')
        before = HospitalMetric.objects.count()

        client = self.admin_client()
        response = client.post('/api/simulate/tick/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(HospitalMetric.objects.count(), before + 1)
        for key in ('new_admissions', 'discharges', 'alerts_raised',
                    'total_active_patients', 'events', 'shift'):
            self.assertIn(key, response.data)

    def test_simulation_keeps_bed_records_consistent(self):
        """After several ticks no bed may be occupied without a patient."""
        client = self.admin_client()
        for _ in range(6):
            client.post('/api/simulate/tick/')

        self.assertEqual(
            Bed.objects.filter(status='occupied', patient__isnull=True).count(), 0,
            'Simulation left beds occupied with no patient',
        )
        self.assertEqual(
            Bed.objects.exclude(status='occupied').filter(
                patient__isnull=False
            ).count(), 0,
            'Simulation left patients in non-occupied beds',
        )

    def test_simulation_never_double_books_a_patient(self):
        client = self.admin_client()
        for _ in range(6):
            client.post('/api/simulate/tick/')

        for patient in Patient.objects.filter(is_discharged=False):
            self.assertLessEqual(
                Bed.objects.filter(patient=patient).count(), 1,
                f'{patient.name} occupies more than one bed',
            )

    def test_discharged_patients_do_not_hold_beds(self):
        client = self.admin_client()
        for _ in range(8):
            client.post('/api/simulate/tick/')

        self.assertEqual(
            Bed.objects.filter(patient__is_discharged=True).count(), 0,
            'A discharged patient is still holding a bed',
        )

    def test_simulation_risk_levels_agree_with_the_scoring_engine(self):
        """The stored risk_level must match what clinical.py computes, or the
        dashboard and the risk board would disagree."""
        client = self.admin_client()
        for _ in range(5):
            client.post('/api/simulate/tick/')

        for patient in Patient.objects.filter(is_discharged=False)[:25]:
            self.assertEqual(
                patient.risk_level, assess_patient(patient)['risk_level'],
                f'{patient.name} stored {patient.risk_level} but scores '
                f"{assess_patient(patient)['risk_level']}",
            )


# ─── TC-13: machine learning forecasts ───────────────────────────

class ForecastTests(HospitalTestCase):

    def test_tc13_forecast_returns_24_hourly_points(self):
        """TC-13: the forecast covers the next 24 hours."""
        client = self.admin_client()
        response = client.get('/api/ai/predictions/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        if not response.data.get('available'):
            self.skipTest('Trained models are not present in this checkout')

        points = response.data['points']
        self.assertEqual(len(points), 24)
        self.assertEqual([p['hours_ahead'] for p in points], list(range(1, 25)))

    def test_forecast_values_are_physically_possible(self):
        """Admissions cannot be negative and ICU cannot exceed its capacity."""
        client = self.admin_client()
        response = client.get('/api/ai/predictions/')
        if not response.data.get('available'):
            self.skipTest('Trained models are not present in this checkout')

        capacity = response.data['icu_capacity']
        for point in response.data['points']:
            self.assertGreaterEqual(point['predicted_inflow'], 0)
            self.assertGreaterEqual(point['predicted_icu_occupancy'], 0)
            self.assertLessEqual(point['predicted_icu_occupancy'], capacity)
            self.assertTrue(0 <= point['predicted_icu_percentage'] <= 100)

    def test_forecast_confidence_decays_with_horizon(self):
        """A 24-hour-out prediction must not claim the same confidence as a
        one-hour-out prediction."""
        client = self.admin_client()
        response = client.get('/api/ai/predictions/')
        if not response.data.get('available'):
            self.skipTest('Trained models are not present in this checkout')

        confidences = [p['confidence'] for p in response.data['points']]
        self.assertGreater(confidences[0], confidences[-1])

    def test_model_status_reports_heldout_accuracy(self):
        client = self.admin_client()
        response = client.get('/api/ai/status/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        if not response.data.get('available'):
            self.skipTest('Trained models are not present in this checkout')

        self.assertEqual(response.data['algorithm'], 'RandomForestRegressor')
        self.assertIn('mae', response.data['icu_occupancy'])
        # The model must beat simply predicting the average.
        self.assertLess(
            response.data['icu_occupancy']['mae'],
            response.data['icu_occupancy']['baseline_mae'],
        )

    def test_explanations_and_recommendations_reflect_live_data(self):
        for bed in self.icu_beds:
            bed.status = 'occupied'
            bed.patient = self.make_patient(name=f'ICU {bed.bed_number}')
            bed.save()

        client = self.admin_client()
        explanations = client.get('/api/ai/explanations/').data['explanations']
        icu = next(e for e in explanations if e['metric'] == 'ICU Occupancy')
        self.assertEqual(icu['value'], '100.0%')
        self.assertEqual(icu['trend'], 'Critical')

        recommendations = client.get('/api/ai/recommendations/').data['recommendations']
        self.assertTrue(any('elective' in r['action'].lower() for r in recommendations),
                        'A full ICU produced no surgery-rescheduling advice')
        for recommendation in recommendations:
            self.assertIn('evidence', recommendation)


# ─── TC-14, TC-15: report generation ─────────────────────────────

class ReportTests(HospitalTestCase):

    REPORT_TYPES = ['hospital_summary', 'patient_census',
                    'bed_utilization', 'ai_forecast']

    def setUp(self):
        super().setUp()
        self.make_patient(name='Report Patient')

    def _body(self, response):
        if getattr(response, 'streaming', False):
            return b''.join(response.streaming_content)
        return response.content

    def test_tc14_every_report_generates_a_valid_pdf(self):
        """TC-14: all four PDF reports download and open correctly."""
        client = self.admin_client()
        for report_type in self.REPORT_TYPES:
            with self.subTest(report=report_type):
                response = client.post(
                    '/api/reports/generate/',
                    {'type': report_type, 'format': 'pdf'}, format='json',
                )
                self.assertEqual(response.status_code, status.HTTP_200_OK)
                self.assertEqual(response['Content-Type'], 'application/pdf')

                body = self._body(response)
                # A real PDF starts with the %PDF- magic bytes.
                self.assertTrue(body.startswith(b'%PDF-'),
                                f'{report_type} did not produce a PDF header')
                self.assertIn(b'%%EOF', body[-2048:],
                              f'{report_type} PDF was truncated')
                self.assertIn('attachment', response['Content-Disposition'])

    def test_tc15_every_report_generates_valid_csv(self):
        """TC-15: CSV export opens in a spreadsheet with the right columns."""
        import csv
        import io

        client = self.admin_client()
        for report_type in self.REPORT_TYPES:
            with self.subTest(report=report_type):
                response = client.post(
                    '/api/reports/generate/',
                    {'type': report_type, 'format': 'csv'}, format='json',
                )
                self.assertEqual(response.status_code, status.HTTP_200_OK)
                self.assertEqual(response['Content-Type'], 'text/csv')

                rows = list(csv.reader(
                    io.StringIO(self._body(response).decode('utf-8'))
                ))
                self.assertGreater(len(rows), 3)

                definition = __import__(
                    'core.reports', fromlist=['resolve_report']
                ).resolve_report(report_type)
                self.assertIn(definition.columns, rows,
                              f'{report_type} CSV is missing its header row')

    def test_unknown_report_type_is_rejected(self):
        client = self.admin_client()
        response = client.post(
            '/api/reports/generate/',
            {'type': 'does_not_exist', 'format': 'pdf'}, format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('available', response.data)

    def test_unknown_format_is_rejected(self):
        client = self.admin_client()
        response = client.post(
            '/api/reports/generate/',
            {'type': 'hospital_summary', 'format': 'docx'}, format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_catalogue_lists_all_four_reports(self):
        client = self.admin_client()
        reports = client.get('/api/reports/').data['reports']
        self.assertEqual(
            sorted(r['key'] for r in reports), sorted(self.REPORT_TYPES)
        )


# ─── TC-16: the chat assistant ───────────────────────────────────

class ChatAssistantTests(HospitalTestCase):

    def test_tc16_icu_bed_question_returns_the_correct_count(self):
        """TC-16: "How many ICU beds are available?" answers instantly and
        correctly."""
        self.icu_beds[0].status = 'occupied'
        self.icu_beds[0].patient = self.make_patient()
        self.icu_beds[0].save()

        client = self.admin_client()
        response = client.post(
            '/api/chat/',
            {'message': 'How many ICU beds are available?'}, format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['intent'], 'icu_beds')
        # Three of four ICU beds remain free.
        self.assertIn('3', response.data['response'])
        self.assertIn('4', response.data['response'])

    def test_assistant_routes_each_question_to_the_right_intent(self):
        client = self.admin_client()
        expectations = {
            'how many icu beds are free': 'icu_beds',
            'who are the critical patients': 'critical_patients',
            'which ward is busiest': 'busiest_ward',
            'which medicines are low on stock': 'medicine',
            'how many doctors are on duty': 'staff',
            'how long is the queue': 'queue',
            "what are today's admissions": 'admissions',
            'are there any active alerts': 'alerts',
            'what are the top diseases': 'diseases',
            'how many beds are available': 'beds',
        }
        for question, expected in expectations.items():
            with self.subTest(question=question):
                response = client.post(
                    '/api/chat/', {'message': question}, format='json'
                )
                self.assertEqual(response.data['intent'], expected)

    def test_assistant_admits_when_it_does_not_understand(self):
        """Guessing an answer would be worse than saying so."""
        client = self.admin_client()
        response = client.post(
            '/api/chat/',
            {'message': 'what is the capital of France'}, format='json',
        )
        self.assertEqual(response.data['intent'], 'unknown')
        self.assertEqual(response.data['confidence'], 0)

    def test_empty_message_is_rejected(self):
        client = self.admin_client()
        response = client.post('/api/chat/', {'message': '   '}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_assistant_offers_starter_questions(self):
        client = self.admin_client()
        response = client.get('/api/chat/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(len(response.data['suggestions']) > 0)


# ─── Pharmacy ────────────────────────────────────────────────────

class PharmacyTests(HospitalTestCase):

    def test_dispensing_reduces_stock_and_writes_a_log(self):
        patient = self.make_patient()
        client = self.client_for('t_nurse', 'nurse-pass-123')

        response = client.post(
            f'/api/medicines/{self.medicine.id}/dispense/',
            {'patient_id': patient.id, 'quantity': 5}, format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.medicine.refresh_from_db()
        self.assertEqual(self.medicine.stock, 45)
        self.assertTrue(
            MedicineLog.objects.filter(
                medicine=self.medicine, patient=patient, quantity=5
            ).exists()
        )
        self.assertTrue(
            PatientTimeline.objects.filter(
                patient=patient, event_type='medicine_given'
            ).exists()
        )

    def test_cannot_dispense_more_than_is_in_stock(self):
        """Overselling stock would make the inventory lie."""
        patient = self.make_patient()
        client = self.client_for('t_nurse', 'nurse-pass-123')

        response = client.post(
            f'/api/medicines/{self.medicine.id}/dispense/',
            {'patient_id': patient.id, 'quantity': 999}, format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.medicine.refresh_from_db()
        self.assertEqual(self.medicine.stock, 50)

    def test_dispensing_below_minimum_raises_an_alert(self):
        patient = self.make_patient()
        client = self.client_for('t_nurse', 'nurse-pass-123')

        client.post(
            f'/api/medicines/{self.medicine.id}/dispense/',
            {'patient_id': patient.id, 'quantity': 45}, format='json',
        )

        self.medicine.refresh_from_db()
        self.assertEqual(self.medicine.stock, 5)
        self.assertTrue(Alert.objects.filter(alert_type='medicine_low').exists())

    def test_restock_clears_the_low_stock_alert(self):
        self.medicine.stock = 2
        self.medicine.save()
        run_alert_engine()
        self.assertTrue(
            Alert.objects.filter(alert_type='medicine_low', is_resolved=False).exists()
        )

        client = self.admin_client()
        client.post(
            f'/api/medicines/{self.medicine.id}/restock/',
            {'quantity': 200}, format='json',
        )

        self.medicine.refresh_from_db()
        self.assertEqual(self.medicine.stock, 202)
        self.assertFalse(
            Alert.objects.filter(alert_type='medicine_low', is_resolved=False).exists(),
            'Restocking did not close the low-stock alert',
        )

    def test_stock_status_reflects_expiry(self):
        self.medicine.expiry_date = date.today() + timedelta(days=10)
        self.medicine.save()
        self.assertEqual(self.medicine.stock_status, 'near_expiry')

        self.medicine.expiry_date = date.today() - timedelta(days=1)
        self.medicine.save()
        self.assertEqual(self.medicine.stock_status, 'expired')

    def test_dispense_rejects_a_zero_or_negative_quantity(self):
        patient = self.make_patient()
        client = self.client_for('t_nurse', 'nurse-pass-123')
        for quantity in (0, -5):
            with self.subTest(quantity=quantity):
                response = client.post(
                    f'/api/medicines/{self.medicine.id}/dispense/',
                    {'patient_id': patient.id, 'quantity': quantity}, format='json',
                )
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


# ─── Discharge ───────────────────────────────────────────────────

class DischargeTests(HospitalTestCase):

    def test_discharge_frees_the_bed_and_closes_the_queue_entry(self):
        patient = self.make_patient()
        bed = self.general_beds[0]
        bed.status, bed.patient = 'occupied', patient
        bed.save()
        PatientQueue.objects.create(patient=patient, priority_score=30)

        client = self.client_for('t_doctor', 'doctor-pass-123')
        response = client.post(
            f'/api/patients/{patient.id}/discharge/', {}, format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        patient.refresh_from_db()
        bed.refresh_from_db()
        self.assertTrue(patient.is_discharged)
        self.assertIsNotNone(patient.discharged_at)
        self.assertEqual(bed.status, 'available')
        self.assertIsNone(bed.patient)
        self.assertEqual(
            PatientQueue.objects.get(patient=patient).status, 'completed'
        )

    def test_discharging_a_critical_patient_requires_an_override(self):
        """A misclick must not discharge an unstable patient."""
        patient = self.make_patient(
            name='Critical', age=79, disease='Stroke', disease_severity=10,
            oxygen_saturation=83.0, heart_rate=145,
            blood_pressure_systolic=198, blood_pressure_diastolic=118,
        )
        client = self.client_for('t_doctor', 'doctor-pass-123')

        refused = client.post(
            f'/api/patients/{patient.id}/discharge/', {}, format='json'
        )
        self.assertEqual(refused.status_code, status.HTTP_409_CONFLICT)
        patient.refresh_from_db()
        self.assertFalse(patient.is_discharged)

        allowed = client.post(
            f'/api/patients/{patient.id}/discharge/',
            {'override': True}, format='json',
        )
        self.assertEqual(allowed.status_code, status.HTTP_200_OK)
        patient.refresh_from_db()
        self.assertTrue(patient.is_discharged)

    def test_cannot_discharge_the_same_patient_twice(self):
        patient = self.make_patient(is_discharged=True)
        client = self.client_for('t_doctor', 'doctor-pass-123')
        response = client.post(
            f'/api/patients/{patient.id}/discharge/', {}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


# ─── Appointments ────────────────────────────────────────────────

class AppointmentTests(HospitalTestCase):

    def test_receptionist_can_schedule_an_appointment(self):
        patient = self.make_patient()
        client = self.client_for('t_reception', 'reception-pass-123')

        response = client.post('/api/appointments/', {
            'patient': patient.id, 'doctor': self.doctor.id,
            'appointment_date': (timezone.now() + timedelta(days=1)).isoformat(),
            'reason': 'Routine follow-up',
        }, format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_double_booking_a_doctor_is_refused(self):
        """Two patients cannot hold the same slot with the same doctor."""
        slot = timezone.now() + timedelta(days=1)
        Appointment.objects.create(
            patient=self.make_patient(name='First'), doctor=self.doctor,
            appointment_date=slot, status='upcoming',
        )

        client = self.client_for('t_reception', 'reception-pass-123')
        response = client.post('/api/appointments/', {
            'patient': self.make_patient(name='Second').id,
            'doctor': self.doctor.id,
            'appointment_date': (slot + timedelta(minutes=5)).isoformat(),
        }, format='json')

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_nurse_cannot_schedule_appointments(self):
        patient = self.make_patient()
        client = self.client_for('t_nurse', 'nurse-pass-123')
        response = client.post('/api/appointments/', {
            'patient': patient.id, 'doctor': self.doctor.id,
            'appointment_date': (timezone.now() + timedelta(days=1)).isoformat(),
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_completing_an_appointment_writes_to_the_timeline(self):
        patient = self.make_patient()
        appointment = Appointment.objects.create(
            patient=patient, doctor=self.doctor,
            appointment_date=timezone.now(), status='upcoming',
        )

        client = self.client_for('t_doctor', 'doctor-pass-123')
        response = client.put(
            f'/api/appointments/{appointment.id}/',
            {'status': 'completed', 'notes': 'Patient recovering well.'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(PatientTimeline.objects.filter(patient=patient).exists())


# ─── Dashboard and analytics integrity ───────────────────────────

class DashboardTests(HospitalTestCase):

    def test_dashboard_counts_match_the_database(self):
        for i in range(4):
            self.make_patient(name=f'Active {i}')
        self.make_patient(name='Gone', is_discharged=True)

        self.general_beds[0].status = 'occupied'
        self.general_beds[0].patient = Patient.objects.first()
        self.general_beds[0].save()

        client = self.admin_client()
        stats = client.get('/api/dashboard/stats/').data

        self.assertEqual(stats['total_patients'],
                         Patient.objects.filter(is_discharged=False).count())
        self.assertEqual(stats['total_beds'], Bed.objects.count())
        self.assertEqual(stats['available_beds'],
                         Bed.objects.filter(status='available').count())
        self.assertEqual(stats['occupied_beds'],
                         Bed.objects.filter(status='occupied').count())

    def test_bed_counts_sum_to_the_total(self):
        client = self.admin_client()
        stats = client.get('/api/dashboard/stats/').data
        counted = (stats['available_beds'] + stats['occupied_beds']
                   + stats['maintenance_beds'])
        self.assertLessEqual(counted, stats['total_beds'])

    def test_risk_counts_sum_to_the_patient_total(self):
        for severity in (1, 4, 7, 10):
            self.make_patient(name=f'Severity {severity}',
                              disease_severity=severity)
        client = self.admin_client()
        for patient in Patient.objects.all():
            client.put(f'/api/patients/{patient.id}/', {}, format='json')

        stats = client.get('/api/dashboard/stats/').data
        by_risk = (stats['critical_patients'] + stats['high_risk_patients']
                   + stats['medium_risk_patients'] + stats['low_risk_patients'])
        self.assertEqual(by_risk, stats['total_patients'])

    def test_analytics_reports_real_zeros_not_invented_numbers(self):
        """An empty month must report zero admissions. The earlier version
        substituted a random number, which made the charts untrustworthy."""
        client = self.admin_client()
        data = client.get('/api/analytics/disease/').data

        total_admissions = sum(m['admissions'] for m in data['monthly_admissions'])
        self.assertLessEqual(total_admissions, Patient.objects.count())

        # With no patients at all, every bucket must be zero.
        Patient.objects.all().delete()
        data = client.get('/api/analytics/disease/').data
        self.assertTrue(
            all(m['admissions'] == 0 for m in data['monthly_admissions']),
            'Analytics invented admissions for an empty database',
        )
        self.assertEqual(data['totals']['total_patients'], 0)

    def test_analytics_age_buckets_do_not_overlap(self):
        """Overlapping bands would double-count patients."""
        for age in (5, 18, 19, 35, 36, 50, 51, 65, 66, 90):
            self.make_patient(name=f'Age {age}', age=age)

        client = self.admin_client()
        data = client.get('/api/analytics/disease/').data
        bucketed = sum(b['count'] for b in data['age_distribution'])
        self.assertEqual(bucketed, Patient.objects.count())

    def test_trend_endpoint_returns_the_requested_window(self):
        client = self.admin_client()
        data = client.get('/api/dashboard/trend/?hours=12').data
        self.assertEqual(data['hours'], 12)
        self.assertEqual(len(data['points']), 12)

    def test_floor_map_includes_every_bed_exactly_once(self):
        client = self.admin_client()
        data = client.get('/api/floor-map/').data

        seen = [
            bed['id']
            for floor in data['floors']
            for ward in floor['wards']
            for bed in ward['beds']
        ]
        self.assertEqual(len(seen), Bed.objects.count())
        self.assertEqual(len(set(seen)), len(seen), 'A bed appeared twice')


# ─── Audit trail ─────────────────────────────────────────────────

class ActivityLogTests(HospitalTestCase):

    def test_state_changing_actions_are_recorded(self):
        from core.models import ActivityLog

        patient = self.make_patient()
        client = self.client_for('t_nurse', 'nurse-pass-123')
        client.post(
            f'/api/beds/{self.general_beds[0].id}/assign/',
            {'patient_id': patient.id}, format='json',
        )

        self.assertTrue(
            ActivityLog.objects.filter(
                action='Assigned bed', user=self.nurse_user
            ).exists()
        )

    def test_login_is_recorded(self):
        from core.models import ActivityLog

        self.admin_client()
        self.assertTrue(
            ActivityLog.objects.filter(
                action='Signed in', user=self.admin
            ).exists()
        )
