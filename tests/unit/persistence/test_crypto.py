import pytest
from cryptography.fernet import Fernet, InvalidToken

import persistence.crypto as _crypto_mod
from persistence.crypto import (
    _CIPHERTEXT_PREFIX,
    _home,
    _is_valid_b64_key,
    _keyring_get,
    _keyring_set,
    _load_or_create_file_key,
    _load_or_create_key,
    clear_key_cache,
    decrypt,
    encrypt,
)


@pytest.fixture(autouse=True)
def _reset_key_cache():
    """Clear the in-process key cache before and after every test."""
    clear_key_cache()
    yield
    clear_key_cache()


def test_encrypt_decrypt_round_trip(monkeypatch):
    store: dict[str, str] = {}
    monkeypatch.setattr("persistence.crypto._keyring_get", lambda u: store.get(u))
    monkeypatch.setattr("persistence.crypto._keyring_set", lambda u, v: store.__setitem__(u, v))
    ciphertext = encrypt("hello")
    assert ciphertext.startswith(_CIPHERTEXT_PREFIX)
    assert decrypt(ciphertext) == "hello"


def test_encrypt_produces_prefixed_ciphertext(monkeypatch):
    store: dict[str, str] = {}
    monkeypatch.setattr("persistence.crypto._keyring_get", lambda u: store.get(u))
    monkeypatch.setattr("persistence.crypto._keyring_set", lambda u, v: store.__setitem__(u, v))
    out = encrypt("payload")
    assert out.startswith(_CIPHERTEXT_PREFIX)
    _, _, payload = out.partition(_CIPHERTEXT_PREFIX)
    Fernet(_load_or_create_key()).decrypt(payload.encode("ascii"))


def test_decrypt_passes_through_plaintext():
    assert decrypt("plain old text") == "plain old text"
    assert decrypt("") == ""
    assert decrypt("not-encrypted-just-a-coincidence") == "not-encrypted-just-a-coincidence"


def test_decrypt_rejects_garbage_ciphertext(monkeypatch):
    store: dict[str, str] = {}
    monkeypatch.setattr("persistence.crypto._keyring_get", lambda u: store.get(u))
    monkeypatch.setattr("persistence.crypto._keyring_set", lambda u, v: store.__setitem__(u, v))
    bad = _CIPHERTEXT_PREFIX + "AAAA" * 16
    with pytest.raises(InvalidToken):
        decrypt(bad)


def test_key_persists_across_calls(monkeypatch):
    store: dict[str, str] = {}
    monkeypatch.setattr("persistence.crypto._keyring_get", lambda u: store.get(u))
    monkeypatch.setattr("persistence.crypto._keyring_set", lambda u, v: store.__setitem__(u, v))
    k1 = _load_or_create_key()
    k2 = _load_or_create_key()
    assert k1 == k2
    assert len(k1) == 44
    assert Fernet(k1) is not None


def test_key_falls_back_to_file_when_keyring_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr("persistence.crypto._keyring_get", lambda u: (_ for _ in ()).throw(
        RuntimeError("no backend")
    ))
    monkeypatch.setattr("persistence.crypto._keyring_set", lambda u, v: (_ for _ in ()).throw(
        RuntimeError("no backend")
    ))
    monkeypatch.setattr("persistence.crypto._home", lambda: tmp_path)

    key = _load_or_create_key()
    fallback = tmp_path / ".wits" / "fernet.key"
    assert fallback.exists()
    assert len(key) == 44
    assert _is_valid_b64_key(fallback.read_text(encoding="utf-8").strip())
    Fernet(key)

    key2 = _load_or_create_key()
    assert key == key2


def test_key_falls_back_raises_when_both_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr("persistence.crypto._keyring_get", lambda u: (_ for _ in ()).throw(
        RuntimeError("no backend")
    ))
    monkeypatch.setattr("persistence.crypto._keyring_set", lambda u, v: (_ for _ in ()).throw(
        RuntimeError("no backend")
    ))
    monkeypatch.setattr("persistence.crypto._home", lambda: tmp_path)
    (tmp_path / ".wits").write_text("not a dir", encoding="utf-8")

    with pytest.raises(RuntimeError, match="no keyring backend and no fallback key file"):
        _load_or_create_key()


def test_file_fallback_returns_existing_valid_key(monkeypatch, tmp_path):
    monkeypatch.setattr("persistence.crypto._keyring_get", lambda u: (_ for _ in ()).throw(
        RuntimeError("no backend")
    ))
    monkeypatch.setattr("persistence.crypto._keyring_set", lambda u, v: (_ for _ in ()).throw(
        RuntimeError("no backend")
    ))
    monkeypatch.setattr("persistence.crypto._home", lambda: tmp_path)

    first = _load_or_create_file_key()
    second = _load_or_create_file_key()
    assert first == second


def test_is_valid_b64_key():
    assert _is_valid_b64_key(None) is False
    assert _is_valid_b64_key("") is False
    assert _is_valid_b64_key("not-base64") is False
    assert _is_valid_b64_key("A" * 43) is False
    real = Fernet.generate_key().decode("ascii")
    assert _is_valid_b64_key(real) is True


def test_real_keyring_path_round_trip():
    key = _load_or_create_key()
    Fernet(key)
    key_again = _load_or_create_key()
    assert key == key_again


