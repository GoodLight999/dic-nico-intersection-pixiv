# -*- coding: utf-8 -*-
import urllib.request
import urllib.parse
import urllib.error
import re
import html
import concurrent.futures
import time
import os

# Create public directory if it doesn't exist
os.makedirs("public", exist_ok=True)

# Logging function that writes to public/log.txt
def log(msg):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    formatted = f"[{timestamp}] {msg}"
    print(formatted)
    try:
        with open("public/log.txt", "a", encoding="utf-8") as f:
            f.write(formatted + "\n")
    except Exception:
        pass

# Clear log file at startup
try:
    with open("public/log.txt", "w", encoding="utf-8") as f:
        f.write("")
except Exception:
    pass

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
}

def fetch_url(url, retries=5):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=15) as response:
                return response.read().decode('utf-8')
        except urllib.error.HTTPError as e:
            if e.code == 429:
                if i == retries - 1:
                    raise e
                wait_time = (i + 1) * 10
                log(f"[RateLimit] HTTP 429 on {url}. Retrying in {wait_time}s...")
                time.sleep(wait_time)
            else:
                if i == retries - 1:
                    raise e
                time.sleep(1 + i)
        except Exception as e:
            if i == retries - 1:
                raise e
            time.sleep(1 + i)

# ----------------- Nico Nico Pedia Scraper -----------------

def katakana_to_hiragana(text):
    result = []
    for c in text:
        code = ord(c)
        if 0x30A1 <= code <= 0x30F6:
            result.append(chr(code - 0x60))
        else:
            result.append(c)
    return "".join(result)

# Fallback character list (used only if index page fetch fails)
CHARS_FALLBACK = [
    "ア", "イ", "ウ", "エ", "オ", "カ", "キ", "ク", "ケ", "コ",
    "サ", "シ", "ス", "セ", "ソ", "タ", "チ", "ツ", "テ", "ト",
    "ナ", "ニ", "ヌ", "ネ", "ノ", "ハ", "ヒ", "フ", "ヘ", "ホ",
    "マ", "ミ", "ム", "メ", "モ", "ヤ", "ユ", "ヨ", "ラ", "リ",
    "ル", "レ", "ロ", "ワ", "ヲ", "ン", "記", "英", "数",
]

def get_nico_char_urls():
    """Fetch the Nico Nico index page and discover character categories with their actual URLs.
    This replicates the Haskell code's behavior:
      scrapeURL "https://dic.nicovideo.jp/m/a/a"
        (texts $ "div" @: [hasClass "st-box_contents"] // "table" // "tr" // "td" // "a")
    By using the actual hrefs from the page, we avoid URL encoding issues and
    automatically adapt to any changes in the site's category structure.
    """
    log("[Nico] Fetching index page to discover character categories...")
    try:
        index_html = fetch_url("https://dic.nicovideo.jp/m/a/a")
        # Extract all /m/yp/a/ links with article counts: href="/m/yp/a/ENCODED/1-"
        # These are the actual crawlable URLs for each character category.
        # The site uses percent-encoded katakana in the URLs.
        yp_links = re.findall(
            r'<a\s+href="(/m/yp/a/([^/]+)/1-)"[^>]*>\((\d+)\)</a>',
            index_html
        )
        if not yp_links:
            log("[Nico] Could not find any /m/yp/a/ links on index page")
            return None
        result = []
        for full_href, encoded_char, count in yp_links:
            decoded = urllib.parse.unquote(encoded_char)
            # Base URL is the yp path without the /1- suffix
            base_url = f"https://dic.nicovideo.jp/m/yp/a/{encoded_char}"
            result.append((decoded, base_url))
            log(f"[Nico] Discovered: {decoded} ({count} articles) -> {base_url}")
        log(f"[Nico] Discovered {len(result)} character categories from index page")
        return result
    except Exception as e:
        log(f"[Nico] Failed to fetch index page: {e}")
        return None

def parse_nico_page(html_content):
    idx = html_content.find('class="article"')
    if idx == -1:
        return []
    article_html = html_content[idx:]
    
    ul_idx = article_html.find('<ul>')
    if ul_idx == -1:
        return []
    article_html = article_html[ul_idx:]
    
    items = article_html.split('<li>')
    results = []
    for item in items[1:]:
        end_idx = item.find('</li>')
        if end_idx != -1:
            item = item[:end_idx]
            
        link_match = re.search(r'<a href="/a/[^"]+">([^<]+)</a>', item)
        if not link_match:
            continue
        word = html.unescape(link_match.group(1)).strip()
        
        yomi_match = re.search(r'\(([^)]+)\)', item[link_match.end():])
        if not yomi_match:
            continue
        yomi = html.unescape(yomi_match.group(1)).strip()
        
        is_redirect = "1" if "(リダイレクト)" in item else "0"
        results.append((word, yomi, is_redirect))
    return results

