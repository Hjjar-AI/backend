#!/usr/bin/env bash
# Quiz launcher: gunicorn behind a user-owned nginx with TLS.
# Ctrl-C (or closing the terminal) stops both.
#
# Location assumed: <project>/backend/scripts/quiz-start.sh
# PROJECT_DIR is derived from the script's own path, so the project can
# be moved anywhere and the launcher keeps working.

set -euo pipefail

# ── Paths ─────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
BACKEND_DIR="$PROJECT_DIR/backend"
FRONTEND_DIR="$PROJECT_DIR/frontend"

# Runtime under $HOME so nginx can write without root, and so the
# gunicorn socket path has no spaces (nginx upstream blocks do not
# tolerate spaces in socket paths). Cert lives with the project.
CONFIG_DIR="$HOME/.config/quiz"
RUNTIME_DIR="$CONFIG_DIR/run"
NGINX_CONF="$CONFIG_DIR/nginx.conf"
CERT_PEM="$BACKEND_DIR/cert/quiz/quiz.pem"
CERT_KEY="$BACKEND_DIR/cert/quiz/quiz.key"

HTTPS_PORT=5443
GUNICORN_WORKERS=3
GUNICORN_SOCK="$RUNTIME_DIR/quiz.sock"

# ── Helpers ───────────────────────────────────────────────────────
fail() { echo "ERROR: $*" >&2; exit 1; }

# ── Virtualenv discovery ──────────────────────────────────────────
# Priority: $QUIZ_VENV override > project-local ./venv > ~/Environments/quizenv
if [ -n "${QUIZ_VENV:-}" ] && [ -x "$QUIZ_VENV/bin/python" ]; then
    VENV="$QUIZ_VENV"
elif [ -x "$PROJECT_DIR/venv/bin/python" ]; then
    VENV="$PROJECT_DIR/venv"
elif [ -x "$HOME/Environments/quizenv/bin/python" ]; then
    VENV="$HOME/Environments/quizenv"
else
    fail "no venv found (checked \$QUIZ_VENV, $PROJECT_DIR/venv, ~/Environments/quizenv)"
fi

# ── Sanity checks ─────────────────────────────────────────────────
[ -d "$BACKEND_DIR" ]                    || fail "$BACKEND_DIR not found (drive mounted?)"
[ -d "$FRONTEND_DIR" ]                   || fail "$FRONTEND_DIR not found"
[ -x "$VENV/bin/gunicorn" ]              || fail "gunicorn missing in $VENV"
[ -f "$CERT_PEM" ]                       || fail "cert missing: $CERT_PEM"
[ -f "$CERT_KEY" ]                       || fail "key missing:  $CERT_KEY"
[ -f "$FRONTEND_DIR/dist/index.html" ]   || fail "frontend not built (cd frontend && pnpm build)"
[ -d "$BACKEND_DIR/staticfiles/admin" ]  || fail "staticfiles not collected (python manage.py collectstatic)"

mkdir -p "$RUNTIME_DIR"
rm -f "$GUNICORN_SOCK"

# ── Generate nginx config ─────────────────────────────────────────
# Rewritten on every launch so the config always matches the current
# paths and ports — no drift, no stale file.
cat > "$NGINX_CONF" <<EOF
worker_processes 1;
daemon off;
pid "$RUNTIME_DIR/nginx.pid";
error_log "$RUNTIME_DIR/nginx-error.log" warn;

events {
    worker_connections 512;
}

