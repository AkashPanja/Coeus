"""Minimal ONVIF client: resolve RTSP stream URIs (GetCapabilities/GetProfiles/GetStreamUri)."""
from __future__ import annotations

import base64
import hashlib
import os
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import quote, urlparse, urlunparse

import httpx

NS = {
    "s": "http://www.w3.org/2003/05/soap-envelope",
    "tds": "http://www.onvif.org/ver10/device/wsdl",
    "trt": "http://www.onvif.org/ver10/media/wsdl",
    "tt": "http://www.onvif.org/ver10/schema",
}


def _security_header(user: str, password: str) -> str:
    if not user:
        return ""
    nonce = os.urandom(16)
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + password.encode())
                              .digest()).decode()
    return f"""<s:Header><Security s:mustUnderstand="1"
 xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">
<UsernameToken><Username>{_xml_escape(user)}</Username>
<Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{digest}</Password>
<Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-soap-message-security-1.0#Base64Binary">{base64.b64encode(nonce).decode()}</Nonce>
<Created xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">{created}</Created>
</UsernameToken></Security></s:Header>"""


def _xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _envelope(body: str, user: str, password: str) -> str:
    return (f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<s:Envelope xmlns:s="{NS["s"]}" xmlns:tds="{NS["tds"]}" xmlns:trt="{NS["trt"]}"'
            f' xmlns:tt="{NS["tt"]}">{_security_header(user, password)}'
            f"<s:Body>{body}</s:Body></s:Envelope>")


def _call(url: str, body: str, user: str, password: str, timeout: float = 8.0) -> ET.Element:
    resp = httpx.post(url, content=_envelope(body, user, password).encode(),
                      headers={"Content-Type": "application/soap+xml; charset=utf-8"},
                      timeout=timeout)
    if resp.status_code >= 400:
        raise RuntimeError(f"ONVIF call failed: HTTP {resp.status_code} {resp.text[:300]}")
    return ET.fromstring(resp.content)


def with_credentials(uri: str, user: str, password: str) -> str:
    if not user:
        return uri
    p = urlparse(uri)
    if p.username:
        return uri
    netloc = f"{quote(user, safe='')}:{quote(password, safe='')}@{p.hostname}"
    if p.port:
        netloc += f":{p.port}"
    return urlunparse(p._replace(netloc=netloc))


def get_stream_uris(host: str, port: int, user: str, password: str) -> list[dict]:
    """Return [{'token', 'name', 'width', 'height', 'uri'}] for every media profile."""
    device_url = f"http://{host}:{port}/onvif/device_service"
    caps = _call(device_url, '<tds:GetCapabilities><tds:Category>Media</tds:Category>'
                             '</tds:GetCapabilities>', user, password)
    xaddr = caps.find(".//tt:Media/tt:XAddr", NS)
    media_url = xaddr.text.strip() if xaddr is not None and xaddr.text else device_url
    profiles = _call(media_url, "<trt:GetProfiles/>", user, password)
    out = []
    for prof in profiles.findall(".//trt:Profiles", NS):
        token = prof.get("token", "")
        name_el = prof.find("tt:Name", NS)
        res = prof.find(".//tt:VideoEncoderConfiguration/tt:Resolution", NS)
        width = int(res.find("tt:Width", NS).text) if res is not None else 0
        height = int(res.find("tt:Height", NS).text) if res is not None else 0
        body = ("<trt:GetStreamUri><trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream>"
                "<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport></trt:StreamSetup>"
                f"<trt:ProfileToken>{_xml_escape(token)}</trt:ProfileToken></trt:GetStreamUri>")
        uri_el = _call(media_url, body, user, password).find(".//tt:Uri", NS)
        if uri_el is None or not uri_el.text:
            continue
        out.append({"token": token, "name": name_el.text if name_el is not None else token,
                    "width": width, "height": height,
                    "uri": with_credentials(uri_el.text.strip(), user, password)})
    out.sort(key=lambda p: p["width"] * p["height"], reverse=True)
    return out
