#!/usr/bin/env python3
"""Fatture in Cloud MCP Server - v2.0.0

MCP Server per integrare Fatture in Cloud con Claude AI.
Permette di gestire fatture elettroniche italiane tramite conversazione.

Author: Mediaform s.c.r.l. (https://media-form.it)
License: MIT
"""

import calendar
import json
import os
import sys
import traceback
from datetime import datetime, timedelta

import fattureincloud_python_sdk as fic
from fattureincloud_python_sdk.exceptions import ApiException
from fattureincloud_python_sdk.api.issued_documents_api import IssuedDocumentsApi
from fattureincloud_python_sdk.api.issued_e_invoices_api import IssuedEInvoicesApi
from fattureincloud_python_sdk.api.received_documents_api import ReceivedDocumentsApi
from fattureincloud_python_sdk.api.clients_api import ClientsApi
from fattureincloud_python_sdk.api.companies_api import CompaniesApi
from fattureincloud_python_sdk.api.info_api import InfoApi

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent, ToolAnnotations

import cache


def _ann(read_only=False, destructive=False, idempotent=False, open_world=True):
    """Shorthand for MCP tool annotations. openWorld defaults to True since
    every tool talks to the FattureInCloud API."""
    return ToolAnnotations(
        readOnlyHint=read_only,
        destructiveHint=destructive,
        idempotentHint=idempotent,
        openWorldHint=open_world,
    )

# a flat ceiling on the pages a single tool call reads; per-list tuning only
# if a real company hits it
MAX_PAGES = 10

# ReceivedDocumentType, from the official OpenAPI spec
RECEIVED_DOCUMENT_TYPES = ("expense", "passive_credit_note", "passive_delivery_note", "self_invoice")

ACCESS_TOKEN = os.getenv("FIC_ACCESS_TOKEN", "")
COMPANY_ID = int(os.getenv("FIC_COMPANY_ID", "0"))
SENDER_EMAIL = os.getenv("FIC_SENDER_EMAIL", "")

configuration = fic.Configuration()
configuration.access_token = ACCESS_TOKEN
api_client = fic.ApiClient(configuration)

issued_api = IssuedDocumentsApi(api_client)
einvoice_api = IssuedEInvoicesApi(api_client)
received_api = ReceivedDocumentsApi(api_client)
clients_api = ClientsApi(api_client)
companies_api = CompaniesApi(api_client)
info_api = InfoApi(api_client)

app = Server("fattureincloud")


def get_total_from_doc(d):
    payments = d.get('payments_list', [])
    if payments:
        return sum(p.get('amount', 0) for p in payments)
    items = d.get('items_list', [])
    return sum((i.get('qty', 0) * i.get('gross_price', 0)) for i in items)


def get_client_by_id(client_id, *, company_id=None):
    if company_id is None:
        company_id = COMPANY_ID
    resource = f"client_{client_id}"
    hit = cache.get(resource, company_id, ttl=timedelta(hours=24))
    if hit is not None:
        return hit
    try:
        response = clients_api.get_client(company_id=company_id, client_id=client_id)
        data = response.data.to_dict()
        cache.put(resource, company_id, data)
        return data
    except:
        return None


def get_ei_code_for_client(client_id, *, company_id=None):
    if company_id is None:
        company_id = COMPANY_ID
    try:
        client = get_client_by_id(client_id, company_id=company_id)
        if client:
            ei_code = (client.get('ei_code') or '').strip()
            if ei_code:
                return ei_code
            pec = (client.get('certified_email') or '').strip()
            if pec:
                return '0000000'
        return '0000000'
    except:
        return '0000000'


@cache.cached("cost_centers", ttl=timedelta(hours=24))
def fetch_cost_centers(*, company_id):
    """Cost centers (FIC `/info/cost_centers`). Used to validate `cost_center`
    on received documents only."""
    try:
        response = info_api.list_cost_centers(company_id=company_id)
        return list(response.data or [])
    except Exception:
        return []


@cache.cached("revenue_centers", ttl=timedelta(hours=24))
def fetch_revenue_centers(*, company_id):
    """Revenue centers (FIC `/info/revenue_centers`). Used to validate
    `revenue_center` on issued documents only.

    FIC keeps cost and revenue centers as two separate registries; the
    `list_cost_centers` MCP tool returns their union (matching the
    'Analisi centri c/r' view in the FIC UI), but mutation validation
    has to use the type-specific list to match how the FIC API itself
    accepts/rejects values."""
    try:
        response = info_api.list_revenue_centers(company_id=company_id)
        return list(response.data or [])
    except Exception:
        return []


@cache.cached("payment_accounts", ttl=timedelta(hours=24))
def fetch_payment_accounts(*, company_id):
    """Payment accounts (FIC `/info/payment_accounts`). Used to resolve the
    `payment_account` argument of `set_payment` by id or by name."""
    # no fallback to []: an outage would read as a company with no accounts,
    # and the shared error path already reports what FIC answered
    response = info_api.list_payment_accounts(company_id=company_id)
    accounts = []
    for a in (response.data or []):
        d = a.to_dict() if hasattr(a, "to_dict") else dict(a)
        acc_type = str(d.get("type") or "").split(".")[-1].lower()
        accounts.append({
            "id": d.get("id"),
            "name": d.get("name"),
            "type": acc_type or None,
            "virtual": d.get("virtual"),
        })
    return accounts


def resolve_payment_account(value):
    """Resolve a `payment_account` argument (numeric id or account name)
    against the cached account list. Returns (account, error_message)."""
    accounts = fetch_payment_accounts(company_id=COMPANY_ID)
    if not accounts:
        return None, ("L'anagrafica conti (/info/payment_accounts) è vuota: nessun conto "
                      "a cui associare il pagamento.")
    names = [a["name"] for a in accounts]

    is_id = (isinstance(value, int) and not isinstance(value, bool)) or (
        isinstance(value, str) and value.strip().isascii() and value.strip().isdigit()
    )
    if is_id:
        wanted = int(value)
        for a in accounts:
            if a["id"] == wanted:
                return a, None
        return None, f"payment_account con id {wanted} non esiste. Disponibili: {names}."

    wanted = str(value).strip().lower()
    exact = [a for a in accounts if (a["name"] or "").strip().lower() == wanted]
    if len(exact) == 1:
        return exact[0], None
    partial = [a for a in accounts if wanted and wanted in (a["name"] or "").lower()]
    if len(partial) == 1:
        return partial[0], None
    if len(partial) > 1:
        return None, f"payment_account '{value}' ambiguo: {[a['name'] for a in partial]}."
    return None, f"payment_account '{value}' non esiste. Disponibili: {names}."


def _enum_value(value):
    """Plain wire value of an SDK enum member ('IssuedDocumentType.CREDIT_NOTE'
    -> 'credit_note'); passes plain strings through."""
    if value is None:
        return None
    return str(value).split(".")[-1].lower()


def _payment_status(p):
    """Normalized payment status: 'paid', 'not_paid' or 'reversed'. The SDK
    returns either a plain string or an enum repr depending on the endpoint."""
    raw = p.get("status")
    if raw is None:
        return ""
    return str(raw).split(".")[-1].lower()


def _payment_entry(p):
    """Serialize one payments_list entry read from the API into a JSON-safe
    payload the FIC modify endpoints accept back unchanged."""
    entry = {
        "amount": p.get("amount"),
        "due_date": str(p.get("due_date"))[:10] if p.get("due_date") else None,
        "status": _payment_status(p) or "not_paid",
    }
    if p.get("id"):
        entry["id"] = p["id"]
    # whatever date the document stores, on whatever status: a reversed
    # payment keeps its date, and a write that echoes the list must not drop it
    if p.get("paid_date"):
        entry["paid_date"] = str(p["paid_date"])[:10]
    account = p.get("payment_account")
    if hasattr(account, "to_dict"):
        account = account.to_dict()
    if isinstance(account, dict) and account.get("id"):
        # PaymentAccount.type defaults to `standard` in the SDK model, so
        # sending the id alone retypes a bank account.
        entry["payment_account"] = {
            k: _enum_value(account[k]) if k == "type" else account[k]
            for k in ("id", "type") if account.get(k)
        }
    terms = p.get("payment_terms")
    if hasattr(terms, "to_dict"):
        terms = terms.to_dict()
    if isinstance(terms, dict):
        terms = {k: v for k, v in terms.items() if v is not None}
        if terms:
            entry["payment_terms"] = terms
    if p.get("ei_raw"):
        entry["ei_raw"] = p["ei_raw"]
    return entry


def _payment_terms_of(doc):
    """Stored payment terms of a document's first installment."""
    return ((doc.get("payments_list") or [{}])[0] or {}).get("payment_terms") or {}


def _payment_days_of(doc):
    """Payment-term days of a document's first installment, absorbed rather
    than judged: this is what the document stores, not what a caller wrote.
    `days: 0` (rimessa diretta) survives, an integral float is the same term,
    and anything else — null, out of range — reads back as 30."""
    days = _payment_terms_of(doc).get("days")
    if isinstance(days, float) and days.is_integer():
        days = int(days)
    if isinstance(days, bool) or not isinstance(days, int) or not 0 <= days <= 3650:
        return 30
    return days


def _due_date(invoice_date, days, terms_type):
    """Due date of a payment term. `standard` counts the days from the document
    date. `end_of_month` counts them and then moves to the last day of the
    month reached. FattureInCloud does not document which of the two Italian
    conventions it applies; its own fix note for "30 giorni fine mese" names a
    document dated the 31st before a 30-day month as the broken case, and the
    document's day can only matter when the days are counted from it. If a
    live check shows the other convention — end of the document's month, then
    the days — this is the function to change."""
    due = invoice_date + timedelta(days=days)
    if terms_type == "end_of_month":
        due = due.replace(day=calendar.monthrange(due.year, due.month)[1])
    return due


def _page_count(response):
    """`last_page` as an int: a listing is one page unless the API says
    otherwise, including when it says it as a string."""
    try:
        last_page = int(getattr(response, "last_page", 1))
    except (TypeError, ValueError):
        return 1
    return last_page if last_page > 0 else 1


def _received_document_type(value):
    """Normalize a ReceivedDocumentType, accepting the old `credit_note`
    spelling this repo used to document. Returns (type, error)."""
    doc_type = {"credit_note": "passive_credit_note"}.get(value, value)
    if doc_type not in RECEIVED_DOCUMENT_TYPES:
        return None, (f"type '{value}' non valido. Ammessi: {sorted(RECEIVED_DOCUMENT_TYPES)}.")
    return doc_type, None


def _all_pages(api_call, *, max_pages=MAX_PAGES, **kwargs):
    """Every list endpoint caps `per_page` at 100 (OpenAPI v2.1.8), so a single
    call is not a year of documents. Follow `last_page` instead of reporting a
    partial total, but stop at `max_pages`: a caller that reads four lists would
    otherwise make dozens of sequential requests against a rate-limited API.
    `last_page` is read once — a document created while paging shifts the pages,
    which an annual dashboard tolerates. Returns (docs, truncated)."""
    response = api_call(per_page=100, page=1, **kwargs)
    docs = list(response.data or [])
    pages = _page_count(response)
    for page in range(2, min(pages, max_pages) + 1):
        page_response = api_call(per_page=100, page=page, **kwargs)
        docs.extend(page_response.data or [])
    return docs, pages > max_pages


def _iso_date(value, field="date", default=None):
    """Parse a YYYY-MM-DD date. A null means the default (today unless given),
    the whole string is validated — no truncation — and FIC receives the padded
    form rather than what the client typed. Returns (datetime, error)."""
    raw = value if value is not None else (default or datetime.now().strftime("%Y-%m-%d"))
    try:
        return datetime.strptime(str(raw).strip(), "%Y-%m-%d"), None
    except (TypeError, ValueError):
        return None, f"{field} '{raw}' non valida: usa il formato YYYY-MM-DD."


def _year_argument(value):
    """Validate the `year` argument. It is interpolated into the `q` filter, so
    it is judged here rather than trusted from the JSON schema. Returns
    (year, error); a null means the current year."""
    if value is None:
        return datetime.now().year, None
    if isinstance(value, bool) or not isinstance(value, int) or not 1900 <= value <= 2100:
        return None, f"year = {value!r}: serve un anno fra 1900 e 2100."
    return value, None


def _month_argument(value):
    """Validate the `month` argument. Returns (month, error); a null means the
    whole year. A string used to reach `{month:02d}` and raise TypeError."""
    if value is None:
        return None, None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 12:
        return None, f"month = {value!r}: serve un intero fra 1 e 12."
    return value, None


def _page_argument(value):
    """Validate the `page` argument. Returns (page, error)."""
    if value is None:
        return 1, None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None, f"page = {value!r}: serve un intero >= 1."
    return value, None


