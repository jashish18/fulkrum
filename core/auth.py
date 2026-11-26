"""Shared-password gate (spec 10/12). Call `require_auth()` at the top of every page."""
from __future__ import annotations

import hmac

import streamlit as st

from core import config


def require_auth() -> str:
    """Stop the page unless the shared password was entered. Returns the user's display name."""
    if st.session_state.get("authed"):
        return st.session_state.get("user_name") or "team"
    if not config.APP_PASSWORD and config.REQUIRE_PASSWORD:
        st.title("Task Rework Dashboard")
        st.error("APP_PASSWORD is not set. Add it to the app's environment variables and restart.")
        st.stop()
    if not config.APP_PASSWORD:
        st.warning("APP_PASSWORD is not set, so the app is open. Set it before deploying.")
        st.session_state["authed"] = True
        st.session_state.setdefault("user_name", "local")
        return "local"
    st.title("Task Rework Dashboard")
    with st.form("login"):
        name = st.text_input("Your name (for the audit log)")
        pw = st.text_input("Team password", type="password")
        ok = st.form_submit_button("Sign in")
    if ok:
        if hmac.compare_digest(pw.encode(), config.APP_PASSWORD.encode()):
            st.session_state["authed"] = True
            st.session_state["user_name"] = name.strip() or "team"
            st.rerun()
        else:
            st.error("Wrong password.")
    st.stop()
