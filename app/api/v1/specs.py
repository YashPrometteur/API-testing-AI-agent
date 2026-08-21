import uuid
from typing import Optional
# pyrefly: ignore [missing-import]
from fastapi import APIRouter, File, Form, HTTPException, Request, status, UploadFile
# pyrefly: ignore [missing-import]
from pydantic import BaseModel
from app.database import get_mongo_db
from app.config.settings import settings
from app.repositories.spec_repository import SpecRepository
from app.tools.spec_parser import OpenAPIValidatorTool, SpecParseError, SwaggerParserTool
from app.utils.datetime import current_utc_iso

router = APIRouter(prefix="/specs", tags=["specs"])


class CreateSpecUrlRequest(BaseModel):
    spec_url: Optional[str] = None
    name: Optional[str] = None


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_spec(
    request: Request,
    spec_url: Optional[str] = Form(None),
    name: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
):
    custom_name = name

    content_type = request.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        try:
            body_json = await request.json()
            if isinstance(body_json, dict):
                spec_url = body_json.get("spec_url") or spec_url
                custom_name = body_json.get("name") or custom_name
        except Exception:
            pass

    if not spec_url and not file:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Both spec_url and file are missing. Provide either a spec_url or a file upload."
        )

    raw_input = ""
    source_type = ""
    source_ref = ""

    if file and file.filename and file.filename.strip():
        source_type = "file"
        source_ref = file.filename
        content_bytes = await file.read()
        raw_input = content_bytes.decode("utf-8", errors="ignore")
    elif spec_url and spec_url.strip() and spec_url.strip().lower() != "string":
        source_type = "url"
        source_ref = spec_url.strip()
        raw_input = spec_url.strip()

    # Parse spec using SwaggerParserTool
    try:
        parse_result = await SwaggerParserTool.parse(raw_input)
        if not parse_result.success:
            err_msg = parse_result.error or "Failed to parse spec"
            if "HTTP status error" in err_msg or "Unreachable URL" in err_msg:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"spec_url unreachable: {err_msg}"
                )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Malformed spec: {err_msg}"
            )
    except SpecParseError as spe:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Malformed spec: {str(spe)}"
        )
    except Exception as e:
        if "Unreachable" in str(e) or "502" in str(e) or "connect" in str(e).lower():
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"spec_url unreachable: {str(e)}"
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Malformed spec: {str(e)}"
        )

    normalized_spec = parse_result.data
    val_result = OpenAPIValidatorTool.validate(normalized_spec)
    if not val_result.data.get("valid", False):
        errs = val_result.data.get("errors", [])
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Malformed spec: OpenAPI validation errors: {', '.join(errs)}"
        )

    # Extract name from info.title or fallback
    info_title = normalized_spec.get("info", {}).get("title")
    spec_name = custom_name or info_title or source_ref

    spec_id = f"spec_{uuid.uuid4().hex[:12]}"
    created_at = current_utc_iso()

    doc = {
        "_id": spec_id,
        "name": spec_name,
        "source_type": source_type,
        "source_ref": source_ref,
        "openapi_version": parse_result.metadata.get("openapi_version", "3.0.0"),
        "raw_spec": normalized_spec,
        "endpoint_count": parse_result.metadata.get("endpoint_count", 0),
        "created_at": created_at,
        "updated_at": created_at,
        "is_deleted": False,
    }

   
    db = await get_mongo_db()
    repo = SpecRepository(db)
    await repo.insert(doc)

    return {
        "spec_id": spec_id,
        "name": spec_name,
        "source_type": source_type,
        "endpoint_count": doc["endpoint_count"],
        "openapi_version": doc["openapi_version"],
        "created_at": created_at,
    }
