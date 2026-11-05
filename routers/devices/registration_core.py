"""Device registration pieces shared by `POST /device-register` and `POST /register`.

Both routes must agree on what counts as an already-registered device and on the shape of a
`Devices` document, otherwise a product registered through the embedded API would not be
recognised as a duplicate on the web journey (or the reverse), and /my-registrations would
read two document shapes.
"""
from datetime import datetime
from typing import Optional


def valid_value(val) -> bool:
    return val is not None and str(val).strip() != "" and str(val).strip().lower() != "string"


def find_duplicate(devices_collection, *, gtin=None, make=None, model=None, serial=None,
                   imei=None, mac=None) -> Optional[dict]:
    """The first existing device matching, in order: IMEI, MAC, make+model+serial,
    GTIN+serial. Returns ``{"device", "matched_field", "matched_value"}`` or None.

    Checked on the caller's raw identifiers, before catalogue enrichment, so the same input
    is a duplicate whichever route it arrives on.
    """
    checks = []
    if valid_value(imei):
        checks.append(({"uniqueParameters.imei": imei}, "imei", imei))
    if valid_value(mac):
        checks.append(({"uniqueParameters.MAC": mac}, "MAC", mac))
    if valid_value(make) and valid_value(model) and valid_value(serial):
        checks.append(({"identifiers.make": make, "identifiers.model": model,
                        "uniqueParameters.serial": serial},
                       "make/model/serial", f"{make} / {model} / {serial}"))
    if valid_value(gtin) and valid_value(serial):
        checks.append(({"identifiers.GTIN": gtin, "uniqueParameters.serial": serial},
                       "GTIN/serial", f"{gtin} / {serial}"))

    for query, field, value in checks:
        found = devices_collection.find_one(query)
        if found:
            return {"device": found, "matched_field": field, "matched_value": value}
    return None


def unique_parameters(mac, serial, imei) -> dict:
    return {
        "MAC": mac or "",
        "serial": serial or "",
        "imei": imei if imei and imei != "string" else "",
    }


def build_device_doc(*, client_id: str, locale: str, source: str, identifiers: dict,
                     unique: dict, registration_parameters: dict, custom_sku_id,
                     master_sku_id, sku_status: str) -> dict:
    return {
        "client": client_id,
        "locale": locale,
        "source": source,
        "identifiers": identifiers,
        "uniqueParameters": unique,
        "registrationParameters": registration_parameters,
        "customSkuId": custom_sku_id,
        "masterSkuId": master_sku_id,
        "skuStatus": sku_status,
        "registeredAt": datetime.utcnow().isoformat() + "Z",
    }
