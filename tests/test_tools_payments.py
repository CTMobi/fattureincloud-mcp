"""Mock-only tests for payment registration (incassi e pagamenti).

Covers:
- list_payment_accounts tool + fetch_payment_accounts caching
- set_payment on issued documents (incassi) and received documents (pagamenti)
- installment selection (single / explicit index / "all") and the ambiguity guard
- payment_account resolution by id and by name
- update_document no longer wiping an already registered payment

No real FIC API calls. All SDK methods are mocked. Safe to run in CI.
"""

import asyncio
import importlib
import json
import sys
from datetime import datetime

import pytest
from unittest.mock import MagicMock, patch

from fattureincloud_python_sdk.models.issued_document import IssuedDocument
from fattureincloud_python_sdk.models.modify_issued_document_request import ModifyIssuedDocumentRequest
from fattureincloud_python_sdk.models.received_document import ReceivedDocument


VAT_TYPES = [
    {"id": 0, "value": 22.0, "description": "22%", "is_disabled": False, "default": True},
    {"id": 1, "value": 4.0, "description": "4%", "is_disabled": False, "default": False},
    {"id": 3, "value": 10.0, "description": "10%", "is_disabled": False, "default": False},
    {"id": 6, "value": 0.0, "description": "Non imponibile", "is_disabled": False, "default": False},
    {"id": 9, "value": 22.0, "description": "22% dismessa", "is_disabled": True, "default": False},
]

ACCOUNTS = [
    {"id": 110, "name": "Banca Intesa", "type": "standard", "virtual": False},
    {"id": 111, "name": "Cassa contanti", "type": "standard", "virtual": False},
]


@pytest.fixture
def server_module(tmp_path, monkeypatch):
    monkeypatch.setenv("FIC_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv("FIC_CACHE_DISABLED", raising=False)
    monkeypatch.setenv("FIC_ACCESS_TOKEN", "test_token")
    monkeypatch.setenv("FIC_COMPANY_ID", "100")
    monkeypatch.setenv("FIC_SENDER_EMAIL", "test@example.invalid")

    for mod in ("server", "cache"):
        if mod in sys.modules:
            del sys.modules[mod]

    server = importlib.import_module("server")

    with patch.object(server.info_api, "list_payment_accounts",
                      return_value=_accounts_response(ACCOUNTS)), \
         patch.object(server.info_api, "list_vat_types",
                      return_value=_vat_types_response()):
        yield server


def _vat_types_response():
    response = MagicMock()
    response.data = [_sdk_obj(v) for v in VAT_TYPES]
    return response


def _accounts_response(accounts):
    response = MagicMock()
    response.data = [_sdk_obj(a) for a in accounts]
    return response


def _sdk_obj(payload):
    """A stand-in for an SDK model instance exposing to_dict()."""
    obj = MagicMock()
    obj.to_dict.return_value = dict(payload)
    return obj


def _run(coro):
    return asyncio.run(coro)


def _doc_response(payload):
    response = MagicMock()
    response.data.to_dict.return_value = payload
    return response


def _issued_doc(payments, ei_status=None, doc_id=42):
    return {
        "id": doc_id,
        "number": 7,
        "type": "invoice",
        "date": "2026-01-10",
        "entity": {"id": 5, "name": "Acme"},
        "visible_subject": "Consulenza",
        "items_list": [{
            "name": "Item", "qty": 1, "net_price": 1000.0,
            "gross_price": 1220.0, "vat": {"id": 0, "value": 22},
        }],
        "payments_list": payments,
        "ei_status": ei_status,
    }


def _received_doc(payments, doc_id=99):
    return {
        "id": doc_id,
        "type": "expense",
        "invoice_number": "2026/123",
        "date": "2026-01-10",
        "entity": {"name": "Supplier Srl", "vat_number": "98765432109"},
        "description": "Affitto",
        "amount_net": 500.0,
        "amount_vat": 110.0,
        "amount_gross": 610.0,
        "items_list": [],
        "payments_list": payments,
    }


def _rate(amount, due_date, status="not_paid", **extra):
    entry = {"amount": amount, "due_date": due_date, "status": status}
    entry.update(extra)
    return entry


def _sent_data(modify_mock, key):
    """`data` payload actually sent to the FIC modify endpoint."""
    return modify_mock.call_args.kwargs[key]["data"]


def _sent_payments(modify_mock, key):
    """payments_list actually sent to the FIC modify endpoint."""
    return _sent_data(modify_mock, key)["payments_list"]


def _sdk_shaped(payload):
    """Round-trip a document payload through the SDK model, so the fixture
    carries what the API really returns: enum members, date objects, and no
    read-only fields."""
    return IssuedDocument.from_dict(payload).to_dict()


# --------------------------------------------------------------------------
# list_payment_accounts
# --------------------------------------------------------------------------

def test_list_payment_accounts_returns_accounts(server_module):
    server = server_module
    result = _run(server.call_tool("list_payment_accounts", {}))
    payload = json.loads(result[0].text)
    assert payload == ACCOUNTS


def test_fetch_payment_accounts_caches_repeated_calls(server_module):
    server = server_module
    with patch.object(server.info_api, "list_payment_accounts",
                      return_value=_accounts_response(ACCOUNTS)) as m:
        for _ in range(5):
            server.fetch_payment_accounts(company_id=100)
    assert m.call_count == 1


# --------------------------------------------------------------------------
# set_payment — issued documents (incassi)
# --------------------------------------------------------------------------

def test_set_payment_issued_single_installment(server_module):
    server = server_module
    doc = _issued_doc([_rate(1220.0, "2026-02-09")])
    paid = _issued_doc([_rate(1220.0, "2026-02-09", "paid",
                              paid_date="2026-02-05",
                              payment_account={"id": 110, "name": "Banca Intesa"})])

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document",
                      return_value=_doc_response(paid)) as modify:
        result = _run(server.call_tool("set_payment", {
            "document_id": 42,
            "document_type": "issued",
            "status": "paid",
            "paid_date": "2026-02-05",
            "payment_account": 110,
        }))

    sent = _sent_payments(modify, "modify_issued_document_request")
    assert sent == [{
        "amount": 1220.0,
        "due_date": "2026-02-09",
        "status": "paid",
        "paid_date": "2026-02-05",
        "payment_account": {"id": 110},
    }]

    payload = json.loads(result[0].text)
    assert payload["success"] is True
    assert payload["totale_pagato"] == 1220.0
    assert payload["residuo"] == 0.0
    assert payload["payments"][0]["status"] == "paid"


def test_set_payment_defaults_paid_date_to_today(server_module):
    server = server_module
    doc = _issued_doc([_rate(1220.0, "2026-02-09")])

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document",
                      return_value=_doc_response(doc)) as modify:
        _run(server.call_tool("set_payment", {
            "document_id": 42, "document_type": "issued", "status": "paid",
        }))

    sent = _sent_payments(modify, "modify_issued_document_request")
    assert sent[0]["paid_date"] == datetime.now().strftime("%Y-%m-%d")


def test_set_payment_not_paid_clears_date_and_account(server_module):
    server = server_module
    doc = _issued_doc([_rate(1220.0, "2026-02-09", "paid",
                             paid_date="2026-02-05",
                             payment_account={"id": 110, "name": "Banca Intesa"})])

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document",
                      return_value=_doc_response(doc)) as modify:
        _run(server.call_tool("set_payment", {
            "document_id": 42, "document_type": "issued", "status": "not_paid",
        }))

    sent = _sent_payments(modify, "modify_issued_document_request")
    assert sent[0]["status"] == "not_paid"
    assert "paid_date" not in sent[0]
    assert "payment_account" not in sent[0]


def test_set_payment_works_on_document_sent_to_sdi(server_module):
    """Unlike update_document, registering an incasso must work after SDI send."""
    server = server_module
    doc = _issued_doc([_rate(1220.0, "2026-02-09")], ei_status="sent")

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document",
                      return_value=_doc_response(doc)) as modify:
        result = _run(server.call_tool("set_payment", {
            "document_id": 42, "document_type": "issued", "status": "paid",
        }))

    assert modify.called
    assert json.loads(result[0].text)["success"] is True


def test_set_payment_preserves_payment_terms_and_id(server_module):
    server = server_module
    doc = _issued_doc([_rate(1220.0, "2026-02-09", id=901,
                             payment_terms={"days": 30, "type": "standard"})])

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document",
                      return_value=_doc_response(doc)) as modify:
        _run(server.call_tool("set_payment", {
            "document_id": 42, "document_type": "issued", "status": "paid",
        }))

    sent = _sent_payments(modify, "modify_issued_document_request")
    assert sent[0]["id"] == 901
    assert sent[0]["payment_terms"] == {"days": 30, "type": "standard"}


