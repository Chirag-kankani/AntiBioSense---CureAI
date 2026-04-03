import os, json, traceback, pickle, gc
import numpy as np, pandas as pd
from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv
from artifact_loader import load_artifact_bundle
import patient_db

load_dotenv()

app = Flask(__name__)
BASE = os.path.dirname(__file__)

# ── Load models & data ───────────────────────────────────────────────
META, MODELS, CARD = load_artifact_bundle(BASE)

# ── Initialize patient database (creates tables if they don't exist) ─
patient_db.init_db()

# ── Load contraindications knowledge base ────────────────────────────
_contra_path = os.path.join(BASE, 'models', 'contraindications.json')
if os.path.exists(_contra_path):
    with open(_contra_path, encoding='utf-8') as f:
        CONTRAINDICATIONS = json.load(f)
    print(f"[INIT] Loaded contraindications for {len(CONTRAINDICATIONS)} antibiotics")
else:
    CONTRAINDICATIONS = {}
    print("[INIT] contraindications.json not found — red-flag warnings disabled")

# ── Load doctor feedback memory ──────────────────────────────────────
_feedback_path = os.environ.get('FEEDBACK_PATH', os.path.join(BASE, 'feedback.json'))

def _load_feedback():
    """Read feedback.json from disk (thread-safe for reads)."""
    try:
        if os.path.exists(_feedback_path):
            with open(_feedback_path, encoding='utf-8') as f:
                return json.load(f)
    except (json.JSONDecodeError, IOError):
        pass
    return {}

def _save_feedback(data):
    """Write feedback.json to disk."""
    with open(_feedback_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def get_clinician_trust(species, ab_results):
    """Look up clinician trust ratios for each antibiotic for the given species.
    Returns a dict keyed by antibiotic code with trust info."""
    feedback = _load_feedback()
    species_fb = feedback.get(species, {})
    if not species_fb:
        return {}
    trust_map = {}
    for ab in ab_results:
        code = ab['code']
        fb = species_fb.get(code)
        if fb:
            pos = fb.get('positive', 0)
            neg = fb.get('negative', 0)
            total = pos + neg
            trust = round(pos / total, 2) if total > 0 else None
            trust_map[code] = {
                'positive': pos,
                'negative': neg,
                'total': total,
                'trust': trust,
            }
    return trust_map

# ── Groq LLM Setup ──────────────────────────────────────────────────
def _dedupe_preserve_order(values):
    seen = set()
    ordered = []
    for value in values:
        if value and value not in seen:
            ordered.append(value)
            seen.add(value)
    return ordered


def _env_list(value):
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


GROQ_KEYS = _dedupe_preserve_order([
    os.environ.get("GROQ_API_KEY_1", ""),
    os.environ.get("GROQ_API_KEY_2", ""),
    os.environ.get("GROQ_API_KEY", ""),
    *_env_list(os.environ.get("GROQ_API_KEYS", "")),
])

GROQ_MODELS = _dedupe_preserve_order(
    _env_list(os.environ.get("GROQ_MODEL", ""))
    + _env_list(os.environ.get("GROQ_MODEL_FALLBACKS", ""))
    + [
        "llama-3.3-70b-versatile",
        "llama-3.1-70b-versatile",
        "llama-3.1-8b-instant",
    ]
)


def _parse_json_content(content):
    if not content:
        return None
    if isinstance(content, (dict, list)):
        return content
    if not isinstance(content, str):
        return None

    stripped = content.strip()
    try:
        return json.loads(stripped)
    except Exception:
        pass

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(stripped[start:end + 1])
        except Exception:
            return None
    return None

# In-memory caches (survive across requests within same server process)
_species_cache = {}
_insights_cache = {}


def call_groq(messages, json_mode=True, model_candidates=None):
    """Call Groq API with automatic key and model failover."""
    try:
        from groq import Groq
    except ImportError:
        print("[LLM] groq package not installed — skipping")
        return None

    keys = GROQ_KEYS or [os.environ.get("GROQ_API_KEY", "")]
    models = model_candidates or GROQ_MODELS
    last_error = None

    for key in keys:
        if not key:
            continue
        for model in models:
            try:
                client = Groq(api_key=key)
                kwargs = {
                    "model": model,
                    "messages": messages,
                    "temperature": 0.1,
                    "max_tokens": 2048,
                }
                if json_mode:
                    kwargs["response_format"] = {"type": "json_object"}
                resp = client.chat.completions.create(**kwargs)
                content = resp.choices[0].message.content
                if json_mode:
                    parsed = _parse_json_content(content)
                    if isinstance(parsed, dict):
                        return parsed
                    last_error = ValueError(f"Invalid JSON from model {model}")
                    print(f"[LLM] Invalid JSON from model {model} using key ...{key[-4:]}")
                    continue
                return content
            except Exception as e:
                last_error = e
                print(f"[LLM] Groq error (key ...{key[-4:]}, model {model}): {e}")
                continue

    if last_error:
        print(f"[LLM] Groq request exhausted all keys/models: {last_error}")
    return None


# ── Species lists & constants ────────────────────────────────────────
FREQ_MAP = {0: 'Never', 1: 'Rarely', 2: 'Regularly', 3: 'Often',
            '0': 'Never', '1': 'Rarely', '2': 'Regularly', '3': 'Often'}

SPECIES_LIST = META['species_list']
AB_COLS = META['ab_cols']

# ── Fallback: rule-based symptom weights (kept as safety net) ────────
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
    'High fever (>38.5°C)':     {'Klebsiella pneumoniae':3,'Serratia marcescens':3,'Acinetobacter baumannii':2,'Escherichia coli':2},
    'Chills/rigors':            {'Serratia marcescens':4,'Klebsiella pneumoniae':3,'Pseudomonas aeruginosa':2},
    'Rapid heart rate':         {'Serratia marcescens':4,'Acinetobacter baumannii':3,'Klebsiella pneumoniae':2},
    'Low blood pressure':       {'Acinetobacter baumannii':4,'Serratia marcescens':4,'Pseudomonas aeruginosa':3},
    'Confusion/altered mental': {'Acinetobacter baumannii':3,'Klebsiella pneumoniae':2,'Serratia marcescens':2},
}


