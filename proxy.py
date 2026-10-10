#!/usr/bin/env python3
"""Web proxy that gives an old SharkRF openSPOT's status page working BrandMeister rows and buttons.

The openSPOT web UI (main.min.js) calls BrandMeister's retired v1 API from the *browser*:
    https://api.brandmeister.network/v1.0/repeater/?action=PROFILE&q=<DMR ID>   (and write calls)
This proxy serves the UI unchanged from the device except for two URLs, which are rewritten to
/bmapi/ on the proxy. /bmapi/ answers in the v1 shape the page expects, using BrandMeister API v2.
Browse to http://<proxy>:<port>/ instead of the device. Stdlib only.

API keys: the openSPOT firmware only stores short keys, but v2 keys are long JWTs. In the openSPOT's
"API key" field enter a short *key name* (letters, digits, - and _). The proxy reads the real token
from <keys-dir>/<name> and sends it to BrandMeister; the token never reaches the browser or the device.
Reading the profile needs no key. The reflector change has no v2 equivalent and reports an error.

/bm serves a standalone, WPSD-style BrandMeister manager page (static and dynamic TGs with names, add/remove,
drop dynamic, drop call) that uses the same /bmapi/ endpoints.
"""
import argparse
import base64
import gzip
import http.client
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

REFRESH_SEC = 60  # how often the Status page reloads the BrandMeister rows
JS_REWRITES = [
    (b'srf_bmapiurl="https://api.brandmeister.network/v1.0/repeater/"', b'srf_bmapiurl="/bmapi/"'),
    (b'"https://api.brandmeister.network/v1.0/auth/validate-token.php?token="', b'"/bmapi/validate?token="'),
    # The old "Last heard" page (?page=lh&Master=<id>) now answers 403. The new site has no per-master view,
    # but #/lh?ContextID=<device id> shows one hotspot's calls, and the page already knows its DMR ID.
    (b'"https://brandmeister.network/?page=lh&Master="+a.split("/")[1].split(" ")[0]',
     b'"https://brandmeister.network/#/lh?ContextID="+srf_bm_dmrid'),
    # BrandMeister's auto-static TG is not in the static list; show it after the static TGs on the Status page
    # and on the Connectors page (there without a remove link, as it cannot be removed through the API).
    (b'});$("#page-status-bm-stgs").html(e==""?"-":e)',
     b'});if(c.autoStatic){e+=(e?", ":"")+c.autoStatic.talkgroup+" (auto)"}$("#page-status-bm-stgs").html(e==""?"-":e)'),
    (b'});$("#page-connectors-bmm-stgs").html(e==""?"-":e)',
     b'});if(c.autoStatic){e+=(e?", ":"")+c.autoStatic.talkgroup+" (auto)"}'
     b'$("#page-connectors-bmm-stgs").html(e==""?"-":e)'),
    # The BM Manager menu entry follows the page's own rule for showing its BrandMeister rows and manager:
    # active connector Homebrew/MMDVM with a BrandMeister server selected.
    (b"function srf_status_update_bm_rows_visibility(){",
     b'function srf_status_update_bm_rows_visibility(){$("#menu-item-bm")'
     b'.toggleClass("hidden",!(srf_connectors_active==2&&srf_is_bm_server_selected()));'),
    # The page loads the BrandMeister rows once, when the connection changes or after its own writes, so TGs
    # changed elsewhere (or a profile fetch that came back empty) stay stale. Reload them periodically while
    # the Status page is showing.
    (b"function srf_handle_status_connected_to_change(){",
     b'setInterval(function(){if(!document.hidden&&$("#page-status").hasClass("active")'
     b'&&!$(".page-status-bm-rows").hasClass("hidden")){srf_handle_status_connected_to_change()}},'
     + str(REFRESH_SEC * 1000).encode() + b");function srf_handle_status_connected_to_change(){"),
]
# The "Get key" button opens brandmeister.network/?page=profile-api (now 403). Send it to the proxy's own
# help page, which explains the key name and links to BrandMeister's new API Keys page.
HTML_REWRITES = [
    (b"window.open('https://brandmeister.network/?page=profile-api', '_blank')",
     b"window.open('/help/api-key', '_blank')"),
    # A menu entry for the proxy's own BrandMeister manager page, between Settings and Logout.
    (b'<li class="pure-menu-item"><a href="javascript:srf_logout()"',
     b'<li class="pure-menu-item hidden" id="menu-item-bm"><a href="/bm" class="pure-menu-link" id="header-menu-item-bm">'
     b'BM Manager</a></li>\n'
     b'<li class="pure-menu-item"><a href="javascript:srf_logout()"'),
]
REWRITES = {"main.min.js_gz": JS_REWRITES, "main.html_gz": HTML_REWRITES}
HELP_FRAME = """<!doctype html><html lang="{lang}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title>
<style>body{font:16px/1.5 system-ui,sans-serif;max-width:42rem;margin:2rem auto;padding:0 1rem;color:#222}
code{background:#eee;padding:.1em .35em;border-radius:3px}pre{background:#eee;padding:.7em;overflow-x:auto}
@media(prefers-color-scheme:dark){body{background:#16181c;color:#e4e6ea}code,pre{background:#2a2e35}a{color:#7ab7ff}}</style>
</head><body>
<p style="text-align:right"><a href="?lang={other}">{switch}</a></p>
{body}
</body></html>"""
# (title, switch-link text, body) per language. {keys_dir} is filled in when the page is served.
HELP_TEXT = {
    "en": ("BrandMeister API key", "Suomeksi", """<h1>BrandMeister API key</h1>
<p>This openSPOT page is served through a small proxy that talks to the current BrandMeister API (v2).
The old key page on brandmeister.network is gone, and the openSPOT can only store a short key,
so the real key stays on the proxy server and this field holds only a <b>key name</b>.</p>
<h2>1. Create a key</h2>
<p>Log in at <a href="https://brandmeister.network/#/profile/api" target="_blank" rel="noopener">
brandmeister.network &rarr; profile &rarr; API Keys</a> and create a key. It is a long token that
starts with <code>eyJ</code>. Anyone holding it can change your hotspots' talkgroups, so keep it private.</p>
<h2>2. Put it on the proxy server</h2>
<p>As root-only files in <code>{keys_dir}</code>, one file per key, the file name being the key name
(letters, digits, <code>-</code> and <code>_</code>, up to 32 characters). Type the token there yourself:</p>
<pre>sudo -u openspotproxy tee {keys_dir}/mykey &gt;/dev/null
sudo chmod 600 {keys_dir}/mykey</pre>
<p>Paste the token, press Enter, then Ctrl-D.</p>
<h2>3. Enter the key name here</h2>
<p>Type <code>mykey</code> (the file name, not the token) in the <b>API key</b> field, press Save, then
Check key. Pick a name that is hard to guess: anyone on your network who knows it can use the key
through this proxy.</p>
<h2>What works</h2>
<p>Add and remove static talkgroups, drop dynamic talkgroups, drop the current call. Changing the
reflector is not possible, because BrandMeister API v2 has no such call.</p>"""),
    "fi": ("BrandMeisterin API-avain", "English", """<h1>BrandMeisterin API-avain</h1>
<p>Tämä openSPOT-sivu tulee pienen välityspalvelimen (proxyn) kautta, joka keskustelee BrandMeisterin
nykyisen API:n (v2) kanssa. BrandMeisterin vanha avainsivu on poistunut, eikä openSPOT pysty tallentamaan
kuin lyhyen avaimen. Siksi oikea avain pysyy proxy-palvelimella, ja tähän kenttään kirjoitetaan vain
<b>avaimen nimi</b>.</p>
<h2>1. Luo avain</h2>
<p>Kirjaudu osoitteessa <a href="https://brandmeister.network/#/profile/api" target="_blank" rel="noopener">
brandmeister.network &rarr; profiili &rarr; API Keys</a> ja luo avain. Se on pitkä merkkijono, joka alkaa
merkeillä <code>eyJ</code>. Avaimen haltija voi muuttaa hotspottiesi puheryhmiä, joten pidä avain
salassa.</p>
<h2>2. Tallenna avain proxy-palvelimelle</h2>
<p>Tiedostona hakemistoon <code>{keys_dir}</code>, jota vain root ja palvelun käyttäjä voivat lukea.
Yksi tiedosto per avain, ja tiedoston nimi on avaimen nimi (kirjaimia, numeroita sekä <code>-</code> ja
<code>_</code>, enintään 32 merkkiä). Kirjoita avain itse:</p>
<pre>sudo -u openspotproxy tee {keys_dir}/mykey &gt;/dev/null
sudo chmod 600 {keys_dir}/mykey</pre>
<p>Liitä avain, paina Enter ja sitten Ctrl-D.</p>
<h2>3. Kirjoita avaimen nimi tähän</h2>
<p>Kirjoita <code>mykey</code> (tiedoston nimi, ei itse avain) <b>API key</b> -kenttään, paina Save ja
sitten Check key. Valitse nimi, jota ei voi arvata: kuka tahansa verkossasi, joka tietää nimen, voi
käyttää avainta tämän proxyn kautta.</p>
<h2>Mikä toimii</h2>
<p>Staattisten puheryhmien lisäys ja poisto, dynaamisten puheryhmien pudotus ja käynnissä olevan
puhelun pudotus. Reflektorin vaihtaminen ei onnistu, koska BrandMeisterin API v2:ssa ei ole sellaista
kutsua.</p>"""),
}
MANAGER_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>BrandMeister Manager</title>
<style>
:root{--blue:#005eb8;--link:#005eb8;--hover:#b5dbff;--bg:#efefef;--card:#fff;--fg:#5c5c5c;--strong:#333;--bar:#e6e6e6;
--line:#e1e1e1;--chip:#cfcfcf;--bad:#c0392b;--on:#2e9e55;--off:#e8a0a6;--ok:#2e7d46}
@media(prefers-color-scheme:dark){:root{--bg:#14171b;--card:#1d2127;--fg:#b8bec6;--strong:#e6e8eb;--bar:#262b32;
--line:#313740;--chip:#3a414b;--bad:#ff8a80;--on:#3dbd6a;--off:#7a4a50;--ok:#6fd58c;--hover:#1c3c5e;--link:#7ab7ff}}
*{box-sizing:border-box}body{font:16px/1.4 'Maven Pro',system-ui,sans-serif;background:var(--bg);color:var(--fg);margin:0}
header{background:var(--blue);color:#fff;min-height:65px;display:flex;align-items:center;overflow-x:auto;white-space:nowrap}
header img{height:36px;margin:0 .6rem 0 .8rem}
header nav{display:flex;margin-left:.8rem}
header nav a{color:#fff;text-decoration:none;font-size:18px;letter-spacing:-1px;line-height:65px;padding:0 1.1rem;
transition:all .2s ease-in-out}
header nav a:hover{background:var(--hover);color:var(--blue)}header nav a.active{background:#fff;color:var(--blue)}
header .end{margin-left:auto;display:flex;align-items:center;padding-right:.9rem;font-size:20px;letter-spacing:-1px}
#hn{margin-right:.7rem}#dots{width:36px;height:36px;position:relative;animation:rot 2s infinite linear;opacity:1;
transition:opacity .5s}#dots.faded{opacity:0}
#dots i{position:absolute;width:60%;height:60%;border-radius:100%;background:#fff;left:20%;top:0;animation:bnc 2s infinite ease-in-out}
#dots i+i{top:auto;bottom:0;animation-delay:-1s}
@keyframes rot{to{transform:rotate(360deg)}}@keyframes bnc{0%,100%{transform:scale(0)}50%{transform:scale(1)}}
h1{margin:0 0 .8rem;font-size:1.5rem;color:var(--strong)}
main{max-width:46rem;margin:1rem auto;padding:0 .8rem}
.card{background:var(--card);border-radius:8px;box-shadow:0 1px 4px #0002;overflow:hidden;margin-bottom:1rem}
.info{background:var(--bar);text-align:center;padding:.6rem 1rem;color:var(--strong)}
.info a,.none a{color:var(--link)}.info b{font-weight:700}
h2{margin:0;padding:.8rem 1.1rem;font-size:1.1rem;color:var(--strong);border-bottom:1px solid var(--line)}
label{font-size:.78rem;display:block;text-transform:uppercase;letter-spacing:.05em}
input,textarea,button{font:inherit;color:var(--strong);background:var(--card);border:1px solid var(--chip);border-radius:5px}
input{padding:.35rem .6rem;width:10rem}textarea{width:100%;padding:.5rem .6rem;font-family:ui-monospace,monospace;resize:vertical}
button{cursor:pointer;padding:.4rem .8rem;font-weight:700;font-size:.8rem;text-transform:uppercase;letter-spacing:.03em}
button.p{background:var(--blue);border-color:var(--blue);color:#fff}button.p:hover{background:var(--hover);color:var(--blue)}
button.s{color:var(--blue);border-color:var(--blue);background:none}button.s:hover{background:var(--hover)}
button.d{background:var(--bad);border-color:var(--bad);color:#fff;padding:.3rem .7rem}
button.o{background:none;color:var(--bad);border-color:var(--bad)}button:disabled{opacity:.5;cursor:default}
.head,.tr{display:grid;grid-template-columns:6rem 4rem 1fr auto;gap:.6rem;align-items:center;padding:.6rem 1.1rem}
.head{background:var(--bar);font-weight:700;font-size:.8rem;text-transform:uppercase;color:var(--fg)}
.tr{border-bottom:1px solid var(--line)}.tr .tg{font-weight:700;color:var(--strong)}
.chip{background:var(--chip);color:var(--strong);border-radius:4px;padding:.05rem .45rem;font-size:.8rem;font-weight:700;
justify-self:start}.acts{display:flex;gap:.7rem;align-items:center;justify-content:flex-end}.none{padding:.8rem 1.1rem}
.sw{position:relative;width:2.9rem;height:1.5rem;display:inline-block}.sw input{opacity:0;width:0;height:0}
.sw span{position:absolute;inset:0;background:var(--off);border-radius:1rem;cursor:pointer;transition:.2s}
.sw span:before{content:"";position:absolute;width:1.1rem;height:1.1rem;left:.2rem;top:.2rem;background:#fff;
border-radius:50%;transition:.2s}.sw input:checked+span{background:var(--on)}
.sw input:checked+span:before{transform:translateX(1.4rem)}.sw input:disabled+span{opacity:.5;cursor:default}
.add{background:var(--bar);padding:1rem 1.1rem;display:grid;grid-template-columns:1fr auto;gap:.8rem}
.add .l{grid-row:span 2}.seg{display:flex;border:1px solid var(--chip);border-radius:6px;overflow:hidden;background:var(--card)}
.seg button{border:0;border-radius:0;background:none;color:var(--fg);flex:1;font-size:.9rem}
.seg button.on{background:var(--blue);color:#fff}.seg button.x{text-decoration:line-through;opacity:.5}
.side{display:flex;flex-direction:column;gap:.5rem;min-width:11rem}.row2{display:flex;gap:.5rem}.row2 button{flex:1}
.hint{padding:.8rem 1.1rem;font-size:.85rem}.bar{display:flex;flex-wrap:wrap;gap:.5rem;padding:.8rem 1.1rem}
#msg{min-height:1.4rem;margin:0 .2rem 1rem;text-align:center}#msg.ok{color:var(--ok)}#msg.err{color:var(--bad)}
.foot{text-align:center;font-size:.85rem;padding-bottom:1.5rem}.foot a{color:var(--link)}
@media(max-width:34rem){.add{grid-template-columns:1fr}.add .l{grid-row:auto}.head,.tr{grid-template-columns:4.6rem 3.4rem 1fr auto}}
</style></head><body>
<header><a href="/"><img src="/hdrlogo.png" alt="openSPOT"></a><nav>
<a href="/main.html_gz#status">Status</a><a href="/main.html_gz#connectors">Connectors</a>
<a href="/main.html_gz#modem">Modem</a><a href="/main.html_gz#settings">Settings</a>
<a href="/bm" class="active">BM Manager</a><a href="/main.html_gz" id="logout">Logout</a></nav>
<div class="end"><span id="hn"></span><div id="dots" class="faded"><i></i><i></i></div></div></header>
<main>
<h1>BrandMeister Manager</h1>
<input id="id" type="hidden"><input id="key" type="hidden">
<div class="card" id="login" hidden><div class="none">Log in to the openSPOT first. The manager takes your hotspot's DMR ID
and API key name from its Connectors page. <a href="/main.html_gz">Log in</a></div></div>
<div id="app" hidden>
<div class="card"><div class="info" id="info">Loading...</div></div>
<div id="msg"></div>
<div class="card"><h2>Static Talkgroups</h2>
<div class="head"><span>Talkgroup</span><span>Slot</span><span>Name</span><span class="acts">Actions</span></div>
<div id="static"></div>
<div class="add"><div class="l"><textarea id="tgs" rows="4" placeholder="Enter Talkgroups (One per line)"></textarea></div>
<div class="side"><div class="seg" id="seg"><button type="button" data-s="1">TS1</button>
<button type="button" data-s="2" class="on">TS2</button></div>
<button id="addbtn" class="p" type="button">Add &amp; Link</button></div>
<div class="row2" style="grid-column:2"><button id="ena" class="s" type="button">Enable all</button>
<button id="dis" class="s" type="button">Disable all</button></div>
<div style="grid-column:2"><button id="del" class="o" style="width:100%" type="button">Delete all</button></div></div>
<div class="hint"><b>Hint:</b> You can add several talkgroups at once. Added talkgroups are linked to BrandMeister.
Disabling a talkgroup unlinks it but keeps it in this list (the list is kept in this browser); enabling links it again.</div></div>
<div class="card"><h2>Dynamic Talkgroups</h2>
<div class="head"><span>Talkgroup</span><span>Slot</span><span>Name</span><span class="acts">Timeout</span></div>
<div id="dynamic"></div><div class="bar" id="acts"></div></div>
</div>
<div class="foot"><a href="/help/api-key" target="_blank" rel="noopener">API key help</a> &middot; <a href="/">openSPOT</a></div>
</main><script>
var $=function(i){return document.getElementById(i)},store=window.localStorage,busy=false,loaded=false,dev={},stat=[],dyn=[],auto=null,slot="2";
function say(t,c){var m=$("msg");m.textContent=t;m.className=c||""}
function creds(){var id=$("id").value.trim(),k=$("key").value.trim();
if(!/^\\d{1,9}$/.test(id)){say("No DMR ID: set it on the openSPOT's Connectors page.","err");return null}
return{id:id,key:k}}
function call(c,method,path,form){var h={},b=null;if(c.key)h.Authorization="Basic "+btoa(c.key+":");
if(form){h["Content-Type"]="application/x-www-form-urlencoded";b=new URLSearchParams(form).toString()}
busyOn();return fetch(path,{method:method,headers:h,body:b}).then(function(r){return r.text().then(function(t){
var j={};try{j=JSON.parse(t)}catch(e){}if(!r.ok)throw new Error(j.message||j.error||j.detail||("HTTP "+r.status));
return j})}).finally(busyOff)}
var pending=0,fadeTimer=null;
function busyOn(){pending++;clearTimeout(fadeTimer);$("dots").classList.remove("faded")}
function busyOff(){if(--pending<=0){pending=0;fadeTimer=setTimeout(function(){$("dots").classList.add("faded")},1000)}}
function jwt(){try{return sessionStorage.getItem("jwt")||store.getItem("jwt")||""}catch(e){return""}}
$("logout").onclick=function(){try{store.removeItem("jwt");sessionStorage.removeItem("jwt")}catch(e){}};
if(jwt())fetch("/checkauth.cgi",{headers:{Authorization:"Bearer "+jwt()}}).then(function(r){return r.json()}).then(function(a){
if(a.success==1&&a.hostname)$("hn").textContent=a.hostname}).catch(function(){});
function offKey(){return"bmOff"+$("id").value.trim()}
function offGet(){try{return JSON.parse(store[offKey()]||"[]")}catch(e){return[]}}
function offSet(a){try{store[offKey()]=JSON.stringify(a)}catch(e){}}
function lab(s){return s==0?"TS2":"TS"+s}
function el(tag,cls,text){var e=document.createElement(tag);if(cls)e.className=cls;if(text!==undefined)e.textContent=text;return e}
function same(a,b){return a.talkgroup==b.talkgroup&&a.slot==b.slot}
function rows(){var on=stat.map(function(t){return{talkgroup:t.talkgroup,slot:t.slot,name:t.name,on:true}});
var off=offGet().filter(function(o){return!on.some(function(r){return same(r,o)})});offSet(off);
return on.concat(off.map(function(o){return{talkgroup:o.talkgroup,slot:o.slot,name:o.name,on:false}}))
.sort(function(a,b){return a.talkgroup-b.talkgroup||a.slot-b.slot})}
function tgRow(t,right){var r=el("div","tr");r.appendChild(el("span","tg","TG "+t.talkgroup));
var c=el("span","chip",t.auto?"AUTO":lab(t.slot));if(t.slot==0)c.title="Simplex hotspot (slot 0)";
if(t.auto)c.title="Linked automatically by BrandMeister (auto-static)";r.appendChild(c);
r.appendChild(el("span","",t.name||""));r.appendChild(right);return r}
function drawStatic(){var box=$("static");box.textContent="";var rs=rows();
if(!rs.length&&!auto)box.appendChild(el("div","none","No static talkgroups."));
if(auto&&!rs.some(function(t){return t.talkgroup==auto.talkgroup}))
box.appendChild(tgRow({talkgroup:auto.talkgroup,name:auto.name,auto:true},el("span","acts","auto-static")));
rs.forEach(function(t){var a=el("span","acts"),sw=el("label","sw"),cb=document.createElement("input");
cb.type="checkbox";cb.checked=t.on;sw.appendChild(cb);sw.appendChild(el("span"));
cb.onchange=function(){cb.checked=t.on;run((t.on?"disable":"enable")+" TG "+t.talkgroup,[toggleOp(t)])};a.appendChild(sw);
var d=el("button","d","Delete");d.type="button";d.onclick=function(){run("delete TG "+t.talkgroup,[deleteOp(t)])};
a.appendChild(d);box.appendChild(tgRow(t,a))})}
function left(ts){var s=Number(ts)-Date.now()/1000;if(!ts||isNaN(s))return ts||"";if(s<=0)return"expired";
var m=Math.floor(s/60);return m>=60?Math.floor(m/60)+" h "+m%60+" min":m>0?m+" min":Math.ceil(s)+" s"}
function drawDynamic(){var box=$("dynamic");box.textContent="";
if(!dyn.length)box.appendChild(el("div","none","No dynamic talkgroups."));
dyn.forEach(function(t){box.appendChild(tgRow(t,el("span","acts",left(t.timeout))))});
var a=$("acts");a.textContent="";(dev.simplex?["0"]:["1","2"]).forEach(function(s){var n=dev.simplex?"":", TS"+s;
[["Drop dynamic TGs"+n,"/bmapi/setRepeaterTarantool.php?action=dropDynamicGroups"],
["Drop current call"+n,"/bmapi/setRepeaterDbus.php?action=dropCallRoute"]].forEach(function(x){
var b=el("button","s",x[0]);b.type="button";b.onclick=function(){run(x[0].toLowerCase(),[{m:"POST",p:x[1],f:{timeslot:s}}])};
a.appendChild(b)})})}
function drawInfo(){var i=$("info");i.textContent="";var id=$("id").value.trim();i.appendChild(document.createTextNode("ID: "));
var a=el("a","",id);a.href="https://brandmeister.network/#/device/"+id;a.target="_blank";a.rel="noopener";i.appendChild(a);
if(dev.callsign)i.appendChild(document.createTextNode(" • "+dev.callsign));
if(dev.master)i.appendChild(document.createTextNode(" • Connected To: BM "+dev.master));
if(dev.simplex)i.appendChild(document.createTextNode(" • Simplex"));
$("seg").firstChild.className=dev.simplex?"x":"";$("seg").firstChild.disabled=!!dev.simplex;
if(dev.simplex)setSlot("2")}
function setSlot(s){slot=s;Array.prototype.forEach.call($("seg").children,function(b){
b.classList.toggle("on",b.dataset.s===s)})}
function draw(){drawInfo();drawStatic();drawDynamic()}
function load(quiet){var c=creds();if(!c)return;if(!quiet)say("Loading...");
return call(c,"GET","/bmapi/manager/profile?q="+c.id).then(function(p){stat=p.static;dyn=p.dynamic;auto=p.auto||null;dev=p.device||{};
loaded=true;draw();if(!quiet)say("Loaded "+c.id+".","ok")}).catch(function(e){say("Could not load: "+e.message,"err")})}
function addOp(t,s){return{m:"POST",p:"/bmapi/talkgroup/?action=ADD",f:{talkgroup:t.talkgroup,timeslot:s},
after:function(){offSet(offGet().filter(function(o){return!same(o,{talkgroup:t.talkgroup,slot:s})}))}}}
function unlinkOp(t,keep){return{m:"POST",p:"/bmapi/talkgroup/?action=DEL",f:{talkgroup:t.talkgroup,timeslot:t.slot},
after:function(){if(keep){var o=offGet();o.push({talkgroup:t.talkgroup,slot:t.slot,name:t.name||""});offSet(o)}}}}
function toggleOp(t){return t.on?unlinkOp(t,true):addOp(t,t.slot)}
function deleteOp(t){if(t.on)return unlinkOp(t,false);
return{local:true,after:function(){offSet(offGet().filter(function(o){return!same(o,t)}))}}}
function run(label,ops){var c=creds();if(!c||busy)return;if(!ops.length){say("Nothing to do.");return}
if(!c.key&&ops.some(function(o){return!o.local})){say("No API key name: save one on the openSPOT's Connectors page (BrandMeister manager).","err");return}
busy=true;say("Working: "+label+"...");var p=Promise.resolve();
ops.forEach(function(o){p=p.then(function(){if(o.local)return;
return call(c,o.m,o.p+(o.p.indexOf("?")<0?"?":"&")+"q="+c.id,o.f)}).then(function(){if(o.after)o.after()})});
p.then(function(){say("Done: "+label+".","ok")}).catch(function(e){say("Failed ("+label+"): "+e.message,"err")})
.then(function(){setTimeout(function(){busy=false;load(true)},1500)})}
Array.prototype.forEach.call($("seg").children,function(b){b.onclick=function(){if(!b.disabled)setSlot(b.dataset.s)}});
$("addbtn").onclick=function(){var v=$("tgs").value.split(/[\\s,;]+/).filter(Boolean),bad=v.filter(function(x){return!/^\\d{1,7}$/.test(x)});
if(!v.length){say("Enter at least one talkgroup.","err");return}
if(bad.length){say("Not a talkgroup number: "+bad[0],"err");return}
var s=dev.simplex?"0":slot,seen={};
run("add "+v.length+" TG"+(v.length>1?"s":""),v.filter(function(x){return seen[x]?0:(seen[x]=1)}).map(function(x){
return addOp({talkgroup:parseInt(x,10)},s)}));$("tgs").value=""};
$("ena").onclick=function(){run("enable all",rows().filter(function(t){return!t.on}).map(toggleOp))};
$("dis").onclick=function(){run("disable all",rows().filter(function(t){return t.on}).map(toggleOp))};
$("del").onclick=function(){var rs=rows();if(rs.length&&confirm("Delete all "+rs.length+" static talkgroups?"))run("delete all",rs.map(deleteOp))};
function osGet(p){return fetch(p,{headers:{Authorization:"Bearer "+jwt()}}).then(function(r){return r.ok?r.json():{}}).catch(function(){return{}})}
function fromOpenspot(){if(!jwt())return Promise.resolve(false);
return Promise.all([osGet("/homebrewsettings.cgi"),osGet("/bmmsettings.cgi")]).then(function(r){
var id=String(r[0].repeater_id||""),k=String(r[1].apikey||"");
var okId=/^\\d{1,9}$/.test(id),okKey=/^[A-Za-z0-9_-]{1,32}$/.test(k);
if(okId)$("id").value=id;if(okKey)$("key").value=k;return okId&&okKey})}
fromOpenspot().then(function(ok){$("login").hidden=ok;$("app").hidden=!ok;if(ok)load(true)});
setInterval(function(){if(!document.hidden&&!busy&&loaded)load(true)},60000);
</script></body></html>"""
BM_V2 = "https://api.brandmeister.network/v2/"
NAMES_SEC = 86400  # TG names change rarely
HOP = {"connection", "keep-alive", "transfer-encoding", "content-length", "proxy-connection", "upgrade"}
CACHE_SEC = 30
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
JSON = "application/json"


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def tg_list(items):
    out, seen = [], set()
    for it in items or []:
        tg = it.get("talkgroup") if isinstance(it, dict) else it
        try:
            tg = int(tg)  # v2 sends "2400", v1 sent 2400
        except (TypeError, ValueError):
            continue
        if tg not in seen:
            seen.add(tg)
            out.append({"talkgroup": tg})
    return out


def auto_static(a, names):
    """The profile's autoStatic entry (one TG BrandMeister linked on its own) as {talkgroup, name}, or None."""
    try:
        tg = int(a["talkgroup"])
    except (TypeError, ValueError, KeyError):
        return None
    return {"talkgroup": tg, "name": names.get(tg, "")}


def v1_profile(v2):
    # v2 has no reflector field; "-" is shown when active is null.
    return {"staticSubscriptions": tg_list(v2.get("staticSubscriptions")),
            "dynamicSubscriptions": tg_list(v2.get("dynamicSubscriptions")),
            "autoStatic": auto_static(v2.get("autoStatic"), {}),  # not in v1; shown by the JS rewrites below
            "reflector": {"active": None}}


def jwt_expired(token):
    """True if the token is not a JWT or its exp claim is in the past. No signature check."""
    try:
        part = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        return "exp" in claims and claims["exp"] < time.time()
    except (IndexError, ValueError):
        return True


class Proxy:
    def __init__(self, a):
        self.device = a.device
        self.keys_dir = a.keys_dir
        self.cache = {}  # dmr id -> (expiry, v1 profile); "d"+id -> manager profile
        self.names = (0, {})  # (expiry, {tg number: name})
        self.lock = threading.Lock()
        self.dev_slots = threading.Semaphore(3)  # the device's HTTP server is small

    def token(self, name):
        if not name or not NAME_RE.match(name):
            return None
        try:
            with open(os.path.join(self.keys_dir, name)) as f:
                return f.read().strip() or None
        except OSError:
            return None

    def bm(self, method, path, token=None, body=None):
        """One BrandMeister v2 call. Returns (status, bytes). Never logs the token."""
        headers = {"User-Agent": "dmrlink-openspot-proxy", "Accept": JSON}
        if token:
            headers["Authorization"] = "Bearer " + token
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = JSON
        try:
            r = urllib.request.urlopen(urllib.request.Request(BM_V2 + path, data, headers, method=method), timeout=10)
            return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def profile(self, dmr_id):
        now = time.time()
        with self.lock:
            hit = self.cache.get(dmr_id)
            if hit and hit[0] > now:
                return hit[1]
        status, raw = self.bm("GET", "device/" + dmr_id + "/profile")
        if status == 404:
            log(f"bm: device {dmr_id} not known to BrandMeister, returning empty profile")
            return v1_profile({})  # not cached: the device may show up at BrandMeister any moment
        elif status == 200:
            v1 = v1_profile(json.loads(raw))
            log(f"bm: profile {dmr_id} static={[t['talkgroup'] for t in v1['staticSubscriptions']]} "
                f"dynamic={[t['talkgroup'] for t in v1['dynamicSubscriptions']]}")
        else:
            raise RuntimeError(f"BrandMeister returned {status}")
        with self.lock:
            self.cache[dmr_id] = (now + CACHE_SEC, v1)
        return v1

    def tg_names(self):
        """BrandMeister's talkgroup directory as {number: name}; stale or empty if the lookup fails."""
        with self.lock:
            exp, names = self.names
        if exp > time.time():
            return names
        try:
            status, raw = self.bm("GET", "talkgroup")
            data = json.loads(raw) if status == 200 else {}
            if isinstance(data, dict) and data:
                names = {int(k): str(v) for k, v in data.items() if str(k).isdigit()}
            exp = time.time() + (NAMES_SEC if names else 60)
        except (OSError, ValueError, http.client.HTTPException) as e:
            log(f"bm: talkgroup names lookup failed: {e}")
            exp = time.time() + 60  # retry soon, keep what we had
        with self.lock:
            self.names = (exp, names)
        return names

    def device_info(self, dmr_id):
        """Callsign, master and simplex flag for the manager header; empty if the lookup fails."""
        try:
            status, raw = self.bm("GET", "device/" + dmr_id)
            d = json.loads(raw) if status == 200 else {}
        except (OSError, ValueError, http.client.HTTPException):
            return {}
        return {"callsign": d.get("callsign") or "", "master": d.get("lastKnownMaster") or "",
                "simplex": d.get("statusText") == "DMO"}

    def detail(self, dmr_id):
        """The profile with slots, timeouts and TG names, for the manager page (cached like profile())."""
        now = time.time()
        with self.lock:
            hit = self.cache.get("d" + dmr_id)
            if hit and hit[0] > now:
                return hit[1]
        status, raw = self.bm("GET", "device/" + dmr_id + "/profile")
        if status == 404:
            return {"static": [], "dynamic": []}
        if status != 200:
            raise RuntimeError(f"BrandMeister returned {status}")
        v2 = json.loads(raw)
        names = self.tg_names()

        def rows(items):
            out, seen = [], set()
            for it in items or []:
                if not isinstance(it, dict):
                    continue
                try:
                    tg, slot = int(it.get("talkgroup")), int(it.get("slot") or 0)
                except (TypeError, ValueError):
                    continue
                if (tg, slot) not in seen:
                    seen.add((tg, slot))
                    out.append({"talkgroup": tg, "slot": slot, "name": names.get(tg, ""),
                                "timeout": it.get("timeout") or ""})
            return sorted(out, key=lambda r: (r["slot"], r["talkgroup"]))
        res = {"static": rows(v2.get("staticSubscriptions")), "dynamic": rows(v2.get("dynamicSubscriptions")),
               "device": self.device_info(dmr_id), "auto": auto_static(v2.get("autoStatic"), names)}
        with self.lock:
            self.cache["d" + dmr_id] = (now + CACHE_SEC, res)
        return res

    def forget(self, dmr_id):
        with self.lock:
            self.cache.pop(dmr_id, None)
            self.cache.pop("d" + dmr_id, None)


def make_handler(px):
    class H(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def reply(self, code, body=b"", headers=(), ctype=None):
            self.send_response(code)
            if ctype:
                self.send_header("Content-Type", ctype)
            for k, v in headers:
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def jreply(self, code, obj):
            self.reply(code, json.dumps(obj).encode(), headers=[("Cache-Control", "no-store")], ctype=JSON)

        def key_name(self):
            """The 'API key' the page sends is a key name: Basic base64('<name>:')."""
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Basic "):
                return None
            try:
                return base64.b64decode(auth[6:]).decode().split(":")[0]
            except (ValueError, UnicodeDecodeError):
                return None

        def write(self, label, dmr_id, method, path, body=None):
            """Run one authenticated v2 write and answer in the v1 style the page expects."""
            token = px.token(self.key_name())
            if not token:
                log(f"bm: {label} {dmr_id} refused: unknown key name")
                return self.jreply(401, {"code": "ERROR", "message": "unknown key name"})
            status, raw = px.bm(method, "device/" + path, token, body)
            px.forget(dmr_id)
            log(f"bm: {label} on {dmr_id} -> {status}")
            if 200 <= status < 300:
                return self.jreply(200, {"code": "OK", "message": f"OK ({label})"})  # the page shows message
            log(f"bm: error body: {raw[:300].decode('utf8', 'replace')!r}")
            self.reply(status, raw or b"{}", ctype=JSON)

        def do_bmapi(self):
            u = urlsplit(self.path)
            sub = u.path[len("/bmapi/"):]
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            form = {}
            if self.command == "POST":
                n = int(self.headers.get("Content-Length") or 0)
                form = {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode()).items()}
            action = q.get("action", "")

            if sub == "validate" and self.command == "GET":
                tok = px.token(q.get("token"))
                if not tok or jwt_expired(tok):
                    return self.jreply(401, {"error": "no usable key under that name"})
                return self.jreply(200, {"ok": True})

            if sub == "manager/name" and self.command == "GET":
                tg = q.get("tg", "")
                return self.jreply(200, {"name": px.tg_names().get(int(tg), "") if tg.isdigit() else ""})

            dmr_id = q.get("q") or q.get("id") or ""
            if not dmr_id.isdigit():
                return self.jreply(400, {"error": "missing or bad DMR ID"})
            if sub == "manager/profile" and self.command == "GET":
                try:
                    return self.jreply(200, px.detail(dmr_id))
                except Exception as e:
                    log(f"bm: v2 lookup failed: {e}")
                    return self.jreply(502, {"error": "BrandMeister lookup failed"})
            slot = form.get("timeslot") or q.get("slot") or "0"
            tg = form.get("talkgroup", "")

            if sub in ("", "index.php") and self.command == "GET" and action.upper() == "PROFILE":
                try:
                    return self.jreply(200, px.profile(dmr_id))
                except Exception as e:  # network or JSON failure: the page retries on error
                    log(f"bm: v2 lookup failed: {e}")
                    return self.jreply(502, {"error": "upstream"})
            if self.command == "POST" and sub == "talkgroup/" and slot.isdigit() and tg.isdigit():
                if action == "ADD":
                    return self.write(f"add TG {tg} slot {slot}", dmr_id, "POST", f"{dmr_id}/talkgroup",
                                      # the live API wants "group"; its published spec says "talkgroup"
                                      {"group": int(tg), "talkgroup": int(tg), "slot": int(slot)})
                if action == "DEL":
                    return self.write(f"remove TG {tg} slot {slot}", dmr_id, "DELETE",
                                      f"{dmr_id}/talkgroup/{slot}/{tg}")
            if self.command == "POST" and slot.isdigit():
                if sub == "setRepeaterTarantool.php" and action == "dropDynamicGroups":
                    return self.write(f"drop dynamic TGs slot {slot}", dmr_id, "GET",
                                      f"{dmr_id}/action/dropDynamicGroups/{slot}")
                if sub == "setRepeaterDbus.php" and action == "dropCallRoute":
                    return self.write(f"drop call slot {slot}", dmr_id, "GET",
                                      f"{dmr_id}/action/dropCallRoute/{slot}")
            if sub == "reflector/setActiveReflector.php":
                return self.jreply(501, {"code": "ERROR", "message": "BrandMeister API v2 has no reflector call"})
            self.jreply(400, {"error": "unsupported"})

        def help_lang(self):
            """?lang=fi|en wins, else the first language in Accept-Language that we have, else English."""
            q = parse_qs(urlsplit(self.path).query).get("lang", [""])[0]
            if q in HELP_TEXT:
                return q
            for part in self.headers.get("Accept-Language", "").split(","):
                tag = part.split(";")[0].strip().lower()[:2]
                if tag in HELP_TEXT:
                    return tag
            return "en"

        def do_proxy(self):
            path = urlsplit(self.path).path
            if path == "/":
                # The device's loader page redirects the browser straight to the device; skip it.
                return self.reply(302, headers=[("Location", "/main.html_gz")])
            if path.startswith("/bmapi/"):
                return self.do_bmapi()
            if path in ("/bm", "/bm/"):
                return self.reply(200, MANAGER_HTML.encode(), headers=[("Cache-Control", "no-cache")],
                                  ctype="text/html; charset=utf-8")
            if path == "/help/api-key":
                lang = self.help_lang()
                title, switch, body = HELP_TEXT[lang]
                page = HELP_FRAME  # plain replaces: the CSS in the frame contains braces
                for key, val in (("{lang}", lang), ("{title}", title), ("{switch}", switch),
                                 ("{other}", "fi" if lang == "en" else "en"), ("{body}", body)):
                    page = page.replace(key, val)
                page = page.replace("{keys_dir}", px.keys_dir)
                return self.reply(200, page.encode(), headers=[("Vary", "Accept-Language")],
                                  ctype="text/html; charset=utf-8")
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else None
            rewrites = next((rw for name, rw in REWRITES.items() if path.endswith(name)), None)
            skip = HOP | {"if-none-match", "if-modified-since"} if rewrites else HOP
            # A browser that cached the file before it was rewritten would get a 304 from the device and
            # keep the old copy, so rewritten files are always fetched in full.
            fwd = {k: v for k, v in self.headers.items() if k.lower() not in skip}
            me = self.headers.get("Host", "")
            fwd["Host"] = px.device
            for h in ("Origin", "Referer"):
                if h in fwd:
                    fwd[h] = fwd[h].replace(me, px.device)
            with px.dev_slots:
                try:
                    c = http.client.HTTPConnection(px.device, 80, timeout=15)
                    c.request(self.command, self.path, body, fwd)
                    r = c.getresponse()
                    data = r.read()
                    heads = r.getheaders()
                    code = r.status
                    c.close()
                except (OSError, http.client.HTTPException) as e:
                    log(f"device: {self.command} {self.path} failed: {e}")
                    return self.reply(502, b"openSPOT unreachable")
            if rewrites and code == 200:
                enc = r.getheader("Content-Encoding", "")
                text = gzip.decompress(data) if enc == "gzip" else data
                for old, new in rewrites:
                    if old in text:
                        text = text.replace(old, new)
                    else:
                        log(f"WARNING: {old[:48]!r} not found in {path} (firmware changed?)")
                data = text
                heads = [(k, v) for k, v in heads if k.lower() not in ("content-encoding", "etag")]
                heads.append(("Cache-Control", "no-cache"))  # revalidate; no ETag, so always a full reply
            out = [(k, v) for k, v in heads if k.lower() not in HOP]
            self.reply(code, data, out)

        do_GET = do_POST = do_HEAD = do_PUT = do_DELETE = do_proxy

    return H


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--device", required=True, help="openSPOT address, e.g. 192.168.1.50")
    p.add_argument("--bind", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8081)
    p.add_argument("--keys-dir", default="/etc/openspot-bm-proxy/keys",
                   help="directory of BrandMeister v2 tokens, one file per key name")
    a = p.parse_args()
    srv = ThreadingHTTPServer((a.bind, a.port), make_handler(Proxy(a)))
    srv.daemon_threads = True
    log(f"openSPOT proxy http://{a.bind}:{a.port}/ -> http://{a.device}/ (keys in {a.keys_dir})")
    srv.serve_forever()


if __name__ == "__main__":
    sys.exit(main())
