"""
SSC Daily Sales Report
Pulls yesterday's metrics from Shopify and emails an HTML report.

Run via GitHub Actions cron, Railway scheduled job, or any other scheduler.
"""

import os
import smtplib
from collections import defaultdict
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

import requests

# ============================================================================
# Configuration (loaded from environment variables)
# ============================================================================

SHOPIFY_SHOP_DOMAIN = os.environ["SHOPIFY_SHOP_DOMAIN"]  # e.g. "sugarloafsocialclub.myshopify.com"
SHOPIFY_CLIENT_ID = os.environ["SHOPIFY_CLIENT_ID"]
SHOPIFY_CLIENT_SECRET = os.environ["SHOPIFY_CLIENT_SECRET"]
SHOPIFY_API_VERSION = os.environ.get("SHOPIFY_API_VERSION", "2025-01")

GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
REPORT_RECIPIENT = os.environ["REPORT_RECIPIENT"]

REPORT_TIMEZONE = os.environ.get("REPORT_TIMEZONE", "America/New_York")
SHOP_NAME = os.environ.get("SHOP_NAME", "SSC")

GRAPHQL_URL = f"https://{SHOPIFY_SHOP_DOMAIN}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"


# ============================================================================
# Date helpers
# ============================================================================

def get_date_ranges(tz_name: str) -> dict:
    """Compute yesterday + comparison ranges in the shop's local timezone."""
    tz = ZoneInfo(tz_name)
    today_start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)

    yesterday_start = today_start - timedelta(days=1)
    yesterday_end = today_start - timedelta(microseconds=1)

    prior_day_start = yesterday_start - timedelta(days=1)
    prior_day_end = yesterday_start - timedelta(microseconds=1)

    same_dow_lw_start = yesterday_start - timedelta(days=7)
    same_dow_lw_end = same_dow_lw_start.replace(hour=23, minute=59, second=59, microsecond=999000)

    return {
        "yesterday": (yesterday_start, yesterday_end),
        "prior_day": (prior_day_start, prior_day_end),
        "same_dow_last_week": (same_dow_lw_start, same_dow_lw_end),
    }


# ============================================================================
# Shopify GraphQL
# ============================================================================

_access_token = None


def get_access_token() -> str:
    """Exchange client credentials for a short-lived Admin API access token.

    Tokens are valid for ~24h; we fetch a fresh one each run.
    """
    global _access_token
    if _access_token:
        return _access_token

    response = requests.post(
        f"https://{SHOPIFY_SHOP_DOMAIN}/admin/oauth/access_token",
        data={
            "client_id": SHOPIFY_CLIENT_ID,
            "client_secret": SHOPIFY_CLIENT_SECRET,
            "grant_type": "client_credentials",
        },
        timeout=30,
    )
    response.raise_for_status()
    _access_token = response.json()["access_token"]
    return _access_token


