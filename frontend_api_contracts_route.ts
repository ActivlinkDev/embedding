import { NextRequest, NextResponse } from "next/server";
import {
  verifyPhoneSession,
  normalizePhone,
  VERIFIED_PHONE_COOKIE,
} from "../../../lib/contractsSession";

const API_BASE = process.env.FASTAPI_BASE_URL || "";
const RAW_KEY = process.env.LOOKUP_API_KEY ?? "";
const AUTH = RAW_KEY ? `Bearer ${RAW_KEY.replace(/^bearer\s+/i, "")}` : null;

type JsonMap = Record<string, unknown>;

function isObject(value: unknown): value is JsonMap {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function firstString(...values: unknown[]) {
  for (const value of values) {
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return undefined;
}

function firstNumber(...values: unknown[]) {
  for (const value of values) {
    if (typeof value === "number" && Number.isFinite(value)) return value;
    if (typeof value === "string" && value.trim()) {
      const parsed = Number(value);
      if (Number.isFinite(parsed)) return parsed;
    }
  }
  return undefined;
}

function dedupeStrings(values: unknown[], limit?: number) {
  const out: string[] = [];
  const seen = new Set<string>();
  for (const value of values) {
    if (typeof value !== "string") continue;
    const trimmed = value.trim();
    if (!trimmed) continue;
    const key = trimmed.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(trimmed);
    if (limit && out.length >= limit) break;
  }
  return out;
}

async function fetchJson(path: string) {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { Accept: "application/json", ...(AUTH ? { Authorization: AUTH } : {}) },
    cache: "no-store",
  });
  if (!res.ok) {
    throw new Error(`Upstream ${res.status} for ${path}`);
  }
  return res.json().catch(() => null);
}

function hydrateFromBasket(contract: JsonMap, basketDoc: JsonMap | null) {
  if (!basketDoc) return contract;

  const items = Array.isArray(basketDoc.Basket) ? basketDoc.Basket.filter(isObject) : [];
  const bestRule = isObject(basketDoc.best_rule) ? basketDoc.best_rule : null;

  const deviceLabels = dedupeStrings(
    items.map((item) =>
      firstString(
        item.device_label,
        [item.make, item.model].filter((part) => typeof part === "string" && part.trim()).join(" "),
        item.deviceId,
      ),
    ),
    4,
  );
  const includedProducts = dedupeStrings(
    items.flatMap((item) => [
      item.product_name,
      item.product_description,
      item.product_id,
      item.category,
    ]),
    8,
  );
  const productImages = dedupeStrings(
    items.flatMap((item) => (Array.isArray(item.product_images) ? item.product_images : [])),
    8,
  );
  const deviceIds = dedupeStrings(items.map((item) => item.deviceId), 4);
  const termMonths = Math.max(
    0,
    ...items.map((item) => firstNumber(item.poc) ?? 0),
  );

  const finalTotal = firstNumber(basketDoc.final_total, basketDoc.subtotal);
  const currency = firstString(...items.map((item) => item.currency));
  const displayName =
    firstString(contract.display_name, bestRule?.name) ||
    (deviceLabels[0] ? `${deviceLabels[0]} Protection` : undefined);

  return {
    ...contract,
    device_label: firstString(contract.device_label, deviceLabels[0]),
    device_id: firstString(contract.device_id, deviceIds[0]),
    device_ids: deviceIds.length ? deviceIds : contract.device_ids,
    included_products:
      dedupeStrings([
        ...(Array.isArray(contract.included_products) ? contract.included_products : []),
        ...includedProducts,
      ]) || [],
    product_images:
      dedupeStrings([
        ...(Array.isArray(contract.product_images) ? contract.product_images : []),
        ...productImages,
      ]) || [],
    term_months: firstNumber(contract.term_months, termMonths || undefined),
    price:
      firstNumber(
        contract.price,
        typeof finalTotal === "number" && finalTotal > 0 ? finalTotal / 100 : undefined,
      ),
    currency: firstString(contract.currency, currency)?.toUpperCase(),
    display_name: displayName || contract.display_name,
    bundle_name: firstString(contract.bundle_name, bestRule?.name),
  };
}

async function phoneOwnsCustomer(customerId: string, verifiedPhone: string): Promise<boolean> {
  const json = await fetchJson(`/customer/by-id?customer_id=${encodeURIComponent(customerId)}`);
  const doc = isObject(json) && isObject(json.data) ? json.data : {};
  const stored = firstString(doc.telephone, doc.phone, doc.mobile) || "";
  const a = normalizePhone(stored);
  const b = normalizePhone(verifiedPhone);
  return !!a && !!b && a === b;
}

export async function GET(req: NextRequest) {
  if (!API_BASE) {
    return NextResponse.json({ error: "CONFIG_ERROR", message: "FASTAPI_BASE_URL not configured" }, { status: 500 });
  }

  const { searchParams } = new URL(req.url);
  const customer_id = searchParams.get("customer_id");
  if (!customer_id) {
    return NextResponse.json({ error: "MISSING_PARAM", message: "customer_id is required" }, { status: 400 });
  }

  const cookieVal = req.cookies.get(VERIFIED_PHONE_COOKIE)?.value;
  if (!cookieVal) {
    return NextResponse.json({ error: "UNAUTHORIZED", reason: "not_verified", message: "Identity verification required." }, { status: 401 });
  }
  const session = verifyPhoneSession(cookieVal);
  if (!session.ok) {
    return NextResponse.json({ error: "UNAUTHORIZED", reason: session.reason, message: "Verification expired or invalid." }, { status: 401 });
  }

  try {
    const owns = await phoneOwnsCustomer(customer_id, session.phone);
    if (!owns) {
      return NextResponse.json({ error: "FORBIDDEN", reason: "customer_mismatch" }, { status: 403 });
    }
  } catch {
    return NextResponse.json({ error: "AUTH_CHECK_FAILED", message: "Could not verify account ownership." }, { status: 502 });
  }

  try {
    const upstream = await fetchJson(`/customers/${encodeURIComponent(customer_id)}/contracts`);
    const contracts = Array.isArray(upstream)
      ? upstream
      : isObject(upstream) && Array.isArray(upstream.data)
        ? upstream.data
        : [];

    const hydrated = await Promise.all(
      contracts.map(async (entry) => {
        const contract = isObject(entry) ? { ...entry } : ({ value: entry } as JsonMap);
        const basketId = firstString(contract.basket_id);
        if (!basketId) return contract;
        try {
          const basketJson = await fetchJson(`/basket/${encodeURIComponent(basketId)}`);
          const basketDoc = isObject(basketJson) ? basketJson : null;
          return hydrateFromBasket(contract, basketDoc);
        } catch {
          return contract;
        }
      }),
    );

    return NextResponse.json({ data: hydrated }, { status: 200 });
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : "Unexpected error";
    return NextResponse.json({ error: "FETCH_FAILED", message: msg }, { status: 500 });
  }
}