# ═══════════════════════════════════════════════════════════════════════
#  BACTERIA PREDICTION
# ═══════════════════════════════════════════════════════════════════════

def _build_species_context(symptoms, species):
    if not symptoms:
        return {
            "supporting_features": [],
            "counterpoints": [],
            "clinical_summary": "",
        }

    support = symptoms[:5]
    if len(symptoms) > 5:
        support = symptoms[:4] + [f"and {len(symptoms) - 4} more symptom(s)"]

    summary = f"Pattern is most consistent with {species} given {'; '.join(support)}."
    return {
        "supporting_features": support,
        "counterpoints": [],
        "clinical_summary": summary,
    }

def predict_species_rule_based(symptoms):
    """Original heuristic fallback — deterministic, instant, always works."""
    if not symptoms:
        return None
    scores = {sp: 0 for sp in SPECIES_LIST}
    for sym in symptoms:
        if sym in SYMPTOM_WEIGHTS:
            for sp, w in SYMPTOM_WEIGHTS[sym].items():
                if sp in scores:
                    scores[sp] += w
    total = sum(scores.values())
    if total == 0:
        return None
    probs = {sp: round(v / total * 100, 1) for sp, v in scores.items() if v > 0}
    top = max(scores, key=scores.get)
    ordered_probs = dict(sorted(probs.items(), key=lambda x: -x[1])[:5])
    result = {
        "species": top,
        "probabilities": ordered_probs,
        "reasoning": f"Most consistent with {top} based on the symptom cluster.",
        "supporting_features": symptoms[:5],
        "counterpoints": [],
        "clinical_summary": f"Rule-based fallback selected {top} from the symptom pattern.",
        "source": "rules",
    }
    result.update(_build_species_context(symptoms, top))
    return result


