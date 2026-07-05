"""Twilio outbound call initiation and management."""

import logging
from urllib.parse import urlencode

from twilio.rest import Client

from app.config import get_settings

logger = logging.getLogger(__name__)


class CallManager:
    """Handles outbound call initiation via Twilio."""

    def __init__(self):
        self.settings = get_settings()
        self.client = Client(
            self.settings.TWILIO_ACCOUNT_SID,
            self.settings.TWILIO_AUTH_TOKEN,
        )

    def initiate_call(
        self,
        to_number: str,
        scenario: str = "margin_shortfall",
        customer_name: str = "Customer",
        metadata: dict | None = None,
    ) -> str:
        """
        Initiate an outbound call via Twilio.

        Args:
            to_number: Customer phone number in E.164 format (e.g. +919876543210).
            scenario: Call scenario — margin_shortfall | kyc_expiry | inbound_support.
            customer_name: Customer name for prompt personalization.
            metadata: Extra context (shortfall_amount, deadline, expiry_date, etc.).

        Returns:
            Twilio Call SID.
        """
        base_url = self.settings.BASE_URL.rstrip("/")

        query_params: dict = {"scenario": scenario, "customer_name": customer_name}
        if metadata:
            query_params.update(metadata)

        webhook_url = f"{base_url}/api/twilio/voice?{urlencode(query_params)}"
        status_url = f"{base_url}/api/twilio/status"

        call = self.client.calls.create(
            to=to_number,
            from_=self.settings.TWILIO_PHONE_NUMBER,
            url=webhook_url,
            status_callback=status_url,
            status_callback_event=["initiated", "ringing", "answered", "completed"],
            status_callback_method="POST",
            record=True,
        )

        logger.info(
            "Outbound call initiated: SID=%s to=%s scenario=%s",
            call.sid,
            to_number,
            scenario,
        )
        return call.sid

    def end_call(self, call_sid: str) -> None:
        """End an active call."""
        self.client.calls(call_sid).update(status="completed")
        logger.info("Call ended: SID=%s", call_sid)

    def get_call_status(self, call_sid: str) -> str:
        """Get current status of a call."""
        call = self.client.calls(call_sid).fetch()
        return call.status
