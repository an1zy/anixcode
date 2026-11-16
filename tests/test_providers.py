from anixcode import providers
from anixcode.settings import Settings, load_settings, save_settings


def test_user_providers_are_listed_first_and_resolved(tmp_path):
    s = Settings(custom_providers=[{"id": "user:my-anix", "name": "Мой ANIX", "base_url": "http://localhost:3001/v1"}])
    ids = [p.id for p in providers.all_providers(s)]
    assert ids[0] == "user:my-anix" and "deepseek" in ids
    p = providers.get("user:my-anix", s)
    assert p.is_user and p.base_url == "http://localhost:3001/v1"
    assert providers.get("deepseek", s).name == "DeepSeek"
    assert providers.get("user:gone", s).id == "custom"
    save_settings(s, tmp_path)
    assert load_settings(tmp_path).custom_providers == s.custom_providers


def test_make_id_is_unique_and_safe():
    assert providers.make_id("Мой ANIX", set()) == "user:anix"
    assert providers.make_id("Мой", set()) == "user:provider"
    assert providers.make_id("Router", {"user:router"}) == "user:router-2"


def test_broken_custom_entries_are_dropped(tmp_path):
    (tmp_path / "settings.json").write_text('{"custom_providers": [{"id": "x"}, 5, '
                                            '{"id": "user:a", "name": "A", "base_url": "http://a"}]}')
    assert [d["id"] for d in load_settings(tmp_path).custom_providers] == ["user:a"]
