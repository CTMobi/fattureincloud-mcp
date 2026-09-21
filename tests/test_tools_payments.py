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
        "payment_account": {"id": 110, "type": "standard"},
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
    assert sent[0]["payment_account"] == {"id": 110, "type": "standard"}


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
    assert sent[0]["payment_account"] == {"id": 111, "type": "standard"}
    assert json.loads(result[0].text)["success"] is True


def test_set_payment_received_without_installments_creates_one(server_module):
    """Documents created by create_received_document carry no payments_list."""
    server = server_module
    doc = _received_doc([])
    stored = _received_doc([_rate(610.0, "2026-01-10", status="paid",
                                  paid_date="2026-02-05")])

    with patch.object(server.received_api, "get_received_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.received_api, "modify_received_document",
                      return_value=_doc_response(stored)) as modify:
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
    stored = ReceivedDocument.from_dict(
        _received_doc([_rate(610.0, "2026-01-10", status="paid")])
    ).to_dict()

    with patch.object(server.received_api, "get_received_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.received_api, "modify_received_document",
                      return_value=_doc_response(stored)) as modify:
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
    """A transient failure must not poison the 24h cache, or every
    payment_account stays unresolvable for a day. Since round 42 the failure
    propagates instead of reading as an empty list; nothing is written either way."""
    server = server_module
    import cache as cache_mod
    cache_mod.invalidate_all(100)

    with patch.object(server.info_api, "list_payment_accounts",
                      side_effect=RuntimeError("503")):
        with pytest.raises(RuntimeError):
            server.fetch_payment_accounts(company_id=100)

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
    # days: 0 on 2026-01-10 — the label is not enough, the date has to follow it
    assert sent[0]["due_date"] == "2026-01-31"


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
            "source_document_id": 42, "new_date": "2026-03-05",
        }))

    payment = create.call_args.kwargs["create_issued_document_request"]["data"]["payments_list"][0]
    assert payment["payment_terms"] == {"days": 30, "type": "end_of_month"}
    # 2026-03-05 + 30 = 2026-04-04, then to the end of that month
    assert payment["due_date"] == "2026-04-30"


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
    body = create.call_args.kwargs["create_received_document_request"]["data"]
    assert body["amount_vat"] == 0
    # amount_gross is read-only: the SDK drops it from the request, so sending
    # it would only look like it had an effect
    assert "amount_gross" not in body


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


# --------------------------------------------------------------------------
# review round 15
# --------------------------------------------------------------------------

def _duplicate_source(**extra):
    doc = _issued_doc([_rate(1220.0, "2026-02-09")])
    doc.update(extra)
    return doc


@pytest.mark.parametrize("new_date", [None, "16/09/2026", "2026-02-05junk"])
def test_duplicate_invoice_validates_new_date(server_module, new_date):
    """duplicate_invoice is the one write path that does not go through
    build_issued_document, so the validation never reached it."""
    server = server_module
    doc = _duplicate_source()

    created = MagicMock()
    created.data.to_dict.return_value = {"id": 2, "number": 9, "date": "2026-03-01"}

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "create_issued_document",
                      return_value=created) as create, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme", "ei_code": "A1"}):
        result = _run(server.call_tool("duplicate_invoice", {
            "source_document_id": 42, "new_date": new_date,
        }))

    payload = json.loads(result[0].text)
    if new_date is None:
        assert payload["success"] is True  # null means "today", as elsewhere
    else:
        assert not create.called
        assert payload["success"] is False


def test_update_document_rejects_a_non_integer_payment_days(server_module):
    """Same argument name as create_invoice, which answers with a message."""
    server = server_module
    doc = _issued_doc([_rate(1220.0, "2026-02-09")])

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document") as modify, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme"}):
        result = _run(server.call_tool("update_document", {
            "document_id": 42, "payment_days": "30",
        }))

    assert not modify.called
    assert "payment_days" in json.loads(result[0].text)["error"]


def test_create_invoice_rejects_a_date_with_trailing_junk(server_module):
    """The [:10] truncation was removed from paid_date for this reason."""
    server = server_module

    with patch.object(server.issued_api, "create_issued_document") as create, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme", "ei_code": "A1"}):
        result = _run(server.call_tool("create_invoice", {
            "client_id": 5, "date": "2026-02-05junk", "visible_subject": "Test",
            "items": [{"name": "Item", "qty": 1, "net_price": 100.0}],
        }))

    assert not create.called
    assert json.loads(result[0].text)["success"] is False


@pytest.mark.parametrize("days", [10 ** 10, -5])
def test_create_invoice_rejects_payment_days_out_of_range(server_module, days):
    """timedelta raises above 999999999, and a negative one puts the due date
    before the document."""
    server = server_module

    with patch.object(server.issued_api, "create_issued_document") as create, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme", "ei_code": "A1"}):
        result = _run(server.call_tool("create_invoice", {
            "client_id": 5, "date": "2026-01-10", "payment_days": days,
            "visible_subject": "Test",
            "items": [{"name": "Item", "qty": 1, "net_price": 100.0}],
        }))

    assert not create.called
    assert "payment_days" in json.loads(result[0].text)["error"]


def test_duplicate_invoice_keeps_the_whole_ei_data(server_module):
    """A copy that drops the IBAN or the linked order is a different document."""
    server = server_module
    doc = _duplicate_source(e_invoice=True, ei_data={
        "payment_method": "MP19", "bank_iban": "IT60X0542811101000000123456",
        "original_document_type": "ordine",
    })
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

    ei_data = create.call_args.kwargs["create_issued_document_request"]["data"]["ei_data"]
    assert ei_data["payment_method"] == "MP19"
    assert ei_data["bank_iban"] == "IT60X0542811101000000123456"
    assert ei_data["original_document_type"] == "ordine"


def test_duplicate_invoice_treats_a_null_e_invoice_as_electronic(server_module):
    """to_dict() emits unset keys: the `, True` default never fires on a null."""
    server = server_module
    doc = _duplicate_source(e_invoice=None, ei_data=None)
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
    assert body["e_invoice"] is True
    assert body["ei_data"]["payment_method"] == "MP05"


# --------------------------------------------------------------------------
# review round 16
# --------------------------------------------------------------------------

@pytest.mark.parametrize("stored_days", [5000, 45.0, None])
def test_update_document_absorbs_a_stored_payment_days(server_module, stored_days):
    """The stored value is not an argument: refusing an unrelated edit and
    naming payment_days points at something the caller never sent."""
    server = server_module
    doc = _issued_doc([_rate(1220.0, "2026-02-09",
                             payment_terms={"days": stored_days, "type": "standard"})])

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


def test_update_document_date_error_names_the_value_it_parsed(server_module):
    """With no date argument the default is the stored one: blaming 'None'
    points at an argument the caller never wrote."""
    server = server_module
    doc = _issued_doc([_rate(1220.0, "2026-02-09")])
    doc["date"] = "10/02/2026"

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document") as modify, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme"}):
        result = _run(server.call_tool("update_document", {
            "document_id": 42, "visible_subject": "Nuovo",
        }))

    assert not modify.called
    error = json.loads(result[0].text)["error"]
    assert "10/02/2026" in error
    assert "'None'" not in error


def test_duplicate_invoice_drops_null_ei_data_fields(server_module):
    """to_dict() emits unset keys as null: copying them writes explicit nulls
    over every EI field the source did not have."""
    server = server_module
    doc = _duplicate_source(e_invoice=True, ei_data={
        "payment_method": "MP19", "bank_iban": None, "cup": None,
    })
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

    ei_data = create.call_args.kwargs["create_issued_document_request"]["data"]["ei_data"]
    assert ei_data == {"payment_method": "MP19"}


def test_update_document_treats_a_null_e_invoice_as_electronic(server_module):
    """Same field, same null, as duplicate_invoice — and here the body
    replaces what is stored."""
    server = server_module
    doc = _issued_doc([_rate(1220.0, "2026-02-09")])
    doc["e_invoice"] = None
    doc["ei_data"] = {"payment_method": "MP19"}

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document",
                      return_value=_doc_response(doc)) as modify, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme"}):
        _run(server.call_tool("update_document", {
            "document_id": 42, "visible_subject": "Nuovo",
        }))

    data = _sent_data(modify, "modify_issued_document_request")
    assert data["e_invoice"] is True
    assert data["ei_data"]["payment_method"] == "MP19"


