from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def test_root_still_responds():
    response = client.get("/")
    assert response.status_code == 200
    assert response.json() == {"message": "Welcome to PEAR Logging Service!"}


def test_demo_page_is_retired():
    assert client.get("/demo/").status_code == 404
