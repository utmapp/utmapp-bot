"""What text from users holds, and what a public comment may say.

The command line is `gamesdb redact|lint|qemu-args` and `gamesdb comment`;
this module works on text only (attachments.py handles the files).

redact     every match replaced by a typed placeholder, e.g. [USER], [IPv4],
           [MAC].  The same value always gets the same placeholder within
           one run ([HOST-1], [HOST-2]), so a log stays readable.  Extra
           literal names (the reporter's display name, a VM name seen in the
           log, ...) are matched case-insensitively.
lint       every match (class, line, a masked excerpt): used on every
           Auto-Triage comment before it is posted.
qemu_args  a UTM debug log's QEMU command line without names, ids, MACs,
           serials or host paths.
comment_problems
           why a comment must not be posted: personal data, links outside
           the reference sites (LINK_DOMAINS, CODE_HOSTS), and with
           --private-source anything that shows its data came from private
           attachments (SOURCE_TELLS).

Patterns are deliberately greedy: a redacted version number costs little, a
leaked home folder name does not.  Review the output anyway.
"""

import ipaddress
import re
import os
import shlex
import urllib.parse

# (class, regex, group to replace or 0).  Order matters: paths first, so the
# user name inside them becomes [USER] rather than being half-matched later.
PATTERNS = [
    ("USER", re.compile(r"(?i)(?<=/Users/)(?!Shared\b|Guest\b|\[)[^/\s\"'<>:]+"), 0),
    ("USER", re.compile(r"(?<=/home/)(?!\[)[^/\s\"'<>:]+"), 0),
    ("USER", re.compile(r"(?i)(?<=[A-Z]:\\Users\\)(?!Public\b|Default\b|All Users\b|\[)[^\\\n\"'<>:]+"), 0),
    ("USER", re.compile(r"(?i)(?<=[A-Z]:/Users/)(?!Public\b|Default\b|\[)[^/\s\"'<>:]+"), 0),
    ("USER", re.compile(r"(?i)(?<=\\\\Users\\\\)(?!Public\b|Default\b|\[)[^\\\s\"'<>:]+"), 0),
    # UTM VM names are chosen by the user: the bundle in paths, QEMU's -name
    ("VMNAME", re.compile(r"(?<=/)(?!\[)([^/\n\"]+)(?=\.utm\b)"), 1),
    ("VMNAME", re.compile(r"(?<=-name \")([^\"\n]+)(?=\")"), 1),
    ("VMNAME", re.compile(r"(?<=-name )(?![\"\[])([^\s,]+)"), 1),
    ("VMNAME", re.compile(r"(?m)(?<=server name: )(?!\[)(.+?)\s*$"), 1),
    # syslog-style lines: "Nov  1 12:37:58 hostname process[pid]:"
    ("HOST", re.compile(r"(?m)^[A-Z][a-z]{2} [ \d]\d \d\d:\d\d:\d\d (?!\[)([^\s\[]+) [^\s\[]+\[\d+\]"), 1),
    ("EMAIL", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), 0),
    ("URL-CRED", re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)"), 0),
    ("MAC", re.compile(r"(?i)\b(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}\b"), 0),
    # no leading boundary: QEMU glues ids to words (drive1368CF9F-…, where the
    # e of "drive" is itself hex); the earliest 8-4-4-4-12 run is the id
    ("UUID", re.compile(r"(?i)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![0-9a-f])"), 0),
    ("SID", re.compile(r"\bS-1-5-21(?:-\d+){3,4}\b"), 0),
    ("STEAMID", re.compile(r"\b7656119\d{10}\b"), 0),
    ("STEAMID", re.compile(r"\[U:1:\d+\]|\bSTEAM_[0-5]:[01]:\d+\b"), 0),
    ("SERIAL", re.compile(r"(?i)\bserial(?:[ _-]?number)?[\"']?\s*[:=]\s*[\"']?([\w-]{6,})"), 1),
    # "serial C02XK1JHMD6T": a serial after the word, without a separator
    ("SERIAL", re.compile(r"(?i)\bserial(?:[ _-]?(?:number|no\.?))?\s+(?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])([A-Z0-9]{8,14})\b"), 1),
    # "Steam account: name", "username = name", "user: Name", "login: name"
    ("USER", re.compile(r"(?i)\b(?:(?:steam|account|login|user)[ _-]?(?:account|name|login|id)?|username)\s*[:=]\s*(?!\[)([^\s,;()\[\]\"']{2,})"), 1),
    ("HWID", re.compile(r"(?i)\b(?:platform[_ ]uuid|provisioning[_ ]udid|hardware[_ ]uuid)[\"']?\s*[:=]\s*[\"']?([\w-]+)"), 1),
    ("HOST", re.compile(r"(?i)\b[\w'’-]{1,40}s?[-' ’]{0,3}(?:MacBook|iMac|Mac[- ]?mini|Mac[- ]?Studio|Mac[- ]?Pro)(?:[- ](?:Pro|Air|M\d))*(?:[- ]\d+)?(?:\.local|\.lan|\.home)?\b"), 0),
    ("HOST", re.compile(r"\b[\w-]+\.(?:local|lan|home|localdomain)\b"), 0),
    ("HOST", re.compile(r"\b(?:DESKTOP|LAPTOP|WIN)-[A-Z0-9]{5,}\b"), 0),
    ("HOST", re.compile(r"\b[A-Z][A-Z0-9]{1,14}-(?:PC|LAPTOP|DESKTOP)\b"), 0),
    ("SECRET", re.compile(r"(?i)\b(?:token|secret|password|passwd|api[_-]?key|auth)[\"']?\s*[:=]\s*[\"']?([^\s\"',;]{6,})"), 1),
    ("SECRET", re.compile(r"\beyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{10,}\b"), 0),
    ("KEY", re.compile(r"\b[A-Z0-9]{5}-[A-Z0-9]{5}-[A-Z0-9]{5}(?:-[A-Z0-9]{5}){1,2}\b"), 0),
]
IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
# candidates only; ipaddress decides (handles :: compression, rejects times and MACs)
IPV6 = re.compile(r"(?<![\w:.])[0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7}(?![\w:])")
PLACEHOLDER = re.compile(r"\[[A-Za-z0-9-]+\]")
# A user name that is also an ordinary word is removed from paths only:
# propagating it would also hit e.g. QEMU's "-netdev user", and the shared
# placeholder would then reveal the name.
COMMON = {"user", "users", "admin", "administrator", "test", "guest", "root", "default",
          "public", "owner", "home", "mac", "utm", "me", "dev", "build", "local", "shared",
          "steam", "windows", "linux", "ubuntu", "pc", "player", "gamer", "games"}