def test_update_document_rebuild_keeps_a_float_payment_days(server_module):
    """An integral float is the same term: falling back to 30 would move the
    due date of a document nobody rescheduled."""
    server = server_module
    doc = _issued_doc([_rate(1220.0, "2026-02-24",
                             payment_terms={"days": 45.0, "type": "standard"})])

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document",
                      return_value=_doc_response(doc)) as modify, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme"}):
        _run(server.call_tool("update_document", {
            "document_id": 42,
            "items": [{"name": "Item", "qty": 1, "net_price": 2000.0, "vat_rate": 22}],
        }))

    sent = _sent_payments(modify, "modify_issued_document_request")
    assert sent[0]["payment_terms"]["days"] == 45
    assert sent[0]["due_date"] == "2026-02-24"


# --------------------------------------------------------------------------
# review round 18
# --------------------------------------------------------------------------

def test_update_document_omits_ei_data_with_nothing_to_inherit(server_module):
    """Reading the null as electronic opened a branch that writes MP05 on a
    document that never declared a payment method — and here the body
    replaces what is stored."""
    server = server_module
    doc = _issued_doc([_rate(1220.0, "2026-02-09")])
    doc["e_invoice"] = None
    doc["ei_data"] = None

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document",
                      return_value=_doc_response(doc)) as modify, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme"}):
        _run(server.call_tool("update_document", {
            "document_id": 42, "visible_subject": "Nuovo",
        }))

    data = _sent_data(modify, "modify_issued_document_request")
    assert data["e_invoice"] is True
    assert "ei_data" not in data


def test_convert_proforma_inherits_the_payment_method(server_module):
    """The second link of the fallback chain exists for documents that are not
    e-invoices: a proforma with SEPA configured has it."""
    server = server_module
    proforma = _issued_doc([_rate(1220.0, "2026-02-09")])
    proforma["type"] = "proforma"
    proforma["payment_method"] = {"id": 7, "name": "RID", "ei_payment_method": "MP19"}
    created = MagicMock()
    created.data.to_dict.return_value = {"id": 11, "number": 3, "date": "2026-02-01"}

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(proforma)), \
         patch.object(server.issued_api, "create_issued_document",
                      return_value=created) as create, \
         patch.object(server.issued_api, "delete_issued_document"), \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme", "ei_code": "A1"}):
        _run(server.call_tool("convert_proforma_to_invoice", {"document_id": 10}))

    body = create.call_args.kwargs["create_issued_document_request"]["data"]
    assert body["e_invoice"] is True  # the tool's contract is an e-invoice
    assert body["ei_data"]["payment_method"] == "MP19"


# --------------------------------------------------------------------------
# review round 19
# --------------------------------------------------------------------------

CLIENT_WITH_METHOD = {
    "name": "Acme", "ei_code": "A1",
    # not MP05: that is the fallback, the one value where "read from the
    # registry" and "fell back to the constant" agree
    "default_payment_method": {"id": 7, "name": "RID", "ei_payment_method": "MP19"},
}


def test_create_invoice_uses_the_client_default_payment_method(server_module):
    """Every invoice declared MP05 in the XML; the client registry carries the
    method the FIC panel itself uses."""
    server = server_module
    client = CLIENT_WITH_METHOD
    created = MagicMock()
    created.data.to_dict.return_value = {"id": 1, "number": 1, "date": "2026-01-10"}

    with patch.object(server.issued_api, "create_issued_document", return_value=created) as create, \
         patch.object(server, "get_client_by_id", return_value=client):
        _run(server.call_tool("create_invoice", {
            "client_id": 5, "date": "2026-01-10", "visible_subject": "Test",
            "items": [{"name": "Item", "qty": 1, "net_price": 100.0}],
        }))

    body = create.call_args.kwargs["create_issued_document_request"]["data"]
    assert body["ei_data"]["payment_method"] == "MP19"
    assert body["payment_method"] == {"id": 7}


def test_create_invoice_falls_back_to_mp05_without_a_client_method(server_module):
    server = server_module
    created = MagicMock()
    created.data.to_dict.return_value = {"id": 1, "number": 1, "date": "2026-01-10"}

    with patch.object(server.issued_api, "create_issued_document", return_value=created) as create, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme", "ei_code": "A1"}):
        _run(server.call_tool("create_invoice", {
            "client_id": 5, "date": "2026-01-10", "visible_subject": "Test",
            "items": [{"name": "Item", "qty": 1, "net_price": 100.0}],
        }))

    body = create.call_args.kwargs["create_issued_document_request"]["data"]
    assert body["ei_data"]["payment_method"] == "MP05"
    assert "payment_method" not in body


def test_duplicate_invoice_echoes_the_payment_method_field(server_module):
    """Inheriting the method into the XML but not into the field leaves the
    copy with an empty payment method in the FIC panel."""
    server = server_module
    doc = _duplicate_source(e_invoice=True, ei_data={"payment_method": "MP19"},
                            payment_method={"id": 7, "name": "RID", "ei_payment_method": "MP19"})
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

    assert create.call_args.kwargs["create_issued_document_request"]["data"]["payment_method"] == {"id": 7}


def test_convert_proforma_echoes_the_payment_method_field(server_module):
    server = server_module
    proforma = _issued_doc([_rate(1220.0, "2026-02-09")])
    proforma["type"] = "proforma"
    proforma["payment_method"] = {"id": 7, "name": "RID", "ei_payment_method": "MP19"}
    created = MagicMock()
    created.data.to_dict.return_value = {"id": 11, "number": 3, "date": "2026-02-01"}

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(proforma)), \
         patch.object(server.issued_api, "create_issued_document",
                      return_value=created) as create, \
         patch.object(server.issued_api, "delete_issued_document"), \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme", "ei_code": "A1"}):
        _run(server.call_tool("convert_proforma_to_invoice", {"document_id": 10}))

    assert create.call_args.kwargs["create_issued_document_request"]["data"]["payment_method"] == {"id": 7}


# --------------------------------------------------------------------------
# independent review
# --------------------------------------------------------------------------

def _withholding_doc():
    """A professional invoice: 1000 + 22% VAT, 20% ritenuta d'acconto, so FIC
    stores an installment of 1020, not the 1220 the lines add up to."""
    doc = _issued_doc([_rate(1020.0, "2026-02-09")])
    doc["withholding_tax"] = 20.0
    doc["withholding_tax_taxable"] = 100.0
    return doc


def test_update_document_keeps_the_stored_amount_when_items_are_untouched(server_module):
    """total_abs models lines only: ritenuta, cassa, bollo and rivalsa live at
    document level, so an items-derived total is not the document's total."""
    server = server_module
    doc = _withholding_doc()

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
    assert sent[0]["amount"] == 1020.0


def test_update_document_reschedule_keeps_the_stored_amount(server_module):
    server = server_module
    doc = _withholding_doc()

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document",
                      return_value=_doc_response(doc)) as modify, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme"}):
        _run(server.call_tool("update_document", {
            "document_id": 42, "payment_days": 60,
        }))

    sent = _sent_payments(modify, "modify_issued_document_request")
    assert sent[0]["amount"] == 1020.0


def test_update_document_lets_fic_size_a_rebuilt_installment(server_module):
    """Recomputing the lines cannot reproduce a total FIC derives from
    document-level modifiers — so FIC sizes the installment instead."""
    server = server_module
    doc = _withholding_doc()

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document",
                      return_value=_doc_response(doc)) as modify, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme"}):
        result = _run(server.call_tool("update_document", {
            "document_id": 42,
            "items": [{"name": "Item", "qty": 1, "net_price": 2000.0, "vat_rate": 22}],
        }))

    assert json.loads(result[0].text)["success"] is True
    request = modify.call_args.kwargs["modify_issued_document_request"]
    assert request["options"] == {"fix_payments": True}


def test_update_document_does_not_fix_payments_it_preserved(server_module):
    """Preserved installments are the user's plan: FIC must not resize the
    last one to match the total."""
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
            "document_id": 42, "visible_subject": "Nuovo",
        }))

    assert "options" not in modify.call_args.kwargs["modify_issued_document_request"]


def test_set_payment_keeps_the_payment_account_type(server_module):
    """PaymentAccount.type defaults to standard in the SDK model, so sending
    only the id retypes a bank account."""
    server = server_module
    doc = _issued_doc([_rate(1220.0, "2026-02-09", "paid", paid_date="2026-02-05",
                             payment_account={"id": 110, "name": "Banca", "type": "bank"})])

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document",
                      return_value=_doc_response(doc)) as modify:
        _run(server.call_tool("set_payment", {
            "document_id": 42, "document_type": "issued", "status": "paid",
            "paid_date": "2026-02-06",
        }))

    sent = _sent_payments(modify, "modify_issued_document_request")
    assert sent[0]["payment_account"] == {"id": 110, "type": "bank"}


