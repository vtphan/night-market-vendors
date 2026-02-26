# Environment Variable Setup for Deployment

This guide walks through every variable in the `.env` file and how to obtain the correct values for a production deployment.

---

## App Settings

### `SECRET_KEY`

A random string used as the cryptographic key for:

- **Session cookies** — signs the session cookie so it can't be tampered with
- **CSRF tokens** — generates tokens that protect forms against cross-site request forgery
- **OTP hashing** — used as the HMAC key when hashing one-time passcodes

In production this variable is **required** — the app will refuse to start without it.

Generate a strong random key:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

Copy the output and set it:

```
SECRET_KEY=your-generated-key-here
```

> **Warning:** If you change this value after deployment, all existing sessions and pending OTP codes will be invalidated. Vendors and admins will need to log in again.

### `APP_URL`

The public URL where the app is accessible. Used to build links in emails (e.g., payment links sent to approved vendors).

```
APP_URL=https://yourdomain.com
```

No trailing slash.

### `DATABASE_URL`

SQLAlchemy connection string. For local development this defaults to SQLite. For production with Supabase PostgreSQL:

```
DATABASE_URL=postgresql://user:password@host:5432/dbname
```

If you are running SQLite on the VPS:

```
DATABASE_URL=sqlite:///data/app.db
```

### `ADMIN_EMAILS`

Comma-separated list of email addresses that have admin access. These are checked against the `admin_users` table on startup.

```
ADMIN_EMAILS=admin1@example.com,admin2@example.com
```

### `DEBUG`

Set to `false` in production. When `true`, the app uses a fallback insecure secret key and may expose debug information.

```
DEBUG=false
```

---

## Stripe

You need a Stripe account at [https://dashboard.stripe.com](https://dashboard.stripe.com).

### `STRIPE_PUBLISHABLE_KEY`

Found in Stripe Dashboard → Developers → API keys. Starts with `pk_live_` (production) or `pk_test_` (test mode).

```
STRIPE_PUBLISHABLE_KEY=pk_live_...
```

This key is safe to expose in the browser — it's used by Stripe Elements to collect card details.

### `STRIPE_SECRET_KEY`

Found in the same place. Starts with `sk_live_` or `sk_test_`. **Never expose this publicly.**

```
STRIPE_SECRET_KEY=sk_live_...
```

Used server-side to create PaymentIntents and process refunds.

### `STRIPE_WEBHOOK_SECRET`

Obtained when you create a webhook endpoint in the Stripe Dashboard:

1. Go to Stripe Dashboard → Developers → Webhooks
2. Click **Add destination**
3. Set the endpoint URL to `https://yourdomain.com/api/webhooks/stripe`
4. Select these events:
   - `payment_intent.succeeded`
   - `charge.refunded`
5. Save — Stripe will display a signing secret starting with `whsec_`

```
STRIPE_WEBHOOK_SECRET=whsec_...
```

This secret is used to verify that incoming webhooks are actually from Stripe.

> **Note:** The local development webhook secret (from `stripe listen`) is different from the production one. Each environment needs its own value.

---

## Resend (Email)

You need a Resend account at [https://resend.com](https://resend.com).

### `RESEND_API_KEY`

Found in Resend Dashboard → API Keys. Create a new key with sending permission.

```
RESEND_API_KEY=re_...
```

### `EMAIL_FROM`

The sender address for all outgoing emails. The domain must be verified in Resend (Resend Dashboard → Domains).

```
EMAIL_FROM=Asian Night Market <noreply@yourdomain.com>
```

---

## Google OAuth (Optional)

Google OAuth provides a "Sign in with Google" option alongside the default OTP (one-time passcode) login. **This is optional** — OTP works without it.

### Setup

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a project (or select an existing one)
3. Navigate to APIs & Services → Credentials
4. Click **Create Credentials → OAuth client ID**
5. Select **Web application**
6. Add authorized redirect URI: `https://yourdomain.com/auth/google/callback`
7. Copy the Client ID and Client Secret

### `GOOGLE_CLIENT_ID`

```
GOOGLE_CLIENT_ID=123456789-abc.apps.googleusercontent.com
```

### `GOOGLE_CLIENT_SECRET`

```
GOOGLE_CLIENT_SECRET=GOCSPX-...
```

If both values are set, the Google sign-in option appears on the login page automatically. If either is blank, the app falls back to OTP-only authentication.

---

## Example Production `.env`

```
# Stripe
STRIPE_PUBLISHABLE_KEY=pk_live_...
STRIPE_SECRET_KEY=sk_live_...
STRIPE_WEBHOOK_SECRET=whsec_...

# Resend
RESEND_API_KEY=re_...
EMAIL_FROM=Asian Night Market <noreply@yourdomain.com>

# App
SECRET_KEY=your-generated-key-here
APP_URL=https://yourdomain.com
DATABASE_URL=postgresql://user:password@host:5432/dbname
ADMIN_EMAILS=admin1@example.com,admin2@example.com
DEBUG=false

# Google OAuth (optional)
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
```
