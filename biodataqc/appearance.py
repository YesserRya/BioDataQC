"""Presentation only: theme accents and an isolated, offline decorative header."""

from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components


HEADER_PATH = Path(__file__).resolve().parent / "assets/dna_header.html"


def apply_style():
    st.markdown("""<style>
    .block-container{padding-top:3.7rem;padding-bottom:2rem;max-width:1440px}
    [data-testid="stAppViewContainer"]{background-image:radial-gradient(ellipse at 92% 0%,#bfece929,transparent 45%)}
    [data-testid="stSidebar"]{border-right:1px solid #294657}
    [data-testid="stSidebar"] h1{font-size:1.75rem;letter-spacing:-.8px;font-weight:700}
    [data-testid="stSidebar"] [data-testid="stCaptionContainer"]{line-height:1.6}
    [data-testid="stMetric"]{background:var(--secondary-background-color,#fff);border:1px solid var(--border-color,#dce7ed);
      border-radius:13px;padding:17px 19px;box-shadow:0 3px 13px #132c4205;border-top:3px solid #80c6c2}
    [data-testid="stMetricLabel"]{font-size:.82rem;font-weight:550}
    [data-testid="stMetricValue"]{font-variant-numeric:tabular-nums;letter-spacing:-1px}
    [data-testid="stTabs"] [data-baseweb="tab-list"]{gap:5px;background:var(--secondary-background-color,#fff);
      border:1px solid var(--border-color,#dce7ed);border-radius:11px;padding:4px 9px;box-shadow:0 3px 13px #132c4205}
    [data-testid="stTabs"] [data-baseweb="tab"]{height:43px;padding:0 11px;border-radius:6px;white-space:nowrap}
    [data-testid="stTabs"] [data-baseweb="tab"] p{font-size:.87rem;font-weight:600}
    [data-testid="stTabs"] [data-baseweb="tab"][aria-selected="true"]{background:#278e8910}
    [data-testid="stMain"] [data-testid="stExpander"]{background:var(--secondary-background-color,#fff);border-radius:11px}
    [data-testid="stDataFrame"]{border-radius:11px;overflow:hidden;border:1px solid var(--border-color,#dce7ed)}
    [data-testid="stButton"] button,[data-testid="stDownloadButton"] button{font-weight:600;min-height:2.55rem}
    [data-testid="stButton"] button:focus-visible,[data-testid="stDownloadButton"] button:focus-visible{outline:3px solid #4ab8b0;outline-offset:3px}
    [data-testid="stAlert"]{border-radius:11px}
    h2,h3{letter-spacing:-.35px}
    @media(max-width:750px){.block-container{padding-left:1rem;padding-right:1rem;padding-top:3.4rem}
      [data-testid="stMetric"]{padding:12px 14px}[data-testid="stTabs"] [data-baseweb="tab"]{padding:0 8px}}
    </style>""", unsafe_allow_html=True)


def render_header(*, animate=True):
    markup = HEADER_PATH.read_text(encoding="utf-8")
    markup = markup.replace("__ANIMATE_DNA__", "true" if animate else "false")
    if hasattr(st, "iframe"):
        st.iframe(markup, height=216, tab_index=-1,
                  alt="BioDataQC delivery-readiness workspace")
    else:
        components.html(markup, height=216, scrolling=False, tab_index=-1)
