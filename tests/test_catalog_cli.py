import sqlite3

from family_archive import cli
from family_archive.catalog import Catalog


def make_catalog(path):
    catalog = Catalog(path)
    record = catalog.create(
        {
            "inspection": {"volume": "FAMILY_1998", "classification": "DATA CD"},
            "source_type": "data_cd",
        }
    )
    record.update(state="INGESTING", file_count=2, logical_file_size=1234)
    catalog.save(record)
    record.update(state="VERIFYING")
    catalog.save(record)
    record.update(state="COMPLETE")
    catalog.save(record)
    catalog.save_files(
        record["source_id"],
        [
            {
                "kind": "file",
                "path": "Photos/Christmas.jpg",
                "filename": "Christmas.jpg",
                "size": 100,
                "mime_type": "image/jpeg",
            },
            {
                "kind": "file",
                "path": "Photos/scan.tif",
                "filename": "scan.tif",
                "size": 1134,
                "mime_type": "application/octet-stream",
            },
        ],
    )
    catalog.close()


def test_catalog_cli_status_list_search_are_read_only(tmp_path, capsys):
    database = tmp_path / "archive.db"
    make_catalog(database)
    before = database.read_bytes()

    assert cli.main(["status", "--database", str(database)]) == 0
    status = capsys.readouterr().out
    assert "Sources:             1" in status
    assert "Files discovered:    2" in status
    assert "1,234 bytes" in status

    assert cli.main(["list", "--database", str(database)]) == 0
    listing = capsys.readouterr().out
    assert "SRC-0001" in listing and "FAMILY_1998" in listing and "COMPLETE" in listing

    assert cli.main(["search", "christmas", "--database", str(database)]) == 0
    assert "SRC-0001  Photos/Christmas.jpg" in capsys.readouterr().out
    assert cli.main(["search", "family_1998", "--database", str(database)]) == 0
    assert "SRC-0001" in capsys.readouterr().out
    assert cli.main(["search", "--media", "photo", "--database", str(database)]) == 0
    media_results = capsys.readouterr().out
    assert "Photos/Christmas.jpg" in media_results
    assert "Photos/scan.tif" not in media_results
    assert database.read_bytes() == before


def test_missing_catalog_is_not_created(tmp_path, capsys):
    database = tmp_path / "missing.db"
    assert cli.main(["status", "--database", str(database)]) == 0
    assert "No catalog found" in capsys.readouterr().out
    assert not database.exists()


def test_empty_catalog_queries(tmp_path, capsys):
    database = tmp_path / "empty.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE sources (
            number INTEGER PRIMARY KEY, source_id TEXT, state TEXT, record TEXT
        );
        CREATE TABLE files (source_id TEXT, path TEXT, record TEXT);
        PRAGMA user_version = 1;
        """
    )
    connection.close()
    assert cli.main(["list", "--database", str(database)]) == 0
    assert "No sources" in capsys.readouterr().out
    assert cli.main(["search", "anything", "--database", str(database)]) == 0
    assert "No matches" in capsys.readouterr().out
