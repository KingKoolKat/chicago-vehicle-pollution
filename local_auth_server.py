import datetime
import hashlib
import hmac
import json
import os
import secrets
import threading
import uuid
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
USERS_FILE = os.path.join(DATA_DIR, "auth_users.txt")
SESSIONS_FILE = os.path.join(DATA_DIR, "auth_sessions.txt")
PBKDF2_ITERATIONS = 120000
SALT_BYTES = 16
SESSION_TTL_SECONDS = 60 * 60 * 24 * 14
LOCK = threading.Lock()

app = FastAPI(title="EcoTrack Local Auth")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class AuthRequest(BaseModel):
    action: str
    token: Optional[str] = None
    userId: Optional[str] = None
    profile: Optional[Dict[str, Any]] = None
    name: Optional[str] = None
    email: Optional[str] = None
    password: Optional[str] = None
    role: Optional[str] = None
    avatarUrl: Optional[str] = None
    currentPassword: Optional[str] = None
    newPassword: Optional[str] = None


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalize_email(email: Any) -> str:
    return str(email or "").strip().lower()


def _ensure_storage() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(USERS_FILE):
        with open(USERS_FILE, "w", encoding="utf-8") as f:
            json.dump([], f)
    if not os.path.exists(SESSIONS_FILE):
        with open(SESSIONS_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f)


def _read_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path: str, payload: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=True, indent=2)


def _load_state() -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    _ensure_storage()
    users = _read_json(USERS_FILE, [])
    sessions = _read_json(SESSIONS_FILE, {})
    if not isinstance(users, list):
        users = []
    if not isinstance(sessions, dict):
        sessions = {}
    return users, sessions


def _save_state(users: List[Dict[str, Any]], sessions: Dict[str, Dict[str, Any]]) -> None:
    _write_json(USERS_FILE, users)
    _write_json(SESSIONS_FILE, sessions)


def _public_user(user: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": user.get("id"),
        "name": user.get("name", ""),
        "email": user.get("email", ""),
        "provider": user.get("provider", "local"),
        "role": user.get("role", "resident"),
        "avatarUrl": user.get("avatar_url", ""),
    }


def _find_user_by_email(users: List[Dict[str, Any]], email: str) -> Tuple[int, Optional[Dict[str, Any]]]:
    clean = _normalize_email(email)
    for idx, user in enumerate(users):
        if _normalize_email(user.get("email")) == clean:
            return idx, user
    return -1, None


def _find_user_by_id(users: List[Dict[str, Any]], user_id: str) -> Tuple[int, Optional[Dict[str, Any]]]:
    for idx, user in enumerate(users):
        if str(user.get("id")) == str(user_id):
            return idx, user
    return -1, None


def _derive_hash(password: str, salt_hex: str) -> str:
    derived = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt_hex),
        PBKDF2_ITERATIONS,
    )
    return derived.hex()


def _new_password(password: str) -> Tuple[str, str]:
    salt = secrets.token_hex(SALT_BYTES)
    return salt, _derive_hash(password, salt)


def _prune_sessions(sessions: Dict[str, Dict[str, Any]]) -> bool:
    now = datetime.datetime.now(datetime.timezone.utc)
    changed = False
    for token in list(sessions.keys()):
        raw_exp = sessions[token].get("expires_at")
        try:
            expires_at = datetime.datetime.fromisoformat(str(raw_exp).replace("Z", "+00:00"))
        except Exception:
            expires_at = None
        if not expires_at or expires_at <= now:
            sessions.pop(token, None)
            changed = True
    return changed


def _new_session(sessions: Dict[str, Dict[str, Any]], user_id: str) -> str:
    token = secrets.token_urlsafe(48)
    expires = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=SESSION_TTL_SECONDS)
    sessions[token] = {
        "user_id": user_id,
        "expires_at": expires.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }
    return token


def _session_user(users: List[Dict[str, Any]], sessions: Dict[str, Dict[str, Any]], token: Optional[str]) -> Optional[Dict[str, Any]]:
    if not token:
        return None
    session = sessions.get(token)
    if not session:
        return None
    _, user = _find_user_by_id(users, str(session.get("user_id", "")))
    return user


