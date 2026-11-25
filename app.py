import streamlit as st
import pandas as pd
import io
import os
import re
import difflib
from datetime import datetime, date
from sqlalchemy import create_engine, text

# ---------------------------------------------------------------------------
# 1. PAGE CONFIG & CONSTANTS
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="JINDAL STEEL LIMITED | WIP & Fabrication Control",
    page_icon="🏗️",
    layout="wide"
)

DB_FILE = "fabtech_wip.db"
CLIENT_LOGO_FILE = "images.png"
CLIENT_NAME = "JINDAL STEEL LIMITED"

MASTER_COLUMNS = [
    "sr_no", "Batch No", "project_no", "description", "revision_no",
    "drawing_no", "element_type", "mark_no", "qty", "part_sl_no",
    "actual_wt", "draw_total_wt", "contractor", "plant", "status",
    "wip_date", "plant prod date", "handover date", "rfd_date", "dispatch_date"
]

# Non-date columns owned by PMH (admin) and PMG
MASTER_DATA_COLUMNS = [
    "sr_no", "Batch No", "project_no", "description", "revision_no",
    "drawing_no", "element_type", "mark_no", "qty", "part_sl_no",
    "actual_wt", "draw_total_wt", "contractor", "plant"
]

# Stage date columns strictly locked to user1 - user5
DATE_COLUMNS = [
    "wip_date", "plant prod date", "handover date", "rfd_date", "dispatch_date"
]

# Alias dictionary for fuzzy/approximate header matching from Go-Live MASTER sheets
COLUMN_ALIASES = {
    "sr_no": [
        "sr_no", "sr no", "sl no", "sl. no", "s.no", "sno", "serial no", "s/n", "item no"
    ],
    "Batch No": [
        "batch no", "batch", "unique-2", "unique 2", "wip batch", "batch_no", "batch id", "lot no"
    ],
    "project_no": [
        "project_no", "project no", "project", "proj no", "wbs", "po no", "job no", "contract no"
    ],
    "description": [
        "description", "unit", "po_description", "po description", "building", "area", "structure", "desc"
    ],
    "revision_no": [
        "revision_no", "revision no", "latestrev", "latest rev", "rev", "rev no", "revision"
    ],
    "drawing_no": [
        "drawing_no", "drawing no", "drawingno", "drg no", "drg_no", "dwg no", "drawing number"
    ],
    "element_type": [
        "element_type", "element type", "drawingtype", "drawing type", "type", "member type", "category"
    ],
    "mark_no": [
        "mark_no", "mark no", "markno", "assembly mark", "mark", "assy mark", "piece mark"
    ],
    "qty": [
        "qty", "quantity", "nos", "no.", "count", "pieces", "pcs"
    ],
    "part_sl_no": [
        "part_sl_no", "part sl no", "partserialno.", "partserialno", "part serial no", "part no", "partno", "part sl"
    ],
    "actual_wt": [
        "actual_wt", "actual wt", "unitwt", "unit wt", "unit weight", "wt", "weight", "net wt", "weight (mt)"
    ],
    "draw_total_wt": [
        "draw_total_wt", "draw total wt", "totalwt", "total wt", "total weight", "drawing weight", "gross wt"
    ],
    "contractor": [
        "contractor", "subcontractor", "sub contractor", "fabricator", "vendor", "vendor name", "agency"
    ],
    "plant": [
        "plant", "shop", "unit name", "location", "fabrication plant", "work center"
    ]
}

# Default User Credentials & Strict Role Mapping (seeded into DB table `app_users`)
DEFAULT_USERS = {
    "admin": {"password": "admin123", "role": "admin",    "target_col": None,              "label": "PMH / Admin (Full View + Master Data)"},
    "pmg":   {"password": "pmg123",   "role": "pmg",      "target_col": None,              "label": "PMG (Master Builder + Drawing / Plant / Contractor / Weight)"},
    "user1": {"password": "user123",  "role": "uploader", "target_col": "wip_date",        "label": "User 1 (WIP Date Only)"},
    "user2": {"password": "user123",  "role": "uploader", "target_col": "plant prod date", "label": "User 2 (Plant Prod Date Only)"},
    "user3": {"password": "user123",  "role": "uploader", "target_col": "handover date",   "label": "User 3 (Handover Date Only)"},
    "user4": {"password": "user123",  "role": "uploader", "target_col": "rfd_date",        "label": "User 4 (RFD Date Only)"},
    "user5": {"password": "user123",  "role": "uploader", "target_col": "dispatch_date",   "label": "User 5 (Dispatch Date Only)"},
}

AGEING_BUCKETS = ["0-7 Days", "8-15 Days", "16-30 Days", "30-60 Days", ">60 Days"]

# ---------------------------------------------------------------------------
# 2. DATABASE (NEON POSTGRESQL / SQLITE) & USER AUTH HELPERS
# ---------------------------------------------------------------------------
def get_db_url():
    try:
        if "DATABASE_URL" in st.secrets and st.secrets["DATABASE_URL"]:
            return str(st.secrets["DATABASE_URL"]).strip()
    except Exception:
        pass
    env_url = os.environ.get("DATABASE_URL", "").strip()
    if env_url:
        return env_url
    return f"sqlite:///{DB_FILE}"

def get_admin_recovery_key():
    try:
        if "ADMIN_RECOVERY_KEY" in st.secrets and st.secrets["ADMIN_RECOVERY_KEY"]:
            return str(st.secrets["ADMIN_RECOVERY_KEY"]).strip()
    except Exception:
        pass
    return os.environ.get("ADMIN_RECOVERY_KEY", "").strip()

@st.cache_resource
def get_engine():
    url = get_db_url()
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg2://", 1)
    elif url.startswith("postgresql://") and "+psycopg2" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg2://", 1)

    connect_args = {}
    if url.startswith("postgresql"):
        connect_args["sslmode"] = "require"

    return create_engine(url, pool_pre_ping=True, connect_args=connect_args)

def is_postgres():
    return get_engine().dialect.name == "postgresql"

def init_db():
    engine = get_engine()
    id_col = "id SERIAL PRIMARY KEY" if is_postgres() else "id INTEGER PRIMARY KEY AUTOINCREMENT"
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS project_master (
                sr_no INTEGER,
                "Batch No" TEXT PRIMARY KEY,
                project_no TEXT,
                description TEXT,
                revision_no TEXT,
                drawing_no TEXT,
                element_type TEXT,
                mark_no TEXT,
                qty INTEGER,
                part_sl_no TEXT,
                actual_wt REAL,
                draw_total_wt REAL,
                contractor TEXT,
                plant TEXT,
                status TEXT,
                wip_date TEXT,
                "plant prod date" TEXT,
                "handover date" TEXT,
                rfd_date TEXT,
                dispatch_date TEXT
            )
        """))
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS audit_log (
                {id_col},
                username TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                "Batch No" TEXT NOT NULL,
                column_updated TEXT NOT NULL,
                old_date TEXT,
                new_date TEXT
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS app_users (
                username TEXT PRIMARY KEY,
                password TEXT NOT NULL,
                role TEXT NOT NULL,
                target_col TEXT,
                label TEXT NOT NULL,
                updated_at TEXT
            )
        """))

        existing_users = pd.read_sql_query(text("SELECT username FROM app_users"), conn)
        existing_set = set(existing_users["username"].tolist()) if not existing_users.empty else set()
        now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for uname, info in DEFAULT_USERS.items():
            if uname not in existing_set:
                conn.execute(
                    text("""
                        INSERT INTO app_users (username, password, role, target_col, label, updated_at)
                        VALUES (:u, :p, :r, :tc, :lbl, :ts)
                    """),
                    {
                        "u": uname,
                        "p": info["password"],
                        "r": info["role"],
                        "tc": info["target_col"],
                        "lbl": info["label"],
                        "ts": now_ts
                    }
                )

def load_users_dict():
    engine = get_engine()
    with engine.connect() as conn:
        df = pd.read_sql_query(text("SELECT * FROM app_users"), conn)
    users_map = {}
    for _, row in df.iterrows():
        uname = str(row["username"]).strip().lower()
        tc = row["target_col"]
        if pd.isna(tc) or str(tc).strip() in ("", "None", "nan"):
            tc = None
        users_map[uname] = {
            "password": str(row["password"]),
            "role": str(row["role"]),
            "target_col": tc,
            "label": str(row["label"]),
            "updated_at": str(row.get("updated_at") or "")
        }
    return users_map

