/*
=====================================================================
 server.js  –  IQ Option + TradingView Snapshot Microservice
=====================================================================

Purpose
-------
Provide a lightweight HTTP service (Express + Puppeteer) that captures
chart screenshots for FX & OTC symbols from **IQ Option** *or* falls
back to **TradingView** symbols when needed. Designed to run locally,
on Render, or behind ngrok.

Key Features
------------
• Endpoints
    GET  /start-browser                – warm / (re)launch Chromium pool
    GET  /health                       – quick ok check
    GET  /pairs[.json]                 – list supported FX + OTC mapping
    GET  /run                          – capture chart screenshot
        query params:
            source=auto|iq|tv         – chart source selection (default auto)
            ticker=<symbol>           – e.g. EURUSD, EUR/USD, EURUSD-OTC
            interval=<tf>             – 1,5,15,60,D,W,M (TV only) (ignored IQ)
            theme=dark|light          – TradingView theme (ignored IQ)
            exchange=<TV exch>        – override (TV mode)
            base=<TV base path>       – chart (default) /chart/<layout>/
            full=1                    – fullPage screenshot (default viewport)
            wait=ms                   – extra wait after load (default 4s)
            w=px h=px                – viewport size override
            debug=1                   – verbose logging overlay (console only)

• Rate limiting + throttle delay across requests (avoid Render OOM).
• Screenshot retry with exponential-ish backoff.
• Minimal in-memory LRU cache (per-URL) w/ TTL to avoid hammering.
• IQ Option loader: https://eu.iqoption.com/traderoom?instrument_type=...
• OTC detection (suffix -OTC), underlying real-market pair used for TV.
• TradingView fallback chain (FX, FX_IDC, OANDA, FOREXCOM, FXCM, IDC ...).
• Optional Telegram relay helper (sendPhoto) when query includes bot/chat.
• Graceful shutdown on SIGTERM/SIGINT for Render.
• Defensive error responses (JSON + HTTP 4xx/5xx).

Environment Variables
---------------------
PORT                      – service port (default 10000)
HEADLESS                  – 'true'|'false' (default true in production)
PUPPETEER_EXEC_PATH       – custom Chromium binary path (optional)
CHROME_EXECUTABLE_PATH    – alt name accepted
PUPPETEER_WS_ENDPOINT     – connect to existing browser over ws:// (optional)
MAX_CONCURRENT_PAGES      – concurrency limit (default 2)
SCREENSHOT_TIMEOUT_MS     – nav timeout (default 60000)
DEFAULT_THEME             – 'dark' (tv) fallback
DEFAULT_INTERVAL          – '1' (tv) fallback
DEFAULT_TV_BASE           – 'chart'
DEFAULT_TV_EXCHANGE       – 'FX'

Optional Telegram Relay (convenience only; main bot handled in Python)
---------------------------------------------------------------------
TELEGRAM_BOT_TOKEN        – if set, /run?bot=1&chat_id=<id> sends screenshot
TELEGRAM_PARSE_MODE       – default Markdown | HTML (ignored screenshot)

Logging
-------
Logs to stdout (Render collects). You can pipe to a file externally if desired.

Security Notes
--------------
• No auth by default. Restrict ingress at reverse proxy if needed.
• Puppeteer launched with hardened args (no-sandbox etc.) for cloud.
• IQ Option pages may redirect to login; we screenshot whatever renders.

=====================================================================
*/

'use strict';

//---------------------------------------------------------------
// Imports
//---------------------------------------------------------------
const path        = require('path');
const fs          = require('fs');
const os          = require('os');
const express     = require('express');
const bodyParser  = require('body-parser');
const FormData    = require('form-data');
const http        = require('http');
const https       = require('https');
const { URL }     = require('url');
let   puppeteer   = require('puppeteer'); // lazy reassign if connect

//---------------------------------------------------------------
// Env Helpers
//---------------------------------------------------------------
const ENV = (k, def=null) => process.env[k] ?? def;