def predict_species_llm(symptoms, patient_info):
    """Use Groq LLM to predict bacterial species from symptoms + demographics."""
    # Cache check
    cache_key = json.dumps({"s": sorted(symptoms), "p": patient_info}, sort_keys=True)
    if cache_key in _species_cache:
        print("[LLM] Species cache hit")
        return _species_cache[cache_key]

    species_list_str = "\n".join(f"  {i+1}. {sp}" for i, sp in enumerate(SPECIES_LIST))

    messages = [
        {
            "role": "system",
            "content": (
                "You are an infectious-disease consultant helping a physician choose the most likely "
                "bacterial species from a constrained laboratory panel. Think like a clinician: weigh "
                "the symptom cluster, age, sex, diabetes, prior hospitalization, and recurrence; avoid "
                "overconfidence when the presentation is nonspecific; and prefer the most plausible organism "
                "from the available options.\n\n"
                "You MUST choose ONLY from the following bacterial species — these are the only organisms "
                "our lab can test for:\n"
                f"{species_list_str}\n\n"
                "Use common-sense clinical pattern recognition:\n"
                "- UTI symptoms favor Escherichia coli first, then Proteus mirabilis and Klebsiella pneumoniae\n"
                "- Respiratory disease can favor Klebsiella pneumoniae, Pseudomonas aeruginosa, or Acinetobacter baumannii\n"
                "- Wound, burn, or green exudative infection increases suspicion for Pseudomonas aeruginosa or Acinetobacter baumannii\n"
                "- GI symptoms favor Escherichia coli or Enterobacteria spp.\n"
                "- Sepsis physiology and prior hospitalization raise concern for Serratia marcescens, Acinetobacter baumannii, or Klebsiella pneumoniae\n"
                "- Diabetes, recurrent infections, and recent hospitalization should shift the differential toward more opportunistic or resistant organisms\n\n"
                "Return ONLY valid JSON with the exact schema below. No markdown, no prose outside JSON, and no emojis."
            )
        },
        {
            "role": "user",
            "content": json.dumps({
                "symptoms": symptoms,
                "other_symptoms_description": f"Additionally, the patient describes: {patient_info.get('other_symptoms', '')}" if patient_info.get("other_symptoms") else "None",
                "age": patient_info.get("age", "unknown"),
                "gender": patient_info.get("gender", "unknown"),
                "diabetes": patient_info.get("diabetes", "unknown"),
                "hypertension": patient_info.get("hypertension", "unknown"),
                "prior_hospitalization": patient_info.get("hospital_before", "unknown"),
                "infection_frequency": patient_info.get("infection_freq", "unknown"),
                "response_schema": {
                    "predicted_species": "exact species name from list",
                    "confidence": 0.85,
                    "top_3": [
                        {"species": "name", "probability": 0.60, "why": "short clinical reason"},
                        {"species": "name", "probability": 0.25, "why": "short clinical reason"},
                        {"species": "name", "probability": 0.15, "why": "short clinical reason"}
                    ],
                    "reasoning": "One concise sentence explaining the leading clinical pattern",
                    "supporting_features": ["short feature", "short feature"],
                    "counterpoints": ["brief caveat"],
                    "clinical_summary": "A two-clause clinician-facing summary"
                }
            }, indent=2)
        }
    ]

    result = call_groq(messages)
    if not result or "predicted_species" not in result:
        return None

    # Validate species is in our list
    pred = result["predicted_species"]
    if pred not in SPECIES_LIST:
        # Try fuzzy match
        for sp in SPECIES_LIST:
            if sp.lower() in pred.lower() or pred.lower() in sp.lower():
                pred = sp
                break
        else:
            print(f"[LLM] Invalid species returned: {pred}")
            return None

    # Build probability dict
    probs = {}
    for item in result.get("top_3", []):
        sp_name = item.get("species", "")
        if sp_name in SPECIES_LIST:
            probs[sp_name] = round(float(item.get("probability", 0)) * 100, 1)

    if pred not in probs:
        probs[pred] = round(float(result.get("confidence", 0.8)) * 100, 1)

    ordered_probs = dict(sorted(probs.items(), key=lambda x: -x[1]))
    supporting_features = result.get("supporting_features", [])
    counterpoints = result.get("counterpoints", [])
    clinical_summary = result.get("clinical_summary", "")
    if not clinical_summary:
        clinical_summary = result.get("reasoning", "")

    output = {
        "species": pred,
        "probabilities": ordered_probs,
        "reasoning": result.get("reasoning", ""),
        "supporting_features": supporting_features,
        "counterpoints": counterpoints,
        "clinical_summary": clinical_summary,
        "confidence": float(result.get("confidence", 0.8)),
        "source": "llm",
    }
    _species_cache[cache_key] = output
    return output