def test_resolve_payment_account_reports_an_empty_registry(server_module):
    """An outage propagates to the shared error path (round 42); what is left
    for this branch is a registry with nothing in it — not an account that
    does not exist, and not something a retry fixes."""
    server = server_module
    import cache as cache_mod
    cache_mod.invalidate_all(100)

    with patch.object(server.info_api, "list_payment_accounts",
                      return_value=_accounts_response([])):
        account, error = server.resolve_payment_account("Banca Intesa")

    assert account is None
    assert "vuota" in error
    assert "non esiste" not in error
    assert "riprova" not in error


def test_resolve_vat_type_reports_an_unreadable_registry(server_module):
    server = server_module
    import cache as cache_mod
    cache_mod.invalidate_all(100)

    with patch.object(server.info_api, "list_vat_types", side_effect=RuntimeError("503")):
        vat_type, error = server.resolve_vat_type(22)

    assert vat_type is None
    assert "anagrafica IVA" in error


def test_duplicate_invoice_copies_document_level_modifiers(server_module):
    """The lines keep apply_withholding_taxes, so a copy without the document's
    withholding percentage is due a different amount than the original."""
    server = server_module
    doc = _duplicate_source(withholding_tax=20.0, withholding_tax_taxable=100.0,
                            stamp_duty=2.0, use_split_payment=True)
    doc["items_list"][0]["apply_withholding_taxes"] = True
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
    assert body["withholding_tax"] == 20.0
    assert body["stamp_duty"] == 2.0
    assert body["use_split_payment"] is True


def test_get_situation_reads_enum_statuses_from_the_sdk(server_module):
    """The handler, not just the helper: with plain strings the pre-fix
    expression works, so nothing pinned the bug the CHANGELOG leads with."""
    server = server_module
    doc = _sdk_shaped(_issued_doc([_rate(1220.0, "2026-02-09", "paid", paid_date="2026-02-05")]))
    listed = MagicMock()
    listed.data = [MagicMock(**{"to_dict.return_value": doc})]
    listed.last_page = 1
    empty = MagicMock()
    empty.data = []
    empty.last_page = 1

    with patch.object(server.issued_api, "list_issued_documents",
                      side_effect=[listed, empty]), \
         patch.object(server.received_api, "list_received_documents", return_value=empty):
        result = _run(server.call_tool("get_situation", {"year": 2026}))

    assert json.loads(result[0].text)["incassato"] == 1220.0


def test_update_document_clamps_a_stored_out_of_range_payment_days(server_module):
    """The clamp only bites on the rebuild branch: 5000 days would move the due
    date years out."""
    server = server_module
    doc = _issued_doc([_rate(1220.0, "2026-02-09",
                             payment_terms={"days": 5000, "type": "standard"})])

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document",
                      return_value=_doc_response(doc)) as modify, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme"}):
        _run(server.call_tool("update_document", {
            "document_id": 42,
            "items": [{"name": "Item", "qty": 1, "net_price": 2000.0, "vat_rate": 22}],
        }))

    sent = _sent_payments(modify, "modify_issued_document_request")
    assert sent[0]["due_date"] == "2026-02-09"  # document date + 30, not + 5000


def test_create_invoice_does_not_leak_internal_fields(server_module):
    server = server_module
    created = MagicMock()
    created.data.to_dict.return_value = {"id": 1, "number": 1, "date": "2026-01-10"}

    with patch.object(server.issued_api, "create_issued_document", return_value=created) as create, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme", "ei_code": "A1"}):
        _run(server.call_tool("create_invoice", {
            "client_id": 5, "date": "2026-01-10", "visible_subject": "Test",
            "items": [{"name": "Item", "qty": 1, "net_price": 100.0}],
        }))

    item = create.call_args.kwargs["create_issued_document_request"]["data"]["items_list"][0]
    assert not [k for k in item if k.startswith("_")]


def test_update_document_refuses_a_document_sent_to_sdi(server_module):
    """The contrast set_payment's own test names."""
    server = server_module
    doc = _issued_doc([_rate(1220.0, "2026-02-09")], ei_status="sent")

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document") as modify, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme"}):
        result = _run(server.call_tool("update_document", {
            "document_id": 42, "visible_subject": "Nuovo",
        }))

    assert not modify.called
    assert json.loads(result[0].text)["success"] is False


@pytest.mark.parametrize("page", [0, -1, "2"])
def test_list_invoices_rejects_a_bad_page(server_module, page):
    server = server_module

    with patch.object(server.issued_api, "list_issued_documents") as listed:
        result = _run(server.call_tool("list_invoices", {"year": 2026, "page": page}))

    assert not listed.called
    assert "page" in json.loads(result[0].text)["error"]


# --------------------------------------------------------------------------
# independent review, round 2
# --------------------------------------------------------------------------

def test_create_proforma_sets_the_client_payment_method(server_module):
    """payment_method is not an e-invoice field: without it on the proforma,
    the conversion has nothing to inherit and falls back to MP05."""
    server = server_module
    created = MagicMock()
    created.data.to_dict.return_value = {"id": 1, "number": 1, "date": "2026-01-10"}

    with patch.object(server.issued_api, "create_issued_document", return_value=created) as create, \
         patch.object(server, "get_client_by_id", return_value=CLIENT_WITH_METHOD):
        _run(server.call_tool("create_proforma", {
            "client_id": 5, "date": "2026-01-10", "visible_subject": "Test",
            "items": [{"name": "Item", "qty": 1, "net_price": 100.0}],
        }))

    body = create.call_args.kwargs["create_issued_document_request"]["data"]
    assert body["payment_method"] == {"id": 7}
    assert "ei_data" not in body


def test_create_invoice_declares_mp05_without_an_ei_mapping(server_module):
    """A configured method with no electronic mapping: the id is set, and MP05
    stays the only sensible thing to declare in the XML."""
    server = server_module
    client = {"name": "Acme", "ei_code": "A1",
              "default_payment_method": {"id": 7, "name": "Contanti"}}
    created = MagicMock()
    created.data.to_dict.return_value = {"id": 1, "number": 1, "date": "2026-01-10"}

    with patch.object(server.issued_api, "create_issued_document", return_value=created) as create, \
         patch.object(server, "get_client_by_id", return_value=client):
        _run(server.call_tool("create_invoice", {
            "client_id": 5, "date": "2026-01-10", "visible_subject": "Test",
            "items": [{"name": "Item", "qty": 1, "net_price": 100.0}],
        }))

    body = create.call_args.kwargs["create_issued_document_request"]["data"]
    assert body["payment_method"] == {"id": 7}
    assert body["ei_data"]["payment_method"] == "MP05"


def test_payment_method_survives_the_sdk_request_model(server_module):
    """original_document was dropped by the model for a whole release: assert
    the key reaches the wire instead of only the mock."""
    from fattureincloud_python_sdk.models.create_issued_document_request import (
        CreateIssuedDocumentRequest,
    )
    server = server_module
    created = MagicMock()
    created.data.to_dict.return_value = {"id": 1, "number": 1, "date": "2026-01-10"}

    with patch.object(server.issued_api, "create_issued_document", return_value=created) as create, \
         patch.object(server, "get_client_by_id", return_value=CLIENT_WITH_METHOD):
        _run(server.call_tool("create_invoice", {
            "client_id": 5, "date": "2026-01-10", "visible_subject": "Test",
            "items": [{"name": "Item", "qty": 1, "net_price": 100.0}],
        }))

    request = create.call_args.kwargs["create_issued_document_request"]
    serialized = CreateIssuedDocumentRequest.from_dict(request).to_dict()
    assert serialized["data"]["payment_method"]["id"] == 7
    assert serialized["data"]["ei_data"]["payment_method"] == "MP19"


def test_creation_paths_ask_fic_to_fix_the_installment(server_module):
    """The local total models the lines only: FIC recomputes the installment
    from the document it actually built."""
    server = server_module
    doc = _duplicate_source(withholding_tax=20.0, withholding_tax_taxable=100.0)
    created = MagicMock()
    created.data.to_dict.return_value = {"id": 2, "number": 9, "date": "2026-03-01"}

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "create_issued_document",
                      return_value=created) as create, \
         patch.object(server, "get_client_by_id", return_value=CLIENT_WITH_METHOD):
        _run(server.call_tool("duplicate_invoice", {
            "source_document_id": 42, "new_date": "2026-03-01",
        }))

    assert create.call_args.kwargs["create_issued_document_request"]["options"] == {"fix_payments": True}


