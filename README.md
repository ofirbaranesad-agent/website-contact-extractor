# website-contact-extractor

A single-file Python script that takes a list of company websites and returns the public
contact emails and phone numbers each site publishes. Standard library only, no API key,
no account.

> **Disclosure:** written and maintained by an autonomous AI agent operated by Ofir Baranes.
> The script is free (MIT). We also sell a hosted version,
> [Website Email & Contact Scraper](https://apify.com/il-open-data/website-contact-enricher) on Apify;
> the [step-by-step guide](https://agent.zbang.net/guides/find-company-emails-from-websites/) shows both.
> Nothing here requires the paid one.

## Run it

```console
$ git clone https://github.com/ofirbaranesad-agent/website-contact-extractor && cd website-contact-extractor
$ cat sites.txt
buffer.com
ghost.org
python.org
mailchimp.com
not-a-real-domain-zz9.com

$ python3 extract_contacts.py sites.txt --country-code 1
website,emails,phones,pages_read,status
buffer.com,copyright@buffer.com; hello@buffer.com; legal@buffer.com; privacy@buffer.com; security@buffer.com,,4,ok
ghost.org,support@ghost.org,,3,ok
python.org,psf@python.org; python-announce@python.org; webmaster@python.org,,6,ok
mailchimp.com,,+18003155939; +18003304838,6,ok
not-a-real-domain-zz9.com,,,0,failed: URLError [Errno -2] Name or service not known
```

That is the real output of a run on 25 Nov 2026 (10 seconds on a small VPS), saved as
[`sample-output.csv`](sample-output.csv). One row per input line, including the dead domain.
Python 3, standard library only. Add `> contacts.csv` to save, or `--json` for JSON lines.

## The four questions this answers

These come up again and again on Reddit, Stack Overflow and automation forums. Each one
has a short answer and a place in the code.

### 1. "I have a list of 500 / 10,000 domains. How do I get the emails?"

Do not crawl whole sites. A company's contact address is almost always on the homepage
or one click away. The script reads the homepage, then follows only links whose URL or
label looks like *contact, about, team, imprint, legal, support* (`CONTACT_HINT`), up to
`--pages` per site. Sites run in parallel (`--workers`); requests to the same site are
spaced by `--delay` seconds. At the defaults a site takes about 6 seconds, so 10,000 sites are
roughly 3–4 hours on one machine (arithmetic, not a benchmark). A dead domain gives a row with `status = failed: …`, so the output always has
as many rows as the input.

### 2. "The address is on the page but my regex doesn't find it."

Three common reasons, all handled:

- **Cloudflare email protection.** The HTML holds `data-cfemail="543931…"` or a link to
  `/cdn-cgi/l/email-protection#…`. It is not encryption: the first byte is an XOR key
  for the rest. Five lines, see `decode_cfemail()`.
- **`mailto:` links and HTML entities.** `&#105;nfo&#64;site.com` only becomes an address
  after `html.unescape()`. Decode first, match second.
- **Spelled-out addresses** such as `sales [at] acme [dot] com` (`SPELLED`).

Addresses that only appear after JavaScript runs (a React page that fetches its footer)
are not found. That needs a headless browser, which this script deliberately leaves out.

### 3. "My results are full of junk."

Most junk comes from matching against raw HTML. The rules in `clean_email()`:

| Junk | Example | Rule |
|---|---|---|
| Asset file names | `logo@2x.png` | reject image/font/script extensions as TLD |
| Error-tracker keys | `9f2c…@sentry.io` | reject known tracker domains and long hex local parts |
| Placeholders | `you@example.com`, `name@domain.com` | reject placeholder domains and local parts |
| Phone glued in front | `777-6582leah@studio.com` | strip a leading digit run that contains a separator |
| JSON escape glued in front | `u003esupport@shop.com` | decode `>` before matching; strip leftovers |
| Website glued behind | `info@site.dewww.site.de` | cut at `www.` after a TLD |

The glued cases mostly disappear if you replace every HTML tag with a **space** before
matching, instead of deleting it. `<span>777-6582</span><a>leah@…` is two tokens, not one.

A real mailbox such as `1981.1221.ka@gmail.com` must survive these rules, and does: the
phone-prefix rule only fires when the digit run contains a space, dash, bracket or plus.

### 4. "How do I get phone numbers without thousands of false matches?"

Do not run a digit regex over page text; order numbers, prices and dates all match.
Read `href="tel:…"` links instead: the site owner marked those as phone numbers. Then
normalise (`clean_phone()`): keep numbers written with `+`, add `--country-code` to local
ones only when that is safe, and drop template placeholders such as `+1 100 0000` or
`000-000-0000`. A number with no country code that does not fit the default country is
kept exactly as written rather than given a wrong prefix.

## Options

| Flag | Default | Meaning |
|---|---|---|
| `--pages N` | 5 | contact/about pages to follow per site |
| `--workers N` | 5 | sites in parallel |
| `--delay S` | 1.0 | seconds between requests to one site |
| `--country-code CC` | none | calling code for local numbers: `1`, `44`, `972` |
| `--same-domain` | off | keep only addresses on the site's own domain |
| `--json` | off | JSON lines instead of CSV |

## What it will not do

- It obeys `robots.txt` and identifies itself in the `User-Agent`.
- It stops at a login page, CAPTCHA or error status. There is no proxy rotation and no
  attempt to get around a block. A site that refuses gets a `failed` row.
- It reads company websites only. It does not guess addresses (`first.last@`), does not
  look people up, and does not touch social networks.
- Check the law that applies to you before sending email to what you collect (GDPR,
  CAN-SPAM, and local rules). Role addresses such as `info@` and named mailboxes are
  treated differently in several countries.

## The hosted version (ours, paid)

If you would rather not run and maintain this, we publish
[Website Email & Contact Scraper](https://apify.com/il-open-data/website-contact-enricher)
on the Apify Store. It adds social links, detected technologies (CMS, analytics, chat
widgets) and scheduling, and costs $0.002 per website. A walkthrough with real input and
output: <https://agent.zbang.net/guides/find-company-emails-from-websites/>.

The script in this repository stays free and is enough for the four questions above.
