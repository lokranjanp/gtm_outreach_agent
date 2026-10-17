"""Streamlit presentation: evidence and decisions stay visible beside the email."""

import streamlit as st

from outreach.config import CONFIG_NAMES, RuntimeConfig, resolve_config
from outreach.demo import DEMO_PRODUCT, SCENARIOS
from outreach.models import LENGTHS, OutreachRequest, ProductProfile, ReviewResponse
from outreach.research import eligible_evidence, public_url
from outreach.runner import Runner
from outreach.storage import Store
from outreach.workflow import word_count

CSS = """
<style>
 .stApp { background: #fafbf9; }
 .block-container { padding-top: 4.5rem; max-width: 1250px; }
 [data-testid="stSidebar"] { background: #eef2ed; }
 h1, h2, h3 { letter-spacing: -.035em; }
 .eyebrow { color: #48775a; font-size: .78rem; font-weight: 700; letter-spacing: .15em; }
 .intro { color: #637064; font-size: 1.05rem; max-width: 740px; margin-bottom: 1.5rem; }
 [data-testid="stMetric"] { background: #f0f4ee; border-radius: 12px; padding: 16px; }
 [data-testid="stVerticalBlockBorderWrapper"] { border-radius: 12px; }
</style>
"""


def execute(callback):
    try:
        with st.status("Working on your outreach…", expanded=True) as status:
            callback(lambda text: status.write(text))
            status.update(label="Saved — ready for your review", state="complete", expanded=False)
    except Exception:
        # Provider exceptions can contain credentials and source content. Do not render them.
        st.error(
            "This step could not finish. Your completed steps are saved. Check API credentials, model access and connectivity, then retry the saved run."
        )
        return
    st.rerun()


def source_form(prefix):
    url = st.text_input("Source URL (optional if text is supplied)", key=f"{prefix}_url")
    text = st.text_area(
        "Paste the article or post, including its date", key=f"{prefix}_text", height=120
    )
    return url, text


def cancel_button(runner, run_id):
    if st.button("Cancel this run", key=f"cancel_{run_id}", type="tertiary"):
        execute(lambda progress: runner.resume(run_id, ReviewResponse(action="cancel"), progress))


def product_page(store):
    st.header("Your product library")
    st.caption("Give the agent approved benefits and proof points it can use in a pitch.")
    products = store.products()
    selected = st.selectbox(
        "Product profile",
        [None] + [p.id for p in products],
        format_func=lambda key: (
            "Create a new profile" if key is None else next(p.name for p in products if p.id == key)
        ),
    )
    existing = next((p for p in products if p.id == selected), None)
    with st.form(f"product_{selected}"):
        name = st.text_input("Product name *", value=existing.name if existing else "")
        value = st.text_area(
            "Value proposition *",
            value=existing.value_proposition if existing else "",
            placeholder="What does it help the buyer do?",
        )
        roles = st.text_input("Buyer roles *", value=existing.buyer_roles if existing else "")
        benefits = st.text_area("Approved benefits *", value=existing.benefits if existing else "")
        proof = st.text_area(
            "Approved proof points (optional)",
            value=existing.proof_points if existing else "",
            help="Only include claims you are comfortable using in outreach.",
        )
        website = st.text_input(
            "Product website (optional)", value=existing.website if existing else ""
        )
        submitted = st.form_submit_button("Save product profile", type="primary")
    if submitted:
        try:
            if website:
                public_url(website)
            kwargs = {"id": existing.id} if existing else {}
            product = ProductProfile(
                name=name,
                value_proposition=value,
                buyer_roles=roles,
                benefits=benefits,
                proof_points=proof,
                website=website,
                **kwargs,
            )
            store.save_product(product)
            st.success(
                "Product profile saved. Existing outreach runs retain their original profile."
            )
        except ValueError:
            st.error(
                "Enter a product name, value proposition (10+ characters), buyer roles and benefits (10+ characters). Check the website URL if supplied."
            )


