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

# Ricerche per canale. Il secondo valore e' la finestra temporale Google (tbs):
# qdr:w = ultima settimana, qdr:m = ultimo mese, qdr:y = ultimo anno
LEAD_GROUPS = {
    "News & Investimenti": ("qdr:m", [
        'CDMO expansion sterile fill finish Europe new facility',
        'pharma "new filling line" OR "fill-finish" OR "aseptic filling" investment Europe',
        'prefilled syringe OR autoinjector OR vial capacity expansion Europe pharma',
        'GLP-1 OR semaglutide OR tirzepatide fill finish capacity Europe',
        'biosimilar OR "cell and gene" OR ADC manufacturing facility Europe investment',
        'pharma plant groundbreaking OR inauguration OR "opens new" sterile injectables Europe',
        '"visual inspection" OR "automated inspection" pharma new equipment installation OR purchase',
    ]),
    "LinkedIn": ("qdr:m", [
        'site:linkedin.com/posts "visual inspection" pharma OR CDMO',
        'site:linkedin.com/posts "fill finish" OR "filling line" expansion Europe',
        'site:linkedin.com/posts AVI OR "automated visual inspection" OR "container closure integrity"',
        'site:linkedin.com/posts CDMO "new site" OR "new facility" OR investing sterile injectables',
        'site:linkedin.com/pulse "visual inspection" OR "Annex 1" pharma',
    ]),
    "Forum & Community": ("qdr:y", [
        'site:reddit.com "visual inspection" pharma OR AVI OR "false reject"',
        'site:reddit.com "fill finish" OR "inspection machine" pharma OR biotech',
        'site:pda.org "visual inspection" OR AVI OR CCIT',
        'site:ispe.org "visual inspection" OR "container closure" OR "Annex 1"',
        'site:researchgate.net OR site:quora.com "automated visual inspection" pharma',
        'forum AVI OR "visual inspection" particles parenteral problem OR challenge OR "false rejects"',
    ]),
    "Regolatorio & Qualita": ("qdr:y", [
        'FDA "warning letter" parenteral particulate OR sterility OR "container closure"',
        'EudraGMDP OR "GMP non-compliance" sterile injectable manufacturer',
        'recall "visible particles" OR "glass particles" OR particulate injectable Europe',
        'MHRA OR BfArM OR AIFA OR ANSM sterile injectables recall OR inspection finding',
        'Annex 1 "contamination control strategy" visual inspection OR CCIT upgrade pharma',
    ]),
    "Job & Hiring": ("qdr:m", [
        'site:linkedin.com/jobs "visual inspection" pharma OR AVI engineer',
        'hiring "visual inspection" OR "automated inspection" sterile manufacturing Europe QA engineer',
        'jobs "AVI" OR "visual inspection" validation OR qualification pharma Europe',
    ]),
    "Gare & Eventi": ("qdr:y", [
        'site:ted.europa.eu inspection machine pharmaceutical OR vials OR syringes',
        'CPHI OR Achema OR Pharmapack OR Interphex "visual inspection" AVI exhibitor OR presentation',
        'tender OR "request for proposal" visual inspection machine pharmaceutical',
    ]),
}

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
    'visual inspection OR AVI OR CCIT pharmaceutical',
    'CDMO expansion OR investment parenteral OR injectable Europe',
    'sterile fill finish new facility OR new line Europe',
    'pharma recall particulate OR sterility injectable Europe',
    'GLP-1 fill finish capacity expansion Europe',
    'Körber OR Seidenader inspection OR AVI',
    'Stevanato inspection OR AVI OR CCIT',
    'Brevetti CEA inspection OR AVI',
]


def search_serper(query: str, max_results: int = 10, tbs: str = "") -> str:
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
        if tbs:
            payload["tbs"] = tbs
        response = requests.post(url, headers=headers, json=payload, timeout=20)
        if not response.ok and tbs:
            print(f"    → Serper HTTP {response.status_code} con filtro tempo, riprovo senza")
            payload.pop("tbs", None)
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


def parse_json_array(text: str):
    """Estrae un array JSON; se la risposta e' troncata recupera gli oggetti completi."""
    text = (text or "").strip()
    match = re.search(r"\[.*\]", text, re.DOTALL)
    candidate = match.group(0) if match else text
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        start = candidate.find("[")
        end = candidate.rfind("}")
        if start != -1 and end > start:
            return json.loads(candidate[start:end + 1] + "]")
        raise


