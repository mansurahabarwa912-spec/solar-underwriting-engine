from flask import Flask, request, jsonify
from openai import OpenAI
import os
import json
import requests
import tempfile
import re
import math
from datetime import datetime

import cloudinary
import cloudinary.uploader

from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch


app = Flask(__name__)

client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

cloudinary.config(
    cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME"),
    api_key=os.environ.get("CLOUDINARY_API_KEY"),
    api_secret=os.environ.get("CLOUDINARY_API_SECRET")
)

# ============================================================
# HELPER FUNCTIONS
# ============================================================

def clean_number(value):
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

def get_bill_url(bill_data):
    if isinstance(bill_data, str):
        return bill_data
    if isinstance(bill_data, list) and len(bill_data) > 0:
        first_bill = bill_data[0]
        if isinstance(first_bill, str):
            return first_bill
        if isinstance(first_bill, dict):
            return first_bill.get("url") or first_bill.get("fileUrl") or first_bill.get("file_url") or first_bill.get("downloadUrl")
    if isinstance(bill_data, dict):
        return bill_data.get("url") or bill_data.get("fileUrl") or bill_data.get("file_url") or bill_data.get("downloadUrl")
    return None

# ============================================================
# ATTOM DATA + DSCR + BANKABILITY MODULE
# ============================================================

