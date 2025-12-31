import pytest
from unittest.mock import patch
from app.crud.logs_crud import get_logs_by_param_patient
from app.schemas.log_query import LogQuery

# Updated sample response to match the actual data structure your code expects
sample_es_response = {
    "hits": {
        "total": {"value": 3},
        "hits": [
            {
                "_source": {
                    "@timestamp": "2024-02-20T10:00:00",
                    "message": {
                        "timestamp": "2024-02-20T10:00:00",
                        "level": "INFO",
                        "logger": "app.logger",
                        "user": "admin",
                        "user_full_name": "Admin User",
                        "table": "Patient",
                        "action": "update",
                        "log_text": "Updated patient record",
                        "message": {
                            "original_data": {"id": 1, "fullName": "Old Name", "nric": "S1234567A"},
                            "updated_data": {"id": 1, "fullName": "New Name", "nric": "S1234567A"},
                            "entity_id": 1
                        }
                    }
                }
            },
            {
                "_source": {
                    "@timestamp": "2024-02-20T09:00:00",
                    "message": {
                        "timestamp": "2024-02-20T09:00:00",
                        "level": "INFO",
                        "logger": "app.logger",
                        "user": "admin",
                        "user_full_name": "Admin User",
                        "table": "DoctorNote",
                        "action": "create",
                        "log_text": "Created doctor note",
                        "message": {
                            "original_data": {},
                            "updated_data": {"id": 2, "patientId": 1, "doctor_id": 3, "remarks": "Patient is alive"},
                            "entity_id": 2
                        }
                    }
                }
            },
            {
                "_source": {
                    "@timestamp": "2025-02-18T15:16:09",
                    "message": {
                        "timestamp": "2025-02-18T15:16:09",
                        "level": "INFO",
                        "logger": "app.logger",
                        "user": "not_admin",
                        "user_full_name": "Not Admin User",
                        "table": "PatientAllergyMapping",
                        "action": "create",
                        "log_text": "Created allergy mapping",
                        "message": {
                            "original_data": {},
                            "updated_data": {
                                "AllergyRemarks": "Patient has severe reactions",
                                "IsDeleted": "0",
                                "PatientID": 2,
                                "AllergyTypeID": 3,
                                "AllergyReactionTypeID": 4
                            },
                            "entity_id": 5
                        }
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
    query = LogQuery()

    logs, total_records, total_pages = get_logs_by_param_patient(query=query)

    # Fixed: Changed "timestamp" to "@timestamp" to match actual code
    expected_query = {
        "query": {"match_all": {}},
        "size": 10,
        "from": 0,
        "sort": [{"@timestamp": {"order": "desc"}}],  # Changed from "timestamp"
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


def test_get_logs_with_patient_filter(mock_es_service):
    """Test getting logs filtered by patient ID"""
    query = LogQuery(patient="1")

    logs, total_records, total_pages = get_logs_by_param_patient(query=query)

    # Verify the query includes patient ID filter
    call_args = mock_es_service.search_documents.call_args
    query_body = call_args.kwargs['body']

    assert 'bool' in query_body['query']
    assert 'must' in query_body['query']['bool']

    # Check that patient filter is present
    has_patient_filter = any(
        'bool' in condition and 'should' in condition['bool']
        for condition in query_body['query']['bool']['must']
    )
    assert has_patient_filter

    assert len(logs) == 3
    assert total_records == 3


def test_get_logs_with_action_filter(mock_es_service):
    """Test getting logs filtered by action"""
    query = LogQuery(action="update")

    logs, total_records, total_pages = get_logs_by_param_patient(query=query)

    # Verify the query includes action filter
    call_args = mock_es_service.search_documents.call_args
    query_body = call_args.kwargs['body']

    assert 'bool' in query_body['query']
    assert 'must' in query_body['query']['bool']

    # Check that action filter is present
    has_action_filter = any(
        'match_phrase' in condition and 'message' in condition['match_phrase']
        for condition in query_body['query']['bool']['must']
    )
    assert has_action_filter

    assert len(logs) == 3


def test_get_logs_with_pagination(mock_es_service):
    """Test pagination parameters"""
    query = LogQuery()

    logs, total_records, total_pages = get_logs_by_param_patient(
        query=query,
        pageNo=1,
        pageSize=5
    )

    call_args = mock_es_service.search_documents.call_args
    query_body = call_args.kwargs['body']

    assert query_body['size'] == 5
    assert query_body['from'] == 5  # pageNo 1 * pageSize 5
    assert total_pages == 1  # 3 records / 5 per page = 1 page


def test_get_logs_with_date_range(mock_es_service):
    """Test date range filtering"""
    query = LogQuery(
        start_date="2024-02-20T00:00:00",
        end_date="2024-02-21T00:00:00"
    )

    logs, total_records, total_pages = get_logs_by_param_patient(query=query)

    call_args = mock_es_service.search_documents.call_args
    query_body = call_args.kwargs['body']

    # Check that date range filter is present
    has_date_filter = any(
        'range' in condition and '@timestamp' in condition['range']
        for condition in query_body['query']['bool']['must']
    )
    assert has_date_filter