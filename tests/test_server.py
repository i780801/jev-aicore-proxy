"""
Unit tests for the FastAPI proxy.  The TypeSafeClient is mocked throughout
so no real OpenRouter credits are spent.

Run unit tests:
    pytest tests/test_server.py -v -m "not integration"

Run integration tests (real OpenRouter calls, costs credits):
    OPENROUTER_API_KEY=sk-or-... pytest tests/test_server.py -m integration -v
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Helpers: build mock SDK return values using the real Pydantic models
# ---------------------------------------------------------------------------

def _make_mock_result():
    """Build a realistic SystemOneResponse using real SDK Pydantic types."""
    from typesafe_sdk import ChoiceAnswer, NoulAnswer, ScoreAnswer, SystemOneResponse, Usage

    return SystemOneResponse(
        model="jev-1.13",
        usage=Usage(input_tokens=42, output_tokens=None),
        answers={
            "is_bug": NoulAnswer(type="noul", noul=0.97),
            "team": ChoiceAnswer(
                type="choice",
                choice="frontend",
                confidence=0.91,
                probabilities={"payments": 0.05, "frontend": 0.91, "account": 0.04},
            ),
            "urgency": ScoreAnswer(
                type="score",
                score=1.7,
                confidence=0.85,
                legend={0: "Can wait", 1: "This week", 2: "Blocking revenue"},
                probabilities={0: 0.05, 1: 0.50, 2: 0.45},
            ),
        },
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def client_with_mock_sdk():
    """TestClient whose lifespan installs a mock SDK client instead of a real one."""
    mock_sdk = MagicMock()
    mock_sdk.system_one.return_value = _make_mock_result()

    # Patch _init_client so the lifespan receives the mock, not a real client.
    with patch("server._init_client", return_value=mock_sdk):
        import server
        with TestClient(server.app, raise_server_exceptions=False) as c:
            yield c, mock_sdk


@pytest.fixture()
def client_no_key():
    """TestClient whose lifespan raises RuntimeError (missing key)."""
    with patch("server._init_client", side_effect=RuntimeError("OPENROUTER_API_KEY")):
        import server
        # TestClient will raise during lifespan; we catch that.
        with pytest.raises(RuntimeError):
            with TestClient(server.app, raise_server_exceptions=True):
                pass


# ---------------------------------------------------------------------------
# Three-question reference payload (mirrors the working direct SDK example)
# ---------------------------------------------------------------------------

THREE_QUESTION_PAYLOAD = {
    "state": {
        "customer_tier": "enterprise",
        "ticket": "My checkout page shows a blank screen after I click Pay.",
    },
    "questions": {
        "is_bug": {
            "type": "noul",
            "instructions": "Is the customer reporting a software defect?",
            "criteria": {
                "true": "The customer describes broken or unexpected product behavior.",
                "false": "The customer is asking a question or requesting a feature.",
            },
        },
        "team": {
            "type": "choice",
            "instructions": "Which team should own this ticket?",
            "criteria": {
                "payments": "Checkout, billing, or payment processing issues.",
                "frontend": "Rendering, layout, or browser compatibility issues.",
                "account": "Login, permissions, or profile issues.",
            },
        },
        "urgency": {
            "type": "score",
            "instructions": "How urgent is this ticket?",
            "criteria": [
                "Can wait for the next release",
                "Should be fixed this week",
                "Blocking revenue right now",
            ],
        },
    },
}


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------

class TestHealth:
    def test_returns_200(self, client_with_mock_sdk):
        client, _ = client_with_mock_sdk
        resp = client.get("/v2/health")
        assert resp.status_code == 200

    def test_body(self, client_with_mock_sdk):
        client, _ = client_with_mock_sdk
        resp = client.get("/v2/health")
        assert resp.json() == {"status": "ok"}

    def test_does_not_call_sdk(self, client_with_mock_sdk):
        client, mock_sdk = client_with_mock_sdk
        client.get("/v2/health")
        mock_sdk.system_one.assert_not_called()


# ---------------------------------------------------------------------------
# /v1/systemone — happy path
# ---------------------------------------------------------------------------

class TestSystemOne:
    def test_happy_path_status(self, client_with_mock_sdk):
        client, _ = client_with_mock_sdk
        resp = client.post("/v1/systemone", json=THREE_QUESTION_PAYLOAD)
        assert resp.status_code == 200

    def test_happy_path_structure(self, client_with_mock_sdk):
        client, _ = client_with_mock_sdk
        body = client.post("/v1/systemone", json=THREE_QUESTION_PAYLOAD).json()
        assert "answers" in body
        assert "model" in body
        assert "usage" in body

    def test_answer_types_present(self, client_with_mock_sdk):
        client, _ = client_with_mock_sdk
        answers = client.post("/v1/systemone", json=THREE_QUESTION_PAYLOAD).json()["answers"]
        assert "is_bug" in answers
        assert "team" in answers
        assert "urgency" in answers

    def test_noul_answer_shape(self, client_with_mock_sdk):
        client, _ = client_with_mock_sdk
        answers = client.post("/v1/systemone", json=THREE_QUESTION_PAYLOAD).json()["answers"]
        assert answers["is_bug"]["type"] == "noul"
        assert "noul" in answers["is_bug"]

    def test_choice_answer_shape(self, client_with_mock_sdk):
        client, _ = client_with_mock_sdk
        answers = client.post("/v1/systemone", json=THREE_QUESTION_PAYLOAD).json()["answers"]
        assert answers["team"]["type"] == "choice"
        assert answers["team"]["choice"] == "frontend"
        assert "probabilities" in answers["team"]

    def test_score_answer_shape(self, client_with_mock_sdk):
        client, _ = client_with_mock_sdk
        answers = client.post("/v1/systemone", json=THREE_QUESTION_PAYLOAD).json()["answers"]
        assert answers["urgency"]["type"] == "score"
        assert "score" in answers["urgency"]
        assert "legend" in answers["urgency"]

    def test_sdk_called_with_correct_args(self, client_with_mock_sdk):
        client, mock_sdk = client_with_mock_sdk
        client.post("/v1/systemone", json=THREE_QUESTION_PAYLOAD)
        mock_sdk.system_one.assert_called_once_with(
            model="jev-1.13",
            state=THREE_QUESTION_PAYLOAD["state"],
            questions=THREE_QUESTION_PAYLOAD["questions"],
        )

    def test_usage_returned(self, client_with_mock_sdk):
        client, _ = client_with_mock_sdk
        body = client.post("/v1/systemone", json=THREE_QUESTION_PAYLOAD).json()
        assert body["usage"]["input_tokens"] == 42

    def test_model_field_returned(self, client_with_mock_sdk):
        client, _ = client_with_mock_sdk
        body = client.post("/v1/systemone", json=THREE_QUESTION_PAYLOAD).json()
        assert body["model"] == "jev-1.13"


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------

class TestValidation:
    def test_missing_state(self, client_with_mock_sdk):
        client, _ = client_with_mock_sdk
        resp = client.post("/v1/systemone", json={"questions": {}})
        assert resp.status_code == 422

    def test_missing_questions(self, client_with_mock_sdk):
        client, _ = client_with_mock_sdk
        resp = client.post("/v1/systemone", json={"state": {}})
        assert resp.status_code == 422

    def test_empty_body(self, client_with_mock_sdk):
        client, _ = client_with_mock_sdk
        resp = client.post("/v1/systemone", json={})
        assert resp.status_code == 422

    def test_non_dict_state(self, client_with_mock_sdk):
        client, _ = client_with_mock_sdk
        resp = client.post("/v1/systemone", json={"state": "bad", "questions": {}})
        assert resp.status_code == 422

    def test_api_key_not_in_validation_error(self, client_with_mock_sdk):
        client, _ = client_with_mock_sdk
        resp = client.post("/v1/systemone", json={})
        body_text = resp.text
        assert "sk-or" not in body_text
        assert "OPENROUTER" not in body_text


# ---------------------------------------------------------------------------
# Upstream failure handling
# ---------------------------------------------------------------------------

class TestUpstreamFailure:
    def test_sdk_exception_returns_502(self, client_with_mock_sdk):
        client, mock_sdk = client_with_mock_sdk
        mock_sdk.system_one.side_effect = RuntimeError("upstream exploded")
        resp = client.post("/v1/systemone", json=THREE_QUESTION_PAYLOAD)
        assert resp.status_code == 502

    def test_sdk_exception_body_safe(self, client_with_mock_sdk):
        client, mock_sdk = client_with_mock_sdk
        mock_sdk.system_one.side_effect = RuntimeError("secret_key_abc123")
        resp = client.post("/v1/systemone", json=THREE_QUESTION_PAYLOAD)
        assert "secret_key_abc123" not in resp.text

    def test_client_unavailable_returns_503(self):
        """When _jev_client is None at request time, return 503."""
        mock_sdk = MagicMock()
        mock_sdk.system_one.return_value = _make_mock_result()
        with patch("server._init_client", return_value=mock_sdk):
            import server
            with TestClient(server.app, raise_server_exceptions=False) as client:
                # Simulate post-startup client teardown
                import server as srv
                srv._jev_client = None
                resp = client.post("/v1/systemone", json=THREE_QUESTION_PAYLOAD)
                assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Startup: missing API key
# ---------------------------------------------------------------------------

class TestStartup:
    def test_missing_key_raises_at_startup(self):
        """_init_client() must raise RuntimeError when no key is set."""
        import server
        original = os.environ.pop("OPENROUTER_API_KEY", None)
        try:
            with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
                server._init_client()
        finally:
            if original is not None:
                os.environ["OPENROUTER_API_KEY"] = original

    def test_key_not_in_logs(self, caplog):
        """The real key must never appear in log output."""
        import logging
        import server
        mock_sdk = MagicMock()
        mock_sdk.system_one.return_value = _make_mock_result()
        with patch("server._init_client", return_value=mock_sdk):
            with caplog.at_level(logging.DEBUG):
                with TestClient(server.app, raise_server_exceptions=False) as c:
                    c.get("/v2/health")
        assert "sk-or" not in caplog.text
        assert "OPENROUTER_API_KEY" not in caplog.text


# ---------------------------------------------------------------------------
# Integration test (real inference — requires OPENROUTER_API_KEY)
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestIntegration:
    """
    These tests hit real OpenRouter and spend credits.
    Only run when OPENROUTER_API_KEY is set and -m integration is passed.

        OPENROUTER_API_KEY=sk-or-... pytest tests/test_server.py -m integration -v
    """

    def test_real_health(self):
        if not os.environ.get("OPENROUTER_API_KEY", ""):
            pytest.skip("OPENROUTER_API_KEY not set")
        import server
        with TestClient(server.app) as c:
            resp = c.get("/v2/health")
            assert resp.status_code == 200

    def test_real_inference(self):
        if not os.environ.get("OPENROUTER_API_KEY", ""):
            pytest.skip("OPENROUTER_API_KEY not set")
        import server
        with TestClient(server.app) as c:
            resp = c.post("/v1/systemone", json=THREE_QUESTION_PAYLOAD)
            assert resp.status_code == 200
            body = resp.json()
            assert "answers" in body
            answers = body["answers"]
            assert "is_bug" in answers
            assert "team" in answers
            assert "urgency" in answers
            # Verify answer shapes from real Jev response
            assert answers["is_bug"]["type"] == "noul"
            assert answers["team"]["type"] == "choice"
            assert answers["urgency"]["type"] == "score"
