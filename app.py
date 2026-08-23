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


# ============================================================
# HELPER FUNCTIONS
# ============================================================

def clean_number(value):
    """
    Convert strings such as:
    '$1,234.56'
    '1,234'
    '1.25 kW'
    '15.7%'
    into floats where possible.
    """

    if value is None:
        return None

    text = str(value).strip()

    if not text:
        return None

    text = text.replace(",", "")
    text = text.replace("$", "")
    text = text.replace("%", "")

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

    # Remove markdown fences if the model returns them.
    if text.startswith("```"):
        text = text.replace("```json", "")
        text = text.replace("```", "")
        text = text.strip()

    # Find the first JSON object if there is extra text.
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
                first_bill.get("url")
                or first_bill.get("fileUrl")
                or first_bill.get("file_url")
                or first_bill.get("downloadUrl")
            )

    if isinstance(bill_data, dict):
        return (
            bill_data.get("url")
            or bill_data.get("fileUrl")
            or bill_data.get("file_url")
            or bill_data.get("downloadUrl")
        )

    return None


# ============================================================
# PDF GENERATOR
# ============================================================

def create_underwriting_pdf(
    output_path,
    property_address,
    utility_provider,
    system_size_kw,
    annual_solar_kwh,
    project_cost,
    year_1_savings,
    simple_payback,
    tax_credit,
    net_project_cost,
    depreciation_tax_savings,
    incentive_adjusted_payback,
    year_1_net_benefit,
    review_flag
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

    story.append(Spacer(1, 12))

    story.append(
        Paragraph(
            "For preliminary screening only — subject to engineering, "
            "utility, legal, and tax review.",
            styles["Normal"]
        )
    )

    story.append(Spacer(1, 20))

    story.append(
        Paragraph(
            "<b>PROPERTY & UTILITY</b>",
            styles["Heading2"]
        )
    )

    property_data = [
        ["Property Address", str(property_address or "N/A")],
        ["Utility Provider", str(utility_provider or "N/A")],
        ["Underwriting Review", str(review_flag or "N/A")]
    ]

    property_table = Table(
        property_data,
        colWidths=[2.0 * inch, 4.5 * inch]
    )

    property_table.setStyle(
        TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold")
        ])
    )

    story.append(property_table)

    story.append(Spacer(1, 20))

    story.append(
        Paragraph(
            "<b>SOLAR SYSTEM</b>",
            styles["Heading2"]
        )
    )

    solar_data = [
        [
            "Preliminary System Size",
            f"{system_size_kw:.2f} kW"
            if system_size_kw is not None
            else "N/A"
        ],
        [
            "Estimated Annual Solar Production",
            f"{annual_solar_kwh:,.0f} kWh"
            if annual_solar_kwh is not None
            else "N/A"
        ]
    ]

    solar_table = Table(
        solar_data,
        colWidths=[3.5 * inch, 3.0 * inch]
    )

    solar_table.setStyle(
        TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold")
        ])
    )

    story.append(solar_table)

    story.append(Spacer(1, 20))

    story.append(
        Paragraph(
            "<b>FINANCIAL UNDERWRITING</b>",
            styles["Heading2"]
        )
    )

    financial_data = [
        [
            "Estimated Project Cost",
            f"${project_cost:,.2f}"
            if project_cost is not None
            else "N/A"
        ],
        [
            "Estimated Year 1 Savings",
            f"${year_1_savings:,.2f}"
            if year_1_savings is not None
            else "N/A"
        ],
        [
            "Simple Payback",
            f"{simple_payback:.2f} years"
            if simple_payback is not None
            else "N/A"
        ],
        [
            "Estimated Tax Credit",
            f"${tax_credit:,.2f}"
            if tax_credit is not None
            else "N/A"
        ],
        [
            "Estimated Net Project Cost",
            f"${net_project_cost:,.2f}"
            if net_project_cost is not None
            else "N/A"
        ],
        [
            "Depreciation Tax Savings",
            f"${depreciation_tax_savings:,.2f}"
            if depreciation_tax_savings is not None
            else "N/A"
        ],
        [
            "Incentive-Adjusted Payback",
            f"{incentive_adjusted_payback:.2f} years"
            if incentive_adjusted_payback is not None
            else "N/A"
        ],
        [
            "Year 1 Net Economic Benefit",
            f"${year_1_net_benefit:,.2f}"
            if year_1_net_benefit is not None
            else "N/A"
        ]
    ]

    financial_table = Table(
        financial_data,
        colWidths=[3.5 * inch, 3.0 * inch]
    )

    financial_table.setStyle(
        TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
            ("VALIGN", (0, 0), (-1, -1), "TOP")
        ])
    )

    story.append(financial_table)

    story.append(Spacer(1, 25))

    story.append(
        Paragraph(
            "<b>IMPORTANT NOTICE</b>",
            styles["Heading2"]
        )
    )

    story.append(
        Paragraph(
            "This report contains preliminary automated estimates. "
            "It is not a final engineering design, tax opinion, utility "
            "interconnection study, investment recommendation, or guarantee "
            "of project economics. Final project decisions should be based "
            "on qualified engineering, tax, legal, utility, and financial review.",
            styles["Normal"]
        )
    )

    document.build(story)


