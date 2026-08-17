"""Tests for Kalshi private-key path resolution."""

from pathlib import Path

from src.clients.private_key import find_private_key_file, iter_private_key_candidates


def test_candidates_include_sibling_and_cwd_names(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    configured = tmp_path / "keys" / "kalshi_private_key.pem"
    names = [p.name for p in iter_private_key_candidates(str(configured))]
    assert "kalshi_private_key.pem" in names
    assert "kalshi_private_key" in names
    sibling = configured.with_suffix("")
    assert any(p == sibling or p.resolve() == sibling for p in iter_private_key_candidates(str(configured)))


def test_finds_pem_next_to_configured_no_extension_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    key_dir = tmp_path / "keys"
    key_dir.mkdir()
    pem = key_dir / "kalshi_private_key.pem"
    pem.write_text("not-a-real-key")
    found = find_private_key_file(str(key_dir / "kalshi_private_key"))
    assert found is not None
    assert found.resolve() == pem.resolve()


def test_finds_no_extension_next_to_configured_pem_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    key_dir = tmp_path / "keys"
    key_dir.mkdir()
    bare = key_dir / "kalshi_private_key"
    bare.write_text("not-a-real-key")
    found = find_private_key_file(str(key_dir / "kalshi_private_key.pem"))
    assert found is not None
    assert found.resolve() == bare.resolve()


def test_skips_directory_stub_and_uses_real_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    stub = tmp_path / "kalshi_private_key.pem"
    stub.mkdir()
    real = tmp_path / "kalshi_private_key"
    real.write_text("not-a-real-key")
    found = find_private_key_file(str(stub))
    assert found is not None
    assert found.resolve() == real.resolve()
    assert found.is_file()


def test_cwd_fallback_when_configured_path_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cwd_key = tmp_path / "kalshi_private_key.pem"
    cwd_key.write_text("not-a-real-key")
    found = find_private_key_file(str(tmp_path / "missing" / "kalshi_private_key.pem"))
    assert found is not None
    assert found.resolve() == cwd_key.resolve()
