"""Adapter from a FastAPI request to python3-saml's WSGI-shaped request dict."""

from fastapi import Request


async def saml_request_data(request: Request) -> dict:
    """Adapt a FastAPI :class:`Request` into python3-saml's WSGI-shaped request dict.

    python3-saml derives ``Destination``/audience checks from these fields, so they
    must reflect what the IdP actually sees — the **public** URL, not an internal
    Docker hostname. ``X-Forwarded-*`` is trusted here because SAML endpoints only
    run in prod behind the nginx/PKI overlay, matching the internal/public URL
    split OIDC's ``discovery.py``/``endpoints.py`` already establish for the same
    reason.
    """
    forwarded_proto = request.headers.get("x-forwarded-proto")
    https = "on" if (forwarded_proto or request.url.scheme) == "https" else "off"
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    if ":" in host:
        server_host, _, server_port = host.partition(":")
    else:
        server_host, server_port = host, "443" if https == "on" else "80"

    post_data: dict = {}
    get_data: dict = dict(request.query_params)
    if request.method == "POST":
        form = await request.form()
        post_data = dict(form)

    return {
        "https": https,
        "http_host": server_host,
        "server_port": server_port,
        "script_name": request.url.path,
        "get_data": get_data,
        "post_data": post_data,
    }
