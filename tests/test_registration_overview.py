import base64
from unittest.mock import MagicMock

import pytest
from bson import ObjectId, Binary
from fastapi import HTTPException
from routers import registration_overview as route


def receipt(data=b'%PDF-1.7\nreceipt', content_type='application/pdf'):
    return route.Receipt(name='receipt.pdf', contentType=content_type, data=base64.b64encode(data).decode())


@pytest.mark.parametrize('data,content_type,status', [
    (b'<script>bad</script>', 'image/jpeg', 415),
    (b'', 'application/pdf', 413),
    (b'%PDF-' + b'x' * (5 * 1024 * 1024 - 4), 'application/pdf', 413),
], ids=['invalid-type', 'empty-file', 'oversized-file'])
def test_invalid_upload_rejected(data, content_type, status):
    with pytest.raises(HTTPException) as error:
        route.decode_receipt(receipt(data, content_type))
    assert error.value.status_code == status


def test_receipt_saved_on_exact_device_without_returning_binary(monkeypatch):
    device_id = str(ObjectId())
    db = MagicMock()
    db.count_documents.return_value = 1
    db.update_one.return_value.matched_count = 1
    db.find.return_value = [{'_id': ObjectId(device_id), 'receipt': {'name': 'receipt.pdf'}}]
    monkeypatch.setattr(route, 'devices_collection', db)
    result = route.registration_overview(route.OverviewRequest(
        device_ids=[device_id], phone='+447700900123', device_id=device_id, receipt=receipt()))
    query, update = db.update_one.call_args.args
    assert query['_id'] == ObjectId(device_id)
    assert query['$or'][0] == {'registrationPhone': '+447700900123'}
    assert isinstance(update['$set']['receipt']['data'], Binary)
    assert update['$set']['receipt']['size'] == len(b'%PDF-1.7\nreceipt')
    assert db.find.call_args.args[1] == {'receipt.data': 0}
    assert result['devices'][0]['receipt']['name'] == 'receipt.pdf'


def test_other_phone_cannot_read_or_replace_receipt(monkeypatch):
    db = MagicMock()
    db.count_documents.return_value = 0
    monkeypatch.setattr(route, 'devices_collection', db)
    device_id = str(ObjectId())
    with pytest.raises(HTTPException) as error:
        route.registration_overview(route.OverviewRequest(
            device_ids=[device_id], phone='+447700900123', device_id=device_id, receipt=receipt()))
    assert error.value.status_code == 403
    db.update_one.assert_not_called()
    db.update_many.assert_not_called()


def test_cannot_upload_to_device_outside_registration_session(monkeypatch):
    db = MagicMock()
    db.count_documents.return_value = 1
    monkeypatch.setattr(route, 'devices_collection', db)
    with pytest.raises(HTTPException) as error:
        route.registration_overview(route.OverviewRequest(
            device_ids=[str(ObjectId())], phone='+447700900123', device_id=str(ObjectId()), receipt=receipt()))
    assert error.value.status_code == 403
    db.update_one.assert_not_called()
