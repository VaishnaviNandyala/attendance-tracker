import datetime
import io
import re
import sqlite3

import pandas as pd
import streamlit as st

con = sqlite3.connect("attendance_v2.db", check_same_thread=False)
con.executescript("""
CREATE TABLE IF NOT EXISTS students(id TEXT PRIMARY KEY, name TEXT, email TEXT,
                                    credits INTEGER);
CREATE TABLE IF NOT EXISTS sessions(id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT,
                                    type TEXT, hours REAL, UNIQUE(date, type));
CREATE TABLE IF NOT EXISTS attendance(session_id INTEGER, student_id TEXT,
                                      UNIQUE(session_id, student_id));
""")
# NOTE: if your existing attendance_v2.db still has an old "year" column, it is
# simply ignored by this app (nothing reads or writes it).
SESSION_TYPES = ["Yoga", "PCW"]


def q(sql, params=()):
    return pd.read_sql(sql, con, params=params)


def parse_ids(raw):
    return sorted(set(re.findall(r"[A-Za-z0-9]+", raw.upper())))


def student_hours():
    """Hours attended per student, split by session type. Recalculated every time."""
    return q("""
        SELECT s.id, s.name, s.email, s.credits,
               COALESCE(SUM(CASE WHEN se.type='Yoga' THEN se.hours END), 0) AS yoga_hours,
               COALESCE(SUM(CASE WHEN se.type='PCW'  THEN se.hours END), 0) AS pcw_hours,
               COUNT(se.id) AS sessions_attended
        FROM students s
        LEFT JOIN attendance a ON a.student_id = s.id
        LEFT JOIN sessions se ON se.id = a.session_id
        GROUP BY s.id ORDER BY s.id""")


def per_session():
    """How many students came to each session."""
    ps = q("""
        SELECT se.date, se.type, se.hours, COUNT(a.student_id) AS present
        FROM sessions se
        LEFT JOIN attendance a ON a.session_id = se.id
        GROUP BY se.id ORDER BY se.date, se.type""")
    ps["present"] = ps["present"].fillna(0).astype(int)
    return ps


def results(yoga_pc, pcw_pc, rule):
    r = student_hours()
    r["credits"] = pd.to_numeric(r["credits"], errors="coerce")
    r["total_hours"] = r["yoga_hours"] + r["pcw_hours"]
    r["required_yoga"] = r["credits"] * yoga_pc
    r["required_pcw"] = r["credits"] * pcw_pc
    if rule.startswith("Both"):
        ok = (r["yoga_hours"] >= r["required_yoga"]) & (r["pcw_hours"] >= r["required_pcw"])
    else:
        ok = r["total_hours"] >= r["required_yoga"] + r["required_pcw"]
    r["result"] = ok.map({True: "P", False: "F"})
    r.loc[r["credits"].isna(), "result"] = "Pending"  # credits arrive at end of semester
    return r


def to_excel(res):
    long = q("""SELECT a.student_id AS id, se.date || ' ' || se.type AS session
                FROM attendance a JOIN sessions se ON se.id = a.session_id""")
    if long.empty:
        matrix = pd.DataFrame()
    else:
        matrix = (long.assign(p=1)
                  .pivot_table(index="id", columns="session", values="p",
                               aggfunc="max", fill_value=0).reset_index())
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        res.to_excel(w, sheet_name="Results", index=False)
        per_session().to_excel(w, sheet_name="Per session", index=False)
        matrix.to_excel(w, sheet_name="Attendance matrix", index=False)
    return buf.getvalue()


st.title("Session Attendance Tracker")
tabs = st.tabs(["1. Roster", "2. Mark attendance", "3. Analysis",
                "4. Results (end of semester)", "5. Credits", "6. Student lookup"])

