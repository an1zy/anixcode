from anixcode.settings import Credentials, Settings, load_auth, load_settings, mask, save_auth, save_settings


def test_roundtrip(tmp_path):
    s = Settings(provider="deepseek", model="deepseek-chat")
    save_settings(s, tmp_path)
    assert load_settings(tmp_path) == s and load_settings(tmp_path).ready
    save_auth({"deepseek": Credentials("sk-1234567890abcdef")}, tmp_path)
    assert load_auth(tmp_path)["deepseek"].api_key == "sk-1234567890abcdef"
    assert mask("sk-1234567890abcdef") == "sk-1…cdef"
    assert load_settings(tmp_path / "missing") == Settings()
