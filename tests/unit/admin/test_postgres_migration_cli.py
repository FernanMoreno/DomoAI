from pathlib import Path

from domoai.admin.cli import _parser


def test_admin_parser_exposes_explicit_postgres_migration() -> None:
    args = _parser().parse_args(
        [
            "migrate-postgres",
            "--source-database",
            str(Path("source.sqlite3")),
            "--dsn",
            "postgresql://control-plane",
        ]
    )

    assert args.command == "migrate-postgres"
    assert args.source_database == Path("source.sqlite3")
