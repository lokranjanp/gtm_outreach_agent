from pathlib import Path

from streamlit.testing.v1 import AppTest

from outreach.demo import DEMO_PRODUCT
from outreach.storage import Store


def by_label(elements, label):
    return next(element for element in elements if element.label == label)


def test_streamlit_demo_complete_review_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTREACH_DATA_DIR", str(tmp_path))
    app = AppTest.from_file(str(Path(__file__).parents[1] / "app.py"), default_timeout=20).run()
    assert not app.exception
    by_label(app.button, "Research this prospect").click().run()
    assert not app.exception
    assert app.session_state["active_run"]
    assert any("FICTIONAL DEMO RUN" in item.value for item in app.info)
    by_label(app.button, "Approve signals and draft email").click().run()
    assert not app.exception
    assert by_label(app.text_area, "Email body").value
    by_label(app.button, "Check and approve").click().run()
    assert not app.exception
    assert any("Approved — ready" in item.value for item in app.success)


def test_product_library_save_and_live_missing_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTREACH_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    app = AppTest.from_file(str(Path(__file__).parents[1] / "app.py"), default_timeout=20).run()
    by_label(app.radio, "Workspace").set_value("Product library").run()
    by_label(app.text_input, "Product name *").set_value("Test product")
    by_label(app.text_area, "Value proposition *").set_value(
        "Help finance coordinate purchase approvals."
    )
    by_label(app.text_input, "Buyer roles *").set_value("Finance leaders")
    by_label(app.text_area, "Approved benefits *").set_value(
        "Track purchase approvals across teams."
    )
    by_label(app.button, "Save product profile").click().run()
    assert not app.exception
    assert any("Product profile saved" in item.value for item in app.success)
    by_label(app.radio, "Workspace").set_value("New outreach").run()
    by_label(app.radio, "Research mode").set_value("Live research").run()
    assert not app.exception
    assert by_label(app.button, "Research this prospect").disabled


def test_gemini_and_tavily_keys_enable_live_ui_without_openai(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTREACH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GEMINI_API_KEY", "local-test-key")
    monkeypatch.setenv("TAVILY_API_KEY", "local-test-tavily")
    Store(tmp_path).save_product(DEMO_PRODUCT)
    app = AppTest.from_file(str(Path(__file__).parents[1] / "app.py"), default_timeout=20).run()
    by_label(app.radio, "Research mode").set_value("Live research").run()
    assert not app.exception
    assert not by_label(app.button, "Research this prospect").disabled
    assert any("gemini" in caption.value for caption in app.caption)


def test_signal_remove_restore_and_progress_tracker_in_ui(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTREACH_DATA_DIR", str(tmp_path))
    app = AppTest.from_file(str(Path(__file__).parents[1] / "app.py"), default_timeout=20).run()
    by_label(app.button, "Research this prospect").click().run()
    assert any(
        "workflow-flow" in item.value and "Waiting for you" in item.value for item in app.markdown
    )
    by_label(app.button, "Remove signal").click().run()
    assert not app.exception
    assert any(item.label == "Removed signals (1)" for item in app.expander)
    by_label(app.button, "Restore signal").click().run()
    assert not app.exception
    assert any(item.label == "Removed signals (0)" for item in app.expander)


def test_recipient_warning_is_outside_the_email_and_does_not_block_approval(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTREACH_DATA_DIR", str(tmp_path))
    app = AppTest.from_file(str(Path(__file__).parents[1] / "app.py"), default_timeout=20).run()
    by_label(app.text_input, "Recipient role *").set_value("Head of Engineering")
    by_label(app.button, "Research this prospect").click().run()
    assert any("Recipient fit" in item.value for item in app.warning)
    by_label(app.button, "Approve signals and draft email").click().run()
    assert not app.exception
    assert "Recipient fit" not in by_label(app.text_area, "Email body").value
    by_label(app.button, "Check and approve").click().run()
    assert not app.exception
    assert any("Approved — ready" in item.value for item in app.success)
    assert any("Recipient fit" in item.value for item in app.warning)
