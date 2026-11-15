# -*- coding: utf-8 -*-
"""
CorpoPulse - Dashboard KPI (Streamlit)
Install  : python -m pip install streamlit pandas
Jalankan : python -m streamlit run main.py --server.port 7890

Login awal : admin / admin123   (ganti lewat tombol ☰ di pojok kanan atas)
Data disimpan di SQLite (corpopulse.db) pada folder yang sama dengan main.py.
Folder _cp_matrix dan _cp_acts dibuat otomatis (berisi komponen tabel yang bisa diklik).

Hak akses:
- admin : akses penuh (KPI Matrix, aktivitas, implementation, tanggal batas, manajemen akun).
- user  : terikat pada satu divisi (FA ... CF) dan boleh dibatasi hingga satu departemen.
          Hanya bisa mengisi KPI Matrix pada kolom divisinya dan mengisi notes/status
          pada KPI Implementation untuk divisi (dan departemen) miliknya, selama belum
          melewati tanggal batas yang diatur admin. Dashboard lain hanya bisa dilihat.
"""
import os
import html
import sqlite3
import hashlib
import secrets
from contextlib import closing, nullcontext
from datetime import datetime, date

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

# ----------------------------------------------------------------------------
# KONFIGURASI
# ----------------------------------------------------------------------------
st.set_page_config(page_title="CorpoPulse", page_icon="📈", layout="wide")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("CP_DATA_DIR", BASE_DIR)
os.makedirs(DATA_DIR, exist_ok=True)
DB = os.path.join(DATA_DIR, "corpopulse.db")
COMP_DIR = os.path.join(BASE_DIR, "_cp_matrix")
ACT_COMP_DIR = os.path.join(BASE_DIR, "_cp_acts")

INK = "#0D1B2A"
ACCENT = "#00B894"
PAPER = "#EEF2F7"
LINE = "#DDE3EC"
MUTED = "#5B6B7F"
SELECT_BORDER = "#8A9BB0"   # warna border dropdown box

DEPTS = ["FA", "MKT", "QA", "PROD1", "PROD2", "ENG", "PPIC", "PS", "PROC", "HR", "CF"]
BASE = ["category", "nomor", "activity", "pic"]
COLS = BASE + [d.lower() for d in DEPTS]

# kode: (kelas bentuk, simbol teks, keterangan)
ROLES = {
    "A": ("a", "●", "Accountable"),   # bulat biru
    "R": ("r", "★", "Responsible"),   # bintang hijau
    "C": ("c", "■", "Consulted"),     # kotak oranye
    "I": ("i", "◆", "Informed"),      # belah ketupat abu
}
CYCLE = ["", "A", "R", "C", "I"]
CAT_COLORS = ["#0F766E", "#1E6FE8", "#B45309", "#7C3AED", "#BE185D", "#4D7C0F"]

MENU = ["KPI Matrix", "KPI Activities", "KPI Implementation", "Summary Dashboard", "Cascading Map"]
SUBTITLE = {
    "KPI Matrix": "Peran setiap divisi pada aktivitas strategis perusahaan.",
    "KPI Activities": "Aktivitas tiap divisi yang ditulis dari simbol pada KPI Matrix.",
    "KPI Implementation": "Catatan dan status implementasi aktivitas per bulan, lengkap dengan ringkasan per divisi.",
    "Summary Dashboard": "Presentase pencapaian implementasi aktivitas di seluruh kategori dan per kategori.",
    "Cascading Map": "Peta strategi satu department: pilih divisi, department, dan tahun untuk menampilkan petanya.",
}

MAX_WORDS = 300
START_YEAR = 2026   # tahun paling awal pada filter Cascading Map
ALL_DEPT = "(Semua department pada divisi)"

# KPI Implementation
MONTHS = ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli", "Agustus",
          "September", "Oktober", "November", "Desember"]
STATUS_KEYS = ["", "implemented", "not_implemented"]
STATUS_LABEL = {"": "— Pilih", "implemented": "Implemented", "not_implemented": "Not Implemented"}
C_IMPL, C_NOT, C_BLANK = "#1FA34A", "#E5484D", "#C9D1DC"   # hijau, merah, abu
# Pilihan departemen per divisi (tulisan "Dept." di akhir nama). Ubah daftar ini bila struktur berubah.
DEPT_OPTIONS = {
    "FA": ["Treasury Dept.", "Financial Planning Analysis Dept.", "Accounting Dept."],
    "MKT": ["Marketing 4W & Export Dept.", "Marketing 2W Dept."],
    "QA": ["QA Inhouse Dept.", "QA Incoming Dept.", "New Model & Customer Support Dept."],
    "PROD1": ["Fork & Assy Dept.", "Casting Dept."],
    "PROD2": ["4W & Spring Dept.", "Steering Stem Dept.", "DC & ROD 2W Dept."],
    "ENG": ["FF & ST Stem Dept.", "Special Project & RC Dept.", "4W & Product Engineering Dept."],
    "PPIC": ["Planning Dept.", "Warehouse Dept."],
    "PS": ["Maintenance 4W & Factory Support Dept.", "Maintenance 2W Dept."],
    "PROC": ["Purchasing Dept.", "Procurement Engineering Dept."],
    "HR": ["Information Technology Dept.", "General Affair Dept.",
           "Human Capital & Compensation Benefit Dept."],
    "CF": ["Internal Audit", "Corporate", "Business Control", "Management System", "Legal",
           "Environment Health & Safety"],   # CF tanpa "Dept."
}
DEFAULT_DEPTS = DEPT_OPTIONS["FA"]


def S(fn, *a, **k):
    """Lebar penuh, kompatibel dengan berbagai versi Streamlit."""
    try:
        return fn(*a, width="stretch", **k)
    except Exception:
        return fn(*a, use_container_width=True, **k)


def esc(t):
    return html.escape(str(t)).replace("$", "&#36;")


def tok(s):
    return (s.replace("__I__", INK).replace("__A__", ACCENT).replace("__P__", PAPER)
            .replace("__L__", LINE).replace("__M__", MUTED).replace("__B__", SELECT_BORDER))


# ----------------------------------------------------------------------------
# HAK AKSES
# ----------------------------------------------------------------------------
def cur_user():
    return st.session_state.get("user") or {}


def is_admin_user(user=None):
    return (user if user is not None else cur_user()).get("role") == "admin"


def can_fill(user, div, dept=None):
    """Admin: semua. User: hanya divisinya, dan bila dibatasi per department, hanya department itu.
    dept=None berarti hanya memeriksa divisi (mis. klik simbol pada KPI Matrix)."""
    if user.get("role") == "admin":
        return True
    if not div or user.get("division") != div:
        return False
    ud = user.get("department") or ""
    return (not ud) or dept is None or dept == ud


def edit_divisions(user):
    """Daftar divisi yang simbolnya boleh diklik pada KPI Matrix."""
    if user.get("role") == "admin":
        return list(DEPTS)
    return [user["division"]] if user.get("division") in DEPTS else []


# ----------------------------------------------------------------------------
# CSS BERSAMA (dipakai halaman utama dan komponen tabel)
# ----------------------------------------------------------------------------
SHAPE_CSS = """
.sh { display:inline-block; vertical-align:middle; }
.sh.a { width:15px; height:15px; border-radius:50%; background:#1E6FE8; }
.sh.r { width:19px; height:19px; background:#1FA34A;
  clip-path:polygon(50% 0%,61% 35%,98% 35%,68% 57%,79% 91%,50% 70%,21% 91%,32% 57%,2% 35%,39% 35%); }
.sh.c { width:14px; height:14px; border-radius:3px; background:#F58220; }
.sh.i { width:18px; height:18px; background:#8A94A3; clip-path:polygon(50% 0%,100% 50%,50% 100%,0% 50%); }
"""

TABLE_CSS = """
.mx-wrap { background:#fff; border:1px solid __L__; border-radius:16px; overflow:auto; max-height:66vh;
  box-shadow:0 1px 2px rgba(13,27,42,.05); }
table.mx { border-collapse:separate; border-spacing:0; table-layout:fixed; width:100%; min-width:1380px; }
.mx th { position:sticky; top:0; z-index:2; background:__I__; color:#fff; font-weight:700; font-size:.82rem;
  padding:14px 8px; text-align:center; letter-spacing:.01em; }
.mx th.l { text-align:left; padding-left:16px; }
.mx td { padding:12px 8px; border-bottom:1px solid #EDF1F6; font-size:.92rem; color:__I__; vertical-align:middle; background:#fff; }
.mx td.l { text-align:left; padding-left:16px; line-height:1.45; }
.mx td.n, .mx td.rc { text-align:center; }
.mx td.n { font-weight:700; color:__M__; }
.mx td.cat { font-weight:800; border-left:5px solid var(--c); background:var(--bg); border-right:1px solid #EDF1F6; }
.mx tr:hover td:not(.cat) { background:#F4F9F8; }
.mx td.empty { text-align:center; padding:42px 10px; color:__M__; font-weight:500; }
.mx tr:last-child td { border-bottom:none; }
 .pill { display:inline-block; padding:3px 12px; border-radius:20px; background:#E8EEF6; font-weight:700; font-size:.8rem; color:__I__; }
.div-logo { display:inline-flex; align-items:center; justify-content:center; width:36px; height:36px; border-radius:10px; background:__I__; color:#fff; font-size:.72rem; font-weight:800; letter-spacing:.02em; box-shadow:0 2px 6px rgba(13,27,42,.14); }
.div-logo-wrap { display:flex; align-items:center; justify-content:center; gap:8px; }
.div-logo-text { font-size:.78rem; font-weight:700; color:__I__; }
.sym-wrap { display:flex; flex-direction:column; align-items:center; justify-content:center; gap:4px; }
.sym-name { font-size:.72rem; font-weight:700; color:__M__; }
"""

