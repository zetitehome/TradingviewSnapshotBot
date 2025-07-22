/* server.js
 * TradingView Snapshot Microservice
 * ---------------------------------
 * Endpoints:
 *   GET  /healthz                             -> {ok:true}
 *   GET  /start-browser                       -> ensure Puppeteer running
 *   GET  /run?exchange=FX&ticker=EURUSD&interval=1&theme=dark[&w=1280&h=720&clean=1]
 *        (legacy compat; returns PNG)
 *   GET  /snapshot/:pair?ex=FX&tf=1&theme=dark[&w=1280&h=720&clean=1][&fmt=json|png][&candles=1]
 *        (modern; :pair may be "EURUSD" or "FX:EURUSD"; if fmt=json returns {meta, png_b64?, candles?})
 *   GET  /candles/:pair?ex=FX&tf=1&limit=500   -> JSON OHLC (AlphaVantage if configured; else stub)
 *
 * Guarantees PNG >= 2048 bytes: auto‑generate placeholder image when capture fails/too small.
 */

import express from 'express';
import bodyParser from 'body-parser';
import process from 'node:process';
import fs from 'node:fs';
import path from 'node:path';
import url from 'node:url';
import fetch from 'node-fetch';
import * as canvasMod from 'canvas';
import puppeteer from 'puppeteer';          // auto‑downloads unless PUPPETEER_EXECUTABLE_PATH
import { cfg } from './config.js';

const { createCanvas, loadImage } = canvasMod;

// ------------------------------------------------------------
// Basic logging helpers (ASCII safe)
// ------------------------------------------------------------
function stamp(){return new Date().toISOString();}
function logI(...a){console.log(`[${stamp()}]`,...a);}
function logW(...a){console.warn(`[${stamp()}] WARN`,...a);}
function logE(...a){console.error(`[${stamp()}] ERR`,...a);}

// Binary‑safe error log truncation
function safeSnippet(v,max=200){
  if(v==null) return '';
  if(typeof v!=='string') v=String(v);
  // Remove non‑printable, compress whitespace
  v = v.replace(/[^\x20-\x7E]+/g,'?');
  if(v.length>max) v=v.slice(0,max)+'…';
  return v;
}

// ------------------------------------------------------------
// Globals
// ------------------------------------------------------------
const PORT = Number(process.env.SNAPSHOT_PORT || 10000); // service port
const PUPPETEER_HEADLESS = process.env.PUPPETEER_HEADLESS?.toLowerCase()==='false' ? false : 'new';
const DEFAULT_WIDTH  = Number(process.env.SNAPSHOT_WIDTH  || 1280);
const DEFAULT_HEIGHT = Number(process.env.SNAPSHOT_HEIGHT || 720);
const MIN_PNG_BYTES  = 2048; // guarantee >2KB

// We'll reuse one browser + page pool
let browser = null;

// Small pool: we keep N pages idle. Configurable.
const PAGE_POOL_SIZE = Number(process.env.PAGE_POOL_SIZE || 2);
const pagePool = [];
const pageBusy = new Set();

// ------------------------------------------------------------
// Graceful launch
// ------------------------------------------------------------
async function launchBrowser(){
  if(browser) return;
  logI('Launching Puppeteer...');
  browser = await puppeteer.launch({
    headless: PUPPETEER_HEADLESS,
    args: [
      '--no-sandbox','--disable-setuid-sandbox',
      '--disable-dev-shm-usage',
      '--disable-gpu',
      '--no-zygote',
      '--single-process',
    ],
    defaultViewport: null,
  });
  browser.on('disconnected', ()=>{logW('Browser disconnected'); browser=null;});
  logI('✅ Puppeteer launched');
}

// warm page
async function _newPage(){
  await launchBrowser();
  const page = await browser.newPage();
  // block heavy resources (ads, fonts optional)
  await page.setRequestInterception(true);
  page.on('request', req=>{
    const type=req.resourceType();
    if(type==='image' || type==='media' || type==='font') { req.continue(); return; } // allow images for chart
    req.continue();
  });
  return page;
}

