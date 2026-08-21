import asyncio
import json
import re
import time
import httpx
import logging
import yaml
from typing import Any, Dict, List, Union
from app.tools import ToolResult

logger = logging.getLogger("spec_parser")


class SpecParseError(Exception):
    """Raised when OpenAPI / Swagger spec input is genuinely malformed or unparseable."""
    pass


class SwaggerParserTool:
    """Tool for fetching, parsing, converting (Swagger 2.0 -> OpenAPI 3.x), and resolving $ref pointers."""

    @staticmethod
    async def fetch_spec_url(url: str) -> str:
        delays = [1.0, 2.0]
        last_err = None
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            for attempt in range(3):
                try:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    return resp.text
                except Exception as e:
                    last_err = e
                    if attempt < 2:
                        delay = delays[attempt]
                        logger.warning(
                            f"Spec URL fetch failed (attempt {attempt + 1}/3): {e}. "
                            f"Retrying in {delay}s..."
                        )
                        await asyncio.sleep(delay)
                    else:
                        raise e
        raise last_err or RuntimeError(f"Unreachable URL: {url}")

    @classmethod
    def _resolve_refs(cls, obj: Any, root: Dict[str, Any], depth: int = 0) -> Any:
        if depth > 20:  # Prevent circular $ref recursion stack overflow
            return obj

        if isinstance(obj, dict):
            if "$ref" in obj and isinstance(obj["$ref"], str):
                ref_path = obj["$ref"]
                if ref_path.startswith("#/"):
                    parts = ref_path.lstrip("#/").split("/")
                    curr = root
                    found = True
                    for p in parts:
                        p_unescaped = p.replace("~1", "/").replace("~0", "~")
                        if isinstance(curr, dict) and p_unescaped in curr:
                            curr = curr[p_unescaped]
                        else:
                            found = False
                            break
                    if found and isinstance(curr, dict):
                        # Merge sibling properties over resolved ref
                        resolved = cls._resolve_refs(curr, root, depth + 1)
                        if isinstance(resolved, dict):
                            merged = dict(resolved)
                            for k, v in obj.items():
                                if k != "$ref":
                                    merged[k] = v
                            return merged
                        return resolved
            return {k: cls._resolve_refs(v, root, depth + 1) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [cls._resolve_refs(item, root, depth + 1) for item in obj]
        return obj

    @classmethod
    def _convert_swagger_2_to_3(cls, spec: Dict[str, Any]) -> Dict[str, Any]:
        spec_v3 = dict(spec)
        spec_v3["openapi"] = "3.0.3"
        spec_v3.pop("swagger", None)

        # Convert host, basePath, schemes -> servers
        host = spec.get("host", "")
        base_path = spec.get("basePath", "")
        schemes = spec.get("schemes", ["http"])
        servers = []
        if host:
            for s in schemes:
                servers.append({"url": f"{s}://{host}{base_path}"})
        elif base_path:
            servers.append({"url": base_path})
        if servers:
            spec_v3["servers"] = servers
            spec_v3.pop("host", None)
            spec_v3.pop("basePath", None)
            spec_v3.pop("schemes", None)

        # Convert definitions -> components.schemas
        components = spec_v3.get("components", {})
        if "definitions" in spec:
            schemas = components.get("schemas", {})
            schemas.update(spec.get("definitions", {}))
            components["schemas"] = schemas
            spec_v3.pop("definitions", None)

        # Convert securityDefinitions -> components.securitySchemes
        if "securityDefinitions" in spec:
            sec_schemes = components.get("securitySchemes", {})
            for sec_name, sec_def in spec["securityDefinitions"].items():
                sec_type = sec_def.get("type", "")
                if sec_type == "basic":
                    sec_schemes[sec_name] = {"type": "http", "scheme": "basic"}
                elif sec_type == "apiKey":
                    sec_schemes[sec_name] = {
                        "type": "apiKey",
                        "name": sec_def.get("name", "api_key"),
                        "in": sec_def.get("in", "header")
                    }
                elif sec_type == "oauth2":
                    sec_schemes[sec_name] = {"type": "oauth2", "flows": {}}
                else:
                    sec_schemes[sec_name] = sec_def
            components["securitySchemes"] = sec_schemes
            spec_v3.pop("securityDefinitions", None)

        spec_v3["components"] = components

        # Fix parameters body in paths
        paths = spec_v3.get("paths", {})
        for path, path_item in paths.items():
            if not isinstance(path_item, dict):
                continue
            for method, op in path_item.items():
                if not isinstance(op, dict):
                    continue
                params = op.get("parameters", [])
                new_params = []
                for p in params:
                    if isinstance(p, dict) and p.get("in") == "body":
                        # Convert body parameter to requestBody
                        schema = p.get("schema", {})
                        op["requestBody"] = {
                            "required": p.get("required", False),
                            "content": {"application/json": {"schema": schema}}
                        }
                    else:
                        new_params.append(p)
                op["parameters"] = new_params
        return spec_v3

    @classmethod
    async def parse(cls, raw_input: Union[str, Dict[str, Any]]) -> ToolResult:
        """Parse raw spec (URL, JSON/YAML string, or dict) into normalized OpenAPI 3.x dict."""
        try:
            spec_dict: Dict[str, Any] = {}
            source_type = "dict"

            if isinstance(raw_input, str):
                raw_input_str = raw_input.strip()
                if raw_input_str.startswith("http://") or raw_input_str.startswith("https://"):
                    source_type = "url"
                    raw_content = await cls.fetch_spec_url(raw_input_str)
                else:
                    source_type = "raw_content"
                    raw_content = raw_input_str

                try:
                    spec_dict = json.loads(raw_content)
                except Exception:
                    try:
                        spec_dict = yaml.safe_load(raw_content)
                    except Exception as ye:
                        raise SpecParseError(f"Failed to parse input as JSON or YAML: {ye}")
            elif isinstance(raw_input, dict):
                spec_dict = raw_input
            else:
                raise SpecParseError(f"Unsupported spec input type: {type(raw_input)}")

            if not isinstance(spec_dict, dict):
                raise SpecParseError("Spec root must be a JSON object / dictionary")

            # Check version & convert Swagger 2.0 -> 3.x
            is_swagger_2 = spec_dict.get("swagger") == "2.0"
            is_openapi_3 = str(spec_dict.get("openapi", "")).startswith("3.")

            if not is_swagger_2 and not is_openapi_3:
                raise SpecParseError("Unsupported specification version. Must be Swagger 2.0 or OpenAPI 3.x.")

            if is_swagger_2:
                spec_dict = cls._convert_swagger_2_to_3(spec_dict)

            # Resolve $ref pointers
            normalized = cls._resolve_refs(spec_dict, spec_dict)

            # Count endpoints
            paths = normalized.get("paths", {})
            op_count = 0
            for p, path_obj in paths.items():
                if isinstance(path_obj, dict):
                    for m in path_obj.keys():
                        if m.lower() in ["get", "post", "put", "delete", "patch", "options", "head"]:
                            op_count += 1

            version = normalized.get("openapi", "3.0.0")
            return ToolResult(
                success=True,
                data=normalized,
                metadata={
                    "openapi_version": version,
                    "endpoint_count": op_count,
                    "source_type": source_type,
                    "converted_from_swagger2": is_swagger_2
                }
            )

        except SpecParseError as spe:
            raise spe
        except httpx.HTTPStatusError as hse:
            return ToolResult(success=False, error=f"HTTP status error fetching spec: {hse.response.status_code}")
        except Exception as e:
            return ToolResult(success=False, error=f"Error parsing specification: {str(e)}")


class OpenAPIValidatorTool:
    """Tool for validating normalized OpenAPI 3.x specification structure."""

    @classmethod
    def validate(cls, spec_dict: Dict[str, Any]) -> ToolResult:
        errors = []
        if not isinstance(spec_dict, dict):
            return ToolResult(success=True, data={"valid": False, "errors": ["Spec must be a dictionary object"]})

        if not str(spec_dict.get("openapi", "")).startswith("3."):
            errors.append("Missing or invalid 'openapi' version string (must start with 3.)")

        if "info" not in spec_dict or not isinstance(spec_dict["info"], dict):
            errors.append("Missing required 'info' object")
        elif "title" not in spec_dict["info"]:
            errors.append("Missing required 'info.title'")

        if "paths" not in spec_dict or not isinstance(spec_dict["paths"], dict):
            errors.append("Missing required 'paths' object")

        valid = len(errors) == 0
        return ToolResult(
            success=True,
            data={"valid": valid, "errors": errors},
            metadata={"error_count": len(errors)}
        )
