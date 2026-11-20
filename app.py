import streamlit as st
import pandas as pd
import sqlite3
import io
import os
from datetime import datetime, date

# ---------------------------------------------------------------------------
# 1. PAGE CONFIG & CONSTANTS
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="FabTech WIP & Fabrication Control",
    page_icon="🏗️",
    layout="wide"
)

DB_FILE = "fabtech_wip.db"

MASTER_COLUMNS = [
    "sr_no", "Batch No", "project_no", "description", "revision_no",
    "drawing_no", "element_type", "mark_no", "qty", "part_sl_no",
    "actual_wt", "draw_total_wt", "contractor", "plant", "status",
    "wip_date", "plant prod date", "handover date", "rfd_date", "dispatch_date"
]

DATE_COLUMNS = [
    "wip_date", "plant prod date", "handover date", "rfd_date", "dispatch_date"
]

AUDIT_COLUMNS = [
    "id", "username", "timestamp", "Batch No", "column_updated", "old_date", "new_date"
]

# User Credentials & Role Mapping
USERS = {
    "admin": {"password": "admin123", "role": "admin", "target_col": None, "label": "Project Manager / Admin"},
    "user1": {"password": "user123", "role": "uploader", "target_col": "wip_date", "label": "User 1 (WIP Date)"},
    "user2": {"password": "user123", "role": "uploader", "target_col": "plant prod date", "label": "User 2 (Plant Prod Date)"},
    "user3": {"password": "user123", "role": "uploader", "target_col": "handover date", "label": "User 3 (Handover Date)"},
    "user4": {"password": "user123", "role": "uploader", "target_col": "rfd_date", "label": "User 4 (RFD Date)"},
    "user5": {"password": "user123", "role": "uploader", "target_col": "dispatch_date", "label": "User 5 (Dispatch Date)"},
}

AGEING_BUCKETS = ["0-7 Days", "8-15 Days", "16-30 Days", "30-60 Days", ">60 Days"]

# ---------------------------------------------------------------------------
# 2. DATABASE & BUSINESS LOGIC HELPERS
# ---------------------------------------------------------------------------
def get_connection():
    return sqlite3.connect(DB_FILE, check_same_thread=False)

def init_db():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
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
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            "Batch No" TEXT NOT NULL,
            column_updated TEXT NOT NULL,
            old_date TEXT,
            new_date TEXT
        )
    """)
    conn.commit()
    conn.close()

def write_audit_entries(entries):
    """
    Appends audit records to SQLite audit_log table.
    Each entry is a tuple: (username, timestamp, batch_no, column_updated, old_date, new_date)
    """
    if not entries:
        return
    conn = get_connection()
    cursor = conn.cursor()
    cursor.executemany(
        """
        INSERT INTO audit_log (username, timestamp, "Batch No", column_updated, old_date, new_date)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        entries
    )
    conn.commit()
    conn.close()

def load_audit_log_df():
    conn = get_connection()
    df = pd.read_sql_query('SELECT * FROM audit_log ORDER BY id DESC', conn)
    conn.close()
    return df

def clean_date_str(val):
    """Normalizes dates to YYYY-MM-DD string or empty string if blank/NaT."""
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
    1. dispatch_date not blank -> DISP
    2. rfd_date not blank      -> RFD
    3. handover date not blank -> B&P
    4. plant prod date not blank -> BLACK OK
    5. wip_date not blank      -> WIP
    6. Else                    -> YTS
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
    conn = get_connection()
    df = pd.read_sql_query("SELECT * FROM project_master ORDER BY sr_no ASC", conn)
    conn.close()
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
    df = df[MASTER_COLUMNS]
    conn = get_connection()
    df.to_sql("project_master", conn, if_exists="replace", index=False)
    conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_batch_no ON project_master ("Batch No")')
    conn.commit()
    conn.close()

def to_excel_bytes(df_dict):
    """Exports one or multiple DataFrames to an Excel byte buffer."""
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
    """
    Filters active WIP items (not DISP, and has a valid wip_date)
    and aggregates actual_wt across ageing buckets with Grand Total row & column.
    """
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