# ---- 1. Roster: import CSV, then edit / add / rename / delete ----
with tabs[0]:
    st.subheader("Import roster")
    f = st.file_uploader("Roster CSV (columns: id, name, email)", type="csv", key="roster")
    # Import only when the button is pressed, so it never overwrites your edits
    # on a later rerun.
    if f and st.button("Import roster CSV"):
        df = pd.read_csv(f, dtype=str).fillna("")
        df.columns = [c.strip().lower() for c in df.columns]
        df["id"] = df["id"].str.strip().str.upper()
        for col in ["name", "email"]:
            if col not in df.columns:
                df[col] = ""
        rows = [(r.id, r.name, r.email) for r in df.itertuples()]
        con.executemany(
            """INSERT INTO students(id, name, email) VALUES (?,?,?)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name,
               email=excluded.email""", rows)  # keeps credits
        con.commit()
        st.success(f"Loaded {len(df)} students")

    st.subheader("Edit roster")
    st.caption("Change names/emails directly in the table. Tick 'delete' to remove a "
               "student (their attendance is removed too). Then press Save.")
    cur = q("SELECT id, name, email FROM students ORDER BY id")
    cur["delete"] = False
    edited = st.data_editor(cur, hide_index=True, disabled=["id"],
                            use_container_width=True, key="roster_editor")
    if st.button("Save roster changes"):
        for r in edited.itertuples():
            if r.delete:
                con.execute("DELETE FROM attendance WHERE student_id=?", (r.id,))
                con.execute("DELETE FROM students WHERE id=?", (r.id,))
            else:
                con.execute("UPDATE students SET name=?, email=? WHERE id=?",
                            (r.name or "", r.email or "", r.id))
        con.commit()
        st.toast("Roster updated")
        st.rerun()

    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Add a student")
        new_id = st.text_input("ID", key="add_id").strip().upper()
        new_name = st.text_input("Name", key="add_name").strip()
        new_email = st.text_input("Email", key="add_email").strip()
        if st.button("Add student"):
            if not new_id:
                st.error("ID is required.")
            elif con.execute("SELECT 1 FROM students WHERE id=?", (new_id,)).fetchone():
                st.error(f"{new_id} already exists.")
            else:
                con.execute("INSERT INTO students(id, name, email) VALUES (?,?,?)",
                            (new_id, new_name, new_email))
                con.commit()
                st.toast(f"Added {new_id}")
                st.rerun()
    with c2:
        st.subheader("Correct a student ID")
        st.caption("Renames the ID everywhere, so their attendance moves with them.")
        ids_now = list(cur["id"])
        if ids_now:
            old_id = st.selectbox("Existing ID", ids_now, key="rename_old")
            fixed_id = st.text_input("New ID", key="rename_new").strip().upper()
            if st.button("Rename ID"):
                if not fixed_id:
                    st.error("Enter the new ID.")
                elif con.execute("SELECT 1 FROM students WHERE id=?", (fixed_id,)).fetchone():
                    st.error(f"{fixed_id} already exists.")
                else:
                    con.execute("UPDATE students SET id=? WHERE id=?", (fixed_id, old_id))
                    con.execute("UPDATE attendance SET student_id=? WHERE student_id=?",
                                (fixed_id, old_id))
                    con.commit()
                    st.toast(f"{old_id} -> {fixed_id}")
                    st.rerun()

# ---- 2. Mark attendance (+ edit / delete saved sessions) ----
with tabs[1]:
    st.subheader("Add attendance")
    c1, c2, c3 = st.columns(3)
    date = c1.date_input("Session date").isoformat()
    stype = c2.selectbox("Session type", SESSION_TYPES)
    hours = c3.number_input("Duration (hours)", min_value=0.25, value=1.0, step=0.25)
    raw = st.text_area("Paste IDs (separated by spaces, commas or new lines)")
    ids = parse_ids(raw)
    roster = set(q("SELECT id FROM students")["id"])
    valid = [i for i in ids if i in roster]
    unknown = [i for i in ids if i not in roster]
    st.write(f"Recognised: **{len(valid)}**")
    if unknown:
        st.warning(f"Not in roster (check for typos): {', '.join(unknown)}")
    if st.button("Save attendance") and valid:
        con.execute("INSERT OR IGNORE INTO sessions(date, type, hours) VALUES (?,?,?)",
                    (date, stype, hours))
        con.execute("UPDATE sessions SET hours=? WHERE date=? AND type=?", (hours, date, stype))
        sid = con.execute("SELECT id FROM sessions WHERE date=? AND type=?",
                          (date, stype)).fetchone()[0]
        con.executemany("INSERT OR IGNORE INTO attendance VALUES (?,?)",
                        [(sid, i) for i in valid])
        con.commit()
        st.success(f"Saved {len(valid)} students for {stype} on {date}")
    st.subheader("Sessions so far")
    st.dataframe(per_session())

    st.divider()
    st.subheader("Edit or delete a saved session")
    sess = q("SELECT id, date, type, hours FROM sessions ORDER BY date DESC, type")
    if sess.empty:
        st.info("No sessions saved yet.")
    else:
        labels = {int(r.id): f"{r.date} - {r.type}" for r in sess.itertuples()}
        pick = st.selectbox("Choose a session", list(labels), format_func=labels.get)
        srow = sess[sess["id"] == pick].iloc[0]
        cur_ids = list(q("SELECT student_id FROM attendance WHERE session_id=? "
                         "ORDER BY student_id", (pick,))["student_id"])
        # Keys include the current data so the boxes refresh after every change.
        tag = f"{pick}_{len(cur_ids)}_{hash(tuple(cur_ids))}"
        e1, e2, e3 = st.columns(3)
        new_date = e1.date_input("Date", value=datetime.date.fromisoformat(srow["date"]),
                                 key=f"ed_date_{tag}").isoformat()
        new_type = e2.selectbox("Type", SESSION_TYPES,
                                index=SESSION_TYPES.index(srow["type"])
                                if srow["type"] in SESSION_TYPES else 0,
                                key=f"ed_type_{tag}")
        new_hours = e3.number_input("Duration (hours)", min_value=0.25,
                                    value=float(srow["hours"]), step=0.25,
                                    key=f"ed_hours_{tag}")
        new_raw = st.text_area("IDs present in this session (edit, add or remove)",
                               value="\n".join(cur_ids), height=200, key=f"ed_ids_{tag}")
        new_ids = parse_ids(new_raw)
        ok_ids = [i for i in new_ids if i in roster]
        bad_ids = [i for i in new_ids if i not in roster]
        st.write(f"Recognised: **{len(ok_ids)}**")
        if bad_ids:
            st.warning(f"Not in roster, will be skipped: {', '.join(bad_ids)}")
        if st.button("Update this session"):
            try:
                con.execute("UPDATE sessions SET date=?, type=?, hours=? WHERE id=?",
                            (new_date, new_type, new_hours, pick))
                con.execute("DELETE FROM attendance WHERE session_id=?", (pick,))
                con.executemany("INSERT OR IGNORE INTO attendance VALUES (?,?)",
                                [(pick, i) for i in ok_ids])
                con.commit()
                st.toast("Session updated")
                st.rerun()
            except sqlite3.IntegrityError:
                con.rollback()
                st.error(f"A {new_type} session on {new_date} already exists. "
                         "Pick a different date/type, or edit that session instead.")

        st.markdown("**Delete this session**")
        confirm = st.checkbox(f"Yes, delete {labels[pick]} and all its attendance",
                              key=f"del_ok_{tag}")
        if st.button("Delete session", disabled=not confirm):
            con.execute("DELETE FROM attendance WHERE session_id=?", (pick,))
            con.execute("DELETE FROM sessions WHERE id=?", (pick,))
            con.commit()
            st.toast("Session deleted")
            st.rerun()

