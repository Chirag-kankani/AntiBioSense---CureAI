"""
predict_standalone.py
=====================
Run this file directly to test the antibiotic resistance model
without needing the web app.

Usage:
    python predict_standalone.py

You can change the patient inputs at the bottom of the file.
"""

import os, json
import pandas as pd
import numpy as np
from artifact_loader import load_artifact_bundle

# ─── Load models ─────────────────────────────────────────────────────────────
BASE = os.path.dirname(os.path.abspath(__file__))

print("Loading models...")
META, MODELS, _CARD = load_artifact_bundle(BASE)
print(f"Loaded {len(MODELS)} antibiotic models.\n")

AB_COLS      = META['ab_cols']
AB_NAMES     = META['ab_full_names']
AB_CLASSES   = META['ab_classes']
SPECIES_LIST = META['species_list']

# Infection frequency mapping (numeric → category string the model expects)
FREQ_MAP = {
    0: 'Never', 1: 'Rarely', 2: 'Regularly', 3: 'Often',
    '0':'Never','1':'Rarely','2':'Regularly','3':'Often'
}

# ─── Symptom → species rule engine ───────────────────────────────────────────
SYMPTOM_WEIGHTS = {
    'Burning urination':        {'Escherichia coli':4,'Proteus mirabilis':3,'Klebsiella pneumoniae':2,'Morganella morganii':1},
    'Frequent urination':       {'Escherichia coli':4,'Klebsiella pneumoniae':2,'Citrobacter spp.':1},
    'Cloudy/bloody urine':      {'Escherichia coli':3,'Proteus mirabilis':3,'Klebsiella pneumoniae':2},
    'Foul-smelling urine':      {'Proteus mirabilis':4,'Morganella morganii':3,'Escherichia coli':2},
    'Pelvic pain':              {'Escherichia coli':3,'Enterobacteria spp.':2},
    'Flank/back pain':          {'Proteus mirabilis':4,'Escherichia coli':3,'Klebsiella pneumoniae':2},
    'Cough with thick mucus':   {'Klebsiella pneumoniae':4,'Pseudomonas aeruginosa':3,'Serratia marcescens':2},
    'Chest pain':               {'Klebsiella pneumoniae':3,'Pseudomonas aeruginosa':3,'Serratia marcescens':2},
    'Coughing blood':           {'Klebsiella pneumoniae':4,'Pseudomonas aeruginosa':3},
    'Shortness of breath':      {'Klebsiella pneumoniae':3,'Pseudomonas aeruginosa':2,'Acinetobacter baumannii':2},
    'Wound infection':          {'Pseudomonas aeruginosa':4,'Acinetobacter baumannii':3,'Serratia marcescens':2},
    'Green/foul pus':           {'Pseudomonas aeruginosa':5,'Serratia marcescens':2},
    'Skin ulcer/necrosis':      {'Acinetobacter baumannii':4,'Pseudomonas aeruginosa':3},
    'Diarrhea':                 {'Escherichia coli':4,'Enterobacteria spp.':3,'Citrobacter spp.':2},
    'Abdominal cramps':         {'Escherichia coli':3,'Enterobacteria spp.':3,'Citrobacter spp.':2},
    'Nausea/vomiting':          {'Escherichia coli':2,'Klebsiella pneumoniae':2,'Enterobacteria spp.':2},
    'Blood in stool':           {'Escherichia coli':4,'Enterobacteria spp.':2},
    'High fever (>38.5C)':      {'Klebsiella pneumoniae':3,'Serratia marcescens':3,'Acinetobacter baumannii':2,'Escherichia coli':2},
    'Chills/rigors':            {'Serratia marcescens':4,'Klebsiella pneumoniae':3,'Pseudomonas aeruginosa':2},
    'Rapid heart rate':         {'Serratia marcescens':4,'Acinetobacter baumannii':3,'Klebsiella pneumoniae':2},
    'Low blood pressure':       {'Acinetobacter baumannii':4,'Serratia marcescens':4,'Pseudomonas aeruginosa':3},
    'Confusion/altered mental': {'Acinetobacter baumannii':3,'Klebsiella pneumoniae':2,'Serratia marcescens':2},
}