def predict_species_from_symptoms(symptoms, patient_info=None):
    """Try LLM first, fall back to rule-based weights if it fails."""
    if patient_info is None:
        patient_info = {}

    # Try LLM
    llm_result = predict_species_llm(symptoms, patient_info)
    if llm_result:
        print(f"[LLM] Species predicted: {llm_result['species']} (structured clinical summary)")
        return llm_result

    # Fallback
    print("[LLM] Falling back to rule-based species prediction")
    return predict_species_rule_based(symptoms)


# ═══════════════════════════════════════════════════════════════════════
#  CARD CLINICAL INSIGHTS (runtime LLM formatting)
# ═══════════════════════════════════════════════════════════════════════

def format_card_insights_llm(species, top3_results, card_species_data):
    """Use Groq LLM to format raw CARD data into clinical insight cards."""
    ab_codes = tuple(ab['code'] for ab in top3_results)
    cache_key = f"{species}:{ab_codes}"
    if cache_key in _insights_cache:
        print("[LLM] CARD insights cache hit")
        return _insights_cache[cache_key]

    # Build CARD context for prompt
    card_sections = []
    for ab in top3_results:
        ab_card = card_species_data.get(ab['code'], {})
        genes = ab_card.get('key_genes', [])
        count = ab_card.get('all_genes_count', 0)
        families = ab_card.get('gene_families', [])
        mechs = ab_card.get('mechanisms', [])

        section = f"Antibiotic: {ab['name']} (code: {ab['code']}, class: {ab['drug_class']})\n"
        section += f"  Susceptibility: {ab['susceptible_pct']}% S, {ab['resistant_pct']}% R\n"
        if genes:
            section += f"  CARD resistance genes: {', '.join(genes)}\n"
            section += f"  Total genes cataloged: {count}\n"
            section += f"  Gene families: {', '.join(families)}\n"
            section += f"  Mechanisms: {', '.join(mechs)}\n"
        else:
            section += "  No resistance genes cataloged in CARD for this combination.\n"
        card_sections.append(section)

    card_context = "\n".join(card_sections)

    messages = [
        {
            "role": "system",
            "content": (
                "You are a clinical microbiologist writing a rapid bedside decision aid for a physician. "
                "Make the output clinically useful in under 10 seconds: explain the dominant resistance mechanism, "
                "name the most relevant marker genes, and state the practical prescribing implication. Write like a "
                "lab consult, not a textbook.\n\n"
                "For EACH antibiotic provided, generate a structured clinical insight with exactly these fields:\n"
                "- mechanism_summary: One clear sentence describing the primary resistance mechanism for this antibiotic. "
                "Keep it specific, concrete, and clinically framed.\n"
                "- key_markers: Array of 2-4 objects, each with \"gene\" and \"role\". Choose the genes most likely to matter "
                "to a prescribing clinician and keep the role text to ten words or fewer.\n"
                "- clinical_implication: One actionable sentence for the prescriber. State whether the drug should be avoided, "
                "used cautiously, supported by combination therapy, or confirmed with AST.\n\n"
                "If no CARD genes are found for a combination, infer the most likely mechanism from the organism and drug class, "
                "and prefix mechanism_summary with [Inferred].\n\n"
                "Respond with ONLY valid JSON, no markdown, no explanation outside JSON, and no emojis."
            )
        },
        {
            "role": "user",
            "content": (
                f"Pathogen: {species}\n\n"
                f"CARD Resistance Data for top recommended antibiotics:\n{card_context}\n\n"
                "Respond with this JSON schema:\n"
                "{\n"
                '  "insights": {\n'
                '    "ANTIBIOTIC_CODE": {\n'
                '      "mechanism_summary": "...",\n'
                '      "key_markers": [{"gene": "...", "role": "..."}, ...],\n'
                '      "clinical_implication": "..."\n'
                "    }\n"
                "  }\n"
                "}"
            )
        }
    ]

    result = call_groq(messages)
    if result and "insights" in result:
        _insights_cache[cache_key] = result["insights"]
        return result["insights"]

    return None


# ═══════════════════════════════════════════════════════════════════════
#  RED FLAG WARNINGS — Contraindication Evaluation
# ═══════════════════════════════════════════════════════════════════════

