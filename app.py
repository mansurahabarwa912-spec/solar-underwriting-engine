from flask import Flask, request, jsonify
from openai import OpenAI
import os
import json
import requests
import tempfile
import re
import traceback

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
# CONFIG
# ============================================================

app = Flask(__name__)

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
ATTOM_API_KEY = os.environ.get("ATTOM_API_KEY")
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
PVWATTS_API_KEY = os.environ.get("PVWATTS_API_KEY", "DEMO_KEY")

GHL_API_TOKEN = os.environ.get("GHL_API_TOKEN")
GHL_LOCATION_ID = os.environ.get("GHL_LOCATION_ID")

# Change these to your actual GHL custom field IDs.
GHL_PDF_URL_FIELD = os.environ.get("GHL_PDF_URL_FIELD")
GHL_REPORT_URL_FIELD = os.environ.get("GHL_REPORT_URL_FIELD")
GHL_STATUS_FIELD = os.environ.get("GHL_STATUS_FIELD")

OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4.1")

client = OpenAI(api_key=OPENAI_API_KEY)

cloudinary.config(
    cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME"),
    api_key=os.environ.get("CLOUDINARY_API_KEY"),
    api_secret=os.environ.get("CLOUDINARY_API_SECRET")
)


# ============================================================
# BASIC HELPERS
# ============================================================

def clean_number(value):
    if value is None:
        return None

    text = str(value).strip()

    if not text:
        return None

    text = (
        text.replace(",", "")
        .replace("$", "")
        .replace("%", "")
    )

    match = re.search(r"-?\d+(?:\.\d+)?", text)

    if not match:
        return None

    try:
        return float(match.group(0))
    except ValueError:
        return None


def parse_ai_json(text):
    if not text:
        return {}

    text = text.strip()

    text = re.sub(
        r"^```(?:json)?",
        "",
        text,
        flags=re.IGNORECASE
    )

    text = re.sub(r"```$", "", text).strip()

    start = text.find("{")
    end = text.rfind("}")

    if start >= 0 and end >= 0:
        text = text[start:end + 1]

    try:
        return json.loads(text)
    except Exception:
        return {}


def log(message, data=None):
    print("\n" + "=" * 70)
    print(message)

    if data is not None:
        try:
            print(json.dumps(data, indent=2, default=str)[:12000])
        except Exception:
            print(str(data))

    print("=" * 70)


# ============================================================
# GHL BILL URL EXTRACTION
# ============================================================

def get_bill_url(bill_data):

    """
    Handles common GHL file-field formats.
    """

    if not bill_data:
        return None

    if isinstance(bill_data, str):

        # Sometimes a field contains JSON as a string.
        try:
            parsed = json.loads(bill_data)
            if parsed != bill_data:
                return get_bill_url(parsed)
        except Exception:
            pass

        if bill_data.startswith("http"):
            return bill_data

        return None

    if isinstance(bill_data, list):

        for item in bill_data:
            url = get_bill_url(item)

            if url:
                return url

        return None

    if isinstance(bill_data, dict):

        possible_keys = [
            "url",
            "fileUrl",
            "file_url",
            "downloadUrl",
            "download_url",
            "link",
            "href",
            "value"
        ]

        for key in possible_keys:

            value = bill_data.get(key)

            if isinstance(value, str) and value.startswith("http"):
                return value

        # Sometimes nested under "files"
        for key in ["files", "file", "attachments", "data"]:

            if key in bill_data:

                url = get_bill_url(bill_data[key])

                if url:
                    return url

    return None


def recursively_find_file_url(obj):

    """
    Last-resort recursive search through the entire GHL webhook payload.
    """

    if isinstance(obj, str):

        if obj.startswith("http") and (
            ".pdf" in obj.lower()
            or "file" in obj.lower()
            or "upload" in obj.lower()
            or "cloudinary" in obj.lower()
        ):
            return obj

        return None

    if isinstance(obj, list):

        for item in obj:

            result = recursively_find_file_url(item)

            if result:
                return result

    if isinstance(obj, dict):

        # Prioritize obvious bill/file fields first.
        for key, value in obj.items():

            key_lower = str(key).lower()

            if any(word in key_lower for word in [
                "bill",
                "utility",
                "upload",
                "file",
                "attachment"
            ]):

                result = get_bill_url(value)

                if result:
                    return result

        # Then search everything.
        for value in obj.values():

            result = recursively_find_file_url(value)

            if result:
                return result

    return None


