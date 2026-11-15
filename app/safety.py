"""Rules that decide whether an automatic email may be sent to a sender.

WHY (AUDIT H1, C4, H9): the old check missed mailing lists, bounces, other
autoresponders, our own aliases and forged senders, and our outgoing mail
did not identify itself as automatic, so another autoresponder could
ping-pong with us. Every check here *blocks* a reply; none can force one.
The ticket is still created for blocked mail, so no lead is lost.
"""

from __future__ import annotations

from email.message import EmailMessage, Message
from email.utils import parseaddr

NO_REPLY_LOCAL_PARTS = (
    "noreply", "no-reply", "no_reply", "donotreply", "do-not-reply", "do_not_reply",
    "mailer-daemon", "postmaster", "bounce", "bounces",
)
LIST_HEADERS = ("List-Id", "List-Unsubscribe", "List-Post", "List-Help", "List-Owner", "List-Subscribe", "List-Archive")
GMAIL_DOMAINS = {"gmail.com", "googlemail.com"}


def normalize_address(address: str) -> str:
    """Canonical form used to compare our own addresses.

    Strips "+tag" (user+hvac@x.com is still user@x.com) and, for Gmail, dots
    in the local part (Gmail ignores them), so aliases are recognised as us.
    """
    address = parseaddr(str(address or ""))[1].strip().lower()
    if "@" not in address:
        return address
    local, domain = address.rsplit("@", 1)
    local = local.split("+", 1)[0]
    if domain in GMAIL_DOMAINS:
        local = local.replace(".", "")
        domain = "gmail.com"
    return f"{local}@{domain}"


def own_address_set(config: dict) -> set[str]:
    addresses = [config.get("email_address", "")] + list(config.get("own_addresses", []) or [])
    return {normalize_address(item) for item in addresses if str(item).strip()}


def own_raw_addresses(config: dict) -> set[str]:
    """Lower-cased addresses exactly as written (for matching Sent-folder From headers)."""
    addresses = [config.get("email_address", "")] + list(config.get("own_addresses", []) or [])
    return {str(item).strip().lower() for item in addresses if str(item).strip()}


def _header(msg: Message, name: str) -> str:
    try:
        return str(msg.get(name, "") or "").strip()
    except Exception:  # malformed header: treat as present-but-unreadable
        return "?"


def is_own_message(msg: Message, sender_email: str, own_addresses: set[str]) -> bool:
    """Mail we sent ourselves (e.g. the dispatch summary) must never be ingested (AUDIT C4)."""
    return normalize_address(sender_email) in own_addresses or bool(_header(msg, "X-HVAC-Auto"))


def _domain_aligned(header_domain: str, from_domain: str) -> bool:
    """Relaxed DMARC-style alignment: same domain or one is a subdomain of the other.

    A bare TLD ("com") never aligns, so a signature for "com" can't vouch for anyone.
    """
    header_domain = header_domain.strip().strip(">").lower().lstrip("@")
    if "@" in header_domain:
        header_domain = header_domain.rsplit("@", 1)[1]
    from_domain = from_domain.lower()
    if "." not in header_domain or "." not in from_domain:
        return False
    return (
        header_domain == from_domain
        or from_domain.endswith("." + header_domain)
        or header_domain.endswith("." + from_domain)
    )


def sender_authenticated(msg: Message, sender_email: str, trusted_authserv_ids: list[str]) -> bool:
    """True if OUR mail server says the From: domain is authentic.

    Only the top-most Authentication-Results header is trusted, and only if its
    server id is one we configured (Gmail: mx.google.com). WHY top-most: the
    receiving server prepends its result, while anything below it was written
    by the sender and can be forged.

    Pass = dmarc=pass, or dkim=pass / spf=pass whose domain aligns with the
    From: domain. WHY alignment matters: SPF checks the hidden envelope sender,
    not the visible From:, so a bare spf=pass doesn't prove From: is genuine.
    """
    try:
        results = msg.get_all("Authentication-Results") or []
    except Exception:
        return False
    if not results or "@" not in sender_email:
        return False
    top = str(results[0])
    authserv_id, _sep, rest = top.partition(";")
    if authserv_id.strip().lower() not in {item.strip().lower() for item in trusted_authserv_ids}:
        return False
    from_domain = sender_email.rsplit("@", 1)[1]
    passed = False
    for clause in rest.split(";"):
        tokens = clause.strip().split()
        if not tokens:
            continue
        method_result = tokens[0].lower()
        props = {}
        for token in tokens[1:]:
            if "=" in token:
                key, value = token.split("=", 1)
                props[key.lower()] = value.strip("();")
        if method_result.startswith("dmarc="):
            if method_result == "dmarc=pass":
                passed = True
            elif method_result == "dmarc=fail":
                return False  # an explicit DMARC failure overrides everything else
        elif method_result == "dkim=pass":
            if _domain_aligned(props.get("header.d", props.get("header.i", "")), from_domain):
                passed = True
        elif method_result == "spf=pass":
            if _domain_aligned(props.get("smtp.mailfrom", ""), from_domain):
                passed = True
    return passed


def reply_block_reason(
    msg: Message,
    sender_email: str,
    own_addresses: set[str],
    *,
    require_auth: bool,
    trusted_authserv_ids: list[str],
) -> str | None:
    """Return why we must NOT auto-email this sender, or None if it is allowed."""
    sender = (sender_email or "").strip().lower()
    if not sender or "@" not in sender:
        return "no valid sender address"
    if normalize_address(sender) in own_addresses:
        return "sender is our own address"
    if _header(msg, "X-HVAC-Auto"):
        return "message was generated by this agent"
    local = sender.split("@", 1)[0]
    if any(tag in local for tag in NO_REPLY_LOCAL_PARTS):
        return "no-reply / system sender address"

    auto_submitted = _header(msg, "Auto-Submitted").lower()
    if auto_submitted and auto_submitted != "no":
        return f"Auto-Submitted: {auto_submitted}"
    if _header(msg, "X-Autoreply") or _header(msg, "X-Autorespond"):
        return "sender is an autoresponder"
    suppress = _header(msg, "X-Auto-Response-Suppress").lower()
    if suppress and any(token in suppress for token in ("all", "autoreply", "oof")):
        return f"X-Auto-Response-Suppress: {suppress}"
    precedence = _header(msg, "Precedence").lower()
    if precedence in {"bulk", "junk", "list", "auto_reply"}:
        return f"Precedence: {precedence}"
    for name in LIST_HEADERS:
        if _header(msg, name):
            return f"mailing list ({name} header)"
    if _header(msg, "Return-Path") == "<>":
        return "bounce (empty Return-Path)"
    content_type = (msg.get_content_type() or "").lower()
    if content_type in {"multipart/report", "message/delivery-status"}:
        return f"delivery report ({content_type})"

    if require_auth and not sender_authenticated(msg, sender, trusted_authserv_ids):
        return "sender not authenticated by the mail server (no aligned DKIM/SPF or DMARC pass)"
    return None


def mark_automatic(message: EmailMessage, kind: str) -> EmailMessage:
    """Label everything we send as automatic (RFC 3834) so other systems don't auto-reply to it.

    kind: ack | follow_up -> auto-replied; dispatch | sms -> auto-generated.
    """
    message["Auto-Submitted"] = "auto-replied" if kind in {"ack", "follow_up"} else "auto-generated"
    message["X-Auto-Response-Suppress"] = "All"  # tells Exchange/Outlook not to send OOF replies
    message["X-HVAC-Auto"] = "true"
    message["X-HVAC-Auto-Type"] = kind
    return message
