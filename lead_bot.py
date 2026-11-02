import os
import smtplib
import json
import requests
import re
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
import pandas as pd
from google import genai
from google.genai import types
import feedparser

import time

GEMINI_MODELS = ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite"]


def generate_json(client, prompt: str):
    """Chiama Gemini con retry (backoff) e modelli di riserva su 429/5xx."""
    last_err = None
    for model in GEMINI_MODELS:
        for attempt in range(1, 5):
            try:
                print(f"    → Gemini {model} (tentativo {attempt})")
                return client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(response_mime_type="application/json"),
                )
            except Exception as e:
                last_err = e
                msg = str(e)
                retryable = any(c in msg for c in ("503", "429", "500", "502", "504", "UNAVAILABLE", "RESOURCE_EXHAUSTED"))
                print(f"    → Gemini errore: {msg[:200]}")
                if not retryable:
                    raise
                time.sleep(min(60, 10 * 2 ** (attempt - 1)))
    raise last_err


# ============================================================
# CONFIGURATION - Query semplificate e stabili
# ============================================================

LEAD_QUERIES = [
    'visual inspection OR AVI CDMO expansion OR "new line" OR investment 2026',
    'CDMO "filling line" OR "inspection line" OR AVI Europe 2026',
    '"visual inspection machine" upgrade OR replacement OR revamping pharma 2026',
    'Annex 1 visual inspection OR CCIT OR "leak test" upgrade 2026',
    '"warning letter" OR recall particulate OR sterility OR "container closure" pharma Europe 2026',
    'site:linkedin.com "visual inspection" OR AVI CDMO OR pharma 2026',
    'site:pda.org visual inspection OR AVI OR CCIT 2026',
    'site:reddit.com visual inspection OR AVI OR "false reject" pharma',
]

COMPETITOR_QUERIES = {
    "Körber": [
        'Körber OR Seidenader inspection OR AVI OR "visual inspection" 2026',
        'site:linkedin.com Körber OR Seidenader inspection OR AVI 2026',
        'Körber Seidenader contract OR installation OR launch 2026',
    ],
    "Stevanato": [
        'Stevanato inspection OR AVI OR CCIT 2026',
        'site:linkedin.com Stevanato inspection OR AVI 2026',
        'Stevanato "visual inspection" OR AVI contract OR installation 2026',
    ],
    "Brevetti": [
        '"Brevetti CEA" OR Brevetti inspection OR AVI 2026',
        'site:linkedin.com "Brevetti CEA" OR Brevetti inspection 2026',
        'Brevetti CEA contract OR installation OR launch 2026',
    ],
}

GOOGLE_NEWS_QUERIES = [
    'visual inspection OR AVI OR CCIT pharmaceutical 2026',
    'CDMO expansion OR "filling line" OR inspection 2026',
    'Körber OR Seidenader inspection OR AVI 2026',
    'Stevanato inspection OR AVI 2026',
    'Brevetti CEA inspection OR AVI 2026',
]


def search_serper(query: str, max_results: int = 10) -> str:
    api_key = os.getenv("SERPER_API_KEY", "").strip()
    if not api_key:
        print("    → SERPER_API_KEY non impostata")
        return ""

    try:
        url = "https://google.serper.dev/search"
        headers = {
            "X-API-KEY": api_key,
            "Content-Type": "application/json"
        }
        payload = {
            "q": query,
            "num": max_results,
            "hl": "en"
        }
        response = requests.post(url, headers=headers, json=payload, timeout=20)
        if not response.ok:
            print(f"    → Serper HTTP {response.status_code}: {response.text[:300]}")
            return ""
        data = response.json()

        results = []
        for item in data.get("organic", [])[:max_results]:
            title = item.get("title", "")
            link = item.get("link", "")
            snippet = item.get("snippet", "")
            results.append(f"Title: {title}\nURL: {link}\nSnippet: {snippet}")

        for item in data.get("news", [])[:5]:
            title = item.get("title", "")
            link = item.get("link", "")
            snippet = item.get("snippet", "")
            date = item.get("date", "")
            results.append(f"[NEWS] {title}\nDate: {date}\nURL: {link}\nSnippet: {snippet}")

        text = "\n\n---\n\n".join(results)
        print(f"    → Serper: {len(results)} risultati ({len(text)} caratteri)")
        return text
    except Exception as e:
        print(f"    → Serper error: {e}")
        return ""


