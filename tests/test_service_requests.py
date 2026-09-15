import base64
from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest
from bson import Binary, ObjectId
from fastapi import HTTPException

from routers.service_requests import availability as avail
from routers.service_requests import media as media_route
from routers.service_requests import service as svc
from routers.service_requests import (
    AddMediaRequest,
    CreateServiceRequest,
    FaultReport,
    SetAppointmentRequest,
    add_media,
    create_service_request,
    decode_fault_photo,
    resolve_fault,
    set_appointment,
)

JPEG = b"\xff\xd8\xff" + b"x" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 64
OWNER = "AO"
OTHER_TENANT = "ARG"


def photo(data=JPEG, content_type="image/jpeg", name="fault.jpg"):
    return {"name": name, "contentType": content_type, "data": base64.b64encode(data).decode()}


def tomorrow() -> str:
    return (date.today() + timedelta(days=1)).isoformat()


@pytest.fixture
def db(monkeypatch):
    """Mongo handles replaced with mocks, and the tenant lookup pinned to one owner."""
    requests, media, devices = MagicMock(), MagicMock(), MagicMock()
    requests.insert_one.return_value.inserted_id = ObjectId()
    media.insert_one.return_value.inserted_id = ObjectId()
    cursor = MagicMock()
    cursor.sort.return_value = []
    media.find.return_value = cursor
    for module in (svc, media_route):
        monkeypatch.setattr(module, "service_requests_collection", requests, raising=False)
        monkeypatch.setattr(module, "media_collection", media, raising=False)
    import routers.service_requests as package
    monkeypatch.setattr(package, "service_requests_collection", requests)
    monkeypatch.setattr(package, "media_collection", media)
    monkeypatch.setattr(svc, "devices_collection", devices)
    monkeypatch.setattr(svc, "client_id_for_key", lambda key: OWNER if key == "AOPON12345" else None)
    monkeypatch.setattr(svc, "next_reference", lambda: "SRV-2026-000001")
    monkeypatch.setattr(package, "next_reference", lambda: "SRV-2026-000001")
    return {"requests": requests, "media": media, "devices": devices}


def own_device(db, category="Dishwasher"):
    db["devices"].find_one.return_value = {
        "_id": ObjectId(), "client": OWNER, "identifiers": {"category": category}}


# --- photo validation -------------------------------------------------------------------

@pytest.mark.parametrize("data,content_type,status", [
    (b"<script>bad</script>", "image/jpeg", 415),   # a .txt renamed to .jpg
    (b"", "image/png", 413),
    (b"%PDF-1.7 receipt", "application/pdf", 415),  # fine as a receipt, not as a fault photo
    (b"\x00\x00\x00\x18ftypmp42", "video/mp4", 415),
], ids=["wrong-magic-bytes", "empty", "pdf-rejected", "video-rejected"])
def test_invalid_fault_photo_rejected(data, content_type, status):
    from routers.service_requests import MediaUpload
    with pytest.raises(HTTPException) as error:
        decode_fault_photo(MediaUpload(**photo(data, content_type)))
    assert error.value.status_code == status


def test_an_oversized_photo_is_refused_by_the_model_before_it_is_decoded():
    # The base64 cap and the 5 MB byte cap are the same boundary, so a huge upload is
    # rejected at validation and never allocates megabytes to decode.
    from routers.service_requests import MediaUpload
    with pytest.raises(ValueError):
        MediaUpload(**photo(JPEG + b"x" * (5 * 1024 * 1024), "image/jpeg"))


def test_the_decoder_caps_bytes_independently_of_the_model():
    # Defence in depth: any future caller that skips MediaUpload still cannot store more
    # than the cap.
    from utils.media import IMAGE_TYPES, decode_upload
    with pytest.raises(HTTPException) as error:
        decode_upload("f.jpg", "image/jpeg",
                      base64.b64encode(JPEG + b"x" * 1024).decode(), IMAGE_TYPES, max_bytes=64)
    assert error.value.status_code == 413


def test_valid_photo_is_stored_as_binary_with_its_real_size():
    from routers.service_requests import MediaUpload
    stored = decode_fault_photo(MediaUpload(**photo(PNG, "image/png")))
    assert isinstance(stored["data"], Binary)
    assert stored["size"] == len(PNG)


# --- tenancy ----------------------------------------------------------------------------