def new_page(store, runner, config: RuntimeConfig):
    live_ready = config.ready
    st.header("Start with a prospect")
    st.caption("One company. One person. A message with a reason to exist.")
    mode = st.radio("Research mode", ["Offline demo", "Live research"], horizontal=True)
    demo = mode == "Offline demo"
    if demo:
        st.info(
            "Offline demo uses fictional companies, people and events, with deterministic sample writing. No API calls are made."
        )
        scenario = st.selectbox("Demo company", list(SCENARIOS))
        sample = SCENARIOS[scenario]
    elif not live_ready:
        st.warning(config.problem)
    products = store.products()
    if demo:
        # Sample writing is specific to this fictional product and its approved benefits.
        products = [DEMO_PRODUCT]
    if not products:
        st.info("Create a product profile in Product library first, or switch to Offline demo.")
        return
    with st.form(f"new_{mode}_{scenario if demo else 'live'}"):
        product = st.selectbox("Product *", products, format_func=lambda p: p.name)
        col1, col2 = st.columns(2)
        company = col1.text_input("Company name *", value=scenario if demo else "")
        website = col2.text_input("Company website (optional)")
        name = col1.text_input("Recipient name *", value=sample["recipient"] if demo else "")
        role = col2.text_input("Recipient role *", value=sample["role"] if demo else "")
        profile = st.text_input("Recipient profile URL (optional)")
        contacted = st.checkbox("We have contacted this person previously")
        with st.expander("Previous outreach context — fill this in for a follow-up"):
            previous = st.text_area("Previous message or a summary")
            contact_date = st.text_input(
                "Approximate contact date", placeholder="e.g. early October 2026"
            )
            reply = st.text_area("Any reply (leave blank if none)")
            skip = st.checkbox(
                "Continue without previous outreach details",
                help="The draft will avoid assumptions about earlier correspondence.",
            )
        length = st.selectbox("Email body length", [*LENGTHS, "Custom"], index=1)
        target = st.number_input(
            "Custom word target",
            min_value=40,
            max_value=250,
            value=120,
            help="Used when Custom is selected; allows ±10%, within 40–250 words.",
        )
        submitted = st.form_submit_button(
            "Research this prospect", type="primary", disabled=not demo and not live_ready
        )
    if submitted:
        try:
            for url in (website, profile):
                if url:
                    public_url(url)
            low, high = LENGTHS.get(
                length, (max(40, round(target * 0.9)), min(250, round(target * 1.1)))
            )
            request = OutreachRequest(
                company_name=company,
                company_url=website,
                recipient_name=name,
                recipient_role=role,
                recipient_url=profile,
                previously_contacted=contacted,
                previous_message=previous,
                contacted_date=contact_date,
                previous_reply=reply,
                skip_history=skip,
                min_words=low,
                max_words=high,
            )
            run_id = runner.create(request, product, "demo" if demo else "live")
            st.session_state.active_run = run_id
            st.session_state.open_run = True
            execute(lambda progress: runner.start(run_id, request, product, progress))
        except ValueError as exc:
            st.error(str(exc))


def evidence_card(item, selected=False):
    with st.container(border=True):
        st.markdown(f"**{'✓ ' if selected else ''}{item['title']}**")
        st.write(item["claim"])
        st.caption(
            f"{item['kind'].title()} · {item['verification'].replace('_', ' ')} · {item['confidence']} confidence"
        )
        event = item.get("event_date")
        published = item.get("published_at")
        st.caption(
            f"Event: {event or 'unknown'} · Published: {published or 'unknown'} · Retrieved: {item.get('retrieved_at', '')[:10]}"
        )
        st.caption("Supporting excerpt")
        st.write(item["quote"])
        if item.get("source_url"):
            st.link_button("Open source", item["source_url"])