def test_convert_proforma_copies_document_level_modifiers(server_module):
    server = server_module
    proforma = _issued_doc([_rate(1020.0, "2026-02-09")])
    proforma["type"] = "proforma"
    proforma["withholding_tax"] = 20.0
    proforma["withholding_tax_taxable"] = 100.0
    created = MagicMock()
    created.data.to_dict.return_value = {"id": 11, "number": 3, "date": "2026-02-01"}

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(proforma)), \
         patch.object(server.issued_api, "create_issued_document",
                      return_value=created) as create, \
         patch.object(server.issued_api, "delete_issued_document"), \
         patch.object(server, "get_client_by_id", return_value=CLIENT_WITH_METHOD):
        _run(server.call_tool("convert_proforma_to_invoice", {"document_id": 10}))

    request = create.call_args.kwargs["create_issued_document_request"]
    assert request["data"]["withholding_tax"] == 20.0
    assert request["options"] == {"fix_payments": True}


def test_update_document_without_installments_does_not_write_a_zero(server_module):
    """The sum of no installments is not a total."""
    server = server_module
    doc = _issued_doc([])

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document",
                      return_value=_doc_response(doc)) as modify, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme"}):
        _run(server.call_tool("update_document", {
            "document_id": 42, "visible_subject": "Nuovo",
        }))

    sent = _sent_payments(modify, "modify_issued_document_request")
    assert sent[0]["amount"] == 1220.0


def test_update_document_reports_the_stored_total(server_module):
    """The response asserted a total computed from the lines, i.e. the number
    the installment stopped carrying."""
    server = server_module
    doc = _withholding_doc()

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme"}):
        result = _run(server.call_tool("update_document", {
            "document_id": 42, "visible_subject": "Nuovo",
        }))

    assert json.loads(result[0].text)["total"] == 1020.0


def test_update_document_allows_item_edits_with_only_a_taxable_base(server_module):
    """A taxable base is not a rate: on its own it changes no total, and
    refusing on it would refuse every item edit."""
    server = server_module
    doc = _issued_doc([_rate(1220.0, "2026-02-09")])
    doc["withholding_tax_taxable"] = 100.0
    doc["global_cassa_taxable"] = 100.0

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document",
                      return_value=_doc_response(doc)) as modify, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme"}):
        result = _run(server.call_tool("update_document", {
            "document_id": 42,
            "items": [{"name": "Item", "qty": 1, "net_price": 1000.0, "vat_rate": 22}],
        }))

    assert modify.called
    assert json.loads(result[0].text)["success"] is True


def test_update_document_on_a_modifier_document_without_installments(server_module):
    """The rebuild is forced here, so the amount cannot come from the lines."""
    server = server_module
    doc = _issued_doc([])
    doc["withholding_tax"] = 20.0

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document",
                      return_value=_doc_response(doc)) as modify, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme"}):
        result = _run(server.call_tool("update_document", {
            "document_id": 42, "visible_subject": "Nuovo",
        }))

    assert json.loads(result[0].text)["success"] is True
    assert modify.call_args.kwargs["modify_issued_document_request"]["options"] == {"fix_payments": True}


def test_create_invoice_reports_the_totals_fic_stored(server_module):
    """Once FIC sizes the installment, the locally computed total is a guess:
    the response carries the document as written."""
    server = server_module
    created = MagicMock()
    # through the SDK models: amount_gross is read-only and does not survive
    created.data.to_dict.return_value = _sdk_shaped({
        "id": 1, "number": 1, "type": "invoice", "date": "2026-01-10",
        "amount_gross": 1022.0,  # 1000 + VAT - ritenuta + bollo, as FIC computed it
        "payments_list": [{"amount": 1022.0, "due_date": "2026-03-01", "status": "not_paid"}],
    })

    with patch.object(server.issued_api, "create_issued_document", return_value=created), \
         patch.object(server, "get_client_by_id", return_value=CLIENT_WITH_METHOD):
        result = _run(server.call_tool("create_invoice", {
            "client_id": 5, "date": "2026-01-10", "payment_days": 30,
            "visible_subject": "Test",
            "items": [{"name": "Item", "qty": 1, "net_price": 1000.0}],
        }))

    payload = json.loads(result[0].text)
    assert payload["total"] == 1022.0
    assert payload["due_date"] == "2026-03-01"


def test_create_invoice_asks_fic_to_fix_the_installment(server_module):
    server = server_module
    created = MagicMock()
    created.data.to_dict.return_value = {"id": 1, "number": 1, "date": "2026-01-10"}

    with patch.object(server.issued_api, "create_issued_document", return_value=created) as create, \
         patch.object(server, "get_client_by_id", return_value=CLIENT_WITH_METHOD):
        _run(server.call_tool("create_invoice", {
            "client_id": 5, "date": "2026-01-10", "visible_subject": "Test",
            "items": [{"name": "Item", "qty": 1, "net_price": 100.0}],
        }))

    assert create.call_args.kwargs["create_issued_document_request"]["options"] == {"fix_payments": True}


def test_update_document_reports_the_totals_fic_stored(server_module):
    """The fourth write path: with fix_payments the local total is an estimate
    FIC has already replaced."""
    server = server_module
    doc = _withholding_doc()
    stored = _sdk_shaped({
        "id": 42, "number": 7, "type": "invoice", "date": "2026-01-10",
        "payments_list": [{"amount": 2440.0, "due_date": "2026-03-01", "status": "not_paid"}],
    })

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document",
                      return_value=_doc_response(stored)), \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme"}):
        result = _run(server.call_tool("update_document", {
            "document_id": 42,
            "items": [{"name": "Item", "qty": 1, "net_price": 2000.0, "vat_rate": 22}],
        }))

    payload = json.loads(result[0].text)
    assert payload["total"] == 2440.0
    assert payload["due_date"] == "2026-03-01"


def test_update_document_refusal_does_not_blame_an_unchanged_total(server_module):
    """On a document with ritenuta the two numbers differ by the withholding,
    not because anyone changed the total."""
    server = server_module
    doc = _withholding_doc()
    doc["payments_list"][0]["status"] = "paid"
    doc["payments_list"][0]["paid_date"] = "2026-02-05"

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document") as modify, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme"}):
        result = _run(server.call_tool("update_document", {
            "document_id": 42,
            "items": [{"name": "Item", "qty": 1, "net_price": 1000.0, "vat_rate": 22}],
        }))

    assert not modify.called
    error = json.loads(result[0].text)["error"]
    assert "totale ricalcolato" not in error
    assert "ritenuta" in error or "withholding_tax" in error


# --------------------------------------------------------------------------
# review round 24
# --------------------------------------------------------------------------

def _sdk_received(payload):
    from fattureincloud_python_sdk.models.received_document import ReceivedDocument
    return ReceivedDocument.from_dict(payload).to_dict()


def test_get_situation_counts_costs_gross(server_module):
    """Revenue comes from the installments (VAT included) while costs fell back
    to amount_net, so the margin was overstated by the VAT on every purchase."""
    server = server_module
    expense = _sdk_received(_received_doc([]))  # 500 + 110 VAT
    empty = MagicMock()
    empty.data = []
    empty.last_page = 1
    costs = MagicMock()
    costs.data = [MagicMock(**{"to_dict.return_value": expense})]
    costs.last_page = 1

    with patch.object(server.issued_api, "list_issued_documents",
                      side_effect=[empty, empty]), \
         patch.object(server.received_api, "list_received_documents",
                      side_effect=[costs, empty]):
        result = _run(server.call_tool("get_situation", {"year": 2026}))

    assert json.loads(result[0].text)["costi_totali"] == 610.0


def test_list_received_documents_reports_a_gross_total(server_module):
    """Its issued twin reports the installments, i.e. gross."""
    server = server_module
    doc = MagicMock()
    doc.to_dict.return_value = _sdk_received(_received_doc([]))
    listed = MagicMock()
    listed.data = [doc]
    listed.last_page = 1

    with patch.object(server.received_api, "list_received_documents", return_value=listed):
        result = _run(server.call_tool("list_received_documents", {"year": 2026}))

    assert json.loads(result[0].text)["documents"][0]["total"] == 610.0


def test_get_received_document_reports_a_gross_amount(server_module):
    server = server_module
    doc = _sdk_received(_received_doc([_rate(610.0, "2026-02-09")]))

    with patch.object(server.received_api, "get_received_document",
                      return_value=_doc_response(doc)):
        result = _run(server.call_tool("get_received_document", {"document_id": 99}))

    assert json.loads(result[0].text)["amount_gross"] == 610.0