def predict_species_from_symptoms(symptoms: list) -> tuple:
    """
    Given a list of symptom strings, return the most likely bacterial
    species and a probability dict for the top 5 candidates.
    """
    if not symptoms:
        return None, {}
    scores = {sp: 0 for sp in SPECIES_LIST}
    for sym in symptoms:
        if sym in SYMPTOM_WEIGHTS:
            for sp, w in SYMPTOM_WEIGHTS[sym].items():
                if sp in scores:
                    scores[sp] += w
    total = sum(scores.values())
    if total == 0:
        return None, {}
    probs = {sp: round(v / total * 100, 1) for sp, v in scores.items() if v > 0}
    top   = max(scores, key=scores.get)
    return top, dict(sorted(probs.items(), key=lambda x: -x[1])[:5])


def predict_resistance(
    species: str,
    age: int        = None,
    gender: str     = None,   # 'M' or 'F'
    diabetes: str   = None,   # 'Yes' or 'No'
    hypertension: str  = None,
    hospital_before: str = None,
    infection_freq: int = None,  # 0=Never 1=Rarely 2=Regularly 3=Often
) -> list:
    """
    Predict resistance for all 15 antibiotics given patient details.

    Returns a list of dicts sorted by susceptibility % (best first):
        [{ 'code', 'name', 'drug_class',
           'susceptible_pct', 'resistant_pct', 'intermediate_pct',
           'verdict', 'model_accuracy' }, ...]
    """
    inf_cat = FREQ_MAP.get(infection_freq, None)

    input_df = pd.DataFrame([{
        'age':              float(age) if age is not None else np.nan,
        'gender':           gender,
        'Diabetes':         diabetes,
        'Hypertension':     hypertension,
        'Hospital_before':  hospital_before,
        'Infection_Freq_cat': inf_cat,
        'species':          species,
    }])

    results = []
    for ab in AB_COLS:
        if ab not in MODELS:
            continue
        pipe = MODELS[ab]['pipeline']
        le   = MODELS[ab]['le']
        try:
            proba = pipe.predict_proba(input_df)[0]
            prob  = {cls: round(float(p) * 100, 1) for cls, p in zip(le.classes_, proba)}
            s = prob.get('Susceptible',   0)
            r = prob.get('Resistant',     0)
            i = prob.get('Intermediate',  0)
            verdict = 'Susceptible' if s >= 60 else ('Resistant' if r >= 60 else 'Intermediate')
            results.append({
                'code':             ab,
                'name':             AB_NAMES[ab],
                'drug_class':       AB_CLASSES[ab],
                'susceptible_pct':  s,
                'resistant_pct':    r,
                'intermediate_pct': i,
                'verdict':          verdict,
                'model_accuracy':   META['model_meta'][ab]['accuracy'],
            })
        except Exception as e:
            print(f"  [warn] {ab}: {e}")

    results.sort(key=lambda x: -x['susceptible_pct'])
    return results


