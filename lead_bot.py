import os
import smtplib
import json
import requests
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

# Codici CPV relativi a macchinari farmaceutici/industriali per stringere la ricerca su TED
# 42921300 (Macchine per l'imbottigliamento o il confezionamento), 33100000 (Dispositivi medici/laboratorio)
TED_CPV_CODES = ["42920000", "42921300", "38540000"] 

def search_web_free(query):
    """Estrae notizie dal web simulando una ricerca in HTML (Gratis)"""
    url = f"https://duckduckgo.com{query}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        return response.text[:4000] # Taglio per evitare di superare i limiti di contesto gratuiti
    except Exception:
        return ""

def search_ted_tenders():
    """Interroga l'API pubblica e gratuita del portale TED europeo per gli ultimi 7 giorni"""
    seven_days_ago = (datetime.now() - timedelta(days=7)).strftime('%Y%m%d')
    # Query esperta per l'API di TED v3
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
    """Invia tutto il materiale a Gemini Flash per estrarre e filtrare i lead industriali"""
    url = f"https://googleapis.com{api_key}"
    
    prompt = f"""
    Sei un analista B2B esperto nel settore farmaceutico in Europa. Analizza il seguente testo proveniente da ricerche sul Web e bandi pubblici europei (TED).
    Identifica aziende farmaceutiche, CDMO o enti pubblici che richiedono o stanno per acquistare macchine di Ispezione Visiva Automatica (AVI) e sistemi di controllo integrità/leak test (CCIT, HVLD, Vacuum Decay).
    
    Scarta lo spam, i rivenditori di componenti usati e i laboratori accademici minori. Concentrati su siti di produzione e linee industriali in Europa.

    Testo da analizzare:
    {combined_text}
    
    Rispondi ESCLUSIVAMENTE in formato JSON con la seguente struttura di lista. Se non trovi lead validi, restituisci un array vuoto []. Non aggiungere altro testo prima o dopo il JSON.
    [
      {{
        "Canale": "Web" o "Bando TED",
        "Azienda_O_Ente": "Nome dell'azienda farmaceutica o dell'acquirente pubblico",
        "Paese": "Nazione Europea",
        "Opportunita_Rilevata": "Spiega chiaramente perché è un lead (es. Espansione stabilimento siringhe sterili, Adeguamento Annex 1, bando per macchinari)",
        "Dettaglio_Fonte": "Contesto o snippet estratto che prova l'opportunità"
      }}
    ]
    """
    
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    try:
        res = requests.post(url, json=payload, timeout=20)
        text_response = res.json()['candidates'][0]['content']['parts'][0]['text']
        # Pulisce eventuali formattazioni markdown inviate dall'AI
        clean_json = text_response.replace("```json", "").replace("```", "").strip()
        return pd.read_json(clean_json)
    except Exception as e:
        print(f"Errore analisi AI: {e}")
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
    
    # 1. Raccolta dati da fonti eterogenee
    raw_data_compiled = ""
    
    print("Scansione bandi di gara TED...")
    ted_results = search_ted_tenders()
    raw_data_compiled += "--- RISULTATI APPALTI PUBBLICI TED EUROPEA ---\n" + ted_results + "\n"
    
    print("Scansione web generalista...")
    for q in WEB_QUERIES:
        raw_data_compiled += f"--- RISULTATI WEB PER QUERY: {q} ---\n" + search_web_free(q) + "\n"
        
    # 2. Elaborazione Intelligente
    print("Analisi in corso con l'AI...")
    df_leads = analyze_all_with_ai(raw_data_compiled, GEMINI_API_KEY)
    
    # 3. Creazione Excel e Spedizione
    if not df_leads.empty:
        # Rimuove eventuali duplicati basati sul nome dell'azienda o ente
        df_leads = df_leads.drop_duplicates(subset=['Azienda_O_Ente'])
        file_name = "Report_Settimanale_Lead_Totale.xlsx"
        df_leads.to_excel(file_name, index=False)
        send_email_with_excel(file_name, RECEIVER_EMAIL, EMAIL_USER, EMAIL_PASS)
    else:
        print("Nessun lead rilevante trovato questa settimana. Nessuna email inviata.")
