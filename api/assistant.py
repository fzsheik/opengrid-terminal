"""The OpenGrid Assistant.

    POST /v1/assistant     {"message": str (<= 2000 chars), "context": {...previous context echo...}}   [data:read]
                           -> envelope({reply, cards, context, engine, grounded, intent, llm})

Read-only: answers come from in-process market reads (assistant/tools.py) and, for a signed-in caller with
deployments:read, reads of the caller's own jobs (assistant/jobtools.py, GET /v1/jobs ownership rules); nothing is
written, no job, quote or route request is created, approved or cancelled, no provider is called. Job actions
(start the free simulation, cancel a job) come back as ACTION cards that the browser performs only after the user
clicks and confirms, through the existing POST /v1/jobs and POST /v1/jobs/{id}/cancel. Anonymous visitors may use it when PUBLIC_PAGES is on (api/pages.PUBLIC_POST
lists this exact POST), under the per-IP public budget plus ASSISTANT_PUBLIC_RATE_LIMIT_PER_MINUTE. Never a 503:
without an LLM (the default) or when the LLM fails, the deterministic engine answers. See docs/OPENGRID2_CONTRACTS.md §5.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from accounts import ratelimit, security
from accounts.auth import Principal, require_scope
from api.common import envelope
from assistant import engine
from config import settings

router = APIRouter()


class AssistantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(..., min_length=1, max_length=engine.MAX_MESSAGE)
    context: dict | None = Field(None, description="the `context` returned by the previous reply, echoed back")


# data:read; anonymous visitors get the PUBLIC principal when PUBLIC_PAGES is on (api/pages.PUBLIC_POST lists this
# path; accounts.auth.principal grants it). A dependency, so a bad key is a 401 before the body is validated.
READ = require_scope("data:read")


@router.post("/v1/assistant", tags=["assistant"], summary="Ask the OpenGrid Assistant (read-only, grounded)")
def assistant(request: Request, who: Principal = Depends(READ), body: AssistantRequest = Body(..., examples=[
        {"message": "What GPU should I use to fine-tune Llama 3 8B with LoRA under $50?", "context": None}])):
    if who.kind == "public":
        d = ratelimit.take(("ip", security.client_ip(request) or "?"), "assistant",
                           max(1, settings.assistant_public_rate_limit_per_minute))
        if not d.allowed:
            raise HTTPException(429, "too many assistant questions from this address; slow down or sign in",
                                headers=d.headers())
    try:
        out = engine.respond(body.message, body.context, who)
    except engine.ContextError as e:
        raise HTTPException(422, str(e))
    return envelope(out, kind="mixed", methodology="data-kinds",
                    note="answers use OpenGrid's live market data: prices are observed market prices (not quotes), "
                         "memory and cost figures are estimates and labelled; the assistant cannot take actions")
