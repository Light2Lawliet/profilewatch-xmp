# ProfileWatch — XMP stack rebuild

The original [ProfileWatch](https://github.com/Light2Lawliet/profilewatch) rebuilt against the
XMP discipline's required stack: **React + TS · Tailwind + shadcn · Postgres (via Supabase) ·
Row-level security + auth · API integration · Graph read**.

Same product idea — an SRE-style reputation monitor with a six-signal health score, incident
detection, churn risk, and AI-drafted remediation — but reshaped around one idea the original
didn't have: **a subscriber only ever sees their own profile.** Row-level security enforces that
in Postgres itself, not in frontend code, which is also exactly the mechanism a
"show-my-own-profile-when-I-try-to-cancel" retention flow needs.

## Architecture

```
web/   React + TypeScript + Tailwind + shadcn-style components, deployed as a static site
api/   FastAPI + Groq (unchanged AI logic from the original build)
       reads/writes Supabase with the service_role key — every account, no RLS
supabase/xmp_schema.sql   relational schema + RLS policies (run once, in the SQL Editor)
```

The frontend talks to Supabase **directly** for identity and data access (a signed-in
subscriber's own account, joined to their reviews and listings in one query — RLS means there's
no way to ask for anyone else's), and to the FastAPI service for the AI-drafted content
(explain, save play, success plan) exactly as the original app did.

## Data model

Real relational tables (`xmp_accounts`, `xmp_reviews`, `xmp_listings`,
`xmp_weekly_health_history`, `xmp_account_owners`) with foreign keys, instead of the original's
single `accounts.data jsonb` blob — a genuine entity graph instead of one flat document. Every
table has RLS enabled with policies that check `xmp_account_owners` for the signed-in user's
`auth.uid()`, so a subscriber's own Supabase queries can only ever return their own account's
rows. The FastAPI service uses the `service_role` key, which bypasses RLS (it needs every account
to compute scores and run the weekly digest), and is itself locked down by the existing access
key + rate limit.

## Setup

1. **Supabase**: same project as the original app is fine (its own `accounts` table is
   untouched). In the SQL Editor, run `supabase/xmp_schema.sql`.
2. **api/**: `pip install -r requirements.txt`, copy `.env.example` to `.env` and fill in
   `GROQ_API_KEY`, `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`. Seed the relational tables and create
   the demo subscriber login:
   ```
   python3 api/scripts/migrate_to_relational.py --demo-account acc_001
   ```
   This prints the demo login (`demo@profilewatch.dev` / a generated password) — that account is
   the only one linked via `xmp_account_owners`, so signing in as it proves the RLS restriction
   actually holds.
3. **web/**: `cd web && npm install`, copy `.env.example` to `.env` and fill in
   `VITE_SUPABASE_URL`, `VITE_SUPABASE_ANON_KEY` (the `anon`/publishable key — never the
   `service_role` one), and `VITE_API_BASE` (the deployed FastAPI URL). `npm run dev`.
4. Run the API: `python3 main.py` from the repo root (the root `main.py`/`requirements.txt` are a
   re-export so `uvicorn main:app` works from the repo root with no custom Root Directory
   setting — see `api/main.py`'s own comment for why).

## What's genuinely new here vs. the original

- Relational schema + RLS instead of a single jsonb blob per account.
- A real login (Supabase Auth), scoping the whole app to one subscriber's own data.
- React + TypeScript + Tailwind + shadcn-style components instead of plain HTML/CSS/JS.

## What's unchanged

- The scoring engine, incident detection, and churn-risk logic (`api/main.py`) — untouched.
- The four Groq-powered endpoints (explain, save play, success plan, weekly digest) — untouched.
- The access-key + rate-limit guards on those endpoints — untouched.
