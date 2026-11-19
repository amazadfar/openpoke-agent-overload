from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from .. import flags
from ..config import Settings, get_settings
from ..models import GmailConnectPayload, GmailDisconnectPayload, GmailStatusPayload
from ..services import disconnect_account, fetch_status, initiate_connect
from ..services.gmail.executor import run_gmail

router = APIRouter(prefix="/gmail", tags=["gmail"])


@router.post("/connect")
# Initiate Gmail OAuth connection flow through Composio
async def gmail_connect(payload: GmailConnectPayload, settings: Settings = Depends(get_settings)) -> JSONResponse:
    if flags.enabled("GMAIL_READS_OFF_LOOP"):
        return await run_gmail(initiate_connect, payload, settings)
    return initiate_connect(payload, settings)


@router.post("/status")
# Check the current Gmail connection status and user information
async def gmail_status(payload: GmailStatusPayload) -> JSONResponse:
    if flags.enabled("GMAIL_READS_OFF_LOOP"):
        # The profile fetch inside is a blocking Composio call (client.py:159).
        return await run_gmail(fetch_status, payload)
    return fetch_status(payload)


@router.post("/disconnect")
# Disconnect Gmail account and clear cached profile data
async def gmail_disconnect(payload: GmailDisconnectPayload) -> JSONResponse:
    if flags.enabled("GMAIL_READS_OFF_LOOP"):
        return await run_gmail(disconnect_account, payload)
    return disconnect_account(payload)