# --------------------------------------------------------------------------
# set_payment — installment selection
# --------------------------------------------------------------------------

def test_set_payment_multiple_installments_requires_index(server_module):
    server = server_module
    doc = _issued_doc([
        _rate(1220.0, "2026-01-31"),
        _rate(1220.0, "2026-02-28"),
        _rate(1220.0, "2026-03-31"),
    ])

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document") as modify:
        result = _run(server.call_tool("set_payment", {
            "document_id": 42, "document_type": "issued", "status": "paid",
        }))

    assert not modify.called
    payload = json.loads(result[0].text)
    assert payload["success"] is False
    assert "payment_index" in payload["error"]
    assert [p["index"] for p in payload["payments"]] == [0, 1, 2]
    assert payload["payments"][1]["due_date"] == "2026-02-28"


def test_set_payment_explicit_index_touches_one_installment(server_module):
    server = server_module
    doc = _issued_doc([
        _rate(1220.0, "2026-01-31"),
        _rate(1220.0, "2026-02-28"),
    ])

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document",
                      return_value=_doc_response(doc)) as modify:
        _run(server.call_tool("set_payment", {
            "document_id": 42, "document_type": "issued", "status": "paid",
            "payment_index": 1, "paid_date": "2026-02-20",
        }))

    sent = _sent_payments(modify, "modify_issued_document_request")
    assert sent[0]["status"] == "not_paid"
    assert "paid_date" not in sent[0]
    assert sent[1]["status"] == "paid"
    assert sent[1]["paid_date"] == "2026-02-20"


def test_set_payment_index_all_marks_every_installment(server_module):
    server = server_module
    doc = _issued_doc([
        _rate(1220.0, "2026-01-31"),
        _rate(1220.0, "2026-02-28"),
    ])

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document",
                      return_value=_doc_response(doc)) as modify:
        _run(server.call_tool("set_payment", {
            "document_id": 42, "document_type": "issued", "status": "paid",
            "payment_index": "all", "paid_date": "2026-03-01",
        }))

    sent = _sent_payments(modify, "modify_issued_document_request")
    assert [p["status"] for p in sent] == ["paid", "paid"]


def test_set_payment_index_out_of_range_is_rejected(server_module):
    server = server_module
    doc = _issued_doc([_rate(1220.0, "2026-01-31")])

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document") as modify:
        result = _run(server.call_tool("set_payment", {
            "document_id": 42, "document_type": "issued", "status": "paid",
            "payment_index": 3,
        }))

    assert not modify.called
    payload = json.loads(result[0].text)
    assert payload["success"] is False
    assert "payment_index" in payload["error"]


# --------------------------------------------------------------------------
# set_payment — payment_account resolution
# --------------------------------------------------------------------------

def test_set_payment_resolves_account_by_name(server_module):
    server = server_module
    doc = _issued_doc([_rate(1220.0, "2026-02-09")])

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document",
                      return_value=_doc_response(doc)) as modify:
        _run(server.call_tool("set_payment", {
            "document_id": 42, "document_type": "issued", "status": "paid",
            "payment_account": "banca intesa",
        }))

    sent = _sent_payments(modify, "modify_issued_document_request")
    assert sent[0]["payment_account"] == {"id": 110}


def test_set_payment_unknown_account_is_rejected(server_module):
    server = server_module
    doc = _issued_doc([_rate(1220.0, "2026-02-09")])

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document") as modify:
        result = _run(server.call_tool("set_payment", {
            "document_id": 42, "document_type": "issued", "status": "paid",
            "payment_account": "Conto Inesistente",
        }))

    assert not modify.called
    payload = json.loads(result[0].text)
    assert payload["success"] is False
    assert "Banca Intesa" in payload["error"]


# --------------------------------------------------------------------------
# set_payment — received documents (pagamenti)
# --------------------------------------------------------------------------

def test_set_payment_received_single_installment(server_module):
    server = server_module
    doc = _received_doc([_rate(610.0, "2026-02-09")])

    with patch.object(server.received_api, "get_received_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.received_api, "modify_received_document",
                      return_value=_doc_response(doc)) as modify:
        result = _run(server.call_tool("set_payment", {
            "document_id": 99, "document_type": "received", "status": "paid",
            "paid_date": "2026-02-05", "payment_account": "Cassa contanti",
        }))

    sent = _sent_payments(modify, "modify_received_document_request")
    assert sent[0]["status"] == "paid"
    assert sent[0]["paid_date"] == "2026-02-05"
    assert sent[0]["payment_account"] == {"id": 111}
    assert json.loads(result[0].text)["success"] is True


def test_set_payment_received_without_installments_creates_one(server_module):
    """Documents created by create_received_document carry no payments_list."""
    server = server_module
    doc = _received_doc([])

    with patch.object(server.received_api, "get_received_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.received_api, "modify_received_document",
                      return_value=_doc_response(doc)) as modify:
        _run(server.call_tool("set_payment", {
            "document_id": 99, "document_type": "received", "status": "paid",
            "paid_date": "2026-02-05",
        }))

    sent = _sent_payments(modify, "modify_received_document_request")
    assert sent == [{
        "amount": 610.0,
        "due_date": "2026-01-10",
        "status": "paid",
        "paid_date": "2026-02-05",
    }]


# --------------------------------------------------------------------------
# update_document must not wipe a registered payment
# --------------------------------------------------------------------------

def test_update_document_preserves_registered_payment(server_module):
    server = server_module
    doc = _issued_doc([_rate(1220.0, "2026-02-09", "paid",
                             paid_date="2026-02-05",
                             payment_account={"id": 110, "name": "Banca Intesa"})])

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document",
                      return_value=_doc_response(doc)) as modify, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme"}):
        _run(server.call_tool("update_document", {
            "document_id": 42, "visible_subject": "Nuovo oggetto",
        }))

    sent = _sent_payments(modify, "modify_issued_document_request")
    assert sent[0]["status"] == "paid"
    assert sent[0]["paid_date"] == "2026-02-05"
    assert sent[0]["payment_account"] == {"id": 110}


def test_update_document_preserves_all_installments(server_module):
    server = server_module
    # installments always sum to the document total (1000 + 22% VAT)
    doc = _issued_doc([
        _rate(610.0, "2026-01-31", "paid", paid_date="2026-01-30"),
        _rate(610.0, "2026-02-28"),
    ])

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document",
                      return_value=_doc_response(doc)) as modify, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme"}):
        _run(server.call_tool("update_document", {
            "document_id": 42, "visible_subject": "Nuovo oggetto",
        }))

    sent = _sent_payments(modify, "modify_issued_document_request")
    assert len(sent) == 2
    assert [p["status"] for p in sent] == ["paid", "not_paid"]


def test_update_document_refuses_to_rewrite_a_registered_payment(server_module):
    """Changing the total of a document whose payment is already registered
    would report cash that was never collected: refuse instead."""
    server = server_module
    doc = _issued_doc([_rate(1220.0, "2026-02-09", "paid",
                             paid_date="2026-02-05",
                             payment_account={"id": 110, "name": "Banca Intesa"})])

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document") as modify, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme"}):
        result = _run(server.call_tool("update_document", {
            "document_id": 42,
            "items": [{"name": "Item", "qty": 1, "net_price": 2000.0, "vat_rate": 22}],
        }))

    assert not modify.called
    payload = json.loads(result[0].text)
    assert payload["success"] is False
    assert "set_payment" in payload["error"]


def test_update_document_refuses_recompute_on_paid_multi_installment(server_module):
    server = server_module
    doc = _issued_doc([
        _rate(500.0, "2026-01-31", "paid", paid_date="2026-01-30"),
        _rate(500.0, "2026-02-28"),
        _rate(500.0, "2026-03-31"),
    ])

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document") as modify, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme"}):
        result = _run(server.call_tool("update_document", {
            "document_id": 42, "date": "2026-02-01",
        }))

    assert not modify.called
    assert json.loads(result[0].text)["success"] is False


def test_update_document_reschedules_paid_installment_when_total_unchanged(server_module):
    """Moving the due date of a settled single-installment document is safe:
    the registered payment survives, only the schedule moves."""
    server = server_module
    doc = _issued_doc([_rate(1220.0, "2026-02-09", "paid",
                             paid_date="2026-02-05",
                             payment_account={"id": 110, "name": "Banca Intesa"})])

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document",
                      return_value=_doc_response(doc)) as modify, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme"}):
        _run(server.call_tool("update_document", {
            "document_id": 42, "payment_days": 60,
        }))

    sent = _sent_payments(modify, "modify_issued_document_request")
    assert len(sent) == 1
    assert sent[0]["amount"] == 1220.0
    assert sent[0]["status"] == "paid"
    assert sent[0]["paid_date"] == "2026-02-05"
    assert sent[0]["payment_account"] == {"id": 110}
    assert sent[0]["due_date"] == "2026-03-11"


