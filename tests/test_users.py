from fastapi.testclient import TestClient

from app.main import app
from app.routers import api, views


client = TestClient(app)


class Snapshot:
    def __init__(self, exists=False, data=None, document_id=""):
        self.exists = exists
        self._data = data or {}
        self.id = document_id

    def to_dict(self):
        return self._data


class Reference:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.written = None

    def get(self):
        return self.snapshot

    def set(self, data):
        self.written = data


class Collection:
    def __init__(self, documents=None):
        self.documents = documents or []
        self.references = {}

    def stream(self):
        return self.documents

    def document(self, document_id):
        return self.references.setdefault(document_id, Reference(Snapshot()))


class DB:
    def __init__(self, collection):
        self.value = collection

    def collection(self, _name):
        return self.value


def test_user_management_api_rejects_non_admin(monkeypatch):
    monkeypatch.setattr(api, "get_current_user", lambda _request: {"id": "user", "info": {"role": "user"}})
    monkeypatch.setattr(api, "request_is_admin", lambda _request: False)
    response = client.get("/api/users")
    assert response.status_code == 403
    assert response.json()["error"] == "Acesso restrito a administradores"


def test_admin_can_list_and_create_normalized_user(monkeypatch):
    collection = Collection([Snapshot(True, {"email": "admin@example.com"}, "admin@example.com")])
    monkeypatch.setattr(api, "get_db", lambda: DB(collection))
    monkeypatch.setattr(api, "get_current_user", lambda _request: {"id": "admin", "info": {"email": "admin@example.com", "role": "admin"}})
    monkeypatch.setattr(api, "request_is_admin", lambda _request: True)

    listed = client.get("/api/users")
    created = client.post("/api/users", json={"email": "  Person@Example.com ", "role": "user"})

    assert listed.status_code == 200
    assert listed.json()["users"][0]["role"] == "admin"
    assert created.status_code == 201
    assert created.json() == {"email": "person@example.com", "role": "user"}
    assert collection.references["person@example.com"].written["created_by"] == "admin@example.com"


def test_users_page_is_admin_only(monkeypatch):
    user = {"id": "user", "info": {"email": "user@example.com", "role": "user"}}
    monkeypatch.setattr(views, "get_current_user", lambda _request: user)
    monkeypatch.setattr(views, "request_is_admin", lambda _request: False)
    denied = client.get("/users", follow_redirects=False)

    user["info"]["role"] = "admin"
    monkeypatch.setattr(views, "request_is_admin", lambda _request: True)
    allowed = client.get("/users")

    assert denied.status_code == 303
    assert denied.headers["location"] == "/dashboard"
    assert allowed.status_code == 200
    assert "Adicionar usuário" in allowed.text
    assert 'href="/users"' in allowed.text
