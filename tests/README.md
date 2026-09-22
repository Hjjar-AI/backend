# Quiz — Test Suite

The backend test suite lives under `backend/tests/`. Its endpoint
contract tests read the sibling `frontend/` checkout when available.

## Run

    python tests/runtests.py                     # everything
    python tests/runtests.py users               # one package
    python tests/runtests.py users.test_authentication
    python tests/runtests.py questions.test_state_import

## Requirements

The test suite uses Django's own test runner. No pytest, no
factory_boy, no extra pip installs. Only the packages already in
`backend/requirements.txt` are needed.

## What it assumes

  • Run these commands from `backend/`, where `manage.py` lives.
  • `runtests.py` adds that directory to `sys.path`, so `config`,
    `apps`, and `tests` are importable.
  • A sibling `frontend/` checkout enables the cross-project endpoint
    contract tests; those tests skip in a backend-only checkout.