def test_update_document_ignores_echoed_unchanged_fields(server_module):
    """MCP clients routinely echo back fields they just read: an identical
    date must not trigger a payments rebuild."""
    server = server_module
    doc = _issued_doc([
        _rate(610.0, "2026-01-31", "paid", paid_date="2026-01-30"),
        _rate(610.0, "2026-02-28"),
    ])

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document",
                      return_value=_doc_response(doc)) as modify, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme"}):
        _run(server.call_tool("update_document", {
            "document_id": 42, "date": "2026-01-10", "visible_subject": "Nuovo",
        }))

    sent = _sent_payments(modify, "modify_issued_document_request")
    assert len(sent) == 2
    assert [p["status"] for p in sent] == ["paid", "not_paid"]


# --------------------------------------------------------------------------
# document identity must survive the PUT
# --------------------------------------------------------------------------

def test_set_payment_keeps_document_type(server_module):
    """The SDK request model defaults `type` to invoice: echoing the document's
    own type is what stops a credit note from being re-typed."""
    server = server_module
    doc = _issued_doc([_rate(1220.0, "2026-02-09")])
    doc["type"] = "credit_note"
    doc["show_totals"] = "nets"

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document",
                      return_value=_doc_response(doc)) as modify:
        _run(server.call_tool("set_payment", {
            "document_id": 42, "document_type": "issued", "status": "paid",
        }))

    data = _sent_data(modify, "modify_issued_document_request")
    assert data["type"] == "credit_note"
    assert data["show_totals"] == "nets"
    # what actually goes over the wire, once the SDK coerces our dict
    serialized = ModifyIssuedDocumentRequest.from_dict({"data": data}).to_dict()
    assert serialized["data"]["type"] == "credit_note"
    assert serialized["data"]["show_totals"] == "nets"


def test_set_payment_keeps_received_document_type(server_module):
    server = server_module
    doc = _received_doc([_rate(610.0, "2026-02-09")])
    doc["type"] = "passive_credit_note"

    with patch.object(server.received_api, "get_received_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.received_api, "modify_received_document",
                      return_value=_doc_response(doc)) as modify:
        _run(server.call_tool("set_payment", {
            "document_id": 99, "document_type": "received", "status": "paid",
        }))

    assert _sent_data(modify, "modify_received_document_request")["type"] == "passive_credit_note"


def test_update_document_keeps_show_totals(server_module):
    server = server_module
    doc = _issued_doc([_rate(1220.0, "2026-02-09")])
    doc["show_totals"] = "nets"

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document",
                      return_value=_doc_response(doc)) as modify, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme"}):
        _run(server.call_tool("update_document", {
            "document_id": 42, "visible_subject": "Nuovo",
        }))

    assert _sent_data(modify, "modify_issued_document_request")["show_totals"] == "nets"


# --------------------------------------------------------------------------
# fixtures shaped like real SDK output (enums, date objects, read-only fields)
# --------------------------------------------------------------------------

def test_payment_status_normalizes_sdk_enum(server_module):
    server = server_module
    from fattureincloud_python_sdk.models.issued_document_status import IssuedDocumentStatus

    assert server._payment_status({"status": IssuedDocumentStatus.PAID}) == "paid"
    assert server._payment_status({"status": IssuedDocumentStatus.NOT_PAID}) == "not_paid"
    assert server._payment_status({"status": "reversed"}) == "reversed"
    assert server._payment_status({"status": None}) == ""
    assert server._payment_status({}) == ""


def test_set_payment_normalizes_sdk_enums_and_dates(server_module):
    server = server_module
    doc = _sdk_shaped(_issued_doc([_rate(1220.0, "2026-02-09", "paid",
                                         paid_date="2026-02-05",
                                         payment_account={"id": 110, "name": "Banca Intesa",
                                                          "type": "standard"})]))

    updated = _sdk_shaped(_issued_doc([_rate(1220.0, "2026-02-09")]))

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document",
                      return_value=_doc_response(updated)) as modify:
        result = _run(server.call_tool("set_payment", {
            "document_id": 42, "document_type": "issued", "status": "not_paid",
        }))

    sent = _sent_payments(modify, "modify_issued_document_request")
    assert sent[0]["status"] == "not_paid"
    assert sent[0]["due_date"] == "2026-02-09"
    assert "paid_date" not in sent[0]
    assert "payment_account" not in sent[0]
    assert json.loads(result[0].text)["payments"][0]["status"] == "not_paid"


def test_set_payment_received_amount_falls_back_to_net_plus_vat(server_module):
    """ReceivedDocument.to_dict() strips amount_gross (read-only), so the
    synthesized installment must be built from net + vat."""
    server = server_module
    doc = ReceivedDocument.from_dict(_received_doc([])).to_dict()
    assert "amount_gross" not in doc

    with patch.object(server.received_api, "get_received_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.received_api, "modify_received_document",
                      return_value=_doc_response(doc)) as modify:
        _run(server.call_tool("set_payment", {
            "document_id": 99, "document_type": "received", "status": "paid",
            "paid_date": "2026-02-05",
        }))

    sent = _sent_payments(modify, "modify_received_document_request")
    assert sent[0]["amount"] == 610.0
    assert sent[0]["due_date"] == "2026-01-10"


def test_set_payment_preserves_ei_raw(server_module):
    """E-invoice raw payment attributes must survive the round-trip — SDI-sent
    invoices are the main use case."""
    server = server_module
    doc = _issued_doc([_rate(1220.0, "2026-02-09", id=901,
                             ei_raw={"DataScadenzaPagamento": "2026-02-09"})],
                      ei_status="sent")

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document",
                      return_value=_doc_response(doc)) as modify:
        _run(server.call_tool("set_payment", {
            "document_id": 42, "document_type": "issued", "status": "paid",
        }))

    sent = _sent_payments(modify, "modify_issued_document_request")
    assert sent[0]["ei_raw"] == {"DataScadenzaPagamento": "2026-02-09"}
    assert sent[0]["id"] == 901


# --------------------------------------------------------------------------
# resolve_payment_account
# --------------------------------------------------------------------------

def test_resolve_payment_account_by_unique_substring(server_module):
    server = server_module
    account, error = server.resolve_payment_account("intesa")
    assert error is None
    assert account["id"] == 110


def test_resolve_payment_account_ambiguous_substring(server_module):
    server = server_module
    import cache as cache_mod
    cache_mod.invalidate_all(100)
    accounts = [
        {"id": 1, "name": "Banca Intesa", "type": "standard", "virtual": False},
        {"id": 2, "name": "Banca Popolare", "type": "standard", "virtual": False},
    ]
    with patch.object(server.info_api, "list_payment_accounts",
                      return_value=_accounts_response(accounts)):
        account, error = server.resolve_payment_account("banca")
    assert account is None
    assert "ambiguo" in error


def test_resolve_payment_account_unknown_id(server_module):
    server = server_module
    account, error = server.resolve_payment_account(999)
    assert account is None
    assert "999" in error


def test_payment_accounts_api_failure_is_not_cached(server_module):
    """A transient failure must not poison the 24h cache with an empty list,
    or every payment_account stays unresolvable for a day."""
    server = server_module
    import cache as cache_mod
    cache_mod.invalidate_all(100)

    with patch.object(server.info_api, "list_payment_accounts",
                      side_effect=RuntimeError("503")):
        assert server.fetch_payment_accounts(company_id=100) == []

    with patch.object(server.info_api, "list_payment_accounts",
                      return_value=_accounts_response(ACCOUNTS)):
        assert server.fetch_payment_accounts(company_id=100) == ACCOUNTS


# --------------------------------------------------------------------------
# review round 2
# --------------------------------------------------------------------------

def test_update_document_keeps_immediate_payment_terms(server_module):
    """`payment_terms.days = 0` (rimessa diretta) is a real term: it must not
    be read back as 30 and trigger a phantom reschedule."""
    server = server_module
    doc = _issued_doc([
        _rate(610.0, "2026-01-10", "paid", paid_date="2026-01-10",
              payment_terms={"days": 0, "type": "standard"}),
        _rate(610.0, "2026-01-10", payment_terms={"days": 0, "type": "standard"}),
    ])

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document",
                      return_value=_doc_response(doc)) as modify, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme"}):
        result = _run(server.call_tool("update_document", {
            "document_id": 42, "visible_subject": "Nuovo",
        }))

    assert json.loads(result[0].text)["success"] is True
    sent = _sent_payments(modify, "modify_issued_document_request")
    assert len(sent) == 2
    assert sent[0]["due_date"] == "2026-01-10"
    assert sent[0]["status"] == "paid"


