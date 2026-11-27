"""Settings: thresholds, score formula, grader model, ZDR."""
import streamlit as st

from core import config, llm, settings
from core.auth import require_auth
from core.scoring import FORMULAS

st.set_page_config(page_title="Settings", layout="wide")
user = require_auth()
st.title("Settings")

st.subheader("Grader model")
if not config.LLM_API_KEY:
    st.warning("No OpenRouter key yet. Add `LLM_API_KEY=...` to the `.env` file in the app folder, "
               "then restart the app. The key is never shown here.")

current = settings.grader_model()


@st.cache_data(ttl=3600, show_spinner="Fetching the models your key can use…")
def _models(_key_hint: str):
    return llm.list_models()


@st.cache_data(ttl=3600, show_spinner="Checking which models offer zero data retention…")
def _zdr_ids(_key_hint: str):
    return llm.zdr_models()


hint = config.LLM_API_KEY[-4:] if config.LLM_API_KEY else ""
models, err = _models(hint)
zdr_ids, zdr_err = _zdr_ids(hint) if config.LLM_API_KEY else (None, None)
if st.button("Refresh model list"):
    _models.clear()
    _zdr_ids.clear()
    st.rerun()

zdr = st.checkbox("Zero data retention (only use providers that don't store or train on data)",
                  value=settings.zdr())
if zdr and zdr_ids is not None and models:
    n_ok = sum(1 for m in models if m["id"] in zdr_ids)
    only_zdr = st.checkbox(f"Only list models with a ZDR provider ({n_ok} of {len(models)})", value=True)
    if only_zdr:
        models = [m for m in models if m["id"] in zdr_ids]
elif zdr and config.LLM_API_KEY:
    st.caption(f"Couldn't check which models offer ZDR ({zdr_err}). If grading says no endpoint matches your "
               "data policy, pick another model or untick ZDR.")

if models:
    ids = [m["id"] for m in models]
    labels = {}
    for m in models:
        price = ""
        try:
            if m["prompt_price"] is not None:
                price = (f" · ${float(m['prompt_price']) * 1e6:.2f} in / "
                         f"${float(m['completion_price'] or 0) * 1e6:.2f} out per 1M tokens")
        except (TypeError, ValueError):
            pass
        tag = " · ZDR" if zdr_ids and m["id"] in zdr_ids else ""
        labels[m["id"]] = f"{m['name']} ({m['id']}){tag}{price}"
    default = current if current in ids else (llm.FALLBACK_MODEL if llm.FALLBACK_MODEL in ids else ids[0])
    pick = st.selectbox(f"Choose from the {len(ids)} models your key can use", ids,
                        index=ids.index(default), format_func=lambda i: labels[i])
    if current and current not in ids:
        st.info(f"The saved model `{current}` isn't in this list" +
                (" (it has no zero-data-retention provider)." if zdr and zdr_ids is not None
                 and current not in zdr_ids else "."))
else:
    if err:
        st.caption(f"Couldn't fetch the model list ({err}). You can type a model id instead.")
    pick = st.text_input("Model id (as OpenRouter names it)", value=current or llm.FALLBACK_MODEL)


st.subheader("Thresholds")
c1, c2 = st.columns(2)
mt = c1.number_input("Model breaks at or above (%)", 0.0, 100.0,
                     settings.get_float("MODEL_THRESHOLD") * 100, step=0.5)
gt = c2.number_input("Gold breaks below (%)", 0.0, 100.0,
                     settings.get_float("GOLD_THRESHOLD") * 100, step=0.5)

st.subheader("Score formula")
keys = list(FORMULAS)
cur_f = settings.get("SCORE_FORMULA")
formula = st.radio("Score", keys, index=keys.index(cur_f) if cur_f in keys else 0,
                   format_func=lambda k: FORMULAS[k])
st.caption("Earned ÷ positive total is always shown alongside.")

if st.button("Save settings", type="primary"):
    settings.put("GRADER_MODEL", pick.strip(), user)
    settings.put("LLM_ZDR", "true" if zdr else "false", user)
    settings.put("MODEL_THRESHOLD", str(mt / 100), user)
    settings.put("GOLD_THRESHOLD", str(gt / 100), user)
    settings.put("SCORE_FORMULA", formula, user)
    st.success("Saved.")