# ============================================================
# HOME
# ============================================================

@app.route("/", methods=["GET"])
def home():

    return jsonify({
        "status": "online"
    })


# ============================================================
# WEBHOOK
# ============================================================

@app.route("/webhook", methods=["POST"])
def webhook():

    data = request.get_json(silent=True) or {}

    print("\n========================")
    print("NEW REQUEST RECEIVED")
    print("========================")

    print(json.dumps(data, indent=4))

    try:

        # ====================================================
        # GET CUSTOM DATA FROM GHL
        # ====================================================

        custom_data = data.get(
            "customData",
            {}
        )

        contact_id = custom_data.get(
            "contact_id"
        )

        bill_data = custom_data.get(
            "utility_bill"
        )

        print("\n========================")
        print("UTILITY BILL DATA")
        print("========================")

        print(bill_data)

        print("\n========================")
        print("CONTACT ID")
        print("========================")

        print(contact_id)

        # ====================================================
        # FIND UTILITY BILL URL
        # ====================================================

        bill_url = get_bill_url(
            bill_data
        )

        if not bill_url:

            print("\n========================")
            print("NO UTILITY BILL URL FOUND")
            print("========================")

            return jsonify({
                "status": "success",
                "message": "No utility bill URL received"
            })

        print("\n========================")
        print("UTILITY BILL URL FOUND")
        print("========================")

        print(bill_url)

        # ====================================================
        # DOWNLOAD UTILITY BILL
        # ====================================================

        response = requests.get(
            bill_url,
            timeout=30
        )

        print("\n========================")
        print("UTILITY BILL DOWNLOAD")
        print("========================")

        print(
            "Status code:",
            response.status_code
        )

        print(
            "File size:",
            len(response.content),
            "bytes"
        )

        print(
            "Content type:",
            response.headers.get("Content-Type")
        )

        if response.status_code != 200:

            return jsonify({
                "status": "error",
                "message": "Could not download utility bill"
            }), 400

        # ====================================================
        # SAVE PDF
        # ====================================================

        with tempfile.NamedTemporaryFile(
            suffix=".pdf",
            delete=False
        ) as temp_file:

            temp_file.write(
                response.content
            )

            pdf_path = temp_file.name

        # ====================================================
        # UPLOAD PDF TO OPENAI
        # ====================================================

        with open(
            pdf_path,
            "rb"
        ) as pdf_file:

            uploaded_file = client.files.create(
                file=pdf_file,
                purpose="user_data"
            )

        print("\n========================")
        print("FILE UPLOADED TO OPENAI")
        print("========================")

        print(
            "File ID:",
            uploaded_file.id
        )

        # ==========================================
        # AI UTILITY BILL EXTRACTION
        # ==========================================

        result = client.responses.create(

            model="gpt-4.1",

            input=[

                {
                    "role": "user",

                    "content": [

                        {
                            "type": "input_file",
                            "file_id": uploaded_file.id
                        },

                        {
                            "type": "input_text",

                            "text": """
        You are extracting commercial electricity bill data
        for preliminary solar underwriting.

        Read the ENTIRE utility bill carefully.

        Do not assume that the information is located on
        the first page. Check tables, usage history,
        charges, demand sections, meter information,
        and rate information.

        Extract the following:

        1. Utility provider
        2. Electricity usage in kWh
        3. Peak/billed demand in kW
        4. Billing period
        5. Service/property address
        6. Electricity energy rate in $/kWh

        IMPORTANT — ELECTRICITY USAGE:

        Look for electricity consumption under labels such as:

        - kWh
        - Usage
        - Electricity Usage
        - Energy Usage
        - Total Usage
        - Energy Consumption
        - Meter Usage
        - Monthly Usage
        - Historical Usage
        - Usage History

        If the bill contains a 12-month usage history,
        calculate the annual electricity usage by adding
        the available monthly kWh values.

        If the bill contains only one billing period's
        electricity usage, extract that monthly usage into
        monthly_kwh_usage.

        Then estimate annual_kwh_usage as:

        monthly_kwh_usage × 12

        ONLY when there is exactly one reliable monthly
        usage value and no 12-month history.

        This is an ESTIMATED annual usage.

        Do NOT return an empty annual_kwh_usage when a
        reliable monthly kWh value is available.

        IMPORTANT — DEMAND:

        Look for demand under labels such as:

        - Demand
        - Peak Demand
        - Maximum Demand
        - Billed Demand
        - Peak kW
        - Demand kW
        - kW Demand
        - Maximum kW
        - Recorded Demand
        - Metered Demand

        Extract the value in kW.

        If the bill contains multiple demand values,
        prefer the billed/maximum demand for the relevant
        billing period.

        If demand genuinely does not appear anywhere on
        the bill, return an empty string.

        DO NOT invent or estimate demand.

        IMPORTANT — ELECTRICITY RATE:

        Find the actual electricity energy/supply rate
        charged for electricity consumption.

        Prefer an explicit $/kWh rate.

        If there is no explicit rate, calculate it ONLY if
        the bill provides enough information:

        energy/supply charges ÷ electricity kWh usage.

        Do NOT include:

        - demand charges
        - taxes
        - fixed customer charges
        - late fees
        - unrelated delivery charges

        If a reliable electricity rate cannot be determined,
        return an empty string.

        IMPORTANT — ADDRESS:

        Extract the actual service/property address shown
        on the bill.

        IMPORTANT — BILLING PERIOD:

        Return the start and end dates if available.

        IMPORTANT:

        Never invent a value.

        Return ONLY valid JSON.

        Use exactly this structure:

        {
            "utility_provider": "",
            "monthly_kwh_usage": "",
            "annual_kwh_usage": "",
            "annual_kwh_source": "",
            "peak_demand_kw": "",
            "billing_period": "",
            "property_address": "",
            "electric_rate_per_kwh": ""
        }

        For annual_kwh_source use one of:

        "12-month-history"

        "monthly_usage_x12"

        ""

        Examples:

        If the bill contains 12 months:

        {
            "monthly_kwh_usage": "",
            "annual_kwh_usage": "145000",
            "annual_kwh_source": "12-month-history"
        }

        If the bill contains one month with 12000 kWh:

        {
            "monthly_kwh_usage": "12000",
            "annual_kwh_usage": "144000",
            "annual_kwh_source": "monthly_usage_x12"
        }

        If demand is not present:

        "peak_demand_kw": ""

        Never use zero to mean missing data.
        """
                        }

                    ]
                }

            ]
        )

        print("\n========================")
        print("AI EXTRACTION")
        print("========================")

        print(result.output_text)

        contact_id = custom_data.get("contact_id")

        print("\n========================")
        print("CONTACT ID")
        print("========================")

        print(contact_id)

        # ==========================================
        # CONVERT AI RESPONSE TO JSON
        # ==========================================

        try:

            ai_text = result.output_text.strip()

            if ai_text.startswith("```"):

                ai_text = ai_text.replace(
                    "```json",
                    ""
                )

                ai_text = ai_text.replace(
                    "```",
                    ""
                )

                ai_text = ai_text.strip()

            extracted_data = json.loads(ai_text)

        except Exception as e:

            print("\n========================")
            print("JSON EXTRACTION ERROR")
            print("========================")

            print("Error:", str(e))
            print("Raw AI response:", result.output_text)

            extracted_data = {}

        print("\n========================")
        print("EXTRACTED DATA")
        print("========================")

        print(
            json.dumps(
                extracted_data,
                indent=4
            )
        )

        # ==========================================
        # NORMALIZE ANNUAL KWH
        # ==========================================

        annual_kwh_raw = extracted_data.get(
            "annual_kwh_usage",
            ""
        )

        monthly_kwh_raw = extracted_data.get(
            "monthly_kwh_usage",
            ""
        )

        annual_kwh = None

        # First preference: AI calculated annual usage
        try:

            if str(annual_kwh_raw).strip():

                annual_kwh = float(
                    str(annual_kwh_raw)
                    .replace(",", "")
                    .replace("kWh", "")
                    .strip()
                )

        except (ValueError, TypeError):

            annual_kwh = None


        # Second preference: monthly usage × 12
        if (
            annual_kwh is None
            and str(monthly_kwh_raw).strip()
        ):

            try:

                monthly_kwh = float(
                    str(monthly_kwh_raw)
                    .replace(",", "")
                    .replace("kWh", "")
                    .strip()
                )

                if monthly_kwh > 0:

                    annual_kwh = monthly_kwh * 12

                    extracted_data[
                        "annual_kwh_usage"
                    ] = str(round(annual_kwh, 2))

                    extracted_data[
                        "annual_kwh_source"
                    ] = "monthly_usage_x12"

            except (ValueError, TypeError):

                annual_kwh = None

        print("\n========================")
        print("NORMALIZED ELECTRICITY USAGE")
        print("========================")

        print(
            "Monthly kWh:",
            monthly_kwh_raw
        )

        print(
            "Annual kWh:",
            annual_kwh
        )

        print(
            "Annual kWh source:",
            extracted_data.get(
                "annual_kwh_source",
                ""
            )
        )

        # ==========================================
        # NORMALIZE PEAK DEMAND
        # ==========================================

        peak_demand_raw = extracted_data.get(
            "peak_demand_kw",
            ""
        )

        peak_demand_kw = None

        try:

            if str(peak_demand_raw).strip():

                peak_demand_kw = float(
                    str(peak_demand_raw)
                    .replace(",", "")
                    .replace("kW", "")
                    .strip()
                )

        except (ValueError, TypeError):

            peak_demand_kw = None

        print("\n========================")
        print("NORMALIZED DEMAND")
        print("========================")

        print(
            "Peak demand:",
            peak_demand_kw,
            "kW"
        )

        # ====================================================
        # PROPERTY ADDRESS
        # ====================================================

        property_address = extracted_data.get(
            "property_address",
            ""
        )

        latitude = ""
        longitude = ""

        if property_address:

            google_api_key = os.environ.get(
                "GOOGLE_MAPS_API_KEY"
            )

            if google_api_key:

                geocode_url = (
                    "https://maps.googleapis.com/maps/api/geocode/json"
                )

                geocode_response = requests.get(

                    geocode_url,

                    params={
                        "address": property_address,
                        "key": google_api_key
                    },

                    timeout=30
                )

                geocode_data = (
                    geocode_response.json()
                )

                print("\n========================")
                print("GOOGLE GEOCODING RESPONSE")
                print("========================")

                print(
                    "Status:",
                    geocode_data.get("status")
                )

                print(
                    "Error:",
                    geocode_data.get(
                        "error_message",
                        "None"
                    )
                )

                if (
                    geocode_data.get("status") == "OK"
                    and geocode_data.get("results")
                ):

                    location = (
                        geocode_data["results"][0]
                        ["geometry"]
                        ["location"]
                    )

                    latitude = location.get(
                        "lat",
                        ""
                    )

                    longitude = location.get(
                        "lng",
                        ""
                    )

        print("\n========================")
        print("PROPERTY COORDINATES")
        print("========================")

        print(
            "Address:",
            property_address
        )

        print(
            "Latitude:",
            latitude
        )

        print(
            "Longitude:",
            longitude
        )

        # ====================================================
        # NREL PVWATTS V8
        # ====================================================

        nrel_api_key = os.environ.get(
            "NREL_API_KEY"
        )

        pvwatts_data = {}

        annual_production_per_kw = None

        print("\n========================")
        print("PVWATTS CHECK")
        print("========================")

        print(
            "Latitude:",
            latitude
        )

        print(
            "Longitude:",
            longitude
        )

        print(
            "NREL API key found:",
            bool(nrel_api_key)
        )

        if (
            latitude
            and longitude
            and nrel_api_key
        ):

            # CORRECT NREL PVWATTS V8 ENDPOINT
            pvwatts_url = (
                "https://developer.nrel.gov/"
                "api/pvwatts/v8.json"
            )

            pvwatts_params = {

                "api_key": nrel_api_key,

                "lat": latitude,

                "lon": longitude,

                # 1 kW baseline
                "system_capacity": 1,

                "azimuth": 180,

                "tilt": 20,

                "array_type": 1,

                "module_type": 1,

                "losses": 14
            }

            try:

                pvwatts_response = requests.get(

                    pvwatts_url,

                    params=pvwatts_params,

                    timeout=30
                )

                print("\n========================")
                print("PVWATTS RESPONSE")
                print("========================")

                print(
                    "Status code:",
                    pvwatts_response.status_code
                )

                pvwatts_data = (
                    pvwatts_response.json()
                )

                print(
                    json.dumps(
                        pvwatts_data,
                        indent=4
                    )
                )

                if pvwatts_response.status_code == 200:

                    annual_production_per_kw = clean_number(
                        pvwatts_data
                        .get("outputs", {})
                        .get("ac_annual")
                    )

            except Exception as e:

                print("\n========================")
                print("PVWATTS ERROR")
                print("========================")

                print(
                    str(e)
                )

        # ====================================================
        # USAGE CALCULATION
        # ====================================================

        annual_kwh = clean_number(
            extracted_data.get(
                "annual_kwh_usage"
            )
        )

        current_period_kwh = clean_number(
            extracted_data.get(
                "current_period_kwh"
            )
        )

        peak_demand_kw = clean_number(
            extracted_data.get(
                "peak_demand_kw"
            )
        )

        usage_source = "bill_annual"

        # If annual usage isn't available,
        # annualize the current billing period.
        if (
            annual_kwh is None
            and current_period_kwh is not None
            and current_period_kwh > 0
        ):

            annual_kwh = (
                current_period_kwh * 12
            )

            usage_source = (
                "annualized_current_billing_period"
            )

        # ====================================================
        # PRELIMINARY SYSTEM SIZING
        # ====================================================

        preliminary_system_size_kw = None

        estimated_annual_solar_kwh = None

        if (
            annual_kwh is not None
            and annual_kwh > 0
            and annual_production_per_kw is not None
            and annual_production_per_kw > 0
        ):

            preliminary_system_size_kw = (
                annual_kwh
                / annual_production_per_kw
            )

            estimated_annual_solar_kwh = (
                preliminary_system_size_kw
                * annual_production_per_kw
            )

        print("\n========================")
        print("PRELIMINARY SYSTEM SIZING")
        print("========================")

        print(
            "Annual usage:",
            annual_kwh,
            "kWh"
        )

        print(
            "Usage source:",
            usage_source
        )

        print(
            "Current period usage:",
            current_period_kwh,
            "kWh"
        )

        print(
            "Peak demand:",
            peak_demand_kw,
            "kW"
        )

        print(
            "PVWatts production per kW:",
            annual_production_per_kw,
            "kWh/year"
        )

        print(
            "Preliminary system size:",
            preliminary_system_size_kw,
            "kW"
        )

        print(
            "Estimated annual solar production:",
            estimated_annual_solar_kwh,
            "kWh/year"
        )

        # ====================================================
        # FINANCIAL VARIABLES
        # ====================================================

        cost_per_watt = clean_number(
            os.environ.get(
                "SOLAR_COST_PER_WATT",
                "1.50"
            )
        )

        if cost_per_watt is None:
            cost_per_watt = 1.50

        estimated_project_cost = None

        estimated_year_1_savings = None

        simple_payback_years = None

        # ====================================================
        # PROJECT COST
        # ====================================================

        if preliminary_system_size_kw is not None:

            estimated_project_cost = (
                preliminary_system_size_kw
                * 1000
                * cost_per_watt
            )

        # ====================================================
        # ELECTRICITY RATE
        # ====================================================

        electricity_rate = clean_number(
            extracted_data.get(
                "electric_rate_per_kwh"
            )
        )

        if (
            estimated_annual_solar_kwh is not None
            and electricity_rate is not None
            and electricity_rate > 0
        ):

            estimated_year_1_savings = (
                estimated_annual_solar_kwh
                * electricity_rate
            )

        # ====================================================
        # SIMPLE PAYBACK
        # ====================================================

        if (
            estimated_project_cost is not None
            and estimated_year_1_savings is not None
            and estimated_year_1_savings > 0
        ):

            simple_payback_years = (
                estimated_project_cost
                / estimated_year_1_savings
            )

        # ====================================================
        # TAX CREDIT
        # ====================================================

        tax_credit_rate_raw = os.environ.get(
            "PRELIMINARY_TAX_CREDIT_RATE",
            ""
        )

        preliminary_tax_credit_rate = None

        estimated_tax_credit = None

        estimated_net_project_cost = None

        if str(
            tax_credit_rate_raw
        ).strip():

            preliminary_tax_credit_rate = (
                clean_number(
                    tax_credit_rate_raw
                )
            )

            if preliminary_tax_credit_rate is not None:

                preliminary_tax_credit_rate /= 100

                if (
                    0 <= preliminary_tax_credit_rate <= 1
                    and estimated_project_cost is not None
                ):

                    estimated_tax_credit = (
                        estimated_project_cost
                        * preliminary_tax_credit_rate
                    )

                    estimated_net_project_cost = (
                        estimated_project_cost
                        - estimated_tax_credit
                    )

        # ====================================================
        # DEPRECIATION
        # ====================================================

        depreciation_rate_raw = os.environ.get(
            "PRELIMINARY_DEPRECIATION_RATE",
            ""
        )

        preliminary_depreciation_rate = None

        estimated_depreciation_benefit = None

        if str(
            depreciation_rate_raw
        ).strip():

            preliminary_depreciation_rate = (
                clean_number(
                    depreciation_rate_raw
                )
            )

            if preliminary_depreciation_rate is not None:

                preliminary_depreciation_rate /= 100

                if (
                    0 <= preliminary_depreciation_rate <= 1
                    and estimated_project_cost is not None
                ):

                    estimated_depreciation_benefit = (
                        estimated_project_cost
                        * preliminary_depreciation_rate
                    )

        # ====================================================
        # CORPORATE TAX RATE
        # ====================================================

        corporate_tax_rate_raw = os.environ.get(
            "PRELIMINARY_CORPORATE_TAX_RATE",
            ""
        )

        preliminary_corporate_tax_rate = None

        estimated_depreciation_tax_savings = None

        if str(
            corporate_tax_rate_raw
        ).strip():

            preliminary_corporate_tax_rate = (
                clean_number(
                    corporate_tax_rate_raw
                )
            )

            if preliminary_corporate_tax_rate is not None:

                preliminary_corporate_tax_rate /= 100

                if (
                    0 <= preliminary_corporate_tax_rate <= 1
                    and estimated_depreciation_benefit is not None
                ):

                    estimated_depreciation_tax_savings = (
                        estimated_depreciation_benefit
                        * preliminary_corporate_tax_rate
                    )

        # ====================================================
        # INCENTIVE-ADJUSTED PAYBACK
        # ====================================================

        incentive_adjusted_payback_years = None

        if (
            estimated_net_project_cost is not None
            and estimated_year_1_savings is not None
            and estimated_year_1_savings > 0
        ):

            incentive_adjusted_payback_years = (
                estimated_net_project_cost
                / estimated_year_1_savings
            )

        # ====================================================
        # YEAR 1 NET ECONOMIC BENEFIT
        # ====================================================

        estimated_year_1_net_economic_benefit = None

        if estimated_year_1_savings is not None:

            estimated_year_1_net_economic_benefit = (
                estimated_year_1_savings
                + (
                    estimated_depreciation_tax_savings
                    if estimated_depreciation_tax_savings
                    is not None
                    else 0
                )
            )

        # ====================================================
        # QUALITY CONTROL
        # ====================================================

        missing_inputs = []

        if annual_kwh is None or annual_kwh <= 0:
            missing_inputs.append(
                "Annual electricity usage"
            )

        if not property_address:
            missing_inputs.append(
                "Property address"
            )

        if (
            annual_production_per_kw is None
            or annual_production_per_kw <= 0
        ):
            missing_inputs.append(
                "PVWatts production"
            )

        if (
            electricity_rate is None
            or electricity_rate <= 0
        ):
            missing_inputs.append(
                "Electricity rate"
            )

        if (
            estimated_project_cost is None
            or estimated_project_cost <= 0
        ):
            missing_inputs.append(
                "Project cost"
            )

        if (
            estimated_year_1_savings is None
            or estimated_year_1_savings <= 0
        ):
            missing_inputs.append(
                "Year 1 savings"
            )

        if missing_inputs:

            underwriting_review_flag = (
                "REVIEW REQUIRED - Missing: "
                + ", ".join(missing_inputs)
            )

        elif (
            preliminary_tax_credit_rate is None
            or estimated_tax_credit is None
            or estimated_net_project_cost is None
        ):

            underwriting_review_flag = (
                "PRELIMINARY - INCENTIVE REVIEW REQUIRED"
            )

        else:

            underwriting_review_flag = (
                "PRELIMINARY - PASS"
            )

        # ====================================================
        # LOG FINANCIAL UNDERWRITING
        # ====================================================

        print("\n========================")
        print("FINANCIAL UNDERWRITING")
        print("========================")

        print(
            "Cost per watt:",
            cost_per_watt
        )

        print(
            "Estimated project cost:",
            estimated_project_cost
        )

        print(
            "Electricity rate:",
            electricity_rate
        )

        print(
            "Estimated Year 1 savings:",
            estimated_year_1_savings
        )

        print(
            "Simple payback:",
            simple_payback_years
        )

        print(
            "Tax credit rate:",
            preliminary_tax_credit_rate
        )

        print(
            "Estimated tax credit:",
            estimated_tax_credit
        )

        print(
            "Net project cost:",
            estimated_net_project_cost
        )

        print(
            "Depreciation benefit:",
            estimated_depreciation_benefit
        )

        print(
            "Depreciation tax savings:",
            estimated_depreciation_tax_savings
        )

        print(
            "Incentive-adjusted payback:",
            incentive_adjusted_payback_years
        )

        print(
            "Year 1 net economic benefit:",
            estimated_year_1_net_economic_benefit
        )

        print(
            "Review flag:",
            underwriting_review_flag
        )

        # ====================================================
        # GENERATE PDF
        # ====================================================

        pdf_path = os.path.join(
            tempfile.gettempdir(),
            f"underwriting_{contact_id or 'unknown'}.pdf"
        )

        pdf_url = None

        try:

            create_underwriting_pdf(

                output_path=pdf_path,

                property_address=property_address,

                utility_provider=extracted_data.get(
                    "utility_provider",
                    ""
                ),

                system_size_kw=
                    preliminary_system_size_kw,

                annual_solar_kwh=
                    estimated_annual_solar_kwh,

                project_cost=
                    estimated_project_cost,

                year_1_savings=
                    estimated_year_1_savings,

                simple_payback=
                    simple_payback_years,

                tax_credit=
                    estimated_tax_credit,

                net_project_cost=
                    estimated_net_project_cost,

                depreciation_tax_savings=
                    estimated_depreciation_tax_savings,

                incentive_adjusted_payback=
                    incentive_adjusted_payback_years,

                year_1_net_benefit=
                    estimated_year_1_net_economic_benefit,

                review_flag=
                    underwriting_review_flag
            )

            print("\n========================")
            print("UNDERWRITING PDF")
            print("========================")

            print(
                "PDF created:",
                pdf_path
            )

            # =================================================
            # CLOUDINARY UPLOAD
            # =================================================

            if (
                os.environ.get("CLOUDINARY_CLOUD_NAME")
                and os.environ.get("CLOUDINARY_API_KEY")
                and os.environ.get("CLOUDINARY_API_SECRET")
            ):

                try:

                    upload_result = (
                        cloudinary.uploader.upload(
                            pdf_path,
                            resource_type="raw",
                            folder="solar_underwriting"
                        )
                    )

                    pdf_url = upload_result.get(
                        "secure_url"
                    )

                    print("\n========================")
                    print("CLOUDINARY PDF UPLOAD")
                    print("========================")

                    print(
                        "PDF URL:",
                        pdf_url
                    )

                except Exception as e:

                    print("\n========================")
                    print("CLOUDINARY UPLOAD ERROR")
                    print("========================")

                    print(
                        str(e)
                    )

        except Exception as e:

            print("\n========================")
            print("PDF GENERATION ERROR")
            print("========================")

            print(
                str(e)
            )

        # ====================================================
        # UPDATE GHL CONTACT
        # ====================================================

        ghl_api_key = os.environ.get(
            "GHL_API_KEY"
        )

        if contact_id and ghl_api_key:

            ghl_url = (
                "https://services.leadconnectorhq.com"
                f"/contacts/{contact_id}"
            )

            ghl_headers = {

                "Authorization":
                    f"Bearer {ghl_api_key}",

                "Version":
                    "2021-07-28",

                "Content-Type":
                    "application/json"
            }

            custom_fields = [

                # --------------------------------------------
                # Utility provider
                # --------------------------------------------

                {
                    "id": "QlceeYQHWz79JpC3RfHG",
                    "fieldValue":
                        extracted_data.get(
                            "utility_provider",
                            ""
                        )
                },

                # --------------------------------------------
                # Annual kWh
                # --------------------------------------------

                {
                    "id": "nxlJKpBjr5vFXpsDt86M",
                    "fieldValue":
                        str(
                            round(
                                annual_kwh,
                                2
                            )
                        )
                        if annual_kwh is not None
                        else ""
                },

                # --------------------------------------------
                # Billing period
                # --------------------------------------------

                {
                    "id": "bJexeasg4bhJN9vZuC6C",
                    "fieldValue":
                        extracted_data.get(
                            "billing_period",
                            ""
                        )
                },

                # --------------------------------------------
                # Peak demand
                # --------------------------------------------

                {
                    "id": "ESOf9cNFnZXFkgTvAL4o",
                    "fieldValue":
                        str(
                            round(
                                peak_demand_kw,
                                2
                            )
                        )
                        if peak_demand_kw is not None
                        else ""
                },

                # --------------------------------------------
                # Property address
                # --------------------------------------------

                {
                    "id": "EoeFaBKcFly95M8DGYzH",
                    "fieldValue":
                        property_address
                },

                # --------------------------------------------
                # System size
                # --------------------------------------------

                {
                    "id": "303wqmJNOMRe7fhZ1OTA",
                    "fieldValue":
                        str(
                            round(
                                preliminary_system_size_kw,
                                2
                            )
                        )
                        if preliminary_system_size_kw
                        is not None
                        else ""
                },

                # --------------------------------------------
                # Annual solar production
                # --------------------------------------------

                {
                    "id": "hsDEvEqotfjoHL1bRoS5",
                    "fieldValue":
                        str(
                            round(
                                estimated_annual_solar_kwh,
                                2
                            )
                        )
                        if estimated_annual_solar_kwh
                        is not None
                        else ""
                },

                # --------------------------------------------
                # Production per kW
                # --------------------------------------------

                {
                    "id": "LuMoa9805spakF6q1qi6",
                    "fieldValue":
                        str(
                            round(
                                annual_production_per_kw,
                                2
                            )
                        )
                        if annual_production_per_kw
                        is not None
                        else ""
                },

                # --------------------------------------------
                # Electricity rate
                # --------------------------------------------

                {
                    "id": "L7WjNOBcBux2B1mkRBlR",
                    "fieldValue":
                        str(
                            electricity_rate
                        )
                        if electricity_rate
                        is not None
                        else ""
                },

                # --------------------------------------------
                # Tax credit rate
                # --------------------------------------------

                {
                    "id": "BG2CefNGA9Dz9myuqymJ",
                    "fieldValue":
                        str(
                            round(
                                preliminary_tax_credit_rate * 100,
                                2
                            )
                        )
                        if preliminary_tax_credit_rate
                        is not None
                        else ""
                },

                # --------------------------------------------
                # Estimated tax credit
                # --------------------------------------------

                {
                    "id": "0e349aKLH3y8jfZ0WlpJ",
                    "fieldValue":
                        str(
                            round(
                                estimated_tax_credit,
                                2
                            )
                        )
                        if estimated_tax_credit
                        is not None
                        else ""
                },

                # --------------------------------------------
                # Net project cost
                # --------------------------------------------

                {
                    "id": "Ymvq1fyoArmNlBZZoqmn",
                    "fieldValue":
                        str(
                            round(
                                estimated_net_project_cost,
                                2
                            )
                        )
                        if estimated_net_project_cost
                        is not None
                        else ""
                },

                # --------------------------------------------
                # Incentive-adjusted payback
                # --------------------------------------------

                {
                    "id": "4hL7mojN2jd5dwgi6Qqy",
                    "fieldValue":
                        str(
                            round(
                                incentive_adjusted_payback_years,
                                2
                            )
                        )
                        if incentive_adjusted_payback_years
                        is not None
                        else ""
                },

                # --------------------------------------------
                # Estimated depreciation benefit
                # --------------------------------------------

                {
                    "id": "5TJQOIFicqCs2aIipsFm",
                    "fieldValue":
                        str(
                            round(
                                estimated_depreciation_benefit,
                                2
                            )
                        )
                        if estimated_depreciation_benefit
                        is not None
                        else ""
                },

                # --------------------------------------------
                # Depreciation tax savings
                # --------------------------------------------

                {
                    "id": "4Twip5swlOpbwTh4Fcfv",
                    "fieldValue":
                        str(
                            round(
                                estimated_depreciation_tax_savings,
                                2
                            )
                        )
                        if estimated_depreciation_tax_savings
                        is not None
                        else ""
                },

                # --------------------------------------------
                # Year 1 net economic benefit
                # --------------------------------------------

                {
                    "id": "XGCAReyZJfuI8eLlhFeU",
                    "fieldValue":
                        str(
                            round(
                                estimated_year_1_net_economic_benefit,
                                2
                            )
                        )
                        if estimated_year_1_net_economic_benefit
                        is not None
                        else ""
                },

                # --------------------------------------------
                # Second incentive payback field
                # --------------------------------------------

                {
                    "id": "NABZeM3IEbGMpVWzkXsd",
                    "fieldValue":
                        str(
                            round(
                                incentive_adjusted_payback_years,
                                2
                            )
                        )
                        if incentive_adjusted_payback_years
                        is not None
                        else ""
                },

                # --------------------------------------------
                # Underwriting review flag
                # --------------------------------------------

                {
                    "id": "2RosGiBTpC9nr9twHtTu",
                    "fieldValue":
                        underwriting_review_flag
                },

                # --------------------------------------------
                # AI Underwriting Summary
                # --------------------------------------------

                {
                    "id": "CecrcV1MWS03t6HpkCVu",
                    "fieldValue": (
                        "PRELIMINARY COMMERCIAL SOLAR UNDERWRITING\n"
                        f"System Size: "
                        f"{round(preliminary_system_size_kw, 2) if preliminary_system_size_kw is not None else 'N/A'} kW\n"
                        f"Annual Usage: "
                        f"{round(annual_kwh, 2) if annual_kwh is not None else 'N/A'} kWh\n"
                        f"Project Cost: "
                        f"${round(estimated_project_cost, 2) if estimated_project_cost is not None else 'N/A'}\n"
                        f"Year 1 Savings: "
                        f"${round(estimated_year_1_savings, 2) if estimated_year_1_savings is not None else 'N/A'}\n"
                        f"Simple Payback: "
                        f"{round(simple_payback_years, 2) if simple_payback_years is not None else 'N/A'} years\n"
                        f"Incentive-Adjusted Payback: "
                        f"{round(incentive_adjusted_payback_years, 2) if incentive_adjusted_payback_years is not None else 'N/A'} years\n"
                        f"Tax Credit: "
                        f"${round(estimated_tax_credit, 2) if estimated_tax_credit is not None else 'N/A'}\n"
                        f"Net Project Cost: "
                        f"${round(estimated_net_project_cost, 2) if estimated_net_project_cost is not None else 'N/A'}\n"
                        f"Depreciation Tax Savings: "
                        f"${round(estimated_depreciation_tax_savings, 2) if estimated_depreciation_tax_savings is not None else 'N/A'}\n"
                        f"Year 1 Net Economic Benefit: "
                        f"${round(estimated_year_1_net_economic_benefit, 2) if estimated_year_1_net_economic_benefit is not None else 'N/A'}\n"
                        f"Review Status: "
                        f"{underwriting_review_flag}\n"
                        "IMPORTANT: Preliminary underwriting only. "
                        "Engineering and tax review required before final "
                        "investment or project decisions."
                    )
                },

                # --------------------------------------------
                # AI Underwriting Report URL
                # --------------------------------------------

                {
                    "id": "2GuvXtwQspvVj15flrod",
                    "fieldValue":
                        pdf_url
                        if pdf_url
                        else ""
                }
            ]

            ghl_payload = {
                "customFields": custom_fields
            }

            ghl_response = requests.put(

                ghl_url,

                headers=ghl_headers,

                json=ghl_payload,

                timeout=30
            )

            print("\n========================")
            print("GHL CONTACT UPDATE")
            print("========================")

            print(
                "Status code:",
                ghl_response.status_code
            )

            print(
                "Response:",
                ghl_response.text
            )

        else:

            print(
                "Missing Contact ID or GHL API key"
            )

        # ====================================================
        # FINAL RESPONSE
        # ====================================================

        return jsonify({

            "status": "success",

            "utility_bill_url":
                bill_url,

            "extracted_data":
                extracted_data,

            "usage_calculation": {

                "annual_kwh":
                    annual_kwh,

                "current_period_kwh":
                    current_period_kwh,

                "source":
                    usage_source
            },

            "solar": {

                "latitude":
                    latitude,

                "longitude":
                    longitude,

                "pvwatts_annual_production_per_kw":
                    annual_production_per_kw,

                "system_size_kw":
                    preliminary_system_size_kw,

                "annual_solar_kwh":
                    estimated_annual_solar_kwh
            },

            "financial_underwriting": {

                "project_cost":
                    estimated_project_cost,

                "year_1_savings":
                    estimated_year_1_savings,

                "simple_payback":
                    simple_payback_years,

                "tax_credit":
                    estimated_tax_credit,

                "net_project_cost":
                    estimated_net_project_cost,

                "depreciation_benefit":
                    estimated_depreciation_benefit,

                "depreciation_tax_savings":
                    estimated_depreciation_tax_savings,

                "year_1_net_economic_benefit":
                    estimated_year_1_net_economic_benefit,

                "incentive_adjusted_payback":
                    incentive_adjusted_payback_years
            },

            "review_status":
                underwriting_review_flag,

            "pdf_url":
                pdf_url
        }) 
    except Exception as e:

        print("\n========================")
        print("FATAL WEBHOOK ERROR")
        print("========================")

        print(
            str(e)
        )

        return jsonify({

            "status": "error",

            "message":
                str(e)

        }), 500


# ============================================================
# LOCAL SERVER
# ============================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=10000
    )
