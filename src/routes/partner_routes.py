"""Partner document management routes.

Allows authenticated partner users to upload private documents into Brain.
Documents are tagged with access_scope='partner_internal' and the org's
internal UUID so RLS + application-layer filters keep them invisible to
other tenants.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
import uuid
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, HttpUrl

from src.database.pool import get_async_db_connection
from src.dependencies.session import UserContext, verify_session_required
from src.services.brain_service import BrainService, PageInput, TimelineInput, _validate_slug

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB
MAX_PDF_PAGES = 500
ALLOWED_EXTENSIONS = {".pdf", ".txt", ".md", ".csv"}


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class DocumentResponse(BaseModel):
    document_id: str
    slug: str
    title: str
    status: str
    created_at: str


class DocumentListResponse(BaseModel):
    documents: list[DocumentResponse]
    total: int


# ---------------------------------------------------------------------------
# Auth guard
# ---------------------------------------------------------------------------

def _require_org(session: UserContext) -> str:
    """Return the internal org UUID or raise 403."""
    org_id = session.get_org_id()
    if not org_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Organization context required. Select an organization first.",
        )
    return org_id


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------

async def _extract_text_from_pdf(data: bytes) -> str:
    """Extract text from PDF bytes using pypdf. Runs in a thread to avoid blocking."""
    def _extract(pdf_bytes: bytes) -> str:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError

        try:
            reader = PdfReader(io.BytesIO(pdf_bytes))
        except (PdfReadError, Exception) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Could not read PDF: {exc}",
            ) from exc
        if len(reader.pages) > MAX_PDF_PAGES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"PDF has {len(reader.pages)} pages, max is {MAX_PDF_PAGES}.",
            )
        parts = []
        for page in reader.pages:
            text = page.extract_text()
            if text:
                parts.append(text)
        return "\n\n".join(parts)

    return await asyncio.to_thread(_extract, data)


def _extract_text_plain(data: bytes) -> str:
    """Decode plain text / markdown / CSV."""
    for encoding in ("utf-8", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# S3 helpers
# ---------------------------------------------------------------------------

def _upload_to_s3(org_id: str, doc_id: str, ext: str, data: bytes) -> str:
    """Upload raw document to S3. Returns the S3 key."""
    from src.utils import get_s3_client, get_bucket_name

    s3_key = f"partner-docs/{org_id}/{doc_id}{ext}"
    s3 = get_s3_client()
    s3.put_object(
        Bucket=get_bucket_name(),
        Key=s3_key,
        Body=data,
    )
    return s3_key


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post(
    "/documents",
    response_model=DocumentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a document for partner ingestion",
)
async def upload_document(
    file: UploadFile = File(...),
    session: UserContext = Depends(verify_session_required),
):
    """Upload a PDF, TXT, MD, or CSV file into Brain as partner-private content.

    The document is stored in S3, text is extracted, and a brain_pages row is
    created with access_scope='partner_internal' and partner_id set to the
    caller's organization UUID. Embedding generation happens asynchronously
    via the hook processor.
    """
    org_id = _require_org(session)
    user_id = session.get_user_id()

    # Validate extension
    filename = file.filename or "document"
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported file type '{ext}'. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
        )

    # Read and validate size
    data = await file.read()
    if len(data) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File size {len(data) / 1024 / 1024:.1f} MB exceeds limit of {MAX_FILE_SIZE / 1024 / 1024:.0f} MB.",
        )
    if len(data) == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Empty file.",
        )

    doc_id = uuid.uuid4().hex[:16]
    title = os.path.splitext(filename)[0]

    # Extract text
    if ext == ".pdf":
        text = await _extract_text_from_pdf(data)
    else:
        text = _extract_text_plain(data)

    if not text.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No text could be extracted from this file.",
        )

    # Upload raw file to S3 (in thread to avoid blocking)
    s3_key = await asyncio.to_thread(_upload_to_s3, org_id, doc_id, ext, data)

    # Write to brain
    slug = _validate_slug(f"partner-doc-{org_id[:8]}-{doc_id}")
    content_hash = hashlib.sha256(data).hexdigest()
    now = datetime.now(timezone.utc)

    brain = BrainService()
    async with get_async_db_connection(
        user_id=user_id,
        partner_id=org_id,
    ) as conn:
        async with conn.transaction():
            await brain.put_page(
                conn,
                slug,
                PageInput(
                    type="source_document",
                    title=title,
                    compiled_truth=text,
                    frontmatter={
                        "source_type": "partner_upload",
                        "original_filename": filename,
                        "s3_key": s3_key,
                        "file_size_bytes": len(data),
                        "content_type": file.content_type,
                    },
                    content_hash=content_hash,
                ),
                owner_uuid=user_id,
            )

            await conn.execute(
                """
                UPDATE brain_pages
                SET access_scope = 'partner_internal',
                    partner_id   = $2::uuid,
                    source_id    = $3,
                    fetched_at   = $4
                WHERE slug = $1
                """,
                slug,
                org_id,
                f"partner-upload-{org_id[:8]}",
                now,
            )

            await brain.add_timeline_entry(
                conn,
                slug,
                TimelineInput(
                    date=date.today(),
                    summary=f"Uploaded by partner: {filename}",
                    source="partner_upload",
                ),
                owner_uuid=user_id,
            )

    return DocumentResponse(
        document_id=doc_id,
        slug=slug,
        title=title,
        status="ready",
        created_at=now.isoformat(),
    )


@router.post(
    "/documents/url",
    response_model=DocumentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Submit a URL for partner ingestion",
)
async def submit_url(
    url: HttpUrl,
    title: Optional[str] = None,
    session: UserContext = Depends(verify_session_required),
):
    """Submit a URL for ingestion into Brain as partner-private content.

    Creates a brain_pending_hooks entry that the hook processor will fetch
    and convert to a brain_pages row with partner isolation tags.
    """
    org_id = _require_org(session)
    user_id = session.get_user_id()

    # SSRF protection: block private/internal network addresses
    _validate_url_safety(str(url))

    doc_id = uuid.uuid4().hex[:16]
    slug = _validate_slug(f"partner-url-{org_id[:8]}-{doc_id}")
    display_title = title or str(url)[:80]
    now = datetime.now(timezone.utc)

    brain = BrainService()
    async with get_async_db_connection(
        user_id=user_id,
        partner_id=org_id,
    ) as conn:
        await brain.enqueue_hook(
            conn,
            hook_type="partner_url_fetch",
            payload={
                "url": str(url),
                "slug": slug,
                "title": display_title,
                "org_id": org_id,
                "user_id": user_id,
            },
        )

    return DocumentResponse(
        document_id=doc_id,
        slug=slug,
        title=display_title,
        status="queued",
        created_at=now.isoformat(),
    )


@router.get(
    "/documents",
    response_model=DocumentListResponse,
    summary="List partner documents",
)
async def list_documents(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: UserContext = Depends(verify_session_required),
):
    """List documents uploaded by this partner organization.

    Only returns documents belonging to the caller's org (filtered by
    partner_id via RLS + application-layer check).
    """
    org_id = _require_org(session)
    user_id = session.get_user_id()

    async with get_async_db_connection(
        user_id=user_id,
        partner_id=org_id,
    ) as conn:
        rows = await conn.fetch(
            """
            SELECT slug, title, created_at, updated_at,
                   frontmatter->>'original_filename' AS filename,
                   frontmatter->>'source_type' AS source_type
            FROM brain_pages
            WHERE access_scope = 'partner_internal'
              AND partner_id = $1::uuid
              AND (frontmatter->>'source_type' IN ('partner_upload', 'partner_url_fetch'))
            ORDER BY created_at DESC
            LIMIT $2 OFFSET $3
            """,
            org_id, limit, offset,
        )

        total = await conn.fetchval(
            """
            SELECT count(*)
            FROM brain_pages
            WHERE access_scope = 'partner_internal'
              AND partner_id = $1::uuid
              AND (frontmatter->>'source_type' IN ('partner_upload', 'partner_url_fetch'))
            """,
            org_id,
        )

    documents = [
        DocumentResponse(
            document_id=row["slug"].split("-")[-1],
            slug=row["slug"],
            title=row["title"],
            status="ready",
            created_at=row["created_at"].isoformat() if row["created_at"] else "",
        )
        for row in rows
    ]

    return DocumentListResponse(documents=documents, total=total)


# ---------------------------------------------------------------------------
# SSRF protection
# ---------------------------------------------------------------------------

def _validate_url_safety(url: str) -> None:
    """Block URLs targeting private/internal networks."""
    import ipaddress
    from urllib.parse import urlparse

    parsed = urlparse(url)
    hostname = parsed.hostname or ""

    if not parsed.scheme or parsed.scheme not in ("http", "https"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only http and https URLs are allowed.",
        )

    blocked_hosts = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"}
    if hostname.lower() in blocked_hosts:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="URLs targeting localhost are not allowed.",
        )

    try:
        addr = ipaddress.ip_address(hostname)
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="URLs targeting private or reserved IP addresses are not allowed.",
            )
    except ValueError:
        # hostname is not an IP, that's fine (it's a domain name)
        # Check for common internal domains
        if hostname.endswith(".internal") or hostname.endswith(".local"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="URLs targeting internal domains are not allowed.",
            )
