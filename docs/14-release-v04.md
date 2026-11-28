# v0.4 — Secure & Admin (staging candidate)

## What changed

- Explicit demo/secure profiles. Secure requires PostgreSQL, configured HTTPS public origin, trusted OIDC issuer, confidential client and bootstrap subject. Tests can explicitly use a temporary SQLite database. Demo preserves the previous Basic/passwordless local behavior. Never expose demo to a network with real patients.
- OIDC authorization-code flow + PKCE S256, single-use server-side state and nonce (5 minutes), RS256 signature validation via discovery/JWKS, issuer/audience/expiry/azp/nonce checks. No access/refresh/ID token stored by Clinic Flow after callback. No role accepted from unverified or external claims; only an allowlisted local subject can obtain a session. Login attempts and sessions use high-entropy opaque cookies; token hashes stored at rest. Staff sessions expire after 8 hours, no sliding expiration. Role change or deactivation revokes existing sessions. Local logout; IdP-wide logout/revocation integration not yet provided.
- Same-origin JSON mutations, exact Origin and Host checks for secure profile; __Host- HttpOnly Secure SameSite=Lax cookies, CSP, HSTS, no-store, disabled OpenAPI UI in secure mode. Nginx TLS, basic rate limits and no direct host port for the application/database. Protect proxy logs and secrets; browser-only CSRF strategy requires same origin.
- Individual operator roles in /admin. One deployment = one clinic; multi-tenant isolation is NOT implemented. Bootstrap admin is provisioned from the exact OIDC subject at first start. Changing bootstrap configuration requires a controlled access review; old accounts are not silently disabled. The configured bootstrap account cannot be modified through the regular admin API.
- Device provisioning: one-time 10-minute random pairing code, hashed at rest; a 30-day opaque device cookie; device role and optional board room scope; heartbeat every 30 seconds, online indication (75 seconds) and revocation. Re-pair via a new device code after expiry; revoke prior devices. Kiosks have facility-wide registration access in this one-clinic profile. Boards cannot mutate queues. Device credentials are not hardware-attested.
- Audit: user changes, login/logout, device create/pair/revoke and queue mutation events with actor, action, object, status and timestamp. Domain changes and audit commit in the same DB transaction; replay does not create a second mutation audit record. Demo actions have demo/system actors. Audit is an application-level append path, not immutable storage; DB operators can alter it. No complete read-access/denial logging or retention policy yet. Protect audit and staff identity data.
- Migration Alembic 0002_security / SQLite schema v4 is additive. Legacy import utility remains limited to synthetic SQLite v2/v3 into migrated empty PostgreSQL. It does not migrate credentials or sessions. Back up before updating.

## Role matrix (server enforced)

| Action | Admin | Registrar | Doctor | Analyst | Board device | Kiosk device |
|---|---|---|---|---|---|---|
| Admin users/devices/audit; room config | yes | no | no | no | no | no |
| Register / create demo appointment | yes | yes | no | no | no | registration only |
| Queue list / call / action | yes | yes | assigned rooms | no | no | no |
| Transfer | yes | yes | source and destination assigned | no | no | no |
| Full ticket history | yes | yes | no | no | no | no |
| Aggregate analytics | yes | no | no | yes | no | no |
| Board snapshot | yes | yes | assigned rooms | no | assigned room or explicitly general board | no |
| Device heartbeat | no | no | no | no | own | own |

The secure policy denies unlisted API routes/methods. Every request checks the current session/device and scope. SSE carries only generic invalidation and rechecks the credential at each notification/heartbeat (up to 10 seconds); timing can reveal aggregate activity within the same clinic. A command already authorized and in flight can finish during revocation. Idempotency command keys are scoped by authenticated actor; old demo keys remain compatible.

## How to run

Demo: existing compose.yaml or compose.postgres.yaml, ports remain bound to loopback.

Secure staging: configure an approved Keycloak realm following deploy/keycloak/README.md. Provide a TLS certificate and private key in local-tls/fullchain.pem and local-tls/privkey.pem (or set CLINIC_TLS_DIR). Cert must match public origin and be trusted by test browsers. Set in a private .env (see .env.example):
- CLINIC_POSTGRES_PASSWORD
- CLINIC_PUBLIC_ORIGIN (e.g. https://localhost:8443; match certificate and IdP callback)
- CLINIC_OIDC_ISSUER, CLINIC_OIDC_CLIENT_ID, CLINIC_OIDC_CLIENT_SECRET
- CLINIC_BOOTSTRAP_SUBJECT (exact subject; not username/email)

    docker compose -f compose.secure.yaml up --build -d

Secure profile uses a separate PostgreSQL volume. Never assume the legacy data is copied automatically. Access /admin through OIDC; provision users first. Add a board or kiosk in admin, copy its code into /pair in a separate browser/device, confirm registration, heartbeat and revocation. Device pairing codes are secrets; do not share them in tickets or logs.

## Tests and release gates

GitHub Actions runs existing queue regression, PostgreSQL integration, real HTTP SSE smoke, Playwright demo smoke, and security tests on SQLite/PostgreSQL. Synthetic OIDC provider tests real RSA-signed tokens, PKCE, nonce, issuer/audience, invalid/expired claims, replay, logout, local role scopes, CSRF, device pairing/revocation, audit and fail-closed config. Consult the workflow for the precise commit/result.

Still needed: actual Keycloak + Nginx/TLS end-to-end deployment test (not replaced by mock tests), secure UI browser E2E, clinical authorization matrix validation, multi-facility data isolation if required, IdP account/session lifecycle synchronization, CSRF and penetration review, secrets management, credential rotation, immutable audit/retention and approved log handling, distributed SSE, backup/restore drills, device hardening and load testing. No real patient data, no production or regulatory compliance claim. Docker Compose secure deployment has not yet been verified on real hardware.

Next stage: v0.5 mock MIS adapter and contract, durable inbox/outbox, appointment changes/reconciliation, then real integration only after vendor authorization.