# ---------------------------------------------------------------------------
# 3. AUTHENTICATION UI
# ---------------------------------------------------------------------------
init_db()

if "logged_in" not in st.session_state:
    st.session_state.logged_in = False
    st.session_state.username = None

if not st.session_state.logged_in:
    st.title("🏗️ FabTech WIP & Fabrication Control System")
    st.markdown("Please log in with your assigned credentials.")

    col1, col2, col3 = st.columns([1, 1.2, 1])
    with col2:
        with st.form("login_form"):
            uname = st.text_input("Username (admin, user1 - user5)").strip().lower()
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
            - **Admin:** `admin` / `admin123` *(Full access to all tabs, Master Upload, Audit Log & DB Backup)*
            - **User 1:** `user1` / `user123` *(Batch Upload: `wip_date`)*
            - **User 2:** `user2` / `user123` *(Batch Upload: `plant prod date`)*
            - **User 3:** `user3` / `user123` *(Batch Upload: `handover date`)*
            - **User 4:** `user4` / `user123` *(Batch Upload: `rfd_date`)*
            - **User 5:** `user5` / `user123` *(Batch Upload: `dispatch_date`)*
            """)
    st.stop()

# ---------------------------------------------------------------------------
# 4. SIDEBAR & DATABASE EXPORT / RESTORE CONTROLS
# ---------------------------------------------------------------------------
current_user = st.session_state.username
user_info = USERS[current_user]
is_admin = user_info["role"] == "admin"

with st.sidebar:
    st.header("👤 Session Info")
    st.success(f"**Logged in as:** {user_info['label']}")
    if st.button("🚪 Logout", use_container_width=True):
        st.session_state.logged_in = False
        st.session_state.username = None
        st.rerun()

    st.divider()
    st.header("💾 Database & Export")

    master_df = load_master_df()
    audit_df = load_audit_log_df()

    export_sheets = {"Project_Master": master_df}
    if is_admin:
        export_sheets["Audit_Log"] = audit_df

    excel_export = to_excel_bytes(export_sheets)
    st.download_button(
        label="📥 Export Workbook (.xlsx)",
        data=excel_export,
        file_name=f"FabTech_WIP_Export_{date.today()}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True
    )

    if os.path.exists(DB_FILE):
        with open(DB_FILE, "rb") as f:
            db_bytes = f.read()
        st.download_button(
            label="🗄️ Export SQLite DB (.db)",
            data=db_bytes,
            file_name=f"fabtech_wip_backup_{date.today()}.db",
            mime="application/octet-stream",
            use_container_width=True
        )

    if is_admin:
        st.subheader("Restore Database (.db)")
        uploaded_db = st.file_uploader("Upload .db backup file", type=["db"], key="db_restore_uploader")
        if uploaded_db is not None:
            if st.button("⚠️ Overwrite & Restore DB", use_container_width=True):
                with open(DB_FILE, "wb") as f:
                    f.write(uploaded_db.getbuffer())
                init_db()
                st.success("Database restored successfully!")
                st.rerun()

# ---------------------------------------------------------------------------
# 5. STAGE DATE BATCH UPLOADER FUNCTION (USED BY USER1-5 & ADMIN)
# ---------------------------------------------------------------------------
def render_stage_uploader(target_col, role_title):
    st.subheader(f"📤 Batch Date Upload — {role_title}")
    st.info(
        f"Upload an Excel (`.xlsx`) or CSV file containing **`Batch No`** and **`{target_col}`**. "
        f"The system will verify quantities and weights against existing dates before overwriting and log every change."
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
            st.error("Project Master Sheet is currently empty. Ask Admin to upload Master Sheet first.")
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
# 6. ROLE-BASED VIEW ROUTING
# ---------------------------------------------------------------------------
st.title("🏗️ FabTech Project Management & WIP Analysis")

# === VIEW A: USER 1 TO USER 5 (UPLOAD ONLY) ===
if not is_admin:
    target_column = user_info["target_col"]
    render_stage_uploader(target_column, user_info["label"])
    st.stop()

# === VIEW B: ADMIN (ALL TABS ACCESS) ===
tab1, tab2, tab3, tab4, tab5 = st.tabs([
    "📋 1. Project Master Sheet",
    "📤 2. Stage Date Batch Upload (Admin)",
    "📊 3. WIP Ageing — Project Wise",
    "🏭 4. WIP Ageing — Contractor Wise",
    "📜 5. Audit Log"
])

# ---------------------------------------------------------------------------
# TAB 1: PROJECT MASTER SHEET (BATCH UPLOAD & VIEW)
# ---------------------------------------------------------------------------
with tab1:
    st.subheader("Project Master Sheet (Batch Upload & Live Status)")

    with st.expander("📥 Batch Upload / Update Project Master Sheet", expanded=master_df.empty):
        st.markdown("""
        Upload an Excel (`.xlsx`) or CSV file with the exact schema:  
        `sr_no`, `Batch No`, `project_no`, `description`, `revision_no`, `drawing_no`, `element_type`, `mark_no`, `qty`, `part_sl_no`, `actual_wt`, `draw_total_wt`, `contractor`, `plant`, `status`, `wip_date`, `plant prod date`, `handover date`, `rfd_date`, `dispatch_date`
        """)

        sample_master = pd.DataFrame([
            {
                "sr_no": 1, "Batch No": "ABC123", "project_no": "201", "description": "CT-T1",
                "revision_no": "Rev-00", "drawing_no": "024014-Q", "element_type": "MAIN BEAM",
                "mark_no": "MB-1", "qty": 1, "part_sl_no": "1/2", "actual_wt": 2.268,
                "draw_total_wt": 2.268, "contractor": "MANOJ", "plant": "Plant-1", "status": "DISP",
                "wip_date": "2026-10-01", "plant prod date": "", "handover date": "",
                "rfd_date": "2026-10-28", "dispatch_date": "2026-11-03"
            },
            {
                "sr_no": 2, "Batch No": "ABC124", "project_no": "201", "description": "CT-T1",
                "revision_no": "Rev-2", "drawing_no": "DP16004-VN02-90UHA-370005-UHASHEET-96/373",
                "element_type": "GIRDER", "mark_no": "1T10G53", "qty": 1, "part_sl_no": "1/1",
                "actual_wt": 3.862, "draw_total_wt": 3.862, "contractor": "CTF", "plant": "PLANT-2",
                "status": "RFD", "wip_date": "2018-12-10", "plant prod date": "", "handover date": "",
                "rfd_date": "2026-11-01", "dispatch_date": ""
            }
        ])

        st.download_button(
            label="📄 Download Sample Master Template (.xlsx)",
            data=to_excel_bytes({"Master_Template": sample_master}),
            file_name="Project_Master_Template.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

        master_upload = st.file_uploader("Upload Project Master File (.xlsx / .csv)", type=["xlsx", "xls", "csv"], key="master_up")
        upload_mode = st.radio(
            "Upload Mode:",
            ["Upsert (Update matching Batch No & Add new Batch No)", "Replace Entire Master Database"],
            horizontal=True
        )

        if master_upload is not None:
            try:
                if master_upload.name.endswith(".csv"):
                    new_m_df = pd.read_csv(master_upload, dtype={"Batch No": str, "project_no": str, "part_sl_no": str})
                else:
                    new_m_df = pd.read_excel(master_upload, dtype={"Batch No": str, "project_no": str, "part_sl_no": str})

                for col in MASTER_COLUMNS:
                    if col not in new_m_df.columns:
                        new_m_df[col] = ""

                new_m_df["Batch No"] = new_m_df["Batch No"].astype(str).str.strip()
                new_m_df = new_m_df[(new_m_df["Batch No"] != "") & (new_m_df["Batch No"].str.lower() != "nan")].copy()
                new_m_df["qty"] = pd.to_numeric(new_m_df["qty"], errors="coerce").fillna(0).astype(int)
                new_m_df["actual_wt"] = pd.to_numeric(new_m_df["actual_wt"], errors="coerce").fillna(0.0)
                new_m_df["draw_total_wt"] = pd.to_numeric(new_m_df["draw_total_wt"], errors="coerce").fillna(0.0)
                new_m_df = recalculate_all_statuses(new_m_df)

                st.write(f"**Preview:** {len(new_m_df)} rows | Total Qty: **{new_m_df['qty'].sum()}** | Total Actual Wt: **{new_m_df['actual_wt'].sum():.3f} MT**")
                st.dataframe(new_m_df.head(10), use_container_width=True)

                if st.button("🚀 Save Master Sheet to Database", type="primary"):
                    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    audit_rows = []
                    existing_lookup = {}
                    if not master_df.empty:
                        existing_lookup = master_df.set_index("Batch No").to_dict(orient="index")

                    for _, r in new_m_df.iterrows():
                        b_no = r["Batch No"]
                        prev_row = existing_lookup.get(b_no, {})
                        for dcol in DATE_COLUMNS:
                            old_d = clean_date_str(prev_row.get(dcol, ""))
                            new_d = clean_date_str(r.get(dcol, ""))
                            if old_d != new_d:
                                audit_rows.append((current_user, ts, b_no, dcol, old_d, new_d))

                    if upload_mode.startswith("Replace") or master_df.empty:
                        save_master_df(new_m_df)
                    else:
                        existing_idx = master_df.set_index("Batch No")
                        incoming_idx = new_m_df.set_index("Batch No")
                        combined = incoming_idx.combine_first(existing_idx).reset_index()
                        save_master_df(combined)

                    write_audit_entries(audit_rows)
                    st.success(f"Project Master Sheet updated, {len(audit_rows)} date changes logged, and statuses recalculated!")
                    st.rerun()
            except Exception as e:
                st.error(f"Failed to process Master Sheet: {e}")

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
        st.info("No records in Project Master yet. Use the batch upload panel above to load data.")

# ---------------------------------------------------------------------------
# TAB 2: ADMIN STAGE DATE UPLOADER
# ---------------------------------------------------------------------------
with tab2:
    selected_stage = st.selectbox(
        "Select Stage Date Column to Batch Upload:",
        DATE_COLUMNS,
        format_func=lambda c: f"{c} ({[u['label'] for u in USERS.values() if u['target_col'] == c][0]})"
    )
    render_stage_uploader(selected_stage, f"Admin Override ({selected_stage})")

# ---------------------------------------------------------------------------
# TAB 3: WIP AGEING — PROJECT WISE
# ---------------------------------------------------------------------------
with tab3:
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

# ---------------------------------------------------------------------------
# TAB 4: WIP AGEING — CONTRACTOR WISE
# ---------------------------------------------------------------------------
with tab4:
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

# ---------------------------------------------------------------------------
# TAB 5: AUDIT LOG (ADMIN ONLY)
# ---------------------------------------------------------------------------
with tab5:
    st.subheader("📜 Batch Upload Audit Log")
    st.caption("Tracks every batch upload date modification: `username`, `timestamp`, `Batch No`, `column_updated`, `old_date`, and `new_date`.")

    if audit_df.empty:
        st.info("No audit log entries recorded yet.")
    else:
        af1, af2, af3 = st.columns(3)
        u_filter = af1.multiselect("Filter by User", sorted(audit_df["username"].unique()))
        c_filter = af2.multiselect("Filter by Column Updated", sorted(audit_df["column_updated"].unique()))
        b_search = af3.text_input("Search Batch No").strip()

        filtered_audit = audit_df.copy()
        if u_filter:
            filtered_audit = filtered_audit[filtered_audit["username"].isin(u_filter)]
        if c_filter:
            filtered_audit = filtered_audit[filtered_audit["column_updated"].isin(c_filter)]
        if b_search:
            filtered_audit = filtered_audit[filtered_audit["Batch No"].str.contains(b_search, case=False, na=False)]

        st.metric("Matching Audit Records", len(filtered_audit))
        st.dataframe(filtered_audit, use_container_width=True, height=450)

        st.download_button(
            label="📥 Export Audit Log (.xlsx)",
            data=to_excel_bytes({"Audit_Log": filtered_audit}),
            file_name=f"FabTech_Audit_Log_{date.today()}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