def search_google_news(query: str, max_entries: int = 8) -> str:
    try:
        url = f"https://news.google.com/rss/search?q={requests.utils.quote(query)}&hl=en&gl=EU&ceid=EU:en"
        feed = feedparser.parse(url)
        results = []
        for entry in feed.entries[:max_entries]:
            title = entry.get("title", "")
            link = entry.get("link", "")
            published = entry.get("published", "")
            summary = entry.get("summary", "")[:300]
            results.append(f"Title: {title}\nDate: {published}\nURL: {link}\nSummary: {summary}")
        text = "\n\n---\n\n".join(results)
        print(f"    → Google News: {len(results)} articoli ({len(text)} caratteri)")
        return text
    except Exception as e:
        print(f"    → Google News error: {e}")
        return ""


def analyze_leads_with_ai(combined_text: str, api_key: str) -> pd.DataFrame:
    try:
        client = genai.Client(api_key=api_key)
        prompt = f"""
You are a senior B2B sales intelligence analyst specialized in pharmaceutical Visual Inspection (AVI) and Container Closure Integrity Testing (CCIT) equipment in Europe.

Analyze the following raw text from web searches and news (focused on 2026).

Extract commercial opportunities for selling Automated Visual Inspection machines and/or CCIT systems.
Include early signals, rumors and medium-quality opportunities.

Focus on:
1. NEW INVESTMENTS / new sterile or parenteral lines
2. UPGRADES / REVAMPING of inspection equipment
3. REGULATORY / QUALITY issues (recalls, warning letters, particulate, leak, sterility, Annex 1)
4. CAPACITY EXPANSIONS in Europe

Return ONLY a valid JSON array:
[
  {{
    "Source_Channel": "LinkedIn" or "PDA" or "News" or "Reddit" or "Web Search",
    "Company_Or_Entity": "Name",
    "Country": "Country",
    "Project_Type": "New Investment" or "Upgrade/Revamping" or "Regulatory/Quality Signal" or "Capacity Expansion" or "Rumor/Early Signal",
    "Priority": "Hot" or "Warm" or "Early Signal",
    "Detected_Opportunity": "Clear explanation",
    "Suggested_Action": "Concrete next step",
    "Source_Context": "Short evidence"
  }}
]

If nothing relevant, return [].

Raw data:
{combined_text[:35000]}
"""
        response = generate_json(client, prompt)
        text_response = response.text or ""
        print(f"    → Gemini risposta ({len(text_response)} car.): {text_response[:300]!r}")
        match = re.search(r"\[.*\]", text_response, re.DOTALL)
        clean_json = match.group(0) if match else text_response.strip()
        data = json.loads(clean_json)
        return pd.DataFrame(data)
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"Error in lead analysis: {e}")
        return pd.DataFrame()