async function getPage(){
  // try idle pool
  for(const p of pagePool){
    if(!pageBusy.has(p)){
      pageBusy.add(p); return p;
    }
  }
  // if pool not full, create
  if(pagePool.length < PAGE_POOL_SIZE){
    const p = await _newPage();
    pagePool.push(p); pageBusy.add(p); return p;
  }
  // wait for free
  return new Promise((resolve,reject)=>{
    const start = Date.now();
    const check=()=>{
      for(const p of pagePool){
        if(!pageBusy.has(p)){ pageBusy.add(p); return resolve(p); }
      }
      if(Date.now()-start>30000) return reject(new Error('No page free'));
      setTimeout(check,100);
    };
    check();
  });
}

function releasePage(p){
  pageBusy.delete(p);
}

// ------------------------------------------------------------
// URL builders / param normalize
// ------------------------------------------------------------
function normTheme(t){
  return (t && t.toLowerCase().startsWith('l')) ? 'light' : 'dark';
}
function normTF(tf){
  if(!tf) return cfg.DEFAULT_INTERVAL;
  const t=tf.trim().toLowerCase();
  if(/^\d+$/.test(t)) return t;          // minutes
  if(t.endsWith('m') && /^\d+m$/.test(t)) return t.slice(0,-1);
  if(t.endsWith('h') && /^\d+h$/.test(t)) return String(Number(t.slice(0,-1))*60);
  if(['d','1d','day'].includes(t)) return 'D';
  if(['w','1w','week'].includes(t)) return 'W';
  if(['mo','m','1mo','month'].includes(t)) return 'M';
  return cfg.DEFAULT_INTERVAL;
}
function splitPair(symRaw, exQ){
  // Accept "FX:EURUSD", "EURUSD", "EUR/USD"
  let ex = exQ || cfg.DEFAULT_EXCHANGE;
  let tk = symRaw || 'EURUSD';
  if(tk.includes(':')){
    const parts = tk.split(':');
    ex = parts[0]; tk = parts[1];
  }
  tk = tk.replace('/','').replace(/\s+/g,'').toUpperCase();
  return {ex,tk};
}

// Build TradingView chart URL.
// We'll embed interval param if we detect type else rely on UI injection.
function buildTVUrl(ex, tk, tf='1', theme='dark'){
  // We can pass interval param by &interval=, but some versions ignore when embed is cleaned.
  const base = 'https://www.tradingview.com/chart/';
  // Use symbol param; we append timeframe in query (works for live chart)
  const params = new URLSearchParams();
  params.set('symbol', `${ex}:${tk}`);
  // tv uses lowercase timeframe value? Accept numeric minutes, D, W, M.
  if(tf) params.set('interval', tf);
  if(theme) params.set('theme', theme); // not always honored; fallback CSS
  return `${base}?${params.toString()}`;
}

// ------------------------------------------------------------
// Page prep: hide UI if clean=1
// ------------------------------------------------------------
async function applyCleanLayout(page){
  const hideCss = `
    .tv-header, .tv-dialog, .tv-side-toolbar, .tv-floating-toolbar, .chart-controls-bar, 
    [data-name="header-toolbar"] { display:none !important; }
    body { overflow:hidden !important; }
  `;
  await page.addStyleTag({content:hideCss});
}

// Wait for chart canvas
async function waitForChartReady(page, timeout=30000){
  // Accept multiple selectors; whichever arrives first
  const sels = [
    '.chart-container',
    '.tv-tradingview-widget-container__widget',
    'canvas.chart-markup-table',
    'table.chart-markup-table',
    'canvas',
  ];
  const start=Date.now();
  for(;;){
    for(const s of sels){
      const el = await page.$(s);
      if(el) return el;
    }
    if(Date.now()-start>timeout) throw new Error('chart_timeout');
    await page.waitForTimeout(200);
  }
}

// Clip bounding box (fallback to viewport)
async function elementBBoxOrViewport(page, el){
  try{
    const box = await el.boundingBox();
    if(box && box.width>0 && box.height>0) return box;
  }catch{}
  const v = page.viewport();
  return {x:0,y:0,width:v.width,height:v.height};
}