def _payment_days_argument(value, default=30):
    """Validate the payment-terms argument the caller sent. A null means the
    default, which comes from the document and is returned untouched — only
    what the caller wrote is judged, and only against 0-3650 days, since a
    negative value dates the installment before the document."""
    if value is None:
        return default, None
    if isinstance(value, bool) or not isinstance(value, int):
        return None, f"payment_days = {value!r}: serve un intero (giorni)."
    if not 0 <= value <= 3650:
        return None, f"payment_days = {value}: fuori intervallo (0-3650 giorni)."
    return value, None


# Percentages and flags FIC applies on top of the lines (ritenuta d'acconto,
# cassa previdenziale, rivalsa INPS, marca da bollo, split payment). The local
# total only models the lines, so a document carrying any of these has a total
# this server cannot reproduce.
AMOUNT_MODIFIER_RATES = (
    "rivalsa", "cassa", "cassa2", "withholding_tax",
    "other_withholding_tax", "stamp_duty", "use_split_payment",
)
# The taxable bases those rates apply to. FIC values them independently of the
# rate, so they answer nothing about whether the total moved — they only have
# to travel with a copy of the document.
AMOUNT_MODIFIER_BASES = (
    "cassa_taxable", "cassa2_taxable", "global_cassa_taxable",
    "withholding_tax_taxable",
)
AMOUNT_MODIFIERS = AMOUNT_MODIFIER_RATES + AMOUNT_MODIFIER_BASES


def _amount_modifiers_of(doc, fields=AMOUNT_MODIFIERS):
    """Document-level amounts to copy. Pass AMOUNT_MODIFIER_RATES to ask the
    narrower question: does this document have a total the lines do not give?"""
    return {k: doc[k] for k in fields if doc.get(k) not in (None, 0, 0.0, False)}


def _ei_data_of(doc, default_payment_method=None):
    """E-invoice attributes to echo back: the stored dict without the nulls
    `to_dict()` emits, with the payment method taken from the document's own
    method registry when the dict does not carry one. Returns {} when there is
    nothing to inherit — omitting the field leaves the stored one alone, while
    writing a default would declare a payment method the document never had."""
    ei_data = {k: v for k, v in (doc.get("ei_data") or {}).items() if v is not None}
    payment_method = (
        ei_data.get("payment_method")
        or (doc.get("payment_method") or {}).get("ei_payment_method")
        or default_payment_method
    )
    if payment_method:
        ei_data["payment_method"] = payment_method
    return ei_data


def _gross_of(doc):
    """Gross amount of a received document. `amount_gross` is read-only, so the
    SDK model drops it from to_dict() and it has to be recomposed."""
    gross = doc.get("amount_gross")
    if gross is not None:
        return gross
    return (doc.get("amount_net") or 0) + (doc.get("amount_vat") or 0)


def _amount_due_of(doc):
    """What the supplier is actually paid: the gross less the withholding the
    buyer keeps back (ritenuta d'acconto)."""
    return (_gross_of(doc)
            - (doc.get("amount_withholding_tax") or 0)
            - (doc.get("amount_other_withholding_tax") or 0))


def _stored_totals(doc, fallback_total, fallback_due_date):
    """Totals to report after a write: FIC sizes the installment from the
    document, so the local estimate is only a fallback for a response that does
    not carry them."""
    payments = doc.get("payments_list") or []
    stored = (payments[0] or {}) if payments else {}
    # `amount_gross` is read-only and does not survive to_dict(): the
    # installments are the only total the response carries. Which sign FIC
    # keeps them with is undocumented, so credit-note callers re-apply theirs.
    total = round(sum(p.get("amount") or 0 for p in payments), 2) if payments else None
    due_date = str(stored.get("due_date") or "")[:10]
    return (
        round(total if total is not None else fallback_total, 2),
        due_date or fallback_due_date,
    )


def _error(message, **extra):
    payload = {"success": False, "error": message}
    payload.update(extra)
    return [TextContent(type="text", text=json.dumps(payload, indent=2, ensure_ascii=False))]


@cache.cached("vat_types", ttl=timedelta(hours=24))
def fetch_vat_types(*, company_id):
    """VAT types (FIC `/info/vat_types`). The spec marks `vat.value` read-only
    on document items, so the rate is selected by `vat.id` — the percentage in
    the payload is ignored."""
    try:
        response = info_api.list_vat_types(company_id=company_id)
    except Exception:
        return []
    types = []
    for v in (response.data or []):
        d = v.to_dict() if hasattr(v, "to_dict") else dict(v)
        types.append({
            "id": d.get("id"),
            "value": d.get("value"),
            "description": d.get("description"),
            "is_disabled": d.get("is_disabled"),
            "default": d.get("default"),
        })
    return types


def resolve_vat_type(rate):
    """Map a percentage to a FIC vat type id. Returns (vat_type, error)."""
    if isinstance(rate, bool) or not isinstance(rate, (int, float)):
        return None, f"vat_rate '{rate}' non valido: usa un numero (es. 22)."
    types = fetch_vat_types(company_id=COMPANY_ID)
    if not types:
        return None, ("Impossibile leggere l'anagrafica IVA (/info/vat_types): "
                      "riprova, l'aliquota non può essere impostata senza.")
    matches = [
        t for t in types
        if t.get("value") is not None and round(t["value"], 2) == round(rate, 2)
        and not t.get("is_disabled")
    ]
    if not matches:
        available = sorted(
            {(round(t["value"], 2), t.get("description")) for t in types
             if t.get("value") is not None and not t.get("is_disabled")}
        )
        return None, (f"vat_rate {rate} non corrisponde a nessuna aliquota configurata attiva. "
                      f"Disponibili: {available}.")
    matches.sort(key=lambda t: (not t.get("default"), t.get("id") or 0))
    if len(matches) > 1 and not matches[0].get("default"):
        # Several 0% types differ by natura (N1…N7), which ends up in the
        # e-invoice XML: picking the lowest id would choose it silently.
        options = [{"id": t["id"], "description": t.get("description")} for t in matches]
        return None, (f"vat_rate {rate} corrisponde a {len(matches)} aliquote con natura "
                      f"diversa: {options}. Indica quale con vat_id.")
    return matches[0], None


def resolve_vat_id(vat_id):
    """Look up a vat type by id. The percentage is needed for the local total,
    and the id deserves the same validation `vat_rate` gets."""
    if isinstance(vat_id, str) and vat_id.strip().isascii() and vat_id.strip().isdigit():
        vat_id = int(vat_id)
    types = fetch_vat_types(company_id=COMPANY_ID)
    if not types:
        return None, ("Impossibile leggere l'anagrafica IVA (/info/vat_types): "
                      "riprova, l'aliquota non può essere impostata senza.")
    if isinstance(vat_id, bool) or not isinstance(vat_id, int):
        options = [(t["id"], t.get("description")) for t in types if not t.get("is_disabled")]
        return None, f"vat_id '{vat_id}' non valido: usa un intero. Disponibili: {options}."
    for vat_type in types:
        if vat_type.get("id") == vat_id:
            if vat_type.get("is_disabled"):
                return None, (f"vat_id {vat_id} ({vat_type.get('description')}) è disattivata "
                              "nell'anagrafica IVA: scegline una attiva.")
            return vat_type, None
    options = [(t["id"], t.get("description")) for t in types if not t.get("is_disabled")]
    return None, f"vat_id {vat_id} non esiste tra le aliquote attive. Disponibili: {options}."


def build_entity_from_client(client_id, client_data=None):
    if not client_data:
        client_data = get_client_by_id(client_id)
    if not client_data:
        return None
    ei_code = get_ei_code_for_client(client_id)
    entity = {
        "id": client_id,
        "name": client_data.get("name", ""),
        "vat_number": client_data.get("vat_number", ""),
        "tax_code": client_data.get("tax_code", ""),
        "address_street": client_data.get("address_street", ""),
        "address_city": client_data.get("address_city", ""),
        "address_postal_code": client_data.get("address_postal_code", ""),
        "address_province": client_data.get("address_province", ""),
        "country": client_data.get("country", "Italia"),
        "ei_code": ei_code,
    }
    pec = (client_data.get("certified_email") or "").strip()
    if pec:
        entity["certified_email"] = pec
    return entity


def build_items_list(items_data, negate=False):
    """Build the items payload. Returns (items, error): the VAT rate has to be
    resolved to a vat type id, since FIC ignores the percentage in the body."""
    if not isinstance(items_data, list):
        return None, f"items deve essere una lista di righe: ricevuto {type(items_data).__name__}."
    if not items_data:
        return None, ("items è vuoto: il documento resterebbe senza righe, con una rata di "
                      "importo zero, e su update_document cancellerebbe quelle esistenti.")
    items_list = []
    for item in items_data:
        position = len(items_list)
        if not isinstance(item, dict):
            return None, (f"La riga in posizione {position} non è un oggetto: "
                          f"ricevuto {item!r}.")
        if not (isinstance(item.get("name"), str) and item["name"].strip()):
            return None, (f"La riga in posizione {position} non ha un name valido: "
                          "serve una stringa non vuota.")
        for field in ("qty", "net_price"):
            value = item.get(field)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None, (f"La riga in posizione {position} ha {field} = {value!r}: "
                              "serve un numero.")
        if item.get("vat_id") is not None:
            vat_type, error = resolve_vat_id(item["vat_id"])
            if error:
                return None, error
        else:
            rate = item.get("vat_rate")
            vat_type, error = resolve_vat_type(22 if rate is None else rate)
            if error:
                return None, error
        discount = item.get("discount")
        if discount is not None and (isinstance(discount, bool)
                                     or not isinstance(discount, (int, float))
                                     or not 0 <= discount <= 100):
            return None, (f"La riga in posizione {position} ha discount = {discount!r}: "
                          "serve una percentuale fra 0 e 100.")
        net_price = item["net_price"]
        if negate:
            net_price = -abs(net_price)
        items_list.append({
            "name": item["name"],
            "description": item.get("description", ""),
            "qty": item["qty"],
            "net_price": net_price,
            "discount": discount or 0,
            # `value` is not sent (read-only): it is kept here only so the local
            # total matches what FIC will compute from the vat type.
            "vat": {"id": vat_type["id"]},
            "_vat_value": vat_type.get("value") or 0,
        })
    return items_list, None


def _item_net(item):
    """Taxable amount of a line. `discount` is a percentual value on the net
    price (OpenAPI: "Issued document item discount percentual value")."""
    qty = item.get("qty") or 0
    net_price = item.get("net_price") or 0
    discount = item.get("discount") or 0
    return qty * net_price * (1 - discount / 100)


def _vat_value(item):
    """Percentage of an item, from the local hint or from what FIC returned."""
    if "_vat_value" in item:
        return item["_vat_value"] or 0
    vat = item.get("vat") or {}
    return vat.get("value") or 0


def _strip_local_fields(items):
    return [{k: v for k, v in i.items() if not k.startswith("_")} for i in items]


def build_issued_document(doc_type, client_id, items_data, date_str, payment_days,
                          visible_subject, negate_prices=False, source_invoice_id=None,
                          revenue_center=None):
    payment_days, days_error = _payment_days_argument(payment_days)
    if days_error:
        return None, days_error
    invoice_date, date_error = _iso_date(date_str)
    if date_error:
        return None, date_error
    date_str = invoice_date.strftime("%Y-%m-%d")

    client_data = get_client_by_id(client_id)
    if not client_data:
        return None, f"Cliente con ID {client_id} non trovato"

    if revenue_center:
        known = fetch_revenue_centers(company_id=COMPANY_ID)
        if revenue_center not in known:
            return None, (
                f"revenue_center '{revenue_center}' non esiste. "
                f"Disponibili: {known}. Crearli da web FIC (Impostazioni → Centri di costo/ricavo)."
            )

    entity = build_entity_from_client(client_id, client_data)
    items_list, items_error = build_items_list(items_data, negate=False)
    if items_error:
        return None, items_error

    due_date = invoice_date + timedelta(days=payment_days)
    total_abs = sum(
        abs(_item_net(i)) * (1 + _vat_value(i) / 100)
        for i in items_list
    )
    result_total = -total_abs if negate_prices else total_abs
    items_list = _strip_local_fields(items_list)

    body_data = {
        "type": doc_type,
        "entity": entity,
        "date": date_str,
        "visible_subject": visible_subject,
        "items_list": items_list,
        "payments_list": [{
            "amount": round(total_abs, 2),
            "due_date": due_date.strftime("%Y-%m-%d"),
            "status": "not_paid",
            "payment_terms": {"days": payment_days, "type": "standard"}
        }]
    }

    if revenue_center:
        body_data["rc_center"] = revenue_center
    client_method = client_data.get("default_payment_method") or {}
    if client_method.get("id"):
        body_data["payment_method"] = {"id": client_method["id"]}
    if doc_type in ("invoice", "credit_note"):
        body_data["e_invoice"] = True
        body_data["ei_data"] = {
            "payment_method": client_method.get("ei_payment_method") or "MP05"
        }
    # `original_document` is not a writable field: it is absent from the spec's
    # IssuedDocument schema and the SDK model drops it, so the link was never
    # made. FIC links documents through /issued_documents/transform.

    response = issued_api.create_issued_document(
        company_id=COMPANY_ID,
        # the local total models the lines only: let FIC size the installment
        # from the document it builds (ritenuta, cassa, rivalsa, bollo)
        create_issued_document_request={"data": body_data, "options": {"fix_payments": True}}
    )
    d = response.data.to_dict()

    stored_total, stored_due_date = _stored_totals(
        d, result_total, due_date.strftime("%Y-%m-%d")
    )
    if negate_prices:
        stored_total = -abs(stored_total)
    result = {
        "success": True,
        "id": d.get("id"),
        "number": d.get("number"),
        "date": str(d.get("date", "")),
        "due_date": stored_due_date,
        "client": client_data.get("name"),
        "ei_code": entity.get("ei_code", "N/A"),
        "total": stored_total,
        "type": doc_type,
        "status": "bozza",
    }
    if revenue_center:
        result["revenue_center"] = revenue_center
    if source_invoice_id:
        result["source_invoice_id"] = source_invoice_id
    return result, None


