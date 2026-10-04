import io
import re
import sqlite3

import pandas as pd
import streamlit as st

con = sqlite3.connect("attendance_v2.db", check_same_thread=False)
con.executescript("""
CREATE TABLE IF NOT EXISTS students(id TEXT PRIMARY KEY, name TEXT, email TEXT,
                                    year INTEGER, credits INTEGER);
CREATE TABLE IF NOT EXISTS sessions(id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT,
                                    type TEXT, hours REAL, UNIQUE(date, type));
CREATE TABLE IF NOT EXISTS attendance(session_id INTEGER, student_id TEXT,
                                      UNIQUE(session_id, student_id));
""")
SESSION_TYPES = ["Yoga", "PCW"]


def q(sql, params=()):
    return pd.read_sql(sql, con, params=params)


def student_hours():
    """Hours attended per student, split by session type. Recalculated every time."""
    return q("""
        SELECT s.id, s.name, s.email, s.year, s.credits,
               COALESCE(SUM(CASE WHEN se.type='Yoga' THEN se.hours END), 0) AS yoga_hours,
               COALESCE(SUM(CASE WHEN se.type='PCW'  THEN se.hours END), 0) AS pcw_hours,
               COUNT(se.id) AS sessions_attended
        FROM students s
        LEFT JOIN attendance a ON a.student_id = s.id
        LEFT JOIN sessions se ON se.id = a.session_id
        GROUP BY s.id ORDER BY s.id""")


def per_session():
    """How many students came to each session, split by year."""
    ps = q("""
        SELECT se.date, se.type, se.hours, COUNT(a.student_id) AS present,
               SUM(st.year = 1) AS year1, SUM(st.year = 2) AS year2, SUM(st.year = 3) AS year3
        FROM sessions se
        LEFT JOIN attendance a ON a.session_id = se.id
        LEFT JOIN students st ON st.id = a.student_id
        GROUP BY se.id ORDER BY se.date, se.type""")
    cols = ["present", "year1", "year2", "year3"]
    ps[cols] = ps[cols].fillna(0).astype(int)
    return ps


def year_summary():
    sh = student_hours()
    g = sh.groupby("year", dropna=False).agg(
        registered=("id", "count"),
        attended_at_least_once=("sessions_attended", lambda s: int((s > 0).sum())),
        avg_sessions=("sessions_attended", "mean"),
    ).reset_index()
    g["participation_%"] = (100 * g["attended_at_least_once"] / g["registered"]).round(1)
    g["avg_sessions"] = g["avg_sessions"].round(1)
    return g


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
        year_summary().to_excel(w, sheet_name="By year", index=False)
        matrix.to_excel(w, sheet_name="Attendance matrix", index=False)
    return buf.getvalue()


st.title("Session Attendance Tracker")
tabs = st.tabs(["1. Roster", "2. Mark attendance", "3. Analysis",
                "4. Results (end of semester)", "5. Credits", "6. Student lookup"])

# ---- 1. Roster: CSV with id, name, email, year (1/2/3) ----
with tabs[0]:
    f = st.file_uploader("Roster CSV (columns: id, name, email, year)", type="csv", key="roster")
    if f:
        df = pd.read_csv(f, dtype=str).fillna("")
        df.columns = [c.strip().lower() for c in df.columns]
        df["id"] = df["id"].str.strip().str.upper()
        if "year" not in df.columns:
            df["year"] = ""
        df["year"] = pd.to_numeric(df["year"], errors="coerce")
        rows = [(r.id, r.name, r.email, None if pd.isna(r.year) else int(r.year))
                for r in df.itertuples()]
        con.executemany(
            """INSERT INTO students(id, name, email, year) VALUES (?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name,
               email=excluded.email,
               year=COALESCE(excluded.year, students.year)""", rows)  # keeps credits
        con.commit()
        st.success(f"Loaded {len(df)} students")
    st.dataframe(q("SELECT id, name, email, year FROM students ORDER BY id"))

# ---- 2. Mark attendance ----
with tabs[1]:
    c1, c2, c3 = st.columns(3)
    date = c1.date_input("Session date").isoformat()
    stype = c2.selectbox("Session type", SESSION_TYPES)
    hours = c3.number_input("Duration (hours)", min_value=0.25, value=1.0, step=0.25)
    raw = st.text_area("Paste IDs (separated by spaces, commas or new lines)")
    ids = sorted(set(re.findall(r"[A-Za-z0-9]+", raw.upper())))
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

# ---- 3. Analysis ----
with tabs[2]:
    ps = per_session()
    if ps.empty:
        st.info("No sessions saved yet.")
    else:
        st.subheader("Attendance per session, by year")
        st.bar_chart(ps.assign(session=ps["date"] + " " + ps["type"])
                       .set_index("session")[["year1", "year2", "year3"]])
        st.subheader("Participation by year")
        st.dataframe(year_summary())

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

# ---- 5. Credits: upload at end of semester ----
with tabs[4]:
    st.write("At the end of the semester, upload a CSV with columns: id, credits")
    cf = st.file_uploader("Credits CSV", type="csv", key="credits")
    if cf:
        cd = pd.read_csv(cf, dtype=str)
        cd.columns = [c.strip().lower() for c in cd.columns]
        cd["id"] = cd["id"].str.strip().str.upper()
        cd["credits"] = pd.to_numeric(cd["credits"], errors="coerce")
        con.executemany("UPDATE students SET credits=? WHERE id=?",
                        [(None if pd.isna(r.credits) else int(r.credits), r.id)
                         for r in cd.itertuples()])
        con.commit()
        st.success(f"Updated credits for {len(cd)} rows")
    st.dataframe(q("SELECT id, name, year, credits FROM students ORDER BY id"))

# ---- 6. Lookup ----
with tabs[5]:
    sid_in = st.text_input("Enter student ID").strip().upper()
    if sid_in:
        d = q("""SELECT se.date, se.type, se.hours FROM attendance a
                 JOIN sessions se ON se.id = a.session_id
                 WHERE a.student_id = ? ORDER BY se.date""", (sid_in,))
        st.write(f"Present in {len(d)} session(s), {d['hours'].sum()} hours")
        st.dataframe(d)