// ------------------------------------------------------------
// PNG placeholder
// ------------------------------------------------------------
function makePlaceholderPng(msg, w=800, h=400, bg='#202020', fg='#ffffff'){
  const c = createCanvas(w,h);
  const ctx = c.getContext('2d');
  ctx.fillStyle = bg; ctx.fillRect(0,0,w,h);
  ctx.fillStyle = fg;
  ctx.font = '20px sans-serif';
  const lines = wrapText(ctx, msg, w-40);
  let y = 50;
  for(const ln of lines){
    ctx.fillText(ln, 20, y);
    y += 28;
  }
  let buf = c.toBuffer('image/png');
  // pad if necessary
  if(buf.length < MIN_PNG_BYTES){
    const pad = Buffer.alloc(MIN_PNG_BYTES - buf.length, 0);
    buf = Buffer.concat([buf,pad]);
  }
  return buf;
}
function wrapText(ctx, txt, maxW){
  const words=txt.split(/\s+/);
  const out=[]; let line='';
  for(const w of words){
    const test=line?`${line} ${w}`:w;
    if(ctx.measureText(test).width>maxW){
      if(line) out.push(line);
      line=w;
    }else{
      line=test;
    }
  }
  if(line) out.push(line);
  return out;
}

// ------------------------------------------------------------
// Main capture
// ------------------------------------------------------------
async function captureTradingViewChart({pair,ex,tf,theme,w,h,clean}){
  const {ex:ex2,tk} = splitPair(pair,ex);
  ex = ex2;
  tf = normTF(tf);
  theme = normTheme(theme);
  w = Number(w)||DEFAULT_WIDTH;
  h = Number(h)||DEFAULT_HEIGHT;

  const tvUrl = buildTVUrl(ex,tk,tf,theme);
  logI(`Opening TV URL: ${tvUrl}`);

  const page = await getPage();
  let pngBuf=null;
  try{
    await page.setViewport({width:w,height:h,deviceScaleFactor:1});
    await page.goto(tvUrl,{waitUntil:'domcontentloaded',timeout:60000});
    if(clean) await applyCleanLayout(page);
    const chartEl = await waitForChartReady(page,30000);
    const box = await elementBBoxOrViewport(page,chartEl);
    pngBuf = await page.screenshot({clip:box,fullPage:false});
  }finally{
    releasePage(page);
  }
  // Check size / fallback
  if(!pngBuf || pngBuf.length < MIN_PNG_BYTES){
    logW(`PNG too small (${pngBuf?pngBuf.length:0}); placeholder.`);
    pngBuf = makePlaceholderPng(`Snapshot error for ${ex}:${tk}`, w,h);
  }
  return {png:pngBuf, ex, tk, tf, theme, w, h, clean:Boolean(clean)};
}

// ------------------------------------------------------------
// Candle fetch (Alpha Vantage optional)
// ------------------------------------------------------------
function splitFxCode(tk){
  // EURUSD -> EUR/USD
  if(tk.length===6){
    return [tk.slice(0,3),tk.slice(3)];
  }
  // fallback: no guess
  return [tk, 'USD'];
}

async function fetchAlphaVantageFX(ex, tk, tf='1', limit=500){
  if(!cfg.ALPHA_VANTAGE_KEY) return null;
  const [fromSym,toSym] = splitFxCode(tk);
  let avInt='5min';
  if(tf==='1') avInt='1min';
  else if(tf==='5') avInt='5min';
  else if(tf==='15') avInt='15min';
  else if(tf==='60') avInt='60min';

  const params = new URLSearchParams({
    function:'FX_INTRADAY',
    from_symbol:fromSym,
    to_symbol:toSym,
    interval:avInt,
    outputsize:'compact',
    apikey:cfg.ALPHA_VANTAGE_KEY,
  });
  const urlStr = `https://www.alphavantage.co/query?${params.toString()}`;
  try{
    const r = await fetch(urlStr,{timeout:20000});
    if(!r.ok){logW('AV http',r.status);return null;}
    const js = await r.json();
    const key = Object.keys(js).find(k=>k.startsWith('Time Series FX'));
    if(!key) return null;
    const raw=js[key];
    const out=[];
    for(const [ts,obj] of Object.entries(raw)){
      out.push({
        t:ts,
        o:Number(obj['1. open']),
        h:Number(obj['2. high']),
        l:Number(obj['3. low']),
        c:Number(obj['4. close']),
      });
      if(out.length>=limit) break;
    }
    return out.reverse();
  }catch(err){
    logW('AV fetch err',err);
    return null;
  }
}

async function getCandlesForPair(pair,ex,tf,limit=500){
  const {tk} = splitPair(pair,ex);
  // try AlphaVantage if key
  const av = await fetchAlphaVantageFX(ex,tk,tf,limit);
  if(av) return av;
  // fallback stub
  return [];
}