def evaluate_contraindications(ab_results, patient_info):
    """Cross-reference each antibiotic against the patient profile to find
    matching contraindication/red-flag rules. Returns a dict keyed by
    antibiotic code, each containing a list of triggered warnings."""
    if not CONTRAINDICATIONS:
        return {}

    age_raw = patient_info.get('age')
    try:
        age = float(age_raw) if age_raw else None
    except (ValueError, TypeError):
        age = None

    gender = patient_info.get('gender', '') or ''
    diabetes = patient_info.get('diabetes', '') or ''
    hypertension = patient_info.get('hypertension', '') or ''
    hospital_before = patient_info.get('hospital_before', '') or ''

    warnings_map = {}

    for ab in ab_results:
        code = ab['code']
        rules = CONTRAINDICATIONS.get(code, {}).get('warnings', [])
        matched = []

        for rule in rules:
            field = rule.get('field', '')
            triggered = False

            if field == '_always':
                triggered = True
            elif field == 'age' and age is not None:
                op = rule.get('operator', '==')
                threshold = float(rule.get('value', 0))
                if op == '>=' and age >= threshold:
                    triggered = True
                elif op == '<=' and age <= threshold:
                    triggered = True
                elif op == '>' and age > threshold:
                    triggered = True
                elif op == '<' and age < threshold:
                    triggered = True
                elif op == '==' and age == threshold:
                    triggered = True
            elif field == 'diabetes' and diabetes == rule.get('value', ''):
                triggered = True
            elif field == 'hypertension' and hypertension == rule.get('value', ''):
                triggered = True
            elif field == 'hospital_before' and hospital_before == rule.get('value', ''):
                triggered = True
            elif field == 'gender' and gender == rule.get('value', ''):
                triggered = True

            if triggered:
                matched.append({
                    'severity': rule.get('severity', 'low'),
                    'message': rule.get('message', ''),
                    'condition': rule.get('condition', ''),
                    'source': rule.get('source', ''),
                })

        if matched:
            warnings_map[code] = matched

    return warnings_map


# ═══════════════════════════════════════════════════════════════════════
#  ANTIBIOTIC PREDICTION (unchanged — 15 RF models)
# ═══════════════════════════════════════════════════════════════════════

def predict_antibiotics(age, gender, diabetes, hypertension, hospital_before, infection_freq, species):
    inf_cat = FREQ_MAP.get(infection_freq, None)

    input_df = pd.DataFrame([{
        'age': float(age) if age else np.nan,
        'gender': gender or None,
        'Diabetes': diabetes or None,
        'Hypertension': hypertension or None,
        'Hospital_before': hospital_before or None,
        'Infection_Freq_cat': inf_cat,
        'species': species
    }])

    results = []
    for ab in AB_COLS:
        if ab not in MODELS:
            continue
        
        # FREE TIER FIX: MODELS[ab] is now a file path, not an unpickled object in RAM.
        # We load one, predict, and immediately delete it to stay under 512MB RAM.
        model_path = MODELS[ab]
        try:
            with open(model_path, 'rb') as f:
                m = pickle.load(f)
            pipe, le = m['pipeline'], m['le']

            proba = pipe.predict_proba(input_df)[0]
            classes = le.classes_
            prob_dict = {cls: round(float(p) * 100, 1) for cls, p in zip(classes, proba)}
            s_prob = prob_dict.get('Susceptible', 0)
            r_prob = prob_dict.get('Resistant', 0)
            i_prob = prob_dict.get('Intermediate', 0)
            results.append({
                'code': ab,
                'name': META['ab_full_names'][ab],
                'drug_class': META['ab_classes'][ab],
                'susceptible_pct': s_prob,
                'resistant_pct': r_prob,
                'intermediate_pct': i_prob,
                'model_accuracy': META['model_meta'][ab]['accuracy'],
                'status': 'Susceptible' if s_prob >= 60 else ('Resistant' if r_prob >= 60 else 'Intermediate')
            })
            
            # Immediately free RAM before loading the next 30MB model
            del m, pipe, le
            gc.collect()

        except Exception as e:
            print(f"Prediction error for {ab}: {e}")
            traceback.print_exc()
            continue

    results.sort(key=lambda x: -x['susceptible_pct'])
    return results


# ═══════════════════════════════════════════════════════════════════════
#  ROUTES
# ═══════════════════════════════════════════════════════════════════════

