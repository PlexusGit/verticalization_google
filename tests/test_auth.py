from types import SimpleNamespace

from app import auth


def test_api_key_authentication(monkeypatch):
    monkeypatch.setattr(auth.settings, "SECURITY_TOKEN", "expected")
    request = SimpleNamespace(session={}, headers={"X-API-Key": "expected"})
    assert auth.request_is_authenticated(request)
    request.headers["X-API-Key"] = "wrong"
    assert not auth.request_is_authenticated(request)


class Document:
    def __init__(self, exists): self.exists = exists


class DocumentRef:
    def __init__(self, exists): self.exists = exists
    def get(self): return Document(self.exists)


class Collection:
    def __init__(self, exists): self.exists = exists
    def document(self, _email): return DocumentRef(self.exists)
    def where(self, *_args): return self
    def limit(self, _limit): return self
    def stream(self): return []


class DB:
    def __init__(self, exists): self.exists = exists
    def collection(self, _name): return Collection(self.exists)


def test_allowlist_prefers_normalized_document_id():
    assert auth.is_user_allowed(DB(True), "  USER@Example.COM ")
    assert not auth.is_user_allowed(DB(False), "missing@example.com")