@app.list_tools()
async def list_tools():
    item_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "minLength": 1, "description": "Nome prodotto/servizio"},
            "description": {"type": "string", "description": "Descrizione estesa"},
            "qty": {"type": "number", "description": "Quantità"},
            "net_price": {"type": "number", "description": "Prezzo netto unitario (sempre positivo)"},
            "vat_rate": {"type": "number", "description": "Aliquota IVA (es. 22). Risolta contro l'anagrafica IVA di FIC"},
            "vat_id": {"type": ["integer", "string"], "description": "ID aliquota IVA (opzionale, vince su vat_rate: serve quando più aliquote hanno la stessa percentuale ma natura diversa)"},
            "discount": {"type": "number", "minimum": 0, "maximum": 100, "description": "Sconto percentuale sulla riga (opzionale)"}
        },
        "required": ["name", "qty", "net_price"]
    }

    return [
        Tool(
            name="list_invoices",
            description="Lista documenti emessi. Ritorna {count, page, pages, truncated, documents}: una pagina di 100 documenti, con truncated=true se ce ne sono altre — in quel caso chiedi la pagina successiva con page. Parametri: year (int), month (int opzionale), query (str opzionale), type (str opzionale: invoice, credit_note, proforma — default: invoice), page (int opzionale)",
            inputSchema={
                "type": "object",
                "properties": {
                    "year": {"type": "integer", "description": "Anno (es. 2024)"},
                    "month": {"type": "integer", "description": "Mese 1-12 (opzionale)"},
                    "query": {"type": "string", "description": "Filtro testuale (opzionale, applicato alla pagina richiesta)"},
                    "type": {"type": "string", "description": "Tipo documento: invoice (default), credit_note, proforma"},
                    "page": {"type": "integer", "description": "Pagina dei risultati (default 1, 100 documenti per pagina — vedi truncated nella risposta)"}
                },
                "required": ["year"]
            },
            annotations=_ann(read_only=True, idempotent=True),
        ),
        Tool(
            name="get_invoice",
            description="Dettaglio documento per ID (fattura, NDC, proforma)",
            inputSchema={
                "type": "object",
                "properties": {
                    "document_id": {"type": "integer", "description": "ID documento"}
                },
                "required": ["document_id"]
            },
            annotations=_ann(read_only=True, idempotent=True),
        ),
        Tool(
            name="get_pdf_url",
            description="Restituisce URL PDF e link web di un documento (fattura, NDC, proforma)",
            inputSchema={
                "type": "object",
                "properties": {
                    "document_id": {"type": "integer", "description": "ID documento"}
                },
                "required": ["document_id"]
            },
            annotations=_ann(read_only=True, idempotent=True),
        ),
        Tool(
            name="list_clients",
            description="Lista clienti",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Filtro nome/ragione sociale (opzionale)"}
                }
            },
            annotations=_ann(read_only=True, idempotent=True),
        ),
        Tool(
            name="get_company_info",
            description="Info azienda collegata",
            inputSchema={"type": "object", "properties": {}},
            annotations=_ann(read_only=True, idempotent=True),
        ),
        Tool(
            name="create_client",
            description="Crea nuovo cliente in anagrafica",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Nome/Ragione sociale"},
                    "vat_number": {"type": "string", "description": "Partita IVA (opzionale)"},
                    "tax_code": {"type": "string", "description": "Codice fiscale (opzionale)"},
                    "ei_code": {"type": "string", "description": "Codice destinatario SDI (opzionale)"},
                    "certified_email": {"type": "string", "description": "PEC (opzionale)"},
                    "email": {"type": "string", "description": "Email ordinaria (opzionale)"},
                    "address_street": {"type": "string", "description": "Indirizzo (opzionale)"},
                    "address_city": {"type": "string", "description": "Città (opzionale)"},
                    "address_postal_code": {"type": "string", "description": "CAP (opzionale)"},
                    "address_province": {"type": "string", "description": "Provincia (opzionale)"},
                    "country": {"type": "string", "description": "Paese (default: Italia)"},
                    "phone": {"type": "string", "description": "Telefono (opzionale)"}
                },
                "required": ["name"]
            },
            annotations=_ann(),
        ),
        Tool(
            name="update_client",
            description="Aggiorna dati cliente esistente. Passa solo i campi da modificare.",
            inputSchema={
                "type": "object",
                "properties": {
                    "client_id": {"type": "integer", "description": "ID cliente"},
                    "name": {"type": "string", "description": "Nome/Ragione sociale (opzionale)"},
                    "vat_number": {"type": "string", "description": "Partita IVA (opzionale)"},
                    "tax_code": {"type": "string", "description": "Codice fiscale (opzionale)"},
                    "ei_code": {"type": "string", "description": "Codice destinatario SDI (opzionale)"},
                    "certified_email": {"type": "string", "description": "PEC (opzionale)"},
                    "email": {"type": "string", "description": "Email ordinaria (opzionale)"},
                    "address_street": {"type": "string", "description": "Indirizzo (opzionale)"},
                    "address_city": {"type": "string", "description": "Città (opzionale)"},
                    "address_postal_code": {"type": "string", "description": "CAP (opzionale)"},
                    "address_province": {"type": "string", "description": "Provincia (opzionale)"},
                    "phone": {"type": "string", "description": "Telefono (opzionale)"}
                },
                "required": ["client_id"]
            },
            annotations=_ann(idempotent=True),
        ),
        Tool(
            name="create_invoice",
            description="Crea nuova fattura (bozza). IMPORTANTE: Chiedere sempre conferma all'utente prima di eseguire.",
            inputSchema={
                "type": "object",
                "properties": {
                    "client_id": {"type": "integer", "description": "ID cliente"},
                    "items": {"type": "array", "minItems": 1, "items": item_schema},
                    "date": {"type": "string", "description": "Data YYYY-MM-DD (default: oggi)"},
                    "payment_days": {"type": "integer", "minimum": 0, "maximum": 3650, "description": "Giorni pagamento (default: 30)"},
                    "visible_subject": {"type": "string", "description": "Oggetto visibile"},
                    "revenue_center": {"type": "string", "description": "Centro di ricavo (opzionale, deve esistere — vedi list_cost_centers)"}
                },
                "required": ["client_id", "items"]
            },
            annotations=_ann(),
        ),
        Tool(
            name="create_credit_note",
            description="Crea nota di credito (bozza). Importi POSITIVI in input, resi negativi automaticamente. IMPORTANTE: Chiedere sempre conferma all'utente prima di eseguire.",
            inputSchema={
                "type": "object",
                "properties": {
                    "client_id": {"type": "integer", "description": "ID cliente"},
                    "items": {"type": "array", "minItems": 1, "items": item_schema},
                    "date": {"type": "string", "description": "Data YYYY-MM-DD (default: oggi)"},
                    "payment_days": {"type": "integer", "minimum": 0, "maximum": 3650, "description": "Giorni pagamento (default: 30)"},
                    "visible_subject": {"type": "string", "description": "Oggetto visibile"},
                    "source_invoice_id": {"type": "integer", "description": "ID fattura originale da stornare (opzionale). Riportato nella risposta, ma l'API non permette di creare il collegamento formale: va fatto dal pannello FIC"},
                    "revenue_center": {"type": "string", "description": "Centro di ricavo (opzionale, deve esistere — vedi list_cost_centers)"}
                },
                "required": ["client_id", "items"]
            },
            annotations=_ann(),
        ),
        Tool(
            name="create_proforma",
            description="Crea proforma (bozza). Non inviabile allo SDI. IMPORTANTE: Chiedere sempre conferma all'utente prima di eseguire.",
            inputSchema={
                "type": "object",
                "properties": {
                    "client_id": {"type": "integer", "description": "ID cliente"},
                    "items": {"type": "array", "minItems": 1, "items": item_schema},
                    "date": {"type": "string", "description": "Data YYYY-MM-DD (default: oggi)"},
                    "payment_days": {"type": "integer", "minimum": 0, "maximum": 3650, "description": "Giorni pagamento (default: 30)"},
                    "visible_subject": {"type": "string", "description": "Oggetto visibile"},
                    "revenue_center": {"type": "string", "description": "Centro di ricavo (opzionale, deve esistere — vedi list_cost_centers)"}
                },
                "required": ["client_id", "items"]
            },
            annotations=_ann(),
        ),
        Tool(
            name="convert_proforma_to_invoice",
            description="Converte una proforma in fattura elettronica (bozza). Di default elimina la proforma originale. IMPORTANTE: Chiedere sempre conferma all'utente prima di eseguire.",
            inputSchema={
                "type": "object",
                "properties": {
                    "document_id": {"type": "integer", "description": "ID proforma da convertire"},
                    "date": {"type": "string", "description": "Data fattura YYYY-MM-DD (default: data proforma)"},
                    "keep_proforma": {"type": "boolean", "description": "Mantieni la proforma originale (default: false)"},
                    "revenue_center": {"type": "string", "description": "Centro di ricavo (opzionale, eredita da proforma se non passato)"}
                },
                "required": ["document_id"]
            },
            annotations=_ann(destructive=True),
        ),
        Tool(
            name="update_document",
            description="Modifica parziale di un documento BOZZA (fattura, NDC, proforma). Passa solo i campi da aggiornare. Funziona solo su documenti non ancora inviati allo SDI. Rifiuta le modifiche che riscriverebbero lo scadenzario: se il documento ha un pagamento registrato su rata singola di cui cambierebbe il totale, azzerare prima il pagamento con set_payment; se ha più rate, il piano va modificato dal pannello FattureInCloud. IMPORTANTE: Chiedere sempre conferma all'utente prima di eseguire.",
            inputSchema={
                "type": "object",
                "properties": {
                    "document_id": {"type": "integer", "description": "ID documento da modificare"},
                    "date": {"type": "string", "description": "Nuova data YYYY-MM-DD (opzionale)"},
                    "visible_subject": {"type": "string", "description": "Nuovo oggetto visibile (opzionale)"},
                    "payment_days": {"type": "integer", "minimum": 0, "maximum": 3650, "description": "Nuovi giorni pagamento (opzionale)"},
                    "items": {
                        "type": "array",
                        "minItems": 1,
                        "items": item_schema,
                        "description": "Nuove righe documento (opzionale). Per NDC, importi sempre positivi."
                    },
                    "revenue_center": {"type": "string", "description": "Centro di ricavo (opzionale, mantiene quello esistente se non passato)"}
                },
                "required": ["document_id"]
            },
            annotations=_ann(idempotent=True),
        ),
        Tool(
            name="duplicate_invoice",
            description="Duplica una fattura esistente con nuova data (crea bozza). Solo fatture: per copiare una proforma usa convert_proforma_to_invoice con keep_proforma=true, altrimenti la proforma di origine viene eliminata. IMPORTANTE: Chiedere sempre conferma all'utente prima di eseguire.",
            inputSchema={
                "type": "object",
                "properties": {
                    "source_document_id": {"type": "integer", "description": "ID fattura da duplicare"},
                    "new_date": {"type": "string", "description": "Nuova data YYYY-MM-DD (default: oggi)"},
                    "payment_days": {"type": "integer", "minimum": 0, "maximum": 3650, "description": "Giorni pagamento (default: eredita da originale)"},
                    "description_replace": {
                        "type": "object",
                        "description": "Sostituzioni testo nella descrizione (es. 2025->2026)",
                        "properties": {
                            "old": {"type": "string"},
                            "new": {"type": "string"}
                        }
                    },
                    "revenue_center": {"type": "string", "description": "Centro di ricavo (opzionale, eredita dalla fattura sorgente se non passato)"}
                },
                "required": ["source_document_id"]
            },
            annotations=_ann(),
        ),
        Tool(
            name="delete_invoice",
            description="Elimina un documento BOZZA (fattura, NDC, proforma). ATTENZIONE: Azione irreversibile! Chiedere SEMPRE conferma esplicita all'utente.",
            inputSchema={
                "type": "object",
                "properties": {
                    "document_id": {"type": "integer", "description": "ID documento da eliminare"}
                },
                "required": ["document_id"]
            },
            annotations=_ann(destructive=True, idempotent=True),
        ),
        Tool(
            name="send_to_sdi",
            description="Invia fattura/NDC allo SDI. ATTENZIONE: Azione irreversibile! Chiedere SEMPRE conferma esplicita all'utente.",
            inputSchema={
                "type": "object",
                "properties": {
                    "document_id": {"type": "integer", "description": "ID documento da inviare"}
                },
                "required": ["document_id"]
            },
            annotations=_ann(),
        ),
        Tool(
            name="get_invoice_status",
            description="Controlla stato e-invoice/SDI di un documento",
            inputSchema={
                "type": "object",
                "properties": {
                    "document_id": {"type": "integer", "description": "ID documento"}
                },
                "required": ["document_id"]
            },
            annotations=_ann(read_only=True, idempotent=True),
        ),
        Tool(
            name="send_email",
            description="Invia copia cortesia via email al cliente. Requires FIC_SENDER_EMAIL to be configured in extension settings. IMPORTANTE: Chiedere conferma prima di eseguire.",
            inputSchema={
                "type": "object",
                "properties": {
                    "document_id": {"type": "integer", "description": "ID documento"},
                    "recipient_email": {"type": "string", "description": "Email destinatario (opzionale)"},
                    "subject": {"type": "string", "description": "Oggetto email (opzionale)"},
                    "body": {"type": "string", "description": "Corpo email (opzionale)"}
                },
                "required": ["document_id"]
            },
            annotations=_ann(),
        ),
        Tool(
            name="list_received_documents",
            description="Lista fatture PASSIVE (ricevute dai fornitori). Ritorna {count, page, pages, truncated, documents}: una pagina di 100 documenti, con truncated=true se ce ne sono altre — in quel caso chiedi la pagina successiva con page. Parametri: year, month (opzionale), type (opzionale: expense, passive_credit_note, passive_delivery_note, self_invoice), page (int opzionale)",
            inputSchema={
                "type": "object",
                "properties": {
                    "year": {"type": "integer", "description": "Anno"},
                    "month": {"type": "integer", "description": "Mese 1-12 (opzionale)"},
                    "type": {
                        "type": "string",
                        "enum": list(RECEIVED_DOCUMENT_TYPES),
                        "description": "Tipo: expense (default), passive_credit_note, passive_delivery_note, self_invoice"
                    },
                    "query": {"type": "string", "description": "Filtro testuale (opzionale, applicato alla pagina richiesta)"},
                    "page": {"type": "integer", "description": "Pagina dei risultati (default 1, 100 documenti per pagina — vedi truncated nella risposta)"}
                },
                "required": ["year"]
            },
            annotations=_ann(read_only=True, idempotent=True),
        ),
        Tool(
            name="get_situation",
            description="Dashboard anno: fatturato netto (fatture - NDC), incassato, da incassare, costi (lordi, note fornitore, totale), margine. Supporta filtro per cliente. Legge al massimo 10 pagine da 100 documenti per lista: oltre quel tetto la risposta porta parziale=true e i totali sono incompleti.",
            inputSchema={
                "type": "object",
                "properties": {
                    "year": {"type": "integer", "description": "Anno (default: corrente)"},
                    "client_name": {"type": "string", "description": "Filtro per nome cliente (opzionale, ricerca parziale)"}
                }
            },
            annotations=_ann(read_only=True, idempotent=True),
        ),
        Tool(
            name="check_numeration",
            description="Verifica continuità numerica delle fatture emesse per un dato anno. Legge al massimo 10 pagine da 100 fatture: oltre quel tetto la risposta porta parziale=true e continuous=null, e i buchi elencati possono essere fatture non lette.",
            inputSchema={
                "type": "object",
                "properties": {
                    "year": {"type": "integer", "description": "Anno da verificare (es. 2025)"}
                },
                "required": ["year"]
            },
            annotations=_ann(read_only=True, idempotent=True),
        ),
        Tool(
            name="list_cost_centers",
            description="Lista combinata di centri di costo e ricavo configurati in FattureInCloud. L'API FIC espone due liste separate (cost_centers per documenti ricevuti, revenue_centers per documenti emessi); questo tool ne ritorna l'unione deduplicata e ordinata, coerente con la vista 'Analisi centri c/r' della UI FIC. Le validation interne dei tool di mutazione sono type-specific: create_invoice/credit_note/proforma/update_document/duplicate_invoice/convert_proforma_to_invoice validano `revenue_center` contro la sola lista revenue_centers; create_received_document valida `cost_center` contro la sola lista cost_centers. Read-only.",
            inputSchema={"type": "object", "properties": {}},
            annotations=_ann(read_only=True, idempotent=True),
        ),
        Tool(
            name="list_payment_accounts",
            description="Lista dei conti di pagamento configurati in FattureInCloud (banche, casse, carte). Serve per valorizzare `payment_account` in set_payment. Read-only.",
            inputSchema={"type": "object", "properties": {}},
            annotations=_ann(read_only=True, idempotent=True),
        ),
        Tool(
            name="set_payment",
            description=(
                "Registra un incasso (documento emesso) o un pagamento (documento ricevuto) "
                "sulle scadenze del documento. Funziona anche su fatture già inviate allo SDI. "
                "Se il documento ha più rate serve payment_index: l'indice della rata, oppure "
                "\"all\" per saldarle tutte. IMPORTANTE: Chiedere sempre conferma all'utente prima di eseguire."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "document_id": {"type": "integer", "description": "ID del documento"},
                    "document_type": {
                        "type": "string",
                        "enum": ["issued", "received"],
                        "description": "issued = documento emesso (incasso), received = documento ricevuto (pagamento)"
                    },
                    "status": {
                        "type": "string",
                        "enum": ["paid", "not_paid"],
                        "description": "paid = registra incasso/pagamento, not_paid = annulla la registrazione"
                    },
                    "paid_date": {"type": "string", "description": "Data incasso/pagamento YYYY-MM-DD (default: oggi; su una rata già pagata resta la data registrata, così ripetere la chiamata non la sposta). Ignorata con status not_paid"},
                    "payment_account": {
                        "type": ["string", "integer"],
                        "description": "Conto su cui registrare: id numerico o nome (vedi list_payment_accounts). Opzionale; ignorato con status not_paid"
                    },
                    "payment_index": {
                        "type": ["integer", "string"],
                        "description": "Indice della rata (0-based) oppure \"all\" per tutte. Obbligatorio se il documento ha più rate"
                    }
                },
                "required": ["document_id", "document_type", "status"]
            },
            # status="not_paid" drops paid_date and payment_account, which this
            # server cannot reconstruct: the same tool registers and clears
            annotations=_ann(destructive=True, idempotent=True),
        ),
        Tool(
            name="get_received_document",
            description="Dettaglio fattura passiva (ricevuta da fornitore) per ID. Read-only.",
            inputSchema={
                "type": "object",
                "properties": {
                    "document_id": {"type": "integer", "description": "ID documento ricevuto"}
                },
                "required": ["document_id"]
            },
            annotations=_ann(read_only=True, idempotent=True),
        ),
        Tool(
            name="create_received_document",
            description="Crea documento passivo (fattura ricevuta, NDC ricevuta, DDT, autofattura) registrando una spesa. IMPORTANTE: Chiedere conferma all'utente prima di eseguire.",
            inputSchema={
                "type": "object",
                "properties": {
                    "supplier_name": {"type": "string", "minLength": 1, "description": "Nome/Ragione sociale del fornitore"},
                    "supplier_vat_number": {"type": "string", "description": "Partita IVA del fornitore (opzionale)"},
                    "type": {
                        "type": "string",
                        "enum": list(RECEIVED_DOCUMENT_TYPES),
                        "description": "Tipo: expense (default), passive_credit_note (NDC ricevuta), passive_delivery_note, self_invoice"
                    },
                    "date": {"type": "string", "description": "Data documento YYYY-MM-DD (default: oggi)"},
                    "amount_net": {"type": "number", "description": "Importo netto"},
                    "amount_vat": {"type": "number", "description": "Importo IVA"},
                    "category": {"type": "string", "description": "Categoria spesa (opzionale)"},
                    "description": {"type": "string", "description": "Descrizione/oggetto (opzionale)"},
                    "cost_center": {"type": "string", "description": "Centro di costo (opzionale, deve esistere — vedi list_cost_centers)"}
                },
                "required": ["supplier_name", "amount_net"]
            },
            annotations=_ann(),
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "list_invoices":
            year, year_error = _year_argument(arguments.get("year"))
            if year_error:
                return _error(year_error)
            month, month_error = _month_argument(arguments.get("month"))
            if month_error:
                return _error(month_error)
            query = arguments.get("query")
            doc_type = arguments.get("type", "invoice")

            q = f"date >= '{year}-01-01' and date <= '{year}-12-31'"
            if month:
                last_day = 31 if month in [1,3,5,7,8,10,12] else 30 if month in [4,6,9,11] else 29
                q = f"date >= '{year}-{month:02d}-01' and date <= '{year}-{month:02d}-{last_day}'"

            page, page_error = _page_argument(arguments.get("page"))
            if page_error:
                return _error(page_error)
            response = issued_api.list_issued_documents(
                company_id=COMPANY_ID, type=doc_type, q=q,
                per_page=100, page=page, fieldset="detailed"
            )
            invoices = []
            for doc in (response.data or []):
                d = doc.to_dict()
                inv = {
                    "id": d.get("id"),
                    "number": d.get("number"),
                    "date": str(d.get("date", "")),
                    "client": d.get("entity", {}).get("name") if d.get("entity") else None,
                    "total": get_total_from_doc(d),
                    "subject": d.get("subject"),
                    "description": d.get("visible_subject")
                }
                if d.get("rc_center"):
                    inv["revenue_center"] = d["rc_center"]
                if query:
                    search_text = f"{inv['client']} {inv['subject']} {inv['description']}".lower()
                    if query.lower() not in search_text:
                        continue
                invoices.append(inv)
            pages = _page_count(response)
            payload = {
                "count": len(invoices),
                "page": page,
                "pages": pages,
                "truncated": page < pages,
                "documents": invoices,
            }
            return [TextContent(type="text", text=json.dumps(payload, indent=2, ensure_ascii=False))]

        elif name == "get_invoice":
            doc_id = arguments["document_id"]
            response = issued_api.get_issued_document(
                company_id=COMPANY_ID, document_id=doc_id, fieldset="detailed"
            )
            d = response.data.to_dict()
            items = []
            for i in d.get("items_list", []):
                items.append({
                    "name": i.get("name"),
                    "description": i.get("description"),
                    "qty": i.get("qty"),
                    "net_price": i.get("net_price", 0),
                    "gross_price": i.get("gross_price", 0),
                    "vat": i.get("vat", {}).get("value") if i.get("vat") else None
                })
            payments = []
            for p in d.get("payments_list", []):
                pa = p.get("payment_account")
                if hasattr(pa, 'to_dict'):
                    pa = pa.to_dict()
                payments.append({
                    "amount": p.get("amount"),
                    "due_date": str(p.get("due_date", "")),
                    "status": _payment_status(p),
                    "paid_date": str(p.get("paid_date", "")) if p.get("paid_date") else None,
                    "payment_account_id": pa.get("id") if isinstance(pa, dict) else None
                })
            result = {
                "id": d.get("id"),
                "number": d.get("number"),
                "date": str(d.get("date", "")),
                "type": d.get("type"),
                "client_id": d.get("entity", {}).get("id") if d.get("entity") else None,
                "client": d.get("entity", {}).get("name") if d.get("entity") else None,
                "total": get_total_from_doc(d),
                "subject": d.get("subject"),
                "description": d.get("visible_subject"),
                "items": items,
                "payments": payments,
                "ei_status": d.get("ei_status"),
            }
            if d.get("rc_center"):
                result["revenue_center"] = d["rc_center"]
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        elif name == "get_pdf_url":
            doc_id = arguments["document_id"]
            response = issued_api.get_issued_document(
                company_id=COMPANY_ID, document_id=doc_id, fieldset="detailed"
            )
            d = response.data.to_dict()
            attachment_url = d.get("attachment_url") or d.get("url") or ""
            web_url = f"https://secure.fattureincloud.it/issued-documents-view-{doc_id}"
            result = {
                "id": doc_id,
                "number": d.get("number"),
                "type": d.get("type", "invoice"),
                "client": d.get("entity", {}).get("name") if d.get("entity") else "",
                "attachment_url": attachment_url,
                "web_url": web_url,
                "note": "attachment_url è il PDF diretto (se disponibile). web_url apre il documento nel browser FIC."
            }
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        elif name == "list_clients":
            query = arguments.get("query")
            response = clients_api.list_clients(company_id=COMPANY_ID, per_page=100)
            clients = []
            for c in (response.data or []):
                cd = c.to_dict()
                client = {"id": cd.get("id"), "name": cd.get("name"),
                          "vat": cd.get("vat_number"), "tax_code": cd.get("tax_code"),
                          "email": cd.get("email")}
                if query and query.lower() not in (client['name'] or '').lower():
                    continue
                clients.append(client)
            return [TextContent(type="text", text=json.dumps(clients, indent=2, ensure_ascii=False))]

        elif name == "get_company_info":
            response = companies_api.get_company_info(company_id=COMPANY_ID)
            d = response.data.to_dict()
            info = d.get("info", d)
            result = {"name": info.get("name"), "vat": info.get("vat_number"),
                      "email": info.get("email"), "address": info.get("address_street"),
                      "city": info.get("address_city"), "province": info.get("address_province")}
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        elif name == "create_client":
            client_data = {
                "name": arguments["name"],
                "vat_number": arguments.get("vat_number", ""),
                "tax_code": arguments.get("tax_code", ""),
                "ei_code": arguments.get("ei_code", ""),
                "certified_email": arguments.get("certified_email", ""),
                "email": arguments.get("email", ""),
                "address_street": arguments.get("address_street", ""),
                "address_city": arguments.get("address_city", ""),
                "address_postal_code": arguments.get("address_postal_code", ""),
                "address_province": arguments.get("address_province", ""),
                "country": arguments.get("country", "Italia"),
                "phone": arguments.get("phone", ""),
            }
            response = clients_api.create_client(
                company_id=COMPANY_ID,
                create_client_request={"data": client_data}
            )
            d = response.data.to_dict()
            result = {
                "success": True,
                "id": d.get("id"),
                "name": d.get("name"),
                "vat_number": d.get("vat_number"),
                "message": f"Cliente '{d.get('name')}' creato con ID {d.get('id')}."
            }
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        elif name == "update_client":
            client_id = arguments["client_id"]
            orig = get_client_by_id(client_id)
            if not orig:
                return [TextContent(type="text", text=json.dumps({"success": False, "error": f"Cliente {client_id} non trovato"}, ensure_ascii=False))]
            fields = ["name", "vat_number", "tax_code", "ei_code", "certified_email",
                      "email", "address_street", "address_city", "address_postal_code",
                      "address_province", "phone"]
            client_data = {}
            for f in fields:
                if f in arguments:
                    client_data[f] = arguments[f]
                elif orig.get(f) is not None:
                    client_data[f] = orig[f]
            response = clients_api.modify_client(
                company_id=COMPANY_ID,
                client_id=client_id,
                modify_client_request={"data": client_data}
            )
            d = response.data.to_dict()
            result = {
                "success": True,
                "id": d.get("id"),
                "name": d.get("name"),
                "message": f"Cliente '{d.get('name')}' aggiornato con successo."
            }
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        elif name == "create_invoice":
            result, error = build_issued_document(
                doc_type="invoice",
                client_id=arguments["client_id"],
                items_data=arguments["items"],
                date_str=arguments.get("date"),
                payment_days=arguments.get("payment_days"),
                visible_subject=arguments.get("visible_subject", ""),
                revenue_center=arguments.get("revenue_center"),
            )
            if error:
                return [TextContent(type="text", text=json.dumps({"success": False, "error": error}, ensure_ascii=False))]
            result["message"] = f"Fattura #{result['number']} creata come bozza. SDI: {result['ei_code']}. Usa send_to_sdi per inviarla."
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        elif name == "create_credit_note":
            result, error = build_issued_document(
                doc_type="credit_note",
                client_id=arguments["client_id"],
                items_data=arguments["items"],
                date_str=arguments.get("date"),
                payment_days=arguments.get("payment_days"),
                visible_subject=arguments.get("visible_subject", ""),
                negate_prices=True,
                source_invoice_id=arguments.get("source_invoice_id"),
                revenue_center=arguments.get("revenue_center"),
            )
            if error:
                return [TextContent(type="text", text=json.dumps({"success": False, "error": error}, ensure_ascii=False))]
            msg = f"NDC #{result['number']} creata come bozza. Totale: {result['total']}."
            if result.get("source_invoice_id"):
                msg += (f" Riferita alla fattura ID {result['source_invoice_id']}, ma il "
                        "collegamento formale non è impostato: l'API lo consente solo "
                        "trasformando il documento, da fare dal pannello FattureInCloud.")
            msg += " Usa send_to_sdi per inviarla."
            result["message"] = msg
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        elif name == "create_proforma":
            result, error = build_issued_document(
                doc_type="proforma",
                client_id=arguments["client_id"],
                items_data=arguments["items"],
                date_str=arguments.get("date"),
                payment_days=arguments.get("payment_days"),
                visible_subject=arguments.get("visible_subject", ""),
                revenue_center=arguments.get("revenue_center"),
            )
            if error:
                return [TextContent(type="text", text=json.dumps({"success": False, "error": error}, ensure_ascii=False))]
            result["message"] = f"Proforma #{result['number']} creata come bozza. Non inviabile allo SDI."
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        elif name == "convert_proforma_to_invoice":
            doc_id = arguments["document_id"]
            keep_proforma = arguments.get("keep_proforma", False)

            orig_resp = issued_api.get_issued_document(
                company_id=COMPANY_ID, document_id=doc_id, fieldset="detailed"
            )
            orig = orig_resp.data.to_dict()

            if orig.get("type") != "proforma":
                return [TextContent(type="text", text=json.dumps({
                    "success": False,
                    "error": f"Il documento {doc_id} non è una proforma (tipo: {orig.get('type')})"
                }, ensure_ascii=False))]

            client_id = orig.get("entity", {}).get("id")
            client_data = get_client_by_id(client_id) if client_id else None
            entity = build_entity_from_client(client_id, client_data) if (client_id and client_data) else orig.get("entity", {})

            invoice_date, date_error = _iso_date(
                arguments.get("date"), default=str(orig.get("date") or "")[:10] or None
            )
            if date_error:
                return _error(date_error)
            date_str = invoice_date.strftime("%Y-%m-%d")

            items_list = []
            for i in orig.get("items_list", []):
                item = {k: v for k, v in i.items() if v is not None and k != "id"}
                item["net_price"] = abs(item.get("net_price", 0))
                items_list.append(item)

            payment_days = _payment_days_of(orig)
            payment_terms_type = _enum_value(_payment_terms_of(orig).get("type")) or "standard"

            due_date = _due_date(invoice_date, payment_days, payment_terms_type)
            total_gross = sum(_item_net(i) * (1 + _vat_value(i) / 100) for i in items_list)

            revenue_center = arguments.get("revenue_center") or orig.get("rc_center")
            if revenue_center:
                known = fetch_revenue_centers(company_id=COMPANY_ID)
                if revenue_center not in known:
                    return [TextContent(type="text", text=json.dumps({
                        "success": False,
                        "error": f"revenue_center '{revenue_center}' non esiste. Disponibili: {known}."
                    }, ensure_ascii=False))]

            body_data = {
                "type": "invoice",
                "e_invoice": True,
                "ei_data": _ei_data_of(orig, default_payment_method="MP05"),
                "entity": entity,
                "date": date_str,
                "visible_subject": orig.get("visible_subject", ""),
                "items_list": items_list,
                "payments_list": [{
                    "amount": round(total_gross, 2),
                    "due_date": due_date.strftime("%Y-%m-%d"),
                    "status": "not_paid",
                    "payment_terms": {"days": payment_days, "type": payment_terms_type}
                }]
            }
            # an explicit null is not an omitted field: only send what exists
            if (orig.get("payment_method") or {}).get("id"):
                body_data["payment_method"] = {"id": orig["payment_method"]["id"]}
            # the lines carry apply_withholding_taxes: without the document-level
            # percentages the invoice is due a different amount than the proforma
            body_data.update(_amount_modifiers_of(orig))
            if revenue_center:
                body_data["rc_center"] = revenue_center
            body = {"data": body_data, "options": {"fix_payments": True}}

            response = issued_api.create_issued_document(
                company_id=COMPANY_ID, create_issued_document_request=body
            )
            d = response.data.to_dict()

            if not keep_proforma:
                issued_api.delete_issued_document(company_id=COMPANY_ID, document_id=doc_id)

            stored_total, stored_due_date = _stored_totals(
                d, total_gross, due_date.strftime("%Y-%m-%d")
            )
            result = {
                "success": True,
                "invoice_id": d.get("id"),
                "invoice_number": d.get("number"),
                "date": date_str,
                "due_date": stored_due_date,
                "client": (client_data or {}).get("name", entity.get("name", "")),
                "ei_code": entity.get("ei_code", "N/A"),
                "total": stored_total,
                "proforma_deleted": not keep_proforma,
                "message": f"Fattura #{d.get('number')} creata da proforma #{orig.get('number')}. {'Proforma eliminata.' if not keep_proforma else 'Proforma mantenuta.'} Usa send_to_sdi per inviarla."
            }
            if revenue_center:
                result["revenue_center"] = revenue_center
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        elif name == "update_document":
            doc_id = arguments["document_id"]

            orig_resp = issued_api.get_issued_document(
                company_id=COMPANY_ID, document_id=doc_id, fieldset="detailed"
            )
            orig = orig_resp.data.to_dict()

            current_status = orig.get("ei_status")
            if current_status and current_status not in [None, "not_sent"]:
                return [TextContent(type="text", text=json.dumps({
                    "success": False,
                    "error": f"Impossibile modificare: documento già inviato allo SDI. Stato: {current_status}"
                }, ensure_ascii=False))]

            doc_type = _enum_value(orig.get("type")) or "invoice"
            is_credit_note = (doc_type == "credit_note")

            invoice_date, date_error = _iso_date(
                arguments.get("date"), default=str(orig.get("date") or "")[:10] or None
            )
            if date_error:
                return _error(date_error)
            date_str = invoice_date.strftime("%Y-%m-%d")
            visible_subject = arguments.get("visible_subject") if "visible_subject" in arguments else (orig.get("visible_subject") or "")

            if "items" in arguments:
                items_list, items_error = build_items_list(arguments["items"], negate=False)
                if items_error:
                    return _error(items_error)
            else:
                # The body replaces the stored array, so echo each item as it
                # came back — product_id, discount, apply_withholding_taxes and
                # the vat type id all have to survive an unrelated edit.
                items_list = []
                for i in orig.get("items_list", []):
                    item = {k: v for k, v in i.items() if v is not None}
                    item["net_price"] = abs(item.get("net_price", 0))
                    items_list.append(item)

            orig_days = _payment_days_of(orig)
            payment_days, days_error = _payment_days_argument(
                arguments.get("payment_days"), default=orig_days
            )
            if days_error:
                return _error(days_error)
            payment_terms_type = _enum_value(_payment_terms_of(orig).get("type")) or "standard"

            due_date = _due_date(invoice_date, payment_days, payment_terms_type)
            total_abs = sum(
                abs(_item_net(i)) * (1 + _vat_value(i) / 100)
                for i in items_list
            )
            result_total = -total_abs if is_credit_note else total_abs
            items_list = _strip_local_fields(items_list)

            client_id = orig.get("entity", {}).get("id")
            client_data = get_client_by_id(client_id) if client_id else None
            entity = build_entity_from_client(client_id, client_data) if (client_id and client_data) else orig.get("entity", {})

            if "revenue_center" in arguments:
                revenue_center = arguments["revenue_center"]
            else:
                revenue_center = orig.get("rc_center")
            if revenue_center:
                known = fetch_revenue_centers(company_id=COMPANY_ID)
                if revenue_center not in known:
                    return [TextContent(type="text", text=json.dumps({
                        "success": False,
                        "error": f"revenue_center '{revenue_center}' non esiste. Disponibili: {known}."
                    }, ensure_ascii=False))]

            # Payments carry the registered incasso (status/paid_date/payment_account).
            # Rebuilding them unconditionally wipes it and collapses N installments
            # into one, so rebuild only when the schedule or the total really moved
            # (argument presence is not enough: clients echo back unchanged fields).
            existing_payments = [_payment_entry(p) for p in (orig.get("payments_list") or [])]
            existing_total = round(sum(p.get("amount") or 0 for p in existing_payments), 2)
            schedule_moved = (
                date_str[:10] != str(orig.get("date", ""))[:10]
                or payment_days != orig_days
            )
            # The local total models the lines only, so it is comparable with
            # what FIC stored only when the caller actually edited the lines.
            # Credit note installments can be stored negative, hence the
            # magnitude comparison.
            items_edited = "items" in arguments
            total_moved = items_edited and round(total_abs, 2) != abs(existing_total)
            if existing_payments and not items_edited:
                # the lines were not touched, so the document's total is the one
                # FIC stored, not the one the lines add up to
                result_total = -abs(existing_total) if is_credit_note else existing_total

            registered = [p for p in existing_payments if p.get("status") != "not_paid"]

            # A rebuilt installment is sized from the lines, which do not model
            # ritenuta, cassa, rivalsa or bollo: ask FIC to size it from the
            # document instead. Preserved installments are the user's own plan,
            # so they are never handed to fix_payments.
            rebuilt = False

            if existing_payments and not (schedule_moved or total_moved):
                payments_list = existing_payments
            elif (registered and total_moved) or len(existing_payments) > 1:
                if registered:
                    reason = (
                        f"ha {len(registered)} rata/e con pagamento registrato: modificarne "
                        "importo o scadenze riscriverebbe un incasso già contabilizzato"
                    )
                    if total_moved:
                        modifiers = _amount_modifiers_of(orig, AMOUNT_MODIFIER_RATES)
                        if modifiers:
                            # the local total models the lines only, so it is not
                            # comparable with what FIC derived from these
                            reason += (", e il totale non è riproducibile qui perché il documento "
                                       f"ha importi calcolati a livello documento "
                                       f"({', '.join(sorted(modifiers))})")
                        else:
                            reason += (f" (totale ricalcolato {round(total_abs, 2)}, "
                                       f"somma rate {abs(existing_total)})")
                else:
                    reason = (f"ha un piano di {len(existing_payments)} rate: ricostruirlo lo "
                              "ridurrebbe a un'unica scadenza, perdendo la rateizzazione")
                # clearing the payment only unblocks a single-installment document:
                # with a plan, len(existing_payments) > 1 refuses it again.
                recovery = (
                    "Azzera prima il pagamento con set_payment (status='not_paid') e ripeti, "
                    "oppure modifica il documento dal pannello FattureInCloud."
                    if registered and len(existing_payments) == 1 else
                    "Il piano rate va modificato dal pannello FattureInCloud."
                )
                return _error(
                    f"Il documento {reason}. {recovery}",
                    payments=[
                        {"index": i, "amount": p.get("amount"), "due_date": p.get("due_date"),
                         "status": p.get("status")}
                        for i, p in enumerate(existing_payments)
                    ],
                )
            elif registered:
                # single settled installment, same total: only the due date moves
                payment = dict(existing_payments[0])
                rebuilt = False
                payment["due_date"] = due_date.strftime("%Y-%m-%d")
                payment["payment_terms"] = {"days": payment_days, "type": payment_terms_type}
                payments_list = [payment]
            else:
                rebuilt = True
                payments_list = [{
                    # with the lines untouched the amount is the document's own,
                    # unless it stores none: the sum of nothing is not a total
                    "amount": (existing_total if existing_payments and not items_edited
                               else round(total_abs, 2)),
                    "due_date": due_date.strftime("%Y-%m-%d"),
                    "status": "not_paid",
                    "payment_terms": {"days": payment_days, "type": payment_terms_type}
                }]

            body_data = {
                "type": doc_type,
                "entity": entity,
                "date": date_str,
                "visible_subject": visible_subject,
                "items_list": items_list,
                "payments_list": payments_list
            }
            if (orig.get("payment_method") or {}).get("id"):
                body_data["payment_method"] = {"id": orig["payment_method"]["id"]}
            if revenue_center:
                body_data["rc_center"] = revenue_center
            if orig.get("show_totals"):
                body_data["show_totals"] = _enum_value(orig["show_totals"])
            if doc_type in ("invoice", "credit_note"):
                # `ei_data` applies only to e-invoices, and the payment method is
                # the document's own: overwriting it with MP05 on an unrelated
                # edit would change what the XML declares.
                body_data["e_invoice"] = True if orig.get("e_invoice") is None else bool(orig["e_invoice"])
                if body_data["e_invoice"]:
                    ei_data = _ei_data_of(orig)
                    if ei_data:
                        body_data["ei_data"] = ei_data

            request = {"data": body_data}
            if rebuilt:
                request["options"] = {"fix_payments": True}
            response = issued_api.modify_issued_document(
                company_id=COMPANY_ID, document_id=doc_id,
                modify_issued_document_request=request
            )
            d = response.data.to_dict()
            stored_total, stored_due_date = _stored_totals(
                d, result_total, due_date.strftime("%Y-%m-%d")
            )
            if is_credit_note:
                stored_total = -abs(stored_total)

            result = {
                "success": True,
                "id": d.get("id"),
                "number": d.get("number"),
                "date": str(d.get("date", "")),
                "due_date": stored_due_date,
                "client": (client_data or {}).get("name", entity.get("name", "")),
                "total": stored_total,
                "type": doc_type,
                "status": "bozza",
                "message": f"Documento #{d.get('number')} aggiornato con successo."
            }
            if revenue_center:
                result["revenue_center"] = revenue_center
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        elif name == "duplicate_invoice":
            source_id = arguments["source_document_id"]
            invoice_date, date_error = _iso_date(arguments.get("new_date"), field="new_date")
            if date_error:
                return _error(date_error)
            new_date_str = invoice_date.strftime("%Y-%m-%d")
            desc_replace = arguments.get("description_replace", {})

            response = issued_api.get_issued_document(
                company_id=COMPANY_ID, document_id=source_id, fieldset="detailed"
            )
            orig = response.data.to_dict()

            # the body below is built as an invoice: duplicating a credit note
            # would turn a reversal into a debit document with the same lines
            source_type = _enum_value(orig.get("type")) or "invoice"
            if source_type != "invoice":
                # convert_proforma_to_invoice deletes the source unless
                # keep_proforma is set, and whoever asked for a copy wants the
                # original kept
                recovery = ("Usa convert_proforma_to_invoice con keep_proforma=true "
                            "(senza, la proforma di origine viene eliminata)."
                            if source_type == "proforma"
                            else "Duplicalo dal pannello FattureInCloud.")
                return _error(
                    f"Il documento {source_id} è di tipo '{source_type}': duplicate_invoice "
                    f"crea solo fatture. {recovery}"
                )

            client_id = orig.get("entity", {}).get("id")
            client_data = get_client_by_id(client_id) if client_id else None
            entity = build_entity_from_client(client_id, client_data) if (client_id and client_data) else orig.get("entity", {})

            items_list = []
            for i in orig.get("items_list", []):
                iname = i.get("name", "")
                idesc = i.get("description", "")
                if desc_replace.get("old") and desc_replace.get("new"):
                    iname = iname.replace(desc_replace["old"], desc_replace["new"])
                    idesc = idesc.replace(desc_replace["old"], desc_replace["new"])
                item = {k: v for k, v in i.items() if v is not None and k != "id"}
                item["name"] = iname
                item["description"] = idesc
                items_list.append(item)

            visible_subject = orig.get("visible_subject", "")
            if desc_replace.get("old") and desc_replace.get("new"):
                visible_subject = visible_subject.replace(desc_replace["old"], desc_replace["new"])

            payment_days, days_error = _payment_days_argument(
                arguments.get("payment_days"), default=_payment_days_of(orig)
            )
            if days_error:
                return _error(days_error)
            payment_terms_type = _enum_value(_payment_terms_of(orig).get("type")) or "standard"

            due_date = _due_date(invoice_date, payment_days, payment_terms_type)
            total_gross = sum(_item_net(i) * (1 + _vat_value(i) / 100) for i in items_list)

            revenue_center = arguments.get("revenue_center") or orig.get("rc_center")
            if revenue_center:
                known = fetch_revenue_centers(company_id=COMPANY_ID)
                if revenue_center not in known:
                    return [TextContent(type="text", text=json.dumps({
                        "success": False,
                        "error": f"revenue_center '{revenue_center}' non esiste. Disponibili: {known}."
                    }, ensure_ascii=False))]

            body_data = {
                "type": "invoice",
                "e_invoice": True if orig.get("e_invoice") is None else bool(orig["e_invoice"]),
                "entity": entity, "date": new_date_str, "visible_subject": visible_subject,
                "items_list": items_list,
                "payments_list": [{"amount": round(total_gross, 2),
                                   "due_date": due_date.strftime("%Y-%m-%d"),
                                   "status": "not_paid",
                                   "payment_terms": {"days": payment_days, "type": payment_terms_type}}]
            }
            if body_data["e_invoice"]:
                body_data["ei_data"] = _ei_data_of(orig, default_payment_method="MP05")
            if (orig.get("payment_method") or {}).get("id"):
                body_data["payment_method"] = {"id": orig["payment_method"]["id"]}
            # the lines carry apply_withholding_taxes: without the document-level
            # percentages the copy is due a different amount than the original
            body_data.update(_amount_modifiers_of(orig))
            if revenue_center:
                body_data["rc_center"] = revenue_center
            body = {"data": body_data, "options": {"fix_payments": True}}
            response = issued_api.create_issued_document(
                company_id=COMPANY_ID, create_issued_document_request=body
            )
            d = response.data.to_dict()
            stored_total, stored_due_date = _stored_totals(
                d, total_gross, due_date.strftime("%Y-%m-%d")
            )
            result = {
                "success": True, "id": d.get("id"), "number": d.get("number"),
                "date": str(d.get("date", "")), "due_date": stored_due_date,
                "client": (client_data or {}).get("name", entity.get("name", "")),
                "ei_code": entity.get("ei_code", "N/A"), "total": stored_total,
                "source_invoice": orig.get("number"), "status": "bozza",
                "message": f"Fattura #{d.get('number')} creata come bozza (duplicata da #{orig.get('number')}). Scadenza: {stored_due_date}."
            }
            if revenue_center:
                result["revenue_center"] = revenue_center
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        elif name == "delete_invoice":
            doc_id = arguments["document_id"]
            check = issued_api.get_issued_document(
                company_id=COMPANY_ID, document_id=doc_id, fieldset="detailed"
            )
            check_data = check.data.to_dict()
            current_status = check_data.get("ei_status")
            if current_status and current_status not in ["null", "not_sent", None]:
                return [TextContent(type="text", text=json.dumps({
                    "success": False,
                    "error": f"Impossibile eliminare: documento già inviato allo SDI. Stato: {current_status}"
                }, ensure_ascii=False))]
            issued_api.delete_issued_document(company_id=COMPANY_ID, document_id=doc_id)
            result = {
                "success": True, "document_id": doc_id,
                "number": check_data.get("number"),
                "client": check_data.get("entity", {}).get("name"),
                "message": f"Documento #{check_data.get('number')} eliminato con successo."
            }
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        elif name == "send_to_sdi":
            doc_id = arguments["document_id"]
            check = issued_api.get_issued_document(
                company_id=COMPANY_ID, document_id=doc_id, fieldset="detailed"
            )
            check_data = check.data.to_dict()
            current_status = check_data.get("ei_status")
            if current_status and current_status not in ["null", "rejected", None, "not_sent"]:
                return [TextContent(type="text", text=json.dumps({
                    "success": False,
                    "error": f"Documento già inviato o in elaborazione. Stato: {current_status}"
                }, ensure_ascii=False))]
            einvoice_api.send_e_invoice(
                company_id=COMPANY_ID, document_id=doc_id,
                send_e_invoice_request={"data": {"withholding_tax_causal": None}}
            )
            result = {
                "success": True, "document_id": doc_id,
                "number": check_data.get("number"),
                "client": check_data.get("entity", {}).get("name"),
                "message": f"Fattura #{check_data.get('number')} inviata allo SDI con successo!"
            }
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        elif name == "get_invoice_status":
            doc_id = arguments["document_id"]
            response = issued_api.get_issued_document(
                company_id=COMPANY_ID, document_id=doc_id, fieldset="detailed"
            )
            d = response.data.to_dict()
            ei_status = d.get("ei_status")
            status_map = {
                None: "Bozza (non inviata)", "not_sent": "Bozza (non inviata)",
                "pending": "In attesa di invio", "sent": "Inviata, in attesa di risposta SDI",
                "delivered": "Consegnata al destinatario", "accepted": "Accettata",
                "rejected": "Rifiutata", "not_delivered": "Non consegnata (messa a disposizione)"
            }
            result = {
                "id": d.get("id"), "number": d.get("number"),
                "client": d.get("entity", {}).get("name"),
                "ei_status": ei_status,
                "ei_status_description": status_map.get(ei_status, ei_status),
                "date": str(d.get("date", ""))
            }
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        elif name == "send_email":
            if not SENDER_EMAIL:
                return [TextContent(type="text", text=json.dumps({
                    "success": False,
                    "error": "❌ Sender email not configured. Open Claude Desktop → Extensions → FattureInCloud → Settings and set the 'Sender email' field, then retry the operation."
                }, ensure_ascii=False))]
            doc_id = arguments["document_id"]
            check = issued_api.get_issued_document(
                company_id=COMPANY_ID, document_id=doc_id, fieldset="detailed"
            )
            check_data = check.data.to_dict()
            recipient_email = arguments.get("recipient_email") or check_data.get("entity", {}).get("email", "")
            if not recipient_email:
                return [TextContent(type="text", text=json.dumps({
                    "success": False, "error": "Nessuna email specificata e cliente senza email in anagrafica"
                }, ensure_ascii=False))]
            email_data = {"data": {
                "sender_email": SENDER_EMAIL, "recipient_email": recipient_email, "cc_email": "",
                "subject": arguments.get("subject") or f"Fattura n. {check_data.get('number')}",
                "body": arguments.get("body") or f"In allegato la fattura n. {check_data.get('number')}.\n\nCordiali saluti.",
                "include": {"document": True, "delivery_note": False, "attachment": False, "accompanying_invoice": False},
                "attach_pdf": True, "send_copy": False
            }}
            issued_api.schedule_email(
                company_id=COMPANY_ID, document_id=doc_id, schedule_email_request=email_data
            )
            result = {
                "success": True, "document_id": doc_id,
                "number": check_data.get("number"), "recipient": recipient_email,
                "message": f"Email con documento #{check_data.get('number')} inviata a {recipient_email}"
            }
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        elif name == "list_received_documents":
            year, year_error = _year_argument(arguments.get("year"))
            if year_error:
                return _error(year_error)
            month, month_error = _month_argument(arguments.get("month"))
            if month_error:
                return _error(month_error)
            doc_type, type_error = _received_document_type(arguments.get("type", "expense"))
            if type_error:
                return _error(type_error)
            query = arguments.get("query")
            q = f"date >= '{year}-01-01' and date <= '{year}-12-31'"
            if month:
                last_day = 31 if month in [1,3,5,7,8,10,12] else 30 if month in [4,6,9,11] else 29
                q = f"date >= '{year}-{month:02d}-01' and date <= '{year}-{month:02d}-{last_day}'"
            page, page_error = _page_argument(arguments.get("page"))
            if page_error:
                return _error(page_error)
            response = received_api.list_received_documents(
                company_id=COMPANY_ID, type=doc_type, q=q,
                per_page=100, page=page, fieldset="detailed"
            )
            docs = []
            for doc in (response.data or []):
                d = doc.to_dict()
                supplier_name = d.get('entity', {}).get('name', '') if d.get('entity') else ''
                desc = d.get('description', '') or ''
                if query and query.lower() not in f"{supplier_name} {desc}".lower():
                    continue
                entry = {
                    "id": d.get("id"), "number": d.get("number"),
                    "date": str(d.get("date", "")), "supplier": supplier_name,
                    "description": desc[:80], "total": _gross_of(d)
                }
                if d.get("rc_center"):
                    entry["cost_center"] = d["rc_center"]
                docs.append(entry)
            pages = _page_count(response)
            payload = {
                "count": len(docs),
                "page": page,
                "pages": pages,
                "truncated": page < pages,
                "documents": docs,
            }
            return [TextContent(type="text", text=json.dumps(payload, indent=2, ensure_ascii=False))]

        elif name == "get_situation":
            year, year_error = _year_argument(arguments.get("year"))
            if year_error:
                return _error(year_error)
            client_filter = (arguments.get("client_name") or "").lower().strip()
            q = f"date >= '{year}-01-01' and date <= '{year}-12-31'"

            emesse, parziale = _all_pages(
                issued_api.list_issued_documents,
                company_id=COMPANY_ID, type="invoice", q=q, fieldset="detailed"
            )
            note_credito, truncated = _all_pages(
                issued_api.list_issued_documents,
                company_id=COMPANY_ID, type="credit_note", q=q, fieldset="detailed"
            )
            parziale = parziale or truncated

            totale_fatturato = totale_incassato = totale_ndc = 0
            fatture_non_pagate = []

            for doc in emesse:
                d = doc.to_dict()
                client_name = d.get('entity', {}).get('name', '') if d.get('entity') else ''
                if client_filter and client_filter not in client_name.lower():
                    continue
                totale_fatturato += get_total_from_doc(d)
                for p in d.get('payments_list', []):
                    status = _payment_status(p)
                    if status == 'paid':
                        totale_incassato += p.get('amount', 0)
                    else:  # not_paid, reversed, or anything FIC adds later
                        fatture_non_pagate.append({
                            "number": d.get("number"),
                            "client": client_name,
                            "amount": p.get('amount', 0),
                            "due_date": str(p.get('due_date', ''))
                        })

            for doc in note_credito:
                d = doc.to_dict()
                client_name = d.get('entity', {}).get('name', '') if d.get('entity') else ''
                if client_filter and client_filter not in client_name.lower():
                    continue
                totale_ndc += abs(get_total_from_doc(d))

            fatturato_netto = totale_fatturato - totale_ndc

            totale_costi = totale_note_fornitore = 0
            if not client_filter:
                ricevute, costi_truncated = _all_pages(
                    received_api.list_received_documents,
                    company_id=COMPANY_ID, type="expense", q=q, fieldset="detailed"
                )
                note_fornitore, note_truncated = _all_pages(
                    received_api.list_received_documents,
                    company_id=COMPANY_ID, type="passive_credit_note", q=q, fieldset="detailed"
                )
                parziale = parziale or costi_truncated or note_truncated
                # revenue is gross (it comes from the installments), so costs have
                # to be gross too or the margin absorbs the purchase VAT; and a
                # supplier credit note reduces them, as an issued one reduces
                # revenue. self_invoice stays out: counting a reverse-charge
                # self-invoice would double the cost.
                totale_note_fornitore = sum(abs(_gross_of(d.to_dict())) for d in note_fornitore)
                totale_costi = sum(_gross_of(d.to_dict()) for d in ricevute) - totale_note_fornitore

            fatture_non_pagate.sort(key=lambda x: x.get('due_date', ''))
            result = {
                "anno": year,
                "filtro_cliente": client_filter or None,
                "fatturato_lordo": round(totale_fatturato, 2),
                "note_credito": round(totale_ndc, 2),
                "fatturato_netto": round(fatturato_netto, 2),
                "incassato": round(totale_incassato, 2),
                "da_incassare": round(fatturato_netto - totale_incassato, 2),
                # same three terms as the revenue side, so lordo - note = totale
                # reads by one rule on both halves
                "costi_lordi": round(totale_costi + totale_note_fornitore, 2) if not client_filter else "N/A (filtro cliente attivo)",
                "costi_totali": round(totale_costi, 2) if not client_filter else "N/A (filtro cliente attivo)",
                "note_fornitore": round(totale_note_fornitore, 2) if not client_filter else "N/A (filtro cliente attivo)",
                "margine_lordo": round(fatturato_netto - totale_costi, 2) if not client_filter else "N/A",
                "prossime_scadenze": fatture_non_pagate[:10],
            }
            if parziale:
                # the totals are real but incomplete: say so rather than let a
                # capped read look like the whole year
                result["parziale"] = True
                result["nota"] = (
                    f"Lette al massimo {MAX_PAGES} pagine da 100 documenti per lista: "
                    f"l'anno {year} ne contiene di più, quindi i totali sono parziali."
                )
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        elif name == "check_numeration":
            year, year_error = _year_argument(arguments.get("year"))
            if year_error:
                return _error(year_error)
            q = f"date >= '{year}-01-01' and date <= '{year}-12-31'"
            listed, parziale = _all_pages(
                issued_api.list_issued_documents,
                company_id=COMPANY_ID, type="invoice", q=q
            )
            docs = [d.to_dict() for d in listed]
            if not docs:
                return [TextContent(type="text", text=json.dumps({
                    "year": year, "status": "Nessuna fattura trovata per questo anno"
                }, ensure_ascii=False))]
            numbers = sorted(set(
                d.get("number") for d in docs
                if d.get("number") is not None and d.get("number") > 0
            ))
            gaps = []
            if numbers:
                if numbers[0] != 1:
                    gaps.append({"type": "start", "expected": 1, "actual": numbers[0],
                                 "missing": list(range(1, numbers[0])),
                                 "note": f"La numerazione parte da {numbers[0]} invece che da 1"})
                for i in range(len(numbers) - 1):
                    if numbers[i + 1] - numbers[i] > 1:
                        missing = list(range(numbers[i] + 1, numbers[i + 1]))
                        gaps.append({"type": "gap", "after": numbers[i], "before": numbers[i + 1],
                                     "missing": missing,
                                     "note": f"Mancano i numeri {missing} tra fattura {numbers[i]} e {numbers[i+1]}"})
            result = {
                "year": year, "total_invoices": len(numbers),
                "first_number": numbers[0] if numbers else None,
                "last_number": numbers[-1] if numbers else None,
                # a truncated read verified nothing about the invoices it never
                # fetched: continuous would assert exactly that
                "continuous": None if parziale else len(gaps) == 0,
                "status": (
                    ("? Verifica parziale: "
                     + (f"{len(gaps)} possibili buchi fra le fatture lette" if gaps
                        else "nessun buco fra le fatture lette"))
                    if parziale else
                    f"⚠ Trovati {len(gaps)} problemi" if gaps else
                    "✓ Numerazione continua"
                ),
                "gaps": gaps
            }
            if parziale:
                # the invoices that were never fetched read as missing numbers
                result["parziale"] = True
                result["nota"] = (
                    f"Lette al massimo {MAX_PAGES} pagine da 100 fatture: la verifica è "
                    f"parziale e i buchi elencati possono essere fatture non lette."
                )
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        elif name == "list_cost_centers":
            cost = fetch_cost_centers(company_id=COMPANY_ID)
            revenue = fetch_revenue_centers(company_id=COMPANY_ID)
            centers = sorted(set(cost) | set(revenue))
            return [TextContent(type="text", text=json.dumps(centers, indent=2, ensure_ascii=False))]

        elif name == "list_payment_accounts":
            accounts = fetch_payment_accounts(company_id=COMPANY_ID)
            return [TextContent(type="text", text=json.dumps(accounts, indent=2, ensure_ascii=False))]

        elif name == "set_payment":
            doc_id = arguments["document_id"]
            doc_kind = arguments["document_type"]
            status = arguments["status"]
            if doc_kind not in ("issued", "received"):
                return _error("document_type deve essere 'issued' o 'received'.")
            if status not in ("paid", "not_paid"):
                return _error("status deve essere 'paid' o 'not_paid'.")

            paid_date = datetime.now().strftime("%Y-%m-%d")
            if status == "paid" and arguments.get("paid_date") is not None:
                paid_date = str(arguments["paid_date"]).strip()
                try:
                    paid_date = datetime.strptime(paid_date, "%Y-%m-%d").strftime("%Y-%m-%d")
                except ValueError:
                    return _error(
                        f"paid_date '{arguments['paid_date']}' non valida: usa il formato YYYY-MM-DD."
                    )

            account = None
            if status == "paid" and arguments.get("payment_account") is not None:
                account, account_error = resolve_payment_account(arguments["payment_account"])
                if account_error:
                    return _error(account_error)

            if doc_kind == "issued":
                response = issued_api.get_issued_document(
                    company_id=COMPANY_ID, document_id=doc_id, fieldset="detailed"
                )
            else:
                response = received_api.get_received_document(
                    company_id=COMPANY_ID, document_id=doc_id, fieldset="detailed"
                )
            d = response.data.to_dict()

            payments = [_payment_entry(p) for p in (d.get("payments_list") or [])]
            nota_importo = None
            if not payments:
                if doc_kind == "issued":
                    return _error(
                        f"Documento {doc_id} senza scadenze di pagamento: impossibile registrare l'incasso."
                    )
                if status == "not_paid":
                    return _error(
                        f"Il documento {doc_id} non ha scadenze registrate: non c'è nessun "
                        f"pagamento da annullare."
                    )
                amount = round(_amount_due_of(d), 2)
                lordo = round(_gross_of(d), 2)
                ritenuta = round(lordo - amount, 2)
                # a synthesized installment reads exactly like a stored one, so
                # the response says which of the two it is every time
                nota_importo = (
                    f"Il documento non aveva scadenze: rata creata per {amount}, "
                    + (f"cioè il lordo {lordo} meno {ritenuta} di ritenuta, che il "
                       f"committente versa all'Erario e non al fornitore."
                       if ritenuta else "pari al totale lordo del documento.")
                )
                payments = [{
                    "amount": amount,
                    "due_date": str(d.get("date", ""))[:10],
                    "status": "not_paid",
                }]

            index = arguments.get("payment_index")
            last = len(payments) - 1
            if index is None:
                if len(payments) > 1:
                    return _error(
                        f"{len(payments)} rate presenti: specifica payment_index (0-{last}) "
                        f'oppure "all" per saldarle tutte.',
                        payments=[
                            {"index": i, "amount": p.get("amount"),
                             "due_date": p.get("due_date"), "status": p.get("status")}
                            for i, p in enumerate(payments)
                        ],
                    )
                targets = [0]
            elif isinstance(index, str) and index.strip().lower() == "all":
                targets = list(range(len(payments)))
            else:
                if isinstance(index, bool) or not isinstance(index, (int, str)):
                    return _error('payment_index deve essere un intero oppure "all".')
                if isinstance(index, str) and not index.strip().lstrip("+-").isascii():
                    return _error('payment_index deve essere un intero oppure "all".')
                try:
                    wanted = int(index)
                except ValueError:
                    return _error('payment_index deve essere un intero oppure "all".')
                if not 0 <= wanted <= last:
                    return _error(
                        f"payment_index {index} fuori intervallo: il documento ha "
                        f"{len(payments)} rate (0-{last})."
                    )
                targets = [wanted]

            for i in targets:
                entry = payments[i]
                stored_status = entry.get("status")
                entry["status"] = status
                if status == "paid":
                    # replaying the call must not move an already registered
                    # payment — but a reversed one was not collected, so the
                    # date it carries is not the date of this collection
                    if (arguments.get("paid_date") is not None
                            or stored_status != "paid"
                            or not entry.get("paid_date")):
                        entry["paid_date"] = paid_date
                    if account:
                        entry["payment_account"] = {"id": account["id"]}
                        if account.get("type"):
                            entry["payment_account"]["type"] = account["type"]
                else:
                    entry.pop("paid_date", None)
                    entry.pop("payment_account", None)

            # The SDK request models default `type` (invoice / expense) and
            # `show_totals`, and those defaults are serialized into the PUT: echo
            # the document's own values so a credit note stays a credit note.
            data = {"payments_list": payments}
            if d.get("type"):
                data["type"] = _enum_value(d["type"])
            if doc_kind == "issued":
                if d.get("show_totals"):
                    data["show_totals"] = _enum_value(d["show_totals"])
            else:
                # ModifyReceivedDocumentRequest marks data.entity required
                # (openapi-enriched.yaml), unlike the issued document one.
                entity = d.get("entity") or {}
                entity = {k: v for k, v in entity.items() if v is not None}
                if entity:
                    data["entity"] = entity
            body = {"data": data}
            if doc_kind == "issued":
                response = issued_api.modify_issued_document(
                    company_id=COMPANY_ID, document_id=doc_id,
                    modify_issued_document_request=body
                )
            else:
                response = received_api.modify_received_document(
                    company_id=COMPANY_ID, document_id=doc_id,
                    modify_received_document_request=body
                )
            updated = response.data.to_dict()

            stored = updated.get("payments_list")
            if stored is not None and not stored:
                # `or` used to treat this like an omitted field and report the
                # installments we sent: a state the document does not carry
                perso = ("il pagamento non risulta registrato" if status == "paid"
                         else "il documento ha perso il piano rate")
                return _error(
                    f"Il documento {doc_id} è tornato dall'API senza scadenze: {perso}. "
                    f"Verificalo dal pannello FattureInCloud prima di riprovare."
                )

            # the names are decoration on a write that already happened: a
            # registry unreadable now must not report that write as failed
            try:
                account_names = {a["id"]: a["name"] for a in fetch_payment_accounts(company_id=COMPANY_ID)}
            except Exception as e:
                print(f"[{name}] conti non leggibili dopo la scrittura: {e!r}", file=sys.stderr)
                account_names = None
            totale_pagato = residuo = 0.0
            view = []
            for i, p in enumerate(stored or payments):
                entry = _payment_entry(p)
                amount = entry.get("amount") or 0
                row = {
                    "index": i,
                    "amount": amount,
                    "due_date": entry.get("due_date"),
                    "status": entry.get("status"),
                }
                if entry.get("paid_date"):
                    row["paid_date"] = entry["paid_date"]
                if entry.get("payment_account"):
                    # the id is the stored one; a name nobody could read is
                    # left out rather than reported as null
                    account_id = entry["payment_account"]["id"]
                    row["payment_account"] = {"id": account_id}
                    if account_id in (account_names or {}):
                        row["payment_account"]["name"] = account_names[account_id]
                if entry.get("status") == "paid":
                    totale_pagato += amount
                else:
                    residuo += amount
                view.append(row)

            # FIC does not document what a PUT does with the fields it is not
            # given; staff describe it as a merge, but nothing guarantees it.
            # The response is the document as stored, so check it rather than
            # reporting a clean success over a document that lost its content.
            # the checks are independent, so they accumulate: assigning one
            # variable meant the last one to fire erased what the others found
            warnings = []
            if account_names is None and any("payment_account" in r for r in view):
                warnings.append(
                    "Nomi dei conti non disponibili: l'anagrafica conti non è leggibile al "
                    "momento, gli id sono quelli registrati sul documento."
                )
            if stored is None:
                # to_dict() drops keys whose value is None, so the schedule may
                # simply not have been reported: the rows below are then the ones
                # sent, and saying so is the difference from claiming they were read
                warnings.append(
                    "La risposta dell'API non riporta le scadenze: le rate qui sotto sono "
                    "quelle inviate, non quelle rilette dal documento. Verificale dal "
                    "pannello FattureInCloud."
                )
            # these two compare against {} and [] on purpose, unlike the check on
            # payments_list above: an absent key makes the response assert nothing
            # — `counterparty` falls back to the document read a moment earlier,
            # which is a real read, and the line items are not reported at all.
            # Both run on either kind: an issued document with no lines would
            # otherwise have nothing watching it.
            if (d.get("entity") or {}) and updated.get("entity") == {}:
                warnings.append(
                    f"Il documento è tornato dall'API senza "
                    f"{'fornitore' if doc_kind == 'received' else 'cliente'}: la PUT potrebbe aver "
                    "sostituito il documento invece di aggiornarne solo le rate. Verificalo "
                    "dal pannello FattureInCloud prima di registrare altri pagamenti."
                )
            if (d.get("items_list") or []) and updated.get("items_list") == []:
                warnings.append(
                    "Il documento è tornato dall'API senza items_list: la PUT potrebbe aver "
                    "sostituito il documento invece di aggiornarne solo le rate. Verifica le "
                    "righe dal pannello FattureInCloud prima di registrare altri pagamenti."
                )
            warning = " ".join(warnings) or None

            number = updated.get("number") or d.get("number") or d.get("invoice_number")
            counterparty = (updated.get("entity") or d.get("entity") or {}).get("name")
            verb = "Incasso" if doc_kind == "issued" else "Pagamento"
            azione = "registrato" if status == "paid" else "annullato"
            result = {
                "success": True,
                "id": d.get("id", doc_id),
                "document_type": doc_kind,
                "number": number,
                "counterparty": counterparty,
                "payments": view,
                "totale_pagato": round(totale_pagato, 2),
                "residuo": round(residuo, 2),
                "message": f"{verb} {azione} su {len(targets)} rata/e del documento #{number}.",
            }
            if nota_importo:
                result["nota_importo"] = nota_importo
            if warning:
                result["warning"] = warning
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        elif name == "get_received_document":
            doc_id = arguments["document_id"]
            response = received_api.get_received_document(
                company_id=COMPANY_ID, document_id=doc_id, fieldset="detailed"
            )
            d = response.data.to_dict()
            items = []
            for i in d.get("items_list", []) or []:
                items.append({
                    "name": i.get("name"),
                    "description": i.get("description"),
                    "qty": i.get("qty"),
                    "net_price": i.get("net_price", 0),
                    "vat": i.get("vat", {}).get("value") if i.get("vat") else None
                })
            payments = []
            for p in d.get("payments_list", []) or []:
                payments.append({
                    "amount": p.get("amount"),
                    "due_date": str(p.get("due_date", "")),
                    "status": _payment_status(p),
                    "paid_date": str(p.get("paid_date", "")) if p.get("paid_date") else None,
                })
            result = {
                "id": d.get("id"),
                "type": d.get("type"),
                "number": d.get("invoice_number"),
                "date": str(d.get("date", "")),
                "supplier": d.get("entity", {}).get("name") if d.get("entity") else None,
                "supplier_vat": d.get("entity", {}).get("vat_number") if d.get("entity") else None,
                "description": d.get("description"),
                "category": d.get("category"),
                "amount_net": d.get("amount_net"),
                "amount_vat": d.get("amount_vat"),
                "amount_gross": _gross_of(d),
                # what the supplier is actually paid: set_payment registers this
                # amount, so the gross alone reads as an unexplained difference
                "amount_withholding_tax": d.get("amount_withholding_tax"),
                "amount_other_withholding_tax": d.get("amount_other_withholding_tax"),
                "amount_due": round(_amount_due_of(d), 2),
                "items": items,
                "payments": payments,
            }
            if d.get("rc_center"):
                result["cost_center"] = d["rc_center"]
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        elif name == "create_received_document":
            cost_center = arguments.get("cost_center")
            if cost_center:
                known = fetch_cost_centers(company_id=COMPANY_ID)
                if cost_center not in known:
                    return [TextContent(type="text", text=json.dumps({
                        "success": False,
                        "error": f"cost_center '{cost_center}' non esiste. Disponibili: {known}."
                    }, ensure_ascii=False))]

            doc_type, type_error = _received_document_type(arguments.get("type", "expense"))
            if type_error:
                return _error(type_error)
            if not (isinstance(arguments.get("supplier_name"), str)
                    and arguments["supplier_name"].strip()):
                return _error("supplier_name non valido: serve una stringa non vuota.")
            amount_net = arguments.get("amount_net")
            amount_vat = arguments.get("amount_vat")
            amount_vat = 0 if amount_vat is None else amount_vat
            for field, value in (("amount_net", amount_net), ("amount_vat", amount_vat)):
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    return _error(f"{field} = {value!r}: serve un numero.")

            document_date, date_error = _iso_date(arguments.get("date"))
            if date_error:
                return _error(date_error)
            date_str = document_date.strftime("%Y-%m-%d")

            entity = {"name": arguments["supplier_name"]}
            if arguments.get("supplier_vat_number"):
                entity["vat_number"] = arguments["supplier_vat_number"]

            body_data = {
                "type": doc_type,
                "entity": entity,
                "date": date_str,
                "amount_net": amount_net,
                "amount_vat": amount_vat,
                # no amount_gross: it is read-only, so the SDK drops it from the
                # request body and FIC computes it from the two amounts above
            }
            if arguments.get("category"):
                body_data["category"] = arguments["category"]
            if arguments.get("description"):
                body_data["description"] = arguments["description"]
            if cost_center:
                body_data["rc_center"] = cost_center

            response = received_api.create_received_document(
                company_id=COMPANY_ID,
                create_received_document_request={"data": body_data}
            )
            d = response.data.to_dict()
            result = {
                "success": True,
                "id": d.get("id"),
                "type": d.get("type"),
                "supplier": d.get("entity", {}).get("name") if d.get("entity") else arguments["supplier_name"],
                "date": str(d.get("date", "")),
                "amount_gross": _gross_of(d),
                "message": f"Documento ricevuto creato (ID {d.get('id')}).",
            }
            if cost_center:
                result["cost_center"] = cost_center
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        else:
            return [TextContent(type="text", text=f"Tool '{name}' non trovato")]

    except Exception as e:
        # the traceback carries local paths and internal structure, and an MCP
        # client feeds whatever comes back into the conversation: it belongs on
        # stderr, where whoever runs the server can read it
        print(f"[{name}] {traceback.format_exc()}", file=sys.stderr)
        if isinstance(e, ApiException):
            # what FIC refused is exactly what the caller has to act on; str(e)
            # would add the response headers, which help nobody
            # these four belong to the generated runtime, not to a declared API:
            # an AttributeError raised here would escape call_tool and leave the
            # client without a response at all
            status = getattr(e, "status", None)
            reason = getattr(e, "reason", None)
            # __init__ fills body from the raw response, but leaves it None when
            # that decode raises: the refusal is then only in the parsed data
            refusal = getattr(e, "data", None) or getattr(e, "body", None)
            if status or reason or refusal:
                detail = " ".join(str(x) for x in (status, reason) if x)
                if refusal:
                    detail = f"{detail}: {refusal}" if detail else str(refusal)
                return _error(f"{type(e).__name__}: {detail}")
            # none of the four: fall through, since the generic message at least
            # names the tool and says the detail is in the server log
        return _error(
            f"{type(e).__name__} durante '{name}'. Il dettaglio è nel log del server."
        )


async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
