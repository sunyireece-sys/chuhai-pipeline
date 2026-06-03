const TRANSPARENT_GIF = Uint8Array.from(
  atob('R0lGODlhAQABAPAAAP///wAAACH5BAAAAAAALAAAAAABAAEAAAICRAEAOw=='),
  function (char) { return char.charCodeAt(0); }
);

const KNOWN_EVENTS = new Set([
  'email_click',
  'token_identify',
  'expired_click',
  'unknown_token',
  'tracking_pixel',
  'page_view',
  'product_view',
  'product_click',
  'product_family_click',
  'cta_click',
  'cert_open',
  'mailto_click',
  'tel_click',
  'form_submit',
  'match_started',
  'match_step_answered',
  'match_abandoned',
  'match_completed',
  'recommendation_viewed',
  'brief_submitted',
  'brief_note_added',
  'dwell_30s',
  'dwell_60s',
  'dwell',
  'outbound_click'
]);

const BOT_PATTERNS = [
  ['googlebot', 'googlebot'],
  ['googleimageproxy', 'google-image-proxy'],
  ['bingbot', 'bingbot'],
  ['bingpreview', 'bing-preview'],
  ['slackbot', 'slackbot'],
  ['discordbot', 'discordbot'],
  ['facebookexternalhit', 'facebook-preview'],
  ['linkedinbot', 'linkedinbot'],
  ['twitterbot', 'twitterbot'],
  ['telegrambot', 'telegrambot'],
  ['whatsapp', 'whatsapp-preview'],
  ['mimecast', 'mimecast'],
  ['proofpoint', 'proofpoint'],
  ['barracuda', 'barracuda'],
  ['safelinks', 'safe-links'],
  ['urlscan', 'urlscan'],
  ['ahrefsbot', 'ahrefs'],
  ['semrushbot', 'semrush'],
  ['petalbot', 'petalbot'],
  ['bytespider', 'bytespider'],
  ['crawler', 'generic-crawler'],
  ['spider', 'generic-spider'],
  ['headless', 'headless-browser'],
  ['python-requests', 'script-client'],
  ['curl/', 'script-client'],
  ['wget/', 'script-client'],
  ['go-http-client', 'script-client']
];

import { systemPrompt } from './grounding.mjs';

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: corsHeaders(request, env) });
    }

    try {
      if (url.pathname === '/t.gif') {
        return await handlePixel(request, env);
      }

      if (url.pathname.startsWith('/t/') || url.pathname.startsWith('/c/')) {
        return await handleTokenRedirect(request, env);
      }

      if (url.pathname === '/api/identify' && request.method === 'POST') {
        return await handleIdentify(request, env);
      }

      if (url.pathname === '/api/event' && request.method === 'POST') {
        const resp = await handleEvent(request, env);
        ctx.waitUntil(syncEvents(env).catch((e) => console.error('inline_sync_failed', e)));
        return resp;
      }

      if (url.pathname === '/api/chat' && request.method === 'POST') {
        return await handleChat(request, env);
      }

      if (isMatchApiPath(url.pathname) && request.method === 'POST') {
        return await handleMatchApiProxy(request, env);
      }

      if (url.pathname === '/admin/tokens' && request.method === 'POST') {
        return await handleAdminTokens(request, env);
      }

      if (url.pathname === '/admin/events' && request.method === 'GET') {
        return await handleAdminEvents(request, env);
      }

      if (url.pathname === '/admin/sync' && request.method === 'POST') {
        requireAdmin(request, env);
        const result = await syncEvents(env);
        return json(result, 200, request, env);
      }

      const assetResp = await env.ASSETS.fetch(request);
      const newHeaders = new Headers(assetResp.headers);
      newHeaders.set('X-Robots-Tag', 'noindex, nofollow');
      return new Response(assetResp.body, { status: assetResp.status, headers: newHeaders });
    } catch (error) {
      console.error('tracking_worker_error', error && error.stack ? error.stack : error);
      const status = error && error.status ? error.status : 500;
      return json({ ok: false, error: status === 500 ? 'internal_error' : error.message }, status, request, env);
    }
  },

  async scheduled(_event, env, ctx) {
    ctx.waitUntil(syncEvents(env));
  }
};