def test_update_document_on_credit_note_with_negative_payment(server_module):
    """Credit note installments can carry negative amounts; comparing them
    against the positive recomputed total must not read as a changed total."""
    server = server_module
    doc = _issued_doc([_rate(-1220.0, "2026-02-09", "paid", paid_date="2026-02-05")])
    doc["type"] = "credit_note"

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document",
                      return_value=_doc_response(doc)) as modify, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme"}):
        result = _run(server.call_tool("update_document", {
            "document_id": 42, "visible_subject": "Nuovo",
        }))

    assert modify.called
    assert json.loads(result[0].text)["success"] is True
    sent = _sent_payments(modify, "modify_issued_document_request")
    assert sent[0]["amount"] == -1220.0
    assert sent[0]["status"] == "paid"


def test_update_document_refuses_to_collapse_an_installment_plan(server_module):
    """A 30/60/90 plan must not disappear silently on an unrelated edit."""
    server = server_module
    doc = _issued_doc([
        _rate(406.67, "2026-01-31"),
        _rate(406.67, "2026-02-28"),
        _rate(406.66, "2026-03-31"),
    ])

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document") as modify, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme"}):
        result = _run(server.call_tool("update_document", {
            "document_id": 42, "date": "2026-02-01",
        }))

    assert not modify.called
    payload = json.loads(result[0].text)
    assert payload["success"] is False
    assert len(payload["payments"]) == 3


def test_set_payment_rejects_malformed_paid_date(server_module):
    server = server_module
    doc = _issued_doc([_rate(1220.0, "2026-02-09")])

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document") as modify:
        result = _run(server.call_tool("set_payment", {
            "document_id": 42, "document_type": "issued", "status": "paid",
            "paid_date": "05/02/2026",
        }))

    assert not modify.called
    payload = json.loads(result[0].text)
    assert payload["success"] is False
    assert "paid_date" in payload["error"]


def test_set_payment_repeat_keeps_the_original_paid_date(server_module):
    """The tool is annotated idempotent: replaying it without paid_date must
    not move an already registered payment to today."""
    server = server_module
    doc = _issued_doc([_rate(1220.0, "2026-02-09", "paid", paid_date="2026-02-05")])

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document",
                      return_value=_doc_response(doc)) as modify:
        _run(server.call_tool("set_payment", {
            "document_id": 42, "document_type": "issued", "status": "paid",
        }))

    sent = _sent_payments(modify, "modify_issued_document_request")
    assert sent[0]["paid_date"] == "2026-02-05"


def test_set_payment_issued_without_installments_is_rejected(server_module):
    server = server_module
    doc = _issued_doc([])

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document") as modify:
        result = _run(server.call_tool("set_payment", {
            "document_id": 42, "document_type": "issued", "status": "paid",
        }))

    assert not modify.called
    assert json.loads(result[0].text)["success"] is False


def test_set_payment_rejects_non_numeric_payment_index(server_module):
    server = server_module
    doc = _issued_doc([_rate(1220.0, "2026-02-09")])

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document") as modify:
        result = _run(server.call_tool("set_payment", {
            "document_id": 42, "document_type": "issued", "status": "paid",
            "payment_index": "prima",
        }))

    assert not modify.called
    assert "payment_index" in json.loads(result[0].text)["error"]


def test_get_situation_counts_reversed_as_outstanding(server_module):
    """A reversed payment was not collected: it belongs to da_incassare, not
    to nowhere."""
    server = server_module
    doc = _issued_doc([_rate(1220.0, "2026-02-09", "reversed")])
    listed = MagicMock()
    listed.data = [MagicMock(**{"to_dict.return_value": doc})]
    empty = MagicMock()
    empty.data = []

    with patch.object(server.issued_api, "list_issued_documents",
                      side_effect=[listed, empty]), \
         patch.object(server.received_api, "list_received_documents", return_value=empty):
        result = _run(server.call_tool("get_situation", {"year": 2026}))

    payload = json.loads(result[0].text)
    assert payload["incassato"] == 0.0
    assert payload["da_incassare"] == 1220.0
    assert [s["amount"] for s in payload["prossime_scadenze"]] == [1220.0]


# --------------------------------------------------------------------------
# review round 3
# --------------------------------------------------------------------------

def test_update_document_on_sdk_shaped_credit_note(server_module):
    """Credit note fixtures were always plain strings, so nothing pinned the
    behaviour on the enum-typed `type` the API really returns."""
    server = server_module
    payload = _issued_doc([_rate(1220.0, "2026-02-09")])
    payload["type"] = "credit_note"
    payload["e_invoice"] = True
    payload["ei_data"] = {"payment_method": "MP05"}
    doc = _sdk_shaped(payload)

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document",
                      return_value=_doc_response(doc)) as modify, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme"}):
        result = _run(server.call_tool("update_document", {
            "document_id": 42, "visible_subject": "Nuovo",
        }))

    data = _sent_data(modify, "modify_issued_document_request")
    assert data["type"] == "credit_note"
    assert data["e_invoice"] is True
    assert data["ei_data"] == {"payment_method": "MP05"}
    payload = json.loads(result[0].text)
    assert payload["total"] == -1220.0
    assert payload["type"] == "credit_note"


def test_update_document_survives_null_payment_terms(server_module):
    """FIC can return payment_terms: null, and days: null inside it."""
    server = server_module
    doc = _issued_doc([_rate(1220.0, "2026-02-09")])
    doc["payments_list"][0]["payment_terms"] = None

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document",
                      return_value=_doc_response(doc)) as modify, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme"}):
        result = _run(server.call_tool("update_document", {
            "document_id": 42, "visible_subject": "Nuovo",
        }))

    assert modify.called
    assert json.loads(result[0].text)["success"] is True


def test_update_document_survives_null_payment_days(server_module):
    server = server_module
    doc = _issued_doc([_rate(1220.0, "2026-02-09",
                             payment_terms={"days": None, "type": "standard"})])

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document",
                      return_value=_doc_response(doc)) as modify, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme"}):
        result = _run(server.call_tool("update_document", {
            "document_id": 42, "visible_subject": "Nuovo",
        }))

    assert modify.called
    assert json.loads(result[0].text)["success"] is True


def test_set_payment_validates_paid_date_before_fetching(server_module):
    """A malformed date must not cost an API round-trip."""
    server = server_module

    with patch.object(server.issued_api, "get_issued_document") as get_doc, \
         patch.object(server.issued_api, "modify_issued_document") as modify:
        result = _run(server.call_tool("set_payment", {
            "document_id": 42, "document_type": "issued", "status": "paid",
            "paid_date": "oggi",
        }))

    assert not get_doc.called
    assert not modify.called
    assert json.loads(result[0].text)["success"] is False


def test_update_document_refusal_names_both_totals(server_module):
    """The refusal message must not claim a changed total without showing the
    two numbers it compared."""
    server = server_module
    doc = _issued_doc([_rate(1220.0, "2026-02-09", "paid", paid_date="2026-02-05")])

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document"), \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme"}):
        result = _run(server.call_tool("update_document", {
            "document_id": 42,
            "items": [{"name": "Item", "qty": 1, "net_price": 2000.0, "vat_rate": 22}],
        }))

    error = json.loads(result[0].text)["error"]
    assert "2440.0" in error and "1220.0" in error


# --------------------------------------------------------------------------
# review round 4
# --------------------------------------------------------------------------

def test_update_document_accepts_null_payment_days(server_module):
    """A client sending payment_days: null must not reach timedelta()."""
    server = server_module
    doc = _issued_doc([_rate(1220.0, "2026-02-09")])

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document",
                      return_value=_doc_response(doc)) as modify, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme"}):
        result = _run(server.call_tool("update_document", {
            "document_id": 42, "visible_subject": "Nuovo", "payment_days": None,
        }))

    assert modify.called
    assert json.loads(result[0].text)["success"] is True


def test_update_document_refusal_points_at_the_panel_for_installment_plans(server_module):
    """Clearing the payment does not unblock a multi-installment document, so
    the message must not suggest it."""
    server = server_module
    doc = _issued_doc([
        _rate(610.0, "2026-01-31", "paid", paid_date="2026-01-30"),
        _rate(610.0, "2026-02-28"),
    ])

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document"), \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme"}):
        result = _run(server.call_tool("update_document", {
            "document_id": 42, "payment_days": 60,
        }))

    error = json.loads(result[0].text)["error"]
    assert "pannello" in error
    assert "set_payment" not in error
    # the total did not change: claiming it did would be the wrong justification
    assert "totale ricalcolato" not in error


def test_update_document_refusal_reports_magnitudes_on_a_credit_note(server_module):
    server = server_module
    doc = _issued_doc([_rate(-1220.0, "2026-02-09", "paid", paid_date="2026-02-05")])
    doc["type"] = "credit_note"

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document"), \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme"}):
        result = _run(server.call_tool("update_document", {
            "document_id": 42,
            "items": [{"name": "Item", "qty": 1, "net_price": 2000.0, "vat_rate": 22}],
        }))

    error = json.loads(result[0].text)["error"]
    assert "set_payment" in error
    assert "2440.0" in error and "1220.0" in error
    assert "-1220.0" not in error


