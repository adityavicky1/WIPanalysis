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

# User Credentials & Strict Role Mapping
USERS = {
    "admin": {"password": "admin123", "role": "admin", "target_col": None, "label": "PMH / Admin (Full View + Master Data)"},
    "pmg":   {"password": "pmg123",   "role": "pmg",   "target_col": None, "label": "PMG (Master Builder + Drawing / Plant / Contractor / Weight)"},
    "user1": {"password": "user123",  "role": "uploader", "target_col": "wip_date",        "label": "User 1 (WIP Date Only)"},
    "user2": {"password": "user123",  "role": "uploader", "target_col": "plant prod date", "label": "User 2 (Plant Prod Date Only)"},
    "user3": {"password": "user123",  "role": "uploader", "target_col": "handover date",   "label": "User 3 (Handover Date Only)"},
    "user4": {"password": "user123",  "role": "uploader", "target_col": "rfd_date",        "label": "User 4 (RFD Date Only)"},
    "user5": {"password": "user123",  "role": "uploader", "target_col": "dispatch_date",   "label": "User 5 (Dispatch Date Only)"},
}

AGEING_BUCKETS = ["0-7 Days", "8-15 Days", "16-30 Days", "30-60 Days", ">60 Days"]

# ---------------------------------------------------------------------------
# 2. DATABASE (NEON POSTGRESQL / SQLITE) & BUSINESS LOGIC HELPERS
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
        df["part_sl_no"] = df["part_sl_no"].astype(str)
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
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        for sheet_name, df in df_dict.items():
            df.to_excel(writer, index=False, sheet_name=sheet_name[:31])
    return output.getvalue()

# ---------------------------------------------------------------------------
# 2B. FUZZY HEADER MATCHING & MASTER SHEET EXTRACTOR HELPERS
# ---------------------------------------------------------------------------
def normalize_header(h):
    s = str(h).strip().lower()
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return s

def detect_header_row(preview_df):
    """
    Finds the true header row within the top 15 rows by counting how many cells
    resemble known structural fabrication column names (skipping top total/summary rows).
    """
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
    """
    Maps the 14 target MASTER_DATA_COLUMNS to the closest matching column in source_columns
    using exact alias checks followed by difflib fuzzy string similarity.
    Returns dict: {target_col: (matched_source_col_or_None, confidence_label)}
    """
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

    # Pass 1: Exact normalized alias match (in priority order of COLUMN_ALIASES)
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

    # Pass 2: Fuzzy approximation for any remaining unmatched target columns
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
    """
    Builds a clean 14-column DataFrame from raw_df using the confirmed column mapping.
    """
    out = pd.DataFrame(index=raw_df.index)

    for target_col in MASTER_DATA_COLUMNS:
        src_col = col_map.get(target_col)
        if src_col and src_col in raw_df.columns:
            out[target_col] = raw_df[src_col]
        else:
            out[target_col] = ""

    # Clean Batch No and filter out blank / summary / subtotal rows
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

    # Auto-populate project_no from filename prefix if project_no column was missing or blank
    out["project_no"] = out["project_no"].fillna("").astype(str).str.strip()
    out.loc[out["project_no"].isin(["", "nan", "None"]), "project_no"] = fallback_project_no

    # Clean numeric fields
    out["qty"] = pd.to_numeric(out["qty"], errors="coerce").fillna(1).astype(int)
    out["actual_wt"] = pd.to_numeric(out["actual_wt"], errors="coerce").fillna(0.0).round(5)
    out["draw_total_wt"] = pd.to_numeric(out["draw_total_wt"], errors="coerce")
    out["draw_total_wt"] = out["draw_total_wt"].fillna(out["actual_wt"]).round(5)

    # Clean text fields
    for txt_col in ["description", "revision_no", "drawing_no", "element_type", "mark_no", "part_sl_no", "contractor", "plant"]:
        out[txt_col] = out[txt_col].fillna("").astype(str).str.strip()
        out.loc[out[txt_col].isin(["nan", "None", "NaT"]), txt_col] = ""

    # If sr_no was missing or all zeros, generate sequential sr_no
    sr_numeric = pd.to_numeric(out["sr_no"], errors="coerce").fillna(0).astype(int)
    if (sr_numeric == 0).all():
        out["sr_no"] = range(start_sr_no, start_sr_no + len(out))
    else:
        out["sr_no"] = sr_numeric

    # Deduplicate by Batch No keeping last valid entry
    out = out.drop_duplicates(subset=["Batch No"], keep="last").reset_index(drop=True)
    return out[MASTER_DATA_COLUMNS]

def upsert_master_records(incoming_df, actor_username):
    """
    Upserts non-date Master Data records into project_master while strictly preserving
    all existing stage dates (wip_date -> dispatch_date) and writing audit trail logs.
    """
    current_master = load_master_df()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    audit_rows = []
    tracked_meta_cols = [
        c for c in ["drawing_no", "revision_no", "contractor", "plant", "actual_wt", "qty"]
        if c in incoming_df.columns
    ]

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
# 2C. WIP AGEING & TURNAROUND ANALYTICS HELPERS
# ---------------------------------------------------------------------------
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

def build_wip_ageing_pivot(df, group_col, ref_date):
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
    wip_df["age_days"] = (ref_ts - wip_df["wip_dt"]).dt.days.clip(lower=0)
    wip_df["Bucket"] = wip_df["age_days"].apply(get_ageing_bucket)
    wip_df[group_col] = wip_df[group_col].fillna("Unassigned").replace("", "Unassigned")

    pivot = pd.pivot_table(
        wip_df,
        values="actual_wt",
        index=group_col,
        columns="Bucket",
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

        with st.expander("Default Credentials Reference"):
            st.markdown("""
            - **PMH / Admin:** `admin` / `admin123` *(Full View + Master Data Upload; Stage Dates Locked)*
            - **PMG:** `pmg` / `pmg123` *(Master Sheet Builder from Go-Live Files + Drawing Upload; Stage Dates Locked)*
            - **User 1:** `user1` / `user123` *(Batch Upload: `wip_date` ONLY)*
            - **User 2:** `user2` / `user123` *(Batch Upload: `plant prod date` ONLY)*
            - **User 3:** `user3` / `user123` *(Batch Upload: `handover date` ONLY)*
            - **User 4:** `user4` / `user123` *(Batch Upload: `rfd_date` ONLY)*
            - **User 5:** `user5` / `user123` *(Batch Upload: `dispatch_date` ONLY)*
            """)
    st.stop()

# ---------------------------------------------------------------------------
# 4. SIDEBAR & DATABASE EXPORT CONTROLS
# ---------------------------------------------------------------------------
current_user = st.session_state.username
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
        export_sheets = {"Project_Master": master_df, "Audit_Log": audit_df}
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
                    # Auto-select 'MASTER' (case-insensitive) if available
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

                # Read full sheet with chosen header row
                if fname.lower().endswith(".csv"):
                    full_raw_df = pd.read_csv(io.BytesIO(file_bytes), header=int(header_row_num), dtype=str)
                else:
                    full_raw_df = pd.read_excel(io.BytesIO(file_bytes), sheet_name=selected_sheet, header=int(header_row_num), dtype=str)

                source_cols = [str(c).strip() for c in full_raw_df.columns]
                full_raw_df.columns = source_cols
                auto_map = approximate_column_mapping(source_cols)

                # Interactive Header Mapping Review & Override
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