async function handlePixel(request, env) {
  const url = new URL(request.url);
  const eventType = normalizeEventType(url.searchParams.get('e') || 'tracking_pixel');
  await recordEvent(request, env, {
    event_type: eventType,
    url: url.searchParams.get('u') || request.url,
    page_title: url.searchParams.get('pt') || '',
    token: url.searchParams.get('token') || url.searchParams.get('t') || '',
    campaign_id: url.searchParams.get('campaign') || '',
    payload: queryPayload(url)
  });
  return new Response(TRANSPARENT_GIF, {
    status: 200,
    headers: {
      'Content-Type': 'image/gif',
      'Cache-Control': 'no-store, no-cache, must-revalidate, proxy-revalidate',
      'Pragma': 'no-cache',
      'Expires': '0'
    }
  });
}

async function handleTokenRedirect(request, env) {
  const url = new URL(request.url);
  const token = decodeURIComponent(url.pathname.slice(3)).trim();
  const result = await identifyByToken(request, env, token, {
    eventType: 'email_click',
    landingUrl: request.url,
    visitorId: url.searchParams.get('vid') || '',
    source: 'redirect'
  });

  const destination = result.destination || siteUrl(env);
  const headers = new Headers({ Location: destination, 'Cache-Control': 'no-store' });
  appendIdentityCookies(headers, request, env, result);
  return new Response(null, { status: result.ok ? 302 : 302, headers });
}

async function handleIdentify(request, env) {
  const body = await readJson(request);
  const token = String(body.token || '').trim();
  const result = await identifyByToken(request, env, token, {
    eventType: 'token_identify',
    landingUrl: body.url || request.url,
    visitorId: body.visitor_id || '',
    source: 'url_param'
  });
  const headers = corsHeaders(request, env);
  appendIdentityCookies(headers, request, env, result);
  return json({
    ok: result.ok,
    identified: result.ok && !result.is_bot,
    destination: result.destination || null,
    expired: result.expired || false
  }, result.ok ? 200 : 404, request, env, headers);
}

async function handleEvent(request, env) {
  const body = await readJson(request);
  const result = await recordEvent(request, env, body);
  const headers = corsHeaders(request, env);
  if (result.set_session_cookie) {
    headers.append('Set-Cookie', buildCookie('_rv_sess', result.session_id, request, env, true));
  }
  if (result.buyer_id && !parseCookies(request.headers.get('Cookie'))._rv_bid) {
    headers.append('Set-Cookie', buildCookie('_rv_bid', String(result.buyer_id), request, env, true));
  }
  return json({ ok: true, id: result.id }, 200, request, env, headers);
}

function isMatchApiPath(pathname) {
  return pathname === '/api/brief' ||
    pathname === '/api/brief/note' ||
    pathname.startsWith('/api/match/');
}

async function handleMatchApiProxy(request, env) {
  if (!env.MATCH_API_BASE_URL) {
    throw httpError(503, 'match_api_not_configured');
  }
  const body = await readJson(request);
  const cookies = parseCookies(request.headers.get('Cookie'));
  const attribution = await getAttribution(env, cookies._rv_sess || body.session_id || '');
  const enriched = Object.assign({}, body, {
    buyer_id: body.buyer_id || cookies._rv_bid || (attribution && attribution.buyer_id) || '',
    token: body.token || cookies._rv_token || (attribution && attribution.token) || '',
    campaign_id: body.campaign_id || cookies._rv_campaign || (attribution && attribution.campaign_id) || '',
    profile_slug: body.profile_slug || (attribution && attribution.profile_slug) || '',
    run_id: body.run_id || (attribution && attribution.run_id) || '',
    company_name: body.company_name || (attribution && attribution.company_name) || '',
    session_id: body.session_id || cookies._rv_sess || '',
    visitor_id: body.visitor_id || body.client_id || (attribution && attribution.visitor_id) || ''
  });

  const url = new URL(request.url);
  const upstreamUrl = env.MATCH_API_BASE_URL.replace(/\/$/, '') + url.pathname;
  const headers = new Headers({ 'Content-Type': 'application/json' });
  if (env.MATCH_API_SECRET) headers.set('X-API-Key', env.MATCH_API_SECRET);
  const response = await fetch(upstreamUrl, {
    method: 'POST',
    headers,
    body: JSON.stringify(enriched)
  });
  const responseHeaders = corsHeaders(request, env);
  responseHeaders.set('Content-Type', response.headers.get('Content-Type') || 'application/json; charset=utf-8');
  responseHeaders.set('Cache-Control', 'no-store');
  return new Response(await response.text(), {
    status: response.status,
    headers: responseHeaders
  });
}

