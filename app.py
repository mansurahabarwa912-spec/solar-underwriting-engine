from flask import Flask, request, jsonify
from openai import OpenAI
import os
import json
import requests
import tempfile
import re
from datetime import datetime
import cloudinary
import cloudinary.uploader
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.lib.styles import getSampleStyleSheet

app = Flask(__name__)
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
cloudinary.config(
    cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME"),
    api_key=os.environ.get("CLOUDINARY_API_KEY"),
    api_secret=os.environ.get("CLOUDINARY_API_SECRET")
)

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
MAX_BILL_SIZE_MB = 20

# ===== YOUR REAL GHL IDS =====
GHL_IDS = {
    "utility_provider": "QlceeYQHWz79JpC3RfHG",
    "annual_kwh": "nxlJKpBjr5vFXpsDt86M",
    "billing_period": "bJexeasg4bhJN9vZuC6C",
    "peak_demand": "ESOf9cNFnZXFkgTvAL4o",
    "property_address": "EoeFaBKcFly95M8DGYzH",
    "system_size": "303wqmJNOMRe7fhZ1OTA",
    "annual_solar_production": "hsDEvEqotfjoHL1bRoS5",
    "production_per_kw": "LuMoa9805spakF6q1qi6",
    "electricity_rate": "L7WjNOBcBux2B1mkRBlR",
    "tax_credit_rate": "BG2CefNGA9Dz9myuqymJ",
    "estimated_tax_credit": "0e349aKLH3y8jfZ0WlpJ",
    "net_project_cost": "Ymvq1fyoArmNlBZZoqmn",
    "depreciation_benefit": "5TJQOIFicqCs2aIipsFm",
    "depreciation_tax_savings": "4Twip5swlOpbwTh4Fcfv",
    "year1_net_benefit": "XGCAReyZJfuI8eLlhFeU",
    "incentive_payback_1": "4hL7mojN2jd5dwgi6Qqy",
    "incentive_payback_2": "NABZeM3IEbGMpVWzkXsd",
    "review_flag": "2RosGiBTpC9nr9twHtTu",
    "ai_summary": "CecrcV1MWS03t6HpkCVu",
    "pdf_url_old": "2GuvXtwQspvVj15flrod",
    "market_value": "1pRHpR2HQ6GbSO6FzWIq",
    "bankability_tier": "9XoLnBUSnDYMK8Pi3EwM",
    "bankability_score": "zQus0phxBkATxKOvhBTx",
    "dscr_after_itc": "jusoMJXFHIH39CxywjQX",
    "pdf_url_new": "a5hYpUIMeeZS9lYMEMhU",
}

# ===== LF105 BATTERY CONSTANTS - EVE Energy =====
LF105 = {
    "cell_ah": 105,
    "cell_v_nom": 3.2,
    "cell_kwh": 0.336,  # 105Ah * 3.2V
    "cell_ir_mohm": 0.32,
    "cycles": 5000,
    "module_cells": 16,
    "module_kwh": 5.376,  # 16 * 0.336
    "module_v": 51.2,
    "cost_per_kwh_cell_only": 95,  # $/kWh Grade A cell
    "cost_per_kwh_full_system": 145,  # $/kWh with BMS + rack + inverter integration
    "roundtrip_eff": 0.92,
    "dod": 0.90,
    "duration_hours": 2,  # bankable 2hr for C&I peak shaving
}

def clean_number(v):
    if v is None: return None
    t = str(v).strip()
    if not t: return None
    neg = "(" in t and ")" in t
    t = t.replace("(", "-").replace(")", "").replace(",", "").replace("$", "").replace("%", "")
    m = re.search(r"-?\d+(?:\.\d+)?", t)
    if not m: return None
    try:
        n = float(m.group(0))
        return -abs(n) if neg and n>0 else n
    except: return None

def get_bill_url(d):
    if isinstance(d, str): return d
    if isinstance(d, list) and d:
        f=d[0]
        if isinstance(f, str): return f
        if isinstance(f, dict): return f.get("url") or f.get("fileUrl") or f.get("file_url")
    if isinstance(d, dict): return d.get("url") or d.get("fileUrl") or d.get("file_url")
    return None