@app.route('/')
def index():
    return render_template('index.html',
        species_list=SPECIES_LIST,
        symptoms=list(SYMPTOM_WEIGHTS.keys()),
        ab_cols=AB_COLS,
        ab_full_names=META['ab_full_names'])


@app.route('/predict', methods=['POST'])
def predict():
    data = request.json
    symptoms = data.get('symptoms', [])
    species = data.get('species', '')

    # Collect patient info for LLM
    patient_info = {
        'age': data.get('age'),
        'gender': data.get('gender'),
        'diabetes': data.get('diabetes'),
        'hypertension': data.get('hypertension'),
        'hospital_before': data.get('hospital_before'),
        'infection_freq': data.get('infection_freq'),
        'other_symptoms': data.get('otherSymptoms', ''),
    }

    reasoning = ""
    species_context = {
        'supporting_features': [],
        'counterpoints': [],
        'clinical_summary': '',
        'source': 'manual',
    }

    if symptoms and not species:
        species_result = predict_species_from_symptoms(symptoms, patient_info)
        if not species_result:
            return jsonify({'error': 'Could not determine bacterial species from the provided symptoms. Please add more symptoms or choose a species manually.'}), 400
        species = species_result['species']
        species_probs = species_result['probabilities']
        reasoning = species_result['reasoning']
        species_context = {
            'supporting_features': species_result.get('supporting_features', []),
            'counterpoints': species_result.get('counterpoints', []),
            'clinical_summary': species_result.get('clinical_summary', ''),
            'source': species_result.get('source', 'llm'),
        }
    else:
        species_probs = {species: 100.0} if species else {}
        if species:
            species_context = {
                'supporting_features': symptoms[:5],
                'counterpoints': [],
                'clinical_summary': f'Manual species selection: {species}.',
                'source': 'manual',
            }

    if not species:
        return jsonify({'error': 'Could not determine bacterial species. Please select more symptoms or choose manually.'}), 400

    # Run 15 RF models
    age = data.get('age') or None
    gender = data.get('gender') or None
    diabetes = data.get('diabetes') or None
    hypertension = data.get('hypertension') or None
    hospital = data.get('hospital_before') or None
    inf_freq = data.get('infection_freq') or None

    ab_results = predict_antibiotics(age, gender, diabetes, hypertension, hospital, inf_freq, species)

    if not ab_results:
        return jsonify({'error': 'Prediction failed for all antibiotics. Check server logs.'}), 500

    top3 = ab_results[:3]
    mdr = sum(1 for r in ab_results if r['resistant_pct'] >= 60) >= 3

    fi_data = META['feature_importances']
    top_ab_code = ab_results[0]['code']
    res_stats = META['resistance_stats'].get(species, {})

    # CARD insights — try LLM formatting, fall back to raw data
    card_species_data = CARD.get(species, {})
    card_insights = None
    try:
        card_insights = format_card_insights_llm(species, top3, card_species_data)
    except Exception as e:
        print(f"[LLM] CARD formatting error: {e}")
        traceback.print_exc()

    # ── Red-flag warnings ──
    warnings_map = evaluate_contraindications(ab_results, patient_info)

    # ── Clinician trust from feedback memory ──
    trust_map = get_clinician_trust(species, ab_results)

    return jsonify({
        'species': species,
        'species_probabilities': species_probs,
        'species_reasoning': reasoning,
        'species_clinical_context': species_context,
        'top3': top3,
        'all_results': ab_results,
        'mdr_flag': mdr,
        'feature_importances': fi_data,
        'top_ab_code': top_ab_code,
        'resistance_stats': res_stats,
        'ab_full_names': META['ab_full_names'],
        'card_ontology': card_species_data,
        'card_insights': card_insights,
        'contraindication_warnings': warnings_map,
        'clinician_trust': trust_map,
    })


# ═══════════════════════════════════════════════════════════════════════
#  DOCTOR FEEDBACK MEMORY
# ═══════════════════════════════════════════════════════════════════════

