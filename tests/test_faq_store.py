from vyxen_core.faq_store import FaqStore


def test_add_and_get():
    store = FaqStore()
    store.add("1", "How to verify?", "Click the button", "123")
    assert store.get("1", "How to verify?") == "Click the button"
    # Case-insensitive fetch
    assert store.get("1", "how to verify?") == "Click the button"


def test_replace_and_list():
    store = FaqStore()
    store.add("1", "What is this?", "A test", "123")
    store.add("1", "What is this?", "Updated answer", "123")
    faqs = store.list("1")
    assert list(faqs.keys()) == ["What is this?"]
    assert faqs["What is this?"] == "Updated answer"


def test_remove():
    store = FaqStore()
    store.add("1", "How to join?", "Use invite", "123")
    assert store.remove("1", "How to join?") is True
    assert store.get("1", "How to join?") is None
    assert store.remove("1", "Missing?") is False