async function handleAdminTokens(request, env) {
  requireAdmin(request, env);
  const body = await readJson(request);
  const items = Array.isArray(body) ? body : [body];
  const results = [];
  for (const item of items) {
    results.push(await upsertToken(env, item));
  }
  return json({ ok: true, tokens: results }, 200, request, env);
}

async function handleAdminEvents(request, env) {
  requireAdmin(request, env);
  const url = new URL(request.url);
  const limit = clamp(parseInt(url.searchParams.get('limit') || '100', 10), 1, 1000);
  const buyerId = url.searchParams.get('buyer_id');
  let stmt;
  if (buyerId) {
    stmt = env.DB.prepare('SELECT * FROM events WHERE buyer_id = ? ORDER BY id DESC LIMIT ?').bind(Number(buyerId), limit);
  } else {
    stmt = env.DB.prepare('SELECT * FROM events ORDER BY id DESC LIMIT ?').bind(limit);
  }
  const rows = await stmt.all();
  return json({ ok: true, events: rows.results || [] }, 200, request, env);
}

async function identifyByToken(request, env, token, options) {
  const now = nowTs();
  const ua = request.headers.get('User-Agent') || '';
  const bot = detectBot(ua);
  const cookies = parseCookies(request.headers.get('Cookie'));
  const sessionId = cookies._rv_sess || crypto.randomUUID();
  const visitorId = options.visitorId || '';
  const tokenRow = await getToken(env, token);
  const expired = tokenRow && tokenRow.expires_at && Number(tokenRow.expires_at) < now;

  if (!tokenRow || expired || tokenRow.status !== 'active') {
    await recordEvent(request, env, {
      event_type: expired ? 'expired_click' : 'unknown_token',
      url: options.landingUrl,
      token,
      visitor_id: visitorId,
      payload: { source: options.source, expired: !!expired }
    }, { sessionId, forceAnonymous: true });
    return {
      ok: false,
      expired: !!expired,
      session_id: sessionId,
      visitor_id: visitorId,
      destination: siteUrl(env)
    };
  }

  const updateStmt = bot.isBot
    ? env.DB.prepare(
      'UPDATE email_tokens SET last_click_ts = ?, bot_click_count = bot_click_count + 1, updated_at = ? WHERE token = ?'
    ).bind(now, now, token)
    : env.DB.prepare(
      'UPDATE email_tokens SET first_click_ts = COALESCE(first_click_ts, ?), last_click_ts = ?, click_count = click_count + 1, updated_at = ? WHERE token = ?'
    ).bind(now, now, now, token);

  await updateStmt.run();

  if (bot.isBot) {
    await recordEvent(request, env, {
      event_type: options.eventType,
      url: options.landingUrl,
      token,
      campaign_id: tokenRow.campaign_id,
      profile_slug: tokenRow.profile_slug || '',
      run_id: tokenRow.run_id || '',
      visitor_id: visitorId,
      payload: {
        source: options.source,
        bot_reason: bot.reason,
        product_slug: tokenRow.product_slug || '',
        destination_path: tokenRow.destination_path || ''
      }
    }, { sessionId, attribution: tokenRow });
    return {
      ok: true,
      is_bot: true,
      no_cookie: true,
      destination: destinationForToken(tokenRow, env)
    };
  }

  await upsertAttribution(env, {
    session_id: sessionId,
    visitor_id: visitorId,
    buyer_id: tokenRow.buyer_id,
    token,
    company_name: tokenRow.company_name,
    email_addr: tokenRow.email_addr,
    campaign_id: tokenRow.campaign_id,
    profile_slug: tokenRow.profile_slug,
    run_id: tokenRow.run_id,
    source: options.source,
    country: request.headers.get('CF-IPCountry') || '',
    user_agent: ua
  });

  await recordEvent(request, env, {
    event_type: options.eventType,
    url: options.landingUrl,
    token,
    campaign_id: tokenRow.campaign_id,
    profile_slug: tokenRow.profile_slug || '',
    run_id: tokenRow.run_id || '',
    visitor_id: visitorId,
    payload: {
      source: options.source,
      profile_slug: tokenRow.profile_slug || '',
      run_id: tokenRow.run_id || '',
      product_slug: tokenRow.product_slug || '',
      destination_path: tokenRow.destination_path || ''
    }
  }, { sessionId, attribution: tokenRow });

  return {
    ok: true,
    is_bot: bot.isBot,
    session_id: sessionId,
    visitor_id: visitorId,
    buyer_id: tokenRow.buyer_id,
    token,
    campaign_id: tokenRow.campaign_id,
    destination: destinationForToken(tokenRow, env)
  };
}

