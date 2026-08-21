import logging
from typing import Any, Dict, List
from app.graph.state import EndpointSpec, TestRunState
from app.tools.spec_parser import OpenAPIValidatorTool, SpecParseError, SwaggerParserTool

logger = logging.getLogger("discovery_agent")


class DiscoveryAgent:
    """Purely deterministic Discovery Agent. Extracts EndpointSpec items from spec_source."""

    async def __call__(self, state: TestRunState) -> Dict[str, Any]:
        spec_source = state.get("spec_source")
        if not spec_source:
            return {
                "status": "failed",
                "errors": ["Missing spec_source in TestRunState"]
            }

        try:
            # Parse raw spec using SwaggerParserTool
            parse_result = await SwaggerParserTool.parse(spec_source)
            if not parse_result.success:
                return {
                    "status": "failed",
                    "errors": [f"Spec parse error: {parse_result.error}"]
                }

            normalized_spec = parse_result.data

            # Validate normalized spec using OpenAPIValidatorTool
            validation_result = OpenAPIValidatorTool.validate(normalized_spec)
            if not validation_result.data.get("valid", False):
                errs = validation_result.data.get("errors", ["Invalid OpenAPI spec structure"])
                return {
                    "status": "failed",
                    "errors": [f"OpenAPI validation failed: {', '.join(errs)}"]
                }

            # Map paths & operations to EndpointSpec objects
            endpoints: List[EndpointSpec] = []
            paths = normalized_spec.get("paths", {})

            for path, path_item in paths.items():
                if not isinstance(path_item, dict):
                    continue

                # Common parameters defined at path level
                path_parameters = path_item.get("parameters", [])

                for method, op in path_item.items():
                    if method.lower() not in ["get", "post", "put", "delete", "patch", "options", "head"]:
                        continue

                    if not isinstance(op, dict):
                        continue

                    operation_id = op.get("operationId") or f"{method.upper()}_{path.replace('/', '_').strip('_')}"
                    
                    # Merge path level parameters with operation level parameters
                    op_parameters = op.get("parameters", [])
                    all_parameters = list(path_parameters) + list(op_parameters)

                    # Extract request schema
                    request_schema = None
                    request_body = op.get("requestBody")
                    if isinstance(request_body, dict):
                        content = request_body.get("content", {})
                        if isinstance(content, dict):
                            json_content = content.get("application/json") or content.get("*/*") or next(iter(content.values()), None) if content else None
                            if isinstance(json_content, dict):
                                request_schema = json_content.get("schema")

                    # Extract response schemas
                    response_schemas = {}
                    responses = op.get("responses", {})
                    if isinstance(responses, dict):
                        for status_code, resp_obj in responses.items():
                            if isinstance(resp_obj, dict):
                                resp_content = resp_obj.get("content", {})
                                schema = None
                                if isinstance(resp_content, dict):
                                    json_resp = resp_content.get("application/json") or next(iter(resp_content.values()), None) if resp_content else None
                                    if isinstance(json_resp, dict):
                                        schema = json_resp.get("schema")
                                response_schemas[str(status_code)] = {
                                    "description": resp_obj.get("description", ""),
                                    "schema": schema
                                }

                    # Extract security requirements
                    global_security = normalized_spec.get("security", [])
                    op_security = op.get("security", global_security)

                    endpoint_obj = {
                        "path": path,
                        "method": method.upper(),
                        "operation_id": operation_id,
                        "summary": op.get("summary", ""),
                        "description": op.get("description", ""),
                        "parameters": all_parameters,
                        "request_schema": request_schema,
                        "response_schemas": response_schemas,
                        "security_requirements": op_security if isinstance(op_security, list) else [],
                        "tags": op.get("tags", []) if isinstance(op.get("tags"), list) else [],
                    }
                    endpoints.append(endpoint_obj)

            logger.info(f"[DISCOVERY] Total raw endpoints extracted from spec: {len(endpoints)}")

            # Filter by target_operations / target_endpoints if specified
            target_operations = state.get("target_operations") or []
            if target_operations:
                target_ops_lower = [t.lower() for t in target_operations]
                filtered_endpoints = []
                for ep in endpoints:
                    op_id = ep.get("operation_id", "").lower()
                    path = ep.get("path", "").lower()
                    method_path = f"{ep.get('method', '').lower()} {path}"
                    if op_id in target_ops_lower or method_path in target_ops_lower or path in target_ops_lower:
                        filtered_endpoints.append(ep)
                endpoints = filtered_endpoints
                op_summary_list = [f"{ep.get('method')} {ep.get('path')} ({ep.get('operation_id')})" for ep in endpoints]
                logger.info(f"[DISCOVERY] Filtered down to {len(endpoints)} target operations matching {target_operations}: {op_summary_list}")
            else:
                logger.info(f"[DISCOVERY] No target_operations specified. Processing all {len(endpoints)} endpoints in spec.")

            logger.info(f"DiscoveryAgent successfully extracted {len(endpoints)} endpoints.")
            return {
                "endpoints": endpoints,
                "status": "running"
            }

        except SpecParseError as spe:
            logger.error(f"DiscoveryAgent unparseable spec hard stop: {spe}")
            return {
                "status": "failed",
                "errors": [f"Unparseable spec error: {str(spe)}"]
            }
        except Exception as e:
            logger.error(f"DiscoveryAgent unexpected error: {e}")
            return {
                "status": "failed",
                "errors": [f"DiscoveryAgent execution failed: {str(e)}"]
            }