def extract_contact_id(payload):

    possible_paths = [
        payload.get("contact_id"),
        payload.get("contactId"),
        payload.get("id")
    ]

    contact = payload.get("contact")

    if isinstance(contact, dict):
        possible_paths.extend([
            contact.get("id"),
            contact.get("contactId")
        ])

    for value in possible_paths:

        if value:
            return value

    return None


# ============================================================
# DOWNLOAD BILL
# ============================================================

def download_file(url):

    log("DOWNLOADING UTILITY BILL", {
        "url": url
    })

    headers = {
        "User-Agent": "MHoldings-Commercial-Solar-Underwriting/1.0"
    }

    response = requests.get(
        url,
        headers=headers,
        timeout=60,
        allow_redirects=True
    )

    log("BILL DOWNLOAD RESPONSE", {
        "status_code": response.status_code,
        "content_type": response.headers.get("Content-Type"),
        "bytes": len(response.content),
        "final_url": response.url
    })

    response.raise_for_status()

    if not response.content:
        raise Exception("Downloaded utility bill is empty.")

    # Basic PDF validation.
    if not response.content.startswith(b"%PDF"):
        content_type = response.headers.get("Content-Type", "")

        if "pdf" not in content_type.lower():
            raise Exception(
                "The GHL file URL did not return a PDF."
            )

    temp = tempfile.NamedTemporaryFile(
        delete=False,
        suffix=".pdf"
    )

    temp.write(response.content)
    temp.close()

    return temp.name


# ============================================================
# OPENAI PDF EXTRACTION
# ============================================================

def extract_utility_bill(pdf_path):

    log("UPLOADING BILL TO OPENAI")

    with open(pdf_path, "rb") as file:

        uploaded_file = client.files.create(
            file=file,
            purpose="user_data"
        )

    log("OPENAI FILE CREATED", {
        "file_id": uploaded_file.id,
        "filename": uploaded_file.filename
    })

    prompt = """
You are extracting data from a commercial electricity utility bill.

Read the PDF carefully.

Return ONLY valid JSON.

Do not guess.
If a value cannot be found, return null.

Use this exact structure:

{
  "utility_provider": null,
  "account_number": null,
  "service_address": null,
  "billing_start_date": null,
  "billing_end_date": null,
  "annual_kwh": null,
  "billing_period_kwh": null,
  "peak_kw": null,
  "electric_rate_per_kwh": null,
  "total_bill_amount": null,
  "demand_charge": null,
  "energy_charge": null,
  "customer_name": null,
  "city": null,
  "state": null,
  "zip": null
}

Important:
- annual_kwh should only be populated if the bill explicitly provides annual usage.
- billing_period_kwh should be the actual kWh for the billing period.
- peak_kw should be the actual peak demand if present.
- electric_rate_per_kwh should be the effective or stated electricity rate if available.
- service_address should be the property's service address.
"""

    response = client.responses.create(
        model=OPENAI_MODEL,
        input=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": prompt
                    },
                    {
                        "type": "input_file",
                        "file_id": uploaded_file.id
                    }
                ]
            }
        ]
    )

    raw_text = response.output_text

    log("OPENAI BILL EXTRACTION", {
        "raw_response": raw_text
    })

    data = parse_ai_json(raw_text)

    if not data:
        raise Exception(
            "OpenAI returned no usable JSON from the utility bill."
        )

    return data


# ============================================================
# GOOGLE GEOCODING
# ============================================================

def geocode_address(address):

    if not GOOGLE_API_KEY or not address:
        return {
            "latitude": None,
            "longitude": None,
            "formatted_address": address
        }

    url = "https://maps.googleapis.com/maps/api/geocode/json"

    params = {
        "address": address,
        "key": GOOGLE_API_KEY
    }

    response = requests.get(
        url,
        params=params,
        timeout=15
    )

    response.raise_for_status()

    data = response.json()

    if data.get("status") != "OK":
        return {
            "latitude": None,
            "longitude": None,
            "formatted_address": address
        }

    result = data["results"][0]

    location = result["geometry"]["location"]

    return {
        "latitude": location["lat"],
        "longitude": location["lng"],
        "formatted_address": result.get(
            "formatted_address",
            address
        )
    }


