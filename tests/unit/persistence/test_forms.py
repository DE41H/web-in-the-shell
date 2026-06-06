"""Tests for FormFieldStore — form field persistence and sensitivity filtering."""

import asyncio

import pytest

from persistence.db import init_db
from persistence.forms import FormFieldStore, _is_sensitive


# ── _is_sensitive ────────────────────────────────────────────────────────────

def test_is_sensitive_password_type():
    assert _is_sensitive("password", "email") is True


def test_is_sensitive_password_name_exact():
    assert _is_sensitive("text", "password") is True


def test_is_sensitive_passwd_name():
    assert _is_sensitive("text", "passwd") is True


def test_is_sensitive_cvv_name():
    assert _is_sensitive("text", "cvv") is True


def test_is_sensitive_otp_name():
    assert _is_sensitive("text", "otp") is True


def test_is_sensitive_card_number_name():
    # field_name = "card_number"
    assert _is_sensitive("text", "card_number") is True


def test_not_sensitive_email_type():
    assert _is_sensitive("email", "email") is False


def test_not_sensitive_text_type():
    assert _is_sensitive("text", "username") is False


def test_not_sensitive_text_name_with_no_keywords():
    assert _is_sensitive("text", "first_name") is False


# ── FormFieldStore.save / get ────────────────────────────────────────────────

async def test_save_and_get_returns_stored_value(tmp_path):
    # save a text field, then get it back
    db_path = tmp_path / "test.db"
    await init_db(db_path)
    async with FormFieldStore(db_path) as store:
        await store.save("example.com", "email", "email", "user@example.com")
        result = await store.get("example.com", "email")
    assert result == "user@example.com"


async def test_save_sensitive_type_is_silently_skipped(tmp_path):
    # save with field_type="password" → get returns None
    db_path = tmp_path / "test.db"
    await init_db(db_path)
    async with FormFieldStore(db_path) as store:
        await store.save("example.com", "secret", "password", "s3cr3t")
        result = await store.get("example.com", "secret")
    assert result is None


async def test_save_sensitive_name_is_silently_skipped(tmp_path):
    # save field with name="user_password" → get returns None
    db_path = tmp_path / "test.db"
    await init_db(db_path)
    async with FormFieldStore(db_path) as store:
        await store.save("example.com", "user_password", "text", "s3cr3t")
        result = await store.get("example.com", "user_password")
    assert result is None


async def test_get_returns_none_for_missing_field(tmp_path):
    db_path = tmp_path / "test.db"
    await init_db(db_path)
    async with FormFieldStore(db_path) as store:
        result = await store.get("example.com", "nonexistent")
    assert result is None


async def test_save_upserts_on_conflict(tmp_path):
    # save twice with same domain+name → second value wins
    db_path = tmp_path / "test.db"
    await init_db(db_path)
    async with FormFieldStore(db_path) as store:
        await store.save("example.com", "username", "text", "alice")
        await store.save("example.com", "username", "text", "bob")
        result = await store.get("example.com", "username")
    assert result == "bob"


# ── FormFieldStore.get_all_for_domain ────────────────────────────────────────

async def test_get_all_for_domain_returns_only_that_domain(tmp_path):
    # save fields for two different domains, get_all returns only the target domain
    db_path = tmp_path / "test.db"
    await init_db(db_path)
    async with FormFieldStore(db_path) as store:
        await store.save("alpha.com", "email", "email", "a@alpha.com")
        await store.save("alpha.com", "username", "text", "alice")
        await store.save("beta.com", "email", "email", "b@beta.com")
        result = await store.get_all_for_domain("alpha.com")
    assert set(result.keys()) == {"email", "username"}
    assert result["email"] == "a@alpha.com"
    assert result["username"] == "alice"


async def test_get_all_for_domain_empty_when_no_fields(tmp_path):
    db_path = tmp_path / "test.db"
    await init_db(db_path)
    async with FormFieldStore(db_path) as store:
        result = await store.get_all_for_domain("empty.com")
    assert result == {}


async def test_get_all_for_domain_excludes_sensitive_fields(tmp_path):
    # sensitive field is not saved, so not present in get_all results
    db_path = tmp_path / "test.db"
    await init_db(db_path)
    async with FormFieldStore(db_path) as store:
        await store.save("example.com", "username", "text", "alice")
        await store.save("example.com", "passwd", "text", "s3cr3t")  # sensitive name
        result = await store.get_all_for_domain("example.com")
    assert "username" in result
    assert "passwd" not in result


# ── FormFieldStore.delete ────────────────────────────────────────────────────

async def test_delete_existing_field_returns_1(tmp_path):
    db_path = tmp_path / "test.db"
    await init_db(db_path)
    async with FormFieldStore(db_path) as store:
        await store.save("example.com", "email", "email", "user@example.com")
        count = await store.delete("example.com", "email")
    assert count == 1


async def test_delete_missing_field_returns_0(tmp_path):
    db_path = tmp_path / "test.db"
    await init_db(db_path)
    async with FormFieldStore(db_path) as store:
        count = await store.delete("example.com", "nonexistent")
    assert count == 0


async def test_delete_does_not_affect_other_fields(tmp_path):
    db_path = tmp_path / "test.db"
    await init_db(db_path)
    async with FormFieldStore(db_path) as store:
        await store.save("example.com", "email", "email", "user@example.com")
        await store.save("example.com", "username", "text", "alice")
        await store.delete("example.com", "email")
        remaining = await store.get("example.com", "username")
    assert remaining == "alice"


# ── FormFieldStore.clear_domain ──────────────────────────────────────────────

async def test_clear_domain_removes_all_fields_for_domain(tmp_path):
    db_path = tmp_path / "test.db"
    await init_db(db_path)
    async with FormFieldStore(db_path) as store:
        await store.save("example.com", "email", "email", "user@example.com")
        await store.save("example.com", "username", "text", "alice")
        await store.clear_domain("example.com")
        result = await store.get_all_for_domain("example.com")
    assert result == {}


async def test_clear_domain_does_not_affect_other_domains(tmp_path):
    db_path = tmp_path / "test.db"
    await init_db(db_path)
    async with FormFieldStore(db_path) as store:
        await store.save("alpha.com", "email", "email", "a@alpha.com")
        await store.save("beta.com", "email", "email", "b@beta.com")
        await store.clear_domain("alpha.com")
        alpha_result = await store.get_all_for_domain("alpha.com")
        beta_result = await store.get_all_for_domain("beta.com")
    assert alpha_result == {}
    assert "email" in beta_result


async def test_clear_domain_returns_count_of_deleted_rows(tmp_path):
    db_path = tmp_path / "test.db"
    await init_db(db_path)
    async with FormFieldStore(db_path) as store:
        await store.save("example.com", "email", "email", "user@example.com")
        await store.save("example.com", "username", "text", "alice")
        await store.save("example.com", "first_name", "text", "Alice")
        count = await store.clear_domain("example.com")
    assert count == 3


# ── Context manager guard ────────────────────────────────────────────────────

def test_require_conn_raises_outside_context(tmp_path):
    store = FormFieldStore(tmp_path / "test.db")
    with pytest.raises(RuntimeError, match="async with"):
        asyncio.run(store.get("example.com", "email"))