def update_user_password(target_username, new_password, actor_username):
    engine = get_engine()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE app_users SET password = :p, updated_at = :ts WHERE username = :u"),
            {"p": new_password, "ts": ts, "u": target_username}
        )
        conn.execute(
            text("""
                INSERT INTO audit_log (username, timestamp, "Batch No", column_updated, old_date, new_date)
                VALUES (:u, :ts, :b, :c, :o, :n)
            """),
            {
                "u": actor_username,
                "ts": ts,
                "b": f"USER:{target_username}",
                "c": "PASSWORD_CHANGED",
                "o": "********",
                "n": "********"
            }
        )

def write_audit_entries(entries):
    if not entries:
        return
    engine = get_engine()
    payload = [
        {"u": e[0], "ts": e[1], "b": e[2], "c": e[3], "o": e[4], "n": e[5]}
        for e in entries
    ]
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO audit_log (username, timestamp, "Batch No", column_updated, old_date, new_date)
                VALUES (:u, :ts, :b, :c, :o, :n)
            """),
            payload
        )

def load_audit_log_df():
    engine = get_engine()
    with engine.connect() as conn:
        df = pd.read_sql_query(text('SELECT * FROM audit_log ORDER BY id DESC'), conn)
    if not df.empty:
        df["Batch No"] = df["Batch No"].astype(str).str.strip()
    return df

def clean_part_sl_no(val):
    """
    Ensures part_sl_no stays strictly in text format (e.g. '1/2', '2/2').
    Recovers values that Excel auto-converted into datetime objects or ISO date strings
    when a fraction like '1/2' or '2/2' was entered without text formatting.
    """
    if pd.isna(val) or val is None:
        return ""
    if isinstance(val, (datetime, date, pd.Timestamp)):
        return f"{val.month}/{val.day}"

    s = str(val).strip()
    if s == "" or s.lower() in ("nan", "nat", "none", "null"):
        return ""

    # Strip accidental leading apostrophe from Excel text-forced cells
    if s.startswith("'"):
        s = s[1:].strip()

    # Recover ISO date strings produced by openpyxl/pandas from '1/2' -> '2026-01-02 00:00:00'
    iso_match = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?)?$", s)
    if iso_match:
        month_num = int(iso_match.group(2))
        day_num = int(iso_match.group(3))
        return f"{month_num}/{day_num}"

    # Recover 'DD-MMM' or 'MMM-YY' style strings if Excel formatted '1/2' as '02-Jan' or 'Jan-02'
    month_map = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12
    }
    d_mmm = re.match(r"^(\d{1,2})[-/]([A-Za-z]{3})$", s)
    if d_mmm and d_mmm.group(2).lower() in month_map:
        return f"{month_map[d_mmm.group(2).lower()]}/{int(d_mmm.group(1))}"

    mmm_d = re.match(r"^([A-Za-z]{3})[-/](\d{1,2})$", s)
    if mmm_d and mmm_d.group(1).lower() in month_map:
        return f"{month_map[mmm_d.group(1).lower()]}/{int(mmm_d.group(2))}"

    # Clean integer floats like '1.0' -> '1'
    if re.match(r"^\d+\.0$", s):
        return s[:-2]

    return s

def clean_date_str(val):
    if pd.isna(val) or val is None:
        return ""
    s = str(val).strip()
    if s == "" or s.lower() in ("nan", "nat", "none", "null"):
        return ""
    try:
        dt = pd.to_datetime(s)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return s

def compute_status(row):
    if clean_date_str(row.get("dispatch_date")) != "":
        return "DISP"
    if clean_date_str(row.get("rfd_date")) != "":
        return "RFD"
    if clean_date_str(row.get("handover date")) != "":
        return "B&P"
    if clean_date_str(row.get("plant prod date")) != "":
        return "BLACK OK"
    if clean_date_str(row.get("wip_date")) != "":
        return "WIP"
    return "YTS"

def recalculate_all_statuses(df):
    if df.empty:
        return df
    for col in DATE_COLUMNS:
        if col in df.columns:
            df[col] = df[col].apply(clean_date_str)
        else:
            df[col] = ""
    df["status"] = df.apply(compute_status, axis=1)
    return df

def load_master_df():
    engine = get_engine()
    with engine.connect() as conn:
        df = pd.read_sql_query(text("SELECT * FROM project_master ORDER BY sr_no ASC"), conn)
    if not df.empty:
        df["part_sl_no"] = df["part_sl_no"].apply(clean_part_sl_no).astype(str)
        df["Batch No"] = df["Batch No"].astype(str).str.strip()
        df["project_no"] = df["project_no"].astype(str).str.strip()
        df = recalculate_all_statuses(df)
    return df

def save_master_df(df):
    df = recalculate_all_statuses(df)
    for col in MASTER_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    df = df[MASTER_COLUMNS].copy()
    df["part_sl_no"] = df["part_sl_no"].apply(clean_part_sl_no).astype(str)
    df["sr_no"] = pd.to_numeric(df["sr_no"], errors="coerce").fillna(0).astype(int)
    df["qty"] = pd.to_numeric(df["qty"], errors="coerce").fillna(0).astype(int)
    df["actual_wt"] = pd.to_numeric(df["actual_wt"], errors="coerce").fillna(0.0).astype(float)
    df["draw_total_wt"] = pd.to_numeric(df["draw_total_wt"], errors="coerce").fillna(0.0).astype(float)

    engine = get_engine()
    with engine.begin() as conn:
        df.to_sql("project_master", conn, if_exists="replace", index=False)
        if is_postgres():
            conn.execute(text('ALTER TABLE project_master ADD PRIMARY KEY ("Batch No")'))
        else:
            conn.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS idx_batch_no ON project_master ("Batch No")'))

def to_excel_bytes(df_dict):
    output = io.BytesIO()
    text_cols_to_lock = {"part_sl_no", "Batch No", "project_no", "revision_no", "drawing_no", "mark_no"}
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        for sheet_name, df in df_dict.items():
            clean_sheet = sheet_name[:31]
            df_export = df.copy()
            if "part_sl_no" in df_export.columns:
                df_export["part_sl_no"] = df_export["part_sl_no"].apply(clean_part_sl_no).astype(str)
            df_export.to_excel(writer, index=False, sheet_name=clean_sheet)

            # Explicitly format text columns as '@' in Excel so '1/2', '2/2' never convert to dates
            ws = writer.sheets[clean_sheet]
            for col_idx, col_name in enumerate(df_export.columns, start=1):
                if col_name in text_cols_to_lock:
                    for row_idx in range(2, len(df_export) + 2):
                        cell = ws.cell(row=row_idx, column=col_idx)
                        cell.number_format = "@"
                        if cell.value is not None:
                            cell.value = str(cell.value)
    return output.getvalue()

# ---------------------------------------------------------------------------
# 2B. FUZZY HEADER MATCHING & MASTER SHEET EXTRACTOR HELPERS
# ---------------------------------------------------------------------------
def normalize_header(h):
    s = str(h).strip().lower()
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return s

def detect_header_row(preview_df):
    all_known = set()
    for aliases in COLUMN_ALIASES.values():
        for a in aliases:
            all_known.add(normalize_header(a))

    best_row = 0
    best_score = -1
    max_check = min(15, len(preview_df))

    for r_idx in range(max_check):
        row_vals = [normalize_header(v) for v in preview_df.iloc[r_idx].values if pd.notna(v)]
        if not row_vals:
            continue
        score = sum(1 for v in row_vals if v in all_known)
        if score > best_score:
            best_score = score
            best_row = r_idx

    return best_row

def approximate_column_mapping(source_columns):
    norm_to_orig = {}
    for col in source_columns:
        col_str = str(col).strip()
        if col_str.lower().startswith("unnamed:"):
            continue
        n = normalize_header(col_str)
        if n and n not in norm_to_orig:
            norm_to_orig[n] = col_str

    used_sources = set()
    mapping = {}

    for target_col in MASTER_DATA_COLUMNS:
        aliases = [normalize_header(a) for a in COLUMN_ALIASES.get(target_col, [target_col])]
        matched = None
        for alias in aliases:
            if alias in norm_to_orig and norm_to_orig[alias] not in used_sources:
                matched = norm_to_orig[alias]
                break
        if matched:
            used_sources.add(matched)
            mapping[target_col] = (matched, "Exact / Alias")
        else:
            mapping[target_col] = (None, "Not Found")

    remaining_norms = {n: orig for n, orig in norm_to_orig.items() if orig not in used_sources}
    for target_col in MASTER_DATA_COLUMNS:
        if mapping[target_col][0] is not None:
            continue
        aliases = [normalize_header(a) for a in COLUMN_ALIASES.get(target_col, [target_col])]
        best_orig = None
        best_ratio = 0.0

        for alias in aliases:
            matches = difflib.get_close_matches(alias, list(remaining_norms.keys()), n=1, cutoff=0.68)
            if matches:
                cand_norm = matches[0]
                ratio = difflib.SequenceMatcher(None, alias, cand_norm).ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_orig = remaining_norms[cand_norm]

        if best_orig:
            used_sources.add(best_orig)
            remaining_norms = {n: orig for n, orig in remaining_norms.items() if orig not in used_sources}
            mapping[target_col] = (best_orig, f"Approx ({int(best_ratio * 100)}%)")

    return mapping

def infer_project_code_from_filename(filename):
    base = os.path.splitext(os.path.basename(filename))[0].strip()
    token = base.split()[0] if base else ""
    return token if token else base

def extract_standardized_df(raw_df, col_map, fallback_project_no="", start_sr_no=1):
    out = pd.DataFrame(index=raw_df.index)

    for target_col in MASTER_DATA_COLUMNS:
        src_col = col_map.get(target_col)
        if src_col and src_col in raw_df.columns:
            out[target_col] = raw_df[src_col]
        else:
            out[target_col] = ""

    out["Batch No"] = out["Batch No"].astype(str).str.strip()
    out = out[
        (out["Batch No"] != "")
        & (out["Batch No"].str.lower() != "nan")
        & (out["Batch No"].str.lower() != "none")
        & (~out["Batch No"].str.lower().str.startswith("total"))
        & (~out["Batch No"].str.lower().str.startswith("grand total"))
    ].copy()

    if out.empty:
        return out

    out["project_no"] = out["project_no"].fillna("").astype(str).str.strip()
    out.loc[out["project_no"].isin(["", "nan", "None"]), "project_no"] = fallback_project_no

    out["qty"] = pd.to_numeric(out["qty"], errors="coerce").fillna(1).astype(int)
    out["actual_wt"] = pd.to_numeric(out["actual_wt"], errors="coerce").fillna(0.0).round(5)
    out["draw_total_wt"] = pd.to_numeric(out["draw_total_wt"], errors="coerce")
    out["draw_total_wt"] = out["draw_total_wt"].fillna(out["actual_wt"]).round(5)

    # Convert part_sl_no strictly as text (recovering '1/2', '2/2' if Excel stored as date)
    out["part_sl_no"] = out["part_sl_no"].apply(clean_part_sl_no).astype(str)

    # Clean remaining text fields
    for txt_col in ["description", "revision_no", "drawing_no", "element_type", "mark_no", "contractor", "plant"]:
        out[txt_col] = out[txt_col].fillna("").astype(str).str.strip()
        out.loc[out[txt_col].isin(["nan", "None", "NaT"]), txt_col] = ""

    sr_numeric = pd.to_numeric(out["sr_no"], errors="coerce").fillna(0).astype(int)
    if (sr_numeric == 0).all():
        out["sr_no"] = range(start_sr_no, start_sr_no + len(out))
    else:
        out["sr_no"] = sr_numeric

    out = out.drop_duplicates(subset=["Batch No"], keep="last").reset_index(drop=True)
    return out[MASTER_DATA_COLUMNS]

def upsert_master_records(incoming_df, actor_username):
    current_master = load_master_df()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    audit_rows = []
    tracked_meta_cols = [
        c for c in ["drawing_no", "revision_no", "contractor", "plant", "actual_wt", "qty"]
        if c in incoming_df.columns
    ]

    if "part_sl_no" in incoming_df.columns:
        incoming_df = incoming_df.copy()
        incoming_df["part_sl_no"] = incoming_df["part_sl_no"].apply(clean_part_sl_no).astype(str)

    existing_batches = set(current_master["Batch No"]) if not current_master.empty else set()
    new_count = int((~incoming_df["Batch No"].isin(existing_batches)).sum())
    upd_count = int((incoming_df["Batch No"].isin(existing_batches)).sum())

    if current_master.empty:
        df_to_save = incoming_df.copy()
        for col in MASTER_COLUMNS:
            if col not in df_to_save.columns:
                df_to_save[col] = ""
        final_df = recalculate_all_statuses(df_to_save)
        for b_no in final_df["Batch No"]:
            audit_rows.append((actor_username, ts, b_no, "NEW_BATCH_CREATED", "", "YTS"))
    else:
        master_idx = current_master.set_index("Batch No")
        present_data_cols = [c for c in MASTER_DATA_COLUMNS if c in incoming_df.columns]

        for _, r in incoming_df.iterrows():
            b_no = r["Batch No"]
            if b_no in master_idx.index:
                for col in present_data_cols:
                    if col == "Batch No":
                        continue
                    old_val = str(master_idx.at[b_no, col]) if pd.notna(master_idx.at[b_no, col]) else ""
                    new_val = str(r[col]) if pd.notna(r[col]) else ""
                    if col in tracked_meta_cols and old_val != new_val:
                        audit_rows.append((actor_username, ts, b_no, col, old_val, new_val))
                    master_idx.at[b_no, col] = r[col]
            else:
                new_record = {col: "" for col in MASTER_COLUMNS if col != "Batch No"}
                for col in present_data_cols:
                    if col != "Batch No":
                        new_record[col] = r[col]
                new_record["status"] = "YTS"
                master_idx.loc[b_no] = new_record
                audit_rows.append((actor_username, ts, b_no, "NEW_BATCH_CREATED", "", "YTS"))

        final_df = recalculate_all_statuses(master_idx.reset_index())

    save_master_df(final_df)
    write_audit_entries(audit_rows)
    return new_count, upd_count

# ---------------------------------------------------------------------------
# 2C. PROJECT SUMMARY PIVOT, WIP AGEING & STYLING HELPERS
# ---------------------------------------------------------------------------
def style_pivot_with_totals(df, index_col, total_col, numeric_cols):
    """
    Applies bold text and soft highlight background to both the GRAND TOTAL row
    and the GRAND TOTAL / Total WIP Wt (MT) column.
    """
    def highlight_cells(data):
        styles = pd.DataFrame("", index=data.index, columns=data.columns)
        if total_col in styles.columns:
            styles[total_col] = "font-weight: bold; background-color: #E8EEF5; color: #0F172A;"
        gt_mask = data[index_col].astype(str).str.strip().str.upper() == "GRAND TOTAL"
        styles.loc[gt_mask, :] = "font-weight: bold; background-color: #D5E2F2; color: #0F172A; border-top: 2px solid #64748B;"
        return styles

    return (
        df.style
        .format({col: "{:.3f}" for col in numeric_cols})
        .apply(highlight_cells, axis=None)
    )

def build_project_status_summary_pivot(df):
    """
    Builds the Project Status Weight Summary table:
    PROJECT | DISP | RFD | B&P | [BLACK OK if > 0] | WIP | YTS | GRAND TOTAL
    """
    if df.empty:
        return pd.DataFrame()

    work = df.copy()
    work["PROJECT"] = work["project_no"].fillna("Unassigned").astype(str).str.strip().replace("", "Unassigned")
    work["actual_wt"] = pd.to_numeric(work["actual_wt"], errors="coerce").fillna(0.0)

    pivot = pd.pivot_table(
        work,
        values="actual_wt",
        index="PROJECT",
        columns="status",
        aggfunc="sum",
        fill_value=0.0
    )

    core_cols = ["DISP", "RFD", "B&P", "WIP", "YTS"]
    for c in core_cols:
        if c not in pivot.columns:
            pivot[c] = 0.0

    ordered_cols = ["DISP", "RFD", "B&P"]
    if "BLACK OK" in pivot.columns and pivot["BLACK OK"].sum() > 0:
        ordered_cols.append("BLACK OK")
    ordered_cols.extend(["WIP", "YTS"])

    pivot = pivot[ordered_cols]
    pivot["GRAND TOTAL"] = pivot.sum(axis=1)
    pivot = pivot.reset_index()

    total_row = {"PROJECT": "GRAND TOTAL"}
    for col in ordered_cols + ["GRAND TOTAL"]:
        total_row[col] = pivot[col].sum()

    pivot = pd.concat([pivot, pd.DataFrame([total_row])], ignore_index=True)

    for col in ordered_cols + ["GRAND TOTAL"]:
        pivot[col] = pivot[col].round(3)

    return pivot

def get_ageing_bucket(days):
    if days <= 7:
        return "0-7 Days"
    elif days <= 15:
        return "8-15 Days"
    elif days <= 30:
        return "16-30 Days"
    elif days <= 60:
        return "30-60 Days"
    else:
        return ">60 Days"

def get_active_wip_with_ageing(df, ref_date):
    if df.empty:
        return pd.DataFrame()

    wip_df = df[(df["status"] != "DISP") & (df["wip_date"] != "")].copy()
    if wip_df.empty:
        return pd.DataFrame()

    wip_df["wip_dt"] = pd.to_datetime(wip_df["wip_date"], errors="coerce")
    wip_df = wip_df.dropna(subset=["wip_dt"])
    if wip_df.empty:
        return pd.DataFrame()

    ref_ts = pd.Timestamp(ref_date)
    wip_df["Age (Days)"] = (ref_ts - wip_df["wip_dt"]).dt.days.clip(lower=0).astype(int)
    wip_df["Ageing Bucket"] = wip_df["Age (Days)"].apply(get_ageing_bucket)
    wip_df["project_no"] = wip_df["project_no"].fillna("Unassigned").replace("", "Unassigned")
    wip_df["contractor"] = wip_df["contractor"].fillna("Unassigned").replace("", "Unassigned")
    return wip_df.drop(columns=["wip_dt"])

def build_wip_ageing_pivot(df, group_col, ref_date):
    wip_df = get_active_wip_with_ageing(df, ref_date)
    if wip_df.empty:
        return pd.DataFrame()

    pivot = pd.pivot_table(
        wip_df,
        values="actual_wt",
        index=group_col,
        columns="Ageing Bucket",
        aggfunc="sum",
        fill_value=0.0
    )

    for b in AGEING_BUCKETS:
        if b not in pivot.columns:
            pivot[b] = 0.0
    pivot = pivot[AGEING_BUCKETS]

    pivot["Total WIP Wt (MT)"] = pivot.sum(axis=1)
    pivot = pivot.reset_index()

    total_row = {group_col: "GRAND TOTAL"}
    for col in AGEING_BUCKETS + ["Total WIP Wt (MT)"]:
        total_row[col] = pivot[col].sum()

    pivot = pd.concat([pivot, pd.DataFrame([total_row])], ignore_index=True)

    for col in AGEING_BUCKETS + ["Total WIP Wt (MT)"]:
        pivot[col] = pivot[col].round(3)

    return pivot

def render_ageing_bucket_drilldown(master_df, ref_date, entity_col, entity_label, key_prefix):
    wip_aged_df = get_active_wip_with_ageing(master_df, ref_date)
    if wip_aged_df.empty:
        return

    st.divider()
    st.markdown(f"### 🔍 Extract Master List by Ageing Bucket ({entity_label}-Wise)")
    st.caption("Select an ageing bucket (and optional filter) to inspect and download the matching Project Master records.")

    c1, c2 = st.columns([1, 1.2])
    bucket_options = ["All Active WIP Buckets"] + AGEING_BUCKETS
    selected_bucket = c1.selectbox(
        "Select Ageing Bucket:",
        options=bucket_options,
        index=1,  # Defaults to '0-7 Days'
        key=f"{key_prefix}_bucket_sel"
    )

    entities = sorted(wip_aged_df[entity_col].dropna().unique().tolist())
    selected_entities = c2.multiselect(
        f"Filter by {entity_label} (Optional — leave empty for all):",
        options=entities,
        key=f"{key_prefix}_entity_sel"
    )

    drill_df = wip_aged_df.copy()
    if selected_bucket != "All Active WIP Buckets":
        drill_df = drill_df[drill_df["Ageing Bucket"] == selected_bucket]
    if selected_entities:
        drill_df = drill_df[drill_df[entity_col].isin(selected_entities)]

    drill_df = drill_df.sort_values(by=["Age (Days)", "sr_no"], ascending=[False, True]).reset_index(drop=True)

    display_cols = ["Ageing Bucket", "Age (Days)"] + MASTER_COLUMNS
    drill_df = drill_df[[c for c in display_cols if c in drill_df.columns]]

    m1, m2, m3 = st.columns(3)
    m1.metric("Matching Batches", len(drill_df))
    m2.metric("Matching Qty", int(drill_df["qty"].sum()) if not drill_df.empty else 0)
    m3.metric("Matching Actual Wt (MT)", f"{drill_df['actual_wt'].sum():.3f}" if not drill_df.empty else "0.000")

    if drill_df.empty:
        st.info(f"No active WIP records found in **{selected_bucket}** for the selected filter.")
    else:
        st.dataframe(drill_df, use_container_width=True, height=380)
        safe_bucket_tag = selected_bucket.replace(" ", "_").replace(">", "GT_")
        st.download_button(
            label=f"📥 Export Master List — {selected_bucket} (.xlsx)",
            data=to_excel_bytes({f"WIP_{safe_bucket_tag}": drill_df}),
            file_name=f"WIP_Master_{entity_label}_{safe_bucket_tag}_{ref_date}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=f"{key_prefix}_dl_btn"
        )

def build_stage_turnaround_df(m_row):
    records = []
    prev_stage = None
    prev_dt = None

    for col in DATE_COLUMNS:
        d_str = clean_date_str(m_row.get(col, ""))
        dt_val = pd.to_datetime(d_str, errors="coerce") if d_str else pd.NaT

        if pd.notna(dt_val) and pd.notna(prev_dt):
            elapsed = (dt_val - prev_dt).days
            elapsed_str = f"{elapsed} days"
            span_label = f"{prev_stage} -> {col}"
        elif pd.notna(dt_val) and prev_dt is None:
            elapsed_str = "Start"
            span_label = "Initial Stage"
        elif pd.notna(dt_val):
            elapsed_str = "—"
            span_label = "Prior stage blank"
        else:
            elapsed_str = "Pending"
            span_label = f"{prev_stage or 'Prior'} -> {col}"

        records.append({
            "Stage Column": col,
            "Stage Date": d_str if d_str else "—",
            "Interval": span_label,
            "Days Elapsed": elapsed_str
        })

        if pd.notna(dt_val):
            prev_stage = col
            prev_dt = dt_val

    return pd.DataFrame(records)

# ---------------------------------------------------------------------------
# 3. INITIALIZE DB & AUTHENTICATION UI
# ---------------------------------------------------------------------------
init_db()
USERS = load_users_dict()

if "logged_in" not in st.session_state:
    st.session_state.logged_in = False
    st.session_state.username = None

if not st.session_state.logged_in:
    col_l, col_c, col_r = st.columns([1, 1.35, 1])
    with col_c:
        if os.path.exists(CLIENT_LOGO_FILE):
            st.image(CLIENT_LOGO_FILE, width=220)
        st.markdown(f"### Client: **{CLIENT_NAME}**")
        st.title("🏗️ WIP & Fabrication Control System")
        st.markdown("Sign in with your assigned credentials to access the portal.")

        with st.form("login_form"):
            uname = st.text_input("Username (admin, pmg, user1 - user5)").strip().lower()
            pwd = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Login", use_container_width=True)

            if submitted:
                if uname in USERS and USERS[uname]["password"] == pwd:
                    st.session_state.logged_in = True
                    st.session_state.username = uname
                    st.rerun()
                else:
                    st.error("Invalid username or password.")

        with st.expander("🔑 Forgot Password / Emergency Admin Recovery"):
            st.caption(
                "If all users forget their passwords, reset any account using the Master Recovery Key "
                "configured in Streamlit Secrets (`ADMIN_RECOVERY_KEY`), or reset `app_users` in Neon SQL Editor."
            )
            configured_key = get_admin_recovery_key()
            if configured_key:
                with st.form("emergency_reset_form"):
                    rec_key_input = st.text_input("Enter Master Recovery Key", type="password")
                    rec_user = st.selectbox("Account to Reset", options=list(USERS.keys()))
                    rec_new_pwd = st.text_input("New Password (min 4 chars)", type="password")
                    rec_submit = st.form_submit_button("Reset Password Now", use_container_width=True)

                    if rec_submit:
                        if rec_key_input != configured_key:
                            st.error("Invalid Master Recovery Key.")
                        elif len(rec_new_pwd.strip()) < 4:
                            st.error("New password must be at least 4 characters.")
                        else:
                            update_user_password(rec_user, rec_new_pwd.strip(), "EMERGENCY_RECOVERY")
                            st.success(f"Password for `{rec_user}` has been reset! You can now log in.")
            else:
                st.info(
                    "Tip: Add `ADMIN_RECOVERY_KEY = \"YourSecretCode\"` to Streamlit Secrets to enable "
                    "self-service emergency password recovery here."
                )

        with st.expander("Role Reference"):
            st.markdown("""
