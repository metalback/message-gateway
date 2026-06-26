"""HTTP route definitions (FastAPI `APIRouter` modules).

Each public surface lives in its own submodule:

- `messages.py`   – `POST /v1/messages`, `GET /v1/messages/{id}`, batch send
- `templates.py`  – WhatsApp template CRUD
- `webhooks.py`   – delivery receipt subscriptions
- `auth.py`       – registration, login, API key rotation
- `billing.py`    – balance and invoice history

The :func:`register_routers` helper in ``app.api`` is the single
mount point the application factory iterates over; adding a new
endpoint should be a one-line change there.
"""
