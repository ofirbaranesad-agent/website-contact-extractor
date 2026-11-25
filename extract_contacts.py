#!/usr/bin/env python3
"""Extract public contact emails and phone numbers from a list of company websites.

Standard library only (Python 3.9+). Reads one domain or URL per line, writes CSV.

    python3 extract_contacts.py sites.txt > contacts.csv

What it does on each site: fetches the homepage, follows up to --pages links that
look like contact/about/team/legal pages, and collects addresses from mailto: links,
Cloudflare-protected addresses, "name [at] domain [dot] com" spellings and plain text.
It obeys robots.txt, waits between requests to the same host, and stops at any
login wall, CAPTCHA or 4xx/5xx instead of trying to get around it.
"""
import argparse, csv, html, json, re, sys, time, urllib.parse, urllib.request, urllib.robotparser
from concurrent.futures import ThreadPoolExecutor

UA = "website-contact-extractor/1.0 (+https://github.com/ofirbaranesad-agent/website-contact-extractor)"
CONTACT_HINT = re.compile(r"contact|about|team|impressum|imprint|legal|support|kontakt|contacto|"
                          r"contatti|nous-contacter|צור|אודות", re.I)
EMAIL = re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9][A-Za-z0-9._%+-]{0,63}@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,24}(?![A-Za-z0-9-])")
SPELLED = re.compile(r"([A-Za-z0-9._%+-]{1,64})\s*[\[({]\s*at\s*[\])}]\s*((?:[A-Za-z0-9-]+\s*(?:[\[({]\s*dot\s*[\])}]|\.)\s*)+[A-Za-z]{2,24})", re.I)
CFEMAIL = re.compile(r'data-cfemail="([0-9a-fA-F]+)"|/cdn-cgi/l/email-protection#([0-9a-fA-F]+)')
TEL = re.compile(r'href=["\']tel:([^"\']+)["\']', re.I)
LINK = re.compile(r'<a\b[^>]*?href=["\']([^"\'#]+)["\'][^>]*>(.*?)</a>', re.I | re.S)
# Things that look like addresses and are not: asset file names, tracking IDs, placeholders.
JUNK_TLD = {"png", "jpg", "jpeg", "gif", "svg", "webp", "avif", "css", "js", "woff", "woff2", "ttf", "map", "ico"}
JUNK_DOMAIN = re.compile(r"(^|\.)(example\.(com|org|net)|sentry\.io|sentry-next\.wixpress\.com|wixpress\.com|"
                         r"domain\.com|email\.com|yourdomain\.com|mysite\.com|company\.com|schema\.org|w3\.org)$", re.I)
JUNK_LOCAL = re.compile(r"^(you|your|name|user|username|email|firstname|john\.?doe|jane\.?doe|test|xxx+)$", re.I)
PHONE_PREFIX = re.compile(r"^[+(]?\d[\d .()+-]{6,}\d(?=[A-Za-z])")       # "777-6582leah@" -> "leah@"
NANP = re.compile(r"^1[2-9]\d{9}$")
ESCAPE_PREFIX = re.compile(r"^(?:u00[0-9a-f]{2}|%[0-9a-f]{2})+", re.I)  # "u003esupport@" -> "support@"


def decode_cfemail(hexstr):
    """Cloudflare 'email protection': first byte is the XOR key for the rest."""
    try:
        raw = bytes.fromhex(hexstr)
        return bytes(b ^ raw[0] for b in raw[1:]).decode("utf-8", "replace")
    except ValueError:
        return ""


def clean_email(addr, site_host=""):
    addr = urllib.parse.unquote(addr).strip(" .,;:<>\"'").lower()
    if addr.count("@") != 1:
        return None
    local, domain = addr.split("@")
    local = ESCAPE_PREFIX.sub("", local)
    # Only strip a digit run when it contains a separator: "1981.ka@" is a real mailbox.
    m = PHONE_PREFIX.match(local)
    if m and re.search(r"[ .()+-]", m.group(0)) and not re.fullmatch(r"[\d.]+", m.group(0)):
        local = local[m.end():]
    # "info@site.dewww.site.de" / "x@gmail.comfacebook.com": text glued after the domain.
    domain = re.sub(r"(\.[a-z]{2,6})www\..*$", r"\1", domain)
    domain = re.sub(r"^((?:gmail|yahoo|hotmail|outlook|icloud|aol)\.com).+$", r"\1", domain)
    tld = domain.rsplit(".", 1)[-1]
    if (not re.search(r"[a-z0-9]", local) or len(local) > 40 or tld in JUNK_TLD
            or JUNK_DOMAIN.search(domain) or JUNK_LOCAL.match(local)
            or re.fullmatch(r"[0-9a-f]{20,}", local)):      # Sentry keys, hashes
        return None
    return f"{local}@{domain}"


def clean_phone(raw, default_cc):
    raw = urllib.parse.unquote(raw).split(";")[0].split(",")[0]
    digits = re.sub(r"\D", "", raw)
    if raw.strip().startswith("+"):
        e164 = "+" + digits
    elif digits.startswith("00"):
        e164 = "+" + digits[2:]
    elif default_cc and not (default_cc == "1" and not NANP.match("1" + digits.lstrip("0"))):
        e164 = "+" + default_cc + digits.lstrip("0")
    else:
        e164 = digits            # no country code on the page and no safe guess: keep as written
    n = len(re.sub(r"\D", "", e164))
    # 8–15 digits per E.164; one repeated digit is a template placeholder; +1 must be a
    # real North American number (10 digits, area code 2–9), which drops "+1 100 0000".
    if not 8 <= n <= 15 or len(set(digits[-7:])) == 1 or (e164.startswith("+1") and not NANP.match(e164[1:])):
        return None
    return e164


