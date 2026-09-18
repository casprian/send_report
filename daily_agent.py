import datetime
from io import StringIO
import mimetypes
import os
import re
import ssl
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen

import boto3
import certifi
import pandas as pd
from botocore.config import Config
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

# -------------------------------------------------------------
# 1. READ GOOGLE SHEET DATA
# -------------------------------------------------------------
SHEET_ID = "10hwoo6rh8i0w-MwcDeCd_paH2lmeCs09PwI9nLSq0NY"
# Direct public CSV export URL (ensure sheet viewing permissions are set)
SHEET_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv"


def require_env(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def normalize_whatsapp_address(value):
    normalized = value.strip()
    if not normalized:
        raise RuntimeError("WhatsApp number cannot be empty")
    if normalized.startswith("whatsapp:"):
        return normalized
    return f"whatsapp:{normalized}"


def normalize_media_url(value):
    if not value:
        return None

    candidate = value.strip()
    if not candidate:
        return None

    parsed = urlparse(candidate)
    if "drive.google.com" not in parsed.netloc:
        return candidate

    # Keep already-compatible direct download links unchanged.
    if parsed.path == "/uc":
        query = parse_qs(parsed.query)
        file_id = query.get("id", [None])[0]
        if file_id:
            return f"https://drive.google.com/uc?export=download&id={file_id}"
        return candidate

    file_id = None
    match = re.search(r"/file/d/([^/]+)", parsed.path)
    if match:
        file_id = match.group(1)
    else:
        query = parse_qs(parsed.query)
        file_id = query.get("id", [None])[0]

    if file_id:
        return f"https://drive.google.com/uc?export=download&id={file_id}"

    return candidate


def is_url_reachable(url):
    try:
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        with urlopen(url, timeout=20, context=ssl_context) as response:
            status_code = getattr(response, "status", 200)
            return 200 <= status_code < 400
    except Exception:
        return False


def build_s3_public_url(bucket, region, key):
    if region == "us-east-1":
        return f"https://{bucket}.s3.amazonaws.com/{key}"
    return f"https://{bucket}.s3.{region}.amazonaws.com/{key}"


def upload_pdf_to_s3(pdf_path):
    bucket = os.environ.get("S3_BUCKET_NAME")
    if not bucket:
        return None

    region = os.environ.get("S3_REGION", "us-east-1")
    timestamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d_%H%M%S")
    key = f"reports/{datetime.date.today().isoformat()}/daily_sales_report_{timestamp}.pdf"

    content_type = mimetypes.guess_type(pdf_path)[0] or "application/pdf"

    s3_client = boto3.client(
        "s3",
        region_name=region,
        endpoint_url=f"https://s3.{region}.amazonaws.com",
        config=Config(signature_version="s3v4"),
    )
    with open(pdf_path, "rb") as pdf_file:
        s3_client.put_object(
            Bucket=bucket,
            Key=key,
            Body=pdf_file,
            ContentType=content_type,
        )

    use_presigned = os.environ.get("S3_USE_PRESIGNED_URL", "true").lower() == "true"
    if use_presigned:
        ttl_seconds = int(os.environ.get("S3_URL_TTL_SECONDS", "604800"))
        return s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=ttl_seconds,
        )

    public_base_url = os.environ.get("S3_PUBLIC_BASE_URL")
    if public_base_url:
        return f"{public_base_url.rstrip('/')}/{key}"

    return build_s3_public_url(bucket, region, key)


def fetch_sheet_data():
    # Use certifi CA bundle to avoid local SSL trust-store issues.
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    with urlopen(SHEET_URL, context=ssl_context, timeout=30) as response:
        csv_text = response.read().decode("utf-8")

    df = pd.read_csv(StringIO(csv_text))
    return df


