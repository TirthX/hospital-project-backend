"""
Train the two Random Forest forecasting models.

Models produced (saved next to this file as .joblib so the API can load them
instantly at start-up):

  patient_inflow_model.joblib   -> new patient arrivals in a given hour
  icu_occupancy_model.joblib    -> occupied ICU beds in a given hour

Both are RandomForestRegressor(n_estimators=100, random_state=42) as recorded
in the project report. The script evaluates on a held-out *chronological*
tail of the dataset rather than a random split, because shuffling time-series
rows lets the model peek at the future and reports an accuracy it would never
reach in production.

Run from the backend/ai_engine directory:
    python train_models.py
"""
import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_absolute_error, r2_score

# Feature columns, in the exact order the API must supply them at inference
# time. FEATURE_COLUMNS is imported by ai_engine.ml so the two can never
# disagree about column order.
FEATURE_COLUMNS = [
    'day_of_week',
    'hour',
    'is_weekend',
    'month',
    'inflow_rolling_6h',
    'icu_rolling_6h',
]

RANDOM_STATE = 42
N_ESTIMATORS = 100
TEST_FRACTION = 0.2

HERE = Path(__file__).resolve().parent
DATA_PATH = HERE.parent.parent / 'data' / 'historical_hospital_data.csv'
INFLOW_MODEL_PATH = HERE / 'patient_inflow_model.joblib'
ICU_MODEL_PATH = HERE / 'icu_occupancy_model.joblib'
METRICS_PATH = HERE / 'model_metrics.json'


def load_dataset(path=DATA_PATH):
    """Load the historical CSV, deriving any missing feature columns."""
    if not path.exists():
        raise FileNotFoundError(
            f'Dataset not found at {path}.\n'
            'Generate it first:  cd data && python generate_data.py'
        )

    df = pd.read_csv(path, parse_dates=['timestamp'])

    # Tolerate an older CSV that predates the extra feature columns.
    if 'is_weekend' not in df.columns:
        df['is_weekend'] = (df['day_of_week'] >= 5).astype(int)
    if 'month' not in df.columns:
        df['month'] = df['timestamp'].dt.month
    if 'inflow_rolling_6h' not in df.columns or 'icu_rolling_6h' not in df.columns:
        import sys
        sys.path.insert(0, str(DATA_PATH.parent))
        from generate_data import add_rolling_features
        df = add_rolling_features(df)

    return df.dropna(subset=FEATURE_COLUMNS + ['patient_inflow', 'icu_occupancy'])


def _train_one(name, X_train, y_train, X_test, y_test, out_path):
    """Fit one regressor, report held-out accuracy and persist it."""
    model = RandomForestRegressor(
        n_estimators=N_ESTIMATORS,
        random_state=RANDOM_STATE,
        criterion='squared_error',
        n_jobs=-1,
    )
    model.fit(X_train, y_train)

    predictions = model.predict(X_test)
    mae = mean_absolute_error(y_test, predictions)
    r2 = r2_score(y_test, predictions)
    # Baseline: always predict the training mean. A model that cannot beat
    # this has learned nothing, so it is worth printing side by side.
    baseline_mae = mean_absolute_error(y_test, np.full_like(y_test, y_train.mean(), dtype=float))

    joblib.dump(model, out_path)

    importance = dict(zip(FEATURE_COLUMNS, model.feature_importances_.round(4)))
    print(f'\n{name}')
    print(f'  MAE            {mae:.3f}   (mean-baseline {baseline_mae:.3f})')
    print(f'  R^2            {r2:.3f}')
    print(f'  feature importance {importance}')
    print(f'  saved -> {out_path.name}')

    return {
        'mae': round(float(mae), 4),
        'baseline_mae': round(float(baseline_mae), 4),
        'r2': round(float(r2), 4),
        'feature_importance': {k: float(v) for k, v in importance.items()},
    }


def train_models():
    df = load_dataset()
    print(f'Loaded {len(df):,} hourly observations from {DATA_PATH.name}')

    X = df[FEATURE_COLUMNS]

    # Chronological split — train on the earlier period, test on the tail.
    split_at = int(len(df) * (1 - TEST_FRACTION))
    X_train, X_test = X.iloc[:split_at], X.iloc[split_at:]
    print(f'Train rows {len(X_train):,} | held-out rows {len(X_test):,} '
          f'(chronological, no shuffling)')

    metrics = {
        'dataset_rows': int(len(df)),
        'train_rows': int(len(X_train)),
        'test_rows': int(len(X_test)),
        'n_estimators': N_ESTIMATORS,
        'random_state': RANDOM_STATE,
        'features': FEATURE_COLUMNS,
    }

    y_inflow = df['patient_inflow']
    metrics['patient_inflow'] = _train_one(
        'Patient inflow model',
        X_train, y_inflow.iloc[:split_at],
        X_test, y_inflow.iloc[split_at:],
        INFLOW_MODEL_PATH,
    )

    y_icu = df['icu_occupancy']
    metrics['icu_occupancy'] = _train_one(
        'ICU occupancy model',
        X_train, y_icu.iloc[:split_at],
        X_test, y_icu.iloc[split_at:],
        ICU_MODEL_PATH,
    )

    METRICS_PATH.write_text(json.dumps(metrics, indent=2))
    print(f'\nMetrics written to {METRICS_PATH.name}')
    return metrics


if __name__ == '__main__':
    train_models()