@app.route('/feedback', methods=['POST'])
def submit_feedback():
    """Record a doctor's positive/negative feedback for a species-antibiotic pair."""
    data = request.json or {}
    species = data.get('species', '').strip()
    ab_code = data.get('antibiotic_code', '').strip()
    vote = data.get('vote', '').strip().lower()

    if not species or not ab_code or vote not in ('positive', 'negative'):
        return jsonify({'error': 'Missing or invalid fields: species, antibiotic_code, vote (positive/negative)'}), 400

    feedback = _load_feedback()

    if species not in feedback:
        feedback[species] = {}
    if ab_code not in feedback[species]:
        feedback[species][ab_code] = {'positive': 0, 'negative': 0, 'trust': None}

    feedback[species][ab_code][vote] += 1

    pos = feedback[species][ab_code]['positive']
    neg = feedback[species][ab_code]['negative']
    total = pos + neg
    feedback[species][ab_code]['trust'] = round(pos / total, 2) if total > 0 else None

    _save_feedback(feedback)

    print(f"[FEEDBACK] {species} / {ab_code}: {vote} (total: {total}, trust: {feedback[species][ab_code]['trust']})")

    return jsonify({
        'ok': True,
        'species': species,
        'antibiotic_code': ab_code,
        'positive': pos,
        'negative': neg,
        'trust': feedback[species][ab_code]['trust'],
    })


@app.route('/feedback/stats')
def feedback_stats():
    """Return the full feedback memory (for debugging / dashboard)."""
    return jsonify(_load_feedback())


# ═══════════════════════════════════════════════════════════════════════
#  PATIENT HISTORY API
# ═══════════════════════════════════════════════════════════════════════

@app.route('/patient/new', methods=['POST'])
def new_patient():
    """Create a new patient record and return the generated PID."""
    data = request.json or {}
    try:
        pid = patient_db.create_patient(
            name=data.get('name', ''),
            age=int(data['age']) if data.get('age') else None,
            gender=data.get('gender', ''),
            diabetes=data.get('diabetes', ''),
            hypertension=data.get('hypertension', ''),
            hospital_before=data.get('hospital_before', ''),
            infection_freq=data.get('infection_freq', ''),
        )
        return jsonify({'patient_id': pid, 'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/patient/search')
def search_patient():
    """Search patients by PID prefix or name. ?q=030426"""
    q = request.args.get('q', '').strip()
    if not q:
        return jsonify({'results': []})
    results = patient_db.search_patients(q)
    return jsonify({'results': results})


@app.route('/patient/<pid>')
def get_patient(pid):
    """Get patient demographics + visit history."""
    patient = patient_db.get_patient(pid)
    if not patient:
        return jsonify({'error': 'Patient not found'}), 404
    visits = patient_db.get_visits(pid)
    # Don't send full_response in the list view (too large)
    visits_summary = []
    for v in visits:
        vs = dict(v)
        vs.pop('full_response', None)
        visits_summary.append(vs)
    return jsonify({'patient': patient, 'visits': visits_summary})


@app.route('/patient/<pid>/visit', methods=['POST'])
def save_patient_visit(pid):
    """Save a prediction result as a visit under a patient."""
    patient = patient_db.get_patient(pid)
    if not patient:
        return jsonify({'error': 'Patient not found'}), 404
    data = request.json or {}
    try:
        visit_id = patient_db.save_visit(
            patient_id=pid,
            symptoms=data.get('symptoms', []),
            other_symptoms=data.get('other_symptoms', ''),
            mode=data.get('mode', 'symptom'),
            predicted_species=data.get('predicted_species', ''),
            species_confidence=data.get('species_confidence'),
            top3=data.get('top3', []),
            mdr=data.get('mdr', False),
            warnings=data.get('warnings', {}),
            full_response=data.get('full_response', {}),
        )
        return jsonify({'visit_id': visit_id, 'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/patient/visit/<int:visit_id>')
def get_visit_detail(visit_id):
    """Get full details of a specific visit (including full_response)."""
    visit = patient_db.get_visit_by_id(visit_id)
    if not visit:
        return jsonify({'error': 'Visit not found'}), 404
    return jsonify({'visit': visit})


@app.route('/heatmap_data')
def heatmap_data():
    return jsonify({
        'resistance_stats': META['resistance_stats'],
        'species_list': SPECIES_LIST,
        'ab_cols': AB_COLS,
        'ab_full_names': META['ab_full_names']
    })


if __name__ == '__main__':
    patient_db.init_db()
    app.run(debug=True, port=5000)
