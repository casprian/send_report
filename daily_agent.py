import datetime
import os
import pandas as pd
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from twilio.rest import Client

# -------------------------------------------------------------
# 1. READ GOOGLE SHEET DATA
# -------------------------------------------------------------
SHEET_ID = "10hwoo6rh8i0w-MwcDeCd_paH2lmeCs09PwI9nLSq0NY"
# Direct public CSV export URL (ensure sheet viewing permissions are set)
SHEET_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv"


def fetch_sheet_data():
    # Read the data into a pandas DataFrame
    df = pd.read_csv(SHEET_URL)
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
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    twilio_whatsapp_number = os.environ.get(
        "TWILIO_WHATSAPP_NUMBER", "whatsapp:+14155238886"
    )

    client = Client(account_sid, auth_token)

    # Note: Publicly accessible URL required for Twilio WhatsApp attachments
    pdf_media_url = "https://your-public-storage.com/daily_sales_report.pdf"

    message = client.messages.create(
        body=f"📊 *Daily Sales Report - {datetime.date.today()}*\nAttached is your daily report automatically generated from Google Sheets.",
        from_=twilio_whatsapp_number,
        to=f"whatsapp:{recipient_number}",
        media_url=[pdf_media_url],
    )

    print(f"Message sent successfully! SID: {message.sid}")


# -------------------------------------------------------------
# MAIN AGENT EXECUTION
# -------------------------------------------------------------
if __name__ == "__main__":
    print("Reading data from Google Sheet...")
    df = fetch_sheet_data()

    print("Generating PDF report...")
    pdf_file = generate_pdf_report(df)

    print("Sending WhatsApp message...")
    # Replace with the target recipient phone number with country code
    RECIPIENT_PHONE = "+966XXXXXXXXX"
    send_whatsapp_pdf(pdf_file, RECIPIENT_PHONE)