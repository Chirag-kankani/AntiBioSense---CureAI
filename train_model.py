"""
train_model.py — PROPERLY preprocessed version.
Fixes applied vs v1:
  1. age=0 treated as invalid (→ NaN, imputed with median)  
  2. Infection_Freq mapped to ordered categories Never/Rarely/Regularly/Often
  3. OrdinalEncoder with meaningful category ordering for all categoricals
  4. Median imputation for age (robust to outliers)
  5. class_weight=balanced for imbalanced antibiotics
  6. F1-macro reported alongside accuracy (honest metric)

Usage: python train_model.py --data Bacteria_dataset_Multiresictance.csv
"""
import argparse, os, json, pickle, re
import pandas as pd, numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.pipeline import Pipeline
from sklearn.compose import ColumnTransformer
from sklearn.preprocessing import OrdinalEncoder, LabelEncoder
from sklearn.model_selection import train_test_split
from sklearn.impute import SimpleImputer
from sklearn.metrics import accuracy_score, f1_score
from sklearn.inspection import permutation_importance

AB_COLS = ['AMX/AMP','AMC','CZ','FOX','CTX/CRO','IPM','GEN','AN',
           'Acide nalidixique','ofx','CIP','C','Co-trimoxazole','Furanes','colistine']
AB_FULL_NAMES = {
    'AMX/AMP':'Amoxicillin/Ampicillin','AMC':'Amoxicillin-Clavulanate','CZ':'Cefazolin',
    'FOX':'Cefoxitin','CTX/CRO':'Cefotaxime/Ceftriaxone','IPM':'Imipenem',
    'GEN':'Gentamicin','AN':'Amikacin','Acide nalidixique':'Nalidixic Acid',
    'ofx':'Ofloxacin','CIP':'Ciprofloxacin','C':'Chloramphenicol',
    'Co-trimoxazole':'Co-trimoxazole','Furanes':'Nitrofurantoin','colistine':'Colistin'
}
AB_CLASSES_MAP = {
    'AMX/AMP':'Penicillin','AMC':'β-Lactam + Inhibitor','CZ':'Cephalosporin (1st gen)',
    'FOX':'Cephalosporin (2nd gen)','CTX/CRO':'Cephalosporin (3rd gen)','IPM':'Carbapenem',
    'GEN':'Aminoglycoside','AN':'Aminoglycoside','Acide nalidixique':'Quinolone',
    'ofx':'Fluoroquinolone','CIP':'Fluoroquinolone','C':'Phenicol',
    'Co-trimoxazole':'Sulfonamide','Furanes':'Nitrofuran','colistine':'Polymyxin'
}


def _safe_model_filename(code):
    safe_code = re.sub(r'[^A-Za-z0-9]+', '_', code).strip('_')
    return f'model_{safe_code}.pkl'

def extract_normalize_species(s):
    if pd.isna(s): return np.nan
    if str(s).strip() in ['?','missing']: return np.nan
    parts = str(s).split(' ', 1)
    if len(parts) < 2: return np.nan
    sp = parts[1].strip()
    for k, v in {
        'E. coli':'Escherichia coli','E.coli':'Escherichia coli','E.cli':'Escherichia coli',
        'E.coi':'Escherichia coli','Escherichia':'Escherichia coli',
        'Proeus mirabilis':'Proteus mirabilis','Prot.eus mirabilis':'Proteus mirabilis',
        'Protus mirabilis':'Proteus mirabilis','Proteus':'Proteus mirabilis',
        'Klbsiella':'Klebsiella pneumoniae','Klebsie.lla':'Klebsiella pneumoniae','Klebsiella':'Klebsiella pneumoniae',
        'Enter.bacteria':'Enterobacteria spp.','Enteobacteria':'Enterobacteria spp.','Enterobacteria':'Enterobacteria spp.',
        'Morganella':'Morganella morganii','Citrobacter':'Citrobacter spp.',
        'Pseudomonas':'Pseudomonas aeruginosa','Acinetobacter':'Acinetobacter baumannii',
        'Serratia':'Serratia marcescens',
    }.items():
        if sp.startswith(k) or sp == k: return v
    return sp

def clean_data(df):
    df = df.copy()
    # Species
    df['species'] = df['Souches'].apply(extract_normalize_species)
    # Age — FIX: age=0 → NaN
    df['age'] = df['age/gender'].apply(
        lambda x: (lambda a: float(a) if a >= 1 else np.nan)(int(str(x).split('/')[0]))
        if pd.notna(x) and '/' in str(x) and str(x).split('/')[0].isdigit() else np.nan)
    df['gender'] = df['age/gender'].apply(
        lambda x: str(x).split('/')[1].strip().upper() if pd.notna(x) and '/' in str(x) else np.nan)
    # Categorical fixes
    df['Diabetes'] = df['Diabetes'].replace({'True':'Yes','missing':np.nan,'?':np.nan})
    df['Hypertension'] = df['Hypertension'].replace({'missing':np.nan,'?':np.nan})
    df['Hospital_before'] = df['Hospital_before'].replace({'missing':np.nan,'?':np.nan})
    # Infection freq — FIX: proper ordered category
    df['Infection_Freq'] = pd.to_numeric(
        df['Infection_Freq'].replace({'missing':np.nan,'?':np.nan,'unknown':np.nan,'error':np.nan}),
        errors='coerce')
    df['Infection_Freq_cat'] = df['Infection_Freq'].map({0.0:'Never',1.0:'Rarely',2.0:'Regularly',3.0:'Often'})
    # Antibiotic normalisation
    ab_mapper = {'R':'Resistant','r':'Resistant','S':'Susceptible','s':'Susceptible',
                 'i':'Intermediate','Intermediate':'Intermediate','missing':np.nan,'?':np.nan}
    for col in AB_COLS:
        df[col] = df[col].map(lambda x: ab_mapper.get(str(x), np.nan) if pd.notna(x) else np.nan)
    return df[df['species'].notna()].copy()