const PORT                 = Number(ENV('PORT', 10000));
const HEADLESS             = /^false$/i.test(ENV('HEADLESS','')) ? false : true;
const EXEC_PATH            = ENV('PUPPETEER_EXEC_PATH', ENV('CHROME_EXECUTABLE_PATH', null));
const WS_ENDPOINT          = ENV('PUPPETEER_WS_ENDPOINT', null); // puppeteer.connect() if set
const MAX_CONCURRENT_PAGES = Number(ENV('MAX_CONCURRENT_PAGES', 2));
const SCREENSHOT_TIMEOUT   = Number(ENV('SCREENSHOT_TIMEOUT_MS', 60000));
const DEFAULT_THEME        = ENV('DEFAULT_THEME', 'dark');
const DEFAULT_INTERVAL     = ENV('DEFAULT_INTERVAL', '1');
const DEFAULT_TV_BASE      = ENV('DEFAULT_TV_BASE', 'chart');
const DEFAULT_TV_EXCHANGE  = ENV('DEFAULT_TV_EXCHANGE', 'FX');
const TELEGRAM_BOT_TOKEN   = ENV('TELEGRAM_BOT_TOKEN', null);
const TELEGRAM_PARSE_MODE  = ENV('TELEGRAM_PARSE_MODE', 'Markdown');

//---------------------------------------------------------------
// Pair Mapping (FX + OTC)  — keep EXACT visible labels
//---------------------------------------------------------------
const FX_PAIRS = [
  'EUR/USD','GBP/USD','USD/JPY','USD/CHF','AUD/USD',
  'NZD/USD','USD/CAD','EUR/GBP','EUR/JPY','GBP/JPY',
  'AUD/JPY','NZD/JPY','EUR/AUD','GBP/AUD','EUR/CAD',
  'USD/MXN','USD/TRY','USD/ZAR','AUD/CHF','EUR/CHF',
];

const OTC_PAIRS = [
  'EUR/USD-OTC','GBP/USD-OTC','USD/JPY-OTC','USD/CHF-OTC','AUD/USD-OTC',
  'NZD/USD-OTC','USD/CAD-OTC','EUR/GBP-OTC','EUR/JPY-OTC','GBP/JPY-OTC',
  'AUD/CHF-OTC','EUR/CHF-OTC','KES/USD-OTC','MAD/USD-OTC',
  'USD/BDT-OTC','USD/MXN-OTC','USD/MYR-OTC','USD/PKR-OTC',
];

const ALL_PAIRS = [...FX_PAIRS, ...OTC_PAIRS];

// Underlying symbol mapping for OTC -> use real-market pair for TV fallback.
const UNDERLYING_OTC_MAP = {
  'EUR/USD-OTC':'EURUSD','GBP/USD-OTC':'GBPUSD','USD/JPY-OTC':'USDJPY',
  'USD/CHF-OTC':'USDCHF','AUD/USD-OTC':'AUDUSD','NZD/USD-OTC':'NZDUSD',
  'USD/CAD-OTC':'USDCAD','EUR/GBP-OTC':'EURGBP','EUR/JPY-OTC':'EURJPY',
  'GBP/JPY-OTC':'GBPJPY','AUD/CHF-OTC':'AUDCHF','EUR/CHF-OTC':'EURCHF',
  'KES/USD-OTC':'USDKES','MAD/USD-OTC':'USDMAD','USD/BDT-OTC':'USDBDT',
  'USD/MXN-OTC':'USDMXN','USD/MYR-OTC':'USDMYR','USD/PKR-OTC':'USDPKR'
};

// Utility canonical key: remove spaces + / + case
function canonKey(str){
  return str.trim().toUpperCase().replace(/\s+/g,'').replace(/[\/]/g,'');
}

// Build quick lookup -> {canon: {label, iqTicker, isOTC}}
const PAIR_MAP = {};
for (const p of FX_PAIRS){
  PAIR_MAP[canonKey(p)] = { label:p, iqTicker:p.replace('/',''), isOTC:false };
}
for (const p of OTC_PAIRS){
  const tk = UNDERLYING_OTC_MAP[p] || p.replace('/','').replace('-OTC','');
  PAIR_MAP[canonKey(p)] = { label:p, iqTicker:tk, isOTC:true };
}

//---------------------------------------------------------------
// TradingView Exchange Fallback Chain
//---------------------------------------------------------------
const TV_EXCH_FALLBACKS = [
  'FX','CURRENCY','QUOTEX','FX_IDC','OANDA','FOREXCOM','FXCM','IDC'
];

