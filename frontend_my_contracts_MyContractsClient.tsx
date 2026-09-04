"use client";

import { useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { PhoneOrEmailForm, OtpForm } from "../../components/ValidateCustomerForms";
import ReusableHeader from "../../components/ReusableHeader";
import {
  resolveClientTheme,
  readClientStylesFromSession,
  normalizeClientStyles,
  type ClientStyleTokens,
} from "../../lib/clientTheme";

type Step = "collect" | "otp" | "contracts";
type Theme = ReturnType<typeof resolveClientTheme>;

type RawContract = Record<string, unknown>;

interface ContractRecord {
  id: string;
  status?: string;
  reference?: string;
  basketId?: string;
  displayName: string;
  deviceLabel: string;
  deviceId?: string;
  orderId?: string;
  offerId?: string;
  typeLabel: string;
  termMonths?: number;
  startDate?: string;
  endDate?: string;
  activatedAt?: string;
  nextPaymentDate?: string;
  billingFrequency?: string;
  price?: number;
  currency?: string;
  documentUrl?: string | null;
  ipidUrl?: string | null;
  includedProducts: string[];
  productImages: string[];
  raw: RawContract;
}

interface DeviceGroup {
  id: string;
  title: string;
  subtitle: string;
  status: string | undefined;
  products: string[];
  productImages: string[];
  contracts: ContractRecord[];
  totalPrice?: number;
  currency?: string;
  primaryDocumentUrl?: string | null;
  primaryIpidUrl?: string | null;
  startDate?: string;
  endDate?: string;
  nextPaymentDate?: string;
}

const STATUS_META: Record<string, { label: string; bg: string; text: string; dot: string }> = {
  ACTIVE: { label: "Active", bg: "#dcfce7", text: "#166534", dot: "#22c55e" },
  PENDING_ACTIVATION: { label: "Pending activation", bg: "#fef3c7", text: "#92400e", dot: "#f59e0b" },
  EXPIRED: { label: "Expired", bg: "#e5e7eb", text: "#4b5563", dot: "#9ca3af" },
  CANCELLED: { label: "Cancelled", bg: "#fee2e2", text: "#991b1b", dot: "#ef4444" },
  RENEWED: { label: "Renewed", bg: "#dbeafe", text: "#1d4ed8", dot: "#3b82f6" },
  VOID: { label: "Void", bg: "#e5e7eb", text: "#4b5563", dot: "#9ca3af" },
};

function statusMeta(status?: string) {
  return STATUS_META[status ?? ""] ?? {
    label: status ? toHeadline(status) : "Unknown",
    bg: "#e5e7eb",
    text: "#4b5563",
    dot: "#9ca3af",
  };
}

function fmtDate(value?: string) {
  if (!value) return "Not available";
  try {
    return new Date(value).toLocaleDateString(undefined, {
      day: "numeric",
      month: "short",
      year: "numeric",
    });
  } catch {
    return value;
  }
}

function fmtPrice(amount?: number, currency?: string) {
  if (amount == null || Number.isNaN(amount)) return "Price to be confirmed";
  const safeCurrency = (currency || "GBP").toUpperCase();
  try {
    return new Intl.NumberFormat(undefined, {
      style: "currency",
      currency: safeCurrency,
      maximumFractionDigits: 2,
    }).format(amount);
  } catch {
    return `${safeCurrency} ${amount.toFixed(2)}`;
  }
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

function stringArray(value: unknown) {
  if (!Array.isArray(value)) return [];
  const out: string[] = [];
  const seen = new Set<string>();
  for (const item of value) {
    if (typeof item !== "string") continue;
    const trimmed = item.trim();
    if (!trimmed) continue;
    const key = trimmed.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(trimmed);
  }
  return out;
}

function toHeadline(value: string) {
  return value
    .toLowerCase()
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (char) => char.toUpperCase());
}

function chooseStatus(records: ContractRecord[]) {
  const priority = ["ACTIVE", "PENDING_ACTIVATION", "RENEWED", "EXPIRED", "CANCELLED", "VOID"];
  for (const key of priority) {
    if (records.some((record) => record.status === key)) return key;
  }
  return records[0]?.status;
}

function chooseDate(records: ContractRecord[], field: keyof ContractRecord, strategy: "min" | "max") {
  const dates = records
    .map((record) => (typeof record[field] === "string" ? record[field] : undefined))
    .filter((value): value is string => Boolean(value))
    .sort();
  if (!dates.length) return undefined;
  return strategy === "min" ? dates[0] : dates[dates.length - 1];
}

function buildContractRecord(raw: RawContract, index: number): ContractRecord {
  const includedProducts = stringArray(raw.included_products).length
    ? stringArray(raw.included_products)
    : stringArray(raw.products);
  const productImages = stringArray(raw.product_images);
  const deviceLabel =
    firstString(
      raw.device_label,
      raw.device_name,
      raw.device_display_name,
      raw.device_model,
      raw.device_id,
    ) || `Covered device ${index + 1}`;

  const typeLabel =
    firstString(raw.bundle_name, raw.product_name, raw.type, raw.cover_type) || "Protection plan";

  const displayName =
    firstString(raw.display_name, raw.reference_name, raw.bundle_name) || `${deviceLabel} protection`;

  return {
    id:
      firstString(raw.id, raw._id, raw.contract_id, raw.reference, raw.basket_id) ||
      `contract-${index}`,
    status: firstString(raw.status)?.toUpperCase(),
    reference: firstString(raw.reference, raw.contract_reference, raw.policy_number),
    basketId: firstString(raw.basket_id),
    displayName,
    deviceLabel,
    deviceId: firstString(raw.device_id),
    orderId: firstString(raw.order_id),
    offerId: firstString(raw.offer_id),
    typeLabel: toHeadline(typeLabel),
    termMonths: firstNumber(raw.term_months, raw.term),
    startDate: firstString(raw.start_date, raw.created_at),
    endDate: firstString(raw.end_date),
    activatedAt: firstString(raw.activated_at),
    nextPaymentDate: firstString(raw.next_payment_date, raw.renewal_date),
    billingFrequency: firstString(raw.billing_frequency, raw.mode),
    price: firstNumber(raw.price),
    currency: firstString(raw.currency),
    documentUrl: firstString(raw.document_url),
    ipidUrl: firstString(raw.ipid_url),
    includedProducts,
    productImages,
    raw,
  };
}

function buildGroups(records: ContractRecord[]) {
  const grouped = new Map<string, ContractRecord[]>();
  for (const record of records) {
    const key = record.deviceId || record.deviceLabel || record.id;
    const bucket = grouped.get(key) || [];
    bucket.push(record);
    grouped.set(key, bucket);
  }

  const groups: DeviceGroup[] = [];
  grouped.forEach((contracts, key) => {
    const uniqueProducts = Array.from(
      new Set(
        contracts
          .flatMap((contract) => contract.includedProducts.length ? contract.includedProducts : [contract.typeLabel])
          .map((value) => value.trim())
          .filter(Boolean),
      ),
    );
    const uniqueImages = Array.from(
      new Set(contracts.flatMap((contract) => contract.productImages).filter(Boolean)),
    );
    const knownPrices = contracts.map((contract) => contract.price).filter((value): value is number => value != null);
    const title = contracts[0]?.deviceLabel || "Protection plan";
    const subtitle =
      uniqueProducts.length > 0
        ? `${uniqueProducts.length} cover item${uniqueProducts.length === 1 ? "" : "s"} included`
        : `${contracts.length} plan${contracts.length === 1 ? "" : "s"} on file`;

    groups.push({
      id: key,
      title,
      subtitle,
      status: chooseStatus(contracts),
      products: uniqueProducts,
      productImages: uniqueImages,
      contracts,
      totalPrice: knownPrices.length ? knownPrices.reduce((sum, value) => sum + value, 0) : undefined,
      currency: contracts.find((contract) => contract.currency)?.currency,
      primaryDocumentUrl: contracts.find((contract) => contract.documentUrl)?.documentUrl,
      primaryIpidUrl: contracts.find((contract) => contract.ipidUrl)?.ipidUrl,
      startDate: chooseDate(contracts, "startDate", "min") || chooseDate(contracts, "activatedAt", "min"),
      endDate: chooseDate(contracts, "endDate", "max"),
      nextPaymentDate: chooseDate(contracts, "nextPaymentDate", "min"),
    });
  });

  return groups.sort((left, right) => {
    const leftScore = left.status === "ACTIVE" ? 0 : left.status === "PENDING_ACTIVATION" ? 1 : 2;
    const rightScore = right.status === "ACTIVE" ? 0 : right.status === "PENDING_ACTIVATION" ? 1 : 2;
    if (leftScore !== rightScore) return leftScore - rightScore;
    return left.title.localeCompare(right.title);
  });
}

function StatusBadge({ status }: { status?: string }) {
  const meta = statusMeta(status);
  return (
    <span
      className="inline-flex items-center gap-2 rounded-full px-3 py-1 text-xs font-semibold"
      style={{ background: meta.bg, color: meta.text }}
    >
      <span className="h-2 w-2 rounded-full" style={{ background: meta.dot }} />
      {meta.label}
    </span>
  );
}

function SummaryBar({ groups, records, theme }: { groups: DeviceGroup[]; records: ContractRecord[]; theme: Theme }) {
  const active = records.filter((record) => record.status === "ACTIVE").length;
  const pending = records.filter((record) => record.status === "PENDING_ACTIVATION").length;
  const documents = records.filter((record) => record.documentUrl || record.ipidUrl).length;
  const stats = [
    { label: "Covered devices", value: groups.length, tone: theme.accent },
    { label: "Active plans", value: active, tone: "#22c55e" },
    { label: "Awaiting activation", value: pending, tone: "#f59e0b" },
    { label: "Documents ready", value: documents, tone: theme.font },
  ];

  return (
    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      {stats.map((item) => (
        <div
          key={item.label}
          className="rounded-[1.5rem] border px-5 py-4 shadow-sm"
          style={{ background: theme.cardBackground, borderColor: theme.cardBorder }}
        >
          <p className="text-3xl font-semibold tracking-tight" style={{ color: item.tone }}>
            {item.value}
          </p>
          <p className="mt-1 text-sm" style={{ color: theme.fontSecondary }}>
            {item.label}
          </p>
        </div>
      ))}
    </div>
  );
}

function OverviewHero({
  groups,
  records,
  customerName,
  phone,
  theme,
}: {
  groups: DeviceGroup[];
  records: ContractRecord[];
  customerName: string;
  phone: string;
  theme: Theme;
}) {
  const firstName = customerName.trim().split(" ")[0];
  const activeGroups = groups.filter((group) => group.status === "ACTIVE").length;

  return (
    <section
      className="relative overflow-hidden rounded-[2rem] border px-6 py-7 shadow-sm sm:px-8 sm:py-8"
      style={{
        background: `linear-gradient(135deg, ${theme.primaryBackground} 0%, ${theme.cardBackground} 45%, ${theme.secondaryBackground} 100%)`,
        borderColor: theme.cardBorder,
      }}
    >
      <div
        className="pointer-events-none absolute -right-16 -top-16 h-48 w-48 rounded-full blur-3xl"
        style={{ background: `${theme.accent}22` }}
      />
      <div
        className="pointer-events-none absolute bottom-0 left-0 h-32 w-32 rounded-full blur-3xl"
        style={{ background: `${theme.font}0d` }}
      />
      <div className="relative grid gap-6 lg:grid-cols-[1.5fr_1fr]">
        <div>
          <p className="text-sm font-medium" style={{ color: theme.fontSecondary }}>
            Verified for {phone || "your account"}
          </p>
          <h1 className="mt-2 text-3xl font-semibold tracking-tight sm:text-4xl" style={{ color: theme.font }}>
            {firstName ? `${firstName}, here is your protection overview.` : "Your protection overview."}
          </h1>
          <p className="mt-3 max-w-2xl text-sm leading-6 sm:text-base" style={{ color: theme.fontSecondary }}>
            Keep track of what is covered, when each plan started, and which documents are ready whenever you need them.
          </p>
          <div className="mt-5 flex flex-wrap gap-3">
            <span
              className="inline-flex items-center gap-2 rounded-full px-4 py-2 text-sm font-medium"
              style={{ background: `${theme.accent}14`, color: theme.font }}
            >
              <ShieldIcon />
              {activeGroups} active device{activeGroups === 1 ? "" : "s"} protected
            </span>
            <span
              className="inline-flex items-center gap-2 rounded-full px-4 py-2 text-sm font-medium"
              style={{ background: `${theme.font}08`, color: theme.fontSecondary }}
            >
              <FolderIcon />
              {records.length} plan{records.length === 1 ? "" : "s"} on file
            </span>
          </div>
        </div>

        <div
          className="rounded-[1.5rem] border p-5"
          style={{ background: `${theme.cardBackground}dd`, borderColor: theme.cardBorder }}
        >
          <p className="text-sm font-semibold" style={{ color: theme.font }}>
            At a glance
          </p>
          <div className="mt-4 space-y-4 text-sm">
            <div className="flex items-center justify-between gap-4">
              <span style={{ color: theme.fontSecondary }}>Devices covered</span>
              <span className="font-semibold" style={{ color: theme.font }}>
                {groups.length}
              </span>
            </div>
            <div className="flex items-center justify-between gap-4">
              <span style={{ color: theme.fontSecondary }}>Documents available</span>
              <span className="font-semibold" style={{ color: theme.font }}>
                {records.filter((record) => record.documentUrl || record.ipidUrl).length}
              </span>
            </div>
            <div className="rounded-2xl px-4 py-3" style={{ background: `${theme.accent}0e` }}>
              <p className="font-medium" style={{ color: theme.font }}>
                Calm next step
              </p>
              <p className="mt-1 leading-6" style={{ color: theme.fontSecondary }}>
                Open a card to view documents, check dates, or confirm what each device is covered for.
              </p>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

function CoverageChips({ values, theme }: { values: string[]; theme: Theme }) {
  if (!values.length) return null;
  return (
    <div className="flex flex-wrap gap-2">
      {values.map((value) => (
        <span
          key={value}
          className="rounded-full px-3 py-1.5 text-xs font-medium"
          style={{ background: `${theme.accent}10`, color: theme.font }}
        >
          {value}
        </span>
      ))}
    </div>
  );
}

function PrimaryAction({
  group,
  theme,
  onToggleDetails,
  expanded,
}: {
  group: DeviceGroup;
  theme: Theme;
  onToggleDetails: () => void;
  expanded: boolean;
}) {
  if (group.primaryDocumentUrl) {
    return (
      <a
        href={group.primaryDocumentUrl}
        target="_blank"
        rel="noopener noreferrer"
        className="inline-flex items-center justify-center gap-2 rounded-full px-4 py-2 text-sm font-semibold"
        style={{ background: theme.buttonBackground, color: theme.buttonText }}
      >
        <DocIcon />
        View policy
      </a>
    );
  }

  if (group.primaryIpidUrl) {
    return (
      <a
        href={group.primaryIpidUrl}
        target="_blank"
        rel="noopener noreferrer"
        className="inline-flex items-center justify-center gap-2 rounded-full px-4 py-2 text-sm font-semibold"
        style={{ background: theme.buttonBackground, color: theme.buttonText }}
      >
        <DocIcon />
        View cover summary
      </a>
    );
  }

  return (
    <button
      onClick={onToggleDetails}
      className="inline-flex items-center justify-center gap-2 rounded-full px-4 py-2 text-sm font-semibold"
      style={{ background: theme.buttonBackground, color: theme.buttonText }}
    >
      {expanded ? "Hide details" : "View details"}
    </button>
  );
}

function SecondaryActions({ group, theme }: { group: DeviceGroup; theme: Theme }) {
  const links = group.primaryDocumentUrl && group.primaryIpidUrl
    ? [{ href: group.primaryIpidUrl, label: "IPID" }]
    : [];

  if (!links.length) return null;

  return (
    <div className="flex flex-wrap gap-2">
      {links.map((link) => (
        <a
          key={link.label}
          href={link.href}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center gap-2 rounded-full border px-4 py-2 text-sm font-medium"
          style={{ borderColor: theme.cardBorder, color: theme.fontSecondary }}
        >
          <DocIcon />
          {link.label}
        </a>
      ))}
    </div>
  );
}

function JourneyStrip({ group, theme }: { group: DeviceGroup; theme: Theme }) {
  const journey = [
    {
      label: "Started",
      value: group.startDate ? fmtDate(group.startDate) : "Awaiting timeline",
    },
    {
      label: "Status today",
      value: statusMeta(group.status).label,
    },
    {
      label: group.nextPaymentDate ? "Next payment" : "Cover until",
      value: group.nextPaymentDate ? fmtDate(group.nextPaymentDate) : group.endDate ? fmtDate(group.endDate) : "Check details below",
    },
  ];

  return (
    <div className="grid gap-3 sm:grid-cols-3">
      {journey.map((item, index) => (
        <div
          key={item.label}
          className="rounded-2xl border px-4 py-3"
          style={{ background: theme.primaryBackground, borderColor: `${theme.cardBorder}`, boxShadow: index === 1 ? `0 0 0 1px ${theme.accent}22 inset` : undefined }}
        >
          <p className="text-xs uppercase tracking-[0.18em]" style={{ color: theme.fontMuted }}>
            {item.label}
          </p>
          <p className="mt-1 text-sm font-semibold" style={{ color: theme.font }}>
            {item.value}
          </p>
        </div>
      ))}
    </div>
  );
}

function ContractBreakdown({ contracts, theme }: { contracts: ContractRecord[]; theme: Theme }) {
  return (
    <div className="space-y-3">
      {contracts.map((contract) => (
        <div
          key={contract.id}
          className="rounded-2xl border p-4"
          style={{ background: theme.primaryBackground, borderColor: theme.cardBorder }}
        >
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <p className="text-sm font-semibold" style={{ color: theme.font }}>
                {contract.displayName}
              </p>
              <p className="mt-1 text-sm" style={{ color: theme.fontSecondary }}>
                {contract.reference || contract.typeLabel}
              </p>
            </div>
            <div className="text-right">
              <p className="text-sm font-semibold" style={{ color: theme.font }}>
                {fmtPrice(contract.price, contract.currency)}
              </p>
              <p className="mt-1 text-xs" style={{ color: theme.fontMuted }}>
                {contract.termMonths ? `${contract.termMonths} month term` : "Term pending confirmation"}
              </p>
            </div>
          </div>

          <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <DetailItem label="Device" value={contract.deviceLabel} theme={theme} />
            <DetailItem label="Cover type" value={contract.typeLabel} theme={theme} />
            <DetailItem label="Start date" value={fmtDate(contract.startDate || contract.activatedAt)} theme={theme} />
            <DetailItem label="End date" value={contract.endDate ? fmtDate(contract.endDate) : "To be confirmed"} theme={theme} />
          </div>

          {contract.includedProducts.length > 0 && (
            <div className="mt-4">
              <p className="mb-2 text-xs uppercase tracking-[0.18em]" style={{ color: theme.fontMuted }}>
                What is covered
              </p>
              <CoverageChips values={contract.includedProducts} theme={theme} />
            </div>
          )}

          {(contract.orderId || contract.offerId || contract.deviceId) && (
            <div
              className="mt-4 rounded-2xl px-4 py-3 text-xs"
              style={{ background: `${theme.font}06`, color: theme.fontSecondary }}
            >
              {contract.orderId ? `Order: ${contract.orderId}` : ""}
              {contract.orderId && (contract.offerId || contract.deviceId) ? " | " : ""}
              {contract.offerId ? `Offer: ${contract.offerId}` : ""}
              {contract.offerId && contract.deviceId ? " | " : ""}
              {contract.deviceId ? `Device ID: ${contract.deviceId}` : ""}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

function DetailItem({ label, value, theme }: { label: string; value: string; theme: Theme }) {
  return (
    <div>
      <p className="text-xs uppercase tracking-[0.18em]" style={{ color: theme.fontMuted }}>
        {label}
      </p>
      <p className="mt-1 text-sm font-medium" style={{ color: theme.font }}>
        {value}
      </p>
    </div>
  );
}

function DeviceGroupCard({ group, theme }: { group: DeviceGroup; theme: Theme }) {
  const [expanded, setExpanded] = useState(false);
  const meta = statusMeta(group.status);
  const heroImage = group.productImages[0];

  return (
    <article
      className="overflow-hidden rounded-[2rem] border shadow-sm"
      style={{ background: theme.cardBackground, borderColor: theme.cardBorder }}
    >
      <div className="grid lg:grid-cols-[1.1fr_1.4fr]">
        <div
          className="relative min-h-[220px] overflow-hidden px-6 py-6"
          style={{
            background: `linear-gradient(160deg, ${meta.bg} 0%, ${theme.primaryBackground} 60%, ${theme.cardBackground} 100%)`,
          }}
        >
          <div className="absolute inset-0 opacity-70" style={{ background: `radial-gradient(circle at top right, ${meta.dot}18, transparent 42%)` }} />
          <div className="relative flex h-full flex-col justify-between gap-5">
            <div className="flex items-start justify-between gap-4">
              <StatusBadge status={group.status} />
              <div
                className="rounded-full px-3 py-1 text-xs font-medium"
                style={{ background: `${theme.primaryBackground}b8`, color: theme.fontSecondary }}
              >
                {group.subtitle}
              </div>
            </div>

            <div>
              <p className="text-sm font-medium" style={{ color: theme.fontSecondary }}>
                Protected device
              </p>
              <h2 className="mt-2 text-2xl font-semibold tracking-tight" style={{ color: theme.font }}>
                {group.title}
              </h2>
              <p className="mt-2 max-w-md text-sm leading-6" style={{ color: theme.fontSecondary }}>
                {group.products.length
                  ? `You are covered for ${group.products.join(", ")}.`
                  : "Your cover details are stored here for quick access whenever you need them."}
              </p>
            </div>

            {heroImage ? (
              <div className="pointer-events-none absolute bottom-5 right-5 hidden h-24 w-24 overflow-hidden rounded-[1.5rem] border bg-white/70 shadow-sm sm:block" style={{ borderColor: `${theme.primaryBackground}aa` }}>
                <img src={heroImage} alt={group.title} className="h-full w-full object-cover" />
              </div>
            ) : (
              <div
                className="pointer-events-none absolute bottom-5 right-5 hidden h-24 w-24 items-center justify-center rounded-[1.5rem] border text-sm font-semibold sm:flex"
                style={{ borderColor: `${theme.primaryBackground}aa`, background: `${theme.primaryBackground}b5`, color: theme.fontSecondary }}
              >
                {group.title.slice(0, 2).toUpperCase()}
              </div>
            )}
          </div>
        </div>

        <div className="space-y-5 px-6 py-6">
          <div className="grid gap-3 sm:grid-cols-3">
            <DetailItem label="Plan value" value={fmtPrice(group.totalPrice, group.currency)} theme={theme} />
            <DetailItem label="Started" value={group.startDate ? fmtDate(group.startDate) : "Awaiting activation"} theme={theme} />
            <DetailItem
              label={group.nextPaymentDate ? "Next payment" : "Cover until"}
              value={group.nextPaymentDate ? fmtDate(group.nextPaymentDate) : group.endDate ? fmtDate(group.endDate) : "See details"}
              theme={theme}
            />
          </div>

          <div>
            <p className="mb-2 text-xs uppercase tracking-[0.18em]" style={{ color: theme.fontMuted }}>
              What is covered
            </p>
            <CoverageChips values={group.products} theme={theme} />
          </div>

          <JourneyStrip group={group} theme={theme} />

          <div className="flex flex-wrap gap-3">
            <PrimaryAction
              group={group}
              theme={theme}
              onToggleDetails={() => setExpanded((value) => !value)}
              expanded={expanded}
            />
            <button
              onClick={() => setExpanded((value) => !value)}
              className="inline-flex items-center justify-center rounded-full border px-4 py-2 text-sm font-medium"
              style={{ borderColor: theme.cardBorder, color: theme.fontSecondary }}
            >
              {expanded ? "Hide cover breakdown" : "View cover breakdown"}
            </button>
            <SecondaryActions group={group} theme={theme} />
          </div>

          {expanded && <ContractBreakdown contracts={group.contracts} theme={theme} />}
        </div>
      </div>
    </article>
  );
}

function EmptyState({ theme }: { theme: Theme }) {
  return (
    <div className="px-8 py-16 text-center">
      <div
        className="mx-auto flex h-16 w-16 items-center justify-center rounded-full"
        style={{ background: `${theme.accent}10` }}
      >
        <FolderIcon />
      </div>
      <p className="mt-5 text-xl font-semibold" style={{ color: theme.font }}>
        No contracts found
      </p>
      <p className="mt-2 text-sm leading-6" style={{ color: theme.fontSecondary }}>
        We could not find any protection plans linked to this verified account yet.
      </p>
    </div>
  );
}

function DocIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8">
      <path d="M4 3.5h7l5 5v8H4z" />
      <path d="M11 3.5v5h5" />
    </svg>
  );
}

function ShieldIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8">
      <path d="M10 2.5 4 4.8V9c0 4 2.7 6.8 6 8 3.3-1.2 6-4 6-8V4.8L10 2.5Z" />
      <path d="m7.8 9.8 1.5 1.5 3-3" />
    </svg>
  );
}

function FolderIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
      <path d="M3 7.5A2.5 2.5 0 0 1 5.5 5H10l2 2h6.5A2.5 2.5 0 0 1 21 9.5v8A2.5 2.5 0 0 1 18.5 20h-13A2.5 2.5 0 0 1 3 17.5Z" />
    </svg>
  );
}

export default function MyContractsClient() {
  const search = useSearchParams();
  const clientKey = search.get("clientKey") || search.get("client") || undefined;

  const [step, setStep] = useState<Step>("collect");
  const [submitting, setSubmitting] = useState(false);
  const [verifying, setVerifying] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [otpError, setOtpError] = useState<string | null>(null);
  const [maskedDest, setMaskedDest] = useState("");
  const [phoneE164, setPhoneE164] = useState("");
  const [customerName, setCustomerName] = useState("");
  const [contracts, setContracts] = useState<ContractRecord[]>([]);
  const [contractsLoading, setContractsLoading] = useState(false);
  const [contractsError, setContractsError] = useState<string | null>(null);

  const [clientStyles, setClientStyles] = useState<ClientStyleTokens>({});
  const [clientLogo, setClientLogo] = useState<string | undefined>();
  const [clientData, setClientData] = useState<unknown>(null);

  useEffect(() => {
    try {
      const stored = sessionStorage.getItem("clientStyles");
      if (stored) {
        const parsed = JSON.parse(stored);
        setClientData(parsed);
        setClientStyles(normalizeClientStyles(parsed));
        const logo =
          parsed?.Logo_URL || parsed?.logo || parsed?.StylesLogo_URL || parsed?.Styles?.Logo_URL || parsed?.Styles?.StylesLogo_URL;
        if (logo) setClientLogo(logo);
      }
    } catch {}
    const fromSession = readClientStylesFromSession();
    if (Object.keys(fromSession).length) setClientStyles(fromSession);
  }, []);

  useEffect(() => {
    if (!clientKey) return;
    let ignore = false;
    const abort = new AbortController();
    (async () => {
      try {
        const res = await fetch(`/api/client/${encodeURIComponent(clientKey)}`, { signal: abort.signal });
        if (!res.ok) return;
        const json = await res.json().catch(() => null);
        if (ignore || !json) return;
        const raw = json?.data || json || {};
        try {
          sessionStorage.setItem("clientStyles", JSON.stringify(raw));
        } catch {}
        setClientData(raw);
        setClientStyles(normalizeClientStyles(raw));
        const logo = raw?.Logo_URL || raw?.StylesLogo_URL || raw?.Styles?.Logo_URL || null;
        if (logo) setClientLogo(logo);
      } catch {}
    })();
    return () => {
      ignore = true;
      abort.abort();
    };
  }, [clientKey]);

  const theme = resolveClientTheme(clientStyles);
  const groups = buildGroups(contracts);

  const handleCollect = async (data: {
    mode: "phone" | "email";
    phoneE164?: string;
    firstName?: string;
    lastName?: string;
    email?: string;
  }) => {
    setError(null);
    setSubmitting(true);
    const phone = data.phoneE164 ?? "";
    try {
      const res = await fetch("/api/otp/request", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ phone }),
      });
      const json = await res.json();
      if (!res.ok) {
        setError(json?.message || json?.error || "Failed to send verification code. Please try again.");
        return;
      }
      setPhoneE164(phone);
      setMaskedDest(json?.masked_destination || json?.maskedDestination || json?.destination_masked || phone);
      setStep("otp");
    } catch {
      setError("Network error. Please check your connection and try again.");
    } finally {
      setSubmitting(false);
    }
  };

  const loadContracts = async (cid: string) => {
    setContractsLoading(true);
    setContractsError(null);
    try {
      const res = await fetch(`/api/contracts?customer_id=${encodeURIComponent(cid)}`);
      const json = await res.json().catch(() => ({}));
      if (!res.ok) {
        const detail = json?.reason || json?.message || json?.error || `HTTP ${res.status}`;
        setContractsError(`Failed to load your contracts (${detail}). Please refresh to try again.`);
        return;
      }

      const data = Array.isArray(json?.data) ? json.data : [];
      setContracts(data.map((entry: RawContract, index: number) => buildContractRecord(entry, index)));
    } catch {
      setContractsError("Network error loading contracts.");
    } finally {
      setContractsLoading(false);
    }
  };

  const handleOtp = async (code: string) => {
    setOtpError(null);
    setVerifying(true);
    try {
      const verifyRes = await fetch("/api/otp/verify", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ phone: phoneE164, code }),
      });
      const verifyJson = await verifyRes.json();
      if (!verifyRes.ok || !verifyJson?.success) {
        const reason = verifyJson?.reason;
        if (reason === "expired") setOtpError("This code has expired. Please request a new one.");
        else if (reason === "too_many_attempts") setOtpError("Too many incorrect attempts. Please request a new code.");
        else setOtpError(verifyJson?.message || "Incorrect code. Please try again.");
        return;
      }

      const digits = phoneE164.replace(/[^0-9]/g, "");
      const cusRes = await fetch("/api/customer/get-or-create", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          firstName: "Customer",
          lastName: "Account",
          telephone: phoneE164,
          email: `lookup-${digits}@noemail.activlink.io`,
        }),
      });
      const cusJson = await cusRes.json();
      const cid =
        cusJson?.customerId ||
        cusJson?.customerID ||
        cusJson?.id ||
        cusJson?.data?.customerId ||
        cusJson?.data?.customerID ||
        cusJson?.data?.id ||
        null;

      if (!cid) {
        setContractsError("Could not locate your account. Please contact support.");
        setStep("contracts");
        return;
      }

      setCustomerName(cusJson?.name || cusJson?.data?.name || "");
      setStep("contracts");
      await loadContracts(cid);
    } catch {
      setOtpError("Verification failed. Please try again.");
    } finally {
      setVerifying(false);
    }
  };

  const handleResend = async () => {
    setOtpError(null);
    try {
      await fetch("/api/otp/request", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ phone: phoneE164 }),
      });
    } catch {}
  };

  const reset = () => {
    setStep("collect");
    setContracts([]);
    setCustomerName("");
    setPhoneE164("");
    setError(null);
    setOtpError(null);
    setContractsError(null);
  };

  return (
    <div style={{ background: theme.secondaryBackground, minHeight: "100vh" }}>
      <ReusableHeader
        headingText="My Contracts"
        clientStyles={clientStyles}
        clientLogo={clientLogo}
        clientData={clientData}
      />

      {(step === "collect" || step === "otp") && (
        <main className="mx-auto w-full max-w-5xl px-4 py-8 sm:px-6 md:px-8" style={{ minHeight: "calc(100dvh - 180px)" }}>
          <section
            className="mx-auto grid max-w-4xl gap-6 rounded-[2rem] border p-5 shadow-md sm:p-6 lg:grid-cols-[1.1fr_0.9fr]"
            style={{ background: theme.cardBackground, borderColor: theme.cardBorder }}
          >
            <div className="rounded-[1.5rem] px-5 py-6" style={{ background: `${theme.accent}10` }}>
              <div className="flex h-11 w-11 items-center justify-center rounded-2xl" style={{ background: theme.accent }}>
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke={theme.buttonText} strokeWidth="2">
                  <path d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z" />
                </svg>
              </div>
              <h1 className="mt-5 text-2xl font-semibold tracking-tight" style={{ color: theme.font }}>
                {step === "otp" ? "Enter your code" : "Verify your identity"}
              </h1>
              <p className="mt-3 text-sm leading-6" style={{ color: theme.fontSecondary }}>
                {step === "otp"
                  ? `We sent a 6-digit code to ${maskedDest}. Once you enter it, your contracts will be ready to view.`
                  : "Use your phone number to unlock a calm, device-first view of your cover and policy documents."}
              </p>
            </div>

            <div>
              {step === "collect" && (
                <PhoneOrEmailForm
                  onSubmit={handleCollect}
                  loading={submitting}
                  error={error}
                  collectNameEmail={false}
                  labelSendCode="Send verification code"
                  styles={clientStyles}
                />
              )}

              {step === "otp" && (
                <>
                  <OtpForm
                    onVerify={handleOtp}
                    onResend={handleResend}
                    loading={verifying}
                    error={otpError}
                    maskedDestination={maskedDest}
                    styles={clientStyles}
                  />
                  <button
                    onClick={() => {
                      setStep("collect");
                      setOtpError(null);
                    }}
                    className="mt-4 w-full rounded-xl py-2 text-sm transition-colors"
                    style={{ color: theme.accent }}
                  >
                    Change phone number
                  </button>
                </>
              )}
            </div>
          </section>
        </main>
      )}

      {step === "contracts" && (
        <main className="mx-auto max-w-7xl px-4 py-8 sm:px-6 md:px-8" style={{ minHeight: "calc(100dvh - 180px)" }}>
          <div className="space-y-6">
            <OverviewHero groups={groups} records={contracts} customerName={customerName} phone={phoneE164} theme={theme} />

            {contractsLoading && (
              <div className="flex items-center justify-center gap-3 py-20" style={{ color: theme.fontSecondary }}>
                <svg className="animate-spin" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83" />
                </svg>
                Loading your contracts...
              </div>
            )}

            {contractsError && (
              <div
                className="rounded-[1.5rem] p-5"
                style={{ background: `${theme.error}12`, border: `1px solid ${theme.error}33`, color: theme.error }}
              >
                <p className="font-semibold">Something went wrong</p>
                <p className="mt-1 text-sm">{contractsError}</p>
              </div>
            )}

            {!contractsLoading && !contractsError && (
              <>
                <SummaryBar groups={groups} records={contracts} theme={theme} />

                {groups.length === 0 ? (
                  <div className="rounded-[2rem] border shadow-sm" style={{ background: theme.cardBackground, borderColor: theme.cardBorder }}>
                    <EmptyState theme={theme} />
                  </div>
                ) : (
                  <div className="space-y-5">
                    {groups.map((group) => (
                      <DeviceGroupCard key={group.id} group={group} theme={theme} />
                    ))}
                  </div>
                )}
              </>
            )}

            <div className="pt-2 text-center">
              <button
                onClick={reset}
                className="rounded-full border px-4 py-2 text-sm font-medium"
                style={{ borderColor: theme.cardBorder, color: theme.fontSecondary }}
              >
                Use a different number
              </button>
            </div>
          </div>
        </main>
      )}
    </div>
  );
}