def test_unknown_clientkey_is_rejected_before_any_query(db):
    with pytest.raises(HTTPException) as error:
        create_service_request(CreateServiceRequest(
            clientkey="not-a-key", deviceId=str(ObjectId()), locale="en_GB",
            fault=FaultReport(freeText="It hums")))
    assert error.value.status_code == 400
    db["devices"].find_one.assert_not_called()
    db["requests"].insert_one.assert_not_called()


def test_device_owned_by_another_tenant_is_404_and_writes_nothing(db):
    # The device exists, but not for this client, so the filtered query misses. A 403 here
    # would confirm the id is real and let one tenant enumerate another's devices.
    db["devices"].find_one.return_value = None
    with pytest.raises(HTTPException) as error:
        create_service_request(CreateServiceRequest(
            clientkey="AOPON12345", deviceId=str(ObjectId()), locale="en_GB",
            fault=FaultReport(freeText="It hums")))
    assert error.value.status_code == 404
    assert db["devices"].find_one.call_args.args[0]["client"] == OWNER
    db["requests"].insert_one.assert_not_called()


def test_every_service_request_read_is_filtered_by_tenant(db):
    db["requests"].find_one.return_value = None
    with pytest.raises(HTTPException) as error:
        svc.owned_request(str(ObjectId()), OWNER)
    assert error.value.status_code == 404
    assert db["requests"].find_one.call_args.args[0]["client"] == OWNER


def test_media_fetch_is_filtered_by_request_and_tenant_together(db):
    db["requests"].find_one.return_value = {"_id": ObjectId(), "client": OWNER}
    db["media"].find_one.return_value = None
    with pytest.raises(HTTPException) as error:
        media_route.fetch_media(media_route.FetchMediaRequest(
            clientkey="AOPON12345", serviceRequestId=str(ObjectId()), mediaId=str(ObjectId())))
    assert error.value.status_code == 404
    query = db["media"].find_one.call_args.args[0]
    assert query["client"] == OWNER and "serviceRequestId" in query


def test_media_with_an_unservable_content_type_is_never_echoed_into_a_header(db):
    db["requests"].find_one.return_value = {"_id": ObjectId(), "client": OWNER}
    db["media"].find_one.return_value = {
        "_id": ObjectId(), "contentType": "text/html", "data": Binary(b"<script>"), "name": "x"}
    with pytest.raises(HTTPException) as error:
        media_route.fetch_media(media_route.FetchMediaRequest(
            clientkey="AOPON12345", serviceRequestId=str(ObjectId()), mediaId=str(ObjectId())))
    assert error.value.status_code == 404


def test_malformed_device_id_is_400_not_a_collection_scan(db):
    with pytest.raises(HTTPException) as error:
        create_service_request(CreateServiceRequest(
            clientkey="AOPON12345", deviceId="not-an-objectid", locale="en_GB",
            fault=FaultReport(freeText="It hums")))
    assert error.value.status_code == 400


# --- creating ---------------------------------------------------------------------------

def test_created_request_snapshots_the_catalogue_text_and_is_scoped(db, monkeypatch):
    own_device(db)
    import routers.service_requests as package
    catalogue = MagicMock()
    catalogue.find_one.return_value = {"Category": "Dishwasher", "Content": [{
        "locale": "en_GB", "FaultTypes": [{
            "typeId": "leak_water", "typeLabel": "Leaking or water damage",
            "faults": [{"faultId": "leak_water-01", "Issue": "Water leaking from the door seal",
                        "Description": "d", "Solution": "s"}]}]}]}
    monkeypatch.setattr(package, "catalogue_collection", catalogue)

    result = create_service_request(CreateServiceRequest(
        clientkey="AOPON12345", deviceId=str(ObjectId()), locale="en_GB",
        fault=FaultReport(faultId="leak_water-01", freeText="Worse on a hot wash")))

    written = db["requests"].insert_one.call_args.args[0]
    assert written["client"] == OWNER
    assert written["status"] == svc.REPORTED
    assert written["fault"]["selectedIssue"] == "Water leaking from the door seal"
    assert written["fault"]["verified"] is True
    assert written["fault"]["source"] == "both"
    assert result["reference"] == "SRV-2026-000001"


