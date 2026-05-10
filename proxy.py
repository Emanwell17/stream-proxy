"""
Clean Stream Proxy for dlhd.pk

Problem: Embedding the dlhd.pk iframe on another site shows "Access Denied"
         because the player checks document.referrer for an authorized domain.
         Also, it's full of ads.

Solution:
  1. Proxy the player HTML through our server
  2. Inject JS early that spoofs document.referrer -> dlhd.pk (bypasses domain check)
  3. Strip all ad scripts, popups, and banners from the HTML
  4. Serve a clean player your site can embed as a simple <iframe>

Usage:
  python proxy.py
  Then embed on your site:  <iframe src="http://localhost:8080/watch/40"></iframe>
  Or open in browser:       http://localhost:8080
"""

import re
import requests
from flask import Flask, Response, request
from urllib.parse import quote, unquote, urljoin

app = Flask(__name__)
import os
PORT = int(os.environ.get("PORT", 8080))
BASE = "https://dlhd.pk"

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
})

AD_DOMAINS = [
    "xadsmart.com", "adsco.re", "jnbhi.com", "nicercurator.com",
    "strobedturtlet.com", "histats.com", "adexchangerapid.com",
    "dtscout.com", "dtscdn.com", "rtmark.net", "mrktmtrcs.net",
    "zeotap.com", "onaudience.com", "crwdcntrl.net", "jackersbrown.cfd",
    "tomefuldunch.cfd", "heliobedlamp.com", "washeddignifybanjara.cfd",
    "chazanboxiana.cyou", "usrpubtrk.com", "jcphi.com",
    "unclingyurta.click", "offletsoroche.life", "adnxs.com",
    "doubleclick.net", "googlesyndication.com", "freewheel.tv",
    "adbanner", "adserv", "popunder", "popcash", "popads",
]

# Injected at the very top of <head> — runs before any other script
# Spoofs document.referrer so the player thinks it's on dlhd.pk
SPOOF_JS = """<script>
(function(){
  // Spoof referrer so domain check passes
  try {
    Object.defineProperty(document, 'referrer', {
      configurable: true,
      get: function(){ return 'https://dlhd.pk/'; }
    });
  } catch(e){}

  // Spoof parent/top location access (some players check this)
  try {
    Object.defineProperty(window, 'top', {
      configurable: true,
      get: function(){ return window; }
    });
  } catch(e){}

  // Block all popup/redirect attempts
  window.open = function(){ return null; };
  window.onbeforeunload = null;

  // Remove "Access Denied" overlays if they appear
  function cleanDenied(){
    document.querySelectorAll('*').forEach(function(el){
      var t = (el.innerText || el.textContent || '');
      if(t.includes('Access Denied') || t.includes('not available on your domain')){
        if(el.children.length === 0 || el.tagName === 'DIV'){
          el.style.display = 'none';
        }
      }
    });
  }
  document.addEventListener('DOMContentLoaded', cleanDenied);
  setTimeout(cleanDenied, 500);
  setTimeout(cleanDenied, 1500);

  // Block new tab link clicks
  document.addEventListener('click', function(e){
    var t = e.target;
    while(t){ if(t.tagName==='A'){ t.target=''; t.rel=''; } t=t.parentElement; }
  }, true);
})();
</script>"""

AD_CSS = """<style>
[id*="ad"],[class*="ad-"],[class*="ads-"],[class*="banner"],
[id*="popup"],[class*="popup"],[id*="overlay"]:not([id*="player"]),
[id*="interstitial"],[class*="interstitial"],[class*="sponsor"],
[class*="promo"],[class*="advert"],[id*="advert"],ins,
#adbanner, .adbanner { display:none!important; }
</style>"""


def strip_ads(html: str) -> str:
    def is_ad(text):
        return any(d in text for d in AD_DOMAINS)

    def drop_if_ad(m):
        return "" if is_ad(m.group(0)) else m.group(0)

    # Remove external ad scripts
    html = re.sub(r'<script[^>]+src=["\'][^"\']*["\'][^>]*>.*?</script>',
                  drop_if_ad, html, flags=re.I|re.DOTALL)
    html = re.sub(r'<script[^>]+src=["\'][^"\']*["\'][^>]*/?>',
                  drop_if_ad, html, flags=re.I)

    # Remove inline scripts with ad/popup references
    def drop_inline(m):
        c = m.group(0)
        if is_ad(c):
            return ""
        # Remove popup/redirect scripts
        if re.search(r'window\.open\s*\(|\.open\s*\(|popunder|pop\(|new tab', c, re.I):
            return ""
        # Remove domain-check scripts (they show "Access Denied")
        if re.search(r'access.?denied|not.?available|authorized.?web|allowed_domain|referrer.*dlhd|dlhd.*referrer', c, re.I):
            return ""
        return c
    html = re.sub(r'<script[^>]*>.*?</script>', drop_inline, html, flags=re.I|re.DOTALL)

    # Remove ad iframes
    html = re.sub(r'<iframe[^>]*>.*?</iframe>', drop_if_ad, html, flags=re.I|re.DOTALL)

    # Remove <ins> ad slots
    html = re.sub(r'<ins\b[^>]*>.*?</ins>', "", html, flags=re.I|re.DOTALL)

    return html


