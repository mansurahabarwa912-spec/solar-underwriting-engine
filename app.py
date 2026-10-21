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
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.enums import TA_LEFT, TA_CENTER

app = Flask(__name__)
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY")) if os.environ.get("OPENAI_API_KEY") else None
cloudinary.config(
    cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME"),
    api_key=os.environ.get("CLOUDINARY_API_KEY"),
    api_secret=os.environ.get("CLOUDINARY_API_SECRET")
)

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "").strip()
MAX_BILL_SIZE_MB = 20

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

LF105 = {"cell_ah": 105, "cell_v_nom": 3.2, "cell_kwh": 0.336, "module_kwh": 5.376, "cost_per_kwh": 145, "roundtrip_eff": 0.92, "cycles": 5000, "ir_mohm": 0.32}

def clean_number(v):
    if v is None: return None
    t = str(v).strip()
    if not t: return None
    neg = "(" in t and ")" in t
    t = t.replace("(", "-").replace(")", "").replace(",", "").replace("$", "").replace("%", "")
    m = re.search(r"-?\d+(?:\.\d+)?", t)
    if not m: return None
    try:
        n = float(m.group(0)); return -abs(n) if neg and n>0 else n
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
    fb = 450000 if (sys_kw and sys_kw<30) else 2500000
    if not api_key or not address:
        return {"status":"fallback","market_value":fb,"lot_size_sqft":34500,"lot_size_acres":0.792,"avm_value":fb}
    try:
        headers={"apikey":api_key,"Accept":"application/json"}
        parts=[p.strip() for p in address.split(",")]; a1=parts[0]; a2=", ".join(parts[1:]) if len(parts)>1 else ""
        detail={}
        for ep in ["property/address","property/detail"]:
            try:
                r=requests.get(f"https://api.gateway.attomdata.com/propertyapi/v1.0.0/{ep}",headers=headers,params={"address1":a1,"address2":a2},timeout=15)
                if r.status_code==200 and r.json().get("property"):
                    detail=r.json()["property"][0]
                    if detail.get("assessment"): break
            except: continue
        assessment=detail.get("assessment",{}); lot=detail.get("lot",{})
        mv=None
        if assessment.get("market",{}).get("mktTtlValue"): mv=clean_number(assessment["market"]["mktTtlValue"])
        if not mv and assessment.get("assessed",{}).get("assdTtlValue"): mv=clean_number(assessment["assessed"]["assdTtlValue"])
        if not mv: mv=fb
        ls2=clean_number(lot.get("lotSize2")); lot_sqft=ls2 if ls2 and ls2>=100 else 34500
        return {"status":"success" if detail else "fallback","market_value":mv,"lot_size_sqft":lot_sqft,"lot_size_acres":lot_sqft/43560,"avm_value":mv}
    except: return {"status":"error","market_value":fb,"lot_size_sqft":34500,"lot_size_acres":0.792,"avm_value":fb}

def calc_payment(principal, rate, years):
    if not principal: return 0
    mr=rate/12/100; n=years*12
    if mr==0: return principal/n
    return principal*(mr*(1+mr)**n)/((1+mr)**n-1)

def calc_dscr(annual_savings, project_cost, prop_value, itc_rate=0.3, down=10, rate=6.5, years=20):
    loan=project_cost*(1-down/100); mp=calc_payment(loan,rate,years); annual_debt=mp*12
    itc=project_cost*itc_rate; net_cost=project_cost-itc; net_loan=net_cost*(1-down/100); mp_net=calc_payment(net_loan,rate,years); annual_debt_net=mp_net*12
    dscr_after=(annual_savings/annual_debt_net) if annual_debt_net else 0; ltv=(loan/prop_value*100) if prop_value else None
    score=50; risks=[]
    if dscr_after>=1.5: score+=20; risks.append(f"DSCR Excellent {dscr_after:.2f}x")
    elif dscr_after>=1.25: score+=10; risks.append(f"DSCR Good {dscr_after:.2f}x")
    else: risks.append(f"DSCR {dscr_after:.2f}x")
    if ltv and ltv<=60: score+=15
    score=max(0,min(100,score))
    if score>=80: tier,odds="A - Highly Bankable","85-95%"
    elif score>=65: tier,odds="B - Bankable with Conditions","65-85%"
    elif score>=50: tier,odds="C - Marginal","40-65%"
    else: tier,odds="D - High Risk","<40%"
    return {"loan_amount":loan,"annual_debt_net":annual_debt_net,"dscr_after_itc":dscr_after,"ltv":ltv,"itc_amount":itc,"net_project_cost":net_cost,"bankability_score":score,"bankability_tier":tier,"approval_odds":odds,"risk_factors":risks}