@app.post("/auth")
def auth(request: AuthRequest) -> Dict[str, Any]:
    action = str(request.action or "").strip().lower()
    if not action:
        return {"ok": False, "message": "Missing 'action'."}

    with LOCK:
        users, sessions = _load_state()
        changed = _prune_sessions(sessions)

        if action == "signup":
            name = str(request.name or "").strip()
            email = _normalize_email(request.email)
            password = str(request.password or "").strip()

            if not name:
                return {"ok": False, "message": "Name is required."}
            if not email or "@" not in email:
                return {"ok": False, "message": "Valid email is required."}
            if len(password) < 8:
                return {"ok": False, "message": "Password must be at least 8 characters."}

            _, existing = _find_user_by_email(users, email)
            if existing:
                return {"ok": False, "message": "An account with this email already exists."}

            salt, password_hash = _new_password(password)
            user = {
                "id": str(uuid.uuid4()),
                "name": name,
                "email": email,
                "provider": "local",
                "role": "resident",
                "avatar_url": "",
                "salt": salt,
                "password_hash": password_hash,
                "created_at": _now_iso(),
            }
            users.append(user)
            token = _new_session(sessions, user["id"])
            _save_state(users, sessions)
            return {"ok": True, "user": _public_user(user), "token": token}

        if action == "login":
            email = _normalize_email(request.email)
            password = str(request.password or "").strip()
            _, user = _find_user_by_email(users, email)
            if not user:
                return {"ok": False, "message": "Incorrect email or password."}
            if user.get("provider", "local") != "local":
                return {"ok": False, "message": "Use Google sign-in for this account."}

            salt = str(user.get("salt", ""))
            expected = str(user.get("password_hash", ""))
            provided = _derive_hash(password, salt) if salt and expected else ""
            if not expected or not hmac.compare_digest(provided, expected):
                return {"ok": False, "message": "Incorrect email or password."}

            token = _new_session(sessions, str(user["id"]))
            _save_state(users, sessions)
            return {"ok": True, "user": _public_user(user), "token": token}

        if action == "google":
            profile = request.profile or {}
            email = _normalize_email(profile.get("email"))
            if not email:
                return {"ok": False, "message": "Google profile missing email."}

            idx, user = _find_user_by_email(users, email)
            if user and user.get("provider") == "local":
                return {"ok": False, "message": "Account exists with password. Log in with email/password."}

            if not user:
                user = {
                    "id": str(profile.get("sub") or uuid.uuid4()),
                    "name": str(profile.get("name") or email.split("@")[0]),
                    "email": email,
                    "provider": "google",
                    "role": "resident",
                    "avatar_url": str(profile.get("picture") or ""),
                    "salt": "",
                    "password_hash": "",
                    "created_at": _now_iso(),
                }
                users.append(user)
            else:
                users[idx]["provider"] = "google"
                users[idx]["salt"] = ""
                users[idx]["password_hash"] = ""
                users[idx]["name"] = str(profile.get("name") or users[idx].get("name") or "")
                users[idx]["avatar_url"] = str(profile.get("picture") or users[idx].get("avatar_url") or "")
                user = users[idx]

            token = _new_session(sessions, str(user["id"]))
            _save_state(users, sessions)
            return {"ok": True, "user": _public_user(user), "token": token}

        if action == "session":
            user = _session_user(users, sessions, request.token)
            if changed:
                _save_state(users, sessions)
            if not user:
                return {"ok": False, "message": "Invalid session."}
            return {"ok": True, "user": _public_user(user)}

        if action == "logout":
            token = str(request.token or "").strip()
            if token and token in sessions:
                sessions.pop(token, None)
                changed = True
            if changed:
                _save_state(users, sessions)
            return {"ok": True}

        if action == "update_profile":
            user = _session_user(users, sessions, request.token)
            if not user:
                if changed:
                    _save_state(users, sessions)
                return {"ok": False, "message": "You need to be logged in."}

            name = str(request.name or "").strip()
            role = "admin" if str(request.role or "").strip().lower() == "admin" else "resident"
            avatar = str(request.avatarUrl or "").strip()
            if not name:
                return {"ok": False, "message": "Name is required."}

            idx, _ = _find_user_by_id(users, str(user.get("id")))
            if idx < 0:
                return {"ok": False, "message": "User record not found."}
            users[idx]["name"] = name
            users[idx]["role"] = role
            users[idx]["avatar_url"] = avatar
            _save_state(users, sessions)
            return {"ok": True, "user": _public_user(users[idx])}

        if action == "update_password":
            user = _session_user(users, sessions, request.token)
            if not user:
                if changed:
                    _save_state(users, sessions)
                return {"ok": False, "message": "You need to be logged in."}
            if user.get("provider", "local") != "local":
                return {"ok": False, "message": "Password changes are only available for local accounts."}

            current = str(request.currentPassword or "").strip()
            new_password = str(request.newPassword or "").strip()
            if len(new_password) < 8:
                return {"ok": False, "message": "New password must be at least 8 characters."}

            salt = str(user.get("salt", ""))
            expected = str(user.get("password_hash", ""))
            provided = _derive_hash(current, salt) if salt and expected else ""
            if not expected or not hmac.compare_digest(provided, expected):
                return {"ok": False, "message": "Current password is incorrect."}

            next_salt, next_hash = _new_password(new_password)
            idx, _ = _find_user_by_id(users, str(user.get("id")))
            if idx < 0:
                return {"ok": False, "message": "User record not found."}
            users[idx]["salt"] = next_salt
            users[idx]["password_hash"] = next_hash
            _save_state(users, sessions)
            return {"ok": True, "user": _public_user(users[idx])}

        if action == "get_user":
            requester = _session_user(users, sessions, request.token)
            if not requester:
                if changed:
                    _save_state(users, sessions)
                return {"ok": False, "message": "You need to be logged in."}
            _, found = _find_user_by_id(users, str(request.userId or ""))
            if not found:
                return {"ok": False, "message": "User record not found."}
            if changed:
                _save_state(users, sessions)
            return {"ok": True, "user": _public_user(found)}

        if changed:
            _save_state(users, sessions)
        return {"ok": False, "message": f"Unsupported action '{action}'."}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("local_auth_server:app", host="127.0.0.1", port=8001, reload=True)