# ============================================================
# PVWATTS
# ============================================================

def run_pvwatts(latitude, longitude, system_size_kw):

    if latitude is None or longitude is None:
        return {
            "annual_solar_kwh": None,
            "error": "Missing coordinates"
        }

    if system_size_kw is None or system_size_kw <= 0:
        return {
            "annual_solar_kwh": None,
            "error": "Missing system size"
        }

    url = "https://developer.nrel.gov/api/pvwatts/v8.json"

    params = {
        "api_key": PVWATTS_API_KEY,
        "azimuth": 180,
        "dataset": "nsrdb",
        "gcr": 0.4,
        "inv_eff": 96,
        "radius": 0,
        "dc_ac_ratio": 1.2,
        "tilt": 20,
        "array_type": 1,
        "module_type": 0,
        "system_capacity": system_size_kw,
        "losses": 14,
        "lat": latitude,
        "lon": longitude
    }

    response = requests.get(
        url,
        params=params,
        timeout=30
    )

    response.raise_for_status()

    data = response.json()

    station = data.get("outputs", {})

    ac_annual = station.get("ac_annual")

    if isinstance(ac_annual, list):

        annual = sum(ac_annual)

    else:

        annual = clean_number(ac_annual)

    return {
        "annual_solar_kwh": annual,
        "raw": data
    }


# ============================================================
# ATTOM PROPERTY DATA
# ============================================================

def fetch_attom_property_data(address):

    if not ATTOM_API_KEY:

        return {
            "status": "NOT_CONFIGURED",
            "owner": None,
            "mortgage_amount": None
        }

    parts = [
        part.strip()
        for part in str(address).split(",")
        if part.strip()
    ]

    address1 = parts[0] if parts else address

    address2 = ", ".join(parts[1:]) if len(parts) > 1 else ""

    url = (
        "https://api.gateway.attomdata.com/"
        "propertyapi/v1.0.0/property/detail"
    )

    headers = {
        "Accept": "application/json",
        "apikey": ATTOM_API_KEY
    }

    params = {
        "address1": address1,
        "address2": address2
    }

    try:

        response = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=20
        )

        log("ATTOM RESPONSE", {
            "status_code": response.status_code
        })

        if response.status_code != 200:

            return {
                "status": "ERROR",
                "owner": None,
                "mortgage_amount": None,
                "message": response.text[:500]
            }

        data = response.json()

        properties = data.get("property", [])

        if not properties:

            return {
                "status": "NO_MATCH",
                "owner": None,
                "mortgage_amount": None
            }

        property_data = properties[0]

        assessment = property_data.get(
            "assessment",
            {}
        )

        owner = None

        owner_data = assessment.get("owner")

        if isinstance(owner_data, dict):

            owner = (
                owner_data.get("ownerName1")
                or owner_data.get("ownerName2")
            )

        mortgage = property_data.get(
            "mortgage",
            {}
        )

        mortgage_amount = mortgage.get(
            "totalFirstMortgageAmount"
        )

        return {
            "status": "MATCHED",
            "owner": owner,
            "mortgage_amount": mortgage_amount,
            "attom_id": (
                property_data
                .get("identifier", {})
                .get("attomId")
            )
        }

    except Exception as e:

        log("ATTOM ERROR", str(e))

        return {
            "status": "ERROR",
            "owner": None,
            "mortgage_amount": None
        }


# ============================================================
# DSCR
# ============================================================

def calculate_dscr(financials_text):

    if not financials_text:
        return {
            "noi": None,
            "debt_service": None,
            "dscr": None
        }

    prompt = f"""
Analyze the following commercial financial information.

Extract:

1. Net Operating Income or EBITDA
2. Annual Debt Service

Return ONLY JSON:

{{
    "net_operating_income": null,
    "annual_debt_service": null
}}

Do not guess.

Financial information:

{financials_text}
"""

    try:

        response = client.responses.create(
            model=OPENAI_MODEL,
            input=prompt
        )

        extracted = parse_ai_json(
            response.output_text
        )

        noi = clean_number(
            extracted.get("net_operating_income")
        )

        debt_service = clean_number(
            extracted.get("annual_debt_service")
        )

        if (
            noi is not None
            and debt_service is not None
            and debt_service > 0
        ):

            dscr = noi / debt_service

        else:

            dscr = None

        return {
            "noi": noi,
            "debt_service": debt_service,
            "dscr": dscr
        }

    except Exception as e:

        log("DSCR ERROR", str(e))

        return {
            "noi": None,
            "debt_service": None,
            "dscr": None
        }


