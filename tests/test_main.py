from fastapi.testclient import TestClient
from app.main import app


def test_demo_page_mounted():
    client = TestClient(app)
    response = client.get("/demo/")
    assert response.status_code == 200
    assert "PEAR Logger Service" in response.text


def test_demo_page_loads_all_service_logs():
    client = TestClient(app)
    response = client.get("/demo/")
    for endpoint, table_id in [
        ("/api/Logs/Patient", "patient-table"),
        ("/api/Logs/Activity", "activity-table"),
        ("/api/Logs/User", "user-table"),
    ]:
        assert endpoint in response.text
        assert f'id="{table_id}"' in response.text
