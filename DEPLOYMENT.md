````
# Quiz — Deployment and First-Run Reference

Companion to `requirements.txt`. That file installs Python packages;
this file installs the system libraries they link against and walks
through a first run.

## Contents

- [1. System packages](#1-system-packages)
- [2. First-run bootstrap](#2-first-run-bootstrap)
- [3. Troubleshooting](#3-troubleshooting)
- [4. OS-specific notes](#4-os-specific-notes)

---

## 1. System packages

Install on the host **before** creating the Python environment. None
of these are pip-installable.

### 1.1 Reverse proxy / TLS stack

Only needed for a networked deployment that terminates HTTPS.

```bash
sudo apt install nginx openssl curl
```

- `nginx` — reverse proxy terminating TLS on port 5443.
- `openssl` — generates the self-signed certificate for the 5443 listener.
- `curl` — smoke-tests endpoints during setup and after deploy.

### 1.2 mysqlclient compile-time headers

Required to build `mysqlclient` (pip package from `requirements.txt`).

```bash
# Debian / Ubuntu
sudo apt install default-libmysqlclient-dev build-essential pkg-config

# Fedora / RHEL
sudo dnf install mariadb-connector-c-devel gcc make pkgconf-pkg-config
```

**Compiler-free alternative.** Replace `mysqlclient` in
`requirements.txt` with `PyMySQL`, and add to `backend/config/__init__.py`:

```python
import pymysql
pymysql.install_as_MySQLdb()
```

Slower per query; no build toolchain needed.

### 1.3 WeasyPrint runtime libraries

`weasyprint` is a Python wheel that `dlopen()`s several C libraries at
import. Without them, `import weasyprint` raises `OSError` naming the
missing `.so`, and PDF export returns a 500. The app itself starts fine
— only `fmt=pdf` is affected.

```bash
# Debian / Ubuntu (Bookworm and newer)
sudo apt install libpango-1.0-0 libpangoft2-1.0-0 \
                 libharfbuzz0b libcairo2 \
                 libgdk-pixbuf-2.0-0 libffi8

# Fedora / RHEL
sudo dnf install pango pangoft2 harfbuzz cairo gdk-pixbuf2 libffi

# Alpine (slim container images)
apk add pango pango-dev harfbuzz cairo gdk-pixbuf libffi
```

### 1.4 libmagic (required by `python-magic`)

```bash
# Debian / Ubuntu
sudo apt install libmagic1

# Fedora / RHEL
sudo dnf install file-libs

# Alpine
apk add file
```

Missing `libmagic` is a **hard failure** at upload time — the MIME
check fails closed rather than accepting unverified files.

### 1.5 Arabic font for PDF export

PDF export embeds a Noto Sans Arabic TTF so the output carries its own
glyphs. The font ships in the repo at:

```
frontend/public/fonts/NotoSansArabic-VariableFont_wdth,wght.ttf
```

`pdf_export.py` searches (in order):

1. `settings.FRONTEND_DIR / 'public' / 'fonts'`
2. `settings.BASE_DIR / 'static' / 'fonts'`

Development satisfies (1) automatically. A production host that ships
only the built `frontend/dist/` tree should copy the TTF during the
build:

```bash
mkdir -p backend/static/fonts
cp frontend/public/fonts/NotoSansArabic-VariableFont_wdth,wght.ttf \
   backend/static/fonts/
```

Missing font is not fatal — Pango falls back to a system font, the PDF
is still generated, and a warning is logged naming the two paths
checked.

### 1.6 MariaDB timezone tables

Django's `TruncMonth` / `TruncWeek` / `TruncDate` on a tz-aware
`DateTimeField` compile to `CONVERT_TZ(..., 'UTC', <TIME_ZONE>)` on
MariaDB. On a stock Debian/Ubuntu install the `mysql.time_zone*`
tables are empty, so `CONVERT_TZ` returns `NULL` and Django raises:

```
ValueError: Database returned an invalid datetime value.
Are time zone definitions for your database installed?
```

Load them once per server (idempotent):

```bash
sudo mysql_tzinfo_to_sql /usr/share/zoneinfo | sudo mysql mysql
```

This codebase works around the empty-tables case by doing the
bucketing in Python (see `apps/analytics/services/admin_advanced.py`),
but loading the tables restores the SQL path and helps any TIMESTAMP
arithmetic MariaDB itself performs.

---

## 2. First-run bootstrap

Run from `backend/` with the virtualenv activated and `backend/.env`
populated. See `backend/.env.example` for required keys.

### 2.1 Environment

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2.2 Schema and seed data

Pick **one** path. Both produce equivalent databases.

#### Path A — incremental (recommended for existing databases)

Runs each seeder individually so a failure is easy to localise.

```bash
python manage.py makemigrations
python manage.py migrate
python manage.py seed_data
python manage.py seed_sample_questions
python manage.py seed_capabilities     # ← required; not run by seed_data
python manage.py seed_tips             # ← optional; onboarding tips
```

`seed_data` creates the `admin` account, the category taxonomy, and
default settings. It prints the initial admin password to **stderr**
once. The account is created with `must_change_password=True`;
`MustChangePasswordMiddleware` will refuse every API and `/admin/`
request except the change-password flow until the password is changed
on first login.

`seed_sample_questions` requires an admin to exist — run `seed_data`
first. It is idempotent and safe to re-run.

`seed_capabilities` is required on a fresh database. Without it, the
`RoleCapabilities` table is empty and the capability system falls back
to the code defaults in `apps/users/capabilities.py`. That fallback is
correct on a new install, but the panel's read-back will be empty until
the row exists.

#### Path B — clean rebuild (development only; DESTROYS the database)

Drops every table, deletes every migration file under the custom apps,
regenerates `0001_initial.py`, migrates, and runs the `seed`
orchestrator.

```bash
python manage.py bootstrap --clean --yes --with-sample-questions
```

Equivalent long form, if you prefer explicit flags:

```bash
python manage.py bootstrap \
    --clean \
    --with-sample-questions \
    --reset-capabilities
```

Flags accepted by `bootstrap`:

| Flag | Effect |
|---|---|
| `--clean` | **Destructive.** Drops every table and deletes every migration file for the custom apps. Requires interactive confirmation; use `--yes` to skip in scripts. |
| `--yes`, `-y` | Skip the interactive `--clean` confirmation. |
| `--with-sample-questions` | Also run `seed_sample_questions` after the default seed set. |
| `--reset-capabilities` | Forward `--reset` to `seed_capabilities` — overwrite every `RoleCapabilities` row with code defaults, discarding panel edits. |
| `--no-db-setup` | Skip the MariaDB provisioning step. Use when the database and user already exist, or when running against SQLite. |
| `--db-admin-user` / `--db-admin-password` | Administrative MariaDB credentials. **Lower precedence** than `DB_ADMIN_USER` / `DB_ADMIN_PASSWORD` in `.env`. CLI values are visible in `ps` and shell history. |
| `--collectstatic` | Run `collectstatic` at the end of the pipeline. |
| `--no-checks` | Skip the explicit Django system-check step. Django reserves `--skip-checks` for its own use; this is the equivalent custom flag. |
| `--skip-seed` | Skip the seed wrapper entirely. Useful for CI runs that seed separately. |

### 2.3 Static files

```bash
python manage.py collectstatic --noinput
```

Copies Django admin's CSS/JS from site-packages into
`backend/staticfiles/`. Without it, `/admin/` loads unstyled. Re-run
after every Django or third-party-app upgrade.

### 2.4 Firewall and development server

The nginx TLS listener (see § 1.1) runs on **5443**; the Django
development server runs on **5004**. The two ports are independent —
open 5443 only if you intend to put nginx in front of the app.

```bash
# Only if nginx is in front:
sudo ufw allow 5443/tcp

# Development server:
python manage.py runserver 0.0.0.0:5004
```

For a self-contained SQLite instance alongside a MariaDB instance, use
the SQLite launcher instead:

```bash
python start_sqlite.py                 # interactive address prompt
python start_sqlite.py 5005            # localhost:5005
python start_sqlite.py --seed-pro-users
python start_sqlite.py --no-prompt
```

`start_sqlite.py` creates `backend/apps/__init__.py`, initialises each
custom app's `migrations/` package, runs `makemigrations` and
`migrate`, and seeds default data if no active superuser exists. It
uses `config/settings_sqlite.py`, which redirects every on-disk path
(`media/`, `uploads/`, `exports/`, `backups/`, the SQLite file itself)
into `backend/SQLite/` and gives the SQLite instance its own session
and CSRF cookie names, so it can run alongside a MariaDB instance from
the same checkout without collision.

### 2.5 Optional — pro / moderator accounts

```bash
python manage.py seed_pro_users
python manage.py seed_pro_users --reset-passwords
```

Creates one moderator account per doctor listed on the About page.
Idempotent by username; existing accounts are left alone unless
`--reset-passwords` is passed. Every account is created with
`must_change_password=True` and the password from
`SEED_PRO_USER_PASSWORD` in `.env` (default `1234test`).

### 2.6 Password reset at any time

```bash
python manage.py reset_admin_password
python manage.py reset_admin_password --qr
```

Prints the new password to **stderr**, sets
`must_change_password=True`, and (with `--qr`) renders an ASCII QR code
for the credential.

### 2.7 Health check

```bash
python manage.py doctor
```

Read-only. Reports configuration, database, cache, PDF stack, Arabic
font, writable directories, and frontend build presence. Exits `1` if
any check fails, so it is usable as a CI gate or a systemd pre-flight.
Run this **before** trusting the bootstrap output — it catches the
"system library missing" and "font not found" cases that nothing else
surfaces until the affected feature is exercised.

---

## 3. Troubleshooting

### `ModuleNotFoundError: No module named 'apps.core.management._app_registry'`

`bootstrap.py` (line ~90) and `start_sqlite.py` (line ~215) both import
`apps.core.management._app_registry`, but the file ships at
`apps/core/management/commands/_app_registry.py`. Move it up one
directory:

```bash
mv backend/apps/core/management/commands/_app_registry.py \
   backend/apps/core/management/_app_registry.py
find backend/apps/core/management -name __pycache__ -type d -exec rm -rf {} +
```

### `import weasyprint` raises `OSError: libpango-1.0.so.0: cannot open shared object file`

System libraries from § 1.3 are missing. The app still runs; only PDF
export returns 500.

### `ValueError: Database returned an invalid datetime value`

MariaDB timezone tables are empty. See § 1.6.

### `PermissionDenied` from `python-magic`

`libmagic` is missing. See § 1.4. Uploads will fail closed.

### Empty `/admin/` styles

`collectstatic` was not run. See § 2.3.

---

## 4. OS-specific notes

### Termux (Android)

Dependency names differ slightly. The Python package list from
`requirements.txt` is installed in the same order.

```bash
pkg install -y \
    python python-pip \
    build-essential pkg-config \
    libjpeg-turbo libpng zlib freetype harfbuzz cairo pango \
    libffi file tzdata openssl curl

python -m pip install --upgrade pip wheel setuptools
pip install -r backend/requirements.txt
```

Notes:

- `pkg-config` exists in Termux; on some mirrors it is `pkgconf`.
- `zlib` is not always pulled transitively by Pillow's build.
- `file` provides `libmagic` for `python-magic`.
- `pkg install python` already bundles pip; `python-pip` is listed
  defensively for older mirrors.
- `xlrd` still works on Termux; `openpyxl` is pure Python.
- WeasyPrint on Termux is fragile. If `import weasyprint` fails after
  installing the above, PDF export is unavailable but every other
  export format still works.

Then:

```bash
python backend/start_sqlite.py
```

### macOS

```bash
brew install mariadb pango cairo libffi
brew services start mariadb
```

`mysqlclient` builds against Homebrew's MariaDB headers out of the box.
`libmagic` is provided by `file` (usually already present).

### Windows

Not a supported deployment target. Development is possible under WSL2
with the Debian/Ubuntu instructions above.

---

## 5. Related files

| File | Purpose |
|---|---|
| `requirements.txt` | Python package pins |
| `backend/.env.example` | Environment variable reference |
| `backend/config/settings.py` | Django settings, with inline rationale |
| `backend/apps/core/management/commands/doctor.py` | Health check |
| `backend/apps/core/management/commands/bootstrap.py` | First-run orchestrator |
````