# ============================================================
# PRELIMINARY BANKABILITY SCREEN
# ============================================================

def calculate_bankability(
    property_data,
    dscr,
    bill_data,
    review_flag
):

    """
    This is a preliminary screening gate.
    It is NOT a lender approval or credit decision.
    """

    issues = []

    if property_data.get("status") != "MATCHED":

        issues.append(
            "Property ownership could not be independently verified."
        )

    if dscr is None:

        issues.append(
            "DSCR unavailable because financial statements were not provided."
        )

    elif dscr < 1.0:

        issues.append(
            "Calculated DSCR is below 1.00x."
        )

    if not bill_data.get("service_address"):

        issues.append(
            "Service address could not be confidently extracted."
        )

    if review_flag != "STANDARD":

        issues.append(
            "Project requires additional underwriting review."
        )

    if issues:

        status = "REVIEW REQUIRED"

    else:

        status = "PRELIMINARY SCREEN PASSED"

    return {
        "status": status,
        "issues": issues
    }


# ============================================================
# SOLAR FINANCIAL MODEL
# ============================================================

def calculate_project_financials(
    bill_data,
    solar_kwh
):

    billing_kwh = clean_number(
        bill_data.get("billing_period_kwh")
    )

    rate = clean_number(
        bill_data.get("electric_rate_per_kwh")
    )

    if rate is None:
        rate = 0.12

    if solar_kwh is None:

        return {
            "system_size_kw": None,
            "project_cost": None,
            "year_1_savings": None,
            "simple_payback": None,
            "tax_credit": None,
            "net_project_cost": None
        }

    # Preliminary sizing assumption.
    #
    # If annual usage is known, use it.
    # Otherwise annualize the billing period.

    annual_usage = clean_number(
        bill_data.get("annual_kwh")
    )

    if annual_usage is None and billing_kwh is not None:

        annual_usage = billing_kwh * 12

    if annual_usage:

        target_offset = min(
            annual_usage,
            solar_kwh
        )

        # Approximate system size based on modeled production.
        production_per_kw = (
            solar_kwh / 1.0
            if solar_kwh > 0
            else None
        )

        system_size_kw = (
            annual_usage / production_per_kw
            if production_per_kw
            else None
        )

    else:

        system_size_kw = None

    # More conservative fallback.
    if system_size_kw is None:

        peak_kw = clean_number(
            bill_data.get("peak_kw")
        )

        if peak_kw:
            system_size_kw = peak_kw * 2

    project_cost = (
        system_size_kw * 1.50 * 1000
        if system_size_kw
        else None
    )

    year_1_savings = (
        min(
            annual_usage if annual_usage else solar_kwh,
            solar_kwh
        ) * rate
        if solar_kwh
        else None
    )

    tax_credit = (
        project_cost * 0.30
        if project_cost
        else None
    )

    net_project_cost = (
        project_cost - tax_credit
        if project_cost is not None
        else None
    )

    simple_payback = (
        project_cost / year_1_savings
        if project_cost and year_1_savings
        else None
    )

    return {
        "system_size_kw": system_size_kw,
        "project_cost": project_cost,
        "year_1_savings": year_1_savings,
        "simple_payback": simple_payback,
        "tax_credit": tax_credit,
        "net_project_cost": net_project_cost
    }


# ============================================================
# PDF REPORT
# ============================================================

