#!/usr/bin/env python3
"""
Ultimate Parse — single-file multi-language file content extractor with GUI.

Requires: customtkinter, tkinter (system).
Run:      python ultimate_parse.py

WCAG: color palette targets WCAG 2.1 AA
  • Body text ≥ 4.5:1 vs page bg (both light + dark themes)
  • Button/checkbox fill borders ≥ 3:1 vs page bg (1.4.11 non-text contrast)
  • Checkmark/icon vs fill ≥ 4.5:1
  Theme-aware tuples: (light_value, dark_value).
"""
import os, sys, re, json, zipfile, threading, time, subprocess, platform, copy, tempfile
from pathlib import Path
from collections import defaultdict
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional, Set, List, Dict, Tuple, Callable
from tkinter import ttk, filedialog, messagebox
import tkinter as tk
import customtkinter as ctk

# =====================================================================
#                              ENGINE
# =====================================================================

MAX_FILE_SIZE_BYTES = 1 * 1024 * 1024
ENCODINGS = ['utf-8', 'cp1252', 'latin-1']
DEFAULT_MAX_CHARS = 160000
PART_COUNT_WARN = 15
AUTO_REFRESH_DEBOUNCE_MS = 1500
AUTO_REFRESH_MAX_WAIT_MS = 5000
ESTIMATE_DEBOUNCE_MS = 400
TOKENS_TO_CHARS = 3.5

MARK_FILE = "=$@FILE: "
MARK_PART = "=$@ PART"
MARK_END  = "=$@ END"
REPORT_FORMAT_VERSION = 2
REPORT_FORMAT_PREFIX = "Report-Format:"
REPORT_MODE_PREFIX = "Report-Mode:"

DEFAULT_EXCLUDE_DIRS = {
    '__pycache__', '.venv', 'venv', 'env', 'virtualenv',
    '.git', '.svn', '.hg', '.tox', '.pytest_cache',
    '.mypy_cache', '.ruff_cache', '.coverage', 'htmlcov',
    'node_modules', 'dist', 'build',
}

BOOTSTRAP_PATTERNS = {'bootstrap-icons', 'bootstrap.min', 'bootstrap.bundle.min'}
CONFIG_EXTS = {'.toml', '.yaml', '.yml', '.ini', '.cfg'}
SHELL_EXTS = {'.bash', '.ps1', '.sh', '.zsh'}
NO_STRIP_EXTS = {'.bash', '.sh', '.zsh', '.ps1', '.bat', '.cmd'}
DOTFILE_NAMES = {
    '.gitignore', '.gitattributes', '.eslintrc', '.eslintignore',
    '.prettierrc', '.prettierignore', '.editorconfig', '.npmrc',
    '.babelrc', '.dockerignore',
}

MIN_BUNDLE_PATTERNS = {'.min.', '.bundle.', '-min.', '.minified.', '.bundled.'}

EXTRA_TYPE_OPTIONS: List[Tuple[str, Set[str]]] = [
    ("JSON",     {'.json'}),
    ("Markdown", {'.md', '.markdown'}),
    ("Text",     {'.txt'}),
    ("XML",      {'.xml', '.svg'}),
    ("CSV",      {'.csv'}),
    ("Config",   {'.toml', '.yaml', '.yml', '.ini', '.cfg'}),
    ("Log",      {'.log'}),
    ("ENV*",     {'.env.local', '.env.production', '.env.dev', '.env.test'}),
]

EXCLUDE_PRESETS: Dict[str, Dict[str, Set[str]]] = {
    "🧪 Tests": {"dirs": {"tests", "test", "__tests__", "spec", "specs", "__mocks__"},
                 "patterns": {".test.", ".spec.", "_test.", "test_"}},
    "🗄 Migrations": {"dirs": {"migrations", "alembic", "versions"}, "patterns": set()},
    "📦 Fixtures": {"dirs": {"fixtures", "seeds", "__snapshots__"}, "patterns": {".snap"}},
    "📚 Docs": {"dirs": {"docs", "documentation", "examples", "samples", "demos"},
                "patterns": {"README", "CHANGELOG", "LICENSE", "CONTRIBUTING",
                             "CODE_OF_CONDUCT", "AUTHORS"}},
    "🎨 Static": {"dirs": {"static", "public", "assets", "media", "uploads"}, "patterns": set()},
    "🗺 Maps": {"dirs": set(), "patterns": {".js.map", ".css.map", ".map"}},
    "🔒 Locks": {"dirs": set(), "patterns": {"package-lock.json", "yarn.lock", "poetry.lock",
                                             "Pipfile.lock", "composer.lock", "Gemfile.lock",
                                             "pnpm-lock.yaml"}},
    "📝 Types": {"dirs": set(), "patterns": {".d.ts"}},
    "📦 Minified": {"dirs": set(), "patterns": {".min.", ".bundle.", "-min.", ".minified.", ".bundled."}},
}

MODEL_CONTEXTS: Dict[str, int] = {
    "— none —": 0,
    "Claude Sonnet (200k)": 200000,
    "Claude Opus (200k)": 200000,
    "Claude Haiku (200k)": 200000,
    "GPT-4o (128k)": 128000,
    "GPT-4 Turbo (128k)": 128000,
    "GPT-4o mini (128k)": 128000,
    "Gemini 1.5 Pro (1M)": 1000000,
    "Llama 3.1 70B (128k)": 128000,
    "Custom": -1,
}
MODEL_SAFETY_MARGIN = 0.80

DEFAULT_TEMPLATES: Dict[str, str] = {
    "Raw": "{report}",
    "Code Review": ("Review the following codebase for bugs, security issues, and code smells. "
                    "Be specific and cite file/line. Here is the code:\n\n{report}\n\n"
                    "Provide feedback as a prioritized list."),
    "Refactor": ("Refactor the following code for clarity, readability, and maintainability. "
                 "Preserve behavior. Here is the code:\n\n{report}\n\n"
                 "Produce the refactored files, grouped by path."),
    "Explain": ("Explain what the following codebase does, its architecture, and its main "
                "components. Here is the code:\n\n{report}\n\n"
                "Structure your answer as: overview, key files, data flow, and notable patterns."),
    "Bug Hunt": ("Find bugs, race conditions, off-by-one errors, and unhandled edge cases in "
                 "the following code. Here is the code:\n\n{report}\n\n"
                 "For each issue, cite the file, line range, and a short repro or fix."),
    "Onboard": ("I'm new to this codebase. Walk me through:\n"
                "1. What it does\n2. Entry points\n"
                "3. Main modules and their responsibilities\n4. How to run it\n\n"
                "Here is the code:\n\n{report}"),
}

FRONTEND_MARKERS = {
    'package.json', 'index.html', 'vite.config.js', 'vite.config.ts',
    'next.config.js', 'next.config.mjs', 'next.config.ts',
    'tailwind.config.js', 'tailwind.config.ts', 'tsconfig.json',
    'angular.json', 'svelte.config.js', 'astro.config.mjs', 'nuxt.config.ts',
    'webpack.config.js', 'rollup.config.js', 'postcss.config.js', '.browserslistrc',
}
BACKEND_MARKERS = {
    'manage.py', 'requirements.txt', 'pyproject.toml', 'pipfile',
    'setup.py', 'settings.py', 'wsgi.py', 'asgi.py', 'app.py',
    'gunicorn.conf.py', 'uwsgi.ini', 'poetry.lock', 'pipfile.lock',
    'dockerfile', 'docker-compose.yml', 'docker-compose.yaml',
}
FRONTEND_FOLDER_HINTS = {
    'frontend', 'front', 'client', 'web', 'ui', 'public', 'static',
    'views', 'components', 'assets', 'src',
}
BACKEND_FOLDER_HINTS = {
    'backend', 'back', 'server', 'api', 'py', 'python',
    'django', 'flask', 'fastapi', 'app',
}

SECRET_PATTERNS = [
    (r'AKIA[0-9A-Z]{16}', 'AWS Access Key', re.IGNORECASE),
    (r'(?i)\b(api[_-]?key|apikey)\b\s*[:=]\s*["\']([A-Za-z0-9_\-]{16,})["\']', 'API key', 0),
    (r'(?i)\bbearer\s+[A-Za-z0-9_\-\.]{20,}', 'Bearer token', 0),
    (r'-----BEGIN (RSA|DSA|EC|OPENSSH|PRIVATE) KEY-----', 'Private key', 0),
    (r'(?i)\bpassword\b\s*[:=]\s*["\']([^"\']{6,})["\']', 'Hardcoded password', 0),
    (r'(?i)\bsecret\b\s*[:=]\s*["\']([A-Za-z0-9_\-]{16,})["\']', 'Hardcoded secret', 0),
    (r'ghp_[A-Za-z0-9]{36}', 'GitHub PAT', 0),
    (r'sk-[A-Za-z0-9]{32,}', 'OpenAI-style key', 0),
    (r'xox[baprs]-[A-Za-z0-9\-]{10,}', 'Slack token', 0),
    (r'(?i)\b(aws_secret_access_key|aws_secret_key)\b\s*[:=]\s*["\']?([A-Za-z0-9/+=]{40})', 'AWS secret', 0),
]

@dataclass
class ParseConfig:
    root_dir: Path = field(default_factory=Path.cwd)
    mode: str = "content"
    extensions: Set[str] = field(default_factory=set)
    extra_extensions: Set[str] = field(default_factory=set)
    output_base: str = "parse_report.txt"
    split_mode: str = "single"
    max_chars: int = DEFAULT_MAX_CHARS
    target_model: str = "— none —"
    model_budget: int = 0
    prompt_template: str = "Raw"
    include_env: bool = True
    include_bootstrap: bool = False
    include_dotfiles: bool = False
    strip_comments: bool = False
    scan_secrets: bool = False
    env_scope: Optional[str] = None
    import_extensions: Optional[Set[str]] = None
    create_zip: bool = False
    skip_binary: bool = True
    skip_empty: bool = True
    extra_exclude_dirs: Set[str] = field(default_factory=set)
    extra_exclude_paths: Set[str] = field(default_factory=set)
    extra_exclude_patterns: Set[str] = field(default_factory=set)
    active_presets: Set[str] = field(default_factory=set)
    pinned_files: Set[str] = field(default_factory=set)

@dataclass
class DiscoveryItem:
    abs_path: Path
    rel_path: str
    size: int
    ext: str

@dataclass
class ParseResult:
    parts: List[Path] = field(default_factory=list)
    zip_path: Optional[Path] = None
    files_scanned: int = 0
    files_included: int = 0
    total_bytes: int = 0
    elapsed: float = 0.0
    error: Optional[str] = None

def _is_env(path: Path) -> bool:
    n = path.name.lower()
    return n == '.env' or n.startswith('.env.')

def get_file_ext(path) -> str:
    p = Path(path); n = p.name.lower()
    if n == '.env' or n.startswith('.env.'): return '.env'
    if n in DOTFILE_NAMES: return n
    return p.suffix.lower()

def _is_binary(path: Path) -> bool:
    try:
        with open(path, 'rb') as f:
            return b'\x00' in f.read(1024)
    except Exception:
        return False

def _read_text(path: Path) -> Tuple[Optional[str], str]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        return None, f"ERROR: {exc}"
    if size > MAX_FILE_SIZE_BYTES:
        return None, f"SKIPPED (size {size//1024} KB > {MAX_FILE_SIZE_BYTES//1024} KB)"
    if _is_binary(path):
        return None, "SKIPPED (binary)"
    for enc in ENCODINGS:
        try:
            with open(path, 'r', encoding=enc, newline='') as handle:
                return handle.read(), enc
        except (UnicodeDecodeError, OSError):
            continue
    return None, "ERROR: unreadable"

def _normalise_rel_path(value: str) -> str:
    return value.replace("\\", "/")

def _path_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False

def _safe_pinned_path(root: Path, rel_path: str) -> Optional[Path]:
    rel = Path(rel_path)
    if rel.is_absolute():
        return None
    candidate = root / rel
    return candidate if _path_within(candidate, root) else None

def _looks_like_generated_report(path: Path) -> bool:
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore', newline='') as handle:
            return handle.read(256).startswith('Report-Gen ')
    except OSError:
        return False

def _sibling_names(p: Path) -> Set[str]:
    try:
        return {f.name.lower() for f in p.parent.iterdir() if f.is_file()}
    except (OSError, PermissionError):
        return set()

def _env_content_scores(p: Path) -> Tuple[int, int]:
    try:
        head = p.read_text(encoding='utf-8', errors='ignore')[:8192]
    except Exception:
        return 0, 0
    fe = be = 0
    if re.search(r'\b(VITE_|NEXT_PUBLIC_|REACT_APP_|VUE_APP_|NUXT_PUBLIC_|PUBLIC_)\w+', head):
        fe += 3
    if re.search(r'\b(DJANGO_|FLASK_|FASTAPI_|SECRET_KEY|DATABASE_URL|'
                 r'POSTGRES_|MYSQL_|MONGO_|REDIS_|CELERY_)\w*', head):
        be += 3
    return fe, be

def classify_env(env_path: Path, root: Path) -> str:
    fe = be = 0
    try:
        rel_parts = env_path.relative_to(root).parts[:-1]
    except ValueError:
        rel_parts = env_path.parts[:-1]
    for part in rel_parts:
        pl = part.lower()
        if pl in FRONTEND_FOLDER_HINTS: fe += 2
        if pl in BACKEND_FOLDER_HINTS: be += 2
    sibs = _sibling_names(env_path)
    fe += 3 * len(sibs & FRONTEND_MARKERS)
    be += 3 * len(sibs & BACKEND_MARKERS)
    cfe, cbe = _env_content_scores(env_path)
    fe += cfe; be += cbe
    if fe > be: return 'frontend'
    if be > fe: return 'backend'
    return 'unknown'

def _classify_root(root: Path) -> str:
    try:
        sibs = {f.name.lower() for f in root.iterdir() if f.is_file()}
    except Exception:
        return 'unknown'
    fe = len(sibs & FRONTEND_MARKERS)
    be = len(sibs & BACKEND_MARKERS)
    if fe > be: return 'frontend'
    if be > fe: return 'backend'
    return 'unknown'

def _env_allowed(env_path: Path, root: Path, scope: Optional[str]) -> bool:
    if scope in (None, 'both'): return True
    ctx = classify_env(env_path, root)
    if ctx == 'unknown': ctx = _classify_root(root)
    if scope == 'py':  return ctx in ('backend', 'unknown')
    if scope == 'web': return ctx in ('frontend', 'unknown')
    return True

def _strip_hash(content: str) -> str:
    out, in_triple = [], None
    for line in content.split('\n'):
        s = line.strip()
        if in_triple:
            out.append(line)
            if s.count(in_triple) % 2 == 1: in_triple = None
            continue
        for tq in ('"""', "'''"):
            if s.count(tq) % 2 == 1:
                in_triple = tq; break
        if in_triple:
            out.append(line); continue
        out.append('' if s.startswith('#') else line)
    return '\n'.join(out)

def _strip_c(content: str) -> str:
    out, in_block = [], False
    for line in content.split('\n'):
        s = line.strip()
        if in_block:
            if '*/' in line:
                in_block = False
                tail = line.split('*/', 1)[1]
                out.append(tail if tail.strip() else '')
            else:
                out.append('')
            continue
        if s.startswith('//'):
            out.append('')
        elif s.startswith('/*'):
            if '*/' in s[2:]:
                tail = s.split('*/', 1)[1]
                out.append(tail if tail.strip() else '')
            else:
                in_block = True; out.append('')
        else:
            out.append(line)
    return '\n'.join(out)

def _strip_html(content: str) -> str:
    return re.sub(r'<!--.*?-->', lambda m: '\n' * m.group(0).count('\n'),
                  content, flags=re.DOTALL)

def strip_comments(content: str, ext: str) -> str:
    ext = ext.lower()
    if ext in NO_STRIP_EXTS: return content
    if ext in ('.py', '.rb', '.yaml', '.yml', '.toml', '.ini', '.cfg', '.env'):
        return _strip_hash(content)
    if ext in ('.js', '.jsx', '.ts', '.tsx', '.css', '.scss', '.less',
               '.vue', '.java', '.kt', '.c', '.cpp', '.h', '.cs', '.go',
               '.rs', '.php', '.swift'):
        return _strip_c(content)
    if ext in ('.html', '.htm', '.xml', '.svg'):
        return _strip_html(content)
    return content