# ---- 3. Analysis ----
with tabs[2]:
    ps = per_session()
    if ps.empty:
        st.info("No sessions saved yet.")
    else:
        st.subheader("Students present per session")
        st.bar_chart(ps.assign(session=ps["date"] + " " + ps["type"])
                       .set_index("session")[["present"]])
        st.subheader("Sessions attended per student")
        st.dataframe(student_hours()[["id", "name", "yoga_hours", "pcw_hours",
                                      "sessions_attended"]])

# ---- 4. Results: P/F only once credits are available ----
with tabs[3]:
    c1, c2 = st.columns(2)
    yoga_pc = c1.number_input("Yoga hours per credit", min_value=0.0, value=9.0)
    pcw_pc = c2.number_input("PCW hours per credit", min_value=0.0, value=5.0)
    rule = st.radio("Pass rule", ["Both yoga and PCW hours must be met", "Total hours only"])
    res = results(yoga_pc, pcw_pc, rule)
    st.caption("Students without credits show 'Pending'. Add credits in the Credits tab.")
    st.dataframe(res)
    st.download_button("Download Excel", to_excel(res), "attendance_report.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

# ---- 5. Credits: upload at end of semester, or edit by hand ----
with tabs[4]:
    st.write("At the end of the semester, upload a CSV with columns: id, credits")
    cf = st.file_uploader("Credits CSV", type="csv", key="credits")
    if cf and st.button("Import credits CSV"):
        cd = pd.read_csv(cf, dtype=str)
        cd.columns = [c.strip().lower() for c in cd.columns]
        cd["id"] = cd["id"].str.strip().str.upper()
        cd["credits"] = pd.to_numeric(cd["credits"], errors="coerce")
        con.executemany("UPDATE students SET credits=? WHERE id=?",
                        [(None if pd.isna(r.credits) else int(r.credits), r.id)
                         for r in cd.itertuples()])
        con.commit()
        st.success(f"Updated credits for {len(cd)} rows")

    st.subheader("Edit credits")
    cr = q("SELECT id, name, credits FROM students ORDER BY id")
    cr["credits"] = pd.to_numeric(cr["credits"], errors="coerce")
    cr_ed = st.data_editor(cr, hide_index=True, disabled=["id", "name"],
                           use_container_width=True, key="credits_editor",
                           column_config={"credits": st.column_config.NumberColumn(
                               "credits", min_value=0, step=1)})
    if st.button("Save credits"):
        con.executemany("UPDATE students SET credits=? WHERE id=?",
                        [(None if pd.isna(r.credits) else int(r.credits), r.id)
                         for r in cr_ed.itertuples()])
        con.commit()
        st.toast("Credits saved")
        st.rerun()

# ---- 6. Lookup ----
with tabs[5]:
    sid_in = st.text_input("Enter student ID").strip().upper()
    if sid_in:
        d = q("""SELECT se.date, se.type, se.hours FROM attendance a
                 JOIN sessions se ON se.id = a.session_id
                 WHERE a.student_id = ? ORDER BY se.date""", (sid_in,))
        st.write(f"Present in {len(d)} session(s), {d['hours'].sum()} hours")
        st.dataframe(d)