def test_a_faultid_the_catalogue_no_longer_carries_is_stored_unverified(db, monkeypatch):
    # A stale tab, an unseeded category, or a regenerated catalogue. The report is real
    # either way — dropping it because a lookup missed would be worse than storing it.
    own_device(db)
    import routers.service_requests as package
    catalogue = MagicMock()
    catalogue.find_one.return_value = None
    monkeypatch.setattr(package, "catalogue_collection", catalogue)

    create_service_request(CreateServiceRequest(
        clientkey="AOPON12345", deviceId=str(ObjectId()), locale="en_GB",
        fault=FaultReport(faultId="gone-99", freeText="It hums then stops")))

    fault = db["requests"].insert_one.call_args.args[0]["fault"]
    assert fault["verified"] is False
    assert fault["freeText"] == "It hums then stops"


def test_caller_cannot_forge_the_fault_wording():
    # FaultReport has no Issue/Description/Solution fields at all, so wording can only ever
    # come from the catalogue. Pydantic ignores the extras rather than storing them.
    report = FaultReport(faultId="leak_water-01", Issue="Free repair please", Solution="Replace it")
    assert not hasattr(report, "Issue")


def test_a_fault_report_with_neither_an_id_nor_text_is_rejected():
    with pytest.raises(ValueError):
        FaultReport()
    with pytest.raises(ValueError):
        FaultReport(freeText="   ")


def test_an_invented_fault_type_is_rejected_at_the_edge():
    with pytest.raises(ValueError):
        FaultReport(typeId="catastrophic_gremlins", freeText="x")


def test_a_rejected_photo_leaves_no_service_request_behind(db):
    own_device(db)
    with pytest.raises(HTTPException) as error:
        create_service_request(CreateServiceRequest(
            clientkey="AOPON12345", deviceId=str(ObjectId()), locale="en_GB",
            fault=FaultReport(freeText="It hums"), media=photo(b"nope", "image/jpeg")))
    assert error.value.status_code == 415
    db["requests"].insert_one.assert_not_called()


def test_a_fifth_photo_is_refused(db):
    db["requests"].find_one.return_value = {
        "_id": ObjectId(), "client": OWNER, "mediaCount": svc.MAX_MEDIA_PER_REQUEST}
    with pytest.raises(HTTPException) as error:
        add_media(AddMediaRequest(
            clientkey="AOPON12345", serviceRequestId=str(ObjectId()), media=photo()))
    assert error.value.status_code == 409
    db["media"].insert_one.assert_not_called()


# --- appointments -----------------------------------------------------------------------

def scheduled_request(db, status=svc.REPORTED, appointment=None):
    doc = {"_id": ObjectId(), "client": OWNER, "status": status, "appointment": appointment}
    db["requests"].find_one.return_value = doc
    db["requests"].find_one_and_update.return_value = {**doc, "status": svc.SCHEDULED}
    return doc


@pytest.mark.parametrize("day,status", [
    (date.today().isoformat(), 409),
    ((date.today() - timedelta(days=30)).isoformat(), 409),
], ids=["today", "past"])
def test_a_date_before_tomorrow_is_refused(db, day, status):
    # The picker may still be offering it — a tab left open overnight does exactly that —
    # so the server re-checks rather than trusting the browser's arithmetic.
    scheduled_request(db)
    with pytest.raises(HTTPException) as error:
        set_appointment(SetAppointmentRequest(
            clientkey="AOPON12345", serviceRequestId=str(ObjectId()), date=day))
    assert error.value.status_code == status


def test_tomorrow_is_accepted_and_recorded(db):
    scheduled_request(db)
    set_appointment(SetAppointmentRequest(
        clientkey="AOPON12345", serviceRequestId=str(ObjectId()), date=tomorrow()))
    update = db["requests"].find_one_and_update.call_args.args[1]
    assert update["$set"]["appointment"]["date"] == tomorrow()
    assert update["$set"]["status"] == svc.SCHEDULED


def test_an_unknown_slot_id_is_400_rather_than_a_guessed_date(db):
    scheduled_request(db)
    with pytest.raises(HTTPException) as error:
        set_appointment(SetAppointmentRequest(
            clientkey="AOPON12345", serviceRequestId=str(ObjectId()), slotId="acme:whatever"))
    assert error.value.status_code == 400