def fix_urls(html: str, base_url: str) -> str:
    """Make all relative URLs absolute so assets load correctly."""
    def fix(m):
        attr, val = m.group(1), m.group(2)
        if val.startswith(("http", "data:", "blob:", "//", "#", "javascript")):
            return m.group(0)
        return f'{attr}="{urljoin(base_url, val)}"'
    return re.sub(r'(src|href|action)=["\']([^"\']+)["\']', fix, html, flags=re.I)


def get_player_url(channel_id: str) -> tuple[str, str]:
    """Follow stream page -> extract real player iframe URL."""
    stream_page  = f"{BASE}/stream/stream-{channel_id}.php"
    watch_referer = f"{BASE}/watch.php?id={channel_id}"
    try:
        r = SESSION.get(stream_page, headers={"Referer": watch_referer}, timeout=10)
        m = re.search(r'<iframe[^>]+src=["\']([^"\']+)["\']', r.text, re.I)
        if m:
            player_url = m.group(1)
            if player_url.startswith("//"):
                player_url = "https:" + player_url
            print(f"  [player] {player_url[:80]}")
            return player_url, stream_page
    except Exception as e:
        print(f"  [!] {e}")
    return stream_page, watch_referer


def build_clean_page(html: str, page_url: str) -> str:
    """Strip ads, fix URLs, inject domain spoof + ad CSS."""
    html = strip_ads(html)
    html = fix_urls(html, page_url)

    # Inject spoof JS at the very start of <head> (before any other script runs)
    if "<head>" in html:
        html = html.replace("<head>", "<head>" + SPOOF_JS + AD_CSS, 1)
    elif "<HEAD>" in html:
        html = html.replace("<HEAD>", "<HEAD>" + SPOOF_JS + AD_CSS, 1)
    else:
        html = SPOOF_JS + AD_CSS + html

    return html


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/watch/<channel_id>")
def watch(channel_id):
    print(f"\n[*] Channel {channel_id}")
    player_url, player_referer = get_player_url(channel_id)

    try:
        r = SESSION.get(player_url, headers={
            "Referer": player_referer,
            "Origin":  BASE,
        }, timeout=12)
        r.raise_for_status()
        html = r.text
    except Exception as e:
        return f"<h2 style='color:red;font-family:sans-serif'>Error loading stream: {e}</h2>", 502

    clean = build_clean_page(html, player_url)
    print(f"  [ok] Serving clean player")
    return Response(clean, content_type="text/html",
                    headers={"Access-Control-Allow-Origin": "*",
                             "X-Frame-Options": "ALLOWALL"})


@app.route("/")
def home():
    return """<!DOCTYPE html>
<html><head><title>Clean Stream</title><style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#111;color:#eee;font-family:Arial,sans-serif;
     display:flex;flex-direction:column;align-items:center;justify-content:center;height:100vh}
h2{margin-bottom:8px}p{color:#888;font-size:13px;margin-bottom:24px;text-align:center}
.row{display:flex;gap:10px;margin-bottom:16px}
input{padding:12px;font-size:16px;width:220px;border-radius:6px;border:none;outline:none}
button{padding:12px 28px;font-size:16px;background:#e55000;color:#fff;border:none;border-radius:6px;cursor:pointer}
button:hover{background:#f70}
.embed-box{display:none;margin-top:20px;background:#222;padding:14px;border-radius:8px;max-width:600px;width:90%}
.embed-box code{font-size:12px;color:#0f0;word-break:break-all}
iframe{display:block;margin-top:20px;border:none;background:#000}
</style></head><body>
<h2>Ad-Free Stream Player</h2>
<p>Find a working channel on dlhd.pk and enter its ID below</p>
<div class="row">
  <input id="cid" type="number" placeholder="Channel ID (e.g. 1)" autofocus/>
  <button onclick="go()">Watch</button>
</div>
<div class="embed-box" id="ebox">
  <p style="color:#aaa;margin-bottom:8px">Embed code for your website:</p>
  <code id="ecode"></code>
</div>
<iframe id="player" width="854" height="480" allowfullscreen></iframe>
<script>
function go(){
  var v = document.getElementById('cid').value;
  if(!v) return;
  var src = '/watch/' + v;
  document.getElementById('player').src = src;
  var full = window.location.origin + '/watch/' + v;
  document.getElementById('ecode').innerText =
    '<iframe src="' + full + '" width="854" height="480" allowfullscreen></iframe>';
  document.getElementById('ebox').style.display = 'block';
}
document.getElementById('cid').onkeydown = function(e){ if(e.key==='Enter') go(); };
</script>
</body></html>"""


if __name__ == "__main__":
    print(f"\n[+] Clean stream proxy running!")
    print(f"    Open: http://localhost:{PORT}\n")
    app.run(host="0.0.0.0", port=PORT, threaded=True, debug=False, use_reloader=False)
