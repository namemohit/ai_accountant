const CACHE_NAME = 'yantrai-accounting-v86';
const ASSETS = [
  '/',
  '/login',
  '/manifest.json',
  '/static/style.css?v=49',
  '/static/index.html'
];

// Install: precache the shell and activate immediately (don't wait for old
// tabs to close) so phones pick up new builds on the next load.
self.addEventListener('install', (e) => {
  self.skipWaiting();
  e.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(ASSETS))
      .catch(err => console.log("Cache error: ", err))
  );
});

// Activate: drop stale caches from older versions, then take control of all
// open clients right away.
self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) => Promise.all(
      keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k))
    )).then(() => self.clients.claim())
  );
});

// Network-first: always try fresh from the network, fall back to cache offline.
self.addEventListener('fetch', (e) => {
  // PWA Share Target: the OS POSTs shared files to /share-target. Stash them in a
  // dedicated cache, then redirect into the app — the front-end uploads them with
  // the logged-in user's token + active company.
  const url = new URL(e.request.url);
  if (e.request.method === 'POST' && url.pathname === '/share-target') {
    e.respondWith((async () => {
      try {
        const form = await e.request.formData();
        const files = form.getAll('files').filter((f) => f && f.size);
        const cache = await caches.open('yantrai-shared');
        for (const k of await cache.keys()) await cache.delete(k);   // clear stale
        let i = 0;
        for (const f of files) {
          await cache.put(new Request(`/__shared/${i}`), new Response(f, {
            headers: {
              'x-file-name': encodeURIComponent(f.name || `shared-${i}`),
              'content-type': f.type || 'application/octet-stream'
            }
          }));
          i++;
        }
      } catch (err) {
        console.log('share-target error:', err);
      }
      return Response.redirect('/?shared=1', 303);
    })());
    return;
  }
  e.respondWith(
    fetch(e.request).catch(() => caches.match(e.request))
  );
});
