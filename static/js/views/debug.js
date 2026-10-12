// Debug view — script stub. All rendering logic lives inline in debug.html;
// this file exists so the lazy loader has a stable entry point (matching the
// connection.js pattern) and future logic can be moved here cleanly.
window._lazyLoadView && window._lazyLoadView('debug', function () {
    // Init is handled by the inline script in debug.html (window.C2DebugApp.init)
    if (typeof window.C2DebugApp !== 'undefined' && typeof window.C2DebugApp.init === 'function') {
        // already inited by the view HTML — nothing to do
    }
});