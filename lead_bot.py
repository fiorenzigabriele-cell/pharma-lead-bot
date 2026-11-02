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
    "Recall EU & Autorita": ("qdr:y", [
        'BfArM Rueckruf Parenteralia OR Injektionsloesung Partikel OR Glaspartikel',
        'AIFA ritiro lotto iniettabile OR fiale OR siringhe particelle OR sterilita',
        'ANSM rappel lot injectable particules OR defaut sterilite OR flacon',
        'MHRA drug alert injection recall particles OR "container closure" OR leak',
        'AEMPS OR HPRA OR Swissmedic recall injectable particles OR sterility OR vial defect',
        'EMA OR "Health Canada" OR TGA recall injectable "visible particles" OR "container defect"',
        'recall syringe OR vial OR cartridge "cracked" OR "crack" OR "leaking" OR "seal defect" pharma',
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
            date = normalize_date(item.get("date", ""))
            results.append(f"Title: {title}\nDate: {date or 'n/a'}\nURL: {link}\nSnippet: {snippet}")

        for item in data.get("news", [])[:5]:
            title = item.get("title", "")
            link = item.get("link", "")
            snippet = item.get("snippet", "")
            date = normalize_date(item.get("date", ""))
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
            published = normalize_date(entry.get("published", ""))
            summary = entry.get("summary", "")[:300]
            results.append(f"Title: {title}\nDate: {published}\nURL: {link}\nSummary: {summary}")
        text = "\n\n---\n\n".join(results)
        print(f"    → Google News: {len(results)} articoli ({len(text)} caratteri)")
        return text
    except Exception as e:
        print(f"    → Google News error: {e}")
        return ""


MASTER_FILE = "leads_master.xlsx"

FDA_RECALL_TERMS = ["particulate", "particles", "container closure", "sterility", "leak",
                    "crack", "glass", "foreign", "defective container", "visible"]
FDA_FORM_RE = re.compile(r"\b(injection|injectable|vial|vials|syringe|syringes|ampule|ampoule|"
                         r"cartridge|infusion|intravenous|parenteral|pen)\b", re.I)


def normalize_date(value) -> str:
    """Converte date relative ('3 days ago') o testuali in YYYY-MM-DD; altrimenti restituisce l'originale."""
    v = str(value or "").strip()
    if not v:
        return ""
    m = re.match(r"(\d+)\s*(minute|hour|day|week|month|year)s?\s+ago", v, re.I)
    if m:
        unit_days = {"minute": 0, "hour": 0, "day": 1, "week": 7, "month": 30, "year": 365}
        days = int(m.group(1)) * unit_days[m.group(2).lower()]
        return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        d = pd.to_datetime(v, errors="coerce", utc=True)
        if pd.notna(d):
            return d.strftime("%Y-%m-%d")
    except Exception:
        pass
    return v


def fetch_fda_recalls(days: int = 270) -> pd.DataFrame:
    """Recall FDA su iniettabili legati a particolato, container closure, sterilita', leak (openFDA)."""
    end = datetime.now()
    start = end - timedelta(days=days)
    rng = f"[{start:%Y%m%d}+TO+{end:%Y%m%d}]"
    rows = {}
    for term in FDA_RECALL_TERMS:
        term_q = term.replace(" ", "+")
        url = (f'https://api.fda.gov/drug/enforcement.json?search=reason_for_recall:"{term_q}"'
               f"+AND+report_date:{rng}&limit=100")
        try:
            r = requests.get(url, timeout=30)
            if r.status_code == 404:  # openFDA risponde 404 quando non ci sono risultati
                continue
            r.raise_for_status()
            for item in r.json().get("results", []):
                product = item.get("product_description", "") or ""
                reason = item.get("reason_for_recall", "") or ""
                if not FDA_FORM_RE.search(product + " " + reason):
                    continue
                key = item.get("recall_number") or f"{item.get('recalling_firm')}|{product[:40]}"
                rd = str(item.get("report_date", ""))
                rows[key] = {
                    "Recall_Date": f"{rd[:4]}-{rd[4:6]}-{rd[6:8]}" if len(rd) == 8 else rd,
                    "Firm": item.get("recalling_firm", ""),
                    "Product": product[:150],
                    "Reason": reason[:300],
                    "Classification": item.get("classification", ""),
                    "Status": item.get("status", ""),
                    "Country": item.get("country", ""),
                    "City": item.get("city", ""),
                    "Recall_Number": item.get("recall_number", ""),
                    "URL": f"https://www.accessdata.fda.gov/scripts/ires/index.cfm?Event={item.get('event_id', '')}",
                }
            print(f"    → openFDA '{term}': ok")
        except Exception as e:
            print(f"    → openFDA '{term}' errore: {e}")
    df = pd.DataFrame(list(rows.values()))
    if not df.empty:
        df = df.sort_values("Recall_Date", ascending=False).reset_index(drop=True)
    print(f"    → Recall FDA rilevanti: {len(df)}")
    return df


def recalls_to_text(df: pd.DataFrame, max_rows: int = 150) -> str:
    if df is None or df.empty:
        return ""
    blocks = []
    for _, r in df.head(max_rows).iterrows():
        blocks.append(
            f"Title: {r['Firm']} - {r['Product']}\nDate: {r['Recall_Date']}\nURL: {r['URL']}\n"
            f"Snippet: FDA class {r['Classification']} ({r['Status']}), country {r['Country']}. Reason: {r['Reason']}"
        )
    return "\n\n---\n\n".join(blocks)


PRIORITY_ORDER = {"Hot": 0, "Warm": 1, "Early Signal": 2}
PREFERRED_COLS = ["NEW", "First_Seen", "Publication_Date", "Age_Days", "Priority", "Confidence",
                  "Company_Or_Entity", "Country", "Project_Type", "Source_Channel",
                  "Detected_Opportunity", "Why_It_Matters", "Suggested_Action",
                  "Verification_Step", "Source_URL", "Source_Context", "Key"]


def lead_key(row) -> str:
    url = str(row.get("Source_URL", "") or "").strip()
    if url.startswith("http"):
        return url.split("#")[0].rstrip("/").lower()
    return "|".join(str(row.get(c, "") or "").strip().lower()[:60]
                    for c in ("Company_Or_Entity", "Project_Type", "Detected_Opportunity"))


def add_age(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    if "Publication_Date" not in df.columns:
        df["Publication_Date"] = ""
    df["Publication_Date"] = df["Publication_Date"].apply(normalize_date)
    dates = pd.to_datetime(df["Publication_Date"], errors="coerce")
    df["Age_Days"] = (pd.Timestamp(datetime.now().date()) - dates).dt.days
    return df


def sort_and_order(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["_n"] = (df.get("NEW", "") != "NEW").astype(int)
    df["_p"] = df["Priority"].map(PRIORITY_ORDER).fillna(3) if "Priority" in df.columns else 3
    df["_a"] = df["Age_Days"].fillna(99999) if "Age_Days" in df.columns else 99999
    df = df.sort_values(["_n", "_p", "_a"], kind="stable").drop(columns=["_n", "_p", "_a"])
    cols = [c for c in PREFERRED_COLS if c in df.columns] + [c for c in df.columns if c not in PREFERRED_COLS]
    return df[cols].reset_index(drop=True)


def update_master(df_new: pd.DataFrame, master_path: str = MASTER_FILE):
    """Unisce i lead di oggi allo storico. Ritorna (storico_completo, solo_nuovi)."""
    today = datetime.now().strftime("%Y-%m-%d")
    master = pd.DataFrame()
    if os.path.exists(master_path):
        try:
            master = pd.read_excel(master_path)
            print(f"  Storico caricato: {len(master)} lead")
        except Exception as e:
            print(f"  Storico non leggibile ({e}), riparto da zero")
    known = set(master["Key"].astype(str)) if "Key" in master.columns else set()

    new_rows = pd.DataFrame()
    if df_new is not None and not df_new.empty:
        df_new = df_new.copy()
        df_new["Key"] = df_new.apply(lead_key, axis=1)
        df_new = df_new.drop_duplicates(subset="Key", keep="first")
        new_rows = df_new[~df_new["Key"].isin(known)].copy()
        new_rows["First_Seen"] = today
        new_rows["NEW"] = "NEW"
    if not master.empty:
        master["NEW"] = ""

    parts = [d for d in (new_rows, master) if not d.empty]
    if not parts:
        return pd.DataFrame(), pd.DataFrame()
    all_df = add_age(pd.concat(parts, ignore_index=True))
    new_df = all_df[all_df["NEW"] == "NEW"].copy()
    return sort_and_order(all_df), sort_and_order(new_df) if not new_df.empty else pd.DataFrame()


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
    "Publication_Date": "YYYY-MM-DD copied from the 'Date:' field of the source item; 'Unknown' if absent. Never guess",
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


def send_email_with_excel(file_path: str, to_email: str, from_email: str, app_password: str, summary: str = ""):
    msg = MIMEMultipart()
    msg["From"] = from_email
    msg["To"] = to_email
    msg["Subject"] = f"Syntegon Intelligence Report – Leads + Competitors ({datetime.now().strftime('%d-%m-%Y')})"
    body = (
        "Ciao,\n\n"
        "In allegato il report settimanale di market & competitive intelligence.\n\n"
        f"{summary}\n\n"
        "Fogli: Leads_Nuovi, Storico_Leads, Recalls_FDA, Competitor_Intelligence\n\n"
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

    print("\n--- Recall FDA (openFDA) ---")
    df_recalls = fetch_fda_recalls()
    lead_texts["Recall FDA (openFDA)"] = recalls_to_text(df_recalls)
    total_chars += len(lead_texts["Recall FDA (openFDA)"])

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

    print("\n=== Storico lead ===")
    df_all, df_new = update_master(df_leads)

    with pd.ExcelWriter(file_name, engine="openpyxl") as writer:
        if not df_new.empty:
            df_new.to_excel(writer, sheet_name="Leads_Nuovi", index=False)
        else:
            pd.DataFrame({"Info": ["Nessun lead NUOVO questa settimana"]}).to_excel(
                writer, sheet_name="Leads_Nuovi", index=False)
        if not df_all.empty:
            df_all.to_excel(writer, sheet_name="Storico_Leads", index=False)
        else:
            pd.DataFrame({"Info": ["Nessun lead rilevante"]}).to_excel(
                writer, sheet_name="Storico_Leads", index=False)
        if df_recalls is not None and not df_recalls.empty:
            df_recalls.to_excel(writer, sheet_name="Recalls_FDA", index=False)
        else:
            pd.DataFrame({"Info": ["Nessun recall FDA rilevante"]}).to_excel(
                writer, sheet_name="Recalls_FDA", index=False)
        if df_competitors is not None and not df_competitors.empty:
            df_competitors.to_excel(writer, sheet_name="Competitor_Intelligence", index=False)
        else:
            pd.DataFrame({"Info": ["Nessun movimento competitor rilevante"]}).to_excel(
                writer, sheet_name="Competitor_Intelligence", index=False)

    if not df_all.empty:
        df_all.to_excel(MASTER_FILE, index=False)
    n_new, n_all = len(df_new), len(df_all)
    n_rec = 0 if df_recalls is None else len(df_recalls)
    print(f"Lead nuovi: {n_new} | storico: {n_all} | recall FDA: {n_rec}")
    summary = (f"Nuovi lead questa settimana: {n_new} (storico totale: {n_all}). "
               f"Recall FDA rilevanti trovati: {n_rec}.")

    both_empty = (df_leads is None or df_leads.empty) and (df_competitors is None or df_competitors.empty)

    if EMAIL_USER and EMAIL_PASS and RECEIVER_EMAIL:
        send_email_with_excel(file_name, RECEIVER_EMAIL, EMAIL_USER, EMAIL_PASS, summary)
    else:
        print(f"Report salvato localmente: {file_name}")

    if both_empty:
        print("ERRORE: analisi AI fallita su lead e competitor (vedi errori sopra).")
        exit(1)