def research_panel(state):
    company = state.get("company", {})
    if not company:
        return
    st.subheader(company["name"])
    st.caption(company["domain"])
    cols = st.columns(3)
    for col, label, key in zip(
        cols,
        ("Industry", "Company size", "Founded"),
        ("industry", "employee_size", "founding_year"),
        strict=True,
    ):
        fact = company.get(key, {})
        col.metric(label, fact.get("value") or "Unknown")
    ceo = company.get("ceo", {}).get("value")
    st.caption(f"CEO: {ceo or 'Unknown'}")
    with st.expander("Research coverage and source-backed company facts"):
        for gap in state.get("gaps", []):
            st.write("• " + gap)
        docs = {d["id"]: d for d in state.get("documents", [])}
        for key, label in (
            ("industry", "Industry"),
            ("employee_size", "Size"),
            ("founding_year", "Founded"),
            ("ceo", "CEO"),
        ):
            fact = company.get(key, {})
            st.write(f"{label}: {fact.get('value') or 'Unknown'}")
            if fact.get("quote"):
                st.caption(fact["quote"])
            for doc_id in fact.get("document_ids", []):
                doc = docs.get(doc_id)
                if doc and doc["url"]:
                    st.link_button(doc["title"], doc["url"], key=f"fact_{key}_{doc_id}")
    with st.expander(f"All research signals ({len(state.get('evidence', []))})"):
        selected = {s["evidence_id"] for s in state.get("selected", [])}
        for item in state.get("evidence", []):
            evidence_card(item, item["id"] in selected)


def input_review(runner, run_id, state):
    st.subheader("A little more context")
    req = state["request"]
    with st.form(f"missing_{run_id}"):
        company = st.text_input("Company name", value=req["company_name"])
        name = st.text_input("Recipient name", value=req["recipient_name"])
        role = st.text_input("Recipient role", value=req["recipient_role"])
        previous, day, reply, skip = (
            req["previous_message"],
            req["contacted_date"],
            req["previous_reply"],
            req["skip_history"],
        )
        if req["previously_contacted"]:
            st.caption(
                "Add previous outreach context, or explicitly choose to continue without it."
            )
            previous = st.text_area("Previous message or summary", value=previous)
            day = st.text_input("Approximate contact date", value=day)
            reply = st.text_area("Any reply", value=reply)
            skip = st.checkbox("Continue without previous outreach details", value=skip)
        submit = st.form_submit_button("Continue", type="primary")
    if submit:
        response = ReviewResponse(
            action="submit",
            company_name=company,
            recipient_name=name,
            recipient_role=role,
            previous_message=previous,
            contacted_date=day,
            previous_reply=reply,
            skip_history=skip,
        )
        execute(lambda progress: runner.resume(run_id, response, progress))


def company_review(runner, run_id, pause):
    st.subheader("Confirm the company")
    candidates = pause.get("candidates", [])
    if not candidates:
        st.info(
            "We could not identify a company confidently. Enter its official website to continue."
        )
    with st.form(f"company_{run_id}"):
        chosen = st.selectbox(
            "Matching companies",
            [""] + [c["domain"] for c in candidates],
            format_func=lambda d: (
                "Enter a website instead"
                if not d
                else next(
                    f"{c['name']} — {d}: {c['description']}" for c in candidates if c["domain"] == d
                )
            ),
        )
        website = st.text_input("Official company website")
        submit = st.form_submit_button("Confirm company", type="primary")
    if submit:
        execute(
            lambda progress: runner.resume(
                run_id,
                ReviewResponse(action="submit", candidate_domain=chosen, source_url=website),
                progress,
            )
        )


