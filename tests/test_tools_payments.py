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
                      return_value=_accounts_response(ACCOUNTS)):
        yield server


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