# -------------------------------------------------------------
# 2. GENERATE PDF REPORT
# -------------------------------------------------------------
def generate_pdf_report(df, filename="daily_sales_report.pdf"):
    c = canvas.Canvas(filename, pagesize=letter)
    width, height = letter

    def truncate(value, size):
        text = str(value) if value is not None else ""
        text = text.strip()
        if not text:
            return "-"
        return text if len(text) <= size else text[: size - 3] + "..."

    margin = 36
    content_width = width - (2 * margin)

    total_entries = len(df)
    order_series = df.get("Order Received", pd.Series([""] * total_entries))
    orders_received = len(
        df[order_series.astype(str).str.lower().isin(["yes", "quoted", "order"])]
    )
    unique_salesmen = (
        df.get("Salesman Name", pd.Series([], dtype="object"))
        .astype(str)
        .replace("nan", "")
        .str.strip()
    )
    active_salesmen = len(unique_salesmen[unique_salesmen != ""].unique())
    quote_rate = (orders_received / total_entries * 100) if total_entries else 0
    report_title = os.environ.get("REPORT_TITLE", "Daily Sales Performance Report")
    primary_color = os.environ.get("REPORT_PRIMARY_COLOR", "#0F172A")
    accent_color = os.environ.get("REPORT_ACCENT_COLOR", "#2563EB")

    top_salesmen = []
    if total_entries and "Salesman Name" in df.columns:
        normalized_names = (
            df["Salesman Name"].astype(str).replace("nan", "").str.strip()
        )
        top_salesmen = normalized_names[normalized_names != ""].value_counts().head(5)

    header_height = 78
    c.setFillColor(colors.HexColor(primary_color))
    c.rect(0, height - header_height, width, header_height, fill=1, stroke=0)

    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 18)
    c.drawString(margin, height - 34, report_title)
    c.setFont("Helvetica", 10)
    c.drawString(
        margin,
        height - 52,
        f"Generated on {datetime.datetime.now().strftime('%d %b %Y, %I:%M %p')}",
    )
    c.drawRightString(width - margin, height - 52, "Automated Reporting Agent")

    card_top = height - header_height - 18
    card_height = 68
    card_gap = 12
    card_width = (content_width - (3 * card_gap)) / 4
    card_specs = [
        ("Total Entries", str(total_entries), accent_color),
        ("Quoted or Ordered", str(orders_received), "#0D9488"),
        ("Active Salesmen", str(active_salesmen), "#7C3AED"),
        ("Quote Rate", f"{quote_rate:.1f}%", "#EA580C"),
    ]

    for index, (label, value, accent) in enumerate(card_specs):
        x = margin + index * (card_width + card_gap)
        c.setFillColor(colors.HexColor("#F8FAFC"))
        c.roundRect(x, card_top - card_height, card_width, card_height, 8, fill=1, stroke=0)
        c.setFillColor(colors.HexColor(accent))
        c.rect(x, card_top - 5, card_width, 5, fill=1, stroke=0)
        c.setFillColor(colors.HexColor("#475569"))
        c.setFont("Helvetica", 9)
        c.drawString(x + 10, card_top - 24, label)
        c.setFillColor(colors.HexColor("#0F172A"))
        c.setFont("Helvetica-Bold", 16)
        c.drawString(x + 10, card_top - 46, value)

    section_top = card_top - card_height - 22
    c.setFillColor(colors.HexColor(primary_color))
    c.setFont("Helvetica-Bold", 11)
    c.drawString(margin, section_top, "Recent Visit and Sales Activity")

    table_width = 376
    panel_gap = 12
    panel_x = margin + table_width + panel_gap
    panel_width = content_width - table_width - panel_gap

    table_headers = ["Date", "Salesman", "Customer", "Status"]
    col_widths = [76, 88, 130, 66]
    row_height = 20
    header_y = section_top - 18

    c.setFillColor(colors.HexColor("#E2E8F0"))
    c.rect(margin, header_y - 4, table_width, row_height, fill=1, stroke=0)
    c.setFillColor(colors.HexColor(primary_color))
    c.setFont("Helvetica-Bold", 9)

    col_x = margin + 8
    for header, col_width in zip(table_headers, col_widths):
        c.drawString(col_x, header_y + 3, header)
        col_x += col_width

    available_height = header_y - 60
    max_rows = max(1, int(available_height / row_height))
    recent_rows = df.tail(max_rows)

    c.setFont("Helvetica", 8.5)
    y = header_y - row_height
    for idx, row in recent_rows.iterrows():
        if (idx % 2) == 0:
            c.setFillColor(colors.HexColor("#F8FAFC"))
            c.rect(margin, y - 2, content_width, row_height, fill=1, stroke=0)

        date_value = row.get("Date", row.get("Timestamp", "-"))
        row_values = [
            truncate(date_value, 16),
            truncate(row.get("Salesman Name", "-"), 18),
            truncate(row.get("Customer/Company Name", "-"), 26),
            truncate(row.get("Current Status", "-"), 15),
        ]

        c.setFillColor(colors.HexColor("#1E293B"))
        x = margin + 8
        for value, col_width in zip(row_values, col_widths):
            c.drawString(x, y + 3, value)
            x += col_width

        y -= row_height
        if y < 54:
            break

    c.setStrokeColor(colors.HexColor("#CBD5E1"))
    c.rect(margin, y + 20, table_width, (header_y - y - 2), fill=0, stroke=1)

    panel_y = section_top - 6
    panel_height = max(130, header_y - y + 6)
    c.setFillColor(colors.HexColor("#F8FAFC"))
    c.roundRect(panel_x, panel_y - panel_height, panel_width, panel_height, 8, fill=1, stroke=0)
    c.setFillColor(colors.HexColor(accent_color))
    c.rect(panel_x, panel_y - 5, panel_width, 5, fill=1, stroke=0)

    c.setFillColor(colors.HexColor(primary_color))
    c.setFont("Helvetica-Bold", 10)
    c.drawString(panel_x + 10, panel_y - 20, "Top 5 Salesmen")
    c.setFont("Helvetica", 8)
    c.setFillColor(colors.HexColor("#64748B"))
    c.drawString(panel_x + 10, panel_y - 34, "Based on entry count for this report")

    c.setFont("Helvetica", 9)
    line_y = panel_y - 54
    if len(top_salesmen) == 0:
        c.setFillColor(colors.HexColor("#64748B"))
        c.drawString(panel_x + 10, line_y, "No salesman data available")
    else:
        rank = 1
        for name, count in top_salesmen.items():
            c.setFillColor(colors.HexColor(primary_color))
            c.drawString(panel_x + 10, line_y, f"{rank}. {truncate(name, 18)}")
            c.drawRightString(panel_x + panel_width - 10, line_y, f"{count} entries")
            line_y -= 18
            rank += 1
            if line_y < panel_y - panel_height + 16:
                break

    c.setFillColor(colors.HexColor("#64748B"))
    c.setFont("Helvetica", 8)
    c.drawString(margin, 28, "Prepared by Daily Agent | Source: Google Sheets")
    c.drawRightString(width - margin, 28, f"Report Date: {datetime.date.today().isoformat()}")

    c.save()
    return filename