def signal_review(runner, run_id, state):
    st.subheader("Choose the reason to reach out")
    selection = state.get("selection", {})
    st.write(selection.get("rationale", ""))
    recommendations = selection.get("recommendations", [])
    eligible = [e.model_dump() for e in eligible_evidence(state.get("evidence", []))]
    by_id = {e["id"]: e for e in eligible}
    defaults = [s["evidence_id"] for s in recommendations if s["evidence_id"] in by_id]
    for signal in recommendations:
        if signal["evidence_id"] in by_id:
            evidence_card(by_id[signal["evidence_id"]], True)
            st.write("Product connection: " + signal["product_connection"])
            st.write("Why this person: " + signal["recipient_relevance"])
            st.caption(signal["company_context"])
    if not defaults:
        st.warning(
            "No sufficiently relevant hook was found. Add a credible source or request another research pass."
        )
    with st.form(f"signals_{run_id}_{state.get('research_pass', 0)}"):
        chosen = st.multiselect(
            "Approve one or two signals",
            list(by_id),
            default=defaults,
            format_func=lambda key: by_id[key]["title"],
            max_selections=2,
        )
        feedback = st.text_area("Guidance for the draft (optional)")
        approve = st.form_submit_button(
            "Approve signals and draft email", type="primary", disabled=not eligible
        )
    if approve:
        execute(
            lambda progress: runner.resume(
                run_id,
                ReviewResponse(action="approve", evidence_ids=chosen, feedback=feedback),
                progress,
            )
        )
    if selection.get("rejected_reasons"):
        with st.expander("Why other hooks were not selected"):
            for reason in selection["rejected_reasons"]:
                st.write(reason)
    with st.expander("Add evidence or research again"):
        with st.form(f"research_{run_id}_{state.get('research_pass', 0)}"):
            url, text = source_form(f"source_{run_id}_{state.get('research_pass', 0)}")
            retry = st.form_submit_button("Research again with this context")
        if retry:
            execute(
                lambda progress: runner.resume(
                    run_id,
                    ReviewResponse(action="research", source_url=url, source_text=text),
                    progress,
                )
            )


def draft_review(runner, run_id, state):
    st.subheader("Make it sound like you")
    draft = state["draft"]
    for issue in state.get("quality_issues", []):
        st.warning(issue)
    if not state.get("quality_issues"):
        st.success("Draft checks passed. Review the wording and approve when ready.")
    key = f"{run_id}_{len(state.get('versions', []))}"
    with st.form(f"draft_{key}"):
        subject = st.text_input("Subject", value=draft["subject"])
        body = st.text_area("Email body", value=draft["body"], height=300)
        st.caption(
            f"Saved body: {word_count(draft['body'])} words · Target: {state['request']['min_words']}–{state['request']['max_words']}"
        )
        feedback = st.text_area(
            "What should change?",
            placeholder="e.g. Make the product connection more direct and soften the CTA.",
        )
        col1, col2, col3 = st.columns(3)
        approve = col1.form_submit_button("Check and approve", type="primary")
        revise = col2.form_submit_button("Regenerate with feedback")
        signals = col3.form_submit_button("Revisit signals")
    if approve or revise or signals:
        action = "approve" if approve else "revise" if revise else "signals"
        execute(
            lambda progress: runner.resume(
                run_id,
                ReviewResponse(action=action, subject=subject, body=body, feedback=feedback),
                progress,
            )
        )
    with st.expander("Why this email is personalized"):
        by_id = {e["id"]: e for e in state["evidence"]}
        for signal in state["selected"]:
            evidence_card(by_id[signal["evidence_id"]], True)
            st.write(signal["product_connection"])
            st.caption(signal["recipient_relevance"])


