from flask import Flask, request, jsonify
from openai import OpenAI
import os
import json
import requests
import tempfile

app = Flask(__name__)

client = OpenAI(
    api_key=os.environ.get("OPENAI_API_KEY")
)


@app.route("/", methods=["GET"])
def home():
    return jsonify({"status": "online"})


@app.route("/webhook", methods=["POST"])
def webhook():

    data = request.get_json(silent=True) or {}

    print("\n========================")
    print("NEW REQUEST RECEIVED")
    print("========================")

    print(json.dumps(data, indent=4))

    # ==========================================
    # GET CUSTOM DATA FROM GHL
    # ==========================================

    custom_data = data.get("customData", {})

    bill_data = custom_data.get("utility_bill")

    print("\n========================")
    print("UTILITY BILL DATA")
    print("========================")

    print(bill_data)

    # ==========================================
    # FIND UTILITY BILL URL
    # ==========================================

    bill_url = None

    if isinstance(bill_data, str):

        bill_url = bill_data

    elif isinstance(bill_data, list) and len(bill_data) > 0:

        first_bill = bill_data[0]

        if isinstance(first_bill, str):

            bill_url = first_bill

        elif isinstance(first_bill, dict):

            bill_url = (
                first_bill.get("url")
                or first_bill.get("fileUrl")
                or first_bill.get("file_url")
                or first_bill.get("downloadUrl")
            )

    elif isinstance(bill_data, dict):

        bill_url = (
            bill_data.get("url")
            or bill_data.get("fileUrl")
            or bill_data.get("file_url")
            or bill_data.get("downloadUrl")
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

    try:

        # ==========================================
        # DOWNLOAD UTILITY BILL
        # ==========================================

        response = requests.get(
            bill_url,
            timeout=30
        )

        print("\n========================")
        print("UTILITY BILL DOWNLOAD")
        print("========================")

        print("Status code:", response.status_code)
        print("File size:", len(response.content), "bytes")
        print("Content type:", response.headers.get("Content-Type"))

        if response.status_code != 200:

            return jsonify({
                "status": "error",
                "message": "Could not download utility bill"
            }), 400

        # ==========================================
        # SAVE PDF TEMPORARILY
        # ==========================================

        with tempfile.NamedTemporaryFile(
            suffix=".pdf",
            delete=False
        ) as temp_file:

            temp_file.write(response.content)

            pdf_path = temp_file.name

        # ==========================================
        # UPLOAD PDF TO OPENAI
        # ==========================================

        uploaded_file = client.files.create(
            file=open(pdf_path, "rb"),
            purpose="user_data"
        )

        print("\n========================")
        print("FILE UPLOADED TO OPENAI")
        print("========================")

        print("File ID:", uploaded_file.id)

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
Read this utility bill carefully.

Extract these fields:

1. Utility provider name
2. Annual electricity usage in kWh
3. Peak demand in kW
4. Billing period
5. service address
6. Electricity energy rate in $/kWh

Find the rate actually charged for electricity energy consumption.

Use an explicit $/kWh energy or supply rate if the bill provides one.

If there is no explicit $/kWh rate, calculate it ONLY when possible using:

energy/supply charges ÷ electricity kWh usage.

Do NOT use:

- demand charges
- taxes
- fixed monthly/customer charges
- late fees
- unrelated delivery charges

If a reliable energy rate cannot be determined from the bill,
return an empty string.

Return ONLY valid JSON in exactly this format:

{
    "utility_provider": "",
    "annual_kwh_usage": "",
    "peak_demand_kw": "",
    "billing_period": "",
    "property_address": "",
    "electric_rate_per_kwh": ""
}

If a value cannot be found, return an empty string.
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
        # PROPERTY ADDRESS → COORDINATES
        # ==========================================

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

            geocode_data = geocode_response.json()

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

        print("Address:", property_address)
        print("Latitude:", latitude)
        print("Longitude:", longitude)

        # ==========================================
        # NREL PVWATTS
        # ==========================================

        nrel_api_key = os.environ.get(
            "NREL_API_KEY"
        )

        pvwatts_data = {}

        print("\n========================")
        print("PVWATTS CHECK")
        print("========================")

        print("Latitude:", latitude)
        print("Longitude:", longitude)

        print(
            "NREL API key found:",
            bool(nrel_api_key)
        )

        if latitude and longitude and nrel_api_key:

            pvwatts_url = (
                "https://developer.nlr.gov/api/pvwatts/v8.json"
            )

            pvwatts_params = {

                "api_key": nrel_api_key,

                "lat": latitude,

                "lon": longitude,

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

            except requests.exceptions.RequestException as e:

                print("\n========================")
                print("PVWATTS CONNECTION ERROR")
                print("========================")

                print(str(e))

                pvwatts_data = {}

        # ==========================================
        # PRELIMINARY SOLAR SYSTEM SIZING
        # ==========================================

        annual_kwh_raw = extracted_data.get(
            "annual_kwh_usage",
            ""
        )

        peak_demand_raw = extracted_data.get(
            "peak_demand_kw",
            ""
        )

        preliminary_system_size_kw = None

        annual_production_per_kw = None

        estimated_annual_solar_kwh = None

        try:

            annual_kwh = float(
                str(annual_kwh_raw)
                .replace(",", "")
                .strip()
            )

            peak_demand_kw = float(
                str(peak_demand_raw)
                .replace(",", "")
                .strip()
            )

            annual_production_per_kw = float(
                pvwatts_data
                .get("outputs", {})
                .get("ac_annual", 0)
            )

            if (
                annual_kwh > 0
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

        except (ValueError, TypeError):

            print(
                "Could not calculate "
                "preliminary system size."
            )

        print("\n========================")
        print("PRELIMINARY SYSTEM SIZING")
        print("========================")

        print(
            "Annual usage:",
            annual_kwh_raw,
            "kWh"
        )

        print(
            "Peak demand:",
            peak_demand_raw,
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

        # ==========================================
        # FINANCIAL VARIABLES
        # ==========================================

        cost_per_watt = float(
            os.environ.get(
                "SOLAR_COST_PER_WATT",
                "1.50"
            )
        )

        estimated_project_cost = None

        estimated_year_1_savings = None

        simple_payback_years = None

        # ==========================================
        # PROJECT COST
        # ==========================================

        if preliminary_system_size_kw is not None:

            estimated_project_cost = (
                preliminary_system_size_kw
                * 1000
                * cost_per_watt
            )

        # ==========================================
        # ELECTRICITY RATE + YEAR 1 SAVINGS
        # ==========================================

        electricity_rate_raw = extracted_data.get(
            "electric_rate_per_kwh",
            ""
        )

        electricity_rate = None

        try:

            electricity_rate = float(
                str(electricity_rate_raw)
                .replace("$", "")
                .replace(",", "")
                .strip()
            )

            if (
                estimated_annual_solar_kwh is not None
                and electricity_rate > 0
            ):

                estimated_year_1_savings = (
                    estimated_annual_solar_kwh
                    * electricity_rate
                )

            if (
                estimated_project_cost is not None
                and estimated_year_1_savings is not None
                and estimated_year_1_savings > 0
            ):

                simple_payback_years = (
                    estimated_project_cost
                    / estimated_year_1_savings
                )

        except (ValueError, TypeError):

            print(
                "Could not calculate "
                "Year 1 savings."
            )

        # ==========================================
        # TAX CREDIT
        # ==========================================

        tax_credit_rate_raw = os.environ.get(
            "PRELIMINARY_TAX_CREDIT_RATE",
            ""
        )

        preliminary_tax_credit_rate = None

        estimated_tax_credit = None

        estimated_net_project_cost = None

        try:

            if str(
                tax_credit_rate_raw
            ).strip() != "":

                preliminary_tax_credit_rate = (
                    float(
                        str(
                            tax_credit_rate_raw
                        )
                        .replace("%", "")
                        .strip()
                    ) / 100
                )

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

        except (ValueError, TypeError):

            print(
                "Could not calculate "
                "preliminary tax credit."
            )

        # ==========================================
        # DEPRECIATION
        # ==========================================

        depreciation_rate_raw = os.environ.get(
            "PRELIMINARY_DEPRECIATION_RATE",
            ""
        )

        preliminary_depreciation_rate = None

        estimated_depreciation_benefit = None

        try:

            if str(
                depreciation_rate_raw
            ).strip() != "":

                preliminary_depreciation_rate = (
                    float(
                        str(
                            depreciation_rate_raw
                        )
                        .replace("%", "")
                        .strip()
                    ) / 100
                )

                if (
                    0 <= preliminary_depreciation_rate <= 1
                    and estimated_project_cost is not None
                ):

                    estimated_depreciation_benefit = (
                        estimated_project_cost
                        * preliminary_depreciation_rate
                    )

        except (ValueError, TypeError):

            print(
                "Could not calculate "
                "depreciation benefit."
            )

        # ==========================================
        # CORPORATE TAX RATE
        # ==========================================

        corporate_tax_rate_raw = os.environ.get(
            "PRELIMINARY_CORPORATE_TAX_RATE",
            ""
        )

        preliminary_corporate_tax_rate = None

        estimated_depreciation_tax_savings = None

        try:

            if str(
                corporate_tax_rate_raw
            ).strip() != "":

                preliminary_corporate_tax_rate = (
                    float(
                        str(
                            corporate_tax_rate_raw
                        )
                        .replace("%", "")
                        .strip()
                    ) / 100
                )

                if (
                    0 <= preliminary_corporate_tax_rate <= 1
                    and estimated_depreciation_benefit is not None
                ):

                    estimated_depreciation_tax_savings = (
                        estimated_depreciation_benefit
                        * preliminary_corporate_tax_rate
                    )

        except (ValueError, TypeError):

            print(
                "Could not calculate "
                "depreciation tax savings."
            )

        # ==========================================
        # INCENTIVE-ADJUSTED PAYBACK
        # ==========================================

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

        # ==========================================
        # YEAR 1 NET ECONOMIC BENEFIT
        # ==========================================

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

        # ==========================================
        # FINANCIAL UNDERWRITING LOG
        # ==========================================

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
            electricity_rate_raw
        )

        print(
            "Estimated Year 1 savings:",
            estimated_year_1_savings
        )

        print(
            "Simple payback:",
            simple_payback_years,
            "years"
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
            "Depreciation rate:",
            preliminary_depreciation_rate
        )

        print(
            "Estimated depreciation benefit:",
            estimated_depreciation_benefit
        )

        print(
            "Corporate tax rate:",
            preliminary_corporate_tax_rate
        )

        print(
            "Estimated depreciation tax savings:",
            estimated_depreciation_tax_savings
        )

        print(
            "Incentive-adjusted payback:",
            incentive_adjusted_payback_years,
            "years"
        )

        print(
            "Estimated Year 1 net economic benefit:",
            estimated_year_1_net_economic_benefit
        )

        # ==========================================
        # UPDATE GHL CONTACT
        # ==========================================

        ghl_api_key = os.environ.get(
            "GHL_API_KEY"
        )

        ghl_location_id = os.environ.get(
            "GHL_LOCATION_ID"
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

            ghl_payload = {

                "customFields": [

                    # Utility provider
                    {
                        "id": "QlceeYQHWz79JpC3RfHG",
                        "fieldValue":
                            extracted_data.get(
                                "utility_provider",
                                ""
                            )
                    },

                    # Annual kWh
                    {
                        "id": "nxlJKpBjr5vFXpsDt86M",
                        "fieldValue":
                            extracted_data.get(
                                "annual_kwh_usage",
                                ""
                            )
                    },

                    # Billing period
                    {
                        "id": "bJexeasg4bhJN9vZuC6C",
                        "fieldValue":
                            extracted_data.get(
                                "billing_period",
                                ""
                            )
                    },

                    # Peak demand
                    {
                        "id": "ESOf9cNFnZXFkgTvAL4o",
                        "fieldValue":
                            extracted_data.get(
                                "peak_demand_kw",
                                ""
                            )
                    },

                    # Property address
                    {
                        "id": "EoeFaBKcFly95M8DGYzH",
                        "fieldValue":
                            extracted_data.get(
                                "property_address",
                                ""
                            )
                    },

                    # System size
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

                    # Annual solar production
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

                    # Production per kW
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

                    # Electricity rate
                    {
                        "id": "L7WjNOBcBux2B1mkRBlR",
                        "fieldValue":
                            extracted_data.get(
                                "electric_rate_per_kwh",
                                ""
                            )
                    },

                    # Tax credit rate
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

                    # Estimated tax credit
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

                    # Net project cost
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

                    # Estimated depreciation benefit
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

                    # Depreciation tax savings
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

                    # Year 1 net economic benefit
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

                    # Incentive-adjusted payback
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
                    }
                ]
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

        # ==========================================
        # FINAL RESPONSE
        # ==========================================

        return jsonify({

            "status": "success",

            "utility_bill_url":
                bill_url,

            "extracted_data":
                extracted_data,

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
            }
        })

    except Exception as e:

        print("\n========================")
        print("ERROR")
        print("========================")

        print(str(e))

        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500


if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=10000
    )
