"""Task Rework Dashboard: entry point. Lists the pages in the sidebar, with Overview first."""
import streamlit as st

st.set_page_config(page_title="Task Rework Dashboard", layout="wide")

pg = st.navigation([
    st.Page("pages/0_Overview.py", title="Overview", icon="📊", default=True),
    st.Page("pages/1_Upload.py", title="Upload", icon="⬆️"),
    st.Page("pages/2_Versions.py", title="Versions", icon="🗂️"),
    st.Page("pages/3_Cards.py", title="Cards", icon="🃏"),
    st.Page("pages/4_Grades.py", title="Grades", icon="✅"),
    st.Page("pages/5_Task.py", title="Task", icon="📄"),
    st.Page("pages/6_Rules.py", title="Rules", icon="📏"),
    st.Page("pages/9_Settings.py", title="Settings", icon="⚙️"),
])
pg.run()