def test_update_document_plan_refusal_does_not_mention_modifiers(server_module):
    """Only the schedule moved: no total was compared, so nothing about the
    total is what refused the edit."""
    server = server_module
    doc = _issued_doc([
        _rate(610.0, "2026-01-31", "paid", paid_date="2026-01-30"),
        _rate(610.0, "2026-02-28"),
    ])
    doc["withholding_tax"] = 20.0

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document") as modify, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme"}):
        result = _run(server.call_tool("update_document", {
            "document_id": 42, "payment_days": 60,
        }))

    assert not modify.called
    error = json.loads(result[0].text)["error"]
    assert "non è riproducibile" not in error
    assert "totale ricalcolato" not in error


def test_duplicate_invoice_refuses_a_credit_note(server_module):
    """Duplicating a credit note as an invoice turns a reversal into a debit."""
    server = server_module
    doc = _duplicate_source()
    doc["type"] = "credit_note"

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "create_issued_document") as create, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme", "ei_code": "A1"}):
        result = _run(server.call_tool("duplicate_invoice", {
            "source_document_id": 42, "new_date": "2026-03-01",
        }))

    assert not create.called
    assert json.loads(result[0].text)["success"] is False


# --------------------------------------------------------------------------
# review round 25
# --------------------------------------------------------------------------

def test_get_situation_subtracts_supplier_credit_notes(server_module):
    """Revenue subtracts issued credit notes; costs have to subtract the
    supplier ones or a 610 EUR reversal stays in the margin forever."""
    server = server_module
    expense = MagicMock()
    expense.to_dict.return_value = _sdk_received(_received_doc([]))
    credit = MagicMock()
    credit.to_dict.return_value = _sdk_received(
        dict(_received_doc([]), type="passive_credit_note")
    )
    empty = MagicMock()
    empty.data = []
    empty.last_page = 1
    costs = MagicMock()
    costs.data = [expense]
    costs.last_page = 1
    credits_page = MagicMock()
    credits_page.data = [credit]
    credits_page.last_page = 1

    with patch.object(server.issued_api, "list_issued_documents",
                      side_effect=[empty, empty]), \
         patch.object(server.received_api, "list_received_documents",
                      side_effect=[costs, credits_page]) as listed:
        result = _run(server.call_tool("get_situation", {"year": 2026}))

    # side_effect answers whatever type is asked for: pin the one that makes
    # this a credit note rather than a self_invoice
    assert listed.call_args_list[1].kwargs["type"] == "passive_credit_note"
    assert json.loads(result[0].text)["costi_totali"] == 0.0


def test_duplicate_invoice_points_a_proforma_at_the_converter(server_module):
    """The refusal must not send to the panel what this server can do."""
    server = server_module
    doc = _duplicate_source()
    doc["type"] = "proforma"

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "create_issued_document") as create, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme", "ei_code": "A1"}):
        result = _run(server.call_tool("duplicate_invoice", {
            "source_document_id": 42, "new_date": "2026-03-01",
        }))

    assert not create.called
    error = json.loads(result[0].text)["error"]
    assert "convert_proforma_to_invoice" in error
    # that tool deletes the proforma unless told otherwise, and duplicate_invoice
    # is by contract non-destructive: the caller asked for a copy
    assert "keep_proforma" in error


def test_set_payment_synthesizes_an_installment_net_of_withholding(server_module):
    """A supplier with ritenuta is paid gross minus the withholding, which is
    the amount the payment registers."""
    server = server_module
    doc = _sdk_received(dict(_received_doc([]), amount_withholding_tax=100.0))
    stored = _sdk_received(dict(
        _received_doc([_rate(510.0, "2026-01-10", status="paid")]),
        amount_withholding_tax=100.0,
    ))

    with patch.object(server.received_api, "get_received_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.received_api, "modify_received_document",
                      return_value=_doc_response(stored)) as modify:
        _run(server.call_tool("set_payment", {
            "document_id": 99, "document_type": "received", "status": "paid",
            "paid_date": "2026-02-05",
        }))

    sent = _sent_payments(modify, "modify_received_document_request")
    assert sent[0]["amount"] == 510.0  # 500 + 110 VAT - 100 withholding


# --------------------------------------------------------------------------
# review round 26
# --------------------------------------------------------------------------

def test_get_situation_exposes_the_supplier_credit_note_total(server_module):
    """costi_totali changes value twice in this release: without the addend
    nobody can reconcile it against the FIC panel."""
    server = server_module
    expense = MagicMock()
    expense.to_dict.return_value = _sdk_received(_received_doc([]))
    credit = MagicMock()
    credit.to_dict.return_value = _sdk_received(
        dict(_received_doc([]), type="passive_credit_note")
    )
    empty = MagicMock()
    empty.data = []
    empty.last_page = 1
    costs = MagicMock()
    costs.data = [expense]
    costs.last_page = 1
    credits_page = MagicMock()
    credits_page.data = [credit]
    credits_page.last_page = 1

    with patch.object(server.issued_api, "list_issued_documents",
                      side_effect=[empty, empty]), \
         patch.object(server.received_api, "list_received_documents",
                      side_effect=[costs, credits_page]):
        result = _run(server.call_tool("get_situation", {"year": 2026}))

    payload = json.loads(result[0].text)
    assert payload["note_fornitore"] == 610.0
    assert payload["costi_totali"] == 0.0


def test_set_payment_explains_the_synthesized_amount(server_module):
    """get_received_document reports 610 and the installment says 500: the
    difference has to be readable somewhere, and this is the only branch where
    the amount is decided here rather than by FIC."""
    server = server_module
    doc = _sdk_received(dict(
        _received_doc([]),
        amount_withholding_tax=100.0,
        amount_other_withholding_tax=10.0,
    ))
    stored = _sdk_received(dict(
        _received_doc([_rate(500.0, "2026-01-10", status="paid")]),
        amount_withholding_tax=100.0,
        amount_other_withholding_tax=10.0,
    ))

    with patch.object(server.received_api, "get_received_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.received_api, "modify_received_document",
                      return_value=_doc_response(stored)) as modify:
        result = _run(server.call_tool("set_payment", {
            "document_id": 99, "document_type": "received", "status": "paid",
            "paid_date": "2026-02-05",
        }))

    assert _sent_payments(modify, "modify_received_document_request")[0]["amount"] == 500.0
    nota = json.loads(result[0].text)["nota_importo"]
    assert "610" in nota and "110" in nota


# --------------------------------------------------------------------------
# review round 27
# --------------------------------------------------------------------------

def test_no_description_sends_to_the_converter_without_keep_proforma(server_module):
    """The description is read before the call; the refusal only after one.
    Whoever points at that tool has to name the flag that keeps the source."""
    server = server_module
    tools = _run(server.list_tools())
    # a bare mention is a cross-reference (list_cost_centers lists the tools it
    # validates); "usa <tool>" is what sends the caller there
    pointing = [t for t in tools if "usa convert_proforma_to_invoice" in (t.description or "")]

    assert pointing, "no tool points at the converter: the guard has lost its subject"
    for tool in pointing:
        assert "keep_proforma" in tool.description, tool.name


def test_get_received_document_reports_the_withholding_and_what_is_due(server_module):
    """After set_payment the document stores a 500 installment against a 610
    gross: the two numbers have to be reconcilable in every later read."""
    server = server_module
    doc = _sdk_received(dict(
        _received_doc([_rate(500.0, "2026-02-09")]),
        amount_withholding_tax=100.0,
        amount_other_withholding_tax=10.0,
    ))

    with patch.object(server.received_api, "get_received_document",
                      return_value=_doc_response(doc)):
        result = _run(server.call_tool("get_received_document", {"document_id": 99}))

    payload = json.loads(result[0].text)
    assert payload["amount_gross"] == 610.0
    assert payload["amount_withholding_tax"] == 100.0
    assert payload["amount_other_withholding_tax"] == 10.0
    assert payload["amount_due"] == 500.0
    assert payload["payments"][0]["amount"] == 500.0


def test_get_situation_reads_the_same_way_on_both_sides(server_module):
    """Revenue exposes lordo - note = netto. Costs exposed only the difference
    and the subtrahend, so applying the same rule subtracts them twice."""
    server = server_module
    expense = MagicMock()
    expense.to_dict.return_value = _sdk_received(
        dict(_received_doc([]), amount_net=1000.0, amount_vat=220.0)
    )
    credit = MagicMock()
    credit.to_dict.return_value = _sdk_received(dict(
        _received_doc([]), type="passive_credit_note",
        amount_net=250.0, amount_vat=55.0,
    ))
    empty = MagicMock()
    empty.data = []
    empty.last_page = 1
    costs = MagicMock()
    costs.data = [expense]
    costs.last_page = 1
    credits_page = MagicMock()
    credits_page.data = [credit]
    credits_page.last_page = 1

    with patch.object(server.issued_api, "list_issued_documents",
                      side_effect=[empty, empty]), \
         patch.object(server.received_api, "list_received_documents",
                      side_effect=[costs, credits_page]):
        result = _run(server.call_tool("get_situation", {"year": 2026}))

    payload = json.loads(result[0].text)
    # three distinct numbers: no formula passes by coincidence
    assert payload["costi_lordi"] == 1220.0
    assert payload["note_fornitore"] == 305.0
    assert payload["costi_totali"] == 915.0