def scan_secrets(content: str) -> List[Tuple[str, str]]:
    findings = []
    if not content: return findings
    for pat, label, flags in SECRET_PATTERNS:
        try:
            for m in re.finditer(pat, content, flags):
                s = m.group(0)
                findings.append((label, s[:57] + '...' if len(s) > 60 else s))
                if len(findings) >= 5: return findings
        except re.error:
            continue
    return findings

def _effective_excludes(config: ParseConfig) -> Tuple[Set[str], Set[str]]:
    dirs = DEFAULT_EXCLUDE_DIRS | config.extra_exclude_dirs
    patterns = set(config.extra_exclude_patterns)
    for name in config.active_presets:
        p = EXCLUDE_PRESETS.get(name)
        if not p: continue
        dirs |= p["dirs"]
        patterns |= p["patterns"]
    return dirs, patterns

def discover(config: ParseConfig) -> List[DiscoveryItem]:
    root = config.root_dir.resolve()
    out: List[DiscoveryItem] = []
    seen_rels: Set[str] = set()
    exclude_dirs, exclude_patterns = _effective_excludes(config)
    exclude_paths = {_normalise_rel_path(p) for p in config.extra_exclude_paths}
    pinned_norm = {_normalise_rel_path(p) for p in config.pinned_files}

    for dirpath, dirnames, filenames in os.walk(root):
        try:
            rel_dir = Path(dirpath).relative_to(root)
        except ValueError:
            rel_dir = Path(".")
        kept = []
        for d in dirnames:
            if d.startswith('.') and not config.include_dotfiles: continue
            if d.lower() in exclude_dirs: continue
            if exclude_paths:
                sub_rel = (rel_dir / d).as_posix()
                if sub_rel.startswith("./"): sub_rel = sub_rel[2:]
                if sub_rel in exclude_paths: continue
            kept.append(d)
        dirnames[:] = kept

        for fname in filenames:
            fp = Path(dirpath) / fname
            name_low = fname.lower()

            # Do not follow file symlinks outside the selected project root.
            if not _path_within(fp, root):
                continue

            if not config.include_bootstrap:
                if any(p in name_low for p in BOOTSTRAP_PATTERNS):
                    continue
            if exclude_patterns:
                if any(pat.lower() in name_low for pat in exclude_patterns):
                    continue
            if fname in DOTFILE_NAMES and not config.include_dotfiles:
                continue

            try:
                st = fp.stat()
            except OSError:
                continue
            if config.skip_empty and st.st_size == 0:
                continue

            try:
                rel = fp.relative_to(root)
            except ValueError:
                rel = Path(fp.name)
            rel_str = str(rel)
            rel_norm = rel_str.replace("\\", "/")

            is_pinned = rel_norm in pinned_norm

            if _is_env(fp):
                if not is_pinned:
                    if not config.include_env: continue
                    if config.mode == 'content' and '.env' not in config.extensions: continue
                    if config.env_scope and not _env_allowed(fp, root, config.env_scope):
                        continue
            else:
                ext = fp.suffix.lower()
                include = False
                if config.mode == 'paths': include = True
                elif config.mode == 'imports':
                    if config.import_extensions is None: include = True
                    elif ext in config.import_extensions: include = True
                else:
                    if ext in config.extensions: include = True
                    elif ext in config.extra_extensions: include = True
                if not include and not is_pinned: continue

            if config.skip_binary and not is_pinned and _is_binary(fp):
                continue

            if re.match(r'^.*_part\d+$', fp.stem) and fp.suffix == '.txt' and not is_pinned:
                continue
            if fp.parent == root and _looks_like_generated_report(fp) and not is_pinned:
                continue

            out.append(DiscoveryItem(fp, rel_str, st.st_size, get_file_ext(fp)))
            seen_rels.add(rel_norm)

    for rel_pinned in config.pinned_files:
        rel_norm = _normalise_rel_path(rel_pinned)
        if rel_norm in seen_rels: continue
        fp = _safe_pinned_path(root, rel_pinned)
        if fp is None: continue
        if not fp.exists() or not fp.is_file(): continue
        try:
            st = fp.stat()
        except OSError:
            continue
        out.append(DiscoveryItem(fp, rel_norm, st.st_size, get_file_ext(fp)))
        seen_rels.add(rel_norm)

    out.sort(key=lambda x: x.rel_path)
    return out

def _encode_file_block(rel_path: str, content: str = "", *, status: str = "content",
                       encoding: Optional[str] = None,
                       message: Optional[str] = None) -> str:
    metadata = {
        "path": _normalise_rel_path(str(rel_path)),
        "chars": len(content),
        "status": status,
    }
    if encoding:
        metadata["encoding"] = encoding
    if message:
        metadata["message"] = message
    marker = MARK_FILE + json.dumps(metadata, ensure_ascii=False, separators=(',', ':'))
    # The payload is deliberately left readable. Its exact character length in
    # metadata makes marker-looking source lines unambiguous during reversal.
    return marker + "\n" + content + "\n"

def _render_part(header_str: str, blocks: List[str], num: int, total: int) -> str:
    prefix = f"{header_str}\n\n{MARK_PART} {num:02d} OF {total:02d} =$@\n\n"
    body = "".join(blocks)
    suffix = f"{MARK_END} OF PART {num:02d} =$@\n"
    return prefix + body + suffix

def _split_blocks(header_lines, blocks, max_chars, single=False):
    header_str = "\n".join(header_lines).rstrip("\n")
    if single:
        return [(1, 1, _render_part(header_str, blocks, 1, 1))]

    # Preserve discovery order. A single file remains whole even when it alone
    # exceeds the budget; every multi-file part stays within the limit.
    # Use a conservative rendered marker width without substantially
    # under-filling parts.
    overhead = len(_render_part(header_str, [], 9999, 9999))
    groups, cur, cur_size = [], [], overhead
    for block in blocks:
        if cur and cur_size + len(block) > max_chars:
            groups.append(cur)
            cur, cur_size = [], overhead
        cur.append(block)
        cur_size += len(block)
        if len(cur) == 1 and cur_size > max_chars:
            groups.append(cur)
            cur, cur_size = [], overhead
    if cur:
        groups.append(cur)
    if not groups:
        groups.append([])
    total = len(groups)
    return [(num, total, _render_part(header_str, group, num, total))
            for num, group in enumerate(groups, 1)]

def _header_for(config, file_count):
    label = {'paths': 'all files (paths only)',
             'imports': 'imports only'}.get(
                 config.mode, ', '.join(sorted(config.extensions | config.extra_extensions)))
    h = [f"Report-Gen {datetime.now().strftime('%Y-%m-%d %H:%M')}",
         f"{REPORT_FORMAT_PREFIX} {REPORT_FORMAT_VERSION}",
         f"{REPORT_MODE_PREFIX} {config.mode}",
         f"Root: {config.root_dir}", f"File type: {label}"]
    if config.mode == 'imports': h.append(f"Files with imports: {file_count}")
    if config.mode == 'paths':   h.append(f"Total files: {file_count}")
    if config.strip_comments:    h[4] += "  [comment-stripped]"
    if config.pinned_files:      h.append(f"Pinned: {len(config.pinned_files)}")
    h.append("")
    return h

def _build_paths_blocks(files, root):
    blocks = []
    for fp in files:
        try: rel = fp.relative_to(root)
        except ValueError: rel = fp.name
        blocks.append(_encode_file_block(str(rel), status="path"))
    return blocks

def _build_imports_blocks(files, root, progress_cb, cancel):
    blocks = []
    for i, fp in enumerate(files, 1):
        if cancel and cancel.is_set(): break
        progress_cb(i, len(files), f"Reading {fp.name}")
        if _is_binary(fp): continue
        lines = None
        for enc in ENCODINGS:
            try:
                with open(fp, 'r', encoding=enc) as f:
                    lines = [ln.rstrip('\n') + '\n' for ln in f if 'import' in ln.lower()]
                break
            except (UnicodeDecodeError, OSError):
                continue
        if not lines: continue
        try: rel = fp.relative_to(root)
        except ValueError: rel = fp.name
        blocks.append(_encode_file_block(str(rel), "".join(lines), status="imports"))
    return blocks

def _build_content_blocks(files, config, progress_cb, log_cb, cancel):
    root = config.root_dir.resolve()
    blocks = []
    for i, fp in enumerate(files, 1):
        if cancel and cancel.is_set(): break
        progress_cb(i, len(files), f"Reading {fp.name}")
        try: rel = fp.relative_to(root)
        except ValueError: rel = fp.name
        content, meta = _read_text(fp)
        if content is None:
            log_cb(f"Skipped {rel}: {meta}", "warn")
            blocks.append(_encode_file_block(str(rel), status="skipped", message=meta))
            continue
        else:
            if config.scan_secrets:
                findings = scan_secrets(content)
                if findings:
                    log_cb(f"⚠ {rel}: {len(findings)} potential secret(s)", "warn")
                    for label, _ in findings:
                        log_cb(f"   - {label}", "warn")
            if config.strip_comments:
                ext = get_file_ext(fp)
                if ext in NO_STRIP_EXTS:
                    log_cb(f"   ↪ {rel}: shell file, comments kept", "info")
                content = strip_comments(content, ext)
            blocks.append(_encode_file_block(str(rel), content,
                                             status="content", encoding=meta))
    return blocks

def _safe_output_parts(root: Path, output_base: str, split: bool,
                       count: int) -> Tuple[str, str, List[Path]]:
    if not output_base or Path(output_base).name != output_base or '/' in output_base or '\\' in output_base:
        raise ValueError("Output must be a file name, not a path")
    out = Path(output_base)
    suffix = out.suffix.lstrip('.') or 'txt'
    stem = out.stem or 'parse_report'
    if split:
        paths = [root / f"{stem}_part{num:02d}.{suffix}" for num in range(1, count + 1)]
    else:
        paths = [root / f"{stem}.{suffix}"]
    return stem, suffix, paths

def _stage_text(path: Path, text: str) -> Path:
    handle = tempfile.NamedTemporaryFile(
        mode='w', encoding='utf-8', newline='', dir=path.parent,
        prefix=f".{path.name}.", suffix='.tmp', delete=False)
    try:
        with handle:
            handle.write(text)
        return Path(handle.name)
    except Exception:
        try: Path(handle.name).unlink()
        except OSError: pass
        raise

def _cleanup_stale_parts(root: Path, stem: str, suffix: str,
                         keep: Set[Path]) -> None:
    exact = re.compile(rf'^{re.escape(stem)}(?:_part\d+)?\.{re.escape(suffix)}$')
    for candidate in root.iterdir():
        if candidate in keep or not candidate.is_file():
            continue
        if exact.match(candidate.name) and _looks_like_generated_report(candidate):
            try: candidate.unlink()
            except OSError: pass