LEAD_PROMPT = """
You are a senior B2B sales intelligence analyst for Syntegon, selling pharmaceutical Automated Visual Inspection (AVI) machines and Container Closure Integrity Testing (CCIT) systems in Europe.

The raw data below comes from the channel: {channel}.

GOAL: maximize RECALL. We want every possible sales opportunity, including weak signals, rumors, indirect clues and unverified claims. A sales person will verify them later. Do NOT filter out an item just because it is uncertain; label the uncertainty in "Confidence".

Count as an opportunity anything suggesting a company may need inspection or CCIT equipment, for example:
- new or expanded sterile / parenteral / fill-finish sites, lines, capacity (vials, syringes, cartridges, autoinjectors, pens, GLP-1, biosimilars, ADC, cell & gene)
- upgrades, replacement, retrofit or revalidation of inspection equipment; high false-reject rates; manual inspection being automated
- quality / regulatory events: recalls, particulates, sterility, leaks, warning letters, GMP findings, Annex 1 compliance projects
- hiring for inspection, QA sterile, validation or engineering roles at a pharma site (investment signal)
- tenders, RFPs, conference talks, forum complaints, posts from people at pharma companies about inspection problems
- customers of competitors (Koerber/Seidenader, Stevanato, Brevetti CEA, Antares Vision, CMP) with ageing machines or issues
- M&A or ownership changes of pharma manufacturing sites

Rules:
- Use ONLY information present in the raw data. Never invent companies, facts or URLs.
- "Source_URL" must be copied from the raw data item the signal comes from.
- If the company is unclear, write "Unknown - to identify" and still include the item when the signal is meaningful.
- Prefer Europe, but include non-European signals as "Early Signal" if they involve groups with European plants.
- Return up to 30 items, most promising first.

Return ONLY a valid JSON array (no markdown):
[
  {{
    "Source_Channel": "{channel}",
    "Company_Or_Entity": "Name",
    "Country": "Country or Unknown",
    "Project_Type": "New Investment" or "Capacity Expansion" or "Upgrade/Revamping" or "Regulatory/Quality Signal" or "Hiring Signal" or "Tender/Event" or "Competitor Customer" or "Rumor/Early Signal",
    "Confidence": "Confirmed" or "Likely" or "Weak signal / Rumor",
    "Priority": "Hot" or "Warm" or "Early Signal",
    "Detected_Opportunity": "What is happening, in plain language",
    "Why_It_Matters": "Why this could mean AVI/CCIT demand",
    "Suggested_Action": "Concrete next step for sales",
    "Verification_Step": "How to verify this signal quickly",
    "Source_URL": "URL from the raw data",
    "Source_Context": "Short evidence"
  }}
]

If really nothing is related, return [].

Raw data:
{text}
"""

BATCH_CHARS = 28000


def split_batches(text: str, size: int = BATCH_CHARS):
    """Spezza il testo senza tagliare a meta' un risultato di ricerca."""
    blocks = text.split("\n\n---\n\n")
    batches, current = [], ""
    for block in blocks:
        if current and len(current) + len(block) > size:
            batches.append(current)
            current = ""
        current += block + "\n\n---\n\n"
    if current.strip():
        batches.append(current)
    return batches


def analyze_leads_with_ai(texts_by_channel: dict, api_key: str) -> pd.DataFrame:
    client = genai.Client(api_key=api_key)
    frames = []
    for channel, text in texts_by_channel.items():
        if len(text.strip()) < 200:
            print(f"  [{channel}] pochi dati, salto")
            continue
        for i, batch in enumerate(split_batches(text), start=1):
            print(f"  [{channel}] batch {i} ({len(batch)} car.)")
            try:
                response = generate_json(client, LEAD_PROMPT.format(channel=channel, text=batch))
                raw = response.text or ""
                print(f"    → Gemini risposta ({len(raw)} car.): {raw[:200]!r}")
                data = parse_json_array(raw)
                print(f"    → {len(data)} segnali estratti")
                if data:
                    frames.append(pd.DataFrame(data))
            except Exception as e:
                import traceback; traceback.print_exc()
                print(f"    → Errore analisi [{channel}] batch {i}: {e}")
            time.sleep(3)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    for col in ("Company_Or_Entity", "Project_Type", "Source_URL"):
        if col not in df.columns:
            df[col] = ""
    df = df.drop_duplicates(subset=["Company_Or_Entity", "Project_Type", "Source_URL"], keep="first")
    order = {"Hot": 0, "Warm": 1, "Early Signal": 2}
    if "Priority" in df.columns:
        df = df.sort_values(by="Priority", key=lambda c: c.map(order).fillna(3), kind="stable")
    return df.reset_index(drop=True)


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

    print("=== FASE 1: Ricerca Lead (360 gradi) ===")
    lead_texts = {}
    total_chars = 0

    for channel, (tbs, queries) in LEAD_GROUPS.items():
        print(f"\n--- {channel} ---")
        chunks = []
        for q in queries:
            print(f"  Query: {q[:75]}...")
            chunks.append(f"--- QUERY ({channel}): {q} ---\n{search_serper(q, tbs=tbs)}")
        lead_texts[channel] = "\n\n---\n\n".join(chunks)
        total_chars += len(lead_texts[channel])

    print("\n--- Google News ---")
    news_chunks = []
    for q in GOOGLE_NEWS_QUERIES:
        print(f"  News: {q[:70]}...")
        news_chunks.append(f"--- GOOGLE NEWS: {q} ---\n{search_google_news(q)}")
    lead_texts["Google News"] = "\n\n---\n\n".join(news_chunks)
    total_chars += len(lead_texts["Google News"])

    print(f"\nTotale testo raccolto: {total_chars} caratteri")
    if total_chars < 2000:
        print("ERRORE: quasi nessun dato dalle ricerche (Serper/Google News). Controlla i log sopra.")
        exit(1)
    print("Analisi lead con AI...")
    df_leads = analyze_leads_with_ai(lead_texts, GEMINI_API_KEY)

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
