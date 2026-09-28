from flask import Flask, request, jsonify
from openai import OpenAI
import os
import json
import requests
import tempfile
import re

import cloudinary
import cloudinary.uploader

from reportlab.lib.pagesizes import letter
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle
)
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch


# ============================================================
# APP CONFIGURATION
# ============================================================

app = Flask(__name__)

client = OpenAI(
    api_key=os.environ.get("OPENAI_API_KEY")
)

cloudinary.config(
    cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME"),
    api_key=os.environ.get("CLOUDINARY_API_KEY"),
    api_secret=os.environ.get("CLOUDINARY_API_SECRET")
)

# New Underwriting API Keys
ATTOM_API_KEY = os.environ.get("ATTOM_API_KEY")


# ============================================================
# HELPER FUNCTIONS
# ============================================================

def clean_number(value):
    """
    Convert strings such as '$1,234.56', '1,234', '1.25 kW', or '15.7%' into floats.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = text.replace(",", "").replace("$", "").replace("%", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def parse_ai_json(text):
    """
    Safely convert the model response into JSON.
    """
    if not text:
        return {}
    text = text.strip()
    if text.startswith("```"):
        text = text.replace("```json", "").replace("```", "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end >= 0:
        text = text[start:end + 1]
    try:
        return json.loads(text)
    except Exception:
        return {}


def get_bill_url(bill_data):
    """
    Handles the different formats GHL can send for a file field.
    """
    if isinstance(bill_data, str):
        return bill_data
    if isinstance(bill_data, list) and len(bill_data) > 0:
        first_bill = bill_data[0]
        if isinstance(first_bill, str):
            return first_bill
        if isinstance(first_bill, dict):
            return (
                first_bill.get("url") or first_bill.get("fileUrl") or 
                first_bill.get("file_url") or first_bill.get("downloadUrl")
            )
    if isinstance(bill_data, dict):
        return (
            bill_data.get("url") or bill_data.get("fileUrl") or 
            bill_data.get("file_url") or bill_data.get("downloadUrl")
        )
    return None


# ============================================================
# NEW UNDERWRITING INTEGRATION MODULES
# ============================================================

def fetch_attom_property_data(address_string):
    """
    Queries ATTOM Data Solutions API to verify property ownership and title risk profiles.
    Returns ownership name and legal structure tags.
    """
    if not ATTOM_API_KEY:
        return {"error": "ATTOM API Key missing", "owner": "Unknown", "mortgages": "Unknown"}
        
    url = "https://attomdata.com"
    headers = {
        "Accept": "application/json",
        "apikey": ATTOM_API_KEY
    }
    params = {"address": address_string}
    
    try:
        response = requests.get(url, headers=headers, params=params, timeout=10)
        if response.status_code == 200:
            data = response.json()
            property_records = data.get("property", [])
            if property_records:
                assessment = property_records[0].get("assessment", {})
                owner = assessment.get("owner", {}).get("ownerName1", "Unknown Owner")
                mortgage_data = property_records[0].get("mortgage", {})
                return {
                    "owner": owner,
                    "mortgages": mortgage_data.get("totalFirstMortgageAmount", "Unknown")
                }
    except Exception as e:
        print(f"ATTOM Property API Error: {str(e)}")
        
    return {"owner": "Unknown / Manual Verification Required", "mortgages": "Unknown"}


def parse_financials_for_dscr(financials_text):
    """
    Leverages LLM parsing layers to extract operational data from tax filings or balance statements
    and calculates the structural Debt Service Coverage Ratio (DSCR).
    """
    prompt = f"""
    Analyze the following commercial financial text summary. Extract the business's current year Net Operating Income (NOI) or EBITDA, and their Annual Debt Service payments.
    Return your calculation strictly as a JSON object with these key metrics:
    {{
        "net_operating_income": float or null,
        "annual_debt_service": float or null
    }}
    Financial Statement Summary: {financials_text}
    """
    
    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0
        )
        extracted_data = parse_ai_json(response.choices[0].message.content)
        
        noi = clean_number(extracted_data.get("net_operating_income"))
        debt_service = clean_number(extracted_data.get("annual_debt_service"))
        
        if noi and debt_service and debt_service > 0:
            dscr = noi / debt_service
        else:
            dscr = None
            
        return {"noi": noi, "debt_service": debt_service, "dscr": dscr}
    except Exception as e:
        print(f"Financial Parsing Error: {str(e)}")
        return {"noi": None, "debt_service": None, "dscr": None}


# ============================================================
# REFACTORED PDF GENERATOR (WITH BANKABILITY)
# ============================================================

def create_underwriting_pdf(
    output_path, property_address, utility_provider, system_size_kw,
    annual_solar_kwh, project_cost, year_1_savings, simple_payback,
    tax_credit, net_project_cost, depreciation_tax_savings,
    incentive_adjusted_payback, year_1_net_benefit, review_flag,
    legal_owner, dscr_value, bankability_status
):
    styles = getSampleStyleSheet()
    document = SimpleDocTemplate(
        output_path, pagesize=letter,
        rightMargin=40, leftMargin=40, topMargin=40, bottomMargin=40
    )
    story = []

    # Title Banner
    story.append(Paragraph("PRELIMINARY COMMERCIAL SOLAR UNDERWRITING REPORT", styles["Title"]))
    story.append(Spacer(1, 12))
    story.append(Paragraph("For screening only — subject to engineering, utility, legal, and formal credit review.", styles["Normal"]))
    story.append(Spacer(1, 15))

    # ---- NEW SECTION: COMMERCIAL RISK & BANKABILITY GATE ----
    story.append(Paragraph("<b>BANKABILITY & CREDIT ASSESSMENT</b>", styles["Heading2"]))
    
    # Establish dynamic safety status flagging
    if bankability_status == "APPROVED":
        status_color = colors.HexColor("#1b5e20") # Safe Dark Green
    elif bankability_status == "REVIEW REQUIRED":
        status_color = colors.HexColor("#b71c1c") # High-Alert Red
    else:
        status_color = colors.HexColor("#e65100") # Warning Orange

    dscr_display = f"{dscr_value:.2f}x" if dscr_value is not None else "Insufficient Data Provided"
    
    bankability_data = [
        ["Financing Status Gate", Paragraph(f"<b>{bankability_status}</b>", styles["Normal"])],
        ["Verified Property Title Holder", str(legal_owner or "Unknown Entity")],
        ["Calculated Debt Service Coverage Ratio (DSCR)", dscr_display]
    ]
    
    bankability_table = Table(bankability_data, colWidths=[3.5 * inch, 3.0 * inch])
    bankability_table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("BACKGROUND", (1, 0), (1, 0), status_color),
        ("TEXTCOLOR", (1, 0), (1, 0), colors.white),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold")
    ]))
    story.append(bankability_table)
    story.append(Spacer(1, 20))

    # Property & Utility Data Table
    story.append(Paragraph("<b>PROPERTY & UTILITY BASELINE</b>", styles["Heading2"]))
    property_data = [
        ["Property Address", str(property_address or "N/A")],
        ["Utility Provider", str(utility_provider or "N/A")],
        ["Underwriting Review Track", str(review_flag or "Standard")]
    ]
    property_table = Table(property_data, colWidths=[2.0 * inch, 4.5 * inch])
    property_table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold")
    ]))
    story.append(property_table)
    story.append(Spacer(1, 20))

    # Solar Production Metrics
    story.append(Paragraph("<b>SOLAR PRODUCTION SYSTEM MODELING</b>", styles["Heading2"]))
    solar_data = [
        ["Preliminary System Size", f"{system_size_kw:.2f} kW" if system_size_kw is not None else "N/A"],
        ["Estimated Annual Solar Production", f"{annual_solar_kwh:,.0f} kWh" if annual_solar_kwh is not None else "N/A"]
    ]
    solar_table = Table(solar_data, colWidths=[3.5 * inch, 3.0 * inch])
    solar_table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold")
    ]))
    story.append(solar_table)
    story.append(Spacer(1, 20))

    # Financial Returns Breakdown
    story.append(Paragraph("<b>FINANCIAL UNDERWRITING & RETURN MATRIX</b>", styles["Heading2"]))
    financial_data = [
        ["Estimated Gross Project Cost", f"${project_cost:,.2f}" if project_cost is not None else "N/A"],
        ["Estimated Year 1 Savings", f"${year_1_savings:,.2f}" if year_1_savings is not None else "N/A"]
    ]
