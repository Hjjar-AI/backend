# backend/apps/database/services/__init__.py
"""
Package surface for the database services.

Split from a single ~600-line module into four sibling modules:

  • backend.py            — backend detection (`_is_sqlite`,
                            `_is_mariadb`) and the two MySQL config
                            helpers (`_mysql_config`,
                            `_write_defaults_file`).
  • backup_service.py     — the `BackupService` class. Every public
                            method (get_info, create_backup,
                            list_backups, restore_backup,
                            clear_database) keeps its original
                            signature and MariaDB branch.
  • _sqlite_reference.py  — the previous SQLite implementation,
                            preserved as runnable reference code.
                            NOT imported by anything.

`BackupService` is re-exported here unchanged, so
`from .services import BackupService` in apps/database/views.py
keeps working without modification.

WHY THE SQLITE CODE WAS EXTRACTED, NOT DELETED
----------------------------------------------
The previous version of this module carried every SQLite branch as
a commented-out block inside the corresponding method. Those
comment blocks accounted for roughly 40 % of the file's length
and turned every diff of an active method into a review exercise
against dead code.

The extracted module (`_sqlite_reference.py`) contains the same
implementation as a runnable Python module. It is deliberately
not imported by `backup_service.py` — the MariaDB branch remains
the only live path, exactly as before. To re-enable SQLite, see
the re-enable recipe in `_sqlite_reference.py`'s module docstring.
"""

from .backup_service import BackupService


__all__ = ['BackupService']