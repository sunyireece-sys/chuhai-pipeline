#!/usr/bin/env node

const endpoint = process.env.TRACKING_WORKER_URL;
const secret = process.env.TRACKING_ADMIN_SECRET;

if (!endpoint || !secret) {
  console.error('Set TRACKING_WORKER_URL and TRACKING_ADMIN_SECRET first.');
  process.exit(1);
}

const args = Object.fromEntries(
  process.argv.slice(2).map((arg) => {
    const [key, ...rest] = arg.replace(/^--/, '').split('=');
    return [key, rest.join('=') || ''];
  })
);

const buyerId = Number(args.buyer_id || args.buyer || Date.now());
const payload = {
  buyer_id: buyerId,
  company_name: args.company || '',
  email_addr: args.email || '',
  campaign_id: args.campaign || new Date().toISOString().slice(0, 10),
  product_slug: args.product || '',
  destination_path: args.path || '',
  metadata: {
    source: 'manual-cli'
  }
};

const response = await fetch(endpoint.replace(/\/$/, '') + '/admin/tokens', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
    'X-API-Key': secret
  },
  body: JSON.stringify(payload)
});

const text = await response.text();
if (!response.ok) {
  console.error(text);
  process.exit(1);
}

const data = JSON.parse(text);
console.log(JSON.stringify(data, null, 2));
