import io
import re
import sqlite3
from datetime import datetime

import pdfplumber
import streamlit as st
from bs4 import BeautifulSoup

DB_PATH = "comenzi.db"


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS comenzi (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_number TEXT UNIQUE,
            data_plasare TEXT NOT NULL,
            note TEXT,
            tip TEXT DEFAULT 'plasata'
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS piese (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            comanda_id INTEGER NOT NULL,
            nume_piesa TEXT NOT NULL,
            cod TEXT,
            cantitate REAL NOT NULL,
            cantitate_primita REAL DEFAULT 0.0,
            data_primire TEXT,
            status TEXT DEFAULT 'asteptata',
            FOREIGN KEY (comanda_id) REFERENCES comenzi(id)
        )
        """
    )
    conn.commit()


def parse_html_and_insert(conn: sqlite3.Connection, html_text: str):
    cursor = conn.cursor()
    soup = BeautifulSoup(html_text, "html.parser")

    order_title = soup.find("h1", class_="page-header")
    if not order_title:
        raise ValueError("Nu am găsit numărul comenzii în HTML.")

    order_number = order_title.text.strip().replace("Order ", "")

    cursor.execute("SELECT id FROM comenzi WHERE order_number = ?", (order_number,))
    if cursor.fetchone():
        return order_number, 0, "exists"

    data = datetime.now().strftime("%Y-%m-%d")
    cursor.execute(
        "INSERT INTO comenzi (order_number, data_plasare, tip) VALUES (?, ?, 'plasata')",
        (order_number, data),
    )
    comanda_id = cursor.lastrowid

    table = soup.find("table", class_="views-table")
    added = 0
    if table and table.find("tbody"):
        for row in table.find("tbody").find_all("tr"):
            cols = row.find_all("td")
            if len(cols) >= 4:
                nume = (
                    cols[1]
                    .text.strip()
                    .replace("\n", " ")
                    .replace("sufficient stock", "")
                    .strip()
                )
                cod_match = re.search(r"\(([\w-]+)\)", nume)
                cod = cod_match.group(1) if cod_match else ""
                try:
                    cant = float(cols[3].text.strip())
                except (ValueError, TypeError):
                    cant = 1.0

                cursor.execute(
                    """
                    INSERT INTO piese (comanda_id, nume_piesa, cod, cantitate, status)
                    VALUES (?, ?, ?, ?, 'asteptata')
                    """,
                    (comanda_id, nume, cod, cant),
                )
                added += 1

    conn.commit()
    return order_number, added, "created"


def _extract_invoice_number(text: str):
    patterns = [
        r"Serie/Numar:\s*([A-Z]{1,6}\s?[A-Z]{0,6}/\d+)",
        r"Nr\.?\s*factura[:\s]*([A-Z0-9\-/]+)",
        r"Invoice\s*(?:No\.?|#|Number)[:\s]*([A-Z0-9\-/]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return match.group(1).strip()
    return None


def _extract_invoice_date(text: str):
    patterns = [
        r"Data:\s*(\d{2}\.\d{2}\.\d{4})",
        r"Date:\s*(\d{2}[./-]\d{2}[./-]\d{4})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return match.group(1).replace("/", ".").replace("-", ".")
    return datetime.now().strftime("%d.%m.%Y")


def parse_pdf_and_insert(conn: sqlite3.Connection, file_bytes: bytes):
    cursor = conn.cursor()
    added = 0

    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)

    order_number = _extract_invoice_number(text)
    if not order_number:
        raise ValueError("Nu am găsit numărul facturii în PDF.")

    cursor.execute("SELECT id FROM comenzi WHERE order_number = ?", (order_number,))
    if cursor.fetchone():
        return order_number, 0, "exists"

    data = _extract_invoice_date(text)

    cursor.execute(
        "INSERT INTO comenzi (order_number, data_plasare, tip, note) VALUES (?, ?, 'viitoare', 'Din PDF')",
        (order_number, data),
    )
    comanda_id = cursor.lastrowid

    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables() or []
            for table in tables:
                header_skipped = False
                for row in table:
                    if not row:
                        continue
                    if not header_skipped and "Nr" in str(row[0]):
                        header_skipped = True
                        continue

                    if len(row) < 5:
                        continue

                    if not row[0] or not re.match(r"^\d+$", str(row[0]).strip()):
                        continue

                    cod = str(row[1] or "").strip()
                    den = str(row[2] or "").strip().replace("...nedefinita...", "").strip()
                    nume = f"{cod} {den}".strip()

                    cant_idx = 4 if len(row) > 4 else 3
                    cant_str = str(row[cant_idx] or "0").replace(",", ".")

                    try:
                        cant = float(re.sub(r"[^0-9.\-]", "", cant_str) or "0")
                    except (ValueError, TypeError):
                        cant = 1.0

                    if cant > 0 and nume:
                        cursor.execute(
                            """
                            INSERT INTO piese (comanda_id, nume_piesa, cod, cantitate, status)
                            VALUES (?, ?, ?, ?, 'in_tranzit')
                            """,
                            (comanda_id, nume, cod, cant),
                        )
                        added += 1

    conn.commit()
    return order_number, added, "created"


def get_comenzi(conn: sqlite3.Connection, tip: str):
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT c.id, c.order_number, c.data_plasare,
               COALESCE(SUM(p.cantitate - p.cantitate_primita), 0) AS lipsa
        FROM comenzi c LEFT JOIN piese p ON c.id = p.comanda_id
        WHERE c.tip = ?
        GROUP BY c.id
        HAVING lipsa > 0
        ORDER BY c.data_plasare DESC
        """,
        (tip,),
    )
    return cursor.fetchall()


