import logging
from typing import Any, Dict, List, Optional
import jsonschema

from app.tools import ToolResult

logger = logging.getLogger("schema_validator")


class SchemaValidatorTool:
    """Tool for validating HTTP response bodies against OpenAPI JSON Schemas."""

    @staticmethod
    def validate(
        response_body: Any,
        response_schema: Optional[Dict[str, Any]]
    ) -> ToolResult:
        """Validate response_body against the provided JSON schema.

        If response_schema is None or empty, schema validation passes automatically.
        """
        if not response_schema or not isinstance(response_schema, dict):
            return ToolResult(
                success=True,
                data={"valid": True, "errors": [], "schema_present": False}
            )

        errors: List[str] = []
        try:
            # OpenAPI schemas may use nullable or type specifications supported by jsonschema Validator
            validator = jsonschema.Draft7Validator(response_schema)
            for err in validator.iter_errors(response_body):
                path_str = ".".join(str(p) for p in err.absolute_path) or "root"
                errors.append(f"Schema violation at '{path_str}': {err.message}")

            is_valid = len(errors) == 0
            return ToolResult(
                success=True,
                data={
                    "valid": is_valid,
                    "errors": errors,
                    "schema_present": True
                }
            )
        except jsonschema.SchemaError as se:
            logger.error(f"[SCHEMA_VALIDATOR] Invalid JSON schema provided: {se.message}")
            return ToolResult(
                success=False,
                error=f"Invalid schema: {se.message}",
                data={"valid": False, "errors": [f"Invalid JSON schema: {se.message}"]}
            )
        except Exception as e:
            return ToolResult(
                success=False,
                error=str(e),
                data={"valid": False, "errors": [f"Schema validation error: {str(e)}"]}
            )