//---------------------------------------------------------------
// Basic Interval Normalizer (TradingView)
//---------------------------------------------------------------
function normInterval(tf){
  if (!tf) return DEFAULT_INTERVAL;
  const t=tf.toString().trim().toLowerCase();
  if (/^\d+m?$/.test(t)){return t.replace('m','');}
  if (/^\d+h$/.test(t)){return String(parseInt(t)*60);} // hours->minutes
  if (t==='d'||t==='1d'||t==='day') return 'D';
  if (t==='w'||t==='1w'||t==='week') return 'W';
  if (t==='m'||t==='1m'||t==='mo'||t==='month') return 'M';
  return DEFAULT_INTERVAL;
}

function normTheme(th){
  if (!th) return DEFAULT_THEME;
  return /^l/i.test(th) ? 'light' : 'dark';
}

//---------------------------------------------------------------
// Symbol Resolver
//---------------------------------------------------------------
/**
 * resolveSymbol(raw) -> { label, iqTicker, tvTicker, isOTC, tvExchList }
 *
 * Accepts inputs like:
 *   EUR/USD
 *   EURUSD
 *   EUR/USD-OTC
 *   QUOTEX:EURUSD
 *   FX:EURUSD
 */
function resolveSymbol(raw){
  if (!raw) raw='EUR/USD';
  let s = String(raw).trim();
  let isOTC=false;
  let exExplicit=null;
  let tkExplicit=null;

  // EX:TK path e.g. FX:EURUSD, QUOTEX:GBPUSD
  if (s.includes(':')){
    const [ex,tk]=s.split(':',2);
    exExplicit=ex.toUpperCase();
    tkExplicit=tk.toUpperCase();
    // crude OTC detection
    if (/-OTC$/i.test(tkExplicit)){
      isOTC=true;
      tkExplicit=tkExplicit.replace(/-OTC$/i,'');
    }
    return {
      label:s,
      iqTicker:tkExplicit,
      tvTicker:tkExplicit,
      isOTC,
      tvExchList:[exExplicit]
    };
  }

  // canonical lookup
  const key = canonKey(s);
  if (PAIR_MAP[key]){
    const {label, iqTicker, isOTC:mapOTC} = PAIR_MAP[key];
    isOTC = mapOTC;
    const tvTicker = iqTicker; // they match for our usage
    return {
      label,
      iqTicker,
      tvTicker,
      isOTC,
      tvExchList:TV_EXCH_FALLBACKS.slice()
    };
  }

  // fallback scrub punctuation -> uppercase
  const scrub = s.toUpperCase().replace(/[^A-Z0-9]/g,'');
  return {
    label:s,
    iqTicker:scrub,
    tvTicker:scrub,
    isOTC:/-OTC$/i.test(s),
    tvExchList:TV_EXCH_FALLBACKS.slice()
  };
}

//---------------------------------------------------------------
// URL Builders
//---------------------------------------------------------------
function buildIqUrl(iqTicker, isOTC){
  // IQ Option uses instrument_type=forex|otc. Some tickers need login.
  const type = isOTC ? 'otc' : 'forex';
  return `https://eu.iqoption.com/traderoom?instrument_type=${type}&active=${encodeURIComponent(iqTicker)}`;
}

function buildTvUrl({base=DEFAULT_TV_BASE, exchange=DEFAULT_TV_EXCHANGE, tvTicker='EURUSD', interval=DEFAULT_INTERVAL, theme=DEFAULT_THEME}){
  // Ensure /chart/ style path
  const hasQ = base.includes('?');
  const prefix = hasQ ? base : `${base}/?`;
  const u = `https://www.tradingview.com/${prefix}symbol=${encodeURIComponent(exchange+':'+tvTicker)}&interval=${encodeURIComponent(interval)}&theme=${encodeURIComponent(theme)}`;
  return u;
}

//---------------------------------------------------------------
// Minimal In-Memory LRU-ish Cache  (by url)
//---------------------------------------------------------------
const CACHE_MAX = 50;
const CACHE_TTL_MS = 10_000; // 10s default
const _cache = new Map(); // url -> {ts, buf}
function cacheGet(url){
  const v=_cache.get(url);
  if (!v) return null;
  if ((Date.now()-v.ts)>CACHE_TTL_MS){_cache.delete(url);return null;}
  return v.buf;
}
function cachePut(url,buf){
  _cache.set(url,{ts:Date.now(),buf});
  if (_cache.size> CACHE_MAX){
    // drop oldest
    const oldest=[..._cache.entries()].sort((a,b)=>a[1].ts-b[1].ts)[0][0];
    _cache.delete(oldest);
  }
}

