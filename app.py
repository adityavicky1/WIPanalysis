import streamlit as st
import pandas as pd
import io
import os
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

# User Credentials & Strict Role Mapping
USERS = {
    "admin": {"password": "admin123", "role": "admin", "target_col": None, "label": "PMH / Admin (Full View + Master Data)"},
    "pmg":   {"password": "pmg123",   "role": "pmg",   "target_col": None, "label": "PMG (Drawing / Plant / Contractor / Weight)"},
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
    """
    Appends audit records to audit_log table.
    Each entry is a tuple: (username, timestamp, batch_no, column_updated, old_date, new_date)
    """
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
    """
    Auto-status priority hierarchy:
    1. dispatch_date not blank   -> DISP
    2. rfd_date not blank        -> RFD
    3. handover date not blank   -> B&P
    4. plant prod date not blank -> BLACK OK
    5. wip_date not blank        -> WIP
    6. Else                      -> YTS
    """
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
            - **PMG:** `pmg` / `pmg123` *(Upload New Drawings / Plant / Contractor / Weight; Stage Dates Locked)*
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
# 5. MASTER DATA UPLOADER (STRICTLY NON-DATE COLUMNS FOR PMH & PMG)
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
            "element_type": "GIRDER", "mark_no": "1T10G53", "qty": 1, "part_sl_no": "1/1",
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
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                audit_rows = []
                tracked_meta_cols = [c for c in ["drawing_no", "revision_no", "contractor", "plant", "actual_wt", "qty"] if c in incoming_df.columns]

                if current_master.empty:
                    for col in MASTER_COLUMNS:
                        if col not in incoming_df.columns:
                            incoming_df[col] = ""
                    final_df = recalculate_all_statuses(incoming_df)
                    for b_no in final_df["Batch No"]:
                        audit_rows.append((current_user, ts, b_no, "NEW_BATCH_CREATED", "", "YTS"))
                else:
                    if upload_mode.startswith("Initialize") and not current_master.empty:
                        st.error("Database already contains records. Use Upsert mode so existing stage dates are preserved.")
                        return

                    master_idx = current_master.set_index("Batch No")
                    for _, r in incoming_df.iterrows():
                        b_no = r["Batch No"]
                        if b_no in master_idx.index:
                            for col in present_data_cols:
                                if col == "Batch No":
                                    continue
                                old_val = str(master_idx.at[b_no, col]) if pd.notna(master_idx.at[b_no, col]) else ""
                                new_val = str(r[col]) if pd.notna(r[col]) else ""
                                if col in tracked_meta_cols and old_val != new_val:
                                    audit_rows.append((current_user, ts, b_no, col, old_val, new_val))
                                master_idx.at[b_no, col] = r[col]
                        else:
                            new_record = {col: "" for col in MASTER_COLUMNS if col != "Batch No"}
                            for col in present_data_cols:
                                if col != "Batch No":
                                    new_record[col] = r[col]
                            new_record["status"] = "YTS"
                            master_idx.loc[b_no] = new_record
                            audit_rows.append((current_user, ts, b_no, "NEW_BATCH_CREATED", "", "YTS"))

                    final_df = recalculate_all_statuses(master_idx.reset_index())

                save_master_df(final_df)
                write_audit_entries(audit_rows)
                st.success(
                    f"Saved {len(incoming_df)} records ({len(new_batches)} added, {len(updated_batches)} updated) "
                    f"to Neon PostgreSQL with all stage dates preserved!"
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
    st.subheader("📐 PMG Portal — New Drawing Addition & Plant / Contractor / Weight Allocation")
    render_master_data_uploader("PMG Engineering & Allocation", allow_full_replace=False)
    st.stop()

tab1, tab2, tab3, tab4 = st.tabs([
    "📋 1. Project Master Sheet (PMH)",
    "📊 2. WIP Ageing — Project Wise",
    "🏭 3. WIP Ageing — Contractor Wise",
    "📜 4. Audit Log & Batch Timeline"
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
    st.subheader("📊 WIP Ageing Analysis — Project Wise (Actual Wt in MT)")
    st.caption("Includes all items with a valid `wip_date` where `status` is NOT `DISP`. Ageing = Reference Date minus `wip_date`.")

    ref_date_proj = st.date_input("Ageing Reference Date", value=date.today(), key="ref_date_proj")
    proj_pivot = build_wip_ageing_pivot(master_df, "project_no", ref_date_proj)

    if proj_pivot.empty:
        st.info("No active WIP records available to calculate Project-wise ageing.")
    else:
        st.dataframe(
            proj_pivot.style.format({col: "{:.3f}" for col in AGEING_BUCKETS + ["Total WIP Wt (MT)"]}),
            use_container_width=True
        )
        st.download_button(
            label="📥 Export Project WIP Ageing (.xlsx)",
            data=to_excel_bytes({"WIP_Ageing_Project": proj_pivot}),
            file_name=f"WIP_Ageing_Project_{ref_date_proj}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

with tab3:
    st.subheader("🏭 WIP Ageing Analysis — Contractor Wise (Actual Wt in MT)")
    st.caption("Includes all items with a valid `wip_date` where `status` is NOT `DISP`. Ageing = Reference Date minus `wip_date`.")

    ref_date_cont = st.date_input("Ageing Reference Date", value=date.today(), key="ref_date_cont")
    cont_pivot = build_wip_ageing_pivot(master_df, "contractor", ref_date_cont)

    if cont_pivot.empty:
        st.info("No active WIP records available to calculate Contractor-wise ageing.")
    else:
        st.dataframe(
            cont_pivot.style.format({col: "{:.3f}" for col in AGEING_BUCKETS + ["Total WIP Wt (MT)"]}),
            use_container_width=True
        )
        st.download_button(
            label="📥 Export Contractor WIP Ageing (.xlsx)",
            data=to_excel_bytes({"WIP_Ageing_Contractor": cont_pivot}),
            file_name=f"WIP_Ageing_Contractor_{ref_date_cont}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

with tab4:
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
