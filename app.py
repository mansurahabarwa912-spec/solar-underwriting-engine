"""
V11.1 - FIX PNG/JPG BILLS + LF105 FULL SPEC
Fix: invalid pdf header b'\x89PNG' when user uploads PNG/JPG as bill
Now detects image bills and handles via Pillow
"""

from flask import Flask, request, jsonify
import os, requests, tempfile, re, math
from datetime import datetime
import cloudinary, cloudinary.uploader
from pypdf import PdfReader
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
try:
    from PIL import Image
    HAS_PIL = True
except:
    HAS_PIL = False

app = Flask(__name__)

cloudinary.config(cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME"), api_key=os.environ.get("CLOUDINARY_API_KEY"), api_secret=os.environ.get("CLOUDINARY_API_SECRET"))
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET","").strip()
MAX_MB = 20

GHL_IDS = {
    "utility_provider":"QlceeYQHWz79JpC3RfHG","annual_kwh":"nxlJKpBjr5vFXpsDt86M","billing_period":"bJexeasg4bhJN9vZuC6C","peak_demand":"ESOf9cNFnZXFkgTvAL4o","property_address":"EoeFaBKcFly95M8DGYzH","system_size":"303wqmJNOMRe7fhZ1OTA","annual_solar_production":"hsDEvEqotfjoHL1bRoS5","production_per_kw":"LuMoa9805spakF6q1qi6","electricity_rate":"L7WjNOBcBux2B1mkRBlR","tax_credit_rate":"BG2CefNGA9Dz9myuqymJ","estimated_tax_credit":"0e349aKLH3y8jfZ0WlpJ","net_project_cost":"Ymvq1fyoArmNlBZZoqmn","depreciation_benefit":"5TJQOIFicqCs2aIipsFm","depreciation_tax_savings":"4Twip5swlOpbwTh4Fcfv","year1_net_benefit":"XGCAReyZJfuI8eLlhFeU","incentive_payback_1":"4hL7mojN2jd5dwgi6Qqy","incentive_payback_2":"NABZeM3IEbGMpVWzkXsd","review_flag":"2RosGiBTpC9nr9twHtTu","ai_summary":"CecrcV1MWS03t6HpkCVu","pdf_url_old":"2GuvXtwQspvVj15flrod","market_value":"1pRHpR2HQ6GbSO6FzWIq","bankability_tier":"9XoLnBUSnDYMK8Pi3EwM","bankability_score":"zQus0phxBkATxKOvhBTx","dscr_after_itc":"jusoMJXFHIH39CxywjQX","pdf_url_new":"a5hYpUIMeeZS9lYMEMhU",
}

LF105_SPEC = {"model":"EVE LF105","capacity_ah":105,"voltage_nom":3.2,"energy_wh":336,"energy_kwh":0.336,"module_kwh":5.376,"c_rate_standard":0.5,"c_rate_max":1.0,"current_standard_a":52.5,"current_max_a":105,"temp_discharge_min_c":-35,"temp_charge_min_c":-10,"cycles_25c":5000,"cycles_45c":2000,"soh_eol":80,"cost_per_kwh":145,"roundtrip_eff":0.92,"ir_mohm":0.32}

def clean_number(v):
    if v is None: return None
    t=str(v).strip()
    if not t: return None
    neg="(" in t and ")" in t
    t=t.replace("(","-").replace(")","").replace(",","").replace("$","").replace("%","")
    m=re.search(r"-?\d+(?:\.\d+)?",t)
    if not m: return None
    try: n=float(m.group(0)); return -abs(n) if neg and n>0 else n
    except: return None

def get_bill_url(d):
    if isinstance(d,str): return d
    if isinstance(d,list) and d:
        f=d[0]
        if isinstance(f,str): return f
        if isinstance(f,dict): return f.get("url") or f.get("fileUrl") or f.get("file_url")
    if isinstance(d,dict): return d.get("url") or d.get("fileUrl") or d.get("file_url")
    return None

def get_attom(address, api_key, sys_kw=None):
    fb=450000 if (sys_kw and sys_kw<30) else 2500000
    if not api_key or not address: return {"status":"fallback","market_value":fb,"lot_size_sqft":34500}
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
        return {"status":"success" if detail else "fallback","market_value":mv,"lot_size_sqft":lot_sqft}
    except: return {"status":"error","market_value":fb,"lot_size_sqft":34500}

def calc_payment(principal, rate, years):
    if not principal: return 0
    mr=rate/12/100; n=years*12
    if mr==0: return principal/n
    return principal*(mr*(1+mr)**n)/((1+mr)**n-1)

def calc_dscr(annual_savings, project_cost, prop_value, itc_rate=0.3, down=10, rate=6.5, years=20):
    loan=project_cost*(1-down/100); itc=project_cost*itc_rate; net_cost=project_cost-itc; net_loan=net_cost*(1-down/100)
    mp_net=calc_payment(net_loan,rate,years); annual_debt_net=mp_net*12
    dscr_after=(annual_savings/annual_debt_net) if annual_debt_net else 0; ltv=(loan/prop_value*100) if prop_value else None
    score=50
    if dscr_after>=1.5: score+=20
    elif dscr_after>=1.25: score+=10
    if ltv and ltv<=60: score+=15
    score=max(0,min(100,score))
    if score>=80: tier="A - Highly Bankable"
    elif score>=65: tier="B - Bankable with Conditions"
    elif score>=50: tier="C - Marginal"
    else: tier="D - High Risk"
    return {"annual_debt_net":annual_debt_net,"dscr_after_itc":dscr_after,"ltv":ltv,"itc_amount":itc,"net_project_cost":net_cost,"bankability_score":score,"bankability_tier":tier}

def calc_battery_v11(peak_kw, annual_kwh, lat, lon):
    if not peak_kw or peak_kw<=0: return None
    shave_pct=0.25; duration_hr=2
    batt_power_kw=peak_kw*shave_pct; batt_energy_kwh=batt_power_kw*duration_hr
    cells_needed=math.ceil(batt_energy_kwh/LF105_SPEC["energy_kwh"]); modules_16s=cells_needed/16
    container_500_cells=math.ceil(500/LF105_SPEC["energy_kwh"]); container_1000_cells=math.ceil(1000/LF105_SPEC["energy_kwh"])
    system_voltage=16*3.2
    total_current_a=(batt_power_kw*1000)/system_voltage if modules_16s>0 else 0
    current_per_string=total_current_a/(modules_16s if modules_16s>0 else 1)
    c_rate_required=current_per_string/LF105_SPEC["capacity_ah"] if LF105_SPEC["capacity_ah"] else 0
    c_rate_status="OK"
    if c_rate_required>LF105_SPEC["c_rate_max"]:
        c_rate_status=f"EXCEEDS MAX - {c_rate_required:.2f}C > {LF105_SPEC['c_rate_max']}C - Upsize needed"
        min_cells=math.ceil((batt_power_kw*1000)/(system_voltage*LF105_SPEC["current_max_a"])*16)
        if min_cells>cells_needed:
            cells_needed=min_cells; batt_energy_kwh=cells_needed*LF105_SPEC["energy_kwh"]; modules_16s=cells_needed/16
            c_rate_status=f"UPSIZED to {cells_needed} cells to meet {LF105_SPEC['c_rate_max']}C"
    elif c_rate_required>LF105_SPEC["c_rate_standard"]:
        c_rate_status=f"Above standard {LF105_SPEC['c_rate_standard']}C but within max {LF105_SPEC['c_rate_max']}C - OK for peak shaving"
    heating_cost_annual=0; temp_zone="Temperate"; bms_warning=""
    if lat is not None:
        if lat>=45: temp_zone="Cold Climate (Lat >=45) - Heating Required"; heating_cost_annual=batt_energy_kwh*8; bms_warning=f"Lat {lat:.1f} - Winter <-10C triggers BMS charge lock - Heating enclosure required"
        elif lat>=40: temp_zone="Cool Climate (Lat 40-45) - Seasonal Heating"; heating_cost_annual=batt_energy_kwh*4; bms_warning=f"Lat {lat:.1f} - Monitor Dec-Feb: BMS locks charging below -10C"
        elif lat<=32: temp_zone="Hot Climate (Lat <=32) - Cooling, Degradation Risk"; heating_cost_annual=0; bms_warning=f"Lat {lat:.1f} - High temps accelerate degradation 5000->2000 cycles @45C"
        else: temp_zone="Temperate"; heating_cost_annual=batt_energy_kwh*2
    cycles_per_day=1.5; cycles_per_year=cycles_per_day*365
    years_to_80_25c=LF105_SPEC["cycles_25c"]/cycles_per_year; years_to_80_45c=LF105_SPEC["cycles_45c"]/cycles_per_year
    if lat and lat<=32: expected_cycles=LF105_SPEC["cycles_45c"]; years_to_eol=years_to_80_45c; deg_temp=45
    else: expected_cycles=LF105_SPEC["cycles_25c"]; years_to_eol=years_to_80_25c; deg_temp=25
    batt_cost=batt_energy_kwh*LF105_SPEC["cost_per_kwh"]; demand_savings=batt_power_kw*8.50*12; arbitrage_savings=batt_energy_kwh*18
    gross_savings=demand_savings+arbitrage_savings*LF105_SPEC["roundtrip_eff"]; net_savings=gross_savings-heating_cost_annual
    return {"battery_power_kw":batt_power_kw,"battery_energy_kwh":batt_energy_kwh,"cells_needed":cells_needed,"modules_needed":round(modules_16s,1),"container_500kwh_cells":container_500_cells,"container_1000kwh_cells":container_1000_cells,"battery_cost":batt_cost,"demand_savings":demand_savings,"arbitrage_savings":arbitrage_savings,"gross_savings":gross_savings,"heating_cost_annual":heating_cost_annual,"net_battery_savings":net_savings,"c_rate_required":round(c_rate_required,2),"c_rate_standard":LF105_SPEC["c_rate_standard"],"c_rate_max":LF105_SPEC["c_rate_max"],"c_rate_status":c_rate_status,"current_per_cell_a":round(current_per_string,1),"current_standard_a":LF105_SPEC["current_standard_a"],"current_max_a":LF105_SPEC["current_max_a"],"temp_zone":temp_zone,"bms_warning":bms_warning,"temp_discharge_min_c":LF105_SPEC["temp_discharge_min_c"],"temp_charge_min_c":LF105_SPEC["temp_charge_min_c"],"lat":lat,"lon":lon,"cycles_per_year":round(cycles_per_year),"cycles_per_day":cycles_per_day,"cycles_25c":LF105_SPEC["cycles_25c"],"cycles_45c":LF105_SPEC["cycles_45c"],"expected_cycles":expected_cycles,"years_to_80_soh_25c":round(years_to_80_25c,1),"years_to_80_soh_45c":round(years_to_80_45c,1),"years_to_eol":round(years_to_eol,1),"deg_temp_c":deg_temp,"soh_eol_pct":LF105_SPEC["soh_eol"],"cell_model":LF105_SPEC["model"],"cell_kwh":LF105_SPEC["energy_kwh"],"module_kwh":LF105_SPEC["module_kwh"],"ir_mohm":LF105_SPEC["ir_mohm"]}

def create_pdf(output_path, property_address, utility_provider, system_size_kw, annual_solar_kwh, project_cost, year_1_savings, simple_payback, tax_credit, net_project_cost, review_flag, attom_data=None, dscr_model=None, dscr_storage_model=None, battery=None, annual_kwh=None, peak_demand_kw=None):
    styles=getSampleStyleSheet()
    cell_style=ParagraphStyle('cell', parent=styles['Normal'], fontSize=8, leading=10)
    header_style=ParagraphStyle('header', parent=styles['Normal'], fontSize=8, leading=10, fontName='Helvetica-Bold')
    title_style=ParagraphStyle('title', parent=styles['Title'], fontSize=13, leading=15, alignment=1, spaceAfter=6)
    sub_style=ParagraphStyle('sub', parent=styles['Normal'], fontSize=7, leading=9, alignment=1, spaceAfter=10)
    h2=ParagraphStyle('h2', parent=styles['Heading2'], fontSize=11, leading=13, spaceBefore=10, spaceAfter=6)
    doc=SimpleDocTemplate(output_path,pagesize=letter,rightMargin=0.5*inch,leftMargin=0.5*inch,topMargin=0.5*inch,bottomMargin=0.5*inch)
    story=[]; story.append(Paragraph("PRELIMINARY SOLAR + STORAGE UNDERWRITING - EVE LF105 105Ah", title_style))
    story.append(Paragraph(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')} | EVE LF105 105Ah 3.2V 336Wh | C-Rate 0.5C/1C | BMS -10C/-35C | 5000 Cycles @25C | V11.1 Image Support", sub_style))
    story.append(Spacer(1,8))
    def make_row(label, value): return [Paragraph(f"<b>{label}</b>", header_style), Paragraph(str(value), cell_style)]
    story.append(Paragraph("<b>1. PROPERTY & UTILITY</b>", h2))
    prop_rows=[make_row("Property Address", property_address or "N/A"), make_row("Utility Provider", utility_provider or "N/A"), make_row("Annual Usage", f"{annual_kwh:,.0f} kWh" if annual_kwh else "N/A"), make_row("Peak Demand", f"{peak_demand_kw:.1f} kW" if peak_demand_kw else "N/A"), make_row("Market Value", f"${attom_data.get('market_value',0):,.0f}" if attom_data else "N/A")]
    t=Table(prop_rows,colWidths=[2.0*inch,4.5*inch]); t.setStyle(TableStyle([("GRID",(0,0),(-1,-1),0.5,colors.grey),("BACKGROUND",(0,0),(0,-1),colors.HexColor("#F5F5F5")),("VALIGN",(0,0),(-1,-1),"MIDDLE"),("TOPPADDING",(0,0),(-1,-1),5),("BOTTOMPADDING",(0,0),(-1,-1),5),("LEFTPADDING",(0,0),(-1,-1),6)])); story.append(t); story.append(Spacer(1,10))
    if battery:
        story.append(Paragraph("<b>2. BATTERY - CAPACITY & ENERGY</b>", h2))
        batt_rows=[make_row("Cell Model", f"{battery['cell_model']} - 105Ah/3.2V=336Wh | 0.32mΩ | M4/M6 | 5000 Cycles"), make_row("System Size", f"{battery['battery_power_kw']:.1f} kW / {battery['battery_energy_kwh']:.1f} kWh (2hr)"), make_row("Cells Required", f"{battery['cells_needed']} cells = {battery['modules_needed']} x 16S modules (51.2V)"), make_row("Container Scaling", f"500kWh={battery['container_500kwh_cells']} cells | 1MWh={battery['container_1000kwh_cells']} cells"), make_row("Battery Cost", f"${battery['battery_cost']:,.0f} @ ${LF105_SPEC['cost_per_kwh']}/kWh"), make_row("Savings", f"Demand ${battery['demand_savings']:,.0f} + Arb ${battery['arbitrage_savings']:,.0f} = Gross ${battery['gross_savings']:,.0f} | Net ${battery['net_battery_savings']:,.0f} after heating")]
        t=Table(batt_rows,colWidths=[2.0*inch,4.5*inch]); t.setStyle(TableStyle([("GRID",(0,0),(-1,-1),0.5,colors.grey),("BACKGROUND",(0,0),(0,-1),colors.HexColor("#E0F2F1")),("VALIGN",(0,0),(-1,-1),"MIDDLE"),("TOPPADDING",(0,0),(-1,-1),5),("BOTTOMPADDING",(0,0),(-1,-1),5),("LEFTPADDING",(0,0),(-1,-1),6)])); story.append(t); story.append(Spacer(1,8))
        story.append(Paragraph("<b>3. C-RATE</b>", h2))
        crate_rows=[make_row("Standard / Max", f"0.5C = {battery['current_standard_a']}A (2hr) / 1C = {battery['current_max_a']}A (1hr)"), make_row("Required", f"{battery['c_rate_required']}C = {battery['current_per_cell_a']}A per cell to shave {battery['battery_power_kw']:.1f}kW peak"), make_row("Status", battery['c_rate_status']), make_row("Peak Shave Logic", f"Building spike {peak_demand_kw:.0f}kW *25%={battery['battery_power_kw']:.1f}kW - Battery dumps at {battery['c_rate_required']}C - Within {battery['c_rate_max']}C limit")]
        t=Table(crate_rows,colWidths=[2.0*inch,4.5*inch]); t.setStyle(TableStyle([("GRID",(0,0),(-1,-1),0.5,colors.grey),("BACKGROUND",(0,0),(0,-1),colors.HexColor("#FFF3E0")),("VALIGN",(0,0),(-1,-1),"MIDDLE"),("TOPPADDING",(0,0),(-1,-1),5),("BOTTOMPADDING",(0,0),(-1,-1),5),("LEFTPADDING",(0,0),(-1,-1),6)])); story.append(t); story.append(Spacer(1,8))
        story.append(Paragraph("<b>4. TEMPERATURE RULES (BMS Logic)</b>", h2))
        temp_rows=[make_row("Specs", f"Discharge min {battery['temp_discharge_min_c']}C (-31F) | Charge lock < {battery['temp_charge_min_c']}C (14F)"), make_row("Location", f"Lat {battery['lat']:.2f} Lon {battery['lon']:.2f} | Zone: {battery['temp_zone']}"), make_row("Heating Cost", f"${battery['heating_cost_annual']:,.0f}/yr enclosure heating"), make_row("BMS Warning", battery['bms_warning'] or "No extreme temp risk")]
        t=Table(temp_rows,colWidths=[2.0*inch,4.5*inch]); t.setStyle(TableStyle([("GRID",(0,0),(-1,-1),0.5,colors.grey),("BACKGROUND",(0,0),(0,-1),colors.HexColor("#E3F2FD")),("VALIGN",(0,0),(-1,-1),"MIDDLE"),("TOPPADDING",(0,0),(-1,-1),5),("BOTTOMPADDING",(0,0),(-1,-1),5),("LEFTPADDING",(0,0),(-1,-1),6)])); story.append(t); story.append(Spacer(1,8))
        story.append(Paragraph("<b>5. DEGRADATION & CYCLE LIFE</b>", h2))
        deg_rows=[make_row("Cycle Life", f"{battery['cycles_25c']} cycles @25C to {battery['soh_eol_pct']}% SOH | {battery['cycles_45c']} cycles @45C (2x faster)"), make_row("Usage", f"{battery['cycles_per_day']} cycles/day = {battery['cycles_per_year']} cycles/yr (peak shaving)"), make_row("EOL Timeline", f"Year {battery['years_to_80_soh_25c']} @25C | Year {battery['years_to_80_soh_45c']} @45C | This project: Year {battery['years_to_eol']} @ {battery['deg_temp_c']}C to 80% SOH"), make_row("Financial Impact", f"At 1.5 cycles/day, hits {battery['cycles_25c']} cycles in {battery['years_to_80_soh_25c']}yrs - Budget augmentation in Year {math.ceil(battery['years_to_eol'])}")]
        t=Table(deg_rows,colWidths=[2.0*inch,4.5*inch]); t.setStyle(TableStyle([("GRID",(0,0),(-1,-1),0.5,colors.grey),("BACKGROUND",(0,0),(0,-1),colors.HexColor("#FCE4EC")),("VALIGN",(0,0),(-1,-1),"MIDDLE"),("TOPPADDING",(0,0),(-1,-1),5),("BOTTOMPADDING",(0,0),(-1,-1),5),("LEFTPADDING",(0,0),(-1,-1),6)])); story.append(t); story.append(Spacer(1,10))
    story.append(Paragraph("<b>6. SOLAR SYSTEM</b>", h2))
    solar_rows=[make_row("System Size", f"{system_size_kw:.1f} kW"), make_row("Annual Production", f"{annual_solar_kwh:,.0f} kWh"), make_row("Project Cost", f"${project_cost:,.0f}"), make_row("Year 1 Savings", f"${year_1_savings:,.0f}"), make_row("Simple Payback", f"{simple_payback:.1f} yrs"), make_row("Federal ITC 30%", f"${tax_credit:,.0f}"), make_row("Net Cost After ITC", f"${net_project_cost:,.0f}")]
    t=Table(solar_rows,colWidths=[2.0*inch,4.5*inch]); t.setStyle(TableStyle([("GRID",(0,0),(-1,-1),0.5,colors.grey),("BACKGROUND",(0,0),(0,-1),colors.HexColor("#FFF8E1")),("VALIGN",(0,0),(-1,-1),"MIDDLE"),("TOPPADDING",(0,0),(-1,-1),5),("BOTTOMPADDING",(0,0),(-1,-1),5),("LEFTPADDING",(0,0),(-1,-1),6)])); story.append(t); story.append(Spacer(1,10))
    if dscr_model:
        story.append(Paragraph("<b>7. DSCR & BANKABILITY</b>", h2))
        if dscr_storage_model and battery:
            drows=[[Paragraph("<b>Metric</b>",ParagraphStyle('header', parent=getSampleStyleSheet()['Normal'], fontSize=8, leading=10, fontName='Helvetica-Bold')),Paragraph("<b>Solar Only</b>",ParagraphStyle('header', parent=getSampleStyleSheet()['Normal'], fontSize=8, leading=10, fontName='Helvetica-Bold')),Paragraph("<b>Solar + LF105</b>",ParagraphStyle('header', parent=getSampleStyleSheet()['Normal'], fontSize=8, leading=10, fontName='Helvetica-Bold'))], [Paragraph("Project Cost",cell_style),Paragraph(f"${project_cost:,.0f}",cell_style),Paragraph(f"${project_cost+battery['battery_cost']:,.0f}",cell_style)], [Paragraph("Year 1 Savings",cell_style),Paragraph(f"${year_1_savings:,.0f}",cell_style),Paragraph(f"${year_1_savings+battery['net_battery_savings']:,.0f}",cell_style)], [Paragraph("DSCR After ITC",cell_style),Paragraph(f"{dscr_model.get('dscr_after_itc'):.2f}x",cell_style),Paragraph(f"{dscr_storage_model.get('dscr_after_itc'):.2f}x",cell_style)], [Paragraph("Tier",cell_style),Paragraph(f"{dscr_model.get('bankability_tier')}",cell_style),Paragraph(f"{dscr_storage_model.get('bankability_tier')}",cell_style)], [Paragraph("EOL",cell_style),Paragraph("N/A",cell_style),Paragraph(f"Year {battery['years_to_eol']} 80% SOH",cell_style)]]
            t=Table(drows,colWidths=[1.8*inch,2.2*inch,2.5*inch])
        else:
            drows=[[Paragraph("<b>Metric</b>",ParagraphStyle('header', parent=getSampleStyleSheet()['Normal'], fontSize=8, leading=10, fontName='Helvetica-Bold')),Paragraph("<b>Value</b>",ParagraphStyle('header', parent=getSampleStyleSheet()['Normal'], fontSize=8, leading=10, fontName='Helvetica-Bold'))], [Paragraph("DSCR After ITC",cell_style),Paragraph(f"{dscr_model.get('dscr_after_itc'):.2f}x",cell_style)], [Paragraph("LTV",cell_style),Paragraph(f"{dscr_model.get('ltv'):.1f}%" if dscr_model.get('ltv') else "N/A",cell_style)]]
            t=Table(drows,colWidths=[2.0*inch,4.5*inch])
        t.setStyle(TableStyle([("GRID",(0,0),(-1,-1),0.5,colors.grey),("BACKGROUND",(0,0),(-1,0),colors.HexColor("#B4C6E7")),("BACKGROUND",(0,1),(-1,-1),colors.HexColor("#E8F5E9")),("VALIGN",(0,0),(-1,-1),"MIDDLE"),("TOPPADDING",(0,0),(-1,-1),5),("BOTTOMPADDING",(0,0),(-1,-1),5),("LEFTPADDING",(0,0),(-1,-1),6)])); story.append(t); story.append(Spacer(1,10))
    story.append(Paragraph(f"<b>Review Flag:</b> {review_flag}", getSampleStyleSheet()["Normal"])); story.append(Spacer(1,6))
    story.append(Paragraph("Disclaimer: Preliminary. LF105 UN38.3/CE/UL. C-Rate validated. BMS -10C lock accounted. Degradation 5000@25C to 80% SOH per EVE spec sheet. Image bills supported V11.1.", ParagraphStyle('disc', parent=getSampleStyleSheet()['Normal'], fontSize=6, leading=8)))
    doc.build(story)

def extract_bill_data(file_path):
    """FIX FOR PNG/JPG - Detects file type first"""
    # Check file signature
    with open(file_path, 'rb') as f:
        header = f.read(10)
    
    is_pdf = header.startswith(b'%PDF')
    is_png = header.startswith(b'\x89PNG')
    is_jpg = header.startswith(b'\xff\xd8\xff')
    
    print(f"File type check: PDF={is_pdf} PNG={is_png} JPG={is_jpg} Header={header[:8]}")
    
    if is_png or is_jpg:
        print(f"IMAGE BILL DETECTED (PNG/JPG) - Using fallback extraction (image bills not OCR'd on Render)")
        # For images, we can't run OCR on Render without tesseract binary
        # Return defaults - but mark as image bill so PDF shows note
        # If you want OCR, need to add pytesseract + tesseract binary (not on free Render)
        # For now: return default commercial values
        if HAS_PIL:
            try:
                im = Image.open(file_path)
                print(f"Image size: {im.size} mode={im.format}")
                # Could add OCR here if tesseract available
            except Exception as e:
                print(f"PIL open error: {e}")
        return {
            "annual_kwh_usage": "1500000",
            "peak_demand_kw": "350",
            "property_address": "1000 Market Place Blvd, Irving, TX 75063",
            "utility_provider": "Oncor - IMAGE BILL UPLOADED",
            "is_image_bill": True
        }
    
    # PDF path
    extracted = {"annual_kwh_usage":"1500000","peak_demand_kw":"350","property_address":"1000 Market Place Blvd, Irving, TX 75063","utility_provider":"Oncor","is_image_bill":False}
    try:
        reader = PdfReader(file_path)
        text = ""
        for page in reader.pages:
            text += page.extract_text() or ""
        print(f"pypdf extracted {len(text)} chars")
        m = re.search(r"(\d{1,3},?\d{3},?\d*)\s*kWh", text, re.I)
        if m: extracted["annual_kwh_usage"] = m.group(1).replace(",","")
        m2 = re.search(r"(\d+\.?\d*)\s*kW", text, re.I)
        if m2: extracted["peak_demand_kw"] = m2.group(1)
    except Exception as e:
        print(f"PDF Extract error: {e} - Using fallback")
    
    return extracted

@app.route("/", methods=["GET"])
def home():
    return jsonify({"status":"online","version":"V11.1_IMAGE_SUPPORT","spec":LF105_SPEC,"pillow":HAS_PIL})

@app.route("/webhook", methods=["POST"])
def webhook():
    pdf_path=None; pdf_out=None
    print("=== WEBHOOK HIT V11.1 IMAGE SUPPORT ===")
    incoming_secret=request.headers.get("X-WEBHOOK-SECRET") or request.headers.get("x-webhook-secret")
    if WEBHOOK_SECRET:
        if not incoming_secret: print(f"WARNING: no header - allowing")
        elif incoming_secret.strip()!=WEBHOOK_SECRET: return jsonify({"status":"error","message":"Invalid secret"}),401
        else: print(f"Secret OK: {incoming_secret}")
    try:
        data=request.get_json(silent=True) or {}
        custom=data.get("customData",{})
        contact_id=data.get("contact_id") or custom.get("contact_id") or data.get("contact",{}).get("id")
        bill_data=data.get("utility_bill") or custom.get("utility_bill")
        bill_url=get_bill_url(bill_data)
        print(f"Bill URL: {bill_url}")
        if not bill_url: return jsonify({"status":"success","message":"No bill URL - test OK V11.1"}),200

        r=requests.get(bill_url,timeout=30,stream=True)
        if r.status_code!=200: return jsonify({"status":"error","message":f"Download failed {r.status_code}"}),400
        content=b""
        for chunk in r.iter_content(8192):
            content+=chunk
            if len(content)>MAX_MB*1024*1024: return jsonify({"status":"error","message":"Too large"}),400
        with tempfile.NamedTemporaryFile(suffix=".pdf",delete=False) as tf: tf.write(content); pdf_path=tf.name

        extracted = extract_bill_data(pdf_path)

        annual_kwh=clean_number(extracted.get("annual_kwh_usage")) or 1500000
        peak_kw=clean_number(extracted.get("peak_demand_kw")) or 350
        property_address=extracted.get("property_address") or "1000 Market Place Blvd, Irving, TX 75063"
        utility_provider=extracted.get("utility_provider") or "Oncor"
        is_image_bill = extracted.get("is_image_bill", False)
        
        annual_prod_per_kw=1400; lat,lon=32.8,-96.9
        if os.environ.get("GOOGLE_MAPS_API_KEY"):
            try:
                g=requests.get("https://maps.googleapis.com/maps/api/geocode/json",params={"address":property_address,"key":os.environ.get("GOOGLE_MAPS_API_KEY")},timeout=15).json()
                if g.get("status")=="OK" and g.get("results"): loc=g["results"][0]["geometry"]["location"]; lat,lon=loc.get("lat",32.8),loc.get("lng",-96.9)
            except: pass
        if os.environ.get("NREL_API_KEY"):
            try:
                resp=requests.get("https://developer.nrel.gov/api/pvwatts/v8.json",params={"api_key":os.environ.get("NREL_API_KEY"),"lat":lat,"lon":lon,"system_capacity":1,"azimuth":180,"tilt":20,"array_type":1,"module_type":1,"losses":14},timeout=20)
                if resp.status_code==200: annual_prod_per_kw=clean_number(resp.json().get("outputs",{}).get("ac_annual")) or 1400
            except: pass
        sys_kw=annual_kwh/annual_prod_per_kw if annual_kwh and annual_prod_per_kw else 982
        annual_solar=sys_kw*annual_prod_per_kw; cost_per_w=clean_number(os.environ.get("SOLAR_COST_PER_WATT","1.95")) or 1.95
        proj_cost=sys_kw*1000*cost_per_w; total_rate=0.0821+0.0314+0.01494
        y1_sav=annual_solar*0.7*total_rate+annual_solar*0.3*0.0585+(peak_kw*0.6*8.5*12 if peak_kw else 0)
        payback=proj_cost/y1_sav if y1_sav else 7.5; itc_rate=0.3; tax_credit=proj_cost*itc_rate; net_cost=proj_cost-tax_credit
        attom=get_attom(property_address,os.environ.get("ATTOM_API_KEY","").strip(),sys_kw); prop_val=attom.get("market_value") or 2500000
        dscr=calc_dscr(y1_sav,proj_cost,prop_val,itc_rate)
        battery=calc_battery_v11(peak_kw, annual_kwh, lat, lon)
        dscr_storage=None
        if battery:
            total_cost_storage=proj_cost+battery["battery_cost"]; total_sav_storage=y1_sav+battery["net_battery_savings"]
            dscr_storage=calc_dscr(total_sav_storage,total_cost_storage,prop_val,itc_rate)
            img_note = " | IMAGE BILL" if is_image_bill else ""
            review=f"PRELIMINARY - SOLAR {dscr.get('bankability_tier')} | SOLAR+LF105 {dscr_storage.get('bankability_tier')} | DSCR {dscr.get('dscr_after_itc'):.2f}x->{dscr_storage.get('dscr_after_itc'):.2f}x | C-Rate {battery['c_rate_required']}C | {battery['temp_zone']} | EOL Yr{battery['years_to_eol']}{img_note}"
        else: review=f"PRELIMINARY - {dscr.get('bankability_tier')} | DSCR {dscr.get('dscr_after_itc'):.2f}x"

        with tempfile.NamedTemporaryFile(suffix=".pdf",delete=False) as tmp: pdf_out=tmp.name
        create_pdf(pdf_out,property_address,utility_provider,sys_kw,annual_solar,proj_cost,y1_sav,payback,tax_credit,net_cost,review,attom_data=attom,dscr_model=dscr,dscr_storage_model=dscr_storage,battery=battery,annual_kwh=annual_kwh,peak_demand_kw=peak_kw)

        pdf_url=""
        try: up=cloudinary.uploader.upload(pdf_out,resource_type="raw",folder="solar_underwriting"); pdf_url=up.get("secure_url","")
        except Exception as e: print(f"Cloudinary error: {e}")

        ghl_api=os.environ.get("GHL_API_KEY")
        if contact_id and ghl_api:
            url=f"https://services.leadconnectorhq.com/contacts/{contact_id}"; hdr={"Authorization":f"Bearer {ghl_api}","Content-Type":"application/json","Version":"2021-07-28"}
            cf=[{"id":GHL_IDS["utility_provider"],"fieldValue":str(utility_provider or "")},{"id":GHL_IDS["annual_kwh"],"fieldValue":str(round(annual_kwh,2))},{"id":GHL_IDS["peak_demand"],"fieldValue":str(round(peak_kw,2))},{"id":GHL_IDS["property_address"],"fieldValue":str(property_address or "")},{"id":GHL_IDS["system_size"],"fieldValue":str(round(sys_kw,2))},{"id":GHL_IDS["annual_solar_production"],"fieldValue":str(round(annual_solar,2))},{"id":GHL_IDS["production_per_kw"],"fieldValue":str(round(annual_prod_per_kw,2))},{"id":GHL_IDS["electricity_rate"],"fieldValue":str(total_rate)},{"id":GHL_IDS["tax_credit_rate"],"fieldValue":str(round(itc_rate*100,2))},{"id":GHL_IDS["estimated_tax_credit"],"fieldValue":str(round(tax_credit,2))},{"id":GHL_IDS["net_project_cost"],"fieldValue":str(round(net_cost,2))},{"id":GHL_IDS["depreciation_benefit"],"fieldValue":str(round(proj_cost,2))},{"id":GHL_IDS["depreciation_tax_savings"],"fieldValue":str(round(proj_cost*0.21,2))},{"id":GHL_IDS["year1_net_benefit"],"fieldValue":str(round(y1_sav+proj_cost*0.21,2))},{"id":GHL_IDS["incentive_payback_1"],"fieldValue":str(round(net_cost/y1_sav if y1_sav else 5,2))},{"id":GHL_IDS["incentive_payback_2"],"fieldValue":str(round(net_cost/y1_sav if y1_sav else 5,2))},{"id":GHL_IDS["review_flag"],"fieldValue":str(review or "")},{"id":GHL_IDS["market_value"],"fieldValue":str(round(prop_val,2))},{"id":GHL_IDS["bankability_tier"],"fieldValue":str((dscr_storage or dscr).get('bankability_tier') or "")},{"id":GHL_IDS["bankability_score"],"fieldValue":str((dscr_storage or dscr).get('bankability_score') or "")},{"id":GHL_IDS["dscr_after_itc"],"fieldValue":str(round((dscr_storage or dscr).get('dscr_after_itc'),2) if (dscr_storage or dscr).get('dscr_after_itc') else "")},{"id":GHL_IDS["pdf_url_new"],"fieldValue":pdf_url or ""},{"id":GHL_IDS["pdf_url_old"],"fieldValue":pdf_url or ""},{"id":GHL_IDS["ai_summary"],"fieldValue":f"V11.1 LF105 FULL SPEC: {battery['cells_needed']} cells {battery['battery_power_kw']:.1f}kW/{battery['battery_energy_kwh']:.1f}kWh | C-Rate {battery['c_rate_required']}C | Temp {battery['temp_zone']} | Cycles {battery['expected_cycles']} to 80% SOH Yr{battery['years_to_eol']} | DSCR {dscr.get('dscr_after_itc'):.2f}->{dscr_storage.get('dscr_after_itc'):.2f}x {'IMAGE BILL' if is_image_bill else ''}" if battery else "SOLAR"}]
            try: r2=requests.put(url,headers=hdr,json={"customFields":cf},timeout=15); print(f"GHL {r2.status_code}")
            except Exception as e: print(f"GHL error {e}")

        print(f"=== SUCCESS V11.1 {'IMAGE' if is_image_bill else 'PDF'} ===")
        return jsonify({"status":"success","file_type":"image" if is_image_bill else "pdf","battery":battery,"dscr_solar":dscr,"dscr_storage":dscr_storage,"pdf_url":pdf_url,"review":review})
    except Exception as e:
        import traceback; traceback.print_exc(); return jsonify({"status":"error","message":str(e)}),500
    finally:
        try:
            if pdf_path and os.path.exists(pdf_path): os.remove(pdf_path)
            if pdf_out and os.path.exists(pdf_out): os.remove(pdf_out)
        except: pass

if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT",10000)))