CSS = tok("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap');

.stApp, .stApp p, .stApp label, .stApp h1, .stApp h2, .stApp h3, .stApp h4,
.stApp button, .stApp input, .stApp textarea, .stApp li, .stApp a,
.stApp [data-baseweb="select"] div, .mx, .mx * {
  font-family:'Plus Jakarta Sans','Segoe UI',system-ui,sans-serif;
}
.stApp { background:__P__; }
.block-container { padding-top:1.5rem; padding-bottom:2rem; max-width:1560px; }
#MainMenu, footer, .stDeployButton, [data-testid="stToolbarActions"], [data-testid="stDecoration"] { display:none !important; }
header[data-testid="stHeader"] { background:transparent; }
""" + SHAPE_CSS + TABLE_CSS + """
.legend { display:flex; gap:28px; flex-wrap:wrap; align-items:center; background:#fff; border:1px solid __L__;
  border-radius:14px; padding:14px 22px; margin-top:16px; }
.legend span.it { display:flex; align-items:center; gap:10px; font-weight:700; font-size:.92rem; color:__I__; }

/* ---------- sidebar ---------- */
[data-testid="stSidebar"] { background:__I__; border-right:1px solid rgba(255,255,255,.06); }
[data-testid="stSidebar"] * { color:#E8EEF6; }
.brand { display:flex; align-items:center; gap:12px; padding:4px 2px 22px; border-bottom:1px solid rgba(255,255,255,.10); margin-bottom:22px; }
.brand b { font-size:1.55rem; font-weight:800; letter-spacing:-.02em; color:#fff; line-height:1; }
.menu-title { font-size:.82rem; font-weight:600; color:#8FA3BB !important; margin:0 4px 8px; }
[data-testid="stSidebar"] [role="radiogroup"] { gap:6px; }
[data-testid="stSidebar"] label[data-baseweb="radio"] { width:100%; padding:11px 14px; border-radius:10px; cursor:pointer;
  border-left:4px solid transparent; transition:background .15s; }
[data-testid="stSidebar"] label[data-baseweb="radio"] > div:first-child { display:none; }
[data-testid="stSidebar"] label[data-baseweb="radio"] p { font-weight:600; font-size:1rem; }
[data-testid="stSidebar"] label[data-baseweb="radio"]:hover { background:rgba(255,255,255,.06); }
[data-testid="stSidebar"] label[data-baseweb="radio"]:has(input:checked) { background:rgba(0,184,148,.16); border-left-color:__A__; }
[data-testid="stSidebar"] label[data-baseweb="radio"]:has(input:checked) p { color:#fff; font-weight:700; }
.me { display:flex; align-items:center; gap:12px; margin-top:34px; padding:12px; border-radius:12px; background:rgba(255,255,255,.06); }
.me .nm { font-weight:700; font-size:.92rem; color:#fff; line-height:1.2; }
.me .rl { font-size:.78rem; color:#8FA3BB !important; }
.av { flex:none; width:38px; height:38px; border-radius:50%; background:__A__; color:__I__ !important; font-weight:800;
  display:flex; align-items:center; justify-content:center; font-size:1rem; }
.av.lg { width:46px; height:46px; font-size:1.2rem; }

/* ---------- judul & ikon akun ---------- */
.ptitle { font-size:2.15rem; font-weight:800; letter-spacing:-.03em; color:__I__; line-height:1.1; margin:0; }
.psub { color:__M__; font-size:.98rem; margin:6px 0 0; font-weight:500; }
[data-testid="stPopover"] { display:flex; justify-content:flex-end; }
[data-testid="stPopover"] button { border:1.5px solid __L__; background:#fff; color:__I__; border-radius:12px;
  font-size:1.35rem; font-weight:700; line-height:1; padding:.35rem .85rem; min-height:46px; }
[data-testid="stPopover"] button:hover { border-color:__A__; color:__A__; }
.acc { display:flex; align-items:center; gap:12px; margin-bottom:8px; }
.acc .nm { font-weight:800; color:__I__; font-size:1.02rem; line-height:1.15; }
.acc .rl { display:inline-block; font-size:.74rem; font-weight:700; padding:1px 10px; border-radius:20px; background:__I__; color:#fff; margin-top:3px; }
.pf { display:grid; grid-template-columns:150px 1fr; gap:10px 14px; margin-top:14px; padding:14px 16px; background:__P__; border-radius:12px; }
.pf .k { color:__M__; font-weight:600; font-size:.86rem; }
.pf .v { color:__I__; font-weight:700; font-size:.92rem; }
.lock { color:#B3261E; font-weight:700; font-size:.84rem; }

/* ---------- kartu ringkasan ---------- */
.tiles { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:14px; margin:20px 0 22px; }
.tile { background:#fff; border:1px solid __L__; border-radius:14px; padding:14px 18px; }
.tile .v { font-size:1.9rem; font-weight:800; color:__I__; letter-spacing:-.02em; line-height:1.15; }
.tile .k { font-size:.84rem; color:__M__; font-weight:600; }
@media (max-width:900px){ .tiles { grid-template-columns:repeat(2,minmax(0,1fr)); } }
.hint { color:__M__; font-size:.9rem; font-weight:500; padding-top:10px; }

/* ---------- KPI Activities: kotak divisi ---------- */
[class*="st-key-tile_"] button { min-height:78px; font-size:1.4rem; font-weight:800; border-radius:14px; letter-spacing:.01em; }
[class*="st-key-tile_"] button[kind="secondary"], [class*="st-key-tile_"] button[data-testid="stBaseButton-secondary"] {
  background:#fff; border:1px solid __L__; color:__I__; }
[class*="st-key-tile_"] button[kind="secondary"]:hover, [class*="st-key-tile_"] button[data-testid="stBaseButton-secondary"]:hover {
  border-color:__A__; color:__A__; }
.tc { text-align:center; color:__M__; font-size:.82rem; font-weight:600; margin:2px 0 14px; }
.sec { font-weight:800; color:__I__; font-size:1.15rem; margin:22px 0 12px; letter-spacing:-.01em; }

/* ---------- dialog aktivitas ---------- */
.ch { display:flex; align-items:flex-start; gap:14px; background:__P__; border-radius:12px; padding:14px 16px; margin-bottom:14px; }
.ch .t { font-weight:800; color:__I__; font-size:1rem; line-height:1.35; }
.ch .s { color:__M__; font-size:.86rem; font-weight:500; margin-top:2px; }
.wc { font-size:.84rem; font-weight:700; color:__M__; margin:-6px 0 10px; }
.wc.bad { color:#B3261E; }

/* ---------- tombol & input ---------- */
.stButton button, [data-testid="stFormSubmitButton"] button { border-radius:11px; font-weight:700; min-height:44px; }
.stButton button[kind="primary"], [data-testid="stFormSubmitButton"] button[kind="primary"],
.stButton button[data-testid="stBaseButton-primary"] { background:__I__; border-color:__I__; color:#fff; }
.stButton button[kind="primary"]:hover, .stButton button[data-testid="stBaseButton-primary"]:hover { background:__A__; border-color:__A__; color:#04251E; }
[data-baseweb="input"], [data-baseweb="select"] > div, [data-baseweb="textarea"] { border-radius:10px !important; }
[data-testid="stForm"] { border:1px solid __L__; border-radius:14px; background:#fff; }
div[role="dialog"] h2 { font-weight:800; letter-spacing:-.02em; }

/* ---------- dropdown box: border jelas supaya terlihat sebagai dropdown ---------- */
[data-testid="stSelectbox"] [data-baseweb="select"] > div,
div[role="dialog"] [data-baseweb="select"] > div {
  border:1.5px solid __B__ !important; background:#fff !important; border-radius:10px !important;
  box-shadow:0 1px 2px rgba(13,27,42,.06); cursor:pointer; transition:border-color .15s, box-shadow .15s;
}
[data-testid="stSelectbox"] [data-baseweb="select"] > div:hover,
div[role="dialog"] [data-baseweb="select"] > div:hover { border-color:__A__ !important; }
[data-testid="stSelectbox"] [data-baseweb="select"]:focus-within > div,
div[role="dialog"] [data-baseweb="select"]:focus-within > div {
  border-color:__A__ !important; box-shadow:0 0 0 3px rgba(0,184,148,.18); }
[data-testid="stSelectbox"] [data-baseweb="select"] svg { color:__I__; }
[data-baseweb="popover"] ul[role="listbox"] { border:1.5px solid __B__; border-radius:10px; }

/* ---------- KPI Implementation ---------- */
.im-h { background:__I__; color:#fff; font-weight:700; font-size:.74rem; text-align:center; padding:10px 2px; border-radius:8px; }
.im-h.l { text-align:left; padding-left:12px; }
.im-r { font-weight:800; font-size:.88rem; color:__I__; padding:8px 0 0 4px; }
.st-key-impl_tbl [data-testid="stForm"] { padding:16px 14px; overflow-x:auto; }
.st-key-impl_tbl textarea { font-size:.76rem; line-height:1.35; padding:6px 7px; }
.st-key-impl_tbl [data-baseweb="select"] div { font-size:.7rem; }
.card { background:#fff; border:1px solid __L__; border-radius:16px; padding:18px 22px; box-shadow:0 1px 2px rgba(13,27,42,.05); }
.card .ct { font-weight:800; font-size:1.02rem; color:__I__; }
.card .cs { font-size:.82rem; color:__M__; font-weight:600; margin:2px 0 16px; }
.card .none { padding:40px 10px; text-align:center; color:__M__; font-weight:500; }
.dwrap { display:flex; align-items:center; gap:28px; flex-wrap:wrap; }
.donut { width:190px; height:190px; border-radius:50%; position:relative; flex:none; }
.donut .hole { position:absolute; inset:30px; background:#fff; border-radius:50%; display:flex; flex-direction:column;
  align-items:center; justify-content:center; }
.donut .hole b { font-size:1.9rem; font-weight:800; color:__I__; line-height:1.1; }
.donut .hole span { font-size:.76rem; color:__M__; font-weight:600; }
.dl { display:flex; flex-direction:column; gap:12px; }
.dl .row { display:flex; align-items:center; gap:10px; font-weight:700; font-size:.9rem; color:__I__; }
.dl .sw { width:14px; height:14px; border-radius:4px; flex:none; }
.dl .row small { color:__M__; font-weight:600; font-size:.8rem; }
.bars { display:flex; gap:26px; justify-content:center; align-items:flex-start; overflow-x:auto; padding-bottom:4px; }
.bg { display:flex; flex-direction:column; align-items:center; min-width:160px; max-width:200px; }
.bb { display:flex; gap:8px; height:220px; padding-top:22px; box-sizing:border-box; border-bottom:2px solid __L__; }
.bw { position:relative; width:38px; height:100%; }
.bar { position:absolute; left:0; right:0; bottom:0; height:var(--h); border-radius:7px 7px 0 0; }
.bv { position:absolute; left:-6px; right:-6px; text-align:center; font-size:.78rem; font-weight:800; color:__I__;
  bottom:calc(var(--h) + 3px); }
.bl { margin-top:8px; font-size:.76rem; font-weight:700; color:__I__; text-align:center; line-height:1.3; }
.bleg { display:flex; gap:20px; justify-content:center; flex-wrap:wrap; margin-top:16px; font-size:.84rem; font-weight:700; color:__I__; }
.bleg span { display:flex; align-items:center; gap:8px; }
.bleg i { width:14px; height:14px; border-radius:4px; display:inline-block; }

.st-key-impl_tbl [data-testid="stDateInput"] input { font-size:.7rem; padding-left:8px; padding-right:4px; }

/* ---------- Summary Dashboard ---------- */
.ovr { display:flex; align-items:center; gap:40px; flex-wrap:wrap; }
.donut.lg { width:230px; height:230px; }
.donut.lg .hole { inset:38px; }
.donut.lg .hole b { font-size:2.5rem; }
.donut.lg .hole span { font-size:.95rem; font-weight:800; color:__I__; }
.cgrid { display:grid; grid-template-columns:repeat(auto-fill,minmax(250px,1fr)); gap:14px; }
.cc { background:#fff; border:1px solid __L__; border-left:6px solid var(--c); border-radius:14px; padding:16px 18px; }
.cc .cn { font-weight:800; color:__I__; font-size:.98rem; line-height:1.3; }
.cc .pc { font-size:2.4rem; font-weight:800; color:var(--c); line-height:1.1; margin-top:8px; letter-spacing:-.02em; }
.cc .fr { font-size:.84rem; font-weight:700; color:__M__; margin-top:2px; }
.pbar { height:8px; border-radius:6px; background:#E8EEF6; margin-top:12px; overflow:hidden; }
.pbar > i { display:block; height:100%; border-radius:6px; background:var(--c); }

.kr { display:grid; grid-template-columns:minmax(90px,150px) 1fr 86px; align-items:center; gap:12px; padding:7px 0; }
.kr .kn { font-weight:700; font-size:.86rem; color:__I__; line-height:1.25; }
.kr .kb { height:14px; border-radius:8px; background:#E8EEF6; overflow:hidden; }
.kr .kb i { display:block; height:100%; border-radius:8px; }
.kr .kv { font-weight:800; font-size:.9rem; color:__I__; text-align:right; }
.kr .kv small { color:__M__; font-weight:700; font-size:.74rem; margin-left:5px; }

/* ---------- Cascading Map ---------- */
.cm { margin-bottom:36px; }
.cm-title { background:#fff; border:2px solid __I__; border-radius:6px; text-align:center; padding:14px 10px; margin-bottom:10px; }
.cm-title b { display:block; font-size:1.4rem; font-weight:800; color:__I__; letter-spacing:.01em; line-height:1.25; }
.cm-title span { display:block; font-size:1.15rem; font-weight:800; color:__M__; margin-top:2px; }
.cm-band { display:flex; border:1px solid var(--c); border-radius:6px; margin-bottom:8px; background:var(--bg); overflow:hidden; }
.cm-v { flex:none; width:56px; background:var(--c); display:flex; align-items:center; justify-content:center; padding:8px 0; }
.cm-v span { writing-mode:vertical-rl; transform:rotate(180deg); color:#fff; font-weight:800; font-size:.95rem;
  letter-spacing:.02em; text-align:center; }
.cm-body { flex:1; display:flex; flex-wrap:wrap; justify-content:space-around; align-items:flex-start; gap:22px 30px;
  padding:22px 20px 20px; min-height:120px; }
.cm-n { width:290px; max-width:100%; display:flex; flex-direction:column; align-items:center; }
.cm-o { position:relative; z-index:1; width:250px; max-width:100%; min-height:62px; padding:10px 30px 10px 22px; box-sizing:border-box;
  border-radius:50%; background:radial-gradient(ellipse at 30% 25%, rgba(255,255,255,.40), rgba(255,255,255,0) 60%), var(--c);
  display:flex; align-items:center; justify-content:center; text-align:center; color:#fff; font-weight:700; font-size:.8rem;
  line-height:1.25; box-shadow:0 3px 8px rgba(13,27,42,.28); }
.cm-o .cm-t { display:-webkit-box; -webkit-line-clamp:3; -webkit-box-orient:vertical; overflow:hidden; }
.cm-b { position:absolute; top:-14px; right:-4px; width:34px; height:34px; border-radius:50%; background:#fff;
  border:2px solid var(--c); display:flex; align-items:center; justify-content:center; box-shadow:0 2px 5px rgba(13,27,42,.25); }
.cm-l { margin-top:-10px; width:100%; background:#fff; border:1px solid var(--c); border-radius:4px; padding:16px 12px 8px;
  box-sizing:border-box; }
.cm-a { font-size:.76rem; color:__I__; line-height:1.35; padding:2px 0; display:-webkit-box; -webkit-line-clamp:2;
  -webkit-box-orient:vertical; overflow:hidden; }
.cm-a b { color:var(--c); margin-right:4px; }
.cm-e { color:__M__; font-weight:600; font-size:.86rem; align-self:center; }

/* ---------- login ---------- */
.hero { background:linear-gradient(150deg,__I__ 0%,#16324D 100%); border-radius:22px; padding:44px 40px; color:#fff; min-height:470px;
  display:flex; flex-direction:column; justify-content:space-between; }
.hero .b { display:flex; align-items:center; gap:14px; }
.hero .b b { font-size:2rem; font-weight:800; letter-spacing:-.03em; }
.hero h2 { color:#fff; font-size:2rem; font-weight:800; letter-spacing:-.03em; line-height:1.15; margin:0 0 10px; padding:0; }
.hero p { color:#B7C6D8; font-size:1rem; margin:0; max-width:380px; line-height:1.55; }
.hero .lg { display:grid; grid-template-columns:1fr 1fr; gap:12px 18px; margin-top:6px; }
.hero .lg span { display:flex; align-items:center; gap:10px; font-weight:600; color:#fff; font-size:.92rem; }
.login-h { font-size:1.7rem; font-weight:800; letter-spacing:-.02em; color:__I__; margin:14px 0 4px; }
.login-s { color:__M__; margin-bottom:18px; font-weight:500; }
</style>
""")


def pulse_icon(size=38):
    return (f'<svg width="{size}" height="{size}" viewBox="0 0 40 40" fill="none">'
            f'<rect width="40" height="40" rx="11" fill="{ACCENT}"/>'
            f'<path d="M6 21h7l3.5-9 6 18 4-11 2 2H34" stroke="{INK}" stroke-width="3" '
            f'stroke-linecap="round" stroke-linejoin="round"/></svg>')


def shape(code):
    cls, _, name = ROLES[code]
    return f'<span class="sh {cls}" title="{name}" aria-label="{name}"></span>'


def legend_html():
    items = "".join(f'<span class="it">{shape(c)}{ROLES[c][2]}</span>' for c in ("A", "R", "C", "I"))
    return f'<div class="legend">{items}</div>'


# ----------------------------------------------------------------------------
# KOMPONEN TABEL KPI MATRIX (simbol bisa diklik pada divisi yang diizinkan)
# ----------------------------------------------------------------------------
COMPONENT_HTML = tok("""<!doctype html>
<html><head><meta charset="utf-8">
<style>
@import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap');
html, body { margin:0; padding:0; background:transparent; font-family:'Plus Jakarta Sans','Segoe UI',system-ui,sans-serif; }
body { padding:2px; }
""" + SHAPE_CSS + TABLE_CSS + """
.cell { display:inline-flex; align-items:center; gap:5px; padding:4px 6px; border-radius:10px; }
.cell.clk { cursor:pointer; transition:background .12s; }
.cell.clk:hover { background:#E6F7F3; }
.cell.clk:hover .sh { transform:scale(1.25); }
.sh { transition:transform .12s; }
.bd { font-size:.68rem; font-weight:800; color:#fff; background:__I__; border-radius:9px; padding:1px 6px; line-height:1.4; }
</style></head>
<body><div id="root"></div>
<script>
(function () {
  var root = document.getElementById("root");
  var SH = {A:"a", R:"r", C:"c", I:"i"};
  var NM = {A:"Accountable", R:"Responsible", C:"Consulted", I:"Informed"};
  function send(type, data) {
    window.parent.postMessage(Object.assign({isStreamlitMessage:true, type:type}, data || {}), "*");
  }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }
  function spans(rows) {
    var s = {}, i = 0;
    while (i < rows.length) {
      var j = i;
      while (j + 1 < rows.length && rows[j + 1].category === rows[i].category) j++;
      s[i] = j - i + 1; i = j + 1;
    }
    return s;
  }
  function render(a) {
    var rows = a.rows || [], depts = a.depts || [], ed = a.edit_depts || [], counts = a.counts || {};
    var pal = a.palette || ["#0F766E"];
    root.innerHTML = "";
    var wrap = el("div", "mx-wrap"), table = el("table", "mx"), thead = el("thead"), hr = el("tr");
    var heads = ["Category", "No.", "Key Strategic Activities", "PIC"].concat(depts);
    var widths = [150, 58, 340, 150].concat(depts.map(function () { return 66; }));
    heads.forEach(function (h, i) {
      var th = el("th", (i === 0 || i === 2 || i === 3) ? "l" : "", h);
      th.style.width = widths[i] + "px";
      hr.appendChild(th);
    });
    thead.appendChild(hr); table.appendChild(thead);
    var tb = el("tbody");
    if (!rows.length) {
      var tr0 = el("tr"), td0 = el("td", "empty", "Belum ada aktivitas yang ditambahkan.");
      td0.colSpan = heads.length; tr0.appendChild(td0); tb.appendChild(tr0);
    } else {
      var sp = spans(rows), colors = {}, n = 0;
      rows.forEach(function (r, idx) {
        var tr = el("tr");
        if (sp[idx]) {
          var c = colors[r.category];
          if (!c) { c = pal[n % pal.length]; colors[r.category] = c; n++; }
          var tc = el("td", "cat l", r.category || "–");
          tc.rowSpan = sp[idx];
          tc.style.setProperty("--c", c); tc.style.setProperty("--bg", c + "12");
          tr.appendChild(tc);
        }
        tr.appendChild(el("td", "n", r.nomor));
        tr.appendChild(el("td", "l", r.activity));
        tr.appendChild(el("td", "l", r.pic));
        depts.forEach(function (d) {
          var td = el("td", "rc"), code = (r.roles || {})[d];
          if (code && SH[code]) {
            var can = ed.indexOf(d) >= 0;
            var box = el("span", "cell"), s = el("span", "sh " + SH[code]);
            box.appendChild(s);
            var cnt = counts[r.id + "|" + d];
            if (cnt) box.appendChild(el("span", "bd", String(cnt)));
            box.title = NM[code] + (can ? " – klik untuk menulis aktivitas" : "");
            if (can) {
              box.className += " clk";
              box.addEventListener("click", function () {
                send("streamlit:setComponentValue", {value:{rid:r.id, dept:d, n:Date.now()}, dataType:"json"});
              });
            }
            td.appendChild(box);
          }
          tr.appendChild(td);
        });
        tb.appendChild(tr);
      });
    }
    table.appendChild(tb); wrap.appendChild(table); root.appendChild(wrap);
    wrap.style.maxHeight = "640px";
    setTimeout(function () { send("streamlit:setFrameHeight", {height: wrap.offsetHeight + 8}); }, 30);
  }
  window.addEventListener("message", function (ev) {
    if (ev.data && ev.data.type === "streamlit:render") render(ev.data.args || {});
  });
  send("streamlit:componentReady", {apiVersion: 1});
})();
</script></body></html>
""")


# ----------------------------------------------------------------------------
# KOMPONEN TABEL KPI ACTIVITIES (kolom Activity bisa diklik bila baris itu boleh diedit)
# ----------------------------------------------------------------------------
ACT_COMPONENT_HTML = tok("""<!doctype html>
<html><head><meta charset="utf-8">
<style>
@import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap');
html, body { margin:0; padding:0; background:transparent; font-family:'Plus Jakarta Sans','Segoe UI',system-ui,sans-serif; }
body { padding:2px; }
""" + SHAPE_CSS + TABLE_CSS + """
.act { white-space:pre-wrap; word-break:break-word; }
.mx td.act-clk { cursor:pointer; transition:background .12s; position:relative; }
.mx td.act-clk:hover { background:#E6F7F3; }
.mx td.act-clk .edt { display:inline-block; margin-left:8px; font-size:.74rem; font-weight:700; color:__A__; opacity:0; transition:opacity .12s; }
.mx td.act-clk:hover .edt { opacity:1; }
</style></head>
<body><div id="root"></div>
<script>
(function () {
  var root = document.getElementById("root");
  var SH = {A:"a", R:"r", C:"c", I:"i"};
  var NM = {A:"Accountable", R:"Responsible", C:"Consulted", I:"Informed"};
  function send(type, data) {
    window.parent.postMessage(Object.assign({isStreamlitMessage:true, type:type}, data || {}), "*");
  }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }
  function render(a) {
    var rows = a.rows || [], pal = a.palette || ["#0F766E"];
    var color = a.color || pal[0];
    root.innerHTML = "";
    var wrap = el("div", "mx-wrap"), table = el("table", "mx"), thead = el("thead"), hr = el("tr");
    table.style.minWidth = "1380px";
    var heads = ["Kategori", "Nomor", "Key Strategic Activities", "No.", "Activity", "Department", "Diisi Oleh", "Simbol"];
    var widths = [150, 70, 260, 60, 400, 210, 100, 120];
    var left = {0:1, 2:1, 4:1, 5:1};
    heads.forEach(function (h, i) {
      var th = el("th", left[i] ? "l" : "", h);
      th.style.width = widths[i] + "px";
      hr.appendChild(th);
    });
    thead.appendChild(hr); table.appendChild(thead);
    var tb = el("tbody");
    if (!rows.length) {
      var tr0 = el("tr"), td0 = el("td", "empty", a.empty || "Belum ada aktivitas.");
      td0.colSpan = heads.length; tr0.appendChild(td0); tb.appendChild(tr0);
    } else {
      rows.forEach(function (r, idx) {
        var tr = el("tr");
        if (idx === 0) {
          var tc = el("td", "cat l", r.category || "–");
          tc.rowSpan = rows.length;
          tc.style.setProperty("--c", color); tc.style.setProperty("--bg", color + "12");
          tr.appendChild(tc);
          var tn = el("td", "n", r.nomor); tn.rowSpan = rows.length; tr.appendChild(tn);
          var tk = el("td", "l", r.key_activity); tk.rowSpan = rows.length; tr.appendChild(tk);
        }
        // kolom No. (nomor urut aktivitas pada KPI ini)
        tr.appendChild(el("td", "n", String(r.seq || "")));
        // kolom Activity (klik = edit, hanya untuk baris yang boleh diedit oleh akun ini)
        var canRow = !!r.can;
        var ta = el("td", "l" + (canRow ? " act-clk" : ""));
        ta.appendChild(el("span", "act", r.activity));
        if (canRow) {
          ta.appendChild(el("span", "edt", "✎ Edit"));
          ta.title = "Klik untuk mengedit atau menghapus aktivitas";
          ta.addEventListener("click", function () {
            send("streamlit:setComponentValue", {value:{aid:r.id, n:Date.now()}, dataType:"json"});
          });
        }
        tr.appendChild(ta);
        tr.appendChild(el("td", "l", r.department));
        // kolom Diisi Oleh
        var td1 = el("td", "rc"), w1 = el("div", "div-logo-wrap");
        w1.title = "Diisi oleh " + r.division;
        w1.appendChild(el("span", "div-logo", r.division));
        td1.appendChild(w1); tr.appendChild(td1);
        // kolom Simbol (peran divisi pada baris KPI Matrix)
        var td2 = el("td", "rc"), w2 = el("div", "sym-wrap");
        if (r.code && SH[r.code]) {
          w2.title = NM[r.code];
          w2.appendChild(el("span", "sh " + SH[r.code]));
          w2.appendChild(el("span", "sym-name", NM[r.code]));
        } else {
          w2.appendChild(el("span", "sym-name", "–"));
        }
        td2.appendChild(w2); tr.appendChild(td2);
        tb.appendChild(tr);
      });
    }
    table.appendChild(tb); wrap.appendChild(table); root.appendChild(wrap);
    wrap.style.maxHeight = "640px";
    setTimeout(function () { send("streamlit:setFrameHeight", {height: wrap.offsetHeight + 8}); }, 30);
  }
  window.addEventListener("message", function (ev) {
    if (ev.data && ev.data.type === "streamlit:render") render(ev.data.args || {});
  });
  send("streamlit:componentReady", {apiVersion: 1});
})();
</script></body></html>
""")


def _write_component(folder, content):
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "index.html")
    old = None
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            old = f.read()
    if old != content:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)


_write_component(COMP_DIR, COMPONENT_HTML)
_write_component(ACT_COMP_DIR, ACT_COMPONENT_HTML)
matrix_component = components.declare_component("corpopulse_matrix", path=COMP_DIR)
activities_component = components.declare_component("corpopulse_activities", path=ACT_COMP_DIR)


# ----------------------------------------------------------------------------
# DATABASE
# ----------------------------------------------------------------------------
def conn():
    return sqlite3.connect(DB)


def run(sql, params=()):
    with closing(conn()) as c:
        cur = c.execute(sql, params)
        c.commit()
        return cur.lastrowid


def rows_of(sql, params=()):
    with closing(conn()) as c:
        return c.execute(sql, params).fetchall()


def hpw(p, salt):
    return hashlib.pbkdf2_hmac("sha256", p.encode(), bytes.fromhex(salt), 120_000).hex()


def add_user(u, n, r, p, division="", department=""):
    salt = secrets.token_hex(16)
    run("INSERT INTO users(username,name,role,salt,pwd,division,department) VALUES (?,?,?,?,?,?,?)",
        (u, n, r, salt, hpw(p, salt), division, department))


def set_password(u, p):
    salt = secrets.token_hex(16)
    run("UPDATE users SET salt=?, pwd=? WHERE username=?", (salt, hpw(p, salt), u))


def get_user(username):
    r = rows_of("SELECT username,name,role,division,department FROM users WHERE username=?", (username,))
    if not r:
        return None
    un, name, role, div, dep = r[0]
    return {"username": un, "name": name, "role": role, "division": div or "", "department": dep or ""}


def verify(u, p):
    r = rows_of("SELECT username,salt,pwd FROM users WHERE username=?", (u,))
    if not r:
        return None
    un, salt, pwd = r[0]
    if secrets.compare_digest(hpw(p, salt), pwd):
        return get_user(un)
    return None


def init_db():
    run("CREATE TABLE IF NOT EXISTS users(username TEXT PRIMARY KEY, name TEXT, role TEXT, salt TEXT, pwd TEXT, "
        "division TEXT DEFAULT '', department TEXT DEFAULT '')")
    ucols = [r[1] for r in rows_of("PRAGMA table_info(users)")]
    if "division" not in ucols:     # migrasi: akun lama mendapat kolom cakupan divisi/department
        run("ALTER TABLE users ADD COLUMN division TEXT DEFAULT ''")
    if "department" not in ucols:
        run("ALTER TABLE users ADD COLUMN department TEXT DEFAULT ''")
    run("CREATE TABLE IF NOT EXISTS kpi_matrix(id INTEGER PRIMARY KEY AUTOINCREMENT, "
        + ", ".join(f"{c} TEXT" for c in COLS) + ")")
    run("CREATE TABLE IF NOT EXISTS activities(id INTEGER PRIMARY KEY AUTOINCREMENT, kpi_id INTEGER, "
        "division TEXT, department TEXT, activity TEXT, created TEXT)")
    ddl = ("CREATE TABLE IF NOT EXISTS implementation(id INTEGER PRIMARY KEY AUTOINCREMENT, act_id INTEGER, "
           "year INTEGER, month INTEGER, note TEXT, status TEXT, note_date TEXT, UNIQUE(act_id, year, month))")
    cols = [r[1] for r in rows_of("PRAGMA table_info(implementation)")]
    if cols and "year" not in cols:   # migrasi tabel lama: data lama dimasukkan ke tahun berjalan
        run("ALTER TABLE implementation RENAME TO implementation_old")
        run(ddl)
        run("INSERT INTO implementation(act_id,year,month,note,status,note_date) "
            "SELECT act_id,?,month,note,status,'' FROM implementation_old", (datetime.now().year,))
        run("DROP TABLE implementation_old")
    else:
        run(ddl)
    run("DELETE FROM implementation WHERE act_id NOT IN (SELECT id FROM activities)")   # bersihkan data yatim
    # migrasi nama lama "Dept. X" -> "X Dept." supaya data yang sudah tersimpan ikut mengikuti format baru
    for aid_, dep_ in rows_of("SELECT id,department FROM activities WHERE department LIKE 'Dept. %'"):
        run("UPDATE activities SET department=? WHERE id=?", (dep_[6:].strip() + " Dept.", aid_))
    for d in DEPTS:   # migrasi tanda lama: ● -> A, ○ -> R
        c = d.lower()
        run(f"UPDATE kpi_matrix SET {c}=CASE {c} WHEN '●' THEN 'A' WHEN '○' THEN 'R' ELSE {c} END")
    if rows_of("SELECT COUNT(*) FROM users")[0][0] == 0:
        add_user("admin", "Administrator", "admin", "admin123")


def fetch_matrix():
    return [tuple("" if v is None else v for v in r)
            for r in rows_of(f"SELECT id,{','.join(COLS)} FROM kpi_matrix ORDER BY id")]


def cat_colors():
    """Warna kategori konsisten di semua halaman (urut kemunculan pertama)."""
    out = {}
    for r in fetch_matrix():
        out.setdefault(r[1], CAT_COLORS[len(out) % len(CAT_COLORS)])
    return out


def count_words(t):
    return len(t.split())


def seq_map(kpi_id):
    """Nomor urut aktivitas pada satu KPI Matrix, sesuai urutan pengisian.
    Pengisian pertama = 1, kedua = 2, dst. Penomoran dimulai ulang untuk tiap KPI (key strategic activity)."""
    ids = [r[0] for r in rows_of("SELECT id FROM activities WHERE kpi_id=? ORDER BY id", (kpi_id,))]
    return {aid: n for n, aid in enumerate(ids, 1)}


def refresh_dialog():
    """Muat ulang isi dialog tanpa menutupnya."""
    try:
        st.rerun(scope="fragment")
    except Exception:   # versi lama / bukan konteks fragmen: muat ulang seluruh halaman
        st.rerun()


# ----------------------------------------------------------------------------
# DIALOG
# ----------------------------------------------------------------------------
def dialog(title, large=False):
    d = getattr(st, "dialog", None) or getattr(st, "experimental_dialog")
    try:
        return d(title, width="large") if large else d(title)
    except TypeError:
        return d(title)


def role_fmt(code):
    return "— Kosong" if code == "" else f"{ROLES[code][1]}  {ROLES[code][2]}"


def activity_form(rid, data):
    """Formulir tambah/ubah baris KPI Matrix. rid=None untuk data baru. (Khusus admin.)"""
    k = "new" if rid is None else str(rid)
    cats = sorted({r[1] for r in fetch_matrix() if r[1]})
    st.markdown(legend_html(), unsafe_allow_html=True)
    with st.form(f"af_{k}"):
        c1, c2 = st.columns(2)
        options = ["(kategori baru)"] + cats
        idx = options.index(data["category"]) if data["category"] in options else 0
        sel = c1.selectbox("Category", options, index=idx, key=f"cs_{k}")
        newcat = c2.text_input("Nama kategori baru", placeholder="Isi bila memilih (kategori baru)", key=f"cn_{k}")
        c3, c4 = st.columns([1, 3])
        nomor = c3.text_input("No.", value=data["nomor"], key=f"no_{k}")
        pic = c4.text_input("PIC", value=data["pic"], key=f"pi_{k}")
        activity = st.text_input("Key Strategic Activities", value=data["activity"], key=f"ac_{k}")
        st.markdown("**Peran tiap departemen**")
        roles = {}
        for i in range(0, len(DEPTS), 4):
            cols = st.columns(4)
            for col, d in zip(cols, DEPTS[i:i + 4]):
                cur = data[d.lower()] if data[d.lower()] in ROLES else ""
                roles[d.lower()] = col.selectbox(d, CYCLE, index=CYCLE.index(cur), format_func=role_fmt,
                                                 key=f"r_{k}_{d}")
        ok = S(st.form_submit_button, "Simpan aktivitas", type="primary")
    if ok:
        category = newcat.strip() if sel == "(kategori baru)" else sel
        if not activity.strip():
            st.error("Key Strategic Activities wajib diisi.")
            return
        vals = {"category": category, "nomor": nomor.strip(), "activity": activity.strip(), "pic": pic.strip(), **roles}
        data_row = [vals[c] for c in COLS]
        if rid is None:
            run(f"INSERT INTO kpi_matrix({','.join(COLS)}) VALUES ({','.join('?' * len(COLS))})", data_row)
        else:
            run(f"UPDATE kpi_matrix SET {','.join(c + '=?' for c in COLS)} WHERE id=?", data_row + [rid])
            for d in DEPTS:   # simbol dikosongkan = penugasan dicabut, aktivitasnya ikut dihapus
                if roles[d.lower()] == "":
                    run("DELETE FROM activities WHERE kpi_id=? AND division=?", (rid, d))
        st.rerun()


@dialog("Tambah aktivitas", large=True)
def dlg_add():
    if not is_admin_user():
        return
    blank = {c: "" for c in COLS}
    blank["nomor"] = str(len(fetch_matrix()) + 1)
    activity_form(None, blank)


@dialog("Ubah atau hapus aktivitas", large=True)
def dlg_edit():
    if not is_admin_user():
        return
    rows = fetch_matrix()
    if not rows:
        st.info("Belum ada aktivitas untuk diubah.")
        return
    by_id = {r[0]: dict(zip(COLS, r[1:])) for r in rows}
    rid = st.selectbox("Pilih aktivitas", list(by_id), key="edit_pick",
                       format_func=lambda i: f"{by_id[i]['nomor'] or '-'}  ·  {by_id[i]['activity'][:70]}")
    activity_form(rid, by_id[rid])
    st.divider()
    sure = st.checkbox("Saya yakin ingin menghapus aktivitas ini", key=f"sure_{rid}")
    if S(st.button, "Hapus aktivitas", key=f"del_{rid}", disabled=not sure):
        run("DELETE FROM kpi_matrix WHERE id=?", (rid,))
        run("DELETE FROM activities WHERE kpi_id=?", (rid,))
        st.rerun()


@dialog("Tulis aktivitas", large=True)
def dlg_cell(rid, dept):
    """Dibuka saat akun yang berhak mengklik simbol pada tabel KPI Matrix."""
    user = cur_user()
    if not can_fill(user, dept):
        st.error("Akun ini tidak memiliki akses untuk divisi tersebut.")
        return
    r = rows_of(f"SELECT category,nomor,activity,{dept.lower()} FROM kpi_matrix WHERE id=?", (rid,))
    if not r or r[0][3] not in ROLES:
        st.error("Simbol ini sudah tidak ada pada tabel.")
        return
    cat, nomor, act, code = r[0]
    st.markdown(
        f'<div class="ch">{shape(code)}<div><div class="t">{esc(act)}</div>'
        f'<div class="s">{esc(dept)} · {ROLES[code][2]} · No. {esc(nomor or "-")} · {esc(cat or "-")}</div></div></div>',
        unsafe_allow_html=True)

    opts = list(DEPT_OPTIONS.get(dept, DEFAULT_DEPTS))
    restricted = (not is_admin_user(user)) and bool(user.get("department"))
    if restricted:   # user dibatasi per department: hanya department miliknya
        opts = [user["department"]]
    ver = st.session_state.get("tx_ver", 0)

    st.markdown("**Tambah aktivitas**")
    st.caption(f"Aktivitas baru ini akan diberi nomor {len(seq_map(rid)) + 1}.")
    dsel = st.selectbox("Department", opts, key=f"nd_{rid}_{dept}_{ver}")
    txt = st.text_area("Activity", height=150, max_chars=4000, key=f"nt_{rid}_{dept}_{ver}",
                       placeholder=f"Tulis aktivitas (maksimal {MAX_WORDS} kata)")
    w = count_words(txt)
    st.markdown(f'<div class="wc{" bad" if w > MAX_WORDS else ""}">{w} / {MAX_WORDS} kata</div>', unsafe_allow_html=True)
    if S(st.button, "Simpan aktivitas", key=f"ns_{rid}_{dept}", type="primary"):
        if w == 0:
            st.error("Aktivitas belum diisi.")
        elif w > MAX_WORDS:
            st.error(f"Aktivitas {w} kata. Maksimal {MAX_WORDS} kata, kurangi {w - MAX_WORDS} kata.")
        else:
            run("INSERT INTO activities(kpi_id,division,department,activity,created) VALUES (?,?,?,?,?)",
                (rid, dept, dsel, txt.strip(), datetime.now().strftime("%Y-%m-%d %H:%M")))
            st.session_state["tx_ver"] = ver + 1
            refresh_dialog()

    acts = rows_of("SELECT id,department,activity FROM activities WHERE kpi_id=? AND division=? ORDER BY id", (rid, dept))
    acts = [a for a in acts if can_fill(user, dept, a[1])]   # user hanya melihat/mengedit milik department-nya
    if acts:
        st.divider()
        st.markdown(f"**Aktivitas tersimpan ({len(acts)})**")
    seq = seq_map(rid)
    for aid, dep, body in acts:
        short = body.replace("\n", " ")
        with st.expander(f"No. {seq.get(aid, '-')}  ·  {dep}  ·  {short[:70]}{'…' if len(short) > 70 else ''}"):
            o = list(opts) if dep in opts else [dep] + list(opts)
            ed_d = st.selectbox("Department", o, index=o.index(dep), key=f"ed_d_{aid}")
            ed_t = st.text_area("Activity", value=body, height=140, max_chars=4000, key=f"ed_t_{aid}")
            ew = count_words(ed_t)
            st.markdown(f'<div class="wc{" bad" if ew > MAX_WORDS else ""}">{ew} / {MAX_WORDS} kata</div>',
                        unsafe_allow_html=True)
            b1, b2 = st.columns(2)
            if S(b1.button, "Simpan perubahan", key=f"ed_s_{aid}", type="primary"):
                if ew == 0 or ew > MAX_WORDS:
                    st.error(f"Isi aktivitas harus 1 sampai {MAX_WORDS} kata.")
                elif not can_fill(user, dept, ed_d):
                    st.error("Akun ini tidak boleh memindahkan aktivitas ke department lain.")
                else:
                    run("UPDATE activities SET department=?, activity=? WHERE id=?", (ed_d, ed_t.strip(), aid))
                    refresh_dialog()
            if S(b2.button, "Hapus aktivitas", key=f"ed_x_{aid}"):
                run("DELETE FROM activities WHERE id=?", (aid,))
                refresh_dialog()


@dialog("Edit aktivitas", large=True)
def dlg_edit_activity(aid):
    """Dibuka saat akun yang berhak mengklik isi kolom Activity pada tabel KPI Activities."""
    user = cur_user()
    r = rows_of(
        "SELECT a.kpi_id,a.division,a.department,a.activity,k.category,k.nomor,k.activity "
        "FROM activities a JOIN kpi_matrix k ON k.id=a.kpi_id WHERE a.id=?", (aid,))
    if not r:
        st.error("Aktivitas ini sudah tidak ada.")
        return
    kid, div, dep, body, cat, nomor, key_act = r[0]
    if not can_fill(user, div, dep):
        st.error("Akun ini tidak memiliki akses untuk mengubah aktivitas tersebut.")
        return
    code = rows_of(f"SELECT {div.lower()} FROM kpi_matrix WHERE id=?", (kid,))[0][0]
    sym = shape(code) if code in ROLES else ""
    role_name = ROLES[code][2] if code in ROLES else "-"
    act_no = seq_map(kid).get(aid, "-")
    st.markdown(
        f'<div class="ch">{sym}<div><div class="t">{esc(key_act)}</div>'
        f'<div class="s">Aktivitas No. {act_no} · {esc(div)} · {esc(role_name)} · KPI No. {esc(nomor or "-")} · {esc(cat or "-")}</div></div></div>',
        unsafe_allow_html=True)

    opts = list(DEPT_OPTIONS.get(div, DEFAULT_DEPTS))
    if (not is_admin_user(user)) and user.get("department"):
        opts = [user["department"]]
    if dep not in opts:
        opts = [dep] + opts
    new_dep = st.selectbox("Department", opts, index=opts.index(dep), key=f"ea_d_{aid}")
    new_txt = st.text_area("Activity", value=body, height=200, max_chars=4000, key=f"ea_t_{aid}")
    w = count_words(new_txt)
    st.markdown(f'<div class="wc{" bad" if w > MAX_WORDS else ""}">{w} / {MAX_WORDS} kata</div>',
                unsafe_allow_html=True)

    b1, b2 = st.columns(2)
    if S(b1.button, "Simpan perubahan", key=f"ea_s_{aid}", type="primary"):
        if w == 0:
            st.error("Aktivitas tidak boleh kosong.")
        elif w > MAX_WORDS:
            st.error(f"Aktivitas {w} kata. Maksimal {MAX_WORDS} kata, kurangi {w - MAX_WORDS} kata.")
        elif not can_fill(user, div, new_dep):
            st.error("Akun ini tidak boleh memindahkan aktivitas ke department lain.")
        else:
            run("UPDATE activities SET department=?, activity=? WHERE id=?", (new_dep, new_txt.strip(), aid))
            st.rerun()   # menutup dialog dan memuat ulang tabel

    st.divider()
    sure = st.checkbox("Saya yakin ingin menghapus aktivitas ini", key=f"ea_sure_{aid}")
    if S(b2.button, "Hapus aktivitas", key=f"ea_x_{aid}", disabled=not sure):
        run("DELETE FROM activities WHERE id=?", (aid,))
        st.rerun()


@dialog("Profil akun")
def dlg_profile():
    u = cur_user()
    initial = esc((u.get("name") or u.get("username") or "?")[:1].upper())
    if is_admin_user(u):
        scope, access = "Semua divisi dan department", "Akses penuh: seluruh menu, manajemen akun, dan batas tanggal pengisian."
    else:
        dep = u.get("department") or "Semua department pada divisi"
        scope = f"Divisi {u.get('division') or '-'}  ·  {dep}"
        access = ("Mengisi KPI Matrix pada kolom divisi sendiri dan notes/status KPI Implementation sesuai cakupan, "
                  "selama belum melewati tanggal batas. Halaman lain hanya dapat dilihat.")
    st.markdown(
        f'<div class="acc"><div class="av lg">{initial}</div><div><div class="nm">{esc(u.get("name"))}</div>'
        f'<span class="rl">{esc(u.get("role"))}</span></div></div>'
        f'<div class="pf"><div class="k">Username</div><div class="v">{esc(u.get("username"))}</div>'
        f'<div class="k">Nama lengkap</div><div class="v">{esc(u.get("name"))}</div>'
        f'<div class="k">Peran</div><div class="v">{esc(u.get("role"))}</div>'
        f'<div class="k">Cakupan</div><div class="v">{esc(scope)}</div>'
        f'<div class="k">Hak akses</div><div class="v" style="font-weight:600">{esc(access)}</div></div>',
        unsafe_allow_html=True)


@dialog("Ganti password")
def dlg_password():
    u = cur_user()["username"]
    with st.form("chg_pw"):
        old = st.text_input("Password lama", type="password")
        new = st.text_input("Password baru (minimal 6 karakter)", type="password")
        new2 = st.text_input("Ulangi password baru", type="password")
        ok = S(st.form_submit_button, "Simpan password", type="primary")
    if ok:
        if not verify(u, old):
            st.error("Password lama salah.")
        elif len(new) < 6 or new != new2:
            st.error("Password baru minimal 6 karakter dan kedua isian harus sama.")
        else:
            set_password(u, new)
            st.success("Password berhasil diubah.")


def scope_inputs(prefix, role, div0="", dept0=""):
    """Pilihan divisi dan department untuk akun berperan user. Admin tidak memiliki batasan."""
    if role != "user":
        st.caption("Admin memiliki akses ke semua divisi dan department.")
        return "", ""
    c1, c2 = st.columns(2)
    div = c1.selectbox("Divisi (FA - CF)", DEPTS, index=DEPTS.index(div0) if div0 in DEPTS else 0,
                       key=f"{prefix}_div")
    dopts = [ALL_DEPT] + DEPT_OPTIONS.get(div, DEFAULT_DEPTS)
    cur = dept0 if dept0 in dopts else ALL_DEPT
    dsel = c2.selectbox("Department", dopts, index=dopts.index(cur), key=f"{prefix}_dept_{div}")
    return div, ("" if dsel == ALL_DEPT else dsel)


@dialog("Manajemen akun", large=True)
def dlg_users():
    if not is_admin_user():
        st.error("Hanya admin yang dapat mengelola akun.")
        return
    me = cur_user()["username"]
    data = rows_of("SELECT username,name,role,division,department FROM users ORDER BY role,username")
    df = pd.DataFrame([(a, b, c, d or "–", e or ("Semua" if d else "–")) for a, b, c, d, e in data],
                      columns=["Username", "Nama", "Peran", "Divisi", "Department"])
    S(st.dataframe, df, hide_index=True)

    t_add, t_edit = st.tabs(["Tambah akun", "Edit / hapus akun"])

    with t_add:
        ver = st.session_state.get("nu_ver", 0)
        a, b = st.columns(2)
        u = a.text_input("Username", key=f"nu_u_{ver}")
        n = b.text_input("Nama lengkap", key=f"nu_n_{ver}")
        r = a.selectbox("Peran", ["user", "admin"], key=f"nu_r_{ver}")
        p = b.text_input("Password awal (minimal 6 karakter)", type="password", key=f"nu_p_{ver}")
        div, dep = scope_inputs(f"nu{ver}", r)
        if S(st.button, "Tambah akun", type="primary", key=f"nu_ok_{ver}"):
            if not u.strip() or len(p) < 6:
                st.error("Username wajib diisi dan password minimal 6 karakter.")
            else:
                try:
                    add_user(u.strip(), n.strip() or u.strip(), r, p, div, dep)
                    st.session_state["nu_ver"] = ver + 1
                    refresh_dialog()
                except sqlite3.IntegrityError:
                    st.error("Username sudah dipakai.")

    with t_edit:
        users = [x[0] for x in rows_of("SELECT username FROM users ORDER BY username")]
        sel = st.selectbox("Pilih akun", users, key="eu_sel")
        rec = rows_of("SELECT name,role,division,department FROM users WHERE username=?", (sel,))[0]
        name0, role0, div0, dep0 = rec[0] or "", rec[1], rec[2] or "", rec[3] or ""
        a, b = st.columns(2)
        name = a.text_input("Nama lengkap", value=name0, key=f"eu_n_{sel}")
        role = b.selectbox("Peran", ["user", "admin"], index=0 if role0 == "user" else 1, key=f"eu_r_{sel}")
        div, dep = scope_inputs(f"eu_{sel}", role, div0, dep0)
        newpw = st.text_input("Password baru (kosongkan bila tidak diganti)", type="password", key=f"eu_p_{sel}")
        admins = rows_of("SELECT COUNT(*) FROM users WHERE role='admin'")[0][0]
        if S(st.button, "Simpan perubahan akun", type="primary", key=f"eu_s_{sel}"):
            if role0 == "admin" and role != "admin" and admins <= 1:
                st.error("Harus tersisa minimal 1 admin.")
            elif newpw and len(newpw) < 6:
                st.error("Password baru minimal 6 karakter.")
            else:
                run("UPDATE users SET name=?, role=?, division=?, department=? WHERE username=?",
                    (name.strip() or sel, role, div, dep, sel))
                if newpw:
                    set_password(sel, newpw)
                refresh_dialog()
        st.divider()
        sure = st.checkbox("Saya yakin ingin menghapus akun ini", key=f"eu_sure_{sel}")
        if S(st.button, "Hapus akun", key=f"eu_x_{sel}", disabled=not sure):
            if sel == me:
                st.error("Akun yang sedang dipakai tidak bisa dihapus.")
            elif role0 == "admin" and admins <= 1:
                st.error("Harus tersisa minimal 1 admin.")
            else:
                run("DELETE FROM users WHERE username=?", (sel,))
                refresh_dialog()


# ----------------------------------------------------------------------------
# HALAMAN: KPI MATRIX
# ----------------------------------------------------------------------------
def tiles_html(rows):
    cats = len({r[1] for r in rows if r[1]})
    assigned = sum(1 for r in rows for v in r[5:] if v in ROLES)
    n_act = rows_of("SELECT COUNT(*) FROM activities")[0][0]
    data = [(len(rows), "Aktivitas strategis"), (cats, "Kategori"), (assigned, "Total penugasan peran"),
            (n_act, "Aktivitas tertulis")]
    return '<div class="tiles">' + "".join(
        f'<div class="tile"><div class="v">{v}</div><div class="k">{k}</div></div>' for v, k in data) + "</div>"


def page_matrix(user):
    is_admin = is_admin_user(user)
    edit_depts = edit_divisions(user)
    rows = fetch_matrix()
    st.markdown(tiles_html(rows), unsafe_allow_html=True)
    if is_admin:
        hint, b1, b2 = st.columns([7, 1.7, 1.7])
        hint.markdown('<div class="hint">Klik simbol pada tabel untuk menulis aktivitas departemen.</div>',
                      unsafe_allow_html=True)
        if S(b1.button, "＋  Tambah aktivitas", type="primary", key="btn_add"):
            dlg_add()
        if S(b2.button, "✎  Ubah / hapus", key="btn_edit"):
            dlg_edit()
    elif edit_depts:
        st.markdown(f'<div class="hint">Anda dapat mengisi aktivitas pada kolom divisi <b>{esc(edit_depts[0])}</b>'
                    f'{" (" + esc(user["department"]) + ")" if user.get("department") else ""}: klik simbol pada kolom tersebut. '
                    'Kolom divisi lain hanya dapat dilihat.</div>', unsafe_allow_html=True)
    else:
        st.markdown('<div class="hint">Akun ini belum memiliki cakupan divisi, sehingga hanya dapat melihat tabel. '
                    'Hubungi admin untuk pengaturan akses.</div>', unsafe_allow_html=True)

    counts = {f"{kid}|{div}": n for kid, div, n in
              rows_of("SELECT kpi_id,division,COUNT(*) FROM activities GROUP BY kpi_id,division")}
    payload = [{"id": r[0], "category": r[1], "nomor": r[2], "activity": r[3], "pic": r[4],
                "roles": {d: r[5 + i] for i, d in enumerate(DEPTS)}} for r in rows]
    val = matrix_component(rows=payload, depts=DEPTS, edit_depts=edit_depts, counts=counts,
                           palette=CAT_COLORS, key="mx_tbl", default=None)
    st.markdown(legend_html(), unsafe_allow_html=True)

    # klik simbol -> buka dialog (nilai komponen bertahan antar-rerun, jadi cek nomor klik)
    if edit_depts and isinstance(val, dict) and val.get("n") != st.session_state.get("mx_seen"):
        st.session_state["mx_seen"] = val.get("n")
        if str(val["dept"]) in edit_depts:
            dlg_cell(int(val["rid"]), str(val["dept"]))


# ----------------------------------------------------------------------------
# HALAMAN: KPI ACTIVITIES
# ----------------------------------------------------------------------------
def page_activities(user):
    # Dropdown mengambil pilihan langsung dari KPI Matrix.
    matrix_rows = fetch_matrix()
    if not matrix_rows:
        st.info("Belum ada data KPI Matrix.")
        return

    kpi_lookup = {r[0]: r for r in matrix_rows}
    kpi_ids = list(kpi_lookup.keys())

    def kpi_label(kid):
        r = kpi_lookup[kid]
        category = r[1] or "Tanpa kategori"
        nomor = r[2] or "-"
        activity = r[3] or "Tanpa nama aktivitas"
        return f"{nomor} · {category} · {activity}"

    previous = st.session_state.get("act_kpi_id")
    if previous not in kpi_ids:
        previous = kpi_ids[0]

    selected_kpi = S(
        st.selectbox,
        "Pilih KPI Matrix",
        options=kpi_ids,
        index=kpi_ids.index(previous),
        format_func=kpi_label,
        key="kpi_activity_selector"
    )
    st.session_state["act_kpi_id"] = selected_kpi

    selected = kpi_lookup[selected_kpi]
    data = rows_of(
        "SELECT id,division,department,activity FROM activities WHERE kpi_id=? ORDER BY id",
        (selected_kpi,))
    total = len(data)

    st.markdown(f'<div class="sec">{esc(selected[3])} · {total} aktivitas</div>', unsafe_allow_html=True)

    # Simbol peran = isi sel KPI Matrix untuk divisi yang mengisi aktivitas tersebut
    payload = []
    for n, (aid, div, dep, act) in enumerate(data, 1):
        code = selected[5 + DEPTS.index(div)] if div in DEPTS else ""
        payload.append({"id": aid, "seq": n, "category": selected[1], "nomor": selected[2], "key_activity": selected[3],
                        "activity": act, "department": dep, "division": div,
                        "code": code if code in ROLES else "", "can": can_fill(user, div, dep)})
    any_edit = any(p["can"] for p in payload)
    if any_edit:
        st.markdown('<div class="hint" style="padding:0 0 10px">Klik isi kolom Activity pada tabel untuk '
                    'mengedit atau menghapus aktivitas (hanya baris sesuai hak akses akun Anda).</div>',
                    unsafe_allow_html=True)

    color = cat_colors().get(selected[1], CAT_COLORS[0])
    val = activities_component(
        rows=payload, color=color, palette=CAT_COLORS,
        empty=("Belum ada aktivitas untuk KPI Matrix yang dipilih. "
               "Aktivitas ditambahkan dengan mengklik simbol pada tabel KPI Matrix."),
        key="act_tbl", default=None)

    # klik kolom Activity -> dialog edit (nilai komponen bertahan antar-rerun, jadi cek nomor klik)
    if any_edit and isinstance(val, dict) and val.get("n") != st.session_state.get("act_seen"):
        st.session_state["act_seen"] = val.get("n")
        dlg_edit_activity(int(val["aid"]))


# ----------------------------------------------------------------------------
# HALAMAN: KPI IMPLEMENTATION
# ----------------------------------------------------------------------------
def box(key):
    try:
        return st.container(key=key)
    except TypeError:   # Streamlit lama tanpa parameter key
        return st.container()


def save_implementation(aid, year, notes, stats, dates, is_admin, locked):
    """Admin menyimpan semua isian (termasuk tanggal batas). User hanya menyimpan notes/status pada bulan yang
    belum terkunci, dan tanggal batas yang sudah ada tidak diubah."""
    with closing(conn()) as c:
        old = {m: (d or "") for m, d in c.execute(
            "SELECT month,note_date FROM implementation WHERE act_id=? AND year=?", (aid, year))}
        for m in range(1, 13):
            if m in locked:
                continue
            note, stt = notes[m].strip(), stats[m]
            if is_admin:
                ds = dates[m].isoformat() if dates[m] else ""
            else:
                ds = old.get(m, "")
            if not note and not stt and not ds:
                c.execute("DELETE FROM implementation WHERE act_id=? AND year=? AND month=?", (aid, year, m))
            else:
                c.execute("INSERT INTO implementation(act_id,year,month,note,status,note_date) VALUES (?,?,?,?,?,?) "
                          "ON CONFLICT(act_id,year,month) DO UPDATE SET note=excluded.note, "
                          "status=excluded.status, note_date=excluded.note_date",
                          (aid, year, m, note, stt, ds))
        c.commit()


def impl_table(user, can_edit, aid, year):
    """Tabel Januari-Desember: Notes, Batas pengisian (diatur admin), dan Status untuk satu aktivitas pada satu tahun."""
    is_admin = is_admin_user(user)
    saved = {m: (n or "", s or "", d or "") for m, n, s, d in
             rows_of("SELECT month,note,status,note_date FROM implementation WHERE act_id=? AND year=?", (aid, year))}
    sig = hashlib.md5(repr(sorted(saved.items())).encode()).hexdigest()[:8]   # kunci widget ikut berubah bila data DB berubah
    kb = f"{aid}_{year}"
    notes, stats, dates, ok = {}, {}, {}, False
    widths = [0.8] + [1] * 12
    dmin, dmax = date(year, 1, 1), date(year + 1, 12, 31)
    today = date.today()

    locked = set()   # bulan yang sudah melewati batas pengisian (berlaku untuk user, bukan admin)
    if not is_admin:
        for m in range(1, 13):
            raw = saved.get(m, ("", "", ""))[2]
            try:
                dl = date.fromisoformat(raw) if raw else None
            except ValueError:
                dl = None
            if dl is not None and today > dl:
                locked.add(m)

    with box("impl_tbl"):
        with (st.form(f"impl_form_{kb}") if can_edit else nullcontext()):
            hc = st.columns(widths, gap="small")
            hc[0].markdown('<div class="im-h l">Bulan</div>', unsafe_allow_html=True)
            for i, name in enumerate(MONTHS, 1):
                hc[i].markdown(f'<div class="im-h">{name}</div>', unsafe_allow_html=True)

            nc = st.columns(widths, gap="small")
            nc[0].markdown('<div class="im-r">Notes</div>', unsafe_allow_html=True)
            for m in range(1, 13):
                notes[m] = nc[m].text_area(f"Notes {MONTHS[m - 1]}", value=saved.get(m, ("", "", ""))[0], height=110,
                                           max_chars=500, key=f"im_n_{kb}_{m}_{sig}",
                                           label_visibility="collapsed", disabled=(not can_edit) or (m in locked))

            dc = st.columns(widths, gap="small")
            dc[0].markdown('<div class="im-r">Batas pengisian</div>', unsafe_allow_html=True)
            for m in range(1, 13):
                raw = saved.get(m, ("", "", ""))[2]
                try:
                    dv = date.fromisoformat(raw) if raw else None
                except ValueError:
                    dv = None
                dates[m] = dc[m].date_input(f"Batas pengisian {MONTHS[m - 1]}", value=dv,
                                            min_value=min(dmin, dv) if dv else dmin,
                                            max_value=max(dmax, dv) if dv else dmax,
                                            format="DD/MM/YYYY", key=f"im_d_{kb}_{m}_{sig}",
                                            label_visibility="collapsed", disabled=not is_admin)

            sc = st.columns(widths, gap="small")
            sc[0].markdown('<div class="im-r">Status</div>', unsafe_allow_html=True)
            for m in range(1, 13):
                cur = saved.get(m, ("", "", ""))[1]
                cur = cur if cur in STATUS_KEYS else ""
                stats[m] = sc[m].selectbox(f"Status {MONTHS[m - 1]}", STATUS_KEYS, index=STATUS_KEYS.index(cur),
                                           format_func=lambda k: STATUS_LABEL[k], key=f"im_s_{kb}_{m}_{sig}",
                                           label_visibility="collapsed", disabled=(not can_edit) or (m in locked))
            if can_edit:
                ok = S(st.form_submit_button, "Simpan notes, batas & status" if is_admin else "Simpan notes & status",
                       type="primary")

    if ok:
        save_implementation(aid, year, notes, stats, dates, is_admin, locked)
        st.success("Data berhasil disimpan.")

    n_i = sum(1 for v in stats.values() if v == "implemented")
    n_n = sum(1 for v in stats.values() if v == "not_implemented")
    if is_admin:
        extra = " · Tanggal pada baris Batas pengisian adalah batas akhir user mengisi notes dan status bulan tersebut."
    elif not can_edit:
        extra = " · Anda hanya dapat melihat; pengisian hanya untuk divisi/department sesuai akun."
    else:
        extra = " · Tanggal batas diatur admin; bulan yang sudah melewati batas terkunci."
    st.markdown(f'<div class="hint">Tahun {year} · Implemented: <b>{n_i}</b> · Not Implemented: <b>{n_n}</b> · '
                f'Belum diisi: <b>{12 - n_i - n_n}</b> (dari 12 bulan){extra}</div>', unsafe_allow_html=True)
    if locked:
        names = ", ".join(MONTHS[m - 1] for m in sorted(locked))
        st.markdown(f'<div class="lock">Terkunci (melewati batas pengisian): {esc(names)}</div>',
                    unsafe_allow_html=True)


def impl_stats(div, month, year):
    """Ringkasan per departemen pada satu divisi. month=0 berarti semua bulan (Jan-Des).

    Satuan hitung = aktivitas x bulan. Direncanakan = jumlah aktivitas dikali jumlah bulan terpilih,
    Implemented / Not Implemented = jumlah bulan yang statusnya sudah dipilih."""
    months = list(range(1, 13)) if month == 0 else [month]
    acts = rows_of("SELECT id,department FROM activities WHERE division=?", (div,))
    dept_of = dict(acts)
    opts = list(DEPT_OPTIONS.get(div, DEFAULT_DEPTS))
    opts += sorted({d for d in dept_of.values() if d not in opts})
    stat = {d: {"planned": 0, "impl": 0, "notimpl": 0} for d in opts}
    for _, d in acts:
        stat[d]["planned"] += len(months)
    for aid, m, s in rows_of("SELECT act_id,month,status FROM implementation WHERE year=?", (year,)):
        if aid in dept_of and m in months:
            if s == "implemented":
                stat[dept_of[aid]]["impl"] += 1
            elif s == "not_implemented":
                stat[dept_of[aid]]["notimpl"] += 1
    return stat


def donut_card(title, subtitle, planned, impl, notimpl):
    head = f'<div class="ct">{esc(title)}</div><div class="cs">{esc(subtitle)}</div>'
    if planned == 0:
        return f'<div class="card">{head}<div class="none">Belum ada aktivitas pada divisi ini.</div></div>'
    blank = planned - impl - notimpl
    pi, pn, pb = impl / planned * 100, notimpl / planned * 100, blank / planned * 100
    grad = (f"conic-gradient({C_IMPL} 0 {pi:.2f}%, {C_NOT} {pi:.2f}% {pi + pn:.2f}%, "
            f"{C_BLANK} {pi + pn:.2f}% 100%)")
    items = [(C_IMPL, "Implemented", impl, pi), (C_NOT, "Not Implemented", notimpl, pn),
             (C_BLANK, "Belum diisi", blank, pb)]
    legend = "".join(f'<div class="row"><span class="sw" style="background:{c}"></span>{n}'
                     f'<small>{v} ({p:.1f}%)</small></div>' for c, n, v, p in items)
    return (f'<div class="card">{head}<div class="dwrap">'
            f'<div class="donut" style="background:{grad}"><div class="hole"><b>{pi:.0f}%</b><span>Implemented</span></div></div>'
            f'<div class="dl">{legend}</div></div></div>')


def bars_card(title, subtitle, stat):
    head = f'<div class="ct">{esc(title)}</div><div class="cs">{esc(subtitle)}</div>'
    mx = max([v["planned"] for v in stat.values()] + [1])
    series = [("planned", "Direncanakan", INK), ("impl", "Implemented", C_IMPL), ("notimpl", "Not Implemented", C_NOT)]
    groups = ""
    for d, v in stat.items():
        bars = "".join(
            f'<div class="bw" style="--h:{v[k] / mx * 100:.1f}%" title="{n}: {v[k]}">'
            f'<span class="bv">{v[k]}</span><div class="bar" style="background:{c}"></div></div>'
            for k, n, c in series)
        groups += f'<div class="bg"><div class="bb">{bars}</div><div class="bl">{esc(d)}</div></div>'
    legend = '<div class="bleg">' + "".join(
        f'<span><i style="background:{c}"></i>{n}</span>' for _, n, c in series) + '</div>'
    return f'<div class="card">{head}<div class="bars">{groups}</div>{legend}</div>'


def page_implementation(user):
    matrix_rows = fetch_matrix()
    if not matrix_rows:
        st.info("Belum ada data KPI Matrix. Tambahkan aktivitas pada menu KPI Matrix terlebih dahulu.")
        return

    kpi_lookup = {r[0]: r for r in matrix_rows}
    kpi_ids = list(kpi_lookup)

    def kpi_label(kid):
        r = kpi_lookup[kid]
        t = r[3] or "Tanpa nama aktivitas"
        return f"{r[2] or '-'} · {t[:60]}{'…' if len(t) > 60 else ''}"

    # ---- dropdown filter ----
    now_y = datetime.now().year
    years = list(range(now_y - 3, now_y + 2))
    f1, f2, f3, f4, f5 = st.columns([2.3, 0.9, 1.7, 2.5, 0.9])
    kpi = f1.selectbox("KPI (No. Key Strategic Activities)", kpi_ids, format_func=kpi_label,
                       key="im_kpi_" + hashlib.md5(str(kpi_ids).encode()).hexdigest()[:6])
    ud = user.get("division", "")
    div = f2.selectbox("Divisi", DEPTS, index=DEPTS.index(ud) if (ud in DEPTS and not is_admin_user(user)) else 0,
                       key="im_div")
    used = [r[0] for r in rows_of("SELECT DISTINCT department FROM activities WHERE division=?", (div,))]
    dept_opts = list(DEPT_OPTIONS.get(div, DEFAULT_DEPTS))
    dept_opts += [d for d in used if d not in dept_opts]
    udep = user.get("department", "")
    dept_idx = dept_opts.index(udep) if (not is_admin_user(user) and ud == div and udep in dept_opts) else 0
    dept = f3.selectbox("Department", dept_opts, index=dept_idx, key=f"im_dept_{div}")
    acts = rows_of("SELECT id,activity FROM activities WHERE kpi_id=? AND division=? AND department=? ORDER BY id",
                   (kpi, div, dept))
    seq = seq_map(kpi)
    a_map = {}
    for aid_, txt_ in acts:
        t = txt_.replace("\n", " ")
        a_map[aid_] = f"{seq.get(aid_, '-')}. {t[:80]}{'…' if len(t) > 80 else ''}"
    aid = f4.selectbox("Activity", list(a_map), format_func=lambda i: a_map[i], disabled=not a_map,
                       key=f"im_act_{kpi}_{div}_{dept}")

    year = f5.selectbox("Tahun", years, index=years.index(now_y), key="im_year")

    # ---- tabel bulanan ----
    st.write("")
    if aid is None:
        st.info(f"Belum ada aktivitas untuk KPI ini pada {div} · {dept}. "
                "Aktivitas ditambahkan lewat menu KPI Matrix (klik simbol divisi).")
    else:
        kr = kpi_lookup[kpi]
        code = kr[5 + DEPTS.index(div)]
        full = dict(acts)[aid]
        st.markdown(
            f'<div class="ch">{shape(code) if code in ROLES else ""}<div><div class="t">{esc(full)}</div>'
            f'<div class="s">Aktivitas No. {seq.get(aid, "-")} · {esc(div)} · {esc(dept)} · KPI No. {esc(kr[2] or "-")} · {esc(kr[3] or "-")}</div></div></div>',
            unsafe_allow_html=True)
        impl_table(user, can_fill(user, div, dept), aid, year)

    # ---- ringkasan ----
    st.markdown('<div class="sec">Ringkasan implementasi</div>', unsafe_allow_html=True)
    s1, s2, _ = st.columns([1.2, 1.2, 4])
    sdiv = s1.selectbox("Berdasarkan divisi", DEPTS, index=DEPTS.index(div), key="im_sdiv")
    speriod = s2.selectbox("Periode", ["All"] + MONTHS, key="im_speriod")
    month = 0 if speriod == "All" else MONTHS.index(speriod) + 1
    stat = impl_stats(sdiv, month, year)
    planned = sum(v["planned"] for v in stat.values())
    impl = sum(v["impl"] for v in stat.values())
    notimpl = sum(v["notimpl"] for v in stat.values())
    per = ("Januari - Desember" if month == 0 else speriod) + f" {year}"
    sub = f"{per} · total direncanakan {planned}"

    g1, g2 = st.columns([1, 1.45], gap="large")
    g1.markdown(donut_card(f"Persentase implementasi · {sdiv}", sub, planned, impl, notimpl),
                unsafe_allow_html=True)
    g2.markdown(bars_card(f"Performa per department · {sdiv}", sub, stat), unsafe_allow_html=True)


# ----------------------------------------------------------------------------
# HALAMAN: SUMMARY DASHBOARD (hanya dilihat, tanpa fitur edit)
# ----------------------------------------------------------------------------
def dash_stats(year, months, div):
    """Hitung catatan implementasi per kategori.

    Satu catatan = satu bulan pada satu aktivitas yang memiliki notes atau status.
    Total notes aktivitas = implemented + not implemented + notes tanpa status.
    Presentase pencapaian = implemented / total notes aktivitas."""
    stat = {}
    for r in fetch_matrix():   # urutan kategori mengikuti KPI Matrix
        stat.setdefault(r[1] or "", {"impl": 0, "notimpl": 0, "nostat": 0})
    sql = ("SELECT k.category,i.status,i.note FROM implementation i "
           "JOIN activities a ON a.id=i.act_id JOIN kpi_matrix k ON k.id=a.kpi_id "
           f"WHERE i.year=? AND i.month IN ({','.join('?' * len(months))})")
    params = [year] + list(months)
    if div != "ALL":
        sql += " AND a.division=?"
        params.append(div)
    for cat, stt, note in rows_of(sql, params):
        if not (stt or (note or "").strip()):
            continue
        d = stat.setdefault(cat or "", {"impl": 0, "notimpl": 0, "nostat": 0})
        if stt == "implemented":
            d["impl"] += 1
        elif stt == "not_implemented":
            d["notimpl"] += 1
        else:
            d["nostat"] += 1
    return stat


def dash_overall(sub, impl, notimpl, nostat):
    tot = impl + notimpl + nostat
    pct = impl / tot * 100 if tot else 0
    if tot:
        pn = notimpl / tot * 100
        grad = (f"conic-gradient({C_IMPL} 0 {pct:.2f}%, {C_NOT} {pct:.2f}% {pct + pn:.2f}%, "
                f"{C_BLANK} {pct + pn:.2f}% 100%)")
    else:
        grad = f"conic-gradient({C_BLANK} 0 100%)"
    items = [(C_IMPL, "Implemented", impl), (C_NOT, "Not Implemented", notimpl), (C_BLANK, "Notes tanpa status", nostat)]
    legend = "".join(f'<div class="row"><span class="sw" style="background:{c}"></span>{n}<small>{v}</small></div>'
                     for c, n, v in items)
    return (f'<div class="card"><div class="ct">Pencapaian seluruh kategori</div><div class="cs">{esc(sub)}</div>'
            f'<div class="ovr"><div class="donut lg" style="background:{grad}">'
            f'<div class="hole"><b>{pct:.0f}%</b><span title="Terimplementasi / total notes aktivitas">{impl}/{tot}</span></div></div>'
            f'<div class="dl">{legend}'
            f'<div class="cs" style="margin:8px 0 0">Angka kecil (mis. 5/12) = aktivitas terimplementasi / total notes aktivitas.</div>'
            f'</div></div></div>')


def dash_cat_cards(stat):
    colors = cat_colors()
    cards = ""
    for cat, v in stat.items():
        tot = v["impl"] + v["notimpl"] + v["nostat"]
        pct = v["impl"] / tot * 100 if tot else 0
        c = colors.get(cat, CAT_COLORS[0])
        cards += (f'<div class="cc" style="--c:{c}"><div class="cn">{esc(cat) or "Tanpa kategori"}</div>'
                  f'<div class="pc">{pct:.0f}%</div><div class="fr">{v["impl"]}/{tot} aktivitas</div>'
                  f'<div class="pbar"><i style="width:{pct:.1f}%"></i></div></div>')
    return f'<div class="cgrid">{cards}</div>'


def dash_contrib(year, months):
    """Jumlah Implemented per divisi dan per kategori pada periode terpilih.
    Tidak dipengaruhi filter divisi: selalu mencakup seluruh divisi."""
    ph = ",".join("?" * len(months))
    rows = rows_of("SELECT a.division,k.category FROM implementation i "
                   "JOIN activities a ON a.id=i.act_id JOIN kpi_matrix k ON k.id=a.kpi_id "
                   f"WHERE i.year=? AND i.status='implemented' AND i.month IN ({ph})", [year] + list(months))
    by_div = {d: 0 for d in DEPTS}
    by_cat = {}
    for r in fetch_matrix():   # urutan kategori mengikuti KPI Matrix
        by_cat.setdefault(r[1] or "", 0)
    for div, cat in rows:
        by_div[div] = by_div.get(div, 0) + 1
        by_cat[cat or ""] = by_cat.get(cat or "", 0) + 1
    return by_div, by_cat


def contrib_card(title, sub, items):
    """items = [(label, jumlah implemented, warna)] -> batang horizontal berisi porsi dari total."""
    head = f'<div class="ct">{esc(title)}</div><div class="cs">{esc(sub)}</div>'
    total = sum(n for _, n, _ in items)
    if total == 0:
        return f'<div class="card">{head}<div class="none">Belum ada aktivitas Implemented pada periode ini.</div></div>'
    body = ""
    for label, n, color in items:
        pct = n / total * 100
        body += (f'<div class="kr"><div class="kn">{esc(label)}</div>'
                 f'<div class="kb"><i style="width:{pct:.1f}%;background:{color}"></i></div>'
                 f'<div class="kv">{pct:.0f}%<small>{n}</small></div></div>')
    return f'<div class="card">{head}{body}</div>'


def dash_not_impl(year, months):
    """Aktivitas yang belum terimplementasi: tidak ada satu pun bulan berstatus Implemented pada periode terpilih."""
    ph = ",".join("?" * len(months))
    done = {r[0] for r in rows_of(
        f"SELECT DISTINCT act_id FROM implementation WHERE year=? AND status='implemented' AND month IN ({ph})",
        [year] + list(months))}
    matrix = {r[0]: r for r in fetch_matrix()}
    cat_order = {}
    for r in matrix.values():
        cat_order.setdefault(r[1], len(cat_order))
    out = []
    for aid, kid, div, dep, act in rows_of("SELECT id,kpi_id,division,department,activity FROM activities ORDER BY id"):
        if aid in done or kid not in matrix:
            continue
        r = matrix[kid]
        code = r[5 + DEPTS.index(div)] if div in DEPTS else ""
        out.append({"aid": aid, "kid": kid, "category": r[1], "nomor": r[2], "key": r[3], "activity": act,
                    "dept": dep, "division": div, "code": code if code in ROLES else "",
                    "o": (cat_order[r[1]], kid, DEPTS.index(div) if div in DEPTS else 99, aid)})
    out.sort(key=lambda x: x["o"])
    return out


def not_impl_html(rows):
    heads = ["Kategori", "Key Strategic Activities", "Activity", "Dept", "Division", "Simbol"]
    widths = [150, 280, 340, 220, 90, 130]
    left = (0, 1, 2, 3)
    ths = "".join(f'<th class="{"l" if i in left else ""}" style="width:{w}px">{h}</th>'
                  for i, (h, w) in enumerate(zip(heads, widths)))
    if not rows:
        body = (f'<tr><td class="empty" colspan="{len(heads)}">Tidak ada aktivitas yang belum terimplementasi '
                'pada periode ini.</td></tr>')
    else:
        colors = cat_colors()
        spans, i = {}, 0
        while i < len(rows):
            j = i
            while j + 1 < len(rows) and rows[j + 1]["category"] == rows[i]["category"]:
                j += 1
            spans[i] = j - i + 1
            i = j + 1
        out = []
        for i, r in enumerate(rows):
            tds = ""
            if i in spans:
                c = colors.get(r["category"], CAT_COLORS[0])
                tds += (f'<td class="cat l" rowspan="{spans[i]}" style="--c:{c};--bg:{c}12">'
                        f'{esc(r["category"]) or "&ndash;"}</td>')
            key = f'{esc(r["nomor"])}. {esc(r["key"])}' if r["nomor"] else esc(r["key"])
            if r["code"]:
                sym = (f'<div class="sym-wrap" title="{ROLES[r["code"]][2]}">{shape(r["code"])}'
                       f'<span class="sym-name">{ROLES[r["code"]][2]}</span></div>')
            else:
                sym = '<span class="sym-name">&ndash;</span>'
            tds += (f'<td class="l">{key}</td>'
                    f'<td class="l">{esc(r["activity"]).replace(chr(10), "<br>")}</td>'
                    f'<td class="l">{esc(r["dept"])}</td>'
                    f'<td class="rc"><div class="div-logo-wrap"><span class="div-logo">{esc(r["division"])}</span></div></td>'
                    f'<td class="rc">{sym}</td>')
            out.append(f"<tr>{tds}</tr>")
        body = "".join(out)
    return (f'<div class="mx-wrap"><table class="mx" style="min-width:1200px"><thead><tr>{ths}</tr></thead>'
            f'<tbody>{body}</tbody></table></div>')


def page_dashboard():
    if not fetch_matrix():
        st.info("Belum ada data KPI Matrix. Tambahkan aktivitas pada menu KPI Matrix terlebih dahulu.")
        return
    now = datetime.now()
    years = list(range(now.year - 3, now.year + 2))
    c1, c2, c3, c4, _ = st.columns([1.3, 1.3, 1, 1.5, 1.6])
    year = c3.selectbox("Tahun", years, index=years.index(now.year), key="sd_year")
    # bulan acuan (real time): bulan berjalan untuk tahun ini, Desember untuk tahun lampau, Januari untuk tahun depan
    ref = now.month if year == now.year else (12 if year < now.year else 1)
    month_ids = list(range(1, 13))
    m_from = c1.selectbox("Dari bulan", month_ids, index=0, format_func=lambda k: MONTHS[k - 1], key=f"sd_from_{year}")
    m_to = c2.selectbox("Sampai bulan", month_ids, index=ref - 1, format_func=lambda k: MONTHS[k - 1],
                        key=f"sd_to_{year}")
    div = c4.selectbox("Divisi", ["ALL"] + DEPTS, format_func=lambda d: "All Divisi" if d == "ALL" else d,
                       key="sd_div")
    lo, hi = min(m_from, m_to), max(m_from, m_to)   # bila terbalik, rentang dibalik otomatis
    months = list(range(lo, hi + 1))
    plabel = MONTHS[lo - 1] if lo == hi else f"{MONTHS[lo - 1]} - {MONTHS[hi - 1]}"
    st.markdown('<div class="hint" style="padding-bottom:0">Pilih bulan awal dan akhir yang sama untuk melihat data satu bulan saja. '
                'Bawaan: Januari sampai bulan berjalan.</div>', unsafe_allow_html=True)
    st.markdown('<div class="hint">Filter divisi hanya memengaruhi bagian pencapaian (seluruh kategori dan per kategori). '
                'Grafik kontribusi dan tabel di bawahnya mengikuti filter waktu dan tahun saja.</div>',
                unsafe_allow_html=True)

    # ---- pencapaian (ikut filter divisi) ----
    stat = dash_stats(year, months, div)
    impl = sum(v["impl"] for v in stat.values())
    notimpl = sum(v["notimpl"] for v in stat.values())
    nostat = sum(v["nostat"] for v in stat.values())
    sub = f"{plabel} {year} · {'All Divisi' if div == 'ALL' else div}"

    st.write("")
    st.markdown(dash_overall(sub, impl, notimpl, nostat), unsafe_allow_html=True)
    st.markdown('<div class="sec">Pencapaian per kategori</div>', unsafe_allow_html=True)
    st.markdown(dash_cat_cards(stat), unsafe_allow_html=True)

    # ---- kontribusi (tidak ikut filter divisi) ----
    by_div, by_cat = dash_contrib(year, months)
    colors = cat_colors()
    div_items = sorted(((d, n, ACCENT) for d, n in by_div.items()), key=lambda x: -x[1])
    cat_items = sorted(((c or "Tanpa kategori", n, colors.get(c, CAT_COLORS[0])) for c, n in by_cat.items()),
                       key=lambda x: -x[1])
    csub = f"{plabel} {year} · porsi dari seluruh aktivitas Implemented ({sum(by_div.values())})"
    st.markdown('<div class="sec">Kontribusi divisi dan kategori</div>', unsafe_allow_html=True)
    k1, k2 = st.columns(2, gap="large")
    k1.markdown(contrib_card("Kontribusi per divisi", csub, div_items), unsafe_allow_html=True)
    k2.markdown(contrib_card("Kontribusi per kategori", csub, cat_items), unsafe_allow_html=True)

    # ---- aktivitas yang belum terimplementasi ----
    rows = dash_not_impl(year, months)
    st.markdown(f'<div class="sec">Aktivitas yang belum terimplementasi · {len(rows)} aktivitas</div>',
                unsafe_allow_html=True)
    st.markdown(f'<div class="hint" style="padding:0 0 10px">Periode {esc(plabel)} {year}. Aktivitas ditampilkan bila tidak ada '
                'satu pun bulan berstatus Implemented pada periode tersebut (termasuk yang statusnya belum diisi).</div>',
                unsafe_allow_html=True)
    st.markdown(not_impl_html(rows), unsafe_allow_html=True)


# ----------------------------------------------------------------------------
# HALAMAN: CASCADING MAP (filter divisi, department, tahun -> satu peta)
# ----------------------------------------------------------------------------
def one_line(t, limit=0):
    t = " ".join(str(t).split())
    if limit and len(t) > limit:
        t = t[:limit].rstrip() + "…"
    return esc(t)


def cascade_map_html(div, dept, year, matrix_rows, by_kpi, seqs):
    """Satu peta cascading untuk satu department pada satu tahun.

    Baris = kategori (nama ditulis tegak di kiri). Oval = key strategic activity (nomor n).
    Kotak di bawah oval = aktivitas department ini, diberi kode n.m (m = nomor aktivitas pada KPI tersebut)."""
    colors = cat_colors()
    cats = []
    for r in matrix_rows:
        if r[1] not in cats:
            cats.append(r[1])
    bands = ""
    for cat in cats:
        c = colors.get(cat, CAT_COLORS[0])
        nodes = ""
        for r in matrix_rows:
            if r[1] != cat or not by_kpi.get(r[0]):
                continue
            code = r[5 + DEPTS.index(div)]
            badge = f'<span class="cm-b">{shape(code)}</span>' if code in ROLES else ""
            nomor = one_line(r[2] or "-")
            items = "".join(
                f'<div class="cm-a" title="{one_line(txt)}"><b>{nomor}.{seqs[r[0]].get(aid, "-")}</b>{one_line(txt, 160)}</div>'
                for aid, txt in by_kpi[r[0]])
            nodes += (f'<div class="cm-n"><div class="cm-o">{badge}<span class="cm-t">{nomor}. {one_line(r[3] or "Tanpa nama")}</span></div>'
                      f'<div class="cm-l">{items}</div></div>')
        if not nodes:
            nodes = '<div class="cm-e">Belum ada aktivitas pada kategori ini.</div>'
        bands += (f'<div class="cm-band" style="--c:{c};--bg:{c}1A"><div class="cm-v"><span>{one_line(cat or "Tanpa kategori")}</span></div>'
                  f'<div class="cm-body">{nodes}</div></div>')
    return (f'<div class="cm"><div class="cm-title"><b>PETA STRATEGI Divisi {esc(div)} · {year}</b><span>{esc(dept)}</span></div>'
            f'{bands}</div>')


def page_cascade():
    matrix_rows = fetch_matrix()
    if not matrix_rows:
        st.info("Belum ada data KPI Matrix. Tambahkan aktivitas pada menu KPI Matrix terlebih dahulu.")
        return

    now_y = datetime.now().year
    years = list(range(START_YEAR, max(START_YEAR, now_y) + 2))   # mulai 2026 sampai tahun depan
    c1, c2, c3, _ = st.columns([1.1, 2.6, 1.0, 2.6])
    div = c1.selectbox("Divisi", DEPTS, key="cm_div")
    acts_all = rows_of("SELECT id,kpi_id,department,activity,created FROM activities WHERE division=? ORDER BY id", (div,))
    depts = list(DEPT_OPTIONS.get(div, DEFAULT_DEPTS))
    depts += [d for d in dict.fromkeys(a[2] for a in acts_all) if d not in depts]
    dept = c2.selectbox("Department", depts, key=f"cm_dept_{div}")
    year = c3.selectbox("Tahun", years, index=years.index(now_y) if now_y in years else 0, key="cm_year")

    def created_year(s):
        try:
            return int(str(s)[:4])
        except (TypeError, ValueError):
            return START_YEAR   # data lama tanpa tanggal dianggap sudah ada sejak tahun awal

    # peta tahun X = aktivitas department ini yang sudah ada (dibuat) sampai akhir tahun X
    by_kpi = {}
    for aid, kid, dep, txt, created in acts_all:
        if dep == dept and created_year(created) <= year:
            by_kpi.setdefault(kid, []).append((aid, txt))
    seqs = {r[0]: seq_map(r[0]) for r in matrix_rows}

    st.markdown(f'<div class="hint" style="padding:0 0 4px">Peta {esc(div)} · {esc(dept)} · tahun {year}. '
                'Kode <b>1.2</b> = nomor key strategic activity (1) . nomor aktivitas pada KPI tersebut (2). '
                'Simbol di pojok oval = peran divisi pada key strategic activity. '
                'Peta tiap tahun menampilkan aktivitas yang sudah ada sampai akhir tahun tersebut.</div>',
                unsafe_allow_html=True)
    st.markdown(legend_html(), unsafe_allow_html=True)
    st.write("")
    st.markdown(cascade_map_html(div, dept, year, matrix_rows, by_kpi, seqs), unsafe_allow_html=True)


# ----------------------------------------------------------------------------
# LOGIN
# ----------------------------------------------------------------------------
def login_view():
    st.markdown("<style>[data-testid='stSidebar'],[data-testid='stSidebarCollapsedControl']{display:none}</style>",
                unsafe_allow_html=True)
    st.write("")
    left, right = st.columns([1.15, 1], gap="large")
    with left:
        lg = "".join(f"<span>{shape(c)}{ROLES[c][2]}</span>" for c in ("A", "R", "C", "I"))
        st.markdown(
            f'<div class="hero"><div class="b">{pulse_icon(46)}<b>CorpoPulse</b></div>'
            f'<div><h2>Peran tiap departemen, terlihat dalam satu matriks.</h2>'
            f'<p>Pantau siapa yang bertanggung jawab, menjalankan, dikonsultasi, dan diberi informasi untuk setiap aktivitas strategis.</p></div>'
            f'<div class="lg">{lg}</div></div>', unsafe_allow_html=True)
    with right:
        st.markdown('<div class="login-h">Masuk</div><div class="login-s">Gunakan akun yang diberikan administrator.</div>',
                    unsafe_allow_html=True)
        with st.form("login"):
            u = st.text_input("Username")
            p = st.text_input("Password", type="password")
            ok = S(st.form_submit_button, "Masuk", type="primary")
        if ok:
            usr = verify(u.strip(), p)
            if usr:
                st.session_state.user = usr
                st.rerun()
            else:
                st.error("Username atau password salah.")


# ----------------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------------
def main():
    init_db()
    st.markdown(CSS, unsafe_allow_html=True)
    if "user" not in st.session_state:
        return login_view()

    # muat ulang data akun dari database (perubahan peran/cakupan oleh admin langsung berlaku;
    # akun yang dihapus otomatis dikeluarkan)
    fresh = get_user(st.session_state.user["username"])
    if not fresh:
        del st.session_state["user"]
        st.rerun()
    st.session_state.user = user = fresh
    is_admin = is_admin_user(user)
    initial = esc((user["name"] or user["username"])[:1].upper())
    if is_admin:
        role_label = "admin"
    else:
        role_label = f"user · {user['division'] or '-'}" + (f" · {user['department']}" if user["department"] else "")

    with st.sidebar:
        st.markdown(f'<div class="brand">{pulse_icon()}<b>CorpoPulse</b></div>'
                    '<div class="menu-title">Menu</div>', unsafe_allow_html=True)
        page = st.radio("Menu", MENU, label_visibility="collapsed")
        st.markdown(f'<div class="me"><div class="av">{initial}</div><div><div class="nm">{esc(user["name"])}</div>'
                    f'<div class="rl">{esc(role_label)}</div></div></div>', unsafe_allow_html=True)

    left, right = st.columns([10, 1])
    with left:
        st.markdown(f'<h1 class="ptitle">{esc(page)}</h1><p class="psub">{esc(SUBTITLE[page])}</p>',
                    unsafe_allow_html=True)
    with right:
        with st.popover("☰"):   # menu akun di pojok kanan atas (tampil di semua halaman, termasuk KPI Activities)
            if S(st.button, "👤  Profil", key="b_prof"):
                dlg_profile()
            if is_admin and S(st.button, "⚙  Manajemen akun", key="b_users"):
                dlg_users()
            if S(st.button, "Ganti password", key="b_pw"):
                dlg_password()
            if S(st.button, "Keluar", key="b_out"):
                for k in list(st.session_state.keys()):
                    del st.session_state[k]
                st.rerun()   # kembali ke halaman autentikasi

    if page == "KPI Matrix":
        page_matrix(user)
    elif page == "KPI Activities":
        page_activities(user)
    elif page == "KPI Implementation":
        page_implementation(user)
    elif page == "Summary Dashboard":
        page_dashboard()
    else:
        page_cascade()


main()