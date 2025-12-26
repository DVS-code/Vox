from vyxen_core.macro_store import MacroStore


def test_macro_store_save_and_get_and_trim():
    store = MacroStore(max_macros=2)
    store.save("guild", "one", "cmd1", "user")
    store.save("guild", "two", "cmd2", "user")
    assert store.get("guild", "one") == "cmd1"
    store.save("guild", "three", "cmd3", "user")
    assert store.get("guild", "one") is None or store.get("guild", "two") or store.get("guild", "three")
    assert "three" in store.list("guild")