# Public interface ids are kept: COM/D3D IIDs name an API, not a machine.
IID_CONTEXT = re.compile(r"(?i)(interface|iid|riid|uuidof|queryinterface|guid of|clsid)\W{0,3}[\w ]{0,30}$")
# Addresses that identify nobody: loopback, unspecified, QEMU's user network
# defaults, documentation ranges.
SAFE_V4 = {"127.0.0.1", "0.0.0.0", "255.255.255.255", "10.0.2.2", "10.0.2.3", "10.0.2.15"}


def _ip_ok(s):
    try:
        ip = ipaddress.ip_address(s)
    except ValueError:
        return True  # not an address (a version number like 1.2.3.4 stays flagged below)
    if str(ip) in SAFE_V4 or ip.is_loopback or ip.is_unspecified:
        return True
    return False


class Redactor:
    def __init__(self, names=()):
        self.names = [n for n in names if n and len(n) >= 3]
        self.seen = {}      # (cls, value) -> placeholder
        self.counts = {}

    def _ph(self, cls, value):
        key = (cls, value.lower())
        if key not in self.seen:
            n = sum(1 for c, _ in self.seen if c == cls) + 1
            self.seen[key] = f"[{cls}-{n}]" if cls in ("HOST", "USER", "NAME", "VMNAME", "IPv4", "IPv6") else f"[{cls}]"
        self.counts[cls] = self.counts.get(cls, 0) + 1
        return self.seen[key]

    def matches(self, text):
        """(class, start, end) of everything that looks personal."""
        out = []
        for n in self.names:
            for m in re.finditer(re.escape(n), text, re.I):
                out.append(("NAME", m.start(), m.end()))
        for cls, rx, g in PATTERNS:
            for m in rx.finditer(text):
                if m.group(g):
                    out.append((cls, m.start(g), m.end(g)))
        for m in IPV4.finditer(text):
            parts = m.group(0).split(".")
            if all(int(p) <= 255 for p in parts) and not _ip_ok(m.group(0)):
                out.append(("IPv4", m.start(), m.end()))
        for m in IPV6.finditer(text):
            s = m.group(0)
            try:
                ip = ipaddress.IPv6Address(s)
            except ValueError:
                continue
            if not (ip.is_loopback or ip.is_unspecified):
                out.append(("IPv6", m.start(), m.end()))
        # what an earlier pass already put in is not personal data, and an id
        # the line calls an interface is a public one
        out = [t for t in out if not PLACEHOLDER.fullmatch(text[t[1]:t[2]]) and
               not (t[0] == "UUID" and IID_CONTEXT.search(text[max(0, t[1] - 60):t[1]]))]
        # a name found once (in a path, -name, ...) is removed wherever else
        # it appears literally, e.g. a VM name in a SPICE or window title line
        learned = {}
        for cls, s0, e0 in out:
            v = text[s0:e0]
            if cls in ("USER", "VMNAME", "HOST", "NAME") and len(v) >= 3 and not PLACEHOLDER.fullmatch(v) \
                    and v.lower() not in COMMON:
                learned.setdefault(v.lower(), cls)
        for v, cls in learned.items():
            rx = (r"\b" if v[0].isalnum() else "") + re.escape(v) + (r"\b" if v[-1].isalnum() else "")
            for m in re.finditer(rx, text, re.I):
                out.append((cls, m.start(), m.end()))
        # longest first, drop overlaps
        out.sort(key=lambda t: (t[1], -(t[2] - t[1])))
        kept, end = [], -1
        for t in out:
            if t[1] >= end:
                kept.append(t)
                end = t[2]
        return kept

    def redact(self, text):
        parts, pos = [], 0
        for cls, s, e in self.matches(text):
            parts.append(text[pos:s])
            parts.append(self._ph(cls, text[s:e]))
            pos = e
        parts.append(text[pos:])
        return "".join(parts)