def crawl_nico_char(char, base_url=None):
    """Crawl one character category from Nico Nico Pedia.
    If base_url is provided (from dynamic index page discovery), use it directly.
    Otherwise, construct the URL with percent-encoding (fallback).
    """
    if base_url is None:
        quoted = urllib.parse.quote(char)
        base_url = f"https://dic.nicovideo.jp/m/yp/a/{quoted}"
    log(f"[Nico] Crawling character: {char} ({base_url})...")
    try:
        first_page = fetch_url(base_url)
    except Exception as e:
        log(f"[Nico] Error fetching first page for {char}: {e}")
        return []
    
    entries = parse_nico_page(first_page)
    
    # Extract pager links from actual hrefs on the page.
    # Instead of constructing URLs from percent-encoded chars, we read the actual
    # href paths the server provides, which handles encoding correctly.
    # Match: href="/m/yp/a/SOMETHING/NUMBER-"
    pager_matches = re.findall(r'href="(/m/yp/a/[^/]+/(\d+)-)"', first_page)
    if not pager_matches:
        log(f"[Nico] Done {char} (1 page, {len(entries)} entries)")
        return entries
    
    # Get the path prefix from the first pager link and compute max offset
    path_prefix = pager_matches[0][0].rsplit('/', 1)[0]  # e.g., /m/yp/a/%E3%82%A2
    offsets = [int(m[1]) for m in pager_matches]
    max_offset = max(offsets)
    
    # Generate all page URLs using the discovered path prefix
    urls = [f"https://dic.nicovideo.jp{path_prefix}/{offset}-"
            for offset in range(51, max_offset + 1, 50)]
    log(f"[Nico] {char}: Fetching {len(urls)} pager pages in parallel...")
    
    # Fetch pager pages in parallel
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(fetch_url, url): url for url in urls}
        for future in concurrent.futures.as_completed(futures):
            url = futures[future]
            try:
                page_html = future.result()
                entries.extend(parse_nico_page(page_html))
            except Exception as e:
                log(f"[Nico] Error fetching {url}: {e}")
                
    log(f"[Nico] Done {char} (total {len(entries)} entries)")
    return entries

def scrape_nico():
    log("Starting Nico Nico Pedia scrape...")
    
    # Load existing entries for merge (prevents data loss when categories fail)
    existing_entries = {}
    if os.path.exists("public/nico-raw.txt"):
        with open("public/nico-raw.txt", "r", encoding="utf-8") as f:
            for line in f:
                parts = line.strip().split("\t")
                if len(parts) == 3:
                    existing_entries[(parts[0], parts[1])] = parts[2]
        log(f"Loaded {len(existing_entries)} existing entries from nico-raw.txt")
    
    # Discover character categories dynamically from the index page
    # (replicates: scrapeURL "https://dic.nicovideo.jp/m/a/a" ...)
    char_urls = get_nico_char_urls()
    if char_urls is None:
        log("[Nico] Falling back to hardcoded character list")
        char_urls = [(c, f"https://dic.nicovideo.jp/m/yp/a/{urllib.parse.quote(c)}")
                     for c in CHARS_FALLBACK]
    
    all_entries = []
    
    # Crawl characters in parallel (3 concurrent)
    def crawl_item(item):
        char, url = item
        return crawl_nico_char(char, url)
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        results = executor.map(crawl_item, char_urls)
        for entries in results:
            all_entries.extend(entries)
    
    # Deduplicate new entries and convert yomi to hiragana
    new_entries = {}
    for word, yomi, redir in all_entries:
        hiragana_yomi = katakana_to_hiragana(yomi)
        key = (word, hiragana_yomi)
        if key not in new_entries or new_entries[key] == "1":
            new_entries[key] = redir
    
    log(f"Total Nico Nico entries scraped: {len(new_entries)}")
    
    # Merge: start with existing data, update with new scraped data.
    # This ensures that if some categories fail (e.g., 記/英/数 return HTTP 400),
    # their previously scraped entries are preserved rather than lost.
    merged = dict(existing_entries)
    merged.update(new_entries)
    log(f"Total entries after merge: {len(merged)}")
    
    with open("public/nico-raw.txt", "w", encoding="utf-8") as f:
        for (word, yomi), redir in sorted(merged.items()):
            f.write(f"{word}\t{yomi}\t{redir}\n")
    log("Wrote public/nico-raw.txt")

def scrape_nico_special_yomi():
    log("Scraping Nico Nico Special Yomi (ID 4652210)...")
    url = "https://dic.nicovideo.jp/id/4652210"
    try:
        html_content = fetch_url(url)
        idx = html_content.find('class="article"')
        if idx == -1:
            log("Failed to find article class in Special Yomi page")
            return
        article_html = html_content[idx:]
        
        items = re.findall(r'<li>(.*?)</li>', article_html, re.DOTALL)
        words = []
        for item in items:
            txt = re.sub(r'<[^>]+>', '', item)
            txt = html.unescape(txt).strip()
            if txt:
                word = re.split(r'[（\(]', txt)[0].strip()
                if word:
                    words.append(word)
                    
        seen = set()
        unique_words = []
        for w in words:
            if w not in seen:
                seen.add(w)
                unique_words.append(w)
                
        with open("public/nico-special-yomi.txt", "w", encoding="utf-8") as f:
            for w in unique_words:
                f.write(f"{w}\n")
        log(f"Wrote public/nico-special-yomi.txt ({len(unique_words)} words)")
    except Exception as e:
        log(f"Error scraping Special Yomi: {e}")