//---------------------------------------------------------------
// Simple Async Queue for Page Concurrency
//---------------------------------------------------------------
class AsyncQueue{
  constructor(limit=1){
    this.limit=limit;
    this.active=0;
    this.q=[]; // array of resolvers
  }
  async acquire(){
    if (this.active < this.limit){
      this.active++;return;
    }
    return new Promise(res=>this.q.push(res));
  }
  release(){
    this.active--;
    if (this.active<0) this.active=0;
    const next=this.q.shift();
    if (next){this.active++;next();}
  }
}

//---------------------------------------------------------------
// Browser Manager
//---------------------------------------------------------------
class BrowserManager{
  constructor(){
    this.browser=null;
    this.queue=new AsyncQueue(MAX_CONCURRENT_PAGES);
    this.launching=null;
    this.closed=false;
  }
  async _doLaunch(){
    if (this.launching) return this.launching;
    this.launching = (async()=>{
      if (WS_ENDPOINT){
        console.log('[Browser] Connecting to existing ws endpoint %s', WS_ENDPOINT);
        this.browser = await puppeteer.connect({browserWSEndpoint:WS_ENDPOINT});
      }else{
        const args=[
          '--no-sandbox','--disable-setuid-sandbox','--disable-dev-shm-usage',
          '--disable-accelerated-2d-canvas','--disable-gpu','--no-zygote',
          '--single-process','--window-size=1920,1080'
        ];
        const opts={headless:HEADLESS, args};
        if (EXEC_PATH) opts.executablePath=EXEC_PATH;
        console.log('[Browser] Launching Chromium...');
        this.browser= await puppeteer.launch(opts);
      }
      this.browser.on('disconnected',()=>{console.log('[Browser] disconnected');this.browser=null;});
      return this.browser;
    })();
    return this.launching;
  }
  async ensure(){
    if (this.browser) return this.browser;
    return this._doLaunch();
  }
  async newPage(){
    const br = await this.ensure();
    await this.queue.acquire();
    const page = await br.newPage();
    // standard UA / viewport
    await page.setUserAgent('Mozilla/5.0 (X11; Linux x86_64)');
    await page.setViewport({width:1920,height:1080,deviceScaleFactor:1});
    page.on('close',()=>this.queue.release());
    return page;
  }
  async close(){
    this.closed=true;
    try{if (this.browser) await this.browser.close();}catch(_){/*ignore*/}
    this.browser=null;
  }
}

const browserMgr = new BrowserManager();

//---------------------------------------------------------------
// IQ Option Screenshot
//---------------------------------------------------------------
async function screenshotIq({iqTicker,isOTC,waitMs=4000,fullPage=false,viewport}){
  const url = buildIqUrl(iqTicker,isOTC);
  const page = await browserMgr.newPage();
  try{
    if (viewport){await page.setViewport(viewport);} // override
    await page.goto(url,{waitUntil:'networkidle2',timeout:SCREENSHOT_TIMEOUT});
    await page.waitForTimeout(waitMs);
    // optionally hide cookie banners etc.
    await safeDismissOverlays(page);
    const buf = await page.screenshot({type:'png',fullPage});
    return {ok:true,buf,url};
  }catch(err){
    console.error('[IQ] screenshot error',err);
    return {ok:false,error:String(err),url};
  }finally{
    try{await page.close();}catch(_){/* */}
  }
}

//---------------------------------------------------------------
// TradingView Screenshot
//---------------------------------------------------------------
async function screenshotTv({base=DEFAULT_TV_BASE,exchange=DEFAULT_TV_EXCHANGE,tvTicker,interval=DEFAULT_INTERVAL,theme=DEFAULT_THEME,waitMs=4000,fullPage=false,viewport}){
  const url = buildTvUrl({base,exchange,tvTicker,interval,theme});
  const page = await browserMgr.newPage();
  try{
    if (viewport){await page.setViewport(viewport);} // override
    await page.goto(url,{waitUntil:'networkidle2',timeout:SCREENSHOT_TIMEOUT});
    await page.waitForTimeout(waitMs);
    await safeDismissOverlays(page);
    const buf = await page.screenshot({type:'png',fullPage});
    return {ok:true,buf,url};
  }catch(err){
    console.error('[TV] screenshot error',err);
    return {ok:false,error:String(err),url};
  }finally{
    try{await page.close();}catch(_){/* */}
  }
}

