from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from django.core.management import call_command

COMMITTED = Path(__file__).resolve().parent.parent / "openapi-schema.yaml"


def _generate(tmp_path: Path) -> str:
    out = tmp_path / "schema.yaml"
    call_command("spectacular", "--file", str(out), "--validate")
    return out.read_text(encoding="utf-8")


def _norm(text: str) -> str:
    return text.replace("\r\n", "\n")


def _ok_schema(schema: dict[str, Any], operation_id: str) -> dict[str, Any]:
    for path_item in schema["paths"].values():
        for op in path_item.values():
            if isinstance(op, dict) and op.get("operationId") == operation_id:
                body = op["responses"]["200"]["content"]["application/json"]
                ref = body["schema"]["$ref"].rsplit("/", 1)[-1]
                return schema["components"]["schemas"][ref]
    raise AssertionError(f"operation {operation_id} not found")


def test_committed_schema_is_current(tmp_path: Path) -> None:
    generated = _generate(tmp_path)
    assert COMMITTED.is_file(), "run: manage.py spectacular --file openapi-schema.yaml"
    assert _norm(COMMITTED.read_text(encoding="utf-8")) == _norm(generated), (
        "openapi-schema.yaml is stale: regenerate it with "
        "`python manage.py spectacular --file openapi-schema.yaml --validate`"
    )


def test_auth_operations_are_typed(tmp_path: Path) -> None:
    schema = yaml.safe_load(_generate(tmp_path))
    assert "mfa_token" in _ok_schema(schema, "auth_login")["properties"]
    verify = _ok_schema(schema, "auth_mfa_verify")["properties"]
    assert "access" in verify and "user" in verify