def lint_text(text, names=(), allow=()):
    r = Redactor(names)
    allow = [re.compile(a) for a in allow]
    found = []
    for cls, s, e in r.matches(text):
        val = text[s:e]
        if any(a.search(val) for a in allow):
            continue
        line = text.count("\n", 0, s) + 1
        masked = val[:2] + "…" + val[-1:] if len(val) > 4 else "…"
        found.append((cls, line, masked))
    return found


def lint_report(found, limit=30):
    return "\n".join(f"  {c} line {ln}: {m}" for c, ln, m in found[:limit])


# ---- a QEMU command line fit to quote ---------------------------------------------

# Options that only identify the machine, the user or the VM, or point at
# host files and sockets: dropped whole.
QEMU_DROP = {"-name", "-uuid", "-L", "-pidfile", "-qmp", "-mon", "-chardev", "-monitor",
             "-serial", "-D", "-trace", "-object", "-smbios", "-incoming", "-loadvm"}
# Keys inside an option's value that hold identities or paths.
QEMU_KEYS = re.compile(r"^(?:mac|serial|wwn|uuid|file|path|addr|filename|socket|chardev|"
                       r"tpmdev|id|drive|netdev|hostfwd|guestfwd|name|bootindex)$")


def qemu_args(text):
    """The `Launching: qemu-system-…` line of a UTM debug log, cut down to its
    technical content: machine, accelerator, CPU, memory, devices and
    display, without names, ids, MACs, serials or host paths; None when
    the log has no such line."""
    m = re.search(r"Launching: (qemu-system-\S+ .*)", text)
    if not m:
        return None
    try:
        argv = shlex.split(m[1])
    except ValueError:
        argv = m[1].split()
    out, i = [argv[0]], 1
    while i < len(argv):
        opt = argv[i]
        val = argv[i + 1] if i + 1 < len(argv) and not argv[i + 1].startswith("-") else None
        i += 2 if val is not None else 1
        if opt in QEMU_DROP or opt == "-drive" and val and "if=pflash" in val:
            continue
        if val is None:
            out.append(opt)
            continue
        parts = []
        for kv in val.split(","):
            k = kv.split("=", 1)[0]
            if "=" in kv and QEMU_KEYS.match(k):
                continue
            if "/" in kv:  # a bare path
                continue
            parts.append(kv)
        if parts:
            out.append(f"{opt} {','.join(parts)}")
    return "\n".join(out)


