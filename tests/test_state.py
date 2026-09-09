from app.state import StateStore


def test_new_state_has_no_synced(state_db_path):
    state = StateStore(state_db_path)
    assert state.get_synced_keys() == set()
    state.close()


def test_mark_synced_excludes_from_future(state_db_path):
    state = StateStore(state_db_path)
    state.mark_synced("chat:1")
    assert state.get_synced_keys() == {"chat:1"}
    state.close()


def test_mark_failed_does_not_exclude(state_db_path):
    state = StateStore(state_db_path)
    state.mark_failed("chat:2", reason="Ombor unreachable")
    assert state.get_synced_keys() == set()
    state.close()


def test_mark_not_an_order(state_db_path):
    state = StateStore(state_db_path)
    state.mark_not_an_order("chat:3")
    assert state.get_synced_keys() == set()
    state.close()


def test_list_needs_review(state_db_path):
    state = StateStore(state_db_path)
    state.mark_needs_review("chat:4", reason="kod topilmadi")
    reviews = state.list_needs_review()
    assert len(reviews) == 1
    assert reviews[0].sync_key == "chat:4"
    state.close()


def test_state_persists_across_reopen(state_db_path):
    state1 = StateStore(state_db_path)
    state1.mark_synced("chat:5")
    state1.close()

    state2 = StateStore(state_db_path)
    assert state2.get_synced_keys() == {"chat:5"}
    state2.close()