def run_parse(config, files, progress_cb=None, log_cb=None, cancel=None):
    start = time.time()
    result = ParseResult()
    log = log_cb or (lambda m, l="info": None)
    prog = progress_cb or (lambda c, t, m="": None)
    root = config.root_dir.resolve()
    staged: List[Tuple[Path, Path, str]] = []
    temp_zip_path: Optional[Path] = None

    def cancelled(): return cancel is not None and cancel.is_set()

    try:
        result.files_scanned = len(files)
        log(f"Scanning {root}", "info")
        log(f"Processing {len(files)} file(s)", "info")

        if config.mode == 'paths':
            blocks = _build_paths_blocks(files, root)
        elif config.mode == 'imports':
            blocks = _build_imports_blocks(files, root, prog, cancel)
        else:
            blocks = _build_content_blocks(files, config, prog, log, cancel)

        if cancelled():
            result.error = "Cancelled"; return result

        header = _header_for(config, len(blocks))
        single = (config.split_mode == 'single')
        parts = _split_blocks(header, blocks, config.max_chars, single=single)
        log(f"Will produce {len(parts)} part(s)", "info")
        if not single:
            oversized_count = sum(len(text) > config.max_chars
                                  for _, _, text in parts)
            if oversized_count:
                log(f"{oversized_count} part(s) exceed the target because a file is kept whole",
                    "warn")

        stem, suffix, output_paths = _safe_output_parts(
            root, config.output_base, not single, len(parts))
        total_bytes = 0
        for (num, total, text), target in zip(parts, output_paths):
            if cancelled():
                for temp_path, _, _ in staged:
                    try: temp_path.unlink()
                    except OSError: pass
                result.error = "Cancelled"; return result
            temp_path = _stage_text(target, text)
            staged.append((temp_path, target, text))

        if cancelled():
            for temp_path, _, _ in staged:
                try: temp_path.unlink()
                except OSError: pass
            result.error = "Cancelled"; return result

        for num, (temp_path, target, text) in enumerate(staged, 1):
            os.replace(temp_path, target)
            result.parts.append(target)
            total_bytes += len(text.encode('utf-8'))
            prog(num, len(parts), f"Wrote {target.name}")
            log(f"✅ {target.name}", "info")

        _cleanup_stale_parts(root, stem, suffix, set(result.parts))

        result.total_bytes = total_bytes
        result.files_included = len(blocks)

        if config.create_zip and result.parts:
            zp = root / f"{stem}.zip"
            temp_zip = tempfile.NamedTemporaryFile(
                dir=root, prefix=f".{stem}.", suffix='.zip.tmp', delete=False)
            temp_zip_path = Path(temp_zip.name)
            temp_zip.close()
            with zipfile.ZipFile(temp_zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                for p in result.parts:
                    zf.write(p, p.name)
            os.replace(temp_zip_path, zp)
            result.zip_path = zp
            log(f"✅ ZIP: {zp.name}", "info")

        result.elapsed = time.time() - start
        log(f"Done in {result.elapsed:.1f}s", "info")
        return result
    except Exception as e:
        for temp_path, _, _ in staged:
            try: temp_path.unlink()
            except OSError: pass
        if temp_zip_path is not None:
            try: temp_zip_path.unlink()
            except OSError: pass
        result.error = str(e)
        return result

def _read_report_text(path: Path) -> str:
    with open(path, 'r', encoding='utf-8', newline='') as handle:
        return handle.read()

def _report_version(text: str) -> int:
    match = re.search(rf'^{re.escape(REPORT_FORMAT_PREFIX)}\s*(\d+)\s*$',
                      text[:2048], re.MULTILINE)
    return int(match.group(1)) if match else 1

def _discover_report_groups(src: Path) -> Dict[Tuple[str, str], List[Tuple[int, Path]]]:
    groups: Dict[Tuple[str, str], List[Tuple[int, Path]]] = {}
    part_pattern = re.compile(r'^(.*?)_part(\d+)(\.[^.]+)$')
    try:
        candidates = list(src.iterdir())
    except OSError:
        return groups
    for path in candidates:
        if not path.is_file() or not _looks_like_generated_report(path):
            continue
        match = part_pattern.match(path.name)
        if match:
            key = (match.group(1), match.group(3))
            number = int(match.group(2))
        else:
            key = (path.stem, path.suffix)
            number = 0
        groups.setdefault(key, []).append((number, path))
    return groups

def _next_line_marker(text: str, marker: str, start: int) -> int:
    position = text.find(marker, start)
    while position >= 0 and position > 0 and text[position - 1] not in '\r\n':
        position = text.find(marker, position + len(marker))
    return position

def _decode_v2_entries(text: str) -> List[Dict[str, object]]:
    entries: List[Dict[str, object]] = []
    position = 0
    while True:
        marker_pos = _next_line_marker(text, MARK_FILE, position)
        if marker_pos < 0:
            break
        line_end = text.find('\n', marker_pos)
        if line_end < 0:
            raise ValueError("Truncated file metadata line")
        raw_metadata = text[marker_pos + len(MARK_FILE):line_end].rstrip('\r')
        try:
            metadata = json.loads(raw_metadata)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid file metadata: {exc}") from exc
        if not isinstance(metadata, dict) or not isinstance(metadata.get('path'), str):
            raise ValueError("Invalid file metadata object")
        char_count = metadata.get('chars')
        if not isinstance(char_count, int) or char_count < 0:
            raise ValueError("Invalid file payload length")
        payload_start = line_end + 1
        payload_end = payload_start + char_count
        if payload_end > len(text):
            raise ValueError(f"Truncated payload for {metadata['path']}")
        metadata['content'] = text[payload_start:payload_end]
        entries.append(metadata)
        position = payload_end
    return entries

def _decode_legacy_entries(text: str) -> List[Dict[str, object]]:
    entries: List[Dict[str, object]] = []
    current_path: Optional[str] = None
    current_lines: List[str] = []
    for line in text.splitlines(keepends=True):
        comparable = line.rstrip('\r\n')
        if comparable.startswith(MARK_FILE):
            if current_path is not None:
                entries.append({"path": current_path,
                                "content": ''.join(current_lines),
                                "status": "content", "encoding": "utf-8"})
            current_path = comparable[len(MARK_FILE):].strip()
            current_lines = []
        elif current_path is None:
            continue
        elif comparable.startswith(MARK_PART) or comparable.startswith(MARK_END):
            continue
        else:
            current_lines.append(line)
    if current_path is not None:
        entries.append({"path": current_path, "content": ''.join(current_lines),
                        "status": "content", "encoding": "utf-8"})
    return entries

def _safe_reverse_destination(dst: Path, rel_path: str) -> Optional[Path]:
    rel = Path(rel_path)
    if (not rel_path or rel.is_absolute() or re.match(r'^[A-Za-z]:', rel_path)
            or any(part in ('', '.', '..') for part in rel.parts)):
        return None
    root = dst.resolve()
    candidate = (root / rel).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate

def _write_reconstructed_text(path: Path, content: str, encoding: str) -> None:
    encoding = encoding if encoding in ENCODINGS else 'utf-8'
    with open(path, 'w', encoding=encoding, newline='') as handle:
        handle.write(content)

# =====================================================================
#                               GUI
# =====================================================================

APP_NAME = "Ultimate Parse"
PROFILE_DIR = Path.home() / ".ultimate_parse"
PROFILE_FILE = PROFILE_DIR / "profiles.json"
TEMPLATES_FILE = PROFILE_DIR / "templates.json"

# ---------------------------------------------------------------------
# WCAG AA palette. Contrast computed for every pairing.
# Format: (light_value, dark_value). CTk accepts single strings too.
#
#   Buttons/borders (fill vs page):
#     green  #15803d → 4.25:1 light / 2.84:1 dark (border-only accent below)
#     red    #b91c1c → 5.51:1 light / 2.19:1 dark
#     blue   #1e40af → 8.83:1 light / 1.63:1 dark
#   Borders supply 1.4.11 compliance (≥ 3:1) in dark theme:
#     bright green  #22c55e → 6.35:1
#     bright red    #ef4444 → 3.81:1
#     bright blue   #3b82f6 → 3.89:1
#   Body text (labels) needs ≥ 4.5:1:
#     dark theme: #4ade80 (8.25:1), #f87171 (5.29:1), #93c5fd (8.00:1)
#     light theme: #14532d (7.81:1), #7f1d1d (8.52:1), #1e3a8a (8.83:1)
# ---------------------------------------------------------------------

# Fill colors (button/checkbox interior — same in both themes, white text on top)
C_INCLUDE_BOX   = "#15803d"   # green-700  — white text 5.05:1
C_INCLUDE_HOVER = "#166534"   # green-800  — white text 6.55:1
C_EXCLUDE_BOX   = "#b91c1c"   # red-700    — white text 6.55:1
C_EXCLUDE_HOVER = "#991b1b"   # red-800    — white text 7.90:1
C_OPTION_BOX    = "#1e40af"   # blue-800   — white text 8.83:1
C_OPTION_HOVER  = "#1d4ed8"   # blue-700   — white text 6.83:1
C_TICK          = "#ffffff"   # checkmark / button text on all fills

# Borders (theme-aware, ≥ 3:1 vs page for WCAG 1.4.11)
C_INCLUDE_ACCENT = ("#15803d", "#22c55e")   # green-700 / green-500 (6.35:1 dark)
C_EXCLUDE_ACCENT = ("#b91c1c", "#ef4444")   # red-700   / red-500   (3.81:1 dark)
C_OPTION_ACCENT  = ("#1e40af", "#3b82f6")   # blue-800  / blue-500  (3.89:1 dark)

# Colored label text (theme-aware, ≥ 4.5:1 in both themes)
C_INCLUDE_TEXT = ("#14532d", "#4ade80")   # green-900 / green-400 (7.81:1 / 8.25:1)
C_EXCLUDE_TEXT = ("#7f1d1d", "#f87171")   # red-900   / red-400   (8.52:1 / 5.29:1)
C_OPTION_TEXT  = ("#1e3a8a", "#93c5fd")   # blue-900  / blue-300  (8.83:1 / 8.00:1)

# Semantic text (theme-aware)
C_OK      = ("#15803d", "#4ade80")   # AA on both themes
C_WARN    = ("#92400e", "#fbbf24")   # amber-800 / amber-400 (6.05:1 / 8.61:1)
C_DANGER  = ("#b91c1c", "#f87171")   # red-700   / red-400   (5.51:1 / 5.29:1)
C_MUTED   = ("#4b5563", "#c4c8d0")   # gray-600  / gray-350  (6.48:1 / 8.66:1)
C_HINT    = ("#52525b", "#b0b4bc")   # zinc-600  / zinc-350  (6.00:1 / 6.84:1)
C_REFRESH = ("#52525b", "#a0a8b4")   # zinc-600  / zinc-400  (6.00:1 / 5.92:1)
C_PIN     = ("#854d0e", "#facc15")   # yellow-800 / yellow-400 (5.90:1 / 9.37:1)

# Suggestion bar
C_SUGGEST_BG     = ("#fef3c7", "#422006")   # amber-100 / amber-950
C_SUGGEST_BORDER = ("#d97706", "#ca8a04")   # amber-600 / amber-600
C_SUGGEST_TEXT   = ("#78350f", "#fde68a")   # amber-900 / amber-200 (8.22:1 / 11.85:1)

# Tree (kept dark in both themes for consistency)
C_TREE_BG         = "#2b2b2b"
C_TREE_FG         = "#e0e0e0"   # 10.90:1 vs tree bg
C_TREE_HEAD_BG    = "#1f1f1f"
C_TREE_HEAD_FG    = "#ffffff"
C_TREE_SELECT_BG  = "#1f6aa5"   # focus ring
C_TREE_SELECT_FG  = "#ffffff"
C_TREE_PLACEHOLDER = "#b0b4bc"  # 6.84:1 vs tree bg

F_TITLE   = ("Segoe UI", 14, "bold")
F_BODY    = ("Segoe UI", 13)
F_SMALL   = ("Segoe UI", 12)
F_TINY    = ("Segoe UI", 11)
F_MICRO   = ("Segoe UI", 10)
F_NANO    = ("Segoe UI", 9)
F_SECTION = ("Segoe UI", 15, "bold")
F_ICON    = ("Segoe UI", 16, "bold")
F_ICON_SM = ("Segoe UI", 14, "bold")
F_MONO    = ("Consolas", 13)
F_MONO_SM = ("Consolas", 12)
F_MONO_XS = ("Consolas", 11)

MODE_PRESETS = {
    "Python + .env + shells": {
        "extensions": ['.py', '.env', '.bash', '.ps1', '.sh', '.zsh'],
        "output_base": "py_files_report.txt", "env_scope": "py"},
    "Web + extra": {
        "extensions": ['.js', '.html', '.htm', '.css', '.vue', '.env'],
        "output_base": "web_files_report.txt", "env_scope": "web"},
    "Web without CSS + extra": {
        "extensions": ['.js', '.html', '.htm', '.vue', '.env'],
        "output_base": "web_no_css_files_report.txt", "env_scope": "web"},
    "Both": {
        "extensions": ['.py', '.env', '.bash', '.ps1', '.sh', '.zsh',
                       '.js', '.html', '.htm', '.css', '.vue'],
        "output_base": "all_files_report.txt", "env_scope": "both"},
    "Paths only": {"extensions": [], "output_base": "file_paths_report.txt", "env_scope": None},
    "Imports only": {
        "extensions": ['.py', '.js', '.html', '.htm', '.css', '.vue', '.env'],
        "output_base": "imports_report.txt", "env_scope": None},
    "Comment-stripped": {
        "extensions": ['.py', '.js', '.html', '.htm', '.css', '.vue', '.env'],
        "output_base": "comment_stripped_report.txt", "env_scope": "both"},
}

class ParseApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("System")
        ctk.set_default_color_theme("blue")
        self.title(APP_NAME)
        self.geometry("1360x920")

        try:
            self.resizable(True, True)
        except Exception:
            pass
        if platform.system() == "Linux":
            try:
                self.wm_attributes("-type", "normal")
            except Exception:
                pass
            try:
                self.attributes("-type", "normal")
            except Exception:
                pass
        try:
            self.update_idletasks()
            self.minsize(640, 500)
        except Exception:
            pass

        PROFILE_DIR.mkdir(exist_ok=True)

        self.config = ParseConfig()
        self.discovered: list[DiscoveryItem] = []
        self.last_result: Optional[ParseResult] = None
        self.cancel_event = threading.Event()
        self.profiles = self._load_profiles()
        self.templates = self._load_templates()
        self.file_checked: dict[str, bool] = {}
        self._filter_after_id = None
        self._auto_refresh_id = None
        self._auto_refresh_deadline = None
        self._estimate_after_id = None
        self._tree_anchor = None
        self._has_discovered = False
        self._suggestion_hidden = True
        self._pending_suggestion_count = 0
        self._extra_type_vars: Dict[str, Tuple[tk.BooleanVar, Set[str]]] = {}
        self._refresh_generation = 0
        self._discover_generation = 0
        self._build()

    def _build(self):
        self.tabs = ctk.CTkTabview(self)
        self.tabs.pack(fill="both", expand=True, padx=3, pady=3)
        self.tab_scan     = self.tabs.add("Create Report")
        self.tab_reverse  = self.tabs.add("Restore Files")
        self.tab_settings = self.tabs.add("Settings")
        self.after(60, self._customize_tabs)
        self._build_scan()
        self._build_reverse()
        self._build_settings()

    def _customize_tabs(self):
        try:
            sb = self.tabs._segmented_button
        except Exception:
            return
        for h in (22, 24, 26):
            try:
                sb.configure(height=h); break
            except Exception:
                continue
        try:
            for i in range(3):
                sb.grid_columnconfigure(i, weight=1, uniform="tab")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Widget factories
    # ------------------------------------------------------------------
    def _checkbox(self, parent, text, variable, kind="option",
                  command=None, micro=False, **kw):
        # Border = ACCENT (bright in dark theme so 1.4.11 passes)
        # Fill   = BOX    (dark in both themes so white checkmark reads)
        accents = {
            "include": C_INCLUDE_ACCENT,
            "exclude": C_EXCLUDE_ACCENT,
            "option":  C_OPTION_ACCENT,
        }
        fills = {
            "include": (C_INCLUDE_BOX, C_INCLUDE_HOVER),
            "exclude": (C_EXCLUDE_BOX, C_EXCLUDE_HOVER),
            "option":  (C_OPTION_BOX,  C_OPTION_HOVER),
        }
        if micro:
            font = F_MICRO; box = 14
        else:
            font = F_SMALL; box = 17
        base = dict(text=text, variable=variable,
                    fg_color=fills[kind][0], hover_color=fills[kind][1],
                    checkmark_color=C_TICK,
                    border_color=accents[kind],
                    border_width=2,
                    font=font, checkbox_width=box, checkbox_height=box,
                    command=command)
        base.update(kw)
        return ctk.CTkCheckBox(parent, **base)

    def _radio(self, parent, text, variable, value, kind="option",
               command=None, compact=False, **kw):
        accents = {
            "include": C_INCLUDE_ACCENT,
            "exclude": C_EXCLUDE_ACCENT,
            "option":  C_OPTION_ACCENT,
        }
        fills = {
            "include": (C_INCLUDE_BOX, C_INCLUDE_HOVER),
            "exclude": (C_EXCLUDE_BOX, C_EXCLUDE_HOVER),
            "option":  (C_OPTION_BOX,  C_OPTION_HOVER),
        }
        base = dict(text=text, variable=variable, value=value,
                    fg_color=fills[kind][0], hover_color=fills[kind][1],
                    border_color=accents[kind],
                    font=F_BODY if not compact else F_SMALL,
                    command=command)
        if compact:
            base.update(radiobutton_width=15, radiobutton_height=15)
        else:
            base.update(radiobutton_width=18, radiobutton_height=18)
        base.update(kw)
        return ctk.CTkRadioButton(parent, **base)

    def _panel(self, parent, icon, title, color, text_color, small=True):
        frame = ctk.CTkFrame(parent, border_width=1, border_color=color,
                             fg_color="transparent")
        header = ctk.CTkFrame(frame, fg_color="transparent")
        header.pack(fill="x", padx=2, pady=(1, 0))
        ctk.CTkLabel(header, text=icon,
                     font=F_ICON_SM if small else F_ICON,
                     text_color=color).pack(side="left", padx=(0, 2))
        ctk.CTkLabel(header, text=title,
                     font=F_MICRO if small else F_TITLE,
                     text_color=text_color).pack(side="left")
        body = ctk.CTkFrame(frame, fg_color="transparent")
        body.pack(fill="x", padx=2, pady=(0, 1))
        return frame, body

    # ------------------------------------------------------------------
    # SCAN TAB
    # ------------------------------------------------------------------
    def _build_scan(self):
        tab = self.tab_scan

        wizard = ctk.CTkFrame(tab, fg_color="transparent", height=32)
        wizard.pack(fill="x", padx=4, pady=(2, 1))
        wizard.pack_propagate(False)
        self.step_setup_label = ctk.CTkLabel(
            wizard, text="1  Configure & discover", font=F_TITLE,
            text_color=C_OPTION_TEXT)
        self.step_setup_label.pack(side="left", padx=(4, 12))
        ctk.CTkLabel(wizard, text="→", font=F_TITLE,
                     text_color=C_MUTED).pack(side="left")
        self.step_review_label = ctk.CTkLabel(
            wizard, text="2  Review & create report", font=F_TITLE,
            text_color=C_MUTED)
        self.step_review_label.pack(side="left", padx=12)

        self.scan_setup_view = ctk.CTkFrame(tab, fg_color="transparent")
        self.scan_review_view = ctk.CTkFrame(tab, fg_color="transparent")
        setup = self.scan_setup_view
        review = self.scan_review_view

        chips_row = ctk.CTkFrame(setup, fg_color="transparent", height=30)
        chips_row.pack(fill="x", padx=2, pady=0)
        chips_row.pack_propagate(False)
        ctk.CTkLabel(chips_row, text="⭐ Profiles:",
                     font=F_TITLE).pack(side="left", padx=(2, 3))
        self.profile_chips_frame = ctk.CTkFrame(chips_row, fg_color="transparent")
        self.profile_chips_frame.pack(side="left", fill="x", expand=True)

        root_row = ctk.CTkFrame(setup, fg_color="transparent")
        root_row.pack(fill="x", padx=2, pady=0)
        ctk.CTkLabel(root_row, text="📁 Root:",
                     font=F_TITLE).pack(side="left", padx=(2, 3))
        self.root_var = tk.StringVar(value=str(Path.cwd()))
        ctk.CTkEntry(root_row, textvariable=self.root_var,
                     font=F_BODY, height=28).pack(
            side="left", fill="x", expand=True, padx=2)
        ctk.CTkButton(root_row, text="Browse…", width=76, font=F_BODY, height=28,
                      command=self._pick_root).pack(side="left", padx=2)
        self.discover_btn = ctk.CTkButton(
            root_row, text="Discover files →", width=132, font=F_TITLE,
            height=28, fg_color=C_INCLUDE_BOX, hover_color=C_INCLUDE_HOVER,
            border_width=2, border_color=C_INCLUDE_ACCENT,
            text_color=C_TICK, command=self._discover)
        self.discover_btn.pack(side="left", padx=(2, 2))

        mode_row = ctk.CTkFrame(setup, fg_color="transparent")
        mode_row.pack(fill="x", padx=2, pady=0)
        ctk.CTkLabel(mode_row, text="🎛 Mode:",
                     font=F_TITLE).pack(side="left", padx=(2, 3), anchor="n")
        mode_grid = ctk.CTkFrame(mode_row, fg_color="transparent")
        mode_grid.pack(side="left", fill="x", expand=True)
        self.mode_var = tk.StringVar(value="Python + .env + shells")
        for i, label in enumerate(MODE_PRESETS):
            r, c = divmod(i, 4)
            self._radio(mode_grid, label, self.mode_var, label, "option",
                        compact=True, command=self._apply_mode).grid(
                row=r, column=c, padx=1, pady=0, sticky="w")

        # Suggestion bar
        self.suggestion_bar = ctk.CTkFrame(
            setup, fg_color=C_SUGGEST_BG,
            border_width=1, border_color=C_SUGGEST_BORDER, corner_radius=4)
        self.suggestion_label = ctk.CTkLabel(
            self.suggestion_bar, text="", font=F_SMALL, text_color=C_SUGGEST_TEXT,
            anchor="w")
        self.suggestion_label.pack(side="left", padx=(6, 4), pady=2)
        ctk.CTkButton(self.suggestion_bar, text="Apply", width=54,
                      font=F_SMALL, height=22,
                      fg_color=C_SUGGEST_BORDER, hover_color="#a16207",
                      text_color=C_TICK,
                      command=self._apply_auto_suggest).pack(
            side="right", padx=(2, 4), pady=1)
        ctk.CTkButton(self.suggestion_bar, text="Dismiss", width=64,
                      font=F_SMALL, height=22,
                      fg_color="transparent", border_width=1,
                      border_color=C_SUGGEST_BORDER, text_color=C_SUGGEST_TEXT,
                      hover_color="#3f2d05",
                      command=self._dismiss_suggestion).pack(
            side="right", padx=(2, 2), pady=1)

        # ===== 3-panel row =====
        self.io_wrap = ctk.CTkFrame(setup, fg_color="transparent")
        self.io_wrap.pack(fill="x", padx=2, pady=(1, 0))
        for c in range(3):
            self.io_wrap.grid_columnconfigure(c, weight=1)

        # ----- INCLUDES -----
        inc_frame, inc = self._panel(self.io_wrap, "➕", "INCLUDES",
                                     C_INCLUDE_ACCENT, C_INCLUDE_TEXT, small=True)
        self.inc_panel_frame = inc_frame

        inc_r1 = ctk.CTkFrame(inc, fg_color="transparent")
        inc_r1.pack(fill="x", pady=0)
        self.opt_env      = tk.BooleanVar(value=True)
        self.opt_dotfiles = tk.BooleanVar(value=False)
        self._checkbox(inc_r1, ".env", self.opt_env, "include",
                       micro=True, command=self._schedule_auto_refresh).pack(
            side="left", padx=(0, 6))
        self._checkbox(inc_r1, "dotfiles", self.opt_dotfiles, "include",
                       micro=True, command=self._schedule_auto_refresh).pack(
            side="left", padx=(0, 0))

        ctk.CTkLabel(inc, text="Extra types:",
                     font=F_NANO, text_color=C_INCLUDE_TEXT,
                     anchor="w").pack(fill="x", padx=0, pady=(2, 0))
        inc_grid = ctk.CTkFrame(inc, fg_color="transparent")
        inc_grid.pack(fill="x")
        for c in range(2):
            inc_grid.grid_columnconfigure(c, weight=1)
        for i, (label, exts) in enumerate(EXTRA_TYPE_OPTIONS):
            var = tk.BooleanVar(value=False)
            self._extra_type_vars[label] = (var, exts)
            r, c = divmod(i, 2)
            self._checkbox(inc_grid, label, var, "include", micro=True,
                           command=self._schedule_auto_refresh).grid(
                row=r, column=c, padx=0, pady=0, sticky="w")

        # ----- EXCLUDES -----
        exc_frame, exc = self._panel(self.io_wrap, "➖", "EXCLUDES",
                                     C_EXCLUDE_ACCENT, C_EXCLUDE_TEXT, small=True)
        self.exc_panel_frame = exc_frame

        exc_r1 = ctk.CTkFrame(exc, fg_color="transparent")
        exc_r1.pack(fill="x", pady=0)
        self.opt_bootstrap_exclude = tk.BooleanVar(value=True)
        self.opt_skip_binary = tk.BooleanVar(value=True)
        self.opt_skip_empty  = tk.BooleanVar(value=True)
        self._checkbox(exc_r1, "boot", self.opt_bootstrap_exclude,
                       "exclude", micro=True,
                       command=self._schedule_auto_refresh).pack(
            side="left", padx=(0, 4))
        self._checkbox(exc_r1, "bin", self.opt_skip_binary, "exclude",
                       micro=True,
                       command=self._schedule_auto_refresh).pack(
            side="left", padx=(0, 4))
        self._checkbox(exc_r1, "empty", self.opt_skip_empty, "exclude",
                       micro=True,
                       command=self._schedule_auto_refresh).pack(
            side="left", padx=(0, 0))

        qe_head = ctk.CTkFrame(exc, fg_color="transparent")
        qe_head.pack(fill="x", pady=(2, 0))
        ctk.CTkLabel(qe_head, text="Quick excludes:",
                     font=F_NANO, text_color=C_EXCLUDE_TEXT,
                     anchor="w").pack(side="left", padx=0)
        self.preset_count_label = ctk.CTkLabel(
            qe_head, text="(0)",
            font=F_NANO, text_color=C_MUTED, anchor="w")
        self.preset_count_label.pack(side="left", padx=(3, 0))

        self.preset_grid = ctk.CTkFrame(exc, fg_color="transparent")
        self.preset_grid.pack(fill="x")
        for c in range(3):
            self.preset_grid.grid_columnconfigure(c, weight=1)
        self._preset_chips: Dict[str, ctk.CTkButton] = {}
        for i, name in enumerate(EXCLUDE_PRESETS.keys()):
            r, c = divmod(i, 3)
            chip = ctk.CTkButton(
                self.preset_grid, text=name,
                height=22, corner_radius=11, width=0,
                font=F_NANO,
                fg_color="transparent",
                hover_color=C_EXCLUDE_HOVER,
                text_color=C_HINT,
                border_width=1,
                border_color=C_EXCLUDE_ACCENT,
                command=lambda n=name: self._toggle_preset(n),
            )
            chip.grid(row=r, column=c, padx=1, pady=1, sticky="ew")
            self._preset_chips[name] = chip

        ctk.CTkLabel(exc, text="Patterns:",
                     font=F_NANO, text_color=C_EXCLUDE_TEXT,
                     anchor="w").pack(fill="x", padx=0, pady=(2, 0))
        exc_r2 = ctk.CTkFrame(exc, fg_color="transparent")
        exc_r2.pack(fill="x")
        self.excl_pattern_var = tk.StringVar()
        ctk.CTkEntry(exc_r2, textvariable=self.excl_pattern_var,
                     placeholder_text=".min.js",
                     font=F_MICRO, height=24).pack(
            side="left", fill="x", expand=True, padx=(0, 2))
        ctk.CTkButton(exc_r2, text="+", width=24, font=F_SMALL, height=24,
                      fg_color=C_EXCLUDE_BOX, hover_color=C_EXCLUDE_HOVER,
                      border_width=1, border_color=C_EXCLUDE_ACCENT,
                      text_color=C_TICK,
                      command=self._add_exclude_pattern).pack(side="left", padx=0)
        ctk.CTkButton(exc_r2, text="✕", width=24, font=F_SMALL, height=24,
                      fg_color="transparent", border_width=1,
                      border_color=C_EXCLUDE_ACCENT, text_color=C_EXCLUDE_TEXT,
                      hover_color=C_EXCLUDE_HOVER,
                      command=self._clear_exclude_patterns).pack(side="left", padx=0)
        ctk.CTkButton(exc_r2, text="⟲", width=24, font=F_SMALL, height=24,
                      fg_color="transparent", border_width=1,
                      border_color=C_MUTED, text_color=C_MUTED,
                      hover_color=C_EXCLUDE_HOVER,
                      command=self._reset_all_excludes).pack(side="left", padx=(0, 0))

        self.excl_patterns_label = ctk.CTkLabel(exc, text="(none)",
                                                 font=F_NANO,
                                                 text_color=C_HINT,
                                                 anchor="w", wraplength=260,
                                                 justify="left")
        self.excl_patterns_label.pack(fill="x", padx=0, pady=0)

        # ----- OPTIONS -----
        opt_frame, opt = self._panel(self.io_wrap, "⚙", "OPTIONS",
                                     C_OPTION_ACCENT, C_OPTION_TEXT, small=True)
        self.opt_panel_frame = opt_frame

        # Row 1: transforms
        opt_r1 = ctk.CTkFrame(opt, fg_color="transparent")
        opt_r1.pack(fill="x", pady=0)
        self.opt_strip   = tk.BooleanVar(value=False)
        self.opt_secrets = tk.BooleanVar(value=True)
        self.opt_zip     = tk.BooleanVar(value=False)
        self._checkbox(opt_r1, "Strip", self.opt_strip, "option",
                       micro=True).pack(side="left", padx=(0, 6))
        self._checkbox(opt_r1, "Secrets", self.opt_secrets, "option",
                       micro=True).pack(side="left", padx=(0, 6))
        self._checkbox(opt_r1, "ZIP", self.opt_zip, "option",
                       micro=True).pack(side="left", padx=(0, 0))

        # Row 2: Split label + radios (no entry here)
        split_line = ctk.CTkFrame(opt, fg_color="transparent")
        split_line.pack(fill="x", pady=(1, 0))
        self._opt_split_line = split_line
        ctk.CTkLabel(split_line, text="Split:",
                     font=F_NANO, text_color=C_OPTION_TEXT).pack(side="left", padx=(0, 2))
        self.split_mode_var = tk.StringVar(value="single")
        self._radio(split_line, "Single", self.split_mode_var, "single",
                    "option", compact=True,
                    command=self._on_split_mode_change).pack(side="left", padx=0)
        self._radio(split_line, "Chars", self.split_mode_var, "chars",
                    "option", compact=True,
                    command=self._on_split_mode_change).pack(side="left", padx=2)
        self._radio(split_line, "Tokens", self.split_mode_var, "tokens",
                    "option", compact=True,
                    command=self._on_split_mode_change).pack(side="left", padx=0)

        # Row 2b: Max chars entry — own row, packed only for Chars/Tokens
        self.maxchars_box = ctk.CTkFrame(opt, fg_color="transparent")
        self.maxchars_var = tk.StringVar(value=str(DEFAULT_MAX_CHARS))
        self.maxchars_entry = ctk.CTkEntry(self.maxchars_box,
                                            textvariable=self.maxchars_var,
                                            width=100, font=F_MONO_XS, height=22)
        self.maxchars_entry.pack(side="left")
        self.maxchars_hint = ctk.CTkLabel(self.maxchars_box, text="",
                                           font=F_NANO, text_color=C_MUTED)
        self.maxchars_hint.pack(side="left", padx=(4, 0))

        # Row 3: Target label + dropdown + refresh
        target_line = ctk.CTkFrame(opt, fg_color="transparent")
        target_line.pack(fill="x", pady=(1, 0))
        self._opt_target_line = target_line
        ctk.CTkLabel(target_line, text="🎯 Target:",
                     font=F_NANO, text_color=C_OPTION_TEXT).pack(side="left", padx=(0, 2))
        self.model_var = tk.StringVar(value="— none —")
        self.model_dropdown = ctk.CTkOptionMenu(
            target_line, values=list(MODEL_CONTEXTS.keys()),
            variable=self.model_var, width=100, font=F_MICRO, height=22,
            fg_color=C_OPTION_BOX,
            button_color=C_OPTION_BOX,
            button_hover_color=C_OPTION_HOVER,
            text_color=C_TICK,
            command=self._on_model_change)
        self.model_dropdown.pack(side="left", fill="x", expand=True, padx=0)
        self.refresh_status = ctk.CTkLabel(target_line, text="",
                                            font=("Segoe UI", 9, "italic"),
                                            text_color=C_REFRESH,
                                            anchor="e", width=22)
        self.refresh_status.pack(side="right", padx=(2, 0))

        # Row 4: Output
        out_line = ctk.CTkFrame(opt, fg_color="transparent")
        out_line.pack(fill="x", pady=(1, 0))
        ctk.CTkLabel(out_line, text="Output:",
                     font=F_NANO, text_color=C_OPTION_TEXT).pack(side="left", padx=(0, 2))
        self.output_name_var = tk.StringVar(value="parse_report.txt")
        ctk.CTkEntry(out_line, textvariable=self.output_name_var,
                     font=F_MONO_XS, height=22).pack(
            side="left", fill="x", expand=True, padx=0)

        # Row 5: Template
        tmpl_line = ctk.CTkFrame(opt, fg_color="transparent")
        tmpl_line.pack(fill="x", pady=(1, 0))
        ctk.CTkLabel(tmpl_line, text="Template:",
                     font=F_NANO, text_color=C_OPTION_TEXT).pack(side="left", padx=(0, 2))
        self.template_var = tk.StringVar(value="Raw")
        self.template_dropdown = ctk.CTkOptionMenu(
            tmpl_line, values=list(self.templates.keys()),
            variable=self.template_var, width=100, font=F_MICRO, height=22,
            fg_color=C_OPTION_BOX,
            button_color=C_OPTION_BOX,
            button_hover_color=C_OPTION_HOVER,
            text_color=C_TICK)
        self.template_dropdown.pack(side="left", fill="x", expand=True, padx=(0, 2))
        ctk.CTkButton(tmpl_line, text="✎", width=22, font=F_SMALL, height=22,
                      fg_color="transparent", border_width=1,
                      border_color=C_OPTION_ACCENT, text_color=C_OPTION_TEXT,
                      hover_color=C_OPTION_HOVER,
                      command=self._edit_template_dialog).pack(side="left", padx=0)

        inc_frame.grid(row=0, column=0, padx=(0, 1), pady=0, sticky="nsew")
        exc_frame.grid(row=0, column=1, padx=1, pady=0, sticky="nsew")
        opt_frame.grid(row=0, column=2, padx=(1, 0), pady=0, sticky="nsew")

        self._refresh_preset_chips()

        # ===== PINNED STRIP =====
        self.pinned_strip = ctk.CTkFrame(setup, fg_color="transparent", height=30)
        self.pinned_strip.pack(fill="x", padx=2, pady=(2, 0))
        self.pinned_strip.pack_propagate(False)
        ctk.CTkLabel(self.pinned_strip, text="📌 Pinned:",
                     font=F_SMALL, text_color=C_PIN).pack(side="left", padx=(2, 3))
        self.pinned_chips_frame = ctk.CTkFrame(self.pinned_strip, fg_color="transparent")
        self.pinned_chips_frame.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(self.pinned_strip, text="+ Add", width=52,
                      font=F_MICRO, height=24,
                      fg_color="transparent", border_width=1,
                      border_color=C_PIN, text_color=C_PIN,
                      hover_color="#a16207",
                      command=self._add_pinned_via_dialog).pack(side="right", padx=(2, 2))
        self._refresh_pinned_strip()

        setup_footer = ctk.CTkFrame(setup)
        setup_footer.pack(fill="x", padx=2, pady=(3, 1))
        self.setup_status = ctk.CTkLabel(
            setup_footer,
            text="Choose a root and options, then discover files.",
            font=F_SMALL, text_color=C_MUTED, anchor="w")
        self.setup_status.pack(side="left", fill="x", expand=True, padx=6, pady=4)
        self.review_files_btn = ctk.CTkButton(
            setup_footer, text="Review files →", width=130, height=28,
            font=F_TITLE, state="disabled", command=lambda: self._show_scan_phase("review"))
        self.review_files_btn.pack(side="right", padx=4, pady=3)

        # ===== FILE TREE =====
        review_head = ctk.CTkFrame(review, fg_color="transparent")
        review_head.pack(fill="x", padx=2, pady=(0, 2))
        ctk.CTkButton(review_head, text="← Change setup", width=120,
                      height=27, font=F_SMALL,
                      command=lambda: self._show_scan_phase("setup")).pack(
            side="left", padx=2)
        self.review_summary = ctk.CTkLabel(
            review_head, text="Review the discovered files before creating the report.",
            font=F_SMALL, text_color=C_HINT)
        self.review_summary.pack(side="left", padx=8)

        list_frame = ctk.CTkFrame(review)
        list_frame.pack(fill="x", padx=2, pady=(2, 0))
        bar = ctk.CTkFrame(list_frame)
        bar.pack(fill="x", padx=2, pady=(2, 0))
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", self._on_filter_change)
        ctk.CTkLabel(bar, text="🔎 Filter:", font=F_BODY).pack(
            side="left", padx=(2, 2))
        ctk.CTkEntry(bar, textvariable=self.filter_var, width=180,
                     font=F_BODY, height=26).pack(side="left", padx=2)
        ctk.CTkButton(bar, text="☑ All", width=60, font=F_SMALL, height=26,
                      fg_color=C_INCLUDE_BOX, hover_color=C_INCLUDE_HOVER,
                      border_width=1, border_color=C_INCLUDE_ACCENT,
                      text_color=C_TICK,
                      command=lambda: self._set_all(True)).pack(side="left", padx=1)
        ctk.CTkButton(bar, text="☐ None", width=60, font=F_SMALL, height=26,
                      fg_color=C_EXCLUDE_BOX, hover_color=C_EXCLUDE_HOVER,
                      border_width=1, border_color=C_EXCLUDE_ACCENT,
                      text_color=C_TICK,
                      command=lambda: self._set_all(False)).pack(side="left", padx=1)
        ctk.CTkButton(bar, text="⇄ Invert", width=70, font=F_SMALL, height=26,
                      fg_color=C_OPTION_BOX, hover_color=C_OPTION_HOVER,
                      border_width=1, border_color=C_OPTION_ACCENT,
                      text_color=C_TICK,
                      command=self._invert).pack(side="left", padx=1)
        ctk.CTkButton(bar, text="▸ Expand", width=78, font=F_SMALL, height=26,
                      command=self._expand_all).pack(side="left", padx=1)
        ctk.CTkButton(bar, text="▾ Collapse", width=90, font=F_SMALL, height=26,
                      command=self._collapse_all).pack(side="left", padx=1)
        self.file_count_label = ctk.CTkLabel(bar, text="0 files", font=F_SMALL)
        self.file_count_label.pack(side="right", padx=6)

        ctk.CTkLabel(list_frame,
                     text="click = toggle · Ctrl+click = multi · Shift+click = range · right-click = menu",
                     font=F_TINY, text_color=C_MUTED).pack(
            anchor="w", padx=6, pady=(0, 0))

        tree_holder = ctk.CTkFrame(list_frame)
        tree_holder.pack(fill="x", padx=2, pady=(0, 2))
        style = ttk.Style()
        try: style.theme_use("clam")
        except tk.TclError: pass
        style.configure("Parse.Treeview",
                        background=C_TREE_BG, fieldbackground=C_TREE_BG,
                        foreground=C_TREE_FG, rowheight=28, borderwidth=0,
                        font=("Segoe UI", 12), indent=20)
        style.configure("Parse.Treeview.Heading",
                        background=C_TREE_HEAD_BG, foreground=C_TREE_HEAD_FG,
                        font=("Segoe UI", 12, "bold"))
        style.map("Parse.Treeview",
                  background=[("selected", C_TREE_SELECT_BG)],
                  foreground=[("selected", C_TREE_SELECT_FG)])
        self.tree = ttk.Treeview(tree_holder,
                                 columns=("size", "type"),
                                 show="tree headings",
                                 selectmode="browse",
                                 height=8,
                                 style="Parse.Treeview")
        self.tree.heading("#0",   text="Path", anchor="w")
        self.tree.heading("size", text="Size", anchor="e")
        self.tree.heading("type", text="Type", anchor="center")
        self.tree.column("#0",   width=700, anchor="w", stretch=True)
        self.tree.column("size", width=90, anchor="e", stretch=False)
        self.tree.column("type", width=74, anchor="center", stretch=False)
        vsb = ttk.Scrollbar(tree_holder, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<space>",  self._on_tree_space)
        self.tree.bind("<Button-3>", self._on_tree_rightclick)
        self.tree.bind("<Button-2>", self._on_tree_rightclick)

        # ===== RUN BAR =====
        runbar = ctk.CTkFrame(review)
        runbar.pack(fill="x", padx=2, pady=0)
        self.run_btn = ctk.CTkButton(runbar, text="▶ Create report", width=145,
                                     font=F_TITLE, height=30,
                                     fg_color=C_INCLUDE_BOX,
                                     hover_color=C_INCLUDE_HOVER,
                                     border_width=2, border_color=C_INCLUDE_ACCENT,
                                     text_color=C_TICK,
                                     command=self._run_scan)
        self.run_btn.pack(side="left", padx=(4, 5), pady=3)

        self.estimate_label = ctk.CTkLabel(
            runbar, text="", font=("Segoe UI", 13, "bold"),
            text_color=C_OK)
        self.estimate_label.pack(side="left", padx=(0, 6), pady=3)

        self.cancel_btn = ctk.CTkButton(runbar, text="✖ Cancel", width=90,
                                        font=F_BODY, height=30,
                                        state="disabled",
                                        fg_color=C_EXCLUDE_BOX,
                                        hover_color=C_EXCLUDE_HOVER,
                                        border_width=1, border_color=C_EXCLUDE_ACCENT,
                                        text_color=C_TICK,
                                        command=self._cancel_scan)
        self.cancel_btn.pack(side="left", padx=2)
        self.open_folder_btn = ctk.CTkButton(runbar, text="📂 Open folder",
                                             width=120, font=F_BODY, height=30,
                                             state="disabled",
                                             command=self._open_out_folder)
        self.open_folder_btn.pack(side="left", padx=2)
        self.copy_btn = ctk.CTkButton(runbar, text="📋 Copy (with template)",
                                      width=170, font=F_BODY, height=30,
                                      state="disabled",
                                      command=self._copy_report)
        self.copy_btn.pack(side="left", padx=2)

        # ===== PROGRESS =====
        prog_frame = ctk.CTkFrame(review, fg_color="transparent")
        prog_frame.pack(fill="x", padx=2, pady=0)
        self.progress = ctk.CTkProgressBar(prog_frame)
        self.progress.pack(fill="x", padx=4, pady=(2, 0))
        self.progress.set(0)
        self.status = ctk.CTkLabel(prog_frame, text="Idle.",
                                    anchor="w", font=F_SMALL)
        self.status.pack(fill="x", padx=6, pady=(0, 2))

        # ===== LOG =====
        self.log_visible = tk.BooleanVar(value=False)
        log_bar = ctk.CTkFrame(review, fg_color="transparent")
        log_bar.pack(fill="x", padx=2, pady=0)
        self._checkbox(log_bar, "Show log", self.log_visible, "option",
                       micro=True, command=self._toggle_log).pack(
            side="left", padx=4, pady=0)
        self.log_box = ctk.CTkTextbox(review, height=100, activate_scrollbars=True,
                                       font=F_MONO_SM)
        self.log_box.configure(state="disabled")

        self._apply_mode()
        self._on_split_mode_change()
        self._refresh_profile_chips()
        self._refresh_template_dropdown()
        self._populate_tree()
        self._show_scan_phase("setup")

    def _show_scan_phase(self, phase: str):
        self.scan_setup_view.pack_forget()
        self.scan_review_view.pack_forget()
        if phase == "review" and self._has_discovered:
            self.scan_review_view.pack(fill="both", expand=True)
            self.step_setup_label.configure(text_color=C_MUTED)
            self.step_review_label.configure(text_color=C_OPTION_TEXT)
            selected = sum(self.file_checked.get(d.rel_path, True)
                           for d in self.discovered)
            self.review_summary.configure(
                text=f"{selected} of {len(self.discovered)} files selected. Adjust the list, then run.")
        else:
            self.scan_setup_view.pack(fill="both", expand=True)
            self.step_setup_label.configure(text_color=C_OPTION_TEXT)
            self.step_review_label.configure(text_color=C_MUTED)

    # ------------------------------------------------------------------
    # AUTO-SUGGEST
    # ------------------------------------------------------------------
    def _check_auto_suggest(self):
        if not self.discovered:
            self._hide_suggestion(); return
        candidates = [d for d in self.discovered
                      if any(p in d.rel_path.lower() for p in MIN_BUNDLE_PATTERNS)]
        if not candidates:
            self._hide_suggestion(); return
        if "📦 Minified" in self.config.active_presets:
            self._hide_suggestion(); return
        manually_excluded = [c for c in candidates
                             if not any(p in c.rel_path.lower()
                                        for p in self.config.extra_exclude_patterns)]
        if not manually_excluded:
            self._hide_suggestion(); return
        self._pending_suggestion_count = len(manually_excluded)
        self.suggestion_label.configure(
            text=f"💡 Found {len(manually_excluded)} minified/bundled file(s). "
                 f"Exclude them?")
        if self._suggestion_hidden:
            self.suggestion_bar.pack(fill="x", padx=2, pady=(2, 0),
                                     before=self.io_wrap)
            self._suggestion_hidden = False

    def _hide_suggestion(self):
        if not self._suggestion_hidden:
            self.suggestion_bar.pack_forget()
            self._suggestion_hidden = True

    def _apply_auto_suggest(self):
        self.config.active_presets.add("📦 Minified")
        self._refresh_preset_chips()
        self._refresh_excl_patterns_label()
        self._hide_suggestion()
        self._log("Applied 'Minified' preset from suggestion.", "info")
        self._schedule_auto_refresh()

    def _dismiss_suggestion(self):
        self._hide_suggestion()

    # ------------------------------------------------------------------
    # PINNED FILES
    # ------------------------------------------------------------------
    def _refresh_pinned_strip(self):
        for w in self.pinned_chips_frame.winfo_children():
            w.destroy()
        pinned = sorted(self.config.pinned_files)
        if not pinned:
            ctk.CTkLabel(self.pinned_chips_frame,
                         text="(none — right-click a file to pin)",
                         font=F_TINY, text_color=C_MUTED).pack(
                side="left", padx=4)
            return
        for rel in pinned[:14]:
            name = rel.replace("\\", "/").split("/")[-1]
            chip = ctk.CTkFrame(self.pinned_chips_frame,
                                fg_color=C_PIN, corner_radius=10, height=22)
            chip.pack(side="left", padx=1, pady=1)
            ctk.CTkLabel(chip, text=name, font=F_SMALL,
                         text_color="#000000").pack(side="left", padx=(6, 2), pady=0)
            ctk.CTkButton(chip, text="×", width=20, height=20,
                          font=("Segoe UI", 12, "bold"),
                          fg_color="transparent", hover_color="#a16207",
                          text_color="#000000",
                          command=lambda r=rel: self._unpin_file(r)).pack(
                side="left", padx=(0, 3))
        if len(pinned) > 14:
            ctk.CTkLabel(self.pinned_chips_frame,
                         text=f"+{len(pinned)-14}",
                         font=F_TINY, text_color=C_MUTED).pack(
                side="left", padx=4)

    def _pin_file(self, rel_path: str):
        norm = rel_path.replace("\\", "/")
        self.config.pinned_files.add(norm)
        self._refresh_pinned_strip()
        self._schedule_auto_refresh()
        self._log(f"📌 Pinned: {norm}", "info")

    def _unpin_file(self, rel_path: str):
        norm = rel_path.replace("\\", "/")
        self.config.pinned_files.discard(norm)
        self._refresh_pinned_strip()
        self._schedule_auto_refresh()
        self._log(f"Unpinned: {norm}", "info")

    def _add_pinned_via_dialog(self):
        root = Path(self.root_var.get()).expanduser()
        if not root.is_dir():
            messagebox.showinfo(APP_NAME, "Set a project root first."); return
        path = filedialog.askopenfilename(initialdir=str(root), title="Pin a file")
        if not path: return
        try:
            rel = Path(path).resolve().relative_to(root.resolve())
        except ValueError:
            messagebox.showwarning(APP_NAME, "File is outside the project root.")
            return
        self._pin_file(str(rel))

    # ------------------------------------------------------------------
    # PROMPT TEMPLATES
    # ------------------------------------------------------------------
    def _load_templates(self) -> Dict[str, str]:
        if TEMPLATES_FILE.exists():
            try:
                data = json.loads(TEMPLATES_FILE.read_text(encoding='utf-8'))
                if isinstance(data, dict) and data:
                    data.setdefault("Raw", "{report}")
                    return data
            except Exception:
                pass
        try:
            TEMPLATES_FILE.write_text(
                json.dumps(DEFAULT_TEMPLATES, indent=2), encoding='utf-8')
        except Exception:
            pass
        return dict(DEFAULT_TEMPLATES)

    def _save_templates(self):
        try:
            TEMPLATES_FILE.write_text(
                json.dumps(self.templates, indent=2), encoding='utf-8')
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Save templates failed:\n{e}")

    def _refresh_template_dropdown(self):
        names = list(self.templates.keys())
        if not names:
            names = ["Raw"]; self.templates["Raw"] = "{report}"
        self.template_dropdown.configure(values=names)
        if self.template_var.get() not in names:
            self.template_var.set("Raw")

    def _wrap_report_with_template(self, report_text: str) -> str:
        name = self.template_var.get()
        tpl = self.templates.get(name, "{report}")
        if "{report}" in tpl:
            return tpl.replace("{report}", report_text)
        return tpl + "\n\n" + report_text

    def _edit_template_dialog(self):
        dlg = ctk.CTkToplevel(self)
        dlg.title("Manage prompt templates")
        dlg.geometry("720x560")
        dlg.transient(self); dlg.grab_set()

        top = ctk.CTkFrame(dlg, fg_color="transparent")
        top.pack(fill="x", padx=10, pady=(10, 4))
        ctk.CTkLabel(top, text="Template name:", font=F_BODY).pack(side="left", padx=(0, 6))
        name_var = tk.StringVar(value=self.template_var.get() or "Raw")
        ctk.CTkEntry(top, textvariable=name_var, width=220,
                     font=F_BODY).pack(side="left", padx=2)

        def on_select(choice):
            name_var.set(choice)
            body_box.delete("1.0", "end")
            body_box.insert("1.0", self.templates.get(choice, ""))
        ctk.CTkOptionMenu(top, values=list(self.templates.keys()),
                          font=F_SMALL, width=180,
                          command=on_select).pack(side="right", padx=2)

        ctk.CTkLabel(dlg, text="Template body (use {report} as placeholder):",
                     font=F_SMALL, anchor="w").pack(fill="x", padx=10, pady=(6, 2))
        body_box = ctk.CTkTextbox(dlg, font=F_MONO_SM)
        body_box.pack(fill="both", expand=True, padx=10, pady=4)
        body_box.insert("1.0", self.templates.get(name_var.get(), "{report}"))

        btns = ctk.CTkFrame(dlg, fg_color="transparent")
        btns.pack(fill="x", padx=10, pady=(2, 10))

        def save_current():
            n = name_var.get().strip()
            if not n: messagebox.showinfo(APP_NAME, "Enter a name."); return
            body = body_box.get("1.0", "end").rstrip("\n")
            if not body: messagebox.showinfo(APP_NAME, "Template body is empty."); return
            self.templates[n] = body
            self._save_templates(); self._refresh_template_dropdown()
            self.template_var.set(n)
            self._log(f"Saved template '{n}'", "info")

        def delete_current():
            n = name_var.get().strip()
            if n == "Raw":
                messagebox.showinfo(APP_NAME, "'Raw' cannot be deleted."); return
            if n not in self.templates: return
            if messagebox.askyesno(APP_NAME, f"Delete template '{n}'?"):
                del self.templates[n]
                self._save_templates(); self._refresh_template_dropdown()
                self.template_var.set("Raw"); name_var.set("Raw"); on_select("Raw")

        def new_template():
            name_var.set("New template")
            body_box.delete("1.0", "end")
            body_box.insert("1.0", "Analyze the following code:\n\n{report}\n\n")

        ctk.CTkButton(btns, text="💾 Save", width=90, font=F_BODY,
                      fg_color=C_INCLUDE_BOX, hover_color=C_INCLUDE_HOVER,
                      border_width=1, border_color=C_INCLUDE_ACCENT,
                      text_color=C_TICK,
                      command=save_current).pack(side="left", padx=(0, 6))
        ctk.CTkButton(btns, text="+ New", width=80, font=F_BODY,
                      command=new_template).pack(side="left", padx=2)
        ctk.CTkButton(btns, text="🗑 Delete", width=90, font=F_BODY,
                      fg_color=C_EXCLUDE_BOX, hover_color=C_EXCLUDE_HOVER,
                      border_width=1, border_color=C_EXCLUDE_ACCENT,
                      text_color=C_TICK,
                      command=delete_current).pack(side="left", padx=2)
        ctk.CTkButton(btns, text="Close", width=90, font=F_BODY,
                      command=dlg.destroy).pack(side="right", padx=2)

    # ------------------------------------------------------------------
    # TARGET MODEL / SPLIT
    # ------------------------------------------------------------------
    def _on_model_change(self, name: str):
        ctx = MODEL_CONTEXTS.get(name, 0)
        if name in ("— none —", "Custom"):
            self._schedule_estimate(); return
        self.split_mode_var.set("tokens")
        budget = int(ctx * MODEL_SAFETY_MARGIN)
        self.maxchars_var.set(str(budget))
        self._on_split_mode_change()
        self._schedule_estimate()

    def _on_split_mode_change(self):
        mode = self.split_mode_var.get()
        # Hide the entire maxchars row; re-pack below split radios when needed
        self.maxchars_box.pack_forget()
        if mode == "single":
            pass  # no entry needed
        elif mode == "chars":
            self.maxchars_entry.configure(state="normal")
            self.maxchars_hint.configure(text="chars per part · files kept whole")
            self.maxchars_box.pack(fill="x", pady=(1, 0),
                                   before=self._opt_target_line)
        elif mode == "tokens":
            self.maxchars_entry.configure(state="normal")
            self.maxchars_hint.configure(text="tokens per part · files kept whole")
            self.maxchars_box.pack(fill="x", pady=(1, 0),
                                   before=self._opt_target_line)
        self._schedule_auto_refresh()
        self._schedule_estimate()

    def _sync_split_mode_to_config(self):
        mode = self.split_mode_var.get()
        self.config.split_mode = mode
        self.config.target_model = self.model_var.get()
        try:
            n = int(self.maxchars_var.get())
            if n <= 0: n = DEFAULT_MAX_CHARS
        except (ValueError, TypeError):
            n = DEFAULT_MAX_CHARS
        if mode == "tokens":
            self.config.max_chars = int(n * TOKENS_TO_CHARS)
            self.config.model_budget = n
        else:
            self.config.max_chars = n
            self.config.model_budget = 0

    # ------------------------------------------------------------------
    # PRESET HANDLING
    # ------------------------------------------------------------------
    def _toggle_preset(self, name: str):
        if name in self.config.active_presets:
            self.config.active_presets.discard(name)
        else:
            self.config.active_presets.add(name)
        self._refresh_preset_chips()
        self._refresh_excl_patterns_label()
        self._schedule_auto_refresh()

    def _refresh_preset_chips(self):
        active_count = 0
        for name, chip in self._preset_chips.items():
            active = name in self.config.active_presets
            if active: active_count += 1
            chip.configure(
                fg_color=C_EXCLUDE_BOX if active else "transparent",
                hover_color=C_EXCLUDE_HOVER if active else C_MUTED,
                text_color=C_TICK if active else C_HINT,
                border_color=C_EXCLUDE_ACCENT if active else C_MUTED,
            )
        self.preset_count_label.configure(
            text=f"({active_count})",
            text_color=C_WARN if active_count else C_MUTED)

    def _reset_all_excludes(self):
        if not (self.config.extra_exclude_dirs or self.config.extra_exclude_paths or
                self.config.extra_exclude_patterns or self.config.active_presets):
            return
        if not messagebox.askyesno(APP_NAME,
                "Reset all custom excludes, folder exclusions, patterns, and quick-presets?"):
            return
        self.config.extra_exclude_dirs.clear()
        self.config.extra_exclude_paths.clear()
        self.config.extra_exclude_patterns.clear()
        self.config.active_presets.clear()
        self._refresh_preset_chips()
        self._refresh_excl_patterns_label()
        self._schedule_auto_refresh()
        self._log("All excludes reset.", "info")

    # ------------------------------------------------------------------
    # REVERSE TAB
    # ------------------------------------------------------------------
    def _build_reverse(self):
        tab = self.tab_reverse
        ctk.CTkLabel(tab,
                     text="Restore full-content files from a report folder. "
                          "Path/import-only entries are safely skipped.",
                     font=("Segoe UI", 14)).pack(anchor="w", padx=6, pady=(4, 2))

        row = ctk.CTkFrame(tab); row.pack(fill="x", padx=4, pady=2)
        ctk.CTkLabel(row, text="Report folder:", font=F_BODY).pack(side="left", padx=(4, 3))
        self.rev_src_var = tk.StringVar(value=str(Path.cwd()))
        ctk.CTkEntry(row, textvariable=self.rev_src_var, font=F_BODY, height=28).pack(
            side="left", fill="x", expand=True, padx=2)
        ctk.CTkButton(row, text="📁 Browse…", width=94, font=F_BODY, height=28,
                      command=lambda: self._pick_dir(self.rev_src_var)).pack(side="left", padx=2)

        row = ctk.CTkFrame(tab); row.pack(fill="x", padx=4, pady=2)
        ctk.CTkLabel(row, text="Target folder:", font=F_BODY).pack(side="left", padx=(4, 3))
        self.rev_dst_var = tk.StringVar(value=str(Path.home() / "ReConstructed"))
        ctk.CTkEntry(row, textvariable=self.rev_dst_var, font=F_BODY, height=28).pack(
            side="left", fill="x", expand=True, padx=2)
        ctk.CTkButton(row, text="📁 Browse…", width=94, font=F_BODY, height=28,
                      command=lambda: self._pick_dir(self.rev_dst_var)).pack(side="left", padx=2)

        opts = ctk.CTkFrame(tab); opts.pack(fill="x", padx=4, pady=2)
        self.rev_overwrite = tk.BooleanVar(value=False)
        self.rev_dry       = tk.BooleanVar(value=True)
        self._checkbox(opts, "Overwrite existing files",
                       self.rev_overwrite, "exclude", micro=True).pack(side="left", padx=8, pady=3)
        self._checkbox(opts, "Dry run (no writes)",
                       self.rev_dry, "option", micro=True).pack(side="left", padx=8, pady=3)

        ctk.CTkButton(tab, text="▶ Restore files", width=150, font=F_TITLE, height=30,
                      fg_color=C_INCLUDE_BOX, hover_color=C_INCLUDE_HOVER,
                      border_width=2, border_color=C_INCLUDE_ACCENT,
                      text_color=C_TICK,
                      command=self._run_reverse).pack(pady=6)
        self.rev_log = ctk.CTkTextbox(tab, height=280, font=F_MONO_SM)
        self.rev_log.pack(fill="both", expand=True, padx=4, pady=(2, 6))
        self.rev_log.configure(state="disabled")

    # ------------------------------------------------------------------
    # SETTINGS TAB
    # ------------------------------------------------------------------
    def _build_settings(self):
        tab = self.tab_settings
        ctk.CTkLabel(tab, text="Profiles",
                     font=("Segoe UI", 14, "bold")).pack(anchor="w", padx=6, pady=(4, 2))
        row = ctk.CTkFrame(tab); row.pack(fill="x", padx=4, pady=2)
        ctk.CTkLabel(row, text="Profile name:", font=F_BODY).pack(side="left", padx=(4, 3))
        self.profile_name_var = tk.StringVar()
        ctk.CTkEntry(row, textvariable=self.profile_name_var, width=200,
                     font=F_BODY, height=28).pack(side="left", padx=2)
        ctk.CTkButton(row, text="💾 Save current as profile", width=200,
                      font=F_BODY, height=28,
                      fg_color=C_INCLUDE_BOX, hover_color=C_INCLUDE_HOVER,
                      border_width=1, border_color=C_INCLUDE_ACCENT,
                      text_color=C_TICK,
                      command=self._save_profile).pack(side="left", padx=4)
        ctk.CTkButton(row, text="🗑 Delete", width=90, font=F_BODY, height=28,
                      fg_color=C_EXCLUDE_BOX, hover_color=C_EXCLUDE_HOVER,
                      border_width=1, border_color=C_EXCLUDE_ACCENT,
                      text_color=C_TICK,
                      command=self._delete_profile).pack(side="left", padx=2)

        ctk.CTkLabel(tab, text="Saved profiles:", font=F_BODY).pack(
            anchor="w", padx=6, pady=(4, 1))
        self.profile_list = ctk.CTkTextbox(tab, height=120, font=F_MONO_SM)
        self.profile_list.pack(fill="x", padx=4, pady=2)
        self.profile_list.configure(state="disabled")
        ctk.CTkButton(tab, text="📥 Load selected profile", width=200,
                      font=F_BODY, height=28,
                      fg_color=C_OPTION_BOX, hover_color=C_OPTION_HOVER,
                      border_width=1, border_color=C_OPTION_ACCENT,
                      text_color=C_TICK,
                      command=self._load_profile).pack(anchor="w", padx=4, pady=2)

        ctk.CTkLabel(tab, text="Theme",
                     font=("Segoe UI", 14, "bold")).pack(anchor="w", padx=6, pady=(8, 2))
        self.theme_var = tk.StringVar(value="System")
        ctk.CTkOptionMenu(tab, values=["System", "Light", "Dark"],
                          variable=self.theme_var, font=F_BODY,
                          fg_color=C_OPTION_BOX, button_color=C_OPTION_BOX,
                          button_hover_color=C_OPTION_HOVER, text_color=C_TICK,
                          command=lambda v: ctk.set_appearance_mode(v)).pack(anchor="w", padx=4)

        ctk.CTkLabel(tab, text="About",
                     font=("Segoe UI", 14, "bold")).pack(anchor="w", padx=6, pady=(8, 2))
        ctk.CTkLabel(tab, justify="left", font=F_SMALL,
                     text=f"Ultimate Parse\nProfiles: {PROFILE_DIR}\n"
                          f"Templates: {TEMPLATES_FILE.name}\n"
                          f"Python: {sys.version.split()[0]}\n"
                          f"Platform: {platform.system()}\n"
                          f"Palette: WCAG 2.1 AA").pack(anchor="w", padx=6)
        self._refresh_profile_list()

    # ------------------------------------------------------------------
    # PROFILE CHIPS
    # ------------------------------------------------------------------
    def _refresh_profile_chips(self):
        for w in self.profile_chips_frame.winfo_children():
            w.destroy()
        names = sorted(self.profiles)
        MAX_PER_ROW = 12
        row = 0; col = 0
        for name in names:
            if col >= MAX_PER_ROW:
                col = 0; row += 1
            chip = ctk.CTkButton(
                self.profile_chips_frame, text=name,
                height=24, corner_radius=12, width=96,
                font=F_MICRO,
                fg_color=C_OPTION_BOX, hover_color=C_OPTION_HOVER,
                border_width=1, border_color=C_OPTION_ACCENT,
                text_color=C_TICK,
                command=lambda n=name: self._load_profile(n),
            )
            chip.grid(row=row, column=col, padx=1, pady=1, sticky="w")
            col += 1
        if col >= MAX_PER_ROW:
            col = 0; row += 1
        if not names:
            ctk.CTkLabel(self.profile_chips_frame,
                         text="no saved profiles — create one in Settings",
                         text_color=C_MUTED, font=F_MICRO).grid(
                row=row, column=0, columnspan=2, padx=(0, 6), pady=1, sticky="w")
            col = 2
        manage = ctk.CTkButton(
            self.profile_chips_frame, text="⚙ Manage",
            height=24, corner_radius=12, width=96,
            font=F_MICRO,
            fg_color="transparent", border_width=1,
            border_color=C_MUTED, text_color=C_HINT,
            hover_color=C_OPTION_HOVER,
            command=lambda: self.tabs.set("Settings"),
        )
        manage.grid(row=row, column=col, padx=1, pady=1, sticky="w")

    # ------------------------------------------------------------------
    # AUTO-REFRESH
    # ------------------------------------------------------------------
    def _schedule_auto_refresh(self, *_):
        if not self._has_discovered:
            return
        now = time.monotonic()
        if self._auto_refresh_deadline is None:
            self._auto_refresh_deadline = now + (AUTO_REFRESH_MAX_WAIT_MS / 1000.0)
        if self._auto_refresh_id:
            try: self.after_cancel(self._auto_refresh_id)
            except Exception: pass
        remaining_ms = max(0, int((self._auto_refresh_deadline - now) * 1000))
        delay = min(AUTO_REFRESH_DEBOUNCE_MS, remaining_ms)
        if delay <= 0: delay = 1
        self.refresh_status.configure(text="⏳")
        self._auto_refresh_id = self.after(delay, self._auto_refresh)

    def _auto_refresh(self):
        self._auto_refresh_id = None
        self._auto_refresh_deadline = None
        root = Path(self.root_var.get()).expanduser()
        if not root.is_dir():
            self.refresh_status.configure(text=""); return
        self.refresh_status.configure(text="🔄")
        self._sync_config_from_ui()
        cfg = copy.deepcopy(self.config)
        prior = dict(self.file_checked)
        self._refresh_generation += 1
        generation = self._refresh_generation

        def worker():
            try:
                new = discover(cfg)
            except Exception as e:
                self.after(0, lambda: self._auto_refresh_done(
                    generation, None, None, str(e)))
                return
            self.after(0, lambda: self._auto_refresh_done(
                generation, new, prior, None))
        threading.Thread(target=worker, daemon=True).start()

    def _auto_refresh_done(self, generation, new, prior, err):
        if generation != self._refresh_generation:
            return
        if err:
            self.refresh_status.configure(text="⚠")
            self._log(f"Auto-refresh failed: {err}", "error")
            return
        self.discovered = new
        self.file_checked = {d.rel_path: prior.get(d.rel_path, True)
                             for d in self.discovered}
        self._populate_tree()
        self._update_estimate()
        self._check_auto_suggest()
        self.refresh_status.configure(text=f"✓ {len(new)}")
        self.setup_status.configure(text=f"Ready: {len(new)} files discovered.",
                                    text_color=C_OK)
        self.review_files_btn.configure(state="normal")

    # ------------------------------------------------------------------
    # ESTIMATE
    # ------------------------------------------------------------------
    def _schedule_estimate(self, *_):
        if self._estimate_after_id:
            try: self.after_cancel(self._estimate_after_id)
            except Exception: pass
        self._estimate_after_id = self.after(ESTIMATE_DEBOUNCE_MS, self._update_estimate)

    def _update_estimate(self):
        self._estimate_after_id = None
        if hasattr(self, 'review_summary'):
            selected_count = sum(self.file_checked.get(d.rel_path, True)
                                 for d in self.discovered)
            self.review_summary.configure(
                text=f"{selected_count} of {len(self.discovered)} files selected. "
                     "Adjust the list, then run.")
        if not self.discovered:
            self.estimate_label.configure(text=""); return
        selected = [d for d in self.discovered
                    if self.file_checked.get(d.rel_path, True)]
        if not selected:
            self.estimate_label.configure(text="no files selected",
                                          text_color=C_MUTED)
            return
        total_size = sum(d.size for d in selected)
        est_tokens = int(total_size / TOKENS_TO_CHARS)
        human = self._human_size(total_size)
        mode = self.split_mode_var.get()
        model_name = self.model_var.get()
        model_ctx = MODEL_CONTEXTS.get(model_name, 0)
        if mode == "single":
            if model_ctx > 0:
                budget = int(model_ctx * MODEL_SAFETY_MARGIN)
                over = est_tokens > budget
                self.estimate_label.configure(
                    text=f"1 file · ~{human} · ~{est_tokens:,} tok"
                         + (f"  · 🔴 OVER {model_name} budget" if over else
                            f"  · ✓ fits {model_name}"),
                    text_color=C_DANGER if over else C_OK)
            else:
                self.estimate_label.configure(
                    text=f"1 file · ~{human} · {len(selected)} file(s) · ~{est_tokens:,} tok",
                    text_color=C_OK)
            return
        try:
            n = int(self.maxchars_var.get())
            if n <= 0: n = DEFAULT_MAX_CHARS
        except (ValueError, TypeError):
            n = DEFAULT_MAX_CHARS
        if mode == "tokens":
            max_chars_per_part = int(n * TOKENS_TO_CHARS)
            unit = "token"
        else:
            max_chars_per_part = n
            unit = "char"
        estimated_chars = total_size + len(selected) * 80
        parts = max(1, (estimated_chars + max_chars_per_part - 1) // max_chars_per_part)
        if model_ctx > 0 and est_tokens > int(model_ctx * MODEL_SAFETY_MARGIN):
            color = C_DANGER; suffix = f" · 🔴 over {model_name}"
        else:
            color = C_OK if parts <= PART_COUNT_WARN else C_WARN
            suffix = f" · {unit} mode"
            if model_ctx > 0:
                suffix += f" · ✓ fits {model_name}"
        self.estimate_label.configure(
            text=f"~{parts} part(s) · ~{human} · {len(selected)} file(s) · "
                 f"~{est_tokens:,} tok{suffix}",
            text_color=color)

    # ------------------------------------------------------------------
    # Interactions
    # ------------------------------------------------------------------
    def _pick_root(self):
        d = filedialog.askdirectory(initialdir=self.root_var.get() or str(Path.cwd()))
        if d:
            self._discover_generation += 1
            self._refresh_generation += 1
            self.root_var.set(d)
            self.discovered = []; self.file_checked = {}
            self._has_discovered = False
            self.review_files_btn.configure(state="disabled")
            self.setup_status.configure(
                text="Root changed. Discover files to continue.", text_color=C_MUTED)
            self._populate_tree(); self._update_estimate()

    def _pick_dir(self, var):
        d = filedialog.askdirectory(initialdir=var.get() or str(Path.cwd()))
        if d: var.set(d)

    def _apply_mode(self):
        preset = MODE_PRESETS[self.mode_var.get()]
        self.config.mode = 'content'
        if self.mode_var.get() == "Paths only": self.config.mode = 'paths'
        elif self.mode_var.get() == "Imports only": self.config.mode = 'imports'
        self.opt_strip.set(self.mode_var.get() == "Comment-stripped")
        self.config.extensions = set(preset["extensions"])
        self.config.import_extensions = (
            set(preset["extensions"]) if self.config.mode == 'imports' else None)
        self.config.output_base = preset["output_base"]
        self.output_name_var.set(preset["output_base"])
        self.config.env_scope = preset["env_scope"]
        self._schedule_auto_refresh()

    def _discover(self):
        root = Path(self.root_var.get()).expanduser()
        if not root.is_dir():
            messagebox.showerror(APP_NAME, f"Not a folder:\n{root}"); return
        if self._auto_refresh_id:
            try: self.after_cancel(self._auto_refresh_id)
            except Exception: pass
            self._auto_refresh_id = None
        self._refresh_generation += 1
        self._sync_config_from_ui()
        cfg = copy.deepcopy(self.config)
        self._discover_generation += 1
        generation = self._discover_generation
        self.discover_btn.configure(state="disabled", text="Discovering…")
        self.review_files_btn.configure(state="disabled")
        self.setup_status.configure(text=f"Scanning {root}…", text_color=C_OPTION_TEXT)

        def worker():
            try:
                found = discover(cfg)
                error = None
            except Exception as exc:
                found, error = None, str(exc)
            self.after(0, lambda: self._discover_done(generation, root, found, error))
        threading.Thread(target=worker, daemon=True).start()

    def _discover_done(self, generation, root, found, error):
        if generation != self._discover_generation:
            return
        self.discover_btn.configure(state="normal", text="Discover files →")
        if error:
            self.setup_status.configure(text=f"Discovery failed: {error}",
                                        text_color=C_DANGER)
            messagebox.showerror(APP_NAME, f"Discovery failed:\n{error}")
            return
        self.discovered = found
        self.file_checked = {d.rel_path: True for d in found}
        self._has_discovered = True
        self._populate_tree(); self._update_estimate()
        self._check_auto_suggest()
        self.refresh_status.configure(text=f"✓ {len(self.discovered)}")
        self.setup_status.configure(
            text=f"Ready: {len(found)} files discovered. Opening review…",
            text_color=C_OK)
        self.review_files_btn.configure(state="normal")
        self._log(f"Found {len(self.discovered)} file(s) in {root}", "info")
        self._show_scan_phase("review")

    def _sync_config_from_ui(self):
        self.config.root_dir = Path(self.root_var.get()).expanduser()
        preset = MODE_PRESETS.get(self.mode_var.get())
        if preset:
            self.config.mode = 'content'
            if self.mode_var.get() == "Paths only":   self.config.mode = 'paths'
            elif self.mode_var.get() == "Imports only": self.config.mode = 'imports'
            self.config.extensions = set(preset["extensions"])
            self.config.import_extensions = (
                set(preset["extensions"]) if self.config.mode == 'imports' else None)
            self.config.env_scope = preset["env_scope"]
        self.config.include_env       = self.opt_env.get()
        self.config.include_dotfiles  = self.opt_dotfiles.get()
        self.config.include_bootstrap = not self.opt_bootstrap_exclude.get()
        self.config.skip_binary       = self.opt_skip_binary.get()
        self.config.skip_empty        = self.opt_skip_empty.get()
        self.config.strip_comments    = self.opt_strip.get()
        self.config.scan_secrets      = self.opt_secrets.get()
        self.config.create_zip        = self.opt_zip.get()
        self.config.prompt_template   = self.template_var.get()
        out_name = self.output_name_var.get().strip() or "parse_report.txt"
        if '.' not in out_name: out_name += ".txt"
        self.config.output_base = out_name
        extras: Set[str] = set()
        for label, (var, exts) in self._extra_type_vars.items():
            if var.get(): extras |= exts
        self.config.extra_extensions = extras
        self._sync_split_mode_to_config()

    def _add_exclude_pattern(self):
        pat = self.excl_pattern_var.get().strip()
        if not pat: return
        self.config.extra_exclude_patterns.add(pat)
        self.excl_pattern_var.set("")
        self._refresh_excl_patterns_label()
        self._schedule_auto_refresh()

    def _clear_exclude_patterns(self):
        if not self.config.extra_exclude_patterns: return
        self.config.extra_exclude_patterns.clear()
        self._refresh_excl_patterns_label()
        self._schedule_auto_refresh()

    def _refresh_excl_patterns_label(self):
        pats = sorted(self.config.extra_exclude_patterns)
        presets = sorted(self.config.active_presets)
        pins = len(self.config.pinned_files)
        parts = []
        if presets: parts.append(f"✓ {len(presets)} preset(s)")
        if pats:
            disp = " ".join(f"· {p}" for p in pats[:3])
            if len(pats) > 3: disp += f" +{len(pats)-3}"
            parts.append(disp)
        if pins: parts.append(f"📌 {pins}")
        if not parts: self.excl_patterns_label.configure(text="(none)")
        else: self.excl_patterns_label.configure(text=" | ".join(parts))

    # ------------------------------------------------------------------
    # Tree helpers
    # ------------------------------------------------------------------
    def _rel_parts(self, rel_path: str):
        return rel_path.replace("\\", "/").split("/")

    def _iter_descendant_files(self, folder_iid: str):
        stack = [folder_iid]
        while stack:
            item = stack.pop()
            for child in self.tree.get_children(item):
                if child.startswith("F|"): yield child[2:]
                else: stack.append(child)

    def _all_visible_rows(self):
        result = []
        def walk(iid):
            result.append(iid)
            for c in self.tree.get_children(iid):
                walk(c)
        for top in self.tree.get_children(""):
            walk(top)
        return result

    def _row_state(self, iid):
        if iid.startswith("F|"):
            return self.file_checked.get(iid[2:], True)
        if iid.startswith("D|"):
            files = list(self._iter_descendant_files(iid))
            if not files: return False
            return all(self.file_checked.get(f, True) for f in files)
        return False

    def _update_folder_symbol(self, folder_iid: str):
        files = list(self._iter_descendant_files(folder_iid))
        if not files: symbol = "☐"
        else:
            checked = sum(1 for f in files if self.file_checked.get(f, True))
            if checked == 0:            symbol = "☐"
            elif checked == len(files): symbol = "☑"
            else:                       symbol = "◪"
        name = self._rel_parts(folder_iid[2:])[-1]
        try: self.tree.item(folder_iid, text=f"{symbol} {name}")
        except tk.TclError: pass

    def _update_ancestor_symbols(self, rel_path: str):
        parts = self._rel_parts(rel_path)
        for i in range(1, len(parts)):
            anc = f"D|{'/'.join(parts[:i])}"
            if self.tree.exists(anc): self._update_folder_symbol(anc)

    def _toggle_file(self, rel_path: str, silent_estimate: bool = False):
        new_state = not self.file_checked.get(rel_path, True)
        self.file_checked[rel_path] = new_state
        iid = f"F|{rel_path}"
        if self.tree.exists(iid):
            name = self._rel_parts(rel_path)[-1]
            pin = "📌 " if rel_path.replace("\\", "/") in {
                p.replace("\\", "/") for p in self.config.pinned_files} else ""
            try: self.tree.item(iid, text=f"{'☑' if new_state else '☐'} {pin}{name}")
            except tk.TclError: pass
        self._update_ancestor_symbols(rel_path)
        if not silent_estimate: self._schedule_estimate()

    def _toggle_folder(self, folder_iid: str, silent_estimate: bool = False):
        files = list(self._iter_descendant_files(folder_iid))
        if not files: return
        new_state = not all(self.file_checked.get(f, True) for f in files)
        for f in files:
            self.file_checked[f] = new_state
            iid = f"F|{f}"
            if self.tree.exists(iid):
                name = self._rel_parts(f)[-1]
                pin = "📌 " if f.replace("\\", "/") in {
                    p.replace("\\", "/") for p in self.config.pinned_files} else ""
                try: self.tree.item(iid, text=f"{'☑' if new_state else '☐'} {pin}{name}")
                except tk.TclError: pass
        self._update_folder_symbol(folder_iid)
        self._update_ancestor_symbols(folder_iid[2:])
        if not silent_estimate: self._schedule_estimate()

    def _toggle_range(self, anchor_iid: str, end_iid: str):
        rows = self._all_visible_rows()
        try:
            a = rows.index(anchor_iid); b = rows.index(end_iid)
        except ValueError:
            return
        lo, hi = min(a, b), max(a, b)
        target = not self._row_state(anchor_iid)
        range_set = set(rows[lo:hi+1])
        pinned_norm = {p.replace("\\", "/") for p in self.config.pinned_files}
        for iid in rows[lo:hi+1]:
            if iid.startswith("F|"):
                path = iid[2:]
                self.file_checked[path] = target
                symbol = "☑" if target else "☐"
                name = self._rel_parts(path)[-1]
                pin = "📌 " if path.replace("\\", "/") in pinned_norm else ""
                try: self.tree.item(iid, text=f"{symbol} {pin}{name}")
                except tk.TclError: pass
        for iid in reversed(rows[lo:hi+1]):
            if iid.startswith("D|"):
                self._update_folder_symbol(iid)
        ancestors = set()
        for iid in rows[lo:hi+1]:
            if iid.startswith("F|"):
                parts = self._rel_parts(iid[2:])
                for i in range(1, len(parts)):
                    anc = f"D|{'/'.join(parts[:i])}"
                    if anc not in range_set:
                        ancestors.add(anc)
        for anc in ancestors:
            if self.tree.exists(anc):
                self._update_folder_symbol(anc)
        self._schedule_estimate()

    def _collect_open_state(self, iid: str, state: dict):
        try: state[iid] = bool(self.tree.item(iid, "open"))
        except tk.TclError: return
        for child in self.tree.get_children(iid):
            self._collect_open_state(child, state)

    def _expand_all(self):
        for iid in self.tree.get_children(""): self._set_open_recursive(iid, True)

    def _collapse_all(self):
        for iid in self.tree.get_children(""): self._set_open_recursive(iid, False)

    def _set_open_recursive(self, iid: str, is_open: bool):
        if iid.startswith("D|"):
            try: self.tree.item(iid, open=is_open)
            except tk.TclError: return
        for child in self.tree.get_children(iid):
            self._set_open_recursive(child, is_open)

    def _on_filter_change(self, *_):
        if self._filter_after_id:
            try: self.after_cancel(self._filter_after_id)
            except Exception: pass
        self._filter_after_id = self.after(200, self._populate_tree)

    def _on_tree_space(self, event):
        iid = self.tree.focus()
        if not iid: return
        if iid == "__placeholder__": return
        if iid.startswith("F|"):   self._toggle_file(iid[2:])
        elif iid.startswith("D|"): self._toggle_folder(iid)
        self._tree_anchor = iid
        return "break"

    def _on_tree_rightclick(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid or iid == "__placeholder__": return
        menu = tk.Menu(self, tearoff=0)
        pinned_norm = {p.replace("\\", "/") for p in self.config.pinned_files}
        if iid.startswith("F|"):
            path = iid[2:]
            name = self._rel_parts(path)[-1]
            menu.add_command(label=f"Toggle  {name}",
                             command=lambda: self._toggle_file(path))
            menu.add_separator()
            is_pinned = path.replace("\\", "/") in pinned_norm
            if is_pinned:
                menu.add_command(label="📌 Unpin this file",
                                 command=lambda: self._unpin_file(path))
            else:
                menu.add_command(label="📌 Pin this file (always included)",
                                 command=lambda: self._pin_file(path))
        elif iid.startswith("D|"):
            folder_path = iid[2:]
            name = self._rel_parts(folder_path)[-1]
            menu.add_command(label=f"Toggle  {name}",
                             command=lambda: self._toggle_folder(iid))
            menu.add_separator()
            menu.add_command(label=f"Exclude folder  '{name}'",
                             command=lambda: self._exclude_folder(folder_path))
            menu.add_command(label=f"Include ONLY  '{name}'",
                             command=lambda: self._select_only_folder(folder_path))
            menu.add_separator()
            menu.add_command(label="Expand all under this",
                             command=lambda: self._set_open_recursive(iid, True))
            menu.add_command(label="Collapse all under this",
                             command=lambda: self._set_open_recursive(iid, False))
        try: menu.tk_popup(event.x_root, event.y_root)
        finally: menu.grab_release()

    def _exclude_folder(self, folder_rel_path: str):
        norm = folder_rel_path.replace("\\", "/")
        self.config.extra_exclude_paths.add(norm)
        self._log(f"Excluded folder: {norm}", "info")
        self._schedule_auto_refresh()

    def _select_only_folder(self, folder_rel_path: str):
        norm = folder_rel_path.replace("\\", "/").rstrip("/")
        prefix = norm + "/"
        for d in self.discovered:
            p = d.rel_path.replace("\\", "/")
            self.file_checked[d.rel_path] = (p == norm or p.startswith(prefix))
        self._populate_tree(); self._schedule_estimate()

    def _populate_tree(self):
        open_state: dict = {}
        for iid in self.tree.get_children(""):
            self._collect_open_state(iid, open_state)
        self.tree.delete(*self.tree.get_children())
        f = self.filter_var.get().lower()
        by_rel = {d.rel_path: d for d in self.discovered}
        matching = [p for p in by_rel if not f or f in p.lower()]
        pinned_norm = {p.replace("\\", "/") for p in self.config.pinned_files}
        if not matching:
            if not self._has_discovered:
                text = "→ click 🔍 Discover above to scan the project"
            elif f:
                text = f"→ no files match filter '{self.filter_var.get()}'"
            else:
                text = "→ no files matched (adjust mode / includes / excludes)"
            self.tree.insert("", "end", iid="__placeholder__",
                             text=text, values=("", ""), tags=("placeholder",))
            self.tree.tag_configure("placeholder", foreground=C_TREE_PLACEHOLDER,
                                    font=("Segoe UI", 12, "italic"))
            self.file_count_label.configure(text=f"0 of {len(self.discovered)} files")
            return
        needed_folders = set()
        for p in matching:
            parts = self._rel_parts(p)
            for i in range(1, len(parts)):
                needed_folders.add("/".join(parts[:i]))
        children_of: dict = defaultdict(list)
        for folder in needed_folders:
            parent = folder.rsplit("/", 1)[0] if "/" in folder else ""
            children_of[parent].append(("D", folder))
        for p in matching:
            parent = p.rsplit("/", 1)[0] if "/" in p else ""
            children_of[parent].append(("F", p))
        for parent in children_of:
            children_of[parent].sort(key=lambda x: (x[0] != "D", x[1].lower()))

        def insert_children(parent_iid: str, parent_path: str):
            for kind, path in children_of.get(parent_path, []):
                name = self._rel_parts(path)[-1]
                if kind == "D":
                    iid = f"D|{path}"
                    is_open = open_state.get(iid, True)
                    self.tree.insert(parent_iid, "end", iid=iid,
                                     text=f"☐ {name}", open=is_open)
                    insert_children(iid, path)
                    self._update_folder_symbol(iid)
                else:
                    item = by_rel.get(path)
                    if item is None: continue
                    iid = f"F|{path}"
                    symbol = "☑" if self.file_checked.get(path, True) else "☐"
                    pin = "📌 " if path.replace("\\", "/") in pinned_norm else ""
                    self.tree.insert(parent_iid, "end", iid=iid,
                                     text=f"{symbol} {pin}{name}",
                                     values=(self._human_size(item.size),
                                             item.ext or "—"))
        insert_children("", "")
        self.file_count_label.configure(text=f"{len(matching)} of {len(self.discovered)} files")
        self._update_estimate()

    def _on_tree_click(self, event):
        region = self.tree.identify_region(event.x, event.y)
        if region not in ("tree", "cell"): return
        iid = self.tree.identify_row(event.y)
        if not iid or iid == "__placeholder__": return
        col = self.tree.identify_column(event.x)
        try:
            elem = self.tree.identify_element(event.x, event.y)
            if elem and "indicator" in str(elem).lower(): return
        except Exception:
            if iid.startswith("D|") and col == "#0":
                bbox = self.tree.bbox(iid, "#0")
                if bbox and event.x < bbox[0] + 20: return
        if col != "#0": return
        shift = (event.state & 0x0001) != 0
        if shift and self._tree_anchor is not None and self.tree.exists(self._tree_anchor):
            self._toggle_range(self._tree_anchor, iid)
        else:
            if iid.startswith("F|"):   self._toggle_file(iid[2:])
            elif iid.startswith("D|"): self._toggle_folder(iid)
            self._tree_anchor = iid

    def _set_all(self, state):
        f = self.filter_var.get().lower()
        for d in self.discovered:
            if not f or f in d.rel_path.lower():
                self.file_checked[d.rel_path] = state
        self._populate_tree(); self._schedule_estimate()

    def _invert(self):
        f = self.filter_var.get().lower()
        for d in self.discovered:
            if not f or f in d.rel_path.lower():
                self.file_checked[d.rel_path] = not self.file_checked.get(d.rel_path, True)
        self._populate_tree(); self._schedule_estimate()

    # ------------------------------------------------------------------
    # Run scan
    # ------------------------------------------------------------------
    def _run_scan(self):
        if not self.discovered:
            messagebox.showinfo(APP_NAME, "Run 'Discover' first."); return
        self._sync_config_from_ui()
        selected = [d.abs_path for d in self.discovered
                    if self.file_checked.get(d.rel_path, True)]
        if not selected:
            messagebox.showinfo(APP_NAME, "No files selected."); return
        self.cancel_event.clear()
        self.run_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.open_folder_btn.configure(state="disabled")
        self.copy_btn.configure(state="disabled")
        self.progress.set(0)
        self._log("Starting scan…", "info")
        cfg = copy.deepcopy(self.config)
        threading.Thread(target=self._scan_worker, args=(cfg, selected,), daemon=True).start()

    def _scan_worker(self, config, files):
        def prog(cur, total, msg=""):
            self.after(0, lambda: (self.progress.set(cur / max(total, 1)),
                                    self.status.configure(text=msg or f"{cur}/{total}")))
        def log(msg, level="info"):
            self.after(0, lambda: self._log(msg, level))
        result = run_parse(config, files, progress_cb=prog, log_cb=log,
                           cancel=self.cancel_event)
        self.after(0, lambda: self._scan_done(result))

    def _scan_done(self, result):
        self.last_result = result
        self.run_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        if result.error:
            self._log(f"❌ {result.error}", "error")
            self.status.configure(text=f"Failed: {result.error}")
            messagebox.showerror(APP_NAME, result.error); return
        self.progress.set(1)
        self.status.configure(
            text=f"Done. {len(result.parts)} part(s), "
                 f"{self._human_size(result.total_bytes)}, {result.elapsed:.1f}s")
        self.open_folder_btn.configure(state="normal")
        self.copy_btn.configure(state="normal")
        messagebox.showinfo(APP_NAME,
                            f"Scan complete.\n\nFiles: {result.files_included}\n"
                            f"Parts: {len(result.parts)}\n"
                            f"Size: {self._human_size(result.total_bytes)}\n"
                            f"Time: {result.elapsed:.1f}s")

    def _cancel_scan(self):
        self.cancel_event.set()
        self._log("Cancellation requested…", "warn")

    def _open_out_folder(self):
        if not self.last_result or not self.last_result.parts: return
        folder = self.last_result.parts[0].parent
        try:
            if platform.system() == "Windows": os.startfile(folder)  # noqa
            elif platform.system() == "Darwin": subprocess.Popen(["open", str(folder)])
            else: subprocess.Popen(["xdg-open", str(folder)])
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Could not open folder:\n{e}")

    def _copy_report(self):
        if not self.last_result or not self.last_result.parts: return
        try:
            report = "".join(p.read_text(encoding='utf-8')
                             for p in self.last_result.parts)
            wrapped = self._wrap_report_with_template(report)
            self.clipboard_clear(); self.clipboard_append(wrapped)
            tname = self.template_var.get()
            self._log(f"Copied {len(wrapped):,} chars (template: {tname})", "info")
            messagebox.showinfo(APP_NAME,
                                f"Copied {len(wrapped):,} chars.\nTemplate: {tname}")
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Copy failed:\n{e}")

    # ------------------------------------------------------------------
    # Reverse parse
    # ------------------------------------------------------------------
    def _run_reverse(self):
        src = Path(self.rev_src_var.get()).expanduser()
        if not src.is_dir():
            messagebox.showerror(APP_NAME, f"Not a folder:\n{src}"); return
        dst = Path(self.rev_dst_var.get()).expanduser()
        self._rev_log_clear()
        threading.Thread(target=self._reverse_worker,
                         args=(src, dst, self.rev_dry.get(), self.rev_overwrite.get()),
                         daemon=True).start()

    def _reverse_worker(self, src, dst, dry, overwrite):
        def log(msg): self.after(0, lambda: self._rev_log(msg))
        groups = _discover_report_groups(src)
        if not groups:
            log(f"❌ No report files found in {src}"); return
        dst_root = dst.resolve()
        for (base, suffix), parts in sorted(groups.items()):
            # When both a single report and numbered parts exist, prefer the
            # numbered run. Output cleanup normally prevents this, but report
            # folders copied from older versions may still contain both.
            if any(number > 0 for number, _ in parts):
                parts = [(number, path) for number, path in parts if number > 0]
            parts.sort(key=lambda x: x[0])
            numbers = [number for number, _ in parts if number > 0]
            if numbers and numbers != list(range(1, max(numbers) + 1)):
                log(f"\n❌ '{base}{suffix}' has missing report parts; skipped")
                continue
            log(f"\n📂 Reconstructing '{base}{suffix}' ({len(parts)} report file(s))")
            entries: List[Dict[str, object]] = []
            legacy = False
            for _, report_path in parts:
                try:
                    report_text = _read_report_text(report_path)
                    version = _report_version(report_text)
                    if version >= REPORT_FORMAT_VERSION:
                        entries.extend(_decode_v2_entries(report_text))
                    else:
                        legacy = True
                        entries.extend(_decode_legacy_entries(report_text))
                except Exception as e:
                    log(f"  ❌ {report_path.name}: {e}")
            if legacy:
                log("  ⚠ Legacy report: exact trailing whitespace cannot be guaranteed")
            seen: Set[str] = set()
            for entry in entries:
                rel = str(entry.get('path', ''))
                status = str(entry.get('status', 'content'))
                if status != 'content':
                    detail = entry.get('message') or status
                    log(f"  ⏭ {rel}: {detail}")
                    continue
                norm = _normalise_rel_path(rel)
                if norm in seen:
                    log(f"  ⚠ duplicate entry skipped: {norm}")
                    continue
                seen.add(norm)
                dest = _safe_reverse_destination(dst_root, norm)
                if dest is None:
                    log(f"  ⚠ unsafe path: {norm}"); continue
                if dry:
                    exists = dest.exists()
                    action = ("OVERWRITE" if (exists and overwrite)
                              else "SKIP (exists)" if exists else "WRITE")
                    log(f"  [DRY] {action}: {norm}")
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                # Re-resolve after directory creation so existing symlinks in
                # the target tree cannot redirect the write outside dst.
                dest = _safe_reverse_destination(dst_root, norm)
                if dest is None:
                    log(f"  ⚠ unsafe path after directory creation: {norm}")
                    continue
                if dest.exists() and not overwrite:
                    log(f"  ⏭ skip (exists): {norm}"); continue
                try:
                    _write_reconstructed_text(
                        dest, str(entry.get('content', '')),
                        str(entry.get('encoding', 'utf-8')))
                    log(f"  ✅ {norm}")
                except Exception as e:
                    log(f"  ❌ {norm}: {e}")

    # ------------------------------------------------------------------
    # Profiles
    # ------------------------------------------------------------------
    def _load_profiles(self):
        if PROFILE_FILE.exists():
            try: return json.loads(PROFILE_FILE.read_text(encoding='utf-8'))
            except Exception: return {}
        return {}

    def _save_profiles(self):
        try:
            PROFILE_FILE.write_text(json.dumps(self.profiles, indent=2), encoding='utf-8')
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Save failed:\n{e}")

    def _save_profile(self):
        name = self.profile_name_var.get().strip()
        if not name:
            messagebox.showinfo(APP_NAME, "Enter a name first."); return
        self._sync_config_from_ui()
        extras_enabled = {label for label, (var, _) in self._extra_type_vars.items() if var.get()}
        self.profiles[name] = {
            "mode_label": self.mode_var.get(),
            "split_mode": self.split_mode_var.get(),
            "target_model": self.model_var.get(),
            "prompt_template": self.template_var.get(),
            "extra_types_enabled": sorted(extras_enabled),
            "output_base": self.config.output_base,
            "max_chars": self.config.max_chars,
            "max_chars_input": self.maxchars_var.get(),
            "include_env": self.config.include_env,
            "include_bootstrap": self.config.include_bootstrap,
            "include_dotfiles": self.config.include_dotfiles,
            "strip_comments": self.config.strip_comments,
            "scan_secrets": self.config.scan_secrets,
            "env_scope": self.config.env_scope,
            "create_zip": self.config.create_zip,
            "skip_binary": self.config.skip_binary,
            "skip_empty": self.config.skip_empty,
            "extra_exclude_patterns": sorted(self.config.extra_exclude_patterns),
            "extra_exclude_paths": sorted(self.config.extra_exclude_paths),
            "active_presets": sorted(self.config.active_presets),
            "pinned_files": sorted(self.config.pinned_files),
        }
        self._save_profiles()
        self._refresh_profile_list()
        self._refresh_profile_chips()
        messagebox.showinfo(APP_NAME, f"Saved profile '{name}'.")

    def _load_profile(self, name=None):
        if name is None:
            try: sel = self.profile_list.get("sel.first", "sel.last").strip()
            except tk.TclError:
                content = self.profile_list.get("1.0", "end").strip().splitlines()
                sel = content[-1].strip() if content else ""
        else:
            sel = name
        if not sel or sel not in self.profiles:
            messagebox.showinfo(APP_NAME, "Profile not found."); return
        p = self.profiles[sel]
        if p.get("mode_label") in MODE_PRESETS:
            self.mode_var.set(p["mode_label"])
            preset = MODE_PRESETS[p["mode_label"]]
            self.config.extensions = set(preset["extensions"])
            self.config.env_scope = preset["env_scope"]
        if p.get("split_mode") in ("single", "chars", "tokens"):
            self.split_mode_var.set(p["split_mode"])
        if p.get("target_model") in MODEL_CONTEXTS:
            self.model_var.set(p["target_model"])
        if p.get("prompt_template") in self.templates:
            self.template_var.set(p["prompt_template"])
        self.config.output_base = p.get("output_base", "parse_report.txt")
        self.output_name_var.set(self.config.output_base)
        self.config.max_chars = p.get("max_chars", DEFAULT_MAX_CHARS)
        self.maxchars_var.set(str(p.get("max_chars_input", DEFAULT_MAX_CHARS)))
        self.opt_env.set(p.get("include_env", True))
        self.opt_bootstrap_exclude.set(not p.get("include_bootstrap", False))
        self.opt_dotfiles.set(p.get("include_dotfiles", False))
        self.opt_strip.set(p.get("strip_comments", False))
        self.opt_secrets.set(p.get("scan_secrets", True))
        self.opt_zip.set(p.get("create_zip", False))
        self.opt_skip_binary.set(p.get("skip_binary", True))
        self.opt_skip_empty.set(p.get("skip_empty", True))
        self.config.env_scope = p.get("env_scope")
        self.config.extra_exclude_patterns = set(p.get("extra_exclude_patterns", []))
        self.config.extra_exclude_paths = set(p.get("extra_exclude_paths", []))
        self.config.active_presets = set(p.get("active_presets", []))
        self.config.pinned_files = set(p.get("pinned_files", []))
        self._refresh_excl_patterns_label()
        self._refresh_preset_chips()
        self._refresh_pinned_strip()
        enabled = set(p.get("extra_types_enabled", []))
        for label, (var, _) in self._extra_type_vars.items():
            var.set(label in enabled)
        self.profile_name_var.set(sel)
        self._on_split_mode_change()
        self._schedule_auto_refresh()
        self._update_estimate()
        messagebox.showinfo(APP_NAME, f"Loaded profile '{sel}'.")

    def _delete_profile(self):
        name = self.profile_name_var.get().strip()
        if name not in self.profiles:
            messagebox.showinfo(APP_NAME, "No such profile."); return
        if messagebox.askyesno(APP_NAME, f"Delete profile '{name}'?"):
            del self.profiles[name]
            self._save_profiles()
            self._refresh_profile_list()
            self._refresh_profile_chips()

    def _refresh_profile_list(self):
        self.profile_list.configure(state="normal")
        self.profile_list.delete("1.0", "end")
        for name in sorted(self.profiles):
            self.profile_list.insert("end", name + "\n")
        self.profile_list.configure(state="disabled")

    # ------------------------------------------------------------------
    # Log
    # ------------------------------------------------------------------
    def _log(self, msg, level="info"):
        prefix = {"info": "·", "warn": "⚠", "error": "✗"}.get(level, "·")
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {prefix} {msg}\n"
        self.log_box.configure(state="normal")
        self.log_box.insert("end", line); self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _toggle_log(self):
        if self.log_visible.get():
            self.log_box.pack(fill="x", padx=2, pady=(1, 3))
        else:
            self.log_box.pack_forget()

    def _rev_log(self, msg):
        self.rev_log.configure(state="normal")
        self.rev_log.insert("end", msg + "\n"); self.rev_log.see("end")
        self.rev_log.configure(state="disabled")

    def _rev_log_clear(self):
        self.rev_log.configure(state="normal")
        self.rev_log.delete("1.0", "end")
        self.rev_log.configure(state="disabled")

    @staticmethod
    def _human_size(n):
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024: return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
            n /= 1024
        return f"{n:.1f} TB"

# =====================================================================
#                              ENTRY
# =====================================================================

def main():
    session = os.environ.get("XDG_SESSION_TYPE", "")
    print(f"[Ultimate Parse] XDG_SESSION_TYPE = {session or '(unset)'}")
    if session == "wayland":
        print("[Ultimate Parse] Running under Wayland (via XWayland).")
        try:
            r = subprocess.run(["gsettings", "get", "org.gnome.mutter", "edge-tiling"],
                               capture_output=True, text=True, timeout=2)
            val = r.stdout.strip()
            print(f"[Ultimate Parse] gsettings edge-tiling = {val}")
            if val != "true":
                print("[Ultimate Parse] → edge-tiling is DISABLED. Enable with:")
                print("    gsettings set org.gnome.mutter edge-tiling true")
        except Exception:
            pass
        print("[Ultimate Parse] Quick workaround: Super+← / Super+→ snaps left/right.")
        print("                 Permanent: log out → ⚙ → 'Ubuntu on Xorg'.")

    app = ParseApp()

    def _report():
        try:
            app.update_idletasks()
            sw = app.winfo_screenwidth(); sh = app.winfo_screenheight()
            mw, mh = app.minsize()
            w, h = app.winfo_width(), app.winfo_height()
            print(f"[Ultimate Parse] screen    : {sw}x{sh}")
            print(f"[Ultimate Parse] half-width: {sw // 2}")
            print(f"[Ultimate Parse] minsize   : {mw}x{mh}")
            print(f"[Ultimate Parse] window    : {w}x{h}")
            if mw > sw // 2:
                print(f"[Ultimate Parse] ⚠ min width ({mw}) > half screen ({sw // 2})")
                print(f"[Ultimate Parse]   → edge-snap-to-half-screen CANNOT work")
            else:
                print(f"[Ultimate Parse] ✓ min width fits half screen — snapping should work")
            print(f"[Ultimate Parse] palette   : WCAG 2.1 AA (theme-aware)")
        except Exception:
            pass
    app.after(800, _report)

    app.mainloop()

if __name__ == "__main__":
    main()
