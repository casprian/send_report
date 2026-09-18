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

    # Title
    c.setFont("Helvetica-Bold", 16)
    c.drawString(
        50, height - 50, f"Daily Sales & Visit Summary - {datetime.date.today()}"
    )

    # Key Metrics
    total_entries = len(df)
    orders_received = len(
        df[
            df["Order Received"].astype(str).str.lower().isin(["yes", "quoted"])
        ]
    )

    c.setFont("Helvetica", 12)
    c.drawString(50, height - 80, f"Total Visits/Entries Logged: {total_entries}")
    c.drawString(
        50, height - 100, f"Quoted/Orders In Progress: {orders_received}"
    )

    # Draw Data Table Highlights
    c.setFont("Helvetica-Bold", 10)
    c.drawString(50, height - 140, "Recent Logged Visits:")

    c.setFont("Helvetica", 9)
    y = height - 160

    # Print top rows as summary
    for idx, row in df.tail(15).iterrows():
        salesman = str(row.get("Salesman Name", "N/A"))[:15]
        customer = str(row.get("Customer/Company Name", "N/A"))[:20]
        status = str(row.get("Current Status", "N/A"))[:15]

        line = f"• {salesman} | {customer} | Status: {status}"
        c.drawString(50, y, line)
        y -= 18
        if y < 50:
            break

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