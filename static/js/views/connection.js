// Connection view — loaded lazily by app.js loadView()
window._lazyLoadView && window._lazyLoadView('connection', function() {
    if (typeof window.initConnectionView === 'function') {
        window.initConnectionView();
    }
});
