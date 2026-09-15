# Changelog

## v2.1.0
- NEW: `set_payment` — register an incasso on an issued document or a pagamento on a received one (`status: paid | not_paid`, `paid_date`, `payment_account`, `payment_index`). Unlike `update_document` it also works on documents already sent to SDI, because collecting an invoice after it cleared the SDI is the normal case. On a document with several installments, a call without `payment_index` writes nothing and returns the installment list (index, amount, due date); `payment_index="all"` settles them all. Received documents with no `payments_list` (those created by `create_received_document`) get one synthesized from net + VAT, with the document date as due date.
- NEW: `list_payment_accounts` — the configured payment accounts (banks, cash, cards), cached 24h like the cost centers. `payment_account` in `set_payment` takes a numeric id or an account name (case-insensitive, unique substrings work) and is validated against this list.
- FIX: `update_document` no longer wipes registered payments. It used to rebuild `payments_list` with `status: not_paid`, losing the status, the paid date and the account, and collapsing N installments into one. Now: the existing installments are reused untouched unless the schedule or the total actually changed (values are compared, not argument presence — MCP clients routinely echo back the fields they just read); an edit that would rewrite the schedule is **refused**, naming `set_payment`: that covers documents with several installments (a rebuild would flatten a 30/60/90 plan into one due date, a loss that cannot be reconstructed) and documents with a registered payment whose total would change (that would report cash that was never collected). When only the due date of a single settled installment moves, the installment is rescheduled keeping its status, `paid_date` and `payment_account`; a single unregistered installment is still recomputed as before. Installment amounts are compared by magnitude, since credit notes can store them negative, and `payment_terms.days = 0` (rimessa diretta) is preserved instead of being read back as 30.
- FIX: `get_situation` always reported `incassato: 0.00`, `da_incassare` equal to the full revenue, and an empty `prossime_scadenze`. It compared `'IssuedDocumentStatus.PAID'` with `'paid'`, because the SDK's `to_dict()` returns the enum member and the old normalization left the name uppercased. The new `_payment_status` (also used by `get_invoice` and `get_received_document`) normalizes to `paid` / `not_paid` / `reversed`. Those three fields change value after this upgrade: they were wrong before, not now.
- FIX: the PUT bodies of `set_payment` and `update_document` no longer alter the document type or `show_totals`. The SDK request models default those fields to non-None values (`type: invoice` for issued documents, `type: expense` for received ones, `show_totals: all`) and the defaults were serialized into the body, so registering a payment on a credit note sent it back to FIC as `type: invoice`. The type and `show_totals` of the document just read are now echoed in the body.
- FIX: `set_payment` validates `paid_date` against `YYYY-MM-DD` instead of forwarding a malformed value to FIC as a stacktrace, and replaying the call without `paid_date` keeps the date already registered on the installment rather than moving the payment to today (the tool is annotated idempotent).
- FIX: `get_situation` no longer drops `reversed` installments. They were counted neither as collected nor as outstanding, so a reversed payment disappeared from `prossime_scadenze`; a reversed payment was not collected and is now listed as outstanding.
- FIX: a transient API failure is no longer cached. `cache.cached` also stored the empty result returned by the fallback in `fetch_payment_accounts` / `fetch_cost_centers` / `fetch_revenue_centers`, which made every `payment_account` unresolvable for 24 hours. Empty results are no longer persisted, and an empty file written by an earlier version now reads as a cache miss instead of serving that failure until it expires.
- FIX: `mcp` is pinned to `<2` in `requirements.txt` and `pyproject.toml`. The floor-only `mcp>=1.0.0` resolves to 2.x, whose lowlevel `Server` no longer exposes the `@list_tools()` / `@call_tool()` decorators `server.py` is built on, so a freshly built bundle failed to import at startup — visible only on the first launch in Claude Desktop, not during the build. The published 2.0.0 bundle shipped mcp 1.27.1, so only rebuilds were affected. Porting to the 2.x handler API is a separate change.
- FIX: `scripts/validate.sh` reads the bundle size with the GNU `stat -c '%s'` first and the BSD `-f '%z'` as fallback (the previous order aborted the script on Linux, where `stat -f` inspects the filesystem), warns instead of reporting 0 MB when neither form works, and its tool check now compares the manifest against the tools the bundled server actually serves — which also catches a dependency that breaks the entry point.