//---------------------------------------------------------------
// Overlay Dismiss Helpers (best-effort; non-fatal)
//---------------------------------------------------------------
async function safeDismissOverlays(page){
  try{
    await page.evaluate(()=>{
      // Basic removal attempts; safe no-ops if elements not found.
      const sel=[
        '[data-name="cookies-ok"]','button[aria-label*="Accept"]',
        '.tv-dialog__close','.js-notice-close','.i-close','.i-remove'
      ];
      sel.forEach(s=>{document.querySelectorAll(s).forEach(n=>n.click?.());});
    });
  }catch(_){/* ignore */}
}

//---------------------------------------------------------------
// Telegram Relay Helpers (optional convenience; main bot separate)
//---------------------------------------------------------------
async function telegramSendPhoto(buffer,chatId,caption=''){
  if (!TELEGRAM_BOT_TOKEN || !chatId) return;
  try{
    const form = new FormData();
    form.append('chat_id', String(chatId));
    if (caption) form.append('caption', caption);
    form.append('photo', buffer, {filename:'snapshot.png',contentType:'image/png'});
    const resp = await fetch(`https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendPhoto`,{
      method:'POST', body:form
    });
    if (!resp.ok){
      console.error('[Telegram] sendPhoto failed',resp.status,await resp.text());
    }
  }catch(err){console.error('[Telegram] sendPhoto error',err);}  
}

async function telegramSendMessage(chatId,text){
  if (!TELEGRAM_BOT_TOKEN || !chatId) return;
  try{
    const resp = await fetch(`https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage`,{
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({chat_id:chatId,text,parse_mode:TELEGRAM_PARSE_MODE})
    });
    if(!resp.ok){console.error('[Telegram] sendMessage failed',resp.status,await resp.text());}
  }catch(err){console.error('[Telegram] sendMessage error',err);}  
}

//---------------------------------------------------------------
// Express App Setup
//---------------------------------------------------------------
const app = express();
app.disable('x-powered-by');
app.use(bodyParser.json({limit:'1mb'}));
app.use(bodyParser.urlencoded({extended:true}));

//---------------------------------------------------------------
// GET /health
//---------------------------------------------------------------
app.get('/health',(req,res)=>{res.status(200).json({ok:true,uptime:process.uptime()});});

//---------------------------------------------------------------
// GET /start-browser – ensure Chromium session
//---------------------------------------------------------------
app.get('/start-browser', async (req,res)=>{
  try{await browserMgr.ensure();res.type('text/plain').send('✅ Browser ready');}
  catch(err){console.error(err);res.status(500).send('Browser launch failed: '+err.message);}  
});

//---------------------------------------------------------------
// GET /pairs (HTML) & /pairs.json
//---------------------------------------------------------------
app.get('/pairs.json',(req,res)=>{
  const out = ALL_PAIRS.map(p=>{
    const r=resolveSymbol(p);return {label:r.label,iqTicker:r.iqTicker,isOTC:r.isOTC};
  });
  res.json({pairs:out});
});

app.get('/pairs',(req,res)=>{
  let html=['<html><head><title>Pairs</title><meta charset="utf-8"></head><body>'];
  html.push('<h1>Supported Pairs</h1>');
  html.push('<h2>FX</h2><ul>');
  FX_PAIRS.forEach(p=>html.push(`<li>${p}</li>`));
  html.push('</ul><h2>OTC</h2><ul>');
  OTC_PAIRS.forEach(p=>html.push(`<li>${p}</li>`));
  html.push('</ul></body></html>');
  res.type('html').send(html.join(''));
});

//---------------------------------------------------------------
// Internal: attempt screenshot auto chain
//---------------------------------------------------------------
async function attemptAutoScreenshot({symbolInfo,interval,theme,waitMs,fullPage,viewport}){
  // 1) IQ Option attempt first
  const iq = await screenshotIq({iqTicker:symbolInfo.iqTicker,isOTC:symbolInfo.isOTC,waitMs,fullPage,viewport});
  if (iq.ok) return {...iq,src:'iq'};

  // 2) TradingView fallback chain
  for (const exch of symbolInfo.tvExchList){
    const tv = await screenshotTv({exchange:exch,tvTicker:symbolInfo.tvTicker,interval,theme,waitMs,fullPage,viewport});
    if (tv.ok) return {...tv,src:'tv',exch};
  }
  return {ok:false,error:`All sources failed for ${symbolInfo.tvTicker}`};
}

