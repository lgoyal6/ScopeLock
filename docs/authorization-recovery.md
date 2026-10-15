# Local authorization and recovery contract

`PolicyService` owns the shared API, MCP and voice authorization logic. A confirmed
mandate is an immutable numbered version with a four-hour default lifetime.
Revocation cancels pending decisions and interrupts the case. Reconfirmation after
revocation or expiry creates a new version; an old holder decision cannot authorize
that version.

Effect authorization and its SQLite effect row share a `BEGIN IMMEDIATE`
transaction. Revocation therefore orders before or after an effect, rather than
between its check and record. An effect committed before revocation remains part
of history. Repeated callbacks return the prior effect; conflicting reuse of a key
is refused. This records a local authorized effect, not a completed action at an
external institution. An external connector needs its own idempotency and outcome
reconciliation before such a claim is valid.

The holder's Spanish answer must itself be affirmative. An English translation
cannot turn a negative or uncertain answer into permission. A supplied conflicting
translation also refuses. The bilingual trigger screen only removes authority.
Its bounded informational-question grammar distinguishes asking why an identifier
is required from disclosing that identifier. Appended instructions, identifier
values and explicitly forbidden typed actions still refuse. This is a finite
pattern screen, not general language understanding or verified translation quality.

Run `.venv/bin/python -m pytest -q` for the offline suite. The test configuration
uses a placeholder credential, disables SDK telemetry and bypasses the SDK-only
deprecation metadata check during collection. Socket connections are blocked.
Four real roleplay tests remain skipped. `--live-guava` is an explicit opt-in that
still requires separate approval for provider usage; this build did not run it.

The two local studies are reproducible with `PYTHONPATH=.` and private paths:

- `bench/policy_sequences.py --inputs PATH --output PATH` compares at least 1,000
  frozen operation sequences with an independent in-memory state machine.
- `bench/bilingual_policy.py --inputs PATH --output PATH` compares curated bilingual
  trigger pairs with explicit labels and a guard-disabled ablation. Known fixture
  regressions and external or unseen-language validation must remain distinct.

Keep protocol inputs, raw operation receipts, code hashes and before-fix results
outside the repository. Repeated operations are not independent users. Neither
study measures speech recognition, live translations or institutional acceptance.

At startup, the call-owning process may run `consult.recover_after_restart()` to
cancel orphaned pending requests. Resolved decisions and committed effect records
remain durable. Exercise crash/reopen and revocation races using the temporary
SQLite files in the test suite. Stop the local process to roll back a running
instance; preserve worktree changes before restoring an earlier code version.
Back up the SQLite database before applying a schema change to retained data.

State-changing HTTP routes are closed unless `SCOPELOCK_API_TOKEN` is configured.
The bearer token is case-scoped: `scopelock.api.server.case_token(case_id)` derives
HMAC-SHA256(secret, case ID), so a token for one case is rejected on another and the
raw server secret is never accepted. Validation errors report field locations only and
never echo submitted values. Keep the console and stdio MCP transport
inside the trusted local operator boundary. The local resolver's `holder` label
is not proof of a real human's identity. This build does not establish end-user
authentication, production tenant isolation or trusted live callback identity.
Live Guava calls, approved credentials, permitted callers and an independently
observed end-to-end workflow are separate verification requirements.

Vendor adapters on the voice leg (today Guava's `IntentRecognizer` and
`guava.helpers.llm.generate` for translation) are untrusted hints. The contract every
classifier or translator must satisfy is checked in
`tests/test_api_and_provider_contracts.py` by standing in every possible output,
including adversarial ones: no classifier label executes a forbidden commitment or
survives revocation, and no translation turns the holder's Spanish "no" into an effect.
Those tests do not execute a live provider. Swapping in another vendor means rerunning
that suite plus a separately approved live conformance run for the new adapter.