def shopify_graphql(query: str, variables: dict = None) -> dict:
    response = requests.post(
        GRAPHQL_URL,
        headers={
            "X-Shopify-Access-Token": get_access_token(),
            "Content-Type": "application/json",
        },
        json={"query": query, "variables": variables or {}},
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    if "errors" in data:
        raise RuntimeError(f"Shopify GraphQL errors: {data['errors']}")
    return data["data"]


ORDERS_QUERY = """
query GetOrders($query: String!, $cursor: String) {
  orders(first: 100, query: $query, after: $cursor, sortKey: CREATED_AT) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id
        name
        createdAt
        displayFinancialStatus
        currentTotalPriceSet { shopMoney { amount } }
        currentSubtotalPriceSet { shopMoney { amount } }
        currentTotalDiscountsSet { shopMoney { amount } }
        totalShippingPriceSet { shopMoney { amount } }
        customer { id numberOfOrders }
        discountCodes
        lineItems(first: 50) {
          edges {
            node {
              title
              quantity
              originalTotalSet { shopMoney { amount } }
              variant {
                id
                sku
                inventoryQuantity
                product { id title }
              }
            }
          }
        }
      }
    }
  }
}
"""

def fetch_orders(start_dt: datetime, end_dt: datetime) -> list:
    query_str = f"created_at:>='{start_dt.isoformat()}' AND created_at:<='{end_dt.isoformat()}'"
    orders = []
    cursor = None
    while True:
        data = shopify_graphql(ORDERS_QUERY, {"query": query_str, "cursor": cursor})
        orders.extend(edge["node"] for edge in data["orders"]["edges"])
        page_info = data["orders"]["pageInfo"]
        if not page_info["hasNextPage"]:
            break
        cursor = page_info["endCursor"]
    return orders


# ============================================================================
# Aggregation
# ============================================================================

def summarize_orders(orders: list) -> dict:
    if not orders:
        return {
            "order_count": 0, "gross_sales": 0.0, "subtotal": 0.0,
            "discounts": 0.0, "shipping": 0.0, "aov": 0.0,
            "new_customers": 0, "returning_customers": 0,
            "top_products": [], "top_discount_codes": [],
        }

    gross = sum(float(o["currentTotalPriceSet"]["shopMoney"]["amount"]) for o in orders)
    subtotal = sum(float(o["currentSubtotalPriceSet"]["shopMoney"]["amount"]) for o in orders)
    discounts = sum(float(o["currentTotalDiscountsSet"]["shopMoney"]["amount"]) for o in orders)
    shipping = sum(float(o["totalShippingPriceSet"]["shopMoney"]["amount"]) for o in orders)

    new_customers = 0
    returning_customers = 0
    for o in orders:
        cust = o.get("customer")
        if not cust:
            continue
        if int(cust.get("numberOfOrders") or 0) <= 1:
            new_customers += 1
        else:
            returning_customers += 1

    product_units = defaultdict(int)
    product_revenue = defaultdict(float)
    product_inventory = {}
    for o in orders:
        for li_edge in o["lineItems"]["edges"]:
            li = li_edge["node"]
            title = li["title"]
            product_units[title] += li["quantity"]
            product_revenue[title] += float(li["originalTotalSet"]["shopMoney"]["amount"])
            if li.get("variant") and li["variant"].get("inventoryQuantity") is not None:
                product_inventory[title] = li["variant"]["inventoryQuantity"]

    top_products = sorted(
        [
            {
                "title": t,
                "units": product_units[t],
                "revenue": product_revenue[t],
                "remaining_inventory": product_inventory.get(t),
            }
            for t in product_units
        ],
        key=lambda x: x["revenue"], reverse=True,
    )[:10]

    code_orders = defaultdict(int)
    code_revenue = defaultdict(float)
    for o in orders:
        for code in o.get("discountCodes", []):
            code_orders[code] += 1
            code_revenue[code] += float(o["currentTotalPriceSet"]["shopMoney"]["amount"])

    top_discount_codes = sorted(
        [{"code": c, "orders": code_orders[c], "revenue": code_revenue[c]} for c in code_orders],
        key=lambda x: x["revenue"], reverse=True,
    )[:5]

    return {
        "order_count": len(orders),
        "gross_sales": gross,
        "subtotal": subtotal,
        "discounts": discounts,
        "shipping": shipping,
        "aov": gross / len(orders) if orders else 0,
        "new_customers": new_customers,
        "returning_customers": returning_customers,
        "top_products": top_products,
        "top_discount_codes": top_discount_codes,
    }


# ============================================================================
# HTML rendering
# ============================================================================

def fmt_money(amount: float) -> str:
    return f"${amount:,.2f}"


def render_email_html(date_str: str, yesterday: dict, prior_day: dict, same_dow_lw: dict, shop_name: str) -> str:
    def delta(curr, prev):
        if not prev:
            return "—"
        pct = (curr - prev) / prev * 100
        sign = "+" if pct >= 0 else ""
        color = "#16a34a" if pct >= 0 else "#dc2626"
        return f'<span style="color:{color}">{sign}{pct:.1f}%</span>'

    products_rows = "".join(
        f"<tr><td style='padding:6px 0'>{p['title']}</td>"
        f"<td style='text-align:right'>{p['units']}</td>"
        f"<td style='text-align:right'>{fmt_money(p['revenue'])}</td>"
        f"<td style='text-align:right'>{p['remaining_inventory'] if p['remaining_inventory'] is not None else '—'}</td></tr>"
        for p in yesterday["top_products"][:10]
    ) or "<tr><td colspan='4' style='text-align:center;color:#6b7280;padding:12px'>No orders yesterday</td></tr>"

    codes_rows = "".join(
        f"<tr><td style='padding:6px 0'>{c['code']}</td>"
        f"<td style='text-align:right'>{c['orders']}</td>"
        f"<td style='text-align:right'>{fmt_money(c['revenue'])}</td></tr>"
        for c in yesterday["top_discount_codes"][:5]
    ) or "<tr><td colspan='3' style='text-align:center;color:#6b7280;padding:12px'>No discount codes used</td></tr>"

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#111;max-width:640px;margin:0 auto;padding:24px;background:#fff">
<h1 style="font-size:20px;margin:0 0 4px 0">{shop_name} Daily Report</h1>
<p style="color:#6b7280;margin:0 0 24px 0">{date_str}</p>

<div style="background:#f9fafb;border-radius:8px;padding:20px;margin-bottom:24px;border:1px solid #e5e7eb;text-align:center">
<div style="color:#6b7280;font-size:12px;text-transform:uppercase;letter-spacing:0.05em;margin-bottom:6px">Gross Sales</div>
<div style="font-size:32px;font-weight:600">{fmt_money(yesterday['gross_sales'])}</div>
<div style="color:#6b7280;font-size:14px;margin-top:6px">{yesterday['order_count']} orders · AOV {fmt_money(yesterday['aov'])}</div>
</div>

<h2 style="font-size:13px;text-transform:uppercase;letter-spacing:0.05em;color:#6b7280;margin:0 0 12px 0">Headline Metrics</h2>
<table style="width:100%;border-collapse:collapse;margin-bottom:24px;font-size:14px">
<thead><tr style="border-bottom:1px solid #e5e7eb">
<th style="text-align:left;padding:8px 0">Metric</th>
<th style="text-align:right;padding:8px 0">Yesterday</th>
<th style="text-align:right;padding:8px 0">vs. Prior Day</th>
<th style="text-align:right;padding:8px 0">vs. Same Day LW</th>
</tr></thead>
<tbody>
<tr style="border-bottom:1px solid #f3f4f6"><td style="padding:8px 0">Orders</td><td style="text-align:right">{yesterday['order_count']}</td><td style="text-align:right">{delta(yesterday['order_count'], prior_day['order_count'])}</td><td style="text-align:right">{delta(yesterday['order_count'], same_dow_lw['order_count'])}</td></tr>
<tr style="border-bottom:1px solid #f3f4f6"><td style="padding:8px 0">Gross Sales</td><td style="text-align:right">{fmt_money(yesterday['gross_sales'])}</td><td style="text-align:right">{delta(yesterday['gross_sales'], prior_day['gross_sales'])}</td><td style="text-align:right">{delta(yesterday['gross_sales'], same_dow_lw['gross_sales'])}</td></tr>
<tr style="border-bottom:1px solid #f3f4f6"><td style="padding:8px 0">AOV</td><td style="text-align:right">{fmt_money(yesterday['aov'])}</td><td style="text-align:right">{delta(yesterday['aov'], prior_day['aov'])}</td><td style="text-align:right">{delta(yesterday['aov'], same_dow_lw['aov'])}</td></tr>
<tr style="border-bottom:1px solid #f3f4f6"><td style="padding:8px 0">Discounts</td><td style="text-align:right">{fmt_money(yesterday['discounts'])}</td><td style="text-align:right">—</td><td style="text-align:right">—</td></tr>
<tr><td style="padding:8px 0">New / Returning</td><td style="text-align:right">{yesterday['new_customers']} / {yesterday['returning_customers']}</td><td style="text-align:right">—</td><td style="text-align:right">—</td></tr>
</tbody></table>

<h2 style="font-size:13px;text-transform:uppercase;letter-spacing:0.05em;color:#6b7280;margin:0 0 12px 0">Top Products</h2>
<table style="width:100%;border-collapse:collapse;margin-bottom:24px;font-size:14px">
<thead><tr style="border-bottom:1px solid #e5e7eb"><th style="text-align:left;padding:8px 0">Product</th><th style="text-align:right;padding:8px 0">Units</th><th style="text-align:right;padding:8px 0">Revenue</th><th style="text-align:right;padding:8px 0">Stock</th></tr></thead>
<tbody>{products_rows}</tbody>
</table>

<h2 style="font-size:13px;text-transform:uppercase;letter-spacing:0.05em;color:#6b7280;margin:0 0 12px 0">Discount Codes Used</h2>
<table style="width:100%;border-collapse:collapse;margin-bottom:24px;font-size:14px">
<thead><tr style="border-bottom:1px solid #e5e7eb"><th style="text-align:left;padding:8px 0">Code</th><th style="text-align:right;padding:8px 0">Orders</th><th style="text-align:right;padding:8px 0">Revenue</th></tr></thead>
<tbody>{codes_rows}</tbody>
</table>

<p style="color:#9ca3af;font-size:12px;margin-top:32px">Generated automatically from Shopify Admin API.</p>
</body></html>"""


# ============================================================================
# Email delivery
# ============================================================================

def send_email(subject: str, html_body: str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = REPORT_RECIPIENT
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.send_message(msg)


# ============================================================================
# Main
# ============================================================================

def main():
    ranges = get_date_ranges(REPORT_TIMEZONE)

    print(f"Fetching orders for {ranges['yesterday'][0].date()}...")
    yesterday_orders = fetch_orders(*ranges["yesterday"])
    prior_day_orders = fetch_orders(*ranges["prior_day"])
    same_dow_lw_orders = fetch_orders(*ranges["same_dow_last_week"])

    yesterday = summarize_orders(yesterday_orders)
    prior_day = summarize_orders(prior_day_orders)
    same_dow_lw = summarize_orders(same_dow_lw_orders)

    print(f"Building email ({yesterday['order_count']} orders, {fmt_money(yesterday['gross_sales'])})...")
    date_str = ranges["yesterday"][0].strftime("%A, %B %-d, %Y")
    html = render_email_html(date_str, yesterday, prior_day, same_dow_lw, SHOP_NAME)

    subject = f"{SHOP_NAME} Daily — {ranges['yesterday'][0].strftime('%b %-d')} — {yesterday['order_count']} orders, {fmt_money(yesterday['gross_sales'])}"

    print(f"Sending email to {REPORT_RECIPIENT}...")
    send_email(subject, html)
    print("Done.")


if __name__ == "__main__":
    main()
