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

# --- CONFIGURAZIONE ---
WEB_QUERIES = [
    '"visual inspection" AND "CCIT" pharma Europe news',
    '"sterile filling line" investment pharmaceutical Europe',
    'CDMO "parenteral" expansion Europe "Annex 1"',
    '"HVLD" OR "vacuum decay" pharmaceutical line Europe'
]

def search_web_free(query):
    """Estrae notizie dal web simulando una ricerca in HTML (Gratis)"""
    url = f"https://duckduckgo.com{query}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        return response.text[:3000]
    except Exception:
        return ""

def search_ted_tenders():
    """Interroga l'API pubblica e gratuita del portale TED europeo per gli ultimi 7 giorni"""
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
                text_summary += f"Bando TED - Acquirente: {buyer} | Titolo: {title} | Dettagli: {summary}\n\n"
            return text_summary
        return ""
    except Exception:
        return ""

def analyze_all_with_ai(combined_text, api_key):
    """Invia il materiale a Gemini Flash isolando la chiave API dall'URL di testo"""
    # L'URL fisso garantisce che non si verifichino deformazioni di stringa sull'host
    url = "https://googleapis.com"
    
    # Passiamo la chiave come parametro di query strutturato
    query_params = {"key": api_key}
    
    prompt = f"""
    Sei un analista B2B esperto nel settore farmaceutico in Europa. Analizza il seguente testo proveniente da ricerche sul Web e bandi pubblici europei (TED).
    Identifica aziende farmaceutiche, CDMO o enti pubblici che richiedono o stanno per acquistare macchine di Ispezione Visiva Automatica (AVI) e sistemi di controllo integrità/leak test (CCIT, HVLD, Vacuum Decay).
    
    Scarta lo spam, i rivenditori di componenti usati e i laboratori accademici minori. Concentrati su siti di produzione e linee industriali in Europa.

    Testo da analizzare:
    {combined_text}
    
    Rispondi fornendo unicamente un array JSON con la struttura qui sotto. Se non trovi nulla restituisci []. Non aggiungere introduzioni.
    [
      {{
        "Canale": "Web" o "Bando TED",
        "Azienda_O_Ente": "Nome dell'azienda o ente",
        "Paese": "Nazione Europea",
        "Opportunita_Rilevata": "Spiegazione del motivo",
        "Dettaglio_Fonte": "Contesto trovato"
      }}
    ]
    """
    
    payload = {
        "contents": [{"parts": [{"text": prompt}]}]
    }
    
    try:
        # requests unisce i parametri in modo sicuro senza intaccare l'host primario
        res = requests.post(url, params=query_params, json=payload, timeout=25)
        
        if res.status_code != 200:
            print(f"Errore API Gemini (Stato {res.status_code}): {res.text}")
            return pd.DataFrame()
            
        res_json = res.json()
        
        if 'candidates' in res_json and len(res_json['candidates']) > 0:
            text_response = res_json['candidates']['content']['parts']['text']
        else:
            print("Gemini ha risposto ma la struttura dei dati non contiene testo valido.")
            return pd.DataFrame()
        
        match = re.search(r'\[.*\]', text_response, re.DOTALL)
        if match:
            clean_json = match.group(0)
        else:
            clean_json = text_response.strip()
            
        data_dicts = json.loads(clean_json)
        return pd.DataFrame(data_dicts)
        
    except Exception as e:
        print(f"Errore durante l'elaborazione dei dati dell'AI: {e}")
        return pd.DataFrame()

def send_email_with_excel(file_path, to_email, from_email, app_password):
    """Invia il file Excel generato via email usando il server SMTP di Gmail"""
    msg = MIMEMultipart()
    msg['From'] = from_email
    msg['To'] = to_email
    msg['Subject'] = f"🎯 AI REPORT: Lead Ispezione Visiva & CCIT Europa ({datetime.now().strftime('%d-%m-%Y')})"
    
    body = "In allegato trovi il report Excel settimanale aggiornato. L'AI ha scansionato il Web e i bandi pubblici europei (TED) filtrando le migliori opportunità."
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
        print("Email inviata con successo!")
    except Exception as e:
        print(f"Errore invio email: {e}")

if __name__ == "__main__":
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
    EMAIL_USER = os.getenv("EMAIL_USER", "").strip()       
    EMAIL_PASS = os.getenv("EMAIL_PASS", "").strip()       
    RECEIVER_EMAIL = os.getenv("RECEIVER_EMAIL", "").strip() 
    
    raw_data_compiled = ""
    
    print("Scansione bandi di gara TED...")
    ted_results = search_ted_tenders()
    raw_data_compiled += "--- RISULTATI APPALTI PUBBLICI TED EUROPEA ---\n" + ted_results + "\n"
    
    print("Scansione web generalista...")
    for q in WEB_QUERIES:
        raw_data_compiled += f"--- RISULTATI WEB PER QUERY: {q} ---\n" + search_web_free(q) + "\n"
        
    print("Analisi in corso con l'AI...")
    df_leads = analyze_all_with_ai(raw_data_compiled, GEMINI_API_KEY)
    
    if df_leads is not None and not df_leads.empty:
        df_leads = df_leads.drop_duplicates(subset=['Azienda_O_Ente'])
        file_name = "Report_Settimanale_Lead_Totale.xlsx"
        df_leads.to_excel(file_name, index=False)
        send_email_with_excel(file_name, RECEIVER_EMAIL, EMAIL_USER, EMAIL_PASS)
    else:
        print("Nessun lead rilevante trovato questa settimana. Nessuna email inviata.")