def calc_battery(peak_kw):
    if not peak_kw or peak_kw<=0: return None
    shave=0.25; duration=2; cost_kwh=145
    batt_power=peak_kw*shave; batt_energy=batt_power*duration
    cells=int(batt_energy/LF105["cell_kwh"]); modules=batt_energy/LF105["module_kwh"]
    batt_cost=batt_energy*cost_kwh; demand_sav=batt_power*8.50*12; arb_sav=batt_energy*18; net_sav=demand_sav+arb_sav*LF105["roundtrip_eff"]
    return {"battery_power_kw":batt_power,"battery_energy_kwh":batt_energy,"cells_needed":cells,"modules_needed":round(modules,1),"battery_cost":batt_cost,"demand_savings":demand_sav,"arbitrage_savings":arb_sav,"net_battery_savings":net_sav,"cell_model":"EVE LF105 105Ah","cell_kwh":LF105["cell_kwh"],"module_kwh":LF105["module_kwh"],"cycles":LF105["cycles"],"ir_mohm":LF105["ir_mohm"]}

# ===== FIXED PDF - NO BLUR =====
def create_pdf(output_path, property_address, utility_provider, system_size_kw, annual_solar_kwh, project_cost, year_1_savings, simple_payback, tax_credit, net_project_cost, review_flag, attom_data=None, dscr_model=None, dscr_storage_model=None, battery=None, annual_kwh=None, peak_demand_kw=None):
    styles=getSampleStyleSheet()
    cell_style=ParagraphStyle('cell', parent=styles['Normal'], fontSize=9, leading=12, alignment=TA_LEFT)
    header_style=ParagraphStyle('header', parent=styles['Normal'], fontSize=9, leading=12, fontName='Helvetica-Bold', alignment=TA_LEFT)
    title_style=ParagraphStyle('title', parent=styles['Title'], fontSize=14, leading=16, alignment=TA_CENTER, spaceAfter=6)
    sub_style=ParagraphStyle('sub', parent=styles['Normal'], fontSize=8, leading=10, alignment=TA_CENTER, spaceAfter=12)

    doc=SimpleDocTemplate(output_path,pagesize=letter,rightMargin=0.6*inch,leftMargin=0.6*inch,topMargin=0.6*inch,bottomMargin=0.6*inch)
    story=[]
    story.append(Paragraph("PRELIMINARY SOLAR + STORAGE UNDERWRITING - EVE LF105 105Ah", title_style))
    story.append(Paragraph(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')} | EVE Energy LF105 105Ah LFP Cell | M4/M6 Threaded | 5000 Cycles", sub_style))
    story.append(Spacer(1,12))

    def make_row(label, value):
        return [Paragraph(f"<b>{label}</b>", header_style), Paragraph(str(value), cell_style)]

    story.append(Paragraph("<b>1. PROPERTY & UTILITY</b>", styles["Heading2"]))
    prop_rows=[make_row("Property Address", property_address or "N/A"), make_row("Utility Provider", utility_provider or "N/A"), make_row("Annual Usage", f"{annual_kwh:,.0f} kWh" if annual_kwh else "N/A"), make_row("Peak Demand", f"{peak_demand_kw:.1f} kW" if peak_demand_kw else "N/A"), make_row("Market Value", f"${attom_data.get('market_value',0):,.0f}" if attom_data else "N/A"), make_row("Lot Size", f"{attom_data.get('lot_size_sqft',0):,.0f} sqft" if attom_data else "N/A")]
    t=Table(prop_rows,colWidths=[2.0*inch,4.5*inch])
    t.setStyle(TableStyle([("GRID",(0,0),(-1,-1),0.5,colors.grey),("BACKGROUND",(0,0),(0,-1),colors.HexColor("#F5F5F5")),("VALIGN",(0,0),(-1,-1),"MIDDLE"),("TOPPADDING",(0,0),(-1,-1),6),("BOTTOMPADDING",(0,0),(-1,-1),6),("LEFTPADDING",(0,0),(-1,-1),8)]))
    story.append(t); story.append(Spacer(1,16))

    if battery:
        story.append(Paragraph("<b>2. BATTERY STORAGE - EVE LF105 105Ah LFP Cell</b>", styles["Heading2"]))
        batt_rows=[make_row("Cell Model", f"{battery['cell_model']} - EVE Energy Stock 300014"), make_row("Cell Spec", f"105Ah / 3.2V / {battery['cell_kwh']}kWh / {battery['ir_mohm']}mΩ IR / 5000 Cycles"), make_row("System Size", f"{battery['battery_power_kw']:.1f} kW / {battery['battery_energy_kwh']:.1f} kWh (2hr)"), make_row("Cells Required", f"{battery['cells_needed']} cells = {battery['modules_needed']} x 16S modules"), make_row("Battery Cost", f"${battery['battery_cost']:,.0f} @ ${LF105['cost_per_kwh']}/kWh"), make_row("Demand Savings", f"${battery['demand_savings']:,.0f}/yr"), make_row("Net Battery Savings", f"${battery['net_battery_savings']:,.0f}/yr")]
        t=Table(batt_rows,colWidths=[2.0*inch,4.5*inch])
        t.setStyle(TableStyle([("GRID",(0,0),(-1,-1),0.5,colors.grey),("BACKGROUND",(0,0),(0,-1),colors.HexColor("#E0F2F1")),("VALIGN",(0,0),(-1,-1),"MIDDLE"),("TOPPADDING",(0,0),(-1,-1),6),("BOTTOMPADDING",(0,0),(-1,-1),6),("LEFTPADDING",(0,0),(-1,-1),8)]))
        story.append(t); story.append(Spacer(1,16))

    story.append(Paragraph("<b>3. SOLAR SYSTEM</b>", styles["Heading2"]))
    solar_rows=[make_row("System Size", f"{system_size_kw:.1f} kW"), make_row("Annual Production", f"{annual_solar_kwh:,.0f} kWh"), make_row("Project Cost", f"${project_cost:,.0f}"), make_row("Year 1 Savings", f"${year_1_savings:,.0f}"), make_row("Simple Payback", f"{simple_payback:.1f} yrs"), make_row("Federal ITC 30%", f"${tax_credit:,.0f}"), make_row("Net Cost After ITC", f"${net_project_cost:,.0f}")]
    t=Table(solar_rows,colWidths=[2.0*inch,4.5*inch])
    t.setStyle(TableStyle([("GRID",(0,0),(-1,-1),0.5,colors.grey),("BACKGROUND",(0,0),(0,-1),colors.HexColor("#FFF8E1")),("VALIGN",(0,0),(-1,-1),"MIDDLE"),("TOPPADDING",(0,0),(-1,-1),6),("BOTTOMPADDING",(0,0),(-1,-1),6),("LEFTPADDING",(0,0),(-1,-1),8)]))
    story.append(t); story.append(Spacer(1,16))

    if dscr_model:
        story.append(Paragraph("<b>4. DSCR & BANKABILITY</b>", styles["Heading2"]))
        if dscr_storage_model and battery:
            drows=[[Paragraph("<b>Metric</b>",header_style),Paragraph("<b>Solar Only</b>",header_style),Paragraph("<b>Solar + LF105</b>",header_style)], [Paragraph("Project Cost",cell_style),Paragraph(f"${project_cost:,.0f}",cell_style),Paragraph(f"${project_cost+battery['battery_cost']:,.0f}",cell_style)], [Paragraph("Year 1 Savings",cell_style),Paragraph(f"${year_1_savings:,.0f}",cell_style),Paragraph(f"${year_1_savings+battery['net_battery_savings']:,.0f}",cell_style)], [Paragraph("DSCR After ITC",cell_style),Paragraph(f"{dscr_model.get('dscr_after_itc'):.2f}x",cell_style),Paragraph(f"{dscr_storage_model.get('dscr_after_itc'):.2f}x",cell_style)], [Paragraph("Tier",cell_style),Paragraph(f"{dscr_model.get('bankability_tier')}",cell_style),Paragraph(f"{dscr_storage_model.get('bankability_tier')}",cell_style)]]
            t=Table(drows,colWidths=[1.8*inch,2.2*inch,2.5*inch])
        else:
            drows=[[Paragraph("<b>Metric</b>",header_style),Paragraph("<b>Value</b>",header_style)], [Paragraph("DSCR After ITC",cell_style),Paragraph(f"{dscr_model.get('dscr_after_itc'):.2f}x",cell_style)], [Paragraph("LTV",cell_style),Paragraph(f"{dscr_model.get('ltv'):.1f}%" if dscr_model.get('ltv') else "N/A",cell_style)], [Paragraph("Tier",cell_style),Paragraph(f"{dscr_model.get('bankability_tier')}",cell_style)]]
            t=Table(drows,colWidths=[2.0*inch,4.5*inch])
        t.setStyle(TableStyle([("GRID",(0,0),(-1,-1),0.5,colors.grey),("BACKGROUND",(0,0),(-1,0),colors.HexColor("#B4C6E7")),("BACKGROUND",(0,1),(-1,-1),colors.HexColor("#E8F5E9")),("VALIGN",(0,0),(-1,-1),"MIDDLE"),("TOPPADDING",(0,0),(-1,-1),6),("BOTTOMPADDING",(0,0),(-1,-1),6),("LEFTPADDING",(0,0),(-1,-1),8)]))
        story.append(t); story.append(Spacer(1,16))

    story.append(Paragraph(f"<b>Review Flag:</b> {review_flag}", styles["Normal"]))
    story.append(Spacer(1,10))
    story.append(Paragraph("Disclaimer: Preliminary only. Verify LF105 UN38.3/CE/UL.", ParagraphStyle('disc', parent=styles['Normal'], fontSize=7, leading=9)))
    doc.build(story)

@app.route("/", methods=["GET"])
def home():
    return jsonify({"status":"online","version":"V8_SHARP_PDF_LF105","message":"Fixed blurry PDF"})

@app.route("/webhook", methods=["POST"])
def webhook():
    pdf_path=None; pdf_out=None
    print("=== WEBHOOK HIT ===")
    incoming_secret=request.headers.get("X-WEBHOOK-SECRET") or request.headers.get("x-webhook-secret")
    if WEBHOOK_SECRET:
        if not incoming_secret: print(f"WARNING: WEBHOOK_SECRET set ({WEBHOOK_SECRET}) but no header - allowing")
        elif incoming_secret.strip()!=WEBHOOK_SECRET: print(f"401 mismatch"); return jsonify({"status":"error","message":"Invalid secret"}),401
        else: print(f"Secret OK: {incoming_secret}")
    try:
        data=request.get_json(silent=True) or {}
        # GHL sends flat keys, not just customData - handle both
        custom=data.get("customData",{})
        contact_id=data.get("contact_id") or custom.get("contact_id")
        bill_data=data.get("utility_bill") or custom.get("utility_bill")
        bill_url=get_bill_url(bill_data)
        print(f"Bill URL: {bill_url} Contact: {contact_id}")
        if not bill_url: return jsonify({"status":"success","message":"No bill URL - test OK"}),200
        r=requests.get(bill_url,timeout=30,stream=True)
        if r.status_code!=200: return jsonify({"status":"error","message":f"Download failed {r.status_code}"}),400
        content=b""
        for chunk in r.iter_content(8192):
            content+=chunk
            if len(content)>MAX_BILL_SIZE_MB*1024*1024: return jsonify({"status":"error","message":"Too large"}),400
        with tempfile.NamedTemporaryFile(suffix=".pdf",delete=False) as tf: tf.write(content); pdf_path=tf.name
        extracted={"annual_kwh_usage":"1500000","peak_demand_kw":"350","property_address":"1000 Market Place Blvd, Irving, TX 75063","utility_provider":"Oncor","billing_period":"Jan 2025"}
        try:
            import fitz
            doc=fitz.open(pdf_path); text=""
            for page in doc: text+=page.get_text()
            print(f"PDF text OK length {len(text)}")
            m=re.search(r"(\d{1,3},?\d{3},?\d*)\s*kWh",text,re.I)
            if m: extracted["annual_kwh_usage"]=m.group(1).replace(",","")
            m2=re.search(r"(\d+\.?\d*)\s*kW",text,re.I)
            if m2: extracted["peak_demand_kw"]=m2.group(1)
        except Exception as e: print(f"Text extract error (using fallback): {e}")

        annual_kwh=clean_number(extracted.get("annual_kwh_usage")) or 1500000
        peak_kw=clean_number(extracted.get("peak_demand_kw")) or 350
        property_address=extracted.get("property_address") or "1000 Market Place Blvd, Irving, TX 75063"
        utility_provider=extracted.get("utility_provider") or "Oncor"
        annual_prod_per_kw=1400; lat,lon=32.8,-96.9
        if os.environ.get("GOOGLE_MAPS_API_KEY"):
            try:
                g=requests.get("https://maps.googleapis.com/maps/api/geocode/json",params={"address":property_address,"key":os.environ.get("GOOGLE_MAPS_API_KEY")},timeout=15).json()
                if g.get("status")=="OK" and g.get("results"): loc=g["results"][0]["geometry"]["location"]; lat,lon=loc.get("lat",32.8),loc.get("lng",-96.9)
            except: pass
        if os.environ.get("NREL_API_KEY"):
            try:
                resp=requests.get("https://developer.nlr.gov/api/pvwatts/v8.json",params={"api_key":os.environ.get("NREL_API_KEY"),"lat":lat,"lon":lon,"system_capacity":1,"azimuth":180,"tilt":20,"array_type":1,"module_type":1,"losses":14},timeout=20)
                if resp.status_code==200: annual_prod_per_kw=clean_number(resp.json().get("outputs",{}).get("ac_annual")) or 1400
            except: pass
        sys_kw=annual_kwh/annual_prod_per_kw if annual_kwh and annual_prod_per_kw else 1121
        annual_solar=sys_kw*annual_prod_per_kw; cost_per_w=clean_number(os.environ.get("SOLAR_COST_PER_WATT","1.95")) or 1.95
        proj_cost=sys_kw*1000*cost_per_w; total_rate=0.0821+0.0314+0.01494
        y1_sav=annual_solar*0.7*total_rate+annual_solar*0.3*0.0585+(peak_kw*0.6*8.5*12 if peak_kw else 0)
        payback=proj_cost/y1_sav if y1_sav else 7.5; itc_rate=0.3; tax_credit=proj_cost*itc_rate; net_cost=proj_cost-tax_credit; dep_benefit=proj_cost; dep_tax_sav=dep_benefit*0.21; inc_payback=net_cost/y1_sav if y1_sav else 5; y1_net=y1_sav+dep_tax_sav
        attom=get_attom(property_address,os.environ.get("ATTOM_API_KEY","").strip(),sys_kw); prop_val=attom.get("market_value") or 2500000
        dscr=calc_dscr(y1_sav,proj_cost,prop_val,itc_rate); battery=calc_battery(peak_kw); dscr_storage=None
        if battery:
            total_cost_storage=proj_cost+battery["battery_cost"]; total_sav_storage=y1_sav+battery["net_battery_savings"]
            dscr_storage=calc_dscr(total_sav_storage,total_cost_storage,prop_val,itc_rate)
            review=f"PRELIMINARY - SOLAR {dscr.get('bankability_tier')} | SOLAR+LF105 {dscr_storage.get('bankability_tier')} | DSCR {dscr.get('dscr_after_itc'):.2f}x -> {dscr_storage.get('dscr_after_itc'):.2f}x"
        else: review=f"PRELIMINARY - {dscr.get('bankability_tier')} | DSCR {dscr.get('dscr_after_itc'):.2f}x"
        with tempfile.NamedTemporaryFile(suffix=".pdf",delete=False) as tmp: pdf_out=tmp.name
        create_pdf(pdf_out,property_address,utility_provider,sys_kw,annual_solar,proj_cost,y1_sav,payback,tax_credit,net_cost,review,attom_data=attom,dscr_model=dscr,dscr_storage_model=dscr_storage,battery=battery,annual_kwh=annual_kwh,peak_demand_kw=peak_kw)
        pdf_url=""
        try:
            up=cloudinary.uploader.upload(pdf_out,resource_type="raw",folder="solar_underwriting"); pdf_url=up.get("secure_url",""); print(f"PDF uploaded: {pdf_url}")
        except Exception as e: print(f"Cloudinary error: {e}")
        ghl_api=os.environ.get("GHL_API_KEY")
        if contact_id and ghl_api:
            url=f"https://services.leadconnectorhq.com/contacts/{contact_id}"; hdr={"Authorization":f"Bearer {ghl_api}","Content-Type":"application/json","Version":"2021-07-28"}
            cf=[{"id":GHL_IDS["utility_provider"],"fieldValue":str(utility_provider or "")},{"id":GHL_IDS["annual_kwh"],"fieldValue":str(round(annual_kwh,2))},{"id":GHL_IDS["peak_demand"],"fieldValue":str(round(peak_kw,2))},{"id":GHL_IDS["property_address"],"fieldValue":str(property_address or "")},{"id":GHL_IDS["system_size"],"fieldValue":str(round(sys_kw,2))},{"id":GHL_IDS["annual_solar_production"],"fieldValue":str(round(annual_solar,2))},{"id":GHL_IDS["production_per_kw"],"fieldValue":str(round(annual_prod_per_kw,2))},{"id":GHL_IDS["electricity_rate"],"fieldValue":str(total_rate)},{"id":GHL_IDS["tax_credit_rate"],"fieldValue":str(round(itc_rate*100,2))},{"id":GHL_IDS["estimated_tax_credit"],"fieldValue":str(round(tax_credit,2))},{"id":GHL_IDS["net_project_cost"],"fieldValue":str(round(net_cost,2))},{"id":GHL_IDS["depreciation_benefit"],"fieldValue":str(round(dep_benefit,2))},{"id":GHL_IDS["depreciation_tax_savings"],"fieldValue":str(round(dep_tax_sav,2))},{"id":GHL_IDS["year1_net_benefit"],"fieldValue":str(round(y1_net,2))},{"id":GHL_IDS["incentive_payback_1"],"fieldValue":str(round(inc_payback,2))},{"id":GHL_IDS["incentive_payback_2"],"fieldValue":str(round(inc_payback,2))},{"id":GHL_IDS["review_flag"],"fieldValue":str(review or "")},{"id":GHL_IDS["market_value"],"fieldValue":str(round(prop_val,2))},{"id":GHL_IDS["bankability_tier"],"fieldValue":str((dscr_storage or dscr).get('bankability_tier') or "")},{"id":GHL_IDS["bankability_score"],"fieldValue":str((dscr_storage or dscr).get('bankability_score') or "")},{"id":GHL_IDS["dscr_after_itc"],"fieldValue":str(round((dscr_storage or dscr).get('dscr_after_itc'),2) if (dscr_storage or dscr).get('dscr_after_itc') else "")},{"id":GHL_IDS["pdf_url_new"],"fieldValue":pdf_url or ""},{"id":GHL_IDS["pdf_url_old"],"fieldValue":pdf_url or ""},{"id":GHL_IDS["ai_summary"],"fieldValue":f"SOLAR+STORAGE LF105: Solar {round(sys_kw,2)}kW | Usage {round(annual_kwh,2)}kWh | Peak {round(peak_kw,1)}kW | Battery {battery['battery_power_kw']:.1f}kW/{battery['battery_energy_kwh']:.1f}kWh LF105 ({battery['cells_needed']} cells) | DSCR {round(dscr.get('dscr_after_itc'),2)}x -> {round(dscr_storage.get('dscr_after_itc'),2) if dscr_storage else 'N/A'}x | Tier {(dscr_storage or dscr).get('bankability_tier')}" if battery else f"SOLAR: {round(sys_kw,2)}kW | DSCR {round(dscr.get('dscr_after_itc'),2)}x"}]
            try: r2=requests.put(url,headers=hdr,json={"customFields":cf},timeout=15); print(f"GHL Update: {r2.status_code}")
            except Exception as e: print(f"GHL error: {e}")
        print("=== SUCCESS 200 ===")
        return jsonify({"status":"success","battery":battery,"dscr_solar":dscr,"dscr_storage":dscr_storage,"pdf_url":pdf_url,"review":review})
    except Exception as e:
        import traceback; traceback.print_exc(); print(f"ERROR: {e}"); return jsonify({"status":"error","message":str(e)}),500
    finally:
        try:
            if pdf_path and os.path.exists(pdf_path): os.remove(pdf_path)
            if pdf_out and os.path.exists(pdf_out): os.remove(pdf_out)
        except: pass

if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT",10000)))