//---------------------------------------------------------------
// GET /run – main screenshot endpoint
//---------------------------------------------------------------
app.get('/run', async (req,res)=>{
  const source   = String(req.query.source||'auto').toLowerCase();
  const rawTick  = req.query.ticker || req.query.symbol || 'EURUSD';
  const interval = normInterval(req.query.interval||DEFAULT_INTERVAL);
  const theme    = normTheme(req.query.theme||DEFAULT_THEME);
  const base     = req.query.base || DEFAULT_TV_BASE;
  const exchQ    = (req.query.exchange||'').toString();
  const fullPage = !!req.query.full;
  const waitMs   = Number(req.query.wait||4000);
  const vw       = req.query.w?Number(req.query.w):null;
  const vh       = req.query.h?Number(req.query.h):null;
  const viewport = (vw&&vh)?{width:vw,height:vh,deviceScaleFactor:1}:null;
  const debug    = !!req.query.debug;
  const chatId   = req.query.chat_id || null; // optional Telegram relay

  const symbolInfo = resolveSymbol(rawTick);
  if (debug) console.log('[RUN] request', {source,rawTick,interval,theme,base,exchQ,symbolInfo});

  // Build cache key
  const cacheKey = JSON.stringify({source,rawTick,interval,theme,base,exchQ,fullPage,vw,vh});
  const cached = cacheGet(cacheKey);
  if (cached){
    if (chatId) telegramSendPhoto(cached,chatId,`${symbolInfo.label} (cache)`);
    res.type('image/png');res.set('X-Cache','HIT');return res.send(cached);
  }

  // choose path
  let result;
  if (source==='iq'){
    result = await screenshotIq({iqTicker:symbolInfo.iqTicker,isOTC:symbolInfo.isOTC,waitMs,fullPage,viewport});
  }else if (source==='tv'){
    const exchange = exchQ || DEFAULT_TV_EXCHANGE;
    result = await screenshotTv({base,exchange,tvTicker:symbolInfo.tvTicker,interval,theme,waitMs,fullPage,viewport});
  }else{ // auto
    result = await attemptAutoScreenshot({symbolInfo,interval,theme,waitMs,fullPage,viewport});
  }

  if (!result.ok){
    const msg = result.error || 'Unknown error';
    return res.status(404).type('text/plain').send('Not Found\n'+msg);
  }

  cachePut(cacheKey,result.buf);
  if (chatId) telegramSendPhoto(result.buf,chatId,`${symbolInfo.label}`);
  res.type('image/png');
  res.set('Cache-Control','no-store');
  res.send(result.buf);
});

//---------------------------------------------------------------
// Fallback root page (simple info)
//---------------------------------------------------------------
app.get('/',(req,res)=>{
  res.type('text/plain').send('TradingView / IQ Option Snapshot Service. Try /run?ticker=EURUSD');
});

//---------------------------------------------------------------
// Start HTTP Server
//---------------------------------------------------------------
const server = http.createServer(app);
server.listen(PORT,()=>{
  console.log(`✅ Snapshot service listening on port ${PORT}`);
});

//---------------------------------------------------------------
// Graceful Shutdown
//---------------------------------------------------------------
['SIGINT','SIGTERM'].forEach(sig=>{
  process.on(sig,async()=>{
    console.log(`\n${sig} received. Closing...`);
    try{server.close();}catch(_){/* */}
    await browserMgr.close();
    process.exit(0);
  });
});

//---------------------------------------------------------------
// Self-test (optional) if run directly and SELF_TEST=1
//---------------------------------------------------------------
if (require.main === module && ENV('SELF_TEST','0')==='1'){
  (async()=>{
    await fetch(`http://localhost:${PORT}/start-browser`).catch(()=>{});
    const r= await fetch(`http://localhost:${PORT}/run?ticker=EURUSD&source=tv`);
    console.log('SELF_TEST status',r.status,'len',r.headers.get('content-length'));
  })();
}

//---------------------------------------------------------------
// End of file
//---------------------------------------------------------------