# -------------------------------------------------------------
# 3. SEND VIA WHATSAPP (TWILIO)
# -------------------------------------------------------------
def send_whatsapp_pdf(pdf_path, recipient_number):
    # Retrieve credentials from environment variables
    account_sid = require_env("TWILIO_ACCOUNT_SID")
    auth_token = require_env("TWILIO_AUTH_TOKEN")
    twilio_whatsapp_number = os.environ.get(
        "TWILIO_WHATSAPP_NUMBER", "whatsapp:+14155238886"
    )
    twilio_whatsapp_number = normalize_whatsapp_address(twilio_whatsapp_number)
    recipient_whatsapp_number = normalize_whatsapp_address(recipient_number)

    client = Client(account_sid, auth_token)

    # Prefer S3 upload URL for this run; fallback to configured static URL.
    pdf_media_url = None
    try:
        pdf_media_url = upload_pdf_to_s3(pdf_path)
    except Exception as exc:
        print(f"S3 upload failed, falling back to configured URL: {exc}")

    if not pdf_media_url:
        pdf_media_url = normalize_media_url(os.environ.get("REPORT_PDF_MEDIA_URL"))

    message_body = (
        f"📊 *Daily Sales Report - {datetime.date.today()}*\n"
        "Automatically generated from Google Sheets."
    )

    message_kwargs = {
        "body": message_body,
        "from_": twilio_whatsapp_number,
        "to": recipient_whatsapp_number,
    }

    if pdf_media_url:
        message_kwargs["body"] = (
            message_body
            + "\n\nReport PDF link: "
            + pdf_media_url
        )
    else:
        message_kwargs["body"] = (
            message_body
            + "\n\nNo valid public PDF URL configured, so this is a text-only update."
            + f"\nLocal report file: {pdf_path}"
        )

    try:
        message = client.messages.create(**message_kwargs)
    except TwilioRestException as exc:
        if exc.code == 63007:
            raise RuntimeError(
                "TWILIO_WHATSAPP_NUMBER is not a configured Twilio WhatsApp sender. "
                "Use the Twilio Sandbox sender whatsapp:+14155238886 or your approved "
                "WhatsApp Business sender, and make sure the recipient joined your sandbox."
            ) from exc
        raise

    print(f"Message sent successfully! SID: {message.sid}")


# -------------------------------------------------------------
# MAIN AGENT EXECUTION
# -------------------------------------------------------------
if __name__ == "__main__":
    recipient_phone = require_env("RECIPIENT_PHONE")

    print("Reading data from Google Sheet...")
    df = fetch_sheet_data()
    print(f"Rows fetched: {len(df)}")

    print("Generating PDF report...")
    pdf_file = generate_pdf_report(df)

    print("Sending WhatsApp message...")
    send_whatsapp_pdf(pdf_file, recipient_phone)