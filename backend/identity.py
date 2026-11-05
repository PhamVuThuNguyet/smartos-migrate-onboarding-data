"""Call the PMS identity GraphQL API. Login returns tokens; `me` is the user context."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

IDENTITY_URL = os.environ.get(
    "SMARTOS_IDENTITY_URL",
    "https://api-new.smartos.space/identity-service/pms/graphql",
)

LOGIN_MUTATION = """
mutation Login($input: LoginCommandInput!) {
  login(input: $input) {
    accessToken
    refreshToken
  }
}
"""

REFRESH_MUTATION = """
mutation Refresh($input: String!) {
  refreshToken(input: $input) {
    accessToken
    refreshToken
  }
}
"""

ME_QUERY = """
query Me {
  me {
    id
    accountId
    accountType
    fullName
    email
    phoneNumber
    phoneNumberDialIndex
    contactPhoneNumber
    contactPhoneNumberDialIndex
    avatar
    locationIds
    businessId
    customerId
    namespace
    isActive
    fullPhoneNumber
    modules
    policies
  }
}
"""

LOGOUT_MUTATION = """
mutation Logout {
  logout {
    success
    code
  }
}
"""


class IdentityError(Exception):
    def __init__(self, message: str, status_code: int = 400, code: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def namespace_of(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    if "://" in raw:
        raw = raw.split("://", 1)[1]
    host = raw.split("/", 1)[0]
    if "." in host:
        return host.split(".", 1)[0]
    return host


def api_base() -> str:
    explicit = os.environ.get("SMARTOS_API_BASE", "").strip().rstrip("/")
    if explicit:
        return explicit
    if "/identity-service/" in IDENTITY_URL:
        return IDENTITY_URL.split("/identity-service/", 1)[0]
    return "https://api-new.smartos.space"


def service_graphql_url(service: str) -> str:
    return f"{api_base()}/{service}-service/pms/graphql"


def _headers(namespace: str, token: str = "") -> dict[str, str]:
    headers = {
        "accept": "*/*",
        "accept-language": "vi",
        "content-type": "application/json",
        "namespace": namespace,
        "timezone": "Asia/Saigon",
    }
    if token:
        headers["authorization"] = f"Bearer {token}"
    return headers


def graphql(
    query: str,
    variables: dict | None,
    namespace: str,
    token: str = "",
    url: str = "",
    timeout: int = 20,
) -> dict:
    payload = json.dumps({"query": query, "variables": variables or {}}).encode()
    request = urllib.request.Request(
        url or IDENTITY_URL,
        data=payload,
        headers=_headers(namespace, token),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")
        try:
            body = json.loads(detail)
        except json.JSONDecodeError as parse_error:
            raise IdentityError(f"SmartOS API lỗi {error.code}", error.code) from parse_error
    except urllib.error.URLError as error:
        raise IdentityError("Không kết nối được SmartOS API") from error

    errors = body.get("errors") or []
    if not errors and body.get("data") is None and body.get("message"):
        errors = [body]
    if errors:
        message = pms_error_text(errors)
        first = errors[0] if isinstance(errors[0], dict) else {}
        extensions = first.get("extensions") if isinstance(first, dict) else {}
        if not isinstance(extensions, dict):
            extensions = {}
        code = str(
            extensions.get("code")
            or (first.get("code") if isinstance(first, dict) else "")
            or ""
        )
        lowered = message.lower()
        status = (
            401
            if code in {"SESSION_EXPIRED", "UNAUTHENTICATED", "UNAUTHORIZED"}
            or "incorrect" in lowered
            or "expired" in lowered
            or "unauthor" in lowered
            else 400
        )
        raise IdentityError(message, status, code)
    return body.get("data") or {}


GENERIC_PMS = {
    "bad request exception",
    "bad request",
    "internal server error",
    "something went wrong",
}


def pms_error_text(errors) -> str:
    parts = []
    for item in errors or []:
        text = _one_pms_message(item)
        if text:
            parts.append(text)
    return " | ".join(dict.fromkeys(parts)) or "SmartOS API lỗi"


def _one_pms_message(item) -> str:
    if not isinstance(item, dict):
        return _as_message_text(item)
    extensions = item.get("extensions") if isinstance(item.get("extensions"), dict) else {}
    original = extensions.get("originalError") or item.get("originalError") or {}
    if not isinstance(original, dict):
        original = {}
    response = original.get("response") if isinstance(original.get("response"), dict) else {}
    for value in (
        original.get("message"),
        response.get("message") if response else None,
        extensions.get("message"),
        item.get("message"),
    ):
        text = _as_message_text(value)
        if text:
            return text
    return ""


def _as_message_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return " | ".join(part for part in (_as_message_text(item) for item in value) if part)
    if isinstance(value, dict):
        return _as_message_text(value.get("message") or value.get("error"))
    text = str(value).strip()
    if not text or text.lower() in GENERIC_PMS:
        return ""
    return text


def refresh_tokens(namespace: str, refresh_token: str) -> dict:
    data = graphql(REFRESH_MUTATION, {"input": refresh_token}, namespace)
    tokens = data.get("refreshToken") or {}
    access = tokens.get("accessToken")
    refresh = tokens.get("refreshToken")
    if not access:
        raise IdentityError("Không làm mới được phiên đăng nhập", 401, "SESSION_EXPIRED")
    return {"accessToken": access, "refreshToken": refresh or refresh_token}


def login(domain: str, username: str, password: str) -> dict:
    namespace = namespace_of(domain)
    if not namespace or not username.strip() or not password:
        raise IdentityError("Nhập business domain, username và password")
    data = graphql(
        LOGIN_MUTATION,
        {"input": {"login": username.strip(), "password": password}},
        namespace,
    )
    tokens = data.get("login") or {}
    access = tokens.get("accessToken")
    refresh = tokens.get("refreshToken")
    if not access or not refresh:
        raise IdentityError("Identity API không trả accessToken")
    me = {}
    try:
        me = (graphql(ME_QUERY, None, namespace, access) or {}).get("me") or {}
    except IdentityError:
        me = {}
    return {
        "domain": namespace,
        "accessToken": access,
        "refreshToken": refresh,
        "me": me,
    }


def logout(domain: str, access_token: str) -> None:
    namespace = namespace_of(domain)
    if not namespace or not access_token:
        return
    try:
        graphql(LOGOUT_MUTATION, None, namespace, access_token)
    except IdentityError:
        return
