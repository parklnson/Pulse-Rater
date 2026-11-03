"""
PULSE RATER — оценка треков.

Как работает: программа поднимает маленький локальный сервер и открывает интерфейс
в отдельном окне Microsoft Edge / Google Chrome (режим приложения, без адресной строки).
Приложение стартует сразу во весь экран; переключать полный экран / оконный режим — F11 или кнопка в углу.
Оценки сохраняются в %APPDATA%\\TrackRater\\tracks.json, загруженные mp3/wav/flac — в папке audio, картинки — в папке img рядом.
Никаких дополнительных библиотек не нужно — только стандартный Python.
"""
import json
import sys
import math
import os
import re
import secrets
import socket
import shutil
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DATA_DIR = Path(os.environ.get("APPDATA") or Path.home()) / "TrackRater"
DATA_FILE = DATA_DIR / "tracks.json"
GAME_FILE = DATA_DIR / "game.json"
PROFILE_FILE = DATA_DIR / "profile.json"
UPDATE_FILE = DATA_DIR / "update.json"

# ---------- версия и автообновление ----------
# Чтобы выпустить обновление: подними APP_VERSION, впиши APP_NOTES и выложи файл по ссылке UPDATE_URL.
APP_VERSION = "2.5"
APP_NOTES = "HiLo: туз — старшая карта, множитель только растёт; значок плеера исчезает после закрытия"
UPDATE_URL = ""          # ссылка на свежий track_rater.py, например https://github.com/ник/pulse-rater/blob/main/track_rater.py
UPD = {"src": None, "ver": None}
APP_FILE = Path(__file__).resolve()          # запоминаем при запуске — дальше рабочая папка может смениться


def ver_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v))[:4]) or (0,)


def norm_upd(u):
    u = (u or "").strip()
    m = re.match(r"^https://github\.com/([^/]+)/([^/]+)/(?:blob|raw)/(.+)$", u)
    if m:
        u = f"https://raw.githubusercontent.com/{m.group(1)}/{m.group(2)}/{m.group(3)}"
    return u if re.match(r"^https://[^\s]+$", u) else ""


def upd_url():
    try:
        u = json.loads(UPDATE_FILE.read_text("utf-8")).get("url") or ""
    except Exception:
        u = ""
    return norm_upd(u or UPDATE_URL)


def upd_check():
    u = upd_url()
    res = {"cur": APP_VERSION, "url": u, "available": False, "latest": None, "notes": ""}
    if not u:
        return res
    sep = "&" if "?" in u else "?"
    raw, _ = http_get(u + sep + "t=" + str(int(time.time())), timeout=15, maxb=8 * 1024 * 1024)
    src = raw.decode("utf-8")
    m = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', src, re.M)
    if not m or "PULSE RATER" not in src or "class Handler" not in src:
        raise ValueError("по ссылке не программа PULSE RATER")
    n = re.search(r'^APP_NOTES\s*=\s*"([^"]*)"', src, re.M)
    res.update(latest=m.group(1), notes=n.group(1) if n else "")
    if ver_tuple(m.group(1)) > ver_tuple(APP_VERSION):
        res["available"] = True
        UPD.update(src=src, ver=m.group(1))
    return res


def upd_apply():
    src = UPD["src"]
    if not src:
        upd_check()
        src = UPD["src"]
    if not src:
        raise ValueError("обновлений нет")
    compile(src, "track_rater.py", "exec")            # битый файл не ставим
    me = upd_target()
    me.parent.mkdir(parents=True, exist_ok=True)
    if me.exists():
        try:
            shutil.copy2(me, me.with_name(me.stem + ".backup.py"))   # прошлая версия остаётся рядом
        except Exception as e:
            launch_log(f"обновление: копия прошлой версии не сохранена: {e}")
    tmp = me.with_name(me.name + ".new")
    tmp.write_bytes(src.encode("utf-8"))
    os.replace(tmp, me)
    launch_log(f"обновлено до {UPD.get('ver')}: {me}")
    STATE["restart_file"] = str(me)
    STATE["restart"] = time.time() + 1.2
    return str(me)


def upd_target():
    """Файл программы, который нужно заменить. Если его уже нет на месте (запускали из временной
    папки, переместили или удалили), новая версия ставится в папку данных программы."""
    cands = [APP_FILE]
    try:
        cands.append(Path(sys.argv[0]).resolve())
    except Exception:
        pass
    for c in cands:
        if c.suffix.lower() in (".py", ".pyw") and c.is_file():
            return c
    return DATA_DIR / "PULSE RATER.py"
AUDIO_DIR = DATA_DIR / "audio"
AUDIO_TYPES = {"mp3": "audio/mpeg", "wav": "audio/wav", "flac": "audio/flac"}
MAX_AUDIO = 500 * 1024 * 1024
IMG_DIR = DATA_DIR / "img"
ARTISTS_FILE = DATA_DIR / "artists.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
MAX_IMG = 12 * 1024 * 1024
OPEN_HOSTS = ("yandex.ru", "yandex.com", "yandex.by", "yandex.kz", "vk.com", "vk.ru", "youtube.com", "youtu.be",
              "deezer.com", "apple.com", "soundcloud.com", "spotify.com", "bandcamp.com")
STATE = {"last": time.time(), "bye": None, "quit": None, "restart": None}

HTML = r'''<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>PULSE RATER</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#000000">
<script>window.__ST=/*ST*/{}/*END*/;</script>
<script>
var THEMES=[
 {id:"euphoria",n:"Эйфория",t:"Классика: малиновый закат",h:352,cy:true},
 {id:"sunset",n:"Закат на Сансет",t:"Янтарь, персик и капелька драмы",h:22,cy:false},
 {id:"acid",n:"Кислотный дождь",t:"Ядовито-зелёный после грозы",h:100,cy:false},
 {id:"mint",n:"Мятная жвачка",t:"Холодная свежесть бирюзы",h:168,cy:false},
 {id:"blue",n:"Синий понедельник",t:"Меланхолия в хорошем качестве",h:214,cy:false},
 {id:"cosmo",n:"Космический борщ",t:"Глубокий фиолет с нотками свёклы",h:268,cy:false},
 {id:"candy",n:"Сладкая вата",t:"Розовый сахарный взрыв",h:322,cy:false}
];
var APPV="2.5";
var DESIGNS=[
 {id:"classic",n:"Эйфория",t:"Как сейчас: живой космос, сияние и звёзды"},
 {id:"ocean",n:"Океан",t:"Глубина: лучи света сквозь воду, пузырьки и бирюза"},
 {id:"sakura",n:"Сакура",t:"Вечерний сад: падающие лепестки и розовая луна"},
 {id:"glass",n:"Стекло",t:"Матовое стекло, мягкие формы и цветной туман"},
 {id:"ambient",n:"Амбиент",t:"Фон — размытая обложка трека, белые кнопки, как в плеере"},
 {id:"fireflies",n:"Светлячки",t:"Ночной лес: светлячки, туман и силуэты сосен"},
 {id:"lux",n:"Люкс",t:"Чёрный бархат и золото ар-деко, золотая пыль и блики света"},
 {id:"nebula",n:"Туманность",t:"Глубокий космос: живая туманность, звёзды и метеоры"}
];
var ST_DEF={design:"classic",theme:"euphoria",hue:352,cycle:true,panelA:54,neb:true,aurora:true,twinkle:true,warp:true,eco:false,
 glow:true,glowSize:100,clickFx:true,trail:false,start:5,pvVol:100,catSrc:"sc",intro:true,outro:true,sounds:true,intKey:"ShiftLeft",vol:100,decimals:false};
var ST=(function(){var o={},k,u=window.__ST||{};for(k in ST_DEF)o[k]=ST_DEF[k];
 for(k in u) if(k in ST_DEF&&typeof u[k]===typeof ST_DEF[k]) o[k]=u[k];return o})();
function stHue(){if(ST.theme==="custom") return +ST.hue||0;for(var i=0;i<THEMES.length;i++) if(THEMES[i].id===ST.theme) return THEMES[i].h;return 352}
function stShift(){return stHue()-352}
function stStart(){return ST.decimals?+ST.start:Math.round(+ST.start)}   // стартовое значение: без десятичных — целое
(function(){var l=0;try{l=+localStorage.getItem("perfLvl")||0}catch(e){}if(l>=1){window.LOWFX=true;document.documentElement.classList.add("lowfx")}if(l>=2){window.LOWFX2=true;document.documentElement.classList.add("lowfx2")}})();
function layerOn(n){
 if(ST.eco||ST[n]===false) return false;
 if(window.LOWFX2) return false;                                      // слабая графика: живой фон выключен совсем
 if(window.LOWFX&&(n==="aurora"||n==="twinkle")) return false;        // облегчённо: без сияния и мерцания
 if(ST.design!=="classic") return false;      // в этих дизайнах свой статичный фон — анимации не нужны
 return true}
function hslRgb(h,s,l){h=((h%360)+360)%360/360;s/=100;l/=100;var q=l<.5?l*(1+s):l+s-l*s,p=2*l-q;
 function f(t){t=(t+1)%1;return Math.round(255*(t<1/6?p+(q-p)*6*t:t<.5?q:t<2/3?p+(q-p)*(2/3-t)*6:p))}
 return f(h+1/3)+","+f(h)+","+f(h-1/3)}
function paintTheme(){
 var H=stHue(),r=document.documentElement,st=r.style,c=r.classList;
 function hh(x){return Math.round(((H+x)%360+360)%360)}
 st.setProperty("--g","hsl("+hh(3)+" 80% 51%)");
 st.setProperty("--g2","hsl("+hh(1)+" 100% 60%)");
 st.setProperty("--g3","hsl("+hh(0)+" 100% 67%)");
 st.setProperty("--g4","hsl("+hh(1)+" 100% 74%)");
 st.setProperty("--g0","hsl("+hh(2)+" 78% 46%)");
 st.setProperty("--ar",hslRgb(H+1,100,60));
 st.setProperty("--tn",hh(-8));
 st.setProperty("--pa",(Math.min(95,Math.max(30,+ST.panelA||54))/100).toFixed(2));
 st.setProperty("--gsz",(+ST.glowSize||100)+"px");
 r.setAttribute("data-design",DESIGNS.some(function(d){return d.id===ST.design})?ST.design:"classic");
 c.toggle("no-glow",!(ST.glow&&!ST.eco));c.toggle("no-aurora",!layerOn("aurora"));
 c.toggle("no-twinkle",!layerOn("twinkle"));c.toggle("no-warp",!layerOn("warp"));c.toggle("no-neb",!layerOn("neb"));
}
paintTheme();
var _stT=0;
function setST(p){for(var k in p) ST[k]=p[k];paintTheme();
 try{window.dispatchEvent(new Event("st-change"))}catch(e){}
 clearTimeout(_stT);_stT=setTimeout(function(){try{fetch("/api/settings",{method:"POST",body:JSON.stringify(ST)})}catch(e){}},350)}

/* частота кадров не ограничивается нигде: фон, анимации и канвасы идут с частотой монитора */
window.fpsGap=function(low,norm){return window.LOWFX?low:(norm||0)};   // фон не рисуется чаще, чем нужно глазу
</script>
<link rel="icon" href="data:,">
<style>
@property --h{syntax:"<number>";inherits:true;initial-value:352}
@property --gc{syntax:"<color>";inherits:true;initial-value:#ff0040}
@property --bo{syntax:"<number>";inherits:true;initial-value:0}
@property --ph{syntax:"<number>";inherits:true;initial-value:352}
:root{--tn:344;--pa:.54;--gsz:100px;--ar:255,51,71;--g3:#ff5566;--g4:#ff7a88;--g0:#d11a2c;--cur:url("data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 width=%2724%27 height=%2724%27 viewBox=%270 0 24 24%27%3E%3Cpath d=%27M4 2.5v16l4.2-3.7 2.8 6.4 2.6-1.1-2.7-6.2h5.6z%27 fill=%27%23fff%27 stroke=%27%23000%27 stroke-width=%271.2%27 stroke-linejoin=%27round%27/%3E%3C/svg%3E") 4 3,default;--cur-p:url("data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 width=%2726%27 height=%2726%27 viewBox=%270 0 26 26%27%3E%3Cpath d=%27M9.5 2.8c-.9 0-1.6.7-1.6 1.6v9.2l-1.7-2c-.6-.7-1.7-.8-2.4-.2-.7.6-.7 1.6-.2 2.3l4.2 6c1 1.5 2.7 2.4 4.5 2.4h3.4c2.6 0 4.7-2.1 4.7-4.7v-5.2c0-.9-.7-1.6-1.6-1.6s-1.6.7-1.6 1.6v-1.1c0-.9-.7-1.6-1.6-1.6s-1.6.7-1.6 1.6v-.9c0-.9-.7-1.6-1.6-1.6-.4 0-.8.1-1.1.4V4.4c0-.9-.7-1.6-1.6-1.6z%27 fill=%27%23fff%27 stroke=%27%23000%27 stroke-width=%271.2%27 stroke-linejoin=%27round%27/%3E%3C/svg%3E") 9 3,pointer;--bg:hsl(var(--tn) 33% 6% / var(--pa));--sub:hsl(var(--tn) 9% 73%);--line:rgba(255,255,255,.1);--g:#e5202e;--g2:#ff3347;--ease:cubic-bezier(.2,.8,.2,1)}
*{box-sizing:border-box}
html,body{cursor:var(--cur)}
[hidden]{display:none!important}
html,body{height:100%}
body{margin:0;background:#000;color:#fff;overflow:hidden;font-family:"Segoe UI Variable Text","Segoe UI",system-ui,-apple-system,Roboto,Arial,sans-serif;-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
button,input{font:inherit;color:inherit}
input:not([type=range]):not([type=checkbox]):not([type=radio]):not([type=button]){cursor:text}
button{cursor:var(--cur-p);border:0;background:none;padding:0}
svg{display:block}
.ico{width:24px;height:24px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
::selection{background:var(--g);color:#fff}
::-webkit-scrollbar{width:12px;height:12px}
::-webkit-scrollbar-thumb{background:rgba(255,255,255,.25);border-radius:6px;border:3px solid transparent;background-clip:padding-box}
::-webkit-scrollbar-thumb:hover{background:rgba(255,255,255,.45);background-clip:padding-box;border:3px solid transparent}
::-webkit-scrollbar-track{background:transparent}

#aurora{position:fixed;inset:-40px;width:calc(100% + 80px);height:calc(100% + 80px);z-index:0;pointer-events:none;transform:translateZ(0)}
#stars,#twinkle,#fxTrail{transform:translateZ(0)}
.list,.pbody,.main{contain:paint}
#fxGlow{position:fixed;left:0;top:0;width:0;height:0;z-index:9500;pointer-events:none;opacity:0;transition:opacity .35s ease;will-change:transform}
#fxGlow.on{opacity:1}
#fxGlow i{position:absolute;left:calc(var(--gsz) / -2);top:calc(var(--gsz) / -2);width:var(--gsz);height:var(--gsz);border-radius:50%;opacity:.5;background:radial-gradient(circle,hsl(calc(350 + var(--bo)) 100% 62% / .22) 0%,hsl(calc(350 + var(--bo)) 100% 55% / .08) 34%,transparent 68%);transition:transform .35s var(--ease),opacity .4s ease}
#fxGlow.mv i{opacity:.85;transform:scale(.78)}
#fxGlow.hot i{transform:scale(1.15)}
#fxGlow.hot.mv i{transform:scale(.92)}
html.no-glow #fxGlow,html.no-aurora #aurora,html.no-twinkle #twinkle,html.no-warp #stars,html.no-neb .nebs{display:none!important}
#fxTrail{position:fixed;inset:0;width:100%;height:100%;z-index:9400;pointer-events:none}
#twinkle{position:fixed;inset:0;width:100%;height:100%;z-index:0;pointer-events:none}
#stars{position:fixed;inset:0;width:100%;height:100%;z-index:0;pointer-events:none}
.nebs{position:fixed;inset:0;z-index:0;overflow:hidden;pointer-events:none}
.base{position:absolute;inset:0;background:radial-gradient(120% 90% at 50% 0%,#2a0810 0%,#0d0305 55%,#000 100%)}
.neb{position:absolute;border-radius:50%;pointer-events:none;will-change:transform}
.n1{width:60vmax;height:60vmax;left:-20vmax;top:-26vmax;background:radial-gradient(circle,hsl(calc(352 + var(--bo)) 100% 56% / .42),transparent 65%);animation:drift1 28s ease-in-out infinite alternate}
.n2{width:64vmax;height:64vmax;right:-24vmax;bottom:-28vmax;background:radial-gradient(circle,hsl(calc(346 + var(--bo)) 100% 37% / .45),transparent 65%);animation:drift2 34s ease-in-out infinite alternate}
.n3{width:40vmax;height:40vmax;left:35vw;top:30vh;background:radial-gradient(circle,hsl(calc(370 + var(--bo)) 100% 64% / .2),transparent 65%);animation:drift3 40s ease-in-out infinite alternate}
.app{position:relative;z-index:1;display:grid;grid-template-columns:clamp(290px,26vw,400px) minmax(0,1fr);gap:8px;padding:8px;height:100vh}
.side{grid-column:1;grid-row:1;display:flex;flex-direction:column;gap:8px;min-height:0}
.panel{background:var(--bg);border-radius:10px}
.side .panel{background:hsl(var(--tn) 33% 4% / .94);contain:paint}
.nav{padding:14px 12px 10px}
.brand{display:flex;align-items:center;padding:0 12px 12px;font-weight:900;letter-spacing:-.03em;font-size:clamp(17px,1.5vw,23px);line-height:1.1}
.brand span{--tile:400px;white-space:nowrap;padding:0 .1em .14em 0;background:linear-gradient(100deg,#6c6c76 0%,#a3a3ad 20%,#ffffff 38%,#c8c8d1 52%,#8d8d97 68%,#f2f2f6 84%,#6c6c76 100%);background-size:400px 100%;-webkit-background-clip:text;background-clip:text;color:transparent;animation:shim 5s linear infinite}
.navbtn{display:flex;align-items:center;gap:16px;width:100%;padding:10px 12px;border-radius:6px;color:var(--sub);font-weight:700;font-size:15px;transition:color .2s}
.navbtn:hover,.navbtn.on{color:#fff}
button.brand{width:100%;text-align:left;cursor:var(--cur-p);outline:0;border-radius:8px}
.brand span{display:inline-block;transition:filter .3s,transform .35s var(--ease)}
.brand:hover span,.brand:focus-visible span{filter:drop-shadow(0 0 16px rgba(var(--ar),.6));transform:translateX(3px)}
.brand:active span{transform:scale(.97)}
.brand:focus-visible{box-shadow:0 0 0 2px rgba(255,255,255,.35)}

.lib{flex:1;min-height:0;display:flex;flex-direction:column}
.lib-h{display:flex;align-items:center;justify-content:space-between;padding:14px 16px 8px;color:var(--sub);font-weight:700}
.lib-h .t{display:flex;align-items:center;gap:12px}
.lib-h .t:hover{color:#fff}
.cnt{font-size:12px;background:#2a2a2a;color:#fff;border-radius:999px;padding:2px 8px}
.cats{display:flex;gap:8px;padding:2px 16px 10px;flex-wrap:wrap}
.cat{display:inline-flex;align-items:center;gap:6px;padding:6px 12px 6px 10px;border-radius:999px;background:rgba(255,255,255,.06);box-shadow:inset 0 0 0 1px rgba(255,255,255,.1);font-size:13px;font-weight:700;color:var(--sub);transition:background .25s,color .2s,box-shadow .25s,transform .2s var(--ease)}
.cat .ico{width:14px;height:14px;stroke-width:2.2}
.cat:hover{color:#fff;background:rgba(255,255,255,.1)}
.cat:active{transform:scale(.96)}
.cat.on{background:linear-gradient(135deg,var(--g2),var(--g0));color:#fff;box-shadow:0 6px 18px -6px rgba(var(--ar),.7)}
.csw{display:grid;grid-template-rows:0fr;visibility:hidden;transition:grid-template-rows .38s var(--ease),visibility 0s .38s}
.csw.open{grid-template-rows:1fr;visibility:visible;transition:grid-template-rows .38s var(--ease),visibility 0s}
.csi{overflow:hidden;min-height:0}
.cs{display:flex;gap:6px;padding:0 16px 10px;overflow-x:auto;scrollbar-width:none;-webkit-mask-image:linear-gradient(90deg,transparent 0,#000 14px,#000 calc(100% - 22px),transparent 100%);mask-image:linear-gradient(90deg,transparent 0,#000 14px,#000 calc(100% - 22px),transparent 100%)}
.cs::-webkit-scrollbar{display:none}
.cpill{flex:none;display:inline-flex;align-items:center;gap:6px;padding:5px 11px;border-radius:999px;background:rgba(255,255,255,.07);font-size:12px;font-weight:600;max-width:190px;transition:background .25s,color .2s,box-shadow .25s}
.cpill>span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cpill em{font-style:normal;font-size:11px;opacity:.55;font-variant-numeric:tabular-nums}
.cpill:hover{background:rgba(255,255,255,.13)}
.cpill.on{background:rgba(var(--ar),.2);box-shadow:inset 0 0 0 1px rgba(255,90,108,.75);color:#fff}
.cpill.on em{opacity:.9}
.cn{font-size:12px;color:var(--sub);padding:5px 2px}
.chips{display:flex;gap:8px;padding:4px 16px 10px;flex-wrap:wrap}
.chip{padding:6px 12px;border-radius:999px;background:#2a2a2a;font-size:13px;font-weight:600;transition:background .2s}
.chip:hover{background:#363636}
.chip.on{background:#fff;color:#000}
.plates{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;padding:2px 16px 10px}
.plates .plate{flex-direction:column;align-items:flex-start;gap:8px;padding:10px 11px;min-width:0}
.plates .plate>span:last-child{min-width:0;max-width:100%}
.plates .plate em{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.plate{display:flex;align-items:center;gap:10px;padding:10px 12px;border-radius:12px;text-align:left;background:linear-gradient(180deg,rgba(255,255,255,.09),rgba(255,255,255,.04));box-shadow:inset 0 0 0 1px rgba(255,255,255,.1);transition:background .25s,transform .25s var(--ease),box-shadow .3s}
.plate:hover,.plate.on{background:linear-gradient(135deg,var(--g2),var(--g0));box-shadow:0 8px 22px -8px rgba(var(--ar),.7)}
.plate:hover{transform:translateY(-2px)}
.plate:active{transform:scale(.97)}
.plate .pi{flex:none;width:34px;height:34px;border-radius:9px;display:grid;place-items:center;background:rgba(0,0,0,.35)}
.plate .pi .ico{width:18px;height:18px}
.plate b{display:block;font-size:14px;font-weight:800;line-height:1.15}
.plate em{display:block;margin-top:2px;font-style:normal;font-size:12px;color:var(--sub)}
.plate:hover em,.plate.on em{color:rgba(255,255,255,.88)}
.lbcv.acv{cursor:var(--cur-p)}
.lbcv.acv:hover .covh,.lbcv.acv:hover .covx{opacity:1}
.lbcv .covx{position:absolute;z-index:3;top:8px;right:8px;width:30px;height:30px;border-radius:50%;background:rgba(0,0,0,.62);display:grid;place-items:center;opacity:0;transition:opacity .2s}
.lbcv .covx svg{width:16px;height:16px}
.lbcv.big>svg{position:relative;z-index:1}
.lbcv{position:relative;width:100%;aspect-ratio:1;border-radius:8px;display:grid;place-items:center;overflow:hidden;color:rgba(255,255,255,.9);box-shadow:0 8px 30px rgba(0,0,0,.45)}
.lbcv>svg{width:34%;height:34%}
.ahead .lbcv.big{width:clamp(140px,16vw,232px);flex:none;box-shadow:0 8px 60px rgba(0,0,0,.55)}
.lbe{padding:60px 24px;text-align:center;color:var(--sub)}
.lbe b{display:block;color:#fff;font-size:16px;margin-bottom:6px}
.keyb{display:flex;gap:6px;flex:none}
.keybtn{padding:8px 14px;border-radius:8px;background:rgba(255,255,255,.1);font-size:13px;font-weight:700;min-width:130px;box-shadow:inset 0 0 0 1px rgba(255,255,255,.14);transition:background .2s,box-shadow .2s}
.keybtn:hover{background:rgba(255,255,255,.18)}
.keybtn.rec{background:rgba(var(--ar),.25);box-shadow:inset 0 0 0 1px rgba(255,90,108,.85)}
.keyx{width:34px;border-radius:8px;background:rgba(255,255,255,.08);font-size:18px;line-height:1;color:var(--sub);transition:background .2s,color .2s}
.keyx:hover{color:#fff;background:rgba(255,255,255,.16)}
.rate{position:relative;isolation:isolate;flex:0 1 360px;min-width:200px;height:56px;border-radius:999px;color:#fff;font-weight:800;font-size:16px;letter-spacing:.16em;text-transform:uppercase;display:grid;place-items:center;box-shadow:0 8px 24px rgba(var(--ar),.35);transition:transform .25s var(--ease),box-shadow .25s}
.rbg{position:absolute;inset:0;z-index:-1;border-radius:inherit;overflow:hidden;background:linear-gradient(110deg,var(--g0),var(--g2) 45%,var(--g3) 75%,var(--g2));background-size:200% 100%;transition:background-position .7s var(--ease)}
.rbg::after{content:"";position:absolute;top:0;bottom:0;left:-60%;width:40%;transform:skewX(-20deg);background:linear-gradient(90deg,transparent,rgba(255,255,255,.45),transparent)}
.rate:hover{transform:translateY(-2px);box-shadow:0 12px 34px rgba(var(--ar),.55)}
.rate:hover .rbg{background-position:100% 0}
.rate:hover .rbg::after{animation:rshine .9s ease}
.rate:active{transform:scale(.97)}
.rate.pop{animation:rpop .6s var(--ease)}
.rate.pop::before{content:"";position:absolute;inset:0;border-radius:inherit;border:2px solid rgba(var(--ar),.95);pointer-events:none;animation:rring .75s ease-out forwards}
.rl{position:relative}
.rpl{position:absolute;width:10px;height:10px;margin:-5px 0 0 -5px;border-radius:50%;background:rgba(255,255,255,.6);pointer-events:none;animation:rrip .7s ease-out forwards}
.rsp{position:absolute;width:var(--s);height:var(--s);margin:calc(var(--s) / -2) 0 0 calc(var(--s) / -2);border-radius:50%;background:#fff;box-shadow:0 0 10px var(--g3);pointer-events:none;animation:rspk .75s ease-out forwards}
@keyframes rshine{to{left:130%}}
@keyframes rpop{0%{transform:scale(1)}25%{transform:scale(.93)}60%{transform:scale(1.045)}100%{transform:scale(1)}}
@keyframes rring{from{opacity:.95;transform:scale(1)}to{opacity:0;transform:scale(1.18,1.7)}}
@keyframes rrip{from{opacity:.8;transform:scale(1)}to{opacity:0;transform:scale(16)}}
@keyframes rspk{from{opacity:1;transform:translate(0,0) scale(1)}to{opacity:0;transform:translate(var(--dx),var(--dy)) scale(.2)}}
.rcrit{display:inline-flex;align-items:center;gap:8px;height:44px;padding:0 16px;border-radius:999px;background:rgba(255,255,255,.07);box-shadow:inset 0 0 0 1px rgba(255,255,255,.12);font-size:13px;font-weight:700;color:var(--sub);white-space:nowrap;transition:color .2s,background .2s,transform .2s var(--ease)}
.rcrit:hover{color:#fff;background:rgba(255,255,255,.13)}
.rcrit:active{transform:scale(.96)}
.rcrit svg{width:16px;height:16px;transition:transform .45s var(--ease)}
.rcrit:hover svg{transform:rotate(-90deg)}
.ap.det .apn span i{font-style:normal;color:var(--g3)}
.apsw{flex:none;padding:8px 14px;border-radius:999px;background:rgba(255,255,255,.1);font-size:13px;font-weight:700;transition:background .2s}
.apsw:hover{background:rgba(255,255,255,.2)}
.tno{font-style:normal;margin-right:10px;color:var(--sub);font-weight:700;font-variant-numeric:tabular-nums}
.mv{display:flex;flex-direction:column;opacity:0;transition:opacity .15s}
.item:hover .mv,.item:focus-within .mv{opacity:1}
.mv button{width:28px;height:15px;display:grid;place-items:center;border-radius:4px;color:var(--sub)}
.mv button:hover{color:#fff;background:rgba(255,255,255,.14)}
.mv .ico{width:14px;height:14px}
.item.dg{opacity:.35}
.item.ins-b{box-shadow:0 -2px 0 var(--g2)}
.item.ins-a{box-shadow:0 2px 0 var(--g2)}
.ren{width:26px;height:26px;border-radius:50%;display:grid;place-items:center;color:var(--sub);transition:color .2s,background .2s}
.ren:hover{color:#fff;background:rgba(255,255,255,.14)}
.ren .ico{width:14px;height:14px}
.ren-in{width:min(100%,640px);margin:2px 0 8px;padding:4px 14px;border:0;outline:0;border-radius:12px;background:rgba(0,0,0,.45);box-shadow:0 0 0 2px rgba(255,255,255,.35);font-weight:900;letter-spacing:-.03em;font-size:clamp(26px,3.6vw,56px);line-height:1.15;color:#fff}
.alh .hint{text-transform:none;letter-spacing:0;font-weight:600;opacity:.7}
body.intm .row .val b{color:var(--g4)}
.srow{display:flex;align-items:center;gap:8px;padding:0 16px 8px}
.sbox{position:relative;flex:1}
.sbox svg{position:absolute;left:10px;top:50%;width:16px;height:16px;transform:translateY(-50%);color:var(--sub)}
.sbox input{width:100%;background:#242424;border:0;outline:0;border-radius:6px;padding:8px 10px 8px 34px;font-size:13px;transition:box-shadow .2s}
.sbox input:focus{box-shadow:0 0 0 2px rgba(255,255,255,.35)}
.sort{font-size:13px;font-weight:600;color:var(--sub);white-space:nowrap;transition:color .2s}
.sort:hover{color:#fff}
.list{flex:1;min-height:0;overflow:auto;padding:2px 8px 8px}
.item{display:grid;grid-template-columns:48px minmax(0,1fr) auto 28px;align-items:center;gap:12px;padding:8px;border-radius:8px;cursor:var(--cur-p);transition:background .15s}
.item:hover{background:rgba(255,255,255,.07)}
.item.on{background:rgba(255,255,255,.12)}
.item.new{animation:slideIn .5s var(--ease)}
.cv{position:relative;width:48px;height:48px;border-radius:6px;display:grid;place-items:center;color:rgba(255,255,255,.92);overflow:hidden}
.cv>svg{width:22px;height:22px}
.tx{min-width:0}
.tx b,.tx span{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tx b{font-size:15px;font-weight:600}
.item.on .tx b{color:var(--g2)}
.tx span{font-size:13px;color:var(--sub)}
.sc{font-family:"Bahnschrift","Segoe UI Variable Display","Segoe UI",system-ui,sans-serif;font-weight:700;font-size:19px;letter-spacing:.02em;font-variant-numeric:tabular-nums lining-nums;text-shadow:0 0 14px currentColor}
.del{width:28px;height:28px;border-radius:50%;display:grid;place-items:center;color:var(--sub);opacity:0;transition:opacity .15s,color .15s}
.del svg{width:16px;height:16px}
.item:hover .del{opacity:1}
.del:hover{color:#fff}
.eqw{position:absolute;inset:0;background:rgba(0,0,0,.5);display:grid;place-items:center}
.eqw.ps .eq i{animation-play-state:paused}
.item.pl .tx b,.trk.pl .pn b{color:var(--g3)}
.lbcv>.eqw{z-index:1}
.lbcv>.eqw .eq{height:22px;gap:3px}
.lbcv>.eqw .eq i{width:5px}
.lbcv .pbadge{z-index:2}
.eq{display:flex;align-items:flex-end;gap:2px;height:14px}
.eq i{width:3px;height:100%;background:var(--g2);border-radius:1px;transform-origin:bottom;animation:eq 1s ease-in-out infinite}
.eq i:nth-child(2){animation-delay:-.4s}.eq i:nth-child(3){animation-delay:-.8s}.eq i:nth-child(4){animation-delay:-.2s}
.empty{padding:40px 24px;text-align:center;color:var(--sub)}
.empty .bigic{width:64px;height:64px;margin:0 auto 16px;border-radius:50%;background:#242424;display:grid;place-items:center;color:#fff}
.empty b{display:block;color:#fff;font-size:16px;margin-bottom:6px}
.empty p{margin:0;font-size:14px;line-height:1.45}

.main{grid-column:2;grid-row:1;position:relative;border-radius:10px;background:var(--bg);overflow:auto;transition:--h .8s ease,opacity .4s ease,transform .55s var(--ease)}
body.pg .main{opacity:0;transform:scale(.985);pointer-events:none}
.hero-bg{position:absolute;left:0;right:0;top:0;height:480px;pointer-events:none;background:linear-gradient(180deg,hsl(calc(var(--h) + var(--bo)) 62% 30% / .85) 0%,hsl(calc(var(--h) + var(--bo)) 55% 15% / .55) 68%,transparent 100%)}
.tools{position:sticky;top:0;z-index:6;height:0;display:flex;justify-content:flex-end}
.round{margin:16px;width:36px;height:36px;border-radius:50%;background:rgba(0,0,0,.45);display:grid;place-items:center;transition:transform .2s var(--ease),background .2s}
.round:hover{transform:scale(1.07);background:rgba(0,0,0,.7)}
.round svg{width:18px;height:18px}
.hero{position:relative;display:flex;align-items:flex-end;gap:28px;padding:84px 32px 8px}
.cover{position:relative;flex:none;width:clamp(150px,17vw,232px);aspect-ratio:1;border-radius:10px;overflow:hidden;display:grid;place-items:center;color:rgba(255,255,255,.92);box-shadow:0 8px 60px rgba(0,0,0,.55);transition:transform .3s var(--ease)}
.cover::after{content:"";position:absolute;inset:0;background:linear-gradient(135deg,rgba(255,255,255,.2),transparent 48%)}
.cover svg{position:relative;z-index:1;width:38%;height:38%;stroke-width:1.5;filter:drop-shadow(0 4px 14px rgba(0,0,0,.35))}
.cover:hover{transform:scale(1.025)}
.meta{min-width:0;flex:1;padding-bottom:6px}
.eyebrow{display:flex;align-items:center;gap:10px;font-size:12px;font-weight:700;letter-spacing:.12em;text-transform:uppercase}
.titlew{border-bottom:2px solid transparent;transition:border-color .25s;margin:2px 0 8px}
.titlew:focus-within{border-color:rgba(255,255,255,.25)}
.big{display:block;width:100%;background:transparent;border:0;outline:0;font-weight:900;letter-spacing:-.035em;line-height:1.24;font-size:clamp(34px,5.4vw,88px);padding:.04em .04em .16em 0;caret-color:var(--g2);text-overflow:ellipsis}
.big::placeholder{color:rgba(255,255,255,.3)}
.sub{display:flex;align-items:center;flex-wrap:wrap;gap:8px 10px;font-size:14px;color:rgba(255,255,255,.88)}
.art{background:transparent;border:0;outline:0;font-weight:700;font-size:16px;width:min(300px,45vw);padding:2px 0;border-bottom:2px solid transparent;transition:border-color .2s;caret-color:var(--g2)}
.art:focus{border-bottom-color:rgba(255,255,255,.35)}
.art::placeholder{color:rgba(255,255,255,.42)}
.dot{opacity:.55}
.prodw{display:inline-flex;align-items:baseline;gap:6px}
.prod{font-size:12px;font-weight:700;letter-spacing:.04em;color:#7f7f89}
.art.beat{width:min(190px,28vw);color:#9d9da7;font-weight:600;font-size:15px}
.phs.pb{background:linear-gradient(100deg,#4e4e57 0%,#6f6f79 25%,#a2a2ac 40%,#6a6a74 60%,#4e4e57 100%);background-size:var(--tile) 100%;-webkit-background-clip:text;background-clip:text;font-weight:600;font-size:15px}
.tx span .pb,.pb{font-style:normal;color:#7d7d87;font-weight:600}
.sub b{font-weight:800}

.content{position:relative;padding:0 32px 56px}
.actions{display:flex;align-items:center;gap:18px;padding:24px 0 22px}
.play{width:56px;height:56px;border-radius:50%;background:var(--g2);color:#fff;display:grid;place-items:center;box-shadow:0 8px 24px rgba(var(--ar),.3);transition:transform .2s var(--ease),background .2s,box-shadow .2s}
.play:hover{transform:scale(1.07);background:var(--g3);box-shadow:0 10px 32px rgba(var(--ar),.55)}
.play:active{transform:scale(.97)}
.play svg{width:26px;height:26px;stroke-width:2.6}
.ibtn{width:40px;height:40px;border-radius:50%;display:grid;place-items:center;color:var(--sub);transition:color .2s,transform .2s var(--ease),background .2s}
.ibtn:hover{color:#fff;transform:scale(1.08);background:rgba(255,255,255,.08)}
.ibtn svg{width:22px;height:22px}
.grid{display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:32px;align-items:start}

.thead,.row{display:grid;grid-template-columns:32px minmax(130px,240px) minmax(140px,1fr) 70px;gap:16px;align-items:center}
.thead{padding:0 16px 10px;color:var(--sub);font-size:12px;letter-spacing:.1em;text-transform:uppercase;border-bottom:1px solid var(--line);margin-bottom:8px}
.thead :last-child{text-align:right}
.row{padding:12px 16px;border-radius:8px;transition:background .25s}
.row:hover{background:rgba(255,255,255,.06)}
.row.drag{background:linear-gradient(90deg,color-mix(in srgb,var(--gc) 16%,transparent),transparent 75%)}
.idx{color:var(--sub);font-size:15px;text-align:center}
.nm b{display:block;font-size:16px;font-weight:600}
.nm span{display:block;font-size:13px;color:var(--sub);margin-top:2px}
.nm .wt{display:inline-block;margin-left:8px;padding:1px 7px;border-radius:999px;background:rgba(255,255,255,.08);font-style:normal;font-size:11px;font-weight:700;color:var(--sub);vertical-align:2px;font-variant-numeric:tabular-nums}
.nm .wt.hi{background:rgba(var(--ar),.22);color:#fff;box-shadow:inset 0 0 0 1px rgba(var(--ar),.5)}
.val{text-align:right;font-variant-numeric:tabular-nums}
.val b{font-size:24px;font-weight:800;display:inline-block}
.val i{font-style:normal;color:var(--sub);font-size:13px;margin-left:2px}

.row,.sl{-webkit-user-select:none;user-select:none;-webkit-user-drag:none}
.sl>*{pointer-events:none}
.sl{position:relative;height:34px;display:flex;align-items:center;cursor:var(--cur-p);touch-action:none;outline:0;--p:50%}
.rail{position:relative;width:100%;height:4px;border-radius:2px;background:#4d4d4d}
.fill{position:absolute;left:0;top:0;bottom:0;width:var(--p);border-radius:2px;background:#fff;overflow:hidden;transition:width .3s var(--ease),background .2s}
.row{--gc:hsl(calc(var(--h) + var(--bo)) 88% 62%)}
.sl:hover .fill,.sl:focus-visible .fill{background:var(--gc)}
.thumb{position:absolute;left:var(--p);top:50%;width:14px;height:14px;margin:-7px 0 0 -7px;border-radius:50%;background:#fff;box-shadow:0 2px 8px rgba(0,0,0,.5);transform:scale(0);transition:left .3s var(--ease),transform .18s var(--ease),box-shadow .25s}
.sl:hover .thumb,.sl:focus-visible .thumb{transform:scale(1)}
.ticks{position:absolute;left:0;right:0;top:calc(50% + 11px);display:flex;justify-content:space-between;opacity:0;transition:opacity .25s;pointer-events:none}
.ticks i{width:3px;height:3px;border-radius:50%;background:rgba(255,255,255,.4)}
.sl.drag .fill{transition:none;background:linear-gradient(90deg,color-mix(in srgb,var(--gc) 55%,#000),var(--gc) 70%,color-mix(in srgb,var(--gc) 65%,#fff));box-shadow:0 0 16px var(--gc)}
.sl.drag .fill::after{content:"";position:absolute;inset:0;background:linear-gradient(90deg,transparent,rgba(255,255,255,.4),transparent);animation:sheen 1.1s ease-in-out infinite}
.sl.drag .thumb{transition:transform .18s var(--ease),box-shadow .25s;transform:scale(1.3);box-shadow:0 0 0 6px color-mix(in srgb,var(--gc) 30%,transparent),0 0 22px var(--gc)}
.sl.drag .ticks{opacity:1}

.score{position:sticky;top:16px;border-radius:14px;padding:28px 24px 22px;text-align:center;background:radial-gradient(130% 90% at 50% 0%,hsl(calc(var(--h) + var(--bo)) 55% 24% / .9) 0%,rgba(24,12,15,.82) 72%);box-shadow:inset 0 0 0 1px rgba(255,255,255,.05)}
.cap{font-size:12px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:var(--sub);margin-bottom:18px}
.ringw{position:relative;width:200px;height:200px;margin:0 auto 20px}
.ringw::before{content:"";position:absolute;inset:-16px;border-radius:50%;background:radial-gradient(circle,hsl(calc(var(--h) + var(--bo)) 70% 50% / .3) 0%,transparent 66%)}
.ringw svg{position:relative;width:100%;height:100%}
#ring{transition:stroke-dashoffset .7s var(--ease),opacity .3s}
.ringc{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center}
.num{transition:color .4s;font-size:60px;font-weight:900;letter-spacing:-.05em;line-height:1;font-variant-numeric:tabular-nums}
.of{color:var(--sub);font-size:13px;margin-top:4px}
.verd{font-size:28px;font-weight:900;letter-spacing:-.03em}
.blurb{color:var(--sub);font-size:14px;margin:4px 0 20px;min-height:20px}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;padding-top:16px;border-top:1px solid var(--line)}
.st b{display:block;font-size:20px;font-weight:800;font-variant-numeric:tabular-nums}
.st span{font-size:11px;color:var(--sub);text-transform:uppercase;letter-spacing:.08em}
.score.hot .ringw::before{animation:pulse 2.2s ease-in-out infinite}

#credit{position:fixed;left:50%;bottom:5px;transform:translateX(-50%);z-index:8;font-size:11px;letter-spacing:.04em;color:#fff;opacity:.28;pointer-events:none;user-select:none;white-space:nowrap}
#toast{position:fixed;left:50%;bottom:28px;z-index:20;transform:translate(-50%,24px);opacity:0;pointer-events:none;background:var(--g2);color:#fff;font-weight:700;font-size:14px;padding:12px 22px;border-radius:8px;box-shadow:0 10px 30px rgba(0,0,0,.55);transition:transform .3s var(--ease),opacity .25s}
#toast.show{transform:translate(-50%,0);opacity:1}
#toast.err{background:#fff;color:#000}

.shake{animation:shake .4s}
@keyframes rgbflow{to{background-position:260px 0}}
@keyframes rgbglow{0%,100%{--gc:#ff0040}17%{--gc:#ff9500}33%{--gc:#ffee00}50%{--gc:#00ff6a}67%{--gc:#00cfff}83%{--gc:#d400ff}}
@keyframes drift1{to{transform:translate3d(20vmax,14vmax,0) scale(1.18)}}
@keyframes drift2{to{transform:translate3d(-22vmax,-10vmax,0) scale(1.12)}}
@keyframes drift3{to{transform:translate3d(-12vmax,16vmax,0) scale(1.25)}}
@keyframes sheen{0%{transform:translateX(-100%)}100%{transform:translateX(100%)}}
@keyframes eq{0%,100%{transform:scaleY(.25)}50%{transform:scaleY(1)}}
@keyframes pulse{50%{transform:scale(1.1);opacity:.7}}
@keyframes slideIn{from{opacity:0;transform:translateX(-18px)}to{opacity:1;transform:none}}
@keyframes shake{20%{transform:translateX(-8px)}40%{transform:translateX(7px)}60%{transform:translateX(-5px)}80%{transform:translateX(3px)}}

.gen{width:min(190px,34vw)}
.ph{position:relative;display:block}
.ph.inl{display:inline-block}
.ph input::placeholder{color:transparent!important}
.phs{--tile:360px;position:absolute;left:0;top:0;max-width:100%;overflow:hidden;white-space:nowrap;pointer-events:none;color:transparent;
  background:linear-gradient(100deg,#6c6c76 0%,#a3a3ad 20%,#ffffff 38%,#c8c8d1 52%,#8d8d97 68%,#f2f2f6 84%,#6c6c76 100%);
  background-size:var(--tile) 100%;-webkit-background-clip:text;background-clip:text;animation:shim 5s linear infinite;transition:opacity .2s}
.phs.pt{--tile:900px;font-weight:900;letter-spacing:-.035em;line-height:1.24;font-size:clamp(34px,5.4vw,88px);padding:.04em .04em .16em 0;text-overflow:ellipsis}
.phs.pa{font-weight:700;font-size:16px;line-height:normal;padding:2px 0}
.ph input:not(:placeholder-shown)+.phs{opacity:0}
.gen::-webkit-calendar-picker-indicator{opacity:.35;cursor:var(--cur-p);transition:opacity .2s}
.gen:hover::-webkit-calendar-picker-indicator{opacity:.9}
@keyframes shim{to{background-position:calc(var(--tile)*-1) 0}}
.gh{position:sticky;top:0;z-index:2;display:flex;align-items:center;justify-content:space-between;gap:8px;padding:10px 8px 6px;margin-top:4px;background:rgba(24,11,15,.94);border-radius:6px;font-size:13px;cursor:var(--cur-p);user-select:none;outline:0;transition:background .2s}
.gh:hover{background:rgba(44,18,25,.96)}
.gh:focus-visible{box-shadow:0 0 0 2px rgba(255,255,255,.35)}
.gh b{font-weight:800}
.gh span{color:var(--sub);font-size:12px}
.gh .gl{display:flex;align-items:center;gap:6px;min-width:0}
.gh .gl b{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.gh .gar{width:16px;height:16px;flex:none;color:var(--sub);stroke-width:2.4;transition:transform .35s var(--ease),color .2s}
.gh:hover .gar{color:#fff}
.grp.shut .gh .gar{transform:rotate(-90deg)}
.gb{display:grid;grid-template-rows:1fr;opacity:1;transition:grid-template-rows .38s var(--ease),opacity .3s ease}
.gbi{min-height:0;overflow:hidden}
.grp.shut .gb{grid-template-rows:0fr;opacity:0}

.gm{position:fixed;inset:0;z-index:15;display:grid;place-items:center;background:rgba(6,1,3,.66);backdrop-filter:blur(6px);animation:gfade .25s ease}
.gcard{width:min(94vw,470px);max-height:calc(100vh - 16px);max-height:calc(100dvh - 16px);overflow:hidden;padding:16px 18px 12px;border-radius:18px;background:rgba(26,12,17,.95);box-shadow:0 30px 80px rgba(0,0,0,.6),inset 0 0 0 1px rgba(255,255,255,.07);animation:gpop .35s var(--ease)}
.ghead{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:10px}
.ghead b{display:block;font-size:20px;font-weight:900;letter-spacing:-.02em}
.ghead span{font-size:13px;color:var(--sub)}
.gx{width:34px;height:34px;border-radius:50%;display:grid;place-items:center;color:var(--sub);transition:color .2s,background .2s,transform .2s var(--ease)}
.gx:hover{color:#fff;background:rgba(255,255,255,.1);transform:scale(1.08)}
.gx svg{width:18px;height:18px}
.gstats{position:relative;z-index:2;flex:none;display:flex;align-items:center;gap:20px;margin-bottom:10px;font-size:13px;color:var(--sub)}
.gstats b{color:#fff;font-size:18px;font-variant-numeric:tabular-nums;margin-left:6px}
.lives{margin-left:auto;display:flex;gap:4px;font-size:18px;color:var(--g2)}
.lives i{font-style:normal;transition:opacity .25s,transform .25s}
.lives i.off{opacity:.2;transform:scale(.75)}
.gwrap{--avail:calc(100vh - 290px);--gh:clamp(220px,min(var(--avail),560px),560px);position:relative;flex:none;height:var(--gh);width:calc(var(--gh)*.7636);max-width:100%;margin:0 auto;border-radius:12px;overflow:hidden;box-shadow:inset 0 0 0 1px rgba(255,255,255,.08)}
.gwrap canvas{display:block;width:100%;height:100%;touch-action:none}
.gwrap.play{cursor:none}
.gover{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:10px;padding:24px;text-align:center;background:rgba(10,3,6,.6)}
.gover b{font-size:26px;font-weight:900;letter-spacing:-.03em}
.gover p{margin:0 0 6px;color:var(--sub);font-size:14px;line-height:1.45;max-width:300px}
.gbtn{padding:12px 32px;border-radius:999px;background:var(--g2);color:#fff;font-weight:800;transition:transform .2s var(--ease),background .2s}
.gbtn:hover{transform:scale(1.06);background:var(--g3)}
.gbtn:active{transform:scale(.97)}
.gtabs{display:flex;gap:4px;margin-bottom:12px;padding:4px;border-radius:999px;background:rgba(255,255,255,.06)}
.gtab{flex:1;padding:8px 10px;border-radius:999px;font-weight:700;font-size:13px;color:var(--sub);transition:background .2s,color .2s}
.gtab:hover{color:#fff}
.gtab.on{background:var(--g2);color:#fff}
.gwrap.sq{--sw:clamp(220px,min(var(--avail),420px,100%),420px);width:var(--sw);height:var(--sw)}
@supports(height:100dvh){.gwrap{--avail:calc(100dvh - 290px)}}
@media(max-aspect-ratio:3/4){.gwrap{max-width:100%;height:auto;aspect-ratio:420/550;width:min(100%,calc(var(--gh)*.7636))}.gwrap.sq{aspect-ratio:1;height:auto;width:min(100%,var(--sw))}}
.rcard{width:min(96vw,780px);overflow:auto}
.rmbody{display:grid;gap:12px}
.rml{display:grid;gap:5px;font-size:12px;color:var(--sub)}
.rml input,.rml select,.rmbody input[type=text]{font:inherit;font-size:14px;color:#fff;width:100%;padding:10px 12px;border-radius:10px;border:1px solid var(--line);background:rgba(0,0,0,.35);outline:0}
.rml select{color-scheme:dark}
.rml input:focus,.rml select:focus{border-color:var(--g3)}
.rmrow{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.rmck{display:flex;gap:10px;align-items:flex-start;font-size:13px;color:var(--sub);cursor:pointer}
.rmck input{margin-top:3px;accent-color:var(--g2)}
.rmck em{font-style:normal;opacity:.7}
.rmck.off{opacity:.45;pointer-events:none}
.rmh{font-weight:800;font-size:14px}
.rmtrack{display:grid;gap:10px;padding:14px;border-radius:12px;background:rgba(255,255,255,.04)}
.rmact{display:flex;gap:10px;flex-wrap:wrap}
.gbtn.ghost{background:rgba(255,255,255,.1);color:var(--sub)}
.gbtn.ghost:hover{background:rgba(255,255,255,.18);color:#fff}
.rminv{display:grid;gap:10px;padding:14px;border-radius:12px;background:rgba(255,255,255,.04)}
.rmcode{display:flex;align-items:baseline;gap:12px;font-size:13px;color:var(--sub)}
.rmcode b{font-size:30px;letter-spacing:.22em;color:#fff;font-weight:900}
.rmlink{display:grid;grid-template-columns:1fr auto;gap:10px;align-items:end}
.rmlink .gbtn{padding:10px 20px}
.rmhelp{font-size:13px;color:var(--sub)}
.rmhelp summary{cursor:pointer;font-weight:700}
.rmhelp p{margin:8px 0 0;line-height:1.5}
.rmhelp code{padding:1px 6px;border-radius:6px;background:rgba(0,0,0,.4);color:#fff;user-select:all}
.rmwarn{opacity:.8}
.rmres{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;font-size:13px;color:var(--sub)}
.rmres .gbtn{padding:9px 18px}
.rmnote{margin:0;font-size:12px;color:var(--sub)}
@media(max-width:560px){.rmrow,.rmlink{grid-template-columns:1fr}}
.ghint{position:relative;z-index:2;margin-top:10px;text-align:center;font-size:12px;color:var(--sub)}
@keyframes gfade{from{opacity:0}}
@keyframes gpop{from{opacity:0;transform:translateY(14px) scale(.97)}}
@media (max-width:1100px){.grid{grid-template-columns:1fr}.score{position:static}}
@media (max-width:860px){
  .app{grid-template-columns:76px minmax(0,1fr)}
  .brand span,.navbtn span,.lib-h .t span,.cnt,.cats,.plates,.csw,.chips,.srow,.tx,.sc,.del,.gh{display:none}
  .grp.shut .gb{grid-template-rows:1fr;opacity:1}
  .item{grid-template-columns:48px;justify-content:center;padding:6px}
  .navbtn{justify-content:center;padding:10px 0}
  .brand{justify-content:center;padding:2px 0 14px}
  .lib-h{justify-content:center;padding:14px 0 8px}
  .nav{padding:14px 6px 10px}
}
@media (max-width:700px){
  .hero{flex-direction:column;align-items:flex-start;gap:18px;padding:70px 20px 8px}
  .content{padding:0 16px 40px}
  .thead{display:none}
  .row{grid-template-columns:24px 1fr 60px;row-gap:6px}
  .row .sl{grid-column:1/-1;grid-row:2}
}
.au{margin:0 0 20px}
.drop{display:flex;align-items:center;gap:14px;padding:12px 14px;border-radius:14px;background:rgba(255,255,255,.05);border:1.5px dashed rgba(255,255,255,.2);transition:border-color .2s,background .2s}
.drop span{font-size:13px;color:var(--sub);line-height:1.35}
.drop.over,body.dragging .drop{border-color:var(--g2);background:rgba(var(--ar),.14)}
.upbtn{flex:none;display:flex;align-items:center;gap:8px;padding:10px 20px;border-radius:999px;background:var(--g2);color:#fff;font-weight:800;font-size:14px;transition:transform .2s var(--ease),background .2s}
.upbtn:hover{transform:scale(1.05);background:var(--g3)}
.upbtn:active{transform:scale(.97)}
.upbtn svg{width:16px;height:16px}
.ap{display:flex;align-items:center;gap:14px;padding:12px 16px 12px 12px;border-radius:14px;background:rgba(0,0,0,.38);box-shadow:inset 0 0 0 1px rgba(255,255,255,.07);animation:gfade .3s ease}
.apb{flex:none;width:46px;height:46px;border-radius:50%;background:var(--g2);color:#fff;display:grid;place-items:center;box-shadow:0 6px 20px rgba(var(--ar),.3);transition:transform .2s var(--ease),background .2s}
.apb:hover{transform:scale(1.07);background:var(--g3)}
.apb:active{transform:scale(.96)}
.apb svg{width:20px;height:20px;fill:currentColor;stroke:none}
.ap{flex-wrap:wrap;row-gap:0}
.apsk{flex:none;width:34px;height:34px;border-radius:50%;display:grid;place-items:center;color:var(--sub);background:rgba(255,255,255,.07);transition:color .2s,background .2s,transform .2s var(--ease),opacity .2s}
.apsk svg{width:15px;height:15px;fill:currentColor;stroke:currentColor;stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
.apsk:hover:not(:disabled){color:#fff;background:rgba(255,255,255,.16);transform:scale(1.1)}
.apsk:disabled{opacity:.3;cursor:default}
.apsk[hidden]{display:none}
.ap:has(#apPrev:not([hidden])){gap:10px}
.tnav{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:10px}
.tnb{display:inline-flex;align-items:center;gap:8px;padding:8px 14px;border-radius:999px;font-size:13px;font-weight:700;color:var(--sub);background:rgba(255,255,255,.06);transition:color .2s,background .2s,transform .2s var(--ease),opacity .2s}
.tnb svg{width:16px;height:16px}
.tnb:hover:not(:disabled){color:#fff;background:rgba(255,255,255,.14);transform:translateY(-1px)}
.tnb.nx{background:var(--g2);color:#fff}
.tnb.nx:hover:not(:disabled){background:var(--g3)}
.tnb:disabled{opacity:.3;cursor:default}
.tnav[hidden]{display:none}
.apvw{flex:0 0 100%;height:0;margin-top:0;opacity:0;overflow:hidden;border-radius:10px;transition:height .45s var(--ease),margin-top .45s var(--ease),opacity .35s ease}
.ap.live .apvw{height:64px;margin-top:12px;opacity:1}
.ap.det .apvw{height:0!important;margin-top:0!important;opacity:0!important}

.apvw canvas{display:block;width:100%;height:64px}
.apm{flex:1;min-width:0}
.apn{display:flex;align-items:baseline;gap:10px;margin-bottom:6px}
.apn b{min-width:0;font-size:14px;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.apn span{flex:none;font-size:12px;color:var(--sub)}
.apt{display:flex;align-items:center;gap:10px;font-size:12px;color:var(--sub);font-variant-numeric:tabular-nums}
.apt i{flex:none;min-width:34px;font-style:normal}
.apt i:last-child{text-align:right}
.rng{-webkit-appearance:none;appearance:none;flex:1;min-width:0;height:14px;margin:0;background:transparent;cursor:var(--cur-p);--p:0%}
.rng::-webkit-slider-runnable-track{height:4px;border-radius:2px;background:linear-gradient(90deg,#fff var(--p),rgba(255,255,255,.2) var(--p))}
.rng::-webkit-slider-thumb{-webkit-appearance:none;width:12px;height:12px;margin-top:-4px;border-radius:50%;background:#fff;box-shadow:0 0 0 0 rgba(255,255,255,.3);transition:box-shadow .2s}
.rng:hover::-webkit-slider-thumb,.rng:focus-visible::-webkit-slider-thumb{box-shadow:0 0 0 6px rgba(255,255,255,.18)}
.rng:focus{outline:0}
.vol{flex:none;display:flex;align-items:center;gap:8px;width:110px;color:var(--sub)}
.vol svg{width:18px;height:18px;flex:none}
.apx{flex:none}
@media (max-width:700px){.vol{display:none}.drop{flex-wrap:wrap}}
/* ---------- страница «Профили артистов» ---------- */
.pm{grid-column:2;grid-row:1;position:relative;z-index:5;min-width:0;min-height:0;display:flex;flex-direction:column;overflow:hidden;border-radius:10px;background:var(--bg);--ph:352;transition:--ph .8s ease;animation:pgIn .55s var(--ease) backwards}
.pm.out{animation:pgOut .28s cubic-bezier(.4,0,1,1) both;pointer-events:none}
.pm-bg{position:absolute;left:0;right:0;top:0;height:460px;pointer-events:none;background:linear-gradient(180deg,hsl(calc(var(--ph) + var(--bo)) 62% 30% / .85) 0%,hsl(calc(var(--ph) + var(--bo)) 55% 15% / .55) 68%,transparent 100%)}
.ptop{position:relative;z-index:3;flex:none;display:flex;align-items:center;gap:10px;padding:16px 24px 0}
.ptop .sp{flex:1}
.pround{width:36px;height:36px;flex:none;border-radius:50%;background:rgba(0,0,0,.45);display:grid;place-items:center;color:#fff;transition:transform .2s var(--ease),background .2s}
.pround:hover{transform:scale(1.07);background:rgba(0,0,0,.7)}
.pround svg{width:18px;height:18px}
.pq{position:relative;width:min(320px,60%)}
.pq svg{position:absolute;left:13px;top:50%;width:16px;height:16px;transform:translateY(-50%);color:var(--sub);pointer-events:none}
.pq input{width:100%;background:rgba(0,0,0,.42);border:0;outline:0;border-radius:999px;padding:9px 14px 9px 38px;font-size:13px;transition:box-shadow .2s,background .2s}
.pq input:focus{background:rgba(0,0,0,.6);box-shadow:0 0 0 2px rgba(255,255,255,.35)}
.pbody{position:relative;z-index:2;flex:1;min-height:0;overflow:auto;padding:0 32px 48px}
.pbody.lv{opacity:0;transform:translateX(calc(var(--d,1)*-26px));transition:opacity .15s ease,transform .15s ease}
.pbody.en{animation:pgSlide .5s var(--ease) backwards}
.pbody.en .pcrd,.pbody.en .item,.pbody.en .ahead,.pbody.en .alh{animation:pgItem .6s var(--ease) backwards;animation-delay:calc(min(var(--i,0),16)*38ms + 70ms)}
.phead{display:flex;align-items:flex-end;justify-content:space-between;flex-wrap:wrap;gap:14px 20px;padding:28px 0 22px}
.psort{display:flex;gap:4px;padding:4px;border-radius:999px;background:rgba(0,0,0,.38);box-shadow:inset 0 0 0 1px rgba(255,255,255,.06)}
.psb{padding:8px 16px;border-radius:999px;font-size:13px;font-weight:700;color:var(--sub);transition:background .3s var(--ease),color .25s,transform .2s var(--ease)}
.psb:hover{color:#fff}
.psb:active{transform:scale(.96)}
.psb.on{background:#fff;color:#000}
.phead h2{margin:0;font-size:clamp(34px,5vw,72px);font-weight:900;letter-spacing:-.045em;line-height:1.1}
.phead p{margin:6px 0 0;font-size:14px;color:rgba(255,255,255,.82)}
.pgrid{transition:opacity .18s ease,transform .18s ease;display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:14px}
.pgrid.out{opacity:0;transform:translateY(10px);pointer-events:none}
.pcrd{--gl:rgb(var(--ar));position:relative;display:flex;flex-direction:column;gap:14px;padding:16px;border-radius:12px;background:linear-gradient(180deg,rgba(255,255,255,.07),rgba(255,255,255,.03));cursor:var(--cur-p);outline:0;box-shadow:inset 0 0 0 1px rgba(255,255,255,.06),0 10px 34px -16px var(--gl);transition:background .3s,transform .35s var(--ease),box-shadow .4s}
.pcrd::after{content:"";position:absolute;inset:0;border-radius:inherit;pointer-events:none;background:radial-gradient(120% 70% at 50% 0%,var(--gl),transparent 70%);opacity:.12;transition:opacity .4s}
.pcrd:hover,.pcrd:focus-visible{background:linear-gradient(180deg,rgba(255,255,255,.12),rgba(255,255,255,.05));transform:translateY(-5px);box-shadow:inset 0 0 0 1px rgba(255,255,255,.14),0 18px 48px -12px var(--gl)}
.pcrd:hover::after,.pcrd:focus-visible::after{opacity:.28}
.pcrd:active{transform:translateY(-2px) scale(.99)}
.pav{position:relative}
.pav::before{content:"";position:absolute;inset:6%;border-radius:50%;background:var(--gl);filter:blur(24px);opacity:.38;transition:opacity .4s,transform .45s var(--ease)}
.pcrd:hover .pav::before{opacity:.75;transform:scale(1.08)}
.pav .av{position:relative;z-index:1;width:100%;height:auto;aspect-ratio:1;font-size:clamp(44px,5vw,80px);transition:transform .45s var(--ease)}
.pcrd:hover .pav .av{transform:scale(1.04)}
.pbadge{z-index:2;position:absolute;right:8px;bottom:8px;padding:3px 10px;border-radius:999px;background:rgba(0,0,0,.66);backdrop-filter:blur(4px);font-weight:800;font-size:14px;font-variant-numeric:tabular-nums}
.pn{min-width:0}
.pn b,.pn span{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pn b{font-size:16px;font-weight:700}
.pn span{margin-top:2px;font-size:13px;color:var(--sub)}
.av{flex:none;display:grid;place-items:center;border-radius:50%;color:#fff;font-weight:900;text-shadow:0 2px 8px rgba(0,0,0,.35);box-shadow:0 4px 18px rgba(0,0,0,.4);width:52px;height:52px;font-size:22px}
.av.big{width:84px;height:84px;font-size:36px}
.ahead{display:flex;align-items:flex-end;gap:28px;padding:36px 0 6px}
.ahead .av.big{width:clamp(140px,16vw,232px);height:clamp(140px,16vw,232px);font-size:clamp(52px,6vw,96px);box-shadow:0 8px 60px rgba(0,0,0,.55)}
.ahead>div:last-child{min-width:0;padding-bottom:6px}
.ahead h3{margin:2px 0 8px;font-size:clamp(34px,5.4vw,88px);font-weight:900;letter-spacing:-.045em;line-height:1.1;overflow-wrap:anywhere}
.ahead p{margin:0;font-size:14px;color:rgba(255,255,255,.88)}
.alh{padding:22px 8px 8px;font-size:12px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:var(--sub)}
@keyframes pgIn{from{opacity:0;transform:translateY(28px) scale(.985)}}
@keyframes pgOut{to{opacity:0;transform:translateY(20px) scale(.99)}}
@keyframes pgSlide{from{opacity:0;transform:translateX(calc(var(--d,1)*30px))}}
@keyframes pgItem{from{opacity:0;transform:translateY(18px) scale(.97)}}
@media (max-width:860px){.pm .tx,.pm .sc{display:block}.pm .item{grid-template-columns:48px minmax(0,1fr) auto 28px;justify-content:stretch;padding:8px}}
@media (max-width:700px){.ahead{flex-direction:column;align-items:flex-start;gap:16px}.pbody{padding:0 16px 40px}.ptop{padding:12px 16px 0}}
.item .lk{width:28px;height:28px;display:grid;place-items:center;color:var(--sub)}
.item .lk svg{width:16px;height:16px}
.pbody .item{cursor:var(--cur-p)}
.cover{cursor:var(--cur-p)}
.covh{position:absolute;inset:0;z-index:2;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:8px;background:rgba(0,0,0,.52);opacity:0;transition:opacity .2s;font-size:13px;font-weight:800;text-align:center}
.cover:hover .covh{opacity:1}
.cover .covh svg{width:30px;height:30px;stroke-width:1.8;filter:none}
.covx{position:absolute;z-index:3;top:8px;right:8px;width:30px;height:30px;border-radius:50%;background:rgba(0,0,0,.62);display:grid;place-items:center;opacity:0;transition:opacity .2s,background .2s}
.cover:hover .covx{opacity:1}
.covx:hover{background:rgba(0,0,0,.88)}
.cover .covx svg{width:16px;height:16px;stroke-width:2.2;filter:none}
.cover.has>svg:first-child{display:none}
.avw{position:relative;flex:none;cursor:var(--cur-p);border-radius:50%}
.avh{position:absolute;inset:0;border-radius:50%;display:grid;place-items:center;background:rgba(0,0,0,.55);opacity:0;transition:opacity .2s}
.avw:hover .avh{opacity:1}
.avh svg{width:28px;height:28px}
.avx{position:absolute;right:-2px;top:-2px;width:26px;height:26px;border-radius:50%;background:#2c2c2c;box-shadow:0 0 0 2px rgba(26,12,17,.95);display:grid;place-items:center;color:#fff;opacity:0;transition:opacity .2s,background .2s}
.avw:hover .avx{opacity:1}
.avx:hover{background:var(--g2)}
.avx svg{width:12px;height:12px;stroke-width:2.6}
.av.img{font-size:0;text-shadow:none}
@media (prefers-reduced-motion:reduce){*,*::before,*::after{animation-duration:.01ms!important;transition-duration:.01ms!important}}


/* ---------- альбомы ---------- */
.albc{margin:0 0 16px;padding:12px 14px;border-radius:12px;background:rgba(255,255,255,.05);box-shadow:inset 0 0 0 1px rgba(255,255,255,.07);text-align:left;animation:gpop .35s var(--ease)}
.albh{display:flex;justify-content:space-between;align-items:baseline;gap:10px;font-size:12px;color:var(--sub)}
.albt{font-weight:800;color:#fff;font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.albh span:last-child{flex:none}
.albn{display:flex;align-items:baseline;gap:10px;margin:6px 0 8px}
.albn b{font-size:32px;font-weight:900;font-variant-numeric:tabular-nums;line-height:1.1;transition:color .3s}
.albn span{font-size:12px;color:var(--sub);line-height:1.3}
.albl{display:flex;flex-direction:column;gap:2px;max-height:160px;overflow:auto;margin:0 -4px}
.albr{display:flex;justify-content:space-between;gap:10px;padding:5px 8px;border-radius:6px;font-size:12px;cursor:var(--cur-p);transition:background .2s}
.albr:hover{background:rgba(255,255,255,.08)}
.albr.on{background:rgba(255,255,255,.14)}
.albr span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.albr b{font-variant-numeric:tabular-nums;flex:none}
.albr.un b{color:var(--sub);font-weight:600}
.item .sc.un{color:var(--sub)!important;font-weight:600}

/* ---------- интро при запуске (PULSE RATER) ---------- */
#intro{position:fixed;inset:0;z-index:9999;display:grid;place-items:center;overflow:hidden;background:#030103;perspective:900px;animation:iOut .9s cubic-bezier(.7,0,.25,1) 2.55s forwards;will-change:opacity,transform}
#intro .ibg{position:absolute;inset:0;background:radial-gradient(70% 55% at 50% 50%,hsl(var(--tn) 70% 10%) 0%,#080207 52%,#000 100%);opacity:0;animation:iFade 1.2s ease .05s forwards}
#intro .igr{position:absolute;inset:-50%;opacity:.07;pointer-events:none;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='180' height='180'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.85' numOctaves='2' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E");animation:iGrain .6s steps(4) infinite}
#intro .ivg{position:absolute;inset:0;pointer-events:none;background:radial-gradient(120% 90% at 50% 50%,transparent 45%,rgba(0,0,0,.85) 100%)}
#intro .ipt{position:absolute;inset:0;pointer-events:none}
#intro .ipt i{position:absolute;left:var(--x);top:var(--y);width:var(--s);height:var(--s);border-radius:50%;background:hsl(var(--tn) 100% 82%);box-shadow:0 0 8px hsl(var(--tn) 100% 62%);opacity:0;animation:iDust var(--t) ease-in-out var(--w) infinite}
#intro .icn{position:absolute;width:clamp(26px,3.6vw,54px);height:clamp(26px,3.6vw,54px);border:0 solid rgba(255,255,255,.3);opacity:0;animation:iCorner 1s cubic-bezier(.2,.8,.2,1) .15s forwards}
#intro .icn.c1{left:4.5vmin;top:4.5vmin;border-width:1px 0 0 1px;--cx:-14px;--cy:-14px}
#intro .icn.c2{right:4.5vmin;top:4.5vmin;border-width:1px 1px 0 0;--cx:14px;--cy:-14px}
#intro .icn.c3{left:4.5vmin;bottom:4.5vmin;border-width:0 0 1px 1px;--cx:-14px;--cy:14px}
#intro .icn.c4{right:4.5vmin;bottom:4.5vmin;border-width:0 1px 1px 0;--cx:14px;--cy:14px}
#intro .irg{position:absolute;left:50%;top:50%;width:22vmin;height:22vmin;margin:-11vmin 0 0 -11vmin;border-radius:50%;border:1px solid rgba(var(--ar),.75);box-shadow:0 0 26px rgba(var(--ar),.35),inset 0 0 26px rgba(var(--ar),.2);opacity:0;animation:iRing 1.9s cubic-bezier(.15,.7,.3,1) var(--d) forwards}
#intro .ig{position:absolute;left:50%;top:50%;width:70vmax;height:70vmax;margin:-35vmax 0 0 -35vmax;border-radius:50%;background:radial-gradient(circle,rgba(var(--ar),.42),rgba(var(--ar),.14) 38%,transparent 66%);opacity:0;animation:iGlow 2.6s ease-out .1s forwards}
#intro .ihalo{position:absolute;left:50%;top:50%;width:min(110vmin,1000px);height:min(110vmin,1000px);margin:calc(min(110vmin,1000px) / -2) 0 0 calc(min(110vmin,1000px) / -2);border-radius:50%;background:conic-gradient(from 0deg,transparent 0%,rgba(var(--ar),.4) 10%,transparent 26%,rgba(255,255,255,.14) 46%,transparent 60%,rgba(var(--ar),.34) 78%,transparent 92%);filter:blur(42px);-webkit-mask-image:radial-gradient(circle,#000 0,#000 30%,transparent 68%);mask-image:radial-gradient(circle,#000 0,#000 30%,transparent 68%);opacity:0;animation:iHalo 1.4s ease 1.15s forwards,iSpin 9s linear 1.15s infinite}
#intro .iecg{position:absolute;left:0;top:50%;width:100%;height:26vh;transform:translateY(-50%);overflow:visible;pointer-events:none;animation:iEcgDim .9s ease 1.3s forwards}
#intro .iecg path{fill:none;stroke:url(#iEg);stroke-linecap:round;stroke-linejoin:round;stroke-dasharray:1;stroke-dashoffset:1;vector-effect:non-scaling-stroke;animation:iDraw .95s cubic-bezier(.45,.05,.2,1) .3s forwards}
#intro .iecg .ie1{stroke-width:9;opacity:.65;filter:blur(7px)}
#intro .iecg .ie2{stroke-width:2.2}
#intro .ishk{position:absolute;left:50%;top:50%;width:30vmin;height:30vmin;margin:-15vmin 0 0 -15vmin;border-radius:50%;border:2px solid rgba(255,255,255,.9);box-shadow:0 0 40px rgba(var(--ar),.8);opacity:0;animation:iShock 1.15s cubic-bezier(.1,.7,.2,1) 1.25s forwards}
#intro .iflash{position:absolute;inset:0;pointer-events:none;background:radial-gradient(circle at 50% 50%,rgba(255,255,255,.5),rgba(var(--ar),.22) 26%,transparent 62%);opacity:0;animation:iFlash .95s ease-out 1.22s}
#intro .ic{position:relative;z-index:2;display:flex;flex-direction:column;align-items:center;gap:clamp(12px,2.2vmin,22px)}
#intro .ieb{font-size:clamp(10px,.9vw,12px);font-weight:700;letter-spacing:.55em;text-indent:.55em;color:rgba(255,255,255,.55);text-transform:uppercase;white-space:nowrap;opacity:0;animation:iEb 1.1s cubic-bezier(.2,.8,.2,1) .9s forwards}
#intro .iwm{position:relative;transform-style:preserve-3d;animation:iHit .6s ease-out 1.25s}
#intro .ib,#intro .ish{font-family:"Segoe UI Variable Display","Segoe UI",system-ui,sans-serif;font-weight:900;font-size:clamp(34px,8vw,128px);line-height:1.08;letter-spacing:-.02em;white-space:nowrap;padding:0 .08em .12em}
#intro .iL{display:inline-block;background:linear-gradient(180deg,#fff 15%,#cfcfd8 55%,#7d7d89 100%);-webkit-background-clip:text;background-clip:text;color:transparent;filter:drop-shadow(0 8px 40px rgba(var(--ar),.45));animation:iLetter .85s cubic-bezier(.2,.8,.2,1) calc(.92s + var(--k) * .035s) backwards}
#intro .iL.a{background:linear-gradient(180deg,var(--g4) 8%,var(--g2) 55%,var(--g0) 100%);-webkit-background-clip:text;background-clip:text}
#intro .ish{position:absolute;inset:0;color:transparent;pointer-events:none;background:linear-gradient(100deg,transparent 42%,rgba(255,255,255,.95) 50%,transparent 58%);background-size:300% 100%;background-position:120% 0;-webkit-background-clip:text;background-clip:text;animation:iSheen 1.1s cubic-bezier(.4,0,.2,1) 1.6s forwards}
#intro .itg{font-size:clamp(11px,1.15vw,15px);font-weight:500;letter-spacing:.34em;text-indent:.34em;text-transform:uppercase;color:rgba(255,255,255,.72);white-space:nowrap;opacity:0;animation:iTg .9s ease 1.5s forwards}
#intro .ipr{position:relative;width:clamp(160px,22vw,300px);height:2px;margin-top:clamp(4px,1.2vmin,12px);border-radius:2px;background:rgba(255,255,255,.1);overflow:hidden;opacity:0;animation:iFade .5s ease .95s forwards}
#intro .ipr i{position:absolute;inset:0;transform-origin:0 50%;transform:scaleX(0);background:linear-gradient(90deg,var(--g0),var(--g2),#fff);box-shadow:0 0 12px rgba(var(--ar),.9);animation:iFill 1.45s cubic-bezier(.5,0,.3,1) 1.05s forwards}
#intro .icr{position:absolute;left:0;right:0;bottom:3.4vh;text-align:center;font-size:12px;font-weight:600;white-space:nowrap;pointer-events:none;background:linear-gradient(100deg,rgba(255,255,255,.35) 0%,rgba(255,255,255,.35) 40%,#fff 50%,rgba(255,255,255,.35) 60%,rgba(255,255,255,.35) 100%);background-size:250% 100%;-webkit-background-clip:text;background-clip:text;color:transparent;opacity:0;animation:iCr 1s cubic-bezier(.2,.8,.2,1) .7s forwards,iCrS 2.2s linear 1.2s infinite}
@keyframes iGrain{0%{transform:translate(0,0)}25%{transform:translate(-3%,2%)}50%{transform:translate(2%,-3%)}75%{transform:translate(-2%,-1%)}100%{transform:translate(1%,3%)}}
@keyframes iDust{0%{opacity:0;transform:translateY(24px) scale(.6)}30%{opacity:.85}100%{opacity:0;transform:translateY(-70px) scale(1)}}
@keyframes iCorner{from{opacity:0;transform:translate(var(--cx),var(--cy)) scale(.6)}to{opacity:1;transform:none}}
@keyframes iRing{0%{opacity:0;transform:scale(.1)}12%{opacity:.9}100%{opacity:0;transform:scale(5)}}
@keyframes iGlow{0%{opacity:0;transform:scale(.4)}35%{opacity:1}100%{opacity:.75;transform:scale(1.15)}}
@keyframes iHalo{to{opacity:.95}}
@keyframes iSpin{to{transform:rotate(360deg)}}
@keyframes iDraw{to{stroke-dashoffset:0}}
@keyframes iEcgDim{to{opacity:.16}}
@keyframes iShock{0%{opacity:1;transform:scale(.2);border-width:3px}100%{opacity:0;transform:scale(4.4);border-width:.5px}}
@keyframes iFlash{0%{opacity:0}12%{opacity:1}100%{opacity:0}}
@keyframes iEb{from{opacity:0;letter-spacing:1.3em;filter:blur(6px)}to{opacity:1;letter-spacing:.55em;filter:blur(0)}}
@keyframes iLetter{from{opacity:0;transform:translateY(.5em) rotateX(75deg) scale(.9);filter:blur(18px)}}
@keyframes iHit{30%{transform:scale(1.045)}}
@keyframes iSheen{from{background-position:120% 0}to{background-position:-20% 0}}
@keyframes iTg{from{opacity:0;transform:translateY(8px);filter:blur(6px)}to{opacity:1;transform:none;filter:none}}
@keyframes iFill{to{transform:scaleX(1)}}
@keyframes iFade{to{opacity:1}}
@keyframes iOut{0%{opacity:1;transform:none;filter:blur(0)}100%{opacity:0;transform:scale(2.3);filter:blur(16px);visibility:hidden}}
@keyframes iSide{from{opacity:0;transform:translateX(-26px)}}
@keyframes iMain{from{opacity:0;transform:translateY(22px) scale(.99)}}
@keyframes iCr{from{opacity:0;letter-spacing:.6em;filter:blur(6px);transform:translateY(8px)}to{opacity:1;letter-spacing:.22em;filter:blur(0);transform:none}}
@keyframes iCrS{from{background-position:130% 0}to{background-position:-130% 0}}
.app>.side{animation:iSide 1s cubic-bezier(.2,.8,.2,1) 2.65s backwards}
.app>.main{animation:iMain 1s cubic-bezier(.2,.8,.2,1) 2.8s backwards}
.skipped .app>.side,.skipped .app>.main{animation:none}
#intro.skip{animation:iOut .4s ease forwards!important}
@media (prefers-reduced-motion:reduce){#intro{display:none}.app>.side,.app>.main{animation:none}}

/* ---------- заставка v2: кинематографичный режим ---------- */
@property --ipc{syntax:"<integer>";inherits:false;initial-value:0}
/* чёрные «кинопанели» раскрываются, как затвор объектива */
#intro .ibar{position:absolute;left:0;right:0;height:12vh;background:#000;z-index:3;pointer-events:none}
#intro .ibar.t{top:0;transform-origin:50% 0;animation:iBar 1.45s cubic-bezier(.76,0,.18,1) .4s forwards}
#intro .ibar.b{bottom:0;transform-origin:50% 100%;animation:iBar 1.45s cubic-bezier(.76,0,.18,1) .4s forwards}
@keyframes iBar{to{transform:scaleY(0)}}
/* большое мягкое боке — глубина кадра */
#intro .ibk{position:absolute;inset:0;pointer-events:none;overflow:hidden}
#intro .ibk i{position:absolute;left:var(--x);top:var(--y);width:var(--s);height:var(--s);margin:calc(var(--s) / -2) 0 0 calc(var(--s) / -2);border-radius:50%;background:radial-gradient(circle,rgba(var(--ar),.26) 0%,rgba(var(--ar),.1) 55%,transparent 72%);box-shadow:inset 0 0 0 1px rgba(255,255,255,.05);opacity:0;animation:iBokeh var(--t) cubic-bezier(.3,.2,.3,1) var(--w) forwards}
@keyframes iBokeh{0%{opacity:0;transform:translate3d(0,30px,0) scale(.82)}35%{opacity:var(--o)}100%{opacity:0;transform:translate3d(var(--dx),-70px,0) scale(1.18)}}
/* анаморфный блик вдоль всего кадра в момент «удара» */
#intro .iflr{position:absolute;left:50%;top:50%;width:min(135vw,1800px);height:2px;margin:-1px 0 0 calc(min(135vw,1800px) / -2);background:linear-gradient(90deg,transparent 0%,rgba(var(--ar),0) 6%,rgba(var(--ar),.85) 30%,#fff 50%,rgba(var(--ar),.85) 70%,rgba(var(--ar),0) 94%,transparent 100%);box-shadow:0 0 18px 2px rgba(var(--ar),.5);will-change:transform,opacity;opacity:0;transform:scaleX(.02);pointer-events:none;animation:iFlare 1.7s cubic-bezier(.16,.8,.2,1) 1.22s forwards}
@keyframes iFlare{0%{opacity:0;transform:scaleX(.02)}10%{opacity:1}55%{opacity:.7}100%{opacity:0;transform:scaleX(1)}}
/* надзаголовок с тонкими линейками по бокам */
#intro .ieb{display:flex;align-items:center;gap:1.6em}
#intro .ieb::before,#intro .ieb::after{content:"";flex:none;width:clamp(28px,5vw,80px);height:1px;transform:scaleX(0);animation:iRule 1.2s cubic-bezier(.2,.8,.2,1) 1s forwards}
#intro .ieb::before{background:linear-gradient(90deg,transparent,rgba(255,255,255,.55));transform-origin:100% 50%}
#intro .ieb::after{background:linear-gradient(270deg,transparent,rgba(255,255,255,.55));transform-origin:0 50%}
@keyframes iRule{to{transform:scaleX(1)}}
/* логотип: буквы поднимаются и «встают» в 3D, внизу — глянцевое отражение */
#intro .ib{perspective:760px;filter:drop-shadow(0 8px 40px rgba(var(--ar),.45))}
#intro .iL{filter:none;will-change:transform,opacity;transform-origin:50% 100%;animation:iLetter2 1s cubic-bezier(.16,.84,.24,1) calc(.92s + var(--k) * .04s) backwards}
@keyframes iLetter2{from{opacity:0;transform:translateY(.4em) rotateX(-62deg) scale(1.03)}}
#intro .ieb{letter-spacing:.55em;animation:iEb2 1.1s cubic-bezier(.2,.8,.2,1) .9s forwards}
@keyframes iEb2{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
#intro .ihalo{will-change:transform,opacity}
/* выход без размытия всего экрана — оно и давало рывок */
@keyframes iOut{0%{opacity:1;transform:none}100%{opacity:0;transform:scale(1.18);visibility:hidden}}
/* счётчик загрузки под полоской */
#intro .ipct{display:block;min-width:5ch;text-align:center;font-size:clamp(10px,.8vw,11px);font-weight:600;letter-spacing:.32em;text-indent:.32em;color:rgba(255,255,255,.5);font-variant-numeric:tabular-nums;opacity:0;animation:iFade .5s ease .95s forwards}
#intro .ipct::before{counter-reset:ipc var(--ipc);content:counter(ipc) "%";animation:iCount 1.45s cubic-bezier(.5,0,.3,1) 1.05s forwards}
@keyframes iCount{to{--ipc:100}}

/* ---------- приветствие после интро ---------- */
.app.enter>.side{animation:iSide .9s var(--ease) backwards}
.app.enter>.main{animation:iMain .9s var(--ease) .08s backwards}
#welcome{position:fixed;inset:0;z-index:9000;display:grid;place-items:center;padding:24px;overflow:auto;background:radial-gradient(90% 70% at 50% 40%,rgba(40,6,11,.8) 0%,rgba(6,1,3,.94) 100%);-webkit-backdrop-filter:blur(22px) saturate(1.2);backdrop-filter:blur(22px) saturate(1.2)}
#welcome.out{pointer-events:none;animation:wlOut .7s cubic-bezier(.4,0,.2,1) forwards}
.wl-c{width:min(100%,860px);display:grid;place-items:center;margin:auto}
.wl-step{display:none;flex-direction:column;align-items:center;text-align:center;width:100%}
#welcome.show .wl-step.on{display:flex}
.wl-step.on>*{animation:wlIn .75s var(--ease) backwards;animation-delay:calc(var(--i,0)*90ms + 60ms)}
.wl-step.leave{animation:wlLeave .24s ease forwards}
.wl-eb{display:flex;align-items:center;gap:12px;font-size:12px;font-weight:700;letter-spacing:.22em;text-transform:uppercase;color:var(--sub)}
.wl-h{margin:18px 0 0;font-weight:900;letter-spacing:-.03em;line-height:1.1;font-size:clamp(30px,6.4vw,92px)}
.wl-brand{--tile:600px;display:inline-block;white-space:nowrap;padding:0 .1em .14em 0;background:linear-gradient(100deg,#6c6c76 0%,#a3a3ad 20%,#ffffff 38%,#c8c8d1 52%,#8d8d97 68%,#f2f2f6 84%,#6c6c76 100%);background-size:600px 100%;-webkit-background-clip:text;background-clip:text;color:transparent;animation:shim 5s linear infinite;filter:drop-shadow(0 0 36px rgba(var(--ar),.35))}
.wl-t{margin:0;font-size:clamp(30px,4.6vw,54px);font-weight:900;letter-spacing:-.04em;line-height:1.1}
.wl-p{margin:14px 0 0;max-width:520px;font-size:clamp(15px,1.6vw,18px);line-height:1.5;color:var(--sub)}
.wl-act{margin-top:34px}
.wl-go{position:relative;display:inline-flex;align-items:center;gap:12px;padding:17px 44px;border-radius:999px;background:linear-gradient(135deg,var(--g2),var(--g));color:#fff;font-weight:800;font-size:18px;letter-spacing:.01em;box-shadow:0 14px 44px -10px rgba(var(--ar),.75),inset 0 1px 0 rgba(255,255,255,.3);transition:transform .3s var(--ease),box-shadow .3s}
.wl-go::after{content:"";position:absolute;inset:0;border-radius:inherit;pointer-events:none;animation:wlRing 2.6s ease-out infinite}
.wl-go:hover{transform:scale(1.06);box-shadow:0 18px 56px -8px rgba(var(--ar),.95),inset 0 1px 0 rgba(255,255,255,.35)}
.wl-go:active{transform:scale(.97)}
.wl-go:focus-visible{outline:2px solid #fff;outline-offset:4px}
.wl-go svg{width:20px;height:20px;transition:transform .3s var(--ease)}
.wl-go:hover svg{transform:translateX(4px)}
.wl-hint{margin-top:18px;font-size:12px;color:rgba(255,255,255,.4)}
.wl-cards{display:grid;grid-template-columns:1fr 1fr;gap:18px;width:100%;margin-top:34px}
.wl-card{--gl:hsl(calc(var(--hh,352) + var(--bo)) 90% 58%);position:relative;display:flex;flex-direction:column;align-items:flex-start;gap:12px;padding:28px 26px 26px;border-radius:18px;text-align:left;overflow:hidden;outline:0;background:linear-gradient(180deg,rgba(255,255,255,.08),rgba(255,255,255,.03));box-shadow:inset 0 0 0 1px rgba(255,255,255,.08),0 14px 44px -20px var(--gl);transition:transform .4s var(--ease),box-shadow .4s}
.wl-card::after{content:"";position:absolute;inset:0;border-radius:inherit;pointer-events:none;background:radial-gradient(120% 80% at 50% 0%,var(--gl),transparent 70%);opacity:.12;transition:opacity .4s}
.wl-card:hover,.wl-card:focus-visible{transform:translateY(-6px);box-shadow:inset 0 0 0 1px rgba(255,255,255,.18),0 24px 60px -14px var(--gl)}
.wl-card:hover::after,.wl-card:focus-visible::after{opacity:.3}
.wl-card:active{transform:translateY(-2px) scale(.99)}
.wl-ic{width:56px;height:56px;border-radius:16px;display:grid;place-items:center;color:#fff;background:linear-gradient(135deg,var(--gl),rgba(0,0,0,.35));box-shadow:0 10px 30px -8px var(--gl)}
.wl-ic svg{width:28px;height:28px}
.wl-card b{font-size:24px;font-weight:900;letter-spacing:-.03em}
.wl-card p{margin:0;font-size:14px;line-height:1.5;color:var(--sub)}
.wl-back{display:inline-flex;align-items:center;gap:8px;margin-top:26px;padding:8px 14px;border-radius:999px;font-size:13px;font-weight:700;color:var(--sub);transition:color .2s,background .2s}
.wl-back:hover{color:#fff;background:rgba(255,255,255,.08)}
.wl-back svg{width:16px;height:16px}
.wl-form{display:flex;flex-direction:column;gap:14px;width:min(100%,460px);margin-top:30px;text-align:left}
.wl-form label{display:flex;flex-direction:column;gap:8px;font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--sub)}
.wl-in{width:100%;background:rgba(255,255,255,.07);border:0;outline:0;border-radius:12px;padding:14px 16px;font-size:16px;font-weight:500;letter-spacing:0;text-transform:none;color:#fff;caret-color:var(--g2);box-shadow:inset 0 0 0 1px rgba(255,255,255,.1);transition:box-shadow .2s,background .2s}
.wl-in:focus{background:rgba(255,255,255,.1);box-shadow:inset 0 0 0 1px rgba(255,255,255,.1),0 0 0 2px rgba(255,255,255,.35)}
.wl-in::placeholder{color:rgba(255,255,255,.35)}
.wl-btns{display:flex;flex-wrap:wrap;gap:12px;justify-content:center;align-items:center;margin-top:28px}
.wl-btns .wl-go{padding:15px 32px;font-size:16px}
.wl-ghost{padding:15px 26px;border-radius:999px;font-weight:700;font-size:15px;background:rgba(255,255,255,.08);box-shadow:inset 0 0 0 1px rgba(255,255,255,.12);transition:background .25s,transform .25s var(--ease)}
.wl-ghost:hover{background:rgba(255,255,255,.14);transform:scale(1.04)}
.wl-ghost:active{transform:scale(.97)}
@keyframes wlIn{from{opacity:0;transform:translateY(22px)}}
@keyframes wlLeave{to{opacity:0;transform:translateY(-12px) scale(.985)}}
@keyframes wlOut{to{opacity:0;transform:scale(1.04)}}
@keyframes wlRing{0%{box-shadow:0 0 0 0 rgba(var(--ar),.55)}70%,100%{box-shadow:0 0 0 22px rgba(var(--ar),0)}}
@media (max-width:640px){.wl-cards{grid-template-columns:1fr}}
@media (prefers-reduced-motion:reduce){.app.enter>.side,.app.enter>.main{animation:none}}

/* ---------- пьедестал: топ-3 артистов ---------- */
.brand.on span{filter:drop-shadow(0 0 16px rgba(var(--ar),.6))}
.tpbody{position:relative;z-index:2;flex:1;min-height:0;overflow:auto;display:flex;flex-direction:column;padding:0 32px}
.tph{flex:none;text-align:center;padding:6px 0 0;animation:pgItem .6s var(--ease) backwards}
.tph h2{margin:8px 0 0;font-family:"Bahnschrift","Segoe UI Variable Display","Segoe UI",system-ui,sans-serif;font-size:clamp(30px,4.4vw,64px);font-weight:800;letter-spacing:-.02em;line-height:1.05;background:linear-gradient(180deg,#fff 25%,#b9b9c6 100%);-webkit-background-clip:text;background-clip:text;color:transparent}
.tph p{margin:8px 0 0;font-size:14px;color:rgba(255,255,255,.7)}
.pod{position:relative;flex:1;min-height:340px;width:100%;max-width:1000px;margin:0 auto;display:flex;align-items:flex-end;justify-content:center;gap:clamp(8px,2vw,26px)}
.pod::after{content:"";position:absolute;left:-4%;right:-4%;bottom:0;height:2px;background:linear-gradient(90deg,transparent,rgba(255,255,255,.35),transparent);box-shadow:0 0 22px 2px rgba(255,80,100,.35)}
.tpl{position:relative;flex:1 1 0;max-width:300px;min-width:0;display:flex;flex-direction:column;align-items:center;justify-content:flex-end}
.tpl.p1{--rgb:255,201,74;--s:clamp(84px,11vw,150px);--sh:min(34vh,270px);--ha:.8;--hb:16px;--d:.75s}
.tpl.p2{--rgb:196,212,255;--s:clamp(70px,9vw,120px);--sh:min(25vh,200px);--ha:.5;--hb:14px;--d:.4s}
.tpl.p3{--rgb:226,142,84;--s:clamp(62px,8vw,104px);--sh:min(18vh,145px);--ha:.3;--hb:12px;--d:.1s}
.tpi{position:relative;width:100%;display:flex;flex-direction:column;align-items:center;gap:5px;margin-bottom:14px;animation:tpPop .8s var(--ease) var(--d) backwards}
.tp-crown{width:clamp(30px,3.4vw,46px);height:clamp(30px,3.4vw,46px);margin-bottom:-4px;color:#ffd35e;filter:drop-shadow(0 0 10px rgba(255,200,70,.9));animation:tpBob 2.6s ease-in-out infinite}
.tp-crown svg{width:100%;height:100%;fill:currentColor;stroke:none}
.tp-av{position:relative;isolation:isolate;width:var(--s);height:var(--s);border-radius:50%;padding:3px;background:conic-gradient(from 200deg,rgba(var(--rgb),1),rgba(var(--rgb),.25) 35%,rgba(var(--rgb),.9) 70%,rgba(var(--rgb),1))}
.tp-av .av{width:100%;height:100%;font-size:calc(var(--s)*.4);box-shadow:inset 0 0 0 2px rgba(0,0,0,.5)}
.tp-av::before{content:"";position:absolute;inset:-30%;z-index:-1;border-radius:50%;background:radial-gradient(circle,rgba(var(--rgb),var(--ha)) 0%,rgba(var(--rgb),calc(var(--ha)*.35)) 40%,transparent 70%);filter:blur(var(--hb))}
.p1 .tp-av::before{animation:tpPulse 2.4s ease-in-out infinite}
.p2 .tp-av::before{animation:tpPulse 3.6s ease-in-out infinite}
.tp-rays{position:absolute;left:50%;top:50%;z-index:-2;width:min(78vh,560px);height:min(78vh,560px);margin:calc(min(78vh,560px)/-2) 0 0 calc(min(78vh,560px)/-2);border-radius:50%;pointer-events:none;background:repeating-conic-gradient(from 0deg,rgba(var(--rgb),.2) 0 6deg,transparent 6deg 20deg);-webkit-mask-image:radial-gradient(circle,#000 0,rgba(0,0,0,.55) 30%,transparent 68%);mask-image:radial-gradient(circle,#000 0,rgba(0,0,0,.55) 30%,transparent 68%);animation:tpSpin 38s linear infinite}
.tp-nm{max-width:100%;padding:0 4px;font-size:clamp(15px,1.7vw,24px);font-weight:800;letter-spacing:-.01em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;text-shadow:0 2px 12px rgba(0,0,0,.5)}
.tp-sc{font-family:"Bahnschrift","Segoe UI Variable Display","Segoe UI",system-ui,sans-serif;font-weight:700;font-size:clamp(26px,3.4vw,52px);line-height:1;letter-spacing:.01em;font-variant-numeric:tabular-nums;text-shadow:0 0 22px currentColor}
.p1 .tp-sc{font-size:clamp(32px,4.4vw,66px);text-shadow:0 0 30px currentColor,0 0 60px currentColor}
.tp-ct{font-size:12px;color:rgba(255,255,255,.6)}
.tp-step{position:relative;width:100%;height:var(--sh);border-radius:14px 14px 0 0;overflow:hidden;display:grid;place-items:center;background:linear-gradient(180deg,rgba(var(--rgb),.32) 0%,rgba(30,14,20,.92) 62%);box-shadow:inset 0 1px 0 rgba(255,255,255,.4),inset 0 0 0 1px rgba(255,255,255,.06);transform-origin:50% 100%;animation:tpGrow .9s var(--ease) var(--d) backwards}
.tp-step b{font-family:"Bahnschrift","Segoe UI Variable Display","Segoe UI",system-ui,sans-serif;font-size:clamp(46px,7vw,110px);font-weight:800;line-height:1;color:rgba(var(--rgb),.9);text-shadow:0 0 28px rgba(var(--rgb),.7);opacity:.9}
.tpl.p1{filter:drop-shadow(0 -10px 46px rgba(255,201,74,.55))}
.tpl.p2{filter:drop-shadow(0 -8px 30px rgba(196,212,255,.34))}
.tpl.p3{filter:drop-shadow(0 -6px 16px rgba(226,142,84,.2))}
.p1 .tp-step::after,.p2 .tp-step::after{content:"";position:absolute;inset:0;background:linear-gradient(105deg,transparent 38%,rgba(255,255,255,.22) 50%,transparent 62%);background-size:260% 100%;animation:tpSheen 3.4s linear infinite}
.p2 .tp-step::after{opacity:.5;animation-duration:5.2s}
.tpl.empty .tpi{visibility:hidden}
.tpl.empty{filter:none}
.tpl.empty .tp-step{background:linear-gradient(180deg,rgba(255,255,255,.08),rgba(255,255,255,.02));box-shadow:inset 0 0 0 1px rgba(255,255,255,.06)}
.tpl.empty .tp-step b{color:rgba(255,255,255,.22);text-shadow:none}
.tp-spk{position:absolute;inset:0;pointer-events:none}
.tp-spk i{position:absolute;left:var(--x);top:var(--y);width:var(--z,4px);height:var(--z,4px);border-radius:50%;background:rgb(var(--rgb));box-shadow:0 0 8px 2px rgba(var(--rgb),.9);opacity:0;animation:tpRise var(--t,3s) ease-out var(--w,0s) infinite}
.tp-empty{margin:auto;text-align:center;color:var(--sub)}
.tp-empty b{display:block;font-size:20px;color:#fff;margin-bottom:6px}
@keyframes tpPop{from{opacity:0;transform:translateY(34px) scale(.92)}}
@keyframes tpGrow{from{transform:scaleY(.02);opacity:.2}}
@keyframes tpPulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.62;transform:scale(1.1)}}
@keyframes tpSpin{to{transform:rotate(360deg)}}
@keyframes tpBob{0%,100%{transform:translateY(0) rotate(-3deg)}50%{transform:translateY(-5px) rotate(3deg)}}
@keyframes tpSheen{from{background-position:140% 0}to{background-position:-140% 0}}
@keyframes tpRise{0%{opacity:0;transform:translate(0,10px) scale(.4)}18%{opacity:1}100%{opacity:0;transform:translate(var(--dx,0px),-90px) scale(1)}}
@media (max-width:700px){.tpbody{padding:0 14px}.tp-ct{display:none}}
@media (prefers-reduced-motion:reduce){.tp-rays,.tp-crown,.tp-av::before,.tp-spk i,.tp-step::after{animation:none!important}}

/* ---------- общий рейтинг ---------- */
.rk{--ph:352}
.rkg{display:flex;flex-wrap:wrap;gap:8px;padding:0 0 20px;animation:pgItem .6s var(--ease) .05s backwards}
.rkc{padding:8px 16px;border-radius:999px;font-size:13px;font-weight:700;color:var(--sub);background:rgba(0,0,0,.38);box-shadow:inset 0 0 0 1px rgba(255,255,255,.06);transition:background .3s var(--ease),color .25s,transform .2s var(--ease)}
.rkc em{font-style:normal;font-weight:600;opacity:.55;margin-left:6px}
.rkc:hover{color:#fff}
.rkc:active{transform:scale(.96)}
.rkc.on{background:#fff;color:#000}
.rkl{display:flex;flex-direction:column;gap:8px;transition:opacity .17s ease,transform .17s ease}
.rkl.out{opacity:0;transform:translateY(10px);pointer-events:none}
.rkr{--c:255,255,255;position:relative;display:grid;grid-template-columns:48px 52px minmax(0,1fr) minmax(90px,26%) 64px;align-items:center;gap:16px;padding:10px 20px 10px 8px;border-radius:12px;background:linear-gradient(90deg,rgba(255,255,255,.07),rgba(255,255,255,.03));box-shadow:inset 0 0 0 1px rgba(255,255,255,.06);cursor:var(--cur-p);outline:0;transition:transform .3s var(--ease),background .3s,box-shadow .3s;animation:pgItem .55s var(--ease) calc(min(var(--i,0),14)*34ms) backwards}
.rkr:hover,.rkr:focus-visible{transform:translateX(5px);background:linear-gradient(90deg,rgba(255,255,255,.13),rgba(255,255,255,.05));box-shadow:inset 0 0 0 1px rgba(255,255,255,.14)}
.rkr .av{width:48px;height:48px;font-size:20px}
.rkp{font-family:"Bahnschrift","Segoe UI Variable Display","Segoe UI",system-ui,sans-serif;font-size:22px;font-weight:800;text-align:center;color:var(--sub);font-variant-numeric:tabular-nums}
.rkr.r1{--c:255,201,74;background:linear-gradient(90deg,rgba(255,201,74,.2),rgba(255,255,255,.04) 65%);box-shadow:inset 0 0 0 1px rgba(255,201,74,.38),0 0 34px -10px rgba(255,201,74,.55)}
.rkr.r2{--c:196,212,255;background:linear-gradient(90deg,rgba(196,212,255,.15),rgba(255,255,255,.04) 65%);box-shadow:inset 0 0 0 1px rgba(196,212,255,.28)}
.rkr.r3{--c:226,142,84;background:linear-gradient(90deg,rgba(226,142,84,.15),rgba(255,255,255,.04) 65%);box-shadow:inset 0 0 0 1px rgba(226,142,84,.28)}
.rkr.r1 .rkp,.rkr.r2 .rkp,.rkr.r3 .rkp{font-size:28px;color:rgb(var(--c));text-shadow:0 0 18px rgba(var(--c),.75)}
.rkr.r1::after{content:"";position:absolute;inset:0;border-radius:inherit;pointer-events:none;background:linear-gradient(105deg,transparent 38%,rgba(255,255,255,.14) 50%,transparent 62%);background-size:260% 100%;animation:tpSheen 4s linear infinite}
.rkn{min-width:0}
.rkn b,.rkn span{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rkn b{font-size:16px;font-weight:700}
.rkn span{margin-top:2px;font-size:13px;color:var(--sub)}
.rkbar{height:6px;border-radius:3px;background:rgba(255,255,255,.09);overflow:hidden}
.rkbar i{display:block;height:100%;border-radius:inherit;transform-origin:0 50%;animation:rkFill .9s var(--ease) calc(min(var(--i,0),14)*34ms + 160ms) backwards}
.rks{font-family:"Bahnschrift","Segoe UI Variable Display","Segoe UI",system-ui,sans-serif;font-size:26px;font-weight:700;text-align:right;font-variant-numeric:tabular-nums;text-shadow:0 0 18px currentColor}
@keyframes rkFill{from{transform:scaleX(0)}}
@media (max-width:700px){.rkr{grid-template-columns:34px 44px minmax(0,1fr) 56px;gap:10px;padding-right:12px}.rkbar{display:none}.rkr .av{width:44px;height:44px}.rks{font-size:22px}}
@media (prefers-reduced-motion:reduce){.rkr.r1::after{animation:none}}

/* ---------- топ-3: фон и пьедестал (умеренно) ---------- */
.tp-fx{position:absolute;inset:0;z-index:1;overflow:hidden;pointer-events:none;border-radius:inherit}
.tp-fx canvas{position:absolute;inset:0;width:100%;height:100%}
.tp-fx i{position:absolute;display:block}
.tb{border-radius:50%;filter:blur(70px);mix-blend-mode:screen;opacity:.45;will-change:transform}
.tb.b1{width:46vmax;height:46vmax;left:-14%;top:-24%;background:radial-gradient(circle,rgba(255,201,74,.4),transparent 65%);animation:tpDrA 18s ease-in-out infinite alternate}
.tb.b2{width:44vmax;height:44vmax;right:-16%;top:6%;background:radial-gradient(circle,rgba(var(--ar),.36),transparent 65%);animation:tpDrB 22s ease-in-out infinite alternate}
@keyframes tpDrA{to{transform:translate(14vmax,10vmax) scale(1.15)}}
@keyframes tpDrB{to{transform:translate(-16vmax,12vmax) scale(.9)}}
.pod::before{content:"";position:absolute;left:10%;right:10%;bottom:-24px;height:50px;border-radius:50%;background:radial-gradient(ellipse at 50% 50%,rgba(255,201,74,.38),rgba(255,70,100,.14) 45%,transparent 70%);filter:blur(14px);pointer-events:none}
.tpl.p1::before{content:"";position:absolute;left:50%;bottom:0;width:90%;height:150%;transform:translateX(-50%);z-index:-1;pointer-events:none;clip-path:polygon(32% 100%,68% 100%,100% 0,0 0);background:linear-gradient(0deg,rgba(var(--rgb),.35),transparent 85%);opacity:.5;animation:tpBeam 3.6s ease-in-out infinite}
@keyframes tpBeam{0%,100%{opacity:.25}50%{opacity:.7}}
.tp-step::before{content:"";position:absolute;left:0;right:0;top:0;height:2px;z-index:2;background:linear-gradient(90deg,transparent,rgba(255,255,255,.9),transparent);background-size:40% 100%;background-repeat:no-repeat;animation:tpEdge 3.2s linear infinite}
@keyframes tpEdge{from{background-position:-60% 0}to{background-position:160% 0}}
.tpl.empty .tp-step::before{display:none}
@media (prefers-reduced-motion:reduce){.tb,.tpl.p1::before,.tp-step::before{animation:none!important}}

/* ---------- анимация закрытия ---------- */
#outro{position:fixed;inset:0;z-index:20000;display:none;place-items:center;overflow:hidden;background:#000;opacity:0;pointer-events:none}
body.bye{pointer-events:none}
body.bye #outro{display:grid;pointer-events:auto;animation:oBlack .9s ease .3s forwards}
body.bye .app,body.bye #credit{animation:oApp .95s cubic-bezier(.5,0,.8,.4) forwards}
#outro .og{position:absolute;left:50%;top:50%;width:80vmax;height:80vmax;margin:-40vmax 0 0 -40vmax;border-radius:50%;background:radial-gradient(circle,rgba(var(--ar),.45),rgba(var(--ar),.16) 38%,transparent 66%);opacity:0}
body.bye #outro .og{animation:oGlow 2.5s ease-in-out .5s forwards}
#outro .oc{position:relative;display:flex;flex-direction:column;align-items:center;gap:clamp(18px,3.4vmin,34px)}
#outro .ot{font-weight:900;font-size:clamp(26px,5.6vw,88px);line-height:1.1;letter-spacing:-.03em;white-space:nowrap;padding:0 .1em .14em 0;background:linear-gradient(180deg,#fff 20%,#a9a9b6 100%);-webkit-background-clip:text;background-clip:text;color:transparent;opacity:0}
body.bye #outro .ot{animation:oIn .95s cubic-bezier(.2,.8,.2,1) .85s forwards,oOut .6s ease-in 2.2s forwards}
#outro .obw{display:flex;align-items:center;justify-content:center;gap:clamp(4px,.6vw,9px);height:8vh;opacity:0}
body.bye #outro .obw{animation:oFadeIn .5s ease 1.1s forwards,oOut .5s ease-in 2.2s forwards}
#outro .obw i{width:clamp(2px,.3vw,4px);height:calc(var(--h)*1%);border-radius:3px;background:linear-gradient(var(--g4),var(--g));box-shadow:0 0 12px rgba(var(--ar),.55);transform-origin:50% 50%}
body.bye #outro .obw i{animation:oBar .9s ease-in-out calc(1.1s + var(--k)*.02s) 2 alternate,oBarEnd .9s cubic-bezier(.5,0,.9,.5) 1.9s forwards}
#outro .os{font-size:clamp(11px,1vw,14px);font-weight:600;letter-spacing:.5em;text-indent:.5em;color:rgba(255,255,255,.55);opacity:0}
body.bye #outro .os{animation:oFadeIn .6s ease 1.5s forwards,oOut .5s ease-in 2.2s forwards}
#outro .ol{position:absolute;left:0;right:0;top:50%;height:2px;margin-top:-1px;background:linear-gradient(90deg,transparent,var(--g4) 28%,#fff 50%,var(--g4) 72%,transparent);box-shadow:0 0 30px 6px rgba(var(--ar),.6);opacity:0;transform:scaleX(0)}
body.bye #outro .ol{animation:oLine .85s cubic-bezier(.4,0,.2,1) 2.2s both}
@keyframes oBlack{to{opacity:1}}
@keyframes oApp{to{opacity:0;transform:scale(.94);filter:blur(10px)}}
@keyframes oGlow{0%{opacity:0;transform:scale(1.4)}35%{opacity:1;transform:scale(1)}100%{opacity:0;transform:scale(.25)}}
@keyframes oIn{from{opacity:0;letter-spacing:.34em;filter:blur(14px)}to{opacity:1;letter-spacing:-.03em;filter:blur(0)}}
@keyframes oOut{to{opacity:0;transform:scale(1.06);filter:blur(12px)}}
@keyframes oFadeIn{to{opacity:1}}
@keyframes oBar{from{transform:scaleY(1)}to{transform:scaleY(.42)}}
@keyframes oBarEnd{to{transform:scaleY(.03)}}
@keyframes oLine{0%{opacity:0;transform:scaleX(.05)}30%{opacity:1;transform:scaleX(1)}100%{opacity:0;transform:scaleX(0)}}
@media (prefers-reduced-motion:reduce){body.bye #outro{animation:none;opacity:1}}

/* ---------- подтверждение выхода ---------- */
#qc{position:fixed;inset:0;z-index:15000;display:grid;place-items:center;padding:24px;background:rgba(4,1,2,.62);-webkit-backdrop-filter:blur(8px);backdrop-filter:blur(8px);animation:qFade .25s ease backwards}
#qc.out{animation:qFadeOut .2s ease forwards;pointer-events:none}
.qcard{width:min(420px,100%);padding:30px 28px 22px;border-radius:18px;text-align:center;background:linear-gradient(180deg,hsl(var(--tn) 54% 10% / .96),hsl(var(--tn) 40% 4.5% / .97));box-shadow:inset 0 0 0 1px rgba(255,255,255,.1),0 30px 80px -20px rgba(var(--ar),.55),0 0 0 1px rgba(0,0,0,.5);animation:qPop .4s var(--ease) backwards}
#qc.out .qcard{animation:qPopOut .2s ease forwards}
.qic{width:60px;height:60px;margin:0 auto 16px;border-radius:50%;display:grid;place-items:center;color:var(--g3);background:radial-gradient(circle,rgba(var(--ar),.28),rgba(var(--ar),.06));box-shadow:inset 0 0 0 1px rgba(var(--ar),.35),0 0 30px rgba(var(--ar),.35)}
.qic svg{width:28px;height:28px}
.qcard b{display:block;font-size:20px;font-weight:800;letter-spacing:-.01em}
.qcard p{margin:8px 0 0;font-size:14px;color:var(--sub)}
.qbt{display:flex;gap:10px;margin-top:24px}
.qbt button{flex:1;padding:13px 16px;border-radius:999px;font-weight:800;font-size:15px;outline:0;transition:transform .2s var(--ease),background .2s,box-shadow .2s}
.qno{background:rgba(255,255,255,.09);box-shadow:inset 0 0 0 1px rgba(255,255,255,.14)}
.qno:hover{background:rgba(255,255,255,.16)}
.qyes{background:linear-gradient(180deg,var(--g2),var(--g));box-shadow:0 8px 26px -8px rgba(var(--ar),.8)}
.qyes:hover{transform:scale(1.04)}
.qbt button:active{transform:scale(.97)}
.qbt button:focus-visible{box-shadow:0 0 0 2px rgba(255,255,255,.55)}
.qh{margin-top:14px;font-size:12px;color:rgba(255,255,255,.4)}
@keyframes qFade{from{opacity:0}}
@keyframes qFadeOut{to{opacity:0}}
@keyframes qPop{from{opacity:0;transform:translateY(18px) scale(.94)}}
@keyframes qPopOut{to{opacity:0;transform:translateY(8px) scale(.97)}}
body.bye #qc{display:none}

/* ---------- каталог релизов ---------- */
.ct{--ph:352}
.ctbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:6px}
.ctin{flex:1 1 320px;display:flex;align-items:center;gap:10px;height:48px;padding:0 18px;border-radius:999px;background:rgba(0,0,0,.45);box-shadow:inset 0 0 0 1px rgba(255,255,255,.1);transition:box-shadow .25s}
.ctin:focus-within{box-shadow:inset 0 0 0 1px rgba(255,255,255,.4),0 0 30px -8px rgba(var(--ar),.6)}
.ctin svg{width:20px;height:20px;opacity:.6;flex:none}
.ctin input{flex:1;min-width:0;background:none;border:0;outline:0;font-size:15px}
.ctseg{display:flex;padding:3px;border-radius:999px;background:rgba(0,0,0,.45);box-shadow:inset 0 0 0 1px rgba(255,255,255,.08)}
.ctseg button{padding:9px 16px;border-radius:999px;font-size:13px;font-weight:700;color:var(--sub);transition:background .3s var(--ease),color .2s}
.ctseg button.on{background:#fff;color:#000}
.ctnote{font-size:12px;color:rgba(255,255,255,.38);margin:8px 4px 0}
.ctv{animation:pgItem .5s var(--ease) backwards}
.cth{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:26px 0 12px;font-size:13px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;color:var(--sub)}
.cth button{font-size:12px;letter-spacing:.02em;text-transform:none;padding:7px 14px;border-radius:999px;background:rgba(255,255,255,.1);color:#fff;font-weight:700;transition:background .2s}
.cth button:hover{background:rgba(255,255,255,.2)}
.ctg{display:flex;flex-wrap:wrap;gap:8px;margin-top:18px}
.ctgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(148px,1fr));gap:14px}
.ctcard{padding:12px;border-radius:14px;text-align:left;background:rgba(255,255,255,.05);box-shadow:inset 0 0 0 1px rgba(255,255,255,.06);transition:transform .3s var(--ease),background .25s;animation:pgItem .5s var(--ease) calc(min(var(--i,0),12)*30ms) backwards}
.ctcard:hover{transform:translateY(-4px);background:rgba(255,255,255,.1)}
.ctcard .im{aspect-ratio:1;border-radius:10px;background:#241418 center/cover no-repeat;display:grid;place-items:center;font-size:34px;font-weight:800;color:rgba(255,255,255,.5)}
.ctcard.ar .im{border-radius:50%}
.ctcard b,.ctcard span{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ctcard b{margin-top:10px;font-size:14px}
.ctcard span{margin-top:2px;font-size:12px;color:var(--sub)}
.ctl{display:flex;flex-direction:column;gap:6px}
.ctr{display:grid;grid-template-columns:48px minmax(0,1fr) auto;gap:14px;align-items:center;padding:8px 12px 8px 8px;border-radius:12px;background:rgba(255,255,255,.04);box-shadow:inset 0 0 0 1px rgba(255,255,255,.05);transition:background .25s;animation:pgItem .5s var(--ease) calc(min(var(--i,0),14)*28ms) backwards}
.ctr:hover{background:rgba(255,255,255,.09)}
.ctr .im{width:48px;height:48px;border-radius:8px;background:#241418 center/cover no-repeat}
.ctn{min-width:0}
.ctn b,.ctn span{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ctn b{font-size:15px}
.ctn span{margin-top:2px;font-size:12.5px;color:var(--sub)}
.cta{display:flex;align-items:center;gap:8px}
.cta button{height:36px;border-radius:999px;display:grid;place-items:center;transition:transform .2s var(--ease),background .2s,opacity .2s}
.ctp,.ctadd{width:36px;background:rgba(255,255,255,.1)}
.ctp:hover,.ctadd:hover{background:rgba(255,255,255,.22);transform:scale(1.08)}
.ctp svg,.ctadd svg{width:18px;height:18px}
.ctp.on{background:#fff;color:#000}
.ctadd:disabled{opacity:.3;pointer-events:none}
.ctrate{padding:0 18px;font-size:13px;font-weight:800;background:linear-gradient(180deg,var(--g2),var(--g));box-shadow:0 6px 20px -8px rgba(var(--ar),.8)}
.ctrate:hover{transform:scale(1.05)}
.ctrate.done{background:rgba(255,255,255,.12);box-shadow:none}
.cthead{display:flex;gap:22px;align-items:center;margin:8px 0 4px;flex-wrap:wrap}
.cthead .im{width:132px;height:132px;border-radius:14px;background:#241418 center/cover no-repeat;flex:none;box-shadow:0 20px 50px -20px rgba(var(--ar),.6)}
.cthead.ar .im{border-radius:50%}
.cthead h2{margin:0;font-size:clamp(26px,3.4vw,42px);letter-spacing:-.02em}
.cthead p{margin:6px 0 0;color:var(--sub);font-size:14px}
.cthead .bt{display:flex;gap:10px;margin-top:14px;flex-wrap:wrap}
.cthead .bt button,.cthead .bt a{height:38px;padding:0 18px;border-radius:999px;display:inline-grid;place-items:center;font-size:13px;font-weight:800;background:rgba(255,255,255,.12);color:#fff;text-decoration:none;cursor:var(--cur-p)}
.cthead .bt .pri{background:linear-gradient(180deg,var(--g2),var(--g))}
.ctback{margin:2px 0 10px;padding:8px 16px 8px 12px;border-radius:999px;background:rgba(0,0,0,.4);font-size:13px;font-weight:700;display:inline-flex;gap:6px;align-items:center;color:var(--sub);transition:color .2s}
.ctback:hover{color:#fff}
.ctback svg{width:16px;height:16px}
.ctload{margin:60px 0;text-align:center;color:var(--sub);font-size:14px}
.ctload i{display:block;width:34px;height:34px;margin:0 auto 14px;border-radius:50%;border:3px solid rgba(255,255,255,.15);border-top-color:#ff5870;animation:ctSpin .8s linear infinite}
.ctload button{margin-top:14px;padding:9px 20px;border-radius:999px;background:rgba(255,255,255,.12);font-weight:700}
@keyframes ctSpin{to{transform:rotate(360deg)}}
@media (max-width:700px){.ctr{grid-template-columns:44px minmax(0,1fr)}.cta{grid-column:1/-1;justify-content:flex-end}.cthead .im{width:96px;height:96px}}
@media (prefers-reduced-motion:reduce){.ctload i{animation-duration:2s}}

/* ---------- «Слушать целиком» ---------- */
.fl{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-top:10px;font-size:13px;color:var(--sub);animation:pgItem .4s var(--ease) backwards}
.fl a,.ctmenu a{padding:7px 14px;border-radius:999px;background:rgba(255,255,255,.09);box-shadow:inset 0 0 0 1px rgba(255,255,255,.08);color:#fff;font-weight:700;text-decoration:none;cursor:var(--cur-p);transition:background .2s,transform .2s var(--ease)}
.fl a:hover,.ctmenu a:hover{background:rgba(255,255,255,.2);transform:translateY(-1px)}
.ctfull{width:auto!important;padding:0 14px;font-size:12px;font-weight:800;background:rgba(255,255,255,.1)}
.ctfull:hover{background:rgba(255,255,255,.22)}
.cta{position:relative}
.ctmenu{position:absolute;right:0;top:42px;z-index:6;display:flex;flex-direction:column;gap:6px;padding:10px;border-radius:14px;background:rgba(22,9,13,.97);box-shadow:inset 0 0 0 1px rgba(255,255,255,.12),0 20px 50px -10px rgba(0,0,0,.8);animation:qPop .25s var(--ease) backwards;white-space:nowrap}
.ctmenu a{display:block;text-align:left;border-radius:10px}

/* ---------- полное прослушивание внутри приложения ---------- */
.flb{display:inline-flex;align-items:center;gap:8px;height:38px;padding:0 18px 0 14px;border-radius:999px;font-weight:800;font-size:14px;background:linear-gradient(180deg,var(--g2),var(--g));box-shadow:0 8px 24px -8px rgba(var(--ar),.8);transition:transform .2s var(--ease)}
.flb:hover{transform:scale(1.04)}
.flb svg{width:18px;height:18px}
#emb{position:fixed;inset:0;z-index:15000;display:grid;place-items:center;padding:20px;background:rgba(4,1,2,.7);-webkit-backdrop-filter:blur(10px);backdrop-filter:blur(10px);animation:qFade .25s ease backwards}
#emb.out{animation:qFadeOut .2s ease forwards;pointer-events:none}
.ebox{width:min(760px,100%);max-height:100%;overflow:auto;padding:22px 24px 20px;border-radius:20px;background:linear-gradient(180deg,rgba(40,12,18,.97),rgba(14,6,9,.98));box-shadow:inset 0 0 0 1px rgba(255,255,255,.1),0 30px 80px -20px rgba(var(--ar),.5);animation:qPop .4s var(--ease) backwards}
#emb.out .ebox{animation:qPopOut .2s ease forwards}
.ehd{display:flex;justify-content:space-between;align-items:flex-start;gap:14px;margin-bottom:14px}
.et{min-width:0}.et b,.et span{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.et b{font-size:20px;font-weight:800}.et span{margin-top:3px;font-size:14px;color:var(--sub)}
.etabs{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px}
.efr{border-radius:14px;overflow:hidden;background:#000;box-shadow:inset 0 0 0 1px rgba(255,255,255,.08)}
.efr iframe{display:block;width:100%;border:0;background:#111}
.efr.r{position:relative;height:0;padding-top:56.25%}
.efr.r iframe{position:absolute;inset:0;height:100%}
.efr .none{padding:34px 20px;text-align:center;color:var(--sub);font-size:14px}
.enote{margin:10px 2px 0;font-size:12.5px;color:rgba(255,255,255,.5);min-height:1em}
.epaste{display:flex;gap:8px;margin-top:16px}
.epaste input{flex:1;min-width:0;height:44px;padding:0 16px;border-radius:999px;background:rgba(0,0,0,.45);box-shadow:inset 0 0 0 1px rgba(255,255,255,.1);border:0;outline:0;font-size:14px}
.epaste input:focus{box-shadow:inset 0 0 0 1px rgba(255,255,255,.4)}
.epaste button{padding:0 20px;border-radius:999px;font-weight:800;font-size:14px;background:linear-gradient(180deg,var(--g2),var(--g))}
.eext{margin-top:14px;font-size:12.5px;color:rgba(255,255,255,.45);display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.eext a{padding:5px 12px;border-radius:999px;background:rgba(255,255,255,.09);color:#fff;font-weight:700;text-decoration:none;cursor:var(--cur-p)}
.eext a:hover{background:rgba(255,255,255,.2)}
body.bye #emb{display:none}

.ectl{display:flex;align-items:center;gap:12px;margin-top:12px;padding:10px 16px 10px 10px;border-radius:14px;background:rgba(0,0,0,.4);box-shadow:inset 0 0 0 1px rgba(255,255,255,.06);animation:pgItem .4s var(--ease) backwards}
.ectl[hidden]{display:none}
.ectl .apb{width:42px;height:42px}
.ectl .apb svg{width:18px;height:18px;fill:currentColor}
.ectl i{font-style:normal;font-size:12px;color:var(--sub);font-variant-numeric:tabular-nums;min-width:34px;text-align:center}
#eFail a{color:#fff;font-weight:700}
@media (max-width:700px){.ectl .vol{display:none}}

/* ---------- выбор площадки ---------- */
#plat{position:fixed;inset:0;z-index:15000;display:grid;place-items:center;padding:24px;background:rgba(4,1,2,.62);-webkit-backdrop-filter:blur(8px);backdrop-filter:blur(8px);animation:qFade .25s ease backwards}
#plat.out{animation:qFadeOut .2s ease forwards;pointer-events:none}
#plat.out .qcard{animation:qPopOut .2s ease forwards}
.pcard{width:min(460px,100%)}
.pcard p{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.plgrid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:20px}
.plb{display:flex;align-items:center;gap:10px;padding:12px 14px;border-radius:12px;text-align:left;font-weight:700;font-size:14px;background:rgba(255,255,255,.07);box-shadow:inset 0 0 0 1px rgba(255,255,255,.08);transition:transform .25s var(--ease),background .2s;animation:pgItem .45s var(--ease) calc(var(--i)*35ms) backwards}
.plb:hover{background:rgba(255,255,255,.15);transform:translateY(-2px)}
.plb i{flex:none;width:12px;height:12px;border-radius:50%;background:var(--c);box-shadow:0 0 12px var(--c)}
.plb.main{grid-column:1/-1;background:linear-gradient(90deg,rgba(255,255,255,.14),rgba(255,255,255,.07))}
.plemb{margin-top:14px;font-size:13px;color:var(--sub);text-decoration:underline;text-underline-offset:3px}
.plemb:hover{color:#fff}
body.bye #plat{display:none}
@media (max-width:480px){.plgrid{grid-template-columns:1fr}}




/* ===================================================================== */
/* ---------- карточки выбора дизайна в настройках ---------- */
.dsn{position:relative;text-align:left;padding:0;border-radius:14px;overflow:hidden;background:rgba(255,255,255,.05);box-shadow:inset 0 0 0 1px rgba(255,255,255,.08);transition:transform .25s var(--ease),box-shadow .25s}
.dsn:hover{transform:translateY(-2px)}
.dsn.on{box-shadow:inset 0 0 0 2px var(--g2),0 8px 26px -10px rgba(var(--ar),.7)}
.dsn b{display:block;padding:9px 12px 0;font-size:13.5px;font-weight:800}
.dsn em{display:block;padding:2px 12px 11px;font-style:normal;font-size:11.5px;color:var(--sub);line-height:1.3}
.pv{position:relative;display:block;height:64px;overflow:hidden}
.pv s{position:absolute;display:block;text-decoration:none}
.pv-classic{background:radial-gradient(70% 90% at 30% 10%,hsl(var(--tn) 80% 40%),transparent 70%),radial-gradient(60% 80% at 90% 100%,hsl(calc(var(--tn) - 30) 70% 30%),transparent 70%),#100308}
.pv-classic s:nth-child(1){left:10px;top:10px;width:30%;height:44px;border-radius:6px;background:rgba(0,0,0,.55)}
.pv-classic s:nth-child(2){left:calc(30% + 16px);right:10px;top:10px;height:44px;border-radius:6px;background:rgba(40,10,18,.6)}
.pv-classic s:nth-child(3){left:calc(30% + 26px);top:20px;width:30%;height:6px;border-radius:3px;background:#fff}
.pv-ocean{background:radial-gradient(90% 80% at 50% -10%,#1a6f8f,#06243a 55%,#020c14)}
.pv-ocean::before{content:"";position:absolute;inset:0;background:repeating-linear-gradient(105deg,transparent 0 18px,rgba(150,235,255,.12) 18px 26px,transparent 26px 46px)}
.pv-ocean s:nth-child(1){left:12px;top:12px;width:28%;height:40px;border-radius:9px;background:rgba(3,22,36,.7);box-shadow:inset 0 0 0 1px rgba(140,220,255,.25)}
.pv-ocean s:nth-child(2){left:calc(28% + 20px);right:12px;top:12px;height:40px;border-radius:9px;background:rgba(3,22,36,.6);box-shadow:inset 0 0 0 1px rgba(140,220,255,.2)}
.pv-ocean s:nth-child(3){right:20px;bottom:10px;width:8px;height:8px;border-radius:50%;box-shadow:0 0 0 1px rgba(200,245,255,.8),-30px -14px 0 -1px rgba(200,245,255,.5),-60px -4px 0 -2px rgba(200,245,255,.5)}
.pv-sakura{background:radial-gradient(60% 80% at 85% 10%,#ffd6e6 0 10%,#ff9cc0 18%,rgba(255,120,170,.25) 32%,transparent 45%),linear-gradient(180deg,#4a1640,#1a0716)}
.pv-sakura s:nth-child(1){left:12px;top:12px;width:28%;height:40px;border-radius:10px;background:rgba(36,10,28,.65);box-shadow:inset 0 0 0 1px rgba(255,180,210,.25)}
.pv-sakura s:nth-child(2){left:calc(28% + 20px);width:38%;top:12px;height:40px;border-radius:10px;background:rgba(36,10,28,.6);box-shadow:inset 0 0 0 1px rgba(255,180,210,.2)}
.pv-sakura s:nth-child(3){left:46%;top:8px;width:9px;height:6px;border-radius:50% 0;background:#ffc0d5;transform:rotate(30deg);box-shadow:30px 30px 0 #ff9cc0,60px 6px 0 #ffd6e6,-40px 36px 0 #ff9cc0}
.pv-fireflies{background:radial-gradient(60% 80% at 80% 0%,#2a7a5e,transparent 60%),linear-gradient(180deg,#0c2c21,#020806)}
.pv-fireflies::before{content:"";position:absolute;left:0;right:0;bottom:0;height:26px;background:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='60' height='26'%3E%3Cpath d='M0 26 L8 8 L16 26 Z M14 26 L24 2 L34 26 Z M30 26 L38 12 L46 26 Z M44 26 L52 6 L60 26 Z' fill='%23020a06'/%3E%3C/svg%3E") repeat-x bottom/60px 26px}
.pv-fireflies s:nth-child(1){left:12px;top:12px;width:28%;height:30px;border-radius:9px;background:rgba(4,22,16,.75);box-shadow:inset 0 0 0 1px rgba(140,255,200,.22)}
.pv-fireflies s:nth-child(2){left:calc(28% + 20px);width:38%;top:12px;height:30px;border-radius:9px;background:rgba(4,22,16,.7);box-shadow:inset 0 0 0 1px rgba(140,255,200,.2)}
.pv-fireflies s:nth-child(3){left:56%;top:46px;width:5px;height:5px;border-radius:50%;background:#f4ffb0;box-shadow:0 0 8px 3px rgba(220,255,120,.7),-40px -8px 0 0 #f4ffb0,-40px -8px 8px 3px rgba(220,255,120,.6),30px -20px 0 0 #f4ffb0,30px -20px 8px 3px rgba(220,255,120,.6),-80px 4px 0 0 #f4ffb0,-80px 4px 8px 3px rgba(220,255,120,.5)}
.pv-lux{background:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='56' height='56'%3E%3Cg fill='none' stroke='%23d4a537' stroke-width='1'%3E%3Cpath d='M0 56 A28 28 0 0 1 56 56 M8 56 A20 20 0 0 1 48 56 M16 56 A12 12 0 0 1 40 56'/%3E%3Cpath d='M28 28 A28 28 0 0 1 84 28 M36 28 A20 20 0 0 1 76 28 M44 28 A12 12 0 0 1 68 28'/%3E%3Cpath d='M-28 28 A28 28 0 0 1 28 28 M-20 28 A20 20 0 0 1 20 28 M-12 28 A12 12 0 0 1 12 28'/%3E%3C/g%3E%3C/svg%3E") 0 0/28px 28px,radial-gradient(80% 90% at 50% 0%,#3a2c16,#0b0906 70%);background-blend-mode:soft-light,normal}
.pv-lux s:nth-child(1){left:12px;top:12px;width:28%;height:40px;border-radius:3px;background:rgba(10,8,5,.85);box-shadow:inset 0 0 0 1px rgba(233,196,106,.5),inset 0 0 0 3px rgba(0,0,0,.4),inset 0 0 0 4px rgba(233,196,106,.25)}
.pv-lux s:nth-child(2){left:calc(28% + 20px);right:12px;top:12px;height:40px;border-radius:3px;background:rgba(10,8,5,.8);box-shadow:inset 0 0 0 1px rgba(233,196,106,.45)}
.pv-lux s:nth-child(3){left:calc(28% + 30px);top:22px;width:34%;height:7px;border-radius:2px;background:linear-gradient(90deg,#b8862b,#fff3c4,#d4a537);box-shadow:0 12px 0 -1px rgba(233,196,106,.45)}
.pv-nebula{background:radial-gradient(40% 60% at 30% 50%,rgba(124,58,237,.8),transparent 70%),radial-gradient(35% 55% at 70% 35%,rgba(34,211,238,.55),transparent 70%),radial-gradient(30% 40% at 55% 85%,rgba(219,39,119,.45),transparent 70%),#05040f}
.pv-nebula::before{content:"";position:absolute;inset:0;background:radial-gradient(1px 1px at 12% 20%,#fff,transparent),radial-gradient(1px 1px at 40% 70%,#fff,transparent),radial-gradient(1.4px 1.4px at 78% 18%,#fff,transparent),radial-gradient(1px 1px at 90% 80%,#fff,transparent),radial-gradient(1px 1px at 60% 10%,#fff,transparent)}
.pv-nebula s:nth-child(1){left:12px;top:12px;width:28%;height:40px;border-radius:11px;background:rgba(10,7,28,.62);box-shadow:inset 0 0 0 1px rgba(167,139,250,.3)}
.pv-nebula s:nth-child(2){left:calc(28% + 20px);width:38%;top:12px;height:40px;border-radius:11px;background:rgba(10,7,28,.55);box-shadow:inset 0 0 0 1px rgba(167,139,250,.25)}
.pv-nebula s:nth-child(3){right:18px;top:16px;width:46px;height:2px;border-radius:2px;background:linear-gradient(90deg,transparent,#fff);transform:rotate(30deg);box-shadow:0 0 6px #c4b5fd}
.pv-ambient{background:linear-gradient(135deg,#ff6a3d,#c2185b 40%,#4a148c 75%,#1a237e);filter:saturate(1.2)}
.pv-ambient::before{content:"";position:absolute;inset:0;background:rgba(0,0,0,.35);backdrop-filter:blur(6px)}
.pv-ambient s:nth-child(1){left:10px;top:10px;width:44px;height:44px;border-radius:8px;background:linear-gradient(135deg,#ff6a3d,#c2185b);box-shadow:0 6px 16px rgba(0,0,0,.5);z-index:1}
.pv-ambient s:nth-child(2){left:64px;top:16px;width:46%;height:8px;border-radius:4px;background:#fff;z-index:1}
.pv-ambient s:nth-child(3){left:64px;top:34px;width:60px;height:16px;border-radius:8px;background:#fff;opacity:.9;z-index:1}
.pv-glass{background:radial-gradient(60% 90% at 15% 20%,hsl(var(--tn) 90% 50% / .8),transparent 70%),radial-gradient(60% 90% at 90% 80%,hsl(calc(var(--tn) - 60) 90% 55% / .8),transparent 70%),#160a1c}
.pv-glass s:nth-child(1){left:10px;top:9px;width:28%;height:46px;border-radius:14px;background:rgba(255,255,255,.16);box-shadow:inset 0 0 0 1px rgba(255,255,255,.35)}
.pv-glass s:nth-child(2){left:calc(28% + 18px);right:10px;top:9px;height:46px;border-radius:14px;background:rgba(255,255,255,.12);box-shadow:inset 0 0 0 1px rgba(255,255,255,.3)}
.pv-glass s:nth-child(3){left:calc(28% + 30px);top:22px;width:34%;height:7px;border-radius:7px;background:#fff}

/* ===================================================================== */
/* ---------- ДИЗАЙН «СТЕКЛО» ---------- */
html[data-design=glass] body{background:#0b0610}
html[data-design=glass] body::before{content:"";position:fixed;inset:0;z-index:0;pointer-events:none;background:
  radial-gradient(55% 50% at 12% 18%,hsl(var(--tn) 90% 45% / .6),transparent 70%),
  radial-gradient(45% 45% at 88% 12%,hsl(calc(var(--tn) + 45) 90% 55% / .42),transparent 70%),
  radial-gradient(65% 60% at 72% 95%,hsl(calc(var(--tn) - 60) 85% 45% / .5),transparent 70%),
  radial-gradient(40% 40% at 35% 75%,hsl(calc(var(--tn) + 20) 80% 50% / .3),transparent 70%)}
html[data-design=glass] .app{padding:16px;gap:16px;grid-template-columns:clamp(280px,24vw,370px) minmax(0,1fr)}
html[data-design=glass] .side{gap:16px}
html[data-design=glass] .side .panel,html[data-design=glass] .main,html[data-design=glass] .pm{background:rgba(255,255,255,.075);-webkit-backdrop-filter:blur(26px) saturate(1.5);backdrop-filter:blur(26px) saturate(1.5);border-radius:28px;box-shadow:inset 0 0 0 1px rgba(255,255,255,.16),inset 0 1px 0 rgba(255,255,255,.22),0 30px 70px -30px rgba(0,0,0,.65)}
html[data-design=glass] .hero-bg,html[data-design=glass] .pm-bg{opacity:.3}
html[data-design=glass] .main,html[data-design=glass] .pm{contain:none}
html[data-design=glass] .brand span{font-weight:800;letter-spacing:-.01em}
html[data-design=glass] .navbtn{border-radius:14px;margin:1px 0;transition:background .25s,color .2s}
html[data-design=glass] .navbtn:hover,html[data-design=glass] .navbtn.on{background:rgba(255,255,255,.12)}
html[data-design=glass] .item{border-radius:16px}
html[data-design=glass] .item.on{background:rgba(255,255,255,.16)}
html[data-design=glass] .cv{border-radius:12px}
html[data-design=glass] .plate,html[data-design=glass] .cat,html[data-design=glass] .chip{border-radius:999px}
html[data-design=glass] .plate{border-radius:18px;background:rgba(255,255,255,.08)}
html[data-design=glass] .sbox input{border-radius:999px;background:rgba(255,255,255,.1)}
html[data-design=glass] .cover{border-radius:26px;box-shadow:0 20px 60px -15px rgba(0,0,0,.7),inset 0 0 0 1px rgba(255,255,255,.2)}
html[data-design=glass] .big,html[data-design=glass] .phs.pt{font-weight:800;letter-spacing:-.04em}
html[data-design=glass] .rate{border-radius:22px;letter-spacing:.08em;text-transform:none;font-size:18px;box-shadow:0 14px 34px -10px rgba(var(--ar),.7),inset 0 1px 0 rgba(255,255,255,.35)}
html[data-design=glass] .rbg{background:linear-gradient(135deg,var(--g3),var(--g2) 50%,var(--g0))}
html[data-design=glass] .rcrit{border-radius:16px;background:rgba(255,255,255,.1)}
html[data-design=glass] .drop{border-radius:22px;border-style:solid;border-color:rgba(255,255,255,.18);background:rgba(255,255,255,.06)}
html[data-design=glass] .ap{border-radius:24px;background:rgba(255,255,255,.08);box-shadow:inset 0 0 0 1px rgba(255,255,255,.14)}
html[data-design=glass] .row{border-radius:18px}
html[data-design=glass] .rail{height:8px;border-radius:8px;background:rgba(255,255,255,.14)}
html[data-design=glass] .fill{border-radius:8px;background:linear-gradient(90deg,rgba(255,255,255,.75),#fff)}
html[data-design=glass] .thumb{width:18px;height:18px;margin:-9px 0 0 -9px;transform:scale(1);box-shadow:0 2px 10px rgba(0,0,0,.45),0 0 0 4px rgba(255,255,255,.18)}
html[data-design=glass] .thead{border-bottom-color:rgba(255,255,255,.14)}
html[data-design=glass] .score{border-radius:28px;background:rgba(255,255,255,.09);box-shadow:inset 0 0 0 1px rgba(255,255,255,.18),inset 0 1px 0 rgba(255,255,255,.25)}
html[data-design=glass] .tnb,html[data-design=glass] .fl a,html[data-design=glass] .flb{border-radius:999px}
html[data-design=glass] .pcrd,html[data-design=glass] .rkr,html[data-design=glass] .ctcard,html[data-design=glass] .ctr,html[data-design=glass] .wl-card{border-radius:22px}
html[data-design=glass] #mp{border-radius:999px;padding-left:9px;background:rgba(40,25,45,.55);-webkit-backdrop-filter:blur(24px) saturate(1.5);backdrop-filter:blur(24px) saturate(1.5);box-shadow:inset 0 0 0 1px rgba(255,255,255,.2),0 20px 50px -14px rgba(0,0,0,.8)}
html[data-design=glass] #mp .mpcv{border-radius:50%}
html[data-design=glass] .stp{background:rgba(30,18,34,.72);-webkit-backdrop-filter:blur(30px);backdrop-filter:blur(30px)}

/* ===================================================================== */
/* ---------- ДИЗАЙН «АМБИЕНТ»: фон из обложки ---------- */
html[data-design=ambient]{--sub:rgba(255,255,255,.66);--line:rgba(255,255,255,.12)}
html[data-design=ambient] body{background:#08080b}
html[data-design=ambient] body::before{content:"";position:fixed;inset:-12%;z-index:0;pointer-events:none;background:var(--amb,linear-gradient(135deg,#3a1030,#101030)) center/cover no-repeat;filter:blur(80px) saturate(1.7) brightness(.62);transform:translateZ(0)}
html[data-design=ambient] body::after{content:"";position:fixed;inset:0;z-index:0;pointer-events:none;background:radial-gradient(120% 90% at 50% 0%,transparent 30%,rgba(0,0,0,.55) 100%),linear-gradient(180deg,rgba(0,0,0,.1),rgba(0,0,0,.45))}
html[data-design=ambient] .app{padding:12px;gap:12px}
html[data-design=ambient] .side{gap:12px}
html[data-design=ambient] .side .panel,html[data-design=ambient] .main,html[data-design=ambient] .pm{background:rgba(0,0,0,.26);border-radius:22px;box-shadow:inset 0 0 0 1px rgba(255,255,255,.08)}
html[data-design=ambient] .hero-bg,html[data-design=ambient] .pm-bg{display:none}
html[data-design=ambient] .brand span{background:none;color:#fff;-webkit-text-fill-color:#fff;animation:none;font-weight:800;letter-spacing:-.02em}
html[data-design=ambient] .navbtn{border-radius:12px;color:rgba(255,255,255,.72)}
html[data-design=ambient] .navbtn:hover,html[data-design=ambient] .navbtn.on{background:rgba(255,255,255,.12);color:#fff}
html[data-design=ambient] .plate{border-radius:16px;background:rgba(255,255,255,.08);box-shadow:none}
html[data-design=ambient] .plate:hover,html[data-design=ambient] .plate.on,html[data-design=ambient] .cat.on{background:#fff;color:#111;box-shadow:none}
html[data-design=ambient] .plate:hover em,html[data-design=ambient] .plate.on em{color:rgba(0,0,0,.6)}
html[data-design=ambient] .plate:hover .pi,html[data-design=ambient] .plate.on .pi{background:rgba(0,0,0,.08)}
html[data-design=ambient] .cat,html[data-design=ambient] .chip{background:rgba(255,255,255,.1);box-shadow:none}
html[data-design=ambient] .sbox input{border-radius:12px;background:rgba(255,255,255,.1)}
html[data-design=ambient] .item{border-radius:12px}
html[data-design=ambient] .item.on{background:rgba(255,255,255,.16)}
html[data-design=ambient] .item.on .tx b{color:#fff}
html[data-design=ambient] .cv{border-radius:8px}
html[data-design=ambient] .sc{text-shadow:none}
html[data-design=ambient] .hero{padding-top:70px}
html[data-design=ambient] .cover{border-radius:14px;box-shadow:0 30px 70px -20px rgba(0,0,0,.85)}
html[data-design=ambient] .cover::after{display:none}
html[data-design=ambient] .eyebrow{color:rgba(255,255,255,.75)}
html[data-design=ambient] .big,html[data-design=ambient] .phs.pt{font-weight:800;letter-spacing:-.035em}
html[data-design=ambient] .rate{color:#111;border-radius:999px;letter-spacing:.06em;text-transform:none;font-size:18px;box-shadow:0 14px 34px -12px rgba(0,0,0,.7)}
html[data-design=ambient] .rbg{background:#fff}
html[data-design=ambient] .rbg::after{background:linear-gradient(90deg,transparent,rgba(0,0,0,.12),transparent)}
html[data-design=ambient] .rate:hover{box-shadow:0 18px 40px -12px rgba(0,0,0,.8)}
html[data-design=ambient] .rcrit{border-radius:999px;background:rgba(255,255,255,.12);box-shadow:none;color:#fff}
html[data-design=ambient] .drop{border-radius:18px;border:0;background:rgba(255,255,255,.08)}
html[data-design=ambient] .upbtn,html[data-design=ambient] .flb,html[data-design=ambient] .tnb.nx,html[data-design=ambient] .gbtn{background:#fff;color:#111;box-shadow:none}
html[data-design=ambient] .upbtn:hover,html[data-design=ambient] .tnb.nx:hover:not(:disabled){background:#f0f0f0}
html[data-design=ambient] .tnb{border-radius:999px}
html[data-design=ambient] .ap{border-radius:20px;background:rgba(0,0,0,.25);box-shadow:inset 0 0 0 1px rgba(255,255,255,.1)}
html[data-design=ambient] .apb:not(.swp){background:#fff;color:#111;box-shadow:0 6px 18px rgba(0,0,0,.4)}
html[data-design=ambient] .apb:not(.swp):hover{background:#fff}
html[data-design=ambient] .row:hover{background:rgba(255,255,255,.07)}
html[data-design=ambient] .rail{height:5px;border-radius:5px;background:rgba(255,255,255,.2)}
html[data-design=ambient] .fill{border-radius:5px}
html[data-design=ambient] .thead{border-bottom-color:rgba(255,255,255,.12)}
html[data-design=ambient] body.intm .row .val b,html[data-design=ambient] .val b{color:#fff}
html[data-design=ambient] .score{border-radius:22px;background:rgba(255,255,255,.08);box-shadow:inset 0 0 0 1px rgba(255,255,255,.1)}
html[data-design=ambient] .ringw::before{display:none}
html[data-design=ambient] .pcrd,html[data-design=ambient] .rkr,html[data-design=ambient] .ctcard,html[data-design=ambient] .ctr,html[data-design=ambient] .wl-card{border-radius:18px}
html[data-design=ambient] .psb.on,html[data-design=ambient] .rkc.on{background:#fff;color:#111}
html[data-design=ambient] #mp{border-radius:20px;background:rgba(18,18,22,.62);-webkit-backdrop-filter:blur(30px) saturate(1.6);backdrop-filter:blur(30px) saturate(1.6);box-shadow:inset 0 0 0 1px rgba(255,255,255,.12),0 20px 50px -14px rgba(0,0,0,.85)}
html[data-design=ambient] #mp .mpcv{border-radius:8px}
html[data-design=ambient] .stp{background:rgba(16,16,20,.82);-webkit-backdrop-filter:blur(30px);backdrop-filter:blur(30px)}

/* ===================================================================== */
/* ---------- ДИЗАЙН «ЛЮКС»: чёрный бархат и золото ---------- */
html[data-design=lux]{--g:#d4a537!important;--g0:#a8801e!important;--g2:#e9c46a!important;--g3:#f6e27a!important;--g4:#fff3c4!important;--ar:212,165,55!important;--tn:42!important;--sub:#bfb39a;--line:rgba(233,196,106,.16)}
html[data-design=lux] body{background:radial-gradient(90% 60% at 50% -14%,#33281a 0%,#16120c 42%,#080706 78%,#040303 100%)}
html[data-design=lux] .app{padding:12px;gap:12px}
html[data-design=lux] .side{gap:12px}
html[data-design=lux] .side .panel,html[data-design=lux] .main,html[data-design=lux] .pm{background:rgba(14,11,8,.74);border-radius:6px;box-shadow:inset 0 0 0 1px rgba(233,196,106,.2),inset 0 0 0 5px rgba(0,0,0,.18),inset 0 0 0 6px rgba(233,196,106,.07),0 24px 60px -30px rgba(0,0,0,.95)}
html[data-design=lux] .hero-bg,html[data-design=lux] .pm-bg{background:linear-gradient(180deg,rgba(212,165,55,.2) 0%,rgba(120,90,30,.1) 60%,transparent 100%)}
html[data-design=lux] .brand span{background:linear-gradient(100deg,#b8862b 0%,#f6e27a 22%,#fffbe6 32%,#d4a537 46%,#8a6a1f 60%,#f6e27a 80%,#b8862b 100%);background-size:400px 100%;-webkit-background-clip:text;background-clip:text}
html[data-design=lux] .wl-brand{background:linear-gradient(100deg,#b8862b 0%,#f6e27a 22%,#fffbe6 32%,#d4a537 46%,#8a6a1f 60%,#f6e27a 80%,#b8862b 100%);background-size:600px 100%;-webkit-background-clip:text;background-clip:text}
html[data-design=lux] .item.on{background:rgba(233,196,106,.12)}
html[data-design=lux] .cover{box-shadow:0 0 0 1px rgba(233,196,106,.2),0 24px 60px -15px rgba(0,0,0,.95),0 0 70px -24px rgba(233,196,106,.55)}
html[data-design=lux] .big{text-shadow:0 6px 34px rgba(233,196,106,.32)}
html[data-design=lux] .rbg{background:linear-gradient(110deg,#a8801e,#e9c46a 40%,#fff3c4 55%,#e9c46a 70%,#b8862b);background-size:200% 100%}
html[data-design=lux] .score{background:radial-gradient(130% 90% at 50% 0%,rgba(212,165,55,.2) 0%,rgba(10,8,5,.88) 72%);box-shadow:inset 0 0 0 1px rgba(233,196,106,.2)}
html[data-design=lux] .ringw::before{background:radial-gradient(circle,rgba(233,196,106,.3) 0%,transparent 66%)}
html[data-design=lux] .pcrd,html[data-design=lux] .rkr,html[data-design=lux] .ctcard,html[data-design=lux] .ctr{box-shadow:inset 0 0 0 1px rgba(233,196,106,.2)}
html[data-design=lux] #mp{background:rgba(10,8,14,.94);box-shadow:inset 0 0 0 1px rgba(233,196,106,.2),0 18px 50px -12px rgba(0,0,0,.9),0 0 46px -18px rgba(233,196,106,.55)}
html[data-design=lux] body::before{content:"";position:fixed;inset:0;z-index:0;pointer-events:none;background:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='56' height='56'%3E%3Cg fill='none' stroke='%23d4a537' stroke-width='1'%3E%3Cpath d='M0 56 A28 28 0 0 1 56 56 M8 56 A20 20 0 0 1 48 56 M16 56 A12 12 0 0 1 40 56'/%3E%3Cpath d='M28 28 A28 28 0 0 1 84 28 M36 28 A20 20 0 0 1 76 28 M44 28 A12 12 0 0 1 68 28'/%3E%3Cpath d='M-28 28 A28 28 0 0 1 28 28 M-20 28 A20 20 0 0 1 20 28 M-12 28 A12 12 0 0 1 12 28'/%3E%3C/g%3E%3C/svg%3E") 0 0/56px 56px;opacity:.06;-webkit-mask:radial-gradient(90% 80% at 50% 30%,#000,transparent 85%);mask:radial-gradient(90% 80% at 50% 30%,#000,transparent 85%)}
html[data-design=lux] .side .panel,html[data-design=lux] .main,html[data-design=lux] .pm{border-radius:6px}
html[data-design=lux] .big,html[data-design=lux] .phead h2{font-family:Georgia,"Times New Roman",serif;font-weight:700;letter-spacing:.01em}
html[data-design=lux] .eyebrow{color:#e9c46a;letter-spacing:.32em}
html[data-design=lux] .navbtn:hover,html[data-design=lux] .navbtn.on{color:#fff3c4}
html[data-design=lux] .navbtn.on{box-shadow:inset 2px 0 0 #d4a537}
html[data-design=lux] .cover{border-radius:4px}
html[data-design=lux] .rate{border-radius:4px;color:#1a1408;box-shadow:0 10px 30px -8px rgba(212,165,55,.55)}
html[data-design=lux] .rate::before{content:"";position:absolute;inset:3px;border-radius:2px;box-shadow:inset 0 0 0 1px rgba(26,20,8,.35);pointer-events:none}
html[data-design=lux] .ap{border-radius:6px;background:rgba(8,6,4,.6);box-shadow:inset 0 0 0 1px rgba(233,196,106,.16)}
html[data-design=lux] .score{border-radius:6px}
html[data-design=lux] #mp{border-radius:6px}
html[data-design=lux] .upbtn,html[data-design=lux] .flb,html[data-design=lux] .tnb.nx{color:#1a1408!important;text-shadow:none}
html[data-design=lux] .upbtn .ico,html[data-design=lux] .flb .ico,html[data-design=lux] .tnb.nx .ico{stroke:#1a1408}
html[data-design=lux] .stp{background:linear-gradient(180deg,rgba(40,32,20,.98),rgba(8,7,5,.98))}
/* ---------- ДИЗАЙН «ТУМАННОСТЬ»: глубокий космос ---------- */
html[data-design=nebula]{--g:#8b5cf6!important;--g0:#6d28d9!important;--g2:#a78bfa!important;--g3:#c4b5fd!important;--g4:#e9d5ff!important;--ar:167,139,250!important;--tn:262!important;--sub:#b4acd6;--line:rgba(167,139,250,.16)}
html[data-design=nebula] body{background:radial-gradient(60% 50% at 78% 18%,rgba(34,211,238,.16),transparent 70%),radial-gradient(70% 60% at 18% 70%,rgba(124,58,237,.24),transparent 70%),radial-gradient(50% 40% at 60% 90%,rgba(219,39,119,.12),transparent 70%),linear-gradient(180deg,#07051a,#05040f 60%,#020208)}
html[data-design=nebula] .app{padding:12px;gap:12px}
html[data-design=nebula] .side{gap:12px}
html[data-design=nebula] .side .panel,html[data-design=nebula] .main,html[data-design=nebula] .pm{background:rgba(10,7,28,.56);border-radius:20px;box-shadow:inset 0 0 0 1px rgba(167,139,250,.16),0 24px 60px -30px rgba(0,0,0,.95)}
html[data-design=nebula] .hero-bg,html[data-design=nebula] .pm-bg{background:linear-gradient(180deg,rgba(139,92,246,.26) 0%,rgba(34,211,238,.1) 60%,transparent 100%)}
html[data-design=nebula] .brand span{background:linear-gradient(100deg,#a78bfa 0%,#e9d5ff 22%,#ffffff 32%,#67e8f9 48%,#c084fc 66%,#f472b6 82%,#a78bfa 100%);background-size:400px 100%;-webkit-background-clip:text;background-clip:text}
html[data-design=nebula] .wl-brand{background:linear-gradient(100deg,#a78bfa 0%,#e9d5ff 22%,#ffffff 32%,#67e8f9 48%,#c084fc 66%,#f472b6 82%,#a78bfa 100%);background-size:600px 100%;-webkit-background-clip:text;background-clip:text}
html[data-design=nebula] .item.on{background:rgba(139,92,246,.12)}
html[data-design=nebula] .cover{box-shadow:0 0 0 1px rgba(167,139,250,.2),0 24px 60px -15px rgba(0,0,0,.95),0 0 70px -24px rgba(139,92,246,.55)}
html[data-design=nebula] .big{text-shadow:0 6px 34px rgba(139,92,246,.32)}
html[data-design=nebula] .rbg{background:linear-gradient(110deg,#6d28d9,#8b5cf6 35%,#22d3ee 65%,#a78bfa);background-size:200% 100%}
html[data-design=nebula] .score{background:radial-gradient(130% 90% at 50% 0%,rgba(139,92,246,.28) 0%,rgba(6,4,20,.86) 72%);box-shadow:inset 0 0 0 1px rgba(167,139,250,.2)}
html[data-design=nebula] .ringw::before{background:radial-gradient(circle,rgba(139,92,246,.3) 0%,transparent 66%)}
html[data-design=nebula] .pcrd,html[data-design=nebula] .rkr,html[data-design=nebula] .ctcard,html[data-design=nebula] .ctr{box-shadow:inset 0 0 0 1px rgba(167,139,250,.2)}
html[data-design=nebula] #mp{background:rgba(10,8,14,.94);box-shadow:inset 0 0 0 1px rgba(167,139,250,.2),0 18px 50px -12px rgba(0,0,0,.9),0 0 46px -18px rgba(139,92,246,.55)}
html[data-design=nebula] .side .panel,html[data-design=nebula] .main,html[data-design=nebula] .pm{border-radius:20px}
html[data-design=nebula] .navbtn:hover,html[data-design=nebula] .navbtn.on{color:#e9d5ff}
html[data-design=nebula] .cover{border-radius:18px}
html[data-design=nebula] .rate{border-radius:999px;box-shadow:0 10px 34px -8px rgba(139,92,246,.7),0 0 40px -14px rgba(34,211,238,.6)}
html[data-design=nebula] .ap{border-radius:18px;background:rgba(8,6,24,.55);box-shadow:inset 0 0 0 1px rgba(167,139,250,.16)}
html[data-design=nebula] .score{border-radius:20px}
html[data-design=nebula] #mp{border-radius:20px}
html[data-design=nebula] .stp{background:linear-gradient(180deg,rgba(24,16,60,.98),rgba(5,4,15,.98))}
/* ---------- ДИЗАЙН «СВЕТЛЯЧКИ»: ночной лес ---------- */
html[data-design=fireflies]{--g:#1fbf86!important;--g0:#0e8a5c!important;--g2:#3fe0a0!important;--g3:#7af0c0!important;--g4:#c2ffe4!important;--ar:63,224,160!important;--tn:155!important;--sub:#a3c7b8;--line:rgba(140,255,200,.14)}
html[data-design=fireflies] body{background:radial-gradient(80% 60% at 72% -12%,#1e5c47 0%,#0c2c21 38%,#05140e 72%,#020806 100%)}
html[data-design=fireflies] body::before{content:"";position:fixed;right:9vw;top:6vh;width:18vmin;height:18vmin;z-index:0;pointer-events:none;border-radius:50%;
  background:radial-gradient(circle,#fbffe8 0%,#e9ffd0 34%,rgba(200,255,170,.3) 50%,rgba(160,255,150,.1) 62%,transparent 72%);opacity:.55}
html[data-design=fireflies] .app{padding:12px;gap:12px}
html[data-design=fireflies] .side{gap:12px}
html[data-design=fireflies] .side .panel,html[data-design=fireflies] .main,html[data-design=fireflies] .pm{background:rgba(4,22,16,.6);border-radius:18px;box-shadow:inset 0 0 0 1px rgba(140,255,200,.12),0 24px 60px -30px rgba(0,0,0,.9)}
html[data-design=fireflies] .hero-bg,html[data-design=fireflies] .pm-bg{background:linear-gradient(180deg,rgba(63,224,160,.24) 0%,rgba(20,90,60,.16) 60%,transparent 100%)}
html[data-design=fireflies] .brand span{background:linear-gradient(100deg,#7af0c0 0%,#d9ffe9 25%,#fffbe0 40%,#b8ff9a 60%,#3fe0a0 80%,#7af0c0 100%);background-size:400px 100%;-webkit-background-clip:text;background-clip:text}
html[data-design=fireflies] .wl-brand{background:linear-gradient(100deg,#7af0c0 0%,#fffbe0 40%,#3fe0a0 80%);background-size:600px 100%;-webkit-background-clip:text;background-clip:text}
html[data-design=fireflies] .navbtn:hover,html[data-design=fireflies] .navbtn.on{color:#d9ffe9}
html[data-design=fireflies] .item.on{background:rgba(120,255,190,.1)}
html[data-design=fireflies] .cover{border-radius:16px;box-shadow:0 0 0 1px rgba(140,255,200,.22),0 24px 60px -15px rgba(0,20,10,.95),0 0 60px -20px rgba(220,255,140,.35)}
html[data-design=fireflies] .big{text-shadow:0 6px 34px rgba(63,224,160,.3)}
html[data-design=fireflies] .rate{border-radius:16px}
html[data-design=fireflies] .rbg{background:linear-gradient(110deg,#0e8a5c,#3fe0a0 45%,#d9ff9a 72%,#3fe0a0);background-size:200% 100%}
html[data-design=fireflies] .ap{border-radius:16px;background:rgba(2,16,11,.55);box-shadow:inset 0 0 0 1px rgba(140,255,200,.14)}
html[data-design=fireflies] .score{border-radius:18px;background:radial-gradient(130% 90% at 50% 0%,rgba(63,224,160,.26) 0%,rgba(3,20,14,.85) 72%);box-shadow:inset 0 0 0 1px rgba(140,255,200,.14)}
html[data-design=fireflies] .ringw::before{background:radial-gradient(circle,rgba(200,255,140,.28) 0%,transparent 66%)}
html[data-design=fireflies] .pcrd,html[data-design=fireflies] .rkr,html[data-design=fireflies] .ctcard,html[data-design=fireflies] .ctr{box-shadow:inset 0 0 0 1px rgba(140,255,200,.12)}
html[data-design=fireflies] #mp{border-radius:18px;background:rgba(4,22,16,.93);box-shadow:inset 0 0 0 1px rgba(140,255,200,.22),0 18px 50px -12px rgba(0,0,0,.9),0 0 46px -18px rgba(200,255,140,.5)}
html[data-design=fireflies] .stp{background:linear-gradient(180deg,rgba(12,44,33,.98),rgba(2,10,7,.98))}

/* ===================================================================== */
/* ---------- ДИЗАЙН «ОКЕАН» ---------- */
#dfx{position:fixed;inset:0;width:100%;height:100%;z-index:0;pointer-events:none;display:none;transform:translateZ(0)}
html[data-design=ocean] #dfx,html[data-design=sakura] #dfx,html[data-design=fireflies] #dfx,html[data-design=lux] #dfx,html[data-design=nebula] #dfx{display:block}
html[data-design=ocean]{--g:#1aa7d6!important;--g0:#0b7fae!important;--g2:#2cc6f0!important;--g3:#5fd8f7!important;--g4:#9be9fb!important;--ar:44,198,240!important;--tn:200!important;--sub:#9fbccb;--line:rgba(140,220,255,.14)}
html[data-design=ocean] body{background:radial-gradient(90% 65% at 50% -12%,#14607e 0%,#073049 35%,#03141f 70%,#01070d 100%)}
html[data-design=ocean] .app{padding:12px;gap:12px}
html[data-design=ocean] .side{gap:12px}
html[data-design=ocean] .side .panel,html[data-design=ocean] .main,html[data-design=ocean] .pm{background:rgba(3,22,36,.6);border-radius:18px;box-shadow:inset 0 0 0 1px rgba(140,220,255,.13),0 24px 60px -30px rgba(0,0,0,.9)}
html[data-design=ocean] .hero-bg,html[data-design=ocean] .pm-bg{background:linear-gradient(180deg,rgba(44,198,240,.26) 0%,rgba(10,70,100,.18) 60%,transparent 100%)}
html[data-design=ocean] .brand span{background:linear-gradient(100deg,#5fd8f7 0%,#bff4ff 25%,#ffffff 40%,#7fe3ff 60%,#2cc6f0 80%,#5fd8f7 100%);background-size:400px 100%;-webkit-background-clip:text;background-clip:text}
html[data-design=ocean] .wl-brand{background:linear-gradient(100deg,#5fd8f7 0%,#ffffff 40%,#2cc6f0 80%);background-size:600px 100%;-webkit-background-clip:text;background-clip:text}
html[data-design=ocean] .navbtn:hover,html[data-design=ocean] .navbtn.on{color:#c9f5ff}
html[data-design=ocean] .item.on{background:rgba(120,220,255,.12)}
html[data-design=ocean] .cover{border-radius:16px;box-shadow:0 0 0 1px rgba(140,220,255,.25),0 24px 60px -15px rgba(0,25,50,.95)}
html[data-design=ocean] .big{text-shadow:0 6px 34px rgba(44,198,240,.3)}
html[data-design=ocean] .rate{border-radius:16px}
html[data-design=ocean] .rbg{background:linear-gradient(110deg,#0b7fae,#2cc6f0 45%,#8eeaff 72%,#2cc6f0);background-size:200% 100%}
html[data-design=ocean] .ap{border-radius:16px;background:rgba(2,16,28,.55);box-shadow:inset 0 0 0 1px rgba(140,220,255,.14)}
html[data-design=ocean] .score{border-radius:18px;background:radial-gradient(130% 90% at 50% 0%,rgba(44,198,240,.28) 0%,rgba(3,20,32,.85) 72%);box-shadow:inset 0 0 0 1px rgba(140,220,255,.14)}
html[data-design=ocean] .ringw::before{background:radial-gradient(circle,rgba(44,198,240,.32) 0%,transparent 66%)}
html[data-design=ocean] .pcrd,html[data-design=ocean] .rkr,html[data-design=ocean] .ctcard,html[data-design=ocean] .ctr{box-shadow:inset 0 0 0 1px rgba(140,220,255,.12)}
html[data-design=ocean] #mp{border-radius:18px;background:rgba(3,22,36,.92);box-shadow:inset 0 0 0 1px rgba(140,220,255,.22),0 18px 50px -12px rgba(0,0,0,.9),0 0 46px -18px rgba(44,198,240,.6)}
html[data-design=ocean] .stp{background:linear-gradient(180deg,rgba(7,48,73,.98),rgba(2,12,20,.98))}

/* ===================================================================== */
/* ---------- ДИЗАЙН «САКУРА» ---------- */
html[data-design=sakura]{--g:#e8558c!important;--g0:#c23a6e!important;--g2:#ff6f9f!important;--g3:#ff92b6!important;--g4:#ffc0d5!important;--ar:255,111,159!important;--tn:335!important;--sub:#d6b6c6;--line:rgba(255,180,210,.14)}
html[data-design=sakura] body{background:radial-gradient(70% 55% at 85% 0%,#5e1f48 0%,transparent 62%),radial-gradient(60% 60% at 0% 100%,#3c1342 0%,transparent 62%),linear-gradient(180deg,#27091f,#13050f 60%,#0a0309)}
html[data-design=sakura] body::before{content:"";position:fixed;right:7vw;top:7vh;width:26vmin;height:26vmin;z-index:0;pointer-events:none;border-radius:50%;background:radial-gradient(circle,#fff1f6 0%,#ffd3e3 30%,rgba(255,160,200,.35) 52%,rgba(255,120,170,.12) 64%,transparent 72%);opacity:.6;filter:blur(1px)}
html[data-design=sakura] .app{padding:12px;gap:12px}
html[data-design=sakura] .side{gap:12px}
html[data-design=sakura] .side .panel,html[data-design=sakura] .main,html[data-design=sakura] .pm{background:rgba(38,10,30,.58);border-radius:20px;box-shadow:inset 0 0 0 1px rgba(255,180,210,.14),0 24px 60px -30px rgba(0,0,0,.9)}
html[data-design=sakura] .hero-bg,html[data-design=sakura] .pm-bg{background:linear-gradient(180deg,rgba(255,111,159,.28) 0%,rgba(120,30,80,.18) 60%,transparent 100%)}
html[data-design=sakura] .brand span{background:linear-gradient(100deg,#ff92b6 0%,#ffd6e6 25%,#ffffff 40%,#ffc0d5 60%,#ff6f9f 80%,#ff92b6 100%);background-size:400px 100%;-webkit-background-clip:text;background-clip:text}
html[data-design=sakura] .wl-brand{background:linear-gradient(100deg,#ff92b6 0%,#ffffff 40%,#ff6f9f 80%);background-size:600px 100%;-webkit-background-clip:text;background-clip:text}
html[data-design=sakura] .navbtn:hover,html[data-design=sakura] .navbtn.on{color:#ffe0ec}
html[data-design=sakura] .item.on{background:rgba(255,160,200,.12)}
html[data-design=sakura] .cover{border-radius:18px;box-shadow:0 0 0 1px rgba(255,180,210,.25),0 24px 60px -15px rgba(40,0,20,.95)}
html[data-design=sakura] .big{text-shadow:0 6px 34px rgba(255,111,159,.3)}
html[data-design=sakura] .rate{border-radius:18px}
html[data-design=sakura] .rbg{background:linear-gradient(110deg,#c23a6e,#ff6f9f 45%,#ffc0d5 72%,#ff6f9f);background-size:200% 100%}
html[data-design=sakura] .ap{border-radius:18px;background:rgba(26,6,20,.55);box-shadow:inset 0 0 0 1px rgba(255,180,210,.14)}
html[data-design=sakura] .score{border-radius:20px;background:radial-gradient(130% 90% at 50% 0%,rgba(255,111,159,.28) 0%,rgba(30,8,22,.85) 72%);box-shadow:inset 0 0 0 1px rgba(255,180,210,.14)}
html[data-design=sakura] .pcrd,html[data-design=sakura] .rkr,html[data-design=sakura] .ctcard,html[data-design=sakura] .ctr{box-shadow:inset 0 0 0 1px rgba(255,180,210,.12)}
html[data-design=sakura] #mp{border-radius:20px;background:rgba(38,10,30,.93);box-shadow:inset 0 0 0 1px rgba(255,180,210,.22),0 18px 50px -12px rgba(0,0,0,.9),0 0 46px -18px rgba(255,111,159,.6)}
html[data-design=sakura] .stp{background:linear-gradient(180deg,rgba(60,18,48,.98),rgba(16,4,12,.98))}

/* =====================================================================
   ПРОФИЛЬ · ПУЛЬСЫ · МАГАЗИН
   ===================================================================== */
.coin{display:inline-block;width:1.15em;height:1.15em;flex:none;vertical-align:-.2em;filter:drop-shadow(0 1px 1.5px rgba(0,0,0,.45))}
.cbig{position:relative;display:inline-grid;place-items:center;flex:none;width:58px;height:58px;border-radius:50%;animation:cbFloat 3.6s ease-in-out infinite}
.cbig .coin{width:100%;height:100%;vertical-align:0;filter:drop-shadow(0 6px 14px rgba(255,170,40,.35)) drop-shadow(0 2px 3px rgba(0,0,0,.5))}
.cbig::after{content:"";position:absolute;inset:4%;border-radius:50%;background:linear-gradient(115deg,transparent 38%,rgba(255,255,255,.85) 50%,transparent 62%);background-size:260% 100%;background-position:130% 0;mix-blend-mode:overlay;animation:cbShine 3.6s ease-in-out infinite;pointer-events:none}
@keyframes cbShine{0%,55%{background-position:130% 0}85%,100%{background-position:-30% 0}}
@keyframes cbFloat{0%,100%{transform:translateY(0)}50%{transform:translateY(-3px)}}
/* шапка боковой панели: логотип + баланс + аватар */
.brandrow{display:flex;align-items:center;gap:8px;padding:0 4px 10px 0}
.brandrow .brand{flex:1;min-width:0;padding:0 12px}
.balp{display:flex;align-items:center;gap:6px;height:32px;padding:0 11px 0 6px;border-radius:999px;background:linear-gradient(180deg,rgba(255,200,80,.16),rgba(255,160,40,.06));
  box-shadow:inset 0 0 0 1px rgba(255,200,90,.32);font-weight:800;font-size:14px;color:#ffe3a3;font-variant-numeric:tabular-nums;transition:transform .25s var(--ease),box-shadow .25s,background .25s}
.balp .coin{width:22px;height:22px;vertical-align:0}
.balp:hover,.balp.on{transform:translateY(-1px);box-shadow:inset 0 0 0 1px rgba(255,210,110,.6),0 6px 20px -6px rgba(255,180,60,.6);background:linear-gradient(180deg,rgba(255,200,80,.26),rgba(255,160,40,.1))}
.balp.bump{animation:balBump .6s var(--ease)}
@keyframes balBump{0%{transform:scale(1)}30%{transform:scale(1.18)}60%{transform:scale(.96)}100%{transform:scale(1)}}
.meb{flex:none;width:36px;height:36px;display:grid;place-items:center;border-radius:50%;transition:transform .25s var(--ease)}
.meb:hover,.meb.on{transform:scale(1.08)}
.meb.on .mav-i{box-shadow:0 0 0 2px #fff}
/* летящие монеты */
.flyc{position:fixed;z-index:10050;width:26px;height:26px;pointer-events:none}
.flyc .coin{width:100%;height:100%;vertical-align:0}
.flyt{position:fixed;z-index:10050;pointer-events:none;display:flex;align-items:center;gap:6px;font:900 26px/1 "Segoe UI Variable Display","Segoe UI",system-ui,sans-serif;color:#ffe08a;text-shadow:0 2px 12px rgba(255,170,40,.6),0 2px 3px rgba(0,0,0,.6);white-space:nowrap}
.flyt .coin{width:28px;height:28px;vertical-align:0}
#toast.cn{display:flex;align-items:center;gap:8px;background:linear-gradient(180deg,#3a2a08,#21160a);color:#ffe3a3;box-shadow:inset 0 0 0 1px rgba(255,200,90,.45),0 10px 30px rgba(0,0,0,.5)}
#toast.cn .coin{width:22px;height:22px}
/* окно награды */
#rwd{position:fixed;inset:0;z-index:9600;display:grid;place-items:center;background:rgba(6,2,6,.62);backdrop-filter:blur(8px);animation:gfade .3s ease}
#rwd.out{animation:gfadeOut .26s ease forwards}
@keyframes gfadeOut{to{opacity:0}}
.rwc{position:relative;width:min(440px,calc(100vw - 40px));padding:34px 30px 26px;border-radius:26px;text-align:center;overflow:hidden;
  background:radial-gradient(120% 70% at 50% 0%,rgba(255,190,70,.24),transparent 60%),linear-gradient(180deg,#24120f,#140a0b);box-shadow:inset 0 0 0 1px rgba(255,200,90,.3),0 30px 80px rgba(0,0,0,.7);animation:rwIn .55s var(--ease)}
#rwd.out .rwc{animation:rwOut .26s ease forwards}
@keyframes rwIn{from{opacity:0;transform:translateY(24px) scale(.9)}}
@keyframes rwOut{to{opacity:0;transform:translateY(10px) scale(.96)}}
.rwc::before{content:"";position:absolute;left:50%;top:92px;width:520px;height:520px;margin:-260px 0 0 -260px;background:repeating-conic-gradient(from 0deg,rgba(255,210,110,.13) 0 9deg,transparent 9deg 22deg);
  -webkit-mask:radial-gradient(circle,#000 10%,transparent 62%);mask:radial-gradient(circle,#000 10%,transparent 62%);animation:rwRays 14s linear infinite;pointer-events:none}
@keyframes rwRays{to{transform:rotate(360deg)}}
.rwcoin{position:relative;width:120px;height:120px;margin:0 auto 14px;perspective:400px}
.rwcoin .coin{width:100%;height:100%;vertical-align:0;filter:drop-shadow(0 14px 30px rgba(255,170,40,.55)) drop-shadow(0 3px 4px rgba(0,0,0,.6));animation:rwSpin 2.6s cubic-bezier(.4,0,.2,1) .15s both}
@keyframes rwSpin{0%{transform:rotateY(0) scale(.3);opacity:0}20%{opacity:1}55%{transform:rotateY(720deg) scale(1.08)}70%{transform:rotateY(720deg) scale(.97)}100%{transform:rotateY(720deg) scale(1)}}
.rwn{position:relative;font:900 58px/1 "Segoe UI Variable Display","Segoe UI",system-ui,sans-serif;letter-spacing:-.03em;background:linear-gradient(180deg,#fff6d6,#ffc94a 55%,#d98a12);-webkit-background-clip:text;background-clip:text;color:transparent;filter:drop-shadow(0 4px 18px rgba(255,170,40,.35))}
.rwt{position:relative;margin-top:10px;font-size:21px;font-weight:800}
.rws{position:relative;margin:8px auto 0;max-width:340px;font-size:14px;line-height:1.45;color:rgba(255,255,255,.72)}
.rwa{position:relative;display:flex;justify-content:center;gap:10px;margin-top:22px}
.rwa .gbtn{padding:11px 24px}
.rwa .gbtn.gold{background:linear-gradient(135deg,#ffd76a,#e9a23a);color:#2a1500}
/* ---------- аватар профиля ---------- */
.mav{position:relative;display:inline-block;flex:none;width:var(--s);height:var(--s);vertical-align:middle}
.mav-i{position:absolute;inset:0;border-radius:50%;display:grid;place-items:center;background-size:cover;background-position:center;font-weight:900;font-size:calc(var(--s) * .42);line-height:1;color:#fff;
  box-shadow:0 calc(var(--s) * .06) calc(var(--s) * .18) rgba(0,0,0,.45);text-shadow:0 2px 8px rgba(0,0,0,.35);overflow:hidden}
.mav-r,.mav-g{position:absolute;inset:calc(var(--s) * -.1);border-radius:50%;pointer-events:none}
.mav-r{--rw:12%;-webkit-mask:radial-gradient(farthest-side,transparent calc(100% - var(--rw)),#000 calc(100% - var(--rw) + 1px));mask:radial-gradient(farthest-side,transparent calc(100% - var(--rw)),#000 calc(100% - var(--rw) + 1px))}
.mav-g{box-shadow:0 0 calc(var(--s) * .14) calc(var(--s) * .01) var(--fgc,transparent)}
.mav-h{position:absolute;left:-20%;top:-40%;width:140%;height:140%;overflow:visible;pointer-events:none;z-index:2;filter:drop-shadow(0 calc(var(--s) * .025) calc(var(--s) * .03) rgba(0,0,0,.45))}
.mav-h .hfl{animation:hFloat 3s ease-in-out infinite}
@keyframes hFloat{0%,100%{transform:translateY(0)}50%{transform:translateY(-4px)}}
.mav-h .hsp{animation:hSpark 2.4s ease-in-out infinite}
@keyframes hSpark{0%,100%{filter:brightness(1)}50%{filter:brightness(1.35) drop-shadow(0 0 3px #fff)}}
.mav-h .hfire{transform-origin:50% 18px;animation:hFire .5s ease-in-out infinite alternate}
@keyframes hFire{to{transform:scaleY(1.06) skewX(-2deg)}}
.mav[data-fr=f_silver] .mav-r{background:conic-gradient(from 30deg,#8a93a3,#ffffff,#b9c1cd,#eef1f6,#7d8796,#ffffff,#8a93a3);--rw:10%}
.mav[data-fr=f_silver]{--fgc:rgba(220,230,255,.25)}
.mav[data-fr=f_pixel] .mav-r{background:repeating-conic-gradient(#38e08a 0 11deg,#0c4a2a 11deg 15deg);--rw:14%;animation:frSpin 6s steps(24) infinite}
.mav[data-fr=f_toxic] .mav-r{background:conic-gradient(#b6ff3b,#2bff88,#e8ff6a,#2bff88,#b6ff3b)}
.mav[data-fr=f_toxic]{--fgc:rgba(170,255,60,.55)}
.mav[data-fr=f_neon] .mav-r{background:radial-gradient(farthest-side,#ff3f9e calc(100% - 9%),#ffd6ec calc(100% - 6%),#ff3f9e 100%);--rw:10%}
.mav[data-fr=f_neon]{--fgc:rgba(255,63,158,.85)}
.mav[data-fr=f_neon] .mav-g{animation:frNeon 2.4s ease-in-out infinite}
@keyframes frNeon{0%,100%{opacity:1}45%{opacity:.75}50%{opacity:.35}55%{opacity:.9}}
.mav[data-fr=f_gold] .mav-r{background:conic-gradient(from 20deg,#a8650c,#ffe9a8,#d99a1c,#fff3c4,#a8650c,#ffd45a,#8a5208,#ffe9a8,#a8650c)}
.mav[data-fr=f_gold]{--fgc:rgba(255,190,70,.45)}
.mav[data-fr=f_ice] .mav-r{background:conic-gradient(from 0deg,#e6fbff,#8ce6ff,#ffffff,#3bb4e6,#c9f4ff,#ffffff,#e6fbff)}
.mav[data-fr=f_ice]{--fgc:rgba(140,230,255,.6)}
.mav[data-fr=f_sakura] .mav-r{background:repeating-conic-gradient(from 0deg,#ffd6e6 0 7deg,#ff8fb4 7deg 15deg);--rw:13%;animation:frSpin 18s linear infinite}
.mav[data-fr=f_sakura]{--fgc:rgba(255,140,180,.55)}
.mav[data-fr=f_pulse] .mav-r{background:conic-gradient(from 0deg,var(--g0),var(--g3),#fff,var(--g2),var(--g0))}
.mav[data-fr=f_pulse]{--fgc:rgba(var(--ar),.7)}
.mav[data-fr=f_pulse] .mav-g{animation:frPulse 1.6s ease-out infinite}
@keyframes frPulse{0%{box-shadow:0 0 0 0 rgba(var(--ar),.75)}100%{box-shadow:0 0 0 calc(var(--s) * .16) rgba(var(--ar),0)}}
.mav[data-fr=f_rainbow] .mav-r{background:conic-gradient(#ff3b3b,#ffb03b,#ffe83b,#3bff7a,#3bd5ff,#7a3bff,#ff3bd5,#ff3b3b);animation:frSpin 4s linear infinite}
.mav[data-fr=f_rainbow]{--fgc:rgba(255,255,255,.3)}
.mav[data-fr=f_galaxy] .mav-r{background:radial-gradient(circle at 20% 30%,#fff 0 1px,transparent 1.5px),radial-gradient(circle at 70% 80%,#fff 0 1px,transparent 1.5px),radial-gradient(circle at 80% 20%,#fff 0 .8px,transparent 1.3px),
  conic-gradient(#2b0a5c,#7a5cff,#00d4ff,#ff4fd8,#3a0e7a,#7a5cff,#2b0a5c);--rw:14%;animation:frSpin 10s linear infinite}
.mav[data-fr=f_galaxy]{--fgc:rgba(140,100,255,.6)}
.mav[data-fr=f_fire] .mav-r{background:conic-gradient(#ff2d0a,#ffb03b,#ff5a1e,#ffe27a,#ff2d0a,#ff8a1e,#ff2d0a);--rw:14%;animation:frSpin 2.4s linear infinite}
.mav[data-fr=f_fire]{--fgc:rgba(255,110,30,.85)}
.mav[data-fr=f_fire] .mav-g{animation:frFlick .18s ease-in-out infinite alternate}
@keyframes frFlick{to{opacity:.65;transform:scale(1.03)}}
.mav[data-fr=f_holo] .mav-r{background:conic-gradient(#ffb3e6,#b3d9ff,#b3ffe0,#fff3b3,#e0b3ff,#ffb3e6);animation:frSpin 6s linear infinite,frHue 5s linear infinite}
.mav[data-fr=f_holo]{--fgc:rgba(200,180,255,.55)}
@keyframes frHue{to{filter:hue-rotate(360deg)}}
.mav[data-fr=f_diamond] .mav-r{background:conic-gradient(from 0deg,#ffffff,#9cecff,#e8d6ff,#ffffff,#b3f0ff,#fff,#d6e4ff,#ffffff);--rw:13%;animation:frSpin 7s linear infinite}
.mav[data-fr=f_diamond]{--fgc:rgba(190,235,255,.75)}
@keyframes frSpin{to{transform:rotate(360deg)}}
.mav-sp{position:absolute;inset:-14%;pointer-events:none;z-index:1}
.mav-sp i{position:absolute;width:16%;height:16%;background:radial-gradient(circle,#fff 0 12%,transparent 13%),conic-gradient(from 45deg,transparent 0 40deg,#fff 45deg,transparent 50deg 130deg,#fff 135deg,transparent 140deg 220deg,#fff 225deg,transparent 230deg 310deg,#fff 315deg,transparent 320deg);
  -webkit-mask:radial-gradient(circle,#000 20%,transparent 70%);mask:radial-gradient(circle,#000 20%,transparent 70%);animation:spTw 2.2s ease-in-out infinite}
.mav-sp i:nth-child(1){left:6%;top:12%}.mav-sp i:nth-child(2){right:2%;top:40%;animation-delay:.7s}.mav-sp i:nth-child(3){left:24%;bottom:0;animation-delay:1.4s}
@keyframes spTw{0%,100%{transform:scale(0) rotate(0)}50%{transform:scale(1) rotate(90deg)}}
/* ---------- значки ---------- */
.mbd{display:inline-grid;place-items:center;flex:none;width:1.05em;height:1.05em;border-radius:50%;vertical-align:-.12em;margin-left:.3em;
  background:radial-gradient(circle at 35% 28%,rgba(255,255,255,.55),transparent 45%),linear-gradient(160deg,var(--b1),var(--b2));box-shadow:inset 0 0 0 1.5px rgba(255,255,255,.35),0 3px 10px -2px var(--b2)}
.mbd svg{width:64%;height:64%}
.mbd.big{width:64px;height:64px;margin:0}
/* ---------- стили ника ---------- */
.mnm{position:relative;display:inline-block;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;vertical-align:bottom;padding:0 .04em .06em 0}
.mnm[data-nm=n_gold]{background:linear-gradient(100deg,#a8650c 0%,#ffe9a8 22%,#d99a1c 40%,#fff6d6 50%,#d99a1c 60%,#ffe9a8 78%,#a8650c 100%);background-size:250% 100%;-webkit-background-clip:text;background-clip:text;color:transparent;animation:nmSheen 4s linear infinite;filter:drop-shadow(0 2px 10px rgba(255,180,60,.35))}
.mnm[data-nm=n_chrome]{background:linear-gradient(180deg,#ffffff 0%,#d8dde6 38%,#5d6878 52%,#c9cfda 66%,#ffffff 100%);-webkit-background-clip:text;background-clip:text;color:transparent;filter:drop-shadow(0 2px 4px rgba(0,0,0,.5))}
.mnm[data-nm=n_neon]{color:#ffe0f0;text-shadow:0 0 4px #fff,0 0 10px #ff3f9e,0 0 22px #ff3f9e,0 0 40px #ff3f9e;animation:nmNeon 3.4s ease-in-out infinite}
@keyframes nmNeon{0%,100%{opacity:1}46%{opacity:1}48%{opacity:.7}50%{opacity:1}52%{opacity:.82}54%{opacity:1}}
.mnm[data-nm=n_rainbow]{background:linear-gradient(90deg,#ff3b3b,#ffb03b,#ffe83b,#3bff7a,#3bd5ff,#7a3bff,#ff3bd5,#ff3b3b);background-size:200% 100%;-webkit-background-clip:text;background-clip:text;color:transparent;animation:nmSheen 3s linear infinite}
.mnm[data-nm=n_fire]{background:linear-gradient(0deg,#ff2d0a 0%,#ff8a1e 40%,#ffe27a 80%,#fff6d6 100%);background-size:100% 200%;-webkit-background-clip:text;background-clip:text;color:transparent;animation:nmFire 1.2s ease-in-out infinite alternate;filter:drop-shadow(0 0 10px rgba(255,100,30,.55))}
@keyframes nmFire{to{background-position:0 100%}}
.mnm[data-nm=n_ice]{background:linear-gradient(180deg,#ffffff,#c9f4ff 50%,#5fc8f0);-webkit-background-clip:text;background-clip:text;color:transparent;filter:drop-shadow(0 0 10px rgba(140,230,255,.55))}
.mnm[data-nm=n_sakura]{background:linear-gradient(180deg,#fff0f5,#ffb3cc 55%,#ff6f9f);-webkit-background-clip:text;background-clip:text;color:transparent;filter:drop-shadow(0 0 12px rgba(255,120,170,.45))}
.mnm[data-nm=n_toxic]{color:#c8ff4a;text-shadow:0 0 8px rgba(170,255,60,.8),0 0 22px rgba(80,255,120,.45)}
.mnm[data-nm=n_shadow]{color:#fff;text-shadow:1px 1px 0 var(--g2),2px 2px 0 var(--g2),3px 3px 0 var(--g0),4px 4px 0 var(--g0),5px 5px 0 var(--g0),8px 8px 14px rgba(0,0,0,.5);overflow:visible;padding-right:.2em}
.mnm[data-nm=n_glitch]{color:#fff;text-shadow:2px 0 #ff2bd6,-2px 0 #00f0ff;animation:nmGlitch 2.6s steps(1) infinite}
@keyframes nmGlitch{0%,100%{text-shadow:2px 0 #ff2bd6,-2px 0 #00f0ff;transform:none}8%{text-shadow:-3px 1px #ff2bd6,3px -1px #00f0ff;transform:translateX(1px)}10%{text-shadow:2px 0 #ff2bd6,-2px 0 #00f0ff;transform:none}
  52%{text-shadow:4px -1px #ff2bd6,-4px 1px #00f0ff;transform:skewX(-6deg)}54%{text-shadow:2px 0 #ff2bd6,-2px 0 #00f0ff;transform:none}}
@keyframes nmSheen{to{background-position:-200% 0}}
/* ---------- фоны профиля ---------- */
.bnr{position:relative;overflow:hidden;background:linear-gradient(135deg,hsl(var(--tn) 60% 22%),hsl(calc(var(--tn) - 30) 50% 10%));isolation:isolate}
.bnr>i{position:absolute;inset:0;pointer-events:none}
.bnr>canvas{position:absolute;inset:0;width:100%;height:100%;pointer-events:none;z-index:2}
.bnr[data-bn=b_sunset]{background:radial-gradient(60% 50% at 50% 100%,#ffd36b 0,#ff9a5a 30%,transparent 60%),linear-gradient(180deg,#3a1c71 0%,#d76d77 55%,#ffaf7b 100%)}
.bnr[data-bn=b_ocean]{background:repeating-linear-gradient(100deg,transparent 0 40px,rgba(180,240,255,.06) 40px 60px,transparent 60px 110px),linear-gradient(180deg,#1a7fa8 0%,#0a4b78 40%,#03182b 100%)}
.bnr[data-bn=b_ice]{background:radial-gradient(40% 60% at 20% 30%,rgba(255,255,255,.7),transparent 60%),radial-gradient(50% 60% at 85% 70%,rgba(200,245,255,.6),transparent 60%),linear-gradient(160deg,#e6fbff 0%,#8ce6ff 40%,#2a6fa8 100%)}
.bnr[data-bn=b_midnight]{background:radial-gradient(1.2px 1.2px at 20% 30%,#fff,transparent),radial-gradient(1px 1px at 70% 20%,#fff,transparent),radial-gradient(1.4px 1.4px at 45% 60%,#fff,transparent),radial-gradient(1px 1px at 85% 55%,#fff,transparent),radial-gradient(1px 1px at 10% 75%,#fff,transparent),radial-gradient(1.2px 1.2px at 60% 85%,#fff,transparent),
  radial-gradient(70% 90% at 75% 0%,#1c2a6b,transparent 70%),linear-gradient(180deg,#0b1030,#03040e);background-size:180px 120px,220px 140px,160px 110px,240px 160px,200px 130px,170px 120px,100% 100%,100% 100%}
.bnr[data-bn=b_vinyl]{background:radial-gradient(circle at 78% 50%,#ff3f6e 0 7%,#7a0718 7.4% 8%,transparent 8.4%),repeating-radial-gradient(circle at 78% 50%,#1c1a1e 0 2px,#0c0b0d 2px 4px),#0c0b0d}
.bnr[data-bn=b_vinyl] .bnr-a{background:conic-gradient(from 0deg at 78% 50%,transparent 0 30deg,rgba(255,255,255,.08) 45deg,transparent 60deg 210deg,rgba(255,255,255,.08) 225deg,transparent 240deg)}
.bnr[data-bn=b_matrix]{background:linear-gradient(180deg,#020a04,#03140a)}
.bnr[data-bn=b_matrix] .bnr-a{background:repeating-linear-gradient(90deg,transparent 0 12px,rgba(0,255,110,.5) 12px 14px,transparent 14px 26px),linear-gradient(180deg,transparent 0%,#000 100%);
  -webkit-mask:repeating-linear-gradient(180deg,#000 0 6px,transparent 6px 14px,#000 14px 18px,transparent 18px 40px);mask:repeating-linear-gradient(180deg,#000 0 6px,transparent 6px 14px,#000 14px 18px,transparent 18px 40px);opacity:.55;animation:bnRain 2.4s linear infinite}
@keyframes bnRain{to{-webkit-mask-position:0 40px;mask-position:0 40px}}
.bnr[data-bn=b_aurora]{background:linear-gradient(180deg,#04121c,#020a10)}
.bnr[data-bn=b_aurora] .bnr-a{background:radial-gradient(40% 60% at 30% 40%,rgba(60,255,170,.55),transparent 70%),radial-gradient(40% 70% at 70% 30%,rgba(140,90,255,.5),transparent 70%),radial-gradient(30% 50% at 50% 70%,rgba(60,200,255,.35),transparent 70%);filter:blur(10px);animation:bnDrift 9s ease-in-out infinite alternate}
@keyframes bnDrift{to{transform:translateX(8%) scaleY(1.15)}}
.bnr[data-bn=b_forest]{background:radial-gradient(circle at 20% 40%,#f4ffb0 0 1.5px,rgba(220,255,120,.35) 2px,transparent 7px),radial-gradient(circle at 62% 70%,#f4ffb0 0 1.5px,rgba(220,255,120,.3) 2px,transparent 8px),radial-gradient(circle at 82% 28%,#f4ffb0 0 1.2px,rgba(220,255,120,.3) 2px,transparent 6px),
  radial-gradient(70% 80% at 80% 0%,#1e5c47,transparent 70%),linear-gradient(180deg,#0b2a20 0%,#05150f 70%,#020806 100%);background-size:140px 90px,190px 120px,160px 100px,100% 100%,100% 100%;animation:bnFf 4s ease-in-out infinite alternate}
@keyframes bnFf{to{background-position:12px -10px,-14px 8px,8px 12px,0 0,0 0}}
.bnr[data-bn=b_forest] .bnr-a{background:linear-gradient(0deg,#020806 0 8%,transparent 8%),repeating-linear-gradient(90deg,transparent 0 30px,rgba(0,0,0,.4) 30px 34px);-webkit-mask:linear-gradient(0deg,#000 0 40%,transparent 80%);mask:linear-gradient(0deg,#000 0 40%,transparent 80%)}
.bnr[data-bn=b_sakura]{background:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='120' height='120'%3E%3Cg fill='%23ffffff' opacity='.55'%3E%3Cpath d='M20 18c4-6 10-4 9 2-1 5-7 6-9 4z'/%3E%3Cpath d='M86 64c5-5 11-2 9 4-2 5-8 5-9 2z'/%3E%3Cpath d='M48 98c3-6 9-5 9 1 0 5-6 6-9 3z'/%3E%3C/g%3E%3C/svg%3E"),
  radial-gradient(50% 70% at 80% 10%,#fff1f6 0,#ffc0d5 25%,transparent 55%),linear-gradient(180deg,#ff9cc0 0%,#c2437a 60%,#4a1640 100%)}
.bnr[data-bn=b_lava]{background:linear-gradient(180deg,#1a0302,#3a0a04)}
.bnr[data-bn=b_lava] .bnr-a{background:radial-gradient(30% 50% at 20% 80%,#ff5a1e,transparent 70%),radial-gradient(25% 45% at 60% 90%,#ffb03b,transparent 70%),radial-gradient(35% 50% at 90% 70%,#ff2d0a,transparent 70%);filter:blur(14px);animation:bnLava 6s ease-in-out infinite alternate}
.bnr[data-bn=b_lava] .bnr-b{background:radial-gradient(20% 40% at 40% 70%,#ffe27a,transparent 70%);filter:blur(18px);animation:bnLava 4s ease-in-out infinite alternate-reverse;opacity:.7}
@keyframes bnLava{to{transform:translate(4%,-12%) scale(1.15)}}
.bnr[data-bn=b_euph]{background:radial-gradient(80% 70% at 50% 120%,#ff6a3d,transparent 60%),linear-gradient(180deg,#1a0308,#3a0816)}
.bnr[data-bn=b_euph] .bnr-a{background:repeating-linear-gradient(90deg,rgba(255,60,90,.0) 0 18px,rgba(255,80,110,.35) 18px 22px,rgba(255,60,90,0) 22px 40px);-webkit-mask:linear-gradient(180deg,transparent 0%,#000 40%,#000 70%,transparent 100%);mask:linear-gradient(180deg,transparent 0%,#000 40%,#000 70%,transparent 100%);filter:blur(3px);animation:bnCurt 7s ease-in-out infinite alternate}
@keyframes bnCurt{to{transform:translateX(-30px) skewX(-8deg)}}
.bnr[data-bn=b_holo]{background:linear-gradient(120deg,#ffb3e6,#b3d9ff,#b3ffe0,#fff3b3,#e0b3ff,#ffb3e6);background-size:300% 300%;animation:bnHolo 8s ease-in-out infinite alternate}
.bnr[data-bn=b_holo] .bnr-a{background:repeating-linear-gradient(115deg,transparent 0 30px,rgba(255,255,255,.25) 30px 34px,transparent 34px 70px);mix-blend-mode:overlay}
@keyframes bnHolo{to{background-position:100% 100%}}
.bnr[data-bn=b_synth]{background:linear-gradient(180deg,#14002a 0%,#3a0a5c 35%,#ff2bd6 62%,#ffb347 66%,#14002a 66.5%,#0a0018 100%)}
.bnr[data-bn=b_synth] .bnr-a{left:50%;top:22%;width:38%;aspect-ratio:1;inset:auto;transform:translateX(-50%);border-radius:50%;background:linear-gradient(180deg,#ffe27a,#ff6a3d 60%,#ff2bd6);
  -webkit-mask:linear-gradient(180deg,#000 0 52%,transparent 52% 56%,#000 56% 62%,transparent 62% 67%,#000 67% 74%,transparent 74% 80%,#000 80%);mask:linear-gradient(180deg,#000 0 52%,transparent 52% 56%,#000 56% 62%,transparent 62% 67%,#000 67% 74%,transparent 74% 80%,#000 80%);height:auto;filter:drop-shadow(0 0 24px #ff2bd6)}
.bnr[data-bn=b_synth] .bnr-b{top:66%;background:repeating-linear-gradient(90deg,rgba(0,240,255,.7) 0 2px,transparent 2px 60px),repeating-linear-gradient(0deg,rgba(255,43,214,.8) 0 2px,transparent 2px 22px);
  transform:perspective(200px) rotateX(55deg);transform-origin:50% 0;inset:66% -30% -40% -30%;animation:bnGrid 1.4s linear infinite}
@keyframes bnGrid{to{background-position:0 0,0 22px}}
.bnr[data-bn=b_city]{background:linear-gradient(180deg,#120328 0%,#4a0f5c 45%,#ff6a3d 85%,#ffb347 100%)}
.bnr[data-bn=b_city] .bnr-a{background:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='400' height='120' preserveAspectRatio='none'%3E%3Cpath d='M0 120V70h20V50h18v30h14V30h22v50h10V60h16v60zM100 120V40h26v20h12V20h20v100zM160 120V64h22V44h18v34h12V24h26v96zM238 120V56h16V36h24v30h10V48h20v72zM308 120V30h28v26h14V66h22V40h28v80z' fill='%23070212'/%3E%3C/svg%3E") bottom/100% 62% no-repeat}
.bnr[data-bn=b_city] .bnr-b{background:radial-gradient(1.5px 1.5px at 10% 70%,#ffe27a,transparent),radial-gradient(1.5px 1.5px at 30% 82%,#7af0ff,transparent),radial-gradient(1.5px 1.5px at 55% 75%,#ffe27a,transparent),radial-gradient(1.5px 1.5px at 82% 80%,#ff8ad8,transparent);background-size:90px 100%;animation:bnWin 3s steps(2) infinite}
@keyframes bnWin{50%{opacity:.6}}
.bnr[data-bn=b_galaxy]{background:radial-gradient(1px 1px at 10% 20%,#fff,transparent),radial-gradient(1.4px 1.4px at 40% 70%,#fff,transparent),radial-gradient(1px 1px at 75% 35%,#fff,transparent),radial-gradient(1.2px 1.2px at 90% 80%,#fff,transparent),#05020f;background-size:150px 110px,210px 140px,170px 120px,230px 150px,100% 100%}
.bnr[data-bn=b_galaxy] .bnr-a{inset:-40%;background:radial-gradient(30% 25% at 40% 50%,rgba(255,79,216,.55),transparent 70%),radial-gradient(35% 30% at 60% 45%,rgba(122,92,255,.6),transparent 70%),radial-gradient(25% 20% at 50% 55%,rgba(0,212,255,.45),transparent 70%);filter:blur(12px);animation:frSpin 40s linear infinite}
.bnr[data-bn=b_gold]{background:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='60' height='60'%3E%3Cg fill='none' stroke='%23d6b578' stroke-width='1' opacity='.55'%3E%3Cpath d='M0 60 L30 30 L60 60 M0 0 L30 30 L60 0'/%3E%3Ccircle cx='30' cy='30' r='6'/%3E%3Cpath d='M30 0 V12 M30 48 V60 M0 30 H12 M48 30 H60'/%3E%3C/g%3E%3C/svg%3E"),radial-gradient(70% 90% at 50% 0%,#3a2a12,#0b0805 70%)}
.bnr[data-bn=b_gold] .bnr-a{background:linear-gradient(110deg,transparent 35%,rgba(255,230,160,.35) 50%,transparent 65%);background-size:300% 100%;animation:bnShine 4s ease-in-out infinite}
@keyframes bnShine{0%{background-position:120% 0}60%,100%{background-position:-60% 0}}
/* ---------- мини-профиль (наборы и примерочная) ---------- */
.mpf{position:relative;border-radius:14px;overflow:hidden;background:rgba(0,0,0,.35)}
.bnr.sm{height:88px}
.mpf-b{position:relative;z-index:3;display:flex;flex-direction:column;align-items:center;gap:8px;margin-top:-46px;padding:0 12px 14px;text-align:center}
.mpf-n{display:flex;align-items:center;justify-content:center;max-width:100%;font-size:19px;font-weight:900;letter-spacing:-.02em}
.mpf-s{font-size:12px;color:var(--sub)}
/* ---------- страница профиля ---------- */
.pm.pf .ptop,.pm.shop .ptop{position:absolute;left:0;right:0;top:0}
.pf .pbody{padding-top:0}
.bnr.pfbn{height:270px;margin:0 -32px;border-radius:0}
.bnr.pfbn::after{content:"";position:absolute;inset:0;z-index:3;background:linear-gradient(180deg,transparent 45%,var(--bg) 100%);pointer-events:none}
.pfhd{position:relative;z-index:4;display:flex;align-items:flex-end;gap:28px;margin-top:-92px;flex-wrap:wrap}
.pfav{position:relative;flex:none;cursor:pointer;margin-left:10px}
.pfav .mav-i{box-shadow:0 0 0 5px var(--bg),0 18px 40px rgba(0,0,0,.5)}
.pfcam{position:absolute;right:4px;bottom:4px;z-index:3;width:40px;height:40px;border-radius:50%;display:grid;place-items:center;background:rgba(0,0,0,.7);box-shadow:0 0 0 3px var(--bg);opacity:0;transform:scale(.8);transition:opacity .25s,transform .25s var(--ease)}
.pfav:hover .pfcam{opacity:1;transform:none}
.pfcam svg{width:20px;height:20px}
.pfid,.pfed{flex:1;min-width:260px;padding-bottom:6px}
.pfnm{display:flex;align-items:center;min-width:0;font-size:clamp(32px,4vw,54px);font-weight:900;letter-spacing:-.04em;line-height:1.15}
.pfbio{margin:6px 0 0;max-width:620px;font-size:15px;line-height:1.5;color:rgba(255,255,255,.85);white-space:pre-wrap;word-break:break-word}
.pfbio.emp{color:var(--sub);font-style:italic}
.pfact{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px}
.pfact .gbtn{display:inline-flex;align-items:center;gap:8px;padding:10px 20px;font-size:14px}
.pfact .coin{width:18px;height:18px;vertical-align:0}
.pfed label{position:relative;display:block;margin-bottom:10px;font-size:12px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--sub)}
.pfed input,.pfed textarea{display:block;width:100%;max-width:560px;margin-top:6px;padding:11px 14px;border:0;border-radius:12px;background:rgba(255,255,255,.08);box-shadow:inset 0 0 0 1px rgba(255,255,255,.12);color:#fff;font:600 16px/1.4 inherit;text-transform:none;letter-spacing:0;outline:0;resize:none;transition:box-shadow .2s}
.pfed input{font-size:22px;font-weight:800}
.pfed input:focus,.pfed textarea:focus{box-shadow:inset 0 0 0 2px var(--g2)}
.pfc{position:absolute;right:calc(100% - min(100%,560px) + 10px);bottom:8px;font-size:11px;letter-spacing:0;color:var(--sub)}
.pfbal,.shbal{display:flex;align-items:center;gap:14px;padding:14px 22px 14px 14px;border-radius:18px;background:linear-gradient(180deg,rgba(255,200,80,.14),rgba(255,160,40,.04));box-shadow:inset 0 0 0 1px rgba(255,200,90,.28)}
.pfbal b,.shbal b{display:block;font-size:30px;font-weight:900;line-height:1;color:#ffe3a3;font-variant-numeric:tabular-nums}
.pfbal em,.shbal em{display:block;margin-top:4px;font-style:normal;font-size:12px;color:rgba(255,227,163,.7)}
.pfst{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin-top:28px}
.pfs{padding:14px 16px;border-radius:14px;background:rgba(255,255,255,.05);box-shadow:inset 0 0 0 1px rgba(255,255,255,.06)}
.pfs em{display:block;font-style:normal;font-size:11px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--sub)}
.pfs b{display:block;margin-top:6px;font-size:26px;font-weight:900;font-variant-numeric:tabular-nums}
.pfs span{font-size:12px;color:var(--sub)}
.pffv{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-top:10px}
.pff{padding:12px 14px;border-radius:14px;background:rgba(255,255,255,.05);box-shadow:inset 0 0 0 1px rgba(255,255,255,.06);min-width:0}
.pff>em{display:block;margin-bottom:8px;font-style:normal;font-size:11px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--sub)}
.pffr{display:flex;align-items:center;gap:12px;min-width:0}
.pffr[data-open],.pffr[data-art]{cursor:pointer}
.pffr .cv,.pffr .av,.pfg{flex:none;width:46px;height:46px;border-radius:10px;font-size:20px}
.pffr .av{border-radius:50%}
.pfg{display:grid;place-items:center;font-weight:900;background:linear-gradient(135deg,var(--g2),var(--g0))}
.pffr>div:nth-child(2){flex:1;min-width:0}
.pffr b,.pffr span{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pffr b{font-size:15px}.pffr span{font-size:12px;color:var(--sub)}
.pffr i{font-style:normal;font-weight:900;font-size:20px}
.pfn{color:var(--sub);font-size:14px}
.pftab{display:inline-flex;flex-wrap:wrap;margin-bottom:14px}
.psb em{font-style:normal;opacity:.55;margin-left:2px}
.pfe{grid-column:1/-1;display:flex;flex-direction:column;align-items:center;gap:6px;padding:40px 20px;text-align:center;color:var(--sub);border-radius:16px;background:rgba(255,255,255,.03)}
.pfe b{font-size:18px;color:#fff}
.pfe .gbtn{margin-top:10px}
/* ---------- магазин ---------- */
.shop .pbody{padding-top:44px}
.shw{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:24px;align-items:start}
.shm{min-width:0}
.shtry{position:sticky;top:12px;display:flex;flex-direction:column;gap:12px;padding:14px;border-radius:18px;background:rgba(255,255,255,.05);box-shadow:inset 0 0 0 1px rgba(255,255,255,.08)}
.shtry-h{font-size:11px;font-weight:800;letter-spacing:.14em;text-transform:uppercase;color:var(--sub)}
#shTry .mpf{transition:box-shadow .3s}
#shTry.trying .mpf{box-shadow:0 0 0 2px rgba(var(--ar),.7),0 10px 40px -10px rgba(var(--ar),.6)}
.shtry .gbtn{padding:10px 18px;font-size:14px}
.kgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:14px;margin-bottom:26px}
.kit{display:flex;flex-direction:column;border-radius:16px;overflow:hidden;background:linear-gradient(180deg,rgba(255,255,255,.07),rgba(255,255,255,.02));box-shadow:inset 0 0 0 1px rgba(255,255,255,.08);
  transition:transform .35s var(--ease),box-shadow .35s;animation:pgItem .5s var(--ease) backwards;animation-delay:calc(var(--i) * 40ms)}
.kit:hover{transform:translateY(-4px);box-shadow:inset 0 0 0 1px rgba(255,255,255,.18),0 18px 40px -16px rgba(0,0,0,.8)}
.kit .mpf{border-radius:0;background:none}
.kit-b{display:flex;align-items:flex-end;justify-content:space-between;gap:10px;padding:2px 14px 14px}
.kit-b b{display:block;font-size:17px;font-weight:900}
.kit-b span{display:block;font-size:12px;color:var(--sub);margin-top:2px}
.kit-b em{display:block;margin-top:6px;font-style:normal;font-size:11px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:rgba(255,227,163,.85)}
.shtab{display:inline-flex;flex-wrap:wrap;margin-bottom:14px}
.igrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:12px;transition:opacity .15s,transform .15s}
.igrid.out{opacity:0;transform:translateY(8px)}
.itm{position:relative;display:flex;flex-direction:column;gap:10px;padding:10px 10px 12px;border-radius:14px;background:linear-gradient(180deg,rgba(255,255,255,.07),rgba(255,255,255,.025));
  box-shadow:inset 0 0 0 1px rgba(255,255,255,.07);transition:transform .3s var(--ease),box-shadow .3s;animation:pgItem .45s var(--ease) backwards;animation-delay:calc(var(--i,0) * 25ms)}
.itm::before{content:"";position:absolute;left:12px;right:12px;top:0;height:2px;border-radius:0 0 2px 2px;background:var(--rc);opacity:.85;box-shadow:0 0 12px var(--rc)}
.itm:hover{transform:translateY(-3px);box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--rc) 55%,transparent),0 14px 30px -14px color-mix(in srgb,var(--rc) 70%,transparent)}
.itm.on{box-shadow:inset 0 0 0 2px var(--g2)}
.ipv{position:relative;height:118px;border-radius:10px;overflow:hidden;display:grid;place-items:center;background:radial-gradient(80% 80% at 50% 40%,color-mix(in srgb,var(--rc) 22%,transparent),transparent 70%),rgba(0,0,0,.28)}
.itm.sm .ipv{height:100px}
.ipv.avp{padding-top:18px}
.ipv.bgp .bnr{position:absolute;inset:0}
.ipv.nmp{padding:0 10px;font-size:22px;font-weight:900;letter-spacing:-.02em;min-width:0}
.ipv.nmp .mnm{max-width:100%}
.ipv.fxp{background:linear-gradient(180deg,#120a1c,#05030a)}
.ipv.fxp canvas{position:absolute;inset:0;width:100%;height:100%}
.itx{min-width:0;padding:0 2px}
.itx em{display:block;font-style:normal;font-size:10.5px;font-weight:800;letter-spacing:.1em;text-transform:uppercase;color:var(--sub)}
.itx b{display:block;margin-top:2px;font-size:14.5px;font-weight:800;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.itx span{display:block;font-size:12px;color:var(--sub)}
.itb{display:flex;align-items:center;justify-content:center;gap:6px;height:36px;padding:0 14px;border-radius:10px;background:rgba(255,255,255,.1);font-weight:800;font-size:14px;color:#fff;transition:background .2s,transform .2s var(--ease)}
.itb:hover{background:rgba(255,255,255,.18)}
.itb:active{transform:scale(.96)}
.itb.on{background:rgba(var(--ar),.22);color:#fff;box-shadow:inset 0 0 0 1px rgba(var(--ar),.6)}
.itb.buy{background:linear-gradient(180deg,#ffd76a,#e9a23a);color:#2a1500;box-shadow:0 6px 16px -8px rgba(255,170,40,.8)}
.itb.buy:hover{background:linear-gradient(180deg,#ffe28a,#f2b04a)}
.itb.buy .coin{width:18px;height:18px;vertical-align:0}
.itb.buy s{opacity:.55;font-weight:700;font-size:12px}
.itb.poor{filter:saturate(.35) brightness(.8)}
.itb.shake,.itb.buy.shake{animation:shake .4s}
.kit .itb{flex:none}
.shsp{position:fixed;z-index:10050;width:8px;height:8px;border-radius:50%;pointer-events:none;background:radial-gradient(circle,#fff6cf,#ffc94a 60%,transparent 70%)}
.shop .phead p .coin{width:16px;height:16px}
@media (max-width:1100px){.shw{grid-template-columns:1fr}.shtry{position:relative;top:0;max-width:340px}.pfst{grid-template-columns:repeat(2,1fr)}.pffv{grid-template-columns:1fr}}
@media (prefers-reduced-motion:reduce){.mav-r,.mav-g,.bnr i,.mnm,.cbig,.cbig::after{animation:none!important}}

/* ===================== v2: коллекции, карточки, профиль ===================== */
.mav-r,.mav-sp{will-change:transform}
.mav-i .mav-sil{width:62%;height:62%;margin-top:14%;fill:rgba(255,255,255,.22)}
.mav-h .hglw{animation:hGlow 2.2s ease-in-out infinite}
@keyframes hGlow{0%,100%{opacity:1}50%{opacity:.55}}
/* пульсирующая рамка — без перерисовки тени каждый кадр */
.mav[data-fr=f_pulse] .mav-g{animation:none;box-shadow:0 0 calc(var(--s) * .14) calc(var(--s) * .01) var(--fgc)}
.mav[data-fr=f_pulse] .mav-g::after{content:"";position:absolute;inset:0;border-radius:50%;border:calc(var(--s) * .025) solid rgba(var(--ar),.8);animation:frRip 1.6s ease-out infinite;will-change:transform,opacity}
@keyframes frRip{from{transform:scale(1);opacity:.9}to{transform:scale(1.32);opacity:0}}
/* ---------- рамки коллекций ---------- */
.mav[data-fr=fx_euph] .mav-r{background:conic-gradient(from 0deg,#ff2d55,#ff8a3d,#ff2d9a,#ffd36b,#ff2d55,#b0103a,#ff2d55);--rw:15%;animation:frSpin 5s linear infinite}
.mav[data-fr=fx_euph]{--fgc:rgba(255,45,85,.85)}
.mav[data-fr=fx_euph] .mav-g{animation:frBreath 2.4s ease-in-out infinite}
@keyframes frBreath{0%,100%{opacity:.6}50%{opacity:1}}
.mav[data-fr=fx_ocean] .mav-r{background:conic-gradient(from 0deg,#e6fbff,#2cc6f0,#0b5f86,#5fd8f7,#ffffff,#2cc6f0,#063a58,#e6fbff);--rw:15%;animation:frSpin 7s linear infinite}
.mav[data-fr=fx_ocean]{--fgc:rgba(44,198,240,.75)}
.mav[data-fr=fx_ocean] .mav-g{animation:frBreath 3.2s ease-in-out infinite}
.mav[data-fr=fx_sakura] .mav-r{background:radial-gradient(circle at 50% 6%,#fff 0 3%,#ff8fb4 3.5% 6%,transparent 6.5%),radial-gradient(circle at 94% 50%,#fff 0 3%,#ff8fb4 3.5% 6%,transparent 6.5%),radial-gradient(circle at 50% 94%,#fff 0 3%,#ff8fb4 3.5% 6%,transparent 6.5%),radial-gradient(circle at 6% 50%,#fff 0 3%,#ff8fb4 3.5% 6%,transparent 6.5%),
  repeating-conic-gradient(from 0deg,#ffe6ef 0 8deg,#ff9cbe 8deg 16deg);--rw:16%;animation:frSpin 22s linear infinite}
.mav[data-fr=fx_sakura]{--fgc:rgba(255,130,175,.7)}
.mav[data-fr=fx_glass] .mav-r{background:conic-gradient(from 0deg,rgba(255,255,255,.95),#b3d9ff,#ffd1f0,#b3ffe0,#fff3b3,#e0b3ff,rgba(255,255,255,.95));--rw:13%;animation:frSpin 9s linear infinite}
.mav[data-fr=fx_glass]{--fgc:rgba(220,230,255,.6)}
.mav[data-fr=fx_ff] .mav-r{background:repeating-conic-gradient(from 0deg,#f4ffb0 0 3deg,#0d3a2a 4deg 26deg),#0d3a2a;--rw:14%;animation:frSpin 14s linear infinite}
.mav[data-fr=fx_ff]{--fgc:rgba(200,255,120,.55)}
.mav[data-fr=fx_ff] .mav-g{animation:frBreath 2s ease-in-out infinite}
.mav[data-fr=fx_amb] .mav-r{background:conic-gradient(from 0deg,#a78bfa,#fb923c,#38bdf8,#f472b6,#a78bfa);--rw:17%;animation:frSpin 8s linear infinite}
.mav[data-fr=fx_amb]{--fgc:rgba(167,139,250,.8)}
.mav[data-fr=fx_amb] .mav-g{box-shadow:0 0 calc(var(--s) * .22) calc(var(--s) * .04) rgba(167,139,250,.55),0 0 calc(var(--s) * .3) calc(var(--s) * .02) rgba(251,146,60,.35)}
/* ---------- фоны коллекций ---------- */
.bnr>i{will-change:transform}
.bnr[data-bn=bx_euph]{background:radial-gradient(1px 1px at 15% 25%,#fff,transparent),radial-gradient(1.3px 1.3px at 70% 18%,#fff,transparent),radial-gradient(1px 1px at 45% 40%,#ffd1dc,transparent),
  radial-gradient(70% 60% at 50% 115%,rgba(255,77,46,.55),transparent 60%),linear-gradient(180deg,#0b0106 0%,#26040f 60%,#3a0816 100%);background-size:170px 110px,230px 140px,190px 120px,100% 100%,100% 100%}
.bnr[data-bn=bx_euph] .bnr-a{inset:-10% -20%;background:repeating-linear-gradient(90deg,rgba(255,40,80,0) 0 16px,rgba(255,70,100,.6) 16px 21px,rgba(255,40,80,0) 21px 36px),repeating-linear-gradient(90deg,rgba(255,140,60,0) 0 30px,rgba(255,150,70,.35) 30px 34px,rgba(255,140,60,0) 34px 60px);
  -webkit-mask:radial-gradient(60% 45% at 50% 40%,#000 0%,transparent 100%);mask:radial-gradient(60% 45% at 50% 40%,#000 0%,transparent 100%);filter:blur(3px);animation:bxCurt 8s ease-in-out infinite alternate}
@keyframes bxCurt{from{transform:translateX(-4%) skewX(6deg)}to{transform:translateX(4%) skewX(-10deg)}}
.bnr[data-bn=bx_euph] .bnr-b{background:radial-gradient(30% 40% at 30% 30%,rgba(255,45,154,.45),transparent 70%),radial-gradient(30% 40% at 72% 36%,rgba(255,110,40,.4),transparent 70%);animation:bnDrift 7s ease-in-out infinite alternate}
.bnr[data-bn=bx_ocean]{background:radial-gradient(60% 50% at 50% -10%,rgba(150,235,255,.55),transparent 60%),linear-gradient(180deg,#0e6a8f 0%,#063a58 45%,#021624 100%)}
.bnr[data-bn=bx_ocean] .bnr-a{inset:0 -30%;background:repeating-linear-gradient(105deg,transparent 0 46px,rgba(200,245,255,.16) 46px 80px,transparent 80px 150px);-webkit-mask:linear-gradient(180deg,#000,transparent 85%);mask:linear-gradient(180deg,#000,transparent 85%);animation:bxRays 9s ease-in-out infinite alternate}
@keyframes bxRays{to{transform:translateX(8%)}}
.bnr[data-bn=bx_ocean] .bnr-b{inset:-20%;background:radial-gradient(closest-side,transparent 70%,rgba(200,250,255,.22) 82%,transparent 96%);background-size:70px 46px;opacity:.5;-webkit-mask:linear-gradient(180deg,#000,transparent 70%);mask:linear-gradient(180deg,#000,transparent 70%);animation:bxCaus 6s linear infinite}
@keyframes bxCaus{to{transform:translate(70px,46px)}}
.bnr[data-bn=bx_sakura]{background:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 400 200' preserveAspectRatio='xMinYMin slice'%3E%3Cpath d='M-10 30 C60 40 110 20 160 46 C190 62 230 58 270 70 M110 30 C120 10 140 0 150 -10 M190 60 C200 80 230 96 250 104' fill='none' stroke='%23250818' stroke-width='7' stroke-linecap='round'/%3E%3Cg fill='%23ffc0d5'%3E%3Ccircle cx='60' cy='36' r='9'/%3E%3Ccircle cx='72' cy='28' r='7'/%3E%3Ccircle cx='120' cy='26' r='10'/%3E%3Ccircle cx='140' cy='6' r='8'/%3E%3Ccircle cx='160' cy='44' r='9'/%3E%3Ccircle cx='200' cy='64' r='8'/%3E%3Ccircle cx='232' cy='92' r='9'/%3E%3Ccircle cx='250' cy='104' r='7'/%3E%3Ccircle cx='272' cy='70' r='8'/%3E%3C/g%3E%3Cg fill='%23ffe6ef'%3E%3Ccircle cx='66' cy='30' r='4'/%3E%3Ccircle cx='126' cy='20' r='5'/%3E%3Ccircle cx='166' cy='40' r='4'/%3E%3Ccircle cx='238' cy='88' r='4'/%3E%3C/g%3E%3C/svg%3E") left top/120% auto no-repeat,
  radial-gradient(circle at 80% 32%,#fff7fa 0 7%,rgba(255,214,230,.75) 9%,rgba(255,180,210,.25) 18%,transparent 32%),linear-gradient(180deg,#2a0c33 0%,#7a2a62 48%,#e07aa4 82%,#ffc2d6 100%)}
.bnr[data-bn=bx_glass]{background:linear-gradient(135deg,#1a1236,#24183d)}
.bnr[data-bn=bx_glass] .bnr-a{inset:-20%;background:radial-gradient(22% 30% at 25% 40%,#ff9ad5,transparent 70%),radial-gradient(24% 34% at 62% 30%,#7ab8ff,transparent 70%),radial-gradient(20% 30% at 80% 70%,#8af5c8,transparent 70%),radial-gradient(18% 26% at 40% 75%,#ffe08a,transparent 70%);filter:blur(16px);animation:bxBlob 12s ease-in-out infinite alternate}
@keyframes bxBlob{to{transform:translate(6%,-5%) rotate(10deg) scale(1.08)}}
.bnr[data-bn=bx_glass] .bnr-b{background:linear-gradient(115deg,transparent 30%,rgba(255,255,255,.18) 42%,transparent 52%),repeating-linear-gradient(90deg,rgba(255,255,255,.07) 0 1px,transparent 1px 90px),rgba(255,255,255,.06)}
.bnr[data-bn=bx_ff]{background:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='160' height='70'%3E%3Cpath d='M0 70 L12 30 L24 70 Z M18 70 L34 14 L50 70 Z M44 70 L56 38 L68 70 Z M62 70 L80 6 L98 70 Z M92 70 L104 34 L116 70 Z M110 70 L126 18 L142 70 Z M136 70 L148 36 L160 70 Z' fill='%23020a07'/%3E%3C/svg%3E") bottom/160px 70px repeat-x,
  radial-gradient(circle at 76% 26%,#fbffe8 0 6%,rgba(220,255,190,.5) 9%,rgba(160,255,150,.12) 18%,transparent 28%),linear-gradient(180deg,#0b2a20 0%,#0f3a2c 55%,#05150f 100%)}
.bnr[data-bn=bx_ff] .bnr-a{background:linear-gradient(0deg,transparent 0 18%,rgba(150,230,190,.16) 30%,transparent 46%);animation:bxFog 10s ease-in-out infinite alternate}
@keyframes bxFog{to{transform:translateX(-6%) scaleY(1.2)}}
.bnr[data-bn=bx_ff] .bnr-b{background:radial-gradient(circle at 20% 50%,#f4ffb0 0 1.6px,rgba(220,255,120,.4) 2.5px,transparent 8px),radial-gradient(circle at 60% 66%,#f4ffb0 0 1.4px,rgba(220,255,120,.35) 2.5px,transparent 7px),radial-gradient(circle at 84% 44%,#f4ffb0 0 1.6px,rgba(220,255,120,.4) 2.5px,transparent 8px);background-size:150px 110px,190px 120px,170px 100px;animation:bxFf 3s ease-in-out infinite alternate}
@keyframes bxFf{0%{opacity:.4}100%{opacity:1}}
.bnr[data-bn=bx_amb]{background:#120a1e}
.bnr[data-bn=bx_amb] .bnr-a{inset:-30%;background:radial-gradient(28% 36% at 30% 40%,#a78bfa,transparent 70%),radial-gradient(30% 36% at 70% 35%,#fb923c,transparent 70%),radial-gradient(26% 34% at 55% 75%,#38bdf8,transparent 70%),radial-gradient(22% 30% at 20% 80%,#f472b6,transparent 70%);filter:blur(22px);animation:bxAmb 14s ease-in-out infinite alternate}
@keyframes bxAmb{50%{transform:rotate(18deg) scale(1.1)}100%{transform:rotate(-10deg) translate(4%,3%)}}
.bnr[data-bn=bx_amb] .bnr-b{background:radial-gradient(120% 90% at 50% 0%,transparent 40%,rgba(0,0,0,.45))}
/* ---------- ники коллекций ---------- */
.mnm[data-nm=nx_euph]{background:linear-gradient(90deg,#ff2d55,#ff8a3d,#ff2d9a,#ffd36b,#ff2d55);background-size:220% 100%;-webkit-background-clip:text;background-clip:text;color:transparent;animation:nmSheen 4s linear infinite;filter:drop-shadow(0 0 12px rgba(255,45,85,.55))}
.mnm[data-nm=nx_ocean]{background:linear-gradient(100deg,#2cc6f0 0%,#bff4ff 35%,#ffffff 50%,#9be9fb 65%,#2cc6f0 100%);background-size:250% 100%;-webkit-background-clip:text;background-clip:text;color:transparent;animation:nmSheen 5s linear infinite;filter:drop-shadow(0 0 12px rgba(44,198,240,.5))}
.mnm[data-nm=nx_sakura]{background:linear-gradient(180deg,#ffffff 10%,#ffd6e6 50%,#ff6f9f 100%);-webkit-background-clip:text;background-clip:text;color:transparent;filter:drop-shadow(0 0 14px rgba(255,111,159,.55))}
.mnm[data-nm=nx_glass]{background:linear-gradient(110deg,#ffffff 0%,#d6ecff 25%,#ffe0f4 50%,#e0fff2 75%,#ffffff 100%);background-size:250% 100%;-webkit-background-clip:text;background-clip:text;color:transparent;animation:nmSheen 6s linear infinite;filter:drop-shadow(0 2px 10px rgba(200,220,255,.45))}
.mnm[data-nm=nx_ff]{background:linear-gradient(180deg,#fbffd0 0%,#c8ff6a 50%,#3fe0a0 100%);-webkit-background-clip:text;background-clip:text;color:transparent;filter:drop-shadow(0 0 12px rgba(200,255,110,.5));animation:nmFf 2.6s ease-in-out infinite}
@keyframes nmFf{50%{opacity:.8}}
.mnm[data-nm=nx_amb]{color:#fff;letter-spacing:.005em;text-shadow:0 6px 26px rgba(167,139,250,.7),0 2px 6px rgba(0,0,0,.5)}
/* ---------- кровавый ник (только админ) ---------- */
.mnm[data-nm=n_blood]{overflow:visible;padding-bottom:.4em;margin-bottom:-.4em}
.mnm[data-nm=n_blood] .bl{position:relative;display:inline-block;background:linear-gradient(180deg,#ff4545 0%,#d6001c 36%,#8e0012 70%,#4a0008 100%);-webkit-background-clip:text;background-clip:text;color:transparent;
  filter:drop-shadow(0 0 9px rgba(210,0,26,.5)) drop-shadow(0 2px 0 #2a0004)}
.mnm[data-nm=n_blood] .bl.sp{width:.26em}
.mnm[data-nm=n_blood] .bl i{position:absolute;left:var(--dx);top:.74em;width:.1em;height:.1em;margin-left:-.05em;border-radius:.03em .03em .06em .06em;background:linear-gradient(90deg,#9a0012,#ff3030 45%,#b0001a);box-shadow:0 0 6px rgba(220,0,30,.6);
  animation:drip 5.6s cubic-bezier(.5,0,.6,1) infinite;animation-delay:var(--dd)}
.mnm[data-nm=n_blood] .bl i::after{content:"";position:absolute;left:50%;bottom:-.07em;width:.15em;height:.17em;margin-left:-.075em;border-radius:50% 50% 50% 50%/60% 60% 40% 40%;
  background:radial-gradient(circle at 35% 35%,#ff6060,#b8001a 60%,#5a0008);animation:drop 5.6s cubic-bezier(.5,0,.8,.6) infinite;animation-delay:var(--dd)}
@keyframes drip{0%{height:.1em}55%,70%{height:calc(var(--dh) + .1em)}100%{height:.1em}}
@keyframes drop{0%,62%{transform:translateY(0);opacity:1}84%{transform:translateY(.8em) scale(.85,1.1);opacity:.9}85%,100%{transform:translateY(.8em);opacity:0}}
.admpf{display:inline-block;flex:none;margin-right:.35em;padding:.16em .42em .14em;border-radius:.3em;font-size:.34em;font-weight:900;letter-spacing:.08em;line-height:1.1;vertical-align:.55em;color:#fff;-webkit-text-fill-color:#fff;
  background:linear-gradient(135deg,#ff2d2d,#8a0010);box-shadow:0 0 0 1px rgba(255,120,120,.5),0 4px 16px rgba(210,0,26,.55);text-shadow:0 1px 2px rgba(0,0,0,.4)}
.admpf.sm{font-size:10px;vertical-align:1px;margin-left:6px}
.mbd.ex{box-shadow:inset 0 0 0 1.5px rgba(255,255,255,.45),0 0 14px -2px var(--b1)}
/* ---------- описание в профиле ---------- */
.pfbio{position:relative;display:inline-block;max-width:620px;margin:12px 0 0;padding:11px 18px 12px 44px;border-radius:16px;font-size:15.5px;font-weight:600;line-height:1.45;color:#fff;font-style:normal;
  background:linear-gradient(135deg,rgba(14,8,12,.82),rgba(14,8,12,.66));box-shadow:inset 0 0 0 1px rgba(255,255,255,.1),inset 3px 0 0 var(--g2),0 10px 30px -12px rgba(0,0,0,.7)}
.pfbio::before{content:"“";position:absolute;left:14px;top:6px;font:900 38px/1 Georgia,"Times New Roman",serif;color:var(--g2);text-shadow:0 0 14px rgba(var(--ar),.6)}
.pfbio.emp{color:rgba(255,255,255,.6);font-weight:500;cursor:pointer}
.pfbio.emp:hover{color:#fff}
.pfnm{filter:drop-shadow(0 3px 14px rgba(0,0,0,.55))}
.pfact .gbtn .ico{width:16px;height:16px}
/* ---------- любимое ---------- */
.pff.pick{cursor:pointer;transition:background .25s,box-shadow .25s,transform .25s var(--ease)}
.pff.pick:hover{background:rgba(255,255,255,.08);box-shadow:inset 0 0 0 1px rgba(var(--ar),.45);transform:translateY(-2px)}
.pff>em .ico{display:inline-block;width:12px;height:12px;vertical-align:-1px;margin-left:4px;opacity:.6}
.pffr.emp b{color:var(--g3)}
.pfplus{flex:none;width:46px;height:46px;border-radius:12px;display:grid;place-items:center;font-size:24px;font-weight:400;color:var(--g3);border:1.5px dashed rgba(var(--ar),.55)}
#fpk{position:fixed;inset:0;z-index:9500;display:grid;place-items:center;padding:20px;background:rgba(6,2,6,.6);animation:gfade .22s ease}
#fpk.out{animation:gfadeOut .22s ease forwards}
.fpc{width:min(520px,100%);max-height:min(640px,86vh);display:flex;flex-direction:column;gap:12px;padding:18px;border-radius:22px;background:linear-gradient(180deg,#22121a,#150b10);box-shadow:inset 0 0 0 1px rgba(255,255,255,.1),0 30px 80px rgba(0,0,0,.7);animation:rwIn .4s var(--ease)}
.fph{display:flex;align-items:center;justify-content:space-between}
.fph b{font-size:22px;font-weight:900;letter-spacing:-.02em}
.fps{display:flex;align-items:center;gap:10px;padding:0 14px;border-radius:12px;background:rgba(255,255,255,.07);box-shadow:inset 0 0 0 1px rgba(255,255,255,.08)}
.fps svg{width:18px;height:18px;opacity:.6}
.fps input{flex:1;height:42px;border:0;outline:0;background:none;color:#fff;font:600 15px inherit}
.fpl{flex:1;min-height:0;overflow:auto;display:flex;flex-direction:column;gap:4px;margin:0 -6px;padding:0 6px}
.fpr{display:flex;align-items:center;gap:12px;width:100%;padding:8px 10px;border-radius:12px;text-align:left;transition:background .2s}
.fpr:hover{background:rgba(255,255,255,.07)}
.fpr.on{background:rgba(var(--ar),.18);box-shadow:inset 0 0 0 1px rgba(var(--ar),.5)}
.fpr .cv,.fpr .av{flex:none;width:44px;height:44px;border-radius:10px;font-size:18px}
.fpr .av{border-radius:50%}
.fpr .tx{flex:1;min-width:0}
.fpr b,.fpr span{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.fpr b{font-size:15px}.fpr span{font-size:12px;color:var(--sub)}
.fpr i{font-style:normal;font-weight:900;font-size:18px}
.fpe{padding:30px;text-align:center;color:var(--sub)}
.fpclr{align-self:flex-start;padding:9px 18px;font-size:13px}
/* ---------- карточки предметов: премиальный вид ---------- */
.igrid{grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:14px}
.itm{gap:0;padding:0;border-radius:18px;overflow:hidden;background:linear-gradient(180deg,#1d1418,#140e11);
  box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--rc) 22%,rgba(255,255,255,.06)),0 10px 24px -16px rgba(0,0,0,.8)}
.itm::before{left:0;right:0;top:auto;bottom:0;height:3px;border-radius:0;opacity:.9;box-shadow:0 0 16px var(--rc);z-index:2}
.itm::after{content:"";position:absolute;inset:0;pointer-events:none;background:linear-gradient(115deg,transparent 40%,rgba(255,255,255,.1) 50%,transparent 60%);background-size:260% 100%;background-position:130% 0;transition:background-position .9s var(--ease)}
.itm:hover{transform:translateY(-4px);box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--rc) 60%,transparent),0 18px 40px -16px color-mix(in srgb,var(--rc) 75%,transparent)}
.itm:hover::after{background-position:-30% 0}
.itm.on{box-shadow:inset 0 0 0 2px var(--g2),0 14px 34px -16px rgba(var(--ar),.8)}
.ipv{height:150px;border-radius:0;background:radial-gradient(70% 70% at 50% 42%,color-mix(in srgb,var(--rc) 30%,transparent),transparent 72%),
  repeating-linear-gradient(135deg,rgba(255,255,255,.018) 0 2px,transparent 2px 9px),linear-gradient(180deg,#24191e,#170f13)}
.itm.sm .ipv{height:128px}
.ipv.avp{padding-top:22px}
.ipv.nmp{font-size:24px}
.ipv.bdp .mbd.big{width:70px;height:70px}
.itx{padding:12px 14px 4px}
.itx em{color:var(--rc)!important;font-size:10px;letter-spacing:.12em}
.itx b{font-size:15.5px;margin-top:3px}
.itb{margin:10px 12px 14px;height:38px;border-radius:12px}
.itb.buy{background:linear-gradient(180deg,rgba(255,215,106,.16),rgba(233,162,58,.08));color:#ffe3a3;box-shadow:inset 0 0 0 1px rgba(255,200,90,.45)}
.itb.buy:hover{background:linear-gradient(180deg,#ffd76a,#e9a23a);color:#2a1500;box-shadow:0 8px 20px -8px rgba(255,170,40,.8)}
.itb.poor{filter:none;opacity:.55}
.itm.r-l{background:linear-gradient(180deg,#2a1f10,#140e09)}
.itm.r-l .ipv{background:radial-gradient(70% 70% at 50% 42%,rgba(255,194,63,.35),transparent 72%),repeating-conic-gradient(from 0deg at 50% 45%,rgba(255,210,110,.06) 0 8deg,transparent 8deg 20deg),linear-gradient(180deg,#2c2012,#16100a)}
.itm.r-l::before{background:linear-gradient(90deg,#a8650c,#ffe9a8,#d99a1c,#fff3c4,#a8650c);background-size:200% 100%;animation:nmSheen 3s linear infinite}
.itm.r-e .ipv{background:radial-gradient(70% 70% at 50% 42%,rgba(181,107,255,.32),transparent 72%),linear-gradient(180deg,#22172c,#140e18)}
.itm.r-x .ipv{background:radial-gradient(70% 70% at 50% 42%,color-mix(in srgb,var(--rc) 40%,transparent),transparent 72%),linear-gradient(180deg,#1f1418,#120c0f)}
/* ---------- коллекции ---------- */
.kgrid2{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:18px;margin:4px 0 34px}
.kit2{position:relative;display:flex;flex-direction:column;border-radius:24px;overflow:hidden;isolation:isolate;
  background:linear-gradient(180deg,#1c1217,#120b0e) padding-box,linear-gradient(140deg,var(--k1),transparent 40%,transparent 60%,var(--k2)) border-box;border:1.5px solid transparent;
  box-shadow:0 20px 50px -26px var(--k1);transition:transform .45s var(--ease),box-shadow .45s;animation:pgItem .6s var(--ease) backwards;animation-delay:calc(var(--i) * 60ms)}
.kit2:hover{transform:translateY(-6px);box-shadow:0 30px 70px -24px var(--k1)}
.kit2-art{position:relative;height:250px}
.bnr.kb{position:absolute;inset:0}
.bnr.kb::after{content:"";position:absolute;inset:0;z-index:3;background:linear-gradient(180deg,transparent 40%,rgba(18,11,14,.92) 100%);pointer-events:none}
.kit2-art::after{content:"";position:absolute;inset:0;z-index:5;pointer-events:none;background:linear-gradient(115deg,transparent 35%,rgba(255,255,255,.16) 50%,transparent 65%);background-size:260% 100%;background-position:130% 0;transition:background-position 1.1s var(--ease)}
.kit2:hover .kit2-art::after{background-position:-30% 0}
.kit2-hero{position:absolute;left:0;right:0;bottom:16px;z-index:4;display:flex;flex-direction:column;align-items:center;gap:12px}
.kit2-nm{display:flex;align-items:center;max-width:92%;font-size:24px;font-weight:900;letter-spacing:-.02em;filter:drop-shadow(0 3px 10px rgba(0,0,0,.6))}
.kit2-tag{position:absolute;left:14px;top:14px;z-index:5;padding:6px 12px;border-radius:999px;font-size:10.5px;font-weight:800;letter-spacing:.12em;text-transform:uppercase;color:#fff;
  background:rgba(0,0,0,.5);box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--k1) 70%,transparent),0 0 20px -6px var(--k1)}
.kit2-b{display:flex;flex-direction:column;gap:14px;padding:6px 20px 20px}
.kit2-t b{display:block;font-size:26px;font-weight:900;letter-spacing:-.03em;background:linear-gradient(90deg,#fff,var(--k2));-webkit-background-clip:text;background-clip:text;color:transparent}
.kit2-t span{display:block;margin-top:4px;font-size:13.5px;line-height:1.4;color:var(--sub)}
.kit2-c{display:flex;flex-wrap:wrap;gap:6px}
.kc{display:inline-flex;align-items:center;gap:6px;padding:5px 10px 5px 8px;border-radius:999px;font-size:12px;font-weight:700;color:rgba(255,255,255,.85);background:rgba(255,255,255,.06);box-shadow:inset 0 0 0 1px rgba(255,255,255,.08)}
.kc svg{width:14px;height:14px;color:var(--k1)}
.kit2-buy{display:flex;align-items:center;justify-content:space-between;gap:12px;height:54px;padding:0 10px 0 20px;border-radius:16px;font-weight:800;font-size:15px;color:#fff;
  background:linear-gradient(135deg,color-mix(in srgb,var(--k1) 85%,#000),color-mix(in srgb,var(--k2) 70%,#000));box-shadow:0 12px 30px -14px var(--k1),inset 0 1px 0 rgba(255,255,255,.25);transition:transform .25s var(--ease),filter .25s}
.kit2-buy:hover{transform:translateY(-2px);filter:brightness(1.12)}
.kit2-buy:active{transform:scale(.98)}
.kit2-buy b{display:flex;align-items:center;gap:8px;height:38px;padding:0 14px;border-radius:12px;background:rgba(0,0,0,.35);color:#ffe3a3;font-size:18px;font-variant-numeric:tabular-nums}
.kit2-buy .coin{width:22px;height:22px;vertical-align:0}
.kit2-buy.poor{filter:saturate(.6) brightness(.85)}
.kit2-buy.shake{animation:shake .4s}
.kit2-btns{display:flex;gap:8px}
.kit2-btns .itb{flex:1;margin:0;height:46px}
.itb.ghost{background:rgba(255,255,255,.06);box-shadow:inset 0 0 0 1px rgba(255,255,255,.12)}
.kit2.own{background:linear-gradient(180deg,#1c1217,#120b0e) padding-box,linear-gradient(140deg,var(--k1),var(--k2)) border-box}
/* ---------- облегчённая графика (включается сама, если компьютер не тянет) ---------- */
.lowfx .bnr>i,.lowfx .mav-sp,.lowfx .itm.r-l::before{animation:none!important}
.lowfx .bnr[data-bn] .bnr-a{filter:none}
html.lowfx[data-design=glass] .side .panel,html.lowfx .pm{-webkit-backdrop-filter:none!important;backdrop-filter:none!important}
.lowfx .itm::after,.lowfx .kit2-art::after{display:none}
/* ---------- звёздный ник (админ 7jade): звёзды летают вокруг, цвета берутся из темы ---------- */
.mnm[data-nm=n_stars]{overflow:visible;isolation:isolate}
.mnm[data-nm=n_stars] .st{position:relative;z-index:1;background:linear-gradient(100deg,var(--g4) 0%,#ffffff 30%,var(--g3) 50%,#ffffff 70%,var(--g2) 100%);background-size:250% 100%;-webkit-background-clip:text;background-clip:text;color:transparent;
  animation:nmSheen 5s linear infinite;filter:drop-shadow(0 0 12px rgba(var(--ar),.65))}
.mnm[data-nm=n_stars] .spx{position:absolute;inset:-.35em -.4em;pointer-events:none;z-index:2}
.mnm[data-nm=n_stars] .spx i{position:absolute;width:var(--sz);height:var(--sz);margin:calc(var(--sz) / -2) 0 0 calc(var(--sz) / -2);
  background:var(--g3);clip-path:polygon(50% 0,61% 39%,100% 50%,61% 61%,50% 100%,39% 61%,0 50%,39% 39%);filter:drop-shadow(0 0 4px var(--g2));
  animation:spxFly var(--sd) ease-in-out infinite;animation-delay:var(--sl);will-change:transform,opacity}
.mnm[data-nm=n_stars] .spx i:nth-child(3n){background:#fff}
.mnm[data-nm=n_stars] .spx i:nth-child(3n+1){background:var(--g4)}
.mnm[data-nm=n_stars] .spx i.dot{clip-path:circle(50%);width:calc(var(--sz) * .55);height:calc(var(--sz) * .55);background:var(--g2)}
@keyframes spxFly{
  0%{transform:translate(0,0) scale(0) rotate(0deg);opacity:0}
  15%{opacity:1}
  30%{transform:translate(var(--s1x),var(--s1y)) scale(1.1) rotate(120deg)}
  60%{transform:translate(var(--s2x),var(--s2y)) scale(.8) rotate(240deg);opacity:.9}
  85%{opacity:.7}
  100%{transform:translate(var(--s3x),var(--s3y)) scale(0) rotate(360deg);opacity:0}}
html.lowfx .mnm[data-nm=n_stars] .spx i:nth-child(n+9){display:none}
/* ---------- любимое: до 3 ---------- */
.pff{display:flex;flex-direction:column;gap:8px}
.pff>em{margin-bottom:2px}
.pfno{flex:none;width:18px;text-align:center;font-size:13px;font-weight:900;color:var(--g3)}
.pffr .cv,.pffr .av,.pfg,.pfplus{width:40px;height:40px}
.pffr.emp{opacity:.85}
.fph>div span{display:block;margin-top:2px;font-size:12.5px;color:var(--sub)}
.fpn{flex:none;width:22px;height:22px;border-radius:50%;display:grid;place-items:center;font-size:12px;font-weight:900;box-shadow:inset 0 0 0 1.5px rgba(255,255,255,.2);color:#fff}
.fpr.on .fpn{background:var(--g2);box-shadow:none}
.fpr.shake{animation:shake .4s}
.fpf{display:flex;align-items:center;gap:8px}
.fpf>span{flex:1;font-size:13px;color:var(--sub)}
.fpf>span b{color:#fff}
.fpf .gbtn{padding:9px 18px;font-size:14px}
/* ---------- убраны плашки «Исполнители / Жанры» в медиатеке ---------- */
#cats,#catWrap{display:none!important}
.pffr>div:not(.cv):not(.pfg):not(.av){flex:1;min-width:0}
.pffr .cv,.pffr .av,.pfg,.pfplus{flex:none!important}
.fpn{display:block;line-height:22px;text-align:center}
.mnm[data-nm=n_stars] .spx i{filter:drop-shadow(0 0 5px var(--g2)) drop-shadow(0 0 2px #fff)}
/* ===================== v4: новые предметы, редкость, уровни ===================== */
.mav-h .hneon{animation:nmNeon 3s ease-in-out infinite}
/* рамки */
.mav[data-fr=f_candy] .mav-r{background:repeating-conic-gradient(from 0deg,#fff 0 12deg,#ff3f6e 12deg 24deg);--rw:13%;animation:frSpin 9s linear infinite}
.mav[data-fr=f_candy]{--fgc:rgba(255,63,110,.35)}
.mav[data-fr=f_matrix] .mav-r{background:repeating-conic-gradient(from 0deg,#38ff8a 0 2deg,transparent 2deg 5deg,#0f7a3a 5deg 7deg,transparent 7deg 11deg),#04140a;--rw:13%;animation:frSpin 16s steps(48) infinite}
.mav[data-fr=f_matrix]{--fgc:rgba(56,255,138,.45)}
.mav[data-fr=f_aurora] .mav-r{background:conic-gradient(from 0deg,#3cffaa,#3cc8ff,#8c5aff,#ff5ad8,#3cffaa);--rw:14%;animation:frSpin 6s linear infinite}
.mav[data-fr=f_aurora]{--fgc:rgba(60,255,170,.55)}
.mav[data-fr=f_aurora] .mav-g{animation:frBreath 3s ease-in-out infinite}
.mav[data-fr=f_obsidian] .mav-r{background:conic-gradient(from 210deg,#0b0b0e,#3a3a44 18%,#0b0b0e 30%,#d6b578 34%,#0b0b0e 38%,#2a2a32 60%,#0b0b0e 75%,#ffe9a8 80%,#0b0b0e 85%);--rw:15%;animation:frSpin 12s linear infinite}
.mav[data-fr=f_obsidian]{--fgc:rgba(214,181,120,.35)}
.mav[data-fr=f_lava] .mav-r{background:radial-gradient(circle at 30% 20%,#ffb03b 0 4%,transparent 5%),radial-gradient(circle at 80% 70%,#ff5a1e 0 5%,transparent 6%),repeating-conic-gradient(from 0deg,#2a0603 0 14deg,#ff4d1e 15deg 17deg,#2a0603 18deg 30deg);--rw:15%;animation:frSpin 18s linear infinite}
.mav[data-fr=f_lava]{--fgc:rgba(255,90,30,.7)}
.mav[data-fr=f_lava] .mav-g{animation:frFlick .3s ease-in-out infinite alternate}
/* фоны */
.bnr[data-bn=b_hearts]{background:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='60' height='60'%3E%3Cpath d='M15 22c-4-6 4-10 7-4 3-6 11-2 7 4l-7 7z' fill='%23ffffff' opacity='.3'/%3E%3Cpath d='M44 50c-3-4 3-7 5-3 2-4 8-1 5 3l-5 5z' fill='%23ffffff' opacity='.22'/%3E%3C/svg%3E"),linear-gradient(160deg,#ff8fb4,#e0558a 55%,#8a1a5c)}
.bnr[data-bn=b_desert]{background:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 400 120' preserveAspectRatio='none'%3E%3Cpath d='M0 70 C80 40 140 90 220 60 C300 30 350 70 400 55 V120 H0Z' fill='%23c2622a'/%3E%3Cpath d='M0 95 C90 70 170 110 260 85 C320 70 360 90 400 80 V120 H0Z' fill='%238a3a1a'/%3E%3C/svg%3E") bottom/100% 70% no-repeat,
  radial-gradient(circle at 70% 40%,#fff3c4 0 6%,rgba(255,200,120,.4) 10%,transparent 26%),linear-gradient(180deg,#3a1c5c 0%,#d76d4a 55%,#ffb36b 100%)}
.bnr[data-bn=b_mountains]{background:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 400 140' preserveAspectRatio='none'%3E%3Cpath d='M0 90 L60 40 L110 80 L170 20 L240 85 L300 35 L360 70 L400 50 V140 H0Z' fill='%23284a7a' opacity='.7'/%3E%3Cpath d='M170 20 L185 34 L176 32 L170 40 L163 31 L155 34Z' fill='%23fff' opacity='.8'/%3E%3Cpath d='M0 120 L80 70 L150 110 L230 60 L310 115 L400 80 V140 H0Z' fill='%23142a4a'/%3E%3C/svg%3E") bottom/100% 80% no-repeat,
  linear-gradient(180deg,#0c1a3a 0%,#3a5a9a 60%,#9ab8e8 100%)}
.bnr[data-bn=b_waves]{background:linear-gradient(180deg,#0a0018 0%,#2a0a4a 100%)}
.bnr[data-bn=b_waves] .bnr-a{inset:20% -10% -10%;background:repeating-linear-gradient(180deg,transparent 0 10px,rgba(255,43,214,.75) 10px 12px);-webkit-mask:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 200 100' preserveAspectRatio='none'%3E%3Cpath d='M0 40 Q25 10 50 40 T100 40 T150 40 T200 40 V100 H0Z'/%3E%3C/svg%3E") 0 0/50% 100% repeat-x;mask:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 200 100' preserveAspectRatio='none'%3E%3Cpath d='M0 40 Q25 10 50 40 T100 40 T150 40 T200 40 V100 H0Z'/%3E%3C/svg%3E") 0 0/50% 100% repeat-x;filter:drop-shadow(0 0 6px #ff2bd6);animation:bxWave 3s linear infinite}
@keyframes bxWave{to{transform:translateX(-25%)}}
.bnr[data-bn=b_waves] .bnr-b{background:radial-gradient(40% 60% at 50% 100%,rgba(0,240,255,.35),transparent 70%)}
.bnr[data-bn=b_rain]{background:linear-gradient(180deg,#05010f 0%,#1a0630 60%,#3a0a3a 100%)}
.bnr[data-bn=b_rain] .bnr-a{inset:-20% 0 0;background:repeating-linear-gradient(100deg,transparent 0 22px,rgba(0,240,255,.5) 22px 23px,transparent 23px 46px),repeating-linear-gradient(100deg,transparent 0 37px,rgba(255,43,214,.45) 37px 38px,transparent 38px 70px);
  -webkit-mask:repeating-linear-gradient(180deg,#000 0 14px,transparent 14px 40px);mask:repeating-linear-gradient(180deg,#000 0 14px,transparent 14px 40px);animation:bnRain2 .7s linear infinite}
@keyframes bnRain2{to{transform:translateY(40px)}}
.bnr[data-bn=b_rain] .bnr-b{background:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='400' height='120' preserveAspectRatio='none'%3E%3Cpath d='M0 120V80h30V60h20v30h16V50h26v40h12V70h20v50zM124 120V56h30V40h18v50h14V30h24v90zM210 120V70h22V50h26v40h14V64h20v56zM292 120V44h28v30h14V60h22v20h18V50h26v70z' fill='%2305010a'/%3E%3C/svg%3E") bottom/100% 55% no-repeat}
.bnr[data-bn=b_orion]{background:radial-gradient(1px 1px at 10% 20%,#fff,transparent),radial-gradient(1.4px 1.4px at 40% 70%,#fff,transparent),radial-gradient(1px 1px at 75% 35%,#fff,transparent),radial-gradient(1.6px 1.6px at 88% 72%,#fff,transparent),#04020c;background-size:140px 100px,210px 140px,170px 120px,240px 160px,100% 100%}
.bnr[data-bn=b_orion] .bnr-a{inset:-40%;background:radial-gradient(22% 18% at 42% 50%,rgba(255,77,109,.7),transparent 70%),radial-gradient(28% 22% at 58% 46%,rgba(255,176,59,.45),transparent 70%),radial-gradient(30% 26% at 50% 56%,rgba(80,120,255,.6),transparent 70%),radial-gradient(18% 14% at 46% 44%,rgba(255,255,255,.55),transparent 70%);filter:blur(14px);animation:frSpin 50s linear infinite}
.bnr[data-bn=b_orion] .bnr-b{background:radial-gradient(circle at 30% 30%,#fff 0 1.2px,rgba(255,255,255,.4) 2px,transparent 6px),radial-gradient(circle at 70% 60%,#fff 0 1.2px,rgba(255,200,220,.4) 2px,transparent 6px);background-size:120px 90px,170px 110px;animation:bxFf 2.2s ease-in-out infinite alternate}
/* ники */
.mnm[data-nm=n_candy]{background:repeating-linear-gradient(120deg,#fff 0 .18em,#ff5f93 .18em .36em);-webkit-background-clip:text;background-clip:text;color:transparent;filter:drop-shadow(0 2px 8px rgba(255,95,147,.5))}
.mnm[data-nm=n_outline]{color:transparent;-webkit-text-stroke:1.5px #7af0ff;filter:drop-shadow(0 0 8px rgba(122,240,255,.8))}
.mnm[data-nm=n_cyber]{color:#e6fbff;text-shadow:-2px 0 0 rgba(0,240,255,.9),2px 0 0 rgba(255,43,214,.9),0 0 18px rgba(0,240,255,.5)}
.mnm[data-nm=n_aurora]{background:linear-gradient(90deg,#3cffaa,#3cc8ff,#8c5aff,#ff5ad8,#3cffaa);background-size:300% 100%;-webkit-background-clip:text;background-clip:text;color:transparent;animation:nmSheen 6s linear infinite;filter:drop-shadow(0 0 12px rgba(60,255,170,.4))}
.mnm[data-nm=n_mythic]{background:linear-gradient(90deg,#ff4d6d,#ffb03b,#ffe83b,#3bff7a,#3bd5ff,#a35bff,#ff3bd5,#ff4d6d);background-size:200% 100%;-webkit-background-clip:text;background-clip:text;color:transparent;animation:nmSheen 2.4s linear infinite;
  filter:drop-shadow(0 0 6px rgba(255,255,255,.6)) drop-shadow(0 0 16px rgba(255,77,109,.6))}
/* редкость */
.rch{position:absolute;left:10px;top:10px;z-index:3;display:flex;align-items:center;gap:5px;padding:4px 9px 4px 7px;border-radius:999px;font-size:10px;font-weight:900;letter-spacing:.1em;text-transform:uppercase;color:var(--rc);
  background:rgba(10,6,8,.72);box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--rc) 60%,transparent),0 0 14px -4px var(--rc)}
.rch svg{width:12px;height:12px}
.itm{position:relative}
.itm.r-m{background:linear-gradient(180deg,#2a1018,#140a0d)}
.itm.r-m .ipv{background:radial-gradient(70% 70% at 50% 42%,rgba(255,77,109,.38),transparent 72%),repeating-conic-gradient(from 0deg at 50% 45%,rgba(255,120,150,.07) 0 6deg,transparent 6deg 15deg),linear-gradient(180deg,#2c1218,#16090d)}
.itm.r-m::before{background:linear-gradient(90deg,#ff4d6d,#ffb03b,#ffe83b,#3bff7a,#3bd5ff,#a35bff,#ff3bd5,#ff4d6d);background-size:200% 100%;animation:nmSheen 2s linear infinite;height:4px}
.itm.r-m .rch{color:#fff;background:linear-gradient(90deg,#ff4d6d,#a35bff);box-shadow:0 0 16px -2px #ff4d6d}
.itm.r-l .rch{color:#2a1500;background:linear-gradient(90deg,#ffe9a8,#ffc23f)}
.rfl{display:flex;flex-wrap:wrap;gap:6px;margin:-4px 0 14px}
.rfb{display:flex;align-items:center;gap:6px;height:30px;padding:0 12px;border-radius:999px;font-size:12px;font-weight:800;color:var(--rc);background:rgba(255,255,255,.04);box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--rc) 35%,transparent);transition:background .2s,color .2s}
.rfb svg{width:12px;height:12px}
.rfb em{font-style:normal;opacity:.55;font-weight:700}
.rfb:hover{background:color-mix(in srgb,var(--rc) 14%,transparent)}
.rfb.on{background:var(--rc);color:#120a0d;box-shadow:0 0 18px -6px var(--rc)}
/* уровни и опыт */
.meb{position:relative}
.meb .lvs{position:absolute;right:-4px;bottom:-3px;z-index:3;min-width:17px;height:17px;padding:0 4px;border-radius:999px;display:grid;place-items:center;font-size:10px;font-weight:900;color:#fff;
  background:linear-gradient(135deg,var(--g3),var(--g0));box-shadow:0 0 0 2px var(--bg),0 2px 8px rgba(0,0,0,.5)}
.meb.lvup .lvs{animation:balBump .6s var(--ease)}
.pflv{position:absolute;left:4px;bottom:6px;z-index:3;min-width:44px;height:44px;padding:0 8px;border-radius:999px;display:grid;place-items:center;font-size:20px;font-weight:900;color:#fff;
  background:linear-gradient(135deg,var(--g3),var(--g0));box-shadow:0 0 0 4px var(--bg),0 6px 18px rgba(var(--ar),.55)}
.xpb{display:flex;align-items:center;gap:10px;max-width:520px;margin-top:10px}
.xpl{flex:none;font-size:12px;font-weight:900;letter-spacing:.08em;text-transform:uppercase;color:var(--g3)}
.xpt{position:relative;flex:1;height:8px;border-radius:8px;background:rgba(255,255,255,.1);overflow:hidden;box-shadow:inset 0 1px 2px rgba(0,0,0,.4)}
.xpt i{position:absolute;left:0;top:0;bottom:0;border-radius:8px;background:linear-gradient(90deg,var(--g0),var(--g2),var(--g4));box-shadow:0 0 12px rgba(var(--ar),.7);transition:width .8s var(--ease)}
.xpt i::after{content:"";position:absolute;inset:0;background:linear-gradient(90deg,transparent,rgba(255,255,255,.5),transparent);background-size:50% 100%;background-repeat:no-repeat;animation:xpShine 2.4s ease-in-out infinite}
@keyframes xpShine{from{background-position:-60% 0}to{background-position:160% 0}}
.xpb em{flex:none;font-style:normal;font-size:12px;font-weight:700;color:var(--sub);font-variant-numeric:tabular-nums}
.flyx{position:fixed;z-index:10050;pointer-events:none;font:900 22px/1 "Segoe UI Variable Display","Segoe UI",system-ui,sans-serif;color:var(--g4);text-shadow:0 0 14px rgba(var(--ar),.8),0 2px 3px rgba(0,0,0,.6);white-space:nowrap}
.rwlv{display:none}
#rwd.lv .rwcoin .coin{display:none}
#rwd.lv .rwlv{position:relative;display:grid;place-items:center;width:120px;height:120px;border-radius:50%;margin:0 auto;font:900 54px/1 "Segoe UI Variable Display","Segoe UI",system-ui,sans-serif;color:#fff;
  background:radial-gradient(circle at 35% 30%,var(--g4),var(--g2) 45%,var(--g0));box-shadow:0 0 0 6px rgba(255,255,255,.12),0 14px 40px rgba(var(--ar),.6);animation:rwSpin 2.2s cubic-bezier(.4,0,.2,1) .15s both}
#rwd.lv .rwcoin::after{content:"УРОВЕНЬ";position:absolute;left:50%;bottom:-8px;transform:translateX(-50%);padding:3px 10px;border-radius:999px;background:#140a0d;font-size:11px;font-weight:900;letter-spacing:.14em;color:var(--g3);box-shadow:inset 0 0 0 1px rgba(var(--ar),.5)}
/* ---------- новая оценка: выбор источника ---------- */
#navCat{display:none!important}
#nwk{position:fixed;inset:0;z-index:9500;display:grid;place-items:center;padding:20px;background:rgba(6,2,6,.62);animation:gfade .22s ease}
#nwk.out{animation:gfadeOut .22s ease forwards}
.nwc{width:min(760px,100%);padding:26px;border-radius:26px;background:linear-gradient(180deg,#22121a,#150b10);box-shadow:inset 0 0 0 1px rgba(255,255,255,.1),0 30px 80px rgba(0,0,0,.7);animation:rwIn .45s var(--ease)}
.nwh{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;margin-bottom:18px}
.nwh b{display:block;font-size:30px;font-weight:900;letter-spacing:-.03em}
.nwh span{display:block;margin-top:4px;font-size:14px;color:var(--sub)}
.nwg{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.nwo{position:relative;display:flex;flex-direction:column;align-items:flex-start;gap:10px;min-height:220px;padding:22px;border-radius:20px;text-align:left;overflow:hidden;
  background:linear-gradient(160deg,color-mix(in srgb,var(--oc) 30%,#1a1014),#140c10);box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--oc) 40%,transparent);transition:transform .35s var(--ease),box-shadow .35s}
.nwo::before{content:"";position:absolute;right:-60px;top:-60px;width:220px;height:220px;border-radius:50%;background:radial-gradient(circle,color-mix(in srgb,var(--oc) 55%,transparent),transparent 70%);opacity:.6;transition:transform .5s var(--ease)}
.nwo:hover{transform:translateY(-4px);box-shadow:inset 0 0 0 1.5px var(--oc),0 20px 50px -20px var(--oc)}
.nwo:hover::before{transform:scale(1.2)}
.nwi{position:relative;width:56px;height:56px;border-radius:16px;display:grid;place-items:center;background:linear-gradient(135deg,var(--oc),color-mix(in srgb,var(--oc) 50%,#000));box-shadow:0 10px 26px -10px var(--oc)}
.nwi svg{width:28px;height:28px}
.nwo b{position:relative;margin-top:6px;font-size:21px;font-weight:900;letter-spacing:-.02em}
.nwo p{position:relative;margin:0;font-size:13.5px;line-height:1.45;color:rgba(255,255,255,.72)}
.nwo kbd{position:absolute;right:16px;bottom:14px;padding:3px 8px;border-radius:6px;font:800 12px inherit;color:var(--sub);background:rgba(255,255,255,.08)}
.nwm{display:block;margin:16px auto 0;font-size:13.5px;font-weight:700;color:var(--sub);text-decoration:underline;text-underline-offset:3px}
.nwm:hover{color:#fff}
@media(max-width:640px){.nwg{grid-template-columns:1fr}}
.ipv.avp{padding-top:34px}
/* ---------- перезарядка кнопки «Оценить» ---------- */
#save .rl{transition:opacity .25s}
#save .rcd{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;gap:9px;opacity:0;pointer-events:none;transition:opacity .25s;letter-spacing:.1em;font-size:14px;border-radius:inherit;overflow:hidden}
#save .rcd svg{width:18px;height:18px;flex:none;fill:none;stroke:currentColor;stroke-width:2.2;stroke-linecap:round}
#save .rcd i{position:absolute;left:0;top:0;bottom:0;width:calc(var(--cdp,0) * 100%);background:linear-gradient(90deg,rgba(255,255,255,.06),rgba(255,255,255,.2));transition:width .25s linear}
#save .rcd span{position:relative;font-variant-numeric:tabular-nums}
#save.cd{cursor:not-allowed;box-shadow:none}
#save.cd .rl{opacity:0}
#save.cd .rcd{opacity:1}
#save.cd .rbg{filter:saturate(.2) brightness(.42)}
#save.cd .rbg::after{display:none}
/* ---------- живые рамки мифических значков ---------- */
.mbd.my{position:relative;box-shadow:inset 0 0 0 1.5px rgba(255,255,255,.4),0 0 14px -2px var(--b1)}
.mbd.my::before{content:"";position:absolute;inset:-2px;border-radius:50%;background:conic-gradient(from 0deg,var(--b1),#fff,var(--b2),#fff,var(--b1));animation:frSpin 3s linear infinite;-webkit-mask:radial-gradient(farthest-side,transparent 84%,#000 85%,#000 100%,transparent 101%);mask:radial-gradient(farthest-side,transparent 84%,#000 85%,#000 100%,transparent 101%)}
/* ---------- анимации новых шапок ---------- */
.mav-h .hwg{animation:hWing 2.6s ease-in-out infinite}
@keyframes hWing{50%{transform:rotate(calc(var(--wd) * -9deg))}}
.mav-h .hph{animation:hPhx 1.4s ease-in-out infinite alternate}
@keyframes hPhx{from{filter:brightness(1)}to{filter:brightness(1.25) saturate(1.2)}}
.mav-h .hray{transform-origin:50px -12px;animation:hRay 6s ease-in-out infinite}
@keyframes hRay{0%,100%{transform:rotate(-5deg);opacity:.35}50%{transform:rotate(5deg);opacity:.65}}
.mav-h .hbn{transform-origin:50px 22px;animation:hBn 1.8s cubic-bezier(.3,1.6,.5,1) infinite}
@keyframes hBn{0%,70%,100%{transform:none}80%{transform:scale(1.06,.94)}90%{transform:scale(.97,1.04) translateY(-3px)}}
@keyframes myFl{to{opacity:.5}}

/* ---------- «Мифическое пламя» — теперь совсем другое: два встречных кольца, золотые ободки и языки пламени ---------- */
.mav[data-fr=f_mythic] .mav-r{--rw:21%;background:radial-gradient(farthest-side,transparent calc(100% - 21%),#fff3c4 calc(100% - 20.4%),#d99a1c calc(100% - 19%),transparent calc(100% - 18%),transparent calc(100% - 3.2%),#ffd45a calc(100% - 2.4%),#7a3a05 100%),
  conic-gradient(from 0deg,#2a0008,#ff1744,#ff6d00,#ffd54f,#fff8e1,#ff6d00,#b0002a,#2a0008,#ff1744,#ff9100,#fff8e1,#ff3d00,#2a0008);animation:frSpin 3.2s linear infinite}
.mav[data-fr=f_mythic]{--fgc:rgba(255,61,0,.9)}
.mav[data-fr=f_mythic] .mav-g{box-shadow:0 0 calc(var(--s) * .14) calc(var(--s) * .02) rgba(255,61,0,.85),0 0 calc(var(--s) * .32) calc(var(--s) * .03) rgba(255,193,7,.45);animation:frBreath 1.8s ease-in-out infinite}
.mav[data-fr=f_mythic] .mav-g::before{content:"";position:absolute;inset:-9%;border-radius:50%;background:repeating-conic-gradient(#ffe082 0 2.6deg,transparent 2.6deg 7deg,#ff9100 7deg 8.4deg,transparent 8.4deg 15deg);-webkit-mask:radial-gradient(farthest-side,transparent 88%,#000 89%,#000 97%,transparent 98%);mask:radial-gradient(farthest-side,transparent 88%,#000 89%,#000 97%,transparent 98%);animation:frSpin 10s linear infinite reverse}
.mav[data-fr=f_mythic] .mav-g::after{content:"";position:absolute;inset:-20%;border-radius:50%;background:repeating-conic-gradient(from 0deg,rgba(255,80,0,0) 0 5deg,rgba(255,150,30,.85) 8deg,rgba(255,60,0,0) 11deg 18deg);-webkit-mask:radial-gradient(farthest-side,transparent 70%,#000 71%,#000 99%,transparent 100%);mask:radial-gradient(farthest-side,transparent 70%,#000 71%,#000 99%,transparent 100%);filter:blur(1.5px);animation:frSpin 6s linear infinite,myFl .22s ease-in-out infinite alternate}
.mav[data-fr=f_ruby] .mav-r{--rw:15%;background:radial-gradient(farthest-side,transparent calc(100% - 15%),#ffe9a8 calc(100% - 14.5%),#a8650c calc(100% - 13%),transparent calc(100% - 12.4%)),repeating-conic-gradient(from 0deg,#ff1744 0 9deg,#ffc2cd 9deg 10.5deg,#8a0018 10.5deg 20deg,#ff4d6d 20deg 28deg,#4a000c 28deg 30deg);animation:frSpin 18s linear infinite}
.mav[data-fr=f_ruby]{--fgc:rgba(255,23,68,.7)}
.mav[data-fr=f_sunburst] .mav-r{--rw:17%;background:repeating-conic-gradient(from 0deg,#fffbe6 0 3deg,#ffc107 3deg 9deg,#ff6d00 9deg 13deg,#ffc107 13deg 15deg);animation:frSpin 9s linear infinite}
.mav[data-fr=f_sunburst]{--fgc:rgba(255,193,7,.8)}
.mav[data-fr=f_sunburst] .mav-g::before{content:"";position:absolute;inset:-16%;border-radius:50%;background:repeating-conic-gradient(rgba(255,214,90,.75) 0 1.5deg,transparent 1.5deg 12deg);-webkit-mask:radial-gradient(farthest-side,transparent 74%,#000 75%,#000 99%,transparent 100%);mask:radial-gradient(farthest-side,transparent 74%,#000 75%,#000 99%,transparent 100%);animation:frSpin 24s linear infinite reverse}
.mav[data-fr=f_vortex] .mav-r{--rw:18%;background:repeating-conic-gradient(from 0deg,#0b0026 0 8deg,#5b21b6 14deg,#22d3ee 20deg,#ffffff 22deg,#0b0026 30deg);animation:frSpin 1.6s linear infinite}
.mav[data-fr=f_vortex]{--fgc:rgba(124,58,237,.85)}
.mav[data-fr=f_vortex] .mav-g::before{content:"";position:absolute;inset:-12%;border-radius:50%;background:conic-gradient(from 0deg,transparent,rgba(34,211,238,.7) 20%,transparent 35%,rgba(167,139,250,.7) 60%,transparent 75%);-webkit-mask:radial-gradient(farthest-side,transparent 78%,#000 79%,#000 99%,transparent 100%);mask:radial-gradient(farthest-side,transparent 78%,#000 79%,#000 99%,transparent 100%);filter:blur(2px);animation:frSpin 2.4s linear infinite reverse}
.mav[data-fr=f_plasma] .mav-r{--rw:17%;background:radial-gradient(farthest-side,transparent calc(100% - 17%),rgba(255,255,255,.95) calc(100% - 16.4%),transparent calc(100% - 15%)),repeating-conic-gradient(from 0deg,#120044 0 7deg,#3d5afe 11deg,#00e5ff 13deg,#ffffff 14deg,#e040fb 16deg,#120044 22deg);animation:frSpin 1.4s linear infinite}
.mav[data-fr=f_plasma]{--fgc:rgba(0,229,255,.9)}
.mav[data-fr=f_plasma] .mav-g{animation:plFl 1.3s steps(1) infinite}
@keyframes plFl{0%,100%{opacity:1}20%{opacity:.55}24%{opacity:1}60%{opacity:.7}63%{opacity:1}}
.mav[data-fr=f_plasma] .mav-g::before{content:"";position:absolute;inset:-10%;border-radius:50%;background:conic-gradient(from 0deg,transparent 0 40deg,#80deea 50deg,transparent 62deg 160deg,#ea80fc 170deg,transparent 182deg 280deg,#ffffff 290deg,transparent 302deg);-webkit-mask:radial-gradient(farthest-side,transparent 86%,#000 87%,#000 99%,transparent 100%);mask:radial-gradient(farthest-side,transparent 86%,#000 87%,#000 99%,transparent 100%);filter:drop-shadow(0 0 4px #00e5ff);animation:frSpin .9s linear infinite reverse}
.mav[data-fr=f_blackhole] .mav-r{--rw:20%;background:conic-gradient(from 0deg,rgba(255,145,0,0),rgba(255,145,0,.55) 18%,rgba(255,240,200,.9) 24%,rgba(255,145,0,0) 40%,transparent 62%,rgba(255,109,0,.45) 80%,transparent),
  radial-gradient(farthest-side,transparent calc(100% - 20%),#ffe0b2 calc(100% - 19.4%),#ff9100 calc(100% - 17.6%),#000 calc(100% - 14%),#000 calc(100% - 2%),#3a1400 100%);animation:frSpin 2.2s linear infinite}
.mav[data-fr=f_blackhole]{--fgc:rgba(255,120,0,.75)}
.mav[data-fr=f_blackhole] .mav-g::before{content:"";position:absolute;inset:-42%;border-radius:50%;background:conic-gradient(from 0deg,#ff6d00,#ffe0b2,#fff,#ff3d00,#5d1a00,#ff9100,#ff6d00);-webkit-mask:radial-gradient(farthest-side,transparent 72%,#000 73%,#000 99%,transparent 100%);mask:radial-gradient(farthest-side,transparent 72%,#000 73%,#000 99%,transparent 100%);animation:bhDisk 3s linear infinite;opacity:.9}
@keyframes bhDisk{from{transform:rotate(-14deg) scaleY(.24) rotate(0)}to{transform:rotate(-14deg) scaleY(.24) rotate(360deg)}}
/* коллекции */
.mav[data-fr=fk_brawl] .mav-r{--rw:19%;background:radial-gradient(farthest-side,#111 0 calc(100% - 17%),transparent calc(100% - 16.4%) calc(100% - 4%),#111 calc(100% - 3.4%)),repeating-conic-gradient(from 0deg,#ffce1f 0 22.5deg,#a855f7 22.5deg 45deg);animation:frSpin 6s steps(16) infinite}
.mav[data-fr=fk_brawl]{--fgc:rgba(168,85,247,.7)}
.mav[data-fr=fk_cs] .mav-r{--rw:12%;background:conic-gradient(from -5deg,#39ff14 0 10deg,#1c2316 10deg 85deg,#39ff14 85deg 95deg,#1c2316 95deg 175deg,#39ff14 175deg 185deg,#1c2316 185deg 265deg,#39ff14 265deg 275deg,#1c2316 275deg)}
.mav[data-fr=fk_cs]{--fgc:rgba(57,255,20,.35)}
.mav[data-fr=fk_cs] .mav-g::before{content:"";position:absolute;inset:-9%;border-radius:50%;background:conic-gradient(from -2deg,#39ff14 0 4deg,transparent 4deg 90deg,#39ff14 90deg 94deg,transparent 94deg 180deg,#39ff14 180deg 184deg,transparent 184deg 270deg,#39ff14 270deg 274deg,transparent 274deg);-webkit-mask:radial-gradient(farthest-side,transparent 84%,#000 85%,#000 99%,transparent 100%);mask:radial-gradient(farthest-side,transparent 84%,#000 85%,#000 99%,transparent 100%);animation:csAim 2.4s ease-in-out infinite}
@keyframes csAim{50%{transform:scale(1.07);opacity:.6}}
.mav[data-fr=fk_pubg] .mav-r{--rw:16%;background:radial-gradient(farthest-side,transparent calc(100% - 16%),#c8b07a calc(100% - 15.4%),transparent calc(100% - 14%),transparent calc(100% - 2%),#c8b07a calc(100% - 1.4%)),
  radial-gradient(circle at 20% 18%,#2f3a18 0 12%,transparent 13%),radial-gradient(circle at 72% 26%,#6b5b3a 0 14%,transparent 15%),radial-gradient(circle at 40% 82%,#232a12 0 13%,transparent 14%),radial-gradient(circle at 88% 70%,#7a6a48 0 11%,transparent 12%),radial-gradient(circle at 8% 60%,#7a6a48 0 10%,transparent 11%),radial-gradient(circle at 55% 50%,#3b4a22 0 10%,transparent 11%),#4b5a2a}
.mav[data-fr=fk_pubg]{--fgc:rgba(242,201,76,.35)}
.mav[data-fr=fk_doom] .mav-r{--rw:17%;background:radial-gradient(farthest-side,transparent calc(100% - 17%),#ff6d00 calc(100% - 16.4%),transparent calc(100% - 15%)),repeating-conic-gradient(from 0deg,#1a0806 0 9deg,#ff3d00 9deg 10.4deg,#ffab40 10.4deg 11.6deg,#1a0806 11.6deg 20deg,#3a1208 20deg 30deg);animation:frSpin 14s linear infinite}
.mav[data-fr=fk_doom]{--fgc:rgba(255,61,0,.85)}
.mav[data-fr=fk_doom] .mav-g{animation:frFlick .16s ease-in-out infinite alternate}
.mav[data-fr=fk_doom] .mav-g::after{content:"";position:absolute;inset:-16%;border-radius:50%;background:repeating-conic-gradient(from 0deg,rgba(255,61,0,0) 0 6deg,rgba(255,90,0,.8) 9deg,rgba(255,61,0,0) 12deg 24deg);-webkit-mask:radial-gradient(farthest-side,transparent 74%,#000 75%,#000 99%,transparent 100%);mask:radial-gradient(farthest-side,transparent 74%,#000 75%,#000 99%,transparent 100%);filter:blur(1.2px);animation:frSpin 4s linear infinite reverse}
.mav[data-fr=fx_lux] .mav-r{--rw:16%;background:radial-gradient(farthest-side,transparent calc(100% - 16%),#fff3c4 calc(100% - 15.4%),#a8650c calc(100% - 13.4%),transparent calc(100% - 12.6%),transparent calc(100% - 3%),#a8650c calc(100% - 2.4%),#fff3c4 100%),
  repeating-conic-gradient(from 0deg,#0b0b0e 0 6deg,#b8862b 6deg 7deg,#fff3c4 7deg 7.6deg,#d4a537 7.6deg 9deg);animation:frSpin 40s linear infinite}
.mav[data-fr=fx_lux]{--fgc:rgba(212,165,55,.7)}
.mav[data-fr=fx_lux] .mav-g::before{content:"";position:absolute;inset:-10%;border-radius:50%;background:conic-gradient(from 0deg,transparent 0 30deg,rgba(255,243,196,.9) 45deg,transparent 60deg 210deg,rgba(255,243,196,.9) 225deg,transparent 240deg);-webkit-mask:radial-gradient(farthest-side,transparent 86%,#000 87%,#000 99%,transparent 100%);mask:radial-gradient(farthest-side,transparent 86%,#000 87%,#000 99%,transparent 100%);animation:frSpin 5s linear infinite}
.mav[data-fr=fx_neb] .mav-r{--rw:17%;background:radial-gradient(circle at 20% 30%,#fff 0 1.2%,transparent 1.8%),radial-gradient(circle at 76% 18%,#fff 0 1%,transparent 1.6%),radial-gradient(circle at 86% 74%,#fff 0 1.2%,transparent 1.8%),radial-gradient(circle at 30% 88%,#e0e7ff 0 1%,transparent 1.6%),
  conic-gradient(from 0deg,#0b0626,#7c3aed,#c084fc,#22d3ee,#0b0626,#db2777,#7c3aed,#0b0626);animation:frSpin 9s linear infinite}
.mav[data-fr=fx_neb]{--fgc:rgba(139,92,246,.8)}
.mav[data-fr=fx_neb] .mav-g{box-shadow:0 0 calc(var(--s) * .16) calc(var(--s) * .02) rgba(139,92,246,.75),0 0 calc(var(--s) * .3) calc(var(--s) * .02) rgba(34,211,238,.4);animation:frBreath 3s ease-in-out infinite}

/* ---------- новые фоны ---------- */
.bnr[data-bn=b_crystal]{background:radial-gradient(60% 80% at 50% 115%,rgba(120,220,255,.5),transparent 60%),radial-gradient(40% 50% at 20% 20%,rgba(167,139,250,.35),transparent 70%),linear-gradient(180deg,#0b0a24,#1a1450 60%,#0a0820)}
.bnr[data-bn=b_crystal] .bnr-a{background:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='400' height='120' viewBox='0 0 400 120' preserveAspectRatio='none'%3E%3Cdefs%3E%3ClinearGradient id='c1' x1='0' y1='0' x2='0' y2='1'%3E%3Cstop offset='0' stop-color='%23e0fbff'/%3E%3Cstop offset='.5' stop-color='%235ad8ff'/%3E%3Cstop offset='1' stop-color='%231b3a8a'/%3E%3C/linearGradient%3E%3ClinearGradient id='c2' x1='0' y1='0' x2='0' y2='1'%3E%3Cstop offset='0' stop-color='%23f3e8ff'/%3E%3Cstop offset='.5' stop-color='%23a78bfa'/%3E%3Cstop offset='1' stop-color='%232e1065'/%3E%3C/linearGradient%3E%3C/defs%3E%3Cg opacity='.55'%3E%3Cpath d='M10 120 L30 40 L46 120Z M140 120 L170 30 L196 120Z M300 120 L330 50 L352 120Z' fill='url(%23c2)'/%3E%3C/g%3E%3Cpath d='M0 120 L18 70 L34 120Z M40 120 L62 20 L84 120Z M80 120 L96 64 L110 120Z M190 120 L214 8 L240 120Z M232 120 L250 58 L266 120Z M270 120 L288 76 L300 120Z M350 120 L372 36 L400 120Z' fill='url(%23c1)'/%3E%3Cpath d='M62 20 L62 120 M214 8 L214 120 M372 36 L372 120' stroke='%23fff' stroke-width='1.4' opacity='.6'/%3E%3Cpath d='M120 120 L134 84 L148 120Z M310 120 L322 92 L334 120Z' fill='url(%23c2)'/%3E%3C/svg%3E") bottom/100% 82% no-repeat;filter:drop-shadow(0 0 10px rgba(90,216,255,.5))}
.bnr[data-bn=b_crystal] .bnr-b{background:linear-gradient(110deg,transparent 40%,rgba(255,255,255,.3) 50%,transparent 60%);background-size:300% 100%;animation:bnShine 5s ease-in-out infinite;mix-blend-mode:overlay}
.bnr[data-bn=b_volcano]{background:radial-gradient(40% 50% at 50% 45%,rgba(255,90,0,.55),transparent 70%),linear-gradient(180deg,#0d0204 0%,#3a0806 55%,#7a1a06 100%)}
.bnr[data-bn=b_volcano] .bnr-a{background:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='400' height='120' viewBox='0 0 400 120' preserveAspectRatio='xMidYMax slice'%3E%3Cdefs%3E%3ClinearGradient id='l' x1='0' y1='0' x2='0' y2='1'%3E%3Cstop offset='0' stop-color='%23ffe082'/%3E%3Cstop offset='.4' stop-color='%23ff6d00'/%3E%3Cstop offset='1' stop-color='%23b31200'/%3E%3C/linearGradient%3E%3C/defs%3E%3Cpath d='M0 120 L0 100 L60 92 L140 40 L170 34 L182 42 L196 36 L226 40 L300 94 L400 104 L400 120Z' fill='%23140504'/%3E%3Cpath d='M182 42 C178 60 170 76 160 120 L170 120 C176 90 182 70 186 52Z M196 40 C204 64 214 84 236 120 L244 120 C222 82 208 60 200 40Z' fill='url(%23l)' opacity='.9'/%3E%3Cpath d='M170 34 L182 42 L196 36' stroke='%23ffb300' stroke-width='3' fill='none'/%3E%3C/svg%3E") bottom/cover no-repeat}
.bnr[data-bn=b_volcano] .bnr-b{background:radial-gradient(2px 2px at 30% 60%,#ffb300,transparent),radial-gradient(1.6px 1.6px at 55% 40%,#ff6d00,transparent),radial-gradient(2px 2px at 70% 70%,#ffe082,transparent),radial-gradient(1.4px 1.4px at 45% 80%,#ff9100,transparent);background-size:120px 90px;animation:bxEmb 3s linear infinite}
@keyframes bxEmb{to{background-position:10px -90px,-8px -90px,6px -90px,-4px -90px}}
.bnr[data-bn=b_planet]{background:radial-gradient(1.2px 1.2px at 8% 20%,#fff,transparent),radial-gradient(1px 1px at 22% 64%,#fff,transparent),radial-gradient(1.4px 1.4px at 35% 14%,#fff,transparent),radial-gradient(1px 1px at 48% 78%,#fff,transparent),radial-gradient(1.2px 1.2px at 61% 30%,#fff,transparent),radial-gradient(1px 1px at 74% 58%,#fff,transparent),radial-gradient(1.3px 1.3px at 86% 18%,#fff,transparent),radial-gradient(1px 1px at 93% 70%,#fff,transparent),radial-gradient(1px 1px at 14% 88%,#fff,transparent),radial-gradient(1px 1px at 55% 8%,#fff,transparent),radial-gradient(60% 80% at 80% 30%,rgba(255,170,90,.18),transparent 70%),linear-gradient(160deg,#0b0a1a,#160d2a 60%,#05040c)}
.bnr[data-bn=b_planet] .bnr-a{background:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='240' height='140' viewBox='0 0 240 140' %3E%3Cdefs%3E%3CradialGradient id='p' cx='.35' cy='.3' r='.8'%3E%3Cstop offset='0' stop-color='%23fff3d6'/%3E%3Cstop offset='.45' stop-color='%23e2b77a'/%3E%3Cstop offset='1' stop-color='%235a3410'/%3E%3C/radialGradient%3E%3ClinearGradient id='b' x1='0' y1='0' x2='0' y2='1'%3E%3Cstop offset='0' stop-color='%23fff' stop-opacity='0'/%3E%3Cstop offset='.3' stop-color='%23a0703a' stop-opacity='.35'/%3E%3Cstop offset='.5' stop-color='%23fff' stop-opacity='0'/%3E%3Cstop offset='.7' stop-color='%23a0703a' stop-opacity='.3'/%3E%3Cstop offset='1' stop-color='%23fff' stop-opacity='0'/%3E%3C/linearGradient%3E%3CclipPath id='t'%3E%3Crect x='0' y='0' width='240' height='70'/%3E%3C/clipPath%3E%3CclipPath id='f'%3E%3Crect x='0' y='70' width='240' height='70'/%3E%3C/clipPath%3E%3C/defs%3E%3Cg transform='rotate(-16 120 70)'%3E%3Cellipse cx='120' cy='70' rx='108' ry='22' fill='none' stroke='%23e9cf9f' stroke-width='9' opacity='.7' clip-path='url(%23t)'/%3E%3Cellipse cx='120' cy='70' rx='96' ry='18' fill='none' stroke='%23b88a52' stroke-width='4' opacity='.6' clip-path='url(%23t)'/%3E%3C/g%3E%3Ccircle cx='120' cy='70' r='46' fill='url(%23p)'/%3E%3Ccircle cx='120' cy='70' r='46' fill='url(%23b)'/%3E%3Cg transform='rotate(-16 120 70)'%3E%3Cellipse cx='120' cy='70' rx='108' ry='22' fill='none' stroke='%23f4ddb0' stroke-width='9' opacity='.9' clip-path='url(%23f)'/%3E%3Cellipse cx='120' cy='70' rx='96' ry='18' fill='none' stroke='%23c89a62' stroke-width='4' opacity='.8' clip-path='url(%23f)'/%3E%3C/g%3E%3C/svg%3E") 62% 50%/auto 120% no-repeat;animation:bxPl 9s ease-in-out infinite alternate}
@keyframes bxPl{to{transform:translateY(-4%) rotate(-2deg)}}
.bnr[data-bn=b_heaven]{background:radial-gradient(50% 70% at 50% 20%,#fffdf0,transparent 70%),linear-gradient(180deg,#ffe9a8 0%,#ffcf6b 45%,#f59e0b 100%)}
.bnr[data-bn=b_heaven] .bnr-a{inset:-80% -20% 0;background:repeating-conic-gradient(from 0deg at 50% 50%,rgba(255,255,255,.55) 0 3deg,transparent 3deg 14deg);-webkit-mask:radial-gradient(60% 60% at 50% 50%,#000,transparent 75%);mask:radial-gradient(60% 60% at 50% 50%,#000,transparent 75%);animation:frSpin 60s linear infinite}
.bnr[data-bn=b_heaven] .bnr-b{background:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='400' height='140' viewBox='0 0 400 140' preserveAspectRatio='xMidYMax slice'%3E%3Cdefs%3E%3ClinearGradient id='g' x1='0' y1='0' x2='0' y2='1'%3E%3Cstop offset='0' stop-color='%23fff8e1'/%3E%3Cstop offset='1' stop-color='%23d4a537'/%3E%3C/linearGradient%3E%3C/defs%3E%3Cg fill='url(%23g)' opacity='.95'%3E%3Cpath d='M160 120 L160 52 C160 20 240 20 240 52 L240 120 L228 120 L228 54 C228 32 172 32 172 54 L172 120Z'/%3E%3Crect x='150' y='48' width='8' height='72'/%3E%3Crect x='242' y='48' width='8' height='72'/%3E%3Ccircle cx='154' cy='44' r='5'/%3E%3Ccircle cx='246' cy='44' r='5'/%3E%3C/g%3E%3Cg fill='%23fffdf5'%3E%3Cellipse cx='40' cy='118' rx='70' ry='22'/%3E%3Cellipse cx='110' cy='124' rx='60' ry='20'/%3E%3Cellipse cx='200' cy='126' rx='90' ry='18'/%3E%3Cellipse cx='300' cy='122' rx='70' ry='22'/%3E%3Cellipse cx='380' cy='118' rx='50' ry='22'/%3E%3C/g%3E%3Cg fill='%23ffeec2' opacity='.8'%3E%3Cellipse cx='70' cy='108' rx='40' ry='14'/%3E%3Cellipse cx='330' cy='108' rx='44' ry='14'/%3E%3C/g%3E%3C/svg%3E") bottom/cover no-repeat;filter:drop-shadow(0 0 14px rgba(255,236,170,.9))}
.bnr[data-bn=b_blackhole]{background:radial-gradient(1.2px 1.2px at 8% 20%,#fff,transparent),radial-gradient(1px 1px at 22% 64%,#fff,transparent),radial-gradient(1.4px 1.4px at 35% 14%,#fff,transparent),radial-gradient(1px 1px at 48% 78%,#fff,transparent),radial-gradient(1.2px 1.2px at 61% 30%,#fff,transparent),radial-gradient(1px 1px at 74% 58%,#fff,transparent),radial-gradient(1.3px 1.3px at 86% 18%,#fff,transparent),radial-gradient(1px 1px at 93% 70%,#fff,transparent),radial-gradient(1px 1px at 14% 88%,#fff,transparent),radial-gradient(1px 1px at 55% 8%,#fff,transparent),radial-gradient(40% 60% at 50% 50%,rgba(255,140,40,.18),transparent 70%),#030208}
.bnr[data-bn=b_blackhole] .bnr-a{background:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='300' height='150' viewBox='0 0 300 150' %3E%3Cdefs%3E%3ClinearGradient id='d' x1='0' y1='0' x2='1' y2='0'%3E%3Cstop offset='0' stop-color='%23ff6d00' stop-opacity='0'/%3E%3Cstop offset='.2' stop-color='%23ff9100'/%3E%3Cstop offset='.45' stop-color='%23fff3e0'/%3E%3Cstop offset='.55' stop-color='%23ffe0b2'/%3E%3Cstop offset='.8' stop-color='%23ff6d00'/%3E%3Cstop offset='1' stop-color='%23ff3d00' stop-opacity='0'/%3E%3C/linearGradient%3E%3CradialGradient id='h' cx='.5' cy='.5' r='.5'%3E%3Cstop offset='.6' stop-color='%23ffb74d' stop-opacity='.0'/%3E%3Cstop offset='.82' stop-color='%23ffcc80' stop-opacity='.9'/%3E%3Cstop offset='.9' stop-color='%23ff6d00' stop-opacity='.4'/%3E%3Cstop offset='1' stop-color='%23ff6d00' stop-opacity='0'/%3E%3C/radialGradient%3E%3Cfilter id='bl'%3E%3CfeGaussianBlur stdDeviation='2.5'/%3E%3C/filter%3E%3CclipPath id='t'%3E%3Crect width='300' height='75'/%3E%3C/clipPath%3E%3CclipPath id='f'%3E%3Crect y='75' width='300' height='75'/%3E%3C/clipPath%3E%3C/defs%3E%3Cellipse cx='150' cy='75' rx='140' ry='16' fill='url(%23d)' filter='url(%23bl)' clip-path='url(%23t)'/%3E%3Ccircle cx='150' cy='75' r='52' fill='url(%23h)' filter='url(%23bl)'/%3E%3Ccircle cx='150' cy='75' r='36' fill='%23000'/%3E%3Ccircle cx='150' cy='75' r='37.5' fill='none' stroke='%23fff3e0' stroke-width='1.6'/%3E%3Cellipse cx='150' cy='75' rx='140' ry='16' fill='url(%23d)' filter='url(%23bl)' clip-path='url(%23f)'/%3E%3Cellipse cx='150' cy='77' rx='118' ry='7' fill='url(%23d)' opacity='.8'/%3E%3C/svg%3E") center/auto 118% no-repeat;animation:bxBh 4s ease-in-out infinite alternate}
@keyframes bxBh{to{filter:brightness(1.25) saturate(1.2)}}
.bnr[data-bn=b_blackhole] .bnr-b{background:radial-gradient(18% 34% at 50% 50%,transparent 60%,rgba(255,200,120,.25) 70%,transparent 80%);animation:frBreath 2.4s ease-in-out infinite}
/* игровые коллекции */
.bnr[data-bn=bk_brawl]{background:#6d28d9}
.bnr[data-bn=bk_brawl] .bnr-a{inset:-120% -20%;background:repeating-conic-gradient(from 0deg,#5b21b6 0 9deg,#8b5cf6 9deg 18deg);animation:frSpin 50s linear infinite}
.bnr[data-bn=bk_brawl] .bnr-b{background:radial-gradient(circle,rgba(0,0,0,.22) 1.2px,transparent 1.7px) 0 0/9px 9px,radial-gradient(55% 75% at 50% 100%,rgba(255,206,31,.75),transparent 70%);box-shadow:inset 0 0 0 4px #111}
.bnr[data-bn=bk_cs]{background:radial-gradient(40% 70% at 80% 10%,#fff4dc,transparent 70%),linear-gradient(180deg,#f6d7a7 0%,#e7b47a 60%,#c88a4c 100%)}
.bnr[data-bn=bk_cs] .bnr-a{background:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='400' height='140' viewBox='0 0 400 140' preserveAspectRatio='xMidYMax slice'%3E%3Cpath d='M0 140 L0 70 L60 70 L60 56 L130 56 L130 140Z' fill='%23c99a62'/%3E%3Cpath d='M0 70 L60 70 L60 56 L130 56' stroke='%23e6c08c' stroke-width='3' fill='none'/%3E%3Cpath d='M70 140 L70 104 C70 84 110 84 110 104 L110 140Z' fill='%232a1a0c'/%3E%3Cpath d='M240 140 L240 40 L330 40 L330 60 L400 60 L400 140Z' fill='%23b07a45'/%3E%3Cpath d='M240 40 L330 40 L330 60 L400 60' stroke='%23d9aa72' stroke-width='3' fill='none'/%3E%3Cpath d='M262 140 L262 96 C262 70 304 70 304 96 L304 140Z' fill='%2321140a'/%3E%3Crect x='346' y='84' width='22' height='22' fill='%231d130a'/%3E%3Crect x='150' y='104' width='36' height='36' fill='%238a5a2a' stroke='%235a3a18' stroke-width='3'/%3E%3Cpath d='M150 104 L186 140 M186 104 L150 140' stroke='%235a3a18' stroke-width='3'/%3E%3Crect x='180' y='118' width='26' height='22' fill='%239a6a36' stroke='%235a3a18' stroke-width='2.4'/%3E%3Crect x='0' y='136' width='400' height='4' fill='%23a87a48'/%3E%3C/svg%3E") bottom/cover no-repeat}
.bnr[data-bn=bk_cs] .bnr-b{background:radial-gradient(40% 40% at 30% 70%,rgba(255,235,200,.45),transparent 70%),radial-gradient(40% 40% at 70% 80%,rgba(255,225,180,.4),transparent 70%);filter:blur(6px);animation:bnDrift 8s ease-in-out infinite alternate}
.bnr[data-bn=bk_pubg]{background:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='400' height='80' viewBox='0 0 400 80' preserveAspectRatio='none'%3E%3Cpath d='M0 60 C60 30 120 40 180 52 C240 64 300 34 400 46 L400 80 L0 80Z' fill='%235d7a3a'/%3E%3Cpath d='M0 70 C80 52 160 62 240 66 C320 70 360 56 400 60 L400 80 L0 80Z' fill='%2341592a'/%3E%3C/svg%3E") bottom/100% 46% no-repeat,linear-gradient(180deg,#5d9ad0 0%,#a9cfe6 55%,#d8e6cf 100%)}
.bnr[data-bn=bk_pubg] .bnr-a{background:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='100' height='96' viewBox='0 0 100 96' %3E%3Cpath d='M10 30 C10 6 90 6 90 30 C78 24 66 24 50 30 C34 24 22 24 10 30Z' fill='%23e53935'/%3E%3Cpath d='M26 25 C30 12 40 8 50 7 C60 8 70 12 74 25 C66 23 58 24 50 28 C42 24 34 23 26 25Z' fill='%231e5bd8'/%3E%3Cpath d='M10 30 L44 72 M90 30 L56 72 M30 26 L46 72 M70 26 L54 72' stroke='%23333' stroke-width='1'/%3E%3Crect x='38' y='70' width='24' height='20' fill='%233a4a7a' stroke='%2316203a' stroke-width='2'/%3E%3Crect x='38' y='76' width='24' height='4' fill='%23e53935'/%3E%3C/svg%3E") 72% 10%/auto 62% no-repeat;animation:pgDrop 5s ease-in-out infinite alternate}
@keyframes pgDrop{from{transform:translate(-2%,-6%) rotate(-4deg)}to{transform:translate(2%,6%) rotate(4deg)}}
.bnr[data-bn=bk_pubg] .bnr-b{background:radial-gradient(18% 40% at 20% 90%,rgba(229,57,53,.75),transparent 70%),radial-gradient(14% 50% at 24% 60%,rgba(229,57,53,.45),transparent 70%);filter:blur(8px);animation:bnDrift 4s ease-in-out infinite alternate}
.bnr[data-bn=bk_doom]{background:radial-gradient(70% 60% at 50% 100%,#ff6a00,#b31200 35%,transparent 72%),linear-gradient(180deg,#120000,#3a0500 60%,#6a0a00)}
.bnr[data-bn=bk_doom] .bnr-a{background:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='400' height='140' viewBox='0 0 400 140' preserveAspectRatio='xMidYMax slice'%3E%3Cpath d='M0 140 L0 96 L20 90 L30 50 L38 88 L60 84 L70 30 L80 82 L110 90 L130 70 L140 92 L170 96 L190 40 L196 20 L202 40 L222 96 L260 90 L270 60 L276 88 L300 82 L314 36 L324 84 L350 92 L366 66 L374 90 L400 94 L400 140Z' fill='%230b0000'/%3E%3Cpath d='M190 40 L196 20 L202 40' stroke='%23ff3d00' stroke-width='2' fill='none'/%3E%3Cpath d='M66 46 L70 30 L74 46 M310 50 L314 36 L318 50' stroke='%23ff6d00' stroke-width='1.6' fill='none'/%3E%3Cpath d='M0 130 C80 120 140 136 200 128 C260 120 320 134 400 126 L400 140 L0 140Z' fill='%23ff6d00' opacity='.55'/%3E%3C/svg%3E") bottom/cover no-repeat}
.bnr[data-bn=bk_doom] .bnr-b{background:radial-gradient(2px 2px at 25% 70%,#ffb300,transparent),radial-gradient(1.6px 1.6px at 60% 50%,#ff6d00,transparent),radial-gradient(2px 2px at 80% 80%,#ffe082,transparent),radial-gradient(1.4px 1.4px at 40% 90%,#ff3d00,transparent);background-size:110px 80px;animation:bxEmb 2.4s linear infinite}
.bnr[data-bn=bx_lux]{background:radial-gradient(70% 90% at 50% 40%,transparent 40%,rgba(0,0,0,.75) 100%),url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='40' height='40' viewBox='0 0 40 40' %3E%3Cg fill='none' stroke='%23d4a537' stroke-width='1' opacity='.32'%3E%3Cpath d='M0 40 A20 20 0 0 1 40 40 M6 40 A14 14 0 0 1 34 40 M12 40 A8 8 0 0 1 28 40'/%3E%3Cpath d='M20 20 A20 20 0 0 1 60 20 M26 20 A14 14 0 0 1 54 20 M32 20 A8 8 0 0 1 48 20'/%3E%3Cpath d='M-20 20 A20 20 0 0 1 20 20 M-14 20 A14 14 0 0 1 14 20 M-8 20 A8 8 0 0 1 8 20'/%3E%3C/g%3E%3C/svg%3E") 0 0/40px 40px,linear-gradient(180deg,#141210,#0b0b0e)}
.bnr[data-bn=bx_lux]::before{content:"";position:absolute;inset:0;opacity:.0}
.bnr[data-bn=bx_lux] .bnr-a{background:linear-gradient(110deg,transparent 35%,rgba(255,230,160,.28) 50%,transparent 65%);background-size:300% 100%;animation:bnShine 5s ease-in-out infinite}
.bnr[data-bn=bx_lux] .bnr-b{background:linear-gradient(#d4a537,#d4a537) 0 12px/100% 1px no-repeat,linear-gradient(#fff3c4,#fff3c4) 0 16px/100% 1px no-repeat,linear-gradient(#d4a537,#d4a537) 0 calc(100% - 12px)/100% 1px no-repeat,linear-gradient(#fff3c4,#fff3c4) 0 calc(100% - 16px)/100% 1px no-repeat;opacity:.7}
.bnr[data-bn=bx_neb]{background:radial-gradient(1.2px 1.2px at 8% 20%,#fff,transparent),radial-gradient(1px 1px at 22% 64%,#fff,transparent),radial-gradient(1.4px 1.4px at 35% 14%,#fff,transparent),radial-gradient(1px 1px at 48% 78%,#fff,transparent),radial-gradient(1.2px 1.2px at 61% 30%,#fff,transparent),radial-gradient(1px 1px at 74% 58%,#fff,transparent),radial-gradient(1.3px 1.3px at 86% 18%,#fff,transparent),radial-gradient(1px 1px at 93% 70%,#fff,transparent),radial-gradient(1px 1px at 14% 88%,#fff,transparent),radial-gradient(1px 1px at 55% 8%,#fff,transparent),linear-gradient(160deg,#07051a,#0e0a2e 55%,#04030c)}
.bnr[data-bn=bx_neb] .bnr-a{inset:-20%;background:radial-gradient(35% 45% at 35% 45%,rgba(124,58,237,.75),transparent 70%),radial-gradient(30% 40% at 62% 40%,rgba(34,211,238,.55),transparent 70%),radial-gradient(25% 35% at 50% 65%,rgba(219,39,119,.5),transparent 70%);filter:blur(14px);animation:bnDrift 10s ease-in-out infinite alternate}
.bnr[data-bn=bx_neb] .bnr-b{background:radial-gradient(1.6px 1.6px at 18% 40%,#fff,transparent),radial-gradient(1.4px 1.4px at 66% 22%,#e0e7ff,transparent),radial-gradient(1.8px 1.8px at 82% 60%,#fff,transparent),radial-gradient(1.2px 1.2px at 40% 76%,#c7d2fe,transparent);animation:frBreath 2.2s ease-in-out infinite}

/* ---------- новые ники ---------- */
.mnm[data-nm=n_lava]{background:linear-gradient(0deg,#3e0a00 0%,#ff3d00 22%,#ffab00 40%,#fff3c4 50%,#ffab00 60%,#ff3d00 78%,#3e0a00 100%);background-size:100% 300%;-webkit-background-clip:text;background-clip:text;color:transparent;animation:nmLava 2.4s linear infinite;filter:drop-shadow(0 0 8px rgba(255,90,0,.65))}
@keyframes nmLava{to{background-position:0 -300%}}
.mnm[data-nm=n_diamond]{background:linear-gradient(115deg,#ffffff 0%,#b3f0ff 18%,#ffffff 30%,#d6e4ff 42%,#9cecff 55%,#ffffff 66%,#e8d6ff 80%,#ffffff 100%);background-size:260% 100%;-webkit-background-clip:text;background-clip:text;color:transparent;animation:nmSheen 3.4s linear infinite;filter:drop-shadow(0 0 6px rgba(160,230,255,.8)) drop-shadow(0 2px 2px rgba(0,0,0,.4))}
.mnm[data-nm=n_void]{background:radial-gradient(1px 1px at 20% 40%,#fff,transparent),radial-gradient(1px 1px at 60% 70%,#fff,transparent),radial-gradient(1px 1px at 80% 30%,#fff,transparent),linear-gradient(180deg,#f3e8ff,#a78bfa 45%,#6d28d9 80%,#3b0a7a);background-size:40px 30px,50px 40px,36px 28px,100% 100%;-webkit-background-clip:text;background-clip:text;color:transparent;animation:nmVoid 3s ease-in-out infinite}
@keyframes nmVoid{0%,100%{filter:drop-shadow(0 0 6px rgba(139,92,246,.9)) drop-shadow(0 0 16px rgba(76,29,149,.8))}50%{filter:drop-shadow(0 0 10px rgba(167,139,250,1)) drop-shadow(0 0 26px rgba(124,58,237,.9))}}
.mnm[data-nm=n_phoenix]{background:linear-gradient(100deg,#b0002a 0%,#ff3d00 18%,#ffab00 32%,#fff8e1 42%,#ffd54f 52%,#ff6d00 68%,#b0002a 84%,#ff3d00 100%);background-size:260% 100%;-webkit-background-clip:text;background-clip:text;color:transparent;animation:nmSheen 2.6s linear infinite,nmPhx .5s ease-in-out infinite alternate}
@keyframes nmPhx{from{filter:drop-shadow(0 0 6px rgba(255,61,0,.8)) drop-shadow(0 -2px 10px rgba(255,171,0,.5))}to{filter:drop-shadow(0 0 10px rgba(255,109,0,.95)) drop-shadow(0 -4px 16px rgba(255,213,79,.7))}}
.mnm[data-nm=nk_brawl]{color:#ffce1f;font-weight:900;overflow:visible;padding:0 .1em .2em;text-shadow:.05em 0 #111,-.05em 0 #111,0 .05em #111,0 -.05em #111,.05em .05em #111,-.05em .05em #111,.05em -.05em #111,-.05em -.05em #111,0 .11em 0 #111,0 .15em 0 #7c3aed}
.mnm[data-nm=nk_cs]{font-family:Consolas,"Courier New",monospace;font-weight:800;letter-spacing:.05em;text-transform:uppercase;color:#f5a524;text-shadow:0 0 10px rgba(245,165,36,.5),.06em .06em 0 #3a2a0a}
.mnm[data-nm=nk_pubg]{font-weight:900;letter-spacing:.03em;text-transform:uppercase;background:linear-gradient(100deg,#c8a23a 0%,#f2c94c 30%,#fff3c4 45%,#f2c94c 60%,#a8801e 100%);background-size:240% 100%;-webkit-background-clip:text;background-clip:text;color:transparent;animation:nmSheen 4s linear infinite;filter:drop-shadow(.05em .06em 0 #3a2a08)}
.mnm[data-nm=nk_doom]{font-weight:900;text-transform:uppercase;letter-spacing:.03em;background:linear-gradient(180deg,#ffffff 0%,#ffd9c2 30%,#ff7a2e 55%,#ff3d00 75%,#ffc46b 100%);-webkit-background-clip:text;background-clip:text;color:transparent;animation:nmDoom .14s ease-in-out infinite alternate}
@keyframes nmDoom{from{filter:drop-shadow(0 .05em 0 #2a0000) drop-shadow(0 0 6px rgba(255,61,0,.75))}to{filter:drop-shadow(0 .05em 0 #2a0000) drop-shadow(0 0 11px rgba(255,90,0,.95))}}
.mnm[data-nm=nx_lux]{font-family:Georgia,"Times New Roman",serif;font-style:italic;letter-spacing:.04em;background:linear-gradient(100deg,#8a6a1f 0%,#f6e27a 20%,#fffbe6 28%,#d4a537 40%,#8a6a1f 55%,#f6e27a 75%,#b8862b 100%);background-size:300% 100%;-webkit-background-clip:text;background-clip:text;color:transparent;animation:nmSheen 6s linear infinite;filter:drop-shadow(0 1px 0 #4a3510) drop-shadow(0 0 10px rgba(212,165,55,.45))}
.mnm[data-nm=nx_neb]{background:linear-gradient(90deg,#a78bfa,#22d3ee,#f472b6,#c4b5fd,#a78bfa);background-size:240% 100%;-webkit-background-clip:text;background-clip:text;color:transparent;animation:nmSheen 5s linear infinite;filter:drop-shadow(0 0 10px rgba(139,92,246,.7))}

/* ---------- мини-плеер ---------- */
#mp{position:fixed;left:50%;bottom:14px;z-index:13;width:min(900px,calc(100vw - 32px));display:grid;grid-template-columns:minmax(150px,250px) auto minmax(140px,1fr) 136px 34px;align-items:center;gap:18px;padding:9px 18px 9px 9px;border-radius:18px;background:hsl(var(--tn) 42% 7% / .94);box-shadow:inset 0 0 0 1px rgba(255,255,255,.1),0 18px 50px -12px rgba(0,0,0,.85),0 0 46px -18px rgba(var(--ar),.75);transform:translateX(-50%);animation:mpIn .45s var(--ease) backwards}
#mp.out{animation:mpOut .25s ease forwards;pointer-events:none}
@keyframes mpIn{from{opacity:0;transform:translate(-50%,26px) scale(.97)}}
@keyframes mpOut{to{opacity:0;transform:translate(-50%,20px) scale(.98)}}
.mpinfo{display:flex;align-items:center;gap:12px;min-width:0;padding:3px 8px 3px 3px;border-radius:12px;text-align:left;transition:background .2s}
.mpinfo:hover{background:rgba(255,255,255,.07)}
.mpcv{position:relative;flex:none;width:46px;height:46px;border-radius:10px;overflow:hidden;box-shadow:0 4px 14px rgba(0,0,0,.5)}
.mpeq{position:absolute;inset:0;display:grid;place-items:center;background:rgba(0,0,0,.45);opacity:0;transition:opacity .25s}
#mp.playing .mpeq{opacity:1}
#mp:not(.playing) .mpeq .eq i{animation-play-state:paused}
.mpt{min-width:0}
.mpt b,.mpt em{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mpt b{font-size:14px;font-weight:700;transition:color .2s}
.mpt em{margin-top:2px;font-style:normal;font-size:12px;color:var(--sub)}
.mpinfo:hover .mpt b{color:var(--g3)}
.mpctl{display:flex;align-items:center;gap:8px}
.mpctl .apb{width:42px;height:42px}
.mpctl .apb svg{width:18px;height:18px}
.mpseek{display:flex;align-items:center;gap:10px;min-width:0;font-size:11.5px;color:var(--sub);font-variant-numeric:tabular-nums}
.mpseek i{flex:none;min-width:32px;font-style:normal}
.mpseek i:last-child{text-align:right}
.mpvol{display:flex;align-items:center;gap:6px;color:var(--sub)}
.mpvol button{flex:none;width:30px;height:30px;border-radius:50%;display:grid;place-items:center;color:inherit;transition:background .2s,color .2s}
.mpvol button:hover{background:rgba(255,255,255,.1);color:#fff}
.mpvol svg{width:18px;height:18px}
.mpvol .mx,#mp.mute .mpvol .wv{display:none}
#mp.mute .mpvol .mx{display:inline}
body.mpon #toast{bottom:100px}
body.mpon #credit{opacity:0}
body.mpon .pbody,body.mpon .content{padding-bottom:110px}
#mp .mpx{order:5;width:34px;height:34px;border-radius:50%;display:grid;place-items:center;color:var(--sub);transition:background .2s,color .2s,transform .2s var(--ease)}
#mp .mpx:hover{background:rgba(255,255,255,.12);color:#fff;transform:rotate(90deg)}
#mp .mpx svg{width:18px;height:18px}
@media(max-width:900px){#mp{grid-template-columns:minmax(0,1fr) auto 34px;gap:10px}.mpseek,.mpvol{display:none}}
/* ---------- онлайн-комната: отдельная страница ---------- */
.rmp .pbody{padding-bottom:40px}
.rmv{animation:pgSlide .45s var(--ease) backwards}
.rmcards{max-width:900px;margin-top:4px}
.rmcards .wl-card{animation:pgItem .55s var(--ease) backwards}
.rmcards .wl-card:nth-child(2){animation-delay:.07s}
.rmresume{display:flex;align-items:center;gap:16px;width:100%;max-width:900px;margin:0 0 18px;padding:16px 20px;border-radius:16px;text-align:left;background:linear-gradient(90deg,rgba(var(--ar),.22),rgba(255,255,255,.04));box-shadow:inset 0 0 0 1px rgba(var(--ar),.45);transition:transform .3s var(--ease),box-shadow .3s;animation:pgItem .5s var(--ease) backwards}
.rmresume:hover{transform:translateY(-3px);box-shadow:inset 0 0 0 1px rgba(var(--ar),.8),0 16px 40px -16px rgba(var(--ar),.8)}
.rmresume>span:nth-child(2){flex:1;min-width:0}
.rmresume b{display:block;font-size:17px;font-weight:800}
.rmresume em{display:block;font-style:normal;font-size:13px;color:var(--sub);margin-top:2px}
.rmresume svg{width:22px;height:22px;flex:none}
.rmdot{flex:none;width:12px;height:12px;border-radius:50%;background:#3ddc84;box-shadow:0 0 0 0 rgba(61,220,132,.6);animation:rmPulse 1.8s ease-out infinite}
@keyframes rmPulse{70%,100%{box-shadow:0 0 0 12px rgba(61,220,132,0)}}
.rmp .rmbody{max-width:1000px}
.rmjoin{display:grid;grid-template-columns:1fr auto;gap:10px;align-items:center}
.rmjoin .gbtn{padding:11px 24px}
#rmFrame,#rmJFrame{width:100%;height:min(70vh,760px);border:0;border-radius:14px;background:rgba(0,0,0,.25);color-scheme:dark}
@media(max-width:560px){.rmjoin{grid-template-columns:1fr}}
/* ---------- настройки ---------- */
#st{position:fixed;inset:0;z-index:14500;display:flex;justify-content:flex-end;background:rgba(4,1,2,.5);-webkit-backdrop-filter:blur(6px);backdrop-filter:blur(6px);animation:qFade .25s ease backwards}
#st.out{animation:qFadeOut .2s ease forwards;pointer-events:none}
#st.out .stp{animation:stOut .2s ease forwards}
.stp{width:min(470px,100%);height:100%;display:flex;flex-direction:column;background:linear-gradient(180deg,hsl(var(--tn) 54% 10% / .98),hsl(var(--tn) 40% 4.5% / .98));box-shadow:-30px 0 80px -20px rgba(var(--ar),.45),inset 1px 0 0 rgba(255,255,255,.1);animation:stIn .4s var(--ease) backwards}
@keyframes stIn{from{transform:translateX(40px);opacity:0}}
@keyframes stOut{to{transform:translateX(30px);opacity:0}}
.sth{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;padding:24px 24px 10px}
.sth h2{margin:0;font-size:26px;font-weight:900;letter-spacing:-.02em}
.sth p{margin:4px 0 0;font-size:13px;color:var(--sub)}
.stb{flex:1;min-height:0;overflow:auto;padding:4px 24px 28px}
.stsec{margin:26px 0 6px;font-size:11px;font-weight:800;letter-spacing:.14em;text-transform:uppercase;color:var(--sub)}
.thg{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:10px}
.thm{position:relative;text-align:left;padding:0;border-radius:14px;overflow:hidden;background:rgba(255,255,255,.05);box-shadow:inset 0 0 0 1px rgba(255,255,255,.08);transition:transform .25s var(--ease),box-shadow .25s}
.thm:hover{transform:translateY(-2px)}
.thm.on{box-shadow:inset 0 0 0 2px var(--g2),0 8px 26px -10px rgba(var(--ar),.7)}
.thm i{display:block;height:52px;background:var(--sw)}
.thm b{display:block;padding:9px 12px 0;font-size:13.5px;font-weight:800}
.thm em{display:block;padding:2px 12px 11px;font-style:normal;font-size:11.5px;color:var(--sub);line-height:1.3}
.strow{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:12px 0;border-bottom:1px solid rgba(255,255,255,.06);cursor:var(--cur-p)}
.stt b{display:block;font-size:14px;font-weight:700}
.stt em{display:block;margin-top:2px;font-style:normal;font-size:12px;color:var(--sub);line-height:1.35}
.sw{-webkit-appearance:none;appearance:none;flex:none;width:44px;height:26px;margin:0;border-radius:999px;background:rgba(255,255,255,.16);position:relative;transition:background .25s;cursor:var(--cur-p)}
.sw::after{content:"";position:absolute;left:3px;top:3px;width:20px;height:20px;border-radius:50%;background:#fff;transition:transform .25s var(--ease)}
.sw:checked{background:linear-gradient(135deg,var(--g2),var(--g))}
.sw:checked::after{transform:translateX(18px)}
.stsl{padding:12px 0;border-bottom:1px solid rgba(255,255,255,.06)}
.stsl .top{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}
.stsl .sv{flex:none;font-size:13px;font-weight:800;font-variant-numeric:tabular-nums}
.sr{width:100%;margin:12px 0 2px;accent-color:var(--g2);cursor:var(--cur-p)}
.sr.hue{-webkit-appearance:none;appearance:none;height:10px;border-radius:999px;background:linear-gradient(90deg,hsl(0 90% 55%),hsl(60 90% 55%),hsl(120 90% 50%),hsl(180 90% 50%),hsl(240 90% 60%),hsl(300 90% 55%),hsl(360 90% 55%))}
.sr.hue::-webkit-slider-thumb{-webkit-appearance:none;width:20px;height:20px;border-radius:50%;background:#fff;box-shadow:0 0 0 3px rgba(0,0,0,.45)}
.stseg{display:flex;gap:4px;padding:4px;border-radius:999px;background:rgba(255,255,255,.08);flex:none}
.stseg button{padding:7px 14px;border-radius:999px;font-size:13px;font-weight:700;color:var(--sub);transition:background .2s,color .2s}
.stseg button.on{background:#fff;color:#000}
.strst{margin-top:28px;width:100%;height:42px;border-radius:999px;background:rgba(255,255,255,.1);font-weight:800;font-size:13.5px;transition:background .2s}
.strst:hover{background:rgba(255,255,255,.2)}
.stnote{margin-top:12px;font-size:12px;color:rgba(255,255,255,.4)}
body.bye #st{display:none}
/* кнопка «включить открытый трек»: пилюля с белой «каплей», как в той версии */
.swp{-webkit-appearance:none;appearance:none;flex:none;width:44px;height:26px;margin:0;border-radius:999px;background:rgba(255,255,255,.16);position:relative;transition:background .25s;animation:wlRing 2.2s ease-out infinite}
.swp::after{content:"";position:absolute;left:3px;top:3px;width:20px;height:20px;border-radius:50%;background:#fff;transition:transform .25s var(--ease)}
/* ---------- подтверждение покупки ---------- */
#bc{position:fixed;inset:0;z-index:15200;display:grid;place-items:center;padding:24px;background:rgba(4,1,2,.7);animation:qFade .2s ease backwards}
#bc.out{animation:qFadeOut .18s ease forwards;pointer-events:none}
#bc.out .qcard{animation:qPopOut .18s ease forwards}
.bcard{width:min(430px,100%)}
.bcard .qic{color:#ffd76a;background:radial-gradient(circle,rgba(255,200,80,.28),rgba(255,160,40,.06));box-shadow:inset 0 0 0 1px rgba(255,200,90,.4),0 0 30px rgba(255,170,40,.3)}
.bcard .qic svg{width:40px;height:40px}
.bcn{margin:10px 0 0!important;font-size:18px!important;font-weight:800;color:#fff!important;overflow-wrap:anywhere}
.bcs{margin-top:4px;font-size:13px;color:var(--sub)}
.bcrows{display:grid;gap:8px;margin-top:20px;padding:14px 16px;border-radius:14px;background:rgba(255,255,255,.05);box-shadow:inset 0 0 0 1px rgba(255,255,255,.07)}
.bcr{display:flex;align-items:center;justify-content:space-between;gap:12px;font-size:13.5px;color:var(--sub)}
.bcr b{display:flex;align-items:center;gap:6px;font-size:18px;font-weight:900;color:#ffe3a3;font-variant-numeric:tabular-nums}
.bcr b .coin{width:20px;height:20px;vertical-align:0}
.bcr:last-child b{color:#fff}
#bc .qyes{background:linear-gradient(180deg,#ffd76a,#e9a23a);color:#2a1500;box-shadow:0 8px 26px -8px rgba(255,170,40,.8)}
body.bye #bc{display:none}

/* ===================== задания, достижения, казино, карточка ===================== */
.ndot{margin-left:auto;min-width:20px;height:20px;padding:0 6px;border-radius:999px;display:grid;place-items:center;background:var(--g2);color:#fff;font-size:11.5px;font-weight:900;box-shadow:0 0 12px rgba(var(--ar),.7);animation:ndot 1.6s ease-in-out infinite}
.ndot[hidden]{display:none}
@keyframes ndot{50%{transform:scale(1.15)}}
#qa,#shr{position:fixed;inset:0;z-index:60;display:grid;place-items:center;padding:20px;background:rgba(4,2,6,.62);backdrop-filter:blur(8px);animation:gfade .22s ease}
#qa.out,#shr.out{animation:gfadeOut .22s ease forwards}
.qcard2,.shc{width:min(760px,100%);max-height:calc(100vh - 40px);display:flex;flex-direction:column;border-radius:22px;background:linear-gradient(180deg,rgba(32,16,24,.98),rgba(14,8,12,.98));box-shadow:0 40px 100px rgba(0,0,0,.7),inset 0 0 0 1px rgba(255,255,255,.08);animation:gpop .35s var(--ease);overflow:hidden}
.qhd{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;padding:22px 24px 8px}
.qhd b{display:block;font-size:26px;font-weight:900;letter-spacing:-.02em}
.qhd span{display:block;margin-top:4px;font-size:13px;color:var(--sub)}
.qtabs{margin:8px 24px 6px;align-self:flex-start}
.qtabs em{font-style:normal;opacity:.65;margin-left:4px;font-size:12px}
.qbody{overflow:auto;padding:8px 24px 22px}
.qlist,.alist{display:grid;gap:10px}
.qrow,.qbonus,.arow{display:grid;grid-template-columns:auto 1fr auto auto;align-items:center;gap:14px;padding:14px 16px;border-radius:16px;background:rgba(255,255,255,.05);box-shadow:inset 0 0 0 1px rgba(255,255,255,.07);transition:background .2s,box-shadow .2s}
.qrow.done:not(.cl){background:linear-gradient(90deg,rgba(var(--ar),.2),rgba(255,255,255,.05));box-shadow:inset 0 0 0 1px rgba(var(--ar),.55),0 0 26px -10px rgba(var(--ar),.8)}
.qrow.cl,.qbonus.cl{opacity:.55}
.qic{width:46px;height:46px;border-radius:14px;display:grid;place-items:center;background:rgba(var(--ar),.16);color:var(--g3)}
.qic svg,.qmed svg{width:24px;height:24px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.qtx{min-width:0}
.qtx b{display:block;font-size:15.5px;font-weight:800}
.qtx span{display:block;margin-top:2px;font-size:12.5px;color:var(--sub)}
.qbar{height:6px;margin-top:9px;border-radius:999px;background:rgba(255,255,255,.1);overflow:hidden}
.qbar i{display:block;height:100%;border-radius:inherit;background:linear-gradient(90deg,var(--g0),var(--g3));transition:width .5s var(--ease)}
.qrw,.arw{display:flex;flex-direction:column;align-items:flex-end;gap:3px;font-size:13px;font-weight:800;white-space:nowrap}
.qrw em,.arw em{font-style:normal;display:inline-flex;align-items:center;gap:5px}
.qrw .coin,.arw .coin{width:16px;height:16px}
.qrw .xp{color:#9be9ff;font-size:12px}
.arw .ait{color:#ffd45a;font-size:11.5px}
.qbtn{padding:10px 18px;border-radius:999px;font-weight:800;font-size:13.5px;color:#2a1500;background:linear-gradient(180deg,#ffd76a,#e9a23a);box-shadow:0 8px 22px -8px rgba(255,170,40,.8);transition:transform .2s var(--ease)}
.qbtn:hover{transform:scale(1.06)}
.qbtn[disabled]{opacity:.35;pointer-events:none;filter:grayscale(1)}
.qpr{min-width:62px;text-align:right;font-weight:800;color:var(--sub);font-variant-numeric:tabular-nums}
.qok{font-weight:800;font-size:13px;color:#7af0a0}
.qbonus{margin-top:6px;background:linear-gradient(90deg,rgba(255,200,80,.12),rgba(255,255,255,.04));box-shadow:inset 0 0 0 1px rgba(255,210,110,.28)}
.qbonus .qic{background:rgba(255,200,80,.18);color:#ffd45a}
.qbonus.ready{box-shadow:inset 0 0 0 1px rgba(255,210,110,.7),0 0 30px -10px rgba(255,190,60,.9)}
.qfoot{margin-top:14px;text-align:center;font-size:12.5px;color:var(--sub)}
.arow{grid-template-columns:auto 1fr auto}
.arow:not(.on) .qtx b{color:rgba(255,255,255,.6)}
.qmed{position:relative;width:50px;height:50px;border-radius:50%;display:grid;place-items:center;color:#fff;background:radial-gradient(circle at 35% 28%,rgba(255,255,255,.6),transparent 45%),linear-gradient(150deg,var(--m1),var(--m2));box-shadow:inset 0 0 0 2px rgba(255,255,255,.35),0 6px 18px -6px var(--m1)}
.qmed svg{filter:drop-shadow(0 1px 1px rgba(0,0,0,.35))}
.qmed.off{background:rgba(255,255,255,.06);box-shadow:inset 0 0 0 1.5px rgba(255,255,255,.12);color:rgba(255,255,255,.28)}
.arow.on{background:linear-gradient(90deg,rgba(255,255,255,.08),rgba(255,255,255,.03))}
/* карточка «Поделиться» */
.shc{width:min(900px,100%)}
.shg{display:grid;grid-template-columns:auto 1fr;gap:24px;padding:10px 24px 24px;min-height:0}
.shp{position:relative;height:min(62vh,600px);aspect-ratio:9/16;border-radius:18px;overflow:hidden;background:rgba(255,255,255,.05);box-shadow:0 20px 50px -15px rgba(0,0,0,.8),inset 0 0 0 1px rgba(255,255,255,.08)}
.shp.post{aspect-ratio:4/5}
.shp.ld::after{content:"";position:absolute;inset:0;background:linear-gradient(100deg,transparent 30%,rgba(255,255,255,.08) 50%,transparent 70%);background-size:200% 100%;animation:shLd 1s linear infinite}
@keyframes shLd{from{background-position:150% 0}to{background-position:-50% 0}}
.shcv{display:block;width:100%;height:100%}
.shs{display:flex;flex-direction:column;align-items:flex-start;gap:14px;padding-top:6px}
.shs p{margin:0;color:var(--sub);font-size:14px;line-height:1.5}
.shs .gbtn{width:100%;max-width:300px}
.gbtn.ghost{background:rgba(255,255,255,.08);box-shadow:inset 0 0 0 1px rgba(255,255,255,.14)}
.shn{font-size:12px;color:var(--sub)}
@media (max-width:760px){.shg{grid-template-columns:1fr}.shp{height:auto;width:100%;max-width:340px;margin:0 auto}}
/* мини-игры: широкое окно для казино */
.gcard.wide{width:min(94vw,640px)}
.gtab{white-space:nowrap;padding:8px 6px;font-size:12.5px}
.chist{margin-left:auto;display:flex;gap:4px;align-items:center;overflow:hidden;max-width:60%}
.chist i{width:12px;height:12px;border-radius:50%;flex:none}
.chist em{font-style:normal;font-size:11px;font-weight:800;padding:2px 6px;border-radius:6px;background:rgba(255,255,255,.07)}
.chist em.g{color:#7af0a0}.chist em.r{color:#ff6b7f}
.chist i.r,.rut.r{background:#e5263f}.chist i.k,.rut.k{background:#23232b}.chist i.z,.rut.z{background:#1fb35c}
.ruw{position:relative;height:96px;border-radius:14px;overflow:hidden;background:#0c0a0e;box-shadow:inset 0 0 0 1px rgba(255,255,255,.08)}
.rustrip{position:absolute;left:0;top:12px;display:flex;will-change:transform}
.rut{flex:none;width:68px;height:72px;margin:0 2px;border-radius:10px;display:grid;place-items:center;font-style:normal;font-weight:900;font-size:22px;color:#fff;box-shadow:inset 0 0 0 1px rgba(255,255,255,.12),inset 0 -10px 18px rgba(0,0,0,.25)}
.rumark{position:absolute;left:50%;top:0;bottom:0;width:4px;margin-left:-2px;background:#ffd45a;box-shadow:0 0 14px #ffb300;border-radius:2px;z-index:2}
.rumark::before,.rumark::after{content:"";position:absolute;left:50%;margin-left:-8px;border:8px solid transparent}
.rumark::before{top:0;border-top-color:#ffd45a}.rumark::after{bottom:0;border-bottom-color:#ffd45a}
.rushade{position:absolute;top:0;bottom:0;width:22%;z-index:1;pointer-events:none}
.rushade.l{left:0;background:linear-gradient(90deg,#0c0a0e,transparent)}.rushade.r{right:0;background:linear-gradient(-90deg,#0c0a0e,transparent)}
.cres{margin:12px 0 10px;text-align:center;font-size:14.5px;font-weight:700;color:var(--sub);min-height:22px}
.cres b{color:#7af0a0}.cres.lose{color:#ff8a9a}
.cres span.r{color:#ff5a6e}.cres span.k{color:#c9cdd8}.cres span.z{color:#3fe07f}
.cbet{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:12px}
.cbin{display:flex;align-items:center;gap:8px;padding:0 12px;height:42px;border-radius:12px;background:rgba(0,0,0,.35);box-shadow:inset 0 0 0 1px var(--line)}
.cbin .coin{width:20px;height:20px}
.cbin input{width:110px;font:inherit;font-size:16px;font-weight:800;color:#fff;background:none;border:0;outline:0}
.cbq{display:flex;gap:4px;flex-wrap:wrap}
.cbq button{height:34px;padding:0 10px;border-radius:9px;font-size:12.5px;font-weight:800;color:var(--sub);background:rgba(255,255,255,.07)}
.cbq button:hover{color:#fff;background:rgba(255,255,255,.13)}
.busy .cbq,.busy .cbin,.busy .cauto{opacity:.45;pointer-events:none}
.cauto{display:flex;align-items:center;gap:6px;height:42px;padding:0 10px;border-radius:12px;background:rgba(0,0,0,.25);font-size:12.5px;font-weight:700;color:var(--sub)}
.cauto input{width:52px;font:inherit;font-weight:800;color:#fff;background:none;border:0;outline:0}
.rubtns{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px}
.rub{display:flex;flex-direction:column;align-items:center;gap:2px;padding:14px 8px;border-radius:14px;color:#fff;font-weight:900;transition:transform .2s var(--ease),filter .2s;box-shadow:inset 0 0 0 1px rgba(255,255,255,.15),inset 0 -14px 24px rgba(0,0,0,.25)}
.rub em{font-style:normal;font-size:13px;opacity:.85}
.rub.r{background:linear-gradient(180deg,#ff4d63,#b3122b)}.rub.k{background:linear-gradient(180deg,#3a3a46,#141419)}.rub.z{background:linear-gradient(180deg,#34d07a,#11803e)}
.rub:hover{transform:translateY(-2px);filter:brightness(1.12)}
.busy .rub{opacity:.5;pointer-events:none}
.puw{position:relative;height:300px;border-radius:14px;overflow:hidden;background:radial-gradient(80% 90% at 50% 100%,rgba(30,200,120,.12),transparent 70%),#07090a;box-shadow:inset 0 0 0 1px rgba(255,255,255,.08);transition:box-shadow .3s}
.puw.live{box-shadow:inset 0 0 0 1px rgba(80,255,160,.35),0 0 30px -12px rgba(60,255,140,.6)}
.puw.dead{box-shadow:inset 0 0 0 1px rgba(255,60,80,.6);background:radial-gradient(80% 90% at 50% 100%,rgba(255,40,60,.15),transparent 70%),#0a0607}
.puw.won{box-shadow:inset 0 0 0 1px rgba(255,210,90,.6),0 0 30px -12px rgba(255,190,60,.7)}
.puw canvas{display:block;width:100%;height:100%}
.puhud{position:absolute;right:18px;top:14px;text-align:right;pointer-events:none}
.puhud b{display:block;font-size:46px;font-weight:900;letter-spacing:-.02em;font-variant-numeric:tabular-nums;text-shadow:0 4px 24px rgba(0,0,0,.6)}
.puw.dead .puhud b{color:#ff5a6e}.puw.won .puhud b{color:#ffd45a}
.puhud span{display:block;margin-top:2px;font-size:13px;color:rgba(255,255,255,.75)}
.puhud span b{display:inline;font-size:13px;color:#7af0a0;text-shadow:none}
.pubtn{display:block;width:100%;height:54px;margin:4px 0 6px;border-radius:14px;font-size:16px;font-weight:900;letter-spacing:.04em;color:#fff;background:linear-gradient(180deg,var(--g2),var(--g0));box-shadow:0 10px 26px -10px rgba(var(--ar),.8);transition:transform .15s var(--ease)}
.pubtn:active{transform:scale(.98)}
.pubtn.cash{color:#2a1500;background:linear-gradient(180deg,#ffe082,#f2a922);box-shadow:0 10px 30px -8px rgba(255,180,40,.9);animation:puCash .6s ease-in-out infinite alternate}
@keyframes puCash{to{box-shadow:0 10px 40px -4px rgba(255,200,60,1)}}
.pubtn.shake,.rub.shake{animation:shake .4s}
.ashb{display:inline-flex;align-items:center;gap:8px;margin-top:12px;height:38px;padding:0 16px;border-radius:999px;font-size:13px;font-weight:800;color:#fff;background:rgba(var(--ar),.2);box-shadow:inset 0 0 0 1px rgba(var(--ar),.5);transition:background .2s,transform .2s var(--ease)}
.ashb:hover{background:rgba(var(--ar),.35);transform:translateY(-1px)}
.ashb svg{width:16px;height:16px}
.albsh{flex:none;width:26px;height:26px;margin-left:6px;border-radius:50%;display:grid;place-items:center;color:var(--sub);background:rgba(255,255,255,.07);transition:color .2s,background .2s}
.albsh:hover{color:#fff;background:rgba(var(--ar),.35)}
.albsh svg{width:14px;height:14px}
.albsh[hidden]{display:none}
.shm{margin-bottom:-4px}
.shm .psb[disabled]{opacity:.35;pointer-events:none}
.pubtn[disabled]{opacity:.55;filter:grayscale(.6);cursor:default;animation:none}
/* предметы за достижения */
.mnm[data-nm=nq_perfect]{background:linear-gradient(100deg,#ffffff 0%,#9be9ff 20%,#ffffff 32%,#ffd45a 50%,#ffffff 66%,#9be9ff 82%,#ffffff 100%);background-size:260% 100%;-webkit-background-clip:text;background-clip:text;color:transparent;animation:nmSheen 3s linear infinite;filter:drop-shadow(0 0 8px rgba(155,233,255,.7)) drop-shadow(0 0 2px rgba(255,212,90,.9))}
.mav[data-fr=fq_legend] .mav-r{--rw:17%;background:radial-gradient(farthest-side,transparent calc(100% - 17%),#ffffff calc(100% - 16.4%),#9be9ff calc(100% - 14.6%),transparent calc(100% - 13.8%),transparent calc(100% - 3%),#ffd45a calc(100% - 2.2%),#a8650c 100%),
  repeating-conic-gradient(from 0deg,#e8f6ff 0 6deg,#9fb6cc 6deg 9deg,#ffffff 9deg 12deg,#ffd45a 12deg 14deg,#c7d2fe 14deg 20deg);animation:frSpin 14s linear infinite}
.mav[data-fr=fq_legend]{--fgc:rgba(155,233,255,.75)}
.mav[data-fr=fq_legend] .mav-g::before{content:"";position:absolute;inset:-12%;border-radius:50%;background:repeating-conic-gradient(rgba(255,212,90,.9) 0 3deg,transparent 3deg 22.5deg);-webkit-mask:radial-gradient(farthest-side,transparent 82%,#000 84%,#000 97%,transparent 99%);mask:radial-gradient(farthest-side,transparent 82%,#000 84%,#000 97%,transparent 99%);animation:frSpin 22s linear infinite reverse}
.bnr[data-bn=bq_lvl]{background:radial-gradient(1.4px 1.4px at 12% 18%,#fff,transparent),radial-gradient(1px 1px at 30% 8%,#fff,transparent),radial-gradient(1.2px 1.2px at 70% 14%,#fff,transparent),radial-gradient(1px 1px at 88% 26%,#fff,transparent),
  radial-gradient(45% 60% at 50% 105%,rgba(255,190,80,.85),transparent 70%),linear-gradient(180deg,#0b1030 0%,#3a1f5c 55%,#c45a2a 100%)}
.bnr[data-bn=bq_lvl] .bnr-a{background:linear-gradient(135deg,transparent 49.6%,#0a0c18 50%) 0 100%/50% 70% no-repeat,linear-gradient(-135deg,transparent 49.6%,#0a0c18 50%) 100% 100%/50% 70% no-repeat,
  linear-gradient(135deg,transparent 49.6%,#141a33 50%) 10% 100%/36% 46% no-repeat,linear-gradient(-135deg,transparent 49.6%,#141a33 50%) 90% 100%/36% 46% no-repeat}
.bnr[data-bn=bq_lvl] .bnr-b{background:radial-gradient(4px 4px at 50% 30%,#fff3c4,transparent),radial-gradient(14px 14px at 50% 30%,rgba(255,212,90,.5),transparent);animation:frBreath 2.4s ease-in-out infinite}

/* минки */
.gcard.wide{width:min(94vw,640px)}
.mist{margin-left:auto;display:flex;align-items:center;gap:6px}
.mist span{font-size:12px}
.mist b{color:#7af0a0;font-size:16px}
.miw{position:relative;display:grid;place-items:center;padding:10px 0 4px}
.mig{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;width:min(100%,380px)}
.mig.shake{animation:shake .4s}
.mic{position:relative;aspect-ratio:1;border-radius:12px;background:linear-gradient(180deg,rgba(255,255,255,.1),rgba(255,255,255,.04));box-shadow:inset 0 0 0 1px rgba(255,255,255,.1),inset 0 -6px 12px rgba(0,0,0,.25);transition:transform .15s var(--ease),background .2s,box-shadow .2s}
.mic:not([disabled]):hover{transform:translateY(-2px);background:linear-gradient(180deg,rgba(var(--ar),.35),rgba(var(--ar),.12));box-shadow:inset 0 0 0 1px rgba(var(--ar),.6),0 6px 16px -6px rgba(var(--ar),.8)}
.mic[disabled]{cursor:default}
.mic i{position:absolute;inset:22%;border-radius:6px;opacity:0;transform:scale(.3);transition:transform .3s cubic-bezier(.3,1.6,.5,1),opacity .2s}
.mic.ok,.mic.gm{background:linear-gradient(180deg,rgba(40,220,130,.28),rgba(20,120,70,.2));box-shadow:inset 0 0 0 1px rgba(80,255,160,.55)}
.mic.ok i,.mic.gm i{opacity:1;transform:scale(1) rotate(45deg);background:linear-gradient(135deg,#d9fff0,#3fe0a0 55%,#0e8a5c);box-shadow:0 0 14px rgba(63,224,160,.8)}
.mic.bm{background:linear-gradient(180deg,rgba(255,60,80,.3),rgba(120,10,25,.3));box-shadow:inset 0 0 0 1px rgba(255,80,100,.6)}
.mic.bm i{opacity:1;transform:scale(1);border-radius:50%;inset:26%;background:radial-gradient(circle at 35% 30%,#666,#111 60%);box-shadow:0 0 0 3px rgba(255,80,100,.4),0 0 16px rgba(255,40,60,.7)}
.mic.bm i::after{content:"";position:absolute;right:-12%;top:-18%;width:30%;height:30%;border-radius:50%;background:#ffd45a;box-shadow:0 0 8px #ff9100}
.mic.rv{opacity:.55}
.mic.boom{animation:miBoom .5s ease-out;opacity:1}
@keyframes miBoom{0%{transform:scale(1)}30%{transform:scale(1.25);box-shadow:0 0 40px 10px rgba(255,60,60,.8)}100%{transform:scale(1)}}
.mihud{text-align:center;margin:8px 0 6px}
.mihud b{display:block;font-size:34px;font-weight:900;font-variant-numeric:tabular-nums}
.mihud span{font-size:13px;color:var(--sub)}
.mihud span b{display:inline;font-size:13px;color:#7af0a0}
.mihud.won>b{color:#ffd45a}.mihud.dead>b{color:#ff5a6e}
.mimn{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:10px;font-size:13px;color:var(--sub)}
.mich{display:flex;gap:4px}
.mich button{height:30px;min-width:34px;padding:0 8px;border-radius:9px;font-size:12.5px;font-weight:800;color:var(--sub);background:rgba(255,255,255,.07)}
.mich button.on{background:var(--g2);color:#fff}
.mimn input{flex:1;min-width:100px;accent-color:var(--g2)}
.mimn>b{min-width:24px;color:#fff;font-size:16px}
.busy .mimn{opacity:.45;pointer-events:none}
.pubtn[disabled]{opacity:.55;cursor:default}
/* автообновление */
#updb{position:fixed;right:22px;bottom:22px;z-index:70;display:flex;align-items:center;gap:14px;max-width:min(560px,calc(100vw - 44px));padding:14px 16px;border-radius:18px;background:linear-gradient(180deg,rgba(36,18,28,.97),rgba(16,8,12,.97));box-shadow:0 24px 60px rgba(0,0,0,.7),inset 0 0 0 1px rgba(var(--ar),.45),0 0 40px -16px rgba(var(--ar),.8);transform:translateY(20px);opacity:0;transition:transform .35s var(--ease),opacity .3s}
#updb.on{transform:none;opacity:1}
#updb[hidden]{display:none}
.updi{flex:none;width:44px;height:44px;border-radius:14px;display:grid;place-items:center;background:rgba(var(--ar),.2);color:var(--g3)}
.updi svg{width:22px;height:22px}
.updi.spin svg{animation:frSpin 1s linear infinite}
.updt{min-width:0}
.updt b{display:block;font-size:15px;font-weight:900}
.updt span{display:block;margin-top:2px;font-size:13px;color:rgba(255,255,255,.8)}
.updt em{display:block;margin-top:3px;font-style:normal;font-size:11.5px;color:var(--sub)}
.upda{display:flex;flex-direction:column;gap:6px;flex:none}
.upda .gbtn{padding:9px 18px;font-size:13px}
.updbtn{flex:none;height:34px;padding:0 14px;border-radius:999px;font-size:12.5px;font-weight:800;color:#fff;background:rgba(255,255,255,.09);box-shadow:inset 0 0 0 1px rgba(255,255,255,.14)}
.updbtn:hover{background:rgba(255,255,255,.16)}
.updbtn.hot{background:var(--g2);box-shadow:none}
.updurl{display:grid;grid-template-columns:1fr auto;gap:6px 8px;margin:4px 0 10px;font-size:12px;color:var(--sub)}
.updurl span{grid-column:1/-1}
.updurl input{font:inherit;font-size:13px;color:#fff;padding:9px 12px;border-radius:10px;border:1px solid var(--line);background:rgba(0,0,0,.35);outline:0;min-width:0}

/* plinko */
.plw{position:relative;border-radius:14px;overflow:hidden;background:radial-gradient(70% 60% at 50% 0%,rgba(var(--ar),.14),transparent 70%),#0b080d;box-shadow:inset 0 0 0 1px rgba(255,255,255,.08)}
.plw canvas{display:block;width:100%;height:auto;max-height:54vh;object-fit:contain}
.plopt{display:flex;flex-wrap:wrap;gap:12px;margin:10px 0}
.plopt .mich{align-items:center}
.plopt .mich span{font-size:12.5px;color:var(--sub);margin-right:4px}
.busy .plopt{opacity:1}
/* hilo */
.hlw{display:flex;flex-direction:column;align-items:center;gap:10px;padding:14px 0 6px;border-radius:14px;background:radial-gradient(60% 70% at 50% 40%,rgba(30,140,80,.25),transparent 70%),#08100c;box-shadow:inset 0 0 0 1px rgba(255,255,255,.08)}
.hlrow{display:flex;gap:6px;min-height:42px}
.hmini{position:relative;width:34px;height:42px;border-radius:6px;display:grid;place-items:center;font-size:12px;font-weight:900;background:#f4f1ea;color:#16161a;box-shadow:0 3px 8px rgba(0,0,0,.4)}
.hmini.r{color:#d0102a}
.hmini em{position:absolute;bottom:-14px;font-style:normal;font-size:10px;color:#7af0a0}
.hmini.bad em{color:#ff6b7f}.hmini.bad{outline:2px solid #ff4d6d}
.hlcard{perspective:800px}
.hc{position:relative;width:132px;height:184px;border-radius:14px;display:grid;place-items:center;background:linear-gradient(160deg,#fffdf7,#ebe6da);color:#16161a;box-shadow:0 18px 40px rgba(0,0,0,.55),inset 0 0 0 1px rgba(0,0,0,.08)}
.hc.r{color:#d0102a}
.hc b{font-size:70px;line-height:1}
.hc i{position:absolute;left:12px;top:8px;font-style:normal;font-size:26px;font-weight:900}
.hc i.bt{left:auto;top:auto;right:12px;bottom:8px;transform:rotate(180deg)}
.hc.flip{animation:hcFlip .45s cubic-bezier(.3,1.3,.5,1)}
@keyframes hcFlip{from{transform:rotateY(90deg) translateY(-10px);opacity:.3}to{transform:none;opacity:1}}
.hlcard.lose .hc{animation:hcFlip .45s,hcLose .5s .2s;box-shadow:0 0 0 4px #ff4d6d,0 18px 40px rgba(0,0,0,.55)}
@keyframes hcLose{20%,60%{transform:translateX(-8px)}40%,80%{transform:translateX(8px)}}
.hls{font-size:13.5px;color:var(--sub);text-align:center;min-height:20px}
.hls b{color:#7af0a0}
.hlbtns{display:grid;grid-template-columns:1fr auto 1fr;gap:8px;margin:12px 0 10px}
.hlb{display:flex;flex-direction:column;align-items:center;gap:3px;padding:12px 10px;border-radius:14px;color:#fff;font-weight:900;box-shadow:inset 0 0 0 1px rgba(255,255,255,.14),inset 0 -12px 22px rgba(0,0,0,.25);transition:transform .15s var(--ease),filter .2s}
.hlb em{font-style:normal;font-size:12px;opacity:.85}
.hlb.hi{background:linear-gradient(180deg,#34d07a,#11803e)}.hlb.lo{background:linear-gradient(180deg,#ff4d63,#b3122b)}
.hlb.skip{padding:12px 14px;background:rgba(255,255,255,.08);font-size:13px}
.hlb:not([disabled]):hover{transform:translateY(-2px);filter:brightness(1.1)}
.hlb[disabled]{opacity:.4;cursor:default}

/* ---------- плавность: тяжёлое рисуем только когда оно на экране ---------- */
.itm{content-visibility:auto;contain-intrinsic-width:auto 190px;contain-intrinsic-height:auto 290px}
.kit2{content-visibility:auto;contain-intrinsic-width:auto 340px;contain-intrinsic-height:auto 480px}
.list .item{content-visibility:auto;contain-intrinsic-height:auto 64px}
.rkr{content-visibility:auto;contain-intrinsic-height:auto 72px}
html.bgp .neb{animation-play-state:paused}
/* карточки магазина и гардероба стоят на паузе и оживают при наведении — иначе 150 анимаций крутятся одновременно */
.itm:not(:hover) .ipv *,.itm:not(:hover) .ipv *::before,.itm:not(:hover) .ipv *::after,
.kit2:not(:hover) .kit2-art *,.kit2:not(:hover) .kit2-art *::before,.kit2:not(:hover) .kit2-art *::after,
.itm.r-l:not(:hover)::before,.itm.r-m:not(:hover)::before,.itm.r-x:not(:hover)::before{animation-play-state:paused!important}
.kit2:not(:hover) .bnr>i,.itm:not(:hover) .bnr>i,.kit2:not(:hover) .mav-r,.itm:not(:hover) .mav-r,.kit2:not(:hover) .mav-sp,.itm:not(:hover) .mav-sp{will-change:auto}
html.lowfx .nebs{filter:none!important;transition:none}
html.lowfx .neb{animation-play-state:paused}
html.lowfx2 .nebs,html.lowfx2 #aurora,html.lowfx2 #twinkle,html.lowfx2 #stars,html.lowfx2 #dfx{display:none!important}
html.lowfx2 *,html.lowfx2 *::before,html.lowfx2 *::after{-webkit-backdrop-filter:none!important;backdrop-filter:none!important}
html.lowfx .itm .ipv *,html.lowfx .kit2-art *{will-change:auto!important}
body.sdrag .main{transition:opacity .4s ease,transform .55s var(--ease)}
</style>
</head>
<body>
<div id="intro" aria-hidden="true">
  <div class="ibg"></div><div class="ig"></div><div class="ihalo"></div>
  <div class="ipt" id="iPt"></div><div class="ibk" id="iBk"></div>
  <div class="irg" style="--d:.25s"></div><div class="irg" style="--d:.55s"></div><div class="irg" style="--d:.85s"></div>
  <svg class="iecg" viewBox="0 0 1000 200" preserveAspectRatio="none"><defs><linearGradient id="iEg" x1="0" x2="1" y1="0" y2="0"><stop offset="0" style="stop-color:rgba(var(--ar),0)"/><stop offset=".3" style="stop-color:var(--g2)"/><stop offset=".5" style="stop-color:#fff"/><stop offset=".7" style="stop-color:var(--g2)"/><stop offset="1" style="stop-color:rgba(var(--ar),0)"/></linearGradient></defs>
    <path class="ie1" pathLength="1" d="M0 100 L370 100 L395 100 L410 78 L425 100 L448 100 L462 18 L482 182 L500 100 L520 100 L536 84 L552 100 L585 100 L600 92 L612 100 L1000 100"/>
    <path class="ie2" pathLength="1" d="M0 100 L370 100 L395 100 L410 78 L425 100 L448 100 L462 18 L482 182 L500 100 L520 100 L536 84 L552 100 L585 100 L600 92 L612 100 L1000 100"/></svg>
  <div class="ishk"></div><div class="iflash"></div><div class="iflr"></div>
  <div class="icn c1"></div><div class="icn c2"></div><div class="icn c3"></div><div class="icn c4"></div>
  <div class="ibar t"></div><div class="ibar b"></div>
  <div class="ic">
    <div class="ieb">Track · Rating · Studio</div>
    <div class="iwm"><div class="ib"><span class="iL" style="--k:0">P</span><span class="iL" style="--k:1">U</span><span class="iL" style="--k:2">L</span><span class="iL" style="--k:3">S</span><span class="iL" style="--k:4">E</span><span class="iL" style="--k:5">&nbsp;</span><span class="iL a" style="--k:6">R</span><span class="iL a" style="--k:7">A</span><span class="iL a" style="--k:8">T</span><span class="iL a" style="--k:9">E</span><span class="iL a" style="--k:10">R</span></div><div class="ish">PULSE&nbsp;RATER</div></div>
    <div class="itg">оценивай музыку по-настоящему</div>
    <div class="ipr"><i></i></div>
    <div class="ipct"></div>
  </div>
  <div class="ivg"></div><div class="igr"></div>
  <div class="icr">Автор - влад сантана</div>
</div>
<script>
(function(){
  var el=document.getElementById("intro");if(!el) return;
  var pt=document.getElementById("iPt");
  if(pt){var h="";for(var i=0;i<38;i++){var r=Math.random;
    h+='<i style="--x:'+(r()*100).toFixed(1)+'%;--y:'+(20+r()*75).toFixed(1)+'%;--s:'+(1+r()*2.6).toFixed(1)+'px;--t:'+(2.2+r()*2.4).toFixed(2)+'s;--w:'+(r()*1.6).toFixed(2)+'s"></i>'}
    pt.innerHTML=h}
  var bk=document.getElementById("iBk");
  if(bk){var b="";for(var j=0;j<9;j++){var q=Math.random;
    b+='<i style="--x:'+(6+q()*88).toFixed(1)+'%;--y:'+(18+q()*70).toFixed(1)+'%;--s:'+(70+q()*190).toFixed(0)+'px;--o:'+(.25+q()*.4).toFixed(2)+';--dx:'+((q()-.5)*90).toFixed(0)+'px;--t:'+(2.4+q()*1.4).toFixed(2)+'s;--w:'+(.2+q()*1.4).toFixed(2)+'s"></i>'}
    bk.innerHTML=b}
  if(window.ST&&ST.intro===false){if(el.parentNode) el.parentNode.removeChild(el);document.body.classList.add("skipped");return}
  var done=false;
  function end(){if(done) return;done=true;if(el.parentNode) el.parentNode.removeChild(el)}
  function skip(){
    if(done||el.classList.contains("skip")) return;
    document.body.classList.add("skipped");el.classList.add("skip");setTimeout(end,450);if(window.introSound) window.introSound.stop();
  }
  el.addEventListener("animationend",function(e){if(e.target===el&&e.animationName==="iOut") end()});
  window.addEventListener("pointerdown",skip,{once:true});
  window.addEventListener("keydown",skip,{once:true});
  setTimeout(end,3800);
})();
</script>
<script>
/* звук интро: синтезируется на лету (Web Audio), расписан по той же шкале времени, что и CSS-анимация */
(function(){
  var AC=window.AudioContext||window.webkitAudioContext;
  if(!AC||matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  if(window.ST&&(ST.intro===false||ST.sounds===false)) return;
  var VOL=.8;                                   // общая громкость интро, 0…1
  var ctx;try{ctx=new AC({latencyHint:"interactive"})}catch(e){return}
  var p0=performance.now(),t0=0,stopped=false;
  var master=ctx.createGain();master.gain.value=0;
  var comp=ctx.createDynamicsCompressor();
  comp.threshold.value=-16;comp.knee.value=18;comp.ratio.value=5;comp.attack.value=.01;comp.release.value=.25;
  master.connect(comp);comp.connect(ctx.destination);
  var bus=ctx.createGain();bus.connect(master);
  var send=ctx.createGain();send.gain.value=.5;bus.connect(send);
  var rev=ctx.createConvolver(),wet=ctx.createGain();wet.gain.value=.9;
  (function(){                                   // синтетическая «комната»: затухающий шум
    var n=ctx.sampleRate*2.8|0,b=ctx.createBuffer(2,n,ctx.sampleRate);
    for(var c=0;c<2;c++){var d=b.getChannelData(c),lp=0;for(var i=0;i<n;i++){lp+=((Math.random()*2-1)-lp)*.3;d[i]=lp*Math.pow(1-i/n,2.6)}}
    rev.buffer=b;
  })();
  send.connect(rev);rev.connect(wet);wet.connect(master);
  var nb=ctx.createBuffer(1,ctx.sampleRate*2,ctx.sampleRate),nd=nb.getChannelData(0);
  for(var i=0;i<nd.length;i++) nd[i]=Math.random()*2-1;

  var at=function(T){return t0+T};
  var late=function(s){return s<ctx.currentTime-.05};
  function out(g,wetAmt,pan){
    var dst=g;
    if(pan&&ctx.createStereoPanner){var sp=ctx.createStereoPanner();sp.pan.value=pan;g.connect(sp);dst=sp}
    dst.connect(bus);
    if(wetAmt){var w=ctx.createGain();w.gain.value=wetAmt;dst.connect(w);w.connect(send)}
  }
  function tone(T,f,dur,o){
    o=o||{};var s=at(T);if(late(s)) return;
    var os=ctx.createOscillator(),g=ctx.createGain(),a=o.a||.01,v=o.v||.1;
    os.type=o.type||"sine";os.frequency.setValueAtTime(f,s);
    if(o.to) os.frequency.exponentialRampToValueAtTime(o.to,s+(o.glide||dur));
    if(o.det) os.detune.value=o.det;
    g.gain.setValueAtTime(.0001,s);
    g.gain.exponentialRampToValueAtTime(v,s+a);
    g.gain.exponentialRampToValueAtTime(.0001,s+dur);
    if(o.lp){var fl=ctx.createBiquadFilter();fl.type="lowpass";fl.frequency.value=o.lp;os.connect(fl);fl.connect(g)}
    else os.connect(g);
    out(g,o.wet,o.pan);os.start(s);os.stop(s+dur+.05);
  }
  function noise(T,dur,f0,f1,v,o){
    o=o||{};var s=at(T);if(late(s)) return;
    var src=ctx.createBufferSource(),fl=ctx.createBiquadFilter(),g=ctx.createGain();
    src.buffer=nb;src.loop=true;
    fl.type=o.type||"bandpass";fl.Q.value=o.q||1.2;
    fl.frequency.setValueAtTime(f0,s);fl.frequency.exponentialRampToValueAtTime(f1,s+dur);
    g.gain.setValueAtTime(.0001,s);
    g.gain.exponentialRampToValueAtTime(v,s+dur*(o.peak||.45));
    g.gain.exponentialRampToValueAtTime(.0001,s+dur);
    src.connect(fl);fl.connect(g);out(g,o.wet,o.pan);src.start(s,Math.random());src.stop(s+dur+.05);
  }

  function schedule(){
    /* 0.1 — свечение: глубокий гул, нарастает до появления логотипа */
    tone(.1,55,3.2,{a:1.1,v:.34});
    tone(.1,82.41,3.2,{a:1.2,v:.1});
    tone(.1,55,3.2,{a:1.1,v:.1,type:"sawtooth",lp:150});

    /* 0.25 / 0.55 / 0.85 — три кольца: тёплые низкие «сонарные» ноты и глухой шелест */
    [[.25,164.81],[.55,196],[.85,246.94]].forEach(function(r,i){
      tone(r[0],r[1],2,{a:.02,v:.2,wet:.8,pan:(i-1)*.35,lp:900});
      tone(r[0],r[1]*2,1.4,{a:.02,v:.03,wet:.8,pan:(i-1)*.35,lp:1200});
      noise(r[0],1.4,900,220,.05,{q:.7,wet:.6,pan:(i-1)*.35});
    });

    /* 0.35→1.25 — логотип проявляется: низкий нарастающий гул-свист */
    noise(.35,.9,180,1700,.17,{q:1.6,peak:.95,wet:.4});
    tone(.35,82.41,.9,{to:246.94,a:.5,v:.1,glide:.9,wet:.5,lp:800});

    /* 1.25 — логотип встаёт на место: глубокий удар и бархатный аккорд Em9 */
    tone(1.25,90,1.1,{to:32,glide:.5,a:.005,v:.6});
    noise(1.25,.25,1200,150,.12,{type:"lowpass",q:.7,peak:.05});
    [82.41,123.47,196,246.94,293.66].forEach(function(f,i){
      tone(1.25,f,2.9,{a:.3,v:.09,type:"triangle",lp:900,det:i%2?6:-6,wet:.7,pan:(i-2)*.2});
    });
    tone(1.3,659.25,2,{a:.03,v:.025,wet:1,lp:1500});

    /* 1.05→2.3 — заполнение полоски: шесть низких нот пентатоники E-moll вверх */
    [164.81,196,220,246.94,293.66,329.63].forEach(function(f,i){
      var T=1.05+i*.25;
      tone(T,f,1,{a:.005,v:.16,type:"triangle",lp:1600,wet:.5,pan:(i-2.5)*.12});
      tone(T,f*2,.4,{a:.005,v:.02,lp:1800,wet:.5});
    });

    /* 2.5 — полоска заполнена: глубокий «колокол» и вылет интро */
    [164.81,246.94,329.63].forEach(function(f,i){tone(2.5,f,2.8,{a:.015,v:.15-i*.03,wet:.8,lp:1400})});
    noise(2.55,.85,250,2800,.15,{q:1.1,peak:.5,wet:.5});

    /* 2.65 / 2.8 — панели встают на места слева и снизу */
    noise(2.65,.7,1400,300,.08,{q:.9,peak:.3,pan:-.55,wet:.3});
    noise(2.8,.75,1200,260,.07,{q:.9,peak:.3,pan:.55,wet:.3});
    tone(2.8,70,.7,{to:42,a:.01,v:.2});
  }

  function start(){
    if(stopped) return;
    var off=(performance.now()-p0)/1000,n=ctx.currentTime;
    t0=n-off;
    master.gain.setValueAtTime(0,n);master.gain.linearRampToValueAtTime(VOL,n+.08);
    schedule();
  }
  if(ctx.state==="running") start();
  else ctx.resume().then(function(){if(ctx.state==="running") start()}).catch(function(){});

  window.introSound={stop:function(){
    stopped=true;
    try{var n=ctx.currentTime;master.gain.cancelScheduledValues(n);master.gain.setValueAtTime(master.gain.value,n);master.gain.linearRampToValueAtTime(0,n+.25)}catch(e){}
    setTimeout(function(){try{ctx.close()}catch(e){}},400);
  }};
  setTimeout(function(){try{ctx.close()}catch(e){}},7500);
})();
</script>
<div id="st" hidden role="dialog" aria-modal="true" aria-label="Настройки">
  <div class="stp">
    <div class="sth"><div><h2>Настройки</h2><p>Всё применяется сразу и запоминается</p></div>
      <button class="pround" id="stClose" type="button" title="Закрыть (Esc)"><svg class="ico" viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg></button></div>
    <div class="stb" id="stBody"></div>
  </div>
</div>
<div id="plat" hidden role="dialog" aria-modal="true" aria-label="Выбор площадки">
  <div class="qcard pcard"><b id="plH">Слушать целиком</b><p id="plT"></p>
    <div class="plgrid" id="plG"></div>
    <button class="plemb" id="plE" type="button">Или открыть встроенный плеер (YouTube, SoundCloud…)</button>
    <div class="qh">Откроется в твоём браузере. Послушай и возвращайся оценивать · Esc — закрыть</div></div>
</div>
<div id="emb" hidden role="dialog" aria-modal="true" aria-label="Полное прослушивание">
  <div class="ebox">
    <div class="ehd"><div class="et"><b id="eT"></b><span id="eA"></span></div><button class="pround" id="eX" type="button" title="Закрыть (Esc)"><svg class="ico" viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg></button></div>
    <div class="etabs" id="eTabs"></div>
    <div class="efr" id="eFr"></div>
    <div class="ectl" id="eCtl" hidden>
      <button class="apb" id="ePl" type="button" title="Играть / пауза"></button>
      <i id="eCur">0:00</i><input class="rng" id="eSeek" type="range" min="0" max="1000" value="0" aria-label="Перемотка"><i id="eDur">0:00</i>
      <div class="vol"><svg class="ico" viewBox="0 0 24 24"><path d="M11 5L6 9H2v6h4l5 4V5z"/><path d="M15.5 8.5a5 5 0 0 1 0 7M18.5 5.5a9 9 0 0 1 0 13"/></svg><input class="rng" id="eVol" type="range" min="0" max="100" value="80" aria-label="Громкость"></div>
    </div>
    <div class="enote" id="eNote"></div>
    <div class="enote" id="eFail" hidden>Плеер пустой или не грузится? <a id="eOpen" target="_blank" rel="noopener">Открыть этот трек на сайте площадки</a></div>
    <div class="epaste"><input id="eIn" type="text" placeholder="Вставь ссылку на трек: Яндекс Музыка, YouTube, SoundCloud, Deezer, Apple Music" autocomplete="off" spellcheck="false"><button id="eGo" type="button">Открыть здесь</button></div>
    <div class="eext">Нет ссылки? Найди трек на сайте и скопируй её сюда: <a id="eY" target="_blank" rel="noopener">Яндекс Музыка</a><a id="eV" target="_blank" rel="noopener">VK Музыка</a><a id="eYT" target="_blank" rel="noopener">YouTube</a></div>
  </div>
</div>
<div id="qc" hidden role="dialog" aria-modal="true" aria-label="Выход">
  <div class="qcard"><div class="qic"><svg class="ico" viewBox="0 0 24 24"><path d="M12 2v10"/><path d="M18.4 6.6a9 9 0 1 1-12.8 0"/></svg></div>
    <b>Вы уверены, что хотите выйти?</b><p>Приложение закроется.</p>
    <div class="qbt"><button class="qno" id="qNo" type="button">Остаться</button><button class="qyes" id="qYes" type="button">Выйти</button></div>
    <div class="qh">Esc — остаться · Enter — выйти</div></div>
</div>
<div id="bc" hidden role="dialog" aria-modal="true" aria-label="Подтверждение покупки">
  <div class="qcard bcard"><div class="qic"><svg class="coin" viewBox="0 0 64 64" aria-hidden="true"><use href="#pcoin"/></svg></div>
    <b id="bcTitle">Купить?</b><p class="bcn" id="bcName"></p><div class="bcs" id="bcSub"></div>
    <div class="bcrows"><div class="bcr"><span>Цена</span><b id="bcPrice"></b></div><div class="bcr"><span>Останется на балансе</span><b id="bcLeft"></b></div></div>
    <div class="qbt"><button class="qno" id="bcNo" type="button">Отмена</button><button class="qyes" id="bcYes" type="button">Купить</button></div>
    <div class="qh">Esc — отмена · Enter — купить</div></div>
</div>
<div id="outro" aria-hidden="true"><div class="og"></div>
  <div class="oc"><div class="ot">PULSE RATER</div><div class="obw"><i style="--h:30;--k:0"></i><i style="--h:52;--k:1"></i><i style="--h:40;--k:2"></i><i style="--h:68;--k:3"></i><i style="--h:56;--k:4"></i><i style="--h:84;--k:5"></i><i style="--h:62;--k:6"></i><i style="--h:100;--k:7"></i><i style="--h:74;--k:8"></i><i style="--h:92;--k:9"></i><i style="--h:66;--k:10"></i><i style="--h:100;--k:11"></i><i style="--h:80;--k:12"></i><i style="--h:58;--k:13"></i><i style="--h:96;--k:14"></i><i style="--h:70;--k:15"></i><i style="--h:88;--k:16"></i><i style="--h:54;--k:17"></i><i style="--h:76;--k:18"></i><i style="--h:44;--k:19"></i><i style="--h:60;--k:20"></i><i style="--h:38;--k:21"></i><i style="--h:50;--k:22"></i><i style="--h:32;--k:23"></i><i style="--h:42;--k:24"></i><i style="--h:28;--k:25"></i><i style="--h:36;--k:26"></i><i style="--h:24;--k:27"></i></div><div class="os">До встречи</div></div>
  <div class="ol"></div></div>
<div class="nebs"><div class="base"></div></div>
<canvas id="aurora"></canvas>
<canvas id="twinkle"></canvas>
<canvas id="fxTrail"></canvas>
<div id="credit">Автор - влад сантана</div>
<div id="fxGlow"><i></i></div>
<canvas id="stars"></canvas>
<canvas id="dfx"></canvas>
<div id="welcome" role="dialog" aria-modal="true" aria-label="Приветствие">
  <div class="wl-c">
    <section class="wl-step on" id="wlHi">
      <div class="wl-eb" style="--i:0"><span class="eq"><i></i><i></i><i></i><i></i></span>Добро пожаловать</div>
      <h1 class="wl-h" style="--i:1"><span class="wl-brand">PULSE RATER</span></h1>
      <p class="wl-p" style="--i:2">Оценивай треки и целые альбомы по пяти критериям: уникальность, сведение, текст, реализация и вайб.</p>
      <div class="wl-act" style="--i:3"><button class="wl-go" id="wlStart" type="button">Начать <svg class="ico" viewBox="0 0 24 24"><path d="M5 12h14M13 6l6 6-6 6"/></svg></button></div>
      <div class="wl-hint" style="--i:4">или нажми Enter</div>
    </section>
    <section class="wl-step" id="wlPick">
      <h2 class="wl-t" style="--i:0">Что будем оценивать?</h2>
      <p class="wl-p" style="--i:1">Выбери формат, с которого начнём.</p>
      <div class="wl-cards" style="--i:2">
        <button class="wl-card" data-k="single" style="--hh:352" type="button"><span class="wl-ic"><svg class="ico" viewBox="0 0 24 24"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg></span><b>Сингл</b><p>Один трек. Оцени его по пяти критериям и добавь в медиатеку.</p></button>
        <button class="wl-card" data-k="album" style="--hh:14" type="button"><span class="wl-ic"><svg class="ico" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="3"/><path d="M12 3a9 9 0 0 1 9 9" stroke-opacity=".5"/></svg></span><b>Альбом</b><p>Несколько треков. Оценишь каждый по очереди, а в конце получишь общую оценку альбома.</p></button>
      </div>
      <div style="--i:3"><button class="wl-back" data-back="hi" type="button"><svg class="ico" viewBox="0 0 24 24"><path d="M19 12H5M12 19l-7-7 7-7"/></svg>Назад</button></div>
    </section>
    <section class="wl-step" id="wlAlb">
      <h2 class="wl-t" style="--i:0">Расскажи про альбом</h2>
      <p class="wl-p" style="--i:1">Название и исполнителя можно будет поменять позже.</p>
      <div class="wl-form" style="--i:2">
        <label>Название альбома<input class="wl-in" id="wlAlbName" placeholder="Новый альбом" autocomplete="off" maxlength="60"></label>
        <label>Исполнитель<input class="wl-in" id="wlAlbArt" placeholder="Необязательно" autocomplete="off" maxlength="60"></label>
      </div>
      <div class="wl-btns" style="--i:3"><button class="wl-go" id="wlUp" type="button"><svg class="ico" viewBox="0 0 24 24"><path d="M12 16V4M7 9l5-5 5 5"/><path d="M4 16v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3"/></svg>Загрузить треки</button><button class="wl-ghost" id="wlManual" type="button">Добавлю вручную</button></div>
      <div class="wl-hint" style="--i:4">MP3, WAV или FLAC — выбери сразу несколько файлов</div>
      <div style="--i:5"><button class="wl-back" data-back="pick" type="button"><svg class="ico" viewBox="0 0 24 24"><path d="M19 12H5M12 19l-7-7 7-7"/></svg>Назад</button></div>
    </section>
  </div>
</div>
<svg width="0" height="0" style="position:absolute;width:0;height:0" aria-hidden="true" focusable="false"><defs>
 <linearGradient id="pc-rim" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#fff4c9"/><stop offset=".3" stop-color="#ffcf55"/><stop offset=".65" stop-color="#df8f14"/><stop offset="1" stop-color="#8c4f05"/></linearGradient>
 <radialGradient id="pc-face" cx=".36" cy=".3" r=".85"><stop offset="0" stop-color="#ffefb5"/><stop offset=".45" stop-color="#ffc23f"/><stop offset="1" stop-color="#c47a0e"/></radialGradient>
 <linearGradient id="pc-in" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#8a4c04"/><stop offset="1" stop-color="#fff0b8"/></linearGradient>
 <symbol id="pcoin" viewBox="0 0 64 64">
  <circle cx="32" cy="32" r="31" fill="url(#pc-rim)"/>
  <circle cx="32" cy="32" r="30.4" fill="none" stroke="#6e3a03" stroke-opacity=".55" stroke-width="1.2"/>
  <circle cx="32" cy="32" r="27.2" fill="none" stroke="#fff3c4" stroke-opacity=".6" stroke-width="1.6" stroke-dasharray="1.4 2.1"/>
  <circle cx="32" cy="32" r="23.6" fill="url(#pc-face)"/>
  <circle cx="32" cy="32" r="23.6" fill="none" stroke="url(#pc-in)" stroke-width="1.8"/>
  <path d="M13.5 34 H21.5 L25 26.5 L29.8 41.5 L34.8 20.5 L38.5 34 H50.5" fill="none" stroke="#fff6d6" stroke-opacity=".8" stroke-width="4.6" stroke-linecap="round" stroke-linejoin="round" transform="translate(0 1.2)"/>
  <path d="M13.5 34 H21.5 L25 26.5 L29.8 41.5 L34.8 20.5 L38.5 34 H50.5" fill="none" stroke="#7a3f03" stroke-width="4.4" stroke-linecap="round" stroke-linejoin="round"/>
  <path d="M11.5 24 A22.5 22.5 0 0 1 34 9.6" fill="none" stroke="#fff" stroke-opacity=".75" stroke-width="2.4" stroke-linecap="round"/>
  <ellipse cx="21" cy="16.5" rx="6.5" ry="2.8" transform="rotate(-38 21 16.5)" fill="#fff" opacity=".45"/>
 </symbol>
 <linearGradient id="hg-gold" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#fff3c4"/><stop offset=".35" stop-color="#ffd45a"/><stop offset=".7" stop-color="#d99a1c"/><stop offset="1" stop-color="#a8650c"/></linearGradient>
 <linearGradient id="hg-gold2" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#ffe9a8"/><stop offset=".5" stop-color="#e9ac2c"/><stop offset="1" stop-color="#a8650c"/></linearGradient>
 <linearGradient id="hg-silver" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#ffffff"/><stop offset=".5" stop-color="#cfd6e2"/><stop offset="1" stop-color="#8a93a3"/></linearGradient>
 <linearGradient id="hg-red" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#ff6a6a"/><stop offset=".55" stop-color="#d0182e"/><stop offset="1" stop-color="#7a0718"/></linearGradient>
 <linearGradient id="hg-pink" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#ffe0ec"/><stop offset=".5" stop-color="#ff9cbe"/><stop offset="1" stop-color="#e0558a"/></linearGradient>
 <linearGradient id="hg-dark" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#55555f"/><stop offset="1" stop-color="#18181d"/></linearGradient>
 <linearGradient id="hg-steel" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#f2f6fb"/><stop offset=".45" stop-color="#a7b2c2"/><stop offset="1" stop-color="#525d6d"/></linearGradient>
 <linearGradient id="hg-indigo" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#8a6cff"/><stop offset=".5" stop-color="#4425b0"/><stop offset="1" stop-color="#1b0c52"/></linearGradient>
 <linearGradient id="hg-brown" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#d49a5a"/><stop offset=".55" stop-color="#8f5826"/><stop offset="1" stop-color="#5a3412"/></linearGradient>
 <linearGradient id="hg-white" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#ffffff"/><stop offset=".6" stop-color="#f1f3f8"/><stop offset="1" stop-color="#c9cdd8"/></linearGradient>
 <linearGradient id="hg-cyan" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#f2fdff"/><stop offset=".5" stop-color="#8ce6ff"/><stop offset="1" stop-color="#3bb4e6"/></linearGradient>
 <linearGradient id="hg-mint" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#8af5c8"/><stop offset=".6" stop-color="#2fbf8a"/><stop offset="1" stop-color="#16825a"/></linearGradient>
 <linearGradient id="hg-pastel" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#fff6c4"/><stop offset="1" stop-color="#ffd36b"/></linearGradient>
 <linearGradient id="hg-fire" x1="0" y1="1" x2="0" y2="0"><stop offset="0" stop-color="#ff2d0a"/><stop offset=".55" stop-color="#ff8a1e"/><stop offset="1" stop-color="#ffd35a"/></linearGradient>
 <linearGradient id="hx-ruby" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#ffe0e8"/><stop offset=".35" stop-color="#ff4d6d"/><stop offset=".75" stop-color="#c2002e"/><stop offset="1" stop-color="#6a0018"/></linearGradient>
 <linearGradient id="hx-aqua" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#e6fbff"/><stop offset=".45" stop-color="#2cc6f0"/><stop offset="1" stop-color="#06455f"/></linearGradient>
 <radialGradient id="hx-pearl" cx=".35" cy=".3" r=".8"><stop offset="0" stop-color="#ffffff"/><stop offset=".6" stop-color="#f1f4ff"/><stop offset="1" stop-color="#b9c8e0"/></radialGradient>
 <linearGradient id="hx-rainbow" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#fff" stop-opacity=".9"/><stop offset=".2" stop-color="#ff7ab6"/><stop offset=".4" stop-color="#ffd36b"/><stop offset=".6" stop-color="#7af0c0"/><stop offset=".8" stop-color="#7ab8ff"/><stop offset="1" stop-color="#b37aff" stop-opacity="0"/></linearGradient>
 <linearGradient id="hg-bat" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#5a1024"/><stop offset=".6" stop-color="#2a0712"/><stop offset="1" stop-color="#120208"/></linearGradient>
 <linearGradient id="hg-lacq" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#5a5a66"/><stop offset=".35" stop-color="#1b1b22"/><stop offset="1" stop-color="#050507"/></linearGradient>
 <linearGradient id="hg-phx" x1="0" y1="1" x2="0" y2="0"><stop offset="0" stop-color="#b0002a"/><stop offset=".4" stop-color="#ff6d00"/><stop offset=".8" stop-color="#ffd54f"/><stop offset="1" stop-color="#fffbe6"/></linearGradient>
 <linearGradient id="hg-neb" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#22d3ee"/><stop offset=".5" stop-color="#a78bfa"/><stop offset="1" stop-color="#f472b6"/></linearGradient>
 <radialGradient id="hg-sun" cx=".5" cy=".5" r=".5"><stop offset="0" stop-color="#fffbe6"/><stop offset=".5" stop-color="#ffd54f"/><stop offset="1" stop-color="#ff6d00"/></radialGradient>
 <radialGradient id="hg-pl1" cx=".35" cy=".3" r=".8"><stop offset="0" stop-color="#ffe0b2"/><stop offset=".6" stop-color="#d9893a"/><stop offset="1" stop-color="#6b3a10"/></radialGradient>
 <radialGradient id="hg-pl2" cx=".35" cy=".3" r=".8"><stop offset="0" stop-color="#e0f7ff"/><stop offset=".6" stop-color="#3b82f6"/><stop offset="1" stop-color="#1e3a8a"/></radialGradient>
 <radialGradient id="hg-pl3" cx=".35" cy=".3" r=".8"><stop offset="0" stop-color="#fce7f3"/><stop offset=".6" stop-color="#db2777"/><stop offset="1" stop-color="#6b0f3a"/></radialGradient>
 <linearGradient id="hg-brawl" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#fff176"/><stop offset=".5" stop-color="#ffce1f"/><stop offset="1" stop-color="#f59e0b"/></linearGradient>
 <linearGradient id="hg-olive" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#7a8a46"/><stop offset=".55" stop-color="#4b5a2a"/><stop offset="1" stop-color="#2a3316"/></linearGradient>
 <linearGradient id="hg-horn" x1="1" y1="1" x2="0" y2="0"><stop offset="0" stop-color="#5a5a62"/><stop offset=".45" stop-color="#26262c"/><stop offset=".85" stop-color="#3a0a04"/><stop offset="1" stop-color="#ff6d00"/></linearGradient>
 <linearGradient id="hg-dia" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#ffffff"/><stop offset=".4" stop-color="#dff6ff"/><stop offset=".7" stop-color="#a5d8ff"/><stop offset="1" stop-color="#e8d6ff"/></linearGradient>
 <filter id="hf-glow" x="-40%" y="-80%" width="180%" height="260%"><feGaussianBlur stdDeviation="3"/></filter>
</defs></svg>
<div class="app">
  <aside class="side">
    <nav class="panel nav">
      <div class="brandrow"><button class="brand" id="brand" type="button" title="На главную"><span>PULSE RATER</span></button>
        <button class="balp" id="balPill" type="button" title="Пульсы — открыть магазин"><svg class="coin" viewBox="0 0 64 64" aria-hidden="true"><use href="#pcoin"/></svg><b data-bal>0</b></button>
        <button class="meb" id="meBtn" type="button" title="Мой профиль"></button></div>
      <button class="navbtn" id="navNew"><svg class="ico" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg><span>Новая оценка</span></button>
      <button class="navbtn" id="navArtists"><svg class="ico" viewBox="0 0 24 24"><rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 10v1a7 7 0 0 0 14 0v-1"/><path d="M12 18v4M8 22h8"/></svg><span>Профили артистов</span></button>
      <button class="navbtn" id="navRank"><svg class="ico" viewBox="0 0 24 24"><path d="M8 21h8M12 17v4M7 4h10v5a5 5 0 0 1-10 0V4z"/><path d="M17 5h3v2a3 3 0 0 1-3 3M7 5H4v2a3 3 0 0 0 3 3"/></svg><span>Общий рейтинг</span></button>
      <button class="navbtn" id="navCat"><svg class="ico" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg><span>Каталог релизов</span></button>
      <button class="navbtn" id="navGame"><svg class="ico" viewBox="0 0 24 24"><path d="M6 11h4M8 9v4M15 12h.01M18 10h.01"/><path d="M17.32 5H6.68a4 4 0 0 0-3.98 3.59C2.6 9.42 2 14.46 2 16a3 3 0 0 0 3 3c1 0 1.5-.5 2-1l1.41-1.41A2 2 0 0 1 9.83 16h4.34a2 2 0 0 1 1.41.59L17 18c.5.5 1 1 2 1a3 3 0 0 0 3-3c0-1.55-.6-6.58-.69-7.26A4 4 0 0 0 17.32 5z"/></svg><span>Игры на пульсы</span></button>
      <button class="navbtn" id="navRoom"><svg class="ico" viewBox="0 0 24 24"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/></svg><span>Онлайн</span></button>
      <button class="navbtn" id="navQst" type="button"><svg class="ico" viewBox="0 0 24 24"><path d="M9 11l3 3 8-8"/><path d="M20 12v7a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h9"/></svg><span>Задания</span></button>
      <button class="navbtn" id="navShop" type="button"><svg class="ico" viewBox="0 0 24 24"><path d="M6 2L3 6v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6l-3-4z"/><path d="M3 6h18"/><path d="M16 10a4 4 0 0 1-8 0"/></svg><span>Магазин</span></button>
      <button class="navbtn" id="navSet" type="button"><svg class="ico" viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg><span>Настройки</span></button>
    </nav>
    <section class="panel lib">
      <div class="lib-h">
        <div class="t"><svg class="ico" viewBox="0 0 24 24"><path d="M4 4v16M10 4v16M16 6l4 14"/></svg><span>Моя медиатека</span><em class="cnt" id="cnt">0</em></div>
      </div>
      <div class="plates" id="plates">
        <button class="plate" id="plSingles" type="button"><span class="pi"><svg class="ico" viewBox="0 0 24 24"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg></span><span><b>Синглы</b><em id="plSn">0</em></span></button>
        <button class="plate" id="plEps" type="button"><span class="pi"><svg class="ico" viewBox="0 0 24 24"><rect x="3" y="7" width="13" height="13" rx="2"/><path d="M8 4h11a2 2 0 0 1 2 2v11"/><circle cx="9.5" cy="13.5" r="2"/></svg></span><span><b>EP</b><em id="plEn">0</em></span></button>
        <button class="plate" id="plAlbums" type="button"><span class="pi"><svg class="ico" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="3"/></svg></span><span><b>Альбомы</b><em id="plAn">0</em></span></button>
      </div>
      <div class="cats" id="cats">
        <button class="cat" data-c="artist" type="button"><svg class="ico" viewBox="0 0 24 24"><circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 3.6-7 8-7s8 3 8 7"/></svg>Исполнители</button>
        <button class="cat" data-c="genre" type="button"><svg class="ico" viewBox="0 0 24 24"><path d="M20.6 13.4l-7.2 7.2a2 2 0 0 1-2.8 0L3 13V3h10l7.6 7.6a2 2 0 0 1 0 2.8z"/><circle cx="7.5" cy="7.5" r="1.2"/></svg>Жанры</button>
      </div>
      <div class="csw" id="catWrap"><div class="csi"><div class="cs" id="catSub"></div></div></div>
      <div class="chips" id="chips">
        <button class="chip on" data-f="all">Все</button>
        <button class="chip" data-f="hi">8+</button>
        <button class="chip" data-f="mid">5–7.9</button>
        <button class="chip" data-f="lo">до 5</button>
      </div>
      <div class="srow">
        <div class="sbox"><svg class="ico" viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/></svg><input id="q" placeholder="Поиск в медиатеке" autocomplete="off"></div>
        <button class="sort" id="sort">Недавние</button>
      </div>
      <div class="list" id="list"></div>
    </section>
  </aside>

  <main class="panel main" id="main">
    <div class="hero-bg"></div>
    <div class="tools"><button class="round" id="fs" title="Во весь экран / оконный режим (F11)"><svg class="ico" viewBox="0 0 24 24" id="fsIc"><path d="M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7"/></svg></button></div>

    <section class="hero">
      <div class="cover" id="cover" title="Нажми, чтобы загрузить обложку"><svg class="ico" viewBox="0 0 24 24"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg><div class="covh"><svg class="ico" viewBox="0 0 24 24"><path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="4"/></svg><span>Обложка</span></div><button class="covx" id="covx" type="button" title="Убрать обложку" hidden><svg class="ico" viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg></button></div>
      <div class="meta">
        <div class="eyebrow"><span class="eq" id="eqTop" hidden><i></i><i></i><i></i><i></i></span><span id="eyebrow">Новая оценка</span></div>
        <div class="titlew" id="titlew"><div class="ph"><input class="big" id="title" placeholder="Название трека" autocomplete="off" maxlength="80"><span class="phs pt" aria-hidden="true">Название трека</span></div></div>
        <div class="sub"><span class="ph inl"><input class="art" id="artist" placeholder="Исполнитель" autocomplete="off" maxlength="60"><span class="phs pa" aria-hidden="true">Исполнитель</span></span><span class="prodw"><span class="prod">prod.</span><span class="ph inl"><input class="art beat" id="beat" placeholder="Битмейкер" autocomplete="off" maxlength="60" spellcheck="false"><span class="phs pa pb" aria-hidden="true">битмейкер</span></span></span><span class="dot">•</span><span class="ph inl"><input class="art gen" id="genre" placeholder="Жанр" list="genres" autocomplete="off" maxlength="40"><span class="phs pa" aria-hidden="true">Жанр</span></span><datalist id="genres"></datalist><span class="dot">•</span><span class="ph inl"><input class="art gen" id="album" placeholder="Альбом" list="albums" autocomplete="off" maxlength="60"><span class="phs pa" aria-hidden="true">Альбом</span></span><datalist id="albums"></datalist><span class="dot">•</span><span id="heroScore"><b>5.0</b> из 10</span></div>
      </div>
    </section>

    <section class="content">
      <div class="actions">
        <button class="rate" id="save" type="button" title="Добавить в медиатеку (Ctrl+Enter)"><span class="rbg"></span><span class="rl">Оценить</span></button>
        <button class="rcrit" id="resetCrit" type="button" title="Вернуть ползунки к стартовому значению — название, исполнитель и аудио останутся"><svg class="ico" viewBox="0 0 24 24"><path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6"/></svg>Сбросить критерии</button>
        <button class="ibtn" id="reset" title="Очистить форму (новая оценка)"><svg class="ico" viewBox="0 0 24 24"><path d="M1 4v6h6"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/></svg></button>
        <button class="ibtn" id="shareCur" title="Поделиться: карточка для сторис и Telegram" hidden><svg class="ico" viewBox="0 0 24 24"><path d="M4 12v7a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-7"/><path d="M16 6l-4-4-4 4"/><path d="M12 2v13"/></svg></button>
        <button class="ibtn" id="delCur" title="Удалить эту оценку" hidden><svg class="ico" viewBox="0 0 24 24"><path d="M3 6h18M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg></button>
      </div>

      <div class="au" id="au">
        <div class="drop" id="drop">
          <button class="upbtn" id="pick" type="button"><svg class="ico" viewBox="0 0 24 24"><path d="M12 16V4M7 9l5-5 5 5"/><path d="M4 16v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3"/></svg>Загрузить трек / альбом</button>
          <span>MP3, WAV или FLAC — можно перетащить файл прямо в окно. Выбери или перетащи сразу несколько файлов — получится альбом: оцени каждый трек, и в конце появится общая оценка альбома.</span>
        </div>
        <div class="ap" id="ap" hidden>
          <button class="apsk" id="apPrev" type="button" title="Предыдущий трек" hidden><svg viewBox="0 0 24 24"><path d="M6 5v14M19 5L9 12l10 7z"/></svg></button>
          <button class="apb" id="apPlay" type="button" title="Играть / пауза"></button>
          <button class="apsk" id="apNext" type="button" title="Следующий трек" hidden><svg viewBox="0 0 24 24"><path d="M18 5v14M5 5l10 7-10 7z"/></svg></button>
          <div class="apm">
            <div class="apn"><b id="apName"></b><span id="apMeta"></span></div>
            <div class="apt"><i id="apCur">0:00</i><input class="rng" id="apSeek" type="range" min="0" max="1000" value="0" aria-label="Перемотка"><i id="apDur">0:00</i></div>
          </div>
          <div class="vol"><svg class="ico" viewBox="0 0 24 24"><path d="M11 5L6 9H2v6h4l5 4V5z"/><path d="M15.5 8.5a5 5 0 0 1 0 7M18.5 5.5a9 9 0 0 1 0 13"/></svg><input class="rng" id="apVol" type="range" min="0" max="100" value="100" aria-label="Громкость"></div>
          <button class="apsw" id="apSwap" type="button" hidden>Включить этот трек</button>
          <button class="ibtn apx" id="apX" type="button" title="Убрать файл из этой оценки"><svg class="ico" viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg></button>
          <div class="apvw" aria-hidden="true"><canvas id="apViz"></canvas></div>
        </div>
        <input type="file" id="file" accept=".mp3,.wav,.flac,audio/mpeg,audio/wav,audio/x-wav,audio/flac,audio/x-flac" multiple hidden>
        <div class="fl" id="fl" hidden><button class="flb" id="flOpen" type="button"><svg class="ico" viewBox="0 0 24 24"><path d="M8 5v14l11-7z" fill="currentColor" stroke="none"/></svg>Слушать целиком</button><span>выбери площадку: Яндекс Музыка, VK, YouTube и другие</span></div>
      </div>

      <div class="grid">
        <div>
          <div class="tnav" id="tnav" hidden>
            <button class="tnb" id="navPrev" type="button" title="Предыдущий трек"><svg class="ico" viewBox="0 0 24 24"><path d="M19 12H5M12 19l-7-7 7-7"/></svg>Вернуться</button>
            <button class="tnb nx" id="navNext" type="button" title="Следующий трек">Оценить следующий<svg class="ico" viewBox="0 0 24 24"><path d="M5 12h14M12 5l7 7-7 7"/></svg></button>
          </div>
          <div class="thead"><span>#</span><span>Критерий</span><span>Оценка</span><span>Балл</span></div>
          <div id="rows"></div>
        </div>
        <aside class="score" id="scoreCard">
          <div class="cap">Итоговая оценка</div>
          <div class="ringw">
            <svg viewBox="0 0 200 200">
              <defs><linearGradient id="rg" x1="0" y1="0" x2="1" y2="1">
                <stop offset="0" style="stop-color:hsl(calc(var(--h) + var(--bo)) 80% 62%)"/>
                <stop offset="1" style="stop-color:hsl(calc(var(--h) + var(--bo) - 35) 85% 52%)"/>
              </linearGradient></defs>
              <circle cx="100" cy="100" r="84" fill="none" stroke="#2a2a2a" stroke-width="12"/>
              <circle id="ring" cx="100" cy="100" r="84" fill="none" stroke="url(#rg)" stroke-width="12" stroke-linecap="round" stroke-dasharray="527.79" stroke-dashoffset="527.79" transform="rotate(-90 100 100)"/>
            </svg>
            <div class="ringc"><div class="num" id="num">0.0</div><div class="of">из 10</div></div>
          </div>
          <div class="verd" id="verd">—</div>
          <div class="blurb" id="blurb"></div>
          <div class="albc" id="albc" hidden>
            <div class="albh"><span class="albt" id="albName"></span><span id="albProg"></span><button class="albsh" id="albSh" type="button" title="Поделиться оценкой всего альбома"><svg class="ico" viewBox="0 0 24 24"><path d="M4 12v7a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-7"/><path d="M16 6l-4-4-4 4"/><path d="M12 2v13"/></svg></button></div>
            <div class="albn"><b id="albScore">—</b><span id="albCap"></span></div>
            <div class="albl" id="albList"></div>
          </div>
          <div class="stats">
            <div class="st"><b id="stN">0</b><span>Оценено</span></div>
            <div class="st"><b id="stA">—</b><span>Средний</span></div>
            <div class="st"><b id="stB">—</b><span>Лучший</span></div>
          </div>
        </aside>
      </div>
    </section>
  </main>

  <section class="pm pf" id="pf" hidden aria-label="Мой профиль">
    <div class="pm-bg"></div>
    <div class="ptop"><span class="sp"></span><button class="pround" id="pfClose" title="Закрыть (Esc)"><svg class="ico" viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg></button></div>
    <div class="pbody" id="pfBody"></div>
  </section>
  <section class="pm shop" id="shop" hidden aria-label="Магазин">
    <div class="pm-bg"></div>
    <div class="ptop"><span class="sp"></span><button class="pround" id="shClose" title="Закрыть (Esc)"><svg class="ico" viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg></button></div>
    <div class="pbody" id="shBody"></div>
  </section>
  <section class="pm" id="pm" hidden aria-label="Профили артистов">
    <div class="pm-bg"></div>
    <div class="ptop">
      <button class="pround" id="pBack" title="Назад (Esc)" hidden><svg class="ico" viewBox="0 0 24 24"><path d="M19 12H5M12 19l-7-7 7-7"/></svg></button>
      <span class="sp"></span>
      <div class="pq" id="pqw"><svg class="ico" viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/></svg><input id="pq" placeholder="Поиск по никнейму" autocomplete="off"></div>
      <button class="pround" id="pClose" title="Закрыть (Esc)"><svg class="ico" viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg></button>
    </div>
    <div class="pbody" id="pBody"></div>
  </section>
  <section class="pm" id="lb" hidden aria-label="Синглы и альбомы">
    <div class="pm-bg"></div>
    <div class="ptop">
      <button class="pround" id="lbBack" title="Назад (Esc)" hidden><svg class="ico" viewBox="0 0 24 24"><path d="M19 12H5M12 19l-7-7 7-7"/></svg></button>
      <span class="sp"></span>
      <button class="pround" id="lbClose" title="Закрыть (Esc)"><svg class="ico" viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg></button>
    </div>
    <div class="pbody" id="lbBody"></div>
  </section>
  <section class="pm tp" id="tp" hidden aria-label="Топ-3 артистов">
    <div class="pm-bg"></div>
    <div class="tp-fx" aria-hidden="true"><i class="tb b1"></i><i class="tb b2"></i><canvas id="tpCv"></canvas></div>
    <div class="ptop"><span class="sp"></span><button class="pround" id="tpClose" title="Закрыть (Esc)"><svg class="ico" viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg></button></div>
    <div class="tpbody" id="tpBody"></div>
  </section>
  <section class="pm rk" id="rk" hidden aria-label="Общий рейтинг">
    <div class="pm-bg"></div>
    <div class="ptop"><span class="sp"></span><button class="pround" id="rkClose" title="Закрыть (Esc)"><svg class="ico" viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg></button></div>
    <div class="pbody" id="rkBody"></div>
  </section>
  <section class="pm ct" id="ct" hidden aria-label="Каталог релизов">
    <div class="pm-bg"></div>
    <div class="ptop"><span class="sp"></span><button class="pround" id="ctClose" title="Закрыть (Esc)"><svg class="ico" viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg></button></div>
    <div class="pbody" id="ctBody"></div>
  </section>
  <section class="pm rmp" id="rmp" hidden aria-label="Онлайн-комната">
    <div class="pm-bg"></div>
    <div class="ptop">
      <button class="pround" id="rmBack" type="button" title="Назад (Esc)" hidden><svg class="ico" viewBox="0 0 24 24"><path d="M19 12H5M12 19l-7-7 7-7"/></svg></button>
      <span class="sp"></span>
      <button class="pround" id="rmClose" type="button" title="Закрыть (Esc)"><svg class="ico" viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg></button>
    </div>
    <div class="pbody" id="rmBody">
      <div class="rmv" id="rmMenu">
        <div class="phead"><div><div class="eyebrow">Онлайн</div><h2>Онлайн-комната</h2><p>Оценивайте треки вместе с друзьями — оценки скрыты, пока все не закончат</p></div></div>
        <button class="rmresume" id="rmResume" type="button" hidden><span class="rmdot"></span><span><b>Вернуться в комнату <span id="rmResCode"></span></b><em>Комната ещё открыта — друзья могут быть внутри</em></span><svg class="ico" viewBox="0 0 24 24"><path d="M5 12h14M13 6l6 6-6 6"/></svg></button>
        <div class="wl-cards rmcards">
          <button class="wl-card" data-v="create" style="--hh:352" type="button"><span class="wl-ic"><svg class="ico" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg></span><b>Создать комнату</b><p>Ты — администратор: выбираешь трек из медиатеки, зовёшь друзей по ссылке и подводишь итоги.</p></button>
          <button class="wl-card" data-v="join" style="--hh:205" type="button"><span class="wl-ic"><svg class="ico" viewBox="0 0 24 24"><path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/><path d="M10 17l5-5-5-5M15 12H3"/></svg></span><b>Подключиться к комнате</b><p>Есть ссылка или код от друга? Заходи и оценивай трек вместе со всеми.</p></button>
        </div>
      </div>
      <div class="rmv" id="rmCreate" hidden>
        <div class="phead"><div><div class="eyebrow">Онлайн</div><h2 id="rmCH">Создать комнату</h2><p id="rmCP">Выбери трек и отправь друзьям ссылку</p></div></div>
        <div class="rmbody">
          <div id="rmSetupName"><label class="rml">Твой ник в комнате (ты администратор, ник можно поменять и потом — в самой комнате)<input type="text" id="rmName" maxlength="24" value="влад сантана" autocomplete="off" spellcheck="false"></label></div>
          <div id="rmLive" hidden>
            <div class="rminv">
              <div class="rmcode"><span>Код комнаты</span><b id="rmCode">—</b></div>
              <div class="rmlink">
                <label class="rml">Ссылка для друзей<input type="text" id="rmLink" readonly></label>
                <button class="gbtn" id="rmCopy" type="button">Копировать</button>
              </div>
              <label class="rml">Публичный адрес (если друзья не в твоей сети) — вставь адрес туннеля<input type="text" id="rmPub" placeholder="https://что-то.trycloudflare.com" autocomplete="off"></label>
              <details class="rmhelp"><summary>Как позвать друга из другого города?</summary>
                <p>Программа запущена у тебя на компьютере, поэтому друг подключается к нему напрямую. Выбери любой способ:</p>
                <p><b>Одна сеть Wi-Fi</b> — просто отправь ссылку выше.</p>
                <p><b>Через интернет без регистрации</b> — установи <code>cloudflared</code> и выполни в консоли <code id="rmCmd"></code>. Он покажет адрес вида https://….trycloudflare.com — вставь его в поле выше, и ссылка обновится.</p>
                <p>Подойдут и <b>ngrok</b> (<code id="rmCmd2"></code>), и VPN-сети <b>Tailscale / Radmin VPN / ZeroTier</b> (тогда в ссылке будет VPN-адрес).</p>
                <p class="rmwarn">Друзья видят только страницу комнаты — твоя медиатека остаётся закрытой. Windows может спросить разрешение для брандмауэра — разреши для частной сети.</p>
              </details>
            </div>
            <iframe id="rmFrame" title="Комната" allow="autoplay"></iframe>
            <div class="rmres" id="rmSave" hidden><span id="rmSaveT"></span><button class="gbtn" id="rmSaveB" type="button">Записать среднюю оценку в трек</button></div>
          </div>
          <div class="rmtrack">
            <div class="rmh" id="rmTH">Какой трек оцениваем</div>
            <label class="rml">Трек из медиатеки<select id="rmPick"></select></label>
            <div class="rmrow"><label class="rml">Название<input type="text" id="rmTitle" maxlength="80" autocomplete="off"></label><label class="rml">Исполнитель<input type="text" id="rmArtist" maxlength="60" autocomplete="off"></label></div>
            <label class="rmck"><input type="checkbox" id="rmShare"><span>Дать друзьям послушать этот трек <em>(файл отправится с твоего компьютера)</em></span></label>
            <div class="rmact"><button class="gbtn" id="rmGo" type="button">Создать комнату</button><button class="gbtn ghost" id="rmLeave" type="button" hidden>Выйти из комнаты</button></div>
          </div>
          <p class="rmnote" id="rmNote">Все оценивают трек по тем же 5 критериям. Оценки скрыты, пока все не закончат, — потом появятся средняя оценка и таблица «кто как оценил».</p>
        </div>
      </div>
      <div class="rmv" id="rmJoinV" hidden>
        <div class="phead"><div><div class="eyebrow">Онлайн</div><h2>Подключиться к комнате</h2><p>Вставь ссылку, которую прислал друг, или код комнаты</p></div></div>
        <div class="rmbody">
          <div class="rmjoin"><input type="text" id="rmJIn" placeholder="http://192.168.0.5:8765/r/ABCDE  или  код комнаты" autocomplete="off" spellcheck="false"><button class="gbtn" id="rmJGo" type="button">Подключиться</button></div>
          <p class="rmnote">Код подойдёт, если комната открыта на этом компьютере. Если друг в другой сети — попроси у него полную ссылку (адрес туннеля).</p>
          <div id="rmJLive" hidden>
            <div class="rmres"><span id="rmJT"></span><button class="gbtn ghost" id="rmJLeave" type="button">Отключиться</button></div>
            <iframe id="rmJFrame" title="Комната друга" allow="autoplay"></iframe>
          </div>
        </div>
      </div>
    </div>
  </section>
</div>
<div class="gm" id="gm" hidden>
  <div class="gcard wide" role="dialog" aria-label="Игры на пульсы">
    <div class="ghead">
      <div><b>Игры на пульсы</b><span>Рулетка, Пульс, Минки, Plinko и HiLo · пульсы — игровая валюта приложения</span></div>
      <button class="gx" id="gClose" title="Закрыть (Esc)"><svg class="ico" viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg></button>
    </div>
    <div class="gtabs" id="gtabs"><button class="gtab on" data-g="roul">Рулетка</button><button class="gtab" data-g="pulse">Пульс</button><button class="gtab" data-g="mines">Минки</button><button class="gtab" data-g="plinko">Plinko</button><button class="gtab" data-g="hilo">HiLo</button></div>
    <div class="gpane" id="pane-stars" hidden>
      <div class="gstats"><div>Счёт<b id="gScore">0</b></div><div>Рекорд<b id="gBest">0</b></div><div class="lives" id="gLives"></div></div>
      <div class="gwrap" id="gwrap">
        <canvas id="gc" width="840" height="1100"></canvas>
        <div class="gover" id="gOver"><b id="gTitle"></b><p id="gSub"></p><button class="gbtn" id="gStart">Играть</button></div>
      </div>
      <div class="ghint">← → / A D или мышь · Пробел — пауза · Esc — закрыть</div>
    </div>
    <div class="gpane" id="pane-snake" hidden>
      <div class="gstats"><div>Счёт<b id="sScore">0</b></div><div>Рекорд<b id="sBest">0</b></div><div>Длина<b id="sLen">3</b></div></div>
      <div class="gwrap sq" id="swrap">
        <canvas id="sc" width="840" height="840"></canvas>
        <div class="gover" id="sOver"><b id="sTitle"></b><p id="sSub"></p><button class="gbtn" id="sStart">Играть</button></div>
      </div>
      <div class="ghint">Стрелки / WASD · Пробел — пауза · Esc — закрыть</div>
    </div>
    <div class="gpane" id="pane-rhythm" hidden>
      <div class="gstats"><div>Счёт<b id="rScore">0</b></div><div>Рекорд<b id="rBest">0</b></div><div>Комбо<b id="rCombo">0</b></div><div class="lives" id="rLives"></div></div>
      <div class="gwrap" id="rwrap">
        <canvas id="rc" width="840" height="1100"></canvas>
        <div class="gover" id="rOver"><b id="rTitle"></b><p id="rSub"></p><button class="gbtn" id="rStart">Играть</button></div>
      </div>
      <div class="ghint">D F J K или стрелки ← ↓ ↑ → · можно кликать по дорожкам · Пробел — пауза</div>
    </div>
    <div class="gpane" id="pane-roul"></div>
    <div class="gpane" id="pane-pulse" hidden></div>
    <div class="gpane" id="pane-mines" hidden></div>
    <div class="gpane" id="pane-plinko" hidden></div>
    <div class="gpane" id="pane-hilo" hidden></div>
  </div>
</div>
<input type="file" id="imgFile" accept="image/*" hidden>
<div id="mp" hidden role="region" aria-label="Мини-плеер">
  <button class="mpinfo" id="mpGo" type="button" title="Вернуться к треку"><span class="mpcv" id="mpCv"><span class="mpeq"><span class="eq"><i></i><i></i><i></i><i></i></span></span></span><span class="mpt"><b id="mpT"></b><em id="mpA"></em></span></button>
  <div class="mpctl"><button class="apsk" id="mpPrev" type="button" title="Предыдущий трек"><svg viewBox="0 0 24 24"><path d="M6 5v14M19 5L9 12l10 7z"/></svg></button><button class="apb mpplay" id="mpPlay" type="button" title="Играть / пауза"></button><button class="apsk" id="mpNext" type="button" title="Следующий трек"><svg viewBox="0 0 24 24"><path d="M18 5v14M5 5l10 7-10 7z"/></svg></button></div>
  <div class="mpseek"><i id="mpCur">0:00</i><input class="rng" id="mpSeek" type="range" min="0" max="1000" value="0" aria-label="Перемотка"><i id="mpDur">0:00</i></div>
  <button class="mpx" id="mpX" type="button" title="Закрыть плеер и остановить трек"><svg class="ico" viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg></button>
  <div class="mpvol"><button id="mpMute" type="button" title="Без звука"><svg class="ico" viewBox="0 0 24 24"><path d="M11 5L6 9H2v6h4l5 4V5z"/><g class="wv"><path d="M15.5 8.5a5 5 0 0 1 0 7M18.5 5.5a9 9 0 0 1 0 13"/></g><g class="mx"><path d="M16 9l5 6M21 9l-5 6"/></g></svg></button><input class="rng" id="mpVol" type="range" min="0" max="100" value="100" aria-label="Громкость"></div>
</div>
<div id="fpk" hidden></div>
<div id="nwk" hidden></div>
<div id="qa" hidden></div>
<div id="shr" hidden></div>
<div id="rwd" hidden><div class="rwc" role="dialog" aria-label="Награда">
  <div class="rwcoin"><svg class="coin" viewBox="0 0 64 64" aria-hidden="true"><use href="#pcoin"/></svg><span class="rwlv" id="rwL"></span></div>
  <div class="rwn" id="rwN">+0</div><div class="rwt" id="rwT"></div><div class="rws" id="rwS"></div>
  <div class="rwa"><button class="gbtn gold" id="rwOk" type="button">Забрать</button><button class="gbtn ghost" id="rwShop" type="button">В магазин</button></div></div></div>
<div id="toast"></div>

<script>
const PRESET=["Рэп","Хип-хоп","Трэп","Дрилл","Поп","Рок","Инди","R&B","Электроника","Фонк","Метал","Лоу-фай","Джаз","Шансон"];
function fillGenres(){
  const m=new Map();
  PRESET.concat(state.tracks.map(t=>t.genre)).forEach(g=>{g=(g||"").trim();if(g&&!m.has(g.toLowerCase())) m.set(g.toLowerCase(),g)});
  $("#genres").innerHTML=[...m.values()].map(g=>`<option value="${esc(g)}">`).join("");
}
function fillAlbums(){
  const m=new Map();
  state.tracks.forEach(t=>{const a=(t.album||"").trim();if(a&&!m.has(a.toLowerCase())) m.set(a.toLowerCase(),a)});
  $("#albums").innerHTML=[...m.values()].map(a=>`<option value="${esc(a)}">`).join("");
}
const CRIT=[
  ["Уникальность","Насколько трек не похож на остальные"],
  ["Сведение","Баланс, чистота и качество звучания"],
  ["Рифмы / образы","Качество текста, метафор и панчлайнов"],
  ["Реализация / структура","Подача, исполнение, куплеты, припев, переходы"],
  ["Атмосфера / вайб","Настроение и эмоции, которые вызывает трек"]
];
const VERD=[[3,"Ужасно","Не зацепило"],[5,"Слабовато","Есть пара интересных моментов"],[6.5,"Норм","Нормально, но без искры"],[8,"Профик","Достойный трек, можно слушать"],[9,"Фрешмен","В плейлист без раздумий"],[10.01,"GOAT","Классика на повторе"]];
const NOTE='<svg class="ico" viewBox="0 0 24 24"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>';
const TRASH='<svg class="ico" viewBox="0 0 24 24"><path d="M3 6h18M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>';
const $=(s,r=document)=>r.querySelector(s);
const state={artist:"",beat:"",title:"",genre:"",album:"",albumId:null,audio:null,audioId:null,file:null,cover:null,coverBlob:null,coverBlobUrl:"",artistImg:{},vals:Array(5).fill(stStart()),editing:null,tracks:[],filter:"all",sort:"new",q:"",cat:"",catVal:""};
const r1=x=>Math.round(x*10)/10;   // шаг шкалы 0.1
const WEIGHTS=[.175,.175,.175,.175,.3];   // вес критериев: «Атмосфера / вайб» — 30 %, остальные по 17,5 % (в сумме 100 %)
const calc=v=>Math.round(v.reduce((a,b,i)=>a+b*(WEIGHTS[i]!=null?WEIGHTS[i]:.2),0)*10+1e-9)/10;   // взвешенная оценка, шкала 0–10
const total=()=>calc(state.vals);
const fmt=s=>(+s).toFixed(1);
const ordOf=t=>t.ord!=null?t.ord:t.id;          // порядок трека в альбоме (после ручной перестановки), иначе — порядок загрузки
const byOrd=(a,b)=>ordOf(a)-ordOf(b);
const isRated=t=>!t.unrated;                      // трек альбома, который ещё не оценён, не влияет на средние
const low=x=>String(x||"").trim().toLowerCase();
function albumInfo(id){
  const items=state.tracks.filter(t=>t.albumId===id);
  if(!items.length) return null;
  const done=items.filter(isRated);
  return{id,name:items[0].album,items,n:items.length,done:done.length,complete:done.length===items.length,
    avg:done.length?done.reduce((a,t)=>a+t.total,0)/done.length:0};
}
const norm=t=>Array.isArray(t.scores)&&t.scores.length===5?{...t,total:calc(t.scores)}:t;   // старые оценки (из 100) пересчитываются
const verdict=s=>VERD.find(v=>s<v[0]);
const RG=[[0,0],[2,16],[3.8,36],[5.2,54],[6.6,88],[8,140],[9.2,172],[10,186]];   // 0 — красный → оранжевый → жёлтый → зелёный → 10 — голубо-бирюзовый   // красный → оранжевый → жёлтый → лайм → зелёный → бирюзовый → голубой
const rgHue=v=>{v=Math.max(0,Math.min(10,+v||0));let i=1;while(i<RG.length-1&&v>RG[i][0]) i++;
  const a=RG[i-1],b=RG[i],k=(v-a[0])/(b[0]-a[0]);return a[1]+(b[1]-a[1])*k};
const rgCol=v=>`hsl(${rgHue(v).toFixed(0)} 88% ${(57+3*Math.sin(Math.max(0,Math.min(10,+v||0))/10*Math.PI)).toFixed(0)}%)`;   // единая шкала цвета оценок во всём приложении
const hueFor=s=>340+s*2.8;   // оттенки красного: от малинового к алому
const esc=s=>String(s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

function hash(s){let h=2166136261;for(let i=0;i<s.length;i++){h^=s.charCodeAt(i);h=Math.imul(h,16777619)}return h>>>0}
function coverBg(a,t){
  const k=(a+"|"+t).trim().toLowerCase();
  if(k==="|") return "linear-gradient(135deg,#3d3d3d,#1f1f1f)";
  const h=hash(k),h1=h%360,h2=(h1+30+(h>>9)%70)%360;
  return `linear-gradient(135deg,hsl(${h1} 68% 48%),hsl(${h2} 72% 26%))`;
}

/* ---------- фон: летящие звёзды ---------- */
const FX={warp:1,bo:stShift(),th:stShift()};

/* ---------- фон: мерцающие звёзды ---------- */
(()=>{
  const cv=$("#twinkle");if(!cv) return;
  const ctx=cv.getContext("2d"),reduce=matchMedia("(prefers-reduced-motion: reduce)").matches;
  let W=0,H=0,dpr=1,S=[];
  function build(){
    dpr=Math.min(devicePixelRatio||1,1.5);
    W=innerWidth;H=innerHeight;cv.width=W*dpr|0;cv.height=H*dpr|0;
    const n=Math.min(260,Math.round(W*H/8500));
    S=Array.from({length:n},()=>{
      const big=Math.random()<.14;
      return{x:Math.random()*W,y:Math.random()*H,r:big?1.1+Math.random()*1.1:.4+Math.random()*.8,
        ph:Math.random()*6.283,sp:.5+Math.random()*2.2,a:.35+Math.random()*.65,big,tint:Math.random()<.3};
    });
  }
  function draw(t){
    ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,W,H);
    const hue=(350+(FX.bo||0))|0,tint=`hsl(${hue} 100% 82%)`;
    for(const s of S){
      const k=reduce?.6:Math.pow(Math.max(0,Math.sin(t*s.sp+s.ph)),3);   // редкие яркие вспышки, остальное время — тихий огонёк
      const al=s.a*(.18+.82*k);
      ctx.fillStyle=ctx.strokeStyle=s.tint?tint:"#fff";
      ctx.globalAlpha=al;
      ctx.beginPath();ctx.arc(s.x,s.y,s.r,0,6.283);ctx.fill();
      if(s.big&&k>.2){
        ctx.globalAlpha=al*.14;ctx.beginPath();ctx.arc(s.x,s.y,s.r*4.5,0,6.283);ctx.fill();
        const L=s.r*(3+9*k);
        ctx.globalAlpha=al*.55;ctx.lineWidth=.7;
        ctx.beginPath();ctx.moveTo(s.x-L,s.y);ctx.lineTo(s.x+L,s.y);ctx.moveTo(s.x,s.y-L);ctx.lineTo(s.x,s.y+L);ctx.stroke();
      }
    }
    ctx.globalAlpha=1;
  }
  let last=0;
  function loop(ms){requestAnimationFrame(loop);if(ms-last<fpsGap(50,30)) return;last=ms;if(!layerOn("twinkle")||window.BGPAUSE) return;draw(ms/1000)}   // каждый кадр монитора
  build();
  addEventListener("resize",()=>{build();if(reduce) draw(0)});
  if(reduce) draw(0);else requestAnimationFrame(loop);
})();

/* ---------- эффекты за курсором: свет строго под указателем + искры по траектории ---------- */
(()=>{
  const glow=$("#fxGlow"),cv=$("#fxTrail");if(!glow||!cv) return;
  const ctx=cv.getContext("2d"),reduce=matchMedia("(prefers-reduced-motion: reduce)").matches;
  const HOT="button,a,input,label,.item,.pcrd,.wl-card,.sl,.chip,.cat,.cpill,.navbtn,.albr,.gh,.cover,.round,.pround,.thm,.sw,.sr,.strow,.stseg button,.plate,.keybtn,.keyx";
  let W=0,H=0,dpr=1,P=[],raf=0,last=0,px=-1,py=-1,have=false,carry=0,lastT=null,mvT=0;
  function size(){dpr=Math.min(devicePixelRatio||1,1.5);W=innerWidth;H=innerHeight;cv.width=W*dpr|0;cv.height=H*dpr|0}
  size();addEventListener("resize",size);
  const hue=()=>350+(FX.bo||0);
  function spark(x,y,burst){
    const a=Math.random()*6.283,v=burst?40+Math.random()*130:6+Math.random()*26;
    P.push({k:Math.random()<.28?1:0,x,y,vx:Math.cos(a)*v,vy:Math.sin(a)*v-(burst?0:8),
      age:0,o:burst?.5:.26,life:(burst?.55:.6)+Math.random()*.5,r:burst?1.2+Math.random()*1.6:.9+Math.random()*1.7,h:hue()+(Math.random()*34-17)});
  }
  function kick(){if(!raf){last=performance.now();raf=requestAnimationFrame(tick)}}
  function tick(ms){
    const dt=Math.min(.05,(ms-last)/1000||.016);last=ms;
    ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,W,H);ctx.globalCompositeOperation="lighter";
    for(let i=P.length-1;i>=0;i--){
      const p=P[i];p.age+=dt;
      const t=p.age/p.life;
      if(t>=1){P.splice(i,1);continue}
      const e=1-t,al=e*e*(p.o||.5);                              // плавное затухание (след полупрозрачный)
      if(p.k===2){                                         // кольцо от клика
        const r=Math.max(0,6+54*(1-Math.pow(Math.max(0,1-t),3)));
        ctx.globalAlpha=al*.9;ctx.lineWidth=2.2*e+.3;ctx.strokeStyle=`hsl(${p.h} 100% 70%)`;
        ctx.beginPath();ctx.arc(p.x,p.y,r,0,6.283);ctx.stroke();continue;
      }
      const damp=Math.pow(.06,dt);p.vx*=damp;p.vy*=damp;
      p.x+=p.vx*dt;p.y+=p.vy*dt;
      const r=Math.max(0,p.r*(1-t*.55)),col=`hsl(${p.h} 100% ${p.k?82:68}%)`;
      ctx.fillStyle=col;
      ctx.globalAlpha=al*.18;ctx.beginPath();ctx.arc(p.x,p.y,r*4,0,6.283);ctx.fill();   // гало
      ctx.globalAlpha=al;ctx.beginPath();ctx.arc(p.x,p.y,r,0,6.283);ctx.fill();         // ядро
      if(p.k){                                             // четырёхлучевая вспышка, как у звёзд фона
        const L=r*(4+5*e);ctx.strokeStyle=col;ctx.lineWidth=.8;ctx.globalAlpha=al*.8;
        ctx.beginPath();ctx.moveTo(p.x-L,p.y);ctx.lineTo(p.x+L,p.y);ctx.moveTo(p.x,p.y-L);ctx.lineTo(p.x,p.y+L);ctx.stroke();
      }
    }
    ctx.globalAlpha=1;ctx.globalCompositeOperation="source-over";
    if(P.length) raf=requestAnimationFrame(tick);else{raf=0;ctx.clearRect(0,0,W,H)}
  }
  function trail(x,y){                                   // искры равномерно вдоль пути — без разрывов при быстром движении
    if(!have){have=true;px=x;py=y;return}
    const dx=x-px,dy=y-py,d=Math.hypot(dx,dy);
    if(d<.5) return;
    const step=9;let n=0;carry+=d;
    while(carry>=step&&n<14){
      carry-=step;n++;
      const f=1-carry/d,sx=px+dx*Math.min(1,Math.max(0,f)),sy=py+dy*Math.min(1,Math.max(0,f));
      spark(sx,sy,false);
    }
    px=x;py=y;kick();
  }
  document.addEventListener("pointermove",e=>{
    if(e.pointerType==="touch") return;
    // свет ставится синхронно в событии — никакой интерполяции и задержки
    glow.style.transform=`translate3d(${e.clientX}px,${e.clientY}px,0)`;
    glow.classList.add("on","mv");clearTimeout(mvT);mvT=setTimeout(()=>glow.classList.remove("mv"),120);   // свечение заметнее, пока мышь движется
    if(e.target!==lastT){lastT=e.target;glow.classList.toggle("hot",!!(e.target.closest&&e.target.closest(HOT)))}
    if(reduce||ST.eco||!ST.trail) return;                  // след из искр выключен (можно включить в настройках)
    const ev=e.getCoalescedEvents?e.getCoalescedEvents():null;
    if(ev&&ev.length>1) ev.forEach(c=>trail(c.clientX,c.clientY));else trail(e.clientX,e.clientY);
  },{passive:true,capture:true});
  document.addEventListener("pointerdown",e=>{
    if(e.pointerType==="touch"||reduce||ST.eco||!ST.clickFx) return;
    P.push({k:2,x:e.clientX,y:e.clientY,vx:0,vy:0,age:0,life:.55,r:0,h:hue()});
    for(let i=0;i<12;i++) spark(e.clientX,e.clientY,true);
    kick();
  },{passive:true,capture:true});
  document.documentElement.addEventListener("pointerleave",()=>{glow.classList.remove("on","mv");have=false;carry=0});
  addEventListener("blur",()=>{glow.classList.remove("on");have=false});
})();
(()=>{
  const cv=$("#stars"),ctx=cv.getContext("2d");
  const reduce=matchMedia("(prefers-reduced-motion: reduce)").matches;
  const rnd=(a,b)=>a+Math.random()*(b-a);
  let W=0,H=0,cx=0,cy=0;
  function resize(){
    const d=Math.min(devicePixelRatio||1,1.25);
    W=innerWidth;H=innerHeight;cx=W/2;cy=H/2;
    cv.width=Math.round(W*d);cv.height=Math.round(H*d);
    ctx.setTransform(d,0,0,d,0,0);
  }
  resize();addEventListener("resize",resize);

  const mkFly=far=>{
    const red=Math.random()<.45;
    return{x:rnd(-1,1),y:rnd(-1,1),z:far?1:rnd(.04,1),v:rnd(.8,1.25),
      h:red?rnd(345,372):null};
  };
  const fly=Array.from({length:230},()=>mkFly(false));
  const tw=Array.from({length:150},()=>({x:Math.random(),y:Math.random(),r:rnd(.35,1.1),p:rnd(0,6.28),s:rnd(.5,1.7),d:rnd(2,9),
    t:Math.random()<.3}));
  const sh=[];
  let last=performance.now(),cur=1,t=0,nextShoot=2.5;

  /* --- планеты и чёрные дыры: рождаются, дрейфуют, меняют цвет и гаснут --- */
  const TAU=Math.PI*2,HUES=[350,18,42,200,225,265,300,175];
  const bodies=[];
  function newBody(init){
    const sc=Math.max(.6,Math.min(W,H)/900);
    const hole=!bodies.some(b=>b.hole)||Math.random()<.28;
    const kind=hole?"hole":["gas","ringed","earth","ice"][(Math.random()*4)|0];
    const R=(hole?rnd(40,62):rnd(46,112))*sc;
    let x=W*.5,y=H*.5;
    for(let i=0;i<14;i++){
      x=rnd(.06,.94)*W;y=rnd(.1,.9)*H;
      if(bodies.every(o=>Math.hypot(o.x-x,o.y-y)>(o.R+R)*2.2)) break;
    }
    const life=rnd(55,95);
    return{kind,hole,x,y,R,vx:rnd(-5,5)*sc,vy:rnd(-3.5,3.5)*sc,life,age:init?rnd(0,life*.55):0,
      h:HUES[(Math.random()*HUES.length)|0],ph:rnd(0,TAU),tilt:rnd(.14,.5)*(Math.random()<.5?1:-1),
      spin:rnd(.012,.03),u0:Math.random(),
      bands:Array.from({length:9},()=>({o:rnd(-40,40),a:rnd(.08,.3)})),
      land:Array.from({length:7},()=>({u:Math.random(),v:rnd(-.7,.7),w:rnd(.18,.4),h:rnd(.1,.3)})),
      clouds:Array.from({length:6},()=>({u:Math.random(),v:rnd(-.8,.8),w:rnd(.2,.5)}))};
  }

  function blob(x,y,rx,ry){ctx.beginPath();ctx.ellipse(x,y,Math.max(.5,rx),Math.max(.5,ry),0,0,TAU);ctx.fill()}
  function drawRing(b,hue,front){
    ctx.save();ctx.translate(b.x,b.y);ctx.rotate(b.tilt);
    const a0=front?0:Math.PI,a1=front?Math.PI:TAU;
    for(let k=0;k<4;k++){
      const rx=b.R*(1.55+k*.2);
      ctx.beginPath();ctx.ellipse(0,0,rx,rx*.24,0,a0,a1);
      ctx.lineWidth=b.R*(k%2?.07:.13);
      ctx.strokeStyle=`hsla(${hue+k*8},45%,${72-k*6}%,${.5-k*.08})`;
      ctx.stroke();
    }
    ctx.restore();
  }
  function drawPlanet(b,e){
    const R=b.R*(.88+.12*e),x=b.x,y=b.y;
    const hue=b.h+FX.bo*.5+Math.sin(t*.035+b.ph)*16;
    const ring=b.kind==="ringed"||(b.kind==="ice"&&b.ph>3.5);
    ctx.globalAlpha=e*.92;
    const bb={x,y,R,tilt:b.tilt};
    if(ring) drawRing(bb,hue,false);
    let g=ctx.createRadialGradient(x,y,R*.85,x,y,R*1.5);                       // атмосфера
    g.addColorStop(0,`hsla(${hue},95%,66%,.38)`);g.addColorStop(1,`hsla(${hue},95%,60%,0)`);
    ctx.fillStyle=g;ctx.beginPath();ctx.arc(x,y,R*1.5,0,TAU);ctx.fill();

    ctx.save();ctx.beginPath();ctx.arc(x,y,R,0,TAU);ctx.clip();
    const sat=b.kind==="ice"?45:72,lit=b.kind==="ice"?78:70;
    g=ctx.createRadialGradient(x-R*.4,y-R*.45,R*.08,x,y,R*1.05);
    g.addColorStop(0,`hsl(${hue} ${sat}% ${lit}%)`);
    g.addColorStop(.55,`hsl(${hue+8} ${sat}% ${lit-26}%)`);
    g.addColorStop(1,`hsl(${hue+22} ${sat}% ${lit-52}%)`);
    ctx.fillStyle=g;ctx.fillRect(x-R,y-R,R*2,R*2);
    if(b.kind==="earth"){
      for(const l of b.land){                                                  // континенты плывут по диску
        const p=(((t*b.spin+l.u)%1)+1)%1*2-1,fx=Math.cos(p*Math.PI/2);
        ctx.fillStyle=`hsla(${hue+95} 45% 42% / ${.75*fx})`;
        blob(x+p*R*1.05,y+l.v*R,R*l.w*fx,R*l.h);
      }
      for(const c of b.clouds){
        const p=(((t*b.spin*1.8+c.u)%1)+1)%1*2-1,fx=Math.cos(p*Math.PI/2);
        ctx.fillStyle=`rgba(255,255,255,${.34*fx})`;
        blob(x+p*R*1.05,y+c.v*R,R*c.w*fx,R*.07);
      }
    }else{
      const n=b.bands.length;
      for(let i=0;i<n;i++){
        const bd=b.bands[i],yy=y-R+(i+.5)/n*R*2+Math.sin(t*.15+i+b.ph)*R*.03,hh=R*2/n*(.5+(i%3)*.25);
        ctx.fillStyle=`hsla(${hue+bd.o} 70% ${i%2?30:75}% / ${bd.a})`;
        ctx.fillRect(x-R,yy-hh/2,R*2,hh);
      }
      const p=(((t*b.spin*1.5+b.u0)%1)+1)%1*2-1,fx=Math.cos(p*Math.PI/2);       // штормовое пятно
      ctx.fillStyle=`hsla(${hue+30} 80% 80% / ${.35*fx})`;
      blob(x+p*R*1.05,y+R*.25,R*.2*fx,R*.1);
    }
    g=ctx.createRadialGradient(x-R*.55,y-R*.55,R*.2,x-R*.05,y-R*.05,R*1.3);     // тень
    g.addColorStop(0,"rgba(0,0,0,0)");g.addColorStop(.55,"rgba(0,0,0,.28)");g.addColorStop(1,"rgba(0,0,0,.9)");
    ctx.fillStyle=g;ctx.fillRect(x-R,y-R,R*2,R*2);
    ctx.restore();

    ctx.lineWidth=1.5;ctx.strokeStyle=`hsla(${hue},100%,80%,.35)`;
    ctx.beginPath();ctx.arc(x,y,R-.75,0,TAU);ctx.stroke();
    if(ring) drawRing(bb,hue,true);
  }
  function drawHole(b,e){
    const R=b.R*(.8+.2*e)*(1+.05*Math.sin(t*.5+b.ph)),x=b.x,y=b.y;
    const hue=b.h+FX.bo*.4+Math.sin(t*.05+b.ph)*20;
    ctx.globalAlpha=e;ctx.globalCompositeOperation="lighter";
    let g=ctx.createRadialGradient(x,y,R*.9,x,y,R*3.4);                         // гравитационное свечение
    g.addColorStop(0,`hsla(${hue+10},100%,60%,.45)`);g.addColorStop(.35,`hsla(${hue},100%,50%,.14)`);g.addColorStop(1,`hsla(${hue},100%,40%,0)`);
    ctx.fillStyle=g;ctx.beginPath();ctx.arc(x,y,R*3.4,0,TAU);ctx.fill();

    ctx.save();ctx.translate(x,y);ctx.rotate(b.tilt*.5);
    const big=R*8;
    const disk=upper=>{                                                        // аккреционный диск
      ctx.save();ctx.beginPath();ctx.rect(-big,upper?-big:0,big*2,big);ctx.clip();ctx.lineCap="round";
      for(let k=0;k<9;k++){
        const rx=R*(1.35+k*.24),ry=rx*.2,sp=1.2/Math.sqrt(k+1);
        for(let s=0;s<3;s++){
          const a0=t*sp+s*2.1+k;
          ctx.beginPath();ctx.ellipse(0,0,rx,ry,0,a0,a0+1.9-k*.12);
          ctx.lineWidth=R*(.2-k*.014);
          ctx.strokeStyle=`hsla(${hue+k*5+s*4},100%,${88-k*6}%,${.5-k*.04})`;
          ctx.stroke();
        }
      }
      ctx.restore();
    };
    disk(true);
    ctx.beginPath();ctx.ellipse(0,0,R*1.38,R*1.34,0,Math.PI*1.02,Math.PI*1.98);  // линзированный свет над дырой
    ctx.lineWidth=R*.1;ctx.strokeStyle=`hsla(${hue+14},100%,82%,.55)`;
    ctx.save();ctx.globalAlpha=e*.2;ctx.lineWidth=R*.36;ctx.stroke();ctx.globalAlpha=e*.3;ctx.lineWidth=R*.2;ctx.stroke();ctx.restore();ctx.stroke();
    ctx.globalCompositeOperation="source-over";                                // горизонт событий
    ctx.fillStyle="#000";ctx.beginPath();ctx.arc(0,0,R,0,TAU);ctx.fill();
    ctx.globalCompositeOperation="lighter";                                    // фотонное кольцо
    ctx.beginPath();ctx.arc(0,0,R*1.03,0,TAU);ctx.lineWidth=R*.05;ctx.strokeStyle=`hsla(${hue+20},100%,85%,.9)`;
    ctx.save();ctx.globalAlpha=e*.2;ctx.lineWidth=R*.3;ctx.stroke();ctx.globalAlpha=e*.3;ctx.lineWidth=R*.15;ctx.stroke();ctx.restore();ctx.stroke();
    disk(false);
    ctx.restore();
    ctx.globalCompositeOperation="source-over";ctx.globalAlpha=1;
  }
  function drawBodies(dt){
    for(let i=bodies.length-1;i>=0;i--){
      const b=bodies[i];
      b.age+=dt;
      if(b.age>=b.life){bodies.splice(i,1);bodies.push(newBody(false));continue}
      b.x+=b.vx*dt;b.y+=b.vy*dt;
      if((b.x<0&&b.vx<0)||(b.x>W&&b.vx>0)) b.vx=-b.vx;
      if((b.y<0&&b.vy<0)||(b.y>H&&b.vy>0)) b.vy=-b.vy;
      const a=Math.max(0,Math.min(1,b.age/7,(b.life-b.age)/7)),e=a*a*(3-2*a);
      if(b.hole) drawHole(b,e);else drawPlanet(b,e);
    }
    ctx.globalAlpha=1;ctx.globalCompositeOperation="source-over";
  }

  function draw(dt){
    t+=dt;
    cur+=(FX.warp-cur)*Math.min(1,dt*3);          // плавный разгон при перетаскивании ползунка
    ctx.clearRect(0,0,W,H);

    // мерцающие звёзды с лёгким дрейфом
    const twc=`hsl(${(350+FX.bo)|0} 100% 85%)`;
    for(const s of tw){
      const a=.2+.6*(.5+.5*Math.sin(t*s.s+s.p));
      const px=((s.x*W-t*s.d)%W+W)%W;
      ctx.globalAlpha=a;ctx.fillStyle=s.t?twc:"#fff";
      ctx.beginPath();ctx.arc(px,s.y*H,s.r,0,6.2832);ctx.fill();
    }

    // звёзды, летящие на зрителя
    ctx.lineCap="round";
    const trail=.012+.05*(cur-1>0?cur-1:0)/2.5;
    for(const s of fly){
      s.z-=dt*.07*s.v*cur;
      if(s.z<=.03){Object.assign(s,mkFly(true));continue}
      const k=1/s.z,sx=cx+s.x*k*cx,sy=cy+s.y*k*cy;
      if(sx<-40||sx>W+40||sy<-40||sy>H+40){Object.assign(s,mkFly(true));continue}
      const k2=1/(s.z+trail*s.v),tx=cx+s.x*k2*cx,ty=cy+s.y*k2*cy;
      ctx.globalAlpha=Math.min(1,(1-s.z)*3.5)*Math.min(1,s.z*14);
      ctx.strokeStyle=s.h===null?"#fff":`hsl(${(s.h+FX.bo)|0} 95% 76%)`;ctx.lineWidth=.5+(1-s.z)*1.9;
      ctx.beginPath();ctx.moveTo(tx,ty);ctx.lineTo(sx,sy);ctx.stroke();
    }

    // падающие звёзды
    if(!reduce&&t>nextShoot){
      nextShoot=t+rnd(3,7);
      const a=rnd(2.45,2.8);
      sh.push({x:rnd(.25,1)*W,y:rnd(-.05,.45)*H,vx:Math.cos(a)*rnd(650,950),vy:Math.sin(a)*rnd(650,950),age:0,life:rnd(.8,1.2),len:rnd(110,190)});
    }
    for(let i=sh.length-1;i>=0;i--){
      const s=sh[i];s.age+=dt;
      if(s.age>=s.life){sh.splice(i,1);continue}
      s.x+=s.vx*dt;s.y+=s.vy*dt;
      const sp=Math.hypot(s.vx,s.vy),ux=s.vx/sp,uy=s.vy/sp;
      const tx=s.x-ux*s.len,ty=s.y-uy*s.len;
      const g=ctx.createLinearGradient(s.x,s.y,tx,ty);
      g.addColorStop(0,"rgba(255,255,255,1)");g.addColorStop(.35,`hsla(${(352+FX.bo)|0},100%,68%,.55)`);g.addColorStop(1,`hsla(${(352+FX.bo)|0},100%,55%,0)`);
      ctx.globalAlpha=Math.sin(Math.PI*s.age/s.life);
      ctx.strokeStyle=g;ctx.lineWidth=1.8;
      ctx.beginPath();ctx.moveTo(s.x,s.y);ctx.lineTo(tx,ty);ctx.stroke();
    }
    ctx.globalAlpha=1;
  }
  if(reduce){draw(0);addEventListener("resize",()=>draw(0));return}
  const frame=now=>{
    requestAnimationFrame(frame);
    if(now-last<fpsGap(48,14)) return;           // не чаще ~60 кадров/с
    const dt=Math.min(.05,(now-last)/1000);last=now;
    if(layerOn("warp")&&!window.BGPAUSE) draw(dt);
  };
  requestAnimationFrame(frame);
})();

/* ---------- северное сияние: живые переливающиеся занавесы ---------- */
(()=>{
  const cv=$("#aurora"),vctx=cv.getContext("2d"),off=document.createElement("canvas"),ctx=off.getContext("2d");
  const reduce=matchMedia("(prefers-reduced-motion: reduce)").matches;
  const PAD=40,STEP=10,N=4;
  let W=0,H=0;
  function resize(){
    W=Math.ceil((innerWidth+PAD*2)/2);H=Math.ceil((innerHeight+PAD*2)/2);   // логические координаты — половина экрана
    cv.width=off.width=Math.ceil(W/3);cv.height=off.height=Math.ceil(H/3);ctx.setTransform(1/3,0,0,1/3,0,0);   // сияние размытое — хватает трети разрешения (в 9 раз меньше пикселей)
  }
  resize();addEventListener("resize",resize);
  const spr=Array.from({length:N},()=>{const c=document.createElement("canvas");c.width=1;c.height=128;return c});
  function paintSprite(c,hue,k){
    const g=c.getContext("2d"),gr=g.createLinearGradient(0,0,0,128);
    gr.addColorStop(0,`hsla(${hue+40},90%,55%,0)`);
    gr.addColorStop(.35,`hsla(${hue+22},95%,55%,${.35*k})`);
    gr.addColorStop(.78,`hsla(${hue},100%,62%,${.95*k})`);
    gr.addColorStop(.9,`hsla(${hue-14},100%,78%,${.6*k})`);
    gr.addColorStop(1,`hsla(${hue-14},100%,70%,0)`);
    g.clearRect(0,0,1,128);g.fillStyle=gr;g.fillRect(0,0,1,128);
  }
  let t=0,last=performance.now();
  function draw(dt){
    t+=dt;
    ctx.clearRect(0,0,W,H);
    ctx.globalCompositeOperation="lighter";
    const base=352+(FX.bo||0);
    for(let i=0;i<N;i++){
      const hue=base+i*16+Math.sin(t*.07+i*1.7)*22;                       // цвет плывёт вдоль занавеса и во времени
      paintSprite(spr[i],hue,.9-i*.1);
      const sp=.22+i*.07,f=.0042+i*.0011;
      for(let x=0;x<W;x+=STEP){
        const ph=x*f*2+t*sp+i*2.1;
        const wave=Math.sin(ph)+.55*Math.sin(ph*2.3-t*.5)+.3*Math.sin(x*.013+t*.27+i);
        const top=H*(.06+.1*i)+wave*H*.07;
        const hh=H*(.5+.18*Math.sin(x*.0065+t*.31+i*1.3)+.07*Math.sin(t*.9+i));
        const ray=.55+.45*Math.sin(x*.19+t*1.1+i*3)*Math.sin(x*.043-t*.4);   // вертикальные лучи, дрожащие и бегущие
        const fade=.5+.5*Math.sin(x*.0058-t*.23+i*2.4);                       // волны яркости вдоль неба
        ctx.globalAlpha=Math.max(0,Math.min(1,(.18+.55*fade)*ray))*.9;
        ctx.drawImage(spr[i],0,0,1,128,x,top,STEP+1,hh);
      }
    }
    ctx.globalAlpha=1;ctx.globalCompositeOperation="source-over";
  }
  const sm=document.createElement("canvas"),sctx=sm.getContext("2d");
  function blit(){const w=Math.max(1,cv.width>>2),h=Math.max(1,cv.height>>2);if(sm.width!==w||sm.height!==h){sm.width=w;sm.height=h}sctx.clearRect(0,0,w,h);sctx.drawImage(off,0,0,w,h);vctx.clearRect(0,0,cv.width,cv.height);vctx.globalAlpha=.55;vctx.drawImage(off,0,0);vctx.globalAlpha=1;vctx.globalCompositeOperation="lighter";vctx.globalAlpha=.55;vctx.drawImage(sm,0,0,cv.width,cv.height);vctx.globalAlpha=1;vctx.globalCompositeOperation="source-over"}
  if(reduce){draw(0);blit();addEventListener("resize",()=>{draw(0);blit()});return}
  let acc=0;
  const frame=now=>{
    requestAnimationFrame(frame);
    const dt=Math.min(.1,(now-last)/1000);last=now;acc+=dt;
    if(acc<fpsGap(50,30)/1000) return;           // сияние — ~30 кадров/с
    if(layerOn("aurora")&&!window.BGPAUSE){draw(Math.min(.1,acc));blit()}acc=0;
  };
  requestAnimationFrame(frame);
})();


/* ---------- живой фон дизайнов «Океан» и «Сакура» ---------- */
(()=>{
  const cv=$("#dfx");if(!cv) return;
  const ctx=cv.getContext("2d"),reduce=matchMedia("(prefers-reduced-motion: reduce)").matches;
  const rnd=(a,b)=>a+Math.random()*(b-a);
  let W=0,H=0,kind="",A=[],B=[],t=0,last=performance.now(),idle=true;
  const live=()=>(ST.design==="ocean"||ST.design==="sakura"||ST.design==="fireflies"||ST.design==="lux"||ST.design==="nebula")&&!ST.eco&&!reduce&&!window.LOWFX2;
  function init(){
    kind=ST.design;A=[];B=[];
    if(kind==="ocean"){
      for(let i=0;i<70;i++) A.push({x:Math.random()*W,y:Math.random()*H,r:rnd(1.2,6.5),v:rnd(16,52),w:rnd(.6,2),ph:rnd(0,6.28)});      // пузырьки
      for(let i=0;i<110;i++) B.push({x:Math.random()*W,y:Math.random()*H,r:rnd(.4,1.4),vx:rnd(-6,6),vy:rnd(-4,4),a:rnd(.15,.6),ph:rnd(0,6.28)});   // взвесь
    }else if(kind==="fireflies"){
      for(let i=0;i<64;i++) A.push({x:Math.random()*W,y:rnd(H*.15,H),ang:rnd(0,6.28),v:rnd(8,26),ph:rnd(0,6.28),s:rnd(.6,1.4),r:rnd(.7,1.5),turn:rnd(.4,1.2)});
      for(let i=0;i<5;i++) B.push({x:Math.random()*W,y:rnd(H*.55,H*.95),rx:rnd(W*.25,W*.5),ry:rnd(40,90),vx:rnd(-10,10),a:rnd(.05,.1)});
      forest();
    }else if(kind==="lux"){
      for(let i=0;i<110;i++) A.push({x:Math.random()*W,y:Math.random()*H,r:rnd(.5,1.9),vy:rnd(3,12),vx:rnd(-4,4),ph:rnd(0,6.28),s:rnd(.5,1.6),gl:Math.random()<.09});
      B.push({x:-1,cd:rnd(1.5,3)});
    }else if(kind==="nebula"){
      nebMake();
      const cols=["#ffffff","#e0e7ff","#c7d2fe","#a5f3fc","#fbcfe8"];
      for(let i=0;i<260;i++) A.push({x:Math.random()*W,y:Math.random()*H,r:rnd(.4,1.5),z:rnd(.15,1),ph:rnd(0,6.28),s:rnd(.4,2.2),c:cols[(Math.random()*cols.length)|0]});
      B.push({t:rnd(1,3),x:null});
    }else if(kind==="sakura"){
      for(let i=0;i<54;i++) A.push(petal(true));
      for(let i=0;i<14;i++) B.push({x:Math.random()*W,y:Math.random()*H,r:rnd(30,110),vx:rnd(-6,6),vy:rnd(-4,4),h:rnd(320,350),a:rnd(.04,.11)});   // боке
    }
  }
  function petal(any){
    return{x:any?Math.random()*W:rnd(-W*.2,W),y:any?Math.random()*H:rnd(-60,-20),s:rnd(5,12),vy:rnd(22,55),vx:rnd(12,38),rot:rnd(0,6.28),vr:rnd(-1.5,1.5),
      fl:rnd(0,6.28),fv:rnd(1.5,3.5),sw:rnd(.6,1.6),ph:rnd(0,6.28),l:rnd(78,90),h:rnd(330,350)};
  }
  function size(){W=innerWidth;H=innerHeight;cv.width=W;cv.height=H;init()}
  function ocean(dt){
    ctx.globalCompositeOperation="lighter";
    for(let i=0;i<6;i++){                                       // лучи света сквозь воду
      const cx=W*(.08+i*.17)+Math.sin(t*.14+i*1.7)*W*.05,w=W*(.035+.02*Math.sin(t*.2+i));
      const g=ctx.createLinearGradient(0,0,0,H*.95);
      g.addColorStop(0,`hsla(190,95%,72%,${.07+.035*Math.sin(t*.35+i*2)})`);g.addColorStop(1,"hsla(195,90%,60%,0)");
      ctx.fillStyle=g;ctx.beginPath();ctx.moveTo(cx-w*.4,0);ctx.lineTo(cx+w*.4,0);ctx.lineTo(cx+w*2.2+H*.22,H*.95);ctx.lineTo(cx-w*.8+H*.22,H*.95);ctx.closePath();ctx.fill();
    }
    for(const p of B){                                           // светящаяся взвесь
      p.x+=p.vx*dt;p.y+=p.vy*dt;if(p.x<0) p.x+=W;if(p.x>W) p.x-=W;if(p.y<0) p.y+=H;if(p.y>H) p.y-=H;
      ctx.globalAlpha=p.a*(.6+.4*Math.sin(t*1.3+p.ph));ctx.fillStyle="#9cf0ff";ctx.beginPath();ctx.arc(p.x,p.y,p.r,0,6.283);ctx.fill();
    }
    ctx.globalCompositeOperation="source-over";
    for(const b of A){                                           // пузырьки
      b.y-=b.v*dt;b.x+=Math.sin(t*b.w+b.ph)*12*dt;
      if(b.y<-12){b.y=H+rnd(5,60);b.x=Math.random()*W}
      ctx.globalAlpha=.55;ctx.lineWidth=1;ctx.strokeStyle="rgba(190,245,255,.6)";
      ctx.beginPath();ctx.arc(b.x,b.y,b.r,0,6.283);ctx.stroke();
      ctx.globalAlpha=.7;ctx.fillStyle="rgba(255,255,255,.8)";ctx.beginPath();ctx.arc(b.x-b.r*.35,b.y-b.r*.35,Math.max(.6,b.r*.22),0,6.283);ctx.fill();
    }
    ctx.globalAlpha=1;
  }
  function sakura(dt){
    for(const b of B){                                           // мягкое боке
      b.x+=b.vx*dt;b.y+=b.vy*dt;if(b.x<-b.r) b.x=W+b.r;if(b.x>W+b.r) b.x=-b.r;if(b.y<-b.r) b.y=H+b.r;if(b.y>H+b.r) b.y=-b.r;
      const g=ctx.createRadialGradient(b.x,b.y,0,b.x,b.y,b.r);
      g.addColorStop(0,`hsla(${b.h},90%,75%,${b.a})`);g.addColorStop(1,`hsla(${b.h},90%,70%,0)`);
      ctx.fillStyle=g;ctx.beginPath();ctx.arc(b.x,b.y,b.r,0,6.283);ctx.fill();
    }
    for(let i=0;i<A.length;i++){                                 // лепестки
      const p=A[i];
      p.y+=p.vy*dt;p.x+=(p.vx+Math.sin(t*p.sw+p.ph)*28)*dt;p.rot+=p.vr*dt;p.fl+=p.fv*dt;
      if(p.y>H+30||p.x>W+40){A[i]=petal(false);continue}
      ctx.save();ctx.translate(p.x,p.y);ctx.rotate(p.rot);ctx.scale(1,.35+.65*Math.abs(Math.sin(p.fl)));
      const s=p.s;
      ctx.fillStyle=`hsla(${p.h},85%,${p.l}%,.88)`;
      ctx.beginPath();ctx.moveTo(0,-s);ctx.quadraticCurveTo(s*.95,-s*.35,s*.12,s);ctx.lineTo(0,s*.78);ctx.lineTo(-s*.12,s);ctx.quadraticCurveTo(-s*.95,-s*.35,0,-s);ctx.fill();
      ctx.fillStyle=`hsla(${p.h-8},80%,${p.l-14}%,.5)`;ctx.beginPath();ctx.ellipse(0,s*.25,s*.18,s*.55,0,0,6.283);ctx.fill();
      ctx.restore();
    }
  }

  /* «Светлячки»: силуэты сосен рисуются один раз в отдельный холст */
  let trees=null,glow=null;
  function forest(){
    trees=document.createElement("canvas");trees.width=W;trees.height=H;
    const g=trees.getContext("2d");
    const layer=(base,hmin,hmax,col,alpha,step,seed)=>{
      let s=seed;const r=()=>{s=(s*9301+49297)%233280;return s/233280};
      const L=document.createElement("canvas");L.width=W;L.height=H;const q=L.getContext("2d");q.fillStyle=col;
      q.fillRect(0,base,W,H-base);
      for(let x=-30;x<W+40;x+=step*(.55+r()*.9)){
        const h=hmin+r()*(hmax-hmin),w=h*(.2+r()*.07);
        q.fillRect(x-w*.06,base-h*.12,w*.12,h*.14);
        for(let k=0;k<5;k++){
          const yb=base-h*.1-h*k*.17,ww=w*(1-k*.17),top=yb-h*.36;
          q.beginPath();q.moveTo(x-ww,yb);q.quadraticCurveTo(x-ww*.35,yb-h*.06,x,top);q.quadraticCurveTo(x+ww*.35,yb-h*.06,x+ww,yb);q.closePath();q.fill();
        }
      }
      g.globalAlpha=alpha;g.drawImage(L,0,0);g.globalAlpha=1;
    };
    layer(H*.84,H*.16,H*.3,"#0f4a35",.45,38,7);
    const f1=g.createLinearGradient(0,H*.55,0,H);f1.addColorStop(0,"rgba(120,220,170,0)");f1.addColorStop(1,"rgba(120,220,170,.07)");g.fillStyle=f1;g.fillRect(0,H*.55,W,H*.45);
    layer(H*.92,H*.22,H*.4,"#08281d",.85,52,21);
    layer(H*.965,H*.26,H*.5,"#020a07",1,80,43);
    glow=document.createElement("canvas");glow.width=glow.height=48;
    const q=glow.getContext("2d"),gr=q.createRadialGradient(24,24,0,24,24,24);
    gr.addColorStop(0,"rgba(255,255,215,1)");gr.addColorStop(.18,"rgba(235,255,150,.9)");gr.addColorStop(.45,"rgba(170,255,110,.28)");gr.addColorStop(1,"rgba(140,255,100,0)");
    q.fillStyle=gr;q.fillRect(0,0,48,48);
  }
  function fireflies(dt){
    for(const f of B){                                          // туман
      f.x+=f.vx*dt;if(f.x<-f.rx) f.x=W+f.rx;if(f.x>W+f.rx) f.x=-f.rx;
      const g=ctx.createRadialGradient(f.x,f.y,0,f.x,f.y,f.rx);g.addColorStop(0,`rgba(150,230,190,${f.a})`);g.addColorStop(1,"rgba(150,230,190,0)");
      ctx.save();ctx.translate(f.x,f.y);ctx.scale(1,f.ry/f.rx);ctx.translate(-f.x,-f.y);ctx.fillStyle=g;ctx.beginPath();ctx.arc(f.x,f.y,f.rx,0,6.283);ctx.fill();ctx.restore();
    }
    if(trees) ctx.drawImage(trees,0,0);
    ctx.globalCompositeOperation="lighter";
    for(const p of A){                                          // светлячки
      p.ang+=Math.sin(t*p.turn+p.ph)*.9*dt;p.x+=Math.cos(p.ang)*p.v*dt;p.y+=Math.sin(p.ang)*p.v*.6*dt-4*dt;
      if(p.x<-20) p.x=W+20;if(p.x>W+20) p.x=-20;if(p.y<H*.08) p.y=H+10;if(p.y>H+20) p.y=H*.1;
      const a=Math.pow(Math.max(0,Math.sin(t*1.3*p.s+p.ph)),2);
      if(a<.02) continue;
      const sz=22*p.r*(.7+.5*a);ctx.globalAlpha=a;ctx.drawImage(glow,p.x-sz/2,p.y-sz/2,sz,sz);
    }
    ctx.globalAlpha=1;ctx.globalCompositeOperation="source-over";
  }

  /* «Люкс»: золотая пыль и широкие блики света */
  function lux(dt){
    const S=B[0];
    if(S.x<0){S.cd-=dt;if(S.cd<=0) S.x=0}
    else{
      S.x+=dt*.1;const x=(S.x*1.6-.3)*W,w=W*.22;
      const g=ctx.createLinearGradient(x-w,0,x+w,w*.7);
      g.addColorStop(0,"rgba(255,226,150,0)");g.addColorStop(.5,"rgba(255,232,170,.075)");g.addColorStop(1,"rgba(255,226,150,0)");
      ctx.fillStyle=g;ctx.fillRect(0,0,W,H);
      if(S.x>1){S.x=-1;S.cd=rnd(6,11)}
    }
    ctx.globalCompositeOperation="lighter";
    for(const p of A){
      p.y+=p.vy*dt;p.x+=(p.vx+Math.sin(t*.6+p.ph)*4)*dt;
      if(p.y>H+4){p.y=-4;p.x=Math.random()*W}if(p.x<-4) p.x=W+4;if(p.x>W+4) p.x=-4;
      const a=.25+.75*Math.pow(Math.max(0,Math.sin(t*1.4*p.s+p.ph)),2);
      ctx.globalAlpha=a;
      if(p.gl&&a>.55){
        const r=3+5*a;ctx.fillStyle="#fff6d6";ctx.beginPath();
        ctx.moveTo(p.x,p.y-r);ctx.quadraticCurveTo(p.x+r*.12,p.y-r*.12,p.x+r,p.y);ctx.quadraticCurveTo(p.x+r*.12,p.y+r*.12,p.x,p.y+r);
        ctx.quadraticCurveTo(p.x-r*.12,p.y+r*.12,p.x-r,p.y);ctx.quadraticCurveTo(p.x-r*.12,p.y-r*.12,p.x,p.y-r);ctx.fill();
      }else{ctx.fillStyle=p.r>1.3?"#f6e27a":"#d4a537";ctx.fillRect(p.x,p.y,p.r,p.r)}
    }
    ctx.globalAlpha=1;ctx.globalCompositeOperation="source-over";
  }
  /* «Туманность»: облака рисуются один раз в маленький холст и мягко растягиваются */
  let neb=null;
  function nebMake(){
    const w=Math.max(80,Math.ceil(W/3)),h=Math.max(60,Math.ceil(H/3));
    neb=document.createElement("canvas");neb.width=w;neb.height=h;const q=neb.getContext("2d");
    const cols=[[124,58,237],[34,211,238],[219,39,119],[79,70,229],[167,139,250],[56,189,248]];
    q.globalCompositeOperation="lighter";
    for(let i=0;i<90;i++){
      const u=Math.random(),x=w*(u*1.2-.1)+rnd(-w*.1,w*.1),y=h*(.82-.62*u)+Math.sin(u*7)*h*.08+rnd(-h*.13,h*.13),r=rnd(w*.04,w*.17);
      const c=cols[(Math.random()*cols.length)|0],g=q.createRadialGradient(x,y,0,x,y,r);
      g.addColorStop(0,`rgba(${c[0]},${c[1]},${c[2]},${rnd(.07,.2).toFixed(3)})`);g.addColorStop(1,`rgba(${c[0]},${c[1]},${c[2]},0)`);
      q.fillStyle=g;q.fillRect(x-r,y-r,r*2,r*2);
    }
    q.globalCompositeOperation="destination-out";
    for(let i=0;i<16;i++){
      const u=Math.random(),x=w*u,y=h*(.8-.6*u)+rnd(-h*.05,h*.05),r=rnd(w*.02,w*.07),g=q.createRadialGradient(x,y,0,x,y,r);
      g.addColorStop(0,"rgba(0,0,0,.35)");g.addColorStop(1,"rgba(0,0,0,0)");q.fillStyle=g;q.fillRect(x-r,y-r,r*2,r*2);
    }
    q.globalCompositeOperation="lighter";
    for(let i=0;i<5;i++){
      const u=.15+Math.random()*.7,x=w*u,y=h*(.8-.6*u),r=rnd(w*.015,w*.03),g=q.createRadialGradient(x,y,0,x,y,r);
      g.addColorStop(0,"rgba(255,255,255,.5)");g.addColorStop(1,"rgba(200,180,255,0)");q.fillStyle=g;q.fillRect(x-r,y-r,r*2,r*2);
    }
  }
  function nebula(dt){
    const ox=Math.sin(t*.035)*W*.02,oy=Math.cos(t*.028)*H*.02;
    if(neb){ctx.globalAlpha=.6+.08*Math.sin(t*.4);ctx.imageSmoothingEnabled=true;ctx.drawImage(neb,-W*.05+ox,-H*.05+oy,W*1.1,H*1.1)}
    for(const p of A){
      const a=(.3+.7*(.5+.5*Math.sin(t*p.s+p.ph)))*p.z;
      let x=(p.x+ox*p.z*3)%W,y=(p.y+oy*p.z*3)%H;if(x<0) x+=W;if(y<0) y+=H;
      ctx.globalAlpha=a;ctx.fillStyle=p.c;ctx.fillRect(x,y,p.r,p.r);
      if(p.r>1.35&&p.z>.7){ctx.globalAlpha=a*.5;ctx.fillRect(x-3,y+p.r/2-.25,6+p.r,.5);ctx.fillRect(x+p.r/2-.25,y-3,.5,6+p.r)}
    }
    const S=B[0];S.t-=dt;
    if(S.t<0){
      if(S.x==null){S.x=rnd(W*.1,W*.8);S.y=rnd(0,H*.45);S.a=rnd(.35,.65)}
      const q=-S.t/.9;
      if(q>1){S.t=rnd(2.5,6);S.x=null}
      else{
        const L=Math.min(W,H)*.22,x=S.x+Math.cos(S.a)*q*W*.35,y=S.y+Math.sin(S.a)*q*W*.35,tx=x-Math.cos(S.a)*L,ty=y-Math.sin(S.a)*L;
        const g=ctx.createLinearGradient(x,y,tx,ty);g.addColorStop(0,`rgba(255,255,255,${(1-q).toFixed(3)})`);g.addColorStop(1,"rgba(170,190,255,0)");
        ctx.globalAlpha=1;ctx.strokeStyle=g;ctx.lineWidth=1.8;ctx.lineCap="round";ctx.beginPath();ctx.moveTo(x,y);ctx.lineTo(tx,ty);ctx.stroke();
      }
    }
    ctx.globalAlpha=1;
  }
  function frame(now){
    requestAnimationFrame(frame);
    if(now-last<fpsGap(48,14)) return;
    const dt=Math.min(.05,(now-last)/1000);last=now;
    if(window.BGPAUSE) return;
    if(!live()){if(!idle){ctx.clearRect(0,0,W,H);idle=true}return}
    if(idle||kind!==ST.design){idle=false;size()}
    t+=dt;ctx.clearRect(0,0,W,H);
    if(kind==="ocean") ocean(dt);else if(kind==="fireflies") fireflies(dt);else if(kind==="lux") lux(dt);else if(kind==="nebula") nebula(dt);else sakura(dt);
  }
  addEventListener("resize",()=>{if(!idle) size()});
  requestAnimationFrame(frame);
})();

/* ---------- цвет фона: оттенок темы + (по желанию) очень медленное переливание ---------- */
(()=>{
  const PERIOD=300, t0=Date.now(), root=document.documentElement;   // полный цикл — 5 минут
  const reduce=matchMedia("(prefers-reduced-motion: reduce)").matches;
  const tick=()=>{
    const t=(Date.now()-t0)/1000;
    // от красного через малиновый и фиолетовый к синему и обратно (зелёного и жёлтого нет)
    const cyc=(ST.cycle&&!reduce&&!ST.eco)?-80*(1-Math.cos(2*Math.PI*t/PERIOD)):0;
    const off=stShift()+cyc;
    FX.th=stShift();FX.bo=off;
    const bv=off.toFixed(1);if(root.__bo!==bv){root.__bo=bv;root.style.setProperty("--bo",bv)}   // не будим перерасчёт стилей всей страницы без нужды
  };
  tick();setInterval(()=>{if(!document.hidden) tick()},2000);addEventListener("st-change",tick);
})();

/* ---------- хранилище ---------- */
async function loadTracks(){
  try{const r=await fetch("/api/tracks",{cache:"no-store"});if(r.ok) return await r.json()}catch(e){}
  try{return JSON.parse(localStorage.getItem("tracks")||"[]")}catch(e){return []}
}
let saveTimer;
const ALBC={map:null};                        // обложки альбомов: albumId → версия картинки
function albCov(aid){
  if(!aid) return 0;
  if(!ALBC.map){ALBC.map=new Map();state.tracks.forEach(t=>{if(t.albumId&&t.albCover) ALBC.map.set(t.albumId,Math.max(ALBC.map.get(t.albumId)||0,t.albCover))})}
  return ALBC.map.get(aid)||0;
}
const albCovUrl=aid=>{const v=albCov(aid);return v?`/api/img/album/${aid}?v=${v}`:""};
const hasCov=t=>!!(t.cover||albCov(t.albumId));
/* обложка трека из каталога (Deezer / Apple Music): сервер сам скачивает её и сохраняет.
   Сначала по прямой ссылке, если не вышло — заново узнаёт ссылку у площадки по номеру трека. */
const catCovBusy=new Set();
async function saveCatCover(id,k,url){
  if(catCovBusy.has(id)) return false;catCovBusy.add(id);
  try{
    const tries=[];
    if(url) tries.push(`/api/cat/savecover?id=${id}&u=${encodeURIComponent(url)}`);
    if(k&&/^(dz|it|sc):\d+$/.test(k)) tries.push(`/api/cat/fixcover?id=${id}&k=${encodeURIComponent(k)}`);
    for(const q of tries) for(let n=0;n<2;n++){
      try{
        const r=await fetch(q,{method:"POST"});
        if(r.ok){
          const t=state.tracks.find(x=>x.id===id);
          if(t){t.cover=Date.now();persist();renderLib();
            if(state.editing===id&&!state.coverBlob){state.cover=t.cover;state.coverPrev="";paintCover()}}
          return true;
        }
        if(r.status===400||r.status===404) break;
      }catch(e){}
      await new Promise(f=>setTimeout(f,700));
    }
    return false;
  }finally{catCovBusy.delete(id)}
}
function persist(){
  ALBC.map=null;
  clearTimeout(persist.sc);persist.sc=setTimeout(()=>{try{ME.scan()}catch(e){}},350);   // награды за новые оценки
  clearTimeout(saveTimer);
  saveTimer=setTimeout(async()=>{
    const body=JSON.stringify(state.tracks);
    try{const r=await fetch("/api/tracks",{method:"POST",body,headers:{"Content-Type":"application/json"}});if(r.ok) return}catch(e){}
    try{localStorage.setItem("tracks",body)}catch(e){}
  },120);
}

/* ---------- слайдеры ---------- */
const rowsEl=$("#rows"),rowEls=[];
/* клавиша «только целые»: пока она зажата, ползунок критерия ставится на целые значения */
const INT={held:!ST.decimals,keys:new Set()};document.body.classList.toggle("intm",INT.held);
const intKeyOn=e=>{
  if(!ST.decimals) return true;      // десятичные выключены в настройках — всегда только целые
  const k=ST.intKey;if(!k) return false;
  if(INT.keys.has(k)) return true;
  return !!e&&((k.startsWith("Shift")&&e.shiftKey)||(k.startsWith("Control")&&e.ctrlKey)||(k.startsWith("Alt")&&e.altKey)||(k.startsWith("Meta")&&e.metaKey));
};
function intSync(){
  const on=intKeyOn();
  if(on===INT.held) return;
  INT.held=on;document.body.classList.toggle("intm",on);
  state.vals.forEach((v,i)=>{try{setRow(i)}catch(e){}});
}
window.addEventListener("keydown",e=>{if(e.code&&!e.repeat){INT.keys.add(e.code);intSync()}},true);
window.addEventListener("keyup",e=>{if(e.code){INT.keys.delete(e.code);intSync()}},true);
window.addEventListener("blur",()=>{INT.keys.clear();intSync()});
window.addEventListener("st-change",()=>{INT.keys.clear();intSync()});
function setRow(i){
  const row=rowEls[i],v=state.vals[i],sl=$(".sl",row);
  sl.setAttribute("aria-valuenow",v);
  const b=$(".val b",row);
  const vs=INT.held&&Number.isInteger(+v)?String(+v):(+v).toFixed(1);
  if(b.textContent!==vs){
    b.textContent=vs;
    b.animate([{transform:"scale(1.3)"},{transform:"scale(1)"}],{duration:200,easing:"ease-out"});
  }
}
function setPos(i,pct){rowEls[i].style.setProperty("--p",pct+"%");$(".sl",rowEls[i]).style.setProperty("--p",pct+"%")}
function setVal(i,v){state.vals[i]=v;setPos(i,v*10);setRow(i);updateScore()}

let _scRaf=0;
function schedScore(){if(_scRaf) return;_scRaf=requestAnimationFrame(()=>{_scRaf=0;updateScore()})}   // при перетаскивании — не чаще одного раза за кадр
function bindSlider(row,i){
  const sl=$(".sl",row),rail=$(".rail",row);
  let drag=false;
  const fromPointer=e=>{
    const r=rail.getBoundingClientRect();
    const f=Math.min(1,Math.max(0,(e.clientX-r.left)/r.width));
    const whole=intKeyOn(e);
    const v=whole?Math.round(f*10):Math.round(f*100)/10;
    sl.style.setProperty("--p",(whole?v*10:f*100)+"%");
    if(v!==state.vals[i]){state.vals[i]=v;setRow(i);schedScore()}
  };
  sl.addEventListener("pointerdown",e=>{
    if(e.button!==0) return;
    e.preventDefault();                                  // не даём браузеру начать выделение / перетаскивание (курсор «стоп»)
    try{const s=getSelection();if(s&&s.rangeCount) s.removeAllRanges()}catch(x){}
    drag=true;FX.warp=3.5;document.body.classList.add("sdrag");try{sl.setPointerCapture(e.pointerId)}catch(x){}
    sl.classList.add("drag");row.classList.add("drag");sl.focus({preventScroll:true});
    fromPointer(e);
  });
  sl.addEventListener("pointermove",e=>{if(drag) fromPointer(e)});
  const end=()=>{
    if(!drag) return;drag=false;FX.warp=1;document.body.classList.remove("sdrag");
    sl.classList.remove("drag");row.classList.remove("drag");
    sl.style.setProperty("--p",state.vals[i]*10+"%");
  };
  sl.addEventListener("pointerup",end);
  sl.addEventListener("pointercancel",end);
  sl.addEventListener("lostpointercapture",end);
  sl.addEventListener("dragstart",e=>e.preventDefault());
  sl.addEventListener("keydown",e=>{
    let v=state.vals[i];const dv=(e.shiftKey||!ST.decimals)?1:.1;   // стрелки: 0.1, Shift+стрелки: 1
    if(e.key==="ArrowRight"||e.key==="ArrowUp") v+=dv;
    else if(e.key==="ArrowLeft"||e.key==="ArrowDown") v-=dv;
    else if(e.key==="Home") v=0;
    else if(e.key==="End") v=10;
    else return;
    e.preventDefault();setVal(i,r1(Math.max(0,Math.min(10,ST.decimals?v:Math.round(v)))));
  });
}
CRIT.forEach((c,i)=>{
  const row=document.createElement("div");
  row.className="row";
  row.innerHTML=`<div class="idx">${i+1}</div>
    <div class="nm"><b>${c[0]}<em class="wt${WEIGHTS[i]>.2?" hi":""}" title="Вес критерия в итоговой оценке">${Math.round(WEIGHTS[i]*1000)/10}%</em></b><span>${c[1]}</span></div>
    <div class="sl" tabindex="0" role="slider" aria-label="${c[0]}" aria-valuemin="0" aria-valuemax="10" aria-valuenow="5">
      <div class="rail"><div class="fill"></div></div>
      <div class="ticks">${"<i></i>".repeat(11)}</div>
      <div class="thumb"></div>
    </div>
    <div class="val"><b>5.0</b><i>/10</i></div>`;
  rowsEl.append(row);rowEls.push(row);bindSlider(row,i);
});
rowsEl.addEventListener("dragstart",e=>e.preventDefault());
rowsEl.addEventListener("selectstart",e=>{if(e.target.closest&&e.target.closest(".sl")) e.preventDefault()});

/* ---------- итоговая оценка ---------- */
const main=$("#main"),ring=$("#ring"),numEl=$("#num"),C=527.79;
let shown=0,raf=0;
function countTo(to){
  cancelAnimationFrame(raf);
  const from=shown,t0=performance.now(),dur=550;
  const step=t=>{
    const k=Math.min(1,(t-t0)/dur),e=1-Math.pow(1-k,3);
    shown=from+(to-from)*e;numEl.textContent=fmt(shown);
    if(k<1) raf=requestAnimationFrame(step);else shown=to;
  };
  raf=requestAnimationFrame(step);
}
function updateScore(){
  const s=total(),v=verdict(s);
  {const hv=hueFor(s).toFixed(1);if(main.__h!==hv){main.__h=hv;main.style.setProperty("--h",hv)}}
  ring.style.strokeDashoffset=C*(1-s/10);
  {const h=rgHue(s),st=(window.__rgS||(window.__rgS=document.querySelectorAll("#rg stop")));if(st[0]){st[0].style.stopColor=`hsl(${h.toFixed(0)} 90% 64%)`;st[1].style.stopColor=`hsl(${(h-14).toFixed(0)} 90% 50%)`}}
  numEl.style.color=s>0?rgCol(s):"";
  ring.style.opacity=s>0?1:0;
  countTo(s);
  $("#verd").textContent=v[1];
  $("#blurb").textContent=v[2];
  $("#heroScore").innerHTML=`<b style="color:${rgCol(s)}">${fmt(s)}</b> из 10 · ${v[1]}`;
  $("#scoreCard").classList.toggle("hot",s>=9);
}
function renderStats(){
  const t=state.tracks.filter(isRated);
  $("#stN").textContent=t.length;
  $("#cnt").textContent=state.tracks.length;
  $("#stA").textContent=t.length?fmt(t.reduce((a,b)=>a+b.total,0)/t.length):"—";
  $("#stB").textContent=t.length?fmt(Math.max(...t.map(x=>x.total))):"—";
  fillGenres();fillAlbums();
}

/* ---------- медиатека ---------- */
const GARROW='<svg class="ico gar" viewBox="0 0 24 24"><path d="M6 9l6 6 6-6"/></svg>';
const shutGenres=new Set((()=>{try{return JSON.parse(localStorage.getItem("shutGenres")||"[]")}catch(e){return []}})());
function saveShut(){try{localStorage.setItem("shutGenres",JSON.stringify([...shutGenres]))}catch(e){}}
function paintAlbum(){
  const cur=state.editing!=null&&state.tracks.find(x=>x.id===state.editing);
  const a=cur&&cur.albumId?albumInfo(cur.albumId):null;
  $("#albc").hidden=!a;
  if(!a) return;
  $("#albName").textContent=a.name;$("#albName").title=a.name;
  $("#albProg").textContent=`оценено ${a.done} из ${a.n}`;
  $("#albSh").dataset.ash=a.id;$("#albSh").hidden=!a.done;
  const sc=$("#albScore");
  if(a.complete){sc.textContent=fmt(a.avg);sc.style.color=rgCol(a.avg)}
  else{sc.textContent="—";sc.style.color=""}
  $("#albCap").textContent=a.complete?(a.n>=2&&a.n<=6?"общая оценка EP":"общая оценка альбома"):a.done?`пока ${fmt(a.avg)} — итог появится, когда оценишь все треки`:"итог появится, когда оценишь все треки";
  $("#albList").innerHTML=a.items.slice().sort(byOrd).map(x=>`<div class="albr${x.id===state.editing?" on":""}${x.unrated?" un":""}" data-id="${x.id}"><span>${esc(x.title)}</span><b>${x.unrated?"—":fmt(x.total)}</b></div>`).join("");
}
const artistNames=t=>{
  const seen=new Set(),out=[];
  String(t.artist||"").split(/[,，]/).forEach(x=>{const n=x.trim(),k=n.toLowerCase();if(n&&n!=="—"&&!seen.has(k)){seen.add(k);out.push(n)}});
  return out;
};
function catOpts(c){
  const m=new Map(),add=(k,n)=>{const o=m.get(k);if(o) o.c++;else m.set(k,{k,n,c:1})};
  state.tracks.forEach(t=>{
    if(c==="artist") artistNames(t).forEach(n=>add(n.toLowerCase(),n));
    else if(c==="genre"){const g=(t.genre||"").trim();if(g) add(g.toLowerCase(),g.charAt(0).toUpperCase()+g.slice(1))}
    else if(t.albumId) add(String(t.albumId),t.album||"Альбом");
  });
  return [...m.values()].sort((a,b)=>b.c-a.c||a.n.localeCompare(b.n,"ru"));
}
const catMatch=t=>state.cat==="artist"?artistNames(t).some(n=>n.toLowerCase()===state.catVal)
  :state.cat==="genre"?(t.genre||"").trim().toLowerCase()===state.catVal:String(t.albumId||"")===state.catVal;
let lastCat="";
function renderCats(){
  let o=state.cat?catOpts(state.cat):[];
  if(state.catVal&&!o.some(x=>x.k===state.catVal)) state.catVal="";
  document.querySelectorAll("#cats .cat").forEach(b=>b.classList.toggle("on",b.dataset.c===state.cat));
  $("#catWrap").classList.toggle("open",!!state.cat);
  if(!state.cat) return;                                    // содержимое остаётся, пока строка плавно схлопывается
  const sub=$("#catSub"),sl=lastCat===state.cat?sub.scrollLeft:0;lastCat=state.cat;
  sub.innerHTML=o.length?o.map(x=>`<button class="cpill${x.k===state.catVal?" on":""}" data-k="${esc(x.k)}" type="button" title="${esc(x.n)}"><span>${esc(x.n)}</span><em>${x.c}</em></button>`).join("")
    :`<span class="cn">${{artist:"Пока нет исполнителей",genre:"Жанры ещё не указаны",album:"Альбомов пока нет"}[state.cat]}</span>`;
  sub.scrollLeft=sl;
}
function renderLib(){
  try{AU.refresh()}catch(e){}
  paintAlbum();renderCats();try{LB.plates()}catch(e){}
  let list=state.tracks.slice();
  const f=state.filter;
  if(state.cat&&state.catVal) list=list.filter(catMatch);
  if(f!=="all") list=list.filter(isRated);
  if(f==="hi") list=list.filter(t=>t.total>=8);
  else if(f==="mid") list=list.filter(t=>t.total>=5&&t.total<8);
  else if(f==="lo") list=list.filter(t=>t.total<5);
  const q=state.q.trim().toLowerCase();
  if(q) list=list.filter(t=>(t.title+" "+t.artist+" "+(t.beat||"")+" "+(t.genre||"")).toLowerCase().includes(q));
  const sv=t=>t.unrated?-1:t.total;
  list.sort(state.sort==="score"?(a,b)=>sv(b)-sv(a):(a,b)=>b.id-a.id);
  const el=$("#list");
  if(!list.length){
    el.innerHTML=state.tracks.length
      ?`<div class="empty"><b>Ничего не найдено</b><p>Попробуй изменить фильтр или запрос</p></div>`
      :`<div class="empty"><div class="bigic">${NOTE}</div><b>Здесь пока пусто</b><p>Оцени первый трек — он появится в твоей медиатеке</p></div>`;
    return;
  }
  const itemHTML=t=>{
    const on=t.id===state.editing;
    return `<div class="item${on?" on":""}" data-id="${t.id}">
      <div class="cv" style="${coverCss(t)}">${hasCov(t)?"":NOTE}</div>
      <div class="tx"><b>${esc(t.title)}</b><span>${esc(t.artist)}${t.beat?' <em class="pb">prod. '+esc(t.beat)+'</em>':""}${t.genre?" · "+esc(t.genre):""}${t.album&&state.sort!=="album"?" · «"+esc(t.album)+"»":""}</span></div>
      ${t.unrated?'<div class="sc un" title="Ещё не оценён">—</div>':`<div class="sc" style="color:${rgCol(t.total)}">${fmt(t.total)}</div>`}
      <button class="del" title="Удалить">${TRASH}</button>
    </div>`;
  };
  const searching=!!q;                                   // при поиске все группы раскрыты, чтобы ничего не спряталось
  const grpHTML=(key,label,meta,items)=>{
    const shut=!searching&&shutGenres.has(key);
    return `<div class="grp${shut?" shut":""}" data-g="${esc(key)}"><div class="gh" role="button" tabindex="0" aria-expanded="${!shut}" title="Свернуть / развернуть"><div class="gl">${GARROW}<b>${esc(label)}</b></div><span>${meta}</span></div><div class="gb"><div class="gbi">${items.map(itemHTML).join("")}</div></div></div>`;
  };
  if(state.sort==="genre"){
    const groups=new Map();
    list.forEach(t=>{
      const g=(t.genre||"").trim(),k=g.toLowerCase();
      if(!groups.has(k)) groups.set(k,{name:g,items:[]});
      groups.get(k).items.push(t);
    });
    const arr=[...groups.values()].sort((a,b)=>!a.name?1:!b.name?-1:a.name.localeCompare(b.name,"ru"));
    el.innerHTML=arr.map(g=>{
      g.items.sort((a,b)=>sv(b)-sv(a));
      const rt=g.items.filter(isRated),avg=rt.length?rt.reduce((s,t)=>s+t.total,0)/rt.length:0;
      return grpHTML(g.name.toLowerCase(),g.name?g.name.charAt(0).toUpperCase()+g.name.slice(1):"Без жанра",rt.length?`${g.items.length} · ср. ${fmt(avg)}`:`${g.items.length}`,g.items);
    }).join("");
  }else if(state.sort==="album"){
    const groups=new Map(),singles=[];
    list.forEach(t=>{
      if(!t.albumId){singles.push(t);return}
      if(!groups.has(t.albumId)) groups.set(t.albumId,{id:t.albumId,name:t.album,items:[]});
      groups.get(t.albumId).items.push(t);
    });
    const arr=[...groups.values()].sort((a,b)=>a.name.localeCompare(b.name,"ru"));
    el.innerHTML=arr.map(g=>{
      g.items.sort(byOrd);                    // в порядке альбома
      const a=albumInfo(g.id);
      return grpHTML("album:"+g.id,g.name,a.complete?`${a.n} · альбом ${fmt(a.avg)}`:`оценено ${a.done} из ${a.n}`,g.items);
    }).join("")+(singles.length?grpHTML("singles","Синглы",`${singles.length}`,singles.sort((a,b)=>b.id-a.id)):"");
  }else el.innerHTML=list.map(itemHTML).join("");
}
function toggleGenre(gh){
  const grp=gh.closest(".grp");if(!grp) return;
  const k=grp.dataset.g,shut=!grp.classList.contains("shut");
  grp.classList.toggle("shut",shut);gh.setAttribute("aria-expanded",String(!shut));
  if(shut) shutGenres.add(k);else shutGenres.delete(k);
  saveShut();
}
$("#list").addEventListener("click",e=>{const gh=e.target.closest(".gh");if(gh) toggleGenre(gh)});
$("#list").addEventListener("keydown",e=>{
  const gh=e.target.closest&&e.target.closest(".gh");
  if(gh&&(e.key==="Enter"||e.key===" ")){e.preventDefault();toggleGenre(gh)}
});
$("#list").addEventListener("click",e=>{
  const item=e.target.closest(".item");if(!item) return;
  const id=+item.dataset.id;
  if(e.target.closest(".del")){removeTrack(id);return}
  loadTrack(id);
});
$("#albList").addEventListener("click",e=>{const r=e.target.closest(".albr");if(r) loadTrack(+r.dataset.id)});
$("#chips").addEventListener("click",e=>{
  const c=e.target.closest(".chip");if(!c) return;
  state.filter=c.dataset.f;
  document.querySelectorAll(".chip").forEach(x=>x.classList.toggle("on",x===c));
  renderLib();
});
$("#cats").addEventListener("click",e=>{
  const b=e.target.closest(".cat");if(!b) return;
  state.cat=state.cat===b.dataset.c?"":b.dataset.c;state.catVal="";
  renderLib();
});
$("#catSub").addEventListener("click",e=>{
  const b=e.target.closest(".cpill");if(!b) return;
  state.catVal=state.catVal===b.dataset.k?"":b.dataset.k;
  renderLib();
});
$("#catSub").addEventListener("wheel",e=>{if(Math.abs(e.deltaY)>Math.abs(e.deltaX)){e.preventDefault();e.currentTarget.scrollLeft+=e.deltaY}},{passive:false});
$("#q").addEventListener("input",e=>{state.q=e.target.value;renderLib()});
$("#sort").addEventListener("click",()=>{
  state.sort=state.sort==="new"?"score":state.sort==="score"?"genre":state.sort==="genre"?"album":"new";
  $("#sort").textContent={new:"Недавние",score:"По оценке",genre:"По жанрам",album:"По альбомам"}[state.sort];
  renderLib();
});

/* ---------- редактор ---------- */
const titleEl=$("#title"),beatEl=$("#beat"),artistEl=$("#artist"),genreEl=$("#genre"),albumEl=$("#album"),coverEl=$("#cover");
/* длинное название не обрезается: шрифт уменьшается, пока строка не поместится */
const fitCtx=document.createElement("canvas").getContext("2d");
function fitTitle(){
  const el=titleEl;el.style.fontSize="";
  const w=el.clientWidth;if(!el.value||!w) return;
  const cs=getComputedStyle(el),fs=parseFloat(cs.fontSize),ls=parseFloat(cs.letterSpacing)||0;
  fitCtx.font=`${cs.fontWeight} ${fs}px ${cs.fontFamily}`;
  const text=fitCtx.measureText(el.value).width+ls*el.value.length+fs*.1;   // ширина текста с запасом под последнюю букву
  if(text>w) el.style.fontSize=Math.max(22,Math.floor(fs*w/text))+"px";
}
let fitT=0;
try{new ResizeObserver(()=>{clearTimeout(fitT);fitT=setTimeout(fitTitle,60)}).observe(titleEl)}catch(e){addEventListener("resize",()=>{clearTimeout(fitT);fitT=setTimeout(fitTitle,120)})}
try{document.fonts.ready.then(fitTitle)}catch(e){}
function paintCover(){
  fitTitle();
  paintFull();
  const own=state.coverBlobUrl||(state.cover&&state.editing!=null?`/api/img/cover/${state.editing}?v=${state.cover}`:"")||state.coverPrev||"";
  const url=own||albCovUrl(state.albumId);
  coverEl.classList.toggle("has",!!url);
  coverEl.style.background=url?`url("${url}") center/cover no-repeat #222`:coverBg(state.artist,state.title);
  document.documentElement.style.setProperty("--amb",url?`url("${url}")`:coverBg(state.artist,state.title));
  $("#covx").hidden=!own;
}
function coverCss(t){
  if(t.cover) return `background:url(/api/img/cover/${t.id}?v=${t.cover}) center/cover no-repeat #222`;
  const a=albCovUrl(t.albumId);                 // своей обложки нет — берём обложку альбома
  return a?`background:url(${a}) center/cover no-repeat #222`:`background:${coverBg(t.artist==="—"?"":t.artist,t.title)}`;
}
function paintMode(){
  const ed=state.editing!==null,cur=ed&&state.tracks.find(x=>x.id===state.editing);
  $("#eyebrow").textContent=ed?(cur&&cur.unrated?"Оцени трек из альбома":"Редактирование оценки"):"Новая оценка";
  $("#eqTop").hidden=!ed;
  $("#delCur").hidden=!ed;
  try{SH.paint()}catch(e){}
  $("#save").title=ed?"Обновить оценку (Ctrl+Enter)":"Добавить в медиатеку (Ctrl+Enter)";
  paintNav();
}
/* ---------- переход между треками: «← Вернуться» / «Оценить следующий →» ---------- */
function navList(){
  const cur=state.editing!=null&&state.tracks.find(x=>x.id===state.editing);
  if(!cur) return [];
  return cur.albumId?state.tracks.filter(t=>t.albumId===cur.albumId).sort(byOrd):state.tracks.filter(t=>!t.albumId);
}
function paintNav(){
  const list=navList(),i=list.findIndex(t=>t.id===state.editing),show=list.length>1&&i>=0;
  const pv=show&&i>0,nx=show&&i<list.length-1;
  $("#tnav").hidden=!show;
  $("#navPrev").disabled=!pv;$("#navNext").disabled=!nx;
  for(const [id,on] of [["apPrev",pv],["apNext",nx]]){const b=$("#"+id);b.hidden=!show;b.disabled=!on}
  if(show){
    const a=list[i-1],b=list[i+1];
    $("#navPrev").title=a?"Назад: «"+a.title+"»":"Это первый трек";
    $("#navNext").title=b?"Дальше: «"+b.title+"»":"Это последний трек";
    $("#apPrev").title=$("#navPrev").title;$("#apNext").title=$("#navNext").title;
  }
}
function editorDirty(){
  const r=state.tracks.find(x=>x.id===state.editing);if(!r) return false;
  const base=!r.unrated&&r.scores?r.scores:Array(5).fill(stStart());
  return state.vals.some((v,i)=>+v!==+base[i])||state.title.trim()!==r.title||state.artist.trim()!==(r.artist==="—"?"":r.artist)
    ||state.beat.trim()!==(r.beat||"")||state.genre.trim()!==(r.genre||"")||state.album.trim()!==(r.album||"")||!!state.file||!!state.coverBlob;
}
function navTrack(d){
  const list=navList(),i=list.findIndex(t=>t.id===state.editing),n=list[i+d];
  if(!n) return;
  if(editorDirty()&&!confirm("Есть несохранённые изменения. Перейти к другому треку без сохранения?")) return;
  loadTrack(n.id);
}
$("#navPrev").addEventListener("click",()=>navTrack(-1));
$("#navNext").addEventListener("click",()=>navTrack(1));
$("#apPrev").addEventListener("click",()=>navTrack(-1));
$("#apNext").addEventListener("click",()=>navTrack(1));
function fillEditor(rec){
  state.artist=rec?(rec.artist==="—"?"":rec.artist):"";
  state.beat=rec?(rec.beat||""):"";beatEl.value=state.beat;
  state.genre=rec?(rec.genre||""):"";
  state.album=rec?(rec.album||""):"";state.albumId=rec?(rec.albumId||null):null;
  state.title=rec?rec.title:"";
  state.vals=rec&&!rec.unrated?[...rec.scores]:Array(5).fill(stStart());
  artistEl.value=state.artist;titleEl.value=state.title;genreEl.value=state.genre;albumEl.value=state.album;
  state.file=null;state.audio=rec&&rec.audio?rec.audio:null;state.audioId=rec?rec.id:null;
  state.stream=rec&&rec.stream?rec.stream:null;state.cat=rec&&rec.cat?rec.cat:null;state.catAlb=rec&&rec.catAlb?rec.catAlb:null;state.embed=rec&&rec.embed?rec.embed:null;AU.sync();
  if(state.coverBlobUrl) URL.revokeObjectURL(state.coverBlobUrl);
  state.coverBlob=null;state.coverBlobUrl="";state.coverPrev="";state.cover=rec&&rec.cover?rec.cover:null;state.catCov=null;state.catCovOff=false;
  state.vals.forEach((v,i)=>{setPos(i,v*10);setRow(i)});
  paintCover();updateScore();
}
function newTrack(){state.editing=null;fillEditor(null);paintMode();renderLib()}
function loadTrack(id){
  const t=state.tracks.find(x=>x.id===id);if(!t) return;
  if(PAGE_CLOSE) PAGE_CLOSE();
  state.editing=id;fillEditor(t);paintMode();renderLib();
  main.scrollTo({top:0,behavior:"smooth"});
}
function removeTrack(id){
  const old=state.tracks.find(x=>x.id===id);if(old&&old.audio) delAudio(id);if(old&&old.cover) delImg("cover",id);
  state.tracks=state.tracks.filter(x=>x.id!==id);
  if(state.editing===id){state.editing=null;fillEditor(null);paintMode()}
  persist();renderStats();renderLib();toast("Удалено из медиатеки");
}
function save(){
  const title=state.title.trim();
  if(!title){
    const w=$("#titlew");w.classList.remove("shake");void w.offsetWidth;w.classList.add("shake");
    toast("Введи название трека",true);titleEl.focus();return false;
  }
  const pv0=state.editing!=null?state.tracks.find(x=>x.id===state.editing):null,fresh=!pv0||!!pv0.unrated;
  if(fresh){                                            // новая оценка — не чаще раза в 30 секунд
    let left=0;try{left=ME.cdLeft()}catch(e){}
    if(left>0){
      const b=$("#save");b.classList.remove("shake");void b.offsetWidth;b.classList.add("shake");
      toast(`Перезарядка: следующую оценку можно поставить через ${Math.ceil(left/1000)} с`,true);return false;
    }
  }
  try{ME.noteRate()}catch(e){}
  const id=state.editing??Date.now(),file=state.file,prev=state.tracks.find(x=>x.id===id);
  const audio=file?{name:file.name,ext:extOf(file.name),size:file.size}:(state.audio||null);
  const coverBlob=state.coverBlob,cover=coverBlob?Date.now():(state.cover||null);
  const albName=state.album.trim();
  let albumId=null;
  if(albName){
    const same=state.tracks.find(t=>t.albumId&&low(t.album)===low(albName));
    if(same) albumId=same.albumId;                                       // такое имя уже есть — трек присоединяется к альбому
    else if(state.albumId){albumId=state.albumId;state.tracks.forEach(t=>{if(t.albumId===albumId) t.album=albName})}   // переименование всего альбома
    else albumId=Date.now();
  }
  const rec={id,artist:state.artist.trim()||"—",...(state.beat.trim()?{beat:state.beat.trim()}:{}),title,genre:state.genre.trim(),...(prev&&prev.ord!=null?{ord:prev.ord}:{}),scores:[...state.vals],total:total(),...(albName?{album:albName,albumId}:{}),...(audio?{audio}:{}),...(cover?{cover}:{}),...(state.cat?{cat:state.cat}:{}),...(state.catAlb?{catAlb:state.catAlb}:{}),...(state.embed?{embed:state.embed}:{}),...(state.stream?{stream:state.stream}:{})};
  const idx=state.tracks.findIndex(x=>x.id===rec.id);
  const upd=idx>=0;
  if(upd) state.tracks[idx]=rec;else state.tracks.push(rec);
  persist();renderStats();
  /* остаёмся на этом же треке — ничего не переключается и музыка не прерывается */
  const k0=AU.key();
  state.editing=rec.id;state.file=null;state.audio=rec.audio||null;state.audioId=rec.id;
  state.title=title;titleEl.value=title;state.beat=state.beat.trim();beatEl.value=state.beat;
  state.album=albName;albumEl.value=albName;state.albumId=albumId;
  state.coverBlob=null;state.cover=cover;           // превью обложки (coverBlobUrl) остаётся на экране
  AU.adopt(k0);
  paintMode();renderLib();paintCover();
  if(!upd){const it=$(`.item[data-id="${rec.id}"]`);if(it) it.classList.add("new")}
  toast(upd?"Оценка обновлена":"Добавлено в медиатеку");
  if(file) uploadAudio(rec.id,rec.audio.ext,file);
  else if(prev&&prev.audio&&!audio) delAudio(rec.id);
  if(coverBlob) uploadImg("cover",rec.id,coverBlob,()=>{const t=state.tracks.find(x=>x.id===rec.id);if(t){delete t.cover;persist();renderLib()}});
  else if(prev&&prev.cover&&!cover) delImg("cover",rec.id);
  else if(!cover&&state.cat&&!state.catCovOff&&(state.catCov||state.coverPrev||!prev)) saveCatCover(rec.id,state.cat,state.catCov);
  if(albumId&&prev&&prev.unrated){const a=albumInfo(albumId);if(a&&a.complete) toast(`Альбом «${a.name}» оценён: ${fmt(a.avg)}`)}
  if(fresh) try{ME.cdStart()}catch(e){}
  if(fresh) try{QA.ev("rate",{t:rec,cat:!!state.cat})}catch(e){}
  return true;
}
function exportCsv(){
  if(!state.tracks.length){toast("Список пуст",true);return}
  const q=v=>'"'+String(v).replace(/"/g,'""')+'"';
  const head=["Исполнитель","Битмейкер","Трек","Альбом","Жанр",...CRIT.map(c=>c[0]),"Итого /10"].map(q).join(";");
  const rows=state.tracks.map(t=>[t.artist,t.beat||"",t.title,t.album||"",t.genre||"",...(t.unrated?CRIT.map(()=>""):t.scores),t.unrated?"":String(t.total).replace(".",",")].map(q).join(";"));
  const blob=new Blob(["\ufeff"+[head,...rows].join("\r\n")],{type:"text/csv;charset=utf-8"});
  const a=document.createElement("a");
  a.href=URL.createObjectURL(blob);a.download="tracks.csv";
  document.body.append(a);a.click();a.remove();
  setTimeout(()=>URL.revokeObjectURL(a.href),3000);
  toast("CSV сохранён в «Загрузки»");
}
let toastTimer;
function toast(text,err){
  const t=$("#toast");t.classList.remove("cn");t.textContent=text;t.classList.toggle("err",!!err);
  t.classList.add("show");clearTimeout(toastTimer);
  toastTimer=setTimeout(()=>t.classList.remove("show"),2200);
}

titleEl.addEventListener("input",e=>{state.title=e.target.value;paintCover()});
artistEl.addEventListener("input",e=>{state.artist=e.target.value;paintCover()});
beatEl.addEventListener("input",e=>{state.beat=e.target.value});
genreEl.addEventListener("input",e=>{state.genre=e.target.value});
albumEl.addEventListener("input",e=>{state.album=e.target.value});
[titleEl,artistEl,beatEl,genreEl,albumEl].forEach(el=>el.addEventListener("keydown",e=>{if(e.key==="Enter") save()}));
function rateFx(e){
  const b=$("#save"),r=b.getBoundingClientRect(),bg=$(".rbg",b),l=$(".rl",b);
  const x=e&&e.clientX?e.clientX-r.left:r.width/2,y=e&&e.clientY?e.clientY-r.top:r.height/2;
  b.classList.remove("pop");void b.offsetWidth;b.classList.add("pop");
  const rp=document.createElement("i");rp.className="rpl";rp.style.left=x+"px";rp.style.top=y+"px";bg.append(rp);
  for(let i=0;i<12;i++){
    const sp=document.createElement("i"),a=i/12*6.283+Math.random()*.5,d=70+Math.random()*60;
    sp.className="rsp";sp.style.cssText=`left:${x}px;top:${y}px;--dx:${(Math.cos(a)*d).toFixed(0)}px;--dy:${(Math.sin(a)*d*.7).toFixed(0)}px;--s:${(3+Math.random()*4).toFixed(1)}px`;
    b.append(sp);
  }
  l.textContent="Оценено ✓";
  clearTimeout(rateFx.t);rateFx.t=setTimeout(()=>{l.textContent="Оценить"},1200);
  setTimeout(()=>{b.querySelectorAll(".rpl,.rsp").forEach(n=>n.remove());b.classList.remove("pop")},800);
}
function saveFx(e){if(save()) rateFx(e&&e.type==="click"?e:null)}
(()=>{                                                   // обратный отсчёт прямо на кнопке «Оценить»
  const b=$("#save"),cd=document.createElement("span");cd.className="rcd";cd.setAttribute("aria-hidden","true");
  cd.innerHTML='<i></i><svg viewBox="0 0 24 24"><circle cx="12" cy="13.5" r="7.5"/><path d="M12 10v3.6l2.4 2M9.5 3h5"/></svg><span></span>';b.append(cd);
  const sp=cd.querySelector("span");let was=false;
  setInterval(()=>{
    let left=0;try{left=ME.cdLeft()}catch(e){}
    const pv=state.editing!=null?state.tracks.find(x=>x.id===state.editing):null,on=left>0&&(!pv||!!pv.unrated);
    if(on!==was){b.classList.toggle("cd",on);was=on}
    if(on){sp.textContent=`Перезарядка · ${Math.ceil(left/1000)} с`;b.style.setProperty("--cdp",(left/ME.CD).toFixed(3))}
  },200);
})();
$("#save").addEventListener("click",saveFx);
$("#resetCrit").addEventListener("click",()=>{
  state.vals=Array(5).fill(stStart());
  state.vals.forEach((v,i)=>{setPos(i,v*10);setRow(i)});updateScore();
  toast("Критерии сброшены — сам трек остался");
});
$("#reset").addEventListener("click",()=>{state.editing=null;fillEditor(null);paintMode();renderLib()});
$("#delCur").addEventListener("click",()=>{if(state.editing!==null) removeTrack(state.editing)});
/* «Новая оценка» — выбор: свой файл или каталог площадок (бывший пункт «Каталог релизов») */
const NEWR=(()=>{
  const el=$("#nwk");
  const UP='<svg class="ico" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M17 8l-5-5-5 5"/><path d="M12 3v12"/></svg>';
  const SR='<svg class="ico" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg>';
  const X='<svg class="ico" viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg>';
  const isOpen=()=>!el.hidden&&!el.classList.contains("out");
  function open(){
    el.innerHTML=`<div class="nwc" role="dialog" aria-label="Новая оценка"><div class="nwh"><div><b>Новая оценка</b><span>Откуда возьмём трек?</span></div><button class="pround" data-x type="button" title="Закрыть (Esc)">${X}</button></div>
      <div class="nwg">
        <button class="nwo" data-o="up" type="button" style="--oc:var(--g2)"><span class="nwi">${UP}</span><b>Загрузить свой трек</b><p>MP3, WAV или FLAC с компьютера. Выбери сразу 2–6 файлов — получится EP, 7 и больше — альбом.</p><kbd>1</kbd></button>
        <button class="nwo" data-o="cat" type="button" style="--oc:#3bb4ff"><span class="nwi">${SR}</span><b>Найти на площадках</b><p>Каталог SoundCloud и Apple Music: ищи треки и альбомы, слушай превью и оценивай без скачивания.</p><kbd>2</kbd></button>
      </div><button class="nwm" data-o="man" type="button">или заполнить вручную, без файла</button></div>`;
    el.classList.remove("out");el.hidden=false;$("#navNew").classList.add("on");
    if(document.activeElement&&document.activeElement.blur) document.activeElement.blur();
  }
  function close(){if(el.hidden) return;$("#navNew").classList.remove("on");el.classList.add("out");setTimeout(()=>{el.hidden=true;el.classList.remove("out");el.innerHTML=""},220)}
  function pick(o){
    if(o==="cat"){close();CT.open();return}
    if(PAGE_CLOSE) PAGE_CLOSE();
    newTrack();main.scrollTo({top:0,behavior:"smooth"});
    if(o==="up") $("#file").click();else setTimeout(()=>titleEl.focus(),80);
    close();
  }
  el.addEventListener("click",e=>{
    if(e.target===el||e.target.closest("[data-x]")){close();return}
    const b=e.target.closest("[data-o]");if(b) pick(b.dataset.o);
  });
  document.addEventListener("keydown",e=>{
    if(!isOpen()) return;
    if(e.key==="Escape"){e.preventDefault();e.stopImmediatePropagation();close()}
    else if(e.key==="1"){e.preventDefault();pick("up")}
    else if(e.key==="2"){e.preventDefault();pick("cat")}
  },true);
  return{open,close,isOpen};
})();
$("#navNew").addEventListener("click",()=>NEWR.isOpen()?NEWR.close():NEWR.open());
document.addEventListener("keydown",e=>{if(e.ctrlKey&&e.key==="Enter"){e.preventDefault();saveFx(e)}});

const fsIc=$("#fsIc"),fsBtn=$("#fs");
const FS_IN='<path d="M4 14h6v6M20 10h-6V4M14 10l7-7M3 21l7-7"/>',FS_OUT='<path d="M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7"/>';
/* полный экран бывает двух видов: из страницы (requestFullscreen) и самого окна браузера (запуск приложения / F11) */
const isFs=()=>!!document.fullscreenElement||matchMedia("(display-mode: fullscreen)").matches||(innerHeight>=screen.height-2&&innerWidth>=screen.width-2);
function paintFs(){const f=isFs();fsIc.innerHTML=f?FS_IN:FS_OUT;fsBtn.title=f?"Оконный режим (F11)":"Во весь экран (F11)"}
fsBtn.addEventListener("click",async()=>{
  if(document.fullscreenElement){try{await document.exitFullscreen()}catch(e){}setTimeout(paintFs,120);return}
  if(isFs()){   // полный экран окна из страницы не выключить — просим сервер нажать F11 за нас
    let ok=false;
    try{const r=await fetch("/api/f11",{method:"POST"});ok=r.ok}catch(e){}
    if(!ok) toast("Не получилось переключить — нажми F11",true);
    setTimeout(paintFs,250);return;
  }
  try{await document.documentElement.requestFullscreen()}catch(e){}
  setTimeout(paintFs,120);
});
document.addEventListener("fullscreenchange",paintFs);
addEventListener("resize",()=>setTimeout(paintFs,80));
try{matchMedia("(display-mode: fullscreen)").addEventListener("change",paintFs)}catch(e){}
paintFs();

/* ---------- картинки: обложки треков и фото артистов ---------- */
const isImageFile=f=>/^image\//.test(f.type)||/\.(png|jpe?g|webp|gif|bmp|avif)$/i.test(f.name);
async function toJpeg(file,max=640){
  const bmp=await createImageBitmap(file);
  const side=Math.min(bmp.width,bmp.height),out=Math.min(max,side);          // квадрат по центру
  const c=document.createElement("canvas");c.width=c.height=out;
  const g=c.getContext("2d");g.fillStyle="#141012";g.fillRect(0,0,out,out);
  g.imageSmoothingQuality="high";
  g.drawImage(bmp,(bmp.width-side)/2,(bmp.height-side)/2,side,side,0,0,out,out);
  if(bmp.close) bmp.close();
  return await new Promise((res,rej)=>c.toBlob(b=>b?res(b):rej(new Error("toBlob")),"image/jpeg",.88));
}
let imgCb=null;
function pickImage(cb){imgCb=cb;$("#imgFile").click()}
$("#imgFile").addEventListener("change",e=>{const f=e.target.files[0];e.target.value="";if(f&&imgCb) imgCb(f)});
async function uploadImg(kind,id,blob,onFail){
  try{
    const r=await fetch(`/api/img/${kind}/${id}`,{method:"POST",body:blob});
    if(!r.ok) throw new Error(r.status);
  }catch(e){if(onFail) onFail();toast("Не удалось сохранить картинку",true)}
}
function delImg(kind,id){fetch(`/api/img/${kind}/${id}`,{method:"DELETE"}).catch(()=>{})}
function saveArtists(){
  fetch("/api/artists",{method:"POST",body:JSON.stringify(state.artistImg),headers:{"Content-Type":"application/json"}}).catch(()=>{});
}
async function setCoverFile(f,msg){
  try{
    const blob=await toJpeg(f);
    if(state.coverBlobUrl) URL.revokeObjectURL(state.coverBlobUrl);
    state.coverBlob=blob;state.coverBlobUrl=URL.createObjectURL(blob);
    paintCover();toast(msg||"Обложка выбрана. Нажми ✓, чтобы сохранить");
  }catch(e){toast("Не удалось открыть картинку",true)}
}
coverEl.addEventListener("click",e=>{if(e.target.closest("#covx")) return;pickImage(setCoverFile)});
$("#covx").addEventListener("click",e=>{
  e.stopPropagation();
  if(state.coverBlobUrl) URL.revokeObjectURL(state.coverBlobUrl);
  state.coverBlob=null;state.coverBlobUrl="";state.coverPrev="";state.cover=null;state.catCovOff=true;paintCover();
  toast("Обложка убрана. Нажми ✓, чтобы применить");
});

/* ---------- обложка, вшитая в сам аудиофайл (ID3 у MP3/WAV, PICTURE у FLAC) ---------- */
const readBytes=async(file,start,len)=>new Uint8Array(await file.slice(start,start+len).arrayBuffer());
const u32=(b,o)=>((b[o]<<24)|(b[o+1]<<16)|(b[o+2]<<8)|b[o+3])>>>0;
const sync32=(b,o)=>(b[o]<<21)|(b[o+1]<<14)|(b[o+2]<<7)|b[o+3];
const latin=(b,a,e)=>{let r="";for(let i=a;i<e;i++) r+=String.fromCharCode(b[i]);return r};
function sniffMime(d){
  if(d[0]===0x89&&d[1]===0x50) return "image/png";
  if(d[0]===0x47&&d[1]===0x49) return "image/gif";
  if(d[0]===0x52&&d[1]===0x49) return "image/webp";
  if(d[0]===0x42&&d[1]===0x4D) return "image/bmp";
  return "image/jpeg";
}
function id3Pictures(b){
  if(b.length<10||b[0]!==0x49||b[1]!==0x44||b[2]!==0x33) return [];
  const ver=b[3],flags=b[5];
  let data=b;
  if((flags&0x80)&&ver<4){                                   // тег целиком «unsynchronised»
    const out=new Uint8Array(b.length);let n=0;
    for(let i=0;i<b.length;i++){out[n++]=b[i];if(b[i]===0xFF&&b[i+1]===0) i++}
    data=out.subarray(0,n);
  }
  const end=Math.min(data.length,10+sync32(b,6));
  let pos=10;
  if(flags&0x40) pos+=ver===4?sync32(data,10):4+u32(data,10);  // расширенный заголовок
  const idLen=ver===2?3:4,hdr=ver===2?6:10,pics=[];
  while(pos+hdr<=end){
    if(data[pos]===0) break;
    const id=latin(data,pos,pos+idLen);
    const size=ver===2?((data[pos+3]<<16)|(data[pos+4]<<8)|data[pos+5]):ver===4?sync32(data,pos+4):u32(data,pos+4);
    const fl=ver===2?0:((data[pos+8]<<8)|data[pos+9]);
    const body=pos+hdr;
    if(size<=0||body+size>data.length) break;
    if(id==="APIC"||id==="PIC"){
      let f=body+((ver===4&&(fl&1))?4:0);const e=body+size,enc=data[f++];
      if(id==="APIC"){while(f<e&&data[f]!==0) f++;f++} else f+=3;       // MIME / формат
      const type=data[f++];
      if(enc===1||enc===2){while(f+1<e&&!(data[f]===0&&data[f+1]===0)) f+=2;f+=2}
      else{while(f<e&&data[f]!==0) f++;f++}                            // описание
      if(f<e) pics.push({type,data:data.subarray(f,e)});
    }
    pos=body+size;
  }
  return pics;
}
async function flacPictures(file,off){
  const mk=await readBytes(file,off,4);
  if(latin(mk,0,4)!=="fLaC") return [];
  let pos=off+4;const pics=[];
  for(let n=0;n<256;n++){
    const h=await readBytes(file,pos,4);if(h.length<4) break;
    const last=h[0]&0x80,type=h[0]&0x7f,len=(h[1]<<16)|(h[2]<<8)|h[3];
    pos+=4;
    if(type===6&&len<=32*1048576){
      const b=await readBytes(file,pos,len);
      let o=0;const ptype=u32(b,o);o+=4;
      o+=4+u32(b,o);                  // MIME
      o+=4+u32(b,o);                  // описание
      o+=16;                          // ширина, высота, глубина, цвета
      const n2=u32(b,o);o+=4;
      pics.push({type:ptype,data:b.subarray(o,o+n2)});
    }
    pos+=len;if(last) break;
  }
  return pics;
}
async function extractCover(file){
  try{
    const ext=extOf(file.name);
    const head=await readBytes(file,0,10);
    let pics=[],off=0;
    if(head[0]===0x49&&head[1]===0x44&&head[2]===0x33){                 // ID3v2 в начале (mp3, иногда flac)
      const len=10+sync32(head,6);off=len;
      if(len<=64*1048576) pics=id3Pictures(await readBytes(file,0,len));
    }
    if(!pics.length&&ext==="flac") pics=await flacPictures(file,off);
    if(!pics.length&&ext==="wav"&&latin(head,0,4)==="RIFF"){             // ID3 внутри RIFF-блока
      let pos=12;
      for(let n=0;n<64&&pos+8<=file.size;n++){
        const c=await readBytes(file,pos,8),id=latin(c,0,4).toLowerCase();
        const sz=(c[4]|(c[5]<<8)|(c[6]<<16))+c[7]*16777216;
        if(id==="id3 "&&sz<=64*1048576){pics=id3Pictures(await readBytes(file,pos+8,sz));break}
        pos+=8+sz+(sz&1);
      }
    }
    pics=pics.filter(x=>x.data&&x.data.length>100);
    if(!pics.length) return null;
    const best=pics.find(x=>x.type===3)||pics[0];                        // 3 = лицевая обложка
    return new Blob([best.data],{type:sniffMime(best.data)});
  }catch(e){return null}
}

/* ---------- ссылки на полное прослушивание (в Яндекс Музыке / VK / на площадке) ---------- */
function fullLinks(artist,title,link){
  const q=encodeURIComponent(`${artist&&artist!=="—"?artist+" ":""}${title}`.trim());
  return{y:"https://music.yandex.ru/search?text="+q,v:"https://vk.com/audio?q="+q,p:link||""};
}
function paintFull(){
  const box=$("#fl");if(!box) return;
  box.hidden=!(state.title||"").trim();
}
const EMB=(()=>{
  const box=$("#emb"),fr=$("#eFr"),tabs=$("#eTabs"),inp=$("#eIn"),note=$("#eNote");
  let ctx=null,srcs=[],act=-1,closeT=0;
  const NOTES={"Deezer":"Целиком трек играет, если ты вошёл в Deezer прямо в этом плеере (нужна подписка). Без входа — фрагмент 30 секунд.",
    "Apple Music":"Целиком трек играет после входа в Apple Music в этом плеере (нужна подписка). Без входа — фрагмент 30 секунд.",
    "Яндекс Музыка":"Целиком трек играет после входа в Яндекс в этом плеере (нужна подписка Плюс).",
    "YouTube":"Играет целиком, вход не нужен.","SoundCloud":"Играет целиком, вход не нужен (если автор разрешил)."};
  const dz=id=>({k:"Deezer",u:`https://widget.deezer.com/widget/dark/track/${id}?tracklist=false`,h:200,page:`https://www.deezer.com/track/${id}`});
  const ap=(al,tr)=>({k:"Apple Music",u:`https://embed.music.apple.com/ru/album/-/${al}?i=${tr}`,h:175,page:`https://music.apple.com/ru/album/-/${al}?i=${tr}`});
  const ORIGIN=encodeURIComponent(location.origin);
  const PLAYI='<svg viewBox="0 0 24 24"><path d="M7 4.5v15a1 1 0 0 0 1.52.85l12-7.5a1 1 0 0 0 0-1.7l-12-7.5A1 1 0 0 0 7 4.5z"/></svg>';
  const PAUSEI='<svg viewBox="0 0 24 24"><rect x="6" y="4" width="4.2" height="16" rx="1.2"/><rect x="13.8" y="4" width="4.2" height="16" rx="1.2"/></svg>';
  const mmss=t=>{if(!isFinite(t)||t<0) return "0:00";t=Math.floor(t);return Math.floor(t/60)+":"+String(t%60).padStart(2,"0")};
  const cBox=$("#eCtl"),cPl=$("#ePl"),cSeek=$("#eSeek"),cVol=$("#eVol"),cCur=$("#eCur"),cDur=$("#eDur");
  const C={api:null,cur:0,dur:0,playing:false,vol:80,got:false,drag:false};
  try{const v=localStorage.getItem("embVol");if(v!==null&&v!==""&&!isNaN(+v)) C.vol=Math.max(0,Math.min(100,+v))}catch(e){}
  let ifr=null,poll=0;
  const send=o=>{try{if(ifr&&ifr.contentWindow) ifr.contentWindow.postMessage(typeof o==="string"?o:JSON.stringify(o),"*")}catch(e){}};
  const yt=(func,args)=>send({event:"command",func,args:args||[],id:1,channel:"widget"});
  const scw=(method,value)=>send(value===undefined?{method}:{method,value});
  function paintCtl(){
    const p=C.dur?Math.min(1,C.cur/C.dur):0;
    if(!C.drag){cSeek.value=Math.round(p*1000);cSeek.style.setProperty("--p",(p*100)+"%");cCur.textContent=mmss(C.cur)}
    cDur.textContent=mmss(C.dur);cPl.innerHTML=C.playing?PAUSEI:PLAYI;
  }
  function paintVol(){cVol.value=C.vol;cVol.style.setProperty("--p",C.vol+"%")}
  function applyVol(){if(C.api==="yt") yt("setVolume",[Math.round(C.vol)]);else if(C.api==="sc") scw("setVolume",Math.round(C.vol))}
  function connected(){C.got=true;clearInterval(poll);applyVol();cBox.hidden=false;paintVol();paintCtl()}
  window.addEventListener("message",e=>{
    if(!ifr||e.source!==ifr.contentWindow) return;
    let d=e.data;
    if(typeof d==="string"){try{d=JSON.parse(d)}catch(x){return}}
    if(!d||typeof d!=="object") return;
    if(C.api==="yt"){
      const i=d.info;if(!i||typeof i!=="object") return;
      if(typeof i.currentTime==="number") C.cur=i.currentTime;
      if(typeof i.duration==="number"&&i.duration>0) C.dur=i.duration;
      if(typeof i.playerState==="number") C.playing=i.playerState===1;
      if(!C.got) connected();
      paintCtl();
    }else if(C.api==="sc"){
      if(d.method==="ready"){
        ["playProgress","play","pause","finish"].forEach(n=>scw("addEventListener",n));
        scw("getDuration");connected();return;
      }
      if(d.method==="getDuration"&&typeof d.value==="number") C.dur=d.value/1000;
      else if(d.method==="playProgress"&&d.value){C.cur=(d.value.currentPosition||0)/1000;if(!C.dur&&d.value.relativePosition>0.001) C.dur=C.cur/d.value.relativePosition}
      else if(d.method==="play") C.playing=true;
      else if(d.method==="pause"||d.method==="finish") C.playing=false;
      paintCtl();
    }
  });
  cPl.addEventListener("click",()=>{
    if(C.api==="yt") yt(C.playing?"pauseVideo":"playVideo");
    else if(C.api==="sc") scw("toggle");
  });
  cSeek.addEventListener("input",()=>{C.drag=true;const t=cSeek.value/1000*C.dur;cCur.textContent=mmss(t);cSeek.style.setProperty("--p",(cSeek.value/10)+"%")});
  cSeek.addEventListener("change",()=>{
    const t=cSeek.value/1000*C.dur;C.drag=false;
    if(C.api==="yt") yt("seekTo",[t,true]);else if(C.api==="sc") scw("seekTo",Math.round(t*1000));
    C.cur=t;paintCtl();
  });
  cVol.addEventListener("input",()=>{
    C.vol=+cVol.value;paintVol();applyVol();
    try{localStorage.setItem("embVol",String(C.vol))}catch(e){}
  });
  const ytId=t=>{const m=t.match(/(?:youtu\.be\/|[?&]v=|\/embed\/|\/shorts\/)([\w-]{11})/);return m?m[1]:null};
  function parse(text){
    text=(text||"").trim();let m;
    if((m=text.match(/music\.yandex\.[a-z]+\/album\/(\d+)\/track\/(\d+)/))) return{k:"Яндекс Музыка",u:`https://music.yandex.ru/iframe/track/${m[2]}/${m[1]}`,h:120,page:`https://music.yandex.ru/album/${m[1]}/track/${m[2]}`};
    if((m=text.match(/music\.yandex\.[a-z]+\/album\/(\d+)/))) return{k:"Яндекс Музыка",u:`https://music.yandex.ru/iframe/album/${m[1]}`,h:380,page:`https://music.yandex.ru/album/${m[1]}`};
    if(/(youtube\.com|youtu\.be)/.test(text)&&ytId(text)) return{k:"YouTube",u:`https://www.youtube.com/embed/${ytId(text)}?rel=0&playsinline=1&enablejsapi=1&origin=${ORIGIN}`,r:1,page:"https://www.youtube.com/watch?v="+ytId(text)};
    if(/soundcloud\.com\//.test(text)) return{k:"SoundCloud",u:"https://w.soundcloud.com/player/?url="+encodeURIComponent(text.split(/\s/)[0])+"&auto_play=false",h:166,page:text.split(/\s/)[0]};
    if((m=text.match(/deezer\.com\/(?:[a-z]{2}\/)?track\/(\d+)/))) return dz(m[1]);
    if((m=text.match(/music\.apple\.com\/[a-z]{2}\/album\/[^/]+\/(\d+)\?(?:[^#]*&)?i=(\d+)/))) return ap(m[1],m[2]);
    return null;
  }
  function paintTabs(){
    tabs.innerHTML=srcs.map((x,i)=>`<button class="rkc${i===act?" on":""}" data-i="${i}" type="button">${esc(x.label||x.k)}</button>`).join("");
  }
  function showSrc(i){
    act=i;const x=srcs[i];paintTabs();
    if(!x){clearInterval(poll);cBox.hidden=true;C.api=null;ifr=null;$("#eFail").hidden=true;fr.className="efr";fr.innerHTML='<div class="none">Вставь ссылку на трек ниже — он откроется здесь целиком, без перехода на сайт.</div>';note.textContent="";return}
    fr.className="efr"+(x.r?" r":"");
    let u=x.u;
    if(x.k==="YouTube"&&!/enablejsapi=1/.test(u)) u+=(u.includes("?")?"&":"?")+"enablejsapi=1&origin="+ORIGIN;
    fr.innerHTML=`<iframe src="${esc(u)}" ${x.r?"":`height="${x.h||200}"`} allow="autoplay *; encrypted-media *; clipboard-write; fullscreen; picture-in-picture" allowfullscreen referrerpolicy="strict-origin-when-cross-origin"></iframe>`;
    note.textContent=NOTES[x.k]||"";
    $("#eFail").hidden=!x.page;if(x.page) $("#eOpen").href=x.page;
    clearInterval(poll);cBox.hidden=true;
    Object.assign(C,{api:x.k==="YouTube"?"yt":x.k==="SoundCloud"?"sc":null,cur:0,dur:0,playing:false,got:false,drag:false});
    ifr=fr.querySelector("iframe");paintVol();paintCtl();
    if(C.api==="yt"&&ifr){
      const hello=()=>send({event:"listening",id:1,channel:"widget"});
      ifr.addEventListener("load",()=>{let n=0;hello();clearInterval(poll);poll=setInterval(()=>{if(C.got||++n>30){clearInterval(poll);return}hello()},400)});
    }
    if(C.api) note.textContent+=" Перемотка и громкость — на панели под плеером.";
    else if(x.k) note.textContent+=" Перемотка и громкость — в самом плеере: эта площадка не даёт управлять им снаружи.";
  }
  function open(c){
    clearTimeout(closeT);ctx=c;srcs=[];
    if(c.dz) srcs.push({...dz(c.dz),label:"Deezer"});
    if(c.itT&&c.itA) srcs.push({...ap(c.itA,c.itT),label:"Apple Music"});
    if(c.saved&&c.saved.u) srcs.push({...c.saved,label:"Моя ссылка · "+c.saved.k});
    $("#eT").textContent=c.title||"Трек";$("#eA").textContent=c.artist&&c.artist!=="—"?c.artist:"";
    const L=fullLinks(c.artist,c.title,"");
    $("#eY").href=L.y;$("#eV").href=L.v;
    $("#eYT").href="https://www.youtube.com/results?search_query="+encodeURIComponent(`${c.artist&&c.artist!=="—"?c.artist+" ":""}${c.title}`);
    inp.value="";
    try{AU.pause()}catch(e){}try{CT.stopPv()}catch(e){}
    box.classList.remove("out");box.hidden=false;
    showSrc(srcs.length?srcs.length-1:-1);
    if(!srcs.length) setTimeout(()=>inp.focus({preventScroll:true}),300);
  }
  function close(){
    if(box.hidden||box.classList.contains("out")) return;
    box.classList.add("out");
    clearInterval(poll);ifr=null;C.api=null;cBox.hidden=true;
    closeT=setTimeout(()=>{box.hidden=true;box.classList.remove("out");fr.innerHTML="";ctx=null},200);   // iframe убирается — звук останавливается
  }
  function addLink(){
    const x=parse(inp.value);
    if(!x){toast("Не получилось распознать ссылку",true);return}
    const label="Ссылка · "+x.k,i=srcs.findIndex(s=>s.custom);
    const item={...x,label,custom:true};
    if(i>=0) srcs[i]=item;else srcs.push(item);
    showSrc(i>=0?i:srcs.length-1);inp.value="";
    if(ctx&&ctx.save){ctx.save({k:x.k,u:x.u,h:x.h||0,r:x.r||0});toast(ctx.hint||"Ссылка открыта")}
  }
  tabs.addEventListener("click",e=>{const b=e.target.closest("[data-i]");if(b) showSrc(+b.dataset.i)});
  $("#eGo").addEventListener("click",addLink);
  inp.addEventListener("keydown",e=>{if(e.key==="Enter"){e.preventDefault();addLink()}});
  $("#eX").addEventListener("click",close);
  box.addEventListener("pointerdown",e=>{if(e.target===box) close()});
  document.addEventListener("keydown",e=>{if(e.key==="Escape"&&!box.hidden&&!box.classList.contains("out")){e.preventDefault();e.stopImmediatePropagation();close()}});
  return{open,close,isOpen:()=>!box.hidden&&!box.classList.contains("out")};
})();
function embCtxEditor(){
  const c=(state.cat||"").split(":");
  return{artist:state.artist,title:state.title,dz:c[0]==="dz"?c[1]:0,itT:c[0]==="it"?c[1]:0,itA:state.catAlb||0,saved:state.embed,
    save:e=>{state.embed=e},hint:"Ссылка открыта. Нажми ✓, чтобы она сохранилась в оценке"};
}

/* ---------- выбор площадки для полного прослушивания (открывается во внешнем браузере) ---------- */
const PLAT=(()=>{
  const box=$("#plat"),grid=$("#plG");
  let ctx=null,closeT=0;
  const SEARCH=[
    ["Яндекс Музыка","#ffcc00","https://music.yandex.ru/search?text="],
    ["VK Музыка","#2787f5","https://vk.com/audio?q="],
    ["YouTube Music","#ff2a44","https://music.youtube.com/search?q="],
    ["YouTube","#ff0000","https://www.youtube.com/results?search_query="],
    ["Spotify","#1ed760","https://open.spotify.com/search/"],
    ["Apple Music","#fa5c70","https://music.apple.com/ru/search?term="],
    ["Deezer","#a238ff","https://www.deezer.com/search/"],
    ["SoundCloud","#ff5500","https://soundcloud.com/search?q="],
  ];
  const DIRECT={dz:["Deezer","#a238ff"],it:["Apple Music","#fa5c70"]};
  async function openExt(url){
    try{const r=await fetch("/api/open",{method:"POST",body:url});if(!r.ok) throw 0}
    catch(e){window.open(url,"_blank","noopener")}
  }
  function open(c){
    clearTimeout(closeT);ctx=c;
    const q=encodeURIComponent(`${c.artist&&c.artist!=="—"?c.artist+" ":""}${c.title||""}`.trim());
    $("#plT").textContent=[c.artist&&c.artist!=="—"?c.artist:"",c.title].filter(Boolean).join(" — ");
    $("#plH").textContent=c.kind==="artist"?"Где послушать исполнителя":c.kind==="album"?"Где послушать альбом":"Слушать целиком";
    $("#plE").hidden=!c.emb;
    const what=c.kind==="artist"?"страница исполнителя":c.kind==="album"?"страница альбома":"страница этого трека";
    const items=[];
    (c.direct||[]).forEach(x=>items.push({...x,main:1}));
    if(c.link&&DIRECT[c.src]) items.push({n:DIRECT[c.src][0]+" · "+what,c:DIRECT[c.src][1],u:c.link,main:1});
    SEARCH.forEach(([n,col,base])=>{
      if(c.link&&DIRECT[c.src]&&DIRECT[c.src][0]===n) return;
      if((c.direct||[]).some(x=>x.n.startsWith(n))) return;
      items.push({n,c:col,u:base+(n==="Spotify"?q.replace(/%20/g,"%20"):q)});
    });
    grid.innerHTML=items.map((x,i)=>`<button class="plb${x.main?" main":""}" style="--c:${x.c};--i:${i}" data-u="${esc(x.u)}" type="button"><i></i>${esc(x.n)}</button>`).join("");
    try{AU.pause()}catch(e){}try{CT.stopPv()}catch(e){}
    box.classList.remove("out");box.hidden=false;
  }
  function close(){
    if(box.hidden||box.classList.contains("out")) return;
    box.classList.add("out");closeT=setTimeout(()=>{box.hidden=true;box.classList.remove("out")},200);
  }
  grid.addEventListener("click",e=>{
    const b=e.target.closest("[data-u]");if(!b) return;
    openExt(b.dataset.u);toast(ctx&&ctx.kind?"Открыл в браузере":"Открыл в браузере. Послушай и возвращайся оценивать");close();
  });
  $("#plE").addEventListener("click",()=>{const c=ctx;close();if(c&&c.emb) setTimeout(()=>EMB.open(c.emb),180)});
  box.addEventListener("pointerdown",e=>{if(e.target===box) close()});
  document.addEventListener("keydown",e=>{
    if(e.key==="Escape"&&!box.hidden&&!box.classList.contains("out")){e.preventDefault();e.stopImmediatePropagation();close()}
  });
  return{open,close};
})();
function platCtxEditor(){
  const c=(state.cat||"").split(":"),sl=state.stream&&state.stream.link;
  return{artist:state.artist,title:state.title,link:sl||"",src:c[0]||"",emb:embCtxEditor()};
}
$("#flOpen").addEventListener("click",()=>PLAT.open(platCtxEditor()));

/* ---------- аудиофайлы: загрузка и плеер внутри приложения ---------- */
const AUDIO_EXT=["mp3","wav","flac"],MAX_MB=500;
const extOf=n=>(String(n).split(".").pop()||"").toLowerCase();
async function uploadAudio(id,ext,file){
  toast("Загружаю файл…");
  try{
    const r=await fetch(`/api/audio/${id}?ext=${ext}`,{method:"POST",body:file});
    if(!r.ok) throw new Error(r.status);
    toast("Файл загружен");
  }catch(e){
    const t=state.tracks.find(x=>x.id===id);
    if(t){delete t.audio;persist();renderLib()}
    toast("Не удалось загрузить файл",true);
  }
}
function delAudio(id){fetch(`/api/audio/${id}`,{method:"DELETE"}).catch(()=>{})}

/* несколько файлов сразу = альбом: каждый файл становится неоценённым треком, оцениваются по очереди */
async function addAlbum(files){
  files=files.slice().sort((a,b)=>a.name.localeCompare(b.name,undefined,{numeric:true}));
  const big=files.filter(f=>f.size>MAX_MB*1048576);
  files=files.filter(f=>f.size<=MAX_MB*1048576);
  if(big.length) toast(`Пропущено файлов больше ${MAX_MB} МБ: ${big.length}`,true);
  if(!files.length) return;
  const isEp=files.length<=6;                         // 2–6 треков — EP, 7 и больше — альбом
  const albName=state.album.trim()||(isEp?"Новый EP":"Новый альбом");
  const same=state.tracks.find(t=>t.albumId&&low(t.album)===low(albName));
  const albumId=same?same.albumId:Date.now();
  const genre=state.genre.trim(),artistIn=state.artist.trim(),base=Date.now()+1000;
  const pairs=files.map((f,i)=>{
    let name=f.name.replace(/\.[^.]+$/,"").replace(/_+/g," ").trim();
    name=name.replace(/^0?\d{1,2}\s*[.\-–—)]\s*/,"").replace(/^0\d\s+/,"").trim()||name;
    const m=name.match(/^(.+?)\s+[-–—]\s+(.+)$/);
    const rec={id:base+i,artist:artistIn||(m?m[1].trim():"—"),title:(m?m[2]:name).trim()||name,genre,scores:[5,5,5,5,5],total:5,
      album:albName,albumId,unrated:true,audio:{name:f.name,ext:extOf(f.name),size:f.size}};
    return[rec,f];
  });
  pairs.forEach(([r])=>state.tracks.push(r));
  state.sort="album";$("#sort").textContent="По альбомам";
  persist();renderStats();renderLib();
  toast(`Загружаю ${isEp?"EP":"альбом"} «${albName}»: ${pairs.length} ${pairs.length%10===1&&pairs.length!==11?"трек":pairs.length%10>1&&pairs.length%10<5&&(pairs.length<10||pairs.length>20)?"трека":"треков"}…`);
  let fail=0,opened=false;
  for(const [rec,f] of pairs){
    try{
      const r=await fetch(`/api/audio/${rec.id}?ext=${rec.audio.ext}`,{method:"POST",body:f});
      if(!r.ok) throw new Error(r.status);
    }catch(e){fail++;delete rec.audio}
    if(!opened){opened=true;loadTrack(pairs[0][0].id)}                  // первый трек сразу открывается для оценки
    try{                                                                // вшитая обложка, если есть
      const b=await extractCover(f);
      if(b){
        const jb=await toJpeg(b);
        const r=await fetch(`/api/img/cover/${rec.id}`,{method:"POST",body:jb});
        if(r.ok){rec.cover=Date.now();persist();renderLib()}
      }
    }catch(e){}
  }
  persist();renderLib();
  toast(fail?`Альбом добавлен, но ${fail} файл(ов) не загрузились`:`${isEp?"EP":"Альбом"} «${albName}» загружен. Оцени каждый трек — общая оценка появится в конце`,!!fail);
}

const AU=(()=>{
  const au=new Audio();au.preload="metadata";
  const drop=$("#drop"),ap=$("#ap"),btn=$("#apPlay"),nm=$("#apName"),meta=$("#apMeta"),seek=$("#apSeek"),tcur=$("#apCur"),tdur=$("#apDur"),vol=$("#apVol");
  const PLAY='<svg viewBox="0 0 24 24"><path d="M7 4.5v15a1 1 0 0 0 1.52.85l12-7.5a1 1 0 0 0 0-1.7l-12-7.5A1 1 0 0 0 7 4.5z"/></svg>';
  const PAUSE='<svg viewBox="0 0 24 24"><rect x="6" y="4" width="4.2" height="16" rx="1.2"/><rect x="13.8" y="4" width="4.2" height="16" rx="1.2"/></svg>';
  const mmss=s=>{if(!isFinite(s)||s<0) return "0:00";s=Math.floor(s);return Math.floor(s/60)+":"+String(s%60).padStart(2,"0")};
  const mb=n=>(n/1048576).toFixed(n>=10485760?0:1)+" МБ";
  let objUrl="";
  function paint(){
    const p=au.duration?au.currentTime/au.duration:0;
    if(document.activeElement!==seek||!seek.matches(":active")) seek.value=Math.round(p*1000);
    seek.style.setProperty("--p",(p*100)+"%");
    tcur.textContent=mmss(au.currentTime);
  }
  /* трек продолжает играть, пока ты открываешь и правишь другие оценки; ключ — какой звук сейчас загружен в плеер */
  let loadedKey="",det=false,playInfo=null,playMeta=null;
  const metaOf=i=>(i.ext||"").toUpperCase()+(i.size?" · "+mb(i.size):"");
  function edInfo(){
    if(state.file) return {name:state.file.name,size:state.file.size,ext:extOf(state.file.name)};
    if(state.audio&&state.audioId!=null) return state.audio;
    if(state.stream&&state.stream.url) return {name:state.stream.name||"Превью из каталога",ext:state.stream.src==="sc"?"SoundCloud":"превью 30 с",size:0};
    return null;
  }
  function paintHead(on){
    if(on){                                           // сверху — открытый сейчас трек, звук идёт от другого
      const i=edInfo(),n=i?i.name:(state.title.trim()||"Новая оценка");
      nm.textContent=n;nm.title=n;
      meta.textContent=(i?metaOf(i):"");
      if(playInfo){const b=document.createElement("i");b.textContent=(meta.textContent?" · ":"")+"в фоне играет: "+playInfo.name;meta.appendChild(b)}
    }else if(playInfo){nm.textContent=playInfo.name;nm.title=playInfo.name;meta.textContent=metaOf(playInfo)}
  }
  const edKey=()=>state.file?"f:"+state.file.name+":"+state.file.size:(state.audio&&state.audioId!=null)?"a:"+state.audioId:(state.stream&&state.stream.url)?"s:"+state.stream.url:"";
  function paintBtn(){
    const sw=det&&!!edKey();                          // открыт другой трек — кнопка сразу включает его
    btn.innerHTML=(!sw&&!au.paused&&!au.ended)?PAUSE:PLAY;
    btn.title=sw?"Включить открытый трек":"Играть / пауза";btn.classList.toggle("swp",sw);
  }
  function setDet(on,ek){
    det=on;ap.classList.toggle("det",on);
    $("#apX").hidden=on;$("#apSwap").hidden=!(on&&ek);
    paintHead(on);
    if(on) drop.hidden=!!ek;                       // у открытого трека нет аудио — можно загрузить файл, музыка не прервётся
    else drop.hidden=!ap.hidden;
    paintBtn();
  }
  function sync(force){
    const ek=edKey(),playing=!au.paused&&!au.ended&&!!au.getAttribute("src");
    if(!force&&loadedKey&&loadedKey===ek&&au.getAttribute("src")){setDet(false,ek);mpVis();return}   // в плеере уже этот трек (играет или на паузе) — не трогаем
    if(!force&&playing){
      setDet(true,ek);return;                                     // играет другой — пусть играет дальше
    }
    setDet(false,ek);loadedKey=ek;
    au.pause();ap.classList.remove("live");
    if(objUrl){URL.revokeObjectURL(objUrl);objUrl=""}
    let src="",info=null;
    if(state.file){objUrl=URL.createObjectURL(state.file);src=objUrl;info={name:state.file.name,size:state.file.size,ext:extOf(state.file.name)}}
    else if(state.audio&&state.audioId!=null){src=`/api/audio/${state.audioId}?t=${Date.now()}`;info=state.audio}
    else if(state.stream&&state.stream.url){src="/api/cat/stream?u="+encodeURIComponent(state.stream.url);info={name:state.stream.name||"Превью из каталога",ext:state.stream.src==="sc"?"SoundCloud":"превью 30 с",size:0}}
    seek.value=0;seek.style.setProperty("--p","0%");tcur.textContent="0:00";tdur.textContent="0:00";btn.innerHTML=PLAY;
    if(!src){au.removeAttribute("src");au.load();playInfo=null;playMeta=null;ap.hidden=true;drop.hidden=false;mpVis();markPlaying();return}
    au.src=src;playInfo=info;playMeta=metaFromEditor(info);mpPaintMeta();mpVis();
    nm.textContent=info.name;nm.title=info.name;
    meta.textContent=metaOf(info);
    drop.hidden=true;ap.hidden=false;
  }
  /* ---------- эквалайзер + осциллограф ---------- */
  const cvz=$("#apViz"),zx=cvz.getContext("2d"),NB=64;
  const lv=new Float32Array(NB),s1=new Float32Array(NB),s2=new Float32Array(NB);
  let actx=null,an=null,fD=null,tD=null,edges=null,zraf=0,zprev=0,zW=0;const zH=64;
  function graph(){
    if(an||actx===false) return;
    const AC=window.AudioContext||window.webkitAudioContext;
    if(!AC){actx=false;return}
    try{
      actx=new AC();const src=actx.createMediaElementSource(au);
      an=actx.createAnalyser();an.fftSize=2048;an.smoothingTimeConstant=.6;
      src.connect(an);an.connect(actx.destination);
      fD=new Uint8Array(an.frequencyBinCount);tD=new Uint8Array(an.fftSize);
      const bh=actx.sampleRate/an.fftSize;edges=[];
      for(let i=0;i<=NB;i++){const f=40*Math.pow(16000/40,i/NB);edges.push(Math.min(an.frequencyBinCount-1,Math.round(f/bh)))}
      for(let i=1;i<=NB;i++) if(edges[i]<=edges[i-1]) edges[i]=Math.min(an.frequencyBinCount-1,edges[i-1]+1);
    }catch(e){actx=false;an=null}
  }
  function zfit(){
    const w=Math.round(cvz.parentNode.getBoundingClientRect().width),d=Math.min(devicePixelRatio||1,2);
    if(w&&w!==zW){zW=w;cvz.width=w*d;cvz.height=zH*d;zx.setTransform(d,0,0,d,0,0)}
  }
  const smooth=(src,out)=>{for(let i=0;i<NB;i++) out[i]=(src[i>0?i-1:0]+src[i]*2+src[i<NB-1?i+1:NB-1])/4};
  function wavePath(arr,k,close){
    const bw=zW/NB,y=i=>zH-3-Math.min(1,arr[i]*k)*(zH-10);
    zx.beginPath();
    if(close){zx.moveTo(0,zH);zx.lineTo(0,y(0))}else zx.moveTo(0,y(0));
    for(let i=0;i<NB-1;i++){const x0=(i+.5)*bw,x1=(i+1.5)*bw;zx.quadraticCurveTo(x0,y(i),(x0+x1)/2,(y(i)+y(i+1))/2)}
    zx.lineTo(zW,y(NB-1));
    if(close){zx.lineTo(zW,zH);zx.closePath()}
  }
  function zframe(ms){
    const dt=Math.min(.05,(ms-zprev)/1000||.016);zprev=ms;zfit();
    const on=!!an&&!au.paused&&!au.ended;
    if(on) an.getByteFrequencyData(fD);
    const hue=350+((typeof FX!=="undefined"&&FX.bo)||0);let live=on;
    for(let i=0;i<NB;i++){
      let v=0;
      if(on){let a=0,c=0;for(let j=edges[i];j<=edges[i+1];j++){a+=fD[j];c++}v=a/Math.max(1,c)/255;v=Math.pow(v,1.45)*(.85+.55*i/NB);if(v>1) v=1}
      lv[i]=v>lv[i]?lv[i]+(v-lv[i])*Math.min(1,dt*22):lv[i]*Math.pow(.05,dt);
      if(lv[i]>.004) live=true;
    }
    smooth(lv,s1);smooth(s1,s2);smooth(s2,s2);
    zx.clearRect(0,0,zW,zH);zx.globalCompositeOperation="lighter";
    const layers=[[s2,1.0,28,.30],[s1,.95,10,.42],[lv,.9,0,.55]];
    for(const [arr,k,off,al] of layers){
      const g=zx.createLinearGradient(0,0,0,zH);
      g.addColorStop(0,`hsla(${hue+off} 100% 66% / ${al})`);g.addColorStop(1,`hsla(${hue+off-14} 100% 40% / 0)`);
      zx.fillStyle=g;wavePath(arr,k,true);zx.fill();
    }
    zx.globalCompositeOperation="source-over";
    zx.lineJoin="round";wavePath(s1,.95,false);
    zx.lineWidth=7;zx.strokeStyle=`hsla(${hue} 100% 60% / .22)`;zx.stroke();
    zx.lineWidth=1.8;zx.strokeStyle=`hsl(${hue} 100% 84%)`;zx.stroke();
    if(live) zraf=requestAnimationFrame(zframe);else{zraf=0;zx.clearRect(0,0,zW,zH)}
  }
  function zkick(){if(!zraf){zprev=performance.now();zraf=requestAnimationFrame(zframe)}}
  const play=()=>{
    if(!au.getAttribute("src")) return;
    graph();if(actx&&actx.state==="suspended") actx.resume().catch(()=>{});
    au.play().catch(()=>{});
  };
  btn.addEventListener("click",()=>{if(det&&edKey()){sync(true);play();return}au.paused?play():au.pause()});
  au.addEventListener("play",()=>{if(mpClosed){mpClosed=false;mpVis();try{markPlaying()}catch(e){}}paintBtn();mpPaintPlay();ap.classList.add("live");zkick()});
  au.addEventListener("pause",()=>{btn.innerHTML=PLAY;mpPaintPlay();ap.classList.remove("live");zkick();if(det) setTimeout(()=>{if(det&&au.paused) sync()},0)});
  au.addEventListener("ended",()=>{btn.innerHTML=PLAY;mpPaintPlay();ap.classList.remove("live");au.currentTime=0;paint();zkick();if(det) setTimeout(()=>{if(det&&au.paused) sync()},0)});
  au.addEventListener("timeupdate",()=>{paint();mpPaintTime()});
  au.addEventListener("loadedmetadata",()=>{tdur.textContent=mmss(au.duration);paint();mpPaintTime()});
  au.addEventListener("error",()=>{const s=au.getAttribute("src")||"";if(s) toast(s.includes("pulse-stream")?"SoundCloud не отдал этот трек — попробуй ещё раз через минуту":"Не удалось воспроизвести этот файл",true)});
  au.addEventListener("loadstart",()=>{if((au.getAttribute("src")||"").includes("pulse-stream")) toast("Загружаю трек с SoundCloud…")});
  seek.addEventListener("input",()=>{
    if(au.duration){au.currentTime=seek.value/1000*au.duration}
    seek.style.setProperty("--p",(seek.value/10)+"%");
  });

  /* ---------- мини-плеер: виден, когда открыта другая страница или плеер ушёл за экран ---------- */
  const mp=$("#mp"),mpPlay=$("#mpPlay"),mpSeek=$("#mpSeek"),mpVol=$("#mpVol"),mpCur=$("#mpCur"),mpDur=$("#mpDur");
  let apSeen=true,mpDrag=false,mpClosed=false;
  const coverOf=t=>t&&t.cover?`/api/img/cover/${t.id}?v=${t.cover}`:(t&&albCovUrl(t.albumId))||"";
  function metaFromEditor(info){
    const id=state.editing;
    return {id,title:state.title.trim()||(info&&info.name)||"Трек",artist:state.artist.trim(),beat:(state.beat||"").trim(),
      cover:state.coverBlobUrl||(state.cover&&id!=null?`/api/img/cover/${id}?v=${state.cover}`:"")||state.coverPrev||"",bg:coverBg(state.artist,state.title)};
  }
  const metaFromTrack=t=>({id:t.id,title:t.title,artist:t.artist==="—"?"":t.artist,beat:t.beat||"",cover:coverOf(t),bg:coverBg(t.artist==="—"?"":t.artist,t.title)});
  const playable=t=>!!(t.audio||(t.stream&&t.stream.url));
  function mpList(){
    if(!playMeta||playMeta.id==null) return [];
    const cur=state.tracks.find(x=>x.id===playMeta.id);if(!cur) return [];
    return (cur.albumId?state.tracks.filter(t=>t.albumId===cur.albumId).sort(byOrd):state.tracks.filter(t=>!t.albumId).sort((a,b)=>b.id-a.id)).filter(playable);
  }
  function mpPaintMeta(){
    if(!playMeta) return;
    const t=playMeta.id!=null&&state.tracks.find(x=>x.id===playMeta.id);
    if(t&&!(state.editing===t.id&&loadedKey===edKey())) Object.assign(playMeta,metaFromTrack(t));   // название/битмейкер могли поменяться
    $("#mpT").textContent=playMeta.title;$("#mpT").title=playMeta.title;
    $("#mpA").innerHTML=esc(playMeta.artist||"")+(playMeta.beat?`${playMeta.artist?" · ":""}<span class="pb">prod. ${esc(playMeta.beat)}</span>`:"");
    $("#mpCv").style.background=playMeta.cover?`url("${playMeta.cover}") center/cover no-repeat #222`:playMeta.bg;
    const l=mpList(),i=l.findIndex(x=>x.id===playMeta.id);
    $("#mpPrev").disabled=!(i>0);$("#mpNext").disabled=!(i>=0&&i<l.length-1);
    $("#mpGo").title=playMeta.id!=null?`Открыть «${playMeta.title}»`:"Вернуться к треку";
    markPlaying();
  }
  function mpVis(){
    const has=!!au.getAttribute("src")&&!!playMeta;
    const busy=!!(document.getElementById("intro")||document.getElementById("welcome"));
    const on=has&&!busy&&!mpClosed&&(document.body.classList.contains("pg")||!apSeen||ap.hidden);
    if(on){mp.classList.remove("out");if(mp.hidden){mp.hidden=false;mpPaintMeta();mpPaintTime()}}
    else if(!mp.hidden&&!mp.classList.contains("out")){
      mp.classList.add("out");setTimeout(()=>{if(mp.classList.contains("out")){mp.hidden=true;mp.classList.remove("out")}},240);
    }
    document.body.classList.toggle("mpon",on);
  }
  function mpPaintTime(){
    if(mp.hidden) return;
    const d=au.duration,p=d?au.currentTime/d:0;
    if(!mpDrag){mpSeek.value=Math.round(p*1000);mpSeek.style.setProperty("--p",(p*100)+"%");mpCur.textContent=mmss(au.currentTime)}
    mpDur.textContent=mmss(d);
  }
  function mpPaintPlay(){
    const on=!au.paused&&!au.ended;
    mpPlay.innerHTML=on?PAUSE:PLAY;mp.classList.toggle("playing",on);
    document.querySelectorAll(".cv>.eqw.pl,.lbcv>.eqw.pl").forEach(n=>n.classList.toggle("ps",!on));
  }
  /* включить трек из медиатеки, не открывая его в редакторе (кнопки ⏮ ⏭ мини-плеера) */
  function playTrack(t){
    if(!t||!playable(t)) return;
    if(state.editing===t.id){sync(true);play();return}
    let src,info,key;
    if(t.audio){src=`/api/audio/${t.id}?t=${Date.now()}`;info={...t.audio,name:t.audio.name||t.title};key="a:"+t.id}
    else{src="/api/cat/stream?u="+encodeURIComponent(t.stream.url);info={name:t.stream.name||t.title,ext:t.stream.src==="sc"?"SoundCloud":"превью 30 с",size:0};key="s:"+t.stream.url}
    au.pause();
    if(objUrl&&!state.file){URL.revokeObjectURL(objUrl);objUrl=""}
    loadedKey=key;au.src=src;playInfo=info;playMeta=metaFromTrack(t);
    seek.value=0;seek.style.setProperty("--p","0%");tcur.textContent="0:00";tdur.textContent="0:00";
    ap.hidden=false;setDet(edKey()!==key,edKey());
    mpPaintMeta();mpVis();play();
  }
  mpPlay.innerHTML=PLAY;
  $("#mpX").addEventListener("click",()=>{             // закрыть плеер: трек останавливается
    mpClosed=true;au.pause();
    if(det) sync(true);                                 // в фоне играл другой трек — в плеер возвращается открытый
    else{try{au.currentTime=0}catch(e){}paint();mpPaintTime()}
    mpVis();markPlaying();
  });
  mpPlay.addEventListener("click",()=>{au.paused?play():au.pause()});
  $("#mpPrev").addEventListener("click",()=>{const l=mpList(),i=l.findIndex(x=>x.id===playMeta.id);if(i>0) playTrack(l[i-1])});
  $("#mpNext").addEventListener("click",()=>{const l=mpList(),i=l.findIndex(x=>x.id===playMeta.id);if(i>=0&&i<l.length-1) playTrack(l[i+1])});
  mpSeek.addEventListener("input",()=>{mpDrag=true;mpSeek.style.setProperty("--p",(mpSeek.value/10)+"%");if(au.duration) mpCur.textContent=mmss(mpSeek.value/1000*au.duration)});
  mpSeek.addEventListener("change",()=>{if(au.duration) au.currentTime=mpSeek.value/1000*au.duration;mpDrag=false;paint();mpPaintTime()});
  $("#mpMute").addEventListener("click",()=>{au.muted=!au.muted;mp.classList.toggle("mute",au.muted);$("#mpMute").title=au.muted?"Включить звук":"Без звука"});
  $("#mpGo").addEventListener("click",()=>{
    const id=playMeta&&playMeta.id,t=id!=null&&state.tracks.find(x=>x.id===id);
    if(t&&state.editing!==id){loadTrack(id);return}
    try{if(PAGE_CLOSE) PAGE_CLOSE()}catch(e){}try{SET.close()}catch(e){}
    main.scrollTo({top:0,behavior:"smooth"});
  });
  new IntersectionObserver(es=>{apSeen=es[es.length-1].isIntersecting;mpVis()},{threshold:.2}).observe(ap);
  new MutationObserver(mpVis).observe(document.body,{attributes:true,attributeFilter:["class"]});
  new MutationObserver(mpVis).observe(document.body,{childList:true});   // интро / приветствие ушли

  /* значок «играет» — у того трека, который звучит сейчас, на всех страницах (а не у открытого в редакторе) */
  let markObs=null;
  function markPlaying(){                     // ничего не пересоздаёт без нужды — значок не перезапускается и не мигает
    const id=!mpClosed&&au.getAttribute("src")&&playMeta&&playMeta.id!=null?String(playMeta.id):null,on=!au.paused&&!au.ended;   // плеер закрыт — значка нет
    document.querySelectorAll(".cv>.eqw.pl,.lbcv>.eqw.pl").forEach(w=>{
      const it=w.closest(".item,.trk");
      if(!it||it.dataset.id!==id) w.remove();else w.classList.toggle("ps",!on);
    });
    document.querySelectorAll(".item.pl,.trk.pl").forEach(it=>{if(it.dataset.id!==id) it.classList.remove("pl")});
    if(id!=null) document.querySelectorAll(`.item[data-id="${id}"],.trk[data-id="${id}"]`).forEach(it=>{
      it.classList.add("pl");
      const cv=it.querySelector(".cv,.lbcv");if(!cv||cv.querySelector(":scope>.eqw.pl")) return;
      const w=document.createElement("div");w.className="eqw pl"+(on?"":" ps");
      w.innerHTML='<span class="eq"><i></i><i></i><i></i><i></i></span>';cv.append(w);
    });
    if(markObs) markObs.takeRecords();
  }
  let markRaf=0;
  markObs=new MutationObserver(()=>{if(!markRaf) markRaf=requestAnimationFrame(()=>{markRaf=0;markPlaying()})});
  ["#list","#pBody","#lbBody"].forEach(s=>{const n=$(s);if(n) markObs.observe(n,{childList:true,subtree:true})});   // только списки треков, а не весь экран
  function setVol(v){
    v=Math.max(0,Math.min(100,+v||0));au.volume=v/100;
    for(const r of [vol,mpVol]){r.value=v;r.style.setProperty("--p",v+"%")}
    if(au.muted&&v>0){au.muted=false;mp.classList.remove("mute")}
  }
  const v0=Math.max(0,Math.min(100,Number.isFinite(+ST.vol)?+ST.vol:100));   // громкость помнится между запусками
  vol.value=v0;au.volume=v0/100;vol.style.setProperty("--p",v0+"%");
  setVol(v0);
  vol.addEventListener("input",()=>{setVol(vol.value);setST({vol:+vol.value})});
  mpVol.addEventListener("input",()=>{setVol(mpVol.value);setST({vol:+mpVol.value})});
  $("#apSwap").addEventListener("click",()=>{sync(true);play()});
  $("#apX").addEventListener("click",()=>{state.file=null;state.audio=null;state.stream=null;sync(true);toast("Файл убран. Нажми «Оценить», чтобы применить")});

  // выбор файла кнопкой и перетаскиванием
  const fileEl=$("#file");
  function setFile(f){
    if(!f) return;
    if(isImageFile(f)){
      if(!$("#pm").hidden&&AP.artistKey()) AP.setImage(f);else setCoverFile(f);
      return;
    }
    const ext=extOf(f.name);
    if(!AUDIO_EXT.includes(ext)){toast("Подходят MP3, WAV, FLAC и картинки",true);return}
    if(f.size>MAX_MB*1048576){toast(`Файл больше ${MAX_MB} МБ`,true);return}
    state.file=f;
    const base=f.name.replace(/\.[^.]+$/,"").replace(/[_]+/g," ").trim();
    if(!state.title.trim()&&base){
      const m=base.match(/^(.+?)\s+[-–—]\s+(.+)$/);
      if(m&&!state.artist.trim()){state.artist=m[1].trim();artistEl.value=state.artist;state.title=m[2].trim()}
      else state.title=base;
      titleEl.value=state.title;paintCover();
    }
    sync(true);play();
    toast("Файл добавлен. Нажми «Оценить», чтобы сохранить");
    if(!state.coverBlob&&!state.cover){                                  // обложка, вшитая в трек, подставляется сама
      extractCover(f).then(b=>{
        if(b&&state.file===f&&!state.coverBlob&&!state.cover)
          setCoverFile(b,"Файл добавлен, обложка взята из него. Нажми «Оценить», чтобы сохранить");
      });
    }
  }
  function setFiles(list){
    const files=[...list],audio=files.filter(f=>AUDIO_EXT.includes(extOf(f.name)));
    if(audio.length>1) addAlbum(audio);else setFile(files[0]);
  }
  $("#pick").addEventListener("click",()=>fileEl.click());
  fileEl.addEventListener("change",()=>{setFiles(fileEl.files);fileEl.value=""});
  let dragN=0;
  const hasFiles=e=>e.dataTransfer&&[...e.dataTransfer.types||[]].includes("Files");
  addEventListener("dragenter",e=>{if(hasFiles(e)){dragN++;document.body.classList.add("dragging")}});
  addEventListener("dragleave",e=>{if(hasFiles(e)){dragN=Math.max(0,dragN-1);if(!dragN) document.body.classList.remove("dragging")}});
  addEventListener("dragover",e=>{if(hasFiles(e)) e.preventDefault()});
  addEventListener("drop",e=>{
    if(!hasFiles(e)) return;
    e.preventDefault();dragN=0;document.body.classList.remove("dragging");
    setFiles(e.dataTransfer.files);
  });
  return{sync,play:force=>{if(force&&det) sync(true);play()},pause:()=>au.pause(),
    key:()=>edKey(),adopt:k0=>{if(loadedKey&&loadedKey===k0){loadedKey=edKey();if(playInfo){playMeta=metaFromEditor(playInfo);mpPaintMeta()}}setDet(det,edKey())},
    refresh:()=>{if(!mp.hidden) mpPaintMeta()}};
})();

/* ---------- профили артистов ---------- */
var PAGE_CLOSE=null;
const AP=(()=>{
  const pm=$("#pm"),body=$("#pBody"),back=$("#pBack"),pq=$("#pq"),pqw=$("#pqw"),navBtn=$("#navArtists");
  let view="list",curKey=null,closeT=0,tok=0,sortMode="rating";
  const SORTS={
    rating:(a,b)=>(!a.key)-(!b.key)||b.avg-a.avg||b.best-a.best||a.name.localeCompare(b.name,"ru"),
    alpha:(a,b)=>(!a.key)-(!b.key)||a.name.localeCompare(b.name,"ru",{sensitivity:"base",numeric:true})
  };
  const plural=(n,a,b,c)=>{const m=n%100,d=n%10;return m>10&&m<20?c:d===1?a:d>1&&d<5?b:c};
  const cnt=n=>n+" "+plural(n,"трек","трека","треков");
  const nameOf=t=>t.artist&&t.artist!=="—"?t.artist.trim():"";
  const initial=n=>(n.replace(/^[^\p{L}\p{N}]+/u,"").charAt(0)||"?").toUpperCase();
  const aid=n=>hash(n.trim().toLowerCase()).toString(36);
  const lastCoverTrack=n=>{
    const k=n.trim().toLowerCase();
    let best=null;
    state.tracks.forEach(t=>{
      if(!t.cover||!names(t).some(x=>x.toLowerCase()===k)) return;
      if(!best||t.id>best.id) best=t;                 // id = момент добавления трека
    });
    return best;
  };
  const avatar=(n,big)=>{
    const v=n&&state.artistImg[aid(n)];
    if(v) return `<div class="av img${big?" big":""}" style="background:url(/api/img/artist/${aid(n)}?v=${v}) center/cover no-repeat #222"></div>`;
    // нет своего фото — берём обложку последнего добавленного трека артиста, у которого она есть
    const lt=n&&lastCoverTrack(n);
    if(lt) return `<div class="av img${big?" big":""}" style="background:url(/api/img/cover/${lt.id}?v=${lt.cover}) center/cover no-repeat #222"></div>`;
    return `<div class="av${big?" big":""}" style="background:${coverBg(n,"")}">${esc(n?initial(n):"?")}</div>`;
  };
  const CAMI='<svg class="ico" viewBox="0 0 24 24"><path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="4"/></svg>';
  async function setImage(f,key){
    key=key||curKey;if(!key) return;
    try{
      const blob=await toJpeg(f),id=aid(key);
      const r=await fetch(`/api/img/artist/${id}`,{method:"POST",body:blob});
      if(!r.ok) throw new Error(r.status);
      state.artistImg[id]=Date.now();saveArtists();
      if(view==="artist"&&curKey===key) showArtist(key,"none");
      toast("Фото артиста сохранено");
    }catch(e){toast("Не удалось сохранить фото",true)}
  }
  function removeImage(key){
    const id=aid(key);
    delete state.artistImg[id];saveArtists();delImg("artist",id);
    showArtist(key,"none");toast("Фото убрано");
  }
  const scoreCol=rgCol;
  // «Blockkid, MAYOT» → два артиста; трек попадает в карточку каждого
  const names=t=>{
    const seen=new Set(),out=[];
    String(t.artist||"").split(/[,，]/).forEach(x=>{
      const n=x.trim(),k=n.toLowerCase();
      if(n&&n!=="—"&&!seen.has(k)){seen.add(k);out.push(n)}
    });
    return out;
  };
  function groups(){
    const m=new Map();
    state.tracks.forEach(t=>{
      const ns=names(t);
      (ns.length?ns:[""]).forEach(name=>{
        const k=name.toLowerCase();
        if(!m.has(k)) m.set(k,{key:k,name,items:[]});
        m.get(k).items.push(t);
      });
    });
    return [...m.values()].map(g=>{
      const rt=g.items.filter(isRated);
      g.avg=rt.length?rt.reduce((a,t)=>a+t.total,0)/rt.length:0;
      g.best=rt.length?Math.max(...rt.map(t=>t.total)):0;
      return g;
    }).sort((a,b)=>(!a.key)-(!b.key)||b.items.length-a.items.length||b.avg-a.avg);
  }
  // плавная смена содержимого страницы: kind = open | fwd | back | none (без анимации, скролл сохраняется)
  function enter(){body.classList.remove("en");void body.offsetWidth;body.classList.add("en")}
  function go(render,kind){
    const my=++tok;
    if(kind==="none"){body.classList.remove("en","lv");render(false);return}
    body.style.setProperty("--d",kind==="back"?-1:kind==="open"?0:1);
    const paint=()=>{body.classList.remove("lv");render(true);enter()};
    if(kind==="open"||pm.hidden){paint();return}
    body.classList.remove("en");body.classList.add("lv");
    setTimeout(()=>{if(my===tok) paint()},150);
  }
  const card=(g,i)=>`<div class="pcrd" style="--i:${i};--hh:${hueFor(g.avg||5).toFixed(0)}" data-k="${esc(g.key)}" role="button" tabindex="0">
          <div class="pav">${avatar(g.name)}<div class="pbadge" style="color:${scoreCol(g.avg)}" title="Средняя оценка">${fmt(g.avg)}</div></div>
          <div class="pn"><b>${g.name?esc(g.name):"Без исполнителя"}</b><span>${cnt(g.items.length)} · лучший ${fmt(g.best)}</span></div>
        </div>`;
  let rsT=0;
  function resort(mode){                                   // плавная смена сортировки: карточки гаснут, перестраиваются и мягко появляются
    sortMode=mode;
    body.querySelectorAll(".psb").forEach(b=>b.classList.toggle("on",b.dataset.s===mode));
    const grid=body.querySelector(".pgrid");
    if(!grid||view!=="list"){showList("open");return}
    const my=++tok;grid.classList.add("out");clearTimeout(rsT);
    rsT=setTimeout(()=>{
      if(my!==tok||view!=="list") return;
      const q=pq.value.trim().toLowerCase(),all=groups();
      const arr=(q?all.filter(g=>(g.name||"без исполнителя").toLowerCase().includes(q)):all).slice().sort(SORTS[sortMode]);
      grid.innerHTML=arr.map(card).join("");body.scrollTop=0;grid.classList.remove("out");
    },180);
  }
  function showList(kind="back"){
    view="list";curKey=null;
    go(anim=>{
      back.hidden=true;pqw.hidden=false;
      pm.style.setProperty("--ph",352);
      const all=groups(),q=pq.value.trim().toLowerCase();
      const arr=(q?all.filter(g=>(g.name||"без исполнителя").toLowerCase().includes(q)):all).slice().sort(SORTS[sortMode]);
      const subt=all.length?`${all.length} ${plural(all.length,"артист","артиста","артистов")} · ${cnt(state.tracks.length)}`:"Пока никого нет";
      const head=`<div class="phead"><div><h2>Профили артистов</h2><p>${subt}</p></div>`+(all.length>1?`<div class="psort" role="group" aria-label="Сортировка"><button class="psb${sortMode==="rating"?" on":""}" data-s="rating" type="button">По рейтингу</button><button class="psb${sortMode==="alpha"?" on":""}" data-s="alpha" type="button">По алфавиту</button></div>`:"")+`</div>`;
      if(!arr.length){
        body.innerHTML=head+(all.length
          ?`<div class="empty"><b>Никого не нашли</b><p>Проверь никнейм в поиске</p></div>`
          :`<div class="empty"><div class="bigic">${NOTE}</div><b>Здесь пока пусто</b><p>Оцени трек и впиши исполнителя — его профиль появится здесь</p></div>`);
      }else{
        body.innerHTML=head+`<div class="pgrid">`+arr.map(card).join("")+`</div>`;
      }
      if(anim) body.scrollTop=0;
    },kind);
  }
  function showArtist(key,kind="fwd"){
    const g=groups().find(x=>x.key===key);
    if(!g){showList();return}
    view="artist";curKey=key;
    go(anim=>{
      back.hidden=false;pqw.hidden=true;
      pm.style.setProperty("--ph",hueFor(g.avg||5).toFixed(1));
      const items=g.items.slice().sort((a,b)=>b.total-a.total||b.id-a.id);
      const hasImg=!!(g.name&&state.artistImg[aid(g.name)]);
      const av=g.name
        ?`<div class="avw" data-act="img" title="Загрузить фото артиста">${avatar(g.name,true)}<div class="avh">${CAMI}</div>${hasImg?'<button class="avx" data-act="rm" type="button" title="Убрать фото"><svg class="ico" viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg></button>':""}</div>`
        :avatar(g.name,true);
      body.innerHTML=`<div class="ahead" style="--i:0">${av}
        <div><div class="eyebrow">Артист</div><h3>${g.name?esc(g.name):"Без исполнителя"}</h3>
        <p>${cnt(items.length)} · средняя <b style="color:${scoreCol(g.avg)}">${fmt(g.avg)}</b> · лучший <b style="color:${scoreCol(g.best)}">${fmt(g.best)}</b></p></div></div>
        <div class="alh" style="--i:1">Треки</div>`
        +items.map((t,i)=>`<div class="item" style="--i:${i+2}" data-id="${t.id}">
          <div class="cv" style="${coverCss(t)}">${hasCov(t)?"":NOTE}</div>
          <div class="tx"><b>${esc(t.title)}</b><span>${t.beat?'<em class="pb">prod. '+esc(t.beat)+'</em> · ':""}${t.genre?esc(t.genre):"без жанра"}${(o=>o.length?" · с "+esc(o.join(", ")):"")(names(t).filter(n=>n.toLowerCase()!==key))}</span></div>
          <div class="sc" style="color:${scoreCol(t.total)}">${fmt(t.total)}</div>
          <div class="lk" title="${t.audio?"Есть аудиофайл":""}">${t.audio?'<svg class="ico" viewBox="0 0 24 24"><path d="M3 18v-6a9 9 0 0 1 18 0v6"/><path d="M21 19a2 2 0 0 1-2 2h-1a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h3zM3 19a2 2 0 0 0 2 2h1a2 2 0 0 0 2-2v-3a2 2 0 0 0-2-2H3z"/></svg>':""}</div>
        </div>`).join("");
      if(anim) body.scrollTop=0;
    },kind);
  }
  function open(){
    clearTimeout(closeT);
    const live=!pm.hidden&&!pm.classList.contains("out");
    pm.classList.remove("out");
    if(live){if(view==="artist"){pq.value="";showList("back")}return}
    pq.value="";pm.hidden=false;
    document.body.classList.add("pg");navBtn.classList.add("on");
    showList("open");
    if(document.activeElement&&document.activeElement.blur) document.activeElement.blur();
  }
  function close(keep){
    if(pm.hidden||pm.classList.contains("out")) return;
    if(keep!==true) document.body.classList.remove("pg");
    navBtn.classList.remove("on");
    pm.classList.add("out");
    closeT=setTimeout(()=>{pm.hidden=true;pm.classList.remove("out")},280);
  }
  body.addEventListener("click",e=>{
    const sb=e.target.closest(".psb");
    if(sb){if(sb.dataset.s!==sortMode) resort(sb.dataset.s);return}
    const act=e.target.closest("[data-act]");
    if(act&&curKey!==null){
      const key=curKey;
      if(act.dataset.act==="rm") removeImage(key);else pickImage(f=>setImage(f,key));
      return;
    }
    const a=e.target.closest(".pcrd");
    if(a){showArtist(a.dataset.k,"fwd");return}
    const it=e.target.closest(".item");
    if(!it) return;
    const id=+it.dataset.id,t=state.tracks.find(x=>x.id===id);
    close();loadTrack(id);
    if(t&&t.audio) AU.play(true);
  });
  back.addEventListener("click",()=>showList("back"));
  body.addEventListener("keydown",e=>{if(e.key==="Enter"&&e.target.classList.contains("pcrd")) e.target.click()});
  $("#pClose").addEventListener("click",close);
  pm.addEventListener("pointerdown",e=>{if(e.target===pm) close()});
  pq.addEventListener("input",()=>showList("none"));
  document.addEventListener("keydown",e=>{
    if(pm.hidden||e.key!=="Escape") return;
    e.preventDefault();
    if(view==="artist") showList("back");else close();
  });
  PAGE_CLOSE=close;
  return{open,close,artistKey:()=>view==="artist"&&curKey?curKey:null,setImage,groups,avatar,cnt,showArtist};
})();
$("#navArtists").addEventListener("click",()=>{TOP.close(true);RK.close(true);CT.close(true);LB.close(true);try{RM.close(true)}catch(e){}try{PF.close(true);SHOP.close(true)}catch(e){}AP.open()});

/* ---------- пьедестал: топ-3 артистов по среднему рейтингу ---------- */
const TOP=(()=>{
  const el=$("#tp"),body=$("#tpBody"),brand=$("#brand");let closeT=0;
  const CROWN='<svg viewBox="0 0 24 24"><path d="M3 8l4.5 4L12 4l4.5 8L21 8l-2 11H5L3 8z"/></svg>';
  const spk=(n,seed)=>{let h="";for(let i=0;i<n;i++){const r=k=>{const v=Math.sin(seed*91.7+i*12.9898+k*78.233)*43758.5453;return v-Math.floor(v)};
    h+=`<i style="--x:${(8+r(1)*84).toFixed(0)}%;--y:${(18+r(2)*70).toFixed(0)}%;--dx:${(r(3)*40-20).toFixed(0)}px;--t:${(2.4+r(4)*2).toFixed(1)}s;--w:${(r(5)*3).toFixed(1)}s;--z:${(3+r(6)*3).toFixed(0)}px"></i>`}return `<div class="tp-spk">${h}</div>`};
  function data(){
    return AP.groups().filter(g=>g.key).map(g=>({...g,n:g.items.filter(isRated).length})).filter(g=>g.n>0)
      .sort((a,b)=>b.avg-a.avg||b.best-a.best||b.n-a.n||a.name.localeCompare(b.name,"ru")).slice(0,3);
  }
  function render(){
    const top=data();
    if(!top.length){
      body.innerHTML=`<div class="tp-empty"><b>Пока некого ставить на пьедестал</b>Оцени треки и впиши исполнителей — лучшие появятся здесь</div>`;return;
    }
    const slot=(place,g)=>{
      if(!g) return `<div class="tpl p${place} empty"><div class="tpi"></div><div class="tp-step"><b>${place}</b></div></div>`;
      const sparks=place===1?spk(9,1):place===2?spk(4,2):spk(1,3);
      return `<div class="tpl p${place}"><div class="tpi">
        ${place===1?`<div class="tp-crown">${CROWN}</div>`:""}
        <div class="tp-av">${place===1?'<div class="tp-rays"></div>':""}${AP.avatar(g.name,true)}</div>
        <b class="tp-nm" title="${esc(g.name)}">${esc(g.name)}</b>
        <div class="tp-sc" style="color:${rgCol(g.avg)}">${fmt(g.avg)}</div>
        <span class="tp-ct">${AP.cnt(g.n)} · средняя оценка</span>
        ${sparks}
      </div><div class="tp-step"><b>${place}</b></div></div>`;
    };
    body.innerHTML=`<div class="tph"><div class="eyebrow" style="justify-content:center">PULSE RATER</div><h2>Топ-3 артистов</h2><p>по среднему рейтингу треков</p></div>
      <div class="pod">${slot(2,top[1])}${slot(1,top[0])}${slot(3,top[2])}</div>`;
    body.scrollTop=0;
  }
  /* ---------- частицы на фоне: искры вверх + конфетти (залп при открытии и раз в 9 секунд) ---------- */
  const cv=$("#tpCv"),cx=cv.getContext("2d"),RM=matchMedia("(prefers-reduced-motion: reduce)").matches;
  const COLS=["255,211,94","255,201,74","255,90,110","255,255,255","196,212,255","226,142,84"];
  let raf=0,ps=[],W=0,H=0,last=0,burst=0;
  const fit=()=>{const r=el.getBoundingClientRect(),d=Math.min(devicePixelRatio||1,2);W=r.width;H=r.height;cv.width=Math.max(1,W*d);cv.height=Math.max(1,H*d);cx.setTransform(d,0,0,d,0,0)};
  const ember=init=>({k:0,x:Math.random()*W,y:init?Math.random()*H:H+10,vx:(Math.random()-.5)*14,vy:-(18+Math.random()*40),r:.8+Math.random()*2.2,ph:Math.random()*6.28,c:COLS[Math.random()*3|0],a:.35+Math.random()*.6});
  const conf=()=>({k:1,x:Math.random()*W,y:-10-Math.random()*H*.3,vx:(Math.random()-.5)*90,vy:50+Math.random()*110,w:5+Math.random()*6,h:8+Math.random()*8,rot:Math.random()*6.28,vr:(Math.random()-.5)*10,ph:Math.random()*6.28,c:COLS[Math.random()*COLS.length|0],life:0,max:4+Math.random()*2});
  function tick(now){
    const dt=Math.min(.05,(now-last)/1000);last=now;
    cx.clearRect(0,0,W,H);
    for(let i=ps.length-1;i>=0;i--){
      const p=ps[i];
      if(p.k===0){
        p.ph+=dt*2;p.x+=(p.vx+Math.sin(p.ph)*12)*dt;p.y+=p.vy*dt;
        if(p.y<-10){ps[i]=ember(false);continue}
        const a=p.a*(.6+.4*Math.sin(p.ph*2.3));
        cx.globalCompositeOperation="lighter";cx.shadowBlur=10;cx.shadowColor=`rgba(${p.c},1)`;
        cx.fillStyle=`rgba(${p.c},${a})`;cx.beginPath();cx.arc(p.x,p.y,p.r,0,6.283);cx.fill();
      }else{
        p.life+=dt;p.ph+=dt*3;p.vy=Math.min(p.vy+30*dt,170);p.x+=(p.vx+Math.sin(p.ph)*30)*dt;p.y+=p.vy*dt;p.rot+=p.vr*dt;
        if(p.life>p.max||p.y>H+20){ps.splice(i,1);continue}
        cx.globalCompositeOperation="source-over";cx.shadowBlur=0;
        cx.globalAlpha=Math.min(1,(p.max-p.life)*1.2)*.9;cx.fillStyle=`rgb(${p.c})`;
        cx.save();cx.translate(p.x,p.y);cx.rotate(p.rot);cx.scale(1,Math.cos(p.ph*1.7));cx.fillRect(-p.w/2,-p.h/2,p.w,p.h);cx.restore();cx.globalAlpha=1;
      }
    }
    cx.shadowBlur=0;cx.globalCompositeOperation="source-over";
    raf=requestAnimationFrame(tick);
  }
  function startFx(){
    if(RM) return;
    fit();ps=[];for(let i=0;i<28;i++) ps.push(ember(true));
    burst=0;last=performance.now();cancelAnimationFrame(raf);raf=requestAnimationFrame(tick);
  }
  function stopFx(){cancelAnimationFrame(raf);raf=0;ps=[];cx.clearRect(0,0,W,H)}
  addEventListener("resize",()=>{if(raf) fit()});
  const isOpen=()=>!el.hidden&&!el.classList.contains("out");
  function open(){
    clearTimeout(closeT);
    if(isOpen()) return;
    el.classList.remove("out");AP.close(true);RK.close(true);CT.close(true);LB.close(true);try{RM.close(true)}catch(e){}try{PF.close(true);SHOP.close(true)}catch(e){}render();
    el.hidden=false;document.body.classList.add("pg");brand.classList.add("on");startFx();
    if(document.activeElement&&document.activeElement.blur) document.activeElement.blur();
  }
  function close(keep){
    if(el.hidden||el.classList.contains("out")) return;
    if(keep!==true) document.body.classList.remove("pg");
    brand.classList.remove("on");el.classList.add("out");
    closeT=setTimeout(()=>{el.hidden=true;el.classList.remove("out");stopFx()},280);
  }
  $("#tpClose").addEventListener("click",close);
  el.addEventListener("pointerdown",e=>{if(e.target===el) close()});
  document.addEventListener("keydown",e=>{if(e.key==="Escape"&&isOpen()){e.preventDefault();close()}});
  return{open,close,isOpen,toggle:()=>isOpen()?close():open()};
})();
PAGE_CLOSE=()=>{AP.close();TOP.close();RK.close();CT.close();LB.close();try{RM.close()}catch(e){}try{PF.close();SHOP.close()}catch(e){}};
$("#brand").addEventListener("click",()=>{try{if(PAGE_CLOSE) PAGE_CLOSE()}catch(e){}try{SET.close()}catch(e){}main.scrollTo({top:0,behavior:"smooth"})});

/* ---------- общий рейтинг артистов (с фильтром по жанру) ---------- */
const RK=(()=>{
  const el=$("#rk"),body=$("#rkBody"),navBtn=$("#navRank");
  let closeT=0,genre="all",tk=0;
  const plural=(n,a,b,c)=>{const m=n%100,d=n%10;return m>10&&m<20?c:d===1?a:d>1&&d<5?b:c};
  const gKey=t=>(t.genre||"").trim().toLowerCase();
  const gLab=k=>k?k.charAt(0).toUpperCase()+k.slice(1):"Без жанра";
  const nArt=n=>n+" "+plural(n,"артист","артиста","артистов");
  function genres(){
    const m=new Map();
    AP.groups().filter(g=>g.key).forEach(g=>g.items.filter(isRated).forEach(t=>{
      const k=gKey(t);if(!m.has(k)) m.set(k,new Set());m.get(k).add(g.key);
    }));
    return [...m.entries()].map(([k,s])=>({k,n:s.size}))
      .sort((a,b)=>(!a.k)-(!b.k)||b.n-a.n||a.k.localeCompare(b.k,"ru"));
  }
  function board(){
    return AP.groups().filter(g=>g.key).map(g=>{
      const it=g.items.filter(t=>isRated(t)&&(genre==="all"||gKey(t)===genre));
      return {key:g.key,name:g.name,n:it.length,
        avg:it.length?it.reduce((a,t)=>a+t.total,0)/it.length:0,
        best:it.length?Math.max(...it.map(t=>t.total)):0};
    }).filter(g=>g.n>0).sort((a,b)=>b.avg-a.avg||b.best-a.best||b.n-a.n||a.name.localeCompare(b.name,"ru"));
  }
  const row=(g,i)=>{
    const p=i+1;
    return `<div class="rkr${p<4?" r"+p:""}" style="--i:${i}" data-k="${esc(g.key)}" role="button" tabindex="0">
      <div class="rkp">${p}</div>${AP.avatar(g.name)}
      <div class="rkn"><b>${esc(g.name)}</b><span>${AP.cnt(g.n)} · лучший ${fmt(g.best)}</span></div>
      <div class="rkbar"><i style="width:${Math.max(2,g.avg*10).toFixed(1)}%;background:${rgCol(g.avg)}"></i></div>
      <div class="rks" style="color:${rgCol(g.avg)}">${fmt(g.avg)}</div>
    </div>`;
  };
  function paintList(){
    const list=body.querySelector(".rkl"),sub=body.querySelector(".rksub");
    if(!list) return;
    const arr=board();
    if(sub) sub.textContent=`${nArt(arr.length)} · ${genre==="all"?"все жанры":gLab(genre)}`;
    list.innerHTML=arr.length?arr.map(row).join(""):`<div class="empty"><b>В этом жанре пока никого</b><p>Оцени трек с таким жанром</p></div>`;
  }
  function render(){
    const gs=genres();
    if(genre!=="all"&&!gs.some(x=>x.k===genre)) genre="all";
    if(!gs.length){
      body.innerHTML=`<div class="phead"><div><h2>Общий рейтинг</h2><p>Пока нет оценённых треков</p></div></div>
        <div class="empty"><div class="bigic">${NOTE}</div><b>Здесь пока пусто</b><p>Оцени трек и впиши исполнителя — рейтинг появится здесь</p></div>`;
      return;
    }
    const all=AP.groups().filter(g=>g.key&&g.items.some(isRated)).length;
    const chip=(k,lab,n)=>`<button class="rkc${genre===k?" on":""}" data-g="${esc(k)}" type="button">${esc(lab)}<em>${n}</em></button>`;
    body.innerHTML=`<div class="phead"><div><h2>Общий рейтинг</h2><p class="rksub"></p></div></div>
      <div class="rkg" role="group" aria-label="Жанр">${chip("all","Все жанры",all)}${gs.map(x=>chip(x.k,gLab(x.k),x.n)).join("")}</div>
      <div class="rkl"></div>`;
    paintList();body.scrollTop=0;
  }
  const isOpen=()=>!el.hidden&&!el.classList.contains("out");
  function open(){
    clearTimeout(closeT);
    if(isOpen()) return;
    el.classList.remove("out");TOP.close(true);AP.close(true);CT.close(true);LB.close(true);try{RM.close(true)}catch(e){}try{PF.close(true);SHOP.close(true)}catch(e){}
    genre="all";render();
    el.hidden=false;document.body.classList.add("pg");navBtn.classList.add("on");
    if(document.activeElement&&document.activeElement.blur) document.activeElement.blur();
  }
  function close(keep){
    if(el.hidden||el.classList.contains("out")) return;
    if(keep!==true) document.body.classList.remove("pg");
    navBtn.classList.remove("on");el.classList.add("out");
    closeT=setTimeout(()=>{el.hidden=true;el.classList.remove("out")},280);
  }
  body.addEventListener("click",e=>{
    const c=e.target.closest(".rkc");
    if(c){
      if(c.dataset.g===genre) return;
      genre=c.dataset.g;
      body.querySelectorAll(".rkc").forEach(x=>x.classList.toggle("on",x===c));
      const list=body.querySelector(".rkl"),my=++tk;
      list.classList.add("out");
      setTimeout(()=>{if(my!==tk) return;paintList();list.classList.remove("out")},170);
      return;
    }
    const r=e.target.closest(".rkr");
    if(r){const k=r.dataset.k;close(true);AP.open();AP.showArtist(k,"open")}
  });
  body.addEventListener("keydown",e=>{if(e.key==="Enter"&&e.target.classList.contains("rkr")) e.target.click()});
  $("#rkClose").addEventListener("click",close);
  el.addEventListener("pointerdown",e=>{if(e.target===el) close()});
  document.addEventListener("keydown",e=>{if(e.key==="Escape"&&isOpen()){e.preventDefault();close()}});
  navBtn.addEventListener("click",()=>isOpen()?close():open());
  return{open,close,isOpen};
})();


/* ---------- мини-игра «Звёздопад» ---------- */
const GM={cur:"roul"};
const HS={stars:0,snake:0,rhythm:0};
let hsP=null;
function hsLoad(){
  return hsP||(hsP=(async()=>{
    try{const r=await fetch("/api/game",{cache:"no-store"});if(r.ok){const j=await r.json();HS.stars=+j.best||0;HS.snake=+j.snake||0;HS.rhythm=+j.rhythm||0}}catch(e){}
    try{HS.stars=Math.max(HS.stars,+localStorage.getItem("gameBest")||0);HS.snake=Math.max(HS.snake,+localStorage.getItem("snakeBest")||0);HS.rhythm=Math.max(HS.rhythm,+localStorage.getItem("rhythmBest")||0)}catch(e){}
  })());
}
async function hsSave(){
  const body=JSON.stringify({best:HS.stars,snake:HS.snake,rhythm:HS.rhythm});
  try{const r=await fetch("/api/game",{method:"POST",body,headers:{"Content-Type":"application/json"}});if(r.ok) return}catch(e){}
  try{localStorage.setItem("gameBest",String(HS.stars));localStorage.setItem("snakeBest",String(HS.snake));localStorage.setItem("rhythmBest",String(HS.rhythm))}catch(e){}
}

const G=(()=>{
  const gm=$("#gm"),wrap=$("#gwrap"),cv=$("#gc"),ctx=cv.getContext("2d"),over=$("#gOver"),btn=$("#gStart");
  const W=420,H=550,S=cv.width/W,PW=78,PY=H-46;
  const rnd=(a,b)=>a+Math.random()*(b-a);
  let st="ready",px=W/2,tx=W/2,score=0,best=0,lives=3,combo=0,mult=1,t=0,spawn=0,shake=0,flash=0,raf=0,last=0,L=0,R=0,newBest=false;
  let items=[],parts=[],floats=[];
  const bgs=Array.from({length:80},()=>({x:rnd(0,W),y:rnd(0,H),r:rnd(.4,1.5),v:rnd(10,40)}));

  const setLives=()=>{$("#gLives").innerHTML=Array.from({length:3},(_,i)=>`<i class="${i<lives?"":"off"}">♥</i>`).join("")};
  const setScore=()=>{$("#gScore").textContent=score};
  const setBest=()=>{$("#gBest").textContent=best};
  async function loadBest(){await hsLoad();best=HS.stars;setBest()}
  function saveBest(){HS.stars=best;hsSave()}

  function reset(){
    items=[];parts=[];floats=[];score=0;lives=3;combo=0;mult=1;t=0;spawn=.5;shake=0;flash=0;newBest=false;
    px=tx=W/2;setScore();setLives();
  }
  function burst(x,y,color,n){
    for(let i=0;i<n;i++){
      const a=rnd(Math.PI*1.08,Math.PI*1.92),v=rnd(60,230),life=rnd(.4,.85);
      parts.push({x,y,vx:Math.cos(a)*v,vy:Math.sin(a)*v,life,max:life,r:rnd(1,2.6),c:color||`hsl(${rnd(0,360)|0} 100% 65%)`});
    }
  }
  function caught(it){
    if(it.type==="meteor"){
      lives--;combo=0;mult=1;shake=.4;flash=.5;
      burst(it.x,PY,"#ff5a3c",24);setLives();
      if(lives<=0) end();
      return;
    }
    const gain=(it.type==="rgb"?5:1)*mult;
    score+=gain;combo++;
    if(combo%5===0&&mult<5) mult++;
    burst(it.x,PY-4,it.type==="rgb"?null:"#ffd9e2",it.type==="rgb"?24:12);
    floats.push({x:it.x,y:PY-18,txt:"+"+gain,life:.7});
    setScore();
  }

  function update(dt){
    t+=dt;
    if(L||R) tx=Math.max(PW/2,Math.min(W-PW/2,tx+(R-L)*470*dt));
    px+=(tx-px)*Math.min(1,dt*16);
    shake=Math.max(0,shake-dt);flash=Math.max(0,flash-dt*1.4);
    for(const s of bgs){s.y+=s.v*dt;if(s.y>H){s.y=-2;s.x=rnd(0,W)}}

    spawn-=dt;
    if(spawn<=0){
      spawn=Math.max(.3,.9-t*.0085)*rnd(.8,1.2);
      const r0=Math.random(),mp=Math.min(.36,.12+t*.003);
      const type=r0<mp?"meteor":r0<mp+.045?"rgb":"star";
      const base=130+Math.min(t*3.2,190);
      const it={type,x:rnd(24,W-24),y:-30,vy:base*rnd(.85,1.25)*(type==="meteor"?1.12:1),
        r:type==="meteor"?rnd(15,22):type==="rgb"?15:12,rot:rnd(0,6.28),vr:rnd(-2.5,2.5)};
      if(type==="meteor") it.pts=Array.from({length:9},()=>rnd(.74,1.08));
      items.push(it);
    }
    for(let i=items.length-1;i>=0;i--){
      const it=items[i];it.y+=it.vy*dt;it.rot+=it.vr*dt;
      const hit=it.y+it.r>PY-6&&it.y-it.r<PY+10&&Math.abs(it.x-px)<PW/2+it.r*.55;
      if(hit){items.splice(i,1);caught(it);if(st!=="playing") return;continue}
      if(it.y>H+40){items.splice(i,1);if(it.type!=="meteor"){combo=0;mult=1}}
    }
    for(let i=parts.length-1;i>=0;i--){
      const p=parts[i];p.life-=dt;
      if(p.life<=0){parts.splice(i,1);continue}
      p.x+=p.vx*dt;p.y+=p.vy*dt;p.vy+=420*dt;
    }
    for(let i=floats.length-1;i>=0;i--){
      const f=floats[i];f.life-=dt;f.y-=40*dt;
      if(f.life<=0) floats.splice(i,1);
    }
  }

  function starPath(c,x,y,r,rot){
    c.beginPath();
    for(let i=0;i<10;i++){const a=rot+i*Math.PI/5-Math.PI/2,rr=i%2?r*.45:r;c.lineTo(x+Math.cos(a)*rr,y+Math.sin(a)*rr)}
    c.closePath();
  }
  function rockPath(c,m){
    c.beginPath();const n=m.pts.length;
    for(let i=0;i<n;i++){const a=m.rot+i*2*Math.PI/n,rr=m.r*m.pts[i];c.lineTo(m.x+Math.cos(a)*rr,m.y+Math.sin(a)*rr)}
    c.closePath();
  }

  function draw(){
    ctx.save();ctx.setTransform(S,0,0,S,0,0);
    if(shake>0) ctx.translate(rnd(-1,1)*shake*14,rnd(-1,1)*shake*14);
    const g=ctx.createLinearGradient(0,0,0,H);g.addColorStop(0,"#1c0812");g.addColorStop(1,"#08020a");
    ctx.fillStyle=g;ctx.fillRect(-20,-20,W+40,H+40);

    ctx.fillStyle="#fff";
    for(const s of bgs){ctx.globalAlpha=.2+s.r*.28;ctx.beginPath();ctx.arc(s.x,s.y,s.r,0,6.2832);ctx.fill()}
    ctx.globalAlpha=1;

    // предметы
    for(const it of items){
      if(it.type==="meteor"){
        const tg=ctx.createLinearGradient(it.x,it.y-it.r*3.2,it.x,it.y);
        tg.addColorStop(0,"rgba(255,90,40,0)");tg.addColorStop(1,"rgba(255,110,50,.55)");
        ctx.fillStyle=tg;ctx.fillRect(it.x-it.r*.55,it.y-it.r*3.2,it.r*1.1,it.r*3.2);
        ctx.shadowColor="#ff3d2e";ctx.shadowBlur=14;
        rockPath(ctx,it);ctx.fillStyle="#2a1519";ctx.fill();
        ctx.lineWidth=2;ctx.strokeStyle="#ff6a3d";ctx.stroke();
        ctx.shadowBlur=0;
        ctx.fillStyle="rgba(0,0,0,.35)";
        ctx.beginPath();ctx.arc(it.x-it.r*.25,it.y-it.r*.2,it.r*.22,0,6.2832);ctx.fill();
        ctx.beginPath();ctx.arc(it.x+it.r*.3,it.y+it.r*.25,it.r*.15,0,6.2832);ctx.fill();
      }else{
        const rgb=it.type==="rgb",col=rgb?`hsl(${(t*220)%360|0} 100% 62%)`:"#ffe9f0";
        ctx.shadowColor=rgb?col:"#ff5a78";ctx.shadowBlur=rgb?20:14;
        starPath(ctx,it.x,it.y,it.r*(rgb?1.25:1),it.rot);
        ctx.fillStyle=col;ctx.fill();
        if(rgb){ctx.lineWidth=2;ctx.strokeStyle="#fff";ctx.stroke()}
        ctx.shadowBlur=0;
      }
    }

    // частицы
    for(const p of parts){ctx.globalAlpha=Math.max(0,p.life/p.max);ctx.fillStyle=p.c;ctx.beginPath();ctx.arc(p.x,p.y,p.r,0,6.2832);ctx.fill()}
    ctx.globalAlpha=1;

    // игрок
    const hue=(350+FX.bo)|0;
    ctx.shadowColor=`hsl(${hue} 100% 60%)`;ctx.shadowBlur=18;
    const pg=ctx.createLinearGradient(px-PW/2,0,px+PW/2,0);
    pg.addColorStop(0,`hsl(${hue-20} 90% 55%)`);pg.addColorStop(.5,"#fff");pg.addColorStop(1,`hsl(${hue+20} 90% 55%)`);
    ctx.fillStyle=pg;ctx.beginPath();ctx.roundRect(px-PW/2,PY,PW,10,5);ctx.fill();
    ctx.shadowBlur=0;
    ctx.fillStyle="rgba(255,255,255,.35)";ctx.beginPath();ctx.roundRect(px-PW/2+8,PY-4,PW-16,3,2);ctx.fill();

    // всплывающие очки и множитель
    ctx.textAlign="center";ctx.font="800 16px 'Segoe UI',sans-serif";
    for(const f of floats){ctx.globalAlpha=Math.min(1,f.life*2);ctx.fillStyle="#fff";ctx.fillText(f.txt,f.x,f.y)}
    ctx.globalAlpha=1;
    if(mult>1||combo>0){
      ctx.textAlign="right";ctx.font="900 22px 'Segoe UI',sans-serif";ctx.fillStyle="#fff";
      ctx.fillText("×"+mult,W-14,32);
      const prog=mult>=5?1:(combo%5)/5;
      ctx.fillStyle="rgba(255,255,255,.18)";ctx.fillRect(W-74,40,60,4);
      ctx.fillStyle=`hsl(${hue} 100% 66%)`;ctx.fillRect(W-74,40,60*prog,4);
    }
    if(flash>0){ctx.fillStyle=`rgba(255,40,60,${flash*.45})`;ctx.fillRect(-20,-20,W+40,H+40)}
    ctx.restore();
  }

  function showOver(){
    const m={
      ready:["Звёздопад","Двигай мышью или стрелками. Лови звёзды, уворачивайся от метеоритов. Пропущенная звезда сбрасывает множитель.","Играть"],
      paused:["Пауза","Трек играет дальше — отдохни минутку.","Продолжить"],
      over:["Игра окончена","Счёт: "+score+(newBest?" — новый рекорд!":""),"Ещё раз"]
    }[st];
    $("#gTitle").textContent=m[0];$("#gSub").textContent=m[1];btn.textContent=m[2];
    over.hidden=false;wrap.classList.remove("play");
  }
  function loop(now){
    if(st!=="playing") return;
    const dt=Math.min(.033,(now-last)/1000);last=now;
    update(dt);
    if(st!=="playing") return;
    draw();raf=requestAnimationFrame(loop);
  }
  function start(){
    if(st==="ready"||st==="over") reset();
    st="playing";over.hidden=true;wrap.classList.add("play");btn.blur();
    last=performance.now();cancelAnimationFrame(raf);raf=requestAnimationFrame(loop);
  }
  function pause(){
    if(st!=="playing") return;
    st="paused";cancelAnimationFrame(raf);draw();showOver();
  }
  function end(){
    st="over";cancelAnimationFrame(raf);
    newBest=score>best;
    if(newBest){best=score;setBest();saveBest()}
    draw();showOver();
  }
  function enter(){L=R=0;draw();showOver()}

  const setX=e=>{const r=cv.getBoundingClientRect();tx=Math.max(PW/2,Math.min(W-PW/2,(e.clientX-r.left)/r.width*W))};
  cv.addEventListener("pointermove",e=>{if(st==="playing") setX(e)});
  cv.addEventListener("pointerdown",e=>{if(st==="playing") setX(e)});
  btn.addEventListener("click",start);
  addEventListener("blur",()=>{if(!gm.hidden) pause()});
  document.addEventListener("visibilitychange",()=>{if(document.hidden&&!gm.hidden) pause()});

  // перехватываем клавиши, чтобы стрелки не крутили ползунки оценки
  document.addEventListener("keydown",e=>{
    if(gm.hidden||GM.cur!=="stars"||e.ctrlKey||e.metaKey||e.altKey) return;
    const c=e.code;
    if(c==="Escape"){M.close()}
    else if(c==="ArrowLeft"||c==="KeyA"){L=1}
    else if(c==="ArrowRight"||c==="KeyD"){R=1}
    else if(c==="Space"||c==="Enter"||c==="KeyP"){
      if(e.target===btn&&c!=="KeyP") return;
      if(!e.repeat){if(st==="playing") pause();else start()}
    }
    else return;
    e.preventDefault();e.stopPropagation();
  },true);
  document.addEventListener("keyup",e=>{
    if(gm.hidden) return;
    if(e.code==="ArrowLeft"||e.code==="KeyA") L=0;
    if(e.code==="ArrowRight"||e.code==="KeyD") R=0;
  },true);

  setLives();loadBest();
  return{enter,pause};
})();
/* ---------- мини-игра «Змейка» ---------- */
const S=(()=>{
  const gm=$("#gm"),cv=$("#sc"),ctx=cv.getContext("2d"),over=$("#sOver"),btn=$("#sStart"),wrap=$("#swrap");
  const N=21,C=20,SZ=N*C,K=cv.width/SZ;
  const rnd=(a,b)=>a+Math.random()*(b-a);
  let st="ready",snake=[],prev=[],dir={x:1,y:0},queue=[],food=null,score=0,best=0,acc=0,last=0,raf=0,t=0,fp=0,shake=0,newBest=false,parts=[];
  const stepTime=()=>1/Math.min(17,9.5+score*.3);

  const setScore=()=>{$("#sScore").textContent=score;$("#sLen").textContent=snake.length};
  const setBest=()=>{$("#sBest").textContent=best};
  async function loadBest(){await hsLoad();best=HS.snake;setBest()}
  function saveBest(){HS.snake=best;hsSave()}

  function placeFood(){
    const free=[];
    for(let x=0;x<N;x++)for(let y=0;y<N;y++) if(!snake.some(s=>s.x===x&&s.y===y)) free.push({x,y});
    food=free.length?free[(Math.random()*free.length)|0]:null;
  }
  function reset(){
    const m=(N/2)|0;
    snake=[{x:m,y:m},{x:m-1,y:m},{x:m-2,y:m}];prev=snake.map(s=>({...s}));
    dir={x:1,y:0};queue=[];score=0;acc=0;t=0;shake=0;newBest=false;parts=[];
    placeFood();setScore();
  }
  function turn(dx,dy){
    const ref=queue.length?queue[queue.length-1]:dir;
    if((dx===ref.x&&dy===ref.y)||(dx===-ref.x&&dy===-ref.y)) return;
    if(queue.length<2) queue.push({x:dx,y:dy});
    /* поворот срабатывает сразу, а не ждёт следующего такта */
    if(st==="playing"&&queue.length===1&&acc>stepTime()*.3){acc=0;step()}
  }
  function burst(cx,cy){
    for(let i=0;i<16;i++){
      const a=rnd(0,6.2832),v=rnd(30,120),life=rnd(.35,.7);
      parts.push({x:cx*C+C/2,y:cy*C+C/2,vx:Math.cos(a)*v,vy:Math.sin(a)*v,life,max:life,c:`hsl(${rnd(0,360)|0} 100% 68%)`});
    }
  }
  function step(){
    if(queue.length) dir=queue.shift();
    prev=snake.map(s=>({...s}));
    const h={x:snake[0].x+dir.x,y:snake[0].y+dir.y};
    const eat=!!food&&h.x===food.x&&h.y===food.y;
    const body=eat?snake:snake.slice(0,-1);
    if(h.x<0||h.y<0||h.x>=N||h.y>=N||body.some(s=>s.x===h.x&&s.y===h.y)){die();return}
    snake.unshift(h);
    if(eat){score++;burst(h.x,h.y);placeFood();setScore()}else snake.pop();
  }
  function die(){
    st="over";cancelAnimationFrame(raf);shake=.4;
    newBest=score>best;
    if(newBest){best=score;setBest();saveBest()}
    draw();showOver();
  }

  function draw(){
    ctx.save();ctx.setTransform(K,0,0,K,0,0);
    if(shake>0) ctx.translate(rnd(-1,1)*shake*10,rnd(-1,1)*shake*10);
    const g=ctx.createLinearGradient(0,0,SZ,SZ);g.addColorStop(0,"#1a0812");g.addColorStop(1,"#08030c");
    ctx.fillStyle=g;ctx.fillRect(-20,-20,SZ+40,SZ+40);
    ctx.fillStyle="rgba(255,255,255,.05)";
    for(let x=0;x<N;x++)for(let y=0;y<N;y++) ctx.fillRect(x*C+C/2-1,y*C+C/2-1,2,2);

    if(food){                                                   // светящаяся еда
      const px=food.x*C+C/2,py=food.y*C+C/2,pu=1+.12*Math.sin(fp*5);
      ctx.shadowColor="#ff5a78";ctx.shadowBlur=18;ctx.fillStyle="#ffe9f0";
      ctx.beginPath();ctx.arc(px,py,C*.28*pu,0,6.2832);ctx.fill();
      ctx.shadowBlur=0;ctx.strokeStyle="rgba(255,90,120,.55)";ctx.lineWidth=2;
      ctx.beginPath();ctx.arc(px,py,C*.42*pu,0,6.2832);ctx.stroke();
    }

    const k=Math.min(1,acc/stepTime());                          // плавное движение между клетками
    const pts=snake.map((s,i)=>{const p=prev[i]||s;return{x:(p.x+(s.x-p.x)*k)*C+C/2,y:(p.y+(s.y-p.y)*k)*C+C/2}});
    ctx.lineCap="round";ctx.lineJoin="round";
    for(let i=pts.length-1;i>=0;i--){
      const p=pts[i],r=C*(.44-Math.min(.14,i*.006)),hue=(t*90+i*14+350)%360;
      ctx.shadowColor=`hsl(${hue} 100% 60%)`;ctx.shadowBlur=i%3===0?(i===0?16:9):0;
      if(i<pts.length-1){
        const q=pts[i+1];ctx.strokeStyle=`hsl(${hue} 90% 58%)`;ctx.lineWidth=r*1.84;
        ctx.beginPath();ctx.moveTo(q.x,q.y);ctx.lineTo(p.x,p.y);ctx.stroke();
      }
      ctx.fillStyle=`hsl(${hue} 95% 60%)`;ctx.beginPath();ctx.arc(p.x,p.y,r,0,6.2832);ctx.fill();
    }
    ctx.shadowBlur=0;
    if(pts.length){                                              // глаза
      const hd=pts[0],ex=dir.x,ey=dir.y;
      for(const s of[-1,1]){
        const x=hd.x+ex*C*.16-ey*s*C*.17,y=hd.y+ey*C*.16+ex*s*C*.17;
        ctx.fillStyle="#fff";ctx.beginPath();ctx.arc(x,y,C*.11,0,6.2832);ctx.fill();
        ctx.fillStyle="#120208";ctx.beginPath();ctx.arc(x+ex*C*.04,y+ey*C*.04,C*.055,0,6.2832);ctx.fill();
      }
    }
    for(const p of parts){ctx.globalAlpha=Math.max(0,p.life/p.max);ctx.fillStyle=p.c;ctx.beginPath();ctx.arc(p.x,p.y,2.2,0,6.2832);ctx.fill()}
    ctx.globalAlpha=1;
    ctx.restore();
  }

  function showOver(){
    const m={
      ready:["Змейка","Ешь светящиеся шарики и не врезайся в стены и в себя. Управление — стрелки или WASD.","Играть"],
      paused:["Пауза","Трек играет дальше.","Продолжить"],
      over:["Игра окончена","Счёт: "+score+(newBest?" — новый рекорд!":""),"Ещё раз"]
    }[st];
    $("#sTitle").textContent=m[0];$("#sSub").textContent=m[1];btn.textContent=m[2];
    over.hidden=false;
  }
  function loop(now){
    if(st!=="playing") return;
    const dt=Math.min(.05,(now-last)/1000);last=now;t+=dt;fp+=dt;
    shake=Math.max(0,shake-dt);acc+=dt;
    let guard=0;
    while(acc>=stepTime()&&guard++<3){acc-=stepTime();step();if(st!=="playing") return}
    for(let i=parts.length-1;i>=0;i--){
      const p=parts[i];p.life-=dt;
      if(p.life<=0){parts.splice(i,1);continue}
      p.x+=p.vx*dt;p.y+=p.vy*dt;
    }
    draw();raf=requestAnimationFrame(loop);
  }
  function start(){
    if(st==="ready"||st==="over") reset();
    st="playing";over.hidden=true;btn.blur();
    last=performance.now();cancelAnimationFrame(raf);raf=requestAnimationFrame(loop);
  }
  function pause(){
    if(st!=="playing") return;
    st="paused";cancelAnimationFrame(raf);draw();showOver();
  }
  function enter(){draw();showOver()}

  btn.addEventListener("click",start);
  addEventListener("blur",()=>{if(!gm.hidden) pause()});
  document.addEventListener("visibilitychange",()=>{if(document.hidden&&!gm.hidden) pause()});
  document.addEventListener("keydown",e=>{
    if(gm.hidden||GM.cur!=="snake"||e.ctrlKey||e.metaKey||e.altKey) return;
    const c=e.code;let d=null;
    if(c==="ArrowUp"||c==="KeyW") d=[0,-1];
    else if(c==="ArrowDown"||c==="KeyS") d=[0,1];
    else if(c==="ArrowLeft"||c==="KeyA") d=[-1,0];
    else if(c==="ArrowRight"||c==="KeyD") d=[1,0];
    if(c==="Escape"){M.close()}
    else if(d){if(st!=="over"){if(st!=="playing") start();turn(d[0],d[1])}}
    else if(c==="Space"||c==="Enter"||c==="KeyP"){
      if(e.target===btn&&c!=="KeyP") return;
      if(!e.repeat){if(st==="playing") pause();else start()}
    }
    else return;
    e.preventDefault();e.stopPropagation();
  },true);

  reset();loadBest();
  return{enter,pause};
})();

/* ---------- окно мини-игр (вкладки) ---------- */
/* ---------- мини-игра «Ритм»: ноты падают по 4 дорожкам ---------- */
const R=(()=>{
  const gm=$("#gm"),cv=$("#rc"),ctx=cv.getContext("2d"),over=$("#rOver"),btn=$("#rStart"),wrap=$("#rwrap");
  const W=420,H=550,K=cv.width/W,LN=4,LW=W/LN,HY=H-78,WIN=46;
  const KEYS={KeyD:0,KeyF:1,KeyJ:2,KeyK:3,ArrowLeft:0,ArrowDown:1,ArrowUp:2,ArrowRight:3},LBL=["D","F","J","K"],HUES=[350,300,195,160];
  const rnd=(a,b)=>a+Math.random()*(b-a);
  let st="ready",notes=[],parts=[],floats=[],score=0,best=0,combo=0,lives=5,t=0,next=0,raf=0,last=0,flash=0,newBest=false,lastLane=-1,grid=0;
  const glow=[0,0,0,0];
  const hue=i=>HUES[i]+((typeof FX!=="undefined"&&FX.bo)||0)*.3;
  const mult=()=>Math.min(4,1+Math.floor(combo/10));
  const setScore=()=>{$("#rScore").textContent=score;$("#rCombo").textContent=combo};
  const setLives=()=>{$("#rLives").innerHTML=Array.from({length:5},(_,i)=>`<i class="${i<lives?"":"off"}">♥</i>`).join("")};
  async function loadBest(){await hsLoad();best=HS.rhythm;$("#rBest").textContent=best}
  function reset(){notes=[];parts=[];floats=[];score=0;combo=0;lives=5;t=0;next=.8;flash=0;newBest=false;lastLane=-1;setScore();setLives()}
  const speed=()=>Math.min(560,250+t*4.2);
  const gap=()=>Math.max(.26,.62-t*.0045);
  function spawn(){
    let l;do{l=(Math.random()*LN)|0}while(l===lastLane&&Math.random()<.7);
    lastLane=l;notes.push({l,y:-20});
    if(t>12&&Math.random()<.16){let l2;do{l2=(Math.random()*LN)|0}while(l2===l);notes.push({l:l2,y:-20})}   // аккорд
    next+=gap()*(Math.random()<.22?.5:1);
  }
  function burst(l,c,n){
    const x=l*LW+LW/2;
    for(let i=0;i<n;i++){const a=rnd(Math.PI*1.1,Math.PI*1.9),v=rnd(80,260),life=rnd(.35,.7);
      parts.push({x,y:HY,vx:Math.cos(a)*v,vy:Math.sin(a)*v,life,max:life,c})}
  }
  function judge(l,txt,col){floats.push({x:l*LW+LW/2,y:HY-34,txt,col,life:.6})}
  function press(l){
    if(st!=="playing") return;
    glow[l]=1;
    let bestN=null,bd=1e9;
    for(const n of notes){if(n.l!==l) continue;const d=Math.abs(n.y-HY);if(d<bd){bd=d;bestN=n}}
    if(!bestN||bd>WIN) return;                               // пустой удар — без штрафа
    notes.splice(notes.indexOf(bestN),1);
    const [pts,txt]=bd<14?[300,"Идеально"]:bd<30?[200,"Отлично"]:[100,"Норм"];
    combo++;score+=pts*mult();
    burst(l,`hsl(${hue(l)|0} 100% ${bd<14?80:68}%)`,bd<14?18:10);
    judge(l,txt,bd<14?"#fff":`hsl(${hue(l)|0} 100% 78%)`);
    setScore();
  }
  function miss(n){
    combo=0;lives--;flash=.45;judge(n.l,"Мимо","#ff6b7a");setScore();setLives();
    if(lives<=0) end();
  }
  function update(dt){
    t+=dt;grid=(grid+speed()*dt)%60;flash=Math.max(0,flash-dt*1.6);
    for(let i=0;i<LN;i++) glow[i]=Math.max(0,glow[i]-dt*4);
    while(t>=next) spawn();
    const v=speed();
    for(let i=notes.length-1;i>=0;i--){
      const n=notes[i];n.y+=v*dt;
      if(n.y>HY+WIN+6){notes.splice(i,1);miss(n);if(st!=="playing") return}
    }
    for(let i=parts.length-1;i>=0;i--){const p=parts[i];p.life-=dt;if(p.life<=0){parts.splice(i,1);continue}p.x+=p.vx*dt;p.y+=p.vy*dt;p.vy+=520*dt}
    for(let i=floats.length-1;i>=0;i--){const f=floats[i];f.life-=dt;f.y-=50*dt;if(f.life<=0) floats.splice(i,1)}
  }
  function rr(x,y,w,h,r){ctx.beginPath();ctx.roundRect(x,y,w,h,r)}
  function draw(){
    ctx.save();ctx.setTransform(K,0,0,K,0,0);
    const g=ctx.createLinearGradient(0,0,0,H);g.addColorStop(0,"#10040c");g.addColorStop(1,"#05020a");
    ctx.fillStyle=g;ctx.fillRect(0,0,W,H);
    for(let i=0;i<LN;i++){                                   // дорожки
      const h=hue(i)|0,x=i*LW;
      const lg=ctx.createLinearGradient(0,0,0,HY);lg.addColorStop(0,`hsla(${h},90%,50%,0)`);lg.addColorStop(1,`hsla(${h},90%,55%,${.07+glow[i]*.28})`);
      ctx.fillStyle=lg;ctx.fillRect(x+2,0,LW-4,HY+30);
      ctx.fillStyle="rgba(255,255,255,.05)";ctx.fillRect(x,0,1,H);
    }
    ctx.fillStyle="rgba(255,255,255,.035)";                  // бегущие линии ритма
    for(let y=grid-60;y<HY;y+=60) ctx.fillRect(0,y,W,1);
    // линия удара и приёмники
    ctx.fillStyle="rgba(255,255,255,.9)";ctx.fillRect(0,HY-1,W,2);
    for(let i=0;i<LN;i++){
      const h=hue(i)|0,x=i*LW;
      ctx.lineWidth=2;ctx.strokeStyle=`hsla(${h},100%,70%,${.45+glow[i]*.55})`;
      rr(x+10,HY-11,LW-20,22,9);ctx.stroke();
      if(glow[i]>0){ctx.fillStyle=`hsla(${h},100%,70%,${glow[i]*.5})`;rr(x+10,HY-11,LW-20,22,9);ctx.fill()}
      ctx.fillStyle=`hsla(${h},100%,85%,.75)`;ctx.font="800 15px 'Segoe UI',sans-serif";ctx.textAlign="center";ctx.fillText(LBL[i],x+LW/2,HY+42);
    }
    // ноты
    for(const n of notes){
      const h=hue(n.l)|0,x=n.l*LW+12,w=LW-24,y=n.y-9;
      ctx.fillStyle=`hsla(${h},100%,60%,.25)`;rr(x-5,y-5,w+10,28,12);ctx.fill();
      const ng=ctx.createLinearGradient(0,y,0,y+18);ng.addColorStop(0,`hsl(${h} 100% 82%)`);ng.addColorStop(1,`hsl(${h} 95% 56%)`);
      ctx.fillStyle=ng;rr(x,y,w,18,8);ctx.fill();
      ctx.fillStyle="rgba(255,255,255,.55)";rr(x+6,y+3,w-12,3,2);ctx.fill();
    }
    for(const p of parts){ctx.globalAlpha=Math.max(0,p.life/p.max);ctx.fillStyle=p.c;ctx.beginPath();ctx.arc(p.x,p.y,2.2,0,6.2832);ctx.fill()}
    ctx.globalAlpha=1;
    ctx.textAlign="center";
    for(const f of floats){ctx.globalAlpha=Math.min(1,f.life*2.5);ctx.fillStyle=f.col;ctx.font="900 16px 'Segoe UI',sans-serif";ctx.fillText(f.txt,f.x,f.y)}
    ctx.globalAlpha=1;
    if(combo>=5){ctx.fillStyle="rgba(255,255,255,.1)";ctx.font="900 74px 'Segoe UI',sans-serif";ctx.fillText(combo,W/2,H*.38);
      ctx.font="800 14px 'Segoe UI',sans-serif";ctx.fillStyle="rgba(255,255,255,.35)";ctx.fillText("КОМБО · ×"+mult(),W/2,H*.38+24)}
    if(flash>0){ctx.fillStyle=`rgba(255,40,60,${flash*.35})`;ctx.fillRect(0,0,W,H)}
    ctx.restore();
  }
  function showOver(){
    const m={
      ready:["Ритм","Ноты падают по четырём дорожкам. Жми D F J K (или стрелки, или кликай по дорожке), когда нота на белой линии. Чем точнее — тем больше очков, серия ×2…×4. 5 промахов — конец.","Играть"],
      paused:["Пауза","Трек играет дальше.","Продолжить"],
      over:["Игра окончена","Счёт: "+score+(newBest?" — новый рекорд!":""),"Ещё раз"]
    }[st];
    $("#rTitle").textContent=m[0];$("#rSub").textContent=m[1];btn.textContent=m[2];over.hidden=false;
  }
  function loop(now){
    if(st!=="playing") return;
    const dt=Math.min(.033,(now-last)/1000);last=now;
    update(dt);if(st!=="playing") return;
    draw();raf=requestAnimationFrame(loop);
  }
  function start(){
    if(st==="ready"||st==="over") reset();
    st="playing";over.hidden=true;btn.blur();
    last=performance.now();cancelAnimationFrame(raf);raf=requestAnimationFrame(loop);
  }
  function pause(){if(st!=="playing") return;st="paused";cancelAnimationFrame(raf);draw();showOver()}
  function end(){
    st="over";cancelAnimationFrame(raf);
    newBest=score>best;
    if(newBest){best=score;$("#rBest").textContent=best;HS.rhythm=best;hsSave()}
    draw();showOver();
  }
  function enter(){draw();showOver()}
  cv.addEventListener("pointerdown",e=>{
    if(st!=="playing") return;
    const r=cv.getBoundingClientRect();press(Math.max(0,Math.min(LN-1,Math.floor((e.clientX-r.left)/r.width*LN))));
  });
  btn.addEventListener("click",start);
  addEventListener("blur",()=>{if(!gm.hidden) pause()});
  document.addEventListener("visibilitychange",()=>{if(document.hidden&&!gm.hidden) pause()});
  document.addEventListener("keydown",e=>{
    if(gm.hidden||GM.cur!=="rhythm"||e.ctrlKey||e.metaKey||e.altKey) return;
    const c=e.code;
    if(c==="Escape"){M.close()}
    else if(c in KEYS){if(!e.repeat) press(KEYS[c])}
    else if(c==="Space"||c==="Enter"||c==="KeyP"){
      if(e.target===btn&&c!=="KeyP") return;
      if(!e.repeat){if(st==="playing") pause();else start()}
    }
    else return;
    e.preventDefault();e.stopPropagation();
  },true);
  reset();loadBest();
  return{enter,pause};
})();


/* ---------- казино на пульсы: общие помощники ---------- */
const CAS=(()=>{
  const rndU=()=>{try{const a=new Uint32Array(1);crypto.getRandomValues(a);return a[0]/4294967296}catch(e){return Math.random()}};
  function betUI(root,inp){
    root.addEventListener("click",e=>{
      const b=e.target.closest("[data-bq]");if(!b||root.classList.contains("busy")) return;
      const bal=ME.data.bal;let v=Math.max(0,Math.floor(+inp.value||0));
      switch(b.dataset.bq){case "half":v=Math.max(1,Math.floor(v/2));break;case "x2":v=v*2;break;case "p10":v+=10;break;case "p100":v+=100;break;case "p1k":v+=1000;break;case "max":v=bal;break;case "clr":v=0;break}
      inp.value=Math.max(0,Math.min(v,bal));
    });
  }
  function takeBet(inp,btn){
    const v=Math.floor(+inp.value||0),bal=ME.data.bal;
    const shake=()=>{if(!btn) return;btn.classList.remove("shake");void btn.offsetWidth;btn.classList.add("shake")};
    if(!(v>=1)){shake();toast("Поставь хотя бы 1 пульс",true);inp.focus();return 0}
    if(v>bal){shake();toast(`Не хватает пульсов: на балансе ${ME.fmtN(bal)}`,true);return 0}
    if(!ME.spend(v)){shake();return 0}
    try{QA.ev("bet",{v})}catch(e){}
    return v;
  }
  const betRow=(id,extra)=>`<div class="cbet"><label class="cbin"><svg class="coin" viewBox="0 0 64 64" aria-hidden="true"><use href="#pcoin"/></svg><input id="${id}" type="number" min="1" step="1" value="10" inputmode="numeric" aria-label="Ставка"></label>
    <div class="cbq"><button data-bq="half" type="button">½</button><button data-bq="x2" type="button">×2</button><button data-bq="p10" type="button">+10</button><button data-bq="p100" type="button">+100</button><button data-bq="p1k" type="button">+1к</button><button data-bq="max" type="button">Всё</button></div>${extra||""}</div>`;
  return{rndU,betUI,takeBet,betRow};
})();

/* ---------- «Рулетка»: красное ×2, чёрное ×2, зелёное ×14 ---------- */
const RU=(()=>{
  const pane=$("#pane-roul");
  pane.innerHTML=`<div class="gstats"><div>Баланс<b data-bal>0</b></div><div class="chist" id="ruHist" title="Последние выпадения"></div></div>
    <div class="ruw"><div class="rustrip" id="ruStrip"></div><i class="rumark"></i><i class="rushade l"></i><i class="rushade r"></i></div>
    <div class="cres" id="ruRes">Выбери ставку и цвет — колесо закрутится сразу</div>
    ${CAS.betRow("ruBet")}
    <div class="rubtns"><button class="rub r" data-c="r" type="button"><b>Красное</b><em>×2</em></button><button class="rub z" data-c="z" type="button"><b>Зелёное</b><em>×14</em></button><button class="rub k" data-c="k" type="button"><b>Чёрное</b><em>×2</em></button></div>
    <div class="ghint">7 красных, 7 чёрных и 1 зелёное поле · пульсы — игровая валюта приложения</div>`;
  const strip=$("#ruStrip"),res=$("#ruRes"),inp=$("#ruBet"),hist=$("#ruHist");
  const ORDER=[0,1,8,2,9,3,10,4,11,5,12,6,13,7,14];          // как на настоящем колесе: цвета чередуются
  const col=n=>n===0?"z":n<=7?"r":"k";
  const TW=72,REP=12;let pos=0,busy=false,pending=null,H=[];
  try{H=JSON.parse(localStorage.getItem("ruHist")||"[]")}catch(e){}
  strip.innerHTML=Array.from({length:ORDER.length*REP},(_,i)=>{const n=ORDER[i%ORDER.length];return `<i class="rut ${col(n)}">${n}</i>`}).join("");
  function paintHist(){hist.innerHTML=H.slice(-12).map(n=>`<i class="${col(n)}"></i>`).join("")}
  function place(idx,anim,dur){
    const w=strip.parentElement.clientWidth||400;
    const x=-(idx*TW+TW/2)+w/2;
    strip.style.transition=anim?`transform ${dur}ms cubic-bezier(.12,.6,.1,1)`:"none";
    strip.style.transform=`translateX(${x}px)`;
  }
  function settle(){
    if(!pending) return;const p=pending;pending=null;
    const n=p.n,c=col(n),win=c===p.c?p.v*(c==="z"?14:2):0;
    H.push(n);if(H.length>40) H=H.slice(-40);try{localStorage.setItem("ruHist",JSON.stringify(H))}catch(e){}
    paintHist();
    pos=ORDER.indexOf(n)+ORDER.length*2;place(pos,false);             // незаметно возвращаем ленту в начало
    if(win){ME.payout(win);res.innerHTML=`<span class="${c}">Выпало ${n}</span> — выигрыш <b>+${ME.fmtN(win)}</b>`;res.className="cres win";try{QA.ev("win",{game:"roul",c,v:p.v,win})}catch(e){}}
    else{res.innerHTML=`<span class="${c}">Выпало ${n}</span> — ставка ${ME.fmtN(p.v)} сгорела`;res.className="cres lose"}
    busy=false;pane.classList.remove("busy");
  }
  function spin(c,btn){
    if(busy) return;
    const v=CAS.takeBet(inp,btn);if(!v) return;
    busy=true;pane.classList.add("busy");
    const n=Math.floor(CAS.rndU()*15);pending={n,c,v};
    const target=ORDER.indexOf(n)+ORDER.length*(REP-3);
    res.className="cres";res.textContent=`Ставка ${ME.fmtN(v)} на ${c==="r"?"красное":c==="k"?"чёрное":"зелёное"}… крутим`;
    place(pos,false);void strip.offsetWidth;
    const jitter=(CAS.rndU()-.5)*TW*.7;
    const w=strip.parentElement.clientWidth||400,x=-(target*TW+TW/2+jitter)+w/2;
    strip.style.transition="transform 4800ms cubic-bezier(.12,.6,.1,1)";strip.style.transform=`translateX(${x}px)`;
    clearTimeout(spin.t);spin.t=setTimeout(settle,4900);
  }
  pane.addEventListener("click",e=>{const b=e.target.closest("[data-c]");if(b) spin(b.dataset.c,b)});
  CAS.betUI(pane,inp);
  function enter(){paintHist();if(!busy){pos=ORDER.length*2;requestAnimationFrame(()=>place(pos,false))}}
  function pause(){if(busy){clearTimeout(spin.t);settle()}}                 // закрыли окно во время прокрутки — результат всё равно засчитан
  addEventListener("resize",()=>{if(!busy) place(pos,false)});
  return{enter,pause};
})();

/* ---------- «Пульс»: множитель растёт, пока бьётся сердце — забери до остановки ---------- */
const PU=(()=>{
  const pane=$("#pane-pulse");
  pane.innerHTML=`<div class="gstats"><div>Баланс<b data-bal>0</b></div><div class="chist" id="puHist" title="На чём останавливалось сердце"></div></div>
    <div class="puw" id="puW"><canvas id="puc" width="1040" height="560"></canvas>
      <div class="puhud"><b id="puX">1.00×</b><span id="puS">Сделай ставку — сердце запустится</span></div></div>
    ${CAS.betRow("puBet",`<label class="cauto" title="Автоматически забрать на этом множителе (0 — выключено)">Авто<input id="puAuto" type="number" min="0" step="0.1" value="0">×</label>`)}
    <button class="pubtn" id="puGo" type="button">Поставить</button>
    <div class="ghint">Чем дольше держишь — тем больше выигрыш. Пробел — поставить / забрать</div>`;
  const cv=$("#puc"),ctx=cv.getContext("2d"),W=cv.width,Hh=cv.height,xEl=$("#puX"),sEl=$("#puS"),go=$("#puGo"),inp=$("#puBet"),auto=$("#puAuto"),hist=$("#puHist"),wrap=$("#puW");
  let lockT=0,st="idle",bet=0,crash=1,t0=0,mult=1,raf=0,beatT=0,beatN=0,trace=[],last=0,endT=0,H=[],cashed=0,flat=0,actx=null;
  try{H=JSON.parse(localStorage.getItem("puHist")||"[]")}catch(e){}
  const k=.14;                                              // рост: ×2 за ~5 с, ×10 за ~16 с
  const mAt=s=>Math.exp(k*s);
  function paintHist(){hist.innerHTML=H.slice(-10).map(v=>`<em class="${v>=2?"g":"r"}">${v.toFixed(2)}</em>`).join("")}
  function beep(f,d,v){
    if(ST.sounds===false) return;
    try{actx=actx||new (window.AudioContext||window.webkitAudioContext)();const o=actx.createOscillator(),g=actx.createGain();o.type="sine";o.frequency.value=f;
      g.gain.setValueAtTime(v||.05,actx.currentTime);g.gain.exponentialRampToValueAtTime(.0001,actx.currentTime+d);o.connect(g).connect(actx.destination);o.start();o.stop(actx.currentTime+d)}catch(e){}
  }
  function ecg(p){                                          // форма одного удара (p — фаза 0..1)
    const g=(c,w,a)=>a*Math.exp(-Math.pow((p-c)/w,2));
    return g(.18,.035,.12)-g(.33,.012,.18)+g(.36,.014,1)-g(.395,.016,.32)+g(.62,.06,.26);
  }
  function draw(now){
    ctx.clearRect(0,0,W,Hh);
    const red=st==="dead",hue=red?0:Math.max(0,140-Math.log2(mult)*38);
    // сетка монитора
    ctx.strokeStyle="rgba(255,255,255,.05)";ctx.lineWidth=2;ctx.beginPath();
    for(let x=0;x<W;x+=52){ctx.moveTo(x,0);ctx.lineTo(x,Hh)}for(let y=0;y<Hh;y+=52){ctx.moveTo(0,y);ctx.lineTo(W,y)}ctx.stroke();
    // линия
    const mid=Hh*.62,amp=Hh*.36;
    ctx.lineJoin="round";ctx.lineCap="round";
    const path=()=>{ctx.beginPath();trace.forEach((v,i)=>{const x=W-(trace.length-1-i)*4,y=mid-v*amp;i?ctx.lineTo(x,y):ctx.moveTo(x,y)})};
    ctx.strokeStyle=`hsla(${hue},95%,60%,.28)`;ctx.lineWidth=14;path();ctx.stroke();
    ctx.strokeStyle=`hsl(${hue},100%,${red?60:66}%)`;ctx.lineWidth=5;path();ctx.stroke();
    const lv=trace.length?trace[trace.length-1]:0;
    ctx.fillStyle="#fff";ctx.beginPath();ctx.arc(W-2,mid-lv*amp,7,0,6.283);ctx.fill();
    // сердце
    const ph=st==="run"?beatT:0,pump=st==="run"?1+.16*Math.exp(-ph*9):red?.86:1;
    const cx=W*.13,cy=Hh*.26,r=56*pump;
    ctx.save();ctx.translate(cx,cy);ctx.scale(r/30,r/30);
    ctx.shadowColor=red?"rgba(120,120,120,.4)":`hsla(${hue},100%,55%,.9)`;ctx.shadowBlur=red?0:24;
    ctx.fillStyle=red?"#555a63":`hsl(${hue<60?350:350-(hue-60)*.2} 90% 58%)`;
    ctx.beginPath();ctx.moveTo(0,24);ctx.bezierCurveTo(-42,0,-22,-34,0,-12);ctx.bezierCurveTo(22,-34,42,0,0,24);ctx.fill();ctx.restore();
    if(red){ctx.fillStyle="rgba(255,40,60,"+(.18*Math.max(0,1-(now-endT)/900))+")";ctx.fillRect(0,0,W,Hh)}
  }
  function step(dt){
    const bpm=st==="run"?Math.min(220,62+Math.log2(mult)*42):0;
    let v=0;
    if(st==="run"){beatT+=dt*bpm/60;if(beatT>=1){beatT-=1;beatN++;beep(880,.08,.04)}v=ecg(beatT)}
    else if(st==="dead"){v=(Math.random()-.5)*.012}
    else{beatT+=dt*1.05;if(beatT>=1) beatT-=1;v=ecg(beatT)*.35}
    for(let i=0;i<3;i++) trace.push(v);
    if(trace.length>W/4+4) trace.splice(0,trace.length-(W/4+4));
  }
  function frame(now){
    raf=requestAnimationFrame(frame);
    const dt=Math.min(.05,(now-last)/1000);last=now;
    if(st==="run"){
      const s=(now-t0)/1000;mult=mAt(s);
      if(mult>=crash){mult=crash;die(now)}
      else{
        xEl.textContent=mult.toFixed(2)+"×";
        const pot=Math.floor(bet*mult);sEl.textContent=`Забрать сейчас: ${ME.fmtN(pot)}`;go.querySelector("b").textContent=ME.fmtN(pot);
        const a=+auto.value||0;if(a>=1.01&&mult>=a) cash();
      }
    }
    step(dt);draw(now);
  }
  function die(now){
    st="dead";endT=now;H.push(crash);if(H.length>30) H=H.slice(-30);try{localStorage.setItem("puHist",JSON.stringify(H))}catch(e){}
    paintHist();beep(660,1.4,.06);
    xEl.textContent=crash.toFixed(2)+"×";wrap.classList.add("dead");wrap.classList.remove("live");
    sEl.textContent=`Сердце остановилось — ставка ${ME.fmtN(bet)} сгорела`;
    setIdle();lockT=performance.now()+1400;go.disabled=true;go.textContent="Сердце остановилось…";   // защита: клик «Забрать» не должен случайно поставить заново
    setTimeout(()=>{go.disabled=false;if(st!=="run") go.textContent="Поставить ещё"},1400);
  }
  function cash(){
    if(st!=="run") return;
    const m=mult,win=Math.floor(bet*m);cashed=m;st="out";
    ME.payout(win);
    try{QA.ev("win",{game:"pulse",m,v:bet,win})}catch(e){}
    xEl.textContent=m.toFixed(2)+"×";wrap.classList.remove("live");wrap.classList.add("won");
    sEl.innerHTML=`Забрал <b>+${ME.fmtN(win)}</b> на ${m.toFixed(2)}× · сердце остановилось бы на ${crash.toFixed(2)}×`;
    H.push(crash);if(H.length>30) H=H.slice(-30);try{localStorage.setItem("puHist",JSON.stringify(H))}catch(e){}
    paintHist();beep(1320,.25,.06);
    setIdle();
  }
  function setIdle(){pane.classList.remove("busy");go.classList.remove("cash");go.innerHTML="Поставить ещё"}
  function start(){
    if(st==="run"){cash();return}
    if(performance.now()<lockT) return;
    const v=CAS.takeBet(inp,go);if(!v) return;
    bet=v;const u=CAS.rndU();crash=Math.min(1000,Math.max(1,Math.floor(.96/(1-u)*100)/100));   // шанс дожить до ×m — 96 % / m
    st="run";t0=performance.now();mult=1;beatT=.5;wrap.classList.remove("dead","won");wrap.classList.add("live");
    pane.classList.add("busy");go.classList.add("cash");go.innerHTML=`Забрать <b>${ME.fmtN(v)}</b>`;
    sEl.textContent="Сердце бьётся…";
  }
  go.addEventListener("click",start);
  CAS.betUI(pane,inp);
  document.addEventListener("keydown",e=>{
    if($("#gm").hidden||GM.cur!=="pulse"||e.ctrlKey||e.metaKey||e.altKey) return;
    if(e.code==="Space"&&e.target.tagName!=="INPUT"){e.preventDefault();e.stopPropagation();if(!e.repeat) start()}
  },true);
  function enter(){paintHist();if(!raf){last=performance.now();raf=requestAnimationFrame(frame)}}
  function pause(){if(st==="run") cash();if(raf){cancelAnimationFrame(raf);raf=0}}   // закрыл окно во время игры — выигрыш забирается автоматически
  return{enter,pause};
})();


/* ---------- «Минки»: открывай клетки, обходи мины — чем больше мин и дальше прошёл, тем выше множитель ---------- */
const MI=(()=>{
  const pane=$("#pane-mines"),N=25;
  pane.innerHTML=`<div class="gstats"><div>Баланс<b data-bal>0</b></div><div class="mist"><span>Следующая клетка</span><b id="miNext">×1.00</b></div></div>
    <div class="miw"><div class="mig" id="miG">${Array.from({length:N},(_,i)=>`<button class="mic" data-i="${i}" type="button" disabled aria-label="Клетка ${i+1}"><i></i></button>`).join("")}</div>
      <div class="mihud" id="miHud"><b id="miX">×1.00</b><span id="miS">Выбери ставку и количество мин</span></div></div>
    <div class="mimn"><span>Мин на поле</span><div class="mich" id="miCh">${[1,3,5,10,15,20,24].map(n=>`<button data-mn="${n}" type="button"${n===3?' class="on"':""}>${n}</button>`).join("")}</div>
      <input id="miN" type="range" min="1" max="24" step="1" value="3" aria-label="Количество мин"><b id="miNv">3</b></div>
    ${CAS.betRow("miBet")}
    <button class="pubtn" id="miGo" type="button">Играть</button>
    <div class="ghint">Каждая открытая клетка без мины увеличивает выигрыш. Наткнулся на мину — ставка сгорела. Забрать можно в любой момент</div>`;
  const grid=$("#miG"),cells=[...grid.children],go=$("#miGo"),inp=$("#miBet"),rng=$("#miN"),nv=$("#miNv"),xEl=$("#miX"),sEl=$("#miS"),nxt=$("#miNext"),hud=$("#miHud");
  let st="idle",mines=3,bet=0,open=0,bomb=new Set();
  const mult=(k,m)=>{let r=.97;for(let i=0;i<k;i++) r*=(N-i)/(N-m-i);return Math.floor(r*100)/100};   // честная вероятность × 0,97
  const fx=v=>"×"+(v>=100?v.toFixed(0):v.toFixed(2));
  function paintM(){nv.textContent=mines;rng.value=mines;$("#miCh").querySelectorAll("button").forEach(b=>b.classList.toggle("on",+b.dataset.mn===mines));
    if(st!=="run"){nxt.textContent=fx(mult(1,mines));xEl.textContent=fx(1);sEl.textContent=`${mines} ${plural(mines,"мина","мины","мин")} — первая клетка даёт ${fx(mult(1,mines))}, все ${N-mines} — ${fx(mult(N-mines,mines))}`}}
  function setMines(n){if(st==="run") return;mines=Math.max(1,Math.min(24,n|0));paintM()}
  $("#miCh").addEventListener("click",e=>{const b=e.target.closest("[data-mn]");if(b) setMines(+b.dataset.mn)});
  rng.addEventListener("input",()=>setMines(+rng.value));
  function reset(){cells.forEach(c=>{c.className="mic";c.disabled=true;c.innerHTML="<i></i>"})}
  function start(){
    if(st==="run"){cash();return}
    const v=CAS.takeBet(inp,go);if(!v) return;
    bet=v;open=0;st="run";bomb=new Set();
    while(bomb.size<mines) bomb.add(Math.floor(CAS.rndU()*N));
    reset();cells.forEach(c=>c.disabled=false);
    pane.classList.add("busy");hud.className="mihud live";
    go.classList.add("cash");go.disabled=true;go.innerHTML="Открой клетку";
    xEl.textContent=fx(1);nxt.textContent=fx(mult(1,mines));sEl.textContent=`Ставка ${ME.fmtN(bet)} · мин: ${mines}`;
  }
  function revealAll(hit){
    cells.forEach((c,i)=>{if(c.classList.contains("ok")||i===hit) return;c.disabled=true;c.classList.add("rv",bomb.has(i)?"bm":"gm")});
  }
  function pick(i){
    if(st!=="run") return;const c=cells[i];if(c.classList.contains("ok")) return;
    if(bomb.has(i)){
      st="dead";c.classList.add("boom","bm");revealAll(i);hud.className="mihud dead";
      xEl.textContent="Мина!";sEl.textContent=`Ставка ${ME.fmtN(bet)} сгорела на ${open+1}-й клетке`;
      grid.classList.remove("shake");void grid.offsetWidth;grid.classList.add("shake");
      end();return;
    }
    open++;c.classList.add("ok");c.disabled=true;
    const m=mult(open,mines),win=Math.floor(bet*m);
    xEl.textContent=fx(m);sEl.textContent=`Открыто ${open} из ${N-mines} · забрать сейчас: ${ME.fmtN(win)}`;
    nxt.textContent=open<N-mines?fx(mult(open+1,mines)):"—";
    go.disabled=false;go.innerHTML=`Забрать <b>${ME.fmtN(win)}</b>`;
    if(open>=N-mines) cash();
  }
  function cash(){
    if(st!=="run") return;
    if(!open){ME.payout(bet);st="idle";reset();end();sEl.textContent="Ставка возвращена";return}   // ни одной клетки — ставка возвращается
    const m=mult(open,mines),win=Math.floor(bet*m);st="out";
    ME.payout(win);revealAll(-1);hud.className="mihud won";
    xEl.textContent=fx(m);sEl.innerHTML=`Забрал <b>+${ME.fmtN(win)}</b> · ${open} ${plural(open,"клетка","клетки","клеток")} при ${mines} ${plural(mines,"мине","минах","минах")}`;
    try{QA.ev("win",{game:"mines",m,v:bet,win,mines,open})}catch(e){}
    end();
  }
  function end(){pane.classList.remove("busy");go.classList.remove("cash");go.disabled=false;go.textContent="Играть ещё";cells.forEach(c=>c.disabled=true)}
  grid.addEventListener("click",e=>{const c=e.target.closest(".mic");if(c) pick(+c.dataset.i)});
  go.addEventListener("click",start);
  CAS.betUI(pane,inp);
  function enter(){paintM()}
  function pause(){if(st==="run") cash()}                    // закрыл окно — открытые клетки засчитываются
  return{enter,pause};
})();


/* ---------- Plinko: шарик падает сквозь штырьки в лунку с множителем ---------- */
const PL=(()=>{
  const pane=$("#pane-plinko");
  const TB={
    8:{low:[5.6,2.1,1.1,1,.5,1,1.1,2.1,5.6],mid:[13,3,1.3,.7,.4,.7,1.3,3,13],high:[29,4,1.5,.3,.2,.3,1.5,4,29]},
    12:{low:[10,3,1.6,1.4,1.1,1,.5,1,1.1,1.4,1.6,3,10],mid:[33,11,4,2,1.1,.6,.3,.6,1.1,2,4,11,33],high:[170,24,8.1,2,.7,.2,.2,.2,.7,2,8.1,24,170]},
    16:{low:[16,9,2,1.4,1.4,1.2,1.1,1,.5,1,1.1,1.2,1.4,1.4,2,9,16],mid:[110,41,10,5,3,1.5,1,.5,.3,.5,1,1.5,3,5,10,41,110],high:[1000,130,26,9,4,2,.2,.2,.2,.2,.2,2,4,9,26,130,1000]}
  };
  pane.innerHTML=`<div class="gstats"><div>Баланс<b data-bal>0</b></div><div class="chist" id="plHist" title="Последние множители"></div></div>
    <div class="plw"><canvas id="plc" width="1040" height="860"></canvas></div>
    <div class="plopt"><div class="mich" id="plRows"><span>Ряды</span>${[8,12,16].map(n=>`<button data-r="${n}" type="button"${n===12?' class="on"':""}>${n}</button>`).join("")}</div>
      <div class="mich" id="plRisk"><span>Риск</span>${[["low","Низкий"],["mid","Средний"],["high","Высокий"]].map(([k,n])=>`<button data-k="${k}" type="button"${k==="mid"?' class="on"':""}>${n}</button>`).join("")}</div></div>
    ${CAS.betRow("plBet")}
    <button class="pubtn" id="plGo" type="button">Бросить шарик</button>
    <div class="ghint">Можно бросать несколько шариков подряд. Чем выше риск — тем больше крайние множители и меньше центральные</div>`;
  const cv=$("#plc"),ctx=cv.getContext("2d"),W=cv.width,H=cv.height,inp=$("#plBet"),go=$("#plGo"),hist=$("#plHist");
  let rows=12,risk="mid",balls=[],raf=0,last=0,hits=[],flash={},H_=[];
  const geo=()=>{const s=Math.min((W*.97)/(rows+2),(H*.9)/(rows*.86+1.1)),top=H*.04;return{s,top,by:top+rows*s*.86+s*.25}};
  const bucketCol=m=>m>=10?"#ff3d5a":m>=3?"#ff7a2e":m>=1.4?"#ffb62e":m>=1?"#e9d23a":"#7ad06b";
  function busy(){return balls.length>0}
  function setOpt(){
    pane.querySelectorAll("#plRows button").forEach(b=>b.classList.toggle("on",+b.dataset.r===rows));
    pane.querySelectorAll("#plRisk button").forEach(b=>b.classList.toggle("on",b.dataset.k===risk));
  }
  pane.addEventListener("click",e=>{
    const r=e.target.closest("[data-r]"),k=e.target.closest("#plRisk [data-k]");
    if((r||k)&&busy()){toast("Дождись, пока упадут все шарики",true);return}
    if(r){rows=+r.dataset.r;setOpt()}else if(k){risk=k.dataset.k;setOpt()}
  });
  function drop(){
    const v=CAS.takeBet(inp,go);if(!v) return;
    const path=[];let right=0;
    for(let i=0;i<rows;i++){const d=CAS.rndU()<.5?0:1;path.push(d);right+=d}
    balls.push({v,path,right,i:0,t:0,rows,risk,dur:.13+CAS.rndU()*.03,trail:[]});
    if(!raf){last=performance.now();raf=requestAnimationFrame(frame)}
  }
  function land(b){
    const m=TB[b.rows][b.risk][b.right],win=Math.floor(b.v*m);
    if(win) ME.payout(win);
    flash[b.right]=1;H_.push(m);if(H_.length>12) H_.shift();
    hist.innerHTML=H_.slice(-8).map(x=>`<em class="${x>=1?"g":"r"}">×${x}</em>`).join("");
    hits.push({x:0,b:b.right,txt:(win>=b.v?"+":"")+ME.fmtN(win),good:m>=1,life:1});
    try{QA.ev("win",{game:"plinko",m,v:b.v,win})}catch(e){}
  }
  function pos(b,level,r){const g=geo();return{x:W/2+(r-level/2)*g.s,y:g.top+level*g.s*.86-g.s*.42}}
  function frame(now){
    raf=requestAnimationFrame(frame);
    const dt=Math.min(.05,(now-last)/1000);last=now;
    const g=geo();
    ctx.clearRect(0,0,W,H);
    // штырьки
    for(let i=0;i<rows;i++) for(let j=0;j<i+3;j++){
      const x=W/2+(j-(i+2)/2)*g.s,y=g.top+i*g.s*.86,k=i+":"+j,f=flash[k]||0;
      ctx.fillStyle=f?`rgba(255,255,255,${.6+f*.4})`:"rgba(255,255,255,.55)";ctx.beginPath();ctx.arc(x,y,Math.max(3,g.s*.09)*(1+f*.6),0,6.283);ctx.fill();
      if(f){flash[k]=Math.max(0,f-dt*4);if(!flash[k]) delete flash[k]}
    }
    // лунки
    const T=TB[rows][risk],bw=g.s*.92;
    T.forEach((m,k)=>{
      const x=W/2+(k-rows/2)*g.s,f=flash[k]||0,y=g.by+(f?Math.sin(f*Math.PI)*8:0);
      ctx.fillStyle=bucketCol(m);ctx.globalAlpha=.85+f*.15;
      const h=g.s*.62;ctx.beginPath();ctx.roundRect?ctx.roundRect(x-bw/2,y,bw,h,8):ctx.rect(x-bw/2,y,bw,h);ctx.fill();ctx.globalAlpha=1;
      ctx.fillStyle="#1a0d05";ctx.font=`900 ${Math.max(11,Math.min(22,g.s*.32))}px "Segoe UI",sans-serif`;ctx.textAlign="center";ctx.textBaseline="middle";
      ctx.fillText(m>=100?m.toFixed(0):String(m),x,y+h/2+1);
      if(f){flash[k]=Math.max(0,f-dt*2.2);if(!flash[k]) delete flash[k]}
    });
    // шарики
    for(let n=balls.length-1;n>=0;n--){
      const b=balls[n];b.t+=dt/b.dur;
      while(b.t>=1&&b.i<b.rows){
        b.t-=1;const r0=b.path.slice(0,b.i).reduce((a,c)=>a+c,0);flash[b.i+":"+(r0+1)]=1;b.i++;
      }
      if(b.i>=b.rows){land(b);balls.splice(n,1);continue}
      const r0=b.path.slice(0,b.i).reduce((a,c)=>a+c,0),r1=r0+b.path[b.i];
      const p0=pos(b,b.i,r0),p1=pos(b,b.i+1,r1),t=b.t;
      const x=p0.x+(p1.x-p0.x)*t,y=p0.y+(p1.y-p0.y)*t*t-Math.sin(Math.PI*t)*g.s*.28;
      b.trail.push([x,y]);if(b.trail.length>6) b.trail.shift();
      b.trail.forEach(([tx,ty],q)=>{ctx.globalAlpha=q/b.trail.length*.3;ctx.fillStyle="#ff4d6d";ctx.beginPath();ctx.arc(tx,ty,g.s*.16,0,6.283);ctx.fill()});
      ctx.globalAlpha=1;const gr=ctx.createRadialGradient(x-3,y-3,1,x,y,g.s*.2);gr.addColorStop(0,"#fff");gr.addColorStop(.4,"#ff7a8c");gr.addColorStop(1,"#d0103a");
      ctx.fillStyle=gr;ctx.beginPath();ctx.arc(x,y,g.s*.19,0,6.283);ctx.fill();
    }
    // всплывающие выигрыши
    for(let n=hits.length-1;n>=0;n--){
      const h=hits[n];h.life-=dt*.8;if(h.life<=0){hits.splice(n,1);continue}
      const x=W/2+(h.b-rows/2)*g.s;ctx.globalAlpha=h.life;ctx.fillStyle=h.good?"#7af0a0":"#ff8a9a";ctx.font=`900 26px "Segoe UI",sans-serif`;ctx.textAlign="center";
      ctx.fillText(h.txt,x,g.by-14-(1-h.life)*40);ctx.globalAlpha=1;
    }
    if(!balls.length&&!hits.length&&!Object.keys(flash).length){cancelAnimationFrame(raf);raf=0;draw0()}
  }
  function draw0(){if(!raf){last=performance.now();raf=requestAnimationFrame(frame)}}
  go.addEventListener("click",drop);
  CAS.betUI(pane,inp);
  document.addEventListener("keydown",e=>{
    if($("#gm").hidden||GM.cur!=="plinko"||e.ctrlKey||e.metaKey||e.altKey) return;
    if(e.code==="Space"&&e.target.tagName!=="INPUT"){e.preventDefault();e.stopPropagation();drop()}
  },true);
  function enter(){draw0()}
  function pause(){                                          // закрыли окно — шарики долетают мгновенно
    for(const b of balls.splice(0)) land(b);
  }
  return{enter,pause};
})();

/* ---------- HiLo: следующая карта выше или ниже. Двойка — младшая, туз — старший ---------- */
const HL=(()=>{
  const pane=$("#pane-hilo");
  const RK=["2","3","4","5","6","7","8","9","10","J","Q","K","A"],SU=[["♠","k"],["♥","r"],["♦","r"],["♣","k"]];
  pane.innerHTML=`<div class="gstats"><div>Баланс<b data-bal>0</b></div><div class="mist"><span>Множитель</span><b id="hlX">×1.00</b></div></div>
    <div class="hlw"><div class="hlrow" id="hlRow"></div><div class="hlcard" id="hlCard"></div><div class="hls" id="hlS">Двойка — самая младшая карта, туз — самый старший</div></div>
    <div class="hlbtns"><button class="hlb hi" data-h="hi" type="button" disabled><b id="hlHiN">Выше или равно</b><em id="hlHi">—</em></button>
      <button class="hlb skip" data-h="skip" type="button" disabled title="Сменить карту бесплатно">Пропустить</button>
      <button class="hlb lo" data-h="lo" type="button" disabled><b id="hlLoN">Ниже или равно</b><em id="hlLo">—</em></button></div>
    ${CAS.betRow("hlBet")}
    <button class="pubtn" id="hlGo" type="button">Начать</button>
    <div class="ghint">Каждая угаданная карта увеличивает множитель. Ошибся — ставка сгорела. Забрать можно после первой угаданной карты · ↑ ↓ — выбор, пробел — старт / забрать</div>`;
  const cardEl=$("#hlCard"),rowEl=$("#hlRow"),sEl=$("#hlS"),xEl=$("#hlX"),go=$("#hlGo"),inp=$("#hlBet"),hiE=$("#hlHi"),loE=$("#hlLo"),hiN=$("#hlHiN"),loN=$("#hlLoN");
  let st="idle",card=null,mult=1,bet=0,steps=0,trail=[];
  const draw=()=>({r:Math.floor(CAS.rndU()*13),s:Math.floor(CAS.rndU()*4)});
  /* ставки для карты r (0 — двойка … 12 — туз). На двойке «выше или равно» выигрывало бы всегда,
     на тузе — «ниже или равно», поэтому там ставка «выше» / «ниже» строгая, а вторая кнопка — «такая же» */
  function bets(r){
    if(r===0) return{hi:{n:"Выше",win:x=>x>0,p:12/13},lo:{n:"Такая же",win:x=>x===0,p:1/13}};
    if(r===12) return{hi:{n:"Такая же",win:x=>x===12,p:1/13},lo:{n:"Ниже",win:x=>x<12,p:12/13}};
    return{hi:{n:"Выше или равно",win:x=>x>=r,p:(13-r)/13},lo:{n:"Ниже или равно",win:x=>x<=r,p:(r+1)/13}};
  }
  const mOf=p=>Math.max(1.01,Math.floor(.97/p*100)/100);       // множитель всегда больше 1 — выигрыш только растёт
  const cardH=(c,cls)=>`<div class="hc ${SU[c.s][1]} ${cls||""}"><i>${RK[c.r]}</i><b>${SU[c.s][0]}</b><i class="bt">${RK[c.r]}</i></div>`;
  function paint(flip){
    cardEl.innerHTML=cardH(card,flip?"flip":"");
    rowEl.innerHTML=trail.slice(-7).map(t=>`<div class="hmini ${SU[t.c.s][1]} ${t.ok?"ok":"bad"}">${RK[t.c.r]}${SU[t.c.s][0]}<em>${t.dir==="hi"?"▲":t.dir==="lo"?"▼":"↻"}</em></div>`).join("");
    const B=bets(card.r);
    hiN.textContent=B.hi.n;loN.textContent=B.lo.n;
    hiE.textContent="×"+mOf(B.hi.p).toFixed(2)+" · "+Math.round(B.hi.p*100)+"%";
    loE.textContent="×"+mOf(B.lo.p).toFixed(2)+" · "+Math.round(B.lo.p*100)+"%";
    xEl.textContent="×"+mult.toFixed(2);
  }
  function btns(on){pane.querySelectorAll(".hlb").forEach(b=>b.disabled=!on)}
  function start(){
    if(st==="run"){cash();return}
    const v=CAS.takeBet(inp,go);if(!v) return;
    bet=v;mult=1;steps=0;trail=[];st="run";card=draw();
    pane.classList.add("busy");btns(true);go.classList.add("cash");go.disabled=true;go.textContent="Угадай следующую карту";
    sEl.textContent=`Ставка ${ME.fmtN(bet)} · выше или ниже?`;paint(true);
  }
  function guess(dir){
    if(st!=="run") return;
    const prev=card,nx=draw();
    if(dir==="skip"){trail.push({c:prev,dir,ok:true});card=nx;paint(true);return}
    const b=bets(prev.r)[dir],ok=b.win(nx.r);
    trail.push({c:prev,dir,ok});card=nx;
    if(!ok){
      st="dead";paint(true);xEl.textContent="×0";cardEl.classList.remove("lose");void cardEl.offsetWidth;cardEl.classList.add("lose");
      sEl.textContent=`Не угадал: ${RK[prev.r]} → ${RK[nx.r]} — ставка ${ME.fmtN(bet)} сгорела`;end();return;
    }
    mult=Math.round(mult*mOf(b.p)*100)/100;steps++;paint(true);
    const win=Math.floor(bet*mult);sEl.innerHTML=`Угадано ${steps} подряд · забрать сейчас: <b>${ME.fmtN(win)}</b>`;
    go.disabled=false;go.innerHTML=`Забрать <b>${ME.fmtN(win)}</b>`;
  }
  function cash(){
    if(st!=="run") return;
    if(!steps){ME.payout(bet);st="idle";end();sEl.textContent="Ставка возвращена";return}
    const win=Math.floor(bet*mult);st="out";ME.payout(win);
    sEl.innerHTML=`Забрал <b>+${ME.fmtN(win)}</b> · ${steps} ${plural(steps,"карта","карты","карт")} подряд, ×${mult.toFixed(2)}`;
    try{QA.ev("win",{game:"hilo",m:mult,v:bet,win})}catch(e){}
    end();
  }
  function end(){pane.classList.remove("busy");btns(false);go.classList.remove("cash");go.disabled=false;go.textContent="Играть ещё"}
  pane.addEventListener("click",e=>{const b=e.target.closest("[data-h]");if(b&&!b.disabled) guess(b.dataset.h)});
  go.addEventListener("click",start);
  CAS.betUI(pane,inp);
  document.addEventListener("keydown",e=>{
    if($("#gm").hidden||GM.cur!=="hilo"||e.target.tagName==="INPUT") return;
    if(e.key==="ArrowUp"&&st==="run"){e.preventDefault();e.stopPropagation();guess("hi")}
    else if(e.key==="ArrowDown"&&st==="run"){e.preventDefault();e.stopPropagation();guess("lo")}
    else if(e.code==="Space"){e.preventDefault();e.stopPropagation();if(!e.repeat) start()}
  },true);
  function enter(){if(!card){card=draw();paint()}}
  function pause(){if(st==="run") cash()}
  return{enter,pause,_t:{bets,mOf}};
})();

const M=(()=>{
  const gm=$("#gm"),panes={roul:$("#pane-roul"),pulse:$("#pane-pulse"),mines:$("#pane-mines"),plinko:$("#pane-plinko"),hilo:$("#pane-hilo")},games={roul:RU,pulse:PU,mines:MI,plinko:PL,hilo:HL};
  function show(name){
    games[GM.cur].pause();
    GM.cur=name;
    document.querySelectorAll(".gtab").forEach(b=>b.classList.toggle("on",b.dataset.g===name));
    for(const k in panes) panes[k].hidden=k!==name;
    games[name].enter();
  }
  function open(){
    gm.hidden=false;
    if(document.activeElement&&document.activeElement.blur) document.activeElement.blur();
    games[GM.cur].enter();
  }
  function close(){games[GM.cur].pause();gm.hidden=true}
  $("#gtabs").addEventListener("click",e=>{const b=e.target.closest(".gtab");if(!b) return;b.blur();show(b.dataset.g)});
  $("#gClose").addEventListener("click",close);
  gm.addEventListener("pointerdown",e=>{if(e.target===gm) close()});
  return{open,close};
})();
$("#navGame").addEventListener("click",M.open);

/* ---------- онлайн-комнаты: отдельная страница (меню → создать / подключиться) ---------- */
const RM=(()=>{
  const el=$("#rmp"),navBtn=$("#navRoom"),backB=$("#rmBack"),body=$("#rmBody");
  const pick=$("#rmPick"),title=$("#rmTitle"),artist=$("#rmArtist"),share=$("#rmShare"),go=$("#rmGo"),frame=$("#rmFrame");
  const jIn=$("#rmJIn"),jFrame=$("#rmJFrame");
  const ls=(k,v)=>{try{if(v===undefined) return localStorage.getItem(k);if(v===null) localStorage.removeItem(k);else localStorage.setItem(k,v)}catch(e){return null}};
  let room=null,res=null,view="menu",closeT=0,joined=ls("roomJoin")||"";
  try{room=JSON.parse(ls("roomHost")||"null")}catch(e){}
  if(room&&!(room.code&&room.key&&room.pid&&room.port)) room=null;
  const trackOf=()=>state.tracks.find(t=>String(t.id)===pick.value)||null;
  function fillPick(){
    const cur=pick.value||(state.editing!=null?String(state.editing):"");
    pick.innerHTML=`<option value="">— ввести вручную —</option>`+state.tracks.slice().sort((a,b)=>b.id-a.id).map(t=>`<option value="${t.id}">${esc(t.title)}${t.artist&&t.artist!=="—"?" — "+esc(t.artist):""}</option>`).join("");
    pick.value=state.tracks.some(t=>String(t.id)===cur)?cur:"";
    onPick(!title.value);
  }
  function onPick(fill){
    const t=trackOf();
    if(fill&&t){title.value=t.title;artist.value=t.artist==="—"?"":t.artist}
    const can=!!(t&&t.audio);
    if(!can) share.checked=false;else if(share.dataset.u!=="1") share.checked=true;
    share.closest(".rmck").classList.toggle("off",!can);
  }
  const payload=()=>{
    const t=trackOf();
    return {track:{title:title.value,artist:artist.value,album:t&&t.album||"",cover:t&&t.cover?String(t.id):"",audio:t&&t.audio?String(t.id):"",note:t&&t.beat?"prod. "+t.beat:""},share:share.checked,
      crit:CRIT,start:stStart(),dec:!!ST.decimals,name:$("#rmName").value.trim()||"Хозяин"};
  };
  function base(){
    const pub=($("#rmPub").value||"").trim().replace(/\/+$/,"");
    if(/^https?:\/\//i.test(pub)) return pub;
    return "http://"+((room.ips&&room.ips[0])||"localhost")+":"+room.port;
  }
  function paintLink(){
    if(!room) return;
    $("#rmLink").value=base()+"/r/"+room.code;
    $("#rmCmd").textContent="cloudflared tunnel --url http://localhost:"+room.port;
    $("#rmCmd2").textContent="ngrok http "+room.port;
  }
  function paint(){
    const live=!!room;
    $("#rmLive").hidden=!live;$("#rmSetupName").hidden=live;$("#rmLeave").hidden=!live;
    $("#rmTH").textContent=live?"Следующий трек":"Какой трек оцениваем";
    $("#rmCH").textContent=live?"Комната "+room.code:"Создать комнату";
    $("#rmCP").textContent=live?"Ты администратор. Отправь друзьям ссылку и оценивайте вместе":"Выбери трек и отправь друзьям ссылку";
    go.textContent=live?"Начать новый раунд с этим треком":"Создать комнату";
    $("#rmNote").hidden=live;
    if(live){
      $("#rmCode").textContent=room.code;
      $("#rmPub").value=ls("roomPub")||"";paintLink();
      const src="http://127.0.0.1:"+room.port+"/r/"+room.code+"#emb=1&k="+encodeURIComponent(room.key)+"&p="+encodeURIComponent(room.pid);
      if(frame.dataset.s!==src){frame.dataset.s=src;frame.src=src}
    }else{frame.removeAttribute("src");delete frame.dataset.s;$("#rmSave").hidden=true;res=null}
    paintMenu();
  }
  function paintMenu(){$("#rmResume").hidden=!room;if(room) $("#rmResCode").textContent=room.code}
  function paintJoin(){
    $("#rmJLive").hidden=!joined;
    if(joined){$("#rmJT").textContent="Подключено: "+joined;if(jFrame.dataset.s!==joined){jFrame.dataset.s=joined;jFrame.src=joined}}
    else{jFrame.removeAttribute("src");delete jFrame.dataset.s}
  }
  function show(v){
    view=v;
    const box={menu:$("#rmMenu"),create:$("#rmCreate"),join:$("#rmJoinV")};
    for(const k in box){box[k].hidden=k!==v;if(k===v){box[k].style.animation="none";void box[k].offsetWidth;box[k].style.animation=""}}
    backB.hidden=v==="menu";body.scrollTop=0;
    if(v==="menu") paintMenu();
    if(v==="create"){$("#rmName").value=(()=>{try{return ME.data.nick}catch(e){return ""}})()||ls("roomName")||"влад сантана";fillPick();paint();if(!room) setTimeout(()=>title.focus({preventScroll:true}),80)}
    if(v==="join"){paintJoin();if(!joined) setTimeout(()=>jIn.focus({preventScroll:true}),80)}
  }
  async function post(path,b){
    const r=await fetch(path,{method:"POST",body:JSON.stringify(b)});
    let j={};try{j=await r.json()}catch(e){}
    return {ok:r.ok,j};
  }
  go.addEventListener("click",async()=>{
    if(!title.value.trim()){toast("Введи название трека",true);title.focus();return}
    go.disabled=true;
    try{
      if(!room){
        const {ok,j}=await post("/api/room/create",payload());
        if(!ok){toast(j.msg||"Не удалось создать комнату",true);return}
        room=j;ls("roomHost",JSON.stringify(j));
        toast("Комната создана — отправь друзьям ссылку");
      }else{
        const b=payload();b.code=room.code;b.key=room.key;
        const {ok}=await post("/api/room/next",b);
        if(!ok){toast("Комната уже закрыта — создай новую",true);leave();return}
        res=null;$("#rmSave").hidden=true;
        toast("Новый раунд начался");
      }
      paint();body.scrollTop=0;
    }catch(e){toast("Не удалось связаться с программой",true)}
    finally{go.disabled=false}
  });
  function leave(){room=null;ls("roomHost",null);paint()}
  $("#rmLeave").addEventListener("click",()=>{if(confirm("Выйти из комнаты? Друзья смогут остаться в ней, пока программа открыта.")) leave()});
  pick.addEventListener("change",()=>onPick(true));
  $("#rmName").addEventListener("change",()=>{const v=$("#rmName").value.trim();if(v) ls("roomName",v)});
  share.addEventListener("change",()=>{share.dataset.u="1"});
  $("#rmPub").addEventListener("input",()=>{ls("roomPub",$("#rmPub").value.trim());paintLink()});
  $("#rmCopy").addEventListener("click",async()=>{
    const v=$("#rmLink").value;
    try{await navigator.clipboard.writeText(v)}catch(e){$("#rmLink").select();document.execCommand("copy")}
    toast("Ссылка скопирована");
  });
  /* подключение к чужой комнате */
  async function joinUrl(v){
    v=v.trim();if(!v) return "";
    if(/^https?:\/\//i.test(v)) return v.split("#")[0];
    if(/^[\w.-]+(:\d+)?\/r\/[A-Za-z0-9]{3,10}/.test(v)) return "http://"+v.split("#")[0];
    const code=v.replace(/[^A-Za-z0-9]/g,"").toUpperCase();
    if(code.length<3||code.length>10) return "";
    if(room&&room.code===code) return "own";
    try{const r=await fetch("/api/room/port",{method:"POST",body:"{}"});const j=await r.json();if(j.port) return "http://127.0.0.1:"+j.port+"/r/"+code}catch(e){}
    return "";
  }
  async function doJoin(){
    const u=await joinUrl(jIn.value);
    if(u==="own"){toast("Это твоя комната — открываю её");show("create");return}
    if(!u){toast("Не получилось распознать ссылку или код",true);jIn.focus();return}
    joined=u;ls("roomJoin",u);jIn.value="";paintJoin();toast("Подключаюсь к комнате…");
  }
  $("#rmJGo").addEventListener("click",doJoin);
  jIn.addEventListener("keydown",e=>{if(e.key==="Enter"){e.preventDefault();doJoin()}});
  $("#rmJLeave").addEventListener("click",()=>{joined="";ls("roomJoin",null);paintJoin()});
  /* сообщения из страницы своей комнаты (iframe) */
  addEventListener("message",e=>{
    if(!room||e.origin!=="http://127.0.0.1:"+room.port||e.source!==frame.contentWindow||!e.data||!e.data.room) return;
    if(e.data.type==="closed"){toast("Комната закрыта");leave()}
    else if(e.data.type==="result"){
      res=e.data;
      const t=trackOf();
      $("#rmSaveT").textContent="Общая оценка комнаты: "+(+res.total).toFixed(1)+" / 10";
      $("#rmSave").hidden=!t;
    }
  });
  $("#rmSaveB").addEventListener("click",()=>{
    const t=trackOf();if(!t||!res) return;
    if(!confirm(`Заменить твою оценку трека «${t.title}» на среднюю по комнате (${(+res.total).toFixed(1)})?`)) return;
    t.scores=res.avg.map(v=>ST.decimals?Math.round(v*10)/10:Math.round(v));t.total=calc(t.scores);delete t.unrated;
    persist();renderStats();renderLib();
    if(state.editing===t.id) fillEditor(t);
    toast("Средняя оценка записана в трек");
  });
  body.addEventListener("click",e=>{
    const c=e.target.closest(".wl-card[data-v]");if(c){show(c.dataset.v);return}
    if(e.target.closest("#rmResume")) show("create");
  });
  backB.addEventListener("click",()=>show("menu"));
  const isOpen=()=>!el.hidden&&!el.classList.contains("out");
  function open(){
    clearTimeout(closeT);
    if(isOpen()){show("menu");return}
    try{AP.close(true);TOP.close(true);RK.close(true);CT.close(true);LB.close(true)}catch(e){}try{PF.close(true);SHOP.close(true)}catch(e){}
    el.classList.remove("out");el.hidden=false;document.body.classList.add("pg");navBtn.classList.add("on");
    if(document.activeElement&&document.activeElement.blur) document.activeElement.blur();
    show("menu");
  }
  function close(keep){
    if(el.hidden||el.classList.contains("out")) return;
    if(keep!==true) document.body.classList.remove("pg");
    navBtn.classList.remove("on");el.classList.add("out");
    closeT=setTimeout(()=>{el.hidden=true;el.classList.remove("out")},280);   // iframe не выгружается — комната продолжает жить
  }
  $("#rmClose").addEventListener("click",close);
  el.addEventListener("pointerdown",e=>{if(e.target===el) close()});
  document.addEventListener("keydown",e=>{
    if(e.key!=="Escape"||!isOpen()) return;
    e.preventDefault();
    if(view!=="menu") show("menu");else close();
  });
  navBtn.addEventListener("click",()=>isOpen()?close():open());
  return{open,close,isOpen};
})();

/* ---------- приветствие после интро ---------- */
(()=>{
  const wl=$("#welcome"),appEl=$(".app");
  if(!wl||!appEl) return;
  const steps={hi:$("#wlHi"),pick:$("#wlPick"),alb:$("#wlAlb")};
  let cur="hi",busy=false,gone=false,shown=false;
  appEl.inert=true;
  function reveal(){
    if(shown) return;shown=true;
    wl.classList.add("show");
    setTimeout(()=>$("#wlStart").focus({preventScroll:true}),450);
  }
  const t=setTimeout(reveal,2650);                    // интро начинает таять на ~2.55 с
  new MutationObserver(()=>{if(document.body.classList.contains("skipped")){clearTimeout(t);reveal()}})
    .observe(document.body,{attributes:true,attributeFilter:["class"]});
  if(matchMedia("(prefers-reduced-motion: reduce)").matches){clearTimeout(t);reveal()}
  function go(name){
    if(busy||gone||name===cur) return;
    busy=true;
    const from=steps[cur],to=steps[name];
    from.classList.add("leave");
    setTimeout(()=>{
      from.classList.remove("on","leave");
      to.classList.add("on");cur=name;busy=false;
      const f=to.querySelector(".wl-card,.wl-in");if(f) f.focus({preventScroll:true});
    },240);
  }
  function finish(act){
    if(gone) return;gone=true;
    appEl.inert=false;
    appEl.classList.add("enter");
    wl.classList.add("out");
    setTimeout(()=>{wl.remove();appEl.classList.remove("enter")},1300);
    if(act) act();
  }
  function startSingle(){
    finish(()=>{newTrack();setTimeout(()=>titleEl.focus(),500)});
  }
  function startAlbum(upload){
    const name=$("#wlAlbName").value.trim(),art=$("#wlAlbArt").value.trim();
    finish(()=>{
      newTrack();
      state.album=name;albumEl.value=name;
      state.artist=art;artistEl.value=art;paintCover();
      if(upload) $("#pick").click();                    // открывается выбор файлов (мы ещё внутри клика)
      else setTimeout(()=>(name?titleEl:albumEl).focus(),500);
    });
  }
  $("#wlStart").addEventListener("click",()=>go("pick"));
  wl.addEventListener("click",e=>{
    const b=e.target.closest("[data-back]");
    if(b){go(b.dataset.back);return}
    const c=e.target.closest(".wl-card");
    if(c){if(c.dataset.k==="single") startSingle();else go("alb")}
  });
  $("#wlUp").addEventListener("click",()=>startAlbum(true));
  $("#wlManual").addEventListener("click",()=>startAlbum(false));
  document.addEventListener("keydown",e=>{
    if(gone) return;
    if(e.key==="Escape"){if(cur==="pick") go("hi");else if(cur==="alb") go("pick")}
    else if(e.key==="Enter"&&e.target.classList&&e.target.classList.contains("wl-in")){e.preventDefault();startAlbum(true)}
  });
})();

/* ---------- каталог релизов: поиск треков и исполнителей на площадках (без скачивания) ---------- */
const CT=(()=>{
  const el=$("#ct"),body=$("#ctBody"),navBtn=$("#navCat");
  const PLAYI='<svg class="ico" viewBox="0 0 24 24"><path d="M8 5v14l11-7z" fill="currentColor" stroke="none"/></svg>';
  const PAUSEI='<svg class="ico" viewBox="0 0 24 24"><path d="M7 5h3.5v14H7zM13.5 5H17v14h-3.5z" fill="currentColor" stroke="none"/></svg>';
  const ADDI='<svg class="ico" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>';
  let closeT=0,src="sc",stack=[],cur={t:"home",g:0},tk=0,tmr=0,pvKey="",view=null,TR=new Map(),LAST={};
  src=ST.catSrc==="it"?"it":"sc";
  const pv=new Audio();pv.preload="none";pv.volume=Math.max(0,Math.min(1,(+ST.pvVol)/100));
  addEventListener("st-change",()=>{pv.volume=Math.max(0,Math.min(1,(+ST.pvVol)/100))});
  const SRC={sc:"SoundCloud",it:"Apple Music",dz:"Deezer"};
  const bg=u=>u?`background-image:url(&quot;${esc(u)}&quot;)`:"";
  const inLib=k=>state.tracks.find(t=>t.cat===k);
  async function api(p){
    let r,j=null;
    try{r=await fetch("/api/cat/"+p)}catch(e){throw new Error("net")}
    try{j=await r.json()}catch(e){}
    if(!r.ok||!j||j.error) throw new Error((j&&j.error)||"net");
    return j;
  }
  const plural=(n,a,b,c)=>{const m=n%100,d=n%10;return m>10&&m<20?c:d===1?a:d>1&&d<5?b:c};
  const fans=n=>n>=1e6?(n/1e6).toFixed(1).replace(".0","")+" млн":n>=1e3?Math.round(n/1e3)+" тыс.":String(n);

  /* ----- слушатели: Яндекс Музыка (месячная аудитория) ----- */
  const YF=new Map();                       // имя -> {n, id} | null (нет данных)
  async function ymGet(name){
    const k=name.toLowerCase();
    if(YF.has(k)) return YF.get(k);
    let r=null;
    try{const j=await api("ymfans?name="+encodeURIComponent(name));if(j&&(j.listeners||j.id)) r={n:j.listeners||0,id:j.id||0}}catch(e){}
    YF.set(k,r);return r;
  }
  async function loadFans(my){
    const nodes=[...view.querySelectorAll("[data-yn]")].filter(x=>x.dataset.yn);
    let i=0;
    const worker=async()=>{
      while(i<nodes.length){
        const el=nodes[i++];if(my!==tk) return;
        const r=await ymGet(el.dataset.yn);
        if(my!==tk||!el.isConnected) continue;
        if(r&&r.n) el.textContent=fans(r.n)+(el.dataset.sfx||"");
      }
    };
    await Promise.all([worker(),worker(),worker()]);
  }

  /* ----- разметка ----- */
  const trRow=(t,i)=>{
    TR.set(t.k,t);const ex=inLib(t.k);
    return `<div class="ctr" style="--i:${i}" data-k="${esc(t.k)}">
      <div class="im" style="${bg(t.cover)}"></div>
      <div class="ctn"><b>${esc(t.title)}</b><span>${esc(t.artist)}${t.album?" · "+esc(t.album):""}</span></div>
      <div class="cta">
        ${t.preview?`<button class="ctp${pvKey===t.k&&!pv.paused?" on":""}" data-a="pv" type="button" title="Послушать фрагмент">${pvKey===t.k&&!pv.paused?PAUSEI:PLAYI}</button>`:""}
        <button class="ctadd" data-a="add" type="button" title="Добавить в медиатеку без оценки"${ex?" disabled":""}>${ADDI}</button>
        <button class="ctrate${ex&&!ex.unrated?" done":""}" data-a="rate" type="button">${ex&&!ex.unrated?"Открыть":"Оценить"}</button>
      </div></div>`;
  };
  const arCard=(a,i)=>`<button class="ctcard ar" style="--i:${i}" data-go="artist" data-id="${a.id}" data-src="${a.src}" type="button"><div class="im" style="${bg(a.pic)}">${a.pic?"":esc((a.name||"?").charAt(0).toUpperCase())}</div><b>${esc(a.name)}</b><span data-yn="${esc(a.name)}" data-sfx=" слушателей в месяц">${a.genre||"Исполнитель"}</span></button>`;
  const alCard=(a,i)=>`<button class="ctcard" style="--i:${i}" data-go="album" data-id="${a.id}" data-src="${a.src}" type="button"><div class="im" style="${bg(a.cover)}"></div><b>${esc(a.title)}</b><span>${esc(a.artist)}${a.year?" · "+a.year:""}</span></button>`;
  const loading=()=>`<div class="ctload"><i></i>Загружаю из ${SRC[src]}…</div>`;
  const failed=e=>`<div class="ctload">${e&&e.message==="net"?"Нет связи с каталогом. Проверь интернет.":"Каталог не ответил. Попробуй ещё раз."}<br><button data-a="retry" type="button">Повторить</button></div>`;
  const backBtn=()=>stack.length?`<button class="ctback" data-a="back" type="button"><svg class="ico" viewBox="0 0 24 24"><path d="M15 18l-6-6 6-6"/></svg>Назад</button>`:"";

  function shell(){
    body.innerHTML=`<div class="phead"><div><h2>Каталог релизов</h2><p>Ищи треки и исполнителей на площадках и оценивай без скачивания</p></div></div>
      <div class="ctbar"><label class="ctin"><svg class="ico" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg><input id="ctQ" type="text" placeholder="Трек, исполнитель или альбом" autocomplete="off" spellcheck="false"></label>
      <div class="ctseg" role="group" aria-label="Площадка"><button data-src="sc" type="button" class="${src==="sc"?"on":""}">SoundCloud</button><button data-src="it" type="button" class="${src==="it"?"on":""}">Apple Music</button></div></div>
      <div class="ctnote">SoundCloud — треки целиком (кроме платных SoundCloud Go+), Apple Music — 30-секундные превью. Ничего не скачивается на диск.</div>
      <div id="ctV"></div>`;
    view=$("#ctV");
  }
  const put=h=>{view.innerHTML=`<div class="ctv">${h}</div>`};

  /* ----- экраны ----- */
  async function show(v,keep){
    cur=v;const my=++tk;
    view.innerHTML=loading();
    try{
      if(v.t==="home"){
        const d=await api("home?src=sc&genre="+(v.g||0));if(my!==tk) return;
        LAST=d;TR.clear();
        put(`<div class="ctg">${[{id:0,name:"Все жанры"},...d.genres].map(g=>`<button class="rkc${(v.g||0)===g.id?" on":""}" data-g="${g.id}" type="button">${esc(g.name)}</button>`).join("")}</div>
          <div class="cth"><span>Популярные исполнители</span></div><div class="ctgrid">${d.artists.map(arCard).join("")}</div>
          <div class="cth"><span>Популярные треки</span></div><div class="ctl">${d.tracks.map(trRow).join("")}</div>`);
      }else if(v.t==="search"){
        const d=await api(`search?src=${src}&q=${encodeURIComponent(v.q)}`);if(my!==tk) return;
        LAST=d;TR.clear();
        if(!d.tracks.length&&!d.artists.length&&!d.albums.length){put(`<div class="ctload">Ничего не нашлось по запросу «${esc(v.q)}»</div>`);return}
        put((d.artists.length?`<div class="cth"><span>Исполнители</span></div><div class="ctgrid">${d.artists.map(arCard).join("")}</div>`:"")
          +(d.albums.length?`<div class="cth"><span>Альбомы</span></div><div class="ctgrid">${d.albums.map(alCard).join("")}</div>`:"")
          +(d.tracks.length?`<div class="cth"><span>Треки</span></div><div class="ctl">${d.tracks.map(trRow).join("")}</div>`:""));
      }else if(v.t==="artist"){
        const d=await api(`artist?src=${v.src}&id=${v.id}`);if(my!==tk) return;
        LAST=d;TR.clear();const a=d.artist;
        put(`${backBtn()}<div class="cthead ar"><div class="im" style="${bg(a.picBig||a.pic)}"></div><div><h2>${esc(a.name||v.name||"Исполнитель")}</h2>
          <p><span data-yn="${esc(a.name||v.name||"")}" data-sfx=" слушателей в месяц · "></span>${d.albums.length} ${plural(d.albums.length,"релиз","релиза","релизов")} · ${SRC[v.src]}</p>
          <div class="bt">${d.top.length?`<button class="pri" data-a="addtop" type="button">Добавить топ-треки</button>`:""}<button data-a="plat" type="button">Открыть на площадке</button></div></div></div>
          ${d.top.length?`<div class="cth"><span>Популярные треки</span></div><div class="ctl">${d.top.map(trRow).join("")}</div>`:""}
          ${d.albums.length?`<div class="cth"><span>Альбомы и синглы</span></div><div class="ctgrid">${d.albums.map(alCard).join("")}</div>`:""}`);
      }else if(v.t==="album"){
        const d=await api(`album?src=${v.src}&id=${v.id}`);if(my!==tk) return;
        LAST=d;TR.clear();const a=d.album;
        put(`${backBtn()}<div class="cthead"><div class="im" style="${bg(a.coverBig||a.cover)}"></div><div><h2>${esc(a.title)}</h2>
          <p><a href="#" data-go="artist" data-id="${a.artistId}" data-src="${v.src}" style="color:#fff;text-decoration:none;font-weight:700">${esc(a.artist)}</a>${a.year?" · "+a.year:""}${d.genre?" · "+esc(d.genre):""} · ${d.tracks.length} ${plural(d.tracks.length,"трек","трека","треков")}</p>
          <div class="bt"><button class="pri" data-a="addalbum" type="button">Добавить альбом целиком</button><button data-a="plat" type="button">Открыть на площадке</button></div></div></div>
          <div class="cth"><span>Треклист</span></div><div class="ctl">${d.tracks.map(trRow).join("")}</div>`);
      }
      if(my===tk) loadFans(my);
    }catch(e){if(my===tk) view.innerHTML=failed(e)}
    if(!keep) body.scrollTop=0;
  }
  function go(v){stack.push(cur);show(v)}
  function back(){const v=stack.pop();if(v) show(v)}
  const runQuery=()=>{
    const q=($("#ctQ").value||"").trim();stack=[];
    show(q?{t:"search",q}:{t:"home",g:0});
  };

  /* ----- действия с треками ----- */
  function stopPv(){try{pv.pause()}catch(e){}pvKey=""}
  function togglePv(t){
    if(pvKey===t.k&&!pv.paused){pv.pause();return}
    try{AU.pause()}catch(e){}
    pvKey=t.k;pv.src="/api/cat/stream?u="+encodeURIComponent(t.preview);
    pv.play().catch(()=>{toast("Не удалось воспроизвести превью",true);pvKey="";repaintBtns()});
  }
  function repaintBtns(){
    body.querySelectorAll(".ctr").forEach(r=>{
      const b=r.querySelector(".ctp");if(!b) return;
      const on=r.dataset.k===pvKey&&!pv.paused;
      b.classList.toggle("on",on);b.innerHTML=on?PAUSEI:PLAYI;
    });
  }
  ["play","pause","ended"].forEach(ev=>pv.addEventListener(ev,repaintBtns));
  async function rate(t){
    const proxy=u=>u?"/api/cat/img?u="+encodeURIComponent(u):"";
    const ex=inLib(t.k);
    if(ex){
      stopPv();loadTrack(ex.id);
      if(!ex.cover&&(t.coverBig||t.cover)){                 // трек добавлен без обложки — подтягиваем её из каталога
        state.catCov=t.coverBig||t.cover;
        state.coverPrev=proxy(t.cover||t.coverBig);paintCover();
        saveCatCover(ex.id,t.k,state.catCov);
        catCover(t,()=>state.editing===ex.id&&!state.coverBlob&&!state.cover);
      }
      return}
    let genre=t.genre||"";
    if(!genre&&t.src==="dz"&&t.albumId){
      toast("Загружаю данные трека…");
      try{genre=(await api("genre?album="+t.albumId)).genre||""}catch(e){}
    }
    stopPv();if(PAGE_CLOSE) PAGE_CLOSE();
    newTrack();
    state.artist=t.artist;state.title=t.title;state.genre=genre;state.album=t.album||"";
    artistEl.value=state.artist;titleEl.value=state.title;genreEl.value=state.genre;albumEl.value=state.album;
    state.stream=t.preview?{url:t.preview,src:t.src,link:t.link||"",name:`${t.artist} — ${t.title}`}:null;
    state.cat=t.k;state.catAlb=t.albumId||null;AU.sync();
    state.catCov=t.coverBig||t.cover||null;                    // по этой ссылке сервер сохранит обложку при оценке
    state.coverPrev=proxy(t.cover||t.coverBig);paintCover();     // обложка видна сразу
    main.scrollTo({top:0,behavior:"smooth"});
    catCover(t,()=>state.cat===t.k&&state.editing===null&&!state.coverBlob);
  }
  /* полноразмерная обложка для сохранения: пробуем большую, потом обычную */
  async function catCover(t,still){
    for(const u of [t.coverBig,t.cover]){
      if(!u) continue;
      for(let tryN=0;tryN<2;tryN++){
        try{
          const r=await fetch("/api/cat/img?u="+encodeURIComponent(u));
          if(!r.ok) continue;
          const b=await r.blob();if(!b.size||!still()) return;
          state.coverBlob=b;state.coverBlobUrl=URL.createObjectURL(b);paintCover();return;
        }catch(e){}
      }
    }
  }
  const mkRec=(t,id,extra)=>({id,artist:t.artist||"—",title:t.title,genre:t.genre||"",scores:[5,5,5,5,5],total:5,unrated:true,cat:t.k,catAlb:t.albumId||null,
    ...(t.preview?{stream:{url:t.preview,src:t.src,link:t.link||"",name:`${t.artist} — ${t.title}`}}:{}),...extra});
  async function saveCovers(list){          // обложки скачивает сам сервер — по две за раз, с повтором при сбое
    let i=0;
    const worker=async()=>{while(i<list.length){const [rec,url]=list[i++];await saveCatCover(rec.id,rec.cat,url)}};
    await Promise.all([worker(),worker()]);
  }
  function addMany(list,opt){
    opt=opt||{};
    const fresh=list.filter(t=>!inLib(t.k));
    if(!fresh.length){toast("Эти треки уже в медиатеке");return}
    const base=Date.now();let albumId=null;
    if(opt.album){
      const same=state.tracks.find(t=>t.albumId&&low(t.album)===low(opt.album));
      albumId=same?same.albumId:base;
    }
    const pairs=fresh.map((t,i)=>{
      const rec=mkRec(t,base+i,{...(opt.genre&&!t.genre?{genre:opt.genre}:{}),...(opt.album?{album:opt.album,albumId}:{...(t.album?{}:{})})});
      state.tracks.push(rec);return[rec,t.coverBig||t.cover]});
    if(opt.album){state.sort="album";$("#sort").textContent="По альбомам"}
    persist();renderStats();renderLib();saveCovers(pairs);
    toast(`Добавлено: ${fresh.length} ${plural(fresh.length,"трек","трека","треков")}. Оцени их в медиатеке`);
    repaintAll();
  }
  function repaintAll(){
    body.querySelectorAll(".ctr").forEach(r=>{
      const t=TR.get(r.dataset.k);if(!t) return;
      const ex=inLib(t.k),add=r.querySelector(".ctadd"),rt=r.querySelector(".ctrate");
      if(add) add.disabled=!!ex;
      if(rt){rt.textContent=ex&&!ex.unrated?"Открыть":"Оценить";rt.classList.toggle("done",!!(ex&&!ex.unrated))}
    });
  }

  function closeMenu(){body.querySelectorAll(".ctmenu").forEach(m=>m.remove())}
  function openMenu(btn,t){
    const had=btn.parentNode.querySelector(".ctmenu");closeMenu();if(had) return;
    const L=fullLinks(t.artist,t.title,t.link),m=document.createElement("div");
    m.className="ctmenu";
    m.innerHTML=`<a href="${esc(L.y)}" target="_blank" rel="noopener">Яндекс Музыка</a><a href="${esc(L.v)}" target="_blank" rel="noopener">VK Музыка</a>${L.p?`<a href="${esc(L.p)}" target="_blank" rel="noopener">${SRC[t.src]||"Площадка"}</a>`:""}`;
    btn.parentNode.append(m);
  }
  /* ----- события ----- */
  body.addEventListener("click",e=>{
    if(!e.target.closest(".ctmenu")&&!e.target.closest('[data-a="full"]')) closeMenu();
    const sg=e.target.closest(".ctseg button");
    if(sg){src=sg.dataset.src;setST({catSrc:src});
      body.querySelectorAll(".ctseg button").forEach(b=>b.classList.toggle("on",b===sg));
      if(($("#ctQ").value||"").trim()) runQuery();return}
    const gc=e.target.closest(".rkc[data-g]");
    if(gc){stack=[];show({t:"home",g:+gc.dataset.g});return}
    const g=e.target.closest("[data-go]");
    if(g){e.preventDefault();
      const s2=g.dataset.src||src;
      go(g.dataset.go==="artist"?{t:"artist",id:g.dataset.id,src:s2}:{t:"album",id:g.dataset.id,src:s2});return}
    const a=e.target.closest("[data-a]");if(!a) return;
    const k=a.closest(".ctr")&&a.closest(".ctr").dataset.k,t=k&&TR.get(k);
    switch(a.dataset.a){
      case "pv":if(t) togglePv(t);break;
      case "rate":if(t) rate(t);break;
      case "add":if(t) addMany([t]);break;
      case "back":back();break;
      case "retry":show(cur,true);break;
      case "plat":{
        if(cur.t==="album"&&LAST.album){const al=LAST.album;
          PLAT.open({kind:"album",artist:al.artist,title:al.title,link:al.link||"",src:al.src||cur.src})}
        else if(LAST.artist){const ar=LAST.artist,nm=ar.name||cur.name||"",y=YF.get(nm.toLowerCase());
          PLAT.open({kind:"artist",artist:nm,title:"",link:ar.link||"",src:ar.src||cur.src,
            direct:y&&y.id?[{n:"Яндекс Музыка · страница исполнителя",c:"#ffcc00",u:"https://music.yandex.ru/artist/"+y.id}]:[]})}
        break}
      case "addtop":addMany(LAST.top||[]);break;
      case "addalbum":addMany(LAST.tracks||[],{album:LAST.album&&LAST.album.title,genre:LAST.genre});break;
    }
  });
  body.addEventListener("input",e=>{if(e.target.id==="ctQ"){clearTimeout(tmr);tmr=setTimeout(runQuery,420)}});
  body.addEventListener("keydown",e=>{if(e.target.id==="ctQ"&&e.key==="Enter"){clearTimeout(tmr);runQuery()}});

  const isOpen=()=>!el.hidden&&!el.classList.contains("out");
  function open(){
    clearTimeout(closeT);
    if(isOpen()) return;
    el.classList.remove("out");TOP.close(true);AP.close(true);RK.close(true);LB.close(true);try{RM.close(true)}catch(e){}try{PF.close(true);SHOP.close(true)}catch(e){}
    stack=[];cur={t:"home",g:0};shell();show(cur);
    el.hidden=false;document.body.classList.add("pg");navBtn.classList.add("on");
    if(document.activeElement&&document.activeElement.blur) document.activeElement.blur();
    setTimeout(()=>{const q=$("#ctQ");if(q&&isOpen()) q.focus({preventScroll:true})},350);
  }
  function close(keep){
    if(el.hidden||el.classList.contains("out")) return;
    stopPv();
    if(keep!==true) document.body.classList.remove("pg");
    navBtn.classList.remove("on");el.classList.add("out");
    closeT=setTimeout(()=>{el.hidden=true;el.classList.remove("out");body.innerHTML="";TR.clear()},280);
  }
  $("#ctClose").addEventListener("click",close);
  el.addEventListener("pointerdown",e=>{if(e.target===el) close()});
  document.addEventListener("keydown",e=>{
    if(e.key!=="Escape"||!isOpen()) return;
    e.preventDefault();
    if(stack.length) back();else close();
  });
  navBtn.addEventListener("click",()=>isOpen()?close():open());
  return{open,close,isOpen,stopPv};
})();

/* ---------- страницы «Синглы» (исполнители → треки) и «Альбомы» (альбом → треки → оценка) ---------- */
const LB=(()=>{
  const el=$("#lb"),body=$("#lbBody"),back=$("#lbBack"),pS=$("#plSingles"),pA=$("#plAlbums"),pE=$("#plEps");
  let view="albums",curAlb=null,closeT=0,dragId=null,curKind="albums";
  const plural=(n,a,b,c)=>{const m=n%100,d=n%10;return m>10&&m<20?c:d===1?a:d>1&&d<5?b:c};
  const cnt=n=>n+" "+plural(n,"трек","трека","треков");
  const nAlb=n=>n+" "+plural(n,"альбом","альбома","альбомов");
  const nArt=n=>n+" "+plural(n,"исполнитель","исполнителя","исполнителей");
  const singles=()=>state.tracks.filter(t=>!t.albumId);
  const colls=()=>{const ids=new Set();state.tracks.forEach(t=>{if(t.albumId) ids.add(t.albumId)});return [...ids].map(albumInfo).filter(Boolean)};
  const isEP=a=>a.n>=2&&a.n<=6;                     // EP — от 2 до 6 треков
  const albums=()=>colls().filter(a=>!isEP(a));
  const eps=()=>colls().filter(isEP);
  const albOf=k=>colls().find(x=>String(x.id)===String(k));
  const mode=()=>view==="singles"||view==="artist"?"singles":curKind;
  const isOpen=()=>!el.hidden&&!el.classList.contains("out");
  const key=n=>String(n||"").trim().toLowerCase();
  const hasArt=(t,k)=>k===""?!artistNames(t).length:artistNames(t).some(n=>key(n)===k);
  function singleArtists(){
    const m=new Map();
    singles().forEach(t=>{const ns=artistNames(t);(ns.length?ns:[""]).forEach(n=>{const k=key(n);if(!m.has(k)) m.set(k,{key:k,name:n,items:[]});m.get(k).items.push(t)})});
    return [...m.values()].map(g=>{const r=g.items.filter(isRated);return{...g,avg:r.length?r.reduce((s,t)=>s+t.total,0)/r.length:0,best:r.length?Math.max(...r.map(t=>t.total)):0}})
      .sort((a,b)=>(!a.key)-(!b.key)||b.avg-a.avg||a.name.localeCompare(b.name,"ru"));
  }
  function plates(){
    $("#plSn").textContent=cnt(singles().length);
    $("#plAn").textContent=nAlb(albums().length);
    $("#plEn").textContent=eps().length+" EP";
  }
  function paint(html,keep){
    const sc=body.scrollTop;body.classList.remove("en");body.innerHTML=html;
    if(keep){body.scrollTop=sc;return}
    void body.offsetWidth;body.classList.add("en");body.scrollTop=0;
  }
  const artistsOf=a=>[...new Set(a.items.flatMap(artistNames))];
  const artLine=a=>{const r=artistsOf(a);return r.length?(r.length>2?r.slice(0,2).join(", ")+" и др.":r.join(", ")):"без исполнителя"};
  const albCover=a=>{const u=albCovUrl(a.id);if(u) return `background:url(${u}) center/cover no-repeat #222`;
    const ct=a.items.find(t=>t.cover);return ct?coverCss(ct):`background:${coverBg(artistsOf(a)[0]||"",a.name)}`};
  const albHas=a=>!!(albCov(a.id)||a.items.some(t=>t.cover));
  const UP='<svg class="ico" viewBox="0 0 24 24"><path d="M6 15l6-6 6 6"/></svg>',DOWN='<svg class="ico" viewBox="0 0 24 24"><path d="M6 9l6 6 6-6"/></svg>';
  const PEN='<svg class="ico" viewBox="0 0 24 24"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>';
  function trackRow(t,i,o){
    o=o||{};
    const sub=o.sub!==undefined?o.sub:`${esc(t.artist)}${t.beat?' <em class="pb">prod. '+esc(t.beat)+'</em>':""}${t.genre?" · "+esc(t.genre):""}`;
    return `<div class="item" style="--i:${i+2}" data-id="${t.id}" tabindex="0"${o.mv?' draggable="true"':""}>
      <div class="cv" style="${coverCss(t)}">${hasCov(t)?"":NOTE}</div>
      <div class="tx"><b>${o.num?`<i class="tno">${i+1}</i>`:""}${esc(t.title)}</b><span>${sub}</span></div>
      ${t.unrated?'<div class="sc un" title="Ещё не оценён">—</div>':`<div class="sc" style="color:${rgCol(t.total)}">${fmt(t.total)}</div>`}
      ${o.mv?`<div class="mv"><button type="button" data-mv="-1" title="Выше">${UP}</button><button type="button" data-mv="1" title="Ниже">${DOWN}</button></div>`:'<div class="lk"></div>'}</div>`;
  }
  const empty=(t,p)=>`<div class="lbe"><b>${t}</b>${p}</div>`;
  const setNav=()=>{back.hidden=view==="albums"||view==="singles";pS.classList.toggle("on",isOpen()&&mode()==="singles");pA.classList.toggle("on",isOpen()&&mode()==="albums");pE.classList.toggle("on",isOpen()&&mode()==="eps")};

  function showAlbums(kind){
    if(kind) curKind=kind;
    view="albums";curAlb=null;
    const ep=curKind==="eps";
    const list=(ep?eps():albums()).sort((a,b)=>(!a.done)-(!b.done)||b.avg-a.avg||a.name.localeCompare(b.name,"ru"));
    const full=list.filter(a=>a.complete).length;
    const cards=list.map((a,i)=>`<div class="pcrd" style="--i:${i+1};--hh:${hueFor(a.done?a.avg:5).toFixed(0)}" data-k="${esc(a.id)}" tabindex="0" role="button">
      <div class="lbcv" style="${albCover(a)}">${albHas(a)?"":NOTE}${a.done?`<span class="pbadge" style="color:${rgCol(a.avg)}">${fmt(a.avg)}</span>`:""}</div>
      <div class="pn"><b title="${esc(a.name)}">${esc(a.name)}</b><span>${esc(artLine(a))} · ${a.complete?cnt(a.n):`оценено ${a.done} из ${a.n}`}</span></div></div>`).join("");
    paint(`<div class="phead" style="--i:0"><div><div class="eyebrow">Медиатека</div><h2>${ep?"EP":"Альбомы"}</h2>
      <p>${list.length?`${ep?list.length+" EP":nAlb(list.length)} · оценено полностью: ${full}${ep?" · EP — релизы от 2 до 6 треков":""}`:"Пока пусто"}</p></div></div>`
      +(list.length?`<div class="pgrid">${cards}</div>`:ep?empty("EP пока нет","Загрузи от 2 до 6 треков сразу — они сохранятся как EP"):empty("Альбомов пока нет","Загрузи 7 и больше треков сразу или впиши название альбома — он появится здесь")));
    setNav();
  }
  function showAlbum(k,keep){
    const a=albOf(k);
    if(!a){showAlbums();return}
    view="album";curAlb=a.id;
    const items=a.items.slice().sort(byOrd);
    paint(`<div class="ahead" style="--i:0"><div class="lbcv big acv" data-acv="1" style="${albCover(a)}" title="Нажми, чтобы сменить обложку альбома">${albHas(a)?"":NOTE}<div class="covh"><svg class="ico" viewBox="0 0 24 24"><path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="4"/></svg><span>${isEP(a)?"Обложка EP":"Обложка альбома"}</span></div>${albCov(a.id)?'<button class="covx" data-acx="1" type="button" title="Убрать обложку"><svg class="ico" viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg></button>':""}</div>
      <div><div class="eyebrow">${isEP(a)?"EP":"Альбом"}<button class="ren" type="button" title="Изменить название">${PEN}</button></div><h3 id="lbTitle">${esc(a.name)}</h3>
      <p>${esc(artLine(a))} · ${cnt(a.n)} · ${a.complete?`общая <b style="color:${rgCol(a.avg)}">${fmt(a.avg)}</b>`:a.done?`оценено ${a.done} из ${a.n}, пока <b style="color:${rgCol(a.avg)}">${fmt(a.avg)}</b>`:`оценено 0 из ${a.n}`}</p>
      ${a.done?`<button class="ashb" data-ash="${a.id}" type="button"><svg class="ico" viewBox="0 0 24 24"><path d="M4 12v7a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-7"/><path d="M16 6l-4-4-4 4"/><path d="M12 2v13"/></svg>Поделиться ${isEP(a)?"EP":"альбомом"}</button>`:""}</div></div>
      <div class="alh" style="--i:1">Треки <span class="hint">— нажми, чтобы изменить оценку; перетаскивай или двигай стрелками, чтобы поменять порядок</span></div>`
      +items.map((t,i)=>trackRow(t,i,{num:1,mv:items.length>1})).join(""),keep);
    setNav();
  }
  let sSort="score";
  function showSingles(keep){
    view="singles";
    const sc=t=>t.unrated?-1:t.total;
    const by={score:(a,b)=>sc(b)-sc(a)||b.id-a.id,new:(a,b)=>b.id-a.id,
      alpha:(a,b)=>a.title.localeCompare(b.title,"ru",{sensitivity:"base",numeric:true})};
    const list=singles().sort(by[sSort]||by.score),n=list.length;
    const cards=list.map((t,i)=>`<div class="pcrd trk" style="--i:${Math.min(i,24)+1};--hh:${hueFor(t.unrated?5:t.total).toFixed(0)}" data-id="${t.id}" role="button" tabindex="0" title="Открыть «${esc(t.title)}»">
      <div class="lbcv" style="${coverCss(t)}">${hasCov(t)?"":NOTE}${t.unrated?"":`<span class="pbadge" style="color:${rgCol(t.total)}">${fmt(t.total)}</span>`}</div>
      <div class="pn"><b title="${esc(t.title)}">${esc(t.title)}</b><span>${esc(t.artist)}${t.beat?' · <em class="pb">prod. '+esc(t.beat)+'</em>':""}${t.genre?" · "+esc(t.genre):""}</span></div></div>`).join("");
    const sb=(k,l)=>`<button class="psb${sSort===k?" on":""}" data-ss="${k}" type="button">${l}</button>`;
    paint(`<div class="phead" style="--i:0"><div><div class="eyebrow">Медиатека</div><h2>Синглы</h2>
      <p>${n?`${cnt(n)} · нажми на трек, чтобы открыть его оценку`:"Пока пусто"}</p></div>`
      +(n>1?`<div class="psort" role="group" aria-label="Сортировка">${sb("score","По оценке")}${sb("new","Недавние")}${sb("alpha","По алфавиту")}</div>`:"")+`</div>`
      +(n?`<div class="pgrid">${cards}</div>`:empty("Синглов пока нет","Треки без альбома попадут сюда")),keep);
    setNav();
  }
  function showArtist(k){
    const all=state.tracks.filter(t=>isRated(t)&&hasArt(t,k)).sort((a,b)=>b.total-a.total||b.id-a.id);
    const nm=(all[0]&&artistNames(all[0]).find(n=>key(n)===k))||"";
    view="artist";
    const sg=all.filter(t=>!t.albumId),al=all.filter(t=>t.albumId);
    const avg=all.length?all.reduce((s,t)=>s+t.total,0)/all.length:0,best=all.length?all[0].total:0;
    let i=0;
    const rows=(arr,fn)=>arr.map(t=>trackRow(t,i++,fn?{sub:fn(t)}:{})).join("");
    paint(`<div class="ahead" style="--i:0">${AP.avatar(nm,true)}
      <div><div class="eyebrow">Исполнитель</div><h3>${nm?esc(nm):"Без исполнителя"}</h3>
      <p>${cnt(all.length)} · средняя <b style="color:${rgCol(avg)}">${fmt(avg)}</b> · лучший <b style="color:${rgCol(best)}">${fmt(best)}</b></p></div></div>`
      +(sg.length?`<div class="alh" style="--i:1">Синглы</div>`+rows(sg):"")
      +(al.length?`<div class="alh" style="--i:1">Из альбомов</div>`+rows(al,t=>`«${esc(t.album||"Альбом")}»${t.beat?' · <em class="pb">prod. '+esc(t.beat)+'</em>':""}${t.genre?" · "+esc(t.genre):""}`):"")
      +(all.length?"":empty("Оценённых треков нет","")));
    setNav();
  }

  /* ----- обложка альбома ----- */
  async function setAlbCover(f){
    const a=albOf(curAlb);if(!a) return;
    try{
      const blob=await toJpeg(f,800);
      const r=await fetch(`/api/img/album/${a.id}`,{method:"POST",body:blob});
      if(!r.ok) throw new Error(r.status);
      const v=Date.now();state.tracks.forEach(t=>{if(t.albumId===a.id) t.albCover=v});
      persist();ALBC.map=null;renderLib();showAlbum(a.id,true);
      if(state.albumId===a.id) paintCover();
      toast("Обложка альбома сохранена — она показывается у треков без своей обложки");
    }catch(e){toast("Не удалось сохранить обложку",true)}
  }
  function removeAlbCover(){
    const a=albOf(curAlb);if(!a) return;
    delImg("album",a.id);
    state.tracks.forEach(t=>{if(t.albumId===a.id) delete t.albCover});
    persist();ALBC.map=null;renderLib();showAlbum(a.id,true);
    if(state.albumId===a.id) paintCover();
    toast("Обложка альбома убрана");
  }
  /* ----- переименование альбома ----- */
  function rename(a,v){
    if(state.tracks.some(t=>t.albumId&&t.albumId!==a.id&&low(t.album)===low(v))){toast("Альбом с таким названием уже есть",true);showAlbum(a.id,true);return}
    state.tracks.forEach(t=>{if(t.albumId===a.id) t.album=v});
    if(state.albumId===a.id){state.album=v;albumEl.value=v}        // открытый в редакторе трек этого альбома
    persist();renderLib();showAlbum(a.id,true);toast("Название альбома изменено");
  }
  function startRename(){
    const a=albOf(curAlb),h=$("#lbTitle");
    if(!a||!h||view!=="album") return;
    const inp=document.createElement("input");inp.className="ren-in";inp.value=a.name;inp.maxLength=80;inp.setAttribute("aria-label","Название альбома");
    h.replaceWith(inp);inp.focus();inp.select();
    let done=false;
    const fin=ok=>{
      if(done) return;done=true;
      const v=inp.value.trim();
      if(ok&&v&&v!==a.name) rename(a,v);else showAlbum(a.id,true);
    };
    inp.addEventListener("keydown",e=>{
      e.stopPropagation();
      if(e.key==="Enter"){e.preventDefault();fin(true)}
      else if(e.key==="Escape"){e.preventDefault();fin(false)}
    });
    inp.addEventListener("blur",()=>fin(true));
  }

  /* ----- порядок треков ----- */
  const ids=()=>{const a=albOf(curAlb);return a?a.items.slice().sort(byOrd).map(t=>t.id):[]};
  function applyOrder(list){
    const a=albOf(curAlb);if(!a) return;
    const m=new Map(a.items.map(t=>[t.id,t]));
    list.forEach((id,i)=>{const t=m.get(id);if(t) t.ord=i+1});
    persist();renderLib();showAlbum(a.id,true);
  }
  function move(id,d){
    const l=ids(),i=l.indexOf(id),j=i+d;
    if(i<0||j<0||j>=l.length) return;
    [l[i],l[j]]=[l[j],l[i]];applyOrder(l);
  }
  const clearIns=()=>body.querySelectorAll(".ins-a,.ins-b,.dg").forEach(x=>x.classList.remove("ins-a","ins-b","dg"));
  body.addEventListener("dragstart",e=>{
    const it=e.target.closest&&e.target.closest(".item[draggable]");
    if(!it||view!=="album"){return}
    dragId=+it.dataset.id;it.classList.add("dg");
    try{e.dataTransfer.effectAllowed="move";e.dataTransfer.setData("text/plain","track")}catch(x){}
  });
  body.addEventListener("dragover",e=>{
    if(dragId==null) return;
    e.preventDefault();
    body.querySelectorAll(".ins-a,.ins-b").forEach(x=>x.classList.remove("ins-a","ins-b"));
    const it=e.target.closest(".item[draggable]");
    if(it&&+it.dataset.id!==dragId){const r=it.getBoundingClientRect();it.classList.add(e.clientY>r.top+r.height/2?"ins-a":"ins-b")}
  });
  body.addEventListener("drop",e=>{
    if(dragId==null) return;
    e.preventDefault();
    const it=e.target.closest(".item[draggable]"),me=dragId;dragId=null;
    if(!it||+it.dataset.id===me){clearIns();return}
    const r=it.getBoundingClientRect(),after=e.clientY>r.top+r.height/2;
    const l=ids().filter(x=>x!==me),at=l.indexOf(+it.dataset.id);
    l.splice(at+(after?1:0),0,me);applyOrder(l);
  });
  body.addEventListener("dragend",()=>{dragId=null;clearIns()});

  function open(m){
    clearTimeout(closeT);
    AP.close(true);TOP.close(true);RK.close(true);CT.close(true);try{RM.close(true)}catch(e){}try{PF.close(true);SHOP.close(true)}catch(e){}
    el.classList.remove("out");el.hidden=false;document.body.classList.add("pg");
    if(m==="singles") showSingles();else showAlbums(m==="eps"?"eps":"albums");
    if(document.activeElement&&document.activeElement.blur) document.activeElement.blur();
  }
  function close(keep){
    if(el.hidden||el.classList.contains("out")) return;
    if(keep!==true) document.body.classList.remove("pg");
    el.classList.add("out");pS.classList.remove("on");pA.classList.remove("on");pE.classList.remove("on");
    closeT=setTimeout(()=>{el.hidden=true;el.classList.remove("out")},280);
  }
  const goBack=()=>{if(view==="artist") showSingles();else showAlbums(curKind)};
  pS.addEventListener("click",()=>isOpen()&&mode()==="singles"?close():open("singles"));
  pA.addEventListener("click",()=>isOpen()&&mode()==="albums"?close():open("albums"));
  pE.addEventListener("click",()=>isOpen()&&mode()==="eps"?close():open("eps"));
  back.addEventListener("click",goBack);
  $("#lbClose").addEventListener("click",close);
  el.addEventListener("pointerdown",e=>{if(e.target===el) close()});
  body.addEventListener("click",e=>{
    if(e.target.closest(".ren")){startRename();return}
    if(e.target.closest("[data-acx]")){removeAlbCover();return}
    if(e.target.closest("[data-acv]")){pickImage(setAlbCover);return}
    const mv=e.target.closest("[data-mv]");
    if(mv){const it=mv.closest(".item");if(it) move(+it.dataset.id,+mv.dataset.mv);return}
    const ss=e.target.closest("[data-ss]");
    if(ss){if(ss.dataset.ss!==sSort){sSort=ss.dataset.ss;showSingles(true)}return}
    const tc=e.target.closest(".pcrd.trk");
    if(tc){loadTrack(+tc.dataset.id);return}
    const c=e.target.closest(".pcrd");
    if(c){if(c.dataset.ar!==undefined) showArtist(c.dataset.ar);else{const a=albOf(c.dataset.k);if(a) curKind=isEP(a)?"eps":"albums";showAlbum(c.dataset.k)}return}
    const it=e.target.closest(".item");
    if(it) loadTrack(+it.dataset.id);          // закрывает страницу и открывает трек в редакторе; играющая музыка не прерывается
  });
  body.addEventListener("keydown",e=>{
    if(e.key==="Enter"&&(e.target.classList.contains("pcrd")||e.target.classList.contains("item"))) e.target.click();
  });
  document.addEventListener("keydown",e=>{
    if(e.key!=="Escape"||!isOpen()) return;
    e.preventDefault();
    if(view==="album"||view==="artist") goBack();else close();
  });
  plates();
  return{open,close,isOpen,plates};
})();

/* ---------- настройки ---------- */
const SET=(()=>{
  const el=$("#st"),body=$("#stBody"),navBtn=$("#navSet");
  let closeT=0,reT=0;
  const swatch=h=>`linear-gradient(135deg,hsl(${h} 100% 62%) 0%,hsl(${(h+28)%360} 85% 42%) 55%,hsl(${(h+352)%360} 60% 9%) 100%)`;
  function fmt(k,v){return k==="glowSize"?Math.round(v)+" px":k==="start"?(ST.decimals?(+v).toFixed(1):String(Math.round(+v))):k==="hue"?Math.round(v)+"°":Math.round(v)+"%"}
  const tx=(t,d)=>`<span class="stt"><b>${t}</b>${d?`<em>${d}</em>`:""}</span>`;
  const tog=(k,t,d)=>`<label class="strow">${tx(t,d)}<input class="sw" type="checkbox" data-k="${k}"${ST[k]?" checked":""}></label>`;
  const sl=(k,t,d,min,max,step)=>`<div class="stsl"><div class="top">${tx(t,d)}<span class="sv" data-v="${k}">${fmt(k,ST[k])}</span></div><input class="sr" type="range" data-k="${k}" min="${min}" max="${max}" step="${step}" value="${ST[k]}"></div>`;
  const KN={ShiftLeft:"Shift (левый)",ShiftRight:"Shift (правый)",ControlLeft:"Ctrl (левый)",ControlRight:"Ctrl (правый)",AltLeft:"Alt (левый)",AltRight:"Alt (правый)",Space:"Пробел",Tab:"Tab",CapsLock:"Caps Lock",Enter:"Enter",Backquote:"` / Ё"};
  function keyName(c){
    if(!c) return "Не назначена";if(KN[c]) return KN[c];
    let m=/^Key([A-Z])$/.exec(c);if(m) return m[1];
    m=/^Digit(\d)$/.exec(c);if(m) return m[1];
    m=/^Numpad(\w+)$/.exec(c);if(m) return "Num "+m[1];
    return c;
  }
  let capOn=false;
  function onCap(e){
    e.preventDefault();e.stopImmediatePropagation();
    if(e.code==="Escape"){stopCap();return}
    if(!e.code) return;
    setST({intKey:e.code});stopCap();
  }
  function stopCap(){
    if(!capOn) return;capOn=false;window.removeEventListener("keydown",onCap,true);
    const b=body.querySelector(".keybtn");
    if(b){b.classList.remove("rec");b.textContent=keyName(ST.intKey);b.blur()}
  }
  function startCap(b){
    if(capOn) return;capOn=true;b.classList.add("rec");b.textContent="Нажми клавишу… (Esc — отмена)";
    window.addEventListener("keydown",onCap,true);
  }
  const later=()=>{clearTimeout(reT);reT=setTimeout(()=>{try{updateScore()}catch(e){}try{renderLib()}catch(e){}},150)};
  function build(){
    const keep=body.scrollTop;
    const th=THEMES.map(t=>`<button class="thm${ST.theme===t.id?" on":""}" data-t="${t.id}" type="button" style="--sw:${swatch(t.h)}"><i></i><b>${t.n}</b><em>${t.t}</em></button>`).join("")
      +`<button class="thm${ST.theme==="custom"?" on":""}" data-t="custom" type="button" style="--sw:linear-gradient(90deg,#ff3b3b,#ffd23b,#3bff7a,#3bd5ff,#7a3bff,#ff3bd5)"><i></i><b>Свой цвет</b><em>Выбери любой оттенок ползунком</em></button>`;
    const ds=DESIGNS.map(d=>`<button class="dsn${ST.design===d.id?" on":""}" data-d="${d.id}" type="button"><i class="pv pv-${d.id}"><s></s><s></s><s></s></i><b>${d.n}</b><em>${d.t}</em></button>`).join("");
    body.innerHTML=`<div class="stsec" style="margin-top:8px">Дизайн</div><div class="thg">${ds}</div>
      <div class="stsec">Цвет</div><div class="thg">${th}</div>
      <div class="stsl"><div class="top">${tx("Свой оттенок","Двигай — приложение перекрасится целиком")}<span class="sv" data-v="hue">${fmt("hue",stHue())}</span></div><input class="sr hue" type="range" data-k="hue" min="0" max="359" step="1" value="${Math.round(stHue())}"></div>
      ${tog("cycle","Плавное переливание цвета","Оттенок фона медленно плывёт примерно за 5 минут")}
      ${sl("panelA","Плотность панелей","Меньше — больше фона просвечивает сквозь панели",30,95,1)}
      <div class="stsec">Фон и эффекты</div>
      ${tog("eco","Режим экономии","Выключает все фоновые анимации. Для слабых ПК и работы от батареи")}
      ${tog("aurora","Северное сияние","Плавные световые занавесы на фоне")}
      ${tog("twinkle","Мерцающие звёзды","Тихое мерцание по всему экрану")}
      ${tog("warp","Летящие звёзды и кометы","Звёздный полёт на заднем плане")}
      ${tog("neb","Туманность","Цветные облака, медленно плывущие по фону")}
      <div class="stsec">Курсор</div>
      ${tog("glow","Свечение под курсором","Мягкий свет, который идёт за указателем")}
      ${sl("glowSize","Размер свечения","",40,200,5)}
      ${tog("clickFx","Искры при клике","Короткая вспышка в месте клика")}
      ${tog("trail","Искры за курсором","След из искр при движении мыши (по умолчанию выключен)")}
      <div class="stsec">Оценивание</div>
      ${sl("start","Стартовое значение ползунков","С какой оценки начинается каждая новая оценка",0,10,ST.decimals?.5:1)}
      ${tog("decimals","Десятичные оценки","Выключено — критерии ставятся только целыми числами (в том числе в онлайн-комнате). Включи, чтобы ставить, например, 7.3")}
      ${sl("pvVol","Громкость превью в каталоге","Для 30-секундных фрагментов в «Каталоге релизов»",0,100,5)}
      <div class="strow" style="cursor:default">${tx("Клавиша «только целые»","Зажми её и кликни/тяни ползунок критерия — оценка встанет на целое число. Нужна, только если включены десятичные оценки")}<div class="keyb"><button type="button" class="keybtn">${keyName(ST.intKey)}</button><button type="button" class="keyx" title="Убрать клавишу">×</button></div></div>
      <div class="strow" style="cursor:default">${tx("Площадка каталога","Какая площадка открывается в каталоге по умолчанию")}<div class="stseg"><button type="button" data-v="sc" class="${ST.catSrc!=="it"?"on":""}">SoundCloud</button><button type="button" data-v="it" class="${ST.catSrc==="it"?"on":""}">Apple Music</button></div></div>
      <div class="stsec">Запуск и выход</div>
      ${tog("intro","Заставка при запуске","Анимация с логотипом. Действует со следующего запуска")}
      ${tog("outro","Анимация при выходе","Если выключить, приложение закроется сразу")}
      ${tog("sounds","Звуки заставки и выхода","Синтезированная музыка при запуске и закрытии")}
      <div class="stsec">Обновления</div>
      <div id="updBox"></div>
      <div class="stsec">Данные</div>
      <div class="strow" style="cursor:default">${tx("Экспорт оценок","Таблица CSV со всеми треками и баллами — откроется в Excel")}<button type="button" class="keybtn stcsv" style="min-width:0">Скачать CSV</button></div>
      <button class="strst" type="button">Сбросить все настройки</button>
      <div class="stnote">Настройки хранятся в папке TrackRater рядом с оценками.</div>`;
    try{UPD.paintSet()}catch(e){}
    body.scrollTop=keep;
  }
  body.addEventListener("input",e=>{
    const r=e.target.closest("input.sr");if(!r) return;
    const k=r.dataset.k,v=+r.value;
    if(k==="hue"){setST({theme:"custom",hue:v});body.querySelectorAll(".thm").forEach(b=>b.classList.toggle("on",b.dataset.t==="custom"));later()}
    else setST({[k]:v});
    const sv=body.querySelector(`[data-v="${k}"]`);if(sv) sv.textContent=fmt(k,v);
  });
  body.addEventListener("change",e=>{const c=e.target.closest("input.sw");if(c){setST({[c.dataset.k]:c.checked});if(c.dataset.k==="decimals"){later();build()}}});
  body.addEventListener("click",e=>{
    const kb=e.target.closest(".keybtn");
    if(kb){startCap(kb);return}
    if(e.target.closest(".keyx")){stopCap();setST({intKey:""});build();return}
    if(e.target.closest(".stcsv")){exportCsv();return}
    const dz=e.target.closest(".dsn");
    if(dz){setST({design:dz.dataset.d});later();build();toast("Дизайн: "+DESIGNS.find(x=>x.id===dz.dataset.d).n);return}
    const t=e.target.closest(".thm");
    if(t){const id=t.dataset.t,th=THEMES.find(x=>x.id===id);
      setST(id==="custom"?{theme:"custom",hue:stHue()}:{theme:id,cycle:th.cy});later();build();return}
    const sg=e.target.closest(".stseg button");
    if(sg){setST({catSrc:sg.dataset.v});build();return}
    if(e.target.closest(".strst")){setST(Object.assign({},ST_DEF));later();build()}
  });
  const isOpen=()=>!el.hidden&&!el.classList.contains("out");
  function open(){
    clearTimeout(closeT);if(isOpen()) return;
    stopCap();build();el.classList.remove("out");el.hidden=false;navBtn.classList.add("on");
    try{CT.stopPv()}catch(e){}
    if(document.activeElement&&document.activeElement.blur) document.activeElement.blur();
  }
  function close(){
    if(el.hidden||el.classList.contains("out")) return;
    el.classList.add("out");navBtn.classList.remove("on");
    closeT=setTimeout(()=>{el.hidden=true;el.classList.remove("out")},220);
  }
  navBtn.addEventListener("click",()=>isOpen()?close():open());
  $("#stClose").addEventListener("click",close);
  el.addEventListener("pointerdown",e=>{if(e.target===el) close()});
  document.addEventListener("keydown",e=>{if(e.key==="Escape"&&isOpen()){e.preventDefault();e.stopImmediatePropagation();close()}},true);
  return{open,close,isOpen};
})();

/* ---------- плавное закрытие приложения ---------- */
const OUT=(()=>{
  let on=false;
  function sound(){
    const AC=window.AudioContext||window.webkitAudioContext;
    if(!AC||matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    try{
      const c=new AC(),t=c.currentTime,m=c.createGain();
      m.gain.setValueAtTime(0,t);m.gain.linearRampToValueAtTime(.5,t+.1);m.gain.linearRampToValueAtTime(0,t+3);m.connect(c.destination);
      const tone=(T,f,d,v,to)=>{const o=c.createOscillator(),g=c.createGain();o.type="triangle";o.frequency.setValueAtTime(f,t+T);
        if(to) o.frequency.exponentialRampToValueAtTime(to,t+T+d);
        g.gain.setValueAtTime(.0001,t+T);g.gain.exponentialRampToValueAtTime(v,t+T+.03);g.gain.exponentialRampToValueAtTime(.0001,t+T+d);
        o.connect(g);g.connect(m);o.start(t+T);o.stop(t+T+d+.05)};
      [329.63,246.94,196,164.81].forEach((f,i)=>tone(.7+i*.28,f,1.4,.1));        // нисходящие ноты, пока проявляется логотип
      tone(.8,82.41,2,.14);tone(.8,123.47,2,.06);                                  // мягкий низкий аккорд
      tone(2.2,60,.8,.22,30);                                                      // глухой удар, когда гаснет свет
      setTimeout(()=>{try{c.close()}catch(e){}},3600);
    }catch(e){}
  }
  function finish(){
    try{navigator.sendBeacon("/api/quit")}catch(e){}
    try{fetch("/api/quit",{method:"POST",keepalive:true}).catch(()=>{})}catch(e){}
    try{window.close()}catch(e){}
  }
  function go(){
    if(on) return;on=true;
    try{if(PAGE_CLOSE) PAGE_CLOSE()}catch(e){}
    try{AU.pause()}catch(e){}
    if(document.activeElement&&document.activeElement.blur) document.activeElement.blur();
    const quick=ST.outro===false;
    if(!quick) document.body.classList.add("bye");
    if(!quick&&ST.sounds!==false) sound();
    setTimeout(finish,quick?150:(matchMedia("(prefers-reduced-motion: reduce)").matches?400:3100));
  }
  /* Esc → «Вы уверены?» → плавное закрытие. Если Esc нужен чему-то другому (страница, игра, приветствие) — окно не показываем */
  const qc=$("#qc"),yes=$("#qYes"),no=$("#qNo");
  let blocked=false,qT=0;
  const busy=()=>{
    const vis=el=>el&&!el.hidden;
    return !!(document.getElementById("intro")||document.getElementById("welcome")||on
      ||vis($("#pm"))||vis($("#lb"))||vis($("#tp"))||vis($("#rk"))||vis($("#ct"))||vis($("#gm"))||vis($("#rmp"))||vis($("#pf"))||vis($("#shop"))||vis($("#rwd"))||vis($("#fpk"))||vis($("#nwk"))||vis($("#qa"))||vis($("#shr"))||vis($("#emb"))||vis($("#plat"))||vis($("#bc"))||vis($("#st")));
  };
  const qOpen=()=>!qc.hidden&&!qc.classList.contains("out");
  function ask(){
    clearTimeout(qT);qc.classList.remove("out");qc.hidden=false;
    if(document.activeElement&&document.activeElement.blur) document.activeElement.blur();
    yes.focus({preventScroll:true});
  }
  function cancel(){
    if(qc.hidden||qc.classList.contains("out")) return;
    qc.classList.add("out");qT=setTimeout(()=>{qc.hidden=true;qc.classList.remove("out")},200);
  }
  document.addEventListener("keydown",e=>{blocked=busy()},true);    // состояние до того, как другие обработчики закроют свои окна
  document.addEventListener("keydown",e=>{
    if(on) return;
    if(qOpen()){
      if(e.key==="Escape"){e.preventDefault();cancel()}
      else if(e.key==="ArrowLeft"||e.key==="ArrowRight"){e.preventDefault();(document.activeElement===yes?no:yes).focus()}
      return;
    }
    if(e.key==="Escape"&&!e.repeat&&!blocked) ask();
  });
  yes.addEventListener("click",()=>{qc.hidden=true;go()});
  no.addEventListener("click",cancel);
  qc.addEventListener("pointerdown",e=>{if(e.target===qc) cancel()});
  return{go,ask};
})();

/* =====================================================================
   ПРОФИЛЬ · ПУЛЬСЫ · МАГАЗИН
   ===================================================================== */
const COIN='<svg class="coin" viewBox="0 0 64 64" aria-hidden="true"><use href="#pcoin"/></svg>';
const CBIG='<span class="cbig"><svg class="coin" viewBox="0 0 64 64" aria-hidden="true"><use href="#pcoin"/></svg></span>';
const RAR={c:{n:"Обычный",c:"#9fb6cc"},r:{n:"Редкий",c:"#4fa8ff"},e:{n:"Эпический",c:"#b56bff"},l:{n:"Легендарный",c:"#ffc23f"},m:{n:"Мифический",c:"#ff4d6d"},x:{n:"Коллекция",c:"#ff5fa2"}};
const SLOTS=[["hat","Шапки"],["frame","Рамки"],["bg","Фоны"],["name","Ники"],["badge","Значки"],["fx","Эффекты"]];
const SLOT_ONE={hat:"Шапка",frame:"Рамка",bg:"Фон профиля",name:"Стиль ника",badge:"Значок",fx:"Эффект"};

/* ---------- шапки: рисуются поверх аватара (координаты аватара 0..100) ---------- */
const STK='stroke-linejoin="round" stroke-linecap="round"';
function wreathSvg(){
  let s='<path d="M8 26 C14 -6 86 -6 92 26" fill="none" stroke="#3f8f4a" stroke-width="3" '+STK+'/>';
  for(let i=0;i<9;i++){
    const a=Math.PI*(1.08+.84*i/8),x=50+44*Math.cos(a),y=27+30*Math.sin(a);
    if(i%2===0) s+=`<ellipse cx="${(x+4).toFixed(1)}" cy="${(y+3).toFixed(1)}" rx="5" ry="2.4" transform="rotate(${(a*57.3+60).toFixed(0)} ${(x+4).toFixed(1)} ${(y+3).toFixed(1)})" fill="#5fbf6a"/>`;
  }
  for(let i=0;i<7;i++){
    const a=Math.PI*(1.12+.76*i/6),x=50+44*Math.cos(a),y=27+30*Math.sin(a),r=i===3?8.5:6.6;
    let p="";
    for(let k=0;k<5;k++){const b=k*72+i*13;p+=`<ellipse cx="${x.toFixed(1)}" cy="${(y-r*.55).toFixed(1)}" rx="${(r*.46).toFixed(1)}" ry="${(r*.62).toFixed(1)}" transform="rotate(${b} ${x.toFixed(1)} ${y.toFixed(1)})"/>`}
    s+=`<g fill="url(#hg-pink)" stroke="#e0558a" stroke-width=".8">${p}</g><circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${(r*.24).toFixed(1)}" fill="#ffe27a"/>`;
  }
  return s;
}
const star4=(x,y,r,f)=>`<path d="M${x} ${y-r} Q${x+r*.18} ${y-r*.18} ${x+r} ${y} Q${x+r*.18} ${y+r*.18} ${x} ${y+r} Q${x-r*.18} ${y+r*.18} ${x-r} ${y} Q${x-r*.18} ${y-r*.18} ${x} ${y-r}Z" fill="${f}"/>`;
const HATS={
  h_crown:`<g transform="rotate(-8 50 4)"><path d="M17 17 L12 -14 L32 0 L50 -25 L68 0 L88 -14 L83 17 Z" fill="url(#hg-gold)" stroke="#8a5208" stroke-width="2" ${STK}/>
    <path d="M24 4 L20 -6 M50 -14 L48 -6" stroke="#fff6cf" stroke-width="2" opacity=".7" ${STK}/>
    <rect x="15" y="7" width="70" height="12" rx="3.5" fill="url(#hg-gold2)" stroke="#8a5208" stroke-width="2"/>
    <circle cx="12" cy="-14" r="4.2" fill="#fff3c4" stroke="#8a5208" stroke-width="1.6"/><circle cx="50" cy="-25" r="4.8" fill="#fff3c4" stroke="#8a5208" stroke-width="1.6"/><circle cx="88" cy="-14" r="4.2" fill="#fff3c4" stroke="#8a5208" stroke-width="1.6"/>
    <circle cx="33" cy="13" r="3.2" fill="#ff3b5c" stroke="#7a0718" stroke-width="1"/><path d="M50 8.5 L54 13 L50 17.5 L46 13Z" fill="#53c4ff" stroke="#0b4a7a" stroke-width="1"/><circle cx="67" cy="13" r="3.2" fill="#38e08a" stroke="#0b5a30" stroke-width="1"/></g>`,
  h_halo:`<g class="hfl"><ellipse cx="50" cy="-17" rx="33" ry="9" fill="none" stroke="#fff4c2" stroke-width="9" opacity=".35" filter="url(#hf-glow)"/>
    <ellipse cx="50" cy="-17" rx="33" ry="9" fill="none" stroke="url(#hg-gold)" stroke-width="5"/><ellipse cx="50" cy="-18.5" rx="30" ry="7" fill="none" stroke="#fffbe6" stroke-width="1.4" opacity=".8"/></g>`,
  h_phones:`<path d="M5 54 C2 -10 98 -10 95 54" fill="none" stroke="url(#hg-dark)" stroke-width="9" ${STK}/><path d="M9 40 C10 -2 90 -2 91 40" fill="none" stroke="#ff3f6e" stroke-width="2.2" opacity=".85" ${STK}/>
    <rect x="-6" y="34" width="22" height="38" rx="10" fill="url(#hg-dark)" stroke="#0b0b0e" stroke-width="2"/><rect x="-2" y="39" width="13" height="28" rx="6.5" fill="url(#hg-pink)"/>
    <rect x="84" y="34" width="22" height="38" rx="10" fill="url(#hg-dark)" stroke="#0b0b0e" stroke-width="2"/><rect x="89" y="39" width="13" height="28" rx="6.5" fill="url(#hg-pink)"/>`,
  h_cat:`<path d="M10 32 L15 -20 L47 6 Z" fill="url(#hg-dark)" stroke="#0b0b0e" stroke-width="2" ${STK}/><path d="M18 21 L21 -7 L39 7 Z" fill="url(#hg-pink)"/>
    <path d="M90 32 L85 -20 L53 6 Z" fill="url(#hg-dark)" stroke="#0b0b0e" stroke-width="2" ${STK}/><path d="M82 21 L79 -7 L61 7 Z" fill="url(#hg-pink)"/>`,
  h_horns:`<path d="M27 15 C11 5 8 -18 21 -32 C21 -17 30 -6 41 6 Z" fill="url(#hg-red)" stroke="#5a0410" stroke-width="2" ${STK}/><path d="M23 -22 C22 -12 27 -4 33 2" fill="none" stroke="#ff9a9a" stroke-width="1.6" opacity=".6" ${STK}/>
    <path d="M73 15 C89 5 92 -18 79 -32 C79 -17 70 -6 59 6 Z" fill="url(#hg-red)" stroke="#5a0410" stroke-width="2" ${STK}/>`,
  h_santa:`<path d="M13 13 C18 -24 66 -38 99 -7 C86 -12 77 -8 75 3 L88 13 Z" fill="url(#hg-red)" stroke="#7a0718" stroke-width="2" ${STK}/>
    <rect x="8" y="3" width="84" height="18" rx="9" fill="url(#hg-white)" stroke="#c9cdd8" stroke-width="1.4"/><circle cx="100" cy="-7" r="8.5" fill="url(#hg-white)" stroke="#c9cdd8" stroke-width="1.4"/>
    <g fill="#ffffff" opacity=".9"><circle cx="20" cy="8" r="2"/><circle cx="36" cy="14" r="1.8"/><circle cx="54" cy="8" r="2"/><circle cx="72" cy="14" r="1.8"/><circle cx="84" cy="8" r="1.6"/></g>`,
  h_cap:`<path d="M13 19 C11 -19 89 -19 87 19 Z" fill="url(#hg-dark)" stroke="#0b0b0e" stroke-width="2" ${STK}/><path d="M50 -15 V18" stroke="#000" stroke-width="1.4" opacity=".5"/>
    <path d="M50 18 C72 10 102 10 114 21 C100 28 70 26 50 23 Z" fill="#26262e" stroke="#0b0b0e" stroke-width="2" ${STK}/><circle cx="50" cy="-11" r="3.2" fill="#ff3f6e"/>
    <path d="M30 6 H38 L41 -1 L45.5 12 L50 -4 L53 6 H62" fill="none" stroke="#ff3f6e" stroke-width="2.8" ${STK}/>`,
  h_beanie:`<path d="M11 20 C9 -27 91 -27 89 20 Z" fill="url(#hg-mint)" stroke="#0f6a48" stroke-width="2" ${STK}/>
    <g stroke="#0f6a48" stroke-width="1.4" opacity=".45"><path d="M30 -12 C27 0 27 10 28 18"/><path d="M50 -18 V18"/><path d="M70 -12 C73 0 73 10 72 18"/></g>
    <rect x="8" y="6" width="84" height="17" rx="6" fill="#1fa070" stroke="#0f6a48" stroke-width="2"/>
    <g stroke="#0f6a48" stroke-width="1.6" opacity=".55">${Array.from({length:10},(_,i)=>`<path d="M${15+i*7.8} 9 V20"/>`).join("")}</g>
    <circle cx="50" cy="-27" r="9.5" fill="url(#hg-white)" stroke="#c9cdd8" stroke-width="1.4"/>`,
  h_wreath:wreathSvg(),
  h_wizard:`<path d="M22 14 C40 -6 52 -30 70 -54 C65 -30 71 -8 80 14 Z" fill="url(#hg-indigo)" stroke="#120838" stroke-width="2" ${STK}/>
    <ellipse cx="51" cy="14" rx="42" ry="8" fill="#2a1580" stroke="#120838" stroke-width="2"/><path d="M26 5 C42 10 60 10 77 5 L78 11 C60 16 42 16 25 11 Z" fill="url(#hg-gold)"/>
    ${star4(55,-18,5,"#ffd45a")}${star4(64,-38,3.6,"#fff3c4")}${star4(42,-4,3.2,"#ffd45a")}`,
  h_party:`<path d="M31 14 L54 -40 L73 14 Z" fill="url(#hg-pink)" stroke="#a02a5c" stroke-width="2" ${STK}/>
    <g fill="#fff">${[[46,0],[60,-6],[53,-22],[40,8],[64,8],[56,-32]].map(([x,y])=>`<circle cx="${x}" cy="${y}" r="2.6"/>`).join("")}</g>
    <circle cx="54" cy="-41" r="6.5" fill="#ffd45a" stroke="#b58a1a" stroke-width="1.4"/>`,
  h_viking:`<path d="M16 6 C2 2 -7 -15 -3 -32 C4 -19 12 -13 23 -11 Z" fill="url(#hg-white)" stroke="#8a7f6a" stroke-width="2" ${STK}/><path d="M84 6 C98 2 107 -15 103 -32 C96 -19 88 -13 77 -11 Z" fill="url(#hg-white)" stroke="#8a7f6a" stroke-width="2" ${STK}/>
    <path d="M13 20 C11 -20 89 -20 87 20 Z" fill="url(#hg-steel)" stroke="#3a4250" stroke-width="2" ${STK}/><path d="M50 -15 V12" stroke="#3a4250" stroke-width="3"/>
    <rect x="11" y="10" width="78" height="11" rx="3" fill="url(#hg-brown)" stroke="#3e2108" stroke-width="1.6"/><g fill="#e9eef5">${[20,35,50,65,80].map(x=>`<circle cx="${x}" cy="15.5" r="1.8"/>`).join("")}</g>`,
  h_bunny:`<g><ellipse cx="35" cy="-21" rx="10.5" ry="31" transform="rotate(-14 35 -21)" fill="url(#hg-white)" stroke="#c9cdd8" stroke-width="1.6"/><ellipse cx="35" cy="-19" rx="5" ry="22" transform="rotate(-14 35 -19)" fill="url(#hg-pink)"/>
    <ellipse cx="65" cy="-21" rx="10.5" ry="31" transform="rotate(14 65 -21)" fill="url(#hg-white)" stroke="#c9cdd8" stroke-width="1.6"/><ellipse cx="65" cy="-19" rx="5" ry="22" transform="rotate(14 65 -19)" fill="url(#hg-pink)"/></g>`,
  h_tiara:`<path d="M16 17 C26 3 40 -8 50 -14 C60 -8 74 3 84 17" fill="none" stroke="url(#hg-silver)" stroke-width="4.5" ${STK}/>
    <path d="M26 9 C30 -1 38 -6 44 -6 M74 9 C70 -1 62 -6 56 -6" fill="none" stroke="#e9eef5" stroke-width="2" ${STK}/>
    <g class="hsp"><path d="M50 -28 L57 -16 L50 -4 L43 -16 Z" fill="url(#hg-cyan)" stroke="#fff" stroke-width="1.4"/><path d="M33 -6 L37 0 L33 6 L29 0 Z" fill="#ff8fd0" stroke="#fff" stroke-width="1"/><path d="M67 -6 L71 0 L67 6 L63 0 Z" fill="#ff8fd0" stroke="#fff" stroke-width="1"/>
    ${star4(58,-26,3.2,"#ffffff")}</g><g fill="#fff">${[[22,12],[40,-4],[60,-4],[78,12]].map(([x,y])=>`<circle cx="${x}" cy="${y}" r="2"/>`).join("")}</g>`,
  h_cowboy:`<path d="M25 12 C23 -21 36 -27 50 -19 C64 -27 77 -21 75 12 Z" fill="url(#hg-brown)" stroke="#3e2108" stroke-width="2" ${STK}/><path d="M50 -19 C47 -10 47 0 50 8" stroke="#3e2108" stroke-width="1.6" fill="none" opacity=".6"/>
    <path d="M25 3 C42 8 58 8 75 3 L75 11 C58 16 42 16 25 11 Z" fill="#2b1606"/><circle cx="50" cy="9" r="2.6" fill="#ffd45a"/>
    <path d="M-8 9 C10 24 90 24 108 9 C100 1 91 6 80 10 C60 17 40 17 20 10 C9 6 0 1 -8 9 Z" fill="url(#hg-brown)" stroke="#3e2108" stroke-width="2" ${STK}/>`,
  h_bucket:`<path d="M21 12 C23 -17 77 -17 79 12 Z" fill="url(#hg-pastel)" stroke="#b58a1a" stroke-width="2" ${STK}/>
    <path d="M6 21 L21 8 L79 8 L94 21 C72 28 28 28 6 21 Z" fill="#ffe08a" stroke="#b58a1a" stroke-width="2" ${STK}/>
    <g fill="#ff8fb4">${[[32,-4],[50,-10],[66,-2],[40,4],[60,4]].map(([x,y])=>`<circle cx="${x}" cy="${y}" r="2.4"/>`).join("")}</g>`,
  h_flame:`<g class="hfire"><path d="M14 18 C8 0 18 -10 22 -24 C28 -12 30 -10 34 -6 C34 -22 42 -34 50 -42 C52 -28 60 -20 64 -8 C68 -16 72 -22 78 -26 C80 -10 92 0 86 18 Z" fill="url(#hg-fire)" stroke="#8a1a04" stroke-width="2" ${STK}/>
    <path d="M28 18 C26 6 34 0 36 -8 C40 0 44 2 48 -16 C54 -4 60 2 62 -4 C66 4 74 8 72 18 Z" fill="#ffe27a" opacity=".85"/></g>`
};
/* ---------- значки ---------- */
const BADGES={
  g_note:["#ff5a8a","#b8124c",'<path d="M9 17V6l9-2v10" fill="none" stroke="#fff" stroke-width="2.2"/><circle cx="7" cy="17" r="2.6" fill="#fff"/><circle cx="16" cy="15" r="2.6" fill="#fff"/>'],
  g_star:["#ffd45a","#c27a08",'<path d="M12 3.5l2.6 5.4 5.9.8-4.3 4.1 1 5.8L12 16.9 6.8 19.6l1-5.8L3.5 9.7l5.9-.8z" fill="#fff"/>'],
  g_heart:["#ff6b9a","#c2185b",'<path d="M12 20s-7-4.4-7-10a4 4 0 0 1 7-2.6A4 4 0 0 1 19 10c0 5.6-7 10-7 10z" fill="#fff"/>'],
  g_moon:["#7a6bff","#2b1a8a",'<path d="M16.5 15.5A7 7 0 0 1 9 5a7 7 0 1 0 7.5 10.5z" fill="#fff"/><circle cx="17" cy="7" r="1" fill="#fff"/>'],
  g_bolt:["#ffe14a","#e08a00",'<path d="M13 2.5L5 13.5h6l-1 8 8-11h-6z" fill="#fff"/>'],
  g_flame:["#ff8a3d","#c42a0a",'<path d="M12 21c-4 0-6.5-2.6-6.5-6 0-4 4-6 3.5-10 3 1.5 4.5 4 4.5 6 1-1 1.5-2.4 1.5-3.6 2.4 1.8 3.5 4.6 3.5 7.6 0 3.4-2.5 6-6.5 6z" fill="#fff"/>'],
  g_ghost:["#a8b8d8","#4a5a7a",'<path d="M6 20V11a6 6 0 0 1 12 0v9l-2-1.6-2 1.6-2-1.6-2 1.6-2-1.6z" fill="#fff"/><circle cx="10" cy="11" r="1.2" fill="#4a5a7a"/><circle cx="14" cy="11" r="1.2" fill="#4a5a7a"/>'],
  g_cat:["#ffb35a","#c2620a",'<path d="M5 9l1-5 4 3h4l4-3 1 5c1 1.5 1 3 1 4 0 4-3.6 7-8 7s-8-3-8-7c0-1 0-2.5 1-4z" fill="#fff"/><circle cx="9.5" cy="13" r="1.1" fill="#c2620a"/><circle cx="14.5" cy="13" r="1.1" fill="#c2620a"/>'],
  g_vinyl:["#3a3a44","#0b0b0f",'<circle cx="12" cy="12" r="8" fill="none" stroke="#fff" stroke-width="1.2" opacity=".6"/><circle cx="12" cy="12" r="5.6" fill="none" stroke="#fff" stroke-width="1" opacity=".5"/><circle cx="12" cy="12" r="3" fill="#ff3f6e"/><circle cx="12" cy="12" r=".9" fill="#fff"/>'],
  g_wave:["#3fd4ff","#0a6aa8",'<path d="M4 12h2M7 8v8M10 5v14M13 9v6M16 6v12M19 10v4" stroke="#fff" stroke-width="2" stroke-linecap="round"/>'],
  g_diamond:["#9cecff","#2a7ab8",'<path d="M7 4h10l4 5-9 11L3 9z" fill="#fff"/><path d="M3 9h18M9 4l3 16 3-16" stroke="#2a7ab8" stroke-width="1" fill="none" opacity=".5"/>'],
  g_crown:["#ffd45a","#a8650c",'<path d="M4 17l-1-10 5 4 4-7 4 7 5-4-1 10z" fill="#fff"/><rect x="4" y="18" width="16" height="2.4" rx="1" fill="#fff"/>']
};
/* ---------- новые шапки и значки ---------- */
const orbit=(n,dur,shape,rx,ry,cy)=>Array.from({length:n},(_,i)=>`<g>${shape(i)}<animateMotion dur="${dur}s" repeatCount="indefinite" begin="-${(dur*i/n).toFixed(2)}s" path="M${50-rx} ${cy} A${rx} ${ry} 0 1 1 ${50+rx} ${cy} A${rx} ${ry} 0 1 1 ${50-rx} ${cy}"/></g>`).join("");
const heartP=(s,f)=>`<path d="M0 ${s*.9} C${-s*1.4} 0 ${-s*.7} ${-s*1.1} 0 ${-s*.35} C${s*.7} ${-s*1.1} ${s*1.4} 0 0 ${s*.9}Z" fill="${f}" stroke="#fff" stroke-width=".8"/>`;
const spike=(x,y,h,w,f)=>`<path d="M${x-w} ${y} L${x} ${y-h} L${x+w} ${y}Z" fill="${f}"/>`;
Object.assign(HATS,{
  h_tophat:`<g transform="rotate(-8 50 6)"><path d="M30 10 L32 -36 C33 -40 67 -40 68 -36 L70 10 Z" fill="url(#hg-dark)" stroke="#0b0b0e" stroke-width="2" ${STK}/>
    <path d="M36 -30 L35 2" stroke="#fff" stroke-width="2" opacity=".18" ${STK}/><path d="M31 -1 L69 -1 L70 9 L30 9 Z" fill="url(#hg-red)"/>
    <ellipse cx="50" cy="11" rx="35" ry="7" fill="#1f1f25" stroke="#0b0b0e" stroke-width="2"/><circle cx="62" cy="4" r="3" fill="url(#hg-gold)"/></g>`,
  h_beret:`<path d="M12 14 C4 -4 30 -20 58 -17 C88 -14 100 2 88 14 Z" fill="url(#hg-red)" stroke="#5a0410" stroke-width="2" ${STK}/>
    <path d="M22 -2 C36 -12 56 -14 72 -8" stroke="#fff" stroke-width="2" opacity=".25" fill="none" ${STK}/><rect x="53" y="-24" width="5" height="8" rx="2.5" fill="#7a0718"/>
    <path d="M15 12 C40 19 64 19 86 12" stroke="#5a0410" stroke-width="3.4" fill="none" ${STK}/>`,
  h_antlers:`<g fill="none" stroke="url(#hg-brown)" stroke-width="5.5" ${STK}><path d="M32 12 C26 -6 20 -18 22 -36"/><path d="M25 -8 C16 -12 10 -18 7 -28"/><path d="M22 -22 C28 -26 31 -32 31 -40"/>
    <path d="M68 12 C74 -6 80 -18 78 -36"/><path d="M75 -8 C84 -12 90 -18 93 -28"/><path d="M78 -22 C72 -26 69 -32 69 -40"/></g>
    <g fill="#ffd45a">${[[22,-36],[7,-28],[31,-40],[78,-36],[93,-28],[69,-40]].map(([x,y])=>`<circle cx="${x}" cy="${y}" r="2.2"/>`).join("")}</g>`,
  h_neoncrown:`<g class="hneon"><path d="M17 16 L13 -12 L32 1 L50 -22 L68 1 L87 -12 L83 16 Z" fill="none" stroke="#ff3f9e" stroke-width="7" opacity=".55" filter="url(#hf-glow)" ${STK}/>
    <path d="M17 16 L13 -12 L32 1 L50 -22 L68 1 L87 -12 L83 16 Z" fill="rgba(255,63,158,.12)" stroke="#ffd6ec" stroke-width="2.6" ${STK}/>
    <path d="M20 8 H80" stroke="#7af0ff" stroke-width="2.4" ${STK}/><circle cx="50" cy="-22" r="3" fill="#fff"/></g>`,
  h_catphones:`<path d="M5 54 C2 -10 98 -10 95 54" fill="none" stroke="url(#hg-pink)" stroke-width="9" ${STK}/>
    <path d="M18 8 L20 -18 L38 -2 Z" fill="url(#hg-pink)" stroke="#c2185b" stroke-width="2" ${STK}/><path d="M23 2 L24 -10 L32 -2 Z" fill="#fff" opacity=".8"/>
    <path d="M82 8 L80 -18 L62 -2 Z" fill="url(#hg-pink)" stroke="#c2185b" stroke-width="2" ${STK}/><path d="M77 2 L76 -10 L68 -2 Z" fill="#fff" opacity=".8"/>
    <rect x="-6" y="34" width="22" height="38" rx="10" fill="url(#hg-white)" stroke="#e0558a" stroke-width="2"/><rect x="-2" y="39" width="13" height="28" rx="6.5" fill="#ff8fb4"/>
    <rect x="84" y="34" width="22" height="38" rx="10" fill="url(#hg-white)" stroke="#e0558a" stroke-width="2"/><rect x="89" y="39" width="13" height="28" rx="6.5" fill="#ff8fb4"/>`,
  h_thorns:`${[18,28,38,50,62,72,82].map((x,i)=>spike(x,12-Math.abs(x-50)*.12,i%2?14:20,4,"#2a1a1e")).join("")}
    <ellipse cx="50" cy="14" rx="39" ry="8" fill="none" stroke="#1d1115" stroke-width="6"/><ellipse cx="50" cy="14" rx="39" ry="8" fill="none" stroke="#5a3a44" stroke-width="1.6" stroke-dasharray="4 5"/>
    ${[[30,18],[50,21],[70,18]].map(([x,y])=>`<path d="M${x} ${y-3} C${x+3} ${y} ${x+2} ${y+4} ${x} ${y+4} C${x-2} ${y+4} ${x-3} ${y} ${x} ${y-3}Z" fill="#d0001a"/>`).join("")}`,
  h_hearts:`<ellipse cx="50" cy="-12" rx="34" ry="9" fill="none" stroke="#ff8fb4" stroke-width="1.2" opacity=".5"/>${orbit(6,5,i=>heartP(4.6,i%2?"#ff5f93":"#ffb3cc"),34,9,-12)}`,
  h_starcrown:`<g filter="url(#hf-glow)" opacity=".6">${[[18,4],[33,-10],[50,-18],[67,-10],[82,4]].map(([x,y],i)=>star4(x,y,i===2?12:8,"#fff3c4")).join("")}</g>
    ${[[18,4],[33,-10],[50,-18],[67,-10],[82,4]].map(([x,y],i)=>star4(x,y,i===2?11:7.5,"url(#hg-gold)")).join("")}
    <path d="M14 14 C30 6 70 6 86 14" stroke="url(#hg-gold)" stroke-width="3.5" fill="none" ${STK}/>${orbit(4,3.6,()=>star4(0,0,3.4,"#ffffff"),40,10,-4)}`
});
Object.assign(BADGES,{
  g_mic:["#ff6b6b","#b0203a",'<rect x="9" y="3" width="6" height="11" rx="3" fill="#fff"/><path d="M6 11a6 6 0 0 0 12 0M12 17v4M9 21h6" stroke="#fff" stroke-width="1.8" fill="none" stroke-linecap="round"/>'],
  g_headph:["#7c83ff","#3a3fb8",'<path d="M4 15v-3a8 8 0 0 1 16 0v3" stroke="#fff" stroke-width="2" fill="none"/><rect x="3" y="13" width="5" height="7" rx="2" fill="#fff"/><rect x="16" y="13" width="5" height="7" rx="2" fill="#fff"/>'],
  g_rocket:["#ff9a3c","#c2410c",'<path d="M12 3c3 2 4.5 5.5 4 10l-2 3h-4l-2-3c-.5-4.5 1-8 4-10z" fill="#fff"/><circle cx="12" cy="9.5" r="1.6" fill="#c2410c"/><path d="M10 17l-1 3 3-1.5 3 1.5-1-3" fill="#ffd45a"/>'],
  g_skull:["#9aa3b2","#2d3340",'<path d="M12 4a7 7 0 0 0-7 7c0 2.4 1.2 4 2.6 5v3h8.8v-3c1.4-1 2.6-2.6 2.6-5a7 7 0 0 0-7-7z" fill="#fff"/><circle cx="9.3" cy="11.5" r="1.8" fill="#2d3340"/><circle cx="14.7" cy="11.5" r="1.8" fill="#2d3340"/><path d="M10 19v-2M12 19v-2M14 19v-2" stroke="#2d3340" stroke-width="1"/>'],
  g_infinity:["#22d3ee","#7c3aed",'<path d="M12 12c-2-3-6-3-6 0s4 3 6 0 6-3 6 0-4 3-6 0z" fill="none" stroke="#fff" stroke-width="2.4" stroke-linecap="round"/>'],
  g_alien:["#a3e635","#166534",'<path d="M12 3c4.4 0 7 3 7 7 0 4.5-4 10-7 10s-7-5.5-7-10c0-4 2.6-7 7-7z" fill="#fff"/><ellipse cx="9" cy="11" rx="2.4" ry="1.4" transform="rotate(30 9 11)" fill="#166534"/><ellipse cx="15" cy="11" rx="2.4" ry="1.4" transform="rotate(-30 15 11)" fill="#166534"/>']
});
/* ---------- каталог предметов: редкость и цена ---------- */
const ITEMS=[
  // шапки
  ["h_cap","hat","Кепка PULSE","c",120],["h_bucket","hat","Панамка","c",140],["h_party","hat","Праздничный колпак","c",160],["h_beanie","hat","Бини","c",180],["h_beret","hat","Берет","c",220],
  ["h_cat","hat","Кошачьи ушки","r",320],["h_bunny","hat","Заячьи ушки","r",340],["h_phones","hat","Студийные наушники","r",380],["h_santa","hat","Новогодний колпак","r",400],
  ["h_horns","hat","Дьявольские рожки","r",450],["h_cowboy","hat","Ковбойская шляпа","r",480],["h_catphones","hat","Наушники с ушками","r",560],["h_tophat","hat","Цилиндр","r",600],
  ["h_wreath","hat","Венок сакуры","e",750],["h_wizard","hat","Колпак волшебника","e",800],["h_halo","hat","Нимб","e",900],["h_antlers","hat","Оленьи рога","e",950],
  ["h_viking","hat","Шлем викинга","e",1000],["h_neoncrown","hat","Неоновая корона","e",1100],["h_flame","hat","Огненная корона","e",1200],
  ["h_crown","hat","Золотая корона","l",1800],["h_hearts","hat","Орбита сердец","l",2200],["h_thorns","hat","Терновый венец","l",2500],["h_tiara","hat","Бриллиантовая диадема","l",2800],
  ["h_starcrown","hat","Звёздная корона","m",5000],
  // рамки
  ["f_silver","frame","Платина","c",120],["f_candy","frame","Леденец","c",150],["f_pixel","frame","8-бит","c",180],["f_toxic","frame","Токсик","c",200],
  ["f_neon","frame","Неон","r",350],["f_gold","frame","Золото","r",400],["f_ice","frame","Лёд","r",420],["f_matrix","frame","Матрица","r",450],["f_sakura","frame","Сакура","r",480],["f_aurora","frame","Северное сияние","r",550],["f_pulse","frame","Пульс","r",600],
  ["f_rainbow","frame","Радуга","e",750],["f_galaxy","frame","Галактика","e",850],["f_obsidian","frame","Обсидиан","e",950],["f_fire","frame","Пламя","e",1050],["f_lava","frame","Лава","e",1100],["f_holo","frame","Голограмма","e",1200],
  ["f_diamond","frame","Бриллиант","l",2400],["f_mythic","frame","Мифическое пламя","m",5500],
  // фоны
  ["b_sunset","bg","Закат","c",120],["b_ocean","bg","Глубина","c",150],["b_ice","bg","Ледник","c",160],["b_hearts","bg","Сердца","c",180],["b_desert","bg","Дюны","c",220],["b_midnight","bg","Полночь","c",250],
  ["b_vinyl","bg","Пластинка","r",320],["b_matrix","bg","Матрица","r",380],["b_mountains","bg","Горы","r",420],["b_aurora","bg","Северное сияние","r",460],["b_forest","bg","Светлячки","r",500],["b_sakura","bg","Сакура","r",550],["b_lava","bg","Лава","r",600],
  ["b_euph","bg","Эйфория","e",750],["b_holo","bg","Голография","e",850],["b_waves","bg","Ретро-волны","e",950],["b_rain","bg","Неоновый дождь","e",1000],["b_synth","bg","Синтвейв","e",1100],["b_city","bg","Ночной город","e",1150],["b_galaxy","bg","Галактика","e",1200],
  ["b_gold","bg","Золотой зал","l",2600],["b_orion","bg","Туманность Ориона","m",6000],
  // ники
  ["n_shadow","name","Тень","c",120],["n_candy","name","Леденец","c",160],["n_toxic","name","Токсик","c",200],
  ["n_sakura","name","Сакура","r",350],["n_neon","name","Неон","r",400],["n_ice","name","Лёд","r",420],["n_outline","name","Неоновый контур","r",480],["n_cyber","name","Кибер","r",550],
  ["n_chrome","name","Хром","e",800],["n_fire","name","Пламя","e",900],["n_aurora","name","Сияние","e",1000],["n_rainbow","name","Радуга","e",1100],["n_glitch","name","Глитч","e",1200],
  ["n_gold","name","Золото","l",2000],["n_mythic","name","Мифический","m",4500],
  // значки
  ["g_note","badge","Меломан","c",100],["g_heart","badge","Любовь к музыке","c",100],["g_star","badge","Критик","c",130],["g_mic","badge","Микрофон","c",150],["g_headph","badge","Наушники","c",170],
  ["g_moon","badge","Ночной слушатель","r",300],["g_bolt","badge","Энергия","r",320],["g_flame","badge","Огонь","r",350],["g_ghost","badge","Призрак","r",380],["g_cat","badge","Котик","r",400],["g_rocket","badge","Ракета","r",450],
  ["g_vinyl","badge","Коллекционер","e",750],["g_wave","badge","Битмейкер","e",800],["g_skull","badge","Череп","e",900],["g_alien","badge","Пришелец","e",1000],["g_diamond","badge","Алмаз","e",1200],
  ["g_crown","badge","Король","l",2000],["g_infinity","badge","Бесконечность","m",4000],
  // эффекты
  ["x_snow","fx","Снегопад","c",150],["x_bubbles","fx","Пузыри","c",180],
  ["x_sparkles","fx","Искры","r",350],["x_hearts","fx","Сердечки","r",400],["x_confetti","fx","Конфетти","r",450],["x_petals","fx","Лепестки","r",500],["x_rings","fx","Пульс-волны","r",550],
  ["x_notes","fx","Ноты","e",800],["x_fireflies","fx","Светлячки","e",900],["x_rain","fx","Неоновый дождь","e",1000],["x_embers","fx","Угольки","e",1100],
  ["x_stars","fx","Звездопад","l",2200],["x_orbit","fx","Орбита","m",5000]
].map(([id,s,n,r,p])=>({id,s,n,r,p}));

/* ---------- новые легендарные и мифические предметы ---------- */
const feather=(x,y,len,w,rot,f,st)=>`<path d="M0 0 C${w} ${-len*.25} ${w*.8} ${-len*.75} 0 ${-len} C${-w*.8} ${-len*.75} ${-w} ${-len*.25} 0 0Z" transform="translate(${x} ${y}) rotate(${rot})" fill="${f}" stroke="${st||"none"}" stroke-width=".8"/>`;
function wingSvg(side,f,st,dark){
  // крыло растёт от виска; side=-1 — левое, 1 — правое
  const ox=side<0?6:94,oy=30;let s="";
  for(let j=0;j<7;j++){
    const rot=side*(28+j*13),len=50-j*3.2,w=dark?6.5:8.5;
    s+=feather(ox,oy+j*1.6,len,w,rot,f,st);
  }
  for(let j=0;j<5;j++) s+=feather(ox,oy+2,30-j*2,6,side*(40+j*14),dark?"#2a0b14":"#ffffff",st);
  return `<g class="hwg" style="transform-origin:${ox}px ${oy}px;--wd:${side}">${s}</g>`;
}
function batWing(side){
  const ox=side<0?8:92,oy=26,sx=side;
  const P=(x,y)=>`${(ox+sx*x).toFixed(1)} ${(oy-y).toFixed(1)}`;
  const d=`M${P(0,0)} L${P(18,48)} L${P(30,40)} Q${P(30,26)} ${P(40,22)} L${P(50,30)} Q${P(46,14)} ${P(56,6)} L${P(64,12)} Q${P(50,-8)} ${P(4,-14)} Z`;
  return `<g class="hwg" style="transform-origin:${ox}px ${oy}px;--wd:${side}"><path d="${d}" fill="url(#hg-bat)" stroke="#1a0008" stroke-width="2" stroke-linejoin="round"/>
    <path d="M${P(2,0)} L${P(18,46)} M${P(4,-2)} L${P(40,22)} M${P(4,-6)} L${P(56,6)}" stroke="#ff2d55" stroke-width="1.4" opacity=".75" fill="none"/></g>`;
}
const flameP=(x,y,h,w,f)=>`<path d="M${x} ${y} C${x-w} ${y-h*.3} ${x-w*.6} ${y-h*.7} ${x} ${y-h} C${x+w*.15} ${y-h*.7} ${x+w*.9} ${y-h*.45} ${x+w*.5} ${y-h*.2} C${x+w*.6} ${y-h*.1} ${x+w*.3} ${y} ${x} ${y}Z" fill="${f}"/>`;
Object.assign(HATS,{
  h_wings:`${wingSvg(-1,"url(#hg-white)","#b9c8e0")}${wingSvg(1,"url(#hg-white)","#b9c8e0")}<ellipse cx="50" cy="-6" rx="24" ry="5.5" fill="none" stroke="#fff3c4" stroke-width="3" opacity=".9"/>`,
  h_bat:`${batWing(-1)}${batWing(1)}${spike(36,14,18,5,"#2a0b14")}${spike(64,14,18,5,"#2a0b14")}<circle cx="36" cy="-1" r="1.6" fill="#ff2d55"/><circle cx="64" cy="-1" r="1.6" fill="#ff2d55"/>`,
  h_samurai:`<path d="M50 4 C38 -6 24 -20 12 -38 C30 -26 42 -16 50 -6 C58 -16 70 -26 88 -38 C76 -20 62 -6 50 4Z" fill="url(#hg-gold)" stroke="#7a4a05" stroke-width="1.6" stroke-linejoin="round"/>
    <path d="M8 26 C6 -12 94 -12 92 26 Z" fill="url(#hg-lacq)" stroke="#0b0b0e" stroke-width="2.2" ${STK}/>
    ${[18,30,42,58,70,82].map(x=>`<path d="M${x} ${4-Math.abs(x-50)*.04} L${x+(x<50?-2:2)} 24" stroke="#c9a14a" stroke-width="1.2" opacity=".7"/>`).join("")}
    <path d="M4 24 C24 32 76 32 96 24 L98 32 C76 41 24 41 2 32 Z" fill="#1b1b22" stroke="url(#hg-gold)" stroke-width="2"/>
    <circle cx="50" cy="4" r="6" fill="url(#hg-gold)" stroke="#7a4a05" stroke-width="1.2"/><circle cx="50" cy="4" r="2.6" fill="#b0102a"/>`,
  h_phoenix:`<g class="hph"><g filter="url(#hf-glow)" opacity=".8">${[[18,14,30],[30,8,42],[42,4,52],[50,2,60],[58,4,52],[70,8,42],[82,14,30]].map(([x,y,h])=>flameP(x,y,h,9,"#ff6a00")).join("")}</g>
    ${[[18,14,30,-18],[30,8,42,-10],[42,4,52,-4],[58,4,52,4],[70,8,42,10],[82,14,30,18]].map(([x,y,h,r])=>feather(x,y,h,7,r,"url(#hg-fire)","#a3260a")).join("")}
    ${feather(50,2,60,8.5,0,"url(#hg-phx)","#a3260a")}
    ${[[42,-30],[50,-44],[58,-30],[30,-18],[70,-18]].map(([x,y])=>`<circle cx="${x}" cy="${y}" r="2.4" fill="#fff6c4"/>`).join("")}</g>
    <path d="M12 22 C30 12 70 12 88 22 L88 13 C70 3 30 3 12 13 Z" fill="url(#hg-gold2)" stroke="#8a5208" stroke-width="1.6"/>
    ${[26,50,74].map((x,i)=>`<path d="M${x} ${i===1?3:7} l4 5 -4 5 -4 -5z" fill="${i===1?"#ff1744":"#ff9100"}" stroke="#fff3c4" stroke-width=".8"/>`).join("")}`,
  h_galaxy:`<g transform="rotate(-10 50 -10)"><ellipse cx="50" cy="-10" rx="46" ry="11" fill="none" stroke="url(#hg-neb)" stroke-width="3.2" opacity=".85"/>
    <ellipse cx="50" cy="-10" rx="46" ry="11" fill="none" stroke="#fff" stroke-width=".8" stroke-dasharray="1 5" opacity=".9"/>
    <circle cx="50" cy="-10" r="8" fill="url(#hg-sun)" filter="url(#hf-glow)"/><circle cx="50" cy="-10" r="5" fill="#fff8e1"/>
    ${orbit(1,4.2,()=>`<circle r="5.5" fill="url(#hg-pl1)"/><ellipse rx="9" ry="2.2" fill="none" stroke="#ffe0b2" stroke-width="1.2"/>`,46,11,-10)}
    ${orbit(1,6.4,()=>`<circle r="4.2" fill="url(#hg-pl2)"/>`,46,11,-10).replace('begin="-0.00s"','begin="-2.2s"')}
    ${orbit(1,3.1,()=>`<circle r="3.2" fill="url(#hg-pl3)"/>`,46,11,-10).replace('begin="-0.00s"','begin="-1.4s"')}</g>
    ${[[14,-30],[88,-34],[30,-40],[74,6]].map(([x,y])=>star4(x,y,2.6,"#fff")).join("")}`,
  h_archangel:`<g class="hray" opacity=".55">${Array.from({length:13},(_,i)=>{const a=(-170+i*13.3)*Math.PI/180;return `<path d="M50 -12 L${(50+Math.cos(a)*70).toFixed(1)} ${(-12+Math.sin(a)*42).toFixed(1)} L${(50+Math.cos(a+.07)*70).toFixed(1)} ${(-12+Math.sin(a+.07)*42).toFixed(1)}Z" fill="#fff3c4"/>`}).join("")}</g>
    <g filter="url(#hf-glow)"><ellipse cx="50" cy="-14" rx="33" ry="9" fill="none" stroke="#ffd45a" stroke-width="6"/></g>
    <ellipse cx="50" cy="-14" rx="33" ry="9" fill="none" stroke="url(#hg-gold)" stroke-width="4"/><ellipse cx="50" cy="-14" rx="33" ry="9" fill="none" stroke="#fffbe6" stroke-width="1.2"/>
    <ellipse cx="50" cy="-24" rx="22" ry="6" fill="none" stroke="url(#hg-gold)" stroke-width="2.4" opacity=".85"/>
    ${orbit(5,4,i=>star4(0,0,i%2?2.4:3.4,"#ffffff"),33,9,-14)}`
});
Object.assign(BADGES,{
  g_trophy:["#ffd45a","#a8650c",'<path d="M7 4h10v5a5 5 0 0 1-10 0z" fill="#fff"/><path d="M7 6H4.5a2.5 2.5 0 0 0 2.6 3.6M17 6h2.5a2.5 2.5 0 0 1-2.6 3.6" stroke="#fff" stroke-width="1.5" fill="none"/><path d="M10.5 14h3l.5 3h-4z" fill="#fff"/><rect x="8" y="17" width="8" height="3" rx="1" fill="#fff"/>'],
  g_sun:["#ffb300","#e65100",'<circle cx="12" cy="12" r="4.4" fill="#fff"/><g stroke="#fff" stroke-width="1.8" stroke-linecap="round">'+[0,45,90,135,180,225,270,315].map(a=>{const r=a*Math.PI/180;return `<path d="M${(12+Math.cos(r)*6.6).toFixed(1)} ${(12+Math.sin(r)*6.6).toFixed(1)}L${(12+Math.cos(r)*9).toFixed(1)} ${(12+Math.sin(r)*9).toFixed(1)}"/>`}).join("")+'</g>'],
  g_phoenix:["#ff6a00","#8a0f1a",'<path d="M12 20c-1-3-4-4-7-8 3 1 5 1 6 0-3-2-4-5-4-8 2 3 4 4 5 4 0-2 0-4 0-5 1 2 1 3 2 5 1 0 3-1 5-4 0 3-1 6-4 8 1 1 3 1 6 0-3 4-6 5-7 8z" fill="#fff"/>'],
  g_galaxy:["#7c3aed","#0b0626",'<g fill="none" stroke="#fff" stroke-width="1.6" stroke-linecap="round"><path d="M12 12c0-3 3-4 5-3s3 4 1 7-7 4-10 1-3-9 2-11 10 0 11 4"/><path d="M12 12c0 3-3 4-5 3s-3-4-1-7 7-4 10-1" opacity=".75"/></g><circle cx="12" cy="12" r="1.6" fill="#fff"/>'],
  g_eye:["#22d3ee","#312e81",'<path d="M12 3l9.5 16.5h-19z" fill="none" stroke="#fff" stroke-width="1.4" stroke-linejoin="round"/><path d="M6.5 14c3-3.6 8-3.6 11 0-3 3.6-8 3.6-11 0z" fill="#fff"/><circle cx="12" cy="14" r="1.9" fill="#312e81"/><path d="M12 3v-1M5 6l-1-1M19 6l1-1" stroke="#fff" stroke-width="1.2"/>']
});
ITEMS.push(...[
  ["h_samurai","hat","Кабуто","l",2900],["h_wings","hat","Ангельские крылья","l",3200],["h_bat","hat","Крылья демона","l",3200],
  ["h_phoenix","hat","Корона феникса","m",6500],["h_galaxy","hat","Планетарий","m",7500],["h_archangel","hat","Нимб архангела","m",9000],
  ["f_ruby","frame","Рубин","l",1900],["f_sunburst","frame","Солнечная вспышка","l",2600],["f_vortex","frame","Воронка","l",3000],
  ["f_plasma","frame","Плазма","m",6800],["f_blackhole","frame","Чёрная дыра","m",8500],
  ["b_crystal","bg","Кристальная пещера","l",2400],["b_volcano","bg","Вулкан","l",2800],["b_planet","bg","Кольца Сатурна","l",3200],
  ["b_heaven","bg","Райские врата","m",7000],["b_blackhole","bg","Горизонт событий","m",9000],
  ["n_lava","name","Магма","l",2300],["n_diamond","name","Бриллиант","l",2900],
  ["n_void","name","Бездна","m",6000],["n_phoenix","name","Феникс","m",7500],
  ["g_sun","badge","Солнце","l",1600],["g_trophy","badge","Кубок","l",2200],["g_phoenix","badge","Феникс","l",2800],
  ["g_galaxy","badge","Галактика","m",5000],["g_eye","badge","Всевидящее око","m",6500],
  ["x_meteor","fx","Метеоритный дождь","l",2600],["x_lightning","fx","Гроза","l",3200],
  ["x_galaxy","fx","Спиральная галактика","m",7500],["x_phoenix","fx","Пламя феникса","m",9000]
].map(([id,s,n,r,p])=>({id,s,n,r,p})));
/* старую эпическую «Радугу» убрали из продажи — она была почти копией «Мифического пламени» */
{const i=ITEMS.findIndex(x=>x.id==="f_rainbow");if(i>=0) ITEMS.splice(i,1)}

/* ---------- эффекты частиц для новых предметов (подключаются в FXP) ---------- */
const FXX={};
(()=>{
  const R=(a,b)=>a+Math.random()*(b-a);
  let sprCache={};
  function spr(key,stops){                                 // мягкое пятно-спрайт, рисуется один раз
    if(sprCache[key]) return sprCache[key];
    const c=document.createElement("canvas");c.width=c.height=64;const q=c.getContext("2d"),g=q.createRadialGradient(32,32,0,32,32,32);
    stops.forEach(([o,col])=>g.addColorStop(o,col));q.fillStyle=g;q.fillRect(0,0,64,64);return sprCache[key]=c;
  }
  FXX.x_meteor={n:5,
    s(p,W,H,any){p.x=R(-W*.1,W*1.1);p.y=R(-H*.6,-10);p.v=R(160,280);p.age=any?R(-.8,1.2):R(-1.6,0);p.life=R(1.6,2.6);p.r=R(1.6,3.2)},
    d(ctx,p,dt,t,W,H){p.age+=dt;if(p.age>p.life) return false;if(p.age<0) return;
      p.x-=p.v*dt*.55;p.y+=p.v*dt;const L=p.r*26,tx=p.x+L*.48,ty=p.y-L*.88;
      const g=ctx.createLinearGradient(p.x,p.y,tx,ty);g.addColorStop(0,"rgba(255,240,200,.95)");g.addColorStop(.25,"rgba(255,140,40,.6)");g.addColorStop(1,"rgba(255,60,20,0)");
      ctx.globalAlpha=1;ctx.strokeStyle=g;ctx.lineWidth=p.r*1.5;ctx.lineCap="round";ctx.beginPath();ctx.moveTo(p.x,p.y);ctx.lineTo(tx,ty);ctx.stroke();
      const s=p.r*9;ctx.globalCompositeOperation="lighter";ctx.drawImage(spr("met",[[0,"rgba(255,255,230,1)"],[.25,"rgba(255,180,60,.8)"],[1,"rgba(255,80,0,0)"]]),p.x-s/2,p.y-s/2,s,s);ctx.globalCompositeOperation="source-over"},
    after(ctx,c,dt,t,W,H){if(!c.tw) c.tw=Array.from({length:30},()=>({x:Math.random(),y:Math.random(),p:R(0,6.28),r:R(.4,1.1)}));
      ctx.fillStyle="#ffe9d6";for(const s of c.tw){ctx.globalAlpha=.2+.4*(.5+.5*Math.sin(t*2+s.p));ctx.fillRect(s.x*W,s.y*H,s.r*1.6,s.r*1.6)}}
  };
  function bolt(W,H){
    const pts=[];let x=R(W*.15,W*.85),y=-4;pts.push([x,y]);
    while(y<H*R(.7,1)){y+=R(8,22);x+=R(-16,16);pts.push([x,y])}
    const br=[];const k=2+((Math.random()*pts.length/2)|0);let bx=pts[k][0],by=pts[k][1];br.push([bx,by]);
    for(let i=0;i<5;i++){by+=R(8,16);bx+=R(4,16)*(Math.random()<.5?-1:1);br.push([bx,by])}
    return{pts,br};
  }
  FXX.x_lightning={n:1,
    s(p,W,H){p.cd=R(.3,1.4);p.b=null;p.age=0},
    d(ctx,p,dt,t,W,H){
      if(!p.b){p.cd-=dt;if(p.cd<=0){p.b=bolt(W,H);p.age=0}return}
      p.age+=dt;const q=p.age/.5;if(q>1){p.b=null;p.cd=R(.7,1.8);return}
      const a=q<.1?1:(1-q)*(Math.sin(q*40)>-.3?1:.35);
      ctx.globalAlpha=a*.28;ctx.fillStyle="#b3c7ff";ctx.fillRect(0,0,W,H);
      const path=arr=>{ctx.beginPath();arr.forEach(([x,y],i)=>i?ctx.lineTo(x,y):ctx.moveTo(x,y));ctx.stroke()};
      ctx.lineJoin="round";ctx.lineCap="round";
      ctx.globalAlpha=a*.5;ctx.strokeStyle="#7aa2ff";ctx.lineWidth=7;path(p.b.pts);
      ctx.globalAlpha=a;ctx.strokeStyle="#f2f6ff";ctx.lineWidth=2.2;path(p.b.pts);ctx.lineWidth=1.2;path(p.b.br);
    },
    before(ctx,c,dt,t,W,H){                                // лёгкий дождь
      if(!c.rn) c.rn=Array.from({length:Math.round(W*H/5000)+10},()=>({x:Math.random()*W,y:Math.random()*H,v:R(300,460),l:R(8,16)}));
      ctx.globalAlpha=.35;ctx.strokeStyle="#a8c4ff";ctx.lineWidth=1;ctx.beginPath();
      for(const r of c.rn){r.y+=r.v*dt;r.x-=r.v*dt*.12;if(r.y>H+r.l){r.y=-r.l;r.x=Math.random()*W*1.1}ctx.moveTo(r.x,r.y);ctx.lineTo(r.x+r.l*.12,r.y-r.l)}ctx.stroke();
    }
  };
  FXX.x_galaxy={n:150,
    s(p,W,H){p.arm=Math.random()<.5?0:Math.PI;p.d=Math.pow(Math.random(),.7);p.off=R(-.35,.35);p.r=R(1,2.4);p.hue=Math.random()<.5?265:195;p.l=R(65,90)},
    d(ctx,p,dt,t,W,H){
      const cx=W/2,cy=H/2,S=Math.min(W*.5,H*1.1),a=p.arm+p.d*5.2+p.off*(1-p.d*.5)-t*.35,dd=p.d*S;
      const x=cx+Math.cos(a)*dd,y=cy+Math.sin(a)*dd*.42;
      ctx.globalAlpha=Math.min(1,.55+.45*(1-p.d)+.2*Math.sin(t*3+p.d*40));ctx.fillStyle=p.d<.12?"#fff":`hsl(${p.hue} 100% ${p.l}%)`;
      ctx.fillRect(x,y,p.r,p.r);
    },
    before(ctx,c,dt,t,W,H){const s=Math.min(W,H)*.9;ctx.globalAlpha=.9;ctx.globalCompositeOperation="lighter";
      ctx.save();ctx.translate(W/2,H/2);ctx.scale(1,.5);ctx.drawImage(spr("gal",[[0,"rgba(255,245,255,.95)"],[.12,"rgba(210,160,255,.55)"],[.45,"rgba(110,70,255,.16)"],[1,"rgba(40,0,120,0)"]]),-s/2,-s/2,s,s);ctx.restore();
      ctx.globalCompositeOperation="source-over"}
  };
  FXX.x_phoenix={n:46,
    s(p,W,H,any){p.x=R(W*.05,W*.95);p.y=any?R(H*.3,H+20):H+R(10,30);p.v=R(40,90);p.life=R(1,2.2);p.age=any?R(0,p.life):0;p.r=R(30,64);p.f=Math.random()<.16},
    d(ctx,p,dt,t,W,H){p.age+=dt;if(p.age>p.life) return false;p.y-=p.v*dt;p.x+=Math.sin(t*3+p.ph)*24*dt;
      const q=p.age/p.life;
      if(p.f){ctx.globalAlpha=(1-q);ctx.save();ctx.translate(p.x,p.y);ctx.rotate(Math.sin(t*2+p.ph)*.8);ctx.fillStyle=q<.5?"#ffe082":"#ff8a00";
        ctx.beginPath();ctx.moveTo(0,-7);ctx.quadraticCurveTo(4,0,0,8);ctx.quadraticCurveTo(-4,0,0,-7);ctx.fill();ctx.restore();return}
      const s=p.r*(1-q*.6);ctx.globalCompositeOperation="lighter";ctx.globalAlpha=(1-q)*.9;
      ctx.drawImage(spr(q<.35?"ph1":"ph2",q<.35?[[0,"rgba(255,250,200,1)"],[.3,"rgba(255,180,40,.7)"],[1,"rgba(255,80,0,0)"]]:[[0,"rgba(255,120,30,.9)"],[.4,"rgba(220,30,30,.45)"],[1,"rgba(120,0,0,0)"]]),p.x-s/2,p.y-s/2,s,s);
      ctx.globalCompositeOperation="source-over"}
  };
})();

/* ---------- эксклюзивные предметы коллекций (только в наборах) ---------- */
function blossom(x,y,r,rot,fill,stroke){
  let p="";for(let k=0;k<5;k++){const b=k*72+(rot||0);p+=`<ellipse cx="${x.toFixed(1)}" cy="${(y-r*.55).toFixed(1)}" rx="${(r*.46).toFixed(1)}" ry="${(r*.64).toFixed(1)}" transform="rotate(${b} ${x.toFixed(1)} ${y.toFixed(1)})"/>`}
  return `<g fill="${fill||"url(#hg-pink)"}" stroke="${stroke||"#e0558a"}" stroke-width=".7">${p}</g><circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${(r*.22).toFixed(1)}" fill="#ffe27a"/>`;
}
const crystal=(x,y,w,h,f)=>`<path d="M${x} ${y} L${x+w} ${y-h*.58} L${x} ${y-h} L${x-w} ${y-h*.58} Z" fill="${f}" stroke="#4a0012" stroke-width="1.4" stroke-linejoin="round"/><path d="M${x} ${y-h} L${x} ${y} M${x-w} ${y-h*.58} L${x} ${y-h*.42} L${x+w} ${y-h*.58}" stroke="#fff" stroke-width="1" opacity=".55" fill="none"/>`;
Object.assign(HATS,{
  hx_euph:`<g class="hsp"><g filter="url(#hf-glow)" opacity=".75">${crystal(50,10,9,44,"#ff2d55")}${crystal(32,12,7,32,"#ff2d55")}${crystal(68,12,7,32,"#ff2d55")}</g>
    ${crystal(19,15,5.5,22,"url(#hx-ruby)")}${crystal(81,15,5.5,22,"url(#hx-ruby)")}${crystal(32,12,7,32,"url(#hx-ruby)")}${crystal(68,12,7,32,"url(#hx-ruby)")}${crystal(50,10,9,44,"url(#hx-ruby)")}
    <path d="M13 21 C30 13 70 13 87 21 L87 13 C70 5 30 5 13 13 Z" fill="url(#hg-gold2)" stroke="#8a5208" stroke-width="1.6"/>
    <circle cx="50" cy="12.5" r="3" fill="#fff4f6" stroke="#ff2d55" stroke-width="1.4"/>${star4(60,-30,4,"#fff")}${star4(36,-14,3,"#ffd1dc")}</g>`,
  hx_ocean:`<path d="M14 20 C30 11 70 11 86 20 L86 12 C70 3 30 3 14 12 Z" fill="url(#hx-aqua)" stroke="#06455f" stroke-width="1.6"/>
    <g fill="none" stroke="url(#hx-aqua)" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"><path d="M24 10 C22 0 18 -6 14 -12 M22 2 C26 -4 30 -6 33 -12"/><path d="M50 6 C50 -8 46 -18 42 -26 M50 -6 C54 -14 58 -18 60 -26"/><path d="M76 10 C78 0 82 -6 86 -12 M78 2 C74 -4 70 -6 67 -12"/></g>
    <g>${[[14,-12],[33,-12],[42,-26],[60,-26],[86,-12],[67,-12],[50,-2]].map(([x,y],i)=>`<circle cx="${x}" cy="${y}" r="${i===6?5.5:4}" fill="url(#hx-pearl)" stroke="#9fd6e8" stroke-width=".8"/>`).join("")}</g>
    ${[26,38,62,74].map(x=>`<circle cx="${x}" cy="14" r="2" fill="#e6fbff"/>`).join("")}`,
  hx_sakura:`<g transform="rotate(10 78 6)"><path d="M58 -30 L98 30" stroke="#3a0e1e" stroke-width="4" stroke-linecap="round"/><path d="M70 -36 L90 30" stroke="#5a1630" stroke-width="3.4" stroke-linecap="round"/>
    <circle cx="58" cy="-30" r="3.4" fill="url(#hg-gold)"/><circle cx="70" cy="-36" r="3" fill="url(#hg-gold)"/>
    <path d="M84 10 L94 40 M88 8 L104 34" stroke="#ffd45a" stroke-width="1" opacity=".9"/>
    ${blossom(94,42,4,10)}${blossom(105,36,3.4,30)}
    ${blossom(66,-4,8,0)}${blossom(80,2,9.5,20)}${blossom(76,-14,7,40)}${blossom(90,-6,6.5,15)}${blossom(62,8,5.5,30)}</g>`,
  hx_glass:`<g class="hfl"><path d="M58 -18 L118 -40 L118 -4 Z" fill="url(#hx-rainbow)" opacity=".55"/>
    <path d="M44 -46 L66 -6 L22 -6 Z" fill="rgba(255,255,255,.22)" stroke="#fff" stroke-width="1.8" stroke-linejoin="round"/>
    <path d="M44 -46 L50 -6 M44 -46 L36 -18 L66 -6" fill="none" stroke="#fff" stroke-width="1" opacity=".6"/>
    <path d="M-10 -24 L40 -22" stroke="#fff" stroke-width="2.2" opacity=".85" stroke-linecap="round"/>${star4(30,-40,3.4,"#fff")}${star4(70,-34,2.6,"#e0f0ff")}</g>`,
  hx_ff:`<path d="M8 26 C14 -8 86 -8 92 26" fill="none" stroke="#2f6b3a" stroke-width="3.4" stroke-linecap="round"/>
    ${Array.from({length:11},(_,i)=>{const a=Math.PI*(1.06+.88*i/10),x=50+44*Math.cos(a),y=27+31*Math.sin(a),rot=a*57.3+(i%2?70:110);
      return `<ellipse cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" rx="9" ry="4" transform="rotate(${rot.toFixed(0)} ${x.toFixed(1)} ${y.toFixed(1)})" fill="${i%3?"#3fae5a":"#5fd07a"}" stroke="#1d5a2c" stroke-width=".8"/>`}).join("")}
    <g class="hglw">${[[20,6],[34,-6],[50,-10],[66,-6],[80,6],[42,-22],[60,-24]].map(([x,y],i)=>`<circle cx="${x}" cy="${y}" r="${i>4?2.4:3.4}" fill="#f4ffb0" filter="url(#hf-glow)"/><circle cx="${x}" cy="${y}" r="${i>4?1.4:2}" fill="#fff"/>`).join("")}</g>`,
  hx_amb:`<path d="M4 54 C0 -12 100 -12 96 54" fill="none" stroke="url(#hg-white)" stroke-width="9" stroke-linecap="round"/><path d="M8 46 C8 -4 92 -4 92 46" fill="none" stroke="#9aa3b2" stroke-width="1.6" opacity=".6"/>
    <path d="M30 -2 C40 -6 60 -6 70 -2" stroke="#a78bfa" stroke-width="3" stroke-linecap="round" opacity=".9"/>
    <rect x="-8" y="30" width="24" height="42" rx="12" fill="url(#hg-white)" stroke="#c9cdd8" stroke-width="1.6"/><rect x="-3" y="36" width="14" height="30" rx="7" fill="#2a2a32"/>
    <rect x="84" y="30" width="24" height="42" rx="12" fill="url(#hg-white)" stroke="#c9cdd8" stroke-width="1.6"/><rect x="89" y="36" width="14" height="30" rx="7" fill="#2a2a32"/>
    <circle cx="4" cy="51" r="2.4" fill="#fb923c"/><circle cx="96" cy="51" r="2.4" fill="#a78bfa"/>`
});
Object.assign(BADGES,{
  gx_euph:["#ff2d55","#7a0018",'<path d="M3 13h4l2-4 3 8 3-12 2 8h4" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>'],
  gx_ocean:["#5fd8f7","#06455f",'<path d="M4 15c0-5 3.6-9 8-9s8 4 8 9z" fill="#fff" opacity=".9"/><path d="M8 15c0-3 1.8-6 4-7M16 15c0-3-1.8-6-4-7M12 8v7" stroke="#06455f" stroke-width="1" fill="none"/><circle cx="12" cy="17" r="3" fill="#fff"/>'],
  gx_sakura:["#ffb3cc","#d23f7a",'<g fill="#fff">'+[0,72,144,216,288].map(a=>`<ellipse cx="12" cy="7" rx="2.8" ry="4" transform="rotate(${a} 12 12)"/>`).join("")+'</g><circle cx="12" cy="12" r="1.8" fill="#ffd45a"/>'],
  gx_glass:["#e0f0ff","#6a7cff",'<path d="M12 4l7 13H5z" fill="rgba(255,255,255,.55)" stroke="#fff" stroke-width="1.4" stroke-linejoin="round"/><path d="M14 11l7-2v5z" fill="#ffd1f0" opacity=".9"/>'],
  gx_ff:["#d9ff7a","#1d7a4a",'<circle cx="12" cy="14" r="4" fill="#fbffd0"/><ellipse cx="8" cy="8" rx="3.4" ry="2" transform="rotate(-30 8 8)" fill="#fff" opacity=".8"/><ellipse cx="16" cy="8" rx="3.4" ry="2" transform="rotate(30 16 8)" fill="#fff" opacity=".8"/>'],
  gx_amb:["#a78bfa","#f97316",'<path d="M5 12h1M8 9v6M11 6v12M14 8v8M17 10v4M19 12h0" stroke="#fff" stroke-width="2.2" stroke-linecap="round"/>']
});
const XITEMS=[
  ["hx_euph","hat","Рубиновая корона","k_euph"],["fx_euph","frame","Аврора","k_euph"],["bx_euph","bg","Алое сияние","k_euph"],["nx_euph","name","Эйфория","k_euph"],["gx_euph","badge","Пульс Эйфории","k_euph"],["xx_euph","fx","Звёздный варп","k_euph"],
  ["hx_ocean","hat","Жемчужная корона","k_ocean"],["fx_ocean","frame","Волна","k_ocean"],["bx_ocean","bg","Бездна","k_ocean"],["nx_ocean","name","Глубина","k_ocean"],["gx_ocean","badge","Жемчужина","k_ocean"],["xx_ocean","fx","Свет бездны","k_ocean"],
  ["hx_sakura","hat","Канзаши","k_sakura"],["fx_sakura","frame","Цветение","k_sakura"],["bx_sakura","bg","Ханами","k_sakura"],["nx_sakura","name","Ханами","k_sakura"],["gx_sakura","badge","Цветок сакуры","k_sakura"],["xx_sakura","fx","Лепестковый ветер","k_sakura"],
  ["hx_glass","hat","Призма","k_glass"],["fx_glass","frame","Кристалл","k_glass"],["bx_glass","bg","Матовое стекло","k_glass"],["nx_glass","name","Стекло","k_glass"],["gx_glass","badge","Грань","k_glass"],["xx_glass","fx","Блики","k_glass"],
  ["hx_ff","hat","Лесной венок","k_ff"],["fx_ff","frame","Рой","k_ff"],["bx_ff","bg","Лунный лес","k_ff"],["nx_ff","name","Светлячок","k_ff"],["gx_ff","badge","Светлячок","k_ff"],["xx_ff","fx","Рой светлячков","k_ff"],
  ["hx_amb","hat","Наушники «Аура»","k_amb"],["fx_amb","frame","Аура","k_amb"],["bx_amb","bg","Живая аура","k_amb"],["nx_amb","name","Минимал","k_amb"],["gx_amb","badge","Волна","k_amb"],["xx_amb","fx","Орбы","k_amb"],
  ["n_blood","name","Кровавый","adm"],["n_stars","name","Звёздный","adm"]
].map(([id,s,n,kit])=>({id,s,n,r:"x",p:0,kit,ex:true}));
ITEMS.push(...XITEMS);
const ITEM=Object.fromEntries(ITEMS.map(i=>[i.id,i]));
const KITS=[
  {id:"k_euph",n:"Эйфория",d:"Алое сияние, звёздный варп и корона из рубинового хрусталя",design:"classic",p:20000,c1:"#ff2d55",c2:"#ff8a3d",items:["hx_euph","fx_euph","bx_euph","nx_euph","gx_euph","xx_euph"]},
  {id:"k_sakura",n:"Сакура",d:"Ханами под луной, канзаши и лепестковый ветер",design:"sakura",p:16000,c1:"#ff6f9f",c2:"#ffc0d5",items:["hx_sakura","fx_sakura","bx_sakura","nx_sakura","gx_sakura","xx_sakura"]},
  {id:"k_ocean",n:"Океан",d:"Бездна, лучи света сквозь толщу воды и жемчужная корона",design:"ocean",p:15000,c1:"#2cc6f0",c2:"#0b7fae",items:["hx_ocean","fx_ocean","bx_ocean","nx_ocean","gx_ocean","xx_ocean"]},
  {id:"k_ff",n:"Светлячки",d:"Лунный лес, лесной венок и рой светлячков",design:"fireflies",p:15000,c1:"#3fe0a0",c2:"#d9ff9a",items:["hx_ff","fx_ff","bx_ff","nx_ff","gx_ff","xx_ff"]},
  {id:"k_glass",n:"Стекло",d:"Матовое стекло, парящая призма и мягкие блики",design:"glass",p:13000,c1:"#b3d9ff",c2:"#e0b3ff",items:["hx_glass","fx_glass","bx_glass","nx_glass","gx_glass","xx_glass"]},
  {id:"k_amb",n:"Амбиент",d:"Живая аура, премиальные наушники и мягкие орбы",design:"ambient",p:12000,c1:"#a78bfa",c2:"#fb923c",items:["hx_amb","fx_amb","bx_amb","nx_amb","gx_amb","xx_amb"]}
];
const KIT_OF=Object.fromEntries(KITS.flatMap(k=>k.items.map(id=>[id,k])));
const DESIGN_NAME={classic:"Эйфория",ocean:"Океан",sakura:"Сакура",glass:"Стекло",fireflies:"Светлячки",ambient:"Амбиент"};
const ADMIN_ROLES={                                    // отпечатки кодов администраторов (сами коды в программе не хранятся)
  "36e8e314c6504ea7defcd0073a1dae3ad72144447c374cb42d61994f660f52bc":{items:["n_blood","n_stars"]},
  "568c3dcf3badc95bb99c686e64f72582dcb0f23ae026d43e77e0b3a1a4fa20ef":{items:["n_stars","n_blood"],nick:"влад сантана"},
  "a2db1503aa58bcbe333cce4b12775d49867802c61e7ef497656732fe6d900851":{items:["n_stars"],nick:"7jade"}
};
const ADMIN_ITEMS={n_blood:"#d0001a",n_stars:"var(--g2)"};

/* ---------- игровые коллекции и коллекции новых дизайнов ---------- */
const KOUT="#111";
Object.assign(HATS,{
  hk_brawl:`<g class="hbn"><path d="M14 22 L10 -14 L30 2 L50 -26 L70 2 L90 -14 L86 22 Z" fill="${KOUT}" stroke="${KOUT}" stroke-width="9" stroke-linejoin="round"/>
    <path d="M14 22 L10 -14 L30 2 L50 -26 L70 2 L90 -14 L86 22 Z" fill="url(#hg-brawl)"/>
    <path d="M15 22 L86 22 L86.6 13 C62 18 38 18 14.2 13 Z" fill="#e88a00"/>
    <path d="M20 8 L17 -4 M46 -10 L50 -18" stroke="#fff6c4" stroke-width="3.2" stroke-linecap="round"/>
    ${[[10,-14],[50,-26],[90,-14]].map(([x,y])=>`<circle cx="${x}" cy="${y}" r="6" fill="#ffd93b" stroke="${KOUT}" stroke-width="3.4"/><circle cx="${x-1.8}" cy="${y-1.8}" r="1.8" fill="#fff"/>`).join("")}
    <path d="M50 -1 l10 10 -10 10 -10 -10z" fill="#a855f7" stroke="${KOUT}" stroke-width="3.2" stroke-linejoin="round"/><path d="M47 4 l3 -3 3 3" stroke="#f3e8ff" stroke-width="2" fill="none" stroke-linecap="round"/>
    <circle cx="26" cy="13" r="3.6" fill="#22c55e" stroke="${KOUT}" stroke-width="2.4"/><circle cx="74" cy="13" r="3.6" fill="#3b82f6" stroke="${KOUT}" stroke-width="2.4"/></g>`,
  hk_cs:`<path d="M5 54 C1 -10 99 -10 95 54" fill="none" stroke="#151812" stroke-width="11" stroke-linecap="round"/>
    <path d="M5 54 C1 -10 99 -10 95 54" fill="none" stroke="#4b5320" stroke-width="5.5" stroke-linecap="round"/>
    <path d="M28 4 C40 -2 60 -2 72 4" stroke="#f5a524" stroke-width="2" fill="none"/>
    ${[[-10,1],[86,-1]].map(([x])=>`<rect x="${x}" y="30" width="24" height="42" rx="8" fill="#1d2118" stroke="#0b0d09" stroke-width="2"/><rect x="${x+4}" y="36" width="16" height="30" rx="5" fill="url(#hg-olive)"/><rect x="${x+8}" y="44" width="8" height="3" rx="1.5" fill="#f5a524"/>`).join("")}
    <path d="M4 66 C8 84 22 92 38 90" stroke="#0b0d09" stroke-width="3.4" fill="none" stroke-linecap="round"/><rect x="36" y="86" width="11" height="8" rx="4" fill="#1d2118" stroke="#f5a524" stroke-width="1"/>`,
  hk_pubg:`<path d="M10 30 C10 64 22 84 32 90 M90 30 C90 64 78 84 68 90" stroke="#2a2a1e" stroke-width="3" fill="none"/>
    <path d="M2 32 C0 -18 100 -18 98 32 C80 24 20 24 2 32 Z" fill="url(#hg-olive)" stroke="#161a0c" stroke-width="2.4" stroke-linejoin="round"/>
    <g fill="#2f3a18" opacity=".8"><ellipse cx="26" cy="6" rx="9" ry="5" transform="rotate(-20 26 6)"/><ellipse cx="58" cy="-6" rx="11" ry="5"/><ellipse cx="78" cy="12" rx="7" ry="4" transform="rotate(25 78 12)"/></g>
    <g fill="#6b5b3a" opacity=".75"><ellipse cx="42" cy="12" rx="8" ry="4"/><ellipse cx="70" cy="-2" rx="5" ry="3"/><ellipse cx="18" cy="20" rx="5" ry="3"/></g>
    <path d="M2 32 C20 24 80 24 98 32" stroke="#a89060" stroke-width="2" fill="none" opacity=".7"/>
    <rect x="40" y="-6" width="20" height="10" rx="2" fill="#1d2010" stroke="#0b0d06" stroke-width="1.4"/><circle cx="50" cy="-1" r="2.4" fill="#3a4a20"/>
    <rect x="-1" y="14" width="10" height="5" rx="1.5" fill="#1d2010"/><rect x="91" y="14" width="10" height="5" rx="1.5" fill="#1d2010"/>
    ${[22,50,78].map((x,i)=>`<circle cx="${x}" cy="${i===1?24:26}" r="1.6" fill="#c8b07a"/>`).join("")}`,
  hk_doom:`<g><path d="M22 20 C2 12 -12 -10 -8 -38 C2 -18 16 -6 38 8 Z" fill="url(#hg-horn)" stroke="#120404" stroke-width="2" stroke-linejoin="round"/>
    <path d="M14 8 C6 2 0 -8 -2 -20" stroke="#ff3d00" stroke-width="1.6" fill="none" opacity=".9" class="hglw"/>
    <path d="M6 -4 l5 2 M0 -16 l5 2 M-4 -28 l4 2" stroke="#ffab40" stroke-width="1.4"/></g><g transform="translate(100 0) scale(-1 1)"><path d="M22 20 C2 12 -12 -10 -8 -38 C2 -18 16 -6 38 8 Z" fill="url(#hg-horn)" stroke="#120404" stroke-width="2" stroke-linejoin="round"/>
    <path d="M14 8 C6 2 0 -8 -2 -20" stroke="#ff3d00" stroke-width="1.6" fill="none" opacity=".9" class="hglw"/>
    <path d="M6 -4 l5 2 M0 -16 l5 2 M-4 -28 l4 2" stroke="#ffab40" stroke-width="1.4"/></g>
    <path d="M22 20 C38 8 62 8 78 20 L74 26 C58 16 42 16 26 26 Z" fill="url(#hg-steel)" stroke="#120404" stroke-width="1.8"/>
    <path d="M50 8 l6 8 -6 8 -6 -8z" fill="#1a0404" stroke="#ff3d00" stroke-width="1.4"/><circle cx="50" cy="16" r="2.4" fill="#7cff4f" class="hglw" filter="url(#hf-glow)"/><circle cx="50" cy="16" r="1.4" fill="#e6ffd9"/>`,
  hx_lux:`<g class="hsp"><path d="M50 -44 l8 9 -8 12 -8 -12z" fill="url(#hg-dia)" stroke="#fff" stroke-width="1"/><path d="M42 -35 h16 M50 -44 l-3 9 3 12 3 -12z" stroke="#9fb8d8" stroke-width=".6" fill="none"/>${star4(62,-42,3.2,"#fff")}</g>
    <path d="M12 22 L12 4 L22 4 L22 -8 L34 -8 L34 -20 L66 -20 L66 -8 L78 -8 L78 4 L88 4 L88 22 Z" fill="url(#hg-gold)" stroke="#6b4300" stroke-width="1.8" stroke-linejoin="round"/>
    <path d="M50 -20 L50 -24 M38 -20 L36 -28 M62 -20 L64 -28 M44 -20 L43 -26 M56 -20 L57 -26" stroke="url(#hg-gold)" stroke-width="2.4" stroke-linecap="round"/>
    <rect x="39" y="-15" width="22" height="10" fill="#0b0b0e" stroke="#fff3c4" stroke-width="1.2"/><path d="M41 -10 h18" stroke="#d4a537" stroke-width=".8"/>
    <rect x="25" y="-3" width="10" height="9" fill="#0b0b0e" stroke="#fff3c4" stroke-width="1"/><rect x="65" y="-3" width="10" height="9" fill="#0b0b0e" stroke="#fff3c4" stroke-width="1"/>
    <path d="M12 14 H88" stroke="#6b4300" stroke-width="1.2"/><path d="M12 18 H88" stroke="#fff3c4" stroke-width=".8" opacity=".7"/>
    ${[18,30,42,58,70,82].map(x=>`<path d="M${x} 8 l2.4 2.6 -2.4 2.6 -2.4 -2.6z" fill="#fff"/>`).join("")}`,
  hx_neb:`<g filter="url(#hf-glow)" opacity=".8"><ellipse cx="44" cy="-8" rx="38" ry="13" fill="#7c3aed" opacity=".55"/><ellipse cx="64" cy="-16" rx="22" ry="9" fill="#22d3ee" opacity=".5"/><ellipse cx="30" cy="-2" rx="16" ry="7" fill="#f472b6" opacity=".45"/></g>
    <polyline points="10,12 24,-6 38,-18 50,-30 62,-18 76,-6 90,12" fill="none" stroke="#e0e7ff" stroke-width="1" opacity=".65"/>
    <g class="hglw">${[[10,12,3],[24,-6,3.6],[38,-18,4],[50,-30,6],[62,-18,4],[76,-6,3.6],[90,12,3]].map(([x,y,r])=>star4(x,y,r,"#ffffff")).join("")}</g>
    ${[[18,-20],[80,-26],[44,-40],[66,-36]].map(([x,y])=>`<circle cx="${x}" cy="${y}" r="1.1" fill="#c7d2fe"/>`).join("")}`
});
Object.assign(BADGES,{
  gk_brawl:["#ffce1f","#7c3aed",'<path d="M12 3l2.6 5.6 6 .7-4.5 4.1 1.2 6L12 16.4 6.7 19.4l1.2-6L3.4 9.3l6-.7z" fill="#fff" stroke="#111" stroke-width="1.6" stroke-linejoin="round"/>'],
  gk_cs:["#3a3f2a","#0b0d09",'<circle cx="12" cy="12" r="6" fill="none" stroke="#39ff14" stroke-width="1.6"/><path d="M12 2v6M12 16v6M2 12h6M16 12h6" stroke="#39ff14" stroke-width="1.6"/><circle cx="12" cy="12" r="1.3" fill="#39ff14"/>'],
  gk_pubg:["#f2c94c","#6b4f12",'<circle cx="10" cy="10.5" r="6.5" fill="#2b2b2b" stroke="#fff" stroke-width="1.4"/><circle cx="9" cy="9.5" r="3.4" fill="#454545"/><path d="M15 15l5.5 5.5" stroke="#fff" stroke-width="3" stroke-linecap="round"/>'],
  gk_doom:["#ff3d00","#2a0000",'<path d="M12 3l7.5 9L12 21l-7.5-9z" fill="none" stroke="#fff" stroke-width="1.5" stroke-linejoin="round"/><path d="M12 7v10M8.5 10.5L12 13l3.5-2.5M9.5 15.5h5" stroke="#ffe0b2" stroke-width="1.5" stroke-linecap="round" fill="none"/>'],
  gx_lux:["#d4a537","#0b0b0e",'<path d="M7 5h10l4 5-9 10L3 10z" fill="#fff"/><path d="M3 10h18M9 5l-2 5 5 10 5-10-2-5" stroke="#d4a537" stroke-width=".9" fill="none"/>'],
  gx_neb:["#22d3ee","#4c1d95",'<path d="M12 2l1.6 7.2L21 7.6l-5.6 4.4L21 16.4l-7.4-1.6L12 22l-1.6-7.2L3 16.4l5.6-4.4L3 7.6l7.4 1.6z" fill="#fff"/><circle cx="12" cy="12" r="2.4" fill="#c4b5fd"/>']
});
const XITEMS2=[
  ["hk_brawl","hat","Корона чемпиона","k_brawl"],["fk_brawl","frame","Арена","k_brawl"],["bk_brawl","bg","Комикс-арена","k_brawl"],["nk_brawl","name","Комикс","k_brawl"],["gk_brawl","badge","Звезда арены","k_brawl"],["xk_brawl","fx","Звёзды арены","k_brawl"],
  ["hk_cs","hat","Тактическая гарнитура","k_cs"],["fk_cs","frame","Прицел","k_cs"],["bk_cs","bg","Пыльные стены","k_cs"],["nk_cs","name","Хедшот","k_cs"],["gk_cs","badge","Снайпер","k_cs"],["xk_cs","fx","Гильзы","k_cs"],
  ["hk_pubg","hat","Шлем 3 уровня","k_pubg"],["fk_pubg","frame","Камуфляж","k_pubg"],["bk_pubg","bg","Аирдроп","k_pubg"],["nk_pubg","name","Победитель","k_pubg"],["gk_pubg","badge","Сковорода","k_pubg"],["xk_pubg","fx","Красная зона","k_pubg"],
  ["hk_doom","hat","Рога палача","k_doom"],["fk_doom","frame","Руническое пламя","k_doom"],["bk_doom","bg","Преисподняя","k_doom"],["nk_doom","name","Ярость","k_doom"],["gk_doom","badge","Руна","k_doom"],["xk_doom","fx","Адский пепел","k_doom"],
  ["hx_lux","hat","Корона ар-деко","k_lux"],["fx_lux","frame","Ар-деко","k_lux"],["bx_lux","bg","Чёрный бархат","k_lux"],["nx_lux","name","Люкс","k_lux"],["gx_lux","badge","Бриллиант","k_lux"],["xx_lux","fx","Золотая пыль","k_lux"],
  ["hx_neb","hat","Созвездие","k_neb"],["fx_neb","frame","Туманность","k_neb"],["bx_neb","bg","Туманность","k_neb"],["nx_neb","name","Космос","k_neb"],["gx_neb","badge","Сверхновая","k_neb"],["xx_neb","fx","Звёздная пыль","k_neb"]
].map(([id,s,n,kit])=>({id,s,n,r:"x",p:0,kit,ex:true}));
XITEMS2.forEach(i=>{ITEMS.push(i);ITEM[i.id]=i});
KITS.unshift(
  {id:"k_lux",n:"Люкс",d:"Чёрный бархат, золото ар-деко, корона с бриллиантом и золотая пыль",design:"lux",p:25000,c1:"#d4a537",c2:"#fff3c4",items:["hx_lux","fx_lux","bx_lux","nx_lux","gx_lux","xx_lux"]},
  {id:"k_neb",n:"Туманность",d:"Космическая туманность, созвездие над головой и звёздная пыль",design:"nebula",p:22000,c1:"#8b5cf6",c2:"#22d3ee",items:["hx_neb","fx_neb","bx_neb","nx_neb","gx_neb","xx_neb"]});
KITS.push(
  {id:"k_doom",n:"Палач ада",style:"DOOM Eternal",d:"Рога палача, руническое пламя, преисподняя и адский пепел",p:18000,c1:"#ff3d00",c2:"#ffab40",items:["hk_doom","fk_doom","bk_doom","nk_doom","gk_doom","xk_doom"]},
  {id:"k_cs",n:"Тактика",style:"CS2",d:"Тактическая гарнитура, прицел, пыльные стены и летящие гильзы",p:15000,c1:"#f5a524",c2:"#5aa9ff",items:["hk_cs","fk_cs","bk_cs","nk_cs","gk_cs","xk_cs"]},
  {id:"k_pubg",n:"Королевская битва",style:"PUBG",d:"Шлем третьего уровня, аирдроп, красный дым и синяя зона",p:15000,c1:"#f2c94c",c2:"#e53935",items:["hk_pubg","fk_pubg","bk_pubg","nk_pubg","gk_pubg","xk_pubg"]},
  {id:"k_brawl",n:"Бравл-арена",style:"Brawl Stars",d:"Мультяшная корона, комиксовый ник и звёзды арены",p:14000,c1:"#ffce1f",c2:"#a855f7",items:["hk_brawl","fk_brawl","bk_brawl","nk_brawl","gk_brawl","xk_brawl"]});
KITS.forEach(k=>k.items.forEach(id=>KIT_OF[id]=k));
Object.assign(DESIGN_NAME,{lux:"Люкс",nebula:"Туманность"});

/* ---------- частицы коллекций ---------- */
(()=>{
  const R=(a,b)=>a+Math.random()*(b-a);
  const SC={};
  function spr(key,stops){
    if(SC[key]) return SC[key];
    const c=document.createElement("canvas");c.width=c.height=64;const q=c.getContext("2d"),g=q.createRadialGradient(32,32,0,32,32,32);
    stops.forEach(([o,col])=>g.addColorStop(o,col));q.fillStyle=g;q.fillRect(0,0,64,64);return SC[key]=c;
  }
  function star5(ctx,r){ctx.beginPath();for(let i=0;i<10;i++){const a=-Math.PI/2+i*Math.PI/5,rr=i%2?r*.45:r;ctx.lineTo(Math.cos(a)*rr,Math.sin(a)*rr)}ctx.closePath()}
  function glint(ctx,x,y,r){ctx.beginPath();ctx.moveTo(x,y-r);ctx.quadraticCurveTo(x+r*.12,y-r*.12,x+r,y);ctx.quadraticCurveTo(x+r*.12,y+r*.12,x,y+r);ctx.quadraticCurveTo(x-r*.12,y+r*.12,x-r,y);ctx.quadraticCurveTo(x-r*.12,y-r*.12,x,y-r);ctx.fill()}
  FXX.xk_brawl={n:16,
    s(p,W,H,any){p.y=any?R(0,H):H+20;p.vy=-R(30,60);p.rot=R(0,6.28);p.vr=R(-2,2);p.r=R(6,12);p.c=["#ffce1f","#ffd93b","#c084fc","#4ade80"][(Math.random()*4)|0]},
    d(ctx,p,dt,t){p.y+=p.vy*dt;p.x+=Math.sin(t*2+p.ph)*20*dt;p.rot+=p.vr*dt;if(p.y<-20) return false;
      const sc=1+.18*Math.sin(t*6+p.ph);ctx.save();ctx.translate(p.x,p.y);ctx.rotate(p.rot);ctx.scale(sc,sc);ctx.globalAlpha=.95;
      star5(ctx,p.r);ctx.lineJoin="round";ctx.lineWidth=3;ctx.strokeStyle="#111";ctx.stroke();ctx.fillStyle=p.c;ctx.fill();
      ctx.fillStyle="rgba(255,255,255,.7)";ctx.beginPath();ctx.arc(-p.r*.2,-p.r*.35,p.r*.14,0,6.28);ctx.fill();ctx.restore()}
  };
  FXX.xk_cs={n:14,
    s(p,W,H,any){p.x=R(0,W);p.y=any?R(-H,H):R(-60,-10);p.vx=R(-40,40);p.vy=R(20,80);p.rot=R(0,6.28);p.vr=R(-9,9);p.b=0;p.fade=1},
    d(ctx,p,dt,t,W,H){p.vy+=260*dt;p.x+=p.vx*dt;p.y+=p.vy*dt;p.rot+=p.vr*dt;
      if(p.y>H-6&&p.b<2){p.y=H-6;p.vy*=-.38;p.vx*=.6;p.vr*=.5;p.b++}
      if(p.b>=2){p.fade-=dt*1.4;if(p.fade<=0) return false}
      ctx.save();ctx.translate(p.x,p.y);ctx.rotate(p.rot);ctx.globalAlpha=Math.max(0,p.fade);
      const g=ctx.createLinearGradient(0,-3,0,3);g.addColorStop(0,"#fff1b8");g.addColorStop(.45,"#e0a92e");g.addColorStop(1,"#8a5a12");
      ctx.fillStyle=g;ctx.fillRect(-7,-2.6,12,5.2);ctx.fillStyle="#6b4410";ctx.fillRect(5,-3,2,6);ctx.restore()},
    after(ctx,c,dt,t,W,H){c.tr=c.tr||{x:-1,cd:1};const T=c.tr;
      if(T.x<0){T.cd-=dt;if(T.cd<=0){T.x=0;T.y=R(H*.2,H*.7);T.dy=R(-.15,.15)}return}
      T.x+=W*2.4*dt;const x=T.x,y=T.y+T.dy*x,g=ctx.createLinearGradient(x-80,0,x,0);g.addColorStop(0,"rgba(255,220,120,0)");g.addColorStop(1,"rgba(255,240,180,.95)");
      ctx.globalAlpha=1;ctx.strokeStyle=g;ctx.lineWidth=1.6;ctx.beginPath();ctx.moveTo(x-80,y-T.dy*80);ctx.lineTo(x,y);ctx.stroke();
      if(T.x>W+80){T.x=-1;T.cd=R(.8,2.2)}}
  };
  FXX.xk_pubg={n:22,
    s(p,W,H,any){p.x=R(-20,W*.45);p.y=any?R(H*.2,H+20):H+R(10,40);p.v=R(14,34);p.r=R(30,70);p.life=R(3,6);p.age=any?R(0,p.life):0;p.vx=R(4,16)},
    d(ctx,p,dt,t,W,H){p.age+=dt;if(p.age>p.life) return false;p.y-=p.v*dt;p.x+=p.vx*dt;const q=p.age/p.life,s=p.r*(.6+q*.9);
      ctx.globalAlpha=Math.sin(Math.PI*q)*.5;ctx.drawImage(spr("smk",[[0,"rgba(255,70,60,.9)"],[.5,"rgba(200,30,30,.45)"],[1,"rgba(120,0,0,0)"]]),p.x-s/2,p.y-s/2,s,s)},
    after(ctx,c,dt,t,W,H){const q=(t%12)/12,R0=Math.hypot(W,H)*.75*(1-q*.75),cx=W*.68,cy=H*.5;
      ctx.globalAlpha=.85;ctx.strokeStyle="#4fa8ff";ctx.lineWidth=2.4;ctx.setLineDash([]);ctx.beginPath();ctx.arc(cx,cy,R0,0,6.283);ctx.stroke();
      ctx.globalAlpha=.12;ctx.fillStyle="#2f7cff";ctx.beginPath();ctx.rect(0,0,W,H);ctx.arc(cx,cy,R0,0,6.283,true);ctx.fill("evenodd");
      ctx.globalAlpha=.9;ctx.strokeStyle="#fff";ctx.lineWidth=1.2;ctx.setLineDash([6,6]);ctx.beginPath();ctx.arc(cx,cy,R0*.45,0,6.283);ctx.stroke();ctx.setLineDash([])}
  };
  FXX.xk_doom={n:48,
    s(p,W,H,any){p.ash=Math.random()<.3;if(p.ash){p.y=any?R(0,H):-6;p.vy=R(10,24);p.r=R(1,2.4)}else{p.y=any?R(0,H):H+6;p.vy=-R(40,90);p.r=R(.8,2.2)}p.life=R(1.4,3);p.age=any?R(0,p.life):0},
    d(ctx,p,dt,t,W,H){p.age+=dt;p.y+=p.vy*dt;p.x+=Math.sin(t*2.6*p.s+p.ph)*(p.ash?12:22)*dt;
      if(p.ash){if(p.y>H+6) return false;ctx.globalAlpha=.55;ctx.fillStyle="#6b6b6b";ctx.fillRect(p.x,p.y,p.r*1.6,p.r);return}
      if(p.age>p.life||p.y<-8) return false;const q=p.age/p.life;ctx.globalAlpha=1-q;ctx.fillStyle=`hsl(${28-q*24} 100% ${68-q*22}%)`;ctx.beginPath();ctx.arc(p.x,p.y,p.r,0,6.283);ctx.fill()},
    before(ctx,c,dt,t,W,H){const g=ctx.createLinearGradient(0,H,0,H*.55);g.addColorStop(0,`rgba(255,${60+30*Math.sin(t*5)},0,.42)`);g.addColorStop(1,"rgba(255,40,0,0)");ctx.globalAlpha=1;ctx.fillStyle=g;ctx.fillRect(0,H*.55,W,H*.45)}
  };
  FXX.xx_lux={n:60,
    s(p,W,H,any){p.y=any?R(0,H):-4;p.vy=R(4,14);p.vx=R(-6,6);p.r=R(.6,1.8);p.gl=Math.random()<.12},
    d(ctx,p,dt,t,W,H){p.y+=p.vy*dt;p.x+=(p.vx+Math.sin(t*.8+p.ph)*5)*dt;if(p.y>H+4) return false;
      const a=.35+.65*Math.max(0,Math.sin(t*2*p.s+p.ph));ctx.globalAlpha=a;ctx.fillStyle=p.gl?"#fffbe6":"#e9c46a";
      if(p.gl&&a>.6){glint(ctx,p.x,p.y,4+3*a)}else{ctx.beginPath();ctx.arc(p.x,p.y,p.r,0,6.283);ctx.fill()}},
    after(ctx,c,dt,t,W,H){const x=((t*.18)%1.6-.3)*W,g=ctx.createLinearGradient(x-W*.15,0,x+W*.15,0);
      g.addColorStop(0,"rgba(255,230,160,0)");g.addColorStop(.5,"rgba(255,236,180,.12)");g.addColorStop(1,"rgba(255,230,160,0)");ctx.globalAlpha=1;ctx.fillStyle=g;ctx.fillRect(0,0,W,H)}
  };
  FXX.xx_neb={n:70,
    s(p,W,H){p.y=R(0,H);p.vx=R(-5,5);p.vy=R(-3,3);p.r=R(.5,1.6);p.hue=[265,190,320,220][(Math.random()*4)|0]},
    d(ctx,p,dt,t,W,H){p.x+=p.vx*dt;p.y+=p.vy*dt;if(p.x<0) p.x=W;if(p.x>W) p.x=0;if(p.y<0) p.y=H;if(p.y>H) p.y=0;
      ctx.globalAlpha=.3+.7*Math.max(0,Math.sin(t*1.5*p.s+p.ph));ctx.fillStyle=`hsl(${p.hue} 100% 82%)`;ctx.beginPath();ctx.arc(p.x,p.y,p.r,0,6.283);ctx.fill()},
    before(ctx,c,dt,t,W,H){
      if(!c.cl) c.cl=Array.from({length:4},(_,i)=>({x:R(.1,.9),y:R(.2,.8),r:R(.35,.6),h:["#7c3aed","#22d3ee","#db2777","#4f46e5"][i],ph:R(0,6)}));
      ctx.globalCompositeOperation="lighter";
      for(const k of c.cl){const s=Math.max(W,H)*k.r,x=k.x*W+Math.sin(t*.2+k.ph)*W*.05,y=k.y*H+Math.cos(t*.17+k.ph)*H*.06;
        ctx.globalAlpha=.32;ctx.drawImage(spr("nb"+k.h,[[0,k.h+"cc"],[.5,k.h+"44"],[1,k.h+"00"]]),x-s/2,y-s/2,s,s)}
      ctx.globalCompositeOperation="source-over";
      c.ss=c.ss||{t:R(.5,2)};const S=c.ss;S.t-=dt;
      if(S.t<0){if(S.t<-.7){S.t=R(1.2,3);S.x=null}else{if(S.x==null){S.x=R(W*.1,W*.7);S.y=R(0,H*.4)}const q=-S.t/.7,x=S.x+q*W*.5,y=S.y+q*H*.35;
        const g=ctx.createLinearGradient(x,y,x-60,y-42);g.addColorStop(0,`rgba(255,255,255,${1-q})`);g.addColorStop(1,"rgba(180,200,255,0)");
        ctx.globalAlpha=1;ctx.strokeStyle=g;ctx.lineWidth=1.8;ctx.lineCap="round";ctx.beginPath();ctx.moveTo(x,y);ctx.lineTo(x-60,y-42);ctx.stroke()}}
    }
  };
})();

/* ---------- частицы для эффектов профиля ---------- */
const FXP=(()=>{
  const list=[];let raf=0,last=0;
  const rnd=(a,b)=>a+Math.random()*(b-a);
  const N={x_snow:46,x_bubbles:22,x_sparkles:30,x_hearts:18,x_petals:26,x_notes:16,x_fireflies:28,x_embers:40,x_stars:3,
    xx_euph:60,xx_ocean:30,xx_sakura:34,xx_glass:14,xx_ff:44,xx_amb:7,x_confetti:40,x_rain:46,x_orbit:26,x_rings:4};
  const io="IntersectionObserver" in window?new IntersectionObserver(es=>es.forEach(e=>{const c=list.find(x=>x.cv===e.target);if(c) c.vis=e.isIntersecting}),{rootMargin:"40px"}):null;
  let glowSpr=null;
  function glow(){
    if(glowSpr) return glowSpr;
    glowSpr=document.createElement("canvas");glowSpr.width=glowSpr.height=48;
    const q=glowSpr.getContext("2d"),g=q.createRadialGradient(24,24,0,24,24,24);
    g.addColorStop(0,"rgba(255,255,215,1)");g.addColorStop(.2,"rgba(235,255,150,.85)");g.addColorStop(.5,"rgba(170,255,110,.25)");g.addColorStop(1,"rgba(140,255,100,0)");
    q.fillStyle=g;q.fillRect(0,0,48,48);return glowSpr;
  }
  function spawn(k,W,H,any){
    const p={x:rnd(0,W),y:any?rnd(0,H):H+10,ph:rnd(0,6.28),s:rnd(.6,1.2),a:0};
    switch(k){
      case "x_snow":p.y=any?rnd(0,H):-6;p.vy=rnd(14,34);p.r=rnd(1,2.8);break;
      case "x_petals":case "xx_sakura":p.y=any?rnd(0,H):-10;p.vy=rnd(18,36);p.vx=rnd(10,30);p.rot=rnd(0,6.28);p.vr=rnd(-1.4,1.4);p.sz=k==="xx_sakura"?rnd(1.2,2):1;break;
      case "x_bubbles":case "xx_ocean":p.vy=-rnd(14,34);p.r=rnd(3,9);break;
      case "x_hearts":p.vy=-rnd(16,30);p.r=rnd(5,9);break;
      case "x_notes":p.vy=-rnd(14,26);p.g="♪♫♬"[Math.floor(Math.random()*3)];p.r=rnd(12,20);break;
      case "x_embers":p.vy=-rnd(26,60);p.r=rnd(.8,2.2);p.life=rnd(1.2,2.8);p.age=any?rnd(0,p.life):0;break;
      case "x_fireflies":case "xx_ff":p.y=rnd(0,H);p.ang=rnd(0,6.28);p.v=rnd(6,18);break;
      case "x_sparkles":p.y=rnd(0,H);p.life=rnd(1.2,2.6);p.age=rnd(0,p.life);p.r=rnd(3,7);break;
      case "x_stars":p.x=rnd(-W*.2,W*.8);p.y=rnd(-H*.4,H*.4);p.v=rnd(320,520);p.life=rnd(.6,1.1);p.age=any?rnd(-2,p.life):rnd(-1.6,0);break;
      case "xx_euph":p.a=rnd(0,6.283);p.d=any?rnd(2,Math.hypot(W,H)*.5):rnd(2,20);p.v=rnd(.9,1.6);p.hue=Math.random()<.3?20:350;break;
      case "x_confetti":p.y=any?rnd(0,H):-8;p.vy=rnd(30,60);p.vx=rnd(-20,20);p.rot=rnd(0,6.28);p.vr=rnd(-4,4);p.w=rnd(4,8);p.h=rnd(2,4);p.hue=[350,45,190,280,140][Math.floor(Math.random()*5)];break;
      case "x_rain":p.y=any?rnd(0,H):-30;p.vy=rnd(260,420);p.len=rnd(10,26);p.hue=Math.random()<.5?190:320;break;
      case "x_orbit":p.a=rnd(0,6.283);p.rr=rnd(.15,.5);p.v=rnd(.3,.9)*(Math.random()<.5?1:-1);p.r=rnd(1,2.6);p.hue=[45,320,200][Math.floor(Math.random()*3)];break;
      case "x_rings":p.age=any?rnd(0,3):0;p.life=3;break;
      default:if(FXX[k]) FXX[k].s(p,W,H,any);break;
      case "xx_glass":case "xx_amb":p.y=rnd(0,H);p.vx=rnd(-8,8);p.vy=rnd(-6,6);p.r=k==="xx_amb"?rnd(24,56):rnd(10,30);p.hue=k==="xx_amb"?[262,25,200,330][Math.floor(Math.random()*4)]:rnd(180,330);break;
    }
    return p;
  }
  function petal(ctx,p,t,col,glw){
    ctx.save();ctx.translate(p.x,p.y);ctx.rotate(p.rot);const z=p.sz||1;ctx.scale(z,z*(.4+.6*Math.abs(Math.sin(t*2*p.s+p.ph))));

    ctx.fillStyle=col;ctx.beginPath();ctx.moveTo(0,-6);ctx.quadraticCurveTo(6,-2,1,6);ctx.lineTo(0,4.6);ctx.lineTo(-1,6);ctx.quadraticCurveTo(-6,-2,0,-6);ctx.fill();ctx.restore();
  }
  function drawOne(c,dt,t){
    const cv=c.cv,ctx=c.ctx,W=cv.clientWidth,H=cv.clientHeight,d=Math.min(devicePixelRatio||1,window.LOWFX?1:(W>500?1.5:1.25));
    if(!W||!H) return;
    if(cv.width!==Math.round(W*d)||cv.height!==Math.round(H*d)){cv.width=Math.round(W*d);cv.height=Math.round(H*d);c.P=null}
    ctx.setTransform(d,0,0,d,0,0);ctx.clearRect(0,0,W,H);
    const k=c.k;
    const X=FXX[k];
    if(!c.P){c.P=Array.from({length:Math.max(1,Math.round((N[k]||(X&&X.n)||20)*Math.min(1.6,W*H/180000+.4)))},()=>spawn(k,W,H,true))}
    if(X&&X.before){X.before(ctx,c,dt,t,W,H);ctx.globalAlpha=1}
    for(let i=0;i<c.P.length;i++){
      const p=c.P[i];
      if(X){if(X.d(ctx,p,dt,t,W,H,i,c)===false) c.P[i]=spawn(k,W,H,false);continue}
      switch(k){
        case "x_snow":{p.y+=p.vy*dt;p.x+=Math.sin(t*p.s+p.ph)*10*dt;if(p.y>H+6){c.P[i]=spawn(k,W,H,false);continue}
          ctx.globalAlpha=.85;ctx.fillStyle="#fff";ctx.beginPath();ctx.arc(p.x,p.y,p.r,0,6.283);ctx.fill();break}
        case "x_petals":case "xx_sakura":{p.y+=p.vy*dt;p.x+=(p.vx+Math.sin(t*p.s+p.ph)*16)*dt;p.rot+=p.vr*dt;if(p.y>H+14||p.x>W+14){c.P[i]=spawn(k,W,H,false);c.P[i].x=rnd(-W*.3,W);continue}
          ctx.globalAlpha=.92;petal(ctx,p,t,k==="xx_sakura"?(i%3?"#ffc6da":"#ffe6ef"):"#ffb3cc",k==="xx_sakura");break}
        case "x_bubbles":{p.y+=p.vy*dt;p.x+=Math.sin(t*p.s*1.5+p.ph)*12*dt;if(p.y<-12){c.P[i]=spawn(k,W,H,false);continue}
          ctx.globalAlpha=.6;ctx.strokeStyle="rgba(200,245,255,.9)";ctx.lineWidth=1.2;ctx.beginPath();ctx.arc(p.x,p.y,p.r,0,6.283);ctx.stroke();
          ctx.fillStyle="rgba(255,255,255,.8)";ctx.beginPath();ctx.arc(p.x-p.r*.35,p.y-p.r*.35,p.r*.22,0,6.283);ctx.fill();break}
        case "xx_ocean":{p.y+=p.vy*dt;p.x+=Math.sin(t*p.s*1.5+p.ph)*12*dt;if(p.y<-12){c.P[i]=spawn(k,W,H,false);continue}
          const g=ctx.createRadialGradient(p.x,p.y,0,p.x,p.y,p.r*2.2);g.addColorStop(0,"rgba(160,240,255,.35)");g.addColorStop(1,"rgba(160,240,255,0)");
          ctx.globalAlpha=1;ctx.fillStyle=g;ctx.beginPath();ctx.arc(p.x,p.y,p.r*2.2,0,6.283);ctx.fill();
          ctx.strokeStyle="rgba(220,250,255,.85)";ctx.lineWidth=1.2;ctx.beginPath();ctx.arc(p.x,p.y,p.r,0,6.283);ctx.stroke();
          ctx.fillStyle="#fff";ctx.beginPath();ctx.arc(p.x-p.r*.35,p.y-p.r*.35,p.r*.24,0,6.283);ctx.fill();break}
        case "x_hearts":{p.y+=p.vy*dt;p.x+=Math.sin(t*p.s*2+p.ph)*14*dt;if(p.y<-14){c.P[i]=spawn(k,W,H,false);continue}
          const r=p.r;ctx.globalAlpha=Math.min(1,p.y/H*1.4)*.85;ctx.fillStyle="#ff5f93";ctx.save();ctx.translate(p.x,p.y);ctx.beginPath();
          ctx.moveTo(0,r*.9);ctx.bezierCurveTo(-r*1.4,0,-r*.7,-r*1.1,0,-r*.35);ctx.bezierCurveTo(r*.7,-r*1.1,r*1.4,0,0,r*.9);ctx.fill();ctx.restore();break}
        case "x_notes":{p.y+=p.vy*dt;p.x+=Math.sin(t*p.s*1.4+p.ph)*16*dt;if(p.y<-20){c.P[i]=spawn(k,W,H,false);continue}
          ctx.globalAlpha=Math.min(1,p.y/H*1.5)*.9;ctx.fillStyle=`hsl(${(330+p.ph*20)%360} 100% 75%)`;ctx.font=`700 ${p.r}px "Segoe UI Symbol",sans-serif`;ctx.fillText(p.g,p.x,p.y);break}
        case "x_embers":{p.age+=dt;p.y+=p.vy*dt;p.x+=Math.sin(t*3*p.s+p.ph)*20*dt;if(p.age>p.life||p.y<-6){c.P[i]=spawn(k,W,H,false);continue}
          const q=p.age/p.life;ctx.globalAlpha=(1-q)*.95;ctx.fillStyle=`hsl(${30-q*20} 100% ${70-q*20}%)`;ctx.beginPath();ctx.arc(p.x,p.y,p.r,0,6.283);ctx.fill();break}
        case "x_fireflies":case "xx_ff":{p.ang+=Math.sin(t*.7+p.ph)*.04;p.x+=Math.cos(p.ang)*p.v*dt;p.y+=Math.sin(p.ang)*p.v*dt;
          if(p.x<-10) p.x=W+10;if(p.x>W+10) p.x=-10;if(p.y<-10) p.y=H+10;if(p.y>H+10) p.y=-10;
          const a=.3+.7*Math.max(0,Math.sin(t*1.6*p.s+p.ph)),sz=(k==="xx_ff"?24:18)*(.7+.4*a);
          ctx.globalAlpha=a;ctx.drawImage(glow(),p.x-sz/2,p.y-sz/2,sz,sz);break}
        case "x_sparkles":{p.age+=dt;if(p.age>p.life){c.P[i]=spawn(k,W,H,true);c.P[i].age=0;continue}
          const q=Math.sin(Math.PI*p.age/p.life),r=p.r*q;ctx.globalAlpha=q;ctx.fillStyle=i%3?"#fff6cf":"#ffffff";ctx.save();ctx.translate(p.x,p.y);ctx.rotate(t*.6+p.ph);
          ctx.beginPath();ctx.moveTo(0,-r);ctx.quadraticCurveTo(r*.15,-r*.15,r,0);ctx.quadraticCurveTo(r*.15,r*.15,0,r);ctx.quadraticCurveTo(-r*.15,r*.15,-r,0);ctx.quadraticCurveTo(-r*.15,-r*.15,0,-r);ctx.fill();ctx.restore();break}
        case "x_stars":{p.age+=dt;if(p.age>p.life){c.P[i]=spawn(k,W,H,false);continue}if(p.age<0) break;
          const q=p.age/p.life,x=p.x+p.v*p.age*.9,y=p.y+p.v*p.age*.45,len=70;const g=ctx.createLinearGradient(x,y,x-len*.9,y-len*.45);
          g.addColorStop(0,`rgba(255,255,255,${1-q})`);g.addColorStop(1,"rgba(160,200,255,0)");ctx.globalAlpha=1;ctx.strokeStyle=g;ctx.lineWidth=2;ctx.lineCap="round";
          ctx.beginPath();ctx.moveTo(x,y);ctx.lineTo(x-len*.9,y-len*.45);ctx.stroke();ctx.fillStyle=`rgba(255,255,255,${1-q})`;ctx.beginPath();ctx.arc(x,y,1.8,0,6.283);ctx.fill();break}
        case "xx_euph":{const R=Math.hypot(W,H)*.55,d0=p.d;p.d*=1+p.v*dt*1.4;p.d+=p.v*dt*14;if(p.d>R){c.P[i]=spawn(k,W,H,false);continue}
          const cx=W/2,cy=H*.55,ca=Math.cos(p.a),sa=Math.sin(p.a),q=Math.min(1,p.d/R*2.2);
          ctx.globalAlpha=q;ctx.strokeStyle=`hsl(${p.hue} 100% ${78-q*10}%)`;ctx.lineWidth=.6+q*1.6;ctx.lineCap="round";
          ctx.beginPath();ctx.moveTo(cx+ca*d0*.9,cy+sa*d0*.9);ctx.lineTo(cx+ca*p.d,cy+sa*p.d);ctx.stroke();break}
        case "x_confetti":{p.y+=p.vy*dt;p.x+=(p.vx+Math.sin(t*2+p.ph)*20)*dt;p.rot+=p.vr*dt;if(p.y>H+10){c.P[i]=spawn(k,W,H,false);continue}
          ctx.save();ctx.translate(p.x,p.y);ctx.rotate(p.rot);ctx.scale(1,Math.abs(Math.sin(t*4+p.ph))*.8+.2);ctx.globalAlpha=.95;ctx.fillStyle=`hsl(${p.hue} 95% 62%)`;ctx.fillRect(-p.w/2,-p.h/2,p.w,p.h);ctx.restore();break}
        case "x_rain":{p.y+=p.vy*dt;if(p.y>H+30){c.P[i]=spawn(k,W,H,false);continue}
          const g=ctx.createLinearGradient(p.x,p.y-p.len,p.x,p.y);g.addColorStop(0,`hsla(${p.hue},100%,70%,0)`);g.addColorStop(1,`hsla(${p.hue},100%,75%,.9)`);
          ctx.globalAlpha=1;ctx.strokeStyle=g;ctx.lineWidth=1.4;ctx.beginPath();ctx.moveTo(p.x,p.y-p.len);ctx.lineTo(p.x-1.5,p.y);ctx.stroke();break}
        case "x_orbit":{p.a+=p.v*dt;const R=Math.min(W,H)*p.rr*1.6,x=W/2+Math.cos(p.a)*R*1.6,y=H/2+Math.sin(p.a)*R*.5,front=Math.sin(p.a)>0;
          ctx.globalAlpha=front?1:.45;ctx.fillStyle=`hsl(${p.hue} 100% ${front?78:65}%)`;
          ctx.beginPath();ctx.arc(x,y,p.r*(front?1.3:.8),0,6.283);ctx.fill();if(front){ctx.globalAlpha=.25;ctx.beginPath();ctx.arc(x,y,p.r*3,0,6.283);ctx.fill()}break}
        case "x_rings":{p.age+=dt;if(p.age>p.life){p.age-=p.life}const q=p.age/p.life,R=Math.max(W,H)*.55*q;
          ctx.globalAlpha=(1-q)*.7;ctx.strokeStyle=`hsl(${(350+i*30)%360} 100% 70%)`;ctx.lineWidth=2;ctx.beginPath();ctx.ellipse(W/2,H*.55,R,R*.45,0,0,6.283);ctx.stroke();break}
        case "xx_glass":case "xx_amb":{p.x+=p.vx*dt;p.y+=p.vy*dt;if(p.x<-p.r) p.x=W+p.r;if(p.x>W+p.r) p.x=-p.r;if(p.y<-p.r) p.y=H+p.r;if(p.y>H+p.r) p.y=-p.r;
          const a=(k==="xx_amb"?.22:.3)*(.6+.4*Math.sin(t*.8*p.s+p.ph));const g=ctx.createRadialGradient(p.x,p.y,0,p.x,p.y,p.r);
          g.addColorStop(0,`hsla(${p.hue},95%,${k==="xx_amb"?65:85}%,${a})`);g.addColorStop(k==="xx_glass"?.85:.2,`hsla(${p.hue},95%,75%,${a*(k==="xx_glass"?.8:.5)})`);g.addColorStop(1,`hsla(${p.hue},95%,75%,0)`);
          ctx.globalAlpha=1;ctx.fillStyle=g;ctx.beginPath();ctx.arc(p.x,p.y,p.r,0,6.283);ctx.fill();
          if(k==="xx_glass"){ctx.strokeStyle=`rgba(255,255,255,${a*1.2})`;ctx.lineWidth=1;ctx.stroke()}break}
      }
    }
    if(X&&X.after) X.after(ctx,c,dt,t,W,H);
    if(k==="x_stars"||k==="xx_euph"){
      if(!c.tw) c.tw=Array.from({length:40},()=>({x:Math.random(),y:Math.random(),p:rnd(0,6.28),r:rnd(.4,1.3)}));
      ctx.fillStyle="#fff";for(const s of c.tw){ctx.globalAlpha=.25+.5*(.5+.5*Math.sin(t*2+s.p));ctx.beginPath();ctx.arc(s.x*W,s.y*H,s.r,0,6.283);ctx.fill()}
    }
    ctx.globalAlpha=1;
  }
  function loop(now){
    const t=now/1000;
    for(let i=list.length-1;i>=0;i--){
      const c=list[i];if(!c.cv.isConnected){if(io) io.unobserve(c.cv);list.splice(i,1);continue}
      if(!c.vis||c.cv.offsetParent===null) {c.last=now;continue}
      if(c.host&&c.drawn&&!c.host.matches(":hover")){c.last=now;continue}   // карточка магазина оживает только под курсором
      const step=c.big?(window.LOWFX?32:14):(window.LOWFX?50:30);
      if(now-c.last<step) continue;
      const dt=Math.min(.05,(now-c.last)/1000);c.last=now;drawOne(c,dt,t);c.drawn=true;
    }
    raf=list.length?requestAnimationFrame(loop):0;
  }
  function attach(cv,k){
    if(!cv||!k) return;
    if(matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const ex=list.find(c=>c.cv===cv);if(ex){if(ex.k!==k){ex.k=k;ex.P=null;ex.tw=null}return}
    const c={cv,ctx:cv.getContext("2d"),k,P:null,last:performance.now(),vis:!io,big:!!cv.closest(".pfbn"),host:cv.closest(".itm,.kit2")};
    list.push(c);if(io) io.observe(cv);
    if(!raf){last=performance.now();raf=requestAnimationFrame(loop)}
  }
  function scan(root){(root||document).querySelectorAll("canvas[data-fx]").forEach(cv=>attach(cv,cv.dataset.fx))}
  return{attach,scan};
})();

/* ---------- профиль и кошелёк ---------- */
async function sha256(s){
  try{const b=await crypto.subtle.digest("SHA-256",new TextEncoder().encode(s));return [...new Uint8Array(b)].map(x=>x.toString(16).padStart(2,"0")).join("")}catch(e){return ""}
}
const ME=(()=>{
  const D={nick:"",bio:"",av:0,bal:0,earned:0,spent:0,got:[],inv:[],eq:{},init:false,favTracks:[],favArtists:[],adminKey:"",xp:0,xpInit:false};
  let ready=false,saveT=0,lastRate=0,admin=false,role=null;
  const got=new Set();
  const rnd=(a,b)=>a+Math.floor(Math.random()*(b-a+1));
  const lo=s=>String(s||"").trim().toLowerCase();
  const owns=id=>D.inv.includes(id);
  const eq=()=>D.eq;
  function save(){
    clearTimeout(saveT);
    saveT=setTimeout(()=>{
      D.got=[...got];
      fetch("/api/profile",{method:"POST",body:JSON.stringify(D),headers:{"Content-Type":"application/json"}}).catch(()=>{});
      try{localStorage.setItem("meBackup",JSON.stringify(D))}catch(e){}
    },250);
  }
  async function checkAdmin(){
    role=D.adminKey?ADMIN_ROLES[await sha256(D.adminKey)]||null:null;admin=!!role;
    for(const id in ADMIN_ITEMS) if(!role||!role.items.includes(id)){D.inv=D.inv.filter(x=>x!==id);if(D.eq.name===id) delete D.eq.name}
    if(role){let ch=false;role.items.forEach(id=>{if(!owns(id)){D.inv.push(id);ch=true}});if(ch||D.roleSeen!==role.items[0]){D.eq.name=role.items[0];D.roleSeen=role.items[0];save()}}
  }
  async function load(){
    let j=null;
    try{const r=await fetch("/api/profile",{cache:"no-store"});if(r.ok) j=await r.json()}catch(e){}
    if(!j||!Object.keys(j).length){try{j=JSON.parse(localStorage.getItem("meBackup")||"null")}catch(e){}}
    if(j&&typeof j==="object") Object.assign(D,j);
    let refund=0;
    if(Array.isArray(D.inv)&&D.inv.includes("f_rainbow")){refund=750;D.bal=(+D.bal||0)+refund;D.spent=Math.max(0,(+D.spent||0)-refund);D.inv=D.inv.filter(x=>x!=="f_rainbow")}
    D.inv=Array.isArray(D.inv)?D.inv.filter(id=>ITEM[id]):[];
    D.eq=D.eq&&typeof D.eq==="object"?D.eq:{};
    for(const k in D.eq) if(!D.eq[k]||!ITEM[D.eq[k]]) delete D.eq[k];
    D.bal=Math.max(0,Math.floor(+D.bal||0));
    if(!Array.isArray(D.favTracks)) D.favTracks=[];if(!Array.isArray(D.favArtists)) D.favArtists=[];
    if(D.favTrack!=null&&!D.favTracks.length) D.favTracks=[D.favTrack];if(D.favArtist&&!D.favArtists.length) D.favArtists=[D.favArtist];
    delete D.favTrack;delete D.favArtist;
    if(!D.nick){let n="";try{n=localStorage.getItem("roomName")||""}catch(e){}D.nick=n||"Меломан"}
    await checkAdmin();
    for(const k in D.eq) if(!owns(D.eq[k])) delete D.eq[k];
    got.clear();(D.got||[]).forEach(k=>got.add(k));
    if(!D.rw2){                                          // переход на новые награды: уже оценённые треки альбомов не оплачиваем задним числом
      state.tracks.forEach(t=>{if(t.albumId&&isRated(t)){got.add("i:"+t.id);got.add("s:"+lo(t.artist)+"|"+lo(t.title))}});
      const ids=new Set();state.tracks.forEach(t=>{if(t.albumId) ids.add(t.albumId)});
      ids.forEach(id=>{if(got.has("a:"+id)){const a=albumInfo(id);if(a&&a.n>6) got.add("a:"+id+":album")}});
      D.rw2=true;D.got=[...got];save();
    }
    try{const c=+localStorage.getItem("rateCd")||0;if(c>(+D.cdUntil||0)) D.cdUntil=c}catch(e){}
    ready=true;paintAll();
    if(refund){save();setTimeout(()=>toastCoin(`Рамку «Радуга» убрали из магазина — ${refund} пульсов вернули на баланс`),2500)}
    scan(true);
  }
  /* ---------- опыт: трек +5, EP +50, альбом +100; уровни ---------- */
  const step=L=>50+30*(L-1);                                // сколько опыта нужно с уровня L на следующий
  function lvl(xp){let L=1,x=xp||0;while(x>=step(L)){x-=step(L);L++}return{L,cur:x,need:step(L)}}
  const lvlBonus=L=>25*L;                                   // пульсы за новый уровень
  function xpScan(){
    let gain=0,tr=0,rel=[];
    for(const t of state.tracks){if(!isRated(t)) continue;const k="x:"+t.id;if(!got.has(k)){got.add(k);gain+=5;tr++}}
    const ids=new Set();state.tracks.forEach(t=>{if(t.albumId) ids.add(t.albumId)});
    for(const id of ids){const a=albumInfo(id);if(!a||!a.complete||a.n<2) continue;const k="xa:"+id;if(got.has(k)) continue;got.add(k);const v=a.n<=6?50:100;gain+=v;rel.push({ep:a.n<=6,v,name:a.name})}
    return{gain,tr,rel};
  }
  function addXp(gain){
    const before=lvl(D.xp).L;D.xp=(D.xp||0)+gain;const after=lvl(D.xp).L;let bonus=0;
    for(let L=before+1;L<=after;L++) bonus+=lvlBonus(L);
    if(bonus){D.bal+=bonus;D.earned=(D.earned||0)+bonus}
    return{before,after,bonus};
  }
  /* ---------- награды ---------- */
  function scan(first){
    if(!ready) return;
    const out=[];
    for(const t of state.tracks){
      if(!isRated(t)) continue;                          // пульсы за каждый трек — и сингл, и трек из EP/альбома
      const k1="i:"+t.id,k2="s:"+lo(t.artist)+"|"+lo(t.title);
      if(got.has(k1)||got.has(k2)){got.add(k1);got.add(k2);continue}
      got.add(k1);got.add(k2);out.push({kind:"single",n:rnd(1,5),name:t.title});
    }
    const ids=new Set();state.tracks.forEach(t=>{if(t.albumId) ids.add(t.albumId)});
    for(const id of ids){
      const a=albumInfo(id);if(!a||!a.complete) continue;
      if(a.n<2) continue;                                // релиз из одного трека — уже оплачен как сингл
      const kind=a.n<=6?"ep":"album",k="a:"+id+":"+kind;
      if(got.has(k)) continue;
      got.add(k);if(got.has("a:"+id)&&kind==="ep") continue;   // старый ключ: бонус за этот EP уже выдан
      if(kind==="ep") out.push({kind:"ep",n:50,name:a.name});
      else out.push({kind:"album",n:100,name:a.name});
    }
    const X=xpScan(),old=D.bal;
    const sum=out.reduce((s,r)=>s+r.n,0);
    D.bal+=sum;D.earned=(D.earned||0)+sum;
    const up=X.gain?addXp(X.gain):{before:lvl(D.xp).L,after:lvl(D.xp).L,bonus:0};
    if(first&&(!D.init||!D.xpInit)){
      const wasInit=D.init;D.init=true;D.xpInit=true;save();paintAll();
      if(sum+up.bonus>0||X.gain>0) whenFree(()=>card({n:sum+up.bonus,label:sum+up.bonus?null:"LVL "+up.after,title:wasInit?`Уровень ${up.after}`:"Награда за твою медиатеку",
        sub:`За уже оценённые треки — ${X.gain} опыта, это уровень ${up.after}.`+(sum+up.bonus?` Пульсы уже на балансе — трать их в магазине.`:"")}));
      return;
    }
    if(!sum&&!X.gain) return;
    save();
    const fromBtn=performance.now()-lastRate<2500?$("#save"):null;
    if(sum) fly(fromBtn,sum,old);
    if(X.gain) flyXp(fromBtn,X.gain,!!sum);
    paintLvl();
    if(up.after>up.before){const b=up.bonus;whenFree(()=>card({n:b,title:`Новый уровень ${up.after}!`,sub:`Опыт за оценки привёл тебя на уровень ${up.after}. Награда — ${b} ${plural(b,"пульс","пульса","пульсов")}.`,lvl:up.after}))}
    const big=out.filter(r=>r.kind!=="single");
    big.forEach(r=>{try{QA.ev(r.kind)}catch(e){}});
    for(const r of big) whenFree(()=>card({n:r.n,quiet:true,title:r.kind==="ep"?"EP оценён полностью":"Альбом оценён полностью",sub:`«${r.name}» — ${r.kind==="ep"?"EP":"альбом"} целиком в твоей медиатеке. Ещё +${r.kind==="ep"?50:100} опыта.`}));
    if(!big.length&&sum) toastCoin(`+${sum} ${plural(sum,"пульс","пульса","пульсов")} и +${X.gain} опыта за оценку`);
    else if(!big.length&&X.gain) toastCoin(`+${X.gain} опыта за оценку`);
  }
  const plural=(n,a,b,c)=>{const m=n%100,d=n%10;return m>10&&m<20?c:d===1?a:d>1&&d<5?b:c};
  const busy=()=>!!(document.getElementById("intro")||document.getElementById("welcome")||document.body.classList.contains("bye"));
  function whenFree(fn){if(busy()) setTimeout(()=>whenFree(fn),600);else fn()}
  /* ---------- анимации ---------- */
  function toastCoin(text){
    const t=$("#toast");t.innerHTML=COIN+"<span>"+esc(text)+"</span>";t.classList.remove("err");t.classList.add("show","cn");
    clearTimeout(toastCoin.t);toastCoin.t=setTimeout(()=>{t.classList.remove("show");setTimeout(()=>t.classList.remove("cn"),300)},2200);
  }
  function countUp(el,from,to){
    const t0=performance.now(),d=Math.min(1200,300+Math.abs(to-from)*25);
    (function f(now){const k=Math.min(1,(now-t0)/d),e=1-Math.pow(1-k,3);el.textContent=fmtN(Math.round(from+(to-from)*e));if(k<1) requestAnimationFrame(f)})(t0);
  }
  const fmtN=n=>n>=10000?n.toLocaleString("ru-RU"):String(n);
  function paintBal(from){
    document.querySelectorAll("[data-bal]").forEach(el=>{if(from!=null&&el.isConnected&&el.offsetParent!==null&&Math.abs(D.bal-from)<5000) countUp(el,from,D.bal);else el.textContent=fmtN(D.bal)});
  }
  function fly(from,n,old){
    const pill=$("#balPill"),pr=pill.getBoundingClientRect();
    const fr=from?from.getBoundingClientRect():{left:innerWidth/2-20,top:innerHeight-120,width:40,height:40};
    const x0=fr.left+fr.width/2,y0=fr.top+fr.height/2,x1=pr.left+18,y1=pr.top+pr.height/2;
    const reduce=matchMedia("(prefers-reduced-motion: reduce)").matches;
    const lbl=document.createElement("div");lbl.className="flyt";lbl.innerHTML=COIN+"+"+n;lbl.style.left=x0+"px";lbl.style.top=y0+"px";document.body.append(lbl);
    lbl.animate([{transform:"translate(-50%,-50%) scale(.6)",opacity:0},{transform:"translate(-50%,-140%) scale(1.15)",opacity:1,offset:.25},{transform:"translate(-50%,-260%) scale(1)",opacity:0}],{duration:1300,easing:"cubic-bezier(.2,.8,.2,1)"}).onfinish=()=>lbl.remove();
    const k=reduce?1:Math.min(8,2+n);let done=0;
    for(let i=0;i<k;i++){
      const c=document.createElement("div");c.className="flyc";c.innerHTML=COIN;c.style.left=x0+"px";c.style.top=y0+"px";document.body.append(c);
      const dx=x1-x0,dy=y1-y0,sx=(Math.random()-.5)*120,sy=-80-Math.random()*90;
      c.animate([{transform:"translate(-50%,-50%) scale(.3) rotate(0deg)",opacity:0},
        {transform:`translate(calc(-50% + ${sx}px),calc(-50% + ${sy}px)) scale(1.1) rotate(${(Math.random()-.5)*200}deg)`,opacity:1,offset:.35},
        {transform:`translate(calc(-50% + ${dx}px),calc(-50% + ${dy}px)) scale(.55) rotate(360deg)`,opacity:.9}],
        {duration:reduce?300:800+i*70,delay:i*45,easing:"cubic-bezier(.55,0,.25,1)",fill:"both"}).onfinish=()=>{
          c.remove();
          if(++done===k){pill.classList.remove("bump");void pill.offsetWidth;pill.classList.add("bump");paintBal(old)}
        };
    }
  }
  function flyXp(from,n,shift){
    const fr=from?from.getBoundingClientRect():{left:innerWidth/2-20,top:innerHeight-120,width:40,height:40};
    const el=document.createElement("div");el.className="flyx";el.textContent="+"+n+" XP";
    el.style.left=(fr.left+fr.width/2+(shift?70:0))+"px";el.style.top=(fr.top+fr.height/2)+"px";document.body.append(el);
    el.animate([{transform:"translate(-50%,-50%) scale(.6)",opacity:0},{transform:"translate(-50%,-150%) scale(1.1)",opacity:1,offset:.25},{transform:"translate(-50%,-280%) scale(1)",opacity:0}],{duration:1400,delay:120,easing:"cubic-bezier(.2,.8,.2,1)",fill:"both"}).onfinish=()=>el.remove();
    const mb=$("#meBtn");if(mb){mb.classList.remove("lvup");void mb.offsetWidth;mb.classList.add("lvup")}
  }
  function paintLvl(){
    const I=lvl(D.xp);
    const mb=$("#meBtn");if(mb){let b=mb.querySelector(".lvs");if(!b){b=document.createElement("span");b.className="lvs";mb.append(b)}b.textContent=I.L;mb.title=`Мой профиль · уровень ${I.L}`}
  }
  const rq=[];let rOpen=false;
  const pill=()=>$("#balPill");
  function card(o){rq.push(o);if(!rOpen) nextCard()}
  function nextCard(){
    const o=rq.shift();if(!o){rOpen=false;return}
    rOpen=true;
    const el=$("#rwd");
    $("#rwN").textContent=o.label||("+"+o.n);$("#rwd").classList.toggle("lv",!!o.lvl);$("#rwL").textContent=o.lvl||"";$("#rwShop").hidden=!!o.noShop;$("#rwOk").textContent=o.ok||"Забрать";$("#rwT").textContent=o.title;$("#rwS").textContent=o.sub||"";
    el.classList.remove("out");el.hidden=false;
    if(!o.quiet) setTimeout(()=>{pill().classList.remove("bump");void pill().offsetWidth;pill().classList.add("bump");paintBal(D.bal-o.n)},700);
    $("#rwOk").focus({preventScroll:true});
  }
  function closeCard(go){
    const el=$("#rwd");if(el.hidden) return;
    el.classList.add("out");
    setTimeout(()=>{el.hidden=true;el.classList.remove("out");if(go){rOpen=false;SHOP.open()}else nextCard()},260);
  }
  $("#rwOk").addEventListener("click",()=>closeCard(false));
  $("#rwShop").addEventListener("click",()=>{rq.length=0;closeCard(true)});
  $("#rwd").addEventListener("pointerdown",e=>{if(e.target.id==="rwd") closeCard(false)});
  document.addEventListener("keydown",e=>{if(!$("#rwd").hidden&&(e.key==="Escape"||e.key==="Enter")){e.preventDefault();e.stopImmediatePropagation();closeCard(false)}},true);
  /* ---------- покупки ---------- */
  function buy(ids,price){
    ids=ids.filter(id=>!owns(id));
    if(!ids.length) return true;
    if(D.bal<price) return false;
    const old=D.bal;D.bal-=price;D.spent=(D.spent||0)+price;D.inv.push(...ids);save();paintBal(old);
    try{QA.ev("buy")}catch(e){}
    return true;
  }
  /* награды заданий и достижений: пульсы + опыт (с повышением уровня) */
  function reward(n,xp,o){
    o=o||{};n=Math.max(0,Math.round(n||0));const old=D.bal;
    D.bal+=n;D.earned=(D.earned||0)+n;
    const up=xp?addXp(xp):{before:lvl(D.xp).L,after:lvl(D.xp).L,bonus:0};
    save();
    if(n&&o.from) fly(o.from,n,old);else paintBal(old);
    if(xp&&o.from) flyXp(o.from,xp,!!n);
    paintLvl();try{PF.refresh()}catch(e){}
    if(up.after>up.before){const b=up.bonus;whenFree(()=>card({n:b,title:`Новый уровень ${up.after}!`,sub:`Награда — ${b} ${plural(b,"пульс","пульса","пульсов")}.`,lvl:up.after}))}
    return up;
  }
  /* казино: ставка списывается сразу и сохраняется — перезапуск программы не вернёт её */
  function spend(n){n=Math.floor(n);if(!(n>0)||D.bal<n) return false;const old=D.bal;D.bal-=n;D.bet=(D.bet||0)+n;save();paintBal(old);return true}
  function payout(n){n=Math.floor(n);if(!(n>0)) return;const old=D.bal;D.bal+=n;D.won=(D.won||0)+n;save();const p=$("#balPill");if(p){p.classList.remove("bump");void p.offsetWidth;p.classList.add("bump")}paintBal(old)}
  function equip(id,on){
    const it=ITEM[id];if(!it||!owns(id)) return;
    if(on===false||(on==null&&D.eq[it.s]===id)) delete D.eq[it.s];else D.eq[it.s]=id;
    save();paintAll();
  }
  function equipSet(ids){ids.forEach(id=>{const it=ITEM[id];if(it&&owns(id)) D.eq[it.s]=id});save();paintAll()}
  /* перезарядка: новую оценку можно ставить не чаще раза в 30 секунд — чтобы не накручивать пульсы */
  const CD=30000;
  function cdLeft(){const l=(+D.cdUntil||0)-Date.now();if(l>CD){D.cdUntil=Date.now()+CD;return CD}return Math.max(0,l)}
  function cdStart(){D.cdUntil=Date.now()+CD;try{localStorage.setItem("rateCd",String(D.cdUntil))}catch(e){}save()}
  function setInfo(o){Object.assign(D,o);save();paintAll()}
  async function tryAdmin(code){
    const r=ADMIN_ROLES[await sha256(code)];if(!r) return false;
    D.adminKey=code;if(r.nick) D.nick=r.nick;
    try{localStorage.setItem("roomName",D.nick)}catch(e){}
    await checkAdmin();save();paintAll();return true;
  }
  /* ---------- отрисовка общих частей ---------- */
  const avUrl=()=>D.av?`/api/img/me/1?v=${D.av}`:"";
  const initial=n=>(String(n||"").replace(/^[^\p{L}\p{N}]+/u,"").charAt(0)||"?").toUpperCase();
  const SIL='<svg viewBox="0 0 24 24" class="mav-sil"><circle cx="12" cy="9" r="4.2"/><path d="M3.5 21c.8-4.4 4.2-7 8.5-7s7.7 2.6 8.5 7z"/></svg>';
  const SPK=new Set(["f_diamond","fx_glass","f_mythic","fx_lux","fx_neb","f_plasma","f_blackhole"]);
  function avatar(size,e,o){
    e=e||D.eq;o=o||{};
    const u=avUrl(),fr=e.frame||"",hat=e.hat||"";
    const img=u?`background-image:url('${u}')`:(o.plain?"background:linear-gradient(160deg,#3a3440,#16131a)":`background:${coverBg(D.nick,"")}`);
    return `<span class="mav${fr?" fr":""}" data-fr="${fr}" style="--s:${size}px">${fr?'<span class="mav-g"></span><span class="mav-r"></span>':""}
      <span class="mav-i" style="${img}">${u?"":o.plain?SIL:esc(initial(D.nick))}</span>
      ${SPK.has(fr)?'<span class="mav-sp"><i></i><i></i><i></i></span>':""}
      ${hat&&HATS[hat]?`<svg class="mav-h" viewBox="-20 -40 140 140" aria-hidden="true">${HATS[hat]}</svg>`:""}</span>`;
  }
  const badge=(id,big)=>{const b=BADGES[id];if(!b) return "";return `<span class="mbd${big?" big":""}${ITEM[id]&&ITEM[id].ex?" ex":""}${ITEM[id]&&ITEM[id].r==="m"?" my":""}" style="--b1:${b[0]};--b2:${b[1]}" title="${esc(ITEM[id]?ITEM[id].n:"")}"><svg viewBox="0 0 24 24">${b[2]}</svg></span>`};
  const DRIPS=[[8,0,.42],[21,1.8,.3],[37,.7,.55],[52,2.6,.36],[66,1.2,.5],[81,3.1,.32],[93,.4,.46]];
  function nameH(e,txt,o){
    o=o||{};e=e||D.eq;
    const t=esc(txt==null?D.nick:txt),st=e.name||"";
    const pre=admin&&txt==null&&!o.noAdm?'<span class="admpf">[ADMIN]</span>':"";
    if(st==="n_blood"){                                 // каждая буква — отдельно, капли стекают прямо с букв
      const raw=[...String(txt==null?D.nick:txt)];let k=0;
      const L=raw.map((ch,i)=>{
        if(ch===" ") return '<span class="bl sp"> </span>';
        const drip=/[\p{L}\p{N}]/u.test(ch)&&(k++%2===0);const q=DRIPS[(i*3)%DRIPS.length];
        return `<span class="bl">${esc(ch)}${drip?`<i style="--dd:${q[1]}s;--dh:${q[2]}em;--dx:${30+((i*37)%40)}%"></i>`:""}</span>`;
      }).join("");
      return `${pre}<span class="mnm" data-nm="n_blood">${L}</span>`;
    }
    if(st==="n_stars"){                                 // звёздочки хаотично летают вокруг ника; цвета — из темы дизайна
      const r=k=>{const v=Math.sin(k*12.9898+78.233)*43758.5453;return v-Math.floor(v)};
      const P=Array.from({length:18},(_,i)=>{
        const a=(n,m)=>((r(i*7+n)*2-1)*m).toFixed(2)+"em";
        return `<i class="${i%4===3?"dot":""}" style="left:${(r(i)*100).toFixed(1)}%;top:${(15+r(i+40)*70).toFixed(1)}%;--s1x:${a(1,.5)};--s1y:${a(2,.45)};--s2x:${a(3,.6)};--s2y:${a(4,.5)};--s3x:${a(5,.55)};--s3y:${a(6,.55)};--sz:${(.2+r(i+9)*.24).toFixed(2)}em;--sd:${(2.2+r(i+3)*2.4).toFixed(2)}s;--sl:${(-r(i+5)*4).toFixed(2)}s"></i>`}).join("");
      return `${pre}<span class="mnm" data-nm="n_stars"><span class="st">${t}</span><span class="spx" aria-hidden="true">${P}</span></span>`;
    }
    return `${pre}<span class="mnm" data-nm="${st}">${t}</span>`;
  }
  function paintAll(){
    const mb=$("#meBtn");if(mb) mb.innerHTML=avatar(30);
    paintBal();paintLvl();
    try{PF.refresh()}catch(e){}
    try{SHOP.refresh()}catch(e){}
  }
  return{load,scan,buy,reward,spend,payout,save,equip,equipSet,setInfo,owns,eq,avatar,badge,nameH,avUrl,tryAdmin,fmtN,get role(){return role},lvl:()=>lvl(D.xp),
    cdLeft,cdStart,CD,
    get data(){return D},get admin(){return admin},noteRate(){lastRate=performance.now()},paintBal,card,toastCoin};
})();

/* ---------- общие фабрики превью ---------- */
function bannerHTML(bg,fx,cls){
  return `<div class="bnr ${cls||""}" data-bn="${bg||""}"><i class="bnr-a"></i><i class="bnr-b"></i>${fx?`<canvas data-fx="${fx}"></canvas>`:""}</div>`;
}
function miniProfile(e,o){
  o=o||{};
  return `<div class="mpf">${bannerHTML(e.bg,e.fx,"sm")}<div class="mpf-b">${ME.avatar(o.av||84,e)}<div class="mpf-n">${ME.nameH(e,o.nick)}${e.badge?ME.badge(e.badge):""}</div>${o.sub?`<div class="mpf-s">${o.sub}</div>`:""}</div></div>`;
}
function itemPreview(it){
  const e={[it.s]:it.id};
  switch(it.s){
    case "hat":case "frame":return `<div class="ipv avp">${ME.avatar(80,e,{plain:true})}</div>`;
    case "bg":return `<div class="ipv bgp">${bannerHTML(it.id,"","fill")}</div>`;
    case "name":return `<div class="ipv nmp">${ME.nameH(e,null,{noAdm:true})}</div>`;
    case "badge":return `<div class="ipv bdp">${ME.badge(it.id,true)}</div>`;
    case "fx":return `<div class="ipv fxp"><canvas data-fx="${it.id}"></canvas></div>`;
  }
  return "";
}
const plural=(n,a,b,c)=>{const m=n%100,d=n%10;return m>10&&m<20?c:d===1?a:d>1&&d<5?b:c};

/* ---------- выбор любимых треков / артистов (до 3) ---------- */
const PICK=(()=>{
  const el=$("#fpk"),MAX=3;let mode="",cb=null,sel=[];
  const has=v=>sel.some(x=>String(x)===String(v));
  function rows(q){
    q=(q||"").trim().toLowerCase();
    if(mode==="track"){
      const list=state.tracks.filter(isRated).filter(t=>!q||(t.title+" "+t.artist).toLowerCase().includes(q)).sort((a,b)=>b.total-a.total||b.id-a.id);
      return list.length?list.map(t=>`<button class="fpr${has(t.id)?" on":""}" data-v="${t.id}" type="button"><span class="fpn">${has(t.id)?sel.findIndex(x=>x===t.id)+1:""}</span><div class="cv" style="${coverCss(t)}">${hasCov(t)?"":NOTE}</div>
        <div class="tx"><b>${esc(t.title)}</b><span>${esc(t.artist)}</span></div><i style="color:${rgCol(t.total)}">${fmt(t.total)}</i></button>`).join(""):`<div class="fpe">Ничего не найдено</div>`;
    }
    const gs=AP.groups().filter(g=>g.key&&g.items.some(isRated)).filter(g=>!q||g.name.toLowerCase().includes(q)).sort((a,b)=>a.name.localeCompare(b.name,"ru"));
    return gs.length?gs.map(g=>{const n=g.items.filter(isRated).length;return `<button class="fpr${has(g.key)?" on":""}" data-v="${esc(g.key)}" type="button"><span class="fpn">${has(g.key)?sel.indexOf(g.key)+1:""}</span>${AP.avatar(g.name)}
      <div class="tx"><b>${esc(g.name)}</b><span>${n} ${plural(n,"оценка","оценки","оценок")}</span></div></button>`}).join(""):`<div class="fpe">Ничего не найдено</div>`;
  }
  const cnt=()=>`Выбрано <b>${sel.length}</b> из ${MAX}`;
  function open(m,cur,done){
    mode=m;cb=done;sel=(cur||[]).slice(0,MAX);
    el.innerHTML=`<div class="fpc"><div class="fph"><div><b>${m==="track"?"Любимые треки":"Любимые артисты"}</b><span>Выбери от 1 до 3 — порядок сохранится</span></div><button class="pround" data-x type="button" title="Закрыть"><svg class="ico" viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg></button></div>
      <div class="fps"><svg class="ico" viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/></svg><input id="fpQ" placeholder="${m==="track"?"Поиск по оценённым трекам":"Поиск артиста"}" autocomplete="off"></div>
      <div class="fpl" id="fpL">${rows("")}</div>
      <div class="fpf"><span id="fpC">${cnt()}</span><button class="gbtn ghost" data-clr type="button">Очистить</button><button class="gbtn" data-ok type="button">Готово</button></div></div>`;
    el.classList.remove("out");el.hidden=false;setTimeout(()=>{const q=$("#fpQ");if(q) q.focus()},50);
  }
  function close(){if(el.hidden) return;el.classList.add("out");setTimeout(()=>{el.hidden=true;el.classList.remove("out");el.innerHTML=""},220)}
  function paint(){$("#fpL").innerHTML=rows($("#fpQ").value);$("#fpC").innerHTML=cnt()}
  el.addEventListener("input",e=>{if(e.target.id==="fpQ") $("#fpL").innerHTML=rows(e.target.value)});
  el.addEventListener("click",e=>{
    if(e.target===el||e.target.closest("[data-x]")){close();return}
    if(e.target.closest("[data-clr]")){sel=[];cb&&cb([]);paint();return}
    if(e.target.closest("[data-ok]")){close();return}
    const r=e.target.closest(".fpr");if(!r) return;
    const v=mode==="track"?+r.dataset.v:r.dataset.v;
    if(has(v)) sel=sel.filter(x=>String(x)!==String(v));
    else{if(sel.length>=MAX){r.classList.remove("shake");void r.offsetWidth;r.classList.add("shake");toast("Можно выбрать до 3 — сначала убери одного",true);return}sel.push(v)}
    cb&&cb(sel.slice());paint();
  });
  document.addEventListener("keydown",e=>{if(e.key==="Escape"&&!el.hidden){e.preventDefault();e.stopImmediatePropagation();close()}},true);
  return{open,isOpen:()=>!el.hidden};
})();

/* ---------- страница «Мой профиль» ---------- */
const PF=(()=>{
  const el=$("#pf"),body=$("#pfBody"),btn=$("#meBtn");
  let closeT=0,edit=false,tab="all";
  const isOpen=()=>!el.hidden&&!el.classList.contains("out");
  const CAM='<svg class="ico" viewBox="0 0 24 24"><path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="4"/></svg>';
  const PEN='<svg class="ico" viewBox="0 0 24 24"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>';
  function stats(){
    const D=ME.data,r=state.tracks.filter(isRated);
    const avg=r.length?r.reduce((s,t)=>s+t.total,0)/r.length:0;
    const favs=(D.favTracks||[]).map(id=>r.find(t=>t.id===id)).filter(Boolean);
    const ga=new Map();r.forEach(t=>{const g=(t.genre||"").trim();if(g) ga.set(g.toLowerCase(),{n:(ga.get(g.toLowerCase())||{n:0}).n+1,g})});
    const genre=[...ga.values()].sort((a,b)=>b.n-a.n)[0];
    let arts=[];try{const gs=AP.groups();arts=(D.favArtists||[]).map(k=>gs.find(g=>g.key===k)).filter(Boolean)}catch(e){}
    return{n:r.length,avg,favs,genre,arts};
  }
  function render(keep){
    const D=ME.data,e=D.eq,s=stats(),sc=body.scrollTop,LV=ME.lvl();
    const owned=ITEMS.filter(i=>ME.owns(i.id)&&(tab==="all"||i.s===tab));
    const tabs=[["all","Все"],...SLOTS].map(([k,n])=>{const c=k==="all"?D.inv.length:ITEMS.filter(i=>i.s===k&&ME.owns(i.id)).length;
      return `<button class="psb${tab===k?" on":""}" data-tab="${k}" type="button">${n}${c?` <em>${c}</em>`:""}</button>`}).join("");
    const inv=owned.length?owned.map((it,i)=>itemCardH(it,i,true)).join("")
      :`<div class="pfe"><b>${D.inv.length?"В этой категории пусто":"Гардероб пока пуст"}</b>Зарабатывай пульсы за оценки и покупай предметы в магазине<button class="gbtn" data-a="shop" type="button">Открыть магазин</button></div>`;
    const card=(lab,val,sub)=>`<div class="pfs"><em>${lab}</em><b>${val}</b>${sub?`<span>${sub}</span>`:""}</div>`;
    const addRow=(t1,t2)=>`<div class="pffr emp"><div class="pfplus">+</div><div><b>${t1}</b><span>${t2}</span></div></div>`;
    const favT=s.favs.map((t,i)=>`<div class="pffr"><span class="pfno">${i+1}</span><div class="cv" style="${coverCss(t)}">${hasCov(t)?"":NOTE}</div><div><b>${esc(t.title)}</b><span>${esc(t.artist)}</span></div><i style="color:${rgCol(t.total)}">${fmt(t.total)}</i></div>`).join("")
      +(s.favs.length<3?addRow(s.favs.length?"Добавить ещё":"Выбрать треки",s.favs.length?`можно ещё ${3-s.favs.length}`:"от 1 до 3 из твоих оценок"):"");
    const favA=s.arts.map((g,i)=>{const n=g.items.filter(isRated).length;return `<div class="pffr"><span class="pfno">${i+1}</span>${AP.avatar(g.name)}<div><b>${esc(g.name)}</b><span>${n} ${plural(n,"оценка","оценки","оценок")}</span></div></div>`}).join("")
      +(s.arts.length<3?addRow(s.arts.length?"Добавить ещё":"Выбрать артистов",s.arts.length?`можно ещё ${3-s.arts.length}`:"от 1 до 3 из медиатеки"):"");
    const genH=s.genre?`<div class="pffr"><div class="pfg">${esc(s.genre.g.charAt(0).toUpperCase())}</div><div><b>${esc(s.genre.g)}</b><span>${s.genre.n} ${plural(s.genre.n,"трек","трека","треков")}</span></div></div>`:`<span class="pfn">Пока нет жанров</span>`;
    const idBlock=edit?`<div class="pfed">
        <label>Никнейм${ME.admin?' <span class="admpf sm">[ADMIN]</span>':""}<input id="pfNick" maxlength="40" value="${esc(D.nick)}" autocomplete="off" spellcheck="false"></label>
        <label>О себе<textarea id="pfBio" maxlength="160" rows="3" placeholder="Пара слов о себе и своей музыке">${esc(D.bio||"")}</textarea><span class="pfc" id="pfCnt">${(D.bio||"").length}/160</span></label>
        <div class="pfact"><button class="gbtn" data-a="save" type="button">Сохранить</button><button class="gbtn ghost" data-a="cancel" type="button">Отмена</button>${D.av?'<button class="gbtn ghost" data-a="noav" type="button">Убрать фото</button>':""}</div></div>`
      :`<div class="pfid"><div class="pfnm">${ME.nameH()}${e.badge?ME.badge(e.badge):""}</div>
        <div class="xpb"><span class="xpl">Уровень ${LV.L}</span><div class="xpt"><i style="width:${(LV.cur/LV.need*100).toFixed(1)}%"></i></div><em>${LV.cur} / ${LV.need} XP</em></div>
        ${D.bio?`<p class="pfbio">${esc(D.bio)}</p>`:`<p class="pfbio emp" data-a="edit">Расскажи о себе — пара слов о твоей музыке</p>`}
        <div class="pfact"><button class="gbtn" data-a="edit" type="button">${PEN}Изменить профиль</button><button class="gbtn ghost" data-a="shop" type="button">${COIN}Магазин</button></div></div>`;
    body.innerHTML=`${bannerHTML(e.bg,e.fx,"pfbn")}
      <div class="pfhd">
        <div class="pfav" data-a="av" title="Сменить фото — можно анимированный GIF">${ME.avatar(168)}<span class="pfcam">${CAM}</span><span class="pflv" title="Уровень">${LV.L}</span></div>
        ${idBlock}
        <div class="pfbal">${CBIG}<div><b data-bal>${ME.fmtN(D.bal)}</b><em>${plural(D.bal,"пульс","пульса","пульсов")} на балансе</em></div></div>
      </div>
      <div class="pfst">${card("Оценено",s.n,plural(s.n,"трек","трека","треков"))}${card("Средняя",s.n?fmt(s.avg):"—","по всем оценкам")}${card("Уровень",LV.L,ME.fmtN(D.xp||0)+" опыта всего")}${card("Заработано",ME.fmtN(D.earned||0),"пульсов всего")}</div>
      <div class="pffv">
        <div class="pff pick" data-pick="track" title="Выбрать любимые треки"><em>Любимые треки ${PEN}</em>${favT}</div>
        <div class="pff pick" data-pick="artist" title="Выбрать любимых артистов"><em>Любимые артисты ${PEN}</em>${favA}</div>
        <div class="pff"><em>Любимый жанр</em>${genH}</div></div>
      <div class="alh">Гардероб <span class="hint">— надевай купленные предметы</span></div>
      <div class="psort pftab">${tabs}</div>
      <div class="igrid inv">${inv}</div>`;
    if(keep) body.scrollTop=sc;
    FXP.scan(body);
    if(edit){const n=$("#pfNick");if(n&&!keep) setTimeout(()=>n.focus(),60)}
  }
  function refresh(){if(isOpen()&&!edit) render(true);else if(isOpen()){const b=body.querySelector(".pfav");if(b) b.innerHTML=ME.avatar(168)+b.querySelector(".pfcam").outerHTML+b.querySelector(".pflv").outerHTML}}
  async function commit(){
    const raw=($("#pfNick").value||"").trim(),b=($("#pfBio").value||"").trim().slice(0,160);
    if(raw&&await ME.tryAdmin(raw)){edit=false;render(true);toast("Режим администратора включён");return}
    const n=raw.replace(/^\s*[\[\(\{]\s*admin\s*[\]\)\}]\s*/i,"").slice(0,24);
    if(!n){toast("Никнейм не может быть пустым",true);$("#pfNick").focus();return}
    edit=false;ME.setInfo({nick:n,bio:b});
    try{localStorage.setItem("roomName",n)}catch(e){}
    toast("Профиль сохранён");
  }
  body.addEventListener("click",async e=>{
    const a=e.target.closest("[data-a]");
    if(a){
      const k=a.dataset.a;
      if(k==="edit"){edit=true;render(true)}
      else if(k==="cancel"){edit=false;render(true)}
      else if(k==="save") commit();
      else if(k==="shop"){close(true);SHOP.open()}
      else if(k==="noav"){delImg("me",1);ME.setInfo({av:0});edit=true;render(true)}
      else if(k==="av") pickImage(async f=>{
        if(!isImageFile(f)){toast("Это не картинка",true);return}
        if(f.type==="image/gif"||/\.gif$/i.test(f.name)){                // GIF загружаем как есть — аватарка остаётся анимированной
          if(f.size>8*1024*1024){toast("GIF слишком большой — до 8 МБ",true);return}
          try{const r=await fetch("/api/img/me/1",{method:"POST",body:f});if(!r.ok) throw 0;ME.setInfo({av:Date.now()});toast("Анимированная аватарка установлена")}
          catch(x){toast("Не удалось сохранить GIF",true)}
          return;
        }
        try{const b=await toJpeg(f,512);const r=await fetch("/api/img/me/1",{method:"POST",body:b});if(!r.ok) throw 0;ME.setInfo({av:Date.now()});toast("Фото профиля обновлено")}
        catch(x){toast("Не удалось сохранить фото",true)}
      });
      return;
    }
    const p=e.target.closest("[data-pick]");
    if(p){const m=p.dataset.pick,D=ME.data;PICK.open(m,m==="track"?D.favTracks:D.favArtists,v=>{ME.setInfo(m==="track"?{favTracks:v}:{favArtists:v});try{QA.ev("fav")}catch(e){}});return}
    const t=e.target.closest("[data-tab]");if(t){tab=t.dataset.tab;render(true);return}
    const q=e.target.closest("[data-eq]");if(q){ME.equip(q.dataset.eq);return}
  });
  body.addEventListener("input",e=>{if(e.target.id==="pfBio") $("#pfCnt").textContent=e.target.value.length+"/160"});
  body.addEventListener("keydown",e=>{
    if(e.target.id==="pfNick"&&e.key==="Enter"){e.preventDefault();commit()}
    if(edit&&e.key==="Escape"){e.preventDefault();e.stopPropagation();edit=false;render(true)}
  });
  function open(){
    clearTimeout(closeT);if(isOpen()) return;
    try{AP.close(true);TOP.close(true);RK.close(true);CT.close(true);LB.close(true);RM.close(true);SHOP.close(true)}catch(x){}
    edit=false;tab="all";el.classList.remove("out");el.hidden=false;document.body.classList.add("pg");btn.classList.add("on");
    render();body.scrollTop=0;
    if(document.activeElement&&document.activeElement.blur) document.activeElement.blur();
  }
  function close(keep){
    if(el.hidden||el.classList.contains("out")) return;
    if(keep!==true) document.body.classList.remove("pg");
    btn.classList.remove("on");el.classList.add("out");edit=false;
    closeT=setTimeout(()=>{el.hidden=true;el.classList.remove("out");body.innerHTML=""},280);
  }
  $("#pfClose").addEventListener("click",()=>close());
  btn.addEventListener("click",()=>isOpen()?close():open());
  document.addEventListener("keydown",e=>{if(e.key==="Escape"&&isOpen()&&!edit&&!PICK.isOpen()){e.preventDefault();close()}});
  return{open,close,refresh,isOpen};
})();

/* ---------- карточка предмета (магазин и гардероб) ---------- */
const GEM='<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 3h10l4 6-9 12L3 9z" fill="currentColor"/><path d="M3 9h18M9 3l3 18 3-18" stroke="rgba(0,0,0,.35)" stroke-width="1" fill="none"/></svg>';
function itemCardH(it,i,wardrobe){
  const own=ME.owns(it.id),on=own&&ME.eq()[it.s]===it.id,poor=!own&&ME.data.bal<it.p;
  const kit=KIT_OF[it.id],rc=kit?kit.c1:it.ach?"#ffd45a":ADMIN_ITEMS[it.id]||RAR[it.r].c;
  const rn=kit?`Коллекция «${kit.n}»`:it.ach?"За достижение":ADMIN_ITEMS[it.id]?"Только для админа":RAR[it.r].n;
  const btn=own?`<button class="itb${on?" on":""}" data-eq="${it.id}" type="button">${on?(wardrobe?"Снять":"Надето ✓"):"Надеть"}</button>`
    :`<button class="itb buy${poor?" poor":""}" data-buy="${it.id}" type="button">${COIN}<b>${ME.fmtN(it.p)}</b></button>`;
  const gem=kit||it.ach||ADMIN_ITEMS[it.id]?"":`<span class="rch">${GEM}${RAR[it.r].n}</span>`;
  return `<div class="itm r-${it.r}${own?" own":""}${on?" on":""}" data-id="${it.id}" style="--rc:${rc};--i:${Math.min(i,16)}">${itemPreview(it)}${gem}
    <div class="itx"><em>${rn}</em><b>${esc(it.n)}</b><span>${SLOT_ONE[it.s]}</span></div>${btn}</div>`;
}

/* ---------- страница «Магазин» ---------- */
const SHOP=(()=>{
  const el=$("#shop"),body=$("#shBody"),nav=$("#navShop"),pillB=$("#balPill");
  let closeT=0,tab="all",tryEq=null,rar="all";
  const isOpen=()=>!el.hidden&&!el.classList.contains("out");
  const kitOwned=k=>k.items.every(id=>ME.owns(id));
  const kitOn=k=>k.items.every(id=>ME.eq()[ITEM[id].s]===id);
  const SLOTI={hat:'<path d="M4 17h16M6 17c0-6 2.7-10 6-10s6 4 6 10"/>',frame:'<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="4.5"/>',bg:'<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 15l5-4 4 3 3-2 6 4"/>',
    name:'<path d="M5 18L10 6h1l5 12M7 14h7"/>',badge:'<circle cx="12" cy="10" r="5"/><path d="M9 14l-1.5 6L12 18l4.5 2L15 14"/>',fx:'<path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5L18 18M6 18l2.5-2.5M15.5 8.5L18 6"/>'};
  function kitCard(k,i){
    const e={};k.items.forEach(id=>e[ITEM[id].s]=id);
    const own=kitOwned(k),on=own&&kitOn(k),poor=!own&&ME.data.bal<k.p,des=k.design&&ST.design===k.design;
    const chips=k.items.map(id=>{const it=ITEM[id];return `<span class="kc" title="${SLOT_ONE[it.s]}"><svg class="ico" viewBox="0 0 24 24">${SLOTI[it.s]}</svg>${esc(it.n)}</span>`}).join("");
    const act=own?`<div class="kit2-btns"><button class="itb${on?" on":""}" data-kiteq="${k.id}" type="button">${on?"Надет ✓":"Надеть набор"}</button>
        ${k.design?`<button class="itb ghost${des?" on":""}" data-design="${k.design}" type="button">${des?"Дизайн включён":"Включить дизайн"}</button>`:""}</div>`
      :`<button class="kit2-buy${poor?" poor":""}" data-kit="${k.id}" type="button"><span>Купить коллекцию</span><b>${COIN}${ME.fmtN(k.p)}</b></button>`;
    return `<div class="kit2${own?" own":""}" data-kitc="${k.id}" style="--k1:${k.c1};--k2:${k.c2};--i:${i}">
      <div class="kit2-art">${bannerHTML(e.bg,e.fx,"kb")}
        <div class="kit2-hero">${ME.avatar(112,e)}<div class="kit2-nm">${ME.nameH(e)}${ME.badge(e.badge)}</div></div>
        <span class="kit2-tag">${own?"✓ В коллекции":k.design?`Эксклюзив · дизайн «${DESIGN_NAME[k.design]}»`:`Эксклюзив · в стиле ${esc(k.style)}`}</span></div>
      <div class="kit2-b"><div class="kit2-t"><b>${esc(k.n)}</b><span>${esc(k.d)}</span></div><div class="kit2-c">${chips}</div>${act}</div></div>`;
  }
  function paintTry(){
    const t=$("#shTry");if(!t) return;
    const e=Object.assign({},ME.eq(),tryEq||{});
    t.innerHTML=miniProfile(e,{av:96,sub:tryEq?"Примерка — так будет выглядеть профиль":"Наведи на предмет, чтобы примерить"});
    t.classList.toggle("trying",!!tryEq);
    FXP.scan(t);
  }
  function grid(){
    const list=ITEMS.filter(i=>!i.ex&&(tab==="all"||i.s===tab)&&(rar==="all"||i.r===rar)).slice().sort((a,b)=>"crelm".indexOf(a.r)-"crelm".indexOf(b.r)||a.p-b.p);
    if(!list.length) return `<div class="pfe"><b>Таких предметов нет</b>Попробуй другую категорию или редкость</div>`;
    return list.map((it,i)=>itemCardH(it,i)).join("");
  }
  function render(keep){
    const sc=body.scrollTop,D=ME.data;
    const tabs=[["all","Все"],...SLOTS].map(([k,n])=>`<button class="psb${tab===k?" on":""}" data-tab="${k}" type="button">${n}</button>`).join("");
    body.innerHTML=`<div class="phead"><div><div class="eyebrow">Пульсы</div><h2>Магазин</h2></div>
        <div class="shbal">${CBIG}<div><b data-bal>${ME.fmtN(D.bal)}</b><em>на балансе</em></div></div></div>
      <div class="alh">Коллекции дизайнов <span class="hint">— эксклюзивные наборы под каждый дизайн приложения, их нет в обычной продаже</span></div>
      <div class="kgrid2">${KITS.filter(k=>k.design).map(kitCard).join("")}</div>
      <div class="alh">Игровые коллекции <span class="hint">— наборы в стиле любимых игр: только оригинальная отрисовка, без логотипов и персонажей</span></div>
      <div class="kgrid2">${KITS.filter(k=>!k.design).map((k,i)=>kitCard(k,i)).join("")}</div>
      <div class="shw"><div class="shm">
        <div class="alh">Предметы <span class="hint">— ${ITEMS.filter(i=>!i.ex).length} штук, от обычных до мифических</span></div>
        <div class="psort shtab">${tabs}</div>
        <div class="rfl">${[["all","Любая редкость"],..."crelm".split("").map(k=>[k,RAR[k].n])].map(([k,n])=>`<button class="rfb${rar===k?" on":""}" data-rar="${k}" type="button" style="--rc:${k==="all"?"#fff":RAR[k].c}">${k==="all"?"":GEM}${n}<em>${ITEMS.filter(i=>!i.ex&&(k==="all"||i.r===k)).length}</em></button>`).join("")}</div>
        <div class="igrid" id="shGrid">${grid()}</div>
      </div><aside class="shtry"><div class="shtry-h">Примерочная</div><div id="shTry"></div>
        <button class="gbtn ghost" data-a="pf" type="button">Мой профиль</button></aside></div>`;
    paintTry();FXP.scan(body);
    if(keep) body.scrollTop=sc;
  }
  function refresh(){if(isOpen()) render(true)}
  function nope(b,need){
    b.classList.remove("shake");void b.offsetWidth;b.classList.add("shake");
    toast(`Не хватает ${ME.fmtN(need)} ${plural(need,"пульса","пульсов","пульсов")} — оценивай треки, чтобы заработать`,true);
  }
  function boom(b){
    const r=b.getBoundingClientRect();
    for(let i=0;i<16;i++){
      const s=document.createElement("i");s.className="shsp";s.style.left=(r.left+r.width/2)+"px";s.style.top=(r.top+r.height/2)+"px";document.body.append(s);
      const a=Math.random()*6.283,d=40+Math.random()*70;
      s.animate([{transform:"translate(-50%,-50%) scale(1)",opacity:1},{transform:`translate(calc(-50% + ${Math.cos(a)*d}px),calc(-50% + ${Math.sin(a)*d}px)) scale(.2)`,opacity:0}],{duration:600+Math.random()*300,easing:"cubic-bezier(.2,.8,.2,1)"}).onfinish=()=>s.remove();
    }
  }
  /* ---------- окно «Точно купить?» ---------- */
  const bc=$("#bc"),bcYes=$("#bcYes"),bcNo=$("#bcNo");
  let bcFn=null,bcT=0,bcAt=0;
  const bcOpen=()=>!bc.hidden&&!bc.classList.contains("out");
  function askBuy(o,fn){
    clearTimeout(bcT);bcFn=fn;bcAt=performance.now();
    $("#bcTitle").textContent=o.title;$("#bcName").textContent=o.name;$("#bcSub").textContent=o.sub||"";
    $("#bcPrice").innerHTML=COIN+ME.fmtN(o.price);$("#bcLeft").innerHTML=COIN+ME.fmtN(Math.max(0,ME.data.bal-o.price));
    bc.classList.remove("out");bc.hidden=false;
    if(document.activeElement&&document.activeElement.blur) document.activeElement.blur();
    bcYes.focus({preventScroll:true});
  }
  function bcClose(){
    if(bc.hidden||bc.classList.contains("out")) return;
    bcFn=null;bc.classList.add("out");bcT=setTimeout(()=>{bc.hidden=true;bc.classList.remove("out")},190);
  }
  bcYes.addEventListener("click",()=>{
    if(!bcFn||performance.now()-bcAt<350) return;          // защита от случайного двойного клика по «Купить»
    const f=bcFn;bcFn=null;bcClose();f();
  });
  bcNo.addEventListener("click",bcClose);
  bc.addEventListener("pointerdown",e=>{if(e.target===bc) bcClose()});
  document.addEventListener("keydown",e=>{
    if(!bcOpen()) return;
    if(e.key==="Escape"){e.preventDefault();e.stopImmediatePropagation();bcClose()}
    else if(e.key==="ArrowLeft"||e.key==="ArrowRight"||e.key==="Tab"){e.preventDefault();e.stopImmediatePropagation();(document.activeElement===bcYes?bcNo:bcYes).focus()}
    else if(e.key==="Enter"){if(e.repeat||performance.now()-bcAt<350) e.preventDefault();e.stopImmediatePropagation()}
    else e.stopImmediatePropagation();
  },true);
  function buyKit(k,kit){
    if(!ME.buy(kit.items,kit.p)){nope(k,kit.p-ME.data.bal);return}
    if(k.isConnected) boom(k);tryEq=null;ME.equipSet(kit.items);
    ME.card({n:0,label:"✦",quiet:true,noShop:true,ok:"Отлично",title:`Коллекция «${kit.n}» твоя`,sub:kit.design?"Все 6 предметов уже надеты. Можешь включить дизайн приложения в стиле коллекции — кнопка на карточке набора.":"Все 6 предметов уже надеты — загляни в профиль, чтобы полюбоваться."});
  }
  body.addEventListener("click",e=>{
    const rf=e.target.closest("[data-rar]");
    if(rf){rar=rf.dataset.rar;body.querySelectorAll(".rfb").forEach(x=>x.classList.toggle("on",x===rf));const g=$("#shGrid");g.classList.add("out");
      setTimeout(()=>{g.innerHTML=grid();g.classList.remove("out");FXP.scan(g)},150);return}
    const t=e.target.closest("[data-tab]");
    if(t){tab=t.dataset.tab;body.querySelectorAll(".shtab .psb").forEach(x=>x.classList.toggle("on",x===t));const g=$("#shGrid");g.classList.add("out");
      setTimeout(()=>{g.innerHTML=grid();g.classList.remove("out");FXP.scan(g)},150);return}
    const b=e.target.closest("[data-buy]");
    if(b){const it=ITEM[b.dataset.buy];
      if(ME.data.bal<it.p){nope(b,it.p-ME.data.bal);return}
      const kt=KIT_OF[it.id],rn=kt?`Коллекция «${kt.n}»`:ADMIN_ITEMS[it.id]?"Только для админа":((RAR[it.r]||{}).n||"");
      askBuy({title:"Купить предмет?",name:`«${it.n}»`,sub:`${SLOT_ONE[it.s]}${rn?" · "+rn:""}`,price:it.p},()=>{
        if(!ME.buy([it.id],it.p)){nope(b,it.p-ME.data.bal);return}
        if(b.isConnected) boom(b);tryEq=null;ME.equip(it.id,true);toast(`«${it.n}» куплено и надето`)});
      return}
    const k=e.target.closest("[data-kit]");
    if(k){const kit=KITS.find(x=>x.id===k.dataset.kit);
      if(ME.data.bal<kit.p){nope(k,kit.p-ME.data.bal);return}
      askBuy({title:"Купить коллекцию?",name:`«${kit.n}»`,sub:`${kit.items.length} предметов${kit.design?" и дизайн приложения":""}`,price:kit.p},()=>buyKit(k,kit));
      return}
    const ds=e.target.closest("[data-design]");
    if(ds){setST({design:ds.dataset.design});toast(`Дизайн «${DESIGN_NAME[ds.dataset.design]}» включён`);render(true);return}
    const q=e.target.closest("[data-eq]");if(q){tryEq=null;ME.equip(q.dataset.eq);return}
    const ke=e.target.closest("[data-kiteq]");if(ke){const kit=KITS.find(x=>x.id===ke.dataset.kiteq);tryEq=null;ME.equipSet(kit.items);toast(`Набор «${kit.n}» надет`);return}
    if(e.target.closest("[data-a=pf]")){close(true);PF.open()}
  });
  let tryT=0,tryP=0;
  body.addEventListener("pointerover",e=>{
    const c=e.target.closest(".itm");if(!c) return;
    clearTimeout(tryT);
    const it=ITEM[c.dataset.id],ne={[it.s]:it.id};
    if(JSON.stringify(ne)!==JSON.stringify(tryEq)){tryEq=ne;clearTimeout(tryP);tryP=setTimeout(paintTry,70)}   // не перерисовываем примерочную на каждую карточку, пока мышь летит
  });
  body.addEventListener("pointerout",e=>{
    const c=e.target.closest(".itm");if(!c||c.contains(e.relatedTarget)) return;
    clearTimeout(tryT);tryT=setTimeout(()=>{tryEq=null;paintTry()},180);
  });
  function open(){
    clearTimeout(closeT);if(isOpen()) return;
    try{AP.close(true);TOP.close(true);RK.close(true);CT.close(true);LB.close(true);RM.close(true);PF.close(true)}catch(x){}
    tab="all";rar="all";tryEq=null;el.classList.remove("out");el.hidden=false;document.body.classList.add("pg");nav.classList.add("on");pillB.classList.add("on");
    render();body.scrollTop=0;
    if(document.activeElement&&document.activeElement.blur) document.activeElement.blur();
  }
  function close(keep){
    if(el.hidden||el.classList.contains("out")) return;
    if(keep!==true) document.body.classList.remove("pg");
    nav.classList.remove("on");pillB.classList.remove("on");el.classList.add("out");bcClose();
    closeT=setTimeout(()=>{el.hidden=true;el.classList.remove("out");body.innerHTML=""},280);
  }
  $("#shClose").addEventListener("click",()=>close());
  nav.addEventListener("click",()=>isOpen()?close():open());
  pillB.addEventListener("click",()=>isOpen()?close():open());
  document.addEventListener("keydown",e=>{if(e.key==="Escape"&&isOpen()){e.preventDefault();close()}});
  return{open,close,refresh,isOpen};
})();



/* ---------- награды за достижения: эксклюзивные предметы ---------- */
Object.assign(HATS,{
  hq_streak:`<g class="hph"><g filter="url(#hf-glow)" opacity=".75">${[[22,16,28],[36,10,40],[50,6,52],[64,10,40],[78,16,28]].map(([x,y,h])=>flameP(x,y,h,10,"#ff6a00")).join("")}</g>
    ${[[22,16,28],[36,10,40],[50,6,52],[64,10,40],[78,16,28]].map(([x,y,h])=>flameP(x,y,h,8.5,"url(#hg-fire)")).join("")}
    ${[[36,10,22],[50,6,30],[64,10,22]].map(([x,y,h])=>flameP(x,y,h,4.5,"#fff3c4")).join("")}</g>
    <path d="M14 24 C30 15 70 15 86 24 L86 16 C70 7 30 7 14 16 Z" fill="url(#hg-gold2)" stroke="#8a5208" stroke-width="1.6"/>
    <text x="50" y="22" text-anchor="middle" font-size="9" font-weight="900" fill="#7a3a05" font-family="Segoe UI,sans-serif">7</text>`
});
Object.assign(BADGES,{
  gq_100:["#ffd45a","#b45309",'<text x="12" y="15.6" text-anchor="middle" font-size="9" font-weight="900" fill="#fff" font-family="Segoe UI,sans-serif">100</text>'],
  gq_green:["#22c55e","#14532d",'<circle cx="12" cy="12" r="8" fill="none" stroke="#fff" stroke-width="2" stroke-dasharray="3.2 2.1"/><circle cx="12" cy="12" r="4.4" fill="#fff"/><text x="12" y="14.2" text-anchor="middle" font-size="6" font-weight="900" fill="#14532d" font-family="Segoe UI,sans-serif">0</text>']
});
FXX.xq_heart={n:12,
  s(p,W,H,any){p.y=any?Math.random()*H:H+10;p.vy=-(16+Math.random()*24);p.r=5+Math.random()*7},
  d(ctx,p,dt,t,W,H){p.y+=p.vy*dt;p.x+=Math.sin(t*2+p.ph)*14*dt;if(p.y<-14) return false;
    const r=p.r*(1+.22*Math.max(0,Math.sin(t*7+p.ph))),a=Math.min(1,p.y/H*1.6)*.9;ctx.globalAlpha=a;ctx.fillStyle="#ff2d55";
    ctx.save();ctx.translate(p.x,p.y);ctx.beginPath();ctx.moveTo(0,r*.9);ctx.bezierCurveTo(-r*1.4,0,-r*.7,-r*1.1,0,-r*.35);ctx.bezierCurveTo(r*.7,-r*1.1,r*1.4,0,0,r*.9);ctx.fill();ctx.restore()},
  after(ctx,c,dt,t,W,H){                                    // кардиограмма поперёк баннера
    const mid=H*.58,amp=H*.22,ph=(t*.9)%1;ctx.globalAlpha=.85;ctx.strokeStyle="#ff4d6d";ctx.lineWidth=2;ctx.lineJoin="round";ctx.beginPath();
    for(let x=0;x<=W;x+=3){const q=((x/W)*2.2-ph*2.2+10)%1,g=(c0,w,a)=>a*Math.exp(-Math.pow((q-c0)/w,2));
      const v=g(.18,.03,.12)-g(.33,.012,.2)+g(.36,.014,1)-g(.395,.016,.3)+g(.6,.05,.25);x?ctx.lineTo(x,mid-v*amp):ctx.moveTo(x,mid-v*amp)}
    ctx.stroke()}
};
const AITEMS=[["gq_100","badge","Сотня","r100"],["fq_legend","frame","Легенда","r500"],["nq_perfect","name","Идеал","p10"],["hq_streak","hat","Огненная неделя","st7"],
  ["bq_lvl","bg","Вершина","l20"],["gq_green","badge","Зелёное поле","green"],["xq_heart","fx","Стальное сердце","pu10"]]
  .map(([id,s,n,ach])=>({id,s,n,r:"x",p:0,ex:true,ach}));
AITEMS.forEach(i=>{ITEMS.push(i);ITEM[i.id]=i});

/* ---------- ежедневные задания и достижения ---------- */
const QA=(()=>{
  const el=$("#qa"),nav=$("#navQst");
  const D=()=>ME.data;
  const today=()=>{const d=new Date();return d.getFullYear()+"-"+String(d.getMonth()+1).padStart(2,"0")+"-"+String(d.getDate()).padStart(2,"0")};
  const dayDiff=(a,b)=>Math.round((new Date(b+"T12:00")-new Date(a+"T12:00"))/864e5);
  const IC={
    note:'<path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>',
    star:'<path d="M12 3l2.7 5.6 6.1.8-4.4 4.2 1.1 6L12 16.8 6.5 19.6l1.1-6L3.2 9.4l6.1-.8z"/>',
    disc:'<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="2.5"/>',
    mic:'<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/>',
    crown:'<path d="M3 18h18M4 15l-1-9 5 4 4-6 4 6 5-4-1 9z"/>',
    flame:'<path d="M12 22c4 0 7-3 7-7 0-4-3-6-4-10-1 3-3 4-4 4 0-2-1-4-2-5-1 4-4 6-4 11 0 4 3 7 7 7z"/>',
    moon:'<path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z"/>',
    cal:'<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/>',
    check:'<path d="M20 6L9 17l-5-5"/>',
    coin:'<circle cx="12" cy="12" r="9"/><path d="M6 12h3l1.5-3 3 6 1.5-3h3"/>',
    bag:'<path d="M6 2L3 6v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6l-3-4z"/><path d="M3 6h18M16 10a4 4 0 0 1-8 0"/>',
    gem:'<path d="M7 3h10l4 6-9 12L3 9z"/><path d="M3 9h18"/>',
    share:'<circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><path d="M8.6 13.5l6.8 4M15.4 6.5l-6.8 4"/>',
    heart:'<path d="M12 21s-8-5-8-11a4.5 4.5 0 0 1 8-2.8A4.5 4.5 0 0 1 20 10c0 6-8 11-8 11z"/>',
    target:'<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/>',
    layers:'<path d="M12 2l10 5-10 5L2 7z"/><path d="M2 17l10 5 10-5M2 12l10 5 10-5"/>',
    up:'<path d="M12 19V5M5 12l7-7 7 7"/>',
    skull:'<path d="M12 3a8 8 0 0 0-8 8c0 3 1.5 4.5 3 5.5V20h10v-3.5c1.5-1 3-2.5 3-5.5a8 8 0 0 0-8-8z"/><circle cx="9" cy="11.5" r="1.5"/><circle cx="15" cy="11.5" r="1.5"/>',
    globe:'<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>',
    gift:'<rect x="3" y="8" width="18" height="13" rx="1"/><path d="M3 12h18M12 8v13M12 8S10 3 7.5 3 5 6 7.5 8M12 8s2-5 4.5-5S19 6 16.5 8"/>'
  };
  const TIER={b:["Бронза","#d9925a","#7a4321"],s:["Серебро","#dfe6ef","#6b7685"],g:["Золото","#ffcf55","#a8650c"],p:["Платина","#8ff3ff","#3a3fb8"]};
  /* ---------- задания ---------- */
  const QUESTS={
    rate3:{n:"Оцени 3 трека",d:"Любые новые треки",goal:3,r:30,x:40,i:"note"},
    rate5:{n:"Оцени 5 треков",d:"Любые новые треки",goal:5,r:50,x:60,i:"note"},
    ep:{n:"Оцени EP целиком",d:"Все треки одного EP (2–6 треков)",goal:1,r:60,x:80,i:"layers"},
    top:{n:"Поставь трек в топ",d:"Оцени трек на 8.5 или выше",goal:1,r:25,x:30,i:"up"},
    gem:{n:"Найди шедевр",d:"Поставь треку 9.0 или выше",goal:1,r:35,x:40,i:"star"},
    low:{n:"Будь строгим",d:"Поставь треку оценку ниже 5",goal:1,r:20,x:25,i:"skull"},
    art2:{n:"Расширь кругозор",d:"Оцени треки двух разных артистов",goal:2,r:30,x:40,i:"mic"},
    cat:{n:"Слушай на площадках",d:"Оцени трек из каталога Deezer / Apple Music",goal:1,r:30,x:40,i:"globe"},
    fav:{n:"Обнови свой топ",d:"Выбери любимые треки или артистов в профиле",goal:1,r:20,x:25,i:"heart"},
    share:{n:"Поделись оценкой",d:"Создай карточку для сторис или Telegram",goal:1,r:25,x:30,i:"share"},
    bet:{n:"Испытай удачу",d:"Сыграй в любую игру на пульсы",goal:1,r:15,x:20,i:"target"}
  };
  const BONUS={r:75,x:50};
  function hash(s){let h=2166136261;for(const c of s){h^=c.charCodeAt(0);h=Math.imul(h,16777619)}return h>>>0}
  function pickDay(day){
    let h=hash(day+"|pulse");const rnd=()=>{h=Math.imul(h^h>>>15,2246822507)>>>0;h=Math.imul(h^h>>>13,3266489909)>>>0;h^=h>>>16;return h/4294967296};
    const first=rnd()<.6?"rate3":"rate5";
    const rest=["ep","top","gem","low","art2","cat","fav","share","bet"];
    const must=rnd()<.5?"top":"ep";                     // каждый день — либо «топ», либо «EP»
    const pool=rest.filter(x=>x!==must);const second=pool[Math.floor(rnd()*pool.length)];
    return [first,must,second];
  }
  function q(){
    const d=D(),day=today();
    if(!d.q||d.q.day!==day){d.q={day,list:pickDay(day),p:{},c:{},bonus:false,arts:[]};ME.save()}
    return d.q;
  }
  function streak(){
    const d=D(),day=today();d.stk=d.stk||{last:"",n:0,best:0};
    if(d.stk.last===day) return;
    d.stk.n=d.stk.last&&dayDiff(d.stk.last,day)===1?d.stk.n+1:1;d.stk.last=day;d.stk.best=Math.max(d.stk.best||0,d.stk.n);ME.save();
  }
  function bump(id,by,set){
    const Q=q();if(!Q.list.includes(id)||Q.c[id]) return;
    const g=QUESTS[id].goal,was=Q.p[id]||0;if(was>=g) return;
    Q.p[id]=Math.min(g,set!=null?set:was+(by||1));ME.save();
    if(Q.p[id]>=g){toast(`Задание выполнено: «${QUESTS[id].n}» — забери награду`);paintDot()}
    else if(isOpen()) render();
  }
  function ev(type,o){
    o=o||{};const d=D();d.st=d.st||{};
    if(type==="rate"){
      const t=o.t;if(!t) return;
      bump("rate3");bump("rate5");
      if(t.total>=8.5) bump("top");if(t.total>=9) bump("gem");if(t.total<5) bump("low");
      if(o.cat) bump("cat");
      const Q=q(),a=low(t.artist);if(a&&a!=="—"&&!Q.arts.includes(a)){Q.arts.push(a);bump("art2",0,Q.arts.length)}
      const h=new Date().getHours();if(h<5) d.st.night=1;
    }
    else if(type==="ep") bump("ep");
    else if(type==="fav") bump("fav");
    else if(type==="share"){d.st.shares=(d.st.shares||0)+1;bump("share")}
    else if(type==="bet"){d.st.bets=(d.st.bets||0)+1;bump("bet")}
    else if(type==="win"){
      if(o.game==="roul"&&o.c==="z") d.st.green=(d.st.green||0)+1;
      if(o.game==="pulse") d.st.puMax=Math.max(d.st.puMax||0,o.m||0);
    }
    else if(type==="buy") d.st.buys=(d.st.buys||0)+1;
    ME.save();
    setTimeout(()=>checkAch(false),300);
    paintDot();if(isOpen()) render();
  }
  function claim(id){
    const Q=q(),Qd=QUESTS[id];if(!Qd||Q.c[id]||(Q.p[id]||0)<Qd.goal) return;
    Q.c[id]=1;D().qDone=(D().qDone||0)+1;
    ME.reward(Qd.r,Qd.x,{from:el.querySelector(`[data-claim="${id}"]`)});
    toast(`+${Qd.r} пульсов и +${Qd.x} опыта за «${Qd.n}»`);
    render();paintDot();setTimeout(()=>checkAch(false),300);
  }
  function claimBonus(){
    const Q=q();if(Q.bonus||!Q.list.every(id=>Q.c[id])) return;
    Q.bonus=true;ME.reward(BONUS.r,BONUS.x,{from:el.querySelector("[data-bonus]")});
    ME.card({n:BONUS.r,quiet:true,noShop:true,title:"Все задания дня выполнены!",sub:`Бонус дня: +${BONUS.r} пульсов и +${BONUS.x} опыта. Новые задания появятся завтра.`});
    render();paintDot();
  }
  /* ---------- достижения ---------- */
  const A=[
    ["r1","Первая оценка","Оцени свой первый трек","b","note",20,S=>S.n>=1],
    ["r10","Разогрев","10 оценок","b","note",50,S=>S.n>=10],
    ["r25","В ритме","25 оценок","s","note",80,S=>S.n>=25],
    ["r50","Полсотни","50 оценок","s","disc",120,S=>S.n>=50],
    ["r100","Сотня","100 оценок","g","disc",250,S=>S.n>=100,"gq_100"],
    ["r250","Меломан со стажем","250 оценок","g","disc",400,S=>S.n>=250],
    ["r500","Легенда медиатеки","500 оценок","p","crown",800,S=>S.n>=500,"fq_legend"],
    ["a10","Оценил 10 артистов","Треки 10 разных исполнителей","s","mic",80,S=>S.arts>=10],
    ["a25","Знаток сцены","25 разных исполнителей","g","mic",200,S=>S.arts>=25],
    ["a50","Энциклопедия","50 разных исполнителей","p","mic",400,S=>S.arts>=50],
    ["ep1","Первый EP","Оцени EP целиком","b","layers",50,S=>S.eps>=1],
    ["al1","Первый альбом","Оцени альбом целиком (7+ треков)","s","disc",100,S=>S.albs>=1],
    ["al5","Коллекционер альбомов","5 альбомов целиком","g","disc",300,S=>S.albs>=5],
    ["p10","Поставил 10/10","Идеальная оценка треку","g","star",150,S=>S.ten>=1,"nq_perfect"],
    ["h9","Ценитель шедевров","10 треков с оценкой 9+","g","star",200,S=>S.h9>=10],
    ["z1","Без пощады","Оценка 2.0 или ниже","s","skull",60,S=>S.zero>=1],
    ["mid","Золотая середина","Ровно 5.0","b","target",40,S=>S.mid>=1],
    ["g5","Всеядный","Треки 5 разных жанров","s","globe",80,S=>S.genres>=5],
    ["night","Ночная смена","Оценка между полуночью и 5 утра","s","moon",60,S=>S.night],
    ["st3","Три дня подряд","Заходи и оценивай 3 дня подряд","b","cal",50,S=>S.streak>=3],
    ["st7","Неделя в ритме","7 дней подряд","g","flame",200,S=>S.streak>=7,"hq_streak"],
    ["st30","Месяц без пропусков","30 дней подряд","p","flame",1000,S=>S.streak>=30],
    ["q10","Исполнитель заданий","Выполни 10 ежедневных заданий","s","check",100,S=>S.qDone>=10],
    ["q50","Трудоголик","50 ежедневных заданий","g","check",400,S=>S.qDone>=50],
    ["l5","Уровень 5","Набери 5 уровень","b","up",50,S=>S.lvl>=5],
    ["l10","Уровень 10","Набери 10 уровень","s","up",150,S=>S.lvl>=10],
    ["l20","Уровень 20","Набери 20 уровень","g","up",400,S=>S.lvl>=20,"bq_lvl"],
    ["e1k","Первая тысяча","Заработай 1 000 пульсов","s","coin",100,S=>S.earned>=1000],
    ["e10k","Богач","Заработай 10 000 пульсов","g","coin",300,S=>S.earned>=10000],
    ["buy1","Первая покупка","Купи что-нибудь в магазине","b","bag",30,S=>S.inv>=1],
    ["inv25","Модник","25 предметов в гардеробе","s","bag",150,S=>S.inv>=25],
    ["myth","Мифическая вещь","Купи мифический предмет","g","gem",250,S=>S.myth],
    ["kit","Коллекционер","Собери любую коллекцию целиком","g","gem",300,S=>S.kit],
    ["fav3","Мой топ","Выбери 3 любимых трека","b","heart",40,S=>S.fav3],
    ["sh1","Поделился","Создай карточку оценки","b","share",40,S=>S.shares>=1],
    ["green","Зелёный свет","Выиграй на зелёном в «Рулетке»","g","target",200,S=>S.green>=1,"gq_green"],
    ["pu5","Крепкое сердце","Забери ×5 и больше в «Пульсе»","s","heart",120,S=>S.puMax>=5],
    ["pu10","Стальные нервы","Забери ×10 и больше в «Пульсе»","g","heart",300,S=>S.puMax>=10,"xq_heart"]
  ].map(([id,n,d,t,i,r,f,item])=>({id,n,d,t,i,r,f,item}));
  function stats(){
    const d=D(),tr=state.tracks.filter(isRated),st=d.st||{};
    const arts=new Set(tr.map(t=>low(t.artist)).filter(a=>a&&a!=="—")),genres=new Set(tr.map(t=>low(t.genre)).filter(Boolean));
    let eps=0,albs=0;const ids=new Set(state.tracks.filter(t=>t.albumId).map(t=>t.albumId));
    ids.forEach(id=>{const a=albumInfo(id);if(a&&a.complete){if(a.n>=7) albs++;else if(a.n>=2) eps++}});
    const own=(d.inv||[]).filter(id=>ITEM[id]&&!ITEM[id].ach&&!ADMIN_ITEMS[id]);
    return{n:tr.length,arts:arts.size,genres:genres.size,eps,albs,ten:tr.filter(t=>t.total>=9.95).length,h9:tr.filter(t=>t.total>=9).length,
      zero:tr.filter(t=>t.total<=2).length,mid:tr.filter(t=>Math.abs(t.total-5)<.001).length,night:!!st.night,streak:(d.stk&&d.stk.n)||0,
      qDone:d.qDone||0,lvl:ME.lvl().L,earned:d.earned||0,inv:own.length,myth:own.some(id=>ITEM[id].r==="m"),
      kit:KITS.some(k=>k.items.every(id=>(d.inv||[]).includes(id))),fav3:(d.favTracks||[]).length>=3,shares:st.shares||0,green:st.green||0,puMax:st.puMax||0};
  }
  function checkAch(silent){
    const d=D();d.ach=d.ach||{};const S=stats(),got=[];
    for(const a of A) if(!d.ach[a.id]&&a.f(S)){d.ach[a.id]=Date.now();got.push(a)}
    if(!got.length) return;
    const sum=got.reduce((s,a)=>s+a.r,0),items=got.filter(a=>a.item).map(a=>a.item);
    items.forEach(id=>{if(!d.inv.includes(id)) d.inv.push(id)});
    ME.reward(sum,0);
    if(silent){
      ME.card({n:sum,quiet:true,noShop:true,title:`Открыто достижений: ${got.length}`,sub:`За то, что ты уже сделал в приложении. ${items.length?`Плюс эксклюзивн${items.length>1?"ые предметы":"ый предмет"}: ${items.map(id=>"«"+ITEM[id].n+"»").join(", ")}. `:""}Все достижения — в разделе «Задания».`});
    }else got.forEach(a=>ME.card({n:a.r,quiet:true,noShop:!a.item,title:`Достижение: «${a.n}»`,sub:a.d+(a.item?` · награда — эксклюзивный предмет «${ITEM[a.item].n}» (уже в гардеробе)`:"")}));
    if(isOpen()) render();
  }
  /* ---------- окно «Задания» ---------- */
  let tab="q";
  const isOpen=()=>!el.hidden&&!el.classList.contains("out");
  const medal=(a,on)=>{const T=TIER[a.t];return `<span class="qmed${on?"":" off"}" style="--m1:${T[1]};--m2:${T[2]}"><svg viewBox="0 0 24 24">${IC[a.i]}</svg></span>`};
  function untilMidnight(){const n=new Date(),m=new Date(n);m.setHours(24,0,0,0);const s=Math.max(0,(m-n)/1000|0);return `${s/3600|0} ч ${(s%3600)/60|0} мин`}
  function render(){
    const Q=q(),d=D(),sc=el.querySelector(".qbody")?el.querySelector(".qbody").scrollTop:0;
    const qs=Q.list.map(id=>{const x=QUESTS[id],p=Q.p[id]||0,done=p>=x.goal,cl=!!Q.c[id];
      return `<div class="qrow${done?" done":""}${cl?" cl":""}"><span class="qic"><svg viewBox="0 0 24 24">${IC[x.i]}</svg></span>
        <div class="qtx"><b>${x.n}</b><span>${x.d}</span><div class="qbar"><i style="width:${Math.min(100,p/x.goal*100)}%"></i></div></div>
        <div class="qrw"><em>${COIN}+${x.r}</em><em class="xp">+${x.x} XP</em></div>
        ${cl?`<span class="qok">✓ Получено</span>`:done?`<button class="qbtn" data-claim="${id}" type="button">Забрать</button>`:`<span class="qpr">${p} / ${x.goal}</span>`}</div>`}).join("");
    const allCl=Q.list.every(id=>Q.c[id]);
    const bonus=`<div class="qbonus${Q.bonus?" cl":allCl?" ready":""}"><span class="qic"><svg viewBox="0 0 24 24">${IC.gift}</svg></span><div class="qtx"><b>Бонус дня</b><span>Выполни все три задания</span></div>
      <div class="qrw"><em>${COIN}+${BONUS.r}</em><em class="xp">+${BONUS.x} XP</em></div>${Q.bonus?`<span class="qok">✓ Получено</span>`:`<button class="qbtn" data-bonus type="button"${allCl?"":" disabled"}>Открыть</button>`}</div>`;
    const S=stats(),nA=A.filter(a=>d.ach&&d.ach[a.id]).length;
    const ach=A.map(a=>{const on=!!(d.ach&&d.ach[a.id]);return `<div class="arow${on?" on":""}">${medal(a,on)}<div class="qtx"><b>${a.n}</b><span>${a.d}</span></div>
      <div class="arw"><em>${COIN}+${a.r}</em>${a.item?`<em class="ait" title="Эксклюзивный предмет">✦ ${esc(ITEM[a.item].n)}</em>`:""}</div></div>`}).join("");
    const stk=(d.stk&&d.stk.n)||0;
    el.innerHTML=`<div class="qcard2" role="dialog" aria-label="Задания и достижения"><div class="qhd"><div><b>Задания</b><span>🔥 Серия: ${stk} ${plural(stk,"день","дня","дней")} подряд · достижений ${nA} из ${A.length}</span></div>
      <button class="pround" data-x type="button" title="Закрыть (Esc)"><svg class="ico" viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg></button></div>
      <div class="psort qtabs"><button class="psb${tab==="q"?" on":""}" data-tab="q" type="button">Задания дня</button><button class="psb${tab==="a"?" on":""}" data-tab="a" type="button">Достижения <em>${nA}/${A.length}</em></button></div>
      <div class="qbody">${tab==="q"?`<div class="qlist">${qs}${bonus}</div><div class="qfoot">Новые задания через ${untilMidnight()}</div>`:`<div class="alist">${ach}</div>`}</div></div>`;
    const b=el.querySelector(".qbody");if(b) b.scrollTop=sc;
  }
  function open(){if(PAGE_CLOSE) PAGE_CLOSE();q();render();el.classList.remove("out");el.hidden=false;nav.classList.add("on");if(document.activeElement&&document.activeElement.blur) document.activeElement.blur()}
  function close(){if(el.hidden) return;nav.classList.remove("on");el.classList.add("out");setTimeout(()=>{el.hidden=true;el.classList.remove("out");el.innerHTML=""},220)}
  el.addEventListener("click",e=>{
    if(e.target===el||e.target.closest("[data-x]")){close();return}
    const c=e.target.closest("[data-claim]");if(c){claim(c.dataset.claim);return}
    if(e.target.closest("[data-bonus]")){claimBonus();return}
    const t=e.target.closest("[data-tab]");if(t){tab=t.dataset.tab;render()}
  });
  document.addEventListener("keydown",e=>{if(isOpen()&&e.key==="Escape"&&$("#rwd").hidden){e.preventDefault();e.stopImmediatePropagation();close()}},true);
  nav.addEventListener("click",()=>isOpen()?close():open());
  function paintDot(){
    const Q=q(),n=Q.list.filter(id=>!Q.c[id]&&(Q.p[id]||0)>=QUESTS[id].goal).length+(!Q.bonus&&Q.list.every(id=>Q.c[id])?1:0);
    let dot=nav.querySelector(".ndot");if(!dot){dot=document.createElement("span");dot.className="ndot";nav.append(dot)}
    dot.textContent=n;dot.hidden=!n;
  }
  function init(){
    streak();q();paintDot();
    const d=D();
    if(!d.achInit){d.achInit=true;ME.save();setTimeout(()=>checkAch(true),2500)}else setTimeout(()=>checkAch(false),2500);
    setInterval(()=>{if(D().q&&D().q.day!==today()){streak();q();paintDot();if(isOpen()) render()}},60000);
  }
  return{ev,open,close,init,isOpen,checkAch,A,stats};
})();

/* ---------- карточка «Поделиться»: картинка для сторис и Telegram ---------- */
const SH=(()=>{
  const el=$("#shr"),btn=$("#shareCur");
  let fmtK="story",cur=null,blob=null,busy=0,mode="t";      // mode: t — трек, a — альбом / EP
  const F='"Segoe UI Variable Display","Segoe UI",system-ui,sans-serif';
  const load=src=>new Promise(res=>{if(!src) return res(null);const im=new Image();if(/^https?:/.test(src)&&!src.startsWith(location.origin)) im.crossOrigin="anonymous";im.onload=()=>res(im);im.onerror=()=>res(null);im.src=src});
  function rr(c,x,y,w,h,r){c.beginPath();c.moveTo(x+r,y);c.arcTo(x+w,y,x+w,y+h,r);c.arcTo(x+w,y+h,x,y+h,r);c.arcTo(x,y+h,x,y,r);c.arcTo(x,y,x+w,y,r);c.closePath()}
  function fit(c,txt,max){if(c.measureText(txt).width<=max) return txt;let s=txt;while(s.length>1&&c.measureText(s+"…").width>max) s=s.slice(0,-1);return s+"…"}
  function cover(c,im,x,y,w,h){const s=Math.max(w/im.width,h/im.height),iw=im.width*s,ih=im.height*s;c.drawImage(im,x+(w-iw)/2,y+(h-ih)/2,iw,ih)}
  const coverUrl=t=>t.cover?`/api/img/cover/${t.id}?v=${t.cover}`:(t.albumId&&albCovUrl(t.albumId))||"";
  async function draw(t){
    const story=fmtK==="story",W=1080,H=story?1920:1350,cv=document.createElement("canvas");cv.width=W;cv.height=H;const c=cv.getContext("2d");
    const [im,av]=await Promise.all([load(coverUrl(t)),load(ME.avUrl())]);
    const cs=getComputedStyle(document.documentElement),g0=cs.getPropertyValue("--g0").trim()||"#c2002e",g2=cs.getPropertyValue("--g2").trim()||"#ff3b5c";
    const sc=t.total,col=rgCol(sc);
    // фон: размытая обложка
    c.fillStyle="#08060a";c.fillRect(0,0,W,H);
    if(im){c.save();c.filter="blur(70px) saturate(1.35) brightness(.55)";cover(c,im,-160,-160,W+320,H+320);c.restore()}
    else{const g=c.createLinearGradient(0,0,W,H);g.addColorStop(0,g2);g.addColorStop(.55,g0);g.addColorStop(1,"#0a0306");c.globalAlpha=.75;c.fillStyle=g;c.fillRect(0,0,W,H);c.globalAlpha=1}
    const vg=c.createLinearGradient(0,0,0,H);vg.addColorStop(0,"rgba(0,0,0,.25)");vg.addColorStop(.45,"rgba(0,0,0,.15)");vg.addColorStop(1,"rgba(0,0,0,.82)");c.fillStyle=vg;c.fillRect(0,0,W,H);
    // шапка
    const L=story?{hy:112,cw:580,cg:58,ty:90,ay:60,sy:200,py:36,ph:66,cy:92,rh:88,fy:130}:{hy:74,cw:380,cg:42,ty:74,ay:44,sy:156,py:26,ph:56,cy:62,rh:62,fy:70};
    let y=L.hy;
    c.fillStyle="rgba(255,255,255,.9)";c.font=`900 34px ${F}`;c.textAlign="center";c.textBaseline="alphabetic";
    if("letterSpacing" in c) c.letterSpacing="10px";c.fillText("PULSE RATER",W/2,y);if("letterSpacing" in c) c.letterSpacing="0px";
    // обложка
    const cw=L.cw,cx=(W-cw)/2,cy=y+L.cg;
    c.save();c.shadowColor="rgba(0,0,0,.7)";c.shadowBlur=80;c.shadowOffsetY=30;rr(c,cx,cy,cw,cw,36);c.fillStyle="#1a1a1f";c.fill();c.restore();
    c.save();rr(c,cx,cy,cw,cw,36);c.clip();
    if(im) cover(c,im,cx,cy,cw,cw);else{const g=c.createLinearGradient(cx,cy,cx+cw,cy+cw);g.addColorStop(0,g2);g.addColorStop(1,g0);c.fillStyle=g;c.fillRect(cx,cy,cw,cw);
      c.fillStyle="rgba(255,255,255,.85)";c.font=`900 ${cw*.36}px ${F}`;c.textBaseline="middle";c.fillText("♪",W/2,cy+cw/2);c.textBaseline="alphabetic"}
    c.restore();c.lineWidth=2;c.strokeStyle="rgba(255,255,255,.18)";rr(c,cx,cy,cw,cw,36);c.stroke();
    // название
    y=cy+cw+L.ty;
    c.fillStyle="#fff";c.font=`900 ${story?76:64}px ${F}`;c.fillText(fit(c,t.title,W-140),W/2,y);
    y+=L.ay;c.fillStyle="rgba(255,255,255,.72)";c.font=`600 ${story?40:34}px ${F}`;c.fillText(fit(c,t.artist+(t.beat?"  ·  prod. "+t.beat:""),W-160),W/2,y);
    // итог
    y+=L.sy;
    c.font=`900 ${story?190:150}px ${F}`;const sTxt=fmt(sc),sw=c.measureText(sTxt).width;
    c.save();c.shadowColor=col;c.shadowBlur=60;c.fillStyle=col;c.textAlign="right";c.fillText(sTxt,W/2+sw/2-40,y);c.restore();
    c.textAlign="left";c.fillStyle="rgba(255,255,255,.6)";c.font=`800 ${story?54:44}px ${F}`;c.fillText("/10",W/2+sw/2-28,y);
    const vd=(verdict(sc)||VERD[VERD.length-1])[1];c.textAlign="center";
    c.font=`900 ${story?38:32}px ${F}`;const vw=c.measureText(vd).width+64,vy=y+L.py;
    c.fillStyle=col;rr(c,(W-vw)/2,vy,vw,L.ph,L.ph/2);c.globalAlpha=.22;c.fill();c.globalAlpha=1;c.strokeStyle=col;c.lineWidth=3;c.stroke();
    c.fillStyle="#fff";c.fillText(vd,W/2,vy+L.ph*.68);
    // критерии
    y=vy+L.ph+L.cy;const bx=120,bw=W-240,rh=L.rh;
    t.scores.forEach((v,i)=>{
      const yy=y+i*rh;c.textAlign="left";c.fillStyle="rgba(255,255,255,.85)";c.font=`700 ${story?32:27}px ${F}`;c.fillText(CRIT[i][0],bx,yy);
      c.textAlign="right";c.fillStyle=rgCol(v);c.font=`900 ${story?34:29}px ${F}`;c.fillText(fmt(v),bx+bw,yy);
      const by=yy+(story?20:16),bh=story?14:12;c.fillStyle="rgba(255,255,255,.12)";rr(c,bx,by,bw,bh,bh/2);c.fill();
      c.fillStyle=rgCol(v);rr(c,bx,by,Math.max(bh,bw*v/10),bh,bh/2);c.fill();
    });
    // подпись
    const fy=H-L.fy,ar=story?44:36,nick=ME.data.nick||"Меломан";
    c.font=`800 ${story?36:30}px ${F}`;const nw=c.measureText(nick).width,tw=ar*2+20+Math.max(nw,c.measureText("x").width);
    let ax=(W-tw)/2+ar;
    c.save();c.beginPath();c.arc(ax,fy,ar,0,6.283);c.clip();if(av) cover(c,av,ax-ar,fy-ar,ar*2,ar*2);else{c.fillStyle=g2;c.fillRect(ax-ar,fy-ar,ar*2,ar*2);c.fillStyle="#fff";c.textAlign="center";c.textBaseline="middle";c.fillText(nick.trim().charAt(0).toUpperCase(),ax,fy+2);c.textBaseline="alphabetic"}c.restore();
    c.strokeStyle=g2;c.lineWidth=4;c.beginPath();c.arc(ax,fy,ar+3,0,6.283);c.stroke();
    c.textAlign="left";c.fillStyle="#fff";c.fillText(nick,ax+ar+20,fy-2);
    c.fillStyle="rgba(255,255,255,.55)";c.font=`600 ${story?26:22}px ${F}`;c.fillText(`уровень ${ME.lvl().L} · ${new Date().toLocaleDateString("ru-RU")}`,ax+ar+20,fy+(story?34:28));
    return cv;
  }
  /* ----- карточка альбома / EP ----- */
  function relOf(id){
    const a=albumInfo(id);if(!a) return null;
    const names=[];a.items.forEach(t=>artistNames(t).forEach(n=>{if(!names.some(x=>x.toLowerCase()===n.toLowerCase())) names.push(n)}));
    const art=names.length>2?names.slice(0,2).join(", ")+" и др.":names.join(", ")||"без исполнителя";
    const items=a.items.slice().sort(byOrd);
    const cv=albCovUrl(id)||(()=>{const t=items.find(x=>x.cover);return t?`/api/img/cover/${t.id}?v=${t.cover}`:""})();
    return Object.assign({},a,{art,items,cv,ep:a.n>=2&&a.n<=6});
  }
  async function drawRel(r){
    const story=fmtK==="story",W=1080,H=story?1920:1350,cv=document.createElement("canvas");cv.width=W;cv.height=H;const c=cv.getContext("2d");
    const [im,av]=await Promise.all([load(r.cv),load(ME.avUrl())]);
    const cs=getComputedStyle(document.documentElement),g0=cs.getPropertyValue("--g0").trim()||"#c2002e",g2=cs.getPropertyValue("--g2").trim()||"#ff3b5c";
    const sc=r.avg,col=r.done?rgCol(sc):"#9aa3b2";
    c.fillStyle="#08060a";c.fillRect(0,0,W,H);
    if(im){c.save();c.filter="blur(70px) saturate(1.35) brightness(.5)";cover(c,im,-160,-160,W+320,H+320);c.restore()}
    else{const g=c.createLinearGradient(0,0,W,H);g.addColorStop(0,g2);g.addColorStop(.55,g0);g.addColorStop(1,"#0a0306");c.globalAlpha=.75;c.fillStyle=g;c.fillRect(0,0,W,H);c.globalAlpha=1}
    const vg=c.createLinearGradient(0,0,0,H);vg.addColorStop(0,"rgba(0,0,0,.25)");vg.addColorStop(.4,"rgba(0,0,0,.2)");vg.addColorStop(1,"rgba(0,0,0,.85)");c.fillStyle=vg;c.fillRect(0,0,W,H);
    const L=story?{hy:112,cw:460,cg:56,ty:132,ay:58,sy:184,sf:170,py:34,ph:62,lg:74,fy:130,ly:52}:{hy:70,cw:290,cg:38,ty:104,ay:44,sy:138,sf:124,py:22,ph:52,lg:48,fy:66,ly:42};
    let y=L.hy;
    c.fillStyle="rgba(255,255,255,.9)";c.font=`900 ${story?34:30}px ${F}`;c.textAlign="center";
    if("letterSpacing" in c) c.letterSpacing="10px";c.fillText("PULSE RATER",W/2,y);if("letterSpacing" in c) c.letterSpacing="0px";
    // обложка с «пластинкой» за ней
    const cw=L.cw,cx=(W-cw)/2-cw*.12,cy=y+L.cg;
    c.save();c.translate(cx+cw*.62,cy+cw/2);
    const vgr=c.createRadialGradient(0,0,cw*.08,0,0,cw*.5);vgr.addColorStop(0,"#2a2a30");vgr.addColorStop(.12,"#111");vgr.addColorStop(1,"#050505");
    c.fillStyle=vgr;c.beginPath();c.arc(cw*.12,0,cw*.48,0,6.283);c.fill();
    c.strokeStyle="rgba(255,255,255,.06)";c.lineWidth=2;for(let k=0;k<7;k++){c.beginPath();c.arc(cw*.12,0,cw*(.2+k*.04),0,6.283);c.stroke()}
    c.fillStyle=g2;c.beginPath();c.arc(cw*.12,0,cw*.09,0,6.283);c.fill();c.restore();
    c.save();c.shadowColor="rgba(0,0,0,.75)";c.shadowBlur=70;c.shadowOffsetY=26;rr(c,cx,cy,cw,cw,28);c.fillStyle="#1a1a1f";c.fill();c.restore();
    c.save();rr(c,cx,cy,cw,cw,28);c.clip();
    if(im) cover(c,im,cx,cy,cw,cw);else{const g=c.createLinearGradient(cx,cy,cx+cw,cy+cw);g.addColorStop(0,g2);g.addColorStop(1,g0);c.fillStyle=g;c.fillRect(cx,cy,cw,cw);
      c.fillStyle="rgba(255,255,255,.85)";c.font=`900 ${cw*.34}px ${F}`;c.textBaseline="middle";c.fillText("♫",cx+cw/2,cy+cw/2);c.textBaseline="alphabetic"}
    c.restore();c.lineWidth=2;c.strokeStyle="rgba(255,255,255,.18)";rr(c,cx,cy,cw,cw,28);c.stroke();
    // название
    y=cy+cw+L.ty;c.textAlign="center";
    c.fillStyle="rgba(255,255,255,.6)";c.font=`800 ${story?26:22}px ${F}`;if("letterSpacing" in c) c.letterSpacing="6px";
    c.fillText((r.ep?"EP":"АЛЬБОМ")+` · ${r.n} ${plural(r.n,"ТРЕК","ТРЕКА","ТРЕКОВ")}`,W/2,cy+cw+L.ly);if("letterSpacing" in c) c.letterSpacing="0px";
    c.fillStyle="#fff";c.font=`900 ${story?72:58}px ${F}`;c.fillText(fit(c,r.name,W-140),W/2,y);
    y+=L.ay;c.fillStyle="rgba(255,255,255,.72)";c.font=`600 ${story?38:32}px ${F}`;c.fillText(fit(c,r.art,W-160),W/2,y);
    // итог
    y+=L.sy;c.font=`900 ${L.sf}px ${F}`;const sTxt=r.done?fmt(sc):"—",sw=c.measureText(sTxt).width;
    c.save();c.shadowColor=col;c.shadowBlur=60;c.fillStyle=col;c.textAlign="right";c.fillText(sTxt,W/2+sw/2-40,y);c.restore();
    c.textAlign="left";c.fillStyle="rgba(255,255,255,.6)";c.font=`800 ${story?50:42}px ${F}`;c.fillText("/10",W/2+sw/2-28,y);
    const vd=r.complete?((verdict(sc)||VERD[VERD.length-1])[1]+(r.ep?" · общая оценка EP":" · общая оценка альбома")):`оценено ${r.done} из ${r.n}`;
    c.textAlign="center";c.font=`900 ${story?32:27}px ${F}`;const vw=c.measureText(vd).width+64,vy=y+L.py;
    c.fillStyle=col;rr(c,(W-vw)/2,vy,vw,L.ph,L.ph/2);c.globalAlpha=.22;c.fill();c.globalAlpha=1;c.strokeStyle=col;c.lineWidth=3;c.stroke();
    c.fillStyle="#fff";c.fillText(vd,W/2,vy+L.ph*.66);
    // список треков
    const top=vy+L.ph+L.lg,bottom=H-L.fy-(story?96:70),n=r.items.length,cols=n>10?2:1,per=Math.ceil(n/cols);
    const rh=Math.min(story?66:56,(bottom-top)/per),fs=Math.max(18,Math.min(story?32:28,rh*.48)),gap=40,bx=100,cwid=(W-2*bx-(cols-1)*gap)/cols;
    let best=null;r.items.forEach(t=>{if(!t.unrated&&(!best||t.total>best.total)) best=t});
    r.items.forEach((t,i)=>{
      const ci=Math.floor(i/per),ri=i%per,x=bx+ci*(cwid+gap),yy=top+ri*rh+fs;
      c.textAlign="left";c.fillStyle="rgba(255,255,255,.45)";c.font=`800 ${fs*.85}px ${F}`;c.fillText(String(i+1).padStart(2,"0"),x,yy);
      const nx=x+fs*1.7,sTx=t.unrated?"—":fmt(t.total);
      c.font=`900 ${fs}px ${F}`;const sw2=c.measureText(sTx).width;
      c.fillStyle=t===best?"#fff":"rgba(255,255,255,.88)";c.font=`${t===best?800:600} ${fs}px ${F}`;c.fillText(fit(c,t.title+(t===best&&n>1?"  ★":""),cwid-fs*1.7-sw2-18),nx,yy);
      c.textAlign="right";c.fillStyle=t.unrated?"rgba(255,255,255,.35)":rgCol(t.total);c.font=`900 ${fs}px ${F}`;c.fillText(sTx,x+cwid,yy);
      const by=yy+Math.max(6,rh*.16),bh=Math.max(3,rh*.07);c.fillStyle="rgba(255,255,255,.09)";rr(c,nx,by,cwid-fs*1.7,bh,bh/2);c.fill();
      if(!t.unrated){c.fillStyle=rgCol(t.total);rr(c,nx,by,Math.max(bh,(cwid-fs*1.7)*t.total/10),bh,bh/2);c.fill()}
    });
    // подпись
    const fy=H-L.fy,ar=story?44:36,nick=ME.data.nick||"Меломан";
    c.font=`800 ${story?36:30}px ${F}`;const nw=c.measureText(nick).width,tw=ar*2+20+nw;
    const ax=(W-tw)/2+ar;
    c.save();c.beginPath();c.arc(ax,fy,ar,0,6.283);c.clip();if(av) cover(c,av,ax-ar,fy-ar,ar*2,ar*2);else{c.fillStyle=g2;c.fillRect(ax-ar,fy-ar,ar*2,ar*2);c.fillStyle="#fff";c.textAlign="center";c.textBaseline="middle";c.fillText(nick.trim().charAt(0).toUpperCase(),ax,fy+2);c.textBaseline="alphabetic"}c.restore();
    c.strokeStyle=g2;c.lineWidth=4;c.beginPath();c.arc(ax,fy,ar+3,0,6.283);c.stroke();
    c.textAlign="left";c.fillStyle="#fff";c.fillText(nick,ax+ar+20,fy-2);
    c.fillStyle="rgba(255,255,255,.55)";c.font=`600 ${story?26:22}px ${F}`;c.fillText(`уровень ${ME.lvl().L} · ${new Date().toLocaleDateString("ru-RU")}`,ax+ar+20,fy+(story?34:28));
    return cv;
  }
  async function render(){
    const my=++busy;el.querySelector(".shp").classList.add("ld");
    const cv=mode==="a"?await drawRel(relOf(cur.albumId)):await draw(cur);if(my!==busy) return;
    const box=el.querySelector(".shp");box.innerHTML="";cv.className="shcv";box.append(cv);box.classList.remove("ld");box.classList.toggle("post",fmtK!=="story");
    blob=await new Promise(r=>cv.toBlob(r,"image/png"));
  }
  function open(t,m){
    if(!t) return;
    mode=m||(t.unrated?"a":"t");
    if(mode==="t"&&t.unrated){toast("Сначала оцени трек",true);return}
    const R=t.albumId?relOf(t.albumId):null;
    if(mode==="a"&&(!R||!R.done)){toast("Сначала оцени хотя бы один трек альбома",true);return}
    cur=t;blob=null;
    const sw=R&&R.n>=2?`<div class="psort shm"><button class="psb${mode==="t"?" on":""}" data-m="t" type="button"${t.unrated?" disabled":""}>Этот трек</button><button class="psb${mode==="a"?" on":""}" data-m="a" type="button"${R.done?"":" disabled"}>Весь ${R.ep?"EP":"альбом"}</button></div>`:"";
    el.innerHTML=`<div class="shc" role="dialog" aria-label="Карточка оценки"><div class="qhd"><div><b>Поделиться оценкой</b><span id="shSub"></span></div>
      <button class="pround" data-x type="button" title="Закрыть (Esc)"><svg class="ico" viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg></button></div>
      <div class="shg"><div class="shp ld"></div><div class="shs">${sw}
        <div class="psort"><button class="psb${fmtK==="story"?" on":""}" data-f="story" type="button">Сторис 9:16</button><button class="psb${fmtK==="post"?" on":""}" data-f="post" type="button">Пост 4:5</button></div>
        <p id="shTxt"></p>
        <button class="gbtn" data-a="dl" type="button">Скачать PNG</button><button class="gbtn ghost" data-a="cp" type="button">Скопировать картинку</button>
        <span class="shn">Скопированную картинку можно сразу вставить в Telegram через Ctrl+V</span></div></div></div>`;
    el.classList.remove("out");el.hidden=false;subs();render();
  }
  function subs(){
    const R=cur.albumId?relOf(cur.albumId):null;
    $("#shSub").textContent=mode==="a"?`${R.ep?"EP":"Альбом"} «${R.name}» — ${R.art}`:`${cur.artist} — ${cur.title}`;
    $("#shTxt").textContent=mode==="a"?`Обложка, общая оценка ${R.ep?"EP":"альбома"}, все треки с оценками (лучший отмечен звездой) и твой ник. Подходит для сторис, Telegram и чатов.`:"Картинка с обложкой, итоговой оценкой, всеми критериями и твоим ником. Подходит для сторис, Telegram и чатов.";
  }
  function close(){if(el.hidden) return;el.classList.add("out");setTimeout(()=>{el.hidden=true;el.classList.remove("out");el.innerHTML=""},220)}
  async function getBlob(){for(let i=0;i<40&&!blob;i++) await new Promise(r=>setTimeout(r,100));return blob}
  el.addEventListener("click",async e=>{
    if(e.target===el||e.target.closest("[data-x]")){close();return}
    const mm=e.target.closest("[data-m]");if(mm&&!mm.disabled){mode=mm.dataset.m;el.querySelectorAll("[data-m]").forEach(b=>b.classList.toggle("on",b===mm));blob=null;subs();render();return}
    const f=e.target.closest("[data-f]");if(f){fmtK=f.dataset.f;el.querySelectorAll("[data-f]").forEach(b=>b.classList.toggle("on",b===f));blob=null;render();return}
    const a=e.target.closest("[data-a]");if(!a) return;
    const b=await getBlob();if(!b){toast("Не получилось нарисовать карточку",true);return}
    if(a.dataset.a==="dl"){
      const u=URL.createObjectURL(b),l=document.createElement("a");l.href=u;l.download=(mode==="a"?`${relOf(cur.albumId).art} — ${relOf(cur.albumId).name}`:`${cur.artist} — ${cur.title}`).replace(/[\\/:*?"<>|]+/g," ").slice(0,90)+".png";
      document.body.append(l);l.click();l.remove();setTimeout(()=>URL.revokeObjectURL(u),4000);toast("Картинка сохранена в «Загрузки»");
    }else{
      try{await navigator.clipboard.write([new ClipboardItem({"image/png":b})]);toast("Картинка скопирована — вставь её в Telegram (Ctrl+V)")}
      catch(x){toast("Не удалось скопировать — скачай PNG",true);return}
    }
    try{QA.ev("share")}catch(x){}
  });
  document.addEventListener("keydown",e=>{if(!el.hidden&&e.key==="Escape"){e.preventDefault();e.stopImmediatePropagation();close()}},true);
  btn.addEventListener("click",()=>open(state.tracks.find(x=>x.id===state.editing)));
  function openAlb(id){const t=state.tracks.filter(x=>x.albumId===id).sort(byOrd);if(t.length) open(t.find(x=>!x.unrated)||t[0],"a")}
  document.addEventListener("click",e=>{const b=e.target.closest("[data-ash]");if(!b) return;e.preventDefault();e.stopPropagation();openAlb(+b.dataset.ash||b.dataset.ash)},true);
  function paint(){const t=state.editing!=null&&state.tracks.find(x=>x.id===state.editing);btn.hidden=!t||(!!t.unrated&&!(t.albumId&&(albumInfo(t.albumId)||{}).done))}
  return{open,openAlb,close,paint};
})();


/* ---------- автообновление: программа сама проверяет новую версию ---------- */
const UPD=(()=>{
  let info=null,later="",busy=false;
  const bar=document.createElement("div");bar.id="updb";bar.hidden=true;document.body.append(bar);
  async function check(manual){
    try{
      const r=await fetch("/api/update/check",{cache:"no-store"});const j=await r.json();info=j;
      if(!r.ok) throw new Error(j.error||"err");
      if(j.available&&(manual||later!==j.latest)) show();
      else if(manual) toast(j.url?`У тебя последняя версия — ${j.cur}`:"Укажи ссылку на обновления в настройках",!j.url);
    }catch(e){if(manual) toast("Не удалось проверить обновления — нет интернета или ссылка неверная",true)}
    paintSet();
  }
  function show(){
    bar.innerHTML=`<div class="updi"><svg class="ico" viewBox="0 0 24 24"><path d="M12 3v12M7 10l5 5 5-5"/><path d="M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/></svg></div>
      <div class="updt"><b>Доступна версия ${esc(info.latest)}</b><span>${esc(info.notes||"Новые функции и исправления")}</span><em>сейчас ${esc(info.cur)} · оценки, пульсы и предметы сохранятся</em></div>
      <div class="upda"><button class="gbtn" data-u="go" type="button">Обновить</button><button class="gbtn ghost" data-u="later" type="button">Позже</button></div>`;
    bar.hidden=false;requestAnimationFrame(()=>bar.classList.add("on"));
  }
  function hide(){bar.classList.remove("on");setTimeout(()=>{bar.hidden=true},300)}
  async function apply(){
    if(busy) return;busy=true;
    bar.innerHTML=`<div class="updi spin"><svg class="ico" viewBox="0 0 24 24"><path d="M21 12a9 9 0 1 1-3-6.7L21 8"/><path d="M21 3v5h-5"/></svg></div><div class="updt"><b>Обновляю до ${esc(info.latest)}…</b><span>Приложение перезапустится само</span></div>`;
    try{
      const r=await fetch("/api/update/apply",{method:"POST"});const j=await r.json().catch(()=>({}));
      if(!r.ok) throw new Error(j.error||"err");
      bar.innerHTML=`<div class="updi"><svg class="ico" viewBox="0 0 24 24"><path d="M20 6L9 17l-5-5"/></svg></div><div class="updt"><b>Готово — перезапуск</b><span>Новое окно откроется через пару секунд</span>${j.path?`<em>Файл программы: ${esc(j.path)}</em>`:""}</div>`;
      setTimeout(()=>{try{window.close()}catch(e){}},1500);
    }catch(e){busy=false;toast("Обновление не удалось: "+(e.message==="err"?"попробуй позже":e.message),true);show()}
  }
  bar.addEventListener("click",e=>{const b=e.target.closest("[data-u]");if(!b) return;if(b.dataset.u==="go") apply();else{later=info&&info.latest||"";hide()}});
  /* блок в настройках */
  function paintSet(){
    const box=document.getElementById("updBox");if(!box) return;
    const i=info||{};
    box.innerHTML=`<div class="strow" style="cursor:default"><span class="stt"><b>Версия ${esc(i.cur||APPV)}</b><em>${i.available?`Доступна ${esc(i.latest)} — ${esc(i.notes||"")}`:i.url?(i.latest?"Установлена последняя версия":"Проверка при каждом запуске"):"Ссылка на обновления не указана"}</em></span>
      ${i.available?`<button type="button" class="updbtn hot" data-us="go">Обновить</button>`:`<button type="button" class="updbtn" data-us="chk">Проверить</button>`}</div>
      <label class="updurl"><span>Ссылка на свежий track_rater.py (GitHub)</span><input id="updUrl" type="text" spellcheck="false" placeholder="https://github.com/ник/pulse-rater/blob/main/track_rater.py" value="${esc(i.url||"")}"><button type="button" class="updbtn" data-us="save">Сохранить</button></label>`;
  }
  document.addEventListener("click",async e=>{
    const b=e.target.closest("[data-us]");if(!b) return;e.stopPropagation();
    if(b.dataset.us==="chk") check(true);
    else if(b.dataset.us==="go") apply();
    else if(b.dataset.us==="save"){
      const u=($("#updUrl")||{}).value||"";
      const r=await fetch("/api/update/url",{method:"POST",body:JSON.stringify({url:u}),headers:{"Content-Type":"application/json"}});
      const j=await r.json().catch(()=>({}));
      if(!r.ok){toast(j.error||"Ссылка не подходит — нужна https-ссылка на track_rater.py",true);return}
      toast(j.url?"Ссылка сохранена — проверяю обновления":"Ссылка удалена");check(true);
    }
  },true);
  function init(){setTimeout(()=>check(false),9000);setInterval(()=>check(false),6*3600*1000)}
  return{check,init,paintSet};
})();

/* ---------- плавность: если компьютер не успевает рисовать, анимации становятся легче ---------- */
(()=>{
  let prev=0,win=[],bad=0,t0=performance.now();
  const root=document.documentElement;
  function setLvl(l){
    try{localStorage.setItem("perfLvl",String(l))}catch(e){}
    if(l>=1&&!window.LOWFX){window.LOWFX=true;root.classList.add("lowfx")}
    if(l>=2&&!window.LOWFX2){window.LOWFX2=true;root.classList.add("lowfx2")}
    try{toast(l>=2?"Графика упрощена до минимума — компьютер не успевал рисовать фон":"Включён облегчённый режим графики — так будет плавнее")}catch(e){}
    try{dispatchEvent(new Event("st-change"))}catch(e){}
  }
  /* программная отрисовка (видеокарта не используется) — сразу самый лёгкий режим */
  try{
    const g=document.createElement("canvas").getContext("webgl"),x=g&&g.getExtension("WEBGL_debug_renderer_info");
    const r=g?String(g.getParameter(x?x.UNMASKED_RENDERER_WEBGL:g.RENDERER)):"SwiftShader";
    if(/swiftshader|llvmpipe|basic render|software/i.test(r)&&!window.LOWFX2) setTimeout(()=>setLvl(2),4000);
  }catch(e){}
  function f(t){
    if(window.LOWFX2) return;
    requestAnimationFrame(f);
    if(document.hidden||t-t0<3500||document.getElementById("intro")){prev=t;return}   // заставку не считаем
    if(prev){const d=t-prev;if(d<250) win.push(d)}prev=t;
    if(win.length>=90){
      win.sort((a,b)=>a-b);const p=win[Math.floor(win.length*.85)];win=[];
      if(p>22){if(++bad>=2){bad=0;setLvl(window.LOWFX?2:1)}}else bad=Math.max(0,bad-1);
    }
  }
  document.addEventListener("visibilitychange",()=>{prev=0;win=[]});
  requestAnimationFrame(f);
})();
/* ---------- пока открыто окно с размытым фоном, живой фон замирает (и не гоняет GPU зря) ---------- */
(()=>{
  const IDS=["intro","welcome","gm","qc","plat","emb","rwd","fpk","nwk","bc","shop","pf","qa","shr"];
  const root=document.documentElement;
  function chk(){
    let on=false;
    for(const id of IDS){const e=document.getElementById(id);if(e&&!e.hidden&&e.getClientRects().length){on=true;break}}
    if(on!==!!window.BGPAUSE){window.BGPAUSE=on;root.classList.toggle("bgp",on)}
  }
  chk();setInterval(chk,250);
})();
/* ---------- запуск ---------- */
(async()=>{
  state.tracks=(await loadTracks()).map(norm);
  try{const r=await fetch("/api/artists",{cache:"no-store"});if(r.ok){const j=await r.json();if(j&&typeof j==="object") state.artistImg=j}}catch(e){}
  paintMode();fillEditor(null);renderStats();renderLib();
  ME.load().then(()=>{try{QA.init()}catch(e){console.error(e)}});
  try{UPD.init()}catch(e){}
  setTimeout(async()=>{
    const miss=state.tracks.filter(t=>t.cat&&!t.cover);
    for(const t of miss) await saveCatCover(t.id,t.cat,"");
  },4000);
  if(location.protocol.startsWith("http")){
    setInterval(()=>fetch("/api/ping").catch(()=>{}),4000);
    addEventListener("pagehide",()=>navigator.sendBeacon("/api/bye"));
  }
})();
</script>
</body>
</html>
'''


# ---------- каталог релизов: открытые API Deezer и Apple Music (iTunes) ----------
# Берём только метаданные (названия, обложки, исполнителей, альбомы) и официальные 30-секундные превью.
# Полные треки не скачиваются — их оцениваем по каталогу.
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
CAT_HOSTS = ("dzcdn.net", "deezer.com", "mzstatic.com", "apple.com", "soundcloud.com", "sndcdn.com", "soundcloud.cloud")
CAT_CACHE = {}
STREAM_CACHE = {}
GENRE_RU = [
    ("hip hop", "Хип-хоп"), ("hip-hop", "Хип-хоп"), ("rap", "Хип-хоп"), ("r&b", "R&B"), ("k-pop", "K-pop"),
    ("pop", "Поп"), ("rock", "Рок"), ("dance", "Танцевальная"), ("electro", "Электроника"),
    ("alternative", "Альтернатива"), ("metal", "Метал"), ("jazz", "Джаз"), ("classic", "Классика"),
    ("latin", "Латино"), ("country", "Кантри"), ("folk", "Фолк"), ("blues", "Блюз"), ("soul", "Соул"),
    ("reggae", "Регги"), ("film", "Саундтреки"), ("soundtrack", "Саундтреки"), ("indie", "Инди"),
    ("african", "Африканская"), ("asian", "Азиатская"), ("kids", "Детская"), ("comedy", "Комедия"),
]


def genre_ru(name):
    low = (name or "").lower()
    for k, v in GENRE_RU:
        if k in low:
            return v
    return name or ""


def host_ok(url):
    try:
        p = urllib.parse.urlparse(url)
    except Exception:
        return False
    h = (p.hostname or "").lower()
    return p.scheme == "https" and any(h == d or h.endswith("." + d) for d in CAT_HOSTS)


def http_get(url, timeout=10, maxb=8 * 1024 * 1024):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(maxb + 1)[:maxb], r.headers.get("Content-Type", "")


YM_HEADERS = {"User-Agent": "Yandex-Music-API", "X-Yandex-Music-Client": "YandexMusicAndroid/24023231",
              "Accept": "application/json", "Accept-Language": "ru"}


def ym_get(url, ttl=3600):
    c = CAT_CACHE.get(url)
    if c and time.time() - c[0] < ttl:
        return c[1]
    req = urllib.request.Request(url, headers=YM_HEADERS)
    with urllib.request.urlopen(req, timeout=8) as r:
        data = json.loads(r.read(2 * 1024 * 1024).decode("utf-8", "replace"))
    CAT_CACHE[url] = (time.time(), data)
    return data


def norm_name(s):
    return re.sub(r"[^\w]+", "", (s or "").lower())


def cat_ymfans(name):
    """Слушатели за месяц из Яндекс Музыки (неофициальный API; при сбое — пустой ответ)."""
    name = (name or "").strip()[:120]
    if not name:
        return {"listeners": 0, "id": 0}
    try:
        res = ym_get("https://api.music.yandex.net/search?type=artist&page=0&text=" + urllib.parse.quote(name))
        arts = ((res.get("result") or {}).get("artists") or {}).get("results") or []
        if not arts:
            return {"listeners": 0, "id": 0}
        want = norm_name(name)
        hit = next((x for x in arts if norm_name(x.get("name")) == want), arts[0])
        aid = hit.get("id") or 0
        n = 0
        try:
            bi = ym_get(f"https://api.music.yandex.net/artists/{aid}/brief-info")
            n = int((((bi.get("result") or {}).get("stats")) or {}).get("lastMonthListeners") or 0)
        except Exception:
            pass
        return {"listeners": n, "id": aid}
    except Exception:
        return {"listeners": 0, "id": 0}


def cover_variants(url):
    out = [url]
    for sz in (600, 300):
        v = re.sub(r"/\d{2,4}x\d{2,4}", f"/{sz}x{sz}", url, count=1)
        if v not in out:
            out.append(v)
    return out


def fetch_cover(url):
    """Скачивает обложку; если большой размер недоступен — пробует поменьше."""
    last = None
    for v in cover_variants(url):
        try:
            body, ct = http_get(v, 12, MAX_IMG)
            if len(body) > 500 and (ct.startswith("image/") or body[:2] == b"\xff\xd8" or body[:4] == b"\x89PNG"):
                return body, ct if ct.startswith("image/") else "image/jpeg"
        except Exception as e:
            last = e
    raise last or RuntimeError("cover")


def jget(url, ttl=300):
    c = CAT_CACHE.get(url)
    if c and time.time() - c[0] < ttl:
        return c[1]
    body, _ = http_get(url)
    data = json.loads(body.decode("utf-8", "replace"))
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        raise RuntimeError(data["error"].get("message") or "api")
    if len(CAT_CACHE) > 500:
        CAT_CACHE.clear()
    CAT_CACHE[url] = (time.time(), data)
    return data


def art(url, size):
    return re.sub(r"\d+x\d+(bb)?\.(jpg|png)", f"{size}x{size}bb.jpg", url or "")


def dz_track(x, alb=None, art_=None):
    a = x.get("artist") or art_ or {}
    al = x.get("album") or alb or {}
    return {"k": f"dz:{x.get('id')}", "src": "dz", "id": x.get("id"),
            "title": x.get("title") or x.get("title_short") or "",
            "artist": a.get("name", ""), "artistId": a.get("id"),
            "album": al.get("title", ""), "albumId": al.get("id"),
            "cover": al.get("cover_medium") or al.get("cover") or "",
            "coverBig": al.get("cover_xl") or al.get("cover_big") or al.get("cover_medium") or "",
            "preview": x.get("preview") or "", "link": x.get("link") or "", "dur": x.get("duration", 0)}


def dz_artist(x):
    return {"src": "dz", "id": x.get("id"), "name": x.get("name", ""),
            "pic": x.get("picture_medium") or x.get("picture") or "",
            "picBig": x.get("picture_xl") or x.get("picture_big") or x.get("picture_medium") or "",
            "fans": x.get("nb_fan", 0), "link": x.get("link") or ""}


def dz_album(x, a=None):
    ar = x.get("artist") or a or {}
    return {"src": "dz", "id": x.get("id"), "title": x.get("title", ""), "artist": ar.get("name", ""),
            "artistId": ar.get("id"), "cover": x.get("cover_medium") or x.get("cover") or "",
            "coverBig": x.get("cover_xl") or x.get("cover_big") or x.get("cover_medium") or "",
            "year": (x.get("release_date") or "")[:4], "link": x.get("link") or "",
            "n": x.get("nb_tracks", 0), "type": x.get("record_type") or ""}


def it_track(x):
    cv = x.get("artworkUrl100") or ""
    return {"k": f"it:{x.get('trackId')}", "src": "it", "id": x.get("trackId"),
            "title": x.get("trackName", ""), "artist": x.get("artistName", ""), "artistId": x.get("artistId"),
            "album": x.get("collectionName", ""), "albumId": x.get("collectionId"),
            "cover": art(cv, 300), "coverBig": art(cv, 1000), "preview": x.get("previewUrl") or "",
            "link": x.get("trackViewUrl") or "", "dur": int((x.get("trackTimeMillis") or 0) / 1000),
            "genre": genre_ru(x.get("primaryGenreName"))}


def it_artist(x):
    return {"src": "it", "id": x.get("artistId"), "name": x.get("artistName", ""), "pic": "", "picBig": "",
            "fans": 0, "link": x.get("artistLinkUrl") or "", "genre": genre_ru(x.get("primaryGenreName"))}


def it_album(x):
    cv = x.get("artworkUrl100") or ""
    return {"src": "it", "id": x.get("collectionId"), "title": x.get("collectionName", ""),
            "artist": x.get("artistName", ""), "artistId": x.get("artistId"), "cover": art(cv, 300),
            "coverBig": art(cv, 1000), "year": (x.get("releaseDate") or "")[:4],
            "link": x.get("collectionViewUrl") or "", "n": x.get("trackCount", 0),
            "genre": genre_ru(x.get("primaryGenreName"))}


DZ = "https://api.deezer.com"
IT = "https://itunes.apple.com"


def cat_home(genre):
    g = int(genre or 0)
    with ThreadPoolExecutor(3) as ex:
        f1 = ex.submit(jget, f"{DZ}/chart/{g}/tracks?limit=30", 600)
        f2 = ex.submit(jget, f"{DZ}/chart/{g}/artists?limit=30", 600)
        f3 = ex.submit(jget, f"{DZ}/genre", 3600)
        tr, ar, gn = f1.result(), f2.result(), f3.result()
    return {"tracks": [dz_track(x) for x in tr.get("data", []) if x.get("id")],
            "artists": [dz_artist(x) for x in ar.get("data", []) if x.get("id")],
            "genres": [{"id": x["id"], "name": genre_ru(x["name"])} for x in gn.get("data", []) if x.get("id")]}


def cat_search(src, q):
    qs = urllib.parse.quote(q)
    if src == "it":
        base = f"{IT}/search?term={qs}&media=music&entity="
        with ThreadPoolExecutor(3) as ex:
            f1 = ex.submit(jget, base + "song&limit=30")
            f2 = ex.submit(jget, base + "musicArtist&limit=12")
            f3 = ex.submit(jget, base + "album&limit=12")
            tr, ar, al = f1.result(), f2.result(), f3.result()
        return {"tracks": [it_track(x) for x in tr.get("results", []) if x.get("trackId")],
                "artists": [it_artist(x) for x in ar.get("results", []) if x.get("artistId")],
                "albums": [it_album(x) for x in al.get("results", []) if x.get("collectionId")]}
    with ThreadPoolExecutor(3) as ex:
        f1 = ex.submit(jget, f"{DZ}/search?q={qs}&limit=30")
        f2 = ex.submit(jget, f"{DZ}/search/artist?q={qs}&limit=12")
        f3 = ex.submit(jget, f"{DZ}/search/album?q={qs}&limit=12")
        tr, ar, al = f1.result(), f2.result(), f3.result()
    return {"tracks": [dz_track(x) for x in tr.get("data", []) if x.get("id")],
            "artists": [dz_artist(x) for x in ar.get("data", []) if x.get("id")],
            "albums": [dz_album(x) for x in al.get("data", []) if x.get("id")]}


def cat_artist(src, aid):
    aid = int(aid)
    if src == "it":
        with ThreadPoolExecutor(2) as ex:
            f1 = ex.submit(jget, f"{IT}/lookup?id={aid}&entity=album&limit=100")
            f2 = ex.submit(jget, f"{IT}/lookup?id={aid}&entity=song&limit=50")
            al, so = f1.result().get("results", []), f2.result().get("results", [])
        head = next((x for x in al if x.get("wrapperType") == "artist"), {})
        return {"artist": it_artist(head) if head else {"src": "it", "id": aid, "name": "", "pic": "", "fans": 0},
                "top": [it_track(x) for x in so if x.get("wrapperType") == "track" and x.get("trackId")],
                "albums": [it_album(x) for x in al if x.get("wrapperType") == "collection"]}
    with ThreadPoolExecutor(3) as ex:
        f1 = ex.submit(jget, f"{DZ}/artist/{aid}")
        f2 = ex.submit(jget, f"{DZ}/artist/{aid}/top?limit=50")
        f3 = ex.submit(jget, f"{DZ}/artist/{aid}/albums?limit=100")
        a, top, albs = f1.result(), f2.result(), f3.result()
    return {"artist": dz_artist(a),
            "top": [dz_track(x) for x in top.get("data", []) if x.get("id")],
            "albums": [dz_album(x, a) for x in albs.get("data", []) if x.get("id")]}


def cat_album(src, alid):
    alid = int(alid)
    if src == "it":
        res = jget(f"{IT}/lookup?id={alid}&entity=song").get("results", [])
        head = next((x for x in res if x.get("wrapperType") == "collection"), {})
        al = it_album(head) if head else {"src": "it", "id": alid, "title": "", "artist": "", "cover": "", "coverBig": "", "year": "", "n": 0}
        tr = [it_track(x) for x in res if x.get("wrapperType") == "track" and x.get("trackId")]
        return {"album": al, "tracks": tr, "genre": al.get("genre", "")}
    a = jget(f"{DZ}/album/{alid}")
    al = dz_album(a)
    gn = ((a.get("genres") or {}).get("data") or [{}])[0].get("name", "")
    tr = [dz_track(x, alb=a, art_=a.get("artist")) for x in (a.get("tracks") or {}).get("data", []) if x.get("id")]
    return {"album": al, "tracks": tr, "genre": genre_ru(gn)}


# ---------- SoundCloud (вместо Deezer): поиск, исполнители, альбомы, полные треки ----------
SC_API = "https://api-v2.soundcloud.com"
SC = {"cid": None, "t": 0}
SC_LOCK = threading.Lock()
SC_GENRES = [("hiphoprap", "Хип-хоп"), ("pop", "Поп"), ("electronic", "Электроника"), ("rbsoul", "R&B"),
             ("rock", "Рок"), ("danceedm", "Танцевальная"), ("house", "Хаус"), ("techno", "Техно"),
             ("drumbass", "Драм-н-бейс"), ("indie", "Инди"), ("alternativerock", "Альтернатива"),
             ("latin", "Латино"), ("reggaeton", "Реггетон"), ("trap", "Трэп"), ("ambient", "Эмбиент")]


def sc_client_id(force=False):
    """Публичный client_id веб-версии SoundCloud: берём его из скриптов сайта и запоминаем на 6 часов."""
    if SC["cid"] and not force and time.time() - SC["t"] < 6 * 3600:
        return SC["cid"]
    html, _ = http_get("https://soundcloud.com/", 15, 4 * 1024 * 1024)
    html = html.decode("utf-8", "replace")
    scripts = re.findall(r'<script[^>]+src="(https://a-v2\.sndcdn\.com/assets/[^"]+\.js)"', html)
    m = re.search(r'client_id["\']?\s*[:=]\s*["\']([0-9A-Za-z]{32})["\']', html)
    if m:
        SC.update(cid=m.group(1), t=time.time())
        return SC["cid"]
    for s in reversed(scripts):
        try:
            js, _ = http_get(s, 15, 8 * 1024 * 1024)
        except Exception:
            continue
        m = re.search(r'client_id\s*[:=]\s*"([0-9A-Za-z]{32})"', js.decode("utf-8", "replace")) or \
            re.search(r'client_id=([0-9A-Za-z]{32})', js.decode("utf-8", "replace"))
        if m:
            SC.update(cid=m.group(1), t=time.time())
            return SC["cid"]
    raise RuntimeError("SoundCloud не отвечает")


def sc_get(path, ttl=300):
    for attempt in (0, 1):
        cid = sc_client_id(attempt == 1)
        url = f"{SC_API}{path}{'&' if '?' in path else '?'}client_id={cid}"
        try:
            return jget(url, ttl)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403) and attempt == 0:
                continue
            raise


def sc_art(u, size="t500x500"):
    return re.sub(r"-(large|t\d+x\d+|crop|original)\.(jpg|png)", f"-{size}.\\2", u or "")


def sc_track(x):
    u = x.get("user") or {}
    pm = x.get("publisher_metadata") or {}
    title = (x.get("title") or "").strip()
    artist = (pm.get("artist") or "").strip()
    if not artist:
        artist = u.get("username", "")
        if " - " in title:                      # «Исполнитель - Название» в заголовке
            a, b = title.split(" - ", 1)
            if a.strip() and b.strip():
                artist, title = a.strip(), b.strip()
    cv = x.get("artwork_url") or u.get("avatar_url") or ""
    tid = x.get("id")
    return {"k": f"sc:{tid}", "src": "sc", "id": tid, "title": title, "artist": artist, "artistId": u.get("id"),
            "album": pm.get("album_title") or "", "albumId": None,
            "cover": sc_art(cv, "t300x300"), "coverBig": sc_art(cv, "t500x500"),
            "preview": f"{SC_API}/tracks/{tid}/pulse-stream" if tid else "", "link": x.get("permalink_url") or "",
            "dur": int((x.get("full_duration") or x.get("duration") or 0) / 1000), "genre": genre_ru(x.get("genre") or ""),
            "plays": x.get("playback_count") or 0, "snip": x.get("policy") == "SNIP"}


def sc_artist(u):
    av = u.get("avatar_url") or ""
    return {"src": "sc", "id": u.get("id"), "name": u.get("username", ""), "pic": sc_art(av, "t300x300"),
            "picBig": sc_art(av, "t500x500"), "fans": u.get("followers_count") or 0, "link": u.get("permalink_url") or ""}


def sc_album(p):
    u = p.get("user") or {}
    tr = p.get("tracks") or []
    cv = p.get("artwork_url") or next((t.get("artwork_url") for t in tr if t.get("artwork_url")), "") or u.get("avatar_url") or ""
    return {"src": "sc", "id": p.get("id"), "title": p.get("title", ""), "artist": u.get("username", ""), "artistId": u.get("id"),
            "cover": sc_art(cv, "t300x300"), "coverBig": sc_art(cv, "t500x500"),
            "year": (p.get("release_date") or p.get("display_date") or p.get("created_at") or "")[:4],
            "link": p.get("permalink_url") or "", "n": p.get("track_count") or len(tr), "type": p.get("set_type") or ""}


def sc_ok(x):
    return x.get("id") and x.get("title") and x.get("kind", "track") == "track"


def sc_users_from(tracks):
    seen, out = set(), []
    for t in tracks:
        u = t.get("user") or {}
        if u.get("id") and u["id"] not in seen:
            seen.add(u["id"])
            out.append(sc_artist(u))
    return out


def sc_home(genre):
    g = int(genre or 0)
    slug = SC_GENRES[g - 1][0] if 0 < g <= len(SC_GENRES) else "all-music"
    tracks = []
    try:
        d = sc_get(f"/charts?kind=top&genre=soundcloud%3Agenres%3A{slug}&limit=50", 900)
        tracks = [c.get("track") or {} for c in d.get("collection", [])]
    except Exception:
        tracks = []
    tracks = [t for t in tracks if sc_ok(t)]
    if len(tracks) < 10:                        # чартов нет — берём самые прослушиваемые по запросу жанра
        q = SC_GENRES[g - 1][1] if g else "хит"
        d = sc_get(f"/search/tracks?q={urllib.parse.quote(slug if g else 'top hits')}&limit=50", 900)
        tracks = sorted([t for t in d.get("collection", []) if sc_ok(t)], key=lambda t: -(t.get("playback_count") or 0))
    return {"tracks": [sc_track(t) for t in tracks[:40]], "artists": sc_users_from(tracks)[:24],
            "genres": [{"id": i + 1, "name": n} for i, (_, n) in enumerate(SC_GENRES)]}


def sc_search(q):
    qs = urllib.parse.quote(q)
    with ThreadPoolExecutor(3) as ex:
        f1 = ex.submit(sc_get, f"/search/tracks?q={qs}&limit=30")
        f2 = ex.submit(sc_get, f"/search/users?q={qs}&limit=12")
        f3 = ex.submit(sc_get, f"/search/albums?q={qs}&limit=12")
        tr = f1.result()
        try:
            ar = f2.result()
        except Exception:
            ar = {}
        try:
            al = f3.result()
        except Exception:
            al = {}
    return {"tracks": [sc_track(x) for x in tr.get("collection", []) if sc_ok(x)],
            "artists": [sc_artist(x) for x in ar.get("collection", []) if x.get("id")],
            "albums": [sc_album(x) for x in al.get("collection", []) if x.get("id")]}


def sc_full_tracks(items):
    """В плейлистах SoundCloud часть треков приходит «заглушками» — дозапрашиваем их пачками."""
    miss = [str(t.get("id")) for t in items if t.get("id") and not t.get("title")]
    full = {}
    for i in range(0, len(miss), 50):
        try:
            for t in sc_get("/tracks?ids=" + ",".join(miss[i:i + 50]), 900):
                full[t.get("id")] = t
        except Exception:
            pass
    return [full.get(t.get("id"), t) for t in items]


def sc_artist_page(aid):
    aid = int(aid)
    with ThreadPoolExecutor(3) as ex:
        f1 = ex.submit(sc_get, f"/users/{aid}")
        f2 = ex.submit(sc_get, f"/users/{aid}/toptracks?limit=50")
        f3 = ex.submit(sc_get, f"/users/{aid}/albums?limit=50")
        u = f1.result()
        try:
            top = f2.result().get("collection", [])
        except Exception:
            top = []
        try:
            albs = f3.result().get("collection", [])
        except Exception:
            albs = []
    if not top:
        try:
            top = sc_get(f"/users/{aid}/tracks?limit=50").get("collection", [])
        except Exception:
            top = []
    if not albs:
        try:
            albs = sc_get(f"/users/{aid}/playlists_without_albums?limit=30").get("collection", [])
        except Exception:
            albs = []
    return {"artist": sc_artist(u), "top": [sc_track(x) for x in top if sc_ok(x)],
            "albums": [sc_album(x) for x in albs if x.get("id")]}


def sc_album_page(alid):
    p = sc_get(f"/playlists/{int(alid)}?representation=full")
    items = sc_full_tracks(p.get("tracks") or [])
    al = sc_album(p)
    tr = [sc_track(x) for x in items if sc_ok(x)]
    for t in tr:
        if not t["album"]:
            t["album"] = al["title"]
    gn = next((t["genre"] for t in tr if t["genre"]), "")
    return {"album": al, "tracks": tr, "genre": gn}


def sc_resolve(u):
    """Ссылка транскодинга SoundCloud → настоящая ссылка на файл/плейлист (нужен client_id)."""
    for attempt in (0, 1):
        cid = sc_client_id(attempt == 1)
        try:
            body, _ = http_get(u + ("&" if "?" in u else "?") + "client_id=" + cid, 12, 1024 * 1024)
            return json.loads(body.decode("utf-8")).get("url") or ""
        except urllib.error.HTTPError as e:
            if e.code in (401, 403) and attempt == 0:
                continue
            raise
    return ""


def sc_fetch_audio(tid):
    """Скачивает трек целиком. SoundCloud почти перестал отдавать обычный mp3 (progressive),
    поэтому основной путь — HLS: плейлист из кусочков, которые склеиваются в один файл."""
    x = sc_get(f"/tracks/{int(tid)}", 60)
    auth = x.get("track_authorization")
    tcs = (x.get("media") or {}).get("transcodings", []) or []

    def kind(t):
        f = t.get("format") or {}
        pr, mt = f.get("protocol", ""), (f.get("mime_type") or "").lower()
        if "encrypted" in pr:
            return 99                                   # зашифрованные (Go+) не берём
        if pr == "progressive":
            return 0
        if pr == "hls" and mt.split(";")[0].strip() == "audio/mpeg":
            return 1
        if pr == "hls" and ("mp4" in mt or "aac" in mt):
            return 2                                    # AAC: init.mp4 + кусочки .m4s склеиваются в обычный mp4
        return 99

    order = sorted([t for t in tcs if kind(t) < 99], key=kind)
    if not order:
        raise RuntimeError("SoundCloud не даёт слушать этот трек")
    last = None
    for t in order:
        try:
            u = t["url"]
            if auth:
                u += ("&" if "?" in u else "?") + "track_authorization=" + urllib.parse.quote(auth)
            real = sc_resolve(u)
            if not real or not host_ok(real):
                continue
            mt = ((t.get("format") or {}).get("mime_type") or "audio/mpeg").split(";")[0].strip()
            if kind(t) == 0:
                body, _ = http_get(real, 40, 40 * 1024 * 1024)
                return body, "audio/mpeg"
            pl, _ = http_get(real, 15, 2 * 1024 * 1024)
            lines = pl.decode("utf-8", "replace").splitlines()
            parts = []
            for ln in lines:
                ln = ln.strip()
                m = re.match(r'#EXT-X-MAP:.*URI="([^"]+)"', ln)
                if m:
                    parts.append(urllib.parse.urljoin(real, m.group(1)))
                elif ln and not ln.startswith("#"):
                    parts.append(urllib.parse.urljoin(real, ln))
            if not parts or len(parts) > 2000 or not all(host_ok(q) for q in parts):
                continue
            with ThreadPoolExecutor(8) as ex:
                chunks = list(ex.map(lambda q: http_get(q, 20, 8 * 1024 * 1024)[0], parts))
            body = b"".join(chunks)
            if len(body) < 2000:
                continue
            return body, ("audio/mp4" if kind(t) == 2 else "audio/mpeg")
        except Exception as e:
            last = e
    raise last or RuntimeError("SoundCloud не отдал аудио")


def cat_cover_url(k):
    """Ссылка на большую обложку трека по ключу каталога dz:<id> / it:<id>."""
    src, _, tid = str(k).partition(":")
    tid = int(tid)
    if src == "dz":
        d = jget(f"{DZ}/track/{tid}", 3600)
        al = d.get("album") or {}
        return al.get("cover_xl") or al.get("cover_big") or al.get("cover_medium") or al.get("cover") or ""
    if src == "it":
        res = jget(f"{IT}/lookup?id={tid}", 3600).get("results", [])
        return art(res[0].get("artworkUrl100", ""), 1000) if res else ""
    if src == "sc":
        x = sc_get(f"/tracks/{tid}", 3600)
        return sc_art(x.get("artwork_url") or (x.get("user") or {}).get("avatar_url") or "", "t500x500")
    return ""


def cat_genre(album_id):
    a = jget(f"{DZ}/album/{int(album_id)}")
    gn = ((a.get("genres") or {}).get("data") or [{}])[0].get("name", "")
    return {"genre": genre_ru(gn), "year": (a.get("release_date") or "")[:4]}


def load_settings():
    try:
        d = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if isinstance(d, dict):
            return {k: v for k, v in d.items() if isinstance(k, str) and len(k) < 24
                    and (isinstance(v, (bool, int, float)) or (isinstance(v, str) and len(v) < 40))}
    except Exception:
        pass
    return {}


def page_html():
    data = json.dumps(load_settings(), ensure_ascii=False).replace("</", "<\\/")
    return HTML.replace("/*ST*/{}/*END*/", data, 1)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body=b"", ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _local(self):
        host = self.headers.get("Host", "")
        return host.startswith("127.0.0.1") or host.startswith("localhost")

    def _img_ref(self):
        m = re.match(r"^/api/img/(cover|artist|album|me)/([a-z0-9]{1,24})(?:\?.*)?$", self.path)
        return (m.group(1), m.group(2)) if m else None

    def _img_get(self, kind, iid):
        f = IMG_DIR / f"{kind}_{iid}.jpg"
        if not f.exists():
            return self._send(404)
        body = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/gif" if body[:3] == b"GIF" else "image/jpeg")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=31536000")
        self.end_headers()
        self.wfile.write(body)

    def _audio_id(self):
        m = re.match(r"^/api/audio/(\d{1,20})(?:\?.*)?$", self.path)
        return m.group(1) if m else None

    def _audio_get(self, aid):
        f = ctype = None
        for ext, ct in AUDIO_TYPES.items():
            c = AUDIO_DIR / f"{aid}.{ext}"
            if c.exists():
                f, ctype = c, ct
                break
        if not f:
            return self._send(404)
        size = f.stat().st_size
        start, end, code = 0, size - 1, 200
        rng = self.headers.get("Range")
        if rng and size:
            m = re.match(r"bytes=(\d*)-(\d*)$", rng.strip())
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    if m.group(2):
                        end = min(int(m.group(2)), size - 1)
                else:
                    start = max(0, size - int(m.group(2)))
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                code = 206
        length = end - start + 1
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-cache")
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with open(f, "rb") as fh:
                fh.seek(start)
                left = length
                while left > 0:
                    chunk = fh.read(min(65536, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def do_DELETE(self):
        if not self._local():
            return self._send(403)
        ref = self._img_ref()
        if ref:
            try:
                (IMG_DIR / f"{ref[0]}_{ref[1]}.jpg").unlink()
            except OSError:
                pass
            return self._send(200, b"{}")
        aid = self._audio_id()
        if not aid:
            return self._send(404)
        for ext in AUDIO_TYPES:
            try:
                (AUDIO_DIR / f"{aid}.{ext}").unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
        self._send(200, b"{}")

    def _room_api(self):
        """Действия хозяина комнаты — только с этого компьютера (создать комнату, сменить трек)."""
        try:
            n = min(int(self.headers.get("Content-Length", 0)), 20000)
            d = json.loads(self.rfile.read(n).decode("utf-8"))
            if not isinstance(d, dict):
                raise ValueError
        except Exception:
            return self._send(400)
        if self.path.startswith("/api/room/port"):
            port = room_server()
            return self._send(200 if port else 500, json.dumps({"port": port}).encode("utf-8"))
        tr = room_track(d.get("track"))
        if self.path.startswith("/api/room/create"):
            crit = room_crit(d.get("crit"))
            if not crit:
                return self._send(400)
            port = room_server()
            if not port:
                return self._send(500, json.dumps({"msg": "Не удалось открыть порт для комнаты"}).encode("utf-8"))
            try:
                start = min(10.0, max(0.0, float(d.get("start", 5))))
            except (TypeError, ValueError):
                start = 5.0
            r, pid = room_create(tr, crit, start, bool(d.get("share")), d.get("name"), bool(d.get("dec")))
            return self._send(200, json.dumps({"code": r["code"], "key": r["key"], "pid": pid, "port": port, "ips": lan_ips()}).encode("utf-8"))
        if self.path.startswith("/api/room/next"):
            with ROOM_LOCK:
                r = ROOMS.get(str(d.get("code") or "").upper())
                if not r or not secrets.compare_digest(str(d.get("key") or ""), r["key"]):
                    return self._send(404)
                room_next(r, tr, bool(d.get("share")), bool(d.get("dec")))
            return self._send(200, b"{}")
        self._send(404)

    def _send_bytes(self, body, ctype):
        size = len(body)
        start, end, code = 0, size - 1, 200
        rng = self.headers.get("Range")
        if rng and size:
            m = re.match(r"bytes=(\d*)-(\d*)$", rng.strip())
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    if m.group(2):
                        end = min(int(m.group(2)), size - 1)
                else:
                    start = max(0, size - int(m.group(2)))
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                code = 206
        chunk = body[start:end + 1]
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(chunk)))
        self.send_header("Cache-Control", "no-cache")
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _cat_get(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        try:
            if u.path == "/api/cat/img":
                url = q.get("u", "")
                if not host_ok(url):
                    return self._send(400)
                body, ct = fetch_cover(url)
                return self._send(200, body, ct)
            if u.path == "/api/cat/stream":
                url = q.get("u", "")
                if not host_ok(url):
                    return self._send(400)
                c = STREAM_CACHE.get(url)
                if not c:
                    m = re.match(r"^https://api-v2\.soundcloud\.com/tracks/(\d+)/pulse-stream$", url)
                    if m:                                     # SoundCloud: целый трек, ссылку получаем заново
                        with SC_LOCK:                         # браузер шлёт несколько запросов сразу — качаем один раз
                            c = STREAM_CACHE.get(url)
                            if not c:
                                body, ct = sc_fetch_audio(m.group(1))
                                if len(STREAM_CACHE) > 6:
                                    STREAM_CACHE.pop(next(iter(STREAM_CACHE)))
                                STREAM_CACHE[url] = c = (body, ct)
                        return self._send_bytes(*c)
                    body, ct = http_get(url, 15, 12 * 1024 * 1024)
                    if not ct.startswith("audio"):
                        ct = "audio/mpeg"
                    if len(STREAM_CACHE) > 24:
                        STREAM_CACHE.pop(next(iter(STREAM_CACHE)))
                    STREAM_CACHE[url] = c = (body, ct)
                return self._send_bytes(*c)
            src = {"it": "it", "dz": "dz"}.get(q.get("src"), "sc")
            if u.path == "/api/cat/ymfans":
                data = cat_ymfans(q.get("name", ""))
            elif u.path == "/api/cat/home":
                data = sc_home(q.get("genre", 0)) if src != "dz" else cat_home(q.get("genre", 0))
            elif u.path == "/api/cat/search":
                text = q.get("q", "").strip()[:120]
                if not text:
                    return self._send(400, b'{"error":"empty"}')
                data = sc_search(text) if src == "sc" else cat_search(src, text)
            elif u.path == "/api/cat/artist":
                data = sc_artist_page(q.get("id", "")) if src == "sc" else cat_artist(src, q.get("id", ""))
            elif u.path == "/api/cat/album":
                data = sc_album_page(q.get("id", "")) if src == "sc" else cat_album(src, q.get("id", ""))
            elif u.path == "/api/cat/genre":
                data = cat_genre(q.get("album", ""))
            else:
                return self._send(404)
            return self._send(200, json.dumps(data, ensure_ascii=False).encode("utf-8"))
        except (ValueError, TypeError):
            return self._send(400, b'{"error":"bad request"}')
        except Exception as e:
            try:
                launch_log(f"каталог {u.path}: {type(e).__name__}: {e}")
            except Exception:
                pass
            msg = json.dumps({"error": str(e)[:160] or "network"}, ensure_ascii=False).encode("utf-8")
            return self._send(502, msg)

    def _cat_fixcover(self):
        q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()}
        cid, key = q.get("id", ""), q.get("k", "")
        if not re.fullmatch(r"\d{1,20}", cid) or not re.fullmatch(r"(dz|it|sc):\d{1,20}", key):
            return self._send(400)
        try:
            url = cat_cover_url(key)
            if not url or not host_ok(url):
                return self._send(404)
            body, _ = fetch_cover(url)
            if body[:2] != b"\xff\xd8" and body[:4] != b"\x89PNG":
                return self._send(415)
            IMG_DIR.mkdir(parents=True, exist_ok=True)
            (IMG_DIR / f"cover_{cid}.jpg").write_bytes(body)
            return self._send(200, b"{}")
        except Exception:
            return self._send(502)

    def _cat_savecover(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        cid, url = q.get("id", ""), q.get("u", "")
        if not re.fullmatch(r"\d{1,20}", cid) or not host_ok(url):
            return self._send(400)
        try:
            body, _ = fetch_cover(url)
            if body[:2] != b"\xff\xd8" and body[:4] != b"\x89PNG":
                return self._send(415)
            IMG_DIR.mkdir(parents=True, exist_ok=True)
            (IMG_DIR / f"cover_{cid}.jpg").write_bytes(body)
            return self._send(200, b"{}")
        except Exception:
            return self._send(502)

    def do_GET(self):
        if not self._local():
            return self._send(403)
        STATE["last"] = time.time()
        STATE["bye"] = None
        if self.path in ("/", "/index.html"):
            self._send(200, page_html().encode("utf-8"), "text/html; charset=utf-8")
        elif self.path.startswith("/api/img/"):
            ref = self._img_ref()
            if ref:
                self._img_get(*ref)
            else:
                self._send(404)
        elif self.path.startswith("/api/cat/"):
            self._cat_get()
        elif self.path.startswith("/api/settings"):
            self._send(200, json.dumps(load_settings(), ensure_ascii=False).encode("utf-8"))
        elif self.path.startswith("/api/artists"):
            self._send(200, ARTISTS_FILE.read_bytes() if ARTISTS_FILE.exists() else b"{}")
        elif self.path.startswith("/api/audio/"):
            aid = self._audio_id()
            if aid:
                self._audio_get(aid)
            else:
                self._send(404)
        elif self.path.startswith("/api/tracks"):
            self._send(200, DATA_FILE.read_bytes() if DATA_FILE.exists() else b"[]")
        elif self.path.startswith("/api/game"):
            self._send(200, GAME_FILE.read_bytes() if GAME_FILE.exists() else b'{"best": 0, "snake": 0}')
        elif self.path.startswith("/api/profile"):
            self._send(200, PROFILE_FILE.read_bytes() if PROFILE_FILE.exists() else b"{}")
        elif self.path.startswith("/api/ping"):
            self._send(200, b"{}")
        elif self.path.startswith("/api/update/check"):
            try:
                self._send(200, json.dumps(upd_check(), ensure_ascii=False).encode("utf-8"))
            except Exception as e:
                self._send(502, json.dumps({"error": str(e)[:200], "cur": APP_VERSION, "url": upd_url()}, ensure_ascii=False).encode("utf-8"))
        else:
            self._send(404)

    def do_POST(self):
        if not self._local():
            return self._send(403)
        if self.path.startswith("/api/quit"):
            STATE["quit"] = time.time() + 0.6
            return self._send(200, b"{}")
        if self.path.startswith("/api/f11"):
            return self._send(200 if press_f11() else 501, b"{}")
        if self.path.startswith("/api/bye"):
            STATE["bye"] = time.time() + 3
            return self._send(200, b"{}")
        if self.path.startswith("/api/open"):
            try:
                n = min(int(self.headers.get("Content-Length", 0)), 2048)
                url = self.rfile.read(n).decode("utf-8").strip()
                p = urllib.parse.urlparse(url)
                h = (p.hostname or "").lower()
                if p.scheme != "https" or not any(h == d or h.endswith("." + d) for d in OPEN_HOSTS):
                    return self._send(400)
                threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
                return self._send(200, b"{}")
            except Exception:
                return self._send(400)
        if self.path.startswith("/api/room/"):
            return self._room_api()
        if self.path.startswith("/api/cat/fixcover"):
            return self._cat_fixcover()
        if self.path.startswith("/api/cat/savecover"):
            return self._cat_savecover()
        if self.path.startswith("/api/settings"):
            try:
                n = min(int(self.headers.get("Content-Length", 0)), 20000)
                data = json.loads(self.rfile.read(n).decode("utf-8"))
                if not isinstance(data, dict):
                    raise ValueError
                clean = {k: v for k, v in data.items() if isinstance(k, str) and len(k) < 24
                         and (isinstance(v, (bool, int, float)) or (isinstance(v, str) and len(v) < 40))}
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                tmp = SETTINGS_FILE.with_suffix(".tmp")
                tmp.write_text(json.dumps(clean, ensure_ascii=False), encoding="utf-8")
                tmp.replace(SETTINGS_FILE)
                return self._send(200, b"{}")
            except Exception:
                return self._send(400)
        if self.path.startswith("/api/tracks"):
            try:
                n = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(n)
                json.loads(raw.decode("utf-8"))
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                tmp = DATA_FILE.with_suffix(".tmp")
                tmp.write_bytes(raw)
                tmp.replace(DATA_FILE)
                return self._send(200, b"{}")
            except Exception:
                return self._send(400)
        if self.path.startswith("/api/update/apply"):
            try:
                path = upd_apply()
                return self._send(200, json.dumps({"path": path}, ensure_ascii=False).encode("utf-8"))
            except Exception as e:
                launch_log(f"обновление не удалось: {type(e).__name__}: {e}")
                return self._send(500, json.dumps({"error": str(e)[:200]}, ensure_ascii=False).encode("utf-8"))
        if self.path.startswith("/api/update/url"):
            try:
                n = int(self.headers.get("Content-Length", 0))
                u = str(json.loads(self.rfile.read(min(n, 4096)).decode("utf-8")).get("url") or "").strip()
                nu = norm_upd(u)
                if u and not nu:
                    return self._send(400, json.dumps({"error": "Нужна https-ссылка"}, ensure_ascii=False).encode("utf-8"))
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                UPDATE_FILE.write_text(json.dumps({"url": nu}), "utf-8")
                UPD.update(src=None, ver=None)
                return self._send(200, json.dumps({"url": nu}).encode("utf-8"))
            except Exception:
                return self._send(400)
        if self.path.startswith("/api/profile"):
            try:
                n = int(self.headers.get("Content-Length", 0))
                if n > 2_000_000:
                    return self._send(413)
                data = json.loads(self.rfile.read(n).decode("utf-8"))
                if not isinstance(data, dict):
                    return self._send(400)
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                tmp = PROFILE_FILE.with_suffix(".tmp")
                tmp.write_bytes(json.dumps(data, ensure_ascii=False).encode("utf-8"))
                tmp.replace(PROFILE_FILE)
                return self._send(200, b"{}")
            except Exception:
                return self._send(400)
        if self.path.startswith("/api/game"):
            try:
                n = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(n).decode("utf-8"))
                best = max(0, int(data.get("best", 0)))
                snake = max(0, int(data.get("snake", 0)))
                rhythm = max(0, int(data.get("rhythm", 0)))
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                tmp = GAME_FILE.with_suffix(".tmp")
                tmp.write_bytes(json.dumps({"best": best, "snake": snake, "rhythm": rhythm}).encode("utf-8"))
                tmp.replace(GAME_FILE)
                return self._send(200, b"{}")
            except Exception:
                return self._send(400)
        if self.path.startswith("/api/img/"):
            ref = self._img_ref()
            n = int(self.headers.get("Content-Length", 0) or 0)
            if not ref:
                return self._send(400)
            if n <= 0 or n > MAX_IMG:
                return self._send(413)
            try:
                raw = self.rfile.read(n)
                is_gif = ref[0] == "me" and raw[:6] in (b"GIF87a", b"GIF89a")
                if raw[:2] != b"\xff\xd8" and not is_gif:
                    return self._send(400)
                IMG_DIR.mkdir(parents=True, exist_ok=True)
                tmp = IMG_DIR / f"{ref[0]}_{ref[1]}.part"
                tmp.write_bytes(raw)
                tmp.replace(IMG_DIR / f"{ref[0]}_{ref[1]}.jpg")
                return self._send(200, b"{}")
            except Exception:
                return self._send(400)
        if self.path.startswith("/api/artists"):
            try:
                n = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(n)
                if not isinstance(json.loads(raw.decode("utf-8")), dict):
                    return self._send(400)
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                tmp = ARTISTS_FILE.with_suffix(".tmp")
                tmp.write_bytes(raw)
                tmp.replace(ARTISTS_FILE)
                return self._send(200, b"{}")
            except Exception:
                return self._send(400)
        if self.path.startswith("/api/audio/"):
            aid = self._audio_id()
            m = re.search(r"[?&]ext=([a-z0-9]+)", self.path)
            ext = m.group(1) if m else ""
            if not aid or ext not in AUDIO_TYPES:
                return self._send(400)
            n = int(self.headers.get("Content-Length", 0) or 0)
            if n <= 0 or n > MAX_AUDIO:
                return self._send(413)
            AUDIO_DIR.mkdir(parents=True, exist_ok=True)
            tmp = AUDIO_DIR / f"{aid}.part"
            try:
                with open(tmp, "wb") as fh:
                    left = n
                    while left > 0:
                        chunk = self.rfile.read(min(1 << 20, left))
                        if not chunk:
                            raise IOError("обрыв загрузки")
                        fh.write(chunk)
                        left -= len(chunk)
                for e in AUDIO_TYPES:
                    if e != ext:
                        try:
                            (AUDIO_DIR / f"{aid}.{e}").unlink()
                        except OSError:
                            pass
                tmp.replace(AUDIO_DIR / f"{aid}.{ext}")
                return self._send(200, b"{}")
            except Exception:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                return self._send(400)
        self._send(404)


# ============================ онлайн-комнаты ============================
# Комнаты живут в памяти этой программы. Для друзей поднимается отдельный маленький сервер
# (только страница комнаты и её API — медиатека и остальной интерфейс по нему недоступны).
ROOM_HTML = r'''<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Комната — PULSE RATER</title>
<style>
:root{--bg:#12060b;--sub:#c9aeb6;--line:rgba(255,255,255,.11);--a:#ff3347;--a2:#ff5566;color-scheme:dark}
*{box-sizing:border-box}
html,body{margin:0;min-height:100%}
body{font:15px/1.45 "Segoe UI",system-ui,-apple-system,sans-serif;color:#fff;background:radial-gradient(1100px 650px at 15% -10%,#4d1024 0,transparent 60%),radial-gradient(900px 600px at 100% 0,#2b0a3b 0,transparent 55%),var(--bg);background-attachment:fixed}
body.emb{background:transparent}
.wrap{max-width:860px;margin:0 auto;padding:24px 16px 48px}
body.emb .wrap{padding:4px 2px 12px}
button{font:inherit;color:inherit;border:0;cursor:pointer;background:none}
input[type=text]{font:inherit;color:#fff;width:100%;padding:12px 14px;border-radius:12px;border:1px solid var(--line);background:rgba(0,0,0,.35);outline:0}
input[type=text]:focus{border-color:var(--a2)}
.card{padding:16px 18px;border-radius:16px;background:rgba(255,255,255,.05);box-shadow:inset 0 0 0 1px var(--line);margin-bottom:14px}
.brand{font-size:12px;letter-spacing:.14em;text-transform:uppercase;color:var(--sub);margin-bottom:14px}
body.emb .brand{display:none}
.btn{padding:11px 22px;border-radius:999px;background:var(--a);font-weight:800;transition:transform .2s,background .2s}
.btn:hover{transform:scale(1.04);background:var(--a2)}
.btn.ghost{background:rgba(255,255,255,.09);color:var(--sub)}
.btn.ghost:hover{background:rgba(255,255,255,.17);color:#fff}
.btn:disabled{opacity:.4;cursor:default;transform:none}
.top{display:flex;gap:16px;align-items:center}
.cov{flex:none;width:84px;height:84px;border-radius:12px;background:linear-gradient(135deg,#7c4dff,#2a1a8a) center/cover no-repeat;display:grid;place-items:center;font-size:34px}
.top h1{margin:0;font-size:26px;line-height:1.1;letter-spacing:-.02em;word-break:break-word}
.top p{margin:4px 0 0;color:var(--sub)}
.tag{display:inline-block;margin-top:6px;padding:2px 10px;border-radius:999px;background:rgba(255,255,255,.09);font-size:12px;color:var(--sub)}
audio{width:100%;margin-top:14px;height:38px}
.chips{display:flex;flex-wrap:wrap;gap:8px}
.chip{display:flex;align-items:center;gap:8px;padding:6px 12px;border-radius:999px;background:rgba(255,255,255,.07);font-size:13px;font-weight:600}
.chip i{width:8px;height:8px;border-radius:50%;background:#6b5a60}
.chip.on i{background:#ffb347}.chip.done i{background:#3ddc84}.chip.off{opacity:.45}
.chip.me{box-shadow:inset 0 0 0 1px var(--a2)}
.crit{display:grid;grid-template-columns:minmax(120px,210px) 1fr 58px;gap:14px;align-items:center;padding:10px 0;border-top:1px solid var(--line)}
.crit:first-child{border-top:0}
.crit b{display:block}.crit small{color:var(--sub);font-size:12px}
.crit output{text-align:right;font-weight:900;font-size:20px;font-variant-numeric:tabular-nums}
input[type=range]{-webkit-appearance:none;appearance:none;width:100%;height:6px;border-radius:9px;outline:0;cursor:pointer;background:linear-gradient(90deg,var(--a2) var(--p,50%),rgba(255,255,255,.16) var(--p,50%))}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:18px;height:18px;border-radius:50%;background:#fff;box-shadow:0 0 0 4px rgba(255,51,71,.25)}
input[type=range]::-moz-range-thumb{width:18px;height:18px;border:0;border-radius:50%;background:#fff}
input[type=range]:disabled{opacity:.45;cursor:default}
.acts{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-top:12px}
.sub{color:var(--sub);font-size:13px}
.ring{position:relative;flex:none;width:150px;height:150px}
.ring svg{width:100%;height:100%;transform:rotate(-90deg)}
.ring div{position:absolute;inset:0;display:grid;place-content:center;text-align:center}
.ring b{font-size:42px;line-height:1;font-weight:900}
.ring span{font-size:12px;color:var(--sub)}
.res{display:flex;gap:22px;align-items:center;flex-wrap:wrap;margin-bottom:14px}
.res h2{margin:0;font-size:28px;letter-spacing:-.02em}
.res p{margin:4px 0 0;color:var(--sub)}
.tw{overflow-x:auto}
table{border-collapse:collapse;width:100%;min-width:420px}
th,td{padding:8px 10px;text-align:left;border-top:1px solid var(--line);vertical-align:middle;font-size:14px}
th{border-top:0;font-size:12px;letter-spacing:.06em;text-transform:uppercase}
td.cn{font-weight:700}
td.av,th:last-child{text-align:right}
.cv{height:5px;border-radius:9px;background:rgba(255,255,255,.12);overflow:hidden;margin-bottom:4px;min-width:50px}
.cv i{display:block;height:100%;border-radius:9px}
td span{font-weight:700;font-variant-numeric:tabular-nums}
tr.ft td{font-size:16px}
.note{margin:12px 0 0;padding:10px 14px;border-radius:12px;background:rgba(255,255,255,.06);font-size:13px;color:var(--sub)}
.note b{color:#fff}
details{margin-top:10px;border-radius:12px;background:rgba(255,255,255,.04);padding:10px 14px}
summary{cursor:pointer;font-weight:700;display:flex;gap:10px;justify-content:space-between}
.err{padding:60px 10px;text-align:center;color:var(--sub)}
.err b{display:block;color:#fff;font-size:22px;margin-bottom:6px}
.join{max-width:420px;margin:60px auto;text-align:center}
.join h1{margin:0 0 6px;font-size:26px}
.join p{color:var(--sub);margin:0 0 16px}
.join .btn{margin-top:12px;width:100%}
#conn{position:fixed;left:50%;top:8px;transform:translateX(-50%);padding:4px 14px;border-radius:999px;background:#7a1020;font-size:12px;z-index:5}
#conn[hidden]{display:none}
.ttl{display:flex;flex-wrap:wrap;align-items:center;gap:6px 12px}
.ttl h1{margin:0}
.npill,input[type=text].nin{font:600 11.5px/1.3 "Segoe UI",system-ui,sans-serif;color:#c8c8cf;letter-spacing:.01em;border-radius:999px;padding:3px 11px;background:rgba(255,255,255,.07);box-shadow:inset 0 0 0 1px rgba(255,255,255,.07)}
input[type=text].nin{width:190px;max-width:100%;border:0;outline:0;background:transparent;box-shadow:inset 0 0 0 1px rgba(255,255,255,.16);transition:box-shadow .2s,background .2s}
input[type=text].nin::placeholder{color:#8f8f98}
input[type=text].nin:hover{background:rgba(255,255,255,.05)}
input[type=text].nin:focus{background:rgba(255,255,255,.08);box-shadow:inset 0 0 0 1px var(--a2);color:#e6e6ea}
.pbar{display:flex;align-items:center;gap:14px;margin-top:16px;padding:12px 16px;border-radius:18px;background:linear-gradient(120deg,rgba(255,51,71,.20),rgba(124,77,255,.16) 60%,rgba(255,255,255,.04));box-shadow:inset 0 0 0 1px rgba(255,255,255,.12),0 10px 30px rgba(0,0,0,.28)}
.pp{flex:none;width:46px;height:46px;border-radius:50%;display:grid;place-items:center;background:linear-gradient(135deg,var(--a),#ff7a45);box-shadow:0 6px 18px rgba(255,51,71,.45);transition:transform .15s,box-shadow .2s}
.pp:hover{transform:scale(1.07);box-shadow:0 8px 24px rgba(255,51,71,.6)}
.pp:active{transform:scale(.96)}
.pp svg{width:20px;height:20px;fill:#fff}
.pp .i-pause{display:none}.pbar.play .pp .i-play{display:none}.pbar.play .pp .i-pause{display:block}
.pmid{flex:1;min-width:0}
.ptime{display:flex;justify-content:space-between;margin-top:7px;font-size:11.5px;color:var(--sub);font-variant-numeric:tabular-nums}
.pbar input[type=range]{height:5px}
.pbar input[type=range]::-webkit-slider-thumb{width:14px;height:14px;box-shadow:0 0 0 3px rgba(255,51,71,.28)}
.vol{flex:none;display:flex;align-items:center;gap:8px;width:136px}
.vb{flex:none;width:30px;height:30px;border-radius:50%;display:grid;place-items:center;color:var(--sub);transition:background .2s,color .2s}
.vb:hover{background:rgba(255,255,255,.12);color:#fff}
.vb svg{width:19px;height:19px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.vb .mx{display:none}.pbar.mute .vb .mx{display:block}.pbar.mute .vb .wv{display:none}
.perr{margin-top:8px;font-size:12px;color:#ff8a96}
.adm{font-weight:900;font-size:10.5px;letter-spacing:.07em;color:#fff;background:linear-gradient(90deg,#ff3347,#ff8a3d);padding:1px 7px;border-radius:6px;box-shadow:0 0 12px rgba(255,90,70,.55)}
.hn{font-weight:800;background:linear-gradient(90deg,#ffd27a,#ff5f8a,#b07cff);-webkit-background-clip:text;background-clip:text;color:transparent}
.chip.host{box-shadow:inset 0 0 0 1px rgba(255,170,90,.55),0 0 16px rgba(255,90,90,.25)}
.kick{flex:none;width:18px;height:18px;margin-left:2px;border-radius:50%;background:rgba(255,255,255,.1);font-size:10px;line-height:1;display:grid;place-items:center;color:var(--sub);transition:background .2s,color .2s}
.kick:hover{background:#7a1020;color:#fff}
.mkadm{flex:none;height:18px;padding:0 6px;margin-left:2px;border-radius:9px;background:rgba(255,255,255,.1);font-size:10px;font-weight:800;line-height:1;color:var(--sub);transition:background .2s,color .2s}
.mkadm:hover{background:linear-gradient(90deg,#ff3347,#ff8a3d);color:#fff}
.mkadm.on{background:rgba(255,140,70,.25);color:#ffb070}
.statrow{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;margin-top:10px}
.btn.sm{padding:6px 14px;font-size:13px;font-weight:700}
@media(max-width:560px){.vol{width:auto}.vol input{display:none}.pbar{gap:10px;padding:10px 12px}}
@media(max-width:560px){.crit{grid-template-columns:1fr 54px}.crit input{grid-column:1/-1;grid-row:2}.top h1{font-size:22px}.cov{width:64px;height:64px}}
</style>
</head>
<body>
<div class="wrap">
  <div class="brand">PULSE RATER · Онлайн-комната</div>
  <div id="app"></div>
</div>
<div id="conn" hidden>Нет связи с хозяином комнаты…</div>
<script>
const $=s=>document.querySelector(s);
const esc=s=>String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const H=new URLSearchParams(location.hash.slice(1));
const EMB=H.get("emb")==="1",HK=H.get("k")||"";
if(EMB) document.body.classList.add("emb");
const LS=(k,v)=>{try{if(v===undefined) return localStorage.getItem(k);localStorage.setItem(k,v)}catch(e){return null}};
let code=(location.pathname.match(/^\/r\/([A-Za-z0-9]{3,10})/)||[])[1];
code=code?code.toUpperCase():"";
let pid=H.get("p")||(code&&LS("rp_"+code))||"";
const VERD=[[3,"Ужасно"],[5,"Слабовато"],[6.5,"Норм"],[8,"Профик"],[9,"Фрешмен"],[10.01,"GOAT"]];
const fv=v=>(Number.isInteger(+v)&&!(S&&S.dec))?String(+v):(+v).toFixed(1);   // без десятичных — целые без «.0»
const verdict=t=>(VERD.find(v=>t<v[0])||VERD[5])[1];
const col=i=>"hsl("+((i*67+345)%360)+" 85% 68%)";
const RG=[[0,0],[2,16],[3.8,36],[5.2,54],[6.6,88],[8,140],[9.2,172],[10,186]];
const rgCol=v=>{v=Math.max(0,Math.min(10,+v||0));let i=1;while(i<RG.length-1&&v>RG[i][0]) i++;
  const a=RG[i-1],b=RG[i],h=a[1]+(b[1]-a[1])*(v-a[0])/(b[0]-a[0]);return "hsl("+h.toFixed(0)+" 88% "+(57+3*Math.sin(v/10*Math.PI)).toFixed(0)+"%)"};
const ADM=()=>!!(HK||(S&&S.adm));     // хозяин или назначенный админ
const tell=m=>{try{if(parent!==window) parent.postMessage(Object.assign({room:1},m),"*")}catch(e){}};

async function api(path,body){
  const r=await fetch(path,body?{method:"POST",body:JSON.stringify(body)}:{cache:"no-store"});
  let j={};try{j=await r.json()}catch(e){}
  return {ok:r.ok,j};
}

/* ---------- старт ---------- */
const app=$("#app");
let S=null,vals=null,lastRound=0,timer=0,touched=false,shown="",halt=false,noteBusy=0;
if(!code) showCode();else start();

function showCode(){
  shown="code";
  app.innerHTML=`<div class="join"><h1>Войти в комнату</h1><p>Введи код, который прислал друг</p>
    <input type="text" id="cc" maxlength="8" placeholder="КОД" style="text-align:center;letter-spacing:.3em;font-size:22px;text-transform:uppercase">
    <button class="btn" id="cg">Войти</button></div>`;
  const go=()=>{const v=$("#cc").value.trim().toUpperCase();if(v) location.href="/r/"+encodeURIComponent(v)};
  $("#cg").onclick=go;$("#cc").onkeydown=e=>{if(e.key==="Enter") go()};$("#cc").focus();
}
function gone(){
  shown="gone";clearTimeout(timer);
  app.innerHTML=`<div class="err"><b>Комната закрыта или не найдена</b>Проверь код — или попроси друга создать новую комнату.</div>`;
  tell({type:"closed"});
}
function start(){
  poll();
}
async function poll(){
  clearTimeout(timer);
  try{
    const {ok,j}=await api("/room/api/state?c="+encodeURIComponent(code)+"&p="+encodeURIComponent(pid));
    if(halt) return;
    $("#conn").hidden=true;
    if(!ok||j.err){gone();return}
    S=j;render();
  }catch(e){$("#conn").hidden=false}
  if(!halt) timer=setTimeout(poll,1500);
}

/* ---------- отрисовка ---------- */
function render(){
  if(!S.me){if(S.kicked){showKicked();return}showJoin();return}
  if(shown!=="room"||S.round!==lastRound){
    lastRound=S.round;shown="room";touched=false;
    vals=S.mine&&S.mine.scores?S.mine.scores.slice():Array(S.crit.length).fill(S.dec?S.start:Math.round(S.start));
    app.innerHTML=`<div class="card" id="top"></div><div class="card"><div class="chips" id="pl"></div><div class="statrow"><div class="sub" id="stat"></div><div class="acts" style="margin:0"><button class="btn ghost sm" id="hren" type="button">✎ Сменить ник</button>${HK?"":'<button class="btn ghost sm" id="hleave" type="button">Выйти из комнаты</button>'}</div></div></div>
      <div class="card" id="rate"></div><div id="res"></div><div id="hostbar"></div><div id="hist"></div>`;
    buildTop();buildRate();bindPlayers();
  }
  syncAdm();paintPlayers();paintStat();
  const done=!!(S.mine&&S.mine.done);
  $("#rate").hidden=S.revealed;
  if(!S.revealed){
    if(!touched&&S.mine&&S.mine.scores){vals=S.mine.scores.slice();syncSliders()}
    $("#rdone").textContent=done?"Изменить оценку":"Готово — оценка отправлена";
    $("#rdone").className="btn"+(done?" ghost":"");
    document.querySelectorAll("#rate input").forEach(x=>x.disabled=done);
    $("#rhint").textContent=done?"Оценка принята. Остальные пока не видят, что ты поставил.":"Оценки скрыты от остальных, пока все не закончат.";
  }
  paintNote();paintRes();paintHost();paintHist();
}
const IC={
  play:'<svg class="i-play" viewBox="0 0 24 24"><path d="M8 5.5v13a1 1 0 0 0 1.5.86l10.5-6.5a1 1 0 0 0 0-1.72L9.5 4.64A1 1 0 0 0 8 5.5z"/></svg>',
  pause:'<svg class="i-pause" viewBox="0 0 24 24"><rect x="6" y="5" width="4.2" height="14" rx="1.3"/><rect x="13.8" y="5" width="4.2" height="14" rx="1.3"/></svg>'
};
const fmtT=s=>{s=isFinite(s)&&s>0?Math.floor(s):0;return Math.floor(s/60)+":"+String(s%60).padStart(2,"0")};
const getVol=()=>{const x=parseFloat(LS("rvol"));return isFinite(x)?Math.min(1,Math.max(0,x)):1};
const getMute=()=>LS("rmute")==="1";
function playerHTML(){
  /* &r=раунд — у каждого раунда свой адрес файла, браузер не подсунет трек из кэша прошлого раунда */
  const src="/room/audio/"+encodeURIComponent(code)+"?p="+encodeURIComponent(pid)+"&amp;r="+S.round;
  return `<div class="pbar" id="pbar"><button class="pp" id="ppb" type="button" aria-label="Играть / пауза">${IC.play}${IC.pause}</button>
    <div class="pmid"><input type="range" id="seek" min="0" max="1000" step="1" value="0" aria-label="Перемотка"><div class="ptime"><span id="pcur">0:00</span><span id="pdur">0:00</span></div></div>
    <div class="vol"><button class="vb" id="vbtn" type="button" aria-label="Звук"><svg viewBox="0 0 24 24"><path d="M11 5 6 9H3v6h3l5 4z" fill="currentColor" stroke="none"/><g class="wv"><path d="M15.5 8.5a5 5 0 0 1 0 7"/><path d="M18.5 5.5a9 9 0 0 1 0 13"/></g><g class="mx"><path d="m16 9 5 6M21 9l-5 6"/></g></svg></button><input type="range" id="vsl" min="0" max="100" step="1" aria-label="Громкость"></div>
    <audio id="aud" preload="metadata" src="${src}"></audio></div><div class="perr" id="perr" hidden></div>`;
}
function initPlayer(){
  const a=$("#aud"),bar=$("#pbar"),pb=$("#ppb"),sk=$("#seek"),vs=$("#vsl"),vb=$("#vbtn"),cur=$("#pcur"),dur=$("#pdur");
  let drag=false;
  const setP=(el,p)=>el.style.setProperty("--p",p+"%");
  a.volume=getVol();a.muted=getMute();           // громкость помнится между раундами
  const paintVol=()=>{const m=a.muted||a.volume===0;bar.classList.toggle("mute",m);vs.value=m?0:Math.round(a.volume*100);setP(vs,vs.value)};
  const paintTime=()=>{
    const d=a.duration;
    if(!drag&&isFinite(d)&&d>0){const p=a.currentTime/d*1000;sk.value=p;setP(sk,p/10)}
    if(!drag) cur.textContent=fmtT(a.currentTime);
    dur.textContent=isFinite(d)?fmtT(d):"0:00";
  };
  paintVol();setP(sk,0);
  pb.onclick=()=>{if(a.paused){const p=a.play();if(p&&p.catch) p.catch(()=>{})}else a.pause()};
  a.addEventListener("play",()=>bar.classList.add("play"));
  a.addEventListener("pause",()=>bar.classList.remove("play"));
  a.addEventListener("ended",()=>bar.classList.remove("play"));
  ["timeupdate","loadedmetadata","durationchange"].forEach(ev=>a.addEventListener(ev,paintTime));
  a.addEventListener("error",()=>{const e=$("#perr");e.hidden=false;e.textContent="Не удалось загрузить трек. Попробуй обновить страницу."});
  sk.addEventListener("input",()=>{drag=true;setP(sk,sk.value/10);const d=a.duration;if(isFinite(d)) cur.textContent=fmtT(sk.value/1000*d)});
  sk.addEventListener("change",()=>{const d=a.duration;if(isFinite(d)) a.currentTime=sk.value/1000*d;drag=false;paintTime()});
  vs.addEventListener("input",()=>{
    const v=vs.value/100;a.volume=v;a.muted=false;LS("rvol",String(v));LS("rmute","0");paintVol();
  });
  vb.onclick=()=>{
    if(a.muted||a.volume===0){a.muted=false;if(a.volume===0){a.volume=.6;LS("rvol","0.6")}}else a.muted=true;
    LS("rmute",a.muted?"1":"0");paintVol();
  };
}
function stopOldAudio(){
  const old=$("#aud");
  if(old){try{old.pause();old.removeAttribute("src");old.load()}catch(e){}}
}
function buildTop(){
  const T=S.track;
  stopOldAudio();
  const cv=T.cover?`style="background-image:url('/room/cover/${esc(code)}?r=${S.round}')"`:"";
  const note=noteHTML();lastAdm=ADM();
  $("#top").innerHTML=`<div class="top"><div class="cov" ${cv}>${T.cover?"":"♪"}</div><div><div class="sub">Раунд ${S.round}</div><div class="ttl"><h1>${esc(T.title||"Без названия")}</h1>${note}</div><p>${esc(T.artist||"")}${T.album?" · "+esc(T.album):""}</p></div></div>`
    +(T.audio?playerHTML():`<div class="sub" style="margin-top:12px">Трек послушай у себя — хозяин комнаты не раздаёт файл.</div>`);
  if(T.audio) initPlayer();
  bindNote();
}
/* плашка с деталями трека: редактируют админы, остальные видят текст */
let lastAdm=false;
const noteHTML=()=>ADM()?`<input type="text" class="nin" id="nin" maxlength="80" placeholder="+ детали: продюсеры, ремикс…" autocomplete="off" spellcheck="false">`:`<span class="npill" id="npill" hidden></span>`;
function syncAdm(){                       // админку выдали или забрали — меняем только плашку, плеер не трогаем
  if(ADM()===lastAdm) return;lastAdm=ADM();
  const old=$("#nin")||$("#npill");if(!old) return;
  const t=document.createElement("template");t.innerHTML=noteHTML();old.replaceWith(t.content.firstChild);bindNote();
}
const fitNote=el=>{el.style.width=Math.min(360,Math.max(150,el.value.length*7+34))+"px"};
function bindNote(){
  const el=$("#nin");if(!el) return;
  el.addEventListener("change",async()=>{
    const v=el.value.trim();if(v===(S.track.note||"")) return;
    S.track.note=v;noteBusy++;
    try{await api("/room/api/note",{c:code,k:HK,p:pid,note:v})}finally{noteBusy--}
  });
  el.addEventListener("keydown",e=>{if(e.key==="Enter"){e.preventDefault();el.blur()}});
  el.addEventListener("input",()=>fitNote(el));
}
function paintNote(){
  const n=S.track.note||"",el=$("#nin");
  if(el){if(!noteBusy&&document.activeElement!==el&&el.value!==n) el.value=n;fitNote(el);return}
  const p=$("#npill");if(p){if(p.textContent!==n) p.textContent=n;p.hidden=!n}
}
function bindPlayers(){
  $("#pl").onclick=async e=>{
    const a=e.target.closest(".mkadm");
    if(a){
      const on=a.dataset.on==="1";
      if(!confirm((on?"Сделать «":"Забрать админку у «")+a.dataset.name+(on?"» администратором? Он сможет показывать итоги, менять детали трека и исключать гостей.":"»?"))) return;
      await api("/room/api/admin",{c:code,k:HK,n:+a.dataset.id,on});poll();return;
    }
    const k=e.target.closest(".kick");if(!k) return;
    if(!confirm("Исключить «"+k.dataset.name+"» из комнаты?")) return;
    await api("/room/api/kick",{c:code,k:HK,p:pid,n:+k.dataset.id});poll();
  };
  const lv=$("#hleave");if(lv) lv.onclick=leaveRoom;
  $("#hren").onclick=async()=>{
    const me=S.players.find(p=>p.me);
    const v=prompt("Новый ник в комнате",me?me.name:"");
    if(v==null||!v.trim()) return;
    const {ok,j}=await api("/room/api/rename",{c:code,p:pid,name:v.trim()});
    if(!ok){alert("Не получилось сменить ник");return}
    if(!HK) LS("rname",j.name);
    poll();
  };
}
async function leaveRoom(){
  if(!confirm("Выйти из комнаты?")) return;
  halt=true;clearTimeout(timer);stopOldAudio();
  try{await api("/room/api/leave",{c:code,p:pid})}catch(e){}
  LS("rp_"+code,"");pid="";shown="left";
  app.innerHTML=`<div class="err"><b>Ты вышел из комнаты</b>Твоя оценка больше не учитывается.<div class="acts" style="justify-content:center;margin-top:18px"><button class="btn" id="rj" type="button">Зайти снова</button><button class="btn ghost" id="rc" type="button">Другая комната</button></div></div>`;
  $("#rj").onclick=()=>{halt=false;shown="";poll()};
  $("#rc").onclick=()=>{location.href="/"};
}
function showKicked(){
  if(shown==="kicked") return;
  shown="kicked";halt=true;clearTimeout(timer);stopOldAudio();
  app.innerHTML=`<div class="err"><b>Тебя исключили из комнаты</b>Администратор убрал тебя из этой комнаты.</div>`;
}
function buildRate(){
  $("#rate").innerHTML=S.crit.map((c,i)=>`<div class="crit"><div><b>${esc(c[0])}</b><small>${esc(c[1])}</small></div><input type="range" min="0" max="10" step="${S.dec?0.1:1}" value="${vals[i]}" data-i="${i}" aria-label="${esc(c[0])}"><output id="o${i}">${fv(vals[i])}</output></div>`).join("")
    +`<div class="acts"><button class="btn" id="rdone"></button><span class="sub" id="rhint"></span></div>`;
  document.querySelectorAll("#rate input").forEach(x=>{
    const i=+x.dataset.i;fill(x);
    x.addEventListener("input",()=>{touched=true;vals[i]=+x.value;$("#o"+i).textContent=fv(x.value);fill(x)});
  });
  $("#rdone").onclick=async()=>{
    const done=!(S.mine&&S.mine.done);
    $("#rdone").disabled=true;
    await api("/room/api/rate",{c:code,p:pid,scores:vals,done});
    touched=false;$("#rdone").disabled=false;poll();
  };
}
function fill(x){x.style.setProperty("--p",(x.value*10)+"%")}
function syncSliders(){document.querySelectorAll("#rate input").forEach(x=>{const i=+x.dataset.i;x.value=vals[i];$("#o"+i).textContent=fv(vals[i]);fill(x)})}
function paintPlayers(){
  $("#pl").innerHTML=S.players.map(p=>{
    const nm=p.admin?`<span class="adm">[ADMIN]</span><span class="hn">${esc(p.name)}</span>`:esc(p.name);
    const mk=HK&&!p.host?`<button class="mkadm${p.admin?" on":""}" type="button" data-id="${p.id}" data-on="${p.admin?0:1}" data-name="${esc(p.name)}" title="${p.admin?"Забрать админку":"Сделать админом"}">${p.admin?"−A":"+A"}</button>`:"";
    const kick=ADM()&&!p.host&&!p.me&&(HK||!p.admin)?`<button class="kick" type="button" data-id="${p.id}" data-name="${esc(p.name)}" title="Исключить из комнаты">✕</button>`:"";
    return `<div class="chip ${p.done?"done":p.online?"on":"off"}${p.me?" me":""}${p.host?" host":""}"><i></i>${nm}${p.host?" 👑":""}${p.me?" (ты)":""}${mk}${kick}</div>`;
  }).join("");
}
function paintStat(){
  if(S.revealed){$("#stat").textContent="Итоги подведены.";return}
  const wait=S.players.filter(p=>!p.done&&p.online);
  const n=S.players.filter(p=>p.done).length;
  $("#stat").textContent=!wait.length&&n>=2?"Все готовы — считаем…":(n+" из "+S.players.length+" оценили"+(wait.length?" · ждём: "+wait.map(p=>p.name).join(", "):""));
}
function paintHost(){
  const el=$("#hostbar");if(!ADM()){el.innerHTML="";return}
  const n=S.players.filter(p=>p.done).length;
  el.innerHTML=`<div class="acts">${S.revealed?"":`<button class="btn" id="hrev" ${n?"":"disabled"}>Показать итоги сейчас</button>`}${HK?'<button class="btn ghost" id="hclose">Закрыть комнату</button>':""}</div>`;
  const r=$("#hrev");if(r) r.onclick=async()=>{await api("/room/api/reveal",{c:code,k:HK,p:pid});poll()};
  if(HK) $("#hclose").onclick=async()=>{if(!confirm("Закрыть комнату для всех?")) return;await api("/room/api/close",{c:code,k:HK});gone()};
}
let sentKey="";
function paintRes(){
  const el=$("#res");
  if(!S.revealed||!S.result){el.innerHTML="";return}
  const key=S.round+":"+S.result.total+":"+S.result.players.length;
  if(el.dataset.k!==key){
    el.dataset.k=key;
    el.innerHTML=`<div class="card">${resultHTML(S.result,S.crit,false)}</div>`;
  }
  if(sentKey!==key){sentKey=key;tell({type:"result",code,round:S.round,total:S.result.total,avg:S.result.avg,title:S.track.title,artist:S.track.artist})}
}
function paintHist(){
  const el=$("#hist"),h=S.history||[];
  if(!h.length){el.innerHTML="";return}
  const key=h.length+":"+S.round;
  if(el.dataset.k===key) return;el.dataset.k=key;
  el.innerHTML=`<div class="sub" style="margin:18px 0 4px">Прошлые треки</div>`+h.slice().reverse().map(x=>`<details><summary><span>${esc(x.track.title||"Без названия")}${x.track.artist?" — "+esc(x.track.artist):""}</span><b style="color:${rgCol(x.result.total)}">${x.result.total.toFixed(1)}</b></summary>${resultHTML(x.result,S.crit,true)}</details>`).join("");
}
function resultHTML(R,crit,compact){
  const P=R.players,n=P.length,rc=rgCol(R.total);
  const C=2*Math.PI*54;
  const cell=(v,c)=>`<td><div class="cv"><i style="width:${v*10}%;background:${c}"></i></div><span>${fv(v)}</span></td>`;
  const head=`<tr><th></th>${P.map((p,i)=>`<th style="color:${col(i)}">${esc(p.name)}</th>`).join("")}<th>Среднее</th></tr>`;
  const rows=crit.map((c,k)=>`<tr><td class="cn">${esc(c[0])}</td>${P.map((p,i)=>cell(p.scores[k],col(i))).join("")}<td class="av"><b>${(+R.avg[k]).toFixed(1)}</b></td></tr>`).join("");
  const foot=`<tr class="ft"><td>Итог</td>${P.map(p=>`<td><b>${(+p.total).toFixed(1)}</b></td>`).join("")}<td class="av"><b>${(+R.total).toFixed(1)}</b></td></tr>`;
  let notes="";
  if(n>=2){
    let bi=0,bs=-1;
    crit.forEach((c,k)=>{const a=P.map(p=>p.scores[k]),s=Math.max(...a)-Math.min(...a);if(s>bs){bs=s;bi=k}});
    notes+=bs<.5?`<div class="note">Вы почти во всём сошлись во мнениях 🤝</div>`:`<div class="note">Сильнее всего спорили про <b>${esc(crit[bi][0])}</b> — разброс ${bs.toFixed(1)}</div>`;
    const dist=P.map(p=>p.scores.reduce((s,v,k)=>s+Math.abs(v-R.avg[k]),0)/crit.length);
    const mi=dist.indexOf(Math.min(...dist)),hi=P.map(p=>p.total).indexOf(Math.max(...P.map(p=>p.total))),lo=P.map(p=>p.total).indexOf(Math.min(...P.map(p=>p.total)));
    notes+=`<div class="note">Ближе всех к общему мнению: <b>${esc(P[mi].name)}</b>`+(hi!==lo?` · самая щедрая оценка: <b>${esc(P[hi].name)}</b>, самая строгая: <b>${esc(P[lo].name)}</b>`:"")+`</div>`;
  }
  const big=compact?"":`<div class="res"><div class="ring"><svg viewBox="0 0 120 120"><circle cx="60" cy="60" r="54" fill="none" stroke="rgba(255,255,255,.12)" stroke-width="9"/><circle cx="60" cy="60" r="54" fill="none" stroke="${rc}" stroke-width="9" stroke-linecap="round" stroke-dasharray="${C.toFixed(1)}" stroke-dashoffset="${(C*(1-R.total/10)).toFixed(1)}"/></svg><div><b style="color:${rc}">${(+R.total).toFixed(1)}</b><span>из 10</span></div></div><div><div class="sub">Общая оценка</div><h2>${verdict(R.total)}</h2><p>Средняя по ${n} ${n===1?"оценке":n<5?"оценкам":"оценкам"}</p></div></div>`;
  return big+`<div class="tw"><table>${head}${rows}${foot}</table></div>`+notes;
}

/* ---------- вход в комнату ---------- */
function showJoin(){
  if(shown==="join") return;shown="join";
  const T=S.track||{};
  app.innerHTML=`<div class="join"><h1>Тебя позвали оценивать трек</h1><p>${esc(T.title||"")}${T.artist?" — "+esc(T.artist):""}</p>
    <input type="text" id="jn" maxlength="24" placeholder="Твоё имя" value="${esc(LS("rname")||"")}">
    <button class="btn" id="jg">Зайти в комнату</button></div>`;
  const go=async()=>{
    const name=$("#jn").value.trim();if(!name){$("#jn").focus();return}
    LS("rname",name);$("#jg").disabled=true;
    const {ok,j}=await api("/room/api/join",{c:code,name,p:pid});
    if(!ok||!j.p){$("#jg").disabled=false;alert(j.msg||"Не удалось войти в комнату");return}
    pid=j.p;LS("rp_"+code,pid);shown="";poll();
  };
  $("#jg").onclick=go;$("#jn").onkeydown=e=>{if(e.key==="Enter") go()};$("#jn").focus();
}
</script>
</body>
</html>
'''
ROOMS = {}
ROOM_LOCK = threading.RLock()
ROOM_SRV = {"srv": None, "port": None}
ROOM_ALPHA = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
ROOM_TTL = 12 * 3600
ROOM_MAX_PLAYERS = 12
ROOM_PORT0 = 8765
ADMIN_NICK = "влад сантана"      # ник хозяина комнаты (показывается с плашкой [ADMIN])


def _rclean(s, n):
    return re.sub(r"\s+", " ", str(s if s is not None else "")).strip()[:n]


ROOM_WEIGHTS = [.175, .175, .175, .175, .3]   # как WEIGHTS в интерфейсе: «Атмосфера / вайб» — 30 %, остальные по 17,5 %


def room_total(v):
    return math.floor(sum(x * (ROOM_WEIGHTS[i] if i < len(ROOM_WEIGHTS) else .2) for i, x in enumerate(v)) * 10 + 0.5 + 1e-9) / 10


def room_track(t):
    t = t if isinstance(t, dict) else {}
    cov = str(t.get("cover") or "")
    aud = str(t.get("audio") or "")
    return {"title": _rclean(t.get("title"), 80), "artist": _rclean(t.get("artist"), 60), "album": _rclean(t.get("album"), 60),
            "cover": cov if re.fullmatch(r"[a-z0-9]{1,24}", cov) else "",
            "audio": aud if re.fullmatch(r"\d{1,20}", aud) else "",
            "note": _rclean(t.get("note"), 80)}


def room_crit(c):
    if not isinstance(c, list) or len(c) != 5:
        return None
    out = []
    for x in c:
        if not isinstance(x, (list, tuple)) or len(x) < 1:
            return None
        out.append([_rclean(x[0], 40), _rclean(x[1] if len(x) > 1 else "", 90)])
    return out


def room_gc():
    now = time.time()
    for c in [c for c, r in ROOMS.items() if now - r["seen"] > ROOM_TTL]:
        ROOMS.pop(c, None)


def room_name(r, name, admin=False, skip=None):
    """Чистит ник: без приставки [ADMIN], не совпадает с ником админа (кроме своего), уникален в комнате."""
    name = _rclean(re.sub(r"^\s*[\[\(\{]\s*admin\s*[\]\)\}]\s*", "", _rclean(name, 24), flags=re.I), 24)
    if not name:
        name = ADMIN_NICK if admin else "Гость"
    squash = lambda s: s.lower().replace(" ", "")
    others = [p for q, p in r["players"].items() if q != skip]
    if not admin and any((p["host"] or p.get("admin")) and squash(p["name"]) == squash(name) for p in others):
        name = "Гость"                                   # нельзя выдать себя за админа
    used = {p["name"].lower() for p in others}
    base, i = name, 2
    while name.lower() in used:
        name = f"{base} {i}"
        i += 1
    return name


def room_add_player(r, name, host=False):
    name = room_name(r, name, admin=host)
    pid = secrets.token_urlsafe(9)
    r["players"][pid] = {"name": name, "seen": time.time(), "scores": None, "done": False, "host": host, "admin": host, "n": r["nseq"]}
    r["nseq"] += 1
    return pid


def room_create(track, crit, start, share, host_name, dec=False):
    with ROOM_LOCK:
        room_gc()
        code = ""
        for _ in range(60):
            code = "".join(secrets.choice(ROOM_ALPHA) for _ in range(5))
            if code not in ROOMS:
                break
        r = {"code": code, "key": secrets.token_urlsafe(16), "seen": time.time(), "crit": crit, "start": start,
             "players": {}, "nseq": 0, "round": 1, "track": track, "share": bool(share and track["audio"]),
             "revealed": False, "result": None, "history": [], "dec": bool(dec), "banned": set()}
        pid = room_add_player(r, host_name, host=True)
        ROOMS[code] = r
        return r, pid


def room_result(r):
    ps = [p for p in r["players"].values() if p["done"] and p["scores"]]
    if not ps:
        return None
    k = len(r["crit"])
    avg = [round(sum(p["scores"][i] for p in ps) / len(ps), 2) for i in range(k)]
    ps.sort(key=lambda p: p["n"])
    return {"avg": avg, "total": room_total(avg),
            "players": [{"name": p["name"], "scores": p["scores"], "total": room_total(p["scores"]), "host": p["host"]} for p in ps]}


def room_reveal(r):
    res = room_result(r)
    if res:
        r["revealed"], r["result"] = True, res
    return bool(res)


def room_auto(r):
    if r["revealed"]:
        return
    now = time.time()
    online = [p for p in r["players"].values() if now - p["seen"] < 8]
    done = [p for p in r["players"].values() if p["done"]]
    if len(done) >= 2 and online and all(p["done"] for p in online):
        room_reveal(r)


def room_next(r, track, share, dec=None):
    if r["revealed"] and r["result"]:
        r["history"].append({"round": r["round"], "track": {"title": r["track"]["title"], "artist": r["track"]["artist"]}, "result": r["result"]})
        r["history"] = r["history"][-30:]
    r["round"] += 1
    r["track"] = track
    if dec is not None:
        r["dec"] = bool(dec)
    r["share"] = bool(share and track["audio"])
    r["revealed"], r["result"] = False, None
    for p in r["players"].values():
        p["scores"], p["done"] = None, False


def room_view(r, pid):
    me = r["players"].get(pid)
    t = r["track"]
    if not me:
        return {"me": False, "round": r["round"], "track": {"title": t["title"], "artist": t["artist"]}, "kicked": pid in r["banned"]}
    me["seen"] = time.time()
    r["seen"] = me["seen"]
    room_auto(r)
    now = time.time()
    plist = sorted(r["players"].items(), key=lambda kv: kv[1]["n"])
    return {"me": True, "code": r["code"], "round": r["round"], "crit": r["crit"], "start": r["start"],
            "track": {"title": t["title"], "artist": t["artist"], "album": t["album"], "note": t.get("note", ""), "cover": bool(t["cover"]), "audio": bool(t["audio"] and r["share"])},
            "players": [{"id": p["n"], "name": p["name"], "done": p["done"], "host": p["host"], "admin": bool(p["host"] or p.get("admin")), "online": now - p["seen"] < 8, "me": q == pid} for q, p in plist],
            "adm": bool(me["host"] or me.get("admin")),
            "dec": r["dec"], "revealed": r["revealed"], "result": r["result"] if r["revealed"] else None,
            "mine": {"scores": me["scores"], "done": me["done"]}, "history": r["history"]}


def lan_ips():
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    return [i for i in ips if not i.startswith("127.") and not i.startswith("169.254.")]


def room_server():
    with ROOM_LOCK:
        if ROOM_SRV["srv"]:
            return ROOM_SRV["port"]
        for port in range(ROOM_PORT0, ROOM_PORT0 + 20):
            try:
                srv = ThreadingHTTPServer(("0.0.0.0", port), RoomHandler)
            except OSError:
                continue
            ROOM_SRV["srv"], ROOM_SRV["port"] = srv, port
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            return port
    return None


class RoomHandler(Handler):
    """Единственное, что видят друзья: страница комнаты и её API."""

    def _body(self, limit=6000):
        try:
            n = min(int(self.headers.get("Content-Length", 0)), limit)
            d = json.loads(self.rfile.read(n).decode("utf-8"))
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _room(self, code):
        return ROOMS.get(str(code or "").upper()[:10])

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        g = lambda k: (q.get(k) or [""])[0]
        if u.path == "/" or re.fullmatch(r"/r/[A-Za-z0-9]{3,10}", u.path):
            return self._send(200, ROOM_HTML.encode("utf-8"), "text/html; charset=utf-8")
        if u.path == "/room/api/state":
            with ROOM_LOCK:
                r = self._room(g("c"))
                if not r:
                    return self._json(404, {"err": "no_room"})
                return self._json(200, room_view(r, g("p")))
        m = re.fullmatch(r"/room/(audio|cover)/([A-Za-z0-9]{3,10})", u.path)
        if m:
            with ROOM_LOCK:
                r = self._room(m.group(2))
                if not r:
                    return self._send(404)
                t = dict(r["track"])
                ok = g("p") in r["players"]
                share = r["share"]
            if m.group(1) == "cover":
                return self._img_get("cover", t["cover"]) if t["cover"] else self._send(404)
            if not (ok and share and t["audio"]):
                return self._send(403)
            return self._audio_get(t["audio"])
        self._send(404)

    def do_POST(self):
        b = self._body()
        with ROOM_LOCK:
            r = self._room(b.get("c"))
            if not r:
                return self._json(404, {"err": "no_room"})
            host = secrets.compare_digest(str(b.get("k") or ""), r["key"])
            me = r["players"].get(str(b.get("p") or ""))
            adm = host or bool(me and (me["host"] or me.get("admin")))      # хозяин или назначенный им админ
            path = self.path.split("?")[0]
            if path == "/room/api/rename":
                if not me:
                    return self._json(403, {})
                me["name"] = room_name(r, b.get("name"), admin=bool(me["host"] or me.get("admin")), skip=str(b.get("p")))
                return self._json(200, {"name": me["name"]})
            if path == "/room/api/admin":
                if not host:
                    return self._json(403, {})
                try:
                    n = int(b.get("n"))
                except (TypeError, ValueError):
                    return self._json(400, {})
                for kv in r["players"].values():
                    if kv["n"] == n and not kv["host"]:
                        kv["admin"] = bool(b.get("on"))
                        return self._json(200, {})
                return self._json(404, {})
            if path == "/room/api/join":
                p = str(b.get("p") or "")
                if p in r["players"]:
                    return self._json(200, {"p": p})
                if p in r["banned"]:
                    return self._json(403, {"msg": "Администратор исключил тебя из этой комнаты"})
                if len(r["players"]) >= ROOM_MAX_PLAYERS:
                    return self._json(403, {"msg": "В комнате уже максимум игроков"})
                return self._json(200, {"p": room_add_player(r, b.get("name"))})
            if path == "/room/api/rate":
                me = r["players"].get(str(b.get("p") or ""))
                if not me or r["revealed"]:
                    return self._json(403, {})
                sc = b.get("scores")
                if not isinstance(sc, list) or len(sc) != len(r["crit"]):
                    return self._json(400, {})
                try:
                    sc = [round(min(10.0, max(0.0, float(x))), 1) for x in sc]
                    if not r["dec"]:
                        sc = [int(math.floor(x + 0.5)) for x in sc]      # десятичные выключены — только целые
                except (TypeError, ValueError):
                    return self._json(400, {})
                me["scores"], me["done"], me["seen"] = sc, bool(b.get("done")), time.time()
                room_auto(r)
                return self._json(200, {})
            if path == "/room/api/reveal":
                if not adm:
                    return self._json(403, {})
                return self._json(200 if room_reveal(r) else 400, {})
            if path == "/room/api/close":
                if not host:
                    return self._json(403, {})
                ROOMS.pop(r["code"], None)
                return self._json(200, {})
            if path == "/room/api/kick":
                if not adm:
                    return self._json(403, {})
                try:
                    n = int(b.get("n"))
                except (TypeError, ValueError):
                    return self._json(400, {})
                for kp, kv in list(r["players"].items()):
                    if kv["n"] == n and not kv["host"] and (host or not kv.get("admin")):
                        r["players"].pop(kp, None)
                        r["banned"].add(kp)
                        room_auto(r)
                        return self._json(200, {})
                return self._json(404, {})
            if path == "/room/api/leave":
                lp = str(b.get("p") or "")
                lv = r["players"].get(lp)
                if not lv or lv["host"]:
                    return self._json(403, {})
                r["players"].pop(lp, None)
                room_auto(r)
                return self._json(200, {})
            if path == "/room/api/note":
                if not adm:
                    return self._json(403, {})
                r["track"]["note"] = _rclean(b.get("note"), 80)
                return self._json(200, {})
        self._json(404, {})

    def do_DELETE(self):
        self._send(405)

    def do_PUT(self):
        self._send(405)

    def do_HEAD(self):
        self._send(405)


def press_f11():
    """Нажимает F11 в активном окне (Windows): так кнопка в углу выходит из полного экрана окна браузера."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        u = ctypes.windll.user32
        u.keybd_event(0x7A, 0, 0, 0)   # F11 вниз
        u.keybd_event(0x7A, 0, 2, 0)   # F11 вверх
        return True
    except Exception:
        return False


def find_browsers():
    """Все Chromium-браузеры на компьютере (умеют открываться окном приложения через --app), в порядке предпочтения."""
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    la = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        pf86 + r"\Microsoft\Edge\Application\msedge.exe",
        pf + r"\Microsoft\Edge\Application\msedge.exe",
        pf + r"\Google\Chrome\Application\chrome.exe",
        pf86 + r"\Google\Chrome\Application\chrome.exe",
        la + r"\Google\Chrome\Application\chrome.exe",
        la + r"\Yandex\YandexBrowser\Application\browser.exe",
        pf + r"\Yandex\YandexBrowser\Application\browser.exe",
        pf + r"\BraveSoftware\Brave-Browser\Application\brave.exe",
        la + r"\BraveSoftware\Brave-Browser\Application\brave.exe",
        la + r"\Vivaldi\Application\vivaldi.exe",
        pf + r"\Vivaldi\Application\vivaldi.exe",
        la + r"\Chromium\Application\chrome.exe",
    ]
    for name in ("msedge", "chrome", "google-chrome", "chromium", "chromium-browser", "brave", "vivaldi"):
        p = shutil.which(name)
        if p:
            candidates.append(p)
    out, seen = [], set()
    for c in candidates:
        k = (c or "").lower()
        if c and k not in seen and os.path.isfile(c):
            seen.add(k)
            out.append(c)
    return out


def find_browser():
    b = find_browsers()
    return b[0] if b else None


def launch_log(msg):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(DATA_DIR / "launch.log", "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except Exception:
        pass


def open_app_window(url):
    """Открывает интерфейс окном приложения. Если браузер повреждён (например, WinError 14001 у «урезанной» Edge
    на сборках Windows без Edge) — пробует следующий, а в крайнем случае открывает обычную вкладку браузера по умолчанию."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Без «--disable-frame-rate-limit»: он заставлял окно рисовать сотни кадров в секунду и грузил ноутбуки до лагов.
    # «--force_high_performance_gpu» — на ноутбуках с двумя видеокартами берём мощную, а не встроенную.
    fps_flags = ["--force_high_performance_gpu"]
    for browser in find_browsers():
        try:
            proc = subprocess.Popen([
                browser,
                f"--app={url}",
                f"--user-data-dir={DATA_DIR / 'profile'}",
                "--window-size=1360,880",
                "--no-first-run",
                "--start-fullscreen",
                "--autoplay-policy=no-user-gesture-required",
                "--no-default-browser-check",
                "--enable-gpu-rasterization",
                "--ignore-gpu-blocklist",
                "--enable-zero-copy",
                "--disable-features=CalculateNativeWinOcclusion",
                *fps_flags,
            ])
        except OSError as e:
            launch_log(f"не запустился {browser}: {e}")
            continue
        try:
            code = proc.wait(timeout=2.5)          # сразу упал с ошибкой — значит браузер нерабочий
            if code != 0:
                launch_log(f"{browser} закрылся с кодом {code}")
                continue
        except subprocess.TimeoutExpired:
            pass                                   # работает — отлично
        launch_log(f"открыт через {browser}")
        return proc
    launch_log("подходящий браузер не найден — открываю браузер по умолчанию")
    try:
        if os.name == "nt":
            os.startfile(url)
        else:
            webbrowser.open(url)
    except Exception:
        webbrowser.open(url)
    return None


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/"

    proc = open_app_window(url)

    quit_now = False
    restart = False
    while True:
        time.sleep(0.5)
        now = time.time()
        if STATE["restart"] and now > STATE["restart"]:
            restart = True
            break
        if STATE["quit"] and now > STATE["quit"]:
            quit_now = True
            break
        if STATE["bye"] and now > STATE["bye"]:
            break
        if now - STATE["last"] > 180:
            break
    server.shutdown()
    if restart:                            # после обновления: закрываем старое окно и запускаем новую версию
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
        me = Path(STATE.get("restart_file") or APP_FILE)
        exe = sys.executable or "python"
        if os.name == "nt" and exe.lower().endswith("python.exe"):          # без чёрного окна консоли
            w = Path(exe).with_name("pythonw.exe")
            if w.exists():
                exe = str(w)
        try:
            subprocess.Popen([exe, str(me)], cwd=str(me.parent))
        except Exception as e:
            launch_log(f"не удалось перезапустить после обновления: {e}")
        return
    if quit_now and proc is not None:      # выход кнопкой: если окно не закрылось само — закрываем его
        try:
            proc.terminate()
        except Exception:
            pass


if __name__ == "__main__":
    main()
