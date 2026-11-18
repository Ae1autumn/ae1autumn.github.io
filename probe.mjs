import { chromium } from 'playwright';

const out = (...a) => console.log(...a);

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });

page.on('console', (m) => {
  const t = m.text();
  if (/giscus|goatcounter|site-data|error/i.test(t)) out('CONSOLE', m.type(), t.slice(0, 300));
});
page.on('pageerror', (e) => out('PAGEERROR', e.message.slice(0, 300)));
page.on('requestfailed', (r) => out('REQFAIL', r.url().slice(0, 140), r.failure()?.errorText));

await page.goto('https://ae1.aermium.com/', { waitUntil: 'load', timeout: 60000 });
await page.waitForTimeout(8000);

out('HOME_STATE ' + JSON.stringify(await page.evaluate(() => ({
  gc: window.GOATCOUNTER,
  giscus: window.GISCUS,
  err: String(window.__siteDataError || ''),
  totalClass: (document.getElementById('feed-total') || {}).className,
  totalText: (document.getElementById('feed-total') || {}).textContent,
  feedCount: (document.getElementById('feed-count') || {}).textContent,
  views: Array.from(document.querySelectorAll('.views-num')).map((e) => e.textContent),
}))));

await page.goto('https://ae1.aermium.com/#/post/issue-22', { waitUntil: 'load', timeout: 60000 });
await page.waitForTimeout(5000);
out('POST_OPEN ' + JSON.stringify(await page.evaluate(() => ({
  hash: location.hash,
  loaded: !!document.querySelector('.post-body'),
}))));

await page.click('#btn-comments');
await page.waitForTimeout(9000);

out('COMMENTS ' + JSON.stringify(await page.evaluate(() => {
  const host = document.getElementById('giscus-host');
  const fb = document.getElementById('comments-fallback');
  const f = host && host.querySelector('iframe.giscus-frame');
  return {
    hostHidden: host ? host.hidden : 'missing',
    hasIframe: !!f,
    src: f ? f.src.slice(0, 180) : null,
    fallbackHidden: fb ? fb.hidden : 'missing',
    fallbackText: fb ? fb.textContent.trim().slice(0, 80) : null,
  };
})));

await page.screenshot({ path: 'comments.png' });
await browser.close();