def create_underwriting_pdf(
    output_path,
    bill,
    geo,
    solar,
    financials,
    property_data,
    dscr_data,
    bankability
):

    styles = getSampleStyleSheet()

    document = SimpleDocTemplate(
        output_path,
        pagesize=letter,
        rightMargin=40,
        leftMargin=40,
        topMargin=40,
        bottomMargin=40
    )

    story = []

    story.append(
        Paragraph(
            "PRELIMINARY COMMERCIAL SOLAR UNDERWRITING REPORT",
            styles["Title"]
        )
    )

    story.append(Spacer(1, 10))

    story.append(
        Paragraph(
            "Screening report only — subject to engineering, "
            "utility, legal, tax, and formal credit review.",
            styles["Normal"]
        )
    )

    story.append(Spacer(1, 18))

    # ========================================================
    # BANKABILITY
    # ========================================================

    story.append(
        Paragraph(
            "<b>PRELIMINARY BANKABILITY & CREDIT SCREEN</b>",
            styles["Heading2"]
        )
    )

    dscr_display = (
        f"{dscr_data['dscr']:.2f}x"
        if dscr_data.get("dscr") is not None
        else "Insufficient Data"
    )

    bankability_table = Table([
        [
            "Screening Status",
            bankability["status"]
        ],
        [
            "Property Owner",
            property_data.get("owner")
            or "Not verified"
        ],
        [
            "DSCR",
            dscr_display
        ],
        [
            "ATTOM Property Match",
            property_data.get("status", "Unknown")
        ]
    ], colWidths=[3.2 * inch, 3.3 * inch])

    bankability_table.setStyle(
        TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey)
        ])
    )

    story.append(bankability_table)

    story.append(Spacer(1, 15))

    if bankability["issues"]:

        story.append(
            Paragraph(
                "<b>Items Requiring Review</b>",
                styles["Heading3"]
            )
        )

        for issue in bankability["issues"]:

            story.append(
                Paragraph(
                    "• " + issue,
                    styles["Normal"]
                )
            )

        story.append(Spacer(1, 15))

    # ========================================================
    # PROPERTY
    # ========================================================

    story.append(
        Paragraph(
            "<b>PROPERTY & UTILITY BASELINE</b>",
            styles["Heading2"]
        )
    )

    property_table = Table([
        [
            "Service Address",
            bill.get("service_address") or "N/A"
        ],
        [
            "Utility Provider",
            bill.get("utility_provider") or "N/A"
        ],
        [
            "Billing Period",
            (
                f"{bill.get('billing_start_date') or 'N/A'} "
                f"to "
                f"{bill.get('billing_end_date') or 'N/A'}"
            )
        ],
        [
            "Billing Period Usage",
            (
                f"{clean_number(bill.get('billing_period_kwh')):,.0f} kWh"
                if clean_number(
                    bill.get("billing_period_kwh")
                ) is not None
                else "N/A"
            )
        ]
    ], colWidths=[2.2 * inch, 4.3 * inch])

    property_table.setStyle(
        TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
            ("VALIGN", (0, 0), (-1, -1), "TOP")
        ])
    )

    story.append(property_table)

    story.append(Spacer(1, 18))

    # ========================================================
    # SOLAR
    # ========================================================

    story.append(
        Paragraph(
            "<b>SOLAR PRODUCTION MODEL</b>",
            styles["Heading2"]
        )
    )

    solar_table = Table([
        [
            "Latitude",
            str(geo.get("latitude") or "N/A")
        ],
        [
            "Longitude",
            str(geo.get("longitude") or "N/A")
        ],
        [
            "Estimated Annual Solar Production",
            (
                f"{solar.get('annual_solar_kwh'):,.0f} kWh"
                if solar.get("annual_solar_kwh") is not None
                else "N/A"
            )
        ]
    ], colWidths=[3.2 * inch, 3.3 * inch])

    solar_table.setStyle(
        TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold")
        ])
    )

    story.append(solar_table)

    story.append(Spacer(1, 18))

    # ========================================================
    # FINANCIAL
    # ========================================================

    story.append(
        Paragraph(
            "<b>PROJECT ECONOMICS</b>",
            styles["Heading2"]
        )
    )

    def money(value):
        return (
            f"${value:,.2f}"
            if value is not None
            else "N/A"
        )

    financial_table = Table([
        [
            "Preliminary System Size",
            (
                f"{financials['system_size_kw']:,.2f} kW"
                if financials.get("system_size_kw")
                else "N/A"
            )
        ],
        [
            "Estimated Project Cost",
            money(financials.get("project_cost"))
        ],
        [
            "Estimated Year 1 Savings",
            money(financials.get("year_1_savings"))
        ],
        [
            "Estimated Tax Credit",
            money(financials.get("tax_credit"))
        ],
        [
            "Estimated Net Project Cost",
            money(financials.get("net_project_cost"))
        ],
        [
            "Simple Payback",
            (
                f"{financials['simple_payback']:.1f} years"
                if financials.get("simple_payback")
                else "N/A"
            )
        ]
    ], colWidths=[3.2 * inch, 3.3 * inch])

    financial_table.setStyle(
        TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold")
        ])
    )

    story.append(financial_table)

    story.append(Spacer(1, 20))

    story.append(
        Paragraph(
            "<b>IMPORTANT:</b> This report is a preliminary "
            "screening analysis. It is not an engineering design, "
            "formal appraisal, tax opinion, credit approval, or "
            "financing commitment.",
            styles["Normal"]
        )
    )

    document.build(story)