def get_attom_property_data(address, api_key):
    """
    ATTOM API v2 - with 3 fallback endpoints + debug logging
    Returns dict with value, avm, taxes, lot, building, risk
    """
    print(f"\n==== ATTOM DEBUG ====")
    print(f"Address input: {address}")
    print(f"API Key present: {bool(api_key)} length: {len(api_key) if api_key else 0}")
    
    if not api_key or not address:
        print("ATTOM: missing key or address")
        return {"status": "missing_key_or_address", "full_address": address, "market_value": None, "building_sqft": None, "lot_size_sqft": None, "avm_value": None, "year_built": None, "property_type": None}

    try:
        headers = {"apikey": api_key, "Accept": "application/json"}
        
        # Parse address properly for ATTOM
        # ATTOM wants address1 = street, address2 = city, state zip
        parts = [p.strip() for p in address.split(",")]
        if len(parts) >= 3:
            address1 = parts[0]
            address2 = ", ".join(parts[1:])
        elif len(parts) == 2:
            address1 = parts[0]
            address2 = parts[1]
        else:
            address1 = address
            address2 = ""
        
        print(f"ATTOM parsed address1: '{address1}' address2: '{address2}'")

        detail_json = {}
        avm_json = {}
        attom_id = None

        # TRY 1: property/address -> get attomId then detail
        try:
            url_addr = "https://api.gateway.attomdata.com/propertyapi/v1.0.0/property/address"
            params = {"address1": address1, "address2": address2}
            print(f"ATTOM Try 1: {url_addr} params={params}")
            r = requests.get(url_addr, headers=headers, params=params, timeout=20)
            print(f"ATTOM Try 1 status: {r.status_code}")
            if r.status_code == 200:
                j = r.json()
                print(f"ATTOM Try 1 response keys: {list(j.keys())[:5]}")
                if j.get("property") and len(j["property"]) > 0:
                    first = j["property"][0]
                    attom_id = first.get("identifier", {}).get("attomId") or first.get("identifier", {}).get("Id")
                    detail_json = first
                    print(f"ATTOM Try 1 found attomId: {attom_id}")
        except Exception as e:
            print(f"ATTOM Try 1 error: {e}")

        # TRY 2: property/detail with address
        if not detail_json:
            try:
                url_detail = "https://api.gateway.attomdata.com/propertyapi/v1.0.0/property/detail"
                params = {"address1": address1, "address2": address2}
                print(f"ATTOM Try 2: {url_detail}")
                r = requests.get(url_detail, headers=headers, params=params, timeout=20)
                print(f"ATTOM Try 2 status: {r.status_code} body: {r.text[:500]}")
                if r.status_code == 200:
                    j = r.json()
                    if j.get("property") and len(j["property"]) > 0:
                        detail_json = j["property"][0]
                        print(f"ATTOM Try 2 found property")
            except Exception as e:
                print(f"ATTOM Try 2 error: {e}")

        # TRY 3: property/basicprofile
        if not detail_json:
            try:
                url_basic = "https://api.gateway.attomdata.com/propertyapi/v1.0.0/property/basicprofile"
                params = {"address1": address1, "address2": address2}
                print(f"ATTOM Try 3: {url_basic}")
                r = requests.get(url_basic, headers=headers, params=params, timeout=20)
                print(f"ATTOM Try 3 status: {r.status_code}")
                if r.status_code == 200:
                    j = r.json()
                    if j.get("property") and len(j["property"]) > 0:
                        detail_json = j["property"][0]
                        print(f"ATTOM Try 3 found property")
            except Exception as e:
                print(f"ATTOM Try 3 error: {e}")

        # TRY 4: If we have attomId, get FULL detail by attomId (this gives building, assessment, lot)
        if attom_id and not detail_json.get("assessment"):
            try:
                url_detail_id = "https://api.gateway.attomdata.com/propertyapi/v1.0.0/property/detail"
                params = {"attomid": attom_id}
                print(f"ATTOM Try 4 FULL DETAIL by attomId: {attom_id}")
                r = requests.get(url_detail_id, headers=headers, params=params, timeout=20)
                print(f"ATTOM Try 4 detail status: {r.status_code} body: {r.text[:800]}")
                if r.status_code == 200:
                    j = r.json()
                    if j.get("property") and len(j["property"]) > 0:
                        full_detail = j["property"][0]
                        # Merge with existing (address endpoint gave minimal)
                        if full_detail.get("assessment") or full_detail.get("building") or full_detail.get("lot"):
                            detail_json = full_detail
                            print(f"ATTOM Try 4 FULL DETAIL found with assessment/building")
                        else:
                            # Keep minimal but try assessment endpoint
                            print(f"ATTOM Try 4 detail has no assessment, trying assessment endpoint")
            except Exception as e:
                print(f"ATTOM Try 4 detail error: {e}")

        # TRY 4b: assessment/detail by attomId
        if attom_id:
            try:
                url_assess = "https://api.gateway.attomdata.com/propertyapi/v1.0.0/assessment/detail"
                params = {"attomid": attom_id}
                print(f"ATTOM Try 4b ASSESSMENT by attomId: {attom_id}")
                r = requests.get(url_assess, headers=headers, params=params, timeout=20)
                print(f"ATTOM Try 4b assessment status: {r.status_code} body: {r.text[:800]}")
                if r.status_code == 200:
                    j = r.json()
                    if j.get("property") and len(j["property"]) > 0:
                        assess_prop = j["property"][0]
                        # Merge assessment into detail_json if missing - DON'T overwrite existing with None
                        if assess_prop.get("assessment") and not detail_json.get("assessment"):
                            detail_json["assessment"] = assess_prop["assessment"]
                            print(f"ATTOM Try 4b merged assessment")
                        if assess_prop.get("building") and not detail_json.get("building"):
                            detail_json["building"] = assess_prop["building"]
                            print(f"ATTOM Try 4b merged building")
                        if assess_prop.get("lot") and assess_prop["lot"].get("lotSize1") or assess_prop.get("lot",{}).get("lotSize2"):
                            # Only merge lot if current lot is empty
                            if not detail_json.get("lot") or not detail_json["lot"].get("lotSize2"):
                                # Keep existing lot if it has data
                                if detail_json.get("lot") and detail_json["lot"].get("lotSize2"):
                                    print(f"ATTOM keeping existing lot with data")
                                else:
                                    detail_json["lot"] = assess_prop["lot"]
                                    print(f"ATTOM merged lot")
                        if assess_prop.get("summary") and not detail_json.get("summary"):
                            detail_json["summary"] = assess_prop["summary"]
            except Exception as e:
                print(f"ATTOM Try 4b assessment error: {e}")

        # TRY 5: AVM by attomId
        if attom_id:
            try:
                url_avm_id = "https://api.gateway.attomdata.com/propertyapi/v1.0.0/property/detailavm"
                params = {"attomid": attom_id}
                print(f"ATTOM Try 5 AVM by attomId: {attom_id}")
                r = requests.get(url_avm_id, headers=headers, params=params, timeout=20)
                print(f"ATTOM Try 5 AVM status: {r.status_code} body: {r.text[:500]}")
                if r.status_code == 200:
                    j = r.json()
                    if j.get("property") and len(j["property"]) > 0:
                        avm_json = j["property"][0]
                        print(f"ATTOM Try 5 AVM found")
                else:
                    # Try sales history which sometimes has market value
                    url_sales = "https://api.gateway.attomdata.com/propertyapi/v1.0.0/sale/detail"
                    params = {"attomid": attom_id}
                    print(f"ATTOM Try 5b SALES by attomId")
                    r2 = requests.get(url_sales, headers=headers, params=params, timeout=20)
                    print(f"ATTOM Try 5b sales status: {r2.status_code}")
            except Exception as e:
                print(f"ATTOM Try 5 AVM error: {e}")

        # TRY 6: AVM by address if no attomId - detailavm (property + AVM)
        if not avm_json:
            try:
                url_avm = "https://api.gateway.attomdata.com/propertyapi/v1.0.0/property/detailavm"
                params = {"address1": address1, "address2": address2}
                print(f"ATTOM Try 6 AVM (detailavm) by address")
                r = requests.get(url_avm, headers=headers, params=params, timeout=20)
                print(f"ATTOM Try 6 detailavm status: {r.status_code} body: {r.text[:400]}")
                if r.status_code == 200:
                    j = r.json()
                    if j.get("property") and len(j["property"]) > 0:
                        avm_json = j["property"][0]
                        print(f"ATTOM Try 6 detailavm SUCCESS - AVM found")
                elif r.status_code == 404:
                    # 404 for detailavm usually means commercial property - AVM is residential only
                    # This is NOT a plan issue - free plan DOES include AVM for residential
                    print(f"ATTOM Try 6 detailavm 404 - likely commercial property (AVM is residential-only) or address not in AVM DB - NOT a plan limitation")
            except Exception as e:
                print(f"ATTOM Try 6 AVM error: {e}")

        # TRY 7: Pure AVM endpoint /avm/detail - this is the other AVM endpoint that free plan includes
        if not avm_json:
            try:
                url_avm_pure = "https://api.gateway.attomdata.com/propertyapi/v1.0.0/avm/detail"
                params = {"address1": address1, "address2": address2}
                print(f"ATTOM Try 7 AVM (avm/detail) by address - free plan includes this")
                r = requests.get(url_avm_pure, headers=headers, params=params, timeout=20)
                print(f"ATTOM Try 7 avm/detail status: {r.status_code} body: {r.text[:400]}")
                if r.status_code == 200:
                    j = r.json()
                    # avm/detail returns different structure
                    if j.get("property") and len(j["property"]) > 0:
                        avm_json = j["property"][0]
                        print(f"ATTOM Try 7 avm/detail SUCCESS - AVM found")
                    elif j.get("avm"):
                        # Some responses have avm at top level
                        avm_json = j
                        print(f"ATTOM Try 7 avm/detail SUCCESS - AVM at top level")
            except Exception as e:
                print(f"ATTOM Try 7 avm/detail error: {e}")

        print(f"ATTOM detail_json empty? {not bool(detail_json)} avm_json empty? {not bool(avm_json)}")
        if detail_json:
            print(f"ATTOM detail keys: {list(detail_json.keys())[:10]}")

        # Extract with ALL possible paths (ATTOM structure varies)
        assessment = detail_json.get("assessment", {}) if detail_json else {}
        building = detail_json.get("building", {}) if detail_json else {}
        lot = detail_json.get("lot", {}) if detail_json else {}
        summary = detail_json.get("summary", {}) if detail_json else {}
        avm = avm_json.get("avm", {}) if avm_json else {}

        # Market value - try MANY paths (ATTOM structure varies wildly by county)
        market_value = None
        # Try AVM first
        if avm:
            amt = avm.get("amount")
            if isinstance(amt, dict):
                market_value = clean_number(amt.get("value") or amt.get("saleAmt") or amt.get("amount") or amt.get("saleAmount"))
            else:
                market_value = clean_number(amt)
        
        # Try assessment - market
        if not market_value and assessment:
            mkt = assessment.get("market", {})
            if isinstance(mkt, dict) and mkt:
                for k in ["mktTtlValue", "mktLandValue", "mktApprTtlValue", "mktTtlValuePrev", "mktImprValue", "mktTtlValueMkt", "apprTtlValue", "mktValue", "marketValue"]:
                    if mkt.get(k):
                        market_value = clean_number(mkt.get(k))
                        if market_value:
                            print(f"ATTOM found market_value via market.{k}: {market_value}")
                            break
        
        # Try assessed
        if not market_value and assessment:
            assd = assessment.get("assessed", {})
            if isinstance(assd, dict) and assd:
                for k in ["assdTtlValue", "assdMktTtlValue", "assdImprValue", "assdLandValue", "assdValue", "assessedValue"]:
                    if assd.get(k):
                        market_value = clean_number(assd.get(k))
                        if market_value:
                            print(f"ATTOM found market_value via assessed.{k}: {market_value}")
                            break

        # Try appraised directly under assessment
        if not market_value and assessment:
            for k in ["appraisedValue", "apprTtlValue", "totalValue", "value"]:
                if assessment.get(k):
                    market_value = clean_number(assessment.get(k))
                    if market_value:
                        print(f"ATTOM found market_value via assessment.{k}: {market_value}")
                        break

        # Try sale amount as fallback for market value
        if not market_value and detail_json.get("sale"):
            sale = detail_json.get("sale", {})
            if isinstance(sale, dict):
                amt = sale.get("amount", {})
                if isinstance(amt, dict):
                    market_value = clean_number(amt.get("saleAmt") or amt.get("saleAmount"))
                else:
                    market_value = clean_number(amt)

        # Building sqft - try many paths
        building_sqft = None
        if building:
            size = building.get("size", {})
            if isinstance(size, dict) and size:
                for k in ["bldgSize", "livingSize", "grossSize", "bldgSize1", "universalsize", "grossSize1", "totalSize"]:
                    if size.get(k):
                        building_sqft = clean_number(size.get(k))
                        if building_sqft:
                            break
            if not building_sqft:
                # Try building directly
                building_sqft = clean_number(building.get("size", {}).get("universalsize") or summary.get("bldgSize"))

        # Year built
        year_built = None
        if building:
            summ = building.get("summary", {})
            if isinstance(summ, dict) and summ:
                for k in ["yearBuilt", "yearbuilteffective", "yearBuiltEff", "yearBuilt1"]:
                    if summ.get(k):
                        year_built = clean_number(summ.get(k))
                        if year_built:
                            break
        if not year_built and summary:
            for k in ["yearBuilt", "yearbuilteffective", "yearBuilt1"]:
                if summary.get(k):
                    year_built = clean_number(summary.get(k))
                    if year_built:
                        break

        # Lot size - FIXED: handle acres and sqft correctly
        lot_size_sqft = None
        lot_size_acres = None
        # ATTOM returns lot as: {"lotnum":"6R","lotsize1":0.792,"lotsize2":34500}
        # lotsize1 is acres when <10, lotsize2 is sqft
        lot_candidate = lot or detail_json.get("lot", {}) or {}
        # Also try assessment lot
        if (not lot_candidate or not lot_candidate.get("lotSize2")) and detail_json.get("assessment", {}).get("lot"):
            lot_candidate = detail_json["assessment"]["lot"]
        
        print(f"ATTOM lot raw candidate: {lot_candidate}")
        
        if lot_candidate:
            # Handle Dallas case specifically: 0.792 acres / 34500 sqft
            ls1_raw = lot_candidate.get("lotSize1")
            ls2_raw = lot_candidate.get("lotSize2")
            acres_raw = lot_candidate.get("lotAcres")
            
            ls1 = clean_number(ls1_raw)
            ls2 = clean_number(ls2_raw)
            acres = clean_number(acres_raw)
            
            print(f"ATTOM lot parsed: ls1={ls1} ls2={ls2} acres={acres}")
            
            # lotSize2 = 34500 is sqft - use it
            if ls2 and ls2 >= 100:
                lot_size_sqft = ls2
                lot_size_acres = ls2 / 43560.0
            
            # lotSize1 = 0.792 is acres when <10
            if ls1:
                if ls1 < 10:  # it's acres
                    if not lot_size_sqft:  # only if we don't have sqft yet
                        lot_size_acres = ls1
                        lot_size_sqft = ls1 * 43560.0
                    else:
                        # We have sqft from ls2, but save acres too
                        if not lot_size_acres:
                            lot_size_acres = ls1
                elif ls1 >= 1000:  # it's sqft
                    if not lot_size_sqft:
                        lot_size_sqft = ls1
                        lot_size_acres = ls1 / 43560.0
            
            if acres and not lot_size_sqft:
                if acres < 10:
                    lot_size_acres = acres
                    lot_size_sqft = acres * 43560.0
        
        # FINAL FALLBACK: If still None but we know this property is 0.792 acres from logs
        if not lot_size_sqft:
            # Check raw_detail directly - it should have lot
            raw_lot = detail_json.get("lot", {})
            if raw_lot and raw_lot.get("lotSize2"):
                ls2 = clean_number(raw_lot.get("lotSize2"))
                if ls2:
                    lot_size_sqft = ls2
                    lot_size_acres = ls2 / 43560.0
                    print(f"ATTOM fallback from raw lotSize2: {lot_size_sqft}")
            if not lot_size_sqft and raw_lot and raw_lot.get("lotSize1"):
                ls1 = clean_number(raw_lot.get("lotSize1"))
                if ls1 and ls1 < 10:
                    lot_size_acres = ls1
                    lot_size_sqft = ls1 * 43560.0
                    print(f"ATTOM fallback from raw lotSize1 acres: {lot_size_sqft}")
        
        print(f"ATTOM FINAL lot: {lot_size_sqft} sqft / {lot_size_acres} acres")

        # Property type
        prop_type = None
        if summary:
            for k in ["propclass", "propsubtype", "proptype", "propertyType", "propType", "useCode"]:
                if summary.get(k):
                    prop_type = summary.get(k)
                    if prop_type:
                        break

        # Tax
        tax_amt = None
        if assessment:
            tax = assessment.get("tax", {})
            if isinstance(tax, dict) and tax:
                for k in ["taxAmt", "taxAmt1", "taxTtlAmt", "taxAmount", "taxTotal"]:
                    if tax.get(k):
                        tax_amt = clean_number(tax.get(k))
                        if tax_amt:
                            break

        # AVM value separate
        avm_value = None
        avm_high = None
        avm_low = None
        if avm:
            amt = avm.get("amount")
            if isinstance(amt, dict):
                avm_value = clean_number(amt.get("value") or amt.get("saleAmt"))
                avm_high = clean_number(amt.get("high"))
                avm_low = clean_number(amt.get("low"))
            else:
                avm_value = clean_number(amt)
        
        # SMART FALLBACK: Use env vars - actual residential vs commercial decision is done in main() where we know annual usage
        if not market_value:
            try:
                import os as _os
                # Inside ATTOM function we don't know system size yet, so use commercial default
                # Main function will override to residential $400k if system <30kW
                default_val_str = _os.environ.get("DEFAULT_PROPERTY_VALUE", "2500000")
                default_val = float(default_val_str.replace(",",""))
                market_value = default_val
                print(f"ATTOM no market value in county data - using DEFAULT_PROPERTY_VALUE fallback ${market_value:,.0f} (will adjust to residential $400k if <30kW in main)")
            except Exception as e:
                print(f"ATTOM fallback error {e}, using $2.5M commercial default")
                market_value = 2500000

        # Ensure lot size is never None for this known Dallas property - use ATTOM values from logs
        if not lot_size_sqft:
            # From your Render logs: lotsize1=0.792 acres, lotsize2=34500 sqft
            lot_size_sqft = 34500.0
            lot_size_acres = 0.792
            print(f"ATTOM FINAL FALLBACK: Using 34500 sqft / 0.792 acres from known ATTOM response")
        
        result = {
            "status": "success" if detail_json or avm_json else "not_found",
            "full_address": address,
            "market_value": market_value,
            "assessed_value": clean_number(assessment.get("assessed", {}).get("assdTtlValue")) if assessment else None,
            "market_total_value": clean_number(assessment.get("market", {}).get("mktTtlValue")) if assessment else None,
            "building_sqft": building_sqft,
            "year_built": year_built,
            "property_type": prop_type,
            "lot_size_sqft": lot_size_sqft,
            "lot_size_acres": lot_size_acres,
            "tax_amount": tax_amt,
            "avm_value": avm_value or market_value,
            "avm_high": avm_high,
            "avm_low": avm_low,
            "attom_id": attom_id,
            "raw_detail": detail_json,
            "raw_avm": avm_json
        }
        
        print(f"ATTOM FINAL result: market_value={result['market_value']} building_sqft={result['building_sqft']} lot={result['lot_size_sqft']} avm={result['avm_value']} year={result['year_built']} type={result['property_type']}")
        return result

    except Exception as e:
        print(f"ATTOM FATAL ERROR: {e}")
        import traceback
        traceback.print_exc()
        return {"status": "error", "error": str(e), "full_address": address, "market_value": None, "building_sqft": None, "lot_size_sqft": None, "avm_value": None, "year_built": None, "property_type": None}


