"""Registration-session scoped overview and receipt storage (trusted frontend only)."""
import base64
import binascii
import os
import re
from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId, Binary
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from pymongo import MongoClient
from utils.dependencies import verify_token

router = APIRouter(tags=['Devices'])
client = MongoClient(os.getenv('MONGO_URI'))
devices_collection = client['Activlink']['Devices']


class MyRegistrationsRequest(BaseModel):
    phone: str = Field(pattern=r'^\+?[1-9]\d{6,14}$')


@router.post('/my-registrations')
def my_registrations(body: MyRegistrationsRequest, _: None = Depends(verify_token)):
    # Trusted frontend supplies the phone exclusively from its signed OTP cookie.
    # Match formatting differences without matching suffixes or other country codes.
    digits = re.sub(r'\D', '', body.phone)
    pattern = r'^\+?[\s().-]*' + r'[\s().-]*'.join(digits) + r'[\s().-]*$'
    phone_match = {'$regex': pattern}
    db = client['Activlink']
    customers = list(db['Customer'].find(
        {'$or': [{field: phone_match} for field in ('telephone', 'phone', 'mobile')]},
        {'_id': 1}))
    customer_ids = [value for doc in customers for value in (doc['_id'], str(doc['_id']))]
    query = {'$or': [{'registrationPhone': phone_match},
                     {'registrationParameters.customerId': {'$in': customer_ids}}]}
    docs = devices_collection.find(query, {'receipt': 0}).sort('registeredAt', -1)
    devices = []
    for doc in docs:
        master_id = doc.get('masterSkuId')
        master = db['MasterSKU'].find_one({'_id': ObjectId(master_id) if ObjectId.is_valid(str(master_id)) else master_id}) if master_id else None
        master = master or {}
        localized = (master.get('locales') or {}).get(doc.get('locale'), {}) or {}
        image = master.get('imageUrl') or (localized.get('assets') or {}).get('primaryImage')
        parameters = doc.get('registrationParameters') or {}
        devices.append({
            'deviceId': str(doc['_id']), 'identifiers': doc.get('identifiers') or {},
            'uniqueParameters': {'serial': (doc.get('uniqueParameters') or {}).get('serial')},
            'registrationParameters': {key: parameters.get(key) for key in ('purchaseDate', 'price', 'currency')},
            'registeredAt': doc.get('registeredAt'),
            'imageUrl': image if isinstance(image, str) else None,
            'description': localized.get('description') if isinstance(localized.get('description'), str) else None,
        })
    return {'devices': devices}


class Receipt(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    contentType: str
    data: str = Field(max_length=6990508)


class OverviewRequest(BaseModel):
    device_ids: list[str] = Field(min_length=1, max_length=80)
    phone: str = Field(pattern=r'^\+[1-9]\d{6,14}$')
    device_id: Optional[str] = None
    receipt: Optional[Receipt] = None


def decode_receipt(receipt: Receipt):
    try:
        data = base64.b64decode(receipt.data, validate=True)
    except (ValueError, binascii.Error):
        raise HTTPException(400, 'Invalid receipt file')
    if not data or len(data) > 5 * 1024 * 1024:
        raise HTTPException(413, 'Receipt must be 5 MB or smaller.')
    signatures = {
        'image/jpeg': data.startswith(b'\xff\xd8\xff'),
        'image/png': data.startswith(b'\x89PNG\r\n\x1a\n'),
        'application/pdf': data.startswith(b'%PDF-'),
        'image/heic': data[4:8] == b'ftyp' and data[8:12] in (b'heic', b'heix', b'hevc', b'hevx'),
        'image/heif': data[4:8] == b'ftyp' and data[8:12] in (b'mif1', b'msf1'),
    }
    if not signatures.get(receipt.contentType):
        raise HTTPException(415, 'Choose a JPEG, PNG, HEIC or PDF receipt.')
    return {'name': receipt.name, 'contentType': receipt.contentType, 'size': len(data),
            'uploadedAt': datetime.now(timezone.utc).isoformat(), 'data': Binary(data)}


@router.post('/registration-overview')
def registration_overview(body: OverviewRequest, _: None = Depends(verify_token)):
    # device_ids and phone are supplied by the frontend after checking signed,
    # HTTP-only registration and OTP cookies, never from browser JSON.
    if any(not ObjectId.is_valid(value) for value in body.device_ids):
        raise HTTPException(400, 'Invalid device identifier')
    ids = [ObjectId(value) for value in body.device_ids]
    owner = {'$or': [{'registrationPhone': body.phone}, {'registrationPhone': {'$exists': False}}]}
    if devices_collection.count_documents({'_id': {'$in': ids}, **owner}) != len(set(body.device_ids)):
        raise HTTPException(403, 'Some registrations are unavailable for this verified phone number')
    if body.receipt:
        if body.device_id not in body.device_ids:
            raise HTTPException(403, 'Device unavailable')
        receipt = decode_receipt(body.receipt)
        result = devices_collection.update_one(
            {'_id': ObjectId(body.device_id), **owner},
            {'$set': {'receipt': receipt, 'registrationPhone': body.phone}})
        if not result.matched_count:
            raise HTTPException(403, 'Device unavailable for this verified phone number')
    devices_collection.update_many({'_id': {'$in': ids}, **owner}, {'$set': {'registrationPhone': body.phone}})
    docs = list(devices_collection.find({'_id': {'$in': ids}, 'registrationPhone': body.phone}, {'receipt.data': 0}))
    if len(docs) != len(set(body.device_ids)):
        raise HTTPException(403, 'Some registrations are unavailable for this verified phone number')
    return {'devices': [{'deviceId': str(doc['_id']), 'identifiers': doc.get('identifiers', {}),
                         'uniqueParameters': doc.get('uniqueParameters', {}),
                         'registrationParameters': doc.get('registrationParameters', {}),
                         'registeredAt': doc.get('registeredAt'), 'receipt': doc.get('receipt')} for doc in docs]}
