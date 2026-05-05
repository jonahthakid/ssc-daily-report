# SSC Daily Sales Report

Pulls yesterday's metrics from Shopify and emails an HTML report. Runs daily on GitHub Actions cron (free).

## What it sends

- Headline number callout: gross sales, orders, AOV
- Headline metrics table: orders, gross sales, AOV, discounts, new vs. returning — with day-over-day and same-day-last-week comparisons
- Top 10 products by revenue with units sold and remaining inventory
- Top 5 discount codes used

## Setup (about 12 minutes)

### 1. Create a Shopify app in the Dev Dashboard

The legacy "Develop apps" flow in the Shopify admin is closed to new apps as of Jan 1, 2026. Use the Dev Dashboard instead.

1. Go to https://shopify.dev/dashboard → **Apps → Create app → Start from Dev Dashboard** → name it `SSC Daily Report`.
2. **Versions** tab → create a version:
   - App URL: `https://example.com` (this app has no UI; the field is unused)
   - Uncheck **Embed app in Shopify admin**
   - Webhooks API version: latest
   - Access scopes: `read_orders`, `read_products`, `read_customers`, `read_inventory`, `read_discounts`
   - Click **Release**
3. **Home** tab → **Install app** → select your store → Install.
4. **Settings** tab → copy the **Client ID** and **Client secret**.

The script exchanges these for a 24h access token at runtime via the client credentials grant.

### 2. Generate a Gmail app password

Requires 2FA enabled on your Google account.

Go to https://myaccount.google.com/apppasswords → create one labeled "SSC Daily Report." You'll get a 16-character password.

### 3. Push to a new GitHub repo

```bash
cd shopify-daily-report
echo ".env" > .gitignore
git init
git add .
git commit -m "Initial commit"
gh repo create ssc-daily-report --private --source=. --push
```

Or if you don't have `gh` CLI: create a private repo on github.com manually, then `git remote add origin <url> && git push -u origin main`.

### 4. Add secrets to GitHub

Repo → **Settings → Secrets and variables → Actions → New repository secret**. Add five:

- `SHOPIFY_SHOP_DOMAIN` (e.g. `sugarloafsocialclub.myshopify.com`)
- `SHOPIFY_CLIENT_ID`
- `SHOPIFY_CLIENT_SECRET`
- `GMAIL_USER`
- `GMAIL_APP_PASSWORD`
- `REPORT_RECIPIENT`

### 5. Test locally first

```bash
cp .env.example .env
# Fill in real values
pip install -r requirements.txt
set -a; source .env; set +a
python report.py
```

You should get an email within ~10 seconds.

### 6. Trigger from GitHub

Actions tab → "SSC Daily Report" → **Run workflow** to test the production path. After that it runs automatically at 12:00 UTC daily.

## Cost

Free across the board — GitHub Actions, Shopify API, Gmail SMTP.

## Modifying

- **Different metrics?** Edit `summarize_orders()` and `render_email_html()` in `report.py`.
- **Multiple recipients?** Change `REPORT_RECIPIENT` to comma-separated, then split in `send_email()`.
- **Slack instead?** Replace `send_email()` with a webhook POST.
- **Different time?** Edit the cron in `.github/workflows/daily-report.yml`. Note: GitHub Actions doesn't auto-adjust for DST.
- **Skip weekends?** Add a date check at the top of `main()`.
- **Want the AI narrative back later?** Add `anthropic>=0.40.0` to `requirements.txt`, set an `ANTHROPIC_API_KEY` env var, and add a `generate_narrative()` call in `main()`.
