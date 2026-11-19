"""scripts/versions.py на временном git-репозитории (настоящий репозиторий не трогается)."""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import versions  # noqa: E402


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "develop")
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "Тест")
    for i, msg in enumerate([
        "Первая версия",
        "Боты: шаблоны\n\nДобавлено:\n- шаблоны ботов\n- статистика\n\nИсправлено:\n- задержка в тест-чате\n\nCo-Authored-By: X <x@y>",
        "Мелочи\n\n- кнопка «Очистить»\n- исправлена ошибка при сохранении,\n  которая падала на пустом графе",
    ]):
        (r / "app.txt").write_text(f"версия {i}\n")
        git(r, "add", "-A")
        git(r, "commit", "-q", "-m", msg)
    return r


def run(repo, *args, capsys=None):
    code = versions.main([*args, "--repo", str(repo)])
    return code


def test_parse_body():
    added, fixed = versions.parse_body("x", "Добавлено:\n- a\n- b\n\nИсправлено:\n- c\n\nCo-Authored-By: z")
    assert added == ["a", "b"] and fixed == ["c"]
    added, fixed = versions.parse_body("x", "- новая кнопка\n- исправлен баг с\n  сохранением")
    assert added == ["новая кнопка"] and fixed == ["исправлен баг с сохранением"]
    assert versions.parse_body("Исправлена ошибка входа", "") == ([], ["Исправлена ошибка входа"])


def test_list_show(repo, capsys):
    g = versions.Git(repo)
    items = versions.commits(g)
    assert [i["subject"] for i in items] == ["Мелочи", "Боты: шаблоны", "Первая версия"]
    assert items[1]["added"] == ["шаблоны ботов", "статистика"] and items[1]["fixed"] == ["задержка в тест-чате"]
    assert run(repo, "list") == 0
    out = capsys.readouterr().out
    assert "Боты: шаблоны" in out and "+2" in out and "✓1" in out
    assert run(repo, "show", "2") == 0
    out = capsys.readouterr().out
    assert "Добавлено" in out and "+ статистика" in out and "✓ задержка в тест-чате" in out and "app.txt" in out


def test_try_back_rollback_undo(repo, capsys):
    head = git(repo, "rev-parse", "HEAD").strip()
    first = git(repo, "rev-list", "--max-parents=0", "HEAD").strip()
    # просмотр версии: своя ветка не меняется
    assert run(repo, "try", "3") == 0
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip().startswith("version/")
    assert (repo / "app.txt").read_text() == "версия 0\n"
    assert git(repo, "rev-parse", "develop").strip() == head
    assert run(repo, "rollback", "1", "--yes") == 1          # из просмотра откатывать нельзя
    assert run(repo, "back") == 0
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "develop"
    assert not git(repo, "branch", "--list", "version/*").strip()

    # незакоммиченные изменения: без --stash отказ, файл цел
    (repo / "app.txt").write_text("моя правка\n")
    assert run(repo, "rollback", "3", "--yes") == 1
    assert "незакоммиченные" in capsys.readouterr().err
    assert (repo / "app.txt").read_text() == "моя правка\n"
    # с --stash: правка спрятана, ветка откачена, резервная копия есть
    assert run(repo, "rollback", "3", "--yes", "--stash") == 0
    assert git(repo, "rev-parse", "HEAD").strip() == first and (repo / "app.txt").read_text() == "версия 0\n"
    backups = git(repo, "branch", "--list", "backup/*").split()
    assert len(backups) == 1 and git(repo, "rev-parse", backups[0]).strip() == head
    assert "моя правка" in git(repo, "stash", "show", "-p")
    # undo — ветка там же, где была до отката
    assert run(repo, "undo") == 0
    assert git(repo, "rev-parse", "HEAD").strip() == head
    assert run(repo, "undo") == 1                            # отменять больше нечего
    assert run(repo, "rollback", "1", "--yes") == 1          # это уже текущая версия
    assert run(repo, "show", "99abcdef") == 1                # нет такой версии


def test_switching_versions_keeps_platform_data(tmp_path):
    """Старая версия хранила backend/data/… в git: просмотр, откат и возврат не трогают живые данные."""
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "develop")
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "Тест")
    data = r / "backend" / "data" / "projects"
    data.mkdir(parents=True)
    (data / "example.json").write_text("из первой версии")
    (r / "app.txt").write_text("1")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "Первая версия")
    git(r, "rm", "-q", "--cached", "-r", "backend/data")
    (r / ".gitignore").write_text("backend/data/\n")
    (r / "app.txt").write_text("2")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "Данные — не в git")
    (data / "example.json").write_text("живые данные")
    (data / "other.json").write_text("ещё проект")

    def live():
        return {p.name: p.read_text() for p in sorted(data.glob("*.json"))}

    want = {"example.json": "живые данные", "other.json": "ещё проект"}
    assert run(r, "try", "2") == 0 and live() == want
    assert (r / "app.txt").read_text() == "1"
    assert run(r, "back") == 0 and live() == want
    assert git(r, "rev-parse", "--abbrev-ref", "HEAD").strip() == "develop"
    assert run(r, "rollback", "2", "--yes") == 0 and live() == want
    assert run(r, "undo") == 0 and live() == want
    assert (r / "app.txt").read_text() == "2"