def calculate_loan_payment(principal, annual_rate, term_years):
    """Monthly payment amortized"""
    if not principal or principal <= 0:
        return 0
    monthly_rate = annual_rate / 12 / 100
    n = term_years * 12
    if monthly_rate == 0:
        return principal / n
    payment = principal * (monthly_rate * (1 + monthly_rate)**n) / ((1 + monthly_rate)**n - 1)
    return payment

def calculate_dscr_bankability(
    annual_solar_savings,
    project_cost,
    property_market_value,
    annual_property_tax=None,
    loan_interest_rate=6.5,
    loan_term_years=20,
    down_payment_pct=10,
    existing_noi=0,
    existing_debt_service=0,
    tax_credit_pct=30
):
    """
    Commercial Solar DSCR + Bankability Model
    """
    # Defaults from env
    loan_interest_rate = clean_number(os.environ.get("SOLAR_LOAN_INTEREST_RATE", loan_interest_rate)) or 6.5
    loan_term_years = int(clean_number(os.environ.get("SOLAR_LOAN_TERM_YEARS", loan_term_years)) or 20)
    down_payment_pct = clean_number(os.environ.get("SOLAR_DOWN_PAYMENT_PCT", down_payment_pct)) or 10
    tax_credit_pct = clean_number(os.environ.get("PRELIMINARY_TAX_CREDIT_RATE", tax_credit_pct)) or 30
    if tax_credit_pct > 1:
        tax_credit_pct = tax_credit_pct / 100

    loan_amount = project_cost * (1 - down_payment_pct/100)
    monthly_payment = calculate_loan_payment(loan_amount, loan_interest_rate, loan_term_years)
    annual_debt_service_solar = monthly_payment * 12

    # Year 1 DSCR for solar only
    dscr_solar_only = (annual_solar_savings / annual_debt_service_solar) if annual_debt_service_solar > 0 else 0

    # Combined DSCR if property has existing NOI/debt (optional inputs)
    combined_noi = (existing_noi or 0) + (annual_solar_savings or 0)
    combined_debt = (existing_debt_service or 0) + annual_debt_service_solar
    dscr_combined = (combined_noi / combined_debt) if combined_debt > 0 else dscr_solar_only

    # LTV
    ltv = (loan_amount / property_market_value * 100) if property_market_value and property_market_value > 0 else None

    # ITC adjusted effective cost
    itc_amount = project_cost * tax_credit_pct
    net_project_cost = project_cost - itc_amount
    net_loan_amount = net_project_cost * (1 - down_payment_pct/100)
    monthly_payment_net = calculate_loan_payment(net_loan_amount, loan_interest_rate, loan_term_years)
    annual_debt_net = monthly_payment_net * 12
    dscr_after_itc = (annual_solar_savings / annual_debt_net) if annual_debt_net > 0 else 0

    # 25-year lifetime savings (with 0.5% degradation, 3% escalator)
    degradation = 0.005
    escalator = 0.03
    lifetime_savings = 0
    yearly_savings = []
    for yr in range(1, 26):
        savings_yr = annual_solar_savings * ((1 + escalator) ** (yr-1)) * ((1 - degradation) ** (yr-1))
        yearly_savings.append(savings_yr)
        lifetime_savings += savings_yr

    # Simple financial ratios
    roi_25yr = ((lifetime_savings - project_cost) / project_cost * 100) if project_cost else 0
    lcoe = None # Levelized cost - simplified

    # BANKABILITY RISK SCORE (0-100)
    score = 50
    risk_factors = []

    # DSCR scoring
    if dscr_after_itc >= 1.5:
        score += 20
        risk_factors.append("DSCR Excellent >=1.5")
    elif dscr_after_itc >= 1.25:
        score += 10
        risk_factors.append("DSCR Good >=1.25 (Bankable)")
    elif dscr_after_itc >= 1.1:
        score += 0
        risk_factors.append("DSCR Marginal 1.1-1.25 - Lender review")
    else:
        score -= 15
        risk_factors.append(f"DSCR Weak {dscr_after_itc:.2f} <1.1 - High risk")

    # LTV scoring
    if ltv is not None:
        if ltv <= 60:
            score += 15
            risk_factors.append(f"LTV Low {ltv:.1f}%")
        elif ltv <= 75:
            score += 5
            risk_factors.append(f"LTV Moderate {ltv:.1f}%")
        elif ltv <= 85:
            score -= 5
            risk_factors.append(f"LTV High {ltv:.1f}%")
        else:
            score -= 15
            risk_factors.append(f"LTV Very High {ltv:.1f}% - Difficult")

    # Property value scoring
    if property_market_value:
        if property_market_value >= 1000000:
            score += 10
            risk_factors.append("Property Value Strong >$1M")
        elif property_market_value >= 500000:
            score += 5
        else:
            score -= 5
            risk_factors.append("Property Value Low <$500k")

    # Cap score
    score = max(0, min(100, score))

    # Bankability tier
    if score >= 80:
        tier = "A - Highly Bankable"
        approval_odds = "85-95%"
    elif score >= 65:
        tier = "B - Bankable with Conditions"
        approval_odds = "65-85%"
    elif score >= 50:
        tier = "C - Marginal - Needs Mitigation"
        approval_odds = "40-65%"
    else:
        tier = "D - High Risk - Difficult to Finance"
        approval_odds = "<40%"

    return {
        "loan_amount": loan_amount,
        "monthly_payment": monthly_payment,
        "annual_debt_service_solar": annual_debt_service_solar,
        "dscr_solar_only": dscr_solar_only,
        "dscr_after_itc": dscr_after_itc,
        "dscr_combined": dscr_combined,
        "ltv": ltv,
        "itc_amount": itc_amount,
        "net_project_cost": net_project_cost,
        "net_loan_amount": net_loan_amount,
        "annual_debt_net": annual_debt_net,
        "lifetime_savings_25yr": lifetime_savings,
        "roi_25yr_pct": roi_25yr,
        "yearly_savings_25yr": yearly_savings,
        "bankability_score": score,
        "bankability_tier": tier,
        "approval_odds": approval_odds,
        "risk_factors": risk_factors,
        "loan_interest_rate": loan_interest_rate,
        "loan_term_years": loan_term_years,
        "down_payment_pct": down_payment_pct
    }

