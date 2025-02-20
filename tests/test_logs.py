import pytest
from unittest.mock import patch
from app.crud.logs_crud import get_logs_by_param

sample_es_response = {
    "hits": {
        "total": {"value": 3},
        "hits": [
            {
                "_source": {
                    "timestamp": "2024-02-20T10:00:00",
                    "action": "update",
                    "table": "Patient",
                    "user": "admin",
                    "message": {
                        "original_data": {"id": 1, "fullName": "Old Name", "nric": "S1234567A"},
                        "updated_data": {"id": 1, "fullName": "New Name", "nric": "S1234567A"}
                    }
                }
            },
            {
                "_source": {
                    "timestamp": "2024-02-20T09:00:00",
                    "action": "create",
                    "table": "DoctorNote",
                    "user": "admin",
                    "message": {
                        "original_data": None,
                        "updated_data": {"id": 2, "patient_id": "1", "doctor_id": "3", "remarks": "Patient is alive"}
                    }
                }
            },
            {
                "_source": {
                    "timestamp": "2025-02-18T15:16:09",
                    "action": "create",
                    "table": "PatientAllergyMapping",
                    "user": "not admin",
                    "message": {
                        "original_data": None,
                        "updated_data": {"AllergyRemarks": "Patient has severe reactions", "IsDeleted": "0", "PatientID": 2, "AllergyTypeID": 3, "AllergyReactionTypeID": 4}
                    }
                }
            }
        ]
    }
}

@pytest.fixture
def mock_es_service():
    with patch('app.crud.logs_crud.es_service', autospec=True) as mock:
        mock.search_documents.return_value = sample_es_response
        yield mock

def test_get_logs_no_params(mock_es_service):
    """Test getting logs without any filter parameters"""
    logs, total_records, total_pages = get_logs_by_param()
    
    expected_query = {
        "query": {"match_all": {}},
        "size": 10,
        "from": 0,
        "sort": [{"timestamp": {"order": "desc"}}],
        "track_total_hits": True,
    }
    
    mock_es_service.search_documents.assert_called_once_with(
        index="*",
        body=expected_query,
        headers={"Content-Type": "application/json"}
    )
    
    assert len(logs) == 3
    assert total_records == 3
    assert total_pages == 1

