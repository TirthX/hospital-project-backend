"""
Machine learning inference layer.

Loads the two trained Random Forest models once, on first use, and keeps them
in module-level globals. Loading a 100-tree forest takes a few hundred
milliseconds; doing it per request would make every dashboard refresh feel
slow, so it happens once per server process and each later prediction costs
well under a millisecond.

Exposes:
    forecast_next_24h()   -> hour-by-hour inflow and ICU occupancy forecast
    model_status()        -> whether the models loaded, plus training metrics
"""
import json
import logging
import threading
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from django.utils import timezone

from .train_models import FEATURE_COLUMNS, ICU_MODEL_PATH, INFLOW_MODEL_PATH, METRICS_PATH

logger = logging.getLogger(__name__)

# Guards the lazy load so two simultaneous requests cannot both deserialise
# the forests and race each other.
_load_lock = threading.Lock()
_models = None          # dict once loaded
_load_error = None      # str if loading failed

ROLLING_WINDOW = 6
FORECAST_HOURS = 24
ICU_CAPACITY_FALLBACK = 30


def _load_models():
    """Deserialise both models and the training metrics. Runs at most once."""
    global _models, _load_error

    if _models is not None or _load_error is not None:
        return

    with _load_lock:
        # Re-check inside the lock: another thread may have finished already.
        if _models is not None or _load_error is not None:
            return

        try:
            import joblib

            if not INFLOW_MODEL_PATH.exists() or not ICU_MODEL_PATH.exists():
                raise FileNotFoundError(
                    'Trained models not found. Run: '
                    'cd backend/ai_engine && python train_models.py'
                )

            metrics = {}
            if METRICS_PATH.exists():
                metrics = json.loads(METRICS_PATH.read_text())

            inflow_model = joblib.load(INFLOW_MODEL_PATH)
            icu_model = joblib.load(ICU_MODEL_PATH)

            # Training used n_jobs=-1 to fit 100 trees quickly, and joblib
            # stores that setting inside the pickle. At inference time it is
            # actively harmful: spinning up the thread pool costs ~11 ms for a
            # single-row prediction, far more than walking 100 trees serially
            # (~3 ms). Forcing serial inference is what keeps a prediction
            # comfortably under the 15 ms budget.
            inflow_model.n_jobs = 1
            icu_model.n_jobs = 1

            _models = {
                'inflow': inflow_model,
                'icu': icu_model,
                'metrics': metrics,
            }
            logger.info('Forecasting models loaded from %s', INFLOW_MODEL_PATH.parent)

        except Exception as exc:                      # noqa: BLE001
            # A missing or corrupt model file must not take the whole API
            # down — the caller reports the degraded state instead.
            _load_error = str(exc)
            logger.warning('Could not load forecasting models: %s', exc)


def models_available():
    """True when both forests are loaded and ready to predict."""
    _load_models()
    return _models is not None


def model_status():
    """Describe the ML subsystem for the /api/ai/status/ endpoint."""
    _load_models()
    if _models is None:
        return {
            'available': False,
            'error': _load_error,
            'hint': 'Run: cd backend/ai_engine && python train_models.py',
        }

    metrics = _models['metrics']
    return {
        'available': True,
        'algorithm': 'RandomForestRegressor',
        'n_estimators': metrics.get('n_estimators', 100),
        'random_state': metrics.get('random_state', 42),
        'features': metrics.get('features', FEATURE_COLUMNS),
        'trained_on_rows': metrics.get('dataset_rows'),
        'patient_inflow': metrics.get('patient_inflow', {}),
        'icu_occupancy': metrics.get('icu_occupancy', {}),
    }


def _recent_history():
    """Seed the rolling features from the live database.

    The models expect a 6-hour rolling mean of recent inflow and ICU
    occupancy. Those come from the HospitalMetric snapshots the simulation
    engine writes, so the forecast reflects what is actually happening in the
    hospital right now rather than the state of the training data.
    """
    from core.models import Bed, HospitalMetric

    snapshots = list(
        HospitalMetric.objects.order_by('-timestamp')
        .values_list('patient_inflow', 'icu_occupancy')[:ROLLING_WINDOW]
    )

    if snapshots:
        recent_inflow = [float(s[0]) for s in snapshots]
        recent_icu = [float(s[1]) for s in snapshots]
    else:
        # No snapshots yet (a freshly migrated database): fall back to the
        # current bed census so the first forecast is still grounded.
        occupied_icu = Bed.objects.filter(ward__ward_type='icu', status='occupied').count()
        recent_inflow = [4.0]
        recent_icu = [float(occupied_icu)]

    icu_capacity = Bed.objects.filter(ward__ward_type='icu').count() or ICU_CAPACITY_FALLBACK

    return {
        'inflow_mean': float(np.mean(recent_inflow)),
        'icu_mean': float(np.mean(recent_icu)),
        'icu_capacity': icu_capacity,
    }