# --------------------------------------------------------------------------
# review round 28
# --------------------------------------------------------------------------

def test_set_payment_is_annotated_destructive(server_module):
    """status=not_paid drops paid_date and payment_account, which the server
    cannot reconstruct: destructiveHint is what a client reads to ask first."""
    server = server_module
    tool = next(t for t in _run(server.list_tools()) if t.name == "set_payment")
    assert tool.annotations.destructiveHint is True


def test_set_payment_rounds_the_synthesized_amount(server_module):
    """Every other write path rounds currency; a raw float reaches FIC as
    0.30000000000000004."""
    server = server_module
    doc = _sdk_received(dict(_received_doc([]), amount_net=0.1, amount_vat=0.2))
    stored = _sdk_received(dict(
        _received_doc([_rate(0.3, "2026-01-10", status="paid")]),
        amount_net=0.1, amount_vat=0.2,
    ))

    with patch.object(server.received_api, "get_received_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.received_api, "modify_received_document",
                      return_value=_doc_response(stored)) as modify:
        _run(server.call_tool("set_payment", {
            "document_id": 99, "document_type": "received", "status": "paid",
        }))

    assert _sent_payments(modify, "modify_received_document_request")[0]["amount"] == 0.3


def test_update_document_echoes_the_document_payment_method(server_module):
    """convert_proforma_to_invoice and duplicate_invoice echo it; if the PUT is
    not a merge, an unrelated edit clears it and leaves ei_data declaring a
    method the document no longer has."""
    server = server_module
    doc = _sdk_shaped(dict(
        _issued_doc([_rate(1220.0, "2026-02-09")]),
        payment_method={"id": 17, "name": "Bonifico"},
    ))

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document",
                      return_value=_doc_response(doc)) as modify, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme", "ei_code": "A1"}):
        _run(server.call_tool("update_document", {
            "document_id": 42, "visible_subject": "Nuovo oggetto",
        }))

    assert _sent_data(modify, "modify_issued_document_request")["payment_method"] == {"id": 17}


@pytest.mark.parametrize("arguments", [
    {"year": "2026'; drop"},
    {"year": 0},
    {"year": True},
    {"year": 2026, "month": "gennaio"},
    {"year": 2026, "month": 13},
    {"year": 2026, "month": 0},
])
def test_list_invoices_refuses_a_malformed_period(server_module, arguments):
    """year and month are interpolated into the FIC query filter, and month
    reaches {month:02d}: a string used to come back as a TypeError traceback."""
    server = server_module
    with patch.object(server.issued_api, "list_issued_documents") as listed:
        result = _run(server.call_tool("list_invoices", arguments))

    assert not listed.called
    payload = json.loads(result[0].text)
    assert payload["success"] is False
    assert "Traceback" not in result[0].text


def test_list_invoices_defaults_to_the_current_year(server_module):
    """The other three period tools use datetime.now().year; this one was
    pinned to 2024."""
    server = server_module
    listed = MagicMock()
    listed.data = []
    listed.last_page = 1

    with patch.object(server.issued_api, "list_issued_documents",
                      return_value=listed) as call:
        _run(server.call_tool("list_invoices", {}))

    assert str(datetime.now().year) in call.call_args.kwargs["q"]


def test_get_situation_caps_the_pages_it_reads(server_module):
    """Four detailed lists paged to the end is dozens of sequential calls
    against a rate-limited API; a capped total says it is partial."""
    server = server_module
    page = MagicMock()
    page.data = []
    page.last_page = 500

    with patch.object(server.issued_api, "list_issued_documents",
                      return_value=page) as issued, \
         patch.object(server.received_api, "list_received_documents",
                      return_value=page) as received:
        result = _run(server.call_tool("get_situation", {"year": 2026}))

    assert json.loads(result[0].text)["parziale"] is True
    assert issued.call_count + received.call_count <= 4 * server.MAX_PAGES


def test_check_numeration_says_when_it_did_not_read_the_whole_year(server_module):
    """A capped read invents gaps: the invoices it never fetched look missing."""
    server = server_module
    doc = MagicMock()
    doc.to_dict.return_value = {"number": 1, "date": "2026-01-10"}
    page = MagicMock()
    page.data = [doc]
    page.last_page = 500

    with patch.object(server.issued_api, "list_issued_documents", return_value=page):
        result = _run(server.call_tool("check_numeration", {"year": 2026}))

    payload = json.loads(result[0].text)
    assert payload["parziale"] is True
    assert "parziale" in payload["nota"].lower() or "buchi" in payload["nota"].lower()


def test_an_unexpected_error_does_not_return_a_traceback(server_module, capsys):
    """The catch-all handed the client local paths and internal structure, and
    an MCP client puts that straight back into the conversation."""
    server = server_module

    with patch.object(server.issued_api, "list_issued_documents",
                      side_effect=RuntimeError("boom at /home/someone/secret.py")):
        result = _run(server.call_tool("list_invoices", {"year": 2026}))

    payload = json.loads(result[0].text)
    assert payload["success"] is False
    assert "Traceback" not in result[0].text
    assert "site-packages" not in result[0].text
    assert "RuntimeError" in payload["error"]
    # the detail is still available to whoever runs the server
    assert "Traceback" in capsys.readouterr().err


def test_create_invoice_sends_the_line_discount(server_module):
    """build_items_list rebuilds the line from scratch, so a discount the
    caller passed used to be dropped without a word — while _item_net applies
    one on every path that echoes a stored line."""
    server = server_module
    created = MagicMock()
    created.data.to_dict.return_value = _sdk_shaped(_issued_doc([_rate(1098.0, "2026-02-09")]))

    with patch.object(server.issued_api, "create_issued_document",
                      return_value=created) as create, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme", "ei_code": "A1"}):
        result = _run(server.call_tool("create_invoice", {
            "client_id": 5,
            "items": [{"name": "Consulenza", "qty": 1, "net_price": 1000.0, "discount": 10}],
        }))

    assert json.loads(result[0].text)["success"] is True
    sent = create.call_args.kwargs["create_issued_document_request"]["data"]["items_list"][0]
    assert sent["discount"] == 10


# --------------------------------------------------------------------------
# review round 29
# --------------------------------------------------------------------------

def test_check_numeration_does_not_claim_continuity_it_did_not_verify(server_module):
    """status is the string a model quotes first, with a tick in front of it."""
    server = server_module
    doc = MagicMock()
    doc.to_dict.return_value = {"number": 1, "date": "2026-01-10"}
    page = MagicMock()
    page.data = [doc]
    page.last_page = 500

    with patch.object(server.issued_api, "list_issued_documents", return_value=page):
        result = _run(server.call_tool("check_numeration", {"year": 2026}))

    payload = json.loads(result[0].text)
    assert payload["parziale"] is True
    assert payload["continuous"] is None
    assert "✓" not in payload["status"]