def train(data_path, out_dir='models'):
    os.makedirs(out_dir, exist_ok=True)
    df = pd.read_csv(data_path)
    df_clean = clean_data(df)
    print(f"Clean rows: {len(df_clean)}")
    species_list = sorted(df_clean['species'].unique().tolist())
    
    FEATURE_COLS = ['age','gender','Diabetes','Hypertension','Hospital_before','Infection_Freq_cat','species']
    CAT_COLS = ['gender','Diabetes','Hypertension','Hospital_before','Infection_Freq_cat','species']
    NUM_COLS = ['age']
    X = df_clean[FEATURE_COLS].copy()
    
    # FIX: OrdinalEncoder with proper category order
    cat_pipe = Pipeline([
        ('imp', SimpleImputer(strategy='most_frequent')),
        ('enc', OrdinalEncoder(
            categories=[['F','M'],['No','Yes'],['No','Yes'],['No','Yes'],
                        ['Never','Rarely','Regularly','Often'], species_list],
            handle_unknown='use_encoded_value', unknown_value=-1))
    ])
    num_pipe = Pipeline([('imp', SimpleImputer(strategy='median'))])
    preprocessor = ColumnTransformer([('cat',cat_pipe,CAT_COLS),('num',num_pipe,NUM_COLS)])
    
    all_models, model_meta, all_fi = {}, {}, {}
    print(f"\n{'Antibiotic':35s} {'Accuracy':>9} {'F1-macro':>9}")
    print("-"*55)
    for ab in AB_COLS:
        y = df_clean[ab].dropna()
        X_ab = X.loc[y.index]
        le = LabelEncoder()
        y_enc = le.fit_transform(y)
        X_tr,X_te,y_tr,y_te = train_test_split(X_ab,y_enc,test_size=0.2,random_state=42,stratify=y_enc)
        pipe = Pipeline([('pre',preprocessor),
                         ('clf',RandomForestClassifier(n_estimators=200,random_state=42,
                                                        n_jobs=-1,class_weight='balanced',
                                                        max_depth=15,min_samples_leaf=3))])
        pipe.fit(X_tr,y_tr)
        y_pred = pipe.predict(X_te)
        acc = accuracy_score(y_te,y_pred)*100
        f1m = f1_score(y_te,y_pred,average='macro')*100
        all_models[ab] = {'pipeline':pipe,'le':le}
        model_meta[ab] = {'accuracy':round(acc,1),'f1_macro':round(f1m,1),
                           'full_name':AB_FULL_NAMES[ab],'drug_class':AB_CLASSES_MAP[ab],
                           'classes':le.classes_.tolist()}
        perm_imp = permutation_importance(pipe, X_te, y_te, n_repeats=5, random_state=42, n_jobs=-1)
        raw_fi = [max(0.0, float(v)) for v in perm_imp.importances_mean]
        fi_sum = sum(raw_fi)
        norm_fi = [v / fi_sum for v in raw_fi] if fi_sum > 0 else raw_fi
        fi = dict(zip(FEATURE_COLS, norm_fi))
        all_fi[ab] = {k:round(float(v),4) for k,v in sorted(fi.items(),key=lambda x:-x[1])}
        print(f"  {AB_FULL_NAMES[ab]:33s}  {acc:>8.1f}%  {f1m:>8.1f}%")
    
    resistance_stats = {}
    for sp in df_clean['species'].unique():
        sub = df_clean[df_clean['species']==sp]
        resistance_stats[sp] = {}
        for ab in AB_COLS:
            col = sub[ab].dropna()
            if len(col)>0:
                resistance_stats[sp][ab] = {
                    'R':round((col=='Resistant').sum()/len(col)*100,1),
                    'S':round((col=='Susceptible').sum()/len(col)*100,1),
                    'I':round((col=='Intermediate').sum()/len(col)*100,1),
                    'n':len(col)}
    
    model_files = {}
    for ab in AB_COLS:
        file_name = _safe_model_filename(ab)
        with open(os.path.join(out_dir, file_name), 'wb') as f:
            pickle.dump(all_models[ab], f, protocol=pickle.HIGHEST_PROTOCOL)
        model_files[ab] = file_name

    legacy_model_path = os.path.join(out_dir, 'models.pkl')
    if os.path.exists(legacy_model_path):
        os.remove(legacy_model_path)

    with open(os.path.join(out_dir,'meta.json'),'w') as f:
        json.dump({'model_meta':model_meta,'feature_importances':all_fi,
                   'resistance_stats':resistance_stats,'ab_full_names':AB_FULL_NAMES,
                   'ab_classes':AB_CLASSES_MAP,'species_list':species_list,'ab_cols':AB_COLS,
                   'model_artifact_format':'split-pickle-v1','model_files':model_files},f,indent=2)
    print(f"\nSaved to {out_dir}/")

if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--data', default='Bacteria_dataset_Multiresictance.csv')
    p.add_argument('--out', default='models')
    args = p.parse_args()
    train(args.data, args.out)
