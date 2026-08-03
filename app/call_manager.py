"""Twilio outbound call initiation."""

import logging

from twilio.rest import Client

from app.config import get_settings

logger = logging.getLogger(__name__)


class CallManager:
    def __init__(self) -> None:
        settings = get_settings()
        self._client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
        self._from_number = settings.TWILIO_PHONE_NUMBER
        self._base_url = settings.BASE_URL.rstrip("/")

    def initiate_call(self, to_number: str, call_id: str) -> str:
        """Place an outbound call; Twilio fetches TwiML from our voice webhook."""
        call = self._client.calls.create(
            to=to_number,
            from_=self._from_number,
            url=f"{self._base_url}/api/twilio/voice?call_id={call_id}",
            method="POST",
            status_callback=f"{self._base_url}/api/twilio/status",
            status_callback_method="POST",
            status_callback_event=["initiated", "answered", "completed"],
        )
        logger.info("Outbound call created: sid=%s to=%s call_id=%s", call.sid, to_number, call_id)
        return call.sid