def test_set_payment_not_paid_ignores_a_stale_paid_date(server_module):
    """paid_date is documented as ignored for not_paid: it must not block the
    call that clears the payment."""
    server = server_module
    doc = _issued_doc([_rate(1220.0, "2026-02-09", "paid", paid_date="2026-02-05")])

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document",
                      return_value=_doc_response(doc)) as modify:
        result = _run(server.call_tool("set_payment", {
            "document_id": 42, "document_type": "issued", "status": "not_paid",
            "paid_date": "05/02/2026",
        }))

    assert modify.called
    assert json.loads(result[0].text)["success"] is True


def test_set_payment_rejects_a_date_that_only_looks_valid_once_truncated(server_module):
    server = server_module

    with patch.object(server.issued_api, "get_issued_document") as get_doc:
        result = _run(server.call_tool("set_payment", {
            "document_id": 42, "document_type": "issued", "status": "paid",
            "paid_date": "2026-02-05junk",
        }))

    assert not get_doc.called
    assert json.loads(result[0].text)["success"] is False


def test_set_payment_rejects_non_string_paid_date(server_module):
    server = server_module

    with patch.object(server.issued_api, "get_issued_document") as get_doc:
        result = _run(server.call_tool("set_payment", {
            "document_id": 42, "document_type": "issued", "status": "paid",
            "paid_date": 20260205,
        }))

    assert not get_doc.called
    payload = json.loads(result[0].text)
    assert payload["success"] is False
    assert "paid_date" in payload["error"]


@pytest.mark.parametrize("index", [1.9, True])
def test_set_payment_rejects_non_integer_payment_index(server_module, index):
    """A money mutation must not round or coerce its way to a different
    installment."""
    server = server_module
    doc = _issued_doc([
        _rate(610.0, "2026-01-31"),
        _rate(610.0, "2026-02-28"),
    ])

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document") as modify:
        result = _run(server.call_tool("set_payment", {
            "document_id": 42, "document_type": "issued", "status": "paid",
            "payment_index": index,
        }))

    assert not modify.called
    assert "payment_index" in json.loads(result[0].text)["error"]


# --------------------------------------------------------------------------
# FIC PUT semantics (openapi-enriched.yaml + fattureincloud/api discussions)
# --------------------------------------------------------------------------

def test_set_payment_received_sends_entity(server_module):
    """ModifyReceivedDocumentRequest marks data.entity required, unlike the
    issued one (openapi-enriched.yaml line 9453)."""
    server = server_module
    doc = _received_doc([_rate(610.0, "2026-02-09")])

    with patch.object(server.received_api, "get_received_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.received_api, "modify_received_document",
                      return_value=_doc_response(doc)) as modify:
        _run(server.call_tool("set_payment", {
            "document_id": 99, "document_type": "received", "status": "paid",
        }))

    data = _sent_data(modify, "modify_received_document_request")
    assert data["entity"] == {"name": "Supplier Srl", "vat_number": "98765432109"}


def test_set_payment_warns_when_the_document_comes_back_emptied(server_module):
    """Residual risk on the partial-PUT assumption: if the response shows the
    line items gone, say so instead of reporting a clean success."""
    server = server_module
    doc = _issued_doc([_rate(1220.0, "2026-02-09")])
    emptied = _issued_doc([_rate(1220.0, "2026-02-09", "paid", paid_date="2026-02-05")])
    emptied["items_list"] = []

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document",
                      return_value=_doc_response(emptied)):
        result = _run(server.call_tool("set_payment", {
            "document_id": 42, "document_type": "issued", "status": "paid",
        }))

    payload = json.loads(result[0].text)
    assert "warning" in payload
    assert "items_list" in payload["warning"]


def test_set_payment_does_not_warn_on_a_normal_response(server_module):
    server = server_module
    doc = _issued_doc([_rate(1220.0, "2026-02-09")])

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document",
                      return_value=_doc_response(doc)):
        result = _run(server.call_tool("set_payment", {
            "document_id": 42, "document_type": "issued", "status": "paid",
        }))

    assert "warning" not in json.loads(result[0].text)


# --------------------------------------------------------------------------
# conformance with the official OpenAPI spec
# --------------------------------------------------------------------------

def test_update_document_preserves_end_of_month_terms(server_module):
    """payment_terms.type is an enum with end_of_month in it: rebuilding an
    installment must not silently convert a fine-mese schedule."""
    server = server_module
    doc = _issued_doc([_rate(1220.0, "2026-02-28",
                             payment_terms={"days": 0, "type": "end_of_month"})])

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document",
                      return_value=_doc_response(doc)) as modify, \
         patch.object(server.info_api, "list_vat_types", return_value=_vat_types_response()), \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme"}):
        _run(server.call_tool("update_document", {
            "document_id": 42,
            "items": [{"name": "Item", "qty": 1, "net_price": 2000.0, "vat_rate": 22}],
        }))

    sent = _sent_payments(modify, "modify_issued_document_request")
    assert sent[0]["payment_terms"]["type"] == "end_of_month"


def test_update_document_preserves_stored_item_fields(server_module):
    """vat.value is read-only — vat.id selects the rate — and an array in the
    body replaces the stored one, so an echoed item must keep its own fields."""
    server = server_module
    doc = _issued_doc([_rate(1220.0, "2026-02-09")])
    doc["items_list"] = [{
        "id": 77, "product_id": 5, "code": "TV3", "name": "Item",
        "description": "", "qty": 1, "net_price": 1000.0, "discount": 10.0,
        "apply_withholding_taxes": True,
        "vat": {"id": 3, "value": 10.0},
    }]

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document",
                      return_value=_doc_response(doc)) as modify, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme"}):
        _run(server.call_tool("update_document", {
            "document_id": 42, "visible_subject": "Nuovo",
        }))

    item = _sent_data(modify, "modify_issued_document_request")["items_list"][0]
    assert item["vat"]["id"] == 3
    assert item["product_id"] == 5
    assert item["discount"] == 10.0
    assert item["apply_withholding_taxes"] is True
    assert item["id"] == 77


def test_create_invoice_resolves_vat_rate_to_a_vat_type_id(server_module):
    """vat.value is read-only, so the rate only takes effect through vat.id."""
    server = server_module
    created = MagicMock()
    created.data.to_dict.return_value = {"id": 1, "number": 1, "date": "2026-01-10"}

    with patch.object(server.info_api, "list_vat_types", return_value=_vat_types_response()), \
         patch.object(server.issued_api, "create_issued_document", return_value=created) as create, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme", "ei_code": "ABC1234"}):
        _run(server.call_tool("create_invoice", {
            "client_id": 5, "date": "2026-01-10", "visible_subject": "Test",
            "items": [{"name": "Item", "qty": 1, "net_price": 100.0, "vat_rate": 10}],
        }))

    item = create.call_args.kwargs["create_issued_document_request"]["data"]["items_list"][0]
    assert item["vat"] == {"id": 3}


def test_create_invoice_rejects_a_vat_rate_with_no_vat_type(server_module):
    server = server_module

    with patch.object(server.info_api, "list_vat_types", return_value=_vat_types_response()), \
         patch.object(server.issued_api, "create_issued_document") as create, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme", "ei_code": "ABC1234"}):
        result = _run(server.call_tool("create_invoice", {
            "client_id": 5, "date": "2026-01-10", "visible_subject": "Test",
            "items": [{"name": "Item", "qty": 1, "net_price": 100.0, "vat_rate": 7}],
        }))

    assert not create.called
    payload = json.loads(result[0].text)
    assert payload["success"] is False
    assert "vat_rate" in payload["error"]


def test_update_document_preserves_e_invoice_and_payment_method(server_module):
    server = server_module
    doc = _issued_doc([_rate(1220.0, "2026-02-09")])
    doc["e_invoice"] = False
    doc["ei_data"] = {"payment_method": "MP08"}

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document",
                      return_value=_doc_response(doc)) as modify, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme"}):
        _run(server.call_tool("update_document", {
            "document_id": 42, "visible_subject": "Nuovo",
        }))

    data = _sent_data(modify, "modify_issued_document_request")
    assert data["e_invoice"] is False
    assert "ei_data" not in data


def test_update_document_keeps_the_stored_payment_method(server_module):
    server = server_module
    doc = _issued_doc([_rate(1220.0, "2026-02-09")])
    doc["e_invoice"] = True
    doc["ei_data"] = {"payment_method": "MP08"}

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document",
                      return_value=_doc_response(doc)) as modify, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme"}):
        _run(server.call_tool("update_document", {
            "document_id": 42, "visible_subject": "Nuovo",
        }))

    assert _sent_data(modify, "modify_issued_document_request")["ei_data"]["payment_method"] == "MP08"


