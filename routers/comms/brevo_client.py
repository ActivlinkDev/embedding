# brevo_client.py
# Minimal async Brevo (Sendinblue) email client for transactional emails.
# Docs: https://developers.brevo.com/reference/sendtransacemail

from __future__ import annotations
import os, base64, asyncio
from typing import Iterable, Mapping, Optional, Sequence, Tuple
import httpx

# Optional (only needed if you use render_template)
try:
    from jinja2 import Environment, FileSystemLoader, select_autoescape  # type: ignore
    _jinja = Environment(loader=FileSystemLoader("templates"), autoescape=select_autoescape())
except Exception:  # pragma: no cover
    _jinja = None

BREVO_API_KEY = os.getenv("BREVO_API_KEY")  # set this in your env/secrets
BREVO_ENDPOINT = os.getenv("BREVO_ENDPOINT", "https://api.brevo.com/v3/smtp/email")
MAIL_FROM = os.getenv("MAIL_FROM", "no-reply@yourdomain.com")
MAIL_FROM_NAME = os.getenv("MAIL_FROM_NAME", "Activlink")

class BrevoError(Exception):
    pass

class BrevoEmailClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        sender_email: Optional[str] = None,
        sender_name: Optional[str] = None,
        timeout: float = 20.0,
        max_retries: int = 3,
        backoff_base: float = 0.5,
    ):
        self.api_key = api_key or BREVO_API_KEY
        if not self.api_key:
            raise BrevoError("BREVO_API_KEY is missing")
        self.sender_email = sender_email or MAIL_FROM
        self.sender_name = sender_name or MAIL_FROM_NAME
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base

        self._headers = {
            "api-key": self.api_key,
            "accept": "application/json",
            "content-type": "application/json",
        }

    async def send(
        self,
        to: Sequence[str],
        subject: str,
        html: str,
        *,
        text: Optional[str] = None,
        cc: Optional[Sequence[str]] = None,
        bcc: Optional[Sequence[str]] = None,
        reply_to: Optional[Tuple[str, str]] = None,  # (email, name)
        headers: Optional[Mapping[str, str]] = None,
        tags: Optional[Sequence[str]] = None,
        attachments: Optional[Sequence[Tuple[str, bytes, str]]] = None,  # (filename, content, mime)
        params: Optional[Mapping[str, str]] = None,  # template variables (if using Brevo templates)
    ) -> dict:
        """
        Returns Brevo JSON response on success; raises BrevoError on failure.
        """

        payload = {
            "sender": {"email": self.sender_email, "name": self.sender_name},
            "to": [{"email": e} for e in to],
            "subject": subject,
            "htmlContent": html,
        }

        if text:
            payload["textContent"] = text
        if cc:
            payload["cc"] = [{"email": e} for e in cc]
        if bcc:
            payload["bcc"] = [{"email": e} for e in bcc]
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

        attempt = 0
        last_exc: Optional[Exception] = None
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            while attempt < self.max_retries:
                try:
                    r = await client.post(BREVO_ENDPOINT, headers=self._headers, json=payload)
                    # 2xx–3xx success
                    if r.status_code < 400:
                        return r.json() if r.headers.get("content-type", "").startswith("application/json") else {"status": r.status_code}
                    # Retry on 429/5xx
                    if r.status_code in (429, 500, 502, 503, 504):
                        await asyncio.sleep(self.backoff_base * (2 ** attempt))
                        attempt += 1
                        continue
                    # Other errors -> raise
                    raise BrevoError(f"Brevo send failed: {r.status_code} {r.text}")
                except (httpx.TimeoutException, httpx.TransportError) as e:
                    last_exc = e
                    await asyncio.sleep(self.backoff_base * (2 ** attempt))
                    attempt += 1

        raise BrevoError(f"Brevo send failed after retries: {last_exc}")

    # Optional convenience: render a Jinja2 template then send
    async def send_template(
        self,
        to: Sequence[str],
        subject: str,
        template_html: str,  # e.g., "welcome.html"
        *,
        template_txt: Optional[str] = None,  # e.g., "welcome.txt"
        vars: Optional[Mapping[str, object]] = None,
        **kwargs,
    ) -> dict:
        if _jinja is None:
            raise BrevoError("Jinja2 is not available; install jinja2 or avoid send_template().")
        html = _jinja.get_template(template_html).render(**(vars or {}))
        text = _jinja.get_template(template_txt).render(**(vars or {})) if template_txt else None
        return await self.send(to=to, subject=subject, html=html, text=text, **kwargs)
