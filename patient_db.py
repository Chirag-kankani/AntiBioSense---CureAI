"""
Patient History Database — SQLite backend for AntibioSense.

PID format: DDMMYY-NNN
  - DDMMYY = date the patient was first registered
  - NNN    = 3-digit sequential counter for that day (001, 002, …)
  Example:  030426-001 = 1st patient registered on 3 Apr 2026

Tables:
  patients — demographics keyed by patient_id
  visits   — prediction snapshots linked to a patient
"""

import os, json, sqlite3, threading
from datetime import datetime

_DB_PATH = os.environ.get('DB_PATH', os.path.join(os.path.dirname(__file__), 'antibiosense.db'))
_local = threading.local()


def _get_conn():
    """Thread-local SQLite connection (safe for Flask request threads)."""
    if not hasattr(_local, 'conn') or _local.conn is None:
        _local.conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
    return _local.conn


def init_db():
    """Create tables if they don't exist. Called once at app startup."""
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS patients (
            patient_id   TEXT PRIMARY KEY,
            name         TEXT DEFAULT '',
            age          INTEGER,
            gender       TEXT,
            diabetes     TEXT DEFAULT '',
            hypertension TEXT DEFAULT '',
            hospital_before TEXT DEFAULT '',
            infection_freq  TEXT DEFAULT '',
            created_at   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS visits (
            visit_id     INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id   TEXT NOT NULL,
            timestamp    TEXT NOT NULL,
            symptoms     TEXT DEFAULT '[]',
            other_symptoms TEXT DEFAULT '',
            mode         TEXT DEFAULT 'symptom',
            predicted_species TEXT,
            species_confidence REAL,
            top3         TEXT DEFAULT '[]',
            mdr          INTEGER DEFAULT 0,
            warnings     TEXT DEFAULT '{}',
            full_response TEXT DEFAULT '{}',
            doctor_feedback TEXT DEFAULT '',
            FOREIGN KEY (patient_id) REFERENCES patients(patient_id)
        );

        CREATE INDEX IF NOT EXISTS idx_visits_patient ON visits(patient_id);
    """)
    conn.commit()
    print(f"[DB] Patient database ready at {_DB_PATH}")


# ── PID Generation ──────────────────────────────────────────────────

def generate_pid():
    """Generate a meaningful PID: DDMMYY-NNN."""
    conn = _get_conn()
    today = datetime.now()
    date_prefix = today.strftime("%d%m%y")  # e.g., "030426"

    # Count how many patients were created today
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM patients WHERE patient_id LIKE ?",
        (f"{date_prefix}-%",)
    ).fetchone()
    seq = (row['cnt'] if row else 0) + 1
    pid = f"{date_prefix}-{seq:03d}"

    # Handle unlikely collision
    while conn.execute("SELECT 1 FROM patients WHERE patient_id=?", (pid,)).fetchone():
        seq += 1
        pid = f"{date_prefix}-{seq:03d}"

    return pid


# ── CRUD Operations ─────────────────────────────────────────────────

def create_patient(name='', age=None, gender='', diabetes='',
                   hypertension='', hospital_before='', infection_freq=''):
    """Create a new patient and return the generated PID."""
    conn = _get_conn()
    pid = generate_pid()
    now = datetime.now().isoformat()
    conn.execute(
        """INSERT INTO patients
           (patient_id, name, age, gender, diabetes, hypertension,
            hospital_before, infection_freq, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (pid, name, age, gender, diabetes, hypertension,
         hospital_before, infection_freq, now)
    )
    conn.commit()
    return pid


def get_patient(patient_id):
    """Fetch patient demographics. Returns dict or None."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM patients WHERE patient_id = ?", (patient_id,)
    ).fetchone()
    if not row:
        return None
    return dict(row)


def search_patients(query):
    """Search patients by PID prefix or name substring. Returns list of dicts."""
    conn = _get_conn()
    rows = conn.execute(
        """SELECT * FROM patients
           WHERE patient_id LIKE ? OR name LIKE ?
           ORDER BY created_at DESC LIMIT 10""",
        (f"%{query}%", f"%{query}%")
    ).fetchall()
    return [dict(r) for r in rows]


def save_visit(patient_id, symptoms, other_symptoms, mode,
               predicted_species, species_confidence, top3,
               mdr, warnings, full_response):
    """Save a prediction as a visit under a patient."""
    conn = _get_conn()
    now = datetime.now().isoformat()
    conn.execute(
        """INSERT INTO visits
           (patient_id, timestamp, symptoms, other_symptoms, mode,
            predicted_species, species_confidence, top3, mdr,
            warnings, full_response)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (patient_id, now,
         json.dumps(symptoms) if isinstance(symptoms, list) else symptoms,
         other_symptoms or '', mode,
         predicted_species, species_confidence,
         json.dumps(top3) if isinstance(top3, list) else top3,
         1 if mdr else 0,
         json.dumps(warnings) if isinstance(warnings, dict) else warnings,
         json.dumps(full_response) if isinstance(full_response, dict) else full_response)
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def get_visits(patient_id):
    """Get all visits for a patient, newest first."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM visits WHERE patient_id = ? ORDER BY timestamp DESC",
        (patient_id,)
    ).fetchall()
    results = []
    for r in rows:
        d = dict(r)
        # Parse JSON fields back
        for field in ('symptoms', 'top3', 'warnings', 'full_response'):
            try:
                d[field] = json.loads(d[field]) if d[field] else {}
            except (json.JSONDecodeError, TypeError):
                pass
        results.append(d)
    return results


def get_visit_by_id(visit_id):
    """Get a single visit by its ID."""
    conn = _get_conn()
    row = conn.execute("SELECT * FROM visits WHERE visit_id = ?", (visit_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    for field in ('symptoms', 'top3', 'warnings', 'full_response'):
        try:
            d[field] = json.loads(d[field]) if d[field] else {}
        except (json.JSONDecodeError, TypeError):
            pass
    return d


def update_visit_feedback(visit_id, feedback):
    """Update doctor feedback for a visit ('positive' or 'negative')."""
    conn = _get_conn()
    conn.execute(
        "UPDATE visits SET doctor_feedback = ? WHERE visit_id = ?",
        (feedback, visit_id)
    )
    conn.commit()