def test_get_situation_reads_every_page(server_module):
    """per_page maxes out at 100 (fic-openapi.yaml:5733): a year with more
    documents than that must not silently report a partial total."""
    server = server_module

    def _page(docs, last_page):
        response = MagicMock()
        response.data = [MagicMock(**{"to_dict.return_value": d}) for d in docs]
        response.last_page = last_page
        return response

    first = _page([_issued_doc([_rate(1000.0, "2026-02-09", "paid")])], 2)
    second = _page([_issued_doc([_rate(500.0, "2026-03-09", "paid")])], 2)
    empty = _page([], 1)

    with patch.object(server.issued_api, "list_issued_documents",
                      side_effect=[first, second, empty]) as listed, \
         patch.object(server.received_api, "list_received_documents", return_value=empty):
        result = _run(server.call_tool("get_situation", {"year": 2026}))

    assert listed.call_args_list[1].kwargs["page"] == 2
    assert json.loads(result[0].text)["incassato"] == 1500.0


def test_create_received_document_maps_the_credit_note_alias(server_module):
    """ReceivedDocumentType has no credit_note: the passive one is
    passive_credit_note (fic-enriched.yaml:8080-8089)."""
    server = server_module
    created = MagicMock()
    created.data.to_dict.return_value = {"id": 9, "type": "passive_credit_note", "date": "2026-01-10"}

    with patch.object(server.received_api, "create_received_document",
                      return_value=created) as create:
        _run(server.call_tool("create_received_document", {
            "supplier_name": "Supplier Srl", "amount_net": 500.0, "type": "credit_note",
        }))

    assert create.call_args.kwargs["create_received_document_request"]["data"]["type"] == "passive_credit_note"


def test_create_credit_note_does_not_claim_a_link_it_cannot_make(server_module):
    """`original_document` is not a writable field: the SDK drops it and the
    spec links documents through /issued_documents/transform instead."""
    server = server_module
    created = MagicMock()
    created.data.to_dict.return_value = {"id": 2, "number": 3, "date": "2026-01-10"}

    with patch.object(server.issued_api, "create_issued_document", return_value=created) as create, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme", "ei_code": "ABC1234"}):
        result = _run(server.call_tool("create_credit_note", {
            "client_id": 5, "date": "2026-01-10", "visible_subject": "Storno",
            "items": [{"name": "Item", "qty": 1, "net_price": 100.0, "vat_rate": 22}],
            "source_invoice_id": 41,
        }))

    body = create.call_args.kwargs["create_issued_document_request"]["data"]
    assert "original_document" not in body
    payload = json.loads(result[0].text)
    assert payload["success"] is True
    assert "41" in payload["message"]
    assert "collegamento" in payload["message"].lower()


# --------------------------------------------------------------------------
# review round 5
# --------------------------------------------------------------------------

@pytest.mark.parametrize("index", ["²", "--1", "٥"])
def test_set_payment_rejects_digit_lookalikes(server_module, index):
    """isdigit() is not the predicate of int(): superscripts and double signs
    raise, and non-ASCII digits would silently select an installment."""
    server = server_module
    doc = _issued_doc([_rate(610.0, "2026-01-31"), _rate(610.0, "2026-02-28")])

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document") as modify:
        result = _run(server.call_tool("set_payment", {
            "document_id": 42, "document_type": "issued", "status": "paid",
            "payment_index": index,
        }))

    assert not modify.called
    assert "payment_index" in json.loads(result[0].text)["error"]


def test_set_payment_does_not_warn_when_the_response_omits_items(server_module):
    """A response that says nothing about items_list is not evidence of a wipe."""
    server = server_module
    doc = _issued_doc([_rate(1220.0, "2026-02-09")])
    without_items = {k: v for k, v in _issued_doc(
        [_rate(1220.0, "2026-02-09", "paid", paid_date="2026-02-05")]).items()
        if k != "items_list"}

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document",
                      return_value=_doc_response(without_items)):
        result = _run(server.call_tool("set_payment", {
            "document_id": 42, "document_type": "issued", "status": "paid",
        }))

    assert "warning" not in json.loads(result[0].text)


def test_set_payment_received_omits_an_empty_entity(server_module):
    """entity is required on the received PUT: sending {} would write an empty
    supplier instead of leaving the stored one alone."""
    server = server_module
    doc = _received_doc([_rate(610.0, "2026-02-09")])
    doc["entity"] = {}

    with patch.object(server.received_api, "get_received_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.received_api, "modify_received_document",
                      return_value=_doc_response(doc)) as modify:
        _run(server.call_tool("set_payment", {
            "document_id": 99, "document_type": "received", "status": "paid",
        }))

    assert "entity" not in _sent_data(modify, "modify_received_document_request")


def test_update_document_applies_line_discounts_to_the_installment(server_module):
    """`discount` is a percentage on the line (enriched:2811): now that echoed
    items keep it, the recomputed total has to apply it."""
    server = server_module
    # 1000 net, 10% line discount, 10% VAT -> 900 * 1.10 = 990
    doc = _issued_doc([_rate(990.0, "2026-02-09")])
    doc["items_list"] = [{
        "name": "Item", "qty": 1, "net_price": 1000.0, "discount": 10.0,
        "vat": {"id": 3, "value": 10.0},
    }]

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document",
                      return_value=_doc_response(doc)) as modify, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme"}):
        result = _run(server.call_tool("update_document", {
            "document_id": 42, "date": "2026-01-15",
        }))

    payload = json.loads(result[0].text)
    assert payload["success"] is True, payload
    sent = _sent_payments(modify, "modify_issued_document_request")
    assert sent[0]["amount"] == 990.0


def test_list_invoices_reports_truncation(server_module):
    """per_page caps at 100: a longer year must not look complete."""
    server = server_module
    doc = MagicMock()
    doc.to_dict.return_value = _issued_doc([_rate(1220.0, "2026-02-09")])
    listed = MagicMock()
    listed.data = [doc]
    listed.last_page = 3

    with patch.object(server.issued_api, "list_issued_documents", return_value=listed):
        result = _run(server.call_tool("list_invoices", {"year": 2026}))

    payload = json.loads(result[0].text)
    assert payload["truncated"] is True
    assert payload["pages"] == 3
    assert len(payload["documents"]) == 1


def test_list_received_documents_normalizes_the_type_alias(server_module):
    server = server_module
    listed = MagicMock()
    listed.data = []
    listed.last_page = 1

    with patch.object(server.received_api, "list_received_documents",
                      return_value=listed) as call:
        _run(server.call_tool("list_received_documents", {"year": 2026, "type": "credit_note"}))

    assert call.call_args.kwargs["type"] == "passive_credit_note"


def test_resolve_vat_type_refuses_an_ambiguous_rate(server_module):
    """Several 0% types differ by natura (N1…N7) — picking the lowest id would
    silently choose the e-invoice nature."""
    server = server_module
    import cache as cache_mod
    cache_mod.invalidate_all(100)
    zero_rates = MagicMock()
    zero_rates.data = [_sdk_obj(v) for v in [
        {"id": 6, "value": 0.0, "description": "Non imponibile art. 8", "is_disabled": False, "default": False},
        {"id": 7, "value": 0.0, "description": "Esente art. 10", "is_disabled": False, "default": False},
    ]]

    with patch.object(server.info_api, "list_vat_types", return_value=zero_rates):
        vat_type, error = server.resolve_vat_type(0)

    assert vat_type is None
    assert "Esente art. 10" in error and "Non imponibile art. 8" in error


def test_create_invoice_accepts_an_explicit_vat_id(server_module):
    """The escape hatch from an ambiguous rate."""
    server = server_module
    created = MagicMock()
    created.data.to_dict.return_value = {"id": 1, "number": 1, "date": "2026-01-10"}

    with patch.object(server.issued_api, "create_issued_document", return_value=created) as create, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme", "ei_code": "ABC1"}):
        _run(server.call_tool("create_invoice", {
            "client_id": 5, "date": "2026-01-10", "visible_subject": "Test",
            "items": [{"name": "Item", "qty": 1, "net_price": 100.0, "vat_id": 6}],
        }))

    item = create.call_args.kwargs["create_issued_document_request"]["data"]["items_list"][0]
    assert item["vat"] == {"id": 6}


# --------------------------------------------------------------------------
# review round 6
# --------------------------------------------------------------------------