# ---- what a public comment may say ------------------------------------------------

MAX_COMMENT = 20000  # characters; the server's limit

# With --private-source (notes built from a report's private attachments) the
# comment must not show where its data came from: the site keeps attachments
# private, and even whether a report has any is not public.
SOURCE_TELLS = [
    ("redaction placeholder", re.compile(r"\[(?:USER|VMNAME|HOST|NAME|IPv4|IPv6|MAC|UUID|EMAIL|SERIAL|SID|"
                                         r"STEAMID|SECRET|KEY|HWID|URL-CRED)(?:-\d+)?\]")),
    ("mentions attachments or uploads", re.compile(r"(?i)\b(?:attach(?:ed|ment|ments|ing)?|upload(?:ed|s|ing)?|"
                                                   r"zip(?:ped)? (?:file|archive)|archive)\b")),
    ("refers to the reporter's files", re.compile(r"(?i)\byour (?:debug )?(?:logs?|screenshots?|recordings?|"
                                                  r"videos?|files?|crash reports?)\b|\b(?:you|the reporter) "
                                                  r"(?:sent|provided|shared|included)\b|\b(?:provided|shared|"
                                                  r"submitted|included) (?:logs?|screenshots?|recordings?|videos?|files?)\b")),
    ("wall-clock time (use relative times)", re.compile(r"\b\d{4}-\d{2}-\d{2}[ T]\d{1,2}:\d{2}|"
                                                        r"\b\d{1,2}:\d{2}:\d{2}(?:[.,]\d+)?\b|"
                                                        r"\b20\d{6}[-_T]\d{4,6}\b|"     # 20261101-123735
                                                        r"\b1[6-9]\d{8}(?:\.\d+)?\b|"   # Unix seconds, 2020s
                                                        r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) +\d{1,2} \d{1,2}:\d{2}")),
]


def source_tells(text):
    out = []
    for what, rx in SOURCE_TELLS:
        for m in rx.finditer(text):
            out.append((what, text.count("\n", 0, m.start()) + 1, m.group(0)[:40]))
    return out


# Domains a bot comment may link to: documentation and standards.  Code
# hosts are open to anyone, so on them only the projects of this stack (and
# the ones it builds on) are allowed: a reporter's own repository is not a
# reference.
CODE_HOSTS = {
    # UTM and the guest driver, the bot's own forks (fix branches: gamesdb
    # fix-branch), DXMT, DXVK, vkd3d-proton, Khronos, Microsoft, Apple, Valve,
    # QEMU, Wine and Mesa mirrors
    "github.com": {"utmapp", "osy", "utmapp-bot", "3shain", "doitsujin", "hanskristian-work", "khronosgroup",
                   "microsoft", "apple", "valvesoftware", "qemu", "wine-mirror", "mesa3d"},
    "gitlab.freedesktop.org": {"mesa", "virgl", "drm"},
    "gitlab.com": {"qemu-project"},
    "gitlab.winehq.org": {"wine"},
}
LINK_DOMAINS = {
    "docs.getutm.app", "getutm.app",
    "learn.microsoft.com", "support.microsoft.com", "developer.apple.com",
    "support.apple.com", "qemu.org", "www.qemu.org", "qemu-project.gitlab.io",
    "docs.mesa3d.org", "mesa3d.org", "registry.khronos.org", "www.khronos.org",
    "docs.vulkan.org", "store.steampowered.com", "steamcommunity.com",
    "help.steampowered.com", "partner.steamgames.com", "www.pcgamingwiki.com",
    "pcgamingwiki.com", "en.wikipedia.org", "wiki.winehq.org", "bugs.winehq.org",
    "gitlab.winehq.org", "www.codeweavers.com", "lists.gnu.org",
}


def links(text):
    """Every URL in Markdown links, autolinks and bare text."""
    found = re.findall(r"\]\(\s*<?([^)\s>]+)", text)
    found += re.findall(r"<(https?://[^>\s]+)>", text)
    found += re.findall(r"(?<![(<\w])(?:https?://|www\.)[^\s)<>\]]+", text)
    return sorted(set(found))


def site_domains():
    """The games database's own site (GAMESDB_SITE, set by db-setup): bot
    comments may link to its pages.  No domain is built in: the site can run
    anywhere."""
    try:
        from gamesdb import tunnel
        site = tunnel.config().get("GAMESDB_SITE") or ""
    except Exception:
        site = os.environ.get("GAMESDB_SITE", "")
    host = urllib.parse.urlsplit(site if "://" in site else "https://" + site).hostname if site else None
    return (host,) if host else ()


def link_ok(url, extra=()):
    u = url if "://" in url else "https://" + url
    p = urllib.parse.urlsplit(u)
    host = (p.hostname or "").lower()
    if p.scheme not in ("http", "https") or not host or p.username or p.password:
        return False
    extra = {d.lower().rstrip("/") for d in extra}
    owner = (p.path.strip("/").split("/") or [""])[0].lower()
    if host in CODE_HOSTS:
        return owner in CODE_HOSTS[host] or f"{host}/{owner}" in extra
    return any(host == d or host.endswith("." + d) for d in LINK_DOMAINS | extra)


def comment_problems(body, names=(), allow=(), allow_link=(), private_source=False):
    """Why this comment must not be posted, or None."""
    if not body:
        return "empty comment"
    if len(body) > MAX_COMMENT:
        return f"the comment is {len(body)} characters; the server takes at most {MAX_COMMENT}"
    # Links in a bot comment render and carry the bot's authority: only
    # public references chosen by the agent, never a URL out of a report or
    # an attachment (it is the attacker's).
    bad = [u for u in links(body) if not link_ok(u, tuple(allow_link or ()) + site_domains())]
    if bad:
        return ("the comment links to sites outside the reference list:\n  " + "\n  ".join(bad[:20]) +
                "\nLink only to public sources you looked up yourself; --allow-link DOMAIN for one "
                "you checked is a public reference.")
    if private_source:
        tells = source_tells(body)
        if tells:
            return ("the comment shows where its data came from (private attachments):\n" +
                    lint_report(tells) + "\nWrite findings as observations with their configuration, "
                    "without placeholders, file references or the reporter's clock.")
    # a public comment: nothing that looks personal goes out
    found = lint_text(body, names, allow)
    if found:
        return ("the comment may contain personal data:\n" + lint_report(found) +
                "\nRedact it (gamesdb redact), or pass --allow REGEX for a match you checked is "
                "public (e.g. a version number taken for an address).")
    return None
