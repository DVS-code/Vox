from vyxen_core.action_journal import ActionJournal


def test_action_journal_records_and_limits():
    journal = ActionJournal(max_entries_per_user=2)
    journal.record("1", "create_role", {"role_id": 10}, None, {"role_id": 10}, True)
    journal.record("1", "create_channel", {"channel_id": 20}, None, {"channel_id": 20}, True)
    journal.record("1", "move_channel", {"channel_id": 20}, {"from": 1}, {"to": 2}, True)

    last = journal.last("1")
    assert last is not None
    assert last.action_type == "move_channel"
    # Oldest entry should have been trimmed
    assert journal.last_reversible("1") is not None


def test_pop_last_reversible():
    journal = ActionJournal()
    journal.record("user", "create_role", {"role_id": 1}, None, {"role_id": 1}, True)
    journal.record("user", "ban_member", {"member_id": 2}, None, None, False)
    entry = journal.pop_last_reversible("user")
    assert entry is not None
    assert entry.action_type == "create_role"
    assert journal.pop_last_reversible("user") is None