async function recordEvent(request, env, body, options = {}) {
  const now = nowTs();
  const cookies = parseCookies(request.headers.get('Cookie'));
  const sessionId = options.sessionId || cookies._rv_sess || body.session_id || crypto.randomUUID();
  const setSessionCookie = !cookies._rv_sess;
  const visitorId = safeText(body.visitor_id || body.client_id || '', 128);
  const ua = request.headers.get('User-Agent') || '';
  const bot = detectBot(ua);
  const attribution = options.attribution || await getAttribution(env, sessionId);
  const buyerId = options.forceAnonymous ? null : coerceInt(cookies._rv_bid) || coerceInt(body.buyer_id) || coerceInt(attribution && attribution.buyer_id);
  const token = safeText(body.token || cookies._rv_token || (attribution && attribution.token) || '', 128);
  const campaignId = safeText(body.campaign_id || cookies._rv_campaign || (attribution && attribution.campaign_id) || '', 128);
  const profileSlug = safeText(body.profile_slug || (attribution && attribution.profile_slug) || '', 160);
  const runId = safeText(body.run_id || (attribution && attribution.run_id) || '', 160);
  const eventType = normalizeEventType(body.event_type);
  const eventUrl = safeUrl(body.url || request.headers.get('Referer') || request.url);
  const parsed = parseMaybeUrl(eventUrl);
  const pagePath = safeText(body.page_path || (parsed ? parsed.pathname : ''), 512);
  const pageTitle = safeText(body.page_title || '', 256);
  const ip = request.headers.get('CF-Connecting-IP') || '';
  const ipHash = await hashIp(ip, env);
  const result = await env.DB.prepare(
    `INSERT INTO events (
      ts, session_id, visitor_id, buyer_id, token, campaign_id, event_type,
      profile_slug, run_id, url, page_path, page_title, referrer, payload_json, ip_prefix, ip_hash,
      country, colo, user_agent, is_bot, bot_reason
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
  ).bind(
    now,
    sessionId,
    visitorId || null,
    buyerId || null,
    token || null,
    campaignId || null,
    eventType,
    profileSlug || null,
    runId || null,
    eventUrl,
    pagePath,
    pageTitle,
    safeUrl(body.referrer || request.headers.get('Referer') || ''),
    safeJson(body.payload || {}),
    ipPrefix(ip),
    ipHash,
    request.headers.get('CF-IPCountry') || '',
    request.cf && request.cf.colo ? String(request.cf.colo) : '',
    safeText(ua, 512),
    bot.isBot ? 1 : 0,
    bot.reason || null
  ).run();

  if (attribution && attribution.session_id) {
    await env.DB.prepare('UPDATE company_attribution SET last_seen_ts = ? WHERE session_id = ?')
      .bind(now, sessionId).run();
  }

  return {
    id: result.meta && result.meta.last_row_id ? result.meta.last_row_id : null,
    session_id: sessionId,
    buyer_id: buyerId,
    set_session_cookie: setSessionCookie
  };
}

async function upsertToken(env, item) {
  const now = nowTs();
  const token = normalizeToken(item.token || crypto.randomUUID().replace(/-/g, '').slice(0, 16));
  const buyerId = Number(item.buyer_id);
  if (!Number.isFinite(buyerId)) throw httpError(400, 'buyer_id is required');
  const sentTs = coerceInt(item.sent_ts) || now;
  const ttlDays = coerceInt(item.ttl_days) || coerceInt(env.TOKEN_TTL_DAYS) || 14;
  const expiresAt = coerceInt(item.expires_at) || sentTs + ttlDays * 86400;

  await env.DB.prepare(
    `INSERT INTO email_tokens (
      token, buyer_id, company_name, email_addr, campaign_id, product_slug,
      destination_path, profile_slug, run_id, sent_ts, expires_at, status, metadata_json, created_at, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(token) DO UPDATE SET
      buyer_id = excluded.buyer_id,
      company_name = excluded.company_name,
      email_addr = excluded.email_addr,
      campaign_id = excluded.campaign_id,
      product_slug = excluded.product_slug,
      destination_path = excluded.destination_path,
      profile_slug = excluded.profile_slug,
      run_id = excluded.run_id,
      sent_ts = excluded.sent_ts,
      expires_at = excluded.expires_at,
      status = excluded.status,
      metadata_json = excluded.metadata_json,
      updated_at = excluded.updated_at`
  ).bind(
    token,
    buyerId,
    safeText(item.company_name || '', 256),
    safeText(item.email_addr || '', 256),
    safeText(item.campaign_id || '', 128),
    safeSlug(item.product_slug || ''),
    safePath(item.destination_path || ''),
    safeText(item.profile_slug || '', 160),
    safeText(item.run_id || '', 160),
    sentTs,
    expiresAt,
    safeText(item.status || 'active', 24),
    safeJson(item.metadata || {}),
    now,
    now
  ).run();

  return {
    token,
    buyer_id: buyerId,
    tracking_url: siteUrl(env).replace(/\/$/, '') + '/t/' + encodeURIComponent(token),
    expires_at: expiresAt
  };
}

async function upsertAttribution(env, row) {
  const now = nowTs();
  await env.DB.prepare(
    `INSERT INTO company_attribution (
      session_id, visitor_id, buyer_id, token, company_name, email_addr, campaign_id,
      profile_slug, run_id, first_seen_ts, last_seen_ts, source, confidence, country, user_agent, metadata_json
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(session_id) DO UPDATE SET
      visitor_id = COALESCE(excluded.visitor_id, company_attribution.visitor_id),
      buyer_id = excluded.buyer_id,
      token = excluded.token,
      company_name = excluded.company_name,
      email_addr = excluded.email_addr,
      campaign_id = excluded.campaign_id,
      profile_slug = excluded.profile_slug,
      run_id = excluded.run_id,
      last_seen_ts = excluded.last_seen_ts,
      source = excluded.source,
      country = excluded.country,
      user_agent = excluded.user_agent,
      metadata_json = excluded.metadata_json`
  ).bind(
    row.session_id,
    row.visitor_id || null,
    row.buyer_id || null,
    row.token || null,
    row.company_name || null,
    row.email_addr || null,
    row.campaign_id || null,
    row.profile_slug || null,
    row.run_id || null,
    now,
    now,
    row.source,
    100,
    row.country || null,
    safeText(row.user_agent || '', 512),
    safeJson(row.metadata || {})
  ).run();
}

async function getToken(env, token) {
  if (!token) return null;
  return env.DB.prepare('SELECT * FROM email_tokens WHERE token = ?').bind(token).first();
}

async function getAttribution(env, sessionId) {
  if (!sessionId) return null;
  return env.DB.prepare('SELECT * FROM company_attribution WHERE session_id = ?').bind(sessionId).first();
}

async function syncEvents(env) {
  if (!env.ECS_WEBHOOK_URL) return { ok: true, skipped: true, reason: 'ECS_WEBHOOK_URL not configured' };
  const limit = clamp(coerceInt(env.EVENT_SYNC_LIMIT) || 500, 1, 1000);
  const rows = await env.DB.prepare('SELECT * FROM events WHERE synced = 0 ORDER BY id LIMIT ?').bind(limit).all();
  const events = rows.results || [];
  if (!events.length) return { ok: true, synced: 0 };

  const headers = new Headers({ 'Content-Type': 'application/json' });
  if (env.ECS_API_SECRET) headers.set('X-API-Key', env.ECS_API_SECRET);
  const response = await fetch(env.ECS_WEBHOOK_URL, {
    method: 'POST',
    headers,
    body: JSON.stringify({
      source: 'redvia-tracking-worker',
      sent_at: nowTs(),
      events
    })
  });

  if (!response.ok) {
    return { ok: false, synced: 0, status: response.status };
  }

  const syncedAt = nowTs();
  const updates = events.map(function (event) {
    return env.DB.prepare('UPDATE events SET synced = 1, synced_at = ? WHERE id = ?').bind(syncedAt, event.id);
  });
  await env.DB.batch(updates);
  return { ok: true, synced: events.length };
}

function destinationForToken(row, env) {
  const base = siteUrl(env).replace(/\/$/, '');
  if (row.destination_path) return base + safePath(row.destination_path);
  if (row.product_slug) return base + '/ingredients/' + safeSlug(row.product_slug) + '.html';
  return base + '/';
}

function appendIdentityCookies(headers, request, env, result) {
  if (result.no_cookie) return;
  if (!result.session_id) return;
  headers.append('Set-Cookie', buildCookie('_rv_sess', result.session_id, request, env, true));
  if (result.buyer_id) headers.append('Set-Cookie', buildCookie('_rv_bid', String(result.buyer_id), request, env, true));
  if (result.token) headers.append('Set-Cookie', buildCookie('_rv_token', result.token, request, env, true));
  if (result.campaign_id) headers.append('Set-Cookie', buildCookie('_rv_campaign', result.campaign_id, request, env, true));
}

function buildCookie(name, value, request, env, httpOnly) {
  const maxAge = coerceInt(env.COOKIE_MAX_AGE) || 7776000;
  const parts = [
    name + '=' + encodeURIComponent(value),
    'Path=/',
    'Max-Age=' + maxAge,
    'SameSite=Lax'
  ];
  if (new URL(request.url).protocol === 'https:') parts.push('Secure');
  if (httpOnly) parts.push('HttpOnly');
  if (env.COOKIE_DOMAIN) parts.push('Domain=' + env.COOKIE_DOMAIN);
  return parts.join('; ');
}

function parseCookies(header) {
  const result = {};
  if (!header) return result;
  header.split(';').forEach(function (part) {
    const idx = part.indexOf('=');
    if (idx === -1) return;
    const key = part.slice(0, idx).trim();
    const value = part.slice(idx + 1).trim();
    if (key) result[key] = decodeURIComponent(value);
  });
  return result;
}

function corsHeaders(request, env) {
  const headers = new Headers({
    'Access-Control-Allow-Methods': 'POST, GET, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type, X-API-Key',
    'Access-Control-Allow-Credentials': 'true',
    'Vary': 'Origin'
  });
  const origin = request.headers.get('Origin');
  if (origin && allowedOrigins(env).has(origin)) {
    headers.set('Access-Control-Allow-Origin', origin);
  }
  return headers;
}

function allowedOrigins(env) {
  const set = new Set();
  if (env.SITE_URL) set.add(env.SITE_URL.replace(/\/$/, ''));
  String(env.ALLOWED_ORIGINS || '').split(',').forEach(function (origin) {
    if (origin.trim()) set.add(origin.trim().replace(/\/$/, ''));
  });
  return set;
}

function requireAdmin(request, env) {
  if (!env.ADMIN_API_SECRET) throw httpError(503, 'ADMIN_API_SECRET is not configured');
  const provided = request.headers.get('X-API-Key') || request.headers.get('Authorization')?.replace(/^Bearer\s+/i, '') || '';
  if (provided !== env.ADMIN_API_SECRET) throw httpError(401, 'unauthorized');
}

async function readJson(request) {
  try {
    return await request.json();
  } catch (_error) {
    throw httpError(400, 'invalid_json');
  }
}

function json(data, status, request, env, extraHeaders) {
  const headers = extraHeaders || corsHeaders(request, env);
  headers.set('Content-Type', 'application/json; charset=utf-8');
  headers.set('Cache-Control', 'no-store');
  return new Response(JSON.stringify(data), { status, headers });
}

function httpError(status, message) {
  const error = new Error(message);
  error.status = status;
  return error;
}

function detectBot(userAgent) {
  const ua = String(userAgent || '').toLowerCase();
  if (!ua) return { isBot: true, reason: 'missing-ua' };
  for (const item of BOT_PATTERNS) {
    if (ua.indexOf(item[0]) !== -1) return { isBot: true, reason: item[1] };
  }
  return { isBot: false, reason: '' };
}

function normalizeEventType(value) {
  const type = safeText(value || 'page_view', 64).replace(/[^a-zA-Z0-9_:-]/g, '_');
  return KNOWN_EVENTS.has(type) ? type : 'outbound_click';
}

function normalizeToken(value) {
  const token = String(value || '').trim();
  if (!/^[A-Za-z0-9_-]{6,80}$/.test(token)) throw httpError(400, 'invalid_token');
  return token;
}

function safeSlug(value) {
  return String(value || '').trim().toLowerCase().replace(/[^a-z0-9-]/g, '').slice(0, 120);
}

function safePath(value) {
  const path = String(value || '').trim();
  if (!path) return '';
  if (!path.startsWith('/')) return '/' + path.replace(/^\/+/, '');
  if (path.startsWith('//') || path.indexOf('://') !== -1) return '';
  return path.slice(0, 512);
}

function safeText(value, max) {
  return String(value || '').replace(/\s+/g, ' ').trim().slice(0, max);
}

function safeUrl(value) {
  return String(value || '').trim().slice(0, 1200);
}

function safeJson(value) {
  try {
    return JSON.stringify(value == null ? {} : value).slice(0, 12000);
  } catch (_error) {
    return '{}';
  }
}

function parseMaybeUrl(value) {
  try {
    return new URL(value);
  } catch (_error) {
    return null;
  }
}

function coerceInt(value) {
  const parsed = parseInt(value, 10);
  return Number.isFinite(parsed) ? parsed : 0;
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function nowTs() {
  return Math.floor(Date.now() / 1000);
}

function siteUrl(env) {
  return String(env.SITE_URL || 'https://redvia.com').replace(/\/$/, '');
}

function queryPayload(url) {
  const payload = {};
  url.searchParams.forEach(function (value, key) {
    if (!['e', 'u', 'pt', 'token', 't', 'campaign'].includes(key)) {
      payload[key] = value;
    }
  });
  return payload;
}

function ipPrefix(ip) {
  if (!ip) return '';
  if (ip.indexOf('.') !== -1) {
    const parts = ip.split('.');
    if (parts.length === 4) return parts.slice(0, 3).join('.') + '.0';
  }
  if (ip.indexOf(':') !== -1) {
    return ip.split(':').slice(0, 4).join(':') + '::';
  }
  return '';
}

async function hashIp(ip, env) {
  if (!ip) return '';
  const salt = env.IP_HASH_SALT || '';
  const input = new TextEncoder().encode(salt + ip);
  const digest = await crypto.subtle.digest('SHA-256', input);
  return Array.from(new Uint8Array(digest)).map(function (byte) {
    return byte.toString(16).padStart(2, '0');
  }).join('');
}

async function handleChat(request, env) {
  if (!env.LLM_API_KEY) throw httpError(503, 'LLM_API_KEY not configured');
  const body = await readJson(request);
  const context = String(body.context || 'home');
  const productName = String(body.productName || '').slice(0, 80);
  const history = (Array.isArray(body.messages) ? body.messages : []).slice(-12);

  const msgs = [{ role: 'system', content: systemPrompt(context, productName) }];
  for (const m of history) {
    if (m && (m.role === 'user' || m.role === 'assistant') && m.content) {
      msgs.push({ role: m.role, content: String(m.content).slice(0, 2000) });
    }
  }

  const apiBase = env.LLM_BASE_URL || 'https://open.bigmodel.cn/api/paas/v4';
  const model = env.LLM_MODEL || 'glm-4-plus';
  const resp = await fetch(`${apiBase}/chat/completions`, {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${env.LLM_API_KEY}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ model, messages: msgs, temperature: 0.3, max_tokens: 260 }),
  });
  if (!resp.ok) {
    const errText = await resp.text();
    return json({ error: `llm_error: ${resp.status} ${errText.slice(0, 500)}` }, 502, request, env);
  }
  const data = await resp.json();
  let text = (data && data.choices && data.choices[0] && data.choices[0].message && data.choices[0].message.content) || '';
  const offer = text.includes('[[OFFER_BRIEF]]');
  text = text.replace('[[OFFER_BRIEF]]', '').trim();
  return json({ reply: text, offerBrief: offer }, 200, request, env);
}
