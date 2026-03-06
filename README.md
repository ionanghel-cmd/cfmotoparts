# Monitor Comenzi CFMoto Parts (Web)

Aplicație web (Streamlit) pentru monitorizarea comenzilor de piese CFMoto, import din:
- HTML pentru comenzi plasate
- PDF pentru invoice-uri viitoare

## Funcționalități
- Stocare locală în SQLite (`comenzi.db`)
- Separare pe tab-uri pentru comenzi plasate și invoice-uri viitoare
- Detalii piese pe comandă + filtrare după cod/denumire
- Raport piese în așteptare

## Rulare locală
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

## Deploy online (GitHub + Streamlit Community Cloud)
1. Urcă proiectul pe GitHub.
2. Intră pe https://share.streamlit.io
3. Selectează repository-ul și fișierul `app.py`.
4. Deploy.

> Notă: baza de date SQLite e locală containerului de deploy. Pentru persistență reală multi-user recomand Postgres/Supabase.