def print_report(patient_label: str, species: str, species_probs: dict, results: list):
    """Pretty-print a full resistance report to the terminal."""
    RESET = '\033[0m'
    BOLD  = '\033[1m'
    GREEN = '\033[92m'
    AMBER = '\033[93m'
    RED   = '\033[91m'
    CYAN  = '\033[96m'
    BLUE  = '\033[94m'

    mdr = sum(1 for r in results if r['resistant_pct'] >= 60) >= 3

    print()
    print("=" * 70)
    print(f"{BOLD}  ANTIBIOTIC RESISTANCE REPORT — {patient_label}{RESET}")
    print("=" * 70)

    # Species
    print(f"\n{BOLD}  Identified pathogen:{RESET} {CYAN}{species}{RESET}")
    if len(species_probs) > 1:
        print(f"  Confidence scores: ", end='')
        print(', '.join(f"{sp} {p}%" for sp, p in list(species_probs.items())[:4]))

    if mdr:
        print(f"\n  {RED}{BOLD}⚠  MULTI-DRUG RESISTANCE DETECTED (resistant to 3+ antibiotics){RESET}")
        print(f"  {RED}   Consult an infectious disease specialist immediately.{RESET}")

    # Top 3
    print(f"\n{BOLD}  TOP 3 RECOMMENDED ANTIBIOTICS:{RESET}")
    medals = ['🥇', '🥈', '🥉']
    for idx, r in enumerate(results[:3]):
        col = GREEN if r['susceptible_pct'] >= 60 else AMBER
        print(f"  {medals[idx]}  {col}{BOLD}{r['name']}{RESET}  ({r['drug_class']})")
        print(f"       Susceptible: {col}{r['susceptible_pct']:.1f}%{RESET}  |  "
              f"Intermediate: {r['intermediate_pct']:.1f}%  |  "
              f"Resistant: {RED}{r['resistant_pct']:.1f}%{RESET}")
        print(f"       Model accuracy: {r['model_accuracy']}%")

    # Full table
    print(f"\n{BOLD}  FULL RESISTANCE PROFILE (all 15 antibiotics):{RESET}")
    print(f"  {'Antibiotic':<32} {'Class':<25} {'S%':>6} {'I%':>6} {'R%':>6}  Verdict")
    print(f"  {'-'*32} {'-'*25} {'-'*6} {'-'*6} {'-'*6}  -------")
    for r in results:
        s, i, rv = r['susceptible_pct'], r['intermediate_pct'], r['resistant_pct']
        v = r['verdict']
        v_col = GREEN if v == 'Susceptible' else (RED if v == 'Resistant' else AMBER)
        print(f"  {r['name']:<32} {r['drug_class']:<25} "
              f"{GREEN}{s:>5.1f}%{RESET} {AMBER}{i:>5.1f}%{RESET} {RED}{rv:>5.1f}%{RESET}  "
              f"{v_col}{v}{RESET}")

    # Feature importance for top antibiotic
    top_code = results[0]['code']
    fi = META['feature_importances'].get(top_code, {})
    if fi:
        print(f"\n{BOLD}  FEATURE IMPORTANCE for {results[0]['name']} model:{RESET}")
        max_fi = max(fi.values()) or 1
        name_map = {
            'species':'Bacterial species','age':'Age',
            'Infection_Freq_cat':'Infection frequency','Hospital_before':'Prior hospitalisation',
            'Diabetes':'Diabetes','gender':'Gender','Hypertension':'Hypertension'
        }
        for feat, score in sorted(fi.items(), key=lambda x: -x[1]):
            bar_len = int(score / max_fi * 30)
            bar = '█' * bar_len + '░' * (30 - bar_len)
            label = name_map.get(feat, feat)
            print(f"  {label:<25} {BLUE}{bar}{RESET} {score*100:.1f}%")

    print("\n" + "=" * 70)
    print(f"  {'⚠  Disclaimer:'} This tool is for research only.")
    print(f"  Always confirm with laboratory AST (antibiotic sensitivity testing).")
    print("=" * 70)


# ═══════════════════════════════════════════════════════════════════════════════
# INTERACTIVE MODE
# ═══════════════════════════════════════════════════════════════════════════════

def get_choice(prompt, options, allow_skip=True):
    """Helper to pick from a numbered list."""
    print(f"\n{prompt}")
    for i, opt in enumerate(options, 1):
        print(f"  {i}. {opt}")
    if allow_skip:
        print("  0. Skip / Unknown")
    while True:
        try:
            raw = input("  Enter number: ").strip()
            if allow_skip and raw == '0':
                return None
            idx = int(raw) - 1
            if 0 <= idx < len(options):
                return options[idx]
        except (ValueError, KeyboardInterrupt):
            pass
        print("  Invalid — try again.")


