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
        return response.text[:4000] 
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
        "limit": 20
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
    """Invia tutto il materiale a Gemini Flash forzando l'output in formato JSON pulito"""
    url = "https://googleapis.com"
    params = {"key": api_key}
    
    prompt = f"""
    Sei un analista B2B esperto nel settore farmaceutico in Europa. Analizza il seguente testo proveniente da ricerche sul Web e bandi pubblici europei (TED).
    Identifica aziende farmaceutiche, CDMO o enti pubblici che richiedono o stanno per acquistare macchine di Ispezione Visiva Automatica (AVI) e sistemi di controllo integrità/leak test (CCIT, HVLD, Vacuum Decay).
    
    Scarta lo spam, i rivenditori di componenti usati e i laboratori accademici minori. Concentrati su siti di produzione e linee industriali in Europa.

    Testo da analizzare:
    {combined_text}
    
    Restituisci una lista in formato JSON con la seguente struttura. Se non trovi lead validi, restituisci una lista vuota [].
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
    
    # Configuriamo la richiesta forzando Gemini a rispondere in JSON nativo
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json"
        }
    }
    
    try:
        res = requests.post(url, params=params, json=payload, timeout=20)
        res_json = res.json()
        
        # Estraiamo il testo della risposta
        text_response = res_json['candidates'][0]['content']['parts'][0]['text']
        
        # Pulizia di sicurezza avanzata con espressioni regolari per estrarre solo il blocco [ ... ]
        match = re.search(r'\[.*\]', text_response, re.DOTALL)
        if match:
            clean_json = match.group(0)
        else:
            clean_json = text_response.strip()
            
        # Carica il JSON in un dizionario Python prima di passarlo a Pandas
        data_dicts = json.loads(clean_json)
        return pd.DataFrame(data_dicts)
        
    except Exception as e:
        print(f"Errore analisi AI: {e}")
        # Se c'è un errore mostriamo anche cosa aveva risposto Gemini per debug
        if 'res_json' in locals():
            print(f"Risposta grezza parziale: {str(res_json)[:200]}")
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
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
    EMAIL_USER = os.getenv("EMAIL_USER")       
    EMAIL_PASS = os.getenv("EMAIL_PASS")       
    RECEIVER_EMAIL = os.getenv("RECEIVER_EMAIL") 
    
    raw_data_compiled = ""
    
    print("Scansione bandi di gara TED...")
    ted_results = search_ted_tenders()
    raw_data_compiled += "--- RISULTATI APPALTI PUBBLICI TED EUROPEA ---\n" + ted_results + "\n"
    
    print("Scansione web generalista...")
    for q in WEB_QUERIES:
        raw_data_compiled += f"--- RISULTATI WEB PER QUERY: {q} ---\n" + search_web_free(q) + "\n"
        
    print("Analisi in corso con l'AI...")
    df_leads = analyze_all_with_ai(raw_data_compiled, GEMINI_API_KEY)
    
    if not df_leads.empty:
        df_leads = df_leads.drop_duplicates(subset=['Azienda_O_Ente'])
        file_name = "Report_Settimanale_Lead_Totale.xlsx"
        df_leads.to_excel(file_name, index=False)
        send_email_with_excel(file_name, RECEIVER_EMAIL, EMAIL_USER, EMAIL_PASS)
    else:
        print("Nessun lead rilevante trovato questa settimana. Nessuna email inviata.")