def run_page(store, runner, config: RuntimeConfig):
    runs = store.runs()
    if not runs:
        st.info("No saved runs yet. Start an outreach to see its research and draft here.")
        return
    ids = [r["id"] for r in runs]
    active = st.session_state.get("active_run")
    run_id = st.selectbox(
        "Saved outreach",
        ids,
        index=ids.index(active) if active in ids else 0,
        format_func=lambda key: next(
            f"{r['company'] or 'Unnamed company'} · {r['mode']} · {r['status']} · {r['created_at'][:16]}"
            for r in runs
            if r["id"] == key
        ),
    )
    st.session_state.active_run = run_id
    run = store.run(run_id)
    if run["mode"] == "demo":
        st.info("FICTIONAL DEMO RUN — sources and events below are sample data, not live research.")
    if run["mode"] == "live" and not config.ready:
        st.warning(config.problem + " Saved research remains available below.")
    snapshot = runner.snapshot(run_id)
    state, pauses = snapshot["state"], snapshot["interrupts"]
    st.caption(f"Run {run_id} · {state.get('phase', 'Starting')}")
    if not state:
        st.warning(
            "This run did not start successfully. Create a new outreach with the same inputs."
        )
        return
    research_panel(state)
    if pauses:
        pause = pauses[0]
        if pause.get("error"):
            st.warning(pause["error"])
        handlers = {
            "inputs": lambda: input_review(runner, run_id, state),
            "company": lambda: company_review(runner, run_id, pause),
            "signals": lambda: signal_review(runner, run_id, state),
            "draft": lambda: draft_review(runner, run_id, state),
        }
        handlers[pause["kind"]]()
        cancel_button(runner, run_id)
    elif state.get("status") == "approved":
        st.success("Approved — ready to copy or download.")
        draft = state["draft"]
        email = f"Subject: {draft['subject']}\n\n{draft['body']}"
        st.code(email, language=None, wrap_lines=True)
        st.download_button(
            "Download approved email", email, file_name="outreach-email.txt", mime="text/plain"
        )
    elif state.get("status") == "cancelled":
        st.info("This run was cancelled. Its research and history are retained.")
    elif snapshot["next"]:
        st.warning("The last step did not finish. Completed research and drafts are saved.")
        if st.button("Retry unfinished step", type="primary"):
            execute(lambda progress: runner.retry(run_id, progress))
    with st.expander(f"Draft history ({len(state.get('versions', []))})"):
        for index, version in enumerate(state.get("versions", []), 1):
            st.caption(f"Version {index} · {version['origin']} · {version['created_at'][:19]}")
            st.write(version["draft"]["subject"])
            st.text(version["draft"]["body"])
    with st.expander("Product profile used for this run"):
        product = state.get("product", {})
        st.write(product.get("name", ""))
        st.write(product.get("value_proposition", ""))
        st.caption(product.get("benefits", ""))


def main():
    st.set_page_config(
        page_title="Signalcraft · Personalized outreach", page_icon="✦", layout="wide"
    )
    st.markdown(CSS, unsafe_allow_html=True)
    st.markdown('<div class="eyebrow">SIGNALCRAFT / OUTREACH STUDIO</div>', unsafe_allow_html=True)
    st.title("A real reason to reach out.")
    st.markdown(
        '<div class="intro">Turn company research into an email that belongs in this person’s inbox. You choose the signals. You approve the words.</div>',
        unsafe_allow_html=True,
    )
    credentials = {}
    try:
        credentials = {key: st.secrets.get(key, "") for key in CONFIG_NAMES}
    except FileNotFoundError:
        pass
    config = resolve_config(credentials)
    store = Store(config.data_dir)
    runner = Runner(store, credentials)
    with st.sidebar:
        st.markdown("### ✦ Signalcraft")
        if st.session_state.pop("open_run", False):
            st.session_state.navigation = "Saved outreach"
        page = st.radio(
            "Workspace", ["New outreach", "Product library", "Saved outreach"], key="navigation"
        )
        st.divider()
        st.caption(f"Model: {config.provider} · {config.model}")
        st.caption(
            "Live research " + ("is configured ✓" if config.ready else "needs configuration")
        )
        st.caption("Local storage · Human reviewed · Drafts only")
    if page == "Product library":
        product_page(store)
    elif page == "New outreach":
        new_page(store, runner, config)
    else:
        run_page(store, runner, config)
