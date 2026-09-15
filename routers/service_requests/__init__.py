"""Create, read and schedule repair service requests.

The customer journey behind these endpoints: find the device, say when it was bought,
report what is wrong with it, get priced options, pick an appointment date, pay.

ORDERING
--------
A service request is created **after** the device is registered, not before. Its tenancy
runs through `Devices.client` (see `service.py`), so a request with no device could not be
scoped to a tenant at all. The frontend therefore holds the fault selection in browser
state until `/device-register` returns a deviceId, then posts it here.

WHAT THE CALLER MAY SET
-----------------------
Ids only. `Issue`, `Description`, `Solution` and the type label are resolved out of
`FaultCatalogue` server-side and stored as a snapshot, so the stored fault cannot be
forged and stays readable even after the catalogue is regenerated.
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field, model_validator

from utils.api_docs import error, json_response, secured
from utils.dependencies import verify_token
from utils.media import IMAGE_TYPES, decode_upload

from ..faults.catalogue import catalogue_collection, find_locale_content
from ..faults.fault_types import is_known_type, type_label
from .availability import is_bookable, resolve_slot_date
from .service import (
    MAX_MEDIA_PER_REQUEST,
    REPORTED,
    SCHEDULED,
    can_transition,
    event,
    media_collection,
    next_reference,
    now,
    object_id,
    owned_device,
    owned_request,
    resolve_client_id,
    serialize_media,
    serialize_request,
    service_requests_collection,
)

router = APIRouter(prefix="/service-requests", tags=["Service"])

FREE_TEXT_MAX = 2000


class MediaUpload(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    contentType: str
    # Same cap as the receipt upload: 5 MB of bytes is ~6.99 MB of base64.
    data: str = Field(max_length=6990508)


class FaultReport(BaseModel):
    """What the customer picked, by id, plus anything they typed."""

    typeId: Optional[str] = Field(default=None, max_length=64, description="Fault type from the fixed vocabulary.")
    faultId: Optional[str] = Field(default=None, max_length=64, description="Fault within that type, from `POST /faults/catalogue`.")
    freeText: Optional[str] = Field(default=None, max_length=FREE_TEXT_MAX, description="The customer's own description.")

    @model_validator(mode="after")
    def at_least_one(self):
        if not (self.faultId or (self.freeText or "").strip()):
            raise ValueError("Report a fault from the catalogue, a description, or both.")
        if self.typeId and not is_known_type(self.typeId):
            raise ValueError("Unknown fault type.")
        return self


class ContactDetails(BaseModel):
    name: Optional[str] = Field(default=None, max_length=200)
    email: Optional[EmailStr] = None
    phone: Optional[str] = Field(default=None, pattern=r"^\+?[1-9]\d{6,14}$")
    addressLine1: Optional[str] = Field(default=None, max_length=200)
    addressLine2: Optional[str] = Field(default=None, max_length=200)
    city: Optional[str] = Field(default=None, max_length=120)
    postcode: Optional[str] = Field(default=None, max_length=16)


class CreateServiceRequest(BaseModel):
    clientkey: str = Field(min_length=1)
    deviceId: str = Field(min_length=1)
    locale: str = Field(pattern=r"^[a-z]{2}_[A-Z]{2}$")
    source: Optional[str] = Field(default="web", max_length=32)
    fault: FaultReport
    media: Optional[MediaUpload] = None
    quote_id: Optional[str] = None


class AddMediaRequest(BaseModel):
    clientkey: str = Field(min_length=1)
    serviceRequestId: str = Field(min_length=1)
    media: MediaUpload


class GetServiceRequest(BaseModel):
    clientkey: str = Field(min_length=1)
    serviceRequestId: Optional[str] = None
    deviceId: Optional[str] = None

    @model_validator(mode="after")
    def exactly_one(self):
        if bool(self.serviceRequestId) == bool(self.deviceId):
            raise ValueError("Supply exactly one of serviceRequestId or deviceId.")
        return self


class SetAppointmentRequest(BaseModel):
    clientkey: str = Field(min_length=1)
    serviceRequestId: str = Field(min_length=1)
    slotId: Optional[str] = Field(default=None, max_length=200)
    date: Optional[str] = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    notes: Optional[str] = Field(default=None, max_length=1000)
    contact: Optional[ContactDetails] = None

    @model_validator(mode="after")
    def exactly_one(self):
        if bool(self.slotId) == bool(self.date):
            raise ValueError("Supply exactly one of slotId or date.")
        return self


def resolve_fault(category: str, locale: str, report: FaultReport) -> dict:
    """Snapshot the reported fault from the catalogue.

    Never raises. A `faultId` the catalogue does not carry — an unseeded category, a stale
    browser tab, a catalogue regenerated since the page loaded — stores what the customer
    typed with `verified: False`. An unverified fault is still a real fault report, and
    losing it because a lookup missed would be worse than storing it unmatched.
    """
    free_text = (report.freeText or "").strip() or None
    snapshot = {
        "typeId": report.typeId,
        "typeLabel": type_label(report.typeId) if report.typeId else None,
        "faultId": report.faultId,
        "selectedIssue": None,
        "selectedDescription": None,
        "selectedSolution": None,
        "freeText": free_text,
        "verified": False,
        "faultsLocale": locale,
    }
    if not report.faultId:
        snapshot["source"] = "free_text"
        return snapshot

    content = find_locale_content(catalogue_collection.find_one({"Category": category}), locale)
    for group in (content or {}).get("FaultTypes", []) or []:
        for entry in group.get("faults", []) or []:
            if entry.get("faultId") != report.faultId:
                continue
            snapshot.update({
                "typeId": group.get("typeId"),
                "typeLabel": group.get("typeLabel"),
                "selectedIssue": entry.get("Issue"),
                "selectedDescription": entry.get("Description"),
                "selectedSolution": entry.get("Solution"),
                "verified": True,
            })
            snapshot["source"] = "both" if free_text else "list"
            return snapshot

    snapshot["source"] = "both" if free_text else "list"
    return snapshot


def decode_fault_photo(media: MediaUpload) -> dict:
    """Validate a fault photo. Images only — a PDF is a valid receipt, not a fault photo."""
    return decode_upload(
        media.name, media.contentType, media.data, IMAGE_TYPES,
        invalid_message="Invalid photo file",
        too_large_message="Photo must be 5 MB or smaller.",
        wrong_type_message="Choose a JPEG, PNG or HEIC photo.",
    )


@router.post(
    "",
    status_code=201,
    summary="Report a fault on a device and open a service request",
    response_description="The service request, with any attached photo's metadata.",
    responses=secured({
        201: json_response("The service request was opened.", {
            "serviceRequestId": "68b2d1f0a4b21d0f8c9e8801",
            "reference": "SRV-2026-000123",
            "status": "REPORTED",
            "deviceId": "6820f1c9a4b21d0f8c9e4471",
            "category": "Dishwasher",
            "fault": {"typeId": "leak_water", "typeLabel": "Leaking or water damage",
                      "faultId": "leak_water-01", "selectedIssue": "Water leaking from the door seal",
                      "freeText": "Worse on a hot wash", "verified": True, "source": "both"},
            "media": [], "mediaCount": 0,
        }),
        400: error("The ClientKey is unknown, or an id is malformed.", "Invalid clientkey."),
        404: error("No device with that id belongs to this client.", "Device not found"),
        413: error("The photo is empty or larger than 5 MB.", "Photo must be 5 MB or smaller."),
        415: error("The bytes are not the image type the request declared.", "Choose a JPEG, PNG or HEIC photo."),
    }),
)
def create_service_request(body: CreateServiceRequest, _: None = Depends(verify_token)):
    """
    Open a service request for a fault the customer has reported on one of their devices.

    **Register the device first.** The request is scoped to its tenant through the device,
    so the device has to exist and belong to this client — a device id belonging to anyone
    else returns `404`, the same as one that does not exist.

    **Send ids, not text.** `fault.faultId` and `fault.typeId` come from
    `POST /faults/catalogue`; the wording is looked up here and stored as a snapshot. A
    `faultId` that no longer resolves is not an error — the report is stored with
    `verified: false` alongside whatever the customer typed.

    A photo may be attached now or later with `POST /service-requests/media`.
    """
    client_id = resolve_client_id(body.clientkey)
    device = owned_device(body.deviceId, client_id)
    category = ((device.get("identifiers") or {}).get("category")) or ""

    # Decode before inserting, so a rejected photo leaves nothing behind.
    photo = decode_fault_photo(body.media) if body.media else None

    timestamp = now()
    doc = {
        "reference": next_reference(),
        "client": client_id,
        "client_key": body.clientkey,
        "deviceId": body.deviceId,
        "locale": body.locale,
        "source": body.source or "web",
        "category": category,
        "status": REPORTED,
        "fault": resolve_fault(category, body.locale, body.fault),
        "mediaCount": 0,
        "appointment": None,
        "appointmentHistory": [],
        "quote_id": body.quote_id,
        "basket_id": None,
        "contract_reference": None,
        "contact": None,
        "events": [event("REPORTED")],
        "createdAt": timestamp,
        "updatedAt": timestamp,
    }
    result = service_requests_collection.insert_one(doc)
    doc["_id"] = result.inserted_id

    if photo:
        media_collection.insert_one({
            "serviceRequestId": result.inserted_id,
            "client": client_id,
            **photo,
        })
        service_requests_collection.update_one(
            {"_id": result.inserted_id, "client": client_id},
            {"$set": {"mediaCount": 1, "updatedAt": now()}},
        )
        doc["mediaCount"] = 1

    return serialize_request(doc)


@router.post(
    "/media",
    summary="Attach a photo of the fault",
    response_description="Metadata for the stored photo. The bytes are never returned here.",
    responses=secured({
        200: json_response("The photo was stored.", {
            "serviceRequestId": "68b2d1f0a4b21d0f8c9e8801", "mediaCount": 2,
            "media": {"mediaId": "68b2d1f0a4b21d0f8c9e8899", "name": "fault.jpg",
                      "contentType": "image/jpeg", "size": 482113, "uploadedAt": "2026-09-15T09:12:00+00:00"},
        }),
        404: error("No service request with that id belongs to this client.", "Service request not found"),
        409: error("The request already has the maximum number of photos.", "This service request already has the maximum number of attachments."),
        413: error("The photo is empty or larger than 5 MB.", "Photo must be 5 MB or smaller."),
        415: error("The bytes are not the image type the request declared.", "Choose a JPEG, PNG or HEIC photo."),
    }),
)
def add_media(body: AddMediaRequest, _: None = Depends(verify_token)):
    """
    Attach a photo to an open service request — up to four in total.

    Photos are validated by their **magic bytes**, not their name or declared type, so a
    file renamed to `.jpg` is rejected. Images only: a PDF is a valid proof of purchase
    and never a valid photo of a fault.
    """
    client_id = resolve_client_id(body.clientkey)
    doc = owned_request(body.serviceRequestId, client_id)

    if doc.get("mediaCount", 0) >= MAX_MEDIA_PER_REQUEST:
        raise HTTPException(409, "This service request already has the maximum number of attachments.")

    photo = decode_fault_photo(body.media)
    inserted = media_collection.insert_one({
        "serviceRequestId": doc["_id"], "client": client_id, **photo})
    updated = service_requests_collection.find_one_and_update(
        {"_id": doc["_id"], "client": client_id},
        {"$inc": {"mediaCount": 1},
         "$set": {"updatedAt": now()},
         "$push": {"events": event("MEDIA_ADDED", {"mediaId": str(inserted.inserted_id)})}},
        return_document=True,
    )
    return {
        "serviceRequestId": str(doc["_id"]),
        "mediaCount": (updated or {}).get("mediaCount", doc.get("mediaCount", 0) + 1),
        "media": serialize_media({"_id": inserted.inserted_id, **photo}),
    }


@router.post(
    "/get",
    summary="Read a service request back",
    response_description="The service request, or this device's service requests.",
    responses=secured({
        200: json_response("Found.", {"serviceRequests": [{"serviceRequestId": "68b2d1f0a4b21d0f8c9e8801", "status": "SCHEDULED"}]}),
        404: error("Nothing with that id belongs to this client.", "Service request not found"),
    }),
)
def get_service_request(body: GetServiceRequest, _: None = Depends(verify_token)):
    """
    Read a service request by its id, or list the ones opened against a device.

    The confirmation screen renders after the Stripe redirect, by which point the browser
    has only ids — this is how it gets the fault and appointment back.

    Photo **metadata** only; fetch the bytes with `POST /service-requests/media/fetch`.
    """
    client_id = resolve_client_id(body.clientkey)
    if body.serviceRequestId:
        return {"serviceRequests": [serialize_request(owned_request(body.serviceRequestId, client_id))]}

    object_id(body.deviceId, "deviceId")  # reject a malformed id rather than scanning
    cursor = service_requests_collection.find(
        {"client": client_id, "deviceId": body.deviceId}).sort("createdAt", -1).limit(20)
    return {"serviceRequests": [serialize_request(doc) for doc in cursor]}


@router.post(
    "/appointment",
    summary="Book or move the engineer appointment",
    response_description="The service request, now scheduled.",
    responses=secured({
        200: json_response("Booked.", {"serviceRequests": [], "status": "SCHEDULED"}),
        400: error("The slotId does not resolve to a date.", "Unknown slotId; re-fetch availability."),
        404: error("No service request with that id belongs to this client.", "Service request not found"),
        409: error("The date is no longer offered, or the request can no longer be scheduled.",
                   "That date is no longer available; re-fetch availability."),
    }),
)
def set_appointment(body: SetAppointmentRequest, _: None = Depends(verify_token)):
    """
    Book the appointment, or move an existing one.

    **The date is re-checked here, against the same availability the picker was given.** A
    picker left open overnight still offers yesterday-plus-one, so a date the browser
    believes is valid may not be; `409` means re-fetch and choose again. Booking again
    simply moves the appointment and keeps the previous one in `appointmentHistory`.

    Pass the opaque `slotId` from availability wherever you have one. `date` is the
    fallback for a client that built its own picker.

    `contact.phone` is expected to come from a verified session on the trusted frontend,
    never from anything the browser typed — this endpoint cannot tell the difference.
    """
    client_id = resolve_client_id(body.clientkey)
    doc = owned_request(body.serviceRequestId, client_id)

    if not can_transition(doc.get("status", REPORTED), SCHEDULED):
        raise HTTPException(409, "This service request can no longer be scheduled.")

    booked_date = resolve_slot_date(body.slotId) if body.slotId else body.date
    if not booked_date:
        raise HTTPException(400, "Unknown slotId; re-fetch availability.")
    if not is_bookable(booked_date):
        raise HTTPException(409, "That date is no longer available; re-fetch availability.")

    appointment = {
        "slotId": body.slotId,
        "date": booked_date,
        "start": None,
        "end": None,
        "windowLabel": None,
        "timezone": None,
        "provider": "static",
        "providerBookingRef": None,
        "notes": body.notes,
        "bookedAt": now(),
    }
    update: dict = {
        "$set": {"appointment": appointment, "status": SCHEDULED, "updatedAt": now()},
        "$push": {"events": event("SCHEDULED", {"date": booked_date, "slotId": body.slotId})},
    }
    if body.contact:
        update["$set"]["contact"] = body.contact.model_dump(exclude_none=True)
    if doc.get("appointment"):
        update["$push"]["appointmentHistory"] = doc["appointment"]

    updated = service_requests_collection.find_one_and_update(
        {"_id": doc["_id"], "client": client_id}, update, return_document=True)
    if not updated:
        raise HTTPException(404, "Service request not found")
    return serialize_request(updated)