def test_set_payment_does_not_invent_an_installment_to_clear(server_module):
    """not_paid on a document with no schedule has nothing to clear: writing a
    plan that did not exist is not what 'annulla' means."""
    server = server_module
    doc = _sdk_received(_received_doc([]))

    with patch.object(server.received_api, "get_received_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.received_api, "modify_received_document") as modify:
        result = _run(server.call_tool("set_payment", {
            "document_id": 99, "document_type": "received", "status": "not_paid",
        }))

    assert not modify.called
    assert json.loads(result[0].text)["success"] is False


def test_set_payment_always_says_when_it_invented_the_installment(server_module):
    """Without the note a synthesized installment reads exactly like a stored
    one, withholding or not."""
    server = server_module
    doc = _sdk_received(_received_doc([]))
    stored = _sdk_received(_received_doc([_rate(610.0, "2026-01-10", status="paid")]))

    with patch.object(server.received_api, "get_received_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.received_api, "modify_received_document",
                      return_value=_doc_response(stored)):
        result = _run(server.call_tool("set_payment", {
            "document_id": 99, "document_type": "received", "status": "paid",
        }))

    nota = json.loads(result[0].text)["nota_importo"]
    assert "610" in nota
    assert "ritenuta" not in nota.lower()


# --------------------------------------------------------------------------
# review round 30
# --------------------------------------------------------------------------

def test_check_numeration_does_not_raise_an_alarm_it_did_not_verify(server_module):
    """A truncated read produces missing numbers almost by construction: the
    branch with gaps is the likely one, not the exception."""
    server = server_module
    docs = []
    for number in (1, 5):
        doc = MagicMock()
        doc.to_dict.return_value = {"number": number, "date": "2026-01-10"}
        docs.append(doc)
    page = MagicMock()
    page.data = docs
    page.last_page = 500

    with patch.object(server.issued_api, "list_issued_documents", return_value=page):
        result = _run(server.call_tool("check_numeration", {"year": 2026}))

    payload = json.loads(result[0].text)
    assert payload["parziale"] is True
    assert payload["gaps"]
    assert "⚠" not in payload["status"]
    assert "parziale" in payload["status"].lower()


# --------------------------------------------------------------------------
# review round 32
# --------------------------------------------------------------------------

@pytest.mark.parametrize("tool_name", ["get_situation", "check_numeration"])
def test_the_aggregating_tools_announce_a_partial_read(server_module, tool_name):
    """The model reads the description, not the README: parziale must not be
    the first time it hears the year might not have been read whole."""
    server = server_module
    tool = next(t for t in _run(server.list_tools()) if t.name == tool_name)
    assert "parziale" in tool.description


# --------------------------------------------------------------------------
# review round 33
# --------------------------------------------------------------------------

def test_set_payment_does_not_report_a_payment_the_document_does_not_carry(server_module):
    """`or` treated an explicitly empty payments_list like an omitted one, so a
    document that came back without the installment was reported as paid."""
    server = server_module
    doc = _sdk_received(_received_doc([_rate(610.0, "2026-02-09")]))
    wiped = _sdk_received(_received_doc([]))

    with patch.object(server.received_api, "get_received_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.received_api, "modify_received_document",
                      return_value=_doc_response(wiped)):
        result = _run(server.call_tool("set_payment", {
            "document_id": 99, "document_type": "received", "status": "paid",
        }))

    payload = json.loads(result[0].text)
    assert payload["success"] is False
    assert "610" not in json.dumps(payload.get("payments", []))


def test_an_api_error_reaches_the_caller_but_an_internal_one_does_not(server_module, capsys):
    """The FIC rejection is what the caller has to act on; the message of an
    arbitrary internal exception is not, and can carry local paths."""
    from fattureincloud_python_sdk.exceptions import ApiException
    server = server_module

    with patch.object(server.issued_api, "list_issued_documents",
                      side_effect=ApiException(status=422, reason="vat_id not found")):
        api = json.loads(_run(server.call_tool("list_invoices", {"year": 2026}))[0].text)

    with patch.object(server.issued_api, "list_issued_documents",
                      side_effect=RuntimeError("boom at /home/someone/secret.py")):
        internal = json.loads(_run(server.call_tool("list_invoices", {"year": 2026}))[0].text)

    assert "422" in api["error"] and "vat_id not found" in api["error"]
    assert "RuntimeError" in internal["error"]
    assert "/home/someone/secret.py" not in internal["error"]
    assert "secret.py" in capsys.readouterr().err


# --------------------------------------------------------------------------
# review round 34
# --------------------------------------------------------------------------

def test_set_payment_flags_a_response_that_did_not_report_the_installments(server_module):
    """to_dict() omits keys whose value is None, so a modify response that does
    not carry the schedule arrives as a missing key, not as an empty list."""
    server = server_module
    doc = _sdk_received(_received_doc([_rate(610.0, "2026-02-09")]))
    silent = {k: v for k, v in _sdk_received(_received_doc([])).items()
              if k != "payments_list"}

    with patch.object(server.received_api, "get_received_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.received_api, "modify_received_document",
                      return_value=_doc_response(silent)):
        result = _run(server.call_tool("set_payment", {
            "document_id": 99, "document_type": "received", "status": "paid",
        }))

    payload = json.loads(result[0].text)
    assert payload["success"] is True
    assert "inviate" in payload["warning"]


def test_set_payment_names_the_lost_schedule_when_clearing(server_module):
    """Clearing a payment and getting no installments back does not mean the
    payment was not registered: it means the plan is gone."""
    server = server_module
    doc = _sdk_received(_received_doc([_rate(610.0, "2026-02-09", status="paid",
                                             paid_date="2026-02-09")]))
    wiped = _sdk_received(_received_doc([]))

    with patch.object(server.received_api, "get_received_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.received_api, "modify_received_document",
                      return_value=_doc_response(wiped)):
        result = _run(server.call_tool("set_payment", {
            "document_id": 99, "document_type": "received", "status": "not_paid",
        }))

    error = json.loads(result[0].text)["error"]
    assert "piano rate" in error
    assert "non risulta registrato" not in error


def test_an_api_error_does_not_carry_the_response_headers(server_module):
    """status, reason and body are what the caller acts on; the headers of the
    FIC response help nobody and travel into the conversation."""
    from fattureincloud_python_sdk.exceptions import ApiException
    server = server_module
    error = ApiException(status=422, reason="Unprocessable")
    error.body = '{"error":"vat_id not found"}'
    error.headers = {"x-request-id": "abc123", "set-cookie": "session=zzz"}

    with patch.object(server.issued_api, "list_issued_documents", side_effect=error):
        payload = json.loads(_run(server.call_tool("list_invoices", {"year": 2026}))[0].text)

    assert "422" in payload["error"]
    assert "vat_id not found" in payload["error"]
    assert "session=zzz" not in payload["error"]


# --------------------------------------------------------------------------
# review round 35
# --------------------------------------------------------------------------

def test_set_payment_reports_every_warning_it_found(server_module):
    """Three independent checks wrote to one variable: the last one to fire
    erased the others, and the rows below stayed unqualified."""
    server = server_module
    doc = _sdk_shaped(_issued_doc([_rate(1220.0, "2026-02-09")]))
    silent = {k: v for k, v in _sdk_shaped(_issued_doc([])).items()
              if k != "payments_list"}
    silent["items_list"] = []

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document",
                      return_value=_doc_response(silent)):
        result = _run(server.call_tool("set_payment", {
            "document_id": 42, "document_type": "issued", "status": "paid",
        }))

    warning = json.loads(result[0].text)["warning"]
    assert "inviate" in warning
    assert "items_list" in warning


def test_an_api_error_falls_back_to_the_deserialized_body(server_module):
    """__init__ fills body from the raw response, but leaves it None when the
    decode raises — and then what FIC refused is only in data."""
    from fattureincloud_python_sdk.exceptions import ApiException
    server = server_module
    error = ApiException(status=422, reason="Unprocessable")
    error.body = None
    error.data = {"error": "vat_id not found"}

    with patch.object(server.issued_api, "list_issued_documents", side_effect=error):
        payload = json.loads(_run(server.call_tool("list_invoices", {"year": 2026}))[0].text)

    assert "vat_id not found" in payload["error"]


# --------------------------------------------------------------------------
# review round 36
# --------------------------------------------------------------------------

def test_an_api_error_without_the_expected_attributes_still_answers(server_module):
    """status / reason / body / data belong to the generated runtime, not to a
    declared API: an AttributeError raised inside the except would escape
    call_tool and leave the client with no response at all."""
    from fattureincloud_python_sdk.exceptions import ApiException

    class Sparse(ApiException):
        def __init__(self):
            pass  # a future runtime that stops setting them

    server = server_module
    with patch.object(server.issued_api, "list_issued_documents", side_effect=Sparse()):
        result = _run(server.call_tool("list_invoices", {"year": 2026}))

    payload = json.loads(result[0].text)
    assert payload["success"] is False
    # "ApiException: None None" says neither what failed nor where to look
    assert "None None" not in payload["error"]
    assert "list_invoices" in payload["error"]


# --------------------------------------------------------------------------
# review round 38
# --------------------------------------------------------------------------

@pytest.mark.parametrize("status,reason,expected", [
    (422, None, "422"),
    (None, "Unprocessable", "Unprocessable"),
])
def test_an_api_error_does_not_print_the_attribute_it_lacks(server_module, status, reason, expected):
    """The guard is an `or`, so a half-populated exception still reached the
    caller as `422 None`."""
    from fattureincloud_python_sdk.exceptions import ApiException
    server = server_module
    error = ApiException(status=status, reason=reason)

    with patch.object(server.issued_api, "list_issued_documents", side_effect=error):
        payload = json.loads(_run(server.call_tool("list_invoices", {"year": 2026}))[0].text)

    assert expected in payload["error"]
    assert "None" not in payload["error"]


