# ScopeLock bilingual holder walkthrough

The original policy, mandate, SQLite, holder-decision and idempotency code runs
on a fresh fictional case per request. The visitor selects a fixed institutional
request and Spanish/English response. Approval, decline and revocation produce
actual local authorization records. No voice service, live translation,
institution or external effect is connected. All temporary records are removed.

Export a fresh deployment bundle from the repository root:

    python3 scripts/export_public_demo.py --out /tmp/scopelock-public-bundle

The exporter copies only the standard-library runtime needed by this endpoint and
records source hashes. The voice translator is imported lazily by its owning
function, so the public demonstration does not install or initialize Guava.
Deploy the exported bundle, rather than the full voice application.

    PYTHONPATH=. python3 -m unittest tests.test_public_workflow