## v2.0.0
- FIX: `list_cost_centers` and `revenue_center` / `cost_center` validation now correctly query the right FIC API endpoint. FIC exposes cost centers and revenue centers as **two separate registries** (`/info/cost_centers` and `/info/revenue_centers`); the previous implementation only queried `/info/cost_centers`, which silently broke `revenue_center` validation on every issued document tool when the account had only revenue centers configured (the common case). Fix: two internal cached fetchers (`fetch_cost_centers`, `fetch_revenue_centers`), the `list_cost_centers` MCP tool returns their deduplicated union (matching the FIC UI "Analisi centri c/r" view), and validation on document mutations is type-specific (issued documents validate `revenue_center` against revenue_centers; received documents validate `cost_center` against cost_centers).
- NEW: MCPB bundle for Claude Desktop one-click installation. Distributable artifact `dist/fattureincloud.mcpb` produced by `scripts/build.sh`. Same 23 tools as v1.9.0, no behavioral changes for users.
- NEW: `manifest.json` (manifest_version 0.3) declaring the server entry point, runtime deps, user_config (API token + company ID + sender email), and tool annotations.
- NEW: tool annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`) on all 23 tools, both in `manifest.json` and on the `Tool()` declarations in `server.py`. Helps MCP clients reason about safe-to-replay vs side-effecting calls.
- NEW: `SECURITY.md` (vulnerability disclosure policy).
- NEW: `docs/PRIVACY.md` (Privacy Policy, mirrored at https://media-form.it/privacy-policy.html and referenced from `manifest.json`).
- NEW: `scripts/build.sh` and `scripts/validate.sh` to produce and validate the bundle.
- NEW: `.mcpbignore` to keep the bundle minimal (no tests, no dev artifacts).
- CHANGE: README extended with MCPB installation path, Privacy & Data handling, Trademark disclaimer, and Contributing sections.
- NOTE: v2.0.0 does not introduce or modify any tool. The version bump reflects the new packaging surface (MCPB) targeted at the Anthropic Software Directory.

## v1.9.0
- NEW: `list_cost_centers` — lista i centri di costo/ricavo configurati in FattureInCloud
- NEW: `get_received_document` — dettaglio fattura passiva per ID
- NEW: `create_received_document` — crea documento passivo (expense / credit_note) con fornitore, importi, categoria, descrizione, opzionale `cost_center`
- NEW: parametro opzionale `revenue_center` su `create_invoice`, `create_credit_note`, `create_proforma`, `convert_proforma_to_invoice`, `update_document`, `duplicate_invoice` (validato contro `list_cost_centers`; convert e duplicate ereditano dal documento sorgente se non passato)
- NEW: parametro opzionale `cost_center` su `create_received_document`
- NEW: cache locale file-based per fetch di anagrafica clienti e centri di costo (key per `company_id`, TTL 24h). Configurabile via `FIC_CACHE_DIR` (default `~/.fattureincloud-mcp/cache/`) e disattivabile via `FIC_CACHE_DISABLED=1`. Riduce le chiamate API ridondanti durante il flusso di creazione fatture (la stessa anagrafica veniva fetchata due volte, ora una sola)
- CHANGE: `get_invoice`, `list_invoices`, `list_received_documents` espongono `revenue_center`/`cost_center` nei risultati quando presenti
- KNOWN ISSUE: la duplicazione fatture può fallire per configurazioni cliente specifiche (workaround: duplicare manualmente dal pannello web FIC). Tracciato in `docs/KNOWN_ISSUES.md`
- NOTE: il modulo `cache.py` è collocato a top level; sarà spostato in `server/cache.py` durante un futuro refactor strutturale del modulo `server`

## v1.8.0
- NEW: `convert_proforma_to_invoice` — converte proforma in fattura elettronica (elimina proforma di default, `keep_proforma=True` per mantenerla)
- NEW: `get_pdf_url` — restituisce URL PDF e link web del documento
- NEW: `create_client` — crea nuovo cliente in anagrafica
- NEW: `update_client` — aggiorna dati cliente esistente
- FIX: `get_situation` — ora sottrae le NDC dal fatturato (fatturato_netto = fatture - NDC) e supporta filtro per cliente
- REMOVED: `mark_payment_paid` / `mark_payment_unpaid` / `list_payment_accounts` — l'API FIC richiede un conto di saldo obbligatorio non recuperabile in modo affidabile via SDK. La marcatura pagamenti va eseguita direttamente dal pannello FIC.

## v1.6.4
- FIX: `create_credit_note` — prezzi e payment positivi, FIC inverte internamente per type=credit_note
- FIX: `update_document` — stessa logica per NDC

## v1.6.3
- FIX: tentativo NDC senza payments_list (non funzionava)

## v1.6.2
- FIX: tentativo NDC con payment negativo (non funzionava)

## v1.6.1
- FIX: tentativo abs() su payment per NDC (non funzionava)

## v1.6.0
- NEW: `update_document` — modifica parziale di qualsiasi documento bozza (fattura, NDC, proforma): data, oggetto, righe, giorni pagamento. Carica l'originale e applica solo i campi passati.

## v1.5.0
- NEW: `create_credit_note` — crea nota di credito; importi positivi in input, negativi automaticamente; `source_invoice_id` opzionale
- NEW: `create_proforma` — crea proforma, non inviabile allo SDI
- CHANGE: `list_invoices` accetta parametro `type`: `invoice` (default), `credit_note`, `proforma`

## v1.4.0
- NEW: `list_invoices` accetta parametro `type`

## v1.3.0
- FIX: `create_invoice` include `ei_code` dall'anagrafica
- FIX: `duplicate_invoice` aggiorna `ei_code`
- NEW: `check_numeration`

## v1.2.0
- NEW: `delete_invoice`
- NEW: `payment_days` in `duplicate_invoice`

## v1.1.0
- Release iniziale
