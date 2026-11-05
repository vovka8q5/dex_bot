import io
from pathlib import Path

from dexbot.config import load_settings, package_config_path
from dexbot.logging_setup import prefer_utf8


def test_journal_and_kill_file_can_be_absolute(tmp_path, monkeypatch):
    journal = tmp_path / "journal.sqlite"
    kill = tmp_path / "KILL"
    monkeypatch.setenv("DEXBOT_JOURNAL", str(journal))
    monkeypatch.setenv("DEXBOT_KILL_FILE", str(kill))
    settings = load_settings(package_config_path())
    assert Path(settings.journal.path) == journal
    assert Path(settings.risk.kill_switch_file) == kill
    assert settings.risk.kill_switch_file == str(kill)


def test_dotenv_utf8_bom_from_notepad(tmp_path, monkeypatch):
    monkeypatch.delenv("DEXBOT_MODE", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_bytes("DEXBOT_MODE=demo\n".encode("utf-8-sig"))
    settings = load_settings(package_config_path())
    assert settings.mode == "demo"
    assert settings.exchange.mode == "demo"


def test_prefer_utf8_does_not_require_posix():
    prefer_utf8(io.StringIO())
    stream = io.TextIOWrapper(io.BytesIO(), encoding="ascii", errors="strict")
    prefer_utf8(stream)
    stream.write("ok")
    assert stream.encoding == "utf-8"