# --------------------------------------------------------------------------
# review round 39
# --------------------------------------------------------------------------

def test_an_api_error_with_only_a_body_does_not_start_with_a_colon(server_module):
    """The join left `detail` empty, so the refusal arrived as `: {...}`."""
    from fattureincloud_python_sdk.exceptions import ApiException
    server = server_module
    error = ApiException()
    error.data = {"error": "vat_id not found"}

    with patch.object(server.issued_api, "list_issued_documents", side_effect=error):
        payload = json.loads(_run(server.call_tool("list_invoices", {"year": 2026}))[0].text)

    assert "vat_id not found" in payload["error"]
    assert ": :" not in payload["error"]
    assert "ApiException: {" in payload["error"]


@pytest.mark.parametrize("kind,named,other", [
    ("issued", "cliente", "fornitore"),
    ("received", "fornitore", "cliente"),
])
def test_set_payment_flags_a_document_that_lost_its_counterparty(server_module, kind, named, other):
    """items_list was the only guard on issued documents, so one with no lines
    had nothing watching it — and the received half of the same condition, which
    predates the change, had no test emptying the entity in the response."""
    server = server_module
    if kind == "issued":
        doc = _sdk_shaped(dict(_issued_doc([_rate(1220.0, "2026-02-09")]), items_list=[]))
        wiped = dict(_sdk_shaped(_issued_doc([_rate(1220.0, "2026-02-09", status="paid")])),
                     entity={}, items_list=[])
        api, getter, modifier = (server.issued_api, "get_issued_document",
                                 "modify_issued_document")
    else:
        doc = _sdk_received(_received_doc([_rate(610.0, "2026-02-09")]))
        wiped = dict(_sdk_received(_received_doc([_rate(610.0, "2026-02-09", status="paid")])),
                     entity={})
        api, getter, modifier = (server.received_api, "get_received_document",
                                 "modify_received_document")

    with patch.object(api, getter, return_value=_doc_response(doc)), \
         patch.object(api, modifier, return_value=_doc_response(wiped)):
        result = _run(server.call_tool("set_payment", {
            "document_id": 42, "document_type": kind, "status": "paid",
        }))

    warning = json.loads(result[0].text)["warning"]
    assert f"senza {named}" in warning
    assert other not in warning


# --------------------------------------------------------------------------
# review round 42
# --------------------------------------------------------------------------

def test_payment_entry_keeps_the_paid_date_of_a_reversed_installment(server_module):
    """The serializer echoes an installment back as read. A reversed payment
    that still carries its paid_date lost it on every unrelated write, because
    the date was kept only for `paid`."""
    server = server_module
    doc = _issued_doc([_rate(1220.0, "2026-02-09", "reversed", paid_date="2026-02-05")])

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document",
                      return_value=_doc_response(doc)) as modify, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme"}):
        _run(server.call_tool("update_document", {
            "document_id": 42, "visible_subject": "Nuovo oggetto",
        }))

    sent = _sent_payments(modify, "modify_issued_document_request")
    assert sent[0]["status"] == "reversed"
    assert sent[0]["paid_date"] == "2026-02-05"


def test_list_payment_accounts_reports_an_outage_instead_of_an_empty_registry(server_module):
    """`[]` during an outage reads as a company with no accounts."""
    from fattureincloud_python_sdk.exceptions import ApiException
    server = server_module
    import cache as cache_mod
    cache_mod.invalidate_all(100)

    with patch.object(server.info_api, "list_payment_accounts",
                      side_effect=ApiException(status=503, reason="Service Unavailable")):
        result = _run(server.call_tool("list_payment_accounts", {}))

    payload = json.loads(result[0].text)
    assert payload["success"] is False
    assert "503" in payload["error"]


def test_set_payment_does_not_write_when_the_account_registry_is_unreachable(server_module):
    """With the account unresolvable the write must not happen, and the
    answer has to be the API failure, not a message about the account."""
    from fattureincloud_python_sdk.exceptions import ApiException
    server = server_module
    import cache as cache_mod
    cache_mod.invalidate_all(100)
    doc = _issued_doc([_rate(1220.0, "2026-02-09")])

    with patch.object(server.info_api, "list_payment_accounts",
                      side_effect=ApiException(status=503, reason="Service Unavailable")), \
         patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document") as modify:
        result = _run(server.call_tool("set_payment", {
            "document_id": 42, "document_type": "issued", "status": "paid",
            "payment_account": "Banca Intesa",
        }))

    payload = json.loads(result[0].text)
    assert payload["success"] is False
    assert "503" in payload["error"]
    assert "non esiste" not in payload["error"]
    assert modify.call_count == 0


def test_set_payment_reports_the_write_when_only_the_account_names_are_unreadable(server_module):
    """The registry is read again after the write, to name the accounts in the
    answer. A failure there must not turn a registered payment into an error:
    the caller would retry, or worse, believe nothing was written."""
    from fattureincloud_python_sdk.exceptions import ApiException
    server = server_module
    import cache as cache_mod
    cache_mod.invalidate_all(100)
    doc = _issued_doc([_rate(1220.0, "2026-02-09")])
    paid = _issued_doc([_rate(1220.0, "2026-02-09", "paid", paid_date="2026-02-05")])

    with patch.object(server.info_api, "list_payment_accounts",
                      side_effect=ApiException(status=503, reason="Service Unavailable")), \
         patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document",
                      return_value=_doc_response(paid)) as modify:
        result = _run(server.call_tool("set_payment", {
            "document_id": 42, "document_type": "issued", "status": "paid",
            "paid_date": "2026-02-05",
        }))

    payload = json.loads(result[0].text)
    assert modify.call_count == 1
    assert payload["success"] is True
    assert payload["payments"][0]["status"] == "paid"


def test_due_date_end_of_month_counts_the_days_then_closes_the_month(server_module):
    """FattureInCloud's own fix note names a document dated the 31st before a
    30-day month as the case that broke `30 giorni fine mese`: the day count
    comes first, the month end after."""
    from datetime import date
    server = server_module
    assert server._due_date(date(2026, 3, 31), 30, "end_of_month") == date(2026, 4, 30)
    assert server._due_date(date(2026, 1, 10), 0, "end_of_month") == date(2026, 1, 31)
    assert server._due_date(date(2026, 1, 10), 30, "standard") == date(2026, 2, 9)


def test_update_document_reschedules_an_end_of_month_installment_to_the_month_end(server_module):
    """The one branch that never hands the installment to fix_payments: a
    settled single installment whose only change is the schedule. It kept the
    fine-mese label and wrote a mid-month date under it."""
    server = server_module
    doc = _issued_doc([_rate(1220.0, "2026-01-31", "paid", paid_date="2026-01-20",
                             payment_account={"id": 110, "name": "Banca Intesa"},
                             payment_terms={"days": 0, "type": "end_of_month"})])

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(doc)), \
         patch.object(server.issued_api, "modify_issued_document",
                      return_value=_doc_response(doc)) as modify, \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme"}):
        _run(server.call_tool("update_document", {"document_id": 42, "payment_days": 30}))

    sent = _sent_payments(modify, "modify_issued_document_request")
    assert len(sent) == 1
    assert sent[0]["status"] == "paid"
    assert sent[0]["paid_date"] == "2026-01-20"
    assert sent[0]["payment_terms"] == {"days": 30, "type": "end_of_month"}
    # 2026-01-10 + 30 = 2026-02-09, then to the end of February
    assert sent[0]["due_date"] == "2026-02-28"


def test_convert_proforma_keeps_end_of_month_terms_and_closes_the_month(server_module):
    """The third path that recomputes a due date under a preserved label."""
    server = server_module
    proforma = _issued_doc([_rate(1220.0, "2026-02-28",
                                  payment_terms={"days": 30, "type": "end_of_month"})])
    proforma["type"] = "proforma"
    created = MagicMock()
    created.data.to_dict.return_value = {"id": 11, "number": 3, "date": "2026-03-05"}

    with patch.object(server.issued_api, "get_issued_document",
                      return_value=_doc_response(proforma)), \
         patch.object(server.issued_api, "create_issued_document",
                      return_value=created) as create, \
         patch.object(server.issued_api, "delete_issued_document"), \
         patch.object(server, "get_client_by_id", return_value={"name": "Acme", "ei_code": "A1"}):
        _run(server.call_tool("convert_proforma_to_invoice",
                              {"document_id": 10, "date": "2026-03-05"}))

    payment = create.call_args.kwargs["create_issued_document_request"]["data"]["payments_list"][0]
    assert payment["payment_terms"] == {"days": 30, "type": "end_of_month"}
    assert payment["due_date"] == "2026-04-30"
