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

# --- CONFIGURATION (NEW INSTALLATIONS + OPEN PROJECTS) ---
WEB_QUERIES = [
    # 1. New installations and expansions
    '"visual inspection" AND "CCIT" pharma Europe news',
    'CDMO "parenteral" expansion Europe "Annex 1"',
    
    # 2. Open projects, revamping, and maintenance
    '"visual inspection machine" upgrade OR revamping pharmaceutical Europe',
    'pharma "inspection line" maintenance tender Europe',
    
    # 3. Compliance and safety issues (Hot leads for replacement)
    '"warning letter" particulate OR "sterility failure" pharmaceutical Europe',
    '"recalled" vials OR syringes inspection defect Europe'
]

def search_web_free(query):
    """Extracts news from the web by simulating an HTML search (Free)"""
    url = f"https://duckduckgo.com{query}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        return response.text[:3000]
    except Exception:
        return ""

def search_ted_tenders():
    """Queries the free public European TED API for notices from the last 7 days"""
    seven_days_ago = (datetime.now() - timedelta(days=7)).strftime('%Y%m%d')
    ted_query = f"publication-date >= {seven_days_ago} AND (FT ~ 'visual inspection' OR FT ~ 'CCIT' OR FT ~ 'leak test')"
    
    url = "https://europa.eu"
    payload = {
        "q": ted_query,
        "fields": ["notice-identifier", "title", "summary", "buyer", "place-of-performance"],
        "limit": 15
    }
    
    try:
        res = requests.post(url, json=payload, timeout=15)
        if res.status_code == 200:
            notices = res.json().get('notices', [])
            text_summary = ""
            for n in notices:
                title = n.get('title', {}).get('en', 'N/A')
                summary = n.get('summary', {}).get('en', 'N/A')
                buyer = n.get('buyer', {}).get('name', {}).get('en', 'N/A')
                text_summary += f"TED Tender - Buyer: {buyer} | Title: {title} | Details: {summary}\n\n"
            return text_summary
        return ""
    except Exception:
        return ""

def analyze_all_with_ai(combined_text, api_key):
    """Sends the aggregated raw data to Gemini using the current gemini-3.8-flash model"""
    try:
        client = genai.Client(api_key=api_key)
        
        prompt = f"""
        You are an expert B2B market analyst in the pharmaceutical packaging and inspection sector in Europe.
        Analyze the following text compiled from web searches and public European procurement tenders (TED).
        
        Identify pharmaceutical companies, CDMOs, or public healthcare entities in Europe that match one of these two scenarios:
        1. NEW INVESTMENT: Installing new sterile/parenteral filling lines that will require Automated Visual Inspection (AVI) and Container Closure Integrity Testing (CCIT) systems.
        2. OPEN PROJECT / UPGRADE: Operating existing lines that urgently need maintenance, machine upgrades, software revamping, or have faced regulatory compliance issues (e.g., Warning Letters, recalls due to particulate contamination or leakage defects).
        
        Discard generic components distributors, used machinery brokers, and minor academic laboratories. Focus strictly on industrial production sites in Europe.

        Raw data to analyze:
        {combined_text}
        
        Respond ONLY with a valid JSON array matching the structure below. If no leads are found, return an empty array []. Do not include any introductory or concluding text outside the JSON.
        [
          {{
            "Source_Channel": "Web Search" or "TED Tender",
            "Company_Or_Entity": "Name of the pharma company, CDMO, or public buyer",
            "Country": "European Country",
            "Project_Type": "New Investment" or "Open Project / Revamping",
            "Detected_Opportunity": "Detailed explanation of why this is a valid commercial lead",
            "Source_Context": "Specific snippet or context extracted from the source that proves the lead"
          }}
        ]
        """
        
        response = client.models.generate_content(
            model='gemini-3.8-flash',
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            ),
        )
        
        text_response = response.text
        
        match = re.search(r'\[.*\]', text_response, re.DOTALL)
        if match:
            clean_json = match.group(0)
        else:
            clean_json = text_response.strip()
            
        data_dicts = json.loads(clean_json)
        return pd.DataFrame(data_dicts)
        
    except Exception as e:
        print(f"Error during AI processing: {e}")
        return pd.DataFrame()

def send_email_with_excel(file_path, to_email, from_email, app_password):
    """Sends the generated Excel report via email using Gmail SMTP server"""
    msg = MIMEMultipart()
    msg['From'] = from_email
    msg['To'] = to_email
    msg['Subject'] = f"🎯 AI MARKET INTELLIGENCE: Weekly Lead Report - AVI & CCIT Europe ({datetime.now().strftime('%d-%m-%Y')})"
    
    body = (
        "Dear Team,\n\n"
        "Please find attached the weekly automated market intelligence report. "
        "The AI agent has scanned public European tenders (TED) and industry web news, "
        "filtering and qualifying commercial opportunities for Visual Inspection and CCIT systems.\n\n"
        "Best regards,\n"
        "Automated Lead Generation Agent"
    )
    msg.attach(MIMEText(body, 'plain'))
    
    with open(file_path, "rb") as attachment:
        part = MIMEBase('application', 'octet-stream')
        part.set_payload(attachment.read())
        encoders.encode_base64(part)
        part.add_header('Content-Disposition', f"attachment; filename= {os.path.basename(file_path)}")
        msg.attach(part)
        
    try:
        server = smtplib.SMTP('://gmail.com', 587)
        server.starttls()
        server.login(from_email, app_password)
        server.sendmail(from_email, to_email, msg.as_string())
        server.quit()
        print("Report email sent successfully!")
    except Exception as e:
        print(f"Error sending email: {e}")

if __name__ == "__main__":
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
    EMAIL_USER = os.getenv("EMAIL_USER", "").strip()       
    EMAIL_PASS = os.getenv("EMAIL_PASS", "").strip()       
    RECEIVER_EMAIL = os.getenv("RECEIVER_EMAIL", "").strip() 
    
    raw_data_compiled = ""
    
    print("Scanning TED procurement tenders...")
    ted_results = search_ted_tenders()
    raw_data_compiled += "--- EUROPEAN TED PROCUREMENT TENDERS ---\n" + ted_results + "\n"
    
    print("Scanning industry web news...")
    for q in WEB_QUERIES:
        raw_data_compiled += f"--- WEB RESULTS FOR QUERY: {q} ---\n" + search_web_free(q) + "\n"
        
    print("Processing data with AI...")
    df_leads = analyze_all_with_ai(raw_data_compiled, GEMINI_API_KEY)
    
    if df_leads is not None and not df_leads.empty:
        df_leads = df_leads.drop_duplicates(subset=['Company_Or_Entity'])
        file_name = "Weekly_Pharma_Leads_Report.xlsx"
        df_leads.to_excel(file_name, index=False)
        send_email_with_excel(file_name, RECEIVER_EMAIL, EMAIL_USER, EMAIL_PASS)
    else:
        print("No relevant leads identified this week. No email sent.")
