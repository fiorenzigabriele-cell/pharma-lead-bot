import os
import smtplib
import json
import requests
import re
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
import pandas as pd
from google import genai
from google.genai import types

# ============================================================
# CONFIGURATION
# ============================================================

# Lead generation queries (Europe - aggressive)
LEAD_QUERIES = [
    # New investments & expansions
    '"visual inspection" OR AVI OR "automated visual inspection" (CDMO OR pharma) (expansion OR "new line" OR investment) Europe',
    'CDMO parenteral OR injectable (expansion OR "new facility" OR "new line") (Europe OR Germany OR France OR Italy OR Spain OR Switzerland)',
    '"filling line" (vial OR syringe OR ampoule) (inspection OR AVI OR CCIT) Europe',

    # Upgrades / revamping / problems
    '"visual inspection machine" (upgrade OR revamping OR replacement OR retrofit) pharmaceutical Europe',
    '("false reject" OR "high reject rate") (visual inspection OR AVI) pharma Europe',
    '"Annex 1" (visual inspection OR CCIT OR "leak test") (compliance OR upgrade) Europe',

    # Regulatory / quality signals
    '"warning letter" OR recall (particulate OR "container closure" OR leak OR sterility) pharmaceutical Europe',
    '"sterility failure" OR "particulate contamination" (vial OR syringe) Europe',
]

# Competitor intelligence queries
COMPETITOR_QUERIES = {
    "Körber": [
        'Körber (inspection OR AVI OR "visual inspection" OR CCIT OR "leak detection") (pharma OR pharmaceutical)',
        'Körber (Seidenader OR Mediseal) (inspection machine OR AVI)',
        '"Körber" ("visual inspection" OR AVI) (launch OR new OR contract OR installation)',
    ],
    "Stevanato": [
        'Stevanato (inspection OR AVI OR "visual inspection" OR CCIT) (pharma OR pharmaceutical)',
        'Stevanato Group (inspection machine OR "visual inspection" OR AVI)',
        '"Stevanato" ("visual inspection" OR AVI OR CCIT) (launch OR contract OR installation OR partnership)',
    ],
    "Brevetti": [
        'Brevetti CEA (inspection OR AVI OR "visual inspection") (pharma OR pharmaceutical)',
        '"Brevetti CEA" OR "Brevetti" (inspection machine OR AVI OR CCIT)',
        'Brevetti (visual inspection OR leak test) (installation OR contract OR new)',
    ],
}

def search_web(query: str) -> str:
    """Simple free web search via DuckDuckGo HTML"""
    try:
        url = f"https://html.duckduckgo.com/html/?q={requests.utils.quote(query)}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        response = requests.get(url, headers=headers, timeout=12)
        return response.text[:4000]
    except Exception as e:
        print(f"Search error for '{query}': {e}")
        return ""


def search_ted_tenders() -> str:
    """Search recent TED tenders related to inspection"""
    # Note: Official TED API requires registration. This is a simplified approach.
    # For production, consider using the official TED API or RSS feeds.
    seven_days_ago = (datetime.now() - timedelta(days=14)).strftime("%Y%m%d")
    queries = [
        f"visual inspection pharmaceutical site:ted.europa.eu",
        f"CCIT OR \"leak test\" OR \"container closure\" pharmaceutical tender Europe",
        f"\"inspection machine\" OR AVI pharmaceutical tender Europe",
    ]
    results = []
    for q in queries:
        results.append(search_web(q))
    return "\n\n".join(results)


def analyze_leads_with_ai(combined_text: str, api_key: str) -> pd.DataFrame:
    """Extract commercial leads from raw text"""
    try:
        client = genai.Client(api_key=api_key)

        prompt = f"""
You are a senior B2B sales intelligence analyst specialized in pharmaceutical Visual Inspection (AVI) and Container Closure Integrity Testing (CCIT) equipment in Europe.

Analyze the following raw text from web searches and public tenders.

Extract ONLY high-quality commercial opportunities for selling Automated Visual Inspection machines and/or CCIT systems.

Focus on:
1. NEW INVESTMENTS: companies or CDMOs installing new sterile/parenteral lines
2. UPGRADES / REVAMPING: companies needing to replace or upgrade existing inspection equipment
3. REGULATORY PRESSURE: companies with quality issues, recalls, warning letters related to particles, leaks, sterility

Ignore: distributors, used equipment brokers, pure academic labs, non-European companies (unless clear European project).

Return ONLY a valid JSON array. No other text.
[
  {{
    "Source_Channel": "Web Search" or "TED Tender",
    "Company_Or_Entity": "Name",
    "Country": "Country",
    "Project_Type": "New Investment" or "Upgrade/Revamping" or "Regulatory/Quality Signal",
    "Priority": "Hot" or "Warm" or "Early Signal",
    "Detected_Opportunity": "Clear explanation why this is a sales opportunity for Visual Inspection / CCIT",
    "Suggested_Action": "Concrete next step for a sales person",
    "Source_Context": "Short evidence snippet"
  }}
]

Raw data:
{combined_text[:28000]}
"""

        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )

        text_response = response.text
        match = re.search(r"\[.*\]", text_response, re.DOTALL)
        clean_json = match.group(0) if match else text_response.strip()
        data = json.loads(clean_json)
        return pd.DataFrame(data)

    except Exception as e:
        print(f"Error in lead analysis: {e}")
        return pd.DataFrame()