def get_piese_for_comanda(conn: sqlite3.Connection, comanda_id: int, query_text: str = ""):
    cursor = conn.cursor()
    query = """
        SELECT id, cod, nume_piesa, cantitate, cantitate_primita,
               (cantitate - cantitate_primita) AS lipsa, status, data_primire
        FROM piese
        WHERE comanda_id = ?
    """
    params = [comanda_id]

    if query_text.strip():
        query += " AND (cod LIKE ? OR nume_piesa LIKE ?)"
        pattern = f"%{query_text.strip()}%"
        params += [pattern, pattern]

    query += " ORDER BY status, nume_piesa"
    cursor.execute(query, params)
    return cursor.fetchall()


def get_raport_asteptate(conn: sqlite3.Connection):
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT c.order_number, c.data_plasare, p.nume_piesa, p.cod,
               (p.cantitate - p.cantitate_primita) AS lipsa, p.status
        FROM piese p JOIN comenzi c ON p.comanda_id = c.id
        WHERE p.cantitate > p.cantitate_primita
        ORDER BY c.data_plasare DESC, lipsa DESC
        """
    )
    return cursor.fetchall()


def rows_to_dicts(rows):
    return [dict(r) for r in rows]


def main():
    st.set_page_config(page_title="Monitor Comenzi CFMoto Parts", layout="wide")
    st.title("Monitorizare Comenzi CFMOTO")

    conn = get_connection()
    init_db(conn)

    col1, col2 = st.columns(2)

    with col1:
        st.subheader("Încarcă comandă plasată (HTML)")
        html_file = st.file_uploader("Fișier HTML", type=["html"], key="html")
        if st.button("Importă HTML", use_container_width=True):
            if not html_file:
                st.warning("Alege un fișier HTML.")
            else:
                try:
                    html_file.seek(0)
                    html_text = html_file.read().decode("utf-8", errors="ignore")
                    number, added, state = parse_html_and_insert(conn, html_text)
                    if state == "exists":
                        st.info(f"{number} există deja.")
                    else:
                        st.success(f"Comanda {number} a fost adăugată cu {added} piese.")
                except Exception as exc:
                    st.error(f"Eroare la import HTML: {exc}")

    with col2:
        st.subheader("Încarcă invoice viitoare (PDF)")
        pdf_file = st.file_uploader("Fișier PDF", type=["pdf"], key="pdf")
        if st.button("Importă PDF", use_container_width=True):
            if not pdf_file:
                st.warning("Alege un fișier PDF.")
            else:
                try:
                    pdf_file.seek(0)
                    file_bytes = pdf_file.read()
                    number, added, state = parse_pdf_and_insert(conn, file_bytes)
                    if state == "exists":
                        st.info(f"{number} există deja.")
                    else:
                        st.success(f"Factura {number} a fost adăugată cu {added} piese viitoare.")
                except Exception as exc:
                    st.error(f"Eroare la import PDF: {exc}")

    tab1, tab2, tab3 = st.tabs(["Comenzi plasate (HTML)", "Invoice viitoare (PDF)", "Raport așteptate"])

    with tab1:
        rows = get_comenzi(conn, "plasata")
        st.dataframe(
            [
                {
                    "ID": row["id"],
                    "Comandă": row["order_number"],
                    "Data": row["data_plasare"],
                    "Piese lipsă (buc)": int(row["lipsa"]),
                }
                for row in rows
            ],
            use_container_width=True,
        )

        selected_id = st.number_input("Vezi detalii comandă (ID)", min_value=0, step=1, value=0, key="det_plasata")
        if selected_id > 0:
            query_text = st.text_input("Caută piesă (cod / denumire)", key="q_plasata")
            detalii = get_piese_for_comanda(conn, selected_id, query_text)
            st.dataframe(rows_to_dicts(detalii), use_container_width=True)

    with tab2:
        rows = get_comenzi(conn, "viitoare")
        st.dataframe(
            [
                {
                    "ID": row["id"],
                    "Factură": row["order_number"],
                    "Data": row["data_plasare"],
                    "Piese lipsă (buc)": int(row["lipsa"]),
                }
                for row in rows
            ],
            use_container_width=True,
        )

        selected_id = st.number_input("Vezi detalii factură (ID)", min_value=0, step=1, value=0, key="det_viitoare")
        if selected_id > 0:
            query_text = st.text_input("Caută piesă (cod / denumire)", key="q_viitoare")
            detalii = get_piese_for_comanda(conn, selected_id, query_text)
            st.dataframe(rows_to_dicts(detalii), use_container_width=True)

    with tab3:
        rows = get_raport_asteptate(conn)
        if not rows:
            st.info("Nimic în așteptare.")
        else:
            lines = []
            current_com = None
            for r in rows:
                if r["order_number"] != current_com:
                    lines.append(f"\n### {r['order_number']} ({r['data_plasare']})")
                    current_com = r["order_number"]
                status_color = "🔴" if r["status"] == "asteptata" else "🟡" if r["status"] == "in_tranzit" else "🟢"
                cod = r["cod"] or "fără cod"
                lines.append(f"- {status_color} {r['nume_piesa']} ({cod}) — lipsă {r['lipsa']:.0f} buc")
            st.markdown("\n".join(lines))

    st.caption(
        "Dacă ai publicat pe GitHub Pages (ionanghel-cmd.github.io), aplicația Python nu poate rula acolo. "
        "Folosește Streamlit Community Cloud pentru runtime Python."
    )

    conn.close()


if __name__ == "__main__":
    main()
