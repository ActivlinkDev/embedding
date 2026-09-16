"""When an engineer can come.

Today there is no availability system: the only rule is that the earliest appointment is
one day ahead. That is three lines of logic. **The response shape is the real deliverable
here** — it is designed so that plugging in a real provider later is a change of values,
not a change of schema, and so the frontend needs no edit when that happens.

Three properties do that work:

* **`slotId` is opaque.** Callers echo it back to book and never parse it. A provider that
  returns ``acme:eng-4471:2026-09-15T08:00`` needs no frontend change; one that returns
  ``static:2026-09-15`` works the same way today.
* **Day granularity is not baked in.** ``date`` plus nullable ``start``, ``end`` and
  ``windowLabel`` describes both "a day" and "08:00-12:00 with engineer 4471".
* **Unavailable days can be returned.** Only bookable dates are emitted today, but
  ``available: false`` is part of the contract so a real provider can grey out dates in
  the picker rather than omitting them.

Every forward-looking field is present and explicitly nullable now. ``capacityRemaining:
null`` means *unknown*, not zero.

`is_bookable` is the single authority on whether a date may be booked, and the appointment
endpoint imports it rather than repeating the rule — otherwise the picker and the booking
check would eventually disagree, and a customer would be shown a date the server rejects.
"""

import os
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional, Protocol

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from utils.api_docs import json_response, secured
from utils.dependencies import verify_token

router = APIRouter(prefix="/service-requests", tags=["Service"])

STATIC_PROVIDER = "static"
SLOT_PREFIX = f"{STATIC_PROVIDER}:"
DEFAULT_TIMEZONE = os.getenv("SERVICE_DEFAULT_TIMEZONE", "Europe/London")
DEFAULT_WINDOW_DAYS = 28

# How far ahead the earliest appointment is. One day, until a real provider says otherwise.
LEAD_TIME_DAYS = 1


class AvailabilityRequest(BaseModel):
    clientkey: str = Field(min_length=1)
    # Ignored by the static provider, accepted now so callers can send them from day one:
    # real availability depends on the fault, the device and the address.
    serviceRequestId: Optional[str] = None
    deviceId: Optional[str] = None
    category: Optional[str] = Field(default=None, max_length=200)
    postcode: Optional[str] = Field(default=None, max_length=16)
    from_date: Optional[str] = Field(default=None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$")
    to_date: Optional[str] = Field(default=None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$")
    days: int = Field(default=DEFAULT_WINDOW_DAYS, ge=1, le=90)
    timezone: Optional[str] = Field(default=None, max_length=64)

    model_config = {"populate_by_name": True}


def today() -> date:
    return datetime.now(timezone.utc).date()


def earliest_bookable_date() -> date:
    return today() + timedelta(days=LEAD_TIME_DAYS)


def is_bookable(value) -> bool:
    """Whether ``value`` (a date or a YYYY-MM-DD string) may be booked.

    The one rule the server must not take the browser's word for: a picker left open
    overnight keeps offering a date that has since become today.
    """
    if isinstance(value, str):
        try:
            value = date.fromisoformat(value)
        except ValueError:
            return False
    if not isinstance(value, date):
        return False
    return value >= earliest_bookable_date()


def slot_id_for(day: date) -> str:
    return f"{SLOT_PREFIX}{day.isoformat()}"


def resolve_slot_date(slot_id: Optional[str]) -> Optional[str]:
    """The YYYY-MM-DD a slot id refers to, or None if it is not one of ours.

    A real provider replaces this with a lookup. Returning None rather than guessing means
    an unrecognised id is a 400 telling the caller to re-fetch, not a silent mis-booking.
    """
    if not isinstance(slot_id, str) or not slot_id.startswith(SLOT_PREFIX):
        return None
    candidate = slot_id[len(SLOT_PREFIX):]
    try:
        return date.fromisoformat(candidate).isoformat()
    except ValueError:
        return None


def _slot(day: date) -> dict:
    return {
        "slotId": slot_id_for(day),
        "date": day.isoformat(),
        "start": None,
        "end": None,
        "windowLabel": None,
        "available": True,
        "capacityRemaining": None,
        "price": None,
        "holdExpiresAt": None,
    }


class AvailabilityProvider(Protocol):
    """What a real availability integration has to supply."""

    name: str

    def earliest_bookable_date(self) -> date: ...

    def slots(self, req: AvailabilityRequest) -> List[dict]: ...


class StaticAvailabilityProvider:
    """Every date from tomorrow, for the requested window. Touches no database.

    Deliberately ignores the fault, device and postcode on the request: there is nothing
    to vary on yet, and pretending otherwise would suggest a capability that does not
    exist.
    """

    name = STATIC_PROVIDER

    def earliest_bookable_date(self) -> date:
        return earliest_bookable_date()

    def slots(self, req: AvailabilityRequest) -> List[dict]:
        start = self.earliest_bookable_date()
        if req.from_date:
            requested = date.fromisoformat(req.from_date)
            # Clamp up, never down: a `from` in the past must not yield unbookable dates.
            start = max(start, requested)

        end = start + timedelta(days=req.days - 1)
        if req.to_date:
            end = min(end, date.fromisoformat(req.to_date))

        count = (end - start).days + 1
        return [_slot(start + timedelta(days=offset)) for offset in range(max(count, 0))]


_PROVIDERS = {STATIC_PROVIDER: StaticAvailabilityProvider}


def get_provider() -> AvailabilityProvider:
    """The configured provider. Swapping it is configuration, not a router change."""
    name = os.getenv("SERVICE_AVAILABILITY_PROVIDER", STATIC_PROVIDER)
    return _PROVIDERS.get(name, StaticAvailabilityProvider)()


@router.post(
    "/availability",
    summary="Appointment dates an engineer could attend",
    response_description="The bookable slots, earliest first.",
    responses=secured({
        200: json_response("Availability for the requested window.", {
            "provider": "static",
            "timezone": "Europe/London",
            "generatedAt": "2026-09-15T09:12:00+00:00",
            "earliestBookableDate": "2026-09-16",
            "slots": [{
                "slotId": "static:2026-09-16", "date": "2026-09-16",
                "start": None, "end": None, "windowLabel": None,
                "available": True, "capacityRemaining": None, "price": None, "holdExpiresAt": None,
            }],
            "nextCursor": None,
        }),
    }),
)
def availability(body: AvailabilityRequest, _: None = Depends(verify_token)):
    """
    The dates a customer may pick for their repair.

    **Book with `slotId`, never with a date you rebuilt yourself.** The id is opaque — its
    current `static:YYYY-MM-DD` shape is an implementation detail that will change when
    real engineer availability arrives, and callers that parse it will break then.

    Today every date from tomorrow onward is offered: there is no capacity system, so
    `capacityRemaining` is `null` (unknown, not zero) and `start`/`end` are `null` because
    slots are whole days. A `from` in the past is clamped forward rather than honoured.

    This reads no customer data and cannot fail on it, so an availability outage never
    blocks a booking — but the date is re-checked when the appointment is actually made.
    """
    provider = get_provider()
    return {
        "provider": provider.name,
        "timezone": body.timezone or DEFAULT_TIMEZONE,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "earliestBookableDate": provider.earliest_bookable_date().isoformat(),
        "slots": provider.slots(body),
        "nextCursor": None,
    }