def analyze_competitors_with_ai(competitor_data: dict, api_key: str) -> pd.DataFrame:
    try:
        client = genai.Client(api_key=api_key)
        all_text = ""
        for name, texts in competitor_data.items():
            all_text += f"\n\n===== {name} =====\n" + "\n".join(texts)

        prompt = f"""
You are a competitive intelligence analyst for Syntegon (pharmaceutical Visual Inspection & CCIT equipment).

Analyze recent public information about:
- Körber (including Seidenader)
- Stevanato Group
- Brevetti CEA

Extract actionable intelligence: new contracts, installations, product launches, partnerships, rumors.

Return ONLY a valid JSON array:
[
  {{
    "Competitor": "Körber" or "Stevanato" or "Brevetti",
    "Type": "Product Launch" or "New Contract/Installation" or "Partnership" or "Messaging/Positioning" or "Weakness/Limitation" or "Rumor" or "Other",
    "Summary": "Clear summary",
    "Implication_for_Syntegon": "What this means for Syntegon",
    "Suggested_Response": "What sales should do",
    "Source_Context": "Short evidence"
  }}
]

If nothing relevant, return [].

Raw data:
{all_text[:35000]}
"""
        response = generate_json(client, prompt)
        text_response = response.text or ""
        print(f"    → Gemini risposta ({len(text_response)} car.): {text_response[:300]!r}")
        match = re.search(r"\[.*\]", text_response, re.DOTALL)
        clean_json = match.group(0) if match else text_response.strip()
        data = json.loads(clean_json)
        return pd.DataFrame(data)
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"Error in competitor analysis: {e}")
        return pd.DataFrame()


def send_email_with_excel(file_path: str, to_email: str, from_email: str, app_password: str):
    msg = MIMEMultipart()
    msg["From"] = from_email
    msg["To"] = to_email
    msg["Subject"] = f"Syntegon Intelligence Report – Leads + Competitors ({datetime.now().strftime('%d-%m-%Y')})"
    body = (
        "Ciao,\n\n"
        "In allegato il report settimanale di market & competitive intelligence.\n\n"
        "Contiene due fogli:\n"
        "1. Leads\n"
        "2. Competitor Intelligence\n\n"
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
    SERPER_API_KEY = os.getenv("SERPER_API_KEY", "").strip()

    if not GEMINI_API_KEY:
        print("ERRORE: GEMINI_API_KEY non impostata.")
        exit(1)
    if not SERPER_API_KEY:
        print("ERRORE: SERPER_API_KEY non impostata.")
        exit(1)

    print("=== FASE 1: Ricerca Lead ===")
    raw_leads = ""

    print("\n--- Serper Search ---")
    for q in LEAD_QUERIES:
        print(f"  Query: {q[:75]}...")
        raw_leads += f"--- QUERY: {q} ---\n{search_serper(q)}\n\n"

    print("\n--- Google News ---")
    for q in GOOGLE_NEWS_QUERIES:
        print(f"  News: {q[:70]}...")
        raw_leads += f"--- GOOGLE NEWS: {q} ---\n{search_google_news(q)}\n\n"

    print(f"\nTotale testo raccolto: {len(raw_leads)} caratteri")
    if len(raw_leads) < 2000:
        print("ERRORE: quasi nessun dato dalle ricerche (Serper/Google News). Controlla i log sopra.")
        exit(1)
    print("Analisi lead con AI...")
    df_leads = analyze_leads_with_ai(raw_leads, GEMINI_API_KEY)

    print("\n=== FASE 2: Competitor Intelligence ===")
    competitor_raw = {}
    for competitor, queries in COMPETITOR_QUERIES.items():
        print(f"  Analizzo {competitor}...")
        texts = []
        for q in queries:
            texts.append(search_serper(q))
        texts.append(search_google_news(f"{competitor} inspection OR AVI 2026"))
        competitor_raw[competitor] = texts

    df_competitors = analyze_competitors_with_ai(competitor_raw, GEMINI_API_KEY)

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

    both_empty = (df_leads is None or df_leads.empty) and (df_competitors is None or df_competitors.empty)

    if EMAIL_USER and EMAIL_PASS and RECEIVER_EMAIL:
        send_email_with_excel(file_name, RECEIVER_EMAIL, EMAIL_USER, EMAIL_PASS)
    else:
        print(f"Report salvato localmente: {file_name}")

    if both_empty:
        print("ERRORE: analisi AI fallita su lead e competitor (vedi errori sopra).")
        exit(1)