# ----------------- Pixiv Sitemap Scraper -----------------

def initialize_pixiv_raw():
    # If pixiv-raw.txt doesn't exist, try to populate it from the existing google dictionary
    if not os.path.exists("public/pixiv-raw.txt") and os.path.exists("public/dic-nico-intersection-pixiv-google.txt"):
        log("Initializing pixiv-raw.txt from existing google dictionary...")
        words = set()
        with open("public/dic-nico-intersection-pixiv-google.txt", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("#") or not line.strip():
                    continue
                parts = line.split("\t")
                if len(parts) >= 2:
                    words.add(parts[1].strip())
        with open("public/pixiv-raw.txt", "w", encoding="utf-8") as f:
            for w in sorted(list(words)):
                f.write(f"{w}\n")
        log(f"Initialized pixiv-raw.txt with {len(words)} words.")

def scrape_pixiv():
    log("Starting Pixiv Sitemap scrape...")
    initialize_pixiv_raw()
    
    # Load existing words
    existing_words = set()
    if os.path.exists("public/pixiv-raw.txt"):
        with open("public/pixiv-raw.txt", "r", encoding="utf-8") as f:
            for line in f:
                w = line.strip()
                if w:
                    existing_words.add(w)
                    
    # Load sitemap cache
    cache = {}
    cache_exists = os.path.exists("public/pixiv-sitemap-cache.txt")
    if cache_exists:
        with open("public/pixiv-sitemap-cache.txt", "r", encoding="utf-8") as f:
            for line in f:
                parts = line.strip().split("\t")
                if len(parts) == 2:
                    cache[parts[0]] = parts[1]
                    
    try:
        main_xml = fetch_url("https://dic.pixiv.net/sitemap.xml")
        
        # Parse sitemap parts and their lastmod dates
        sitemap_blocks = re.findall(r'<sitemap>(.*?)</sitemap>', main_xml, re.DOTALL)
        
        sitemaps_to_crawl = []
        new_cache = {}
        
        for block in sitemap_blocks:
            loc_match = re.search(r'<loc>(.*?)</loc>', block)
            lastmod_match = re.search(r'<lastmod>(.*?)</lastmod>', block)
            if loc_match and lastmod_match:
                url = loc_match.group(1).strip()
                lastmod = lastmod_match.group(1).strip()
                new_cache[url] = lastmod
                
                # If cache did NOT exist, we assume all current sitemaps are already crawled
                # (since we loaded words from the existing dictionary)
                if not cache_exists:
                    # Do not crawl, just populate the cache
                    pass
                elif cache.get(url) != lastmod:
                    sitemaps_to_crawl.append(url)
                    
        log(f"Total sitemaps in sitemap.xml: {len(new_cache)}")
        log(f"Sitemaps to crawl (changed or new): {len(sitemaps_to_crawl)}")
        
        if sitemaps_to_crawl:
            words_from_crawl = set()
            
            def process_sub_sitemap(url):
                log(f"[Pixiv] Fetching {url}...")
                try:
                    xml = fetch_url(url)
                    locs = re.findall(r'<loc>https://dic.pixiv.net/a/([^<]+)</loc>', xml)
                    local_words = []
                    for loc in locs:
                        word = urllib.parse.unquote(loc).strip()
                        if word:
                            local_words.append(word)
                    log(f"[Pixiv] Done {url}: found {len(local_words)} words.")
                    return local_words
                except Exception as e:
                    log(f"[Pixiv] Error fetching sub-sitemap {url}: {e}")
                    if url in new_cache:
                        del new_cache[url]
                    return []
                    
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                results = executor.map(process_sub_sitemap, sitemaps_to_crawl)
                for w_list in results:
                    words_from_crawl.update(w_list)
                    
            existing_words.update(words_from_crawl)
            log(f"Added {len(words_from_crawl)} new words from crawled sitemaps.")
            
        # Save updated word list
        with open("public/pixiv-raw.txt", "w", encoding="utf-8") as f:
            for w in sorted(list(existing_words)):
                f.write(f"{w}\n")
        log(f"Total Pixiv entries saved: {len(existing_words)}")
        
        # Save updated cache
        with open("public/pixiv-sitemap-cache.txt", "w", encoding="utf-8") as f:
            for url, lastmod in sorted(new_cache.items()):
                f.write(f"{url}\t{lastmod}\n")
        log("Wrote public/pixiv-sitemap-cache.txt")
        
    except Exception as e:
        log(f"Error scraping Pixiv sitemap: {e}")

# ----------------- Main Execution -----------------

if __name__ == "__main__":
    start_time = time.time()
    
    scrape_nico_special_yomi()
    scrape_pixiv()
    scrape_nico()
    
    log(f"Finished all scrapes in {time.time() - start_time:.2f} seconds.")