def analyze_competitors_with_ai(competitor_data: dict, api_key: str) -> pd.DataFrame:
    """Analyze competitor movements"""
    try:
        client = genai.Client(api_key=api_key)

        all_text = ""
        for name, texts in competitor_data.items():
            all_text += f"\n\n===== {name} =====\n" + "\n".join(texts)

        prompt = f"""
You are a competitive intelligence analyst for Syntegon (pharmaceutical Visual Inspection & CCIT equipment).

Analyze the following recent public information about these three competitors:
- Körber (including Seidenader)
- Stevanato Group
- Brevetti CEA

Extract actionable intelligence. For each relevant finding return an object with:

Return ONLY a valid JSON array:
[
  {{
    "Competitor": "Körber" or "Stevanato" or "Brevetti",
    "Type": "Product Launch" or "New Contract/Installation" or "Partnership" or "Messaging/Positioning" or "Weakness/Limitation" or "Other",
    "Summary": "Clear summary of the finding",
    "Implication_for_Syntegon": "What this means for Syntegon sales strategy",
    "Suggested_Response": "What Syntegon sales should do or say",
    "Source_Context": "Short evidence"
  }}
]

If nothing relevant is found for a competitor, skip it. Do not invent information.

Raw data:
{all_text[:28000]}
"""

        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )

        text_response = response.text
        match = re.search(r"\[.*\]", text_response, re.DOTALL)
        clean_json = match.group(0) if match else text_response.strip()
        data = json.loads(clean_json)
        return pd.DataFrame(data)

    except Exception as e:
        print(f"Error in competitor analysis: {e}")
        return pd.DataFrame()


def send_email_with_excel(file_path: str, to_email: str, from_email: str, app_password: str):
    """Send the Excel report via Gmail"""
    msg = MIMEMultipart()
    msg["From"] = from_email
    msg["To"] = to_email
    msg["Subject"] = f"Syntegon Intelligence Report – Leads + Competitors ({datetime.now().strftime('%d-%m-%Y')})"

    body = (
        "Ciao,\n\n"
        "In allegato il report settimanale di market & competitive intelligence.\n\n"
        "Contiene due fogli:\n"
        "1. Leads → opportunità commerciali (Visual Inspection & CCIT) in Europa\n"
        "2. Competitor Intelligence → movimenti di Körber, Stevanato e Brevetti\n\n"
        "Buon lavoro,\n"
        "Pharma Lead Bot"
    )
    msg.attach(MIMEText(body, "plain"))

    with open(file_path, "rb") as attachment:
        part = MIMEBase("application", "octet-stream")
        part.set_payload(attachment.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f"attachment; filename= {os.path.basename(file_path)}")
        msg.attach(part)

    try:
        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(from_email, app_password)
        server.sendmail(from_email, to_email, msg.as_string())
        server.quit()
        print("Email inviata con successo.")
    except Exception as e:
        print(f"Errore invio email: {e}")


if __name__ == "__main__":
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
    EMAIL_USER = os.getenv("EMAIL_USER", "").strip()
    EMAIL_PASS = os.getenv("EMAIL_PASS", "").strip()
    RECEIVER_EMAIL = os.getenv("RECEIVER_EMAIL", "").strip()

    if not GEMINI_API_KEY:
        print("ERRORE: GEMINI_API_KEY non impostata.")
        exit(1)

    # ---------------------------------------------------------
    # 1. LEAD GENERATION
    # ---------------------------------------------------------
    print("=== FASE 1: Ricerca Lead ===")
    raw_leads = "--- TED / PUBLIC TENDERS ---\n" + search_ted_tenders() + "\n\n"

    for q in LEAD_QUERIES:
        print(f"  Query: {q[:80]}...")
        raw_leads += f"--- QUERY: {q} ---\n{search_web(q)}\n\n"

    print("Analisi lead con AI...")
    df_leads = analyze_leads_with_ai(raw_leads, GEMINI_API_KEY)

    # ---------------------------------------------------------
    # 2. COMPETITOR INTELLIGENCE
    # ---------------------------------------------------------
    print("\n=== FASE 2: Competitor Intelligence ===")
    competitor_raw = {}
    for competitor, queries in COMPETITOR_QUERIES.items():
        print(f"  Analizzo {competitor}...")
        texts = []
        for q in queries:
            texts.append(search_web(q))
        competitor_raw[competitor] = texts

    df_competitors = analyze_competitors_with_ai(competitor_raw, GEMINI_API_KEY)

    # ---------------------------------------------------------
    # 3. SAVE & SEND
    # ---------------------------------------------------------
    file_name = f"Syntegon_Intelligence_Report_{datetime.now().strftime('%Y%m%d')}.xlsx"

    with pd.ExcelWriter(file_name, engine="openpyxl") as writer:
        if df_leads is not None and not df_leads.empty:
            df_leads = df_leads.drop_duplicates(subset=["Company_Or_Entity"], keep="first")
            df_leads.to_excel(writer, sheet_name="Leads", index=False)
            print(f"Lead trovati: {len(df_leads)}")
        else:
            pd.DataFrame({"Info": ["Nessun lead rilevante questa settimana"]}).to_excel(
                writer, sheet_name="Leads", index=False
            )
            print("Nessun lead trovato.")

        if df_competitors is not None and not df_competitors.empty:
            df_competitors.to_excel(writer, sheet_name="Competitor_Intelligence", index=False)
            print(f"Insight competitor: {len(df_competitors)}")
        else:
            pd.DataFrame({"Info": ["Nessun movimento competitor rilevante"]}).to_excel(
                writer, sheet_name="Competitor_Intelligence", index=False
            )
            print("Nessun insight competitor.")

    if EMAIL_USER and EMAIL_PASS and RECEIVER_EMAIL:
        send_email_with_excel(file_name, RECEIVER_EMAIL, EMAIL_USER, EMAIL_PASS)
    else:
        print(f"Report salvato localmente: {file_name}")
        print("Configura EMAIL_USER, EMAIL_PASS e RECEIVER_EMAIL per l'invio automatico.")