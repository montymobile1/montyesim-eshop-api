# Pre-go-live security checklist for a new client Supabase instance (TEOS-329)

Source: SOC "Supabase Estate Security Assessment and Hardening", 20 Oct 2026, top action #6 and section 7
("Hardening baseline for every Supabase database"). Use it for every new client instance and for a re-check of an
existing one. A client does not go live until every box is ticked and the record at the end is filled in.

## 1. Create the database from the template

- [ ] New Supabase project on PostgreSQL **17 or later** (T12).
- [ ] Load `supabase_ddl.sql` **whole**, as `postgres`, stopping at the first error:
  ```sh
  psql "$DB_URL" -X -v ON_ERROR_STOP=1 -f supabase_ddl.sql
  ```
  The last section of the file is the TEOS-329 sweep. It locks the schema: RLS enabled and forced on every table,
  nothing for anon or PUBLIC, authenticated limited to SELECT on the per-user tables, SECURITY DEFINER functions
  pinned to `search_path = ''` and closed to anon, no extension in `public`, no default privileges for anon, and
  pgAudit. No manual fix is needed after it. If it stops with `TEOS-329: ...`, the message names the objects.
  Fix them in `supabase_ddl.sql` and load it again; do not patch the database by hand.
- [ ] A `WARNING: TEOS-329: pgaudit ...` or `ALTER ROLE ... refused` during the load means auditing is not complete.
  Open a Supabase support ticket with the statement from the warning. T11a fails until then.

## 2. Project settings the database cannot hold

- [ ] **Anonymous sign-ins off** (Authentication → Sign In / Providers). T8a fails when they are on, unless the
  SOC accepted it.
- [ ] **Do NOT disable Supabase sign-ups** ("Allow new users to sign up"): email and phone login of new users depend
  on it. Leave email auto-confirm as it is too. The script reports both as WARN, not FAIL:
  - Email login of a new customer calls `sign_in_with_otp` without `should_create_user=False`
    (`app/config/config.py`, `authenticate`). GoTrue creates the account during that call, and it refuses to when
    sign-ups are disabled. Phone login and the store-review account create users with `auth.sign_up`
    (`app/services/auth_service.py`). **Disabling sign-ups breaks new-customer login** until those calls move to the
    admin API.
  - Email login does not need auto-confirm, because verifying the OTP confirms the address. Phone login signs up a
    placeholder address (`<msisdn>_user@esim.com`) that nobody can confirm. Without auto-confirm, GoTrue sends a
    confirmation email to that address for every new phone user: it bounces and counts against the Auth email
    rate limit. This has not been tested with auto-confirm off.
- [ ] **Storage:** no public bucket and no storage policy for anon (T10). The one exception is the admin API's
  `media` bucket (`ADMIN_MEDIA_BUCKET`: banners and tag icons), which the apps read through public URLs. It is
  accepted by the SOC (#14) for public marketing assets only; record that on every run with
  `--accept "T10a:media=public marketing assets only, SOC #14"`. Any other public bucket fails.
- [ ] **Log drain** to the SOC's collector (Project Settings → Log Drains). The script cannot read it from the
  database; pass the drain's name with `--log-drain` (T11b).

## 3. Run the security check

From a checkout of this repository (it only reads; it prints key and object names, never values):

```sh
SECURITY_CHECK_DATABASE_URL='postgresql://postgres.<ref>:<password>@<pooler-host>:5432/postgres' \
SUPABASE_URL='https://<ref>.supabase.co' SUPABASE_ANON_KEY='<anon key>' \
venv/bin/python scripts/security_check.py --log-drain '<drain name>' \
    --accept "T10a:media=public marketing assets only, SOC #14" [--accept 'ID[:OBJECT]=<reason, SOC ref>' ...]
```

Set the variables in the shell (or from the secret store), not in a file that is committed.

- [ ] Exit code **0**. `1` means at least one FAIL. `3` means a check could not run: the Auth settings or the log
  drain were not given. `2` means the script could not run at all.
- [ ] Every `ACCEPTED` row has a SOC reference in its reason.
- [ ] The `A.1 exposure:` line shows `total=0`. This line is **our approximation of the SOC's Appendix A.1 query**,
  not the SOC's query itself (that query is not in this repository). It has the same eight counts as the A.1
  report of the TEOS-323 hardening script (`teos323_harden.sql`). If the SOC's own measurement differs, the SOC's
  figure is the one that counts; report the difference so this line can be aligned.
- [ ] Output saved and attached to the client's go-live ticket.

| ID | Checks | Blocking |
|---|---|---|
| B1 | RLS enabled and forced on every table in the API schemas | FAIL |
| B2 | no policy for anon/PUBLIC; no `USING (true)` / `WITH CHECK (true)` for authenticated | FAIL |
| B3 | anon has no privilege on tables, views or sequences | FAIL |
| B4 | authenticated cannot INSERT/UPDATE/DELETE/TRUNCATE (TRUNCATE ignores RLS) | FAIL |
| B5 | anon cannot execute SECURITY INVOKER functions | FAIL |
| B6 / B6s | default privileges of postgres / supabase_admin grant nothing to anon, authenticated, PUBLIC | FAIL / WARN |
| T4 | SECURITY DEFINER functions not executable by anon or authenticated | FAIL |
| T5a / T5b | DEFINER functions pinned to `search_path = ''` / INVOKER functions with any fixed search_path | FAIL / WARN |
| T6 | no extension in `public` (or another API schema) | FAIL |
| T8a / T8b / T8c | anonymous sign-ins / self sign-up / email auto-confirm | FAIL / WARN / WARN |
| T10a / T10b | public storage buckets / storage policies for anon or authenticated writes | FAIL |
| T11a / T11b | pgAudit installed, preloaded, configured, parameters not logged / log drain | FAIL / UNKNOWN until given |
| T12 | PostgreSQL major ≥ 17 (`--min-pg-major`) | FAIL |

## 4. People and accounts (baseline control 10)

- [ ] Supabase organisation: every member is a named person (no shared or team mailbox logins), and MFA is
  enforced for the organisation or confirmed per member.
- [ ] Vercel team: the same.
- [ ] Render workspace: the same.
- [ ] Service keys live only in Render / Vercel environment settings, not in a repository or a browser bundle.

## 5. SOC sign-off

- [ ] Script output (section 3) and this checklist sent to the SOC.
- [ ] SOC sign-off recorded below: who, when, and the reference. Accepted risks are listed with their SOC reference.

## Record (one row per go-live or re-check)

| Client / project | Date (UTC) | Operator | Script exit | A.1 total | Accepted (ID:object) | Accounts (4) checked by | SOC sign-off (who, date, ref) |
|---|---|---|---|---|---|---|---|
| | | | | | | | |