def test_create_invoice_with_vat_id_keeps_the_gross_in_the_installment(server_module):
    """vat_id is the way out of an ambiguous rate, so the caller has no reason
    to also pass vat_rate: the percentage has to come from the registry."""
    server = server_module
    created = MagicMock()
    created.data.to_dict.return_value = {"id": 1, "number": 1, "date": "2026-01-10"}

    with patch.object(server.issued_api, "create_issued_document", return_value=created) as create, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme", "ei_code": "ABC1"}):
        result = _run(server.call_tool("create_invoice", {
            "client_id": 5, "date": "2026-01-10", "visible_subject": "Test",
            "items": [{"name": "Item", "qty": 1, "net_price": 100.0, "vat_id": 0}],
        }))

    body = create.call_args.kwargs["create_issued_document_request"]["data"]
    assert body["items_list"][0]["vat"] == {"id": 0}
    assert body["payments_list"][0]["amount"] == 122.0
    assert json.loads(result[0].text)["total"] == 122.0


def test_create_invoice_rejects_an_unknown_vat_id(server_module):
    server = server_module

    with patch.object(server.issued_api, "create_issued_document") as create, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme", "ei_code": "ABC1"}):
        result = _run(server.call_tool("create_invoice", {
            "client_id": 5, "date": "2026-01-10", "visible_subject": "Test",
            "items": [{"name": "Item", "qty": 1, "net_price": 100.0, "vat_id": 99}],
        }))

    assert not create.called
    payload = json.loads(result[0].text)
    assert payload["success"] is False
    assert "vat_id" in payload["error"]


def test_list_invoices_accepts_a_page(server_module):
    """truncated: true is only actionable if the next page can be asked for."""
    server = server_module
    listed = MagicMock()
    listed.data = []
    listed.last_page = "3"  # the API may hand the page count back as a string

    with patch.object(server.issued_api, "list_issued_documents", return_value=listed) as call:
        result = _run(server.call_tool("list_invoices", {"year": 2026, "page": 2}))

    assert call.call_args.kwargs["page"] == 2
    payload = json.loads(result[0].text)
    assert payload["page"] == 2
    assert payload["pages"] == 3
    assert payload["truncated"] is True


def test_list_received_documents_accepts_a_page(server_module):
    server = server_module
    listed = MagicMock()
    listed.data = []
    listed.last_page = 2

    with patch.object(server.received_api, "list_received_documents", return_value=listed) as call:
        result = _run(server.call_tool("list_received_documents", {"year": 2026, "page": 2}))

    payload = json.loads(result[0].text)
    assert call.call_args.kwargs["page"] == 2
    assert payload["page"] == 2
    # last page: asking for another one would return an empty list
    assert payload["truncated"] is False


# --------------------------------------------------------------------------
# review round 7
# --------------------------------------------------------------------------

def test_create_invoice_rejects_a_disabled_vat_id(server_module):
    """resolve_vat_type skips disabled rates, so vat_id must not let one in."""
    server = server_module

    with patch.object(server.issued_api, "create_issued_document") as create, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme", "ei_code": "A1"}):
        result = _run(server.call_tool("create_invoice", {
            "client_id": 5, "date": "2026-01-10", "visible_subject": "Test",
            "items": [{"name": "Item", "qty": 1, "net_price": 100.0, "vat_id": 9}],
        }))

    assert not create.called
    payload = json.loads(result[0].text)
    assert payload["success"] is False
    assert "disattivata" in payload["error"]


@pytest.mark.parametrize("vat_id", [True, 1.0])
def test_create_invoice_rejects_a_non_integer_vat_id(server_module, vat_id):
    """`t["id"] == True` is true for id 1: a boolean would bill at 4%."""
    server = server_module

    with patch.object(server.issued_api, "create_issued_document") as create, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme", "ei_code": "A1"}):
        result = _run(server.call_tool("create_invoice", {
            "client_id": 5, "date": "2026-01-10", "visible_subject": "Test",
            "items": [{"name": "Item", "qty": 1, "net_price": 100.0, "vat_id": vat_id}],
        }))

    assert not create.called
    assert "vat_id" in json.loads(result[0].text)["error"]


# --------------------------------------------------------------------------
# review round 8
# --------------------------------------------------------------------------

def _only_disabled_22():
    response = MagicMock()
    response.data = [_sdk_obj(v) for v in [
        {"id": 9, "value": 22.0, "description": "22% dismessa", "is_disabled": True, "default": False},
        {"id": 3, "value": 10.0, "description": "10%", "is_disabled": False, "default": False},
    ]]
    return response


def test_resolve_vat_type_does_not_offer_disabled_rates(server_module):
    """A rate that exists only as a disabled type is not available: listing it
    sends the caller back to the value just refused."""
    server = server_module
    import cache as cache_mod
    cache_mod.invalidate_all(100)

    with patch.object(server.info_api, "list_vat_types", return_value=_only_disabled_22()):
        vat_type, error = server.resolve_vat_type(22)

    assert vat_type is None
    assert "22% dismessa" not in error
    assert "10%" in error


def test_resolve_vat_id_lists_only_active_options(server_module):
    server = server_module
    import cache as cache_mod
    cache_mod.invalidate_all(100)

    with patch.object(server.info_api, "list_vat_types", return_value=_only_disabled_22()):
        vat_type, error = server.resolve_vat_id(99)

    assert vat_type is None
    assert "22% dismessa" not in error


def test_resolve_vat_id_accepts_a_numeric_string(server_module):
    """payment_account and payment_index both take one: this is the same
    shape of argument."""
    server = server_module
    vat_type, error = server.resolve_vat_id("3")
    assert error is None
    assert vat_type["id"] == 3


def test_resolve_vat_id_error_does_not_name_a_missing_tool(server_module):
    server = server_module
    _, error = server.resolve_vat_id(1.5)
    assert "list_vat_types" not in error


# --------------------------------------------------------------------------
# review round 9
# --------------------------------------------------------------------------

def test_create_invoice_treats_a_null_vat_rate_as_absent(server_module):
    """Clients echo back the fields they just read, nulls included."""
    server = server_module
    created = MagicMock()
    created.data.to_dict.return_value = {"id": 1, "number": 1, "date": "2026-01-10"}

    with patch.object(server.issued_api, "create_issued_document", return_value=created) as create, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme", "ei_code": "A1"}):
        result = _run(server.call_tool("create_invoice", {
            "client_id": 5, "date": "2026-01-10", "visible_subject": "Test",
            "items": [{"name": "Item", "qty": 1, "net_price": 100.0, "vat_rate": None}],
        }))

    assert json.loads(result[0].text)["success"] is True
    item = create.call_args.kwargs["create_issued_document_request"]["data"]["items_list"][0]
    assert item["vat"] == {"id": 0}


@pytest.mark.parametrize("rate", ["22", True])
def test_resolve_vat_type_rejects_a_non_numeric_rate(server_module, rate):
    """round() on a string or a bool would reach the model as a stacktrace —
    and round(True, 2) is 1, i.e. a 1% line."""
    server = server_module
    vat_type, error = server.resolve_vat_type(rate)
    assert vat_type is None
    assert "vat_rate" in error


def test_resolve_vat_id_type_error_lists_the_options(server_module):
    """The other two refusals list them inline; this one pointed at a message
    a first-try mistake has never seen."""
    server = server_module
    _, error = server.resolve_vat_id(1.5)
    assert "22%" in error


# --------------------------------------------------------------------------
# review round 10
# --------------------------------------------------------------------------

@pytest.mark.parametrize("item", [
    {"name": "X", "qty": 1, "net_price": None, "vat_rate": 22},
    {"name": "X", "qty": None, "net_price": 100.0, "vat_rate": 22},
    {"name": "X", "net_price": 100.0, "vat_rate": 22},
    {"name": "X", "qty": 1, "net_price": "100", "vat_rate": 22},
])
def test_create_invoice_rejects_lines_without_usable_amounts(server_module, item):
    """qty and net_price decide the amount: a null one produces a 0.00
    installment and a 0 total, with nothing to signal it."""
    server = server_module

    with patch.object(server.issued_api, "create_issued_document") as create, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme", "ei_code": "A1"}):
        result = _run(server.call_tool("create_invoice", {
            "client_id": 5, "date": "2026-01-10", "visible_subject": "Test",
            "items": [item],
        }))

    assert not create.called
    payload = json.loads(result[0].text)
    assert payload["success"] is False
    assert "net_price" in payload["error"] or "qty" in payload["error"]


def test_resolve_vat_id_reports_an_unreadable_registry(server_module):
    """A type error must not present an empty registry as the list of rates."""
    server = server_module
    import cache as cache_mod
    cache_mod.invalidate_all(100)

    with patch.object(server.info_api, "list_vat_types", side_effect=RuntimeError("503")):
        vat_type, error = server.resolve_vat_id(1.5)

    assert vat_type is None
    assert "anagrafica IVA" in error
    assert "Disponibili: []" not in error


# --------------------------------------------------------------------------
# review round 11
# --------------------------------------------------------------------------

