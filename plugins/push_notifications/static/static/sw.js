/* MeshDash Push Notifications — Service Worker v2.1 */

self.addEventListener('push', function(event) {
    if (!event.data) return;
    var data;
    try { data = event.data.json(); }
    catch(e) { data = { title:'MeshDash', body: event.data.text(), url:'/' }; }

    var ptype = data.packet_type || '';
    var isDM  = data.tag && data.tag.startsWith('meshdash-dm');
    var isSOS = data.tag === 'meshdash-sos' || ptype === 'SOS';
    var isKw  = data.tag === 'meshdash-kw';
    var isDet = data.tag && data.tag.startsWith('meshdash-det');

    var vibrate = [100];
    if (isSOS || isDet) vibrate = [300,100,300,100,300,100,600]; // urgent
    else if (isDM)      vibrate = [200,100,200,100,200];
    else if (isKw)      vibrate = [300,100,300];

    var options = {
        body:               data.body  || '',
        icon:               data.icon  || '/static/icons/favicon.ico',
        badge:              data.badge || '/static/icons/favicon.ico',
        tag:                data.tag   || 'meshdash',
        renotify:           true,
        requireInteraction: isDM || isSOS,
        silent:             false,
        vibrate:            vibrate,
        data: { url: data.url || '/', tag: data.tag || '' },
        actions: (isDM || isSOS) ? [
            { action:'open',    title:'↗ Open' },
            { action:'dismiss', title:'✕ Dismiss' },
        ] : [],
    };

    event.waitUntil(self.registration.showNotification(data.title || 'MeshDash', options));
});

self.addEventListener('notificationclick', function(event) {
    event.notification.close();
    if (event.action === 'dismiss') return;
    var url = (event.notification.data && event.notification.data.url) || '/';
    event.waitUntil(
        clients.matchAll({ type:'window', includeUncontrolled:true }).then(function(wcs) {
            for (var i=0; i<wcs.length; i++) {
                var c = wcs[i];
                if (c.url.includes(self.location.host) && 'focus' in c) {
                    c.focus();
                    if ('navigate' in c && c.url !== url) c.navigate(url);
                    return;
                }
            }
            if (clients.openWindow) return clients.openWindow(url);
        })
    );
});

self.addEventListener('install',  function(e) { e.waitUntil(self.skipWaiting()); });
self.addEventListener('activate', function(e) { e.waitUntil(clients.claim()); });
