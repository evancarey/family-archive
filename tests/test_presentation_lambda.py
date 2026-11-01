import importlib.util
from pathlib import Path

HANDLER_PATH = (
    Path(__file__).parents[1] / "infra" / "presentation" / "lambda" / "handler.py"
)
SPEC = importlib.util.spec_from_file_location("presentation_handler", HANDLER_PATH)
assert SPEC and SPEC.loader
handler = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(handler)


def test_health_handler_requires_jwt_authorizer_claims():
    response = handler.lambda_handler(
        {
            "routeKey": "GET /api/health",
            "requestContext": {"authorizer": {"jwt": {"claims": {"sub": "family-user"}}}},
        },
        None,
    )

    assert response["statusCode"] == 200
    assert response["headers"]["cache-control"] == "private, no-store"
    assert response["body"] == '{"status": "ready"}'


def test_health_handler_rejects_missing_authorizer_claims():
    response = handler.lambda_handler({"requestContext": {}}, None)

    assert response["statusCode"] == 401
    assert response["body"] == '{"error": "Sign-in required"}'
