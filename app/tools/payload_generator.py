import copy
import logging
import re
from typing import Any, Dict, List, Optional
import uuid

from app.graph.state import EndpointSpec
from app.utils.ids import generate_test_case_id

logger = logging.getLogger("payload_generator")


class SchemaPayloadGenerator:
    """Senior QA / SDET deterministic generator producing complete boundary, permutation, and security test suites directly from OpenAPI schemas."""

    @staticmethod
    def _get_default_valid_value(prop_schema: Dict[str, Any], prop_name: str = "") -> Any:
        """Generate a single valid value matching the JSON Schema type and constraints."""
        if not prop_schema or not isinstance(prop_schema, dict):
            return "test_val"

        prop_type = prop_schema.get("type")
        if not prop_type and "anyOf" in prop_schema:
            for sub in prop_schema["anyOf"]:
                if sub.get("type") and sub["type"] != "null":
                    return SchemaPayloadGenerator._get_default_valid_value(sub, prop_name)
            return "test_val"

        if "enum" in prop_schema and prop_schema["enum"]:
            return prop_schema["enum"][0]

        if prop_type == "string":
            if prop_schema.get("format") == "date-time":
                return "2026-08-31T10:00:00.000Z"
            if prop_schema.get("format") == "date" or "dob" in prop_name.lower() or "birth" in prop_name.lower():
                return "1995-05-15"
            if prop_schema.get("format") == "email" or "email" in prop_name.lower():
                return f"qa.user.{uuid.uuid4().hex[:6]}@example.com"
            if "otp" in prop_name.lower() or "pin" in prop_name.lower() or "passcode" in prop_name.lower():
                return "123456"
            if "country" in prop_name.lower() or "dial" in prop_name.lower():
                return "+1"
            if "identifier" in prop_name.lower() or "contact" in prop_name.lower() or "login_id" in prop_name.lower():
                return f"qa.user.{uuid.uuid4().hex[:4]}@example.com"
            if prop_name.lower() in ("lang", "language") or "locale" in prop_name.lower():
                return "en"
            if "first" in prop_name.lower() or "fname" in prop_name.lower():
                return "Alexander"
            if "last" in prop_name.lower() or "lname" in prop_name.lower() or "surname" in prop_name.lower():
                return "Johnson"
            if prop_schema.get("format") == "uuid" or "session" in prop_name.lower() or "token" in prop_name.lower() or prop_name.lower().endswith("_id"):
                return "550e8400-e29b-41d4-a716-446655440000"
            if "phone" in prop_name.lower() or "mobile" in prop_name.lower():
                return "+14155552671"
            if "name" in prop_name.lower() or "author" in prop_name.lower() or "user" in prop_name.lower():
                return "Elizabeth"
            if "title" in prop_name.lower() or "slug" in prop_name.lower():
                return "StandardTitle"
            if "desc" in prop_name.lower() or "bio" in prop_name.lower():
                return "Valid QA test description string."
            min_len = prop_schema.get("minLength", 1)
            return ("ValidAlpha" * max(min_len, 1))[:prop_schema.get("maxLength", 20)]

        elif prop_type in ("integer", "number"):
            min_val = prop_schema.get("minimum")
            if min_val is not None:
                return min_val + 1 if prop_type == "integer" else float(min_val + 1.0)
            return 10 if prop_type == "integer" else 19.99

        elif prop_type == "boolean":
            return True

        elif prop_type == "array":
            items_schema = prop_schema.get("items", {"type": "string"})
            return [SchemaPayloadGenerator._get_default_valid_value(items_schema, prop_name)]

        elif prop_type == "object":
            sub_props = prop_schema.get("properties", {})
            return {k: SchemaPayloadGenerator._get_default_valid_value(v, k) for k, v in sub_props.items()}

        return "valid_sample"

    @classmethod
    def generate_valid_body(cls, schema: Optional[Dict[str, Any]]) -> Optional[Any]:
        """Build a complete, schema-valid JSON body including all properties."""
        if not schema or not isinstance(schema, dict):
            return None

        schema_type = schema.get("type", "object")
        if schema_type == "array":
            items_schema = schema.get("items", {})
            return [cls.generate_valid_body(items_schema)]

        props = schema.get("properties", {})
        if not props and "anyOf" in schema:
            for sub in schema["anyOf"]:
                if sub.get("properties"):
                    props = sub["properties"]
                    break

        if not props:
            return {}

        body = {}
        for prop_name, prop_spec in props.items():
            body[prop_name] = cls._get_default_valid_value(prop_spec, prop_name)

        # Cross-field conditional rules (e.g. request-otp endpoint rules, add-secondary)
        if isinstance(body, dict):
            if "secondary_contact" in body:
                body["type"] = "phone"
                body["country_code"] = "+1"
                body["secondary_contact"] = "9179847541"
            elif body.get("type") == "email" and "country_code" in body:
                body.pop("country_code", None)
            elif body.get("type") == "phone":
                body["identifier"] = "9876543210"
                body["country_code"] = "+1"

        return body

    @classmethod
    def generate_minimal_valid_body(cls, schema: Optional[Dict[str, Any]]) -> Optional[Any]:
        """Build a minimal valid body containing ONLY required properties."""
        if not schema or not isinstance(schema, dict):
            return None
        props = schema.get("properties", {})
        required = schema.get("required", [])
        if not required:
            # If nothing strictly required, pick first property or empty object
            if props:
                first_k = next(iter(props))
                return {first_k: cls._get_default_valid_value(props[first_k], first_k)}
            return {}

        body = {k: cls._get_default_valid_value(props.get(k, {}), k) for k in required if k in props}
        if isinstance(body, dict):
            if "secondary_contact" in body:
                body["type"] = "phone"
                body["country_code"] = "+1"
                body["secondary_contact"] = "9179847541"
            elif body.get("type") == "email" and "country_code" in body:
                body.pop("country_code", None)
            elif body.get("type") == "phone":
                body["identifier"] = "9876543210"
                body["country_code"] = "+1"
        return body

    @classmethod
    def generate_category_cases(
        cls,
        endpoint: EndpointSpec,
        category: str,
        target_count: int = 1
    ) -> List[Dict[str, Any]]:
        """Generate deterministic test cases for a specified category and endpoint."""
        cat = category.lower()
        if cat == "functional":
            return cls.generate_functional(endpoint)
        elif cat == "negative":
            return cls.generate_negative(endpoint)
        elif cat == "edge_case":
            return cls.generate_edge_case(endpoint)
        elif cat == "security":
            return cls.generate_security(endpoint)
        elif cat == "auth":
            return cls.generate_auth(endpoint)
        elif cat == "authz":
            return cls.generate_authz(endpoint)
        return []

    @classmethod
    def generate_functional(cls, endpoint: EndpointSpec) -> List[Dict[str, Any]]:
        """Comprehensive functional test suite: full payload, minimal payload, enum permutations, and valid boundaries."""
        op_id = endpoint.get("operation_id", "operation")
        path = endpoint.get("path", "/")
        method = endpoint.get("method", "GET").upper()
        req_schema = endpoint.get("request_schema") or {}
        props = req_schema.get("properties", {})
        cases = []

        expected_code = 200
        if method == "POST":
            expected_code = 201 if "201" in endpoint.get("response_schemas", {}) else 200
        elif method == "DELETE":
            expected_code = 204 if "204" in endpoint.get("response_schemas", {}) else 200

        full_body = cls.generate_valid_body(req_schema) if method in ("POST", "PUT", "PATCH") else None

        # 1. Full Payload: All valid fields populated
        cases.append({
            "id": generate_test_case_id("functional"),
            "endpoint_operation_id": op_id,
            "category": "functional",
            "name": f"Functional: Full valid payload - {method} {path}",
            "request": {
                "method": method,
                "path": path,
                "headers": {},
                "query_params": {},
                "path_params": {},
                "body": full_body
            },
            "expected": {
                "status_code": expected_code,
                "response_schema_match": True,
                "max_latency_ms": 5000.0
            },
            "priority": "high",
            "generated_by": "deterministic_payload_generator_v1",
            "rationale": "Verify successful response when all schema properties are provided with valid data."
        })

        # 2. Minimal Payload: Strictly required fields only (for POST/PUT/PATCH with optional fields)
        if method in ("POST", "PUT", "PATCH") and req_schema.get("required") and len(req_schema.get("required", [])) < len(props):
            min_body = cls.generate_minimal_valid_body(req_schema)
            cases.append({
                "id": generate_test_case_id("functional"),
                "endpoint_operation_id": op_id,
                "category": "functional",
                "name": f"Functional: Minimal required fields only - {method} {path}",
                "request": {
                    "method": method,
                    "path": path,
                    "headers": {},
                    "query_params": {},
                    "path_params": {},
                    "body": min_body
                },
                "expected": {
                    "status_code": expected_code,
                    "response_schema_match": True,
                    "max_latency_ms": 5000.0
                },
                "priority": "medium",
                "generated_by": "deterministic_payload_generator_v1",
                "rationale": "Verify endpoint accepts payload containing only mandatory fields while omitting optional ones."
            })

        # 3. Enum Permutations: 1 valid functional case for each enum choice
        if isinstance(full_body, dict):
            for prop_name, prop_spec in props.items():
                enums = prop_spec.get("enum", [])
                if len(enums) > 1:
                    for enum_val in enums[1:]:
                        enum_body = copy.deepcopy(full_body)
                        enum_body[prop_name] = enum_val

                        # Apply cross-field semantics for enum changes (e.g. request-otp type)
                        if prop_name == "type" and enum_val == "phone":
                            enum_body["identifier"] = "9876543210"
                            enum_body["country_code"] = "+1"
                        elif prop_name == "type" and enum_val == "email":
                            enum_body["identifier"] = "qa.tester@example.com"
                            enum_body.pop("country_code", None)

                        cases.append({
                            "id": generate_test_case_id("functional"),
                            "endpoint_operation_id": op_id,
                            "category": "functional",
                            "name": f"Functional: Valid enum '{enum_val}' for '{prop_name}' - {method} {path}",
                            "request": {
                                "method": method,
                                "path": path,
                                "headers": {},
                                "query_params": {},
                                "path_params": {},
                                "body": enum_body
                            },
                            "expected": {
                                "status_code": expected_code,
                                "response_schema_match": True,
                                "max_latency_ms": 5000.0
                            },
                            "priority": "medium",
                            "generated_by": "deterministic_payload_generator_v1",
                            "rationale": f"Verify legitimate alternate enum value '{enum_val}' for property '{prop_name}'."
                        })

        # 4. Field Boundary & Format Variations (Email subdomains, name boundaries, secondary contact types)
        if isinstance(full_body, dict):
            # Email with subdomain
            if "identifier" in full_body and full_body.get("type") == "email":
                sub_body = copy.deepcopy(full_body)
                sub_body["identifier"] = f"qa.user.{uuid.uuid4().hex[:4]}@sub.domain.org"
                cases.append({
                    "id": generate_test_case_id("functional"),
                    "endpoint_operation_id": op_id,
                    "category": "functional",
                    "name": f"Functional: Valid email with subdomain - {method} {path}",
                    "request": {"method": method, "path": path, "headers": {}, "query_params": {}, "path_params": {}, "body": sub_body},
                    "expected": {"status_code": expected_code, "response_schema_match": True, "max_latency_ms": 5000.0},
                    "priority": "medium",
                    "generated_by": "deterministic_payload_generator_v1",
                    "rationale": "Verify endpoint accepts valid email addresses with subdomains."
                })

            # Secondary Contact variations (phone vs email)
            if "secondary_contact" in full_body:
                # Secondary Phone variation
                phone_sec_body = copy.deepcopy(full_body)
                phone_sec_body["type"] = "phone"
                phone_sec_body["country_code"] = "+1"
                phone_sec_body["secondary_contact"] = "9179847541"
                phone_sec_body["is_test"] = True
                cases.append({
                    "id": generate_test_case_id("functional"),
                    "endpoint_operation_id": op_id,
                    "category": "functional",
                    "name": f"Functional: Add secondary phone contact - {method} {path}",
                    "request": {"method": method, "path": path, "headers": {}, "query_params": {}, "path_params": {}, "body": phone_sec_body},
                    "expected": {"status_code": expected_code, "response_schema_match": True, "max_latency_ms": 5000.0},
                    "priority": "medium",
                    "generated_by": "deterministic_payload_generator_v1",
                    "rationale": "Verify endpoint successfully adds a phone as secondary contact."
                })

                # Secondary Email variation
                email_sec_body = copy.deepcopy(full_body)
                email_sec_body["type"] = "email"
                email_sec_body.pop("country_code", None)
                email_sec_body["secondary_contact"] = f"qa.secondary.{uuid.uuid4().hex[:4]}@sub.example.com"
                email_sec_body["is_test"] = True
                cases.append({
                    "id": generate_test_case_id("functional"),
                    "endpoint_operation_id": op_id,
                    "category": "functional",
                    "name": f"Functional: Add secondary email contact - {method} {path}",
                    "request": {"method": method, "path": path, "headers": {}, "query_params": {}, "path_params": {}, "body": email_sec_body},
                    "expected": {"status_code": expected_code, "response_schema_match": True, "max_latency_ms": 5000.0},
                    "priority": "medium",
                    "generated_by": "deterministic_payload_generator_v1",
                    "rationale": "Verify endpoint successfully adds an email as secondary contact."
                })

            # Name min length (2 chars)
            if "first_name" in full_body and "last_name" in full_body:
                min_name_body = copy.deepcopy(full_body)
                min_name_body["first_name"] = "Al"
                min_name_body["last_name"] = "Li"
                cases.append({
                    "id": generate_test_case_id("functional"),
                    "endpoint_operation_id": op_id,
                    "category": "functional",
                    "name": f"Functional: Minimum valid name length (2 characters) - {method} {path}",
                    "request": {"method": method, "path": path, "headers": {}, "query_params": {}, "path_params": {}, "body": min_name_body},
                    "expected": {"status_code": expected_code, "response_schema_match": True, "max_latency_ms": 5000.0},
                    "priority": "medium",
                    "generated_by": "deterministic_payload_generator_v1",
                    "rationale": "Verify endpoint accepts boundary 2-character minimum first and last names."
                })

                # Compound / Multi-word name
                compound_name_body = copy.deepcopy(full_body)
                compound_name_body["first_name"] = "Mary Jane"
                compound_name_body["last_name"] = "Alexander Smith"
                cases.append({
                    "id": generate_test_case_id("functional"),
                    "endpoint_operation_id": op_id,
                    "category": "functional",
                    "name": f"Functional: Valid compound name format - {method} {path}",
                    "request": {"method": method, "path": path, "headers": {}, "query_params": {}, "path_params": {}, "body": compound_name_body},
                    "expected": {"status_code": expected_code, "response_schema_match": True, "max_latency_ms": 5000.0},
                    "priority": "medium",
                    "generated_by": "deterministic_payload_generator_v1",
                    "rationale": "Verify endpoint accepts valid compound names."
                })

        return cases

    @classmethod
    def generate_negative(cls, endpoint: EndpointSpec) -> List[Dict[str, Any]]:
        """Senior QA Negative test suite: single & multi-field omissions, data type mutations, format/semantic violations, and invalid enums."""
        op_id = endpoint.get("operation_id", "operation")
        path = endpoint.get("path", "/")
        method = endpoint.get("method", "GET").upper()
        req_schema = endpoint.get("request_schema") or {}
        cases = []

        valid_base_body = cls.generate_valid_body(req_schema) or {}
        props = req_schema.get("properties", {})
        required_fields = req_schema.get("required", [])
        expected_negative_status = 422 if "422" in endpoint.get("response_schemas", {}) else 400

        if method in ("POST", "PUT", "PATCH") and isinstance(valid_base_body, dict):
            # 1. Single-field omission for each required field
            for req_field in required_fields:
                if req_field in valid_base_body:
                    omitted_body = copy.deepcopy(valid_base_body)
                    omitted_body.pop(req_field, None)
                    cases.append({
                        "id": generate_test_case_id("negative"),
                        "endpoint_operation_id": op_id,
                        "category": "negative",
                        "name": f"Negative: Missing required field '{req_field}' - {method} {path}",
                        "request": {
                            "method": method,
                            "path": path,
                            "headers": {},
                            "query_params": {},
                            "path_params": {},
                            "body": omitted_body
                        },
                        "expected": {
                            "status_code": expected_negative_status,
                            "response_schema_match": True,
                            "max_latency_ms": 5000.0
                        },
                        "priority": "high",
                        "generated_by": "deterministic_payload_generator_v1",
                        "rationale": f"Omit required property '{req_field}' to test schema validation rejection."
                    })

            # 2. Multi-field omission: Omit ALL required fields simultaneously ({})
            if len(required_fields) >= 2:
                cases.append({
                    "id": generate_test_case_id("negative"),
                    "endpoint_operation_id": op_id,
                    "category": "negative",
                    "name": f"Negative: All required fields omitted simultaneously - {method} {path}",
                    "request": {
                        "method": method,
                        "path": path,
                        "headers": {},
                        "query_params": {},
                        "path_params": {},
                        "body": {}
                    },
                    "expected": {
                        "status_code": expected_negative_status,
                        "response_schema_match": True,
                        "max_latency_ms": 5000.0
                    },
                    "priority": "high",
                    "generated_by": "deterministic_payload_generator_v1",
                    "rationale": "Verify API returns multi-error validation response when all required properties are missing."
                })

            # 3. Wrong data types for each property
            for prop_name, prop_spec in props.items():
                prop_type = prop_spec.get("type", "string")
                if prop_type in ("integer", "number"):
                    wrong_val = "not_a_valid_number"
                elif prop_type == "string":
                    wrong_val = 123456
                elif prop_type == "array":
                    wrong_val = "not_an_array_string"
                elif prop_type == "boolean":
                    wrong_val = "not_a_boolean"
                elif prop_type == "object":
                    wrong_val = "not_an_object"
                else:
                    wrong_val = {"invalid": "shape"}

                wrong_type_body = copy.deepcopy(valid_base_body)
                wrong_type_body[prop_name] = wrong_val
                cases.append({
                    "id": generate_test_case_id("negative"),
                    "endpoint_operation_id": op_id,
                    "category": "negative",
                    "name": f"Negative: Wrong data type for '{prop_name}' - {method} {path}",
                    "request": {
                        "method": method,
                        "path": path,
                        "headers": {},
                        "query_params": {},
                        "path_params": {},
                        "body": wrong_type_body
                    },
                    "expected": {
                        "status_code": expected_negative_status,
                        "response_schema_match": True,
                        "max_latency_ms": 5000.0
                    },
                    "priority": "high",
                    "generated_by": "deterministic_payload_generator_v1",
                    "rationale": f"Send incompatible type '{type(wrong_val).__name__}' for property '{prop_name}' (expected '{prop_type}')."
                })

            # 4. Format & Semantic Violations (Email, Date, Phone)
            for prop_name, prop_spec in props.items():
                fmt = prop_spec.get("format", "")
                # Email format violation
                if fmt == "email" or "email" in prop_name.lower():
                    bad_email_body = copy.deepcopy(valid_base_body)
                    bad_email_body[prop_name] = "invalid_email_without_at_sign"
                    cases.append({
                        "id": generate_test_case_id("negative"),
                        "endpoint_operation_id": op_id,
                        "category": "negative",
                        "name": f"Negative: Malformed email syntax on '{prop_name}' - {method} {path}",
                        "request": {
                            "method": method,
                            "path": path,
                            "headers": {},
                            "query_params": {},
                            "path_params": {},
                            "body": bad_email_body
                        },
                        "expected": {
                            "status_code": expected_negative_status,
                            "response_schema_match": True,
                            "max_latency_ms": 5000.0
                        },
                        "priority": "high",
                        "generated_by": "deterministic_payload_generator_v1",
                        "rationale": f"Pass malformed email string to test RFC email format validation on '{prop_name}'."
                    })

                # Date format & Future Date of Birth violation
                if fmt in ("date", "date-time") or "dob" in prop_name.lower() or "birth" in prop_name.lower():
                    future_date_body = copy.deepcopy(valid_base_body)
                    future_date_body[prop_name] = "2045-01-01"
                    cases.append({
                        "id": generate_test_case_id("negative"),
                        "endpoint_operation_id": op_id,
                        "category": "negative",
                        "name": f"Negative: Invalid future date boundary on '{prop_name}' - {method} {path}",
                        "request": {
                            "method": method,
                            "path": path,
                            "headers": {},
                            "query_params": {},
                            "path_params": {},
                            "body": future_date_body
                        },
                        "expected": {
                            "status_code": expected_negative_status,
                            "response_schema_match": True,
                            "max_latency_ms": 5000.0
                        },
                        "priority": "medium",
                        "generated_by": "deterministic_payload_generator_v1",
                        "rationale": f"Test business validation rules for future date value on '{prop_name}'."
                    })

                # Phone number format violation
                if "phone" in prop_name.lower() or "mobile" in prop_name.lower():
                    bad_phone_body = copy.deepcopy(valid_base_body)
                    bad_phone_body[prop_name] = "123-abc-phone-invalid"
                    cases.append({
                        "id": generate_test_case_id("negative"),
                        "endpoint_operation_id": op_id,
                        "category": "negative",
                        "name": f"Negative: Letters in phone number field '{prop_name}' - {method} {path}",
                        "request": {
                            "method": method,
                            "path": path,
                            "headers": {},
                            "query_params": {},
                            "path_params": {},
                            "body": bad_phone_body
                        },
                        "expected": {
                            "status_code": expected_negative_status,
                            "response_schema_match": True,
                            "max_latency_ms": 5000.0
                        },
                        "priority": "medium",
                        "generated_by": "deterministic_payload_generator_v1",
                        "rationale": f"Send alphabetic characters in numeric phone field '{prop_name}'."
                    })

                # Name letters-only format violation (digits/symbols in name field)
                if ("first" in prop_name.lower() or "last" in prop_name.lower() or "name" in prop_name.lower()) and prop_spec.get("type") == "string":
                    bad_name_body = copy.deepcopy(valid_base_body)
                    bad_name_body[prop_name] = "John_123!"
                    cases.append({
                        "id": generate_test_case_id("negative"),
                        "endpoint_operation_id": op_id,
                        "category": "negative",
                        "name": f"Negative: Digits and symbols in letters-only field '{prop_name}' - {method} {path}",
                        "request": {
                            "method": method,
                            "path": path,
                            "headers": {},
                            "query_params": {},
                            "path_params": {},
                            "body": bad_name_body
                        },
                        "expected": {
                            "status_code": expected_negative_status,
                            "response_schema_match": True,
                            "max_latency_ms": 5000.0
                        },
                        "priority": "high",
                        "generated_by": "deterministic_payload_generator_v1",
                        "rationale": f"Test letters-only constraint by passing digits and symbols into '{prop_name}'."
                    })

            # 5. Invalid Enum Values
            for prop_name, prop_spec in props.items():
                if "enum" in prop_spec and prop_spec["enum"]:
                    invalid_enum_body = copy.deepcopy(valid_base_body)
                    invalid_enum_body[prop_name] = "INVALID_ENUM_VALUE_XYZ"
                    cases.append({
                        "id": generate_test_case_id("negative"),
                        "endpoint_operation_id": op_id,
                        "category": "negative",
                        "name": f"Negative: Invalid enum value for '{prop_name}' - {method} {path}",
                        "request": {
                            "method": method,
                            "path": path,
                            "headers": {},
                            "query_params": {},
                            "path_params": {},
                            "body": invalid_enum_body
                        },
                        "expected": {
                            "status_code": expected_negative_status,
                            "response_schema_match": True,
                            "max_latency_ms": 5000.0
                        },
                        "priority": "high",
                        "generated_by": "deterministic_payload_generator_v1",
                        "rationale": f"Pass value 'INVALID_ENUM_VALUE_XYZ' outside permitted choices {prop_spec.get('enum')}."
                    })

            # 6. Whitespace-only string on required string fields
            for req_field in required_fields:
                if props.get(req_field, {}).get("type") == "string":
                    ws_body = copy.deepcopy(valid_base_body)
                    ws_body[req_field] = "     "
                    cases.append({
                        "id": generate_test_case_id("negative"),
                        "endpoint_operation_id": op_id,
                        "category": "negative",
                        "name": f"Negative: Whitespace-only string on required field '{req_field}' - {method} {path}",
                        "request": {
                            "method": method,
                            "path": path,
                            "headers": {},
                            "query_params": {},
                            "path_params": {},
                            "body": ws_body
                        },
                        "expected": {
                            "status_code": expected_negative_status,
                            "response_schema_match": True,
                            "max_latency_ms": 5000.0
                        },
                        "priority": "medium",
                        "generated_by": "deterministic_payload_generator_v1",
                        "rationale": f"Test input trimming validation by sending whitespace-only string on required '{req_field}'."
                    })

            # 7. Null value for required properties
            for req_field in required_fields:
                null_body = copy.deepcopy(valid_base_body)
                null_body[req_field] = None
                cases.append({
                    "id": generate_test_case_id("negative"),
                    "endpoint_operation_id": op_id,
                    "category": "negative",
                    "name": f"Negative: Explicit null value for required field '{req_field}' - {method} {path}",
                    "request": {
                        "method": method,
                        "path": path,
                        "headers": {},
                        "query_params": {},
                        "path_params": {},
                        "body": null_body
                    },
                    "expected": {
                        "status_code": expected_negative_status,
                        "response_schema_match": True,
                        "max_latency_ms": 5000.0
                    },
                    "priority": "high",
                    "generated_by": "deterministic_payload_generator_v1",
                    "rationale": f"Pass explicit null on non-nullable required property '{req_field}'."
                })

            # 8. Real API Business Logic Violations
            # Wrong OTP rejection
            if "otp" in props:
                wrong_otp_body = copy.deepcopy(valid_base_body)
                wrong_otp_body["otp"] = "00000"
                cases.append({
                    "id": generate_test_case_id("negative"),
                    "endpoint_operation_id": op_id,
                    "category": "negative",
                    "name": f"Negative: Invalid OTP format (5 digits) - {method} {path}",
                    "request": {"method": method, "path": path, "headers": {}, "query_params": {}, "path_params": {}, "body": wrong_otp_body},
                    "expected": {"status_code": 422 if "422" in endpoint.get("response_schemas", {}) else 400, "response_schema_match": True, "max_latency_ms": 5000.0},
                    "priority": "high",
                    "generated_by": "deterministic_payload_generator_v1",
                    "rationale": "Verify endpoint rejects malformed 5-digit OTP code."
                })

            # Expired / Invalid Session ID
            if "session_id" in props:
                expired_sess_body = copy.deepcopy(valid_base_body)
                expired_sess_body["session_id"] = "00000000-0000-0000-0000-000000000000"
                cases.append({
                    "id": generate_test_case_id("negative"),
                    "endpoint_operation_id": op_id,
                    "category": "negative",
                    "name": f"Negative: Expired or nonexistent session ID - {method} {path}",
                    "request": {"method": method, "path": path, "headers": {}, "query_params": {}, "path_params": {}, "body": expired_sess_body},
                    "expected": {"status_code": 400, "response_schema_match": True, "max_latency_ms": 5000.0},
                    "priority": "high",
                    "generated_by": "deterministic_payload_generator_v1",
                    "rationale": "Verify endpoint rejects expired or fabricated session ID."
                })

            # Short Password (< 8 chars)
            if "password" in props:
                short_pwd_body = copy.deepcopy(valid_base_body)
                short_pwd_body["password"] = "123"
                cases.append({
                    "id": generate_test_case_id("negative"),
                    "endpoint_operation_id": op_id,
                    "category": "negative",
                    "name": f"Negative: Password shorter than minimum length - {method} {path}",
                    "request": {"method": method, "path": path, "headers": {}, "query_params": {}, "path_params": {}, "body": short_pwd_body},
                    "expected": {"status_code": 422, "response_schema_match": True, "max_latency_ms": 5000.0},
                    "priority": "high",
                    "generated_by": "deterministic_payload_generator_v1",
                    "rationale": "Verify endpoint rejects passwords below the minimum length threshold."
                })

        # 9. GET / DELETE / Param-based Negative Cases
        has_path_params = bool(re.search(r"\{[^}]+\}", path)) or bool(endpoint.get("path_params"))
        endpoint_params = endpoint.get("parameters") or []
        constrained_query_params = [
            p for p in endpoint_params
            if isinstance(p, dict) and p.get("in") == "query" and (
                p.get("required") or (p.get("schema") or {}).get("type") in ("integer", "number", "boolean") or (p.get("schema") or {}).get("enum")
            )
        ]
        has_query_params = bool(constrained_query_params)

        if has_path_params:
            # Non-existent ID parameter / Malformed URL path
            cases.append({
                "id": generate_test_case_id("negative"),
                "endpoint_operation_id": op_id,
                "category": "negative",
                "name": f"Negative: Non-existent ID format - {method} {path}",
                "request": {
                    "method": method,
                    "path": path,
                    "headers": {},
                    "query_params": {},
                    "path_params": {"id": "non_existent_id_000000", "plan_id": "non_existent_plan_000000", "profile_id": "non_existent_user_000000", "photo_id": "non_existent_photo_000000"},
                    "body": None
                },
                "expected": {
                    "status_code": 404 if "404" in endpoint.get("response_schemas", {}) else 400,
                    "response_schema_match": True,
                    "max_latency_ms": 5000.0
                },
                "priority": "high",
                "generated_by": "deterministic_payload_generator_v1",
                "rationale": "Verify endpoint returns HTTP 404 Not Found or 400 Bad Request when resource ID does not exist."
            })

        if has_query_params:
            first_q = constrained_query_params[0]
            q_name = first_q.get("name", "filter")
            q_type = (first_q.get("schema") or {}).get("type", "string")
            wrong_q_val = "not_a_number" if q_type in ("integer", "number") else 123456
            cases.append({
                "id": generate_test_case_id("negative"),
                "endpoint_operation_id": op_id,
                "category": "negative",
                "name": f"Negative: Invalid query parameter '{q_name}' type - {method} {path}",
                "request": {
                    "method": method,
                    "path": path,
                    "headers": {},
                    "query_params": {q_name: wrong_q_val},
                    "path_params": {},
                    "body": None
                },
                "expected": {
                    "status_code": 422 if "422" in endpoint.get("response_schemas", {}) else 400,
                    "response_schema_match": True,
                    "max_latency_ms": 5000.0
                },
                "priority": "medium",
                "generated_by": "deterministic_payload_generator_v1",
                "rationale": f"Test parameter type validation by passing invalid value for query parameter '{q_name}'."
            })
        elif not cases and not has_path_params:
            cases.append({
                "id": generate_test_case_id("negative"),
                "endpoint_operation_id": op_id,
                "category": "negative",
                "name": f"Negative: Non-existent resource subpath - {method} {path}",
                "request": {
                    "method": method,
                    "path": path.rstrip("/") + "/non_existent_endpoint_xyz",
                    "headers": {},
                    "query_params": {},
                    "path_params": {},
                    "body": None
                },
                "expected": {
                    "status_code": 404,
                    "response_schema_match": True,
                    "max_latency_ms": 5000.0
                },
                "priority": "medium",
                "generated_by": "deterministic_payload_generator_v1",
                "rationale": "Verify server returns HTTP 404 Not Found on nonexistent resource paths."
            })

        return cases

    @classmethod
    def generate_edge_case(cls, endpoint: EndpointSpec) -> List[Dict[str, Any]]:
        """Senior QA Edge Case suite: min/max boundary limits, large payload buffers, floating-point precision, empty string, and unicode."""
        op_id = endpoint.get("operation_id", "operation")
        path = endpoint.get("path", "/")
        method = endpoint.get("method", "GET").upper()
        req_schema = endpoint.get("request_schema") or {}
        cases = []

        valid_base_body = cls.generate_valid_body(req_schema) or {}
        props = req_schema.get("properties", {})
        expected_negative_status = 422 if "422" in endpoint.get("response_schemas", {}) else 400

        if isinstance(valid_base_body, dict):
            for prop_name, prop_spec in props.items():
                prop_type = prop_spec.get("type", "string")

                # Numeric boundaries: Zero, negative numbers, large overflow, precision
                if prop_type in ("integer", "number"):
                    # 1. Negative boundary (-1)
                    edge_neg = copy.deepcopy(valid_base_body)
                    edge_neg[prop_name] = -1 if prop_type == "integer" else -10.99
                    cases.append({
                        "id": generate_test_case_id("edge_case"),
                        "endpoint_operation_id": op_id,
                        "category": "edge_case",
                        "name": f"Edge Case: Negative boundary value on '{prop_name}' - {method} {path}",
                        "request": {
                            "method": method,
                            "path": path,
                            "headers": {},
                            "query_params": {},
                            "path_params": {},
                            "body": edge_neg
                        },
                        "expected": {
                            "status_code": expected_negative_status,
                            "response_schema_match": True,
                            "max_latency_ms": 5000.0
                        },
                        "priority": "medium",
                        "generated_by": "deterministic_payload_generator_v1",
                        "rationale": f"Test boundary rejection for negative numeric value on '{prop_name}'."
                    })

                    # 2. Zero boundary (0)
                    edge_zero = copy.deepcopy(valid_base_body)
                    edge_zero[prop_name] = 0 if prop_type == "integer" else 0.0
                    cases.append({
                        "id": generate_test_case_id("edge_case"),
                        "endpoint_operation_id": op_id,
                        "category": "edge_case",
                        "name": f"Edge Case: Zero boundary value on '{prop_name}' - {method} {path}",
                        "request": {
                            "method": method,
                            "path": path,
                            "headers": {},
                            "query_params": {},
                            "path_params": {},
                            "body": edge_zero
                        },
                        "expected": {
                            "status_code": expected_negative_status if prop_spec.get("minimum", 0) > 0 else 200,
                            "response_schema_match": True,
                            "max_latency_ms": 5000.0
                        },
                        "priority": "medium",
                        "generated_by": "deterministic_payload_generator_v1",
                        "rationale": f"Test zero value boundary condition on '{prop_name}'."
                    })

                    # 3. High precision float
                    if prop_type == "number":
                        edge_prec = copy.deepcopy(valid_base_body)
                        edge_prec[prop_name] = 19.9999999999
                        cases.append({
                            "id": generate_test_case_id("edge_case"),
                            "endpoint_operation_id": op_id,
                            "category": "edge_case",
                            "name": f"Edge Case: High floating-point precision on '{prop_name}' - {method} {path}",
                            "request": {
                                "method": method,
                                "path": path,
                                "headers": {},
                                "query_params": {},
                                "path_params": {},
                                "body": edge_prec
                            },
                            "expected": {
                                "status_code": 200,
                                "response_schema_match": True,
                                "max_latency_ms": 5000.0
                            },
                            "priority": "low",
                            "generated_by": "deterministic_payload_generator_v1",
                            "rationale": f"Test decimal precision handling and rounding on '{prop_name}'."
                        })

                    # 4. Large Integer Overflow (999999999999)
                    edge_overflow = copy.deepcopy(valid_base_body)
                    edge_overflow[prop_name] = 999999999999
                    cases.append({
                        "id": generate_test_case_id("edge_case"),
                        "endpoint_operation_id": op_id,
                        "category": "edge_case",
                        "name": f"Edge Case: Large integer overflow on '{prop_name}' - {method} {path}",
                        "request": {
                            "method": method,
                            "path": path,
                            "headers": {},
                            "query_params": {},
                            "path_params": {},
                            "body": edge_overflow
                        },
                        "expected": {
                            "status_code": 200 if method in ("POST", "PUT", "PATCH") else 400,
                            "response_schema_match": True,
                            "max_latency_ms": 5000.0
                        },
                        "priority": "low",
                        "generated_by": "deterministic_payload_generator_v1",
                        "rationale": f"Test upper bounds integer overflow on property '{prop_name}'."
                    })

                # String boundaries: Empty string, exact min/max length, Unicode & Emojis, Long text buffer
                elif prop_type == "string":
                    # 1. Empty string ("")
                    edge_empty = copy.deepcopy(valid_base_body)
                    edge_empty[prop_name] = ""
                    cases.append({
                        "id": generate_test_case_id("edge_case"),
                        "endpoint_operation_id": op_id,
                        "category": "edge_case",
                        "name": f"Edge Case: Empty string on '{prop_name}' - {method} {path}",
                        "request": {
                            "method": method,
                            "path": path,
                            "headers": {},
                            "query_params": {},
                            "path_params": {},
                            "body": edge_empty
                        },
                        "expected": {
                            "status_code": expected_negative_status if prop_spec.get("minLength", 0) > 0 else 200,
                            "response_schema_match": True,
                            "max_latency_ms": 5000.0
                        },
                        "priority": "medium",
                        "generated_by": "deterministic_payload_generator_v1",
                        "rationale": f"Test empty string boundary on property '{prop_name}'."
                    })

                    # 2. International Unicode & Emojis
                    edge_unicode = copy.deepcopy(valid_base_body)
                    edge_unicode[prop_name] = "ñáéíóú 🚀 \u0000\t"
                    cases.append({
                        "id": generate_test_case_id("edge_case"),
                        "endpoint_operation_id": op_id,
                        "category": "edge_case",
                        "name": f"Edge Case: Unicode & Emojis on '{prop_name}' - {method} {path}",
                        "request": {
                            "method": method,
                            "path": path,
                            "headers": {},
                            "query_params": {},
                            "path_params": {},
                            "body": edge_unicode
                        },
                        "expected": {
                            "status_code": 200 if method in ("POST", "PUT", "PATCH") else 400,
                            "response_schema_match": True,
                            "max_latency_ms": 5000.0
                        },
                        "priority": "low",
                        "generated_by": "deterministic_payload_generator_v1",
                        "rationale": f"Test UTF-8 decoding and internationalization support on property '{prop_name}'."
                    })

                    # 3. Oversized string buffer (1,000 chars)
                    edge_large_str = copy.deepcopy(valid_base_body)
                    edge_large_str[prop_name] = "A" * 1000
                    cases.append({
                        "id": generate_test_case_id("edge_case"),
                        "endpoint_operation_id": op_id,
                        "category": "edge_case",
                        "name": f"Edge Case: 1000-character long string buffer on '{prop_name}' - {method} {path}",
                        "request": {
                            "method": method,
                            "path": path,
                            "headers": {},
                            "query_params": {},
                            "path_params": {},
                            "body": edge_large_str
                        },
                        "expected": {
                            "status_code": expected_negative_status if prop_spec.get("maxLength", 5000) < 1000 else 200,
                            "response_schema_match": True,
                            "max_latency_ms": 5000.0
                        },
                        "priority": "low",
                        "generated_by": "deterministic_payload_generator_v1",
                        "rationale": f"Test maximum length string buffer boundary on '{prop_name}'."
                    })

                # Array boundaries: Empty array []
                elif prop_type == "array":
                    edge_arr_empty = copy.deepcopy(valid_base_body)
                    edge_arr_empty[prop_name] = []
                    cases.append({
                        "id": generate_test_case_id("edge_case"),
                        "endpoint_operation_id": op_id,
                        "category": "edge_case",
                        "name": f"Edge Case: Empty array '[]' on '{prop_name}' - {method} {path}",
                        "request": {
                            "method": method,
                            "path": path,
                            "headers": {},
                            "query_params": {},
                            "path_params": {},
                            "body": edge_arr_empty
                        },
                        "expected": {
                            "status_code": 200,
                            "response_schema_match": True,
                            "max_latency_ms": 5000.0
                        },
                        "priority": "medium",
                        "generated_by": "deterministic_payload_generator_v1",
                        "rationale": f"Test boundary behavior when array property '{prop_name}' is empty."
                    })

        if not cases:
            cases.append({
                "id": generate_test_case_id("edge_case"),
                "endpoint_operation_id": op_id,
                "category": "edge_case",
                "name": f"Edge Case: Empty query parameters boundary - {method} {path}",
                "request": {
                    "method": method,
                    "path": path,
                    "headers": {},
                    "query_params": {},
                    "path_params": {},
                    "body": None
                },
                "expected": {
                    "status_code": 200,
                    "response_schema_match": True,
                    "max_latency_ms": 5000.0
                },
                "priority": "low",
                "generated_by": "deterministic_payload_generator_v1",
                "rationale": "Test default response with boundary empty parameters."
            })

        return cases

    @classmethod
    def generate_security(cls, endpoint: EndpointSpec) -> List[Dict[str, Any]]:
        """Senior QA Security suite: Mass Assignment / Privilege Escalation, SQLi, NoSQLi, XSS, and Path Traversal."""
        op_id = endpoint.get("operation_id", "operation")
        path = endpoint.get("path", "/")
        method = endpoint.get("method", "GET").upper()
        req_schema = endpoint.get("request_schema") or {}
        cases = []

        valid_base_body = cls.generate_valid_body(req_schema) or {}
        props = req_schema.get("properties", {})

        # 1. Mass Assignment / Privilege Escalation Probe
        if method in ("POST", "PUT", "PATCH") and isinstance(valid_base_body, dict):
            mass_assign_body = copy.deepcopy(valid_base_body)
            mass_assign_body["is_admin"] = True
            mass_assign_body["role"] = "superuser"
            mass_assign_body["wallet_balance"] = 999999
            mass_assign_body["permissions"] = ["*"]
            cases.append({
                "id": generate_test_case_id("security"),
                "endpoint_operation_id": op_id,
                "category": "security",
                "name": f"Security: Mass Assignment & Privilege Escalation Probe - {method} {path}",
                "request": {
                    "method": method,
                    "path": path,
                    "headers": {},
                    "query_params": {},
                    "path_params": {},
                    "body": mass_assign_body
                },
                "expected": {
                    "status_code": 200 if method in ("POST", "PUT", "PATCH") else 400,
                    "response_schema_match": True,
                    "max_latency_ms": 5000.0
                },
                "priority": "high",
                "generated_by": "deterministic_payload_generator_v1",
                "rationale": "Verify endpoint safely ignores or strips unauthorized admin/privilege fields without granting elevated rights."
            })

        # 2. Injections across string properties (SQLi, NoSQLi, XSS, Path Traversal)
        injections = [
            ("SQLi", "' OR '1'='1' --"),
            ("SQLi_Union", "' UNION SELECT NULL, NULL --"),
            ("NoSQLi", {"$gt": ""}),
            ("NoSQLi_Ne", {"$ne": None}),
            ("XSS", "<script>alert(1)</script>"),
            ("XSS_Img", "<img src=x onerror=alert(1)>"),
            ("Path_Traversal", "../../../../etc/passwd")
        ]

        if isinstance(valid_base_body, dict):
            for prop_name, prop_spec in props.items():
                if prop_spec.get("type") == "string":
                    for inj_type, inj_payload in injections:
                        sec_body = copy.deepcopy(valid_base_body)
                        sec_body[prop_name] = inj_payload
                        cases.append({
                            "id": generate_test_case_id("security"),
                            "endpoint_operation_id": op_id,
                            "category": "security",
                            "name": f"Security: {inj_type} injection probe on '{prop_name}' - {method} {path}",
                            "request": {
                                "method": method,
                                "path": path,
                                "headers": {},
                                "query_params": {},
                                "path_params": {},
                                "body": sec_body
                            },
                            "expected": {
                                "status_code": 400 if "NoSQL" in inj_type else 200,
                                "response_schema_match": True,
                                "max_latency_ms": 5000.0
                            },
                            "priority": "high",
                            "generated_by": "deterministic_payload_generator_v1",
                            "rationale": f"Verify input sanitization and parameter binding resilience against {inj_type} on '{prop_name}'."
                        })
                    break  # Apply the rich injection suite to the primary string field

        if not cases:
            # Query param injection for GET/DELETE
            cases.append({
                "id": generate_test_case_id("security"),
                "endpoint_operation_id": op_id,
                "category": "security",
                "name": f"Security: SQLi parameter injection - {method} {path}",
                "request": {
                    "method": method,
                    "path": path,
                    "headers": {},
                    "query_params": {"q": "' OR 1=1 --"},
                    "path_params": {},
                    "body": None
                },
                "expected": {
                    "status_code": 400,
                    "response_schema_match": True,
                    "max_latency_ms": 5000.0
                },
                "priority": "high",
                "generated_by": "deterministic_payload_generator_v1",
                "rationale": "Test parameter sanitization against SQL injection."
            })

        return cases

    @classmethod
    def generate_auth(cls, endpoint: EndpointSpec) -> List[Dict[str, Any]]:
        """Senior QA Auth test suite: missing authorization header, malformed bearer format, and unsupported content-type."""
        op_id = endpoint.get("operation_id", "operation")
        path = endpoint.get("path", "/")
        method = endpoint.get("method", "GET").upper()
        req_schema = endpoint.get("request_schema")
        body = cls.generate_valid_body(req_schema) if method in ("POST", "PUT", "PATCH") else None

        has_security = bool(endpoint.get("security_requirements", []))
        expected_status = 401 if has_security else 200
        cases = []

        # 1. Missing Authorization Header
        cases.append({
            "id": generate_test_case_id("auth"),
            "endpoint_operation_id": op_id,
            "category": "auth",
            "name": f"Auth: Missing Authorization Header - {method} {path}",
            "request": {
                "method": method,
                "path": path,
                "headers": {},
                "query_params": {},
                "path_params": {},
                "body": body,
                "omit_auth": True
            },
            "expected": {
                "status_code": expected_status,
                "response_schema_match": True,
                "max_latency_ms": 5000.0
            },
            "priority": "high",
            "generated_by": "deterministic_payload_generator_v1",
            "rationale": "Verify endpoint correctly rejects unauthenticated requests with HTTP 401 Unauthorized."
        })

        # 2. Malformed Bearer Token
        cases.append({
            "id": generate_test_case_id("auth"),
            "endpoint_operation_id": op_id,
            "category": "auth",
            "name": f"Auth: Malformed Bearer token format - {method} {path}",
            "request": {
                "method": method,
                "path": path,
                "headers": {"Authorization": "Bearer invalid_expired_jwt_token_12345"},
                "query_params": {},
                "path_params": {},
                "body": body
            },
            "expected": {
                "status_code": expected_status,
                "response_schema_match": True,
                "max_latency_ms": 5000.0
            },
            "priority": "high",
            "generated_by": "deterministic_payload_generator_v1",
            "rationale": "Verify endpoint rejects corrupted / expired JWT credentials with HTTP 401 Unauthorized."
        })

        # 3. Unsupported Content-Type (e.g. text/plain on JSON endpoint)
        if method in ("POST", "PUT", "PATCH"):
            cases.append({
                "id": generate_test_case_id("auth"),
                "endpoint_operation_id": op_id,
                "category": "auth",
                "name": f"Auth: Unsupported Content-Type Header - {method} {path}",
                "request": {
                    "method": method,
                    "path": path,
                    "headers": {"Content-Type": "text/plain"},
                    "query_params": {},
                    "path_params": {},
                    "body": "plain_text_unsupported_body"
                },
                "expected": {
                    "status_code": 415 if "415" in endpoint.get("response_schemas", {}) else 422,
                    "response_schema_match": True,
                    "max_latency_ms": 5000.0
                },
                "priority": "medium",
                "generated_by": "deterministic_payload_generator_v1",
                "rationale": "Verify endpoint validates Content-Type header and rejects unsupported MIME types with HTTP 415 or 422."
            })

        return cases

    @classmethod
    def generate_authz(cls, endpoint: EndpointSpec) -> List[Dict[str, Any]]:
        """Senior QA Authz test suite: Read-only unauthorized role on write operations and cross-tenant privilege checks."""
        op_id = endpoint.get("operation_id", "operation")
        path = endpoint.get("path", "/")
        method = endpoint.get("method", "GET").upper()
        req_schema = endpoint.get("request_schema")
        body = cls.generate_valid_body(req_schema) if method in ("POST", "PUT", "PATCH") else None

        has_security = bool(endpoint.get("security_requirements", []))
        expected_status = 403 if has_security else 200
        cases = []

        # 1. Read-Only / Guest token on write endpoint
        cases.append({
            "id": generate_test_case_id("authz"),
            "endpoint_operation_id": op_id,
            "category": "authz",
            "name": f"Authz: Insufficient Role Permissions (Read-Only Token) - {method} {path}",
            "request": {
                "method": method,
                "path": path,
                "headers": {"Authorization": "Bearer read_only_guest_role_token"},
                "query_params": {},
                "path_params": {},
                "body": body
            },
            "expected": {
                "status_code": expected_status,
                "response_schema_match": True,
                "max_latency_ms": 5000.0
            },
            "priority": "high",
            "generated_by": "deterministic_payload_generator_v1",
            "rationale": "Verify endpoint enforces RBAC authorization and rejects unprivileged tokens with HTTP 403 Forbidden."
        })

        # 2. Cross-Tenant IDOR role check
        cases.append({
            "id": generate_test_case_id("authz"),
            "endpoint_operation_id": op_id,
            "category": "authz",
            "name": f"Authz: Cross-Tenant / IDOR Unauthorized Access Probe - {method} {path}",
            "request": {
                "method": method,
                "path": path,
                "headers": {"Authorization": "Bearer tenant_b_unauthorized_token"},
                "query_params": {},
                "path_params": {},
                "body": body
            },
            "expected": {
                "status_code": expected_status,
                "response_schema_match": True,
                "max_latency_ms": 5000.0
            },
            "priority": "high",
            "generated_by": "deterministic_payload_generator_v1",
            "rationale": "Verify endpoint prevents multi-tenant data leakage by enforcing tenant ownership with HTTP 403/404."
        })

        return cases
