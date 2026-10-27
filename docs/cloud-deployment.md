# Personal cloud deployment

The cloud entry point serves the built React application, every API, and `/mcp` from one FastAPI process. Local mode retains the existing loopback launcher. Cloud mode requires a pre-initialized library and complete settings; it never bootstraps an anonymous session or a new empty volume.

## Configure login

Copy `deploy/.env.example` to a deployment-only `.env` (outside Git). Configure a standard OIDC authorization-code client, with callback `https://YOUR-HOST/auth/callback`, the exact issuer URL and the owner's fixed `sub`. Email/display name is not an ownership check. Generate two independent secrets with `python -c "import secrets; print(secrets.token_urlsafe(48))"`. Keep them with the deployment secrets, separate from library backups. The app uses Authlib for state, nonce, PKCE and signed ID-token verification. Session and flow cookies are opaque; authorization state stays server-side. Account registration is disabled in this application.

Only exact `Host` authorities in `LIT_ALLOWED_HOSTS` are accepted. The application does not infer its public URL from forwarded headers. The supplied Nginx example preserves Host, terminates HTTPS and avoids OAuth query-string logs. Use a new virtual host and certificate; the existing apex certificate may not cover a new subdomain.

## Initialize and run

Build `docker compose -f deploy/compose.yml build`. Prepare an empty directory owned by UID/GID 10001. Initialize it explicitly with a one-off container, or restore a verified backup into a new directory. Example (run from the checkout):

```sh
mkdir -p /opt/litworkbench/data
chown 10001:10001 /opt/litworkbench/data
docker run --rm --network none -v /opt/litworkbench/data:/data litworkbench:cloud \
  python -c 'from litworkbench.store import Workspace; Workspace("/data").initialize()'
docker compose -f deploy/compose.yml up -d
```

This is a separate Compose project, image and loopback port `18876`; it does not modify other applications. Check health before installing the new Nginx host. Do not expose port 18876 publicly. The runtime user is non-root; root filesystem is read-only, data is a persistent bind mount, temporary files have a bounded tmpfs, logs rotate. Start exactly one replica/ASGI worker.

The 2 GiB host profile initially disables the background worker and omits OCR model dependencies. Imports remain queued until the worker is deliberately enabled; the settings payload reports worker availability. Do not mistake a queued import for recognized content. To enable OCR, rebuild with `WITH_OCR=true`, configure compatible Linux model paths and measured resource limits, then set `LIT_WORKER_ENABLED=true`. The 768 MiB Compose limit is a web-only starting budget, **not an accepted OCR limit**. Test representative PDFs and subprocess shutdown before enabling long batches alongside other services.

## Backup, migration and restore

Stop writes and the relevant worker before the final migration snapshot. `scripts/migrate_cloud_workspace.py` uses SQLite's backup API under a write exclusion lock and copies the snapshot's referenced immutable source, staging, note-asset and export files. It validates SHA-256 and records table counts/primary IDs. It refuses existing targets and targets inside source or code directories. It does not copy environment files, previous backup directories or unrelated local files. Keep the finished backup on a separate disk/service; a copy on the running server alone is not off-host protection.

```sh
python scripts/migrate_cloud_workspace.py backup /path/to/library /path/to/new-backup
python scripts/migrate_cloud_workspace.py restore /path/to/new-backup /path/to/new-restored-library
```

Restore validates every listed file, database integrity, foreign keys, counts and IDs before creating its target. It upgrades supported older schemas in the copy, clears browser/PAT/OAuth state, pauses unfinished jobs/tasks and clears old process leases. Interrupted OCR attempts become failed with a migration reason while ready/adopted candidates and historical snapshots remain intact. `restore-report.json` preserves previous task states. Missing dependencies fail restore instead of silently creating a runnable partial library. Generated page render caches are rebuilt as needed.

Before manually continuing migrated jobs, verify their staging/source files, model availability and limits; review the migration report. Starting the service does not automatically resume those paused jobs. To roll back, pair the old image with its matching backup in another new directory, preserve any newer writes, verify read-only, then switch the mount. Never run an older image against a newer schema or overwrite the active library.

## Acceptance boundaries

Local fixture OIDC tests exercise actual Authlib signature/claims validation, but do not establish production-provider access. `scripts/accept_codex_mcp.py` tests actual Codex CLI and HTTP against a synthetic library, with per-invocation configuration and temporary process credentials. It does not migrate the user's library or change global Codex configuration. Production acceptance additionally requires a real HTTPS host/certificate, real owner login, backup destination and recovery rehearsal, real Codex connection to that host, and login/readback from a second physical device.