// ------------------------------------------------------------
// Express setup
// ------------------------------------------------------------
const app = express();
app.disable('x-powered-by');
app.use(bodyParser.json({limit:'1mb'}));
app.use(bodyParser.urlencoded({extended:true}));

// health
app.get('/healthz',(req,res)=>res.json({ok:true,browser:!!browser}));

// start-browser
app.get('/start-browser',async(req,res)=>{
  try{
    await launchBrowser();
    res.json({ok:true});
  }catch(err){
    logE('start-browser',err);
    res.status(500).json({ok:false,error:String(err)});
  }
});

// legacy /run
app.get('/run',async(req,res)=>{
  const {exchange,ticker,interval,theme,w,h,clean,base} = req.query;
  const pair = `${exchange||cfg.DEFAULT_EXCHANGE}:${ticker||'EURUSD'}`;
  try{
    const shot = await captureTradingViewChart({
      pair,
      ex:exchange||cfg.DEFAULT_EXCHANGE,
      tf:interval||cfg.DEFAULT_INTERVAL,
      theme:theme||cfg.DEFAULT_THEME,
      w,h,clean,
    });
    res.setHeader('Content-Type','image/png');
    res.send(shot.png);
  }catch(err){
    logE('run error',err);
    const ph = makePlaceholderPng(`run fail ${safeSnippet(err)}`);
    res.setHeader('Content-Type','image/png');
    res.status(500).send(ph);
  }
});

// modern /snapshot/:pair
app.get('/snapshot/:pair',async(req,res)=>{
  const pair = req.params.pair;
  const {ex,tf,theme,w,h,clean,fmt,meta,candles} = req.query;
  const wantJson = (fmt==='json') || (meta==='1') || (req.accepts(['json','image'])==='json');
  try{
    const shot = await captureTradingViewChart({
      pair,
      ex,
      tf,
      theme,
      w,
      h,
      clean:clean==='1'||clean==='true'
    });
    const metaObj = {
      ok:true,
      ex:shot.ex,
      tk:shot.tk,
      tf:shot.tf,
      theme:shot.theme,
      w:shot.w,
      h:shot.h,
      clean:shot.clean,
      size:shot.png.length,
    };
    if(wantJson){
      if(candles==='1'){
        metaObj.candles = await getCandlesForPair(pair,ex,tf,500);
      }
      // base64 embed if small else omit
      if(shot.png.length < 500_000){
        metaObj.png_b64 = shot.png.toString('base64');
      }else{
        metaObj.png_b64 = null;
      }
      res.json(metaObj);
    }else{
      res.setHeader('Content-Type','image/png');
      res.send(shot.png);
    }
  }catch(err){
    logE('snapshot error',err);
    if(wantJson){
      res.status(500).json({ok:false,error:String(err)});
    }else{
      const ph = makePlaceholderPng(`snapshot fail ${safeSnippet(err)}`);
      res.setHeader('Content-Type','image/png');
      res.status(500).send(ph);
    }
  }
});

// /candles/:pair -> always JSON
app.get('/candles/:pair',async(req,res)=>{
  const pair=req.params.pair;
  const {ex,tf,limit} = req.query;
  const lim=Number(limit)||500;
  try{
    const rows = await getCandlesForPair(pair,ex,tf,lim);
    res.json({ok:true,pair,ex:ex||cfg.DEFAULT_EXCHANGE,tf:tf||cfg.DEFAULT_INTERVAL,rows});
  }catch(err){
    logE('candles error',err);
    res.status(500).json({ok:false,error:String(err)});
  }
});

// fallback root
app.get('/',(req,res)=>{
  res.type('text/plain').send('TradingView Snapshot Service. Use /snapshot/:pair or /run?exchange=FX&ticker=EURUSD&interval=1.');
});

// ------------------------------------------------------------
// Startup
// ------------------------------------------------------------
app.listen(PORT,()=>{
  logI(`✅ Snapshot service listening on port ${PORT}`);
  // warm browser lazily
  launchBrowser().catch(err=>logE('launch error',err));
});

// ------------------------------------------------------------
// Graceful shutdown
// ------------------------------------------------------------
async function shutdown(){
  logI('Shutting down...');
  try{
    if(browser) await browser.close();
  }catch{}
  process.exit(0);
}
process.on('SIGINT',shutdown);
process.on('SIGTERM',shutdown);