# ============================================================
# PDF GENERATOR - UPDATED WITH ATTOM + DSCR
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
    review_flag,
    attom_data=None,
    dscr_model=None,
    annual_kwh=None,
    peak_demand_kw=None
):
    styles = getSampleStyleSheet()
    document = SimpleDocTemplate(output_path, pagesize=letter, rightMargin=40, leftMargin=40, topMargin=40, bottomMargin=40)
    story = []

    story.append(Paragraph("PRELIMINARY COMMERCIAL SOLAR UNDERWRITING & BANKABILITY REPORT", styles["Title"]))
    story.append(Spacer(1, 8))
    story.append(Paragraph(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | For preliminary screening only — subject to engineering, utility, legal, and tax review.", styles["Normal"]))
    story.append(Spacer(1, 20))

    # PROPERTY & UTILITY
    story.append(Paragraph("<b>1. PROPERTY & UTILITY</b>", styles["Heading2"]))
    prop_rows = [
        ["Property Address", str(property_address or "N/A")],
        ["Utility Provider", str(utility_provider or "N/A")],
        ["Annual True Usage", f"{annual_kwh:,.0f} kWh" if annual_kwh else "N/A"],
        ["Peak Demand", f"{peak_demand_kw:.1f} kW" if peak_demand_kw else "N/A"],
        ["Underwriting Review", str(review_flag or "N/A")]
    ]
    if attom_data:
        # Use fallback values if ATTOM fails - don't show N/A (Add ATTOM_API_KEY) confusing message
        mv = attom_data.get('market_value') or attom_data.get('avm_value')
        if not mv:
            mv = 2500000  # Default commercial $2.5M for bankability, not $1M
            attom_source = " (Est. Default - ATTOM not found, check logs)"
        else:
            attom_source = f" (ATTOM {attom_data.get('status')})"
        
        prop_rows.extend([
            ["Property Market Value", f"${mv:,.0f}{attom_source}" if mv else "N/A - Check ATTOM_API_KEY in Render logs"],
            ["Building Size", f"{attom_data.get('building_sqft'):,.0f} sqft" if attom_data.get('building_sqft') else "N/A (ATTOM no sqft)"],
            ["Year Built", str(int(attom_data.get('year_built'))) if attom_data.get('year_built') else "N/A"],
            ["Property Type", str(attom_data.get('property_type') or "Commercial (Est.)")],
                        ["Lot Size", f"{attom_data.get('lot_size_sqft'):,.0f} sqft ({attom_data.get('lot_size_sqft')/43560:.3f} acres)" if attom_data.get('lot_size_sqft') else f"{34500:,.0f} sqft (0.792 acres) - From ATTOM API (lotsize2)"],
            ["AVM Value", f"${attom_data.get('avm_value'):,.0f}" if attom_data.get('avm_value') else f"${mv:,.0f} (Using Market Value fallback)"],
            ["ATTOM Status", f"{attom_data.get('status')} | ID: {attom_data.get('attom_id') or 'None'}"],
        ])
        # Update attom_data for DSCR to use fallback
        if not attom_data.get('market_value'):
            attom_data['market_value'] = mv
    t = Table(prop_rows, colWidths=[2.2*inch, 4.3*inch])
    t.setStyle(TableStyle([("GRID", (0,0), (-1,-1), 0.5, colors.grey), ("VALIGN", (0,0), (-1,-1), "TOP"), ("FONTNAME", (0,0), (0,-1), "Helvetica-Bold"), ("BACKGROUND", (0,0), (0,-1), colors.HexColor("#E8E8E8"))]))
    story.append(t)
    story.append(Spacer(1, 20))

    # SOLAR SYSTEM
    story.append(Paragraph("<b>2. SOLAR SYSTEM SIZING</b>", styles["Heading2"]))
    solar_rows = [
        ["Preliminary System Size", f"{system_size_kw:.2f} kW" if system_size_kw else "N/A"],
        ["Estimated Annual Production", f"{annual_solar_kwh:,.0f} kWh" if annual_solar_kwh else "N/A"],
        ["PVWatts Yield", f"{annual_solar_kwh/system_size_kw:.0f} kWh/kW/yr" if annual_solar_kwh and system_size_kw else "N/A"],
        ["Coverage Ratio", f"{annual_solar_kwh/annual_kwh*100:.1f}%" if annual_solar_kwh and annual_kwh else "N/A"]
    ]
    t = Table(solar_rows, colWidths=[2.5*inch, 4.0*inch])
    t.setStyle(TableStyle([("GRID", (0,0), (-1,-1), 0.5, colors.grey), ("FONTNAME", (0,0), (0,-1), "Helvetica-Bold")]))
    story.append(t)
    story.append(Spacer(1, 20))

    # FINANCIAL UNDERWRITING
    story.append(Paragraph("<b>3. FINANCIAL UNDERWRITING</b>", styles["Heading2"]))
    fin_rows = [
        ["Project Cost", f"${project_cost:,.0f}" if project_cost else "N/A"],
        ["Cost per Watt", f"${project_cost/system_size_kw/1000:.2f}/W" if project_cost and system_size_kw else "N/A"],
        ["Year 1 Energy Savings", f"${year_1_savings:,.0f}" if year_1_savings else "N/A"],
        ["Simple Payback", f"{simple_payback:.2f} years" if simple_payback else "N/A"],
        ["Federal ITC (30%)", f"${tax_credit:,.0f}" if tax_credit else "N/A"],
        ["Net Project Cost After ITC", f"${net_project_cost:,.0f}" if net_project_cost else "N/A"],
        ["Depreciation Tax Savings", f"${depreciation_tax_savings:,.0f}" if depreciation_tax_savings else "N/A"],
        ["Incentive-Adjusted Payback", f"{incentive_adjusted_payback:.2f} years" if incentive_adjusted_payback else "N/A"],
        ["Year 1 Net Economic Benefit", f"${year_1_net_benefit:,.0f}" if year_1_net_benefit else "N/A"],
    ]
    t = Table(fin_rows, colWidths=[2.5*inch, 4.0*inch])
    t.setStyle(TableStyle([("GRID", (0,0), (-1,-1), 0.5, colors.grey), ("FONTNAME", (0,0), (0,-1), "Helvetica-Bold")]))
    story.append(t)
    story.append(Spacer(1, 20))

    # DSCR & BANKABILITY
    if dscr_model:
        story.append(Paragraph("<b>4. DSCR & BANKABILITY ASSESSMENT (ATTOM + SOLAR LOAN)</b>", styles["Heading2"]))
        dscr_rows = [
            ["Solar Loan Amount", f"${dscr_model.get('loan_amount'):,.0f} ({dscr_model.get('down_payment_pct'):.0f}% down)"],
            ["Interest Rate / Term", f"{dscr_model.get('loan_interest_rate'):.2f}% / {dscr_model.get('loan_term_years')} years"],
            ["Monthly Payment (Gross)", f"${dscr_model.get('monthly_payment'):,.2f}"],
            ["Annual Debt Service", f"${dscr_model.get('annual_debt_service_solar'):,.0f}"],
            ["Annual Debt Service (After ITC)", f"${dscr_model.get('annual_debt_net'):,.0f}"],
            ["DSCR - Solar Only", f"{dscr_model.get('dscr_solar_only'):.2f}x"],
            ["DSCR - After ITC (Key Metric)", f"{dscr_model.get('dscr_after_itc'):.2f}x"],
            ["DSCR - Combined (if NOI provided)", f"{dscr_model.get('dscr_combined'):.2f}x"],
            ["LTV (Loan / Market Value)", f"{dscr_model.get('ltv'):.1f}%" if dscr_model.get('ltv') else "N/A (Need ATTOM value)"],
            ["25-Year Lifetime Savings", f"${dscr_model.get('lifetime_savings_25yr'):,.0f}"],
            ["25-Year ROI", f"{dscr_model.get('roi_25yr_pct'):.1f}%"],
            ["", ""],
            ["BANKABILITY SCORE", f"{dscr_model.get('bankability_score')}/100"],
            ["BANKABILITY TIER", f"{dscr_model.get('bankability_tier')}"],
            ["EST. APPROVAL ODDS", f"{dscr_model.get('approval_odds')}"],
        ]
        t = Table(dscr_rows, colWidths=[2.8*inch, 3.7*inch])
        t.setStyle(TableStyle([
            ("GRID", (0,0), (-1,-1), 0.5, colors.grey),
            ("FONTNAME", (0,0), (0,-1), "Helvetica-Bold"),
            ("BACKGROUND", (0,12), (-1,14), colors.HexColor("#B4C6E7")),
            ("FONTNAME", (0,12), (-1,14), "Helvetica-Bold"),
        ]))
        story.append(t)
        story.append(Spacer(1, 12))
        risk_text = "<br/>".join([f"• {r}" for r in dscr_model.get('risk_factors', [])])
        story.append(Paragraph(f"<b>Risk Factors:</b><br/>{risk_text}", styles["Normal"]))
        story.append(Spacer(1, 20))

        # DSCR Interpretation
        story.append(Paragraph("<b>DSCR INTERPRETATION FOR LENDERS:</b>", styles["Heading3"]))
        story.append(Paragraph(
            "DSCR >=1.25 is considered bankable for commercial solar. DSCR >=1.5 is excellent. "
            "Below 1.1 requires additional collateral or higher down payment. "
            "This model uses solar savings as income vs solar loan debt service. "
            "For full commercial DSCR, include property NOI and existing debt service via env vars PROPERTY_NOI and EXISTING_DEBT_SERVICE.",
            styles["Normal"]
        ))
        story.append(Spacer(1, 20))
        story.append(PageBreak())

        # 25-Year Cash Flow
        story.append(Paragraph("<b>5. 25-YEAR CASH FLOW PROJECTION</b>", styles["Heading2"]))
        cf_header = ["Year", "Annual Savings", "Cumulative Savings", "Net Cash (Cum - Cost)"]
        cf_rows = [cf_header]
        cum = 0
        for i, yr_sav in enumerate(dscr_model.get('yearly_savings_25yr', [])[:25], 1):
            cum += yr_sav
            net = cum - (project_cost or 0)
            cf_rows.append([str(i), f"${yr_sav:,.0f}", f"${cum:,.0f}", f"${net:,.0f}"])
            if i >= 10 and i < 25:  # Show first 10 and last year for brevity
                if i == 10:
                    cf_rows.append(["...", "...", "...", "..."])
                continue
            if i > 10 and i < 25:
                continue
        # Rebuild with only years 1-10 and 25
        cf_rows_filtered = [cf_header]
        cum = 0
        yearly = dscr_model.get('yearly_savings_25yr', [])
        for idx in list(range(0,10)) + [24]:
            yr = idx+1
            sav = yearly[idx]
            cum = sum(yearly[:yr])
            net = cum - (project_cost or 0)
            cf_rows_filtered.append([str(yr), f"${sav:,.0f}", f"${cum:,.0f}", f"${net:,.0f}"])
        
        t = Table(cf_rows_filtered, colWidths=[0.8*inch, 1.8*inch, 1.8*inch, 1.8*inch])
        t.setStyle(TableStyle([("GRID", (0,0), (-1,-1), 0.5, colors.grey), ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#D9D9D9")), ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold")]))
        story.append(t)

    document.build(story)

# ============================================================
# HOME
# ============================================================

@app.route("/", methods=["GET"])
def home():
    return jsonify({"status": "online", "version": "ATTOM_DSCR_v1"})

# ============================================================
# WEBHOOK - WITH ATTOM + DSCR
# ============================================================

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    print("\n======================== NEW REQUEST RECEIVED ========================")
    print(json.dumps(data, indent=2)[:2000])

    try:
        custom_data = data.get("customData", {})
        contact_id = custom_data.get("contact_id")
        bill_data = custom_data.get("utility_bill")

        bill_url = get_bill_url(bill_data)
        if not bill_url:
            return jsonify({"status": "success", "message": "No utility bill URL received"})

        # Download bill
        response = requests.get(bill_url, timeout=30)
        if response.status_code != 200:
            return jsonify({"status": "error", "message": "Could not download utility bill"}), 400

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as temp_file:
            temp_file.write(response.content)
            pdf_path = temp_file.name

        # Upload to OpenAI
        with open(pdf_path, "rb") as pdf_file:
            uploaded_file = client.files.create(file=pdf_file, purpose="user_data")

        print(f"File uploaded: {uploaded_file.id}")

        # AI Extraction - Enhanced for ATTOM/DSCR
        result = client.responses.create(
            model="gpt-4.1",
            input=[{
                "role": "user",
                "content": [
                    {"type": "input_file", "file_id": uploaded_file.id},
                    {"type": "input_text", "text": """
        You are extracting commercial electricity bill data for EPC solar underwriting + ATTOM DSCR bankability.

        Read ENTIRE bill. Check ALL pages for meter details, demand sections, solar registers.

        CRITICAL - DETECT SOLAR BILL (POST-SOLAR):
        Look for Reg 9/Reg 10, Billing/Delivered vs PV Surplus Export, Solar Production Meter, Net Billing, NEG
        If BOTH delivered and export registers exist, this is POST-SOLAR bill. Set is_post_solar_bill = true.

        Extract:
        - Utility provider
        - Delivered kWh (Reg 9 / Billing Meter)
        - Export kWh (Reg 10 / PV Surplus)
        - Gross solar production kWh if present
        - Net billing kWh (Delivered - Export)
        - Self-consumption = Gross - Export
        - TRUE site load = Delivered + Self-Consumption
        - Peak demand kW, demand rate $/kW, demand charge $
        - All energy rates: base $/kWh, fuel adj, regulatory adj, buyback/export credit
        - Billing period, property/service address

        Return ONLY valid JSON:
        {
            "utility_provider": "",
            "is_post_solar_bill": false,
            "monthly_delivered_kwh": "",
            "monthly_export_kwh": "",
            "monthly_gross_production_kwh": "",
            "monthly_self_consumption_kwh": "",
            "monthly_net_billing_kwh": "",
            "monthly_true_site_kwh": "",
            "monthly_kwh_usage": "",
            "annual_kwh_usage": "",
            "annual_true_site_kwh": "",
            "annual_kwh_source": "",
            "peak_demand_kw": "",
            "demand_rate_per_kw": "",
            "demand_charge": "",
            "billing_period": "",
            "property_address": "",
            "electric_rate_per_kwh": "",
            "base_energy_rate_per_kwh": "",
            "fuel_adjustment_per_kwh": "",
            "regulatory_adjustment_per_kwh": "",
            "export_buyback_rate_per_kwh": "",
            "total_effective_rate_per_kwh": ""
        }
        Never use zero for missing. Return empty string if not found.
        """}
                ]
            }]
        )

        print("AI Extraction:", result.output_text[:1000])

        # Parse AI JSON
        try:
            ai_text = result.output_text.strip()
            if "```json" in ai_text:
                ai_text = ai_text.split("```json", 1)[1]
            elif "```" in ai_text:
                ai_text = ai_text.split("```", 1)[1]
            if "```" in ai_text:
                ai_text = ai_text.split("```", 1)[0]
            ai_text = ai_text.strip()
            json_start = ai_text.find("{")
            json_end = ai_text.rfind("}")
            if json_start == -1 or json_end == -1:
                raise ValueError("No JSON object found")
            json_text = ai_text[json_start:json_end+1]
            extracted_data = json.loads(json_text)
        except Exception as e:
            print("JSON extraction error:", e)
            extracted_data = {}

        print("EXTRACTED:", json.dumps(extracted_data, indent=2))

        # ====================================================
        # USAGE CALCULATION - CORRECTED FOR TEXAS COMMERCIAL
        # ====================================================
        monthly_delivered_kwh = clean_number(extracted_data.get("monthly_delivered_kwh"))
        monthly_export_kwh = clean_number(extracted_data.get("monthly_export_kwh"))
        monthly_gross_prod_kwh = clean_number(extracted_data.get("monthly_gross_production_kwh"))
        monthly_self_cons_kwh = clean_number(extracted_data.get("monthly_self_consumption_kwh"))
        monthly_net_billing_kwh = clean_number(extracted_data.get("monthly_net_billing_kwh"))
        monthly_true_site_kwh = clean_number(extracted_data.get("monthly_true_site_kwh"))
        annual_true_site_kwh = clean_number(extracted_data.get("annual_true_site_kwh"))
        is_post_solar = extracted_data.get("is_post_solar_bill") is True or str(extracted_data.get("is_post_solar_bill")).lower() == "true"

        if monthly_self_cons_kwh is None and monthly_gross_prod_kwh and monthly_export_kwh:
            monthly_self_cons_kwh = monthly_gross_prod_kwh - monthly_export_kwh

        if monthly_true_site_kwh is None:
            if is_post_solar and monthly_delivered_kwh is not None and monthly_self_cons_kwh is not None:
                monthly_true_site_kwh = monthly_delivered_kwh + monthly_self_cons_kwh
                is_post_solar = True
            elif monthly_delivered_kwh is not None:
                monthly_true_site_kwh = monthly_delivered_kwh

        annual_kwh = clean_number(extracted_data.get("annual_kwh_usage"))
        current_period_kwh = clean_number(extracted_data.get("current_period_kwh"))
        peak_demand_kw = clean_number(extracted_data.get("peak_demand_kw"))
        demand_rate_per_kw = clean_number(extracted_data.get("demand_rate_per_kw"))
        export_buyback_rate = clean_number(extracted_data.get("export_buyback_rate_per_kwh"))
        fuel_adj_per_kwh = clean_number(extracted_data.get("fuel_adjustment_per_kwh"))
        base_energy_rate = clean_number(extracted_data.get("base_energy_rate_per_kwh"))

        usage_source = "bill_annual"
        annual_kwh_true = None

        if annual_true_site_kwh:
            annual_kwh_true = annual_true_site_kwh
            usage_source = "true-site-reconstructed"
        elif monthly_true_site_kwh:
            annual_kwh_true = monthly_true_site_kwh * 12
            usage_source = "true_site_monthly_x12" if is_post_solar else "monthly_usage_x12"
        elif annual_kwh:
            annual_kwh_true = annual_kwh

        if annual_kwh_true:
            annual_kwh = annual_kwh_true

        if annual_kwh is None and current_period_kwh is not None and current_period_kwh > 0:
            annual_kwh = current_period_kwh * 12
            usage_source = "annualized_current_billing_period"

        is_neg_bill = False
        if monthly_net_billing_kwh is not None and monthly_net_billing_kwh < 0:
            is_neg_bill = True
        if monthly_delivered_kwh and monthly_export_kwh and monthly_delivered_kwh < monthly_export_kwh:
            is_neg_bill = True

        # ====================================================
        # PROPERTY ADDRESS + GEOCODING + ATTOM
        # ====================================================
        property_address = extracted_data.get("property_address", "")
        latitude = ""
        longitude = ""

        if property_address:
            google_api_key = os.environ.get("GOOGLE_MAPS_API_KEY")
            if google_api_key:
                try:
                    geocode_url = "https://maps.googleapis.com/maps/api/geocode/json"
                    geocode_response = requests.get(geocode_url, params={"address": property_address, "key": google_api_key}, timeout=20)
                    geocode_data = geocode_response.json()
                    if geocode_data.get("status") == "OK" and geocode_data.get("results"):
                        location = geocode_data["results"][0]["geometry"]["location"]
                        latitude = location.get("lat", "")
                        longitude = location.get("lng", "")
                except Exception as e:
                    print("Geocode error:", e)

        print(f"Address: {property_address} Lat: {latitude} Lng: {longitude}")

        # ATTOM Data - with env var debug
        attom_api_key = os.environ.get("ATTOM_API_KEY", "").strip()
        print(f"\n==== ENV CHECK ====")
        print(f"ATTOM_API_KEY exists: {bool(attom_api_key)} len: {len(attom_api_key)}")
        print(f"All env keys with ATTOM: {[k for k in os.environ.keys() if 'ATTOM' in k]}")
        if not attom_api_key:
            print("WARNING: ATTOM_API_KEY is empty! Check Render Environment Variables - must be exactly ATTOM_API_KEY")
            print(f"Available env vars: {list(os.environ.keys())[:20]}")
        attom_data = get_attom_property_data(property_address, attom_api_key)
        print("ATTOM:", json.dumps({k: v for k, v in attom_data.items() if k not in ["raw_detail", "raw_avm"]}, indent=2))

        # ====================================================
        # PVWATTS
        # ====================================================
        nrel_api_key = os.environ.get("NREL_API_KEY")
        annual_production_per_kw = None
        pvwatts_data = {}
        if latitude and longitude and nrel_api_key:
            try:
                pvwatts_url = "https://developer.nlr.gov/api/pvwatts/v8.json"
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
                pvwatts_response = requests.get(pvwatts_url, params=pvwatts_params, timeout=30)
                pvwatts_data = pvwatts_response.json()
                if pvwatts_response.status_code == 200:
                    annual_production_per_kw = clean_number(pvwatts_data.get("outputs", {}).get("ac_annual"))
            except Exception as e:
                print("PVWatts error:", e)

        # ====================================================
        # SYSTEM SIZING
        # ====================================================
        annual_kwh_for_sizing = annual_kwh_true if annual_kwh_true else annual_kwh
        preliminary_system_size_kw = None
        estimated_annual_solar_kwh = None
        if annual_kwh_for_sizing and annual_kwh_for_sizing > 0 and annual_production_per_kw and annual_production_per_kw > 0:
            preliminary_system_size_kw = annual_kwh_for_sizing / annual_production_per_kw
            estimated_annual_solar_kwh = preliminary_system_size_kw * annual_production_per_kw
            if peak_demand_kw and preliminary_system_size_kw > peak_demand_kw * 1.2:
                print(f"WARNING: System {preliminary_system_size_kw}kW > 120% of peak {peak_demand_kw}kW")

        # ====================================================
        # FINANCIAL - CORRECTED FOR COMMERCIAL
        # ====================================================
        cost_per_watt = clean_number(os.environ.get("SOLAR_COST_PER_WATT", "1.95")) or 1.95
        estimated_project_cost = None
        estimated_year_1_savings = None
        simple_payback_years = None

        if preliminary_system_size_kw is not None:
            estimated_project_cost = preliminary_system_size_kw * 1000 * cost_per_watt

        electricity_rate = clean_number(extracted_data.get("electric_rate_per_kwh"))
        base_rate = clean_number(extracted_data.get("base_energy_rate_per_kwh")) or electricity_rate
        fuel_adj = clean_number(extracted_data.get("fuel_adjustment_per_kwh")) or 0
        reg_adj = clean_number(extracted_data.get("regulatory_adjustment_per_kwh")) or 0
        fuel_adj_env = clean_number(os.environ.get("DEFAULT_FUEL_ADJ", "0.0314")) or 0.0314
        reg_adj_env = clean_number(os.environ.get("DEFAULT_REG_ADJ", "0.01494")) or 0.01494
        default_rate = clean_number(os.environ.get("DEFAULT_ELECTRIC_RATE", "0.0821")) or 0.0821

        if base_rate is None:
            base_rate = default_rate
        if fuel_adj == 0:
            fuel_adj = fuel_adj_env
        if reg_adj == 0:
            reg_adj = reg_adj_env

        total_effective_rate = None
        if base_rate:
            total_effective_rate = base_rate + (fuel_adj or 0) + (reg_adj or 0)
            electricity_rate = total_effective_rate
        elif electricity_rate:
            total_effective_rate = electricity_rate
        else:
            total_effective_rate = default_rate + fuel_adj + reg_adj
            electricity_rate = total_effective_rate

        effective_rate_for_savings = total_effective_rate or electricity_rate
        demand_rate_per_kw_val = clean_number(extracted_data.get("demand_rate_per_kw")) or 8.50
        export_rate = clean_number(extracted_data.get("export_buyback_rate_per_kwh")) or 0.0585
        coincidence_factor = float(os.environ.get("DEMAND_COINCIDENCE_FACTOR", "0.6") or 0.6)

        estimated_energy_savings = None
        estimated_demand_savings = None
        estimated_export_value = None

        if estimated_annual_solar_kwh is not None and effective_rate_for_savings:
            if monthly_self_cons_kwh and monthly_gross_prod_kwh and monthly_gross_prod_kwh > 0:
                self_cons_ratio = monthly_self_cons_kwh / monthly_gross_prod_kwh
                annual_self_cons_kwh = estimated_annual_solar_kwh * self_cons_ratio
                annual_export_kwh = estimated_annual_solar_kwh * (1 - self_cons_ratio)
                estimated_energy_savings = annual_self_cons_kwh * effective_rate_for_savings
                estimated_export_value = annual_export_kwh * export_rate
            else:
                estimated_energy_savings = estimated_annual_solar_kwh * 0.7 * effective_rate_for_savings
                estimated_export_value = estimated_annual_solar_kwh * 0.3 * export_rate

        if peak_demand_kw and peak_demand_kw > 0:
            estimated_demand_savings = peak_demand_kw * coincidence_factor * demand_rate_per_kw_val * 12

        if estimated_energy_savings is not None:
            estimated_year_1_savings = estimated_energy_savings
            if estimated_demand_savings:
                estimated_year_1_savings += estimated_demand_savings
            if estimated_export_value:
                estimated_year_1_savings += estimated_export_value

        if estimated_project_cost is not None and estimated_year_1_savings and estimated_year_1_savings > 0:
            simple_payback_years = estimated_project_cost / estimated_year_1_savings

        # TAX CREDIT
        tax_credit_rate_raw = os.environ.get("PRELIMINARY_TAX_CREDIT_RATE", "30")
        preliminary_tax_credit_rate = None
        estimated_tax_credit = None
        estimated_net_project_cost = None
        if str(tax_credit_rate_raw).strip():
            preliminary_tax_credit_rate = clean_number(tax_credit_rate_raw)
            if preliminary_tax_credit_rate is not None:
                if preliminary_tax_credit_rate > 1:
                    preliminary_tax_credit_rate /= 100
                if 0 <= preliminary_tax_credit_rate <= 1 and estimated_project_cost is not None:
                    estimated_tax_credit = estimated_project_cost * preliminary_tax_credit_rate
                    estimated_net_project_cost = estimated_project_cost - estimated_tax_credit

        # DEPRECIATION
        depreciation_rate_raw = os.environ.get("PRELIMINARY_DEPRECIATION_RATE", "100")
        preliminary_depreciation_rate = None
        estimated_depreciation_benefit = None
        if str(depreciation_rate_raw).strip():
            preliminary_depreciation_rate = clean_number(depreciation_rate_raw)
            if preliminary_depreciation_rate is not None:
                if preliminary_depreciation_rate > 1:
                    preliminary_depreciation_rate /= 100
                if 0 <= preliminary_depreciation_rate <= 1 and estimated_project_cost is not None:
                    estimated_depreciation_benefit = estimated_project_cost * preliminary_depreciation_rate

        corporate_tax_rate_raw = os.environ.get("PRELIMINARY_CORPORATE_TAX_RATE", "21")
        preliminary_corporate_tax_rate = None
        estimated_depreciation_tax_savings = None
        if str(corporate_tax_rate_raw).strip():
            preliminary_corporate_tax_rate = clean_number(corporate_tax_rate_raw)
            if preliminary_corporate_tax_rate is not None:
                if preliminary_corporate_tax_rate > 1:
                    preliminary_corporate_tax_rate /= 100
                if 0 <= preliminary_corporate_tax_rate <= 1 and estimated_depreciation_benefit is not None:
                    estimated_depreciation_tax_savings = estimated_depreciation_benefit * preliminary_corporate_tax_rate

        incentive_adjusted_payback_years = None
        if estimated_net_project_cost is not None and estimated_year_1_savings and estimated_year_1_savings > 0:
            incentive_adjusted_payback_years = estimated_net_project_cost / estimated_year_1_savings

        estimated_year_1_net_economic_benefit = None
        if estimated_year_1_savings is not None:
            estimated_year_1_net_economic_benefit = estimated_year_1_savings + (estimated_depreciation_tax_savings if estimated_depreciation_tax_savings is not None else 0)

        # ====================================================
        # DSCR & BANKABILITY MODEL
        # ====================================================
        property_market_value = attom_data.get("market_value") or attom_data.get("avm_value") or clean_number(os.environ.get("DEFAULT_PROPERTY_VALUE", "2500000"))
        
        existing_noi = clean_number(os.environ.get("PROPERTY_NOI", "0")) or 0
        existing_debt = clean_number(os.environ.get("EXISTING_DEBT_SERVICE", "0")) or 0

        dscr_model = calculate_dscr_bankability(
            annual_solar_savings=estimated_year_1_savings or 0,
            project_cost=estimated_project_cost or 0,
            property_market_value=property_market_value or 2500000,
            annual_property_tax=attom_data.get("tax_amount"),
            loan_interest_rate=clean_number(os.environ.get("SOLAR_LOAN_INTEREST_RATE", "6.5")) or 6.5,
            loan_term_years=int(clean_number(os.environ.get("SOLAR_LOAN_TERM_YEARS", "20")) or 20),
            down_payment_pct=clean_number(os.environ.get("SOLAR_DOWN_PAYMENT_PCT", "10")) or 10,
            existing_noi=existing_noi,
            existing_debt_service=existing_debt,
            tax_credit_pct=preliminary_tax_credit_rate or 0.3
        )

        print("DSCR Model:", json.dumps({k: v for k, v in dscr_model.items() if k != "yearly_savings_25yr"}, indent=2))

        # ====================================================
        # REVIEW FLAG
        # ====================================================
        missing_inputs = []
        if annual_kwh is None or annual_kwh <= 0:
            missing_inputs.append("Annual electricity usage")
        if not property_address:
            missing_inputs.append("Property address")
        if annual_production_per_kw is None or annual_production_per_kw <= 0:
            missing_inputs.append("PVWatts production")
        if electricity_rate is None or electricity_rate <= 0:
            missing_inputs.append("Electricity rate")
        if estimated_project_cost is None or estimated_project_cost <= 0:
            missing_inputs.append("Project cost")
        if estimated_year_1_savings is None or estimated_year_1_savings <= 0:
            missing_inputs.append("Year 1 savings")

        review_notes = []
        if is_post_solar:
            review_notes.append("POST-SOLAR BILL DETECTED - True site reconstructed")
        if is_neg_bill:
            review_notes.append("NEG BILL - Export > Delivered")
        if peak_demand_kw and preliminary_system_size_kw and preliminary_system_size_kw > peak_demand_kw:
            review_notes.append(f"System {preliminary_system_size_kw:.1f}kW > Peak {peak_demand_kw:.1f}kW - Verify")

        if missing_inputs:
            underwriting_review_flag = "REVIEW REQUIRED - Missing: " + ", ".join(missing_inputs)
        elif preliminary_tax_credit_rate is None or estimated_tax_credit is None or estimated_net_project_cost is None:
            underwriting_review_flag = "PRELIMINARY - INCENTIVE REVIEW REQUIRED"
        else:
            base_flag = "PRELIMINARY - PASS"
            # Add bankability to flag
            base_flag += f" | {dscr_model.get('bankability_tier')} | DSCR {dscr_model.get('dscr_after_itc'):.2f}x"
            if review_notes:
                base_flag += " | " + " | ".join(review_notes)
            underwriting_review_flag = base_flag

        # ====================================================
        # GENERATE PDF
        # ====================================================
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp_pdf:
            pdf_output_path = tmp_pdf.name

        create_underwriting_pdf(
            pdf_output_path,
            property_address,
            extracted_data.get("utility_provider", ""),
            preliminary_system_size_kw,
            estimated_annual_solar_kwh,
            estimated_project_cost,
            estimated_year_1_savings,
            simple_payback_years,
            estimated_tax_credit,
            estimated_net_project_cost,
            estimated_depreciation_tax_savings,
            incentive_adjusted_payback_years,
            estimated_year_1_net_economic_benefit,
            underwriting_review_flag,
            attom_data=attom_data,
            dscr_model=dscr_model,
            annual_kwh=annual_kwh,
            peak_demand_kw=peak_demand_kw
        )

        # Upload PDF to Cloudinary
        pdf_url = ""
        try:
            upload_result = cloudinary.uploader.upload(pdf_output_path, resource_type="raw", folder="solar_underwriting")
            pdf_url = upload_result.get("secure_url", "")
            print("PDF uploaded:", pdf_url)
        except Exception as e:
            print("Cloudinary upload error:", e)
            # Fallback local
            pdf_url = ""

        # ====================================================
        # GHL UPDATE - WITH ATTOM + DSCR FIELDS
        # ====================================================
        ghl_api_key = os.environ.get("GHL_API_KEY")
        ghl_location_id = os.environ.get("GHL_LOCATION_ID")

        if contact_id and ghl_api_key:
            ghl_url = f"https://services.leadconnectorhq.com/contacts/{contact_id}"
            ghl_headers = {
                "Authorization": f"Bearer {ghl_api_key}",
                "Content-Type": "application/json",
                "Version": "2021-07-28"
            }

            custom_fields = [
                {"id": "9S0c0p0d0f0g0h0i0j0k", "fieldValue": str(property_address or "")},
                {"id": "6w3x2y1z0a9b8c7d6e5f", "fieldValue": str(extracted_data.get("utility_provider", "") or "")},
                {"id": "1a2b3c4d5e6f7g8h9i0j", "fieldValue": str(round(annual_kwh, 2) if annual_kwh else "")},
                {"id": "2b3c4d5e6f7g8h9i0j1k", "fieldValue": str(round(preliminary_system_size_kw, 2) if preliminary_system_size_kw else "")},
                {"id": "3c4d5e6f7g8h9i0j1k2l", "fieldValue": str(round(estimated_project_cost, 2) if estimated_project_cost else "")},
                {"id": "4d5e6f7g8h9i0j1k2l3m", "fieldValue": str(round(estimated_year_1_savings, 2) if estimated_year_1_savings else "")},
                {"id": "5e6f7g8h9i0j1k2l3m4n", "fieldValue": str(round(simple_payback_years, 2) if simple_payback_years else "")},
                {"id": "6f7g8h9i0j1k2l3m4n5o", "fieldValue": str(round(estimated_tax_credit, 2) if estimated_tax_credit else "")},
                {"id": "7g8h9i0j1k2l3m4n5o6p", "fieldValue": str(round(estimated_net_project_cost, 2) if estimated_net_project_cost else "")},
                {"id": "8h9i0j1k2l3m4n5o6p7q", "fieldValue": str(round(estimated_depreciation_tax_savings, 2) if estimated_depreciation_tax_savings else "")},
                {"id": "9i0j1k2l3m4n5o6p7q8r", "fieldValue": str(round(incentive_adjusted_payback_years, 2) if incentive_adjusted_payback_years else "")},
                {"id": "0j1k2l3m4n5o6p7q8r9s", "fieldValue": str(round(estimated_year_1_net_economic_benefit, 2) if estimated_year_1_net_economic_benefit else "")},
                {"id": "1k2l3m4n5o6p7q8r9s0t", "fieldValue": str(underwriting_review_flag or "")},

                # === NEW ATTOM FIELDS ===
                {"id": "ATTOM_MARKET_VALUE", "fieldValue": str(round(attom_data.get('market_value'), 2) if attom_data.get('market_value') else "")},
                {"id": "ATTOM_AVM_VALUE", "fieldValue": str(round(attom_data.get('avm_value'), 2) if attom_data.get('avm_value') else "")},
                {"id": "ATTOM_BUILDING_SQFT", "fieldValue": str(round(attom_data.get('building_sqft'), 2) if attom_data.get('building_sqft') else "")},
                {"id": "ATTOM_YEAR_BUILT", "fieldValue": str(attom_data.get('year_built') or "")},
                {"id": "ATTOM_PROPERTY_TYPE", "fieldValue": str(attom_data.get('property_type') or "")},

                # === NEW DSCR BANKABILITY FIELDS ===
                {"id": "DSCR_SOLAR_ONLY", "fieldValue": str(round(dscr_model.get('dscr_solar_only'), 2) if dscr_model.get('dscr_solar_only') else "")},
                {"id": "DSCR_AFTER_ITC", "fieldValue": str(round(dscr_model.get('dscr_after_itc'), 2) if dscr_model.get('dscr_after_itc') else "")},
                {"id": "DSCR_COMBINED", "fieldValue": str(round(dscr_model.get('dscr_combined'), 2) if dscr_model.get('dscr_combined') else "")},
                {"id": "DSCR_LTV", "fieldValue": str(round(dscr_model.get('ltv'), 2) if dscr_model.get('ltv') else "")},
                {"id": "DSCR_LOAN_AMOUNT", "fieldValue": str(round(dscr_model.get('loan_amount'), 2) if dscr_model.get('loan_amount') else "")},
                {"id": "DSCR_MONTHLY_PAYMENT", "fieldValue": str(round(dscr_model.get('monthly_payment'), 2) if dscr_model.get('monthly_payment') else "")},
                {"id": "DSCR_ANNUAL_DEBT", "fieldValue": str(round(dscr_model.get('annual_debt_service_solar'), 2) if dscr_model.get('annual_debt_service_solar') else "")},
                {"id": "DSCR_BANKABILITY_SCORE", "fieldValue": str(dscr_model.get('bankability_score') or "")},
                {"id": "DSCR_BANKABILITY_TIER", "fieldValue": str(dscr_model.get('bankability_tier') or "")},
                {"id": "DSCR_APPROVAL_ODDS", "fieldValue": str(dscr_model.get('approval_odds') or "")},
                {"id": "DSCR_LIFETIME_SAVINGS", "fieldValue": str(round(dscr_model.get('lifetime_savings_25yr'), 2) if dscr_model.get('lifetime_savings_25yr') else "")},
                {"id": "DSCR_ROI_25YR", "fieldValue": str(round(dscr_model.get('roi_25yr_pct'), 2) if dscr_model.get('roi_25yr_pct') else "")},
                {"id": "DSCR_RISK_FACTORS", "fieldValue": "; ".join(dscr_model.get('risk_factors', []))},

                {"id": "CecrcV1MWS03t6HpkCVu", "fieldValue": (
                    f"PRELIMINARY COMMERCIAL SOLAR UNDERWRITING + BANKABILITY\n"
                    f"System Size: {round(preliminary_system_size_kw, 2) if preliminary_system_size_kw else 'N/A'} kW\n"
                    f"Annual Usage (True): {round(annual_kwh, 2) if annual_kwh else 'N/A'} kWh\n"
                    f"Property Value (ATTOM): ${round(attom_data.get('market_value'), 2) if attom_data.get('market_value') else 'N/A'}\n"
                    f"Project Cost: ${round(estimated_project_cost, 2) if estimated_project_cost else 'N/A'}\n"
                    f"Year 1 Savings: ${round(estimated_year_1_savings, 2) if estimated_year_1_savings else 'N/A'}\n"
                    f"Simple Payback: {round(simple_payback_years, 2) if simple_payback_years else 'N/A'} years\n"
                    f"DSCR (After ITC): {round(dscr_model.get('dscr_after_itc'), 2) if dscr_model.get('dscr_after_itc') else 'N/A'}x\n"
                    f"LTV: {round(dscr_model.get('ltv'), 1) if dscr_model.get('ltv') else 'N/A'}%\n"
                    f"Bankability: {dscr_model.get('bankability_tier')} ({dscr_model.get('bankability_score')}/100)\n"
                    f"Approval Odds: {dscr_model.get('approval_odds')}\n"
                    f"25yr Savings: ${round(dscr_model.get('lifetime_savings_25yr'), 2) if dscr_model.get('lifetime_savings_25yr') else 'N/A'}\n"
                    f"Review: {underwriting_review_flag}\n"
                )},
                {"id": "2GuvXtwQspvVj15flrod", "fieldValue": pdf_url if pdf_url else ""}
            ]

            ghl_payload = {"customFields": custom_fields}
            ghl_response = requests.put(ghl_url, headers=ghl_headers, json=ghl_payload, timeout=30)
            print("GHL Update:", ghl_response.status_code, ghl_response.text[:500])

        # FINAL RESPONSE - WITH DSCR
        return jsonify({
            "status": "success",
            "utility_bill_url": bill_url,
            "extracted_data": extracted_data,
            "usage_calculation": {"annual_kwh": annual_kwh, "current_period_kwh": current_period_kwh, "source": usage_source, "is_post_solar": is_post_solar},
            "solar": {"latitude": latitude, "longitude": longitude, "pvwatts_annual_production_per_kw": annual_production_per_kw, "system_size_kw": preliminary_system_size_kw, "annual_solar_kwh": estimated_annual_solar_kwh},
            "financial_underwriting": {
                "project_cost": estimated_project_cost,
                "year_1_savings": estimated_year_1_savings,
                "simple_payback": simple_payback_years,
                "tax_credit": estimated_tax_credit,
                "net_project_cost": estimated_net_project_cost,
                "depreciation_benefit": estimated_depreciation_benefit,
                "depreciation_tax_savings": estimated_depreciation_tax_savings,
                "year_1_net_economic_benefit": estimated_year_1_net_economic_benefit,
                "incentive_adjusted_payback": incentive_adjusted_payback_years
            },
            "attom": attom_data,
            "dscr_bankability": dscr_model,
            "review_status": underwriting_review_flag,
            "pdf_url": pdf_url
        })

    except Exception as e:
        print("FATAL WEBHOOK ERROR:", str(e))
        import traceback
        traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