class Site:
    def __init__(self, target, args):
        self.url = target if "://" in target else "https://" + target
        self.host = urllib.parse.urlsplit(self.url).hostname or ""
        self.args, self.robots, self.last = args, None, 0.0

    def allowed(self, url):
        if self.robots is None:
            self.robots = urllib.robotparser.RobotFileParser()
            try:
                body = self.fetch(urllib.parse.urljoin(self.url, "/robots.txt"), check=False)
                self.robots.parse(body.splitlines())
            except Exception:
                self.robots.parse([])            # no robots.txt = nothing disallowed
        return self.robots.can_fetch(UA, url)

    def fetch(self, url, check=True):
        if check and not self.allowed(url):
            raise PermissionError("disallowed by robots.txt")
        wait = self.args.delay - (time.time() - self.last)
        if wait > 0:
            time.sleep(wait)
        self.last = time.time()
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,*/*;q=0.5"})
        with urllib.request.urlopen(req, timeout=self.args.timeout) as r:
            if "html" not in r.headers.get("Content-Type", "html") and check:
                return ""
            raw = r.read(2_000_000)
            return raw.decode(r.headers.get_content_charset() or "utf-8", "replace")

    def run(self):
        row = {"website": self.host, "emails": set(), "phones": set(), "pages": 0, "status": "ok"}
        try:
            home = self.fetch(self.url)
        except Exception as e:
            row["status"] = f"failed: {getattr(e, 'code', '') or type(e).__name__} {getattr(e, 'reason', e)}"[:80]
            return row
        pages, seen = [home], {self.url.rstrip("/")}
        for href, label in LINK.findall(home):
            if len(pages) > self.args.pages:
                break
            url = urllib.parse.urljoin(self.url, html.unescape(href))
            u = urllib.parse.urlsplit(url)
            same = (u.hostname or "").removeprefix("www.") == self.host.removeprefix("www.")
            if u.scheme in ("http", "https") and same and url.rstrip("/") not in seen \
                    and CONTACT_HINT.search(href + " " + re.sub(r"<[^>]+>", "", label)):
                seen.add(url.rstrip("/"))
                try:
                    pages.append(self.fetch(url))
                except Exception:
                    pass
        row["pages"] = len(pages)
        for page in pages:
            for a, b in CFEMAIL.findall(page):
                row["emails"].add(clean_email(decode_cfemail(a or b)))
            for tel in TEL.findall(page):
                row["phones"].add(clean_phone(html.unescape(tel), self.args.country_code))
            # Decode entities and JSON escapes BEFORE matching, so ">info@" never forms.
            text = html.unescape(page)
            text = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), text)
            text = re.sub(r"(?s)<(script|style)\b(?![^>]*ld\+json).*?</\1>", " ", text)
            for m in re.findall(r'mailto:([^"\'?>\s]+)', text):
                row["emails"].add(clean_email(m))
            text = re.sub(r"<[^>]+>", " ", text)      # tags become spaces: no gluing across elements
            for m in EMAIL.findall(text):
                row["emails"].add(clean_email(m))
            for local, dom in SPELLED.findall(text):
                dom = re.sub(r"\s*[\[({]\s*dot\s*[\])}]\s*|\s*\.\s*", ".", dom)
                row["emails"].add(clean_email(f"{local}@{dom}"))
        row["emails"].discard(None)
        row["phones"].discard(None)
        if self.args.same_domain:
            base = self.host.removeprefix("www.")
            row["emails"] = {e for e in row["emails"] if e.split("@")[1].endswith(base)}
        return row


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("file", help="text file with one domain or URL per line ('-' for stdin)")
    p.add_argument("--pages", type=int, default=5, help="contact/about pages to follow per site (default 5)")
    p.add_argument("--workers", type=int, default=5, help="sites in parallel (default 5)")
    p.add_argument("--delay", type=float, default=1.0, help="seconds between requests to one site (default 1)")
    p.add_argument("--timeout", type=float, default=15)
    p.add_argument("--country-code", default="", help="calling code for local numbers, e.g. 1, 44, 972")
    p.add_argument("--same-domain", action="store_true", help="keep only addresses on the site's own domain")
    p.add_argument("--json", action="store_true", help="JSON lines instead of CSV")
    args = p.parse_args()
    src = sys.stdin if args.file == "-" else open(args.file, encoding="utf-8")
    targets = list(dict.fromkeys(l.strip() for l in src if l.strip() and not l.startswith("#")))
    out = csv.writer(sys.stdout)
    if not args.json:
        out.writerow(["website", "emails", "phones", "pages_read", "status"])
    with ThreadPoolExecutor(args.workers) as pool:
        for row in pool.map(lambda t: Site(t, args).run(), targets):
            emails, phones = sorted(row["emails"]), sorted(row["phones"])
            if args.json:
                print(json.dumps({**row, "emails": emails, "phones": phones}, ensure_ascii=False), flush=True)
            else:
                out.writerow([row["website"], "; ".join(emails), "; ".join(phones), row["pages"], row["status"]])
                sys.stdout.flush()


if __name__ == "__main__":
    main()