# ============================================================
# CLOUDINARY
# ============================================================

def upload_report_to_cloudinary(pdf_path):

    result = cloudinary.uploader.upload(
        pdf_path,
        resource_type="raw",
        folder="mholdings/underwriting"
    )

    return result.get("secure_url")


# ============================================================
# GHL UPDATE
# ============================================================

def update_ghl_contact(
    contact_id,
    report_url=None,
    status=None
):

    if not GHL_API_TOKEN or not contact_id:
        return {
            "status": "SKIPPED",
            "reason": "GHL credentials/contact ID missing"
        }

    custom_fields = []

    if GHL_REPORT_URL_FIELD and report_url:

        custom_fields.append({
            "id": GHL_REPORT_URL_FIELD,
            "field_value": report_url
        })

    if GHL_STATUS_FIELD and status:

        custom_fields.append({
            "id": GHL_STATUS_FIELD,
            "field_value": status
        })

    if not custom_fields:
        return {
            "status": "SKIPPED",
            "reason": "No GHL custom fields configured"
        }

    url = (
        "https://services.leadconnectorhq.com/"
        f"contacts/{contact_id}"
    )

    headers = {
        "Authorization": f"Bearer {GHL_API_TOKEN}",
        "Version": "2021-07-28",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    payload = {
        "customFields": custom_fields
    }

    response = requests.put(
        url,
        headers=headers,
        json=payload,
        timeout=20
    )

    log("GHL UPDATE", {
        "status_code": response.status_code,
        "response": response.text[:1000]
    })

    return {
        "status_code": response.status_code,
        "response": response.text[:1000]
    }


# ============================================================
# HEALTH CHECK
# ============================================================

@app.route("/", methods=["GET"])
def home():

    return jsonify({
        "service": "MHoldings Commercial Solar Underwriting",
        "status": "online",
        "webhook": "/webhook"
    })


# ============================================================
# DEBUG ENDPOINT
# ============================================================

@app.route("/debug", methods=["POST"])
def debug():

    payload = request.get_json(
        silent=True
    )

    log("DEBUG GHL PAYLOAD", payload)

    return jsonify({
        "received": True,
        "keys": list(payload.keys())
        if isinstance(payload, dict)
        else None
    })


# ============================================================
# MAIN GHL WEBHOOK
# ============================================================

@app.route("/webhook", methods=["POST"])
def webhook():

    try:

        payload = request.get_json(
            silent=True
        )

        if payload is None:

            log(
                "GHL SENT NON-JSON REQUEST",
                {
                    "content_type": request.content_type,
                    "raw_body": request.data[:5000].decode(
                        "utf-8",
                        errors="replace"
                    )
                }
            )

            return jsonify({
                "success": False,
                "error": "Request was not valid JSON."
            }), 400

        # ====================================================
        # CRITICAL DEBUGGING
        # ====================================================

        log(
            "========== GHL WEBHOOK RECEIVED ==========",
            payload
        )

        # ====================================================
        # CONTACT
        # ====================================================

        contact_id = extract_contact_id(payload)

        log(
            "CONTACT ID",
            {
                "contact_id": contact_id
            }
        )

        # ====================================================
        # BILL URL
        # ====================================================

        bill_url = None

        # First try likely fields.
        likely_fields = [
            "utility_bill",
            "utilityBill",
            "bill",
            "bill_url",
            "billUrl",
            "file",
            "files",
            "attachment",
            "attachments"
        ]

        for field in likely_fields:

            if field in payload:

                bill_url = get_bill_url(
                    payload[field]
                )

                if bill_url:
                    break

        # Search nested payload if necessary.
        if not bill_url:

            bill_url = recursively_find_file_url(
                payload
            )

        log(
            "UTILITY BILL URL FOUND",
            {
                "bill_url": bill_url
            }
        )

        if not bill_url:

            log(
                "!!! BILL URL NOT FOUND !!!",
                payload
            )

            return jsonify({
                "success": False,
                "error": (
                    "GHL webhook reached Render, "
                    "but no utility-bill URL was found."
                )
            }), 400

        # ====================================================
        # DOWNLOAD BILL
        # ====================================================

        pdf_path = download_file(
            bill_url
        )

        # ====================================================
        # OPENAI EXTRACTION
        # ====================================================

        bill_data = extract_utility_bill(
            pdf_path
        )

        log(
            "EXTRACTED BILL DATA",
            bill_data
        )

        # ====================================================
        # ADDRESS
        # ====================================================

        property_address = bill_data.get(
            "service_address"
        )

        if not property_address:

            raise Exception(
                "No service address could be extracted."
            )

        geo = geocode_address(
            property_address
        )

        log(
            "GEOCODE RESULT",
            geo
        )

        # ====================================================
        # PRELIMINARY SYSTEM SIZE
        # ====================================================

        peak_kw = clean_number(
            bill_data.get("peak_kw")
        )

        if peak_kw:

            system_size_kw = peak_kw * 2

        else:

            system_size_kw = 500

        # ====================================================
        # PVWATTS
        # ====================================================

        solar = run_pvwatts(
            geo.get("latitude"),
            geo.get("longitude"),
            system_size_kw
        )

        log(
            "PVWATTS RESULT",
            solar
        )

        # ====================================================
        # FINANCIAL MODEL
        # ====================================================

        financials = calculate_project_financials(
            bill_data,
            solar.get("annual_solar_kwh")
        )

        # ====================================================
        # PROPERTY
        # ====================================================

        property_data = fetch_attom_property_data(
            property_address
        )

        # ====================================================
        # DSCR
        #
        # This remains unavailable unless you provide
        # financial-statement information.
        # ====================================================

        financial_text = payload.get(
            "financial_statements"
        )

        dscr_data = calculate_dscr(
            financial_text
        )

        # ====================================================
        # REVIEW FLAG
        # ====================================================

        review_flag = "STANDARD"

        if (
            not bill_data.get("billing_period_kwh")
            or not property_data.get("owner")
        ):

            review_flag = "REVIEW"

        # ====================================================
        # BANKABILITY
        # ====================================================

        bankability = calculate_bankability(
            property_data,
            dscr_data.get("dscr"),
            bill_data,
            review_flag
        )

        # ====================================================
        # CREATE REPORT
        # ====================================================

        report_file = tempfile.NamedTemporaryFile(
            delete=False,
            suffix=".pdf"
        )

        report_file.close()

        create_underwriting_pdf(
            report_file.name,
            bill_data,
            geo,
            solar,
            financials,
            property_data,
            dscr_data,
            bankability
        )

        # ====================================================
        # CLOUDINARY
        # ====================================================

        report_url = upload_report_to_cloudinary(
            report_file.name
        )

        log(
            "FINAL REPORT",
            {
                "report_url": report_url
            }
        )

        # ====================================================
        # UPDATE GHL
        # ====================================================

        ghl_update = update_ghl_contact(
            contact_id,
            report_url,
            bankability["status"]
        )

        # ====================================================
        # CLEANUP
        # ====================================================

        try:
            os.remove(pdf_path)
        except Exception:
            pass

        try:
            os.remove(report_file.name)
        except Exception:
            pass

        # ====================================================
        # SUCCESS
        # ====================================================

        return jsonify({
            "success": True,
            "message": "Commercial solar underwriting completed.",
            "contact_id": contact_id,
            "bill_received": True,
            "bill_url": bill_url,
            "bill_data": bill_data,
            "geocoding": geo,
            "solar": {
                "annual_solar_kwh":
                    solar.get("annual_solar_kwh")
            },
            "financials": financials,
            "property": property_data,
            "dscr": dscr_data,
            "bankability": bankability,
            "report_url": report_url,
            "ghl_update": ghl_update
        })

    except Exception as e:

        log(
            "!!!!!!!! WEBHOOK ERROR !!!!!!!!",
            {
                "error": str(e),
                "traceback": traceback.format_exc()
            }
        )

        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


# ============================================================
# LOCAL / RENDER START
# ============================================================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            5000
        )
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