http {
    include /etc/nginx/mime.types;
    default_type application/octet-stream;

    access_log "$RUNTIME_DIR/nginx-access.log";

    # All scratch dirs under \$HOME so nginx runs as a normal user.
    client_body_temp_path "$RUNTIME_DIR/client_body";
    proxy_temp_path       "$RUNTIME_DIR/proxy";
    fastcgi_temp_path     "$RUNTIME_DIR/fastcgi";
    uwsgi_temp_path       "$RUNTIME_DIR/uwsgi";
    scgi_temp_path        "$RUNTIME_DIR/scgi";

    sendfile on;
    tcp_nopush on;
    keepalive_timeout 65;
    client_max_body_size 50M;

    # TLS
    ssl_certificate     "$CERT_PEM";
    ssl_certificate_key "$CERT_KEY";
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_cache shared:SSL:10m;
    ssl_session_timeout 1h;
    ssl_prefer_server_ciphers off;

    # gzip
    gzip on;
    gzip_types text/plain text/css application/json application/javascript
               text/xml application/xml image/svg+xml;
    gzip_min_length 1024;

    upstream quiz_backend {
        server unix:$GUNICORN_SOCK;
    }

    server {
        listen $HTTPS_PORT ssl;
        server_name _;

        # Vite-built bundles — the biggest files, served directly
        location /assets/ {
            alias "$FRONTEND_DIR/dist/assets/";
            access_log off;
            expires 30d;
            add_header Cache-Control "public, immutable";
        }

        # Django admin + DRF assets (from collectstatic)
        location /static/ {
            alias "$BACKEND_DIR/staticfiles/";
            access_log off;
            expires 30d;
            add_header Cache-Control "public, immutable";
        }

        # User uploads (question images)
        location /media/ {
            alias "$BACKEND_DIR/media/";
            access_log off;
            expires 7d;
        }

        # Everything else → gunicorn → Django (API + SPA routing)
        location / {
            proxy_pass http://quiz_backend;
            proxy_http_version 1.1;
            proxy_set_header Host              \$host;
            proxy_set_header X-Real-IP         \$remote_addr;
            proxy_set_header X-Forwarded-For   \$proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto \$scheme;
            proxy_redirect off;
            proxy_read_timeout 120s;
        }
    }
}
EOF

# Validate the config before starting anything.
if ! nginx -t -c "$NGINX_CONF" -p "$RUNTIME_DIR" >/dev/null 2>&1; then
    echo "nginx config is invalid:"
    nginx -t -c "$NGINX_CONF" -p "$RUNTIME_DIR" || true
    exit 1
fi

# ── Cleanup on exit ───────────────────────────────────────────────
NGINX_PID=""
GUNICORN_PID=""
cleanup() {
    echo
    echo "Shutting down…"
    [ -n "$NGINX_PID" ]    && kill "$NGINX_PID"    2>/dev/null || true
    [ -n "$GUNICORN_PID" ] && kill "$GUNICORN_PID" 2>/dev/null || true
    wait 2>/dev/null || true
    rm -f "$GUNICORN_SOCK"
    echo "Stopped."
}
trap cleanup EXIT INT TERM

# ── Start gunicorn ────────────────────────────────────────────────
cd "$BACKEND_DIR"

echo "→ gunicorn ($GUNICORN_WORKERS workers) on unix:$GUNICORN_SOCK"
"$VENV/bin/gunicorn" \
    --workers "$GUNICORN_WORKERS" \
    --bind "unix:$GUNICORN_SOCK" \
    --umask 0077 \
    --access-logfile - \
    --error-logfile - \
    --capture-output \
    config.wsgi:application &
GUNICORN_PID=$!

# Wait for the socket to answer, or fail loudly.
for _ in $(seq 1 40); do
    if [ -S "$GUNICORN_SOCK" ] && \
       curl -sf --unix-socket "$GUNICORN_SOCK" http://x/api/v1/health/ >/dev/null 2>&1; then
        break
    fi
    sleep 0.25
done
kill -0 "$GUNICORN_PID" 2>/dev/null || fail "gunicorn exited during startup"
[ -S "$GUNICORN_SOCK" ]            || fail "gunicorn socket not created"

# ── Start nginx ───────────────────────────────────────────────────
echo "→ nginx TLS on 0.0.0.0:$HTTPS_PORT"
nginx -c "$NGINX_CONF" -p "$RUNTIME_DIR" &
NGINX_PID=$!

sleep 0.5
kill -0 "$NGINX_PID" 2>/dev/null || fail "nginx exited — see $RUNTIME_DIR/nginx-error.log"

# ── Banner ────────────────────────────────────────────────────────
echo
echo "═══════════════════════════════════════════════════════════════════"
echo "  Quiz is running."
echo "  Local:   https://127.0.0.1:$HTTPS_PORT"
echo "  Public:  https://<your-public-ip>:$HTTPS_PORT"
echo "  Logs:    $RUNTIME_DIR/nginx-error.log"
echo "           $RUNTIME_DIR/nginx-access.log"
echo "  Ctrl-C to stop."
echo "═══════════════════════════════════════════════════════════════════"
echo

# Block until either process dies; the trap handles the other.
wait -n "$GUNICORN_PID" "$NGINX_PID" || true