- **PMH / Admin (`admin`):** Full View + Status Summary + Ageing + User Password Control + Master Data Upload
- **PMG (`pmg`):** Master Sheet Builder from Go-Live Files + Drawing Upload
- **User 1 (`user1`):** Batch Upload (`wip_date` ONLY)
- **User 2 (`user2`):** Batch Upload (`plant prod date` ONLY)
- **User 3 (`user3`):** Batch Upload (`handover date` ONLY)
- **User 4 (`user4`):** Batch Upload (`rfd_date` ONLY)
- **User 5 (`user5`):** Batch Upload (`dispatch_date` ONLY)
            """)
    st.stop()

# ---------------------------------------------------------------------------
# 4. SIDEBAR & DATABASE EXPORT CONTROLS
# ---------------------------------------------------------------------------
current_user = st.session_state.username
if current_user not in USERS:
    st.session_state.logged_in = False
    st.session_state.username = None
    st.rerun()

user_info = USERS[current_user]
role = user_info["role"]
is_admin = role == "admin"
is_pmg = role == "pmg"

with st.sidebar:
    if os.path.exists(CLIENT_LOGO_FILE):
        st.image(CLIENT_LOGO_FILE, width=160)
    st.caption(f"**Client:** {CLIENT_NAME}")
    st.caption(f"**Storage:** {'Neon PostgreSQL (Persistent)' if is_postgres() else 'Local SQLite'}")
    st.header("👤 Session Info")
    st.success(f"**Logged in as:** {user_info['label']}")
    if st.button("🚪 Logout", use_container_width=True):
        st.session_state.logged_in = False
        st.session_state.username = None
        st.rerun()

    st.divider()
    st.header("💾 Export & Backup")

    master_df = load_master_df()
    audit_df = load_audit_log_df()

    if is_admin:
        summary_pivot_export = build_project_status_summary_pivot(master_df)
        export_sheets = {
            "Project_Master": master_df,
            "Project_Status_Summary": summary_pivot_export,
            "Audit_Log": audit_df
        }
        excel_export = to_excel_bytes(export_sheets)
        st.download_button(
            label="📥 Export Full Workbook (.xlsx)",
            data=excel_export,
            file_name=f"JSL_WIP_Export_{date.today()}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True
        )
    else:
        st.info("Full workbook export is restricted to PMH (Admin).")

# ---------------------------------------------------------------------------
# 5A. PMG MASTER SHEET BUILDER (SINGLE OR MULTIPLE GO-LIVE FILES)
# ---------------------------------------------------------------------------
def render_pmg_master_builder():
    st.subheader("🛠️ Master Sheet Builder — Extract Standardized Drawing Format from Go-Live Files")
    st.markdown(
        "Upload **one or multiple** Go-Live Excel workbooks (e.g., `1369P1 SMS CASTER.xlsx`) or CSV files. "
        "The tool automatically locates the **`MASTER`** tab, detects the header row, approximates slightly varying headers "
        "to match the 14 required columns (`sr_no` through `plant`), and lets you download or push the consolidated Master Sheet."
    )

    golive_files = st.file_uploader(
        "Select Single or Multiple Go-Live Files (.xlsx / .xls / .csv)",
        type=["xlsx", "xls", "csv"],
        accept_multiple_files=True,
        key="pmg_golive_multi_uploader"
    )

    if not golive_files:
        st.info("Upload one or more Go-Live files above to extract and standardize the `MASTER` sheet.")
        return

    extracted_frames = []
    running_sr = 1

    for idx, g_file in enumerate(golive_files):
        file_bytes = g_file.getvalue()
        fname = g_file.name
        default_proj = infer_project_code_from_filename(fname)

        with st.expander(f"📄 File {idx + 1}: `{fname}`", expanded=True):
            try:
                if fname.lower().endswith(".csv"):
                    selected_sheet = "CSV"
                    preview_df = pd.read_csv(io.BytesIO(file_bytes), header=None, nrows=15)
                else:
                    xls = pd.ExcelFile(io.BytesIO(file_bytes))
                    sheet_names = xls.sheet_names
                    master_idx = 0
                    for s_i, s_name in enumerate(sheet_names):
                        if s_name.strip().upper() == "MASTER":
                            master_idx = s_i
                            break

                    c_s1, c_s2 = st.columns([1.2, 1])
                    selected_sheet = c_s1.selectbox(
                        "Sheet to Extract From:",
                        sheet_names,
                        index=master_idx,
                        key=f"sheet_sel_{idx}_{fname}"
                    )
                    preview_df = pd.read_excel(io.BytesIO(file_bytes), sheet_name=selected_sheet, header=None, nrows=15)

                detected_hdr = detect_header_row(preview_df)
                c_h1, c_h2 = st.columns([1, 1.2])
                header_row_num = c_h1.number_input(
                    "Header Row Index (0-based, auto-detected):",
                    min_value=0,
                    max_value=max(0, len(preview_df) - 1),
                    value=int(detected_hdr),
                    key=f"hdr_row_{idx}_{fname}"
                )
                proj_override = c_h2.text_input(
                    "Fallback `project_no` (used if source has no project column):",
                    value=default_proj,
                    key=f"proj_ovr_{idx}_{fname}"
                ).strip()

                if fname.lower().endswith(".csv"):
                    full_raw_df = pd.read_csv(io.BytesIO(file_bytes), header=int(header_row_num), dtype=str)
                else:
                    full_raw_df = pd.read_excel(io.BytesIO(file_bytes), sheet_name=selected_sheet, header=int(header_row_num), dtype=str)

                source_cols = [str(c).strip() for c in full_raw_df.columns]
                full_raw_df.columns = source_cols
                auto_map = approximate_column_mapping(source_cols)

                st.markdown("##### 🔍 Header Approximation & Mapping Review")
                dropdown_options = ["(None / Leave Blank)"] + source_cols
                confirmed_map = {}

                map_rows_summary = []
                cols_grid = st.columns(2)
                for t_i, target_col in enumerate(MASTER_DATA_COLUMNS):
                    matched_src, conf_badge = auto_map[target_col]
                    default_idx = dropdown_options.index(matched_src) if matched_src in dropdown_options else 0

                    with cols_grid[t_i % 2]:
                        chosen = st.selectbox(
                            f"**{target_col}** — *{conf_badge}*",
                            options=dropdown_options,
                            index=default_idx,
                            key=f"map_{idx}_{fname}_{target_col}"
                        )
                    confirmed_map[target_col] = None if chosen == "(None / Leave Blank)" else chosen
                    map_rows_summary.append({
                        "Target Format Column": target_col,
                        "Matched Source Header": confirmed_map[target_col] or "—",
                        "Match Status": conf_badge if chosen == matched_src else "Manual Override"
                    })

                with st.expander("View Header Mapping Summary Table", expanded=False):
                    st.dataframe(pd.DataFrame(map_rows_summary), use_container_width=True, hide_index=True)

                clean_part_df = extract_standardized_df(
                    full_raw_df,
                    confirmed_map,
                    fallback_project_no=proj_override,
                    start_sr_no=running_sr
                )

                if clean_part_df.empty:
                    st.warning(f"No valid rows with non-blank `Batch No` extracted from `{fname}` ({selected_sheet}).")
                else:
                    st.success(
                        f"Extracted **{len(clean_part_df)}** rows from `{fname}` (`{selected_sheet}`) · "
                        f"Total Actual Wt: **{clean_part_df['actual_wt'].sum():.3f} MT**"
                    )
                    extracted_frames.append(clean_part_df)
                    running_sr += len(clean_part_df)

            except Exception as e:
                st.error(f"Error processing `{fname}`: {e}")

    if extracted_frames:
        st.divider()
        st.subheader("📦 Consolidated Master Sheet Preview (Ready for Drawing Upload)")

        combined_df = pd.concat(extracted_frames, ignore_index=True)
        combined_df = combined_df.drop_duplicates(subset=["Batch No"], keep="last").reset_index(drop=True)
        combined_df["sr_no"] = range(1, len(combined_df) + 1)

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Files Processed", len(extracted_frames))
        m2.metric("Unique Batches Extracted", len(combined_df))
        m3.metric("Total Qty", int(combined_df["qty"].sum()))
        m4.metric("Total Actual Wt (MT)", f"{combined_df['actual_wt'].sum():.3f}")

        st.dataframe(combined_df, use_container_width=True, height=380)

        col_dl, col_db = st.columns([1, 1.2])
        with col_dl:
            st.download_button(
                label="📥 Download Standardized Master Sheet (.xlsx)",
                data=to_excel_bytes({"Master_Drawing_Upload": combined_df}),
                file_name=f"Master_Drawing_Upload_{date.today()}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True
            )

        with col_db:
            if st.button("🚀 Save Extracted Master Data Directly to Database", type="primary", use_container_width=True):
                new_cnt, upd_cnt = upsert_master_records(combined_df, current_user)
                st.success(
                    f"Saved {len(combined_df)} standardized records ({new_cnt} new batches added, "
                    f"{upd_cnt} existing batches updated) while preserving all stage dates!"
                )
                st.rerun()

# ---------------------------------------------------------------------------
# 5B. MASTER DATA UPLOADER (STRICTLY NON-DATE COLUMNS FOR PMH & PMG)
# ---------------------------------------------------------------------------
def render_master_data_uploader(uploader_role_label, allow_full_replace=False):
    st.info(
        "🔒 **Stage Date Protection Active:** Any date columns (`wip_date`, `plant prod date`, `handover date`, "
        "`rfd_date`, `dispatch_date`) in the uploaded file are **automatically ignored**. "
        "Existing batches retain their recorded stage dates, and new batches start as **YTS** until updated by `user1`–`user5`."
    )

    sample_master = pd.DataFrame([
        {
            "sr_no": 1, "Batch No": "ABC123", "project_no": "201", "description": "CT-T1",
            "revision_no": "Rev-00", "drawing_no": "024014-Q", "element_type": "MAIN BEAM",
            "mark_no": "MB-1", "qty": 1, "part_sl_no": "1/2", "actual_wt": 2.268,
            "draw_total_wt": 2.268, "contractor": "MANOJ", "plant": "Plant-1"
        },
        {
            "sr_no": 2, "Batch No": "ABC124", "project_no": "201", "description": "CT-T1",
            "revision_no": "Rev-2", "drawing_no": "DP16004-VN02-90UHA-370005-UHASHEET-96/373",
            "element_type": "GIRDER", "mark_no": "1T10G53", "qty": 1, "part_sl_no": "2/2",
            "actual_wt": 3.862, "draw_total_wt": 3.862, "contractor": "CTF", "plant": "PLANT-2"
        }
    ])

    st.download_button(
        label="📄 Download Master / Drawing Upload Template (.xlsx)",
        data=to_excel_bytes({"Master_Data_Template": sample_master}),
        file_name="JSL_Master_Drawing_Template.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

    master_upload = st.file_uploader(
        f"Upload Master / Drawing Data File (.xlsx / .csv) — {uploader_role_label}",
        type=["xlsx", "xls", "csv"],
        key=f"master_up_{current_user}"
    )

    if allow_full_replace:
        upload_mode = st.radio(
            "Upload Mode:",
            ["Upsert (Add New Batches & Update Non-Date Fields)", "Initialize Fresh Master (Only when DB is empty)"],
            horizontal=True
        )
    else:
        upload_mode = "Upsert (Add New Batches & Update Non-Date Fields)"

    if master_upload is not None:
        try:
            if master_upload.name.endswith(".csv"):
                raw_df = pd.read_csv(master_upload, dtype={"Batch No": str, "project_no": str, "part_sl_no": str})
            else:
                raw_df = pd.read_excel(master_upload, dtype={"Batch No": str, "project_no": str, "part_sl_no": str})

            if "Batch No" not in raw_df.columns:
                st.error("Uploaded file must contain the `Batch No` column.")
                return

            ignored_dates = [c for c in DATE_COLUMNS if c in raw_df.columns and raw_df[c].notna().any()]
            if ignored_dates:
                st.warning(
                    f"⚠️ Detected date values in columns {ignored_dates}. Per governance rules, these date columns "
                    f"will be **ignored** so only `user1`–`user5` can update stage dates."
                )

            present_data_cols = [c for c in MASTER_DATA_COLUMNS if c in raw_df.columns]
            incoming_df = raw_df[present_data_cols].copy()
            incoming_df["Batch No"] = incoming_df["Batch No"].astype(str).str.strip()
            incoming_df = incoming_df[
                (incoming_df["Batch No"] != "") & (incoming_df["Batch No"].str.lower() != "nan")
            ].drop_duplicates(subset=["Batch No"], keep="last").copy()

            if incoming_df.empty:
                st.warning("No valid rows with non-blank `Batch No` found.")
                return

            if "part_sl_no" in incoming_df.columns:
                incoming_df["part_sl_no"] = incoming_df["part_sl_no"].apply(clean_part_sl_no).astype(str)
            if "sr_no" in incoming_df.columns:
                incoming_df["sr_no"] = pd.to_numeric(incoming_df["sr_no"], errors="coerce").fillna(0).astype(int)
            if "qty" in incoming_df.columns:
                incoming_df["qty"] = pd.to_numeric(incoming_df["qty"], errors="coerce").fillna(0).astype(int)
            if "actual_wt" in incoming_df.columns:
                incoming_df["actual_wt"] = pd.to_numeric(incoming_df["actual_wt"], errors="coerce").fillna(0.0)
            if "draw_total_wt" in incoming_df.columns:
                incoming_df["draw_total_wt"] = pd.to_numeric(incoming_df["draw_total_wt"], errors="coerce").fillna(0.0)

            current_master = load_master_df()
            existing_batches = set(current_master["Batch No"]) if not current_master.empty else set()
            new_batches = incoming_df[~incoming_df["Batch No"].isin(existing_batches)]
            updated_batches = incoming_df[incoming_df["Batch No"].isin(existing_batches)]

            c1, c2, c3 = st.columns(3)
            c1.metric("Total Rows in File", len(incoming_df))
            c2.metric("New Batches / Drawings to Add", len(new_batches))
            c3.metric("Existing Batches to Update (Dates Preserved)", len(updated_batches))

            st.dataframe(incoming_df.head(15), use_container_width=True)

            if st.button("🚀 Save Master / Drawing Data to Database", type="primary"):
                if upload_mode.startswith("Initialize") and not current_master.empty:
                    st.error("Database already contains records. Use Upsert mode so existing stage dates are preserved.")
                    return

                new_cnt, upd_cnt = upsert_master_records(incoming_df, current_user)
                st.success(
                    f"Saved {len(incoming_df)} records ({new_cnt} added, {upd_cnt} updated) "
                    f"with all stage dates preserved!"
                )
                st.rerun()
        except Exception as e:
            st.error(f"Failed to process file: {e}")

# ---------------------------------------------------------------------------
# 6. STAGE DATE BATCH UPLOADER (STRICTLY FOR USER 1 TO USER 5)
# ---------------------------------------------------------------------------
def render_stage_uploader(target_col, role_title):
    st.subheader(f"📤 Batch Date Upload — {role_title}")
    st.info(
        f"Upload an Excel (`.xlsx`) or CSV file containing **`Batch No`** and **`{target_col}`**. "
        f"Only `{target_col}` will be updated; all other dates and engineering fields remain locked."
    )

    sample_template = pd.DataFrame({
        "Batch No": ["ABC123", "ABC124"],
        target_col: [str(date.today()), str(date.today())]
    })
    st.download_button(
        label=f"📄 Download {target_col} Upload Template",
        data=to_excel_bytes({"Upload_Template": sample_template}),
        file_name=f"Template_{target_col.replace(' ', '_')}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

    uploaded_file = st.file_uploader(
        f"Select Excel/CSV file for {target_col}",
        type=["xlsx", "xls", "csv"],
        key=f"uploader_{target_col}"
    )

    if uploaded_file is not None:
        try:
            if uploaded_file.name.endswith(".csv"):
                up_df = pd.read_csv(uploaded_file, dtype={"Batch No": str})
            else:
                up_df = pd.read_excel(uploaded_file, dtype={"Batch No": str})
        except Exception as e:
            st.error(f"Error reading file: {e}")
            return

        if "Batch No" not in up_df.columns or target_col not in up_df.columns:
            st.error(f"Uploaded file must contain exact columns: `Batch No` and `{target_col}`.")
            st.write("Detected columns:", list(up_df.columns))
            return

        up_df["Batch No"] = up_df["Batch No"].astype(str).str.strip()
        up_df[target_col] = up_df[target_col].apply(clean_date_str)
        valid_up = up_df[
            (up_df["Batch No"] != "")
            & (up_df["Batch No"].str.lower() != "nan")
            & (up_df[target_col] != "")
        ].copy()
        valid_up = valid_up.drop_duplicates(subset=["Batch No"], keep="last")

        if valid_up.empty:
            st.warning(f"No valid rows found with non-blank `Batch No` and `{target_col}`.")
            return

        current_master = load_master_df()
        if current_master.empty:
            st.error("Project Master Sheet is empty. Ask PMH / PMG to upload the Master / Drawing sheet first.")
            return

        merged = pd.merge(
            valid_up[["Batch No", target_col]].rename(columns={target_col: "New_Date"}),
            current_master[["Batch No", "project_no", "mark_no", "qty", "actual_wt", target_col]].rename(columns={target_col: "Existing_Date"}),
            on="Batch No",
            how="left",
            indicator=True
        )

        matched = merged[merged["_merge"] == "both"].copy()
        unmatched = merged[merged["_merge"] == "left_only"].copy()
        already_dated = matched[matched["Existing_Date"] != ""]

        st.markdown("### 📊 Pre-Upload Reconciliation Summary")
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Matched Batches", len(matched), f"{len(unmatched)} Unmatched")
        m2.metric("Total Qty Uploaded", int(matched["qty"].sum()))
        m3.metric("Total Actual Wt Uploaded (MT)", f"{matched['actual_wt'].sum():.3f}")
        m4.metric(
            "Already Has Date (Overwrite)",
            f"{len(already_dated)} Batches",
            f"Qty: {int(already_dated['qty'].sum())} | Wt: {already_dated['actual_wt'].sum():.3f} MT",
            delta_color="inverse"
        )

        if len(unmatched) > 0:
            with st.expander(f"⚠️ {len(unmatched)} Batch Numbers Not Found in Master Sheet (Will be skipped)"):
                st.dataframe(unmatched[["Batch No", "New_Date"]], use_container_width=True)

        if len(already_dated) > 0:
            st.warning(
                f"⚠️ **Overwrite Alert:** **{len(already_dated)}** batches (Total Qty: **{int(already_dated['qty'].sum())}**, "
                f"Actual Wt: **{already_dated['actual_wt'].sum():.3f} MT**) ALREADY have a recorded `{target_col}`. "
                f"Confirm below if you want to overwrite those existing dates."
            )
            with st.expander("View Batches With Existing Dates"):
                st.dataframe(
                    already_dated[["Batch No", "project_no", "mark_no", "qty", "actual_wt", "Existing_Date", "New_Date"]],
                    use_container_width=True
                )

        if not matched.empty:
            st.markdown("#### Preview of Matched Records to Update")
            st.dataframe(
                matched[["Batch No", "project_no", "mark_no", "qty", "actual_wt", "Existing_Date", "New_Date"]],
                use_container_width=True,
                height=250
            )

            confirm_chk = st.checkbox(
                f"I have verified the uploaded Qty ({int(matched['qty'].sum())}) and Actual Wt ({matched['actual_wt'].sum():.3f} MT) "
                f"and confirm updating/overwriting `{target_col}` in Project Master.",
                key=f"confirm_{target_col}"
            )

            if st.button("✅ Confirm & Apply Update to Master Sheet", type="primary", disabled=not confirm_chk):
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                audit_rows = []
                date_map = {}

                for _, row in matched.iterrows():
                    b_no = row["Batch No"]
                    old_d = clean_date_str(row["Existing_Date"])
                    new_d = clean_date_str(row["New_Date"])
                    date_map[b_no] = new_d
                    if old_d != new_d:
                        audit_rows.append((current_user, ts, b_no, target_col, old_d, new_d))

                mask = current_master["Batch No"].isin(date_map.keys())
                current_master.loc[mask, target_col] = current_master.loc[mask, "Batch No"].map(date_map)
                save_master_df(current_master)
                write_audit_entries(audit_rows)

                st.success(
                    f"Updated `{target_col}` for {len(matched)} batches, logged {len(audit_rows)} changes to Audit Log, "
                    f"and recalculated statuses!"
                )
                st.rerun()

# ---------------------------------------------------------------------------
# 7. ROLE-BASED VIEW ROUTING
# ---------------------------------------------------------------------------
st.title(f"🏗️ {CLIENT_NAME} — WIP & Fabrication Control")

if role == "uploader":
    render_stage_uploader(user_info["target_col"], user_info["label"])
    st.stop()

if is_pmg:
    pmg_tab1, pmg_tab2 = st.tabs([
        "🛠️ 1. Master Sheet Builder (From Go-Live Files)",
        "📤 2. Upload Formatted Master / Drawing Sheet"
    ])
    with pmg_tab1:
        render_pmg_master_builder()
    with pmg_tab2:
        st.subheader("📐 PMG Portal — Upload Pre-Formatted Drawing / Plant / Contractor / Weight Sheet")
        render_master_data_uploader("PMG Engineering & Allocation", allow_full_replace=False)
    st.stop()

# Admin (PMH) Tabs
tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
    "📋 1. Project Master Sheet (PMH)",
    "📈 2. Project Status Summary",
    "📊 3. WIP Ageing — Project Wise",
    "🏭 4. WIP Ageing — Contractor Wise",
    "📜 5. Audit Log & Batch Timeline",
    "🔐 6. User Password Management"
])

with tab1:
    st.subheader("Project Master Sheet (PMH Upload & Live Fabrication Status)")

    with st.expander("📥 PMH Batch Upload / Update Project Master (Non-Date Fields)", expanded=master_df.empty):
        render_master_data_uploader("PMH / Admin", allow_full_replace=True)

    if not master_df.empty:
        st.divider()
        k1, k2, k3, k4, k5, k6 = st.columns(6)
        k1.metric("Total Batches", len(master_df))
        k2.metric("Total Qty", int(master_df["qty"].sum()))
        k3.metric("Total Wt (MT)", f"{master_df['actual_wt'].sum():.3f}")
        k4.metric("DISP Wt (MT)", f"{master_df[master_df['status'] == 'DISP']['actual_wt'].sum():.3f}")
        k5.metric("RFD Wt (MT)", f"{master_df[master_df['status'] == 'RFD']['actual_wt'].sum():.3f}")
        wip_only_wt = master_df[~master_df["status"].isin(["DISP", "YTS"])]["actual_wt"].sum()
        k6.metric("Active WIP Wt (MT)", f"{wip_only_wt:.3f}")

        f1, f2, f3 = st.columns(3)
        proj_filter = f1.multiselect("Filter Project No", sorted(master_df["project_no"].unique()))
        cont_filter = f2.multiselect("Filter Contractor", sorted(master_df["contractor"].dropna().unique()))
        stat_filter = f3.multiselect("Filter Status", ["DISP", "RFD", "B&P", "BLACK OK", "WIP", "YTS"])

        filtered_df = master_df.copy()
        if proj_filter:
            filtered_df = filtered_df[filtered_df["project_no"].isin(proj_filter)]
        if cont_filter:
            filtered_df = filtered_df[filtered_df["contractor"].isin(cont_filter)]
        if stat_filter:
            filtered_df = filtered_df[filtered_df["status"].isin(stat_filter)]

        st.dataframe(filtered_df, use_container_width=True, height=450)
    else:
        st.info("No records in Project Master yet. Use the PMH batch upload panel above to load data.")

with tab2:
    st.subheader("📈 Project Status Summary Pivot (Actual Wt in MT)")
    st.caption("Summarizes total Actual Weight (MT) across fabrication stages by Project Number.")

    summary_pivot = build_project_status_summary_pivot(master_df)
    if summary_pivot.empty:
        st.info("No records in Project Master yet to generate the Project Status Summary.")
    else:
        num_cols = [c for c in summary_pivot.columns if c != "PROJECT"]
        styled_summary = style_pivot_with_totals(summary_pivot, "PROJECT", "GRAND TOTAL", num_cols)
        st.dataframe(styled_summary, use_container_width=True, hide_index=True)

        st.download_button(
            label="📥 Export Project Status Summary (.xlsx)",
            data=to_excel_bytes({"Project_Status_Summary": summary_pivot}),
            file_name=f"Project_Status_Summary_{date.today()}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

with tab3:
    st.subheader("📊 WIP Ageing Analysis — Project Wise (Actual Wt in MT)")
    st.caption("Includes all items with a valid `wip_date` where `status` is NOT `DISP`. Ageing = Reference Date minus `wip_date`.")

    ref_date_proj = st.date_input("Ageing Reference Date", value=date.today(), key="ref_date_proj")
    proj_pivot = build_wip_ageing_pivot(master_df, "project_no", ref_date_proj)

    if proj_pivot.empty:
        st.info("No active WIP records available to calculate Project-wise ageing.")
    else:
        num_cols_proj = AGEING_BUCKETS + ["Total WIP Wt (MT)"]
        styled_proj_pivot = style_pivot_with_totals(proj_pivot, "project_no", "Total WIP Wt (MT)", num_cols_proj)
        st.dataframe(styled_proj_pivot, use_container_width=True, hide_index=True)

        st.download_button(
            label="📥 Export Project WIP Ageing (.xlsx)",
            data=to_excel_bytes({"WIP_Ageing_Project": proj_pivot}),
            file_name=f"WIP_Ageing_Project_{ref_date_proj}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        render_ageing_bucket_drilldown(master_df, ref_date_proj, "project_no", "Project", "proj_age")

with tab4:
    st.subheader("🏭 WIP Ageing Analysis — Contractor Wise (Actual Wt in MT)")
    st.caption("Includes all items with a valid `wip_date` where `status` is NOT `DISP`. Ageing = Reference Date minus `wip_date`.")

    ref_date_cont = st.date_input("Ageing Reference Date", value=date.today(), key="ref_date_cont")
    cont_pivot = build_wip_ageing_pivot(master_df, "contractor", ref_date_cont)

    if cont_pivot.empty:
        st.info("No active WIP records available to calculate Contractor-wise ageing.")
    else:
        num_cols_cont = AGEING_BUCKETS + ["Total WIP Wt (MT)"]
        styled_cont_pivot = style_pivot_with_totals(cont_pivot, "contractor", "Total WIP Wt (MT)", num_cols_cont)
        st.dataframe(styled_cont_pivot, use_container_width=True, hide_index=True)

        st.download_button(
            label="📥 Export Contractor WIP Ageing (.xlsx)",
            data=to_excel_bytes({"WIP_Ageing_Contractor": cont_pivot}),
            file_name=f"WIP_Ageing_Contractor_{ref_date_cont}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        render_ageing_bucket_drilldown(master_df, ref_date_cont, "contractor", "Contractor", "cont_age")

with tab5:
    st.subheader("📜 Batch Upload Audit Log & Chronological Timeline")
    st.caption("Tracks every batch upload modification across PMH, PMG, and User1–User5, including elapsed days between consecutive stage dates.")

    if audit_df.empty:
        st.info("No audit log entries recorded yet.")
    else:
        st.markdown("### 🕒 Batch Chronological Stage Timeline")
        audited_batches = sorted(audit_df["Batch No"].dropna().unique())
        selected_batch = st.selectbox("Select Batch No to inspect chronological changes:", audited_batches)

        if selected_batch:
            batch_history = audit_df[audit_df["Batch No"] == selected_batch].copy()
            batch_history = batch_history.sort_values(by=["timestamp", "id"], ascending=[True, True]).reset_index(drop=True)
            batch_history["Step"] = batch_history.index + 1

            batch_history["new_dt"] = pd.to_datetime(batch_history["new_date"], errors="coerce")
            batch_history["prev_new_dt"] = batch_history["new_dt"].shift(1)
            batch_history["prev_stage"] = batch_history["column_updated"].shift(1)

            def format_audit_elapsed(row):
                if pd.isna(row["new_dt"]):
                    return "—"
                if pd.isna(row["prev_new_dt"]):
                    return "First dated entry"
                days_diff = (row["new_dt"] - row["prev_new_dt"]).days
                return f"{days_diff} days (from {row['prev_stage']})"

            batch_history["Days Since Prev Date"] = batch_history.apply(format_audit_elapsed, axis=1)

            if not master_df.empty and selected_batch in master_df["Batch No"].values:
                m_row = master_df[master_df["Batch No"] == selected_batch].iloc[0]
                s1, s2, s3, s4 = st.columns(4)
                s1.metric("Current Status", m_row["status"])
                s2.metric("Project / Mark", f"{m_row['project_no']} / {m_row['mark_no']}")
                s3.metric("Contractor", m_row["contractor"] or "N/A")
                s4.metric("Actual Wt (MT)", f"{float(m_row['actual_wt']):.3f}")

                st.markdown("#### ⏱️ Stage-to-Stage Elapsed Days (Master Stage Dates)")
                stage_turnaround_df = build_stage_turnaround_df(m_row)
                st.dataframe(stage_turnaround_df, use_container_width=True, hide_index=True)

            st.markdown("#### 🗓️ Audit Upload Sequence & Consecutive Stage Date Delta")
            timeline_view = batch_history[[
                "Step", "timestamp", "column_updated", "old_date", "new_date", "Days Since Prev Date", "username"
            ]].rename(
                columns={
                    "timestamp": "Upload Timestamp",
                    "column_updated": "Field / Stage",
                    "old_date": "Previous Value",
                    "new_date": "Updated Value",
                    "username": "Updated By"
                }
            )
            st.dataframe(timeline_view, use_container_width=True, hide_index=True)

            for _, ev in batch_history.iterrows():
                prev_display = ev["old_date"] if ev["old_date"] else "*Blank*"
                new_display = ev["new_date"] if ev["new_date"] else "*Blank*"
                st.markdown(
                    f"**#{ev['Step']}** · `{ev['timestamp']}` · **`{ev['column_updated']}`**: "
                    f"{prev_display} $\\rightarrow$ **{new_display}** · "
                    f"*Elapsed: **{ev['Days Since Prev Date']}*** *(by `{ev['username']}`)*"
                )

        st.divider()

        st.markdown("### 📋 Complete Audit Log Table")
        af1, af2, af3 = st.columns(3)
        u_filter = af1.multiselect("Filter by User", sorted(audit_df["username"].unique()))
        c_filter = af2.multiselect("Filter by Field / Stage", sorted(audit_df["column_updated"].unique()))
        b_search = af3.text_input("Search Batch No").strip()

        filtered_audit = audit_df.copy()
        if u_filter:
            filtered_audit = filtered_audit[filtered_audit["username"].isin(u_filter)]
        if c_filter:
            filtered_audit = filtered_audit[filtered_audit["column_updated"].isin(c_filter)]
        if b_search:
            filtered_audit = filtered_audit[filtered_audit["Batch No"].str.contains(b_search, case=False, na=False)]

        st.metric("Matching Audit Records", len(filtered_audit))
        st.dataframe(filtered_audit, use_container_width=True, height=400)

        st.download_button(
            label="📥 Export Audit Log (.xlsx)",
            data=to_excel_bytes({"Audit_Log": filtered_audit}),
            file_name=f"JSL_Audit_Log_{date.today()}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

with tab6:
    st.subheader("🔐 User Password Management & Recovery Controls")
    st.markdown(
        "Update your own password (`admin`) or set new passwords for `pmg` and `user1`–`user5`. "
        "All changes are saved permanently in the database and recorded in the Audit Log."
    )

    col_pw_form, col_pw_table = st.columns([1, 1.35])

    with col_pw_form:
        st.markdown("#### ✏️ Change / Reset User Password")
        with st.form("admin_change_password_form"):
            target_u = st.selectbox(
                "Select User Account:",
                options=list(USERS.keys()),
                format_func=lambda u: f"{u} — {USERS[u]['label']}"
            )
            new_pw = st.text_input("New Password (minimum 4 characters)", type="password")
            confirm_pw = st.text_input("Confirm New Password", type="password")
            save_pw_btn = st.form_submit_button("💾 Update Password", type="primary", use_container_width=True)

            if save_pw_btn:
                if len(new_pw.strip()) < 4:
                    st.error("Password must be at least 4 characters long.")
                elif new_pw.strip() != confirm_pw.strip():
                    st.error("New password and confirmation do not match.")
                else:
                    update_user_password(target_u, new_pw.strip(), current_user)
                    st.success(f"Password for **`{target_u}`** updated successfully!")
                    st.rerun()

    with col_pw_table:
        st.markdown("#### 👥 Active Portal Accounts")
        user_rows = []
        for u_name, u_data in USERS.items():
            user_rows.append({
                "Username": u_name,
                "Role / Access Scope": u_data["label"],
                "Assigned Stage Column": u_data["target_col"] or "All Non-Date / Master",
                "Last Password Update": u_data.get("updated_at") or "Default"
            })
        st.dataframe(pd.DataFrame(user_rows), use_container_width=True, hide_index=True)

    st.divider()
    with st.expander("🛟 Disaster Recovery Guide: What if ALL users forget their passwords?"):
        st.markdown("""
1. **Method 1 — Master Recovery Key in Streamlit Secrets (Fastest Self-Service):**
  - In Streamlit Cloud (**App Settings → Secrets**) or your server environment variables, add:
     ```toml
     ADMIN_RECOVERY_KEY = "JSL-FabTech-Reset-2026"
     ```
- On the Login Screen, open **🔑 Forgot Password / Emergency Admin Recovery**, enter that recovery key, select `admin` (or any user), and set a new password immediately.
2. **Method 2 — Neon PostgreSQL SQL Editor (Direct Database Reset):**
- Log in to your Neon Console, open **SQL Editor**, and run:
     ```sql
     UPDATE app_users SET password = 'NewAdminPassword123' WHERE username = 'admin';
     ```
- Or run `DELETE FROM app_users;` and reload the Streamlit app to automatically restore all default credentials (`admin123`, `pmg123`, `user123`).
        """)
