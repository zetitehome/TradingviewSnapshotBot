// config.js
import 'dotenv/config';
import process from 'node:process';

function warn(msg){console.warn(`[ENV-WARN] ${msg}`);}
function die(msg){throw new Error(`[ENV-ERROR] ${msg}`);}
function asInt(n,d){const r=process.env[n];if(r==null||r==='')return d;const v=Number(r);if(!Number.isFinite(v)){warn(`${n} not int (${r}); ${d}`);return d;}return v;}
function asJSON(n,d={}){const r=process.env[n];if(!r)return d;try{return JSON.parse(r);}catch{warn(`${n} JSON parse fail`);return d;}}

const TELEGRAM_BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN?.trim();
if(!TELEGRAM_BOT_TOKEN) die('TELEGRAM_BOT_TOKEN required');

const TELEGRAM_CHAT_ID   = process.env.TELEGRAM_CHAT_ID?.trim() || null;
const SNAPSHOT_BASE_URL  = process.env.SNAPSHOT_BASE_URL?.trim() || 'http://localhost:10000'; // informational

const DEFAULT_EXCHANGE   = (process.env.DEFAULT_EXCHANGE || 'FX').trim();
const DEFAULT_INTERVAL   = (process.env.DEFAULT_INTERVAL || '1').trim();
const DEFAULT_THEME      = (process.env.DEFAULT_THEME || 'dark').trim();

const TV_WEBHOOK_PORT    = asInt('TV_WEBHOOK_PORT',8081);
const TV_WEBHOOK_URL     = process.env.TV_WEBHOOK_URL?.trim() || `http://localhost:${TV_WEBHOOK_PORT}/webhook`;

const UI_VISION_URL         = process.env.UI_VISION_URL?.trim() || '';
const UI_VISION_MACRO_NAME  = process.env.UI_VISION_MACRO_NAME?.trim() || 'PocketTrade';
const UI_VISION_MACRO_PARAMS= asJSON('UI_VISION_MACRO_PARAMS', {});

const ALPHA_VANTAGE_KEY  = process.env.ALPHA_VANTAGE_KEY?.trim() || '';

export const cfg = {
  TELEGRAM_BOT_TOKEN,
  TELEGRAM_CHAT_ID,
  SNAPSHOT_BASE_URL,
  DEFAULT_EXCHANGE,
  DEFAULT_INTERVAL,
  DEFAULT_THEME,
  TV_WEBHOOK_PORT,
  TV_WEBHOOK_URL,
  UI_VISION_URL,
  UI_VISION_MACRO_NAME,
  UI_VISION_MACRO_PARAMS,
  ALPHA_VANTAGE_KEY,
};