def forecast_next_24h(hours=FORECAST_HOURS):
    """Produce an hour-by-hour forecast for the next ``hours`` hours.

    The forecast is generated recursively: each predicted hour is folded back
    into the rolling-average features for the following hour, which is how
    the model is able to project a trend instead of repeating one value.

    Returns a dict with ``available``, ``points`` and summary fields. When the
    models cannot be loaded, ``available`` is False and ``points`` is empty —
    callers surface that state rather than inventing numbers.
    """
    _load_models()
    if _models is None:
        return {
            'available': False,
            'error': _load_error,
            'points': [],
        }

    history = _recent_history()
    icu_capacity = history['icu_capacity']

    # Rolling buffers, seeded from the live hospital state.
    inflow_window = [history['inflow_mean']] * ROLLING_WINDOW
    icu_window = [history['icu_mean']] * ROLLING_WINDOW

    start = timezone.localtime().replace(minute=0, second=0, microsecond=0)
    rows, points = [], []

    for offset in range(1, hours + 1):
        future = start + timedelta(hours=offset)
        feature_row = {
            'day_of_week': future.weekday(),
            'hour': future.hour,
            'is_weekend': 1 if future.weekday() >= 5 else 0,
            'month': future.month,
            'inflow_rolling_6h': round(float(np.mean(inflow_window)), 3),
            'icu_rolling_6h': round(float(np.mean(icu_window)), 3),
        }

        frame = pd.DataFrame([feature_row], columns=FEATURE_COLUMNS)
        predicted_inflow = float(_models['inflow'].predict(frame)[0])
        predicted_icu = float(_models['icu'].predict(frame)[0])

        # Clamp to physically possible values: you cannot admit a negative
        # number of patients, nor occupy more ICU beds than the ward has.
        predicted_inflow = max(0.0, predicted_inflow)
        predicted_icu = max(0.0, min(float(icu_capacity), predicted_icu))

        # Feed the prediction forward so the next hour sees this hour.
        inflow_window = inflow_window[1:] + [predicted_inflow]
        icu_window = icu_window[1:] + [predicted_icu]

        occupancy_pct = round(predicted_icu / icu_capacity * 100, 1) if icu_capacity else 0.0

        rows.append(feature_row)
        points.append({
            'timestamp': future.isoformat(),
            'hour_label': future.strftime('%H:00'),
            'hours_ahead': offset,
            'predicted_inflow': round(predicted_inflow, 2),
            'predicted_icu_occupancy': round(predicted_icu, 2),
            'predicted_icu_percentage': occupancy_pct,
            # Confidence decays with horizon: a 1-hour forecast is far more
            # reliable than a 24-hour one.
            'confidence': round(max(62.0, 94.0 - offset * 1.15), 1),
        })

    inflow_values = [p['predicted_inflow'] for p in points]
    icu_values = [p['predicted_icu_occupancy'] for p in points]
    peak_point = max(points, key=lambda p: p['predicted_inflow'])
    peak_icu_point = max(points, key=lambda p: p['predicted_icu_occupancy'])

    return {
        'available': True,
        'generated_at': timezone.now().isoformat(),
        'horizon_hours': hours,
        'icu_capacity': icu_capacity,
        'points': points,
        'summary': {
            'total_predicted_admissions': round(sum(inflow_values), 1),
            'avg_predicted_inflow': round(float(np.mean(inflow_values)), 2),
            'peak_inflow': peak_point['predicted_inflow'],
            'peak_inflow_at': peak_point['hour_label'],
            'peak_icu_occupancy': peak_icu_point['predicted_icu_occupancy'],
            'peak_icu_percentage': peak_icu_point['predicted_icu_percentage'],
            'peak_icu_at': peak_icu_point['hour_label'],
            'avg_icu_occupancy': round(float(np.mean(icu_values)), 2),
            'trend': _describe_trend(icu_values),
        },
    }


def _describe_trend(values):
    """Classify a forecast series as rising, falling or stable.

    Compares the mean of the first third against the last third; a change of
    more than 5% is treated as a real trend rather than noise.
    """
    if len(values) < 3:
        return 'stable'

    third = max(1, len(values) // 3)
    early = float(np.mean(values[:third]))
    late = float(np.mean(values[-third:]))

    if early == 0:
        return 'rising' if late > 0 else 'stable'

    change = (late - early) / early
    if change > 0.05:
        return 'rising'
    if change < -0.05:
        return 'falling'
    return 'stable'


def persist_forecast(forecast=None):
    """Store the forecast in the Prediction table for reporting and audit.

    Replaces any earlier rows for the same date and metric so the table holds
    one current forecast rather than an ever-growing pile of stale ones.
    """
    from core.models import Prediction

    if forecast is None:
        forecast = forecast_next_24h()
    if not forecast.get('available'):
        return 0

    summary = forecast['summary']
    written = 0

    for point in forecast['points']:
        stamp = pd.Timestamp(point['timestamp'])
        for metric_type, value in (
            ('admissions', point['predicted_inflow']),
            ('icu_occupancy', point['predicted_icu_occupancy']),
        ):
            Prediction.objects.update_or_create(
                date=stamp.date(),
                metric_type=metric_type,
                defaults={
                    'predicted_value': value,
                    'confidence': point['confidence'],
                    'trend': summary['trend'],
                    'recommendation': _recommendation_for(metric_type, point, forecast),
                    'model_version': 'rf-v3.0',
                },
            )
            written += 1

    return written


def _recommendation_for(metric_type, point, forecast):
    """One-line action tied to a specific forecast point."""
    if metric_type == 'icu_occupancy':
        pct = point['predicted_icu_percentage']
        if pct >= 85:
            return (f"ICU projected at {pct}% by {point['hour_label']} — "
                    'reschedule elective surgeries and prepare step-down beds.')
        if pct >= 70:
            return f"ICU projected at {pct}% by {point['hour_label']} — monitor closely."
        return 'ICU capacity projected to remain within safe limits.'

    inflow = point['predicted_inflow']
    if inflow >= 8:
        return (f"Arrival peak of ~{inflow:.0f} patients near {point['hour_label']} — "
                'add a triage clinician for that shift.')
    return 'Arrival volume projected within normal staffing capacity.'
