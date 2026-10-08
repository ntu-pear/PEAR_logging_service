from typing import Any, Dict, List

CLINICAL_FIELDS: Dict[str, Any] = {
    "message": "",
    "original_data": None,
    "updated_data": None,
    "patient_full_name": None,
}


def redact_clinical(rows: List[Any], role: str) -> List[Any]:
    if role != "ADMIN":
        return rows
    redacted = []
    for row in rows:
        if hasattr(row, "model_copy"):
            redacted.append(row.model_copy(update=CLINICAL_FIELDS))
        else:
            redacted.append({**row, **CLINICAL_FIELDS})
    return redacted