def interactive_mode():
    print("\n" + "=" * 70)
    print("  ANTIBIOSENSE — Standalone Prediction Tool")
    print("  (press Ctrl+C at any time to exit)")
    print("=" * 70)

    # How to determine species
    print("\nHow do you want to identify the bacterial species?")
    print("  1. Enter symptoms (rule-based prediction)")
    print("  2. Select species directly")
    mode = input("  Enter 1 or 2: ").strip()

    species = None
    species_probs = {}

    if mode == '1':
        print("\nAvailable symptoms (enter comma-separated numbers):")
        syms = list(SYMPTOM_WEIGHTS.keys())
        for i, s in enumerate(syms, 1):
            print(f"  {i:2d}. {s}")
        raw = input("\n  Your selection (e.g. 1,3,5): ").strip()
        chosen = []
        for part in raw.split(','):
            try:
                idx = int(part.strip()) - 1
                if 0 <= idx < len(syms):
                    chosen.append(syms[idx])
            except ValueError:
                pass
        if not chosen:
            print("  No valid symptoms selected. Exiting.")
            return
        print(f"\n  Selected: {', '.join(chosen)}")
        species, species_probs = predict_species_from_symptoms(chosen)
        if not species:
            print("  Could not determine species. Please try selecting species directly.")
            return
        print(f"  → Predicted species: {species}")
    else:
        species = get_choice("Select bacterial species:", SPECIES_LIST, allow_skip=False)
        species_probs = {species: 100.0}

    # Patient details
    print("\n--- Patient Details (press Enter to skip any field) ---")
    age_raw = input("  Age (number): ").strip()
    age = int(age_raw) if age_raw.isdigit() else None

    gender = get_choice("Gender:", ['F', 'M'])
    diabetes = get_choice("Diabetes:", ['Yes', 'No'])
    hypertension = get_choice("Hypertension:", ['Yes', 'No'])
    hospital = get_choice("Prior hospitalisation:", ['Yes', 'No'])
    freq = get_choice("Infection frequency:", ['0 = Never','1 = Rarely','2 = Regularly','3 = Often'])
    inf_freq = int(freq[0]) if freq else None

    label = f"Age {age or '?'}/{gender or '?'}, {species}"

    print("\nRunning prediction...")
    results = predict_resistance(
        species=species, age=age, gender=gender,
        diabetes=diabetes, hypertension=hypertension,
        hospital_before=hospital, infection_freq=inf_freq
    )

    print_report(label, species, species_probs, results)


# ═══════════════════════════════════════════════════════════════════════════════
# BATCH / QUICK TEST MODE
# Change the examples below and run: python predict_standalone.py
# ═══════════════════════════════════════════════════════════════════════════════

QUICK_TESTS = [
    {
        'label': 'Female 45, Diabetic, E.coli UTI',
        'symptoms': ['Burning urination', 'Frequent urination', 'Pelvic pain'],
        'age': 45, 'gender': 'F', 'diabetes': 'Yes',
        'hypertension': 'No', 'hospital_before': 'No', 'infection_freq': 1,
    },
    {
        'label': 'Male 68, Hospital patient, Pseudomonas wound',
        'species': 'Pseudomonas aeruginosa',
        'age': 68, 'gender': 'M', 'diabetes': 'No',
        'hypertension': 'Yes', 'hospital_before': 'Yes', 'infection_freq': 3,
    },
    {
        'label': 'Female 30, Klebsiella pneumonia',
        'species': 'Klebsiella pneumoniae',
        'age': 30, 'gender': 'F', 'diabetes': 'No',
        'hypertension': 'No', 'hospital_before': 'No', 'infection_freq': 0,
    },
]


def run_quick_tests():
    for test in QUICK_TESTS:
        label    = test['label']
        symptoms = test.get('symptoms', [])
        species  = test.get('species', None)

        if symptoms and not species:
            species, species_probs = predict_species_from_symptoms(symptoms)
        else:
            species_probs = {species: 100.0}

        results = predict_resistance(
            species=species,
            age=test.get('age'),
            gender=test.get('gender'),
            diabetes=test.get('diabetes'),
            hypertension=test.get('hypertension'),
            hospital_before=test.get('hospital_before'),
            infection_freq=test.get('infection_freq'),
        )
        print_report(label, species, species_probs, results)


# ═══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    import sys

    if '--quick' in sys.argv:
        # python predict_standalone.py --quick
        run_quick_tests()
    else:
        # python predict_standalone.py  → interactive mode
        try:
            interactive_mode()
        except KeyboardInterrupt:
            print("\n\nExiting. Goodbye!")
