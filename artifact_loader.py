"""Shared loader for AntibioSense serialized artifacts."""

from __future__ import annotations

import json
import pickle
from pathlib import Path
from typing import Any


LEGACY_MODELS_FILENAME = "models.pkl"
MODEL_FILES_KEY = "model_files"


def _read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _read_pickle(path: Path) -> Any:
    with path.open("rb") as handle:
        return pickle.load(handle)


def _load_split_models(model_dir: Path, meta: dict[str, Any]) -> dict[str, Any]:
    model_files = meta.get(MODEL_FILES_KEY)
    if not isinstance(model_files, dict) or not model_files:
        raise ValueError("Split model manifest is missing from meta.json.")

    expected_codes = meta.get("ab_cols")
    if not isinstance(expected_codes, list) or not expected_codes:
        raise ValueError("meta.json must define ab_cols before split models can be loaded.")

    missing_manifest_entries = [code for code in expected_codes if code not in model_files]
    unexpected_manifest_entries = [code for code in model_files if code not in expected_codes]
    if missing_manifest_entries or unexpected_manifest_entries:
        details = []
        if missing_manifest_entries:
            details.append("missing entries: " + ", ".join(missing_manifest_entries))
        if unexpected_manifest_entries:
            details.append("unexpected entries: " + ", ".join(unexpected_manifest_entries))
        raise FileNotFoundError("Split model manifest is inconsistent (" + "; ".join(details) + ").")

    loaded_models: dict[str, Any] = {}
    missing_files: list[str] = []
    for code in expected_codes:
        relative_path = model_files[code]
        file_path = model_dir / relative_path
        if not file_path.is_file():
            missing_files.append(f"{code} -> {relative_path}")
            continue
        loaded_models[code] = _read_pickle(file_path)

    if missing_files:
        raise FileNotFoundError(
            "Split model artifacts are missing: " + ", ".join(missing_files)
        )

    return loaded_models



def load_artifact_bundle(base_dir: str | Path, allow_legacy: bool = True) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Load meta.json, model artifacts, and CARD metadata from a workspace base dir."""

    base_path = Path(base_dir)
    model_dir = base_path / "models"

    meta_path = model_dir / "meta.json"
    card_path = model_dir / "card_ontology.json"
    legacy_models_path = model_dir / LEGACY_MODELS_FILENAME

    meta = _read_json(meta_path)
    card = _read_json(card_path)

    if meta.get(MODEL_FILES_KEY):
        models = _load_split_models(model_dir, meta)
        return meta, models, card

    if allow_legacy and legacy_models_path.is_file():
        return meta, _read_pickle(legacy_models_path), card

    raise FileNotFoundError(
        "No loadable model artifacts found in "
        f"{model_dir}. Expected split files referenced by meta.json or {LEGACY_MODELS_FILENAME}."
    )