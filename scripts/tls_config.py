"""Shared web/TLS settings and local HTTPS readiness with public-host SNI."""
import hashlib
from http.client import HTTPSConnection
import json
import re
import socket
import ssl
from urllib.parse import urlsplit
from urllib.request import urlopen

LE_PRODUCTION = "https://acme-v02.api.letsencrypt.org/directory"
LE_STAGING = "https://acme-staging-v02.api.letsencrypt.org/directory"


def web_settings(values):
    enabled = values.get("tlsEnabled", False)
    if type(enabled) is not bool:
        raise ValueError("tlsEnabled must be true or false")
    for key, default in (("httpsPort", 8443), ("httpRedirectPort", 8080)):
        port = values.get(key, default)
        if type(port) is not int or not 1024 <= port <= 65535:
            raise ValueError(f"{key} must be an integer from 1024 to 65535")
    host = values.get("publicHostname", "")
    email = values.get("acmeEmail", "")
    accepted = values.get("acmeAcceptTos", False)
    if type(accepted) is not bool:
        raise ValueError("acmeAcceptTos must be true or false")
    if not isinstance(host, str) or not isinstance(email, str):
        raise ValueError("publicHostname and acmeEmail must be strings")
    acme_url = values.get("acmeUrl", LE_PRODUCTION)
    parsed_ca = urlsplit(acme_url) if isinstance(acme_url, str) else None
    if not parsed_ca or parsed_ca.scheme != "https" or not parsed_ca.hostname or parsed_ca.username or parsed_ca.password or parsed_ca.fragment:
        raise ValueError("acmeUrl must be an absolute HTTPS directory URL")
    if enabled:
        if not re.fullmatch(r"(?=.{1,253}$)(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,63}", host):
            raise ValueError("Set publicHostname to a public DNS hostname, without scheme, port or path")
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            raise ValueError("Set acmeEmail before enabling TLS")
        if not accepted:
            raise ValueError("Read your ACME provider's terms and explicitly set acmeAcceptTos: true")
        host = host.lower()
        https_port = values.get("httpsPort", 8443)
        redirect_port = values.get("httpRedirectPort", 8080)
        if len({https_port, redirect_port, values["sshPort"]}) != 3:
            raise ValueError("HTTPS, HTTP redirect and SSH published ports must differ")
        # Separate cache namespaces prevent staging certificates/accounts being
        # mistaken for production credentials after switching ACME providers.
        cache = "/var/lib/gitea/https"
        if acme_url == LE_STAGING:
            cache += "-staging"
        elif acme_url != LE_PRODUCTION:
            cache += "-" + hashlib.sha256(acme_url.encode()).hexdigest()[:12]
        return {"enabled": True, "hostname": host, "port": https_port,
                "redirectPort": redirect_port, "rootUrl": f"https://{host}/",
                "protocol": "https", "email": email, "acmeUrl": acme_url, "cache": cache}
    domain = values.get("sshDomain", "localhost")
    default_host = f"[{domain}]" if ":" in domain and not domain.startswith("[") else domain
    root_url = values.get("rootUrl", f'http://{default_host}:{values["httpPort"]}/')
    parsed = urlsplit(root_url) if isinstance(root_url, str) else None
    if not parsed or parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("rootUrl must be an absolute HTTP(S) URL without credentials, query or fragment")
    return {"enabled": False, "hostname": domain, "port": values["httpPort"],
            "redirectPort": values.get("httpRedirectPort", 8080), "rootUrl": root_url.rstrip("/") + "/",
            "protocol": "http", "email": email, "acmeUrl": acme_url, "cache": "/var/lib/gitea/https"}


def web_environment(values):
    web = web_settings(values)
    return {"HTTP_PORT": str(web["port"]), "SSH_DOMAIN": web["hostname"],
            "GITEA_PROTOCOL": web["protocol"], "GITEA_ROOT_URL": web["rootUrl"],
            "GITEA_DOMAIN": web["hostname"], "PUBLIC_HOSTNAME": web["hostname"],
            "HTTP_REDIRECT_PORT": str(web["redirectPort"]), "ACME_EMAIL": web["email"],
            "ACME_URL": web["acmeUrl"], "ACME_DIRECTORY": web["cache"],
            "TLS_ENABLED": "true" if web["enabled"] else "false"}


class LocalHTTPSConnection(HTTPSConnection):
    """Connect to this instance's local published port, retaining hostname checks."""
    def connect(self):
        raw = socket.create_connection(("127.0.0.1", self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def readiness_version(values):
    web = web_settings(values)
    if not web["enabled"]:
        with urlopen(f'http://127.0.0.1:{web["port"]}/api/v1/version', timeout=10) as response:
            return json.load(response).get("version")
    connection = LocalHTTPSConnection(web["hostname"], port=web["port"],
                                      context=ssl.create_default_context(), timeout=10)
    try:
        connection.request("GET", "/api/v1/version", headers={"Host": web["hostname"]})
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError(f"HTTPS readiness returned HTTP {response.status}")
        return json.loads(response.read()).get("version")
    finally:
        connection.close()