def get_attom(address, api_key, sys_kw=None):
    if not api_key or not address:
        fb = 450000 if (sys_kw and sys_kw<30) else 2500000
        return {"status":"missing","market_value":fb,"building_sqft":None,"lot_size_sqft":34500,"lot_size_acres":0.792,"avm_value":fb,"year_built":None,"property_type":"Commercial","attom_id":None}
    try:
        headers={"apikey":api_key,"Accept":"application/json"}
        parts=[p.strip() for p in address.split(",")]
        a1=parts[0]; a2=", ".join(parts[1:]) if len(parts)>1 else ""
        detail={}
        attom_id=None
        for ep in ["property/address","property/detail","property/basicprofile"]:
            try:
                r=requests.get(f"https://api.gateway.attomdata.com/propertyapi/v1.0.0/{ep}",headers=headers,params={"address1":a1,"address2":a2},timeout=15)
                if r.status_code==200 and r.json().get("property"):
                    detail=r.json()["property"][0]
                    attom_id=detail.get("identifier",{}).get("attomId")
                    if detail.get("assessment"): break
            except: continue
        assessment=detail.get("assessment",{}); lot=detail.get("lot",{}); summary=detail.get("summary",{}); building=detail.get("building",{})
        mv=None
        if assessment.get("market",{}).get("mktTtlValue"): mv=clean_number(assessment["market"]["mktTtlValue"])
        if not mv and assessment.get("assessed",{}).get("assdTtlValue"): mv=clean_number(assessment["assessed"]["assdTtlValue"])
        if not mv: mv=450000 if (sys_kw and sys_kw<30) else 2500000
        ls1,ls2=clean_number(lot.get("lotSize1")),clean_number(lot.get("lotSize2"))
        lot_sqft,lot_ac=None,None
        if ls2 and ls2>=100: lot_sqft,lot_ac=ls2,ls2/43560
        elif ls1 and ls1>=100: lot_sqft,lot_ac=ls1,ls1/43560
        elif ls1 and ls1<10: lot_ac,lot_sqft=ls1,ls1*43560
        if not lot_sqft: lot_sqft,lot_ac=34500,0.792
        return {"status":"success" if detail else "fallback","market_value":mv,"building_sqft":clean_number(building.get("size",{}).get("bldgSize")),"lot_size_sqft":lot_sqft,"lot_size_acres":lot_ac,"avm_value":mv,"year_built":clean_number(building.get("summary",{}).get("yearBuilt")),"property_type":summary.get("propclass") or "Commercial","attom_id":attom_id}
    except Exception as e:
        fb=450000 if (sys_kw and sys_kw<30) else 2500000
        return {"status":"error","market_value":fb,"building_sqft":None,"lot_size_sqft":34500,"lot_size_acres":0.792,"avm_value":fb,"year_built":None,"property_type":"Commercial","attom_id":None}

def calc_payment(principal, rate, years):
    if not principal: return 0
    mr=rate/12/100; n=years*12
    if mr==0: return principal/n
    return principal*(mr*(1+mr)**n)/((1+mr)**n-1)

def calc_dscr(annual_savings, project_cost, prop_value, itc_rate=0.3, down=10, rate=6.5, years=20, existing_noi=0, existing_debt=0):
    loan=project_cost*(1-down/100)
    mp=calc_payment(loan,rate,years); annual_debt=mp*12
    itc=project_cost*itc_rate; net_cost=project_cost-itc
    net_loan=net_cost*(1-down/100); mp_net=calc_payment(net_loan,rate,years); annual_debt_net=mp_net*12
    dscr_solar=(annual_savings/annual_debt) if annual_debt else 0
    dscr_after=(annual_savings/annual_debt_net) if annual_debt_net else 0
    comb_noi=existing_noi+annual_savings; comb_debt=existing_debt+annual_debt_net
    dscr_comb=(comb_noi/comb_debt) if comb_debt else dscr_after
    ltv=(loan/prop_value*100) if prop_value else None
    score=50; risks=[]
    if dscr_after>=1.5: score+=20; risks.append(f"DSCR Excellent {dscr_after:.2f}x")
    elif dscr_after>=1.25: score+=10; risks.append(f"DSCR Good {dscr_after:.2f}x")
    elif dscr_after<1.1: score-=15; risks.append(f"DSCR Weak {dscr_after:.2f}x")
    if ltv:
        if ltv<=60: score+=15; risks.append(f"LTV Low {ltv:.1f}%")
        elif ltv<=75: score+=5; risks.append(f"LTV Moderate {ltv:.1f}%")
        else: score-=5; risks.append(f"LTV High {ltv:.1f}%")
    score=max(0,min(100,score))
    if score>=80: tier,odds="A - Highly Bankable","85-95%"
    elif score>=65: tier,odds="B - Bankable with Conditions","65-85%"
    elif score>=50: tier,odds="C - Marginal","40-65%"
    else: tier,odds="D - High Risk","<40%"
    return {"loan_amount":loan,"monthly_payment":mp,"annual_debt_service_solar":annual_debt,"dscr_solar_only":dscr_solar,"dscr_after_itc":dscr_after,"dscr_combined":dscr_comb,"ltv":ltv,"itc_amount":itc,"net_project_cost":net_cost,"annual_debt_net":annual_debt_net,"bankability_score":score,"bankability_tier":tier,"approval_odds":odds,"risk_factors":risks,"loan_interest_rate":rate,"loan_term_years":years,"down_payment_pct":down}

