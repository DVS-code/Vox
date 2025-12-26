from vyxen_core.setup_wizard import SetupWizardStore


def test_wizard_flow():
    store = SetupWizardStore()
    session = store.start("guild", "user")
    assert session.stage == 0
    prompt = store.next_prompt(session)
    assert "purpose" in prompt.lower()

    prompt2, done = store.advance(session, "car community")
    assert done is False
    assert "roles" in prompt2.lower()

    prompt3, done = store.advance(session, "Admin, Mod, Member")
    assert done is False
    assert "channels" in prompt3.lower()

    prompt4, done = store.advance(session, "general, rules")
    assert done is False
    assert "moderation" in prompt4.lower()

    prompt5, done = store.advance(session, "medium")
    assert done is False
    assert "welcome" in prompt5.lower()

    summary, done = store.advance(session, "warm")
    assert done is True
    assert "Setup plan" in summary
    assert "car community" in summary
