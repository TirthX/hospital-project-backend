from django.core.management.base import BaseCommand
from django.utils import timezone
from core.clinical import apply_risk_assessment, estimate_wait_minutes, priority_score
from core.models import (
    HospitalUser, Ward, Bed, Patient, Doctor, Nurse,
    Appointment, Medicine, MedicineLog, PatientTimeline,
    PatientQueue, Alert, HospitalMetric, Prediction, ActivityLog
)
import random
from datetime import timedelta, date


class Command(BaseCommand):
    help = 'Seeds the database with comprehensive hospital data for demo'

    def handle(self, *args, **options):
        self.stdout.write('🏥 Seeding Hospital Command Center database...\n')

        self._create_users()
        self._create_wards()
        self._create_beds()
        self._create_doctors()
        self._create_nurses()
        self._create_patients()
        self._create_appointments()
        self._create_medicines()
        self._create_medicine_logs()
        self._create_alerts()
        self._create_metrics()
        self._create_predictions()
        self._create_activity_logs()

        self.stdout.write(self.style.SUCCESS('\n✅ Database seeded successfully!'))
        self.stdout.write(self.style.SUCCESS('   Login credentials:'))
        self.stdout.write(self.style.SUCCESS('   Admin    → admin / admin123'))
        self.stdout.write(self.style.SUCCESS('   Doctor   → dr.sharma / doctor123'))
        self.stdout.write(self.style.SUCCESS('   Nurse    → nurse.priya / nurse123'))
        self.stdout.write(self.style.SUCCESS('   Reception→ reception / reception123'))

    def _create_users(self):
        if HospitalUser.objects.filter(username='admin').exists():
            self.stdout.write('  Users already exist, skipping...')
            return

        HospitalUser.objects.create_superuser(
            'admin', 'admin@hospital.com', 'admin123',
            role='admin', department='Administration', first_name='Tirth', last_name='Admin'
        )
        HospitalUser.objects.create_user(
            'dr.sharma', 'sharma@hospital.com', 'doctor123',
            role='doctor', department='Cardiology', first_name='Rajesh', last_name='Sharma'
        )
        HospitalUser.objects.create_user(
            'nurse.priya', 'priya@hospital.com', 'nurse123',
            role='nurse', department='ICU', first_name='Priya', last_name='Patel'
        )
        HospitalUser.objects.create_user(
            'reception', 'reception@hospital.com', 'reception123',
            role='receptionist', department='Reception', first_name='Anita', last_name='Desai'
        )
        self.stdout.write('  ✓ Created 4 users')

    def _create_wards(self):
        if Ward.objects.exists():
            self.stdout.write('  Wards already exist, skipping...')
            return

        wards = [
            {'name': 'Emergency Ward', 'ward_type': 'emergency', 'floor': 1, 'capacity': 15,
             'description': 'Emergency triage and critical care admissions'},
            {'name': 'General Ward A', 'ward_type': 'general', 'floor': 2, 'capacity': 30,
             'description': 'General medicine and recovery'},
            {'name': 'General Ward B', 'ward_type': 'general', 'floor': 2, 'capacity': 25,
             'description': 'Post-operative care and observation'},
            {'name': 'ICU', 'ward_type': 'icu', 'floor': 3, 'capacity': 12,
             'description': 'Intensive care unit for critical patients'},
            {'name': 'Operation Theatre', 'ward_type': 'ot', 'floor': 3, 'capacity': 6,
             'description': 'Surgical operation theatres'},
            {'name': 'Laboratory', 'ward_type': 'laboratory', 'floor': 1, 'capacity': 0,
             'description': 'Pathology and diagnostic lab'},
        ]
        for w in wards:
            Ward.objects.create(**w)
        self.stdout.write(f'  ✓ Created {len(wards)} wards')

    def _create_beds(self):
        if Bed.objects.exists():
            self.stdout.write('  Beds already exist, skipping...')
            return

        wards = Ward.objects.exclude(ward_type__in=['laboratory', 'reception'])
        total = 0
        for ward in wards:
            prefix_map = {
                'emergency': 'ER', 'general': 'GEN', 'icu': 'ICU', 'ot': 'OT'
            }
            prefix = prefix_map.get(ward.ward_type, 'BED')
            ward_index = list(Ward.objects.filter(ward_type=ward.ward_type)).index(ward)
            suffix = chr(65 + ward_index) if ward_index > 0 else ''

            for i in range(1, ward.capacity + 1):
                # Beds are created free. Occupancy is a consequence of a
                # patient being placed in a bed, not a status rolled at
                # random — otherwise a bed can end up marked occupied with
                # nobody in it, which is exactly the inconsistency the
                # automation engine's integrity rule reports.
                Bed.objects.create(
                    bed_number=f"{prefix}{suffix}-{i:02d}",
                    ward=ward,
                    status='maintenance' if random.random() < 0.04 else 'available',
                )
                total += 1
        self.stdout.write(f'  ✓ Created {total} beds')

    def _create_doctors(self):
        if Doctor.objects.exists():
            self.stdout.write('  Doctors already exist, skipping...')
            return

        doctors_data = [
            ('Rajesh Sharma', 'Cardiology', 'General', 'morning', 12),
            ('Anjali Mehta', 'Neurology', 'ICU', 'morning', 8),
            ('Vikram Singh', 'Orthopedics', 'General', 'afternoon', 15),
            ('Sneha Reddy', 'Pediatrics', 'General', 'morning', 6),
            ('Arjun Nair', 'General Medicine', 'Emergency', 'night', 10),
            ('Kavita Joshi', 'Surgery', 'OT', 'morning', 20),
            ('Rahul Verma', 'Emergency Medicine', 'Emergency', 'afternoon', 7),
            ('Deepika Iyer', 'Pulmonology', 'ICU', 'night', 9),
            ('Suresh Gupta', 'Gastroenterology', 'General', 'morning', 14),
            ('Priyanka Das', 'Dermatology', 'General', 'afternoon', 5),
            ('Amit Patel', 'General Medicine', 'General', 'morning', 11),
            ('Neha Kulkarni', 'Cardiology', 'ICU', 'night', 8),
            ('Rohit Agarwal', 'Surgery', 'OT', 'afternoon', 16),
            ('Meera Bhat', 'Emergency Medicine', 'Emergency', 'morning', 4),
            ('Kiran Deshmukh', 'Neurology', 'General', 'night', 7),
        ]

        statuses = ['available', 'available', 'available', 'busy', 'busy', 'off_duty']
        for name, spec, dept, shift, exp in doctors_data:
            Doctor.objects.create(
                name=name, specialization=spec, department=dept,
                shift=shift, experience_years=exp,
                status=random.choice(statuses),
                today_patients=random.randint(2, 12),
                phone=f'+91 {random.randint(70000, 99999)} {random.randint(10000, 99999)}'
            )
        self.stdout.write(f'  ✓ Created {len(doctors_data)} doctors')

    def _create_nurses(self):
        if Nurse.objects.exists():
            self.stdout.write('  Nurses already exist, skipping...')
            return

        nurses_data = [
            ('Priya Patel', 'ICU', 'morning'),
            ('Sunita Rao', 'Emergency', 'morning'),
            ('Geeta Sharma', 'General', 'morning'),
            ('Lakshmi Nair', 'ICU', 'afternoon'),
            ('Asha Kumari', 'General', 'afternoon'),
            ('Rekha Singh', 'Emergency', 'afternoon'),
            ('Anita Desai', 'General', 'night'),
            ('Bharti Jain', 'ICU', 'night'),
            ('Pooja Mishra', 'Emergency', 'night'),
            ('Swati Kulkarni', 'OT', 'morning'),
            ('Nandini Hegde', 'General', 'morning'),
            ('Ritu Agarwal', 'ICU', 'morning'),
            ('Kavya Menon', 'Emergency', 'afternoon'),
            ('Divya Saxena', 'General', 'afternoon'),
            ('Shalini Tiwari', 'OT', 'afternoon'),
            ('Jyoti Choudhary', 'General', 'night'),
            ('Pallavi Reddy', 'ICU', 'night'),
            ('Smita Deshpande', 'Emergency', 'morning'),
            ('Vandana Bhatt', 'General', 'morning'),
            ('Archana Pillai', 'OT', 'night'),
        ]

        statuses = ['available', 'available', 'available', 'busy', 'busy', 'off_duty']
        for name, dept, shift in nurses_data:
            Nurse.objects.create(
                name=name, department=dept, shift=shift,
                status=random.choice(statuses),
                today_patients=random.randint(3, 15),
                phone=f'+91 {random.randint(70000, 99999)} {random.randint(10000, 99999)}'
            )
        self.stdout.write(f'  ✓ Created {len(nurses_data)} nurses')

    def _vitals_for_severity(self, severity):
        """Return (hr, temp, systolic, diastolic, spo2) matching a severity.

        The bands do not overlap between severity tiers, so a severity-9
        patient always presents worse observations than a severity-2 one and
        the derived risk level is never contradicted by the vitals shown
        beside it on screen.
        """
        if severity >= 8:
            return (
                random.choice([random.randint(126, 148), random.randint(40, 54)]),
                round(random.uniform(100.6, 104.0), 1),
                random.choice([random.randint(166, 196), random.randint(78, 89)]),
                random.randint(50, 112),
                round(random.uniform(82.0, 90.5), 1),
            )
        if severity >= 6:
            return (
                random.randint(101, 124),
                round(random.uniform(99.6, 102.3), 1),
                random.randint(142, 164),
                random.randint(86, 99),
                round(random.uniform(90.6, 93.9), 1),
            )
        if severity >= 4:
            return (
                random.randint(88, 104),
                round(random.uniform(98.5, 100.1), 1),
                random.randint(128, 146),
                random.randint(80, 92),
                round(random.uniform(94.0, 96.9), 1),
            )
        return (
            random.randint(64, 88),
            round(random.uniform(97.3, 99.1), 1),
            random.randint(106, 128),
            random.randint(66, 84),
            round(random.uniform(96.8, 99.6), 1),
        )

    def _create_medicine_logs(self):
        """Seed the administration ledger so the pharmacy page has history."""
        if MedicineLog.objects.exists():
            self.stdout.write('  Medicine logs already exist, skipping...')
            return

        medicines = list(Medicine.objects.all())
        patients = list(Patient.objects.filter(is_discharged=False)[:30])
        nurses = list(Nurse.objects.all())
        if not (medicines and patients):
            return

        created = 0
        for _ in range(60):
            medicine = random.choice(medicines)
            patient = random.choice(patients)
            nurse = random.choice(nurses) if nurses else None
            MedicineLog.objects.create(
                medicine=medicine, patient=patient,
                quantity=random.randint(1, 4),
                administered_by=nurse.name if nurse else 'Duty Nurse',
                notes=random.choice([
                    'Administered during morning round.',
                    'Administered during evening round.',
                    'Given as prescribed by attending doctor.',
                    'Single dose administered on request.',
                ]),
            )
            created += 1
        self.stdout.write(f'  ✓ Created {created} medicine administration logs')

    def _create_patients(self):
        if Patient.objects.count() > 10:
            self.stdout.write('  Patients already exist, skipping...')
            return

        Patient.objects.all().delete()

        first_names_m = [
            'Aarav', 'Vivaan', 'Aditya', 'Vihaan', 'Arjun', 'Reyansh',
            'Mohammad', 'Sai', 'Arnav', 'Dhruv', 'Kabir', 'Ritvik',
            'Sahil', 'Shaurya', 'Ayaan', 'Kartik', 'Ishaan', 'Laksh',
            'Yash', 'Pranav', 'Rohan', 'Gaurav', 'Manish', 'Rajesh'
        ]
        first_names_f = [
            'Saanvi', 'Aanya', 'Aadhya', 'Aaradhya', 'Ananya', 'Pari',
            'Anika', 'Navya', 'Diya', 'Myra', 'Sara', 'Kiara',
            'Isha', 'Riya', 'Meera', 'Kavya', 'Sanya', 'Nisha',
            'Priya', 'Sneha', 'Pooja', 'Divya', 'Neha', 'Komal'
        ]
        last_names = [
            'Sharma', 'Patel', 'Singh', 'Kumar', 'Verma', 'Gupta',
            'Reddy', 'Joshi', 'Nair', 'Rao', 'Desai', 'Mehta',
            'Iyer', 'Shah', 'Mishra', 'Das', 'Bhat', 'Pillai'
        ]

        diseases = [
            'Cardiac Arrest', 'Pneumonia', 'Diabetes', 'Fracture', 'Dengue',
            'Malaria', 'COVID-19', 'Typhoid', 'Asthma', 'Hypertension',
            'Stroke', 'Appendicitis', 'Kidney Stone', 'Liver Cirrhosis', 'Tuberculosis'
        ]

        departments = ['ICU', 'General', 'Emergency', 'General', 'General']
        doctors = list(Doctor.objects.all())
        now = timezone.now()

        for i in range(55):
            gender = random.choice(['Male', 'Female'])
            if gender == 'Male':
                name = f"{random.choice(first_names_m)} {random.choice(last_names)}"
            else:
                name = f"{random.choice(first_names_f)} {random.choice(last_names)}"

            age = random.randint(5, 85)
            disease = random.choice(diseases)
            is_emergency = random.random() < 0.15
            # Weighted towards routine cases. A uniform 1-10 draw makes
            # roughly half the hospital critical, which is not what a real
            # case mix looks like and devalues the Critical signal on the
            # risk board.
            severity = random.choices(
                population=[1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
                weights=[10, 14, 16, 15, 13, 10, 8, 6, 5, 3],
                k=1,
            )[0]
            dept = 'Emergency' if is_emergency else random.choice(departments)

            # Vitals are generated from severity in tight, non-overlapping
            # bands. The earlier version drew from wide overlapping ranges
            # and then set risk_level from severity alone, which produced
            # patients stored as "critical" while their vitals were normal —
            # so the dashboard and the risk board disagreed. Risk is now
            # derived from these vitals by the clinical engine instead.
            hr, temp, bp_s, bp_d, o2 = self._vitals_for_severity(severity)

            is_discharged = random.random() < 0.2
            # Spread arrivals over the past two weeks, weighted towards the
            # last 24 hours so the dashboard's hourly trend chart has a
            # realistic curve. Without this every seeded patient shares one
            # admitted_at timestamp and the trend collapses to a single spike.
            if random.random() < 0.45:
                admitted_at = now - timedelta(
                    hours=random.randint(0, 23), minutes=random.randint(0, 59),
                )
            else:
                admitted_at = now - timedelta(
                    days=random.randint(1, 14),
                    hours=random.randint(0, 23), minutes=random.randint(0, 59),
                )
            admitted_days_ago = max(0, (now - admitted_at).days)

            conditions = ['Stable', 'Under Observation', 'Critical', 'Recovering', 'Post-Op']
            if severity >= 7:
                condition = random.choice(['Critical', 'Under Observation'])
            else:
                condition = random.choice(conditions)

            patient = Patient.objects.create(
                name=name, age=age, gender=gender,
                phone=f'+91 {random.randint(70000, 99999)} {random.randint(10000, 99999)}',
                disease=disease, disease_severity=severity,
                condition=condition,
                heart_rate=hr, temperature=temp,
                blood_pressure_systolic=bp_s, blood_pressure_diastolic=bp_d,
                oxygen_saturation=o2,
                is_emergency=is_emergency, is_discharged=is_discharged,
                department=dept,
                attending_doctor=random.choice(doctors) if doctors else None,
                expected_discharge=admitted_at + timedelta(days=random.randint(1, 10)),
                discharged_at=(
                    # Somewhere between admission and now, never before it.
                    admitted_at + (now - admitted_at) * random.uniform(0.5, 0.98)
                    if is_discharged else None
                ),
            )

            # admitted_at is auto_now_add, so it can only be backdated after
            # the row exists.
            Patient.objects.filter(pk=patient.pk).update(admitted_at=admitted_at)
            patient.admitted_at = admitted_at

            # Score the patient with the same engine the API uses, so the
            # stored risk level always matches what the risk board computes.
            assessment = apply_risk_assessment(patient)

            # Create timeline events for each patient
            self._create_patient_timeline(patient, admitted_days_ago)

            # Create queue entry for non-discharged patients
            if not is_discharged and random.random() < 0.4:
                PatientQueue.objects.create(
                    patient=patient,
                    priority_score=priority_score(patient, assessment=assessment),
                    doctor_assigned=random.choice(doctors) if doctors and random.random() > 0.3 else None,
                    estimated_wait_minutes=random.randint(5, 70),
                    estimated_consultation_minutes=random.randint(10, 45),
                    status=random.choice(['waiting', 'waiting', 'in_consultation']),
                    department=dept,
                )

        placed = self._place_patients_in_beds()
        self.stdout.write(
            f'  ✓ Created 55 patients with timelines and queue entries '
            f'({placed} placed in beds)'
        )

    def _place_patients_in_beds(self):
        """Put every active patient into a clinically appropriate free bed.

        Mirrors the simulation engine's placement preference so seeded and
        simulated patients end up in the same kinds of ward: critical cases
        to ICU, emergency arrivals to the emergency ward, everyone else to a
        general bed.
        """
        preferences = {
            'critical': ['icu', 'emergency', 'general'],
            'high': ['icu', 'general', 'emergency'],
            'medium': ['general', 'emergency'],
            'low': ['general', 'emergency'],
        }
        placed = 0

        # Sickest first, so the limited ICU capacity goes to those who need it.
        patients = sorted(
            Patient.objects.filter(is_discharged=False),
            key=lambda item: -item.disease_severity,
        )

        for patient in patients:
            order = (['emergency', 'icu', 'general'] if patient.is_emergency
                     else preferences.get(patient.risk_level, ['general']))

            for ward_type in order:
                bed = self._free_bed_in(ward_type)
                if bed:
                    bed.status = 'occupied'
                    bed.patient = patient
                    bed.save(update_fields=['status', 'patient'])
                    placed += 1
                    break

        return placed

    def _free_bed_in(self, ward_type):
        """Pick a free bed in the least-loaded ward of this type.

        Taking simply the first free bed fills one ward to capacity and
        leaves its neighbour empty, which is both unrealistic and makes the
        floor map look broken. Choosing the emptiest ward spreads patients
        the way an admissions desk actually would.

        ICU keeps two beds in reserve: a unit pinned at 100% would hold the
        icu_high alert open permanently and leave no capacity for the
        simulation to escalate a deteriorating patient into.
        """
        reserve = 2 if ward_type == 'icu' else 0

        candidates = []
        for ward in Ward.objects.filter(ward_type=ward_type):
            free = ward.beds.filter(status='available', patient__isnull=True).count()
            if free > reserve:
                candidates.append((ward.occupancy_percentage, ward))

        if not candidates:
            return None

        # Emptiest ward first.
        candidates.sort(key=lambda pair: pair[0])
        return candidates[0][1].beds.filter(
            status='available', patient__isnull=True,
        ).first()

    def _create_patient_timeline(self, patient, days_ago):
        now = timezone.now()
        base = now - timedelta(days=days_ago)

        events = [
            ('registered', 'Patient registered and initial assessment completed', base),
            ('vitals_recorded', f'Vitals: HR {patient.heart_rate}, Temp {patient.temperature}°F, '
                                f'BP {patient.blood_pressure_systolic}/{patient.blood_pressure_diastolic}, '
                                f'SpO2 {patient.oxygen_saturation}%',
             base + timedelta(minutes=15)),
        ]

        if patient.attending_doctor:
            events.append(
                ('doctor_assigned', f'Dr. {patient.attending_doctor.name} assigned as attending physician',
                 base + timedelta(minutes=30))
            )

        if days_ago > 0:
            events.append(('lab_test', 'Blood test, CBC, and urinalysis ordered', base + timedelta(hours=1)))
            events.append(('lab_result', 'Lab results received — reviewed by attending doctor',
                           base + timedelta(hours=4)))

        if days_ago > 1:
            events.append(('medicine_given', f'Medication administered as per treatment plan',
                           base + timedelta(hours=6)))

        if patient.department == 'ICU':
            events.append(('shifted_icu', 'Patient shifted to ICU for intensive monitoring',
                           base + timedelta(hours=2)))

        if patient.risk_level in ['high', 'critical']:
            events.append(('condition_update', f'Condition update: {patient.condition}. '
                                                f'Risk level: {patient.get_risk_level_display()}',
                           base + timedelta(hours=8)))

        if patient.is_discharged:
            events.append(('recovered', 'Patient showing significant improvement',
                           now - timedelta(days=1)))
            events.append(('discharged', 'Patient discharged with follow-up instructions',
                           patient.discharged_at or now))

        for event_type, desc, ts in events:
            PatientTimeline.objects.create(
                patient=patient, event_type=event_type,
                description=desc,
            )

    def _create_appointments(self):
        if Appointment.objects.exists():
            self.stdout.write('  Appointments already exist, skipping...')
            return

        patients = list(Patient.objects.filter(is_discharged=False)[:30])
        doctors = list(Doctor.objects.all())
        now = timezone.now()
        statuses = ['upcoming', 'upcoming', 'upcoming', 'completed', 'completed', 'cancelled', 'missed']
        reasons = [
            'Follow-up consultation', 'Routine check-up', 'Lab result review',
            'Post-surgery follow-up', 'Medication review', 'Specialist consultation',
            'Emergency consultation', 'Discharge assessment', 'Physiotherapy session',
            'Dietary consultation'
        ]

        for i in range(50):
            patient = random.choice(patients)
            doctor = random.choice(doctors)
            status = random.choice(statuses)

            if status == 'completed':
                appt_date = now - timedelta(days=random.randint(1, 14), hours=random.randint(0, 8))
            elif status in ['cancelled', 'missed']:
                appt_date = now - timedelta(days=random.randint(0, 7), hours=random.randint(0, 8))
            else:
                appt_date = now + timedelta(days=random.randint(0, 7), hours=random.randint(1, 10))

            Appointment.objects.create(
                patient=patient, doctor=doctor,
                appointment_date=appt_date,
                status=status,
                reason=random.choice(reasons),
                notes=f'Appointment #{i + 1}' if random.random() > 0.5 else '',
            )
        self.stdout.write(f'  ✓ Created 50 appointments')

    def _create_medicines(self):
        if Medicine.objects.exists():
            self.stdout.write('  Medicines already exist, skipping...')
            return

        meds = [
            ('Amoxicillin 500mg', 'antibiotic', 150, 30, 'MedPlus Pharma'),
            ('Azithromycin 250mg', 'antibiotic', 200, 40, 'Sun Pharma'),
            ('Ciprofloxacin 500mg', 'antibiotic', 80, 25, 'Cipla Ltd'),
            ('Paracetamol 500mg', 'painkiller', 500, 100, 'Cipla Ltd'),
            ('Ibuprofen 400mg', 'painkiller', 300, 50, 'Dr. Reddys'),
            ('Diclofenac 50mg', 'painkiller', 250, 40, 'Sun Pharma'),
            ('Oseltamivir 75mg', 'antiviral', 60, 20, 'Hetero Drugs'),
            ('Remdesivir 100mg', 'antiviral', 15, 10, 'Gilead Sciences'),
            ('Aspirin 75mg', 'cardiac', 400, 80, 'Bayer India'),
            ('Atorvastatin 10mg', 'cardiac', 350, 60, 'Ranbaxy'),
            ('Metoprolol 50mg', 'cardiac', 180, 30, 'Torrent Pharma'),
            ('Amlodipine 5mg', 'cardiac', 220, 40, 'Pfizer India'),
            ('Salbutamol Inhaler', 'respiratory', 45, 15, 'Cipla Ltd'),
            ('Budesonide Inhaler', 'respiratory', 30, 10, 'AstraZeneca'),
            ('Montelukast 10mg', 'respiratory', 120, 25, 'Sun Pharma'),
            ('Omeprazole 20mg', 'gastrointestinal', 280, 50, 'Dr. Reddys'),
            ('Domperidone 10mg', 'gastrointestinal', 200, 35, 'Torrent Pharma'),
            ('Ondansetron 4mg', 'gastrointestinal', 150, 30, 'Sun Pharma'),
            ('Vitamin D3 1000IU', 'supplement', 400, 60, 'HealthVit'),
            ('Calcium + Vitamin D', 'supplement', 350, 50, 'Shelcal'),
            ('Iron Folic Acid', 'supplement', 300, 45, 'Mankind Pharma'),
            ('Lidocaine 2%', 'anesthetic', 100, 20, 'Neon Labs'),
            ('Propofol 1%', 'anesthetic', 25, 10, 'Fresenius Kabi'),
            ('Povidone Iodine', 'antiseptic', 200, 40, 'Win Medicare'),
            ('Chlorhexidine Solution', 'antiseptic', 180, 35, 'Raman & Weil'),
            ('Metformin 500mg', 'other', 250, 40, 'USV Limited'),
            ('Insulin Glargine', 'other', 20, 8, 'Sanofi India'),
            ('Levothyroxine 50mcg', 'other', 180, 30, 'Abbott India'),
            ('Phenytoin 100mg', 'other', 90, 20, 'Sun Pharma'),
            ('Dexamethasone 4mg', 'other', 120, 25, 'Zydus Cadila'),
        ]

        today = date.today()
        for name, cat, stock, min_s, supplier in meds:
            # Vary stock — some low, some near expiry
            actual_stock = stock
            if random.random() < 0.15:
                actual_stock = random.randint(0, min_s)  # Low stock
            elif random.random() < 0.1:
                actual_stock = 0  # Out of stock

            expiry_offset = random.randint(30, 730)
            if random.random() < 0.1:
                expiry_offset = random.randint(-30, 25)  # Near expiry or expired

            Medicine.objects.create(
                name=name, category=cat,
                stock=actual_stock, min_stock=min_s,
                expiry_date=today + timedelta(days=expiry_offset),
                supplier=supplier,
                unit_price=round(random.uniform(5, 500), 2),
            )
        self.stdout.write(f'  ✓ Created {len(meds)} medicines')

    def _create_alerts(self):
        if Alert.objects.count() > 5:
            self.stdout.write('  Alerts already exist, skipping...')
            return

        Alert.objects.all().delete()
        alerts = [
            ('critical', 'icu_high', 'ICU occupancy at 92% — only 1 bed remaining',
             'ICU Ward is nearing full capacity. Consider transferring stable patients.'),
            ('critical', 'patient_critical', 'Patient Aarav Sharma (ICU-03) — vitals dropping',
             'Heart rate irregular, SpO2 below 90%. Immediate attention required.'),
            ('critical', 'emergency_surge', '5 emergency patients admitted in last hour',
             'Unusual spike in emergency admissions. Additional staff may be needed.'),
            ('warning', 'medicine_low', 'Remdesivir stock critically low — 5 units remaining',
             'Current consumption rate will exhaust stock in 2 days.'),
            ('warning', 'staff_shortage', 'Night shift understaffed — 2 nurses short',
             'ICU and Emergency wards need additional nursing staff for night shift.'),
            ('warning', 'long_wait', 'Average wait time exceeded 45 minutes in Emergency',
             'Patient queue building up. Consider opening additional consultation room.'),
            ('warning', 'bed_shortage', 'General Ward A at 88% capacity',
             'Only 4 beds available. Upcoming admissions may need to be redirected.'),
            ('info', 'system', 'Shift handover completed — morning to afternoon',
             'All patient records transferred. 12 doctors and 8 nurses now on duty.'),
            ('info', 'system', 'AI prediction model updated with latest data',
             'Model retrained with last 48 hours of data. Accuracy: 94.2%'),
            ('info', 'system', 'Daily medicine inventory audit completed',
             'All stock levels verified. 3 items flagged for reorder.'),
        ]

        for level, atype, msg, desc in alerts:
            Alert.objects.create(
                level=level, alert_type=atype,
                message=msg, description=desc,
                source='AI Engine' if 'AI' in desc or 'predict' in desc.lower() else 'System Monitor',
                is_resolved=random.random() < 0.3,
            )
        self.stdout.write(f'  ✓ Created {len(alerts)} alerts')

    def _create_metrics(self):
        if HospitalMetric.objects.count() > 20:
            self.stdout.write('  Metrics already exist, skipping...')
            return

        HospitalMetric.objects.all().delete()
        now = timezone.now()

        for i in range(48):
            hour = (now - timedelta(hours=i)).hour
            # Simulate realistic patterns
            is_peak = 8 <= hour <= 20
            base_patients = random.randint(80, 130) if is_peak else random.randint(60, 100)

            HospitalMetric.objects.create(
                total_patients=base_patients,
                new_admissions=random.randint(3, 12) if is_peak else random.randint(1, 5),
                discharges=random.randint(1, 6) if is_peak else random.randint(0, 3),
                icu_occupancy=random.randint(7, 12),
                general_occupancy=random.randint(30, 50),
                emergency_patients=random.randint(2, 8) if is_peak else random.randint(1, 4),
                available_beds=random.randint(8, 25),
                avg_wait_time=round(random.uniform(15, 55) if is_peak else random.uniform(8, 30), 1),
                patient_inflow=random.randint(3, 15) if is_peak else random.randint(1, 6),
                doctors_on_duty=random.randint(8, 15),
                nurses_on_duty=random.randint(10, 20),
                staff_count=random.randint(35, 55),
                critical_alerts=random.randint(0, 4),
            )
        self.stdout.write('  ✓ Created 48 hours of metrics')

    def _create_predictions(self):
        """Seed the Prediction table from the trained models.

        The earlier version wrote random numbers, which meant the AI Forecast
        page and the forecast report showed values no model had produced.
        These rows now come from the Random Forest models, with the derived
        staffing and demand figures computed from the forecast admissions.
        """
        if Prediction.objects.exists():
            self.stdout.write('  Predictions already exist, skipping...')
            return

        from ai_engine.ml import forecast_next_24h, persist_forecast

        forecast = forecast_next_24h()
        if not forecast.get('available'):
            self.stdout.write(self.style.WARNING(
                '  ! Trained models not found — skipping forecast seeding.\n'
                '    Run: cd ai_engine && python train_models.py'
            ))
            return

        written = persist_forecast(forecast)
        summary = forecast['summary']

        # Derived operational forecasts, computed from the model's projected
        # admissions rather than invented: an admission needs roughly one
        # bed, a doctor covers ~12 patients and a nurse ~4, and each
        # admission consumes about 6 medicine units.
        today = date.today()
        daily_admissions = summary['total_predicted_admissions']
        available_beds = Bed.objects.filter(status='available').count()

        derived = [
            ('bed_availability',
             max(0, available_beds - daily_admissions * 0.35),
             f'Projected {daily_admissions:.0f} admissions against '
             f'{available_beds} free beds today.'),
            ('doctor_requirement',
             max(1, round(daily_admissions / 12, 1)),
             'Doctors needed to hold a 1:12 doctor-to-patient ratio '
             'at the projected load.'),
            ('nurse_requirement',
             max(1, round(daily_admissions / 4, 1)),
             'Nurses needed to hold a 1:4 nurse-to-patient ratio '
             'at the projected load.'),
            ('medicine_demand',
             round(daily_admissions * 6, 1),
             'Estimated medicine units consumed at the projected '
             'admission volume.'),
        ]

        for metric_type, value, recommendation in derived:
            Prediction.objects.update_or_create(
                date=today, metric_type=metric_type,
                defaults={
                    'predicted_value': value,
                    'confidence': forecast['points'][0]['confidence'],
                    'trend': summary['trend'],
                    'recommendation': recommendation,
                    'model_version': 'rf-v3.0-derived',
                },
            )

        self.stdout.write(
            f'  ✓ Created {written + len(derived)} model-generated predictions '
            f"(peak ICU {summary['peak_icu_percentage']}% at "
            f"{summary['peak_icu_at']})"
        )

    def _create_activity_logs(self):
        if ActivityLog.objects.count() > 5:
            self.stdout.write('  Activity logs already exist, skipping...')
            return

        ActivityLog.objects.all().delete()
        actions = [
            ('Patient admitted', 'Patient', 'New patient registration and bed assignment'),
            ('Bed assigned', 'Bed', 'Bed allocated to incoming patient'),
            ('Vitals recorded', 'Patient', 'Patient vital signs updated'),
            ('Medicine dispensed', 'Medicine', 'Medication administered to patient'),
            ('Alert generated', 'Alert', 'System generated automatic alert'),
            ('Report generated', 'Report', 'Hospital summary report exported'),
            ('Shift change', 'Staff', 'Staff shift handover completed'),
            ('Patient discharged', 'Patient', 'Patient discharged with instructions'),
            ('AI model prediction', 'Prediction', 'AI engine generated new forecast'),
            ('Appointment created', 'Appointment', 'New appointment scheduled'),
        ]

        admin = HospitalUser.objects.filter(username='admin').first()
        for action, model, details in actions:
            ActivityLog.objects.create(
                user=admin, action=action,
                target_model=model, details=details,
            )
        self.stdout.write('  ✓ Created activity logs')
