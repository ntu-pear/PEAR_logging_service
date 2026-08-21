from fastapi.testclient import TestClient
from app.main import app


def test_demo_page_mounted():
    client = TestClient(app)
    response = client.get("/demo/")
    assert response.status_code == 200
    assert "PEAR Logger Service" in response.text