def test_create_invoice_rejects_an_empty_items_list(server_module):
    """Per-line checks do not run when there are no lines: the document would
    be created with no rows and a 0.00 installment."""
    server = server_module

    with patch.object(server.issued_api, "create_issued_document") as create, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme", "ei_code": "A1"}):
        result = _run(server.call_tool("create_invoice", {
            "client_id": 5, "date": "2026-01-10", "visible_subject": "Test", "items": [],
        }))

    assert not create.called
    assert json.loads(result[0].text)["success"] is False


def test_update_document_rejects_an_empty_items_list(server_module):
    """An array in the body replaces the stored one, so items=[] would delete
    the document's lines."""
    server = server_module
    doc = _issued_doc([_rate(1220.0, "2026-02-09")])

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document") as modify, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme"}):
        result = _run(server.call_tool("update_document", {
            "document_id": 42, "items": [],
        }))

    assert not modify.called
    assert json.loads(result[0].text)["success"] is False


def test_create_invoice_rejects_a_line_without_name(server_module):
    """name is required too, and the refusal must identify the line without
    relying on the field that is missing."""
    server = server_module

    with patch.object(server.issued_api, "create_issued_document") as create, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme", "ei_code": "A1"}):
        result = _run(server.call_tool("create_invoice", {
            "client_id": 5, "date": "2026-01-10", "visible_subject": "Test",
            "items": [
                {"name": "Prima", "qty": 1, "net_price": 100.0},
                {"qty": 1, "net_price": 50.0},
            ],
        }))

    assert not create.called
    error = json.loads(result[0].text)["error"]
    assert "posizione 1" in error
    assert "name" in error


# --------------------------------------------------------------------------
# review round 12
# --------------------------------------------------------------------------

@pytest.mark.parametrize("items", [
    [None],
    ["Consulenza"],
    [{"name": 123, "qty": 1, "net_price": 100.0}],
    [{"name": "   ", "qty": 1, "net_price": 100.0}],
])
def test_create_invoice_rejects_malformed_lines(server_module, items):
    """item.get() on a non-dict raises, and a non-string name reaches the SDK
    model: both come back as stacktraces."""
    server = server_module

    with patch.object(server.issued_api, "create_issued_document") as create, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme", "ei_code": "A1"}):
        result = _run(server.call_tool("create_invoice", {
            "client_id": 5, "date": "2026-01-10", "visible_subject": "Test", "items": items,
        }))

    assert not create.called
    payload = json.loads(result[0].text)
    assert payload["success"] is False
    assert "posizione 0" in payload["error"]


# --------------------------------------------------------------------------
# review round 13
# --------------------------------------------------------------------------

def test_duplicate_invoice_preserves_end_of_month_terms(server_module):
    """The third rebuild path: it computed the terms type and then wrote the
    constant anyway."""
    server = server_module
    doc = _issued_doc([_rate(1220.0, "2026-02-28",
                             payment_terms={"days": 30, "type": "end_of_month"})])
    created = MagicMock()
    created.data.to_dict.return_value = {"id": 2, "number": 9, "date": "2026-03-01"}

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "create_issued_document",
                      return_value=created) as create, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme", "ei_code": "A1"}):
        _run(server.call_tool("duplicate_invoice", {
            "source_document_id": 42, "new_date": "2026-03-01",
        }))

    payment = create.call_args.kwargs["create_issued_document_request"]["data"]["payments_list"][0]
    assert payment["payment_terms"] == {"days": 30, "type": "end_of_month"}


@pytest.mark.parametrize("arguments", [
    {"supplier_name": "Supplier Srl", "amount_net": None},
    {"supplier_name": "Supplier Srl", "amount_net": True},
    {"supplier_name": "Supplier Srl", "amount_net": "100"},
    {"supplier_name": 123, "amount_net": 100.0},
])
def test_create_received_document_rejects_malformed_amounts(server_module, arguments):
    """amount_net and amount_vat are the passive-side qty/net_price: a bool
    registers a 1.00 EUR expense, a null raises inside the generic handler."""
    server = server_module

    with patch.object(server.received_api, "create_received_document") as create:
        result = _run(server.call_tool("create_received_document", arguments))

    assert not create.called
    assert json.loads(result[0].text)["success"] is False


def test_create_invoice_names_a_non_list_items_argument(server_module):
    """Iterating a string yields characters: the refusal pointed at a line
    that does not exist."""
    server = server_module

    with patch.object(server.issued_api, "create_issued_document") as create, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme", "ei_code": "A1"}):
        result = _run(server.call_tool("create_invoice", {
            "client_id": 5, "date": "2026-01-10", "visible_subject": "Test",
            "items": "Consulenza",
        }))

    assert not create.called
    error = json.loads(result[0].text)["error"]
    assert "items" in error
    assert "posizione 0" not in error


def test_create_received_document_treats_a_null_vat_as_zero(server_module):
    """Optional field: a null is an absent value, as elsewhere in this PR."""
    server = server_module
    created = MagicMock()
    created.data.to_dict.return_value = {"id": 9, "type": "expense", "date": "2026-01-10"}

    with patch.object(server.received_api, "create_received_document",
                      return_value=created) as create:
        result = _run(server.call_tool("create_received_document", {
            "supplier_name": "Supplier Srl", "amount_net": 100.0, "amount_vat": None,
        }))

    assert json.loads(result[0].text)["success"] is True
    assert create.call_args.kwargs["create_received_document_request"]["data"]["amount_gross"] == 100.0


# --------------------------------------------------------------------------
# review round 14
# --------------------------------------------------------------------------

def test_create_invoice_treats_null_date_and_payment_days_as_absent(server_module):
    server = server_module
    created = MagicMock()
    created.data.to_dict.return_value = {"id": 1, "number": 1, "date": "2026-01-10"}

    with patch.object(server.issued_api, "create_issued_document", return_value=created) as create, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme", "ei_code": "A1"}):
        result = _run(server.call_tool("create_invoice", {
            "client_id": 5, "date": None, "payment_days": None, "visible_subject": "Test",
            "items": [{"name": "Item", "qty": 1, "net_price": 100.0}],
        }))

    assert json.loads(result[0].text)["success"] is True
    assert create.called


@pytest.mark.parametrize("date_value", ["10/02/2026", "oggi", "2026-13-01"])
def test_create_invoice_rejects_a_malformed_date(server_module, date_value):
    """set_payment answers with a message for the same spelling; here it was a
    ValueError from the generic handler, after three reads."""
    server = server_module

    with patch.object(server.issued_api, "create_issued_document") as create, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme", "ei_code": "A1"}):
        result = _run(server.call_tool("create_invoice", {
            "client_id": 5, "date": date_value, "visible_subject": "Test",
            "items": [{"name": "Item", "qty": 1, "net_price": 100.0}],
        }))

    assert not create.called
    payload = json.loads(result[0].text)
    assert payload["success"] is False
    assert "date" in payload["error"]


def test_create_received_document_rejects_a_malformed_date(server_module):
    server = server_module

    with patch.object(server.received_api, "create_received_document") as create:
        result = _run(server.call_tool("create_received_document", {
            "supplier_name": "Supplier Srl", "amount_net": 100.0, "date": "10/02/2026",
        }))

    assert not create.called
    assert json.loads(result[0].text)["success"] is False


def test_duplicate_invoice_preserves_e_invoice_and_payment_method(server_module):
    """A copy that changes the payment method changes what the XML declares."""
    server = server_module
    doc = _issued_doc([_rate(1220.0, "2026-02-09")])
    doc["e_invoice"] = False
    doc["ei_data"] = {"payment_method": "MP19"}
    created = MagicMock()
    created.data.to_dict.return_value = {"id": 2, "number": 9, "date": "2026-03-01"}

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "create_issued_document",
                      return_value=created) as create, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme", "ei_code": "A1"}):
        _run(server.call_tool("duplicate_invoice", {
            "source_document_id": 42, "new_date": "2026-03-01",
        }))

    body = create.call_args.kwargs["create_issued_document_request"]["data"]
    assert body["e_invoice"] is False
    assert "ei_data" not in body


def test_create_invoice_normalizes_an_unpadded_date(server_module):
    """strptime accepts "2026-2-5": send FIC the padded form rather than
    whatever the client typed."""
    server = server_module
    created = MagicMock()
    created.data.to_dict.return_value = {"id": 1, "number": 1, "date": "2026-02-05"}

    with patch.object(server.issued_api, "create_issued_document", return_value=created) as create, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme", "ei_code": "A1"}):
        _run(server.call_tool("create_invoice", {
            "client_id": 5, "date": "2026-2-5", "visible_subject": "Test",
            "items": [{"name": "Item", "qty": 1, "net_price": 100.0}],
        }))

    assert create.call_args.kwargs["create_issued_document_request"]["data"]["date"] == "2026-02-05"
