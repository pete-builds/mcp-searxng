# Demo: one research task, as an agent sees it

`bench/demo.py`, run 2026-11-20 against a self-hosted SearXNG (2026.11.15)
through this server over HTTP with a bearer token. Output unedited apart from
the server's own log line.

What it shows, in order:

1. **`search_deep` with `include_domains`** pulls three pages and keeps only
   primary sources: the RFC, the IETF drafts, Cloudflare's docs. Each result
   says how many engines returned it.
2. **`read_url`** reads the RFC as markdown, and **refuses the PDF with a code**
   instead of handing the agent two megabytes of binary decoded as text.
3. **`search_news` comes back `degraded=True`**: one engine answered, and its
   results are unrelated filler ("Adopt 815: Meet 'Diamond'"). A plain search
   wrapper returns the same junk with nothing to say it is junk. Here the agent
   is told, in the response, not to trust it.
4. **`read_url` refuses a cloud-metadata address** before any request is made.

```text

### search_deep, primary sources only
pages fetched 3/3; 8 results; engines ['bing', 'duckduckgo', 'yahoo']; degraded=False
  [2 engines] RFC 9114 - HTTP/3 - IETF Datatracker
      https://datatracker.ietf.org/doc/html/rfc9114
  [2 engines] PDF RFC 9114: HTTP/3 - Internet Engineering Task Force
      https://www.ietf.org/ietf-ftp/rfc/rfc9114.pdf
  [2 engines] draft-ietf-quic-http-29 - Hypertext Transfer Protocol Version 3 (HTTP/
      https://datatracker.ietf.org/doc/draft-ietf-quic-http/29/
  [2 engines] Hypertext Transfer Protocol Version 3 (HTTP/3)
      https://www.ietf.org/archive/id/draft-ietf-quic-http-34.html
  [2 engines] HTTP/3 (with QUIC) · Cloudflare Speed docs
      https://developers.cloudflare.com/speed/optimization/protocol/http3/

read_url https://datatracker.ietf.org/doc/html/rfc9114
  -> 166516 chars; "| RFC 9114 | HTTP/3 | June 2022 | | Bishop | Standards Track | \[Page\] | - Stream: - Internet Engineering Task Force (IETF) - RFC: - [9114](https://www.rfc-edi..."

read_url https://www.ietf.org/ietf-ftp/rfc/rfc9114.pdf
  -> UNSUPPORTED_CONTENT_TYPE: ReadError: application/pdf is not text; read_url only returns HTML, XML, JSON and plain te

read_url https://datatracker.ietf.org/doc/draft-ietf-quic-http/29/
  -> 162273 chars; "# Hypertext Transfer Protocol Version 3 (HTTP/3) draft-ietf-quic-http-29 The information below is for an old version of the document. | Document | Type | | This..."

### search_news, past week
degraded=True  engines ['bing news']
    Adopt 815: Meet 'Diamond'
    Amitabh Kant highlights India's electric car adoption reaching 8.8%, o
    Everpure: 3 Key Takeaways for Long-Term Investors

### refusals
  https://arxiv.org/pdf/1706.03762
    -> UNSUPPORTED_CONTENT_TYPE
  http://169.254.169.254/latest/meta-data/
    -> BLOCKED_URL
```