def calc_battery_storage(peak_kw, annual_kwh, demand_rate=8.50):
    """NEW: Battery sizing based on bill + LF105"""
    if not peak_kw or peak_kw<=0:
        return None
    # Bankable C&I strategy: shave 25% of peak, 2-hour duration
    shave_pct = float(os.environ.get("BESS_SHAVE_PCT","0.25"))
    duration = float(os.environ.get("BESS_DURATION_HRS","2"))
    cost_per_kwh = float(os.environ.get("BESS_COST_PER_KWH","145"))

    batt_power_kw = peak_kw * shave_pct
    batt_energy_kwh = batt_power_kw * duration
    # LF105 cells needed
    cells_needed = batt_energy_kwh / LF105["cell_kwh"]
    modules_needed = batt_energy_kwh / LF105["module_kwh"]
    
    batt_cost = batt_energy_kwh * cost_per_kwh
    
    # Savings: Demand charge reduction + arbitrage
    # Demand: shave * rate * 12
    demand_savings = batt_power_kw * demand_rate * 12
    # Arbitrage: $15-25/kWh/year in Texas for commercial
    arbitrage_rate = float(os.environ.get("BESS_ARBITRAGE_RATE","18"))
    arbitrage_savings = batt_energy_kwh * arbitrage_rate
    # Roundtrip efficiency loss
    eff_loss = arbitrage_savings * (1 - LF105["roundtrip_eff"])
    net_batt_savings = demand_savings + arbitrage_savings - eff_loss

    return {
        "battery_power_kw": batt_power_kw,
        "battery_energy_kwh": batt_energy_kwh,
        "cells_needed": int(cells_needed),
        "modules_needed": round(modules_needed,1),
        "battery_cost": batt_cost,
        "demand_savings": demand_savings,
        "arbitrage_savings": arbitrage_savings,
        "net_battery_savings": net_batt_savings,
        "shave_pct": shave_pct*100,
        "duration_hrs": duration,
        "cell_model": "EVE LF105 105Ah",
        "cell_kwh": LF105["cell_kwh"],
        "module_kwh": LF105["module_kwh"],
        "ir_mohm": LF105["cell_ir_mohm"],
        "cycles": LF105["cycles"],
    }