def test_rescheduling_keeps_the_previous_appointment(db):
    previous = {"date": tomorrow(), "slotId": "static:" + tomorrow()}
    scheduled_request(db, status=svc.SCHEDULED, appointment=previous)
    later = (date.today() + timedelta(days=5)).isoformat()
    set_appointment(SetAppointmentRequest(
        clientkey="AOPON12345", serviceRequestId=str(ObjectId()), date=later))
    update = db["requests"].find_one_and_update.call_args.args[1]
    assert update["$push"]["appointmentHistory"] == previous


@pytest.mark.parametrize("status", [svc.COMPLETED, svc.CANCELLED])
def test_a_finished_request_can_no_longer_be_scheduled(db, status):
    scheduled_request(db, status=status)
    with pytest.raises(HTTPException) as error:
        set_appointment(SetAppointmentRequest(
            clientkey="AOPON12345", serviceRequestId=str(ObjectId()), date=tomorrow()))
    assert error.value.status_code == 409


def test_supplying_both_or_neither_slot_and_date_is_rejected():
    for kwargs in ({}, {"slotId": "static:x", "date": tomorrow()}):
        with pytest.raises(ValueError):
            SetAppointmentRequest(clientkey="k", serviceRequestId=str(ObjectId()), **kwargs)


# --- availability -----------------------------------------------------------------------

@pytest.mark.parametrize("from_date", [
    "2020-01-01", (date.today() - timedelta(days=1)).isoformat(), date.today().isoformat(), None,
], ids=["long-past", "yesterday", "today", "unset"])
def test_no_offered_date_is_ever_earlier_than_tomorrow(from_date):
    body = avail.AvailabilityRequest(clientkey="k", **({"from": from_date} if from_date else {}))
    slots = avail.StaticAvailabilityProvider().slots(body)
    assert slots, "the window must not come back empty"
    assert min(slot["date"] for slot in slots) >= tomorrow()
    assert all(avail.is_bookable(slot["date"]) for slot in slots)


def test_the_window_honours_days_and_a_to_date():
    assert len(avail.StaticAvailabilityProvider().slots(
        avail.AvailabilityRequest(clientkey="k", days=5))) == 5
    capped = avail.StaticAvailabilityProvider().slots(
        avail.AvailabilityRequest(clientkey="k", days=30, **{"to": tomorrow()}))
    assert [slot["date"] for slot in capped] == [tomorrow()]


def test_a_to_date_before_the_earliest_yields_no_slots_rather_than_an_error():
    assert avail.StaticAvailabilityProvider().slots(
        avail.AvailabilityRequest(clientkey="k", **{"to": "2020-01-01"})) == []


def test_slot_ids_round_trip_and_reject_anything_else():
    day = date.today() + timedelta(days=3)
    assert avail.resolve_slot_date(avail.slot_id_for(day)) == day.isoformat()
    for bad in ("", None, "static:", "static:not-a-date", "other:2026-01-01", "2026-01-01"):
        assert avail.resolve_slot_date(bad) is None


def test_every_forward_looking_slot_field_is_present_and_nullable():
    # The shape is the contract a real provider has to fill; a field missing today is a
    # breaking change the day it appears.
    slot = avail.StaticAvailabilityProvider().slots(avail.AvailabilityRequest(clientkey="k", days=1))[0]
    assert set(slot) == {"slotId", "date", "start", "end", "windowLabel",
                         "available", "capacityRemaining", "price", "holdExpiresAt"}
    assert slot["capacityRemaining"] is None and slot["available"] is True


# --- the checkout hook ------------------------------------------------------------------

def test_contract_issuance_confirms_the_service_request(db):
    db["requests"].update_one.return_value.matched_count = 1
    assert svc.attach_contract_to_service_request(str(ObjectId()), "ACT-2026-01", "ORD-2026-01", "b1")
    query, update = db["requests"].update_one.call_args.args
    assert update["$set"]["status"] == svc.CONFIRMED
    assert update["$set"]["contract_reference"] == "ACT-2026-01"
    assert query["status"]["$nin"] == [svc.COMPLETED, svc.CANCELLED]


@pytest.mark.parametrize("value", [None, "", "not-an-objectid"])
def test_a_basket_line_with_no_service_request_is_a_quiet_no_op(db, value):
    # The ordinary cover journey carries no service request; that must not raise inside a
    # paid checkout.
    assert svc.attach_contract_to_service_request(value, "ACT-1", "ORD-1", "b1") is False
    db["requests"].update_one.assert_not_called()
