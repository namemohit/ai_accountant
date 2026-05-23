const CACHE_NAME = 'yantrai-accounting-v57';
const ASSETS = [
  '/',
  '/login',
  '/manifest.json',
  '/static/style.css?v=39',
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
  e.respondWith(
    fetch(e.request).catch(() => caches.match(e.request))
  );
});