def create_pdf(output_path, property_address, utility_provider, system_size_kw, annual_solar_kwh, project_cost, year_1_savings, simple_payback, tax_credit, net_project_cost, depreciation_tax_savings, incentive_adjusted_payback, year_1_net_benefit, review_flag, attom_data=None, dscr_model=None, dscr_storage_model=None, battery=None, annual_kwh=None, peak_demand_kw=None):
    styles=getSampleStyleSheet()
    doc=SimpleDocTemplate(output_path,pagesize=letter,rightMargin=40,leftMargin=40,topMargin=40,bottomMargin=40)
    story=[]
    story.append(Paragraph("PRELIMINARY COMMERCIAL SOLAR + STORAGE UNDERWRITING & BANKABILITY REPORT",styles["Title"]))
    story.append(Spacer(1,8))
    story.append(Paragraph(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | Includes EVE LF105 105Ah LFP Battery Storage | Preliminary only",styles["Normal"]))
    story.append(Spacer(1,20))

    story.append(Paragraph("<b>1. PROPERTY & UTILITY</b>",styles["Heading2"]))
    prop_rows=[["Property Address",str(property_address or "N/A")],["Utility Provider",str(utility_provider or "N/A")],["Annual Usage",f"{annual_kwh:,.0f} kWh" if annual_kwh else "N/A"],["Peak Demand",f"{peak_demand_kw:.1f} kW" if peak_demand_kw else "N/A"],["Review Flag",str(review_flag or "N/A")]]
    if attom_data:
        mv=attom_data.get('market_value')
        prop_rows.extend([["Market Value",f"${mv:,.0f}" if mv else "N/A"],["Lot Size",f"{attom_data.get('lot_size_sqft'):,.0f} sqft ({attom_data.get('lot_size_acres'):.3f} acres)" if attom_data.get('lot_size_sqft') else "N/A"],["ATTOM Status",f"{attom_data.get('status')}"]])
    t=Table(prop_rows,colWidths=[2.2*inch,4.3*inch])
    t.setStyle(TableStyle([("GRID",(0,0),(-1,-1),0.5,colors.grey),("FONTNAME",(0,0),(0,-1),"Helvetica-Bold"),("BACKGROUND",(0,0),(0,-1),colors.HexColor("#E8E8E8"))]))
    story.append(t); story.append(Spacer(1,20))

    story.append(Paragraph("<b>2. SOLAR SYSTEM SIZING</b>",styles["Heading2"]))
    solar_rows=[["System Size",f"{system_size_kw:.2f} kW" if system_size_kw else "N/A"],["Annual Production",f"{annual_solar_kwh:,.0f} kWh" if annual_solar_kwh else "N/A"]]
    t=Table(solar_rows,colWidths=[2.5*inch,4.0*inch])
    t.setStyle(TableStyle([("GRID",(0,0),(-1,-1),0.5,colors.grey),("FONTNAME",(0,0),(0,-1),"Helvetica-Bold")]))
    story.append(t); story.append(Spacer(1,20))

    # NEW SECTION 3: BATTERY STORAGE LF105
    if battery:
        story.append(Paragraph("<b>3. BATTERY STORAGE - EVE LF105 105Ah LFP</b>",styles["Heading2"]))
        batt_rows=[
            ["Cell Model",f"{battery['cell_model']} (EVE Energy Stock 300014)"],
            ["Cell Spec",f"{LF105['cell_ah']}Ah / {LF105['cell_v_nom']}V / {battery['cell_kwh']} kWh / {battery['ir_mohm']}mΩ IR"],
            ["System Size",f"{battery['battery_power_kw']:.1f} kW / {battery['battery_energy_kwh']:.1f} kWh ({battery['duration_hrs']}hr duration)"],
            ["Cells Required",f"{battery['cells_needed']} cells ({battery['modules_needed']} x 16S modules of {battery['module_kwh']} kWh)"],
            ["Cycle Life",f"{battery['cycles']} cycles @ 80% DOD (M4/M6 threaded hole, EV&ESS compatible)"],
            ["Strategy",f"Peak Shaving {battery['shave_pct']:.0f}% of {peak_demand_kw:.0f}kW peak"],
            ["Battery Cost",f"${battery['battery_cost']:,.0f} (@ ${os.environ.get('BESS_COST_PER_KWH','145')}/kWh full system)"],
            ["Demand Savings",f"${battery['demand_savings']:,.0f}/yr (shave {battery['battery_power_kw']:.1f}kW x ${8.50}/kW/mo)"],
            ["Arbitrage Savings",f"${battery['arbitrage_savings']:,.0f}/yr"],
            ["Net Battery Savings",f"${battery['net_battery_savings']:,.0f}/yr"],
        ]
        t=Table(batt_rows,colWidths=[2.2*inch,4.3*inch])
        t.setStyle(TableStyle([("GRID",(0,0),(-1,-1),0.5,colors.grey),("FONTNAME",(0,0),(0,-1),"Helvetica-Bold"),("BACKGROUND",(0,0),(0,-1),colors.HexColor("#E0F2F1"))]))
        story.append(t); story.append(Spacer(1,20))

    story.append(Paragraph(f"<b>{'4' if battery else '3'}. FINANCIAL - SOLAR ONLY vs SOLAR+STORAGE</b>",styles["Heading2"]))
    total_cost_storage = (project_cost or 0) + (battery['battery_cost'] if battery else 0)
    total_savings_storage = (year_1_savings or 0) + (battery['net_battery_savings'] if battery else 0)
    fin_rows=[
        ["Metric","Solar Only","Solar + LF105 Storage"],
        ["Project Cost",f"${project_cost:,.0f}" if project_cost else "N/A",f"${total_cost_storage:,.0f}" if total_cost_storage else "N/A"],
        ["Year 1 Savings",f"${year_1_savings:,.0f}" if year_1_savings else "N/A",f"${total_savings_storage:,.0f}" if total_savings_storage else "N/A"],
        ["Simple Payback",f"{simple_payback:.2f} yrs" if simple_payback else "N/A",f"{total_cost_storage/total_savings_storage:.2f} yrs" if total_cost_storage and total_savings_storage else "N/A"],
        ["Federal ITC (30%)",f"${tax_credit:,.0f}" if tax_credit else "N/A",f"${total_cost_storage*0.3:,.0f}" if total_cost_storage else "N/A (Storage also 30% ITC)"],
    ]
    t=Table(fin_rows,colWidths=[2.0*inch,2.2*inch,2.3*inch])
    t.setStyle(TableStyle([("GRID",(0,0),(-1,-1),0.5,colors.grey),("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),("BACKGROUND",(0,0),(-1,0),colors.HexColor("#D9D9D9")),("FONTNAME",(0,0),(0,-1),"Helvetica-Bold")]))
    story.append(t); story.append(Spacer(1,20))

    if dscr_model:
        story.append(Paragraph(f"<b>{'5' if battery else '4'}. DSCR & BANKABILITY</b>",styles["Heading2"]))
        if dscr_storage_model and battery:
            dscr_rows=[
                ["Metric","Solar Only","Solar + Storage (LF105)"],
                ["DSCR After ITC",f"{dscr_model.get('dscr_after_itc'):.2f}x",f"{dscr_storage_model.get('dscr_after_itc'):.2f}x"],
                ["LTV",f"{dscr_model.get('ltv'):.1f}%" if dscr_model.get('ltv') else "N/A",f"{dscr_storage_model.get('ltv'):.1f}%" if dscr_storage_model.get('ltv') else "N/A"],
                ["Bankability Score",f"{dscr_model.get('bankability_score')}/100",f"{dscr_storage_model.get('bankability_score')}/100"],
                ["Tier",f"{dscr_model.get('bankability_tier')}",f"{dscr_storage_model.get('bankability_tier')}"],
                ["Approval Odds",f"{dscr_model.get('approval_odds')}",f"{dscr_storage_model.get('approval_odds')}"],
            ]
        else:
            dscr_rows=[["Solar Loan",f"${dscr_model.get('loan_amount'):,.0f}"],["DSCR After ITC",f"{dscr_model.get('dscr_after_itc'):.2f}x"],["LTV",f"{dscr_model.get('ltv'):.1f}%" if dscr_model.get('ltv') else "N/A"],["Bankability",f"{dscr_model.get('bankability_tier')} ({dscr_model.get('bankability_score')}/100)"]]
        t=Table(dscr_rows,colWidths=[2.0*inch,2.2*inch,2.3*inch] if battery else [2.8*inch,3.7*inch])
        t.setStyle(TableStyle([("GRID",(0,0),(-1,-1),0.5,colors.grey),("FONTNAME",(0,0),(0,-1),"Helvetica-Bold"),("BACKGROUND",(0,0),(-1,0),colors.HexColor("#B4C6E7") if not battery else colors.HexColor("#D9D9D9")),("BACKGROUND",(0,1),(-1,-1),colors.HexColor("#E8F5E9") if battery else colors.white)]))
        story.append(t); story.append(Spacer(1,12))
        risks="<br/>".join([f"• {r}" for r in (dscr_storage_model or dscr_model).get('risk_factors',[])])
        story.append(Paragraph(f"<b>Risk Factors (with Storage):</b><br/>{risks}",styles["Normal"]))

    doc.build(story)

@app.route("/", methods=["GET"])
def home():
    return jsonify({"status":"online","version":"V5_SOLAR_STORAGE_LF105","battery":"EVE LF105 105Ah"})

@app.route("/webhook", methods=["POST"])
def webhook():
    pdf_path=pdf_out=None; fid=None
    try:
        if WEBHOOK_SECRET and request.headers.get("X-WEBHOOK-SECRET")!=WEBHOOK_SECRET:
            return jsonify({"status":"error","message":"Unauthorized"}),401
        data=request.get_json(silent=True) or {}
        custom=data.get("customData",{}); contact_id=custom.get("contact_id"); bill_data=custom.get("utility_bill")
        bill_url=get_bill_url(bill_data)
        if not bill_url: return jsonify({"status":"success","message":"No bill"})
        if bill_url.startswith("http://"): return jsonify({"status":"error","message":"HTTPS only"}),400
        r=requests.get(bill_url,timeout=30,stream=True)
        if r.status_code!=200: return jsonify({"status":"error","message":"Download failed"}),400
        content=b""
        for chunk in r.iter_content(8192):
            content+=chunk
            if len(content)>MAX_BILL_SIZE_MB*1024*1024: return jsonify({"status":"error","message":"Too large"}),400
        with tempfile.NamedTemporaryFile(suffix=".pdf",delete=False) as tf: tf.write(content); pdf_path=tf.name
        with open(pdf_path,"rb") as f: uploaded=client.files.create(file=f,purpose="user_data"); fid=uploaded.id
        result=client.responses.create(model="gpt-4.1",input=[{"role":"user","content":[{"type":"input_file","file_id":fid},{"type":"input_text","text":"Extract commercial electricity bill. Return ONLY JSON: {\"utility_provider\": \"\", \"is_post_solar_bill\": false, \"annual_kwh_usage\": \"\", \"peak_demand_kw\": \"\", \"property_address\": \"\", \"electric_rate_per_kwh\": \"\", \"base_energy_rate_per_kwh\": \"\", \"billing_period\": \"\"}"}]}])
        try:
            txt=result.output_text.strip()
            if "```json" in txt: txt=txt.split("```json",1)[1]
            if "```" in txt: txt=txt.split("```",1)[0]
            extracted=json.loads(txt[txt.find("{"):txt.rfind("}")+1])
        except: extracted={}

        annual_kwh=clean_number(extracted.get("annual_kwh_usage")); peak_kw=clean_number(extracted.get("peak_demand_kw"))
        property_address=extracted.get("property_address","")
        lat=lon=""
        if property_address and os.environ.get("GOOGLE_MAPS_API_KEY"):
            try:
                g=requests.get("https://maps.googleapis.com/maps/api/geocode/json",params={"address":property_address,"key":os.environ.get("GOOGLE_MAPS_API_KEY")},timeout=15).json()
                if g.get("status")=="OK" and g.get("results"): loc=g["results"][0]["geometry"]["location"]; lat,lon=loc.get("lat",""),loc.get("lng","")
            except: pass
        annual_prod_per_kw=None
        if lat and lon and os.environ.get("NREL_API_KEY"):
            try:
                resp=requests.get("https://developer.nlr.gov/api/pvwatts/v8.json",params={"api_key":os.environ.get("NREL_API_KEY"),"lat":lat,"lon":lon,"system_capacity":1,"azimuth":180,"tilt":20,"array_type":1,"module_type":1,"losses":14},timeout=20)
                if resp.status_code==200: annual_prod_per_kw=clean_number(resp.json().get("outputs",{}).get("ac_annual"))
            except: pass
        sys_kw=(annual_kwh/annual_prod_per_kw) if annual_kwh and annual_prod_per_kw else None
        annual_solar=sys_kw*annual_prod_per_kw if sys_kw and annual_prod_per_kw else None
        cost_per_w=clean_number(os.environ.get("SOLAR_COST_PER_WATT","1.95")) or 1.95
        proj_cost=sys_kw*1000*cost_per_w if sys_kw else None
        base_rate=clean_number(extracted.get("base_energy_rate_per_kwh")) or clean_number(os.environ.get("DEFAULT_ELECTRIC_RATE","0.0821")) or 0.0821
        fuel=clean_number(os.environ.get("DEFAULT_FUEL_ADJ","0.0314")) or 0.0314
        reg=clean_number(os.environ.get("DEFAULT_REG_ADJ","0.01494")) or 0.01494
        total_rate=base_rate+fuel+reg
        y1_sav=None
        if annual_solar and total_rate: y1_sav=annual_solar*0.7*total_rate + annual_solar*0.3*0.0585 + (peak_kw*0.6*8.5*12 if peak_kw else 0)
        payback=(proj_cost/y1_sav) if proj_cost and y1_sav else None
        itc_rate=0.3; tax_credit=proj_cost*itc_rate if proj_cost else None; net_cost=proj_cost-tax_credit if proj_cost and tax_credit else None
        dep_benefit=proj_cost*1.0 if proj_cost else None; dep_tax_sav=dep_benefit*0.21 if dep_benefit else None
        inc_payback=(net_cost/y1_sav) if net_cost and y1_sav else None
        y1_net=(y1_sav or 0)+(dep_tax_sav or 0)

        attom=get_attom(property_address,os.environ.get("ATTOM_API_KEY","").strip(),sys_kw)
        prop_val=attom.get("market_value") or (450000 if sys_kw and sys_kw<30 else 2500000)
        dscr=calc_dscr(y1_sav or 0, proj_cost or 0, prop_val, itc_rate)

        # NEW: BATTERY CALC
        battery=calc_battery_storage(peak_kw, annual_kwh, demand_rate=8.50)
        dscr_storage=None
        if battery:
            total_cost_storage=(proj_cost or 0)+battery["battery_cost"]
            total_sav_storage=(y1_sav or 0)+battery["net_battery_savings"]
            dscr_storage=calc_dscr(total_sav_storage, total_cost_storage, prop_val, itc_rate)
            review=f"PRELIMINARY - SOLAR {dscr.get('bankability_tier')} | SOLAR+LF105 {dscr_storage.get('bankability_tier')} | DSCR {dscr.get('dscr_after_itc'):.2f}x → {dscr_storage.get('dscr_after_itc'):.2f}x with storage"
        else:
            review=f"PRELIMINARY - {dscr.get('bankability_tier')} | DSCR {dscr.get('dscr_after_itc'):.2f}x"

        with tempfile.NamedTemporaryFile(suffix=".pdf",delete=False) as tmp: pdf_out=tmp.name
        create_pdf(pdf_out,property_address,extracted.get("utility_provider",""),sys_kw,annual_solar,proj_cost,y1_sav,payback,tax_credit,net_cost,dep_tax_sav,inc_payback,y1_net,review,attom_data=attom,dscr_model=dscr,dscr_storage_model=dscr_storage,battery=battery,annual_kwh=annual_kwh,peak_demand_kw=peak_kw)

        pdf_url=""
        try:
            up=cloudinary.uploader.upload(pdf_out,resource_type="raw",folder="solar_underwriting")
            pdf_url=up.get("secure_url","")
        except Exception as e: print(f"Cloudinary: {e}")

        ghl_api=os.environ.get("GHL_API_KEY")
        if contact_id and ghl_api:
            url=f"https://services.leadconnectorhq.com/contacts/{contact_id}"
            hdr={"Authorization":f"Bearer {ghl_api}","Content-Type":"application/json","Version":"2021-07-28"}
            cf=[
                {"id":GHL_IDS["utility_provider"],"fieldValue":str(extracted.get("utility_provider","") or "")},
                {"id":GHL_IDS["annual_kwh"],"fieldValue":str(round(annual_kwh,2) if annual_kwh else "")},
                {"id":GHL_IDS["billing_period"],"fieldValue":str(extracted.get("billing_period","") or "")},
                {"id":GHL_IDS["peak_demand"],"fieldValue":str(round(peak_kw,2) if peak_kw else "")},
                {"id":GHL_IDS["property_address"],"fieldValue":str(property_address or "")},
                {"id":GHL_IDS["system_size"],"fieldValue":str(round(sys_kw,2) if sys_kw else "")},
                {"id":GHL_IDS["annual_solar_production"],"fieldValue":str(round(annual_solar,2) if annual_solar else "")},
                {"id":GHL_IDS["production_per_kw"],"fieldValue":str(round(annual_prod_per_kw,2) if annual_prod_per_kw else "")},
                {"id":GHL_IDS["electricity_rate"],"fieldValue":str(total_rate or "")},
                {"id":GHL_IDS["tax_credit_rate"],"fieldValue":str(round(itc_rate*100,2))},
                {"id":GHL_IDS["estimated_tax_credit"],"fieldValue":str(round(tax_credit,2) if tax_credit else "")},
                {"id":GHL_IDS["net_project_cost"],"fieldValue":str(round(net_cost,2) if net_cost else "")},
                {"id":GHL_IDS["depreciation_benefit"],"fieldValue":str(round(dep_benefit,2) if dep_benefit else "")},
                {"id":GHL_IDS["depreciation_tax_savings"],"fieldValue":str(round(dep_tax_sav,2) if dep_tax_sav else "")},
                {"id":GHL_IDS["year1_net_benefit"],"fieldValue":str(round(y1_net,2) if y1_net else "")},
                {"id":GHL_IDS["incentive_payback_1"],"fieldValue":str(round(inc_payback,2) if inc_payback else "")},
                {"id":GHL_IDS["incentive_payback_2"],"fieldValue":str(round(inc_payback,2) if inc_payback else "")},
                {"id":GHL_IDS["review_flag"],"fieldValue":str(review or "")},
                {"id":GHL_IDS["market_value"],"fieldValue":str(round(prop_val,2) if prop_val else "")},
                {"id":GHL_IDS["bankability_tier"],"fieldValue":str((dscr_storage or dscr).get('bankability_tier') or "")},
                {"id":GHL_IDS["bankability_score"],"fieldValue":str((dscr_storage or dscr).get('bankability_score') or "")},
                {"id":GHL_IDS["dscr_after_itc"],"fieldValue":str(round((dscr_storage or dscr).get('dscr_after_itc'),2) if (dscr_storage or dscr).get('dscr_after_itc') else "")},
                {"id":GHL_IDS["pdf_url_new"],"fieldValue":pdf_url or ""},
                {"id":GHL_IDS["pdf_url_old"],"fieldValue":pdf_url or ""},
                {"id":GHL_IDS["ai_summary"],"fieldValue":(
                    f"PRELIMINARY SOLAR+STORAGE UNDERWRITING (EVE LF105)\n"
                    f"Solar: {round(sys_kw,2) if sys_kw else 'N/A'}kW | Usage: {round(annual_kwh,2) if annual_kwh else 'N/A'}kWh | Peak: {round(peak_kw,1) if peak_kw else 'N/A'}kW\n"
                    f"Property: ${round(prop_val,2)} | Lot: {round(attom.get('lot_size_sqft'),0) if attom.get('lot_size_sqft') else 'N/A'} sqft\n"
                    f"Solar Cost: ${round(proj_cost,2) if proj_cost else 'N/A'} | Solar Savings: ${round(y1_sav,2) if y1_sav else 'N/A'}\n"
                    f"Battery: {battery['battery_power_kw']:.1f}kW/{battery['battery_energy_kwh']:.1f}kWh LF105 ({battery['cells_needed']} cells) | Cost: ${round(battery['battery_cost'],2)} | Savings: ${round(battery['net_battery_savings'],2)}/yr\n" if battery else ""
                    f"Solar DSCR: {round(dscr.get('dscr_after_itc'),2) if dscr.get('dscr_after_itc') else 'N/A'}x -> Solar+Storage DSCR: {round(dscr_storage.get('dscr_after_itc'),2) if dscr_storage and dscr_storage.get('dscr_after_itc') else 'N/A'}x\n"
                    f"Bankability: {dscr.get('bankability_tier')} -> {(dscr_storage or dscr).get('bankability_tier')} ({(dscr_storage or dscr).get('bankability_score')}/100)\n"
                    f"Review: {review}\n"
                )},
            ]
            try: requests.put(url,headers=hdr,json={"customFields":cf},timeout=15)
            except Exception as e: print(f"GHL error: {e}")

        return jsonify({"status":"success","solar":{"system_size_kw":sys_kw,"annual_kwh":annual_kwh,"peak_kw":peak_kw},"battery":battery,"dscr_solar":dscr,"dscr_storage":dscr_storage,"attom":attom,"pdf_url":pdf_url,"review":review})
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"status":"error","message":str(e)}),500
    finally:
        try:
            if pdf_path and os.path.exists(pdf_path): os.remove(pdf_path)
            if pdf_out and os.path.exists(pdf_out): os.remove(pdf_out)
            if fid: client.files.delete(fid)
        except: pass

if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT",10000)))