def test_real_home_path_round_trip(monkeypatch, tmp_path):
    monkeypatch.setattr("persistence.crypto._keyring_get", lambda u: (_ for _ in ()).throw(
        RuntimeError("no backend")
    ))
    monkeypatch.setattr("persistence.crypto._keyring_set", lambda u, v: (_ for _ in ()).throw(
        RuntimeError("no backend")
    ))
    monkeypatch.setattr("persistence.crypto._home", lambda: tmp_path)

    key = _load_or_create_file_key()
    Fernet(key)
    second = _load_or_create_file_key()
    assert key == second


def test_home_function_returns_path():
    from pathlib import Path
    assert isinstance(_home(), Path)


def test_keyring_helpers_call_real_keyring(monkeypatch):
    captured: dict[str, str] = {}
    import keyring as real_keyring

    monkeypatch.setattr(
        real_keyring, "get_password", lambda s, u: captured.get((s, u))
    )
    monkeypatch.setattr(
        real_keyring, "set_password", lambda s, u, v: captured.__setitem__((s, u), v)
    )
    assert _keyring_get("u") is None
    _keyring_set("u", "v")
    assert _keyring_get("u") == "v"


def test_file_fallback_raises_when_existing_key_is_corrupt(monkeypatch, tmp_path):
    """H5: a corrupt key file must raise RuntimeError, not silently overwrite."""
    monkeypatch.setattr("persistence.crypto._home", lambda: tmp_path)
    fallback = tmp_path / ".wits"
    fallback.mkdir(parents=True, exist_ok=True)
    key_file = fallback / "fernet.key"
    key_file.write_text("garbage-not-valid", encoding="utf-8")
    original_content = key_file.read_text(encoding="utf-8")
    with pytest.raises(RuntimeError, match="corrupt"):
        _load_or_create_file_key()
    # Must NOT have overwritten the corrupt file.
    assert key_file.read_text(encoding="utf-8") == original_content


def test_file_fallback_swallows_chmod_errors(monkeypatch, tmp_path):
    monkeypatch.setattr("persistence.crypto._home", lambda: tmp_path)

    def raising_chmod(self, mode):
        raise NotImplementedError("chmod not supported")

    chmod_target = (
        "pathlib.PosixPath.chmod"
        if hasattr(tmp_path, "chmod")
        else "pathlib.Path.chmod"
    )
    monkeypatch.setattr(chmod_target, raising_chmod)
    key = _load_or_create_file_key()
    Fernet(key)


# ── New tests for H5, M5 fixes ───────────────────────────────────────────────

def test_corrupt_key_file_raises_runtime_error_not_overwrite(monkeypatch, tmp_path):
    """H5: corrupt key file must raise RuntimeError instead of silently overwriting."""
    monkeypatch.setattr("persistence.crypto._home", lambda: tmp_path)
    key_dir = tmp_path / ".wits"
    key_dir.mkdir(parents=True, exist_ok=True)
    key_file = key_dir / "fernet.key"
    key_file.write_text("this-is-definitely-not-a-valid-fernet-key", encoding="utf-8")
    original = key_file.read_text(encoding="utf-8")

    with pytest.raises(RuntimeError, match="corrupt"):
        _load_or_create_file_key()

    # File content must be unchanged — no silent overwrite.
    assert key_file.read_text(encoding="utf-8") == original


def test_clear_key_cache_allows_reload(monkeypatch, tmp_path):
    """M5/clear_key_cache: after clearing, the next call reloads the key."""
    store: dict[str, str] = {}
    monkeypatch.setattr("persistence.crypto._keyring_get", lambda u: store.get(u))
    monkeypatch.setattr("persistence.crypto._keyring_set", lambda u, v: store.__setitem__(u, v))

    k1 = _load_or_create_key()
    assert _crypto_mod._KEY_CACHE is not None

    clear_key_cache()
    assert _crypto_mod._KEY_CACHE is None

    k2 = _load_or_create_key()
    # Same key is loaded because it was persisted to the mock keyring.
    assert k1 == k2


def test_key_is_cached_after_first_call(monkeypatch):
    """M5: _load_or_create_key populates the module-level cache."""
    store: dict[str, str] = {}
    monkeypatch.setattr("persistence.crypto._keyring_get", lambda u: store.get(u))
    monkeypatch.setattr("persistence.crypto._keyring_set", lambda u, v: store.__setitem__(u, v))

    assert _crypto_mod._KEY_CACHE is None
    _load_or_create_key()
    assert _crypto_mod._KEY_CACHE is not None


def test_key_cached_second_call_does_not_invoke_load_uncached(monkeypatch):
    """M5: second call to _load_or_create_key uses cache, not keyring."""
    call_count = {"n": 0}
    real_load = _crypto_mod._load_key_uncached

    def counting_load():
        call_count["n"] += 1
        return real_load()

    store: dict[str, str] = {}
    monkeypatch.setattr("persistence.crypto._keyring_get", lambda u: store.get(u))
    monkeypatch.setattr("persistence.crypto._keyring_set", lambda u, v: store.__setitem__(u, v))
    monkeypatch.setattr("persistence.crypto._load_key_uncached", counting_load)

    _load_or_create_key()
    _load_or_create_key()
    _load_or_create_key()

    # _load_key_uncached should only be called once (on cache miss).
    assert call_count["n"] == 1


def test_encrypt_decrypt_uses_cached_key(monkeypatch):
    """M5: encrypt + decrypt both use cached key, not reloading per call."""
    store: dict[str, str] = {}
    monkeypatch.setattr("persistence.crypto._keyring_get", lambda u: store.get(u))
    monkeypatch.setattr("persistence.crypto._keyring_set", lambda u, v: store.__setitem__(u, v))

    ct = encrypt("test value")
    pt = decrypt(ct)
    assert pt == "test value"
    # Both operations used the same cached key — verified by successful round-trip.
