# brevo_client.py
# Minimal async Brevo client for transactional emails.
# Docs: https://developers.brevo.com/reference/sendtransacemail

from __future__ import annotations
import os, base64, asyncio
from typing import Mapping, Optional, Sequence, Tuple, Union
import httpx

BREVO_API_KEY = os.getenv("BREVO_API_KEY")
BREVO_ENDPOINT = os.getenv("BREVO_ENDPOINT", "https://api.brevo.com/v3/smtp/email")
MAIL_FROM = os.getenv("MAIL_FROM", "no-reply@activlink.io")
MAIL_FROM_NAME = os.getenv("MAIL_FROM_NAME", "Activlink")
# Staging: Brevo accepts and validates the request but delivers nothing.
BREVO_SANDBOX = os.getenv("BREVO_SANDBOX", "false").lower() == "true"

# A recipient is an address, or (address, display name).
Recipient = Union[str, Tuple[str, str]]


class BrevoError(Exception):
    pass


def _recipient(value: Recipient) -> dict:
    if isinstance(value, tuple):
        email, name = value
        return {"email": email, "name": name} if name else {"email": email}
    return {"email": value}


class BrevoEmailClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        sender_email: Optional[str] = None,
        sender_name: Optional[str] = None,
        timeout: float = 20.0,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        self.api_key = api_key or BREVO_API_KEY
        if not self.api_key:
            raise BrevoError("BREVO_API_KEY is missing")
        self.sender_email = sender_email or MAIL_FROM
        self.sender_name = sender_name or MAIL_FROM_NAME
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self._transport = transport

        self._headers = {
            "api-key": self.api_key,
            "accept": "application/json",
            "content-type": "application/json",
        }
        if BREVO_SANDBOX:
            self._headers["X-Sib-Sandbox"] = "drop"

    async def send(
        self,
        to: Sequence[Recipient],
        subject: Optional[str] = None,
        html: Optional[str] = None,
        *,
        template_id: Optional[int] = None,
        text: Optional[str] = None,
        reply_to: Optional[Tuple[str, str]] = None,  # (email, name)
        headers: Optional[Mapping[str, str]] = None,
        tags: Optional[Sequence[str]] = None,
        attachments: Optional[Sequence[Tuple[str, bytes, str]]] = None,  # (filename, content, mime)
        params: Optional[Mapping[str, object]] = None,  # template variables
    ) -> dict:
        """
        Returns Brevo JSON response on success; raises BrevoError on failure.

        Send either `template_id` (a Brevo template, filled from `params`; `subject`, if given,
        overrides the template's) or `subject` + `html`.

        Only 429 is retried. Brevo has no idempotency key, so a timeout or 5xx may mean the
        message was accepted, and retrying would email the customer twice. Callers record a
        failure and decide themselves whether to send again.
        """
        if template_id is None and not (subject and html):
            raise BrevoError("Either template_id or subject and html are required")

        payload: dict = {
            "sender": {"email": self.sender_email, "name": self.sender_name},
            "to": [_recipient(e) for e in to],
        }
        if template_id is not None:
            payload["templateId"] = int(template_id)
        if subject:
            payload["subject"] = subject
        if html:
            payload["htmlContent"] = html
        if text:
            payload["textContent"] = text
        if reply_to:
            payload["replyTo"] = {"email": reply_to[0], "name": reply_to[1]}
        if headers:
            payload["headers"] = dict(headers)
        if tags:
            payload["tags"] = list(tags)
        if params:
            payload["params"] = dict(params)
        if attachments:
            payload["attachment"] = [
                {
                    "name": fname,
                    "content": base64.b64encode(content).decode("utf-8"),
                }
                for (fname, content, _mime) in attachments
            ]

        async with httpx.AsyncClient(timeout=self.timeout, transport=self._transport) as client:
            for attempt in range(self.max_retries):
                try:
                    r = await client.post(BREVO_ENDPOINT, headers=self._headers, json=payload)
                except (httpx.TimeoutException, httpx.TransportError) as e:
                    raise BrevoError(f"Brevo request failed: {type(e).__name__}") from e
                if r.status_code < 400:
                    return r.json() if r.headers.get("content-type", "").startswith("application/json") else {"status": r.status_code}
                if r.status_code == 429 and attempt + 1 < self.max_retries:
                    await asyncio.sleep(self.backoff_base * (2 ** attempt))
                    continue
                # The body echoes the request on some errors, so keep it short in messages
                # that end up in logs.
                raise BrevoError(f"Brevo send failed: {r.status_code} {r.text[:300]}")

        raise BrevoError("Brevo send failed: rate limited")
