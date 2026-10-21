# Security findings, 2026-10-20

These came up while building the `/api/v1/app/configurations` allowlist (branch
`fix/config-allowlist-no-secrets`). They are **outside that story** and not fixed by it. This file contains
names and file paths only, no secret values.

## 1. Service-role key in the admin's browser bundle
- **Repo:** `montyesim-eshop-admin`
- **Where:** `src/core/apis/usersAPI.jsx:20-35` says `VITE_SUPABASE_KEY` holds the Supabase **service-role**
  key.
- **Why it matters:** any `VITE_*` variable is compiled into the JavaScript the browser downloads. Anyone who
  can load the admin site can extract the key. The service-role key bypasses row-level security on every table.
- **Suggested direction:** move privileged admin operations behind a server, and rotate the service-role key
  afterwards.

## 2. `app_config` readable and writable with the public anon key
- **Repos:** `montyesim-eshop-admin` and `montyesim-eshop-api`.
- **Admin policies:** `src/core/procedures/rls.sql:4-26` and `src/core/procedures/rls-db-format.sql:5-14`
  define SELECT, INSERT, UPDATE and DELETE policies on `app_config` with `USING (true)`.
- **API grants:** `supabase_ddl.sql` grants `anon` and `authenticated` delete, insert, select, update (and more)
  on `app_config`.
- **Why it matters:** the anon key is public. The API returns it as `SUPABASE_BASE_ANON_KEY`, and the mobile app
  and SDKs use it. If these policies and grants are live, anyone can read and change `app_config` directly
  through Supabase, without going through `/configurations`. That covers settings such as payment types and
  login type, and any secret rows that still exist.
- **To check on QA and production:** whether RLS is enabled on `app_config` and which policies are live.

## 3. Secret values copied into `audit_log`; stale settings form can re-insert deleted rows
- **Repo:** `montyesim-eshop-admin`
- **Copies in `audit_log`:** every settings save inserts full `old_data` / `new_data` snapshots of the
  `app_config` rows into `audit_log` (`table_name='app_config'`) at `src/core/apis/settingsAPI.jsx:53-62`. The
  Settings Logs screen reads them back (`settingsAPI.jsx:99`). Values of secret rows (for example
  `ESIM_HUB_API_KEY`, `DCB_HUB_API_KEY`, `RESELLER_ID`) are probably stored there. Deleting the `app_config` rows
  with `scripts/remove_secrets_from_app_config.py` does not remove those copies.
- **Settings screen:** `src/pages/settings/Settings.jsx:73-76` and `src/pages/settings/EditSettings.jsx` list and
  edit every `app_config` row unmasked, and allow adding arbitrary keys. There is no deny-list for secret-shaped
  keys.
- **Re-insert risk:** `updateSettings` upserts the whole form (`settingsAPI.jsx:44-46`). A Settings edit page
  opened before the clean-up and saved after it would re-insert the deleted secret rows. They wouldn't be
  returned by the API any more, but they would be readable as in finding 2.

## 4. Web `.env.qa` committed to git history
- **Repo:** `montyesim-eshop-web`
- **What:** `.env.qa` was committed and deleted several times: added in 349990a, c9978b1 and 301d978; deleted in
  eca0d6d, 0225cad and 500c0be.
- **Variable names it held:** `VITE_SUPABASE_KEY`, `VITE_APP_API_KEY`, `VITE_APP_VAPID_KEY` and other `VITE_*`
  variables. Their values remain in the repo history.
- **To confirm:** most `VITE_*` values are public client keys by design. Confirm that `VITE_SUPABASE_KEY` there
  is the anon key and not a service-role key, and rotate anything that isn't meant to be public.

## Also fixed in the story (for reference)
`app/services/integration/esim_hub_service.py` logged every Hub request as a curl line at debug level, with the
`Api-Key` and `Tenant` header values. Loguru's default stderr sink is DEBUG (`app/main.py` only adds an INFO file
sink), so Render logs held those values. The two headers are now masked. **Older Render logs may still contain
them; consider rotating `ESIM_HUB_API_KEY`.**
